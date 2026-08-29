# SPDX-License-Identifier: AGPL-3.0-or-later
"""A title that came back: the ledger, the four conditions, and the hold.

The rule these pin is stated in ``services/library_seen.py`` and argued in
``docs/history/RETURN_PLAN.md``. Each condition exists because of a measurement, so each
gets a case proving it can still hold a return back on its own. The population that would
have shipped without condition 2, one external id carrying two \\*arr entries, gets its own
case (``docs/LEARNINGS.md``, assumption 16).

Nothing here re-implements the rule. Every case calls ``is_return``, ``id_key``,
``within_cap`` or the real gate.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from reaper.clock import utcnow
from reaper.config import Settings
from reaper.db.base import Base
from reaper.db.models import (
    ActionStep,
    Candidate,
    ReapRun,
    RunState,
    Snapshot,
    StepState,
)
from reaper.db.session import create_engine, create_session_factory
from reaper.engine import identity
from reaper.engine.gates import ABSTAIN, PROTECT, Facts, GateConfig, GateId, ReturnedGate
from reaper.engine.observation import Absent, Known, Unknown
from reaper.services import library_seen
from tests._reasons import text

# Whole seconds. ``UtcTimestamp`` stores epoch seconds, so a microsecond would survive in
# memory and not in the row (``test_watch_evidence``'s NOW, same reason).
NOW = utcnow().replace(microsecond=0)

#: Deliberately not the shipped 7. A fixture pinning the production default cannot prove
#: the caller passed anything, because an omission and a correct pass read alike.
ABSENCE_DAYS = 11

#: Deliberately not the shipped 548, for the same reason.
HOLD_DAYS = 400


def _seen(
    *,
    keys: set[int],
    last_seen_days_ago: float = 60,
    returned_days_ago: float | None = None,
    by_reaper: bool | None = None,
) -> library_seen.Seen:
    return library_seen.Seen(
        rating_keys=frozenset(keys),
        last_seen_at=NOW - timedelta(days=last_seen_days_ago),
        returned_at=(NOW - timedelta(days=returned_days_ago)) if returned_days_ago else None,
        returned_by_reaper=by_reaper,
    )


#: The one id every helper below sights under, unless a case names another.
SIGHTING_ID = "movie:tmdb:1"


def _sighting(
    *, key: int = 900, added_days_ago: float = 3, id_key: str = SIGHTING_ID
) -> library_seen.Sighting:
    return library_seen.Sighting(
        id_key=id_key,
        rating_key=key,
        added_at=NOW - timedelta(days=added_days_ago),
    )


#: An irregular scan history, the shape a real install has: a dense run, a long pause, then
#: more. Every case that means to exercise condition 4 narrows the window instead of thinning
#: this list, so the four conditions stay independently testable.
SCANS = sorted(NOW - timedelta(days=d) for d in (58, 55, 50, 40, 25, 1))


def _batch(*sightings: library_seen.Sighting) -> dict[str, set[int]]:
    """One scan's keys, folded exactly as both lanes fold them."""
    batch: dict[str, set[int]] = {}
    for sighting in sightings:
        library_seen.note_sighting(batch, sighting)
    return batch


def _is_return(
    seen: library_seen.Seen,
    sighting: library_seen.Sighting,
    *,
    live_keys: set[int] | None = None,
    scans: list[datetime] | None = None,
) -> bool:
    return library_seen.is_return(
        seen,
        sighting,
        live_keys=live_keys if live_keys is not None else set(),
        scan_instants=SCANS if scans is None else scans,
        cooling_off_days=ABSENCE_DAYS,
        now=NOW,
    )


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


class TestTheLedgerKey:
    """One id, one row, and never two different titles in it."""

    def test_a_movie_and_a_show_sharing_a_number_are_two_rows(self) -> None:
        """Movie and TV tmdb ids share one integer space.

        Without the media kind in the key, a film and a show carrying the same tmdb id would
        overwrite each other's rating keys and read as a return on every scan.
        """
        movie = library_seen.id_key(media_type="movie", tmdb=12345)
        show = library_seen.id_key(media_type="season", tmdb=12345, season=1)
        assert movie is not None
        assert show is None, "no tvdb and no imdb means the TV ladder has nothing to key on"

    def test_the_kind_leads_every_key(self) -> None:
        assert library_seen.id_key(media_type="movie", tmdb=12345) == "movie:tmdb:12345"
        assert library_seen.id_key(media_type="season", tvdb=678) == "tv:tvdb:678"

    def test_a_season_is_its_show_plus_its_number(self) -> None:
        """Assumption 16's trap. A show's TVDb id is shared by every season it has, so a key
        without the number counts the season structure rather than the seasons."""
        s1 = library_seen.id_key(media_type="season", tvdb=678, season=1)
        s3 = library_seen.id_key(media_type="season", tvdb=678, season=3)
        assert s1 == "tv:tvdb:678:s1"
        assert s3 == "tv:tvdb:678:s3"
        assert s1 != s3

    def test_each_lane_falls_back_to_imdb(self) -> None:
        assert library_seen.id_key(media_type="movie", imdb="tt7") == "movie:imdb:tt7"
        assert library_seen.id_key(media_type="season", imdb="tt7", season=2) == "tv:imdb:tt7:s2"

    def test_a_title_with_no_id_has_no_key(self) -> None:
        # A stated limitation, the same reach every id-matched feature here already has.
        assert library_seen.id_key(media_type="movie") is None
        assert library_seen.id_key(media_type="season", season=1) is None

    def test_the_ladders_are_the_ones_the_plex_resolver_binds_on(self) -> None:
        """The key and the bind must rest on the same id, so the preferred kind here is
        asserted against the resolver's own tuples rather than transcribed into a comment
        nobody re-reads."""
        assert library_seen.id_key(media_type="movie", tmdb=1, imdb="tt1") == (
            f"movie:{identity.MOVIE_ID_PRIORITY[0]}:1"
        )
        assert library_seen.id_key(media_type="season", tvdb=1, imdb="tt1", season=4) == (
            f"tv:{identity.SHOW_ID_PRIORITY[0]}:1:s4"
        )


class TestTheFourConditions:
    """Each one, proved to hold a return back on its own."""

    def test_the_whole_rule_fires_when_every_condition_holds(self) -> None:
        assert _is_return(_seen(keys={1}), _sighting()) is True

    def test_a_key_already_recorded_is_not_a_return(self) -> None:
        # Condition 1. The ordinary state of a title that has sat in one place.
        assert _is_return(_seen(keys={900}), _sighting(key=900)) is False

    def test_an_old_key_still_listed_in_plex_is_not_a_return(self) -> None:
        """Condition 2, and the case that would have shipped.

        About one movie entry in 150 shares its TMDb id with a second \\*arr entry, one per
        copy, each bound to a different Plex listing (``docs/LEARNINGS.md``, assumption 16).
        The bind moving between two listings that *both* still exist is not a return, and
        without this the ledger would hold both copies forever.
        """
        assert _is_return(_seen(keys={1}), _sighting(), live_keys={1}) is False

    def test_one_of_several_old_keys_still_listed_is_enough_to_refuse(self) -> None:
        assert _is_return(_seen(keys={1, 2, 3}), _sighting(), live_keys={3}) is False

    def test_an_absence_under_the_bar_is_not_a_return(self) -> None:
        """Condition 3. Every rating-key change measured on a real library completed inside
        30 hours, so mechanical churn resolves far under a multi-day bar."""
        seen = _seen(keys={1}, last_seen_days_ago=ABSENCE_DAYS - 1)
        assert _is_return(seen, _sighting(added_days_ago=0)) is False

    def test_the_bar_is_the_value_passed_not_the_shipped_default(self) -> None:
        # The fixture bar differs from the production default, so this can only pass if
        # the argument reaches the comparison. Same seen row and same sighting on both
        # sides, so the bar is the only thing that moves.
        seen = _seen(keys={1}, last_seen_days_ago=60)
        arrived_45_days_ago = _sighting(added_days_ago=45)
        assert _is_return(seen, arrived_45_days_ago) is True
        assert (
            library_seen.is_return(
                seen,
                arrived_45_days_ago,
                live_keys=set(),
                scan_instants=SCANS,
                cooling_off_days=20,
                now=NOW,
            )
            is False
        )

    def test_one_scan_inside_the_absence_is_not_enough(self) -> None:
        """Condition 4, the half a clock cannot do.

        ``last_seen_at`` is the last time Reaper *looked*, so on an install with a 202-hour
        gap between scans a file swapped out in minutes reads as an eight-day absence.
        Requiring that Reaper actually ran during it closes that.
        """
        one_scan = [NOW - timedelta(days=30)]
        assert _is_return(_seen(keys={1}), _sighting(), scans=one_scan) is False

    def test_no_scan_inside_the_absence_is_not_enough(self) -> None:
        # A library rebuilt in an afternoon. Every key reissued, and nothing ran while it
        # was away.
        assert _is_return(_seen(keys={1}), _sighting(), scans=[]) is False

    def test_scans_outside_the_absence_do_not_count(self) -> None:
        # Both endpoints are open. The scan that last saw it did not run while it was
        # missing, and neither did one simultaneous with the copy's arrival.
        outside = [NOW - timedelta(days=60), NOW - timedelta(days=3), NOW]
        assert _is_return(_seen(keys={1}), _sighting(), scans=outside) is False


class TestWhatCannotManufactureAReturn:
    """Absence is never an input, so missing data delays a detection and never invents one."""

    def test_a_title_with_no_recorded_key_is_not_a_return(self) -> None:
        # A row written this scan, or one whose stored keys would not parse. There is no
        # earlier key whose disappearance could be checked.
        assert _is_return(_seen(keys=set()), _sighting()) is False

    def test_a_copy_with_no_arrival_date_is_not_a_return(self) -> None:
        # No clock, so condition 3 cannot be answered and the hold is withheld.
        no_date = library_seen.Sighting(id_key="movie:tmdb:1", rating_key=900, added_at=None)
        assert _is_return(_seen(keys={1}), no_date) is False

    def test_an_arrival_date_in_the_future_is_clamped(self) -> None:
        """A clock ahead of Reaper's must not widen the gap it is measured against.

        The fixture is chosen so that only the clamp decides it, which an obvious fixture
        does not. With the shared scan history, deleting the clamp leaves this False
        anyway, because the widened window still holds under two scans and condition 4
        refuses it on its own. A test that both the correct and the broken code pass is not
        a proof.

        Here, last seen was 5 days ago, arrival claims "in a year", and two scans ran
        inside the last 5 days. Clamped, the gap is 5 days and fails the eleven-day bar.
        Unclamped it is 370 days, clears the bar, and finds both scans inside, so it would
        return True.
        """
        seen = _seen(keys={1}, last_seen_days_ago=5)
        ahead = _sighting(added_days_ago=-365)
        recent = [NOW - timedelta(days=4), NOW - timedelta(days=2)]
        assert _is_return(seen, ahead, scans=recent) is False
        # ...and the same fixture with an honest date *does* return True, so the case
        # above fails for the clamp and not because nothing could ever pass here.
        honest = _sighting(added_days_ago=1)
        assert _is_return(_seen(keys={1}, last_seen_days_ago=60), honest, scans=recent) is True

    def test_an_arrival_before_the_last_sighting_is_not_a_return(self) -> None:
        seen = _seen(keys={1}, last_seen_days_ago=3)
        assert _is_return(seen, _sighting(added_days_ago=60)) is False


class TestTheScanCount:
    """``scans_inside`` counts a strict interior, which is what condition 4 asks for."""

    def test_it_counts_only_what_ran_between(self) -> None:
        start, end = NOW - timedelta(days=50), NOW - timedelta(days=10)
        assert library_seen.scans_inside(SCANS, start, end) == 2

    def test_the_endpoints_themselves_do_not_count(self) -> None:
        start, end = SCANS[0], SCANS[-1]
        assert library_seen.scans_inside(SCANS, start, end) == len(SCANS) - 2

    def test_an_empty_history_counts_nothing(self) -> None:
        assert library_seen.scans_inside([], NOW - timedelta(days=9), NOW) == 0


class TestThePopulationCap:
    """This feature's own guard over its own inputs. A separate scan-level cap exists too,
    and this is not that one."""

    def test_a_small_library_keeps_every_detection(self) -> None:
        # One real return on a 20-item library is 5%, which the share alone would refuse.
        assert library_seen.within_cap(1, 20) is True

    def test_a_plausible_share_of_a_real_library_stands(self) -> None:
        assert library_seen.within_cap(20, 3500) is True

    def test_a_whole_library_looking_returned_at_once_is_refused(self) -> None:
        # The slow-rebuild residue. Every key reissued while Reaper kept scanning.
        assert library_seen.within_cap(3500, 3500) is False

    def test_the_cap_bites_just_above_its_own_share(self) -> None:
        bound = 1000
        allowed = int(bound * library_seen.RETURN_POPULATION_CAP)
        assert library_seen.within_cap(allowed, bound) is True
        assert library_seen.within_cap(allowed + 1, bound) is False


class TestTheObservations:
    """A lookup that found nothing and a lookup that could not happen are different."""

    def test_no_ledger_row_is_unknown_on_both_fields(self) -> None:
        days, by_reaper = library_seen.observations(None, now=NOW)
        assert isinstance(days, Unknown)
        assert isinstance(by_reaper, Unknown)
        assert days.reason == library_seen.NO_RETURN_RECORD_REASON

    def test_a_row_with_no_return_is_absent_on_both_fields(self) -> None:
        days, by_reaper = library_seen.observations(_seen(keys={1}), now=NOW)
        assert isinstance(days, Absent)
        assert isinstance(by_reaper, Absent)

    def test_a_recorded_return_carries_its_age_and_its_author(self) -> None:
        seen = _seen(keys={1}, returned_days_ago=30, by_reaper=True)
        days, by_reaper = library_seen.observations(seen, now=NOW)
        assert days == Known(value=30.0, source="reaper")
        assert by_reaper == Known(value=True, source="reaper")

    def test_a_return_reaper_cannot_claim_is_a_real_false(self) -> None:
        # Not a gap. It means Reaper has no record of removing it, which is what the
        # second sentence tells the operator.
        _, by_reaper = library_seen.observations(
            _seen(keys={1}, returned_days_ago=5, by_reaper=False), now=NOW
        )
        assert by_reaper == Known(value=False, source="reaper")


class TestTheGate:
    """The hold itself. It can only ever keep a file, and it never blocks."""

    def _facts(self, days_ago: float | None, *, by_reaper: bool = False) -> Facts:
        return Facts(
            title="x",
            days_observed_unwatched=Known(value=5000.0, source="t"),
            distinct_watchers=Known(value=0, source="t"),
            distinct_watchers_all_time=Known(value=0, source="t"),
            size_bytes=Known(value=1, source="t"),
            imdb_rating_tenths=Absent(source="t"),
            imdb_votes=Absent(source="t"),
            season_rank=Absent(source="t"),
            is_streaming_now=Known(value=False, source="t"),
            is_managed=Known(value=True, source="t"),
            in_curated_list=Absent(source="t"),
            is_whitelisted=Known(value=False, source="t"),
            returned_days_ago=(
                Known(value=days_ago, source="reaper")
                if days_ago is not None
                else Absent(source="reaper")
            ),
            returned_by_reaper=Known(value=by_reaper, source="reaper"),
        )

    def _gate(self) -> ReturnedGate:
        return ReturnedGate(config=GateConfig(threshold=HOLD_DAYS, window_days=ABSENCE_DAYS))

    def test_a_recent_return_is_kept_and_says_how_long_is_left(self) -> None:
        result = self._gate().evaluate(self._facts(100))
        assert result.outcome == PROTECT
        assert result.gate is GateId.RETURNED
        assert text(result.detail).endswith(" left")
        assert "came back" in text(result.detail)

    def test_reaper_says_so_when_its_own_journal_shows_the_removal(self) -> None:
        ours = self._gate().evaluate(self._facts(100, by_reaper=True))
        theirs = self._gate().evaluate(self._facts(100, by_reaper=False))
        assert text(ours.detail).startswith("you removed this before")
        assert text(theirs.detail).startswith("this left your library")
        # Same hold either way. Splitting the length would mean a second knob for a
        # difference nobody has measured.
        assert ours.outcome == theirs.outcome == PROTECT

    def test_the_countdown_is_measured_against_the_configured_hold(self) -> None:
        # HOLD_DAYS is not the shipped default, so a gate reading its own constant instead
        # of the config cannot pass this.
        near_end = self._gate().evaluate(self._facts(HOLD_DAYS - 2))
        assert near_end.outcome == PROTECT
        assert "2 days left" in text(near_end.detail)

    def test_an_expired_hold_stops_keeping_the_file(self) -> None:
        result = self._gate().evaluate(self._facts(HOLD_DAYS + 1))
        assert result.outcome == ABSTAIN
        assert result.blocked is False
        assert "came back" in text(result.detail)

    def test_a_title_that_never_left_does_not_fire(self) -> None:
        result = self._gate().evaluate(self._facts(None))
        assert result.outcome == ABSTAIN
        assert result.blocked is False

    def test_an_unreadable_return_abstains_without_blocking(self) -> None:
        """The documented deviation from the fail-closed arm every other gate takes.

        The ledger is empty on a fresh install and on the first scan after this ships, so
        blocking here would amber-flag the whole library and abstain every verdict in it for
        months. The items whose Plex bind genuinely failed are already blocked by the four
        Plex-dependent gates reading the same resolution.
        """
        facts = self._facts(None)
        unknown = Facts(
            **{
                **{f.name: getattr(facts, f.name) for f in facts.__dataclass_fields__.values()},
                "returned_days_ago": Unknown(reason="nothing on record", source="reaper"),
            }
        )
        result = self._gate().evaluate(unknown)
        assert result.outcome == ABSTAIN
        assert result.blocked is False


class TestTheLedgerRoundTrip:
    """What is written, what is preserved, and what only ever grows."""

    async def test_a_first_sighting_records_the_key_and_no_return(
        self, session: AsyncSession
    ) -> None:
        key = SIGHTING_ID
        await library_seen.record(session, _batch(_sighting(key=7)), returns={}, now=NOW)
        await session.flush()
        stored = await library_seen.recall_all(session)
        assert stored[key].rating_keys == frozenset({7})
        assert stored[key].returned_at is None
        assert stored[key].returned_by_reaper is None

    async def test_two_copies_of_one_title_both_get_their_key_recorded(
        self, session: AsyncSession
    ) -> None:
        """Assumption 16, one layer up from where it was first paid for.

        One external id routinely carries *two* \\*arr entries, one per copy, each bound to
        a different Plex listing. A batch holding one sighting per id drops whichever copy
        the scan judged first, so that copy's key is never recorded on any scan while its
        sibling exists. A key that was never recorded can never later be noticed as gone,
        which is the coverage this feature exists to have for exactly that population.

        The stored row was designed as a set for this reason. The batch feeding it has to
        be one too.
        """
        first = _sighting(key=11)
        second = _sighting(key=22)
        await library_seen.record(session, _batch(first, second), returns={}, now=NOW)
        await session.flush()
        assert (await library_seen.recall_all(session))[SIGHTING_ID].rating_keys == frozenset(
            {11, 22}
        )

    async def test_the_key_set_only_ever_grows(self, session: AsyncSession) -> None:
        key = SIGHTING_ID
        await library_seen.record(session, _batch(_sighting(key=7)), returns={}, now=NOW)
        await session.flush()
        await library_seen.record(session, _batch(_sighting(key=8)), returns={}, now=NOW)
        await session.flush()
        stored = await library_seen.recall_all(session)
        assert stored[key].rating_keys == frozenset({7, 8})

    async def test_a_recorded_return_survives_later_ordinary_sightings(
        self, session: AsyncSession
    ) -> None:
        """The hold is a durable fact about the title, and it runs in months.

        A later scan seeing the title again must not clear it, or the hold would last exactly
        one scan and the countdown the operator reads would be a lie.
        """
        key = SIGHTING_ID
        await library_seen.record(session, _batch(_sighting(key=7)), returns={key: True}, now=NOW)
        await session.flush()
        later = NOW + timedelta(days=9)
        await library_seen.record(session, _batch(_sighting(key=7)), returns={}, now=later)
        await session.flush()
        stored = await library_seen.recall_all(session)
        assert stored[key].returned_at == NOW
        assert stored[key].returned_by_reaper is True
        assert stored[key].last_seen_at == later

    async def test_unreadable_stored_keys_read_as_none_rather_than_raising(
        self, session: AsyncSession
    ) -> None:
        # An illegible field costs a detection, never the row.
        key = SIGHTING_ID
        await library_seen.record(session, _batch(_sighting(key=7)), returns={}, now=NOW)
        await session.flush()
        row = await session.get(library_seen.LibrarySeen, key)  # type: ignore[attr-defined]
        assert row is not None
        row.rating_keys_json = "not json"
        await session.flush()
        assert (await library_seen.recall_all(session))[key].rating_keys == frozenset()


class TestTheJournalJoin:
    """Which sentence the operator reads, and nothing else."""

    async def _journal(
        self,
        session: AsyncSession,
        *,
        kind: str,
        state: StepState,
        media_type: str = "movie",
        media_key: str = "radarr:1:5",
    ) -> None:
        snapshot = Snapshot(
            created_at=NOW,
            policy_hash="p",
            horizon_at=NOW - timedelta(days=400),
        )
        session.add(snapshot)
        await session.flush()
        session.add(
            Candidate(
                snapshot_id=snapshot.id,
                media_key=media_key,
                title="x",
                media_type=media_type,
                tmdb_id=42,
                tvdb_id=678,
                verdict="condemn",
                explanation_json="{}",
                created_at=NOW,
            )
        )
        run = ReapRun(
            snapshot_id=snapshot.id,
            policy_hash="p",
            state=RunState.COMPLETED,
            approved_manifest_hash="m",
            approved_by="tester",
            approved_at=NOW,
        )
        session.add(run)
        await session.flush()
        session.add(
            ActionStep(
                run_id=run.id,
                media_key=media_key,
                ordinal=0,
                kind=kind,
                method="DELETE",
                path="/x",
                idempotency_key="k",
                state=state,
                created_at=NOW,
            )
        )
        await session.flush()

    async def test_a_verified_delete_is_a_removal_reaper_can_claim(
        self, session: AsyncSession
    ) -> None:
        await self._journal(session, kind="radarr_delete", state=StepState.VERIFIED)
        assert await library_seen.removed_by_reaper(session, {"movie:tmdb:42"}) == {"movie:tmdb:42"}

    async def test_an_unmonitor_is_not_a_removal(self, session: AsyncSession) -> None:
        # It changes monitoring and deletes nothing, so claiming it would tell an operator
        # their settings removed a file Reaper never touched.
        await self._journal(session, kind="sonarr_unmonitor", state=StepState.VERIFIED)
        assert await library_seen.removed_by_reaper(session, {"movie:tmdb:42"}) == set()

    async def test_a_step_that_never_sent_is_not_a_removal(self, session: AsyncSession) -> None:
        await self._journal(session, kind="radarr_delete", state=StepState.PENDING)
        assert await library_seen.removed_by_reaper(session, {"movie:tmdb:42"}) == set()

    async def test_an_id_the_journal_never_names_is_simply_absent(
        self, session: AsyncSession
    ) -> None:
        await self._journal(session, kind="radarr_delete", state=StepState.VERIFIED)
        assert await library_seen.removed_by_reaper(session, {"movie:tmdb:999"}) == set()

    async def test_a_season_answers_under_its_own_number(self, session: AsyncSession) -> None:
        """The TV path, which the movie cases above cannot reach.

        ``Candidate`` carries no season number of its own, so the id key is rebuilt by reading
        the last segment of the season's ``media_key`` (``sonarr:1:42:3``). Get that wrong and
        every TV return reads as "it left some other way", which is the sentence that tells an
        operator their settings are innocent when the journal says otherwise.
        """
        await self._journal(
            session,
            kind="sonarr_delete_files",
            state=StepState.VERIFIED,
            media_type="season",
            media_key="sonarr:1:42:3",
        )
        wanted = {"tv:tvdb:678:s3", "tv:tvdb:678:s4"}
        assert await library_seen.removed_by_reaper(session, wanted) == {"tv:tvdb:678:s3"}

    async def test_nothing_asked_is_nothing_queried(self, session: AsyncSession) -> None:
        assert await library_seen.removed_by_reaper(session, set()) == set()
