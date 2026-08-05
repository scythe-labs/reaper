# SPDX-License-Identifier: AGPL-3.0-or-later
"""The keep rules that make a list act, maintained beside the registry.

A list is defined on Settings -> Lists; what it does is an ``on_list`` keep rule on
Policy naming it. These pin the two maintenance moves and the fail-safe around them:

* adding a list writes NO rule -- the operator sets what it does on Policy, so a hand-added
  list reads "Not used by your policy yet" until they do and is never a silent protection;
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
    """The API is what keeps the registry and the policies telling one story: create writes
    no rule, rename follows the rule the operator set, delete detaches -- each in the same
    request."""

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

    @staticmethod
    def _keep_on_policy(client: TestClient, name: str) -> None:
        """What the operator does on Policy after adding the list: an outright keep rule
        naming it, in both policies. Seeded through the real save route so rename and delete
        act on a rule the policy actually carries, the way they do in the app."""
        for media_type in ("movie", "tv"):
            body = client.get("/api/policy", params={"media_type": media_type}).json()["body"]
            body["protect_conditions"] = [
                *body["protect_conditions"],
                {"field": "on_list", "op": Op.EQ.value, "value": name},
            ]
            r = client.post("/api/policy", json=body)
            assert r.status_code == 200, r.text

    def test_create_writes_no_rule_and_reads_unused(self, client: TestClient) -> None:
        """Adding a list is not a protection the operator did not ask for: it carries no
        policy use and no policy body names it until they set one."""
        made = self._make(client)

        assert made["policy_use"] == []
        assert self._use_for(client, "Keep") == []
        for media_type in ("movie", "tv"):
            body = client.get("/api/policy", params={"media_type": media_type}).json()["body"]
            values = [c["value"] for c in body["protect_conditions"] if c["field"] == "on_list"]
            assert "Keep" not in values, media_type

    def test_rename_carries_the_rules_to_the_new_name(self, client: TestClient) -> None:
        made = self._make(client)
        self._keep_on_policy(client, "Keep")

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
        self._keep_on_policy(client, "Keep")
        # The rule the delete has to strip is actually in force first, so the assertion
        # below pins the detach rather than passing on a policy that never named the list.
        for media_type in ("movie", "tv"):
            body = client.get("/api/policy", params={"media_type": media_type}).json()["body"]
            assert "Keep" in [
                c["value"] for c in body["protect_conditions"] if c["field"] == "on_list"
            ], media_type

        r = client.delete(f"/api/lists/configured/{made['id']}")

        assert r.status_code == 204
        for media_type in ("movie", "tv"):
            body = client.get("/api/policy", params={"media_type": media_type}).json()["body"]
            values = [c["value"] for c in body["protect_conditions"] if c["field"] == "on_list"]
            assert "Keep" not in values, media_type
