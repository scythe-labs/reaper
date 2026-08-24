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

**It compares field names and field types. Optionality it does not compare, and that is now a
measurement rather than a bound.** The server's ``required`` list describes what a REQUEST may
leave out; every model here is a response, and a response model with a default still serializes
its field, so 210 members read "optional" on the server while the route sends every one. The
browser's ``?`` is a third fact again: several fields carry it because no component reads them
and the fixtures are not made to carry them. Nothing true is on both sides of that comparison,
so the ``?`` is dropped on both and the type beside it is what gets checked.

Names alone was the bound until W4.2, on the argument that comparing types would flag eight
pairs on day one and train the next author to silence the guard. Measured, it flags 42 across
749 members in four classes, three of which are deliberate and named in ``NARROWED`` and
``WIDENED`` below, and one of which was a live inaccuracy: three members typed non-null against
a server that sends ``null``. **Nested inline objects are still compared at their top level
only**: TS spells ``LeavingSoonSettings.last`` as an inline object where the server declares a
whole ``LeavingSoonLastOut``, so ``last`` is compared and its members are not.

**The last class in this file checks the hop before that one**, service record to wire model,
for the routes that build the model off the record instead of naming every field. It is here
because it is the same failure at the previous hop, and the two hops are what the paragraph
above says nothing announces.
"""

from __future__ import annotations

import ast
import dataclasses
import datetime
import decimal
import enum
import importlib
import pkgutil
import re
import types
import uuid
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Literal, Union, get_args, get_origin

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

#: Reconciled by hand against the tree: 128 under ``reaper.api.*`` and 17 across the two engine
#: modules (+1 for ``RewatchOddsOut``, #554 stage 2, mirrored in the browser as ``RewatchOdds``;
#: +2 more for the same stage's ``RewatchOddsFitOut``/``RewatchOddsBlockOut``, the Policy page's
#: ladder-and-echo payload, mirrored as ``RewatchOddsFit``/``RewatchOddsBlock``; +1 more for
#: #868 phase 5's ``DiscordTestOut``, split off ``TestOut`` for the Discord webhook test's
#: typed reason; -1 for ``NotificationLanguageIn``, gone with the route it bodied -- the
#: language is one setting now and rides ``GeneralSettingsIn``). It is here because the
#: collision assertion below is flag-shaped, and a flag cannot see a member that left the walk
#: (rule 145).
_EXPECTED_SERVER_MODELS = 145

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
    # One item of a 422 validation list's `code`/`params`/`msg` triple (phase 8b): the
    # browser's own name for `api.errors.validation_error_items`'s wire shape, which is a
    # list of dicts the server builds inline rather than a model with a name to pair against.
    "ApiErrorItem",
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
# Both +3 again for #554 stage 2's frontend step: `RewatchOdds` pairs with `RewatchOddsOut`,
# `RewatchOddsFit` with `RewatchOddsFitOut`, and `RewatchOddsBlock` with `RewatchOddsBlockOut`,
# all three on the plain suffix rule with no ALIAS entry needed.
# Both +1 again for #868 phase 5: `DiscordTest` pairs with the new `DiscordTestOut` on the
# suffix rule, split off `TestOut` because the Discord webhook test now sends a typed reason
# where the *arr/Seerr connection test still sends free English (docs/history/I18N_PLAN.md §5).
# INTERFACES alone +1 for phase 8b: `ApiErrorItem` is new and sits in CLIENT_ONLY above --
# the 422 list it describes is a plain list of dicts server-side, not a named model to pair.
# INTERFACES alone -1 for the i18n legacy-layer cleanup: `Progress` had no reader anywhere in
# the tree and no server model to sit in CLIENT_ONLY for, so it was deleted rather than kept.
EXPECTED_INTERFACES = 99
EXPECTED_PAIRS = 97

_BLOCK_COMMENT = re.compile(r"/\*.*?\*/", re.DOTALL)
_LINE_COMMENT = re.compile(r"//[^\n]*")
_INTERFACE = re.compile(r"^export interface (\w+)(?:\s+extends\s+([\w,\s]+?))?\s*\{", re.MULTILINE)
_MEMBER = re.compile(r"^\s*(\w+)\s*\??\s*:\s*(.*)$", re.DOTALL)
_ALIAS_HEAD = re.compile(r"^export type (\w+)\s*=\s*", re.MULTILINE)

#: Everything from here down is the client, not the wire types: inline object literals that
#: are request shapes, generics, and URL strings that would defeat the comment stripper.
CLIENT_MARKER = "\nexport const api"


def _strip_comments(source: str) -> str:
    """The wire types with the client and every comment gone."""
    cut = source.find(CLIENT_MARKER)
    text = source if cut == -1 else source[:cut]
    return _LINE_COMMENT.sub("", _BLOCK_COMMENT.sub("", text))


def _declarations(source: str) -> dict[str, tuple[dict[str, str], list[str]]]:
    """Every ``export interface`` in ``source`` as ``name -> (own members, base names)``.

    A member maps to the text of its type, whitespace collapsed, so the same walk answers both
    the name comparison and the type one. The ``?`` is dropped rather than recorded: what it
    means here is not what it means on the server, so nothing below compares optionality, and
    ``TestTheTwoCopiesAgreeOnTypes`` says why.

    Brace-depth aware on purpose (rule 147). Anchoring on the delimiter that one spelling
    happens to put there -- a two-space indent, a quote after ``:`` -- reads the plain
    declarations and silently skips the messy ones, and this file has four messy spellings:
    a member's inline nested object, a union of object literals, a trailing ``//`` after a
    field, and doc comments between every pair of fields. A member ends at the first ``;`` at
    depth zero, so a nested object contributes its own name and none of its members.
    """
    text = _strip_comments(source)

    found: dict[str, tuple[dict[str, str], list[str]]] = {}
    for head in _INTERFACE.finditer(text):
        bases = [b.strip() for b in (head.group(2) or "").split(",") if b.strip()]
        depth, cursor = 1, head.end()
        while cursor < len(text) and depth:
            if text[cursor] == "{":
                depth += 1
            elif text[cursor] == "}":
                depth -= 1
            cursor += 1
        fields: dict[str, str] = {}
        buffer, inner = "", 0
        for char in text[head.end() : cursor - 1]:
            if char == "{":
                inner += 1
            elif char == "}":
                inner -= 1
            if char == ";" and inner == 0:
                member = _MEMBER.match(buffer)
                if member:
                    fields[member.group(1)] = " ".join(member.group(2).split())
                buffer = ""
            else:
                buffer += char
        found[head.group(1)] = (fields, bases)
    return found


def _type_aliases(source: str) -> dict[str, str]:
    """Every ``export type NAME = ...;`` in ``source``, as its right-hand side.

    The 17 of them are the browser's enums, and the interface walk above is blind to all of
    them -- they are not interfaces, and a member typed ``Verdict`` reads as one opaque word.
    Expanding them is what lets a member's type be compared against the server's literals
    rather than against a name the server never heard.

    Depth-aware for the same reason the interface walk is (rule 147): two of the seventeen are
    object literals whose own members end in ``;``, so an alias read to its first semicolon
    stops in the middle of ``ListConfigBody`` and of ``CustomCondemn``'s first arm and reports
    a truncated type as the whole one.
    """
    text = _strip_comments(source)
    found: dict[str, str] = {}
    for head in _ALIAS_HEAD.finditer(text):
        cursor, depth = head.end(), 0
        while cursor < len(text) and not (text[cursor] == ";" and depth == 0):
            depth += (text[cursor] == "{") - (text[cursor] == "}")
            cursor += 1
        found[head.group(1)] = " ".join(text[head.end() : cursor].split())
    return found


def _with_inherited(
    name: str, declarations: dict[str, tuple[dict[str, str], list[str]]]
) -> set[str]:
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


def _member_types(
    name: str, declarations: dict[str, tuple[dict[str, str], list[str]]]
) -> dict[str, str]:
    """``_with_inherited`` carrying each member's type text. A subclass wins over its base."""
    fields, bases = declarations[name]
    resolved: dict[str, str] = {}
    for base in bases:
        if base in declarations:
            resolved.update(_member_types(base, declarations))
    resolved.update(fields)
    return resolved


#: Python annotations that render as one TypeScript word. ``datetime`` is a string on the wire
#: because that is what Pydantic serializes it as, and the browser types those members
#: ``string``; comparing the annotation itself would flag every timestamp in the tree.
_PRIMITIVES: dict[object, str] = {
    str: "string",
    int: "number",
    float: "number",
    bool: "boolean",
    bytes: "string",
    type(None): "null",
    datetime.datetime: "string",
    datetime.date: "string",
    datetime.timedelta: "string",
    decimal.Decimal: "number",
    uuid.UUID: "string",
    Any: "unknown",
    object: "unknown",
}


def _as_typescript(annotation: object, depth: int = 0) -> str:
    """One Pydantic annotation as the TypeScript the browser would have to write for it.

    Rendered from ``model_fields`` rather than from the OpenAPI document, which is the source
    the name comparison above already reads. The document was measured against this and loses
    two things the comparison needs: ``dict[str, int]`` erases to a bare ``object`` there, and
    a field with a default drops out of ``required`` even though the route always sends it, so
    six members would compare as unrelated and 210 as optional-versus-required against a
    document that is describing request validation rather than what a response carries.

    Bounded (rule 147) to the constructs ``reaper.api.*`` and the two engine modules actually
    annotate with. Anything past ``depth`` or outside the table renders ``unknown``, which
    compares as "the server declares nothing here" rather than as a mismatch.
    """
    if depth > 6:
        return "unknown"
    try:
        if annotation in _PRIMITIVES:
            return _PRIMITIVES[annotation]
    except TypeError:  # an unhashable annotation, which nothing here uses today
        return "unknown"
    origin, args = get_origin(annotation), get_args(annotation)
    if origin is Literal:
        return " | ".join(_as_literal(arg) for arg in args)
    if origin in (Union, types.UnionType):
        return " | ".join(dict.fromkeys(_as_typescript(arg, depth + 1) for arg in args))
    if origin in (list, set, frozenset, tuple):
        inner = _as_typescript(args[0], depth + 1) if args else "unknown"
        return f"({inner})[]" if "|" in inner else f"{inner}[]"
    if origin is dict:
        key = _as_typescript(args[0], depth + 1) if args else "string"
        value = _as_typescript(args[1], depth + 1) if args else "unknown"
        return f"Record<{key}, {value}>"
    if isinstance(annotation, type):
        if issubclass(annotation, BaseModel):
            return annotation.__name__
        if issubclass(annotation, enum.Enum):
            return " | ".join(_as_literal(member.value) for member in annotation)
    return "unknown"


def _as_literal(value: object) -> str:
    if value is None:
        return "null"
    if value is True or value is False:
        return "true" if value else "false"
    return f'"{value}"' if isinstance(value, str) else str(value)


def _server_models() -> tuple[dict[str, dict[str, str]], dict[str, dict[str, str]]]:
    """The wire models and the two engine modules the browser also mirrors, kept apart.

    Each maps a field to the TypeScript its annotation renders as, so one walk answers both
    the name comparison and the type one. ``_as_typescript`` says why the annotations are read
    rather than the served document.

    ``model_fields`` rather than the OpenAPI document: it already includes inherited fields,
    and it covers models no route publishes -- ``PolicyIn`` is nested inside ``PolicyOut``
    rather than returned, and the browser mirrors it as its own type.
    """
    wire: dict[str, dict[str, str]] = {}
    inner: dict[str, dict[str, str]] = {}
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
                bucket[value.__name__] = {
                    field: _as_typescript(info.annotation)
                    for field, info in value.model_fields.items()
                }
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


def _pair(name: str, wire: Mapping[str, object], inner: Mapping[str, object]) -> str | None:
    """The server model a browser type mirrors, or ``None`` if it mirrors nothing.

    Takes either shape the walk is read in, since only the keys decide a pairing.
    """
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
def browser_member_types() -> dict[str, dict[str, str]]:
    declarations = _declarations(API_TS.read_text(encoding="utf-8"))
    return {name: _member_types(name, declarations) for name in declarations}


@pytest.fixture(scope="module")
def server_member_types() -> tuple[dict[str, dict[str, str]], dict[str, dict[str, str]]]:
    return _server_models()


@pytest.fixture(scope="module")
def server_tables(
    server_member_types: tuple[dict[str, dict[str, str]], dict[str, dict[str, str]]],
) -> tuple[dict[str, set[str]], dict[str, set[str]]]:
    """The same walk as field-name sets, which is all the name comparison needs."""
    wire, inner = server_member_types
    return (
        {name: set(fields) for name, fields in wire.items()},
        {name: set(fields) for name, fields in inner.items()},
    )


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
        assert list(found["Plain"][0]) == ["id", "title"]
        assert list(found["Fancy"][0]) == [
            "status",
            "optional_thing",
            "state",
            "counts",
            "rows",
            "nested",
            "wrapped",
        ], "a nested object's own members must not be read as the outer type's"
        assert found["Fancy"][1] == ["Plain"]

    def test_it_reads_each_member_type_whole(self) -> None:
        """The type comparison is only as good as the text collected here, and every one of
        these spellings is in ``api.ts``. A wrapped union read at its first line, or an inline
        object read to its first ``;``, compares as a different type than the file declares."""
        members = _declarations(self.SAMPLE)["Fancy"][0]

        assert members["status"] == '"matched" | "unmatched" | null'
        assert members["optional_thing"] == "string | null", "the `?` is dropped, not the type"
        assert members["state"] == "string", "a trailing line comment is not part of the type"
        assert members["counts"] == "Record<string, number>"
        assert members["rows"] == "Plain[]"
        assert members["nested"] == "{ inner_one: string; inner_two: number; } | null", (
            "an inline object is collected whole, up to the `;` that ends the OUTER member"
        )
        assert members["wrapped"] == '| "a" | "b"', "a union wrapped over three lines is one type"

    def test_it_collects_every_type_alias(self) -> None:
        """The 17 aliases are the browser's enums, and a member typed with one reads as a
        single word until it is expanded."""
        assert _type_aliases(self.SAMPLE) == {
            "Verdict": '"condemn" | "protect" | "abstain"',
            "Union": '| { kind: "boolean"; weight: number; } | { kind: "graded"; floor: number; }',
        }

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
        assert list(found["X"][0]) == ["real"]


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


#: A ``|`` inside any of these brackets belongs to an inner type, not to the union being split.
_BRACKETS = {"{": "}", "<": ">", "(": ")", "[": "]"}


def _union_members(text: str) -> frozenset[str]:
    """One TypeScript type as its top-level union members.

    ``Record<string, A | B>`` and ``{ a: X | null }`` are one member each, so the split counts
    depth rather than splitting on every ``|`` (rule 147). A leading ``|`` is the wrapped
    spelling and contributes nothing.

    A member that is itself a parenthesized union is flattened into this one, because
    ``("spare" | "reap") | null`` names three things and not two. ``_expanded`` adds that pair
    when an alias standing for a union lands in an array position, where it is load-bearing.
    """
    parts, buffer, depth = [], "", 0
    for char in text:
        if char in _BRACKETS:
            depth += 1
        elif char in _BRACKETS.values():
            depth -= 1
        if char == "|" and depth == 0:
            parts.append(buffer)
            buffer = ""
        else:
            buffer += char
    parts.append(buffer)

    members: set[str] = set()
    for part in (p.strip() for p in parts):
        if _is_wrapped(part):
            members |= _union_members(part[1:-1])
        elif part:
            members.add(part)
    return frozenset(members)


def _is_wrapped(text: str) -> bool:
    """Whether one pair of parentheses encloses the whole of ``text``."""
    if not (text.startswith("(") and text.endswith(")")):
        return False
    depth = 0
    for position, char in enumerate(text):
        depth += (char == "(") - (char == ")")
        if depth == 0:
            return position == len(text) - 1
    return False


def _expanded(text: str, aliases: dict[str, str], depth: int = 0) -> str:
    """One TypeScript type with every alias name replaced by what it stands for.

    Without this, a member typed ``Verdict`` is one opaque word and the comparison below can
    only say it is not spelled ``string``. Bounded at four hops, which is three more than this
    file needs today.
    """
    if depth > 3:
        return text

    def one(hit: re.Match[str]) -> str:
        body = aliases.get(hit.group(1))
        if body is None:
            return hit.group(1)
        # Parenthesized when it is a union, because `PolicyRepair[]` expanded bare would bind
        # the `[]` to the union's last member and read as four types rather than one array.
        return f"({body})" if len(_union_members(body)) > 1 else body

    grown = re.sub(r"\b(\w+)\b", one, text)
    return grown if grown == text else _expanded(grown, aliases, depth + 1)


def _server_name_to_browser(text: str, browser_types: dict[str, dict[str, str]]) -> str:
    """``MatchOut`` reads as ``Match`` here, so a rename is not counted as a disagreement."""
    reverse = {server: browser for browser, server in ALIAS.items()}

    def one(hit: re.Match[str]) -> str:
        word = hit.group(1)
        if word in reverse:
            return reverse[word]
        for suffix in ("Out", "IO", "In"):
            if word.endswith(suffix) and word[: -len(suffix)] in browser_types:
                return word[: -len(suffix)]
        return word

    return re.sub(r"\b(\w+)\b", one, text)


def _element(text: str) -> str:
    """``("a" | "b")[]`` and ``X[]`` as what they hold."""
    inner = text[:-2]
    return inner[1:-1] if _is_wrapped(inner) else inner


def _covers(declared: str, narrower: str) -> bool:
    """Whether the server's ``declared`` member admits the browser's ``narrower`` one.

    ``str`` admits any string literal, and that is the whole of the narrowing class below: the
    wire models type ``verdict`` as a bare ``str`` on purpose, so the browser's three-literal
    union is a subset of what the server may send rather than a contradiction of it.
    """
    if declared == narrower:
        return True
    # `Any` and `dict[str, Any]` are the absence of a declaration, so nothing about them can
    # disagree with the browser. Two members reach this: PolicyBody.custom_condemn and
    # ListConfig.config, both of which the browser types and the model leaves open.
    if declared == "unknown" or (declared.startswith("Record<") and declared.endswith("unknown>")):
        return True
    if declared.endswith("[]") and narrower.endswith("[]"):
        return _within(_union_members(_element(narrower)), _union_members(_element(declared)))
    if declared == "string":
        return narrower.startswith('"') and narrower.endswith('"')
    if declared == "number":
        return narrower.replace(".", "", 1).lstrip("-").isdigit()
    if declared == "boolean":
        return narrower in ("true", "false")
    # A member the browser spells as an inline object pairs with the model the server named
    # for it, and neither side is read past its top level -- so this holds in both directions
    # rather than reading as a narrowing. LeavingSoonSettings.last and .last_skip are the two
    # cases; the file docstring carries the bound.
    return (declared.isidentifier() and narrower.startswith("{")) or (
        narrower.isidentifier() and declared.startswith("{")
    )


def _within(inner: frozenset[str], outer: frozenset[str]) -> bool:
    return all(any(_covers(one, member) for one in outer) for member in inner)


#: Members where the browser's type is a strict subset of the server's, which is safe in the
#: direction that matters: the browser refuses a value the server could send rather than
#: passing one the server would reject. Every entry today is one shape -- the wire model types
#: the field as a bare ``str`` and the browser closes it to the literals it dispatches on.
#:
#: **The bare ``str`` is deliberate on the server and stays.** ``verdict`` and ``override``
#: are ``Literal`` types in ``engine.verdict``, but the wire models validate rows read back
#: out of the database, where nothing constrains the column -- no CHECK, and no migration ever
#: wrote an out-of-set value. Narrowing the wire model turns a legacy value from "renders
#: oddly" into a 500 on the review queue, which fails in the operator's face rather than
#: toward keeping the file.
#:
#: A member listed here is not a bug. A member NOT listed here is, which is why this is a set
#: and not a count (rule 145): swapping one narrowing for another keeps a count whole.
NARROWED: set[str] = {
    "Candidate.override",
    "Candidate.override_own",
    "Candidate.show_override",
    "Candidate.show_status",
    "Candidate.verdict",
    "CandidateDetail.override",
    "CandidateDetail.override_own",
    "CandidateDetail.show_override",
    "CandidateDetail.show_status",
    "CandidateDetail.verdict",
    "Group.show_override",
    "Group.show_status",
    "GroupSeasonMark.override",
    "GroupSeasonMark.verdict",
    "Instance.kind",
    "ListConfig.config",
    "Match.status",
    "PersonTitle.verdict",
    "PlexLibrary.kind",
    "PlexLinkPoll.status",
    "PlexPoll.status",
    "PlexResources.source",
    "RatingRule.source",
    "SeerrService.kind",
    "WhitelistEntry.decision",
}

#: Members where the browser's type is a strict superset of the server's: the server declares
#: a ``Literal`` or an enum and the browser types the field ``string``. Nothing crashes on one
#: -- the browser accepts everything the server can send -- but the compiler stops helping.
#:
#: ``Policy.repairs`` is the deliberate one, and ``api.ts`` says why beside it: the union ends
#: in ``(string & {})`` so an id this build has never heard of is a value TypeScript admits
#: exists rather than a cast, which is what lets ``PolicyEditor``'s fallback handle it. The
#: other six carry no such argument. They are the plain ``string`` the field was first written
#: as, and each is one place ``docs/history/SIMPLIFICATION_PLAN.md``'s 4.3 has left to do
#: -- ``GateId``
#: already exists as a union in ``components/policyMeta.ts``, so ``GateSetting.gate`` is the
#: cheapest of them.
WIDENED: set[str] = {
    "Condition.op",
    "GateSetting.gate",
    "Policy.repairs",
    "SignalProbe.signal",
    "SignalSetting.signal",
    "VocabField.ops",
    "Vocabulary.lane",
}

#: Phase 8a (#870-880's continuation) typed the *server* side of every refusal,
#: `PlexPollOut.reason` and its Settings-link sibling `PlexLinkPollOut.reason` among them, ahead
#: of the browser: composing the sentence needed `ui.json`'s `error.*` namespace, which did not
#: exist yet. Phase 8b added that namespace, updated `api.ts` to `ReasonKey | null` for both
#: members, and composed the retrying text from it in the same change, so this is empty now --
#: kept declared, rather than deleted along with the loop that reads it, so a future conversion
#: needing the same temporary two-sided-change shape has a named place to add its members.
PENDING_PHASE_8B: set[str] = set()


class TestTheTwoCopiesAgreeOnTypes:
    """The second half of the guard: the two copies agree about what is IN each field.

    Names were all this file compared until now, and the reason given was that comparing types
    would flag eight pairs on day one. Measured, it flags 42 across 749 members, and they sort
    into four classes rather than one, three of which are worth naming and one of which was a
    live inaccuracy. The two sets above hold the deliberate ones by name.

    **Optionality is still not compared, and that is now a measurement rather than a bound.**
    The server's ``required`` list describes what a REQUEST may omit; every model here is a
    response, and a response model with a default still serializes the field, so 210 members
    read "optional" on the server while the route sends every one of them. The browser's ``?``
    is a different fact again -- several fields carry it because no component reads them and
    the fixtures are not made to carry them. There is no true reference to compare against, so
    the ``?`` is dropped on both sides and the type beside it is what is checked.

    **Dropping a ``null`` is the one narrowing that is never allowed**, and it has its own
    test. A component reading ``x.foo`` on a value the server sends as ``null`` throws where a
    component switching on an unexpected string takes a fall-through arm.
    """

    def _diff(
        self,
        browser_member_types: dict[str, dict[str, str]],
        server_member_types: tuple[dict[str, dict[str, str]], dict[str, dict[str, str]]],
    ) -> dict[str, tuple[str, str, frozenset[str], frozenset[str]]]:
        """Every member whose two declarations do not render as the same type."""
        wire, inner = server_member_types
        out: dict[str, tuple[str, str, frozenset[str], frozenset[str]]] = {}
        aliases = _type_aliases(API_TS.read_text(encoding="utf-8"))
        for name in sorted(browser_member_types):
            counterpart = _pair(name, wire, inner)
            if counterpart is None:
                continue
            declared = wire.get(counterpart) or inner.get(counterpart) or {}
            for field, written in sorted(browser_member_types[name].items()):
                if field not in declared:
                    continue  # a name difference, which the class above already reports
                mine = _union_members(_expanded(written, aliases))
                theirs = _union_members(
                    _server_name_to_browser(declared[field], browser_member_types)
                )
                if not (_within(mine, theirs) and _within(theirs, mine)):
                    out[f"{name}.{field}"] = (written, declared[field], mine, theirs)
        return out

    def test_no_paired_member_disagrees_in_a_way_nobody_declared(
        self,
        browser_member_types: dict[str, dict[str, str]],
        server_member_types: tuple[dict[str, dict[str, str]], dict[str, dict[str, str]]],
    ) -> None:
        """The guard itself. Rule 144: the message names the file to edit."""
        unrelated: list[str] = []
        for member, (written, declared, mine, theirs) in self._diff(
            browser_member_types, server_member_types
        ).items():
            if member in PENDING_PHASE_8B:
                continue
            if _within(mine, theirs) or _within(theirs, mine):
                continue
            unrelated.append(f"{member}: the browser says `{written}`, the server `{declared}`")

        assert unrelated == [], (
            "frontend/src/api.ts and the response models disagree about what a field holds, "
            "and not in either direction anyone declared. Neither type admits the other, so "
            "one of them is describing data that is not sent. Edit frontend/src/api.ts to "
            "match the model, or the model to match what the route really returns:\n  "
            + "\n  ".join(unrelated)
        )

    def test_the_browser_never_drops_a_null_the_server_declares(
        self,
        browser_member_types: dict[str, dict[str, str]],
        server_member_types: tuple[dict[str, dict[str, str]], dict[str, dict[str, str]]],
    ) -> None:
        """The narrowing that crashes rather than degrades, so it has no allowed set.

        ``Explanation.match`` was typed ``Match`` against a server ``MatchOut | None``, and
        ``base_score``/``keep_discount`` were ``number`` against ``float | None``. All three
        read as non-null to the compiler while the server really sends ``null``, and all three
        were already guarded at every call site -- the code knew, the declaration did not, so
        the next reader of one was the one who would find out.
        """
        dropped: list[str] = []
        for member, (written, declared, mine, theirs) in self._diff(
            browser_member_types, server_member_types
        ).items():
            if "null" in theirs and "null" not in mine:
                dropped.append(f"{member}: the browser says `{written}`, the server `{declared}`")

        assert dropped == [], (
            "the server can send null for these and frontend/src/api.ts does not say so, so "
            "every component reading one is one unguarded property access from a blank panel. "
            "Add `| null` in frontend/src/api.ts and follow the compiler:\n  "
            + "\n  ".join(dropped)
        )

    def test_every_deliberate_difference_is_the_one_that_was_declared(
        self,
        browser_member_types: dict[str, dict[str, str]],
        server_member_types: tuple[dict[str, dict[str, str]], dict[str, dict[str, str]]],
    ) -> None:
        """Rule 103's last sentence, at the member level: a difference the guard tolerates is
        written down, not silenced. This is also the walk's population statement (rule 145) --
        a swap that keeps the total would pass a count and fails here."""
        narrowed, widened = set(), set()
        for member, (_written, _declared, mine, theirs) in self._diff(
            browser_member_types, server_member_types
        ).items():
            if _within(mine, theirs):
                narrowed.add(member)
            elif _within(theirs, mine):
                widened.add(member)

        assert narrowed == NARROWED, (
            "the set of members where the browser types a field more tightly than the server "
            "declares it has changed. Add or remove the entry in NARROWED in this file:\n  "
            + "\n  ".join(sorted(narrowed ^ NARROWED))
        )
        assert widened == WIDENED, (
            "the set of members where the server declares a closed set and the browser types "
            "it `string` has changed. Add or remove the entry in WIDENED in this file:\n  "
            + "\n  ".join(sorted(widened ^ WIDENED))
        )


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
                pair = f"{name} <-> {counterpart}"
                drifted.append(
                    f"{pair}:"
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

    def test_the_browser_knows_every_field_type_the_vocabulary_serves(self) -> None:
        """``VocabField.type`` was a bare ``str`` here, the same hole one level down. Both sides
        agreed the field had a ``type`` and neither could disagree about its values.

        Two of the six convert, and a conversion is what writes a policy value. A size is typed
        in GB and stored in bytes, a rating typed as 7.5 and stored as 75. A member the browser
        has no name for reaches the ladders below and takes a fall-through arm, which at
        ``coerceValue`` means the typed number stored raw.

        This message is the one place those sites are listed, and ``api.ts`` points here rather
        than restating them (rule 144).
        """
        from reaper.engine.fields import FieldType

        server = {member.value for member in FieldType}
        assert self._declared("FieldType") == server, (
            "engine.fields.FieldType and frontend/src/api.ts's FieldType union disagree. Eight "
            "sites in frontend/src/components/PolicyRuleEditors.tsx dispatch on this value and "
            "not one of them is exhaustive. Four branch on it and fall through: `rampDefaults`, "
            "`rampValue`, `coerceValue`, `describeCondition`. One picks the ramp bound's "
            "component, a plain number box for anything that is neither `days` nor `bytes`. "
            "Three pick a `step`, which decides what can be typed at all: on a whole step "
            "`QuantityInput` withholds a fractional keystroke, so a new fractional member is "
            "untypeable before `coerceValue` ever sees it. Adding the member to the union is the "
            "first half of the change. Reading those eight is the second."
        )
        assert len(server) == 6

    def test_the_request_model_reads_the_declaration_rather_than_restating_it(self) -> None:
        """Rule 131. ``OverrideIn.decision`` used to spell the pair itself, which is a second
        copy of the vocabulary sitting one import away from the first."""
        from reaper.api.schemas import OverrideIn
        from reaper.engine.verdict import Override

        assert set(get_args(OverrideIn.model_fields["decision"].annotation)) == set(
            get_args(Override)
        )


class TestTheRewatchKeepNameIsOneDeclaration:
    """The built-in rewatch keep's name is typed twice (rule 144). ``engine/signals.py``'s
    ``REWATCH_KEEP`` is the field ``evaluate_keep`` special-cases and the row name the stored
    explanation carries; ``frontend/src/api.ts``'s ``REWATCH_KEEP`` mirrors it so
    ``WhyPanel.tsx`` can suppress the "Your rule" tag on the built-in row without a second
    hardcoded string. A drift here renders the built-in row tagged as if the operator wrote
    it, or leaves an operator's own rule of the same name silently untagged.
    """

    def test_the_two_declarations_agree(self) -> None:
        from reaper.engine.signals import REWATCH_KEEP

        found = re.search(
            r'export const REWATCH_KEEP = "([^"]+)";', API_TS.read_text(encoding="utf-8")
        )
        assert found is not None, (
            "frontend/src/api.ts no longer declares `export const REWATCH_KEEP` as one "
            "statement, so this guard is reading nothing. Re-point it at the new spelling."
        )
        assert found.group(1) == REWATCH_KEEP, (
            f"engine/signals.py's REWATCH_KEEP ({REWATCH_KEEP!r}) and frontend/src/api.ts's "
            f"REWATCH_KEEP ({found.group(1)!r}) disagree. WhyPanel.tsx's "
            "`keep.name !== REWATCH_KEEP` check would then suppress the built-in row's tag "
            "on the wrong name, or never suppress it at all."
        )


class TestEveryGateIdHasOperatorCopy:
    """A gate id the browser has no copy for is printed at the operator as a slug.

    ``gateMeta`` (``frontend/src/components/policyMeta.ts``) is the browser's one
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
    ``_kept_reason`` turns the same ids into the review queue's chip. It is deliberately not
    covered, because its own fallback is the id ``kept.unknown``, whose catalog entry
    (``chip.sentence.kept.unknown``) composes "A protection applies." -- vague, but a
    sentence, never a slug. Swap that fallback for anything id-shaped and it needs a guard
    of its own.

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
            "gateMeta with a plain-language label (tsc requires it once the union moves)."
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
            f"policyMeta.ts no longer closes gateMeta with `{clause}`, so tsc has stopped "
            "requiring a label for every gate id and for the hand-spare tally."
        )
        for gate in self._declared():
            assert f"\n    {gate}: {{" in source, f"{gate} has no gateMeta entry"

    def _marked_retired(self) -> set[str]:
        """Every id ``gateMeta`` marks ``retired``.

        Comments are stripped first, and each entry is read brace-depth aware, so
        ``server_popularity``'s nested ``window`` object is part of its entry rather than an
        entry of its own (rule 147). A file this stops finding entries in fails the set
        comparison below rather than matching nothing.
        """
        text = _LINE_COMMENT.sub("", _BLOCK_COMMENT.sub("", self._source()))
        start = text.find("export function gateMeta")
        assert start != -1, "policyMeta.ts no longer declares `export function gateMeta`."
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
#:
#: Keyed on the ENCLOSING FUNCTION rather than the line, which is the same key
#: ``_MEMBERSHIP_INVENTORY`` uses in ``test_repo_hygiene.py`` and for the same reason: a line
#: number moves whenever anything above it does, so a list of them fails on somebody else's
#: unrelated diff. A function name moves only when this site does.
_COLLAPSE_SITES = [
    "src/reaper/api/backup.py::restore_prepare",
    "src/reaper/api/breakdown.py::get_reap_breakdown",
    "src/reaper/api/fairness.py::_row_out",
    "src/reaper/api/review.py::_deep_links",
    "src/reaper/api/settings.py::instance_seerr_services",
    "src/reaper/api/settings.py::test_new_instance",
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
        cannot hold the number still while ``COLLAPSED_PAIRS`` goes stale. A site at module
        level rather than inside a function is invisible to the walk and would have nothing
        to key on; none exists, and one would have no route to serve.
        """
        sites = []
        for path in sorted((REPO / "src" / "reaper" / "api").rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for parent in ast.walk(tree):
                if not isinstance(parent, ast.FunctionDef | ast.AsyncFunctionDef):
                    continue
                for node in ast.walk(parent):
                    if not isinstance(node, ast.Call):
                        continue
                    for keyword in node.keywords:
                        collapsed = keyword.arg == "from_attributes"
                        if collapsed and getattr(keyword.value, "value", None) is True:
                            sites.append(f"{path.relative_to(REPO)}::{parent.name}")

        assert sorted(sites) == _COLLAPSE_SITES, (
            f"the sites building a wire model off a service record are now {sorted(sites)}. "
            "Add or remove its pair in COLLAPSED_PAIRS above, then move this list."
        )


class TestEveryPlanStepIdHasOperatorCopy:
    """The plan table's kind and state cells read plain words, held to the server's sets.

    ``stepKind``/``stepState`` (``frontend/src/components/ReapPlan.tsx``) turn stored step
    ids into copy, with the raw id as the unknown-id fallback (#851). Rule 66: that
    fallback is exactly what makes a missing member silent, so both maps are pinned here,
    both directions. The literal request the page promises lives whole in the Request
    column, which is what freed these cells to be copy.

    ``StepState`` is an enum and mirrors directly. The kinds have no enum: the planner's
    ``kind="..."`` literals are the declaration, parsed as source the way the jobs-page
    guard reads ``JobsPanel`` (rule 147: quoted literals only, and a spelling either
    matcher stops finding asserts rather than matching nothing).
    """

    REAP_PLAN_TSX = REPO / "frontend" / "src" / "components" / "ReapPlan.tsx"
    PLANNER_PY = REPO / "src" / "reaper" / "services" / "planner.py"

    def _frontend_cases(self, fn: str) -> set[str]:
        source = self.REAP_PLAN_TSX.read_text(encoding="utf-8")
        found = re.search(
            rf"function {fn}\((?:kind|state): string\): string \{{(.*?)\n\}}", source, re.S
        )
        assert found is not None, (
            f"ReapPlan.tsx no longer declares {fn} the way this guard reads it. "
            "Re-point the matcher at the new spelling."
        )
        return set(re.findall(r'=== "([^"]+)"', found.group(1)))

    def test_the_browser_words_every_step_state(self) -> None:
        from reaper.db.models import StepState

        assert self._frontend_cases("stepState") == {member.value for member in StepState}, (
            "db.models.StepState and ReapPlan.tsx's stepState disagree. A state the browser "
            "does not know renders as its raw id in the plan table (rule 21); a state the "
            "server no longer stores is dead copy. Move both sides together."
        )

    def test_the_browser_words_every_step_kind_the_planner_emits(self) -> None:
        emitted = set(re.findall(r'kind="([^"]+)"', self.PLANNER_PY.read_text(encoding="utf-8")))
        assert emitted, (
            "services/planner.py no longer spells any kind= as a quoted literal, so this "
            "guard is reading nothing. Re-point it at the new spelling."
        )
        assert self._frontend_cases("stepKind") == emitted, (
            "the planner's step kinds and ReapPlan.tsx's stepKind disagree. A kind the "
            "browser does not know renders as its raw id in the plan table (rule 21); a "
            "kind the planner no longer emits is dead copy. Move both sides together."
        )
