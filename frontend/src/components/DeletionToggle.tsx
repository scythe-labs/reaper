// SPDX-License-Identifier: AGPL-3.0-or-later
//
// The deletion switch: whether Reaper is allowed to remove anything at all.
//
// Extracted from the Settings safety panel so the Policy workspace can carry it as the
// last step of the decision pipeline. Turning deletion ON always takes the admin
// password; turning it OFF never does, because the off direction can only make Reaper
// safer. This is safety UI, so it never renders nothing: while the state is unknown it
// says so, in the amber "we could not look" tone, never in a way that reads as safe.

import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { api } from "../api";
import { useSafety } from "../useSafety";

export function DeletionToggle() {
  const queryClient = useQueryClient();
  const { data, isLoading, isError } = useSafety();
  const [confirming, setConfirming] = useState(false);
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);

  // ["safety"] is the whole list: it is the one query that carries whether deletion is on,
  // and every surface that gates on it (this switch, the app banner, the Reap page's
  // Execute button) reads it through useSafety. There used to be a ["health"] line here
  // too, left behind by the health-based safety read App.tsx retired, naming a cache no
  // component subscribes to (B-35).
  const refresh = () => {
    void queryClient.invalidateQueries({ queryKey: ["safety"] });
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
  // Unknown must never read as safe: say it plainly, in amber -- and still offer the one
  // direction that can only make Reaper safer. Turning deletion OFF takes no password and
  // needs no prior state, so the operator who wants read-only RIGHT NOW must not be handed
  // advice to assume the worst and nothing to click. (Turning it ON is the direction that
  // stays gone: it takes a password, and offering it against a state we could not read would
  // be arming on a guess.)
  if (isError || !data) {
    return (
      <>
        {toggle.isSuccess ? (
          <div className="safety-state safe">
            <span className="banner-dot" aria-hidden="true" />
            <div>
              <strong>Deletion is off. Reaper is read-only.</strong>
            </div>
          </div>
        ) : (
          <p className="notice notice-warn">
            Reaper couldn't confirm whether deletion is on. Until it can, treat it as on.
          </p>
        )}
        <div className="safety-row">
          <div>
            <strong>Turn deletion off</strong>
            <p className="help">
              Puts Reaper back to read-only right away. Safe to press either way: if it was
              already off, nothing changes.
            </p>
          </div>
          <button
            className="ghost danger"
            disabled={toggle.isPending}
            onClick={() => toggle.mutate({ enabled: false })}
          >
            {toggle.isPending ? "Turning off…" : "Turn off"}
          </button>
        </div>
        {error && <p className="notice notice-error">{error}</p>}
      </>
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
            {/* The placeholder is a hint, not a name: it disappears the moment you type.
                The label names the field either way. */}
            <input
              type="password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              maxLength={128}
              placeholder="admin password"
              aria-label="Admin password"
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
