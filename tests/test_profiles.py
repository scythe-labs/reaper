# SPDX-License-Identifier: AGPL-3.0-or-later
"""Profile persistence, the caps and grace settings a run obeys.

The tricky part is the foreign key. A profile references a policy row, but a fresh
install has never saved one, so it runs on the in-code default. Saving a profile has to
persist the default policy first. These tests check that this works, that it is
idempotent, and that the domain's invariants hold through the service.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from reaper.clock import utcnow
from reaper.config import Settings
from reaper.db.base import Base
from reaper.db.models import AppSetting, Profile
from reaper.db.models import ListConfig as ListConfigModel
from reaper.db.models import Policy as PolicyModel
from reaper.db.session import create_engine, create_session_factory
from reaper.engine.policy import DEFAULT_MOVIE_POLICY, DEFAULT_TV_POLICY, ProfileSettings
from reaper.engine.policy_migrations import PolicyRepair
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
    settings = Settings(data_dir=tmp_path, secret_key="k")
    engine = create_engine(settings)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory: async_sessionmaker[AsyncSession] = create_session_factory(engine)
    async with factory() as s:
        yield s
    await engine.dispose()


class TestActiveProfileSettings:
    async def test_defaults_before_anything_is_saved(self, session: AsyncSession) -> None:
        """A never-configured install reads the cautious built-in settings, and it is
        never allowed to do more than those defaults permit."""
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
        removed. Under extra='forbid' that would crash every read, including the settings
        page an operator would use to fix it, so the loader drops unknown keys and keeps
        the operator's real settings, defaulting any new field to its cautious value. This
        is the upgrade path for the removed 'require_approval' setting."""
        # Create the profile normally (sets up the policy FK), then overwrite its blob with
        # one an older build would have written, carrying a departed 'require_approval' key
        # and no 'caps_enabled'.
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
        """A blob that is not repairable, whether bad JSON or a value out of range, must
        not crash the read path either. It degrades to the built-in cautious defaults,
        with caps on."""
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
        """The shipped defaults can be looser than what the operator saved, for example a
        shorter grace period or a higher cap, so the fall-back is flagged. The scan
        degrades on it, and the Pace page shows a recovery notice instead of swapping
        values silently."""
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
        """Dropping a departed key keeps the operator's real values, so this case is benign
        and must not be flagged. Flagging it would degrade every scan after a routine
        upgrade."""
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


class TestSavingWritesNoPolicyRow:
    async def test_the_first_save_leaves_the_policy_table_empty(
        self, session: AsyncSession
    ) -> None:
        """Saving Pace settings is not saving a policy, and it must not write one.

        An empty policy table is what keeps ``active_policy`` computing the wider,
        computed body on every read (see
        ``TestTheDefaultPolicyKeepsThePlexListsTheRegistryHolds``), instead of reading
        back a stale row."""
        await save_profile_settings(session, ProfileSettings())

        assert (await session.execute(select(func.count()).select_from(PolicyModel))).scalar() == 0
        assert (await session.execute(select(func.count()).select_from(Profile))).scalar() == 1

    async def test_saving_twice_does_not_fork_the_profile(self, session: AsyncSession) -> None:
        await save_profile_settings(session, ProfileSettings(max_items_per_run=5))
        await save_profile_settings(session, ProfileSettings(max_items_per_run=7))

        count = (await session.execute(select(func.count()).select_from(Profile))).scalar()
        assert count == 1  # updated in place, not duplicated
        assert (await active_profile_settings(session)).max_items_per_run == 7


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
    """``active_policy`` is read by the editor, the simulator, and the scan alike, so an
    unreadable stored body has to fall back. A raise here would take out the page that
    fixes it.

    This pins two escapes that both sit outside the main handler. Malformed JSON must
    not reach ``json.loads`` unguarded inside the ``ValidationError`` branch, and valid
    JSON that is not an object must not reach ``rebalance`` and raise ``AttributeError``.
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
        # Nothing of the stored body survives, so there is nothing left for a second
        # repair to name. A notice saying their lists were converted would describe a
        # body that is not on screen.
        assert active.body == DEFAULT_MOVIE_POLICY
        assert active.name == "default"

    async def test_a_body_that_only_misses_the_budget_is_still_rescaled(
        self, session: AsyncSession
    ) -> None:
        """The fallback must not swallow the repairable case. A body written before weights
        had to total 100 still keeps the operator's own tuning after it is rescaled.

        The gate row still carries the retired ``secondary`` key, because this is the one
        path that reaches ``rebalance`` without ``recover_rating_rules`` stripping it
        first. The rating bar already moved, so no recovery fires here, on a database the
        ``secondary`` migration has not reached yet. ``PolicyBody`` is ``extra="forbid"``,
        so without ``drop_retired_gate_keys`` inside ``rebalance`` itself, the repair
        would return ``None`` and throw away the operator's tuning.
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
        """The one recovery that runs on a body that validates perfectly well.

        A body written before the rating bar moved off the gate row loads clean and keeps
        nothing, so validation cannot see the loss. The bar is restored and flagged,
        because restoring it changes what the scan decides, so the run degrades until the
        operator saves.
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
        """A body needing both repairs keeps the restored rating bar through the rebalance.

        A body that predates the rating-bar move and also carries weights that no longer
        total 100 needs both repairs at once. The order matters: the rebalance must run
        on the body with the bar already restored, not on the original body, or the
        recovered bar is dropped and only the weight change is reported to the operator.
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

        # Both repairs appear in the order they were applied. The bar survived the
        # rebalance, and the operator's notices read in the order the repairs happened.
        assert active.repairs == (PolicyRepair.RATING_RULES_RESTORED, PolicyRepair.RESCALED)
        assert sum(s.weight for s in active.body.signals) == 100
        assert [(r.source, r.floor, r.min_votes) for r in active.body.keep_rating_rules] == [
            (RatingSource.IMDB, 75, 1000)
        ]

    async def test_a_deliberately_empty_rating_bar_is_left_alone(
        self, session: AsyncSession
    ) -> None:
        """An explicit empty list means the operator cleared their bars on purpose, not
        that the value is missing. Restoring it would put back a protection they
        deliberately removed, and would degrade every scan to tell them so."""
        stored = json.loads(DEFAULT_MOVIE_POLICY.model_dump_json())
        stored["keep_rating_rules"] = []

        await _store_policy(session, json.dumps(stored))

        active = await active_policy(session, "movie")

        assert active.repairs == ()
        assert active.repaired is False
        assert active.body.keep_rating_rules == ()


def _legacy_list_body() -> dict[str, Any]:
    """A stored body from before every list protected through its own keep rule. It
    carries the keep tags on the policy, plus the two retired list gates, both enabled."""
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
            created_at=utcnow(),
        )
    )
    await session.commit()


async def _seed_list_rows(session: AsyncSession) -> None:
    """The registry rows the conversion's rules must point at, under names the operator may
    have chosen. Resolution goes by what each row holds, these tags, that preset, never by
    spelling, which is the operator's to change, and never by age, which is theirs to
    change too."""
    await _add_list(session, "Films worth keeping", "imdb", {"preset": "top250"})
    await _add_list(
        session, "My tagged titles", "arr_tag", {"tags": ["reaper-keep"], "match": "any"}
    )


class TestALegacyListBodyIsConvertedOnLoad:
    """``active_policy`` composes ``convert_list_protections`` first, on the raw dict.
    ``keep_tags`` is a key ``Frozen`` forbids, so a merely-legacy body read without the
    conversion would fall back to the shipped default, a silent substitution that must
    never happen. The conversion is a repair like the others. It is flagged, it degrades
    the scan, and it is never adopted silently."""

    async def test_the_body_loads_with_its_gates_as_rules_and_is_flagged(
        self, session: AsyncSession
    ) -> None:
        await _seed_list_rows(session)
        await _store_policy(session, json.dumps(_legacy_list_body()))

        active = await active_policy(session, "movie")

        assert active.repairs == (PolicyRepair.LISTS_MIGRATED,)
        assert active.repaired is True  # the scan degrades on it
        assert active.name == "stored"
        # Each enabled gate became a rule naming the current list of its source, using
        # the operator's own names, resolved from the registry rather than assumed.
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
        """The IMDb chart is movies only, so migrating a TV body carries over the tag list
        but not the IMDb list. A TV rule naming the IMDb list could never match a season.
        The curated_list gate strips clean on the TV body, since its protection was never
        live there."""
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
    """A list added for something else must not inherit a rule nothing gave it.

    The conversion runs on every load of a legacy body and re-reads the registry each
    time, so which list it names is answered fresh whenever the registry changes.
    Resolving by age instead would be wrong: delete the tag list this converts, and the
    next arr-tag list becomes the oldest of its source, silently taking over an outright
    keep. Settings -> Lists reads this same conversion (``list_rules.usage``), so a row
    named this way would read "Keeps every title on it" for a list no rule mentions, and
    Remove would not be able to take it off, since the writer behind it refuses to touch
    a repaired policy on purpose.
    """

    async def test_a_decoy_older_than_the_tagged_list_does_not_take_its_rule(
        self, session: AsyncSession
    ) -> None:
        """Age decides nothing. The decoy is added first, and the list carries a tag the
        operator added beside the stored one, so neither position nor an exact set match
        is what finds it."""
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
        """The operator's own sequence. Add a list, then remove the one their tags became.

        Removing it takes the protection with it, and the scan then refuses to run with a
        retired gate it cannot answer. That is the loud, fail-closed exit this needs,
        instead of silently handing the protection to a list nobody chose.
        """
        await _seed_list_rows(session)
        await _add_list(session, "Saturday movie night", "arr_tag", {"tags": ["movie-night"]})
        await _store_policy(session, json.dumps(_legacy_list_body()))
        # Reads straight from the table. `list_config.all_lists` seeds the shipped
        # defaults on a registry that has never been seeded, which would put a second
        # tag list on the screen this test is about.
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
        """The retired curated_list gate named one shipped list, the IMDb Top 250. A list
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
        """An explicit empty ``keep_tags`` means the operator cleared it on purpose, so
        there is no protection to carry and no list to find. The gate may leave, because
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
    returns before ``convert_list_protections`` can run. Nothing points a rule at that
    collection, and the whitelisted gate that once covered it is retired, so a protection
    that worked on the previous release would silently stop firing on this one."""

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
        """No library synced, so a collection's media type is unknown. It stays on both
        policies, failing open. The narrowed cases are the two tests below."""
        await self._seed_plex_collection(session)

        active = await active_policy(session, media_type)

        values = {str(c.value) for c in active.body.protect_conditions if c.field == "on_list"}
        assert "Never Reap" in values
        # The shipped conditions are still there on top of the new one, and nothing is
        # flagged, because putting the rule back removes the loss rather than announcing
        # it. The shipped set is specific to the media type, since the IMDb chart is on
        # the movie default alone.
        default = DEFAULT_MOVIE_POLICY if media_type == "movie" else DEFAULT_TV_POLICY
        shipped = {str(c.value) for c in default.protect_conditions if c.field == "on_list"}
        assert shipped <= values
        assert active.repaired is False
        assert active.name == "default"

    async def test_saving_the_pace_settings_does_not_take_the_collections_rule_away(
        self, session: AsyncSession
    ) -> None:
        """The rule above is computed on the way out, so anything that writes a policy row
        has to write the computed body, not the shipped one. Writing the shipped body
        instead would silently drop the operator's keep-collection rule the moment
        recency starts returning that stored row, with ``repaired`` still False, so
        nothing would degrade and no notice would fire.
        """
        await self._seed_plex_collection(session)

        await save_profile_settings(session, ProfileSettings(grace_days=21))

        active = await active_policy(session, "movie")
        values = {str(c.value) for c in active.body.protect_conditions if c.field == "on_list"}
        assert "Never Reap" in values
        assert active.repaired is False

    async def test_a_collection_in_a_movie_library_lands_on_the_movie_policy_alone(
        self, session: AsyncSession
    ) -> None:
        """The library is synced as a movie library, so the collection holds movies only.
        Its keep rule seeds on the movie policy and not the TV one, where it could never
        match a season and would read as a protection the operator never chose."""
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
        """The mirror case. A collection in a show library seeds on the TV policy alone."""
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

    @pytest.mark.parametrize("blob", ["123", "null", "not json at all"])
    async def test_a_corrupt_plex_libraries_setting_does_not_crash_the_load(
        self, session: AsyncSession, blob: str
    ) -> None:
        """``active_policy`` must never raise, for any stored input. A malformed or
        non-list ``plex_libraries`` value, such as a restored backup or a hand-edit, makes
        ``get_plex_libraries`` raise ``ValueError`` or ``TypeError`` inside the scope
        read, which must not escape. It falls back to no scoping instead, so the
        collection keeps both policies, failing open exactly as the migration does on the
        same value. Each blob here is a real crash trigger. ``123`` and ``null`` parse to
        a non-iterable, and ``not json`` fails to parse at all."""
        await self._seed_plex_collection(session)  # "Never Reap" in "Films"
        session.add(AppSetting(key="plex_libraries", value_json=blob, updated_at=utcnow()))
        await session.commit()

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
        assert "Never Reap" in tv

    async def test_a_registry_it_cannot_read_leaves_the_shipped_rules_alone(
        self, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The direction this helper promises. It may add cover, never withdraw it.

        A read failure here is not the same as "no lists of your own". The only safe
        reading of a registry that cannot be read is the shipped set unchanged, so this
        test drives a real read failure and checks the default is returned, rather than
        only reasoning about it.
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
        """Leaving the shipped rules alone is right, but it is not enough on its own.

        The seeded collection above has no rule naming it in this body, so the scan is
        about to judge the whole library with that keep list doing nothing. The scan's
        own registry read is a separate query that can succeed even after a transient
        error here, so this case must still be flagged: it needs a log line and a
        degradation, not an empty ``repairs`` that lets the run complete clean.

        This is not a ``PolicyRepair``, since every member of that enum is answered by
        saving the policy, and saving fixes nothing here. It still reads as ``repaired``,
        because that is what degrades the scan and what stops ``list_rules`` from
        persisting a body that is missing the rules.
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
        """A read that succeeds and finds nothing of the operator's own falls back to the
        shipped conditions silently. It must not degrade every scan on an install that
        simply has no Plex lists."""
        active = await active_policy(session, "movie")

        assert active.lists_unreadable is False
        assert active.repaired is False

    async def test_a_watchlist_definition_is_kept_too(self, session: AsyncSession) -> None:
        """The other source ``_conversion_list_names`` returns as the operator's own. A
        watchlist spans the account and can hold both types, so its rule stays on both
        policies even with libraries synced. Scoping to one policy is a collection
        concern, not a watchlist one."""
        session.add(
            ListConfigModel(
                name="My watchlist",
                source="plex_watchlist",
                config_json="{}",
                enabled=True,
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
        """Both names are lower-cased before comparison, the same way every reader of a
        list name does it, so a duplicate rule cannot arrive by capitalization alone."""
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
