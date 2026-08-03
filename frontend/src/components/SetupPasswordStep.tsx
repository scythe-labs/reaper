// SPDX-License-Identifier: AGPL-3.0-or-later
//
// The wizard's first step: set the admin password.
//
// It is the only step with no Skip and no Back, because it is not really about the wizard.
// This one password is three things at once: the local account -- which, on a Plex-only
// install, does not otherwise exist at all, however firmly the login screen's own copy says
// Reaper "always keeps at least one" -- the credential that arms deletion, and the credential
// `restore/confirm` refuses without. Letting an operator past it leaves an install that
// cannot be armed, cannot be restored, and locks its owner out the first time plex.tv is
// unreachable.
//
// The typed password is handed up to the wizard on success. That is what lets the restore
// door on a later step confirm without asking for it a second time: the operator types it
// once, here, and the browser passes the same string on to `restore/confirm`, whose own
// password check is untouched. It lives in React state for the length of the flow and is
// never persisted -- the same exposure as the box it was typed into.

import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useState, type ReactNode } from "react";
import { announce } from "../announce";
import { api } from "../api";
import { Notice } from "./Notice";
import { MIN_ADMIN_PASSWORD } from "./Settings";
import { StepCard } from "./SetupStepper";

/** The step's one error region. Which box points at it varies with which complaint is live,
 *  so the id is named once for both ends of the association (rule 67) -- the same arrangement
 *  `AdminPasswordForm` uses, and for the same reason. */
const ERROR_ID = "setup-password-error";

export function SetupPasswordStep({
  onDone,
}: {
  /** Handed the password that was just set, so the flow can confirm a restore with it
   *  without asking again. */
  onDone: (password: string) => void;
}) {
  const queryClient = useQueryClient();
  const [pw, setPw] = useState("");
  const [confirm, setConfirm] = useState("");

  const save = useMutation({
    // No current password: this step only ever runs when none is set, which is exactly the
    // case the route lets through on a signed-in session alone.
    mutationFn: () => api.setAdminPassword(pw),
    onSuccess: async () => {
      // Settled before the step advances, not at issuance (rule 85): the next step reads the
      // setup status, and moving on before the refetch lands would show it the old answer and
      // bounce the operator straight back here.
      await queryClient.invalidateQueries({ queryKey: ["setup"] });
      await queryClient.invalidateQueries({ queryKey: ["safety"] });
      announce("Password saved.");
      onDone(pw);
    },
    // No onError: the failure renders from `save.error` below. "Saved" and "that didn't work"
    // must not look alike on the form that sets the key arming deletion.
  });

  const tooShort = pw.length > 0 && pw.length < MIN_ADMIN_PASSWORD;
  const mismatch = confirm.length > 0 && confirm !== pw;
  const valid = pw.length >= MIN_ADMIN_PASSWORD && confirm.length > 0 && confirm === pw;

  // Live validation explains why the button is off while typing; a failed submit reuses the
  // same region. Validation wins over a stale submit error, so the operator sees the thing
  // they can fix right now rather than the thing that went wrong a moment ago.
  const error: ReactNode = tooShort ? (
    <>
      Use at least {MIN_ADMIN_PASSWORD} characters. <b>{pw.length} so far.</b>
    </>
  ) : mismatch ? (
    "The two passwords don't match."
  ) : save.error ? (
    `That didn't save: ${save.error.message}`
  ) : null;

  // Only the box the live complaint is about points at the region; a submit failure is about
  // neither box, so on that branch nothing claims it.
  const owner = tooShort ? "pw" : mismatch ? "confirm" : null;

  return (
    <StepCard step="password" title="Set your password">
      <p className="blurb">
        It signs you in when Plex can't be reached, and it confirms every deletion. Reaper needs it
        before it goes any further.
      </p>

      <form
        onSubmit={(e) => {
          e.preventDefault();
          if (valid && !save.isPending) save.mutate();
        }}
      >
        <label className="field-sm">
          <span className="field-label">Password</span>
          <input
            type="password"
            autoComplete="new-password"
            value={pw}
            maxLength={128}
            onChange={(e) => setPw(e.target.value)}
            aria-describedby={owner === "pw" ? ERROR_ID : undefined}
          />
          <span className="help">At least {MIN_ADMIN_PASSWORD} characters.</span>
        </label>

        <label className="field-sm">
          <span className="field-label">Confirm password</span>
          <input
            type="password"
            autoComplete="new-password"
            value={confirm}
            maxLength={128}
            onChange={(e) => setConfirm(e.target.value)}
            aria-describedby={owner === "confirm" ? ERROR_ID : undefined}
          />
        </label>

        {error && (
          <Notice tone="error" id={ERROR_ID}>
            {error}
          </Notice>
        )}

        <p className="help">
          Your local account will be called <code>admin</code>. You can rename it later in Settings.
        </p>

        {/* No Skip and no Back: see the note at the top of this file. */}
        <div className="step-actions">
          <span className="spacer" />
          <button type="submit" className="primary btn-lg" disabled={!valid || save.isPending}>
            {save.isPending ? "Saving…" : "Set password and continue"}
          </button>
        </div>
      </form>

      <div className="step-foot">
        Restoring a backup? Set a password first. It's what confirms the restore.
      </div>
    </StepCard>
  );
}
