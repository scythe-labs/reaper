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
}): ReviewFreshness {
  const { viewSnapshotId, latestSnapshotId } = opts;
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
  // The scan we have already acted on, so we decide exactly once per newer snapshot.
  const handled = useRef<number | null>(null);

  useEffect(() => {
    if (!behind) {
      // The list caught up (any refetch to the latest snapshot). Reset for the next scan.
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
      refreshRef.current();
    }
  }, [behind, latestSnapshotId]);

  return {
    showBar: nudging && behind && !dismissed,
    showMarker: nudging && behind && dismissed,
    dismiss: () => setDismissed(true),
  };
}
