# SPDX-License-Identifier: AGPL-3.0-or-later
"""Name matching and container absence on the keep-list path.

Two ways a keep list stops protecting without saying so, both proven here:

* **A name that only differs in case.** The keep collection is looked for in the library the
  operator named, and the comparison must be case-folded on both sides. Without that, an
  exact-match filter stops finding the collection whenever the library name differs only in
  case, which fails the whole HARD keep-list sync and leaves the scan unable to run.
* **A configured tag that will not resolve.** A tag that is absent upstream is
  indistinguishable from one the operator renamed there, and a rename withdraws protection
  from every title still carrying it. So every configured tag must resolve, even under match
  ANY, where only one tag matching is normally enough.

The three states a fetch can be in stay distinguishable throughout: a container that is
missing, one that is present and genuinely empty, and one that is populated with rows that
carry no usable id.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from reaper.clients.base import IntegrationError
from reaper.config import Settings
from reaper.db.session import create_engine
from reaper.engine.fields import Condition, CustomProtectGate, Op
from reaper.engine.gates import PROTECT
from reaper.engine.observation import Absent, Known
from reaper.engine.policy import DEFAULT_TAG_LIST_NAME
from reaper.services.lists import (
    ArrTagRule,
    ContainerMissingError,
    ListKind,
    PlexCollection,
    configured,
    load_membership_index,
    on_list_fact,
    sync,
)
from reaper.services.snapshot import protection_sync_degradations
from tests._fakes import FakeSonarr
from tests.test_engine_invariants import _ALL_READABLE


@pytest.fixture
async def engine(tmp_path: Path) -> AsyncIterator[AsyncEngine]:
    eng = create_engine(Settings(data_dir=tmp_path, secret_key="k"))
    yield eng
    await eng.dispose()


class _FakePlexServer:
    """One library, titled however the test spells it, holding one collection."""

    def __init__(self, section_title: str, *, collection: str = "Never Reap") -> None:
        self.library = self._Library(section_title, collection)

    class _Library:
        def __init__(self, section_title: str, collection: str) -> None:
            self._section = _FakePlexServer._Section(section_title, collection)

        def sections(self) -> list[object]:
            return [self._section]

    class _Section:
        def __init__(self, title: str, collection: str) -> None:
            self.title = title
            self._collection = collection

        def collection(self, name: str) -> object:
            from plexapi.exceptions import NotFound

            if name != self._collection:
                raise NotFound("no such collection")
            return _FakePlexServer._Collection()

    class _Collection:
        def items(self) -> list[object]:
            return [
                SimpleNamespace(
                    type="movie",
                    title="A title",
                    guids=[SimpleNamespace(id="imdb://tt0000001")],
                    guid=None,
                )
            ]


class TestTheLibraryNameIsMatchedCaseFolded:
    """The library name comparison must be case-insensitive, or an exact-match filter
    silently stops finding the keep collection whenever the operator's library name differs
    only in case. That reads as a missing library, which fails the HARD keep-list sync and
    degrades every scan. A working keep list turns into a permanently un-executable install."""

    @pytest.mark.parametrize("spelling", ["Movies", "movies", "MOVIES", "  Movies  "])
    async def test_the_collection_is_found_whatever_the_case(self, spelling: str) -> None:
        provider = PlexCollection(server=_FakePlexServer(spelling), section_name="Movies")

        items = await provider.fetch()

        assert [i.imdb_id for i in items] == ["tt0000001"]

    async def test_a_genuinely_different_library_name_still_fails(self) -> None:
        """The other direction: folding case must not turn a library that is not there into
        a match, or the operator never learns they named the wrong one."""
        provider = PlexCollection(server=_FakePlexServer("TV Shows"), section_name="Movies")

        with pytest.raises(IntegrationError) as caught:
            await provider.fetch()

        assert 'no library called "Movies" anymore' in str(caught.value)

    async def test_a_missing_collection_is_still_a_missing_container(self) -> None:
        """The library is there and the collection is not. This case must stay
        distinguishable, because ``sync`` may otherwise read it as a genuinely empty first
        sync."""
        provider = PlexCollection(
            server=_FakePlexServer("movies", collection="Something Else"),
            section_name="Movies",
        )

        with pytest.raises(ContainerMissingError):
            await provider.fetch()


class TestEveryConfiguredKeepTagMustResolve:
    """Every configured keep tag must resolve, even under match ANY. If only one tag needed
    to resolve, a keep tag the operator renamed in their *arr would silently drop every title
    carrying it from the keep list while the settings screen still read healthy. An absent
    tag and a renamed one look like the same fetch failure, so both must fail the sync."""

    @staticmethod
    def _sonarr(*labels: str, tagged: bool = True) -> FakeSonarr:
        tags = [{"id": i, "label": label} for i, label in enumerate(labels, start=1)]
        series = [{"title": "A", "tvdbId": 10, "tags": [1]}] if tagged and tags else []
        return FakeSonarr(tag_rows=tags, series_rows=series)

    async def test_a_missing_tag_under_any_keeps_the_stored_membership(
        self, engine: AsyncEngine
    ) -> None:
        rule = ArrTagRule(self._sonarr("keep", "gold"), ("keep", "gold"), "any")
        assert await sync(engine, rule, kind=ListKind.WHITELIST) == 1

        # "gold" was renamed upstream. "keep" still resolves, which is exactly the case that
        # must fail the sync instead of quietly dropping everything the renamed tag
        # protected.
        renamed = ArrTagRule(self._sonarr("keep"), ("keep", "gold"), "any")
        with pytest.raises(IntegrationError) as caught:
            await sync(engine, renamed, kind=ListKind.WHITELIST)

        assert "'gold'" in str(caught.value)
        index = await load_membership_index(engine)
        assert index.lookup(media_type="tv", tvdb_id=10)  # the swap never ran

    async def test_on_a_first_sync_it_is_an_error_not_an_empty_list(
        self, engine: AsyncEngine
    ) -> None:
        """The trap in the strict direction. With nothing stored, a ``ContainerMissingError``
        could be misread as a genuinely empty first sync. Routing the partial case there
        would store the surviving tag's members as an empty list and report the list
        healthy, so the tags that do resolve would protect nothing. This must be a plain
        failure instead, so the scan degrades."""
        rule = ArrTagRule(self._sonarr("keep"), ("keep", "gold"), "any")

        with pytest.raises(IntegrationError):
            await sync(engine, rule, kind=ListKind.WHITELIST)

        row = next(r for r in await configured(engine) if r.slug == rule.slug)
        assert row.last_error
        assert row.last_synced_at is None
        reasons = await protection_sync_degradations(engine, {rule.slug: "error: partial"})
        assert reasons  # a scan holding this may not delete anything

    async def test_a_tag_nobody_has_created_yet_is_still_a_quiet_first_sync(
        self, engine: AsyncEngine
    ) -> None:
        """The bound on the rule above. A fresh install has no 'reaper-keep' tag in its *arr,
        and nothing is protecting anything yet, so a first sync that finds no configured tag
        stays an empty success. Making that an error would leave every new install unable to
        scan out of the box."""
        rule = ArrTagRule(self._sonarr("other"), ("reaper-keep",), "any")

        assert await sync(engine, rule, kind=ListKind.WHITELIST) == 0

    async def test_a_present_but_unused_tag_syncs_as_genuinely_empty(
        self, engine: AsyncEngine
    ) -> None:
        """The tag exists and nothing carries it. That must read as an empty list, not a
        missing container, so it can empty the stored membership. Otherwise, untagging the
        last title could never take effect."""
        rule = ArrTagRule(self._sonarr("keep"), ("keep",), "any")
        assert await sync(engine, rule, kind=ListKind.WHITELIST) == 1

        untagged = ArrTagRule(self._sonarr("keep", tagged=False), ("keep",), "any")
        assert await sync(engine, untagged, kind=ListKind.WHITELIST) == 0

        index = await load_membership_index(engine)
        assert not index.lookup(media_type="tv", tvdb_id=10)

    async def test_a_populated_tag_whose_titles_carry_no_ids_keeps_the_membership(
        self, engine: AsyncEngine
    ) -> None:
        """The third state: the tag resolves, titles carry it, and not one of them can be
        identified. A non-empty fetch that filters down to zero must be a failure, never an
        empty success, or the membership swap would wipe a keep list during an upstream id
        outage."""
        rule = ArrTagRule(self._sonarr("keep"), ("keep",), "any")
        assert await sync(engine, rule, kind=ListKind.WHITELIST) == 1

        idless = FakeSonarr(
            tag_rows=[{"id": 1, "label": "keep"}],
            series_rows=[{"title": "A", "tags": [1]}],  # no tvdbId, no imdbId
        )
        with pytest.raises(ContainerMissingError):
            await sync(engine, ArrTagRule(idless, ("keep",), "any"), kind=ListKind.WHITELIST)

        index = await load_membership_index(engine)
        assert index.lookup(media_type="tv", tvdb_id=10)

    async def test_under_all_a_missing_tag_is_still_a_missing_container(
        self, engine: AsyncEngine
    ) -> None:
        """Unchanged, and deliberately: under ALL an absent tag rules every title out, so an
        empty membership is the arithmetically correct answer when nothing is stored yet."""
        rule = ArrTagRule(self._sonarr("keep"), ("keep", "gold"), "all")

        with pytest.raises(ContainerMissingError):
            await rule.fetch()


class TestTheNameAKeepRuleMatches:
    """A list protects through a keep rule naming it, so the name the scan compares against
    has to be the one the operator typed on Settings -> Lists.

    A tag list is one stored row per *arr instance, and its display name says which one
    ("Keepers (4k)"), while the rule itself stores "Keepers" once. ``on_list`` matches per
    element and exactly, so the fact built for matching must use the rule name, not the
    display name. Every *arr instance carries a name, so this affects every tag list,
    including the one a fresh install ships.

    This is driven through the real ``sync`` and ``load_membership_index`` and evaluated by
    the real gate, because each half looks correct alone. The row stores what it displays,
    and the matcher matches what it is given.
    """

    @staticmethod
    def _sonarr() -> FakeSonarr:
        return FakeSonarr(
            tag_rows=[{"id": 1, "label": "reaper-keep"}],
            series_rows=[{"title": "A", "tvdbId": 10, "tags": [1]}],
        )

    @staticmethod
    def _gate(names: str) -> CustomProtectGate:
        return CustomProtectGate(condition=Condition(field="on_list", op=Op.EQ, value=names))

    @pytest.mark.parametrize("instance", ["4k", "HD", None])
    async def test_the_shipped_keep_rule_fires_whatever_the_instance_is_called(
        self, engine: AsyncEngine, instance: str | None
    ) -> None:
        """Swept over instance names because a fixture that reaches for just one value (an
        unnamed instance) would hide a bug here. An unnamed instance appends nothing to the
        display name, so matching would look correct even if the matcher used the wrong name
        field."""
        rule = ArrTagRule(
            self._sonarr(),
            ("reaper-keep",),
            "any",
            instance_id=1,
            instance_name=instance,
            list_id=7,
            list_name=DEFAULT_TAG_LIST_NAME,
        )
        await sync(engine, rule, kind=ListKind.WHITELIST)

        index = await load_membership_index(engine)
        found = index.lookup(media_type="tv", tvdb_id=10)
        facts = replace(_ALL_READABLE, on_lists=on_list_fact(found))
        result = self._gate(DEFAULT_TAG_LIST_NAME).evaluate(facts)

        assert result.outcome == PROTECT
        assert result.blocked is False

    async def test_the_operator_still_sees_which_server_a_row_came_from(
        self, engine: AsyncEngine
    ) -> None:
        """The two names stay separate rather than collapsing into one. The row still says
        which *arr it is, which is what the Lists screen and the degraded-scan message name
        when one instance's check fails."""
        rule = ArrTagRule(
            self._sonarr(),
            ("reaper-keep",),
            "any",
            instance_id=1,
            instance_name="4k",
            list_id=7,
            list_name="Keepers",
        )
        await sync(engine, rule, kind=ListKind.WHITELIST)

        row = next(r for r in await configured(engine) if r.slug == rule.slug)
        stored = index_row = (await load_membership_index(engine)).lookup(
            media_type="tv", tvdb_id=10
        )[0]

        assert row.display_name == "Keepers (4k)"
        assert stored.display_name == "Keepers (4k)"
        assert index_row.matched_by() == "Keepers"

    async def test_one_list_across_four_servers_reads_as_one_name(
        self, engine: AsyncEngine
    ) -> None:
        """Four stored rows, one instruction. The fact says the list once, or a rule using
        ``in`` against a list of names would see the same list four times."""
        for instance_id, name in ((1, "HD"), (2, "4k"), (3, "kids"), (4, "anime")):
            await sync(
                engine,
                ArrTagRule(
                    self._sonarr(),
                    ("reaper-keep",),
                    "any",
                    instance_id=instance_id,
                    instance_name=name,
                    list_id=7,
                    list_name="Keepers",
                ),
                kind=ListKind.WHITELIST,
            )

        found = (await load_membership_index(engine)).lookup(media_type="tv", tvdb_id=10)

        assert len(found) == 4
        assert on_list_fact(found) == Known(value="Keepers", source="lists")

    async def test_a_row_stored_before_the_column_existed_keeps_its_old_spelling(
        self, engine: AsyncEngine
    ) -> None:
        """The widened database's fallback. A row synced by an older build has no stored rule
        name, and reads as its display name instead. That is exactly what it was matched by
        before the column existed, so widening the schema never withdraws a protection that
        was working."""
        rule = ArrTagRule(
            self._sonarr(),
            ("reaper-keep",),
            "any",
            list_id=7,
            list_name="Keepers",
        )
        await sync(engine, rule, kind=ListKind.WHITELIST)
        async with engine.begin() as conn:
            await conn.execute(text("UPDATE protection_list SET rule_name = NULL"))

        found = (await load_membership_index(engine)).lookup(media_type="tv", tvdb_id=10)

        assert on_list_fact(found) == Known(value=rule.display_name, source="lists")

    async def test_an_item_on_no_list_is_a_checked_miss_not_an_unreadable_one(self) -> None:
        """This must read as Absent, never Unknown. The gate reports a checked miss rather
        than blocking, which is the difference between "we looked" and "we could not
        look"."""
        assert on_list_fact([]) == Absent(source="lists")
