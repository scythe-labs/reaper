# SPDX-License-Identifier: AGPL-3.0-or-later
"""The Leaving Soon shelf reconcile.

The shelf should track the grace set exactly, per library. Movies appear on their movie
library's shelf when they enter grace and come off when they leave it, and seasons do the
same in their TV library. The reconcile is pure and gets pinned directly. The per-library
orchestration is driven against a fake Plex client, so none of it needs a server. The real
adapter's live behavior is the separate, supervised verification step.
"""

from __future__ import annotations

from datetime import timedelta

from reaper.clients.plex import normalize_label
from reaper.clock import utcnow
from reaper.engine.reason import Reason
from reaper.services.app_settings import DEFAULT_LEAVING_SOON_NAME
from reaper.services.grace import GraceItem, GraceReport
from reaper.services.leaving_soon import (
    LeavingSoonResult,
    ShelfName,
    ShelfOutcome,
    announce_new,
    reconcile,
    sync_section,
    sync_shelves,
)

NOW = utcnow()
#: Every test that is not about renaming uses the shipped name, already on the server.
SHELF = ShelfName(DEFAULT_LEAVING_SOON_NAME, DEFAULT_LEAVING_SOON_NAME)


def _item(rating_key: int | None, *, title: str = "Film", media_type: str = "movie") -> GraceItem:
    return GraceItem(
        media_key=f"radarr:1:{rating_key or 0}",
        candidate_id=rating_key or 0,
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
        total_bytes_in_grace=sum(i.size_bytes or 0 for i in items),
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
        labeled: dict[int, set[int]] | None = None,
        name: str = DEFAULT_LEAVING_SOON_NAME,
        other: dict[int, tuple[str, set[int]]] | None = None,
    ) -> None:
        self._section_items = section_items
        #: What the collection and the labels on this server are called. One name for the
        #: whole server, as in the real thing. A reconcile looking under any other name finds
        #: nothing, which is what makes a rename test mean something.
        self.name = name
        # Collection state per section key. A section absent has no collection yet.
        self.collections = dict(collections or {})
        self.labeled = {k: set(v) for k, v in (labeled or {}).items()}
        self.calls: list[tuple[str, object]] = []
        #: A collection in the section that is not Reaper's shelf, the operator's own, under
        #: a name of its own. Only a rename onto that name can see it.
        self.other = {k: (n, set(v)) for k, (n, v) in (other or {}).items()}
        # Collection rating keys are distinct from section keys, to catch conflation.
        # Collection key = section key + 9000, and a non-shelf collection is + 8000.
        self._ckey = {k: k + 9000 for k in section_items}

    async def section_rating_keys(self, section_key: int, *, kind: str) -> set[int]:
        return set(self._section_items[section_key])

    async def find_collection(self, section_key: int, name: str) -> int | None:
        found = self.other.get(section_key)
        if found is not None and normalize_label(name) == normalize_label(found[0]):
            return section_key + 8000
        if normalize_label(name) != normalize_label(self.name):
            return None
        return self._ckey[section_key] if section_key in self.collections else None

    async def collection_children(self, collection_key: int) -> set[int]:
        if collection_key < 9000:
            return set(self.other[collection_key - 8000][1])
        section_key = collection_key - 9000
        return set(self.collections[section_key])

    async def labeled_in_section(self, section_key: int, *, kind: str, label: str) -> set[int]:
        if normalize_label(label) != normalize_label(self.name):
            return set()
        return set(self.labeled.get(section_key, set()))

    async def create_collection(
        self, section_key: int, *, kind: str, name: str, rating_keys: list[int]
    ) -> int:
        self.calls.append(("create", (section_key, tuple(rating_keys))))
        self.collections[section_key] = set(rating_keys)
        return self._ckey[section_key]

    async def add_to_collection(self, collection_key: int, rating_keys: list[int]) -> None:
        self.calls.append(("collection_add", tuple(rating_keys)))
        if collection_key < 9000:
            self.other[collection_key - 8000][1].update(rating_keys)
            return
        self.collections[collection_key - 9000].update(rating_keys)

    async def remove_collection_members(
        self, section_key: int, *, name: str, rating_keys: list[int]
    ) -> None:
        assert normalize_label(name) == normalize_label(self.name)
        self.calls.append(("collection_remove", tuple(rating_keys)))
        # The real client resolves the section by key. This fake detaches the keys from
        # whichever section's collection holds them instead.
        for members in self.collections.values():
            members -= set(rating_keys)

    async def rename_collection(self, collection_key: int, name: str) -> None:
        self.calls.append(("collection_rename", (collection_key, name)))
        # In place, so the members ride along untouched. That is the property the real
        # client's editTitle buys, and the reason the shelf is not dropped and rebuilt.
        self.name = name

    async def delete_collection(self, collection_key: int) -> None:
        self.calls.append(("collection_delete", collection_key))
        if collection_key < 9000:
            self.other.pop(collection_key - 8000, None)
            return
        self.collections.pop(collection_key - 9000, None)

    async def add_label(self, section_key: int, rating_keys: list[int], label: str) -> None:
        self.calls.append(("label_add", (tuple(rating_keys), label)))
        self.labeled.setdefault(section_key, set()).update(rating_keys)

    async def remove_label(self, section_key: int, rating_keys: list[int], label: str) -> None:
        self.calls.append(("label_remove", (tuple(rating_keys), label)))
        self.labeled.get(section_key, set()).difference_update(rating_keys)


class _FakeNotifier:
    def __init__(self) -> None:
        self.announced: list[str] | None = None

    async def announce_leaving_soon(self, titles: list[str], *, grace_days: int) -> bool:
        self.announced = titles
        return True


def _outcome(
    *,
    added: int = 0,
    removed: int = 0,
    applied: bool = True,
    error: str | None = None,
    title: str = "Movies",
) -> ShelfOutcome:
    return ShelfOutcome(
        section_key=2,
        section_title=title,
        kind="movie",
        added=added,
        removed=removed,
        on_shelf=added,
        applied=applied,
        error=error,
    )


def _pass(*outcomes: ShelfOutcome) -> LeavingSoonResult:
    return LeavingSoonResult(
        outcomes=list(outcomes),
        notified=False,
        announced=frozenset(),
        movies_on_shelves=0,
        seasons_on_shelves=0,
    )


class TestTheSummary:
    """The one typed reason every surface reports this pass with.

    This is composed here and nowhere else. The stored Jobs row, the "Update now" response,
    and the Plex panel's status line all build this id under ``jobs.result.*``, so the four
    branches and their order are the whole contract. Getting the order wrong means one of
    those surfaces reports the wrong reason. The id and raw parameters are the only thing the
    server decides. The sentence itself is composed in the browser (``why.ts``'s
    ``composeIn``).
    """

    def test_nothing_turned_on_is_reported_as_itself_not_as_preview(self) -> None:
        # `applied` is false with no outcomes. A ladder that checks for preview first would
        # misread this exact state as a misconfiguration succeeding as a dry run.
        result = _pass()
        assert result.applied is False
        assert result.summary == Reason("shelf_no_libraries")
        assert result.ok is False

    def test_a_library_that_failed_beats_the_preview_caveat(self) -> None:
        result = _pass(_outcome(applied=False, error="connection refused"))
        assert result.summary == Reason("shelf_failed", {"libraries": "Movies"})
        assert result.ok is False

    def test_the_failing_library_is_named_and_the_working_one_is_not(self) -> None:
        """Naming the failing library is the whole answer the operator gets, so it has to
        name the right one. A generic "some shelves didn't update" message would make one
        unreachable library out of several read exactly like all of them failing."""
        result = _pass(
            _outcome(added=4),
            _outcome(applied=False, error="connection refused", title="Kids TV"),
        )
        assert result.summary == Reason("shelf_failed", {"libraries": "Kids TV"})
        assert result.ok is False

    def test_every_failing_library_is_named(self) -> None:
        result = _pass(
            _outcome(applied=False, error="connection refused", title="Kids TV"),
            _outcome(applied=False, error="timed out"),
        )
        assert result.summary == Reason("shelf_failed", {"libraries": "Kids TV, Movies"})

    def test_the_raw_cause_stays_out_of_the_reason(self) -> None:
        """``str(exc)`` reads like an internal error, not something to show an operator, so
        it stays out of the reason shown on screen. It survives in the
        ``leaving_soon.problems`` log event instead, which is where a raw cause belongs. The
        ``libraries`` parameter names sections, never the exception text, so an equality
        check against the exact reason proves that rather than a substring search."""
        result = _pass(_outcome(applied=False, error="HTTPStatusError: 502 Bad Gateway"))
        assert result.summary == Reason("shelf_failed", {"libraries": "Movies"})

    def test_a_preview_is_not_a_failure(self) -> None:
        # Read-only with no opt-in. The result is computed and announced but never written.
        # The tick stays green, and the reason says why nothing moved in Plex.
        result = _pass(_outcome(added=3, applied=False))
        assert result.summary == Reason("shelf_preview")
        assert result.ok is True

    def test_a_clean_pass_counts_what_it_did(self) -> None:
        result = _pass(_outcome(added=4, removed=1), _outcome(added=2))
        assert result.summary == Reason("shelf_updated", {"added": 6, "removed": 1})
        assert result.ok is True

    def test_the_counts_are_raw_numbers_not_pre_formatted(self) -> None:
        # The browser renders these counts under jobs.result.shelf_updated with
        # `{added, number}`/`{removed, number}`, so the server's job is only to pass the raw
        # counts through unformatted.
        result = _pass(_outcome(added=1200, removed=25))
        assert result.summary == Reason("shelf_updated", {"added": 1200, "removed": 25})


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
            shelf=SHELF,
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
            shelf=SHELF,
        )
        assert outcome.added == 1
        assert outcome.applied is False
        assert plex.calls == []  # nothing written

    async def test_it_removes_before_it_adds(self) -> None:
        """Remove first, then add. That way, the shelf never briefly over-covers if a later
        add fails partway. Item 8 stays on the shelf, so this exercises a partial detach
        (batch removeCollection), not a whole-collection drop."""
        plex = _FakePlex(
            section_items={10: {1, 8, 9}},
            collections={10: {8, 9}},
            labeled={10: {8, 9}},
        )
        await sync_section(
            plex,  # type: ignore[arg-type]
            section_key=10,
            section_title="Movies",
            kind="movie",
            in_grace={1, 8},
            apply=True,
            shelf=SHELF,
        )
        kinds = [name for name, _ in plex.calls]
        assert kinds.index("collection_remove") < kinds.index("collection_add")
        assert kinds.index("label_remove") < kinds.index("label_add")
        assert plex.collections[10] == {1, 8}  # 9 detached, 1 added, 8 stayed

    async def test_a_full_clear_drops_the_whole_collection(self) -> None:
        """Nothing stays in grace, so the shelf is emptied by one whole-collection delete,
        never a detach per member."""
        plex = _FakePlex(
            section_items={10: {7, 8, 9}},
            collections={10: {7, 8, 9}},
            labeled={10: {7, 8, 9}},
        )
        await sync_section(
            plex,  # type: ignore[arg-type]
            section_key=10,
            section_title="Movies",
            kind="movie",
            in_grace=set(),
            apply=True,
            shelf=SHELF,
        )
        kinds = [name for name, _ in plex.calls]
        assert "collection_delete" in kinds
        assert "collection_remove" not in kinds  # never per-member
        assert 10 not in plex.collections  # collection gone

    async def test_a_total_swap_drops_then_recreates(self) -> None:
        """The whole current membership leaves and a fresh set arrives, as if a list were
        replaced from another tool. This drops the collection whole, then recreates it from
        the new set, rather than detaching the old members one by one."""
        plex = _FakePlex(
            section_items={10: {1, 2, 8, 9}},
            collections={10: {8, 9}},  # the stale, externally-set membership
            labeled={10: {8, 9}},
        )
        await sync_section(
            plex,  # type: ignore[arg-type]
            section_key=10,
            section_title="Movies",
            kind="movie",
            in_grace={1, 2},  # the real grace set, disjoint from what was on the shelf
            apply=True,
            shelf=SHELF,
        )
        kinds = [name for name, _ in plex.calls]
        assert kinds.index("collection_delete") < kinds.index("create")
        assert "collection_remove" not in kinds
        assert plex.collections[10] == {1, 2}

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
            shelf=SHELF,
        )
        assert ("create", (10, (1, 2))) in plex.calls

    async def test_a_matching_shelf_writes_nothing_and_still_counts_as_applied(self) -> None:
        """A library whose shelf already matches must still report ``applied=True``, not
        False. Reporting False there would make a fully-written pass across several
        libraries claim "preview only" for the ones that already matched. Nothing left to
        write plus permission to write is what applied means."""
        plex = _FakePlex(
            section_items={10: {1}},
            collections={10: {1}},
            labeled={10: {1}},
        )
        outcome = await sync_section(
            plex,  # type: ignore[arg-type]
            section_key=10,
            section_title="Movies",
            kind="movie",
            in_grace={1},
            apply=True,
            shelf=SHELF,
        )
        assert plex.calls == []
        assert outcome.added == 0 and outcome.removed == 0
        assert outcome.applied is True

    async def test_an_empty_library_counts_as_applied_alongside_a_written_one(self) -> None:
        """One movie library takes writes, and one TV library has nothing in grace. The pass
        wrote everything it should, so it must report applied, not preview, across the whole
        aggregate."""
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
            shelf=SHELF,
        )
        assert all(o.applied for o in outcomes)


class TestRenamingTheShelf:
    """The operator names the shelf, so a rename has to move what is already in the library.

    Plex still holds the shelf under the old name until a pass carries it across, which is
    what ``ShelfName.previous`` is. Getting this wrong strands a collection and a label in
    somebody's library under a name nothing will ever look for again.
    """

    async def test_the_collection_is_retitled_and_keeps_its_members(self) -> None:
        """This must retitle the collection, never drop and rebuild it. The rating key
        survives, so a poster or a Plex Home screen pin survives with it. Nothing is added or
        detached here. The same three titles are on the shelf before and after."""
        plex = _FakePlex(
            section_items={10: {1, 2, 3}},
            collections={10: {1, 2, 3}},
            labeled={10: {1, 2, 3}},
        )
        await sync_section(
            plex,  # type: ignore[arg-type]
            section_key=10,
            section_title="Movies",
            kind="movie",
            in_grace={1, 2, 3},
            apply=True,
            shelf=ShelfName(current="Last chance", previous=DEFAULT_LEAVING_SOON_NAME),
        )
        assert ("collection_rename", (9010, "Last chance")) in plex.calls
        assert ("collection_delete", 9010) not in plex.calls
        assert plex.collections[10] == {1, 2, 3}
        assert plex.name == "Last chance"

    async def test_the_old_label_comes_off_and_the_new_one_goes_on(self) -> None:
        """A label is a tag on each item and Plex offers no rename for one, so carrying it
        across is a removal under the old name plus an add under the new. Leaving the old
        one behind would keep every Plex user's smart collections and overlays marking
        titles Reaper no longer tracks."""
        plex = _FakePlex(
            section_items={10: {1, 2}},
            collections={10: {1, 2}},
            labeled={10: {1, 2}},
        )
        await sync_section(
            plex,  # type: ignore[arg-type]
            section_key=10,
            section_title="Movies",
            kind="movie",
            in_grace={1, 2},
            apply=True,
            shelf=ShelfName(current="Last chance", previous=DEFAULT_LEAVING_SOON_NAME),
        )
        assert ("label_remove", ((1, 2), DEFAULT_LEAVING_SOON_NAME)) in plex.calls
        assert ("label_add", ((1, 2), "Last chance")) in plex.calls

    async def test_an_empty_shelf_still_loses_the_old_label(self) -> None:
        """Nothing is in grace, so every plan is a no-op under the new name. The write step
        must still run under the old name, because the old label is still on two titles, and
        only a pass that reads the old name can see them."""
        plex = _FakePlex(section_items={10: {1, 2}}, labeled={10: {1, 2}})
        await sync_section(
            plex,  # type: ignore[arg-type]
            section_key=10,
            section_title="Movies",
            kind="movie",
            in_grace=set(),
            apply=True,
            shelf=ShelfName(current="Last chance", previous=DEFAULT_LEAVING_SOON_NAME),
        )
        assert ("label_remove", ((1, 2), DEFAULT_LEAVING_SOON_NAME)) in plex.calls
        assert plex.labeled[10] == set()

    async def test_renaming_onto_a_name_the_library_already_uses_merges_rather_than_splits(
        self,
    ) -> None:
        """The operator renames the shelf to a title their library already has. Re-titling
        would leave two collections under one name, and Plex hands a lookup only one of
        them, so half the shelf would sit somewhere nothing ever takes titles back off.
        Reaper's old shelf is dropped and the collection that already carries the name takes
        the whole set."""
        plex = _FakePlex(
            section_items={10: {1, 2}},
            collections={10: {2}},
            labeled={10: {2}},
            other={10: ("Last chance", {1})},
        )
        await sync_section(
            plex,  # type: ignore[arg-type]
            section_key=10,
            section_title="Movies",
            kind="movie",
            in_grace={1, 2},
            apply=True,
            shelf=ShelfName(current="Last chance", previous=DEFAULT_LEAVING_SOON_NAME),
        )
        assert ("collection_rename", (9010, "Last chance")) not in plex.calls
        assert ("collection_delete", 9010) in plex.calls
        # The collection that already carried the name survives, and the shelf lands in it.
        assert plex.other[10][1] == {1, 2}
        assert ("label_remove", ((2,), DEFAULT_LEAVING_SOON_NAME)) in plex.calls
        assert ("label_add", ((1, 2), "Last chance")) in plex.calls

    async def test_a_capitalization_change_is_not_a_rename(self) -> None:
        """Plex title-cases what it is given, so "leaving soon" and "Leaving Soon" name one
        shelf. Treating them as different would re-title a collection to a name it already
        answers to, and strip every label to add the same label back."""
        shelf = ShelfName(current="leaving soon", previous="Leaving Soon")
        assert shelf.renaming is False

        plex = _FakePlex(section_items={10: {1}}, collections={10: {1}}, labeled={10: {1}})
        await sync_section(
            plex,  # type: ignore[arg-type]
            section_key=10,
            section_title="Movies",
            kind="movie",
            in_grace={1},
            apply=True,
            shelf=shelf,
        )
        assert plex.calls == []

    async def test_a_preview_moves_nothing(self) -> None:
        """Read-only, so the rename waits with everything else. The old shelf stays exactly
        as it is until Reaper is allowed to write."""
        plex = _FakePlex(section_items={10: {1}}, collections={10: {1}}, labeled={10: {1}})
        await sync_section(
            plex,  # type: ignore[arg-type]
            section_key=10,
            section_title="Movies",
            kind="movie",
            in_grace={1},
            apply=False,
            shelf=ShelfName(current="Last chance", previous=DEFAULT_LEAVING_SOON_NAME),
        )
        assert plex.calls == []
        assert plex.name == DEFAULT_LEAVING_SOON_NAME

    async def test_clearing_the_shelf_drops_it_instead_of_renaming_it(self) -> None:
        """Turning the shelf off during an outstanding rename. Every member is leaving, so
        the collection goes in one request. Re-titling something this pass is about to
        delete is a wasted round trip, and the label still has to come off under the old
        name, or it stays on every title forever."""
        plex = _FakePlex(section_items={10: {1, 2}}, collections={10: {1, 2}}, labeled={10: {1, 2}})
        await sync_section(
            plex,  # type: ignore[arg-type]
            section_key=10,
            section_title="Movies",
            kind="movie",
            in_grace=set(),
            apply=True,
            shelf=ShelfName(current="Last chance", previous=DEFAULT_LEAVING_SOON_NAME),
        )
        kinds = [name for name, _ in plex.calls]
        assert "collection_rename" not in kinds
        assert ("collection_delete", 9010) in plex.calls
        assert ("label_remove", ((1, 2), DEFAULT_LEAVING_SOON_NAME)) in plex.calls
        assert plex.labeled[10] == set()


class TestSyncShelves:
    async def test_movies_and_seasons_go_to_their_own_libraries(self) -> None:
        """A movie key must never enter a TV library's reconcile, nor a season a movie
        library's reconcile. Each library's shelf holds only what lives in it."""
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
            shelf=SHELF,
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
            shelf=SHELF,
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
        """No rating key means Plex cannot address the item. The announced set is keyed on
        rating keys too, so an unmatched item cannot be tracked either. Staying silent is
        better than spamming Discord with a title nobody can confirm."""
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
        assert announced == set()  # retried on the next pass
