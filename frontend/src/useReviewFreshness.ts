// SPDX-License-Identifier: AGPL-3.0-or-later
//
// When a scan finishes while the review queue is open, the list underneath goes stale: the
// backend now serves a newer snapshot, but the mounted query still shows the old one. This
// decides what to do about it, once, at the moment the newer snapshot appears:
//   - if the reviewer is mid-review (scrolled in, a panel open, a decision in flight), hold
//     their place and raise a nudge they can act on;
//   - otherwise refresh quietly.
//
// Everything is derived from one fact, "the list is behind the newest scan", so any refetch
// that pulls the latest snapshot (a hand override, a filter change, Show latest) clears the
// whole thing on its own. There is no dismiss flag the app has to remember to reset, so
// nothing here can be left stale by mistake.

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
  /** Pull the latest snapshot into the view (invalidate the review queries). Returns a promise
   *  that settles when those refetches have finished, which is how a failed silent refresh is
   *  told from one still in flight (see the note on the second effect below). */
  onSilentRefresh: () => void | Promise<unknown>;
  /** Fired once when a silent refresh's swap has actually landed (behind -> caught up), so
   *  the caller confirms it (a toast) only after it happened, never at issuance. */
  onSilentCaughtUp?: () => void;
}): ReviewFreshness {
  const { viewSnapshotId, latestSnapshotId } = opts;
  const behind =
    latestSnapshotId !== null && viewSnapshotId !== null && latestSnapshotId > viewSnapshotId;

  const [nudging, setNudging] = useState(false);
  const [dismissed, setDismissed] = useState(false);

  // The decision inputs are read lazily through refs so the effect depends only on the
  // snapshot ids. A scroll (which changes isBusy) must not re-run it and re-decide a scan we
  // already handled, and a fresh onSilentRefresh identity each render must not either.
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
  // a raised nudge.
  const awaitingSilent = useRef(false);
  // `settledId` is which silent refresh has settled, the signal the effect below waits for.
  // Watching the list's `isFetching` flag instead would only work if a render happened to be
  // committed during the fetch: a refetch that rejects in the same microtask flush never
  // shows up as fetching at all, so the nudge would silently never come. What the refresh
  // returns settles exactly when its work is done, whatever the render schedule.
  //
  // Each refresh carries its own id, and the effect below acts only on the settle of the one
  // still in flight. A bare monotonic tick compared against 0 would only protect the first
  // refresh of a mount: on the second, the tick would already be past 0, so the nudge would
  // go up in the very commit that issued the refresh, and since that arm also clears
  // `awaitingSilent`, the caught-up confirmation could never fire again for the rest of the
  // session.
  const silentId = useRef(0);
  const [settledId, setSettledId] = useState(0);

  useEffect(() => {
    if (!behind) {
      // The list caught up (any refetch to the latest snapshot). If a silent refresh was
      // waiting on exactly this, confirm the swap now, only now. Then reset for the next
      // scan.
      if (awaitingSilent.current) {
        awaitingSilent.current = false;
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
      const id = ++silentId.current;
      void Promise.resolve(refreshRef.current())
        .then(() => setSettledId(id))
        // A refresh that rejects has settled too, and settled without catching up, so it
        // takes the same path: the effect below sees the list still behind and raises the
        // nudge, rather than leaving the reviewer on a stale list with nothing to press.
        // Today's caller cannot reject (query-core swallows each query's failure), but the
        // declared return type invites one that can.
        .catch(() => setSettledId(id));
    }
  }, [behind, latestSnapshotId]);

  // A silent refresh whose refetch finished while the list is still behind never caught up (a
  // network blip): raise the nudge rather than leave the list silently stale. The reviewer is
  // idle, so the bar is theirs to act on.
  //
  // `behind` is a dependency as well as a guard, so a swap landing after this fires still
  // reaches the effect above and clears the nudge. It does not also confirm the catch-up:
  // this effect has already set `awaitingSilent` false, which is the flag that gate reads, so
  // the toast is skipped for a refresh whose settle is observed a commit before its swap.
  // Clearing the nudge is what matters, so the operator is not left staring at a stale one;
  // missing that one toast is the acceptable cost.
  useEffect(() => {
    // Only the refresh currently in flight: an id that has not caught up to the one just issued
    // is either the starting value or an earlier refresh's, and neither says anything about this
    // one. Superseded settles are dropped for free, since a newer scan bumps the id again.
    if (!awaitingSilent.current || settledId !== silentId.current) return;
    if (behind) {
      awaitingSilent.current = false;
      setNudging(true);
    }
  }, [settledId, behind]);

  return {
    showBar: nudging && behind && !dismissed,
    showMarker: nudging && behind && dismissed,
    dismiss: () => setDismissed(true),
  };
}
