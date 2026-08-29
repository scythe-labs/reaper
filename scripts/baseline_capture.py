# SPDX-License-Identifier: AGPL-3.0-or-later
"""Freeze what Reaper concludes about one whole real library, de-identified by construction.

This complements ``tests/_policy_lab.py``, the tier enforced in CI, which replays a sample
of fact vectors through ``judge_facts`` and pins the judgment for each one. This script
instead reads every candidate in one stored snapshot and records, per item:

* a digest of ``Candidate.facts_json``, the evidence the scan froze;
* the verdict triple the scan stored;
* the plan ``services.planner.build_plan`` builds from that snapshot: its ordered items,
  the ordinal each was given, and the manifest hash the executor would check.

``Snapshot.evidence_hash`` is not used here. It hashes the policy fields that decide what
gathering asks for. There is one per snapshot, and it stays constant while the policy does,
so it would not change even if what gathering produced changed.

This script never writes to the real database and never re-scans the library. It opens the
source database read-only and works from a copy, because ``build_plan`` writes a run row and
its steps, and the operator's database must not carry a plan nobody asked for. It skips a
fresh scan for a different reason: scoring depends on the wall clock, so scores can shift
even when the library and the code have not changed.

Diff this file at phase boundaries, not on every pull request, and read the item lines as
counts and set membership rather than line by line.

Usage: run ``uv run python scripts/baseline_capture.py`` from the repo root to re-capture
the same snapshot the committed file already names. Pass ``--snapshot <id>`` to re-base
onto a different one. That is a deliberate act: every item id is positional, so capturing a
different snapshot moves every line, and the diff no longer means anything.
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
from typing import Any, get_args

from sqlalchemy import select

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "src"))

from reaper.config import Settings  # noqa: E402
from reaper.db.models import ActionStep  # noqa: E402
from reaper.db.session import create_engine, create_session_factory  # noqa: E402
from reaper.engine.policy import SCORER_VERSION  # noqa: E402
from reaper.engine.verdict import Verdict  # noqa: E402
from reaper.logging import configure_logging  # noqa: E402
from reaper.services.planner import PlanError, build_plan  # noqa: E402
from reaper.services.profiles import active_profile  # noqa: E402

OUT = REPO / "tests" / "fixtures" / "whole_library_baseline.json"

#: Where the real library lives. Reads the same environment variable the app does, because
#: a worktree has no ``data/`` directory of its own (same reasoning as ``policy_lab_extract``).
DATA_DIR = Path(os.environ.get("REAPER_DATA_DIR", "").strip() or (REPO / "data"))

#: The filename a copied database must use, because ``Settings.database_url`` builds the URL
#: from the data directory, not a path.
DB_NAME = "reaper.db"

NOTE = (
    "What Reaper concludes about one whole real library, de-identified by construction. "
    "Regenerate with scripts/baseline_capture.py. Item ids are positional over the "
    "snapshot's candidates ordered by media key, so they are stable across captures of the "
    "same snapshot and meaningless across captures of different ones."
)


def digest(value: str | None) -> str | None:
    """Return a short one-way digest, or ``None`` when there is nothing to digest.

    Twelve hex characters. This capture only ever asks "is this field the same as last
    time", so a full digest would quadruple the file for no extra answer. Returns
    ``None`` rather than digesting an empty string, because a candidate with no frozen
    facts and one with empty facts are different states worth telling apart.
    """
    if value is None:
        return None
    return hashlib.sha256(value.encode()).hexdigest()[:12]


#: The ``ActionStep.kind`` values ``services.planner`` writes, hand-written here since
#: there is no single declaration to read them from. ``test_baseline_capture`` checks this
#: set against the literals in ``planner.py``, so a mismatch fails that test instead of
#: silently going stale.
_STEP_KINDS = frozenset(
    {
        "radarr_delete",
        "sonarr_unmonitor",
        "sonarr_verify_unmonitor",
        "sonarr_delete_files",
    }
)

#: What ``decide_verdict`` returns, read directly off its declared return type. Nothing is
#: hand-written here: mypy fails the build if a ``return`` in that function falls outside
#: the declared set.
_VERDICTS = frozenset(get_args(Verdict))

#: Every string this capture may contain, besides an item id and a hex digest. The write is
#: refused if any other string appears, since three of ``build_plan``'s refusal messages
#: include media keys, and this file must never carry one.
_VOCABULARY = frozenset({NOTE, "movie", "season", "planned", "refused"}) | _STEP_KINDS | _VERDICTS

_ITEM_ID = re.compile(r"^i\d{4,}$")
_DIGEST = re.compile(r"^[0-9a-f]{8,64}$")


def offending_strings(payload: Any, path: str = "") -> list[str]:
    """Return every string leaf this file may not carry, with the path that produced it.

    This checks every string field, because nothing in this payload is typed by hand:
    each one is an item id, a media type, a verdict, a step kind, a hex digest, or the
    note. If a media key, a title, a path, or a Sonarr/Radarr row id reaches this list,
    the write is refused instead of merely flagged for review.

    Numbers, booleans, and nulls pass without inspection. A count could describe the
    operator's library, but this capture is built entirely from counts and ratios, so
    refusing them would refuse the file itself.
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
    """Write the capture to disk. The only writer, and the only place the guard runs.

    Matches ``policy_lab_extract.write_fixture``'s shape for the same reason: a second
    writer would be a second path without the guard, and that unguarded path is the one
    that would eventually run against a real library. ``test_baseline_capture`` checks
    that this stays the only writer.
    """
    if offenders := offending_strings(payload):
        sys.exit(
            "refusing to write: the capture carries strings that are not an item id, a "
            "digest, or a known term, and this file is committed.\n" + "\n".join(offenders[:10])
        )
    OUT.write_text(json.dumps(payload, separators=(",", ":"), sort_keys=True) + "\n")


def committed_snapshot_id() -> int | None:
    """The snapshot the capture on disk was cut against, or ``None`` if there is none yet."""
    if not OUT.exists():
        return None
    stored = json.loads(OUT.read_text()).get("snapshot")
    value = stored.get("id") if isinstance(stored, dict) else None
    return value if isinstance(value, int) else None


def choose_snapshot(conn: sqlite3.Connection, asked: int | None) -> int:
    """Return which snapshot to capture, refusing a choice that would make the diff meaningless.

    Item ids are positional, so capturing a different snapshot moves every line in the
    file at once, and an unexplained move like that should be treated as a problem to
    investigate. Re-basing onto a new snapshot is still available, since the audit
    outlives any one scan schedule, but it only happens when asked, never by default.
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
    """Return one row per candidate, plus the media-key-to-item-id map the plan is expressed in.

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
    """Run ``build_plan`` against the copy, roll it back, and report the result by item id.

    Calls the real planner rather than reimplementing it, since the ordering, the canary
    seat, the unmeasured allowance, and the manifest hash are exactly what this capture
    exists to freeze. A reimplementation would only freeze itself.

    Rolling back is a second safeguard alongside the copy: even if a bug leaves an
    approved run somewhere, it cannot land in the operator's database, which is never
    opened for writing, or in the copy, which is deleted afterward.
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
                # Printed, never written. Three of build_plan's refusals name the media
                # keys they refused on, so writing this message to a committed file would
                # leak exactly what the rest of this script exists to keep out. The
                # operator running the capture sees the reason here; the file only
                # records that there was one.
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
                # Position matches the ordinal: build_plan increments it once per item
                # that gets steps, so the item at index 0 is the canary, the smallest
                # measured item, sent and verified alone before anything else runs.
                "items_in_ordinal_order": ordered,
                "step_kinds": sorted(kinds),
                "manifest": run.approved_manifest_hash,
                "policy": run.policy_hash,
                "held_back_unknown_size": run.held_back_unknown_size,
                "max_unmeasured": profile.settings.max_unmeasured_per_run,
                # The real planning route refuses this case, because a run bounded by
                # numbers nobody chose is not a run the operator approved. A capture only
                # records state, so it stores this flag instead: a later reader sees why
                # the plan looks smaller than expected, instead of mistaking it for a
                # regression.
                "settings_fell_back": profile.repaired,
            }
            # Never committed. build_plan flushes rather than commits, so this line is
            # what keeps the run row out of even the throwaway copy.
            await session.rollback()
            return plan
    finally:
        await engine.dispose()


def migrate_the_copy(data_dir: Path) -> str:
    """Run ``alembic upgrade head`` against the copy, and return the revision it reached.

    The operator's database sits at whatever revision their last boot applied, so it can
    be several migrations behind. The ORM does not tolerate that: ``build_plan`` selects
    every mapped column, so a missing column fails the whole read. Migrating the copy
    first is how this capture answers "what does this build conclude" instead of "what
    did an old build conclude".

    The revision is recorded beside the capture, because reading a baseline under a
    different schema than the one that produced it makes the diff impossible to
    attribute.

    ``alembic/env.py`` resolves its database URL through ``Settings``, which reads the
    environment. This sets that variable for the call and restores it afterward, since
    process-global state should always be put back by whoever changed it, even in a
    one-shot script.
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
        # Working from a copy is what keeps the operator's database safe: build_plan
        # writes a run row and its steps, and the source connection above is read-only
        # and never handed to anything that writes. The temp directory deletes the copy
        # when this block exits, however it exits.
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
    """Parse ``--snapshot <id>``, or no arguments. Exits on anything unrecognized.

    Matches ``policy_lab_extract.parse_argv``'s behavior: a near-miss flag must not fall
    through to the default, since the default here would silently answer about a
    different snapshot.
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
    # Configured at WARNING because the planner logs every item it drops at DEBUG and
    # every item it holds back at INFO. Left unconfigured, structlog would print all of
    # it, and a library with thousands of media keys would scroll the line the operator
    # is watching for off the screen. This is about keeping the terminal readable, not
    # about hiding information: it is the operator's own terminal and their own library.
    configure_logging(level="WARNING")
    capture(parse_argv(sys.argv[1:]))


if __name__ == "__main__":
    main()
