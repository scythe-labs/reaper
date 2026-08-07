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
from reaper.engine.policy import (
    DEFAULT_MOVIE_POLICY,
    DEFAULT_TV_POLICY,
    PolicyRepair,
    ProfileSettings,
)
from reaper.ratings import RatingSource
from reaper.services import app_settings, list_config, list_rules, profiles
from reaper.services.profiles import (
    active_policy,
    active_profile,
    active_profile_settings,
    save_profile_settings,
)
from reaper.services.scan_runner import ScanConfigError, build_gates


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


async def _store_policy(
    session: AsyncSession, body_json: str, *, media_type: str = "movie"
) -> None:
    """Put a raw body straight into the table, bypassing every in-app writer.

    Only an externally edited, truncated or restored row can hold something
    ``model_dump_json`` would never produce, which is exactly the row these tests are about.
    """
    session.add(
        PolicyModel(
            policy_hash="h",
            body_json=body_json,
            media_type=media_type,
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

        assert active.repairs == (PolicyRepair.FELL_BACK,)
        assert active.repaired is True  # the scan degrades on it
        # Alone: nothing of the stored body survived for a second repair to be about, and a
        # notice saying their lists were converted would describe a body that is not on screen.
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

        assert active.repairs == (PolicyRepair.RESCALED,)
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

        assert active.repairs == (PolicyRepair.RATING_RULES_RESTORED,)
        assert active.repaired is True  # the scan degrades on it
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

        # Both repairs, in the order they were applied: the bar survived the rebalance, and
        # the operator's notices read in the order the repairs happened.
        assert active.repairs == (PolicyRepair.RATING_RULES_RESTORED, PolicyRepair.RESCALED)
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

        assert active.repairs == ()
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


async def _add_list(
    session: AsyncSession, name: str, source: str, config: dict[str, object]
) -> None:
    """One registry row, added in call order so a test can put a decoy in front of the row
    the conversion must find."""
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


async def _seed_list_rows(session: AsyncSession) -> None:
    """The registry rows the conversion's rules must point at, under names the operator may
    have chosen: resolution is by what each row HOLDS -- these tags, that preset -- never by
    spelling, which is theirs to change, and never by age, which is theirs to change too."""
    await _add_list(session, "Films worth keeping", "imdb", {"preset": "top250"})
    await _add_list(
        session, "My tagged titles", "arr_tag", {"tags": ["reaper-keep"], "match": "any"}
    )


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

        assert active.repairs == (PolicyRepair.LISTS_MIGRATED,)
        assert active.repaired is True  # the scan degrades on it
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

        assert active.repairs == ()
        assert active.repaired is False
        assert active.body == DEFAULT_MOVIE_POLICY

    async def test_a_migrated_tv_body_does_not_gain_the_imdb_rule(
        self, session: AsyncSession
    ) -> None:
        """#539: the IMDb chart is movies only, so migrating a TV body carries over the tag
        list but not the IMDb list -- a TV rule naming it can never match a season (rule 38).
        The curated_list gate strips clean on the TV body, its protection was never live
        there."""
        await _seed_list_rows(session)
        body = json.loads(DEFAULT_TV_POLICY.model_dump_json())
        body["protect_conditions"] = []
        body["keep_tags"] = ["reaper-keep"]
        body["keep_tags_match"] = "any"
        body["gates"] = [
            {"gate": "whitelisted", "enabled": True},
            {"gate": "curated_list", "enabled": True},
            *body["gates"],
        ]
        await _store_policy(session, json.dumps(body), media_type="tv")

        active = await active_policy(session, "tv")

        values = {str(c.value) for c in active.body.protect_conditions if c.field == "on_list"}
        assert values == {"My tagged titles"}
        assert "Films worth keeping" not in values
        # Both retired gate rows left, so the TV scan is not refused over a movies-only list.
        assert not {g.gate.value for g in active.body.gates} & {"whitelisted", "curated_list"}


class TestTheConversionNamesTheListTheOperatorsProtectionBecame:
    """A list they added for something else may not inherit a rule nothing gave it.

    The conversion runs on every load of a legacy body and re-reads the registry each time,
    so which list it names is answered fresh whenever the registry changes. Answering it by
    AGE gave the answer away: delete the tag list this converts and the next arr-tag list
    becomes the oldest of its source, so it silently took over an outright keep. Settings ->
    Lists reads that same conversion (``list_rules.usage``), so the row read "Keeps every
    title on it" for a list no rule mentions, and Remove could not take it off -- the writer
    behind it declines to touch a repaired policy on purpose.
    """

    async def test_a_decoy_older_than_the_tagged_list_does_not_take_its_rule(
        self, session: AsyncSession
    ) -> None:
        """Age decides nothing. The decoy is added FIRST, and the list carries a tag the
        operator added beside the stored one, so neither position nor an exact set match is
        what finds it."""
        await _add_list(session, "Saturday movie night", "arr_tag", {"tags": ["movie-night"]})
        await _add_list(session, "My tagged titles", "arr_tag", {"tags": ["reaper-keep", "gold"]})
        await _store_policy(session, json.dumps(_legacy_list_body()))

        active = await active_policy(session, "movie")

        values = {str(c.value) for c in active.body.protect_conditions if c.field == "on_list"}
        assert values == {"My tagged titles"}
        assert "whitelisted" not in {g.gate.value for g in active.body.gates}

    async def test_removing_the_tagged_list_does_not_hand_its_rule_to_another(
        self, session: AsyncSession
    ) -> None:
        """The operator's own sequence: add a list, remove the one their tags became.

        Removing it takes the protection with it, and the scan then refuses rather than
        running with a retired gate it cannot answer -- the loud, fail-closed exit rule 38
        asks for, where the silent one handed the protection to a list nobody chose.
        """
        await _seed_list_rows(session)
        await _add_list(session, "Saturday movie night", "arr_tag", {"tags": ["movie-night"]})
        await _store_policy(session, json.dumps(_legacy_list_body()))
        # Read straight from the table: `list_config.all_lists` seeds the shipped defaults on
        # a registry that has never been seeded, which would put a second tag list on the
        # screen this test is about.
        tagged = (
            await session.execute(
                select(ListConfigModel).where(ListConfigModel.name == "My tagged titles")
            )
        ).scalar_one()

        await list_config.delete(session, tagged.id)
        await session.commit()
        # The Lists screen's own removal path, and it declines to write here (the policy is
        # repaired), which is exactly why the conversion must not be naming a list either.
        await list_rules.detach_list(session, tagged.name)

        active = await active_policy(session, "movie")
        values = {str(c.value) for c in active.body.protect_conditions if c.field == "on_list"}
        assert "Saturday movie night" not in values
        assert values == {"Films worth keeping"}
        # The gate stays, so the scan stops instead of running a protection short.
        assert "whitelisted" in {g.gate.value for g in active.body.gates}
        with pytest.raises(ScanConfigError, match="pointing at a list that is no longer there"):
            build_gates(active.body)

    async def test_an_imdb_list_of_their_own_does_not_take_the_shipped_ones_rule(
        self, session: AsyncSession
    ) -> None:
        """The retired CURATED_LIST gate named one shipped list, the IMDb Top 250. A list
        the operator pasted an id for is not that list, whatever order it sits in."""
        await _add_list(session, "My watchlist", "imdb", {"list_id": "ls000000000"})
        await _add_list(
            session, "My tagged titles", "arr_tag", {"tags": ["reaper-keep"], "match": "any"}
        )
        await _store_policy(session, json.dumps(_legacy_list_body()))

        active = await active_policy(session, "movie")

        values = {str(c.value) for c in active.body.protect_conditions if c.field == "on_list"}
        assert values == {"My tagged titles"}
        assert "curated_list" in {g.gate.value for g in active.body.gates}

    async def test_a_body_whose_tags_were_cleared_names_no_tag_list(
        self, session: AsyncSession
    ) -> None:
        """An explicit empty ``keep_tags`` is an operator who cleared it (rule 1), so there
        is no protection to carry and no list to find -- and the gate may leave, because
        nothing it covered is being dropped."""
        await _seed_list_rows(session)
        body = _legacy_list_body()
        body["keep_tags"] = []
        await _store_policy(session, json.dumps(body))

        active = await active_policy(session, "movie")

        values = {str(c.value) for c in active.body.protect_conditions if c.field == "on_list"}
        assert values == {"Films worth keeping"}
        assert "whitelisted" not in {g.gate.value for g in active.body.gates}


class TestTheDefaultPolicyKeepsThePlexListsTheRegistryHolds:
    """The shipped conditions name the lists ``list_config.DEFAULT_LISTS`` seeds. A Plex keep
    collection arrives by migration instead, and an install that has never saved a policy
    returns before ``convert_list_protections`` can run -- so nothing pointed a rule at it,
    while the WHITELISTED gate that used to spare its titles is retired. A protection that
    fired on the previous release and cannot fire on this one, silently."""

    @staticmethod
    async def _seed_plex_collection(
        session: AsyncSession, name: str = "Never Reap", library: str = "Films"
    ) -> None:
        session.add(
            ListConfigModel(
                name=name,
                source="plex_collection",
                config_json=json.dumps({"library": library, "collection": name}),
                enabled=True,
                built_in=False,
                created_at=utcnow(),
            )
        )
        await session.commit()

    @staticmethod
    async def _seed_libraries(session: AsyncSession, *libraries: tuple[str, str]) -> None:
        """Store synced Plex libraries as ``(title, kind)`` pairs, the shape a library sync
        writes (``app_settings.set_plex_libraries``). ``kind`` is Plex's own ``movie``/``show``."""
        await app_settings.set_plex_libraries(
            session,
            [
                {"key": i, "title": title, "kind": kind, "enabled": True}
                for i, (title, kind) in enumerate(libraries)
            ],
        )
        await session.commit()

    @pytest.mark.parametrize("media_type", ["movie", "tv"])
    async def test_an_unsynced_collection_is_kept_outright_on_both(
        self, session: AsyncSession, media_type: str
    ) -> None:
        """No library synced, so a collection's media type is unknown: it stays on both policies,
        fail-open (#545, rules 65/91). The narrowed cases are the two below."""
        await self._seed_plex_collection(session)

        active = await active_policy(session, media_type)

        values = {str(c.value) for c in active.body.protect_conditions if c.field == "on_list"}
        assert "Never Reap" in values
        # Additive: the shipped conditions are still there, and nothing is flagged, because
        # putting the rule back removes the loss rather than announcing it. The shipped set is
        # the media type's own -- the IMDb chart is on the movie default alone (#539).
        default = DEFAULT_MOVIE_POLICY if media_type == "movie" else DEFAULT_TV_POLICY
        shipped = {str(c.value) for c in default.protect_conditions if c.field == "on_list"}
        assert shipped <= values
        assert active.repaired is False
        assert active.name == "default"

    async def test_a_collection_in_a_movie_library_lands_on_the_movie_policy_alone(
        self, session: AsyncSession
    ) -> None:
        """#545: the library is synced as a movie library, so the collection holds movies only.
        Its keep rule seeds on the movie policy and not the TV one, where it could never match a
        season and would read as a protection the operator never chose (rule 38)."""
        await self._seed_libraries(session, ("Films", "movie"))
        await self._seed_plex_collection(session, library="Films")

        movie = {
            str(c.value)
            for c in (await active_policy(session, "movie")).body.protect_conditions
            if c.field == "on_list"
        }
        tv = {
            str(c.value)
            for c in (await active_policy(session, "tv")).body.protect_conditions
            if c.field == "on_list"
        }
        assert "Never Reap" in movie
        assert "Never Reap" not in tv

    async def test_a_collection_in_a_show_library_lands_on_the_tv_policy_alone(
        self, session: AsyncSession
    ) -> None:
        """The mirror: a collection in a show library seeds on the TV policy alone."""
        await self._seed_libraries(session, ("Shows", "show"))
        await self._seed_plex_collection(session, library="Shows")

        movie = {
            str(c.value)
            for c in (await active_policy(session, "movie")).body.protect_conditions
            if c.field == "on_list"
        }
        tv = {
            str(c.value)
            for c in (await active_policy(session, "tv")).body.protect_conditions
            if c.field == "on_list"
        }
        assert "Never Reap" not in movie
        assert "Never Reap" in tv

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
            _session: AsyncSession, *, keep_tags: tuple[str, ...]
        ) -> tuple[str | None, str | None, tuple[str, ...], dict[str, frozenset[str]]]:
            raise SQLAlchemyError("the registry could not be read")

        monkeypatch.setattr(profiles, "_conversion_list_names", unreadable)

        active = await active_policy(session, "movie")

        values = {str(c.value) for c in active.body.protect_conditions if c.field == "on_list"}
        assert values == {"IMDb Top 250", "Titles you've tagged"}, (
            "an unreadable registry moved the shipped conditions"
        )

    async def test_a_registry_it_cannot_read_says_so_instead_of_scanning_quietly(
        self, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Leaving the shipped rules alone is right and is not enough on its own.

        The seeded collection above has no rule naming it in this body, so the scan is about
        to judge the whole library with that keep list doing nothing. It used to carry a log
        line and an empty ``repairs``, so the scan's degradation loop never fired -- and the
        scan's OWN registry read is a separate query that can succeed on a transient error,
        which is what let the run complete clean (rules 65/91).

        Not a ``PolicyRepair``: every member of that enum is answered by saving the policy,
        and saving fixes nothing here. It still reads as ``repaired``, because that is what
        degrades the scan and what stops ``list_rules`` persisting a body missing the rules.
        """
        await self._seed_plex_collection(session)

        async def unreadable(
            _session: AsyncSession, *, keep_tags: tuple[str, ...]
        ) -> tuple[str | None, str | None, tuple[str, ...], dict[str, frozenset[str]]]:
            raise SQLAlchemyError("the registry could not be read")

        monkeypatch.setattr(profiles, "_conversion_list_names", unreadable)

        active = await active_policy(session, "movie")

        assert active.lists_unreadable is True
        assert active.repaired is True, "the scan would not degrade"
        assert active.repairs == (), "nothing was repaired, so no save can clear it"

    async def test_a_registry_that_reads_fine_is_not_flagged(self, session: AsyncSession) -> None:
        """The other side of rule 65's line: a read that SUCCEEDS and finds nothing of the
        operator's own falls back to the shipped conditions silently, and must not degrade
        every scan on an install that simply has no Plex lists."""
        active = await active_policy(session, "movie")

        assert active.lists_unreadable is False
        assert active.repaired is False

    async def test_a_watchlist_definition_is_kept_too(self, session: AsyncSession) -> None:
        """The other source ``_conversion_list_names`` returns as the operator's own. A
        watchlist spans the account and can hold both types, so its rule stays on both policies
        even with libraries synced -- the scoping is a collection concern (#545)."""
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
        await self._seed_libraries(session, ("Films", "movie"), ("Shows", "show"))

        movie = {
            str(c.value)
            for c in (await active_policy(session, "movie")).body.protect_conditions
            if c.field == "on_list"
        }
        tv = {
            str(c.value)
            for c in (await active_policy(session, "tv")).body.protect_conditions
            if c.field == "on_list"
        }
        assert "My watchlist" in movie
        assert "My watchlist" in tv

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
