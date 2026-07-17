# SPDX-License-Identifier: AGPL-3.0-or-later
"""The profile: how much Reaper may do, and how long it waits.

A profile is the container for the mutable, non-hashed part of a configuration -- the
four caps, the grace period, whether a run needs human approval -- kept deliberately
*off* the policy hash so tightening a limit never voids a pending approval (see
``engine.policy.ProfileSettings``).

For now Reaper is single-profile: there is one profile, and these helpers read and update
it. The model already supports many (per-library, per-media-type), and the multi-profile
UI is later work; nothing here forecloses it.

Why this exists as its own service: the reap executor's caps must come from the owner's
configured limits, not a hardcoded default. Before this, a plan was dry-run against
``ProfileSettings()`` -- the built-in cap of 10 items -- so a real 400-item condemned set
could never be walked even in simulation. The caps are a policy decision the owner makes,
and this is where that decision is stored and read.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from reaper.clock import utcnow
from reaper.db.models import Policy as PolicyModel
from reaper.db.models import Profile
from reaper.engine.policy import (
    DEFAULT_MOVIE_POLICY,
    DEFAULT_TV_POLICY,
    PolicyBody,
    ProfileSettings,
)

DEFAULT_PROFILE_NAME = "default"


async def active_profile_settings(session: AsyncSession) -> ProfileSettings:
    """The caps/grace/approval settings a run must obey.

    The single profile's settings, or the built-in defaults if no profile has been saved
    yet. Defaults are the *cautious* ones -- ten items a run, approval required -- so an
    install that has configured nothing is not thereby permitted to do more.
    """
    row = (
        await session.execute(select(Profile).order_by(Profile.id.asc()).limit(1))
    ).scalar_one_or_none()
    if row is None:
        return ProfileSettings()
    return ProfileSettings.model_validate_json(row.settings_json)


async def active_policy(session: AsyncSession, media_type: str = "movie") -> tuple[PolicyBody, str]:
    """The policy Reaper is currently working to, for one media type.

    Movies and TV are tuned separately -- keep-last-N seasons and the season-rank signal only
    make sense for TV, and a library often wants a gentler hand on one than the other -- so
    there are two policies, chosen here by ``media_type`` ("movie" or "tv").

    The most recently saved one for that type, or the built-in default if none has been saved.
    Policy rows are **immutable and append-only** -- editing writes a new row with a new hash
    rather than mutating the old one, because snapshots, approvals and audit entries point at
    that hash and must stay interpretable years later.
    """
    row = (
        await session.execute(
            select(PolicyModel)
            .where(PolicyModel.media_type == media_type)
            .order_by(PolicyModel.id.desc())
            .limit(1)
        )
    ).scalar_one_or_none()

    if row is None:
        return (DEFAULT_TV_POLICY if media_type == "tv" else DEFAULT_MOVIE_POLICY), "default"
    return PolicyBody.model_validate_json(row.body_json), row.name


async def active_policies(session: AsyncSession) -> tuple[PolicyBody, PolicyBody]:
    """The (movie, tv) policies in force, in that fixed order -- the pair a scan runs to."""
    movie, _ = await active_policy(session, "movie")
    tv, _ = await active_policy(session, "tv")
    return movie, tv


async def _ensure_active_policy_row(session: AsyncSession) -> int:
    """The id of a persisted policy row, creating one from the default if none exists.

    A profile references a policy by foreign key, but a fresh install has never saved
    one -- it runs on ``DEFAULT_MOVIE_POLICY``, which lives in code, not the table. So we
    persist it (append-only, content-addressed like any policy) and point the profile at
    it. Idempotent: the same default is never written twice, because the hash is unique.
    """
    latest = (
        await session.execute(select(PolicyModel).order_by(PolicyModel.id.desc()).limit(1))
    ).scalar_one_or_none()
    if latest is not None:
        return latest.id

    body: PolicyBody = DEFAULT_MOVIE_POLICY
    row = PolicyModel(
        policy_hash=body.policy_hash(),
        body_json=body.model_dump_json(),
        media_type=body.media_type,
        name=DEFAULT_PROFILE_NAME,
        created_at=utcnow(),
    )
    session.add(row)
    await session.flush()
    return row.id


async def save_profile_settings(
    session: AsyncSession, settings: ProfileSettings
) -> ProfileSettings:
    """Create or update the single profile's settings.

    Validation (a run cap above the rolling cap, a grace period under a week) is enforced
    by ``ProfileSettings`` itself, so it holds for every caller -- this service, the API,
    and the scheduler alike. Tightening a cap here is always safe: it cannot void a
    pending approval, because the caps are not part of the policy hash.

    The profile ships **disabled** and this does not enable it. Turning a profile on --
    letting it act -- is a separate, deliberate step, never a side effect of saving a cap.
    """
    profile = (
        await session.execute(select(Profile).order_by(Profile.id.asc()).limit(1))
    ).scalar_one_or_none()

    now = utcnow()
    if profile is None:
        profile = Profile(
            name=DEFAULT_PROFILE_NAME,
            enabled=False,
            active_policy_id=await _ensure_active_policy_row(session),
            settings_json=settings.model_dump_json(),
            created_at=now,
            updated_at=now,
        )
        session.add(profile)
    else:
        profile.settings_json = settings.model_dump_json()
        profile.updated_at = now

    await session.flush()
    return settings
