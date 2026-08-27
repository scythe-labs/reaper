# SPDX-License-Identifier: AGPL-3.0-or-later
"""The two read-only surfaces: what Scales counts, and what the shelf announces.

Scales deletes nothing, so its findings are all the same shape. Each one is a number the
operator reads while deciding, and that number must mean what it says. A request for
seasons the scan does not hold must not inflate the board's request count or the
watch-rate denominator while appearing in no list an operator can open. One title reached
through two id groups must not charge the same person twice. Leaving Soon's half is the
announced set, read at the top of a pass and written at the bottom with minutes of network
I/O in between, and the Plex clients its read-only path builds must always be closed.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Sequence
from contextlib import suppress
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from reaper.clients.plex import PlexError
from reaper.clients.seerr import MediaRequest, Requester
from reaper.clock import utcnow
from reaper.config import RuntimeSafety, Settings
from reaper.crypto import SecretBox
from reaper.db.base import Base
from reaper.db.session import create_engine, create_session_factory
from reaper.engine.reason import Reason
from reaper.services import app_settings, leaving_soon
from reaper.services.fairness import (
    UNMATCHED_SET_ASIDE,
    CandidateInfo,
    WatchEvidence,
    roll_up,
)
from reaper.services.grace import GraceItem, GraceReport
from reaper.services.leaving_soon import LeavingSoonUnlinkedError

GB = 1024**3
NOW = utcnow()


# ---------------------------------------------------------------------------
# Scales: what a request is counted toward
# ---------------------------------------------------------------------------


def _req(
    *,
    plex_id: int,
    name: str,
    tmdb: int | None = 7,
    imdb: str | None = None,
    request_id: int = 1,
    media_type: str = "tv",
    seasons: tuple[int, ...] = (),
) -> MediaRequest:
    return MediaRequest(
        request_id=request_id,
        media_type=media_type,
        is_4k=False,
        requested_at=NOW - timedelta(days=500),
        requester=Requester(
            seerr_user_id=plex_id,
            plex_id=plex_id,
            username=name.lower(),
            display_name=name,
            email=None,
        ),
        tmdb_id=tmdb,
        tvdb_id=None,
        imdb_id=imdb,
        plex_rating_key=None,
        arr_id=1,
        arr_instance_id=0,
        available_at=NOW - timedelta(days=400),
        portal_key="",
        seasons=seasons,
    )


def _season(
    *, cid: int, number: int, rating_key: int, tmdb: int | None = 7, imdb: str | None = None
) -> CandidateInfo:
    return CandidateInfo(
        candidate_id=cid,
        plex_rating_key=rating_key,
        verdict="condemn",
        size_bytes=5 * GB,
        title=f"Season {number}",
        media_type="season",
        group_key="sonarr:1:series:1",
        group_title="A Show",
        tmdb_id=tmdb,
        imdb_id=imdb,
        tvdb_id=None,
        effective_condemn=True,
        season_number=number,
    )


class TestASeasonTheScanDoesNotHold:
    """A season-scoped request is the default shape Seerr sends, and a scan routinely holds
    only some of a show's seasons (the rest protected, or filtered out).

    The show itself still matched the scan, so a check that only looked at the show would
    call this request "in scan." But its specific seasons scoped to nothing the scan held,
    so the drawer skipped it. It counted toward totals, yet appeared on no list.
    """

    def test_it_is_not_counted_as_a_request_the_scan_has(self) -> None:
        report = roll_up(
            [_req(plex_id=100, name="A", seasons=(5,))],
            [_season(cid=1, number=1, rating_key=555)],
            {},
            snapshot_at=NOW,
        )
        assert report.rows == []
        assert report.not_in_scan == 1

    def test_it_lands_in_the_not_in_scan_panel_where_it_can_be_seen(self) -> None:
        """Simply skipping this request in the roll-up would make it vanish from every
        surface instead, which is a quieter version of the same problem."""
        report = roll_up(
            [_req(plex_id=100, name="A", seasons=(5,))],
            [_season(cid=1, number=1, rating_key=555)],
            {},
            snapshot_at=NOW,
        )
        (row,) = report.unmatched
        assert row.request_count == 1
        assert row.reason == UNMATCHED_SET_ASIDE
        assert row.requested_by == ["A"]

    def test_a_co_requester_who_asked_for_a_season_in_the_scan_is_untouched(self) -> None:
        """Requests are classified per request, not per group. One person's phantom season
        must not take their co-requester's real one off the board with it."""
        report = roll_up(
            [
                _req(plex_id=100, name="A", seasons=(1,), request_id=1),
                _req(plex_id=200, name="B", seasons=(5,), request_id=2),
            ],
            [_season(cid=1, number=1, rating_key=555)],
            {},
            snapshot_at=NOW,
        )
        assert [(r.name, r.requests_made) for r in report.rows] == [("A", 1)]
        assert report.not_in_scan == 1

    def test_the_watch_rate_denominator_only_counts_what_could_move_its_numerator(self) -> None:
        """The board divides ``played_by_them`` by ``requests_made``. A request that scopes
        to nothing can never increment the numerator, so it must never count toward the
        denominator either.

        The phantom request is listed first on purpose. Counting whichever request arrives
        first, rather than the one that actually matched, would attribute this person's
        watch to the wrong request and report their watch rate as 0%.
        """
        report = roll_up(
            [
                _req(plex_id=100, name="A", seasons=(5,), request_id=1),
                _req(plex_id=100, name="A", seasons=(1,), request_id=2),
            ],
            [_season(cid=1, number=1, rating_key=555)],
            {"555": WatchEvidence(plays_by_user={100: 4}, distinct_watchers=1)},
            snapshot_at=NOW,
        )
        (row,) = report.rows
        assert (row.played_by_them, row.requests_made) == (1, 1)

    def test_a_whole_show_request_still_binds_every_season(self) -> None:
        """The guard checks for an empty scope, not for season-scoping itself. An
        unscoped request still binds the whole matched set.
        """
        report = roll_up(
            [_req(plex_id=100, name="A", seasons=())],
            [_season(cid=1, number=1, rating_key=555), _season(cid=2, number=2, rating_key=556)],
            {},
            snapshot_at=NOW,
        )
        (row,) = report.rows
        assert (row.requests_made, row.gb_granted_bytes) == (1, 10 * GB)
        assert report.not_in_scan == 0


class TestOneTitleReachedTwoWays:
    """Requests group by a single content key (tmdb, else tvdb, else imdb), but candidates
    are indexed under every id they carry. Co-requests that carry different ids for the
    same title can split into two groups that resolve to the same candidates, so a person
    appearing in both groups must be charged only once. The report's totals dedupe by
    candidate set already, so the per-row counts must agree with that total.
    """

    def test_the_same_person_is_charged_once(self) -> None:
        cand = _season(cid=1, number=1, rating_key=555, tmdb=7, imdb="tt7")
        report = roll_up(
            [
                _req(plex_id=100, name="A", tmdb=7, imdb=None, request_id=1),
                _req(plex_id=100, name="A", tmdb=None, imdb="tt7", request_id=2),
            ],
            [cand],
            {},
            snapshot_at=NOW,
        )
        (row,) = report.rows
        assert row.requests_made == 1
        assert row.gb_granted_bytes == 5 * GB
        assert row.reclaimable_items == 1

    def test_the_row_still_agrees_with_the_report_total(self) -> None:
        """The report-level total is correct on its own. This pins the per-row count
        against it, so a fix for the row-level bug cannot break this side instead.
        """
        cand = _season(cid=1, number=1, rating_key=555, tmdb=7, imdb="tt7")
        report = roll_up(
            [
                _req(plex_id=100, name="A", tmdb=7, imdb=None, request_id=1),
                _req(plex_id=100, name="A", tmdb=None, imdb="tt7", request_id=2),
            ],
            [cand],
            {},
            snapshot_at=NOW,
        )
        assert report.total_reclaimable_items == 1
        assert report.rows[0].reclaimable_items == report.total_reclaimable_items

    def test_two_different_people_are_still_two_rows(self) -> None:
        """Deduping is by (person, matched set), never by matched set alone. Co-requesters
        of one title are the ordinary case, and both of them must be charged.
        """
        cand = _season(cid=1, number=1, rating_key=555, tmdb=7, imdb="tt7")
        report = roll_up(
            [
                _req(plex_id=100, name="A", tmdb=7, imdb=None, request_id=1),
                _req(plex_id=200, name="B", tmdb=None, imdb="tt7", request_id=2),
            ],
            [cand],
            {},
            snapshot_at=NOW,
        )
        assert sorted((r.name, r.requests_made) for r in report.rows) == [("A", 1), ("B", 1)]

    def test_two_genuinely_different_titles_both_count(self) -> None:
        """The dedup key is the matched candidate set, so it cannot collapse two titles."""
        report = roll_up(
            [
                _req(plex_id=100, name="A", tmdb=7, request_id=1),
                _req(plex_id=100, name="A", tmdb=8, request_id=2),
            ],
            [
                _season(cid=1, number=1, rating_key=555, tmdb=7),
                _season(cid=2, number=1, rating_key=556, tmdb=8),
            ],
            {},
            snapshot_at=NOW,
        )
        (row,) = report.rows
        assert (row.requests_made, row.gb_granted_bytes) == (2, 10 * GB)


# ---------------------------------------------------------------------------
# Leaving Soon: the announced set, and who closes the Plex client
# ---------------------------------------------------------------------------


@pytest.fixture
async def factory(tmp_path: Path) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    settings = Settings(data_dir=tmp_path, secret_key="test-key")
    engine = create_engine(settings)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield create_session_factory(engine)
    await engine.dispose()


def _settings(tmp_path: Path) -> Settings:
    return Settings(data_dir=tmp_path, secret_key="test-key")


def _grace_report(keys: Sequence[int]) -> GraceReport:
    items = [
        GraceItem(
            media_key=f"radarr:1:{k}",
            candidate_id=k,
            plex_rating_key=k,
            title=f"Title {k}",
            media_type="movie",
            size_bytes=1,
            first_flagged_at=NOW,
            grace_ends_at=NOW + timedelta(days=10),
            days_remaining=10,
            in_grace=True,
        )
        for k in keys
    ]
    return GraceReport(
        grace_days=14,
        in_grace=items,
        ready=[],
        total_bytes_in_grace=len(items),
        total_bytes_ready=0,
    )


class _RecordingNotifier:
    """Records each post, and the post itself awaits, so two passes can genuinely overlap
    during the window where the announced set is read, then later written.
    """

    def __init__(self, posts: list[tuple[str, ...]]) -> None:
        self._posts = posts

    async def announce_leaving_soon(self, titles: list[str], *, grace_days: int) -> bool:
        self._posts.append(tuple(sorted(titles)))
        await asyncio.sleep(0.02)
        return True


class _Rendezvous:
    """Holds the first pass at the announced-set read until a second pass reaches it.

    Without this, the race is left to the scheduler, which does not cooperate. Stubbed
    out, one pass reliably runs read-post-write to completion before the other gets a
    turn, so a test written the obvious way would pass with the lock removed and pin
    nothing.

    It is bounded, so it works both ways. With the lock in place, the second pass cannot
    reach the read, so the wait simply expires and the passes proceed serialized, which is
    the property under test, at the cost of one short timeout.
    """

    def __init__(self, real: Any, *, wait: float = 0.3) -> None:
        self._real = real
        self._wait = wait
        self._second_arrived = asyncio.Event()
        self.arrivals = 0

    async def __call__(self, session: AsyncSession) -> set[int]:
        self.arrivals += 1
        if self.arrivals == 1:
            with suppress(TimeoutError):
                async with asyncio.timeout(self._wait):
                    await self._second_arrived.wait()
        else:
            self._second_arrived.set()
        seen: set[int] = await self._real(session)
        return seen


class TestOverlappingPassesAnnounceOnce:
    """Two entry points can race with nothing serializing them: "Update now" from the Reap
    page, and the after-scan hook that fires at the end of every scan.

    Both read the same announced set, both decide the same title is new, and the
    operator's users get told twice. Then the later writer persists a set built from its
    own read from before either wrote, dropping what the first pass recorded, so the title
    is announced a third time on the next pass.
    """

    @pytest.fixture(autouse=True)
    def _stub_sources(self, monkeypatch: pytest.MonkeyPatch, posts: list[Any]) -> None:
        async def _notifier(*_args: object, **_kwargs: object) -> _RecordingNotifier:
            return _RecordingNotifier(posts)

        async def _report(*_args: object, **_kwargs: object) -> GraceReport:
            return _grace_report([11, 22])

        monkeypatch.setattr(leaving_soon, "build_notifier", _notifier)
        monkeypatch.setattr(leaving_soon, "grace_report", _report)

    @pytest.fixture
    def posts(self) -> list[tuple[str, ...]]:
        return []

    @pytest.fixture
    def overlap(self, monkeypatch: pytest.MonkeyPatch) -> _Rendezvous:
        meeting = _Rendezvous(app_settings.get_leaving_soon_announced)
        monkeypatch.setattr(app_settings, "get_leaving_soon_announced", meeting)
        return meeting

    async def _both(self, factory: async_sessionmaker[AsyncSession], tmp_path: Path) -> None:
        box = SecretBox("test-key")
        settings = _settings(tmp_path)
        await asyncio.gather(
            leaving_soon.after_scan(factory, settings, box),
            leaving_soon.after_scan(factory, settings, box),
        )

    async def test_the_users_are_told_once(
        self,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
        posts: list[tuple[str, ...]],
        overlap: _Rendezvous,
    ) -> None:
        """The shelf is off, so both passes fall through to the Discord-only path. This is
        not an edge case. For an operator running Leaving Soon without the shelf, it is the
        only path that ever announces.
        """
        await self._both(factory, tmp_path)
        assert posts == [("Title 11", "Title 22")]

    async def test_neither_pass_loses_the_other_s_record(
        self,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
        posts: list[tuple[str, ...]],
        overlap: _Rendezvous,
    ) -> None:
        """The durable half, and the reason the duplicate post above matters beyond one
        extra message. What is announced must end up recorded, or it is announced again
        next pass, and the pass after that.

        This is a companion invariant, not a second proof of the same thing. With both
        passes seeing the same grace set, whether the losing write drops anything depends
        on which one finishes last.
        """
        await self._both(factory, tmp_path)
        async with factory() as session:
            assert await overlap._real(session) == {11, 22}

    async def test_both_passes_really_did_reach_the_critical_read(
        self,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
        overlap: _Rendezvous,
    ) -> None:
        """A guard on the setup above, not a claim about the app's behavior. It proves the
        setup actually exercises what it says it does. If a future change made the second
        pass bail out before the announced-set read, the two assertions above would hold
        for the wrong reason.
        """
        await self._both(factory, tmp_path)
        assert overlap.arrivals == 2

    async def test_a_later_pass_announces_nothing_new(
        self,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
        posts: list[tuple[str, ...]],
    ) -> None:
        """Sequential, with no rendezvous. This is the plain idempotence the durable set
        exists for."""
        box = SecretBox("test-key")
        settings = _settings(tmp_path)
        await leaving_soon.after_scan(factory, settings, box)
        await leaving_soon.after_scan(factory, settings, box)
        assert len(posts) == 1


class TestTheLockBindsToTheRunningLoop:
    async def test_one_lock_per_loop(self) -> None:
        """Two callers on one loop meet one lock, which is what serializes a pass against
        every other one in the process.

        That the lock is per loop, and that a closed loop's lock is collected, are
        properties of ``aio.per_loop_lock``, pinned in ``tests/test_aio.py``. This asserts
        only that this module reads through it.
        """
        assert leaving_soon._pass_lock() is leaving_soon._pass_lock()


class _CountingClient:
    """Stands in for a PlexClient so the test can count what was opened against what was
    closed. Never reaches the network."""

    def __init__(self, opened: list[_CountingClient]) -> None:
        self.closed = False
        opened.append(self)

    async def aclose(self) -> None:
        self.closed = True


class TestNoPlexClientIsLeftOpen:
    """Every library toggled off, while deletion is unarmed, must never leak a Plex client
    or its pooled connections. ``cleanup_sections`` must close any client it opens, on
    every path through the function, including the ones that return early.
    """

    @pytest.fixture
    def opened(self) -> list[_CountingClient]:
        return []

    @pytest.fixture(autouse=True)
    def _stub_client(self, monkeypatch: pytest.MonkeyPatch, opened: list[Any]) -> None:
        async def _client(*_args: object, **_kwargs: object) -> _CountingClient:
            return _CountingClient(opened)

        monkeypatch.setattr(leaving_soon, "_plex_client", _client)

    async def test_the_read_only_cleanup_never_opens_one(
        self,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
        opened: list[_CountingClient],
    ) -> None:
        ran = await leaving_soon.cleanup_sections(
            factory, _settings(tmp_path), SecretBox("test-key"), sections=[{"key": 1}]
        )
        assert ran is False
        assert opened == [], "a client built behind a guard that returns has no owner to close it"

    async def test_a_cleanup_that_does_write_closes_what_it_opened(
        self,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
        opened: list[_CountingClient],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The other half. The guard must not become a reason the client is never closed."""

        async def _safety(*_args: object, **_kwargs: object) -> RuntimeSafety:
            # The read-only opt-in that lets the shelf be written while deletion is off.
            # ``leaving_soon_write_allowed`` is derived from it.
            return RuntimeSafety(allow_leaving_soon_unarmed=True)

        async def _sync(*_args: object, **_kwargs: object) -> list[Any]:
            return []

        monkeypatch.setattr(app_settings, "runtime_safety", _safety)
        monkeypatch.setattr(leaving_soon, "sync_shelves", _sync)

        ran = await leaving_soon.cleanup_sections(
            factory, _settings(tmp_path), SecretBox("test-key"), sections=[{"key": 1}]
        )
        assert ran is True
        assert [c.closed for c in opened] == [True]

    async def test_a_pass_that_fails_while_gathering_leaves_none_open(
        self,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
        opened: list[_CountingClient],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """``run_sync`` builds the client first, and only wraps its try/finally around
        four awaited reads that come later, so any one of those reads raising must still
        not leak the client."""

        async def _boom(*_args: object, **_kwargs: object) -> GraceReport:
            raise RuntimeError("the grace read failed")

        monkeypatch.setattr(leaving_soon, "grace_report", _boom)
        async with factory() as session:
            await app_settings.set_leaving_soon_enabled(session, enabled=True)
            await session.commit()

        with pytest.raises(RuntimeError):
            await leaving_soon.run_sync(factory, _settings(tmp_path), SecretBox("test-key"))

        assert all(c.closed for c in opened), "a client outlived the pass that owned it"


class TestADegradedScanDoesNotReachTheShelf:
    """A degraded snapshot cannot be planned, so its side effects are gated the same way
    its plan is.

    The shelf and the Discord heads-up both read the same condemned set the planner
    refuses to touch. Labeling titles "leaving soon" in someone's library, or telling a
    room of people a title is about to go, on evidence Reaper has already declared
    untrustworthy, is the same mistake as deleting on it, minus the file.

    The guard sits in ``_run_pass``, where both the automatic after-scan path and
    ``POST /api/leaving-soon/sync`` converge, so both paths get the same protection.
    """

    async def _snapshot(self, factory: async_sessionmaker[AsyncSession], *, degraded: bool) -> None:
        from reaper.db.models import Snapshot

        async with factory() as session:
            session.add(
                Snapshot(
                    created_at=utcnow(),
                    horizon_at=utcnow(),
                    policy_hash="h",
                    degraded=degraded,
                )
            )
            await session.commit()

    async def test_the_manual_sync_refuses_on_a_degraded_scan(
        self, factory: async_sessionmaker[AsyncSession], tmp_path: Path
    ) -> None:
        async with factory() as session:
            await app_settings.set_leaving_soon_enabled(session, enabled=True)
            await session.commit()
        await self._snapshot(factory, degraded=True)

        with pytest.raises(leaving_soon.LeavingSoonDegradedError) as caught:
            await leaving_soon.run_sync(factory, _settings(tmp_path), SecretBox("test-key"))
        # Plain language that says what to do about it.
        assert "couldn't be trusted" in str(caught.value)

    async def test_a_clean_scan_after_a_degraded_one_is_not_blocked(
        self, factory: async_sessionmaker[AsyncSession], tmp_path: Path
    ) -> None:
        """The latest scan decides. A degraded run the operator has since re-scanned past
        must not keep the shelf shut forever.
        """
        async with factory() as session:
            await app_settings.set_leaving_soon_enabled(session, enabled=True)
            await session.commit()
        await self._snapshot(factory, degraded=True)
        await self._snapshot(factory, degraded=False)

        # It gets past the degraded guard and fails later, on the missing Plex link. This
        # names the exact error rather than catching any exception, because the point is
        # which gate stopped it. A test that accepted any failure would pass even if the
        # degraded guard had fired instead.
        with pytest.raises(LeavingSoonUnlinkedError, match="needs a linked Plex server"):
            await leaving_soon.run_sync(factory, _settings(tmp_path), SecretBox("test-key"))

    async def test_no_snapshot_at_all_is_not_degraded(
        self, factory: async_sessionmaker[AsyncSession]
    ) -> None:
        """A fresh install has nothing to announce, which is not the same as untrustworthy."""
        async with factory() as session:
            assert await leaving_soon._latest_scan_degraded(session) is False

    async def test_the_after_scan_hook_does_not_announce_either(
        self,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """With the shelf off, the hook falls through to a Discord-only heads-up that
        never reaches ``_run_pass``'s guard. That fall-through reads the same condemned
        set, so it carries the check too.

        A notifier is forced in on purpose. Without one, the fall-through would return
        before it ever announces, and this test would pass even with the guard deleted.
        """
        announced: list[object] = []

        async def _spy(*args: object, **kwargs: object) -> tuple[bool, list[str]]:
            announced.append(args)
            return (False, [])

        async def _notifier(*args: object, **kwargs: object) -> object:
            return object()  # stands in for a configured Discord webhook

        monkeypatch.setattr(leaving_soon, "announce_new", _spy)
        monkeypatch.setattr(leaving_soon, "build_notifier", _notifier)
        # Shelf off (the default), so run_sync raises Disabled and the hook falls through.
        await self._snapshot(factory, degraded=True)

        await leaving_soon.after_scan(factory, _settings(tmp_path), SecretBox("test-key"))

        assert announced == [], "a degraded scan still announced titles as leaving soon"

    async def test_the_after_scan_hook_still_announces_on_a_clean_scan(
        self,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The control for the test above. Same setup, clean snapshot, and the heads-up
        goes out. Without this, the test above would pass even on a hook that never
        announces."""
        announced: list[object] = []

        async def _spy(*args: object, **kwargs: object) -> tuple[bool, list[str]]:
            announced.append(args)
            return (False, [])

        async def _notifier(*args: object, **kwargs: object) -> object:
            return object()

        monkeypatch.setattr(leaving_soon, "announce_new", _spy)
        monkeypatch.setattr(leaving_soon, "build_notifier", _notifier)
        await self._snapshot(factory, degraded=False)

        await leaving_soon.after_scan(factory, _settings(tmp_path), SecretBox("test-key"))

        assert len(announced) == 1


class TestAScanThatSkippedTheShelfSaysSo:
    """Every skip in ``after_scan`` returns before ``_run_pass`` writes its record. Without
    its own record, the Jobs row would just re-read the last completed pass and answer for
    the scan with it, showing a green dot, an old timestamp, and counts, under a line
    reading "Runs after every scan."

    The skip is written to its own row and never cleared. The row prefers it only while it
    is newer than the completed pass, so a pass that later completes wins on its own
    timestamp. This is the same arrangement ``ScanRow`` already uses for a scheduled scan
    that crashed and wrote no snapshot.
    """

    async def _snapshot(self, factory: async_sessionmaker[AsyncSession], *, degraded: bool) -> None:
        from reaper.db.models import Snapshot

        async with factory() as session:
            session.add(
                Snapshot(
                    created_at=utcnow(),
                    horizon_at=utcnow(),
                    policy_hash="h",
                    degraded=degraded,
                )
            )
            await session.commit()

    async def _skip(self, factory: async_sessionmaker[AsyncSession]) -> tuple[str, Reason] | None:
        async with factory() as session:
            return await app_settings.get_leaving_soon_last_skip(session)

    async def test_an_untrustworthy_scan_is_written_down(
        self, factory: async_sessionmaker[AsyncSession], tmp_path: Path
    ) -> None:
        async with factory() as session:
            await app_settings.set_leaving_soon_enabled(session, enabled=True)
            await session.commit()
        await self._snapshot(factory, degraded=True)

        await leaving_soon.after_scan(factory, _settings(tmp_path), SecretBox("test-key"))

        skip = await self._skip(factory)
        assert skip is not None, "a scan skipped the shelf and left no record of it"
        # The exact code, because the row trails it after a timestamp, and this is the
        # whole of what the operator is told. This is a typed value, not a transcribed
        # phrase.
        assert skip[1].id == "error.leaving_soon.skip_degraded"
        assert skip[0]

    async def test_no_plex_link_is_written_down(
        self, factory: async_sessionmaker[AsyncSession], tmp_path: Path
    ) -> None:
        """No Plex link at all, which ``_run_pass`` raises ``LeavingSoonUnlinkedError`` for.

        This is a different clause from the degraded case above and the unreachable case
        below, and it is asserted as such. The clause is the operator's only signal for
        which one happened, and the fixes differ, so a change collapsing two clauses
        together would still satisfy a test that only checked that some record exists.
        """
        async with factory() as session:
            await app_settings.set_leaving_soon_enabled(session, enabled=True)
            await session.commit()
        await self._snapshot(factory, degraded=False)

        await leaving_soon.after_scan(factory, _settings(tmp_path), SecretBox("test-key"))

        skip = await self._skip(factory)
        assert skip is not None
        assert skip[1].id == "error.leaving_soon.skip_unlinked"

    async def test_a_linked_server_that_will_not_answer_is_written_down(
        self,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The arm where a linked Plex server exists but does not answer.

        The case above has no link at all, so the pass never gets as far as talking to a
        server. This test reaches the arm where the server is contacted and stalls.
        """

        async def _stalled(*args: object, **kwargs: object) -> object:
            raise PlexError("movie listing for section 3 stalled at 200 of 1000")

        async with factory() as session:
            await app_settings.set_leaving_soon_enabled(session, enabled=True)
            await session.commit()
        await self._snapshot(factory, degraded=False)
        monkeypatch.setattr(leaving_soon, "_plex_client", _stalled)

        await leaving_soon.after_scan(factory, _settings(tmp_path), SecretBox("test-key"))

        skip = await self._skip(factory)
        assert skip is not None
        # The row clause carries no client text at all. A Jobs row is scanned, not read
        # closely, so the diagnostic tail belongs on the route's response instead, where
        # someone is actually reading it.
        assert skip[1].id == "error.leaving_soon.skip_unreachable"

    async def test_the_shelf_being_off_is_not_written_down(
        self, factory: async_sessionmaker[AsyncSession], tmp_path: Path
    ) -> None:
        """The one skip with nothing on screen to correct. The row renders its "Off"
        branch from the same setting and shows no last-run line at all. Recording a skip
        here would put a failure into a row that never draws one, and it would outlive the
        operator turning the shelf back on.
        """
        await self._snapshot(factory, degraded=False)  # shelf off is the default

        await leaving_soon.after_scan(factory, _settings(tmp_path), SecretBox("test-key"))

        assert await self._skip(factory) is None

    async def test_a_completed_pass_is_newer_than_the_skip_before_it(
        self, factory: async_sessionmaker[AsyncSession], tmp_path: Path
    ) -> None:
        """Nothing clears the skip row, so what makes a recovery visible is the completed
        pass carrying a later timestamp. This is pinned here rather than left to the
        reader, because the reader's comparison is the only thing that retires a skip. If
        the two were written with the same instant, or out of order, a shelf that
        recovered would keep reporting the old failure forever.
        """
        async with factory() as session:
            await app_settings.set_leaving_soon_enabled(session, enabled=True)
            await session.commit()
        await self._snapshot(factory, degraded=True)
        await leaving_soon.after_scan(factory, _settings(tmp_path), SecretBox("test-key"))

        # A completed pass, written the way _run_pass writes one, but called directly
        # here. Reaching it through after_scan would need a reachable Plex, and what is
        # under test is the ordering of the two rows, not the pass that produces one of
        # them.
        async with factory() as session:
            await app_settings.set_leaving_soon_last(
                session,
                at=(utcnow() + timedelta(seconds=1)).isoformat(),
                movies=3,
                seasons=4,
                applied=True,
                ok=True,
                reason=Reason("shelf_updated", {"added": 1, "removed": 0}),
            )
            await session.commit()

        skip = await self._skip(factory)
        async with factory() as session:
            last = await app_settings.get_leaving_soon_last(session)
        assert skip is not None and last is not None
        assert last["at"] > skip[0], "a completed pass could not retire the skip before it"

    async def test_a_specific_reason_survives_a_later_surprise(
        self,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The no-link path records its reason and then falls through to the Discord
        heads-up, which sits inside the catch-all. A surprise down there must not
        overwrite "no Plex server is linked" with the vague clause, because the specific
        one is the only clause that tells the operator what to fix. The unreachable arm
        beside it falls through the same way, so this test covers both.

        A notifier is forced in because the fall-through would return before announcing
        without one. The surprise is planted in ``announce_new`` rather than
        ``build_notifier``, because ``_run_pass`` builds a notifier too, so a raise from
        there would never reach the fall-through at all.
        """
        async with factory() as session:
            await app_settings.set_leaving_soon_enabled(session, enabled=True)
            await session.commit()
        await self._snapshot(factory, degraded=False)

        async def _notifier(*args: object, **kwargs: object) -> object:
            return object()  # stands in for a configured Discord webhook

        async def _boom(*args: object, **kwargs: object) -> object:
            raise RuntimeError("something nobody has a name for")

        monkeypatch.setattr(leaving_soon, "build_notifier", _notifier)
        monkeypatch.setattr(leaving_soon, "announce_new", _boom)

        await leaving_soon.after_scan(factory, _settings(tmp_path), SecretBox("test-key"))

        skip = await self._skip(factory)
        assert skip is not None
        assert skip[1].id == "error.leaving_soon.skip_unlinked"

    async def test_a_surprise_with_no_name_still_says_the_shelf_did_not_move(
        self,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The catch-all's own clause, for a failure inside the pass that no branch above
        it names. The row cannot say what to fix here, so it says the one thing it knows.
        The shelf did not move. Without this clause, the row would stay silent, and
        silence reads as success.
        """
        async with factory() as session:
            await app_settings.set_leaving_soon_enabled(session, enabled=True)
            await session.commit()
        await self._snapshot(factory, degraded=False)

        async def _boom(*args: object, **kwargs: object) -> object:
            raise RuntimeError("something nobody has a name for")

        # Inside _run_pass, above the Plex link check, so no named branch can claim it.
        monkeypatch.setattr(leaving_soon, "build_notifier", _boom)

        await leaving_soon.after_scan(factory, _settings(tmp_path), SecretBox("test-key"))

        skip = await self._skip(factory)
        assert skip is not None
        assert skip[1].id == "error.leaving_soon.skip_failed"
