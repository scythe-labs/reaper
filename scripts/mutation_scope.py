"""Scoped mutation runner: break one function on purpose, see whether a test notices.

Not a coverage tool and not a score. It answers one question per mutant -- *if this line
silently stopped doing its job, would the suite fail?* -- and a survivor is a branch nothing
is defending. `docs/LEARNINGS.md` records what the first run found.

Run it:

    uv run python scripts/mutation_scope.py --zone policy-repair-shims
    uv run python scripts/mutation_scope.py --zone policy-save-boundary
    uv run python scripts/mutation_scope.py --zone engine-gates

Add a zone to `ZONES` below: the module, the functions (`Class.method` for a method), the tests
that could plausibly kill a mutant in them, and a probe. **Scope by function, not by file.**
The two shims are 60 mutants and three minutes, the save boundary 78; the same operators over
all of `policy.py` would be past a thousand, which is how an exercise like this stops being
run at all.

Two passes per mutant:

1. *kill or survive* -- run the tests against the mutated source.
2. *direction* -- for survivors only, re-run the zone's `probe` corpus and diff
   against baseline. This is the half that makes the output readable: "seven survived" is a
   number, while "each of these makes the shim refuse a legal repair, leaving the operator's
   bar empty" is a finding. Reaper's whole safety argument is directional, so a survivor that
   widens what gets deleted is not the same finding as one that keeps more.

Mutation is a byte-precise text splice, never an AST unparse, so a mutant differs from the
original on one line and reports as `func:line '<=' -> '<'`, readable straight against the
source. Splicing is also what gives `1 <= floor <= 100` two separately-addressable mutants:
the AST hands back a single `Compare` node with a list of operators and no position for any
of them.

**A probe only sees what it records, and each zone is responsible for something different.**
Zone 1 returns a repaired body, zone 2 an accept-or-refuse plus the operator's sentence, zone 3
whether the gate still holds the file. Getting that wrong has cost three findings so far: a
corpus that imported a test fixture inherited the blind spot hiding the defect; one recording
only a verdict called an operator string reading "-1" no change; and one passing a percentage
rating unnormalized tested an 800% bar against a 75% floor. So **read "no change on the probe
corpus" as a question**, and sanity-check what the baseline actually answers before trusting a
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
class Zone:
    """What to mutate, what gets to object, and how to tell survivors apart.

    ``probe`` is Python source run in a fresh subprocess against the mutated module. It must
    print ``{"cases": {name: answer}}`` as JSON. Whatever an "answer" is, it has to be a value
    the direction of a change is readable from: a returned body for a repair shim, an
    accepted-or-rejected verdict for a validator.
    """

    module: Path
    functions: tuple[str, ...]
    tests: tuple[str, ...]
    probe: str


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
    """Body line span of each named function, docstring excluded.

    The docstrings here are long and carry prose comparisons; mutating them produces mutants
    no test could ever kill and no author would ever act on.

    A method is named ``Class.method``, because a bare name is not unique: `engine/gates.py`
    holds nine `evaluate` methods, and keying on the bare name silently kept the last one --
    a report about a function nobody asked for, reading exactly like a real answer. So an
    ambiguous name raises instead of binding, the way anything else here refuses to guess.
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

    This is the operator that catches a line doing real work that nothing asserts on -- the
    shape rule 7/24 is about. It found the schema restamp.
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
    """Whether ``if <test>:`` fits on the one line the splice can rewrite."""
    return node.test.lineno == node.lineno and node.test.end_lineno == node.test.lineno


def skipped_guards(source: str, spans: dict[str, tuple[int, int]]) -> dict[str, list[int]]:
    """Branches the operator below cannot make dead, grouped by the reason, so `main` says so.

    The two spellings it rejects (rule 147). A **wrapped** ``if`` header cannot take a
    byte-precise single-line splice; a **ternary** is a branch this operator was simply not
    written for. Token swaps still reach inside both tests, so neither is wholly unmutated --
    what is missing is specifically the delete-this-guard edit.

    Reporting the count is the whole point: "27 guards mutated" printed beside a silent 11
    skipped reads as a complete sweep, which is the claim shape this runner exists to
    distrust. A bound the tool declines to mention is the same silent cap the guard operator
    was added to remove.
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

    The operator the rest of the set could not express. Token swaps need a token with an
    opposite, and the deletion above walks assignments only, so a guard on ``isinstance`` or
    ``in`` produced nothing at all -- and a function holding no mutable token reported exactly
    like one whose mutants all died. That blind spot covered the shape this codebase is most
    about: rule 2's fail-closed guards parse and type-check after the edit, so nothing else
    catches their removal either.

    Two choices worth keeping. **The test is still evaluated**, rather than replaced by
    ``False`` outright, so a walrus (``if blocked := _blocked(...)``) still binds its name and
    the mutant fails on the missing branch instead of a ``NameError`` two lines down --
    reporting the guard as undefended for the right reason. And the parentheses are not
    cosmetic: ``if a or b:`` spliced without them binds as ``a or (b and False)``, which is
    still live down the ``a`` arm and would report a killed guard as surviving.

    Single-line ``if`` headers only, like every other operator here, because the splice is
    byte precise: a test wrapped across lines is skipped rather than half-rewritten, and a
    ternary is left alone. `skipped_guards` groups both and `main` prints them -- 77 of the 78
    ``if``s in the shipped zones are covered, beside 10 ternaries that are not.
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
        # `not x` -> `x`: swallow the trailing space so the result still parses.
        head, tail = line[: m.col], line[m.end_col :].lstrip(" ")
        lines[m.line - 1] = head + tail
    else:
        lines[m.line - 1] = line[: m.col] + m.replacement + line[m.end_col :]
    return "".join(lines)


#: Everything a worker copy needs to run the suite. Two non-obvious members:
#: `README.md`, because `[project] readme` points at it and the build backend reads it during
#: `uv sync`; and `frontend/src`, because some backend tests reconcile a Python vocabulary
#: against the TSX that renders it (`test_review_chips.py` opens `WhyPanel.tsx`). Listed as a
#: nested path deliberately -- copying all of `frontend` would drag `node_modules` into every
#: worker. If a zone's tests need something absent here, the baseline check says so before any
#: mutant runs, which is exactly how `frontend/src` got found.
WORKER_PATHS = (
    "src",
    "tests",
    "alembic",
    "alembic.ini",
    "pyproject.toml",
    "uv.lock",
    "README.md",
    "frontend/src",
)


def make_worker(root: Path) -> Path:
    """A throwaway copy of the tree with its own venv, so a mutant never touches the real one.

    This is what lets the run go parallel, but the isolation is the bigger win on its own: the
    first version of this script wrote each mutant into the actual source file and restored it
    in a `finally`, which means an interrupted run leaves the tree modified. Here the real
    checkout is only ever read.

    Cheap enough to stop being a trade-off. On APFS the copy is a clone (`cp -Rc`, ~0.1s) and
    `uv sync` against a warm cache is well under a second, so a worker costs about as much as
    one mutant. `uv sync` inside the copy is also what makes the isolation real: it installs
    the project editable against the COPY's `src`, so `import reaper` there cannot reach back
    to the original.
    """
    root.mkdir(parents=True, exist_ok=True)
    for rel in WORKER_PATHS:
        source = REPO / rel
        if not source.exists():
            continue
        # Copy INTO the path's own parent, so a nested member lands where its readers expect
        # it rather than flattened to the root.
        destination = root / rel
        destination.parent.mkdir(parents=True, exist_ok=True)
        # -c asks for APFS clones; other filesystems fall back to a real copy.
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
    """`killed` when the suite notices, `survived` when it does not."""
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
#: The recovery bodies are assembled from the stored shape by hand, NOT by dumping the current
#: model: the first run's one real defect was a fixture that stamped the current
#: `schema_version` where a genuinely affected body carries the previous one, and a probe built
#: on that fixture could not see it either.
REPAIR_SHIM_PROBE = r"""
import json
from reaper.engine.policy import (
    MAX_SCORE,
    SCHEMA_VERSION,
    GateId,
    RatingSource,
    rebalance,
    recover_rating_rules,
)

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


#: Zone 2: the save boundary. Twelve validators decide what an operator is allowed to store,
#: so the dangerous direction here is the opposite of a repair shim's -- a mutant that makes a
#: validator ACCEPT what it should refuse lets a policy that protects nothing be saved, while
#: one that refuses a legal policy only blocks the operator, loudly. The probe records
#: accepted-or-rejected rather than a value, so that asymmetry is what the diff shows.
#:
#: Field keys and rating sources are drawn from the live registries rather than hardcoded: a
#: probe naming a field that later moves lanes would silently stop testing the branch it was
#: written for.
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
#: on -- the outcome, whether the gate blocked, and the sentence the panel prints.
#:
#: The direction rule here is one predicate: **does this result still hold the file?** A gate
#: holds it by protecting outright, or by blocking because it could not check (rule 93's
#: corollary -- a hold standing in for "we could not answer" is `blocked`, and it holds). So a
#: survivor that turns holds into does-not-hold has silently withdrawn a protection library-wide,
#: and one that turns does-not-hold into holds has only kept a file nobody asked to keep.
GATES_PROBE = r"""
import json
from reaper.engine.gates import (
    CuratedListGate,
    DataHorizonGate,
    Facts,
    GateConfig,
    GateId,
    MinDormancyGate,
    RatingFloorGate,
    RatingRule,
    ServerPopularityGate,
    StreamingNowGate,
    WhitelistGate,
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

def cfg(gate_id, **kw):
    return GateConfig(gate=gate_id, **kw)

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
pop = ServerPopularityGate(cfg(GateId.SERVER_POPULARITY, threshold=3, window_days=365))
# "well-over" matters as much as "at-floor": every case sitting at or below the floor left
# `count >= floor` free to become `count == floor`, which stops protecting the most-watched
# titles on the server and changes nothing a probe can see.
for label, n in (("at-floor", 3), ("well-over-floor", 10), ("one-under", 2),
                 ("one-watcher", 1), ("nobody", 0)):
    gate_case(f"popularity-{label}", pop, distinct_watchers=Known(value=n, source="x"),
              history_reach_days=Known(value=400.0, source="x"))
# A floor of 1 is the only way to reach the PROTECT arm's singular, and an Absent count is
# the only way to reach the `else 0` fallback.
solo = ServerPopularityGate(cfg(GateId.SERVER_POPULARITY, threshold=1, window_days=365))
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
dorm = MinDormancyGate(cfg(GateId.MIN_DORMANCY, threshold=1095))
for label, days in (("at-floor", 1095.0), ("one-under", 1094.0), ("well-under", 400.0),
                    ("well-over", 1500.0)):
    gate_case(f"dormancy-{label}", dorm, days_observed_unwatched=Known(value=days, source="x"))
gate_case("dormancy-unreadable", dorm,
          days_observed_unwatched=Unknown(reason="r", source="x"))
gate_case("dormancy-absent", dorm, days_observed_unwatched=Absent(source="x"))

# --- the three switch-shaped gates ---
for name, gate, field in (
    ("streaming", StreamingNowGate(cfg(GateId.STREAMING_NOW)), "is_streaming_now"),
    ("whitelist", WhitelistGate(cfg(GateId.WHITELISTED)), "is_whitelisted"),
):
    gate_case(f"{name}-true", gate, **{field: Known(value=True, source="x")})
    gate_case(f"{name}-false", gate, **{field: Known(value=False, source="x")})
    gate_case(f"{name}-unreadable", gate, **{field: Unknown(reason="r", source="x")})
curated = CuratedListGate(cfg(GateId.CURATED_LIST))
gate_case("curated-on-a-list", curated, in_curated_list=Known(value="a list", source="x"))
gate_case("curated-on-no-list", curated, in_curated_list=Absent(source="x"))
gate_case("curated-unreadable", curated, in_curated_list=Unknown(reason="r", source="x"))
gate_case("horizon-unreadable", DataHorizonGate(cfg(GateId.DATA_HORIZON)),
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


#: `ratings.py` is the layer *under* the rating bar: `RatingFloorGate` holds a file only where
#: `Rating.meets` says the bar cleared, and it can only ever consider a rating the two parsers
#: managed to interpret. So the direction here is the opposite of a validator's -- a mutant that
#: makes `meets` answer False, or makes a parser return None where a number was readable, does
#: not refuse a save, it silently withdraws the protection and hands the file to the reap list.
#: `keeps` / `lets-go` is recorded per case for exactly that reason.
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


ZONES: dict[str, Zone] = {
    "policy-repair-shims": Zone(
        module=Path("src/reaper/engine/policy.py"),
        functions=("rebalance", "recover_rating_rules"),
        tests=(
            "tests/test_policy.py::TestRebalancingAnOldPolicy",
            "tests/test_policy.py::TestRestoringALostRatingBar",
            "tests/test_profiles.py",
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
        probe=SAVE_BOUNDARY_PROBE,
    ),
    "engine-gates": Zone(
        module=Path("src/reaper/engine/gates.py"),
        functions=(
            "thaw_defers_to_owner",
            "RatingRule.threshold_text",
            "RatingRule.describe_bar",
            "RatingFloorGate.evaluate",
            "StreamingNowGate.evaluate",
            "history_shortfall",
            "lifetime_shortfall",
            "progress_is_establishable",
            "ServerPopularityGate.evaluate",
            "WhitelistGate.evaluate",
            "CuratedListGate.evaluate",
            "MinDormancyGate.evaluate",
            "DataHorizonGate.evaluate",
            "evaluate_all",
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
            # The only file that kills a deletion of either `RatingFloorGate` guard. Without
            # it the zone reported two survivors the full suite fails on, and a false
            # survivor costs a reader the trust the real ones need.
            "tests/test_policy_permutations.py",
        ),
        probe=GATES_PROBE,
    ),
    "ratings": Zone(
        module=Path("src/reaper/ratings.py"),
        functions=(
            "Rating.meets",
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
}
DEFAULT_ZONE = "policy-repair-shims"

#: The two delegating validators contribute no mutants: they hand the whole decision to the
#: `fields` registry, so there is nothing here to flip. They are named in the zone anyway,
#: because a zone claiming to be "the save boundary" and quietly omitting two of its members
#: would be the flag-shaped coverage claim rule 145 is about. Their logic is a separate zone.


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
    """Per-function counts, keyed on the functions the ZONE declares.

    Counting the mutants instead would drop a function that generated none, and a function
    missing from the report reads exactly like one whose mutants all died -- the flag-shaped
    coverage claim of rule 145, in the tool built to find it. A zero here says the run tried
    nothing at all in that function, which is a different answer from "defended" and has to
    look like one.
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
            """One thread per worker, taking every mutant.

            Round-robin rather than contiguous blocks: mutants in the same function tend to
            cost the same, so slicing by position would hand one worker every slow one.
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
        # The real tree is only ever read: mutants are written into the worker copies.
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
