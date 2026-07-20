// SPDX-License-Identifier: AGPL-3.0-or-later
//
// The grace panel: what is counting down, and what has cleared.
//
// Reaper never deletes the moment it condemns. A condemned item waits out a grace window
// first, and this is where the owner watches that clock -- and stops it. Canceling a
// grace is "spare it" (the same manual whitelist the review queue uses), so an item the
// owner rescues here is protected everywhere, at once.
//
// Nothing here deletes. The "ready" list is what *would* be eligible once the reap step
// exists; it is shown so the countdown is honest, not so anything fires on its own.

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, type GraceItem, type LeavingSoonResult } from "../api";
import { count, itemBytes, totalBytes } from "../format";
import { useOverrideMutations } from "../useOverrideMutations";

function ItemRow({
  item,
  onCancel,
  onOpenReasons,
  pending,
}: {
  item: GraceItem;
  onCancel: () => void;
  onOpenReasons: (candidateId: number) => void;
  pending: boolean;
}) {
  return (
    <li>
      {/* "Why is this one counting down?" is the question this list raises, so the title
          is the way to the answer instead of a name to retype in the review queue. */}
      <button
        className="link grace-title"
        title="Why this one is counting down"
        onClick={() => onOpenReasons(item.candidate_id)}
      >
        {item.title}
      </button>
      <span className="grace-size muted">{itemBytes(item.size_bytes)}</span>
      <span className="grace-remaining">
        {item.in_grace ? `${item.days_remaining}d left` : "ready"}
      </span>
      {/* Twenty rows can render twenty "cancel" buttons, so the label names the file each
          one keeps. */}
      <button
        className="link"
        aria-label={`Cancel the countdown and keep ${item.title}`}
        disabled={pending}
        onClick={onCancel}
      >
        cancel
      </button>
    </li>
  );
}

/** The one line that reports what an update pass did, honestly: what changed, whether
 *  the writes landed, and where to allow them when they didn't. */
function UpdateResult({
  result,
  onGoToPlexSettings,
}: {
  result: LeavingSoonResult;
  onGoToPlexSettings: () => void;
}) {
  if (!result.applied) {
    return (
      <span className="muted">
        {count(result.added_count)} to add, {count(result.cleared_count)} to clear · preview
        only. Reaper is read-only, so nothing is written in Plex.{" "}
        <button className="link" onClick={onGoToPlexSettings}>
          Allow updates while read-only in Settings → Plex
        </button>
        , or turn deletion on.
      </span>
    );
  }
  return (
    <span className="muted">
      {count(result.added_count)} added, {count(result.cleared_count)} cleared · shelves
      updated in Plex{result.notified && " · Discord notified"}. Updates on its own after
      every scan.
    </span>
  );
}

export function GracePanel({
  onGoToPlexSettings,
  onOpenReasons,
}: {
  onGoToPlexSettings: () => void;
  /** Open one item's reasoning on the review screen. */
  onOpenReasons: (candidateId: number) => void;
}) {
  const queryClient = useQueryClient();
  const { refresh } = useOverrideMutations();
  const { data, isPending, isError } = useQuery({ queryKey: ["grace"], queryFn: api.grace });

  // Which of the three bar states to show: off (with the way to turn it on), or the
  // update button. The settings are read fresh here so flipping the switch in Settings
  // is reflected the next time this panel renders.
  const settings = useQuery({
    queryKey: ["leaving-soon-settings"],
    queryFn: api.leavingSoonSettings,
  });

  const cancel = useMutation({
    mutationFn: (mediaKey: string) => api.spare(mediaKey),
    // Sparing here is the same decision the review queue makes, so it refreshes the same
    // caches: useOverrideMutations' refresh() owns that list (grace included).
    onSuccess: refresh,
  });

  const mark = useMutation({
    mutationFn: api.syncLeavingSoon,
    onSuccess: () => void queryClient.invalidateQueries({ queryKey: ["leaving-soon-settings"] }),
  });

  if (isPending) return <p className="muted">Loading…</p>;
  // An unreadable grace list must never look like an empty one: items may be counting
  // down, or ready, and simply not shown. Say so, in the amber "we could not look" tone.
  if (isError || !data) {
    return (
      <p className="notice notice-warn">
        Couldn't load the grace countdown. Items may still be waiting or ready to reap,
        Reaper just can't show them right now. Reload to try again.
      </p>
    );
  }

  const soonest = data.in_grace[0]?.days_remaining;

  return (
    <details className="grace" open={data.ready_count > 0}>
      <summary>
        Grace &amp; countdown ·{" "}
        <strong>{count(data.in_grace_count)}</strong>
        <span className="muted">
          {" "}
          in grace ({totalBytes(data.total_bytes_in_grace, data.unknown_size_in_grace)})
        </span>
        {data.in_grace_count > 0 && soonest !== undefined && (
          <span className="muted">, soonest clears in {soonest}d</span>
        )}
        {data.ready_count > 0 && (
          <>
            {" · "}
            <strong className="grace-ready">{count(data.ready_count)}</strong>
            <span className="muted">
              {" "}
              ready ({totalBytes(data.total_bytes_ready, data.unknown_size_ready)})
            </span>
          </>
        )}
      </summary>

      <p className="blurb">
        A condemned item waits out {data.grace_days} days before it is eligible to be
        reaped. Cancel does more than stop the clock: it spares the file, so it leaves the
        queue and the plan too.
      </p>

      <div className="leaving-soon-bar">
        {settings.isPending ? (
          <span className="muted">Checking the Leaving Soon settings…</span>
        ) : settings.isError || !settings.data ? (
          <span className="muted">
            Couldn't check the Leaving Soon settings. The countdown above is unaffected.
          </span>
        ) : !settings.data.enabled ? (
          <span className="muted">
            Leaving Soon in Plex is off, so nobody browsing your libraries gets a warning
            there.{" "}
            <button className="link" onClick={onGoToPlexSettings}>
              Turn it on in Settings → Plex.
            </button>
          </span>
        ) : (
          <>
            <button disabled={mark.isPending} onClick={() => mark.mutate()}>
              {mark.isPending ? "Updating…" : "Update “Leaving Soon” now"}
            </button>
            {mark.data ? (
              <UpdateResult result={mark.data} onGoToPlexSettings={onGoToPlexSettings} />
            ) : (
              <span className="muted">Updates on its own after every scan.</span>
            )}
          </>
        )}
        {mark.data && mark.data.problems.length > 0 && (
          <span className="ls-problems">{mark.data.problems.join(" · ")}</span>
        )}
      </div>
      {mark.error && (
        <p className="notice notice-error">The shelves didn't update: {mark.error.message}</p>
      )}

      {data.ready.length > 0 && (
        <>
          <h3 className="grace-heading grace-ready">Grace has cleared</h3>
          <ul className="grace-list">
            {data.ready.map((item) => (
              <ItemRow
                key={item.media_key}
                item={item}
                pending={cancel.isPending}
                onCancel={() => cancel.mutate(item.media_key)}
                onOpenReasons={onOpenReasons}
              />
            ))}
          </ul>
        </>
      )}

      {data.in_grace.length > 0 && (
        <>
          <h3 className="grace-heading">Counting down</h3>
          <ul className="grace-list">
            {data.in_grace.slice(0, 20).map((item) => (
              <ItemRow
                key={item.media_key}
                item={item}
                pending={cancel.isPending}
                onCancel={() => cancel.mutate(item.media_key)}
                onOpenReasons={onOpenReasons}
              />
            ))}
          </ul>
          {data.in_grace_count > 20 && (
            <p className="muted">…and {count(data.in_grace_count - 20)} more counting down.</p>
          )}
        </>
      )}

      {cancel.error && (
        <p className="notice notice-error">
          The item wasn't spared: {cancel.error.message}
        </p>
      )}
    </details>
  );
}
