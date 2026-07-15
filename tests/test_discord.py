# SPDX-License-Identifier: AGPL-3.0-or-later
"""Discord notifications.

The behaviour that matters is not "does it send a nice embed" -- it is "can a broken
webhook ever break Reaper". So the failure paths get the most attention: every one must
return False and stay silent, never raise, and never log the URL.
"""

from __future__ import annotations

import httpx
import pytest
import respx

from reaper.notify.discord import DiscordNotifier, Embed

WEBHOOK = "https://discord.com/api/webhooks/123/token-is-a-secret"


def _notifier() -> DiscordNotifier:
    # build_notifier now resolves the webhook from the database; that path is exercised in
    # tests/test_review_backend_core_B.py. These tests are about the notifier's own
    # behaviour, so they construct it directly against the webhook.
    return DiscordNotifier(WEBHOOK)


class TestPost:
    @respx.mock
    async def test_a_2xx_is_success(self) -> None:
        route = respx.post(WEBHOOK).mock(return_value=httpx.Response(204))
        notifier = _notifier()
        assert await notifier.post(Embed(title="hi", description="there")) is True
        assert route.called
        # The embed structure Discord expects.
        sent = route.calls.last.request
        assert b'"embeds"' in sent.content

    @respx.mock
    async def test_a_4xx_is_a_quiet_failure(self) -> None:
        respx.post(WEBHOOK).mock(return_value=httpx.Response(400, json={"message": "bad"}))
        notifier = _notifier()
        assert await notifier.post(Embed(title="hi", description="x")) is False

    @respx.mock
    async def test_a_network_error_never_raises(self) -> None:
        respx.post(WEBHOOK).mock(side_effect=httpx.ConnectError("down"))
        notifier = _notifier()
        # The whole point: a dead webhook returns False, it does not blow up the caller.
        assert await notifier.post(Embed(title="hi", description="x")) is False


class TestLeavingSoon:
    @respx.mock
    async def test_it_counts_and_lists_titles(self) -> None:
        route = respx.post(WEBHOOK).mock(return_value=httpx.Response(204))
        notifier = _notifier()
        ok = await notifier.announce_leaving_soon(["A", "B", "C"], grace_days=14)
        assert ok is True
        body = route.calls.last.request.content.decode()
        assert "3 titles are leaving soon" in body
        assert "14-day grace" in body

    @respx.mock
    async def test_a_single_title_is_singular(self) -> None:
        route = respx.post(WEBHOOK).mock(return_value=httpx.Response(204))
        notifier = _notifier()
        await notifier.announce_leaving_soon(["Solo"], grace_days=14)
        assert "1 title is leaving soon" in route.calls.last.request.content.decode()

    @respx.mock
    async def test_a_long_list_is_truncated_but_the_count_is_whole(self) -> None:
        route = respx.post(WEBHOOK).mock(return_value=httpx.Response(204))
        notifier = _notifier()
        titles = [f"Film {i}" for i in range(50)]
        await notifier.announce_leaving_soon(titles, grace_days=14)
        body = route.calls.last.request.content.decode()
        assert "50 titles are leaving soon" in body  # honest headline
        assert "and 30 more" in body  # 50 - 20 shown

    async def test_an_empty_list_sends_nothing(self) -> None:
        notifier = _notifier()
        # No respx route registered: if it tried to post, it would error. It must not.
        assert await notifier.announce_leaving_soon([], grace_days=14) is False


class TestTheUrlIsNeverLogged:
    @respx.mock
    async def test_logs_carry_the_status_not_the_url(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        respx.post(WEBHOOK).mock(return_value=httpx.Response(400))
        notifier = _notifier()
        await notifier.post(Embed(title="hi", description="x"))
        out = capsys.readouterr()
        assert "token-is-a-secret" not in (out.out + out.err)
