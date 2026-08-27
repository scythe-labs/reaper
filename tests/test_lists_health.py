# SPDX-License-Identifier: AGPL-3.0-or-later
"""What the Lists screen says about a protection list, and when it says it.

The columns this test reads were written on every sync since lists shipped, but nothing
displayed them until this screen. The states matter because each one asks the operator for
something different. A failing list with members stored still covers them and can wait. A
failing list with none is protecting nothing right now. A keep list that merely went stale is
an early warning. The degraded-scan notice does not fire until ``WHITELIST_STALE_AFTER`` has
passed.

The staleness bound is not restated here. It is imported from the module that enforces it, so
a change to the bound moves the screen and the degradation check together, and this test can
never assert a number the scan no longer uses.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine as sa_create_engine
from sqlalchemy import text

from reaper.clock import utcnow
from reaper.config import Settings
from reaper.db.base import Base
from reaper.main import create_app
from reaper.services import app_settings
from reaper.services.lists import ConfiguredList, ListHealth, ListKind, ListMode
from reaper.services.snapshot import WHITELIST_STALE_AFTER
from tests._auth import login

NOW = utcnow()


def _row(**over: object) -> ConfiguredList:
    """A healthy keep list, checked a minute ago. Each test spoils exactly one column."""
    base: dict[str, object] = {
        "slug": "sonarr-1-keeptags-any",
        "display_name": "Sonarr tag: reaper-keep",
        "mode": ListMode.HARD.value,
        "kind": ListKind.WHITELIST.value,
        "enabled": True,
        "item_count": 37,
        "last_synced_at": int((NOW - timedelta(minutes=1)).timestamp()),
        "last_error": None,
    }
    return ConfiguredList(**(base | over))  # type: ignore[arg-type]


def _health(row: ConfiguredList) -> ListHealth:
    return row.health(stale_after=WHITELIST_STALE_AFTER, now=NOW)


class TestHealth:
    def test_a_recent_successful_check_is_working(self) -> None:
        assert _health(_row()) is ListHealth.WORKING

    def test_an_error_reads_as_failing_even_with_members_stored(self) -> None:
        """``sync`` swaps membership atomically, so a failed refresh leaves the previous
        copy in place and those titles stay protected. The row must still report the check
        as failed. Coasting on old membership is not a healthy list, and without this state,
        the only thing that would ever tell the operator is a later degraded-scan notice."""
        assert _health(_row(last_error="Sonarr refused the request")) is ListHealth.FAILING

    def test_an_error_outranks_never_checked(self) -> None:
        """A first sync that fails records the error and leaves ``last_synced_at`` NULL,
        so the two conditions are true at once. Reporting "not checked yet" there would
        hide the error, which is the only reason the operator opened this screen."""
        row = _row(
            last_error="there is no library called 'Movies'", last_synced_at=None, item_count=0
        )
        assert _health(row) is ListHealth.FAILING

    def test_a_list_that_has_never_synced_says_so(self) -> None:
        assert _health(_row(last_synced_at=None, item_count=0)) is ListHealth.NEVER_CHECKED

    def test_a_keep_list_past_the_bound_is_stale(self) -> None:
        """The early warning. Every hour past the bound is an hour a newly keep-tagged
        title is unprotected, and the scan does not degrade until the same bound."""
        old = int((NOW - WHITELIST_STALE_AFTER - timedelta(hours=1)).timestamp())
        assert _health(_row(last_synced_at=old)) is ListHealth.STALE

    def test_a_keep_list_just_inside_the_bound_is_still_working(self) -> None:
        fresh = int((NOW - WHITELIST_STALE_AFTER + timedelta(hours=1)).timestamp())
        assert _health(_row(last_synced_at=fresh)) is ListHealth.WORKING

    def test_a_curated_list_does_not_go_stale(self) -> None:
        """A curated external list churns slowly and keeps protecting from its stored copy,
        so the staleness bound is a keep-list concern only. Applying it here would mark a
        list like the IMDb Top 250 stale after any ordinary scheduler pause, even though its
        membership never moved."""
        old = int((NOW - WHITELIST_STALE_AFTER - timedelta(days=30)).timestamp())
        assert _health(_row(kind=ListKind.CURATED.value, last_synced_at=old)) is ListHealth.WORKING


@pytest.fixture
def client(tmp_path: Path) -> Iterator[TestClient]:
    settings = Settings(data_dir=tmp_path, secret_key="k")
    engine = sa_create_engine(settings.sync_database_url)
    Base.metadata.create_all(engine)
    engine.dispose()
    with TestClient(create_app(settings)) as c:
        login(c, settings)
        # The cache tables are created by ``lists.ensure_schema`` on first read, so this is
        # the request a fresh install makes before anything has ever synced. It is also what
        # lets ``_store`` below write to a table that now exists.
        assert c.get("/api/lists").status_code == 200
        yield c


@pytest.fixture
def store(tmp_path: Path) -> Iterator[object]:
    """Write rows into ``protection_list`` the way a sync would, over a second, separate sync
    engine.

    This must not be the app's own ``cache_engine``, which is async. Driving it from a
    synchronous test body raises ``MissingGreenlet`` instead of doing anything. A second
    connection to the same file is exactly what the sync itself is, from the scan's
    perspective.
    """
    engine = sa_create_engine(f"sqlite:///{tmp_path / 'cache.db'}")

    def _store(
        *,
        slug: str,
        display_name: str,
        kind: str = ListKind.WHITELIST.value,
        mode: str = ListMode.HARD.value,
        enabled: int = 1,
        item_count: int = 0,
        last_synced_at: int | None = None,
        last_error: str | None = None,
        stats_json: str | None = None,
    ) -> None:
        # Every column is spelled out instead of assembled from whatever the caller passed.
        # Building the query dynamically would interpolate column names, which is a SQL
        # injection risk. Naming them here also means a column added to the table must be
        # considered here instead of silently defaulting.
        with engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT OR REPLACE INTO protection_list "
                    "(slug, display_name, mode, kind, enabled, item_count, "
                    " last_synced_at, last_error, stats_json) "
                    "VALUES (:slug, :display_name, :mode, :kind, :enabled, :item_count, "
                    "        :last_synced_at, :last_error, :stats_json)"
                ),
                {
                    "slug": slug,
                    "display_name": display_name,
                    "mode": mode,
                    "kind": kind,
                    "enabled": enabled,
                    "item_count": item_count,
                    "last_synced_at": last_synced_at,
                    "last_error": last_error,
                    "stats_json": stats_json,
                },
            )

    yield _store
    engine.dispose()


class TestRoute:
    def test_a_fresh_install_has_no_lists_rather_than_an_error(self, client: TestClient) -> None:
        """Nothing has synced yet, so the table is empty. An empty list is the honest answer
        and the screen renders its own empty state from it."""
        r = client.get("/api/lists")
        assert r.status_code == 200
        assert r.json() == []

    def test_a_row_carries_its_state_and_what_it_still_protects(
        self, client: TestClient, store: Any
    ) -> None:
        store(
            slug="imdb-top-250",
            display_name="IMDb Top 250",
            mode=ListMode.HARD.value,
            kind=ListKind.CURATED.value,
            item_count=250,
            last_synced_at=int((NOW - timedelta(minutes=8)).timestamp()),
        )
        [row] = client.get("/api/lists").json()
        assert row["name"] == "IMDb Top 250"
        assert row["state"] == "working"
        assert row["item_count"] == 250
        assert row["error"] is None
        assert row["last_checked_at"] is not None

    def test_the_error_reaches_the_operator_verbatim(self, client: TestClient, store: Any) -> None:
        """The message tells the operator exactly what to fix, a library whose name does not
        match what Reaper asked for, so they can act without digging further."""
        store(
            slug="plex-collection-never-reap",
            display_name='Plex collection: "Never Reap"',
            mode=ListMode.HARD.value,
            kind=ListKind.WHITELIST.value,
            item_count=0,
            last_error="there is no library called 'Movies'",
        )
        [row] = client.get("/api/lists").json()
        assert row["state"] == "failing"
        assert row["error"] == "there is no library called 'Movies'"
        assert row["item_count"] == 0
        assert row["last_checked_at"] is None

    def test_a_retired_list_is_not_offered_as_one_that_protects(
        self, client: TestClient, store: Any
    ) -> None:
        """``retire_absent`` disables a slug the configuration no longer produces and keeps
        its members, so a disabled row still reads as populated. Listing it would show a
        healthy-looking keep list protecting nothing. A slug also carries the match mode, so
        flipping keep tags from ANY to ALL and back leaves both spellings behind."""
        for slug, enabled in (("sonarr-1-keeptags-any", 0), ("sonarr-1-keeptags-all", 1)):
            store(
                slug=slug,
                display_name=f"Sonarr tag: reaper-keep ({slug})",
                mode=ListMode.HARD.value,
                kind=ListKind.WHITELIST.value,
                enabled=enabled,
                item_count=37,
                last_synced_at=int(NOW.timestamp()),
            )
        slugs = [row["slug"] for row in client.get("/api/lists").json()]
        assert slugs == ["sonarr-1-keeptags-all"]

    def test_a_legacy_row_is_adopted_by_its_definition_on_read(
        self, client: TestClient, store: Any
    ) -> None:
        """An upgrade's membership rows keep their pre-registry slugs until something
        rewrites them. This route adopts them onto their definitions before answering
        (``lists.adopt_legacy``), so the screen renders one editable list per protection
        instead of an uneditable orphan beside a definition protecting nothing."""
        store(
            slug="sonarr-1-keeptags-any",
            display_name="Sonarr tag: reaper-keep",
            item_count=37,
            last_synced_at=int(NOW.timestamp()),
        )
        tag_def = next(
            d for d in client.get("/api/lists/configured").json() if d["source"] == "arr_tag"
        )

        [row] = client.get("/api/lists").json()

        assert row["slug"] == f"sonarr-1-keeptags-any-list{tag_def['id']}"
        assert row["list_id"] == tag_def["id"]
        assert row["item_count"] == 37

    def test_a_tag_list_row_carries_its_per_tag_counts_and_server(
        self, client: TestClient, store: Any
    ) -> None:
        """What the last good check recorded beyond the count: which tags are doing the
        protecting, and which *arr instance the row was read from."""
        store(
            slug="sonarr-1-keeptags-any-list3",
            display_name="Tagged titles",
            item_count=3,
            last_synced_at=int(NOW.timestamp()),
            stats_json='{"tags": {"keep": 2, "gold": 1}, "server": "hd"}',
        )
        [row] = client.get("/api/lists").json()
        assert row["tags"] == {"keep": 2, "gold": 1}
        assert row["server"] == "hd"

    @pytest.mark.parametrize("stored", [None, "not json", '{"tags": "nope", "server": ""}'])
    def test_missing_or_malformed_stats_read_as_unknown_not_zero(
        self, client: TestClient, store: Any, stored: str | None
    ) -> None:
        """A row from before the counts were recorded, or one whose body will not parse, must
        answer null for unknown. It must never answer zero, which would read as these tags
        protecting nothing."""
        store(
            slug="sonarr-1-keeptags-any-list3",
            display_name="Tagged titles",
            item_count=3,
            last_synced_at=int(NOW.timestamp()),
            stats_json=stored,
        )
        [row] = client.get("/api/lists").json()
        assert row["tags"] is None
        assert row["server"] is None

    def test_the_route_needs_a_session(self, client: TestClient) -> None:
        assert TestClient(client.app).get("/api/lists").status_code == 401


class TestAuthorableMedia:
    """The media types the Policy picker offers each list on (``authorable_media``). The scope
    function is unit-tested in ``test_policy``. These tests pin the endpoint wiring: the join
    by ``list_id``, the synced flag read off ``last_synced_at``, and the Plex library read."""

    @staticmethod
    def _tag(client: TestClient) -> dict[str, Any]:
        return next(
            d for d in client.get("/api/lists/configured").json() if d["source"] == "arr_tag"
        )

    def test_an_unsynced_tag_is_offered_on_neither(self, client: TestClient) -> None:
        """A fresh install's keep-tag list has no membership yet. No sync has read what media
        it holds, so a rule on it could keep nothing. It must be offered on neither media
        type, never silently on both."""
        assert self._tag(client)["authorable_media"] == []

    def test_a_synced_but_empty_tag_is_offered_on_both(
        self, client: TestClient, store: Any
    ) -> None:
        """A sync landed and found nothing protectable. Verified but empty must still be
        offered on both media types, so a list the operator means to fill is protectable
        right away. This exercises the ``list_id`` join and the synced flag through the real
        endpoint."""
        tag_id = self._tag(client)["id"]
        store(
            slug=f"sonarr-1-keeptags-any-list{tag_id}",
            display_name="Tagged titles",
            last_synced_at=int(NOW.timestamp()),
        )
        assert sorted(self._tag(client)["authorable_media"]) == ["movie", "tv"]

    def test_a_collection_takes_its_library_kind_without_a_sync(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The Plex exception: a collection lives in one library, whose kind gives the type
        before any sync. A movie library scopes the collection's rule to the Movies policy."""

        async def _libs(_session: Any) -> list[dict[str, Any]]:
            return [{"title": "Films", "kind": "movie"}]

        monkeypatch.setattr(app_settings, "get_plex_libraries", _libs)
        created = client.post(
            "/api/lists/configured",
            json={
                "name": "Keep Films",
                "source": "plex_collection",
                "config": {"library": "Films", "collection": "Keep"},
            },
        )
        assert created.status_code == 201
        assert created.json()["authorable_media"] == ["movie"]
