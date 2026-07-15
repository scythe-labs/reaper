// SPDX-License-Identifier: AGPL-3.0-or-later
//
// The caps: how much a single reap may do, and how long the grace period lasts.
//
// These live with the plan because they govern it -- a plan over the item or byte cap
// ABORTS (never truncates), and that abort is what the owner sees on the Reap screen. The
// domain enforces the invariants (a per-run cap may not exceed the rolling 30-day cap;
// grace is at least a week), so an impossible combination comes back as a reason to read,
// not a silent clamp.

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { api, type ProfileSettings } from "../api";
import { bytes } from "../format";
import { QuantityInput, SIZE_UNITS, TIME_UNITS } from "./QuantityInput";

export function CapsEditor() {
  const queryClient = useQueryClient();
  const { data: saved } = useQuery({ queryKey: ["profile"], queryFn: api.profile });
  const [draft, setDraft] = useState<ProfileSettings | null>(null);

  useEffect(() => {
    if (saved && draft === null) setDraft(saved);
  }, [saved, draft]);

  const save = useMutation({
    mutationFn: (s: ProfileSettings) => api.saveProfile(s),
    onSuccess: (s) => {
      setDraft(s);
      void queryClient.invalidateQueries({ queryKey: ["profile"] });
    },
  });

  if (!draft) return null;

  const dirty = saved && JSON.stringify(draft) !== JSON.stringify(saved);
  const set = (patch: Partial<ProfileSettings>) => setDraft({ ...draft, ...patch });

  return (
    <details className="caps">
      <summary>
        Caps &amp; grace — <span className="muted">a run may delete at most </span>
        <strong>{draft.max_items_per_run}</strong>
        <span className="muted"> titles / </span>
        <strong>{bytes(draft.max_bytes_per_run)}</strong>
        <span className="muted">, grace </span>
        <strong>{draft.grace_days} days</strong>
      </summary>

      <div className="caps-grid">
        <label>
          <span>Most titles per run</span>
          <input
            type="number"
            min={1}
            value={draft.max_items_per_run}
            onChange={(e) => set({ max_items_per_run: Number(e.target.value) || 1 })}
          />
        </label>
        <label>
          <span>Most space per run</span>
          <QuantityInput
            value={draft.max_bytes_per_run}
            units={SIZE_UNITS}
            onChange={(v) => set({ max_bytes_per_run: v })}
          />
        </label>
        <label>
          <span>Most titles per month</span>
          <input
            type="number"
            min={1}
            value={draft.max_items_per_30d}
            onChange={(e) => set({ max_items_per_30d: Number(e.target.value) || 1 })}
          />
        </label>
        <label>
          <span>Most space per month</span>
          <QuantityInput
            value={draft.max_bytes_per_30d}
            units={SIZE_UNITS}
            onChange={(v) => set({ max_bytes_per_30d: v })}
          />
        </label>
        <label>
          <span>Grace period</span>
          <QuantityInput
            value={draft.grace_days}
            units={TIME_UNITS}
            min={7}
            onChange={(v) => set({ grace_days: v })}
          />
        </label>
        <label className="toggle">
          <input
            type="checkbox"
            checked={draft.require_approval}
            onChange={(e) => set({ require_approval: e.target.checked })}
          />
          <span>Ask me before every run deletes anything</span>
        </label>
      </div>

      <p className="blurb">
        A run over a cap aborts entirely — it never deletes the part that fits, because
        which items die must not depend on sort order.
      </p>

      <div className="editor-actions">
        <button className="primary" disabled={!dirty || save.isPending} onClick={() => save.mutate(draft)}>
          {save.isPending ? "Saving…" : dirty ? "Save caps" : "Saved"}
        </button>
        {save.error && <p className="error">{save.error.message}</p>}
      </div>
    </details>
  );
}
