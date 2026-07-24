// SPDX-License-Identifier: AGPL-3.0-or-later
//
// When a scan finishes while the review queue is open, the list underneath goes stale: the
// backend now serves a newer snapshot, but the mounted query still shows the old one. This
// decides what to do about it, once, at the moment the newer snapshot appears:
//   - if the reviewer is mid-review (scrolled in, a panel open, a decision in flight), hold
//     their place and raise a nudge they can act on;
//   - otherwise refresh quietly.
//
// Everything is derived from one fact -- "the list is behind the newest scan" -- so ANY
// refetch that pulls the latest snapshot (a hand override, a filter change, Show latest)
// clears the whole thing on its own. There is no dismiss flag the app has to remember to
// reset, which is the bug the derived shape exists to prevent.

import { useEffect, useRef, useState } from "react";

export interface ReviewFreshness {
  /** Show the full nudge bar: a newer scan landed mid-review and has not been dismissed. */
  showBar: boolean;
  /** Show the slim "one scan behind" marker: dismissed, but the list is still behind. */
  showMarker: boolean;
  /** Collapse the bar to the marker. Deferring, not hiding: the marker stays until the
   *  list catches up, so the reviewer is never silently left on an old scan. */
  dismiss: () => void;
}

export function useReviewFreshness(opts: {
  /** The snapshot the list is currently showing (from the candidates page header). */
  viewSnapshotId: number | null;
  /** The newest completed scan's snapshot, from the polled scan status. */
  latestSnapshotId: number | null;
  /** Whether the reviewer is mid-review right now. Read only at the instant a newer scan
   *  appears, so a later scroll never re-decides a scan already handled. */
  isBusy: () => boolean;
  /** Pull the latest snapshot into the view (invalidate the review queries). */
  onSilentRefresh: () => void;
  /** Whether the review list is fetching right now. React Query keeps the old data on a refetch
   *  error, so error flags stay clear; a fetch that went and settled while the list is still
   *  behind is how we tell a failed silent refresh from one still in flight, so the hook can
   *  nudge instead of leaving the list silently stale (PR-5). */
  refreshFetching?: boolean;
  /** Fired once when a SILENT refresh's swap has actually landed (behind -> caught up), so the
   *  caller confirms it (a toast) only after it happened, never at issuance (rule 85). */
  onSilentCaughtUp?: () => void;
}): ReviewFreshness {
  const { viewSnapshotId, latestSnapshotId, refreshFetching = false } = opts;
  const behind =
    latestSnapshotId !== null &&
    viewSnapshotId !== null &&
    latestSnapshotId > viewSnapshotId;

  const [nudging, setNudging] = useState(false);
  const [dismissed, setDismissed] = useState(false);

  // The decision inputs are read lazily through refs so the effect depends only on the
  // snapshot ids -- a scroll (which changes isBusy) must not re-run it and re-decide a scan
  // we already handled, and a fresh onSilentRefresh identity each render must not either.
  const busyRef = useRef(opts.isBusy);
  busyRef.current = opts.isBusy;
  const refreshRef = useRef(opts.onSilentRefresh);
  refreshRef.current = opts.onSilentRefresh;
  const caughtUpRef = useRef(opts.onSilentCaughtUp);
  caughtUpRef.current = opts.onSilentCaughtUp;
  // The scan we have already acted on, so we decide exactly once per newer snapshot.
  const handled = useRef<number | null>(null);
  // A silent refresh is in flight and its swap has not landed yet: we still owe either a
  // caught-up confirmation (behind -> false) or, if its refetch settles without catching up,
  // a raised nudge. `sawFetch` records that the refetch actually started, so the settle check
  // below can't misfire in the gap between issuing the refresh and the fetch beginning.
  const awaitingSilent = useRef(false);
  const sawFetch = useRef(false);

  useEffect(() => {
    if (!behind) {
      // The list caught up (any refetch to the latest snapshot). If a silent refresh was waiting
      // on exactly this, confirm the swap now -- only now (rule 85). Then reset for the next scan.
      if (awaitingSilent.current) {
        awaitingSilent.current = false;
        sawFetch.current = false;
        caughtUpRef.current?.();
      }
      setNudging(false);
      setDismissed(false);
      handled.current = null;
      return;
    }
    if (handled.current === latestSnapshotId) return; // already decided for this scan
    handled.current = latestSnapshotId;
    if (busyRef.current()) {
      setNudging(true);
    } else {
      awaitingSilent.current = true;
      sawFetch.current = false;
      refreshRef.current();
    }
  }, [behind, latestSnapshotId]);

  // A silent refresh whose refetch went and finished while the list is STILL behind never caught
  // up (a network blip): raise the nudge rather than leave the list silently stale -- the
  // reviewer is idle, so the bar is theirs to act on (PR-5).
  useEffect(() => {
    if (!awaitingSilent.current) return;
    if (refreshFetching) {
      sawFetch.current = true;
      return;
    }
    if (sawFetch.current && behind) {
      awaitingSilent.current = false;
      sawFetch.current = false;
      setNudging(true);
    }
  }, [refreshFetching, behind]);

  return {
    showBar: nudging && behind && !dismissed,
    showMarker: nudging && behind && dismissed,
    dismiss: () => setDismissed(true),
  };
}
