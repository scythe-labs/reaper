# SPDX-License-Identifier: AGPL-3.0-or-later
"""The Leaving Soon shelf reconcile.

The shelf should track the grace set exactly, per library: movies appear on their movie
library's shelf when they enter grace and come off when they leave it; seasons do the
same in their TV library. The reconcile is pure and gets pinned directly; the per-library
orchestration is driven against a fake Plex client, so none of it needs a server -- the
real adapter's live behavior is the separate, supervised verification step.
"""

from __future__ import annotations

from datetime import timedelta

from reaper.clock import utcnow
from reaper.services.grace import GraceItem, GraceReport
from reaper.services.leaving_soon import (
    LEAVING_SOON_COLLECTION,
    LEAVING_SOON_LABEL,
    LeavingSoonResult,
    ShelfOutcome,
    announce_new,
    reconcile,
    sync_section,
    sync_shelves,
)

NOW = utcnow()


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
    ) -> None:
        self._section_items = section_items
        # collection state per section key; a section absent has no collection yet.
        self.collections = dict(collections or {})
        self.labeled = {k: set(v) for k, v in (labeled or {}).items()}
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

    async def labeled_in_section(self, section_key: int, *, kind: str, label: str) -> set[int]:
        assert label == LEAVING_SOON_LABEL
        return set(self.labeled.get(section_key, set()))

    async def create_collection(
        self, section_key: int, *, kind: str, name: str, rating_keys: list[int]
    ) -> int:
        self.calls.append(("create", (section_key, tuple(rating_keys))))
        self.collections[section_key] = set(rating_keys)
        return self._ckey[section_key]

    async def add_to_collection(self, collection_key: int, rating_keys: list[int]) -> None:
        self.calls.append(("collection_add", tuple(rating_keys)))
        self.collections[collection_key - 9000].update(rating_keys)

    async def remove_collection_members(
        self, section_key: int, *, name: str, rating_keys: list[int]
    ) -> None:
        assert name == LEAVING_SOON_COLLECTION
        self.calls.append(("collection_remove", tuple(rating_keys)))
        # The real client resolves the section by key; here we detach the keys from whichever
        # section's collection holds them.
        for members in self.collections.values():
            members -= set(rating_keys)

    async def delete_collection(self, collection_key: int) -> None:
        self.calls.append(("collection_delete", collection_key))
        self.collections.pop(collection_key - 9000, None)

    async def add_label(self, section_key: int, rating_keys: list[int], label: str) -> None:
        self.calls.append(("label_add", tuple(rating_keys)))
        self.labeled.setdefault(section_key, set()).update(rating_keys)

    async def remove_label(self, section_key: int, rating_keys: list[int], label: str) -> None:
        self.calls.append(("label_remove", tuple(rating_keys)))


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
    """The one sentence every surface reports this pass with (rule 104).

    Written here and nowhere else: the stored Jobs row, the "Update now" response and the
    Plex panel's status line all render this string, so the four branches and their ORDER
    are the whole contract. The order is what #555 was: the route named the no-libraries
    case after the row had already been stored as a preview.
    """

    def test_nothing_turned_on_is_reported_as_itself_not_as_preview(self) -> None:
        # The bug's exact state. `applied` is false with no outcomes, so a ladder that asks
        # about preview first calls a misconfiguration a successful dry run.
        result = _pass()
        assert result.applied is False
        assert result.summary == "No libraries are turned on, so no shelf was updated"
        assert result.ok is False

    def test_a_library_that_failed_beats_the_preview_caveat(self) -> None:
        result = _pass(_outcome(applied=False, error="connection refused"))
        assert result.summary == "These shelves didn't update: Movies"
        assert result.ok is False

    def test_the_failing_library_is_named_and_the_working_one_is_not(self) -> None:
        """The sentence used to be "Some shelves didn't update", so one unreachable library
        out of several read exactly like all of them failing, and no surface carried the
        detail: the per-library list was on the wire but nothing rendered it. Naming them is
        the whole answer the operator gets, so it has to name the right ones."""
        result = _pass(
            _outcome(added=4),
            _outcome(applied=False, error="connection refused", title="Kids TV"),
        )
        assert result.summary == "These shelves didn't update: Kids TV"
        assert result.ok is False

    def test_every_failing_library_is_named(self) -> None:
        result = _pass(
            _outcome(applied=False, error="connection refused", title="Kids TV"),
            _outcome(applied=False, error="timed out"),
        )
        assert result.summary == "These shelves didn't update: Kids TV, Movies"

    def test_the_raw_cause_stays_out_of_the_sentence(self) -> None:
        """``str(exc)`` is stack-shaped and rule 21 keeps it off the screen. It survives in
        the ``leaving_soon.problems`` log event, which is where a raw cause belongs."""
        result = _pass(_outcome(applied=False, error="HTTPStatusError: 502 Bad Gateway"))
        assert "502" not in result.summary
        assert "HTTPStatusError" not in result.summary

    def test_a_preview_is_not_a_failure(self) -> None:
        # Read-only with no opt-in: computed, announced, and deliberately not written. The
        # tick stays green, and the sentence says why nothing moved in Plex.
        result = _pass(_outcome(added=3, applied=False))
        assert result.summary == "Preview only, nothing written"
        assert result.ok is True

    def test_a_clean_pass_counts_what_it_did(self) -> None:
        result = _pass(_outcome(added=4, removed=1), _outcome(added=2))
        assert result.summary == "6 added, 1 cleared"
        assert result.ok is True

    def test_the_counts_carry_thousands_separators(self) -> None:
        # The browser used to format these itself and now renders this string as it arrives,
        # so the grouping has to be here or it is nowhere.
        assert _pass(_outcome(added=1200, removed=25)).summary == "1,200 added, 25 cleared"


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
        fails partway. Item 8 stays on the shelf, so this is a partial detach (batch
        removeCollection), not a whole-collection drop."""
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
        )
        kinds = [name for name, _ in plex.calls]
        assert kinds.index("collection_remove") < kinds.index("collection_add")
        assert kinds.index("label_remove") < kinds.index("label_add")
        assert plex.collections[10] == {1, 8}  # 9 detached, 1 added, 8 stayed

    async def test_a_full_clear_drops_the_whole_collection(self) -> None:
        """Nothing stays in grace, so the shelf is emptied by ONE whole-collection delete,
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
        )
        kinds = [name for name, _ in plex.calls]
        assert "collection_delete" in kinds
        assert "collection_remove" not in kinds  # never per-member
        assert 10 not in plex.collections  # collection gone

    async def test_a_total_swap_drops_then_recreates(self) -> None:
        """The whole current membership leaves AND a fresh set arrives (a list replaced
        from another tool): drop the collection whole, then recreate it from the new set --
        never detach the old members one by one."""
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
        )
        assert ("create", (10, (1, 2))) in plex.calls

    async def test_a_matching_shelf_writes_nothing_and_still_counts_as_applied(self) -> None:
        """Found live: a library whose shelf already matched reported applied=False,
        which made a fully-written pass across several libraries claim "preview only".
        Nothing to write plus permission to write IS the applied state."""
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
