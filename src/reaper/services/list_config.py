# SPDX-License-Identifier: AGPL-3.0-or-later
"""The operator's own protection lists: naming them, pointing them somewhere, removing them.

Every list used to be derived. Reaper worked out which existed from the keep tags on the
policy, a Plex collection name it had hardcoded to ``"Never Reap"`` in a library hardcoded to
``"Movies"``, and the one curated list it ships with. An operator whose library is called
anything else got nothing, and had no screen on which to say so (#483).

This module owns the *definitions*. ``services.lists`` still owns fetching and membership,
and still writes both to ``cache.db``; nothing here touches that. The split is deliberate and
is the reason for two tables: membership is a rebuildable mirror of somebody else's data, and
a list the operator named is not rebuildable from anything.

**Everything here is fail-closed toward keeping.** Removing a list withdraws a protection, so
it is refused for a built-in and refused while a policy rule still names it -- an operator who
deletes the list a keep rule points at has silently unprotected everything that rule covered,
and the rule would go on reading as a live protection.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Literal

import structlog
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from reaper.clock import utcnow
from reaper.db.models import ListConfig
from reaper.services.lists import ListSource

log = structlog.get_logger(__name__)


class ListConfigError(ValueError):
    """A refusal the operator should read, in their words. The API maps it to a 4xx."""


@dataclass(frozen=True, slots=True)
class ListDefinition:
    """One definition, decoded, for the code that builds providers from it.

    A plain value rather than the ORM row, because the sync runs against ``cache.db`` and has
    no session on the settings database. Passing the row itself would either hold that session
    open across every network read the sync makes, or hand the sync a detached instance whose
    attribute access raises.
    """

    id: int
    name: str
    source: ListSource
    config: dict[str, Any]
    enabled: bool

    @property
    def tags(self) -> tuple[str, ...]:
        """The *arr tag spellings, for an ``ARR_TAG`` definition. Empty for any other source."""
        raw = self.config.get("tags")
        return tuple(str(t) for t in raw) if isinstance(raw, list) else ()

    @property
    def match(self) -> Literal["any", "all"]:
        """Whether a title needs all the tags or any of them. Anything unrecognized reads as
        ANY, matching ``_clean_config``: the two spellings of this default must agree, and ANY
        is the wider list, which is the keep direction."""
        return "all" if self.config.get("match") == "all" else "any"


async def definitions(session: AsyncSession) -> list[ListDefinition]:
    """Every definition, decoded, shipped ones included.

    Disabled rows are returned too, and deliberately: the sync builds providers only from the
    enabled ones, and the retire sweep then disables every stored list this configuration no
    longer produces. A disabled definition that were simply omitted here would be
    indistinguishable from one that never existed -- and its stored membership would go on
    protecting, so the switch on the settings screen would be a switch that does nothing.
    """
    out: list[ListDefinition] = []
    for row in await all_lists(session):
        try:
            body = json.loads(row.config_json or "{}")
        except ValueError:
            # Unreadable is not empty. A body that will not parse cannot say which library or
            # which tags, so no provider is built and the stored membership is left alone
            # rather than replaced by a sync of a guessed configuration (rule 93).
            log.warning("list_config.unreadable", list_id=row.id)
            continue
        out.append(
            ListDefinition(
                id=row.id,
                name=row.name,
                source=ListSource(row.source),
                config=body if isinstance(body, dict) else {},
                enabled=row.enabled,
            )
        )
    return out


#: What Reaper ships with, created once on the first read so the screen is never empty on a
#: fresh install and the operator can see what a list looks like before making one. Built-in,
#: so it can be pointed elsewhere or switched off but never deleted: the default policy
#: carries a keep rule naming it, and a rule naming a list that does not exist is a protection
#: that reads as live and covers nothing (rule 25).
BUILT_INS: tuple[tuple[str, ListSource, dict[str, Any]], ...] = (
    ("IMDb Top 250", ListSource.CURATED, {"list": "imdb-top-250"}),
)


async def ensure_built_ins(session: AsyncSession) -> None:
    """Create the shipped lists if they are not there. Idempotent, and never overwrites.

    Matched on ``built_in`` rather than on the name, so an operator who RENAMED the shipped
    list keeps their name instead of getting a second row beside it every time this runs.
    """
    existing = set(
        (await session.execute(select(ListConfig.source).where(ListConfig.built_in))).scalars()
    )
    for name, source, config in BUILT_INS:
        if source.value in existing:
            continue
        session.add(
            ListConfig(
                name=name,
                source=source.value,
                config_json=json.dumps(config),
                enabled=True,
                built_in=True,
                created_at=utcnow(),
            )
        )
    await session.commit()


async def all_lists(session: AsyncSession) -> Sequence[ListConfig]:
    """Every configured list, shipped ones included, oldest first so the order holds still."""
    await ensure_built_ins(session)
    rows = await session.execute(select(ListConfig).order_by(ListConfig.id))
    return list(rows.scalars())


async def get(session: AsyncSession, list_id: int) -> ListConfig:
    row = await session.get(ListConfig, list_id)
    if row is None:
        raise ListConfigError("That list no longer exists. Reload the page.")
    return row


def _clean_name(name: str) -> str:
    cleaned = name.strip()
    if not cleaned:
        raise ListConfigError("Give the list a name, so you can pick it out on the Policy screen.")
    if len(cleaned) > 100:
        raise ListConfigError("That name is too long. Keep it under 100 characters.")
    return cleaned


def _clean_config(source: ListSource, config: dict[str, Any]) -> str:
    """Refuse a configuration that could never match anything, at the save boundary.

    A list saved with no collection name or no tags syncs to empty and then sits on the
    screen reading "Nothing on it", which is indistinguishable from a collection the operator
    has not filled in yet. Refusing here says which box is empty, while they are looking at
    it (rule 108 is the same check for a rule value that strips to nothing).
    """
    if source is ListSource.PLEX_COLLECTION:
        library = str(config.get("library", "")).strip()
        collection = str(config.get("collection", "")).strip()
        if not library:
            raise ListConfigError("Say which Plex library to look in.")
        if not collection:
            raise ListConfigError("Say which collection in that library to read.")
        return json.dumps({"library": library, "collection": collection})
    if source is ListSource.ARR_TAG:
        tags = [str(t).strip() for t in config.get("tags", []) if str(t).strip()]
        if not tags:
            raise ListConfigError(
                "Add at least one tag, spelled as it appears in Sonarr or Radarr."
            )
        match = "all" if config.get("match") == "all" else "any"
        return json.dumps({"tags": tags, "match": match})
    # A curated list carries only which shipped list it is, and that is not the operator's to
    # retype. Kept as it was rather than rebuilt from the request.
    return json.dumps({"list": str(config.get("list", "imdb-top-250"))})


async def create(
    session: AsyncSession, *, name: str, source: str, config: dict[str, Any]
) -> ListConfig:
    try:
        kind = ListSource(source)
    except ValueError:
        raise ListConfigError("Pick where the list comes from.") from None
    if kind is ListSource.CURATED:
        raise ListConfigError("The lists Reaper ships with cannot be added by hand.")
    row = ListConfig(
        name=_clean_name(name),
        source=kind.value,
        config_json=_clean_config(kind, config),
        enabled=True,
        built_in=False,
        created_at=utcnow(),
    )
    session.add(row)
    try:
        await session.commit()
    except IntegrityError:
        # The unique name. Caught rather than pre-checked, because a read-then-insert can be
        # beaten between the two and the constraint is the thing that actually holds.
        await session.rollback()
        raise ListConfigError("You already have a list with that name. Pick another.") from None
    log.info("list_config.created", source=kind.value)
    return row


async def update(
    session: AsyncSession,
    list_id: int,
    *,
    name: str | None = None,
    config: dict[str, Any] | None = None,
    enabled: bool | None = None,
) -> ListConfig:
    row = await get(session, list_id)
    if name is not None:
        row.name = _clean_name(name)
    if config is not None:
        row.config_json = _clean_config(ListSource(row.source), config)
    if enabled is not None:
        row.enabled = enabled
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        raise ListConfigError("You already have a list with that name. Pick another.") from None
    log.info("list_config.updated", list_id=list_id, enabled=row.enabled)
    return row


async def delete(session: AsyncSession, list_id: int) -> None:
    """Remove a list. Refused for one Reaper ships with, because a shipped keep rule names it.

    **A second refusal belongs here and is not written yet.** Deleting a list a policy rule
    points at withdraws a protection while the rule goes on rendering as a live one, so this
    should also refuse while any rule names it. No rule can name a list until the ``on_list``
    field exists, and a guard that cannot fire reads as protection that is not there
    (rule 38/117), so it lands in the change that adds the field rather than sitting here
    inert.
    """
    row = await get(session, list_id)
    if row.built_in:
        raise ListConfigError(
            "This list ships with Reaper, so it cannot be deleted. You can switch it off instead."
        )
    await session.delete(row)
    await session.commit()
    log.info("list_config.deleted", list_id=list_id)
