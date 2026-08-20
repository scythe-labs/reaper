# SPDX-License-Identifier: AGPL-3.0-or-later
"""Did this title leave the library and come back? (#553)

A return is the clearest evidence Reaper can get that a removal was wrong: somebody went and
fetched the title again. This module is the memory that makes one visible, and the rule that
decides what counts.

**Plex is the witness, not the journal and not the \\*arr.** The journal only knows about
deletions *Reaper* performed, so an operator who removes a title by hand and re-fetches it
produces the same evidence and leaves no trace in it -- and it is blind to the commonest
return Reaper itself causes, a season prune, which deletes episode files while the Sonarr
series row survives. ``Radarr.added`` is no better: it marks when the row was created, so it
never moves for an operator who deletes the file and leaves the entry. A file that leaves the
Plex library and comes back gets a new rating key, and both scan lanes already carry it.

The journal keeps exactly one job here: choosing which sentence the operator reads.

**The rule.** A return is a title present now under a Plex rating key Reaper has never
recorded for it, when every key it HAS recorded is gone from the index, and it was gone for a
real span of time that Reaper was awake for. Four conditions, and each exists because of a
measurement rather than a worry (``docs/history/RETURN_PLAN.md``, ``docs/LEARNINGS.md``):

1. A key never recorded before. Rules out the ordinary state of a title that has sat still.
2. Every recorded key gone from the index. Rules out a title listed twice, where the bind
   moved between two listings that both still exist. About one movie entry in 150 shares its
   TMDb id with a second \\*arr entry, so without this the ledger would read the other copy's
   key as a change on every scan and hold both copies forever (assumption 16).
3. A minimum absence, the operator's ``window_days``. Every rating-key change measured on a
   real library completed within 2.5 to 30 hours: mechanical churn, a file replaced in place
   or a mistake put straight back, resolves in hours, where a regret takes as long as it takes
   somebody to notice. A seven-day default clears the measured ceiling five times over.
4. At least two scans ran inside that absence. A clock alone is not enough, because
   ``last_seen_at`` is the last time Reaper *looked*: on a library averaging a 17-hour scan
   interval but containing a 202-hour one, a file upgraded during a pause reads as an
   eight-day absence. Requiring that Reaper actually RAN while the title was missing closes it
   at both ends. A dense cadence leans on the clock, a sparse one on the count, and neither
   can be tuned into a false return.

**The hold starts on the scan after the one that detects it**, and that is deliberate rather
than a limitation worked around. The detection is visible for exactly one scan, so it is
written to the ledger and read back as an ordinary stored fact, which lets both lanes read it
identically and lets the population cap below be applied over a whole scan rather than over
whichever items had been judged so far. It costs nothing an operator can reach: a title that
just came back carries a fresh Plex ``added_at``, so its dormancy is near zero and the
dormancy floor is already keeping it.
"""

from __future__ import annotations

import json
from bisect import bisect_left, bisect_right
from collections.abc import Container, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime

import structlog
from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from reaper.db import KEY_CHUNK
from reaper.db.models import ActionStep, Candidate, LibrarySeen, ReapRun, Snapshot, StepState
from reaper.engine.dormancy import dormancy_days
from reaper.engine.observation import Absent, Known, Observation, Unknown

log = structlog.get_logger(__name__)

#: Rows per INSERT. ``watch_evidence._CHUNK``'s sibling and set the same way: six bound values
#: a row puts this at 1,200, over SQLite's historical 999-variable ceiling, so it is lower.
#: Deliberately not ``db.KEY_CHUNK``, which bounds a ``WHERE ... IN`` at one variable per key
#: (rule 94). :func:`recall_all` binds nothing at all and needs no companion.
_CHUNK = 150

#: How many scans must have run while the title was missing (condition 4 above).
_SCANS_INSIDE_THE_ABSENCE = 2

#: The share of this scan's bound items that may look returned before the whole batch is
#: refused. A Plex library rebuilt from scratch reissues every rating key at once, and a
#: rebuild slow enough to outlast the cooling-off period while Reaper keeps scanning satisfies
#: all four conditions library-wide.
#:
#: This is #553's own guard over its own inputs, not the general one. **#809 is the scan-level
#: rebuild check**, which belongs beside ``history_sync._check_regression`` and refuses to
#: JUDGE a scan where identity moved wholesale; this is narrower and stays after #809 lands,
#: because it is about what this feature is willing to believe. Nothing here blocks a scan.
#:
#: 2% because the measured rate of key churn is roughly one entry in a thousand and no title
#: on that library left and came back at all in 24 days, so a real return is a handful of items
#: (``docs/LEARNINGS.md``). Two percent of a 3,500-title library is 70 at once, which no
#: operator produces by re-fetching things they missed.
RETURN_POPULATION_CAP = 0.02

#: The floor under which the cap does not apply, because a share is meaningless over a handful
#: of items: on a 20-item test library one genuine return is 5%. Below this, every detection
#: stands.
_CAP_APPLIES_ABOVE = 200

#: Why a title's return cannot be established: nothing in the ledger to look up. It has no
#: external id, or Reaper is seeing it for the first time, which is every title on a fresh
#: install and on the first scan after this ships.
#:
#: Named rather than typed at the site so the drift walk can see it
#: (``test_review_chips.test_no_reason_is_typed_by_hand``). It reaches no why-panel CAUSE
#: slot, because that slot is the tail of a BLOCKED gate's detail and ``ReturnedGate`` never
#: blocks; the exemption is written down in that file's ``_NO_PANEL_ROUTE`` rather than assumed.
NO_RETURN_RECORD_REASON = "no_return_record"

#: The journal kinds that actually remove a file. ``sonarr_unmonitor`` and
#: ``sonarr_verify_unmonitor`` change monitoring alone and delete nothing, so a season Reaper
#: unmonitored but never emptied is not a removal Reaper can claim (``ActionStep.kind`` names
#: all four).
#:
#: **The same two kinds as ``executor._TERMINAL_DELETE_KINDS``, and deliberately a second
#: copy.** Sharing one declaration would make this module import the executor, which imports
#: the whole send path, to decide a SENTENCE -- and this reader only ever chooses which of two
#: wordings the operator sees, where that one prices the rolling delete cap. They are named
#: for each other here so a third kind reaches both (rule 72); nothing else keeps them in step,
#: and a miss costs the more specific of two true sentences, never a file.
_REMOVING_KINDS = frozenset({"radarr_delete", "sonarr_delete_files"})


@dataclass(frozen=True, slots=True)
class Seen:
    """One id's row, as the scan reads it."""

    rating_keys: frozenset[int]
    last_seen_at: datetime
    returned_at: datetime | None
    returned_by_reaper: bool | None


@dataclass(frozen=True, slots=True)
class Sighting:
    """One item, bound to Plex on this scan. Built only for a confident bind."""

    id_key: str
    rating_key: int
    added_at: datetime | None
    """The Plex listing's own arrival date, which is when the returning copy landed. ``None``
    where Plex reports none, and then condition 3 has no clock and no return is found."""


def id_key(
    *,
    media_type: str,
    tmdb: int | None = None,
    imdb: str | None = None,
    tvdb: int | None = None,
    season: int | None = None,
) -> str | None:
    """This item's ledger key: ``movie:tmdb:12345``, ``tv:tvdb:678``, ``tv:tvdb:678:s3``.

    ``None`` when the item carries no id the ladder accepts, which is a stated limit rather
    than a bug: a title with no external id cannot be tracked across a delete and a re-add by
    any id-matched feature in the tree. It resolves ``Unknown`` and takes no hold.

    **The media kind leads, because a bare tmdb id is not a stable key across kinds** (rule
    52): movie and TV tmdb ids share one integer space, so ``tmdb:12345`` can name two
    different titles.

    The ladders are the ones the Plex resolver already binds on
    (``identity.MOVIE_ID_PRIORITY``, ``identity.SHOW_ID_PRIORITY``), so an item's ledger key
    and its bind rest on the same id. Restated rather than imported because those tuples drive
    ``resolve``'s narrowing over a ``PlexIndex`` and this needs one winner, not an ordered
    walk; ``tests/test_library_seen.py`` pins the two orders against each other.

    An item whose available ids CHANGE between scans gets a new key and simply starts over,
    with no return found. That is the safe direction and the only one available: nothing can
    tell a title that gained a tmdb id from a different title.
    """
    if media_type == "movie":
        for prefix, value in (("tmdb", tmdb), ("imdb", imdb)):
            if value is not None:
                return f"movie:{prefix}:{value}"
        return None
    for prefix, value in (("tvdb", tvdb), ("imdb", imdb)):
        if value is not None:
            base = f"tv:{prefix}:{value}"
            return base if season is None else f"{base}:s{season}"
    return None


async def recall_all(session: AsyncSession) -> dict[str, Seen]:
    """The whole ledger, keyed by ``id_key``.

    The whole table for ``watch_evidence.recall_all``'s reason: the TV lane needs its rows
    before it has resolved a single season, inside a concurrent task holding no session of its
    own, so there is nothing to filter on yet. One row per title ever bound makes this
    library-sized, and it binds no variables, so it cannot meet SQLite's ceiling (rule 94).

    A ``rating_keys_json`` that will not parse reads as an EMPTY set, which costs a detection
    and can never invent one: with no recorded key, condition 1 holds trivially but condition 2
    has nothing to have gone missing, and :func:`is_return` requires at least one recorded key.
    """
    rows = (await session.execute(select(LibrarySeen))).scalars()
    return {
        row.id_key: Seen(
            rating_keys=_read_keys(row.rating_keys_json, row.id_key),
            last_seen_at=row.last_seen_at,
            returned_at=row.returned_at,
            returned_by_reaper=row.returned_by_reaper,
        )
        for row in rows
    }


def _read_keys(raw: str | None, id_key_for_log: str) -> frozenset[int]:
    try:
        decoded = json.loads(raw or "[]")
        return frozenset(int(k) for k in decoded)
    except (ValueError, TypeError):
        log.warning("library_seen.keys_unreadable", id_key=id_key_for_log)
        return frozenset()


def scans_inside(instants: Sequence[datetime], start: datetime, end: datetime) -> int:
    """How many scans ran strictly between two instants.

    ``instants`` is every ``Snapshot.created_at``, sorted, read once per scan. Nothing prunes
    snapshots, so this reaches back as far as the install does. Where a retention policy ever
    does prune them, the count falls and a return goes undetected, which withholds a
    protection rather than granting one falsely.

    Strictly between, both ends open: the scan at ``start`` is the one that last saw the
    title, and one simultaneous with the copy's arrival did not run while it was missing.
    """
    return bisect_left(instants, end) - bisect_right(instants, start)


def is_return(
    seen: Seen,
    sighting: Sighting,
    *,
    live_keys: Container[int],
    scan_instants: Sequence[datetime],
    cooling_off_days: int,
    now: datetime,
) -> bool:
    """Whether this sighting is a title coming back. The four conditions, in order.

    ``live_keys`` is what Plex still lists, and each lane supplies its own: the movie lane the
    whole ``PlexIndex``, the season lane its show's own season keys. A season's earlier keys
    can only ever have been seasons of that show, so the narrower set answers the same
    question at a fraction of the size.

    Pure. Every input is already in hand when the item is judged, so this runs inside the
    per-item loop with no I/O, exactly as ``watch_evidence.went_blind`` does.
    """
    if not seen.rating_keys:
        # Nothing recorded to have moved. The row was written this scan, or its stored keys
        # were unreadable; either way there is no earlier key whose absence could be checked.
        return False
    if sighting.rating_key in seen.rating_keys:
        return False
    if any(key in live_keys for key in seen.rating_keys):
        return False
    if sighting.added_at is None:
        return False
    # Clamped, because an arrival date in the future is not evidence of a longer absence. A
    # clock ahead of Reaper's would otherwise widen every gap it touches.
    arrived = min(sighting.added_at, now)
    gap = arrived - seen.last_seen_at
    if gap.total_seconds() < cooling_off_days * 86_400:
        return False
    return scans_inside(scan_instants, seen.last_seen_at, arrived) >= _SCANS_INSIDE_THE_ABSENCE


def observations(
    seen: Seen | None, *, now: datetime
) -> tuple[Observation[float], Observation[bool]]:
    """``Facts.returned_days_ago`` and ``Facts.returned_by_reaper``, from one ledger row.

    The one derivation, called by both fact builders, so the two lanes cannot disagree about
    what a missing row means (rules 35, 104). ``seen`` is ``None`` for a title with no external
    id and for one Reaper has never bound before, and both are ``Unknown`` -- there was nothing
    to look up, which is not the same as looking and finding no return (rule 93).

    Elapsed days are floored by ``dormancy_days``, which leaves more of the hold standing: the
    bound that produces less deletion pressure (rule 31).
    """
    if seen is None:
        return (
            Unknown(reason=NO_RETURN_RECORD_REASON, source="reaper"),
            Unknown(reason=NO_RETURN_RECORD_REASON, source="reaper"),
        )
    if seen.returned_at is None:
        return Absent(source="reaper"), Absent(source="reaper")
    return (
        Known(value=float(dormancy_days(seen.returned_at, now=now)), source="reaper"),
        Known(value=bool(seen.returned_by_reaper), source="reaper"),
    )


def within_cap(returns: int, bound: int) -> bool:
    """Whether this scan's crop of returns is small enough to believe.

    Above :data:`_CAP_APPLIES_ABOVE` items a share is meaningful and
    :data:`RETURN_POPULATION_CAP` applies; below it every detection stands, because on a small
    library one real return is already a large fraction of it.
    """
    if bound <= _CAP_APPLIES_ABOVE:
        return True
    return returns <= bound * RETURN_POPULATION_CAP


async def removed_by_reaper(session: AsyncSession, id_keys: Iterable[str]) -> set[str]:
    """Which of these ids Reaper's own journal says it removed the files of.

    One query for the whole scan, run only when something looked returned, because the answer
    is a property of the journal rather than of the item and the journal is small: retention
    pins every snapshot a run points at, so the rows behind a deletion outlive everything else.

    A step counts when its file is confirmed gone, which is ``VERIFIED`` **or** a durable
    ``file_removed_at`` -- rule 97's pair. A step whose file went but whose exclusion failed
    stays ``FAILED`` and still removed the file, and claiming Reaper did not is the wrong way
    for this sentence to be wrong: it would tell an operator their own settings are innocent.

    ``False`` for an id absent from the result is a real answer, not a gap: it means Reaper has
    no record of removing it, which is exactly what the second sentence says.
    """
    wanted = set(id_keys)
    if not wanted:
        return set()
    rows = await session.execute(
        select(
            Candidate.media_type,
            Candidate.media_key,
            Candidate.tmdb_id,
            Candidate.imdb_id,
            Candidate.tvdb_id,
        )
        .join(ReapRun, ReapRun.snapshot_id == Candidate.snapshot_id)
        .join(
            ActionStep,
            (ActionStep.run_id == ReapRun.id) & (ActionStep.media_key == Candidate.media_key),
        )
        .where(
            ActionStep.kind.in_(_REMOVING_KINDS),
            (ActionStep.state == StepState.VERIFIED) | (ActionStep.file_removed_at.is_not(None)),
        )
        .distinct()
    )
    removed: set[str] = set()
    for media_type, media_key, tmdb, imdb, tvdb in rows:
        key = id_key(
            media_type=media_type,
            tmdb=tmdb,
            imdb=imdb,
            tvdb=tvdb,
            season=_season_of(media_key) if media_type == "season" else None,
        )
        if key is not None and key in wanted:
            removed.add(key)
    return removed


def _season_of(media_key: str) -> int | None:
    """The season number out of ``sonarr:1:42:3`` (``season_scan.season_media_key``'s form).

    Read off the key rather than stored, because ``Candidate`` carries no season number of its
    own. An unparseable key yields ``None``, which drops the row from the journal answer and
    costs the operator the more specific of two sentences, never the hold.
    """
    parts = media_key.split(":")
    if len(parts) != 4:
        return None
    try:
        return int(parts[3])
    except ValueError:
        return None


async def scan_instants(session: AsyncSession) -> list[datetime]:
    """Every scan's start, sorted -- the input to condition 4. One read per scan."""
    rows = (await session.execute(select(Snapshot.created_at).order_by(Snapshot.created_at))).all()
    return [row[0] for row in rows]


def note_sighting(batch: dict[str, set[int]], sighting: Sighting) -> None:
    """Fold one item's Plex key into this scan's batch, for :func:`record` to write.

    **A set per id, not one key per id, and that is the measured requirement rather than
    caution.** One external id routinely carries TWO \\*arr entries, one per copy, each bound
    to a different Plex listing (``docs/LEARNINGS.md``, assumption 16). A batch keyed one
    ``Sighting`` per id drops whichever copy the scan judged first, so the ledger never
    records that copy's key at all -- and a key that was never recorded cannot later be
    noticed as gone, which is the coverage this feature exists to have for exactly that
    population. That finding was already paid for once, in the stored row; this is the same
    failure one layer up, in the batching.

    One helper, called by both lanes, so neither can be the one that overwrites (rule 72).
    """
    batch.setdefault(sighting.id_key, set()).add(sighting.rating_key)


async def record(
    session: AsyncSession,
    keys_seen: Mapping[str, set[int]],
    *,
    returns: Mapping[str, bool],
    now: datetime,
) -> None:
    """Write this scan's sightings, and stamp the returns the cap let through.

    ``keys_seen`` is every Plex key this scan bound to each id, built by :func:`note_sighting`.

    ``returns`` maps an id key to whether Reaper's journal claims the removal. A key absent
    from it is an ordinary sighting and leaves any stored ``returned_at`` exactly as it was:
    the hold is a durable fact about the title, and a later scan that merely sees it again
    must not clear it.

    The key set only ever grows, and the merge happens here rather than in SQL because SQLite
    has no set type and the union is over a handful of ints on a row already in hand. The
    trade is that two concurrent scans could each write a union missing the other's key; only
    one scan runs at a time, and the cost of losing a key is a detection, never a false one.
    """
    if not keys_seen:
        return
    # Re-read inside the write rather than trusting the map the scan loaded at its start
    # (rule 58), and chunked on ``db.KEY_CHUNK`` because this list is the whole bound library
    # (rule 94).
    wanted = list(keys_seen)
    stored: dict[str, LibrarySeen] = {}
    for start in range(0, len(wanted), KEY_CHUNK):
        found = await session.execute(
            select(LibrarySeen).where(LibrarySeen.id_key.in_(wanted[start : start + KEY_CHUNK]))
        )
        stored.update({row.id_key: row for row in found.scalars().all()})
    rows = []
    for key, fresh in keys_seen.items():
        previous = stored.get(key)
        keys = _read_keys(previous.rating_keys_json, key) if previous else frozenset()
        returned_at = previous.returned_at if previous else None
        returned_by = previous.returned_by_reaper if previous else None
        if key in returns:
            returned_at = now
            returned_by = returns[key]
        rows.append(
            {
                "id_key": key,
                "rating_keys_json": json.dumps(sorted(keys | fresh)),
                "first_seen_at": previous.first_seen_at if previous else now,
                "last_seen_at": now,
                "returned_at": returned_at,
                "returned_by_reaper": returned_by,
            }
        )
    for start in range(0, len(rows), _CHUNK):
        chunk = rows[start : start + _CHUNK]
        stmt = sqlite_insert(LibrarySeen).values(chunk)
        await session.execute(
            stmt.on_conflict_do_update(
                index_elements=[LibrarySeen.id_key],
                set_={
                    "rating_keys_json": stmt.excluded.rating_keys_json,
                    "last_seen_at": stmt.excluded.last_seen_at,
                    "returned_at": stmt.excluded.returned_at,
                    "returned_by_reaper": stmt.excluded.returned_by_reaper,
                },
            )
        )
