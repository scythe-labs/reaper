# SPDX-License-Identifier: AGPL-3.0-or-later
"""Migration tests.

These exist because the two settings they exercise -- the metadata naming
convention and Alembic's ``render_as_batch`` -- cannot be added later. If a
future schema change is the first thing to discover they were misconfigured, the
fix is rewriting the entire migration history.
"""

from __future__ import annotations

import importlib.util
import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from alembic import (
    command,
    op,  # noqa: F401  # imported so op.f is resolvable in migrations
)
from alembic.config import Config
from alembic.runtime.environment import EnvironmentContext
from alembic.script import ScriptDirectory
from sqlalchemy import (
    Column,
    ForeignKey,
    Integer,
    MetaData,
    String,
    Table,
    UniqueConstraint,
    create_engine,
    inspect,
    text,
)
from sqlalchemy.engine import Engine

from reaper.config import Settings
from reaper.db.base import NAMING_CONVENTION
from reaper.engine.gates import GateId
from reaper.engine.policy import DEFAULT_MOVIE_POLICY, PolicyBody, recover_rating_rules

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# The revision just before the size_bytes-nullability heal, and the heal itself. A database
# built from the earlier baseline (size_bytes NOT NULL, held_back_unknown_size DEFAULT 0) sits
# at the former; upgrading it must reach the latter with the columns reshaped and rows intact.
_PRIOR_HEAD = "6f708192a3b4"
_HEAL_HEAD = "708192a3b4c5"


@pytest.fixture
def sqlite_engine(tmp_path: Path) -> Iterator[Engine]:
    engine = create_engine(f"sqlite:///{tmp_path / 'test.db'}")
    yield engine
    engine.dispose()


def test_exactly_one_migration_head() -> None:
    """A branched history is a merge conflict waiting to corrupt someone's database."""
    script = ScriptDirectory.from_config(Config(str(PROJECT_ROOT / "alembic.ini")))
    heads = script.get_heads()
    assert len(heads) == 1, f"Expected a single head, found {heads}"


def test_batch_mode_can_drop_a_named_constraint(sqlite_engine: Engine) -> None:
    """The point of the whole arrangement.

    SQLite cannot ``ALTER TABLE ... DROP CONSTRAINT``. Alembic's batch mode
    rebuilds the table instead -- but it can only drop a constraint it can *name*,
    and SQLite auto-generates anonymous names. The naming convention is what makes
    the name predictable, and this test proves the two work together.
    """
    from alembic.migration import MigrationContext
    from alembic.operations import Operations

    md = MetaData(naming_convention=NAMING_CONVENTION)
    table = Table(
        "thing",
        md,
        Column("id", Integer, primary_key=True),
        Column("code", String(20)),
        UniqueConstraint("code"),  # anonymous -- the convention names it
    )
    md.create_all(sqlite_engine)

    with sqlite_engine.connect() as conn:
        ctx = MigrationContext.configure(conn, opts={"as_batch": True})
        ops = Operations(ctx)

        # Without the convention this raises ValueError: Constraint must have a name
        with ops.batch_alter_table("thing", copy_from=table) as batch:
            batch.drop_constraint("uq_thing_code", type_="unique")

        conn.commit()

        # The constraint is gone: the duplicate below would previously have failed.
        conn.exec_driver_sql("INSERT INTO thing (id, code) VALUES (1, 'dup')")
        conn.exec_driver_sql("INSERT INTO thing (id, code) VALUES (2, 'dup')")
        conn.commit()

        count = conn.exec_driver_sql("SELECT COUNT(*) FROM thing").scalar_one()
        assert count == 2


class _ConfigureCalled(Exception):  # noqa: N818 -- a control-flow signal, not an error
    """Carries env.py's real ``context.configure()`` keyword arguments back to the test."""

    def __init__(self, kwargs: dict[str, Any]) -> None:
        super().__init__("env.py called context.configure()")
        self.kwargs = kwargs


def _env_py_configure_kwargs(
    *, as_sql: bool, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> dict[str, Any]:
    """Run the real ``alembic/env.py`` and return what it passed to ``context.configure()``.

    env.py cannot be imported -- Alembic execs it by path -- so this drives it the
    way Alembic itself does and intercepts the one call that matters. ``as_sql``
    picks the branch: True runs ``run_migrations_offline``, False the online one.

    Nothing is migrated: ``configure`` raises as soon as it has the keyword
    arguments, which also means a future env.py that never calls it fails here
    rather than passing on an empty result.
    """
    settings = Settings(data_dir=tmp_path, secret_key="k")  # type: ignore[call-arg]
    monkeypatch.setattr("reaper.config.get_settings", lambda: settings)

    # env.py replays alembic.ini's logging config, and fileConfig() disables every
    # logger it does not name -- for the rest of the session, not just this test.
    monkeypatch.setattr("logging.config.fileConfig", lambda *a, **kw: None)

    def _capture(**kwargs: Any) -> None:
        raise _ConfigureCalled(kwargs)

    # env.py holds the alembic.context *module*, so it resolves configure at call time.
    monkeypatch.setattr("alembic.context.configure", _capture)

    config = Config(str(PROJECT_ROOT / "alembic.ini"))
    script = ScriptDirectory.from_config(config)
    with (
        EnvironmentContext(config, script, as_sql=as_sql),
        pytest.raises(_ConfigureCalled) as caught,
    ):
        script.run_env()
    return caught.value.kwargs


@pytest.mark.parametrize("as_sql", [True, False], ids=["offline", "online"])
def test_env_py_configures_batch_mode(
    as_sql: bool, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """What alembic/env.py actually passes -- not what a test hand-configures.

    ``test_batch_mode_can_drop_a_named_constraint`` proves batch mode plus the
    naming convention can drop a constraint, but it configures batch mode itself.
    This one reads the shipped env.py, so flipping ``render_as_batch`` to False
    fails here instead of years later, in the first migration that needs it.

    Both call sites are covered: env.py configures once per branch, and only the
    branch under test runs.
    """
    kwargs = _env_py_configure_kwargs(as_sql=as_sql, tmp_path=tmp_path, monkeypatch=monkeypatch)

    assert kwargs.get("render_as_batch") is True, (
        "alembic/env.py must pass render_as_batch=True. Without it SQLite cannot "
        "drop a constraint, and the fix is rewriting the migration history."
    )
    # The other half of the pair: the convention only helps if the metadata carrying
    # it is the metadata env.py hands to Alembic.
    assert kwargs["target_metadata"].naming_convention == NAMING_CONVENTION


def _alembic_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Config:
    """An alembic Config whose env.py resolves the DB URL to ``tmp_path/reaper.db``."""
    settings = Settings(data_dir=tmp_path, secret_key="k")  # type: ignore[call-arg]
    monkeypatch.setattr("reaper.config.get_settings", lambda: settings)
    monkeypatch.setattr("logging.config.fileConfig", lambda *a, **kw: None)
    return Config(str(PROJECT_ROOT / "alembic.ini"))


def _size_bytes_nullable(engine: Engine) -> bool:
    col = next(c for c in inspect(engine).get_columns("candidate") if c["name"] == "size_bytes")
    return bool(col["nullable"])


def _held_back_default(engine: Engine) -> object:
    col = next(
        c for c in inspect(engine).get_columns("reap_run") if c["name"] == "held_back_unknown_size"
    )
    return col["default"]


def _instance_has_import_exclusion(engine: Engine) -> bool:
    return any(c["name"] == "add_import_exclusion" for c in inspect(engine).get_columns("instance"))


# The add_import_exclusion migration and the revision just before it. A database created fresh
# during the ~30-minute window when this column briefly lived in the frozen baseline already
# carries it, so the additive migration's add_column must be guarded, not plain (B-8, rule 81).
_BEFORE_IMPORT_EXCLUSION = "1f2a3b4c5d6e"
_IMPORT_EXCLUSION = "2b3c4d5e6f70"


def test_add_import_exclusion_upgrades_an_in_window_database(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A database that already has the column upgrades instead of boot-looping.

    Build the schema up to the revision before the add, then simulate the in-window database by
    adding ``add_import_exclusion`` by hand -- exactly the shape a fresh install got during the
    ~30 minutes the column lived in the baseline. Upgrading to head must skip the add (the
    reflection guard) rather than raise "duplicate column name", which would refuse every boot.
    """
    config = _alembic_config(tmp_path, monkeypatch)
    command.upgrade(config, _BEFORE_IMPORT_EXCLUSION)
    engine = create_engine(f"sqlite:///{tmp_path / 'reaper.db'}")

    assert _instance_has_import_exclusion(engine) is False  # the reverted baseline lacks it
    with engine.begin() as conn:
        conn.execute(
            text("ALTER TABLE instance ADD COLUMN add_import_exclusion BOOLEAN NOT NULL DEFAULT 0")
        )
    assert _instance_has_import_exclusion(engine) is True  # the in-window shape

    # Upgrading over the already-present column must not raise; the guard skips the add.
    command.upgrade(config, "head")
    assert _instance_has_import_exclusion(engine) is True

    engine.dispose()


def test_add_import_exclusion_still_adds_the_column_when_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The normal path is untouched: a database without the column still gets it."""
    config = _alembic_config(tmp_path, monkeypatch)
    command.upgrade(config, _BEFORE_IMPORT_EXCLUSION)
    engine = create_engine(f"sqlite:///{tmp_path / 'reaper.db'}")
    assert _instance_has_import_exclusion(engine) is False

    command.upgrade(config, _IMPORT_EXCLUSION)
    assert _instance_has_import_exclusion(engine) is True

    engine.dispose()


def test_heal_migration_relaxes_old_not_null_size_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The heal reshapes a database built from the pre-freeze baseline, preserving its rows.

    A database created before the baseline was corrected has ``candidate.size_bytes`` NOT NULL
    and ``reap_run.held_back_unknown_size`` DEFAULT 0, and no ALTER ever ran to fix it. Stamp
    such a shape at the prior head, upgrade, and both columns must reach the model shape while
    the existing row survives and a NULL size (an unknown-size item) now inserts.
    """
    config = _alembic_config(tmp_path, monkeypatch)
    engine = create_engine(f"sqlite:///{tmp_path / 'reaper.db'}")

    md = MetaData(naming_convention=NAMING_CONVENTION)
    Table("snapshot", md, Column("id", Integer, primary_key=True))
    Table(
        "candidate",
        md,
        Column("id", Integer, primary_key=True),
        Column(
            "snapshot_id", Integer, ForeignKey("snapshot.id", ondelete="CASCADE"), nullable=False
        ),
        Column("media_key", String(100), nullable=False),
        Column("size_bytes", Integer, nullable=False),  # the old, un-healed shape
        UniqueConstraint("snapshot_id", "media_key"),
    )
    Table(
        "reap_run",
        md,
        Column("id", Integer, primary_key=True),
        Column(
            "snapshot_id", Integer, ForeignKey("snapshot.id", ondelete="RESTRICT"), nullable=False
        ),
        Column("held_back_unknown_size", Integer, nullable=False, server_default="0"),  # old shape
    )
    md.create_all(engine)
    with engine.begin() as conn:
        conn.execute(text("INSERT INTO snapshot (id) VALUES (1)"))
        conn.execute(
            text(
                "INSERT INTO candidate (id, snapshot_id, media_key, size_bytes) VALUES "
                "(1, 1, 'k1', 123)"
            )
        )
        conn.execute(text("INSERT INTO reap_run (id, snapshot_id) VALUES (1, 1)"))

    assert _size_bytes_nullable(engine) is False  # precondition: the broken shape
    assert _held_back_default(engine) is not None

    command.stamp(config, _PRIOR_HEAD)
    # To the heal, not to head: this database is hand-built with only the two tables the
    # heal touches, so a later additive migration against some other table would fail here
    # for a reason that has nothing to do with what is being tested.
    command.upgrade(config, _HEAL_HEAD)

    # Healed to the model shape.
    assert _size_bytes_nullable(engine) is True
    assert _held_back_default(engine) is None
    # The existing row survived the table copy unchanged.
    with engine.connect() as conn:
        assert conn.execute(text("SELECT size_bytes FROM candidate WHERE id = 1")).scalar() == 123
        # A NULL size (an item Radarr could not size) now inserts, where before it raised.
        conn.execute(
            text(
                "INSERT INTO candidate (id, snapshot_id, media_key, size_bytes) VALUES "
                "(2, 1, 'k2', NULL)"
            )
        )
        conn.commit()
        assert conn.execute(text("SELECT COUNT(*) FROM candidate")).scalar() == 2

    engine.dispose()


def test_heal_migration_is_noop_on_corrected_schema(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """On a database already at the corrected shape, the heal changes nothing and never rebuilds.

    A fresh install reaches ``_PRIOR_HEAD`` with size_bytes already nullable, so both guards in
    the migration are false and it is a pure version bump. The upgrade must succeed and leave the
    model shape in place (a rebuild that dropped data or a guard that fired would fail here).
    """
    config = _alembic_config(tmp_path, monkeypatch)
    command.upgrade(config, _PRIOR_HEAD)
    engine = create_engine(f"sqlite:///{tmp_path / 'reaper.db'}")

    assert _size_bytes_nullable(engine) is True  # the baseline already ships the corrected shape
    assert _held_back_default(engine) is None

    command.upgrade(config, "head")

    assert _size_bytes_nullable(engine) is True
    assert _held_back_default(engine) is None
    with engine.connect() as conn:
        version = conn.execute(text("SELECT version_num FROM alembic_version")).scalar()
    # Whatever head is today -- read from the script directory, never a second copy of the
    # revision id that has to be hand-edited every time a migration lands.
    script = ScriptDirectory.from_config(Config(str(PROJECT_ROOT / "alembic.ini")))
    assert version == script.get_current_head()

    engine.dispose()


# --- the dead vote floor leaving stored policy bodies (issue #266) -------------------------

#: The revision that retires the gate row's dead vote floor, and the one before it.
_PRIOR_SECONDARY = "d5e6f708192a"
_SECONDARY = "e6f708192a3b"


def _migration_module() -> Any:
    """The revision's own module, loaded from the file.

    Alembic versions are not importable as a package, and the point of these tests is to
    exercise the code that will actually run on an operator's database, not a copy of it.
    """
    path = PROJECT_ROOT / "alembic" / "versions" / "20260730_1200_retire_policy_gate_secondary.py"
    spec = importlib.util.spec_from_file_location("_retire_secondary", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _legacy_body(*, recoverable: bool) -> dict[str, Any]:
    """A stored body of the shape every install seeded before the rating bar moved.

    ``recoverable=True`` withholds ``keep_rating_rules``, which is what makes the gate's
    ``secondary`` the last surviving copy of the operator's vote floor.
    """
    raw: dict[str, Any] = json.loads(DEFAULT_MOVIE_POLICY.model_dump_json())
    for gate in raw["gates"]:
        gate["secondary"] = 0
        if gate["gate"] == GateId.RATING_FLOOR.value:
            gate["enabled"], gate["threshold"], gate["secondary"] = True, 75, 1000
    if recoverable:
        del raw["keep_rating_rules"]
    else:
        raw["keep_rating_rules"] = [{"source": "imdb", "floor": 75, "min_votes": 1000}]
    return raw


def _seed_policy(engine: Engine, body: dict[str, Any], *, name: str = "default") -> None:
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO policy (policy_hash, body_json, media_type, name, created_at)"
                " VALUES (:h, :b, 'movie', :n, 1750000000)"
            ),
            {"h": "seeded-hash", "b": json.dumps(body), "n": name},
        )


def _policy_rows(engine: Engine) -> list[tuple[int, str, str]]:
    with engine.begin() as conn:
        return [
            (int(r[0]), str(r[1]), str(r[2]))
            for r in conn.execute(text("SELECT id, policy_hash, body_json FROM policy ORDER BY id"))
        ]


def test_the_secondary_migration_retires_an_inert_number(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A body whose vote floor is dead gets a fresh row without it, and the old row survives.

    The append is the whole design: rewriting in place would leave every approval and audit
    entry pointing at a ``policy_hash`` its own row no longer produces (``db.models.Policy``
    is append-only by contract). So the parent row must come through byte-identical.
    """
    config = _alembic_config(tmp_path, monkeypatch)
    command.upgrade(config, _PRIOR_SECONDARY)
    engine = create_engine(f"sqlite:///{tmp_path / 'reaper.db'}")
    _seed_policy(engine, _legacy_body(recoverable=False))
    before = _policy_rows(engine)

    command.upgrade(config, _SECONDARY)

    after = _policy_rows(engine)
    assert after[0] == before[0], "the parent row was edited; it must be left exactly as saved"
    assert len(after) == len(before) + 1
    _, new_hash, new_body = after[-1]
    assert all("secondary" not in g for g in json.loads(new_body)["gates"])
    # It loads into the model the field was removed from.
    assert PolicyBody.model_validate_json(new_body)
    # And the row's own hash describes its own content, so nothing reads as stale on arrival.
    assert new_hash == PolicyBody.model_validate_json(new_body).policy_hash()
    assert new_hash != before[0][1]

    engine.dispose()


def test_the_secondary_migration_will_not_touch_a_bar_it_would_destroy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A body still carrying a recoverable rating bar is left completely alone.

    ``secondary`` is that operator's vote floor and nothing else records it. Stripping it here
    would delete a protection; synthesizing the bar here would persist a safety value they
    never approved, with no flag, no degraded scan and no editor draft -- the silent
    substitution rules 65 and 105 exist to forbid. So the row keeps the key, and
    ``recover_rating_rules`` keeps putting the bar back at load time, which is where the
    operator is told about it.
    """
    config = _alembic_config(tmp_path, monkeypatch)
    command.upgrade(config, _PRIOR_SECONDARY)
    engine = create_engine(f"sqlite:///{tmp_path / 'reaper.db'}")
    _seed_policy(engine, _legacy_body(recoverable=True))
    before = _policy_rows(engine)

    command.upgrade(config, _SECONDARY)

    assert _policy_rows(engine) == before, "a recoverable bar was migrated out from under the shim"
    # And the shim still finds it, so the protection is genuinely still reachable.
    restored = recover_rating_rules(json.loads(before[0][2]))
    assert restored is not None
    assert restored["keep_rating_rules"] == [{"source": "imdb", "floor": 75, "min_votes": 1000}]
    # What the shim hands back drops the retired key, so it loads despite the stored row keeping it.
    assert PolicyBody.model_validate(restored)

    engine.dispose()


def test_the_secondary_migration_is_a_noop_on_a_body_without_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every fresh install is this case, and re-running must not append a row each time."""
    config = _alembic_config(tmp_path, monkeypatch)
    command.upgrade(config, _PRIOR_SECONDARY)
    engine = create_engine(f"sqlite:///{tmp_path / 'reaper.db'}")
    clean = json.loads(DEFAULT_MOVIE_POLICY.model_dump_json())
    _seed_policy(engine, clean)
    before = _policy_rows(engine)

    command.upgrade(config, _SECONDARY)

    assert _policy_rows(engine) == before

    engine.dispose()


def test_the_migration_reads_a_recoverable_bar_exactly_as_the_shim_does() -> None:
    """The migration's copy of the trigger agrees with ``recover_rating_rules``, case for case.

    The migration cannot import the shim -- a revision must mean the same thing forever, and an
    import would let a later edit change what an old migration did -- so the predicate is a hand
    copy, and a hand copy needs a drift guard (rules 103, 144). The table is written from the
    shim's stated contract, not transcribed from its branches (rule 119): each row is a reason a
    bar is or is not the last copy of something.
    """
    recoverable = _migration_module().bar_is_still_recoverable
    base = _legacy_body(recoverable=True)

    def variant(**gate_patch: Any) -> dict[str, Any]:
        body = json.loads(json.dumps(base))
        for gate in body["gates"]:
            if gate["gate"] == GateId.RATING_FLOOR.value:
                gate.update(gate_patch)
        return body

    cases: list[tuple[str, object]] = [
        ("the shape every pre-move install carries", base),
        (
            "an operator who cleared their bars keeps an empty set",
            {**base, "keep_rating_rules": []},
        ),
        ("already moved", {**base, "keep_rating_rules": [{"s": 1}]}),
        ("a disabled gate protected nothing either way", variant(enabled=False)),
        ("no votes required is not a bar", variant(secondary=0)),
        ("a floor of zero is not a bar", variant(threshold=0)),
        ("a floor past the scale is not a bar", variant(threshold=101)),
        ("true is not one vote", variant(secondary=True)),
        ("true is not a floor", variant(threshold=True)),
        ("a string is not a floor", variant(threshold="75")),
        ("nothing at all", variant(threshold=None, secondary=None)),
        ("not a body", "policy"),
        ("gates are not a list", {**base, "gates": {}}),
    ]

    disagreed = [
        f"{why}: migration says {recoverable(body)}, shim says {shim}"
        for why, body in cases
        if recoverable(body) is not (shim := recover_rating_rules(body) is not None)
    ]
    assert not disagreed, "the migration's copy of the trigger has drifted:\n" + "\n".join(
        disagreed
    )
