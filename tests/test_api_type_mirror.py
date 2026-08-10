# SPDX-License-Identifier: AGPL-3.0-or-later
"""The browser's copy of the wire types is checked against the server's declarations.

``frontend/src/api.ts`` is a hand-maintained mirror of the response models, and nothing
checked the two agreed. Rule 103 is the standing requirement -- a hardcoded list that mirrors
the model set carries a drift guard -- and this is the largest such mirror in the tree.

**What got through, and why nothing noticed.** ``MatchOut`` gained ``by`` and
``merged_rating_keys``; the TS ``Match`` carried neither, and ``merged_rating_keys`` is
load-bearing on the deletion path -- ``executor._plex_keys`` re-reads it so every listing of a
merged bind is protected together, and the panel states the count so the operator can see that
interlock's own input. The boundary is crossed by hand at two hops on some paths (an ORM row, a
service dataclass, then the wire model) and each hop is a separate edit, so nothing announces a
field that stopped at one of them.

**This is a pin, not a repair (rule 118).** That drift was fixed by hand before this landed, so
the guard is green on arrival and fails the moment someone drops a field again, rather than
proving anything about today.

**It compares field NAMES only, and that is a deliberate bound.** Types and optionality are not
checked: ``status`` is ``str | None`` on the server and a closed union of four literals in TS,
and several fields are marked ``?`` in TS while the server always sends them, because no
component reads them and the fixtures are not made to carry them. Those are intentional
everywhere they occur, so comparing them would flag eight pairs on day one and train the next
author to silence the guard. Names are apples-to-apples across the whole corpus, and a name is
what #260 lost. **Nested inline objects are compared at their top level only**: TS spells
``LeavingSoonSettings.last`` as an inline object where the server declares a whole
``LeavingSoonLastOut``, so ``last`` is compared and its members are not.

**The last class in this file checks the hop before that one**, service record to wire model,
for the routes that build the model off the record instead of naming every field. It is here
because it is the same failure at the previous hop, and the two hops are what the paragraph
above says nothing announces.
"""

from __future__ import annotations

import ast
import dataclasses
import importlib
import pkgutil
import re
from pathlib import Path
from typing import get_args

import pytest
from pydantic import BaseModel

import reaper.api
from reaper.api.backup import RestoreSummaryOut
from reaper.api.schemas import (
    CandidateLinkOut,
    LinksOut,
    ReapBreakdownOut,
    RequesterRowOut,
    SignalCountOut,
)
from reaper.api.settings import SeerrServiceOut
from reaper.services.breakdown import ReapBreakdown, SignalCount
from reaper.services.deep_links import CandidateLink, DeepLinks
from reaper.services.fairness import RequesterRow
from reaper.services.instances import ServiceInstanceSuggestion
from reaper.services.restore import RestoreSummary

REPO = Path(__file__).resolve().parents[1]
API_TS = REPO / "frontend" / "src" / "api.ts"

#: The modules whose Pydantic models the browser mirrors. ``api.*`` is the wire layer, and the
#: two engine modules the browser also mirrors directly are kept in a second bucket, so a name
#: living in both is a pairing question rather than a clash -- ``ALIAS`` is where those are
#: written down. This used to name ``PolicyBody``/``ProfileSettings`` as existing in both; the
#: wire spells them ``PolicyBodyOut`` and ``ProfileSettingsIO``, so no name is shared today.
WIRE_PACKAGE = "reaper.api."
INNER_MODULES = ("reaper.engine.policy", "reaper.engine.explanation")

#: Reconciled by hand against the tree: 125 under ``reaper.api.*`` and 15 across the two engine
#: modules. It is here because the collision assertion below is flag-shaped, and a flag cannot
#: see a member that left the walk (rule 145).
_EXPECTED_SERVER_MODELS = 140

#: Browser types whose server counterpart is spelled differently. Each is a real pair -- the
#: field sets are compared -- and the rename is the only reason a suffix rule cannot find it.
ALIAS = {
    # The wire body the policy editor posts. The server calls the request model ``PolicyIn``
    # and keeps ``engine.policy.PolicyBody`` (which carries schema_version/scorer_version and
    # no name) for the frozen internal copy, so an exact-name match would pair the wrong one.
    "PolicyBody": "PolicyIn",
    "AuthUser": "UserOut",
    "InstanceTest": "TestOut",
    "LogsPage": "LogsOut",
    "LeavingSoonResult": "LeavingSoonOut",
    "VocabField": "FieldOut",
    "GradedKeep": "GradedKeepSpec",
    "RatingRule": "RatingRuleSpec",
    # The probe's answer. The server suffixes its response models ``Out``; the browser
    # names this one for what it is rather than for which direction it travels, because the
    # editor reads it as a result and never posts it.
    "PolicyProbeResult": "PolicyProbeOut",
    # What one "Check now" on Settings -> Lists did. Same shape as the entry above: the
    # browser names it for what it is, and never posts it.
    "ListSyncResult": "ListSyncOut",
}

#: Browser types with no server declaration to mirror, classified rather than silenced
#: (rule 103). If one of these gains a server counterpart it must move out of this list.
CLIENT_ONLY = {
    # The query the browser sends as URL parameters, not a body any model validates.
    "CandidateQuery",
    # A UI-side subset of ScanStatus (phase/done/total/detail) that several components take
    # as a prop; the server has no model of the subset.
    "Progress",
}

#: Reconciled by hand against the tree (rule 145). ``grep -c '^export interface'`` on api.ts is
#: the first number; a walk that silently stopped collecting would drop below it while every
#: name-comparison below still passed, because a type absent from the walk is absent from both
#: halves of the comparison.
# Both +2, for the policy probe's request and its answer: `SignalProbe` pairs with
# `SignalProbeIn` on the suffix rule, and `PolicyProbeResult` needed the ALIAS entry above.
# The third new name, `PolicyProbe`, is a type alias rather than an interface and is counted
# by neither walk -- which is the reason these two numbers are reconciled against the tree
# separately instead of one being derived from the other.
# Both +1 again for the desktop build's Settings group: `DesktopSettings` pairs with
# `DesktopSettingsOut` on the suffix rule.
# Both +1 again for Settings -> Lists (#475): `ProtectionList` pairs with `ProtectionListOut`
# on the same suffix rule.
# Both +2 again for the rest of that screen: `ListConfig` (the list DEFINITIONS the operator
# edits) pairs with `ListConfigOut` on the suffix rule, and `ListSyncResult` with `ListSyncOut`
# through the ALIAS entry above. The third new name, `ListConfigBody`, is a type alias rather
# than an interface and is counted by neither walk -- the same case the `PolicyProbe` note
# above describes, and the reason these two numbers are reconciled against the tree separately.
# Both +1 again for W8-5's split of the connection test: `InstanceProbe` pairs with
# `InstanceProbeOut` on the suffix rule, and needs no ALIAS entry. `InstanceTest` keeps its
# own ALIAS to `TestOut`, and both sides narrow to the same three fields. The `TestVerdict`
# alias it replaced was an `export type` and was counted by neither walk.
# Both +1 again for W8-2's steps window: `RunSteps` pairs with `RunStepsOut` on the suffix
# rule. It is its own route rather than query parameters on the run detail, so it is its own
# type rather than a widened `Run`.
# PAIRS alone +1 for W8-1's candidates envelope: `CandidatePage` was the browser's own
# assembly of a bare list plus four response headers and sat in CLIENT_ONLY above. It is a
# served model now, `CandidatePageOut`, pairing on the suffix rule, so it left that set
# without being a new interface.
# Both +1 again for the second half of W8-1: `GroupRollup` pairs with `GroupRollupOut` on the
# suffix rule. It is the show-level rollup that used to be four fields on every season row.
EXPECTED_INTERFACES = 94
EXPECTED_PAIRS = 92

_BLOCK_COMMENT = re.compile(r"/\*.*?\*/", re.DOTALL)
_LINE_COMMENT = re.compile(r"//[^\n]*")
_INTERFACE = re.compile(r"^export interface (\w+)(?:\s+extends\s+([\w,\s]+?))?\s*\{", re.MULTILINE)
_MEMBER = re.compile(r"^\s*(\w+)\s*\??\s*:")

#: Everything from here down is the client, not the wire types: inline object literals that
#: are request shapes, generics, and URL strings that would defeat the comment stripper.
CLIENT_MARKER = "\nexport const api"


def _declarations(source: str) -> dict[str, tuple[list[str], list[str]]]:
    """Every ``export interface`` in ``source`` as ``name -> (own fields, base names)``.

    Brace-depth aware on purpose (rule 147). Anchoring on the delimiter that one spelling
    happens to put there -- a two-space indent, a quote after ``:`` -- reads the plain
    declarations and silently skips the messy ones, and this file has four messy spellings:
    a member's inline nested object, a union of object literals, a trailing ``//`` after a
    field, and doc comments between every pair of fields. A member ends at the first ``;`` at
    depth zero, so a nested object contributes its own name and none of its members.
    """
    cut = source.find(CLIENT_MARKER)
    text = source if cut == -1 else source[:cut]
    text = _LINE_COMMENT.sub("", _BLOCK_COMMENT.sub("", text))

    found: dict[str, tuple[list[str], list[str]]] = {}
    for head in _INTERFACE.finditer(text):
        bases = [b.strip() for b in (head.group(2) or "").split(",") if b.strip()]
        depth, cursor = 1, head.end()
        while cursor < len(text) and depth:
            if text[cursor] == "{":
                depth += 1
            elif text[cursor] == "}":
                depth -= 1
            cursor += 1
        fields: list[str] = []
        buffer, inner = "", 0
        for char in text[head.end() : cursor - 1]:
            if char == "{":
                inner += 1
            elif char == "}":
                inner -= 1
            if char == ";" and inner == 0:
                member = _MEMBER.match(buffer)
                if member:
                    fields.append(member.group(1))
                buffer = ""
            else:
                buffer += char
        found[head.group(1)] = (fields, bases)
    return found


def _with_inherited(name: str, declarations: dict[str, tuple[list[str], list[str]]]) -> set[str]:
    """One interface's fields including everything it extends.

    ``CandidateDetail extends Candidate`` and ``InstanceProbe extends InstanceTest`` are the
    two cases today, and resolving them is not optional: Pydantic reports inherited fields on
    the subclass, so leaving TS unresolved reports every parent field as server-only and buries
    a real difference in the noise.
    """
    fields, bases = declarations[name]
    resolved = set(fields)
    for base in bases:
        if base in declarations:
            resolved |= _with_inherited(base, declarations)
    return resolved


def _server_models() -> tuple[dict[str, set[str]], dict[str, set[str]]]:
    """The wire models and the two engine modules the browser also mirrors, kept apart.

    ``model_fields`` rather than the OpenAPI document: it already includes inherited fields,
    and it covers models no route publishes -- ``PolicyIn`` is nested inside ``PolicyOut``
    rather than returned, and the browser mirrors it as its own type.
    """
    wire: dict[str, set[str]] = {}
    inner: dict[str, set[str]] = {}
    homes: dict[tuple[str, str], type[BaseModel]] = {}
    collisions: list[str] = []
    names = [f"{WIRE_PACKAGE}{m.name}" for m in pkgutil.iter_modules(reaper.api.__path__)]
    for module_name in [*names, *INNER_MODULES]:
        module = importlib.import_module(module_name)
        bucket = wire if module_name.startswith(WIRE_PACKAGE) else inner
        for value in vars(module).values():
            if (
                isinstance(value, type)
                and issubclass(value, BaseModel)
                and value is not BaseModel
                and value.__module__ == module_name
            ):
                # Keyed per BUCKET, because only a same-bucket collision masks: ``wire``
                # and ``inner`` are separate dicts, so an engine model sharing a name with a
                # wire model overwrites nothing. Keying on the name alone would forbid the
                # engine/wire pairing ``ALIAS`` exists to describe.
                seen = "wire" if bucket is wire else "inner"
                prior = homes.get((seen, value.__name__))
                if prior is not None and prior is not value:
                    collisions.append(f"{value.__name__}: {prior.__module__} and {module_name}")
                homes[(seen, value.__name__)] = value
                bucket[value.__name__] = set(value.model_fields)
    assert len(wire) + len(inner) == _EXPECTED_SERVER_MODELS, (
        f"expected {_EXPECTED_SERVER_MODELS} models under {WIRE_PACKAGE}* plus "
        f"{', '.join(INNER_MODULES)}, walked {len(wire)} + {len(inner)}. A flag-shaped "
        "assertion cannot tell a model that COMPLIES from one that dropped out of the walk "
        "(rule 145), and this walk is narrower than it looks: pkgutil.iter_modules does not "
        "recurse, so an api module moved under a subpackage leaves silently. Three BaseModels "
        "sit outside it deliberately -- config.InstanceSeed and config.RuntimeSafety are not "
        "wire types, and main.HealthResponse IS a published component whose name could collide "
        "with an api one without this walk seeing either."
    )
    assert collisions == [], (
        "two different models share one class name, so this walk keeps only the one imported "
        "last and every comparison below runs against it alone. FastAPI names the published "
        "component off the class too, so both operations get module-qualified component names "
        "the moment either side gains a field, including the operation nobody edited. Declare "
        "it once, or give one a distinct name:\n  " + "\n  ".join(sorted(collisions))
    )
    return wire, inner


def _pair(name: str, wire: dict[str, set[str]], inner: dict[str, set[str]]) -> str | None:
    """The server model a browser type mirrors, or ``None`` if it mirrors nothing."""
    for table in (wire, inner):
        for candidate in (ALIAS.get(name), f"{name}Out", name, f"{name}IO", f"{name}In"):
            if candidate and candidate in table:
                return candidate
    return None


@pytest.fixture(scope="module")
def browser_types() -> dict[str, set[str]]:
    declarations = _declarations(API_TS.read_text(encoding="utf-8"))
    return {name: _with_inherited(name, declarations) for name in declarations}


@pytest.fixture(scope="module")
def server_tables() -> tuple[dict[str, set[str]], dict[str, set[str]]]:
    return _server_models()


class TestTheParserReadsEveryFormThisTreeUses:
    """Rule 147: a guard that scans source text is bounded by the syntax it can parse.

    The spellings accepted are written out here and driven, rather than asserted about in a
    comment. Each block below is a form that really occurs in ``api.ts``; the two that would
    defeat a line-oriented matcher are the six-space members of a union of object literals
    (not an interface, so deliberately not collected) and a member whose type is an inline
    object, whose OWN members must not be mistaken for the outer type's.
    """

    SAMPLE = """
export type Verdict = "condemn" | "protect" | "abstain";

export interface Plain {
  id: number;
  title: string;
}

export interface Fancy extends Plain {
  /** A doc comment between fields, which is the dominant form in this file.
   *  It runs to several lines and mentions { braces } and ; semicolons. */
  status: "matched" | "unmatched" | null;
  // A bare line comment inside the body.
  optional_thing?: string | null;
  state: string; // verified | failed | skipped
  counts: Record<string, number>;
  rows: Plain[];
  nested: {
    inner_one: string;
    inner_two: number;
  } | null;
  wrapped:
    | "a"
    | "b";
}

export type Union =
  | {
      kind: "boolean";
      weight: number;
    }
  | {
      kind: "graded";
      floor: number;
    };

export const api = {
  save: (body: Plain) => post<Plain>("/api/thing", body),
};

export interface AfterTheClient {
  never_collected: string;
}
"""

    def test_it_collects_the_forms_it_claims_and_no_others(self) -> None:
        found = _declarations(self.SAMPLE)

        assert set(found) == {"Plain", "Fancy"}, (
            "an `export type` alias is not an interface and is not collected; nothing below "
            "`export const api` is either"
        )
        assert found["Plain"][0] == ["id", "title"]
        assert found["Fancy"][0] == [
            "status",
            "optional_thing",
            "state",
            "counts",
            "rows",
            "nested",
            "wrapped",
        ], "a nested object's own members must not be read as the outer type's"
        assert found["Fancy"][1] == ["Plain"]

    def test_inheritance_is_resolved(self) -> None:
        found = _declarations(self.SAMPLE)
        assert _with_inherited("Fancy", found) == {
            "id",
            "title",
            "status",
            "optional_thing",
            "state",
            "counts",
            "rows",
            "nested",
            "wrapped",
        }

    def test_a_field_hidden_in_a_comment_is_not_collected(self) -> None:
        """The stripper runs before the walk, so prose naming a field cannot invent one."""
        found = _declarations(
            "export interface X {\n  /** talks about phantom: string; here */\n  real: number;\n}"
        )
        assert found["X"][0] == ["real"]


class TestTheWalkCoversThePopulationItClaims:
    def test_it_finds_every_exported_interface_in_the_file(
        self, browser_types: dict[str, set[str]]
    ) -> None:
        """Rule 145. The declarations are counted straight out of the text as well, so a walk
        that stopped collecting fails here rather than passing every comparison below on a
        population that quietly shrank."""
        declared = len(
            [
                line
                for line in API_TS.read_text(encoding="utf-8").splitlines()
                if line.startswith("export interface ")
            ]
        )
        assert declared == EXPECTED_INTERFACES, (
            f"frontend/src/api.ts now declares {declared} interfaces, not {EXPECTED_INTERFACES}. "
            "Reconcile the number here by hand, then update EXPECTED_INTERFACES."
        )
        assert len(browser_types) == EXPECTED_INTERFACES

    def test_no_collected_type_is_empty(self, browser_types: dict[str, set[str]]) -> None:
        """A brace-matching bug reads a type as having no members, and an empty set matches
        nothing while flagging every server field -- loud. An empty set on BOTH sides would be
        silent, which is what this catches."""
        empty = sorted(name for name, fields in browser_types.items() if not fields)
        assert empty == [], f"parsed with no fields, so the parser is broken on them: {empty}"


class TestEveryBrowserTypeIsPairedOrClassified:
    def test_the_unpaired_set_is_exactly_the_classified_one(
        self,
        browser_types: dict[str, set[str]],
        server_tables: tuple[dict[str, set[str]], dict[str, set[str]]],
    ) -> None:
        """Rule 103's last sentence: a member the guard flags is classified in writing, not
        silenced. A new browser type with no server model has to be argued into CLIENT_ONLY."""
        wire, inner = server_tables
        unpaired = {name for name in browser_types if _pair(name, wire, inner) is None}

        assert unpaired == CLIENT_ONLY, (
            "these browser types mirror no server model. If that is deliberate, add each to "
            "CLIENT_ONLY in this file with the reason; if it is a rename, add it to ALIAS:\n  "
            + "\n  ".join(sorted(unpaired ^ CLIENT_ONLY))
        )

    def test_the_pair_count_holds(
        self,
        browser_types: dict[str, set[str]],
        server_tables: tuple[dict[str, set[str]], dict[str, set[str]]],
    ) -> None:
        wire, inner = server_tables
        paired = [n for n in browser_types if _pair(n, wire, inner) is not None]
        assert len(paired) == EXPECTED_PAIRS


class TestTheTwoCopiesAgree:
    def test_no_paired_type_has_lost_or_gained_a_field(
        self,
        browser_types: dict[str, set[str]],
        server_tables: tuple[dict[str, set[str]], dict[str, set[str]]],
    ) -> None:
        """The guard itself. Rule 144: the message names the file to edit, because a comment
        asking the next author to remember does nothing."""
        wire, inner = server_tables
        drifted: list[str] = []
        for name in sorted(browser_types):
            counterpart = _pair(name, wire, inner)
            if counterpart is None:
                continue
            fields = wire.get(counterpart, inner.get(counterpart, set()))
            server_only = sorted(fields - browser_types[name])
            browser_only = sorted(browser_types[name] - fields)
            if server_only or browser_only:
                drifted.append(
                    f"{name} <-> {counterpart}:"
                    + (
                        f" the server sends {server_only} and the browser has no field for it;"
                        if server_only
                        else ""
                    )
                    + (
                        f" the browser declares {browser_only} and no server model sends it;"
                        if browser_only
                        else ""
                    )
                )

        assert drifted == [], (
            "frontend/src/api.ts and the response models disagree. The browser's copy is "
            "hand-maintained, so it does not follow a field added or removed on the server "
            "(#260 lost merged_rating_keys this way, which the executor re-reads to protect "
            "every listing of a merged bind). Edit frontend/src/api.ts to match, or record a "
            "deliberate difference in this file:\n  " + "\n  ".join(drifted)
        )


class TestEverySimulatorRefusalReachesThePanel:
    """The refusal vocabulary crosses two boundaries, and only one of them is type-checked.

    ``api.schemas.SimStale`` is what the route sends. The browser mirrors it as a string
    union in ``api.ts``, and ``PolicySimulator.tsx`` gives each member a heading through a
    ``Record<SimStale, string>`` -- which ``tsc`` already keeps complete, so a member missing
    a heading cannot compile. What nothing checks is the step before it: a member added on
    the server and never added to the union. TypeScript is perfectly happy with a union that
    is missing a value it will be handed at runtime, and the panel would then fall back to
    the general heading for a refusal that has its own remedy.

    The class above compares field NAMES between paired models and cannot see this: the two
    sides agree that ``stale_kind`` exists, and disagree about what may be in it.

    Bounded per rule 147: this reads the union however it is spaced, but only while it is
    spelled as quoted literals in one ``export type SimStale = ...`` declaration. The count
    is pinned against the enum so a declaration this matcher stops finding fails loudly
    rather than silently matching nothing.
    """

    UNION = re.compile(r"export type SimStale\s*=\s*([^;]+);")

    def _declared(self) -> set[str]:
        found = self.UNION.search(API_TS.read_text(encoding="utf-8"))
        assert found is not None, (
            "frontend/src/api.ts no longer declares `export type SimStale` as one statement, "
            "so this guard is reading nothing. Re-point it at the new spelling."
        )
        return set(re.findall(r'"([^"]+)"', found.group(1)))

    def test_the_browser_knows_every_refusal_the_server_can_send(self) -> None:
        from reaper.api.schemas import SimStale

        server = {member.value for member in SimStale}
        assert self._declared() == server, (
            "api.schemas.SimStale and frontend/src/api.ts's SimStale union disagree. A "
            "refusal the browser does not know falls back to the general 'needs a fresh "
            "scan' heading, which names the wrong remedy. Add it to the union, and "
            "PolicySimulator.tsx's STALE_HEADINGS (tsc requires it once the union moves)."
        )

    def test_the_panel_gives_each_one_a_heading(self) -> None:
        """The `Record<SimStale, string>` is the real guard; this pins that it still exists.

        A future author swapping the record for a lookup with a default would take the
        compile-time completeness away without anything failing, which is the shape rule 118
        is about: the guard would be gone and its proof would still read green.
        """
        panel = (REPO / "frontend" / "src" / "components" / "PolicySimulator.tsx").read_text(
            encoding="utf-8"
        )
        assert "const STALE_HEADINGS: Record<SimStale, string>" in panel, (
            "PolicySimulator.tsx no longer types its refusal headings as "
            "Record<SimStale, string>, so tsc has stopped requiring one per refusal."
        )
        for value in self._declared():
            assert f"{value}:" in panel, f"{value} has no heading in STALE_HEADINGS"


class TestTheCentralVocabularyIsOneDeclaration:
    """``Verdict`` and ``Override`` are the words the whole app is written in, and until now
    they were declared in TypeScript and passed around Python as a bare ``str``.

    Neither is covered by the field comparison above. That walk pairs ``export interface``
    declarations and compares field NAMES; these two are ``export type`` unions, so both sides
    agree that ``verdict`` exists and neither notices that they disagree about what may be in
    it. That is the same hole ``SimStale`` above is written for, at the two names it would cost
    the most: a verdict the browser does not know renders with no ``.score-*`` class at all,
    because ``ReviewQueue.tsx`` interpolates the value straight into a class name.

    Bounded per rule 147: read however the union is spaced, but only while it is one
    ``export type NAME = ...;`` statement of quoted literals. The member count is pinned as
    well as the membership, so a declaration this stops finding fails loudly rather than
    quietly matching nothing (rule 145).
    """

    def _declared(self, name: str) -> set[str]:
        found = re.search(rf"export type {name}\s*=\s*([^;]+);", API_TS.read_text(encoding="utf-8"))
        assert found is not None, (
            f"frontend/src/api.ts no longer declares `export type {name}` as one statement, so "
            "this guard is reading nothing. Re-point it at the new spelling."
        )
        return set(re.findall(r'"([^"]+)"', found.group(1)))

    def test_the_browser_knows_every_verdict_the_engine_decides(self) -> None:
        from reaper.engine.verdict import Verdict

        server = set(get_args(Verdict))
        assert self._declared("Verdict") == server, (
            "engine.verdict.Verdict and frontend/src/api.ts's Verdict union disagree. The "
            "queue interpolates this value into a class name (ReviewQueue.tsx's "
            "`score-${...}` and `strip-${...}`), so a verdict the browser does not know "
            "renders unstyled rather than failing."
        )
        assert len(server) == 3

    def test_the_browser_knows_every_override_the_owner_can_set(self) -> None:
        from reaper.engine.verdict import Override

        server = set(get_args(Override))
        assert self._declared("Override") == server, (
            "engine.verdict.Override and frontend/src/api.ts's Override union disagree. "
            "api.schemas.OverrideIn.decision validates against the Python side, so a member "
            "only the browser knows is refused at the route with a 422."
        )
        assert len(server) == 2

    def test_the_request_model_reads_the_declaration_rather_than_restating_it(self) -> None:
        """Rule 131. ``OverrideIn.decision`` used to spell the pair itself, which is a second
        copy of the vocabulary sitting one import away from the first."""
        from reaper.api.schemas import OverrideIn
        from reaper.engine.verdict import Override

        assert set(get_args(OverrideIn.model_fields["decision"].annotation)) == set(
            get_args(Override)
        )


class TestEveryGateIdHasOperatorCopy:
    """A gate id the browser has no copy for is printed at the operator as a slug.

    ``GATE_META`` (``frontend/src/components/policyMeta.ts``) is the browser's one
    declaration of what each protection is called. The policy simulator's "Why titles were
    spared" list reads it by id, and until #551 an id it lacked fell through to a
    ``titleCase`` of the slug -- so "Season Progression" and "Custom", both of which fire on
    ordinary scans, were the reasons shown beside a count in the panel an operator reads
    while deciding what to delete (rule 21).

    ``tsc`` now keeps that map complete against a ``GateId`` union declared beside it, which
    is the real guard: a gate added to the engine with no copy cannot compile. **This pins
    the two things the compiler cannot see** -- that the union still says what the enum
    says, and that the ``satisfies`` clause enforcing completeness is still there. Both are
    rule 118's shape: without them the guard could be deleted and its proof stay green.

    **The one sibling copy, named here rather than guarded** (rule 144): ``api/review.py``'s
    ``_kept_phrase`` turns the same ids into the review queue's chip. It is deliberately not
    covered, because its own fallback is already a sentence -- "a protection applies" -- so a
    gate arriving without a branch there reads vaguely, never as a slug. Reword that fallback
    into anything id-shaped and it needs a guard of its own.

    Bounded per rule 147: the union is read however it is spaced and wrapped, but only while
    it is spelled as quoted literals in one ``export type GateId = ...;`` statement. A
    declaration this matcher stops finding asserts rather than matching nothing, and the set
    comparison against the enum is the pin -- a partially-read union fails it.
    """

    POLICY_META_TS = REPO / "frontend" / "src" / "components" / "policyMeta.ts"
    UNION = re.compile(r"export type GateId\s*=\s*([^;]+);")

    def _source(self) -> str:
        return self.POLICY_META_TS.read_text(encoding="utf-8")

    def _declared(self) -> set[str]:
        found = self.UNION.search(self._source())
        assert found is not None, (
            "frontend/src/components/policyMeta.ts no longer declares `export type GateId` "
            "as one statement, so this guard is reading nothing. Re-point it at the new "
            "spelling."
        )
        return set(re.findall(r'"([^"]+)"', found.group(1)))

    def test_the_browser_names_every_gate_the_engine_can_emit(self) -> None:
        from reaper.engine.gates import GateId

        engine = {member.value for member in GateId}
        assert self._declared() == engine, (
            "engine.gates.GateId and policyMeta.ts's GateId union disagree. A gate the "
            "browser does not know is printed as its raw id in the policy simulator's "
            '"Why titles were spared" list (rule 21). Add it to the union, and to '
            "GATE_META with a plain-language label (tsc requires it once the union moves)."
        )

    def test_the_map_still_has_to_cover_the_union(self) -> None:
        """The ``satisfies`` clause is what makes a missing label a build failure.

        A future author swapping it for a plain annotation would take that away with
        nothing failing. The hand-spare id is generated from the server's own constant
        rather than typed here (rule 144): the browser keys copy on what ``api/simulate.py``
        tallies under, and the two spellings must be one fact.
        """
        from reaper.api.simulate import HAND_SPARE_TALLY_ID

        source = self._source()
        clause = f'satisfies Record<GateId | "{HAND_SPARE_TALLY_ID}", GateMeta>'
        assert clause in source, (
            f"policyMeta.ts no longer closes GATE_META with `{clause}`, so tsc has stopped "
            "requiring a label for every gate id and for the hand-spare tally."
        )
        for gate in self._declared():
            assert f"\n  {gate}: {{" in source, f"{gate} has no GATE_META entry"

    def _marked_retired(self) -> set[str]:
        """Every id ``GATE_META`` marks ``retired``.

        Comments are stripped first, and each entry is read brace-depth aware, so
        ``server_popularity``'s nested ``window`` object is part of its entry rather than an
        entry of its own (rule 147). A file this stops finding entries in fails the set
        comparison below rather than matching nothing.
        """
        text = _LINE_COMMENT.sub("", _BLOCK_COMMENT.sub("", self._source()))
        start = text.find("export const GATE_META")
        assert start != -1, "policyMeta.ts no longer declares `export const GATE_META`."
        body, entry = text[text.index("{", start) + 1 :], re.compile(r"(\w+)\s*:\s*\{")
        found: set[str] = set()
        cursor = 0
        while (head := entry.search(body, cursor)) is not None:
            depth, end = 1, head.end()
            while end < len(body) and depth:
                depth += {"{": 1, "}": -1}.get(body[end], 0)
                end += 1
            if re.search(r"\bretired\s*:\s*true\b", body[head.end() : end - 1]):
                found.add(head.group(1))
            cursor = end
        return found

    def test_the_browser_marks_exactly_the_ids_no_policy_row_can_carry(self) -> None:
        """``retired`` is the browser's copy of ``POLICY_AUTHORABLE_GATES``, inverted.

        It stopped being decoration in #627. ``PolicyEditor``'s protection switch reads it to
        decide that turning a row OFF removes the row, because a policy carrying one of these
        ids is refused by the save boundary in either position -- so the two sets drifting
        breaks the page in whichever direction they drift: a live protection whose switch
        deletes its own row, or a leftover whose switch writes a body that cannot be saved.

        ``hand_spare`` is excluded because it is not a gate at all: ``api/simulate.py`` tallies
        hand spares under it and no policy body can carry it.
        """
        from reaper.api.simulate import HAND_SPARE_TALLY_ID
        from reaper.engine.gates import POLICY_AUTHORABLE_GATES, GateId

        unauthorable = {member.value for member in GateId} - {
            gate.value for gate in POLICY_AUTHORABLE_GATES
        }

        assert self._marked_retired() - {HAND_SPARE_TALLY_ID} == unauthorable, (
            "policyMeta.ts's `retired` flags and engine.gates.POLICY_AUTHORABLE_GATES "
            "disagree. Marking an authorable gate retired makes its switch delete the row "
            "instead of turning the protection off; leaving an unauthorable one unmarked "
            "puts a body the save boundary refuses behind the Save button."
        )


#: The wire models built by ``model_validate(record, from_attributes=True)`` off a service
#: record rather than field by field (W5-4), each paired with the record it reads. Seven
#: pairs over six call sites: ``SignalCountOut`` and ``CandidateLinkOut`` are validated
#: inside their parent, and ``SeerrServiceOut`` is built at two routes.
COLLAPSED_PAIRS = (
    (ReapBreakdownOut, ReapBreakdown),
    (SignalCountOut, SignalCount),
    (RequesterRowOut, RequesterRow),
    (LinksOut, DeepLinks),
    (CandidateLinkOut, CandidateLink),
    (RestoreSummaryOut, RestoreSummary),
    (SeerrServiceOut, ServiceInstanceSuggestion),
)


#: The call sites, reconciled by hand against the pair table above. Seven pairs over six
#: sites: two models validate inside their parent, and ``SeerrServiceOut`` builds at two routes.
_COLLAPSE_SITES = [
    "src/reaper/api/backup.py:235",
    "src/reaper/api/breakdown.py:28",
    "src/reaper/api/fairness.py:162",
    "src/reaper/api/review.py:1300",
    "src/reaper/api/settings.py:584",
    "src/reaper/api/settings.py:655",
]


class TestAWireModelReadsOnlyFieldsItsRecordCarries:
    """The other half of this file's mirror: the hop from a service record to the wire model,
    where a field list used to be transcribed by hand at the route.

    ``from_attributes`` selects by the WIRE model's field list, so a wire field the record
    does not carry fails at request time when it is required, and is filled from its own
    default when it is not -- which is the silent half. The hand-written constructor raised
    ``AttributeError`` for both. This restores the loud answer, before a request.
    """

    @pytest.mark.parametrize(
        "wire,record", COLLAPSED_PAIRS, ids=[w.__name__ for w, _ in COLLAPSED_PAIRS]
    )
    def test_the_record_carries_every_field_the_wire_model_declares(
        self, wire: type[BaseModel], record: type
    ) -> None:
        carried = {f.name for f in dataclasses.fields(record)}
        assert set(wire.model_fields) <= carried, (
            f"{wire.__name__} declares {sorted(set(wire.model_fields) - carried)}, which "
            f"{record.__module__}.{record.__name__} does not carry. The route builds it "
            "with model_validate(..., from_attributes=True), so a defaulted field would be "
            "served as its default rather than failing."
        )

    def test_every_collapsed_site_is_in_the_table_above(self) -> None:
        """A table nothing reconciles cannot see a site that never joined it (rule 145).

        It walks the AST rather than the text, which is what makes it immune to the two
        things a matcher of this shape usually misses (rule 147): the call spelled over
        several lines, and a docstring that names the keyword without calling anything.

        **One spelling it still cannot see**, stated rather than implied: a model setting
        ``model_config = ConfigDict(from_attributes=True)`` and then calling a bare
        ``model_validate`` is the same collapse. No model in the tree does that, and a site
        written that way is added to the pair table by hand.

        The assertion is the site LIST, not its length, so swapping one collapse for another
        cannot hold the number still while ``COLLAPSED_PAIRS`` goes stale.
        """
        sites = []
        for path in sorted((REPO / "src" / "reaper" / "api").rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                for keyword in node.keywords:
                    value = keyword.value
                    if keyword.arg == "from_attributes" and getattr(value, "value", None) is True:
                        sites.append(f"{path.relative_to(REPO)}:{node.lineno}")

        assert sorted(sites) == _COLLAPSE_SITES, (
            f"the sites building a wire model off a service record are now {sorted(sites)}. "
            "Add or remove its pair in COLLAPSED_PAIRS above, then move this list."
        )
