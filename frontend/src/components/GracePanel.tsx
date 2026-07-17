// SPDX-License-Identifier: AGPL-3.0-or-later
//
// The grace panel: what is counting down, and what has cleared.
//
// Reaper never deletes the moment it condemns. A condemned item waits out a grace window
// first, and this is where the owner watches that clock -- and stops it. Cancelling a
// grace is "spare it" (the same manual whitelist the review queue uses), so an item the
// owner rescues here is protected everywhere, at once.
//
// Nothing here deletes. The "ready" list is what *would* be eligible once the reap step
// exists; it is shown so the countdown is honest, not so anything fires on its own.

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, type GraceItem } from "../api";
import { bytes, count } from "../format";

function ItemRow({ item, onCancel, pending }: { item: GraceItem; onCancel: () => void; pending: boolean }) {
  return (
    <li>
      <span className="grace-title">{item.title}</span>
      <span className="grace-size muted">{bytes(item.size_bytes)}</span>
      <span className="grace-remaining">
        {item.in_grace ? `${item.days_remaining}d left` : "ready"}
      </span>
      <button className="link" disabled={pending} onClick={onCancel}>
        cancel
      </button>
    </li>
  );
}

export function GracePanel() {
  const queryClient = useQueryClient();
  const { data, isPending, isError } = useQuery({ queryKey: ["grace"], queryFn: api.grace });

  const cancel = useMutation({
    mutationFn: (mediaKey: string) => api.spare(mediaKey),
    onSuccess: () => {
      // Sparing removes the item from grace and protects it in the queue and the plan.
      void queryClient.invalidateQueries({ queryKey: ["grace"] });
      void queryClient.invalidateQueries({ queryKey: ["whitelist"] });
      void queryClient.invalidateQueries({ queryKey: ["candidates"] });
    },
  });

  const mark = useMutation({ mutationFn: api.syncLeavingSoon });

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
        <span className="muted"> in grace ({bytes(data.total_bytes_in_grace)})</span>
        {data.in_grace_count > 0 && soonest !== undefined && (
          <span className="muted">, soonest clears in {soonest}d</span>
        )}
        {data.ready_count > 0 && (
          <>
            {" · "}
            <strong className="grace-ready">{count(data.ready_count)}</strong>
            <span className="muted"> ready ({bytes(data.total_bytes_ready)})</span>
          </>
        )}
      </summary>

      <p className="blurb">
        A condemned item waits out {data.grace_days} days before it is eligible to be
        reaped. Cancel resets nothing else. It spares the file, so it leaves the queue and
        the plan too.
      </p>

      <div className="leaving-soon-bar">
        <button disabled={mark.isPending} onClick={() => mark.mutate()}>
          {mark.isPending ? "Marking…" : "Mark “Leaving Soon” in Plex"}
        </button>
        {mark.data && (
          <span className="muted">
            {mark.data.to_add_count} to mark, {mark.data.to_remove_count} to clear
            {mark.data.notified && " · Discord notified"}
            {mark.data.applied
              ? " · label written in Plex"
              : " · preview only (enable deletion, or set REAPER_ALLOW_UNARMED_LEAVING_SOON)"}
          </span>
        )}
        {mark.error && <span className="error">{mark.error.message}</span>}
      </div>

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
              />
            ))}
          </ul>
          {data.in_grace_count > 20 && (
            <p className="muted">…and {count(data.in_grace_count - 20)} more counting down.</p>
          )}
        </>
      )}

      {cancel.error && <p className="error">{cancel.error.message}</p>}
    </details>
  );
}
