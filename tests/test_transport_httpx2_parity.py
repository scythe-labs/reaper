# SPDX-License-Identifier: AGPL-3.0-or-later
"""Pins the httpx2 transport-extension contract that ``GuardedTransport`` depends on.

``GuardedTransport`` (clients/base.py) is the safety guard on every outgoing request: it
subclasses ``httpx2.AsyncBaseTransport``, inspects each request's method, path, and
extensions, and either refuses a mutation or delegates to an inner transport. This file
proves, independently of that class, that httpx2's ``AsyncBaseTransport`` extension point
behaves the way ``GuardedTransport`` needs it to.

Three properties, each the exact shape base.py relies on:
  * an override of ``handle_async_request`` runs for every request,
  * it sees the same duck-typed request (``method``, ``url.path``, and the per-request
    ``extensions`` dict that carries ``reaper_mutation_approved``),
  * its return value reaches the caller, and an exception it raises also reaches the
    caller, without the inner transport ever being touched.

If any of these regresses, ``GuardedTransport`` is unsafe, and this test catches it before a
live server does. This file only checks the library contract; ``RuntimeSafety`` and the real
refusal messages stay in the one production class.
"""

from __future__ import annotations

import httpx2
import pytest

BASE = "https://arr.test"


class _RecordingInner(httpx2.AsyncBaseTransport):
    """A stand-in for ``AsyncHTTPTransport``. Records what reached the wire and returns 200.

    A request only lands here if the guard above it chose to delegate. An empty ``seen``
    list proves a refusal happened before the send.
    """

    def __init__(self) -> None:
        self.seen: list[tuple[str, str, bool]] = []

    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        approved = request.extensions.get("reaper_mutation_approved") is True
        self.seen.append((request.method, request.url.path, approved))
        return httpx2.Response(200, json={"ok": True})


class _BlockedError(RuntimeError):
    """This test file's stand-in for ``SafetyViolationError``."""


class _MiniGuard(httpx2.AsyncBaseTransport):
    """A minimal version of ``GuardedTransport``'s transport mechanism.

    It refuses a non-safe method unless the per-request mutation-approved extension is
    set, and otherwise delegates to the inner transport.
    """

    _SAFE = frozenset({"GET", "HEAD", "OPTIONS"})

    def __init__(self, inner: httpx2.AsyncBaseTransport) -> None:
        self._inner = inner

    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        if (
            request.method.upper() not in self._SAFE
            and request.extensions.get("reaper_mutation_approved") is not True
        ):
            raise _BlockedError(f"Blocked {request.method} {request.url.path}")
        return await self._inner.handle_async_request(request)


async def test_a_read_is_delegated_to_the_inner_transport() -> None:
    inner = _RecordingInner()
    async with httpx2.AsyncClient(transport=_MiniGuard(inner), base_url=BASE) as client:
        response = await client.get("/api/v3/system/status")
    assert response.status_code == 200
    assert inner.seen == [("GET", "/api/v3/system/status", False)]


async def test_an_undeclared_mutation_never_reaches_the_inner_transport() -> None:
    inner = _RecordingInner()
    async with httpx2.AsyncClient(transport=_MiniGuard(inner), base_url=BASE) as client:
        with pytest.raises(_BlockedError, match="Blocked POST /api/v3/movie/1"):
            await client.post("/api/v3/movie/1", json={})
    assert inner.seen == []  # refused before the send, exactly as the guard must


async def test_a_declared_mutation_is_delegated_carrying_its_extension() -> None:
    inner = _RecordingInner()
    async with httpx2.AsyncClient(transport=_MiniGuard(inner), base_url=BASE) as client:
        response = await client.post(
            "/api/v3/movie/1", json={}, extensions={"reaper_mutation_approved": True}
        )
    assert response.status_code == 200
    assert inner.seen == [("POST", "/api/v3/movie/1", True)]
