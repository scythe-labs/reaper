# SPDX-License-Identifier: AGPL-3.0-or-later
"""``_reasons.text``'s namespace argument, proven against a fixture (rule 119).

Phase 1 of the i18n plan (docs/history/I18N_PLAN.md) generalized the reason composer so a
chip status or a policy warning can carry its own catalog section instead of crowding
``why.*``: ``why.ts``'s ``composeIn(namespace, key)`` and this module's twin,
``_reasons.text(reason, namespace=...)``. Neither namespace has production content yet, so
this test injects the same two fixture messages ``why.test.ts``'s "composeIn" describe
block uses and checks both composers render them the same way -- proving the namespace
argument walks the catalog identically on both sides, not just that "why" still works.
"""

from __future__ import annotations

import pytest

import tests._reasons as _reasons
from reaper.engine.reason import Reason
from tests._reasons import text

#: The exact fixture frontend/src/why.test.ts's "composeIn" describe block adds to i18next
#: at test time. Keeping the two literal (rather than sharing a file across languages) is
#: the cheapest way to pin agreement between two runtimes; the pairing is what this
#: docstring and that block's comment each cite.
_FIXTURE: dict[str, dict[str, object]] = {
    "chip.text": {"fixture_count": "{n, plural, one {# stray file} other {# stray files}}"},
    "warning": {"fixture_nested": "blocked because {cause}"},
    "why": {"cause": {"fixture_cause": "the fixture reason fired"}},
}


@pytest.fixture(autouse=True)
def _fixture_catalog(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_reasons, "catalog", lambda namespace="why": _FIXTURE.get(namespace, {}))


def test_a_chip_text_entry_composes_under_its_own_namespace() -> None:
    assert text(Reason("fixture_count", {"n": 3}), namespace="chip.text") == "3 stray files"


def test_a_warning_entrys_nested_reason_still_resolves_under_why() -> None:
    reason = Reason("fixture_nested", {"cause": Reason("cause.fixture_cause", {})})
    assert text(reason, namespace="warning") == "blocked because the fixture reason fired"
