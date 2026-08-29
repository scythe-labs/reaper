# SPDX-License-Identifier: AGPL-3.0-or-later
"""Database layer."""

#: Keys per ``WHERE col IN (...)`` expansion. An expanding bind parameter binds one
#: variable per key, and SQLite refuses a statement that carries more variables than
#: ``SQLITE_LIMIT_VARIABLE_NUMBER``: 999 on older builds, far more on current ones. This
#: constant clears the lower limit, so no call site has to re-derive it per platform.
#: Every reader that feeds a library-sized key set into one statement chunks on this and
#: merges the results, which is exact because the chunks hold disjoint keys.
#:
#: It bounds one expansion, and a statement may hold several. ``fairness._evidence_index``
#: binds ``:keys`` three times in one UNION, so a chunk there uses three times this many
#: variables, still far under the limit, but a fourth use of the pattern would need its
#: own arithmetic.
#:
#: It counts keys, not rows: a multi-row INSERT binds several variables per row, so
#: ``snapshot._insert_first_flags`` (three a row) and ``watch_evidence._CHUNK`` (four)
#: keep their own smaller numbers and explain why beside them.
#:
#: It lives here, in the package's own ``__init__``, because the bound belongs to the
#: database rather than to any one query, and because this module imports nothing: the
#: cache-db readers (``fairness``, ``imdb_dataset``) can use it without pulling in the
#: engine or ``Settings``.
KEY_CHUNK = 500
