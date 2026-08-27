# SPDX-License-Identifier: AGPL-3.0-or-later
"""The IMDb ratings dataset.

Every other unknown in Reaper protects an item. A missing *rating* does not. The rule is
"keep this if IMDb >= 7.5", so an absent rating means the protection cannot fire and a
well-rated film becomes deletable. An empty or half-loaded table would therefore strip
protection from the entire library, silently, and in the one direction that destroys data.
So these tests are mostly about refusing to answer.
"""

from __future__ import annotations

import gzip
from collections.abc import AsyncIterator
from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from reaper.clock import utcnow
from reaper.config import Settings
from reaper.db.session import create_engine
from reaper.services.imdb_dataset import (
    DatasetDegradedError,
    DatasetState,
    ImdbRatings,
    load,
    parse_rows,
)

SAMPLE = (
    "tconst\taverageRating\tnumVotes\n"
    "tt0000001\t9.3\t3000000\n"  # a film
    "tt0000002\t9.2\t2000000\n"  # another film
    "tt0000003\t9.2\t2500000\n"  # a series, since the dataset covers TV
    "tt9999999\t8.9\t12\n"  # high rating, 12 votes: noise
    "tt0000000\t\\N\t\\N\n"  # IMDb's null
    "malformed-line\n"
)


@pytest.fixture
def archive(tmp_path: Path) -> Path:
    path = tmp_path / "title.ratings.tsv.gz"
    with gzip.open(path, "wt", encoding="utf-8") as handle:
        handle.write(SAMPLE)
    return path


@pytest.fixture
async def engine(tmp_path: Path) -> AsyncIterator[AsyncEngine]:
    eng = create_engine(Settings(data_dir=tmp_path, secret_key="k"))
    yield eng
    await eng.dispose()


class TestParsing:
    def test_rows_are_parsed(self, archive: Path) -> None:
        with archive.open("rb") as handle:
            rows = list(parse_rows(handle))

        assert ("tt0000001", 9.3, 3_000_000) in rows

    def test_nulls_are_skipped_not_coerced_to_zero(self, archive: Path) -> None:
        """IMDb writes \\N for null. A rating of 0.0 would read as 'terrible film, delete
        it', the precise inversion of the truth."""
        with archive.open("rb") as handle:
            ids = [r[0] for r in parse_rows(handle)]

        assert "tt0000000" not in ids

    def test_malformed_lines_are_skipped(self, archive: Path) -> None:
        with archive.open("rb") as handle:
            rows = list(parse_rows(handle))

        assert all(r[0].startswith("tt") for r in rows)

    def test_a_wrong_header_is_an_error_not_a_silent_empty_load(self, tmp_path: Path) -> None:
        """If IMDb changes the format, we must fail loudly. A silently empty parse
        would swap in an empty table and disarm every rating protection."""
        bad = tmp_path / "bad.tsv.gz"
        with gzip.open(bad, "wt", encoding="utf-8") as handle:
            handle.write("something\telse\n")

        with bad.open("rb") as handle, pytest.raises(ValueError, match="Unexpected IMDb"):
            list(parse_rows(handle))


class TestDegradedStateRefusesToAnswer:
    """The heart of it."""

    async def test_lookup_before_any_load_raises(self, engine: AsyncEngine) -> None:
        """It must not return an empty dict. The caller would read that as 'none of these
        films are rated', conclude no rating protection applies, and hand the whole
        library to the reaper."""
        with pytest.raises(DatasetDegradedError, match="missing or stale"):
            await ImdbRatings(engine).lookup(["tt0000001"])

    async def test_stale_data_raises(self, engine: AsyncEngine, archive: Path) -> None:
        await load(engine, archive)

        async with engine.begin() as conn:
            old = int((utcnow() - timedelta(days=90)).timestamp())
            await conn.execute(
                text("UPDATE imdb_dataset_sync SET synced_at = :t WHERE id = 1"), {"t": old}
            )

        with pytest.raises(DatasetDegradedError):
            await ImdbRatings(engine, max_age=timedelta(days=14)).lookup(["tt0000001"])

    def test_state_reports_degraded_when_empty(self) -> None:
        assert DatasetState(row_count=0, synced_at=None).degraded() is True

    def test_state_reports_degraded_when_old(self) -> None:
        stale = DatasetState(row_count=1, synced_at=utcnow() - timedelta(days=30))
        assert stale.degraded(max_age=timedelta(days=14)) is True

    def test_fresh_state_is_healthy(self) -> None:
        fresh = DatasetState(row_count=1_694_979, synced_at=utcnow())
        assert fresh.degraded() is False


class TestAtomicLoad:
    async def test_load_populates_and_marks_fresh(self, engine: AsyncEngine, archive: Path) -> None:
        loaded = await load(engine, archive)

        assert loaded.rows == 4  # the two nulls and the malformed line are excluded
        state = await ImdbRatings(engine).state()
        assert state.row_count == 4
        assert state.degraded() is False

    async def test_the_dropped_row_count_reaches_the_caller(
        self, engine: AsyncEngine, archive: Path
    ) -> None:
        """The skip count is what tells anyone the file's format moved under us.

        A load that keeps millions of rows but drops half of them still clears the
        zero-row tripwire, so nothing else catches it, and every rating it lost is a title
        that no longer has a rating to protect it. Logging alone left it where no operator
        would ever look.
        """
        loaded = await load(engine, archive)

        assert loaded.skipped == 2  # the two IMDb nulls, and the malformed line has 2 columns
        assert loaded.skip_fraction > 0
        assert loaded.drifted is True  # 2 of 6 read, far past the drift threshold

    async def test_a_clean_load_does_not_read_as_drift(
        self, engine: AsyncEngine, tmp_path: Path
    ) -> None:
        """IMDb always carries a trickle of nulls, so the flag must mean "the shape
        changed", not "one row was unrated"."""
        clean = tmp_path / "clean.tsv.gz"
        with gzip.open(clean, "wt", encoding="utf-8") as handle:
            handle.write("tconst\taverageRating\tnumVotes\n")
            for n in range(100):
                handle.write(f"tt{n:07d}\t7.5\t1000\n")

        loaded = await load(engine, clean)

        assert loaded.rows == 100
        assert loaded.skipped == 0
        assert loaded.drifted is False

    async def test_an_empty_parse_is_refused_rather_than_swapped_in(
        self, engine: AsyncEngine, tmp_path: Path
    ) -> None:
        """A header-only file must not replace good data with nothing. That would
        look exactly like 'nothing in your library is rated'."""
        empty = tmp_path / "empty.tsv.gz"
        with gzip.open(empty, "wt", encoding="utf-8") as handle:
            handle.write("tconst\taverageRating\tnumVotes\n")

        with pytest.raises(ValueError, match="zero rows"):
            await load(engine, empty)

    async def test_a_failed_load_leaves_the_previous_data_intact(
        self, engine: AsyncEngine, archive: Path, tmp_path: Path
    ) -> None:
        """Staging + swap is there so a download that dies halfway cannot leave the
        library unprotected."""
        await load(engine, archive)

        empty = tmp_path / "empty.tsv.gz"
        with gzip.open(empty, "wt", encoding="utf-8") as handle:
            handle.write("tconst\taverageRating\tnumVotes\n")
        with pytest.raises(ValueError):
            await load(engine, empty)

        # The good data survived, and lookups still work.
        found = await ImdbRatings(engine).lookup(["tt0000001"])
        assert found["tt0000001"].average_rating == 9.3


class TestLookup:
    async def test_returns_rating_and_votes(self, engine: AsyncEngine, archive: Path) -> None:
        await load(engine, archive)

        found = await ImdbRatings(engine).lookup(["tt0000001", "tt0000002"])

        assert found["tt0000001"].average_rating == 9.3
        assert found["tt0000001"].num_votes == 3_000_000
        assert len(found) == 2

    async def test_covers_television_not_just_film(
        self, engine: AsyncEngine, archive: Path
    ) -> None:
        """The reason to prefer the dataset over an API. It rates TV series and
        individual episodes, which nothing else gives us for free."""
        await load(engine, archive)

        found = await ImdbRatings(engine).lookup(["tt0000003"])  # the series
        assert found["tt0000003"].num_votes > 1_000_000

    async def test_an_unknown_id_is_simply_absent(self, engine: AsyncEngine, archive: Path) -> None:
        """Absent from a *healthy* dataset is a real answer. This title genuinely has no
        IMDb rating. That is different from the dataset being broken, which raises."""
        await load(engine, archive)

        found = await ImdbRatings(engine).lookup(["tt0000404"])
        assert found == {}

    async def test_the_vote_count_is_what_makes_a_floor_meaningful(
        self, engine: AsyncEngine, archive: Path
    ) -> None:
        """8.9/10 from 12 votes is noise. It is an obscure title carrying a high score on a
        tiny number of votes, and every library holds a handful. Without the vote count, a
        rating floor would protect every one and fill the library with well-rated junk."""
        await load(engine, archive)
        found = await ImdbRatings(engine).lookup(["tt9999999"])

        rating = found["tt9999999"]
        assert rating.average_rating >= 7.5  # would pass a naive floor
        assert rating.num_votes < 1000  # ...but is meaningless
