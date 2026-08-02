// SPDX-License-Identifier: AGPL-3.0-or-later
//
// The deletion switch: whether Reaper is allowed to remove anything at all.
//
// Extracted from the Settings safety panel so the Policy workspace can carry it as the
// last step of the decision pipeline. Turning deletion ON always takes the admin
// password; turning it OFF never does, because the off direction can only make Reaper
// safer. This is safety UI, so it never renders nothing: while the state is unknown it
// says so, in the amber "we could not look" tone, never in a way that reads as safe.
//
// **It says which way it went, out loud.** It used to signal the outcome the way the rest of
// the app did: the form unmounted and a `<strong>` in an unfocused subtree rewrote itself.
// Focus fell to `<body>`, nothing was announced, and an operator driving by ear could not
// tell whether they had just armed the app to delete their library (#170). Both directions
// now `announce()`, and focus goes back to the button that opened the form on both of its
// exits -- the confirm and the cancel -- because the form takes the focused element with it
// when it goes.

import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useEffect, useRef, useState } from "react";
import { announce } from "../announce";
import { api } from "../api";
import { useSafety } from "../useSafety";
import { Notice } from "./Notice";

export function DeletionToggle() {
  const queryClient = useQueryClient();
  const { data, isLoading, isError } = useSafety();
  const [confirming, setConfirming] = useState(false);
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  // Whichever button is holding the row's action slot -- "Turn on…" before, "Turn off" after.
  // One ref for both, because it is one control position: what the operator wants back is the
  // place they left, and after arming that place says the opposite thing.
  const rowButtonRef = useRef<HTMLButtonElement>(null);
  // Set when the password form is about to go and focus has to follow it somewhere. Read in the
  // effect below rather than acted on inline, because the button does not exist until the commit
  // that unmounts the form has landed, and `.focus()` on a node that is not there is a silent
  // no-op -- the exact shape that leaves focus on `<body>`.
  const returnToRow = useRef(false);
  useEffect(() => {
    if (!returnToRow.current || confirming) return;
    returnToRow.current = false;
    rowButtonRef.current?.focus();
  }, [confirming]);
  /** Close the password form and put the operator back on the button that opened it. */
  const closeForm = () => {
    returnToRow.current = true;
    setConfirming(false);
    setPassword("");
  };

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
    onSuccess: (_data, vars) => {
      setPassword("");
      setConfirming(false);
      setError(null);
      // The same two sentences the state block above shows, said out loud (rule 144: one fact,
      // and the visible copy is a few lines up, so keep them reading alike). Announced from the
      // settled mutation, never at issuance (rule 85) -- this is the switch that decides whether
      // Reaper may delete, so a premature "on" would be the worst possible thing to be wrong
      // about.
      announce(vars.enabled ? "Deletion is on." : "Deletion is off. Reaper is read-only.");
      // Arming happens from the password form, which unmounts on success and takes the focused
      // Confirm button with it. Without this, focus lands on `<body>` and the next Tab restarts
      // at the top of the page.
      if (vars.enabled) returnToRow.current = true;
      refresh();
    },
    onError: (e: Error) => setError(e.message),
  });

  if (isLoading) {
    return <p className="muted">Checking whether deletion is on…</p>;
  }
  // Unknown must never read as safe: say it plainly, in amber, and still offer OFF -- no
  // password, no prior state, the one direction that can only make Reaper safer -- so the
  // operator who wants read-only RIGHT NOW has something to click. ON stays gone: it takes a
  // password, and offering it against a state we could not read would be arming on a guess.
  if (isError || !data) {
    return (
      <>
        {/* Only a toggle that turned deletion OFF may claim read-only here. `isSuccess` alone
            records that a toggle landed, not which way it went: after a successful ARM whose
            follow-up read then failed, it painted the green "read-only" block over a host that
            was armed -- the one always-visible surface saying the opposite of the truth, in the
            reassuring direction (rule 144). `SafetyBanner` (App.tsx) already reads this state
            correctly, and the two sit on screen together (rule 72). */}
        {toggle.isSuccess && toggle.variables?.enabled === false ? (
          <div className="safety-state safe">
            <span className="banner-dot" aria-hidden="true" />
            <div>
              <strong>Deletion is off. Reaper is read-only.</strong>
            </div>
          </div>
        ) : (
          <Notice tone="warn">
            Reaper couldn't confirm whether deletion is on. Until it can, treat it as on.
          </Notice>
        )}
        <div className="safety-row">
          <div>
            <strong>Turn deletion off</strong>
            <p className="help">
              Puts Reaper back to read-only right away. Safe to press either way: if it was already
              off, nothing changes.
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
        {error && <Notice tone="error">{error}</Notice>}
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
          <button
            ref={rowButtonRef}
            className="ghost danger"
            onClick={() => toggle.mutate({ enabled: false })}
          >
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
            {/* Cancel drops the typed password with the form. Closing the form alone left the
                admin password sitting in component state for as long as this panel stayed
                mounted, and refilled the field the next time it was opened (S-5). */}
            <button type="button" className="ghost sm" onClick={closeForm}>
              Cancel
            </button>
          </form>
        ) : (
          <button ref={rowButtonRef} className="primary" onClick={() => setConfirming(true)}>
            Turn on…
          </button>
        )}
      </div>
      {error && <Notice tone="error">{error}</Notice>}
    </>
  );
}
