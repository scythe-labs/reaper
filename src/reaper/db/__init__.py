# SPDX-License-Identifier: AGPL-3.0-or-later
"""Database layer."""

#: Keys per ``WHERE col IN (...)`` expansion (rule 94). An expanding bindparam binds one
#: variable per key, and SQLite refuses any statement carrying more of them than
#: ``SQLITE_LIMIT_VARIABLE_NUMBER``: 32,766 on the runtime this ships against (sqlite
#: 3.53.1, read back from the driver rather than remembered), and 999 on builds older than
#: 3.32. This clears both, so no call site has to re-derive it per platform. Every reader
#: that feeds a library-sized key set into one statement chunks on this and merges the
#: results, which is exact because the chunks are disjoint keys.
#:
#: **It bounds one expansion, and a statement may hold several.**
#: ``fairness._evidence_index`` binds ``:keys`` three times in one UNION, so a chunk there is
#: three times this number of variables. Still far under the ceiling above, but a fourth arm
#: is arithmetic somebody has to do rather than a limit that holds itself.
#:
#: A KEY bound, not a row bound: a multi-row INSERT binds several variables per row, so
#: ``snapshot._insert_first_flags`` (three a row) and ``watch_evidence._CHUNK`` (four)
#: keep their own smaller numbers and say why beside them.
#:
#: It lives here, in the package's own ``__init__``, because the bound belongs to the
#: database rather than to any one query, and because this module imports nothing: the
#: cache-db readers (``fairness``, ``imdb_dataset``) take it without pulling in the engine
#: or ``Settings``.
KEY_CHUNK = 500
