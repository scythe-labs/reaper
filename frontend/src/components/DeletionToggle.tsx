// SPDX-License-Identifier: AGPL-3.0-or-later
//
// The deletion switch: whether Reaper is allowed to remove anything at all.
//
// Extracted from the Settings safety panel so the Policy workspace can carry it as the
// last step of the decision pipeline. Turning deletion ON always takes the admin
// password; turning it OFF never does, because the off direction can only make Reaper
// safer. This is safety UI, so it never renders nothing: while the state is unknown it
// says so, in the amber "we could not look" tone, never in a way that reads as safe.

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { api } from "../api";

export function DeletionToggle() {
  const queryClient = useQueryClient();
  const { data, isLoading, isError } = useQuery({ queryKey: ["safety"], queryFn: api.safety });
  const [confirming, setConfirming] = useState(false);
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);

  const refresh = () => {
    void queryClient.invalidateQueries({ queryKey: ["safety"] });
    void queryClient.invalidateQueries({ queryKey: ["health"] });
  };
  const toggle = useMutation({
    mutationFn: (vars: { enabled: boolean; password?: string }) =>
      api.setDeletion(vars.enabled, vars.password),
    onSuccess: () => {
      setPassword("");
      setConfirming(false);
      setError(null);
      refresh();
    },
    onError: (e: Error) => setError(e.message),
  });

  if (isLoading) {
    return <p className="muted">Checking whether deletion is on…</p>;
  }
  // Unknown must never read as safe: say it plainly, in amber.
  if (isError || !data) {
    return (
      <p className="notice notice-warn">
        Reaper couldn't confirm whether deletion is on. Until it can, treat it as on.
      </p>
    );
  }

  const on = data.destructive_enabled;

  return (
    <>
      {/* No `data.note` here: it says where to turn deletion on, and this IS that place. */}
      <div className={`safety-state ${on ? "armed" : "safe"}`}>
        <span className="banner-dot" aria-hidden="true" />
        <div>
          <strong>{on ? "Deletion is on." : "Deletion is off. Reaper is read-only."}</strong>
        </div>
      </div>

      <div className="safety-row">
        <div>
          <strong>{on ? "Turn deletion off" : "Turn deletion on"}</strong>
          <p className="help">
            {on
              ? "Puts Reaper back to read-only right away."
              : "Reaper will be allowed to delete media you approve. You'll still review and approve every run."}
          </p>
        </div>
        {on ? (
          <button className="ghost danger" onClick={() => toggle.mutate({ enabled: false })}>
            Turn off
          </button>
        ) : !data.has_password ? (
          <span className="muted">Set an admin password first, in Settings → Security.</span>
        ) : confirming ? (
          <form
            className="pw-form"
            onSubmit={(e) => {
              e.preventDefault();
              setError(null);
              toggle.mutate({ enabled: true, password });
            }}
          >
            <input
              type="password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              placeholder="admin password"
              autoComplete="current-password"
              autoFocus
            />
            <button type="submit" className="primary sm" disabled={!password || toggle.isPending}>
              Confirm
            </button>
            <button type="button" className="ghost sm" onClick={() => setConfirming(false)}>
              Cancel
            </button>
          </form>
        ) : (
          <button className="primary" onClick={() => setConfirming(true)}>
            Turn on…
          </button>
        )}
      </div>
      {error && <p className="notice notice-error">{error}</p>}
    </>
  );
}
