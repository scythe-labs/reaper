# SPDX-License-Identifier: AGPL-3.0-or-later
"""Each Sonarr and Radarr instance's recycle bin, read as a reap starts.

A delete through Sonarr or Radarr moves the files into the instance's recycle bin when one
is set. The space frees only when the bin's cleanup empties it.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

import structlog
from sqlalchemy import ColumnElement, delete, or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from reaper.clients.base import IntegrationError, SafetyViolationError
from reaper.config import Settings
from reaper.crypto import SecretBox
from reaper.db.models import ActionStep, Instance, ReapBin
from reaper.notify.discord import build_notifier
from reaper.services import run_totals
from reaper.services.planner import MediaRef

log = structlog.get_logger(__name__)

#: (kind, instance id), the first two parts of a ``media_key``.
InstanceKey = tuple[str, int]


#: The states of a bin a reap turned off and has not yet put back.
OFF_STATES = ("turning_off", "off")


@runtime_checkable
class BinReader(Protocol):
    async def media_management(self) -> dict[str, Any]: ...


@runtime_checkable
class BinSwitch(BinReader, Protocol):
    async def turn_off_recycle_bin(self, config: dict[str, Any]) -> None: ...
    async def restore_recycle_bin(self, config: dict[str, Any], path: str) -> None: ...


class BinError(Exception):
    """A bin read back different from what Reaper set or expected."""


#: What can go wrong changing a bin: the call, the guard, or the read-back.
SWITCH_ERRORS = (IntegrationError, SafetyViolationError, BinError)


def label(kind: str, name: str) -> str:
    """How operator copy names an instance: its kind, then its name."""
    return f"{kind.title()} {name}"


@dataclass(frozen=True)
class Bin:
    kind: str
    instance_id: int
    name: str
    path: str | None
    """The bin's folder. Empty means no bin. ``None`` means it could not be read."""
    cleanup_days: int | None

    @property
    def key(self) -> InstanceKey:
        return (self.kind, self.instance_id)

    @property
    def state(self) -> str:
        """``on``, ``none``, or ``unknown``, the wire form the browser reads."""
        if self.path is None:
            return "unknown"
        return "on" if self.path else "none"


def instances_of(media_keys: Iterable[str]) -> list[InstanceKey]:
    """The instances these media keys live on, in first-seen order."""
    seen: dict[InstanceKey, None] = {}
    for media_key in media_keys:
        ref = MediaRef.parse(media_key)
        seen[(ref.kind, ref.instance_id)] = None
    return list(seen)


def readers(
    radarr: Mapping[int, object], sonarr: Mapping[int, object]
) -> dict[InstanceKey, BinReader]:
    """The clients that can read a bin, keyed the way ``instances_of`` keys an instance."""
    out: dict[InstanceKey, BinReader] = {}
    for kind, clients in (("radarr", radarr), ("sonarr", sonarr)):
        for instance_id, client in clients.items():
            if isinstance(client, BinReader):
                out[(kind, instance_id)] = client
    return out


def switches(
    radarr: Mapping[int, object], sonarr: Mapping[int, object]
) -> dict[InstanceKey, BinSwitch]:
    """The clients that can also change a bin."""
    out: dict[InstanceKey, BinSwitch] = {}
    for kind, clients in (("radarr", radarr), ("sonarr", sonarr)):
        for instance_id, client in clients.items():
            if isinstance(client, BinSwitch):
                out[(kind, instance_id)] = client
    return out


def _parse(config: dict[str, Any]) -> tuple[str | None, int | None]:
    """The bin's folder and cleanup days from a media management body. A missing
    ``recycleBin`` key is unknown. A null or empty one is no bin. A blank or non-text one is
    unknown."""
    if "recycleBin" not in config:
        path = None
    else:
        raw = config["recycleBin"]
        if raw is None or raw == "":
            path = ""
        elif isinstance(raw, str) and raw.strip():
            path = raw.strip()
        else:
            path = None
    days = config.get("recycleBinCleanupDays")
    return path, days if isinstance(days, int) and not isinstance(days, bool) else None


async def read_bins(
    session: AsyncSession,
    clients: Mapping[InstanceKey, BinReader],
    instances: Iterable[InstanceKey],
) -> list[Bin]:
    """Read each instance's bin now. A bin that cannot be read comes back unknown."""
    wanted = list(instances)
    names = {
        row.id: row.name
        for row in (
            await session.execute(select(Instance).where(Instance.id.in_({i for _, i in wanted})))
        ).scalars()
    }
    bins: list[Bin] = []
    for kind, instance_id in wanted:
        path: str | None = None
        days: int | None = None
        client = clients.get((kind, instance_id))
        if client is not None:
            try:
                path, days = _parse(await client.media_management())
            except IntegrationError as exc:
                log.warning(
                    "recycle_bin.unreadable", kind=kind, instance_id=instance_id, code=exc.code
                )
        bins.append(Bin(kind, instance_id, names.get(instance_id, kind.title()), path, days))
    return bins


async def record(session: AsyncSession, run_id: int, bins: Iterable[Bin]) -> None:
    """Replace the run's stored bins with these, and commit."""
    await session.execute(delete(ReapBin).where(ReapBin.run_id == run_id))
    session.add_all(
        ReapBin(
            run_id=run_id,
            kind=b.kind,
            instance_id=b.instance_id,
            instance_name=b.name,
            bin_path=b.path,
            cleanup_days=b.cleanup_days,
        )
        for b in bins
    )
    await session.commit()


async def stored(session: AsyncSession, run_id: int) -> list[Bin]:
    """The bins a run recorded as it started. Empty for a run from before bins were read."""
    rows = (
        await session.execute(select(ReapBin).where(ReapBin.run_id == run_id).order_by(ReapBin.id))
    ).scalars()
    return [Bin(r.kind, r.instance_id, r.instance_name, r.bin_path, r.cleanup_days) for r in rows]


def _on(keys: Iterable[InstanceKey]) -> ColumnElement[bool] | None:
    clauses = [ActionStep.media_key.like(f"{kind}:{iid}:%") for kind, iid in keys]
    return or_(*clauses) if clauses else None


async def removed_on(
    session: AsyncSession, run_id: int, keys: Iterable[InstanceKey]
) -> run_totals.RunTotals:
    """What the run removed on these instances, counted the way the run's totals are."""
    where = _on(keys)
    if where is None:
        return run_totals.aggregate_rows([])
    rows = (await session.execute(run_totals.totals_query(run_id).where(where))).all()
    return run_totals.aggregate_rows(rows)


async def turn_off(client: BinSwitch, path: str) -> None:
    """Turn off a bin read as ``path`` moments ago, and confirm it reads off."""
    config = await client.media_management()
    current, _ = _parse(config)
    if current == "":
        return
    if current != path:
        raise BinError("the bin changed after it was read")
    await client.turn_off_recycle_bin(config)
    if _parse(await client.media_management())[0] != "":
        raise BinError("the bin still reads on")


async def put_back(client: BinSwitch, path: str) -> str:
    """Put a bin back at ``path``. Returns ``restored``, or ``left`` when it now holds
    another folder, which means someone set it during the reap."""
    config = await client.media_management()
    current, _ = _parse(config)
    if current is None:
        raise BinError("the bin could not be read")
    if current == path:
        return "restored"
    if current:
        return "left"
    await client.restore_recycle_bin(config, path)
    if _parse(await client.media_management())[0] != path:
        raise BinError("the bin did not come back")
    return "restored"


async def still_off(session: AsyncSession) -> list[ReapBin]:
    """Every bin a reap turned off and has not put back."""
    rows = await session.execute(
        select(ReapBin).where(ReapBin.state.in_(OFF_STATES)).order_by(ReapBin.id)
    )
    return list(rows.scalars())


async def restore_pending(
    factory: async_sessionmaker[AsyncSession],
    box: SecretBox,
    settings: Settings,
    clients: Mapping[InstanceKey, BinSwitch],
) -> list[ReapBin]:
    """Put back every bin a reap left off, and tell Discord once about each one that stays
    off and once when it comes back. Returns the bins still off."""
    async with factory() as session:
        rows = await still_off(session)
        if not rows:
            return []
        notifier = await build_notifier(session, box, settings)
        for row in rows:
            name = label(row.kind, row.instance_name)
            client = clients.get((row.kind, row.instance_id))
            try:
                if client is None or not row.bin_path:
                    raise BinError("no client or no folder to put back")
                row.state = await put_back(client, row.bin_path)
            except SWITCH_ERRORS as exc:
                log.warning("recycle_bin.still_off", instance=name, error=str(exc))
                if notifier is not None and not row.announced:
                    row.announced = await notifier.announce_bin_still_off(name, run_id=row.run_id)
                continue
            log.info("recycle_bin.restored", instance=name, state=row.state)
            if notifier is not None and row.announced:
                await notifier.announce_bin_back_on(name)
        await session.commit()
        return [r for r in rows if r.state in OFF_STATES]
