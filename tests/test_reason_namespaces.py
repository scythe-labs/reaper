# SPDX-License-Identifier: AGPL-3.0-or-later
"""``_reasons.text``'s namespace argument, proven against a fixture (rule 119).

#868 moves the status chip and the policy warnings onto their own catalog sections, so
``why.ts`` gained ``composeIn(namespace, key)`` and this module's twin gained
``_reasons.text(reason, namespace=...)``. Neither section has production content yet. This
test injects the two fixture messages ``why.test.ts``'s "composeIn" block uses and checks
both composers render them the same way.
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
