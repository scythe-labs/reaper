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
import pytest
import respx

from reaper.clients.base import IntegrationError
from reaper.db.models import InstanceKind
from reaper.services import instances as instances_service
from reaper.services.instances import (
    _GENERIC_FAILURE,
    _causes,
    _explain_failure,
)

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
            httpx.ConnectError("[SSL: CERTIFICATE_VERIFY_FAILED]"),
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
        "unauthorised",
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
        _wrapped(httpx.ConnectTimeout("timed out")),
        "Couldn't open a connection to the server in time.",
    ),
    (
        "pool timeout",
        InstanceKind.SONARR,
        _wrapped(httpx.PoolTimeout("no free connection")),
        "Couldn't open a connection to the server in time.",
    ),
    (
        "read timeout",
        InstanceKind.TAUTULLI,
        _wrapped(httpx.ReadTimeout("timed out")),
        "The server didn't answer in time.",
    ),
    (
        "unsupported scheme",
        InstanceKind.RADARR,
        _wrapped(httpx.UnsupportedProtocol("no scheme")),
        "That isn't an address Reaper can use. Start it with http:// or https://.",
    ),
    (
        "malformed url",
        InstanceKind.RADARR,
        _wrapped(httpx.InvalidURL("not a url")),
        "That isn't an address Reaper can use. Start it with http:// or https://.",
    ),
    (
        "connection refused",
        InstanceKind.SONARR,
        _wrapped(httpx.ConnectError("all connection attempts failed")),
        "Couldn't reach the server at this address. Check the URL and port, and that the "
        "service is running.",
    ),
    (
        "proxy error",
        InstanceKind.SONARR,
        _wrapped(httpx.ProxyError("proxy refused")),
        "Couldn't reach the server at this address. Check the URL and port, and that the "
        "service is running.",
    ),
    (
        "connection dropped",
        InstanceKind.SEERR,
        _wrapped(httpx.ReadError("connection reset")),
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
        "nothing we recognise",
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

    @respx.mock
    async def test_a_rejected_key_fails_even_though_status_is_public(self) -> None:
        respx.get(f"{SEERR}/api/v1/status").mock(
            return_value=httpx.Response(200, json={"version": "1.0.0"})
        )
        authed = respx.get(f"{SEERR}/api/v1/request").mock(
            return_value=httpx.Response(403, json={"message": "forbidden"})
        )
        result = await instances_service.test_connection(InstanceKind.SEERR, SEERR, "wrong-key")
        assert result.ok is False
        assert result.detail == KEY_REFUSED
        assert authed.called  # the public status probe alone must not decide the outcome

    @respx.mock
    async def test_a_good_key_connects_and_reports_the_version(self) -> None:
        respx.get(f"{SEERR}/api/v1/status").mock(
            return_value=httpx.Response(200, json={"version": "1.33.2"})
        )
        authed = respx.get(f"{SEERR}/api/v1/request").mock(
            return_value=httpx.Response(
                200, json={"pageInfo": {"results": 3}, "results": [{"id": 1}]}
            )
        )
        result = await instances_service.test_connection(InstanceKind.SEERR, SEERR, "good-key")
        assert result.ok is True
        assert result.version == "1.33.2"
        assert authed.called

    @respx.mock
    async def test_an_instance_with_no_requests_yet_still_connects(self) -> None:
        """Zero requests is healthy, not a rejected key: an empty authed page passes."""
        respx.get(f"{SEERR}/api/v1/status").mock(
            return_value=httpx.Response(200, json={"version": "2.0.0"})
        )
        respx.get(f"{SEERR}/api/v1/request").mock(
            return_value=httpx.Response(200, json={"pageInfo": {"results": 0}, "results": []})
        )
        result = await instances_service.test_connection(InstanceKind.SEERR, SEERR, "good-key")
        assert result.ok is True
