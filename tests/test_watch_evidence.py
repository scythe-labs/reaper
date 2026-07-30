# SPDX-License-Identifier: AGPL-3.0-or-later
"""The watch-history blindness check.

A Plex rating key is not stable: a re-added file gets a new one, while Tautulli keeps every
earlier play filed under the old one. The mirror is read by the current key, so those plays
vanish and the item reads ``Known(0)`` watchers with maximum dormancy -- an affirmative
"nobody ever watched this" about a title somebody watched.

These tests pin the invariant that separates that from a genuinely unwatched item (all-time
watch evidence cannot fall), the two branches that detect a fall, the cases that must NOT
fire (a never-watched item above all, or the whole library stops being reapable), and the
monotonic write -- including the SQLite trap where ``max()`` of anything and NULL is NULL.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine as sa_create_engine
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from reaper.main import create_app
from tests._auth import login

from reaper.clients.sonarr_stats import SeasonStats
from reaper.clock import utcnow
from reaper.config import Settings
from reaper.db.base import Base
from reaper.db.models import WatchHighWater
from reaper.db.session import create_engine, create_session_factory
from reaper.engine.gates import Facts
from reaper.engine.observation import Known, Unknown
from reaper.services import lists, season_scan, watch_evidence
from reaper.services.snapshot import RawItem, ScanContext, build_facts
from reaper.services.watch_evidence import Mark, Reading, went_blind

# Whole seconds. ``UtcTimestamp`` stores epoch seconds (``db.types.EpochDateTime``), so a
# microsecond here would survive in memory and not in the row, and every round-trip
# assertion below would fail on the truncation rather than on the behavior. Production never
# sees it: both sides of the comparison come from epoch seconds already.
NOW = utcnow().replace(microsecond=0)
EARLIER = NOW - timedelta(days=30)


@pytest.fixture
def client(tmp_path: Path) -> Iterator[TestClient]:
    """A logged-in client over an empty database, exactly a fresh install."""
    settings = Settings(data_dir=tmp_path, secret_key="k")  # type: ignore[call-arg]
    engine = sa_create_engine(settings.sync_database_url)
    Base.metadata.create_all(engine)
    engine.dispose()
    with TestClient(create_app(settings)) as c:
        login(c, settings)
        yield c


@pytest.fixture
async def session(tmp_path: Path) -> AsyncIterator[AsyncSession]:
    settings = Settings(data_dir=tmp_path, secret_key="test-key")  # type: ignore[call-arg]
    engine = create_engine(settings)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory: async_sessionmaker[AsyncSession] = create_session_factory(engine)
    async with factory() as s:
        yield s
    await engine.dispose()


class TestWhatDoesNotFire:
    """The false positives that would cost the operator their whole reap list."""

    def test_a_never_watched_item_never_fires(self) -> None:
        # THE case. Zero to zero is not a fall, so the item keeps its honest Known(0) and
        # stays condemnable -- which is the entire point of the feature. If this ever
        # inverts, nothing in the library is reapable again.
        mark = Mark(watchers_all_time=0, last_played_at=None)
        assert went_blind(mark, Reading(watchers_all_time=0, last_played_at=None)) is None

    def test_an_item_with_no_mark_yet_never_fires(self) -> None:
        # Every item on the first scan after this ships, and every genuinely new item.
        assert went_blind(None, Reading(watchers_all_time=0, last_played_at=None)) is None

    def test_an_unchanged_count_does_not_fire(self) -> None:
        mark = Mark(watchers_all_time=4, last_played_at=EARLIER)
        assert went_blind(mark, Reading(watchers_all_time=4, last_played_at=EARLIER)) is None

    def test_a_growing_count_does_not_fire(self) -> None:
        mark = Mark(watchers_all_time=4, last_played_at=EARLIER)
        assert went_blind(mark, Reading(watchers_all_time=5, last_played_at=NOW)) is None

    def test_a_partial_drop_that_keeps_the_latest_play_does_not_fire(self) -> None:
        # Deliberately not flagged: it does not cross the never-watched boundary that drives
        # the condemn lane, and dormancy is unaffected because the latest play still stands.
        mark = Mark(watchers_all_time=5, last_played_at=EARLIER)
        assert went_blind(mark, Reading(watchers_all_time=3, last_played_at=EARLIER)) is None


class TestWhatFires:
    def test_a_watched_item_reading_zero_fires(self) -> None:
        mark = Mark(watchers_all_time=4, last_played_at=EARLIER)
        reason = went_blind(mark, Reading(watchers_all_time=0, last_played_at=None))
        assert reason == watch_evidence.BLIND_REASON

    def test_the_latest_play_moving_backwards_in_time_fires(self) -> None:
        # A play cannot un-happen. Catches the partial case the counter misses: some plays
        # still readable under the current key, but the most recent one was under a key that
        # moved, so dormancy inflates while the count stays positive.
        mark = Mark(watchers_all_time=5, last_played_at=NOW)
        reason = went_blind(mark, Reading(watchers_all_time=4, last_played_at=EARLIER))
        assert reason == watch_evidence.BLIND_REASON

    def test_the_latest_play_disappearing_fires(self) -> None:
        mark = Mark(watchers_all_time=2, last_played_at=NOW)
        reason = went_blind(mark, Reading(watchers_all_time=2, last_played_at=None))
        assert reason == watch_evidence.BLIND_REASON

    def test_the_reason_names_no_internal_machinery(self) -> None:
        # Rule 21: operator copy says the outcome and keeps rating keys, guids and mirrors
        # out of it. No em dashes either.
        reason = watch_evidence.BLIND_REASON
        assert "—" not in reason
        for jargon in ("rating key", "guid", "mirror", "Tautulli", "Plex", "watch_event"):
            assert jargon not in reason


class TestTheReadingIsOnlyTakenForMatchedItems:
    def test_an_unmatched_item_has_no_reading(self) -> None:
        # It must not be recorded as zero: that mark would hold the item's true evidence
        # down forever, so the check could never fire for it again.
        assert watch_evidence.reading_for(None, {}, {}) is None

    def test_a_matched_item_reads_its_own_key(self) -> None:
        reading = watch_evidence.reading_for(77, {77: 3, 88: 9}, {77: EARLIER})
        assert reading == Reading(watchers_all_time=3, last_played_at=EARLIER)

    def test_a_matched_item_with_no_plays_reads_zero(self) -> None:
        reading = watch_evidence.reading_for(77, {}, {})
        assert reading == Reading(watchers_all_time=0, last_played_at=None)


class TestTheMarkOnlyEverRises:
    async def test_a_first_reading_is_stored(self, session: AsyncSession) -> None:
        await watch_evidence.record(session, {"radarr:1:5": Reading(4, EARLIER)}, now=NOW)
        marks = await watch_evidence.recall_all(session)
        assert marks["radarr:1:5"] == Mark(watchers_all_time=4, last_played_at=EARLIER)

    async def test_a_blind_reading_cannot_lower_the_mark(self, session: AsyncSession) -> None:
        # The property the whole check rests on. If a blind scan could lower the mark, the
        # first one would write zero as the new baseline and no later scan would ever notice.
        await watch_evidence.record(session, {"radarr:1:5": Reading(4, NOW)}, now=NOW)
        await watch_evidence.record(session, {"radarr:1:5": Reading(0, None)}, now=NOW)
        marks = await watch_evidence.recall_all(session)
        assert marks["radarr:1:5"] == Mark(watchers_all_time=4, last_played_at=NOW)

    async def test_a_null_reading_does_not_erase_a_stored_last_played(
        self, session: AsyncSession
    ) -> None:
        # SQLite's scalar max() returns NULL if ANY argument is NULL, so an unguarded
        # max(excluded, stored) would wipe a real timestamp the first time an item read
        # blind -- exactly when the mark is needed.
        await watch_evidence.record(session, {"radarr:1:5": Reading(4, NOW)}, now=NOW)
        await watch_evidence.record(session, {"radarr:1:5": Reading(4, None)}, now=NOW)
        marks = await watch_evidence.recall_all(session)
        assert marks["radarr:1:5"].last_played_at == NOW

    async def test_a_later_play_raises_the_mark(self, session: AsyncSession) -> None:
        await watch_evidence.record(session, {"radarr:1:5": Reading(4, EARLIER)}, now=NOW)
        await watch_evidence.record(session, {"radarr:1:5": Reading(6, NOW)}, now=NOW)
        marks = await watch_evidence.recall_all(session)
        assert marks["radarr:1:5"] == Mark(watchers_all_time=6, last_played_at=NOW)

    async def test_recording_nothing_is_not_an_error(self, session: AsyncSession) -> None:
        await watch_evidence.record(session, {}, now=NOW)
        assert await watch_evidence.recall_all(session) == {}


class TestRecallAndForget:
    async def test_a_library_sized_write_survives_the_sqlite_variable_ceiling(
        self, session: AsyncSession
    ) -> None:
        # Rule 94: the WRITE binds four variables per row, so it is chunked. The read binds
        # none (it is the whole table), which is why it needs no chunking of its own.
        keys = [f"radarr:1:{n}" for n in range(1200)]
        await watch_evidence.record(session, {k: Reading(1, NOW) for k in keys}, now=NOW)
        marks = await watch_evidence.recall_all(session)
        assert len(marks) == 1200

    async def test_forget_all_clears_every_mark_and_counts_them(
        self, session: AsyncSession
    ) -> None:
        await watch_evidence.record(
            session,
            {"radarr:1:5": Reading(4, NOW), "sonarr:1:9:2": Reading(2, NOW)},
            now=NOW,
        )
        assert await watch_evidence.forget_all(session) == 2
        assert (await session.execute(select(WatchHighWater))).scalars().all() == []

    async def test_forgetting_an_empty_table_reports_zero(self, session: AsyncSession) -> None:
        assert await watch_evidence.forget_all(session) == 0


# ---------------------------------------------------------------------------
# What the two fact builders do with the flag. Both lanes, deliberately in one place:
# rule 72 wants the season path treated identically to the movie path, and a reader
# checking that should not have to visit two files to see it.
# ---------------------------------------------------------------------------

_EMPTY_INDEX = lists.MembershipIndex({}, {}, {})
# Recent on purpose. A re-added file carries a FRESH arrival date, so this is exactly the
# value that would let the builder measure a confident, tiny dormancy off the one input
# that still looks readable when the plays behind it are not.
JUST_ADDED = NOW - timedelta(days=3)


def _movie_facts(*, blind: str | None, added_at: datetime = JUST_ADDED) -> Facts:
    item = RawItem(
        media_key="radarr:1:1",
        title="A title",
        media_type="movie",
        size_bytes=8_000_000_000,
        imdb_id="tt0000001",
        tmdb_id=1,
        plex_rating_key=10,
        added_at=added_at,
        has_file=True,
    )
    return build_facts(
        item,
        ScanContext(horizon=NOW - timedelta(days=4000)),
        membership_index=_EMPTY_INDEX,
        imdb={},
        last_played={},
        watchers_window={10: 0},
        watchers_all_time={10: 0},
        whitelisted=set(),
        watch_blind_reason=blind,
    )


class TestTheStartFreshRoute:
    """The escape hatch. Rebuild a library without repairing its history and every watched
    title reads zero at once, so every one is held back and nothing is reapable. Correct, and
    unusable, so the operator can discard the record."""

    def test_it_forgets_every_mark_and_says_how_many(self, client: TestClient) -> None:
        engine = sa_create_engine(client.app.state.settings.sync_database_url)  # type: ignore[attr-defined]
        with engine.begin() as conn:
            for n in (5, 6):
                conn.execute(
                    text(
                        "INSERT INTO watch_high_water "
                        "(media_key, watchers_all_time, last_played_at, updated_at) "
                        "VALUES (:k, 3, NULL, 1)"
                    ),
                    {"k": f"radarr:1:{n}"},
                )
        engine.dispose()

        body = client.post("/api/settings/watch-evidence/reset").json()
        assert body == {"forgotten": 2}
        # Idempotent: a second press has nothing left to discard and says so rather than
        # failing, so a double click cannot read as an error.
        assert client.post("/api/settings/watch-evidence/reset").json() == {"forgotten": 0}


class TestTheMovieLaneReportsUnknownNotZero:
    def test_a_flagged_movie_has_unknown_watchers(self) -> None:
        facts = _movie_facts(blind=watch_evidence.BLIND_REASON)
        assert isinstance(facts.distinct_watchers, Unknown)
        assert isinstance(facts.distinct_watchers_all_time, Unknown)
        assert facts.distinct_watchers_all_time.reason == watch_evidence.BLIND_REASON

    def test_a_flagged_movie_does_not_measure_dormancy_off_a_fresh_arrival(self) -> None:
        # The trap the ordering exists for: added_at is three days old and perfectly
        # readable, so the un-flagged builder returns a confident ~3 days dormant.
        assert isinstance(_movie_facts(blind=None).days_observed_unwatched, Known)
        assert isinstance(
            _movie_facts(blind=watch_evidence.BLIND_REASON).days_observed_unwatched, Unknown
        )

    def test_an_unflagged_movie_still_reports_its_measured_zero(self) -> None:
        # The never-watched case has to keep working, or nothing is reapable.
        facts = _movie_facts(blind=None)
        assert isinstance(facts.distinct_watchers_all_time, Known)
        assert facts.distinct_watchers_all_time.value == 0


def _season_facts(*, blind: str | None) -> Facts:
    return season_scan.build_season_facts(
        title="Show · Season 3",
        season=SeasonStats(
            season_number=3,
            monitored=False,
            episode_file_count=10,
            size_on_disk=8 * 1024**3,
            total_episode_count=10,
            wanted_episode_count=0,
        ),
        rank=2,
        plex_rating_key=700,
        season_added_at=JUST_ADDED,
        horizon=NOW - timedelta(days=4000),
        reach_days=4000,
        last_played=None,
        watchers_window=0,
        watchers_all_time=0,
        active_rating_keys=set(),
        activity_degraded=False,
        whitelisted=False,
        curated=[],
        rating_looked_up=True,
        watch_blind_reason=blind,
    )


class TestTheSeasonLaneMatchesTheMovieLane:
    """Rule 72: the season path reads its history by the season's own Plex key, so the same
    fall is detectable there and must land on the same three observations."""

    def test_a_flagged_season_has_unknown_watchers(self) -> None:
        facts = _season_facts(blind=watch_evidence.BLIND_REASON)
        assert isinstance(facts.distinct_watchers, Unknown)
        assert isinstance(facts.distinct_watchers_all_time, Unknown)
        assert facts.distinct_watchers_all_time.reason == watch_evidence.BLIND_REASON

    def test_a_flagged_season_does_not_measure_dormancy_off_a_fresh_arrival(self) -> None:
        assert isinstance(_season_facts(blind=None).days_observed_unwatched, Known)
        assert isinstance(
            _season_facts(blind=watch_evidence.BLIND_REASON).days_observed_unwatched, Unknown
        )

    def test_an_unflagged_season_still_reports_its_measured_zero(self) -> None:
        facts = _season_facts(blind=None)
        assert isinstance(facts.distinct_watchers_all_time, Known)
        assert facts.distinct_watchers_all_time.value == 0
