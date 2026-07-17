# SPDX-License-Identifier: AGPL-3.0-or-later
"""Regenerate the policy-lab fixture from a real library, de-identified by construction.

Reads the newest non-degraded snapshot in ``data/reaper.db`` plus the local mirrors in
``data/cache.db`` and writes ``tests/fixtures/policy_lab_vectors.json``: a stratified
sample of real fact *shapes* for the permutation harness in
``tests/test_policy_permutations.py``.

What the fixture contains, and deliberately nothing more:

* observation states (known / absent / unknown) per fact, with numeric values only:
  day counts, watcher counts, season numbers, rating tenths;
* vote counts rounded to two significant figures and sizes to 100 MB, so no value is
  precise enough to fingerprint a file;
* genre names replaced by frequency-ranked tokens (``Genre01``), rare quality names
  collapsed to ``Other``;
* per-show season shapes as ``(season_number, watchers)`` pairs;
* a pinned baseline: every vector judged under the SHIPPED default policies, so the
  harness can detect any change in engine behaviour against real shapes.

No titles, ids, media keys, paths, hosts, or usernames -- the golden rule applies to
fixtures exactly as it does to code.

Usage: ``uv run python scripts/policy_lab_extract.py`` from the repo root.
"""

from __future__ import annotations

import json
import random
import re
import sqlite3
import sys
from collections import Counter
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "src"))

from tests._policy_lab import judge  # noqa: E402

from reaper.engine.policy import DEFAULT_MOVIE_POLICY, DEFAULT_TV_POLICY  # noqa: E402
from reaper.services.scan_runner import build_gates  # noqa: E402

OUT = REPO / "tests" / "fixtures" / "policy_lab_vectors.json"
TARGET_PER_TYPE = 220
TARGET_SHOWS = 100
WINDOW_DAYS = 365

UNITS = {"year": 365, "years": 365, "month": 30, "months": 30, "day": 1, "days": 1}


def parse_humanized(text: str) -> float | None:
    """Invert ``clock.humanize_days``, cutting the threshold phrase the gate detail
    appends ("..., less than the 1 year Reaper waits")."""
    text = re.split(r", (?:less than|past) ", text)[0]
    if "today" in text:
        return 0.0
    total, seen = 0, False
    for num, unit in re.findall(r"(\d+)\s+(year|years|month|months|day|days)\b", text):
        total += int(num) * UNITS[unit]
        seen = True
    return float(total) if seen else None


def round_votes(votes: int) -> int:
    """Two significant figures: enough for every vote-floor comparison the engine makes,
    coarse enough to identify nothing."""
    if votes < 100:
        return votes
    magnitude = 10 ** (len(str(votes)) - 2)
    return round(votes / magnitude) * magnitude


def round_size(size: int) -> int:
    return round(size / 100_000_000) * 100_000_000


def obs(state: str, value: Any = None) -> dict[str, Any]:
    return {"state": state, "value": value} if state == "known" else {"state": state}


def main() -> None:
    rng = random.Random(42)
    rdb = sqlite3.connect(f"file:{REPO / 'data' / 'reaper.db'}?mode=ro", uri=True)
    cdb = sqlite3.connect(f"file:{REPO / 'data' / 'cache.db'}?mode=ro", uri=True)

    row = rdb.execute(
        "SELECT id, created_at FROM snapshot WHERE degraded = 0 ORDER BY id DESC LIMIT 1"
    ).fetchone()
    if row is None:
        sys.exit("no non-degraded snapshot to extract from")
    snap_id, created_at = row
    now = datetime.fromtimestamp(created_at, tz=UTC)
    window_start = int((now - timedelta(days=WINDOW_DAYS)).timestamp())

    # ---- bulk watch stats ---------------------------------------------------
    movie_user_last: dict[int, dict[int, int]] = {}
    for k, u, last in cdb.execute(
        "SELECT rating_key, user_id, MAX(watched_at) FROM watch_event "
        "WHERE media_type='movie' GROUP BY rating_key, user_id"
    ):
        movie_user_last.setdefault(int(k), {})[int(u)] = int(last)
    movie_last = {
        int(k): int(last)
        for k, last in cdb.execute(
            "SELECT rating_key, MAX(watched_at) FROM watch_event "
            "WHERE media_type='movie' GROUP BY rating_key"
        )
    }
    season_user_last: dict[int, dict[int, int]] = {}
    for k, u, last in cdb.execute(
        "SELECT parent_rating_key, user_id, MAX(watched_at) FROM watch_event "
        "WHERE media_type='episode' AND parent_rating_key IS NOT NULL "
        "GROUP BY parent_rating_key, user_id"
    ):
        season_user_last.setdefault(int(k), {})[int(u)] = int(last)
    season_last = {
        int(k): int(last)
        for k, last in cdb.execute(
            "SELECT parent_rating_key, MAX(watched_at) FROM watch_event "
            "WHERE media_type='episode' AND parent_rating_key IS NOT NULL "
            "GROUP BY parent_rating_key"
        )
    }

    def stats(keys: list[int], media_type: str) -> tuple[float | None, int, int, list[float]]:
        user_last = movie_user_last if media_type == "movie" else season_user_last
        last_map = movie_last if media_type == "movie" else season_last
        merged: dict[int, int] = {}
        for key in keys:
            for u, last in user_last.get(key, {}).items():
                if u not in merged or last > merged[u]:
                    merged[u] = last
        last = max((last_map[k] for k in keys if k in last_map), default=None)
        days = (now - datetime.fromtimestamp(last, tz=UTC)).days if last else None
        recency = sorted(round((created_at - la) / 86400.0) for la in merged.values())
        window = sum(1 for la in merged.values() if la >= window_start)
        return days, window, len(merged), [float(r) for r in recency]

    def imdb(imdb_id: str | None) -> tuple[int, int] | None:
        if not imdb_id:
            return None
        hit = cdb.execute(
            "SELECT average_rating, num_votes FROM imdb_rating WHERE tconst=?", (imdb_id,)
        ).fetchone()
        return (int(hit[0] * 10), int(hit[1])) if hit else None

    # ---- genre / quality token maps ----------------------------------------
    genre_freq: Counter[str] = Counter()
    quality_freq: Counter[str] = Counter()
    for genres_json, quality in rdb.execute(
        "SELECT genres_json, quality FROM candidate WHERE snapshot_id=?", (snap_id,)
    ):
        if genres_json:
            genre_freq.update(json.loads(genres_json))
        if quality:
            quality_freq[quality] += 1
    genre_token = {
        name: f"Genre{i + 1:02d}" for i, (name, _) in enumerate(genre_freq.most_common())
    }
    common_quality = {name for name, n in quality_freq.items() if n >= 5}

    # ---- per-candidate vectors ---------------------------------------------
    movie_pol = DEFAULT_MOVIE_POLICY
    tv_pol = DEFAULT_TV_POLICY
    unwatched_cfg = {
        "movie": next(s for s in movie_pol.signals if s.signal.value == "unwatched"),
        "season": next(s for s in tv_pol.signals if s.signal.value == "unwatched"),
    }

    vectors: list[dict[str, Any]] = []
    group_of: dict[str, list[int]] = {}
    show_watchers: dict[str, dict[int, int]] = {}

    cur = rdb.execute(
        "SELECT media_key, media_type, plex_rating_key, size_bytes, year, genres_json, "
        "quality, imdb_id, group_key, verdict, score, coverage_bp, explanation_json "
        "FROM candidate WHERE snapshot_id=?",
        (snap_id,),
    )
    for (
        media_key,
        media_type,
        rating_key,
        size_bytes,
        year,
        genres_json,
        quality,
        imdb_id,
        group_key,
        _verdict,
        _stored_score,
        _stored_cov,
        explanation_json,
    ) in cur:
        exp = json.loads(explanation_json)
        sig = {s["id"]: s for s in exp["signals"]}
        gates: dict[str, tuple[str, str]] = {}
        override = None
        for bucket, name in (
            ("protections_fired", "fired"),
            ("protections_checked", "checked"),
            ("protections_unknown", "unknown"),
        ):
            for p in exp[bucket]:
                if p["detail"] == "You spared this by hand.":
                    override = "spare"
                    continue
                gates.setdefault(p["gate"], (name, p["detail"]))

        merged_keys = (exp.get("match") or {}).get("merged_rating_keys") or None
        keys = merged_keys or ([rating_key] if rating_key else [])
        days, window, ever, recency = stats(keys, media_type) if keys else (None, 0, 0, [])

        # dormancy: exact from the mirror; else invert the stored evaluation
        unwatched = sig.get("unwatched")
        cfg = unwatched_cfg[media_type]
        dormancy: dict[str, Any]
        if unwatched is None or not unwatched["evaluated"]:
            dormancy = obs("unknown")
        elif days is not None:
            dormancy = obs("known", float(days))
        else:
            contribution, weight = unwatched["contribution"], unwatched["weight"]
            md_detail = gates.get("min_dormancy", ("", ""))[1]
            if 0 < contribution < weight:
                dormancy = obs(
                    "known",
                    round(cfg.floor + (contribution / weight) * (cfg.saturate_at - cfg.floor)),
                )
            elif (parsed := parse_humanized(md_detail)) is not None:
                dormancy = obs("known", parsed)
            else:
                dormancy = obs("unknown")

        few = sig.get("few_watchers")
        watchers_known = few is not None and few["evaluated"]

        hit = imdb(imdb_id)
        if hit is not None:
            rating_obs = obs("known", hit[0])
            votes_obs = obs("known", round_votes(hit[1]))
        else:
            rating_obs = obs("absent")
            votes_obs = obs("absent")

        if media_type == "movie":
            rank_obs = obs("absent")
        else:
            sr = sig.get("season_rank")
            if sr is None or not sr["evaluated"]:
                rank_obs = obs("unknown")
            elif m := re.search(r"number (\d+) counting back", sr["detail"]):
                rank_obs = obs("known", int(m.group(1)))
            else:
                rank_obs = obs("unknown")

        def from_gate(
            gate: str, fired_means: bool, gates: dict[str, tuple[str, str]] = gates
        ) -> dict[str, Any]:
            state = gates.get(gate, (None, ""))[0]
            if state == "fired":
                return obs("known", fired_means)
            if state == "unknown":
                return obs("unknown")
            return obs("known", not fired_means)

        curated_state = gates.get("curated_list", (None, ""))[0]
        curated_obs = (
            obs("known", "ListA")
            if curated_state == "fired"
            else obs("unknown")
            if curated_state == "unknown"
            else obs("absent")
        )

        if genres_json:
            tokens = [genre_token[g] for g in json.loads(genres_json) if g in genre_token]
            genres_obs = obs("known", ", ".join(tokens)) if tokens else obs("absent")
        else:
            genres_obs = obs("absent")

        if media_type == "movie" and quality:
            quality_obs = obs("known", quality if quality in common_quality else "Other")
        else:
            quality_obs = obs("absent")

        if media_type == "movie" and year:
            age = (now.date() - date(int(year), 12, 31)).days
            release_obs = obs("known", float(max(0, age)))
        else:
            release_obs = obs("absent")

        guard_state = gates.get("season_progression", (None, ""))[0]
        guard = {"state": guard_state or "checked"} if media_type == "season" else None

        season_number = int(media_key.rsplit(":", 1)[1]) if media_type == "season" else None

        vectors.append(
            {
                "media_type": media_type,
                "facts": {
                    "days_observed_unwatched": dormancy,
                    "distinct_watchers": obs("known", window) if watchers_known else obs("unknown"),
                    "distinct_watchers_all_time": (
                        obs("known", ever) if watchers_known else obs("unknown")
                    ),
                    "size_bytes": obs("known", round_size(int(size_bytes))),
                    "imdb_rating_tenths": rating_obs,
                    "imdb_votes": votes_obs,
                    "season_rank": rank_obs,
                    "is_streaming_now": from_gate("streaming_now", True),
                    "is_managed": from_gate("unmanaged", False),
                    "in_curated_list": curated_obs,
                    "is_whitelisted": from_gate("whitelisted", True),
                    "others_watching": obs("absent"),
                    "requested": obs("unknown"),
                    "genres": genres_obs,
                    "release_age_days": release_obs,
                    "quality": quality_obs,
                    "show_ended": obs("unknown") if media_type == "season" else obs("absent"),
                },
                "guard": guard,
                "override": override,
                "play_recency_days": recency,
                "season_number": season_number,
                "_group": group_key,  # dropped before writing; used to build show shapes
            }
        )
        if media_type == "season" and group_key and season_number is not None:
            group_of.setdefault(group_key, []).append(len(vectors) - 1)
            show_watchers.setdefault(group_key, {})[season_number] = ever if watchers_known else 0

    # ---- stratified sample --------------------------------------------------
    def signature(v: dict[str, Any]) -> tuple:
        states = tuple(sorted(f"{k}:{o['state']}" for k, o in v["facts"].items()))
        guard = (v.get("guard") or {}).get("state")
        return (v["media_type"], states, guard, bool(v.get("override")))

    buckets: dict[tuple, list[dict[str, Any]]] = {}
    for v in vectors:
        buckets.setdefault(signature(v), []).append(v)

    sample: list[dict[str, Any]] = []
    for bucket in buckets.values():
        rng.shuffle(bucket)
        sample.extend(bucket[:3])
    for media_type in ("movie", "season"):
        pool = [v for v in vectors if v["media_type"] == media_type and v not in sample]
        want = TARGET_PER_TYPE - sum(1 for v in sample if v["media_type"] == media_type)
        if want > 0 and pool:
            sample.extend(rng.sample(pool, min(want, len(pool))))

    # ---- show shapes --------------------------------------------------------
    groups = sorted(show_watchers, key=lambda g: -len(show_watchers[g]))
    shows = [
        {"seasons": sorted((int(n), int(w)) for n, w in show_watchers[g].items())}
        for g in groups[:TARGET_SHOWS]
    ]

    # ---- pinned baseline under the shipped defaults -------------------------
    gate_lists = {"movie": build_gates(movie_pol), "season": build_gates(tv_pol)}
    for i, v in enumerate(sample):
        v.pop("_group", None)
        v["id"] = f"v{i:04d}"
        policy = movie_pol if v["media_type"] == "movie" else tv_pol
        verdict, score_value, coverage_bp, _, _ = judge(v, policy, gate_lists[v["media_type"]])
        v["baseline"] = {"verdict": verdict, "score": score_value, "coverage_bp": coverage_bp}

    out = {
        "schema": 1,
        "note": (
            "De-identified real library shapes for the policy permutation harness. "
            "Regenerate with scripts/policy_lab_extract.py. The baseline block is the "
            "engine's own output under the shipped default policies at extraction time."
        ),
        "vectors": sample,
        "shows": shows,
    }
    OUT.write_text(json.dumps(out, indent=1, sort_keys=True) + "\n")
    by_type = Counter(v["media_type"] for v in sample)
    print(f"wrote {OUT.relative_to(REPO)}: {dict(by_type)} vectors, {len(shows)} show shapes")


if __name__ == "__main__":
    main()
