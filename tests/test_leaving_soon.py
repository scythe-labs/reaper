# SPDX-License-Identifier: AGPL-3.0-or-later
"""The Leaving Soon shelf reconcile.

The shelf should track the grace set exactly, per library: movies appear on their movie
library's shelf when they enter grace and come off when they leave it; seasons do the
same in their TV library. The reconcile is pure and gets pinned directly; the per-library
orchestration is driven against a fake Plex client, so none of it needs a server -- the
real adapter's live behaviour is the separate, supervised verification step.
"""

from __future__ import annotations

from datetime import timedelta

from reaper.clock import utcnow
from reaper.services.grace import GraceItem, GraceReport
from reaper.services.leaving_soon import (
    LEAVING_SOON_COLLECTION,
    LEAVING_SOON_LABEL,
    announce_new,
    reconcile,
    sync_section,
    sync_shelves,
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


class _FakePlex:
    """The slice of the client the reconcile touches, with a call log.

    One section per instance keeps each test's arithmetic readable; ``sync_shelves``
    tests hand the same fake to several libraries by keying the per-section state on
    the section key.
    """

    def __init__(
        self,
        *,
        section_items: dict[int, set[int]],
        collections: dict[int, set[int]] | None = None,
        labelled: dict[int, set[int]] | None = None,
    ) -> None:
        self._section_items = section_items
        # collection state per section key; a section absent has no collection yet.
        self.collections = dict(collections or {})
        self.labelled = {k: set(v) for k, v in (labelled or {}).items()}
        self.calls: list[tuple[str, object]] = []
        # collection rating keys are distinct from section keys to catch conflation:
        # collection key = section key + 9000.
        self._ckey = {k: k + 9000 for k in section_items}

    async def section_rating_keys(self, section_key: int, *, kind: str) -> set[int]:
        return set(self._section_items[section_key])

    async def find_collection(self, section_key: int, name: str) -> int | None:
        assert name == LEAVING_SOON_COLLECTION
        return self._ckey[section_key] if section_key in self.collections else None

    async def collection_children(self, collection_key: int) -> set[int]:
        section_key = collection_key - 9000
        return set(self.collections[section_key])

    async def labelled_in_section(self, section_key: int, *, kind: str, label: str) -> set[int]:
        assert label == LEAVING_SOON_LABEL
        return set(self.labelled.get(section_key, set()))

    async def create_collection(
        self, section_key: int, *, kind: str, name: str, rating_keys: list[int]
    ) -> int:
        self.calls.append(("create", (section_key, tuple(rating_keys))))
        self.collections[section_key] = set(rating_keys)
        return self._ckey[section_key]

    async def add_to_collection(self, collection_key: int, rating_keys: list[int]) -> None:
        self.calls.append(("collection_add", tuple(rating_keys)))
        self.collections[collection_key - 9000].update(rating_keys)

    async def remove_from_collection(self, collection_key: int, rating_keys: list[int]) -> None:
        self.calls.append(("collection_remove", tuple(rating_keys)))
        self.collections[collection_key - 9000] -= set(rating_keys)

    async def add_label(self, section_title: str, rating_keys: list[int], label: str) -> None:
        self.calls.append(("label_add", tuple(rating_keys)))
        self.labelled.setdefault(0, set()).update(rating_keys)

    async def remove_label(self, section_title: str, rating_keys: list[int], label: str) -> None:
        self.calls.append(("label_remove", tuple(rating_keys)))


class _FakeNotifier:
    def __init__(self) -> None:
        self.announced: list[str] | None = None

    async def announce_leaving_soon(self, titles: list[str], *, grace_days: int) -> bool:
        self.announced = titles
        return True


class TestReconcile:
    def test_it_adds_new_and_removes_departed(self) -> None:
        plan = reconcile(should_be_marked={1, 2, 3}, currently_marked={2, 3, 4})
        assert plan.to_add == [1]  # newly in grace
        assert plan.to_remove == [4]  # left grace, unmark it

    def test_a_matching_set_is_a_noop(self) -> None:
        plan = reconcile(should_be_marked={1, 2}, currently_marked={1, 2})
        assert plan.is_noop

    def test_the_plan_is_sorted_and_stable(self) -> None:
        plan = reconcile(should_be_marked={5, 1, 3}, currently_marked=set())
        assert plan.to_add == [1, 3, 5]


class TestSyncSection:
    async def test_it_marks_only_what_lives_in_the_section(self) -> None:
        """The grace set is intersected with the section's own keys: an item in another
        library must never leak onto this library's shelf."""
        plex = _FakePlex(section_items={10: {1, 2}}, collections={10: set()})
        outcome = await sync_section(
            plex,  # type: ignore[arg-type]
            section_key=10,
            section_title="Movies",
            kind="movie",
            in_grace={1, 2, 700},  # 700 lives elsewhere
            apply=True,
        )
        assert outcome.on_shelf == 2
        assert plex.collections[10] == {1, 2}

    async def test_preview_computes_but_does_not_write(self) -> None:
        plex = _FakePlex(section_items={10: {1}})
        outcome = await sync_section(
            plex,  # type: ignore[arg-type]
            section_key=10,
            section_title="Movies",
            kind="movie",
            in_grace={1},
            apply=False,
        )
        assert outcome.added == 1
        assert outcome.applied is False
        assert plex.calls == []  # nothing written

    async def test_it_removes_before_it_adds(self) -> None:
        """Remove first, then add: the shelf never briefly over-covers if a later add
        fails partway."""
        plex = _FakePlex(
            section_items={10: {1, 9}},
            collections={10: {9}},
            labelled={10: {9}},
        )
        await sync_section(
            plex,  # type: ignore[arg-type]
            section_key=10,
            section_title="Movies",
            kind="movie",
            in_grace={1},
            apply=True,
        )
        kinds = [name for name, _ in plex.calls]
        assert kinds.index("collection_remove") < kinds.index("collection_add")
        assert kinds.index("label_remove") < kinds.index("label_add")

    async def test_the_first_marks_create_the_collection_with_its_items(self) -> None:
        """Plex refuses an empty collection, so the shelf is born already holding its
        items rather than created bare and filled after."""
        plex = _FakePlex(section_items={10: {1, 2}})
        await sync_section(
            plex,  # type: ignore[arg-type]
            section_key=10,
            section_title="Movies",
            kind="movie",
            in_grace={1, 2},
            apply=True,
        )
        assert ("create", (10, (1, 2))) in plex.calls

    async def test_a_matching_shelf_writes_nothing_and_still_counts_as_applied(self) -> None:
        """Found live: a library whose shelf already matched reported applied=False,
        which made a fully-written pass across several libraries claim "preview only".
        Nothing to write plus permission to write IS the applied state."""
        plex = _FakePlex(
            section_items={10: {1}},
            collections={10: {1}},
            labelled={10: {1}},
        )
        outcome = await sync_section(
            plex,  # type: ignore[arg-type]
            section_key=10,
            section_title="Movies",
            kind="movie",
            in_grace={1},
            apply=True,
        )
        assert plex.calls == []
        assert outcome.added == 0 and outcome.removed == 0
        assert outcome.applied is True

    async def test_an_empty_library_counts_as_applied_alongside_a_written_one(self) -> None:
        """The aggregate: one movie library takes writes, one TV library has nothing in
        grace. The pass wrote everything it should -- it must report applied, not
        preview."""
        plex = _FakePlex(section_items={10: {1}, 20: set()})
        outcomes = await sync_shelves(
            plex,  # type: ignore[arg-type]
            [
                {"key": 10, "title": "Movies", "kind": "movie", "enabled": True},
                {"key": 20, "title": "TV", "kind": "show", "enabled": True},
            ],
            movie_keys={1},
            season_keys=set(),
            apply=True,
        )
        assert all(o.applied for o in outcomes)


class TestSyncShelves:
    async def test_movies_and_seasons_go_to_their_own_libraries(self) -> None:
        """A movie key must never enter a TV library's reconcile, nor a season a movie
        library's -- each library's shelf holds only what lives in it."""
        plex = _FakePlex(section_items={10: {1}, 20: {700}})
        outcomes = await sync_shelves(
            plex,  # type: ignore[arg-type]
            [
                {"key": 10, "title": "Movies", "kind": "movie", "enabled": True},
                {"key": 20, "title": "TV", "kind": "show", "enabled": True},
            ],
            movie_keys={1},
            season_keys={700},
            apply=True,
        )
        assert plex.collections[10] == {1}
        assert plex.collections[20] == {700}
        by_kind = {o.kind: o.on_shelf for o in outcomes}
        assert by_kind == {"movie": 1, "show": 1}

    async def test_one_broken_library_does_not_block_the_rest(self) -> None:
        from reaper.clients.plex import PlexError

        class _HalfBroken(_FakePlex):
            async def section_rating_keys(self, section_key: int, *, kind: str) -> set[int]:
                if section_key == 10:
                    raise PlexError("unreachable")
                return await super().section_rating_keys(section_key, kind=kind)

        plex = _HalfBroken(section_items={10: {1}, 20: {700}})
        outcomes = await sync_shelves(
            plex,  # type: ignore[arg-type]
            [
                {"key": 10, "title": "Movies", "kind": "movie", "enabled": True},
                {"key": 20, "title": "TV", "kind": "show", "enabled": True},
            ],
            movie_keys={1},
            season_keys={700},
            apply=True,
        )
        assert outcomes[0].error is not None
        assert outcomes[1].error is None
        assert plex.collections[20] == {700}  # the healthy library still reconciled


class TestAnnounce:
    async def test_it_announces_only_the_newly_in_grace(self) -> None:
        report = _report(
            [
                _item(1, title="New Arrival"),
                _item(2, title="Already Announced"),
                _item(700, title="A Show · Season 2", media_type="season"),
            ]
        )
        notifier = _FakeNotifier()
        notified, announced = await announce_new(
            notifier,  # type: ignore[arg-type]
            report,
            already_announced={2},
        )
        assert notified is True
        assert notifier.announced == ["New Arrival", "A Show · Season 2"]
        assert announced == {1, 2, 700}

    async def test_items_plex_never_matched_are_skipped(self) -> None:
        """No rating key means Plex cannot address it; the announced set is keyed on
        rating keys, so it cannot be tracked either. Discord silence over spam."""
        report = _report([_item(1), _item(None, title="Unmatched")])
        notifier = _FakeNotifier()
        _notified, announced = await announce_new(
            notifier,  # type: ignore[arg-type]
            report,
            already_announced=set(),
        )
        assert notifier.announced == ["Film"]
        assert announced == {1}

    async def test_nothing_new_means_no_post(self) -> None:
        report = _report([_item(1)])
        notifier = _FakeNotifier()
        notified, announced = await announce_new(
            notifier,  # type: ignore[arg-type]
            report,
            already_announced={1},
        )
        assert notified is False
        assert notifier.announced is None
        assert announced == {1}

    async def test_the_announced_set_is_pruned_to_items_still_in_grace(self) -> None:
        """A title that left grace and later returns must be announced afresh: its key
        leaves the persisted set the moment it is no longer in grace."""
        report = _report([_item(1)])
        _notified, announced = await announce_new(None, report, already_announced={1, 99})
        assert announced == {1}  # 99 left grace, so it may be announced again one day

    async def test_a_failed_post_is_not_marked_announced(self) -> None:
        class _FailingNotifier:
            async def announce_leaving_soon(self, titles: list[str], *, grace_days: int) -> bool:
                return False

        report = _report([_item(1)])
        _notified, announced = await announce_new(
            _FailingNotifier(),  # type: ignore[arg-type]
            report,
            already_announced=set(),
        )
        assert announced == set()  # retried on the next pass, not silently dropped
