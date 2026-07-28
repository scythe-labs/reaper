# SPDX-License-Identifier: AGPL-3.0-or-later
"""The reap breakdown: what a reap would remove, and why.

Pins the ledger (policy verdict, hand-spares out, hand-reaps in, the net), the movie/season
split, and the by-reason participation tally -- which counts every condemned title that
trips each signal, so the counts overlap and never partition.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from reaper.clock import utcnow
from reaper.config import Settings
from reaper.db.base import Base
from reaper.db.models import Candidate, Snapshot
from reaper.db.session import create_engine, create_session_factory
from reaper.services import breakdown, whitelist

GB = 1024**3
NOW = utcnow()


@pytest.fixture
async def session(tmp_path: Path) -> AsyncIterator[AsyncSession]:
    settings = Settings(data_dir=tmp_path, secret_key="test-key")  # type: ignore[call-arg]
    engine = create_engine(settings)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory: async_sessionmaker[AsyncSession] = create_session_factory(engine)
    async with factory() as s:
        yield s
    await engine.dispose()


def _explain(adds: list[str], *, keeps: list[str] | None = None) -> str:
    """A frozen explanation whose ``signals`` block adds the given ids and (optionally)
    carries an ``argues_keep`` signal that must never be counted."""
    signals = [
        {"id": sid, "contribution": 10.0, "weight": 70, "evaluated": True, "state": "adds"}
        for sid in adds
    ]
    signals += [
        {"id": sid, "contribution": 0.0, "weight": 20, "evaluated": True, "state": "argues_keep"}
        for sid in keeps or []
    ]
    return json.dumps({"signals": signals, "protections_fired": [], "protections_unknown": []})


def _refused_explain(*, match: object = None) -> str:
    """A frozen explanation whose hand reap ``condemned.reap_override_verdict`` refuses.

    Two shapes are left that do (``engine.verdict``): a *fired* structural gate, and a row
    whose identity is in doubt. A protection that merely could not be CHECKED is no longer
    one of them, which is why this helper stopped building one -- a fixture that still did
    would report zero held reaps and the assertions below would pass on an empty set.
    """
    return json.dumps(
        {
            "signals": [],
            "protections_fired": (
                [] if match else [{"gate": "streaming_now", "detail": "playing right now"}]
            ),
            "protections_unknown": [],
            **({"match": match} if match else {}),
        }
    )


async def _add(
    session: AsyncSession,
    *,
    snapshot_id: int,
    media_key: str,
    verdict: str = "condemn",
    media_type: str = "movie",
    size: int | None = 5 * GB,
    explanation: str | None = None,
) -> None:
    session.add(
        Candidate(
            snapshot_id=snapshot_id,
            media_key=media_key,
            title=f"Item {media_key}",
            media_type=media_type,
            size_bytes=size,
            verdict=verdict,
            score=90,
            coverage_bp=10_000,
            explanation_json=explanation if explanation is not None else _explain(["unwatched"]),
            created_at=NOW,
        )
    )
    await session.flush()


async def _snapshot(session: AsyncSession) -> int:
    snap = Snapshot(
        created_at=NOW, policy_hash="p" * 64, scoring_hash="s" * 64, horizon_at=NOW, item_count=0
    )
    session.add(snap)
    await session.flush()
    return snap.id


async def test_no_snapshot_is_empty(session: AsyncSession) -> None:
    report = await breakdown.reap_breakdown(session)
    assert report.has_snapshot is False
    assert report.will_reap == 0
    assert report.condemned_by == []


async def test_ledger_without_overrides(session: AsyncSession) -> None:
    snap = await _snapshot(session)
    await _add(session, snapshot_id=snap, media_key="radarr:1:1", size=3 * GB)
    await _add(
        session, snapshot_id=snap, media_key="sonarr:1:2:s1", media_type="season", size=8 * GB
    )

    report = await breakdown.reap_breakdown(session)

    assert report.has_snapshot is True
    assert report.policy_condemned == 2
    assert report.hand_spared == 0
    assert report.hand_reaped == 0
    assert report.will_reap == 2
    assert report.will_reap_bytes == 11 * GB
    assert report.movies == 1
    assert report.seasons == 1
    assert report.movies_unknown == 0
    assert report.seasons_unknown == 0


async def test_the_split_carries_its_unmeasured_share(session: AsyncSession) -> None:
    """The page subtracts the held-back rows from the split as well as the total, so the
    split needs its own unmeasured counts -- otherwise one reap gets two numbers."""
    snap = await _snapshot(session)
    await _add(session, snapshot_id=snap, media_key="radarr:1:1", size=3 * GB)
    await _add(session, snapshot_id=snap, media_key="radarr:1:2", size=None)
    await _add(
        session, snapshot_id=snap, media_key="sonarr:1:2:s1", media_type="season", size=8 * GB
    )
    await _add(session, snapshot_id=snap, media_key="sonarr:1:2:s2", media_type="season", size=None)

    report = await breakdown.reap_breakdown(session)

    assert (report.movies, report.movies_unknown) == (2, 1)
    assert (report.seasons, report.seasons_unknown) == (2, 1)
    # And they add up to the whole unmeasured tail the headline subtracts.
    assert report.movies_unknown + report.seasons_unknown == report.will_reap_unknown


async def test_a_hand_spare_leaves_the_net(session: AsyncSession) -> None:
    snap = await _snapshot(session)
    await _add(session, snapshot_id=snap, media_key="radarr:1:1")
    await _add(session, snapshot_id=snap, media_key="radarr:1:2")
    await whitelist.set_override(
        session, media_key="radarr:1:1", title="x", decision="spare", note=None
    )

    report = await breakdown.reap_breakdown(session)

    assert report.policy_condemned == 2
    assert report.hand_spared == 1
    assert report.will_reap == 1


async def test_an_expired_spare_still_keeps_the_title_and_is_counted_as_expired(
    session: AsyncSession,
) -> None:
    """A spare whose clock has passed goes on keeping the file until a SCAN realizes it.

    That is the whole reason the count exists. ``purge_expired_spares`` runs only inside the
    scan transaction, so between the clock passing and the next scan this ledger, the planner
    and the executor all still read the spare -- the title is genuinely kept and genuinely
    absent from the reap, with nothing else on the page to explain why. The count is what lets
    the Reap page say so.
    """
    snap = await _snapshot(session)
    await _add(session, snapshot_id=snap, media_key="radarr:1:1")
    await _add(session, snapshot_id=snap, media_key="radarr:1:2")
    # One spare set 30 days ago for 10 days: expired 20 days back.
    await whitelist.set_override(
        session,
        media_key="radarr:1:1",
        title="x",
        decision="spare",
        note=None,
        spare_days=10,
        now=NOW - timedelta(days=30),
    )
    # ...and one still running, which must NOT be counted.
    await whitelist.set_override(
        session,
        media_key="radarr:1:2",
        title="y",
        decision="spare",
        note=None,
        spare_days=10,
        now=NOW,
    )

    report = await breakdown.reap_breakdown(session)

    assert report.hand_spared == 2
    assert report.spares_expired == 1
    # Still kept, both of them: the expired one is NOT back on the reap list.
    assert report.will_reap == 0


async def test_a_forever_spare_never_counts_as_expired(session: AsyncSession) -> None:
    """It has no clock to run out. Reading a null expiry as "passed" would put a permanent
    warning on the Reap page telling the operator to scan for a spare that will never move."""
    snap = await _snapshot(session)
    await _add(session, snapshot_id=snap, media_key="radarr:1:1")
    await whitelist.set_override(
        session, media_key="radarr:1:1", title="x", decision="spare", note=None
    )

    report = await breakdown.reap_breakdown(session)

    assert report.hand_spared == 1
    assert report.spares_expired == 0


async def test_the_count_is_scoped_to_this_snapshot_s_condemned_rows(
    session: AsyncSession,
) -> None:
    """The notice claims those titles are being held out of THIS reap, so the count is taken
    over the snapshot's condemned rows -- not over the whitelist, which outlives any one scan.

    Two rows an expired spare must NOT speak for: one the scan left alone (nothing is being
    held back), and one that is not in the snapshot at all (a spare on a title since removed
    from the library). Counting either would send the operator scanning for no change.

    The ``spared_rows`` filter this walks is load-bearing, and the first row below pins it:
    the sum asks "would this still be spared after the purge?", which an unspared condemned
    row also answers no to. Walking all condemned rows would count every one of them.
    """
    snap = await _snapshot(session)
    await _add(session, snapshot_id=snap, media_key="radarr:1:1")  # condemned, no override
    await _add(session, snapshot_id=snap, media_key="radarr:1:9", verdict="abstain")
    long_ago = NOW - timedelta(days=30)
    # Spared and expired, but the scan left it alone: it is keeping nothing out of the reap.
    await whitelist.set_override(
        session,
        media_key="radarr:1:9",
        title="x",
        decision="spare",
        note=None,
        spare_days=1,
        now=long_ago,
    )
    # Spared and expired, and not in the snapshot at all.
    await whitelist.set_override(
        session,
        media_key="radarr:1:404",
        title="gone",
        decision="spare",
        note=None,
        spare_days=1,
        now=long_ago,
    )

    report = await breakdown.reap_breakdown(session)

    assert report.hand_spared == 0
    assert report.spares_expired == 0


async def test_a_season_kept_by_a_second_spare_is_not_counted_as_released(
    session: AsyncSession,
) -> None:
    """A title only counts when a scan would actually hand it back to policy.

    Spares nest. A season spared for 10 days inside a show spared forever has a clock of its
    own that has passed, but ``purge_expired_spares`` deletes only the season's row -- and the
    show's forever spare goes on keeping it. Counting it would put "1 title is kept by a spare
    that expired. A new scan judges it again" on the Reap page for a title that cannot move,
    which is the false promise the notice exists to avoid (rule 61).
    """
    snap = await _snapshot(session)
    await _add(session, snapshot_id=snap, media_key="sonarr:1:7:2", media_type="season")
    await whitelist.set_override(
        session, media_key="sonarr:1:7", title="show", decision="spare", note=None
    )
    await whitelist.set_override(
        session,
        media_key="sonarr:1:7:2",
        title="season",
        decision="spare",
        note=None,
        spare_days=10,
        now=NOW - timedelta(days=30),
    )

    report = await breakdown.reap_breakdown(session)

    assert report.hand_spared == 1
    assert report.spares_expired == 0
    assert report.will_reap == 0


async def test_a_whole_show_spare_counts_every_season_it_holds(session: AsyncSession) -> None:
    """The count is TITLES, not spares: one expired whole-show spare holding three condemned
    seasons releases three titles, and the page says so in those words."""
    snap = await _snapshot(session)
    for n in (1, 2, 3):
        await _add(session, snapshot_id=snap, media_key=f"sonarr:1:7:{n}", media_type="season")
    await whitelist.set_override(
        session,
        media_key="sonarr:1:7",
        title="show",
        decision="spare",
        note=None,
        spare_days=10,
        now=NOW - timedelta(days=30),
    )

    report = await breakdown.reap_breakdown(session)

    assert report.hand_spared == 3
    assert report.spares_expired == 3


async def test_a_hand_reap_joins_the_net(session: AsyncSession) -> None:
    """A row the policy did not condemn, forced on by hand, is a net reap the policy
    verdict never had."""
    snap = await _snapshot(session)
    await _add(session, snapshot_id=snap, media_key="radarr:1:1")  # condemned
    # An abstained row with a clean explanation resolves to condemn under a hand reap.
    await _add(
        session, snapshot_id=snap, media_key="radarr:1:9", verdict="abstain", explanation="{}"
    )
    await whitelist.set_override(
        session, media_key="radarr:1:9", title="x", decision="reap", note=None
    )

    report = await breakdown.reap_breakdown(session)

    assert report.policy_condemned == 1
    assert report.hand_reaped == 1
    assert report.will_reap == 2


async def test_a_refused_hand_reap_is_reported_as_held(session: AsyncSession) -> None:
    """A hand reap the engine refuses is HELD: not in the net, but counted so the operator's
    mark is not silently dropped (PR-2).

    Both surviving refusals are seeded, because they refuse for unrelated reasons and the
    breakdown must count either: a structural stop (something playing right now) and a row
    whose Plex match Reaper could not read, so it does not know what the row even is.
    """
    snap = await _snapshot(session)
    await _add(session, snapshot_id=snap, media_key="radarr:1:1")  # condemned by policy
    for key, explanation in (
        ("radarr:1:9", _refused_explain()),
        ("radarr:1:10", _refused_explain(match={"status": "unmatched"})),
    ):
        await _add(
            session, snapshot_id=snap, media_key=key, verdict="abstain", explanation=explanation
        )
        await whitelist.set_override(session, media_key=key, title="x", decision="reap", note=None)

    report = await breakdown.reap_breakdown(session)

    assert report.hand_reaped == 0  # the refused reaps are not honored, so not in the net
    assert report.hand_reaped_held == 2  # but they are reported, not dropped
    assert report.will_reap == 1  # only the policy-condemned row


async def test_a_hand_reap_past_a_protection_nobody_could_check_is_in_the_net(
    session: AsyncSession,
) -> None:
    """The counterpart, and the reason the fixture above stopped using a blocked protection.

    A protection that could not be CHECKED no longer refuses a hand reap, so a row carrying
    one is a net reap like any other. Counting it as held would understate what the operator
    is about to remove, on the page whose whole job is that number.
    """
    snap = await _snapshot(session)
    await _add(session, snapshot_id=snap, media_key="radarr:1:1")  # condemned by policy
    await _add(
        session,
        snapshot_id=snap,
        media_key="radarr:1:9",
        verdict="abstain",
        explanation=json.dumps(
            {
                "signals": [],
                "protections_fired": [],
                "protections_unknown": [{"gate": "keep_list", "detail": "could not check"}],
            }
        ),
    )
    await whitelist.set_override(
        session, media_key="radarr:1:9", title="x", decision="reap", note=None
    )

    report = await breakdown.reap_breakdown(session)

    assert report.hand_reaped == 1
    assert report.hand_reaped_held == 0
    assert report.will_reap == 2


async def test_by_reason_participation_overlaps(session: AsyncSession) -> None:
    snap = await _snapshot(session)
    # One trips two signals, the other trips one. Counts overlap: unwatched=2, low_rating=1.
    await _add(
        session,
        snapshot_id=snap,
        media_key="radarr:1:1",
        explanation=_explain(["unwatched", "low_rating"], keeps=["few_watchers"]),
    )
    await _add(
        session, snapshot_id=snap, media_key="radarr:1:2", explanation=_explain(["unwatched"])
    )

    report = await breakdown.reap_breakdown(session)

    by = {s.id: s.count for s in report.condemned_by}
    assert by == {"unwatched": 2, "low_rating": 1}
    # An argues_keep signal is never counted as a reason to remove.
    assert "few_watchers" not in by
    # Sorted most-common first.
    assert report.condemned_by[0].id == "unwatched"


async def test_state_absent_falls_back_to_contribution(session: AsyncSession) -> None:
    """Rows frozen before the ``state`` field: a positive contribution still counts as a
    driver, a zero one does not."""
    snap = await _snapshot(session)
    exp = json.dumps(
        {
            "signals": [
                {"id": "unwatched", "contribution": 40.0, "weight": 70, "evaluated": True},
                {"id": "size", "contribution": 0.0, "weight": 10, "evaluated": True},
            ],
            "protections_fired": [],
            "protections_unknown": [],
        }
    )
    await _add(session, snapshot_id=snap, media_key="radarr:1:1", explanation=exp)

    report = await breakdown.reap_breakdown(session)

    by = {s.id: s.count for s in report.condemned_by}
    assert by == {"unwatched": 1}


async def test_unreadable_explanation_still_counts_the_item(session: AsyncSession) -> None:
    snap = await _snapshot(session)
    await _add(session, snapshot_id=snap, media_key="radarr:1:1", explanation="not json")

    report = await breakdown.reap_breakdown(session)

    assert report.policy_condemned == 1
    assert report.will_reap == 1
    assert report.condemned_by == []  # no signals readable, but the file is still on the list


async def test_unmeasured_item_is_carried_as_a_count(session: AsyncSession) -> None:
    snap = await _snapshot(session)
    await _add(session, snapshot_id=snap, media_key="radarr:1:1", size=None)
    await _add(session, snapshot_id=snap, media_key="radarr:1:2", size=4 * GB)

    report = await breakdown.reap_breakdown(session)

    assert report.will_reap == 2
    assert report.will_reap_bytes == 4 * GB
    assert report.will_reap_unknown == 1
