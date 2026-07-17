# SPDX-License-Identifier: AGPL-3.0-or-later
"""The season-pruning guards.

"Keep the last N seasons" is where TV pruning goes wrong, so each guard has a test that
is really a re-enactment of a bug a shipping competitor has. Every case here resolves
toward keeping a season; the ones that prune are the ones where every guard agreed.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from reaper.clients.sonarr_stats import SeasonStats
from reaper.services.season_pruning import (
    active_progress,
    plan_series_prune,
    sequential_protections,
)

GB = 1024**3


def _season(
    n: int,
    *,
    files: int = 5,
    size: int = GB,
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


def _reasons(plan: object) -> dict[int, str]:
    return {p.season_number: p.reason for p in plan.protected}  # type: ignore[attr-defined]


class TestKeepLastN:
    def test_the_newest_n_seasons_are_kept(self) -> None:
        seasons = [_season(n) for n in range(1, 6)]  # 1..5
        plan = plan_series_prune(
            series_title="Show", seasons=seasons, keep_last=2, keep_first_season=False
        )
        # Rank 1 = season 5, rank 2 = season 4 -> kept. 2 and 3 prunable; 1 also prunable
        # only because keep_first_season is off here.
        assert 5 not in plan.prunable
        assert 4 not in plan.prunable
        assert {2, 3}.issubset(set(plan.prunable))

    def test_a_negative_keep_is_clamped_not_widened(self) -> None:
        """A negative keep_last must never make *more* prunable than keep_last=0."""
        seasons = [_season(n) for n in range(1, 4)]
        plan = plan_series_prune(
            series_title="Show", seasons=seasons, keep_last=-5, keep_first_season=False
        )
        # keep_last clamped to 0, so nothing is protected by the keep rule; every season
        # is prunable (no other guard fires here).
        assert set(plan.prunable) == {1, 2, 3}

    def test_empty_seasons_are_neither_pruned_nor_protected(self) -> None:
        seasons = [_season(1), _season(2, files=0, size=0)]
        plan = plan_series_prune(
            series_title="Show", seasons=seasons, keep_last=0, keep_first_season=False
        )
        assert 2 not in plan.prunable
        assert 2 not in _reasons(plan)

    def test_an_announced_fileless_season_does_not_spend_a_keep_slot(self) -> None:
        """Seasons 1-5 on disk plus an announced, still-empty season 6. Keep-last-2 must
        keep seasons 4 and 5: if the fileless season 6 took rank 1, only season 5 among
        the real seasons would be kept and season 4 would be deleted."""
        seasons = [_season(n) for n in range(1, 6)] + [_season(6, files=0, size=0)]
        plan = plan_series_prune(
            series_title="Show", seasons=seasons, keep_last=2, keep_first_season=False
        )
        assert 4 not in plan.prunable
        assert 5 not in plan.prunable
        assert {2, 3}.issubset(set(plan.prunable))
        assert 6 not in plan.prunable
        assert 6 not in _reasons(plan)


class TestKeepFirstSeason:
    def test_the_first_real_season_is_kept_by_default(self) -> None:
        seasons = [_season(n) for n in range(1, 6)]
        plan = plan_series_prune(series_title="Show", seasons=seasons, keep_last=1)
        assert 1 not in plan.prunable
        assert "first season" in _reasons(plan)[1]

    def test_it_uses_the_lowest_real_season_not_literally_one(self) -> None:
        """A show whose earliest season on disk is 2 keeps season 2 as its first."""
        seasons = [_season(2), _season(3), _season(4)]
        plan = plan_series_prune(series_title="Show", seasons=seasons, keep_last=1)
        assert 2 not in plan.prunable


class TestSpecialsAndIncomplete:
    def test_specials_are_never_pruned(self) -> None:
        seasons = [_season(0), _season(1), _season(2)]
        plan = plan_series_prune(
            series_title="Show", seasons=seasons, keep_last=0, keep_first_season=False
        )
        assert 0 not in plan.prunable
        assert "special" in _reasons(plan)[0].lower()

    def test_a_still_downloading_season_is_left_alone(self) -> None:
        """wanted > on-disk means Sonarr is mid-download. Pruning now makes the two tools
        fight each other."""
        seasons = [_season(1), _season(2, files=3, wanted=8)]
        plan = plan_series_prune(
            series_title="Show", seasons=seasons, keep_last=0, keep_first_season=False
        )
        assert 2 not in plan.prunable
        assert "downloading" in _reasons(plan)[2]


class TestAiring:
    def test_a_currently_airing_season_is_protected(self) -> None:
        seasons = [_season(n) for n in range(1, 5)]
        plan = plan_series_prune(
            series_title="Show",
            seasons=seasons,
            keep_last=0,
            keep_first_season=False,
            airing_seasons={3},
        )
        assert 3 not in plan.prunable
        assert "airing" in _reasons(plan)[3]


class TestSequentialProgression:
    def test_partway_protects_the_current_season_only(self) -> None:
        # Watched to episode 5 of a 10-episode season 3 -> still on 3, not yet reaching 4.
        assert sequential_protections({"alice": {3: 5}}, {3: 10}) == {3}

    def test_finishing_a_season_protects_the_next_one(self) -> None:
        # Completed season 3's last on-disk episode -> ready for 4; 3 is no longer protected here.
        assert sequential_protections({"alice": {3: 10}}, {3: 10}) == {4}

    def test_a_finished_season_with_lookahead_protects_further(self) -> None:
        assert sequential_protections({"alice": {3: 10}}, {3: 10}, lookahead=1) == {4, 5}

    def test_an_unknown_final_episode_falls_back_to_season_level(self) -> None:
        # Sonarr could not supply the season's last episode -> protect both m and m+1.
        assert sequential_protections({"alice": {3: 5}}, {3: None}) == {3, 4}

    def test_an_unknown_watched_position_falls_back_to_season_level(self) -> None:
        # A season with only un-backfilled (NULL-index) rows -> position unknown -> {m, m+1}.
        assert sequential_protections({"alice": {3: None}}, {3: 10}) == {3, 4}

    def test_it_unions_across_viewers(self) -> None:
        # alice is partway on 2 -> {2}; bob finished 5 -> {6}.
        assert sequential_protections({"alice": {2: 3}, "bob": {5: 8}}, {2: 10, 5: 8}) == {2, 6}

    def test_a_mid_binge_season_is_not_deleted(self) -> None:
        """The bug: 'keep last 2' would delete season 3 out from under someone still watching it."""
        seasons = [_season(n) for n in range(1, 7)]  # 1..6
        plan = plan_series_prune(
            series_title="Show",
            seasons=seasons,
            keep_last=2,  # keeps 5, 6
            keep_first_season=False,
            progress_by_user={"alice": {3: 5}},  # partway through season 3
            season_final_episode={3: 10},
        )
        assert 3 not in plan.prunable
        assert "part-way" in _reasons(plan)[3]


class TestKeepRuleConflict:
    def test_a_more_watched_pruned_season_raises_a_conflict(self) -> None:
        """'Season 1 is the only good one': it is old (prunable by rank) but far more
        watched than the recent seasons the rule keeps."""
        seasons = [_season(n) for n in range(1, 5)]  # 1..4
        plan = plan_series_prune(
            series_title="Show",
            seasons=seasons,
            keep_last=2,  # keeps 3, 4
            keep_first_season=False,
            watchers_by_season={1: 40, 2: 5, 3: 2, 4: 1},
        )
        assert 1 in plan.prunable  # the rule would remove it
        assert not plan.auto_approvable  # ...but it refuses to do so unattended
        assert any(c.pruned_season == 1 for c in plan.conflicts)

    def test_equal_watchers_are_not_a_conflict(self) -> None:
        seasons = [_season(n) for n in range(1, 5)]
        plan = plan_series_prune(
            series_title="Show",
            seasons=seasons,
            keep_last=2,
            keep_first_season=False,
            watchers_by_season={1: 0, 2: 0, 3: 0, 4: 0},
        )
        assert plan.auto_approvable
        assert plan.conflicts == []

    def test_a_clean_prune_is_auto_approvable(self) -> None:
        seasons = [_season(n) for n in range(1, 5)]
        plan = plan_series_prune(
            series_title="Show",
            seasons=seasons,
            keep_last=2,
            keep_first_season=True,
            watchers_by_season={1: 10, 2: 0, 3: 8, 4: 9},  # kept seasons are the watched ones
        )
        assert plan.auto_approvable
        assert 2 in plan.prunable  # dormant middle season, cleanly prunable

    def test_the_detector_can_be_switched_off(self) -> None:
        """flag_keep_conflicts=False: the same lopsided watch pattern raises nothing, and
        the plan is auto-approvable -- the owner asked Reaper to follow the rule quietly."""
        seasons = [_season(n) for n in range(1, 5)]
        plan = plan_series_prune(
            series_title="Show",
            seasons=seasons,
            keep_last=2,
            keep_first_season=False,
            flag_keep_conflicts=False,
            watchers_by_season={1: 40, 2: 5, 3: 2, 4: 1},
        )
        assert 1 in plan.prunable
        assert plan.conflicts == []
        assert plan.auto_approvable


NOW = datetime(2026, 7, 17, tzinfo=UTC)


def _days_ago(days: int) -> datetime:
    return NOW - timedelta(days=days)


#: Two viewers mid-show, reused across the expiry cases below.
PROGRESS = {"alice": {3: 5}, "bob": {5: 8}}


class TestInProgressExpiry:
    """active_progress: the hold half of the mid-binge guard. Every edge keeps the viewer."""

    def test_zero_hold_days_never_expires(self) -> None:
        last = {"alice": _days_ago(10_000), "bob": _days_ago(10_000)}
        assert active_progress(PROGRESS, last, now=NOW, hold_days=0) == PROGRESS

    def test_a_stale_viewer_is_dropped_and_a_fresh_one_kept(self) -> None:
        last = {"alice": _days_ago(300), "bob": _days_ago(3)}
        held = active_progress(PROGRESS, last, now=NOW, hold_days=180)
        assert set(held) == {"bob"}

    def test_activity_exactly_at_the_bound_still_holds(self) -> None:
        """>= not >: reduced precision on the boundary resolves toward keeping."""
        last = {"alice": _days_ago(180), "bob": _days_ago(181)}
        held = active_progress(PROGRESS, last, now=NOW, hold_days=180)
        assert set(held) == {"alice"}

    def test_an_unreadable_last_watch_keeps_the_hold(self) -> None:
        """None means "we could not look", which must never read as "they quit"."""
        last: dict[str, datetime | None] = {"alice": None}
        held = active_progress(PROGRESS, last, now=NOW, hold_days=180)
        assert "alice" in held
        # bob is absent from the map entirely -- same unknown, same hold.
        assert "bob" in held

    def test_an_expired_viewer_no_longer_pins_a_season(self) -> None:
        """End to end through the planner: the same mid-binge show prunes season 3 once
        its only viewer's activity is filtered out as abandoned."""
        seasons = [_season(n) for n in range(1, 7)]
        held = active_progress({"alice": {3: 5}}, {"alice": _days_ago(400)}, now=NOW, hold_days=180)
        plan = plan_series_prune(
            series_title="Show",
            seasons=seasons,
            keep_last=2,
            keep_first_season=False,
            progress_by_user=held,
            season_final_episode={3: 10},
        )
        assert 3 in plan.prunable


class TestInProgressToggle:
    def test_switching_the_guard_off_removes_its_protection(self) -> None:
        seasons = [_season(n) for n in range(1, 7)]
        plan = plan_series_prune(
            series_title="Show",
            seasons=seasons,
            keep_last=2,
            keep_first_season=False,
            keep_in_progress=False,
            progress_by_user={"alice": {3: 5}},  # partway through season 3, ignored
            season_final_episode={3: 10},
        )
        assert 3 in plan.prunable

    def test_the_guard_is_on_by_default(self) -> None:
        seasons = [_season(n) for n in range(1, 7)]
        plan = plan_series_prune(
            series_title="Show",
            seasons=seasons,
            keep_last=2,
            keep_first_season=False,
            progress_by_user={"alice": {3: 5}},
            season_final_episode={3: 10},
        )
        assert 3 not in plan.prunable


class TestKeepSpecialsToggle:
    def test_specials_become_prunable_when_allowed(self) -> None:
        seasons = [_season(0), _season(1), _season(2)]
        plan = plan_series_prune(
            series_title="Show",
            seasons=seasons,
            keep_last=0,
            keep_first_season=False,
            keep_specials=False,
        )
        assert 0 in plan.prunable

    def test_specials_never_spend_a_keep_slot_either_way(self) -> None:
        """keep_last=1 with specials allowed to go: the newest REAL season takes the slot,
        specials do not shift the ranking, and idle specials are still prunable."""
        seasons = [_season(0), _season(1), _season(2)]
        plan = plan_series_prune(
            series_title="Show",
            seasons=seasons,
            keep_last=1,
            keep_first_season=False,
            keep_specials=False,
        )
        assert 2 not in plan.prunable  # rank 1, kept by the rule
        assert 0 in plan.prunable

    def test_still_downloading_specials_are_left_alone(self) -> None:
        """Turning keep_specials off surrenders only the specials rule; the airing and
        still-downloading guards still apply to Season 0."""
        seasons = [_season(0, files=3, wanted=8), _season(1)]
        plan = plan_series_prune(
            series_title="Show",
            seasons=seasons,
            keep_last=0,
            keep_first_season=False,
            keep_specials=False,
        )
        assert 0 not in plan.prunable
        assert "downloading" in _reasons(plan)[0]


class TestKeepLastScope:
    def test_keep_last_can_be_switched_off_for_this_show(self) -> None:
        # Under a "requested only" scope, a non-requested show passes apply_keep_last=False,
        # so the last-N floor no longer shields its old seasons.
        seasons = [_season(n) for n in range(1, 5)]  # 1..4
        plan = plan_series_prune(
            series_title="Show",
            seasons=seasons,
            keep_last=2,
            keep_first_season=False,
            apply_keep_last=False,
        )
        assert set(plan.prunable) == {1, 2, 3, 4}  # nothing shielded by keep-last

    def test_keep_last_applies_by_default(self) -> None:
        seasons = [_season(n) for n in range(1, 5)]
        plan = plan_series_prune(
            series_title="Show", seasons=seasons, keep_last=2, keep_first_season=False
        )
        assert 3 not in plan.prunable and 4 not in plan.prunable  # last 2 kept


class TestKeepLastOverCount:
    def test_a_high_keep_last_reads_clearly_for_a_short_show(self) -> None:
        seasons = [_season(n) for n in range(1, 4)]  # 3 seasons
        plan = plan_series_prune(
            series_title="Show", seasons=seasons, keep_last=10, keep_first_season=False
        )
        assert not plan.prunable
        assert "only 3 seasons" in _reasons(plan)[3]
