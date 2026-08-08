# SPDX-License-Identifier: AGPL-3.0-or-later
"""The manual whitelist.

The one guarantee that matters: an item the owner spared is never reaped -- and that has
to hold even in the window between "spare" and the next scan, when a frozen snapshot's
candidate row still reads ``condemn``. So the planner is tested for the exclusion
directly, not just the service.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterable
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from reaper.clock import utcnow
from reaper.config import Settings
from reaper.db.base import Base
from reaper.db.models import ActionStep, Candidate, Instance, InstanceKind, Snapshot, WhitelistEntry
from reaper.db.session import create_engine, create_session_factory
from reaper.services import whitelist
from reaper.services.planner import build_plan

GB = 1024**3


@pytest.fixture
async def session(tmp_path: Path) -> AsyncIterator[AsyncSession]:
    settings = Settings(data_dir=tmp_path, secret_key="test-key")
    engine = create_engine(settings)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory: async_sessionmaker[AsyncSession] = create_session_factory(engine)
    async with factory() as s:
        yield s
    await engine.dispose()


async def _seed_instances(session: AsyncSession, media_keys: Iterable[str]) -> None:
    """Seed the Instance rows a plan resolves each candidate's media_key against.

    A scan only condemns items from instances that exist, so a real plan reads a live row for
    every candidate, and ``build_plan`` refuses a movie whose Radarr is gone. These tests
    fabricate candidates directly, so they seed the matching instances. One row per id -- the
    planner keys on the id alone, never the kind.
    """
    for instance_id in sorted({int(key.split(":")[1]) for key in media_keys}):
        session.add(
            Instance(
                id=instance_id,
                kind=InstanceKind.RADARR,
                name=f"i{instance_id}",
                base_url="https://arr.test",
                api_key_enc="enc",
                created_at=utcnow(),
            )
        )
    await session.flush()


async def _snapshot_with(session: AsyncSession, condemned: list[tuple[str, int]]) -> int:
    now = utcnow()
    await _seed_instances(session, [media_key for media_key, _ in condemned])
    snapshot = Snapshot(
        created_at=now,
        policy_hash="p" * 64,
        scoring_hash="s" * 64,
        horizon_at=now,
        item_count=len(condemned),
    )
    session.add(snapshot)
    await session.flush()
    for i, (media_key, size) in enumerate(condemned):
        session.add(
            Candidate(
                snapshot_id=snapshot.id,
                media_key=media_key,
                title=f"Movie {i}",
                media_type="movie",
                size_bytes=size,
                verdict="condemn",
                score=90,
                coverage_bp=10_000,
                explanation_json="{}",
                created_at=now,
            )
        )
    await session.flush()
    return snapshot.id


class TestService:
    async def test_spare_then_read_back(self, session: AsyncSession) -> None:
        await whitelist.spare(session, media_key="radarr:1:7", title="Kept", note="a favorite")
        assert await whitelist.overrides(session) == {"radarr:1:7": "spare"}
        spared = await whitelist.list_spared(session)
        assert (spared[0].title, spared[0].note) == ("Kept", "a favorite")

    async def test_spare_is_idempotent_and_updates_the_note(self, session: AsyncSession) -> None:
        await whitelist.spare(session, media_key="radarr:1:7", title="Kept", note="one")
        await whitelist.spare(session, media_key="radarr:1:7", title="Kept", note="two")
        spared = await whitelist.list_spared(session)
        assert len(spared) == 1
        assert spared[0].note == "two"

    async def test_unspare_removes_and_reports(self, session: AsyncSession) -> None:
        await whitelist.spare(session, media_key="radarr:1:7", title="Kept", note=None)
        assert await whitelist.unspare(session, media_key="radarr:1:7") is True
        assert await whitelist.unspare(session, media_key="radarr:1:7") is False
        assert await whitelist.overrides(session) == {}


class TestOverrideService:
    async def test_reap_override_reads_back_separately_from_spare(
        self, session: AsyncSession
    ) -> None:
        await whitelist.spare(session, media_key="radarr:1:7", title="Kept", note=None)
        await whitelist.set_override(
            session, media_key="radarr:1:9", title="Gone", decision="reap", note="done with it"
        )
        assert await whitelist.overrides(session) == {"radarr:1:7": "spare", "radarr:1:9": "reap"}
        assert await whitelist.override_for(session, "radarr:1:9") == "reap"
        assert await whitelist.override_for(session, "radarr:1:404") is None

    async def test_setting_reap_flips_a_spare_in_place(self, session: AsyncSession) -> None:
        await whitelist.spare(session, media_key="radarr:1:7", title="Kept", note=None)
        await whitelist.set_override(
            session, media_key="radarr:1:7", title="Kept", decision="reap", note=None
        )
        assert await whitelist.overrides(session) == {"radarr:1:7": "reap"}
        # is_spared reflects the decision, not mere presence.
        assert await whitelist.is_spared(session, "radarr:1:7") is False

    async def test_remove_override_clears_either_decision(self, session: AsyncSession) -> None:
        await whitelist.set_override(
            session, media_key="radarr:1:9", title="Gone", decision="reap", note=None
        )
        assert await whitelist.remove_override(session, media_key="radarr:1:9") is True
        assert await whitelist.overrides(session) == {}


class TestTimedSpare:
    async def test_a_forever_spare_stores_no_expiry(self, session: AsyncSession) -> None:
        # spare_days defaults to 0 -- forever, the original behavior -- so the column stays NULL.
        entry = await whitelist.spare(session, media_key="radarr:1:7", title="Kept", note=None)
        assert entry.spare_expires_at is None

    async def test_a_timed_spare_stores_an_expiry_that_many_days_out(
        self, session: AsyncSession
    ) -> None:
        # Timestamps are stored as whole epoch seconds, so anchor the clock at second precision.
        t0 = utcnow().replace(microsecond=0)
        entry = await whitelist.spare(
            session, media_key="radarr:1:7", title="Kept", note=None, spare_days=30, now=t0
        )
        assert entry.spare_expires_at == t0 + timedelta(days=30)
        # It is still an active spare everywhere live -- the clock is realized only at scan.
        assert await whitelist.overrides(session) == {"radarr:1:7": "spare"}
        assert await whitelist.spare_expiries(session) == {"radarr:1:7": t0 + timedelta(days=30)}

    async def test_overrides_effective_at_drops_an_expired_spare_but_keeps_a_live_one(
        self, session: AsyncSession
    ) -> None:
        t0 = utcnow()
        await whitelist.spare(
            session, media_key="radarr:1:7", title="Short", note=None, spare_days=1, now=t0
        )
        await whitelist.spare(
            session, media_key="radarr:1:8", title="Long", note=None, spare_days=90, now=t0
        )
        after = t0 + timedelta(days=2)
        # Live: both still spared (fail toward keeping). At-scan: the 1-day spare is gone.
        assert await whitelist.overrides(session) == {
            "radarr:1:7": "spare",
            "radarr:1:8": "spare",
        }
        assert await whitelist.overrides_effective_at(session, after) == {"radarr:1:8": "spare"}

    async def test_a_forever_spare_never_expires_at_scan(self, session: AsyncSession) -> None:
        t0 = utcnow()
        await whitelist.spare(session, media_key="radarr:1:7", title="Kept", note=None, now=t0)
        far_future = t0 + timedelta(days=10_000)
        assert await whitelist.overrides_effective_at(session, far_future) == {
            "radarr:1:7": "spare"
        }

    async def test_flipping_a_timed_spare_to_reap_clears_the_expiry(
        self, session: AsyncSession
    ) -> None:
        await whitelist.spare(
            session, media_key="radarr:1:7", title="Kept", note=None, spare_days=30
        )
        entry = await whitelist.set_override(
            session, media_key="radarr:1:7", title="Kept", decision="reap", note=None
        )
        # A reap never expires, and must not inherit the spare's stale clock.
        assert entry.spare_expires_at is None
        assert await whitelist.spare_expiries(session) == {}

    async def test_spare_expiries_excludes_reaps(self, session: AsyncSession) -> None:
        await whitelist.set_override(
            session, media_key="radarr:1:9", title="Gone", decision="reap", note=None
        )
        assert await whitelist.spare_expiries(session) == {}

    async def test_effective_spare_expiry_follows_override_precedence(self) -> None:
        t_show = utcnow() + timedelta(days=10)
        t_season = utcnow() + timedelta(days=3)
        decisions = {"sonarr:1:88": "spare", "sonarr:1:88:3": "spare"}
        expiries = {"sonarr:1:88": t_show, "sonarr:1:88:3": t_season}
        # A season's own spare wins over its show's, expiry and all.
        assert whitelist.effective_spare_expiry("sonarr:1:88:3", decisions, expiries) == t_season
        # A season with no spare of its own inherits the show spare's expiry.
        assert whitelist.effective_spare_expiry("sonarr:1:88:2", decisions, expiries) == t_show

    async def test_effective_spare_expiry_is_none_for_a_forever_spare(self) -> None:
        decisions = {"radarr:1:7": "spare"}
        assert (
            whitelist.effective_spare_expiry("radarr:1:7", decisions, {"radarr:1:7": None}) is None
        )


class TestCoveringSpareExpiry:
    """When the item stops being KEPT, which is not the same question as which spare is in
    force. Precedence picks the row Reaper reads; when that row expires the other one is still
    on file, so a color or a sentence about the item's fate has to ask this one instead."""

    SHOW = "sonarr:1:88"
    SEASON = "sonarr:1:88:3"

    def test_a_forever_show_spare_outlasts_a_spent_season_spare(self) -> None:
        # The case this function exists for: the season's own spare has run out, the show's
        # never does, so the file is kept forever. Precedence would answer "expired".
        spent = utcnow() - timedelta(days=2)
        decisions = {self.SHOW: "spare", self.SEASON: "spare"}
        expiries: dict[str, datetime | None] = {self.SHOW: None, self.SEASON: spent}
        assert whitelist.effective_spare_expiry(self.SEASON, decisions, expiries) == spent
        assert whitelist.covering_spare_expiry(self.SEASON, decisions, expiries) is None

    def test_a_forever_season_spare_wins_over_a_timed_show_spare(self) -> None:
        # Forever beats every date whichever level holds it.
        decisions = {self.SHOW: "spare", self.SEASON: "spare"}
        expiries: dict[str, datetime | None] = {
            self.SHOW: utcnow() + timedelta(days=10),
            self.SEASON: None,
        }
        assert whitelist.covering_spare_expiry(self.SEASON, decisions, expiries) is None

    def test_it_takes_the_later_of_two_timed_spares_either_way_round(self) -> None:
        soon = utcnow() + timedelta(days=3)
        later = utcnow() + timedelta(days=30)
        decisions = {self.SHOW: "spare", self.SEASON: "spare"}
        assert (
            whitelist.covering_spare_expiry(
                self.SEASON, decisions, {self.SHOW: later, self.SEASON: soon}
            )
            == later
        )
        # And it is a max, not "prefer the show": the season's own may be the longer one.
        assert (
            whitelist.covering_spare_expiry(
                self.SEASON, decisions, {self.SHOW: soon, self.SEASON: later}
            )
            == later
        )

    def test_a_show_set_to_reap_contributes_no_cover(self) -> None:
        # The gate that keeps the fix honest. A season spare lapsing under a REAPED show really
        # does hand the file back, so it must go on reading as expired -- testing the show's
        # decision, not merely finding an expiry for its key, is what makes that true.
        spent = utcnow() - timedelta(days=2)
        decisions = {self.SHOW: "reap", self.SEASON: "spare"}
        expiries: dict[str, datetime | None] = {self.SHOW: None, self.SEASON: spent}
        assert whitelist.covering_spare_expiry(self.SEASON, decisions, expiries) == spent

    def test_an_uncovered_key_falls_back_to_the_precedence_answer(self) -> None:
        # Callers must ask only about spared items. One that slips through must not read
        # "kept forever" out of a key nothing spares.
        decisions = {self.SEASON: "reap"}
        assert whitelist.covering_spare_expiry(self.SEASON, decisions, {}) is None
        assert whitelist.covering_spare_expiry("radarr:1:7", {}, {}) is None

    def test_a_movie_answers_from_its_own_spare_alone(self) -> None:
        spent = utcnow() - timedelta(days=1)
        decisions = {"radarr:1:7": "spare"}
        expiries: dict[str, datetime | None] = {"radarr:1:7": spent}
        assert whitelist.covering_spare_expiry("radarr:1:7", decisions, expiries) == spent


class TestPurgeExpiredSpares:
    """The durable half of realizing a timed spare (B-1): the scan drops an expired spare from
    the map it judges on AND deletes the row, so every live consumer converges."""

    async def test_purges_only_expired_spares_and_returns_their_keys(
        self, session: AsyncSession
    ) -> None:
        t0 = utcnow()
        await whitelist.spare(
            session, media_key="radarr:1:7", title="Short", note=None, spare_days=1, now=t0
        )
        await whitelist.spare(
            session, media_key="radarr:1:8", title="Long", note=None, spare_days=90, now=t0
        )
        await whitelist.spare(session, media_key="radarr:1:9", title="Forever", note=None, now=t0)

        purged = await whitelist.purge_expired_spares(session, t0 + timedelta(days=2))

        assert purged == ["radarr:1:7"]
        # overrides() converges the moment the row is gone: the live and forever spares remain.
        assert await whitelist.overrides(session) == {
            "radarr:1:8": "spare",
            "radarr:1:9": "spare",
        }

    async def test_purges_at_the_exact_expiry_boundary(self, session: AsyncSession) -> None:
        # `spare_expires_at <= now` matches overrides_effective_at's own boundary, so the two
        # halves agree on the same tick.
        t0 = utcnow()
        entry = await whitelist.spare(
            session, media_key="radarr:1:7", title="Short", note=None, spare_days=1, now=t0
        )
        assert entry.spare_expires_at is not None
        assert await whitelist.purge_expired_spares(session, entry.spare_expires_at) == [
            "radarr:1:7"
        ]

    async def test_purges_nothing_before_expiry(self, session: AsyncSession) -> None:
        t0 = utcnow()
        await whitelist.spare(
            session, media_key="radarr:1:7", title="Short", note=None, spare_days=5, now=t0
        )
        assert await whitelist.purge_expired_spares(session, t0 + timedelta(days=1)) == []
        assert await whitelist.overrides(session) == {"radarr:1:7": "spare"}

    async def test_never_purges_a_reap_even_with_a_stale_expiry(
        self, session: AsyncSession
    ) -> None:
        # A reap should never carry an expiry (set_override nulls it), but guard the predicate
        # directly: a hand-built reap row with a past expiry must survive the purge.
        t0 = utcnow()
        session.add(
            WhitelistEntry(
                media_key="radarr:1:9",
                title="Gone",
                decision="reap",
                spare_expires_at=t0 - timedelta(days=1),
                created_at=t0,
            )
        )
        await session.flush()
        assert await whitelist.purge_expired_spares(session, t0) == []
        assert await whitelist.overrides(session) == {"radarr:1:9": "reap"}


class TestEffectiveOverride:
    def test_a_show_override_covers_its_seasons(self) -> None:
        decisions = {"sonarr:1:88": "reap"}
        assert whitelist.effective_override("sonarr:1:88:3", decisions) == "reap"
        assert whitelist.effective_override("sonarr:1:88", decisions) == "reap"

    def test_a_season_override_wins_over_its_show(self) -> None:
        decisions = {"sonarr:1:88": "reap", "sonarr:1:88:3": "spare"}
        assert whitelist.effective_override("sonarr:1:88:3", decisions) == "spare"

    def test_a_movie_has_no_show_and_matches_only_itself(self) -> None:
        assert whitelist.effective_override("radarr:1:7", {"radarr:1:8": "reap"}) is None

    def test_no_override_returns_none(self) -> None:
        assert whitelist.effective_override("sonarr:1:88:3", {}) is None


class TestPlannerExcludesSparedItems:
    async def test_a_spared_condemned_item_never_enters_a_plan(self, session: AsyncSession) -> None:
        """The heart of it: the snapshot still says condemn, but the plan must not touch a
        file spared after that snapshot was frozen."""
        snapshot_id = await _snapshot_with(
            session, [("radarr:1:1", 1 * GB), ("radarr:1:2", 5 * GB), ("radarr:1:3", 9 * GB)]
        )
        await whitelist.spare(session, media_key="radarr:1:2", title="Movie 1", note=None)

        await build_plan(session, snapshot_id=snapshot_id, approved_by="admin")

        planned_keys = {s.media_key for s in (await session.execute(select(ActionStep))).scalars()}
        assert "radarr:1:2" not in planned_keys
        assert planned_keys == {"radarr:1:1", "radarr:1:3"}

    async def test_sparing_a_whole_show_excludes_its_condemned_seasons(
        self, session: AsyncSession
    ) -> None:
        """A spare on the show key must reach every one of its condemned seasons -- even
        those the frozen snapshot still reads ``condemn`` -- exactly as an item spare does."""
        snapshot_id = await _snapshot_with(
            session, [("sonarr:1:88:2", 3 * GB), ("sonarr:1:88:3", 4 * GB), ("radarr:1:1", 1 * GB)]
        )
        await whitelist.spare(session, media_key="sonarr:1:88", title="A Show", note=None)

        await build_plan(session, snapshot_id=snapshot_id, approved_by="admin")

        planned_keys = {s.media_key for s in (await session.execute(select(ActionStep))).scalars()}
        assert planned_keys == {"radarr:1:1"}

    async def test_realizing_an_expired_spare_makes_the_item_plannable_again(
        self, session: AsyncSession
    ) -> None:
        """B-1: once the scan realizes an expired spare (drop from the judged map + purge the
        row), the item leaves the live override set, so a plan built on the still-condemned
        snapshot targets it again -- the dead-end this fix exists to prevent."""
        t0 = utcnow()
        snapshot_id = await _snapshot_with(
            session, [("radarr:1:1", 1 * GB), ("radarr:1:2", 5 * GB)]
        )
        await whitelist.spare(
            session, media_key="radarr:1:2", title="Movie 1", note=None, spare_days=1, now=t0
        )
        # The scan's realization, in the order scan() runs it: same `now` for both halves.
        after = t0 + timedelta(days=2)
        await whitelist.overrides_effective_at(session, after)
        assert await whitelist.purge_expired_spares(session, after) == ["radarr:1:2"]

        await build_plan(session, snapshot_id=snapshot_id, approved_by="admin")

        planned_keys = {s.media_key for s in (await session.execute(select(ActionStep))).scalars()}
        assert planned_keys == {"radarr:1:1", "radarr:1:2"}

    async def test_sparing_every_condemned_item_leaves_no_plan(self, session: AsyncSession) -> None:
        """If everything condemned is spared, there is nothing to build -- and that is a
        refusal to plan, not an empty run that looks executable."""
        from reaper.services.planner import PlanError

        snapshot_id = await _snapshot_with(session, [("radarr:1:1", 1 * GB)])
        await whitelist.spare(session, media_key="radarr:1:1", title="Movie 0", note=None)

        with pytest.raises(PlanError):
            await build_plan(session, snapshot_id=snapshot_id, approved_by="admin")
