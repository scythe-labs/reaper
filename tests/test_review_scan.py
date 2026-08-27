# SPDX-License-Identifier: AGPL-3.0-or-later
"""Scan-lane behavior that the rest of the suite does not already cover.

Two things pinned here:

* the movie -> Plex join must fail closed on duplicate titles, exactly as the season
  path does, instead of resolving to whichever title matched last;
* the grace clock must restart when a rescued item is re-condemned after a real gap.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import datetime, timedelta
from pathlib import Path
from typing import ClassVar

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from structlog.testing import capture_logs

from reaper.clients.plex import PlexError
from reaper.clock import from_epoch, utcnow
from reaper.config import Settings
from reaper.db.base import Base
from reaper.db.models import FirstFlagged
from reaper.db.session import create_engine, create_session_factory
from reaper.engine import identity
from reaper.ratings import Rating, RatingSource
from reaper.services import history_sync, lists
from reaper.services.library_index import _RETIRED_DEGRADE_FLOOR, _RETIRED_DEGRADE_SHARE
from reaper.services.season_scan import build_tv_index
from reaper.services.snapshot import (
    _fold_merged_watch_stats,
    _insert_first_flags,
    _raw_items,
    _watch_stats,
    build_movie_index,
    protection_sync_degradations,
    record_first_flagged_bulk,
)
from tests._fakes import FakeTautulli, movie_library, show_library

# Second precision: the timestamp columns round-trip through SQLite at whole-second
# precision, so a microsecond-bearing utcnow() would not compare equal after a fetch.
NOW = utcnow().replace(microsecond=0)


class TestMovieDecisionLog:
    """Every movie Radarr returns emits one greppable decision line, so "why isn't my movie
    in the queue" is answerable from the log. This is the movie twin of
    season_scan.series_decision."""

    def _index(self) -> identity.PlexIndex:
        return identity.PlexIndex.build(
            [identity.PlexItem(rating_key=1, title="A Film", year=2020, added_at=NOW)]
        )

    def test_a_movie_with_a_file_logs_candidate(self) -> None:
        movie = {
            "id": 7,
            "title": "A Film",
            "year": 2020,
            "tmdbId": 500,
            "hasFile": True,
            "sizeOnDisk": 1234,
        }
        with capture_logs() as logs:
            _raw_items([movie], self._index(), instance_id=3)
        decisions = [e for e in logs if e["event"] == "scan.movie_decision"]
        assert len(decisions) == 1
        d = decisions[0]
        assert d["outcome"] == "candidate"
        assert d["title"] == "A Film"
        assert d["tmdb_id"] == 500
        assert d["has_file"] is True
        assert d["size_bytes"] == 1234
        assert d["instance_id"] == 3

    def test_a_movie_without_a_file_logs_no_file(self) -> None:
        movie = {"id": 8, "title": "Not Downloaded", "year": 2021, "hasFile": False}
        with capture_logs() as logs:
            items = _raw_items([movie], self._index(), instance_id=3)
        assert items == []  # dropped: nothing on disk to reap
        decisions = [e for e in logs if e["event"] == "scan.movie_decision"]
        assert len(decisions) == 1
        d = decisions[0]
        assert d["outcome"] == "no_file"
        assert d["has_file"] is False
        assert d["size_bytes"] is None


# ---------------------------------------------------------------------------
# The movie -> Plex join: duplicate titles must fail closed (the high finding).
# ---------------------------------------------------------------------------


class TestTheMovieJoinAtTheScanLane:
    """End-to-end proof that ``_raw_items`` threads the shared resolver correctly. Two
    films can share a title (remakes), and neither of these fixtures carries an external
    id, so an ambiguous title must leave the candidate unmatched (Unknown facts -> ABSTAIN,
    executor spares a keyless item) and a year that singles out one Plex row must bind it.
    The resolver's own tier logic is unit-tested in ``test_identity.py``."""

    def _index(self) -> identity.PlexIndex:
        # A remake pair, distinguished only by year. Neither carries an id, so the
        # title+year backstop is what binds them.
        return identity.PlexIndex.build(
            [
                identity.PlexItem(rating_key=11, title="The Mummy", year=1999, added_at=NOW),
                identity.PlexItem(rating_key=22, title="The Mummy", year=2017, added_at=NOW),
            ]
        )

    def test_raw_items_leaves_an_ambiguous_movie_unmatched(self) -> None:
        movie = {"id": 1, "title": "The Mummy", "hasFile": True, "sizeOnDisk": 1}
        items = _raw_items([movie], self._index(), instance_id=1)
        assert len(items) == 1
        assert items[0].plex_rating_key is None
        assert items[0].added_at is None

    def test_raw_items_binds_a_disambiguated_movie_by_title(self) -> None:
        movie = {"id": 1, "title": "The Mummy", "year": 2017, "hasFile": True, "sizeOnDisk": 1}
        items = _raw_items([movie], self._index(), instance_id=1)
        assert items[0].plex_rating_key == 22
        assert items[0].added_at == NOW
        assert items[0].matched_by is identity.MatchedBy.TITLE_YEAR

    def test_raw_items_binds_by_tmdb_across_a_title_difference(self) -> None:
        """A Plex row whose title differs (a regional or renamed title) still binds when
        the tmdb id agrees. That is what id matching is for."""
        index = identity.PlexIndex.build(
            [
                identity.PlexItem(
                    rating_key=42,
                    title="A Regional Title",
                    year=2020,
                    added_at=NOW,
                    ids=identity.ExternalIds.of(tmdb=1001),
                )
            ]
        )
        movie = {
            "id": 1,
            "title": "The Original Title",
            "year": 2020,
            "tmdbId": 1001,
            "hasFile": True,
            "sizeOnDisk": 1,
        }
        items = _raw_items([movie], index, instance_id=1)
        assert items[0].plex_rating_key == 42
        assert items[0].matched_by is identity.MatchedBy.TMDB

    def test_raw_items_abstains_when_a_shared_id_names_two_rows(self) -> None:
        """A duplicate tmdb across two Plex items is ambiguous -> keyless candidate."""
        index = identity.PlexIndex.build(
            [
                identity.PlexItem(
                    rating_key=1,
                    title="A",
                    year=2020,
                    added_at=NOW,
                    ids=identity.ExternalIds.of(tmdb=1001),
                ),
                identity.PlexItem(
                    rating_key=2,
                    title="B",
                    year=2020,
                    added_at=NOW,
                    ids=identity.ExternalIds.of(tmdb=1001),
                ),
            ]
        )
        movie = {
            "id": 9,
            "title": "A",
            "year": 2020,
            "tmdbId": 1001,
            "hasFile": True,
            "sizeOnDisk": 1,
        }
        items = _raw_items([movie], index, instance_id=1)
        assert items[0].plex_rating_key is None
        assert items[0].matched_by is None

    def test_raw_items_narrows_a_shared_id_by_the_movies_file_name(self) -> None:
        """The split-library case through the scan path: two Plex rows share the tmdb id
        (an HD and a 4K section), and the movie's own file name picks its copy."""
        index = identity.PlexIndex.build(
            [
                identity.PlexItem(
                    rating_key=1,
                    title="Example",
                    year=2020,
                    added_at=NOW,
                    ids=identity.ExternalIds.of(tmdb=1001),
                    file_basename="example (2020) 1080p.mkv",
                    files=(identity.PlexFile("example (2020) 1080p.mkv"),),
                ),
                identity.PlexItem(
                    rating_key=2,
                    title="Example",
                    year=2020,
                    added_at=NOW,
                    ids=identity.ExternalIds.of(tmdb=1001),
                    file_basename="example (2020) 2160p.mkv",
                    files=(identity.PlexFile("example (2020) 2160p.mkv"),),
                ),
            ]
        )
        movie = {
            "id": 9,
            "title": "Example",
            "year": 2020,
            "tmdbId": 1001,
            "hasFile": True,
            "sizeOnDisk": 1,
            "movieFile": {"relativePath": "Example (2020) 2160p.mkv"},
        }
        items = _raw_items([movie], index, instance_id=1)
        assert items[0].plex_rating_key == 2
        assert items[0].added_at == NOW
        assert items[0].matched_by is identity.MatchedBy.ID_AND_BASENAME
        assert items[0].match_status is identity.MatchStatus.MATCHED

    def test_raw_items_merges_byte_identical_twin_listings(self) -> None:
        """The curated re-list case through the scan path: two Plex rows share the tmdb
        id AND the file name, and Radarr's exact byte size proves they are one file
        listed twice. The item binds to the earliest listing and carries every listing's
        key, so its watch stats can be read from all of them."""
        earlier = NOW - timedelta(days=900)
        index = identity.PlexIndex.build(
            [
                identity.PlexItem(
                    rating_key=1,
                    title="Example",
                    year=2020,
                    added_at=earlier,
                    ids=identity.ExternalIds.of(tmdb=1001),
                    file_basename="example (2020).mkv",
                    files=(identity.PlexFile("example (2020).mkv", 7_000),),
                ),
                identity.PlexItem(
                    rating_key=2,
                    title="Example",
                    year=2020,
                    added_at=NOW,
                    ids=identity.ExternalIds.of(tmdb=1001),
                    file_basename="example (2020).mkv",
                    files=(identity.PlexFile("example (2020).mkv", 7_000),),
                ),
            ]
        )
        movie = {
            "id": 9,
            "title": "Example",
            "year": 2020,
            "tmdbId": 1001,
            "hasFile": True,
            "sizeOnDisk": 7_050,
            "movieFile": {"relativePath": "Example (2020).mkv", "size": 7_000},
        }
        items = _raw_items([movie], index, instance_id=1)
        assert items[0].plex_rating_key == 1
        assert items[0].added_at == earlier
        assert items[0].matched_by is identity.MatchedBy.MERGED_LISTINGS
        assert items[0].match_status is identity.MatchStatus.MATCHED
        assert items[0].merged_rating_keys == (1, 2)


# ---------------------------------------------------------------------------
# build_movie_index: Tautulli spine + plexapi enrichment, fail-closed on sweep failure.
# ---------------------------------------------------------------------------


class _FakePlexSweep:
    def __init__(self, items: dict[int, identity.PlexItem]) -> None:
        self._items = items

    async def library_guid_index(
        self, *, section_type: str, allowed_sections: set[int] | None = None
    ) -> dict[int, identity.PlexItem]:
        return self._items


class _FakePlexBrokenSweep:
    async def library_guid_index(
        self, *, section_type: str, allowed_sections: set[int] | None = None
    ) -> dict[int, object]:
        raise PlexError("the sweep blew up")


_SPINE_ROWS = [{"rating_key": 100, "title": "Example", "year": 2020, "added_at": "1700000000"}]


class TestBuildMovieIndex:
    async def test_ids_and_basename_enrich_the_tautulli_spine(self) -> None:
        """The plexapi sweep joins onto the spine by rating key, and added_at STILL comes
        from Tautulli (the dormancy floor must not shift)."""
        swept = {
            100: identity.PlexItem(
                rating_key=100,
                title="Example",
                year=2020,
                added_at=from_epoch("1600000000"),  # differs from the spine on purpose
                ids=identity.ExternalIds.of(tmdb=1001),
                file_basename="example (2020).mkv",
                video_resolution="1080",
                content_rating="PG-13",
                runtime_minutes=95,
                ratings=(
                    Rating(
                        source=RatingSource.ROTTEN_TOMATOES_CRITIC,
                        value=7.7,
                        votes=None,
                        provider="plex",
                    ),
                ),
            )
        }
        index = await build_movie_index(
            movie_library(_SPINE_ROWS),
            _FakePlexSweep(swept),  # type: ignore[arg-type]
            degrade=lambda _r: None,
        )
        item = index.by_rating_key[100]
        assert item.added_at == from_epoch("1700000000")  # from the spine, not the sweep
        assert item.ids.tmdb == 1001
        assert item.file_basename == "example (2020).mkv"
        assert index.by_tmdb[1001] == [100]
        # The display metadata must survive the spine rebuild. This loop copies fields one
        # by one, and a field missed here silently never reaches a card.
        assert item.video_resolution == "1080"
        assert item.content_rating == "PG-13"
        assert item.runtime_minutes == 95
        assert [r.source for r in item.ratings] == [RatingSource.ROTTEN_TOMATOES_CRITIC]

    async def test_an_item_the_tautulli_cache_has_not_listed_still_enters_the_index(self) -> None:
        """Tautulli's media-info listing is a cache and lags fresh additions. An item the
        plexapi sweep sees but the spine does not must still enter the index (with Plex's
        own added-at), or the resolver falsely reports a matched item as unmatched."""
        fresh = identity.PlexItem(
            rating_key=200,
            title="Example Fresh",
            year=2026,
            added_at=from_epoch("1700000500"),
            ids=identity.ExternalIds.of(tmdb=2002),
            file_basename="example fresh (2026).mkv",
        )
        swept = {
            100: identity.PlexItem(
                rating_key=100,
                title="Example",
                year=2020,
                added_at=None,
                ids=identity.ExternalIds.of(tmdb=1001),
                file_basename="example (2020).mkv",
            ),
            200: fresh,
        }
        index = await build_movie_index(
            movie_library(_SPINE_ROWS),
            _FakePlexSweep(swept),  # type: ignore[arg-type]
            degrade=lambda _r: None,
        )
        assert index.by_rating_key[200] == fresh  # in the index, Plex's added_at intact
        res = identity.resolve_movie(
            ids=identity.ExternalIds.of(tmdb=2002),
            title="Example Fresh",
            year=2026,
            file_basename=None,
            index=index,
        )
        assert res.rating_key == 200
        assert res.status is identity.MatchStatus.MATCHED

    async def test_a_sweep_failure_degrades_but_still_matches_by_title(self) -> None:
        """A failed GUID sweep degrades the snapshot, so nothing may be deleted from it, but
        items still fall through to the title+year backstop. The reason is written for the
        operator, who reads it verbatim in the incomplete-scan notice, so it says "Plex" in
        plain words rather than naming a GUID or calling the scan un-executable."""
        reasons: list[str] = []
        index = await build_movie_index(
            movie_library(_SPINE_ROWS),
            _FakePlexBrokenSweep(),  # type: ignore[arg-type]
            degrade=reasons.append,
        )
        assert reasons and "couldn't read your Plex libraries" in reasons[0]
        assert index.by_rating_key[100].ids.empty  # nothing enriched
        res = identity.resolve_movie(
            ids=identity.ExternalIds(), title="Example", year=2020, file_basename=None, index=index
        )
        assert res.rating_key == 100  # the backstop still works

    async def test_no_plex_client_means_no_enrichment_and_no_degrade(self) -> None:
        """A movie-only deployment with no Plex configured: no ids, no degrade for the
        sweep (it was already un-executable, since a real reap refuses without Plex)."""
        reasons: list[str] = []
        index = await build_movie_index(
            movie_library(_SPINE_ROWS),
            None,
            degrade=reasons.append,
        )
        assert reasons == []
        assert index.by_rating_key[100].ids.empty


class TestRetiredSpineRows:
    """A rating key the Tautulli cache still lists that Plex no longer has.

    It reaches the index carrying a title and a year and nothing else, which is exactly
    the shape tier 3 binds on, so it can attach a live file to an item that is gone.
    """

    #: The phantom: still in Tautulli's listing, absent from the sweep. Its title and year
    #: are the *arr's, which is what lets it win the title tier.
    _RETIRED: ClassVar[dict[str, object]] = {
        "rating_key": 100,
        "title": "Example",
        "year": 2020,
        "added_at": "1500000000",
    }
    #: The item Plex actually holds for that file. A different title, so only its file name
    #: or its ids can reach it.
    _REAL = identity.PlexItem(
        rating_key=300,
        title="Example Alternate Cut",
        year=1996,
        added_at=from_epoch("1700000000"),
        ids=identity.ExternalIds.of(tmdb=3003),
        file_basename="example (2020).mkv",
    )

    async def test_a_row_plex_no_longer_has_cannot_bind_a_live_file(self) -> None:
        """The condemn-side risk. With the *arr's file renamed, nothing reaches the real row,
        so title+year binding the file to the retired key would read as matched, report
        watchers as Known(0), and anchor dormancy on the phantom's stale added-at. Unmatched
        is the right answer, and it keeps the file."""
        reasons: list[str] = []
        index = await build_movie_index(
            movie_library([self._RETIRED]),
            _FakePlexSweep({300: self._REAL}),  # type: ignore[arg-type]
            degrade=reasons.append,
        )
        assert 100 not in index.by_rating_key
        res = identity.resolve_movie(
            ids=identity.ExternalIds.of(tmdb=9009),  # names nothing in Plex
            title="Example",
            year=2020,
            file_basename="Example (2020) Bluray-1080p.mkv",  # the *arr renamed it
            index=index,
        )
        assert res.rating_key is None
        assert res.status is identity.MatchStatus.UNMATCHED
        assert reasons == []  # one retired row is an ordinary stale cache, not a failure

    async def test_a_row_plex_no_longer_has_cannot_veto_a_good_bind(self) -> None:
        """The keep-side reach, and the one an operator sees: the file name named the real
        row, the phantom's title named itself, the two disagreed and the item abstained as
        'more than one thing in your Plex' over a library holding exactly one copy."""
        index = await build_movie_index(
            movie_library([self._RETIRED]),
            _FakePlexSweep({300: self._REAL}),  # type: ignore[arg-type]
            degrade=lambda _r: None,
        )
        res = identity.resolve_movie(
            ids=identity.ExternalIds.of(tmdb=9009),
            title="Example",
            year=2020,
            file_basename="example (2020).mkv",  # still names the real row
            index=index,
        )
        assert res.rating_key == 300
        assert res.status is identity.MatchStatus.MATCHED

    async def test_a_failed_sweep_retires_nothing(self) -> None:
        """A sweep that raised returns the same empty map a genuinely empty library does, so
        reading it as "Plex has none of these" would retire the whole library on a read that
        never happened. Instead the library stays as it was, and the snapshot degrades."""
        reasons: list[str] = []
        index = await build_movie_index(
            movie_library([self._RETIRED]),
            _FakePlexBrokenSweep(),  # type: ignore[arg-type]
            degrade=reasons.append,
        )
        assert 100 in index.by_rating_key
        assert reasons and "couldn't read your Plex libraries" in reasons[0]

    async def test_no_plex_configured_retires_nothing(self) -> None:
        """The other silent-empty case. Nothing was swept because nothing was asked."""
        reasons: list[str] = []
        index = await build_movie_index(
            movie_library([self._RETIRED]),
            None,
            degrade=reasons.append,
        )
        assert 100 in index.by_rating_key
        assert reasons == []

    async def test_the_show_index_retires_the_same_rows(self) -> None:
        """``build_tv_index`` is a thin wrapper over the same builder, so the prune reaches
        shows for free. The resolver above it still needs its own check, though: a show
        binds on tvdb and a folder name, never a file size, so it has one less corroborator
        between a phantom and a live series than a movie does. Both arms are checked here,
        on the show ladder."""
        retired_show = {
            "rating_key": 100,
            "title": "Example Show",
            "year": 2020,
            "added_at": "1500000000",
        }
        real_show = identity.PlexItem(
            rating_key=300,
            title="Example Show Under Another Name",
            year=2020,
            added_at=from_epoch("1700000000"),
            ids=identity.ExternalIds.of(tvdb=7007),
            file_basename="example show",
        )
        index = await build_tv_index(
            show_library([retired_show]),
            _FakePlexSweep({300: real_show}),  # type: ignore[arg-type]
            degrade=lambda _r: None,
        )
        assert 100 not in index.by_rating_key

        # The folder still names the real show: it binds, where the phantom's title would
        # have contradicted it and abstained.
        bound = identity.resolve_show(
            ids=identity.ExternalIds.of(tvdb=9009),  # names nothing in Plex
            title="Example Show",
            year=2020,
            file_basename="Example Show",
            index=index,
        )
        assert (bound.rating_key, bound.status) == (300, identity.MatchStatus.MATCHED)

        # Sonarr's folder was renamed, so nothing reaches the real show. Unmatched keeps
        # every season. Binding the phantom instead would hand them all a dead rating key.
        stranded = identity.resolve_show(
            ids=identity.ExternalIds.of(tvdb=9009),
            title="Example Show",
            year=2020,
            file_basename="Example Show (2020) [tvdb-9009]",
            index=index,
        )
        assert stranded.rating_key is None
        assert stranded.status is identity.MatchStatus.UNMATCHED

    @pytest.mark.parametrize(
        ("total", "retired", "degrades"),
        [
            (400, 41, True),  # past both bounds: floor 20, share 40
            (400, 40, False),  # past the floor, exactly AT the share
            (100, 15, False),  # past the share (10), under the floor
            (1000, 50, False),  # past the floor, well under the share (100)
            (400, 400, True),  # a section the sweep never walked
        ],
    )
    async def test_a_large_gap_degrades_and_a_small_one_does_not(
        self, total: int, retired: int, degrades: bool
    ) -> None:
        """A stale cache retires a handful. A library the sweep never walked retires all of
        it, and every item in it would resolve unmatched with nothing saying why. Both
        bounds must be passed, so a small library cannot degrade on one retired row."""
        rows = [
            {"rating_key": k, "title": f"Example {k}", "year": 2020, "added_at": "1500000000"}
            for k in range(total)
        ]
        swept = {
            k: identity.PlexItem(
                rating_key=k,
                title=f"Example {k}",
                year=2020,
                added_at=from_epoch("1700000000"),
                ids=identity.ExternalIds.of(tmdb=5000 + k),
            )
            for k in range(retired, total)
        }
        reasons: list[str] = []
        index = await build_movie_index(
            movie_library(rows),
            _FakePlexSweep(swept),  # type: ignore[arg-type]
            degrade=reasons.append,
        )
        assert len(index.by_rating_key) == total - retired
        gap = [r for r in reasons if "no longer in Plex" in r]
        assert bool(gap) is degrades
        if degrades:
            assert str(retired) in gap[0]

    async def test_one_library_vanishing_whole_degrades_beside_a_healthy_one(self) -> None:
        """The case the share exists for, and the one a single overall share cannot see.

        A small library the sweep cannot reach (a restricted share, a section removed and
        re-created under a new key) retires whole, while a large healthy one keeps the
        overall ratio down: 150 of 2,150 is past the floor but under 10% of the total, so
        one global share reports nothing and every item in that library silently resolves
        unmatched. Bucketed per section, the vanished library is 100% of its own spine and
        the scan says so.
        """
        healthy = [
            {"rating_key": k, "title": f"Example {k}", "year": 2020, "added_at": "1500000000"}
            for k in range(2000)
        ]
        vanished = [
            {"rating_key": k, "title": f"Example {k}", "year": 2020, "added_at": "1500000000"}
            for k in range(5000, 5150)
        ]
        # Only the healthy section's rows come back from the sweep.
        swept = {
            k: identity.PlexItem(
                rating_key=k,
                title=f"Example {k}",
                year=2020,
                added_at=from_epoch("1700000000"),
                ids=identity.ExternalIds.of(tmdb=5000 + k),
            )
            for k in range(2000)
        }
        reasons: list[str] = []
        index = await build_movie_index(
            FakeTautulli(sections={1: healthy, 2: vanished}),
            _FakePlexSweep(swept),  # type: ignore[arg-type]
            degrade=reasons.append,
        )
        assert len(index.by_rating_key) == 2000
        gap = [r for r in reasons if "no longer in Plex" in r]
        assert gap, "a library that vanished whole must be announced, not averaged away"
        assert "150" in gap[0]
        # The bound it slips past when the share is measured over every section at once.
        assert _RETIRED_DEGRADE_SHARE * (len(healthy) + len(vanished)) > 150

    async def test_a_stale_row_in_each_of_many_libraries_does_not_degrade(self) -> None:
        """The other side, so the per-section share cannot pass by degrading on anything.

        A handful of rows lagging in each of several libraries is an ordinary stale cache,
        not a scope mismatch. Each section is well under its own share, so nothing fires
        even though the total clears the floor.
        """
        sections: dict[int, list[dict[str, object]]] = {}
        swept: dict[int, identity.PlexItem] = {}
        for section in range(6):
            base = section * 1000
            rows = [
                {
                    "rating_key": base + k,
                    "title": f"Example {base + k}",
                    "year": 2020,
                    "added_at": "1500000000",
                }
                for k in range(200)
            ]
            sections[section + 1] = rows
            # Five stale rows per section: 5 of 200 is under the 10% share, 30 total is
            # over the floor of 20.
            for k in range(5, 200):
                swept[base + k] = identity.PlexItem(
                    rating_key=base + k,
                    title=f"Example {base + k}",
                    year=2020,
                    added_at=from_epoch("1700000000"),
                    ids=identity.ExternalIds.of(tmdb=90000 + base + k),
                )
        reasons: list[str] = []
        await build_movie_index(
            FakeTautulli(sections=sections),
            _FakePlexSweep(swept),  # type: ignore[arg-type]
            degrade=reasons.append,
        )
        assert _RETIRED_DEGRADE_FLOOR < 30, "the total must clear the floor, or this proves nothing"
        assert [r for r in reasons if "no longer in Plex" in r] == []


# ---------------------------------------------------------------------------
# A cache database, for everything below that reads the watch mirror or the lists.
# ---------------------------------------------------------------------------


@pytest.fixture
async def cache_engine(tmp_path: Path) -> AsyncIterator[AsyncEngine]:
    eng = create_engine(Settings(data_dir=tmp_path, secret_key="k"))
    await history_sync.ensure_schema(eng)  # the watch_event table the fold reads
    yield eng
    await eng.dispose()


# ---------------------------------------------------------------------------
# Merged listings: one file listed twice in Plex, plays split across the rating keys.
# ---------------------------------------------------------------------------


async def _play_event(
    engine: AsyncEngine, row_id: int, rating_key: int, user_id: int, when: datetime
) -> None:
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO watch_event (row_id, rating_key, user_id, watched_at, "
                "watched_status, percent_complete, media_type) "
                "VALUES (:id, :rk, :user, :at, 1.0, 100, 'movie')"
            ),
            {"id": row_id, "rk": rating_key, "user": user_id, "at": int(when.timestamp())},
        )


class TestMergedWatchStatsFold:
    """The fold reads a merged group's plays as a UNION onto the canonical key: exact
    distinct watchers (one person through two listings counts once), the latest play
    through any listing, and nothing else's stats touched."""

    async def test_the_group_union_lands_on_the_canonical_key(
        self, cache_engine: AsyncEngine
    ) -> None:
        old = NOW - timedelta(days=400)  # outside the 365-day window
        recent = NOW - timedelta(days=10)
        await _play_event(cache_engine, 1, 100, 1, old)  # user 1, canonical listing
        await _play_event(cache_engine, 2, 200, 1, recent)  # user 1 again, other listing
        await _play_event(cache_engine, 3, 200, 2, recent)  # user 2, other listing only
        await _play_event(cache_engine, 4, 200, 9, old)  # user 9, old, other listing
        await _play_event(cache_engine, 5, 300, 3, recent)  # an unrelated item

        last_played, window, ever = await _watch_stats(
            cache_engine, rating_keys={100, 300}, window_days=365
        )
        assert ever.get(100) == 1  # before the fold: the canonical key alone

        await _fold_merged_watch_stats(
            cache_engine,
            groups={100: (100, 200)},
            window_days=365,
            last_played=last_played,
            watchers_window=window,
            watchers_all_time=ever,
        )
        assert last_played[100] == from_epoch(int(recent.timestamp()))
        assert window[100] == 2  # users 1 and 2, since user 9's play is outside the window
        assert ever[100] == 3  # user 1 counted once despite playing both listings
        # The unrelated item's stats are untouched.
        assert ever.get(300) == 1

    async def test_plays_only_under_the_other_listing_still_count(
        self, cache_engine: AsyncEngine
    ) -> None:
        """Every play sits under the file's other listing. Without the fold the canonical
        key would read "never watched", the exact under-count that condemns."""
        recent = NOW - timedelta(days=5)
        await _play_event(cache_engine, 1, 200, 7, recent)

        last_played, window, ever = await _watch_stats(
            cache_engine, rating_keys={100}, window_days=365
        )
        assert 100 not in last_played

        await _fold_merged_watch_stats(
            cache_engine,
            groups={100: (100, 200)},
            window_days=365,
            last_played=last_played,
            watchers_window=window,
            watchers_all_time=ever,
        )
        assert last_played[100] == from_epoch(int(recent.timestamp()))
        assert window[100] == 1
        assert ever[100] == 1

    async def test_a_library_sized_group_set_does_not_blow_the_variable_ceiling(
        self, cache_engine: AsyncEngine
    ) -> None:
        """The IN was expanded whole, unlike every sibling that batches at 500.

        Past SQLite's bound-variable ceiling the statement raises OperationalError, which
        is not an IntegrationError and so is caught nowhere: the whole scan dies rather
        than one fold being skipped. Enough merged listings to need that many binds is a
        big library, not an exotic one.
        """
        # 36,000 keys: past SQLite's bound-variable ceiling (32,766 on a current build,
        # 999 on an older one) and past the 500-key chunk many times over.
        groups = {rk: (rk, rk + 100_000) for rk in range(1_000, 19_000)}
        await _play_event(cache_engine, 1, 101_000, 4, NOW - timedelta(days=3))

        last_played, window, ever = await _watch_stats(
            cache_engine, rating_keys=set(groups), window_days=365
        )
        await _fold_merged_watch_stats(
            cache_engine,
            groups=groups,
            window_days=365,
            last_played=last_played,
            watchers_window=window,
            watchers_all_time=ever,
        )

        # And it is not merely "did not raise": the play under the second listing of the
        # group in the middle of the run still folded onto its canonical key.
        assert ever[1_000] == 1
        assert window[1_000] == 1

    async def test_a_group_with_no_plays_changes_nothing(self, cache_engine: AsyncEngine) -> None:
        last_played, window, ever = await _watch_stats(
            cache_engine, rating_keys={100}, window_days=365
        )
        await _fold_merged_watch_stats(
            cache_engine,
            groups={100: (100, 200)},
            window_days=365,
            last_played=last_played,
            watchers_window=window,
            watchers_all_time=ever,
        )
        assert 100 not in last_played
        assert 100 not in window
        assert 100 not in ever


# ---------------------------------------------------------------------------
# The grace clock restarts when a rescued item is re-condemned after a gap.
# ---------------------------------------------------------------------------


@pytest.fixture
async def session(tmp_path: Path) -> AsyncIterator[AsyncSession]:
    settings = Settings(data_dir=tmp_path, secret_key="test-key")
    engine = create_engine(settings)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory: async_sessionmaker[AsyncSession] = create_session_factory(engine)
    async with factory() as s:
        yield s
    await engine.dispose()


class TestTheGraceClockRestartsOnReCondemnation:
    async def test_a_return_after_a_long_gap_restarts_the_clock(
        self, session: AsyncSession
    ) -> None:
        """Condemned long ago, rescued, then re-condemned a full dormancy period later.
        The item must serve a fresh grace window, or it drops straight into ``ready`` with
        no countdown and no Leaving Soon warning, and is reaped with zero grace."""
        long_ago = NOW - timedelta(days=400)
        session.add(
            FirstFlagged(
                media_key="radarr:1:1",
                first_flagged_at=long_ago,
                # Last seen condemned 400 days ago. It left the condemned set for far
                # longer than a grace window and has now returned.
                last_seen_condemned_at=long_ago,
            )
        )
        await session.flush()

        await record_first_flagged_bulk(session, ["radarr:1:1"], NOW, grace_days=14)

        row = await session.get(FirstFlagged, "radarr:1:1")
        assert row is not None
        assert row.first_flagged_at == NOW  # the clock restarted
        assert row.last_seen_condemned_at == NOW

    async def test_an_uninterrupted_condemn_does_not_reset_the_clock(
        self, session: AsyncSession
    ) -> None:
        """The other direction. An item that stayed condemned, or missed only a snapshot
        or two to a transient outage, keeps its original clock, so it can age out."""
        started = NOW - timedelta(days=5)
        session.add(
            FirstFlagged(
                media_key="radarr:1:2",
                first_flagged_at=started,
                last_seen_condemned_at=NOW - timedelta(days=1),  # gap well under the window
            )
        )
        await session.flush()

        await record_first_flagged_bulk(session, ["radarr:1:2"], NOW, grace_days=14)

        row = await session.get(FirstFlagged, "radarr:1:2")
        assert row is not None
        assert row.first_flagged_at == started  # NOT moved
        assert row.last_seen_condemned_at == NOW  # only the last-seen bumped


class TestTheGraceRecorderToleratesACompetingWriter:
    async def test_an_already_present_key_does_not_abort_the_insert(
        self, session: AsyncSession
    ) -> None:
        """Two writers racing on one key: the loser's insert must do nothing (the winner's
        row already says "condemned around now"), never raise a primary-key collision out
        of a scan. Simulated by inserting a row the recorder's read predates."""
        earlier = NOW - timedelta(hours=1)
        session.add(
            FirstFlagged(
                media_key="radarr:1:9", first_flagged_at=earlier, last_seen_condemned_at=earlier
            )
        )
        await session.flush()

        await _insert_first_flags(
            session,
            [
                FirstFlagged(
                    media_key="radarr:1:9", first_flagged_at=NOW, last_seen_condemned_at=NOW
                )
            ],
        )

        row = await session.get(FirstFlagged, "radarr:1:9")
        assert row is not None
        assert row.first_flagged_at == earlier  # the winner's clock survives


# ---------------------------------------------------------------------------
# A failed whitelist sync with an empty keep-list degrades the snapshot.
# ---------------------------------------------------------------------------


class TestProtectionSyncDegradations:
    async def test_a_failed_whitelist_with_no_members_degrades(
        self, cache_engine: AsyncEngine
    ) -> None:
        """A whitelist that failed to sync and has no membership fails open, the worst
        direction. It must degrade the snapshot so no reap runs against an empty keep-list."""
        await _seed_list(cache_engine, slug="reaper-keep", kind="whitelist", members=0)
        reasons = await protection_sync_degradations(cache_engine, {"reaper-keep": "error: boom"})
        assert any("reaper-keep" in r for r in reasons)

    async def test_a_failed_whitelist_with_a_fresh_copy_does_not_degrade(
        self, cache_engine: AsyncEngine
    ) -> None:
        """The atomic swap preserved prior membership and the last successful sync is
        recent, so the keep-list still protects. A transient failure need not stop the
        scan."""
        await _seed_list(
            cache_engine,
            slug="reaper-keep",
            kind="whitelist",
            members=3,
            last_synced_at=int(NOW.timestamp()) - 3600,  # an hour ago
        )
        reasons = await protection_sync_degradations(cache_engine, {"reaper-keep": "error: boom"})
        assert reasons == []

    async def test_a_failed_whitelist_whose_row_is_disabled_degrades(
        self, cache_engine: AsyncEngine
    ) -> None:
        """A retired slug keeps its members, so counting rows alone is not enough to know it
        still protects.

        ``lists.retire_absent`` disables a superseded slug with ``enabled = 0`` and leaves
        its membership rows in place. The gate reads membership through
        ``load_membership_index``, which joins ``WHERE l.enabled = 1``, so those rows protect
        nothing. A slug carries the operator's match mode, so flipping keep-tags from "any"
        to "all" and back retires and revives slugs in normal use. A failed sync landing in
        that window must not let a disabled list vouch for it and skip the degrade.
        """
        await _seed_list(
            cache_engine,
            slug="reaper-keep",
            kind="whitelist",
            members=50,
            last_synced_at=int(NOW.timestamp()) - 3600,  # fresh, so staleness is not the cause
            enabled=0,
        )
        reasons = await protection_sync_degradations(cache_engine, {"reaper-keep": "error: boom"})
        assert any("reaper-keep" in r for r in reasons)

    async def test_a_failed_whitelist_stale_beyond_the_bound_degrades(
        self, cache_engine: AsyncEngine
    ) -> None:
        """A stale-but-populated list protects only within the bound. Past it, every title
        keep-tagged since the last successful sync has been unprotected for days, so the
        snapshot degrades until a sync succeeds."""
        await _seed_list(
            cache_engine,
            slug="reaper-keep",
            kind="whitelist",
            members=3,
            last_synced_at=int((NOW - timedelta(days=3)).timestamp()),
        )
        reasons = await protection_sync_degradations(cache_engine, {"reaper-keep": "error: boom"})
        assert any("reaper-keep" in r and "hours old" in r for r in reasons)

    async def test_a_failed_whitelist_with_members_but_no_sync_record_degrades(
        self, cache_engine: AsyncEngine
    ) -> None:
        """Members with no last-successful-sync stamp: recency cannot be confirmed, so it
        is not assumed (fail closed)."""
        await _seed_list(
            cache_engine, slug="reaper-keep", kind="whitelist", members=3, last_synced_at=None
        )
        reasons = await protection_sync_degradations(cache_engine, {"reaper-keep": "error: boom"})
        assert any("reaper-keep" in r for r in reasons)

    async def test_a_failed_curated_hard_list_with_no_members_degrades(
        self, cache_engine: AsyncEngine
    ) -> None:
        """A curated list feeds a HARD gate: its members are PROTECTED outright, so an empty
        one fails OPEN exactly as an empty whitelist does. The gate protects nothing and a
        scan could reap the very titles the list exists to save. The first sync of the IMDb
        Top 250 failing must degrade the snapshot, not pass silently."""
        await _seed_list(cache_engine, slug="imdb-top-250", kind="curated", members=0)
        reasons = await protection_sync_degradations(cache_engine, {"imdb-top-250": "error: 503"})
        assert any("imdb-top-250" in r for r in reasons)

    async def test_a_failed_curated_hard_list_with_members_does_not_degrade_on_staleness(
        self, cache_engine: AsyncEngine
    ) -> None:
        """Recency is a keep-list concern. A curated external list churns slowly and its
        stored copy keeps protecting, so a stale-but-populated one does not degrade: only
        the whitelist applies the staleness bound."""
        await _seed_list(
            cache_engine,
            slug="imdb-top-250",
            kind="curated",
            members=250,
            last_synced_at=int((NOW - timedelta(days=30)).timestamp()),
        )
        reasons = await protection_sync_degradations(cache_engine, {"imdb-top-250": "error: 503"})
        assert reasons == []

    async def test_a_failed_soft_list_does_not_degrade(self, cache_engine: AsyncEngine) -> None:
        """A soft-mode list only feeds a scoring nudge, so an empty one costs no protection,
        and losing it does not make the snapshot un-executable. The mode decides this:
        degrade on mode=hard, and only on mode=hard."""
        await _seed_list(cache_engine, slug="soft-list", kind="curated", mode="soft", members=0)
        reasons = await protection_sync_degradations(cache_engine, {"soft-list": "error: 503"})
        assert reasons == []

    async def test_a_successful_sync_never_degrades(self, cache_engine: AsyncEngine) -> None:
        reasons = await protection_sync_degradations(cache_engine, {"reaper-keep": 12})
        assert reasons == []


class TestAKeepListNothingCheckedAtAll:
    """The gap the three checks above cannot see. They walk ``synced``, so a list nothing
    built a provider for is absent from it entirely.

    Unlinking Plex is the way in. ``DELETE /api/settings/plex`` drops only the server row, so
    the collection and watchlist definitions stay enabled, no provider is built for either,
    and their stored membership goes on protecting and goes on aging with nothing bounding
    it. A keep list unreadable for months read exactly like one checked minutes ago.
    """

    async def test_an_unchecked_keep_list_past_the_bound_degrades(
        self, cache_engine: AsyncEngine
    ) -> None:
        """The removed-server case. Its slug is not in ``synced`` at all, which is the
        difference from every test above."""
        await _seed_list(
            cache_engine,
            slug="plex-collection:1",
            kind="whitelist",
            members=40,
            last_synced_at=int((NOW - timedelta(days=3)).timestamp()),
        )

        reasons = await protection_sync_degradations(cache_engine, {})

        assert any("plex-collection:1" in r and "hours old" in r for r in reasons), reasons

    async def test_an_unchecked_keep_list_inside_the_bound_does_not_degrade(
        self, cache_engine: AsyncEngine
    ) -> None:
        """The control, and the reason the bound exists at all. A scan that runs between two
        nightly list refreshes finds every list unchecked by itself but recently checked by
        the job, and must not degrade on that."""
        await _seed_list(
            cache_engine,
            slug="plex-collection:1",
            kind="whitelist",
            members=40,
            last_synced_at=int(NOW.timestamp()) - 3600,
        )

        assert await protection_sync_degradations(cache_engine, {}) == []

    async def test_an_unchecked_keep_list_with_no_sync_record_degrades(
        self, cache_engine: AsyncEngine
    ) -> None:
        await _seed_list(
            cache_engine, slug="plex-collection:1", kind="whitelist", members=0, last_synced_at=None
        )

        reasons = await protection_sync_degradations(cache_engine, {})

        assert any("plex-collection:1" in r for r in reasons), reasons

    async def test_an_install_that_never_synced_the_list_at_all_stays_clean(
        self, cache_engine: AsyncEngine
    ) -> None:
        """The case this must not fire on, and the reason it keys on a stored row.

        The seed migration puts an enabled Plex collection definition on every install,
        including one that has never linked Plex. Nothing ever synced it, so no
        ``protection_list`` row exists, and an operator who does not use Plex must not meet
        an incomplete-scan notice on every scan forever.
        """
        await lists.ensure_schema(cache_engine)

        assert await protection_sync_degradations(cache_engine, {}) == []

    async def test_a_retired_row_does_not_degrade(self, cache_engine: AsyncEngine) -> None:
        """``retire_absent`` disables a superseded slug and keeps its members, so a row the
        operator switched off or replaced would otherwise degrade every scan from then on
        with no way to clear it."""
        await _seed_list(
            cache_engine,
            slug="plex-collection:1",
            kind="whitelist",
            members=40,
            last_synced_at=int((NOW - timedelta(days=30)).timestamp()),
            enabled=0,
        )

        assert await protection_sync_degradations(cache_engine, {}) == []

    async def test_an_unchecked_curated_list_does_not_degrade_on_staleness(
        self, cache_engine: AsyncEngine
    ) -> None:
        """The same split as the failed-sync walk above. A curated external list churns
        slowly and keeps protecting from its stored copy, so the recency bound is a
        keep-list rule and this stays one line of policy rather than two."""
        await _seed_list(
            cache_engine,
            slug="imdb-top-250",
            kind="curated",
            members=250,
            last_synced_at=int((NOW - timedelta(days=30)).timestamp()),
        )

        assert await protection_sync_degradations(cache_engine, {}) == []

    async def test_a_list_checked_this_pass_is_left_to_the_walk_above(
        self, cache_engine: AsyncEngine
    ) -> None:
        """A successful check is a successful check, whatever the stored stamp said before
        it, and a failed one is already reported by name. Either way this must not add a
        second sentence about the same list."""
        await _seed_list(
            cache_engine,
            slug="plex-collection:1",
            kind="whitelist",
            members=40,
            last_synced_at=int((NOW - timedelta(days=30)).timestamp()),
        )

        assert await protection_sync_degradations(cache_engine, {"plex-collection:1": 40}) == []


async def _seed_list(
    engine: AsyncEngine,
    *,
    slug: str,
    kind: str,
    members: int,
    mode: str = "hard",
    last_synced_at: int | None = None,
    enabled: int = 1,
) -> None:
    """Write a protection_list row (with mode + kind) and ``members`` membership rows.

    ``enabled`` defaults to 1, matching the column's own ``NOT NULL DEFAULT 1``. Pass 0 for
    a slug ``retire_absent`` has superseded: those keep their membership rows on purpose.
    """
    await lists.ensure_schema(engine)
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO protection_list "
                "(slug, display_name, mode, kind, weight, enabled, last_error, last_synced_at) "
                "VALUES (:slug, :slug, :mode, :kind, 0, :enabled, 'error: boom', :synced) "
                "ON CONFLICT(slug) DO UPDATE SET mode = :mode, kind = :kind, "
                "enabled = :enabled, last_synced_at = :synced"
            ),
            {
                "slug": slug,
                "kind": kind,
                "mode": mode,
                "synced": last_synced_at,
                "enabled": enabled,
            },
        )
        for i in range(members):
            await conn.execute(
                text(
                    "INSERT INTO protection_list_item "
                    "(slug, media_type, imdb_id, tmdb_id, tvdb_id, title, rank) "
                    "VALUES (:slug, 'movie', :imdb, NULL, NULL, :title, NULL)"
                ),
                {"slug": slug, "imdb": f"tt{i:07d}", "title": f"Film {i}"},
            )
