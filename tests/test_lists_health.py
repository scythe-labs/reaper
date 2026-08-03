# SPDX-License-Identifier: AGPL-3.0-or-later
"""What the Lists screen says about a protection list, and when it says it (#475).

The columns this reads were written on every sync since lists shipped, and nothing ever
showed them. The states are pinned here because each one asks the operator for something
different: a failing list with members still covers them and can wait, a failing list with
none is protecting nothing right now, and a keep list that merely went stale is the early
warning the degraded-scan notice does not give until ``WHITELIST_STALE_AFTER`` has passed.

The staleness bound is not restated here. It is imported from the module that *enforces*
it, so a change to the bound moves the screen and the degradation check together and
cannot leave this test asserting a number the scan no longer uses (rule 144).
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
        copy in place and those titles are still protected. The row still has to say the
        check failed: coasting is a reprieve, not a healthy list, and the only thing that
        ever told the operator was a degraded scan two days later."""
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
        so the staleness bound is a keep-list concern. Applying it here would mark the IMDb
        Top 250 stale on any install whose scheduler paused for two days, over a list whose
        membership had not moved."""
        old = int((NOW - WHITELIST_STALE_AFTER - timedelta(days=30)).timestamp())
        assert _health(_row(kind=ListKind.CURATED.value, last_synced_at=old)) is ListHealth.WORKING


@pytest.fixture
def client(tmp_path: Path) -> Iterator[TestClient]:
    settings = Settings(data_dir=tmp_path, secret_key="k")  # type: ignore[call-arg]
    engine = sa_create_engine(settings.sync_database_url)
    Base.metadata.create_all(engine)
    engine.dispose()
    with TestClient(create_app(settings)) as c:
        login(c, settings)
        # The cache tables are created by ``lists.ensure_schema`` on first read, not at boot,
        # so this is the request a fresh install makes before anything has ever synced. It is
        # also what lets ``_store`` below write to a table that now exists.
        assert c.get("/api/lists").status_code == 200
        yield c


@pytest.fixture
def store(tmp_path: Path) -> Iterator[object]:
    """Write rows into ``protection_list`` the way a sync would, over a SEPARATE sync engine.

    Not the app's own ``cache_engine``: that one is async, and driving it from a synchronous
    test body raises ``MissingGreenlet`` rather than doing anything. A second connection to
    the same file is what the sync itself is, from the scan's perspective.
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
    ) -> None:
        # Every column spelled out rather than assembled from whatever the caller passed. The
        # dynamic version read as a SQL-injection vector to ruff (S608) and it was right to:
        # the column names were interpolated. Naming them also means a column added to the
        # table has to be considered here rather than silently defaulting.
        with engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT OR REPLACE INTO protection_list "
                    "(slug, display_name, mode, kind, enabled, item_count, "
                    " last_synced_at, last_error) "
                    "VALUES (:slug, :display_name, :mode, :kind, :enabled, :item_count, "
                    "        :last_synced_at, :last_error)"
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
        """The whole issue. The message names the thing to go and fix -- a library that is
        not called what Reaper asked for -- and until this route existed it lived only in a
        column nothing read."""
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
        """``retire_absent`` disables a slug the configuration no longer produces and KEEPS
        its members, so a disabled row still reads as populated. Listing it would show a
        healthy-looking keep list protecting nothing -- and a slug carries the match mode, so
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

    def test_the_route_needs_a_session(self, client: TestClient) -> None:
        assert TestClient(client.app).get("/api/lists").status_code == 401  # type: ignore[arg-type]
