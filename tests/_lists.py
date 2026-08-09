# SPDX-License-Identifier: AGPL-3.0-or-later
"""The registry fingerprint a fixture's snapshot has to carry, read from production.

A snapshot records which protection lists its scan gathered membership under
(``Snapshot.list_config_hash``), and ``api.simulate.simulate`` refuses when that no longer
matches the registry -- a list retagged or renamed after the scan changes what every
``on_list`` rule protects, and no policy hash can see it (#512).

Fixtures here write their snapshot straight to the database instead of running a scan, so
they have to record the same thing the scan would. This is that value, taken through the
production helpers rather than restated: ``list_config.definitions`` is also what seeds the
default lists on first read, so calling it is what puts the registry in the state the booted
app will find. Restating the seed instead would pin the fixture to today's shipped list set
and go quietly wrong the day it changes (rule 119).

Called BEFORE the app boots, like the snapshot write it belongs to: a second engine on the
same SQLite file under a live async pool is a lock waiting to happen.
"""

from __future__ import annotations

import asyncio

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from reaper.config import Settings
from reaper.services import list_config


def seeded_fingerprint(settings: Settings) -> str:
    """Seed the default lists the way a first read does, and fingerprint them the way a
    scan does."""

    async def run() -> str:
        engine = create_async_engine(settings.database_url)
        try:
            async with async_sessionmaker(engine, expire_on_commit=False)() as session:
                return list_config.fingerprint(await list_config.definitions(session))
        finally:
            await engine.dispose()

    return asyncio.run(run())
