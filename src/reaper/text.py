# SPDX-License-Identifier: AGPL-3.0-or-later
"""The comparison form for a name the operator typed and a name a service stored.

One derivation, because rule 88 is a fail-open protection rule: when one side of a name
comparison is folded and the other is not, the keep rule stops matching and nothing
announces it. It was spelled inline 37 times over 33 lines in 14 modules before this module
existed.
"""

from __future__ import annotations


def fold(value: str) -> str:
    """Trim it, then case-fold it. The form both sides of a name comparison take.

    ``casefold`` rather than ``lower``, which matters outside ASCII: a German list named
    ``STRASSE`` and one named ``Straße`` fold together and lower-case apart.

    **SQL does not have this function, and one comparison crosses that line.**
    ``list_config._refuse_name_twice`` compares ``func.lower()`` in SQLite against a folded
    Python string, so the two disagree on exactly the characters ``casefold`` handles and
    ``lower`` does not. The column's ``NOCASE`` collation is what actually holds the
    uniqueness, and it is ASCII-only too, so the divergence is between two ASCII-equal
    answers today. It is named here rather than hidden because a fold that reaches SQL
    would need a different answer.

    ``str`` and not ``object``: eight call sites already wrap in ``str(...)``, and an
    ``object`` signature would let ``fold(None)`` key on ``"none"``.
    """
    return value.strip().casefold()
