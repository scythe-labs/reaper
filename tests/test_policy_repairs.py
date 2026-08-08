# SPDX-License-Identifier: AGPL-3.0-or-later
"""``PolicyRepair`` is one declaration with four surfaces, and this walks all four.

A stored body Reaper had to repair to load it degrades every scan, and the *only* thing that
clears the degradation is an operator saving the policy. So each repair owes four things:

1. it reaches ``PolicyOut.repairs``, or the browser never hears about it;
2. the editor opens dirty on it, or there is no Save to press;
3. the editor says which repair happened, or the operator does not know what to check;
4. the incomplete-scan notice names what to check, or the remedy points at nothing.

``lists_migrated`` shipped with (1) missing and everything downstream of it therefore missing
too: every scan degraded telling the operator to open the policy page and save, the policy
page stayed clean, and there was no way out at all (#516). Four booleans made that possible,
because each one is remembered separately and reads correct on its own line. This file is what
replaces remembering: it walks the enum and fails naming the file that is short a member.

Rule 145: the walks are counted as well as checked, because a matcher can only break on a
member it collected, and one that drops out of the walk is missing from the guard and from the
proof at once.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from reaper.api.schemas import PolicyOut
from reaper.engine.policy import PolicyRepair
from reaper.services.scan_runner import _REPAIR_CHECKS, _what_to_check

REPO = Path(__file__).resolve().parents[1]
EDITOR = REPO / "frontend/src/components/PolicyEditor.tsx"
API_TS = REPO / "frontend/src/api.ts"

#: Reconciled by hand against ``PolicyRepair``: rescaled, fell_back, rating_rules_restored,
#: lists_migrated. Pinned so a member dropping out of every walk below cannot read as four
#: green walks over three members (rule 145). The frontend pins the same number in
#: ``PolicyEditor.test.tsx``, which is the other half of this agreement.
EXPECTED_REPAIRS = 4


def _editor_notice_ids() -> list[str]:
    """The keys of ``REPAIR_NOTICES`` in the editor.

    Read as the whole object literal and then scanned for keys, rather than anchored on a
    delimiter one spelling happens to put there: rule 147, whose case was a matcher that read
    a literal className and silently skipped a ternary. The braces are balanced by counting,
    so a nested object inside an entry cannot end the literal early.
    """
    text = EDITOR.read_text()
    start = text.index("export const REPAIR_NOTICES")
    open_brace = text.index("{", start)
    depth = 0
    for i in range(open_brace, len(text)):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                literal = text[open_brace : i + 1]
                break
    else:  # pragma: no cover - an unbalanced literal would not compile
        pytest.fail(f"REPAIR_NOTICES in {EDITOR.name} has no closing brace")
    # Keys at depth 1 only: the nested entries carry `tone`/`where`/`text`, never a repair id.
    ids: list[str] = []
    depth = 0
    for line in literal.splitlines():
        match = re.match(r"\s*([A-Za-z_][A-Za-z0-9_]*):\s*\{", line)
        if match and depth == 1:
            ids.append(match.group(1))
        depth += line.count("{") - line.count("}")
    return ids


class TestEveryRepairIsWiredEverywhere:
    def test_the_walk_covers_every_repair(self) -> None:
        """The count first, so the three checks below are known to be walking four members."""
        assert len(list(PolicyRepair)) == EXPECTED_REPAIRS

    def test_the_scan_names_what_to_check_for_each(self) -> None:
        """Rule 144: the degradation's remedy clause is generated per repair, so a member with
        no entry sends the operator to check nothing in particular."""
        missing = sorted(r.value for r in PolicyRepair if r not in _REPAIR_CHECKS)
        assert not missing, (
            f"src/reaper/services/scan_runner.py:_REPAIR_CHECKS is missing {missing}. "
            "Every PolicyRepair names what the operator should look at."
        )

    def test_the_editor_says_what_happened_for_each(self) -> None:
        """The sibling copy, in the other tree. Named by file so the failure is actionable
        from here rather than being a puzzle about which frontend file is short a key."""
        ids = _editor_notice_ids()
        assert len(ids) == EXPECTED_REPAIRS, (
            f"{EDITOR.relative_to(REPO)}:REPAIR_NOTICES holds {len(ids)} entries "
            f"({sorted(ids)}), expected {EXPECTED_REPAIRS}"
        )
        missing = sorted(r.value for r in PolicyRepair if r.value not in ids)
        assert not missing, (
            f"{EDITOR.relative_to(REPO)}:REPAIR_NOTICES is missing {missing}. "
            "Every PolicyRepair gets a sentence saying what changed and what clears it."
        )

    def test_the_typescript_union_carries_each(self) -> None:
        """`PolicyRepair` in `api.ts` is the type the editor's props are checked against. It
        admits unknown ids on purpose, so a missing member is a lost autocomplete rather than
        a broken build -- which is exactly why it needs a test."""
        text = API_TS.read_text()
        union = text[text.index("export type PolicyRepair") :].split(";", 1)[0]
        missing = sorted(r.value for r in PolicyRepair if f'"{r.value}"' not in union)
        assert not missing, f"{API_TS.relative_to(REPO)}:PolicyRepair is missing {missing}."


class TestTheRemedyClause:
    def test_names_every_repair_a_body_carried(self) -> None:
        """Two repairs compose, and naming only the first sends the operator to a control
        that is not the one that moved."""
        said = _what_to_check((PolicyRepair.LISTS_MIGRATED, PolicyRepair.RESCALED))
        assert said == "check your keep rules and the points"

    def test_says_something_for_a_repair_with_no_entry(self) -> None:
        """The fallback exists so a half-wired repair still produces a sentence. The test
        above is what stops it staying half-wired."""
        assert _what_to_check(()) == "check the values"

    @pytest.mark.parametrize("repair", list(PolicyRepair))
    def test_reads_as_a_sentence_for_each(self, repair: PolicyRepair) -> None:
        """Rule 21: it lands verbatim inside "open the policy page, ..., and save" on three
        screens, so it must be a clause, not a field name."""
        said = _what_to_check((repair,))
        assert said.startswith("check ")
        assert "_" not in said
        assert "—" not in said


class TestTheResponseCarriesThem:
    def test_policy_out_serializes_repairs_as_plain_strings(self) -> None:
        """The browser reads these as strings, so an enum that serialized as an object would
        make every id in `REPAIR_NOTICES` a miss and every notice the unknown fallback."""
        # `model_construct` skips validation by design, which is the point here -- the plugin
        # types it as needing every field anyway.
        out = PolicyOut.model_construct(repairs=[PolicyRepair.LISTS_MIGRATED])  # type: ignore[call-arg]
        assert json.loads(out.model_dump_json())["repairs"] == ["lists_migrated"]

    def test_repairs_defaults_to_empty(self) -> None:
        """An ordinary load reports nothing, so the editor opens clean and the savebar stays
        away. `dirty` counts this list, so a non-empty default would make every policy page
        permanently dirty."""
        assert PolicyOut.model_fields["repairs"].get_default(call_default_factory=True) == []
