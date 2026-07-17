# SPDX-License-Identifier: AGPL-3.0-or-later
"""Reading Sonarr season statistics correctly.

This module exists because of one field that is a trap. The behaviour below was
confirmed against a production Sonarr instance holding a four-figure number of
series.

``seasons[].statistics.episodeCount`` is **not** "how many episodes this season
has". It is Sonarr's *download intent*: roughly, episodes that have aired and are
monitored, plus any episode that already has a file. Three consequences, each
observed on a large fraction of seasons:

* ``episodeCount > episodeFileCount`` wherever Sonarr *wants* episodes it does not
  have -- a long-running show mid-download reports more episodes than exist on disk.
* A monitored season whose episodes have not aired yet reports ``episodeCount=0``.
* **An unmonitored season reports ``episodeCount=0`` even when it is complete**, with
  a non-zero ``totalEpisodeCount`` and every episode long since aired. On a mature
  library this is the *majority* case, because finished shows get unmonitored.

So a "keep the last 2 seasons" rule built on ``episodeCount`` would misjudge which
seasons hold bytes, and -- far worse -- would read a complete, unmonitored season as
empty and conclude there is nothing there to protect.

**For any decision about what exists on disk, use ``episodeFileCount`` and
``sizeOnDisk``. They are the only fields that describe reality.**

``totalEpisodeCount`` is the honest "how long is this season" figure -- it counts
every episode including unaired -- and is what season *ranking* should use.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class SeasonStats:
    """The subset of Sonarr's season statistics that describes reality."""

    season_number: int
    monitored: bool

    # On disk. The only fields safe to reason about for deletion.
    episode_file_count: int
    size_on_disk: int

    # How long the season is, including unaired episodes. Safe for ranking.
    total_episode_count: int

    # Sonarr's download intent. Retained for display only -- see the module
    # docstring. NEVER branch on this to decide what to delete.
    wanted_episode_count: int

    @property
    def has_content(self) -> bool:
        """Does this season occupy disk?

        Note this is deliberately *not* ``wanted_episode_count > 0``: a monitored
        season with nothing aired reports zero wanted episodes while still holding
        files, and an unmonitored season reports zero while holding gigabytes.
        """
        return self.episode_file_count > 0

    @property
    def is_incomplete(self) -> bool:
        """Sonarr wants episodes it does not have.

        Worth surfacing rather than acting on: pruning a season mid-download is a
        good way to make Reaper and Sonarr fight each other.
        """
        return self.wanted_episode_count > self.episode_file_count


def parse_season_stats(season: dict[str, Any]) -> SeasonStats | None:
    """Read one entry of a Sonarr series' ``seasons`` array."""
    stats = season.get("statistics")
    if not isinstance(stats, dict):
        return None

    return SeasonStats(
        season_number=int(season.get("seasonNumber", 0)),
        monitored=bool(season.get("monitored", False)),
        episode_file_count=int(stats.get("episodeFileCount") or 0),
        size_on_disk=int(stats.get("sizeOnDisk") or 0),
        total_episode_count=int(stats.get("totalEpisodeCount") or 0),
        wanted_episode_count=int(stats.get("episodeCount") or 0),
    )


def rank_seasons(seasons: list[SeasonStats], *, include_specials: bool = False) -> dict[int, int]:
    """Rank seasons newest-first: rank 1 is the most recent season *with files on disk*.

    This is what a "keep the last N seasons" rule counts against.

    Specials (season 0) are excluded by default. They are not part of the run of
    the show, they are frequently the oldest-numbered and newest-dated content at
    once, and letting them occupy a rank slot silently shifts every real season by
    one -- so "keep the last 2" would keep specials plus one real season.

    Seasons without files are excluded for the same reason: an announced-but-
    undownloaded next season would otherwise take rank 1 and shift every real season
    down one, so "keep the last 2" would protect the empty shell plus one real season
    and leave the season the rule meant to keep prunable. There is nothing to keep in
    a season with no files, so it never occupies a keep-slot.
    """
    real = [s for s in seasons if s.has_content and (include_specials or s.season_number > 0)]
    ordered = sorted(real, key=lambda s: s.season_number, reverse=True)
    return {s.season_number: i + 1 for i, s in enumerate(ordered)}
