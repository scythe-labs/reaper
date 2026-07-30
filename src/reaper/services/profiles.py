# SPDX-License-Identifier: AGPL-3.0-or-later
"""The profile: how much Reaper may do, and how long it waits.

A profile is the container for the mutable, non-hashed part of a configuration -- the
four caps, whether those caps are enforced, the grace period, and the unknown-size
allowance -- kept deliberately *off* the policy hash so tightening a limit never voids a
pending approval (see ``engine.policy.ProfileSettings``).

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

import json
from dataclasses import dataclass

import structlog
from pydantic import ValidationError
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
    combine_hashes,
    rebalance,
    recover_rating_rules,
)

log = structlog.get_logger(__name__)

DEFAULT_PROFILE_NAME = "default"


@dataclass(frozen=True, slots=True)
class ActiveProfile:
    """The profile settings in force, and whether they are the ones the operator saved."""

    settings: ProfileSettings

    fell_back: bool = False
    """The stored settings blob was unreadable, so ``settings`` is the SHIPPED DEFAULT.

    Unlike an upgrade that only drops a departed key (the operator's real caps and grace
    survive), a full fall-back replaces every value -- and the defaults can be *looser* than
    what they saved: a grace of 30 becomes 14 (items become deletable 16 days early), a run
    cap of 5 becomes 10. So this is surfaced on the settings page and degrades the scan,
    exactly the way ``ActivePolicy.fell_back`` is, never a silent log line (rule 65)."""

    @property
    def repaired(self) -> bool:
        """What is in hand is not what is stored. The scan degrades on this, so a run never
        executes against limits nobody saved -- mirroring ``ActivePolicy.repaired``."""
        return self.fell_back


async def active_profile(session: AsyncSession) -> ActiveProfile:
    """The caps, grace, and unknown-size settings a run must obey, with a flag for whether
    the stored blob was recovered.

    The single profile's settings, or the built-in defaults if no profile has been saved
    yet. Defaults are the *cautious* ones -- ten items a run, caps on -- so an install
    that has configured nothing is not thereby permitted to do more.

    **This must not raise on a stored blob this model no longer accepts**, for the same
    reason ``active_policy`` does not: it is read by scans, execute, grace, the shelf, and
    the very settings page an operator would use to fix it, so a field removed after a row
    was written would otherwise take out all of them at once. Under ``extra="forbid"`` a
    departed key is a hard error, so unknown keys are dropped and the rest re-validated --
    an upgrade keeps the operator's caps and grace, losing only the gone field, and any new
    field falls back to its cautious default (``caps_enabled`` True); that salvage keeps the
    operator's real values, so it is NOT flagged. A blob unreadable for any *other* reason
    falls back to the built-in defaults and IS flagged (``fell_back``), because those
    defaults can be looser than what was saved.
    """
    row = (
        await session.execute(select(Profile).order_by(Profile.id.asc()).limit(1))
    ).scalar_one_or_none()
    if row is None:
        return ActiveProfile(ProfileSettings())
    try:
        return ActiveProfile(ProfileSettings.model_validate_json(row.settings_json))
    except ValidationError as exc:
        try:
            raw = json.loads(row.settings_json)
        except ValueError:
            raw = None
        if isinstance(raw, dict):
            known = {k: v for k, v in raw.items() if k in ProfileSettings.model_fields}
            try:
                settings = ProfileSettings.model_validate(known)
            except ValidationError:
                pass
            else:
                dropped = sorted(set(raw) - set(known))
                log.info("profile.settings_migrated", dropped=dropped)
                # The operator's real values survived; only a departed key was dropped. Benign.
                return ActiveProfile(settings)
        # The stored policy could not be read and Reaper is falling back to DEFAULT caps and
        # grace, which changes deletion behavior. Carry the validation error so which field
        # broke is answerable. A policy holds no secrets (caps, grace, thresholds), so the
        # detail is safe to log.
        log.warning("profile.settings_unreadable", error=str(exc))
        return ActiveProfile(ProfileSettings(), fell_back=True)


async def active_profile_settings(session: AsyncSession) -> ProfileSettings:
    """The caps, grace, and unknown-size settings a run must obey (the settings alone).

    Every reader that just needs the numbers calls this. The scan and the settings GET call
    :func:`active_profile` instead, to also learn whether the stored blob had to be
    recovered (which they surface and degrade on -- rule 65)."""
    return (await active_profile(session)).settings


async def active_policy_row(session: AsyncSession, media_type: str) -> PolicyModel | None:
    """The newest saved policy row for one media type -- the row in force.

    "Active" is purely recency: rows are append-only, so whatever was saved last for the
    media type is what the next scan runs to. Every reader and writer of that rule goes
    through here, so it cannot drift -- ``save_policy`` in particular must judge "did this
    save change anything?" against exactly the row a GET would return.
    """
    return (
        await session.execute(
            select(PolicyModel)
            .where(PolicyModel.media_type == media_type)
            .order_by(PolicyModel.id.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


@dataclass(frozen=True, slots=True)
class ActivePolicy:
    body: PolicyBody
    name: str

    rescaled: bool = False
    """This is the operator's own body, rescaled to load (``policy.rebalance``).

    Their tuning survived; only the units moved. The rescale is not exactly
    score-preserving -- integer rounding can move a score a point or two, enough to cross
    a condemn line (see ``policy.rebalance``) -- which is why this flag makes ``repaired``
    true and the editor opens on it as an unsaved draft for them to review and re-save.
    """

    fell_back: bool = False
    """The stored body could not be repaired, so this is the SHIPPED DEFAULT.

    Their tuning did not survive. Louder than ``rescaled`` in the UI, because the numbers
    on screen are ones they never chose.
    """

    rating_rules_recovered: bool = False
    """This is the operator's own body with their rating bar restored
    (``policy.recover_rating_rules``).

    The bar moved off the RATING_FLOOR gate row with no backfill, so a body written before
    that move still validates while protecting nothing. Restoring it changes what the scan
    decides -- titles their bar keeps stop being condemnable -- so it is never adopted
    silently: this flag makes ``repaired`` true, the scan degrades, and the editor opens on
    it as an unsaved draft the operator reviews and saves.
    """

    @property
    def repaired(self) -> bool:
        """Any recovery: what is in hand is not what is stored.

        The scan reads this one. Every case means the run would execute against a policy
        nobody saved, which is the substitution the journal exists to prevent -- so all of
        them degrade the snapshot, however the body was arrived at. Do NOT infer this from
        the name: an operator's own policy is frequently *called* "default", so the name
        cannot tell the recoveries apart (it silently did not, once).
        """
        return self.rescaled or self.fell_back or self.rating_rules_recovered


async def active_policy(session: AsyncSession, media_type: str = "movie") -> ActivePolicy:
    """The policy Reaper is currently working to, for one media type.

    Movies and TV are tuned separately -- keep-last-N seasons and the season-rank signal only
    make sense for TV, and a library often wants a gentler hand on one than the other -- so
    there are two policies, chosen here by ``media_type`` ("movie" or "tv").

    The most recently saved one for that type, or the built-in default if none has been saved.
    Policy rows are **immutable and append-only** -- editing writes a new row rather than
    mutating the old one, because snapshots, approvals and audit entries point at these
    rows by hash and must stay interpretable years later.

    **This must not raise on a stored body that no longer validates.** It is read from the
    editor, the simulator and the scan, so a validator added after a row was written would
    otherwise take out all three at once, including the page that fixes it. That holds for
    a body that is not JSON at all and one that decodes to something other than an object:
    the decode below is guarded, and ``policy.rebalance`` returns ``None`` rather than
    raising on any shape it cannot read. A body whose removal weights predate the
    100-point budget is rescaled and flagged ``repaired``; anything else unreadable falls
    back to the shipped default, also flagged.

    One recovery runs on a body that validates *perfectly well*: a rating bar written
    before the bar moved off the gate row is restored by ``policy.recover_rating_rules``,
    because that body loads cleanly while protecting nothing. It is checked first, on the
    raw dict, since validation cannot see what is missing.
    """
    row = await active_policy_row(session, media_type)
    default = DEFAULT_TV_POLICY if media_type == "tv" else DEFAULT_MOVIE_POLICY

    if row is None:
        return ActivePolicy(default, "default")

    try:
        raw = json.loads(row.body_json)
    except ValueError:
        # Not JSON at all. `model_validate_json` below reports it the same way, and the
        # two recoveries that read `raw` both refuse a non-dict.
        raw = None

    restored = recover_rating_rules(raw)
    if restored is not None:
        try:
            body = PolicyBody.model_validate(restored)
        except ValidationError:
            # The stored body was legacy-shaped but does not load even with the bar put
            # back (hand-edited, or broken some other way). Fall through and read it as
            # stored; the editor's "this protection has no rating sources" warning covers
            # the bar, and any other fault takes the paths below.
            log.warning("policy.rating_rules_unrecoverable", media_type=media_type)
        else:
            log.info("policy.rating_rules_recovered", media_type=media_type, name=row.name)
            return ActivePolicy(body, row.name, rating_rules_recovered=True)

    try:
        return ActivePolicy(PolicyBody.model_validate_json(row.body_json), row.name)
    except ValidationError:
        # Pydantic raises ValidationError for malformed JSON too, so we land here with a
        # body that json.loads could not read either. Both that and a body that decodes to
        # something other than an object (a list, a number, null) fall through to the
        # shipped default below; neither may escape as an exception.
        # Compose the two shims, never race them. A body that needs its rating bar put back
        # AND needs rescaling arrives here with `restored` already holding the recovered
        # bar, because the validate above failed on the weights, not on the bar. Rebalancing
        # `raw` would silently drop that recovery: the operator would be told their weights
        # were rescaled and nothing about the protection that vanished, and saving the draft
        # the editor opens would write the loss back permanently (rules 105 and 65).
        source = restored if isinstance(restored, dict) else raw
        repaired = rebalance(source) if isinstance(source, dict) else None
        if repaired is not None:
            recovered = source is restored
            log.info(
                "policy.rebalanced",
                media_type=media_type,
                name=row.name,
                rating_rules_recovered=recovered,
            )
            return ActivePolicy(
                PolicyBody.model_validate(repaired),
                row.name,
                rescaled=True,
                rating_rules_recovered=recovered,
            )
        log.warning("policy.unreadable", media_type=media_type, name=row.name)
        return ActivePolicy(default, "default", fell_back=True)


async def active_policies(session: AsyncSession) -> tuple[ActivePolicy, ActivePolicy]:
    """The (movie, tv) policies in force, in that fixed order -- the pair a scan runs to."""
    return (await active_policy(session, "movie"), await active_policy(session, "tv"))


async def live_policy_hash(session: AsyncSession) -> str:
    """The fingerprint of the policy pair in force right now.

    The same combination a scan stamps onto its snapshot (``policy.combine_hashes``, movie
    then TV), computed here so the scan and the executor cannot drift into two spellings of
    "the current policy". The executor compares a run's stored hash against this before it
    deletes anything: an operator who added a protection after approving a plan changed no
    candidate row, so the manifest still matches while the plan now targets files the new
    policy keeps.

    A policy Reaper had to recover reads as whatever ``active_policy`` handed back -- the
    shipped default when the stored body was unreadable -- so a repaired policy produces a
    hash that will not match a snapshot scored under the real one. That refuses the run,
    which is the direction rule 65 wants.
    """
    movie, tv = await active_policies(session)
    return combine_hashes(movie.body.policy_hash(), tv.body.policy_hash())


async def _ensure_active_policy_row(session: AsyncSession) -> int:
    """The id of a persisted policy row, creating one from the default if none exists.

    A profile references a policy by foreign key, but a fresh install has never saved
    one -- it runs on ``DEFAULT_MOVIE_POLICY``, which lives in code, not the table. So we
    persist it (append-only, content-addressed like any policy) and point the profile at
    it. Idempotent: the ``latest`` check writes only when the table has no rows at all.
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

    The row this creates carries ``Profile.enabled = False`` and this function never flips
    it -- but do not read that as the interlock. Nothing in ``src/`` reads the column: it is
    written at creation and never consulted, so there is no on-step and no consumer, and it
    gates nothing. What actually keeps a saved cap from acting is that the scheduler never
    deletes and the one route that does (``api.runs.execute_run``) needs the host armed and
    the typed content-bound phrase.
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
