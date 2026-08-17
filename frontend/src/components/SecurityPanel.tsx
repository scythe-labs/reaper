// SPDX-License-Identifier: AGPL-3.0-or-later
//
// Settings -> Security: the admin password that confirms a real deletion.
//
// Nothing here deletes anything and nothing here arms anything. The deletion switch lives in
// Policy -> Deletion; this panel only sets the password that switch demands. The three boxes
// live in `AdminPasswordForm`, a child, so the draft signal is declared there and passed up
// through the panel, which is the hop rule 146 is about.

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { type ChangeEvent, type ReactNode, useEffect, useState } from "react";
import { announce } from "../announce";
import { api } from "../api";
import { useSafety } from "../useSafety";
import { StaleReadNotice } from "./StaleReadNotice";
import { Notice } from "./Notice";

// The same floor the server applies (MIN_PASSWORD_LENGTH in
// reaper/services/admin_password.py), so the placeholder, the live message, and the server
// rule all state one number. Exported because the first-run wizard sets this same password
// on its own step and states the same floor: a second literal 12 there would be one rule
// written twice, and the copy that drifts is the one nobody edits (rule 67, rule 144).
export const MIN_ADMIN_PASSWORD = 12;

/** The password form's one error region, named once for both ends of the association (rule 67).
 *  Which BOX claims it varies: the region carries whichever complaint is live, and only the box
 *  that complaint is about points at it -- see `errorOwner`, which derives that from the same
 *  chain the message comes off. Two independent predicates cannot decide it, because they
 *  overlap and the region does not. */
const PASSWORD_ERROR_ID = "admin-password-error";

function AdminPasswordForm({
  needed,
  /** Called whenever this form gains or loses typed text, so the section rail can hold a switch
   *  that would discard it. Pass a STABLE function: it is an effect dependency. */
  onDirtyChange,
}: {
  needed: boolean;
  onDirtyChange?: ((dirty: boolean) => void) | undefined;
}) {
  const queryClient = useQueryClient();
  const [current, setCurrent] = useState("");
  const [pw, setPw] = useState("");
  const [confirm, setConfirm] = useState("");
  const [msg, setMsg] = useState<string | null>(null);

  // Did a recovery code open this session? That is the one case the server takes a new
  // password without the current one, because a forgotten password is what recovery mode is
  // FOR -- demanding it here left the operator signed in and still locked out (#433).
  //
  // Rule 17/36 wants the unknown and failed states answered explicitly, and they are: `?? false`
  // is the strict answer, not a placeholder. A session this form cannot read is treated exactly
  // like an ordinary one, so the current-password box stays live and required. It reads the same
  // ["me"] entry `App` resolved before anything under it could mount, so in practice there is no
  // pending state to pass through. And the server re-reads the session's own mark on the request
  // either way: this can only relax what the FORM asks for, never what the API allows.
  const { data: me } = useQuery({ queryKey: ["me"], queryFn: api.me, retry: false });
  const viaRecovery = me?.via_recovery ?? false;
  const skipCurrent = needed || viaRecovery;

  const save = useMutation({
    // Only the new password is sent: the confirm field exists so a typo can't lock the
    // operator out of the key that arms deletion, and it never leaves the browser.
    mutationFn: () => api.setAdminPassword(pw, skipCurrent ? undefined : current),
    onSuccess: () => {
      setCurrent("");
      setPw("");
      setConfirm("");
      setMsg("Password saved.");
      // The visible half is a `.muted` span beside the button, which announces nothing. The
      // failure half of this form already speaks (`Notice`, `role="alert"`), so success was
      // the only outcome of changing the password that arms deletion an operator could not
      // hear -- the one asymmetry the comment below is careful about, reached another way.
      announce("Password saved.");
      void queryClient.invalidateQueries({ queryKey: ["safety"] });
      // The server spends the recovery mark in the same transaction as the new hash, so this
      // session is an ordinary one from here on. Re-read it, or the current-password box would
      // stay grayed out over a session that no longer excuses it and the next change would fail
      // at the API instead of at the form.
      void queryClient.invalidateQueries({ queryKey: ["me"] });
    },
    onError: () => {
      // Deliberately NOT the mirror of onSuccess: nothing is written to `msg`, because a
      // failure renders from `save.error` as an error notice and this password is what
      // confirms turning deletion on -- "saved" and "wrong password" must never look alike.
      //
      // The re-read is the whole point. A second tab mounted before the mark was spent holds
      // `via_recovery: true` in a cache that nothing refetches (`main.tsx` sets
      // `refetchOnWindowFocus: false`), so its box stays parked and empty while the server
      // refuses every submit: a form with no way out but a reload. Re-reading here turns the
      // refusal into the state that explains it, with the current-password box live again.
      void queryClient.invalidateQueries({ queryKey: ["me"] });
    },
  });

  const tooShort = pw.length > 0 && pw.length < MIN_ADMIN_PASSWORD;
  const mismatch = confirm.length > 0 && confirm !== pw;
  const needCurrent = !skipCurrent && current.length === 0;
  const valid =
    pw.length >= MIN_ADMIN_PASSWORD && confirm.length > 0 && confirm === pw && !needCurrent;

  // The third live complaint, and the one that used to be missing: an empty current password
  // turns Save off with nothing on the page saying so, on the form that sets the key arming
  // deletion (#188). Gated on a new password having been typed, like the two above are gated on
  // their own box: on a pristine form nothing is wrong yet, and a complaint about a box the
  // operator has not reached reads as a failure rather than a next step.
  const askCurrent = needCurrent && pw.length > 0;

  // One red error region under the row. Live validation (too short, then mismatch, then the
  // missing current password) explains why Save is off while typing; a failed submit reuses the
  // same box. Validation wins over a stale submit error so the operator sees the thing they can
  // fix right now -- including a current password they have just cleared, which is why
  // `askCurrent` sits above `save.error` rather than below it.
  const errorNode: ReactNode = tooShort ? (
    <>
      Use at least {MIN_ADMIN_PASSWORD} characters. <b>{pw.length} so far.</b>
    </>
  ) : mismatch ? (
    "The passwords don't match."
  ) : askCurrent ? (
    "Enter the current password to save."
  ) : save.error ? (
    needed ? (
      `The password wasn't set: ${save.error.message}`
    ) : (
      `The password wasn't changed: ${save.error.message}`
    )
  ) : null;

  // Which BOX the live complaint belongs to, derived from the same chain that picks it rather
  // than from the predicates separately. `tooShort` and `mismatch` are independent and both
  // hold constantly -- any short password with a non-matching confirm -- while the region shows
  // only the first. Read off the two predicates, the confirm box then pointed at a region
  // holding "Use at least 12 characters", reading the box above it out as its own problem, and
  // "The passwords don't match." was not on the page at all to be reached (#174).
  const errorOwner: "new" | "confirm" | "current" | null = tooShort
    ? "new"
    : mismatch
      ? "confirm"
      : askCurrent
        ? "current"
        : null;

  // What this form would LOSE, reported up through `SecurityPanel` to `Settings` so leaving the
  // section can stop and ask first. Any of the three boxes counts: a password too short to save,
  // or one whose confirm does not match yet, is still text the operator typed and still gone on
  // unmount -- reporting only the saveable form (`valid`) would drop exactly the half-finished
  // ones silently.
  //
  // Rule 146 asks two things of this signal, and this component answers both trivially: it has no
  // early return, so every state it renders is one where all three boxes are on screen. What the
  // second claim does bind is the panel above, whose own early returns unmount this form -- see
  // `SecurityPanel`.
  const typed = current.length > 0 || pw.length > 0 || confirm.length > 0;
  useEffect(() => {
    onDirtyChange?.(typed);
  }, [typed, onDirtyChange]);
  useEffect(() => () => onDirtyChange?.(false), [onDirtyChange]);

  // Typing clears the "saved" note and any stale failure, so neither lingers over a form the
  // operator is now re-editing.
  const onEdit = (set: (v: string) => void) => (e: ChangeEvent<HTMLInputElement>) => {
    setMsg(null);
    if (save.isError) save.reset();
    set(e.target.value);
  };

  return (
    <div className="safety-row pw-row">
      <div className="pw-head">
        <strong>{needed ? "Set an admin password" : "Change the admin password"}</strong>
        <p className="help">
          {needed
            ? "Choose something long, and keep it somewhere safe."
            : viaRecovery
              ? "You can set a new one without the old password."
              : "Changing it needs the current password first."}
        </p>
      </div>
      <div className="pw-col">
        <form
          className="pw-form"
          onSubmit={(e) => {
            e.preventDefault();
            setMsg(null);
            save.mutate();
          }}
        >
          {/* The current password proves who you are; a divider sets it apart from the new
              one below. First-time setup has no current password, so neither is shown.

              A recovery session keeps the box on screen but parks it: disabled and dimmed,
              the way an option behind a switch reads (`.set-row.dim`, rule 18). Hiding it
              instead would leave an operator who has used this form before wondering which
              form they were looking at, and the one line under it is the answer to the
              question the empty box asks. */}
          {!needed && (
            <>
              <label className={viaRecovery ? "field-sm dim" : "field-sm"}>
                <span className="field-label">Current password</span>
                <input
                  type="password"
                  value={viaRecovery ? "" : current}
                  onChange={onEdit(setCurrent)}
                  disabled={viaRecovery}
                  autoComplete="current-password"
                  maxLength={128}
                  aria-describedby={errorOwner === "current" ? PASSWORD_ERROR_ID : undefined}
                />
                {viaRecovery && (
                  <span className="help">Not needed. A recovery code signed you in.</span>
                )}
              </label>
              <hr className="pw-sep" />
            </>
          )}
          <label className="field-sm">
            <span className="field-label">New password</span>
            {/* The placeholder states the length up front; the label names the field. The
                cap is the server's own, so a long pasted passphrase is stopped in the box
                rather than coming back as a validator's sentence. */}
            <input
              type="password"
              value={pw}
              onChange={onEdit(setPw)}
              placeholder={`at least ${MIN_ADMIN_PASSWORD} characters`}
              autoComplete="new-password"
              maxLength={128}
              // One region carries three different complaints, so each box describes itself
              // with the live one only while it is the one about IT -- `errorOwner`, off the
              // same chain that picks the message. `aria-invalid` stays on this box's own
              // predicate: a short password is short whichever complaint is showing.
              aria-invalid={tooShort ? true : undefined}
              aria-describedby={errorOwner === "new" ? PASSWORD_ERROR_ID : undefined}
            />
          </label>
          <label className="field-sm">
            <span className="field-label">Confirm new password</span>
            <input
              type="password"
              value={confirm}
              onChange={onEdit(setConfirm)}
              autoComplete="new-password"
              maxLength={128}
              aria-invalid={mismatch ? true : undefined}
              aria-describedby={errorOwner === "confirm" ? PASSWORD_ERROR_ID : undefined}
            />
          </label>
          <div className="add-actions">
            <button type="submit" className="primary" disabled={!valid || save.isPending}>
              Save
            </button>
            {msg && <span className="muted">{msg}</span>}
          </div>
        </form>
        {/* One box, two kinds of message, so the flag is read off the same chain that picks the
            text. `errorOwner` is non-null for exactly the three live branches and null for the
            fourth, because `valid` requires all three to be clear before Save can be pressed at
            all -- so a submit failure always mounts this fresh, which is the insertion
            `role="alert"` is announced on.

            `standing` on the live ones: they explain why Save is off WHILE THE OPERATOR TYPES,
            and the first of them renders `{pw.length} so far`, so its text changed inside a live
            region on every keystroke and re-announced the whole string each time -- around
            eleven interruptions on the way to a valid password, on the form that sets the key
            arming deletion. Nothing is lost by not interrupting: all three inputs point here
            through `aria-describedby`, so the complaint is read as the description of the box
            the operator is standing in. This is the case `Notice.tsx` names outright. */}
        {errorNode && (
          <Notice tone="error" id={PASSWORD_ERROR_ID} standing={errorOwner !== null}>
            {errorNode}
          </Notice>
        )}
      </div>
    </div>
  );
}

export function SecurityPanel({
  /** Called whenever the password form gains or loses typed text, so the section rail can hold a
   *  switch that would discard it. Pass a STABLE function: it is an effect dependency. */
  onDirtyChange,
}: {
  onDirtyChange?: ((dirty: boolean) => void) | undefined;
} = {}) {
  const { data, isLoading, isError } = useSafety();

  if (isLoading) {
    return (
      <div className="panel">
        <h2>Security</h2>
        <p className="muted">Loading…</p>
      </div>
    );
  }
  // Rule 146: the draft this panel reports upward lives in `AdminPasswordForm` below, so an early
  // return here does not merely hide the form -- it unmounts it, and three typed password boxes go
  // with it. That is not a rare state: `useSafety` refetches every 15 seconds and on window focus,
  // so ONE failed poll while someone is choosing a password used to replace the form mid-typing
  // with a "couldn't load" paragraph and say nothing about what it took. React Query keeps the
  // last good row through a failed refetch, so the form stays on it and this branch is now only
  // for a load that never landed one -- the same shape `GeneralPanel` and `PlexPanel` already use
  // (rule 72), with the same one line saying the read failed.
  if (!data) {
    return (
      <div className="panel">
        <h2>Security</h2>
        <Notice tone="error">Couldn't load these settings. Reload to try again.</Notice>
      </div>
    );
  }

  return (
    <div className="panel">
      <h2>Security</h2>
      <p className="blurb">
        The admin password. It confirms turning deletion on (in <strong>Policy → Deletion</strong>),
        and it's also how you sign in without Plex.
      </p>

      {isError && <StaleReadNotice />}

      <AdminPasswordForm needed={!data.has_password} onDirtyChange={onDirtyChange} />
    </div>
  );
}
