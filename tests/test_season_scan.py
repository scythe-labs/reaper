# SPDX-License-Identifier: AGPL-3.0-or-later
"""The TV/season scan path.

Season pruning deletes whole seasons, so this half of the scan is held to the same
standard as the movie half, plus one more. A season must be resolved to its own Plex
rating key before its watch history can be read, so every test here is really asking
whether an *uncertain* resolution can lead to a deletion. The answer, everywhere, must be
no. A season Reaper cannot see is judged, at worst, ABSTAIN, never condemn.

The load-bearing test is ``TestNothingUnseenIsCondemned``. It runs an unresolved season
through the real default policy and asserts the verdict cannot be "condemn".
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from collections.abc import Set as AbstractSet
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine
from structlog.testing import capture_logs

from reaper.clients.base import IntegrationError
from reaper.clients.plex import PlexError, PlexSeasonRow
from reaper.clients.sonarr_stats import SeasonStats
from reaper.clock import utcnow
from reaper.config import Settings
from reaper.db.session import create_cache_engine
from reaper.engine import identity
from reaper.engine.gates import ABSTAIN, PROTECT, Evaluation, GateId, GateResult, evaluate_all
from reaper.engine.observation import Absent, Known, Unknown
from reaper.engine.policy import DEFAULT_MOVIE_POLICY, DEFAULT_TV_POLICY
from reaper.engine.reason import Reason, legacy
from reaper.engine.signals import Score, SignalConfig
from reaper.engine.signals import score as score_signals
from reaper.ratings import Rating, RatingSource
from reaper.services import history_sync, lists, requested_by, rewatch, season_evidence, season_scan
from reaper.services.condemned import reap_override_verdict_decoded
from reaper.services.scan_runner import build_gates
from reaper.services.season_pruning import plan_series_prune
from reaper.services.snapshot import _explain, _verdict, judge_facts
from tests._fakes import FakeSonarr, FakeTautulli, show_library
from tests._reasons import flat as reason_flat
from tests._reasons import text as reason_text

GB = 1024**3


def _season_policy(**edits: Any) -> season_evidence.SeasonPolicy:
    """The shipped TV policy's nine season settings, with whichever ones a test varies.

    This is built through the real ``from_body`` off ``DEFAULT_TV_POLICY`` rather than
    spelled out, so the settings a test does not vary are the shipped values, not a second
    copy of them.
    """
    return replace(season_evidence.SeasonPolicy.from_body(DEFAULT_TV_POLICY), **edits)


def _season(
    n: int,
    *,
    files: int = 5,
    size: int | None = GB,
    total: int = 10,
    wanted: int = 0,
    monitored: bool = False,
) -> SeasonStats:
    return SeasonStats(
        season_number=n,
        monitored=monitored,
        episode_file_count=files,
        size_on_disk=size,
        total_episode_count=total,
        wanted_episode_count=wanted,
    )


def _season_payload(n: int, *, files: int = 5, size: int = GB, wanted: int = 0) -> dict[str, Any]:
    """A Sonarr ``seasons[]`` entry, as the API returns it."""
    return {
        "seasonNumber": n,
        "monitored": False,
        "statistics": {
            "episodeFileCount": files,
            "sizeOnDisk": size,
            "totalEpisodeCount": 10,
            "episodeCount": wanted,
        },
    }


# ---------------------------------------------------------------------------
# Keys and titles
# ---------------------------------------------------------------------------


class TestKeys:
    def test_a_season_key_is_four_parts(self) -> None:
        assert season_scan.season_media_key(1, 42, 3) == "sonarr:1:42:3"

    def test_the_title_names_the_season(self) -> None:
        assert season_scan.season_title("Example Show", 3) == "Example Show, Season 3"

    def test_specials_are_named_not_numbered(self) -> None:
        assert "Specials" in season_scan.season_title("Example Show", 0)


class TestSeasonRequester:
    """The season-precise tvdb key outranks the show-level Plex rating key, so two people
    who asked for different seasons attribute to their own season, not a blurred "A + 1
    other".
    """

    def test_different_seasons_attribute_to_their_own_requester(self) -> None:
        tvdb = 81189
        show_rk = 500
        # build_map files each requested season under its season key, and files both
        # requesters under the show-level rating key too, since Seerr stores a TV
        # request's ratingKey at the show level.
        requested = {
            requested_by.season_key(tvdb, 1) or "": "Alice",
            requested_by.season_key(tvdb, 2) or "": "Bob",
            requested_by.rating_key_key(show_rk) or "": "Alice + 1 other",
        }
        s1 = season_scan.season_requester(
            requested,
            media_key="sonarr:1:9:1",
            group_key="sonarr:1:9",
            tvdb_id=tvdb,
            season_number=1,
            show_rating_key=show_rk,
        )
        s2 = season_scan.season_requester(
            requested,
            media_key="sonarr:1:9:2",
            group_key="sonarr:1:9",
            tvdb_id=tvdb,
            season_number=2,
            show_rating_key=show_rk,
        )
        assert s1 == "Alice"  # not "Alice + 1 other"
        assert s2 == "Bob"

    def test_the_show_rating_key_still_beats_the_whole_show_union(self) -> None:
        # A whole-show request has no season key, so the rating-key tier, being more
        # specific, still wins over the loose tvdb union.
        tvdb = 81189
        requested = {
            requested_by.rating_key_key(500) or "": "Alice",
            requested_by.show_key(tvdb) or "": "Alice + 1 other",
        }
        name = season_scan.season_requester(
            requested,
            media_key="sonarr:1:9:1",
            group_key="sonarr:1:9",
            tvdb_id=tvdb,
            season_number=1,
            show_rating_key=500,
        )
        assert name == "Alice"

    def test_the_mapped_media_key_wins_over_everything(self) -> None:
        requested = {"sonarr:1:9:1": "Mapped", requested_by.season_key(81189, 1) or "": "Loose"}
        name = season_scan.season_requester(
            requested,
            media_key="sonarr:1:9:1",
            group_key="sonarr:1:9",
            tvdb_id=81189,
            season_number=1,
            show_rating_key=500,
        )
        assert name == "Mapped"


class TestParseSeasons:
    def test_an_entry_without_statistics_is_dropped_not_guessed(self) -> None:
        """A season Sonarr cannot describe is left out entirely rather than defaulted to
        zeros. Zeros would read as an empty season and quietly drop it from protection.
        """
        series = {"seasons": [_season_payload(1), {"seasonNumber": 2}]}
        seasons = season_scan.parse_seasons(series)
        assert [s.season_number for s in seasons] == [1]


# ---------------------------------------------------------------------------
# Airing detection, conservative on purpose
# ---------------------------------------------------------------------------


class TestAiring:
    def test_a_continuing_series_protects_its_latest_season(self) -> None:
        seasons = [_season(1), _season(2), _season(3)]
        series = {"status": "continuing", "ended": False}
        assert season_scan.airing_seasons(series, seasons) == {3}

    def test_an_ended_series_marks_nothing_airing(self) -> None:
        seasons = [_season(1), _season(2)]
        series = {"status": "ended", "ended": True}
        assert season_scan.airing_seasons(series, seasons) == set()

    def test_specials_never_count_as_the_latest_airing_season(self) -> None:
        seasons = [_season(0), _season(1)]
        series = {"status": "continuing"}
        assert season_scan.airing_seasons(series, seasons) == {1}


# ---------------------------------------------------------------------------
# The show join, the one place a wrong answer could delete the wrong thing
# ---------------------------------------------------------------------------


class TestTheShowJoin:
    """The Sonarr series to Plex show join runs through the one shared resolver
    (``identity.resolve_show``). These cases carry no external id, so they exercise the
    title+year backstop. See ``test_identity.py`` for the id tiers.
    """

    def _index(self, *items: tuple[int, str, int | None]) -> identity.PlexIndex:
        return identity.PlexIndex.build(
            [
                identity.PlexItem(rating_key=rk, title=title, year=year, added_at=None)
                for rk, title, year in items
            ]
        )

    def _match(self, index: identity.PlexIndex, title: str, year: int | None) -> int | None:
        return identity.resolve_show(
            ids=identity.ExternalIds(), title=title, year=year, file_basename=None, index=index
        ).rating_key

    def test_a_unique_title_matches(self) -> None:
        assert self._match(self._index((900, "Example Show", 2010)), "Example Show", 2010) == 900

    def test_a_missing_title_is_no_match(self) -> None:
        assert self._match(self._index(), "Nowhere", None) is None

    def test_a_duplicate_title_is_disambiguated_by_year(self) -> None:
        index = self._index((1, "Same Title", 2001), (2, "Same Title", 2005))  # original, remake
        assert self._match(index, "Same Title", 2005) == 2

    def test_a_duplicate_title_with_no_year_refuses_to_guess(self) -> None:
        """The wrong show join reads the wrong show's watch history and could condemn a
        season people are watching. With nothing to disambiguate on, this refuses. The
        season goes Unknown and abstains, rather than being matched arbitrarily.
        """
        index = self._index((1, "Same Title", 2001), (2, "Same Title", 2005))
        assert self._match(index, "Same Title", None) is None

    def test_a_lone_title_match_with_a_conflicting_year_is_refused(self) -> None:
        """The remake is scanned, but the only Plex show with that title is the original,
        since the remake is indexed under a different title. A single title hit is not a
        safe join when the known years disagree, because binding would read the original's
        history.
        """
        index = self._index((1, "Same Title", 2001))
        assert self._match(index, "Same Title", 2005) is None

    def test_a_lone_title_match_with_an_agreeing_year_binds(self) -> None:
        assert self._match(self._index((1, "Same Title", 2005)), "Same Title", 2005) == 1

    def test_a_lone_title_match_binds_when_a_year_is_missing(self) -> None:
        """Plex often has no year. A title-only join stays as safe as the movie path's."""
        assert self._match(self._index((1, "Same Title", None)), "Same Title", 2005) == 1


class TestResolveSeasonKeys:
    class _FakeChildren:
        def __init__(self, children: list[dict[str, Any]]) -> None:
            self._children = children

        async def children_metadata(self, rating_key: int) -> list[dict[str, Any]]:
            return self._children

    async def test_it_maps_number_to_key_and_arrival(self) -> None:
        client = self._FakeChildren([{"media_index": 1, "rating_key": 101, "added_at": "1000000"}])
        result = await season_scan.resolve_season_keys(client, 900)  # type: ignore[arg-type]
        assert result[1].rating_key == 101
        assert result[1].added_at is not None

    async def test_a_duplicated_season_number_is_dropped_not_guessed(self) -> None:
        """Two "Season 2" items, from a split or mis-scanned library, are ambiguous.
        Binding to one risks reading an empty duplicate's history for a watched season, so
        the season is dropped entirely -> no Plex key -> Unknown facts -> abstain.
        """
        client = self._FakeChildren(
            [
                {"media_index": 1, "rating_key": 101},
                {"media_index": 2, "rating_key": 201},
                {"media_index": 2, "rating_key": 202},  # duplicate season number
            ]
        )
        result = await season_scan.resolve_season_keys(client, 900)  # type: ignore[arg-type]
        assert 1 in result
        assert 2 not in result  # dropped, not bound to 201 or 202


class TestSeasonsFromRows:
    """The shared ambiguity policy the season sweep and the per-show fallback both run
    through, so a duplicate season number is dropped the same way whichever path found it."""

    def test_it_maps_number_to_key_and_parses_added_at(self) -> None:
        rows = [(1, 101, "1000000"), (2, 202, None)]
        out = season_scan.seasons_from_rows(rows)
        assert out[1].rating_key == 101
        assert out[1].added_at is not None
        assert out[2].rating_key == 202
        assert out[2].added_at is None  # absent added-at stays None, never a 1970 date

    def test_a_duplicated_number_is_dropped(self) -> None:
        out = season_scan.seasons_from_rows([(1, 101, None), (2, 201, None), (2, 202, None)])
        assert 1 in out
        assert 2 not in out  # the whole season abstains rather than bind to one duplicate

    def test_rows_missing_index_or_key_are_skipped(self) -> None:
        out = season_scan.seasons_from_rows([(None, 101, None), (3, None, None), (4, 404, None)])
        assert set(out) == {4}


# ---------------------------------------------------------------------------
# The guard -> gate translation
# ---------------------------------------------------------------------------


def _hand_reap(result: GateResult) -> str:
    """What a hand reap does to a season carrying this one guard result.

    This follows the path the queue's Reap button actually takes. The guard result is
    frozen by the real writer (``snapshot._explain``) and re-read by
    ``condemned.reap_override_verdict_decoded``, which every read-side consumer calls
    (``snapshot.effective_fate`` routes a hand reap through it rather than deciding live).
    It is scored 0 against a threshold of 100, so nothing but the override can condemn it.

    **This answer stays constant for this gate, and that is deliberate, not a gap.**
    ``season_progression`` is in neither ``STRUCTURAL_GATES`` nor any remaining hold, so
    every shape the guard can emit, all 16 combinations of outcome, blocked,
    defers_to_owner, and detail, condemns under a hand reap. The callers below do not lean
    on this line to tell their shapes apart. Each asserts the typed ``defers_to_owner`` and
    the detail instead, which do vary. What this line pins is the guarantee itself.
    **No season-guard shape may ever refuse the operator's own hand.** A future change that
    re-adds a hold would break that guarantee.

    This goes through the freeze because that is the only way a hand reap is decided.
    ``effective_fate`` routes every one of them through ``condemned`` off the frozen
    explanation, and ``snapshot._verdict`` takes no override at all. Going through
    ``_explain`` also makes these assertions cover the writer, since a field the writer
    stops emitting changes the answer here.
    """
    policy = DEFAULT_MOVIE_POLICY.model_copy(update={"condemn_at": 100})
    frozen = json.loads(
        _explain(Evaluation(results=[result]), Score(value=0.0, coverage=1.0, results=[]), policy)
    )
    return reap_override_verdict_decoded(frozen, score=0)


class TestGuardResult:
    def test_a_protected_season_becomes_a_protecting_gate(self) -> None:
        plan = plan_series_prune(series_title="S", seasons=[_season(1), _season(2)], keep_last=1)
        # Season 1 is the first season -> protected.
        result = season_evidence.guard_result(plan, 1)
        assert result.gate is GateId.SEASON_PROGRESSION
        assert result.outcome == PROTECT

    def test_a_keep_rule_conflict_blocks_rather_than_condemns(self) -> None:
        """The old season is the good one. It is prunable by rank, but far more watched
        than a season the rule keeps. That is not a delete to make unattended, so it
        blocks, which forces the whole item to ABSTAIN and sends it to a human.
        """
        plan = plan_series_prune(
            series_title="S",
            seasons=[_season(n) for n in range(1, 5)],
            keep_last=2,
            keep_first_season=False,
            watchers_by_season={1: 40, 2: 1, 3: 1, 4: 1},
        )
        result = season_evidence.guard_result(plan, 1)
        assert result.outcome == ABSTAIN
        assert result.blocked is True

    def test_a_cleanly_prunable_season_neither_protects_nor_blocks(self) -> None:
        plan = plan_series_prune(
            series_title="S",
            seasons=[_season(n) for n in range(1, 5)],
            keep_last=1,
            keep_first_season=False,
        )
        result = season_evidence.guard_result(plan, 2)
        assert result.outcome == ABSTAIN
        assert result.blocked is False

    def test_a_conflict_that_made_the_comparison_defers_to_the_owner(self) -> None:
        """Season 1 was watched 40 times against Season 4's once. The comparison was made,
        and the keep rule lost it. That is the deliberate "you decide" flag, and a hand
        reap is the decision it asked for.

        The flag no longer decides the reap, a block does that now, but it still picks the
        chip the operator reads (``api.review._chip``), which is why it is still produced
        and still asserted here. It is the one shape whose chip names the comparison.
        """
        plan = plan_series_prune(
            series_title="S",
            seasons=[_season(n) for n in range(1, 5)],
            keep_last=2,
            keep_first_season=False,
            watchers_by_season={1: 40, 2: 1, 3: 1, 4: 1},
        )
        result = season_evidence.guard_result(plan, 1)

        assert result.blocked is True
        assert result.defers_to_owner is True
        assert _hand_reap(result) == "condemn"

    def test_a_conflict_whose_comparison_failed_is_flagged_as_refused(self) -> None:
        """Season 4 is kept by the rule but is on disk without ever being resolved in
        Plex, so nobody could read who watched it (``kept_watchers=None``).
        ``_detect_conflicts`` still raises the conflict instead of letting an unread
        number clear a protection, and the season still goes to a human.

        The typed ``defers_to_owner`` flag, not the wording of the message, is what marks
        this shape as refused rather than deferred. Neither one moves the verdict.
        """
        plan = plan_series_prune(
            series_title="S",
            seasons=[_season(n) for n in range(1, 5)],
            keep_last=1,
            keep_first_season=False,
            watchers_by_season={1: 40, 2: 1, 3: 1, 4: None},
        )
        result = season_evidence.guard_result(plan, 1)

        assert result.blocked is True
        assert result.defers_to_owner is False
        assert not reason_text(result.detail).startswith("could not check")
        assert _hand_reap(result) == "condemn"

    def test_a_readable_conflict_does_not_mask_a_refused_one_on_the_same_season(self) -> None:
        """One pruned season carries a conflict per kept season, so more than one shape
        can be true at once. Reading only the first conflict would let a readable
        comparison mask a refused one. Season 5 is on disk but not yet resolved in Plex,
        so it is kept and unreadable, while Seasons 1 and 4 read fine.

        The flag answers for every comparison behind the block, so one refusal decides it,
        and the message follows the flag. The operator is shown the season nobody could
        read, not the one that happened to sort first. A first-match read would tell them
        Reaper made a comparison it actually refused to make, on the card they are
        deciding from.
        """
        plan = plan_series_prune(
            series_title="S",
            seasons=[_season(n) for n in range(1, 6)],
            keep_last=2,
            keep_first_season=True,
            watchers_by_season={1: 0, 2: 9, 3: 1, 4: 2, 5: None},
        )
        # The premise, asserted rather than assumed. A readable conflict really does come
        # first for this season, so a first-match read would report "compared" and release.
        season_2 = [c for c in plan.conflicts if c.pruned_season == 2]
        assert [c.kept_watchers for c in season_2] == [0, 2, None]

        result = season_evidence.guard_result(plan, 2)

        assert result.blocked is True
        assert result.defers_to_owner is False
        # The message names the season that could not be read, not the ones that could.
        assert "could not check who watched Season 5" in reason_text(result.detail)

    def test_a_conflict_the_mirror_could_not_settle_is_flagged_as_refused(self) -> None:
        """The third shape. Season 1 was watched before Tautulli was installed, so its
        count is a lower bound and no comparison against it can be made. That is
        ``Unknown``, not a decision, so it joins the unreadable arm rather than the
        deliberate "you decide" one, the reading the mid-binge hold takes when the reach
        cannot establish who is part-way through. Reading only ``kept_watchers is None``
        would have grouped it with the settled shape and claimed, on the card, that a
        comparison had been made.

        The flag no longer changes the verdict. The item still goes to a human, who may
        still say remove it. It only changes what they are told, which is why the two
        Unknown shapes are kept apart from the settled one.
        """
        plan = plan_series_prune(
            series_title="S",
            seasons=[_season(n) for n in range(1, 5)],
            keep_last=2,
            keep_first_season=False,
            watchers_by_season={1: 0, 2: 0, 3: 1, 4: 1},
            shortfall_by_season={1: legacy("your watch history only goes back 12 months")},
        )
        result = season_evidence.guard_result(plan, 1)

        assert result.blocked is True
        assert result.defers_to_owner is False
        assert "cannot tell whether Season 1 is watched more than" in reason_text(result.detail)
        assert _hand_reap(result) == "condemn"

    def test_a_settleable_conflict_is_not_masked_by_one_the_mirror_refused(self) -> None:
        """This gets the same precedence as the unreadable arm, for the same reason. One
        pruned season carries a conflict per kept season, so a comparison Reaper did make
        can sort ahead of one it could not. The refusal decides the flag and the message,
        or a first-match read would report "compared" and release the reap on a season
        nothing established.
        """
        plan = plan_series_prune(
            series_title="S",
            seasons=[_season(n) for n in range(1, 5)],
            keep_last=2,  # keeps 3, 4
            keep_first_season=False,
            watchers_by_season={1: 9, 2: 0, 3: 1, 4: 2},
            shortfall_by_season={4: legacy("your watch history only goes back 12 months")},
        )
        # The premise, asserted rather than assumed. The made comparison really does come
        # first for this season.
        assert [c.shortfall for c in plan.conflicts if c.pruned_season == 1] == [
            None,
            legacy("your watch history only goes back 12 months"),
        ]

        result = season_evidence.guard_result(plan, 1)

        assert result.defers_to_owner is False
        assert "Season 4" in reason_text(result.detail)


# ---------------------------------------------------------------------------
# Facts assembly, and the Unknown discipline
# ---------------------------------------------------------------------------


def _facts(**over: Any) -> Any:
    base: dict[str, Any] = {
        "title": "Show · Season 3",
        "season": _season(3, size=8 * GB),
        "rank": 2,
        "plex_rating_key": 700,
        # No ledger row. This is the ordinary state of a season Reaper has not bound
        # before, and what most cases here want unless a test is specifically about that
        # ledger row.
        "seen": None,
        "season_added_at": utcnow() - timedelta(days=4000),
        "horizon": utcnow() - timedelta(days=4000),
        # Sampled once per scan on ``snapshot.ScanContext``, so the builder is handed it
        # rather than measuring it. Default deep enough that the popularity gate answers.
        "reach_days": 4000,
        "last_played": None,
        "watchers_window": 0,
        "watchers_all_time": 0,
        "active_rating_keys": set(),
        "activity_degraded": False,
        "whitelisted": False,
        "curated": [],
        # The show carried an IMDb id, so a missing rating means "looked up, unrated".
        # Override to False for the other case, where there is no id, so nothing was ever
        # asked.
        "rating_looked_up": True,
    }
    base.update(over)
    return season_scan.build_season_facts(**base)


class TestSeasonsRecordTheHistoryReachToo:
    """The movie and season builders must behave identically here, in the same change.

    Both lanes count watchers out of the same watch-history table over the same policy
    window, so both need to say how far back that table reaches, or
    ``ServerPopularityGate`` (shared by both) blocks every season forever. See
    ``test_fact_layer_states.TestTheScanRecordsHowFarBackItsHistoryReaches``.
    """

    def test_the_season_builder_records_the_reach_it_was_given(self) -> None:
        """This value is given, not measured. The reach is sampled once per scan on
        ``snapshot.ScanContext``, so the two lanes cannot freeze different values, and
        this builder's job is only to carry it onto the season's own Facts.
        """
        facts = _facts(horizon=utcnow() - timedelta(days=200), reach_days=200)

        assert facts.history_reach_days == Known(value=200, source="tautulli")

    def test_an_unresolved_season_still_records_it(self) -> None:
        """The reach is a property of the watch-history table, not of the season, so it
        is known even for a season whose own watch facts are not. If it rode on the rating
        key instead, every unmatched season would block on the reach rather than on the
        honest "Plex has not matched this" that the watch facts already carry.
        """
        facts = _facts(plex_rating_key=None, horizon=utcnow() - timedelta(days=200), reach_days=200)

        assert facts.history_reach_days == Known(value=200, source="tautulli")


class TestBuildSeasonFacts:
    def test_an_unresolved_season_has_unknown_watch_facts(self) -> None:
        """No Plex rating key means no history to read. Dormancy, popularity, and
        streaming all go Unknown, and Unknown, through the gates, protects.
        """
        facts = _facts(plex_rating_key=None)
        assert isinstance(facts.days_observed_unwatched, Unknown)
        assert isinstance(facts.distinct_watchers, Unknown)
        assert isinstance(facts.is_streaming_now, Unknown)

    def test_an_ambiguous_show_gets_the_honest_unknown_reason(self) -> None:
        """An AMBIGUOUS show (two Plex items share its id) is not "unmatched". Plex has
        it, more than once. The Unknown reason must tell that story, or the why-panel
        claims the show couldn't be found when the opposite is true.
        """
        facts = _facts(plex_rating_key=None, show_match_status=identity.MatchStatus.AMBIGUOUS)
        assert isinstance(facts.days_observed_unwatched, Unknown)
        # Shared with the movie lane. The same base id, media-selected.
        assert facts.days_observed_unwatched.reason == Reason(
            "cause.plex_ambiguous", {"mediaType": "season"}
        )

        unmatched = _facts(plex_rating_key=None, show_match_status=identity.MatchStatus.UNMATCHED)
        assert isinstance(unmatched.days_observed_unwatched, Unknown)
        assert unmatched.days_observed_unwatched.reason == Reason(
            "cause.plex_unmatched", {"mediaType": "season"}
        )

    def test_a_season_unmatched_within_a_matched_show_is_warned(self) -> None:
        """The show bound to Plex but this season did not, so it abstains and shows only
        as kept-to-be-safe. A warning names it so "why is this season kept" is answerable
        from the log. This is the season-level twin of the movie and show miss.
        """
        with capture_logs() as logs:
            _facts(plex_rating_key=None, show_match_status=identity.MatchStatus.MATCHED)

        warned = [e for e in logs if e["event"] == "scan.plex_unmatched"]
        assert len(warned) == 1
        assert warned[0]["log_level"] == "warning"
        assert warned[0]["media_type"] == "season"

    def test_a_season_of_an_unmatched_show_is_not_warned_again(self) -> None:
        """When the whole show failed to bind, every season is Unknown too, but that miss
        is already warned once at the show level. The per-season path stays quiet so one
        unresolved show is not re-logged once per season."""
        for status in (identity.MatchStatus.UNMATCHED, identity.MatchStatus.AMBIGUOUS, None):
            with capture_logs() as logs:
                _facts(plex_rating_key=None, show_match_status=status)
            assert [e for e in logs if e["event"] == "scan.plex_unmatched"] == []

    def test_a_matched_season_is_not_warned(self) -> None:
        """A season that binds to Plex stays silent. The warning fires only on a real
        miss."""
        with capture_logs() as logs:
            _facts(plex_rating_key=700, show_match_status=identity.MatchStatus.MATCHED)

        assert [e for e in logs if e["event"] == "scan.plex_unmatched"] == []

    def test_a_matched_season_with_no_arrival_date_is_warned(self) -> None:
        """Matched to a Plex season, but with no added-at and no plays, dormancy is
        Unknown, so it abstains and shows only as kept-to-be-safe. A warning names it, the
        same as the movie path. This is a distinct event from the unmatched case, because
        this season did bind.
        """
        with capture_logs() as logs:
            facts = _facts(plex_rating_key=700, last_played=None, season_added_at=None)

        assert isinstance(facts.days_observed_unwatched, Unknown)
        warned = [e for e in logs if e["event"] == "scan.no_added_at"]
        assert len(warned) == 1
        assert warned[0]["log_level"] == "warning"
        assert warned[0]["media_type"] == "season"

    def test_a_resolved_season_has_known_dormancy(self) -> None:
        facts = _facts(plex_rating_key=700)
        assert isinstance(facts.days_observed_unwatched, Known)

    def test_a_season_being_streamed_is_known_streaming(self) -> None:
        facts = _facts(plex_rating_key=700, active_rating_keys={700})
        assert isinstance(facts.is_streaming_now, Known)
        assert facts.is_streaming_now.value is True

    def test_streaming_is_unknown_when_activity_could_not_be_read(self) -> None:
        """Even a resolved season goes Unknown-streaming if sessions could not be read.
        Not being able to look is never the same as nobody watching."""
        facts = _facts(plex_rating_key=700, activity_degraded=True)
        assert isinstance(facts.is_streaming_now, Unknown)

    def test_a_season_of_a_show_we_looked_up_and_found_unrated_is_absent(self) -> None:
        """There is no free per-season IMDb rating. Sonarr's ratings are flat TVDB. A
        show that could be looked up and was not found is Absent, meaning unrated, so a
        rating keep does not hold it.
        """
        facts = _facts()
        assert isinstance(facts.imdb_rating_tenths, Absent)

    def test_a_season_of_a_show_with_no_imdb_id_is_unknown(self) -> None:
        """Neither Sonarr nor Plex gave us an id, so no lookup happened. Absent here
        would claim we checked, and would withdraw every rating-based keep from a show
        purely because Sonarr lacks an id for it. See tests/test_fact_layer_states.py."""
        facts = _facts(rating_looked_up=False)
        assert isinstance(facts.imdb_rating_tenths, Unknown)
        assert isinstance(facts.imdb_votes, Unknown)

    def test_a_numbered_season_carries_its_rank(self) -> None:
        facts = _facts(rank=2)
        assert isinstance(facts.season_rank, Known) and facts.season_rank.value == 2

    def test_a_special_with_no_rank_is_absent_not_unknown(self) -> None:
        """rank_seasons deliberately leaves specials out of the ranking, so a special
        reaches here with rank=None. That is Absent, meaning the rank was looked up and
        it genuinely has no rank slot. Recording it as Unknown would claim Sonarr could
        not be read, and would make the SEASON_RANK signal say "could not tell which
        season this is", dragging the special's coverage down for a rank it was never
        meant to have.
        """
        facts = _facts(rank=None)
        assert isinstance(facts.season_rank, Absent)

    def test_a_season_is_always_managed(self) -> None:
        facts = _facts()
        assert isinstance(facts.is_managed, Known) and facts.is_managed.value is True

    def test_size_comes_from_sonarr(self) -> None:
        facts = _facts(season=_season(3, size=8 * GB))
        assert isinstance(facts.size_bytes, Known) and facts.size_bytes.value == 8 * GB

    def test_a_season_whose_size_sonarr_did_not_report_is_unknown(self) -> None:
        """The counterpart of the movie case. As Known(0) it would read as a real
        measurement, the maximum pressure a size signal can apply, and any "keep large
        files" rule would silently stop holding the season. See
        tests/test_fact_layer_states.py.
        """
        facts = _facts(season=_season(3, size=None))
        assert isinstance(facts.size_bytes, Unknown)

    def test_dormancy_is_measured_from_the_seasons_own_arrival(self) -> None:
        """A season backfilled into an old show arrived recently. Dormancy must count
        from the season's own added date, not the show's. Using the show's date would read
        a just-added season as decades dormant and condemn a file nobody could have
        watched.
        """
        facts = _facts(
            season_added_at=utcnow() - timedelta(days=5),  # files landed 5 days ago
            horizon=utcnow() - timedelta(days=4000),  # mature install
            last_played=None,
        )
        assert isinstance(facts.days_observed_unwatched, Known)
        assert facts.days_observed_unwatched.value < 30

    def test_a_resolved_season_with_no_arrival_date_is_unknown_dormancy(self) -> None:
        """A season with neither an arrival date nor a play has its dormancy Unknown,
        never a Known dormancy fabricated from the horizon. Unknown then forces the
        dormancy gates to protect. Both inputs are pinned absent deliberately, because a
        play alone is enough to measure from, which is what the next test covers.
        """
        facts = _facts(season_added_at=None, last_played=None)
        assert isinstance(facts.days_observed_unwatched, Unknown)

    def test_a_season_carries_whatever_rewatch_observations_it_is_given(self) -> None:
        """The builder is a pass-through for the show's rewatch pair, exactly like
        ``show_ended``. ``_judge_series`` computes the real Known/Absent/Unknown once per
        show and hands it in ready-made (``TestShowLevelRewatchFacts`` covers that
        computation end to end). A caller that supplies neither still gets the fail-closed
        ``Absent`` default, which is what a raw call with nothing to report should read as.
        """
        viewings = Known(value=3, source="tautulli")
        last_play_days = Known(value=17, source="tautulli")
        facts = _facts(
            plex_rating_key=700, rewatch_viewings=viewings, rewatch_last_play_days=last_play_days
        )
        assert facts.rewatch_viewings is viewings
        assert facts.rewatch_last_play_days is last_play_days

        defaulted = _facts(plex_rating_key=700)
        assert isinstance(defaulted.rewatch_viewings, Absent)
        assert isinstance(defaulted.rewatch_last_play_days, Absent)

    def test_no_arrival_date_but_a_play_measures_from_the_play(self) -> None:
        """This pins the case where an arrival date is missing but a play exists.

        Dormancy is days since the last play, so a play alone is a real measurement, and
        the number must come from the play, not the horizon. The horizon in this fixture
        is 4000 days back, so measuring from it instead would read as far more pressure
        than the play actually supports.
        """
        facts = _facts(season_added_at=None, last_played=utcnow() - timedelta(days=12))

        assert isinstance(facts.days_observed_unwatched, Known)
        # A range, not an equality. Production samples its own `utcnow()`, and comparing
        # two samples of the same clock is a source of flaky failures. 4000 is what the
        # horizon would give, so this discriminates the play from the fallback by three
        # orders of magnitude.
        assert 11 <= facts.days_observed_unwatched.value <= 13


# ---------------------------------------------------------------------------
# The load-bearing safety property
# ---------------------------------------------------------------------------


def _judge(facts: Any, guard: Any = None) -> str:
    """Run one season's facts through the real default policy and return its verdict."""
    gates = build_gates(DEFAULT_MOVIE_POLICY)
    results = list(evaluate_all(gates, facts).results)
    if guard is not None:
        results = [guard, *results]
    evaluation = Evaluation(results=results)
    signals = [
        SignalConfig(signal=s.signal, weight=s.weight, saturate_at=s.saturate_at, floor=s.floor)
        for s in DEFAULT_MOVIE_POLICY.signals
    ]
    sc = score_signals(signals, facts)
    return _verdict(evaluation, round(sc.value), round(sc.coverage * 10_000), DEFAULT_MOVIE_POLICY)


class TestNothingUnseenIsCondemned:
    def test_an_unresolved_season_cannot_be_condemned(self) -> None:
        facts = _facts(plex_rating_key=None)
        clean = season_evidence.guard_result(
            plan_series_prune(
                series_title="S", seasons=[_season(3)], keep_last=0, keep_first_season=False
            ),
            3,
        )
        assert _judge(facts, clean) == "abstain"

    def test_a_seen_dormant_unwatched_season_can_be_condemned(self) -> None:
        """The other side of the guarantee. When the evidence is real and the guards
        allow it, a season is condemnable, or the whole path would be inert.
        """
        facts = _facts(plex_rating_key=700, last_played=None, watchers_window=0)
        clean = season_evidence.guard_result(
            plan_series_prune(
                series_title="S",
                seasons=[_season(n) for n in range(1, 6)],
                keep_last=2,
                keep_first_season=False,
            ),
            3,
        )
        assert _judge(facts, clean) == "condemn"

    def test_a_guard_protected_season_wins_over_any_score(self) -> None:
        facts = _facts(plex_rating_key=700, last_played=None, watchers_window=0)
        protect = season_evidence.guard_result(
            plan_series_prune(series_title="S", seasons=[_season(3)], keep_last=1), 3
        )
        assert protect.outcome == PROTECT
        assert _judge(facts, protect) == "protect"

    def test_a_freshly_backfilled_season_of_an_old_show_is_not_condemned(self) -> None:
        """A mature install with a horizon about 4 years back, and an old show whose
        middle season the operator just backfilled. Its files landed 5 days ago and were
        never played. keep-last and keep-first protect the newest and first seasons by
        number, not this middle season, so only the dormancy discipline stands between it
        and a wrongful condemn. It must read as freshly arrived, not decades dormant.
        """
        facts = _facts(
            plex_rating_key=700,
            season_added_at=utcnow() - timedelta(days=5),
            horizon=utcnow() - timedelta(days=4000),
            last_played=None,
            watchers_window=0,
        )
        clean = season_evidence.guard_result(
            plan_series_prune(
                series_title="S",
                seasons=[_season(n) for n in range(1, 7)],
                keep_last=2,
                keep_first_season=True,
            ),
            3,  # a middle season -> not protected by keep-last or keep-first
        )
        assert clean.outcome == ABSTAIN and clean.blocked is False  # guard does not protect it
        assert _judge(facts, clean) != "condemn"

    def test_a_resolved_season_with_unknown_arrival_abstains(self) -> None:
        """A season resolved in Plex but whose arrival date could not be read must
        abstain, not be condemned off the horizon. This is the exact fail-open the movie
        path guards against.
        """
        facts = _facts(
            plex_rating_key=700,
            season_added_at=None,
            horizon=utcnow() - timedelta(days=4000),
            last_played=None,
            watchers_window=0,
        )
        clean = season_evidence.guard_result(
            plan_series_prune(
                series_title="S",
                seasons=[_season(n) for n in range(1, 6)],
                keep_last=2,
                keep_first_season=False,
            ),
            3,
        )
        assert _judge(facts, clean) == "abstain"


# ---------------------------------------------------------------------------
# Per-season watch statistics, against a real watch-history table
# ---------------------------------------------------------------------------


@pytest.fixture
async def cache_engine(tmp_path: Path) -> AsyncIterator[AsyncEngine]:
    settings = Settings(data_dir=tmp_path, secret_key="test-key")
    engine = create_cache_engine(settings)
    await history_sync.ensure_schema(engine)
    yield engine
    await engine.dispose()


async def _episode(
    engine: AsyncEngine,
    *,
    season_key: int,
    user_id: int,
    show_key: int = 42,
    days_ago: int = 1,
    episode: int | None = None,
    status: float | None = 1.0,
    percent_complete: int = 100,
) -> None:
    """``status=None`` is a row where Tautulli never reported whether the episode
    finished."""
    when = int((utcnow() - timedelta(days=days_ago)).timestamp())
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO watch_event (rating_key, parent_rating_key, "
                "grandparent_rating_key, user_id, watched_at, watched_status, "
                "percent_complete, media_type, media_index) "
                "VALUES (:rk, :season, :show, :uid, :ts, :status, :pct, 'episode', :ep)"
            ),
            {
                "rk": season_key * 1000 + user_id + (episode or 0),
                "season": season_key,
                "show": show_key,
                "uid": user_id,
                "ts": when,
                "ep": episode,
                "status": status,
                "pct": percent_complete,
            },
        )


class TestTheCacheIsRebuiltNotMigrated:
    """Cache tables are never migrated. The Alembic baseline says so, and they are
    rebuildable by definition. A stale shape is dropped and recreated, not patched.
    """

    async def test_a_stale_table_is_rebuilt_and_the_new_shape_holds_unknowns(
        self, cache_engine: AsyncEngine
    ) -> None:
        """The old table had `watched_status REAL NOT NULL`, which is what made "Tautulli
        did not say" indistinguishable from "did not finish". After the rebuild the column
        accepts NULL, and the emptied table makes the next sync a full one (history_sync
        goes incremental only when `before.rows` is non-zero), so the cache heals itself."""
        async with cache_engine.begin() as conn:
            await conn.execute(text("DROP TABLE watch_event"))
            await conn.execute(
                text(
                    "CREATE TABLE watch_event ("
                    " row_id INTEGER PRIMARY KEY, rating_key INTEGER NOT NULL,"
                    " parent_rating_key INTEGER, grandparent_rating_key INTEGER,"
                    " user_id INTEGER NOT NULL, watched_at INTEGER NOT NULL,"
                    " watched_status REAL NOT NULL, percent_complete INTEGER NOT NULL,"
                    " media_type TEXT NOT NULL)"
                )
            )
            await conn.execute(
                text(
                    "INSERT INTO watch_event VALUES "
                    "(1, 10, 700, 42, 1, 1700000000, 1.0, 100, 'episode')"
                )
            )

        await history_sync.ensure_schema(cache_engine)

        async with cache_engine.begin() as conn:
            cols = (await conn.execute(text("PRAGMA table_info(watch_event)"))).all()
            assert {row[1] for row in cols} >= {"media_index", "watched_status"}
            assert not any(row[1] == "watched_status" and row[3] for row in cols)
            # Emptied, so the next sync is a full one rather than an incremental gap.
            assert (await conn.execute(text("SELECT COUNT(*) FROM watch_event"))).scalar() == 0
            # An unreported completion is now storable as such.
            await conn.execute(
                text(
                    "INSERT INTO watch_event VALUES "
                    "(2, 11, 700, 42, 1, 1700000001, NULL, 50, 'episode', 4)"
                )
            )

    async def test_the_not_null_shape_a_real_upgrade_carries_is_rebuilt(
        self, cache_engine: AsyncEngine
    ) -> None:
        """The shape an actually-upgraded install has. All ten columns are in order, with
        the old `watched_status REAL NOT NULL`. The column names alone did not change, so
        a check that compares names alone would leave this table in place and the next
        sync would die on the first unreported completion. The rebuild has to fire on
        nullability instead.
        """
        async with cache_engine.begin() as conn:
            await conn.execute(text("DROP TABLE watch_event"))
            await conn.execute(
                text(
                    "CREATE TABLE watch_event ("
                    " row_id INTEGER PRIMARY KEY, rating_key INTEGER NOT NULL,"
                    " parent_rating_key INTEGER, grandparent_rating_key INTEGER,"
                    " user_id INTEGER NOT NULL, watched_at INTEGER NOT NULL,"
                    " watched_status REAL NOT NULL, percent_complete INTEGER NOT NULL,"
                    " media_type TEXT NOT NULL, media_index INTEGER)"
                )
            )
            await conn.execute(
                text(
                    "INSERT INTO watch_event VALUES "
                    "(1, 10, 700, 42, 1, 1700000000, 0.0, 100, 'episode', 3)"
                )
            )

        await history_sync.ensure_schema(cache_engine)

        async with cache_engine.begin() as conn:
            cols = (await conn.execute(text("PRAGMA table_info(watch_event)"))).all()
            assert not any(row[1] == "watched_status" and row[3] for row in cols)
            # The carried-over 0.0 was the ambiguous one the shape change existed to fix,
            # so it goes with the table rather than surviving as a fake "did not finish".
            assert (await conn.execute(text("SELECT COUNT(*) FROM watch_event"))).scalar() == 0
            await conn.execute(
                text(
                    "INSERT INTO watch_event VALUES "
                    "(2, 11, 700, 42, 1, 1700000001, NULL, 50, 'episode', 4)"
                )
            )

    async def test_a_current_table_is_left_alone(self, cache_engine: AsyncEngine) -> None:
        """Rebuilding a healthy cache would cost a full re-sync on every startup."""
        await _episode(cache_engine, season_key=720, user_id=1, episode=1)
        for _ in range(3):
            await history_sync.ensure_schema(cache_engine)
        async with cache_engine.begin() as conn:
            assert (await conn.execute(text("SELECT COUNT(*) FROM watch_event"))).scalar() == 1


class TestSeasonWatchStats:
    async def test_a_never_synced_cache_reads_as_no_plays(self, tmp_path: Path) -> None:
        """The schema guard against a cache that was never synced at all.

        The `cache_engine` fixture runs `ensure_schema` itself, so every other test in
        this class meets a table that already exists. This one takes a raw cache instead.
        Without the guard, the read raises `no such table: watch_event`, nothing catches
        it, and the whole scan aborts on a technical error instead of reading no plays.

        Reading no plays is not itself what keeps the file safe. An empty watch-history
        table resolves the horizon to `utcnow()`, so a season with an arrival date reads
        Known zero days dormant. `snapshot.scan` degrades the whole snapshot un-plannably
        on that empty table, which is the actual protection.
        """
        settings = Settings(data_dir=tmp_path, secret_key="test-key")
        engine = create_cache_engine(settings)
        try:
            stats = await season_scan.season_watch_stats(engine, {701}, window_days=365)
        finally:
            await engine.dispose()
        assert stats.last_played == {}
        assert stats.watchers_all_time == {}
        assert stats.watchers_window == {}

    async def test_plays_aggregate_by_season(self, cache_engine: AsyncEngine) -> None:
        await _episode(cache_engine, season_key=701, user_id=1)
        await _episode(cache_engine, season_key=701, user_id=2)
        await _episode(cache_engine, season_key=702, user_id=1)

        stats = await season_scan.season_watch_stats(cache_engine, {701, 702}, window_days=365)
        assert stats.watchers_all_time[701] == 2
        assert stats.watchers_all_time[702] == 1
        assert 701 in stats.last_played

    async def test_the_window_excludes_old_plays(self, cache_engine: AsyncEngine) -> None:
        await _episode(cache_engine, season_key=703, user_id=1, days_ago=2000)
        stats = await season_scan.season_watch_stats(cache_engine, {703}, window_days=365)
        assert stats.watchers_all_time[703] == 1
        assert stats.watchers_window.get(703, 0) == 0

    async def test_user_season_keys_are_collected(self, cache_engine: AsyncEngine) -> None:
        await _episode(cache_engine, season_key=704, user_id=1)
        await _episode(cache_engine, season_key=705, user_id=1)
        stats = await season_scan.season_watch_stats(cache_engine, {704, 705}, window_days=365)
        assert stats.user_season_keys[1] == {704, 705}

    async def test_a_users_latest_play_per_season_is_collected(
        self, cache_engine: AsyncEngine
    ) -> None:
        """Two plays under one season keep the newer timestamp. The mid-binge expiry
        judges a viewer by their most recent activity, never their first.
        """
        await _episode(cache_engine, season_key=706, user_id=1, days_ago=300, episode=1)
        await _episode(cache_engine, season_key=706, user_id=1, days_ago=5, episode=2)
        stats = await season_scan.season_watch_stats(cache_engine, {706}, window_days=365)
        when = stats.user_season_last[1][706]
        assert when is not None
        assert when > utcnow() - timedelta(days=6)

    async def test_an_unreported_completion_above_the_position_makes_it_unknown(
        self, cache_engine: AsyncEngine
    ) -> None:
        """The fail-open this closes, at the level it actually lives.

        Episodes 1-3 are recorded complete. Episodes 4-10 were played, but Tautulli never
        said whether they finished. Reading those as "not completed" puts the viewer at
        episode 3, so `sequential_protections` calls them still on this season, and the
        next season, the one they are actually about to watch, loses its protection. The
        default lookahead is 0, so nothing else covers it. Position must read as unknown,
        which drops the guard to season level.
        """
        for ep in (1, 2, 3):
            await _episode(cache_engine, season_key=710, user_id=1, episode=ep)
        for ep in (4, 10):
            await _episode(cache_engine, season_key=710, user_id=1, episode=ep, status=None)

        stats = await season_scan.season_watch_stats(cache_engine, {710}, window_days=365)

        assert 710 in stats.user_season_keys[1]  # they clearly watched it
        assert stats.user_season_progress.get(1, {}).get(710) is None  # ...but where is unknown

    async def test_an_unreported_completion_below_the_position_is_ignored(
        self, cache_engine: AsyncEngine
    ) -> None:
        """The other side, so this does not become "any odd row blinds the guard".

        An unreported episode 2 cannot change where a viewer who has completed episode 9
        has got to, so the position stays exact and the guard keeps its precision.
        """
        for ep in (1, 9):
            await _episode(cache_engine, season_key=711, user_id=1, episode=ep)
        await _episode(cache_engine, season_key=711, user_id=1, episode=2, status=None)

        stats = await season_scan.season_watch_stats(cache_engine, {711}, window_days=365)

        assert stats.user_season_progress[1][711] == 9

    async def test_a_genuine_zero_still_means_not_finished(self, cache_engine: AsyncEngine) -> None:
        """0.0 is a real answer, "started it, did not finish", and must keep behaving as
        one. Tautulli reporting a viewer 25%, 50%, or 75% through an episode says they are
        still at it, while 0 says they are not, so only 0 leaves the position exact.
        """
        await _episode(cache_engine, season_key=712, user_id=1, episode=3)
        await _episode(cache_engine, season_key=712, user_id=1, episode=4, status=0.0)

        stats = await season_scan.season_watch_stats(cache_engine, {712}, window_days=365)

        assert stats.user_season_progress[1][712] == 3


class TestProgressByUser:
    def test_progress_is_scoped_to_this_show(self) -> None:
        """A user's progress in another series must not leak in. Only this show's season
        keys are consulted, mapped to season numbers with their completed-episode
        positions.
        """
        stats = season_scan.SeasonWatchStats(
            user_season_keys={7: {701, 702, 999}},
            user_season_progress={7: {701: 4, 702: 9}},
        )
        # 701 -> season 2, 702 -> season 3. 999 belongs to another show and is ignored.
        this_show = {701: 2, 702: 3}
        assert season_scan._progress_by_user(stats, this_show) == {"7": {2: 4, 3: 9}}

    def test_a_touched_season_with_no_episode_index_is_none(self) -> None:
        # Touched the season (any play) but no episode index -> position unknown -> None,
        # which drops the guard to its season-level fallback for that season.
        stats = season_scan.SeasonWatchStats(user_season_keys={7: {701}}, user_season_progress={})
        assert season_scan._progress_by_user(stats, {701: 2}) == {"7": {2: None}}


class TestLastWatchedByUser:
    def test_the_shows_most_recent_play_wins_and_other_shows_are_ignored(self) -> None:
        old = datetime(2025, 1, 1, tzinfo=UTC)
        new = datetime(2026, 6, 1, tzinfo=UTC)
        elsewhere = datetime(2026, 7, 1, tzinfo=UTC)
        stats = season_scan.SeasonWatchStats(
            user_season_keys={7: {701, 702, 999}},
            user_season_last={7: {701: old, 702: new, 999: elsewhere}},
        )
        # 999 is another show. Its recency must not keep this show's hold alive.
        assert season_scan._last_watched_by_user(stats, {701: 2, 702: 3}) == {"7": new}

    def test_any_unreadable_timestamp_means_unknown(self) -> None:
        """One readable-old and one unreadable play. The unreadable one could be recent,
        so the viewer's whole-show recency is None and their hold stays.
        """
        old = datetime(2025, 1, 1, tzinfo=UTC)
        stats = season_scan.SeasonWatchStats(
            user_season_keys={7: {701, 702}},
            user_season_last={7: {701: old, 702: None}},
        )
        assert season_scan._last_watched_by_user(stats, {701: 2, 702: 3}) == {"7": None}


# ---------------------------------------------------------------------------
# End to end, with fake clients
# ---------------------------------------------------------------------------


def _degrade_sink() -> tuple[list[str], Any]:
    reasons: list[str] = []
    return reasons, reasons.append


async def _seed_ratings(engine: AsyncEngine, ratings: dict[str, tuple[float, int]]) -> None:
    """A fresh, non-stale IMDb dataset holding exactly ``{imdb id: (score, votes)}``.

    Without it, ``gather`` degrades the snapshot on the ratings read, and a degraded scan
    abstains on everything. That would let a test asserting "not condemned" pass for a
    reason it never meant to check.
    """
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "CREATE TABLE IF NOT EXISTS imdb_rating "
                "(tconst TEXT PRIMARY KEY, average_rating REAL, num_votes INTEGER)"
            )
        )
        await conn.execute(
            text(
                "CREATE TABLE IF NOT EXISTS imdb_dataset_sync (id INTEGER PRIMARY KEY, "
                "synced_at INTEGER NOT NULL, row_count INTEGER NOT NULL)"
            )
        )
        await conn.execute(
            text(
                "INSERT OR REPLACE INTO imdb_dataset_sync (id, synced_at, row_count) "
                "VALUES (1, :ts, :n)"
            ),
            {"ts": int(utcnow().timestamp()), "n": 1_000_000},
        )
        for tconst, (score, votes) in ratings.items():
            await conn.execute(
                text(
                    "INSERT OR REPLACE INTO imdb_rating (tconst, average_rating, num_votes) "
                    "VALUES (:t, :s, :v)"
                ),
                {"t": tconst, "s": score, "v": votes},
            )


def _source(client: Any) -> season_scan.SonarrSource:
    return season_scan.SonarrSource(client=client, instance_id=1, name="hd")


def _season_rows(children: dict[int, list[dict[str, Any]]]) -> dict[int, list[PlexSeasonRow]]:
    """Turn ``{show_key: [{media_index, rating_key, added_at?}]}`` into the sweep's shape,
    so a test can describe seasons the same way for the sweep and the Tautulli fallback."""
    return {
        show: [
            PlexSeasonRow(
                season_index=c.get("media_index"),
                rating_key=int(c["rating_key"]),
                added_at=c.get("added_at"),
            )
            for c in rows
        ]
        for show, rows in children.items()
    }


class _FakePlexGuids:
    """A stand-in for the plexapi sweeps. It provides the GUID index (rating_key ->
    PlexItem) and the season index (show rating_key -> its season rows). ``seasons``
    empty means the sweep found nothing, which sends every show to the per-show Tautulli
    fallback.
    """

    def __init__(
        self,
        items: dict[int, identity.PlexItem],
        seasons: dict[int, list[PlexSeasonRow]] | None = None,
        *,
        season_index_error: bool = False,
    ) -> None:
        self._items = items
        self._seasons = seasons or {}
        self._season_index_error = season_index_error

    async def library_guid_index(
        self, *, section_type: str, allowed_sections: set[int] | None = None
    ) -> dict[int, identity.PlexItem]:
        return self._items

    async def library_season_index(
        self, *, allowed_sections: set[int] | None = None
    ) -> dict[int, list[PlexSeasonRow]]:
        if self._season_index_error:
            raise PlexError("season sweep failed")
        return self._seasons


class TestGatherEndToEnd:
    async def test_a_matched_prunable_season_is_gathered_with_its_plex_key(
        self, cache_engine: AsyncEngine
    ) -> None:
        series = [
            {
                "id": 42,
                "title": "Long Show",
                "year": 2005,
                "status": "ended",
                "ended": True,
                "imdbId": "tt0001",
                "seasons": [_season_payload(n) for n in range(1, 6)],  # 1..5
            }
        ]
        tautulli = show_library(
            rows=[{"rating_key": 900, "title": "Long Show", "year": 2005, "added_at": "1000000"}],
            children={900: [{"media_index": n, "rating_key": 900 + n} for n in range(1, 6)]},
        )
        _reasons, degrade = _degrade_sink()

        judgments = await season_scan.gather(
            cache_engine,
            sonarrs=[_source(FakeSonarr(series_rows=series))],
            tautulli=tautulli,
            horizon=utcnow() - timedelta(days=4000),
            reach_days=4000,
            active_rating_keys=set(),
            activity_degraded=False,
            season_policy=_season_policy(keep_last_seasons=2, keep_first_season=True),
            window_days=365,
            whitelisted=set(),
            degrade=degrade,
            watch_marks={},
            seen_marks={},
            seen_scans=[],
            seen_absence_days=7,
        )

        by_key = {j.media_key: j for j in judgments}
        # Season 3 is prunable (outside keep-last 2, not the first), and resolves to its
        # Plex key.
        assert "sonarr:1:42:3" in by_key
        assert by_key["sonarr:1:42:3"].plex_rating_key == 903
        # The card poster comes from the show's key (900), never the season's. A season
        # often has no poster of its own, so the season key would 404 to a placeholder.
        assert by_key["sonarr:1:42:3"].poster_rating_key == 900
        # The first and last-two seasons are protected, and emitted so the panel shows why.
        assert by_key["sonarr:1:42:1"].guard_result.outcome == PROTECT
        assert by_key["sonarr:1:42:5"].guard_result.outcome == PROTECT

    async def test_a_show_the_sweep_missed_falls_back_to_the_per_show_read(
        self, cache_engine: AsyncEngine
    ) -> None:
        """The season sweep is present but returns nothing for this show, either from a
        partial sweep or a show the sweep could not place. Resolution must fall back to
        the per-show Tautulli read instead of losing the season, so this path can resolve
        at least as much as the one it replaced.
        """
        series = [
            {
                "id": 42,
                "title": "Long Show",
                "year": 2005,
                "status": "ended",
                "ended": True,
                "imdbId": "tt0001",
                "seasons": [_season_payload(n) for n in range(1, 6)],
            }
        ]
        tautulli = show_library(
            rows=[{"rating_key": 900, "title": "Long Show", "year": 2005, "added_at": "1000000"}],
            children={900: [{"media_index": n, "rating_key": 900 + n} for n in range(1, 6)]},
        )
        # Plex is linked and matches the show, but the season sweep is empty for it.
        plex = _FakePlexGuids(
            {
                900: identity.PlexItem(
                    rating_key=900,
                    title="Long Show",
                    year=2005,
                    added_at=None,
                    ids=identity.ExternalIds.of(imdb="tt0001"),
                )
            },
            seasons={},
        )
        _reasons, degrade = _degrade_sink()

        judgments = await season_scan.gather(
            cache_engine,
            sonarrs=[_source(FakeSonarr(series_rows=series))],
            tautulli=tautulli,
            plex=plex,  # type: ignore[arg-type]
            horizon=utcnow() - timedelta(days=4000),
            reach_days=4000,
            active_rating_keys=set(),
            activity_degraded=False,
            season_policy=_season_policy(keep_last_seasons=2, keep_first_season=True),
            window_days=365,
            whitelisted=set(),
            degrade=degrade,
            watch_marks={},
            seen_marks={},
            seen_scans=[],
            seen_absence_days=7,
        )

        by_key = {j.media_key: j for j in judgments}
        # Season 3 still resolves to its Plex key, via the per-show fallback.
        assert by_key["sonarr:1:42:3"].plex_rating_key == 903

    async def test_a_raising_season_sweep_falls_back_per_show_and_does_not_degrade(
        self, cache_engine: AsyncEngine
    ) -> None:
        """library_season_index raising, rather than returning empty, hits the ``except
        PlexError`` branch. The whole library falls back to the per-show read instead of
        degrading, since the same data is reachable one show at a time. The empty-dict
        path above exercises different code than this except.
        """
        series = [
            {
                "id": 42,
                "title": "Long Show",
                "year": 2005,
                "status": "ended",
                "ended": True,
                "imdbId": "tt0001",
                "seasons": [_season_payload(n) for n in range(1, 6)],
            }
        ]
        tautulli = show_library(
            rows=[{"rating_key": 900, "title": "Long Show", "year": 2005, "added_at": "1000000"}],
            children={900: [{"media_index": n, "rating_key": 900 + n} for n in range(1, 6)]},
        )
        plex = _FakePlexGuids(
            {
                900: identity.PlexItem(
                    rating_key=900,
                    title="Long Show",
                    year=2005,
                    added_at=None,
                    ids=identity.ExternalIds.of(imdb="tt0001"),
                )
            },
            season_index_error=True,  # the sweep raises
        )
        reasons, degrade = _degrade_sink()

        judgments = await season_scan.gather(
            cache_engine,
            sonarrs=[_source(FakeSonarr(series_rows=series))],
            tautulli=tautulli,
            plex=plex,  # type: ignore[arg-type]
            horizon=utcnow() - timedelta(days=4000),
            reach_days=4000,
            active_rating_keys=set(),
            activity_degraded=False,
            season_policy=_season_policy(keep_last_seasons=2, keep_first_season=True),
            window_days=365,
            whitelisted=set(),
            degrade=degrade,
            watch_marks={},
            seen_marks={},
            seen_scans=[],
            seen_absence_days=7,
        )

        by_key = {j.media_key: j for j in judgments}
        # Season 3 resolves via the per-show fallback, and the raise did not degrade the
        # scan. It logs a warning and falls back instead. The only degradations here are
        # unrelated.
        assert by_key["sonarr:1:42:3"].plex_rating_key == 903
        assert not any("sweep" in r.lower() or "season" in r.lower() for r in reasons)

    async def test_episodes_are_not_fetched_when_keep_in_progress_is_off(
        self, cache_engine: AsyncEngine
    ) -> None:
        """With mid-binge protection off, ``season_final_episode`` is never consulted, so
        the whole Sonarr episodes() fan-out is skipped. Skipping only ever keeps more.
        """
        series = [
            {
                "id": 42,
                "title": "Long Show",
                "year": 2005,
                "status": "ended",
                "ended": True,
                "imdbId": "tt0001",
                "seasons": [_season_payload(n) for n in range(1, 6)],
            }
        ]
        sonarr = FakeSonarr(
            series_rows=series, episode_rows={42: [{"seasonNumber": 3, "episodeNumber": 8}]}
        )
        tautulli = show_library(
            rows=[{"rating_key": 900, "title": "Long Show", "year": 2005, "added_at": "1000000"}],
            children={900: [{"media_index": n, "rating_key": 900 + n} for n in range(1, 6)]},
        )
        _reasons, degrade = _degrade_sink()

        off = await season_scan.gather(
            cache_engine,
            sonarrs=[_source(sonarr)],
            tautulli=tautulli,
            horizon=utcnow() - timedelta(days=4000),
            reach_days=4000,
            active_rating_keys=set(),
            activity_degraded=False,
            season_policy=_season_policy(
                keep_last_seasons=2, keep_first_season=True, keep_in_progress=False
            ),
            window_days=365,
            whitelisted=set(),
            degrade=degrade,
            watch_marks={},
            seen_marks={},
            seen_scans=[],
            seen_absence_days=7,
        )
        assert sonarr.episodes_called == []  # the fan-out was skipped
        assert "sonarr:1:42:3" in {j.media_key for j in off}  # seasons still resolve

        # With the guard on instead, the fan-out runs, confirming the skip above is a real
        # branch.
        sonarr_on = FakeSonarr(
            series_rows=series, episode_rows={42: [{"seasonNumber": 3, "episodeNumber": 8}]}
        )
        await season_scan.gather(
            cache_engine,
            sonarrs=[_source(sonarr_on)],
            tautulli=tautulli,
            horizon=utcnow() - timedelta(days=4000),
            reach_days=4000,
            active_rating_keys=set(),
            activity_degraded=False,
            season_policy=_season_policy(
                keep_last_seasons=2, keep_first_season=True, keep_in_progress=True
            ),
            window_days=365,
            whitelisted=set(),
            degrade=degrade,
            watch_marks={},
            seen_marks={},
            seen_scans=[],
            seen_absence_days=7,
        )
        assert sonarr_on.episodes_called == [42]

    async def test_a_show_duplicated_in_plex_narrows_by_its_folder_name(
        self, cache_engine: AsyncEngine
    ) -> None:
        """Two Plex rows share the show's tvdb id (an HD and a 4K section). The series
        folder name picks the copy this Sonarr instance manages, and the seasons resolve
        under that copy's rating key."""
        series = [
            {
                "id": 56,
                "title": "Duplicated Show",
                "year": 2020,
                "status": "ended",
                "ended": True,
                "tvdbId": 4242,
                "path": "/tv-4k/Duplicated Show (2160p)",
                "seasons": [_season_payload(n) for n in range(1, 6)],
            }
        ]
        tautulli = show_library(
            rows=[
                {
                    "rating_key": 800,
                    "title": "Duplicated Show",
                    "year": 2020,
                    "added_at": "1000000",
                },
                {
                    "rating_key": 900,
                    "title": "Duplicated Show",
                    "year": 2020,
                    "added_at": "1000000",
                },
            ],
            children={900: [{"media_index": n, "rating_key": 900 + n} for n in range(1, 6)]},
        )
        plex = _FakePlexGuids(
            {
                800: identity.PlexItem(
                    rating_key=800,
                    title="Duplicated Show",
                    year=2020,
                    added_at=None,
                    ids=identity.ExternalIds.of(tvdb=4242),
                    file_basename="duplicated show",
                    files=(identity.PlexFile("duplicated show"),),
                ),
                900: identity.PlexItem(
                    rating_key=900,
                    title="Duplicated Show",
                    year=2020,
                    added_at=None,
                    ids=identity.ExternalIds.of(tvdb=4242),
                    file_basename="duplicated show (2160p)",
                    files=(identity.PlexFile("duplicated show (2160p)"),),
                ),
            },
            # The season sweep carries both copies' seasons. The 4K copy (900) is the one
            # the folder name binds this Sonarr to, so its keys are the ones that resolve.
            seasons=_season_rows(
                {
                    800: [{"media_index": n, "rating_key": 800 + n} for n in range(1, 6)],
                    900: [{"media_index": n, "rating_key": 900 + n} for n in range(1, 6)],
                }
            ),
        )
        _reasons, degrade = _degrade_sink()

        judgments = await season_scan.gather(
            cache_engine,
            sonarrs=[_source(FakeSonarr(series_rows=series))],
            tautulli=tautulli,
            plex=plex,  # type: ignore[arg-type]
            horizon=utcnow() - timedelta(days=4000),
            reach_days=4000,
            active_rating_keys=set(),
            activity_degraded=False,
            season_policy=_season_policy(keep_last_seasons=2, keep_first_season=True),
            window_days=365,
            whitelisted=set(),
            degrade=degrade,
            watch_marks={},
            seen_marks={},
            seen_scans=[],
            seen_absence_days=7,
        )

        pruned = next(j for j in judgments if j.media_key == "sonarr:1:56:3")
        assert pruned.matched_by is identity.MatchedBy.ID_AND_BASENAME
        assert pruned.match_status is identity.MatchStatus.MATCHED
        assert pruned.plex_rating_key == 903  # season 3 under the 4K copy, never the HD one

    async def test_plex_supplies_the_rating_and_poster_when_sonarr_cannot(
        self, cache_engine: AsyncEngine
    ) -> None:
        """Sonarr has no imdbId (common for reality/recent shows), but the show matches
        Plex by tvdb, and Plex carries the imdb id. The rating comes through on the Plex
        id, and the card poster uses the show's key, so neither the rating nor the poster
        is lost to a Sonarr/TVDB metadata gap.
        """
        await _seed_ratings(cache_engine, {"tt7777": (7.1, 38)})
        series = [
            {
                "id": 55,
                "title": "Reality Show",
                "year": 2020,
                "status": "ended",
                "ended": True,
                "tvdbId": 4242,  # matches Plex by tvdb, no imdbId from Sonarr
                "tmdbId": 999,
                "titleSlug": "reality-show",
                "seasons": [_season_payload(n) for n in range(1, 6)],
            }
        ]
        tautulli = show_library(
            rows=[
                {"rating_key": 800, "title": "Reality Show", "year": 2020, "added_at": "1000000"}
            ],
            children={800: [{"media_index": n, "rating_key": 800 + n} for n in range(1, 6)]},
        )
        plex = _FakePlexGuids(
            {
                800: identity.PlexItem(
                    rating_key=800,
                    title="Reality Show",
                    year=2020,
                    added_at=None,
                    ids=identity.ExternalIds.of(tvdb=4242, imdb="tt7777"),
                    content_rating="TV-PG",
                    runtime_minutes=50,
                    ratings=(
                        Rating(
                            source=RatingSource.ROTTEN_TOMATOES_AUDIENCE,
                            value=7.6,
                            votes=None,
                            provider="plex",
                        ),
                    ),
                )
            },
            seasons=_season_rows(
                {800: [{"media_index": n, "rating_key": 800 + n} for n in range(1, 6)]}
            ),
        )
        _reasons, degrade = _degrade_sink()

        judgments = await season_scan.gather(
            cache_engine,
            sonarrs=[_source(FakeSonarr(series_rows=series))],
            tautulli=tautulli,
            plex=plex,  # type: ignore[arg-type]
            horizon=utcnow() - timedelta(days=4000),
            reach_days=4000,
            active_rating_keys=set(),
            activity_degraded=False,
            season_policy=_season_policy(keep_last_seasons=2, keep_first_season=True),
            window_days=365,
            whitelisted=set(),
            degrade=degrade,
            watch_marks={},
            seen_marks={},
            seen_scans=[],
            seen_absence_days=7,
        )

        pruned = next(j for j in judgments if j.media_key == "sonarr:1:55:3")
        # Rating resolved via the Plex-supplied imdb id, even though Sonarr had none.
        assert isinstance(pruned.facts.imdb_rating_tenths, Known)
        assert pruned.facts.imdb_rating_tenths.value == 71
        # Poster uses the show's key (800), not the season's (803).
        assert pruned.poster_rating_key == 800
        assert pruned.plex_rating_key == 803
        # The show's display metadata is inherited by every season row. This covers the
        # Sonarr web coordinate, certification, runtime, and a ratings row whose IMDb
        # entry is the same dataset number the scoring signal froze, never a second
        # source.
        assert pruned.title_slug == "reality-show"
        # Outbound-link coordinates. The show's tmdb id, and the imdb id resolved the
        # same way the rating was, since Sonarr had none, so the Plex-matched one serves.
        assert pruned.tmdb_id == 999
        assert pruned.imdb_id == "tt7777"
        # Sonarr's native tvdb id rides onto every season row too, so Scales can join a
        # request to this show even when it has no tmdb id (services.fairness).
        assert pruned.tvdb_id == 4242
        assert pruned.content_rating == "TV-PG"
        assert pruned.runtime_minutes == 50
        # The show's ended-ness is a show-level fact too. It is one reading of the series,
        # stamped onto every season row so the card can state it without a second fetch.
        assert pruned.show_status == "ended"
        assert pruned.ratings_json is not None
        stored = json.loads(pruned.ratings_json)
        assert stored["imdb"] == 71
        assert stored["imdb_votes"] == 38
        assert stored["rotten_tomatoes_audience"] == 76

    async def test_a_tvdb_only_keep_row_still_whitelists_the_show(
        self, cache_engine: AsyncEngine
    ) -> None:
        """The membership lookup must pass every id the show carries. A show with no
        imdbId in Sonarr (common) whose keep tag was stored under its tvdb id must still
        read whitelisted. An explicitly-set protection that fails open on the deletion
        path is the worst possible failure.
        """
        series = [
            {
                "id": 77,
                "title": "Tagged Show",
                "year": 2018,
                "status": "ended",
                "ended": True,
                "tvdbId": 5150,  # no imdbId from Sonarr
                "seasons": [_season_payload(n) for n in range(1, 6)],
            }
        ]
        tautulli = show_library(
            rows=[{"rating_key": 700, "title": "Tagged Show", "year": 2018, "added_at": "1000000"}],
            children={700: [{"media_index": n, "rating_key": 700 + n} for n in range(1, 6)]},
        )
        keep_row = lists.Membership(
            slug="arr-tag-keep",
            display_name='Sonarr tag "reaper-keep"',
            mode=lists.ListMode.HARD,
            kind=lists.ListKind.WHITELIST,
            rank=None,
        )
        index = lists.MembershipIndex(
            _by_imdb={}, _by_tmdb={}, _by_tvdb={5150: ((0, "tv", keep_row),)}, _by_plex_key={}
        )
        _reasons, degrade = _degrade_sink()

        judgments = await season_scan.gather(
            cache_engine,
            sonarrs=[_source(FakeSonarr(series_rows=series))],
            tautulli=tautulli,
            horizon=utcnow() - timedelta(days=4000),
            reach_days=4000,
            active_rating_keys=set(),
            activity_degraded=False,
            season_policy=_season_policy(keep_last_seasons=2, keep_first_season=True),
            window_days=365,
            whitelisted=set(),
            degrade=degrade,
            membership_index=index,
            watch_marks={},
            seen_marks={},
            seen_scans=[],
            seen_absence_days=7,
        )

        assert judgments, "the show's seasons must still be gathered"
        for judgment in judgments:
            assert isinstance(judgment.facts.is_whitelisted, Known)
            assert judgment.facts.is_whitelisted.value is True

    async def test_sonarrs_unknown_id_sentinel_does_not_shadow_the_one_plex_matched(
        self, cache_engine: AsyncEngine
    ) -> None:
        """Sonarr's ``tt0000000`` "unknown" sentinel must not stand in for the imdb id
        Plex matched. The sentinel is truthy, so if the ids were not cleaned before the
        membership lookup, the lookup would run under an id no keep row carries, and the
        row stored under the real id would go unfound. The show owns every season beneath
        it, so one shadowed id would un-protect the whole series at once. This is pinned
        end to end rather than left to the hygiene gate, because that gate only reads the
        source for an ``ExternalIds.of`` call and would stay green even if the lookup were
        re-pointed at the raw payload.

        The keep row is stored under imdb alone, on purpose. Giving it a tvdb id as well
        would let the lookup find it by tvdb regardless of what the imdb arm does, and the
        test would stop discriminating.
        """
        series = [
            {
                "id": 91,
                "title": "Sentinel Show",
                "year": 2019,
                "status": "ended",
                "ended": True,
                "tvdbId": 5150,
                "imdbId": "tt0000000",  # the sentinel, not a real id
                "seasons": [_season_payload(n) for n in range(1, 6)],
            }
        ]
        tautulli = show_library(
            rows=[
                {"rating_key": 900, "title": "Sentinel Show", "year": 2019, "added_at": "1000000"}
            ],
            children={900: [{"media_index": n, "rating_key": 900 + n} for n in range(1, 6)]},
        )
        plex = _FakePlexGuids(
            {
                900: identity.PlexItem(
                    rating_key=900,
                    title="Sentinel Show",
                    year=2019,
                    added_at=None,
                    ids=identity.ExternalIds.of(tvdb=5150, imdb="tt0000042"),
                    content_rating="TV-14",
                    runtime_minutes=45,
                    ratings=(),
                )
            },
            seasons=_season_rows(
                {900: [{"media_index": n, "rating_key": 900 + n} for n in range(1, 6)]}
            ),
        )
        keep_row = lists.Membership(
            slug="arr-tag-keep",
            display_name='Sonarr tag "reaper-keep"',
            mode=lists.ListMode.HARD,
            kind=lists.ListKind.WHITELIST,
            rank=None,
        )
        index = lists.MembershipIndex(
            _by_imdb={"tt0000042": ((0, "tv", keep_row),)},
            _by_tmdb={},
            _by_tvdb={},
            _by_plex_key={},
        )
        _reasons, degrade = _degrade_sink()

        judgments = await season_scan.gather(
            cache_engine,
            sonarrs=[_source(FakeSonarr(series_rows=series))],
            tautulli=tautulli,
            plex=plex,  # type: ignore[arg-type]
            horizon=utcnow() - timedelta(days=4000),
            reach_days=4000,
            active_rating_keys=set(),
            activity_degraded=False,
            season_policy=_season_policy(keep_last_seasons=2, keep_first_season=True),
            window_days=365,
            whitelisted=set(),
            degrade=degrade,
            membership_index=index,
            watch_marks={},
            seen_marks={},
            seen_scans=[],
            seen_absence_days=7,
        )

        assert judgments, "the show's seasons must still be gathered"
        for judgment in judgments:
            assert isinstance(judgment.facts.is_whitelisted, Known)
            assert judgment.facts.is_whitelisted.value is True, (
                "the sentinel shadowed the imdb id Plex matched, so the keep row stored under "
                "the real id was not found and every season of the show lost its protection"
            )

    async def test_an_unmatched_series_yields_unresolved_seasons(
        self, cache_engine: AsyncEngine
    ) -> None:
        """Plex has not matched the show. Its prunable seasons still appear, so the owner
        learns Plex failed to match them, but with no Plex key and Unknown facts, so they
        can only abstain.
        """
        series = [
            {
                "id": 7,
                "title": "Unmatched Show",
                "status": "ended",
                "ended": True,
                "seasons": [_season_payload(n) for n in range(1, 6)],
            }
        ]
        tautulli = show_library([])
        _reasons, degrade = _degrade_sink()

        judgments = await season_scan.gather(
            cache_engine,
            sonarrs=[_source(FakeSonarr(series_rows=series))],
            tautulli=tautulli,
            horizon=utcnow() - timedelta(days=4000),
            reach_days=4000,
            active_rating_keys=set(),
            activity_degraded=False,
            season_policy=_season_policy(keep_last_seasons=2, keep_first_season=True),
            window_days=365,
            whitelisted=set(),
            degrade=degrade,
            watch_marks={},
            seen_marks={},
            seen_scans=[],
            seen_absence_days=7,
        )
        pruned = next(j for j in judgments if j.media_key == "sonarr:1:7:3")
        assert pruned.plex_rating_key is None
        assert isinstance(pruned.facts.days_observed_unwatched, Unknown)

    async def test_a_fully_protected_short_show_is_surfaced_as_kept(
        self, cache_engine: AsyncEngine
    ) -> None:
        """A show with no prunable season is not dropped. It is gathered and surfaced as
        kept, every season protected by its guard, so content is never hidden from the UI.
        """
        series = [
            {
                "id": 9,
                "title": "Two Seasons",
                "status": "ended",
                "ended": True,
                "seasons": [_season_payload(1), _season_payload(2)],
            }
        ]
        _reasons, degrade = _degrade_sink()
        judgments = await season_scan.gather(
            cache_engine,
            sonarrs=[_source(FakeSonarr(series_rows=series))],
            tautulli=show_library([]),
            # Deep enough to span the default hold, so the two keep floors this test is
            # about are what hold these seasons. At reach 0, the mid-binge guard cannot be
            # established and holds every season on its own, which would make the
            # assertion below pass even with both floors removed.
            horizon=utcnow() - timedelta(days=4000),
            reach_days=4000,
            active_rating_keys=set(),
            activity_degraded=False,
            season_policy=_season_policy(keep_last_seasons=2, keep_first_season=True),
            window_days=365,
            whitelisted=set(),
            degrade=degrade,
            watch_marks={},
            seen_marks={},
            seen_scans=[],
            seen_absence_days=7,
        )
        # Both content-bearing seasons appear, each protected by a guard (never condemned).
        assert {j.media_key for j in judgments} == {"sonarr:1:9:1", "sonarr:1:9:2"}
        assert all(j.guard_result.outcome is PROTECT for j in judgments)

    async def test_a_candidate_show_logs_its_decision(self, cache_engine: AsyncEngine) -> None:
        """Every scanned series emits one greppable decision line. A show with a
        prunable season records outcome=candidate, the prunable season numbers, and the
        raw per-season file counts Sonarr reported. This is the record an operator greps
        by title.
        """
        series = [
            {
                "id": 42,
                "title": "Long Show",
                "year": 2005,
                "status": "ended",
                "ended": True,
                "tvdbId": 700,
                "seasons": [_season_payload(n) for n in range(1, 6)],
            }
        ]
        _reasons, degrade = _degrade_sink()
        with capture_logs() as logs:
            await season_scan.gather(
                cache_engine,
                sonarrs=[_source(FakeSonarr(series_rows=series))],
                tautulli=show_library([]),
                horizon=utcnow() - timedelta(days=4000),
                reach_days=4000,
                active_rating_keys=set(),
                activity_degraded=False,
                season_policy=_season_policy(keep_last_seasons=2, keep_first_season=True),
                window_days=365,
                whitelisted=set(),
                degrade=degrade,
                watch_marks={},
                seen_marks={},
                seen_scans=[],
                seen_absence_days=7,
            )
        decisions = [e for e in logs if e["event"] == "season_scan.series_decision"]
        assert len(decisions) == 1
        d = decisions[0]
        assert d["outcome"] == "candidate"
        assert d["title"] == "Long Show"
        assert d["tvdb_id"] == 700
        assert sorted(d["prunable"]) == [2, 3]  # outside keep-last 2, not the first
        assert {s["n"] for s in d["seasons"]} == {1, 2, 3, 4, 5}
        # The per-instance read count, the twin of snapshot.radarr's movie count.
        sonarr = [e for e in logs if e["event"] == "season_scan.sonarr"]
        assert len(sonarr) == 1
        assert sonarr[0]["series"] == 1

    async def test_a_fully_protected_show_logs_the_keep_reasons(
        self, cache_engine: AsyncEngine
    ) -> None:
        """A show with nothing prunable is dropped from the queue, but its decision line
        names outcome=fully_protected and why each on-disk season is kept. That makes "why
        isn't my show in review" answerable without re-running the scan.
        """
        series = [
            {
                "id": 9,
                "title": "Two Seasons",
                "status": "ended",
                "ended": True,
                "seasons": [_season_payload(1), _season_payload(2)],
            }
        ]
        _reasons, degrade = _degrade_sink()
        with capture_logs() as logs:
            await season_scan.gather(
                cache_engine,
                sonarrs=[_source(FakeSonarr(series_rows=series))],
                tautulli=show_library([]),
                horizon=utcnow(),
                reach_days=0,
                active_rating_keys=set(),
                activity_degraded=False,
                season_policy=_season_policy(keep_last_seasons=2, keep_first_season=True),
                window_days=365,
                whitelisted=set(),
                degrade=degrade,
                watch_marks={},
                seen_marks={},
                seen_scans=[],
                seen_absence_days=7,
            )
        decisions = [e for e in logs if e["event"] == "season_scan.series_decision"]
        assert len(decisions) == 1
        d = decisions[0]
        assert d["outcome"] == "fully_protected"
        assert d["prunable"] == []
        assert {p["season"] for p in d["protected"]} == {1, 2}
        assert all(p["reason"] for p in d["protected"])

    async def test_a_shallow_mirror_holds_every_season_of_a_prunable_show(
        self, cache_engine: AsyncEngine
    ) -> None:
        """The reach affects the mid-binge guard too, not only the watcher counts.

        The same show under the same policy runs twice, differing only in how far back the
        watch history goes. Past the hold, the middle seasons are candidates. Short of it,
        a viewer whose plays all predate the horizon leaves no rows, so "nobody is
        part-way through" is a claim the history cannot support, and every season is held
        instead.

        The two reach values straddle the *stated* 200-day hold, not the 180-day default,
        so a call site that hardcoded the default, or dropped the reach argument
        altogether, fails here. At reach 190, the default would still read as
        establishable.

        The show is bound in Plex, and every season with it, so the watch history's depth
        is the only thing that moves between the two runs. Left unbound, the guard would
        have no rating key to read a place in the show from and would block for that
        reason instead, a true sentence about a fixture that never meant to say it, which
        would let a broken reach pass here undetected.
        """
        series = [
            {
                "id": 11,
                "title": "Five Seasons",
                "year": 2005,
                "status": "ended",
                "ended": True,
                "imdbId": "tt0011",
                "seasons": [_season_payload(n) for n in range(1, 6)],
            }
        ]
        plex_items = {
            800: identity.PlexItem(
                rating_key=800,
                title="Five Seasons",
                year=2005,
                added_at=None,
                ids=identity.ExternalIds.of(imdb="tt0011"),
            )
        }
        # Inside the shallower of the two reaches, so no watcher count is a lower bound in
        # either run and the keep-conflict detector stays out of a test about the hold.
        arrived = str(int((utcnow() - timedelta(days=100)).timestamp()))
        sweep = {
            800: [
                {"media_index": n, "rating_key": 800 + n, "added_at": arrived} for n in range(1, 6)
            ]
        }

        async def _run(reach_days: int) -> list[season_scan.SeasonJudgment]:
            _reasons, degrade = _degrade_sink()
            return await season_scan.gather(
                cache_engine,
                sonarrs=[_source(FakeSonarr(series_rows=series))],
                tautulli=show_library(
                    rows=[
                        {
                            "rating_key": 800,
                            "title": "Five Seasons",
                            "year": 2005,
                            "added_at": arrived,
                        }
                    ],
                    children={},
                ),
                plex=_FakePlexGuids(plex_items, seasons=_season_rows(sweep)),  # type: ignore[arg-type]
                horizon=utcnow() - timedelta(days=reach_days),
                reach_days=reach_days,
                active_rating_keys=set(),
                activity_degraded=False,
                season_policy=_season_policy(
                    keep_last_seasons=2, keep_first_season=True, in_progress_hold_days=200
                ),
                window_days=365,
                whitelisted=set(),
                degrade=degrade,
                watch_marks={},
                seen_marks={},
                seen_scans=[],
                seen_absence_days=7,
            )

        deep = await _run(400)
        assert [j.media_key for j in deep if j.guard_result.outcome is not PROTECT] == [
            "sonarr:1:11:2",
            "sonarr:1:11:3",
        ]

        shallow = await _run(190)
        assert all(j.guard_result.outcome is PROTECT for j in shallow)
        assert {
            reason_text(j.guard_result.detail)
            for j in shallow
            if j.media_key in {"sonarr:1:11:2", "sonarr:1:11:3"}
        } == {"Your watch history is too short to tell who's part-way through."}

        # ...and blocked, not merely protecting. A plain PROTECT on this gate does not
        # hold a hand reap (`verdict.STRUCTURAL_GATES` carries neither), while the
        # keep-rule conflict this blanket hold displaces does. Without the block, a season
        # a hand reap was refused on could become one it deletes.
        blocked = {j.media_key for j in shallow if j.guard_result.blocked}
        assert blocked == {"sonarr:1:11:2", "sonarr:1:11:3"}
        assert not any(j.guard_result.defers_to_owner for j in shallow)
        # Narrow on purpose. Seasons 1, 4, and 5 are held by protections that genuinely
        # fired (earliest season, keep-last), and a definite keep must stay definite.
        # Blocking every kept season here would be wrong, and would fail this assertion.
        assert all(j.guard_result.outcome is PROTECT for j in shallow)
        # Nothing in the deep arm is blocked. With the watch history spanning the hold,
        # there is no unanswered question to hold anything on.
        assert not any(j.guard_result.blocked for j in deep)

    async def test_a_season_plex_never_resolved_holds_the_one_its_viewer_is_up_to(
        self, cache_engine: AsyncEngine
    ) -> None:
        """This runs end to end against the real default policy.

        A viewer finished Season 3 yesterday, so the mid-binge guard should hold Season 4,
        the season they are about to watch. Season 3's plays are filed under its own Plex
        key, so the guard can only see them if that key was resolved. The two runs differ
        by one thing: a second "Season 3" item in the Plex sweep, which
        ``seasons_from_rows`` drops as ambiguous (a split or mis-scanned library produces
        these).

        Season 3 itself is safe either way. With no key, its own facts are Unknown and it
        abstains. Its siblings are the ones at risk: they resolved, they carry fully
        readable facts, and they would condemn at full confidence on a viewer nothing can
        see, unless the guard also holds them on the same unresolved key.
        """
        await _seed_ratings(cache_engine, {"tt0472": (5.5, 400)})
        # Five completed episodes of Season 3: she has finished it and is up to Season 4.
        for episode in range(1, 6):
            await _episode(cache_engine, season_key=903, user_id=7, episode=episode, days_ago=1)
        series = [
            {
                "id": 42,
                "title": "Long Show",
                "year": 2005,
                "status": "ended",
                "ended": True,
                "imdbId": "tt0472",
                "seasons": [_season_payload(n) for n in range(1, 6)],
            }
        ]
        episodes = {
            42: [
                {"seasonNumber": n, "episodeNumber": e, "hasFile": True}
                for n in range(1, 6)
                for e in range(1, 6)
            ]
        }
        plex_items = {
            900: identity.PlexItem(
                rating_key=900,
                title="Long Show",
                year=2005,
                added_at=None,
                ids=identity.ExternalIds.of(imdb="tt0472"),
            )
        }
        # Arrived 2000 days ago, past the 1095-day dormancy floor, so an unwatched season
        # here really is condemnable, and inside the 4000-day reach, so no watcher count is
        # a lower bound and the keep-rule conflict detector stays out of the way. Both
        # halves matter. Either one alone would leave these seasons abstaining for a reason
        # the test does not mean to check.
        arrived = str(int((utcnow() - timedelta(days=2000)).timestamp()))
        clean = {
            900: [
                {"media_index": n, "rating_key": 900 + n, "added_at": arrived} for n in range(1, 6)
            ]
        }
        # The same list, plus a second "Season 3", so season 3 alone loses its key.
        split = {900: [*clean[900], {"media_index": 3, "rating_key": 9903, "added_at": arrived}]}

        async def _run(sweep: dict[int, list[dict[str, Any]]]) -> dict[str, Any]:
            _reasons, degrade = _degrade_sink()
            judgments = await season_scan.gather(
                cache_engine,
                sonarrs=[_source(FakeSonarr(series_rows=series, episode_rows=episodes))],
                tautulli=show_library(
                    rows=[
                        {
                            "rating_key": 900,
                            "title": "Long Show",
                            "year": 2005,
                            "added_at": "1000000",
                        }
                    ],
                    children={},
                ),
                plex=_FakePlexGuids(plex_items, seasons=_season_rows(sweep)),  # type: ignore[arg-type]
                horizon=utcnow() - timedelta(days=4000),
                reach_days=4000,
                active_rating_keys=set(),
                activity_degraded=False,
                season_policy=_season_policy(keep_last_seasons=0, keep_first_season=False),
                window_days=365,
                whitelisted=set(),
                degrade=degrade,
                watch_marks={},
                seen_marks={},
                seen_scans=[],
                seen_absence_days=7,
            )
            assert not _reasons, f"the scan degraded, so no verdict here means anything: {_reasons}"
            return {j.media_key: j for j in judgments}

        control = await _run(clean)
        season_4 = control["sonarr:1:42:4"]
        assert season_4.guard_result.outcome is PROTECT
        assert reason_text(season_4.guard_result.detail) == "a viewer is part-way through the show"
        assert _judge(season_4.facts, season_4.guard_result) == "protect"
        # ...and the siblings really are condemnable on their own evidence, so "not
        # condemned" below is a statement about the guard, not about some unrelated
        # abstain the fixture happened to produce.
        assert _judge(control["sonarr:1:42:1"].facts, control["sonarr:1:42:1"].guard_result) == (
            "condemn"
        )

        broken = await _run(split)
        assert broken["sonarr:1:42:3"].plex_rating_key is None  # the ambiguous one
        for n in (1, 2, 4, 5):
            judgment = broken[f"sonarr:1:42:{n}"]
            assert _judge(judgment.facts, judgment.guard_result) != "condemn"
            assert reason_text(judgment.guard_result.detail) == (
                "A season of this show isn't matched in Plex, so who's part-way through is unknown."
            )
            # Blocked, not a plain keep. The guard could not be answered, so the panel
            # says "couldn't check" rather than green, and a hand reap still overrules.
            assert judgment.guard_result.blocked is True

    async def test_a_failed_season_read_stops_the_show_asserting_nobody_is_watching(
        self, cache_engine: AsyncEngine
    ) -> None:
        """``resolve_season_keys`` raising, for a show that did bind to Plex. Returning an
        empty map is fail-closed for that show's own seasons, since they all abstain on
        Unknown facts, but it says nothing about the assertion the show then makes about
        viewer progress, where the mid-binge guard must not report as checked and passed.
        """
        await _seed_ratings(cache_engine, {"tt0472": (5.5, 400)})
        series = [
            {
                "id": 42,
                "title": "Long Show",
                "year": 2005,
                "status": "ended",
                "ended": True,
                "imdbId": "tt0472",
                "seasons": [_season_payload(n) for n in range(1, 6)],
            }
        ]

        class _DeadChildren(FakeTautulli):
            async def children_metadata(self, rating_key: int) -> list[dict[str, Any]]:
                raise IntegrationError("tautulli", "connection refused")

        plex_items = {
            900: identity.PlexItem(
                rating_key=900,
                title="Long Show",
                year=2005,
                added_at=None,
                ids=identity.ExternalIds.of(imdb="tt0472"),
            )
        }
        _reasons, degrade = _degrade_sink()
        judgments = await season_scan.gather(
            cache_engine,
            sonarrs=[_source(FakeSonarr(series_rows=series))],
            tautulli=_DeadChildren(
                sections={
                    3: [
                        {
                            "rating_key": 900,
                            "title": "Long Show",
                            "year": 2005,
                            "added_at": "1000000",
                        }
                    ]
                },
                section_types={3: "show"},
            ),
            # The show binds to Plex. Only its season list is unreadable, so the sweep is
            # empty and every season falls to the per-show read that raises.
            plex=_FakePlexGuids(plex_items, seasons={}),  # type: ignore[arg-type]
            horizon=utcnow() - timedelta(days=4000),
            reach_days=4000,
            active_rating_keys=set(),
            activity_degraded=False,
            season_policy=_season_policy(keep_last_seasons=0, keep_first_season=False),
            window_days=365,
            whitelisted=set(),
            degrade=degrade,
            watch_marks={},
            seen_marks={},
            seen_scans=[],
            seen_absence_days=7,
        )

        by_key = {j.media_key: j for j in judgments}
        assert len(by_key) == 5
        for judgment in by_key.values():
            assert judgment.plex_rating_key is None
            assert reason_text(judgment.guard_result.detail) == (
                "A season of this show isn't matched in Plex, so who's part-way through is unknown."
            )
            assert judgment.guard_result.blocked is True

    async def test_a_show_plex_never_matched_at_all_is_left_alone(
        self, cache_engine: AsyncEngine
    ) -> None:
        """This is the deliberate boundary on the mid-binge hold, pinned because it is the
        boundary a later author would most reasonably widen.

        The hold fires only where the show bound to Plex and some of its seasons did not,
        because that is the mix where a readable sibling exists to condemn on the hidden
        viewer. Where nothing about the show resolved, every season already takes Unknown
        from its own branch and abstains, so widening the hold to cover it would move a
        whole population of unmatched shows out of the review queue and protect nothing
        further.

        So nothing moves, and the guard says what it did instead of what it found. The
        check never ran, in the same words the season's four Plex-dependent gates use, so
        the panel prints the cause once for all five rather than reporting a pass beside
        them.
        """
        series = [
            {
                "id": 77,
                "title": "Never Matched",
                "status": "ended",
                "ended": True,
                "seasons": [_season_payload(n) for n in range(1, 6)],
            }
        ]
        _reasons, degrade = _degrade_sink()
        judgments = await season_scan.gather(
            cache_engine,
            sonarrs=[_source(FakeSonarr(series_rows=series))],
            tautulli=show_library([]),
            horizon=utcnow() - timedelta(days=4000),
            reach_days=4000,
            active_rating_keys=set(),
            activity_degraded=False,
            season_policy=_season_policy(keep_last_seasons=2, keep_first_season=True),
            window_days=365,
            whitelisted=set(),
            degrade=degrade,
            watch_marks={},
            seen_marks={},
            seen_scans=[],
            seen_absence_days=7,
        )
        by_key = {j.media_key: j for j in judgments}
        assert by_key["sonarr:1:77:2"].plex_rating_key is None
        guard = by_key["sonarr:1:77:2"].guard_result
        # ABSTAIN, never PROTECT. The season stays in the review queue's abstain lane,
        # which is the whole reason the hold was scoped away from here.
        assert guard.outcome is ABSTAIN
        # Blocked and unestablishable, so the panel renders it amber under "left for you
        # to decide" rather than green under "protections it cleared", and the panel's
        # conflict branch skips it. Nothing was compared, so nothing is being handed over.
        assert guard.blocked
        assert guard.unestablishable
        assert not guard.defers_to_owner
        # The cause is the one the season's own Unknown facts carry, character for
        # character, which is what makes `WhyPanel.LeftForYou` group all five under one
        # heading instead of opening a second box saying the same thing. `guard_result`
        # attaches the season `mediaType` to the bare id it froze. The fact builder
        # attaches the same param directly, off the one shared table, `gates.no_key_reason`.
        cause = season_evidence.no_key_reason(identity.MatchStatus.UNMATCHED)
        assert reason_flat(guard.detail) == (
            f"blocked[check=check.season_progress cause=cause.{cause}[mediaType=season]]"
        )
        unwatched = by_key["sonarr:1:77:2"].facts.days_observed_unwatched
        assert isinstance(unwatched, Unknown)
        assert unwatched.reason == Reason(f"cause.{cause}", {"mediaType": "season"})

    async def test_a_show_without_files_logs_no_content(self, cache_engine: AsyncEngine) -> None:
        """A show Sonarr has no downloaded episodes for is dropped as no_content, and its
        decision line says so with the zero file counts, so it is not mistaken for a bug."""
        series = [
            {
                "id": 5,
                "title": "Nothing Downloaded",
                "status": "continuing",
                "seasons": [_season_payload(1, files=0), _season_payload(2, files=0)],
            }
        ]
        _reasons, degrade = _degrade_sink()
        with capture_logs() as logs:
            await season_scan.gather(
                cache_engine,
                sonarrs=[_source(FakeSonarr(series_rows=series))],
                tautulli=show_library([]),
                horizon=utcnow(),
                reach_days=0,
                active_rating_keys=set(),
                activity_degraded=False,
                season_policy=_season_policy(keep_last_seasons=2, keep_first_season=True),
                window_days=365,
                whitelisted=set(),
                degrade=degrade,
                watch_marks={},
                seen_marks={},
                seen_scans=[],
                seen_absence_days=7,
            )
        decisions = [e for e in logs if e["event"] == "season_scan.series_decision"]
        assert len(decisions) == 1
        d = decisions[0]
        assert d["outcome"] == "no_content"
        assert d["prunable"] == []
        assert d["protected"] == []
        assert [s["files"] for s in d["seasons"]] == [0, 0]

    async def test_an_unreachable_sonarr_degrades_the_snapshot(
        self, cache_engine: AsyncEngine
    ) -> None:
        class _DeadSonarr:
            async def series(self) -> list[dict[str, Any]]:
                raise IntegrationError("sonarr", "connection refused")

            async def root_folders(self) -> list[dict[str, Any]]:
                raise IntegrationError("sonarr", "connection refused")

        reasons, degrade = _degrade_sink()
        judgments = await season_scan.gather(
            cache_engine,
            sonarrs=[_source(_DeadSonarr())],
            tautulli=show_library([]),
            horizon=utcnow(),
            reach_days=0,
            active_rating_keys=set(),
            activity_degraded=False,
            season_policy=_season_policy(keep_last_seasons=2, keep_first_season=True),
            window_days=365,
            whitelisted=set(),
            degrade=degrade,
            watch_marks={},
            seen_marks={},
            seen_scans=[],
            seen_absence_days=7,
        )
        assert judgments == []
        assert any("sonarr" in r and "unreachable" in r for r in reasons)


class TestShowLevelRewatchFacts:
    """``services.rewatch.show_rewatch_stats`` is read once per scan, and
    ``_judge_series`` turns it into the show's ``rewatch_viewings`` and
    ``rewatch_last_play_days`` pair, stamped identically on every season of that show, the
    same shape ``show_ended`` already carries. The cohort pair (``rewatch_cohort_n`` and
    ``rewatch_cohort_k``) is the season lane's own Stage 2 fit, off the TV curve
    ``gather`` fits once per scan (``TestTheTVCohortFit`` below). A show whose current
    dormancy lands nowhere in that curve reads ``Unknown`` with the shared reason, never
    ``Absent``. The season lane always has an opinion about its own cohort, even when that
    opinion is "cannot say", the same discipline ``rewatch_viewings`` and
    ``rewatch_last_play_days`` already follow.
    """

    async def test_a_show_with_replayed_episodes_is_known_on_every_season(
        self, cache_engine: AsyncEngine
    ) -> None:
        series = [
            {
                "id": 1,
                "title": "Replayed Show",
                "year": 2011,
                "status": "ended",
                "ended": True,
                "seasons": [_season_payload(1), _season_payload(2)],
            }
        ]
        tautulli = show_library(
            rows=[
                {"rating_key": 900, "title": "Replayed Show", "year": 2011, "added_at": "1000000"}
            ],
            children={
                900: [{"media_index": 1, "rating_key": 901}, {"media_index": 2, "rating_key": 902}]
            },
        )
        # Two episodes, each played twice ~150 days apart. An older viewing and a later
        # replay of the same two episode keys (rewatch.replay_period_count's >= 1/4-overlap
        # floor), never a release-following binge.
        for days_ago, episode in ((200, 1), (195, 2), (50, 1), (45, 2)):
            await _episode(
                cache_engine,
                season_key=901,
                user_id=1,
                show_key=900,
                days_ago=days_ago,
                episode=episode,
            )
        _reasons, degrade = _degrade_sink()

        judgments = await season_scan.gather(
            cache_engine,
            sonarrs=[_source(FakeSonarr(series_rows=series))],
            tautulli=tautulli,
            horizon=utcnow() - timedelta(days=4000),
            reach_days=4000,
            active_rating_keys=set(),
            activity_degraded=False,
            season_policy=_season_policy(keep_last_seasons=2, keep_first_season=True),
            window_days=365,
            whitelisted=set(),
            degrade=degrade,
            watch_marks={},
            seen_marks={},
            seen_scans=[],
            seen_absence_days=7,
        )

        by_key = {j.media_key: j for j in judgments}
        s1, s2 = by_key["sonarr:1:1:1"].facts, by_key["sonarr:1:1:2"].facts
        # 1 replay period, never a value the "no entry" fallback (0) would also produce.
        # Last play is the more recent pair, 45 days back.
        assert s1.rewatch_viewings == Known(value=1, source="tautulli")
        assert s1.rewatch_last_play_days == Known(value=45, source="tautulli")
        # Show-level. Computed once and stamped as the same object on the show's other
        # season.
        assert s2.rewatch_viewings is s1.rewatch_viewings
        assert s2.rewatch_last_play_days is s1.rewatch_last_play_days
        # This show is the only one in the whole scan and its season carries no added_at
        # (``children`` above has none), so it contributes no training pair. The fit is
        # empty and no dormancy lands anywhere in it (``TestTheTVCohortFit`` below covers a
        # populated curve).
        assert s1.rewatch_cohort_n == Unknown(
            reason=rewatch.NO_REWATCH_ESTIMATE_REASON, source="tautulli"
        )
        assert s1.rewatch_cohort_k == Unknown(
            reason=rewatch.NO_REWATCH_ESTIMATE_REASON, source="tautulli"
        )

    async def test_a_resolved_show_with_no_qualified_plays_is_known_zero_and_absent(
        self, cache_engine: AsyncEngine
    ) -> None:
        """The show resolved and the watch history was read, but it holds nothing for
        this show. That is a checked zero, not the failed read Unknown would claim. It
        also has no anchor at all for the Stage 2 cohort, no play, and, like the sibling
        test above, no season added_at, so the cohort reads Unknown rather than Known,
        whatever the fit found elsewhere.
        """
        series = [
            {
                "id": 2,
                "title": "Unwatched Show",
                "year": 2012,
                "status": "ended",
                "ended": True,
                "seasons": [_season_payload(1)],
            }
        ]
        tautulli = show_library(
            rows=[
                {"rating_key": 800, "title": "Unwatched Show", "year": 2012, "added_at": "1000000"}
            ],
            children={800: [{"media_index": 1, "rating_key": 801}]},
        )
        _reasons, degrade = _degrade_sink()

        judgments = await season_scan.gather(
            cache_engine,
            sonarrs=[_source(FakeSonarr(series_rows=series))],
            tautulli=tautulli,
            horizon=utcnow() - timedelta(days=4000),
            reach_days=4000,
            active_rating_keys=set(),
            activity_degraded=False,
            season_policy=_season_policy(keep_last_seasons=2, keep_first_season=True),
            window_days=365,
            whitelisted=set(),
            degrade=degrade,
            watch_marks={},
            seen_marks={},
            seen_scans=[],
            seen_absence_days=7,
        )

        judgment = next(j for j in judgments if j.media_key == "sonarr:1:2:1")
        facts = judgment.facts
        assert facts.rewatch_viewings == Known(value=0, source="tautulli")
        assert isinstance(facts.rewatch_last_play_days, Absent)
        assert facts.rewatch_cohort_n == Unknown(
            reason=rewatch.NO_REWATCH_ESTIMATE_REASON, source="tautulli"
        )
        assert facts.rewatch_cohort_k == Unknown(
            reason=rewatch.NO_REWATCH_ESTIMATE_REASON, source="tautulli"
        )
        assert judgment.rewatch_block is None

    async def test_an_unresolved_show_is_unknown_on_both_rewatch_observations(
        self, cache_engine: AsyncEngine
    ) -> None:
        """No Plex show key means no key to look this show's plays up under. That is a
        failed look, never the checked absence the resolved cases above record.
        """
        series = [
            {
                "id": 3,
                "title": "Missing Show",
                "status": "ended",
                "ended": True,
                "seasons": [_season_payload(1)],
            }
        ]
        _reasons, degrade = _degrade_sink()

        judgments = await season_scan.gather(
            cache_engine,
            sonarrs=[_source(FakeSonarr(series_rows=series))],
            tautulli=show_library([]),
            horizon=utcnow() - timedelta(days=4000),
            reach_days=4000,
            active_rating_keys=set(),
            activity_degraded=False,
            season_policy=_season_policy(keep_last_seasons=2, keep_first_season=True),
            window_days=365,
            whitelisted=set(),
            degrade=degrade,
            watch_marks={},
            seen_marks={},
            seen_scans=[],
            seen_absence_days=7,
        )

        facts = next(j for j in judgments if j.media_key == "sonarr:1:3:1").facts
        assert isinstance(facts.rewatch_viewings, Unknown)
        assert isinstance(facts.rewatch_last_play_days, Unknown)
        assert facts.rewatch_viewings.reason == Reason(
            "cause.plex_unmatched", {"mediaType": "season"}
        )
        assert facts.rewatch_last_play_days.reason == Reason(
            "cause.plex_unmatched", {"mediaType": "season"}
        )
        # The cohort's Unknown carries the one shared reason (rewatch.NO_REWATCH_ESTIMATE_
        # REASON), not the match-status reason above. Every "nothing to show" cause reads
        # the same to the operator (services.snapshot.build_facts's own comment).
        assert facts.rewatch_cohort_n == Unknown(
            reason=rewatch.NO_REWATCH_ESTIMATE_REASON, source="tautulli"
        )
        assert facts.rewatch_cohort_k == Unknown(
            reason=rewatch.NO_REWATCH_ESTIMATE_REASON, source="tautulli"
        )


class TestTheTVCohortFit:
    """Stage 2 for TV: the TV curve ``gather`` fits off
    ``services.rewatch.show_rewatch_outcomes``, matching the movie lane's own fit in
    ``snapshot.scan``, and the per-show cohort lookup ``_judge_series`` stamps off it.
    """

    async def test_a_show_in_a_fitted_block_stamps_known_cohort_on_every_season(
        self, cache_engine: AsyncEngine
    ) -> None:
        """Two shows train the fit's (0, 365] block. Show A was watched again inside the
        year, and Show B was not, so the pooled block is a distinguishable, non-default
        n=2/k=1, never the "no entry" zero a bug swallowing the fit could also produce.
        Show A's own current dormancy (its most recent play) falls in that same block, so
        every one of its seasons is stamped ``Known`` off it, sharing the identical object
        (``is``) the way ``rewatch_viewings`` already does.
        """
        series = [
            {
                "id": 10,
                "title": "Fitted Show A",
                "year": 2011,
                "status": "ended",
                "ended": True,
                "seasons": [_season_payload(1), _season_payload(2)],
            },
            {
                "id": 11,
                "title": "Fitted Show B",
                "year": 2012,
                "status": "ended",
                "ended": True,
                "seasons": [_season_payload(1)],
            },
        ]
        tautulli = show_library(
            rows=[
                {"rating_key": 910, "title": "Fitted Show A", "year": 2011},
                {"rating_key": 920, "title": "Fitted Show B", "year": 2012},
            ],
            children={
                910: [{"media_index": 1, "rating_key": 911}, {"media_index": 2, "rating_key": 912}],
                920: [{"media_index": 1, "rating_key": 921}],
            },
        )
        # Show A: an old play (well before the year-back cutoff) trains the fit, and a
        # recent one both marks the training pair "watched again" and anchors its own
        # current lookup at ~5 days dormant, inside the (0, 365] block the training pairs
        # populate.
        await _episode(cache_engine, season_key=911, user_id=1, show_key=910, days_ago=400)
        await _episode(cache_engine, season_key=911, user_id=1, show_key=910, days_ago=5, episode=2)
        # Show B: the same old-play training anchor, never watched again. It is the
        # block's other member.
        await _episode(cache_engine, season_key=921, user_id=2, show_key=920, days_ago=400)
        _reasons, degrade = _degrade_sink()

        judgments = await season_scan.gather(
            cache_engine,
            sonarrs=[_source(FakeSonarr(series_rows=series))],
            tautulli=tautulli,
            horizon=utcnow() - timedelta(days=4000),
            reach_days=4000,
            active_rating_keys=set(),
            activity_degraded=False,
            season_policy=_season_policy(keep_last_seasons=2, keep_first_season=True),
            window_days=365,
            whitelisted=set(),
            degrade=degrade,
            watch_marks={},
            seen_marks={},
            seen_scans=[],
            seen_absence_days=7,
        )

        by_key = {j.media_key: j for j in judgments}
        s1 = by_key["sonarr:1:10:1"]
        s2 = by_key["sonarr:1:10:2"]
        assert s1.facts.rewatch_cohort_n == Known(value=2, source="tautulli")
        assert s1.facts.rewatch_cohort_k == Known(value=1, source="tautulli")
        # Every season of the show shares the identical block object.
        assert s2.facts.rewatch_cohort_n is s1.facts.rewatch_cohort_n
        assert s2.facts.rewatch_cohort_k is s1.facts.rewatch_cohort_k
        assert s1.rewatch_block is not None
        assert s1.rewatch_block is s2.rewatch_block
        assert (s1.rewatch_block.n, s1.rewatch_block.k) == (2, 1)

        # Pipeline-level. The season lane's Known cohort really reaches the stored
        # explanation through the shared judge_facts/_explain path snapshot.scan uses for
        # every season row, not just the in-memory Facts object.
        gates = build_gates(DEFAULT_TV_POLICY)
        signals = [
            SignalConfig(signal=s.signal, weight=s.weight, saturate_at=s.saturate_at, floor=s.floor)
            for s in DEFAULT_TV_POLICY.signals
        ]
        judged = judge_facts(
            s1.facts,
            gates,
            DEFAULT_TV_POLICY,
            signals=signals,
            custom_condemn=DEFAULT_TV_POLICY.custom_signal_configs(),
            keeps=DEFAULT_TV_POLICY.keep_configs(),
            window_days=DEFAULT_TV_POLICY.popularity_window_days(),
            extra_results=(s1.guard_result,),
            rewatch_block=s1.rewatch_block,
        )
        stored = json.loads(judged.explanation)
        assert stored["rewatch_odds"] == {
            "n": 2,
            "k": 1,
            "lo_days": 0.0,
            "hi_days": 365.0,
            "state": "thin",
            "bound_pct": 91,
        }

    async def test_the_cohort_lookup_uses_the_any_play_anchor_not_the_qualified_one(
        self, cache_engine: AsyncEngine
    ) -> None:
        """The discriminating case. This show's only recent play is unqualified (low
        percent complete), so the stage 1 keep's qualified ``rewatch_last_play_days``
        reads its older qualified play (~400 days back). The Stage 2 cohort lookup must
        instead anchor on the any-play last play (~5 days back, unqualified), exactly as
        ``services.rewatch.show_rewatch_outcomes`` promises, or it reads the wrong
        dormancy entirely.

        The fit is trained off this show's own old-play-to-recent-play pair, which lands
        in the (0, 365] block. If the lookup mistakenly anchored on the qualified ~400-day
        play instead, that dormancy would fall in the different (365, 548] block, which
        the fit never populated, and the cohort would read Unknown instead of Known. That
        divergence is what makes this test discriminate the two anchors, rather than
        merely re-asserting the cohort code path the test above already covers.
        """
        series = [
            {
                "id": 20,
                "title": "Unqualified Recent Play Show",
                "year": 2013,
                "status": "ended",
                "ended": True,
                "seasons": [_season_payload(1)],
            }
        ]
        tautulli = show_library(
            rows=[{"rating_key": 930, "title": "Unqualified Recent Play Show", "year": 2013}],
            children={930: [{"media_index": 1, "rating_key": 931}]},
        )
        # The older qualified play. This is what rewatch_last_play_days (stage 1) must
        # read, and what trains the fit's (0, 365] block (~35 days before the year-back
        # cutoff).
        await _episode(cache_engine, season_key=931, user_id=1, show_key=930, days_ago=400)
        # The newer play has low percent_complete and no watched_status, so
        # `rewatch.qualifies` rejects it. Stage 2's any-play anchor counts it regardless
        # (module docstring: "any user, any completion").
        await _episode(
            cache_engine,
            season_key=931,
            user_id=1,
            show_key=930,
            days_ago=5,
            episode=2,
            status=None,
            percent_complete=10,
        )
        _reasons, degrade = _degrade_sink()

        judgments = await season_scan.gather(
            cache_engine,
            sonarrs=[_source(FakeSonarr(series_rows=series))],
            tautulli=tautulli,
            horizon=utcnow() - timedelta(days=4000),
            reach_days=4000,
            active_rating_keys=set(),
            activity_degraded=False,
            season_policy=_season_policy(keep_last_seasons=2, keep_first_season=True),
            window_days=365,
            whitelisted=set(),
            degrade=degrade,
            watch_marks={},
            seen_marks={},
            seen_scans=[],
            seen_absence_days=7,
        )

        facts = next(j for j in judgments if j.media_key == "sonarr:1:20:1").facts
        # Stage 1 stays on the older qualified play.
        assert facts.rewatch_last_play_days == Known(value=400, source="tautulli")
        # Stage 2's cohort is Known. This is only possible if the lookup anchored on the
        # recent any-play (~5 days, inside the trained block), not the qualified ~400-day
        # play (outside it, where the cohort would read Unknown).
        assert facts.rewatch_cohort_n == Known(value=1, source="tautulli")
        assert facts.rewatch_cohort_k == Known(value=1, source="tautulli")


class TestUserSeasonProgress:
    async def test_progress_uses_the_max_completed_episode(self, cache_engine: AsyncEngine) -> None:
        await _episode(cache_engine, season_key=706, user_id=1, episode=3)
        await _episode(cache_engine, season_key=706, user_id=1, episode=7)
        stats = await season_scan.season_watch_stats(cache_engine, {706}, window_days=365)
        assert stats.user_season_progress[1][706] == 7

    async def test_a_null_episode_index_is_not_a_progress_row(
        self, cache_engine: AsyncEngine
    ) -> None:
        # A movie-style row (no media_index) still counts as a touch, but not as a position.
        await _episode(cache_engine, season_key=707, user_id=1)  # episode=None
        stats = await season_scan.season_watch_stats(cache_engine, {707}, window_days=365)
        assert 707 in stats.user_season_keys[1]
        assert 707 not in stats.user_season_progress.get(1, {})

    @pytest.mark.parametrize(
        ("plays", "why"),
        [
            # Nothing completed at all. `max_ep is None`, so there is no position to name.
            ([(2, None)], "no completed episode"),
            # Completed ep 1, then plays of ep 4 Tautulli never reported the completion
            # of. They may be further on than the position says, so it is dropped rather
            # than trusted low. Being wrong in that direction unprotects the season they
            # are about to watch next.
            ([(1, 1.0), (4, None)], "a later play whose completion is unknown"),
            # The same reach, reported rather than missing. Tautulli quantizes
            # `watched_status` against the operator's own threshold, so a play that
            # stopped short arrives as 0.75 and not as NULL. It is still a play of
            # episode 4.
            ([(1, 1.0), (4, 0.75)], "a later play that stopped short of complete"),
        ],
    )
    async def test_a_dropped_position_holds_the_season_rather_than_clearing_it(
        self, cache_engine: AsyncEngine, plays: list[tuple[int, float | None]], why: str
    ) -> None:
        """``season_watch_stats`` drops a progress row down two branches, and both are
        locally keep-safe by intent. What makes them keep-safe *downstream* is an
        invariant that lives in a different query, with no test of its own. The
        ``pairs`` read that fills ``user_season_keys`` carries no ``media_index`` filter,
        so it is a strict superset of the ``progress`` read. A viewer whose position was
        dropped is therefore still present as a *touch*. ``_progress_by_user`` records
        them as ``None``, meaning position unknown, not absent, and ``_anchor_positions``
        fails closed on that and holds the season plus the one after it.

        Narrowing ``pairs`` to match ``progress``'s filters would make the viewer vanish
        instead, and the mid-binge guard would then read a dropped position as "nobody is
        part-way through". That change looks like a tidy-up and is a protection loss,
        which is why the chain is asserted end to end here rather than at the query.
        """
        for episode, status in plays:
            await _episode(cache_engine, season_key=903, user_id=7, episode=episode, status=status)
        stats = await season_scan.season_watch_stats(cache_engine, {901, 902, 903}, window_days=365)
        assert stats.user_season_progress.get(7, {}).get(903) is None, f"{why} left a position"

        key_to_number = {901: 1, 902: 2, 903: 3}
        progress = season_scan._progress_by_user(stats, key_to_number)
        # Present, and Unknown. This is the distinction the whole chain turns on.
        assert progress == {"7": {3: None}}

        plan = plan_series_prune(
            series_title="Show",
            seasons=[_season(n) for n in (1, 2, 3, 4)],
            keep_last=0,
            keep_first_season=False,
            progress_by_user=progress,
            last_play_by_user=season_scan._last_play_by_user_season(stats, key_to_number),
            season_final_episode={1: 5, 2: 5, 3: 5, 4: 5},
        )
        # Season 3 because she may still be on it, season 4 because she may have finished
        # it. With the position unknown, `_anchor_positions` cannot tell and holds both.
        assert plan.prunable == [1, 2]
        held = {p.season_number: reason_text(p.reason) for p in plan.protected}
        assert held[3] == "a viewer is part-way through the show"
        assert held[4] == "a viewer is part-way through the show"

    @pytest.mark.parametrize("status", [0.25, 0.5, 0.75])
    async def test_a_finale_that_stopped_short_still_holds_the_next_season(
        self, cache_engine: AsyncEngine, status: float
    ) -> None:
        """A viewer completed every episode but the last, and reached the last one
        without finishing it. They are done with the season, and the next one is what
        they start next.

        Read as "still on season 3", the guard would hold the season they have just
        finished and release the one they are about to start. That points the protection
        at the wrong season, not merely missing it, and the released season carries the
        old plays that let it score. `watched_status` is quantized against the operator's
        own watched-percent threshold, so it arrives as one of 0, 0.25, 0.5, 0.75, or 1,
        and matching unfinished plays on `IS NULL` alone would miss the three middle
        values entirely. This sweeps all three rather than pinning just 0.75, so a fix
        that only covers one value fails here.
        """
        for episode in range(1, 10):
            await _episode(cache_engine, season_key=913, user_id=7, episode=episode)
        await _episode(cache_engine, season_key=913, user_id=7, episode=10, status=status)

        stats = await season_scan.season_watch_stats(cache_engine, {913, 914}, window_days=365)
        assert stats.user_season_progress.get(7, {}).get(913) is None, "position read 9, not 10"

        key_to_number = {913: 3, 914: 4}
        plan = plan_series_prune(
            series_title="Show",
            seasons=[_season(n, files=10, total=10) for n in (3, 4)],
            keep_last=0,
            keep_first_season=False,
            progress_by_user=season_scan._progress_by_user(stats, key_to_number),
            last_play_by_user=season_scan._last_play_by_user_season(stats, key_to_number),
            season_final_episode={3: 10, 4: 10},
        )
        assert plan.prunable == []
        assert {p.season_number for p in plan.protected} == {3, 4}


class TestFinalEpisodes:
    def test_uses_the_highest_on_disk_episode_ignoring_gaps_and_missing_files(self) -> None:
        episodes = [
            {"seasonNumber": 1, "episodeNumber": 1, "hasFile": True},
            {"seasonNumber": 1, "episodeNumber": 3, "hasFile": True},  # gap at 2, still on disk
            {"seasonNumber": 1, "episodeNumber": 4, "hasFile": False},  # not on disk -> ignored
            {"seasonNumber": 2, "episodeNumber": 1, "hasFile": True},
        ]
        assert season_scan._final_episodes(episodes) == {1: 3, 2: 1}


class TestKeepLastApplies:
    def _index(self, *, shows: AbstractSet[str] = frozenset()) -> requested_by.RequestIndex:
        return requested_by.RequestIndex(
            available=True,
            movie_keys=frozenset(),
            show_keys=frozenset(shows),
            season_keys=frozenset(),
        )

    def test_all_scope_always_applies(self) -> None:
        assert season_scan._keep_last_applies({"tvdbId": 1}, "all", None) is True

    def test_requested_scope_applies_when_the_show_was_requested(self) -> None:
        index = self._index(shows={"tv:tvdb:1"})
        assert season_scan._keep_last_applies({"tvdbId": 1}, "requested", index) is True

    def test_requested_scope_skips_a_show_that_was_not_requested(self) -> None:
        assert season_scan._keep_last_applies({"tvdbId": 1}, "requested", self._index()) is False

    def test_requested_scope_fails_closed_when_it_cannot_tell(self) -> None:
        # No index -> Unknown -> keep-last still applies (Unknown counts as "might be requested").
        assert season_scan._keep_last_applies({"tvdbId": 1}, "requested", None) is True
