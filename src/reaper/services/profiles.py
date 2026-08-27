# SPDX-License-Identifier: AGPL-3.0-or-later
"""The profile: how much Reaper may do, and how long it waits.

A profile holds the settings that change often and never affect deletion decisions: the
four caps, whether those caps are enforced, the grace period, and the unknown-size
allowance. These stay out of the policy hash, so tightening a limit never voids an
approval already given (see ``engine.policy.ProfileSettings``).

Reaper currently has one profile, and these helpers read and update it. The data model
already supports several (per-library, per-media-type); the multi-profile UI is later
work, and nothing here blocks it.

This is its own service because the reap executor must use the caps the owner
configured, never a hardcoded default. The caps are the owner's own choice; this is
where that choice is stored and read.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

import structlog
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from reaper.clock import utcnow
from reaper.db.models import ListConfig as ListConfigModel
from reaper.db.models import Policy as PolicyModel
from reaper.db.models import Profile
from reaper.engine.fields import Op
from reaper.engine.policy import (
    DEFAULT_MOVIE_POLICY,
    DEFAULT_TV_POLICY,
    ConditionSpec,
    PolicyBody,
    ProfileSettings,
    combine_hashes,
)
from reaper.engine.policy_migrations import (
    BOTH_MEDIA_TYPES,
    PolicyRepair,
    conversion_list_names,
    convert_list_protections,
    has_legacy_list_protections,
    legacy_keep_tags,
    library_media_types,
    own_list_media_scope,
    rebalance,
    recover_rating_rules,
)
from reaper.services import app_settings
from reaper.text import fold

log = structlog.get_logger(__name__)

DEFAULT_PROFILE_NAME = "default"


@dataclass(frozen=True, slots=True)
class ActiveProfile:
    """The profile settings in force now, and whether they match what the operator saved."""

    settings: ProfileSettings

    fell_back: bool = False
    """True when the stored settings could not be read, so ``settings`` holds the shipped
    defaults instead of what the operator saved.

    This differs from a smaller repair that only drops one removed field and keeps the rest
    of the operator's values. Here every value is replaced, and the defaults can be less
    strict than what was saved, for example letting items become deletable sooner. The
    settings page shows this, and the scan is marked untrusted, the same way
    ``ActivePolicy.fell_back`` works. It is never left as just a log line."""

    @property
    def repaired(self) -> bool:
        """True when the loaded settings are not what the operator stored. The scan is
        marked untrusted, so a run never acts on limits nobody saved. Mirrors
        ``ActivePolicy.repaired``."""
        return self.fell_back


async def active_profile(session: AsyncSession) -> ActiveProfile:
    """The caps, grace period, and unknown-size settings the current run must obey, plus a
    flag for whether the stored values had to be recovered.

    Returns the one profile's settings, or the built-in defaults if nothing has been saved
    yet. The defaults are cautious, so an install that has configured nothing is not
    thereby allowed to do more.

    This function must never raise on a stored value this version no longer accepts.
    Scans, execute, grace, the shelf, and the settings page that would fix a bad value all
    read through here, so one bad field would otherwise break all of them at once. If a
    field was removed since the row was saved, that field alone is dropped and the rest of
    the operator's caps and grace load normally; a new field falls back to its cautious
    default. That case is not flagged, because the operator's real values still loaded. Any
    other unreadable value falls back to the built-in defaults instead, and that case IS
    flagged, because those defaults can be less strict than what was saved.
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
    """The caps, grace, and unknown-size settings a run must obey, without the recovery flag.

    Callers that only need the numbers use this. The scan and the settings GET call
    :func:`active_profile` instead, because they also need to know whether the stored
    values had to be recovered, so they can show that and mark the scan untrusted."""
    return (await active_profile(session)).settings


async def active_policy_row(session: AsyncSession, media_type: str) -> PolicyModel | None:
    """The newest saved policy row for one media type: the row currently in force.

    "Active" means most recent. Rows are never edited in place, only added, so whatever was
    saved last is what the next scan uses. Every reader and writer goes through this
    function, so they cannot disagree; ``save_policy`` in particular checks "did this save
    change anything?" against exactly the row a GET would return.
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

    repairs: tuple[PolicyRepair, ...] = ()
    """Every change this policy body needed before it could load, in the order applied.

    A single list rather than separate flags, because one repair can affect several parts
    of the app at once and a flag is easy to miss updating everywhere. ``PolicyRepair``
    documents what each entry means.

    A body can need more than one repair, such as both its rating bar restored and its
    weights rescaled, and this holds all of them in the order ``active_policy`` applied
    them. The operator's notices then read in that same order.
    """

    lists_unreadable: bool = False
    """True when the list registry could not be read while building this policy, so any
    keep rule from the operator's own Plex lists is missing from it.

    This is not a ``PolicyRepair``, because every entry in that list is fixed by the
    operator opening the policy page and saving, and saving does not fix this. It still
    counts as ``repaired``: the scan is marked untrusted, since a failed read is not the
    same as nothing configured, and ``services.list_rules`` refuses to save a policy row,
    since saving would make the missing rules permanent. Only the message ``scan_runner``
    shows tells the two cases apart.
    """

    @property
    def repaired(self) -> bool:
        """True on any recovery: the loaded policy is not exactly what was stored.

        The scan checks this. Every recovery case means a run would otherwise act on a
        policy nobody actually saved, so all of them mark the scan untrusted, whatever the
        exact repair was. Judge this flag, never the policy's name: an operator's own
        policy is often named "default" too, so the name cannot tell a real save from a
        recovered one.
        """
        return bool(self.repairs) or self.lists_unreadable


async def active_policy(session: AsyncSession, media_type: str = "movie") -> ActivePolicy:
    """The policy Reaper follows right now, for one media type.

    Movies and TV are tuned separately, since features like keeping the last N seasons only
    apply to TV, and an operator may want a gentler policy for one than the other.
    ``media_type`` picks which ("movie" or "tv").

    Returns the most recently saved policy for that type, or the built-in default if none
    has been saved. Policy rows are never edited in place: saving a change always writes a
    new row, because snapshots, approvals, and audit entries refer back to a specific row by
    its hash and must still make sense years later.

    This function must never raise on a stored policy body that no longer validates. The
    editor, the simulator, and the scan all read through here, so a validation rule added
    after a row was written would otherwise break all three, including the editor page that
    would fix it. That covers a body that is not valid JSON and one that decodes to
    something other than an object. A body whose removal weights were written before scores
    summed to 100 is rescaled and marked ``repaired``; anything else unreadable falls back
    to the shipped default and is also marked ``repaired``.

    One recovery runs even on a body that otherwise loads cleanly: a rating rule written
    before rating conditions moved off the gate row is restored by
    ``policy_migrations.recover_rating_rules``, because such a body loads fine while
    protecting nothing. This check runs first, on the raw stored data, since validation
    alone cannot see what is missing.
    """
    row = await active_policy_row(session, media_type)
    default = DEFAULT_TV_POLICY if media_type == "tv" else DEFAULT_MOVIE_POLICY

    if row is None:
        body, lists_unreadable = await _default_with_own_lists(session, default)
        return ActivePolicy(body, "default", lists_unreadable=lists_unreadable)

    try:
        raw = json.loads(row.body_json)
    except ValueError:
        # Not valid JSON. `model_validate` below reports it the same way, and the recovery
        # steps that read `raw` all refuse anything that is not a dict.
        raw = None

    # List-protection conversion runs FIRST, on the raw dict. It removes keys the current
    # model rejects (`keep_tags`), so every later step must see its output, or an
    # older-but-valid body would look unreadable and silently fall back to the shipped
    # default. Every repair below is applied in order and combined rather than picking one:
    # a body needing two repairs reports both, and the editor tells the operator about both.
    repairs: tuple[PolicyRepair, ...] = ()
    if has_legacy_list_protections(raw):
        tag_name, imdb_name, own_names, scope = await _conversion_list_names(
            session, keep_tags=legacy_keep_tags(raw)
        )
        converted = convert_list_protections(
            raw,
            media_type=media_type,
            tag_list_name=tag_name,
            imdb_list_name=imdb_name,
            collection_list_names=own_names,
            collection_media_scope=scope,
        )
        if converted is not None:
            raw = converted
            repairs = (*repairs, PolicyRepair.LISTS_MIGRATED)
            log.info("policy.lists_migrated", media_type=media_type, name=row.name)

    restored = recover_rating_rules(raw)
    if restored is not None:
        try:
            body = PolicyBody.model_validate(restored)
        except ValidationError:
            # This body has the old rating-bar shape but still does not load once the bar
            # is restored (hand-edited, or broken some other way). Fall through and try it
            # as stored; the editor's own warning covers a protection with no rating
            # sources, and any other problem is handled below.
            log.warning("policy.rating_rules_unrecoverable", media_type=media_type)
        else:
            log.info("policy.rating_rules_recovered", media_type=media_type, name=row.name)
            return ActivePolicy(body, row.name, (*repairs, PolicyRepair.RATING_RULES_RESTORED))

    try:
        return ActivePolicy(PolicyBody.model_validate(raw), row.name, repairs)
    except ValidationError:
        # A body that was not JSON, decoded to something other than an object, or failed
        # validation all land here; none of them may raise past this function.
        # Combine the two recoveries rather than picking one. A body that needs both its
        # rating bar restored and its weights rescaled arrives here with `restored` already
        # holding the recovered bar, since validation failed on the weights, not the bar.
        # Rescaling `raw` instead would silently drop that recovery: the operator would be
        # told their weights were rescaled with no mention of the vanished protection, and
        # saving the editor's draft would make the loss permanent.
        source = restored if isinstance(restored, dict) else raw
        rescaled = rebalance(source) if isinstance(source, dict) else None
        if rescaled is not None:
            recovered = source is restored
            log.info(
                "policy.rebalanced",
                media_type=media_type,
                name=row.name,
                rating_rules_recovered=recovered,
            )
            if recovered:
                repairs = (*repairs, PolicyRepair.RATING_RULES_RESTORED)
            return ActivePolicy(
                PolicyBody.model_validate(rescaled),
                row.name,
                (*repairs, PolicyRepair.RESCALED),
            )
        log.warning("policy.unreadable", media_type=media_type, name=row.name)
        # Nothing of the stored body survives, so the repairs attempted along the way are
        # not reported. The operator is looking at the shipped default, and a notice saying
        # their lists were converted would describe a body no longer on screen.
        return ActivePolicy(default, "default", (PolicyRepair.FELL_BACK,))


async def _conversion_list_names(
    session: AsyncSession,
    *,
    keep_tags: tuple[str, ...],
) -> tuple[str | None, str | None, tuple[str, ...], dict[str, frozenset[str]]]:
    """The registry rows ``convert_list_protections`` points its rules at, plus which media
    type each of the operator's own Plex lists may protect. Both come from
    ``policy_migrations.conversion_list_names`` and ``policy_migrations.own_list_media_scope``,
    so the load path here and the upgrade migration always agree.

    ``plex_libraries`` is read on a best-effort basis. Losing it only widens a collection's
    rule to cover both media types instead of narrowing it to one, which is the keep
    direction, so a failed read here does not mark the scan untrusted. The registry read
    below can still raise, and the caller marks the scan untrusted when it does.

    A malformed stored value is also caught here, not only a database error: a corrupt or
    non-list setting (from a restored backup, or a hand-edit) makes ``get_plex_libraries``
    raise ``ValueError`` or ``TypeError``, and ``active_policy`` must never raise. On either
    error this falls back to no scoping, the same way the sibling migration
    ``_library_media_types`` does for the same value."""
    try:
        libraries = await app_settings.get_plex_libraries(session)
    except (SQLAlchemyError, ValueError, TypeError):
        log.warning("policy.plex_libraries_unreadable")
        libraries = []
    rows = [
        (str(r.source), str(r.name), r.config_json)
        for r in (
            await session.execute(
                select(
                    ListConfigModel.source,
                    ListConfigModel.name,
                    ListConfigModel.config_json,
                ).order_by(ListConfigModel.id)
            )
        ).all()
    ]
    tag, imdb, own = conversion_list_names(rows, keep_tags=keep_tags)
    scope = own_list_media_scope(rows, library_media_types(libraries))
    return tag, imdb, own, scope


async def _default_with_own_lists(
    session: AsyncSession, default: PolicyBody
) -> tuple[PolicyBody, bool]:
    """The shipped default policy plus a keep rule for each Plex list the registry holds, and
    whether the registry could be read at all.

    The shipped conditions name the lists ``list_config.DEFAULT_LISTS`` seeds, scoped to the
    media type each applies to. A Plex keep collection is added later by migration, so an
    install that has never saved a policy has no rule pointing at it yet: this function adds
    that rule directly, before the row above would otherwise return with nothing protecting
    the collection.

    This only adds conditions, never removes any. An unreadable registry, or one with
    nothing of its own, leaves the shipped conditions exactly as they were.

    A registry read that succeeds and finds nothing may fall back to the shipped conditions
    without comment. A read that fails may not: it marks the scan untrusted instead, because
    a scan running with no rule protecting the operator's own keep collection must not look
    like an ordinary clean scan.
    """
    try:
        # No tags to resolve: there is no stored policy body here, so nothing was ever
        # protecting by tag. Only the Plex half is read, by name.
        _, _, own, scope = await _conversion_list_names(session, keep_tags=())
    except SQLAlchemyError:
        log.warning("policy.default_lists_unreadable")
        return default, True
    carried = {
        fold(str(c.value))
        for c in default.protect_conditions
        if c.field == "on_list" and isinstance(c.value, str)
    }
    # Case-folded on both sides, like every comparison of a list name. A single-library
    # collection's rule is added only to the policy for that library's media type; an
    # unsynced or ambiguous library, and every watchlist, keep the rule on both media types.
    extra = tuple(
        ConditionSpec(field="on_list", op=Op.EQ, value=name)
        for name in dict.fromkeys(own)
        if fold(name) not in carried
        and default.media_type in scope.get(fold(name), BOTH_MEDIA_TYPES)
    )
    if not extra:
        return default, False
    return (
        default.model_copy(update={"protect_conditions": default.protect_conditions + extra}),
        False,
    )


async def active_policies(session: AsyncSession) -> tuple[ActivePolicy, ActivePolicy]:
    """The (movie, tv) policies in force, in that order. The pair a scan uses."""
    return (await active_policy(session, "movie"), await active_policy(session, "tv"))


async def live_policy_hash(session: AsyncSession) -> str:
    """The fingerprint of the policy pair in force right now.

    Computed the same way a scan stamps it onto its snapshot (movie then TV), so the scan and
    the executor always agree on what "the current policy" means. The executor compares a
    run's stored hash against this before it deletes anything: an operator who added a
    protection after approving a plan changed no candidate row, so the plan's manifest still
    matches even though the new policy would now keep some of those files.

    A policy Reaper had to recover reads as whatever ``active_policy`` returns, which is the
    shipped default when the stored body was unreadable. So a recovered policy produces a
    hash that will not match a snapshot scored under the real one, and the run is refused,
    which is the safe outcome.
    """
    movie, tv = await active_policies(session)
    return combine_hashes(movie.body.policy_hash(), tv.body.policy_hash())


async def save_profile_settings(
    session: AsyncSession, settings: ProfileSettings
) -> ProfileSettings:
    """Create or update the single profile's settings.

    ``ProfileSettings`` itself enforces validation, such as a run cap above the rolling cap
    or a grace period under a week, so every caller gets the same rules. Tightening a cap
    here is always safe: it cannot void a pending approval, since caps are not part of the
    policy hash.

    Saving a profile never arms deletion by itself. The scheduler never deletes, and the
    one route that does (``api.runs.execute_run``) still needs the host armed and the
    matching confirmation phrase.

    This never writes a policy row. An unsaved install computes a wider default policy in
    ``active_policy``, including the operator's own Plex keep collection; writing a plain
    default policy row here would silently replace that wider computation the moment the
    operator saved any profile setting. Leaving the policy table empty keeps that
    computation active until the operator saves a policy of their own.
    """
    profile = (
        await session.execute(select(Profile).order_by(Profile.id.asc()).limit(1))
    ).scalar_one_or_none()

    now = utcnow()
    if profile is None:
        profile = Profile(
            name=DEFAULT_PROFILE_NAME,
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
