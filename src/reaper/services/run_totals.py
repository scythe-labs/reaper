# SPDX-License-Identifier: AGPL-3.0-or-later
"""What a reap run actually deleted, aggregated from the durable journal.

One query, and one aggregation of its rows, shared by two callers that must never drift
apart: ``Executor._write_run_totals``, which writes ``ReapRun``'s four terminal-totals
columns once a real run reaches COMPLETED or ABORTED, and the migration that backfills
those same columns for every run that finished before they existed
(``alembic/versions/*_add_reap_run_totals.py``). Both execute the identical statement
``totals_query`` builds; only the caller differs, an ``AsyncSession`` for a live executor
and a plain sync ``Connection`` for a migration, since a ``Select`` built off the ORM
classes runs unchanged on either.

Counting "deleted" reuses the discipline ``Executor._rolling_30d_deletions`` already
established: a step counts once its file is confirmed gone, VERIFIED or
``file_removed_at`` set, never from ``state`` alone. A movie whose delete succeeded but
whose import exclusion never landed ends FAILED and stays FAILED, but its bytes are
still off disk, so it still counts here.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any

from sqlalchemy import Select, select

from reaper.db.models import ActionStep, Candidate, ReapRun, StepState

#: The irreversible step of an item's plan, the one that removes files. A movie has a
#: ``radarr_delete``; a season's file delete is ``sonarr_delete_files``, reached only
#: after its reversible unmonitor and the verification of it. Everything else in a
#: season plan is a read or a reversible edit.
#:
#: Declared here rather than in ``services.executor``: the terminal-totals write, the
#: per-item outcomes read (``api.runs._run_outcomes``), the executor's own send loop and
#: its rolling-budget query all filter on this same set, and a set copied into three or
#: four modules is that many memberships to keep in step by hand.
TERMINAL_DELETE_KINDS = frozenset({"radarr_delete", "sonarr_delete_files"})


@dataclass(frozen=True)
class RunTotals:
    """One run's terminal counts, the shape ``ReapRun``'s four totals columns hold."""

    deleted_items: int
    deleted_bytes: int
    deleted_unmeasured: int
    skipped: int


def totals_query(run_id: int) -> Select[tuple[StepState, bool, int | None]]:
    """Every terminal delete step of one run, joined back to its frozen candidate for
    the size the operator approved: the one statement both callers run.

    Returns raw rows rather than the aggregate, so the identical statement executes
    unmodified on an ``AsyncSession`` (``await session.execute(...)``) and a sync
    ``Connection`` (a migration's ``conn.execute(...)``). :func:`aggregate_rows` is what
    turns either result into a :class:`RunTotals`.
    """
    return (
        select(
            ActionStep.state,
            ActionStep.file_removed_at.is_not(None),
            Candidate.size_bytes,
        )
        .select_from(ActionStep)
        .join(ReapRun, ReapRun.id == ActionStep.run_id)
        .join(
            Candidate,
            (Candidate.snapshot_id == ReapRun.snapshot_id)
            & (Candidate.media_key == ActionStep.media_key),
        )
        .where(ActionStep.run_id == run_id, ActionStep.kind.in_(TERMINAL_DELETE_KINDS))
    )


def aggregate_rows(rows: Iterable[Sequence[Any]]) -> RunTotals:
    """Turn ``totals_query``'s rows into one run's totals.

    A step counts as deleted from its file's actual removal, never from ``state`` alone:
    VERIFIED, or a FAILED step whose ``file_removed_at`` is set because the delete landed
    but a follow-up check (the exclusion, the Plex refresh) did not. Every other step,
    including one still PENDING or SENT on a run that never reached a terminal state,
    counts as neither deleted nor skipped.

    ``rows`` is typed as plain sequences rather than the query's own three-column tuple
    shape: SQLAlchemy's ``Row`` is a ``Sequence``, not a ``tuple``, at the type level, so
    a caller handing this either an ``AsyncSession`` result or a migration's plain sync
    ``Connection`` result satisfies it the same way.
    """
    deleted_items = 0
    deleted_bytes = 0
    deleted_unmeasured = 0
    skipped = 0
    for state, file_removed, size_bytes in rows:
        # The Enum column's own type does the string-to-member conversion whether this
        # ran through the ORM or a migration's plain Connection, but read defensively by
        # value either way: a raw SQL read anywhere in this path (a future caller, a
        # hand-run query) is one string comparison away from working, not a crash.
        value = state.value if isinstance(state, StepState) else state
        if value == StepState.VERIFIED.value or bool(file_removed):
            deleted_items += 1
            if size_bytes is None:
                deleted_unmeasured += 1
            else:
                deleted_bytes += size_bytes
        elif value == StepState.SKIPPED.value:
            skipped += 1
    return RunTotals(
        deleted_items=deleted_items,
        deleted_bytes=deleted_bytes,
        deleted_unmeasured=deleted_unmeasured,
        skipped=skipped,
    )
