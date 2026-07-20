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
  harness can detect any change in engine behavior against real shapes.

No titles, ids, media keys, paths, hosts, or usernames -- the golden rule applies to
fixtures exactly as it does to code.

Usage: ``uv run python scripts/policy_lab_extract.py`` from the repo root.
"""

from __future__ import annotations

import json
import random
import sqlite3
import sys
from collections import Counter
from datetime import UTC, date, datetime
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


#: The observation states ``facts_codec._obs_to_dict`` emits. The fixture uses the same
#: three names, so this is not a translation: it is the allow-list that sends anything
#: unrecognized (a future state, a corrupt row) to the caller's default.
_STATES = frozenset({"known", "absent", "unknown"})


def stored_obs(
    frozen: dict[str, Any], field: str, transform: Any = None, *, default: str = "unknown"
) -> dict[str, Any]:
    """One observation, read from the snapshot's FROZEN facts rather than re-derived.

    Everything else in this file reconstructs facts from ``explanation_json`` and the
    gate details, which is a second implementation of the fact layer and drifts from the
    first. It did: production learned to tell "we looked and there is no rating" from
    "we had no id to look one up with" (``display_meta.dataset_lookup``), and this script
    kept collapsing both to ``absent``, so a regenerated fixture could not contain an
    ``Unknown`` rating however many scans it read. Those two states are opposite
    instructions to the keep lane, so the harness was sweeping evidence that no real scan
    produces.

    ``facts_json`` is the evidence the scan froze, which is exactly what the fixture wants
    a de-identified copy of. ``transform`` blunts precision on the way out (sizes, votes);
    identifying values must be tokenised by the caller, never passed through.
    """
    entry = frozen.get(field)
    if not isinstance(entry, dict):
        return obs(default)
    kind = raw if (raw := str(entry.get("k"))) in _STATES else default
    if kind != "known":
        return obs(kind)
    value = entry.get("v")
    return obs("known", transform(value) if transform is not None else value)


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

    # ---- bulk watch stats ---------------------------------------------------
    movie_user_last: dict[int, dict[int, int]] = {}
    for k, u, last in cdb.execute(
        "SELECT rating_key, user_id, MAX(watched_at) FROM watch_event "
        "WHERE media_type='movie' GROUP BY rating_key, user_id"
    ):
        movie_user_last.setdefault(int(k), {})[int(u)] = int(last)
    season_user_last: dict[int, dict[int, int]] = {}
    for k, u, last in cdb.execute(
        "SELECT parent_rating_key, user_id, MAX(watched_at) FROM watch_event "
        "WHERE media_type='episode' AND parent_rating_key IS NOT NULL "
        "GROUP BY parent_rating_key, user_id"
    ):
        season_user_last.setdefault(int(k), {})[int(u)] = int(last)

    def recency_days(keys: list[int], media_type: str) -> list[float]:
        """Days since each distinct viewer last played any of ``keys``, ascending.

        The only thing this script still derives from the watch mirror. Dormancy, watcher
        counts and every other fact come from the snapshot's frozen facts (``stored_obs``).
        """
        user_last = movie_user_last if media_type == "movie" else season_user_last
        merged: dict[int, int] = {}
        for key in keys:
            for u, last in user_last.get(key, {}).items():
                if u not in merged or last > merged[u]:
                    merged[u] = last
        return sorted(float(round((created_at - la) / 86400.0)) for la in merged.values())

    # An `imdb()` helper lived here, looking ratings up in the cache to rebuild the
    # rating observation. It was the drift: the snapshot already froze that observation,
    # with a three-state answer this could not express. Read the frozen facts instead
    # (stored_obs) rather than asking the dataset a second time.

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

    vectors: list[dict[str, Any]] = []
    group_of: dict[str, list[int]] = {}
    show_watchers: dict[str, dict[int, int]] = {}

    cur = rdb.execute(
        "SELECT media_key, media_type, plex_rating_key, year, genres_json, "
        "quality, group_key, verdict, score, coverage_bp, explanation_json, facts_json "
        "FROM candidate WHERE snapshot_id=?",
        (snap_id,),
    )
    for (
        media_key,
        media_type,
        rating_key,
        year,
        genres_json,
        quality,
        group_key,
        _verdict,
        _stored_score,
        _stored_cov,
        explanation_json,
        facts_json,
    ) in cur:
        exp = json.loads(explanation_json)
        # The frozen evidence, which is what the fixture is a de-identified copy of.
        # Preferred over any reconstruction below; see stored_obs.
        frozen = (json.loads(facts_json).get("obs") or {}) if facts_json else {}
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
        # Watch-recency days per user, for the show-shape map. The only thing still read
        # out of the mirror rather than the frozen facts, because it is not a Fact: it
        # describes the show, not the item.
        recency = recency_days(keys, media_type) if keys else []

        ever_obs = stored_obs(frozen, "distinct_watchers_all_time")

        # Every numeric fact, straight from the frozen evidence.
        #
        # These were all reconstructed: dormancy inverted out of a signal's contribution
        # or parsed back out of a humanized gate detail, season rank pulled out of a
        # detail string with a regex. That is a second implementation of the fact layer
        # AND it parses operator-facing copy, so a wording change silently corrupts the
        # fixture. It did, twice, in one session: the rating states collapsed (see
        # stored_obs) and every season's rank fell to Unknown when the season-rank
        # sentence was reworded, quietly deleting 210 known ranks from the sweep.
        #
        # `facts_json` is the evidence the scan actually judged. Read that.
        dormancy = stored_obs(frozen, "days_observed_unwatched", float)
        rating_obs = stored_obs(frozen, "imdb_rating_tenths")
        votes_obs = stored_obs(frozen, "imdb_votes", round_votes)
        rank_obs = stored_obs(frozen, "season_rank", default="absent")

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
                    "distinct_watchers": stored_obs(frozen, "distinct_watchers"),
                    "distinct_watchers_all_time": stored_obs(frozen, "distinct_watchers_all_time"),
                    "size_bytes": stored_obs(frozen, "size_bytes", round_size),
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
            show_watchers.setdefault(group_key, {})[season_number] = ever_obs.get("value") or 0

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
