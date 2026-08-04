# SPDX-License-Identifier: AGPL-3.0-or-later
"""Profile persistence: the caps/grace settings a run obeys.

The fiddly bit is the foreign key -- a profile references a policy row, but a fresh
install has never saved one (it runs on the in-code default). So saving a profile has to
persist the default policy first. These prove that works, is idempotent, and that the
domain's invariants hold through the service.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from reaper.clock import utcnow
from reaper.config import Settings
from reaper.db.base import Base
from reaper.db.models import ListConfig as ListConfigModel
from reaper.db.models import Policy as PolicyModel
from reaper.db.models import Profile
from reaper.db.session import create_engine, create_session_factory
from reaper.engine.policy import DEFAULT_MOVIE_POLICY, ProfileSettings
from reaper.ratings import RatingSource
from reaper.services import profiles
from reaper.services.profiles import (
    active_policy,
    active_profile,
    active_profile_settings,
    save_profile_settings,
)


@pytest.fixture
async def session(tmp_path: Path) -> AsyncIterator[AsyncSession]:
    settings = Settings(data_dir=tmp_path, secret_key="k")  # type: ignore[call-arg]
    engine = create_engine(settings)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory: async_sessionmaker[AsyncSession] = create_session_factory(engine)
    async with factory() as s:
        yield s
    await engine.dispose()


class TestActiveProfileSettings:
    async def test_defaults_before_anything_is_saved(self, session: AsyncSession) -> None:
        """A never-configured install reads the cautious built-ins -- it is not thereby
        permitted to do more than the defaults allow."""
        settings = await active_profile_settings(session)
        assert settings.max_items_per_run == 10
        assert settings.caps_enabled is True

    async def test_a_saved_profile_is_what_loads(self, session: AsyncSession) -> None:
        await save_profile_settings(session, ProfileSettings(max_items_per_run=25, grace_days=30))
        loaded = await active_profile_settings(session)
        assert loaded.max_items_per_run == 25
        assert loaded.grace_days == 30

    async def test_a_legacy_field_in_stored_settings_does_not_crash_the_read(
        self, session: AsyncSession
    ) -> None:
        """A profile written by an older build can carry a field this model has since
        removed. Under extra='forbid' that would crash EVERY read -- including the settings
        page an operator would use to fix it -- so the loader drops unknown keys and keeps
        the operator's real settings, defaulting any new field to its cautious value. This
        is the upgrade path for the removed 'require_approval' setting."""
        # Create the profile normally (sets up the policy FK), then overwrite its blob with
        # one an older build would have written: a departed 'require_approval', no
        # 'caps_enabled'.
        await save_profile_settings(session, ProfileSettings(max_items_per_run=25, grace_days=30))
        row = (
            await session.execute(select(Profile).order_by(Profile.id.asc()).limit(1))
        ).scalar_one()
        row.settings_json = (
            '{"max_items_per_run":25,"max_bytes_per_run":500000000000,'
            '"max_items_per_30d":100,"max_bytes_per_30d":2000000000000,'
            '"grace_days":30,"require_approval":false,"max_unmeasured_per_run":0}'
        )
        await session.flush()

        loaded = await active_profile_settings(session)
        assert loaded.max_items_per_run == 25  # operator's real setting preserved
        assert loaded.grace_days == 30  # preserved
        assert loaded.caps_enabled is True  # new field falls back to the cautious default

    async def test_an_unreadable_settings_blob_degrades_to_cautious_defaults(
        self, session: AsyncSession
    ) -> None:
        """A blob that is not repairable (bad JSON, or a value out of range) must not crash
        the read path either. It degrades to the built-in cautious defaults -- caps on."""
        await save_profile_settings(session, ProfileSettings(max_items_per_run=25))
        row = (
            await session.execute(select(Profile).order_by(Profile.id.asc()).limit(1))
        ).scalar_one()
        row.settings_json = "not json at all"
        await session.flush()

        loaded = await active_profile_settings(session)
        assert loaded.max_items_per_run == 10  # cautious default
        assert loaded.caps_enabled is True

    async def test_an_unreadable_blob_is_flagged_fell_back(self, session: AsyncSession) -> None:
        """The shipped defaults can be LOOSER than what the operator saved (a shorter grace,
        a higher cap), so the fall-back is flagged: the scan degrades on it and the Pace page
        shows a recovery notice, never a silent swap (rule 14)."""
        await save_profile_settings(session, ProfileSettings(max_items_per_run=25, grace_days=30))
        row = (
            await session.execute(select(Profile).order_by(Profile.id.asc()).limit(1))
        ).scalar_one()
        row.settings_json = "not json at all"
        await session.flush()

        active = await active_profile(session)
        assert active.fell_back is True
        assert active.repaired is True
        assert active.settings.grace_days == 14  # the shipped default, looser than the saved 30

    async def test_a_key_only_migration_is_not_flagged(self, session: AsyncSession) -> None:
        """Dropping a departed key keeps the operator's real values, so it is benign and must
        NOT be flagged -- flagging it would degrade every scan after a routine upgrade."""
        await save_profile_settings(session, ProfileSettings(max_items_per_run=25, grace_days=30))
        row = (
            await session.execute(select(Profile).order_by(Profile.id.asc()).limit(1))
        ).scalar_one()
        row.settings_json = (
            '{"max_items_per_run":25,"max_bytes_per_run":500000000000,'
            '"max_items_per_30d":100,"max_bytes_per_30d":2000000000000,'
            '"grace_days":30,"require_approval":false,"max_unmeasured_per_run":0}'
        )
        await session.flush()

        active = await active_profile(session)
        assert active.fell_back is False
        assert active.settings.grace_days == 30  # the operator's real value survived


class TestSavingCreatesTheBackingPolicyRow:
    async def test_the_first_save_persists_the_default_policy(self, session: AsyncSession) -> None:
        """The profile's FK needs a policy row; a fresh install has none, so saving must
        create one from the in-code default."""
        assert (await session.execute(select(func.count()).select_from(PolicyModel))).scalar() == 0

        await save_profile_settings(session, ProfileSettings())

        assert (await session.execute(select(func.count()).select_from(PolicyModel))).scalar() == 1
        profile = (await session.execute(select(Profile))).scalar_one()
        assert profile.active_policy_id is not None

    async def test_saving_twice_does_not_fork_the_profile(self, session: AsyncSession) -> None:
        await save_profile_settings(session, ProfileSettings(max_items_per_run=5))
        await save_profile_settings(session, ProfileSettings(max_items_per_run=7))

        count = (await session.execute(select(func.count()).select_from(Profile))).scalar()
        assert count == 1  # updated in place, not duplicated
        assert (await active_profile_settings(session)).max_items_per_run == 7

    async def test_the_unread_enabled_column_keeps_its_shipped_value(
        self, session: AsyncSession
    ) -> None:
        """`Profile.enabled` is written False at creation, and that is ALL this pins (#271).

        It used to claim this was what stopped a starter template deleting a library. Nothing
        in `src/` reads the column, so a profile written `enabled=True` would scan and reap
        identically, and a test asserting a safeguard nobody implemented is rule 7/24's
        failure. What actually keeps a fresh install from acting is the master switch shipping
        off (`test_app.test_destructive_actions_are_off_by_default`,
        `test_settings_api.TestSafety.test_it_starts_read_only`) and the content-bound typed
        phrase on `api.runs.execute_run`.

        Kept rather than deleted because the attribute cannot go: `db.models.Profile.enabled`
        records why `alembic check` blocks that.
        """
        await save_profile_settings(session, ProfileSettings())
        profile = (await session.execute(select(Profile))).scalar_one()
        assert profile.enabled is False


async def _store_policy(session: AsyncSession, body_json: str) -> None:
    """Put a raw body straight into the table, bypassing every in-app writer.

    Only an externally edited, truncated or restored row can hold something
    ``model_dump_json`` would never produce, which is exactly the row these tests are about.
    """
    session.add(
        PolicyModel(
            policy_hash="h",
            body_json=body_json,
            media_type="movie",
            name="stored",
            created_at=utcnow(),
        )
    )
    await session.flush()


class TestACorruptPolicyBodyNeverRaises:
    """``active_policy`` is read by the editor, the simulator and the scan alike, so an
    unreadable stored body has to fall back -- a raise takes out the page that fixes it.

    The two escapes this pins were both outside the handler: malformed JSON reaches the
    ``ValidationError`` branch and used to blow up in ``json.loads`` there, and valid JSON
    that is not an object used to reach ``rebalance`` and raise ``AttributeError``.
    """

    @pytest.mark.parametrize(
        "body_json",
        [
            "",
            "not json at all",
            '{"media_type": "movie", "condemn_at": 70,',  # truncated mid-write
            '["signals", "as", "a", "list"]',
            "42",
            "null",
            '"a bare string"',
            '{"signals": [7]}',
            '{"signals": "text"}',
        ],
        ids=[
            "empty",
            "not-json",
            "truncated",
            "json-array",
            "json-number",
            "json-null",
            "json-string",
            "signal-not-an-object",
            "signals-not-a-list",
        ],
    )
    async def test_it_falls_back_to_the_shipped_default(
        self, session: AsyncSession, body_json: str
    ) -> None:
        await _store_policy(session, body_json)

        active = await active_policy(session, "movie")

        assert active.fell_back is True
        assert active.rescaled is False
        assert active.repaired is True  # the scan degrades on it
        assert active.body == DEFAULT_MOVIE_POLICY
        assert active.name == "default"

    async def test_a_body_that_only_misses_the_budget_is_still_rescaled(
        self, session: AsyncSession
    ) -> None:
        """The fallback must not swallow the repairable case: a body written before removal
        weights had to total 100 keeps the operator's own tuning.

        The gate row still carries the retired ``secondary`` key, because this is the one
        path that reaches ``rebalance`` without ``recover_rating_rules`` having stripped it
        first: a rating bar already moved, so no recovery fires, on a database the
        ``secondary`` migration has not reached. ``PolicyBody`` is ``extra="forbid"``, so
        without ``drop_retired_gate_keys`` in ``rebalance`` itself the repair returns
        ``None`` and the operator's tuning is thrown away (rule 118).
        """
        legacy = json.loads(DEFAULT_MOVIE_POLICY.model_dump_json())
        for gate in legacy["gates"]:
            if gate["gate"] == "rating_floor":
                gate["secondary"] = 1000
        for signal in legacy["signals"]:
            signal["weight"] *= 2

        await _store_policy(session, json.dumps(legacy))

        active = await active_policy(session, "movie")

        assert active.fell_back is False
        assert active.rescaled is True
        assert active.name == "stored"
        assert sum(s.weight for s in active.body.signals) == 100

    async def test_a_lost_rating_bar_is_restored_and_flagged(self, session: AsyncSession) -> None:
        """The one recovery that runs on a body which validates PERFECTLY WELL.

        A body written before the rating bar moved off the gate row loads clean and keeps
        nothing, so validation cannot see the loss. It is restored, and flagged: restoring
        it changes what the scan decides, so the run degrades until the operator saves.
        """
        legacy = json.loads(DEFAULT_MOVIE_POLICY.model_dump_json())
        del legacy["keep_rating_rules"]
        for gate in legacy["gates"]:
            if gate["gate"] == "rating_floor":
                gate["enabled"], gate["threshold"], gate["secondary"] = True, 75, 1000

        await _store_policy(session, json.dumps(legacy))

        active = await active_policy(session, "movie")

        assert active.rating_rules_recovered is True
        assert active.repaired is True  # the scan degrades on it
        assert active.rescaled is False and active.fell_back is False
        assert active.name == "stored"
        assert [(r.source, r.floor, r.min_votes) for r in active.body.keep_rating_rules] == [
            (RatingSource.IMDB, 75, 1000)
        ]

    async def test_a_body_needing_both_repairs_keeps_the_rating_bar(
        self, session: AsyncSession
    ) -> None:
        """The two shims used to race: the rebalance re-read the RAW body and lost the bar.

        A body that predates the rating-bar move AND carries weights that no longer total
        100 needs both repairs. The recovery ran first and produced a body with the bar put
        back, but that body still failed to validate -- on the weights -- so the code fell
        through and rebalanced the ORIGINAL, dropping the recovered bar on the floor. The
        operator was then told only that their units moved, and saving the draft the editor
        opens on wrote the loss back permanently (rules 105 and 65).
        """
        legacy = json.loads(DEFAULT_MOVIE_POLICY.model_dump_json())
        del legacy["keep_rating_rules"]
        for gate in legacy["gates"]:
            if gate["gate"] == "rating_floor":
                gate["enabled"], gate["threshold"], gate["secondary"] = True, 75, 1000
        for signal in legacy["signals"]:  # weights no longer total 100
            signal["weight"] *= 2

        await _store_policy(session, json.dumps(legacy))

        active = await active_policy(session, "movie")

        assert active.rescaled is True
        assert active.rating_rules_recovered is True  # the bar survived the rebalance
        assert active.fell_back is False
        assert sum(s.weight for s in active.body.signals) == 100
        assert [(r.source, r.floor, r.min_votes) for r in active.body.keep_rating_rules] == [
            (RatingSource.IMDB, 75, 1000)
        ]

    async def test_a_deliberately_empty_rating_bar_is_left_alone(
        self, session: AsyncSession
    ) -> None:
        """An explicit empty list is an operator who cleared their bars, not a lost one
        (rule 1: omitted is not the same as explicitly empty). Restoring it would put back a
        protection they deliberately removed, and degrade every scan telling them so."""
        stored = json.loads(DEFAULT_MOVIE_POLICY.model_dump_json())
        stored["keep_rating_rules"] = []

        await _store_policy(session, json.dumps(stored))

        active = await active_policy(session, "movie")

        assert active.rating_rules_recovered is False
        assert active.repaired is False
        assert active.body.keep_rating_rules == ()


def _legacy_list_body() -> dict[str, object]:
    """A stored body from before every list protected through its own keep rule: the keep
    tags on the policy, plus the two retired list gates, both enabled."""
    body = json.loads(DEFAULT_MOVIE_POLICY.model_dump_json())
    body["protect_conditions"] = []
    body["keep_tags"] = ["reaper-keep"]
    body["keep_tags_match"] = "any"
    body["gates"] = [
        {"gate": "whitelisted", "enabled": True},
        {"gate": "curated_list", "enabled": True},
        *body["gates"],
    ]
    return body


async def _seed_list_rows(session: AsyncSession) -> None:
    """The registry rows the conversion's rules must point at, under names the operator
    may have chosen: resolution is by source and age, never by spelling."""
    for name, source, config in (
        ("Films worth keeping", "imdb", {"preset": "top250"}),
        ("My tagged titles", "arr_tag", {"tags": ["reaper-keep"], "match": "any"}),
    ):
        session.add(
            ListConfigModel(
                name=name,
                source=source,
                config_json=json.dumps(config),
                enabled=True,
                built_in=False,
                created_at=utcnow(),
            )
        )
    await session.commit()


class TestALegacyListBodyIsConvertedOnLoad:
    """``active_policy`` composes ``convert_list_protections`` FIRST, on the raw dict:
    ``keep_tags`` is a key ``Frozen`` forbids, so a merely-legacy body read without the
    conversion falls back to the shipped default -- the silent substitution rule 65
    forbids. The conversion is a repair like the others: flagged, degrading, never
    silently adopted (rule 105)."""

    async def test_the_body_loads_with_its_gates_as_rules_and_is_flagged(
        self, session: AsyncSession
    ) -> None:
        await _seed_list_rows(session)
        await _store_policy(session, json.dumps(_legacy_list_body()))

        active = await active_policy(session, "movie")

        assert active.lists_migrated is True
        assert active.repaired is True  # the scan degrades on it
        assert active.fell_back is False
        assert active.name == "stored"
        # Each enabled gate became a rule naming the CURRENT list of its source -- the
        # operator's own names, resolved from the registry rather than assumed.
        values = {str(c.value) for c in active.body.protect_conditions if c.field == "on_list"}
        assert values == {"My tagged titles", "Films worth keeping"}
        # ...and the retired gate rows left the body.
        assert not {g.gate.value for g in active.body.gates} & {"whitelisted", "curated_list"}

    async def test_a_body_that_is_not_legacy_shaped_is_untouched(
        self, session: AsyncSession
    ) -> None:
        await _seed_list_rows(session)
        await _store_policy(session, DEFAULT_MOVIE_POLICY.model_dump_json())

        active = await active_policy(session, "movie")

        assert active.lists_migrated is False
        assert active.repaired is False
        assert active.body == DEFAULT_MOVIE_POLICY


class TestTheDefaultPolicyKeepsThePlexListsTheRegistryHolds:
    """``DEFAULT_LIST_CONDITIONS`` names the two lists ``list_config.DEFAULT_LISTS`` seeds. A
    Plex keep collection arrives by migration instead, and an install that has never saved a
    policy returns before ``convert_list_protections`` can run -- so nothing pointed a rule at
    it, while the WHITELISTED gate that used to spare its titles is retired. A protection that
    fired on the previous release and cannot fire on this one, silently."""

    @staticmethod
    async def _seed_plex_collection(session: AsyncSession, name: str = "Never Reap") -> None:
        session.add(
            ListConfigModel(
                name=name,
                source="plex_collection",
                config_json=json.dumps({"library": "Films", "collection": name}),
                enabled=True,
                built_in=False,
                created_at=utcnow(),
            )
        )
        await session.commit()

    @pytest.mark.parametrize("media_type", ["movie", "tv"])
    async def test_a_seeded_collection_is_kept_outright(
        self, session: AsyncSession, media_type: str
    ) -> None:
        await self._seed_plex_collection(session)

        active = await active_policy(session, media_type)

        values = {str(c.value) for c in active.body.protect_conditions if c.field == "on_list"}
        assert "Never Reap" in values
        # Additive: the shipped conditions are still there, and nothing is flagged, because
        # putting the rule back removes the loss rather than announcing it.
        assert {"IMDb Top 250", "Titles you've tagged"} <= values
        assert active.repaired is False
        assert active.name == "default"

    async def test_a_registry_it_cannot_read_leaves_the_shipped_rules_alone(
        self, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The direction this helper promises: it may ADD cover, never withdraw it.

        A read failure here is not "no lists of your own" (rules 65/91), and the only safe
        reading of an unanswerable registry is the shipped set unchanged -- returning the
        default is what makes that true, so the failure is driven rather than argued.
        """
        await self._seed_plex_collection(session)

        async def unreadable(
            _session: AsyncSession,
        ) -> tuple[str | None, str | None, tuple[str, ...]]:
            raise SQLAlchemyError("the registry could not be read")

        monkeypatch.setattr(profiles, "_conversion_list_names", unreadable)

        active = await active_policy(session, "movie")

        values = {str(c.value) for c in active.body.protect_conditions if c.field == "on_list"}
        assert values == {"IMDb Top 250", "Titles you've tagged"}, (
            "an unreadable registry moved the shipped conditions"
        )
        assert active.repaired is False

    async def test_a_watchlist_definition_is_kept_too(self, session: AsyncSession) -> None:
        """The other source ``_conversion_list_names`` returns as the operator's own."""
        session.add(
            ListConfigModel(
                name="My watchlist",
                source="plex_watchlist",
                config_json="{}",
                enabled=True,
                built_in=False,
                created_at=utcnow(),
            )
        )
        await session.commit()

        active = await active_policy(session, "movie")

        assert "My watchlist" in {
            str(c.value) for c in active.body.protect_conditions if c.field == "on_list"
        }

    async def test_an_empty_registry_leaves_the_shipped_conditions_alone(
        self, session: AsyncSession
    ) -> None:
        """The helper may only ever ADD cover. With nothing of the operator's own to point
        at, the default is returned exactly as shipped rather than with its rules rebuilt."""
        active = await active_policy(session, "movie")

        assert active.body == DEFAULT_MOVIE_POLICY

    async def test_a_collection_the_default_already_names_gains_no_second_rule(
        self, session: AsyncSession
    ) -> None:
        """Case-folded on both sides, the comparison every reader of a list name makes
        (rule 88), so a duplicate rule cannot arrive by capitalization."""
        await self._seed_plex_collection(session, name="imdb top 250")

        active = await active_policy(session, "movie")

        values = [str(c.value) for c in active.body.protect_conditions if c.field == "on_list"]
        assert values == [c.value for c in DEFAULT_MOVIE_POLICY.protect_conditions]


class TestInvariantsHoldThroughTheService:
    async def test_a_run_cap_above_the_rolling_cap_is_refused(self, session: AsyncSession) -> None:
        with pytest.raises(ValueError, match="rolling cap"):
            ProfileSettings(max_items_per_run=200, max_items_per_30d=100)

    async def test_grace_under_a_week_is_refused(self, session: AsyncSession) -> None:
        with pytest.raises(ValueError):
            ProfileSettings(grace_days=3)
