# SPDX-License-Identifier: AGPL-3.0-or-later
"""The seven getters that resolve "stored wins, else the environment seed", in one table.

``app_settings`` writes that precedence seven times, and the seven do not share one shape,
so two helpers hold the parts that do repeat. The three on/off switches call
``_env_seeded_switch``, which owns the order. Both Discord getters call
``_decrypted_or_absent``, which owns what a credential that will not decrypt means. The
last two reach neither: ``get_trusted_proxies`` decodes the seed and cleans the stored
side, and ``get_timezone`` validates both and falls through to a third source.

The gate below is what neither helper buys. ``_env_seeded_getters`` reads the module and
collects every function that calls ``_get`` and takes a ``Settings``, so an eighth one
fails this file until its case is written, including one that calls no helper at all,
which is what an author who never read this docstring writes. The ``_get`` call is the
load-bearing half. Hiding it inside a helper would have left this walk collecting only
four.

Getting the precedence backwards is silent, and it does not fail in a random direction. A
stored ``false`` mistaken for "nothing stored" hands the answer back to the environment, so
every mutation of it widens what the app may do: a deletion switch turned off in the UI
re-arms itself on the next restart, and a proxy list the operator cleared goes on deciding
auth.
"""

from __future__ import annotations

import ast
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from reaper.config import Settings
from reaper.crypto import SecretBox
from reaper.db.base import Base
from reaper.db.session import create_engine, create_session_factory
from reaper.services import app_settings

#: Two distinct webhook URLs, so a case cannot pass by reading the wrong one. The host is
#: Discord's because ``api/settings.py`` validates it there. Nothing here posts.
_SEED_WEBHOOK = "https://discord.com/api/webhooks/1/seed-token"
_STORED_WEBHOOK = "https://discord.com/api/webhooks/2/stored-token"


@pytest.fixture
async def factory(tmp_path: Path) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    settings = Settings(data_dir=tmp_path, secret_key="test-key")
    engine = create_engine(settings)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield create_session_factory(engine)
    await engine.dispose()


def _seeded(tmp_path: Path, **overrides: object) -> Settings:
    """A ``Settings`` carrying one seed, always a value the shipped default is not. Every
    switch below ships off, and every string ships empty, so an omitted seed and a correct
    one would otherwise produce the same answer."""
    return Settings(data_dir=tmp_path, secret_key="test-key", **overrides)  # type: ignore[arg-type]


class TestTheStoredValueWinsOverTheEnvironmentSeed:
    """One case per getter. Each stores a value the seed disagrees with, so the assertion can
    only hold if the stored row was read first."""

    async def test_deletion_stays_off_after_the_ui_turned_it_off(
        self, factory: async_sessionmaker[AsyncSession], tmp_path: Path
    ) -> None:
        """The one that costs a file. ``REAPER_DESTRUCTIVE_ACTIONS_ENABLED`` ships an install
        armed, and a stored ``false`` is the operator disarming it. Reading that false as
        "nothing stored" re-arms deletion at the next restart, with nothing said.

        ``test_startup_log.py`` pins the same precedence through a whole boot. This pins the
        getter, which is the level a new caller reaches."""
        armed = _seeded(tmp_path, destructive_actions_enabled=True)
        async with factory() as session:
            assert await app_settings.destructive_enabled(session, armed) is True
            await app_settings.set_destructive_enabled(session, enabled=False)
            assert await app_settings.destructive_enabled(session, armed) is False

    async def test_forwarded_headers_stay_off_after_the_ui_turned_them_off(
        self, factory: async_sessionmaker[AsyncSession], tmp_path: Path
    ) -> None:
        trusting = _seeded(tmp_path, proxy_trust_enabled=True)
        async with factory() as session:
            assert await app_settings.proxy_trust_enabled(session, trusting) is True
            await app_settings.set_proxy_trust_enabled(session, enabled=False)
            assert await app_settings.proxy_trust_enabled(session, trusting) is False

    async def test_an_emptied_proxy_list_is_kept_rather_than_reverting_to_the_seed(
        self, factory: async_sessionmaker[AsyncSession], tmp_path: Path
    ) -> None:
        """An explicit empty list is a choice, never "nothing stored". Both states are
        reachable, because ``GeneralSettingsIn.trusted_proxies`` defaults to ``None`` and
        stores ``[]`` as itself, so an operator who clears the list while trust stays on has
        said to trust nobody. Falling back to ``REAPER_TRUSTED_PROXIES`` there leaves a
        forwarded header from a proxy they removed still deciding auth.

        The getter's docstring already claimed this, and nothing tested it. Swapping its
        ``value is None`` for ``not value`` passed the whole suite."""
        seeded = _seeded(tmp_path, trusted_proxies="10.0.0.5, 172.16.0.0/12")
        async with factory() as session:
            assert await app_settings.get_trusted_proxies(session, seeded) == [
                "10.0.0.5",
                "172.16.0.0/12",
            ]
            await app_settings.set_trusted_proxies(session, [])
            assert await app_settings.get_trusted_proxies(session, seeded) == []

    async def test_the_shelf_opt_in_keeps_a_stored_off_over_an_enabling_seed(
        self, factory: async_sessionmaker[AsyncSession], tmp_path: Path
    ) -> None:
        env_on = _seeded(tmp_path, allow_unarmed_leaving_soon=True)
        async with factory() as session:
            assert await app_settings.leaving_soon_unarmed(session, env_on) is True
            await app_settings.set_leaving_soon_unarmed(session, allowed=False)
            assert await app_settings.leaving_soon_unarmed(session, env_on) is False

    async def test_the_stored_zone_wins_over_the_seed(
        self, factory: async_sessionmaker[AsyncSession], tmp_path: Path
    ) -> None:
        """Both sides are validated here, so the seed can also answer for a corrupt stored
        value. That fall-through is ``test_timezone_setting.py``'s. This pins only the order
        two *valid* zones resolve in, which is what the walk below needs a case for."""
        seeded = _seeded(tmp_path, timezone="Asia/Tokyo")
        async with factory() as session:
            assert await app_settings.get_timezone(session, seeded) == "Asia/Tokyo"
            await app_settings.set_timezone(session, "Europe/Paris")
            assert await app_settings.get_timezone(session, seeded) == "Europe/Paris"

    async def test_a_webhook_edited_in_the_ui_survives_a_stale_environment_variable(
        self, factory: async_sessionmaker[AsyncSession], tmp_path: Path
    ) -> None:
        """The seed is imported once on first read and the database is the truth afterward, so
        a URL changed in the UI must not be clobbered by the env var it was seeded from."""
        box = SecretBox("test-key")
        seeded = _seeded(tmp_path, discord_webhook=_SEED_WEBHOOK)
        async with factory() as session:
            assert await app_settings.get_discord_webhook(session, box, seeded) == _SEED_WEBHOOK
            await app_settings.set_discord_webhook(session, box, _STORED_WEBHOOK)
            assert await app_settings.get_discord_webhook(session, box, seeded) == _STORED_WEBHOOK

    async def test_a_webhook_that_no_longer_decrypts_reads_as_absent_beside_a_live_seed(
        self, factory: async_sessionmaker[AsyncSession], tmp_path: Path
    ) -> None:
        """``has_discord_webhook``'s precedence is only observable here. With a readable stored
        value and a seed both present, either order answers True, so this drives the case where
        they part: a value written under a key that has since rotated. Stored-first says "not
        configured", which is what ``get_discord_webhook`` will do on every send. Seed-first
        says "connected" and the Settings panel then promises notifications that silently never
        post."""
        rotated_away, current = SecretBox("old-key"), SecretBox("new-key")
        seeded = _seeded(tmp_path, discord_webhook=_SEED_WEBHOOK)
        async with factory() as session:
            await app_settings.set_discord_webhook(session, rotated_away, _STORED_WEBHOOK)
            assert await app_settings.has_discord_webhook(session, current, seeded) is False
            assert await app_settings.get_discord_webhook(session, current, seeded) is None

    @pytest.mark.parametrize("blank", ["", "   ", "\n"])
    async def test_a_stored_webhook_that_decrypts_to_nothing_is_not_configured(
        self, factory: async_sessionmaker[AsyncSession], tmp_path: Path, blank: str
    ) -> None:
        """The two branches of ``get_discord_webhook`` answered "nothing configured" two ways.

        The seed branch treats an empty string as absent. The stored branch returned it
        instead. Every consumer tests ``is None``, and ``notify.build_notifier`` gates on
        ``webhook is None``, so an empty string built a ``DiscordNotifier("")`` that posts
        nowhere while the Settings panel said connected. Absent and present-but-empty were
        conflated in one direction and separated in the other.

        Written through ``box.encrypt`` directly, because no writer can reach this state
        today. ``api/settings.py`` validates the URL, and the seed write is guarded. That is
        exactly why the pin belongs here. The guard is the only thing keeping the shape
        unreachable, and nothing was watching it.

        The seed is live beside it, so the stored row cannot merely be falling through to a
        second empty answer.
        """
        box = SecretBox("test-key")
        seeded = _seeded(tmp_path, discord_webhook=_SEED_WEBHOOK)
        async with factory() as session:
            await app_settings._set(session, app_settings.DISCORD_WEBHOOK_KEY, box.encrypt(blank))

            assert await app_settings.get_discord_webhook(session, box, seeded) is None
            assert await app_settings.has_discord_webhook(session, box, seeded) is False

    async def test_a_stored_api_key_is_not_touched_by_that_clause(
        self, factory: async_sessionmaker[AsyncSession]
    ) -> None:
        """``_decrypted_or_absent`` is shared, and the clause deliberately is not.

        It answers a decrypt *failure*, and ``get_api_key`` reads the same helper. The
        emptiness check found no equivalent problem for API keys, so it sits at the
        webhook's two call sites rather than in the helper both go through. This pins that
        a key still comes back exactly as stored.
        """
        box = SecretBox("test-key")
        async with factory() as session:
            await app_settings.set_api_key(session, box, "rk_live_abc")
            assert await app_settings.get_api_key(session, box) == "rk_live_abc"


#: The seven above, named so a matcher that stops collecting is caught rather than
#: passing vacuously. A subtraction against an empty population finds nothing missing.
#: ``destructive_enabled``, ``get_timezone``, ``proxy_trust_enabled``, ``get_trusted_proxies``,
#: ``get_discord_webhook``, ``has_discord_webhook``, ``leaving_soon_unarmed``.
_EXPECTED_ENV_SEEDED_GETTERS = 7


def _env_seeded_getters() -> set[str]:
    """Every ``app_settings`` function that reads a stored row and takes the environment.

    This reads off the AST rather than the source text. It looks for a call to ``_get`` by
    name, and a parameter whose annotation mentions ``Settings``. Matching the *annotation*
    rather than the parameter's name is what keeps a getter visible that spells the
    argument something else.

    ``ast.walk`` runs instead of the module's ``body``, so a definition nested in a class or
    a ``try:`` is collected too. The count pin cannot cover every hole though. A member
    that never entered the walk leaves the population at seven, so the subtraction finds
    nothing missing and the gate reads green while the new getter has no case at all.

    Two things are invisible to it, and neither is spelled in the module today: a getter
    that reaches the environment through something other than a parameter, and one that
    reads its row through a helper other than ``_get``. Either would need this widened.
    """
    source = Path(app_settings.__file__).read_text(encoding="utf-8")
    found = set()
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.AsyncFunctionDef | ast.FunctionDef) or node.name == "_get":
            continue
        reads_a_row = any(
            isinstance(call, ast.Call)
            and isinstance(call.func, ast.Name)
            and call.func.id == "_get"
            for call in ast.walk(node)
        )
        args = node.args
        takes_the_environment = any(
            arg.annotation is not None and "Settings" in ast.unparse(arg.annotation)
            for arg in [*args.posonlyargs, *args.args, *args.kwonlyargs]
        )
        if reads_a_row and takes_the_environment:
            found.add(node.name)
    return found


def _getters_driven_here() -> set[str]:
    """Every ``app_settings.<name>`` this file names, read off its own source rather than
    listed by hand. A hand-kept list is a claim of coverage that nothing checks.

    It reads a reference, not an assertion, so a getter called here and never asserted on would
    satisfy it. What that leaves uncovered is one author writing a call and no `assert` beside
    it, against a list that goes stale on its own.
    """
    source = Path(__file__).read_text(encoding="utf-8")
    return {
        node.attr
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name)
        and node.value.id == "app_settings"
    }


def test_every_env_seeded_getter_is_driven_above() -> None:
    """An eighth getter fails here until its case is written.

    A helper binds only the getters that call it, so an author who writes the precedence out by
    hand is exactly as free to read a stored ``false`` as "nothing stored" as the three were
    before ``_env_seeded_switch`` existed. The helper gives that author the right thing to call
    and this makes them look, which is why the tree carries both.
    """
    population = _env_seeded_getters()
    assert len(population) == _EXPECTED_ENV_SEEDED_GETTERS, (
        f"expected {_EXPECTED_ENV_SEEDED_GETTERS} env-seeded getters in "
        f"{Path(app_settings.__file__).name}, collected {len(population)}: {sorted(population)}."
        "\nIf one was added or removed, move the number and this docstring's list with it. If"
        " not, the matcher stopped reading a shape the module now uses."
    )
    missing = sorted(population - _getters_driven_here())
    assert not missing, (
        "these app_settings getters resolve `stored wins, else the environment seed` and no"
        f" case in {Path(__file__).name} drives them: {', '.join(missing)}."
        "\nAdd one that stores a value the seed disagrees with. Reading a stored value as"
        " `nothing stored` falls back to the environment, which widens what the app may do."
    )
