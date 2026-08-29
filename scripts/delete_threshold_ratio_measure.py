#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Replay real watch history and measure the delete-threshold ratio-to-score curve.

The delete threshold is a score out of 100. A planned feature will let the operator say
"one mistake per N cleared" and translate that ratio into a score. This script produces
the evidence for that: for each candidate score threshold, how many titles the current
policy would have flagged at a real cutoff, and how many of those were actually played
again within the following year (the "mistakes").

This script only prints ratios, counts, and spans, for either input shape. It never
prints a title, path, id, or username.

    uv run python scripts/delete_threshold_ratio_measure.py data/
    uv run python scripts/delete_threshold_ratio_measure.py reaper-dump.json.gz

Two input shapes, auto-detected:

* a **Reaper data directory** holding ``reaper.db`` and ``cache.db``. The watch history
  mirror (table ``watch_event``) lives in ``cache.db`` (see ``services/history_sync.py``);
* an **anonymized Tautulli dump**, the gzipped-or-plain JSON file
  ``scripts/tautulli_anon_dump.py`` writes. ``reference_now`` from the dump is always the
  clock, because its timestamps are shifted for anonymity and the wall clock would
  compute a cutoff that does not match them.

## What one cutoff means

One wall-clock instant is picked for the whole server: ``cutoff = now - 365 days`` (the
1-year default; ``--cutoff-days`` overrides it). Every title is scored as the policy
would have scored it at the cutoff, using only history at or before it. A play that
happens after the cutoff, but within a year of it, counts as a "mistake": the policy
would have deleted a title someone came back for. Every title uses this one shared
cutoff, including its own last play, which is what stops a later play from leaking into
an earlier score (docs/SIGNALS.md covers the same discipline for a baseline).

``now`` always comes from the data, never the wall clock. The dump path reads
``reference_now`` (shifted along with every other timestamp in the file, so the two
stay comparable). The data-dir path takes the newest ``watch_event`` row instead of
asking the OS. Both choices keep two runs over the same input printing identical
output.

## The approximation, spelled out once

Full policy scoring needs Sonarr/Radarr and Plex evidence that a pure history replay
does not have (current ratings, live watcher windows, season structure). This script
approximates it with the dormancy-driven core instead, imported from ``reaper.engine``
rather than reimplemented: the shipped ``UNWATCHED`` signal alone (``floor=365``,
``saturate=1825``, the same ramp both default policies ship) as the score, plus the
``MIN_DORMANCY`` gate (1,095 days) as the one hard floor. ``docs/SIGNALS.md`` found that
dormancy alone scores as well as, or better than, the full default signal set, so this
is the same approximation the app's own backtest relies on, not a new one. It drops
``FEW_WATCHERS``, ``LOW_RATING``, ``SEASON_RANK``, ``RATING_FLOOR``,
``SERVER_POPULARITY``, ``REWATCH_ODDS``, ``STREAMING_NOW``, ``DATA_HORIZON``, and the
coverage floor. Every run prints this paragraph's claim as one header line, so nobody
mistakes the curve for a full policy replay.

## The two lanes

Every title lands in exactly one lane, and the two lanes are always reported
separately. Combining them into one number would improve it for the wrong reasons, the
same trap a pooled baseline falls into.

* **played before the cutoff** -- dormancy is days since that last play. Measurable
  from watch history alone, for both input shapes.
* **never played before the cutoff** -- dormancy is days since the title arrived. The
  Tautulli dump carries an arrival date per item, so this lane is fully measurable
  there. ``reaper.db`` does not keep one (``Facts`` derives dormancy at scan time and
  never stores the raw arrival date, see ``engine/dormancy.py``), so for the data-dir
  input, this lane's population is counted and its curve is printed as unmeasured
  rather than left out.

## Qualification

Before trusting a resolved ratio, the script checks three things: enough history
before the cutoff to judge dormancy at all (at least one more year, matching the
cutoff rule above); enough titles in a lane to make its rate meaningful
(``REWATCH_BLOCK_FLOOR_N``, imported rather than reinvented, the same floor the app's
own rewatch-probability fit uses for a cohort); and a defensible bound on any row with
zero mistakes observed, via ``gates.wilson_upper``'s 95% upper bound on ``k/n``, so a
row with zero mistakes is never read as "infinitely good."
"""

from __future__ import annotations

import argparse
import gzip
import json
import sqlite3
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from reaper.engine.gates import (
    PROTECT,
    Facts,
    GateConfig,
    MinDormancyGate,
    wilson_upper,
)
from reaper.engine.gates import REWATCH_BLOCK_FLOOR_N as MIN_COHORT_N
from reaper.engine.observation import Known, Unknown
from reaper.engine.signals import SignalConfig, SignalId
from reaper.engine.signals import score as engine_score
from reaper.services.rewatch import RewatchOutcome, training_pair

#: The shipped default for both media types (``engine/policy.py``,
#: ``DEFAULT_MOVIE_POLICY`` / ``DEFAULT_TV_POLICY``), the ``UNWATCHED`` signal's ramp.
UNWATCHED_FLOOR_DAYS = 365
UNWATCHED_SATURATE_DAYS = 1825

#: ``GateId.MIN_DORMANCY``'s shipped threshold, both policies (``engine/policy.py``).
MIN_DORMANCY_GATE_DAYS = 1095

#: The score thresholds the curve is measured at.
THRESHOLDS: range = range(60, 96)

#: How far past the cutoff a play counts as a "mistake". This is the planned
#: delete-threshold feature's own window, and the training window
#: ``services.rewatch``'s Stage 2 fit was validated over.
OUTCOME_WINDOW_DAYS = 365

#: How much history before the cutoff is needed to trust a dormancy reading there
#: (see "What one cutoff means" above).
MIN_HISTORY_BEFORE_CUTOFF_DAYS = 365

_SOURCE = "delete_threshold_ratio_measure"

DAY_SECONDS = 86_400

#: The two ``watch_event`` sweeps ``load_datadir`` runs, as fixed literals never built
#: from a parameter. A movie is keyed on its own rating key, a season on its episodes'
#: ``parent_rating_key`` (the season's own key, matching ``Candidate.plex_rating_key``
#: for a season row).
_MOVIE_SWEEP_SQL = (
    "SELECT rating_key, watched_at FROM watch_event WHERE media_type = ? AND rating_key IS NOT NULL"
)
_SEASON_SWEEP_SQL = (
    "SELECT parent_rating_key, watched_at FROM watch_event "
    "WHERE media_type = ? AND parent_rating_key IS NOT NULL"
)


# --------------------------------------------------------------------------- scoring


def _minimal_facts(days: float) -> Facts:
    """Return a ``Facts`` object carrying only dormancy. Every other field is ``Unknown``.

    This script never looked at those other fields, so ``Unknown`` is the honest state
    to use, not ``Absent``. ``Unknown`` is also the one state that can never condemn a
    title (``engine/observation.py``). That is safe here because only the ``UNWATCHED``
    signal and the ``MIN_DORMANCY`` gate ever read this object, and neither reads any
    other field.
    """
    unreadable = Unknown(reason="not_replayed", source=_SOURCE)
    return Facts(
        title="",
        days_observed_unwatched=Known(value=days, source=_SOURCE),
        distinct_watchers=unreadable,
        distinct_watchers_all_time=unreadable,
        size_bytes=unreadable,
        imdb_rating_tenths=unreadable,
        imdb_votes=unreadable,
        season_rank=unreadable,
        is_streaming_now=unreadable,
        is_managed=unreadable,
        in_curated_list=unreadable,
        is_whitelisted=unreadable,
    )


def unwatched_score(days: float) -> float:
    """Score 0-100 from the shipped ``UNWATCHED`` signal alone, via the engine's ``score()``.

    The weight passed in does not affect the result, since it is the only signal in the
    denominator and cancels out. It is set to the shipped value of 70 only so a reader
    cross-checking ``engine/policy.py`` sees the same number.
    """
    result = engine_score(
        [
            SignalConfig(
                signal=SignalId.UNWATCHED,
                weight=70,
                saturate_at=UNWATCHED_SATURATE_DAYS,
                floor=UNWATCHED_FLOOR_DAYS,
            )
        ],
        _minimal_facts(days),
    )
    return result.value


def min_dormancy_protects(days: float) -> bool:
    """Return whether the real ``MinDormancyGate``, at its shipped 1,095-day threshold,
    protects this title, regardless of what the score threshold asks for."""
    result = MinDormancyGate(config=GateConfig(threshold=MIN_DORMANCY_GATE_DAYS)).evaluate(
        _minimal_facts(days)
    )
    return result.outcome == PROTECT


# --------------------------------------------------------------------------- the curve


def build_curve(pairs: list[tuple[float, bool]]) -> list[tuple[int, int, int]]:
    """Return one row per threshold: ``(threshold, flagged, mistakes)``.

    Pure and small enough to unit test directly. See ``tests/test_delete_threshold_ratio.py``.
    """
    scored = [
        (unwatched_score(days), min_dormancy_protects(days), watched_again)
        for days, watched_again in pairs
    ]
    rows = []
    for threshold in THRESHOLDS:
        flagged_watched = [
            watched_again
            for score, protected, watched_again in scored
            if not protected and score >= threshold
        ]
        rows.append((threshold, len(flagged_watched), sum(flagged_watched)))
    return rows


def _ratio(part: int, whole: int) -> str:
    """Format ``part`` of ``whole`` as "1 in N", matching ``return_signal_measure.py``."""
    if not whole or not part:
        return f"{part} of {whole:,}"
    return f"{part} of {whole:,}, about 1 in {round(whole / part):,}"


def ratio_text(flagged: int, mistakes: int) -> str:
    """Return the good-deletions-per-mistake ratio for one threshold row.

    A row with zero mistakes never prints as "infinitely good". ``wilson_upper`` gives
    the 95% upper bound on the true mistake rate when zero events were observed,
    converted into the same "1 mistake per N cleared" shape the non-zero rows use, so
    every row is comparable.
    """
    if flagged == 0:
        return "no titles flagged"
    if mistakes == 0:
        bound = wilson_upper(0, flagged)
        if bound <= 0:
            return "0 mistakes observed"
        return (
            f"0 mistakes observed; at least 1 in {int(1 / bound):,} if it happens at all"
            " (Wilson 95% bound)"
        )
    good = flagged - mistakes
    return f"1 mistake per {flagged / mistakes:,.0f} cleared ({good:,} good, {mistakes:,} mistakes)"


# --------------------------------------------------------------------------- inputs


@dataclass(frozen=True, slots=True)
class Replay:
    """One server's replay inputs, already split into the two lanes."""

    now_epoch: int
    cutoff_epoch: int
    earliest_epoch: int | None
    """The earliest watch history held, or ``None`` when there is none at all."""
    lane_a: list[tuple[float, bool]]
    """Played before the cutoff: (dormancy_days_at_cutoff, watched_again)."""
    lane_b: list[tuple[float, bool]] | None
    """Never played before the cutoff, same shape. ``None`` when this input shape
    cannot measure it (the data-dir path has no arrival date)."""
    lane_b_population: int
    """Titles in lane b, whether or not ``lane_b`` itself could be measured."""
    withheld: int = 0
    """Titles seen but excluded from both lanes, either not present at the cutoff
    (they arrived after it) or with no honest anchor at all."""


def _dt(epoch: int) -> datetime:
    return datetime.fromtimestamp(epoch, tz=UTC)


def _dormancy_days(reference_epoch: int, cutoff_epoch: int) -> int:
    return (_dt(cutoff_epoch) - _dt(reference_epoch)).days


def title_pair(
    token: str,
    plays_by_token: dict[str, list[int]],
    added_at_epoch: int | None,
    *,
    cutoff_epoch: int,
    window_end_epoch: int,
    earliest_epoch: int | None = None,
) -> tuple[tuple[float, bool] | None, bool]:
    """Return one title's ``(dormancy_days_at_cutoff, watched_again)`` training pair via
    the real ``rewatch.training_pair``, plus whether it landed in lane A (played before
    the cutoff). Returns ``pair=None`` when the title is withheld: not present at the
    cutoff, or with no honest anchor on either side (``training_pair``'s own contract).

    ``earliest_epoch`` is the start of the watch history held. A never-played title's
    anchor clamps to it, the same way the live engine's ``dormancy.reference_instant``
    clamps to the mirror's horizon. Without that clamp, an arrival date older than the
    history could read as dormancy nobody actually measured.

    Kept separate from :func:`load_dump` so the cutoff and leak logic can be tested
    without a real dump file (``tests/test_delete_threshold_ratio_measure.py``).
    """
    if added_at_epoch is not None and earliest_epoch is not None:
        added_at_epoch = max(added_at_epoch, earliest_epoch)
    times = plays_by_token.get(token, [])
    before = [t for t in times if t <= cutoff_epoch]
    after = [t for t in times if cutoff_epoch < t <= window_end_epoch]
    last_before = max(before) if before else None
    watched_again = bool(after)
    outcome = (
        None
        if last_before is None and not watched_again
        else RewatchOutcome(
            last_play_at_or_before_cutoff=_dt(last_before) if last_before is not None else None,
            watched_again=watched_again,
        )
    )
    added_at = _dt(added_at_epoch) if added_at_epoch is not None else None
    pair = training_pair(outcome, added_at=added_at, cutoff=_dt(cutoff_epoch))
    return pair, last_before is not None


# --- Reaper data directory --------------------------------------------------------


def load_datadir(path: Path, cutoff_days: int) -> Replay:
    """Read ``reaper.db`` (candidates) and ``cache.db`` (``watch_event``), both read-only.

    Lane A's dormancy comes straight off ``watch_event``, so it needs no arrival date.
    Lane B would need one, and ``Facts`` never persists it (``engine/dormancy.py``), so
    for this input shape lane B is counted but never scored.
    """
    rdb = sqlite3.connect(f"file:{path / 'reaper.db'}?mode=ro", uri=True)
    cdb = sqlite3.connect(f"file:{path / 'cache.db'}?mode=ro", uri=True)
    try:
        now_epoch = cdb.execute("SELECT MAX(watched_at) FROM watch_event").fetchone()[0]
        if now_epoch is None:
            sys.exit("cache.db holds no watch history at all; nothing to measure")
        earliest_epoch = cdb.execute("SELECT MIN(watched_at) FROM watch_event").fetchone()[0]
        cutoff_epoch = now_epoch - cutoff_days * DAY_SECONDS
        window_end_epoch = cutoff_epoch + OUTCOME_WINDOW_DAYS * DAY_SECONDS

        snap = rdb.execute("SELECT id FROM snapshot ORDER BY created_at DESC LIMIT 1").fetchone()
        if snap is None:
            sys.exit("no snapshot in reaper.db; run at least one scan first")
        (snap_id,) = snap

        candidates = rdb.execute(
            "SELECT media_type, plex_rating_key FROM candidate "
            "WHERE snapshot_id = ? AND plex_rating_key IS NOT NULL",
            (snap_id,),
        ).fetchall()

        def _sweep(query: str, media_type: str) -> tuple[dict[int, int], set[int]]:
            """Return every rating key's last play at-or-before the cutoff, and the set
            played again within the following year. One pass over ``watch_event``, no
            ``IN`` clause, since this is an offline batch read rather than a
            scan-sized live query. ``query`` is always one of the two literals above,
            never built from a parameter, so this never interpolates SQL, even for a
            read-only query."""
            last_before: dict[int, int] = {}
            watched_after: set[int] = set()
            for raw_key, watched_at in cdb.execute(query, (media_type,)):
                key = int(raw_key)
                if watched_at <= cutoff_epoch:
                    if key not in last_before or watched_at > last_before[key]:
                        last_before[key] = watched_at
                elif watched_at <= window_end_epoch:
                    watched_after.add(key)
            return last_before, watched_after

        movie_last_before, movie_watched_after = _sweep(_MOVIE_SWEEP_SQL, "movie")
        season_last_before, season_watched_after = _sweep(_SEASON_SWEEP_SQL, "episode")

        lane_a: list[tuple[float, bool]] = []
        lane_b_population = 0
        for media_type, plex_rating_key in candidates:
            key = int(plex_rating_key)
            if media_type == "movie":
                last_before, watched_after = movie_last_before, movie_watched_after
            elif media_type == "season":
                last_before, watched_after = season_last_before, season_watched_after
            else:
                continue
            if key in last_before:
                days = _dormancy_days(last_before[key], cutoff_epoch)
                lane_a.append((float(days), key in watched_after))
            else:
                lane_b_population += 1

        return Replay(
            now_epoch=now_epoch,
            cutoff_epoch=cutoff_epoch,
            earliest_epoch=earliest_epoch,
            lane_a=lane_a,
            lane_b=None,
            lane_b_population=lane_b_population,
        )
    finally:
        rdb.close()
        cdb.close()


# --- Tautulli dump -----------------------------------------------------------------


def _load_dump(path: Path) -> dict[str, Any]:
    raw = path.read_bytes()
    if raw[:2] == b"\x1f\x8b":
        raw = gzip.decompress(raw)
    data: dict[str, Any] = json.loads(raw)
    return data


def load_dump(path: Path, cutoff_days: int) -> Replay:
    """Read a ``scripts/tautulli_anon_dump.py`` dump. ``reference_now`` is the clock.

    The dump carries an arrival date per item, so both lanes are measurable here,
    unlike the data-dir path.
    """
    data = _load_dump(path)
    now_epoch = int(data["reference_now"])
    cutoff_epoch = now_epoch - cutoff_days * DAY_SECONDS
    window_end_epoch = cutoff_epoch + OUTCOME_WINDOW_DAYS * DAY_SECONDS
    earliest_epoch = data.get("history_begins_at")

    by_movie: dict[str, list[int]] = {}
    by_season: dict[str, list[int]] = {}
    for play in data.get("plays") or []:
        at = play.get("at")
        if at is None:
            continue
        if play.get("type") == "movie":
            by_movie.setdefault(play["item"], []).append(int(at))
        elif play.get("type") == "episode" and play.get("season"):
            by_season.setdefault(play["season"], []).append(int(at))

    lane_a: list[tuple[float, bool]] = []
    lane_b: list[tuple[float, bool]] = []
    withheld = 0

    def _place(
        token: str, plays_by_token: dict[str, list[int]], added_at_epoch: int | None
    ) -> None:
        nonlocal withheld
        pair, in_lane_a = title_pair(
            token,
            plays_by_token,
            added_at_epoch,
            cutoff_epoch=cutoff_epoch,
            window_end_epoch=window_end_epoch,
            earliest_epoch=earliest_epoch,
        )
        if pair is None:
            withheld += 1
        elif in_lane_a:
            lane_a.append(pair)
        else:
            lane_b.append(pair)

    for item in data.get("items") or []:
        if item.get("type") == "movie":
            _place(item["token"], by_movie, item.get("added_at"))

    for show in data.get("seasons") or []:
        for season in show.get("seasons") or []:
            _place(season["token"], by_season, season.get("added_at"))

    return Replay(
        now_epoch=now_epoch,
        cutoff_epoch=cutoff_epoch,
        earliest_epoch=earliest_epoch,
        lane_a=lane_a,
        lane_b=lane_b,
        lane_b_population=len(lane_b),
        withheld=withheld,
    )


# --------------------------------------------------------------------------- reporting


def _fmt(epoch: int | None) -> str:
    return _dt(epoch).date().isoformat() if epoch is not None else "unknown"


def print_qualification(replay: Replay, cutoff_days: int) -> None:
    print("\nQUALIFICATION")
    if replay.earliest_epoch is None:
        print("  history before cutoff: none -- no watch history in this input at all")
        history_ok = False
    else:
        history_days = (replay.cutoff_epoch - replay.earliest_epoch) / DAY_SECONDS
        history_ok = history_days >= MIN_HISTORY_BEFORE_CUTOFF_DAYS
        print(
            f"  history before cutoff: {history_days:,.0f} days"
            f" (need >= {MIN_HISTORY_BEFORE_CUTOFF_DAYS}) -- {'OK' if history_ok else 'SHORT'}"
        )

    lane_a_ok = len(replay.lane_a) >= MIN_COHORT_N
    print(
        f"  lane A cohort: {len(replay.lane_a):,} titles"
        f" (need >= {MIN_COHORT_N}) -- {'OK' if lane_a_ok else 'THIN'}"
    )

    if replay.lane_b is None:
        print(f"  lane B cohort: {replay.lane_b_population:,} titles, not scored (see header)")
        lane_b_ok: bool | None = None
    else:
        lane_b_ok = len(replay.lane_b) >= MIN_COHORT_N
        print(
            f"  lane B cohort: {len(replay.lane_b):,} titles"
            f" (need >= {MIN_COHORT_N}) -- {'OK' if lane_b_ok else 'THIN'}"
        )
    if replay.withheld:
        print(
            f"  {replay.withheld:,} titles excluded: not present at the cutoff, or no honest anchor"
        )

    print(
        "  a threshold row with zero mistakes observed is reported as a Wilson 95% upper"
        " bound, never as a bare zero"
    )

    qualifies = history_ok and lane_a_ok
    verdict = "qualifies" if qualifies else "does NOT qualify"
    print(f"  VERDICT: {verdict} to resolve a ratio into a score")
    if not qualifies:
        missing = []
        if not history_ok:
            missing.append(
                f"needs >= {MIN_HISTORY_BEFORE_CUTOFF_DAYS} days of history before the cutoff"
            )
        if not lane_a_ok:
            missing.append(f"needs >= {MIN_COHORT_N} titles played at least once before the cutoff")
        print(f"    missing: {'; '.join(missing)}")


def print_lane(
    title: str, pairs: list[tuple[float, bool]] | None, unmeasured_note: str | None
) -> None:
    print(f"\n{title}")
    if pairs is None:
        print(f"  {unmeasured_note}")
        return
    n = len(pairs)
    mistakes_ever = sum(1 for _, watched_again in pairs if watched_again)
    print(f"  {n:,} titles, {_ratio(mistakes_ever, n)} watched again within the following year")
    if n == 0:
        return
    print(f"  {'threshold':>9}  {'flagged':>9}  {'mistakes':>9}  ratio")
    for threshold, flagged, mistakes in build_curve(pairs):
        print(f"  {threshold:>9}  {flagged:>9,}  {mistakes:>9,}  {ratio_text(flagged, mistakes)}")


def run(replay: Replay, *, kind: str, cutoff_days: int) -> None:
    print("Reaper delete-threshold ratio measurement")
    # Prints the kind only. The path the operator typed never appears in shareable
    # output.
    print(f"input: {kind}")
    now_note = (
        "the dump's own reference clock, shifted for anonymity, never this machine's"
        if kind == "Tautulli dump"
        else "from the newest event in the data, never the wall clock"
    )
    print(f"now: {_fmt(replay.now_epoch)} ({now_note})")
    print(f"cutoff: {_fmt(replay.cutoff_epoch)} (now minus {cutoff_days} days)")
    print(
        "approximation: score = the shipped UNWATCHED signal alone (0-100, floor"
        f" {UNWATCHED_FLOOR_DAYS}d, saturate {UNWATCHED_SATURATE_DAYS}d) plus the MIN_DORMANCY"
        f" gate ({MIN_DORMANCY_GATE_DAYS}d). Drops FEW_WATCHERS, LOW_RATING, SEASON_RANK,"
        " RATING_FLOOR, SERVER_POPULARITY, REWATCH_ODDS, STREAMING_NOW, DATA_HORIZON and the"
        " coverage floor -- docs/SIGNALS.md found dormancy alone scores as well as the full"
        " default signal set."
    )

    print_qualification(replay, cutoff_days)

    print_lane("LANE A -- played at least once before the cutoff", replay.lane_a, None)
    if replay.lane_b is None:
        print_lane(
            "LANE B -- never played before the cutoff",
            None,
            f"{replay.lane_b_population:,} titles; reaper.db keeps no arrival date, so their"
            " dormancy at the cutoff cannot be reconstructed. Not included in the curve.",
        )
    else:
        print_lane("LANE B -- never played before the cutoff", replay.lane_b, None)
    print()


# --------------------------------------------------------------------------- entry point


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Replay real watch history and measure the delete-threshold ratio-to-score "
            "curve. Read-only; prints ratios, counts and spans only."
        ),
    )
    parser.add_argument(
        "input",
        type=Path,
        help="a Reaper data directory (reaper.db + cache.db), or a tautulli_anon_dump.py output",
    )
    parser.add_argument(
        "--cutoff-days",
        type=int,
        default=365,
        help="how many days before 'now' the cutoff sits (default: 365)",
    )
    args = parser.parse_args(argv)

    path: Path = args.input
    if args.cutoff_days <= 0:
        parser.error("--cutoff-days must be positive")

    if path.is_dir():
        if not (path / "reaper.db").exists() or not (path / "cache.db").exists():
            print(
                f"{path} is a directory but does not hold both reaper.db and cache.db",
                file=sys.stderr,
            )
            return 1
        replay = load_datadir(path, args.cutoff_days)
        run(replay, kind="Reaper data directory", cutoff_days=args.cutoff_days)
        return 0

    if not path.is_file():
        print(f"no such file or directory: {path}", file=sys.stderr)
        return 1

    replay = load_dump(path, args.cutoff_days)
    run(replay, kind="Tautulli dump", cutoff_days=args.cutoff_days)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
