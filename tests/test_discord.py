# SPDX-License-Identifier: AGPL-3.0-or-later
"""Discord notifications.

The behavior that matters is not "does it send a nice embed" -- it is "can a broken
webhook ever break Reaper". So the failure paths get the most attention: every one must
return False and stay silent, never raise, and never log the URL.

notify/discord.py is on httpx2 (Reaper is mid-migration off the unmaintained httpx; see
docs/history/PLAN-narrative.md). respx cannot intercept an httpx2 client, so these use
the ``httpx2_mock``
fixture from pytest-httpx2, which is a ``respx.Router`` wired to httpx2. The Router API is
respx's: the mocked ``return_value`` is an ``httpx.Response`` (respx's own currency, which
the plugin hands to the httpx2 client), while a ``side_effect`` exception is an httpx2 one
(so the client raises the type discord.py actually catches).
"""

from __future__ import annotations

import httpx
import httpx2
import pytest
import respx

from reaper.notify.discord import DiscordNotifier, Embed

WEBHOOK = "https://discord.com/api/webhooks/123/token-is-a-secret"


def _notifier() -> DiscordNotifier:
    # build_notifier now resolves the webhook from the database; that path is exercised in
    # tests/test_review_backend_core_B.py. These tests are about the notifier's own
    # behavior, so they construct it directly against the webhook.
    return DiscordNotifier(WEBHOOK)


class TestPost:
    async def test_a_2xx_is_success(self, httpx2_mock: respx.Router) -> None:
        route = httpx2_mock.post(WEBHOOK).mock(return_value=httpx.Response(204))
        notifier = _notifier()
        assert await notifier.post(Embed(title="hi", description="there")) is True
        assert route.called
        # The embed structure Discord expects.
        sent = route.calls.last.request
        assert b'"embeds"' in sent.content

    async def test_a_4xx_is_a_quiet_failure(self, httpx2_mock: respx.Router) -> None:
        httpx2_mock.post(WEBHOOK).mock(return_value=httpx.Response(400, json={"message": "bad"}))
        notifier = _notifier()
        assert await notifier.post(Embed(title="hi", description="x")) is False

    async def test_a_network_error_never_raises(self, httpx2_mock: respx.Router) -> None:
        httpx2_mock.post(WEBHOOK).mock(side_effect=httpx2.ConnectError("down"))
        notifier = _notifier()
        # A dead webhook returns False, it does not blow up the caller.
        assert await notifier.post(Embed(title="hi", description="x")) is False


class TestLeavingSoon:
    async def test_it_counts_and_lists_titles(self, httpx2_mock: respx.Router) -> None:
        route = httpx2_mock.post(WEBHOOK).mock(return_value=httpx.Response(204))
        notifier = _notifier()
        ok = await notifier.announce_leaving_soon(["A", "B", "C"], grace_days=14)
        assert ok is True
        body = route.calls.last.request.content.decode()
        assert "3 titles are leaving soon" in body
        # The countdown reaches your users as a notice, never as a deadline or a
        # reprieve: nothing on the deletion path reads the grace window (see the module
        # docstring on services/grace.py), so the owner may reap on day one. The rescue is
        # therefore stated unconditioned on the clock, and the days describe only how long
        # the titles SHOW as leaving.
        assert "Watch one and it stays" in body
        assert "leaving for the next 14 days" in body
        assert "Nothing is removed automatically" in body
        # The retired promise, which offered a runway the code does not enforce.
        assert "in the next 14 days and it stays" not in body
        # The shelf is not an unwatched set. A hand reap overrules a fired min_dormancy and
        # lands the item here, so the most ordinary way onto this list is the owner watching
        # something and reaping it the next day to reclaim the space. The blanket
        # "Unwatched" this once opened with announced that to every user as a fact
        # about a film one of them had just played.
        assert "Unwatched" not in body
        assert "unwatched" not in body

    async def test_a_single_title_is_singular(self, httpx2_mock: respx.Router) -> None:
        route = httpx2_mock.post(WEBHOOK).mock(return_value=httpx.Response(204))
        notifier = _notifier()
        await notifier.announce_leaving_soon(["Solo"], grace_days=14)
        assert "1 title is leaving soon" in route.calls.last.request.content.decode()

    async def test_a_long_list_is_truncated_but_the_count_is_whole(
        self, httpx2_mock: respx.Router
    ) -> None:
        route = httpx2_mock.post(WEBHOOK).mock(return_value=httpx.Response(204))
        notifier = _notifier()
        titles = [f"Film {i}" for i in range(50)]
        await notifier.announce_leaving_soon(titles, grace_days=14)
        body = route.calls.last.request.content.decode()
        assert "50 titles are leaving soon" in body  # honest headline
        assert "and 30 more" in body  # 50 - 20 shown

    async def test_an_empty_list_sends_nothing(self, httpx2_mock: respx.Router) -> None:
        notifier = _notifier()
        # No route registered: if it tried to post, the mock would flag an unexpected
        # request. It must not.
        assert await notifier.announce_leaving_soon([], grace_days=14) is False


class TestTheUrlIsNeverLogged:
    async def test_logs_carry_the_status_not_the_url(
        self, httpx2_mock: respx.Router, capsys: pytest.CaptureFixture[str]
    ) -> None:
        httpx2_mock.post(WEBHOOK).mock(return_value=httpx.Response(400))
        notifier = _notifier()
        await notifier.post(Embed(title="hi", description="x"))
        out = capsys.readouterr()
        assert "token-is-a-secret" not in (out.out + out.err)
