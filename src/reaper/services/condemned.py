# SPDX-License-Identifier: AGPL-3.0-or-later
"""The effective condemned set: what a reap may actually act on, right now.

A snapshot is frozen evidence, but the owner keeps deciding after it is taken: a hand
**spare** removes an item from everything destructive, and a hand **reap** adds one --
the owner looked and decided -- unless a hard safety stop still holds. Before this
module, only the spare half was applied post-snapshot; a hand reap silently waited for
the next scan while the queue showed no change and the plan excluded it.

This is the ONE place that assembles "condemned, as of this moment":

    scan-condemned rows  -  hand-spares  +  hand-reaps that decide_verdict honors

Everything that acts or counts imports it -- grace (and through it the Leaving Soon
shelf), the planner's step expansion, the confirmation-phrase count, the executor's
per-item keep-set, and the per-show numbers the queue shows beside destructive buttons
-- so the number beside a button is derived from the exact set the server will act on.

The decision itself is NOT here. ``decide_verdict`` is the single decision function;
:func:`reap_override_verdict` only plumbs a frozen row's stored facts into it. Anything
unreadable in those facts reads as *blocked*, which decide_verdict resolves toward
keeping the file.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from reaper.db.models import Candidate
from reaper.engine.verdict import STRUCTURAL_GATES, block_holds_reap, decide_verdict
from reaper.services import whitelist

#: A stored ``match`` block that is present but is not an object. Distinct from a match
#: that is genuinely absent, and it is not a status Plex ever reports: it is what
#: :func:`match_state` returns for a record it cannot read.
MATCH_UNREADABLE = "unreadable"

#: Match states that HOLD a hand reap. Unmatched and ambiguous are the two Plex reports
#: meaning "we could not tie this to one thing"; unreadable is the same not-checked state
#: arrived at differently, so it belongs with them.
BAD_MATCH_STATES = frozenset({MATCH_UNREADABLE, "unmatched", "ambiguous"})


def match_state(explanation: Mapping[str, Any]) -> str | None:
    """The Plex match state a stored explanation records, or ``None`` if it records none.

    The one derivation of this three-way answer (rule 104). Both the reap-override
    decision below and the review queue's chips and card reasons read it, and they used to
    disagree: the decision treated an unreadable match as a hold while the card told the
    operator the item had merely "scored below your threshold", which is the opposite of
    the decision actually in force (rule 61).

    Three answers, and the middle one is the whole point:

    * a status Plex reported (``matched`` / ``unmatched`` / ``ambiguous``);
    * :data:`MATCH_UNREADABLE`, when the block is THERE but is not an object. That is "we
      cannot tell what this was tied to in Plex", the same not-checked state ``unmatched``
      and ``ambiguous`` describe, and it holds a reap. Reading it as absent would be the
      inversion this codebase exists to avoid: evidence we could not read becoming
      evidence that nothing was wrong, which REMOVES a hold on a deletion (B-10, rule 96);
    * ``None``, when the block is genuinely absent (missing, or null). That stays
      permissive, because the field is optional precisely so a row scored before it
      shipped still reads, and those rows were judged fine without it.
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

    Inputs are derived from the frozen explanation -- which protections fired, whether
    any could not be checked, whether the Plex match was clean -- and handed to
    ``decide_verdict`` with ``override="reap"``. That branch consults only
    ``blocked_holds_reap`` and ``safety_protected`` (a structural gate: streaming right
    now, or a file no *arr manages); the score, coverage and thresholds are never read on
    it, so the zeros below are inert plumbing, pinned by a test, not hidden policy.

    A block that could not be *checked* holds the reap (fail-closed); a block that is a
    deliberate "the owner should decide" flag does not, since the reap IS that decision.
    Today that flag is the keep-rule conflict, and only the half of it whose comparisons all
    came back with a number -- the same conflict raised because a kept season could not be
    read is a plumbing failure and holds (:func:`reaper.engine.verdict.block_holds_reap`).
    A malformed or unreadable explanation reads as blocked: not being able to see why an
    item was kept is not permission to remove it.
    """
    try:
        exp = json.loads(explanation_json)
    except (ValueError, TypeError):
        exp = None
    return reap_override_verdict_decoded(exp, score=score)


def _protection_entries(value: object) -> tuple[list[dict[Any, Any]], bool]:
    """One stored protections list, as ``(readable object entries, anything unreadable)``.

    Three cases, kept apart on purpose (rule 1). ``None`` is genuinely absent -- the scan
    looked and stored nothing -- and stays permissive. A list is readable, and its object
    entries come back for the ``.get`` readers below. Anything else is unreadable: a scalar
    where a list belongs, or a list carrying an entry that is not an object.

    Filtering the unreadable entries away *silently* is the bug this replaces. Three strings
    in ``protections_unknown`` filtered down to an empty list, so ``blocked`` went ``False``
    and a hand reap condemned a file whose kept-reasons nobody could read -- evidence we
    cannot see turned into evidence that nothing was wrong. The list is judged before it is
    filtered, and an unreadable entry holds the reap exactly as a bad Plex match does
    (rule 96). ``api.routes._has_blocked_protections`` reads the same stored block with the
    same posture and says so in its own docstring; these two must not disagree, because the
    permissive one is the one on the destructive path.
    """
    if value is None:
        return [], False
    if not isinstance(value, list):
        return [], True
    entries = [e for e in value if isinstance(e, dict)]
    return entries, len(entries) != len(value)


def reap_override_verdict_decoded(explanation: object, *, score: int) -> str:
    """:func:`reap_override_verdict` over an ALREADY-DECODED explanation.

    Same decision, same fail-closed posture -- the only difference is that the caller
    already ran ``json.loads``. The review queue decodes each row's explanation once and
    hands the dict to every display extractor and to this, rather than re-parsing the
    same multi-KB document three or four times per row.

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
    unreadable = fired_unreadable or unknown_unreadable
    bad_match = match_state(exp) in BAD_MATCH_STATES

    return decide_verdict(
        protected=bool(fired),
        # Truthful but inert, like the zeros below: the reap branch reads only
        # ``blocked_holds_reap`` and ``safety_protected``, so nothing here is the interlock.
        blocked=bool(unknown) or bad_match or unreadable,
        # THIS is the interlock. A block holds the reap unless it is a deliberate "you
        # decide" deferral (the keep-rule conflict); a bad Plex match always holds, and so
        # does anything in either list this code could not read. Read off the same primitive
        # the scan uses, so a stored explanation and a live evaluation agree.
        blocked_holds_reap=bad_match
        or unreadable
        or any(
            block_holds_reap(
                str(e.get("gate") or ""),
                # ``is True`` is the thaw, not a style tic. A row frozen before the flag
                # shipped carries no key at all, and there is nothing in it that can tell
                # a made comparison from a refused one -- the wording that used to stand
                # in for the flag is exactly what failed. So absent reads as "does not
                # defer" and the reap is held, the keep direction (rule 104). Self-clearing
                # without needing a rewrite: a later scan stores its own rows, and every
                # reap path keys on a snapshot id, so pre-flag rows go unreachable.
                defers_to_owner=e.get("defers_to_owner") is True,
            )
            for e in unknown
        ),
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

    The backend twin of the frontend's ``handFate``, collapsed to the three stored lanes: a
    hand spare -- and a hand reap the engine will not honor yet -- keep the item (``"protect"``);
    a honored hand reap condemns it (``"condemn"``); with no override the pure-policy verdict
    stands. This is the ONE place a stored (pure-policy) verdict is turned into an effective
    lane, so the review-queue tab filter and the scan-summary counts route through it and can
    never disagree with :func:`effective_condemned`.
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

    Only an overridden row can change lanes, so only overridden rows are read here -- the raw
    ``verdict == lane`` query stays the cheap indexed path, and a caller splices these few moves
    onto it (the tab filter) or applies them as count deltas (the scan summary). Returns
    ``(candidate, from_lane, to_lane)`` for each row whose :func:`effective_verdict` differs from
    its stored verdict. A show-level override reaches its seasons by ``group_key``; a per-item
    override by ``media_key``.
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
    """The hand reaps the engine will NOT honor yet: a reap override on a row the scan did not
    condemn, whose reap is refused (blocked evidence, a structural gate). These are HELD, not
    in the reap set -- the complement of what :func:`effective_condemned` admits from the same
    overridden rows, surfaced so the breakdown can report the operator's marks that are on
    hold rather than dropping them silently (PR-2). Reuses ``reap_is_effective``, never a
    second copy of the honor test.
    """
    reap_keys = sorted(k for k, d in decisions.items() if d == "reap")
    return [
        c
        for c in await _reap_overridden_rows(session, snapshot_id, reap_keys)
        if whitelist.effective_override(c.media_key, decisions) == "reap"
        and not reap_is_effective(c)
    ]
