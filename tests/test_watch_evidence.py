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

from collections.abc import AsyncIterator
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine as sa_create_engine
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

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
from tests._auth import TEST_PASSWORD, clear_admin_password

# Whole seconds. ``UtcTimestamp`` stores epoch seconds (``db.types.EpochDateTime``), so a
# microsecond here would survive in memory and not in the row, and every round-trip
# assertion below would fail on the truncation rather than on the behavior. Production never
# sees it: both sides of the comparison come from epoch seconds already.
NOW = utcnow().replace(microsecond=0)
EARLIER = NOW - timedelta(days=30)


@pytest.fixture
async def session(tmp_path: Path) -> AsyncIterator[AsyncSession]:
    settings = Settings(data_dir=tmp_path, secret_key="test-key")
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
        # A fully blind reading carries no evidence, so ``record`` skips it outright; the two
        # tests below cover the partial case, where a row IS written and the SQL max() is the
        # only thing holding the mark up.
        await watch_evidence.record(session, {"radarr:1:5": Reading(4, NOW)}, now=NOW)
        await watch_evidence.record(session, {"radarr:1:5": Reading(0, None)}, now=NOW)
        marks = await watch_evidence.recall_all(session)
        assert marks["radarr:1:5"] == Mark(watchers_all_time=4, last_played_at=NOW)

    async def test_a_lower_but_still_readable_count_cannot_lower_the_mark(
        self, session: AsyncSession
    ) -> None:
        # The partially blind item: some plays still visible under the current key, so the
        # reading carries evidence and IS written. Nothing skips this row, which makes the
        # SQL ``max()`` on the counter the only thing standing between a partial fall and a
        # mark that quietly follows it down (rule 118).
        await watch_evidence.record(session, {"radarr:1:5": Reading(5, NOW)}, now=NOW)
        await watch_evidence.record(session, {"radarr:1:5": Reading(2, NOW)}, now=NOW)
        marks = await watch_evidence.recall_all(session)
        assert marks["radarr:1:5"].watchers_all_time == 5

    async def test_an_earlier_last_play_cannot_lower_the_mark(self, session: AsyncSession) -> None:
        # The same partial case on the timestamp arm: the most recent play was recorded under
        # a key that moved, so the latest one still visible is older. The mark must keep the
        # later instant, or the next scan compares against the fallen value and reads honest.
        await watch_evidence.record(session, {"radarr:1:5": Reading(4, NOW)}, now=NOW)
        await watch_evidence.record(session, {"radarr:1:5": Reading(4, EARLIER)}, now=NOW)
        marks = await watch_evidence.recall_all(session)
        assert marks["radarr:1:5"].last_played_at == NOW

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


class TestAReadingOfNothingIsNotAMark:
    """No watchers and no play is the ABSENCE of evidence, not a measurement of none.

    The two are the same thing only for an item whose history was readable, and the read path
    cannot tell which it has. So no row is written, and an absent mark keeps meaning exactly
    what it says. This is deliberately behavior-neutral -- a stored zero and no row both make
    ``went_blind`` return ``None`` -- and the tests below pin both halves of that: the row
    does not appear, and nothing about what fires moves.
    """

    def test_a_reading_of_nothing_carries_no_evidence(self) -> None:
        assert not watch_evidence.carries_evidence(Reading(0, None))

    def test_a_play_with_no_counted_watcher_still_carries_evidence(self) -> None:
        # The arms are independent on purpose: a play Reaper can see is evidence even where
        # the watcher count did not survive, and dropping it would forfeit the timestamp arm
        # of the check for that item.
        assert watch_evidence.carries_evidence(Reading(0, EARLIER))

    def test_a_counted_watcher_with_no_play_still_carries_evidence(self) -> None:
        assert watch_evidence.carries_evidence(Reading(3, None))

    async def test_no_row_is_written_for_a_reading_of_nothing(self, session: AsyncSession) -> None:
        await watch_evidence.record(session, {"radarr:1:5": Reading(0, None)}, now=NOW)
        assert await watch_evidence.recall_all(session) == {}

    async def test_the_watched_item_beside_it_is_still_recorded(
        self, session: AsyncSession
    ) -> None:
        # The skip is per row, not per call: one unwatched item in a chunk must not cost the
        # rest of the chunk its marks.
        await watch_evidence.record(
            session,
            {"radarr:1:5": Reading(0, None), "radarr:1:6": Reading(2, NOW)},
            now=NOW,
        )
        assert set(await watch_evidence.recall_all(session)) == {"radarr:1:6"}

    async def test_a_later_real_play_still_starts_the_mark(self, session: AsyncSession) -> None:
        # The item read nothing for as many scans as you like, then was watched. Skipping the
        # empty readings must not stop the first real one landing.
        await watch_evidence.record(session, {"radarr:1:5": Reading(0, None)}, now=NOW)
        await watch_evidence.record(session, {"radarr:1:5": Reading(1, NOW)}, now=NOW)
        marks = await watch_evidence.recall_all(session)
        assert marks["radarr:1:5"] == Mark(watchers_all_time=1, last_played_at=NOW)

    def test_an_absent_mark_decides_exactly_what_a_stored_zero_did(self) -> None:
        # Why this is safe to change: the two states were already indistinguishable to the
        # only function that reads them, so removing the row moves no decision. A never-watched
        # item stays condemnable either way, which is what keeps the check usable library-wide.
        assert went_blind(None, Reading(0, None)) is None
        assert went_blind(Mark(0, None), Reading(0, None)) is None


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

    async def test_forget_one_takes_that_title_and_leaves_the_rest(
        self, session: AsyncSession
    ) -> None:
        # Before #275, clearing one stale mark meant `forget_all`, which discards every
        # real mark protecting every other title. So the assertion that matters is the
        # SECOND one -- what survives, not what went.
        await watch_evidence.record(
            session,
            {"radarr:1:5": Reading(4, NOW), "sonarr:1:9:2": Reading(2, NOW)},
            now=NOW,
        )
        assert await watch_evidence.forget_one(session, "radarr:1:5") is True
        marks = await watch_evidence.recall_all(session)
        assert set(marks) == {"sonarr:1:9:2"}
        assert marks["sonarr:1:9:2"].watchers_all_time == 2

    async def test_forgetting_a_title_with_no_mark_is_not_an_error(
        self, session: AsyncSession
    ) -> None:
        # A title can be held for reasons that are not a mark, and an operator can press this
        # twice. Both must report "there was nothing" rather than failing.
        assert await watch_evidence.forget_one(session, "radarr:1:404") is False

    async def test_a_forgotten_title_reads_honestly_again(self, session: AsyncSession) -> None:
        # The behavior the operator is buying, driven end to end through the real check
        # rather than asserted off the row: the mark is what makes `went_blind` fire, so
        # after the escape the same falling reading must come back clean (rule 118).
        await watch_evidence.record(session, {"radarr:1:5": Reading(4, NOW)}, now=NOW)
        fallen = Reading(0, None)
        marks = await watch_evidence.recall_all(session)
        assert (
            watch_evidence.went_blind(marks.get("radarr:1:5"), fallen)
            == watch_evidence.BLIND_REASON
        )

        await watch_evidence.forget_one(session, "radarr:1:5")
        marks = await watch_evidence.recall_all(session)
        assert watch_evidence.went_blind(marks.get("radarr:1:5"), fallen) is None


# ---------------------------------------------------------------------------
# What the two fact builders do with the flag. Both lanes, deliberately in one place:
# rule 72 wants the season path treated identically to the movie path, and a reader
# checking that should not have to visit two files to see it.
# ---------------------------------------------------------------------------

_EMPTY_INDEX = lists.MembershipIndex({}, {}, {}, {})
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


def _seed_marks(client: TestClient, *keys: str) -> None:
    """Rows in ``watch_high_water``, by raw SQL like the snapshot helper below."""
    engine = sa_create_engine(client.app.state.settings.sync_database_url)  # type: ignore[attr-defined]
    with engine.begin() as conn:
        for key in keys:
            conn.execute(
                text(
                    "INSERT INTO watch_high_water "
                    "(media_key, watchers_all_time, last_played_at, updated_at) "
                    "VALUES (:k, 3, NULL, 1)"
                ),
                {"k": key},
            )
    engine.dispose()


def _marks_held(client: TestClient) -> int:
    """How many marks survive, read straight out of the table rather than off the route.

    The route's own count is what these tests are judging, so believing it about whether the
    delete happened would be circular: a refusal that returned ``{"forgotten": 0}`` while
    emptying the table reads identically from the response body.
    """
    engine = sa_create_engine(client.app.state.settings.sync_database_url)  # type: ignore[attr-defined]
    with engine.begin() as conn:
        held = conn.execute(text("SELECT count(*) FROM watch_high_water")).scalar_one()
    engine.dispose()
    return int(held)


class TestTheStartFreshRoute:
    """The escape hatch. Rebuild a library without repairing its history and every watched
    title reads zero at once, so every one is held back and nothing is reapable. Correct, and
    unusable, so the operator can discard the record.

    It is gated on the admin password, like arming deletion and confirming a restore, because
    the record is the only thing separating "plays we can no longer read" from "nobody ever
    watched this" -- so discarding it withdraws that protection from every title at once, and
    the next scan scores those titles as never watched. Same gate, same lockout, same refusal
    with no password set (rule 72).
    """

    def test_it_forgets_every_mark_and_says_how_many(self, client: TestClient) -> None:
        _seed_marks(client, "radarr:1:5", "radarr:1:6")

        body = client.post(
            "/api/settings/watch-evidence/reset", json={"password": TEST_PASSWORD}
        ).json()
        assert body == {"forgotten": 2}
        # Idempotent: a second press has nothing left to discard and says so rather than
        # failing, so a double click cannot read as an error.
        assert client.post(
            "/api/settings/watch-evidence/reset", json={"password": TEST_PASSWORD}
        ).json() == {"forgotten": 0}

    def test_a_wrong_or_missing_password_keeps_every_mark(self, client: TestClient) -> None:
        """Both spellings of "did not confirm" are refused, and the marks are still there.

        The omitted case is not a validator's 422: the field is optional on the wire precisely
        so that an empty submit gets the same plain sentence a wrong password gets.
        """
        _seed_marks(client, "radarr:1:5", "radarr:1:6")

        for payload in ({"password": "not-the-admin-password"}, {}):
            refused = client.post("/api/settings/watch-evidence/reset", json=payload)
            assert refused.status_code == 403, refused.text
            assert refused.json()["detail"] == "That password didn't match. The record was kept."
            assert _marks_held(client) == 2

        # And the right one still works afterwards: a refusal locks nothing but the attempt.
        ok = client.post("/api/settings/watch-evidence/reset", json={"password": TEST_PASSWORD})
        assert ok.status_code == 200, ok.text
        assert _marks_held(client) == 0

    def test_with_no_admin_password_set_it_points_at_the_password_step(
        self, client: TestClient
    ) -> None:
        """Not a 403: there is nothing to type, so "that didn't match" would send the operator
        to guess at a password that does not exist. Same sentence shape as arming deletion and
        confirming a restore, naming this action (rule 72)."""
        _seed_marks(client, "radarr:1:5")
        clear_admin_password(client)

        refused = client.post("/api/settings/watch-evidence/reset", json={"password": ""})
        assert refused.status_code == 400, refused.text
        assert refused.json()["detail"] == (
            "Set an admin password first. It's what confirms forgetting the record."
        )
        assert _marks_held(client) == 1

    def test_repeated_wrong_passwords_are_locked_out(self, client: TestClient) -> None:
        """This is a password-guessing surface like the other two, so past the threshold it
        stops hashing and answers 429 instead of running another Argon2 verify (rule 11/98)."""
        codes = [
            client.post(
                "/api/settings/watch-evidence/reset", json={"password": f"wrong-{n}"}
            ).status_code
            for n in range(6)
        ]
        assert codes == [403] * 5 + [429]

    def test_one_title_can_be_forgotten_without_touching_the_others(
        self, client: TestClient
    ) -> None:
        """The per-title escape over the wire (#275), and the blast radius is the assertion.

        The global reset above is what this exists to avoid: an operator clearing one stale
        record should not lose the records holding every other title back.
        """
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

        assert client.delete("/api/settings/watch-evidence/radarr:1:5").json() == {"removed": True}
        # The other title's record is untouched, which is the whole difference from the reset.
        assert client.get("/api/settings/watch-evidence").json()["titles"] == 1
        # Idempotent, same as the reset: pressing it again reports nothing to remove.
        assert client.delete("/api/settings/watch-evidence/radarr:1:5").json() == {"removed": False}


def _insert_snapshot(client: TestClient, *, blind: int | None) -> None:
    """One snapshot row with a given ``watch_blind_items``. Raw SQL, like the class above.

    ``UtcTimestamp`` stores epoch seconds, so the two timestamps are integers here.
    """
    engine = sa_create_engine(client.app.state.settings.sync_database_url)  # type: ignore[attr-defined]
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO snapshot (created_at, policy_hash, scoring_hash, horizon_at, "
                "item_count, degraded, watch_blind_items) "
                "VALUES (1, 'h', 's', 1, 1, 0, :blind)"
            ),
            {"blind": blind},
        )
    engine.dispose()


class TestTheRouteThatSaysWhetherAnythingIsHeldBack:
    """The number that decides whether an operator presses the discard, so the three states
    are pinned separately. ``None`` is "no scan has counted", which is NOT zero: collapsing it
    would print an affirmative "nothing is being held back" about a scan that never looked
    (rule 93), on the one surface that talks the operator into discarding protection evidence.
    """

    def test_a_fresh_install_reports_nothing_recorded(self, client: TestClient) -> None:
        # No snapshot at all: the honest answer is null, and this is the shape a fresh
        # install actually returns, which is what the frontend fixture must state.
        assert client.get("/api/settings/watch-evidence").json() == {
            "titles": 0,
            "held_back": None,
        }

    def test_a_snapshot_predating_the_count_stays_unknown(self, client: TestClient) -> None:
        # An existing database upgraded into this feature: the column is NULL because that
        # scan could not have counted, not because it counted none.
        _insert_snapshot(client, blind=None)
        assert client.get("/api/settings/watch-evidence").json()["held_back"] is None

    def test_a_scan_that_counted_none_reports_zero(self, client: TestClient) -> None:
        # The other side of the same coin: zero is a real answer and must survive as zero,
        # because "none right now" is what tells the operator to leave the control alone.
        _insert_snapshot(client, blind=0)
        assert client.get("/api/settings/watch-evidence").json()["held_back"] == 0

    def test_it_answers_from_the_latest_scan_not_the_first(self, client: TestClient) -> None:
        # Non-zero behind, zero in front, so a query that read the wrong end of the table
        # would return 4 here and could not be told from a correct one by the rows above.
        _insert_snapshot(client, blind=4)
        _insert_snapshot(client, blind=0)
        assert client.get("/api/settings/watch-evidence").json()["held_back"] == 0

    def test_it_counts_the_titles_holding_a_record(self, client: TestClient) -> None:
        engine = sa_create_engine(client.app.state.settings.sync_database_url)  # type: ignore[attr-defined]
        with engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO watch_high_water "
                    "(media_key, watchers_all_time, last_played_at, updated_at) "
                    "VALUES ('radarr:1:5', 3, NULL, 1)"
                )
            )
        engine.dispose()
        assert client.get("/api/settings/watch-evidence").json()["titles"] == 1


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
        seen=None,
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
