# SPDX-License-Identifier: AGPL-3.0-or-later
"""User-authored protections.

The bridge between the field registry and the gate lane. A ``CustomProtectGate``
wraps a ``RuleSet`` the owner wrote themselves and presents it as a gate -- which
means it inherits the gate lane's central property for free:

    **it can only return PROTECT or ABSTAIN.**

There is no ``CustomCondemnGate``, and there never will be. That asymmetry is the
whole reason the owner can be trusted to author rules here: the worst a badly
written protection can do is keep a file.
"""

from __future__ import annotations

from dataclasses import dataclass

from reaper.engine.fields import Lane, RuleSet, evaluate_rules
from reaper.engine.gates import ABSTAIN, PROTECT, Facts, GateId, GateResult


@dataclass(frozen=True, slots=True)
class CustomProtectGate:
    """A protection the owner wrote."""

    name: str
    rules: RuleSet
    id: GateId = GateId.CUSTOM

    def __post_init__(self) -> None:
        if self.rules.lane is not Lane.PROTECT:
            raise ValueError(
                "A custom gate may only hold PROTECT rules. There is no way to author "
                "a condemnation, and that is deliberate."
            )

    def evaluate(self, facts: Facts) -> GateResult:
        result = evaluate_rules(self.rules, facts)

        if result.matched:
            reasons = "; ".join(r.detail for r in result.results if r.matched)
            return GateResult(self.id, PROTECT, detail=f"{self.name}: {reasons}")

        if result.blocked:
            blocked = "; ".join(r.detail for r in result.results if r.blocked)
            return GateResult(self.id, ABSTAIN, blocked=True, detail=f"{self.name}: {blocked}")

        checked = "; ".join(r.detail for r in result.results)
        return GateResult(self.id, ABSTAIN, detail=f"checked: {self.name} -- {checked}")
