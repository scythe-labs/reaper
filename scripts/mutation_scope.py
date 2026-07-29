"""Scoped mutation runner: break one function on purpose, see whether a test notices.

Not a coverage tool and not a score. It answers one question per mutant -- *if this line
silently stopped doing its job, would the suite fail?* -- and a survivor is a branch nothing
is defending. `docs/LEARNINGS.md` records what the first run found.

Run it:

    uv run python scripts/mutation_scope.py --zone policy-repair-shims
    uv run python scripts/mutation_scope.py --zone policy-save-boundary

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

**A probe case built from a test fixture inherits whatever that fixture cannot see.** The
first run mislabeled its one real defect for exactly that reason, so the recovery cases below
are built from the stored *shape* by hand rather than by dumping the current model.
"""

from __future__ import annotations

import argparse
import ast
import io
import json
import subprocess
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
    return out + statement_deletions(source, spans, len(out))


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


def run_tests(zone: Zone, timeout: float) -> str:
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
            argv, cwd=REPO, capture_output=True, text=True, timeout=timeout
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
}
DEFAULT_ZONE = "policy-repair-shims"

#: The two delegating validators contribute no mutants: they hand the whole decision to the
#: `fields` registry, so there is nothing here to flip. They are named in the zone anyway,
#: because a zone claiming to be "the save boundary" and quietly omitting two of its members
#: would be the flag-shaped coverage claim rule 145 is about. Their logic is a separate zone.


def probe(zone: Zone, timeout: float) -> dict[str, object] | str:
    try:
        proc = subprocess.run(  # noqa: S603 -- fixed argv, no caller input
            ["uv", "run", "python", "-c", zone.probe],  # noqa: S607 -- `uv` is how CLAUDE.md runs everything
            cwd=REPO,
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


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--zone", choices=sorted(ZONES), default=DEFAULT_ZONE)
    parser.add_argument("--report", type=Path, default=None)
    args = parser.parse_args()

    name, zone = args.zone, ZONES[args.zone]
    report = args.report or REPO / f"mutation-report-{name}.json"

    path = REPO / zone.module
    original = path.read_text()
    mutants = generate(original, zone)
    print(f"zone {name}: {len(mutants)} mutants across {len(zone.functions)} targets\n", flush=True)

    baseline = probe(zone, 180)
    if isinstance(baseline, str):
        raise SystemExit(f"baseline {baseline}")

    started = time.time()
    if (status := run_tests(zone, 900)) != "survived":
        raise SystemExit(f"baseline suite is not green ({status}); fix that before mutating")
    each = time.time() - started
    print(
        f"baseline green in {each:.1f}s -> about {len(mutants) * each / 60:.0f} min\n", flush=True
    )

    try:
        for i, m in enumerate(mutants, 1):
            try:
                mutated = splice(original, m)
                compile(mutated, str(path), "exec")
            except ValueError as exc:
                m.status = "skipped"
                print(f"[{i}/{len(mutants)}] SKIP  {m.label}: {exc}", flush=True)
                continue
            except SyntaxError:
                m.status = "invalid"
                print(f"[{i}/{len(mutants)}] INVAL {m.label}", flush=True)
                continue
            path.write_text(mutated)
            m.status = run_tests(zone, max(120.0, each * 6))
            if m.status == "survived":
                after = probe(zone, 180)
                if isinstance(after, str):
                    m.direction = [after]
                else:
                    m.direction = [
                        f"{k}: {json.dumps(baseline.get(k))} -> {json.dumps(v)}"
                        for k, v in after.items()
                        if baseline.get(k) != v
                    ] or ["no change on the probe corpus -- equivalent, or a case is missing"]
            print(
                f"[{i}/{len(mutants)}] "
                f"{ {'killed': 'kill ', 'survived': 'SURV '}.get(m.status, m.status) } {m.label}",
                flush=True,
            )
            for detail in m.direction:
                print(f"          {detail}", flush=True)
    finally:
        path.write_text(original)
        print("\nsource restored", flush=True)

    report.write_text(
        json.dumps(
            [
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
            indent=2,
        )
        + "\n"
    )

    counts: dict[str, int] = {}
    for m in mutants:
        counts[m.status] = counts.get(m.status, 0) + 1
    print("\n== summary ==")
    for status in sorted(counts):
        print(f"  {status}: {counts[status]}")
    print(f"\nreport: {report}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
