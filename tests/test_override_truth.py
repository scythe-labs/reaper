# SPDX-License-Identifier: AGPL-3.0-or-later
"""A hand reap takes effect the moment it is set -- honestly.

Before this pass a reap override changed nothing until the next scan: the queue kept its
green pill, the counts and the plan excluded the item, and grace never started. Now one
module (``services.condemned``) assembles the effective condemned set -- scan-condemned
minus hand-spares plus hand-reaps the engine honors -- and grace, the planner, the
confirmation counts and the executor all read it. Pinned here:

* ``reap_override_verdict`` plumbs a frozen row into ``decide_verdict`` and nothing
  else: every protection loses to the owner -- fired or merely unchecked alike -- and the
  only two things that still refuse are the ones that are not protections at all, a bad
  Plex match and an explanation this code cannot parse. Both are "we do not know what this
  row IS", which is a different question from "we could not check whether it is wanted";
* the effective set adds and removes the right rows, including show-level decisions and
  a season spared back out of a reaped show;
* grace and the planner see a hand-reap immediately, and the plan refuses a refused one;
* the override routes start the grace clock on an effective reap and remove it again
  when the reap is withdrawn, so a stale hand-reap timestamp can never shorten a later,
  real condemnation's window (rule 4).
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine as sa_create_engine
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import Session

from reaper import logbuffer
from reaper.clock import utcnow
from reaper.config import Settings
from reaper.db.base import Base
from reaper.db.models import (
    ActionStep,
    Candidate,
    FirstFlagged,
    Instance,
    InstanceKind,
    Snapshot,
)
from reaper.main import create_app
from reaper.services import grace, whitelist
from reaper.services.condemned import (
    effective_condemned,
    reap_is_effective,
    reap_override_verdict,
)
from reaper.services.planner import PlanError, build_plan
from tests._auth import login

GB = 1024**3
NOW = utcnow()


def stored_explanation(**blocks: Any) -> str:
    """One stored explanation, carrying the blocks every real one carries.

    The scaffolding is not what these tests are about -- each varies ONE block and asserts
    what a hand reap does with it -- but it has to be present, because a reap is now refused
    on any document the why panel cannot render (#142), and a bare
    ``{"protections_fired": [...]}`` is one of those. ``services.snapshot._explain`` has
    written all six of these keys in every generation of the writer, so a fixture without
    them was asserting reap behavior against a row no scan could ever have produced.

    Deliberately NOT a transcription of ``_explain`` (rule 119): it carries the minimum the
    reader requires, so a new REQUIRED field on ``Explanation`` fails these tests rather than
    being back-filled here unnoticed. The values are inert -- the reap branch reads neither
    score nor coverage (``test_the_score_is_inert_on_the_reap_branch`` pins that) -- and each
    test that cares passes its own block.
    """
    return json.dumps(
        {
            "score": 50,
            "coverage": 1.0,
            "signals": [],
            "protections_fired": [],
            "protections_checked": [],
            "protections_unknown": [],
            **blocks,
        }
    )


CAUTIOUS = stored_explanation(
    protections_fired=[
        {
            "gate": "season_progression",
            "detail": "the newest season of a show that is still running",
        }
    ]
)
STRUCTURAL = stored_explanation(
    protections_fired=[{"gate": "streaming_now", "detail": "someone is watching it right now"}]
)
UNMANAGED = stored_explanation(
    protections_fired=[{"gate": "unmanaged", "detail": "no *arr manages this file"}]
)
BLOCKED = stored_explanation(
    protections_unknown=[
        {"gate": "curated_list", "detail": "could not check curated lists: IMDb timed out"}
    ]
)
# The keep-rule conflict: the season guard flags a prunable season watched MORE than one
# the rule keeps, and hands the call to a human. It rides in `protections_unknown` (blocked,
# so the scan abstains and asks for a look), and `defers_to_owner` marks it as a decision
# for the owner rather than a source Reaper could not read. A hand reap IS that decision,
# so it condemns. The flag no longer decides that -- no block holds a hand reap -- but it
# still picks the operator's chip (`api.routes._chip`), so the shapes below keep varying it.
KEEP_RULE_CONFLICT = stored_explanation(
    protections_unknown=[
        {
            "gate": "season_progression",
            "detail": "5 people watched this season, more than one your keep rule protects.",
            "defers_to_owner": True,
        }
    ]
)
# The same gate blocked by a genuine plumbing failure rather than a decision put to the
# owner. It used to be the fail-closed half of a two-condition test; now it lands where every
# other block lands, and it is kept because it is the shape a reader most expects to differ.
CONFLICT_BUT_PLUMBING = stored_explanation(
    protections_unknown=[
        {
            "gate": "season_progression",
            "detail": "could not check the sequential guard",
            "defers_to_owner": False,
        }
    ]
)
# The shape that shipped broken: the kept season is on disk but was never resolved in Plex,
# so the comparison could not be made at all (`season_pruning.PruneConflict` with
# `kept_watchers=None`). Note where the phrase sits -- the message opens with the watcher
# count, so the old `detail.startswith("could not check")` arm never matched it. The sentence
# is the one the producer emitted at the time, kept verbatim rather than refreshed: these are
# STORED rows, an operator's database is full of ones written by older versions, and a fixture
# that tracks the current copy would stop covering them. It is here because the wording trap
# must go on deciding nothing, whichever way the decision itself points.
CONFLICT_COMPARISON_REFUSED = stored_explanation(
    protections_unknown=[
        {
            "gate": "season_progression",
            "detail": (
                "40 people watched Season 1. Reaper could not check who watched "
                "Season 4, which it is keeping because it is one of the newest "
                "seasons your rule keeps. Kept for now."
            ),
            "defers_to_owner": False,
        }
    ]
)
# A row frozen before the flag shipped: same conflict, no `defers_to_owner` key at all.
# Nothing in it can tell a made comparison from a refused one -- which no longer changes the
# reap, and still changes the chip the operator is shown (rule 104's thaw, in `_chip`).
LEGACY_CONFLICT_NO_FLAG = stored_explanation(
    protections_unknown=[
        {
            "gate": "season_progression",
            "detail": "5 people watched this season, more than one your keep rule protects.",
        }
    ]
)
UNMATCHED = stored_explanation(match={"status": "unmatched"})
CLEAN_ABSTAIN = stored_explanation(threshold=70)


class TestReapOverrideVerdict:
    def test_a_cautious_protection_loses_to_the_owner(self) -> None:
        assert reap_override_verdict(CAUTIOUS, score=50) == "condemn"

    def test_a_structural_stop_still_wins(self) -> None:
        assert reap_override_verdict(STRUCTURAL, score=99) == "protect"
        assert reap_override_verdict(UNMANAGED, score=99) == "protect"

    @pytest.mark.parametrize(
        "gate", ["curated_list", "server_popularity", "min_dormancy", "custom"]
    )
    def test_an_unchecked_protection_no_longer_holds_the_reap(self, gate: str) -> None:
        """The reversal, on the stored-row path, across four distinct gates.

        The old rule was a gate-id membership test, so one case cannot tell "no gate holds"
        from "this gate happens to be on the permitted list"; the sweep can. ``blocked`` is
        still computed and still true for each of these rows -- the scan abstained on them
        and nothing automatic will touch them -- but it is no longer what
        ``reap_override_verdict`` hands to ``blocked_holds_reap``. The owner is reading a
        panel that names the check that came back empty, and is entitled to answer it.

        What did NOT move is asserted in the same class, deliberately adjacent: a structural
        stop, a bad Plex match, an unreadable protections list and an unreadable document all
        still keep the file."""
        row = stored_explanation(
            protections_unknown=[
                {"gate": gate, "detail": "could not check curated lists: IMDb timed out"}
            ]
        )

        assert reap_override_verdict(row, score=99) == "condemn"

    def test_a_keep_rule_conflict_loses_to_the_owner(self) -> None:
        """The season keep-rule conflict is a deliberate "you decide" flag. A hand reap is
        exactly the decision it asked for, so it condemns."""
        assert reap_override_verdict(KEEP_RULE_CONFLICT, score=90) == "condemn"

    def test_the_stored_deferral_flag_no_longer_moves_the_reap(self) -> None:
        """Every value ``defers_to_owner`` can carry on disk reaches the same verdict.

        This was the strictest interlock on the path: the flag was thawed with ``is True``,
        because that was the one value that let a hand reap through a block, and relaxing it
        to a truthy test would have let stored junk -- the string ``"false"`` among it --
        release a file while saying the opposite. The reap does not read the flag at all
        now, so the sweep asserts the property that replaced it: no value of a stored key can
        change what a hand reap does. It fails the moment any reader of this key re-appears
        on the decision path.

        The ``is True`` strictness itself is still live one surface over -- ``routes._chip``
        reads the same key the same way to pick the operator's chip, pinned in
        ``test_review_chips.py`` -- which is why the key is still written and still varied
        here rather than dropped."""
        for raw_flag in ("true", "false", '"true"', '"false"', '"0"', "1", "[]", "{}", "null"):
            row = stored_explanation(
                protections_unknown=[
                    {
                        "gate": "season_progression",
                        "detail": "d",
                        "defers_to_owner": json.loads(raw_flag),
                    }
                ]
            )
            assert reap_override_verdict(row, score=90) == "condemn", raw_flag
        # ...including the row frozen before the key existed at all, which carries none.
        assert reap_override_verdict(CONFLICT_BUT_PLUMBING, score=90) == "condemn"
        assert reap_override_verdict(CONFLICT_COMPARISON_REFUSED, score=90) == "condemn"
        assert reap_override_verdict(LEGACY_CONFLICT_NO_FLAG, score=90) == "condemn"

    def test_the_wording_of_a_blocked_reason_decides_nothing(self) -> None:
        """The trap that shipped broken, pinned from the other side.

        A hand reap once removed a season whose keep-rule comparison Reaper had explicitly
        declined to make, because the arm meant to catch it tested
        ``detail.startswith("could not check")`` and the one message it existed for opens
        with the watcher count. Both wordings are swept here, and they must land in the same
        place -- whichever place that is. A future reader who reintroduces a hold and hangs
        it off the sentence fails this test rather than shipping the same defect a third
        time (rule 92)."""
        prefixed = reap_override_verdict(CONFLICT_BUT_PLUMBING, score=90)
        count_first = reap_override_verdict(CONFLICT_COMPARISON_REFUSED, score=90)

        assert prefixed == count_first == "condemn"

    def test_a_protections_list_that_cannot_be_read_holds_the_reap(self) -> None:
        """The other of the two holds left on this path, and the fail-open it closes.

        The readers use ``.get``, so both lists were filtered to objects first -- and a list
        of three strings filtered down to an empty one, so ``blocked`` went False and the
        reap condemned a file whose kept-reasons nobody could read. Evidence we cannot see
        must never become evidence that nothing was wrong (rule 96).
        ``routes._has_blocked_protections`` reads the same block with this posture already;
        the destructive reader was the permissive one.

        It survived the reversal for the same reason a bad match did, and the distinction is
        worth stating because these rows LOOK like the released ones. An unreadable entry is
        not a check that came back empty: the panel could not render the reasons at all, so
        there was nothing for the owner to consent to. A block they can read is theirs to
        answer; one that never reached them is not.

        Each row is otherwise a complete document, so the list is the only thing wrong with
        it. **Both readers refuse these shapes and the assertion cannot tell which one did**
        (rule 118): a protections list carrying a string fails ``_protection_entries`` and
        fails ``Explanation`` too, since the field is typed ``list[GateOutcomeOut]``. That is
        the subsumption #142 intended, not a gap -- what this pins is that the shape keeps the
        file, and ``TestThePanelAndTheReapAgree`` is where the two readers are held level."""
        for bad_list in (
            ["season_progression could not be checked"],
            [None],
            [{"gate": "x", "detail": "d"}, "and a string"],
            "could not check the season guard",
        ):
            row = stored_explanation(protections_unknown=bad_list)
            assert reap_override_verdict(row, score=90) == "protect", bad_list
        for bad_fired in (["whitelisted"], 1):
            row = stored_explanation(protections_fired=bad_fired)
            assert reap_override_verdict(row, score=90) == "protect", bad_fired

    def test_a_scalar_protections_list_does_not_raise_out_of_the_reap(self) -> None:
        """A scalar where a list belongs used to raise ``TypeError`` straight out of this
        function. The ``except`` above covers ``json.loads`` only, so it escaped into the
        review queue, the planner's step expansion, the confirmation-phrase count and the
        executor's per-item spare re-read (rule 112's path) -- one malformed row taking down
        every surface that counts what a reap will remove."""
        assert reap_override_verdict(stored_explanation(protections_unknown=1), score=90) == (
            "protect"
        )
        assert reap_override_verdict(stored_explanation(protections_fired=2.5), score=90) == (
            "protect"
        )

    def test_a_genuinely_empty_protections_list_stays_permissive(self) -> None:
        """The other side of the same rule, so the fix above cannot be over-read: an empty
        list is a readable answer -- the scan looked and found nothing holding the item --
        and it must still let a hand reap through, or every clean abstain stops being
        reapable."""
        assert reap_override_verdict(stored_explanation(), score=90) == "condemn"
        assert reap_override_verdict(stored_explanation(protections_unknown=[]), score=90) == (
            "condemn"
        )

    def test_a_recorded_nothing_and_a_missing_answer_are_not_the_same_row(self) -> None:
        """Rule 1 on this path, and the half of #142 most easily read as a regression.

        An explicit ``[]`` above says the scan looked and nothing held the item, and it reaps.
        A ``null``, or no key at all, says only that nothing was recorded -- and the why panel
        has never been able to render such a row, so the operator is shown no signals, no
        protections and no threshold above the Reap button. ``"{}"`` is that row at its
        smallest, and it used to condemn: it reads like the permissive case and is not one.
        ``engine.verdict`` promises they "cannot consent to reasons the panel never rendered",
        and these are the rows that made it false. They keep the file now.

        The direction is the point. Omitted is not empty, and where they are told apart on a
        deletion path the omitted one resolves toward keeping."""
        assert reap_override_verdict("{}", score=90) == "protect"
        assert reap_override_verdict(stored_explanation(protections_unknown=None), score=90) == (
            "protect"
        )

    @pytest.mark.parametrize(
        "match",
        [{"status": "unmatched"}, {"status": "ambiguous"}, "a string where a block belongs"],
        ids=["unmatched", "ambiguous", "unreadable"],
    )
    def test_a_bad_plex_match_still_refuses_the_reap(self, match: object) -> None:
        """One of the two holds left on this path, and the reason it is not a protection.

        A bad match says the file behind this row may not be the one the owner is looking
        at, so a reap here could remove something they never saw. That is identity, not
        judgment, and there is nothing for them to overrule -- which is exactly why it
        survived a change that released every judgment. All three states are swept: the two
        Plex reports, and the ``MATCH_UNREADABLE`` one Plex never reports, reached by a match
        block that is present but is not an object (rule 96).

        Each row also carries a blocked protection, so the sweep cannot pass on a hold that
        the gate block is quietly still supplying. It is otherwise a complete, readable
        document, so it cannot pass on #142's wider hold either: the string case thaws to an
        absent match (``Explanation._thaw_match`` guards the outer shape), leaving the match
        itself as the only thing that can refuse the reap."""
        row = stored_explanation(
            match=match,
            protections_unknown=[
                {"gate": "curated_list", "detail": "could not check curated lists: IMDb timed out"}
            ],
        )

        assert reap_override_verdict(row, score=99) == "protect"

    def test_a_match_problem_reads_as_blocked(self) -> None:
        assert reap_override_verdict(UNMATCHED, score=99) == "protect"

    def test_a_clean_abstain_condemns(self) -> None:
        assert reap_override_verdict(CLEAN_ABSTAIN, score=10) == "condemn"

    def test_an_unreadable_explanation_keeps_the_file(self) -> None:
        assert reap_override_verdict("not json", score=99) == "protect"
        assert reap_override_verdict("[1, 2]", score=99) == "protect"

    def test_the_score_is_inert_on_the_reap_branch(self) -> None:
        """decide_verdict's reap branch never reads the score or the thresholds; the
        zeros this module passes are plumbing, not policy. If this ever fails, the
        engine's decision order changed and services.condemned must be revisited.

        Swept over both answers, so a score that started deciding one of them shows up
        whichever direction it pushed."""
        for score in (0, 50, 100):
            assert reap_override_verdict(CAUTIOUS, score=score) == "condemn"
            assert reap_override_verdict(KEEP_RULE_CONFLICT, score=score) == "condemn"
            assert reap_override_verdict(BLOCKED, score=score) == "condemn"
            assert reap_override_verdict(CONFLICT_BUT_PLUMBING, score=score) == "condemn"
            assert reap_override_verdict(CONFLICT_COMPARISON_REFUSED, score=score) == "condemn"
            assert reap_override_verdict(STRUCTURAL, score=score) == "protect"
            assert reap_override_verdict(UNMANAGED, score=score) == "protect"
            assert reap_override_verdict(UNMATCHED, score=score) == "protect"


# --- fixtures over a real database ------------------------------------------


async def _snapshot(session: AsyncSession) -> int:
    snap = Snapshot(
        created_at=NOW, policy_hash="p" * 64, scoring_hash="s" * 64, horizon_at=NOW, item_count=0
    )
    session.add(snap)
    await session.flush()
    return snap.id


async def _row(
    session: AsyncSession,
    snapshot_id: int,
    media_key: str,
    *,
    verdict: str,
    explanation: str = "{}",
    group_key: str | None = None,
    size: int = 2 * GB,
) -> None:
    # The instance a real plan resolves this candidate against. A scan only condemns items
    # from instances that exist, and build_plan refuses a movie whose Radarr is gone; these
    # tests fabricate candidates, so they seed the instance. Idempotent, since a test adds
    # several rows sharing one instance id.
    instance_id = int(media_key.split(":")[1])
    if instance_id not in set((await session.execute(select(Instance.id))).scalars().all()):
        session.add(
            Instance(
                id=instance_id,
                kind=InstanceKind.RADARR,
                name=f"i{instance_id}",
                base_url="https://arr.test",
                api_key_enc="enc",
                created_at=NOW,
            )
        )
        await session.flush()
    session.add(
        Candidate(
            snapshot_id=snapshot_id,
            media_key=media_key,
            title=f"Item {media_key}",
            media_type="season" if media_key.count(":") == 3 else "movie",
            size_bytes=size,
            verdict=verdict,
            score=80,
            coverage_bp=10_000,
            explanation_json=explanation,
            group_key=group_key,
            created_at=NOW,
        )
    )
    await session.flush()


class TestEffectiveCondemned:
    async def test_spares_leave_and_honored_reaps_join(
        self, async_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        async with async_factory() as session:
            snap = await _snapshot(session)
            await _row(session, snap, "radarr:1:1", verdict="condemn")
            await _row(session, snap, "radarr:1:2", verdict="condemn")
            await _row(session, snap, "radarr:1:3", verdict="protect", explanation=CAUTIOUS)
            await _row(session, snap, "radarr:1:4", verdict="protect", explanation=STRUCTURAL)
            await whitelist.set_override(
                session, media_key="radarr:1:2", title="t", decision="spare", note=None
            )
            for key in ("radarr:1:3", "radarr:1:4"):
                await whitelist.set_override(
                    session, media_key=key, title="t", decision="reap", note=None
                )
            decisions = await whitelist.overrides(session)

            effective = await effective_condemned(session, snap, decisions)

        # 1 stays, 2 is spared out, 3's cautious protection loses to the owner,
        # 4's structural stop still wins.
        assert sorted(effective) == ["radarr:1:1", "radarr:1:3"]

    async def test_a_show_level_reap_reaches_seasons_but_not_a_spared_one(
        self, async_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        async with async_factory() as session:
            snap = await _snapshot(session)
            show = "sonarr:1:42"
            await _row(
                session, snap, f"{show}:1", verdict="protect", explanation=CAUTIOUS, group_key=show
            )
            await _row(
                session,
                snap,
                f"{show}:2",
                verdict="abstain",
                explanation=CLEAN_ABSTAIN,
                group_key=show,
            )
            await whitelist.set_override(
                session, media_key=show, title="t", decision="reap", note=None
            )
            # Season 2 is spared back out by its own key: the item's key wins.
            await whitelist.set_override(
                session, media_key=f"{show}:2", title="t", decision="spare", note=None
            )
            decisions = await whitelist.overrides(session)

            effective = await effective_condemned(session, snap, decisions)

        assert sorted(effective) == [f"{show}:1"]

    async def test_grace_starts_the_moment_the_owner_reaps(
        self, async_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        async with async_factory() as session:
            snap = await _snapshot(session)
            await _row(session, snap, "radarr:1:5", verdict="protect", explanation=CAUTIOUS)
            await whitelist.set_override(
                session, media_key="radarr:1:5", title="t", decision="reap", note=None
            )

            report = await grace.grace_report(session, grace_days=14, now=NOW)

        assert [i.media_key for i in report.in_grace] == ["radarr:1:5"]

    async def test_the_plan_includes_an_honored_reap_and_refuses_a_refused_one(
        self, async_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        async with async_factory() as session:
            snap = await _snapshot(session)
            await _row(session, snap, "radarr:1:6", verdict="condemn")
            await _row(session, snap, "radarr:1:7", verdict="protect", explanation=CAUTIOUS)
            await _row(session, snap, "radarr:1:8", verdict="protect", explanation=STRUCTURAL)
            for key in ("radarr:1:7", "radarr:1:8"):
                await whitelist.set_override(
                    session, media_key=key, title="t", decision="reap", note=None
                )

            run = await build_plan(session, snapshot_id=snap, approved_by="test")
            await session.flush()

            planned_keys = {
                s.media_key
                for s in (
                    (await session.execute(select(ActionStep).where(ActionStep.run_id == run.id)))
                    .scalars()
                    .all()
                )
            }
            # The honored reap is planned; the structural refusal is not.
            assert planned_keys == {"radarr:1:6", "radarr:1:7"}

            # Naming the refused one explicitly fails loudly, never silently shrinks.
            with pytest.raises(PlanError):
                await build_plan(
                    session,
                    snapshot_id=snap,
                    approved_by="test",
                    only_media_keys={"radarr:1:8"},
                )

    async def test_reap_is_effective_reads_the_frozen_row(
        self, async_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        async with async_factory() as session:
            snap = await _snapshot(session)
            await _row(session, snap, "radarr:1:9", verdict="condemn")
            await _row(session, snap, "radarr:1:10", verdict="protect", explanation=STRUCTURAL)
            fetched = (
                (await session.execute(select(Candidate).where(Candidate.snapshot_id == snap)))
                .scalars()
                .all()
            )
            by_key = {c.media_key: c for c in fetched}
        assert reap_is_effective(by_key["radarr:1:9"]) is True
        assert reap_is_effective(by_key["radarr:1:10"]) is False


# --- the override routes start and stop the grace clock -----------------------


@pytest.fixture
def client(tmp_path: Path) -> Iterator[TestClient]:
    """A logged-in client over a database seeded with one snapshot and three rows:
    a scan-condemned movie, a cautiously-protected one, and a structurally-protected
    one. Sync seeding, because the app builds its own async engine over the same file."""
    settings = Settings(data_dir=tmp_path, secret_key="k")
    engine = sa_create_engine(settings.sync_database_url)
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        snap = Snapshot(
            created_at=NOW,
            policy_hash="p" * 64,
            scoring_hash="s" * 64,
            horizon_at=NOW,
            item_count=3,
        )
        s.add(snap)
        s.flush()

        def add(media_key: str, verdict: str, explanation: str) -> None:
            s.add(
                Candidate(
                    snapshot_id=snap.id,
                    media_key=media_key,
                    title=f"Item {media_key}",
                    media_type="movie",
                    size_bytes=GB,
                    verdict=verdict,
                    score=80,
                    coverage_bp=10_000,
                    explanation_json=explanation,
                    created_at=NOW,
                )
            )

        add("radarr:1:21", "condemn", "{}")
        add("radarr:1:22", "protect", CAUTIOUS)
        add("radarr:1:23", "protect", STRUCTURAL)
        s.add(
            FirstFlagged(media_key="radarr:1:21", first_flagged_at=NOW, last_seen_condemned_at=NOW)
        )
        s.commit()
    engine.dispose()

    with TestClient(create_app(settings)) as c:
        login(c, settings)
        yield c


def _clock_rows(tmp_path: Path) -> dict[str, Any]:
    settings = Settings(data_dir=tmp_path, secret_key="k")
    engine = sa_create_engine(settings.sync_database_url)
    with Session(engine) as s:
        rows = {f.media_key: f.first_flagged_at for f in s.query(FirstFlagged).all()}
    engine.dispose()
    return rows


def _seed_clock(
    tmp_path: Path,
    media_key: str,
    *,
    first_flagged_at: datetime,
    last_seen_condemned_at: datetime,
) -> None:
    """Force a grace clock into a chosen (possibly stale) state, standing in for the scans that
    re-condemned an item while it still showed spared -- the B-2 burn-down."""
    settings = Settings(data_dir=tmp_path, secret_key="k")
    engine = sa_create_engine(settings.sync_database_url)
    with Session(engine) as s:
        s.merge(
            FirstFlagged(
                media_key=media_key,
                first_flagged_at=first_flagged_at,
                last_seen_condemned_at=last_seen_condemned_at,
            )
        )
        s.commit()
    engine.dispose()


class TestOverrideRoutesAndTheGraceClock:
    def test_an_honored_reap_starts_the_clock_and_withdrawing_it_stops_it(
        self, client: TestClient, tmp_path: Path
    ) -> None:
        response = client.post(
            "/api/override", json={"media_key": "radarr:1:22", "decision": "reap"}
        )
        assert response.status_code == 200, response.text
        assert "radarr:1:22" in _clock_rows(tmp_path)

        response = client.delete("/api/override/radarr:1:22")
        assert response.status_code == 200
        assert "radarr:1:22" not in _clock_rows(tmp_path)

    def test_a_refused_reap_never_starts_a_clock(self, client: TestClient, tmp_path: Path) -> None:
        response = client.post(
            "/api/override", json={"media_key": "radarr:1:23", "decision": "reap"}
        )
        assert response.status_code == 200, response.text
        assert "radarr:1:23" not in _clock_rows(tmp_path)

    def test_unreaping_a_scan_condemned_item_keeps_the_scan_clock(
        self, client: TestClient, tmp_path: Path
    ) -> None:
        """The scan owns that clock: the item is still condemned, so its countdown must
        survive an override coming and going."""
        client.post("/api/override", json={"media_key": "radarr:1:21", "decision": "reap"})
        client.delete("/api/override/radarr:1:21")
        assert "radarr:1:21" in _clock_rows(tmp_path)

    def test_flipping_a_reap_to_spare_removes_the_hand_clock(
        self, client: TestClient, tmp_path: Path
    ) -> None:
        client.post("/api/override", json={"media_key": "radarr:1:22", "decision": "reap"})
        assert "radarr:1:22" in _clock_rows(tmp_path)
        client.post("/api/override", json={"media_key": "radarr:1:22", "decision": "spare"})
        assert "radarr:1:22" not in _clock_rows(tmp_path)

    def test_sparing_a_scan_condemned_item_restarts_its_clock_on_unspare(
        self, client: TestClient, tmp_path: Path
    ) -> None:
        """A scan-condemned item the owner SPARES leaves the reap list, so its clock is
        dropped; un-sparing re-enters it on a FRESH window rather than the weeks-old one it
        left with, which would drop it straight past grace with no warning (rule 4).
        radarr:1:21 was first flagged at the fixture's NOW."""
        original = _clock_rows(tmp_path)["radarr:1:21"]
        # Spared: off the list, clock gone.
        client.post("/api/override", json={"media_key": "radarr:1:21", "decision": "spare"})
        assert "radarr:1:21" not in _clock_rows(tmp_path)
        # Un-spared: back on the list, but the countdown starts now -- not the spent one.
        client.delete("/api/override/radarr:1:21")
        refreshed = _clock_rows(tmp_path)
        assert "radarr:1:21" in refreshed
        assert refreshed["radarr:1:21"] > original

    def test_clearing_a_spare_restarts_a_clock_burned_down_while_invisible(
        self, client: TestClient, tmp_path: Path
    ) -> None:
        """B-2 defense in depth (rule 71): if an item was invisibly re-condemned while it still
        showed spared (the burn-down Phase 1's durable purge now prevents), clearing the stale
        spare must NOT coast on the spent clock -- it restarts on a fresh window. Without the
        cleared_spare wipe, record_first_flagged_bulk would honor the weeks-old first_flagged."""
        # Spared: off the list, its scan clock is dropped.
        client.post("/api/override", json={"media_key": "radarr:1:21", "decision": "spare"})
        assert "radarr:1:21" not in _clock_rows(tmp_path)
        # The invisible burn-down: first flagged three weeks ago, last seen condemned yesterday
        # -- exactly what the recorder would treat as a clock still legitimately running.
        _seed_clock(
            tmp_path,
            "radarr:1:21",
            first_flagged_at=NOW - timedelta(days=21),
            last_seen_condemned_at=NOW - timedelta(days=1),
        )
        # Clearing the spare re-enters the item on a FRESH window, not the spent one.
        client.delete("/api/override/radarr:1:21")
        refreshed = _clock_rows(tmp_path)["radarr:1:21"]
        assert refreshed > NOW - timedelta(days=2)

    def test_the_queue_reports_whether_a_reap_took(
        self, client: TestClient, tmp_path: Path
    ) -> None:
        client.post("/api/override", json={"media_key": "radarr:1:22", "decision": "reap"})
        client.post("/api/override", json={"media_key": "radarr:1:23", "decision": "reap"})

        # A hand reap the engine honors moves the item onto the Condemned lane; one it will not
        # honor yet (a held reap) stays on the Kept lane, its stored verdict pure policy beneath.
        condemned = {
            r["media_key"]: r for r in client.get("/api/candidates?verdict=condemn&limit=50").json()
        }
        kept = {
            r["media_key"]: r for r in client.get("/api/candidates?verdict=protect&limit=50").json()
        }

        assert condemned["radarr:1:22"]["override_effective"] is True
        assert kept["radarr:1:23"]["override_effective"] is False


# --- override views in API responses: own vs inherited-from-show ---------------


@pytest.fixture
def show_client(tmp_path: Path) -> Iterator[TestClient]:
    """A logged-in client over one show with two seasons: one the scan condemned, one it
    cautiously kept. Both carry the show's group key, so a whole-show override reaches them."""
    settings = Settings(data_dir=tmp_path, secret_key="k")
    engine = sa_create_engine(settings.sync_database_url)
    Base.metadata.create_all(engine)
    show = "sonarr:1:42"
    with Session(engine) as s:
        snap = Snapshot(
            created_at=NOW,
            policy_hash="p" * 64,
            scoring_hash="s" * 64,
            horizon_at=NOW,
            item_count=2,
        )
        s.add(snap)
        s.flush()
        for n, verdict, explanation in ((1, "condemn", "{}"), (2, "protect", CAUTIOUS)):
            s.add(
                Candidate(
                    snapshot_id=snap.id,
                    media_key=f"{show}:{n}",
                    title=f"Season {n}",
                    media_type="season",
                    size_bytes=GB,
                    verdict=verdict,
                    score=80,
                    coverage_bp=10_000,
                    explanation_json=explanation,
                    group_key=show,
                    group_title="A Show",
                    created_at=NOW,
                )
            )
        s.commit()
    engine.dispose()

    with TestClient(create_app(settings)) as c:
        login(c, settings)
        yield c


class TestOverrideViewsInResponses:
    """The three views a control needs: the decision in effect (colors the row), the item's
    OWN decision (what the control toggles), and the show's decision (the note's source)."""

    SHOW = "sonarr:1:42"

    def _seasons(self, client: TestClient) -> dict[str, Any]:
        group = client.get(f"/api/groups/{self.SHOW}").json()
        return {s["media_key"]: s for s in group["seasons"]}

    def test_a_whole_show_spare_reads_as_inherited_on_each_season(
        self, show_client: TestClient
    ) -> None:
        show_client.post("/api/override", json={"media_key": self.SHOW, "decision": "spare"})
        group = show_client.get(f"/api/groups/{self.SHOW}").json()
        # The whole-show control toggles the show key, so the group reports the show's decision.
        assert group["show_override"] == "spare"
        season = self._seasons(show_client)[f"{self.SHOW}:1"]
        assert season["override"] == "spare"  # effective: the row reads kept
        assert season["override_own"] is None  # nothing of its own for a season control to undo
        assert season["show_override"] == "spare"  # what the "kept by the whole show" note names

    def test_a_season_spared_on_its_own_owns_it(self, show_client: TestClient) -> None:
        show_client.post("/api/override", json={"media_key": f"{self.SHOW}:1", "decision": "spare"})
        group = show_client.get(f"/api/groups/{self.SHOW}").json()
        assert group["show_override"] is None  # the show itself is undecided
        season = self._seasons(show_client)[f"{self.SHOW}:1"]
        assert season["override"] == "spare"
        assert season["override_own"] == "spare"  # its own key: the control can clear it
        assert season["show_override"] is None  # no whole-show note

    def test_an_own_reap_wins_over_a_show_spare_in_the_views(self, show_client: TestClient) -> None:
        show_client.post("/api/override", json={"media_key": self.SHOW, "decision": "spare"})
        show_client.post("/api/override", json={"media_key": f"{self.SHOW}:1", "decision": "reap"})
        season = self._seasons(show_client)[f"{self.SHOW}:1"]
        assert season["override"] == "reap"  # the item's own key wins: it will be removed
        assert season["override_own"] == "reap"
        assert season["show_override"] == "spare"  # the note still names the show's choice

    def test_a_movie_owns_its_effective_decision(self, client: TestClient) -> None:
        client.post("/api/override", json={"media_key": "radarr:1:22", "decision": "spare"})
        rows = client.get("/api/candidates?verdict=protect&limit=50").json()
        movie = {r["media_key"]: r for r in rows}["radarr:1:22"]
        assert movie["override"] == "spare"
        assert movie["override_own"] == "spare"  # no show to inherit from
        assert movie["show_override"] is None


class TestAHandDecisionLeavesARecord:
    """The operator's own overrides were the one action in the app that logged nothing.

    Setting one at least leaves a `WhitelistEntry` row. **Clearing one deletes that row**
    and wipes the grace clock in the same call, so after an un-spare nothing anywhere
    recorded that the spare had ever existed -- and "this was spared and Reaper deleted
    it" had no answer at all. `prior` is what makes the line a transition rather than a
    snapshot: without it, a spare flipped to reap reads identically to a fresh reap.

    Read off the real ring rather than ``capture_logs``: ``create_app`` turns structlog's
    logger caching on, which permanently deafens a route's module logger to any later
    capture (``conftest._capturable_logs`` explains the mechanism). The ring is also the
    stronger proof -- it is what the operator downloads.
    """

    @staticmethod
    def _overrides(after: int) -> list[str]:
        """The override lines written since ``after``, by sequence rather than by index.

        The ring is process-global and no fixture resets it, and ``since`` truncates to its
        newest ``limit``, so a baseline taken as a LENGTH indexes into a window that slides
        out from under it: an override line sitting in the oldest slot when the baseline is
        sampled shifts every later line down one. That reads as a missing line in the three
        tests below and as an allowed one in the fourth, which is the direction that matters.
        A sequence cursor names the lines this request produced whatever ran before it.
        """
        return [
            line.text
            for line in logbuffer.RING.since(after, limit=logbuffer.RING_SIZE)
            if "whitelist.override" in line.text
        ]

    def test_setting_one_records_the_decision(self, client: TestClient) -> None:
        before = logbuffer.RING.last_seq()
        client.post("/api/override", json={"media_key": "radarr:1:22", "decision": "reap"})

        new = self._overrides(before)
        assert len(new) == 1
        assert "media_key=radarr:1:22" in new[0]
        assert "decision=reap" in new[0]
        assert "prior=None" in new[0]

    @pytest.mark.parametrize("route", ["/api/override", "/api/whitelist"])
    def test_clearing_one_records_what_it_used_to_be(self, client: TestClient, route: str) -> None:
        """The row is gone after this, so the log is the only surviving record.

        Both delete routes, because they are line-for-line the same body under two names
        and only one was driven -- the shape rule 72 exists for. A record that survives an
        un-spare on one path and not the other is the same silence, reachable from the UI.
        """
        client.post("/api/override", json={"media_key": "radarr:1:22", "decision": "spare"})
        before = logbuffer.RING.last_seq()
        cleared = client.delete(f"{route}/radarr:1:22")
        assert cleared.json() == {"removed": True}, cleared.text

        new = self._overrides(before)
        assert len(new) == 1
        assert "decision=cleared" in new[0]
        assert "prior=spare" in new[0]

    def test_flipping_a_spare_to_a_reap_records_both_ends(self, client: TestClient) -> None:
        client.post("/api/override", json={"media_key": "radarr:1:22", "decision": "spare"})
        before = logbuffer.RING.last_seq()
        client.post("/api/override", json={"media_key": "radarr:1:22", "decision": "reap"})

        new = self._overrides(before)
        assert len(new) == 1
        assert "prior=spare" in new[0]
        assert "decision=reap" in new[0]

    @pytest.mark.parametrize("route", ["/api/override", "/api/whitelist"])
    def test_clearing_nothing_says_nothing(self, client: TestClient, route: str) -> None:
        """No override to remove is not a decision, and a line for it would read as one."""
        before = logbuffer.RING.last_seq()
        response = client.delete(f"{route}/radarr:1:99")

        assert response.json() == {"removed": False}
        assert self._overrides(before) == []
