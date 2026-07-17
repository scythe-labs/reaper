# SPDX-License-Identifier: AGPL-3.0-or-later
"""Regression tests for the code-review fixes in the ``clients`` lane.

Each test names the defect it guards against. These sit alongside
``test_guarded_transport`` and ``test_upstream_quirks`` -- the safety-critical suites --
because most of what is fixed here is about *failing closed*: a transient blip must not
abort a scan, a plex.tv outage must not become an open door, and a script-bearing image
must not be relayed same-origin.
"""

from __future__ import annotations

import httpx
import pytest
import respx

from reaper.clients.arr import RadarrClient
from reaper.clients.base import IntegrationError
from reaper.clients.plextv import PlexTvClient
from reaper.clients.tautulli import ALLOWED_IMAGE_TYPES, TautulliClient
from reaper.config import RuntimeSafety
from reaper.services.plex_link import (
    PlexLinkError,
    PlexLinkRetryableError,
    reachable_connection,
)

READ_ONLY = RuntimeSafety(destructive_enabled=False)
ARMED = RuntimeSafety(destructive_enabled=True)


class TestRedirectsNeverCarryCredentialsAway:
    """``follow_redirects`` is off at the client. A read may hop within the configured
    origin only -- the API-key header must never chase a Location elsewhere -- and a
    redirected mutation is refused outright rather than replayed at a new URL."""

    @respx.mock
    async def test_a_same_origin_redirect_on_a_read_is_followed(self) -> None:
        respx.get("https://radarr.test/api/v3/system/status").mock(
            return_value=httpx.Response(
                301, headers={"location": "https://radarr.test/api/v4/system/status"}
            )
        )
        respx.get("https://radarr.test/api/v4/system/status").mock(
            return_value=httpx.Response(200, json={"version": "6.0.0"})
        )
        async with RadarrClient("https://radarr.test", "k", safety=READ_ONLY) as client:
            assert (await client.system_status())["version"] == "6.0.0"

    @respx.mock
    async def test_a_cross_origin_redirect_on_a_read_is_refused(self) -> None:
        respx.get("https://radarr.test/api/v3/system/status").mock(
            return_value=httpx.Response(301, headers={"location": "https://elsewhere.test/x"})
        )
        async with RadarrClient("https://radarr.test", "k", safety=READ_ONLY) as client:
            with pytest.raises(IntegrationError, match="cross-origin"):
                await client.system_status()

    @respx.mock
    async def test_a_redirect_loop_gives_up(self) -> None:
        respx.get("https://radarr.test/api/v3/system/status").mock(
            return_value=httpx.Response(
                302, headers={"location": "https://radarr.test/api/v3/system/status"}
            )
        )
        async with RadarrClient("https://radarr.test", "k", safety=READ_ONLY) as client:
            with pytest.raises(IntegrationError, match="too many redirects"):
                await client.system_status()

    @respx.mock
    async def test_a_redirected_mutation_is_refused_not_replayed(self) -> None:
        """A 307 preserves method and body: auto-following would re-fire the approved
        DELETE -- credential headers, mutation approval and all -- at whatever URL the
        upstream chose. It must surface as an error instead."""
        respx.delete(host="radarr.test", path="/api/v3/movie/5").mock(
            return_value=httpx.Response(307, headers={"location": "https://elsewhere.test/movie/5"})
        )
        async with RadarrClient("https://radarr.test", "k", safety=ARMED) as client:
            with pytest.raises(IntegrationError, match="refused redirect"):
                await client.delete_movie(5, delete_files=True, add_exclusion=True)


class TestTagsBodyMustBeAList:
    @respx.mock
    async def test_a_non_list_200_is_an_error_not_an_empty_whitelist(self) -> None:
        """A reverse proxy's error page arrives as a 200 with a JSON object (or HTML).
        Masking it as [] once let a keep-tag sync read an empty whitelist out of an
        error page and wipe the stored one."""
        respx.get("https://radarr.test/api/v3/tag").mock(
            return_value=httpx.Response(200, json={"error": "bad gateway"})
        )
        async with RadarrClient("https://radarr.test", "k", safety=READ_ONLY) as client:
            with pytest.raises(IntegrationError, match="did not return a list"):
                await client.tags()


class TestSendRetriesTransientTransportErrors:
    """The ``@retry`` on the read path must actually fire.

    It never did: ``_send`` caught ``httpx.TransportError``/``TimeoutException`` and
    re-raised them as ``IntegrationError`` *inside* the retried body, so the predicate never
    matched and every momentary blip aborted on the first attempt with zero retries. The fix
    moves the retry to ``_request`` (which lets raw httpx errors escape) and maps to
    ``IntegrationError`` only in the outer ``_send``, after the retries are spent.
    """

    @respx.mock
    async def test_a_transient_transport_error_is_retried_then_succeeds(self) -> None:
        route = respx.get("https://radarr.test/api/v3/system/status").mock(
            side_effect=[
                httpx.ConnectError("blip"),
                httpx.ReadError("blip"),
                httpx.Response(200, json={"version": "6.3.0"}),
            ]
        )
        async with RadarrClient("https://radarr.test", "k", safety=READ_ONLY) as client:
            status = await client.system_status()

        assert status["version"] == "6.3.0"
        # The decisive assertion: it took all three attempts, i.e. the first two were retried.
        assert route.call_count == 3

    @respx.mock
    async def test_a_persistent_transport_error_maps_to_integration_error_after_retries(
        self,
    ) -> None:
        route = respx.get("https://radarr.test/api/v3/system/status").mock(
            side_effect=httpx.ConnectError("down")
        )
        async with RadarrClient("https://radarr.test", "k", safety=READ_ONLY) as client:
            with pytest.raises(IntegrationError, match="unreachable"):
                await client.system_status()

        # Exhausted the retry budget (stop_after_attempt(3)) before giving up.
        assert route.call_count == 3

    @respx.mock
    async def test_a_4xx_is_not_retried(self) -> None:
        """A definite answer from the service is not a transport failure. Retrying a 404
        wastes the budget and delays the error."""
        route = respx.get("https://radarr.test/api/v3/system/status").mock(
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

    @respx.mock
    async def test_connect_timeout_is_named_not_reported_as_read_timeout(self) -> None:
        respx.get("https://radarr.test/api/v3/system/status").mock(
            side_effect=httpx.ConnectTimeout("no route")
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

    @respx.mock
    async def test_resources_transport_error_becomes_integration_error(self) -> None:
        respx.get("https://plex.tv/api/v2/resources").mock(side_effect=httpx.ConnectError("out"))
        async with PlexTvClient("cid", safety=READ_ONLY) as plextv:
            with pytest.raises(IntegrationError):
                await plextv.resources("user-token")

    @respx.mock
    async def test_resources_non_json_body_becomes_integration_error(self) -> None:
        """A plex.tv maintenance page: HTTP 200, but HTML, not JSON."""
        respx.get("https://plex.tv/api/v2/resources").mock(
            return_value=httpx.Response(200, text="<html>maintenance</html>")
        )
        async with PlexTvClient("cid", safety=READ_ONLY) as plextv:
            with pytest.raises(IntegrationError):
                await plextv.resources("user-token")

    @respx.mock
    async def test_owns_server_fails_closed_on_outage(self) -> None:
        """The whole point: an outage denies access cleanly rather than crashing the guard."""
        respx.get("https://plex.tv/api/v2/resources").mock(side_effect=httpx.ConnectTimeout("down"))
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

    @respx.mock
    async def test_svg_upstream_is_rejected(self) -> None:
        respx.get("https://tautulli.test/api/v2").mock(
            return_value=httpx.Response(
                200, content=b"<svg onload=alert(1)>", headers={"content-type": "image/svg+xml"}
            )
        )
        async with TautulliClient("https://tautulli.test", "k", safety=READ_ONLY) as client:
            assert await client.poster(1) is None

    @respx.mock
    async def test_a_raster_image_passes_and_charset_is_stripped(self) -> None:
        respx.get("https://tautulli.test/api/v2").mock(
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
