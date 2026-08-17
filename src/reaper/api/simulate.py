# SPDX-License-Identifier: AGPL-3.0-or-later
"""What a policy WOULD do, replayed against the last scan's frozen evidence.

Nothing here writes, and nothing here re-decides: the simulator replays stored evidence
through the same ``decide_verdict`` a scan uses, so a number on the policy editor and the
verdict a real scan reaches come from one decision function (rule 28). A replay that cannot
read its evidence refuses with a typed reason rather than guessing, which is why
``_refused`` exists and why every arm of ``SimStale`` is a sentence.

Reads ``_to_body`` and ``_candidate_media_type`` from ``api/policy.py``, and the three
stored-explanation readers from ``api/review.py``.
"""

from __future__ import annotations

import asyncio
import json
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import replace

import structlog
from fastapi import APIRouter, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import load_only

from reaper.api import tags as api_tags
from reaper.api.deps import newest_snapshot, session_factory
from reaper.api.policy import _candidate_media_type, _to_body
from reaper.api.review import (
    _decode_explanation,
    _entries,
    _replayed_evidence,
)
from reaper.api.schemas import (
    GateCountOut,
    PolicyIn,
    SimExampleOut,
    SimStale,
    SimulationOut,
)
from reaper.db.models import (
    Candidate,
    SeasonPruneEvidence,
)
from reaper.engine import facts_codec
from reaper.engine.dormancy import history_reach_days
from reaper.engine.gates import PROTECT, GateId, GateResult
from reaper.engine.observation import Known
from reaper.engine.policy import (
    PolicyBody,
    combine_hashes,
)
from reaper.engine.signals import SignalConfig
from reaper.engine.verdict import decide_verdict
from reaper.services import (
    list_config,
    season_evidence,
    whitelist,
)
from reaper.services.condemned import (
    effective_verdict,
    reap_is_effective,
)
from reaper.services.planner import MediaRef, PlanError
from reaper.services.profiles import active_policy
from reaper.services.scan_runner import build_gates
from reaper.services.season_pruning import SeriesPrunePlan
from reaper.services.snapshot import HAND_SPARE_DETAIL, effective_fate, judge_facts

log = structlog.get_logger(__name__)

router = APIRouter(prefix="/api")

#: How many candidates a simulation loop walks before handing the event loop back. The
#: simulator is pure computation over rows already in memory, so without a yield it holds
#: the single loop for the whole library -- and the policy editor fires this on a 250 ms
#: debounce, so dragging a weight slider queues one full-library replay after another and
#: every other request (scan status, the review queue, auth) stalls behind them (P2-1).
_SIM_YIELD_EVERY = 500


def _refused(kind: SimStale, media_type: str) -> SimulationOut:
    """No numbers, and the reason -- typed for the panel, and said once in words.

    The sentence lives here and the panel renders it, supplying only the heading beside it.
    It used to live in both, one copy per side, "kept in step" by a comment and by nothing
    else -- and the operator never saw this one at all, so the copy that was read and the copy
    that was reviewed were different strings. Rule 144: the way to keep two statements of one
    fact agreeing is to have one of them.

    The panel keeps one sentence of its own, reached only when ``stale_reason`` is null, which
    a build older than this route can produce. It is deliberately generic for that reason: it
    is the one case where the browser cannot know what happened.

    Every sentence states the condition, never who caused it. An upgrade that adds a field to
    the hashed body leaves the recorded hash unmatchable until the next scan, so "you changed"
    can be false for an operator who changed nothing.
    """
    lane = "movies" if media_type == "movie" else "TV"
    reasons = {
        # States the condition and stops there. Naming the cause was wrong more often than
        # it helped: a hash mismatch cannot say WHICH field moved, and after any upgrade that
        # re-scopes the hash it has moved for nobody -- so the old sentence told an operator
        # who had changed nothing that their keep tags read differently.
        SimStale.GATHERS_DIFFERENTLY: (
            f"Your {lane} policy doesn't match the last scan, so these numbers can't be "
            "worked out from it. Run a scan to apply it, then this becomes exact again."
        ),
        SimStale.SEASONS_NOT_RECORDED: (
            "The last scan didn't record what your season rules need, so there are no numbers "
            "to show. Run a scan, then this becomes exact again."
        ),
        # One scan shape produces this now: the guard was off, so the episode lists were never
        # asked for. A scan that asked and got no answer from Sonarr replays off the empty map
        # it planned from and reaches no refusal at all (#500, `season_evidence`). The sentence
        # still states what the scan lacks rather than the switch, like the two above it: the
        # remedy is a scan, and the only other way out is turning a protection off.
        SimStale.IN_PROGRESS_NOT_READ: (
            "The last scan didn't read where anyone had gotten to in each show, so there are "
            "no numbers to show. Run a scan, then this becomes exact again."
        ),
    }
    return SimulationOut(
        exact=False,
        stale_kind=kind,
        stale_reason=reasons[kind],
        condemned=0,
        protected=0,
        abstained=0,
        reclaimable_bytes=0,
        newly_condemned=0,
        no_longer_condemned=0,
        condemned_before=0,
        changed_titles=0,
        histogram=[0] * 10,
    )


class _SeasonEvidenceMissingError(Exception):
    """A season row cannot be re-decided exactly, so the whole lane refuses.

    Whole-lane rather than per-show on purpose: the panel's headline is a count over every
    governed row, and a replay that answered for most shows and quietly held the rest at
    their scan-time verdict would put a confident number on screen that no scan will ever
    produce. There is no partial answer to give (``docs/LEARNINGS.md`` §8).
    """

    def __init__(self, kind: SimStale) -> None:
        super().__init__(kind.value)
        self.kind = kind


async def _season_payloads(session: AsyncSession, *, snapshot_id: int) -> dict[str, str]:
    """Every show's frozen prune inputs for this snapshot, still JSON, keyed by show key.

    One query, and no decoding: :class:`_SeasonReplay` thaws a show the first time a row asks
    about it. Thawing all of them here was the same total work whenever every show has a row
    in the lane, and it was all of it in one uninterrupted block -- measured at 0.21 ms per
    show for a ten-season show with 25 viewers, so ~205 ms on a thousand shows, in front of a
    request that arrives on a 250 ms debounce while a control is being dragged
    (``docs/LEARNINGS.md``, "What frozen season evidence costs"). Decoding inside the row loop
    spreads it across the yields that loop already takes, skips a show no candidate row asks
    about, and lets a refusal fire on the first row instead of after the whole library is
    decoded (#502).
    """
    return dict(
        (
            await session.execute(
                select(SeasonPruneEvidence.group_key, SeasonPruneEvidence.payload_json).where(
                    SeasonPruneEvidence.snapshot_id == snapshot_id
                )
            )
        )
        .tuples()
        .all()
    )


class _SeasonReplay:
    """One show's frozen evidence and the plan derived from it, thawed on first use.

    Built per simulate request and discarded with it, so a memoized plan can never be served
    under a different draft. Memoized per SHOW because the plan is a property of the show:
    deriving it per row would re-sort the seasons and re-run the keep-rule conflict detector
    once for every season of that show, over a lane whose rows outnumber its shows several
    times over (#493 measured the movie lane's replay at 275 ms for 3,468 rows).

    Every refusal here is fail-closed and takes the whole lane with it: the panel's headline
    is a count over every governed row, and answering for most shows while holding the rest at
    their scan-time verdict would put a number on screen that no scan will ever produce
    (``docs/LEARNINGS.md`` §8). A show with no payload and a payload that will not decode get
    the same refusal, because the operator's remedy is the same one -- scan again -- and
    because a bundle that half-parses is exactly what must not be replayed from (rule 96).
    """

    def __init__(
        self, payloads: Mapping[str, str], *, policy: season_evidence.SeasonPolicy
    ) -> None:
        self._payloads = payloads
        self._policy = policy
        self._derived: dict[str, tuple[season_evidence.SeasonPruneInput, SeriesPrunePlan]] = {}

    def for_show(self, show: str) -> tuple[season_evidence.SeasonPruneInput, SeriesPrunePlan]:
        cached = self._derived.get(show)
        if cached is not None:
            return cached
        payload = self._payloads.get(show)
        if payload is None:
            raise _SeasonEvidenceMissingError(SimStale.SEASONS_NOT_RECORDED)
        try:
            bundle = season_evidence.from_dict(json.loads(payload))
        # OSError and OverflowError are here for one reason: `from_epoch` ends in
        # `datetime.fromtimestamp`, which raises OSError (errno 84) rather than ValueError
        # for an epoch outside the platform's range. Listing only the tuple `from_dict`'s
        # docstring named would have turned a corrupt timestamp into a 500 on a route whose
        # whole contract is to refuse cleanly.
        except (ValueError, TypeError, KeyError, AttributeError, OSError, OverflowError):
            log.warning("simulate.season_bundle_unreadable", group_key=show)
            raise _SeasonEvidenceMissingError(SimStale.SEASONS_NOT_RECORDED) from None
        if season_evidence.missing_episode_map(bundle, policy=self._policy):
            raise _SeasonEvidenceMissingError(SimStale.IN_PROGRESS_NOT_READ)
        derived = (bundle, season_evidence.plan_from_frozen(bundle, policy=self._policy))
        self._derived[show] = derived
        return derived


def _season_guard_replay(
    row: Candidate, frozen: Sequence[GateResult], *, seasons: _SeasonReplay
) -> list[GateResult]:
    """This season's frozen gate results, with the season guard re-derived under the draft.

    Replaced **in place**, never appended: ``judge_facts`` reads the extras in order and the
    hand spare is inserted ahead of them, so moving the guard's position would change which
    protection the panel names first.

    Fail-closed on everything it cannot answer, exactly as :class:`_SeasonReplay` is. A
    media_key that will not parse, a season the plan never decided, and a row whose frozen
    extras carry no season guard at all are three ways of not knowing, and each raises rather
    than letting the row through on the ordinary gates alone -- which would drop a whole
    protection lane and preview deletions the scan holds.
    """
    show = whitelist.show_key(row.media_key)
    if show is None:
        raise _SeasonEvidenceMissingError(SimStale.SEASONS_NOT_RECORDED)
    try:
        season = MediaRef.parse(row.media_key).season
    except PlanError:
        season = None
    if season is None:
        raise _SeasonEvidenceMissingError(SimStale.SEASONS_NOT_RECORDED)

    bundle, plan = seasons.for_show(show)
    # The plan has to have DECIDED this season, and a payload that decodes does not establish
    # that. `guard_result` answers a season the plan never saw with its terminal arm: a clean
    # ABSTAIN reading "checked: prunable by the keep-last / keep-first season rules". The row
    # then replays with the whole season-protection lane silently absent, condemnable on score
    # alone, under `exact: true` -- the one shape where missing evidence produces a preview
    # rather than a refusal, which is the direction the prime directive forbids.
    #
    # Asked of the plan, not of the payload's season list. The plan is the set `guard_result`
    # answers from and it is the smaller one: `plan_series_prune` keeps only content-bearing
    # seasons, so a bundle listing a season with no episode file passed a list check and still
    # reached the terminal arm (#501). The two agree only while `_judge_series` writes no
    # Candidate row for a fileless season, which is an invariant held in another module.
    if season not in set(plan.prunable) | {p.season_number for p in plan.protected}:
        raise _SeasonEvidenceMissingError(SimStale.SEASONS_NOT_RECORDED)

    replayed = season_evidence.guard_result(
        plan, season, progress_unknown_reason=bundle.progress_unknown_reason
    )
    if not any(r.gate is GateId.SEASON_PROGRESSION for r in frozen):
        raise _SeasonEvidenceMissingError(SimStale.SEASONS_NOT_RECORDED)
    return [replayed if r.gate is GateId.SEASON_PROGRESSION else r for r in frozen]


async def _replay_simulation(
    rows: list[Candidate],
    policy: PolicyBody,
    decisions: dict[str, str],
    *,
    reach_days: int | None,
    season_payloads: Mapping[str, str] | None,
) -> SimulationOut:
    """Re-decide the governed rows by replaying the REAL engine over each row's frozen Facts.

    Reached only when the edit left the evidence hash unchanged, so the frozen Facts (and the
    season guard) still describe what a scan would gather. For each row we rebuild its Facts,
    hand them to the scan's own judging function (``snapshot.judge_facts``) and apply any live
    hand override through the scan's own ``snapshot.effective_fate`` -- so the counts are
    bit-identical to a fresh scan under this policy, with zero API calls. Exact for weight,
    rating-bar, custom-rule, protect-condition and threshold edits.

    ``reach_days`` is how far back the watch history reached when the snapshot was taken,
    recomputed from the snapshot's own stored ``horizon_at``. Rows frozen before
    ``Facts.history_reach_days`` existed thaw it as ``Unknown`` (rule 104), which the
    popularity gate reads as un-checkable and blocks on -- correct for a fact nobody
    recorded, but it would report every older row as held and break the bit-identical
    promise above. The snapshot row carries the horizon that a re-scan would measure the
    same way, so the gap is filled from stored evidence rather than left as a guess. It
    only ever *fills*: a row that froze its own reach keeps it.

    ``Facts.days_since_added`` is the one gap that CANNOT be filled the same way, so the
    bit-identical promise above is narrowed by exactly this much: on a row frozen before that
    field existed, a rule authored on ``watchers_all_time`` reads as un-establishable
    wherever a deeper mirror could still overturn it (``fields.reach_shortfall``'s
    ITEM_LIFETIME arm has no span to compare the reach against), until the next scan
    re-freezes it. Not every such rule -- ``fields._survives_more_history`` lets the two
    already-earned outcomes through untouched, so an "at least N" the count already clears
    and an "at most N" it already exceeds still evaluate normally. Measured over the 440 lab
    vectors: ``gte 1`` gives 415 matched against 25 blocked, ``lte 5`` gives 172 checked and
    268 blocked -- half the outcome matrix, not all of it. Unlike the reach there is no
    stored source to recover it from -- ``Candidate`` carries no arrival date, and
    ``Facts.days_since_added`` rules out deriving it from dormancy, which is clamped to the
    mirror's edge. The divergence is the keep direction: a blocked protect condition forces
    abstain and a blocked keep takes its full discount, so the preview under-counts
    removals rather than promising one the scan would refuse. Closing it properly means an
    arrival date on ``Candidate`` in an additive migration.

    Both calls are production's, never a lookalike (rules 3/22). This loop used to assemble
    the evaluate / score / round / decide pipeline itself and push the hand override straight
    through ``decide_verdict``; that is the same drift the policy lab had to be pulled back
    from, and the second half of it matters on its own: every other read-side consumer derives
    a hand reap's fate from the frozen explanation (``condemned.reap_override_verdict``), so
    deciding it live here was one edit away from the simulator promising a deletion the
    planner holds.
    """
    gates = build_gates(policy)
    signals = [
        SignalConfig(signal=s.signal, weight=s.weight, saturate_at=s.saturate_at, floor=s.floor)
        for s in policy.signals
    ]
    custom = policy.custom_signal_configs()
    keeps = policy.keep_configs()
    window = policy.popularity_window_days()
    # Built from the DRAFT, so each season plan is re-derived under the operator's edit rather
    # than under the one the scan ran with. One thaw and one plan per show, reused by that
    # show's season rows and discarded with the request.
    seasons = (
        _SeasonReplay(season_payloads, policy=season_evidence.SeasonPolicy.from_body(policy))
        if season_payloads is not None
        else None
    )

    histogram = [0] * 10
    condemned = protected = abstained = 0
    reclaimable = 0
    unknown_size = 0
    newly = gone = 0
    before = 0
    changed = 0
    # (row, re-decided score) -- the NEW score, since a weight edit moves it, not the stored one.
    newly_rows: list[tuple[Candidate, int]] = []
    spared_by: Counter[str] = Counter()

    for index, row in enumerate(rows):
        if index % _SIM_YIELD_EVERY == 0:
            await asyncio.sleep(0)
        facts, extra = facts_codec.facts_from_dict(json.loads(row.facts_json or "{}"))
        if reach_days is not None and not isinstance(facts.history_reach_days, Known):
            facts = replace(
                facts,
                history_reach_days=Known(value=reach_days, source="tautulli"),
            )
        override = whitelist.effective_override(row.media_key, decisions)

        # The season guard rides in `extra`, frozen by the scan -- and for a TV row it is
        # re-derived here from the show's frozen plan inputs, so a season rule moves the panel
        # instead of blanking it (#491). `None` is the movie lane, which has no season guard.
        replayed_extra = (
            _season_guard_replay(row, extra, seasons=seasons)
            if seasons is not None
            else list(extra)
        )

        # A hand spare enters as an extra PROTECT so the simulator applies it LIVE -- the stored
        # verdict is pure policy now, so the override is re-applied here, never read off the row.
        # The reap override is carried into effective_fate, which honors it only past the
        # cautious cases.
        merged_extra = replayed_extra
        if override == "spare":
            merged_extra.insert(
                0, GateResult(GateId.WHITELISTED, PROTECT, detail=HAND_SPARE_DETAIL)
            )
        judged = judge_facts(
            facts,
            gates,
            policy,
            signals=signals,
            custom_condemn=custom,
            keeps=keeps,
            window_days=window,
            extra_results=merged_extra,
            # The Plex match is REPLAYED, not re-derived: it is identity evidence the scan
            # gathered, and this path re-decides policy over frozen evidence. Omitting it
            # built an explanation whose match block read "no status recorded", which
            # ``condemned.match_state`` reads as "no bad match" -- so a hand-reaped row that
            # production holds because Reaper cannot tell WHICH file it is was previewed
            # here as a deletion, and its bytes counted toward reclaimable.
            #
            # That divergence is new. While a blocked gate still held a hand reap the two
            # sides agreed by accident: an unmatched item has no rating key, so every
            # Plex-dependent gate blocked and the reap was held on both paths for a reason
            # that had nothing to do with the match. Once the block stopped holding, the
            # match became the whole interlock on the stored path and the only one this
            # path was not carrying (rule 3/22).
            **_replayed_evidence(row),
        )
        score_value = judged.score
        verdict = effective_fate(judged, override)

        histogram[min(score_value // 10, 9)] += 1
        # The pre-edit fate is the EFFECTIVE one (override applied), matching the effective "now"
        # verdict below -- so a hand reap's condemnation is never miscounted as a change the
        # POLICY edit caused. The stored verdict alone is pure policy and would misattribute it.
        was = effective_verdict(row, decisions)
        was_condemned = was == "condemn"
        if was_condemned:
            before += 1
        # `changed` counts every lane move, not only the two that cross the threshold: a
        # protection edit moves titles between protect and abstain without going near it,
        # which the two deltas cannot see by construction, and the panel's other rows report
        # as absolute counts (#488).
        #
        # Both deltas read off `was` and `verdict` alone, so each counts every way into and
        # out of the removal list. `gone` was counted in the abstain arm below, which made it
        # condemn -> abstain only: an outright keep takes a condemned title straight to
        # protect, so a rule that emptied most of the removal list reported -0.
        if verdict != was:
            changed += 1
            if verdict == "condemn":
                newly += 1
            elif was_condemned:
                gone += 1
        if verdict == "condemn":
            condemned += 1
            if row.size_bytes is None:
                unknown_size += 1
            else:
                reclaimable += row.size_bytes
            if not was_condemned:
                newly_rows.append((row, score_value))
        elif verdict == "protect":
            protected += 1
            # A set for the same reason the threshold path's spare branch uses one: a row
            # both hand-spared and keep-list tagged carries the injected WHITELISTED and
            # the gate's own, and a gate spares a row once.
            spared_by.update(_spared_by_id(r) for r in judged.evaluation.protectors)
        else:
            abstained += 1

    newly_rows.sort(key=lambda rs: rs[1], reverse=True)
    return SimulationOut(
        exact=True,
        condemned=condemned,
        protected=protected,
        abstained=abstained,
        reclaimable_bytes=reclaimable,
        unknown_size_items=unknown_size,
        newly_condemned=newly,
        no_longer_condemned=gone,
        condemned_before=before,
        changed_titles=changed,
        histogram=histogram,
        examples_newly_condemned=[
            SimExampleOut(title=r.title, year=r.year, score=s) for r, s in newly_rows[:5]
        ],
        protected_by=[
            GateCountOut(gate=gate, count=n)
            for gate, n in sorted(spared_by.items(), key=lambda kv: (-kv[1], kv[0]))
        ],
    )


@router.post("/policy/simulate", tags=[api_tags.POLICY])
async def simulate(request: Request, payload: PolicyIn) -> SimulationOut:
    """Re-decide the last snapshot under a candidate policy. **Zero API calls.**

    This is what makes threshold-tuning honest: the knob and its blast radius sit in the
    same viewport. Move the threshold, and the count, the byte total and the histogram
    move with it -- instantly, without touching Sonarr, Radarr or Tautulli.

    **It works for whatever the frozen evidence can still answer, and refuses the rest.**
    Three tiers, most exact first, enumerated again at the branch below:

    1. ``scoring_hash`` matches -- re-compare the *stored* scores and verdicts against the
       new numbers, through the same ``engine.verdict`` decision the scan uses. Exact for
       ``condemn_at`` and ``coverage_floor_bp``, and the cheapest path.
    2. Scoring changed but ``evidence_hash`` matches, and there is at least one governed row
       and every one of them froze its Facts -- **replay** the real
       ``score``/``evaluate_all``/``decide_verdict`` over ``Candidate.facts_json`` under the
       edited policy (``_replay_simulation``). Exact for every field in
       ``PolicyBody._EVIDENCE_REPLAYABLE_FIELDS``: a weight, a rating bar, a custom condemn
       rule, a graded keep, a protect condition, a protection switched on or off, or any of
       the nine season rules. Still zero API calls.
    3. Otherwise the edit changed what a scan would *gather* (the popularity window) -- the
       frozen evidence is stale, so it **returns nothing but the reason**. A plausible wrong
       answer is worse than a blank: the owner acts on it.

    Tier 2 needs a snapshot that actually froze its evidence: a pre-facts-freeze snapshot
    has a null ``evidence_hash`` or rows with no ``facts_json``, and falls to tier 3.

    **One refusal sits above all three**, because it is not about the policy at all: the
    protection lists decide which titles an ``on_list`` rule keeps, they are not policy
    fields, and both tiers read evidence derived from them. So a registry that no longer
    fingerprints the way the snapshot recorded refuses outright (``list_config.fingerprint``,
    #512), whatever the edit was -- including no edit at all, which is the case where the
    operator has changed a list and the panel would otherwise restate pre-edit numbers.

    **The TV lane has a second precondition the hash cannot express.** A season's guard is
    re-derived from a per-show bundle the scan freezes beside the Facts
    (``db.models.SeasonPruneEvidence``), and two policies that gather identically can still
    disagree about whether that bundle is there and describes the row. A draft holding the
    mid-binge seasons over a scan that ran with the hold off is the same shape: the policy
    gathers the same things, and Sonarr's episode lists were never asked for, so there is
    nothing to place a viewer in. A scan that asked and got no answer is not that state -- it
    planned from the empty map, and a replay off the same map returns what it decided (#500).
    Both questions are asked of the stored evidence in ``_SeasonReplay``, and each refuses
    with its own ``SimStale`` so the panel names the control at fault (#491, #495).

    **This is not the state an upgrading install is in**, which is worth saying because the
    obvious reading is wrong. Moving the season fields out of ``evidence_hash`` changed the
    formula, so a snapshot recorded by an earlier build cannot match it whatever the operator
    does, and lands on tier 3 for *any* edit until the next scan -- the one-scan cost
    ``PolicyBody.evidence_hash`` documents. The season-specific refusals cover a bundle that
    is missing, unreadable, or short **after** that scan, not the upgrade itself.

    Two kinds of row are never re-decided on score: a row with a protection that could
    not be checked stays abstained at any threshold (the scan refuses to condemn on
    unchecked protections), and a row under a hand override follows the owner's decision
    whatever the threshold: a spare protects, and a reap condemns when the engine
    honors it (services.condemned).

    With separate movie and TV policies, the snapshot's scoring hash is the *combination* of
    both. Editing one leaves the other untouched, so we recombine the candidate policy with the
    other type's current policy and compare that -- and re-decide only the candidates the edited
    policy governs (movies, or seasons), since the other type's verdicts have not moved.
    """
    body = _to_body(payload)
    target = _candidate_media_type(body.media_type)

    async with session_factory(request)() as session:
        snapshot = await newest_snapshot(session)
        if snapshot is None:
            raise HTTPException(404, "No scan has run yet, so there is nothing to simulate.")

        other_type = "movie" if body.media_type == "tv" else "tv"
        other = (await active_policy(session, other_type)).body

        def _combined(pick: Callable[[PolicyBody], str]) -> str:
            movie_h, tv_h = (
                (pick(body), pick(other))
                if body.media_type == "movie"
                else (pick(other), pick(body))
            )
            return combine_hashes(movie_h, tv_h)

        rows = (
            (
                await session.execute(
                    select(Candidate)
                    .where(
                        Candidate.snapshot_id == snapshot.id,
                        Candidate.media_type == target,
                    )
                    # Only the columns both loops below actually read. A full entity load
                    # dragged every row's summary, genres and artwork keys through the
                    # request as well, and on a large library that materialization is a
                    # bigger share of the stall than the scoring itself (P2-1).
                    .options(
                        load_only(
                            Candidate.media_key,
                            Candidate.media_type,
                            Candidate.title,
                            Candidate.year,
                            Candidate.score,
                            Candidate.coverage_bp,
                            Candidate.verdict,
                            Candidate.size_bytes,
                            Candidate.facts_json,
                            Candidate.explanation_json,
                        )
                    )
                )
            )
            .scalars()
            .all()
        )
        decisions = await whitelist.overrides(session)

        # Above the tier split, because BOTH tiers below read evidence derived from list
        # membership: the replay through the frozen `Facts.on_lists`, the threshold path
        # through the stored verdict that fact produced. The lists are not policy, so no hash
        # either tier compares can notice that the operator retagged, renamed, repointed or
        # switched one off -- and the panel would report the membership the scan froze while
        # calling it exact (#512).
        #
        # Either side being `None` refuses on its own, and neither is compared to the other:
        # a snapshot predating the column has one, a scan that degraded for an unreadable
        # registry stamped one (`scan_runner`), and `current_fingerprint` returns one when the
        # registry cannot be read right now. `None` is "unknown" on both sides, never "no
        # lists" (rules 93, 104) -- so an equality test alone answered "exact" for two
        # unknowns, which is the one pairing where both scans were degraded.
        current = await list_config.current_fingerprint(session)
        if (
            snapshot.list_config_hash is None
            or current is None
            or snapshot.list_config_hash != current
        ):
            return _refused(SimStale.GATHERS_DIFFERENTLY, body.media_type)

        # Three tiers of re-decide, most exact first:
        #  1. Scoring behavior unchanged -> re-compare the STORED score against the new
        #     thresholds. Exact and cheapest (below).
        #  2. Scoring changed but the EVIDENCE is unchanged, and every governed row froze its
        #     Facts -> replay the real engine over those Facts. Exact for weight/rating/custom
        #     edits, still zero API calls.
        #  3. Otherwise the edit changed what the scan gathers (the popularity window)
        #     -> the frozen evidence is stale, so refuse rather than guess.
        if snapshot.scoring_hash != _combined(PolicyBody.scoring_hash):
            replayable = snapshot.evidence_hash and snapshot.evidence_hash == _combined(
                PolicyBody.evidence_hash
            )
            if replayable and rows and all(r.facts_json for r in rows):
                try:
                    # Only the TV lane carries a season guard, so only it reads the frozen
                    # bundles. A movie replay passes None and skips the whole re-derivation.
                    payloads = (
                        await _season_payloads(session, snapshot_id=snapshot.id)
                        if target == "season"
                        else None
                    )
                    return await _replay_simulation(
                        list(rows),
                        body,
                        decisions,
                        # From the snapshot's own two stored instants, so a row frozen before
                        # the reach was a fact replays on the reach that scan actually had.
                        reach_days=history_reach_days(snapshot.horizon_at, now=snapshot.created_at),
                        season_payloads=payloads,
                    )
                except _SeasonEvidenceMissingError as missing:
                    return _refused(missing.kind, body.media_type)
            return _refused(SimStale.GATHERS_DIFFERENTLY, body.media_type)

    histogram = [0] * 10
    condemned = protected = abstained = 0
    reclaimable = 0
    unknown_size = 0
    newly = gone = 0
    before = 0
    changed = 0
    newly_rows: list[Candidate] = []
    spared_by: Counter[str] = Counter()

    for index, row in enumerate(rows):
        # The threshold path is cheaper per row than the replay above, but it still parses
        # an explanation per protect/abstain row over the whole library, so it yields too.
        if index % _SIM_YIELD_EVERY == 0:
            await asyncio.sleep(0)
        histogram[min(row.score // 10, 9)] += 1

        # The EFFECTIVE pre-edit fate, read exactly as the replay path reads it, so "titles
        # that change" means one thing whichever tier answered (rule 72). It was the raw
        # stored verdict here, which is pure policy: harmless while every override row
        # returned before this was read, and a trap for the next branch added above them.
        was = effective_verdict(row, decisions)
        was_condemned = was == "condemn"
        if was_condemned:
            before += 1

        # A hand override wins at any threshold: the owner looked and decided, and the
        # scan honors that decision, so the simulator must too. A spare protects; a
        # reap condemns exactly when the engine honors it (services.condemned) -- a
        # safety stop or an unchecked protection still keeps the file, and re-deciding
        # such a row on its score would report movement no scan will ever show.
        override = whitelist.effective_override(row.media_key, decisions)
        if override is not None:
            if override == "spare":
                protected += 1
                # Credit every protection that fired, not the hand spare alone. The replay
                # path counts them all (``_replay_simulation``, which injects the spare
                # ALONGSIDE the gate results), so crediting one here made the "why titles
                # were spared" tally jump the moment any scoring edit moved the route to
                # that path -- one protection per hand-spared row that had also earned it,
                # appearing as an effect of an edit that cannot change which gates fire,
                # beside a headline count that correctly held still.
                # A set because a row both hand-spared and keep-list tagged fires
                # WHITELISTED twice, and a gate spares a row once.
                spared_by.update({*_fired_gates(row.explanation_json), HAND_SPARE_TALLY_ID})
            elif reap_is_effective(row):
                condemned += 1
                if row.size_bytes is None:
                    unknown_size += 1
                else:
                    reclaimable += row.size_bytes
            else:
                # A hand reap the engine will not honor yet is KEPT for now (a held reap), so it
                # buckets as protected -- matching condemned.effective_verdict. Its own pure-policy
                # verdict (protect or abstain) is stored raw now and no longer stands in for the
                # effective fate here.
                protected += 1
                spared_by.update(_fired_gates(row.explanation_json))
            # Nothing added to `changed`: the lane these three arms pick IS `was`. Both read
            # the override first and fall back to the same stored explanation through
            # ``condemned``, and neither consults a threshold, so an override row cannot move.
            continue

        # A protection always wins, whatever the threshold. Only the score-based
        # verdicts can move.
        if row.verdict == "protect":
            protected += 1
            spared_by.update(_fired_gates(row.explanation_json))
            # `was` is the stored verdict on this arm (no override), so protect stays protect.
            continue

        # A row with a protection that could not be checked abstains at ANY threshold:
        # the scan refuses to condemn on unchecked protections ("we could not look" is
        # not "we looked and it was fine"), so counting it as a deletion here would be
        # exactly the plausible wrong answer this route promises to refuse.
        if _has_blocked_protections(row.explanation_json):
            abstained += 1
            if was != "abstain":
                changed += 1
            # A stored condemn cannot also carry a blocking protection (decide_verdict
            # abstains on one), so this only fires for a row whose explanation is
            # unreadable -- and if that row was condemned, it genuinely is not any more.
            if was_condemned:
                gone += 1
            continue

        now_condemned = (
            decide_verdict(
                protected=False,
                blocked=False,
                score=row.score,
                coverage_bp=row.coverage_bp,
                condemn_at=body.condemn_at,
                coverage_floor_bp=body.coverage_floor_bp,
            )
            == "condemn"
        )
        if ("condemn" if now_condemned else "abstain") != was:
            changed += 1

        if now_condemned:
            condemned += 1
            if row.size_bytes is None:
                unknown_size += 1
            else:
                reclaimable += row.size_bytes
            if not was_condemned:
                newly += 1
                newly_rows.append(row)
        else:
            abstained += 1
            if was_condemned:
                gone += 1

    # The few names the owner will actually recognize: the highest-scoring titles this
    # draft flags that the saved policy does not. A count is abstract; a familiar title
    # is what stops a bad threshold before it is saved.
    newly_rows.sort(key=lambda r: r.score, reverse=True)

    return SimulationOut(
        exact=True,
        condemned=condemned,
        protected=protected,
        abstained=abstained,
        reclaimable_bytes=reclaimable,
        unknown_size_items=unknown_size,
        newly_condemned=newly,
        no_longer_condemned=gone,
        condemned_before=before,
        changed_titles=changed,
        histogram=histogram,
        examples_newly_condemned=[
            SimExampleOut(title=r.title, year=r.year, score=r.score) for r in newly_rows[:5]
        ],
        protected_by=[
            GateCountOut(gate=gate, count=n)
            for gate, n in sorted(spared_by.items(), key=lambda kv: (-kv[1], kv[0]))
        ],
    )


#: The spared-by tally's id for a hand spare. Not a gate: the replay path injects the spare as
#: a WHITELISTED protector so ``judge_facts`` applies it live, and the threshold path adds it
#: beside the gates that fired. Tallying it under ``whitelisted`` reported every hand spare as
#: list membership -- and on a fresh install, where ``WhitelistGate`` is retired and no gate
#: emits that id at all, "Why titles were spared" was hand spares wearing a list's name, which
#: invites softening a keep rule that covers none of them (rule 144). The frontend's
#: ``GATE_META`` carries the copy for it.
HAND_SPARE_TALLY_ID = "hand_spare"


def _spared_by_id(result: GateResult) -> str:
    """The tally id for one protector: its gate, unless it is the injected hand spare."""
    return HAND_SPARE_TALLY_ID if result.detail == HAND_SPARE_DETAIL else result.gate.value


def _fired_gates(explanation_json: str) -> list[str]:
    """The gates that fired in one stored explanation, for the spared-by tally.

    Parsed by ``_decode_explanation`` and read through ``_entries``, the same two guards
    every display extractor above goes through. It used to guard only the ``json.loads``
    and then call ``.get`` on whatever came back, so a stored top level that is a list, a
    null or a number raised an AttributeError out of the whole simulation -- one legacy row
    500-ing the policy editor's live preview on every drag of a threshold.

    The unreadable fallback is an EMPTY tally, and here that is the cautious direction: the
    caller has already counted the row in ``protected`` before asking this, so losing the
    attribution under-credits the "what saved these" list and can never move an item toward
    deletion. ``_has_blocked_protections`` reads the opposite way for exactly that reason.
    """
    exp = _decode_explanation(explanation_json)
    if exp is None:
        return []
    return [str(entry["gate"]) for entry in _entries(exp, "protections_fired") if entry.get("gate")]


def _has_blocked_protections(explanation_json: str) -> bool:
    """Did this row store any protection that could not be checked?

    Read from the stored explanation's ``protections_unknown`` block, the same record the
    why-panel renders amber, and parsed by ``_decode_explanation`` so a stored top level
    that is a list, a null or a number degrades instead of raising out of the simulation.

    The unreadable fallback HOLDS the row (rule 96). This answer is the only thing standing
    between a row and a score-based condemn in the loop below, so reading an explanation we
    could not parse as "nothing was blocking" would turn evidence we cannot see into
    evidence that nothing was wrong, and preview a deletion the scan would refuse. Present
    but unreadable is blocked; genuinely absent stays permissive, since a readable
    explanation with no ``protections_unknown`` (or an empty one) is a scan that looked and
    found nothing holding the item.

    ``_entries`` is deliberately not used to read the list: it drops entries that are not
    objects, which would read a malformed block as an empty one -- the permissive
    direction. Any entry at all holds, readable or not.
    """
    exp = _decode_explanation(explanation_json)
    if exp is None:
        return True
    unknown = exp.get("protections_unknown")
    if unknown is None:
        return False
    return bool(unknown) if isinstance(unknown, list) else True
