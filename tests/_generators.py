# SPDX-License-Identifier: AGPL-3.0-or-later
"""Shared glue for testing the two committed-artifact generators.

``scripts/policy_lab_extract.py`` and ``scripts/baseline_capture.py`` both write a
de-identified file into the repository, both need a real library to run in full, and both
therefore rest on the same structural argument: **there is exactly one writer, and the guard
lives in it.** A second writer would be a second path where the guard is not, and it would be
the one nobody can reach from a test.

That argument is checked by walking the script's own source, which is the same walk for both
scripts -- so it lives here rather than in one test file with a copy in the other. The copy is
what the simplification plan calls "one derivation written N times", and rule 119 asks for the
shared part extracted rather than the mirror patched.
"""

from __future__ import annotations

import ast
import importlib.util
import sys
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]

#: Spellings that put bytes on disk. Rule 147 asks for the accepted set to be written down and
#: the matcher proven against the ones it rejects, which each caller's own test does. The
#: residual blind spot is fully dynamic dispatch (``getattr(OUT, "write_" + "text")``), which
#: no walk over names can see.
WRITE_SPELLINGS = frozenset(
    {
        "write_text",
        "write_bytes",
        "writelines",
        "write",
        "dump",
        "open",
        "replace",
        "rename",
        "copy",
        "copyfile",
        "copy2",
        "touch",
    }
)


def load_script(path: Path) -> Any:
    """Import a generator by path rather than by name, and put ``sys.path`` back.

    ``scripts/`` is not a package and is not on the path. Executing the module runs its own
    two ``sys.path.insert`` calls, so importing it here mutates process-global state the next
    xdist worker inherits (rule 133) -- restoring it is what makes that untrue, not the spec.
    An earlier docstring credited the spec, and was measured wrong: under pytest those two
    entries happen to be present already, so the mechanism looked clean because of the
    environment, which is rule 119's environmental accident.
    """
    before = list(sys.path)
    try:
        spec = importlib.util.spec_from_file_location(path.stem, path)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    finally:
        sys.path[:] = before
    return module


def functions_that_can_write(source: str) -> set[str]:
    """Every function naming a write spelling, whether it calls it or merely takes a
    reference to it.

    Attributes and bare names rather than calls only: ``writer = OUT.write_text`` followed by
    ``writer(...)`` is a call whose callee is a local, so a call-shaped matcher sees nothing,
    and that alias was measured invisible to the first version of this guard.
    """
    tree = ast.parse(source)
    owners: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef):
            continue
        for inner in ast.walk(node):
            named = (
                inner.attr
                if isinstance(inner, ast.Attribute)
                else inner.id
                if isinstance(inner, ast.Name)
                else None
            )
            if named in WRITE_SPELLINGS:
                owners.add(node.name)
    return owners


def keyword_string_values(source: str, keyword: str) -> set[str]:
    """Every literal string passed as ``keyword=`` anywhere in ``source``.

    Reads the whole call and inspects inside it rather than anchoring on a quote character,
    which is rule 147's preference: ``kind="x"`` and ``kind='x'`` are one node to the parser
    and two patterns to a regex, and the third spelling nobody thought of is the one that
    slips past. A non-literal value (an f-string, a variable) is invisible here on purpose --
    it is also invisible to the hand-written list this reconciles, so a caller that counts
    what it collected sees the population shrink.
    """
    found: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Call):
            continue
        for word in node.keywords:
            if (
                word.arg == keyword
                and isinstance(word.value, ast.Constant)
                and isinstance(word.value.value, str)
            ):
                found.add(word.value.value)
    return found


def returned_string_literals(source: str, function: str) -> set[str]:
    """Every string a named function returns as a literal.

    The drift guard for a set of magic strings that has no declaration to derive from. Where
    one is introduced, derive from it and delete the caller (rule 103's first option beats its
    second).

    Walks INSIDE each return rather than reading its value as a constant. Rule 147 again:
    ``return "protect" if blocked else "condemn"`` is a return whose value is an ``IfExp``, so
    a constant-shaped matcher collects neither arm -- and `decide_verdict`, the one function
    this exists for, opens with exactly that line.
    """
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and node.name == function:
            return {
                leaf.value
                for inner in ast.walk(node)
                if isinstance(inner, ast.Return) and inner.value is not None
                for leaf in ast.walk(inner.value)
                if isinstance(leaf, ast.Constant) and isinstance(leaf.value, str)
            }
    raise AssertionError(f"no function named {function!r} in the source given")
