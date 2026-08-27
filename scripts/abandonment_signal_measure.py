#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Measure whether an abandoned (partial) play predicts a title gets played again.

When someone starts a title and stops partway through, that could mean they plan to
finish it, or that they tried it and did not want it. This script checks which is true
more often, using real watch history, so the app can decide whether an abandoned-play
signal is worth adding. Finding no measurable difference either way still counts as a
successful measurement.

This script prints only ratios, counts, and spans. It never prints a title, path, id, or
username, except the input path the operator gives on the command line (the same rule
``delete_threshold_ratio_measure.py`` follows).

    uv run python scripts/abandonment_signal_measure.py data/
    uv run python scripts/abandonment_signal_measure.py reaper-dump.json.gz

## What "abandoned" means

A play counts as abandoned when ``qualifies()`` in ``services/rewatch.py`` would discard
it as partial. This script imports and calls that function directly instead of
reimplementing it, so it measures exactly the plays the rewatch engine discards.

``qualifies()`` checks, in order: a reported ``watched_status`` under 0.5 counts as
abandoned. With no status, a ``percent_complete`` under 50 counts as abandoned. A play
with no status and 0 percent complete still counts as a completed play, because an
unknown status favors keeping the title.

## The two lanes

* **ABANDONED** -- a title with at least one abandoned play and no completed
  (qualifying) play before the cutoff. A title with both an abandoned and a completed
  play before the cutoff has a mixed history, so it is excluded from every cohort
  instead of assigned to one.
* **CONTROL** -- the closest honest same-age comparison. Its shape depends on the input:
    * the anonymized dump carries an arrival date, so its control is titles with no
      plays at all before the cutoff, matched on the same dormancy-age band;
    * the Reaper data directory's ``reaper.db`` keeps no arrival date
      (``docs/LEARNINGS.md`` covers this limitation), so a never-played title cannot be
      anchored there. Its control is instead titles whose only pre-cutoff plays are
      completed, matched on the same bands.

Both lanes are compared within fixed dormancy-age bands, the same ``_BUCKET_EDGES``
``reaper.services.rewatch`` already uses for this kind of question, and pooled only over
bands with at least ``REWATCH_BLOCK_FLOOR_N`` titles on both sides, so a thin band
cannot swing the pooled result. Every rate is reported beside its Wilson 95% upper bound
(``gates.wilson_upper``), so a small cohort's zero is never read as "never happens".

## The cutoff

One wall-clock instant for the whole server, the same rule
``delete_threshold_ratio_measure.py`` uses: ``cutoff = now - 365 days``. ``now`` always
comes from the data, never the wall clock (the dump path reads ``reference_now``, the
data-dir path takes the newest ``watch_event`` row), so two runs over the same input
print identical output.

## Reusing the step-2 script

This script imports ``scripts/delete_threshold_ratio_measure.py`` for its dump-reading
(gzip-or-plain JSON) and its cutoff/window constants, instead of duplicating them. It
does not modify that file. Its two SQL sweeps read two extra columns
(``watched_status``, ``percent_complete``) that the other script's sweeps do not select,
since only this measurement needs them. The sweep shape, candidates from the latest
snapshot, one query per media kind, matches ``load_datadir``'s, noted at each call site
below.

## The stop gate

The pooled absolute lift (replay-probability difference, abandoned minus control)
decides the result: under 0.05, the abandonment signal is not built. Each run prints
one VERDICT line.
"""

from __future__ import annotations

import argparse
import itertools
import sqlite3
import sys
from dataclasses import dataclass
from pathlib import Path

import delete_threshold_ratio_measure as ratio_measure  # type: ignore[import-not-found]

from reaper.engine.gates import REWATCH_BLOCK_FLOOR_N, wilson_upper
from reaper.services.rewatch import _BUCKET_EDGES as DORMANCY_BAND_EDGES
from reaper.services.rewatch import qualifies

#: The pooled absolute lift below which the abandonment signal is not worth building.
LIFT_BAR = 0.05

#: Cohort labels. A title lands in exactly one of these, or is excluded (mixed history)
#: or withheld (no honest dormancy anchor).
ABANDONED = "abandoned"
CONTROL_COMPLETED = "completed_only"
CONTROL_NEVER_PLAYED = "never_played"

#: Maps one band's ``(lo_days, hi_days]`` key to per-cohort-label counts in it. Defined
#: before ``Cohort`` because a ``type`` alias evaluates its body lazily (PEP 695), so the
#: forward reference to ``Cohort`` resolves once the module finishes loading.
type BandTable = dict[tuple[float, float | None], dict[str, Cohort]]

#: The two live-pair SQL sweeps: copies of ``delete_threshold_ratio_measure.py``'s
#: ``_MOVIE_SWEEP_SQL`` and ``_SEASON_SWEEP_SQL``, extended with the two columns
#: ``qualifies()`` needs, which that script's own sweeps do not select.
_MOVIE_SWEEP_SQL = (
    "SELECT rating_key, watched_at, watched_status, percent_complete FROM watch_event "
    "WHERE media_type = ? AND rating_key IS NOT NULL"
)
_SEASON_SWEEP_SQL = (
    "SELECT parent_rating_key, watched_at, watched_status, percent_complete FROM watch_event "
    "WHERE media_type = ? AND parent_rating_key IS NOT NULL"
)


# --------------------------------------------------------------------------- pure logic


@dataclass(frozen=True, slots=True)
class Cohort:
    """One band's title count and how many of them were played again."""

    n: int = 0
    k: int = 0


def _rate(cohort: Cohort) -> float:
    return cohort.k / cohort.n if cohort.n else 0.0


def classify_title(
    *, has_abandoned: bool, has_qualified: bool, has_play_before: bool
) -> str | None:
    """Return which cohort a title's pre-cutoff plays place it in.

    Returns ``None`` when the title has both an abandoned and a completed play before
    the cutoff. That mixed history fits neither an abandoned-only nor a completed-only
    cohort, so it is excluded rather than assigned to one.

    ``has_play_before=False`` always returns :data:`CONTROL_NEVER_PLAYED`. The caller
    decides whether that label can be used: the live pair has no arrival date to anchor
    a never-played title, so it must be withheld there instead.
    """
    if not has_play_before:
        return CONTROL_NEVER_PLAYED
    if has_abandoned and not has_qualified:
        return ABANDONED
    if has_qualified and not has_abandoned:
        return CONTROL_COMPLETED
    return None


def band_for(days: float) -> tuple[float, float | None]:
    """Return which fixed dormancy-age band ``days`` falls in, over :data:`DORMANCY_BAND_EDGES`.

    Each band is half-open, ``(lo, hi]``, and closed at zero. ``reaper.services.rewatch``'s
    ``fit_blocks`` and ``block_for`` use the same rule on the same edges, so a title
    measured at exactly 0 days dormancy (played the day of the cutoff) lands in the
    first band instead of matching no band at all.
    """
    bounds = [*itertools.pairwise(DORMANCY_BAND_EDGES), (DORMANCY_BAND_EDGES[-1], None)]
    for lo, hi in bounds:
        if (days > lo or (lo == 0 and days == 0)) and (hi is None or days <= hi):
            return (lo, hi)
    raise AssertionError(f"unreachable: {days} days matched no band")  # pragma: no cover


def _add(
    bands: BandTable, band: tuple[float, float | None], label: str, watched_again: bool
) -> None:
    per_band = bands.setdefault(band, {})
    prev = per_band.get(label, Cohort())
    per_band[label] = Cohort(n=prev.n + 1, k=prev.k + (1 if watched_again else 0))


def pooled_lift(
    bands: BandTable, control_label: str, floor: int
) -> tuple[float | None, Cohort, Cohort, list[tuple[float, float | None]]]:
    """Return the pooled absolute lift (abandoned rate minus control rate).

    Pools every band with at least ``floor`` titles on both sides, and also returns the
    two pooled cohorts and which bands were included. Returns ``lift=None`` when no
    band qualifies, since there is nothing to pool.
    """
    included = [
        band
        for band, per_label in bands.items()
        if per_label.get(ABANDONED, Cohort()).n >= floor
        and per_label.get(control_label, Cohort()).n >= floor
    ]
    pooled_a = Cohort(
        n=sum(bands[b].get(ABANDONED, Cohort()).n for b in included),
        k=sum(bands[b].get(ABANDONED, Cohort()).k for b in included),
    )
    pooled_c = Cohort(
        n=sum(bands[b].get(control_label, Cohort()).n for b in included),
        k=sum(bands[b].get(control_label, Cohort()).k for b in included),
    )
    if pooled_a.n == 0 or pooled_c.n == 0:
        return None, pooled_a, pooled_c, included
    return _rate(pooled_a) - _rate(pooled_c), pooled_a, pooled_c, included


def verdict_line(lift: float | None) -> str:
    if lift is None:
        return "VERDICT: no band had enough titles on both sides to pool; signal not justified"
    if lift > 0:
        direction = "argues keep (abandoners return more often than the control)"
    elif lift < 0:
        direction = "argues delete (abandoners return less often than the control)"
    else:
        direction = "no direction (abandoners return exactly as often as the control)"
    justified = "signal justified" if abs(lift) >= LIFT_BAR else "signal not justified"
    return f"VERDICT: pooled lift {lift:+.3f}, {direction}, {justified} (bar {LIFT_BAR:.2f})"


# --------------------------------------------------------------------------- live pair


def load_datadir_observations(path: Path, cutoff_days: int) -> tuple[BandTable, dict[str, int]]:
    """Read ``reaper.db`` (candidates, for item context) and ``cache.db`` (``watch_event``),
    both read-only.

    Copies ``delete_threshold_ratio_measure.py``'s ``load_datadir`` snapshot/candidate
    read, extended to carry ``watched_status`` and ``percent_complete`` so
    :func:`classify_title` can apply ``qualifies()``.
    """
    rdb = sqlite3.connect(f"file:{path / 'reaper.db'}?mode=ro", uri=True)
    cdb = sqlite3.connect(f"file:{path / 'cache.db'}?mode=ro", uri=True)
    try:
        now_epoch = cdb.execute("SELECT MAX(watched_at) FROM watch_event").fetchone()[0]
        if now_epoch is None:
            sys.exit("cache.db holds no watch history at all; nothing to measure")
        cutoff_epoch = now_epoch - cutoff_days * ratio_measure.DAY_SECONDS
        window_end_epoch = (
            cutoff_epoch + ratio_measure.OUTCOME_WINDOW_DAYS * ratio_measure.DAY_SECONDS
        )

        snap = rdb.execute("SELECT id FROM snapshot ORDER BY created_at DESC LIMIT 1").fetchone()
        if snap is None:
            sys.exit("no snapshot in reaper.db; run at least one scan first")
        (snap_id,) = snap
        candidates = rdb.execute(
            "SELECT media_type, plex_rating_key FROM candidate "
            "WHERE snapshot_id = ? AND plex_rating_key IS NOT NULL",
            (snap_id,),
        ).fetchall()

        def _sweep(query: str, media_type: str) -> dict[int, list[tuple[int, float | None, int]]]:
            rows: dict[int, list[tuple[int, float | None, int]]] = {}
            for key, watched_at, watched_status, percent_complete in cdb.execute(
                query, (media_type,)
            ):
                rows.setdefault(int(key), []).append(
                    (int(watched_at), watched_status, int(percent_complete))
                )
            return rows

        movie_plays = _sweep(_MOVIE_SWEEP_SQL, "movie")
        season_plays = _sweep(_SEASON_SWEEP_SQL, "episode")

        bands: BandTable = {}
        counts = {
            "abandoned": 0,
            "completed_only": 0,
            "mixed_excluded": 0,
            "unanchored_excluded": 0,
        }

        for media_type, plex_rating_key in candidates:
            key = int(plex_rating_key)
            if media_type == "movie":
                rows = movie_plays.get(key)
            elif media_type == "season":
                rows = season_plays.get(key)
            else:
                continue
            if not rows:
                continue
            before = [(e, s, p) for e, s, p in rows if e <= cutoff_epoch]
            watched_again = any(cutoff_epoch < e <= window_end_epoch for e, _, _ in rows)
            has_abandoned = any(not qualifies(s, p) for _, s, p in before)
            has_qualified = any(qualifies(s, p) for _, s, p in before)
            label = classify_title(
                has_abandoned=has_abandoned,
                has_qualified=has_qualified,
                has_play_before=bool(before),
            )
            if label is None:
                counts["mixed_excluded"] += 1
                continue
            if label == CONTROL_NEVER_PLAYED:
                # reaper.db keeps no arrival date, so a title with no play before the
                # cutoff cannot be anchored here. It is not this source's control
                # (docs/LEARNINGS.md).
                counts["unanchored_excluded"] += 1
                continue
            reference_epoch = max(e for e, _, _ in before)
            days = (cutoff_epoch - reference_epoch) / ratio_measure.DAY_SECONDS
            _add(bands, band_for(days), label, watched_again)
            counts[label] += 1

        return bands, counts
    finally:
        rdb.close()
        cdb.close()


# --------------------------------------------------------------------------- dump


def load_dump_observations(path: Path, cutoff_days: int) -> tuple[BandTable, dict[str, int]]:
    """Read a ``scripts/tautulli_anon_dump.py`` dump via
    ``delete_threshold_ratio_measure._load_dump`` (gzip-or-plain JSON, imported rather
    than rewritten here). ``reference_now`` in the dump is the clock.

    Copies that script's ``load_dump`` item/season/play traversal, extended to carry
    ``watched_status`` and ``percent_complete`` per play so :func:`classify_title` can
    apply ``qualifies()``. That loader discards both fields.
    """
    data = ratio_measure._load_dump(path)
    now_epoch = int(data["reference_now"])
    cutoff_epoch = now_epoch - cutoff_days * ratio_measure.DAY_SECONDS
    window_end_epoch = cutoff_epoch + ratio_measure.OUTCOME_WINDOW_DAYS * ratio_measure.DAY_SECONDS

    by_movie: dict[str, list[tuple[int, float | None, int]]] = {}
    by_season: dict[str, list[tuple[int, float | None, int]]] = {}
    for play in data.get("plays") or []:
        at = play.get("at")
        if at is None:
            continue
        status = play.get("watched_status")
        status = float(status) if status is not None else None
        pct = int(play.get("percent_complete") or 0)
        if play.get("type") == "movie":
            by_movie.setdefault(play["item"], []).append((int(at), status, pct))
        elif play.get("type") == "episode" and play.get("season"):
            by_season.setdefault(play["season"], []).append((int(at), status, pct))

    bands: BandTable = {}
    counts = {
        "abandoned": 0,
        "never_played": 0,
        "mixed_excluded": 0,
        "withheld": 0,
        "completed_not_used": 0,
    }

    def _place(
        token: str,
        plays_by_token: dict[str, list[tuple[int, float | None, int]]],
        added_at: int | None,
    ) -> None:
        rows = plays_by_token.get(token, [])
        before = [(e, s, p) for e, s, p in rows if e <= cutoff_epoch]
        watched_again = any(cutoff_epoch < e <= window_end_epoch for e, _, _ in rows)
        has_abandoned = any(not qualifies(s, p) for _, s, p in before)
        has_qualified = any(qualifies(s, p) for _, s, p in before)
        label = classify_title(
            has_abandoned=has_abandoned, has_qualified=has_qualified, has_play_before=bool(before)
        )
        if label is None:
            counts["mixed_excluded"] += 1
            return
        if label == CONTROL_COMPLETED:
            # This source's control is CONTROL_NEVER_PLAYED. A title played and fully
            # completed before the cutoff fits neither cohort here, so it is counted
            # for transparency and left out of every band.
            counts["completed_not_used"] += 1
            return
        if before:
            reference_epoch = max(e for e, _, _ in before)
        elif added_at is not None and added_at <= cutoff_epoch:
            reference_epoch = added_at
        else:
            # No plays before the cutoff and no usable arrival date either. This
            # matches by hand the same withhold rule ``rewatch.training_pair`` states,
            # since this needs the raw (status, percent) pairs that helper does not carry.
            counts["withheld"] += 1
            return
        days = (cutoff_epoch - reference_epoch) / ratio_measure.DAY_SECONDS
        _add(bands, band_for(days), label, watched_again)
        counts[label] += 1

    for item in data.get("items") or []:
        if item.get("type") == "movie":
            _place(item["token"], by_movie, item.get("added_at"))
    for show in data.get("seasons") or []:
        for season in show.get("seasons") or []:
            _place(season["token"], by_season, season.get("added_at"))

    return bands, counts


# --------------------------------------------------------------------------- reporting


def _fmt_band(band: tuple[float, float | None]) -> str:
    lo, hi = band
    return f">{int(lo)}d" if hi is None else f"{int(lo)}-{int(hi)}d"


def print_band_table(bands: BandTable, control_label: str, floor: int) -> None:
    print(
        f"  {'band':>10}  {'abandoned n/k':>14}  {'rate':>6}  {'w95':>6}  "
        f"{'control n/k':>13}  {'rate':>6}  {'w95':>6}  {'lift':>7}  status"
    )
    for band in sorted(bands, key=lambda b: b[0]):
        a = bands[band].get(ABANDONED, Cohort())
        c = bands[band].get(control_label, Cohort())
        a_rate, c_rate = _rate(a), _rate(c)
        a_w = wilson_upper(a.k, a.n) if a.n else 0.0
        c_w = wilson_upper(c.k, c.n) if c.n else 0.0
        included = a.n >= floor and c.n >= floor
        lift_str = f"{a_rate - c_rate:+.3f}" if a.n and c.n else "n/a"
        status = "" if included else "THIN, excluded from pooled"
        print(
            f"  {_fmt_band(band):>10}  {f'{a.n}/{a.k}':>14}  {a_rate:>6.3f}  {a_w:>6.3f}  "
            f"{f'{c.n}/{c.k}':>13}  {c_rate:>6.3f}  {c_w:>6.3f}  {lift_str:>7}  {status}"
        )


def run(
    bands: BandTable,
    counts: dict[str, int],
    *,
    control_label: str,
    kind: str,
    source: str,
    cutoff_days: int,
) -> None:
    print("Reaper abandonment-signal measurement")
    print(f"input: {kind} at {source}")
    print(f"cutoff: now minus {cutoff_days} days (one wall-clock instant for the whole server)")
    print()
    print("ABANDONED = a play services/rewatch.py's qualifies() discards as partial: a")
    print("reported watched_status under 0.5, or with no status a percent_complete under 50,")
    print("except a status-less 0 percent play, which qualifies() counts as a play and is")
    print("therefore NOT abandoned (unknown resolves toward keeping).")
    if control_label == CONTROL_COMPLETED:
        print("CONTROL (nearest honest control on this source): titles whose only pre-cutoff")
        print("plays are completed. reaper.db keeps no arrival date, so a never-played")
        print('control cannot be anchored here (docs/LEARNINGS.md, "The delete threshold')
        print("buys volume, not precision\"); this source's control is not the dump's.")
    else:
        print("CONTROL: titles with no plays at all before the cutoff, dormancy measured")
        print("from their arrival date.")
    print()
    excluded = counts.get("mixed_excluded", 0)
    withheld = counts.get("withheld", 0) + counts.get("unanchored_excluded", 0)
    print(
        f"titles: {counts.get(ABANDONED, 0):,} abandoned,"
        f" {counts.get(control_label, 0):,} control, "
        f"{excluded:,} excluded (mixed history), {withheld:,} withheld (no honest anchor)"
    )
    print(
        f"a band under {REWATCH_BLOCK_FLOOR_N} titles on either side is reported but excluded"
        " from the pooled verdict; any rate is reported beside its Wilson 95% upper bound"
    )
    print()
    print_band_table(bands, control_label, REWATCH_BLOCK_FLOOR_N)
    print()
    lift, pooled_a, pooled_c, included = pooled_lift(bands, control_label, REWATCH_BLOCK_FLOOR_N)
    print(
        f"pooled over {len(included)} band(s) with >= {REWATCH_BLOCK_FLOOR_N} titles on both sides:"
        f" abandoned {pooled_a.n:,}/{pooled_a.k:,} ({_rate(pooled_a):.3f}), "
        f"control {pooled_c.n:,}/{pooled_c.k:,} ({_rate(pooled_c):.3f})"
    )
    print(verdict_line(lift))
    print()


# --------------------------------------------------------------------------- entry point


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Measure whether an abandoned (partial) play predicts a title gets played "
            "again. Read-only; prints ratios, counts and spans only."
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
        bands, counts = load_datadir_observations(path, args.cutoff_days)
        run(
            bands,
            counts,
            control_label=CONTROL_COMPLETED,
            kind="Reaper data directory",
            source=str(path),
            cutoff_days=args.cutoff_days,
        )
        return 0

    if not path.is_file():
        print(f"no such file or directory: {path}", file=sys.stderr)
        return 1

    bands, counts = load_dump_observations(path, args.cutoff_days)
    run(
        bands,
        counts,
        control_label=CONTROL_NEVER_PLAYED,
        kind="Tautulli dump",
        source=str(path),
        cutoff_days=args.cutoff_days,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
