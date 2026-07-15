# SPDX-License-Identifier: AGPL-3.0-or-later
"""Timestamp storage and conversion.

Timestamps are stored as integer unix epoch. That is a deliberate choice to make
a bug class *unrepresentable* rather than merely rejected.

SQLite has no date type and stores no timezone, so ``DateTime(timezone=True)`` is
silently a no-op there: aware datetimes go in and naive ones come back. Comparing
naive against aware raises TypeError at best -- and at worst, if both happen to be
naive, compares a UTC instant against a local one and is quietly wrong by the UTC
offset. In a tool whose every decision rests on "when was this last watched",
quietly wrong is the failure mode that matters.

An integer cannot carry that ambiguity. There is no tzinfo to lose.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import StatementError
from sqlalchemy.ext.asyncio import AsyncSession

from reaper.clock import days_since, from_epoch, to_epoch, utcnow
from reaper.config import Settings
from reaper.db.base import Base
from reaper.db.models import RecoveryToken
from reaper.db.session import create_engine, create_session_factory


@pytest.fixture
async def session(tmp_path: Path) -> AsyncIterator[AsyncSession]:
    settings = Settings(data_dir=tmp_path, secret_key="test-key")  # type: ignore[call-arg]
    engine = create_engine(settings)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = create_session_factory(engine)
    async with factory() as s:
        yield s
    await engine.dispose()


async def _store(session: AsyncSession, created: datetime, expires: datetime) -> RecoveryToken:
    session.add(RecoveryToken(token_hash="h" * 64, created_at=created, expires_at=expires))
    await session.flush()
    session.expunge_all()  # force a real read back from SQLite, not the identity map
    row = await session.scalar(select(RecoveryToken).where(RecoveryToken.token_hash == "h" * 64))
    assert row is not None
    return row


class TestStorage:
    async def test_stored_as_an_integer(self, session: AsyncSession) -> None:
        """The column really is an INTEGER, so no dialect can reinterpret it."""
        now = utcnow()
        await _store(session, now, now + timedelta(minutes=15))

        raw = (
            await session.execute(text("SELECT typeof(created_at), created_at FROM recovery_token"))
        ).one()
        assert raw[0] == "integer"
        assert raw[1] == int(now.timestamp())

    async def test_read_back_timezone_aware_utc(self, session: AsyncSession) -> None:
        now = utcnow()
        row = await _store(session, now, now + timedelta(minutes=15))

        assert row.created_at.tzinfo is not None
        assert row.created_at.utcoffset() == timedelta(0)

    async def test_the_instant_survives_the_round_trip(self, session: AsyncSession) -> None:
        now = utcnow()
        row = await _store(session, now, now + timedelta(minutes=15))

        # Whole-second precision; the instant must not move.
        assert abs((row.created_at - now).total_seconds()) < 1

    async def test_a_non_utc_datetime_is_normalised_not_shifted(
        self, session: AsyncSession
    ) -> None:
        """A caller handing us US/Eastern must not move the underlying instant."""
        eastern = timezone(timedelta(hours=-5))
        instant = datetime(2026, 7, 14, 12, 0, 0, tzinfo=eastern)

        row = await _store(session, instant, instant + timedelta(minutes=15))

        assert row.created_at == instant  # same moment
        assert row.created_at.hour == 17  # 12:00-05:00 == 17:00Z

    async def test_comparing_a_stored_timestamp_to_utcnow_works(
        self, session: AsyncSession
    ) -> None:
        """The operation the whole product depends on: 'is this older than my floor?'"""
        past = utcnow() - timedelta(days=400)
        row = await _store(session, past, past + timedelta(minutes=15))

        assert round(days_since(row.created_at)) == 400

    async def test_ordering_works_in_sql(self, session: AsyncSession) -> None:
        """Integers sort as instants. (A TEXT date column would sort correctly only
        by luck of ISO-8601 formatting.)"""
        now = utcnow()
        for i, offset in enumerate([300, 100, 200]):
            session.add(
                RecoveryToken(
                    token_hash=f"{i}" * 64,
                    created_at=now - timedelta(days=offset),
                    expires_at=now,
                )
            )
        await session.flush()

        rows = (
            (await session.execute(select(RecoveryToken).order_by(RecoveryToken.created_at)))
            .scalars()
            .all()
        )
        assert [round(days_since(r.created_at)) for r in rows] == [300, 200, 100]

    async def test_storing_a_naive_datetime_is_refused(self, session: AsyncSession) -> None:
        """Fail loudly at the boundary rather than storing an ambiguous instant."""
        naive = datetime(2026, 7, 14, 12, 0, 0)  # noqa: DTZ001 -- deliberately naive

        session.add(RecoveryToken(token_hash="n" * 64, created_at=naive, expires_at=naive))
        with pytest.raises(StatementError, match="naive datetime"):
            await session.flush()


class TestEpochConversion:
    """The boundary with Tautulli and Plex, which both speak epoch ints."""

    def test_from_epoch_returns_aware_utc(self) -> None:
        dt = from_epoch(1_752_517_200)
        assert dt is not None
        assert dt.tzinfo is UTC

    def test_roundtrip(self) -> None:
        now = utcnow()
        restored = from_epoch(to_epoch(now))
        assert restored is not None
        assert abs((restored - now).total_seconds()) < 1

    @pytest.mark.parametrize("value", [0, "0", None, "", "not-a-number", -1])
    def test_absent_stays_absent(self, value: object) -> None:
        """The trap that would silently mis-delete.

        Tautulli and Plex use 0 (and sometimes "") for 'never played'. Coerced
        naively, that becomes 1970-01-01 -- which the scoring engine reads as
        *maximally stale*, the exact opposite of the truth, and precisely the
        media it must not touch. Unknown must remain unknown, so that it can only
        ever protect an item.
        """
        assert from_epoch(value) is None  # type: ignore[arg-type]

    def test_a_string_epoch_is_accepted(self) -> None:
        """Tautulli returns some timestamps as strings."""
        assert from_epoch("1752517200") == from_epoch(1_752_517_200)

    def test_to_epoch_refuses_naive(self) -> None:
        naive = datetime(2026, 7, 14, 12, 0, 0)  # noqa: DTZ001
        with pytest.raises(ValueError, match="naive datetime"):
            to_epoch(naive)


class TestClock:
    def test_utcnow_is_aware_and_utc(self) -> None:
        now = utcnow()
        assert now.tzinfo is UTC
        assert now.utcoffset() == timedelta(0)

    def test_days_since(self) -> None:
        now = utcnow()
        assert round(days_since(now - timedelta(days=612), now=now)) == 612
