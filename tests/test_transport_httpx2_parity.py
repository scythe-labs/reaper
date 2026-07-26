# SPDX-License-Identifier: AGPL-3.0-or-later
"""Transport-subclass parity for the httpx -> httpx2 migration.

``GuardedTransport`` (clients/base.py) is the safety linchpin: it subclasses
``httpx2.AsyncBaseTransport``, inspects each request's method / path / extensions, and
either refuses a mutation or delegates to an inner transport. It is now ported, alongside
the whole BaseClient stack and its respx tests (see docs/history/PLAN-narrative.md).
This file pinned the one
library contract that port depended on before it landed -- that httpx2's
``AsyncBaseTransport`` extension point behaves identically to httpx's -- and stays as a
permanent regression pin on that contract now that the port is done.

Three properties, each the exact shape base.py uses:
  * an override of ``handle_async_request`` is invoked for every request,
  * it sees the same duck-typed request (``method``, ``url.path``, and the per-request
    ``extensions`` dict that carries ``reaper_mutation_approved``),
  * its return value reaches the caller, and an exception it raises is NOT swallowed and
    the inner transport is never touched.

If any of these ever regresses, the base.py port is unsafe and must stop here, at a test,
rather than at a live server. This is a migration probe, not a second home for the guard's
policy -- ``RuntimeSafety`` and the real refusal messages stay in the one production class.
"""

from __future__ import annotations

import httpx2
import pytest

BASE = "https://arr.test"


class _RecordingInner(httpx2.AsyncBaseTransport):
    """Stands in for ``AsyncHTTPTransport``: records what reached the wire and returns 200.

    A request only lands here if the guard above it chose to delegate, so ``seen`` staying
    empty is the proof that a refusal happened *before* the send.
    """

    def __init__(self) -> None:
        self.seen: list[tuple[str, str, bool]] = []

    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        approved = request.extensions.get("reaper_mutation_approved") is True
        self.seen.append((request.method, request.url.path, approved))
        return httpx2.Response(200, json={"ok": True})


class _BlockedError(RuntimeError):
    """The migration probe's stand-in for ``SafetyViolationError``."""


class _MiniGuard(httpx2.AsyncBaseTransport):
    """``GuardedTransport``'s shape, minimized to the transport mechanism under test:
    refuse a non-safe method unless the per-request mutation-approved extension is set,
    otherwise delegate to the inner transport."""

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
