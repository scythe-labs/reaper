# SPDX-License-Identifier: AGPL-3.0-or-later
"""Regressions from the backend-core-B review pass.

Two clusters of behavior the review pinned:

* **Discord is DB-backed and unbreakable.** The webhook lives in the database,
  Fernet-encrypted like an instance API key, seeded once from the environment; and the
  notifier's guarantee -- *nothing here raises into a scan, a plan, or a run* -- must hold
  even for a malformed URL (``httpx2.InvalidURL`` is not an ``HTTPError``) and must honor a
  429 rather than dropping the warning.
* **The Leaving Soon heads-up is idempotent.** In the default read-only install the Plex
  label is never written, so the label diff cannot be the "what is new" signal -- a
  persisted announced-set is, or every repeated sync re-spams the same titles.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import timedelta
from pathlib import Path

import httpx
import pytest
import respx
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from reaper.clock import utcnow
from reaper.config import Settings
from reaper.crypto import SecretBox
from reaper.db.base import Base
from reaper.db.session import create_engine, create_session_factory
from reaper.notify.discord import DiscordNotifier, Embed, build_notifier
from reaper.services import app_settings
from reaper.services.grace import GraceItem, GraceReport
from reaper.services.leaving_soon import announce_new

WEBHOOK = "https://discord.com/api/webhooks/123/token-is-a-secret"


@pytest.fixture
async def factory(tmp_path: Path) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    settings = Settings(data_dir=tmp_path, secret_key="test-key")  # type: ignore[call-arg]
    engine = create_engine(settings)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield create_session_factory(engine)
    await engine.dispose()


def _box() -> SecretBox:
    return SecretBox("test-key")


def _settings(**overrides: object) -> Settings:
    return Settings(secret_key="test-key", **overrides)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# The webhook is DB-backed, encrypted, write-only, and seeded once from the env
# ---------------------------------------------------------------------------


class TestDiscordWebhookStorage:
    async def test_stored_webhook_roundtrips_and_is_not_plaintext(
        self, factory: async_sessionmaker[AsyncSession]
    ) -> None:
        box = _box()
        async with factory() as session:
            await app_settings.set_discord_webhook(session, box, WEBHOOK)
            await session.commit()

        async with factory() as session:
            # The raw stored value is a Fernet token, never the URL itself.
            raw = await app_settings._get(session, app_settings.DISCORD_WEBHOOK_KEY, default=None)
            assert raw is not None
            assert "token-is-a-secret" not in str(raw)
            assert await app_settings.get_discord_webhook(session, box, _settings()) == WEBHOOK
            assert await app_settings.has_discord_webhook(session, box) is True

    async def test_clear_turns_notifications_off(
        self, factory: async_sessionmaker[AsyncSession]
    ) -> None:
        box = _box()
        async with factory() as session:
            await app_settings.set_discord_webhook(session, box, WEBHOOK)
            await app_settings.clear_discord_webhook(session)
            await session.commit()
        async with factory() as session:
            assert await app_settings.has_discord_webhook(session, box) is False
            assert await app_settings.get_discord_webhook(session, box, _settings()) is None

    async def test_env_webhook_is_seeded_into_the_db_once(
        self, factory: async_sessionmaker[AsyncSession]
    ) -> None:
        """REAPER_DISCORD_WEBHOOK is a first-boot seed: read once, it lands in the DB so the
        database is the source of truth thereafter."""
        box = _box()
        seeded = _settings(discord_webhook=WEBHOOK)
        async with factory() as session:
            assert await app_settings.get_discord_webhook(session, box, seeded) == WEBHOOK
            await session.commit()

        # With nothing in the environment now, the seeded value still resolves from the DB.
        async with factory() as session:
            assert await app_settings.get_discord_webhook(session, box, _settings()) == WEBHOOK

    async def test_has_webhook_counts_an_unseeded_env_value(
        self, factory: async_sessionmaker[AsyncSession]
    ) -> None:
        async with factory() as session:
            assert (
                await app_settings.has_discord_webhook(
                    session, _box(), _settings(discord_webhook=WEBHOOK)
                )
                is True
            )

    async def test_an_undecryptable_stored_webhook_reads_as_absent(
        self, factory: async_sessionmaker[AsyncSession]
    ) -> None:
        """A stored webhook that will not decrypt (the secret key changed) must never raise
        into a scan -- it is treated as absent and can be re-entered."""
        async with factory() as session:
            await app_settings.set_discord_webhook(session, _box(), WEBHOOK)
            await session.commit()
        other_key = SecretBox("a-different-key")
        async with factory() as session:
            assert await app_settings.get_discord_webhook(session, other_key, _settings()) is None

    async def test_an_undecryptable_stored_webhook_reads_as_not_configured(
        self, factory: async_sessionmaker[AsyncSession]
    ) -> None:
        """The has-check must agree with the send path: a stored webhook that no longer
        decrypts is skipped by every send, so the UI must not report it as configured."""
        async with factory() as session:
            await app_settings.set_discord_webhook(session, _box(), WEBHOOK)
            await session.commit()
        other_key = SecretBox("a-different-key")
        async with factory() as session:
            assert await app_settings.has_discord_webhook(session, other_key) is False


class TestBuildNotifier:
    async def test_no_webhook_anywhere_means_no_notifier(
        self, factory: async_sessionmaker[AsyncSession]
    ) -> None:
        async with factory() as session:
            assert await build_notifier(session, _box(), _settings()) is None

    async def test_a_stored_webhook_yields_a_notifier(
        self, factory: async_sessionmaker[AsyncSession]
    ) -> None:
        box = _box()
        async with factory() as session:
            await app_settings.set_discord_webhook(session, box, WEBHOOK)
            await session.commit()
        async with factory() as session:
            assert await build_notifier(session, box, _settings()) is not None


# ---------------------------------------------------------------------------
# The notifier cannot break a run: not on a malformed URL, not on a 429
# ---------------------------------------------------------------------------


class TestNotifierNeverRaises:
    async def test_a_malformed_url_returns_false_and_does_not_raise(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """An embedded newline makes httpx2 raise InvalidURL, which is NOT an HTTPError. It
        must still be swallowed, and the URL must not be logged."""
        notifier = DiscordNotifier("https://discord.com/api/webhooks/1/tok\nen-is-secret")
        assert await notifier.post(Embed(title="hi", description="x")) is False
        out = capsys.readouterr()
        assert "en-is-secret" not in (out.out + out.err)

    async def test_a_429_with_retry_after_is_retried_and_can_succeed(
        self, httpx2_mock: respx.Router
    ) -> None:
        # discord.py is on httpx2; its mock is the httpx2_mock Router (respx cannot see an
        # httpx2 client). The mocked responses stay httpx.Response -- respx's own currency,
        # which the plugin hands to the httpx2 client.
        route = httpx2_mock.post(WEBHOOK).mock(
            side_effect=[
                httpx.Response(429, headers={"Retry-After": "0"}),
                httpx.Response(204),
            ]
        )
        notifier = DiscordNotifier(WEBHOOK)
        assert await notifier.post(Embed(title="hi", description="x")) is True
        assert route.call_count == 2  # first 429, then the honored retry

    async def test_a_429_without_retry_after_is_a_quiet_failure(
        self, httpx2_mock: respx.Router
    ) -> None:
        route = httpx2_mock.post(WEBHOOK).mock(return_value=httpx.Response(429))
        notifier = DiscordNotifier(WEBHOOK)
        # Nothing useful to wait on -> fall through to the ordinary rejected path, no retry.
        assert await notifier.post(Embed(title="hi", description="x")) is False
        assert route.call_count == 1


# ---------------------------------------------------------------------------
# Leaving Soon: the announce is idempotent even when the label is never written
# ---------------------------------------------------------------------------

NOW = utcnow()


def _item(rating_key: int, *, title: str = "Film") -> GraceItem:
    return GraceItem(
        media_key=f"radarr:1:{rating_key}",
        candidate_id=rating_key,
        plex_rating_key=rating_key,
        title=title,
        media_type="movie",
        size_bytes=1,
        first_flagged_at=NOW,
        grace_ends_at=NOW + timedelta(days=10),
        days_remaining=10,
        in_grace=True,
    )


def _report(items: list[GraceItem], *, grace_days: int = 14) -> GraceReport:
    return GraceReport(
        grace_days=grace_days,
        in_grace=items,
        ready=[],
        total_bytes_in_grace=sum(i.size_bytes for i in items),
        total_bytes_ready=0,
    )


class _CountingNotifier:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    async def announce_leaving_soon(self, titles: list[str], *, grace_days: int) -> bool:
        self.calls.append(titles)
        return True


class TestIdempotentAnnounce:
    """The announce is driven by the grace set and the persisted announced-set alone --
    never by the Plex mark diff, which in the default read-only install never shrinks
    (nothing is written), so it would re-spam the same titles on every pass."""

    async def test_repeated_passes_announce_each_title_once(self) -> None:
        report = _report([_item(1, title="A"), _item(2, title="B")])
        notifier = _CountingNotifier()

        _notified, announced = await announce_new(
            notifier,  # type: ignore[arg-type]
            report,
            already_announced=set(),
        )
        assert sorted(notifier.calls[0]) == ["A", "B"]

        # Second pass, same grace set, nothing written to Plex in between. The previously
        # announced set is fed back in -- there must be no second announce.
        notified, announced = await announce_new(
            notifier,  # type: ignore[arg-type]
            report,
            already_announced=set(announced),
        )
        assert len(notifier.calls) == 1  # still just the one announce
        assert notified is False
        assert set(announced) == {1, 2}

    async def test_a_title_that_leaves_grace_is_announced_afresh_if_it_returns(self) -> None:
        notifier = _CountingNotifier()
        # Announced while in grace.
        _n1, a1 = await announce_new(
            notifier,  # type: ignore[arg-type]
            _report([_item(1, title="A")]),
            already_announced=set(),
        )
        assert set(a1) == {1}

        # It leaves grace (watched/spared): the announced set is pruned so it does not linger.
        _n2, a2 = await announce_new(
            notifier,  # type: ignore[arg-type]
            _report([]),
            already_announced=set(a1),
        )
        assert set(a2) == set()

        # It returns to grace later -> announced again, because the record was pruned.
        _n3, a3 = await announce_new(
            notifier,  # type: ignore[arg-type]
            _report([_item(1, title="A")]),
            already_announced=set(a2),
        )
        assert list(notifier.calls) == [["A"], ["A"]]  # two announces, once each spell
        assert set(a3) == {1}

    async def test_a_failed_announce_is_not_recorded_so_it_retries(self) -> None:
        class _FailingNotifier:
            async def announce_leaving_soon(self, titles: list[str], *, grace_days: int) -> bool:
                return False

        notified, announced = await announce_new(
            _FailingNotifier(),  # type: ignore[arg-type]
            _report([_item(1, title="A")]),
            already_announced=set(),
        )
        assert notified is False
        # Not marked announced: a later pass will try again rather than swallow the warning.
        assert set(announced) == set()
