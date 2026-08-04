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
from datetime import datetime
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
    select,
    text,
)
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from reaper.config import Settings
from reaper.db.base import NAMING_CONVENTION
from reaper.db.models import ListConfig
from reaper.db.models import Policy as PolicyModel
from reaper.engine.gates import GateId
from reaper.engine.policy import (
    DEFAULT_MOVIE_POLICY,
    PolicyBody,
    has_legacy_list_protections,
    recover_rating_rules,
)

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


def test_the_seeded_keep_collection_is_readable_through_the_orm(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The seeded "Never Reap" definition loads, rather than 500ing every read of the table.

    ``list_config.created_at`` is an ``EpochDateTime``: an INTEGER unix timestamp whose read
    side calls ``datetime.fromtimestamp`` on whatever is stored. A raw ``INSERT`` binds a
    datetime around that type and lands an ISO string, which SQLite stores happily and the ORM
    then raises ``TypeError: 'str' object cannot be interpreted as an integer`` on -- so the
    Lists screen sat on "Loading your lists…" forever and the route 500ed, on the first page
    load after upgrading. Found by driving a real install, which is the only place the two
    writers meet: the shipped list's row goes through the ORM and was fine, so the mismatch
    needed a database holding a row from each.

    This asserts the READ, not the stored shape, because the read is what broke.
    """
    config = _alembic_config(tmp_path, monkeypatch)
    command.upgrade(config, "head")
    engine = create_engine(f"sqlite:///{tmp_path / 'reaper.db'}")

    with Session(engine) as session:
        rows = session.execute(select(ListConfig)).scalars().all()

    # The Arr-style migration behind the seed adds the tag list AND the IMDb list beside
    # it: it sets the seeded flag, so it must leave every list the default policy's keep
    # rules name, or a fresh install boots with a rule naming a list that does not exist
    # (found by driving one). See TestTheArrStyleListsMigration for the rest.
    assert [r.name for r in rows] == ["Never Reap", "Titles you've tagged", "IMDb Top 250"]
    assert isinstance(rows[0].created_at, datetime)
    assert rows[0].source == "plex_collection"
    # The library it points at is the one the code used to hardcode, so the first scan after
    # an upgrade reads exactly the collection the operator already had (#483 is the screen
    # that lets them change it, not a silent re-pointing).
    assert json.loads(rows[0].config_json) == {"library": "Movies", "collection": "Never Reap"}
    assert isinstance(rows[1].created_at, datetime)

    engine.dispose()


# The Arr-style lists migration and the revision just before it (the "Never Reap" seed).
_PRIOR_ARR_STYLE = "b2c3d4e5f6a7"
_ARR_STYLE = "c3d4e5f6a7b8"


def _list_rows(engine: Engine) -> list[tuple[str, str, str, int, int]]:
    with engine.begin() as conn:
        return [
            (str(r[0]), str(r[1]), str(r[2]), int(r[3]), int(r[4]))
            for r in conn.execute(
                text("SELECT name, source, config_json, enabled, built_in FROM list_config")
            )
        ]


def _lists_seeded_flag(engine: Engine) -> str | None:
    with engine.begin() as conn:
        row = conn.execute(
            text("SELECT value_json FROM app_setting WHERE key = 'lists_seeded'")
        ).one_or_none()
    return None if row is None else str(row[0])


class TestTheArrStyleListsMigration:
    """Every list is Arr-style: 'curated' rows respell as 'imdb', the policy keep tags
    become a tag list on Settings -> Lists, and the seed flag is set so ``ensure_defaults``
    never adds a second shipped copy beside an upgraded install's own rows."""

    def _upgraded(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        *,
        seed: Any = None,
    ) -> Engine:
        config = _alembic_config(tmp_path, monkeypatch)
        command.upgrade(config, _PRIOR_ARR_STYLE)
        engine = create_engine(f"sqlite:///{tmp_path / 'reaper.db'}")
        if seed is not None:
            seed(engine)
        command.upgrade(config, _ARR_STYLE)
        return engine

    @staticmethod
    def _seed_curated_row(engine: Engine) -> None:
        with engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO list_config "
                    "(name, source, config_json, enabled, built_in, created_at) "
                    "VALUES ('IMDb Top 250', 'curated', :config, 0, 1, 1750000000)"
                ),
                {"config": json.dumps({"list": "imdb-top-250"})},
            )

    def test_a_curated_row_respells_as_imdb_and_keeps_its_name(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        engine = self._upgraded(tmp_path, monkeypatch, seed=self._seed_curated_row)

        rows = {name: (source, config) for name, source, config, _, _ in _list_rows(engine)}

        assert rows["IMDb Top 250"][0] == "imdb"
        assert json.loads(rows["IMDb Top 250"][1]) == {"preset": "top250"}
        engine.dispose()

    def test_every_row_comes_out_enabled_and_not_built_in(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The Protecting switch and the built-in lock left the UI, so a disabled or
        locked row would render with no control that can change it."""
        engine = self._upgraded(tmp_path, monkeypatch, seed=self._seed_curated_row)

        for name, _source, _config, enabled, built_in in _list_rows(engine):
            assert enabled == 1, name
            assert built_in == 0, name
        engine.dispose()

    def test_the_stored_keep_tags_become_the_tag_list(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The operator's own tags, both media types unioned, ``all`` only when every body
        that spoke said ``all`` -- ``any`` is the wider net, which is the keep direction."""

        def seed(engine: Engine) -> None:
            movie = json.loads(DEFAULT_MOVIE_POLICY.model_dump_json())
            movie["keep_tags"] = ["Keep-This", "gold"]
            movie["keep_tags_match"] = "all"
            tv = json.loads(DEFAULT_MOVIE_POLICY.model_dump_json())
            tv["keep_tags"] = ["gold", "silver"]
            tv["keep_tags_match"] = "any"
            with engine.begin() as conn:
                conn.execute(
                    text(
                        "INSERT INTO policy (policy_hash, body_json, media_type, name, created_at)"
                        " VALUES (:h, :b, :mt, 'default', 1750000000)"
                    ),
                    [
                        {"h": "h-movie", "b": json.dumps(movie), "mt": "movie"},
                        {"h": "h-tv", "b": json.dumps(tv), "mt": "tv"},
                    ],
                )

        engine = self._upgraded(tmp_path, monkeypatch, seed=seed)

        rows = {
            name: config for name, source, config, _, _ in _list_rows(engine) if source == "arr_tag"
        }
        assert json.loads(rows["Titles you've tagged"]) == {
            "tags": ["Keep-This", "gold", "silver"],
            "match": "any",
        }
        assert _lists_seeded_flag(engine) == "true"
        engine.dispose()

    def test_a_body_with_no_keep_tags_key_seeds_the_shipped_default_tag(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A body that carries no key ran on the shipped default tag, so it had a live
        protection to carry over; seeding nothing would withdraw it."""

        def seed(engine: Engine) -> None:
            _seed_policy(engine, json.loads(DEFAULT_MOVIE_POLICY.model_dump_json()))

        engine = self._upgraded(tmp_path, monkeypatch, seed=seed)

        rows = {
            name: config for name, source, config, _, _ in _list_rows(engine) if source == "arr_tag"
        }
        assert json.loads(rows["Titles you've tagged"]) == {
            "tags": ["reaper-keep"],
            "match": "any",
        }
        engine.dispose()

    def test_an_existing_tag_list_is_not_duplicated(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An install from this branch's earlier builds already defined its tag list;
        seeding a second would make one protection two rows with two names."""

        def seed(engine: Engine) -> None:
            with engine.begin() as conn:
                conn.execute(
                    text(
                        "INSERT INTO list_config "
                        "(name, source, config_json, enabled, built_in, created_at) "
                        "VALUES ('Mine', 'arr_tag', :config, 1, 0, 1750000000)"
                    ),
                    {"config": json.dumps({"tags": ["mine"], "match": "any"})},
                )

        engine = self._upgraded(tmp_path, monkeypatch, seed=seed)

        tag_rows = [r for r in _list_rows(engine) if r[1] == "arr_tag"]
        assert [r[0] for r in tag_rows] == ["Mine"]
        assert _lists_seeded_flag(engine) == "true"
        engine.dispose()

    def test_the_flag_is_set_so_ensure_defaults_stands_down(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Any install with state to carry gets the flag; the first read after the upgrade
        must keep the operator's rows as the whole registry rather than add shipped copies."""
        engine = self._upgraded(tmp_path, monkeypatch, seed=self._seed_curated_row)

        assert _lists_seeded_flag(engine) == "true"
        engine.dispose()


# The list_config shape heal and the revision just before it.
_ARR_STYLE_HEAD = "c3d4e5f6a7b8"
_LIST_CONFIG_HEAL = "d4e5f6a7b8c9"


class TestTheListConfigShapeHeal:
    """``add_list_config`` first created ``created_at`` as DATETIME with server defaults on
    three columns; the migration is corrected in place, so only a database created in that
    window still carries the old shape. The heal rebuilds it to the model's, keeping rows."""

    @staticmethod
    def _regress_to_the_old_shape(engine: Engine) -> None:
        """The table exactly as the earlier spelling created it, with one stored row."""
        with engine.begin() as conn:
            conn.execute(text("DROP TABLE list_config"))
            conn.execute(
                text(
                    "CREATE TABLE list_config ("
                    " id INTEGER NOT NULL PRIMARY KEY,"
                    " name VARCHAR(100) NOT NULL,"
                    " source VARCHAR(32) NOT NULL,"
                    " config_json TEXT NOT NULL DEFAULT '{}',"
                    " enabled BOOLEAN NOT NULL DEFAULT 1,"
                    " built_in BOOLEAN NOT NULL DEFAULT 0,"
                    " created_at DATETIME NOT NULL,"
                    " CONSTRAINT uq_list_config_name UNIQUE (name))"
                )
            )
            conn.execute(
                text(
                    "INSERT INTO list_config "
                    "(name, source, config_json, enabled, built_in, created_at) "
                    "VALUES ('Mine', 'arr_tag', :config, 1, 0, 1750000000)"
                ),
                {"config": json.dumps({"tags": ["keep"], "match": "any"})},
            )

    def test_an_in_window_database_is_reshaped_and_its_rows_survive(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        config = _alembic_config(tmp_path, monkeypatch)
        command.upgrade(config, _ARR_STYLE_HEAD)
        engine = create_engine(f"sqlite:///{tmp_path / 'reaper.db'}")
        self._regress_to_the_old_shape(engine)

        command.upgrade(config, _LIST_CONFIG_HEAL)

        columns = {c["name"]: c for c in inspect(engine).get_columns("list_config")}
        assert isinstance(columns["created_at"]["type"], Integer)
        for name in ("config_json", "enabled", "built_in"):
            assert columns[name]["default"] is None, name
        # The row came through, and the ORM reads it: the whole reason the type matters.
        with Session(engine) as session:
            [row] = session.execute(select(ListConfig)).scalars().all()
        assert row.name == "Mine"
        assert isinstance(row.created_at, datetime)
        engine.dispose()

    def test_a_corrected_database_is_left_untouched(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Every fresh install takes the guard's other arm: nothing to rebuild, and the
        shape at head is already the model's -- which is also what keeps ``alembic check``
        green in CI."""
        config = _alembic_config(tmp_path, monkeypatch)
        command.upgrade(config, "head")
        engine = create_engine(f"sqlite:///{tmp_path / 'reaper.db'}")

        columns = {c["name"]: c for c in inspect(engine).get_columns("list_config")}
        assert isinstance(columns["created_at"]["type"], Integer)
        for name in ("config_json", "enabled", "built_in"):
            assert columns[name]["default"] is None, name
        engine.dispose()


# The case-insensitive name constraint and the revision just before it.
_NAME_NOCASE = "e5f6a7b8c9d0"


class TestAListNameIsUniqueWithoutRegardToCase:
    """``list_config.name`` was unique byte for byte while every reader case-folds it, so two
    rows differing only in case answered to one keep rule: the second never got a rule of its
    own, and deleting either one took that rule away and stopped the other protecting (#508).

    The collation is what makes the stored constraint compare the way the code does. A
    database that already holds a collision has to upgrade rather than refuse to boot, which
    is the whole reason the disambiguation runs before the rebuild.

    Every name here is one the migrations do not seed, so what these assert on is what the
    test put there.
    """

    @staticmethod
    def _added(engine: Engine) -> list[str]:
        """The names this test added, in insert order. The seeded definitions are skipped by
        their id: every row here is inserted with the fixed stamp below."""
        with engine.begin() as conn:
            return [
                str(r.name)
                for r in conn.execute(
                    text("SELECT name FROM list_config WHERE created_at = 1750000000 ORDER BY id")
                )
            ]

    @staticmethod
    def _add(engine: Engine, *names: str) -> None:
        with engine.begin() as conn:
            for name in names:
                conn.execute(
                    text(
                        "INSERT INTO list_config "
                        "(name, source, config_json, enabled, built_in, created_at) "
                        "VALUES (:name, 'arr_tag', :config, 1, 0, 1750000000)"
                    ),
                    {"name": name, "config": json.dumps({"tags": ["keep"], "match": "any"})},
                )

    def test_the_constraint_refuses_a_name_differing_only_in_case(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        config = _alembic_config(tmp_path, monkeypatch)
        command.upgrade(config, "head")
        engine = create_engine(f"sqlite:///{tmp_path / 'reaper.db'}")

        self._add(engine, "Keepers")
        with pytest.raises(IntegrityError):
            self._add(engine, "keepers")

        engine.dispose()

    def test_a_database_already_holding_a_collision_upgrades(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Both rows survive, the older keeps its spelling, and the constraint lands. Refusing
        the upgrade instead would leave the operator unable to boot at all, which is a worse
        answer than a list that has to be renamed."""
        config = _alembic_config(tmp_path, monkeypatch)
        command.upgrade(config, _LIST_CONFIG_HEAL)
        engine = create_engine(f"sqlite:///{tmp_path / 'reaper.db'}")
        self._add(engine, "Keepers", "keepers", "KEEPERS")

        command.upgrade(config, _NAME_NOCASE)

        assert self._added(engine) == ["Keepers", "keepers (2)", "KEEPERS (3)"]
        with pytest.raises(IntegrityError):
            self._add(engine, "KEEPers")
        engine.dispose()

    def test_a_database_with_no_collision_keeps_every_name(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The ordinary upgrade renames nothing. A migration that suffixed a name it did not
        have to would withdraw the keep rule naming it."""
        config = _alembic_config(tmp_path, monkeypatch)
        command.upgrade(config, _LIST_CONFIG_HEAL)
        engine = create_engine(f"sqlite:///{tmp_path / 'reaper.db'}")
        self._add(engine, "Keepers", "Kids", "Awards")

        command.upgrade(config, _NAME_NOCASE)

        assert self._added(engine) == ["Keepers", "Kids", "Awards"]
        engine.dispose()

    def test_running_it_again_on_a_converted_database_changes_nothing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The guard reads the stored DDL, because SQLite reflection does not report a
        collation: ``get_columns`` gives the type and nothing about how it compares. Replayed
        by stamping back and upgrading again, the shape a re-run actually takes."""
        config = _alembic_config(tmp_path, monkeypatch)
        command.upgrade(config, "head")
        engine = create_engine(f"sqlite:///{tmp_path / 'reaper.db'}")
        self._add(engine, "Keepers")

        command.stamp(config, _LIST_CONFIG_HEAL)
        command.upgrade(config, _NAME_NOCASE)

        assert self._added(engine) == ["Keepers"]
        with pytest.raises(IntegrityError):
            self._add(engine, "keepers")
        engine.dispose()


# ---------------------------------------------------------------------------
# Persisting the list-protection conversion (d5e6f7a8b9c0).
# ---------------------------------------------------------------------------

_PRIOR_LIST_CONVERSION = "a1b2c3d4e5f7"
_LIST_CONVERSION = "d5e6f7a8b9c0"


def _legacy_list_body() -> dict[str, Any]:
    """A stored body from before every list protected through its own keep rule: the keep
    tags on the policy, and both retired list gates enabled. The shape every install that
    upgrades into the lists release is carrying."""
    body: dict[str, Any] = json.loads(DEFAULT_MOVIE_POLICY.model_dump_json())
    body["protect_conditions"] = []
    body["keep_tags"] = ["reaper-keep"]
    body["keep_tags_match"] = "any"
    body["gates"] = [
        {"gate": "whitelisted", "enabled": True},
        {"gate": "curated_list", "enabled": True},
        *body["gates"],
    ]
    return body


def _only_these_lists(engine: Engine, *sources: str) -> None:
    """Leave the registry holding exactly these sources, under names an operator might have
    chosen. The upgrade to the prior revision seeds rows of its own (the tag list, the shipped
    IMDb list), and the conversion resolves by source and age rather than by spelling, so a
    case about a MISSING list has to clear them rather than add beside them."""
    names = {"arr_tag": "My tagged titles", "imdb": "Films worth keeping"}
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM list_config"))
        for source in sources:
            conn.execute(
                text(
                    "INSERT INTO list_config (name, source, config_json, enabled, built_in,"
                    " created_at) VALUES (:n, :s, '{}', 1, 0, 1750000000)"
                ),
                {"n": names[source], "s": source},
            )


def _seed_policy_of(engine: Engine, media_type: str, body: dict[str, Any]) -> None:
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO policy (policy_hash, body_json, media_type, name, created_at)"
                " VALUES (:h, :b, :m, 'mine', 1750000000)"
            ),
            {"h": f"seeded-{media_type}", "b": json.dumps(body), "m": media_type},
        )


def _all_policy_rows(engine: Engine) -> list[tuple[int, str, str, str]]:
    with engine.begin() as conn:
        return [
            (int(r[0]), str(r[1]), str(r[2]), str(r[3]))
            for r in conn.execute(
                text("SELECT id, media_type, policy_hash, body_json FROM policy ORDER BY id")
            )
        ]


class TestPersistingTheListConversion:
    """The conversion used to run on load and never be written back, so ``repaired`` stayed
    true forever and every scan degraded with a notice the operator could not clear (#516).
    This writes it once, where an upgrade can carry it.

    Appended, never edited in place: snapshots and approvals point at the parent row by hash
    (``db.models.Policy`` is append-only by contract).
    """

    def _upgraded(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Config, Engine]:
        config = _alembic_config(tmp_path, monkeypatch)
        command.upgrade(config, _PRIOR_LIST_CONVERSION)
        return config, create_engine(f"sqlite:///{tmp_path / 'reaper.db'}")

    def test_a_legacy_body_is_converted_and_the_parent_row_survives(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        config, engine = self._upgraded(tmp_path, monkeypatch)
        _only_these_lists(engine, "arr_tag", "imdb")
        _seed_policy_of(engine, "movie", _legacy_list_body())
        before = _all_policy_rows(engine)

        command.upgrade(config, _LIST_CONVERSION)

        after = _all_policy_rows(engine)
        assert after[0] == before[0], "the parent row was edited; it must be left as saved"
        assert len(after) == len(before) + 1
        _, media_type, new_hash, new_body = after[-1]
        assert media_type == "movie"
        stored = json.loads(new_body)
        # Each enabled gate became a rule naming the operator's OWN list name, resolved from
        # the registry: an `on_list` rule naming a list that does not exist reads as a green
        # "checked, did not fire", which is the fail-open direction.
        values = {c["value"] for c in stored["protect_conditions"] if c["field"] == "on_list"}
        assert values == {"My tagged titles", "Films worth keeping"}
        assert not {g["gate"] for g in stored["gates"]} & {"whitelisted", "curated_list"}
        assert "keep_tags" not in stored
        # It loads, and its hash describes its own content, so nothing reads as stale.
        assert new_hash == PolicyBody.model_validate_json(new_body).policy_hash()
        assert new_hash != before[0][2]
        engine.dispose()

    def test_the_load_shim_then_reports_no_repair_at_all(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The point of the whole revision. Before it, this body came back
        ``lists_migrated`` on every load, so every scan degraded and the banner never
        cleared however many times the operator scanned."""
        config, engine = self._upgraded(tmp_path, monkeypatch)
        _only_these_lists(engine, "arr_tag", "imdb")
        _seed_policy_of(engine, "movie", _legacy_list_body())

        command.upgrade(config, _LIST_CONVERSION)

        newest = _all_policy_rows(engine)[-1][3]
        assert has_legacy_list_protections(json.loads(newest)) is False
        engine.dispose()

    def test_it_carries_both_media_types(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Movies and TV are tuned separately and BOTH degrade the scan, so converting one
        leaves the banner exactly as unclearable as before (rule 72)."""
        config, engine = self._upgraded(tmp_path, monkeypatch)
        _only_these_lists(engine, "arr_tag", "imdb")
        _seed_policy_of(engine, "movie", _legacy_list_body())
        tv = _legacy_list_body()
        tv["media_type"] = "tv"
        _seed_policy_of(engine, "tv", tv)

        command.upgrade(config, _LIST_CONVERSION)

        newest = {r[1]: r[3] for r in _all_policy_rows(engine)}
        assert set(newest) == {"movie", "tv"}
        for body_json in newest.values():
            assert has_legacy_list_protections(json.loads(body_json)) is False
        engine.dispose()

    def test_it_leaves_a_body_alone_when_the_replacement_list_is_missing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The refusal that matters. With no tag list to name, the conversion deliberately
        KEEPS the enabled ``whitelisted`` row rather than converting it to a rule naming
        nothing -- so persisting its output would store a body ``build_gates`` refuses to
        scan. The row stays legacy, the load shim keeps handling it, and the editor is where
        the operator is told (which it now does, with a Save)."""
        config, engine = self._upgraded(tmp_path, monkeypatch)
        _only_these_lists(engine, "imdb")  # no arr_tag list for the keep tags to become
        _seed_policy_of(engine, "movie", _legacy_list_body())
        before = _all_policy_rows(engine)

        command.upgrade(config, _LIST_CONVERSION)

        assert _all_policy_rows(engine) == before
        engine.dispose()

    def test_it_is_a_noop_on_a_body_that_is_not_legacy_shaped(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Every fresh install is this case, and an operator who already saved the converted
        draft is too. Re-running must not append a row either time."""
        config, engine = self._upgraded(tmp_path, monkeypatch)
        _only_these_lists(engine, "arr_tag", "imdb")
        _seed_policy_of(engine, "movie", json.loads(DEFAULT_MOVIE_POLICY.model_dump_json()))
        before = _all_policy_rows(engine)

        command.upgrade(config, _LIST_CONVERSION)

        assert _all_policy_rows(engine) == before
        engine.dispose()

    def test_it_survives_a_database_with_no_policy_at_all(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A first boot reaches head with an empty policy table. An upgrade that raised here
        would leave the operator unable to start at all."""
        config, engine = self._upgraded(tmp_path, monkeypatch)

        command.upgrade(config, _LIST_CONVERSION)

        assert _all_policy_rows(engine) == []
        engine.dispose()

    def test_a_body_that_is_not_json_does_not_fail_the_upgrade(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A hand-edited or truncated row must not stop an upgrade whose other revisions
        have nothing to do with policy bodies. It falls to the load shim, which reports it."""
        config, engine = self._upgraded(tmp_path, monkeypatch)
        with engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO policy (policy_hash, body_json, media_type, name, created_at)"
                    " VALUES ('h', 'not json at all', 'movie', 'mine', 1750000000)"
                )
            )
        before = _all_policy_rows(engine)

        command.upgrade(config, _LIST_CONVERSION)

        assert _all_policy_rows(engine) == before
        engine.dispose()

    def test_the_written_row_reads_back_through_the_orm(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``created_at`` is an INTEGER unix timestamp, because raw SQL goes around
        ``db.types.EpochDateTime``. An ISO string here lands a row every later read raises
        on, which is a 500 on the first page load after upgrading (b2c3d4e5f6a7 found it)."""
        config, engine = self._upgraded(tmp_path, monkeypatch)
        _only_these_lists(engine, "arr_tag", "imdb")
        _seed_policy_of(engine, "movie", _legacy_list_body())

        command.upgrade(config, _LIST_CONVERSION)

        with Session(engine) as session:
            rows = session.execute(select(PolicyModel).order_by(PolicyModel.id)).scalars().all()
        assert isinstance(rows[-1].created_at, datetime)
        engine.dispose()
