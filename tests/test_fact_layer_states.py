# SPDX-License-Identifier: AGPL-3.0-or-later
"""The per-adapter contract: a failed lookup is ``Unknown``, never ``Absent``.

``Absent`` is a privileged state. It means "we looked, there is genuinely none", and
the keep lane acts on it by withdrawing protection (``signals.evaluate_keep``, and see
``test_engine_invariants.test_an_absent_keep_field_withdraws_its_keep_and_that_is_deliberate``
for why that is correct). ``Known(0)`` is worse still: an affirmative zero is maximum
condemnation pressure on several signals.

So the safety of the whole score lane rests on a contract these tests pin: **no source
failure, and no missing identifier, may ever surface as ``Absent`` or as ``Known(0)``.**
Every one of these is a place where the fact builder must be able to tell "we asked and
the answer was none" from "we never got to ask".
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from reaper.engine.observation import Absent, Known, Unknown
from reaper.engine.policy import DEFAULT_MOVIE_POLICY
from reaper.services import lists
from reaper.services.snapshot import RawItem, ScanContext, _reported_size, build_facts

_EMPTY_INDEX = lists.MembershipIndex({}, {}, {})


def _raw(**overrides: object) -> RawItem:
    """A movie Radarr knows about and Plex has matched, with nothing else stipulated."""
    base: dict[str, object] = {
        "media_key": "radarr:1:1",
        "title": "A title",
        "media_type": "movie",
        "size_bytes": 8_000_000_000,
        "imdb_id": "tt0000001",
        "tmdb_id": 1,
        "plex_rating_key": 10,
        "added_at": datetime(2020, 1, 1, tzinfo=UTC),
        "has_file": True,
    }
    base.update(overrides)
    return RawItem(**base)  # type: ignore[arg-type]


def _facts(item: RawItem, *, imdb: dict[str, object] | None = None):
    return build_facts(
        item,
        ScanContext(horizon=datetime(2019, 1, 1, tzinfo=UTC)),
        membership_index=_EMPTY_INDEX,
        imdb=imdb or {},  # type: ignore[arg-type]
        last_played={},
        watchers_window={10: 0},
        watchers_all_time={10: 0},
        whitelisted=set(),
    )


class TestARatingWeCouldNotLookUpIsUnknown:
    """``display_meta.dataset_entry`` returns ``None`` for two different stories."""

    def test_a_movie_with_no_imdb_id_at_all_has_an_unknown_rating(self) -> None:
        """No id from Radarr and no id from Plex means we never performed a lookup.

        Recording that as ``Absent`` tells the keep lane "this title has no IMDb
        rating", which withdraws every rating-based keep, leaves coverage reading
        100%, and does not degrade the snapshot. Nothing anywhere else in the scan
        reports that this item was never checked.
        """
        facts = _facts(_raw(imdb_id=None, plex_imdb_id=None))

        assert isinstance(facts.imdb_rating_tenths, Unknown)
        assert isinstance(facts.imdb_votes, Unknown)

    def test_a_movie_we_did_look_up_and_did_not_find_is_absent(self) -> None:
        """The other story, and the one ``Absent`` is for. This must keep working:
        a title genuinely missing from the dataset is not protected by a rating keep,
        because it is unrated, not well rated."""
        facts = _facts(_raw(imdb_id="tt0000001"), imdb={})

        assert isinstance(facts.imdb_rating_tenths, Absent)


class TestASizeWeCouldNotReadIsUnknown:
    """Two halves, and both have to hold: the Radarr payload must not manufacture a
    zero, and ``build_facts`` must not wrap one in ``Known``."""

    @pytest.mark.parametrize("payload", [{}, {"sizeOnDisk": 0}, {"sizeOnDisk": None}])
    def test_a_movie_with_no_reported_size_reads_as_none(self, payload: dict[str, object]) -> None:
        """``hasFile`` true with no usable ``sizeOnDisk`` is a partial payload, not a
        0-byte file."""
        assert _reported_size(payload) is None

    def test_a_reported_size_reads_as_itself(self) -> None:
        assert _reported_size({"sizeOnDisk": 8_000_000_000}) == 8_000_000_000

    def test_an_unreadable_size_reaches_the_score_as_unknown(self) -> None:
        """As ``Known(0)`` it would read as a real measurement: maximum pressure on a
        size signal, and any "keep large files" rule silently stops protecting it."""
        facts = _facts(_raw(size_bytes=None, has_file=True))

        assert isinstance(facts.size_bytes, Unknown)

    def test_a_real_size_stays_known(self) -> None:
        facts = _facts(_raw(size_bytes=8_000_000_000))

        assert facts.size_bytes == Known(value=8_000_000_000, source="radarr")


class TestWatchCountsFromAStaleMirrorAreNotZero:
    """Watch stats are read from the local ``watch_event`` cache, not live.

    So a Tautulli ingest that stopped a month ago does not raise, does not degrade
    the snapshot, and does not look any different from a genuinely quiet library:
    ``watchers_window.get(rating_key, 0)`` keeps returning an affirmative
    ``Known(0)`` while dormancy grows against a frozen mirror. Every item drifts
    toward condemnation at exactly the rate the outage lasts.
    """

    # The staleness behaviour itself is pinned by driving the real scan, in
    # tests/test_scan_pipeline.py::TestAStaleMirrorDegradesTheSnapshot, and the two clocks
    # are held apart in tests/test_history_sync.py, by
    # TestTheIngestClockIsSeparateFromTheWatchingClock.
    # A `hasattr` check used to sit here naming `latest()` as the signal that tells a quiet
    # library from a stalled ingest. It was wrong on both counts: the shipped guard reads
    # `last_synced_at`, and asserting on a name passes on a broken body and fails on a
    # rename (the anti-pattern H-1 was raised about).

    def test_the_staleness_bound_is_two_nightly_cycles(self) -> None:
        """Pinned so it cannot drift silently. Tighter and a paused ingest blocks every
        scan; looser and items drift toward condemnation for the length of the outage."""
        from reaper.services import snapshot

        assert timedelta(hours=48) == snapshot.MIRROR_STALE_AFTER


class TestAPolicyVersionBumpCannotBrickTheEditor:
    """``schema_version``/``scorer_version`` are read back through
    ``PolicyBody.model_validate_json`` on both the scan path and ``GET /api/policy``,
    and that call site has no fallback. Pinned to a single ``Literal``, the next bump
    would fail every stored body at once, taking out the policy editor along with the
    scan: the operator could not even open the page to fix it."""

    def test_a_body_written_by_an_older_reaper_still_loads(self) -> None:
        from reaper.engine.policy import DEFAULT_MOVIE_POLICY, PolicyBody

        older = DEFAULT_MOVIE_POLICY.model_dump()
        older["schema_version"] = 1
        older["scorer_version"] = 1

        assert PolicyBody.model_validate(older).schema_version == 1

    def test_a_body_written_by_a_newer_reaper_is_refused(self) -> None:
        """The other direction stays closed. A body from a future Reaper may mean things
        this build cannot interpret, and guessing at a policy is guessing at deletions."""
        import pydantic

        from reaper.engine.policy import DEFAULT_MOVIE_POLICY, SCHEMA_VERSION, PolicyBody

        newer = DEFAULT_MOVIE_POLICY.model_dump()
        newer["schema_version"] = SCHEMA_VERSION + 1

        with pytest.raises(pydantic.ValidationError):
            PolicyBody.model_validate(newer)


class TestARepairedPolicyCannotExecute:
    """A rescaled policy is safe to SCAN on and unsafe to DELETE on, and those are
    different questions.

    The rescale cannot move a score, so the numbers a scan produces are right. But the
    body it ran was never saved by anyone: it is Reaper's repair of a stored row, and an
    approval names a policy hash. Executing against one nobody chose is the substitution
    the journal exists to prevent, so the scan degrades and the snapshot is not
    executable until the operator opens the editor and saves.

    These pin the flags. The behaviour they feed -- the scan degrading and the plan being
    refused -- is driven end to end in
    ``test_scan_pipeline.TestARepairedPolicyCannotBeReapedFrom``.
    """

    def test_the_repaired_flag_is_carried_not_swallowed(self) -> None:
        """``active_policy`` repairs rather than raising, and every caller can still tell
        that it did. A repair that looked identical to a clean load would put the scan on
        an unapproved policy silently, which is the whole risk."""
        from reaper.services.profiles import ActivePolicy

        assert ActivePolicy(DEFAULT_MOVIE_POLICY, "mine").repaired is False
        assert ActivePolicy(DEFAULT_MOVIE_POLICY, "mine", rescaled=True).repaired is True
        assert ActivePolicy(DEFAULT_MOVIE_POLICY, "default", fell_back=True).repaired is True

    def test_the_two_recoveries_are_flags_and_not_read_off_the_name(self) -> None:
        """Regression. These were briefly told apart by ``name != "default"``, which looks
        reasonable and is wrong: an operator's own policy is very often *called* "default",
        so their rescaled policy was reported as unreadable and the editor stopped offering
        to save it. The name carries no such meaning; only the flags do."""
        from reaper.services.profiles import ActivePolicy

        theirs = ActivePolicy(DEFAULT_MOVIE_POLICY, "default", rescaled=True)

        assert theirs.rescaled is True
        assert theirs.fell_back is False
