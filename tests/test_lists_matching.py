# SPDX-License-Identifier: AGPL-3.0-or-later
"""Name matching and container absence on the keep-list path.

Two ways a keep list stops protecting without saying so, both proven here:

* **A name that only differs in case.** The keep collection is looked for in the
  library the operator named, and the comparison must be case-folded on BOTH sides
  (rule 88). An exact-match filter stopped finding the collection of anyone whose
  library is spelled "movies", which failed the whole HARD keep-list sync and left
  every scan un-executable.
* **A configured tag that will not resolve.** A tag that is absent upstream is
  indistinguishable from one the operator RENAMED there, and a rename withdraws the
  protection from every title still carrying it. So every configured tag has to
  resolve, including under match ANY, where a sibling tag resolving used to be enough
  to sync and atomically replace the membership (rule 27).

The three states a fetch can be in stay distinguishable throughout: a container that is
missing, one that is present and genuinely empty, and one that is populated with rows
that carry no usable id (rule 90).
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
from tests.test_engine_invariants import _ALL_READABLE


@pytest.fixture
async def engine(tmp_path: Path) -> AsyncIterator[AsyncEngine]:
    eng = create_engine(Settings(data_dir=tmp_path, secret_key="k"))  # type: ignore[call-arg]
    yield eng
    await eng.dispose()


class _FakeSonarr:
    """A Sonarr stand-in: not a RadarrClient, so ``ArrTagRule`` takes the series path."""

    service = "sonarr"

    def __init__(self, tags: list[dict[str, object]], series: list[dict[str, object]]) -> None:
        self._tags = tags
        self._series = series

    async def tags(self) -> list[dict[str, object]]:
        return self._tags

    async def series(self) -> list[dict[str, object]]:
        return self._series


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
    """Rule 88. ``library.section(title)`` -- the call the section filter replaced -- matched
    case-insensitively, so the exact-match filter that followed it silently stopped finding
    the keep collection of an operator whose library is spelled in a different case. That
    reads as a missing LIBRARY, which fails the HARD keep-list sync, which degrades every
    scan: a working keep list turned into a permanently un-executable install."""

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

        assert "there is no library called 'Movies'" in str(caught.value)

    async def test_a_missing_collection_is_still_a_missing_container(self) -> None:
        """The library is there and the collection is not: the case that must stay
        distinguishable, because ``sync`` may read it as a genuinely empty first sync."""
        provider = PlexCollection(
            server=_FakePlexServer("movies", collection="Something Else"),
            section_name="Movies",
        )

        with pytest.raises(ContainerMissingError):
            await provider.fetch()


class TestEveryConfiguredKeepTagMustResolve:
    """Under match ANY, one tag resolving used to be enough: the sync succeeded, atomically
    replaced the membership, and cleared ``last_error``. So a keep tag the operator RENAMED
    in their *arr took every title carrying it off the keep list, while the settings screen
    read healthy. An absent tag and a renamed one are the same fetch, so both fail."""

    @staticmethod
    def _sonarr(*labels: str, tagged: bool = True) -> _FakeSonarr:
        tags = [{"id": i, "label": label} for i, label in enumerate(labels, start=1)]
        series = [{"title": "A", "tvdbId": 10, "tags": [1]}] if tagged and tags else []
        return _FakeSonarr(tags, series)

    async def test_a_missing_tag_under_any_keeps_the_stored_membership(
        self, engine: AsyncEngine
    ) -> None:
        rule = ArrTagRule(self._sonarr("keep", "gold"), ("keep", "gold"), "any")  # type: ignore[arg-type]
        assert await sync(engine, rule, kind=ListKind.WHITELIST) == 1

        # "gold" was renamed upstream. "keep" still resolves, which is exactly the case
        # that used to sync happily and drop everything the renamed tag protected.
        renamed = ArrTagRule(self._sonarr("keep"), ("keep", "gold"), "any")  # type: ignore[arg-type]
        with pytest.raises(IntegrationError) as caught:
            await sync(engine, renamed, kind=ListKind.WHITELIST)

        assert "'gold'" in str(caught.value)
        index = await load_membership_index(engine)
        assert index.lookup(media_type="tv", tvdb_id=10)  # the swap never ran

    async def test_on_a_first_sync_it_is_an_error_not_an_empty_list(
        self, engine: AsyncEngine
    ) -> None:
        """The trap in the strict direction: with nothing stored, a ContainerMissingError
        is read as a genuinely empty first sync. Routing the partial case there would store
        the SURVIVING tag's members as [] and report the list healthy, so the tags that do
        resolve would protect nothing. It is a plain failure instead, and the scan degrades."""
        rule = ArrTagRule(self._sonarr("keep"), ("keep", "gold"), "any")  # type: ignore[arg-type]

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
        """The bound on the rule above. A fresh install has no 'reaper-keep' tag in its
        *arr, and nothing is protecting anything yet, so a first sync that finds NO
        configured tag stays an empty success. Making that an error would leave every new
        install un-scannable out of the box."""
        rule = ArrTagRule(self._sonarr("other"), ("reaper-keep",), "any")  # type: ignore[arg-type]

        assert await sync(engine, rule, kind=ListKind.WHITELIST) == 0

    async def test_a_present_but_unused_tag_syncs_as_genuinely_empty(
        self, engine: AsyncEngine
    ) -> None:
        """Rule 27's other side: the tag exists and nothing carries it. That is an empty
        list, not a missing container, and it must be able to empty the stored membership --
        otherwise un-tagging your last title could never take effect."""
        rule = ArrTagRule(self._sonarr("keep"), ("keep",), "any")  # type: ignore[arg-type]
        assert await sync(engine, rule, kind=ListKind.WHITELIST) == 1

        untagged = ArrTagRule(self._sonarr("keep", tagged=False), ("keep",), "any")  # type: ignore[arg-type]
        assert await sync(engine, untagged, kind=ListKind.WHITELIST) == 0

        index = await load_membership_index(engine)
        assert not index.lookup(media_type="tv", tvdb_id=10)

    async def test_a_populated_tag_whose_titles_carry_no_ids_keeps_the_membership(
        self, engine: AsyncEngine
    ) -> None:
        """Rule 90, the third state: the tag resolves, titles carry it, and not one of them
        can be identified. A non-empty fetch that filters to zero is a failure, never an
        empty success, or the swap wipes a keep list over an upstream id outage."""
        rule = ArrTagRule(self._sonarr("keep"), ("keep",), "any")  # type: ignore[arg-type]
        assert await sync(engine, rule, kind=ListKind.WHITELIST) == 1

        idless = _FakeSonarr(
            [{"id": 1, "label": "keep"}],
            [{"title": "A", "tags": [1]}],  # no tvdbId, no imdbId
        )
        with pytest.raises(ContainerMissingError):
            await sync(engine, ArrTagRule(idless, ("keep",), "any"), kind=ListKind.WHITELIST)  # type: ignore[arg-type]

        index = await load_membership_index(engine)
        assert index.lookup(media_type="tv", tvdb_id=10)

    async def test_under_all_a_missing_tag_is_still_a_missing_container(
        self, engine: AsyncEngine
    ) -> None:
        """Unchanged, and deliberately: under ALL an absent tag rules every title out, so an
        empty membership is the arithmetically correct answer when nothing is stored yet."""
        rule = ArrTagRule(self._sonarr("keep"), ("keep", "gold"), "all")  # type: ignore[arg-type]

        with pytest.raises(ContainerMissingError):
            await rule.fetch()


class TestTheNameAKeepRuleMatches:
    """A list protects through a keep rule naming it, so the name the scan compares against
    has to be the one the operator typed on Settings -> Lists.

    A tag list is one stored row per *arr instance and its DISPLAY name says which ("Keepers
    (4k)"), while the rule stores "Keepers" once. ``on_list`` matches per element and
    exactly, so building the fact from display names matched nothing: every tag list on a
    named instance protected nothing while the Lists screen reported it healthy (#507). Every
    *arr instance carries a name, so that was every tag list, including the one a fresh
    install ships.

    Driven through the real ``sync`` and ``load_membership_index`` and evaluated by the real
    gate, because each half looked correct alone -- the row stored what it displayed, and the
    matcher matched what it was given.
    """

    @staticmethod
    def _sonarr() -> _FakeSonarr:
        return _FakeSonarr(
            [{"id": 1, "label": "reaper-keep"}],
            [{"title": "A", "tvdbId": 10, "tags": [1]}],
        )

    @staticmethod
    def _gate(names: str) -> CustomProtectGate:
        return CustomProtectGate(condition=Condition(field="on_list", op=Op.EQ, value=names))

    @pytest.mark.parametrize("instance", ["4k", "HD", None])
    async def test_the_shipped_keep_rule_fires_whatever_the_instance_is_called(
        self, engine: AsyncEngine, instance: str | None
    ) -> None:
        """Swept over instance names because the bug was invisible at the one value a
        fixture reaches for: an unnamed instance appends nothing and matched all along
        (rule 141)."""
        rule = ArrTagRule(
            self._sonarr(),  # type: ignore[arg-type]
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
        """The two names are kept apart rather than collapsed: the row still says which *arr
        it is, which is what the Lists screen and the degraded-scan sentence name when one
        instance's check fails."""
        rule = ArrTagRule(
            self._sonarr(),  # type: ignore[arg-type]
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
                    self._sonarr(),  # type: ignore[arg-type]
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
        """The widened database's fallback. A row synced by an older build has no stored
        rule name, and reads as its display name -- which is exactly what it was matched by
        then, so widening never withdraws a protection that was working."""
        rule = ArrTagRule(
            self._sonarr(),  # type: ignore[arg-type]
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
        """Absent, never Unknown: the gate reports a checked miss rather than blocking, which
        is the difference between "we looked" and "we could not look" (rule 93)."""
        assert on_list_fact([]) == Absent(source="lists")
