# SPDX-License-Identifier: AGPL-3.0-or-later
"""What a failed connection test tells the operator.

``_explain_failure`` is the first screen a new operator sees when a URL or a key is
wrong, and its correctness is almost entirely a matter of *branch order*: certificate
failures arrive wrapped in a ``ConnectError`` (so the SSL check has to be read first),
``ssl.SSLCertVerificationError`` is itself a ``ValueError`` (so it has to be read before
the unparseable-body branch), and every transport failure is wrapped in an
``IntegrationError`` (so the no-status branch has to be read last). The table below
builds a real cause chain per family and pins the exact sentence, so a reordering shows
up as a failing test rather than as advice that names the wrong fix.

The chains are built with implicit chaining (``__context__``), not ``raise ... from``,
because the walk in ``_causes`` follows both and only one of them is exercised by the
client layer's explicit re-raises.
"""

from __future__ import annotations

import ssl

import httpx
import httpx2
import pytest
import respx

from reaper.clients.base import IntegrationError
from reaper.clients.seerr import SeerrClient, SeerrService
from reaper.config import RuntimeSafety
from reaper.db.models import Instance, InstanceKind
from reaper.services import instances as instances_service
from reaper.services.instances import (
    _GENERIC_FAILURE,
    _causes,
    _encode_service_instance_map,
    _explain_failure,
    _host_port,
    _suggest_instance,
    decode_service_instance_map,
)

pytestmark = pytest.mark.httpx2(assert_all_called=False)

SELF_SIGNED = (
    "The server's certificate is signed by an authority this machine doesn't know. "
    "Only turn off the certificate check if this is your own server on your own "
    "network: your API key travels on this connection."
)
CERT_REJECTED = (
    "The server's certificate was rejected. It may have expired, or be for a different "
    "address, or something may be sitting between Reaper and the server."
)


def _cert_error(verify_code: int, message: str) -> ssl.SSLCertVerificationError:
    err = ssl.SSLCertVerificationError(f"certificate verify failed: {message}")
    err.verify_code = verify_code
    err.verify_message = message
    return err


def _chain(*excs: BaseException) -> BaseException:
    """Link ``excs`` innermost-first via ``__context__`` and return the outermost.

    This is what the interpreter does when one exception is raised while another is
    being handled, which is how the client layer's wrappers reach the operator.
    """
    outermost: BaseException | None = None
    for exc in excs:
        try:
            raise exc
        except BaseException as raised:
            raised.__context__ = outermost
            outermost = raised
    assert outermost is not None
    return outermost


def _wrapped(inner: BaseException, *, status: int | None = None) -> BaseException:
    """The shape the client layer produces: an ``IntegrationError`` over a transport error."""
    return _chain(inner, IntegrationError("service", "request failed", status=status))


CASES: list[tuple[str, InstanceKind, BaseException, str]] = [
    # -- certificates: read before the transport branch, and before the body branch ----
    (
        "self-signed under a connect error",
        InstanceKind.SONARR,
        _chain(
            _cert_error(18, "self signed certificate"),
            httpx2.ConnectError("[SSL: CERTIFICATE_VERIFY_FAILED]"),
            IntegrationError("service", "request failed"),
        ),
        SELF_SIGNED,
    ),
    (
        "unknown authority",
        InstanceKind.RADARR,
        _wrapped(_cert_error(20, "unable to get local issuer certificate")),
        SELF_SIGNED,
    ),
    (
        "expired certificate is not waved off",
        InstanceKind.RADARR,
        _wrapped(_cert_error(10, "certificate has expired")),
        CERT_REJECTED,
    ),
    (
        "name mismatch is not waved off",
        InstanceKind.TAUTULLI,
        _wrapped(_cert_error(62, "Hostname mismatch")),
        CERT_REJECTED,
    ),
    (
        "a verification failure with no code is not waved off",
        InstanceKind.SEERR,
        _wrapped(ssl.SSLCertVerificationError("certificate verify failed")),
        CERT_REJECTED,
    ),
    (
        "a handshake failure is not waved off",
        InstanceKind.SONARR,
        _wrapped(ssl.SSLError("handshake failure")),
        CERT_REJECTED,
    ),
    # -- HTTP statuses ----------------------------------------------------------------
    (
        "unauthorized",
        InstanceKind.SONARR,
        IntegrationError("service", "unauthorized", status=401),
        "Sonarr refused the API key. Copy it again from its own settings.",
    ),
    (
        "forbidden",
        InstanceKind.TAUTULLI,
        IntegrationError("service", "forbidden", status=403),
        "Tautulli refused the API key. Copy it again from its own settings.",
    ),
    (
        "not found",
        InstanceKind.RADARR,
        IntegrationError("service", "not found", status=404),
        "Radarr answered, but there is nothing at this address. Check for a missing or "
        "extra path at the end of the URL.",
    ),
    (
        "rate limited",
        InstanceKind.SEERR,
        IntegrationError("service", "too many requests", status=429),
        "Seerr asked Reaper to slow down. Wait a moment and test again.",
    ),
    (
        "redirect",
        InstanceKind.SONARR,
        IntegrationError("service", "found", status=302),
        "The server sent Reaper somewhere else, and Reaper won't send your API key to a "
        "different address. Check the URL and anything proxying it.",
    ),
    (
        "server error",
        InstanceKind.RADARR,
        IntegrationError("service", "server error", status=500),
        "Radarr reported a problem of its own (HTTP 500). Check its log.",
    ),
    (
        "some other status",
        InstanceKind.SONARR,
        IntegrationError("service", "teapot", status=418),
        "Sonarr refused the request (HTTP 418).",
    ),
    # -- transport families -----------------------------------------------------------
    (
        "connect timeout",
        InstanceKind.SONARR,
        _wrapped(httpx2.ConnectTimeout("timed out")),
        "Couldn't open a connection to the server in time.",
    ),
    (
        "pool timeout",
        InstanceKind.SONARR,
        _wrapped(httpx2.PoolTimeout("no free connection")),
        "Couldn't open a connection to the server in time.",
    ),
    (
        "read timeout",
        InstanceKind.TAUTULLI,
        _wrapped(httpx2.ReadTimeout("timed out")),
        "The server didn't answer in time.",
    ),
    (
        "unsupported scheme",
        InstanceKind.RADARR,
        _wrapped(httpx2.UnsupportedProtocol("no scheme")),
        "That isn't an address Reaper can use. Start it with http:// or https://.",
    ),
    (
        "malformed url",
        InstanceKind.RADARR,
        _wrapped(httpx2.InvalidURL("not a url")),
        "That isn't an address Reaper can use. Start it with http:// or https://.",
    ),
    (
        "connection refused",
        InstanceKind.SONARR,
        _wrapped(httpx2.ConnectError("all connection attempts failed")),
        "Couldn't reach the server at this address. Check the URL and port, and that the "
        "service is running.",
    ),
    (
        "proxy error",
        InstanceKind.SONARR,
        _wrapped(httpx2.ProxyError("proxy refused")),
        "Couldn't reach the server at this address. Check the URL and port, and that the "
        "service is running.",
    ),
    (
        "connection dropped",
        InstanceKind.SEERR,
        _wrapped(httpx2.ReadError("connection reset")),
        "The connection to the server broke before it answered.",
    ),
    # -- body, bare wrapper, and the fallback ------------------------------------------
    (
        "unparseable body",
        InstanceKind.TAUTULLI,
        _wrapped(ValueError("Expecting value: line 1 column 1")),
        "The address answered, but not with data from Tautulli. Check the URL.",
    ),
    (
        "answered and turned the request down",
        InstanceKind.TAUTULLI,
        IntegrationError("service", "api key not valid"),
        "Tautulli answered, but turned the request down. Check the API key first, then the URL.",
    ),
    (
        "nothing we recognize",
        InstanceKind.SONARR,
        RuntimeError("something else entirely"),
        _GENERIC_FAILURE,
    ),
]


@pytest.mark.parametrize(
    ("kind", "exc", "expected"),
    [(kind, exc, expected) for _name, kind, exc, expected in CASES],
    ids=[name for name, _kind, _exc, _expected in CASES],
)
def test_explain_failure_sentence(kind: InstanceKind, exc: BaseException, expected: str) -> None:
    assert _explain_failure(kind, exc) == expected


def test_no_failure_message_recommends_skipping_verification_except_the_safe_one() -> None:
    """Only the unknown-authority case may offer the "turn off the check" remedy."""
    for name, kind, exc, _expected in CASES:
        sentence = _explain_failure(kind, exc)
        if sentence == SELF_SIGNED:
            continue
        assert "certificate check" not in sentence, name


def test_failure_copy_has_no_em_dashes() -> None:
    for name, kind, exc, _expected in CASES:
        assert "—" not in _explain_failure(kind, exc), name


def test_causes_follows_implicit_context() -> None:
    """A wrapper raised while handling another exception still exposes the original."""
    inner = ssl.SSLError("handshake failure")
    outer = _chain(inner, IntegrationError("service", "request failed"))
    assert inner in _causes(outer)


def test_causes_stops_on_a_cycle() -> None:
    a = RuntimeError("a")
    b = RuntimeError("b")
    a.__cause__ = b
    b.__cause__ = a
    assert _causes(a) == [a, b]


SEERR = "https://seerr.test"
KEY_REFUSED = "Seerr refused the API key. Copy it again from its own settings."


class TestSeerrConnectionExercisesTheKey:
    """A green connection test must mean the key was accepted, not merely that the URL
    resolves. Seerr's ``/status`` is public and passes with any key, so the test also
    reads an authenticated route. These pin that it does -- so a wrong key that a live
    instance answered ``/status`` for is caught at test time, not weeks later as a scan
    warning with the requester signal silently dark.
    """

    async def test_a_rejected_key_fails_even_though_status_is_public(
        self, httpx2_mock: respx.Router
    ) -> None:
        httpx2_mock.get(f"{SEERR}/api/v1/status").mock(
            return_value=httpx.Response(200, json={"version": "1.0.0"})
        )
        authed = httpx2_mock.get(f"{SEERR}/api/v1/request").mock(
            return_value=httpx.Response(403, json={"message": "forbidden"})
        )
        result = await instances_service.test_connection(InstanceKind.SEERR, SEERR, "wrong-key")
        assert result.ok is False
        assert result.detail == KEY_REFUSED
        assert authed.called  # the public status probe alone must not decide the outcome

    async def test_a_good_key_connects_and_reports_the_version(
        self, httpx2_mock: respx.Router
    ) -> None:
        httpx2_mock.get(f"{SEERR}/api/v1/status").mock(
            return_value=httpx.Response(200, json={"version": "1.33.2"})
        )
        authed = httpx2_mock.get(f"{SEERR}/api/v1/request").mock(
            return_value=httpx.Response(
                200, json={"pageInfo": {"results": 3}, "results": [{"id": 1}]}
            )
        )
        result = await instances_service.test_connection(InstanceKind.SEERR, SEERR, "good-key")
        assert result.ok is True
        assert result.version == "1.33.2"
        assert authed.called

    async def test_an_instance_with_no_requests_yet_still_connects(
        self, httpx2_mock: respx.Router
    ) -> None:
        """Zero requests is healthy, not a rejected key: an empty authed page passes."""
        httpx2_mock.get(f"{SEERR}/api/v1/status").mock(
            return_value=httpx.Response(200, json={"version": "2.0.0"})
        )
        httpx2_mock.get(f"{SEERR}/api/v1/request").mock(
            return_value=httpx.Response(200, json={"pageInfo": {"results": 0}, "results": []})
        )
        result = await instances_service.test_connection(InstanceKind.SEERR, SEERR, "good-key")
        assert result.ok is True


class TestServiceInstanceMapCodec:
    """A stored serviceId -> instance map must be exactly as harmless as an absent one: a bad
    body reads as {} (build_map then falls back to the loose union), and empty stores as NULL."""

    def test_null_and_garbage_decode_to_empty(self) -> None:
        assert decode_service_instance_map(None) == {}
        assert decode_service_instance_map("") == {}
        assert decode_service_instance_map("not json") == {}
        assert decode_service_instance_map('["a", "b"]') == {}  # a list is not a map

    def test_decode_keeps_str_key_positive_int_value(self) -> None:
        assert decode_service_instance_map('{"2": 7, "3": 8}') == {"2": 7, "3": 8}

    def test_decode_drops_every_unusable_pair(self) -> None:
        # bool (a JSON true), zero, negative, non-int, and a blank key can never name a real
        # instance, so each is dropped rather than crash a scan (rule 32).
        raw = '{"2": true, "3": 0, "4": -1, "5": "x", "  ": 9}'
        assert decode_service_instance_map(raw) == {}

    def test_encode_empty_is_null_not_braces(self) -> None:
        assert _encode_service_instance_map(None) is None
        assert _encode_service_instance_map({}) is None
        assert _encode_service_instance_map({" ": 5}) is None  # blank key -> empty -> NULL

    def test_encode_drops_non_positive_and_bool_values(self) -> None:
        assert _encode_service_instance_map({"2": 0, "3": -1, "4": True}) is None

    def test_encode_decode_round_trips(self) -> None:
        raw = _encode_service_instance_map({" 2 ": 7, "3": 8})
        assert raw is not None
        assert decode_service_instance_map(raw) == {"2": 7, "3": 8}  # key trimmed


def _arr(instance_id: int, kind: InstanceKind, base_url: str) -> Instance:
    return Instance(
        id=instance_id, kind=kind, name=f"n{instance_id}", base_url=base_url, api_key_enc=""
    )


def _svc(kind: str, hostname: str | None, port: int | None) -> SeerrService:
    return SeerrService(
        service_id=2,
        kind=kind,
        name="svc",
        is_4k=False,
        hostname=hostname,
        port=port,
        use_ssl=False,
        base_url="",
    )


class TestSuggestInstance:
    """Prefill only: suggest a Reaper instance when EXACTLY ONE matches by kind + host + port;
    an ambiguous or missing match leaves the operator to pick (Seerr and Reaper often reach the
    same server at different addresses, so a miss is expected, not an error)."""

    def test_host_port_fills_scheme_default_and_lowercases(self) -> None:
        assert _host_port("http://Host:7878") == ("host", 7878)
        assert _host_port("https://host") == ("host", 443)
        assert _host_port("http://host") == ("host", 80)

    def test_a_single_match_by_kind_host_port_is_suggested(self) -> None:
        rows = [
            _arr(7, InstanceKind.RADARR, "http://10.0.0.5:7878"),
            _arr(8, InstanceKind.SONARR, "http://10.0.0.5:7878"),  # wrong kind, ignored
        ]
        assert _suggest_instance(_svc("radarr", "10.0.0.5", 7878), rows) == 7

    def test_two_instances_at_the_same_address_are_ambiguous(self) -> None:
        rows = [
            _arr(7, InstanceKind.RADARR, "http://10.0.0.5:7878"),
            _arr(9, InstanceKind.RADARR, "http://10.0.0.5:7878"),
        ]
        assert _suggest_instance(_svc("radarr", "10.0.0.5", 7878), rows) is None

    def test_a_different_address_does_not_match(self) -> None:
        # The docker-hostname vs LAN-ip case: no match, so the operator picks.
        rows = [_arr(7, InstanceKind.RADARR, "http://radarr:7878")]
        assert _suggest_instance(_svc("radarr", "10.0.0.5", 7878), rows) is None

    def test_a_service_with_no_hostname_suggests_nothing(self) -> None:
        rows = [_arr(7, InstanceKind.RADARR, "http://x:7878")]
        assert _suggest_instance(_svc("radarr", None, 7878), rows) is None


class TestSeerrServicesListing:
    async def test_lists_sonarr_then_radarr_services(self, httpx2_mock: respx.Router) -> None:
        httpx2_mock.get(f"{SEERR}/api/v1/settings/sonarr").mock(
            return_value=httpx.Response(
                200,
                json=[
                    {"id": 0, "name": "Sonarr", "hostname": "sonarr", "port": 8989, "is4k": False},
                    {"id": 1, "name": "Sonarr 4K", "hostname": "s", "port": 8989, "is4k": True},
                    {"name": "no id -> skipped"},
                ],
            )
        )
        httpx2_mock.get(f"{SEERR}/api/v1/settings/radarr").mock(
            return_value=httpx.Response(
                200,
                json=[
                    {"id": 2, "name": "Radarr", "hostname": "radarr", "port": 7878, "is4k": False}
                ],
            )
        )
        client = SeerrClient(SEERR, "k", safety=RuntimeSafety(destructive_enabled=False))
        async with client:
            services = await client.services()
        assert [(s.service_id, s.kind, s.is_4k) for s in services] == [
            (0, "sonarr", False),
            (1, "sonarr", True),
            (2, "radarr", False),
        ]

    async def test_a_non_list_body_is_refused(self, httpx2_mock: respx.Router) -> None:
        httpx2_mock.get(f"{SEERR}/api/v1/settings/sonarr").mock(
            return_value=httpx.Response(200, json={"message": "forbidden"})
        )
        client = SeerrClient(SEERR, "k", safety=RuntimeSafety(destructive_enabled=False))
        with pytest.raises(IntegrationError):
            async with client:
                await client.services()
