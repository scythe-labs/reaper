# SPDX-License-Identifier: AGPL-3.0-or-later
"""Client stand-ins that the type checker holds to the real client's shape.

Every fake here **inherits the client it stands for**, and the whole of ``tests/`` is on the
mypy gate (``uv run mypy src/reaper tests/``). Both halves are load-bearing, and neither works
alone:

* Inheriting means an override whose parameter types no longer match the real method is a
  Liskov violation, which mypy reports.
* Being on the gate is what makes that report reach anybody. Until #580 nothing under
  ``tests/`` was checked, so the 252 ``# type: ignore`` comments there suppressed nothing.

**What that buys, precisely, is the drift a test run cannot see.** A change to a method's
*shape* -- a parameter added, renamed or removed -- already fails loudly today: the production
caller passes the new argument, the fake does not accept it, and the suite raises ``TypeError``.
Measured on this tree: adding one keyword-only argument to ``TautulliClient.children_metadata``
turned 44 tests red while ``mypy src/reaper`` stayed green. What nothing caught was a change to
a *type* alone, since Python does not enforce annotations at runtime and the fake was checked by
nobody. That is the gap these classes close, and it is the one phase 8's ``clients/arr.py`` work
needs closed before it starts. It caught one the moment it was switched on: ``test_launcher``'s
``_FakeServer.run()`` took no ``sockets`` argument, where the ``uvicorn.Server`` it stands in
for does.

**A structural fake anywhere in ``tests/`` inherits its real class for this reason**, not only
the ones in this module -- ``test_degraded_side_effects``'s ``_RawLibraries`` and
``test_kdf_and_session_upkeep``'s ``_Client`` are the same pattern kept beside the cases that
need a shape this module cannot build.

The constructors deliberately do **not** call ``super().__init__``, so no fake owns an
``httpx2.AsyncClient`` and none can reach the network. A method that is inherited rather than
overridden therefore fails on a missing ``_client`` instead of quietly opening a socket, which
is the failure worth having (rule 37).

**Not everything belongs here.** ``test_reap_loop.py``'s four deletion-path simulators stay in
that file: each is tuned by several subclasses declared beside it, and separating a simulator
from the tests that dictate its failure modes is the wrong trade on the one path that deletes.
They inherit their real client in place for the same reason these do.
"""

from __future__ import annotations

from typing import Any

from reaper.clients.arr import RadarrClient, SonarrClient
from reaper.clients.base import IntegrationError
from reaper.clients.seerr import (
    MediaRequest,
    QuotaStatus,
    SeerrClient,
    SeerrUser,
    TitleInfo,
    UserQuota,
)
from reaper.clients.tautulli import TautulliClient

#: No limit of any kind, which is what a user with no quota configured reads as.
UNLIMITED_QUOTA = QuotaStatus(limit=None, days=None, used=0, remaining=None, restricted=False)

# ---------------------------------------------------------------------------
# Tautulli
# ---------------------------------------------------------------------------


class FakeTautulli(TautulliClient):
    """Tautulli's read surface over canned rows, one page per section.

    ``sections`` maps a section id to its rows and its type, which is what lets one class
    stand for the single-library scan and the several-library one alike -- a per-library
    bound cannot be exercised against a single bucket, because there the share IS the
    overall share and the test passes whether the count is bucketed or not.

    Every page after the first comes back empty, which is how the real sweep terminates.
    """

    def __init__(
        self,
        *,
        sections: dict[int, list[dict[str, Any]]] | None = None,
        section_types: dict[int, str] | None = None,
        children: dict[int, list[dict[str, Any]]] | None = None,
        sessions: list[dict[str, Any]] | None = None,
        user_rows: list[dict[str, Any]] | None = None,
        fail_libraries: bool = False,
        fail_users: bool = False,
    ) -> None:
        self._sections = sections or {}
        self._section_types = section_types or {}
        self._children = children or {}
        self._sessions = sessions or []
        self._user_rows = user_rows or []
        self._fail_libraries = fail_libraries
        self._fail_users = fail_users

    async def libraries(self) -> list[dict[str, Any]]:
        if self._fail_libraries:
            raise IntegrationError("tautulli", "libraries unavailable")
        return [
            {
                "section_id": sid,
                "section_type": self._section_types.get(sid, "movie"),
                "section_name": f"Library {sid}",
            }
            for sid in self._sections
        ]

    async def library_media_info(
        self,
        section_id: int,
        *,
        start: int = 0,
        length: int = 100,
        order_column: str = "added_at",
        order_dir: str = "desc",
    ) -> dict[str, Any]:
        return {"data": self._sections.get(section_id, []) if start == 0 else []}

    async def children_metadata(self, rating_key: int) -> list[dict[str, Any]]:
        return self._children.get(rating_key, [])

    async def activity(self) -> dict[str, Any]:
        return {"sessions": self._sessions}

    async def users(self) -> list[dict[str, Any]]:
        if self._fail_users:
            raise IntegrationError("tautulli", "users unavailable")
        return list(self._user_rows)


def movie_library(rows: list[dict[str, Any]], *, section_id: int = 1) -> FakeTautulli:
    """The one-movie-library shape five suites want, without spelling the two maps out."""
    return FakeTautulli(sections={section_id: rows}, section_types={section_id: "movie"})


def show_library(
    rows: list[dict[str, Any]],
    *,
    children: dict[int, list[dict[str, Any]]] | None = None,
    section_id: int = 3,
) -> FakeTautulli:
    """The same for a show library, whose seasons come back from ``children_metadata``."""
    return FakeTautulli(
        sections={section_id: rows},
        section_types={section_id: "show"},
        children=children or {},
    )


def scan_library(
    *,
    movies: list[dict[str, Any]] | None = None,
    shows: list[dict[str, Any]] | None = None,
    children: dict[int, list[dict[str, Any]]] | None = None,
    sessions: list[dict[str, Any]] | None = None,
    fail_libraries: bool = False,
) -> FakeTautulli:
    """Both libraries at once, which is the shape the whole-scan tests drive.

    Section 1 is the movie library and section 3 the show library, the ids those suites
    already used, so what a row belongs to is readable from the id in the assertion.
    """
    return FakeTautulli(
        sections={1: movies or [], 3: shows or []},
        section_types={1: "movie", 3: "show"},
        children=children,
        sessions=sessions,
        fail_libraries=fail_libraries,
    )


class PagingTautulli(TautulliClient):
    """History as the incremental sync sees it: newest first, filtered by ``after``, paged.

    Records the ``after`` of each call so a test can assert an incremental sync fetched a few
    rows rather than all of them. Separate from ``FakeTautulli`` because it simulates the
    server's own filter and paging, where the others serve one canned page; folding the two
    together would produce a class whose behavior no caller could predict from its arguments.
    """

    def __init__(self, rows: list[dict[str, Any]], *, total: int | None = None) -> None:
        self.rows = rows
        self.total = total if total is not None else len(rows)
        self.after_calls: list[str | None] = []
        #: The read budget each call arrived with, probe included, so a test can tell the
        #: sweep's own budget from the client-wide one every other caller keeps.
        self.read_timeouts: list[float | None] = []
        #: ``include_activity`` as each call sent it, probe included. ``None`` is the
        #: client's default and means Tautulli decides, so a walk that omits it is visible.
        self.include_activity: list[int | None] = []

    async def history(
        self,
        *,
        rating_key: int | None = None,
        parent_rating_key: int | None = None,
        grandparent_rating_key: int | None = None,
        user_id: int | None = None,
        after: str | None = None,
        length: int = 100,
        start: int = 0,
        grouping: int = 0,
        include_activity: int | None = None,
        read_timeout: float | None = None,
    ) -> dict[str, Any]:
        self.read_timeouts.append(read_timeout)
        self.include_activity.append(include_activity)
        # The length=1 probe the regression check makes does not count as a page fetch.
        if length > 1:
            self.after_calls.append(after)

        served = self.rows
        if after is not None:
            cutoff = _date_to_epoch(after)
            served = [r for r in self.rows if int(r["date"]) >= cutoff]

        window = served[start : start + length]
        return {
            "data": window,
            "recordsFiltered": len(served),
            "recordsTotal": self.total,
        }


def _date_to_epoch(date_str: str) -> int:
    """``YYYY-MM-DD`` to the epoch second Tautulli would filter from."""
    from datetime import UTC, datetime

    return int(datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=UTC).timestamp())


# ---------------------------------------------------------------------------
# Sonarr and Radarr
# ---------------------------------------------------------------------------


class FakeSonarr(SonarrClient):
    """Sonarr's read surface: the series list, its tags, its root folders, its episodes.

    ``service`` and the class identity both come from the real client, which is what the
    keep-tag rule branches on -- it takes the series path for anything that is not a
    ``RadarrClient``, and four separate stand-ins used to assert that with a hand-written
    ``service = "sonarr"`` class attribute instead.
    """

    def __init__(
        self,
        *,
        series_rows: list[dict[str, Any]] | None = None,
        tag_rows: list[dict[str, Any]] | None = None,
        episode_rows: dict[int, list[dict[str, Any]]] | None = None,
        root_path: str = "/data/tv",
        root_accessible: bool = True,
        fail_series: bool = False,
        fail_tags: bool = False,
    ) -> None:
        self._series = series_rows or []
        self._tags = tag_rows or []
        self._episodes = episode_rows or {}
        self._root_path = root_path
        self._root_accessible = root_accessible
        self._fail_series = fail_series
        self._fail_tags = fail_tags
        self.episodes_called: list[int] = []

    async def series(self) -> list[dict[str, Any]]:
        if self._fail_series:
            raise IntegrationError("sonarr", "unreachable")
        return self._series

    async def tags(self) -> list[dict[str, Any]]:
        # A bare RuntimeError rather than IntegrationError: this stands for the *arr being
        # unreachable at the socket, which is the shape the sync's own handler has to
        # survive, and mapping it to a domain error here would test the mapping instead.
        if self._fail_tags:
            raise RuntimeError("connection refused")
        return self._tags

    async def root_folders(self) -> list[dict[str, Any]]:
        if self._fail_series:
            raise IntegrationError("sonarr", "unreachable")
        return [{"path": self._root_path, "accessible": self._root_accessible}]

    async def episodes(self, series_id: int) -> list[dict[str, Any]]:
        self.episodes_called.append(series_id)
        return self._episodes.get(series_id, [])


class FakeRadarr(RadarrClient):
    """Radarr's read surface for the scan: the movie list and the root folders it sits on."""

    def __init__(
        self,
        *,
        movie_rows: list[dict[str, Any]] | None = None,
        root_path: str = "/data/movies",
        root_accessible: bool = True,
        fail_movies: bool = False,
    ) -> None:
        self._movies = movie_rows or []
        self._root_path = root_path
        self._root_accessible = root_accessible
        self._fail_movies = fail_movies

    async def movies(self) -> list[dict[str, Any]]:
        if self._fail_movies:
            raise IntegrationError("radarr", "unreachable (boom)")
        return self._movies

    async def root_folders(self) -> list[dict[str, Any]]:
        if self._fail_movies:
            raise IntegrationError("radarr", "unreachable (boom)")
        return [{"path": self._root_path, "accessible": self._root_accessible}]


# ---------------------------------------------------------------------------
# Seerr
# ---------------------------------------------------------------------------


class FakeSeerr(SeerrClient):
    """A request portal over canned requests, users, quotas and titles.

    ``instance_key``, ``base_url`` and ``link_base_url`` are attributes rather than methods
    because the fairness report reads them straight off the client to build the row's link.
    """

    def __init__(
        self,
        requests: list[MediaRequest] | None = None,
        users: list[SeerrUser] | None = None,
        quotas: dict[int, UserQuota] | None = None,
        titles: dict[int, TitleInfo] | None = None,
        *,
        instance_key: str = "",
        base_url: str = "https://seerr.example",
        link_base_url: str | None = None,
        unreachable: bool = False,
    ) -> None:
        self._requests = requests or []
        self._users = users or []
        self._quotas = quotas or {}
        self._titles = titles or {}
        self._unreachable = unreachable
        self.instance_key = instance_key
        self.base_url = base_url
        self.link_base_url = link_base_url

    async def all_requests(self, *, filter_: str = "available") -> list[MediaRequest]:
        if self._unreachable:
            raise IntegrationError("seerr", "down")
        return self._requests

    async def users(self, *, take: int = 100) -> list[SeerrUser]:
        if self._unreachable:
            raise IntegrationError("seerr", "down")
        return self._users

    async def quota(self, user_id: int) -> UserQuota:
        if self._unreachable:
            raise IntegrationError("seerr", "down")
        return self._quotas.get(user_id, UserQuota(movie=UNLIMITED_QUOTA, tv=UNLIMITED_QUOTA))

    async def title(self, *, tmdb_id: int, media_type: str) -> TitleInfo:
        info = self._titles.get(tmdb_id)
        if info is None:
            raise IntegrationError("seerr", f"no title for {tmdb_id}")
        return info
