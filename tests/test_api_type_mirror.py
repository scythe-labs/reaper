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
"""

from __future__ import annotations

import importlib
import pkgutil
import re
from pathlib import Path

import pytest
from pydantic import BaseModel

import reaper.api

REPO = Path(__file__).resolve().parents[1]
API_TS = REPO / "frontend" / "src" / "api.ts"

#: The modules whose Pydantic models the browser mirrors. ``api.*`` is the wire layer and wins
#: any name collision with the two engine modules, which the browser also mirrors directly:
#: ``PolicyBody``/``ProfileSettings`` exist in BOTH, and the browser's copy is the wire one.
WIRE_PACKAGE = "reaper.api."
INNER_MODULES = ("reaper.engine.policy", "reaper.engine.explanation")

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
}

#: Browser types with no server declaration to mirror, classified rather than silenced
#: (rule 103). If one of these gains a server counterpart it must move out of this list.
CLIENT_ONLY = {
    # Assembled in the browser from the response body plus the X-Total-Count and
    # X-Total-Bytes headers, so no single model describes it.
    "CandidatePage",
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
EXPECTED_INTERFACES = 87
EXPECTED_PAIRS = 84

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

    ``CandidateDetail extends Candidate`` is the only case today, and resolving it is not
    optional: Pydantic reports inherited fields on the subclass, so leaving TS unresolved
    reports every parent field as server-only and buries a real difference in the noise.
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
                bucket[value.__name__] = set(value.model_fields)
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
