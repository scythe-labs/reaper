# SPDX-License-Identifier: AGPL-3.0-or-later
"""The keep rules that make a list act, maintained beside the registry.

A list is defined on Settings -> Lists; what it does is an ``on_list`` keep rule on
Policy naming it. These pin the three maintenance moves and the fail-safe around them:

* adding a list writes a keeps-it-outright rule into BOTH policies, through the editor's
  own append-only shape, so a pending approval bound to the old hash refuses to execute
  (rule 113);
* renaming a list re-spells every rule naming it, both strengths, so a rename never turns
  a live protection into a rule naming nothing (rule 25);
* deleting a list deletes its rules, and the API route pairs the two so neither can
  happen alone;
* a policy Reaper had to REPAIR to load is never written back from here -- persisting a
  shim's output plus one rule would silently adopt the whole repair without the
  operator's review (rule 65).
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine as sa_create_engine
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from reaper.clock import utcnow
from reaper.config import Settings
from reaper.db.base import Base
from reaper.db.models import Policy as PolicyModel
from reaper.db.session import create_engine, create_session_factory
from reaper.engine.fields import Op
from reaper.engine.policy import (
    DEFAULT_MOVIE_POLICY,
    DEFAULT_TV_POLICY,
    GradedKeepSpec,
    PolicyBody,
)
from reaper.main import create_app
from reaper.services import list_rules, profiles
from tests._auth import login


@pytest.fixture
async def session(tmp_path: Path) -> Iterator[AsyncSession]:
    settings = Settings(data_dir=tmp_path, secret_key="k")  # type: ignore[call-arg]
    sync = sa_create_engine(settings.sync_database_url)
    Base.metadata.create_all(sync)
    sync.dispose()
    engine = create_engine(settings)
    factory: async_sessionmaker[AsyncSession] = create_session_factory(engine)
    async with factory() as s:
        yield s
    await engine.dispose()


@pytest.fixture
def client(tmp_path: Path) -> Iterator[TestClient]:
    settings = Settings(data_dir=tmp_path, secret_key="k")  # type: ignore[call-arg]
    engine = sa_create_engine(settings.sync_database_url)
    Base.metadata.create_all(engine)
    engine.dispose()
    with TestClient(create_app(settings)) as c:
        login(c, settings)
        yield c


async def _save_policy(session: AsyncSession, body: PolicyBody) -> None:
    """A stored row, through the same append-only shape every save uses."""
    session.add(
        PolicyModel(
            policy_hash=body.policy_hash(),
            body_json=body.model_dump_json(),
            media_type=body.media_type,
            name="default",
            created_at=utcnow(),
        )
    )
    await session.commit()


async def _policy_count(session: AsyncSession) -> int:
    rows = await session.execute(select(PolicyModel))
    return len(list(rows.scalars()))


async def _on_list_rules(session: AsyncSession, media_type: str) -> list[str]:
    active = await profiles.active_policy(session, media_type)
    return [str(c.value) for c in active.body.protect_conditions if c.field == "on_list"]


class TestAttach:
    async def test_attach_adds_one_rule_per_media_type_as_new_rows(
        self, session: AsyncSession
    ) -> None:
        """Append-only, hash moves: the rule lands as a NEW policy row per media type, so
        a pending approval bound to the previous hash refuses to execute (rule 113)."""
        await _save_policy(session, DEFAULT_MOVIE_POLICY)
        await _save_policy(session, DEFAULT_TV_POLICY)
        before_movie = (await profiles.active_policy(session, "movie")).body.policy_hash()

        await list_rules.attach_list(session, "My new list")

        assert await _policy_count(session) == 4  # two originals, two appended
        for media_type in ("movie", "tv"):
            assert "My new list" in await _on_list_rules(session, media_type)
        after_movie = (await profiles.active_policy(session, "movie")).body.policy_hash()
        assert after_movie != before_movie

    async def test_attach_does_not_duplicate_an_existing_rule(self, session: AsyncSession) -> None:
        """Matched case-folded (rule 88): a second rule for one list would double the row
        on Policy for no change in effect."""
        await _save_policy(session, DEFAULT_MOVIE_POLICY)
        await _save_policy(session, DEFAULT_TV_POLICY)
        await list_rules.attach_list(session, "My new list")
        rows_after_first = await _policy_count(session)

        await list_rules.attach_list(session, "  MY NEW LIST  ")

        assert await _policy_count(session) == rows_after_first
        assert (await _on_list_rules(session, "movie")).count("My new list") == 1

    async def test_attach_skips_a_policy_that_needed_repair(self, session: AsyncSession) -> None:
        """A repaired body is not the one the operator saved, and persisting it plus one
        rule would adopt the whole repair without their review (rule 65). The other media
        type's healthy policy still gains its rule."""
        session.add(
            PolicyModel(
                policy_hash="broken",
                body_json="not json at all",
                media_type="movie",
                name="default",
                created_at=utcnow(),
            )
        )
        await session.commit()
        await _save_policy(session, DEFAULT_TV_POLICY)
        assert (await profiles.active_policy(session, "movie")).repaired

        await list_rules.attach_list(session, "My new list")

        # The broken movie row was never written back...
        movie_rows = [
            r
            for r in (await session.execute(select(PolicyModel))).scalars()
            if r.media_type == "movie"
        ]
        assert [r.body_json for r in movie_rows] == ["not json at all"]
        # ...while the healthy TV policy gained the rule.
        assert "My new list" in await _on_list_rules(session, "tv")


class TestRename:
    @staticmethod
    def _body_with_both_strengths(base: PolicyBody, name: str) -> PolicyBody:
        return PolicyBody.model_validate(
            {
                **base.model_dump(mode="json"),
                "protect_conditions": [
                    {"field": "on_list", "op": Op.EQ.value, "value": name},
                ],
                "graded_keeps": [
                    GradedKeepSpec(
                        name="lean",
                        field="on_list",
                        value=name,
                        max_discount=15,
                        floor=0,
                        saturate_at=1,
                    ).model_dump(mode="json")
                ],
            }
        )

    async def test_rename_respells_conditions_and_graded_keeps(self, session: AsyncSession) -> None:
        await _save_policy(session, self._body_with_both_strengths(DEFAULT_MOVIE_POLICY, "Old"))
        await _save_policy(session, self._body_with_both_strengths(DEFAULT_TV_POLICY, "OLD"))

        await list_rules.rename_list(session, "old", "New name")

        for media_type in ("movie", "tv"):
            body = (await profiles.active_policy(session, media_type)).body
            assert [str(c.value) for c in body.protect_conditions] == ["New name"], media_type
            assert [k.value for k in body.graded_keeps] == ["New name"], media_type

    async def test_a_rename_to_the_same_spelling_writes_nothing(
        self, session: AsyncSession
    ) -> None:
        await _save_policy(session, self._body_with_both_strengths(DEFAULT_MOVIE_POLICY, "Same"))
        before = await _policy_count(session)

        await list_rules.rename_list(session, "Same", "  SAME ")

        assert await _policy_count(session) == before

    async def test_rename_skips_a_policy_that_needed_repair(self, session: AsyncSession) -> None:
        session.add(
            PolicyModel(
                policy_hash="broken",
                body_json="not json at all",
                media_type="movie",
                name="default",
                created_at=utcnow(),
            )
        )
        await session.commit()

        await list_rules.rename_list(session, "Old", "New")

        movie_rows = [
            r
            for r in (await session.execute(select(PolicyModel))).scalars()
            if r.media_type == "movie"
        ]
        assert [r.body_json for r in movie_rows] == ["not json at all"]


class TestDetach:
    async def test_detach_removes_both_strengths_in_both_policies(
        self, session: AsyncSession
    ) -> None:
        await _save_policy(
            session, TestRename._body_with_both_strengths(DEFAULT_MOVIE_POLICY, "Going")
        )
        await _save_policy(
            session, TestRename._body_with_both_strengths(DEFAULT_TV_POLICY, "going")
        )

        await list_rules.detach_list(session, "GOING")

        for media_type in ("movie", "tv"):
            body = (await profiles.active_policy(session, media_type)).body
            assert body.protect_conditions == (), media_type
            assert body.graded_keeps == (), media_type

    async def test_detach_leaves_other_lists_rules_alone(self, session: AsyncSession) -> None:
        body = PolicyBody.model_validate(
            {
                **DEFAULT_MOVIE_POLICY.model_dump(mode="json"),
                "protect_conditions": [
                    {"field": "on_list", "op": Op.EQ.value, "value": "Going"},
                    {"field": "on_list", "op": Op.EQ.value, "value": "Staying"},
                ],
            }
        )
        await _save_policy(session, body)

        await list_rules.detach_list(session, "Going")

        assert await _on_list_rules(session, "movie") == ["Staying"]

    async def test_detach_skips_a_policy_that_needed_repair(self, session: AsyncSession) -> None:
        session.add(
            PolicyModel(
                policy_hash="broken",
                body_json="not json at all",
                media_type="tv",
                name="default",
                created_at=utcnow(),
            )
        )
        await session.commit()

        await list_rules.detach_list(session, "Going")

        tv_rows = [
            r
            for r in (await session.execute(select(PolicyModel))).scalars()
            if r.media_type == "tv"
        ]
        assert [r.body_json for r in tv_rows] == ["not json at all"]


class TestUsage:
    async def test_usage_reports_each_rule_keyed_by_the_casefolded_name(
        self, session: AsyncSession
    ) -> None:
        """The shape ``api.schemas.ListPolicyUseOut`` serializes: hard rules carry no
        points, a lean carries its discount."""
        await _save_policy(
            session, TestRename._body_with_both_strengths(DEFAULT_MOVIE_POLICY, "My List")
        )

        used = await list_rules.usage(session)

        assert used["my list"] == [
            {"media_type": "movie", "strength": "hard", "points": None},
            {"media_type": "movie", "strength": "lean", "points": 15},
        ]

    async def test_a_list_no_rule_names_is_absent(self, session: AsyncSession) -> None:
        await _save_policy(session, DEFAULT_MOVIE_POLICY)

        used = await list_rules.usage(session)

        assert "unnamed list" not in used


class TestTheRoutesPairTheTwoSurfaces:
    """The API is what keeps the registry and the policies telling one story: create
    attaches, rename follows, delete detaches -- each in the same request."""

    @staticmethod
    def _use_for(client: TestClient, name: str) -> list[dict[str, object]]:
        rows = client.get("/api/lists/configured").json()
        [row] = [r for r in rows if r["name"] == name]
        return list(row["policy_use"])

    @staticmethod
    def _make(client: TestClient, name: str = "Keep") -> dict[str, object]:
        r = client.post(
            "/api/lists/configured",
            json={"name": name, "source": "arr_tag", "config": {"tags": ["keep"]}},
        )
        assert r.status_code == 201, r.text
        return dict(r.json())

    def test_create_attaches_the_rule_and_answers_with_its_use(self, client: TestClient) -> None:
        made = self._make(client)

        assert {(u["media_type"], u["strength"]) for u in made["policy_use"]} == {
            ("movie", "hard"),
            ("tv", "hard"),
        }
        assert self._use_for(client, "Keep") == made["policy_use"]

    def test_rename_carries_the_rules_to_the_new_name(self, client: TestClient) -> None:
        made = self._make(client)

        r = client.patch(f"/api/lists/configured/{made['id']}", json={"name": "Keep forever"})

        assert r.status_code == 200
        assert len(self._use_for(client, "Keep forever")) == 2
        # The policy itself names the new spelling; nothing still says "Keep".
        for media_type in ("movie", "tv"):
            body = client.get("/api/policy", params={"media_type": media_type}).json()["body"]
            values = [c["value"] for c in body["protect_conditions"] if c["field"] == "on_list"]
            assert "Keep forever" in values, media_type
            assert "Keep" not in values, media_type

    def test_delete_detaches_the_rules_with_the_row(self, client: TestClient) -> None:
        made = self._make(client)

        r = client.delete(f"/api/lists/configured/{made['id']}")

        assert r.status_code == 204
        for media_type in ("movie", "tv"):
            body = client.get("/api/policy", params={"media_type": media_type}).json()["body"]
            values = [c["value"] for c in body["protect_conditions"] if c["field"] == "on_list"]
            assert "Keep" not in values, media_type
