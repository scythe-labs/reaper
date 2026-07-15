# SPDX-License-Identifier: AGPL-3.0-or-later
"""The Leaving Soon reconcile.

The label set should track the grace set exactly: mark what entered grace, unmark what
left it. The reconcile is pure and gets pinned directly; the orchestration is driven
against a fake target and a fake notifier, so none of it needs a Plex server -- the real
Plex adapter's live behaviour is the separate, supervised verification step.
"""

from __future__ import annotations

from datetime import timedelta

from reaper.clock import utcnow
from reaper.services.grace import GraceItem, GraceReport
from reaper.services.leaving_soon import (
    LeavingSoonPlan,
    PlexLabelTarget,
    reconcile,
    sync,
)

NOW = utcnow()


def _item(rating_key: int | None, *, title: str = "Film", media_type: str = "movie") -> GraceItem:
    return GraceItem(
        media_key=f"radarr:1:{rating_key or 0}",
        plex_rating_key=rating_key,
        title=title,
        media_type=media_type,
        size_bytes=1,
        first_flagged_at=NOW,
        grace_ends_at=NOW + timedelta(days=10),
        days_remaining=10,
        in_grace=True,
    )


def _report(items: list[GraceItem], *, grace_days: int = 14) -> GraceReport:
    return GraceReport(
        grace_days=grace_days,
        in_grace=items,
        ready=[],
        total_bytes_in_grace=sum(i.size_bytes for i in items),
        total_bytes_ready=0,
    )


class _FakeTarget:
    def __init__(self, current: set[int]) -> None:
        self._current = current
        self.applied: LeavingSoonPlan | None = None

    async def current(self) -> set[int]:
        return self._current

    async def apply(self, plan: LeavingSoonPlan) -> None:
        self.applied = plan


class _FakeNotifier:
    def __init__(self) -> None:
        self.announced: list[str] | None = None

    async def announce_leaving_soon(self, titles: list[str], *, grace_days: int) -> bool:
        self.announced = titles
        return True


class TestReconcile:
    def test_it_adds_new_and_removes_departed(self) -> None:
        plan = reconcile(should_be_labelled={1, 2, 3}, currently_labelled={2, 3, 4})
        assert plan.to_add == [1]  # newly in grace
        assert plan.to_remove == [4]  # left grace, unmark it

    def test_a_matching_set_is_a_noop(self) -> None:
        plan = reconcile(should_be_labelled={1, 2}, currently_labelled={1, 2})
        assert plan.is_noop

    def test_the_plan_is_sorted_and_stable(self) -> None:
        plan = reconcile(should_be_labelled={5, 1, 3}, currently_labelled=set())
        assert plan.to_add == [1, 3, 5]


class TestSync:
    async def test_it_marks_the_in_grace_set(self) -> None:
        report = _report([_item(1), _item(2)])
        target = _FakeTarget(current={2})  # 2 already marked
        result = await sync(target, report, apply=True)
        assert result.plan.to_add == [1]
        assert result.applied is True
        assert target.applied is not None

    async def test_preview_computes_but_does_not_write(self) -> None:
        report = _report([_item(1)])
        target = _FakeTarget(current=set())
        result = await sync(target, report, apply=False)
        assert result.plan.to_add == [1]
        assert result.applied is False
        assert target.applied is None  # nothing written

    async def test_items_plex_has_not_matched_are_not_labelled(self) -> None:
        """No rating key means Plex cannot address it -- it must not appear in the plan."""
        report = _report([_item(1), _item(None, title="Unmatched")])
        target = _FakeTarget(current=set())
        result = await sync(target, report, apply=True)
        assert result.plan.to_add == [1]

    async def test_a_season_is_excluded_from_the_movie_scoped_label(self) -> None:
        """The Leaving Soon label lives in the movie library. A season's rating key points
        at a TV item, so it must not enter the movie-section reconcile -- there it could be
        added but never removed (current() is movie-scoped) and re-announced every run."""
        report = _report([_item(1), _item(700, title="A Season", media_type="season")])
        target = _FakeTarget(current=set())
        result = await sync(target, report, apply=True)
        assert result.plan.to_add == [1]  # the movie only; the season key 700 is excluded

    async def test_it_announces_only_the_newly_marked_titles(self) -> None:
        report = _report([_item(1, title="New Arrival"), _item(2, title="Already Marked")])
        target = _FakeTarget(current={2})  # 2 was already labelled
        notifier = _FakeNotifier()
        result = await sync(target, report, notifier=notifier, apply=True)  # type: ignore[arg-type]
        assert result.notified is True
        assert notifier.announced == ["New Arrival"]  # not the already-marked one

    async def test_no_new_titles_means_no_announce(self) -> None:
        report = _report([_item(1)])
        target = _FakeTarget(current={1})  # already marked, nothing new
        notifier = _FakeNotifier()
        result = await sync(target, report, notifier=notifier, apply=True)  # type: ignore[arg-type]
        assert result.notified is False
        assert notifier.announced is None


class TestPlexLabelTargetOrder:
    async def test_it_removes_before_it_adds(self) -> None:
        """Remove first, then add: the label set never briefly over-covers if a later add
        fails partway."""
        calls: list[str] = []

        class _Plex:
            async def items_with_label(self, section: str, label: str) -> set[int]:
                return {9}

            async def remove_label(self, section: str, keys: list[int], label: str) -> None:
                calls.append("remove")

            async def add_label(self, section: str, keys: list[int], label: str) -> None:
                calls.append("add")

        target = PlexLabelTarget(_Plex(), "Movies")
        await target.apply(LeavingSoonPlan(to_add=[1], to_remove=[9]))
        assert calls == ["remove", "add"]
