# SPDX-License-Identifier: AGPL-3.0-or-later
"""The effective condemned set: what a reap may actually act on, right now.

A snapshot freezes evidence, but the owner keeps deciding after it is taken. A hand
**spare** removes an item from every destructive action, and a hand **reap** adds one,
unless a hard safety stop still holds.

This is the one place that assembles "condemned, as of this moment":

    scan-condemned rows  -  hand-spares  +  hand-reaps that decide_verdict honors

Everything that acts or counts on this set imports it: grace (and through it the
Leaving Soon shelf), the planner's step expansion, the confirmation-phrase count, the
executor's per-item keep-set, and the per-show numbers the queue shows beside
destructive buttons. The number beside a button always comes from the exact set the
server will act on.

The decision itself lives elsewhere. ``decide_verdict`` is the single decision
function. :func:`reap_override_verdict` only feeds a frozen row's stored facts into it.
Anything unreadable in those facts reads as *blocked*, and decide_verdict resolves a
block toward keeping the file.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from reaper.db.models import Candidate
from reaper.engine.explanation import read_explanation
from reaper.engine.identity import MatchStatus
from reaper.engine.verdict import STRUCTURAL_GATES, decide_verdict
from reaper.services import whitelist

#: A stored ``match`` block that is present but is not an object. Distinct from a match
#: that is genuinely absent, and it is not a status Plex ever reports: it is what
#: :func:`match_state` returns for a record it cannot read.
MATCH_UNREADABLE = "unreadable"

#: The one match state that does not hold a hand reap. Every other value does, including
#: a status this build has never seen before: :func:`bad_match` tests against this single
#: clean value, rather than against a list of bad ones. A list of known bad values could
#: miss a value it does not enumerate, such as one written by a later version and rolled
#: back. A missed value would read as a clean bind and let a reap through on a row the
#: resolver never actually identified.
MATCH_CLEAN = MatchStatus.MATCHED.value

#: Match states that hold a hand reap, for the drift test and for readers. It is every
#: resolver outcome that is not a confident bind, plus :data:`MATCH_UNREADABLE` (a
#: different way of not being able to tie a row to one thing). Built from the enum, never
#: listed by hand, so adding a new status later can never leave this set out of date.
#: This documents the known bad values. It is not the gate: :func:`bad_match` checks
#: against :data:`MATCH_CLEAN` instead, so an unrecognized value still holds.
BAD_MATCH_STATES = frozenset(
    {MATCH_UNREADABLE, *(s.value for s in MatchStatus if s is not MatchStatus.MATCHED)}
)


def bad_match(explanation: Mapping[str, Any]) -> bool:
    """Does this row's Plex match hold a hand reap? True for anything but a confident bind.

    Checked against :data:`MATCH_CLEAN`, not :data:`BAD_MATCH_STATES`. An unrecognized
    status means Reaper does not know what this row is, so it must hold. Checking
    against the list of known bad values instead would let an unrecognized status read
    as clean. ``None`` (no match block at all) stays permissive; see :func:`match_state`.
    """
    state = match_state(explanation)
    return state is not None and state != MATCH_CLEAN


def match_state(explanation: Mapping[str, Any]) -> str | None:
    """The Plex match state a stored explanation records, or ``None`` if it records none.

    The one place this three-way answer is derived. Both the reap-override decision
    below and the review queue's chips and card reasons read it, so the two can never
    disagree about what an unreadable or missing match means.

    Three answers, and the middle one matters most:

    * a status the resolver recorded (:class:`~reaper.engine.identity.MatchStatus`);
    * :data:`MATCH_UNREADABLE`, when the block is there but is not an object. Reaper
      cannot tell what this row was tied to in Plex, the same as any non-matched status,
      so it holds a reap. Reading this as absent would turn evidence Reaper could not
      read into evidence that nothing was wrong, removing a hold that should stay in
      place;
    * ``None``, when the block is genuinely absent (missing, or null). This stays
      permissive: the field is optional so a row scored before it existed still reads,
      and those rows were judged fine without it.
    """
    match = explanation.get("match")
    if match is None:
        return None
    if not isinstance(match, dict):
        return MATCH_UNREADABLE
    status = match.get("status")
    return str(status) if status else None


def reap_override_verdict(explanation_json: str, *, score: int) -> str:
    """What a hand reap does to one stored row: ``"condemn"`` or ``"protect"``.

    Reads the frozen explanation (which protections fired, whether any could not be
    checked, whether the Plex match was clean) and hands it to ``decide_verdict`` with
    ``override="reap"``. That branch reads only ``blocked_holds_reap`` and
    ``safety_protected`` (streaming right now, or a file no *arr manages). It never reads
    the score, coverage, or thresholds, so the zero values passed below do nothing; a
    test pins that they stay inert.

    A protection that could not be checked does not hold the reap. The owner is looking
    at the panel that names the check that came back empty, and clicking reap is their
    answer to it. Only a protection that actually fired refuses the reap.

    Two other holds remain, and neither is a protection. A bad Plex match means the file
    behind this row may not be the one the owner is looking at, so the reap could remove
    something they never saw. A malformed or unreadable explanation means the panel could
    not render any reasons at all, so there was nothing to consent to. Both mean Reaper
    does not know what this row is, which is different from not knowing whether it is
    wanted.
    """
    try:
        exp = json.loads(explanation_json)
    except (ValueError, TypeError):
        exp = None
    return reap_override_verdict_decoded(exp, score=score)


def _protection_entries(value: object) -> tuple[list[dict[Any, Any]], bool]:
    """One stored protections list, as ``(readable object entries, anything unreadable)``.

    Three cases, kept apart. ``None`` means the scan looked and stored nothing, and stays
    permissive. A list is readable, and its object entries come back for the ``.get``
    readers below. Anything else is unreadable: a scalar where a list belongs, or a list
    carrying an entry that is not an object.

    An unreadable entry must hold the reap, the same as a bad Plex match, rather than
    being silently dropped from the count: dropping it can turn a real, unread protection
    into "nothing was blocking". ``api.simulate._has_blocked_protections`` reads the same
    stored block the same way; the two must never disagree, since this is the permissive
    side and it sits on the destructive path.

    This answers only which protections are legible enough to count. Whether the whole
    stored document can be rendered at all is a separate question, answered by
    ``engine.explanation.read_explanation``, which the caller checks alongside this
    function's two flags.
    """
    if value is None:
        return [], False
    if not isinstance(value, list):
        return [], True
    entries = [e for e in value if isinstance(e, dict)]
    return entries, len(entries) != len(value)


def has_blocked_protections_decoded(explanation: object) -> bool:
    """Did this row's already-decoded explanation carry a protection that could not be
    checked?

    Reads ``protections_unknown`` through :func:`_protection_entries`, the same
    readable/unreadable split :func:`reap_override_verdict_decoded` applies to the same
    block below, so a malformed entry holds here exactly as it holds a hand reap.
    Anything that is not a dict at the top level, including a decode failure the caller
    already turned into ``None``, also holds: evidence Reaper cannot read must never read
    as "nothing was blocking". ``api.simulate``'s threshold-only replay and
    ``api.policy``'s threshold curve both call this rather than parsing the block
    themselves, so all three answer "is this row unchecked" the same way.
    """
    if not isinstance(explanation, dict):
        return True
    unknown, unreadable = _protection_entries(explanation.get("protections_unknown"))
    return bool(unknown) or unreadable


def reap_override_verdict_decoded(explanation: object, *, score: int) -> str:
    """:func:`reap_override_verdict` over an already-decoded explanation.

    Same decision, same fail-closed posture. The only difference is that the caller
    already ran ``json.loads``. The review queue decodes each row's explanation once and
    hands the dict to every display extractor and to this function, instead of parsing
    the same document again for each one.

    Anything that is not a dict (a decode that failed, a stored top level that is a list
    or null) reads as blocked and keeps the file.
    """
    if not isinstance(explanation, dict):
        return decide_verdict(
            protected=False,
            blocked=True,
            score=score,
            coverage_bp=0,
            condemn_at=0,
            coverage_floor_bp=0,
            override="reap",
            safety_protected=True,
        )
    exp = explanation

    fired, fired_unreadable = _protection_entries(exp.get("protections_fired"))
    unknown, unknown_unreadable = _protection_entries(exp.get("protections_unknown"))
    # Whether the why panel can render this row's explanation at all. The two lists above
    # answer which protections fired or could not be checked; this is the wider question
    # of whether the stored document itself is legible, and it can only ever withdraw a
    # reap the operator was already being shown no reasons for.
    unreadable = fired_unreadable or unknown_unreadable or read_explanation(exp) is None
    match_holds = bad_match(exp)

    return decide_verdict(
        protected=bool(fired),
        # Truthful but inert, like the zeros below: the reap branch reads only
        # ``blocked_holds_reap`` and ``safety_protected``.
        blocked=bool(unknown) or match_holds or unreadable,
        # The actual interlock, and it is exactly two things, neither of them a
        # protection. A gate that could not be checked does not hold a hand reap: the
        # owner is reading the panel that names the failed check and is answering it.
        #
        # What still holds is not knowing what this row is. A bad Plex match means the
        # file behind this row may not be the one the owner is looking at, so the reap
        # could remove something they never saw. An entry Reaper could not parse means
        # the panel could not render any reasons, so there was nothing to consent to.
        #
        # ``unknown`` (the list of blocked gates) is not read here: a gate that could not
        # be checked does not hold a reap. It still drives ABSTAIN through ``blocked``
        # above, so nothing automatic touches the item either way.
        blocked_holds_reap=match_holds or unreadable,
        score=score,
        coverage_bp=0,
        condemn_at=0,
        coverage_floor_bp=0,
        override="reap",
        safety_protected=any(str(e.get("gate")) in STRUCTURAL_GATES for e in fired),
    )


def reap_is_effective(candidate: Candidate) -> bool:
    """Whether a hand reap on this frozen row actually condemns it."""
    if candidate.verdict == "condemn":
        return True
    return (
        reap_override_verdict(candidate.explanation_json, score=int(candidate.score)) == "condemn"
    )


def reap_is_effective_decoded(candidate: Candidate, explanation: object) -> bool:
    """:func:`reap_is_effective` for a caller that already decoded the explanation."""
    if candidate.verdict == "condemn":
        return True
    return reap_override_verdict_decoded(explanation, score=int(candidate.score)) == "condemn"


def effective_verdict(candidate: Candidate, decisions: dict[str, str]) -> str:
    """The lane an item lands in once its hand override is applied.

    The backend twin of the frontend's ``handFate``, collapsed to the three stored
    lanes. A hand spare keeps the item, and so does a hand reap the engine will not
    honor yet (``"protect"``). A honored hand reap condemns it (``"condemn"``). With no
    override, the pure-policy verdict stands. This is the one place a stored verdict
    becomes an effective lane, so the review-queue tab filter and the scan-summary
    counts can never disagree with :func:`effective_condemned`.
    """
    override = whitelist.effective_override(candidate.media_key, decisions)
    if override == "spare":
        return "protect"
    if override == "reap":
        return "condemn" if reap_is_effective(candidate) else "protect"
    return candidate.verdict


async def _reap_overridden_rows(
    session: AsyncSession, snapshot_id: int, reap_keys: list[str]
) -> list[Candidate]:
    """The not-already-condemned rows a reap decision could cover.

    A reap decision names an item (a movie's or a season's media_key) or a whole show (the
    seasons' ``group_key``). Shared by :func:`effective_condemned` and :func:`held_reaps` so
    the two never disagree on which rows a reap even touches.
    """
    if not reap_keys:
        return []
    return list(
        (
            await session.execute(
                select(Candidate).where(
                    Candidate.snapshot_id == snapshot_id,
                    Candidate.verdict != "condemn",
                    or_(
                        Candidate.media_key.in_(reap_keys),
                        Candidate.group_key.in_(reap_keys),
                    ),
                )
            )
        )
        .scalars()
        .all()
    )


async def effective_condemned(
    session: AsyncSession, snapshot_id: int, decisions: dict[str, str]
) -> dict[str, Candidate]:
    """Every candidate in this snapshot a reap may act on, keyed by ``media_key``.

    ``decisions`` is ``whitelist.overrides(session)``, passed in so one read serves the
    caller's whole request. Spares win over everything (a per-season spare beats a
    show-level reap, exactly as ``effective_override`` resolves it); a hand reap adds a
    row only when :func:`reap_override_verdict` condemns it.
    """
    condemned = (
        (
            await session.execute(
                select(Candidate).where(
                    Candidate.snapshot_id == snapshot_id, Candidate.verdict == "condemn"
                )
            )
        )
        .scalars()
        .all()
    )
    out = {
        c.media_key: c
        for c in condemned
        if whitelist.effective_override(c.media_key, decisions) != "spare"
    }

    reap_keys = sorted(k for k, d in decisions.items() if d == "reap")
    for c in await _reap_overridden_rows(session, snapshot_id, reap_keys):
        # A season spared back out of a hand-reaped show resolves to "spare" here.
        if whitelist.effective_override(c.media_key, decisions) != "reap":
            continue
        if reap_is_effective(c):
            out[c.media_key] = c
    return out


async def overridden_lane_shifts(
    session: AsyncSession, snapshot_id: int, decisions: dict[str, str]
) -> list[tuple[Candidate, str, str]]:
    """Every candidate a hand override moves to a lane other than its pure-policy verdict.

    Only an overridden row can change lanes, so only overridden rows are read here. The
    raw ``verdict == lane`` query stays the cheap indexed path, and a caller splices
    these few moves onto it (the tab filter) or applies them as count deltas (the scan
    summary). Returns ``(candidate, from_lane, to_lane)`` for each row whose
    :func:`effective_verdict` differs from its stored verdict. A show-level override
    reaches its seasons by ``group_key``; a per-item override by ``media_key``.
    """
    if not decisions:
        return []
    keys = sorted(decisions)
    affected = (
        (
            await session.execute(
                select(Candidate).where(
                    Candidate.snapshot_id == snapshot_id,
                    or_(Candidate.media_key.in_(keys), Candidate.group_key.in_(keys)),
                )
            )
        )
        .scalars()
        .all()
    )
    shifts: list[tuple[Candidate, str, str]] = []
    for c in affected:
        effective = effective_verdict(c, decisions)
        if effective != c.verdict:
            shifts.append((c, c.verdict, effective))
    return shifts


async def held_reaps(
    session: AsyncSession, snapshot_id: int, decisions: dict[str, str]
) -> list[Candidate]:
    """The hand reaps the engine will not honor yet.

    A reap override on a row the scan did not condemn, whose reap is refused: a fired
    structural gate, or a row Reaper cannot identify. Evidence that merely could not be
    checked no longer refuses one. These rows are held, not in the reap set: the
    complement of what :func:`effective_condemned` admits from the same overridden rows.
    Surfaced so the breakdown can report the operator's marks that are on hold, instead
    of dropping them silently. Reuses ``reap_is_effective`` rather than a second copy of
    the honor test.
    """
    reap_keys = sorted(k for k, d in decisions.items() if d == "reap")
    return [
        c
        for c in await _reap_overridden_rows(session, snapshot_id, reap_keys)
        if whitelist.effective_override(c.media_key, decisions) == "reap"
        and not reap_is_effective(c)
    ]
