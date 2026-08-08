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
from structlog.testing import capture_logs

from reaper.clock import utcnow
from reaper.engine import identity
from reaper.engine.gates import Facts, GateConfig, GateId, ServerPopularityGate
from reaper.engine.observation import Absent, Known, Unknown
from reaper.engine.policy import DEFAULT_MOVIE_POLICY
from reaper.engine.verdict import decide_verdict
from reaper.services import lists
from reaper.services.imdb_dataset import ImdbRating
from reaper.services.snapshot import RawItem, ScanContext, _reported_size, build_facts

_EMPTY_INDEX = lists.MembershipIndex({}, {}, {}, {})


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


def _facts(
    item: RawItem,
    *,
    imdb: dict[str, ImdbRating] | None = None,
    membership_index: lists.MembershipIndex | None = None,
    last_played: dict[int, datetime] | None = None,
) -> Facts:
    return build_facts(
        item,
        ScanContext(horizon=datetime(2019, 1, 1, tzinfo=UTC)),
        membership_index=membership_index or _EMPTY_INDEX,
        imdb=imdb or {},
        last_played=last_played if last_played is not None else {},
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


class TestNobodyIsWatchingIsNotSaidOfAnItemNobodyChecked:
    """``is_streaming_now`` is one of the two structural gates, and it is matched by rating
    key. An item with no key has no key to match, so the check never ran -- but it recorded
    a definite ``Known(False)``, three lines under a comment reading "Never assume False,
    that is how a tool deletes a file somebody is watching" (rules 93 and 7/24). Every
    sibling fact takes ``Unknown`` on the same condition, and so does the season builder's
    twin (``season_scan``, rule 72)."""

    def test_an_unmatched_movie_does_not_claim_nobody_is_watching(self) -> None:
        facts = _facts(_raw(plex_rating_key=None))

        assert isinstance(facts.is_streaming_now, Unknown)

    def test_an_ambiguous_match_is_the_one_that_stings(self) -> None:
        """Plex DOES hold this title, in two copies, so somebody can be streaming it right
        now. Reaper refused to guess which copy's history to read, which is correct, and then
        asserted nobody was watching, which is not. The reason names the real situation."""
        facts = _facts(_raw(plex_rating_key=None, match_status=identity.MatchStatus.AMBIGUOUS))

        streaming = facts.is_streaming_now
        assert isinstance(streaming, Unknown)
        assert "more than one Plex item" in streaming.reason

    def test_a_matched_movie_nobody_is_streaming_is_still_a_definite_no(self) -> None:
        """The control. A genuine "we looked and nobody is watching" must stay ``Known``, or
        the veto would block every item and nothing could ever be reaped."""
        facts = _facts(_raw(plex_rating_key=10))

        assert facts.is_streaming_now == Known(value=False, source="tautulli")


class TestAKeepListRowIsFoundByEveryIdTheMovieCarries:
    """Radarr is tmdb-native and a blank ``imdbId`` is ordinary, so a movie's imdb id is
    often the one Plex matched. A "Never Reap" collection on a legacy-agent Plex library
    is stored under an imdb id and nothing else, so looking the movie up by Radarr's ids
    alone would miss it -- and a film the owner put on the keep list would be condemned
    on a healthy, executable snapshot."""

    @staticmethod
    def _keep_list_stored_under_imdb_only() -> lists.MembershipIndex:
        membership = lists.Membership(
            slug="never-reap",
            display_name="Never Reap",
            mode=lists.ListMode.HARD,
            kind=lists.ListKind.WHITELIST,
            rank=None,
        )
        return lists.MembershipIndex({"tt0000042": ((1, "movie", membership),)}, {}, {}, {})

    def test_a_movie_whose_only_imdb_id_came_from_plex_is_still_protected(self) -> None:
        facts = _facts(
            _raw(imdb_id=None, tmdb_id=7, plex_imdb_id="tt0000042"),
            membership_index=self._keep_list_stored_under_imdb_only(),
        )

        assert facts.is_whitelisted == Known(value=True, source="Never Reap")

    def test_a_movie_carrying_neither_imdb_id_is_not_falsely_protected(self) -> None:
        """The other direction: the fallback must not invent a match."""
        facts = _facts(
            _raw(imdb_id=None, tmdb_id=7, plex_imdb_id=None),
            membership_index=self._keep_list_stored_under_imdb_only(),
        )

        assert facts.is_whitelisted == Known(value=False, source="lists")


class TestAMatchedItemWithNoArrivalDateIsWarned:
    """Matched to Plex but nothing to measure dormancy from, so the item abstains and shows
    only as kept-to-be-safe, never on the reap list. A warning names it so "why isn't this
    reapable" is answerable from the log, the same as an unmatched item.

    "Nothing to measure from" means no arrival date AND no play (#272, #257). This lane used
    to stop at the missing arrival date whatever history it held, while the season lane
    measured from the play -- one derived value with two thaw rules. The thaw is now
    ``engine.dormancy.reference_instant``'s, so both lanes take one branch.
    """

    def test_a_matched_movie_with_neither_an_added_at_nor_a_play_is_warned(self) -> None:
        with capture_logs() as logs:
            facts = _facts(_raw(added_at=None), last_played={})

        assert isinstance(facts.days_observed_unwatched, Unknown)
        warned = [e for e in logs if e["event"] == "scan.no_added_at"]
        assert len(warned) == 1
        assert warned[0]["log_level"] == "warning"
        assert warned[0]["media_type"] == "movie"

    def test_no_added_at_but_a_play_measures_from_the_play_and_is_silent(self) -> None:
        """The divergence itself: same evidence, and this lane used to answer Unknown where
        the season lane answered with a number. Neither lane had a test for it, because both
        no-arrival-date tests pinned the play absent (#257). The item is judged now, and the
        warning must not fire -- it says dormancy could not be measured, and it was."""
        with capture_logs() as logs:
            facts = _facts(_raw(added_at=None), last_played={10: utcnow() - timedelta(days=12)})

        assert isinstance(facts.days_observed_unwatched, Known)
        # A range, not an equality: production samples its own `utcnow()` and comparing two
        # samples of the clock it reads is rule 133's flake. The 2019 horizon would give
        # thousands of days, so this discriminates the play from every fallback.
        assert 11 <= facts.days_observed_unwatched.value <= 13
        assert [e for e in logs if e["event"] == "scan.no_added_at"] == []

    def test_a_matched_movie_with_an_added_at_is_not_warned(self) -> None:
        with capture_logs() as logs:
            _facts(_raw())

        assert [e for e in logs if e["event"] == "scan.no_added_at"] == []


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


class TestTheScanRecordsHowFarBackItsHistoryReaches:
    """The watcher lane's half of the horizon defense.

    Dormancy has been clamped to the horizon since early on (``dormancy.reference_instant``),
    so an item older than the mirror reads as dormant since the mirror's edge rather than
    for decades. The *watcher count* had no equivalent: it was counted over
    ``utcnow() - window_days`` whatever the mirror held, so on a history younger than the
    window a title several people watched inside that window counted as nobody, and the
    gate printed "Nobody here watched it in the last year" about a year it had not seen.

    The count itself cannot be fixed by clamping the query -- there are no rows before the
    horizon to find -- so the scan records the reach instead and the gate refuses to
    answer past it.
    """

    def test_the_builder_records_the_reach_from_the_scan_horizon(self) -> None:
        now = utcnow()
        facts = build_facts(
            _raw(),
            ScanContext(horizon=now - timedelta(days=90)),
            membership_index=_EMPTY_INDEX,
            imdb={},
            last_played={},
            watchers_window={10: 0},
            watchers_all_time={10: 0},
            whitelisted=set(),
        )

        assert facts.history_reach_days == Known(value=90, source="tautulli")

    def test_the_reach_is_the_scans_own_sample_not_a_fresh_clock_read(self) -> None:
        """Sampled once, on the context, because it describes the mirror and not the item.

        Re-reading the clock inside the per-item builder let one scan freeze two different
        reaches: an item built after the day count ticked up to the popularity window was
        answered where an identical item built moments earlier was held. The distinctive
        value here cannot arise from the horizon, so this fails if the derivation moves
        back into the builder.
        """
        context = ScanContext(horizon=utcnow() - timedelta(days=90))
        context.reach_days = 4242

        facts = build_facts(
            _raw(),
            context,
            membership_index=_EMPTY_INDEX,
            imdb={},
            last_played={},
            watchers_window={10: 0},
            watchers_all_time={10: 0},
            whitelisted=set(),
        )

        assert facts.history_reach_days == Known(value=4242, source="tautulli")

    def test_a_title_watched_before_a_young_horizon_is_held_not_condemned(self) -> None:
        """The issue, driven through the builder and the shipped gate.

        The mirror reaches back 90 days; the operator lowered the dormancy floor, so the
        clamp no longer saves the item on its own; several people watched this title eight
        months ago, which is inside the year-long window and outside the mirror. The scan
        sees a zero, and must not call it one.
        """
        gate = ServerPopularityGate(
            GateConfig(GateId.SERVER_POPULARITY, threshold=3, window_days=365)
        )
        facts = build_facts(
            _raw(),
            ScanContext(horizon=utcnow() - timedelta(days=90)),
            membership_index=_EMPTY_INDEX,
            imdb={},
            last_played={},
            watchers_window={10: 0},  # what the mirror can see: nobody, in 90 days
            watchers_all_time={10: 0},
            whitelisted=set(),
        )

        result = gate.evaluate(facts)

        assert result.blocked is True
        assert result.detail.startswith("could not check")
        assert (
            decide_verdict(
                protected=False,
                blocked=True,
                blocked_holds_reap=True,
                score=100,
                coverage_bp=10_000,
                condemn_at=1,
                coverage_floor_bp=0,
            )
            == "abstain"
        )


class TestWatchCountsFromAStaleMirrorAreNotZero:
    """Watch stats are read from the local ``watch_event`` cache, not live.

    So a Tautulli ingest that stopped a month ago does not raise, does not degrade
    the snapshot, and does not look any different from a genuinely quiet library:
    ``watchers_window.get(rating_key, 0)`` keeps returning an affirmative
    ``Known(0)`` while dormancy grows against a frozen mirror. Every item drifts
    toward condemnation at exactly the rate the outage lasts.
    """

    # The staleness behavior itself is pinned by driving the real scan, in
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

    These pin the flags. The behavior they feed -- the scan degrading and the plan being
    refused -- is driven end to end in
    ``test_scan_pipeline.TestARepairedPolicyCannotBeReapedFrom``.
    """

    def test_the_repaired_flag_is_carried_not_swallowed(self) -> None:
        """``active_policy`` repairs rather than raising, and every caller can still tell
        that it did. A repair that looked identical to a clean load would put the scan on
        an unapproved policy silently, which is the whole risk."""
        from reaper.engine.policy import PolicyRepair
        from reaper.services.profiles import ActivePolicy

        assert ActivePolicy(DEFAULT_MOVIE_POLICY, "mine").repaired is False
        # Every member, so a repair added without reaching this property is caught here as
        # well as by the copy walks in `test_policy_repairs.py`.
        for repair in PolicyRepair:
            assert ActivePolicy(DEFAULT_MOVIE_POLICY, "mine", (repair,)).repaired is True

    def test_the_two_recoveries_are_flags_and_not_read_off_the_name(self) -> None:
        """Regression. These were briefly told apart by ``name != "default"``, which looks
        reasonable and is wrong: an operator's own policy is very often *called* "default",
        so their rescaled policy was reported as unreadable and the editor stopped offering
        to save it. The name carries no such meaning; only the flags do."""
        from reaper.engine.policy import PolicyRepair
        from reaper.services.profiles import ActivePolicy

        theirs = ActivePolicy(DEFAULT_MOVIE_POLICY, "default", (PolicyRepair.RESCALED,))

        assert theirs.repairs == (PolicyRepair.RESCALED,)
