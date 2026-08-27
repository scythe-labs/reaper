# SPDX-License-Identifier: AGPL-3.0-or-later
"""Discord notifications.

The Discord webhook is the channel people actually see "these titles are leaving
soon" on. The Plex "Leaving Soon" collection only reaches users who pinned that
library, and a server cannot force that, so the webhook is what makes the grace
period a real warning instead of a formality.

Two rules apply to everything in this module:

* A notification is a courtesy, never a gate. Every failure is caught and logged, so
  nothing here can raise into a scan, a plan, or a run. The worst a broken webhook can
  do is fail to send a message.
* The webhook URL is a credential: the token lives in the URL path, so the whole URL
  is a secret. It is never logged, never echoed, and never put in an error shown to
  the operator. This module posts to Discord with a plain httpx2 client rather than
  ``GuardedTransport``, because it never mutates anyone's library.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

import httpx2
import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from reaper.config import Settings
from reaper.crypto import SecretBox
from reaper.i18n import DEFAULT_TAG, say
from reaper.services import app_settings

log = structlog.get_logger(__name__)

# Discord embeds cap descriptions at 4096 characters and fields at 25. We stay well
# under both by capping the list of titles we enumerate.
_MAX_TITLES = 20
_TIMEOUT = httpx2.Timeout(connect=5.0, read=10.0, write=10.0, pool=5.0)

# Discord rate-limits per webhook and returns 429 with a Retry-After. We honor it once
# and cap how long we wait. A notification is a courtesy, not a gate, so a webhook
# that asks us to sleep for minutes must never stall a scan, a plan, or a run. If it
# still returns 429 after the retry, we fall through to the ordinary "rejected" path.
_MAX_RETRY_AFTER = 5.0

# Muted amber, so the color reads as a gentle notice rather than an alarm.
_LEAVING_SOON_COLOR = 0xE8A33D


@dataclass(frozen=True)
class Embed:
    title: str
    description: str
    color: int = _LEAVING_SOON_COLOR

    def to_payload(self) -> dict[str, object]:
        return {
            "embeds": [{"title": self.title, "description": self.description, "color": self.color}]
        }


class DiscordNotifier:
    """Posts embeds to one webhook.

    Construct through :func:`build_notifier`, which returns ``None`` when no webhook is
    configured. That lets a caller write ``if notifier: await notifier.post(...)``
    instead of threading webhook config through every call site.

    ``app_name`` is the sender name the message shows (Settings -> General), so two
    Reaper installs posting into the same channel stay easy to tell apart. ``app_url``
    adds an "open" link at the end of list messages when set, and adds nothing when
    empty. ``language`` is the BCP 47 tag every ``say(...)`` call in this class reads
    its text through (Settings -> Notifications). It defaults to English until the
    operator picks one of ``reaper.i18n.shipped_tags()``.
    """

    def __init__(
        self,
        webhook_url: str,
        *,
        app_name: str = "Reaper",
        app_url: str | None = None,
        language: str = DEFAULT_TAG,
    ) -> None:
        self._url = webhook_url
        self._app_name = app_name
        self._app_url = app_url
        self._language = language

    async def post(self, embed: Embed) -> bool:
        """Send one embed and report whether it landed. Never raises.

        The URL itself is never logged, on success or failure. The outcome and status
        code carry everything useful without carrying the token.
        """
        payload = embed.to_payload()
        payload["username"] = self._app_name
        # The client is built and closed here, on every path, so nothing outside this
        # method owns it.
        async with httpx2.AsyncClient(timeout=_TIMEOUT) as client:
            try:
                response = await client.post(self._url, json=payload)
                # A 429 is not a real failure. The same message can land moments later,
                # so we honor Retry-After and retry once, within a bounded wait, instead
                # of dropping the warning.
                if response.status_code == 429:
                    response = await self._retry_after_429(client, payload, embed.title, response)
                response.raise_for_status()
                log.info("discord.posted", status=response.status_code, title=embed.title)
                return True
            except httpx2.HTTPStatusError as exc:
                log.warning("discord.rejected", status=exc.response.status_code, title=embed.title)
                return False
            except Exception as exc:
                # Broad on purpose. This module never raises into a scan, a plan, or a
                # run, and httpx2.InvalidURL subclasses Exception directly rather than
                # HTTPError, so a malformed webhook (an embedded newline, a control
                # character, a bad IPv6 host) would slip past a narrower
                # ``except httpx2.HTTPError``. We log only the exception type name:
                # str(exc) can include the request URL, and the URL is the credential.
                log.warning("discord.unreachable", error=type(exc).__name__, title=embed.title)
                return False

    async def _retry_after_429(
        self,
        client: httpx2.AsyncClient,
        payload: dict[str, object],
        title: str,
        response: httpx2.Response,
    ) -> httpx2.Response:
        """Wait out one rate limit, then re-post the same payload.

        Returns the retry's response, or the original 429 response unchanged when
        there is no usable wait time. Either way, the caller runs ``raise_for_status``
        next, so a still-failing 429 ends up on the ordinary ``discord.rejected`` path.
        """
        retry_after = _parse_retry_after(response.headers.get("Retry-After"))
        if retry_after is None:
            return response
        # Log the status only, never the URL. The token lives in the URL path.
        log.info("discord.rate_limited", retry_after=retry_after, title=title)
        await asyncio.sleep(min(retry_after, _MAX_RETRY_AFTER))
        return await client.post(self._url, json=payload)

    async def announce_leaving_soon(self, titles: list[str], *, grace_days: int) -> bool:
        """Announce "These N titles are leaving soon." The count in the title is
        always accurate, even when the list of names below it is cut short.

        ``grace_days`` says how long titles are shown as leaving, not a promise or a
        deadline. Nothing on the deletion path checks this window (see the module
        docstring on ``services.grace``), so text like "watch it within 14 days and it
        stays" would promise something the code does not enforce: the owner can delete
        a title on day one. The one true statement is that watching a title keeps it,
        and a person decides every deletion.

        This shelf can include titles someone has already watched.
        ``services.leaving_soon`` builds it from ``condemned.effective_condemned``,
        which includes items the owner marked for removal by hand. A hand-picked
        removal overrides the ``min_dormancy`` gate, which is not one of
        ``verdict.STRUCTURAL_GATES`` and so can be overruled this way. So a common way
        a title lands on this shelf is: the owner watches something, decides the next
        day to reclaim the space, and marks it for removal themselves. Calling the
        whole shelf "Unwatched" would then tell that owner a title they watched last
        night had never been played. This method has no way to check each title's
        watched state, so it leaves that claim out rather than guess at it.

        Written in ``self._language`` (Settings -> Notifications). English until the
        operator changes it.
        """
        if not titles:
            return False
        tag = self._language
        shown = titles[:_MAX_TITLES]
        lines = "\n".join(f"• {t}" for t in shown)
        remaining = len(titles) - _MAX_TITLES
        if remaining > 0:
            lines += "\n" + say("discord.leaving_soon.more", tag, remaining=remaining)
        # The link back to Reaper: the Application URL from Settings -> General, when set.
        link = ""
        if self._app_url:
            link_text = say("discord.leaving_soon.open_link", tag, app_name=self._app_name)
            link = f"\n\n[{link_text}]({self._app_url})"
        body = say("discord.leaving_soon.body", tag, grace_days=grace_days)
        return await self.post(
            Embed(
                title=say("discord.leaving_soon.title", tag, count=len(titles)),
                description=f"{body}\n\n{lines}{link}",
            )
        )


def _parse_retry_after(raw: str | None) -> float | None:
    """Discord sends Retry-After as seconds, often fractional. A value that fails to
    parse, or a negative one, becomes ``None``. Skipping the retry is safer than
    guessing how long to wait."""
    if not raw:
        return None
    try:
        seconds = float(raw)
    except ValueError:
        return None
    return seconds if seconds >= 0 else None


async def build_notifier(
    session: AsyncSession, box: SecretBox, settings: Settings
) -> DiscordNotifier | None:
    """A notifier when a webhook is configured, or ``None`` when notifications are off.

    The webhook lives in the database, Fernet-encrypted and edited in the web UI. This
    reads it through ``app_settings`` from a live session rather than from
    ``Settings``, because ``REAPER_DISCORD_WEBHOOK`` only seeds the value on first boot.
    """
    webhook = await app_settings.get_discord_webhook(session, box, settings)
    if webhook is None:
        return None
    return DiscordNotifier(
        webhook,
        app_name=await app_settings.get_application_name(session),
        app_url=await app_settings.get_application_url(session),
        language=await app_settings.get_notification_language(session),
    )
