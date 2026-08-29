# SPDX-License-Identifier: AGPL-3.0-or-later
"""The Plex client is held to the same safety rule as everything else.

plexapi speaks ``requests``, not ``httpx``, so it does not pass through
``GuardedTransport``. Without a second guard on the requests side, every Plex write,
including labels, collections, and ``emptyTrash``, would be the one destructive path in
the codebase with no interlock in front of it. ``GuardedSession`` closes that gap, and
these tests pin it shut.

Nothing here talks to a real Plex server. GuardedSession makes its refusal decision
*before* the request is dispatched, so a blocked call never opens a socket.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any
from unittest import mock

import pytest
import requests
from structlog.testing import capture_logs

from reaper.clients.base import SafetyViolationError
from reaper.clients.plex import (
    PLEX_READ_TIMEOUT,
    GuardedSession,
    PlexClient,
    PlexError,
    benign_shelf_write,
    declared_mutation,
    normalize_label,
)
from reaper.config import RuntimeSafety

ARMED = RuntimeSafety(destructive_enabled=True)
READ_ONLY = RuntimeSafety(destructive_enabled=False)
# The operator opted in to writing the benign Leaving Soon shelf while still read-only.
SHELF_UNARMED = RuntimeSafety(destructive_enabled=False, allow_leaving_soon_unarmed=True)


@contextmanager
def _transport() -> Iterator[mock.MagicMock]:
    """Stand in for the socket at the one layer below the guard, and record what reached it.

    ``GuardedSession.request`` decides, then ends in ``super().request(...)``, which prepares
    the request and hands it to ``Session.send``. Patching ``send`` proves a call got all the
    way through, and which call did.

    Firing a live request at ``127.0.0.1:1`` and asserting only that some exception is not a
    ``SafetyViolationError`` would not prove much: a ``TypeError`` from a bad keyword argument,
    or an ``AttributeError`` from a broken ``GuardedSession``, would satisfy that check just as
    well as a real pass would, and the test would also depend on TCP port 1 being closed on
    the runner.
    """
    with mock.patch.object(requests.Session, "send", autospec=True) as send:
        send.return_value = requests.Response()
        yield send


def _sent(send: mock.MagicMock) -> tuple[str, str]:
    """The method and URL of the single request that reached the transport."""
    assert send.call_count == 1, f"expected exactly one request, got {send.call_count}"
    prepared = send.call_args.args[1]
    return str(prepared.method), str(prepared.url)


class TestAMutatingCallIsRefusedUnlessArmedAndDeclared:
    def test_a_get_is_always_allowed(self) -> None:
        """Reads never need arming. GuardedSession must let a GET through to the transport."""
        session = GuardedSession(READ_ONLY)

        with _transport() as send:
            session.get("http://127.0.0.1:1/library/sections", timeout=0.05)

        assert _sent(send) == ("GET", "http://127.0.0.1:1/library/sections")

    def test_a_put_is_blocked_when_not_armed(self) -> None:
        """Deletion is off, and no declaration can lift it. It can only be turned on from
        the UI with the admin password, never by a stray declaration."""
        session = GuardedSession(READ_ONLY)

        with declared_mutation(), pytest.raises(SafetyViolationError, match="turned off"):
            session.put("http://127.0.0.1:1/library/x")

    def test_a_put_is_blocked_when_armed_but_not_declared(self) -> None:
        """Armed, but nobody wrote the intent to the journal. The declaration is there so
        a mutation which skipped the executor cannot reach Plex."""
        session = GuardedSession(ARMED)

        with pytest.raises(SafetyViolationError, match="wasn't declared"):
            session.put("http://127.0.0.1:1/library/x")

    def test_turning_deletion_off_blocks_even_a_declared_mutation(self) -> None:
        """Deletion off wins over everything. Even with a run in flight and its intent
        already journalled, turning deletion off makes the guard refuse it."""
        session = GuardedSession(READ_ONLY)

        with declared_mutation(), pytest.raises(SafetyViolationError):
            session.delete("http://127.0.0.1:1/library/x")

    def test_the_declaration_does_not_leak_past_its_block(self) -> None:
        """The context manager is the whole window. Once it closes, a later write is
        undeclared again, so a declaration cannot carry over into unrelated code."""
        session = GuardedSession(ARMED)

        with declared_mutation():
            pass

        with pytest.raises(SafetyViolationError, match="wasn't declared"):
            session.post("http://127.0.0.1:1/library/x")


class TestTheBenignShelfIsGatedSeparately:
    """Writing the Leaving Soon shelf (a label plus a collection) is a mutation, but a
    reversible one that touches no file. It is gated like a delete by default. An operator
    can opt in to allowing it while read-only, but that opt-in must never carry over into
    the deletion path."""

    def test_the_label_is_blocked_when_read_only_and_not_opted_in(self) -> None:
        session = GuardedSession(READ_ONLY)
        with benign_shelf_write(), pytest.raises(SafetyViolationError, match="Leaving Soon"):
            session.put("http://127.0.0.1:1/library/sections/3/all?type=1&id=42")

    def test_the_label_writes_when_armed(self) -> None:
        """Armed is at least as permissive as a delete, so the label goes through to the
        transport. No declaration is needed, because the shelf write is not journalled
        through the executor."""
        session = GuardedSession(ARMED)
        with _transport() as send, benign_shelf_write():
            session.put("http://127.0.0.1:1/library/sections/3/all?type=1&id=42", timeout=0.05)

        assert _sent(send) == ("PUT", "http://127.0.0.1:1/library/sections/3/all?type=1&id=42")

    def test_the_label_writes_read_only_when_opted_in(self) -> None:
        """The warning can be written during the grace countdown, before deletion is ever
        enabled."""
        session = GuardedSession(SHELF_UNARMED)
        with _transport() as send, benign_shelf_write():
            session.put("http://127.0.0.1:1/library/sections/3/all?type=1&id=42", timeout=0.05)

        assert _sent(send) == ("PUT", "http://127.0.0.1:1/library/sections/3/all?type=1&id=42")

    def test_creating_a_collection_is_a_shelf_shape(self) -> None:
        """POST /library/collections rides the same opt-in as the label."""
        session = GuardedSession(SHELF_UNARMED)
        with _transport() as send, benign_shelf_write():
            session.post(
                "http://127.0.0.1:1/library/collections?type=1&title=Leaving+Soon",
                timeout=0.05,
            )

        assert _sent(send)[0] == "POST"

    def test_adding_collection_items_is_a_shelf_shape(self) -> None:
        session = GuardedSession(SHELF_UNARMED)
        with _transport() as send, benign_shelf_write():
            session.put("http://127.0.0.1:1/library/collections/900/items?uri=x", timeout=0.05)

        assert _sent(send) == ("PUT", "http://127.0.0.1:1/library/collections/900/items?uri=x")

    def test_deleting_a_whole_collection_is_a_shelf_shape(self) -> None:
        """The one DELETE the shelf may issue, dropping the whole emptied collection in
        one request. It removes only the collection object, never an item or its files."""
        session = GuardedSession(SHELF_UNARMED)
        with _transport() as send, benign_shelf_write():
            session.delete("http://127.0.0.1:1/library/collections/900", timeout=0.05)

        assert _sent(send) == ("DELETE", "http://127.0.0.1:1/library/collections/900")

    def test_detaching_one_collection_child_is_no_longer_a_shelf_shape(self) -> None:
        """The per-item ``.../children/{key}`` DELETE is not on the benign list. Detaching
        items goes through a batch tag-edit instead, plus the whole-collection delete above.
        Inside a benign block, this path falls back to the armed-and-declared rule."""
        session = GuardedSession(SHELF_UNARMED)
        with benign_shelf_write(), pytest.raises(SafetyViolationError, match="turned off"):
            session.delete("http://127.0.0.1:1/library/collections/900/children/42")

    def test_deleting_metadata_is_never_a_shelf_shape(self) -> None:
        """The most important case in this class. ``DELETE /library/metadata/{key}`` removes
        an item and, on a server that allows media deletion, its files too. It must never
        ride the shelf opt-in, whether inside a benign block or not."""
        session = GuardedSession(SHELF_UNARMED)
        with benign_shelf_write(), pytest.raises(SafetyViolationError, match="turned off"):
            session.delete("http://127.0.0.1:1/library/metadata/900")

    def test_the_benign_branch_is_confined_to_the_shelf_endpoints(self) -> None:
        """The shelf-only promise is structural. Inside a benign block, a PUT to any other
        path, such as emptyTrash, falls back to the armed-and-declared rule instead of
        riding the opt-in."""
        session = GuardedSession(SHELF_UNARMED)
        with benign_shelf_write(), pytest.raises(SafetyViolationError, match="turned off"):
            session.put("http://127.0.0.1:1/library/sections/3/emptyTrash")

    def test_the_benign_branch_is_verb_exact(self) -> None:
        """A DELETE to the label-edit path inside a benign block is not a label write,
        and a POST to the collection-items path is not an add."""
        session = GuardedSession(SHELF_UNARMED)
        with benign_shelf_write(), pytest.raises(SafetyViolationError, match="turned off"):
            session.delete("http://127.0.0.1:1/library/sections/3/all?type=1&id=42")
        with benign_shelf_write(), pytest.raises(SafetyViolationError, match="turned off"):
            session.post("http://127.0.0.1:1/library/collections/900/items?uri=x")

    def test_the_opt_in_does_not_unlock_deletions(self) -> None:
        """A delete is not wrapped in benign_shelf_write, so the opt-in flag is invisible
        to it. Allowing a reversible shelf must never widen what can be deleted."""
        session = GuardedSession(SHELF_UNARMED)
        with declared_mutation(), pytest.raises(SafetyViolationError, match="turned off"):
            session.delete("http://127.0.0.1:1/movie/1?deleteFiles=true")

    def test_the_benign_flag_does_not_leak_past_its_block(self) -> None:
        """Outside the context, a write is back on the strict delete path."""
        session = GuardedSession(SHELF_UNARMED)
        with benign_shelf_write():
            pass
        with pytest.raises(SafetyViolationError, match="turned off"):
            session.put("http://127.0.0.1:1/library/x")


class TestGetShapedMutationsAreGated:
    """Plex triggers a section scan with GET /library/sections/{key}/refresh. On a server
    that empties trash after every scan, rescanning a path with missing files purges those
    items. Method filtering alone would let this GET through, so the guard classifies the
    path as a mutation regardless of verb."""

    def test_a_refresh_get_is_blocked_when_read_only(self) -> None:
        session = GuardedSession(READ_ONLY)
        with pytest.raises(SafetyViolationError, match="turned off"):
            session.get("http://127.0.0.1:1/library/sections/3/refresh?path=%2Fmovies%2FX")

    def test_a_refresh_get_is_blocked_when_armed_but_not_declared(self) -> None:
        session = GuardedSession(ARMED)
        with pytest.raises(SafetyViolationError, match="wasn't declared"):
            session.get("http://127.0.0.1:1/library/sections/3/refresh")

    def test_a_refresh_get_passes_when_armed_and_declared(self) -> None:
        """The executor's path. Armed, with the intent journalled, and the request reaches
        the transport."""
        session = GuardedSession(ARMED)
        with _transport() as send, declared_mutation():
            session.get("http://127.0.0.1:1/library/sections/3/refresh", timeout=0.05)

        assert _sent(send) == ("GET", "http://127.0.0.1:1/library/sections/3/refresh")

    def test_an_ordinary_section_read_stays_free(self) -> None:
        """Reads that merely look like the refresh path's neighbors (the section
        listing is_refreshing polls) must not need arming."""
        session = GuardedSession(READ_ONLY)
        with _transport() as send:
            session.get("http://127.0.0.1:1/library/sections/3", timeout=0.05)

        assert _sent(send) == ("GET", "http://127.0.0.1:1/library/sections/3")


class TestLeavingSoonWriteAllowed:
    def test_armed_allows_it(self) -> None:
        assert ARMED.leaving_soon_write_allowed is True

    def test_read_only_blocks_it_by_default(self) -> None:
        assert READ_ONLY.leaving_soon_write_allowed is False

    def test_the_opt_in_allows_it_while_read_only(self) -> None:
        assert SHELF_UNARMED.leaving_soon_write_allowed is True

    def test_the_opt_in_never_allows_deletion(self) -> None:
        assert SHELF_UNARMED.destructive_allowed is False


class TestLabelNormalization:
    """Plex title-cases label tags on the way in. Every comparison must account for it,
    or Reaper fails to find a label it wrote, and 'I can't find my Leaving-Soon mark'
    becomes 'this item isn't flagged, so it's safe to act on'."""

    def test_case_and_whitespace_are_folded(self) -> None:
        assert normalize_label("Leaving-Soon") == normalize_label("leaving-soon")
        assert normalize_label("  reaper-keep ") == normalize_label("Reaper-Keep")

    def test_distinct_labels_stay_distinct(self) -> None:
        assert normalize_label("leaving-soon") != normalize_label("reaper-keep")


class TestSessionTlsChoice:
    """The Plex session honors the per-server certificate choice (requests reads it
    off ``Session.verify``), and on is the only default."""

    def test_verification_defaults_on(self) -> None:
        session = GuardedSession(RuntimeSafety(destructive_enabled=False))
        assert session.verify is True

    def test_the_opt_out_reaches_requests(self) -> None:
        session = GuardedSession(RuntimeSafety(destructive_enabled=False), verify=False)
        assert session.verify is False


class _WritesOnFirstTouch:
    """A stand-in for plexapi, reduced to the one thing that matters here. It issues
    the write.

    Every mutating ``PlexClient`` method reaches plexapi inside an ``asyncio.to_thread``
    closure, and plexapi's writes go through the ``GuardedSession`` the client handed it.
    Standing this up instead of a live server lets all eight methods be driven at once.
    The first attribute the closure touches issues a real PUT through a real guard, and a
    refusing guard raises before the request is dispatched, exactly as it would in
    production.

    The ``AssertionError`` is the control. It fires only if the guard let the write
    through, which would make every assertion below prove nothing.
    """

    def __init__(self, session: GuardedSession) -> None:
        object.__setattr__(self, "_session", session)

    def __getattr__(self, name: str) -> object:
        session: GuardedSession = object.__getattribute__(self, "_session")
        session.put("http://plex.test/library/sections/1/all")
        raise AssertionError(f"the guard let a write through on .{name}")


class TestAGuardRefusalReachesTheCallerAsARefusal:
    """The eight mutating methods route a guard refusal through ahead of their catch-all.

    They all inherit this behavior from ``PlexClient._call``, which is why every case below
    still discriminates: delete the guard-refusal arm from ``_call`` and all eight fail,
    along with the helper case at the end.

    Mapping ``except Exception`` uniformly instead would turn a guard refusal into a
    ``PlexError``. ``leaving_soon.sync_shelves._reconcile`` catches ``PlexError`` per
    library and continues past it, so that would turn a safety refusal into one line
    beside "Plex is unreachable". The executor's ``_flush_refreshes`` and
    ``_finalize_plex`` swallow a ``PlexError`` even more completely, by design.

    **What this pins and what it does not.** It pins that a refusal raised inside the
    worker leaves the method as a ``SafetyViolationError`` rather than a ``PlexError``.
    The guard's own decision, which requests are mutations and when arming and the
    declaration are required, is pinned by the classes above and is not re-derived here.
    """

    @staticmethod
    def _client() -> PlexClient:
        client = PlexClient("http://plex.test", "t", safety=READ_ONLY)
        # Injected so `_connect` returns without a socket. The guard under it is real.
        client._server = _WritesOnFirstTouch(GuardedSession(READ_ONLY))  # type: ignore[assignment]
        return client

    @pytest.mark.parametrize(
        ("name", "call"),
        [
            ("add_label", lambda c: c.add_label(1, [11], "leaving-soon")),
            ("remove_label", lambda c: c.remove_label(1, [11], "leaving-soon")),
            (
                "create_collection",
                lambda c: c.create_collection(
                    1, kind="movie", name="Leaving Soon", rating_keys=[11]
                ),
            ),
            ("add_to_collection", lambda c: c.add_to_collection(9, [11])),
            (
                "remove_collection_members",
                lambda c: c.remove_collection_members(1, name="Leaving Soon", rating_keys=[11]),
            ),
            ("delete_collection", lambda c: c.delete_collection(9)),
            ("refresh_path", lambda c: c.refresh_path(1, "/media/movies/x")),
            ("empty_trash", lambda c: c.empty_trash(1)),
        ],
    )
    async def test_the_refusal_is_not_relabeled_as_a_plex_error(self, name: str, call: Any) -> None:
        """One case per mutating method, so a single arm dropped fails by name.

        This asserts ``SafetyViolationError`` explicitly, rather than only checking the
        result is not a ``PlexError``. The two are unrelated classes, so the raises clause
        already tells them apart, and naming the expected error is what makes a failure
        message say which relabeling happened.
        """
        client = self._client()
        with pytest.raises(SafetyViolationError, match="turned off"):
            await call(client)

    async def test_the_stand_in_reaches_a_real_guard(self) -> None:
        """The control for the control. If ``_WritesOnFirstTouch`` stopped issuing a
        request, every case above would pass on an ``AttributeError`` that never touched
        the guard, and the class would read as a proof of nothing."""
        session = GuardedSession(ARMED)
        with (
            declared_mutation(),
            _transport() as send,
            pytest.raises(AssertionError, match="let a write through"),
        ):
            _WritesOnFirstTouch(session).library  # noqa: B018
        assert _sent(send) == ("PUT", "http://plex.test/library/sections/1/all")

    async def test_a_write_that_declares_no_arm_of_its_own_still_refuses(self) -> None:
        """Proves the refusal comes from ``_call`` itself, not from a per-method safety arm.

        The cases above go through the eight methods that exist today. This one goes
        through ``_call`` with a body that has no arm anywhere near it, the shape a ninth
        mutating method written next year would take: someone adds a closure and hands it
        to ``_call``, and the refusal reaches the caller without their having thought about
        it. Delete the arm from ``_call`` and this test fails while every case above still
        passes, because those assert the methods and this asserts the helper directly.
        """
        client = self._client()

        def refuses() -> None:
            raise SafetyViolationError("deletion is turned off on this host")

        with pytest.raises(SafetyViolationError, match="turned off"):
            await client._call(refuses, what="do something nobody has written yet")

    async def test_an_ordinary_plexapi_failure_is_still_mapped(self) -> None:
        """The other half of that arm-ahead-of-catch-all design. Anything that is not a
        refusal still becomes a ``PlexError`` naming the attempt, so a caller's
        ``except PlexError`` keeps working.

        The ``what`` is the whole message minus the exception text, so this also pins the
        helper's message template."""
        client = self._client()

        def boom() -> None:
            raise ValueError("the server said no")

        with pytest.raises(PlexError, match="Could not read the thing: the server said no"):
            await client._call(boom, what="read the thing")


class TestEveryRefusalIsOnTheRecord:
    """Checks that every guard refusal leaves a trace in the log.

    At the point of refusal nothing is written by default, so the only trace is whatever
    the caller makes of the exception. The executor's ``_flush_refreshes`` and
    ``_finalize_plex`` catch ``Exception`` on purpose, because a reap must not fail on a
    follow-up, and each logs the refusal under an event naming the wrong cause. What
    matters is having a discriminator to tell refusals apart, not the wording.
    ``sync_shelves._reconcile`` catches only ``PlexError``, so a shelf refusal passes
    through it untouched today.

    ``reason`` is that discriminator, not the logged sentence. The message is operator
    copy and can be reworded, so anything reading these log lines matches on ``reason``
    instead.
    """

    @pytest.mark.parametrize(
        ("safety", "declared", "reason"),
        [
            (READ_ONLY, False, "not_armed"),
            (ARMED, False, "not_declared"),
        ],
    )
    def test_a_blocked_plex_write_says_why(
        self, safety: RuntimeSafety, declared: bool, reason: str
    ) -> None:
        session = GuardedSession(safety)
        with capture_logs() as logs, pytest.raises(SafetyViolationError):
            if declared:
                with declared_mutation():
                    session.put("http://plex.test/library/sections/1/all")
            else:
                session.put("http://plex.test/library/sections/1/all")

        blocked = [line for line in logs if line["event"] == "plex.write_blocked"]
        assert len(blocked) == 1, logs
        assert blocked[0]["reason"] == reason
        assert blocked[0]["method"] == "PUT"
        assert blocked[0]["path"] == "/library/sections/1/all"

    def test_the_shelf_refusal_says_why_too(self) -> None:
        """The third arm, which the other two cannot reach: a benign shelf shape while
        read-only and without the operator's opt-in."""
        session = GuardedSession(READ_ONLY)
        with (
            capture_logs() as logs,
            benign_shelf_write(),
            pytest.raises(SafetyViolationError),
        ):
            session.put("http://plex.test/library/sections/1/all")

        blocked = [line for line in logs if line["event"] == "plex.write_blocked"]
        assert [line["reason"] for line in blocked] == ["shelf_not_allowed"]

    def test_the_path_carries_no_token(self) -> None:
        """plexapi puts X-Plex-Token in the query string. The guard splits it off before
        deciding, and the logged path is that split value, never the URL."""
        session = GuardedSession(READ_ONLY)
        with capture_logs() as logs, pytest.raises(SafetyViolationError):
            session.put("http://plex.test/library/sections/1/all?X-Plex-Token=supersecret")

        blocked = [line for line in logs if line["event"] == "plex.write_blocked"]
        assert blocked[0]["path"] == "/library/sections/1/all"
        assert "supersecret" not in repr(blocked)

    def test_an_allowed_write_says_nothing(self) -> None:
        """The control. A guard that logged every mutation would bury the refusals."""
        session = GuardedSession(ARMED)
        with capture_logs() as logs, declared_mutation(), _transport():
            session.put("http://plex.test/library/sections/1/all")

        assert [line for line in logs if line["event"] == "plex.write_blocked"] == []


class TestTheConnectionCarriesReapersOwnTimeout:
    """``query`` reads the timeout off the server object and passes it explicitly on every
    call, so a default set on the session alone would be overridden and the widening would
    read as applied while doing nothing."""

    async def test_the_server_is_built_with_the_wider_budget(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import plexapi
        import plexapi.server

        captured: dict[str, Any] = {}

        class _Stub:
            def __init__(
                self, baseurl: str, token: str, session: Any = None, timeout: int | None = None
            ) -> None:
                captured["timeout"] = timeout

        monkeypatch.setattr(plexapi.server, "PlexServer", _Stub)
        await PlexClient("http://plex.test", "t", safety=READ_ONLY).connect()

        assert captured["timeout"] == PLEX_READ_TIMEOUT
        # Wider than plexapi's own default, which the constant alone cannot prove.
        assert PLEX_READ_TIMEOUT > plexapi.TIMEOUT
