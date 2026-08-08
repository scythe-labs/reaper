# SPDX-License-Identifier: AGPL-3.0-or-later
"""The TV/season scan path.

Season pruning deletes whole seasons, so this half of the scan is held to the same
standard as the movie half and one extra: because a season must be resolved to its own
Plex rating key before its watch history can be read, every test here is really asking
whether an *uncertain* resolution can lead to a deletion. The answer, everywhere, must be
no -- a season we cannot see is judged, at worst, ABSTAIN, never CONDEMN.

The load-bearing test is ``TestNothingUnseenIsCondemned``: it runs an unresolved season
through the real default policy and asserts the verdict cannot be "condemn".
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from collections.abc import Set as AbstractSet
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
from reaper.engine.policy import DEFAULT_MOVIE_POLICY
from reaper.engine.signals import Score, SignalConfig
from reaper.engine.signals import score as score_signals
from reaper.ratings import Rating, RatingSource
from reaper.services import history_sync, lists, requested_by, season_scan
from reaper.services.condemned import reap_override_verdict_decoded
from reaper.services.scan_runner import build_gates
from reaper.services.season_pruning import plan_series_prune
from reaper.services.snapshot import _explain, _verdict
from tests._fakes import FakeSonarr, FakeTautulli, show_library

GB = 1024**3


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
    """B-10: the season-precise tvdb key outranks the show-level Plex rating key, so two people
    who asked for different seasons attribute to their own season, not a blurred "A + 1 other"."""

    def test_different_seasons_attribute_to_their_own_requester(self) -> None:
        tvdb = 81189
        show_rk = 500
        # build_map files: each requested season under its season key, and BOTH requesters under
        # the show-level rating key (Seerr stores a TV request's ratingKey at the show level).
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
        # A whole-show request has no season key, so the rating-key tier (copy precision) still
        # wins over the loose tvdb union.
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
        zeros -- zeros would read as an empty season and quietly drop it from protection."""
        series = {"seasons": [_season_payload(1), {"seasonNumber": 2}]}
        seasons = season_scan.parse_seasons(series)
        assert [s.season_number for s in seasons] == [1]


# ---------------------------------------------------------------------------
# Airing detection -- conservative on purpose
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
# The show join -- the one place a wrong answer could delete the wrong thing
# ---------------------------------------------------------------------------


class TestTheShowJoin:
    """The Sonarr series -> Plex show join now runs through the one shared resolver
    (``identity.resolve_show``). These cases carry no external id, so they exercise the
    title+year backstop -- the exact behavior the old ``match_show`` guaranteed, preserved
    now that there is a single implementation (see ``test_identity.py`` for the id tiers)."""

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
        index = self._index((1, "The Office", 2001), (2, "The Office", 2005))  # UK, US
        assert self._match(index, "The Office", 2005) == 2

    def test_a_duplicate_title_with_no_year_refuses_to_guess(self) -> None:
        """The wrong show join reads the wrong show's watch history and could condemn a
        season people are watching. With nothing to disambiguate on, refuse -> the season
        goes Unknown and abstains, rather than being matched to a coin-flip."""
        index = self._index((1, "The Office", 2001), (2, "The Office", 2005))
        assert self._match(index, "The Office", None) is None

    def test_a_lone_title_match_with_a_conflicting_year_is_refused(self) -> None:
        """The US series is scanned but the ONLY Plex show with that title is the UK one
        (the US show is indexed under a different title). A single title hit is not a safe
        join when the known years disagree -- binding would read the UK show's history."""
        index = self._index((1, "The Office", 2001))
        assert self._match(index, "The Office", 2005) is None

    def test_a_lone_title_match_with_an_agreeing_year_binds(self) -> None:
        assert self._match(self._index((1, "The Office", 2005)), "The Office", 2005) == 1

    def test_a_lone_title_match_binds_when_a_year_is_missing(self) -> None:
        """Plex often has no year; a title-only join stays as safe as the movie path's."""
        assert self._match(self._index((1, "The Office", None)), "The Office", 2005) == 1


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
        """Two 'Season 2' items (a split/mis-scanned library) is ambiguous. Binding to one
        risks reading an empty duplicate's history for a watched season, so the season is
        dropped entirely -> no Plex key -> Unknown facts -> abstain."""
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

    Through the path the queue's Reap button actually takes: the guard result is FROZEN by
    the real writer (``snapshot._explain``) and re-read by
    ``condemned.reap_override_verdict_decoded``, which is what every read-side consumer
    calls (``snapshot.effective_fate`` routes a hand reap through it rather than deciding
    live). Scored 0 against a threshold of 100, so nothing but the override can condemn.

    **This answer is CONSTANT for this gate, and that is the point rather than a gap
    (rule 118).** ``season_progression`` is in neither ``STRUCTURAL_GATES`` nor any
    remaining hold, so every shape the guard can emit -- all 16 combinations of outcome x
    blocked x defers_to_owner x detail -- condemns under a hand reap. Measured. So the
    callers below do NOT lean on this line to tell their shapes apart; each asserts the
    typed ``defers_to_owner`` and the detail, which do vary. What this line pins is the
    guarantee itself: **no season-guard shape may ever refuse the operator's own hand
    again**, which is exactly what the reversal bought and exactly what a future change
    re-adding a hold would break.

    It goes through the freeze rather than ``snapshot._verdict(..., override="reap")`` for
    two reasons. That call is a dead path -- its only caller, ``judge_facts``, passes
    ``override=None`` unconditionally, and ``effective_fate`` routes every hand reap through
    ``condemned`` off the frozen explanation instead -- so asserting on it proved nothing
    about production. And going through ``_explain`` makes these assertions cover the
    writer: a field the writer stops emitting changes the answer here.
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
        result = season_scan.guard_result(plan, 1)
        assert result.gate is GateId.SEASON_PROGRESSION
        assert result.outcome == PROTECT

    def test_a_keep_rule_conflict_blocks_rather_than_condemns(self) -> None:
        """The old season is the good one: prunable by rank, but far more watched than a
        season the rule keeps. That is not a delete to make unattended -- it blocks, which
        forces the whole item to ABSTAIN and sends it to a human."""
        plan = plan_series_prune(
            series_title="S",
            seasons=[_season(n) for n in range(1, 5)],
            keep_last=2,
            keep_first_season=False,
            watchers_by_season={1: 40, 2: 1, 3: 1, 4: 1},
        )
        result = season_scan.guard_result(plan, 1)
        assert result.outcome == ABSTAIN
        assert result.blocked is True

    def test_a_cleanly_prunable_season_neither_protects_nor_blocks(self) -> None:
        plan = plan_series_prune(
            series_title="S",
            seasons=[_season(n) for n in range(1, 5)],
            keep_last=1,
            keep_first_season=False,
        )
        result = season_scan.guard_result(plan, 2)
        assert result.outcome == ABSTAIN
        assert result.blocked is False

    def test_a_conflict_that_made_the_comparison_defers_to_the_owner(self) -> None:
        """Season 1 was watched 40 times against Season 4's once: the comparison WAS made
        and the keep rule lost it. That is the deliberate "you decide" flag, and a hand reap
        is the decision it asked for.

        The flag no longer decides the reap -- no block does -- but it still picks the chip
        the operator reads (``api.routes._chip``), which is why it is still produced and
        still asserted here. It is the one shape whose chip names the comparison."""
        plan = plan_series_prune(
            series_title="S",
            seasons=[_season(n) for n in range(1, 5)],
            keep_last=2,
            keep_first_season=False,
            watchers_by_season={1: 40, 2: 1, 3: 1, 4: 1},
        )
        result = season_scan.guard_result(plan, 1)

        assert result.blocked is True
        assert result.defers_to_owner is True
        assert _hand_reap(result) == "condemn"

    def test_a_conflict_whose_comparison_failed_is_flagged_as_refused(self) -> None:
        """The other arm, and the one that shipped broken: Season 4 is kept by the rule but
        is on disk without ever being resolved in Plex, so nobody could read who watched it
        (``kept_watchers=None``). ``_detect_conflicts`` still raises the conflict rather than
        letting an unread number clear a protection, and the season still goes to a human.

        What this arm used to carry was the hold on the hand reap, and the arm meant to
        catch it tested ``detail.startswith("could not check")`` while the message opens with
        the watcher count -- so the Reap button released the season anyway. The hold is gone
        by decision, not by accident, so what is pinned now is the wording trap itself: the
        message does not carry the retired prefix, the typed flag says which shape this is,
        and neither one moves the verdict."""
        plan = plan_series_prune(
            series_title="S",
            seasons=[_season(n) for n in range(1, 5)],
            keep_last=1,
            keep_first_season=False,
            watchers_by_season={1: 40, 2: 1, 3: 1, 4: None},
        )
        result = season_scan.guard_result(plan, 1)

        assert result.blocked is True
        assert result.defers_to_owner is False
        assert not result.detail.startswith("could not check")
        assert _hand_reap(result) == "condemn"

    def test_a_readable_conflict_does_not_mask_a_refused_one_on_the_same_season(self) -> None:
        """One pruned season carries a conflict per kept season, so both shapes at once --
        and reading only the first let a readable comparison mask a refused one, releasing
        the hand reap that #84 exists to hold. Shipped defaults, and the benign cause
        ``_detect_conflicts`` calls the commonest: Season 5 is on disk but not yet resolved
        in Plex, so it is kept AND unreadable while Seasons 1 and 4 read fine.

        The flag answers for EVERY comparison behind the block, so one refusal decides it,
        and the message follows the flag: the operator is shown the season nobody could read
        rather than the one that happened to sort first. What the flag decides is now the
        chip rather than the reap, and the precedence matters more for that, not less -- a
        first-match read would tell them Reaper made a comparison it refused to make, on the
        card they are deciding from."""
        plan = plan_series_prune(
            series_title="S",
            seasons=[_season(n) for n in range(1, 6)],
            keep_last=2,
            keep_first_season=True,
            watchers_by_season={1: 0, 2: 9, 3: 1, 4: 2, 5: None},
        )
        # The premise, asserted rather than assumed: a readable conflict really does come
        # first for this season, so a first-match read would report "compared" and release.
        season_2 = [c for c in plan.conflicts if c.pruned_season == 2]
        assert [c.kept_watchers for c in season_2] == [0, 2, None]

        result = season_scan.guard_result(plan, 2)

        assert result.blocked is True
        assert result.defers_to_owner is False
        # The message names the season that could not be read, not the ones that could.
        assert "could not check who watched Season 5" in result.detail

    def test_a_conflict_the_mirror_could_not_settle_is_flagged_as_refused(self) -> None:
        """The third shape. Season 1 was watched before Tautulli was installed, so its count
        is a lower bound and no comparison against it can be made. That is ``Unknown``, not
        a decision (rule 93), so it joins the unreadable arm rather than the deliberate "you
        decide" one -- the reading the mid-binge hold takes when the reach cannot establish
        who is part-way through. Reading only ``kept_watchers is None`` would have grouped
        it with the settled shape and claimed, on the card, that a comparison had been made.

        The flag no longer changes the verdict -- the item still goes to a human, who may
        still say remove it -- only what they are told, which is why the two Unknown shapes
        are kept apart from the settled one."""
        plan = plan_series_prune(
            series_title="S",
            seasons=[_season(n) for n in range(1, 5)],
            keep_last=2,
            keep_first_season=False,
            watchers_by_season={1: 0, 2: 0, 3: 1, 4: 1},
            shortfall_by_season={1: "your watch history only goes back 12 months"},
        )
        result = season_scan.guard_result(plan, 1)

        assert result.blocked is True
        assert result.defers_to_owner is False
        assert "cannot tell whether Season 1 is watched more than" in result.detail
        assert _hand_reap(result) == "condemn"

    def test_a_settleable_conflict_is_not_masked_by_one_the_mirror_refused(self) -> None:
        """Same precedence the unreadable arm gets, for the same reason: one pruned season
        carries a conflict per kept season, so a comparison Reaper DID make can sort ahead of
        one it could not. The refusal decides the flag and the message, or a first-match read
        would report "compared" and release the reap on a season nothing established."""
        plan = plan_series_prune(
            series_title="S",
            seasons=[_season(n) for n in range(1, 5)],
            keep_last=2,  # keeps 3, 4
            keep_first_season=False,
            watchers_by_season={1: 9, 2: 0, 3: 1, 4: 2},
            shortfall_by_season={4: "your watch history only goes back 12 months"},
        )
        # The premise, asserted rather than assumed: the made comparison really does come
        # first for this season.
        assert [c.shortfall for c in plan.conflicts if c.pruned_season == 1] == [
            None,
            "your watch history only goes back 12 months",
        ]

        result = season_scan.guard_result(plan, 1)

        assert result.defers_to_owner is False
        assert "Season 4" in result.detail


# ---------------------------------------------------------------------------
# Facts assembly, and the Unknown discipline
# ---------------------------------------------------------------------------


def _facts(**over: Any) -> Any:
    base: dict[str, Any] = {
        "title": "Show · Season 3",
        "season": _season(3, size=8 * GB),
        "rank": 2,
        "plex_rating_key": 700,
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
        # Override to False for the other case: no id, so we never asked.
        "rating_looked_up": True,
    }
    base.update(over)
    return season_scan.build_season_facts(**base)


class TestSeasonsRecordTheHistoryReachToo:
    """Rule 72: the movie builder's fix lands on its twin in the same change.

    Both lanes count watchers out of the same ``watch_event`` mirror over the same
    policy window, so both need to say how far back that mirror reaches or
    ``ServerPopularityGate`` (shared by both) blocks every season forever. See
    ``test_fact_layer_states.TestTheScanRecordsHowFarBackItsHistoryReaches``.
    """

    def test_the_season_builder_records_the_reach_it_was_given(self) -> None:
        """Given, not measured: the reach is sampled once per scan on
        ``snapshot.ScanContext`` so the two lanes cannot freeze different values, and this
        builder's job is only to carry it onto the season's own Facts."""
        facts = _facts(horizon=utcnow() - timedelta(days=200), reach_days=200)

        assert facts.history_reach_days == Known(value=200, source="tautulli")

    def test_an_unresolved_season_still_records_it(self) -> None:
        """The reach is a property of the mirror, not of the season, so it is known even
        for a season whose own watch facts are not. Were it to ride on the rating key,
        every unmatched season would block on the reach instead of on the honest
        "Plex has not matched this" the watch facts already carry."""
        facts = _facts(plex_rating_key=None, horizon=utcnow() - timedelta(days=200), reach_days=200)

        assert facts.history_reach_days == Known(value=200, source="tautulli")


class TestBuildSeasonFacts:
    def test_an_unresolved_season_has_unknown_watch_facts(self) -> None:
        """No Plex rating key means no history to read. Dormancy, popularity and streaming
        all go Unknown -- and Unknown, through the gates, protects."""
        facts = _facts(plex_rating_key=None)
        assert isinstance(facts.days_observed_unwatched, Unknown)
        assert isinstance(facts.distinct_watchers, Unknown)
        assert isinstance(facts.is_streaming_now, Unknown)

    def test_an_ambiguous_show_gets_the_honest_unknown_reason(self) -> None:
        """An AMBIGUOUS show (two Plex items share its id) is not "unmatched" -- Plex has
        it, more than once. The Unknown reason must tell that story, or the why-panel
        claims the show couldn't be found when the opposite is true."""
        facts = _facts(plex_rating_key=None, show_match_status=identity.MatchStatus.AMBIGUOUS)
        assert isinstance(facts.days_observed_unwatched, Unknown)
        assert facts.days_observed_unwatched.reason == "more than one Plex item matches this show"

        unmatched = _facts(plex_rating_key=None, show_match_status=identity.MatchStatus.UNMATCHED)
        assert isinstance(unmatched.days_observed_unwatched, Unknown)
        assert unmatched.days_observed_unwatched.reason == "Plex has not matched this season"

    def test_a_season_unmatched_within_a_matched_show_is_warned(self) -> None:
        """The show bound to Plex but this season did not, so it abstains and shows only
        as kept-to-be-safe. A warning names it so "why is this season kept" is answerable
        from the log: the season-level twin of the movie and show miss."""
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
        """A season that binds to Plex is silent: the warning fires only on a real miss."""
        with capture_logs() as logs:
            _facts(plex_rating_key=700, show_match_status=identity.MatchStatus.MATCHED)

        assert [e for e in logs if e["event"] == "scan.plex_unmatched"] == []

    def test_a_matched_season_with_no_arrival_date_is_warned(self) -> None:
        """Matched to a Plex season, but no added-at and no plays: dormancy is Unknown, so
        it abstains and shows only as kept-to-be-safe. A warning names it, the same as the
        movie path. A distinct event from the unmatched case: this season DID bind."""
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
        """Even a resolved season goes Unknown-streaming if we could not read sessions:
        not being able to look is never the same as nobody watching."""
        facts = _facts(plex_rating_key=700, activity_degraded=True)
        assert isinstance(facts.is_streaming_now, Unknown)

    def test_a_season_of_a_show_we_looked_up_and_found_unrated_is_absent(self) -> None:
        """There is no free per-season IMDb rating; Sonarr's ratings are flat TVDB. A
        show we could look up and did not find is Absent: unrated, so a rating keep
        does not hold it."""
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
        reaches here with rank=None. That is Absent: we looked, and it genuinely has no
        rank slot. Recording it as Unknown claimed Sonarr could not be read and made the
        SEASON_RANK signal say "could not tell which season this is", dragging the special's
        coverage down for a rank it was never meant to have."""
        facts = _facts(rank=None)
        assert isinstance(facts.season_rank, Absent)

    def test_a_season_is_always_managed(self) -> None:
        facts = _facts()
        assert isinstance(facts.is_managed, Known) and facts.is_managed.value is True

    def test_size_comes_from_sonarr(self) -> None:
        facts = _facts(season=_season(3, size=8 * GB))
        assert isinstance(facts.size_bytes, Known) and facts.size_bytes.value == 8 * GB

    def test_a_season_whose_size_sonarr_did_not_report_is_unknown(self) -> None:
        """The mirror of the movie case. As Known(0) it reads as a real measurement:
        maximum pressure on a size signal, and any "keep large files" rule silently
        stops holding the season. See tests/test_fact_layer_states.py."""
        facts = _facts(season=_season(3, size=None))
        assert isinstance(facts.size_bytes, Unknown)

    def test_dormancy_is_measured_from_the_seasons_own_arrival(self) -> None:
        """A season backfilled into an old show arrived recently. Dormancy must count from
        the season's OWN added date, not the show's -- using the show's would read a
        just-added season as decades dormant and condemn a file nobody could have watched."""
        facts = _facts(
            season_added_at=utcnow() - timedelta(days=5),  # files landed 5 days ago
            horizon=utcnow() - timedelta(days=4000),  # mature install
            last_played=None,
        )
        assert isinstance(facts.days_observed_unwatched, Known)
        assert facts.days_observed_unwatched.value < 30

    def test_a_resolved_season_with_no_arrival_date_is_unknown_dormancy(self) -> None:
        """A season with neither an arrival date nor a play has its dormancy Unknown -- never a
        Known dormancy fabricated from the horizon. Unknown then forces the dormancy gates to
        protect. Both inputs are pinned absent deliberately: a play alone is enough to measure
        from, which is what the next test covers."""
        facts = _facts(season_added_at=None, last_played=None)
        assert isinstance(facts.days_observed_unwatched, Unknown)

    def test_no_arrival_date_but_a_play_measures_from_the_play(self) -> None:
        """The state neither lane had a test for (#257), on the lane that got it right.

        Both existing no-arrival-date tests pin `last_played=None`, so the divergence they
        were meant to cover was invisible: this lane measured from the play while the movie
        lane discarded it and abstained. Dormancy IS days since the last play, so the play
        alone is a real measurement -- and the number must come from the play, not the horizon,
        which is 4000 days back in this fixture and would read as far more pressure.
        """
        facts = _facts(season_added_at=None, last_played=utcnow() - timedelta(days=12))

        assert isinstance(facts.days_observed_unwatched, Known)
        # A range, not an equality: production samples its own `utcnow()`, and comparing two
        # samples of the clock it reads is rule 133's flake. 4000 is what the horizon would
        # give, so this discriminates the play from the fallback by three orders of magnitude.
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
        clean = season_scan.guard_result(
            plan_series_prune(
                series_title="S", seasons=[_season(3)], keep_last=0, keep_first_season=False
            ),
            3,
        )
        assert _judge(facts, clean) == "abstain"

    def test_a_seen_dormant_unwatched_season_can_be_condemned(self) -> None:
        """The other side of the guarantee: when the evidence is real and the guards allow
        it, a season IS condemnable -- otherwise the whole path would be inert."""
        facts = _facts(plex_rating_key=700, last_played=None, watchers_window=0)
        clean = season_scan.guard_result(
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
        protect = season_scan.guard_result(
            plan_series_prune(series_title="S", seasons=[_season(3)], keep_last=1), 3
        )
        assert protect.outcome == PROTECT
        assert _judge(facts, protect) == "protect"

    def test_a_freshly_backfilled_season_of_an_old_show_is_not_condemned(self) -> None:
        """The critical regression. A mature install (horizon ~4y ago); an old show whose
        MIDDLE season the operator just backfilled (files landed 5 days ago, never played).
        keep-last/keep-first protect the newest and first seasons by NUMBER -- not this
        middle season -- so only the dormancy discipline stands between it and a wrongful
        condemn. It must read as freshly arrived, not decades dormant."""
        facts = _facts(
            plex_rating_key=700,
            season_added_at=utcnow() - timedelta(days=5),
            horizon=utcnow() - timedelta(days=4000),
            last_played=None,
            watchers_window=0,
        )
        clean = season_scan.guard_result(
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
        """A season resolved in Plex but whose arrival date could not be read must abstain,
        not be condemned off the horizon -- the exact fail-open the movie path guards."""
        facts = _facts(
            plex_rating_key=700,
            season_added_at=None,
            horizon=utcnow() - timedelta(days=4000),
            last_played=None,
            watchers_window=0,
        )
        clean = season_scan.guard_result(
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
# Per-season watch statistics, against a real mirror
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
) -> None:
    """``status=None`` is the row Tautulli never told us the completion of."""
    when = int((utcnow() - timedelta(days=days_ago)).timestamp())
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO watch_event (rating_key, parent_rating_key, "
                "grandparent_rating_key, user_id, watched_at, watched_status, "
                "percent_complete, media_type, media_index) "
                "VALUES (:rk, :season, :show, :uid, :ts, :status, 100, 'episode', :ep)"
            ),
            {
                "rk": season_key * 1000 + user_id + (episode or 0),
                "season": season_key,
                "show": show_key,
                "uid": user_id,
                "ts": when,
                "ep": episode,
                "status": status,
            },
        )


class TestTheCacheIsRebuiltNotMigrated:
    """Cache tables are never migrated -- the Alembic baseline says so, and they are
    rebuildable by definition. A stale shape is dropped and recreated, not patched."""

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
        """The shape an actually-upgraded install has: all ten columns, in order, with the
        old `watched_status REAL NOT NULL`. Nothing about the *names* changed, so a check
        that compared names alone left this table in place and the next sync died on the
        first unreported completion. The rebuild has to fire on nullability."""
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
        """The schema guard both siblings carry (rule 72), against the state needing it.

        The `cache_engine` fixture runs `ensure_schema` itself, so every other test in this
        class meets a table that already exists; this one takes a raw cache. Without the
        guard the read raises `no such table: watch_event`, nothing catches it, and the whole
        scan aborts on a technical string (rule 21) instead of reading no plays.

        Reading no plays is not itself what keeps the file: an empty mirror resolves the
        horizon to `utcnow()`, so a season with an arrival date reads Known ZERO days
        dormant. `snapshot.scan` degrades the whole snapshot un-plannably on that mirror,
        which is the actual hold.
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
        """Two plays under one season keep the newer timestamp -- the mid-binge expiry
        judges a viewer by their most recent activity, never their first."""
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

        Episodes 1-3 are recorded complete; 4-10 were played but Tautulli never said
        whether they finished. Reading those as "not completed" puts the viewer at
        episode 3, so `sequential_protections` calls them still-on-this-season and the
        NEXT season -- the one they are actually about to watch -- loses its protection.
        The default lookahead is 0, so nothing else covers it. Position must read as
        unknown, which drops the guard to season level.
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
        """0.0 is a real answer ("started it, did not finish") and must keep behaving as
        one. Only a MISSING status is unknown; conflating them again in the other
        direction would blind the guard on every partially-watched episode."""
        await _episode(cache_engine, season_key=712, user_id=1, episode=3)
        await _episode(cache_engine, season_key=712, user_id=1, episode=4, status=0.0)

        stats = await season_scan.season_watch_stats(cache_engine, {712}, window_days=365)

        assert stats.user_season_progress[1][712] == 3


class TestProgressByUser:
    def test_progress_is_scoped_to_this_show(self) -> None:
        """A user's progress in another series must not leak in: only this show's season
        keys are consulted, mapped to season numbers with their completed-episode positions."""
        stats = season_scan.SeasonWatchStats(
            user_season_keys={7: {701, 702, 999}},
            user_season_progress={7: {701: 4, 702: 9}},
        )
        # 701 -> season 2, 702 -> season 3; 999 belongs to another show and is ignored.
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
        # 999 is another show: its recency must not keep this show's hold alive.
        assert season_scan._last_watched_by_user(stats, {701: 2, 702: 3}) == {"7": new}

    def test_any_unreadable_timestamp_means_unknown(self) -> None:
        """One readable-old and one unreadable play: the unreadable one could be recent,
        so the viewer's whole-show recency is None and their hold stays."""
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

    Without it ``gather`` degrades the snapshot on the ratings read, and a degraded scan
    abstains on everything -- which would let a test asserting "not condemned" pass for a
    reason it never meant to check (rule 118)."""
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
    """A stand-in for the plexapi sweeps: the GUID index (rating_key -> PlexItem) and the
    season index (show rating_key -> its season rows). ``seasons`` empty means the sweep
    found nothing, which sends every show to the per-show Tautulli fallback."""

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
            keep_last_seasons=2,
            keep_first_season=True,
            window_days=365,
            whitelisted=set(),
            degrade=degrade,
            watch_marks={},
        )

        by_key = {j.media_key: j for j in judgments}
        # Season 3 is prunable (outside keep-last 2, not the first): resolved to its Plex key.
        assert "sonarr:1:42:3" in by_key
        assert by_key["sonarr:1:42:3"].plex_rating_key == 903
        # The card poster comes from the SHOW's key (900), never the season's -- a season
        # often has no poster of its own, so the season key would 404 to a placeholder.
        assert by_key["sonarr:1:42:3"].poster_rating_key == 900
        # The first and last-two seasons are protected, and emitted so the panel shows why.
        assert by_key["sonarr:1:42:1"].guard_result.outcome == PROTECT
        assert by_key["sonarr:1:42:5"].guard_result.outcome == PROTECT

    async def test_a_show_the_sweep_missed_falls_back_to_the_per_show_read(
        self, cache_engine: AsyncEngine
    ) -> None:
        """The season sweep is present but returns nothing for this show (a partial sweep, or
        a show the sweep could not place). Resolution must fall back to the per-show Tautulli
        read rather than lose the season -- the belt-and-suspenders that keeps S2 unable to
        resolve LESS than the path it replaced."""
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
            keep_last_seasons=2,
            keep_first_season=True,
            window_days=365,
            whitelisted=set(),
            degrade=degrade,
            watch_marks={},
        )

        by_key = {j.media_key: j for j in judgments}
        # Season 3 still resolves to its Plex key, via the per-show fallback.
        assert by_key["sonarr:1:42:3"].plex_rating_key == 903

    async def test_a_raising_season_sweep_falls_back_per_show_and_does_not_degrade(
        self, cache_engine: AsyncEngine
    ) -> None:
        """library_season_index RAISING (not returning empty) hits the ``except PlexError``
        branch: the whole library falls back to the per-show read rather than degrading, since
        the same data is reachable one show at a time (I-2). The empty-dict path above exercises
        different code than this except."""
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
            keep_last_seasons=2,
            keep_first_season=True,
            window_days=365,
            whitelisted=set(),
            degrade=degrade,
            watch_marks={},
        )

        by_key = {j.media_key: j for j in judgments}
        # Season 3 resolves via the per-show fallback, and the raise did not degrade the scan
        # (it logs a warning and falls back; the only degradations here are unrelated).
        assert by_key["sonarr:1:42:3"].plex_rating_key == 903
        assert not any("sweep" in r.lower() or "season" in r.lower() for r in reasons)

    async def test_episodes_are_not_fetched_when_keep_in_progress_is_off(
        self, cache_engine: AsyncEngine
    ) -> None:
        """With mid-binge protection off, ``season_final_episode`` is never consulted, so the
        whole Sonarr episodes() fan-out is skipped (I-2). Skipping only ever keeps more."""
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
            keep_last_seasons=2,
            keep_first_season=True,
            window_days=365,
            whitelisted=set(),
            degrade=degrade,
            keep_in_progress=False,
            watch_marks={},
        )
        assert sonarr.episodes_called == []  # the fan-out was skipped
        assert "sonarr:1:42:3" in {j.media_key for j in off}  # seasons still resolve

        # Companion: with the guard ON, the fan-out runs, so the skip above is a real branch.
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
            keep_last_seasons=2,
            keep_first_season=True,
            window_days=365,
            whitelisted=set(),
            degrade=degrade,
            keep_in_progress=True,
            watch_marks={},
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
            # The season sweep carries both copies' seasons; the 4K copy (900) is the one the
            # folder name binds this Sonarr to, so its keys are the ones that resolve.
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
            keep_last_seasons=2,
            keep_first_season=True,
            window_days=365,
            whitelisted=set(),
            degrade=degrade,
            watch_marks={},
        )

        pruned = next(j for j in judgments if j.media_key == "sonarr:1:56:3")
        assert pruned.matched_by is identity.MatchedBy.ID_AND_BASENAME
        assert pruned.match_status is identity.MatchStatus.MATCHED
        assert pruned.plex_rating_key == 903  # season 3 under the 4K copy, never the HD one

    async def test_plex_supplies_the_rating_and_poster_when_sonarr_cannot(
        self, cache_engine: AsyncEngine
    ) -> None:
        """Sonarr has no imdbId (common for reality/recent shows), but the show matches Plex
        by tvdb and Plex carries the imdb id. The rating comes through on the Plex id, and
        the card poster uses the show's key -- so neither the rating nor the poster is lost
        to a Sonarr/TVDB metadata gap."""
        await _seed_ratings(cache_engine, {"tt7777": (7.1, 38)})
        series = [
            {
                "id": 55,
                "title": "Reality Show",
                "year": 2020,
                "status": "ended",
                "ended": True,
                "tvdbId": 4242,  # matches Plex by tvdb; NO imdbId from Sonarr
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
            keep_last_seasons=2,
            keep_first_season=True,
            window_days=365,
            whitelisted=set(),
            degrade=degrade,
            watch_marks={},
        )

        pruned = next(j for j in judgments if j.media_key == "sonarr:1:55:3")
        # Rating resolved via the Plex-supplied imdb id, even though Sonarr had none.
        assert isinstance(pruned.facts.imdb_rating_tenths, Known)
        assert pruned.facts.imdb_rating_tenths.value == 71
        # Poster uses the show's key (800), not the season's (803).
        assert pruned.poster_rating_key == 800
        assert pruned.plex_rating_key == 803
        # The show's display metadata is inherited by every season row: the Sonarr web
        # coordinate, certification, runtime, and a ratings row whose IMDb entry is the
        # SAME dataset number the scoring signal froze (never a second source).
        assert pruned.title_slug == "reality-show"
        # Outbound-link coordinates: the show's tmdb id, and the imdb id resolved the
        # same way the rating was (Sonarr had none, so the Plex-matched one serves).
        assert pruned.tmdb_id == 999
        assert pruned.imdb_id == "tt7777"
        # Sonarr's native tvdb id rides onto every season row too, so Scales can join a
        # request to this show even when it has no tmdb id (services.fairness; rule 29).
        assert pruned.tvdb_id == 4242
        assert pruned.content_rating == "TV-PG"
        assert pruned.runtime_minutes == 50
        # The show's ended-ness is a show-level fact too: one reading of the series,
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
        read whitelisted -- an explicitly-set protection that fails open on the deletion
        path is the worst possible failure."""
        series = [
            {
                "id": 77,
                "title": "Tagged Show",
                "year": 2018,
                "status": "ended",
                "ended": True,
                "tvdbId": 5150,  # NO imdbId from Sonarr
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
            keep_last_seasons=2,
            keep_first_season=True,
            window_days=365,
            whitelisted=set(),
            degrade=degrade,
            membership_index=index,
            watch_marks={},
        )

        assert judgments, "the show's seasons must still be gathered"
        for judgment in judgments:
            assert isinstance(judgment.facts.is_whitelisted, Known)
            assert judgment.facts.is_whitelisted.value is True

    async def test_an_unmatched_series_yields_unresolved_seasons(
        self, cache_engine: AsyncEngine
    ) -> None:
        """Plex has not matched the show. Its prunable seasons still appear -- so the owner
        learns Plex failed to match them -- but with no Plex key and Unknown facts, so they
        can only abstain."""
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
            keep_last_seasons=2,
            keep_first_season=True,
            window_days=365,
            whitelisted=set(),
            degrade=degrade,
            watch_marks={},
        )
        pruned = next(j for j in judgments if j.media_key == "sonarr:1:7:3")
        assert pruned.plex_rating_key is None
        assert isinstance(pruned.facts.days_observed_unwatched, Unknown)

    async def test_a_fully_protected_short_show_is_surfaced_as_kept(
        self, cache_engine: AsyncEngine
    ) -> None:
        """A show with no prunable season is NOT dropped: it is gathered and surfaced as kept,
        every season protected by its guard, so content is never hidden from the UI."""
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
            # about are what hold these seasons. At reach 0 the mid-binge guard is
            # un-establishable and holds EVERY season on its own, which made the assertion
            # below pass with both floors deleted (rule 141).
            horizon=utcnow() - timedelta(days=4000),
            reach_days=4000,
            active_rating_keys=set(),
            activity_degraded=False,
            keep_last_seasons=2,
            keep_first_season=True,
            window_days=365,
            whitelisted=set(),
            degrade=degrade,
            watch_marks={},
        )
        # Both content-bearing seasons appear, each protected by a guard (never condemned).
        assert {j.media_key for j in judgments} == {"sonarr:1:9:1", "sonarr:1:9:2"}
        assert all(j.guard_result.outcome is PROTECT for j in judgments)

    async def test_a_candidate_show_logs_its_decision(self, cache_engine: AsyncEngine) -> None:
        """Every scanned series emits one greppable decision line. A show with a prunable
        season records outcome=candidate, the prunable season numbers, and the raw per-season
        file counts Sonarr reported -- the record an operator greps by title."""
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
                keep_last_seasons=2,
                keep_first_season=True,
                window_days=365,
                whitelisted=set(),
                degrade=degrade,
                watch_marks={},
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
        """A show with nothing prunable is dropped from the queue, but its decision line names
        outcome=fully_protected and why each on-disk season is kept -- so "why isn't my show in
        review" is answerable without re-running the scan."""
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
                keep_last_seasons=2,
                keep_first_season=True,
                window_days=365,
                whitelisted=set(),
                degrade=degrade,
                watch_marks={},
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
        """The reach reaches the mid-binge guard, not only the watcher counts (rule 140).

        The same show under the same policy twice, differing only in how far back the watch
        mirror goes. Past the hold, the middle seasons are candidates. Short of it, a viewer
        whose plays all predate the horizon leaves no rows, so "nobody is part-way through"
        is a claim the history cannot support and every season is held instead.

        The reaches straddle the *stated* 200-day hold, not the 180-day default, so a call
        site that hardcoded the default (or dropped the reach altogether) fails here (rule
        141): at reach 190 the default would still read as establishable.

        The show is BOUND in Plex, and every season with it, so the mirror's depth is the
        only thing that moves. Left unbound, the guard has no rating key to read a place in
        the show from and blocks for that instead (#486) -- which is a true sentence about a
        fixture that never meant to say it, and would let a broken reach pass here.
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
        # Inside the SHALLOWER of the two reaches, so no watcher count is a lower bound in
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
                keep_last_seasons=2,
                keep_first_season=True,
                window_days=365,
                in_progress_hold_days=200,
                whitelisted=set(),
                degrade=degrade,
                watch_marks={},
            )

        deep = await _run(400)
        assert [j.media_key for j in deep if j.guard_result.outcome is not PROTECT] == [
            "sonarr:1:11:2",
            "sonarr:1:11:3",
        ]

        shallow = await _run(190)
        assert all(j.guard_result.outcome is PROTECT for j in shallow)
        assert {
            j.guard_result.detail
            for j in shallow
            if j.media_key in {"sonarr:1:11:2", "sonarr:1:11:3"}
        } == {"your watch history is too short to tell who is part-way through"}

        # ...and BLOCKED, not merely protecting. A plain PROTECT on this gate does not hold
        # a hand reap (`verdict.STRUCTURAL_GATES` carries neither), while the keep-rule
        # conflict this blanket hold displaces did -- so without the block, closing #95
        # would have made a season a hand reap was refused on into one it deletes.
        blocked = {j.media_key for j in shallow if j.guard_result.blocked}
        assert blocked == {"sonarr:1:11:2", "sonarr:1:11:3"}
        assert not any(j.guard_result.defers_to_owner for j in shallow)
        # Narrow on purpose: seasons 1, 4 and 5 are held by protections that genuinely
        # FIRED (earliest season, keep-last), and a definite keep must stay definite. A fix
        # that blocked every kept season would fail here.
        assert all(j.guard_result.outcome is PROTECT for j in shallow)
        # Nothing in the deep arm is blocked: with the mirror spanning the hold there is
        # no unanswered question to hold anything on.
        assert not any(j.guard_result.blocked for j in deep)

    async def test_a_season_plex_never_resolved_holds_the_one_its_viewer_is_up_to(
        self, cache_engine: AsyncEngine
    ) -> None:
        """#472, end to end and against the real default policy.

        A viewer finished Season 3 yesterday, so the mid-binge guard should hold Season 4 --
        the season they are about to watch. Season 3's plays are filed under its own Plex key,
        so the guard can only see them if that key was resolved. The two runs differ by one
        thing: a second "Season 3" item in the Plex sweep, which ``seasons_from_rows`` drops
        as ambiguous (a split or mis-scanned library emits these).

        Season 3 itself is safe either way -- with no key its own facts are Unknown and it
        abstains. Its SIBLINGS are the loss: they resolved, they carry fully readable facts,
        and they condemn at full confidence on a viewer nothing can see. Before the fix
        Season 4 came out condemned with the guard reporting "checked: prunable", which is
        rule 93's failure -- a panel asserting a check that never ran.
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
        # Arrived 2000 days ago: past the 1095-day dormancy floor, so an unwatched season here
        # really is condemnable, and inside the 4000-day reach, so no watcher count is a lower
        # bound and the keep-rule conflict detector stays out of the way. Both halves matter --
        # either one left these seasons abstaining for a reason the test does not mean to check.
        arrived = str(int((utcnow() - timedelta(days=2000)).timestamp()))
        clean = {
            900: [
                {"media_index": n, "rating_key": 900 + n, "added_at": arrived} for n in range(1, 6)
            ]
        }
        # The same list, plus a second "Season 3" -- so season 3 alone loses its key.
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
                keep_last_seasons=0,  # nothing shields season 4 but the mid-binge guard
                keep_first_season=False,
                window_days=365,
                whitelisted=set(),
                degrade=degrade,
                watch_marks={},
            )
            assert not _reasons, f"the scan degraded, so no verdict here means anything: {_reasons}"
            return {j.media_key: j for j in judgments}

        control = await _run(clean)
        season_4 = control["sonarr:1:42:4"]
        assert season_4.guard_result.outcome is PROTECT
        assert season_4.guard_result.detail == "a viewer is part-way through the show"
        assert _judge(season_4.facts, season_4.guard_result) == "protect"
        # ...and the siblings ARE condemnable evidence-wise, so the run below is not passing
        # on some unrelated abstain (rule 141).
        # ...and the siblings really are condemnable on their own evidence, so "not condemned"
        # below is a statement about the fix rather than about some unrelated abstain the
        # fixture happened to produce (rule 141).
        assert _judge(control["sonarr:1:42:1"].facts, control["sonarr:1:42:1"].guard_result) == (
            "condemn"
        )

        broken = await _run(split)
        assert broken["sonarr:1:42:3"].plex_rating_key is None  # the ambiguous one
        for n in (1, 2, 4, 5):
            judgment = broken[f"sonarr:1:42:{n}"]
            assert _judge(judgment.facts, judgment.guard_result) != "condemn"
            assert judgment.guard_result.detail == (
                "a season of this show is not matched in Plex, so who is part-way through "
                "is unknown"
            )
            # Blocked, not a plain keep: the guard could not be ANSWERED (rule 93), so the
            # panel says "couldn't check" rather than green, and a hand reap still overrules.
            assert judgment.guard_result.blocked is True

    async def test_a_failed_season_read_stops_the_show_asserting_nobody_is_watching(
        self, cache_engine: AsyncEngine
    ) -> None:
        """#472's own reproduction: ``resolve_season_keys`` raising, for a show that DID bind
        to Plex. Returning an empty map is fail-closed for that show's own seasons -- they all
        abstain on Unknown facts -- but it said nothing about the assertion the show then made
        about viewer progress, and the mid-binge guard reported as checked and passed."""
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
            # The show binds to Plex; only its season list is unreadable, so the sweep is empty
            # and every season falls to the per-show read that raises.
            plex=_FakePlexGuids(plex_items, seasons={}),  # type: ignore[arg-type]
            horizon=utcnow() - timedelta(days=4000),
            reach_days=4000,
            active_rating_keys=set(),
            activity_degraded=False,
            keep_last_seasons=0,
            keep_first_season=False,
            window_days=365,
            whitelisted=set(),
            degrade=degrade,
            watch_marks={},
        )

        by_key = {j.media_key: j for j in judgments}
        assert len(by_key) == 5
        for judgment in by_key.values():
            assert judgment.plex_rating_key is None
            assert judgment.guard_result.detail == (
                "a season of this show is not matched in Plex, so who is part-way through "
                "is unknown"
            )
            assert judgment.guard_result.blocked is True

    async def test_a_show_plex_never_matched_at_all_is_left_alone(
        self, cache_engine: AsyncEngine
    ) -> None:
        """The deliberate boundary on #472's fix, pinned because it is the half a later author
        would most reasonably widen.

        The hold fires only where the SHOW bound to Plex and some of its seasons did not,
        because that is the mix where a readable sibling exists to condemn on the hidden
        viewer. Where nothing about the show resolved, every season already takes Unknown from
        its own branch and abstains, so widening the hold to cover it would move a whole
        population of unmatched shows out of the review queue and protect nothing further.

        So nothing moves, and the guard says what it did instead of what it found (#486): the
        check never ran, in the same words the season's four Plex-dependent gates use, so the
        panel prints the cause once for all five rather than reporting a pass beside them.
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
            keep_last_seasons=2,
            keep_first_season=True,
            window_days=365,
            whitelisted=set(),
            degrade=degrade,
            watch_marks={},
        )
        by_key = {j.media_key: j for j in judgments}
        assert by_key["sonarr:1:77:2"].plex_rating_key is None
        guard = by_key["sonarr:1:77:2"].guard_result
        # ABSTAIN, never PROTECT: the season stays in the review queue's abstain lane, which
        # is the whole reason the hold was scoped away from here.
        assert guard.outcome is ABSTAIN
        # Blocked and unestablishable, so the panel renders it amber under "left for you to
        # decide" rather than green under "protections it cleared" (rule 93), and the panel's
        # conflict branch skips it -- nothing was compared, so nothing is being handed over.
        assert guard.blocked
        assert guard.unestablishable
        assert not guard.defers_to_owner
        # The cause is the one the season's own Unknown facts carry, character for character,
        # which is what makes `WhyPanel.LeftForYou` group all five under one heading instead
        # of opening a second box saying the same thing (rule 144).
        cause = season_scan.no_key_reason(identity.MatchStatus.UNMATCHED)
        assert guard.detail == f"could not check who is part-way through it: {cause}"
        unwatched = by_key["sonarr:1:77:2"].facts.days_observed_unwatched
        assert isinstance(unwatched, Unknown)
        assert unwatched.reason == cause

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
                keep_last_seasons=2,
                keep_first_season=True,
                window_days=365,
                whitelisted=set(),
                degrade=degrade,
                watch_marks={},
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
            keep_last_seasons=2,
            keep_first_season=True,
            window_days=365,
            whitelisted=set(),
            degrade=degrade,
            watch_marks={},
        )
        assert judgments == []
        assert any("sonarr" in r and "unreachable" in r for r in reasons)


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
            # Nothing completed at all: `max_ep is None`, so there is no position to name.
            ([(2, None)], "no completed episode"),
            # Completed ep 1, then plays of ep 4 Tautulli never reported the completion of.
            # They may be further on than the position says, so it is dropped rather than
            # trusted low -- being wrong in that direction unprotects the season they are
            # about to watch next.
            ([(1, 1.0), (4, None)], "a later play whose completion is unknown"),
        ],
    )
    async def test_a_dropped_position_holds_the_season_rather_than_clearing_it(
        self, cache_engine: AsyncEngine, plays: list[tuple[int, float | None]], why: str
    ) -> None:
        """#470. ``season_watch_stats`` drops a progress row down two branches, and both are
        locally keep-safe by intent. What makes them keep-safe *downstream* is an invariant
        that lives in a different query and nothing pinned: the ``pairs`` read that fills
        ``user_season_keys`` carries no ``media_index`` filter, so it is a strict superset of
        the ``progress`` read. A viewer whose position was dropped is therefore still present
        as a *touch*, ``_progress_by_user`` records them as ``None`` -- position unknown, not
        absent -- and ``_anchor_positions`` fails closed on that and holds the season plus the
        one after it (rule 93).

        Narrowing ``pairs`` to match ``progress``'s filters would make the viewer vanish
        instead, and the mid-binge guard would then read a dropped position as "nobody is
        part-way through". That change looks like a tidy-up and is a protection loss, which is
        why the chain is asserted end to end here rather than at the query.
        """
        for episode, status in plays:
            await _episode(cache_engine, season_key=903, user_id=7, episode=episode, status=status)
        stats = await season_scan.season_watch_stats(cache_engine, {901, 902, 903}, window_days=365)
        assert stats.user_season_progress.get(7, {}).get(903) is None, f"{why} left a position"

        key_to_number = {901: 1, 902: 2, 903: 3}
        progress = season_scan._progress_by_user(stats, key_to_number)
        # Present, and Unknown -- the distinction the whole chain turns on.
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
        # Season 3 because she may still be on it, season 4 because she may have finished it:
        # with the position unknown, `_anchor_positions` cannot tell and holds both.
        assert plan.prunable == [1, 2]
        held = {p.season_number: p.reason for p in plan.protected}
        assert held[3] == "a viewer is part-way through the show"
        assert held[4] == "a viewer is part-way through the show"


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
