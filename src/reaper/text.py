# SPDX-License-Identifier: AGPL-3.0-or-later
"""One shared form for comparing a name the operator typed against a name a service stored.

Comparing a folded name against an unfolded one breaks the match silently: the keep rule
stops matching and nothing announces it. Every comparison uses this same fold so that
never happens.
"""

from __future__ import annotations


def fold(value: str) -> str:
    """Fold a name for comparison: trim it, then case-fold it. Both sides of a name
    comparison use this same form.

    Uses ``casefold`` instead of ``lower``, which matters outside ASCII: a German list
    named ``STRASSE`` and one named ``Straße`` match under ``casefold`` but not under
    ``lower``.

    SQL has no ``casefold`` equivalent. ``list_config._refuse_name_twice`` compares
    SQLite's ``func.lower()`` against a folded Python string, so the two can disagree on
    non-ASCII characters. The column's ``NOCASE`` collation is what actually enforces the
    uniqueness, and it is ASCII-only too, so today both answers agree. A fold that reaches
    SQL would need to use ``lower()`` there instead.

    Takes a ``str``, not ``object``: every caller already converts its value with
    ``str(...)`` first, and an ``object`` parameter would let ``fold(None)`` silently
    become the string ``"none"``.
    """
    return value.strip().casefold()
