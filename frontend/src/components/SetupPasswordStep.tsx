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
import { Trans, useTranslation } from "react-i18next";
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
  const { t } = useTranslation();
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
      announce(t("security.form.saved"));
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
    <Trans
      i18nKey="security.form.tooShortError"
      values={{ min: MIN_ADMIN_PASSWORD, count: pw.length }}
      components={{ b: <b /> }}
    />
  ) : mismatch ? (
    t("setup.password.mismatchError")
  ) : save.error ? (
    t("setup.password.saveFailedError", { message: save.error.message })
  ) : null;

  // Only the box the live complaint is about points at the region; a submit failure is about
  // neither box, so on that branch nothing claims it.
  const owner = tooShort ? "pw" : mismatch ? "confirm" : null;

  return (
    <StepCard step="password" title={t("setup.password.title")}>
      <p className="blurb">{t("setup.password.blurb")}</p>

      <form
        onSubmit={(e) => {
          e.preventDefault();
          if (valid && !save.isPending) save.mutate();
        }}
      >
        <label className="field-sm">
          <span className="field-label">{t("setup.password.passwordLabel")}</span>
          <input
            type="password"
            autoComplete="new-password"
            value={pw}
            maxLength={128}
            onChange={(e) => setPw(e.target.value)}
            // One region carries both complaints, so each box describes itself with the live
            // one only while it is the one about IT. `aria-invalid` stays on this box's own
            // predicate: a short password is short whichever complaint is showing.
            aria-invalid={tooShort ? true : undefined}
            aria-describedby={owner === "pw" ? ERROR_ID : undefined}
          />
          <span className="help">{t("setup.password.minHelp", { min: MIN_ADMIN_PASSWORD })}</span>
        </label>

        <label className="field-sm">
          <span className="field-label">{t("setup.password.confirmLabel")}</span>
          <input
            type="password"
            autoComplete="new-password"
            value={confirm}
            maxLength={128}
            onChange={(e) => setConfirm(e.target.value)}
            aria-invalid={mismatch ? true : undefined}
            aria-describedby={owner === "confirm" ? ERROR_ID : undefined}
          />
        </label>

        {/* `standing` on the live ones, the arrangement `AdminPasswordForm` uses and for its
            reason. They explain why the button is off while the operator types, and the first
            renders `{pw.length} so far`, so a live region re-announces the whole string on every
            keystroke. That is around eleven interruptions on the way to a valid password, on the
            form that sets the key arming deletion. Nothing is lost by not interrupting: both
            boxes point here through `aria-describedby`, so the complaint is read as the
            description of the box the operator is standing in. A failed submit is a reaction and
            keeps `role="alert"`, which is what `owner === null` selects. */}
        {error && (
          <Notice tone="error" id={ERROR_ID} standing={owner !== null}>
            {error}
          </Notice>
        )}

        <p className="help">
          <Trans i18nKey="setup.password.adminNote" components={{ code: <code /> }} />
        </p>

        {/* No Skip and no Back: see the note at the top of this file. */}
        <div className="step-actions">
          <span className="spacer" />
          <button type="submit" className="primary btn-lg" disabled={!valid || save.isPending}>
            {save.isPending ? t("common.saving") : t("setup.password.submit")}
          </button>
        </div>
      </form>

      <div className="step-foot">{t("setup.password.restoreFootNote")}</div>
    </StepCard>
  );
}
