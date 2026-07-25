# SPDX-License-Identifier: AGPL-3.0-or-later
"""One-time import of integrations declared in the environment.

The database is the source of truth for credentials. This exists only so that a
declarative deployment (docker-compose with an env file) does not have to be
clicked together by hand on first boot.

Semantics, chosen so that the environment can never silently overwrite something
you changed in the UI:

* An instance is imported only if no instance of that ``(kind, name)`` exists.
* An existing instance is **never** updated from the environment. If you rotate
  a key, rotate it in the UI -- or delete the instance and let it re-seed.
* Once every declared instance is present, Reaper logs that the environment
  variables can be removed.
"""

from __future__ import annotations

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from reaper.clock import utcnow
from reaper.config import InstanceSeed
from reaper.crypto import SecretBox
from reaper.db.models import Instance, InstanceKind
from reaper.services.instances import SINGLETON_KINDS

log = structlog.get_logger(__name__)


async def seed_instances(
    session: AsyncSession,
    seeds: list[InstanceSeed],
    box: SecretBox,
) -> tuple[int, int]:
    """Import declared instances. Returns ``(imported, skipped)``."""
    if not seeds:
        return 0, 0

    imported = skipped = 0
    seeded_singletons: set[InstanceKind] = set()
    #: ``(kind, name)`` pairs already added in THIS batch. The duplicate check below is a
    #: query, and nothing is flushed until the end, so two seeds naming the same instance
    #: both read "not there yet" and both get added -- one instance the operator declared,
    #: two rows the scan would walk (B-15). The singleton path has always had its own
    #: in-batch set for exactly this; this is the same guard for every other kind.
    seeded_pairs: set[tuple[InstanceKind, str]] = set()
    for seed in seeds:
        try:
            kind = InstanceKind(seed.kind)
        except ValueError:
            log.warning("seed.unknown_kind", kind=seed.kind)
            continue

        if kind in SINGLETON_KINDS:
            # A singleton kind (Tautulli) allows exactly one, the same invariant the UI
            # enforces. If one already exists -- from the UI, a prior boot, or earlier in
            # this batch -- skip the rest rather than seed a second the scan would silently
            # ignore. The in-batch set covers a session that has not flushed yet.
            present = await session.scalar(select(Instance).where(Instance.kind == kind))
            if present is not None or kind in seeded_singletons:
                log.warning("seed.singleton_skipped", kind=kind.value, name=seed.name)
                skipped += 1
                continue

        existing = await session.scalar(
            select(Instance).where(Instance.kind == kind, Instance.name == seed.name)
        )
        if existing is not None or (kind, seed.name) in seeded_pairs:
            # Never clobber the database from the environment: the UI is where
            # credentials are managed once they exist. The in-batch set covers the repeat
            # that this session has added but not yet flushed, which the query cannot see.
            skipped += 1
            continue

        session.add(
            Instance(
                kind=kind,
                name=seed.name,
                base_url=seed.base_url,
                api_key_enc=box.encrypt(seed.api_key.get_secret_value()),
                enabled=True,
                created_at=utcnow(),
            )
        )
        if kind in SINGLETON_KINDS:
            seeded_singletons.add(kind)
        seeded_pairs.add((kind, seed.name))
        imported += 1
        log.info("seed.imported", kind=kind.value, name=seed.name, base_url=seed.base_url)

    await session.flush()

    if imported:
        log.info(
            "seed.complete",
            imported=imported,
            skipped=skipped,
            detail=(
                "Credentials are now encrypted in the database. The REAPER_*_API_KEY "
                "variables can be removed from your environment."
            ),
        )
    return imported, skipped
