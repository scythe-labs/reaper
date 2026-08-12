# SPDX-License-Identifier: AGPL-3.0-or-later
"""The IMDb ratings dataset.

``https://datasets.imdbws.com/title.ratings.tsv.gz`` -- 8.2 MB gzipped, ~1.69M
rows, refreshed daily, no API key. Columns: ``tconst``, ``averageRating``,
``numVotes``.

Why this rather than an API: it is the source of truth, it is free, it needs no
key or rate-limit budget, and -- uniquely -- it covers **TV series and individual
episodes**, not just movies. It also gives us the **vote count**, without which a
rating floor is meaningless: a 9.5 from 12 votes is noise, and a rule that
protects on rating alone would preserve junk.

## The failure direction that matters

Every other unknown in Reaper protects an item. **A missing rating does the
opposite.** The rule is "do not delete this if IMDb >= 7.5", so an absent rating
means the protection cannot fire, and a well-rated film becomes deletable. An
empty or half-loaded table would therefore silently strip protection from the
entire library -- the worst possible failure, arriving quietly.

Two defenses:

* **The load is atomic.** Rows go into a staging table and are swapped in only
  once every row has landed. A download that dies halfway leaves the previous
  data intact rather than a partial one.
* **Degradation is loud and fails closed.** If the table is empty, or the data is
  older than ``max_age_days``, ``ImdbRatings.degraded`` is true -- and the rating
  gate must then protect everything rather than quietly protect nothing. Callers
  cannot forget this: ``lookup`` refuses to answer while degraded.
"""

from __future__ import annotations

import asyncio
import gzip
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime, timedelta
from itertools import islice
from pathlib import Path
from typing import IO
from urllib.parse import urlsplit

import httpx2
import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from reaper.clients.public import PublicClient
from reaper.clock import utcnow
from reaper.db import KEY_CHUNK

log = structlog.get_logger(__name__)

DATASET_URL = "https://datasets.imdbws.com/title.ratings.tsv.gz"

# Ratings move slowly, but a dataset stuck for a fortnight means the nightly job
# has been silently failing, and that is worth surfacing.
DEFAULT_MAX_AGE = timedelta(days=14)

_CHUNK = 65_536


@dataclass(frozen=True)
class ImdbRating:
    tconst: str
    average_rating: float
    num_votes: int


@dataclass(frozen=True)
class DatasetState:
    row_count: int
    synced_at: datetime | None

    def degraded(
        self, *, max_age: timedelta = DEFAULT_MAX_AGE, now: datetime | None = None
    ) -> bool:
        if self.row_count == 0 or self.synced_at is None:
            return True
        return (now or utcnow()) - self.synced_at > max_age


class DatasetDegradedError(RuntimeError):
    """The ratings data is missing or stale.

    Raised rather than returning None, because a None here would be read as "this
    film has no rating" -- and therefore "no rating protection applies" -- which
    would strip protection from every title at once.
    """


def _take_batch(rows: Iterator[tuple[str, float, int]], size: int) -> list[dict[str, object]]:
    """The next ``size`` parsed rows as insert parameters.

    Called via ``asyncio.to_thread``: the gunzip + TSV parse behind ``rows`` is
    CPU-bound, and pulling it batch-by-batch in a worker thread keeps the event loop
    serving requests during the dataset refresh.
    """
    return [
        {"tconst": tconst, "average_rating": rating, "num_votes": votes}
        for tconst, rating, votes in islice(rows, size)
    ]


#: Above this share of dropped rows, a load is treated as format drift rather than the
#: ordinary trickle of IMDb nulls. A healthy dataset drops a fraction of a percent; the
#: shape this catches is an upstream column change that silently halves rating coverage
#: while still loading millions of rows, so the zero-row tripwire never fires.
DRIFT_SKIP_FRACTION = 0.05


@dataclass(frozen=True, slots=True)
class LoadResult:
    """What one dataset load produced: rows kept, and rows dropped as unreadable."""

    rows: int
    skipped: int

    @property
    def skip_fraction(self) -> float:
        seen = self.rows + self.skipped
        return self.skipped / seen if seen else 0.0

    @property
    def drifted(self) -> bool:
        """Whether enough rows were dropped to suspect the format changed."""
        return self.skip_fraction >= DRIFT_SKIP_FRACTION


def parse_rows(
    stream: IO[bytes], counters: dict[str, int] | None = None
) -> Iterator[tuple[str, float, int]]:
    """Parse the gzipped TSV without holding it in memory.

    Rows with ``\\N`` (IMDb's null) or malformed numbers are skipped rather than
    coerced: a rating of 0.0 would read as "terrible film, delete it".

    ``counters`` (optional) accumulates a ``skipped`` count of the dropped rows. ``load``
    returns it on :class:`LoadResult` and the nightly job puts it in front of the
    operator, because a large skip fraction quietly shrinks rating coverage while staying
    above the zero-row tripwire that would otherwise catch it -- and less rating coverage
    means fewer well-rated titles protected.
    """

    def _skip() -> None:
        if counters is not None:
            counters["skipped"] = counters.get("skipped", 0) + 1

    with gzip.open(stream, "rt", encoding="utf-8") as handle:
        header = handle.readline()
        if not header.startswith("tconst"):
            raise ValueError(f"Unexpected IMDb dataset header: {header[:60]!r}")

        for line in handle:
            parts = line.rstrip("\n").split("\t")
            if len(parts) != 3:
                _skip()
                continue
            tconst, rating, votes = parts
            if not tconst.startswith("tt"):
                _skip()
                continue
            try:
                yield tconst, float(rating), int(votes)
            except ValueError:
                _skip()
                continue  # \N or junk -- absent, not zero


async def download(destination: Path, *, url: str = DATASET_URL) -> Path:
    destination.parent.mkdir(parents=True, exist_ok=True)
    tmp = destination.with_suffix(".part")

    # Through clients/ like every other fetch (rule 33): shared timeouts, error mapping,
    # and redirect policy. The dataset mirror redirects to a CDN, which PublicClient
    # (credential-less by construction) is allowed to follow.
    parts = urlsplit(url)
    path = parts.path + (f"?{parts.query}" if parts.query else "")
    async with PublicClient(
        f"{parts.scheme}://{parts.netloc}", timeout=httpx2.Timeout(60.0)
    ) as client:
        await client.stream_to(path, tmp)

    # Rename only once the whole file is on disk, so a killed process cannot leave
    # a truncated archive that parses as a short but valid dataset.
    tmp.replace(destination)
    log.info("imdb.downloaded", path=str(destination))
    return destination


async def load(engine: AsyncEngine, archive: Path) -> LoadResult:
    """Load the dataset, swapping it in atomically.

    The multi-minute parse+insert populates a *staging* table in many short transactions,
    each committed on its own, so no write lock is held across the parse. The cache DB is
    WAL with a 5s ``busy_timeout``, which allows concurrent readers but only one writer; a
    single transaction spanning the whole load would make every other cache writer
    (``history_sync`` during a scan, ``lists.sync``, the nightly curated refresh) wait out
    the timeout and fail with 'database is locked'. Only the final swap must be atomic, and
    it holds the write lock for well under a second.

    Crash safety is unchanged. The previous ``imdb_rating`` stays live until the swap, so a
    process killed mid-populate leaves only a partial ``imdb_rating_staging`` -- which the
    next run's ``DROP TABLE IF EXISTS imdb_rating_staging`` below clears before starting.
    """
    insert = text(
        "INSERT OR REPLACE INTO imdb_rating_staging (tconst, average_rating, num_votes) "
        "VALUES (:tconst, :average_rating, :num_votes)"
    )

    # -- staging, rebuilt from scratch each run (its own short transaction) --------
    async with engine.begin() as conn:
        await conn.execute(text("DROP TABLE IF EXISTS imdb_rating_staging"))
        await conn.execute(
            text(
                "CREATE TABLE imdb_rating_staging ("
                "  tconst TEXT PRIMARY KEY,"
                "  average_rating REAL NOT NULL,"
                "  num_votes INTEGER NOT NULL"
                ")"
            )
        )

    # -- populate staging: one short transaction per batch, no lock held across the parse --
    rows = 0
    counters: dict[str, int] = {"skipped": 0}
    with archive.open("rb") as handle:
        parsed = parse_rows(handle, counters)
        while True:
            # The 1.7M-row gunzip + parse is CPU-bound; producing each batch in a worker
            # thread keeps the event loop serving requests through the refresh.
            batch = await asyncio.to_thread(_take_batch, parsed, 10_000)
            if not batch:
                break
            async with engine.begin() as conn:
                await conn.execute(insert, batch)
            rows += len(batch)

    if rows == 0:
        # Never swap in an empty table: it would look exactly like "nothing is rated",
        # and every rating protection in the library would stop firing. The partial (empty)
        # staging table is left for the next run's DROP to clear; imdb_rating stays live.
        raise ValueError("IMDb dataset parsed to zero rows; refusing to swap it in.")

    # -- the swap: the only part that must be atomic, and it is sub-second ---------
    # Until this single short transaction commits, the previous data is still live.
    async with engine.begin() as conn:
        await conn.execute(text("DROP TABLE IF EXISTS imdb_rating"))
        await conn.execute(text("ALTER TABLE imdb_rating_staging RENAME TO imdb_rating"))
        await conn.execute(
            text("CREATE INDEX IF NOT EXISTS ix_imdb_rating_votes ON imdb_rating (num_votes)")
        )

        await conn.execute(
            text(
                "CREATE TABLE IF NOT EXISTS imdb_dataset_sync ("
                "  id INTEGER PRIMARY KEY CHECK (id = 1),"
                "  synced_at INTEGER NOT NULL,"
                "  row_count INTEGER NOT NULL"
                ")"
            )
        )
        await conn.execute(
            text(
                "INSERT OR REPLACE INTO imdb_dataset_sync (id, synced_at, row_count) "
                "VALUES (1, :synced_at, :row_count)"
            ),
            {"synced_at": int(utcnow().timestamp()), "row_count": rows},
        )

    # ``skipped`` counts rows dropped as null or malformed. A sudden jump against a steady
    # ``rows`` is the signature of an upstream format change silently shrinking coverage.
    skipped = counters["skipped"]
    result = LoadResult(rows=rows, skipped=skipped)
    if result.drifted:
        # Loud, because this is the failure that removes protection: fewer ratings means
        # fewer well-rated titles kept, and the zero-row tripwire never fires for a load
        # that is merely half a load. Returned as well as logged -- the caller puts it in
        # front of the operator (see scheduler.refresh_ratings).
        log.warning("imdb.load_drift", rows=rows, skipped=skipped, fraction=result.skip_fraction)
    else:
        log.info("imdb.loaded", rows=rows, skipped=skipped)
    return result


class ImdbRatings:
    """Read access to the loaded dataset."""

    def __init__(self, engine: AsyncEngine, *, max_age: timedelta = DEFAULT_MAX_AGE) -> None:
        self._engine = engine
        self._max_age = max_age

    async def state(self) -> DatasetState:
        async with self._engine.connect() as conn:
            exists = await conn.execute(
                text("SELECT name FROM sqlite_master WHERE type='table' AND name='imdb_rating'")
            )
            if exists.first() is None:
                return DatasetState(row_count=0, synced_at=None)

            row = (
                await conn.execute(
                    text("SELECT synced_at, row_count FROM imdb_dataset_sync WHERE id = 1")
                )
            ).first()

        if row is None:
            return DatasetState(row_count=0, synced_at=None)

        from datetime import UTC

        return DatasetState(
            row_count=int(row.row_count),
            synced_at=datetime.fromtimestamp(int(row.synced_at), tz=UTC),
        )

    async def lookup(self, imdb_ids: list[str]) -> dict[str, ImdbRating]:
        """Ratings for a batch of IMDb ids.

        Raises ``DatasetDegradedError`` when the data is missing or stale. It does
        *not* quietly return an empty mapping: the caller would read that as "none
        of these are rated", conclude that no rating protection applies, and make
        the entire library deletable.
        """
        state = await self.state()
        if state.degraded(max_age=self._max_age):
            raise DatasetDegradedError(
                f"The IMDb ratings dataset is missing or stale "
                f"(rows={state.row_count}, synced_at={state.synced_at}). "
                "Rating protections cannot be evaluated, so nothing may be reaped on "
                "rating grounds until it is refreshed."
            )

        if not imdb_ids:
            return {}

        out: dict[str, ImdbRating] = {}
        async with self._engine.connect() as conn:
            # Chunked to stay well under SQLite's variable limit.
            for start in range(0, len(imdb_ids), KEY_CHUNK):
                chunk = imdb_ids[start : start + KEY_CHUNK]
                placeholders = ", ".join(f":id{i}" for i in range(len(chunk)))
                params = {f"id{i}": value for i, value in enumerate(chunk)}
                # The interpolated fragment is ":id0, :id1, ..." -- generated here,
                # never user input. The ids themselves are bound parameters.
                query = (
                    "SELECT tconst, average_rating, num_votes FROM imdb_rating "  # noqa: S608
                    f"WHERE tconst IN ({placeholders})"
                )
                result = await conn.execute(text(query), params)
                for row in result:
                    out[row.tconst] = ImdbRating(
                        tconst=row.tconst,
                        average_rating=float(row.average_rating),
                        num_votes=int(row.num_votes),
                    )
        return out


async def refresh(engine: AsyncEngine, data_dir: Path) -> LoadResult:
    """Download and load. The nightly job."""
    archive = await download(data_dir / "title.ratings.tsv.gz")
    return await load(engine, archive)
