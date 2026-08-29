# SPDX-License-Identifier: AGPL-3.0-or-later
"""Checks ``_reasons.text``'s namespace argument against a shared fixture.

``why.ts`` has ``composeIn(namespace, key)`` and this module has ``_reasons.text(reason,
namespace=...)``. Both are composers that must render the same message for the same input.
This test injects the two fixture messages ``why.test.ts``'s "composeIn" block uses and
checks both composers render them the same way.
"""

from __future__ import annotations

import pytest

import tests._reasons as _reasons
from reaper.engine.reason import Reason
from tests._reasons import text

#: The Python copy of the fixture frontend/src/why.test.ts's "composeIn" describe block
#: loads into i18next. The two files write the same fixture directly, in their own
#: language, rather than sharing one file, so each runtime is checked on its own real code
#: path. Keep them in sync by hand: this comment and that block's comment point at each other.
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
