# SPDX-License-Identifier: AGPL-3.0-or-later
"""Regression tests for the code-review fixes in the ``clients`` lane.

Each test names the defect it guards against. These sit alongside
``test_guarded_transport`` and ``test_upstream_quirks`` -- the safety-critical suites --
because most of what is fixed here is about *failing closed*: a transient blip must not
abort a scan, a plex.tv outage must not become an open door, and a script-bearing image
must not be relayed same-origin.
"""

from __future__ import annotations

import inspect
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any
from unittest import mock

import httpx
import httpx2
import pytest
import requests
import respx

from reaper import logbuffer
from reaper.clients.arr import RadarrClient, SonarrClient
from reaper.clients.base import (
    DEFAULT_TIMEOUT,
    BaseClient,
    IntegrationError,
    SafetyViolationError,
)
from reaper.clients.plex import GuardedSession
from reaper.clients.plextv import PlexTvClient
from reaper.clients.public import PublicClient
from reaper.clients.seerr import SeerrClient
from reaper.clients.tautulli import ALLOWED_IMAGE_TYPES, TautulliClient
from reaper.config import RuntimeSafety
from reaper.logging import configure_logging
from reaper.services.plex_link import (
    PlexLinkError,
    PlexLinkRetryableError,
    reachable_connection,
)

pytestmark = pytest.mark.httpx2(assert_all_called=False)

READ_ONLY = RuntimeSafety(destructive_enabled=False)
ARMED = RuntimeSafety(destructive_enabled=True)


class TestRedirectsNeverCarryCredentialsAway:
    """``follow_redirects`` is off at the client. A read may hop within the configured
    origin only -- the API-key header must never chase a Location elsewhere -- and a
    redirected mutation is refused outright rather than replayed at a new URL."""

    async def test_a_same_origin_redirect_on_a_read_is_followed(
        self, httpx2_mock: respx.Router
    ) -> None:
        httpx2_mock.get("https://radarr.test/api/v3/system/status").mock(
            return_value=httpx.Response(
                301, headers={"location": "https://radarr.test/api/v4/system/status"}
            )
        )
        httpx2_mock.get("https://radarr.test/api/v4/system/status").mock(
            return_value=httpx.Response(200, json={"version": "6.0.0"})
        )
        async with RadarrClient("https://radarr.test", "k", safety=READ_ONLY) as client:
            assert (await client.system_status())["version"] == "6.0.0"

    async def test_a_cross_origin_redirect_on_a_read_is_refused(
        self, httpx2_mock: respx.Router
    ) -> None:
        httpx2_mock.get("https://radarr.test/api/v3/system/status").mock(
            return_value=httpx.Response(301, headers={"location": "https://elsewhere.test/x"})
        )
        async with RadarrClient("https://radarr.test", "k", safety=READ_ONLY) as client:
            with pytest.raises(IntegrationError, match="cross-origin"):
                await client.system_status()

    async def test_a_redirect_loop_gives_up(self, httpx2_mock: respx.Router) -> None:
        httpx2_mock.get("https://radarr.test/api/v3/system/status").mock(
            return_value=httpx.Response(
                302, headers={"location": "https://radarr.test/api/v3/system/status"}
            )
        )
        async with RadarrClient("https://radarr.test", "k", safety=READ_ONLY) as client:
            with pytest.raises(IntegrationError, match="too many redirects"):
                await client.system_status()

    async def test_a_redirected_mutation_is_refused_not_replayed(
        self, httpx2_mock: respx.Router
    ) -> None:
        """A 307 preserves method and body: auto-following would re-fire the approved
        DELETE -- credential headers, mutation approval and all -- at whatever URL the
        upstream chose. It must surface as an error instead."""
        httpx2_mock.delete(host="radarr.test", path="/api/v3/movie/5").mock(
            return_value=httpx.Response(307, headers={"location": "https://elsewhere.test/movie/5"})
        )
        async with RadarrClient("https://radarr.test", "k", safety=ARMED) as client:
            with pytest.raises(IntegrationError, match="refused redirect"):
                await client.delete_movie(5, delete_files=True, add_exclusion=True)


class TestTagsBodyMustBeAList:
    async def test_a_non_list_200_is_an_error_not_an_empty_whitelist(
        self, httpx2_mock: respx.Router
    ) -> None:
        """A reverse proxy's error page arrives as a 200 with a JSON object (or HTML).
        Masking it as [] once let a keep-tag sync read an empty whitelist out of an
        error page and wipe the stored one."""
        httpx2_mock.get("https://radarr.test/api/v3/tag").mock(
            return_value=httpx.Response(200, json={"error": "bad gateway"})
        )
        async with RadarrClient("https://radarr.test", "k", safety=READ_ONLY) as client:
            with pytest.raises(IntegrationError, match="did not return a list"):
                await client.tags()


class TestEveryListReadRefusesANonListBody:
    """``tags`` was the only one that got this right. Every sibling coerced a non-list 200
    to ``[]``, and each of those empties tells a lie with its own consequence: an auth
    proxy's JSON error page read as "this Radarr holds no movies", so a whole instance left
    the scan while the snapshot stayed executable and the operator was told a complete run
    (rules 28, 93, 72). ``movie_by_id`` and ``series_by_id`` already raised, which is what
    makes the coercion an inconsistency rather than a house style."""

    @pytest.mark.parametrize(
        ("client_cls", "host", "path", "call"),
        [
            (RadarrClient, "radarr.test", "/api/v3/movie", lambda c: c.movies()),
            (RadarrClient, "radarr.test", "/api/v3/rootfolder", lambda c: c.root_folders()),
            (RadarrClient, "radarr.test", "/api/v3/exclusions", lambda c: c.exclusions()),
            (SonarrClient, "sonarr.test", "/api/v3/series", lambda c: c.series()),
            (SonarrClient, "sonarr.test", "/api/v3/episodefile", lambda c: c.episode_files(1)),
            (SonarrClient, "sonarr.test", "/api/v3/episode", lambda c: c.episodes(1)),
            (
                SonarrClient,
                "sonarr.test",
                "/api/v3/importlistexclusion",
                lambda c: c.exclusions(),
            ),
            (SeerrClient, "seerr.test", "/api/v1/settings/sonarr", lambda c: c.services()),
        ],
    )
    async def test_a_non_list_200_raises(
        self,
        httpx2_mock: respx.Router,
        client_cls: type[RadarrClient] | type[SonarrClient] | type[SeerrClient],
        host: str,
        path: str,
        call: Any,
    ) -> None:
        httpx2_mock.get(host=host, path=path).mock(
            return_value=httpx.Response(200, json={"error": "bad gateway"})
        )
        async with client_cls(f"https://{host}", "k", safety=READ_ONLY) as client:
            with pytest.raises(IntegrationError, match="did not return a list"):
                await call(client)

    async def test_a_genuinely_empty_list_is_still_empty(self, httpx2_mock: respx.Router) -> None:
        """The control, and the reason this cannot just raise on anything falsy: a Radarr
        with no movies yet is answering the question, and must not degrade a scan."""
        httpx2_mock.get("https://radarr.test/api/v3/movie").mock(
            return_value=httpx.Response(200, json=[])
        )
        async with RadarrClient("https://radarr.test", "k", safety=READ_ONLY) as client:
            assert await client.movies() == []


class TestEveryObjectReadRefusesANonObjectBody:
    """The list guards' other half, and the half nothing drove. Eleven shape guards were
    written out by hand in ``arr.py``; the parametrize above reached seven of them and
    ``tags`` had its own test, so the three object reads were the members missing from the
    proof (rules 145, 147). They are now one helper with the eight list reads, which is
    exactly why the population has to be pinned: a site quietly reverted to ``get_json``
    coerces again, and only a per-site case says so.

    Seerr's five sit here too, because the same helper serves them (rule 72)."""

    @pytest.mark.parametrize(
        ("client_cls", "host", "path", "call"),
        [
            (RadarrClient, "radarr.test", "/api/v3/system/status", lambda c: c.system_status()),
            (RadarrClient, "radarr.test", "/api/v3/movie/7", lambda c: c.movie_by_id(7)),
            (SonarrClient, "sonarr.test", "/api/v3/series/7", lambda c: c.series_by_id(7)),
            (SeerrClient, "seerr.test", "/api/v1/status", lambda c: c.status()),
            (SeerrClient, "seerr.test", "/api/v1/request", lambda c: c.requests()),
            (SeerrClient, "seerr.test", "/api/v1/user", lambda c: c.users()),
            (SeerrClient, "seerr.test", "/api/v1/user/7/quota", lambda c: c.quota(7)),
            (
                SeerrClient,
                "seerr.test",
                "/api/v1/movie/7",
                lambda c: c.title(tmdb_id=7, media_type="movie"),
            ),
        ],
    )
    async def test_a_non_object_200_raises(
        self,
        httpx2_mock: respx.Router,
        client_cls: type[RadarrClient] | type[SonarrClient] | type[SeerrClient],
        host: str,
        path: str,
        call: Any,
    ) -> None:
        httpx2_mock.get(host=host, path=path).mock(
            return_value=httpx.Response(200, json=["bad gateway"])
        )
        async with client_cls(f"https://{host}", "k", safety=READ_ONLY) as client:
            with pytest.raises(IntegrationError, match="did not return an object"):
                await call(client)

    async def test_the_message_names_the_path_that_was_asked(
        self, httpx2_mock: respx.Router
    ) -> None:
        """The three arr messages used to be hand-written and dropped the API prefix, so
        an operator on a v5 Sonarr read "series/7 did not return an object" and could not
        tell which API path had answered. Generating the message from the path fixes that,
        and this is the assertion that would notice it going back."""
        httpx2_mock.get(host="sonarr.test", path="/api/v5/series/7").mock(
            return_value=httpx.Response(200, json=["bad gateway"])
        )
        async with SonarrClient(
            "https://sonarr.test", "k", safety=READ_ONLY, api_path_prefix="/api/v5"
        ) as client:
            with pytest.raises(
                IntegrationError, match=r"/api/v5/series/7 did not return an object"
            ):
                await client.series_by_id(7)

    async def test_a_helper_cannot_be_asked_not_to_raise(self) -> None:
        """The one property that makes the extraction safe rather than convenient: a
        ``default=`` or ``coerce=`` parameter would reopen rules 28/93 at every call site
        at once, from one line nobody reviews again."""
        for helper in (BaseClient.get_list, BaseClient.get_dict):
            assert set(inspect.signature(helper).parameters) == {
                "self",
                "path",
                "params",
                "headers",
            }


class TestAShortSeerrWalkRefusesRatherThanUndercounting:
    """``build_request_index`` sets ``available=True`` when every Seerr "was read in full",
    and its docstring says exactly why that matters: a confident ``Known(value=False)`` off
    a partial view adds delete pressure to a title a blinded portal in fact holds a request
    for. The walk could end short and return normally, so nothing upstream could tell a
    complete read from a truncated one, and the claim was false without anyone noticing
    (rules 56/89, 7/24).

    The existing guard only fired on rows-without-a-total. The undetected case is its
    mirror: a total that promises more, and a page that hands back none. A third way out
    was missing entirely, and neither guard can see it: the walk's length is whatever the
    server's reported total says it is, and nothing bounded that number."""

    @staticmethod
    def _page(mock: respx.Router, path: str, *responses: httpx.Response) -> None:
        mock.get(host="seerr.test", path=path).mock(side_effect=list(responses))

    def _client(self) -> SeerrClient:
        return SeerrClient("https://seerr.test", "k", safety=READ_ONLY)

    async def test_a_page_that_cannot_be_read_is_not_a_page_with_no_rows(
        self, httpx2_mock: respx.Router
    ) -> None:
        """``results: null`` used to coerce to [] and end the walk as though it were done."""
        self._page(
            httpx2_mock,
            "/api/v1/request",
            httpx.Response(200, json={"pageInfo": {"results": 500}, "results": None}),
        )
        async with self._client() as client:
            with pytest.raises(IntegrationError, match="did not return a list of results"):
                await client.all_requests()

    async def test_an_empty_page_before_the_total_is_reached_refuses(
        self, httpx2_mock: respx.Router
    ) -> None:
        """The server says there are 500 and hands back none on page two. Refusing costs
        the requester index for this scan, which makes every lookup Unknown, which keeps."""
        full = [{"id": i, "type": "movie", "media": {"tmdbId": i}} for i in range(100)]
        self._page(
            httpx2_mock,
            "/api/v1/request",
            httpx.Response(200, json={"pageInfo": {"results": 500}, "results": full}),
            httpx.Response(200, json={"pageInfo": {"results": 500}, "results": []}),
        )
        async with self._client() as client:
            with pytest.raises(IntegrationError, match="stopped at 100 of 500"):
                await client.all_requests()

    async def test_a_portal_with_no_requests_at_all_is_still_fine(
        self, httpx2_mock: respx.Router
    ) -> None:
        """The control. A genuine zero must not raise, or a fresh Seerr would degrade
        every scan."""
        self._page(
            httpx2_mock,
            "/api/v1/request",
            httpx.Response(200, json={"pageInfo": {"results": 0}, "results": []}),
        )
        async with self._client() as client:
            assert await client.all_requests() == []

    async def test_the_user_walk_carries_the_same_guard(self, httpx2_mock: respx.Router) -> None:
        """Rule 72: the same loop, twenty lines down."""
        self._page(
            httpx2_mock,
            "/api/v1/user",
            httpx.Response(200, json={"pageInfo": {"results": 500}, "results": None}),
        )
        async with self._client() as client:
            with pytest.raises(IntegrationError, match="did not return a list of results"):
                await client.users()

    @staticmethod
    def _endless(path: str, body: dict[str, Any], allowed: int, asked: list[str]) -> Any:
        """A portal that answers every page in full and never lowers its total.

        The mock REFUSES the page past the cap rather than serving it, so deleting the cap
        fails this test in three round trips instead of wedging the suite on an unbounded
        walk (rule 118). `AssertionError` is not caught anywhere on this path: the retry
        predicate matches transport errors only."""

        def _respond(request: httpx.Request) -> httpx.Response:
            asked.append(request.url.params["skip"])
            assert len(asked) <= allowed, f"the walk asked {path} for a page past the cap"
            return httpx.Response(200, json=body)

        return _respond

    async def test_a_portal_that_never_stops_promising_more_is_bounded(
        self, httpx2_mock: respx.Router, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A total the walk cannot reach in any sane number of round trips, with every page
        full so neither existing guard fires. The fixture's 10,000 would end on its own at
        page 100, which is the point: the cap stops it at 3 and the count is what stops it,
        never the total. The trip raises rather than returning short, because the caller's
        `available=True` is a claim that this read finished (rules 56/89)."""
        monkeypatch.setattr("reaper.clients.seerr.MAX_PAGES", 3)
        rows = [{"id": i, "type": "movie", "media": {"tmdbId": i}} for i in range(2)]
        asked: list[str] = []
        httpx2_mock.get(host="seerr.test", path="/api/v1/request").mock(
            side_effect=self._endless(
                "/request", {"pageInfo": {"results": 10_000}, "results": rows}, 3, asked
            )
        )
        async with self._client() as client:
            with pytest.raises(IntegrationError, match="never finished, after 6 requests"):
                await client.all_requests()
        assert asked == ["0", "100", "200"]

    async def test_the_user_walk_is_bounded_too(
        self, httpx2_mock: respx.Router, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Rule 72 again, and a different cap value from the case above so neither test
        rests on one number (rule 141). Production is 1,000, so nothing here can pass by
        matching a hardcoded bound."""
        monkeypatch.setattr("reaper.clients.seerr.MAX_PAGES", 2)
        asked: list[str] = []
        httpx2_mock.get(host="seerr.test", path="/api/v1/user").mock(
            side_effect=self._endless(
                "/user",
                {"pageInfo": {"results": 10_000}, "results": [{"id": 1}, {"id": 2}]},
                2,
                asked,
            )
        )
        async with self._client() as client:
            with pytest.raises(IntegrationError, match="never finished, after 4 accounts"):
                await client.users()
        assert asked == ["0", "100"]


class TestSendRetriesTransientTransportErrors:
    """The ``@retry`` on the read path must actually fire.

    It never did: ``_send`` caught ``httpx2.TransportError``/``TimeoutException`` and
    re-raised them as ``IntegrationError`` *inside* the retried body, so the predicate never
    matched and every momentary blip aborted on the first attempt with zero retries. The fix
    moves the retry to ``_request`` (which lets raw httpx2 errors escape) and maps to
    ``IntegrationError`` only in the outer ``_send``, after the retries are spent.
    """

    async def test_a_transient_transport_error_is_retried_then_succeeds(
        self, httpx2_mock: respx.Router
    ) -> None:
        route = httpx2_mock.get("https://radarr.test/api/v3/system/status").mock(
            side_effect=[
                httpx2.ConnectError("blip"),
                httpx2.ReadError("blip"),
                httpx.Response(200, json={"version": "6.3.0"}),
            ]
        )
        async with RadarrClient("https://radarr.test", "k", safety=READ_ONLY) as client:
            status = await client.system_status()

        assert status["version"] == "6.3.0"
        # The decisive assertion: it took all three attempts, i.e. the first two were retried.
        assert route.call_count == 3

    async def test_a_persistent_transport_error_maps_to_integration_error_after_retries(
        self, httpx2_mock: respx.Router
    ) -> None:
        route = httpx2_mock.get("https://radarr.test/api/v3/system/status").mock(
            side_effect=httpx2.ConnectError("down")
        )
        async with RadarrClient("https://radarr.test", "k", safety=READ_ONLY) as client:
            with pytest.raises(IntegrationError, match="unreachable"):
                await client.system_status()

        # Exhausted the retry budget (stop_after_attempt(3)) before giving up.
        assert route.call_count == 3

    async def test_a_4xx_is_not_retried(self, httpx2_mock: respx.Router) -> None:
        """A definite answer from the service is not a transport failure. Retrying a 404
        wastes the budget and delays the error."""
        route = httpx2_mock.get("https://radarr.test/api/v3/system/status").mock(
            return_value=httpx.Response(404)
        )
        async with RadarrClient("https://radarr.test", "k", safety=READ_ONLY) as client:
            with pytest.raises(IntegrationError):
                await client.system_status()

        assert route.call_count == 1


class TestTimeoutMessageNamesTheKind:
    """A ConnectTimeout must not be reported as the 30s read timeout.

    The message hardcoded ``DEFAULT_TIMEOUT.read`` (30s), so a 5s ConnectTimeout (host up
    but refusing connections) read as 'slow to respond' instead of 'unreachable'."""

    async def test_connect_timeout_is_named_not_reported_as_read_timeout(
        self, httpx2_mock: respx.Router
    ) -> None:
        httpx2_mock.get("https://radarr.test/api/v3/system/status").mock(
            side_effect=httpx2.ConnectTimeout("no route")
        )
        async with RadarrClient("https://radarr.test", "k", safety=READ_ONLY) as client:
            with pytest.raises(IntegrationError) as exc:
                await client.system_status()

        message = str(exc.value)
        assert "ConnectTimeout" in message
        assert "30" not in message  # never the misleading fixed read-timeout figure

    @pytest.mark.parametrize(
        ("kind", "shrinkable"),
        [(httpx2.ReadTimeout, True), (httpx2.ConnectTimeout, False), (httpx2.PoolTimeout, False)],
    )
    async def test_only_a_read_timeout_is_marked_as_one(
        self,
        httpx2_mock: respx.Router,
        kind: type[httpx2.TimeoutException],
        shrinkable: bool,
    ) -> None:
        """A read timeout says the service took the request and could not finish the body,
        so asking for less is worth trying -- ``history_sync.sync`` halves its page on it.
        A connect or pool timeout says nothing about size, and marking one would have the
        history walk shrink its way through an unreachable host before giving up."""
        httpx2_mock.get("https://radarr.test/api/v3/system/status").mock(
            side_effect=kind("no answer")
        )
        async with RadarrClient("https://radarr.test", "k", safety=READ_ONLY) as client:
            with pytest.raises(IntegrationError) as exc:
                await client.system_status()

        assert exc.value.read_timed_out is shrinkable

    async def test_a_status_failure_is_not_marked_as_a_timeout(
        self, httpx2_mock: respx.Router
    ) -> None:
        """The flag defaults to False, so an answer that arrived cannot be mistaken for one
        that never did."""
        httpx2_mock.get("https://radarr.test/api/v3/system/status").mock(
            return_value=httpx.Response(503)
        )
        async with RadarrClient("https://radarr.test", "k", safety=READ_ONLY) as client:
            with pytest.raises(IntegrationError) as exc:
                await client.system_status()

        assert exc.value.read_timed_out is False


class TestOneBulkReadCanWidenItsOwnReadBudget:
    """A client's timeout is shared by every method on it, so the history sweep's minute-long
    page and the artwork proxy's answer to a browser were bound to one number, and the sweep
    paid for that with 5x the requests (#780). ``read_timeout`` moves the budget per call.

    httpx resolves the effective timeout onto the outgoing request's ``extensions``, so what
    reached the wire is readable rather than inferred from the argument.
    """

    @staticmethod
    def _budget(route: respx.Route) -> dict[str, float | None]:
        extensions: dict[str, Any] = route.calls.last.request.extensions
        return dict(extensions["timeout"])

    async def test_a_call_that_asks_for_nothing_keeps_the_clients_budget(
        self, httpx2_mock: respx.Router
    ) -> None:
        route = httpx2_mock.get("https://tautulli.test/api/v2").mock(
            return_value=httpx.Response(200, json={"response": {"result": "success", "data": {}}})
        )
        async with TautulliClient("https://tautulli.test", "k", safety=READ_ONLY) as client:
            await client.history(length=1)

        assert self._budget(route) == {
            "connect": DEFAULT_TIMEOUT.connect,
            "read": DEFAULT_TIMEOUT.read,
            "write": DEFAULT_TIMEOUT.write,
            "pool": DEFAULT_TIMEOUT.pool,
        }

    async def test_only_the_read_leg_moves(self, httpx2_mock: respx.Router) -> None:
        """Connect, write and pool say nothing about how much was asked for, so widening
        them would buy a bulk read nothing and cost every failure mode its speed."""
        route = httpx2_mock.get("https://tautulli.test/api/v2").mock(
            return_value=httpx.Response(200, json={"response": {"result": "success", "data": {}}})
        )
        async with TautulliClient("https://tautulli.test", "k", safety=READ_ONLY) as client:
            await client.history(length=25_000, read_timeout=61.0)

        assert self._budget(route) == {
            "connect": DEFAULT_TIMEOUT.connect,
            "read": 61.0,
            "write": DEFAULT_TIMEOUT.write,
            "pool": DEFAULT_TIMEOUT.pool,
        }

    async def test_the_wider_budget_does_not_stick_to_the_client(
        self, httpx2_mock: respx.Router
    ) -> None:
        """The next call on the same client is back to the shared budget. A widening that
        leaked would hand a browser-facing read the sweep's minute."""
        route = httpx2_mock.get("https://tautulli.test/api/v2").mock(
            return_value=httpx.Response(200, json={"response": {"result": "success", "data": {}}})
        )
        async with TautulliClient("https://tautulli.test", "k", safety=READ_ONLY) as client:
            await client.history(length=25_000, read_timeout=61.0)
            await client.history(length=1)

        assert self._budget(route)["read"] == DEFAULT_TIMEOUT.read


class TestPlexTvErrorsAreMapped:
    """plex.tv login/authorization calls must fail closed.

    ``account``/``resources``/``_post`` used to call the raw httpx client, so a plex.tv
    outage surfaced as a raw ``httpx`` error and a maintenance HTML page as a ``ValueError``
    -- neither of which ``owns_server``'s ``except IntegrationError`` catches. Routing them
    through the base mapping makes the fail-closed guard reliable.
    """

    async def test_resources_transport_error_becomes_integration_error(
        self, httpx2_mock: respx.Router
    ) -> None:
        httpx2_mock.get("https://plex.tv/api/v2/resources").mock(
            side_effect=httpx2.ConnectError("out")
        )
        async with PlexTvClient("cid", safety=READ_ONLY) as plextv:
            with pytest.raises(IntegrationError):
                await plextv.resources("user-token")

    async def test_resources_non_json_body_becomes_integration_error(
        self, httpx2_mock: respx.Router
    ) -> None:
        """A plex.tv maintenance page: HTTP 200, but HTML, not JSON."""
        httpx2_mock.get("https://plex.tv/api/v2/resources").mock(
            return_value=httpx.Response(200, text="<html>maintenance</html>")
        )
        async with PlexTvClient("cid", safety=READ_ONLY) as plextv:
            with pytest.raises(IntegrationError):
                await plextv.resources("user-token")

    async def test_owns_server_fails_closed_on_outage(self, httpx2_mock: respx.Router) -> None:
        """An outage denies access cleanly rather than crashing the guard."""
        httpx2_mock.get("https://plex.tv/api/v2/resources").mock(
            side_effect=httpx2.ConnectTimeout("down")
        )
        async with PlexTvClient("cid", safety=READ_ONLY) as plextv:
            assert await plextv.owns_server("user-token", "machine-123") is False


class TestPosterImageAllowList:
    """The poster proxy relays bytes same-origin, so it must reject script-bearing types.

    ``_image`` used a bare ``"image" in ctype`` check, which admits ``image/svg+xml`` -- an
    SVG can carry script that executes in Reaper's origin if the poster URL is opened
    directly. The guard is now a raster allow-list.
    """

    def test_svg_is_not_on_the_allow_list(self) -> None:
        assert "image/svg+xml" not in ALLOWED_IMAGE_TYPES
        assert {"image/jpeg", "image/png", "image/webp"} == ALLOWED_IMAGE_TYPES

    async def test_svg_upstream_is_rejected(self, httpx2_mock: respx.Router) -> None:
        httpx2_mock.get("https://tautulli.test/api/v2").mock(
            return_value=httpx.Response(
                200, content=b"<svg onload=alert(1)>", headers={"content-type": "image/svg+xml"}
            )
        )
        async with TautulliClient("https://tautulli.test", "k", safety=READ_ONLY) as client:
            assert await client.poster(1) is None

    async def test_a_raster_image_passes_and_charset_is_stripped(
        self, httpx2_mock: respx.Router
    ) -> None:
        httpx2_mock.get("https://tautulli.test/api/v2").mock(
            return_value=httpx.Response(
                200, content=b"\xff\xd8\xff", headers={"content-type": "image/jpeg; charset=binary"}
            )
        )
        async with TautulliClient("https://tautulli.test", "k", safety=READ_ONLY) as client:
            result = await client.poster(1)

        assert result is not None
        content, media_type = result
        assert content == b"\xff\xd8\xff"
        # The "; charset=..." parameter is dropped, so the served media type is clean.
        assert media_type == "image/jpeg"


class TestReachableProbeIsRetryable:
    """A server unreachable *right now* is a transient failure, not a spent PIN.

    ``poll_link`` consumed the pending PIN in a blanket ``finally``, so a probe that failed
    because the server was mid-restart forced the owner through a fresh OAuth flow despite
    a successful sign-in. ``reachable_connection`` now raises the retryable subclass so
    ``poll_link`` can leave the PIN intact.
    """

    def test_retryable_is_a_plexlinkerror_subclass(self) -> None:
        # Subclassing matters: the CLI flow's ``except PlexLinkError`` must still catch it.
        assert issubclass(PlexLinkRetryableError, PlexLinkError)

    async def test_reachable_raises_retryable_when_no_connection_answers(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from reaper.clients import plextv as plextv_module
        from reaper.services import plex_link as plex_link_module

        async def _never_reachable(*_args: object, **_kwargs: object) -> bool:
            return False

        # Both the imported name and the source symbol, so the patch holds regardless of how
        # reachable_connection references it.
        monkeypatch.setattr(plex_link_module, "probe_connection", _never_reachable)
        monkeypatch.setattr(plextv_module, "probe_connection", _never_reachable)

        resource = plextv_module.PlexResource(
            name="Server",
            client_identifier="machine-123",
            owned=True,
            provides="server",
            access_token="tok",
            connections=[
                plextv_module.PlexConnection(
                    uri="https://192.0.2.1:32400",
                    address="192.0.2.1",
                    port=32400,
                    local=True,
                    relay=False,
                    protocol="https",
                )
            ],
        )

        with pytest.raises(PlexLinkRetryableError):
            await reachable_connection(resource, "tok")


@pytest.fixture
def call_lines(_restore_logging: None) -> Iterator[Callable[[], list[str]]]:
    """Drive the real logging pipeline at DEBUG; the callable reads the ring on demand.

    Not ``capture_logs``: ``configure_logging`` sets ``cache_logger_on_first_use``, and
    the first use of a module logger under that flag permanently replaces its ``bind``
    with a closure holding the then-current processors -- so once any earlier test in the
    worker has booted an app, ``reaper.clients.base``'s logger can never be intercepted
    again (``conftest._capturable_logs``, from the other end). Reading the ring is the
    stronger assertion for a leak test anyway: it is the rendered line the operator
    downloads, after the scrubber, rather than the dict before it.
    """
    logbuffer.RING = logbuffer.LogRing()
    configure_logging(level="DEBUG")

    def read() -> list[str]:
        return [
            line.text for line in logbuffer.RING.since(0) if line.text.startswith("client.call")
        ]

    yield read
    logbuffer.RING = logbuffer.LogRing()


class TestEveryOutboundCallIsTraced:
    """One DEBUG line per outbound call, and it never carries a credential.

    Nothing else records that one of these calls happened: the HTTP libraries are pinned
    to WARNING because they log the URL verbatim (``logging._NOISY_LOGGERS``), so this is
    the only trace there can be -- and the reason those libraries are quiet is exactly
    the reason this line must not grow a URL, a query string, or a header.

    All three client surfaces emit through `base.trace_call`: `BaseClient._send` for the
    *arr calls, `GuardedSession.request` for every Plex call including the deletion path's
    `refresh_path` and `empty_trash`, and `PublicClient._stream_once` for the ratings
    dataset. The Discord webhook stays out, since its path is the credential (rule 33). The
    Plex, blocked-mutation, and stream cases are the last three below.
    """

    async def test_a_read_reports_service_status_and_shape(
        self, httpx2_mock: respx.Router, call_lines: Callable[[], list[str]]
    ) -> None:
        httpx2_mock.get("https://radarr.test/api/v3/system/status").mock(
            return_value=httpx.Response(200, json={"version": "6.0.0"})
        )
        async with RadarrClient("https://radarr.test", "k", safety=READ_ONLY) as client:
            await client.system_status()

        (call,) = call_lines()
        assert "service=radarr" in call
        assert "method=GET" in call
        assert "path=/api/v3/system/status" in call
        assert "status=200" in call
        assert "mutation=False" in call
        assert "duration_ms=" in call

    async def test_a_call_that_never_answered_reports_no_status(
        self, httpx2_mock: respx.Router, call_lines: Callable[[], list[str]]
    ) -> None:
        """The shape a scan stuck on one service takes: an ask with no answer beside it."""
        httpx2_mock.get("https://radarr.test/api/v3/system/status").mock(
            side_effect=httpx2.ConnectTimeout("nope")
        )
        async with RadarrClient("https://radarr.test", "k", safety=READ_ONLY) as client:
            with pytest.raises(IntegrationError):
                await client.system_status()

        assert "status=None" in call_lines()[-1]

    async def test_a_mutation_is_marked_as_one(
        self, httpx2_mock: respx.Router, call_lines: Callable[[], list[str]]
    ) -> None:
        httpx2_mock.delete(host="radarr.test", path="/api/v3/movie/5").mock(
            return_value=httpx.Response(200, json={})
        )
        async with RadarrClient("https://radarr.test", "k", safety=ARMED) as client:
            await client.delete_movie(5, delete_files=True, add_exclusion=True)

        assert "mutation=True" in call_lines()[-1]

    async def test_a_mutation_that_never_answered_reports_no_status(
        self, httpx2_mock: respx.Router, call_lines: Callable[[], list[str]]
    ) -> None:
        """A delete that timed out is the one an operator must be able to find, because the
        file may or may not be gone and only the next scan settles it. `_mutate` maps the
        timeout kind rather than a fixed budget, and the trace still lands from `finally`."""
        httpx2_mock.delete(host="radarr.test", path="/api/v3/movie/5").mock(
            side_effect=httpx2.ConnectTimeout("nope")
        )
        async with RadarrClient("https://radarr.test", "k", safety=ARMED) as client:
            with pytest.raises(IntegrationError, match=r"timed out \(ConnectTimeout\)"):
                await client.delete_movie(5, delete_files=True, add_exclusion=True)

        call = call_lines()[-1]
        assert "mutation=True" in call
        assert "status=None" in call

    async def test_an_unreachable_host_maps_to_the_domain_error_on_a_mutation(
        self, httpx2_mock: respx.Router
    ) -> None:
        """A raw transport exception escaping here would defeat every `except
        IntegrationError` between this and the executor's per-item failure record."""
        httpx2_mock.delete(host="radarr.test", path="/api/v3/movie/5").mock(
            side_effect=httpx2.ConnectError("no route")
        )
        async with RadarrClient("https://radarr.test", "k", safety=ARMED) as client:
            with pytest.raises(IntegrationError, match="unreachable"):
                await client.delete_movie(5, delete_files=True, add_exclusion=True)

    async def test_an_error_status_on_a_mutation_carries_the_status(
        self, httpx2_mock: respx.Router, call_lines: Callable[[], list[str]]
    ) -> None:
        """The executor branches on `status` to decide whether a step failed or is worth a
        retry, so a mutation refused with 4xx/5xx must arrive typed and not as a bare 200."""
        httpx2_mock.delete(host="radarr.test", path="/api/v3/movie/5").mock(
            return_value=httpx.Response(500, json={})
        )
        async with RadarrClient("https://radarr.test", "k", safety=ARMED) as client:
            with pytest.raises(IntegrationError) as caught:
                await client.delete_movie(5, delete_files=True, add_exclusion=True)

        assert caught.value.status == 500
        assert "status=500" in call_lines()[-1]

    async def test_a_redirected_read_is_refused_when_it_cannot_be_replayed(
        self, httpx2_mock: respx.Router
    ) -> None:
        """A redirect with no Location, or on a method that is not GET or HEAD, has nothing
        safe to follow, so it is refused rather than guessed at."""
        httpx2_mock.get("https://radarr.test/api/v3/system/status").mock(
            return_value=httpx.Response(302)  # a redirect carrying no Location
        )
        async with RadarrClient("https://radarr.test", "k", safety=READ_ONLY) as client:
            with pytest.raises(IntegrationError, match="refused redirect"):
                await client.system_status()

    async def test_the_trace_never_carries_a_query_string_or_a_header(
        self, httpx2_mock: respx.Router, call_lines: Callable[[], list[str]]
    ) -> None:
        """Tautulli takes its key as a query parameter, so a trace that logged params --
        or the resolved URL rather than the path argument -- would write the credential
        into the ring and the 0600 file (rule 13). The scrubber would catch this
        particular spelling, which is why the assertion is on the whole rendered line:
        not logging a credential is a stronger guarantee than redacting one.
        """
        httpx2_mock.get(host="tautulli.test", path="/api/v2").mock(
            return_value=httpx.Response(200, json={"response": {"result": "success", "data": []}})
        )
        async with TautulliClient(
            "https://tautulli.test", "SUPERSECRET", safety=READ_ONLY
        ) as client:
            await client.users()

        (call,) = call_lines()
        assert "SUPERSECRET" not in call
        assert "apikey" not in call
        assert "path=/api/v2" in call

    def test_a_plex_call_is_traced_by_path_and_never_the_token(
        self, call_lines: Callable[[], list[str]]
    ) -> None:
        """`PlexClient` rides plexapi through `GuardedSession`, not `BaseClient`, so this is
        the only line a Plex read or an `emptyTrash` on the deletion path produces. plexapi
        carries `X-Plex-Token` in the query string (rule 13), so the line logs the path split
        and never the URL, exactly like the Tautulli case above.
        """
        session = GuardedSession(READ_ONLY)
        response = requests.Response()
        response.status_code = 200
        with mock.patch.object(requests.Session, "send", autospec=True, return_value=response):
            session.get("http://plex.test/library/sections?X-Plex-Token=SUPERSECRET")

        (call,) = call_lines()
        assert "service=plex" in call
        assert "path=/library/sections" in call
        assert "status=200" in call
        assert "mutation=False" in call
        assert "SUPERSECRET" not in call
        assert "X-Plex-Token" not in call

    def test_a_blocked_plex_mutation_is_not_traced(
        self, call_lines: Callable[[], list[str]]
    ) -> None:
        """The guard raises above the trace, so a refused write reaches no wire and leaves no
        line. A blocked mutation never happened: tracing it would invent a call and log the
        token the block kept off the wire. This pins that ordering (rule 118).
        """
        session = GuardedSession(READ_ONLY)
        with mock.patch.object(requests.Session, "send", autospec=True) as send:
            with pytest.raises(SafetyViolationError, match="Blocked"):
                session.delete("http://plex.test/library/metadata/1?X-Plex-Token=SUPERSECRET")
            send.assert_not_called()

        assert call_lines() == []

    async def test_a_streamed_download_is_traced(
        self, httpx2_mock: respx.Router, call_lines: Callable[[], list[str]], tmp_path: Path
    ) -> None:
        """`PublicClient._stream_once` hand-rolls its loop past `_send`, so the ratings
        dataset -- the longest single outbound operation in the app -- traces itself. `path`
        is the argument, never the post-redirect target.
        """
        httpx2_mock.get("https://data.test/ratings.tsv").mock(
            return_value=httpx.Response(200, content=b"col\tval\n")
        )
        async with PublicClient("https://data.test") as client:
            await client.stream_to("/ratings.tsv", tmp_path / "out.tsv")

        (call,) = call_lines()
        assert "service=public-fetch" in call
        assert "path=/ratings.tsv" in call
        assert "status=200" in call
