# SPDX-License-Identifier: AGPL-3.0-or-later
"""The season-pruning guards.

"Keep the last N seasons" is where TV pruning goes wrong, so each guard has a test that
is really a re-enactment of a bug a shipping competitor has. Every case here resolves
toward keeping a season; the ones that prune are the ones where every guard agreed.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from inspect import signature

import pytest

from reaper.clients.sonarr_stats import SeasonStats
from reaper.engine.gates import GateId, progress_is_establishable
from reaper.engine.policy import (
    GateSetting,
    PolicyBody,
    ProfileSettings,
    SignalSetting,
    inspect,
)
from reaper.engine.signals import SignalId
from reaper.services.season_pruning import (
    UNANSWERABLE_REASONS,
    _because,
    active_progress,
    plan_series_prune,
    sequential_protections,
)

GB = 1024**3

#: ``plan_series_prune`` flags that pick the sentence a hold carries without being a hold
#: themselves, and why each is one. Classified in writing rather than skipped (rule 103),
#: because "holds nothing" is a claim about the arm rather than permission to stop checking
#: it: the reason it names still has to be one ``UNANSWERABLE_REASONS`` knows.
_HOLDS_NOTHING_ALONE = {
    # The show has no Plex rating key anywhere, so no play was queried and no depth of mirror
    # can name a viewer's place. It re-words whichever real cause fired. Holding on it would
    # move every unmatched show off the review queue and onto the Protected page, which #486
    # declined and `test_a_show_plex_never_matched_at_all_is_left_alone` pins (#489).
    "progress_show_unmatched",
}


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
        assert "the earliest season on disk" in _reasons(plan)[1]

    def test_it_uses_the_lowest_real_season_not_literally_one(self) -> None:
        """A show whose earliest season on disk is 2 keeps season 2 as its first."""
        seasons = [_season(2), _season(3), _season(4)]
        plan = plan_series_prune(series_title="Show", seasons=seasons, keep_last=1)
        assert 2 not in plan.prunable


class TestEveryKeepReasonStatesOnlyWhatWasObserved:
    """A kept season's reason is read on a panel the operator can check against Sonarr,
    so a reason that overstates its evidence gets caught being wrong and costs the panel
    its credibility on the rows that matter (rule 21).

    Three of these reasons named what the observation is *usually* a sign of rather than
    the observation: a download in progress, a season on the air, the pilot. Each is
    routinely false, and each case below is the ordinary shape rather than an edge.
    """

    def test_a_permanently_short_season_is_not_called_a_download(self) -> None:
        """``is_incomplete`` is ``wanted > on-disk``, which ``clients.sonarr_stats``
        documents as download *intent*, not a live queue. An ended show permanently
        missing one aired episode is indistinguishable from one mid-download here, and it
        is the case the operator's off switch exists for, so it would have read "still
        downloading" forever."""
        seasons = [_season(1), _season(2, files=9, wanted=10)]
        plan = plan_series_prune(
            series_title="Show", seasons=seasons, keep_last=0, keep_first_season=False
        )
        reason = _reasons(plan)[2]
        assert 2 not in plan.prunable
        assert "download" not in reason
        assert reason == "episodes are missing from this season"

    def test_a_show_between_seasons_is_not_called_currently_airing(self) -> None:
        """``season_scan.airing_seasons`` returns the season to *treat as* airing: the
        newest content-bearing season of any series Sonarr still calls running. A
        continuing show in the gap between seasons is the ordinary state, not an edge, and
        nothing about it is on the air."""
        seasons = [_season(n) for n in range(1, 4)]
        plan = plan_series_prune(
            series_title="Show",
            seasons=seasons,
            keep_last=0,
            keep_first_season=False,
            airing_seasons={3},
        )
        reason = _reasons(plan)[3]
        assert 3 not in plan.prunable
        assert "airing" not in reason
        assert reason == "the newest season of a show that is still running"

    def test_the_earliest_season_on_disk_is_not_called_the_first(self) -> None:
        """``first_real`` is the minimum over seasons that HAVE files. Where season 1 was
        never downloaded or was deleted by hand, season 2 wore the reason -- and keeping
        season 2 does not let anyone start the show, which is the whole justification the
        sentence offered."""
        seasons = [_season(2), _season(3), _season(4)]
        plan = plan_series_prune(series_title="Show", seasons=seasons, keep_last=1)
        reason = _reasons(plan)[2]
        assert 2 not in plan.prunable
        assert "first season" not in reason
        assert reason == "the earliest season on disk, so there is somewhere to start"

    def test_the_mid_binge_clause_stays_about_the_show(self) -> None:
        """``_because`` restates a reason for the conflict message, it never sharpens it.
        It used to rewrite "part-way through the show" as "part-way through *it*", moving
        the claim onto one season -- and ``sequential_protections`` protects the untouched
        NEXT season too, so the same sentence could say nobody had played it while
        claiming someone was midway through it."""
        assert _because("a viewer is part-way through the show") == (
            "a viewer is part-way through the show"
        )

    def test_every_reason_this_module_produces_has_a_because_clause(self) -> None:
        """``_protection_reason`` and ``_because`` are one closed vocabulary, and a reword
        landing on only one side degrades every conflict message to the generic clause
        with nothing failing. So drive the real producer over every branch that reaches
        ``_because`` and check each is still recognized (rule 119).

        Specials are the one reason with no clause of its own: ``_detect_conflicts``
        excludes Season 0 from both sides, so it can never be the kept season in a message.
        """
        plans = [
            # Missing episodes, a mid-binge hold, and the newest season of a running show.
            plan_series_prune(
                series_title="Show",
                seasons=[_season(0), _season(1, files=9, wanted=10), _season(2), _season(3)],
                keep_last=1,
                airing_seasons={3},
                progress_by_user={"someone": {2: 4}},
            ),
            # The earliest season on disk, and the keep-last rank clause beside it.
            plan_series_prune(
                series_title="Show",
                seasons=[_season(2), _season(3), _season(4)],
                keep_last=2,
            ),
            # The whole-show variant of keep-last: the rule reaches further than the disk.
            plan_series_prune(
                series_title="Show",
                seasons=[_season(1), _season(2)],
                keep_last=3,
                keep_first_season=False,
            ),
        ]
        produced = {r for plan in plans for r in _reasons(plan).values()}
        produced -= {"specials are never auto-pruned"}
        for reason in produced:
            assert _because(reason) != "your season rule keeps it", reason
        # And the fixtures really did reach every clause, so a branch losing its parser
        # cannot hide behind a thin set. Counted on the clauses, not the reasons: the
        # keep-last one carries the rank, so it is several strings and one clause.
        assert len({_because(r) for r in produced}) == 5, sorted(produced)


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
        assert "episodes are missing" in _reasons(plan)[2]


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
        assert "still running" in _reasons(plan)[3]


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

    def test_finishing_a_season_advances_over_a_hole_to_the_next_real_one(self) -> None:
        """Rule 124: the anchored position must be a season that exists.

        Seasons are not always a contiguous run -- Sonarr never filled one, someone deleted
        one by hand, or Reaper pruned one on an earlier run. Advancing to `anchor + 1`
        blindly pinned the hold on a number nothing holds, so the viewer got no protection
        at all and season 5, the one they are about to watch, stayed prunable. With the
        default lookahead of 0 nothing else covers it, and each prune widens the hole that
        hides the next one.
        """
        assert sequential_protections({"alice": {3: 10}}, {3: 10}, on_disk={3, 5}) == {5}

    def test_finishing_the_last_season_protects_nothing(self) -> None:
        """Someone who finished the show is not mid-binge, so the guard holds nothing.

        Unchanged in effect from before the hole fix: `anchor + 1` simply matched no season.
        Stated as its own case so advancing-over-a-hole cannot quietly grow into protecting
        the finale of every completed show.
        """
        assert sequential_protections({"alice": {3: 10}}, {3: 10}, on_disk={1, 2, 3}) == set()

    def test_an_unknown_position_still_holds_the_anchor_over_a_hole(self) -> None:
        """The fail-closed branch keeps the anchor and adds the next REAL season, not m+1."""
        assert sequential_protections({"alice": {3: None}}, {3: 10}, on_disk={3, 7}) == {3, 7}

    def test_it_unions_across_viewers(self) -> None:
        # alice is partway on 2 -> {2}; bob finished 5 -> {6}.
        assert sequential_protections({"alice": {2: 3}, "bob": {5: 8}}, {2: 10, 5: 8}) == {2, 6}

    def test_a_rewatcher_is_anchored_where_they_actually_are(self) -> None:
        """The bug this rule exists for: someone who finished the show and started again.

        By season NUMBER they are anchored on the finale, judged ready for a season that
        does not exist, and protected nowhere -- so the season they are working through
        today is prunable, and the conflict detector does not catch it either (every
        season shares the same all-time watcher count). By TIME they are exactly where
        they are.
        """
        progress = {"alice": {1: 10, 2: 3, 3: 10, 4: 10, 5: 10, 6: 10}}
        finals = dict.fromkeys(range(1, 7), 10)
        times = {
            "alice": {
                1: datetime(2024, 1, 1, tzinfo=UTC),
                2: datetime(2026, 7, 20, tzinfo=UTC),  # today: they are on season 2
                3: datetime(2024, 3, 1, tzinfo=UTC),
                4: datetime(2024, 4, 1, tzinfo=UTC),
                5: datetime(2024, 5, 1, tzinfo=UTC),
                6: datetime(2024, 6, 1, tzinfo=UTC),
            }
        }

        assert 2 in sequential_protections(progress, finals, last_play_by_user=times)

    def test_the_number_anchor_survives_a_dip_into_an_old_season(self) -> None:
        """Recency alone is not enough either: someone mid-binge on the newest season who
        watches one old episode today must not lose the hold on the season they are on."""
        progress = {"alice": {2: 10, 6: 4}}  # finished 2 long ago, part-way through 6
        finals = {2: 10, 6: 10}
        times = {
            "alice": {
                2: datetime(2026, 7, 20, tzinfo=UTC),  # dipped back in today
                6: datetime(2026, 7, 18, tzinfo=UTC),
            }
        }

        protected = sequential_protections(progress, finals, last_play_by_user=times)

        assert 6 in protected  # still their binge
        assert 3 in protected  # and they are ready for what follows the one they re-watched

    def test_no_readable_times_keeps_exactly_the_old_anchor(self) -> None:
        """Unreadable is not evidence that they are somewhere else."""
        progress = {"alice": {3: 5}}
        assert sequential_protections(progress, {3: 10}, last_play_by_user={}) == {3}
        assert sequential_protections(
            progress, {3: 10}, last_play_by_user={"alice": {3: None}}
        ) == {3}

    def test_specials_anchor_only_when_they_can_be_pruned(self) -> None:
        """A viewer part-way through the specials gets no hold at all unless the operator
        turned keep_specials off, which is the one setting that can remove them."""
        progress = {"alice": {0: 3}}
        finals = {0: 10}

        assert sequential_protections(progress, finals) == set()
        assert sequential_protections(progress, finals, include_specials=True) == {0}

    def test_finishing_the_specials_does_not_protect_season_one(self) -> None:
        """Season 1 is not "the next special". Specials are not a sequence."""
        assert sequential_protections({"a": {0: 10}}, {0: 10}, include_specials=True) == set()

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

    def test_an_unmeasured_kept_season_holds_the_prune_without_inventing_a_number(
        self,
    ) -> None:
        """A season on disk that Plex never resolved has no watch history to read, and
        neither reading it as 0 nor skipping it is right.

        Reading it as 0 turned "we could not measure it" into "we measured it and nobody
        watched", so the operator was told in plain words that N people watched one season
        more than another, against a number that was never taken. Skipping it, which
        replaced that, threw away the hold along with the bad sentence: a well-watched
        prunable season became auto-approvable purely because the season it would have
        been measured against could not be read. That is unreadable evidence clearing a
        protection (rule 93).

        So the hold stays and only the arithmetic goes. Here season 3 is the unmeasured
        one the rule keeps.
        """
        seasons = [_season(n) for n in range(1, 5)]  # 1..4
        plan = plan_series_prune(
            series_title="Show",
            seasons=seasons,
            keep_last=2,  # keeps 3, 4
            keep_first_season=False,
            watchers_by_season={1: 40, 2: 5, 3: None, 4: 41},
        )

        assert 1 in plan.prunable  # the rule would remove it
        assert not plan.auto_approvable  # ...but not unattended, because 3 is unreadable

        # Season 4 was measured and out-watches both prunable seasons, so it raises
        # nothing. Each watched prunable season fires against the unreadable 3.
        assert [(c.pruned_season, c.kept_season) for c in plan.conflicts] == [(1, 3), (2, 3)]
        conflict = plan.conflicts[0]
        assert conflict.kept_watchers is None
        assert "could not check" in conflict.message
        # And it never asserts the comparison it could not make. That phrase is the whole
        # bug: it read as a measured fact about a number nobody ever took.
        assert "more than watched" not in conflict.message

    def test_zero_still_means_measured_and_unwatched(self) -> None:
        """The three-state map must not collapse "nobody watched it" into "unmeasured":
        a resolved season nobody watched is exactly what the detector compares against."""
        seasons = [_season(n) for n in range(1, 5)]
        plan = plan_series_prune(
            series_title="Show",
            seasons=seasons,
            keep_last=2,  # keeps 3, 4
            keep_first_season=False,
            watchers_by_season={1: 40, 2: 5, 3: 0, 4: 0},
        )

        assert any(c.pruned_season == 1 and c.kept_season == 3 for c in plan.conflicts)

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

    def test_the_message_names_a_still_downloading_kept_season(self) -> None:
        """The real case behind this: keep-last off, keep-first off, so the only thing
        protecting Season 4 is that Sonarr has not finished downloading it. An older,
        more-watched season conflicts with it, and the message must name THAT reason --
        not a vague 'your keep rule protects'."""
        seasons = [_season(n) for n in range(1, 4)] + [_season(4, wanted=10, files=5)]
        plan = plan_series_prune(
            series_title="Show",
            seasons=seasons,
            keep_last=0,
            keep_first_season=False,
            watchers_by_season={1: 0, 2: 0, 3: 4, 4: 3},
        )
        assert "episodes are missing" in _reasons(plan)[4]  # season 4 kept only for that
        conflict = next(c for c in plan.conflicts if c.pruned_season == 3)
        assert conflict.kept_season == 4
        assert "episodes are missing from it" in conflict.message
        assert "which your keep rule protects" not in conflict.message

    def test_the_message_names_a_keep_last_kept_season(self) -> None:
        """When the kept season is held by keep-last, the message says so in plain words."""
        seasons = [_season(n) for n in range(1, 5)]
        plan = plan_series_prune(
            series_title="Show",
            seasons=seasons,
            keep_last=2,  # keeps 3, 4
            keep_first_season=False,
            watchers_by_season={1: 40, 2: 5, 3: 2, 4: 1},
        )
        conflict = next(c for c in plan.conflicts if c.pruned_season == 1)
        assert "one of the newest seasons your rule keeps" in conflict.message


#: What ``engine.gates.lifetime_shortfall`` hands the planner for a season that arrived
#: before the mirror did. Its exact wording is that function's business; what matters here is
#: that a season carries one at all.
SHORT = "your watch history only goes back 12 months"


class TestATruncatedMirrorCannotClearTheConflict:
    """Both counts the detector compares are all-time, and ``watch_event`` begins at the
    mirror's horizon, so a season that arrived before it reports a LOWER BOUND.

    The truncation is not evenly spread. It falls on exactly the old seasons keep-last wants
    to prune, whose plays are the ones behind the horizon, so an unqualified comparison is
    biased against the seasons the detector exists to protect.
    """

    def test_a_truncated_mirror_reaches_the_same_decision_as_a_full_one(self) -> None:
        """The filed bug, driven both ways. A show added five years ago whose Season 1 had
        five viewers four years back, seen through a Tautulli installed a year ago: every
        one of those plays is behind the horizon, so the count reads 0 and cannot out-rank
        anything. The show went from "Needs a look" to auto-approvable and its older seasons
        became removable on score alone.

        The two mirrors are the SAME library. Only the evidence differs, so the decision may
        not: whatever the mirror can see, Reaper must not conclude more than it saw.
        """
        seasons = [_season(n) for n in (1, 2, 3)]
        full = plan_series_prune(
            series_title="Show",
            seasons=seasons,
            keep_last=1,  # keeps 3
            # The non-default, needed to make Season 1 prunable at all and reproduce the
            # filed transcript.
            keep_first_season=False,
            watchers_by_season={1: 5, 2: 3, 3: 1},
        )
        truncated = plan_series_prune(
            series_title="Show",
            seasons=seasons,
            keep_last=1,
            keep_first_season=False,
            # The same library through a mirror that starts after all three arrived.
            watchers_by_season={1: 0, 2: 0, 3: 1},
            shortfall_by_season={1: SHORT, 2: SHORT, 3: SHORT},
        )

        pairs = [(1, 3), (2, 3)]
        assert [(c.pruned_season, c.kept_season) for c in full.conflicts] == pairs
        assert [(c.pruned_season, c.kept_season) for c in truncated.conflicts] == pairs
        assert full.auto_approvable is False
        assert truncated.auto_approvable is False

    def test_marking_the_truncated_count_unreadable_would_not_have_done_it(self) -> None:
        """The trap this fix had to get past, pinned so a later simplification cannot walk
        back into it.

        ``None`` on the pruned side means "nobody could measure this" and takes the same
        skip a 0 takes, so routing a truncated count to ``None`` changes nothing: same
        branch, same skip, same lost hold. The hold has to come from the reach arm, which is
        why an unreadable count and an unsupported one are kept apart.
        """
        seasons = [_season(n) for n in (1, 2, 3)]
        unreadable = plan_series_prune(
            series_title="Show",
            seasons=seasons,
            keep_last=1,
            keep_first_season=False,
            watchers_by_season={1: None, 2: None, 3: 1},
        )
        assert unreadable.conflicts == []
        assert unreadable.auto_approvable is True  # ...which is exactly why it is not the fix

    def test_a_zero_the_mirror_cannot_support_is_not_an_unwatched_season(self) -> None:
        """The load-bearing arm. A 0 read over a mirror that covers the season's whole life
        is a measurement; the same 0 read over a shorter one is silence."""
        seasons = [_season(n) for n in range(1, 5)]
        common = {
            "series_title": "Show",
            "seasons": seasons,
            "keep_last": 2,  # keeps 3, 4
            "keep_first_season": False,
            "watchers_by_season": {1: 0, 2: 0, 3: 1, 4: 1},
        }
        answered = plan_series_prune(**common)  # type: ignore[arg-type]
        assert answered.conflicts == []  # measured, nobody watched: nothing to flag

        unsupported = plan_series_prune(**common, shortfall_by_season={1: SHORT})  # type: ignore[arg-type]
        assert [(c.pruned_season, c.kept_season) for c in unsupported.conflicts] == [(1, 3), (1, 4)]
        # Season 2's own count IS supported, so its 0 still clears. The bound is applied per
        # season, not per show: a season backfilled into an old show arrived recently.
        assert all(c.pruned_season != 2 for c in unsupported.conflicts)

    def test_an_answered_count_still_clears_against_a_truncated_kept_season(self) -> None:
        """The other direction, and it must NOT hold: more history can only ever raise the
        kept count, so a pruned count already below it stays below it. An outcome the bound
        already earns needs no reach at all (``fields._survives_more_history``).

        The pruned count is 1 rather than 0 deliberately (rule 118). A 0 takes the
        ``pruned_watchers == 0`` skip *before* the kept loop is entered, so the earlier
        version of this test proved nothing about the arm it named: ``for kept in
        kept_seasons`` never executed, and the empty list came from the skip. Now the pair
        reaches the loop and the empty list comes from the ``else: continue`` this is about
        -- which is what makes the blanket-hold mutation fail here (see the sibling test).
        """
        plan = plan_series_prune(
            series_title="Show",
            seasons=[_season(n) for n in range(1, 5)],
            keep_last=2,  # keeps 3, 4
            keep_first_season=False,
            watchers_by_season={1: 1, 2: 0, 3: 3, 4: 3},
            shortfall_by_season={3: SHORT, 4: SHORT},  # the KEPT seasons are the truncated ones
        )
        assert plan.conflicts == []
        assert plan.auto_approvable is True

    def test_a_short_mirror_holds_every_prunable_season_of_an_old_show(self) -> None:
        """The blanket effect, pinned because it is large and was once denied in writing.

        Where the mirror does not reach back to when ANY prunable season arrived, every one
        of them conflicts against every kept season regardless of the counts: each count is
        a lower bound and more history can always lift it above the others. Nothing is
        auto-approvable, so TV pruning is inert on such a show until the mirror catches up
        or a human decides -- and because every conflict carries ``shortfall``,
        ``season_scan.guard_result`` marks all of them as comparisons it did not make, which
        is what the operator's chip says on the card.

        This is the prime directive's answer, not a defect -- but the docstring of
        ``_detect_conflicts`` once claimed the detector did *not* degenerate this way, and
        the mutation that makes the degeneration total (``elif pruned_shortfall is not None
        or kept_shortfall is not None``) passed the entire 2626-test suite. Rule 118: the
        behavior a reader is most likely to try to "simplify" needs a test that fails.
        """
        plan = plan_series_prune(
            series_title="Show",
            seasons=[_season(n) for n in range(1, 6)],
            keep_last=2,  # keeps 4, 5
            keep_first_season=False,
            # Nobody read as watching anything, and not one count the mirror can support.
            watchers_by_season=dict.fromkeys(range(1, 6), 0),
            shortfall_by_season=dict.fromkeys(range(1, 6), SHORT),
        )
        assert plan.prunable == [1, 2, 3]
        # Every prunable season, against every kept season. Not a subset.
        assert [(c.pruned_season, c.kept_season) for c in plan.conflicts] == [
            (1, 4),
            (1, 5),
            (2, 4),
            (2, 5),
            (3, 4),
            (3, 5),
        ]
        assert plan.auto_approvable is False
        # All of them carry the reason, so none of them is reported as a comparison made.
        assert all(c.shortfall == SHORT for c in plan.conflicts)
        # And no message asserts a count off a mirror that cannot support one.
        assert not any("0 people watched" in c.message for c in plan.conflicts)

    def test_the_policy_page_now_speaks_for_the_hold_this_test_pins(self) -> None:
        """The other half of #224, tied to this test so neither can drift alone.

        The sibling above proves the scan holds every prunable season of a show older than the
        mirror. That was true and the policy editor said NOTHING about it, which is the whole
        of #224: an operator saw an empty automatic lane, no warning, and no way to learn their
        watch history was the cause.

        ``policy.inspect``'s warning asserts a behavior of THIS module, across a module
        boundary, so it is the shape rule 144 warns about: a sentence that reads as
        demonstrably correct while vouching for a consistency nothing checks. If
        ``_detect_conflicts`` ever stops degenerating this way, the sibling above goes red and
        so does this, rather than leaving a warning that quietly became a lie. That is why the
        pin lives here beside the behavior and not only in ``test_policy``.
        """
        plan = plan_series_prune(
            series_title="Show",
            seasons=[_season(n) for n in range(1, 6)],
            keep_last=2,
            keep_first_season=False,
            watchers_by_season=dict.fromkeys(range(1, 6), 0),
            shortfall_by_season=dict.fromkeys(range(1, 6), SHORT),
        )
        # The state the warning describes: nothing removable without a human.
        assert plan.auto_approvable is False
        assert plan.prunable and not any(
            p not in {c.pruned_season for c in plan.conflicts} for p in plan.prunable
        )

        # And the editor says so, on a TV policy whose reach clears the dormancy floor so every
        # other member of the family is correctly silent.
        body = PolicyBody(
            media_type="tv",
            condemn_at=70,
            gates=(GateSetting(gate=GateId.MIN_DORMANCY, threshold=30),),
            signals=(SignalSetting(signal=SignalId.UNWATCHED, weight=100, saturate_at=730),),
            # Spanned by the 90-day reach below, so the mid-binge guard is establishable. At
            # the shipped 180 that guard holds every season on disk instead, which drains
            # `prunable` and correctly silences the warning under test.
            in_progress_hold_days=30,
        )
        spoken = [w.field for w in inspect(body, ProfileSettings(), history_reach_days=90.0)]
        assert "flag_keep_conflicts" in spoken

    def test_a_truncated_count_that_already_out_ranks_an_answered_one_still_compares(
        self,
    ) -> None:
        """The same "already earned" reading on the losing side: the pruned count is a lower
        bound, more history can only raise it, and it ALREADY beats a kept count the mirror
        supports. That is a comparison Reaper really made, so it keeps the comparison wording
        and stays the operator's call to overrule."""
        plan = plan_series_prune(
            series_title="Show",
            seasons=[_season(n) for n in range(1, 5)],
            keep_last=2,  # keeps 3, 4
            keep_first_season=False,
            watchers_by_season={1: 40, 2: 0, 3: 1, 4: 1},
            shortfall_by_season={1: SHORT},  # the winning count is the truncated one
        )
        conflict = next(c for c in plan.conflicts if c.pruned_season == 1)
        assert conflict.shortfall is None
        assert "more than watched Season 3" in conflict.message

    def test_a_truncated_kept_count_stops_asserting_arithmetic_it_cannot_take(self) -> None:
        """The rule lost, but only against a lower bound: more history could lift the kept
        season back above the pruned one. The season is held either way, so what changes is
        the sentence. Reaper must not tell an operator deciding what to delete that one
        season was watched "more than" another off a number that can still move."""
        plan = plan_series_prune(
            series_title="Show",
            seasons=[_season(n) for n in range(1, 5)],
            keep_last=2,  # keeps 3, 4
            keep_first_season=False,
            watchers_by_season={1: 9, 2: 0, 3: 2, 4: 20},
            shortfall_by_season={3: SHORT},  # kept Season 3 predates the mirror
        )
        conflict = next(c for c in plan.conflicts if c.pruned_season == 1 and c.kept_season == 3)
        assert conflict.shortfall == SHORT
        assert "more than watched" not in conflict.message
        assert "Reaper cannot tell whether Season 1 is watched more than Season 3" in (
            conflict.message
        )
        # Season 4 out-watches Season 1 on counts the mirror supports, so it raises nothing:
        # the reach is consulted per comparison, not switched on for the whole show.
        assert not [c for c in plan.conflicts if c.pruned_season == 1 and c.kept_season == 4]

    def test_the_message_says_why_and_never_invents_a_count(self) -> None:
        """Operator copy, checked as copy (rule 21): it names both seasons, gives the real
        reason in the words every other reader of a truncated count uses, and states no
        number, because no number was established."""
        plan = plan_series_prune(
            series_title="Show",
            seasons=[_season(n) for n in (1, 2, 3)],
            keep_last=1,
            keep_first_season=False,
            watchers_by_season={1: 0, 2: 0, 3: 1},
            shortfall_by_season={1: SHORT, 2: SHORT, 3: SHORT},
        )
        message = plan.conflicts[0].message
        assert message == (
            "Reaper cannot tell whether Season 1 is watched more than Season 3, since your "
            "watch history only goes back 12 months. Season 3 is kept because it is one of "
            "the newest seasons your rule keeps. Left for you to decide instead of removing "
            "it."
        )
        # Rule 21: no em dashes in operator copy. Escaped rather than written literally, so
        # the assertion does not itself smuggle the character ruff bans (RUF001).
        assert "\u2014" not in message and "\u2013" not in message

    def test_every_conflict_shape_invites_the_decision_the_engine_now_honors(self) -> None:
        """The closing phrase and the reap must agree, in whichever direction they point.

        This was briefly split. While a blocked gate still held a hand reap, the two refused
        shapes ended "Kept for now" instead, because inviting a decision the engine would
        refuse is rule 92's failure pointed at operator copy: an operator who acts on "Left
        for you to decide" got the reap declined and a generic "a protection couldn't be
        checked" in place of the sentence they acted on. ``engine.verdict`` no longer works
        that way, so all three shapes are the operator's to settle and the split would now be
        the misleading half. Swept over all three rather than asserted on one, because the
        failure mode is one shape drifting away from the others.

        What does NOT depend on the reversal, and is asserted beside it: no refused shape
        states a watcher count it cannot stand behind. That was the other half of the same
        fix and it survives the reap changing hands.
        """
        common = {
            "series_title": "Show",
            "seasons": [_season(n) for n in (1, 2, 3)],
            "keep_last": 1,
            "keep_first_season": False,
        }
        unsupported = plan_series_prune(
            **common,  # type: ignore[arg-type]
            watchers_by_season={1: 0, 2: 0, 3: 1},
            shortfall_by_season={1: SHORT, 2: SHORT, 3: SHORT},
        )
        unreadable = plan_series_prune(
            **common,  # type: ignore[arg-type]
            watchers_by_season={1: 4, 2: 0, 3: None},
        )
        settleable = plan_series_prune(
            **common,  # type: ignore[arg-type]
            watchers_by_season={1: 9, 2: 0, 3: 1},
        )
        for plan in (unsupported, unreadable, settleable):
            message = plan.conflicts[0].message
            assert message.endswith("Left for you to decide instead of removing it.")
            assert "Kept for now" not in message
        # The two Reaper could not settle still assert no arithmetic. The unsupported one
        # cannot stand behind its own count; the unreadable one cannot stand behind the
        # kept season's.
        assert "0 people watched" not in unsupported.conflicts[0].message
        assert "more than watched" not in unreadable.conflicts[0].message
        # ...while the comparison that WAS made still states it.
        assert "more than watched Season 3" in settleable.conflicts[0].message

    def test_an_unreadable_kept_count_never_prints_a_bound_as_a_measurement(self) -> None:
        """The pruned season's own shortfall rides on the conflict even when what could not
        be read is the KEPT count, because the message is chosen off it.

        Without it this arm printed "0 people watched Season 1" for a count the same call
        had just ruled unsupportable, one line from a chip saying Reaper could not check who
        watched these seasons. The hold does not move (both shapes refuse); the sentence
        does. Found independently by all three review lanes.
        """
        plan = plan_series_prune(
            series_title="Show",
            seasons=[_season(n) for n in (1, 2, 3)],
            keep_last=1,  # keeps 3, which Plex has not resolved
            keep_first_season=False,
            watchers_by_season={1: 0, 2: 0, 3: None},
            shortfall_by_season={1: SHORT, 2: SHORT},
        )
        conflict = next(c for c in plan.conflicts if c.pruned_season == 1)
        assert conflict.kept_watchers is None  # the kept count really is the unreadable one
        assert conflict.shortfall == SHORT  # ...and the pruned bound still reached the message
        assert "0 people watched" not in conflict.message
        assert "Reaper cannot tell whether Season 1" in conflict.message

    def test_the_off_switch_silences_the_reach_arm_too(self) -> None:
        """``flag_keep_conflicts`` off means the operator asked Reaper to follow the keep
        rule quietly. The new arm is part of that detector, not a second one behind it."""
        plan = plan_series_prune(
            series_title="Show",
            seasons=[_season(n) for n in (1, 2, 3)],
            keep_last=1,
            keep_first_season=False,
            flag_keep_conflicts=False,
            watchers_by_season={1: 0, 2: 0, 3: 1},
            shortfall_by_season={1: SHORT, 2: SHORT, 3: SHORT},
        )
        assert plan.conflicts == []
        assert plan.auto_approvable is True


class TestIncompleteSeasonProtection:
    def test_an_incomplete_season_is_protected_by_default(self) -> None:
        """Sonarr still wants an episode it does not have -> kept, so a removal never
        fights an in-progress download."""
        seasons = [_season(1), _season(2, wanted=10, files=5)]
        plan = plan_series_prune(
            series_title="Show", seasons=seasons, keep_last=0, keep_first_season=False
        )
        assert 2 not in plan.prunable
        assert "episodes are missing" in _reasons(plan)[2]

    def test_the_protection_can_be_switched_off(self) -> None:
        """protect_incomplete=False: an ended show Sonarr permanently lists as missing an
        episode is judged like any other season, so its stale-incomplete season is prunable."""
        seasons = [_season(1), _season(2, wanted=10, files=5)]
        plan = plan_series_prune(
            series_title="Show",
            seasons=seasons,
            keep_last=0,
            keep_first_season=False,
            protect_incomplete=False,
        )
        assert 2 in plan.prunable
        assert 2 not in _reasons(plan)  # no longer a protected season


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


class TestTheMirrorMustSpanTheHold:
    """progress_is_establishable: the guard's claim is only as good as the mirror under it.

    ``in_progress_hold_days`` is not a bound on the watch mirror, it is the span the guard
    claims to cover, so a mirror shallower than it leaves viewers the guard cannot see --
    and a viewer with no rows is indistinguishable from no viewer at all (rules 93 and 140).
    """

    @pytest.mark.parametrize(
        ("reach_days", "hold_days", "establishable"),
        [
            (400, 180, True),  # the mirror covers the whole hold window
            (180, 180, True),  # exactly spanning is spanning
            (179, 180, False),  # one day short is short
            (90, 180, False),  # the shallow mirror from the issue
            (0, 30, False),  # an empty mirror establishes nothing
            (400, 0, False),  # a hold that never expires...
            (10_000, 0, False),  # ...which no reach covers, however deep
            # A one-day hold is the shortest real one (the policy field is `ge=0`), and the
            # only case that separates "the hold never expires" from "the hold is short".
            # Without it the zero test alone pins the guard, and `hold_days <= 0` is free to
            # become `<= 1`, which stops establishing a hold an operator really can set.
            (400, 1, True),
            (0, 1, False),  # ...but no mirror at all still spans nothing
        ],
    )
    def test_the_span_the_guard_claims_is_checked_against_the_reach(
        self, reach_days: int, hold_days: int, establishable: bool
    ) -> None:
        assert (
            progress_is_establishable(reach_days=reach_days, hold_days=hold_days) is establishable
        )

    def test_a_negative_hold_is_not_reachable_so_nothing_here_pins_it(self) -> None:
        """Stated rather than tested, because a test could not fail (rule 118).

        ``hold_days <= 0`` also catches a negative, but ``PolicyBody.in_progress_hold_days``
        is `ge=0`, so no stored policy can produce one. Mutating the ``<=`` to ``==`` is
        therefore invisible to any input the app can reach, and a case built to catch it
        would be asserting on a state the save boundary already refuses. This asserts the
        bound instead, so the claim goes red if the field is ever widened to allow one.
        """
        assert PolicyBody.model_fields["in_progress_hold_days"].metadata[0].ge == 0

    def test_a_viewer_the_mirror_cannot_see_still_holds_the_next_season(self) -> None:
        """The reproduction from the issue: one viewer finished Season 3 120 days ago, under
        the shipped 180-day hold. The two runs are identical but for how far the mirror
        reaches -- and at 90 days it holds none of that viewer's plays, so they contribute no
        rows and the guard is asked a question the history cannot answer."""
        common = {
            "series_title": "Show",
            "seasons": [_season(n) for n in range(1, 7)],
            "keep_last": 2,
            "keep_first_season": False,
            "season_final_episode": {3: 10},
        }

        # 400 days: the play is inside the mirror, and the guard names the season they are up
        # to -- Season 3 is finished, so the hold advances to Season 4.
        deep = plan_series_prune(
            **common,  # type: ignore[arg-type]
            progress_by_user=active_progress(
                {"alice": {3: 10}}, {"alice": _days_ago(120)}, now=NOW, hold_days=180
            ),
            progress_established=progress_is_establishable(reach_days=400, hold_days=180),
        )
        assert deep.prunable == [1, 2, 3]
        assert _reasons(deep)[4] == "a viewer is part-way through the show"

        # 90 days: that same play predates the horizon, so the viewer is simply absent.
        shallow = plan_series_prune(
            **common,  # type: ignore[arg-type]
            progress_by_user={},
            progress_established=progress_is_establishable(reach_days=90, hold_days=180),
        )
        assert shallow.prunable == []
        assert (
            _reasons(shallow)[4]
            == "your watch history is too short to tell who is part-way through"
        )

    def test_an_unbounded_hold_is_never_establishable(self) -> None:
        """0 holds a viewer's place forever, and a viewer whose every play predates the
        horizon is invisible at any reach -- with no expiry to make that harmless."""
        plan = plan_series_prune(
            series_title="Show",
            seasons=[_season(n) for n in range(1, 7)],
            keep_last=2,
            keep_first_season=False,
            progress_by_user={},
            progress_established=progress_is_establishable(reach_days=10_000, hold_days=0),
        )
        assert plan.prunable == []

    def test_a_viewer_the_mirror_can_see_keeps_the_sharper_reason(self) -> None:
        """The reach check is last, so a season we can actually name a viewer for says so.
        The rest of the show gets the honest "we could not tell" instead."""
        plan = plan_series_prune(
            series_title="Show",
            seasons=[_season(n) for n in range(1, 7)],
            keep_last=2,
            keep_first_season=False,
            progress_by_user={"alice": {3: 10}},
            season_final_episode={3: 10},
            progress_established=False,
        )
        reasons = _reasons(plan)
        assert reasons[4] == "a viewer is part-way through the show"
        assert reasons[1] == "your watch history is too short to tell who is part-way through"

    def test_the_guards_off_switch_also_silences_the_reach_check(self) -> None:
        """An operator who turned the guard off is making no claim for a shallow mirror to
        fall short of, so the seasons stay prunable."""
        plan = plan_series_prune(
            series_title="Show",
            seasons=[_season(n) for n in range(1, 7)],
            keep_last=2,
            keep_first_season=False,
            keep_in_progress=False,
            progress_established=False,
        )
        # Season 4 too: with the guard off, nothing holds the season a viewer would be up to.
        assert plan.prunable == [1, 2, 3, 4]

    def test_an_unanswerable_hold_is_marked_unestablishable_and_a_real_one_is_not(self) -> None:
        """The hold carries a typed flag, because the two kinds of keep are not the same.

        A season kept because a protection *fired* is a definite keep. A season kept because
        the guard could not be ANSWERED is Unknown (rule 93), and only the second may hold a
        hand reap -- `season_scan.guard_result` reads this flag to mark the result blocked.
        Pinned on a plan carrying both, so a fix that flags every protected season fails.
        """
        plan = plan_series_prune(
            series_title="Show",
            seasons=[_season(n) for n in range(1, 7)],
            keep_last=2,
            keep_first_season=False,
            progress_by_user={"alice": {3: 10}},
            season_final_episode={3: 10},
            progress_established=False,
        )
        flags = {p.season_number: p.unestablishable for p in plan.protected}
        # Season 4 is held by a viewer we can actually see: a definite keep, not an unknown.
        assert flags[4] is False
        # Seasons 1 and 2 are the blanket hold, and 5/6 are the keep-last floor.
        assert flags[1] is True
        assert flags[2] is True


class TestASeasonWithNoPlexKeyHidesItsViewer:
    """``progress_seasons_unmatched``: the third route to an unanswerable mid-binge guard.

    The mirror can span the hold perfectly and every play still be readable, and the guard
    still have no answer -- because a season with no Plex rating key was never *asked* about.
    Its plays sit under a key the scan never learned, so its viewer is absent from
    ``progress_by_user`` the same way a viewer beyond the horizon is, and for the same reason
    the absence must not read as "nobody is part-way through" (rules 93, 140).
    """

    def test_an_unmatched_season_holds_the_seasons_that_did_resolve(self) -> None:
        """The counterfactual pair, at this module's own interface. A viewer finished Season 3
        and is about to start Season 4; with Season 3 resolved the guard names Season 4, and
        with it unresolved the viewer is invisible and Season 4 is offered for reaping."""
        common = {
            "series_title": "Show",
            "seasons": [_season(n) for n in range(1, 7)],
            "keep_last": 2,
            "keep_first_season": False,
            "season_final_episode": {3: 10},
        }
        seen = plan_series_prune(
            **common,  # type: ignore[arg-type]
            progress_by_user={"alice": {3: 10}},
        )
        assert _reasons(seen)[4] == "a viewer is part-way through the show"
        assert seen.prunable == [1, 2, 3]

        hidden = plan_series_prune(
            **common,  # type: ignore[arg-type]
            progress_by_user={},  # her plays are under a key this scan never learned
            progress_seasons_unmatched=True,
        )
        assert hidden.prunable == []
        assert (
            _reasons(hidden)[4]
            == "a season of this show is not matched in Plex, so who is part-way through is "
            "unknown"
        )

    def test_the_hold_is_marked_unanswerable_and_a_visible_viewer_is_not(self) -> None:
        """Rule 93's encoding: a check that could not be ANSWERED is blocked, and a
        protection that fired stays a definite keep. Pinned on a plan carrying both."""
        plan = plan_series_prune(
            series_title="Show",
            seasons=[_season(n) for n in range(1, 7)],
            keep_last=2,
            keep_first_season=False,
            progress_by_user={"alice": {3: 10}},
            season_final_episode={3: 10},
            progress_seasons_unmatched=True,
        )
        flags = {p.season_number: p.unestablishable for p in plan.protected}
        assert flags[4] is False  # a viewer we can see: definite
        assert flags[1] is True and flags[2] is True  # the blanket hold

    def test_the_guards_off_switch_also_silences_it(self) -> None:
        """The same off-switch its two siblings honor: an operator who turned the mid-binge
        guard off is making no claim for an unmatched season to undermine."""
        plan = plan_series_prune(
            series_title="Show",
            seasons=[_season(n) for n in range(1, 7)],
            keep_last=2,
            keep_first_season=False,
            keep_in_progress=False,
            progress_seasons_unmatched=True,
        )
        assert plan.prunable == [1, 2, 3, 4]

    def test_a_show_that_never_bound_is_not_blamed_on_the_mirror(self) -> None:
        """#489. A show with no Plex rating key anywhere held every prunable season with "your
        watch history is too short to tell who is part-way through" -- true, and the one
        remedy that cannot work: with no address, nobody's place is readable at any depth.

        It still holds exactly the same seasons. Only the sentence moves, which is the whole
        change: this arm is reporting, not protecting.
        """
        common = {
            "series_title": "Show",
            "seasons": [_season(n) for n in range(1, 7)],
            "keep_last": 2,
            "keep_first_season": False,
        }
        unbound = plan_series_prune(
            **common,  # type: ignore[arg-type]
            progress_established=False,
            progress_show_unmatched=True,
        )
        assert _reasons(unbound)[1] == (
            "this show is not matched in Plex, so who is part-way through is unknown"
        )
        # The premise: without the show-level fact the same call names the mirror, so the
        # assertion above is reading the new arm and not a coincidence.
        assert _reasons(
            plan_series_prune(**common, progress_established=False)  # type: ignore[arg-type]
        )[1] == ("your watch history is too short to tell who is part-way through")
        # And it holds the same seasons either way.
        assert unbound.prunable == []

    def test_the_wider_failures_are_named_first(self) -> None:
        """All three unanswerable causes at once. The reason shown is the widest, because its
        remedy is the one that fixes the others as a side effect -- and copy naming the
        narrowest would send the operator to inspect one season when their whole mirror is
        too short."""
        common = {
            "series_title": "Show",
            "seasons": [_season(n) for n in range(1, 7)],
            "keep_last": 2,
            "keep_first_season": False,
        }
        all_three = plan_series_prune(
            **common,  # type: ignore[arg-type]
            progress_established=False,
            progress_unreadable=True,
            progress_seasons_unmatched=True,
        )
        assert _reasons(all_three)[1] == (
            "your watch history is too short to tell who is part-way through"
        )
        both_readable_ones = plan_series_prune(
            **common,  # type: ignore[arg-type]
            progress_unreadable=True,
            progress_seasons_unmatched=True,
        )
        assert _reasons(both_readable_ones)[1] == (
            "some plays are no longer readable, so who is part-way through is unknown"
        )

    def test_every_unanswerable_cause_produces_a_reason_the_flag_set_names(self) -> None:
        """``UNANSWERABLE_REASONS`` is what turns a hold into a *blocked* one, so a cause whose
        reason the set does not name is a hold that renders green -- "checked and passed" for a
        check that never ran (rules 93, 142).

        The causes are discovered from ``plan_series_prune``'s own signature rather than
        listed, so a fifth is covered the moment it is added: a hand-written list can only
        pin the members somebody remembered (rule 145). Each is driven by inverting its
        default, since ``progress_established`` reads the opposite way from the others.

        The fourth arrived exactly this way. ``progress_show_unmatched`` (#489) reddened this
        test on the commit that added it, before any test written *for* it ran, which is the
        whole reason the walk discovers rather than lists -- and what it caught was real: the
        first draft held on it, which moves every unmatched show off the review queue and onto
        the Protected page, the trade #486 declined.
        """
        causes = {
            name: param.default
            for name, param in signature(plan_series_prune).parameters.items()
            if name.startswith("progress_") and isinstance(param.default, bool)
        }
        # Reconciled by hand against the module: established, unreadable, seasons_unmatched,
        # show_unmatched.
        assert len(causes) == 4, f"the walk collected {sorted(causes)}"
        for name, default in causes.items():
            plan = plan_series_prune(
                series_title="Show",
                seasons=[_season(n) for n in range(1, 7)],
                keep_last=2,
                keep_first_season=False,
                # A reporting-only flag holds nothing by itself, so it is driven with the
                # widest holding cause beside it. Classified rather than skipped (rule 103):
                # the reason it names still has to be one UNANSWERABLE_REASONS knows, or the
                # hold it re-words renders green.
                **({"progress_established": False} if name in _HOLDS_NOTHING_ALONE else {}),  # type: ignore[arg-type]
                **{name: not default},  # type: ignore[arg-type]
            )
            assert plan.prunable == [], f"{name} did not hold the seasons"
            held = _reasons(plan)[1]
            assert held in UNANSWERABLE_REASONS, (
                f"{name} holds seasons with {held!r}, which UNANSWERABLE_REASONS does not "
                "name, so season_scan.guard_result renders that hold as a definite keep"
            )


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
        assert "episodes are missing" in _reasons(plan)[0]


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
