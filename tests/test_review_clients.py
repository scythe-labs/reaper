# SPDX-License-Identifier: AGPL-3.0-or-later
"""Regression tests for the code-review fixes in the ``clients`` lane.

Each test names the defect it guards against. These sit alongside
``test_guarded_transport`` and ``test_upstream_quirks`` -- the safety-critical suites --
because most of what is fixed here is about *failing closed*: a transient blip must not
abort a scan, a plex.tv outage must not become an open door, and a script-bearing image
must not be relayed same-origin.
"""

from __future__ import annotations

from typing import Any

import httpx
import httpx2
import pytest
import respx

from reaper.clients.arr import RadarrClient, SonarrClient
from reaper.clients.base import IntegrationError
from reaper.clients.plextv import PlexTvClient
from reaper.clients.seerr import SeerrClient
from reaper.clients.tautulli import ALLOWED_IMAGE_TYPES, TautulliClient
from reaper.config import RuntimeSafety
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
        ],
    )
    async def test_a_non_list_200_raises(
        self,
        httpx2_mock: respx.Router,
        client_cls: type[RadarrClient] | type[SonarrClient],
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


class TestAShortSeerrWalkRefusesRatherThanUndercounting:
    """``build_request_index`` sets ``available=True`` when every Seerr "was read in full",
    and its docstring says exactly why that matters: a confident ``Known(value=False)`` off
    a partial view adds delete pressure to a title a blinded portal in fact holds a request
    for. The walk could end short and return normally, so nothing upstream could tell a
    complete read from a truncated one, and the claim was false without anyone noticing
    (rules 56/89, 7/24).

    The existing guard only fired on rows-without-a-total. The undetected case is its
    mirror: a total that promises more, and a page that hands back none."""

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
        """The whole point: an outage denies access cleanly rather than crashing the guard."""
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
