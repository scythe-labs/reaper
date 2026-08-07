# SPDX-License-Identifier: AGPL-3.0-or-later
"""Freeze what Reaper concludes about a WHOLE real library, de-identified by construction.

Tier B of ``docs/SIMPLIFICATION_PLAN.md``'s behavioral baseline. ``tests/_policy_lab.py``
replays 440 sampled fact vectors through ``judge_facts`` and pins the judgment per vector;
that is the CI-enforced tier and it samples. This one reads every candidate in one stored
snapshot and records, per item:

* a digest of ``Candidate.facts_json``, the evidence the scan froze;
* the verdict triple the scan stored;
* the plan ``services.planner.build_plan`` builds from that snapshot -- its ordered items,
  the ordinal each was given, and the manifest hash the executor would check.

``Snapshot.evidence_hash`` is **not** the first of those and is deliberately absent here: it
hashes the policy fields that decide what gathering asks for, there is one per snapshot, and
it is constant while the policy is -- so a refactor that changed what gathering produced would
not move it at all.

**Read-only, and it never re-scans.** The source database is opened ``mode=ro`` and copied;
everything else runs against the copy, because ``build_plan`` writes a run row and its steps
and the operator's database must not carry a plan nobody asked for. A re-scan is excluded for
a different reason: it is wall-clock dependent, and advancing only the clock by 30 days moves
45% of score lines against a library and a build that never changed.

**Diff it at phase boundaries, never per pull request**, and read the item lines as counts and
set membership rather than line by line.

Usage: ``uv run python scripts/baseline_capture.py`` from the repo root, against the same
snapshot the committed capture names. ``--snapshot <id>`` re-bases onto a different one, which
is a deliberate act: every item id is positional, so a capture of a different snapshot moves
every line and the diff answers nothing.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import sqlite3
import sys
import tempfile
from pathlib import Path
from typing import Any

from sqlalchemy import select

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "src"))

from reaper.config import Settings  # noqa: E402
from reaper.db.models import ActionStep  # noqa: E402
from reaper.db.session import create_engine, create_session_factory  # noqa: E402
from reaper.engine.policy import SCORER_VERSION  # noqa: E402
from reaper.logging import configure_logging  # noqa: E402
from reaper.services.planner import PlanError, build_plan  # noqa: E402
from reaper.services.profiles import active_profile  # noqa: E402

OUT = REPO / "tests" / "fixtures" / "whole_library_baseline.json"

#: Where the real library lives, honoring the same env var the app does. Same reasoning as
#: ``policy_lab_extract``: a worktree has no ``data/`` of its own.
DATA_DIR = Path(os.environ.get("REAPER_DATA_DIR", "").strip() or (REPO / "data"))

#: The name a copied database must carry, because ``Settings.database_url`` builds the URL
#: from the data directory rather than taking a path.
DB_NAME = "reaper.db"

NOTE = (
    "What Reaper concludes about one whole real library, de-identified by construction. "
    "Regenerate with scripts/baseline_capture.py. Item ids are positional over the "
    "snapshot's candidates ordered by media key, so they are stable across captures of the "
    "same snapshot and meaningless across captures of different ones."
)


def digest(value: str | None) -> str | None:
    """A short one-way digest, or ``None`` where there was nothing to digest.

    Twelve hex characters. The capture is a change detector, so the question it asks of this
    field is only ever "is it the same as last time"; a full digest would quadruple the file
    for no extra answer. ``None`` rather than a digest of the empty string, because a
    candidate with no frozen facts and one whose facts are empty are different states and
    rule 93's distinction binds a baseline as much as a fact.
    """
    if value is None:
        return None
    return hashlib.sha256(value.encode()).hexdigest()[:12]


#: The ``ActionStep.kind`` values ``services.planner`` writes. Hand-written here and
#: reconciled by ``test_baseline_capture`` against the literals in ``planner.py``, which is
#: rule 103's second option: there is no declaration to derive from, so the drift guard is a
#: test that fails when the planner's set changes. Guessing them is not free -- the first
#: version of this list guessed four names and the write refused, which is the guard working
#: and is also why it is not left to a reviewer.
_STEP_KINDS = frozenset(
    {
        "radarr_delete",
        "sonarr_unmonitor",
        "sonarr_verify_unmonitor",
        "sonarr_delete_files",
    }
)

#: What ``decide_verdict`` returns. Also hand-written, also reconciled by the same test.
#: W4.3 of the simplification plan proposes typing this as a ``Literal``; when that lands,
#: derive from it and delete the guard.
_VERDICTS = frozenset({"condemn", "abstain", "protect"})

#: Every string this capture may contain, beyond an item id and a hex digest. Anything else
#: is refused before the file is written, which is what makes committing a title
#: unconstructible rather than merely unlikely -- three of ``build_plan``'s refusals name
#: media keys in their message, and this file must never carry one.
_VOCABULARY = frozenset({NOTE, "movie", "season", "planned", "refused"}) | _STEP_KINDS | _VERDICTS

_ITEM_ID = re.compile(r"^i\d{4,}$")
_DIGEST = re.compile(r"^[0-9a-f]{8,64}$")


def offending_strings(payload: Any, path: str = "") -> list[str]:
    """Every string leaf this file may not carry, with the path that produced it.

    The extractor scans the one free-text field a human types into its fixture. This scans
    **everything**, because nothing here is typed by hand and so nothing here has a reason to
    be prose: an item id, a media type, a verdict, a step kind, a hex digest, and the note.
    A media key, a title, a path or an *arr's own row id reaching this list is the golden
    rule broken, and the write is refused rather than reviewed.

    Numbers, booleans and nulls pass without inspection. A count is a stat about the
    operator's library in the strictest reading, but the capture is counts by construction --
    ratios and shapes is exactly what it is -- and refusing them would refuse the file.
    """
    if isinstance(payload, dict):
        return [
            bad
            for key, value in payload.items()
            for bad in offending_strings(value, f"{path}.{key}" if path else str(key))
        ]
    if isinstance(payload, list):
        return [
            bad
            for index, value in enumerate(payload)
            for bad in offending_strings(value, f"{path}[{index}]")
        ]
    if not isinstance(payload, str):
        return []
    if payload in _VOCABULARY or _ITEM_ID.match(payload) or _DIGEST.match(payload):
        return []
    return [f"{path}: {payload!r}"]


def write_capture(payload: dict[str, Any]) -> None:
    """The one place the capture reaches disk, and the one place the guard runs.

    Same shape as ``policy_lab_extract.write_fixture`` and for the same reason: a second
    writer would be a second path where the guard is not, and the sibling that skipped it
    would be the one run against a real library. ``test_baseline_capture`` pins that this
    stays the only writer.
    """
    if offenders := offending_strings(payload):
        sys.exit(
            "refusing to write: the capture carries strings that are not an item id, a "
            "digest, or a known term, and this file is committed.\n" + "\n".join(offenders[:10])
        )
    OUT.write_text(json.dumps(payload, indent=1, sort_keys=True) + "\n")


def committed_snapshot_id() -> int | None:
    """The snapshot the capture on disk was cut against, or ``None`` if there is none yet."""
    if not OUT.exists():
        return None
    stored = json.loads(OUT.read_text()).get("snapshot")
    value = stored.get("id") if isinstance(stored, dict) else None
    return value if isinstance(value, int) else None


def choose_snapshot(conn: sqlite3.Connection, asked: int | None) -> int:
    """Which snapshot to capture, refusing the choice that makes the diff meaningless.

    Item ids are positional, so a capture against a different snapshot moves every line in
    the file at once -- and S8 reads any unexplained movement as a stop. Re-basing is a real
    thing to want (the audit outlives a scan schedule), so it is available and it is
    deliberate rather than what happens by default.
    """
    committed = committed_snapshot_id()
    wanted = asked if asked is not None else committed
    if wanted is None:
        row = conn.execute(
            "SELECT id FROM snapshot WHERE degraded = 0 ORDER BY id DESC LIMIT 1"
        ).fetchone()
        if row is None:
            sys.exit("no non-degraded snapshot to capture from")
        return int(row[0])
    if conn.execute("SELECT 1 FROM snapshot WHERE id = ?", (wanted,)).fetchone() is None:
        sys.exit(
            f"snapshot {wanted} is not in this database. The committed capture was cut "
            "against it, so re-capturing here would compare two different libraries. Pass "
            "--snapshot <id> to re-base onto one this database has, knowing every item id "
            "moves."
        )
    if committed is not None and wanted != committed:
        print(
            f"re-basing from snapshot {committed} onto {wanted}: every item id is positional, "
            "so expect the whole file to move and do not read this diff as a regression"
        )
    return wanted


def read_items(
    conn: sqlite3.Connection, snapshot_id: int
) -> tuple[list[dict[str, Any]], dict[str, str]]:
    """One row per candidate, plus the media key to item id map the plan is expressed in.

    Ordered by media key, which is what makes the positional ids stable: it is a total order
    over a frozen set of rows, so the same snapshot yields the same ids on every capture.
    ``build_plan`` orders its own condemned read the same way, for an unrelated reason.
    """
    items: list[dict[str, Any]] = []
    ids: dict[str, str] = {}
    rows = conn.execute(
        "SELECT media_key, media_type, verdict, score, coverage_bp, facts_json "
        "FROM candidate WHERE snapshot_id = ? ORDER BY media_key",
        (snapshot_id,),
    )
    for index, (media_key, media_type, verdict, score, coverage_bp, facts_json) in enumerate(rows):
        item_id = f"i{index:04d}"
        ids[media_key] = item_id
        items.append(
            {
                "id": item_id,
                "media_type": media_type,
                "verdict": verdict,
                "score": score,
                "coverage_bp": coverage_bp,
                "facts": digest(facts_json),
            }
        )
    return items, ids


async def _plan(data_dir: Path, snapshot_id: int, ids: dict[str, str]) -> dict[str, Any]:
    """``build_plan`` against the COPY, rolled back, reported by item id.

    Production's own planner, never a re-derivation of it (rule 3/22): the ordering, the
    canary seat, the unmeasured allowance and the manifest hash are the things this capture
    exists to freeze, and a lookalike would freeze the lookalike.

    The rollback is belt to the copy's braces. Nothing here should outlive the process
    either way, and the two together mean a bug in this script cannot leave an approved run
    in a database -- not the operator's, which is never opened for writing, and not the copy,
    which is deleted.
    """
    settings = Settings(data_dir=data_dir)
    engine = create_engine(settings)
    try:
        async with create_session_factory(engine)() as session:
            profile = await active_profile(session)
            try:
                run = await build_plan(
                    session,
                    snapshot_id=snapshot_id,
                    approved_by="baseline-capture",
                    max_unmeasured=profile.settings.max_unmeasured_per_run,
                )
            except PlanError as exc:
                # Printed, never written. Three of ``build_plan``'s refusals name the media
                # keys they refused on, so putting this message in a committed file would
                # leak exactly what the rest of this script exists to keep out. The operator
                # running the capture reads the reason here; the file records that there was
                # one.
                print(f"build_plan refused: {exc}")
                return {"outcome": "refused"}
            steps = (
                await session.execute(
                    select(ActionStep.media_key, ActionStep.kind)
                    .where(ActionStep.run_id == run.id)
                    .order_by(ActionStep.ordinal, ActionStep.id)
                )
            ).all()
            ordered: list[str] = []
            kinds: set[str] = set()
            for media_key, kind in steps:
                item = ids[media_key]
                if item not in ordered:
                    ordered.append(item)
                kinds.add(kind)
            plan = {
                "outcome": "planned",
                # Position IS the ordinal: ``build_plan`` increments it once per item that
                # got steps, so the item at index 0 is the canary -- the smallest measured
                # item, sent and verified alone before anything else may run.
                "items_in_ordinal_order": ordered,
                "step_kinds": sorted(kinds),
                "manifest": run.approved_manifest_hash,
                "policy": run.policy_hash,
                "held_back_unknown_size": run.held_back_unknown_size,
                "max_unmeasured": profile.settings.max_unmeasured_per_run,
                # The route that plans for real refuses on this, because a run bounded by
                # numbers nobody chose is not a run the operator approved (rule 65/91). A
                # capture is evidence rather than an action, so it records the state instead
                # -- and records it, rather than logging it, because the file is what a later
                # session reads and a True here explains a plan that would otherwise look
                # like a regression.
                "settings_fell_back": profile.repaired,
            }
            # Never committed. ``build_plan`` flushes rather than commits, so this is what
            # keeps the run row out of even the throwaway copy.
            await session.rollback()
            return plan
    finally:
        await engine.dispose()


def migrate_the_copy(data_dir: Path) -> str:
    """``alembic upgrade head`` against the COPY, returning the revision it reached.

    The operator's database sits wherever their last boot left it, and a tester who has not
    restarted since a migration landed is several revisions behind -- which the ORM below
    does not survive: ``build_plan`` selects every mapped column, so one column the file
    lacks fails the whole read. Migrating the copy is how the capture answers "what does
    THIS build conclude", which is the only question a baseline is asked.

    The revision is recorded beside the capture, because a baseline read under a different
    schema than the one that produced it is a diff nobody can attribute.

    ``alembic/env.py`` resolves its URL through ``Settings``, which reads the environment, so
    the variable is set for the call and put back afterward (rule 133 -- process-global state
    is restored by whoever moved it, even in a one-shot script, because the next reader of
    this file will copy the pattern).
    """
    from alembic import command
    from alembic.config import Config

    before = os.environ.get("REAPER_DATA_DIR")
    os.environ["REAPER_DATA_DIR"] = str(data_dir)
    try:
        config = Config()
        config.set_main_option("script_location", str(REPO / "alembic"))
        command.upgrade(config, "head")
    finally:
        if before is None:
            os.environ.pop("REAPER_DATA_DIR", None)
        else:
            os.environ["REAPER_DATA_DIR"] = before
    with sqlite3.connect(data_dir / DB_NAME) as conn:
        row = conn.execute("SELECT version_num FROM alembic_version").fetchone()
    return str(row[0]) if row else "unknown"


def capture(snapshot_asked: int | None) -> None:
    source = DATA_DIR / DB_NAME
    if not source.exists():
        sys.exit(f"no database at {source}. This capture needs a real library to read.")

    read_only = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
    try:
        snapshot_id = choose_snapshot(read_only, snapshot_asked)
        items, ids = read_items(read_only, snapshot_id)
        degraded = read_only.execute(
            "SELECT degraded FROM snapshot WHERE id = ?", (snapshot_id,)
        ).fetchone()
        # The copy is the whole reason the operator's database is safe: ``build_plan`` writes
        # a run row and its steps, and the source above is open ``mode=ro`` and never handed
        # to anything that writes. The directory takes the copy with it on the way out,
        # whichever way this block leaves.
        with tempfile.TemporaryDirectory(prefix="reaper-baseline-") as tmp:
            destination = sqlite3.connect(Path(tmp) / DB_NAME)
            try:
                read_only.backup(destination)
            finally:
                destination.close()
            revision = migrate_the_copy(Path(tmp))
            plan = asyncio.run(_plan(Path(tmp), snapshot_id, ids))
    finally:
        read_only.close()

    verdicts: dict[str, int] = {}
    for item in items:
        verdicts[item["verdict"]] = verdicts.get(item["verdict"], 0) + 1

    write_capture(
        {
            "schema": 1,
            "note": NOTE,
            "scorer_version": SCORER_VERSION,
            "migration": revision,
            "snapshot": {
                "id": snapshot_id,
                "degraded": bool(degraded[0]) if degraded else None,
                "candidates": len(items),
            },
            "verdicts": verdicts,
            "items": items,
            "plan": plan,
        }
    )
    print(
        f"wrote {OUT.relative_to(REPO)}: snapshot {snapshot_id}, {len(items)} items, "
        f"{verdicts}, plan {plan['outcome']}"
    )


def parse_argv(argv: list[str]) -> int | None:
    """``--snapshot <id>``, or nothing. Anything unrecognized exits.

    Same posture as ``policy_lab_extract.parse_argv``: a near miss must not fall through to
    the default, because the default here silently answers about a different snapshot.
    """
    snapshot: int | None = None
    index = 0
    while index < len(argv):
        arg = argv[index]
        if arg.startswith("--snapshot="):
            raw = arg.removeprefix("--snapshot=")
        elif arg == "--snapshot":
            if index + 1 >= len(argv):
                sys.exit("--snapshot needs an id: --snapshot <id>")
            index += 1
            raw = argv[index]
        else:
            sys.exit(
                f"unrecognized argument {arg!r}.\n"
                "Usage: baseline_capture.py [--snapshot <id>]\n"
                "With no flags it re-captures the snapshot the committed file names."
            )
        if not raw.isdigit():
            sys.exit(f"--snapshot takes a snapshot id, not {raw!r}")
        snapshot = int(raw)
        index += 1
    return snapshot


def main() -> None:
    # WARNING, because the planner names every item it drops at DEBUG and every item it
    # holds back at INFO. Unconfigured, structlog prints all of it, and five thousand media
    # keys scroll the one line the operator is running this for off the screen. Their own
    # terminal and their own library, so this is legibility rather than the golden rule.
    configure_logging(level="WARNING")
    capture(parse_argv(sys.argv[1:]))


if __name__ == "__main__":
    main()
