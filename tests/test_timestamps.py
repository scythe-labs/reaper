# SPDX-License-Identifier: AGPL-3.0-or-later
"""Checks timestamp storage and conversion.

Timestamps are stored as an integer unix epoch. That choice makes a whole class of bugs
impossible to write, not just easy to catch.

SQLite has no date type and stores no timezone. ``DateTime(timezone=True)`` is silently a
no-op there. Aware datetimes go in, and naive ones come back out. Comparing a naive datetime
against an aware one raises a TypeError at best. At worst, if both happen to be naive, it
compares a UTC instant against a local one and is silently wrong by the UTC offset. In a
tool whose every decision rests on "when was this last watched", a silently wrong answer is
the failure mode that matters most.

An integer has no timezone to get wrong.
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
    settings = Settings(data_dir=tmp_path, secret_key="test-key")
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
    session.expunge_all()  # force SQLAlchemy to read the row back from SQLite
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

        # Precision is whole seconds, so the round-tripped instant must match within one second.
        assert abs((row.created_at - now).total_seconds()) < 1

    async def test_a_non_utc_datetime_is_normalized_not_shifted(
        self, session: AsyncSession
    ) -> None:
        """Converting a US/Eastern datetime must not change the instant it names."""
        eastern = timezone(timedelta(hours=-5))
        instant = datetime(2026, 7, 14, 12, 0, 0, tzinfo=eastern)

        row = await _store(session, instant, instant + timedelta(minutes=15))

        assert row.created_at == instant  # same moment
        assert row.created_at.hour == 17  # 12:00-05:00 == 17:00Z

    async def test_comparing_a_stored_timestamp_to_utcnow_works(
        self, session: AsyncSession
    ) -> None:
        """The whole product depends on this comparison: whether a timestamp is older
        than its floor."""
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


#: The instant every text spelling below names.
_INSTANT = datetime(2026, 8, 24, 14, 30, 56, tzinfo=UTC)


async def _read_back_with_created_at(session: AsyncSession, raw: str) -> RecoveryToken:
    """Store a row, overwrite ``created_at`` with a raw TEXT value, and read it back.

    Using raw SQL is deliberate. It bypasses the ORM type, which is the only way one of
    these columns can hold text at all.
    """
    now = utcnow()
    await _store(session, now, now + timedelta(minutes=15))
    await session.execute(text("UPDATE recovery_token SET created_at = :raw"), {"raw": raw})
    session.expunge_all()
    row = await session.scalar(select(RecoveryToken))
    assert row is not None
    return row


class TestATextValueInAnEpochColumn:
    """Nothing in Reaper writes a text value into this column. Only a hand ``sqlite3`` edit
    would. A bad decode here raises for every reader of the row, since ``session.get``
    decodes the whole row at once.
    """

    @pytest.mark.parametrize("raw", ["2026-08-24T14:30:56+00:00", "2026-08-24T10:30:56-04:00"])
    async def test_an_iso_spelling_that_names_one_instant_decodes(
        self, session: AsyncSession, raw: str
    ) -> None:
        row = await _read_back_with_created_at(session, raw)

        assert row.created_at == _INSTANT

    async def test_an_epoch_typed_as_text_is_an_integer_by_the_time_it_lands(
        self, session: AsyncSession
    ) -> None:
        """SQLite's INTEGER affinity converts a well-formed numeric literal on the way in, so
        the read side never sees an epoch stored as text. That is why the decode logic only
        needs to handle ISO strings. This test pins that affinity behavior, not the decode
        logic, so it passes even against a version of the read side without the ISO fix.
        """
        row = await _read_back_with_created_at(session, str(int(_INSTANT.timestamp())))

        assert row.created_at == _INSTANT

    @pytest.mark.parametrize("raw", ["2026-08-24T14:30:56", "yesterday", ""])
    async def test_a_spelling_that_names_no_instant_raises(
        self, session: AsyncSession, raw: str
    ) -> None:
        """This includes the naive spelling. Reading a bad value as ``None`` would be worse
        than raising, since ``NULL`` means "never played" on ``WatchHighWater.last_played_at``.
        Silently treating a bad value as "never played" would push that item toward deletion
        instead of raising a clear error.
        """
        with pytest.raises(ValueError, match="Unreadable timestamp"):
            await _read_back_with_created_at(session, raw)


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
        """Checks that a missing play date is never coerced into a real date.

        Tautulli and Plex use 0, and sometimes an empty string, for "never played". Coercing
        that naively into a date gives 1970-01-01, which the scoring engine would read as
        the most unwatched an item can possibly be, exactly the item it must protect.
        Treating it as unknown instead means it can only ever help keep the item.
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
