# SPDX-License-Identifier: AGPL-3.0-or-later
"""The two read-only surfaces: what Scales counts, and what the shelf announces.

Scales deletes nothing, so its findings are all the same shape -- a number the operator
reads while deciding, that did not mean what it said. A request for seasons the scan does
not hold inflated the board's request count and the watch-rate denominator while appearing
in neither list an operator could open (B2-19). One title reached through two id groups
charged the same person twice (B-14). Leaving Soon's half is the announced set, read at the
top of a pass and written at the bottom with minutes of network I/O in between (B2-20), plus
the Plex clients its read-only path built and never closed (PR-3).
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
    """A season-scoped request is the DEFAULT shape Seerr sends, and a scan routinely holds
    only some of a show's seasons (the rest protected, or filtered out). The show still
    matched, so the request was not "not in scan"; its own seasons scoped to nothing, so the
    drawer skipped it. It counted, and it was nowhere (B2-19)."""

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
        """The alternative fix -- skip it in the roll-up and stop there -- makes the request
        vanish from every surface, which is a quieter version of the same problem."""
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
        """Classified per request, not per group: one person's phantom season must not take
        their co-requester's real one off the board with it."""
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
        """The board divides ``played_by_them`` by ``requests_made``. A request that scopes to
        nothing can never increment the numerator, and the old per-group dedup counted the
        FIRST request it saw for a person and skipped the rest -- so a phantom arriving first
        took the row, and the season they had actually watched was never looked at. Their
        watch rate read 0%. The order here is the point of the test."""
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
        """The guard is on an empty SCOPE, not on season-scoping itself: an unscoped request
        binds the whole matched set exactly as before."""
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
    """Requests group by a single content key (tmdb, else tvdb, else imdb), but candidates are
    indexed under EVERY id they carry. So co-requests that carry different ids split into two
    groups that resolve to the same candidates, and a person in both was counted in both
    (B-14). The report totals already deduped by candidate set, so the board disagreed with
    its own header."""

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
        """The half that was always right, pinned so the fix converges on it rather than
        breaking the other way."""
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
        """Deduped by (person, matched set), never by matched set alone: co-requesters of one
        title are the ordinary case and both must be charged."""
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
    """Posts are recorded and the post AWAITS, so two passes genuinely overlap in the window
    the announced set used to be read and written across."""

    def __init__(self, posts: list[tuple[str, ...]]) -> None:
        self._posts = posts

    async def announce_leaving_soon(self, titles: list[str], *, grace_days: int) -> bool:
        self._posts.append(tuple(sorted(titles)))
        await asyncio.sleep(0.02)
        return True


class _Rendezvous:
    """Holds the first pass at the announced-set READ until a second pass reaches it.

    Without this the race is left to the scheduler, and the scheduler does not cooperate:
    stubbed out, one pass reliably runs read-post-write to completion before the other gets
    a turn, so a test written the obvious way passes with the lock removed and pins nothing.

    Bounded, so it works both ways. With the lock in place the second pass CANNOT reach the
    read, the wait simply expires, and the passes proceed serialized -- which is the property
    under test -- at the cost of one short timeout.
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
    """Two entry points, nothing serializing them: "Update now" from the Reap page and the
    after-scan hook, which fires at the end of every scan. Both read the same announced set,
    both decide the same title is new, and your users are told twice -- then the later
    writer persists a set built from ITS pre-I/O read and drops what the first recorded, so
    the title is announced a third time next pass (B2-20)."""

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
        """The shelf is off, so both passes fall through to the Discord-only path -- which is
        not an edge case: for an operator running Leaving Soon without the shelf it is the
        only path that ever announces."""
        await self._both(factory, tmp_path)
        assert posts == [("Title 11", "Title 22")]

    async def test_neither_pass_loses_the_other_s_record(
        self,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
        posts: list[tuple[str, ...]],
        overlap: _Rendezvous,
    ) -> None:
        """The durable half, and the reason the duplicate post above matters beyond the one
        extra message: what is announced must end up recorded, or it is announced again next
        pass, and the pass after that. A companion invariant rather than a second proof --
        with both passes seeing the same grace set, whether the losing write drops anything
        depends on which finishes last."""
        await self._both(factory, tmp_path)
        async with factory() as session:
            assert await overlap._real(session) == {11, 22}

    async def test_both_passes_really_did_reach_the_critical_read(
        self,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
        overlap: _Rendezvous,
    ) -> None:
        """Not a behavior claim: a guard that the setup above is exercising what it says it
        is. If a future change made the second pass bail out before the announced-set read,
        the two assertions above would hold for the wrong reason."""
        await self._both(factory, tmp_path)
        assert overlap.arrivals == 2

    async def test_a_later_pass_announces_nothing_new(
        self,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
        posts: list[tuple[str, ...]],
    ) -> None:
        """Sequential, no rendezvous: the plain idempotence the durable set exists for."""
        box = SecretBox("test-key")
        settings = _settings(tmp_path)
        await leaving_soon.after_scan(factory, settings, box)
        await leaving_soon.after_scan(factory, settings, box)
        assert len(posts) == 1


class TestTheLockBindsToTheRunningLoop:
    async def test_one_lock_per_loop(self) -> None:
        """Two callers on one loop meet one lock, which is what serializes a pass against
        every other one in the process.

        That the lock is per-LOOP, and that a closed loop's lock is collected, are properties
        of ``aio.per_loop_lock`` and are pinned in ``tests/test_aio.py``. This asserts only
        that this module reads through it.
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
    """``cleanup_sections`` built the client and returned on the very next line whenever
    writing was not allowed -- the DEFAULT state -- so every library toggled off while
    deletion was unarmed leaked a client and its pooled connections (PR-3)."""

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
        """The other half: the guard must not become a reason the client is never closed."""

        async def _safety(*_args: object, **_kwargs: object) -> RuntimeSafety:
            # The read-only opt-in, which is what lets the shelf be written while deletion
            # is off; ``leaving_soon_write_allowed`` is derived from it.
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
        """``run_sync`` built the client first and only started its try/finally four awaited
        reads later, so any one of them raising leaked it."""

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
    """A degraded snapshot is un-plannable, and its side effects are gated with its plan.

    The shelf and the Discord heads-up both read the same condemned set the planner
    refuses to touch. Labeling titles "leaving soon" in someone's library, or telling a
    room of people a title is about to go, on evidence Reaper has already declared
    untrustworthy is the same mistake as deleting on it, minus the file (rule 116).

    ``scan_runner`` already skipped the after-scan shelf on a degraded snapshot, but that
    covered only the automatic path: ``POST /api/leaving-soon/sync`` still labeled and
    announced. The guard now sits in ``_run_pass``, where both paths converge.
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
        # Plain language, and it says what to do about it (rule 21).
        assert "couldn't be trusted" in str(caught.value)

    async def test_a_clean_scan_after_a_degraded_one_is_not_blocked(
        self, factory: async_sessionmaker[AsyncSession], tmp_path: Path
    ) -> None:
        """The LATEST scan decides. A degraded run the operator has since re-scanned past
        must not keep the shelf shut forever."""
        async with factory() as session:
            await app_settings.set_leaving_soon_enabled(session, enabled=True)
            await session.commit()
        await self._snapshot(factory, degraded=True)
        await self._snapshot(factory, degraded=False)

        # It gets past the degraded guard and fails later, on the missing Plex link. Named
        # exactly, not caught as a bare Exception: the point is WHICH gate stopped it, and a
        # test that accepts any failure would pass even if the degraded guard had fired
        # (rule 119).
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
        """With the SHELF OFF, the hook falls through to a Discord-only heads-up that
        never reaches ``_run_pass``'s guard. That fall-through reads the same condemned
        set, so it carries the check too.

        A notifier is forced in on purpose: without one the fall-through returns before it
        would ever announce, and the test would pass with the guard deleted.
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
        """The control for the test above: same setup, clean snapshot, and the heads-up
        goes out. Without this the one above would pass on a hook that never announces."""
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
    """Every skip in ``after_scan`` returns before ``_run_pass`` writes its record, so the
    Jobs row re-read the last COMPLETED pass and answered for the scan with it: a green dot,
    an old timestamp and counts, under a line reading "Runs after every scan".

    The skip is written to its own row and never cleared. The row prefers it only while it
    is newer than the completed pass, so a pass that later completes wins on its own
    timestamp -- the arrangement ``ScanRow`` already uses for a scheduled scan that crashed
    and wrote no snapshot.
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
        # The exact code, because the row trails it after a timestamp and this is the whole
        # of what the operator is told. Typed, not a transcribed phrase (phase 8a).
        assert skip[1].id == "error.leaving_soon.skip_degraded"
        assert skip[0]

    async def test_no_plex_link_is_written_down(
        self, factory: async_sessionmaker[AsyncSession], tmp_path: Path
    ) -> None:
        """No Plex link at all, which ``_run_pass`` raises ``LeavingSoonUnlinkedError`` for.

        A different clause from the degraded case above and from the unreachable case below,
        and asserted as such: they are the operator's only signal for which happened, and the
        fixes differ, so a change collapsing them would still satisfy a test that only
        checked a record exists. All three shared two classes until #734.
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
        """The arm the route now answers 502 for, and the one this file could not reach
        before: with no link, the pass never got as far as talking to a server (#734)."""

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
        # The row clause carries no client text at all: a Jobs row is scanned, and the
        # diagnostic tail belongs on the route's response, where someone is reading it.
        assert skip[1].id == "error.leaving_soon.skip_unreachable"

    async def test_the_shelf_being_off_is_not_written_down(
        self, factory: async_sessionmaker[AsyncSession], tmp_path: Path
    ) -> None:
        """The one skip with nothing on screen to correct: the row renders its "Off" branch
        from the same setting and shows no last-run line at all. Recording here would put a
        failure into a row that never draws one, and it would outlive the operator turning
        the shelf back on."""
        await self._snapshot(factory, degraded=False)  # shelf off is the default

        await leaving_soon.after_scan(factory, _settings(tmp_path), SecretBox("test-key"))

        assert await self._skip(factory) is None

    async def test_a_completed_pass_is_newer_than_the_skip_before_it(
        self, factory: async_sessionmaker[AsyncSession], tmp_path: Path
    ) -> None:
        """Nothing clears the skip row, so what makes a recovery visible is the completed
        pass carrying a LATER timestamp. Pinned here rather than left to the reader, because
        the reader's comparison is the only thing that retires a skip: were the two written
        with the same instant, or out of order, a shelf that recovered would keep reporting
        the old failure forever."""
        async with factory() as session:
            await app_settings.set_leaving_soon_enabled(session, enabled=True)
            await session.commit()
        await self._snapshot(factory, degraded=True)
        await leaving_soon.after_scan(factory, _settings(tmp_path), SecretBox("test-key"))

        # A completed pass, written the way _run_pass writes one. Called directly: reaching
        # it through after_scan needs a reachable Plex, and what is under test is the
        # ordering of the two rows, not the pass that produces one of them.
        async with factory() as session:
            await app_settings.set_leaving_soon_last(
                session,
                at=(utcnow() + timedelta(seconds=1)).isoformat(),
                movies=3,
                seasons=4,
                applied=True,
                ok=True,
                result="1 added, 0 cleared",
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
        heads-up, which sits inside the catch-all. A surprise down there must not overwrite
        "no Plex server is linked" with the vague clause: the specific one is the only clause
        that tells the operator what to go and fix. The unreachable arm beside it falls
        through the same way, so this covers both (#734 split the two).

        A notifier is forced in because the fall-through returns before announcing without
        one, and the surprise is planted in ``announce_new`` rather than ``build_notifier``:
        ``_run_pass`` builds a notifier too, so a raise from there never reaches the
        fall-through and the test would prove the opposite branch (which is how it was
        written first, and it passed).
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
        """The catch-all's own clause, for a failure inside the pass that no branch above it
        names. The row cannot say what to fix here, so it says the one thing it knows: the
        shelf did not move. Silence is what it did before, and silence reads as success."""
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
