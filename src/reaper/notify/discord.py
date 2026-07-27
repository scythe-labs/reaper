# SPDX-License-Identifier: AGPL-3.0-or-later
"""Discord notifications.

The Discord webhook is the *real* channel for "these titles are leaving soon". The Plex
"Leaving Soon" collection only reaches users who have pinned the library -- something no
server can force -- so a webhook to a channel people actually watch is what makes the
grace period a warning rather than a formality.

Two rules run through everything here:

* **Best-effort.** A notification is a courtesy, never a gate. Every failure is caught and
  logged; nothing in this module can raise into a scan, a plan, or a run. The worst a
  broken webhook can do is not send a message.
* **The URL is a credential.** A Discord webhook token lives in the path, so the whole URL
  is a secret -- it is never logged, never echoed, never put in an error surfaced to the
  UI. This is not a managed integration, so it uses a plain httpx2 client, *not* the
  GuardedTransport: it posts to Discord, it does not mutate anyone's library.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

import httpx2
import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from reaper.config import Settings
from reaper.crypto import SecretBox
from reaper.services import app_settings

log = structlog.get_logger(__name__)

# Discord's embed limits, the ones we can actually hit: 4096 chars of description, 25
# fields. We stay well under by capping the list of titles we enumerate.
_MAX_TITLES = 20
_TIMEOUT = httpx2.Timeout(connect=5.0, read=10.0, write=10.0, pool=5.0)

# Discord rate-limits per webhook and returns 429 with a Retry-After. We honor it once,
# but a notification is a courtesy, not a gate -- so the wait is bounded: a webhook that
# asks us to sleep for minutes must never stall a scan/plan/run. Cap the wait, retry once,
# and if it still 429s fall through to the ordinary "rejected" path.
_MAX_RETRY_AFTER = 5.0

# Muted amber -- "on its way out", not an alarm.
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
    """Posts embeds to one webhook. Construct via :func:`build_notifier`, which returns
    ``None`` when no webhook is configured -- so callers express "notify if we can" as
    ``if notifier: await notifier.post(...)`` rather than threading config everywhere.

    ``app_name`` is the sender name the message wears (Settings -> General), so two
    installs posting into one channel stay tellable-apart. ``app_url`` adds an "open"
    link at the end of list messages; empty means no link, never a broken one.
    """

    def __init__(
        self,
        webhook_url: str,
        *,
        client: httpx2.AsyncClient | None = None,
        app_name: str = "Reaper",
        app_url: str | None = None,
    ) -> None:
        self._url = webhook_url
        self._client = client
        self._app_name = app_name
        self._app_url = app_url

    async def post(self, embed: Embed) -> bool:
        """Send one embed. Returns whether it landed; never raises.

        The URL is not logged on success or failure -- only the outcome and the status
        code, which carry everything useful without carrying the token.
        """
        client = self._client or httpx2.AsyncClient(timeout=_TIMEOUT)
        payload = embed.to_payload()
        payload["username"] = self._app_name
        try:
            response = await client.post(self._url, json=payload)
            # A 429 is not a real failure -- the same message can land moments later. Honor
            # Retry-After and retry once (bounded) rather than silently dropping the warning.
            if response.status_code == 429:
                response = await self._retry_after_429(client, payload, embed.title, response)
            response.raise_for_status()
            log.info("discord.posted", status=response.status_code, title=embed.title)
            return True
        except httpx2.HTTPStatusError as exc:
            log.warning("discord.rejected", status=exc.response.status_code, title=embed.title)
            return False
        except Exception as exc:
            # Broad on purpose. The module's guarantee is that *nothing here raises into a
            # scan, a plan, or a run*, and httpx2.InvalidURL is NOT an HTTPError -- it
            # subclasses Exception directly -- so a malformed webhook (an embedded newline,
            # a control char, a bad IPv6 host) would slip past a narrower ``except
            # httpx2.HTTPError``. We log the type name only: str(exc) can carry the request
            # URL, and the URL is the credential.
            log.warning("discord.unreachable", error=type(exc).__name__, title=embed.title)
            return False
        finally:
            if self._client is None:
                await client.aclose()

    async def _retry_after_429(
        self,
        client: httpx2.AsyncClient,
        payload: dict[str, object],
        title: str,
        response: httpx2.Response,
    ) -> httpx2.Response:
        """Wait out one rate-limit and re-post the same payload. Returns the retry's
        response, or the original 429 unchanged when there is nothing useful to wait on
        -- either way the caller runs ``raise_for_status`` next, so a still-failing 429
        becomes the ordinary ``discord.rejected`` path."""
        retry_after = _parse_retry_after(response.headers.get("Retry-After"))
        if retry_after is None:
            return response
        # The status, not the URL: the token lives in the path.
        log.info("discord.rate_limited", retry_after=retry_after, title=title)
        await asyncio.sleep(min(retry_after, _MAX_RETRY_AFTER))
        return await client.post(self._url, json=payload)

    async def announce_leaving_soon(self, titles: list[str], *, grace_days: int) -> bool:
        """ "These N titles are leaving soon." The headline count is honest even when the
        enumerated list is truncated for length.

        ``grace_days`` is described as how long they *show* as leaving, never as a deadline
        or a reprieve. Nothing on the deletion path reads the window (see the module
        docstring on ``services.grace``), so a promise like "watch it in the next 14 days
        and it stays" would offer a runway the code does not enforce: the owner can reap on
        day one. What is true is the outcome, unconditioned on the clock -- watching it
        keeps it, and a person starts every removal.

        The shelf is NOT an unwatched set, so this says nothing about whether anyone has
        played these. ``services.leaving_soon`` builds it from the in-grace slice of
        ``condemned.effective_condemned``, which merges hand reaps, and a hand reap
        overrules a fired ``min_dormancy`` (that gate is not in ``verdict.STRUCTURAL_GATES``)
        -- so the most ordinary way onto this shelf is the owner watching something, finding
        it in Sanctuary the next day, and reaping it to reclaim the space. The blanket
        "Unwatched" this used to open with then told the whole household that a film one of
        them watched last night had gone unplayed. The dormancy that would let it branch per
        title is not on this call, so the claim is dropped rather than guessed.
        """
        if not titles:
            return False
        shown = titles[:_MAX_TITLES]
        lines = "\n".join(f"• {t}" for t in shown)
        if len(titles) > _MAX_TITLES:
            lines += f"\n…and {len(titles) - _MAX_TITLES} more."
        count = len(titles)
        noun = "title is" if count == 1 else "titles are"
        # The way back in: the Application URL from Settings -> General, when set.
        link = f"\n\n[Open {self._app_name}]({self._app_url})" if self._app_url else ""
        return await self.post(
            Embed(
                title=f"{count} {noun} leaving soon",
                description=(
                    f"Watch one and it stays. Nothing "
                    f"is removed automatically: they show as leaving for the next "
                    f"{grace_days} days, and every removal is started by hand.\n\n"
                    f"{lines}{link}"
                ),
            )
        )


def _parse_retry_after(raw: str | None) -> float | None:
    """Discord sends Retry-After as seconds (often fractional). Anything unparseable or
    negative yields ``None`` -- we would rather skip the retry than guess a wait."""
    if not raw:
        return None
    try:
        seconds = float(raw)
    except ValueError:
        return None
    return seconds if seconds >= 0 else None


async def build_notifier(
    session: AsyncSession,
    box: SecretBox,
    settings: Settings,
    *,
    client: httpx2.AsyncClient | None = None,
) -> DiscordNotifier | None:
    """A notifier if a webhook is configured, else ``None`` (notifications off).

    The webhook lives in the database (Fernet-encrypted, edited in the web UI), so it is
    read through ``app_settings`` from a live session rather than off ``Settings`` --
    ``REAPER_DISCORD_WEBHOOK`` is only a first-boot seed. ``client`` is injectable so tests
    can drive it with pytest-httpx2 without a real network.
    """
    webhook = await app_settings.get_discord_webhook(session, box, settings)
    if webhook is None:
        return None
    return DiscordNotifier(
        webhook,
        client=client,
        app_name=await app_settings.get_application_name(session),
        app_url=await app_settings.get_application_url(session),
    )
