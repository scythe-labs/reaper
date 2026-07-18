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

    def test_history_sync_exposes_how_fresh_the_mirror_is(self) -> None:
        from reaper.services import history_sync

        assert hasattr(history_sync, "latest"), (
            "history_sync.latest() is what lets the scan tell a quiet library from a "
            "stalled ingest. horizon() answers the opposite question."
        )

    def test_the_staleness_bound_is_two_nightly_cycles(self) -> None:
        """Pinned so it cannot drift silently. Tighter and a paused ingest blocks every
        scan; looser and items drift toward condemnation for the length of the outage."""
        from reaper.services import snapshot

        assert timedelta(hours=48) == snapshot.MIRROR_STALE_AFTER
