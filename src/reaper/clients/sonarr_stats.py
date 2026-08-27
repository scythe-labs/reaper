# SPDX-License-Identifier: AGPL-3.0-or-later
"""How to read Sonarr's season statistics without being misled by one field.

``seasons[].statistics.episodeCount`` counts Sonarr's download intent: episodes
that have aired and are monitored, plus any episode that already has a file. It is
easy to mistake for the number of episodes a season actually has, and treating it
that way causes three problems:

* A show mid-download can want more episodes than it has on disk, so
  ``episodeCount`` can be higher than ``episodeFileCount``.
* A monitored season with nothing aired yet reports ``episodeCount=0``.
* An unmonitored season reports ``episodeCount=0`` even when it is complete, with a
  non-zero ``totalEpisodeCount`` and every episode aired long ago. On a mature
  library, most finished seasons fall into this case, because finished shows get
  unmonitored.

A rule that keeps the last 2 seasons based on ``episodeCount`` would misjudge which
seasons hold files. Worse, it could read a complete, unmonitored season as empty.

Use ``episodeFileCount`` and ``sizeOnDisk`` to decide what is actually on disk.
``totalEpisodeCount`` counts every episode, aired or not, so season ranking should
use it instead.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class SeasonStats:
    """The subset of Sonarr's season statistics that describes reality."""

    season_number: int
    monitored: bool

    # On disk right now. Safe to use for a deletion decision.
    episode_file_count: int

    size_on_disk: int | None
    """Bytes the season's episode files hold, or ``None`` when Sonarr reported files but no
    size.

    Treat ``None`` as unknown, not zero. A season with ``episode_file_count > 0`` and no
    ``sizeOnDisk`` has an incomplete statistics payload, not an empty season. Reading it
    as ``0`` would score it as taking the least possible space, which would silently
    disable any "keep large files" rule. ``has_content`` checks the file count instead of
    this field, so "does it hold files" still works when the size cannot be read.

    This counts the episode files Sonarr tracks, the same files a season prune deletes,
    so the number matches what a delete actually frees. The movie side works
    differently: a movie delete removes the whole folder, while this counts only files.
    A season folder itself holds slightly more than the sum of its files, but the
    difference is sidecar files a prune leaves in place.

    The movie path applies the same rule; see ``services.snapshot._reported_size`` and
    ``tests/test_fact_layer_states.py``.
    """

    # Episode count including ones that have not aired yet. Safe to use for ranking.
    total_episode_count: int

    # Sonarr's download intent, kept only for display. See the module docstring.
    # Never use this to decide what to delete.
    wanted_episode_count: int

    @property
    def has_content(self) -> bool:
        """Does this season hold any files on disk?

        This checks ``episode_file_count``, not ``wanted_episode_count``: a monitored
        season with nothing aired yet reports zero wanted episodes while still holding
        files, and an unmonitored season reports zero while holding gigabytes of files.
        """
        return self.episode_file_count > 0

    @property
    def is_incomplete(self) -> bool:
        """Sonarr wants episodes it does not have yet.

        This is useful to show the operator, not to act on. Pruning a season
        mid-download would make Reaper delete what Sonarr is still trying to fetch.
        """
        return self.wanted_episode_count > self.episode_file_count


def _reported_size(raw: Any) -> int | None:
    """A size Sonarr reported, or ``None``. Both zero and a missing value count as ``None``."""
    return int(raw) if isinstance(raw, int | float) and raw > 0 else None


def parse_season_stats(season: dict[str, Any]) -> SeasonStats | None:
    """Read one entry of a Sonarr series' ``seasons`` array."""
    stats = season.get("statistics")
    if not isinstance(stats, dict):
        return None

    return SeasonStats(
        season_number=int(season.get("seasonNumber", 0)),
        monitored=bool(season.get("monitored", False)),
        episode_file_count=int(stats.get("episodeFileCount") or 0),
        # `or 0` here would turn a partial payload into an empty season. See size_on_disk.
        size_on_disk=_reported_size(stats.get("sizeOnDisk")),
        total_episode_count=int(stats.get("totalEpisodeCount") or 0),
        wanted_episode_count=int(stats.get("episodeCount") or 0),
    )


def rank_seasons(seasons: list[SeasonStats], *, include_specials: bool = False) -> dict[int, int]:
    """Rank seasons newest-first. Rank 1 is the most recent season that has files on disk.

    A "keep the last N seasons" rule counts against this rank.

    Specials (season 0) are excluded by default. They are not part of the show's normal
    run, and they can be both the oldest-numbered and the most recently added content at
    once. Giving them a rank would shift every real season down by one, so "keep the
    last 2" would keep specials plus only one real season.

    Seasons without files are excluded for the same reason. An announced season that has
    not downloaded yet would otherwise take rank 1 and shift every real season down one,
    so "keep the last 2" would protect an empty shell instead of the season the rule
    meant to keep. A season with no files has nothing to keep, so it never takes a rank
    slot.
    """
    real = [s for s in seasons if s.has_content and (include_specials or s.season_number > 0)]
    ordered = sorted(real, key=lambda s: s.season_number, reverse=True)
    return {s.season_number: i + 1 for i, s in enumerate(ordered)}
