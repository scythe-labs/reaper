# SPDX-License-Identifier: AGPL-3.0-or-later
"""The keep rules that make a list act, maintained beside the registry.

A list is defined on Settings -> Lists; what it does is a keep rule on Policy naming it
(the ``on_list`` field, either strength). These helpers keep the two surfaces telling one
story:

* **adding a list writes no rule.** The operator chooses whether and how strongly it
  protects, on Policy, and the Lists row reads "Not used by your policy yet" until they do
  -- so adding a list is never a silent protection nobody asked for. The lists Reaper ships
  and the ones an upgrade migrated are named by the policy body itself (the default body,
  ``convert_list_protections``), which is why those act from the first scan and a hand-added
  one does not;
* **renaming a list re-spells the rules naming it**, so a rename never turns a live
  protection into a rule naming nothing;
* **deleting a list deletes its rules**, so none goes on rendering as a live protection
  covering nothing (rule 25).

Every write lands through the same append-only shape the policy editor's save uses: a new
policy row per media type, hash recomputed, active-by-recency semantics unchanged -- so a
pending approval bound to the old hash refuses to execute, exactly as after any policy
edit (rule 113).

**A repaired policy is never written back from here.** ``active_policy`` can hand back a
body a shim produced (rescaled, recovered, converted); persisting that plus one rule would
silently adopt the whole repair without the operator's review, the substitution rule 65
forbids. Those cases skip the write and the Lists screen shows the honest result: the list
is not used by the policy yet.
"""

from __future__ import annotations

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from reaper.clock import utcnow
from reaper.db.models import Policy as PolicyModel
from reaper.engine.policy import PolicyBody
from reaper.services import profiles
from reaper.text import fold

log = structlog.get_logger(__name__)

MEDIA_TYPES = ("movie", "tv")


def _names_equal(a: object, b: str) -> bool:
    """Both sides case-folded (rule 88): the rule's value and the list's name are each the
    operator's typing, at different times."""
    return fold(str(a)) == fold(b)


async def _save(session: AsyncSession, media_type: str, body: PolicyBody) -> None:
    """Append the edited body, the editor's own semantics: no-op when the hash is already
    the active one, never an UPDATE."""
    active_row = await profiles.active_policy_row(session, media_type)
    if active_row is not None and active_row.policy_hash == body.policy_hash():
        return
    session.add(
        PolicyModel(
            policy_hash=body.policy_hash(),
            body_json=body.model_dump_json(),
            media_type=media_type,
            name=active_row.name if active_row is not None else "default",
            created_at=utcnow(),
        )
    )


async def rename_list(session: AsyncSession, old: str, new: str) -> None:
    """Re-spell every rule naming the list, both strengths, both policies."""
    if fold(old) == fold(new):
        return
    for media_type in MEDIA_TYPES:
        active = await profiles.active_policy(session, media_type)
        if active.repaired:
            log.warning("list_rules.rename_skipped_repaired", media_type=media_type)
            continue
        body = active.body
        conditions = tuple(
            c.model_copy(update={"value": new})
            if c.field == "on_list" and _names_equal(c.value, old)
            else c
            for c in body.protect_conditions
        )
        keeps = tuple(
            k.model_copy(update={"value": new})
            if k.field == "on_list" and k.value is not None and _names_equal(k.value, old)
            else k
            for k in body.graded_keeps
        )
        if conditions == body.protect_conditions and keeps == body.graded_keeps:
            continue
        edited = body.model_copy(update={"protect_conditions": conditions, "graded_keeps": keeps})
        await _save(session, media_type, PolicyBody.model_validate(edited.model_dump()))
    await session.commit()


async def detach_list(session: AsyncSession, name: str) -> None:
    """Remove every rule naming the list, both strengths, both policies. Paired with
    ``list_config.delete`` by the API route, so a deleted list's rules leave with it."""
    for media_type in MEDIA_TYPES:
        active = await profiles.active_policy(session, media_type)
        if active.repaired:
            log.warning("list_rules.detach_skipped_repaired", media_type=media_type)
            continue
        body = active.body
        conditions = tuple(
            c
            for c in body.protect_conditions
            if not (c.field == "on_list" and _names_equal(c.value, name))
        )
        keeps = tuple(
            k
            for k in body.graded_keeps
            if not (k.field == "on_list" and k.value is not None and _names_equal(k.value, name))
        )
        if conditions == body.protect_conditions and keeps == body.graded_keeps:
            continue
        edited = body.model_copy(update={"protect_conditions": conditions, "graded_keeps": keeps})
        await _save(session, media_type, PolicyBody.model_validate(edited.model_dump()))
    await session.commit()


async def usage(session: AsyncSession) -> dict[str, list[dict[str, object]]]:
    """How the policies use each list, keyed by the list name's case-folded form.

    Each entry is ``{"media_type", "strength", "points"}``, the shape
    ``api.schemas.ListPolicyUseOut`` serializes. Read from the same ``active_policy`` the
    scan reads, shims included, so this line and the scan cannot tell two stories about
    whether a list is protecting.
    """
    out: dict[str, list[dict[str, object]]] = {}
    for media_type in MEDIA_TYPES:
        active = await profiles.active_policy(session, media_type)
        for c in active.body.protect_conditions:
            if c.field == "on_list":
                key = fold(str(c.value))
                out.setdefault(key, []).append(
                    {"media_type": media_type, "strength": "hard", "points": None}
                )
        for k in active.body.graded_keeps:
            if k.field == "on_list" and k.value is not None:
                key = fold(k.value)
                out.setdefault(key, []).append(
                    {"media_type": media_type, "strength": "lean", "points": k.max_discount}
                )
    return out


__all__ = ["MEDIA_TYPES", "detach_list", "rename_list", "usage"]
