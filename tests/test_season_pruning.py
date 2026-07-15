# SPDX-License-Identifier: AGPL-3.0-or-later
"""The season-pruning guards.

"Keep the last N seasons" is where TV pruning goes wrong, so each guard has a test that
is really a re-enactment of a bug a shipping competitor has. Every case here resolves
toward keeping a season; the ones that prune are the ones where every guard agreed.
"""

from __future__ import annotations

from reaper.clients.sonarr_stats import SeasonStats
from reaper.services.season_pruning import (
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
    def test_it_protects_the_watched_season_and_the_next(self) -> None:
        assert sequential_protections({"alice": 3}) == {3, 4}

    def test_it_unions_across_viewers(self) -> None:
        assert sequential_protections({"alice": 2, "bob": 5}) == {2, 3, 5, 6}

    def test_a_mid_binge_season_is_not_deleted(self) -> None:
        """The bug: 'keep last 2' would delete season 3 out from under someone who just
        finished it and is about to start 4."""
        seasons = [_season(n) for n in range(1, 7)]  # 1..6
        plan = plan_series_prune(
            series_title="Show",
            seasons=seasons,
            keep_last=2,  # keeps 5, 6
            keep_first_season=False,
            watched_max_by_user={"alice": 3},
        )
        assert 3 not in plan.prunable
        assert 4 not in plan.prunable
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
