# SPDX-License-Identifier: AGPL-3.0-or-later
"""Whether this build can serve the database it was pointed at.

``alembic upgrade head`` only ever moves forward, so it is not a check by itself. Point
an older Reaper at a database a newer one migrated, the ordinary shape of a rollback
where the operator puts the previous image back after a bad upgrade, and nothing in the
boot sequence asks whether this build understands what is on disk. The mismatch surfaces
later, as SQLAlchemy emitting SQL that names a column that is not there, in the middle of
whatever the operator was doing. Refusing to open the database costs one lookup, and
refusing is the fail-closed choice: a boot that does not happen deletes nothing.

The gate is one verdict, :func:`refusal`, read from two places, both before anything is
served:

* :mod:`reaper.preflight`, which the container entrypoint, ``scripts/dev-local.sh`` and
  :func:`reaper.launcher.main` all run before ``alembic upgrade head``. That is the
  earliest point any of them can be stopped. It runs after the staged-restore swap on
  purpose: restoring a backup is one of the two ways out the message names, so checking
  first would take that door away.
* :func:`reaper.main.lifespan`, before the first row is read or written. It covers a
  process started without preflight, which is every way of launching uvicorn by hand.

``reaper-admin`` (:mod:`reaper.cli`) is deliberately not gated. It is the escape hatch
for an operator already locked out, it writes one auth row, and refusing there would
take away the last door in the building.

This module also answers a second question about the shipped migration scripts:
:func:`needs_snapshot`, whether anything between the database's revision and head asks
for a copy of the database before it runs. It lives here because reading those scripts
is what this module already does, and because the two answers are read one after the
other by the same caller (:mod:`reaper.preflight`).

There are three possible answers, and the middle one is the whole subject:

``None``
    No ``alembic_version`` row: a database built straight from the models, which is what
    a test carries, and a first boot before its own migrations run. Allowed.
a revision this build ships
    At head, or an ancestor ``alembic upgrade head`` will carry forward. Allowed.
anything else
    Refused. It is usually a newer Reaper's revision. It can also be a branch, or a
    database from a fork, and this build cannot tell which. Not being able to tell is
    never treated as "it is fine," because all three cases end the same way: SQL against
    a schema nobody here understands.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from reaper.buildinfo import install_root

#: What the operator sees when the database sits at a revision this build does not ship.
#: Leads with the state of their data, then the two ways out. The revision id itself is
#: deliberately left out: a hash is not an explanation, and tells the reader nothing they
#: can act on. The app logs it as a separate field for support instead
#: (``reaper.main._refuse_unservable_schema``).
#:
#: This message deliberately promises nothing about the state of the data. Preflight runs
#: the staged-restore swap immediately above this gate, so a restore armed under a newer
#: build and applied after a rollback can reach this message having just replaced the
#: database, which means "nothing was changed" would sometimes be false.
DATABASE_IS_NEWER = (
    "Reaper stopped. A newer version of Reaper set up this database, and this older "
    "version can't read it safely. Put the newer version back, or restore a backup made "
    "with this one."
)

#: The fail-closed answer when the migrations that ship beside the code cannot be read at
#: all. Without them there is no set to compare against, so the question is unanswerable
#: rather than answered "fine". ``launcher.main`` refuses the same broken install earlier
#: where it reaches it (a bare ``pip install``), in its own words but the same noun.
MIGRATIONS_UNREADABLE = (
    "Reaper stopped. It can't find the database migrations that ship with it, so it "
    "can't tell whether this database is one it can read. Install Reaper again."
)


class SchemaRefusedError(RuntimeError):
    """This build must not open the database it was pointed at.

    Carries operator copy: the message is printed and logged as it stands, never wrapped
    in a second sentence written somewhere else.
    """


def alembic_dir() -> Path:
    """The shipped ``alembic/`` directory, in every install shape.

    It sits at the project root beside ``src/`` (a source checkout), at the image workdir
    (``/app/alembic``), at ``$SNAP/alembic``, and inside the unpacked PyInstaller bundle.
    Walking up from this module reaches all four, since each puts the migrations above the
    package; ``install_root()`` names the two packaged shapes outright and is tried after
    the walk, so it can only ever add a place to look and never change an answer the walk
    already gave.
    """
    candidates = [parent / "alembic" for parent in Path(__file__).resolve().parents]
    root = install_root()
    if root is not None:
        candidates.append(root / "alembic")
    for candidate in candidates:
        if (candidate / "env.py").is_file():
            return candidate
    raise SchemaRefusedError(MIGRATIONS_UNREADABLE)


def _script() -> Any:
    """Alembic's reader for the shipped ``alembic/`` directory.

    Built the same way at all three call sites below, so the construction logic lives in
    one place. Raises whatever Alembic raises; each caller decides what an unreadable
    script directory means for the question it is answering.
    """
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    config = Config()
    config.set_main_option("script_location", str(alembic_dir()))
    return ScriptDirectory.from_config(config)


def known_revisions() -> frozenset[str]:
    """Every Alembic revision id this build ships, from the migration scripts.

    The migration modules import only Alembic and SQLAlchemy, so walking them has no side
    effects. This is the one declaration of "what schemas can this build serve": the boot
    gate below and :mod:`reaper.services.restore`'s backup gate both read it, and a second
    copy of the set would be a second answer that could drift from this one.
    """
    try:
        return frozenset(revision.revision for revision in _script().walk_revisions())
    except Exception as exc:  # unreadable migrations are unanswerable, never "fine"
        raise SchemaRefusedError(MIGRATIONS_UNREADABLE) from exc


def current_head() -> str | None:
    """The newest revision this build ships, or ``None`` when it cannot be read.

    Cosmetic, and only the restore summary consults it: a miss there reads as "older",
    which is the conservative side. Nothing that refuses anything reads this.
    """
    try:
        head: str | None = _script().get_current_head()
        return head
    except Exception:
        return None


#: The module-level attribute a revision sets to ask for a snapshot of the database before
#: it runs: ``needs_snapshot = True``, beside ``revision`` and ``down_revision``.
#:
#: It lives on the revision because the revision is the only thing that knows. A revision
#: that cannot be undone by its own ``downgrade()`` sets it: a ``drop_column`` that takes
#: the data with it, or any batch rebuild, where the table is copied from SQLite's
#: reflection and a constraint that reflection does not report is lost on the way through.
#: An ordinary ``ADD COLUMN`` sets nothing and costs nothing.
SNAPSHOT_ATTR = "needs_snapshot"


def needs_snapshot(revision: str | None) -> bool:
    """Whether anything between ``revision`` and head asks to be snapshotted first.

    ``revision`` is what the database on disk sits at, and ``None`` answers ``False`` here.

    That is only correct for two of the three things :func:`stored_revision` answers
    ``None`` for. No file and no ``alembic_version`` row are both "nothing to lose." The
    third is a database it could not read, and an unreadable database is an ambiguity, not
    an answer. Deciding that case needs the file itself, which this function is not given,
    so :mod:`reaper.preflight` decides it at the call site and snapshots a database that
    exists but would not say what it is.

    Every ambiguity this function can see answers ``True``. A script directory that will
    not read, a revision that is not an ancestor of head, a revision module that raises on
    import: none of these means "no snapshot needed," and taking a snapshot that was not
    wanted only costs a file. Reading a revision's module runs it, which has no side
    effects for the same reason :func:`known_revisions` walking them does not.
    """
    if revision is None:
        return False
    try:
        script = _script()
        head = script.get_current_head()
        if head is None:
            return True
        return any(
            getattr(pending.module, SNAPSHOT_ATTR, False)
            for pending in script.iterate_revisions(head, revision)
        )
    except Exception:
        return True


def stored_revision(db_path: Path) -> str | None:
    """The revision a database file sits at, without creating one that is not there.

    ``sqlite3.connect`` creates the file it is pointed at, so checking existence first
    matters, not just for tidiness: this runs before migrations, and on a first boot
    ``reaper.db`` does not exist yet. Conjuring an empty one would leave Alembic migrating
    a file this function invented.

    ``None`` covers everything that means "nothing to judge": no file, no
    ``alembic_version`` table (a database built straight from the models, which is what a
    test carries), and an empty one. A file that is not a readable database also reads as
    ``None``, and that is not the fail-open hole it looks like: an unreadable file is not
    a schema this build is behind, and SQLite refuses it loudly a moment later when
    migrations or the app open it for real.
    """
    if not db_path.is_file():
        return None
    con = sqlite3.connect(db_path)
    try:
        row = con.execute("SELECT version_num FROM alembic_version").fetchone()
    except sqlite3.Error:
        return None
    finally:
        con.close()
    return str(row[0]) if row and row[0] else None


def refusal(revision: str | None) -> str | None:
    """The operator sentence when this build must not serve ``revision``, else ``None``.

    One verdict with two presentations: preflight prints it and stops the boot, the app's
    lifespan logs it and raises. Never softened into a warning anywhere, because the
    process must not go on to write rows into a schema it does not know.
    """
    if revision is None:
        return None
    try:
        known = known_revisions()
    except SchemaRefusedError as exc:
        return str(exc)
    if revision in known:
        return None
    return DATABASE_IS_NEWER
