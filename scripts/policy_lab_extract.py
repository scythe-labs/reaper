# SPDX-License-Identifier: AGPL-3.0-or-later
"""Regenerate the policy-lab fixture from a real library, de-identified by construction.

Reads the newest non-degraded snapshot in ``data/reaper.db`` plus the local mirrors in
``data/cache.db`` and writes ``tests/fixtures/policy_lab_vectors.json``: a stratified
sample of real fact shapes for the permutation harness in
``tests/test_policy_permutations.py``.

The fixture contains, and nothing more:

* observation states (known / absent / unknown) per fact, with numeric values only:
  day counts, watcher counts, season numbers, rating tenths;
* vote counts rounded to two significant figures and sizes to 100 MB, so no value is
  precise enough to identify a file;
* genre names replaced by frequency-ranked tokens (``Genre01``), rare quality names
  collapsed to ``Other``;
* per-show season shapes as ``(season_number, watchers)`` pairs;
* two pinned baselines per vector: ``baseline`` under the shipped default policies, and
  ``lane_baseline`` under ``tests._policy_lab.lane_policy``, which adds the custom
  condemn and graded keep rules the defaults leave empty, so a change to the
  operator-authored lanes moves a pinned number too, instead of moving nothing at all.

No titles, ids, media keys, paths, hosts, or usernames. A fixture is committed to the
repo, so it follows the same rule code does.

Usage: run ``uv run python scripts/policy_lab_extract.py`` from the repo root.

``--rebaseline`` re-pins only the baseline block on the fixture already committed, using
the shapes it already holds. Use this mode after an intentional engine change: it needs
no real library, so CI and every contributor can reproduce it, and it prints every
vector that moved.

Both modes refuse to write a moved baseline while ``SCORER_VERSION`` still reads what
the fixture was cut under, because that combination would leave plans approved under
the old numbers still executable. Bump the constant, or pass ``--unbumped="<why>"``
when no approval is owed a void. The full extract is held to the same bar: it
re-judges the old fixture's vectors before it writes the new sample, since
re-sampling on its own would not excuse skipping the check (see
``guard_the_full_extract``). ``refuse_unless_the_scorer_moved`` is the interlock both
modes call.
"""

from __future__ import annotations

import json
import os
import random
import re
import sqlite3
import sys
from collections import Counter
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "src"))

from tests._policy_lab import (  # noqa: E402
    baseline_differences,
    lane_gates,
    lane_policy,
    pinned_baseline,
    reach_of,
)

from reaper.engine.policy import (  # noqa: E402
    DEFAULT_MOVIE_POLICY,
    DEFAULT_TV_POLICY,
    SCORER_VERSION,
)
from reaper.services.scan_runner import build_gates  # noqa: E402

OUT = REPO / "tests" / "fixtures" / "policy_lab_vectors.json"
TARGET_PER_TYPE = 220
TARGET_SHOWS = 100

#: Where the real library lives. Reads the same environment variable the app does
#: (``launcher.py``). A worktree has no ``data/`` directory of its own, so this extract can
#: only run from the main checkout. Symlinking ``data`` into the worktree is not a safe
#: workaround either: ``.gitignore``'s ``/data/`` entry does not match a symlink, so it shows
#: up untracked and one ``git add -A`` away from being committed. Both databases are opened
#: read-only.
DATA_DIR = Path(os.environ.get("REAPER_DATA_DIR", "").strip() or (REPO / "data"))


def round_votes(votes: int) -> int:
    """Return the count rounded to two significant figures: enough for every vote-floor
    comparison the engine makes, coarse enough to identify nothing."""
    if votes < 100:
        return votes
    magnitude = 10 ** (len(str(votes)) - 2)
    return round(votes / magnitude) * magnitude


def round_size(size: int) -> int:
    return round(size / 100_000_000) * 100_000_000


def obs(state: str, value: Any = None) -> dict[str, Any]:
    return {"state": state, "value": value} if state == "known" else {"state": state}


#: The observation states ``facts_codec._obs_to_dict`` emits. The fixture uses these same
#: three names, so this set is not a translation. It is the allow-list that sends anything
#: unrecognized (a future state, a corrupt row) to the caller's default.
_STATES = frozenset({"known", "absent", "unknown"})


def stored_obs(
    frozen: dict[str, Any], field: str, transform: Any = None, *, default: str = "unknown"
) -> dict[str, Any]:
    """Return one observation, read from the snapshot's frozen facts rather than re-derived.

    Everything else in this file reconstructs facts from ``explanation_json`` and the
    gate details. That is a second implementation of the fact layer, and it can drift
    from the first: production distinguishes "we looked and there is no rating" from
    "we had no id to look one up with" (``display_meta.dataset_lookup``), and a
    reconstruction that collapses both to ``absent`` can never produce an ``Unknown``
    rating, no matter how many scans it reads. Those two states mean opposite things to
    the keep lane, so a harness built that way would test evidence no real scan
    produces.

    ``facts_json`` is the evidence the scan froze, exactly what the fixture wants a
    de-identified copy of. ``transform`` blunts precision on the way out (sizes,
    votes). The caller must tokenize any identifying value before it reaches here,
    never pass one through untouched.
    """
    entry = frozen.get(field)
    if not isinstance(entry, dict):
        return obs(default)
    kind = raw if (raw := str(entry.get("k"))) in _STATES else default
    if kind != "known":
        return obs(kind)
    value = entry.get("v")
    return obs("known", transform(value) if transform is not None else value)


def shown(path: Path) -> str:
    """Return a path for the summary line, repo-relative where that is meaningful.

    ``relative_to`` raises on a path outside the repo instead of returning the
    absolute one. Both writers call this after the fixture is already on disk, so
    without this fallback a fully successful run could end in a traceback and read as
    a failed regeneration. This only matters when ``OUT`` is redirected, which is what
    the tests do; a summary line is never worth crashing over.
    """
    return str(path.relative_to(REPO) if path.is_relative_to(REPO) else path)


def write_fixture(fixture: dict[str, Any]) -> None:
    """Write the fixture to disk. The only writer, and the only place the version stamp is set.

    The full extract's own writer needs a real ``data/reaper.db``, so no test can
    drive it directly. Setting the stamp only here means a stamp added to
    ``rebaseline`` and forgotten in ``main`` cannot happen, since there is only one
    writer to set it in.

    Keep this the only ``OUT.write_text`` in the file. ``test_policy_lab_extract``
    checks that, because a second writer would bring back the split this exists to
    remove.
    """
    fixture["scorer_version"] = SCORER_VERSION
    OUT.write_text(json.dumps(fixture, separators=(",", ":"), sort_keys=True) + "\n")


def stamped_scorer(fixture: dict[str, Any]) -> int | None:
    """Return the ``SCORER_VERSION`` this fixture's baselines were cut under.

    Returns ``None`` when the fixture carries no usable stamp: the field is absent, or
    holds something that is not a version number. ``None`` means "cannot tell", and
    only matters where a caller checks it: ``refuse_unless_the_scorer_moved`` is the
    one caller, and it treats an unstamped fixture whose baselines did not move as
    fine to write with no refusal. That covers every fixture written before this
    mechanism existed, on its first re-pin.

    ``bool`` is excluded on purpose. Python's ``isinstance(True, int)`` is ``True``,
    so a stamp of ``true`` would read back as the integer 1, compare unequal to the
    running constant, and be read as "the scorer moved", the wrong, fail-open
    direction. ``0`` and negative numbers are rejected for the same reason:
    production declares this field ``ge=1`` (``PolicyBody.scorer_version``), so
    nothing below 1 is a real version.
    """
    value = fixture.get("scorer_version")
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        return None
    return value


#: The two baselines every vector carries, and the policy each is judged under.
#: ``baseline`` is the shipped default. ``lane_baseline`` is ``_policy_lab.lane_policy``,
#: which adds the operator-authored rules the defaults leave empty, so ``evaluate_custom``
#: and ``evaluate_keep`` are pinned by something. Declared as one table because both writers
#: and the permutation test all walk it, so a baseline added on one path only cannot happen.
BASELINES: tuple[tuple[str, Any, Any], ...] = (
    ("baseline", lambda mt: DEFAULT_MOVIE_POLICY if mt == "movie" else DEFAULT_TV_POLICY, None),
    ("lane_baseline", lane_policy, lane_gates),
)


def rejudge(fixture: dict[str, Any]) -> int:
    """Re-pin every vector's baselines in place, and return how many moved.

    Shared by both regeneration paths, so the comparison always runs on both. A path
    that skipped it once let a full extract launder an engine change through
    unchecked.

    A baseline is the whole block ``_policy_lab.pinned_baseline`` builds: the
    decision, the three gate lists, and the arithmetic the panel states. So "moved"
    covers a refactor that changed which protections were checked, or what a signal
    contributed, even while the rounded score held still. Adding a field to that
    block moves every baseline at once. That case is not an engine change; re-run
    with ``--unbumped="<why>"``.
    """
    default_gates = {
        "movie": build_gates(DEFAULT_MOVIE_POLICY),
        "season": build_gates(DEFAULT_TV_POLICY),
    }
    # The reach of the sample being judged, not of whatever fixture is on disk. The lab
    # derives ``history_reach_days`` from the oldest play across the vectors, and caches it
    # at module level off ``load_fixture()``. Judging a new sample against a cached reach
    # from the previous fixture would silently disagree with what the permutation test
    # computes when it reloads the new one.
    reach = reach_of(fixture["vectors"])
    moved = 0
    first_pinned = 0
    for v in fixture["vectors"]:
        media_type = v["media_type"]
        for key, policy_for, gates_for in BASELINES:
            policy = policy_for(media_type)
            gates = gates_for(media_type) if gates_for else default_gates[media_type]
            fresh = pinned_baseline(v, policy, gates, reach=reach)
            was = v.get(key)
            # A key the fixture never carried has not moved: there was no number to move
            # from. Counting it as movement would trip the refusal for every vector
            # whenever a baseline is added, even though the engine never changed, and an
            # escape hatch people reach for by reflex stops being read. Only a value that
            # existed before and differs now counts as a scoring change.
            if was is None:
                first_pinned += 1
            elif fresh != was:
                # Reported leaf by leaf, not block by block. A whole-block dump at this
                # size scrolls past and hides which signal moved, and the author reads this
                # output to decide whether the change was the one they meant to make.
                for line in baseline_differences(was, fresh):
                    print(f"{v['id']} {key}.{line}")
                moved += 1
            v[key] = fresh
    if first_pinned:
        print(f"{first_pinned} baseline(s) pinned for the first time; nothing to compare")
    return moved


def refuse_unless_the_scorer_moved(moved: int, was: int | None, unbumped: str | None) -> None:
    """The interlock. Refuses to write a moved baseline under a scorer that never bumped.

    Printing the moved vectors used to be the only safeguard, and printing is not a
    gate. An author could regenerate, watch the suite go green, leave
    ``SCORER_VERSION`` in place, and leave every pending approval bound to a
    ``policy_hash`` this build still computes, so a run on the Reap page would
    execute on scores this build no longer produces. Only a regeneration step can
    catch this, since only it holds the old baseline and the new one at once. A test
    comparing them only knows they disagree.

    A stamp ahead of the running constant is refused outright, whether or not
    anything moved. That fixture came from a newer build, and production makes the
    same call on the same value: ``PolicyBody.scorer_version`` is
    ``le=SCORER_VERSION``, so a body from a newer Reaper is refused because this
    build cannot interpret it. Writing here would silently re-stamp it backward and
    destroy the only evidence of where it came from.
    """
    if was is not None and was > SCORER_VERSION:
        sys.exit(
            f"refusing to re-pin: this fixture was cut under scorer {was} and this build runs "
            f"{SCORER_VERSION}, so it came from a newer Reaper than the one you are holding. "
            "Re-pinning would stamp it backwards and lose that. Check out the build that cut "
            "it, or take the fixture from the branch that matches this one."
        )

    # Strictly less than, not merely different. Using ``!=`` would read a stamp ahead of
    # the running constant as a bump, which grants permission it should not have. The
    # exit above already rejects that case first, so today the two spellings behave the
    # same. This stays ``<`` because that safety depends on the exit above staying in
    # place, not on this line, and a future edit that softens the exit would silently
    # bring back the fail-open case.
    scorer_moved = was is not None and was < SCORER_VERSION
    if moved and not scorer_moved and unbumped is None:
        sys.exit(
            f"refusing to re-pin: {moved} baseline(s) moved and SCORER_VERSION is still "
            f"{SCORER_VERSION}, the version these were cut under. A plan approved under the "
            "old numbers is bound to a policy_hash this build still computes, so it would "
            "execute on scores this build would not produce (rule 113).\n"
            "Bump SCORER_VERSION in src/reaper/engine/policy.py and run this again, which "
            "voids those approvals and asks every operator to re-scan.\n"
            'Or re-run with --unbumped="<why>" if no approval is owed a void. Real cases, '
            "all four seen: a shipped DEFAULT policy moved, so no operator's stored body "
            "changed; a loader shim already rewrites every affected body; a PolicyBody field "
            "was added or removed, which moves every stored hash on its own; or only the "
            "harness that judges these vectors changed (tests/_policy_lab.py), so the engine "
            "did not move at all. A second engine change inside one release is also fine if "
            "the release already bumped once: this compares against the fixture, not the tag."
        )


def rebaseline(unbumped: str | None = None) -> None:
    """Re-pin the baseline block on the fixture already committed, without a real library.

    The vectors are de-identified fact shapes and do not change when the engine does.
    Only ``baseline``, the engine's own output under the shipped defaults, does.
    Without this mode, an intentional engine change could only be re-pinned by
    someone with a real ``data/reaper.db``, which rules out CI and every contributor
    who is not the operator.
    """
    fixture = json.loads(OUT.read_text())
    if not fixture.get("vectors"):
        sys.exit(
            "refusing to re-pin: the fixture carries no vectors, so nothing can be compared "
            "and a clean run here would mean only that there was nothing to check."
        )

    was = stamped_scorer(fixture)
    moved = rejudge(fixture)
    refuse_unless_the_scorer_moved(moved, was, unbumped)

    # The note explains the last cut whose baselines moved without a bump, so it is
    # written or cleared only when baselines actually moved. Recording one on a run that
    # changed nothing would justify nothing, and clearing one on such a run would drop
    # the justification for baselines still sitting in the file.
    if moved and unbumped is not None:
        fixture["scorer_note"] = unbumped
    elif moved:
        fixture.pop("scorer_note", None)
    write_fixture(fixture)
    # Counts baselines, not vectors: each vector carries one per entry in ``BASELINES``,
    # so counting vectors here would print a total higher than the vector count and read
    # as a broken counter.
    total = len(fixture["vectors"]) * len(BASELINES)
    print(f"re-pinned {shown(OUT)}: {moved} of {total} baselines moved")


#: What a ``--unbumped`` reason may contain: letters, spaces, and light punctuation, no
#: digits, no paths, no ``@``. The fixture is committed, so it follows the same rule code
#: does: never a real title, host, path, username, or measurement. This is the first
#: free-text field a human types straight into that file, so the charset is checked here,
#: at the boundary, instead of being left for a reviewer to notice.
_REASON_ALLOWED = re.compile(r"^[A-Za-z][A-Za-z_ ,;:'()-]+$")


def check_reason(raw: str) -> str:
    """Return a stated reason, or exit. ``--unbumped=`` states nothing and ``--unbumped=x``
    states barely more, and this flag exists precisely to make someone state the case."""
    reason = raw.strip()
    if not reason:
        sys.exit(
            "--unbumped needs a reason. Say what invalidated the approvals instead of the "
            "scorer bump, in a phrase the next reader can check."
        )
    if len(reason) < 12 or " " not in reason:
        sys.exit(
            f"--unbumped reason {reason!r} is too short to check. Write a phrase, not a "
            "token: what changed, and why no approval is owed a void."
        )
    if not _REASON_ALLOWED.match(reason):
        sys.exit(
            f"--unbumped reason {reason!r} carries characters this fixture may not: letters, "
            "spaces, underscores and , ; : ' ( ) - only. It is committed, so no numbers, "
            "paths, hosts or titles. Describe the kind of change, never a measurement."
        )
    return reason


def parse_argv(argv: list[str]) -> tuple[bool, str | None]:
    """Return ``(rebaseline, unbumped)``. Exits on anything unrecognized.

    A near-miss like ``--rebaseline=true`` or ``--re-baseline`` must never fall through
    to the full extract, since that path runs against a live library with no interlock,
    and a typo is most likely exactly when someone is retyping this command line after
    the refusal asked them to. ``--unbumped`` accepts both ``--unbumped="<why>"`` and
    ``--unbumped <why>``, since one of those is how most other CLIs work, and silently
    dropping the reason someone typed would lose the justification they believed they
    gave.
    """
    rebaseline_mode = False
    unbumped: str | None = None
    index = 0
    while index < len(argv):
        arg = argv[index]
        if arg == "--rebaseline":
            rebaseline_mode = True
        elif arg.startswith("--unbumped="):
            unbumped = check_reason(arg.removeprefix("--unbumped="))
        elif arg == "--unbumped":
            if index + 1 >= len(argv):
                sys.exit('--unbumped needs a reason: --unbumped="<why>"')
            index += 1
            unbumped = check_reason(argv[index])
        else:
            sys.exit(
                f"unrecognized argument {arg!r}.\n"
                'Usage: policy_lab_extract.py [--rebaseline] [--unbumped="<why>"]\n'
                "With no flags it re-extracts from a real data/reaper.db."
            )
        index += 1
    return rebaseline_mode, unbumped


def guard_the_full_extract(unbumped: str | None) -> None:
    """Hold the full extract to the same bar as ``--rebaseline``.

    Re-sampling makes the new vectors incomparable to the old ones, but the old
    fixture and its baselines are still on disk when the new one is written.
    Re-judging those old vectors under the current engine answers the same "did the
    scorer move" question and needs no database, so this path checks it the same way
    ``--rebaseline`` does.

    This matters more here, not less: the only person who can run a full extract is
    the operator with a real library, who is also the person most likely to be
    regenerating right after an engine change.
    """
    if not OUT.exists():
        return
    old = json.loads(OUT.read_text())
    if not old.get("vectors"):
        return
    moved = rejudge(old)
    refuse_unless_the_scorer_moved(moved, stamped_scorer(old), unbumped)


def main() -> None:
    rebaseline_mode, unbumped = parse_argv(sys.argv[1:])
    if rebaseline_mode:
        rebaseline(unbumped)
        return

    guard_the_full_extract(unbumped)

    rng = random.Random(42)
    rdb = sqlite3.connect(f"file:{DATA_DIR / 'reaper.db'}?mode=ro", uri=True)
    cdb = sqlite3.connect(f"file:{DATA_DIR / 'cache.db'}?mode=ro", uri=True)

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
        """Return days since each distinct viewer last played any of ``keys``, ascending.

        The only value this script still derives from the watch mirror. Dormancy,
        watcher counts, and every other fact come from the snapshot's frozen facts
        (``stored_obs``).
        """
        user_last = movie_user_last if media_type == "movie" else season_user_last
        merged: dict[int, int] = {}
        for key in keys:
            for u, last in user_last.get(key, {}).items():
                if u not in merged or last > merged[u]:
                    merged[u] = last
        return sorted(float(round((created_at - la) / 86400.0)) for la in merged.values())

    # Ratings are read from the frozen facts (stored_obs), never rebuilt from the cache.
    # A lookup against the cache can only tell known from absent, while the frozen facts
    # also carry unknown, so rebuilding from the cache would lose a real state.

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
        # The frozen evidence. This is what the fixture is a de-identified copy of, and
        # it is preferred over any reconstruction below (see stored_obs).
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
        # Watch-recency days per user, for the show-shape map. The only value still read
        # from the mirror instead of the frozen facts, because it is not a Fact: it
        # describes the show, not the item.
        recency = recency_days(keys, media_type) if keys else []

        ever_obs = stored_obs(frozen, "distinct_watchers_all_time")

        # Every numeric fact, straight from the frozen evidence.
        #
        # Reconstructing facts by parsing operator-facing text is fragile: dormancy
        # inverted out of a signal's contribution, or season rank pulled from a detail
        # string with a regex, both depend on wording that can change. A reworded
        # sentence would silently corrupt the fixture instead of raising an error.
        #
        # `facts_json` is the evidence the scan actually judged. Read that instead.
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
                    # These three fields must come from the frozen facts, not hand-written
                    # constants. A constant pinning `requested` and `show_ended` to unknown,
                    # with no arrival date at all, would sweep evidence no real scan
                    # produces, even though the scan actually froze all three for every
                    # candidate.
                    "requested": stored_obs(frozen, "requested"),
                    "days_since_added": stored_obs(frozen, "days_since_added", float),
                    "genres": genres_obs,
                    "release_age_days": release_obs,
                    "quality": quality_obs,
                    "show_ended": stored_obs(frozen, "show_ended", default="absent"),
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
    for i, v in enumerate(sample):
        v.pop("_group", None)
        v["id"] = f"v{i:04d}"
    # Runs through the same shared table used by the re-pin path, so a baseline can never
    # exist on one path and not the other. Every key starts absent here, so every
    # baseline is pinned for the first time rather than counted as moved.
    rejudge({"vectors": sample})

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
    # ``scorer_version`` is stamped by the writer (write_fixture), not set here directly.
    # The scorer-moved check for this path already ran earlier, in guard_the_full_extract.
    write_fixture(out)
    by_type = Counter(v["media_type"] for v in sample)
    print(f"wrote {shown(OUT)}: {dict(by_type)} vectors, {len(shows)} show shapes")


if __name__ == "__main__":
    main()
