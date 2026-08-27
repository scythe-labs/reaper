"""Scoped mutation runner: break one function on purpose, and see whether a test notices.

This measures whether a test would catch a broken line, not how much code a test
suite covers. For each mutant, it asks one question: if this line silently stopped
doing its job, would the suite fail? A survivor is a branch nothing defends.
``docs/LEARNINGS.md`` records what running it has found.

Run it, one zone per run:

    uv run python scripts/mutation_scope.py --zone <name>
    uv run python scripts/mutation_scope.py --help    # every zone there is

The zone names are not listed here on purpose. ``--zone`` reads its choices straight
from ``ZONES``, so ``--help`` always matches the declaration, while a list copied into
this docstring could drift from it unnoticed.

Add a zone to ``ZONES`` below: the module, the functions (``Class.method`` for a
method), the tests that could plausibly kill a mutant in them, and a probe. Scope each
zone by function, not by file. Running the same operators over a whole large module
generates far more mutants than one sitting can run through, which is how an exercise
like this stops getting run at all. A single function can still be large, and that is
a cost worth paying, because it is a cost per answer rather than per file.

Two passes run over each mutant:

1. Kill or survive: run the tests against the mutated source.
2. Direction, for survivors only: re-run the zone's ``probe`` corpus and diff it
   against the baseline. This is what makes the output readable. "Seven survived" is
   just a number, while "each of these makes the shim refuse a legal repair, leaving
   the operator's bar empty" is a finding. Reaper's safety argument depends on
   direction, so a survivor that widens what gets deleted is a different finding from
   one that keeps more.

Mutation works as a byte-precise text splice, never an AST unparse, so a mutant
differs from the original on exactly one line and reports as
``func:line '<=' -> '<'``, readable straight against the source. Splicing also lets
``1 <= floor <= 100`` produce two separately addressable mutants, since the AST
represents that as a single ``Compare`` node with a list of operators and no position
for any one of them.

A probe only sees what it records, and each zone's probe records something different:
zone 1 returns a repaired body, zone 2 an accept-or-refuse verdict plus the operator's
sentence, zone 3 whether the gate still holds the file. Getting that wrong has produced
false readings before: a probe that only records a verdict can call a reworded operator
string "no change", and a probe that never normalizes a percentage rating can test the
wrong bar entirely. Treat "no change on the probe corpus" as a question to check, not
an answer to trust, and confirm what the baseline actually measures before trusting a
survivor list built on it.
"""

from __future__ import annotations

import argparse
import ast
import io
import itertools
import json
import os
import shutil
import subprocess
import tempfile
import threading
import time
import tokenize
from dataclasses import dataclass, field
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


@dataclass(frozen=True)
class Omission:
    """Callables in a zone's module that the zone deliberately does not mutate.

    Each group carries one written reason. Every name must appear in ``functions`` or
    here, since ``zone_drift`` fails on any name in neither. Grouped rather than listed
    one line each because the reasons genuinely repeat: a zone scoped to two shims
    omits the rest of its file for one reason.
    """

    reason: str
    functions: tuple[str, ...]


@dataclass(frozen=True)
class Zone:
    """What to mutate, what gets a say, and how to tell survivors apart.

    ``probe`` is Python source run in a fresh subprocess against the mutated module. It
    must print ``{"cases": {name: answer}}`` as JSON. Each "answer" has to be a value
    that shows the direction of a change: a returned body for a repair shim, an
    accepted-or-rejected verdict for a validator.

    ``functions`` and ``omits`` together must account for every callable in ``module``,
    checked by ``zone_drift``. A function with no mutable token can still be declared
    and will report zero mutants. Leaving it out of both lists entirely is what
    ``zone_drift`` does not allow.
    """

    module: Path
    functions: tuple[str, ...]
    tests: tuple[str, ...]
    probe: str
    omits: tuple[Omission, ...] = ()


def module_callables(module: Path) -> set[str]:
    """Return every function and method in a module, named the way a zone names one.

    A method is named ``Class.method``. This never produces a bare method name, even
    though ``func_spans`` accepts one, so a zone's list and this set can be compared as
    written.
    """
    tree = ast.parse((REPO / module).read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            names.update(
                f"{node.name}.{child.name}"
                for child in node.body
                if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef)
            )
    names.update(
        node.name for node in tree.body if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
    )
    return names


def zone_drift(name: str, zone: Zone) -> list[str]:
    """Return complaints about a zone whose list no longer matches its module, one line each.

    A zone's function list is hand-written, so it can silently fall out of sync with
    its module: a zone that declares only some of a module's callables reports a clean
    sweep while the rest of its mutable surface goes untested. This check catches that
    drift before a run is trusted.
    """
    declared = set(zone.functions)
    omitted = {f for group in zone.omits for f in group.functions}
    actual = module_callables(zone.module)
    complaints = []
    for missing in sorted(actual - declared - omitted):
        complaints.append(f"{name}: {zone.module}'s {missing} is in neither functions= nor omits=")
    for gone in sorted((declared | omitted) - actual):
        complaints.append(f"{name}: {gone} is declared but no longer exists in {zone.module}")
    for both in sorted(declared & omitted):
        complaints.append(f"{name}: {both} is in functions= and omits= at once")
    return complaints


#: Swaps per token. Deliberately small: an operator whose every mutant is caught by the
#: type checker or the parser costs a run slot and teaches nothing.
COMPARE_SWAPS = {
    "<": ("<=", ">", ">=", "=="),
    "<=": ("<", ">=", ">", "=="),
    ">": (">=", "<", "<=", "=="),
    ">=": (">", "<=", "<", "=="),
    "==": ("!=",),
    "!=": ("==",),
}
BINOP_SWAPS = {"+": ("-",), "-": ("+",), "*": ("/",), "/": ("*",)}
NAME_SWAPS = {"and": ("or",), "or": ("and",), "True": ("False",), "False": ("True",)}


@dataclass
class Mutant:
    ident: str
    func: str
    line: int
    col: int
    end_col: int
    original: str
    replacement: str
    kind: str
    status: str = "?"
    direction: list[str] = field(default_factory=list)

    @property
    def label(self) -> str:
        return f"{self.func}:{self.line} {self.original!r} -> {self.replacement!r}"


def func_spans(source: str, functions: tuple[str, ...], module: Path) -> dict[str, tuple[int, int]]:
    """Return the body line span of each named function, docstring excluded.

    Docstrings in this codebase are long and carry prose comparisons. Mutating them
    would produce mutants no test could ever kill and no author would ever act on.

    A method is named ``Class.method``, because a bare name is not unique:
    ``engine/gates.py`` alone defines several ``evaluate`` methods, and keying on the
    bare name would silently keep the last match, reporting on a function nobody asked
    about as if it were a real answer. An ambiguous name raises instead, the same way
    everything else here refuses to guess.
    """
    found: dict[str, list[tuple[int, int]]] = {}

    def record(name: str, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        head = node.body[0]
        docstring = (
            isinstance(head, ast.Expr)
            and isinstance(head.value, ast.Constant)
            and isinstance(head.value.value, str)
        )
        start = (head.end_lineno or head.lineno) + 1 if docstring else head.lineno
        found.setdefault(name, []).append((start, node.end_lineno or node.lineno))

    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.ClassDef):
            for child in node.body:
                if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef):
                    record(f"{node.name}.{child.name}", child)
                    record(child.name, child)
        elif isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            record(node.name, node)

    spans: dict[str, tuple[int, int]] = {}
    for name in functions:
        matches = found.get(name, [])
        if not matches:
            raise SystemExit(f"{name!r} not found in {module}")
        if len(matches) > 1:
            raise SystemExit(f"{name!r} is ambiguous in {module}; qualify it as Class.method")
        spans[name] = matches[0]
    return spans


def owner(spans: dict[str, tuple[int, int]], line: int) -> str | None:
    return next((n for n, (lo, hi) in spans.items() if lo <= line <= hi), None)


def generate(source: str, zone: Zone) -> list[Mutant]:
    spans = func_spans(source, zone.functions, zone.module)
    out: list[Mutant] = []
    seen: set[tuple[int, int, str]] = set()
    for tok in tokenize.generate_tokens(io.StringIO(source).readline):
        line, col = tok.start
        fn = owner(spans, line)
        if fn is None or tok.start[0] != tok.end[0]:
            continue
        text = tok.string
        swaps: tuple[str, ...] = ()
        kind = ""
        if tok.type == tokenize.OP and text in COMPARE_SWAPS:
            swaps, kind = COMPARE_SWAPS[text], "compare"
        elif tok.type == tokenize.OP and text in BINOP_SWAPS:
            swaps, kind = BINOP_SWAPS[text], "binop"
        elif tok.type == tokenize.NAME and text in NAME_SWAPS:
            swaps, kind = NAME_SWAPS[text], "logic"
        elif tok.type == tokenize.NAME and text == "not":
            swaps, kind = ("",), "drop-not"
        elif tok.type == tokenize.NUMBER and text.isdigit():
            n = int(text)
            swaps = (str(n + 1),) + ((str(n - 1),) if n > 0 else ())
            kind = "constant"
        for rep in swaps:
            if (line, col, rep) in seen:
                continue
            seen.add((line, col, rep))
            out.append(
                Mutant(
                    ident=f"m{len(out):03d}",
                    func=fn,
                    line=line,
                    col=col,
                    end_col=tok.end[1],
                    original=text,
                    replacement=rep,
                    kind=kind,
                )
            )
    out += statement_deletions(source, spans, len(out))
    return out + guard_deletions(source, spans, len(out))


def statement_deletions(
    source: str, spans: dict[str, tuple[int, int]], start_id: int
) -> list[Mutant]:
    """Replace one assignment with `pass`.

    This is the operator that catches a line doing real work that nothing asserts on:
    a comment can claim a safeguard runs, and only a mutant like this one checks that
    a test would notice if it stopped running.
    """
    lines = source.splitlines()
    out: list[Mutant] = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Assign | ast.AugAssign):
            continue
        fn = owner(spans, node.lineno)
        if fn is None or node.lineno != node.end_lineno:
            continue
        raw = lines[node.lineno - 1]
        out.append(
            Mutant(
                ident=f"m{start_id + len(out):03d}",
                func=fn,
                line=node.lineno,
                col=len(raw) - len(raw.lstrip()),
                end_col=len(raw.rstrip()),
                original=raw.strip(),
                replacement="pass",
                kind="delete-statement",
            )
        )
    return out


def _spliceable_header(node: ast.If) -> bool:
    """Return whether ``if <test>:`` fits on the one line the splice can rewrite."""
    return node.test.lineno == node.lineno and node.test.end_lineno == node.test.lineno


def skipped_guards(source: str, spans: dict[str, tuple[int, int]]) -> dict[str, list[int]]:
    """Return branches the operator below cannot make dead, grouped by reason for `main` to report.

    Two shapes are skipped. A wrapped ``if`` header cannot take a byte-precise
    single-line splice. A ternary is a branch this operator was not written to rewrite.
    Token swaps still reach inside both kinds of test, so neither is left entirely
    unmutated; what is missing is specifically the delete-this-guard edit.

    The count of skipped branches always stays in the report. Printing a mutant count
    next to a silent gap in coverage would read as a complete sweep, which is the same
    kind of unearned claim this runner exists to catch.
    """
    wrapped: list[int] = []
    ternary: list[int] = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.If) and owner(spans, node.lineno) is not None:
            if not _spliceable_header(node):
                wrapped.append(node.lineno)
        elif isinstance(node, ast.IfExp) and owner(spans, node.lineno) is not None:
            ternary.append(node.lineno)
    return {
        "if header wrapped across lines": sorted(wrapped),
        "ternary, which this operator does not rewrite": sorted(ternary),
    }


def guard_deletions(source: str, spans: dict[str, tuple[int, int]], start_id: int) -> list[Mutant]:
    """Make one branch dead: ``if <test>:`` becomes ``if (<test>) and False:``.

    This is the operator the rest of the set cannot express. Token swaps need a token
    with an opposite, and the assignment-deletion operator above only walks
    assignments, so a guard built on ``isinstance`` or ``in`` produced no mutant at
    all. A function with no mutable token reported exactly like one whose mutants all
    died, hiding a fail-closed guard from every other operator here.

    Two choices matter. The test is still evaluated, rather than replaced by ``False``
    outright, so a walrus assignment (``if blocked := _blocked(...)``) still binds its
    name. The mutant then fails on the missing branch instead of a ``NameError`` two
    lines down, reporting the guard as undefended for the right reason. The
    parentheses are not cosmetic either: ``if a or b:`` spliced without them binds as
    ``a or (b and False)``, which stays live down the ``a`` arm and would report a
    killed guard as surviving.

    This only rewrites single-line ``if`` headers, like every other operator here,
    because the splice is byte precise: a test wrapped across lines is skipped rather
    than half-rewritten, and a ternary is left alone. `skipped_guards` groups both
    kinds of skip and `main` prints them.
    """
    lines = source.splitlines()
    out: list[Mutant] = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.If):
            continue
        test = node.test
        if not _spliceable_header(node):
            continue
        fn = owner(spans, node.lineno)
        if fn is None:
            continue
        end_col = test.end_col_offset
        if end_col is None:
            continue
        original = lines[node.lineno - 1][test.col_offset : end_col]
        out.append(
            Mutant(
                ident=f"m{start_id + len(out):03d}",
                func=fn,
                line=node.lineno,
                col=test.col_offset,
                end_col=end_col,
                original=original,
                replacement=f"({original}) and False",
                kind="delete-guard",
            )
        )
    return out


def splice(source: str, m: Mutant) -> str:
    lines = source.splitlines(keepends=True)
    line = lines[m.line - 1]
    found = line[m.col : m.end_col]
    if found != m.original:
        raise ValueError(f"{m.ident}: expected {m.original!r} at {m.line}:{m.col}, found {found!r}")
    if m.kind == "drop-not":
        # `not x` becomes `x`. This also swallows the trailing space, so the result
        # still parses.
        head, tail = line[: m.col], line[m.end_col :].lstrip(" ")
        lines[m.line - 1] = head + tail
    else:
        lines[m.line - 1] = line[: m.col] + m.replacement + line[m.end_col :]
    return "".join(lines)


#: Everything a worker copy needs to run the suite. Two members are not obvious:
#: `README.md`, because `[project] readme` points at it and the build backend reads it during
#: `uv sync`; and `frontend/src`, because some backend tests check a Python vocabulary against
#: the TSX that renders it (`test_review_chips.py` opens `WhyPanel.tsx`). `frontend/src` is
#: listed as a nested path on purpose, since copying all of `frontend` would drag
#: `node_modules` into every worker. If a zone's tests need something not listed here, the
#: baseline check reports that before any mutant runs.
WORKER_PATHS = (
    "src",
    "tests",
    # A worker missing this path fails to collect any test that reads a script under
    # `scripts/`, and an uncollectable test file makes every mutant it would have
    # caught read as a survivor instead.
    "scripts",
    "alembic",
    "alembic.ini",
    "pyproject.toml",
    "uv.lock",
    "README.md",
    "frontend/src",
)


def make_worker(root: Path) -> Path:
    """Copy the tree, with its own venv, so a mutant never touches the real one.

    This lets the run go parallel, but the isolation matters even alone: an
    interrupted run can never leave the real checkout modified, since it is only ever
    read.

    On APFS the copy is a clone (`cp -Rc`), and `uv sync` against a warm cache is
    fast, so a worker costs little more than one mutant does. `uv sync` inside the
    copy is also what makes the isolation real: it installs the project editable
    against the copy's own `src`, so `import reaper` there cannot reach back to the
    original.
    """
    root.mkdir(parents=True, exist_ok=True)
    for rel in WORKER_PATHS:
        source = REPO / rel
        if not source.exists():
            continue
        # Copies into the path's own parent, so a nested member lands where its
        # readers expect it instead of flattened to the root.
        destination = root / rel
        destination.parent.mkdir(parents=True, exist_ok=True)
        # -c asks for an APFS clone. Other filesystems fall back to a real copy.
        for flags in ("-Rc", "-R"):
            proc = subprocess.run(  # noqa: S603 -- flags and paths are this file's own
                ["cp", flags, str(source), str(destination)],  # noqa: S607 -- `cp` from PATH
                capture_output=True,
                text=True,
            )
            if proc.returncode == 0:
                break
        else:
            raise SystemExit(f"could not copy {rel} into {root}: {proc.stderr.strip()}")
    sync = subprocess.run(
        ["uv", "sync", "--all-extras", "--quiet"],  # noqa: S607 -- `uv` is the documented entry
        cwd=root,
        capture_output=True,
        text=True,
    )
    if sync.returncode != 0:
        raise SystemExit(f"uv sync failed in worker {root}: {sync.stderr.strip()[-300:]}")
    return root


def run_tests(zone: Zone, workdir: Path, timeout: float) -> str:
    """Return `killed` when the suite notices, `survived` when it does not."""
    argv = [
        "uv",
        "run",
        "pytest",
        *zone.tests,
        "-q",
        "-x",
        "--no-header",
        "-p",
        "no:cacheprovider",
        "--hypothesis-seed=0",
    ]
    try:
        proc = subprocess.run(  # noqa: S603 -- argv is this file's literals plus zone.tests
            argv, cwd=workdir, capture_output=True, text=True, timeout=timeout
        )
    except subprocess.TimeoutExpired:
        return "timeout"
    return {0: "survived", 1: "killed"}.get(proc.returncode, f"error({proc.returncode})")


#: Run in a subprocess so each mutant is probed by a fresh import of the mutated module.
#:
#: The recovery bodies are assembled from the stored shape by hand, instead of dumping the
#: current model. A fixture that stamps the current `schema_version` where a genuinely
#: affected body carries the previous one would hide the same defect from a probe built on it.
REPAIR_SHIM_PROBE = r"""
import json
from reaper.engine.gates import GateId
from reaper.engine.policy import SCHEMA_VERSION
from reaper.engine.policy_migrations import rebalance, recover_rating_rules
from reaper.engine.signals import MAX_SCORE

def legacy(threshold=75, secondary=1000, **gate):
    row = {"gate": GateId.RATING_FLOOR.value, "enabled": True,
           "threshold": threshold, "secondary": secondary}
    row.update(gate)
    if row.get("enabled") is None:
        del row["enabled"]
    # No `keep_rating_rules` key at all, at the schema an affected body really carries.
    return {"schema_version": SCHEMA_VERSION - 1, "gates": [row],
            "signals": [{"signal": "unwatched", "weight": MAX_SCORE}]}

def over_budget(weights):
    return {"signals": [{"signal": "unwatched", "weight": w} for w in weights]}

def sig(v):
    if not isinstance(v, dict):
        return v
    out = {}
    for key in ("keep_rating_rules", "schema_version"):
        if key in v:
            out[key] = v[key]
    if "signals" in v:
        out["weights"] = [s.get("weight") for s in v["signals"]]
    return out

cases, order = {}, []
def case(name, fn, arg):
    order.append(name)
    try:
        cases[name] = sig(fn(arg))
    except Exception as exc:
        cases[name] = f"RAISED {type(exc).__name__}"

case("bar-restored", recover_rating_rules, legacy())
case("floor-at-1", recover_rating_rules, legacy(threshold=1))
case("floor-at-100", recover_rating_rules, legacy(threshold=100))
case("one-vote", recover_rating_rules, legacy(secondary=1))
case("floor-at-0", recover_rating_rules, legacy(threshold=0))
case("floor-at-101", recover_rating_rules, legacy(threshold=101))
case("zero-votes", recover_rating_rules, legacy(secondary=0))
case("floor-is-a-bool", recover_rating_rules, legacy(threshold=True))
case("gate-disabled", recover_rating_rules, legacy(enabled=False))
case("no-switch-key", recover_rating_rules, legacy(enabled=None))

cleared = legacy()
cleared["keep_rating_rules"] = []
case("operator-cleared", recover_rating_rules, cleared)

case("rescale-1-1-1-5", rebalance, over_budget([1, 1, 1, 5]))
case("rescale-equal-pair", rebalance, over_budget([7, 7]))
case("rescale-zero-total", rebalance, over_budget([0]))
case("rescale-negative", rebalance, over_budget([-5]))
case("not-an-object", rebalance, 42)

print(json.dumps({"order": order, "cases": cases}))
"""


#: Zone 2: the save boundary. A set of validators decides what an operator is allowed to
#: store, so the dangerous direction here is the opposite of a repair shim's. A mutant that
#: makes a validator accept what it should refuse lets a policy that protects nothing be
#: saved, while one that refuses a legal policy only blocks the operator, loudly. The probe
#: records accepted-or-rejected rather than a value, so that asymmetry is what the diff shows.
#:
#: Field keys and rating sources are drawn from the live registries rather than hardcoded, so
#: a probe naming a field that later moves lanes cannot silently stop testing the branch it
#: was written for.
SAVE_BOUNDARY_PROBE = r"""
import json
from reaper.engine.fields import BY_KEY, Lane, Op
from reaper.engine.policy import (
    DEFAULT_MOVIE_POLICY,
    GateId,
    GateSetting,
    GradedCondemnSpec,
    GradedKeepSpec,
    PolicyBody,
    ProfileSettings,
    RatingRuleSpec,
    RatingSource,
    SignalSetting,
    is_percentage_source,
)

cases, order = {}, []

# Record accept-or-refuse AND the sentence the operator would read. A validator's job is both
# halves: refusing the policy, and saying what to change. A probe recording only the verdict
# called a mutant "no observable change" when it had turned a remedy into "Give out the other
# -1", so the message belongs in the answer here.
def case(name, build):
    order.append(name)
    try:
        build()
        cases[name] = "accepted"
    except Exception as exc:
        text = " ".join(str(exc).split())
        cases[name] = f"rejected:{type(exc).__name__}:{text[:160]}"

# --- GateSetting._protective_floors: the two floors, each side of each boundary ---
for gate, floor in ((GateId.SERVER_POPULARITY, 1), (GateId.MIN_DORMANCY, 5)):
    tag = gate.value
    case(f"gate-{tag}-at-floor", lambda g=gate, f=floor: GateSetting(gate=g, threshold=f))
    case(f"gate-{tag}-below-floor", lambda g=gate, f=floor: GateSetting(gate=g, threshold=f - 1))
    case(f"gate-{tag}-far-below", lambda g=gate: GateSetting(gate=g, threshold=0))
    case(
        f"gate-{tag}-below-but-off",
        lambda g=gate, f=floor: GateSetting(gate=g, threshold=f - 1, enabled=False),
    )
# The rating gate is only a switch now: it must NOT police a threshold of its own.
case("gate-rating-floor-zero-is-fine", lambda: GateSetting(gate=GateId.RATING_FLOOR, threshold=0))

# --- the three floor/saturate_at copies (rules 72, 104): equality is the boundary ---
numeric_condemn = next(
    k for k, s in BY_KEY.items() if Lane.CONDEMN in s.lanes and Op.GTE in s.ops
)
numeric_any = next(k for k, s in BY_KEY.items() if Op.GTE in s.ops)
non_numeric = next((k for k, s in BY_KEY.items() if Op.GTE not in s.ops), None)
non_condemn = next((k for k, s in BY_KEY.items() if Lane.CONDEMN not in s.lanes), None)

for label, build in (
    ("signal", lambda fl, sat: SignalSetting(signal=DEFAULT_MOVIE_POLICY.signals[0].signal,
                                             weight=10, floor=fl, saturate_at=sat)),
    ("graded-condemn", lambda fl, sat: GradedCondemnSpec(name="r", field=numeric_condemn,
                                                         weight=10, floor=fl, saturate_at=sat)),
    ("graded-keep", lambda fl, sat: GradedKeepSpec(name="r", field=numeric_any,
                                                   max_discount=10, floor=fl, saturate_at=sat)),
):
    case(f"{label}-floor-below-saturate", lambda b=build: b(4, 5))
    case(f"{label}-floor-equals-saturate", lambda b=build: b(5, 5))
    case(f"{label}-floor-above-saturate", lambda b=build: b(6, 5))

# --- the registry checks the two graded specs make ---
def condemn_on(key):
    return lambda: GradedCondemnSpec(name="r", field=key, weight=10, saturate_at=5)

def keep_on(key):
    return lambda: GradedKeepSpec(name="r", field=key, max_discount=10, floor=0, saturate_at=5)

case("graded-condemn-unknown-field", condemn_on("no_such_field"))
case("graded-keep-unknown-field", keep_on("no_such_field"))
if non_condemn is not None:
    case("graded-condemn-wrong-lane", condemn_on(non_condemn))
if non_numeric is not None:
    case("graded-condemn-not-a-number", condemn_on(non_numeric))
    case("graded-keep-not-a-number", keep_on(non_numeric))

# --- RatingRuleSpec: a vote floor means something on one kind of source and not the other ---
pct = next(s for s in RatingSource if is_percentage_source(s))
voted = next(s for s in RatingSource if not is_percentage_source(s))
case("pct-source-no-votes", lambda: RatingRuleSpec(source=pct, floor=75, min_votes=0))
case("pct-source-one-vote", lambda: RatingRuleSpec(source=pct, floor=75, min_votes=1))
case("voted-source-no-votes", lambda: RatingRuleSpec(source=voted, floor=75, min_votes=0))
case("voted-source-one-vote", lambda: RatingRuleSpec(source=voted, floor=75, min_votes=1))

# --- ProfileSettings caps: each relationship at equality and one past it ---
def caps(**over):
    base = dict(max_items_per_run=10, max_items_per_30d=10,
                max_bytes_per_run=1_000, max_bytes_per_30d=1_000, max_unmeasured_per_run=0)
    return lambda: ProfileSettings(**(base | over))

case("caps-items-equal", caps())
case("caps-items-run-over", caps(max_items_per_run=11))
case("caps-items-run-over-but-off", caps(max_items_per_run=11, caps_enabled=False))
case("caps-bytes-equal", caps(max_bytes_per_run=1_000))
case("caps-bytes-run-over", caps(max_bytes_per_run=1_001))
case("caps-unmeasured-equals-run", caps(max_unmeasured_per_run=10))
case("caps-unmeasured-over-run", caps(max_items_per_run=9, max_unmeasured_per_run=10))

# --- PolicyBody: the weight budget, and the duplicate checks ---
body = DEFAULT_MOVIE_POLICY.model_dump(mode="json")

def reweighted(total):
    raw = json.loads(json.dumps(body))
    sigs = raw["signals"]
    raw["custom_condemn"] = []
    per = total // len(sigs)
    for s in sigs:
        s["weight"] = per
    sigs[0]["weight"] = per + (total - per * len(sigs))
    return lambda: PolicyBody.model_validate(raw)

case("weights-total-100", reweighted(100))
case("weights-total-99", reweighted(99))
case("weights-total-101", reweighted(101))

def duplicated(key):
    raw = json.loads(json.dumps(body))
    if raw.get(key):
        raw[key] = [raw[key][0], json.loads(json.dumps(raw[key][0]))]
    return lambda: PolicyBody.model_validate(raw)

for key in ("gates", "signals", "keep_rating_rules"):
    case(f"duplicate-{key}", duplicated(key))

print(json.dumps({"order": order, "cases": cases}))
"""


#: Zone 3: the gates. These are the hard protections, so the probe records what a caller acts
#: on: the outcome, whether the gate blocked, and the sentence the panel prints.
#:
#: The direction rule here is one question: does this result still hold the file? A gate
#: holds it either by protecting outright, or by blocking because it could not check (a hold
#: standing in for "we could not answer" is `blocked`, and it still holds). A survivor that
#: turns a hold into a does-not-hold has silently withdrawn a protection library-wide, while
#: one that turns a does-not-hold into a hold has only kept a file nobody asked to keep.
GATES_PROBE = r"""
import json
from reaper.engine.gates import (
    DataHorizonGate,
    Facts,
    GateConfig,
    MinDormancyGate,
    RatingFloorGate,
    RatingRule,
    ServerPopularityGate,
    StreamingNowGate,
    history_shortfall,
    lifetime_shortfall,
    progress_is_establishable,
    thaw_defers_to_owner,
)
from reaper.engine.observation import Absent, Known, Unknown
from reaper.ratings import Rating, RatingSource

cases, order = {}, []

def record(name, value):
    order.append(name)
    cases[name] = value

# A gate result reads as "holds/OUTCOME/blocked -- detail", so a change of mind about holding
# the file is the first thing the diff shows.
def verdict(fn):
    try:
        r = fn()
    except Exception as exc:
        return f"RAISED {type(exc).__name__}"
    holds = "holds" if (r.outcome == "PROTECT" or r.blocked) else "lets-go"
    return f"{holds}/{r.outcome}/blocked={r.blocked} -- {' '.join((r.detail or '').split())[:90]}"

def gate_case(name, gate, **facts):
    base = dict(
        title="t",
        days_observed_unwatched=Absent(source="x"),
        distinct_watchers=Absent(source="x"),
        distinct_watchers_all_time=Absent(source="x"),
        size_bytes=Absent(source="x"),
        imdb_rating_tenths=Absent(source="x"),
        imdb_votes=Absent(source="x"),
        season_rank=Absent(source="x"),
        is_streaming_now=Absent(source="x"),
        is_managed=Absent(source="x"),
        in_curated_list=Absent(source="x"),
        is_whitelisted=Absent(source="x"),
    )
    record(name, verdict(lambda: gate.evaluate(Facts(**(base | facts)))))

# --- RatingFloorGate: the empty set, the IMDb fail-closed guard, and the bar itself ---
imdb_bar = RatingRule(source=RatingSource.IMDB, floor=75, min_votes=1000)
pct_bar = RatingRule(source=RatingSource.ROTTEN_TOMATOES_CRITIC, floor=75)
def rated(value, votes, source=RatingSource.IMDB):
    return (Rating(source=source, value=value, votes=votes, provider="p"),)

unreadable = Unknown(reason="r", source="x")
readable = dict(imdb_rating_tenths=Known(value=80, source="x"),
                imdb_votes=Known(value=5000, source="x"))

gate_case("rating-no-rules-configured", RatingFloorGate(rules=()))
# Fail closed: an IMDb bar whose own rating or vote count could not be read must HOLD.
gate_case("rating-imdb-rating-unreadable", RatingFloorGate(rules=(imdb_bar,)),
          imdb_rating_tenths=unreadable, imdb_votes=Known(value=5000, source="x"),
          ratings=rated(8.0, 5000))
gate_case("rating-imdb-votes-unreadable", RatingFloorGate(rules=(imdb_bar,)),
          imdb_rating_tenths=Known(value=80, source="x"), imdb_votes=unreadable,
          ratings=rated(8.0, 5000))
gate_case("rating-clears-the-bar", RatingFloorGate(rules=(imdb_bar,)),
          ratings=rated(8.0, 5000), **readable)
gate_case("rating-exactly-at-the-bar", RatingFloorGate(rules=(imdb_bar,)),
          ratings=rated(7.5, 1000), **readable)
gate_case("rating-just-under-the-bar", RatingFloorGate(rules=(imdb_bar,)),
          ratings=rated(7.4, 5000), **readable)
gate_case("rating-votes-one-short", RatingFloorGate(rules=(imdb_bar,)),
          ratings=rated(8.0, 999), **readable)
gate_case("rating-no-rating-at-all", RatingFloorGate(rules=(imdb_bar,)), **readable)
# A percentage bar carries no IMDb rule, so the fail-closed guard must not fire for it.
# 80% arrives as 8.0 and 70% as 7.0: a percentage is normalized onto the 0-10 scale before
# it reaches here, so passing 80.0 would be 800% and would clear every bar there is.
gate_case("rating-percentage-bar-clears", RatingFloorGate(rules=(pct_bar,)),
          ratings=rated(8.0, None, RatingSource.ROTTEN_TOMATOES_CRITIC))
gate_case("rating-percentage-bar-misses", RatingFloorGate(rules=(pct_bar,)),
          ratings=rated(7.0, None, RatingSource.ROTTEN_TOMATOES_CRITIC))
# match=all fails closed toward NOT protecting; match=any keeps on one cleared bar.
two = RatingFloorGate(rules=(imdb_bar, pct_bar), match="all")
gate_case("rating-all-one-cleared-one-missed", two,
          ratings=rated(8.0, 5000) + rated(7.0, None, RatingSource.ROTTEN_TOMATOES_CRITIC),
          **readable)
gate_case("rating-all-both-cleared", two,
          ratings=rated(8.0, 5000) + rated(8.0, None, RatingSource.ROTTEN_TOMATOES_CRITIC),
          **readable)
gate_case("rating-any-one-cleared-one-missed",
          RatingFloorGate(rules=(imdb_bar, pct_bar), match="any"),
          ratings=rated(8.0, 5000) + rated(7.0, None, RatingSource.ROTTEN_TOMATOES_CRITIC),
          **readable)

# --- ServerPopularityGate: the watcher floor, the pluralization, and the reach bound ---
pop = ServerPopularityGate(GateConfig(threshold=3, window_days=365))
# "well-over" matters as much as "at-floor": every case sitting at or below the floor left
# `count >= floor` free to become `count == floor`, which stops protecting the most-watched
# titles on the server and changes nothing a probe can see.
for label, n in (("at-floor", 3), ("well-over-floor", 10), ("one-under", 2),
                 ("one-watcher", 1), ("nobody", 0)):
    gate_case(f"popularity-{label}", pop, distinct_watchers=Known(value=n, source="x"),
              history_reach_days=Known(value=400.0, source="x"))
# A floor of 1 is the only way to reach the PROTECT arm's singular, and an Absent count is
# the only way to reach the `else 0` fallback.
solo = ServerPopularityGate(GateConfig(threshold=1, window_days=365))
gate_case("popularity-single-watcher-protects", solo,
          distinct_watchers=Known(value=1, source="x"),
          history_reach_days=Known(value=400.0, source="x"))
gate_case("popularity-count-genuinely-absent", solo,
          distinct_watchers=Absent(source="x"),
          history_reach_days=Known(value=400.0, source="x"))
gate_case("popularity-watchers-unreadable", pop,
          distinct_watchers=Unknown(reason="r", source="x"),
          history_reach_days=Known(value=400.0, source="x"))
# A history shorter than the window makes a sub-floor count a LOWER BOUND, not an answer.
gate_case("popularity-reach-shorter-than-window", pop,
          distinct_watchers=Known(value=1, source="x"),
          history_reach_days=Known(value=100.0, source="x"))
gate_case("popularity-reach-exactly-the-window", pop,
          distinct_watchers=Known(value=1, source="x"),
          history_reach_days=Known(value=365.0, source="x"))
gate_case("popularity-reach-unreadable", pop,
          distinct_watchers=Known(value=1, source="x"),
          history_reach_days=Unknown(reason="r", source="x"))

# --- MinDormancyGate: the floor, at it and either side ---
dorm = MinDormancyGate(GateConfig(threshold=1095))
for label, days in (("at-floor", 1095.0), ("one-under", 1094.0), ("well-under", 400.0),
                    ("well-over", 1500.0)):
    gate_case(f"dormancy-{label}", dorm, days_observed_unwatched=Known(value=days, source="x"))
gate_case("dormancy-unreadable", dorm,
          days_observed_unwatched=Unknown(reason="r", source="x"))
gate_case("dormancy-absent", dorm, days_observed_unwatched=Absent(source="x"))

# --- the three switch-shaped gates ---
for name, gate, field in (
    ("streaming", StreamingNowGate(GateConfig()), "is_streaming_now"),
):
    gate_case(f"{name}-true", gate, **{field: Known(value=True, source="x")})
    gate_case(f"{name}-false", gate, **{field: Known(value=False, source="x")})
    gate_case(f"{name}-unreadable", gate, **{field: Unknown(reason="r", source="x")})
gate_case("horizon-unreadable", DataHorizonGate(GateConfig()),
          days_observed_unwatched=Unknown(reason="r", source="x"))

# --- the three pure span helpers, at and either side of every boundary ---
def obs(v):
    return Known(value=v, source="x") if v is not None else Unknown(reason="r", source="x")

for label, reach, needed in (("covers-exactly", 365.0, 365.0), ("one-day-short", 364.0, 365.0),
                             ("a-month-short", 335.0, 365.0), ("just-inside-margin", 336.0, 365.0),
                             ("covers-easily", 900.0, 365.0), ("unreadable", None, 365.0)):
    record(f"history-shortfall-{label}", str(history_shortfall(obs(reach), needed)))
record("lifetime-reach-covers-age", str(lifetime_shortfall(obs(400.0), obs(400.0))))
record("lifetime-reach-short-of-age", str(lifetime_shortfall(obs(399.0), obs(400.0))))
record("lifetime-age-unknown", str(lifetime_shortfall(obs(400.0), obs(None))))
for label, reach, hold in (("spans-exactly", 180, 180), ("one-short", 179, 180),
                           ("spans-easily", 900, 180), ("hold-never-expires", 400, 0),
                           ("hold-of-one-day", 400, 1), ("hold-negative", 400, -1)):
    record(f"progress-{label}", str(progress_is_establishable(reach_days=reach, hold_days=hold)))

for label, v in (("true", True), ("false", False), ("none", None), ("junk-string", "yes"),
                 ("junk-int", 1)):
    record(f"thaw-{label}", str(thaw_defers_to_owner(v)))

# --- the bar's own wording, in each source's units ---
record("bar-text-score", imdb_bar.threshold_text())
record("bar-text-percentage", pct_bar.threshold_text())
record("bar-describe-score-with-votes", imdb_bar.describe_bar())
record("bar-describe-score-no-votes",
       RatingRule(source=RatingSource.IMDB, floor=75, min_votes=0).describe_bar())
record("bar-describe-score-one-vote",
       RatingRule(source=RatingSource.IMDB, floor=75, min_votes=1).describe_bar())
record("bar-describe-percentage", pct_bar.describe_bar())

print(json.dumps({"order": order, "cases": cases}))
"""


#: `ratings.py` is the layer under the rating bar: `RatingFloorGate` holds a file only where
#: `Rating.meets` says the bar cleared, and it can only consider a rating the two parsers
#: managed to interpret. The direction here is the opposite of a validator's: a mutant that
#: makes `meets` answer False, or makes a parser return None where a number was readable, does
#: not refuse a save, it silently withdraws the protection and hands the file to the reap
#: list. `keeps` / `lets-go` is recorded per case for exactly that reason.
RATINGS_PROBE = r"""
import json
from reaper.ratings import (
    Rating,
    RatingSource,
    describe_votes,
    from_plex,
    from_radarr,
    is_percentage_source,
    merge_by_source,
    pick,
    source_label,
)

cases, order = {}, []

def record(name, value):
    order.append(name)
    cases[name] = value

def rating(source, value, votes=None):
    return Rating(source=source, value=value, votes=votes, provider="p")

# --- Rating.meets: does the bar clear, and which way does a mutant push it? ---
# `RatingFloorGate.evaluate` calls this as `rating.meets(rule.floor / 10, ...)` and appends to
# `cleared` only where it answers True, so False is "this bar was missed" -- the deletable side.
def bar(name, source, value, floor, votes=None, min_votes=0):
    try:
        answer = rating(source, value, votes).meets(floor, min_votes=min_votes)
    except Exception as exc:
        record(name, f"RAISED {type(exc).__name__}")
        return
    record(name, f"{'keeps' if answer else 'lets-go'}/{answer}")

IMDB, RT = RatingSource.IMDB, RatingSource.ROTTEN_TOMATOES_CRITIC

# An uninterpretable source may never justify a deletion, so UNKNOWN fails closed to lets-go
# even where the number itself sits far above the bar.
bar("meets-unknown-source-far-above-bar", RatingSource.UNKNOWN, 9.9, 1.0)
# The floor, at it and either side. Equality is the case an inclusive `>=` turns on.
bar("meets-floor-exactly", IMDB, 7.5, 7.5, votes=5000, min_votes=1000)
bar("meets-a-hair-under-floor", IMDB, 7.4, 7.5, votes=5000, min_votes=1000)
bar("meets-a-hair-over-floor", IMDB, 7.6, 7.5, votes=5000, min_votes=1000)
bar("meets-floor-of-zero", IMDB, 0.0, 0.0)
bar("meets-floor-of-ten", IMDB, 10.0, 10.0)
# The vote floor, at it and either side. `votes < min_votes` refuses, so equality clears.
bar("meets-votes-exactly-at-floor", IMDB, 8.0, 7.5, votes=1000, min_votes=1000)
bar("meets-votes-one-short", IMDB, 8.0, 7.5, votes=999, min_votes=1000)
bar("meets-votes-one-over", IMDB, 8.0, 7.5, votes=1001, min_votes=1000)
# A vote floor of 1 against a single vote: both ends of `min_votes > 0` and `votes < min_votes`
# at their smallest legal values, which is where an off-by-one hides.
bar("meets-one-vote-against-a-floor-of-one", IMDB, 8.0, 7.5, votes=1, min_votes=1)
bar("meets-zero-votes-against-a-floor-of-one", IMDB, 8.0, 7.5, votes=0, min_votes=1)
bar("meets-no-votes-against-a-floor-of-one", IMDB, 8.0, 7.5, votes=None, min_votes=1)
# min_votes of 0 is "no vote floor asked for", so an absent or zero count must still clear.
bar("meets-no-votes-and-no-vote-floor", IMDB, 8.0, 7.5, votes=None, min_votes=0)
bar("meets-zero-votes-and-no-vote-floor", IMDB, 8.0, 7.5, votes=0, min_votes=0)
# A percentage source counts no votes at all, so a vote floor must not apply to it -- the bar
# clears on the value alone even with no count and a four-figure floor asked for.
bar("meets-percentage-ignores-a-vote-floor", RT, 8.4, 7.5, votes=None, min_votes=1000)
bar("meets-percentage-under-bar-still-refused", RT, 7.4, 7.5, votes=None, min_votes=1000)

record("has-vote-count-imdb", str(rating(IMDB, 8.0).has_meaningful_vote_count))
record("has-vote-count-percentage", str(rating(RT, 8.0).has_meaningful_vote_count))
record("has-vote-count-unknown", str(rating(RatingSource.UNKNOWN, 8.0).has_meaningful_vote_count))
for source in RatingSource:
    record(f"is-percentage-{source.value}", str(is_percentage_source(source)))
    record(f"label-{source.value}", source_label(source))

# --- the two parsers: None here is a rating the gate never gets to consider ---
def parsed(name, fn):
    try:
        out = fn()
    except Exception as exc:
        record(name, f"RAISED {type(exc).__name__}")
        return
    if out is None:
        record(name, "dropped")
        return
    if isinstance(out, list):
        record(name, "; ".join(f"{r.source.value}={r.value:.3f}/{r.votes}" for r in out) or "empty")
        return
    record(name, f"{out.source.value}={out.value:.3f}/{out.votes}")

IMAGE = "imdb://image.rating.imdb"
RT_IMAGE = "rottentomatoes://image.rating.ripe"
parsed("plex-none-value", lambda: from_plex(None, IMAGE))
parsed("plex-empty-value", lambda: from_plex("", IMAGE))
parsed("plex-junk-value", lambda: from_plex("eight", IMAGE))
parsed("plex-no-image", lambda: from_plex("8.2", None))
parsed("plex-unreadable-image", lambda: from_plex("8.2", "who://knows"))
parsed("plex-imdb", lambda: from_plex("8.2", IMAGE))
parsed("plex-rt-critic", lambda: from_plex("8.4", RT_IMAGE))
parsed("plex-rt-audience", lambda: from_plex("8.4", RT_IMAGE, audience=True))
parsed("plex-imdb-in-audience-slot", lambda: from_plex("8.2", IMAGE, audience=True))
# Plex serves 0-10 in every slot, so a percentage-shaped source ABOVE 10 proves an agent that
# skipped the normalization. Both sides of that 10, and both ends of the 0-10 range itself.
parsed("plex-percentage-raw-84", lambda: from_plex("84", RT_IMAGE))
parsed("plex-percentage-exactly-10", lambda: from_plex("10", RT_IMAGE))
parsed("plex-percentage-just-over-10", lambda: from_plex("10.1", RT_IMAGE))
parsed("plex-percentage-raw-100", lambda: from_plex("100", RT_IMAGE))
parsed("plex-percentage-raw-101", lambda: from_plex("101", RT_IMAGE))
parsed("plex-score-exactly-10", lambda: from_plex("10", IMAGE))
parsed("plex-score-just-over-10", lambda: from_plex("10.1", IMAGE))
parsed("plex-score-exactly-0", lambda: from_plex("0", IMAGE))
parsed("plex-score-negative", lambda: from_plex("-0.1", IMAGE))

parsed("radarr-not-a-dict", lambda: from_radarr(None))
parsed("radarr-a-list", lambda: from_radarr([]))
parsed("radarr-empty", lambda: from_radarr({}))
parsed("radarr-imdb", lambda: from_radarr({"imdb": {"value": 8.2, "votes": 1200}}))
parsed("radarr-imdb-no-votes", lambda: from_radarr({"imdb": {"value": 8.2}}))
parsed("radarr-entry-not-a-dict", lambda: from_radarr({"imdb": 8.2}))
parsed("radarr-value-none", lambda: from_radarr({"imdb": {"value": None}}))
parsed("radarr-value-empty", lambda: from_radarr({"imdb": {"value": ""}}))
parsed("radarr-value-junk", lambda: from_radarr({"imdb": {"value": "eight"}}))
# Radarr hands percentages raw, so 96 is 9.6 and 100 is the top of the scale.
parsed("radarr-rt-96", lambda: from_radarr({"rottenTomatoes": {"value": 96}}))
parsed("radarr-rt-exactly-100", lambda: from_radarr({"rottenTomatoes": {"value": 100}}))
parsed("radarr-rt-101", lambda: from_radarr({"rottenTomatoes": {"value": 101}}))
parsed("radarr-rt-zero", lambda: from_radarr({"rottenTomatoes": {"value": 0}}))
parsed("radarr-metacritic-85", lambda: from_radarr({"metacritic": {"value": 85}}))
parsed("radarr-tmdb-exactly-10", lambda: from_radarr({"tmdb": {"value": 10}}))
parsed("radarr-tmdb-just-over-10", lambda: from_radarr({"tmdb": {"value": 10.1}}))
parsed("radarr-imdb-negative", lambda: from_radarr({"imdb": {"value": -0.1}}))
parsed("radarr-trakt", lambda: from_radarr({"trakt": {"value": 7.7, "votes": 40}}))
# Votes on a percentage source, and votes this code cannot read. Both were missing from the
# first corpus, and both times the runner reported a survivor as "no observable change": a
# percentage source is the only place `and` differs from `or` on that line, and the malformed
# count is the only thing that enters the `except` at all -- where `votes` is unassigned this
# iteration, so dropping the assignment leaks the PREVIOUS source's count into this rating.
parsed("radarr-percentage-with-a-vote-count",
       lambda: from_radarr({"rottenTomatoes": {"value": 96, "votes": 500}}))
parsed("radarr-votes-with-a-thousands-separator",
       lambda: from_radarr({"imdb": {"value": 8.2, "votes": "1,234"}}))
parsed("radarr-votes-as-a-list", lambda: from_radarr({"imdb": {"value": 8.2, "votes": [1]}}))
parsed("radarr-unreadable-votes-after-a-readable-count", lambda: from_radarr({
    "imdb": {"value": 8.2, "votes": 1200}, "tmdb": {"value": 7.9, "votes": "3,000"},
}))
parsed("radarr-every-source", lambda: from_radarr({
    "imdb": {"value": 8.2, "votes": 1200}, "tmdb": {"value": 7.9, "votes": 30},
    "metacritic": {"value": 85}, "rottenTomatoes": {"value": 96}, "trakt": {"value": 7.7},
}))

# --- provenance and the operator's own sentence ---
imdb_many = rating(IMDB, 8.2, 120000)
parsed("pick-present", lambda: pick([imdb_many], IMDB))
parsed("pick-absent", lambda: pick([imdb_many], RT))
parsed("pick-from-empty", lambda: pick([], IMDB))
parsed("merge-prefers-the-first-group",
       lambda: list(merge_by_source([imdb_many], [rating(IMDB, 1.0, 3)])))
parsed("merge-keeps-distinct-sources",
       lambda: list(merge_by_source([imdb_many], [rating(RT, 8.4)])))
parsed("merge-of-nothing", lambda: list(merge_by_source([], ())))

for label, count in (("none", None), ("zero", 0), ("one", 1), ("two", 2), ("many", 120000)):
    record(f"votes-clause-{label}", describe_votes(count))
record("describe-imdb", imdb_many.describe())
record("describe-percentage-no-votes", rating(RT, 8.4).describe())
record("user-imdb", imdb_many.describe_for_user())
record("user-imdb-one-vote", rating(IMDB, 8.2, 1).describe_for_user())
record("user-imdb-no-votes", rating(IMDB, 8.2).describe_for_user())
record("user-percentage", rating(RT, 8.4).describe_for_user())
record("user-percentage-rounds", rating(RT, 8.45).describe_for_user())

print(json.dumps({"order": order, "cases": cases}))
"""


#: `inspect` is the dangerous-config detector: it refuses nothing and deletes nothing, it
#: only speaks. The whole answer is the sentences it produces, so the probe records every
#: warning verbatim. A corpus that only recorded counts would call a reworded remedy no
#: change.
#:
#: The direction rule is which way a survivor moves the operator. A mutant that drops a
#: warning leaves someone staring at a page that looks fine while a dangerous setting stays
#: live; one that adds a warning to a healthy policy is noise, and noise is what teaches an
#: operator to ignore the panel. Dropping a warning is worse, and `severity` is part of each
#: recorded answer because demoting a `danger` to a `warn` is the same loss in a quieter form.
#:
#: The corpus is built around each threshold's boundary rather than an arbitrary example
#: value, since a case driven well inside a region cannot tell `>=` from `>`. Each threshold
#: here is driven at the value, and at one point on either side of it.
INSPECT_PROBE = r"""
import json
from reaper.engine.policy import (
    DEFAULT_MOVIE_POLICY,
    DEFAULT_TV_POLICY,
    GateId,
    GateSetting,
    PolicyBody,
    ProfileSettings,
    RatingRuleSpec,
    SignalSetting,
)
from reaper.engine.policy_warnings import inspect
from reaper.engine.signals import SignalId
from reaper.ratings import RatingSource

cases, order = {}, []

# Every warning the page would print, in order, severity and field included.
def record(name, body, **kwargs):
    order.append(name)
    try:
        got = inspect(body, kwargs.pop("settings", None) or ProfileSettings(), **kwargs)
    except Exception as exc:
        cases[name] = f"RAISED {type(exc).__name__}: {' '.join(str(exc).split())[:160]}"
        return
    # The typed reason's repr, not composed English (#868): id
    # and params are what a mutant can actually change, and a repr is deterministic where a
    # composed sentence would need the frontend catalog this script does not load.
    cases[name] = [f"{w.severity}|{w.field}|{w.reason!r}" for w in got]

def policy(**overrides):
    base = {
        "media_type": "movie",
        "condemn_at": 70,
        "gates": (GateSetting(gate=GateId.WHITELISTED),),
        "signals": (SignalSetting(signal=SignalId.UNWATCHED, weight=100, saturate_at=730),),
    }
    return PolicyBody(**{**base, **overrides})

# --- the control. A shipped default that starts warning is a mutant caught by direction
# alone, and without it a corpus of deliberately-broken policies cannot tell "this branch
# stopped firing" from "this branch now fires everywhere".
record("default-movie", DEFAULT_MOVIE_POLICY)
record("default-tv", DEFAULT_TV_POLICY)

# --- the rating bar, on a source read out of ten: `floor >= 90` and `floor <= 20` ---
def rating_bar(source, floor, votes):
    return policy(
        gates=(GateSetting(gate=GateId.RATING_FLOOR),),
        keep_rating_rules=(RatingRuleSpec(source=source, floor=floor, min_votes=votes),),
    )

for floor in (19, 20, 21, 89, 90, 91):
    record(f"ten-scale-bar-{floor}", rating_bar(RatingSource.IMDB, floor, 1000))

# --- the same bar on a percentage source, which takes the OTHER arm and no vote floor.
# Nothing in `tests/` drove this arm at all: its two sentences appear in no test file.
for floor in (19, 20, 21):
    record(
        f"percentage-bar-{floor}",
        rating_bar(RatingSource.ROTTEN_TOMATOES_CRITIC, floor, 0),
    )
# The arm each floor does NOT take, so a mutant merging the two is visible: 91 is loud on
# the ten-point scale and silent as a percentage, where 91% is an ordinary bar.
record("percentage-bar-91", rating_bar(RatingSource.ROTTEN_TOMATOES_CRITIC, 91, 0))

# --- the watch window: `window_days < 30`, and the gate switch that gates it ---
def window(days, enabled=True):
    return policy(
        gates=(
            GateSetting(
                gate=GateId.SERVER_POPULARITY, threshold=2, window_days=days, enabled=enabled
            ),
        )
    )

for days in (29, 30, 31):
    # Reach is stated and long, so the short-window warning is the only one in play rather
    # than the shortfall lane the same window feeds.
    record(f"window-{days}", window(days), history_reach_days=800.0)
    # The merged remedy: the same boundary read a second time, down a different branch.
    record(f"window-{days}-short-history", window(days), history_reach_days=3.0)
record("window-29-gate-off", window(29, enabled=False), history_reach_days=800.0)

# --- the condemn threshold: `condemn_at <= 30`, the one `danger` on this list ---
for at in (29, 30, 31):
    record(f"condemn-at-{at}", policy(condemn_at=at))

print(json.dumps({"order": order, "cases": cases}))
"""


ZONES: dict[str, Zone] = {
    "policy-repair-shims": Zone(
        module=Path("src/reaper/engine/policy_migrations.py"),
        functions=("rebalance", "recover_rating_rules"),
        tests=(
            "tests/test_policy.py::TestRebalancingAnOldPolicy",
            "tests/test_policy.py::TestRestoringALostRatingBar",
            "tests/test_profiles.py",
        ),
        omits=(
            Omission(
                "The one-way list-protection conversion and its helpers. This zone is the two "
                "shims that rewrite a stored body on LOAD, and `REPAIR_SHIM_PROBE` reads a "
                "repaired body back; the conversion runs once per body and answers a different "
                "question, so it wants its own zone and probe rather than a share of this one. "
                "`authorable_media_scope` joined them at #549: it decides what the POLICY EDITOR "
                "offers, which no probe here reads and which gates no deletion.",
                (
                    "authorable_media_scope",
                    "convert_list_protections",
                    "has_legacy_list_protections",
                    "conversion_list_names",
                    "legacy_keep_tags",
                    "own_list_media_scope",
                    "library_media_types",
                    "_config_value",
                ),
            ),
            Omission(
                "Declared in `policy-save-boundary` instead, as `PolicyBody._drop_retired_gates`, "
                "which is the validator that calls it and the surface the probe drives.",
                ("drop_retired_gate_keys",),
            ),
        ),
        probe=REPAIR_SHIM_PROBE,
    ),
    "policy-save-boundary": Zone(
        module=Path("src/reaper/engine/policy.py"),
        functions=(
            "GateSetting._protective_floors",
            "SignalSetting._floor_below_saturation",
            "ConditionSpec._valid_protect_condition",
            "BooleanCondemnSpec._valid_condemn_condition",
            "GradedCondemnSpec._valid_graded",
            "GradedKeepSpec._valid_keep",
            "RatingRuleSpec._vote_floor_matches_the_source",
            "PolicyBody._pin_to_the_running_scorer",
            "PolicyBody._drop_retired_gates",
            "PolicyBody._rewatch_odds_row",
            "PolicyBody._returned_row",
            "PolicyBody._weights_total_one_hundred",
            "PolicyBody._no_duplicates",
            "ProfileSettings._run_cap_within_rolling_cap",
        ),
        tests=(
            "tests/test_policy.py",
            "tests/test_custom_condemn.py",
            "tests/test_signal_quality.py",
            "tests/test_profiles.py",
            "tests/test_policy_permutations.py",
        ),
        omits=(
            Omission(
                "Translators and hashes, not validators. `SAVE_BOUNDARY_PROBE` offers a body "
                "and reads back accepted-or-rejected, so a mutant in one of these changes what "
                "a scan then does with an accepted body rather than what the boundary lets "
                "through. That is the scan's behavior and wants a probe that runs a scan.",
                (
                    "ConditionSpec.to_condition",
                    "PolicyBody.popularity_window_days",
                    "PolicyBody.returned_absence_days",
                    "PolicyBody.rating_rules",
                    "PolicyBody.keep_configs",
                    "PolicyBody.custom_signal_configs",
                    "PolicyBody._gathering_evidence",
                    "PolicyBody.canonical_json",
                    "PolicyBody.policy_hash",
                    "PolicyBody.scoring_hash",
                    "PolicyBody.evidence_hash",
                    "combine_hashes",
                ),
            ),
            Omission(
                "Operator copy, not a decision. Its twin `fields._join_or` sits in another "
                "module, so a zone over one of them would report on half a pair.",
                ("join_and",),
            ),
        ),
        probe=SAVE_BOUNDARY_PROBE,
    ),
    "engine-gates": Zone(
        module=Path("src/reaper/engine/gates.py"),
        functions=(
            "thaw_defers_to_owner",
            "RatingRule.describe_bar",
            "_rating_value",
            "blocked_reason",
            "no_key_reason_id",
            "no_key_reason",
            "no_added_at_reason",
            "no_size_reason",
            "RatingFloorGate.evaluate",
            "StreamingNowGate.evaluate",
            "history_shortfall",
            "lifetime_shortfall",
            "progress_is_establishable",
            "ServerPopularityGate.evaluate",
            "MinDormancyGate.evaluate",
            "RewatchOddsGate.evaluate",
            "ReturnedGate.evaluate",
            "wilson_upper",
            "DataHorizonGate.evaluate",
            "evaluate_all",
            # `_blocked` matters most in principle: it is the one fail-closed helper every
            # gate routes through, so deleting its `Unknown` guard would withdraw the block
            # from every gate that uses it at once. `_miss_reason` matters most in practice,
            # since it is where survivors have actually turned up before, including one live
            # on a default policy.
            # The four `Evaluation` properties carry no mutable token and report zero, which
            # is the honest answer `evaluate_all` already gave.
            "_blocked",
            "GateResult.fired",
            "RatingFloorGate._miss_reason",
            "Evaluation.checked_and_did_not_fire",
            "Evaluation.protected",
            "Evaluation.blocked",
            "Evaluation.protectors",
            "Evaluation.could_not_be_checked",
        ),
        omits=(
            Omission(
                "Protocol members. The body is `...`, so there is nothing to mutate and no "
                "implementation to answer for; the real ones are declared above.",
                ("Gate.id", "Gate.evaluate"),
            ),
        ),
        tests=(
            "tests/test_engine_invariants.py",
            "tests/test_signal_quality.py",
            "tests/test_review_chips.py",
            "tests/test_season_pruning.py",
            "tests/test_fields.py",
            "tests/test_policy.py",
            "tests/test_facts_codec.py",
            "tests/test_override_truth.py",
            # The only test file that kills a deletion of either `RatingFloorGate` guard.
            # Without it, those two mutants would report as survivors even though the full
            # suite fails on them, and a false survivor costs a reader the trust the real
            # ones need.
            "tests/test_policy_permutations.py",
        ),
        probe=GATES_PROBE,
    ),
    "ratings": Zone(
        module=Path("src/reaper/ratings.py"),
        functions=(
            "Rating.meets",
            # Declared alongside `meets`. Hoisting a shared helper out of a zoned function
            # moves its mutants somewhere no zone covers unless the helper is named too, the
            # same kind of gap `describe_votes` opened once before (docs/LEARNINGS.md).
            "Rating.short_of_vote_floor",
            "Rating.has_meaningful_vote_count",
            "Rating.describe",
            "Rating.describe_for_user",
            "describe_votes",
            "is_percentage_source",
            "source_label",
            "_to_ten",
            "from_plex",
            "from_radarr",
            "pick",
            "merge_by_source",
        ),
        tests=(
            "tests/test_upstream_quirks.py",
            "tests/test_engine_invariants.py",
            "tests/test_display_meta.py",
            "tests/test_facts_codec.py",
            "tests/test_review_scan.py",
            "tests/test_season_scan.py",
            "tests/test_plex_sweep.py",
            "tests/test_policy.py",
        ),
        probe=RATINGS_PROBE,
    ),
    # One function, and by far the largest zone here: every warning the policy editor
    # prints is a branch inside `inspect`. Splitting it by warning would report per-slice
    # kill rates that no longer add up to one answer about the detector, so it stays
    # scoped whole.
    "policy-inspect": Zone(
        module=Path("src/reaper/engine/policy_warnings.py"),
        # `_protect_blocks_on_reach` decides one of `inspect`'s warnings, so it belongs in
        # this zone too. `INSPECT_PROBE` reads the warnings out, so its mutants are
        # answerable here.
        functions=("inspect", "_protect_blocks_on_reach"),
        tests=(
            "tests/test_policy.py",
            "tests/test_custom_condemn.py",
            "tests/test_season_pruning.py",
            "tests/test_policy_permutations.py",
            # The route that hands these warnings to the editor. It kills nothing the file
            # above does not, and it is here because a mutant that changes a warning's shape
            # breaks at the serializer rather than at an assertion.
            "tests/test_api.py::TestPolicyValidation",
        ),
        probe=INSPECT_PROBE,
    ),
}
DEFAULT_ZONE = "policy-repair-shims"

#: The two delegating validators contribute no mutants, since they hand the whole decision to
#: the `fields` registry and leave nothing here to flip. They are still named in the zone,
#: because a zone claiming to be "the save boundary" while quietly omitting two of its members
#: would overstate its own coverage. Their logic lives in a separate zone.


def probe(zone: Zone, workdir: Path, timeout: float) -> dict[str, object] | str:
    try:
        proc = subprocess.run(  # noqa: S603 -- fixed argv, no caller input
            ["uv", "run", "python", "-c", zone.probe],  # noqa: S607 -- `uv` is how CLAUDE.md runs everything
            cwd=workdir,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return "probe timed out"
    if proc.returncode != 0:
        last = proc.stderr.strip().splitlines()[-1:]
        return f"probe failed: {last[0] if last else '(no stderr)'}"
    loaded: dict[str, object] = json.loads(proc.stdout)["cases"]
    return loaded


@dataclass
class FunctionTally:
    generated: int = 0
    status: dict[str, int] = field(default_factory=dict)


def tally(zone: Zone, mutants: list[Mutant]) -> dict[str, FunctionTally]:
    """Return per-function counts, keyed on the functions the zone declares.

    Counting only the mutants that were generated would drop a function that generated
    none, and a function missing from the report would read exactly like one whose
    mutants all died. A row showing zero says the run tried nothing at all in that
    function, which is a different answer from "defended" and needs to look like one.
    """
    rows = {fn: FunctionTally() for fn in zone.functions}
    for m in mutants:
        row = rows.setdefault(m.func, FunctionTally())
        row.generated += 1
        row.status[m.status] = row.status.get(m.status, 0) + 1
    return rows


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--zone", choices=sorted(ZONES), default=DEFAULT_ZONE)
    parser.add_argument("--report", type=Path, default=None)
    parser.add_argument(
        "--workers",
        type=int,
        default=min(8, max(1, (os.cpu_count() or 2) - 2)),
        help="parallel worker copies; each gets its own tree and venv",
    )
    parser.add_argument(
        "--keep-workers",
        action="store_true",
        help="leave the worker copies on disk to inspect a failing mutant",
    )
    args = parser.parse_args()

    name, zone = args.zone, ZONES[args.zone]
    report = args.report or REPO / f"mutation-report-{name}.json"

    # Checked before anything is copied or run, since a zone whose list has drifted would
    # report a clean sweep of a surface it never asked about. `tests/test_repo_hygiene.py`
    # checks every zone in CI, but this script does not run in CI, so this is the same check
    # for the person running it by hand.
    if drift := zone_drift(name, zone):
        raise SystemExit("\n".join(drift))

    original = (REPO / zone.module).read_text()
    mutants = generate(original, zone)
    workers = max(1, min(args.workers, len(mutants) or 1))
    print(f"zone {name}: {len(mutants)} mutants across {len(zone.functions)} targets", flush=True)
    skipped = skipped_guards(original, func_spans(original, zone.functions, zone.module))
    for why, lines in skipped.items():
        if lines:
            at = ", ".join(str(n) for n in lines)
            print(f"  {len(lines)} branch(es) the guard operator cannot delete -- {why}: {at}")

    pool = Path(tempfile.mkdtemp(prefix=f"mutation-{name}-"))
    started = time.time()
    roots = [make_worker(pool / f"w{i}") for i in range(workers)]
    print(f"{workers} worker copies ready in {time.time() - started:.1f}s\n", flush=True)

    try:
        baseline = probe(zone, roots[0], 240)
        if isinstance(baseline, str):
            raise SystemExit(f"baseline {baseline}")

        started = time.time()
        if (status := run_tests(zone, roots[0], 900)) != "survived":
            raise SystemExit(f"baseline suite is not green ({status}); fix that before mutating")
        each = time.time() - started
        print(
            f"baseline green in {each:.1f}s -> about "
            f"{len(mutants) * each / 60 / workers:.0f} min across {workers} workers\n",
            flush=True,
        )

        speak = threading.Lock()
        done = itertools.count(1)

        def report_one(m: Mutant, extra: str = "") -> None:
            mark = {"killed": "kill ", "survived": "SURV "}.get(m.status, m.status)
            with speak:
                print(f"[{next(done)}/{len(mutants)}] {mark} {m.label}{extra}", flush=True)
                for detail in m.direction:
                    print(f"          {detail}", flush=True)

        def process(m: Mutant, root: Path) -> None:
            target = root / zone.module
            try:
                mutated = splice(original, m)
                compile(mutated, str(target), "exec")
            except ValueError as exc:
                m.status = "skipped"
                report_one(m, f": {exc}")
                return
            except SyntaxError:
                m.status = "invalid"
                report_one(m)
                return
            target.write_text(mutated)
            m.status = run_tests(zone, root, max(120.0, each * 6))
            if m.status == "survived":
                after = probe(zone, root, 240)
                if isinstance(after, str):
                    m.direction = [after]
                else:
                    m.direction = [
                        f"{k}: {json.dumps(baseline.get(k))} -> {json.dumps(v)}"
                        for k, v in after.items()
                        if baseline.get(k) != v
                    ] or ["no change on the probe corpus -- equivalent, or a case is missing"]
            report_one(m)

        def drain(index: int) -> None:
            """Run one thread per worker, taking every mutant.

            Round-robin rather than contiguous blocks, because mutants in the same
            function tend to cost the same. Slicing by position would hand one worker
            every slow one.
            """
            root = roots[index]
            for m in mutants[index::workers]:
                process(m, root)

        threads = [threading.Thread(target=drain, args=(i,)) for i in range(workers)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
    finally:
        if args.keep_workers:
            print(f"\nworker copies left at {pool}", flush=True)
        else:
            shutil.rmtree(pool, ignore_errors=True)
        # The real tree is only ever read. Mutants are written into the worker copies instead.
        assert (REPO / zone.module).read_text() == original, "the source under test changed"

    per_function = tally(zone, mutants)
    report.write_text(
        json.dumps(
            {
                "zone": name,
                "functions": {
                    fn: {"generated": t.generated, "status": t.status}
                    for fn, t in per_function.items()
                },
                "mutants": [
                    {
                        "id": m.ident,
                        "func": m.func,
                        "line": m.line,
                        "kind": m.kind,
                        "from": m.original,
                        "to": m.replacement,
                        "status": m.status,
                        "direction": m.direction,
                    }
                    for m in mutants
                ],
            },
            indent=2,
        )
        + "\n"
    )

    counts: dict[str, int] = {}
    for m in mutants:
        counts[m.status] = counts.get(m.status, 0) + 1
    print("\n== per function ==")
    width = max(len(f) for f in zone.functions)
    for fn in zone.functions:
        row = per_function[fn]
        if not row.generated:
            print(f"  {fn:<{width}}  0 mutants -- nothing was tried here")
            continue
        verdict = ", ".join(f"{n} {status}" for status, n in sorted(row.status.items()))
        print(f"  {fn:<{width}}  {row.generated} mutants: {verdict}")
    print("\n== summary ==")
    for status in sorted(counts):
        print(f"  {status}: {counts[status]}")
    print(f"\nreport: {report}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
