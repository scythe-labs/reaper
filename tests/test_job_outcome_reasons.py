# SPDX-License-Identifier: AGPL-3.0-or-later
"""Phase 11a: a background job's outcome, and a scan's live step, both carry a typed reason.

Two catalogs, two producers, walked the same shape `test_repo_hygiene.py` walks the refusal
catalog in (rule 145): every id a producer can emit is a key in the catalog, and every catalog
key has a producer that can emit it. Both directions, both populations pinned, so a walk that
quietly stopped reading part of a file cannot pass by agreeing with a catalog shrunk to match.

``jobs.result.*`` is `services.scheduler._record_run`'s ``result`` and
`services.leaving_soon.LeavingSoonResult.summary`'s return value -- a bare id, composed by the
browser under that namespace (`frontend/src/components/JobStatus.tsx`'s ``jobResultText``).
``shell.scanBar.step.*`` is `services.snapshot.Progress`'s ``detail`` -- also a bare id,
composed under that namespace by `frontend/src/components/ScanBar.tsx` and its siblings.
"""

from __future__ import annotations

import ast
from pathlib import Path

from ._reasons import catalog

REPO = Path(__file__).resolve().parents[1]
SRC = REPO / "src" / "reaper"


def _reason_literal_ids(node: ast.AST) -> dict[str, list[int]]:
    """Every ``Reason("<id>")`` call literal under ``node``, id -> line numbers."""
    sites: dict[str, list[int]] = {}
    for call in ast.walk(node):
        if (
            isinstance(call, ast.Call)
            and isinstance(call.func, ast.Name)
            and call.func.id == "Reason"
            and call.args
            and isinstance(call.args[0], ast.Constant)
            and isinstance(call.args[0].value, str)
        ):
            sites.setdefault(call.args[0].value, []).append(call.lineno)
    return sites


def _jobs_result_sites() -> dict[str, list[str]]:
    """Every ``jobs.result.*`` id: every ``Reason(...)`` literal in the whole of
    ``services/scheduler.py`` (every ``_record_run`` result there is a bare id under this
    namespace; the file raises no coded refusal) plus every one inside
    ``LeavingSoonResult.summary``'s own body. Scoped to that one method rather than the whole
    of ``services/leaving_soon.py``, because ``_record_skip`` next door builds
    ``error.leaving_soon.*`` reasons -- a different namespace this walk must not collect.
    """
    sites: dict[str, list[str]] = {}

    sched_path = SRC / "services" / "scheduler.py"
    for id_, lines in _reason_literal_ids(
        ast.parse(sched_path.read_text(encoding="utf-8"))
    ).items():
        sites.setdefault(id_, []).extend(f"services/scheduler.py:{ln}" for ln in lines)

    ls_path = SRC / "services" / "leaving_soon.py"
    ls_tree = ast.parse(ls_path.read_text(encoding="utf-8"))
    summary_body: ast.AST | None = None
    for node in ast.walk(ls_tree):
        if isinstance(node, ast.ClassDef) and node.name == "LeavingSoonResult":
            for item in node.body:
                if isinstance(item, ast.FunctionDef) and item.name == "summary":
                    summary_body = item
    assert summary_body is not None, (
        "LeavingSoonResult.summary moved or was renamed; update this walk's target"
    )
    for id_, lines in _reason_literal_ids(summary_body).items():
        sites.setdefault(id_, []).extend(f"services/leaving_soon.py:{ln}" for ln in lines)

    return sites


_EXPECTED_JOBS_RESULT_IDS = 24
_EXPECTED_JOBS_RESULT_SITES = 26


def test_every_job_result_reason_has_a_producer_and_a_catalog_entry() -> None:
    """The two-way agreement rule 145 asks for, modeled on `test_repo_hygiene.py`'s
    `test_every_refusal_code_has_a_raiser_and_a_catalog_entry`: every id a job outcome can
    carry is a key under `jobs.result.*` in `ui.json`, and every key there has a producer.
    """
    sites = _jobs_result_sites()
    total = sum(len(v) for v in sites.values())
    assert len(sites) == _EXPECTED_JOBS_RESULT_IDS, (
        f"expected {_EXPECTED_JOBS_RESULT_IDS} distinct jobs.result ids, found {len(sites)}: "
        f"{sorted(sites)}. If you added or removed one, bump the constant here."
    )
    assert total == _EXPECTED_JOBS_RESULT_SITES, (
        f"expected {_EXPECTED_JOBS_RESULT_SITES} producing sites across those ids, found "
        f"{total}. A count that fell without the id count falling too means the walk stopped "
        "seeing a spelling the tree uses (rule 147)."
    )

    entries = set(catalog("jobs.result"))
    walked = set(sites)
    assert walked <= entries, (
        f"jobs.result ids with no catalog entry: {sorted(walked - entries)}. Add each to "
        "ui.json's jobs.result section."
    )
    assert entries <= walked, (
        f"jobs.result catalog entries with no producer this walk can see: "
        f"{sorted(entries - walked)}. Either the id is dead, or it is produced through a "
        "shape _jobs_result_sites does not recognize yet -- teach the walk the shape rather "
        "than deleting the entry."
    )


def _progress_detail_arg(call: ast.Call) -> ast.expr | None:
    """`Progress`'s `detail` is its 4th positional field, or a `detail=` keyword."""
    if len(call.args) > 3:
        return call.args[3]
    for kw in call.keywords:
        if kw.arg == "detail":
            return kw.value
    return None


def _scan_step_sites() -> dict[str, list[str]]:
    """Every ``shell.scanBar.step.*`` id: the ``detail`` argument of every ``Progress(...)``
    call in ``services/scan_runner.py`` and ``services/snapshot.py``, where that argument is
    a literal ``Reason(...)`` call. The "complete" emit's bare ``None`` detail carries no id
    and is correctly invisible to this walk -- it is not a producer of any catalog entry.
    """
    sites: dict[str, list[str]] = {}
    for rel in ("services/scan_runner.py", "services/snapshot.py"):
        tree = ast.parse((SRC / rel).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "Progress"
            ):
                continue
            detail = _progress_detail_arg(node)
            if (
                isinstance(detail, ast.Call)
                and isinstance(detail.func, ast.Name)
                and detail.func.id == "Reason"
                and detail.args
                and isinstance(detail.args[0], ast.Constant)
                and isinstance(detail.args[0].value, str)
            ):
                sites.setdefault(detail.args[0].value, []).append(f"{rel}:{node.lineno}")
    return sites


_EXPECTED_SCAN_STEP_IDS = 10
_EXPECTED_SCAN_STEP_SITES = 11


def test_every_scan_step_reason_has_a_producer_and_a_catalog_entry() -> None:
    """The same two-way agreement for a scan's live step: every id a `Progress` emit can
    carry is a key under `shell.scanBar.step.*` in `ui.json`, and every key there has a
    producer."""
    sites = _scan_step_sites()
    total = sum(len(v) for v in sites.values())
    assert len(sites) == _EXPECTED_SCAN_STEP_IDS, (
        f"expected {_EXPECTED_SCAN_STEP_IDS} distinct shell.scanBar.step ids, found "
        f"{len(sites)}: {sorted(sites)}. If you added or removed one, bump the constant here."
    )
    assert total == _EXPECTED_SCAN_STEP_SITES, (
        f"expected {_EXPECTED_SCAN_STEP_SITES} producing sites across those ids, found "
        f"{total}. A count that fell without the id count falling too means the walk stopped "
        "seeing a spelling the tree uses (rule 147)."
    )

    entries = set(catalog("shell.scanBar.step"))
    walked = set(sites)
    assert walked <= entries, (
        f"shell.scanBar.step ids with no catalog entry: {sorted(walked - entries)}. Add each "
        "to ui.json's shell.scanBar.step section."
    )
    assert entries <= walked, (
        f"shell.scanBar.step catalog entries with no producer this walk can see: "
        f"{sorted(entries - walked)}. Either the id is dead, or it is produced through a "
        "shape _scan_step_sites does not recognize yet -- teach the walk the shape rather "
        "than deleting the entry."
    )
