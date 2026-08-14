# SPDX-License-Identifier: AGPL-3.0-or-later
"""Re-measure the four numbers #553's design rests on, against any real ``reaper.db``.

``docs/RETURN_PLAN.md`` picks a cooling-off default from measurements taken on one library,
and ``docs/LEARNINGS.md`` records them. Both say the measurement re-runs anywhere with
snapshot history. This is what makes that true, and rule 68 is why it is committed rather
than pasted into a transcript.

Read-only, and it prints ratios and spans only: no title, no id, no path. Point it at a
copy if you would rather not have a reader open the live database at all.

    uv run python scripts/return_signal_measure.py data/reaper.db

What each number decides:

1. **Snapshot span and cadence.** The window everything below is measured over, and the
   gap between scans. The largest gap is the one that matters: a last sighting records
   when Reaper looked, not when a title left, so a cooling-off bar under the largest gap
   can be cleared by a pause alone.
2. **Titles that left and came back.** The base rate for a return happening at all. It was
   zero on the measured library, which is what says an accidental return is not a thing
   that needs telling apart from a deliberate one.
3. **Rating-key churn, and how fast.** The detector's noise floor. The spans are the
   finding: mechanical churn resolves in hours, a regret takes as long as somebody takes
   to notice, and the cooling-off bar lives in the gap between those two populations.
4. **External ids carrying more than one \\*arr entry.** Two copies of one title, each
   bound to a different Plex listing. Above zero, a ledger keyed on the id alone thrashes
   between them forever, which is why the ledger holds a set.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

SPAN = """
SELECT COUNT(*), MIN(created_at), MAX(created_at) FROM snapshot
"""

CADENCE = """
SELECT AVG(d), MAX(d) FROM (
  SELECT created_at - LAG(created_at) OVER (ORDER BY created_at) AS d FROM snapshot
) WHERE d IS NOT NULL
"""

# Presence gaps. A title seen, then missing, then seen again -- keyed on the external id for
# movies and on media_key for seasons, since a season prune leaves the Sonarr series row
# intact and its media_key with it.
GAPS = """
WITH snaps AS (SELECT id, ROW_NUMBER() OVER (ORDER BY created_at) rn FROM snapshot),
     present AS (
       SELECT {key} AS k, s.rn
       FROM candidate c JOIN snaps s ON s.id = c.snapshot_id
       WHERE c.media_type = ? AND {key} IS NOT NULL
       GROUP BY 1, 2
     ),
     span AS (SELECT k, MIN(rn) lo, MAX(rn) hi, COUNT(*) seen FROM present GROUP BY k)
SELECT COUNT(*), SUM(CASE WHEN seen < (hi - lo + 1) THEN 1 ELSE 0 END) FROM span
"""

# Rating-key churn for a STABLE *arr entry, and the span of each change. Keyed on media_key
# on purpose: keyed on the external id, two copies of one title read as churn that is not
# there, which is finding 4 below.
CHURN = """
WITH snaps AS (SELECT id, created_at, ROW_NUMBER() OVER (ORDER BY created_at) rn FROM snapshot),
     seen AS (
       SELECT c.media_key mk, c.plex_rating_key rk, s.rn, s.created_at
       FROM candidate c JOIN snaps s ON s.id = c.snapshot_id
       WHERE c.media_type = ? AND c.plex_rating_key IS NOT NULL
     ),
     edges AS (
       SELECT mk, rk, created_at,
              LAG(rk) OVER (PARTITION BY mk ORDER BY rn) prev_rk,
              LAG(created_at) OVER (PARTITION BY mk ORDER BY rn) prev_at
       FROM seen
     )
SELECT (created_at - prev_at) / 3600.0
FROM edges WHERE prev_rk IS NOT NULL AND rk <> prev_rk ORDER BY 1
"""

TRACKED = """
SELECT COUNT(DISTINCT media_key) FROM candidate
WHERE media_type = ? AND plex_rating_key IS NOT NULL
"""

# One ledger key, two *arr entries, in a single scan.
#
# The key has to be the one the ledger would use, not merely the external id. A show's
# tvdb_id is shared by every season it has, so grouping seasons on it alone counts the
# season structure and reports a duplicate for every multi-season show. The season's key is
# the show id plus the season number, which is the trailing segment of ``media_key``
# (``sonarr:{instance}:{series}:{season}``) -- ``rtrim`` strips the digits to find where it
# starts, since SQLite has no split.
SEASON_KEY = "c.tvdb_id || ':' || substr(c.media_key, length(rtrim(c.media_key, '0123456789')) + 1)"

DUPLICATES = """
SELECT COUNT(*) FROM (
  SELECT {key} AS k FROM candidate c
  WHERE c.snapshot_id = (SELECT id FROM snapshot ORDER BY created_at DESC LIMIT 1)
    AND c.media_type = ? AND {id} IS NOT NULL
  GROUP BY k HAVING COUNT(*) > 1
)
"""


def _ratio(part: int, whole: int) -> str:
    """``part`` of ``whole`` as "1 in N", which is the shape docs/ records."""
    if not whole or not part:
        return f"{part} of {whole:,}"
    return f"{part} of {whole:,}, about 1 in {round(whole / part):,}"


def main(db: Path) -> int:
    if not db.exists():
        print(f"no database at {db}", file=sys.stderr)
        return 1

    # Read-only URI: this runs against a live install, and it must not be the thing that
    # takes a write lock while a scan is waiting behind it.
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        scans, first, last = con.execute(SPAN).fetchone()
        if not scans or scans < 2:
            print(f"{scans or 0} snapshots. Needs at least 2 to measure anything.")
            return 1
        mean_gap, max_gap = con.execute(CADENCE).fetchone()

        print(f"\n1. {scans} scans over {(last - first) / 86400:.1f} days")
        print(f"   between scans: {mean_gap / 3600:.1f}h mean, {max_gap / 3600:.1f}h largest")
        print(f"   ⇒ a cooling-off bar under {max_gap / 86400:.1f} days is clearable by a pause")

        print("\n2. left the library and came back")
        for kind, key in (("movie", "c.tmdb_id"), ("season", "c.media_key")):
            tracked, gapped = con.execute(GAPS.format(key=key), (kind,)).fetchone()
            print(f"   {kind + 's:':9} {_ratio(gapped or 0, tracked)}")

        print("\n3. bound Plex rating key changed, *arr entry unchanged")
        for kind in ("movie", "season"):
            (tracked,) = con.execute(TRACKED, (kind,)).fetchone()
            spans = [row[0] for row in con.execute(CHURN, (kind,))]
            print(f"   {kind + 's:':9} {_ratio(len(spans), tracked)}")
            if spans:
                shown = ", ".join(f"{h:.1f}" for h in spans[:12])
                more = f", and {len(spans) - 12} more" if len(spans) > 12 else ""
                print(f"   {'':9} each change spanned, in hours: {shown}{more}")
                print(f"   {'':9} ⇒ slowest {max(spans) / 24:.1f} days; the bar clears it")

        print("\n4. one ledger key, two *arr entries, in the newest scan")
        for kind, key, col in (
            ("movie", "c.tmdb_id", "c.tmdb_id"),
            ("season", SEASON_KEY, "c.tvdb_id"),
        ):
            (dupes,) = con.execute(DUPLICATES.format(key=key, id=col), (kind,)).fetchone()
            note = " ⇒ the ledger must hold a set" if dupes else ""
            print(f"   {kind + 's:':9} {dupes:,} keys{note}")
        print()
    finally:
        con.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(Path(sys.argv[1] if len(sys.argv) > 1 else "data/reaper.db")))
