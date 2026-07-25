# SPDX-License-Identifier: AGPL-3.0-or-later
"""The Plex client is held to the same safety rule as everything else.

plexapi speaks ``requests``, not ``httpx``, so it does not pass through
``GuardedTransport``. Without a second guard on the requests side, every Plex write --
labels, collections, and ``emptyTrash`` -- would be the one destructive path in the
codebase with no interlock in front of it. ``GuardedSession`` closes that, and these
tests pin it shut.

Nothing here talks to a real Plex server: GuardedSession makes its refusal decision
*before* the request is dispatched, so a blocked call never opens a socket.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from unittest import mock

import pytest
import requests

from reaper.clients.base import SafetyViolationError
from reaper.clients.plex import (
    GuardedSession,
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
    the request and hands it to ``Session.send``. Patching ``send`` therefore proves a call
    got all the way through -- and says WHICH call did.

    These tests used to prove the allowed cases by firing a live request at
    ``127.0.0.1:1`` and asserting ``pytest.raises(Exception)`` plus "not a
    SafetyViolationError". That is unfalsifiable in practice: a ``TypeError`` from a bad
    kwarg or an ``AttributeError`` from a refactored ``GuardedSession`` satisfies it just as
    well, so the label-write test would still have passed against a session that was broken
    outright -- and it depended on TCP port 1 being closed on the runner.
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
        """Deletion is off. No declaration can lift it -- off means off, and it is turned
        on only from the UI with the admin password, never by a stray declaration."""
        session = GuardedSession(READ_ONLY)

        with declared_mutation(), pytest.raises(SafetyViolationError, match="turned off"):
            session.put("http://127.0.0.1:1/library/x")

    def test_a_put_is_blocked_when_armed_but_not_declared(self) -> None:
        """Armed, but nobody wrote the intent to the journal. The whole point of the
        declaration is that a mutation which skipped the executor cannot reach Plex."""
        session = GuardedSession(ARMED)

        with pytest.raises(SafetyViolationError, match="not declared"):
            session.put("http://127.0.0.1:1/library/x")

    def test_turning_deletion_off_blocks_even_a_declared_mutation(self) -> None:
        """Deletion off wins over everything. A run in flight, intent duly journalled --
        with deletion turned off, the guard still refuses it."""
        session = GuardedSession(READ_ONLY)

        with declared_mutation(), pytest.raises(SafetyViolationError):
            session.delete("http://127.0.0.1:1/library/x")

    def test_the_declaration_does_not_leak_past_its_block(self) -> None:
        """The context manager is the whole window. Once it closes, a later write is
        undeclared again -- a declaration cannot bleed into unrelated code."""
        session = GuardedSession(ARMED)

        with declared_mutation():
            pass

        with pytest.raises(SafetyViolationError, match="not declared"):
            session.post("http://127.0.0.1:1/library/x")


class TestTheBenignShelfIsGatedSeparately:
    """Writing the Leaving Soon shelf (label + collection) is a mutation, but a
    reversible one that touches no file. It is gated like a delete by default, and an
    operator can opt in to allowing it while read-only -- but that opt-in must never
    leak into the deletion path."""

    def test_the_label_is_blocked_when_read_only_and_not_opted_in(self) -> None:
        session = GuardedSession(READ_ONLY)
        with benign_shelf_write(), pytest.raises(SafetyViolationError, match="Leaving Soon"):
            session.put("http://127.0.0.1:1/library/sections/3/all?type=1&id=42")

    def test_the_label_writes_when_armed(self) -> None:
        """Armed is at least as permissive as a delete, so the label goes through to the
        transport. No declaration needed: the shelf is not journalled through the executor."""
        session = GuardedSession(ARMED)
        with _transport() as send, benign_shelf_write():
            session.put("http://127.0.0.1:1/library/sections/3/all?type=1&id=42", timeout=0.05)

        assert _sent(send) == ("PUT", "http://127.0.0.1:1/library/sections/3/all?type=1&id=42")

    def test_the_label_writes_read_only_when_opted_in(self) -> None:
        """The whole point: the warning can be written during the grace countdown, before
        deletion is ever enabled."""
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
        """The one DELETE the shelf may issue: dropping the whole (emptied) collection in
        one request. It removes only the collection object, never an item or its files."""
        session = GuardedSession(SHELF_UNARMED)
        with _transport() as send, benign_shelf_write():
            session.delete("http://127.0.0.1:1/library/collections/900", timeout=0.05)

        assert _sent(send) == ("DELETE", "http://127.0.0.1:1/library/collections/900")

    def test_detaching_one_collection_child_is_no_longer_a_shelf_shape(self) -> None:
        """The per-item ``.../children/{key}`` DELETE was replaced by a batch tag-edit
        (detach many at once) plus the whole-collection delete above, so it is no longer on
        the benign list: inside a benign block it falls back to the armed-and-declared rule."""
        session = GuardedSession(SHELF_UNARMED)
        with benign_shelf_write(), pytest.raises(SafetyViolationError, match="turned off"):
            session.delete("http://127.0.0.1:1/library/collections/900/children/42")

    def test_deleting_metadata_is_never_a_shelf_shape(self) -> None:
        """THE load-bearing negative. ``DELETE /library/metadata/{key}`` removes an item
        and -- on a server allowing media deletion -- its files. It must never ride the
        shelf opt-in, inside a benign block or not."""
        session = GuardedSession(SHELF_UNARMED)
        with benign_shelf_write(), pytest.raises(SafetyViolationError, match="turned off"):
            session.delete("http://127.0.0.1:1/library/metadata/900")

    def test_the_benign_branch_is_confined_to_the_shelf_endpoints(self) -> None:
        """The 'shelf only' promise is structural: inside a benign block, a PUT to any
        OTHER path (say, emptyTrash) falls back to the armed-and-declared rule instead
        of riding the opt-in."""
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
        """A delete is NOT wrapped in benign_shelf_write, so the opt-in flag is invisible
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
    """Plex triggers a section scan with GET /library/sections/{key}/refresh -- and on a
    server that empties trash after every scan, rescanning a path with missing files
    purges those items. Method filtering alone would wave it through, so the guard
    classifies the path as a mutation regardless of verb."""

    def test_a_refresh_get_is_blocked_when_read_only(self) -> None:
        session = GuardedSession(READ_ONLY)
        with pytest.raises(SafetyViolationError, match="turned off"):
            session.get("http://127.0.0.1:1/library/sections/3/refresh?path=%2Fmovies%2FX")

    def test_a_refresh_get_is_blocked_when_armed_but_not_declared(self) -> None:
        session = GuardedSession(ARMED)
        with pytest.raises(SafetyViolationError, match="not declared"):
            session.get("http://127.0.0.1:1/library/sections/3/refresh")

    def test_a_refresh_get_passes_when_armed_and_declared(self) -> None:
        """The executor's path: armed, intent journalled, and the request reaches the
        transport."""
        session = GuardedSession(ARMED)
        with _transport() as send, declared_mutation():
            session.get("http://127.0.0.1:1/library/sections/3/refresh", timeout=0.05)

        assert _sent(send) == ("GET", "http://127.0.0.1:1/library/sections/3/refresh")

    def test_an_ordinary_section_read_stays_free(self) -> None:
        """Reads that merely LOOK like the refresh path's neighbors (the section
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
    or Reaper fails to find a label it wrote -- and 'I can't find my Leaving-Soon mark'
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
