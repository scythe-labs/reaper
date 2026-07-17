# SPDX-License-Identifier: AGPL-3.0-or-later
"""Validate Reaper's ingested mirrors against the source systems, read-only.

The policy engine's fidelity harness (``tests/test_policy_permutations.py``) proves the
engine reproduces its own stored decisions. This script closes the other half of the
loop: it checks that the *inputs* to those decisions -- the local watch-history mirror,
the IMDb ratings table, and the frozen candidate facts -- faithfully reflect what
Tautulli, the raw IMDb dataset file, and Radarr/Sonarr actually say.

Checks, each printing aggregates only (never a title, user, or key):

* **History totals and horizon** -- Tautulli's ``recordsTotal`` vs the mirror's row
  count, and the oldest source rows vs the mirror's horizon. Expected drift: plays
  recorded since the last sync (a scan re-syncs first). Pagination detail that will
  bite anyone repeating this by hand: ``get_history`` prepends current live sessions
  to the list but excludes them from ``recordsTotal``, so the ordered set is
  ``recordsFiltered`` long and a last-page fetch at ``recordsTotal - N`` misses the
  oldest rows by however many streams are playing right now. The horizon check below
  pages past ``recordsTotal`` for exactly this reason.
* **Per-item history** -- for sampled items: row counts, last-played, and distinct
  watchers recomputed from raw ``get_history`` rows under the sync's own skip rules,
  vs the mirror. Plus the mid-binge guard's exact inputs: per-row ``media_index`` /
  ``watched_status`` and the per-user max-completed-episode aggregate.
* **Never-played is really never-played** -- items with no mirror rows are confirmed
  to have no source history either.
* **Dormancy derivation** -- for never-played items, the stored why-panel phrase vs
  ``(scan time - max(added_at, horizon))`` recomputed from the source's ``added_at``.
  Humanized phrases keep two units, so agreement is within 30 days by construction.
* **IMDb** -- the ``imdb_rating`` table vs the raw ``title.ratings.tsv.gz`` on disk:
  full row counts, a random sample, and every candidate's id.
* **Radarr / Sonarr** -- every movie candidate joined back to its instance by media
  key: sizes, quality, genres, year, ids; season candidates checked for content-season
  sets, sizes, and an independently recomputed season rank.

Instance URLs come from the ``instance`` table; API keys from the environment (or a
gitignored ``.env``) as ``REAPER_{KIND}_{NAME}_API_KEY`` -- the same names the dev
seeding uses. Instances without a key in the environment are skipped with a notice
(keys stored encrypted in the database are not read by this script).

Usage: ``uv run python scripts/validate_ingest.py`` from the repo root.
"""

from __future__ import annotations

import asyncio
import gzip
import json
import os
import random
import re
import sqlite3
import sys
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from reaper.clients.arr import RadarrClient, SonarrClient  # noqa: E402
from reaper.clients.base import BaseClient, RuntimeSafety  # noqa: E402
from reaper.clients.tautulli import TautulliClient  # noqa: E402

SAFETY = RuntimeSafety(destructive_enabled=False)
UNITS = {"year": 365, "years": 365, "month": 30, "months": 30, "day": 1, "days": 1}
#: humanize_days keeps the two most significant units, so a "years, months" phrase
#: truncates up to 29 days -- agreement within this bound is exact.
HUMANIZE_SLACK_DAYS = 30

failures: list[str] = []


def report(check: str, ok: bool, message: str) -> None:
    print(f"  {'ok' if ok else 'MISMATCH'}: {message}")
    if not ok:
        failures.append(f"{check}: {message}")


def load_env() -> dict[str, str]:
    out = dict(os.environ)
    env_file = REPO / ".env"
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, _, value = line.partition("=")
                out.setdefault(key.strip(), value.strip().strip('"').strip("'"))
    return out


def parse_humanized(text: str) -> float | None:
    """Invert ``clock.humanize_days``, cutting the gate detail's threshold phrase."""
    text = re.split(r", (?:less than|past) ", text)[0]
    if "today" in text:
        return 0.0
    total, seen = 0, False
    for num, unit in re.findall(r"(\d+)\s+(year|years|month|months|day|days)\b", text):
        total += int(num) * UNITS[unit]
        seen = True
    return float(total) if seen else None


def sync_style_rows(raw_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Exactly the skip rules ``history_sync.sync`` applies before writing a row."""
    kept = []
    for row in raw_rows:
        if row.get("row_id") is None:
            continue
        date = row.get("date") or row.get("started")
        if not date or row.get("user_id") is None or row.get("rating_key") is None:
            continue
        kept.append(row)
    return kept


async def check_history(
    tautulli: TautulliClient,
    cdb: sqlite3.Connection,
    rng: random.Random,
    rdb: sqlite3.Connection,
    snap_id: int,
) -> None:
    print("history: totals and horizon")
    page = await tautulli.history(length=2, start=0)
    records_total = int(page.get("recordsTotal") or 0)
    mirror_rows = cdb.execute("SELECT COUNT(*) FROM watch_event").fetchone()[0]
    mirror_max = cdb.execute("SELECT MAX(watched_at) FROM watch_event").fetchone()[0] or 0
    gap = records_total - mirror_rows
    # The mirror lags the live source by plays since its last sync (a scan re-syncs
    # first), and leads it by any rows the source deleted. Both are by design; what
    # would indicate an ingest bug is a large shortfall of OLD rows.
    newest = await tautulli.history(length=max(50, min(1000, abs(gap) + 50)), start=0)
    mirror_ids = {r[0] for r in cdb.execute("SELECT row_id FROM watch_event")}
    missing_old = [
        r
        for r in sync_style_rows(newest.get("data") or [])
        if int(r["row_id"]) not in mirror_ids
        and int(r.get("date") or r.get("started") or 0) <= mirror_max
    ]
    report(
        "history-total",
        len(missing_old) <= 2,
        f"source={records_total} mirror={mirror_rows} (post-sync plays account for the "
        f"gap; source rows older than the mirror's newest yet absent: {len(missing_old)})",
    )

    # The true oldest rows sit PAST recordsTotal: live sessions are prepended to the
    # list but not counted, shifting every persisted row down by the live count. Page
    # from recordsTotal - 5 through the end of the real set to cover both offsets.
    tail_rows: list[dict[str, Any]] = []
    start = max(0, records_total - 5)
    while True:
        page = await tautulli.history(length=25, start=start)
        rows = page.get("data") or []
        tail_rows.extend(rows)
        if len(rows) < 25:
            break
        start += 25
    tail_dates = [int(r.get("date") or r.get("started")) for r in sync_style_rows(tail_rows)]
    source_oldest = min(tail_dates) if tail_dates else None
    mirror_min = cdb.execute("SELECT MIN(watched_at) FROM watch_event").fetchone()[0]
    # mirror older than source is legitimate (the mirror preserves rows the source may
    # later delete); source older than mirror would mean the sync never ingested the
    # oldest history, which understates the horizon and is a real ingest bug.
    report(
        "history-horizon",
        source_oldest is None or mirror_min is None or mirror_min <= source_oldest,
        f"source oldest={source_oldest} mirror horizon={mirror_min} "
        f"({'equal' if source_oldest == mirror_min else 'mirror leads or trails'})",
    )

    print("history: per-item facts on sampled keys")
    movie_keys = [
        int(r[0])
        for r in cdb.execute("SELECT DISTINCT rating_key FROM watch_event WHERE media_type='movie'")
    ]
    season_keys = [
        int(r[0])
        for r in cdb.execute(
            "SELECT DISTINCT parent_rating_key FROM watch_event "
            "WHERE media_type='episode' AND parent_rating_key IS NOT NULL"
        )
    ]
    rng.shuffle(movie_keys)
    rng.shuffle(season_keys)
    mismatch = Counter()
    checked = 0
    for key in movie_keys[:20]:
        raw = await tautulli.history(rating_key=key, length=10_000)
        rows = [
            r for r in sync_style_rows(raw.get("data") or []) if str(r.get("media_type")) == "movie"
        ]
        n, last, users = cdb.execute(
            "SELECT COUNT(*), MAX(watched_at), COUNT(DISTINCT user_id) FROM watch_event "
            "WHERE rating_key=? AND media_type='movie'",
            (key,),
        ).fetchone()
        src_last = max((int(r.get("date") or r.get("started")) for r in rows), default=None)
        checked += 1
        if len(rows) != n:
            mismatch["movie_rows"] += 1
        if src_last != last:
            mismatch["movie_last_played"] += 1
        if len({r["user_id"] for r in rows}) != users:
            mismatch["movie_watchers"] += 1
    binge_mismatch = 0
    for key in season_keys[:12]:
        raw = await tautulli.history(parent_rating_key=key, length=10_000)
        src_rows = [
            r
            for r in sync_style_rows(raw.get("data") or [])
            if str(r.get("media_type")) == "episode"
        ]
        n, last, users = cdb.execute(
            "SELECT COUNT(*), MAX(watched_at), COUNT(DISTINCT user_id) FROM watch_event "
            "WHERE parent_rating_key=? AND media_type='episode'",
            (key,),
        ).fetchone()
        checked += 1
        if len(src_rows) != n:
            mismatch["season_rows"] += 1
        src_last = max((int(r.get("date") or r.get("started")) for r in src_rows), default=None)
        if src_last != last:
            mismatch["season_last_played"] += 1
        if len({r["user_id"] for r in src_rows}) != users:
            mismatch["season_watchers"] += 1
        # The mid-binge guard's aggregate: per-user max COMPLETED episode.
        src_progress: dict[int, int] = {}
        for r in src_rows:
            if float(r.get("watched_status") or 0) == 1 and r.get("media_index") not in (None, ""):
                user = int(r["user_id"])
                src_progress[user] = max(src_progress.get(user, 0), int(r["media_index"]))
        mirror_progress = {
            int(u): int(mx)
            for u, mx in cdb.execute(
                "SELECT user_id, MAX(media_index) FROM watch_event "
                "WHERE parent_rating_key=? AND media_type='episode' "
                "AND media_index IS NOT NULL AND watched_status=1 GROUP BY user_id",
                (key,),
            )
        }
        if src_progress != mirror_progress:
            binge_mismatch += 1
    report(
        "history-items",
        not mismatch and not binge_mismatch,
        f"{checked} keys compared (counts, last-played, watchers, binge progress): "
        f"{dict(mismatch) if mismatch else 'all match'}"
        + (f", binge aggregates off on {binge_mismatch} keys" if binge_mismatch else ""),
    )

    print("history: never-played means never played, and dormancy derives from added_at")
    candidate_keys = [
        int(r[0])
        for r in rdb.execute(
            "SELECT plex_rating_key FROM candidate WHERE snapshot_id=? AND media_type='movie' "
            "AND plex_rating_key IS NOT NULL",
            (snap_id,),
        )
    ]
    never = [
        k
        for k in candidate_keys
        if not cdb.execute("SELECT 1 FROM watch_event WHERE rating_key=? LIMIT 1", (k,)).fetchone()
    ]
    rng.shuffle(never)
    created_at, horizon_at = rdb.execute(
        "SELECT created_at, horizon_at FROM snapshot WHERE id=?", (snap_id,)
    ).fetchone()
    scan_time = datetime.fromtimestamp(created_at, tz=UTC)
    horizon = datetime.fromtimestamp(horizon_at, tz=UTC)
    ghost_history = derivation_off = derivation_checked = 0
    for key in never[:10]:
        raw = await tautulli.history(rating_key=key, length=100)
        if sync_style_rows(raw.get("data") or []):
            ghost_history += 1
            continue
        row = rdb.execute(
            "SELECT explanation_json FROM candidate WHERE snapshot_id=? AND plex_rating_key=?",
            (snap_id, key),
        ).fetchone()
        if row is None:
            continue
        explanation = json.loads(row[0])
        unwatched = next((s for s in explanation["signals"] if s["id"] == "unwatched"), None)
        if unwatched is None or not unwatched["evaluated"]:
            continue
        stored_days = parse_humanized(unwatched["detail"])
        meta = await tautulli.metadata(key)
        added = meta.get("added_at")
        if not added or stored_days is None:
            continue
        derivation_checked += 1
        derived = (scan_time - max(datetime.fromtimestamp(int(added), tz=UTC), horizon)).days
        if abs(stored_days - derived) > HUMANIZE_SLACK_DAYS:
            derivation_off += 1
    report(
        "history-never-played",
        ghost_history == 0 and derivation_off == 0,
        f"{len(never[:10])} never-played items: {ghost_history} with source history, "
        f"{derivation_off}/{derivation_checked} dormancy derivations off by more than "
        f"{HUMANIZE_SLACK_DAYS} days",
    )


def check_imdb(
    cdb: sqlite3.Connection, rdb: sqlite3.Connection, snap_id: int, rng: random.Random
) -> None:
    print("imdb: raw dataset file vs the mirror table")
    tsv_path = REPO / "data" / "title.ratings.tsv.gz"
    if not tsv_path.exists():
        print("  skipped: no title.ratings.tsv.gz on disk")
        return
    tsv: dict[str, tuple[float, int]] = {}
    with gzip.open(tsv_path, "rt") as f:
        f.readline()
        for line in f:
            parts = line.rstrip("\n").split("\t")
            if len(parts) == 3 and parts[0].startswith("tt"):
                tsv[parts[0]] = (float(parts[1]), int(parts[2]))
    table_count = cdb.execute("SELECT COUNT(*) FROM imdb_rating").fetchone()[0]
    sample_ids = rng.sample(sorted(tsv), min(500, len(tsv)))
    qmarks = ",".join("?" * len(sample_ids))
    table_rows = {
        t: (float(r), int(v))
        for t, r, v in cdb.execute(
            f"SELECT tconst, average_rating, num_votes FROM imdb_rating "  # noqa: S608
            f"WHERE tconst IN ({qmarks})",
            sample_ids,
        )
    }
    sample_bad = sum(1 for t in sample_ids if table_rows.get(t) != tsv[t])
    candidate_ids = [
        r[0]
        for r in rdb.execute(
            "SELECT DISTINCT imdb_id FROM candidate WHERE snapshot_id=? AND imdb_id IS NOT NULL",
            (snap_id,),
        )
    ]
    qmarks = ",".join("?" * len(candidate_ids))
    candidate_rows = {
        t: (float(r), int(v))
        for t, r, v in cdb.execute(
            f"SELECT tconst, average_rating, num_votes FROM imdb_rating "  # noqa: S608
            f"WHERE tconst IN ({qmarks})",
            candidate_ids,
        )
    }
    # Ids in one side only, or with different numbers, are drift; absent from both is
    # simply a title the dataset does not rate (correctly Absent in the scan).
    candidate_bad = sum(
        1
        for t in candidate_ids
        if (t in candidate_rows or t in tsv) and candidate_rows.get(t) != tsv.get(t)
    )
    report(
        "imdb",
        table_count == len(tsv) and sample_bad == 0 and candidate_bad == 0,
        f"file={len(tsv)} rows, table={table_count}; sample mismatches={sample_bad}/"
        f"{len(sample_ids)}; candidate-id mismatches={candidate_bad}/{len(candidate_ids)}",
    )


async def check_radarr(
    instance_id: int, client: RadarrClient, rdb: sqlite3.Connection, snap_id: int
) -> None:
    movies = await client.movies()
    with_file = [m for m in movies if m.get("hasFile")]
    candidates = {
        row[0]: row
        for row in rdb.execute(
            "SELECT media_key, size_bytes, quality, genres_json, year, imdb_id, tmdb_id "
            "FROM candidate WHERE snapshot_id=? AND media_type='movie' AND media_key LIKE ?",
            (snap_id, f"radarr:{instance_id}:%"),
        )
    }
    joined = 0
    mismatch = Counter()
    for movie in with_file:
        row = candidates.get(f"radarr:{instance_id}:{movie['id']}")
        if row is None:
            mismatch["source_only"] += 1
            continue
        joined += 1
        _, size_bytes, quality, genres_json, year, imdb_id, tmdb_id = row
        if int(movie.get("sizeOnDisk") or 0) != int(size_bytes):
            mismatch["size"] += 1
        source_quality = (
            (((movie.get("movieFile") or {}).get("quality") or {}).get("quality")) or {}
        ).get("name")
        if (source_quality or None) != (quality or None):
            mismatch["quality"] += 1
        if [str(g) for g in (movie.get("genres") or []) if g] != (
            json.loads(genres_json) if genres_json else []
        ):
            mismatch["genres"] += 1
        if (int(movie["year"]) if movie.get("year") else None) != (int(year) if year else None):
            mismatch["year"] += 1
        if movie.get("tmdbId") and tmdb_id and int(movie["tmdbId"]) != int(tmdb_id):
            mismatch["tmdb_id"] += 1
        if movie.get("imdbId") and imdb_id and str(movie["imdbId"]) != str(imdb_id):
            mismatch["imdb_id"] += 1
    snapshot_only = len(candidates) - joined
    # Genre edits and library changes since the snapshot are source-side drift, not
    # ingest bugs -- flagged only when they exceed a handful.
    drift = sum(mismatch.values()) + snapshot_only
    report(
        f"radarr-{instance_id}",
        drift <= max(3, len(candidates) // 200),
        f"source hasFile={len(with_file)}, snapshot={len(candidates)}, joined={joined}, "
        f"field mismatches={dict(mismatch) if mismatch else 'none'}, "
        f"snapshot-only={snapshot_only}",
    )


async def check_sonarr(
    instance_id: int,
    client: SonarrClient,
    rdb: sqlite3.Connection,
    snap_id: int,
    rng: random.Random,
) -> None:
    series_list = await client.series()
    series_by_id = {int(s["id"]): s for s in series_list}
    by_show: dict[int, dict[int, tuple[int, str]]] = {}
    for media_key, size_bytes, explanation_json in rdb.execute(
        "SELECT media_key, size_bytes, explanation_json FROM candidate "
        "WHERE snapshot_id=? AND media_type='season' AND media_key LIKE ?",
        (snap_id, f"sonarr:{instance_id}:%"),
    ):
        _, _, sid, n = media_key.split(":")
        by_show.setdefault(int(sid), {})[int(n)] = (int(size_bytes), explanation_json)
    mismatch = Counter()
    shows = seasons = 0
    for sid in rng.sample(sorted(by_show), min(20, len(by_show))):
        series = series_by_id.get(sid)
        if series is None:
            mismatch["show_gone"] += 1
            continue
        shows += 1
        source_content: dict[int, int] = {}
        for season in series.get("seasons") or []:
            stats = season.get("statistics") or {}
            if int(stats.get("episodeFileCount") or 0) > 0:
                source_content[int(season["seasonNumber"])] = int(stats.get("sizeOnDisk") or 0)
        snapshot_seasons = by_show[sid]
        if set(source_content) != set(snapshot_seasons):
            mismatch["season_set"] += 1
        # Rank recomputed independently of the engine: newest-first, specials excluded.
        ranked = sorted((n for n in source_content if n != 0), reverse=True)
        source_rank = {n: i + 1 for i, n in enumerate(ranked)}
        for n, (size_bytes, explanation_json) in snapshot_seasons.items():
            seasons += 1
            if n in source_content and source_content[n] != size_bytes:
                mismatch["size"] += 1
            explanation = json.loads(explanation_json)
            rank_signal = next(
                (s for s in explanation["signals"] if s["id"] == "season_rank"), None
            )
            if rank_signal and rank_signal["evaluated"] and n in source_rank:
                m = re.search(r"number (\d+) counting back", rank_signal["detail"])
                if m and int(m.group(1)) != source_rank[n]:
                    mismatch["rank"] += 1
    report(
        f"sonarr-{instance_id}",
        sum(mismatch.values()) <= max(2, seasons // 30),
        f"{shows} shows / {seasons} seasons: {dict(mismatch) if mismatch else 'all match'}",
    )


async def main() -> None:
    env = load_env()
    rng = random.Random(7)
    rdb = sqlite3.connect(f"file:{REPO / 'data' / 'reaper.db'}?mode=ro", uri=True)
    cdb = sqlite3.connect(f"file:{REPO / 'data' / 'cache.db'}?mode=ro", uri=True)
    snap = rdb.execute(
        "SELECT id FROM snapshot WHERE degraded = 0 ORDER BY id DESC LIMIT 1"
    ).fetchone()
    if snap is None:
        sys.exit("no non-degraded snapshot to validate against")
    snap_id = int(snap[0])
    print(f"validating mirrors + snapshot {snap_id} against source systems (read-only)")

    clients: list[BaseClient] = []
    tautulli: TautulliClient | None = None
    radarrs: dict[int, RadarrClient] = {}
    sonarrs: dict[int, SonarrClient] = {}
    for iid, kind, name, base_url in rdb.execute(
        "SELECT id, kind, name, base_url FROM instance WHERE enabled = 1"
    ):
        key = env.get(f"REAPER_{kind}_{name}_API_KEY")
        if not key:
            print(f"  skipping {kind} instance {iid}: no REAPER_{kind}_{name}_API_KEY in env")
            continue
        if kind == "TAUTULLI":
            tautulli = TautulliClient(base_url, key, safety=SAFETY)
            clients.append(tautulli)
        elif kind == "RADARR":
            radarrs[int(iid)] = RadarrClient(base_url, key, safety=SAFETY)
            clients.append(radarrs[int(iid)])
        elif kind == "SONARR":
            sonarrs[int(iid)] = SonarrClient(base_url, key, safety=SAFETY)
            clients.append(sonarrs[int(iid)])

    try:
        if tautulli is not None:
            await check_history(tautulli, cdb, rng, rdb, snap_id)
        check_imdb(cdb, rdb, snap_id, rng)
        for iid, radarr in radarrs.items():
            print(f"radarr instance {iid}: source vs snapshot candidates")
            await check_radarr(iid, radarr, rdb, snap_id)
        for iid, sonarr in sonarrs.items():
            print(f"sonarr instance {iid}: source vs snapshot candidates")
            await check_sonarr(iid, sonarr, rdb, snap_id, rng)
    finally:
        for client in clients:
            await client.aclose()

    if failures:
        print(f"\n{len(failures)} check(s) failed:")
        for failure in failures:
            print(f"  - {failure}")
        sys.exit(1)
    print("\nall ingest checks passed")


if __name__ == "__main__":
    asyncio.run(main())
