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
the API pairs it with ``list_rules.detach_list``: the keep rules naming the list leave the
policies in the same request, and none can go on reading as a live protection covering
nothing.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Literal

import structlog
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from reaper.clock import utcnow
from reaper.db.models import ListConfig
from reaper.engine.policy import DEFAULT_IMDB_LIST_NAME, DEFAULT_TAG_LIST_NAME
from reaper.services import app_settings
from reaper.services import lists as lists_service
from reaper.services.lists import ListSource

log = structlog.get_logger(__name__)


class ListConfigError(ValueError):
    """A refusal the operator should read, in their words. The API maps it to a 4xx."""


class ListRegistryUnreadableError(RuntimeError):
    """A stored definition's body will not parse, so the registry cannot say which lists
    exist. Raised only for a caller that is about to SYNC (``strict=True``), because that
    pass retires every list the registry no longer produces: a row read as absent is a
    protection switched off by a decode error. Rules 65/91 -- a read failure is "unknown",
    never "nothing configured"."""


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
        """The *arr tag spellings, for an ``ARR_TAG`` definition. Empty for any other source.

        Deduplicated on the comparison form here as well as at the save boundary, so a body
        stored before that check existed reads as one tag per tag rather than reporting a
        count of zero against the spelling it lost (#509). Read-side too, because the
        alternative is asking the operator to re-save a list to correct a number.
        """
        raw = self.config.get("tags")
        if not isinstance(raw, list):
            return ()
        seen: set[str] = set()
        out: list[str] = []
        for entry in raw:
            tag = str(entry)
            key = tag.strip().casefold()
            if key in seen:
                continue
            seen.add(key)
            out.append(tag)
        return tuple(out)

    @property
    def match(self) -> Literal["any", "all"]:
        """Whether a title needs all the tags or any of them. Anything unrecognized reads as
        ANY, matching ``_clean_config``: the two spellings of this default must agree, and ANY
        is the wider list, which is the keep direction."""
        return "all" if self.config.get("match") == "all" else "any"

    @property
    def imdb_variant(self) -> str:
        """The mirror path variant for an ``IMDB`` definition: a preset key or a public
        list id. Anything unrecognized reads as the Top 250, the list Reaper has always
        shipped, rather than as a request the mirror will 404."""
        preset = str(self.config.get("preset", "")).strip()
        if preset in lists_service.IMDB_PRESETS:
            return preset
        list_id = str(self.config.get("list_id", "")).strip()
        return list_id if list_id else "top250"


async def definitions(session: AsyncSession, *, strict: bool = False) -> list[ListDefinition]:
    """Every definition, decoded, shipped ones included.

    Disabled rows are returned too, and deliberately: the sync builds providers only from the
    enabled ones, and the retire sweep then disables every stored list this configuration no
    longer produces. A disabled definition that were simply omitted here would be
    indistinguishable from one that never existed -- and its stored membership would go on
    protecting, so the switch on the settings screen would be a switch that does nothing.

    ``strict`` is what a caller about to SYNC passes: an unreadable body then raises
    :class:`ListRegistryUnreadableError` instead of dropping that row, because dropping it is
    indistinguishable from a delete and the sweep disables its membership. The screens that
    only DISPLAY the registry stay tolerant, so a single bad row still renders beside the
    others rather than 500ing the page an operator would fix it from.
    """
    out: list[ListDefinition] = []
    for row in await all_lists(session):
        try:
            body = json.loads(row.config_json or "{}")
        except ValueError:
            # Unreadable is not empty. A body that will not parse cannot say which library or
            # which tags, so nothing may be synced from a guessed configuration (rule 93) --
            # and dropping the row instead reads to the sweep as a list the operator deleted,
            # which disables the membership it is still protecting with. `strict` is what
            # holds the whole pass still; the tolerant path below only ever displays.
            log.warning("list_config.unreadable", list_id=row.id)
            if strict:
                raise ListRegistryUnreadableError(f"list {row.id} has an unreadable body") from None
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


def fingerprint(definitions: Sequence[ListDefinition]) -> str:
    """Identifies what these definitions would make a scan GATHER into ``Facts.on_lists``.

    The lists are not policy, so no policy hash can carry them: an operator who retags a list,
    repoints it, renames it or switches it off changes which titles their ``on_list`` rules
    protect without touching a policy body. ``PolicyBody.evidence_hash`` used to cover exactly
    this, back when the tags were ``keep_tags`` on the body; moving membership into gathered
    evidence moved it out from under that hash, and the simulator went on replaying the
    membership a scan froze while calling the answer exact (#512).

    Only the ENABLED rows, because a disabled list gathers nothing: editing one changes no
    membership and must not cost a scan, while disabling one drops it from this set and
    rightly does. Name is in because a keep rule matches on it (``lists.on_list_fact`` joins
    the names, not the ids), so a rename is a membership change even when the list is
    otherwise untouched. Sorted by id, so the answer does not depend on row order.
    """
    payload = [
        {
            "id": d.id,
            "name": d.name,
            "source": d.source.value,
            "config": d.config,
        }
        for d in sorted((d for d in definitions if d.enabled), key=lambda d: d.id)
    ]
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(canonical.encode("ascii")).hexdigest()


async def current_fingerprint(session: AsyncSession) -> str | None:
    """:func:`fingerprint` over the stored registry, or ``None`` when it cannot be read.

    ``None`` is the fail-closed answer and never "no lists configured" (rules 65/91): a
    caller comparing it against a snapshot's recorded value gets a mismatch and refuses,
    which is what an unreadable protection registry has to mean.
    """
    try:
        return fingerprint(await definitions(session, strict=True))
    except (SQLAlchemyError, ListRegistryUnreadableError):
        log.warning("list_config.fingerprint_unreadable")
        return None


#: What a fresh install starts with: the two lists the default policy's keep rules name.
#: Deletable like any other list, because deleting one takes its rules with it
#: (``services.list_rules``); the seed runs once, tracked by a flag rather than by the rows,
#: so removing them does not resurrect them on the next read.
DEFAULT_LISTS: tuple[tuple[str, ListSource, dict[str, Any]], ...] = (
    (DEFAULT_IMDB_LIST_NAME, ListSource.IMDB, {"preset": "top250"}),
    (DEFAULT_TAG_LIST_NAME, ListSource.ARR_TAG, {"tags": ["reaper-keep"], "match": "any"}),
)


async def ensure_defaults(session: AsyncSession) -> None:
    """Seed the default lists exactly once. The keep-tags upgrade migration sets the same
    flag after seeding from the operator's stored policy, so an upgraded install keeps its
    own tags and never gains a second, shipped copy beside them."""
    if await app_settings.lists_seeded(session):
        return
    for name, source, config in DEFAULT_LISTS:
        session.add(
            ListConfig(
                name=name,
                source=source.value,
                config_json=json.dumps(config),
                enabled=True,
                built_in=False,
                created_at=utcnow(),
            )
        )
    await app_settings.set_lists_seeded(session)
    await session.commit()


async def all_lists(session: AsyncSession) -> Sequence[ListConfig]:
    """Every configured list, oldest first so the order holds still."""
    await ensure_defaults(session)
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
    if "," in cleaned:
        # The ``on_list`` fact comma-joins the names holding an item (``lists.on_list_fact``)
        # and ``fields._compare`` splits it back on commas, so a name carrying one is never an
        # element of its own fact: the auto-attached keep rule matches nothing while both the
        # Lists row and the Policy screen render it as an outright protection. The other
        # direction is worse -- an item on "Kids, Holiday" alone satisfies a rule naming a
        # different list called "Holiday". Refused here, where they are looking at the box
        # (rule 108 is the same check for a rule value that strips to nothing).
        raise ListConfigError(
            "A list name can't have a comma in it. Reaper separates names with one."
        )
    return cleaned


async def _refuse_name_twice(session: AsyncSession, name: str, *, this_row: int | None) -> None:
    """Refuse a name another list already answers to, capitalized differently or not.

    The column's ``NOCASE`` collation is what actually holds (a read-then-insert can be
    beaten between the two), and it raises ``IntegrityError``, which says nothing an
    operator can act on. This runs first so they read the reason; the constraint is the
    backstop.

    Case-folded on both sides (rule 88), the comparison every reader of a list name makes:
    two lists whose names differ only in case are one list to a keep rule naming either.
    """
    stmt = select(ListConfig.id).where(func.lower(ListConfig.name) == name.strip().casefold())
    if this_row is not None:
        stmt = stmt.where(ListConfig.id != this_row)
    if (await session.execute(stmt)).first() is not None:
        raise ListConfigError("You already have a list with that name. Pick another.")


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
        # One entry per tag, on the comparison form Sonarr and Radarr themselves use: they
        # lower-case every label, so "Keep" and "keep" are one tag there and were two here.
        # Stored twice, the pair collapsed to one tag id at fetch time and only the later
        # spelling was counted, so the Lists screen showed a chip reading "Keep 0" beside
        # "keep 12" for a tag protecting everything it names -- and under match ALL the
        # collapsed set made ALL behave as ANY (#509). The FIRST spelling is kept, which is
        # the one the operator typed before the duplicate.
        seen: set[str] = set()
        tags: list[str] = []
        for raw in config.get("tags", []):
            tag = str(raw).strip()
            key = tag.casefold()
            if not tag or key in seen:
                continue
            seen.add(key)
            tags.append(tag)
        if not tags:
            raise ListConfigError(
                "Add at least one tag, spelled as it appears in Sonarr or Radarr."
            )
        match = "all" if config.get("match") == "all" else "any"
        return json.dumps({"tags": tags, "match": match})
    if source is ListSource.PLEX_WATCHLIST:
        # Nothing to configure: the watchlist is the signed-in account's own.
        return json.dumps({})
    preset = str(config.get("preset", "")).strip()
    if preset:
        if preset not in lists_service.IMDB_PRESETS:
            raise ListConfigError("Pick one of the IMDb presets, or paste a list id instead.")
        return json.dumps({"preset": preset})
    # A pasted URL carries the id inside it, so the id is extracted rather than the paste
    # being bounced back for retyping.
    found = re.search(r"ls\d{6,}", str(config.get("list_id", "")))
    if not found:
        # An all-zero id of the right shape, not a real one: this is operator-facing copy, and
        # a real id is somebody's actual list. The modal's own copy of this sentence and the
        # placeholder beside it spell the same example (rule 144).
        raise ListConfigError("Paste the list's id or URL. An IMDb list id looks like ls000000000.")
    return json.dumps({"list_id": found.group(0)})


async def create(
    session: AsyncSession, *, name: str, source: str, config: dict[str, Any]
) -> ListConfig:
    try:
        kind = ListSource(source)
    except ValueError:
        raise ListConfigError("Pick where the list comes from.") from None
    cleaned = _clean_name(name)
    await _refuse_name_twice(session, cleaned, this_row=None)
    row = ListConfig(
        name=cleaned,
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
        cleaned = _clean_name(name)
        await _refuse_name_twice(session, cleaned, this_row=list_id)
        row.name = cleaned
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
    """Remove a list. Every list is removable, the shipped ones included.

    A rule naming the deleted list must not go on rendering as a live protection, so the API
    route pairs this with ``list_rules.detach_list``, which strips the list's keep rules from
    every stored policy in the same request; ``tests/test_list_rules.py`` pins the pairing.
    """
    row = await get(session, list_id)
    await session.delete(row)
    await session.commit()
    log.info("list_config.deleted", list_id=list_id)
