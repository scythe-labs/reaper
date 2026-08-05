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
* two pinned baselines per vector: ``baseline`` under the SHIPPED default policies, and
  ``lane_baseline`` under ``tests._policy_lab.lane_policy``, which adds the custom condemn
  and graded keep rules the defaults leave empty -- so a change to the operator-authored
  lanes moves a pinned number too, rather than moving nothing at all.

No titles, ids, media keys, paths, hosts, or usernames -- the golden rule applies to
fixtures exactly as it does to code.

Usage: ``uv run python scripts/policy_lab_extract.py`` from the repo root.

``--rebaseline`` re-pins only the baseline block on the fixture already committed, using
the shapes it already holds. That is the mode to use after an intentional engine change:
it needs no real library, so CI and every contributor can reproduce it, and it prints
every vector that moved.

**Both modes refuse** to write a moved baseline while ``SCORER_VERSION`` still reads what the
fixture was cut under, because that combination leaves plans approved under the old numbers
executable (rule 113). Bump the constant, or pass ``--unbumped="<why>"`` when no approval is
owed a void. The full extract is held to the same bar by re-judging the OLD fixture's vectors
before it writes the new sample -- see ``guard_the_full_extract`` for why re-sampling does not
excuse it. ``refuse_unless_the_scorer_moved`` is the interlock both call.
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

from tests._policy_lab import judge, lane_gates, lane_policy, reach_of  # noqa: E402

from reaper.engine.policy import (  # noqa: E402
    DEFAULT_MOVIE_POLICY,
    DEFAULT_TV_POLICY,
    SCORER_VERSION,
)
from reaper.services.scan_runner import build_gates  # noqa: E402

OUT = REPO / "tests" / "fixtures" / "policy_lab_vectors.json"
TARGET_PER_TYPE = 220
TARGET_SHOWS = 100

#: Where the real library lives, honoring the same env var the app does
#: (``launcher.py``). A worktree has no ``data/`` of its own, so the extract could only be
#: run from the main checkout -- and the alternative, symlinking ``data`` into the worktree,
#: is a directory-shaped entry that ``.gitignore``'s ``/data/`` does not match, so it shows
#: up untracked and one ``git add -A`` from being committed. Both databases are opened
#: read-only.
DATA_DIR = Path(os.environ.get("REAPER_DATA_DIR", "").strip() or (REPO / "data"))


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


def shown(path: Path) -> str:
    """A path for the summary line, repo-relative where that is meaningful.

    ``relative_to`` RAISES on a path outside the repo rather than returning the absolute
    one, and both writers call it *after* the fixture is on disk -- so a run that fully
    succeeded would end in a traceback and read as a failed regeneration. Only reachable
    with ``OUT`` redirected, which is what the tests do, and a summary line is never worth
    a crash.
    """
    return str(path.relative_to(REPO) if path.is_relative_to(REPO) else path)


def write_fixture(fixture: dict[str, Any]) -> None:
    """The one place the fixture reaches disk, and the one place the stamp is set.

    Rule 72 asks for the sibling to be swept, and the sibling here is unreachable from a
    test: the full extract needs a real ``data/reaper.db``, so a stamp set in ``rebaseline``
    and forgotten in ``main`` would be invisible to the whole suite -- mutation-tested, and
    it was. Setting it here instead makes that defect unconstructible rather than merely
    covered, which is the better trade whenever it is available.

    Keep this the only ``OUT.write_text`` in the file; ``test_policy_lab_extract`` pins that,
    because a second writer would restore the split this exists to remove.
    """
    fixture["scorer_version"] = SCORER_VERSION
    OUT.write_text(json.dumps(fixture, indent=1, sort_keys=True) + "\n")


def stamped_scorer(fixture: dict[str, Any]) -> int | None:
    """The ``SCORER_VERSION`` this fixture's baselines were cut under, or ``None`` when it
    carries no usable stamp: absent, or present as something that is not a version number.

    ``None`` means "cannot tell", which is fail-closed *only where a caller checks it* --
    ``refuse_unless_the_scorer_moved`` does, and it is the one caller. Note what that does
    NOT say: an unstamped fixture whose baselines did not move is written and stamped with
    no refusal at all, which is deliberate, because that is every fixture predating this
    mechanism on its first re-pin.

    ``bool`` is excluded by hand. ``isinstance(True, int)`` is ``True`` in Python, so a
    stamp of ``true`` read back as the integer 1 and compared unequal to the running
    constant -- which this file's refusal reads as "the scorer moved", the fail-open
    direction. ``0`` and negatives are rejected for the same reason: production declares
    this field ``ge=1`` (``PolicyBody.scorer_version``) and nothing below 1 is a version.
    """
    value = fixture.get("scorer_version")
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        return None
    return value


#: The two baselines every vector carries, and the policy each is judged under. ``baseline``
#: is the shipped default; ``lane_baseline`` is ``_policy_lab.lane_policy``, which adds the
#: operator-authored rules the defaults leave empty, so ``evaluate_custom`` and
#: ``evaluate_keep`` are pinned by something. Declared as one table because both writers and
#: the permutation test all walk it, and a second baseline added on one path only would be
#: the ``scorer_version`` split all over again (rule 72).
BASELINES: tuple[tuple[str, Any, Any], ...] = (
    ("baseline", lambda mt: DEFAULT_MOVIE_POLICY if mt == "movie" else DEFAULT_TV_POLICY, None),
    ("lane_baseline", lane_policy, lane_gates),
)


def rejudge(fixture: dict[str, Any]) -> int:
    """Re-pin every vector's baselines in place; return how many moved.

    Shared by both regeneration paths so the comparison cannot exist on one and not the
    other, which is exactly the split that let the full extract launder an engine change.
    """
    default_gates = {
        "movie": build_gates(DEFAULT_MOVIE_POLICY),
        "season": build_gates(DEFAULT_TV_POLICY),
    }
    # The reach of the sample being judged, not of whatever fixture is on disk. The lab
    # derives ``history_reach_days`` from the oldest play across the vectors, cached at
    # module level off ``load_fixture()`` -- so a full extract judged its new sample against
    # the PREVIOUS fixture's reach and wrote that as ground truth, then the permutation test
    # recomputed with the new one and disagreed. Exactly what ``judge``'s docstring warns a
    # generator can do: a lab that disagrees with the scan does not fail, it records the
    # disagreement. Pre-existing, and it surfaced the first time anyone ran a full extract
    # after the reach was introduced -- the two samples differ by eighteen days.
    reach = reach_of(fixture["vectors"])
    moved = 0
    first_pinned = 0
    for v in fixture["vectors"]:
        media_type = v["media_type"]
        for key, policy_for, gates_for in BASELINES:
            policy = policy_for(media_type)
            gates = gates_for(media_type) if gates_for else default_gates[media_type]
            verdict, score_value, coverage_bp = judge(v, policy, gates, reach=reach)[:3]
            fresh = {"verdict": verdict, "score": score_value, "coverage_bp": coverage_bp}
            was = v.get(key)
            # A key the fixture never carried has not MOVED -- there was no number to move
            # from. Counting it as movement makes every added baseline trip the refusal for
            # every vector at once, which teaches people to reach for --unbumped on a change
            # that never touched the engine, and an escape hatch used by reflex is one that
            # stops being read. Only a value that existed and now differs is a scoring
            # change (rule 93's distinction: absent is not the same as unreadable, and
            # neither is the same as changed).
            if was is None:
                first_pinned += 1
            elif fresh != was:
                print(f"{v['id']} {key}: {was} -> {fresh}")
                moved += 1
            v[key] = fresh
    if first_pinned:
        print(f"{first_pinned} baseline(s) pinned for the first time; nothing to compare")
    return moved


def refuse_unless_the_scorer_moved(moved: int, was: int | None, unbumped: str | None) -> None:
    """The interlock. Exits rather than letting a moved baseline be written under a scorer
    that stood still.

    Printing the moved vectors was the whole safeguard before this, and printing is not a
    gate: the author regenerated, the suite went green, ``SCORER_VERSION`` stayed put, and
    every pending approval stayed bound to a ``policy_hash`` this build still computes -- so
    the run on the Reap page executes on scores this build would not produce (rule 113).
    Only a regeneration step can tell, because only it holds the old baseline and the new one
    at once; a test comparing them knows they disagree and nothing more.

    A stamp AHEAD of the running constant is refused outright, whether or not anything moved.
    That fixture came from a newer build, and production makes the same call on the same value
    (``PolicyBody.scorer_version`` is ``le=SCORER_VERSION``: "a body from a newer Reaper is
    refused, because we genuinely cannot interpret it"). Writing would silently re-stamp it
    DOWNWARD and destroy the only evidence of where it came from.
    """
    if was is not None and was > SCORER_VERSION:
        sys.exit(
            f"refusing to re-pin: this fixture was cut under scorer {was} and this build runs "
            f"{SCORER_VERSION}, so it came from a newer Reaper than the one you are holding. "
            "Re-pinning would stamp it backwards and lose that. Check out the build that cut "
            "it, or take the fixture from the branch that matches this one."
        )

    # Strictly BELOW, not merely different. ``!=`` read a stamp AHEAD of the running
    # constant as a bump, which is permission. The exit above now rejects that case first,
    # so the two spellings are equivalent as this function stands -- mutation-tested, and
    # swapping them back turns nothing red. It stays ``<`` because the equivalence is a
    # property of that exit, not of this line, and a later edit that softens the exit would
    # hand the fail-open back with no test to notice.
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

    The vectors are de-identified fact *shapes* and do not change when the engine does;
    only ``baseline`` (the engine's own output under the shipped defaults) does. An
    intentional engine change would otherwise be un-re-pinnable by anyone without a real
    ``data/reaper.db`` -- i.e. by CI, and by every contributor who is not the operator.
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

    # The note explains the last cut whose baselines moved without a bump, so it is only
    # written and only cleared when baselines actually moved. Recording one on a run that
    # changed nothing justifies nothing, and clearing one on such a run would drop the
    # justification for the baselines still sitting in the file.
    if moved and unbumped is not None:
        fixture["scorer_note"] = unbumped
    elif moved:
        fixture.pop("scorer_note", None)
    write_fixture(fixture)
    print(f"re-pinned {shown(OUT)}: {moved} of {len(fixture['vectors'])} vectors moved")


#: What a ``--unbumped`` reason may contain. Letters, spaces and light punctuation -- no
#: digits, no paths, no ``@``. The fixture is committed and the golden rule binds it as it
#: binds code: never a real title, host, path, username or **stat**. This is the first
#: free-text field a human types straight into that file, so the charset is where it is
#: caught, at the boundary, rather than left to a reviewer noticing.
_REASON_ALLOWED = re.compile(r"^[A-Za-z][A-Za-z_ ,;:'()-]+$")


def check_reason(raw: str) -> str:
    """A stated reason, or exit. ``--unbumped=`` states nothing and ``--unbumped=x`` states
    barely more, and the flag exists precisely to make someone state the case."""
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
    """``(rebaseline, unbumped)``. Anything unrecognized exits.

    A near miss used to fall straight through to the full extract: ``--rebaseline=true`` and
    ``--re-baseline`` both ran the path with no interlock, against a live library, and the
    refusal's own message asks the author to retype this command line -- which is when a typo
    happens. Both spellings of the flag are accepted because one of them is how every other
    CLI works and silently dropping it loses the justification the author believed they gave.
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

    This path used to be exempt, on the reasoning that "a full extract re-samples different
    shapes from a live library, so 'the baseline moved' is not a question it can ask." That
    was wrong, and measured: the OLD fixture and its baselines are still on disk when the new
    one is written, so re-judging *those* vectors under the current engine answers exactly
    the same question and needs no database. With a ramp change, ``--rebaseline`` refused 338
    moved baselines while this path wrote all 338 and re-stamped the scorer. Re-sampling makes
    the NEW vectors incomparable; it says nothing about the old ones.

    It matters more here than there, not less: the only person who can run a full extract is
    the operator with a real library, who is also the person most likely to be regenerating
    after an engine change.
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
                    # These three were hand-written constants -- ``requested`` and
                    # ``show_ended`` pinned to unknown, and no arrival date at all -- while
                    # the scan had frozen all three for every candidate. The lab was
                    # sweeping evidence no real scan produces, which is the failure
                    # ``stored_obs`` was written for, one field list later. Read what was
                    # frozen (rule 35).
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
    # One pass through the shared table, so a baseline can never exist on the re-pin path
    # and not on the extract path. Every key starts absent, so all of them read as moved.
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
    # ``scorer_version`` is stamped by the writer, not spelled here. No refusal on this path
    # either: a full extract re-samples different shapes from a live library, so "the
    # baseline moved" is not a question it can ask.
    write_fixture(out)
    by_type = Counter(v["media_type"] for v in sample)
    print(f"wrote {shown(OUT)}: {dict(by_type)} vectors, {len(shows)} show shapes")


if __name__ == "__main__":
    main()
