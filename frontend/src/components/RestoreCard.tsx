// SPDX-License-Identifier: AGPL-3.0-or-later
//
// Restore from a backup: choose a file, confirm with the admin password, restart to finish.
//
// It lives here rather than inside Settings because it is now reached from two places. Settings
// is the one an operator finds later; the other is the first-run wizard's Connect step, where
// restoring is the *alternative* to adding services one at a time and so belongs beside them
// rather than four screens further on, at the end of a setup the restore is about to overwrite
// (#385). `RestoreCard` is the Settings card, chrome and all; `RestoreFlow` is the same flow
// with no chrome, for a modal that draws its own heading.
//
// **The wizard cannot offer it first.** `POST /api/settings/backup/restore/confirm` refuses
// outright without an admin password ("Set an admin password first. It's what confirms a
// restore."), and the wizard's opening step is what creates one. That is a constraint on where
// the door can go, not an accident of layout.
//
// **The password is typed once.** Where the caller already holds it -- the wizard does, from
// its first step -- it passes it in and the box is replaced by a sentence naming which password
// will be used. The backend's check is untouched: the same string reaches `restoreConfirm`
// either way. A held password that the server then rejects falls back to the box rather than
// dead-ending, because the operator has no other way to correct it from here.

import { useQueryClient } from "@tanstack/react-query";
import {
  type ChangeEvent,
  type DragEvent,
  type RefObject,
  useEffect,
  useRef,
  useState,
} from "react";
import { Trans, useTranslation } from "react-i18next";
import { announce } from "../announce";
import { api, type RestoreSummary } from "../api";
import { useSuccessorFocus } from "../focus";
import { since } from "../format";
import i18next from "../i18n";
import { Notice } from "./Notice";

/** What an armed restore says, in one place because it is said twice: the warn notice on the card
 *  and the sentence spoken into the live region when the confirm lands. Two constants rather than
 *  one so the notice can bold the lead without the announcement inheriting markup -- and so
 *  neither copy can be reworded without the other (rule 144).
 *
 *  It used to instruct -- "Restart Reaper's container to finish" -- because that was the only way
 *  to finish. `Restart now` is under it now, so the sentence says what is true and the button says
 *  what to do (#386). */
export const RESTORE_ARMED_LEAD = i18next.t("backup.restore.armedLead");
export const RESTORE_ARMED_REST = i18next.t("backup.restore.armedRest");

/** The same pair for the state after `Restart now` is pressed. It claims only what the server
 *  accepted -- the stop -- and never that Reaper is back, which this page cannot see: the
 *  connection it would ask over is the one about to go (rule 85). */
export const RESTORE_STOPPING_LEAD = i18next.t("backup.restore.stoppingLead");
export const RESTORE_STOPPING_REST = i18next.t("backup.restore.stoppingRest");

/** What both surfaces pass in. Split out because two components take exactly this set. */
export interface RestoreProps {
  armed: boolean;
  /** The admin password, where the caller already holds it: the wizard takes it on its first
   *  step and keeps it for the length of the flow, so the door on a later step confirms with
   *  it rather than asking for the same string twice. Null (the Settings case, and a wizard
   *  resumed in a fresh tab) draws the box instead. Never persisted by either. */
  heldPassword?: string | null | undefined;
  /** Called whenever a staged backup is waiting on this card, so the section rail can hold a
   *  switch that would drop it. Pass a STABLE function: it is an effect dependency. */
  onDirtyChange?: ((dirty: boolean) => void) | undefined;
}

/** The Settings card: the flow in the panel chrome the rest of that page uses. */
export function RestoreCard(props: RestoreProps) {
  const { t } = useTranslation();
  return (
    <section className="rules-card">
      <h3>{t("backup.restore.heading")}</h3>
      <RestoreFlow {...props} />
    </section>
  );
}

// The restore flow: choose a backup file, confirm with the admin password, then restart to finish.
// A restore never happens live -- the upload is validated and staged, the password arms the swap,
// and preflight does the swap on the next boot before migrations. Three states: idle (choose a
// file), chosen (a validated summary + password), and armed (a restore is staged and waiting for
// Reaper to restart). `armed` is server state (the READY marker), so it survives a reload and shows
// even if this browser never did the confirm.
//
// The armed state has a fourth on top of it, `stopping`, which is not server state and cannot be:
// it says the server accepted a stop, and the read that would confirm anything more is the one the
// stop ends. It does not survive a reload, and does not need to -- a reload after Reaper came back
// finds no armed restore at all, because the swap has happened.
//
// No heading and no container of its own, so a modal can draw them: `RestoreCard` above adds
// the Settings chrome, and `SetupRestoreModal` lets `ModalShell` carry the title. Both render
// the same three states, in the same words.
export function RestoreFlow({ armed, heldPassword, onDirtyChange }: RestoreProps) {
  const { t } = useTranslation();
  const qc = useQueryClient();
  const inputRef = useRef<HTMLInputElement>(null);
  const [summary, setSummary] = useState<RestoreSummary | null>(null);
  const [fileName, setFileName] = useState<string | null>(null);
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [dragging, setDragging] = useState(false);
  const [preparing, setPreparing] = useState(false);
  // A held password the SERVER rejected. It cannot normally be wrong -- the wizard set it a
  // minute ago -- but "cannot normally" is not a state to dead-end on: the only correction from
  // inside this flow is the box, so a refusal gives the box back rather than leaving the
  // operator pressing Restore against a string they cannot see or change.
  const [heldRejected, setHeldRejected] = useState(false);
  // The server took the stop. Local, not server state: the one read that could confirm it is the
  // one the stop is about to end, so this is what THIS browser was told, and it says only that.
  const [stopping, setStopping] = useState(false);
  const useHeld = heldPassword != null && heldPassword !== "" && !heldRejected;
  // Pressing Restore replaces this whole card with the restart prompt, two hops later: the staged
  // summary is dropped first, then the refetch flips `armed`. Focus lands on "Cancel restore" --
  // and it stays `disabled` until `busy` clears in the `finally`, one commit after it mounts, which
  // is why the move waits on being actable rather than on being present (#173).
  //
  // Not on "Restart now", though that is the continuation an operator wants: a programmatic focus
  // move puts the control under whatever key is pressed next, and one of these two stops the app.
  // The reversible one takes the landing.
  const afterArm = useSuccessorFocus();
  // ...and the same hop again when "Restart now" replaces itself with the stopping state, where
  // "Reload" is the only control left.
  const afterStop = useSuccessorFocus();

  // The two sends that outlive a render, held so the unmount cleanup below can wait on them
  // instead of deciding against state that has not settled yet. A prepare stages its archive on
  // the SERVER when it resolves, which can be after this card is gone; a confirm carries the
  // admin password and arms the swap.
  const prepareRef = useRef<Promise<RestoreSummary> | null>(null);
  const confirmRef = useRef<Promise<unknown> | null>(null);

  const refreshInfo = () => qc.invalidateQueries({ queryKey: ["backup-info"] });

  // The token of the staging this card is holding, kept beside `summary` rather than read off
  // it, because the unmount cleanup below runs with `[]` deps and cannot see state. It is
  // written on the same three transitions `summary` is, and written SYNCHRONOUSLY on each: a
  // ref assigned during render carries the last rendered value, which is a render behind the
  // moment `choose` learns the token, and an unmount committed inside that gap would find a
  // staged archive it could no longer name.
  const tokenRef = useRef<string | null>(null);

  const reset = () => {
    setSummary(null);
    tokenRef.current = null;
    setFileName(null);
    setPassword("");
    setError(null);
  };

  // The admin password belongs to the summary it was typed against, so it goes whenever that
  // summary does: here, and on a failed confirm below. Dropping the summary alone unmounted
  // the field while the password stayed in state, and refilled it against a different backup
  // once the next file staged (S-5).
  const choose = async (file: File) => {
    setError(null);
    setSummary(null);
    tokenRef.current = null;
    setPassword("");
    setFileName(file.name);
    setBusy(true);
    setPreparing(true);
    const prepared = api.restorePrepare(file);
    prepareRef.current = prepared;
    try {
      const reviewed = await prepared;
      tokenRef.current = reviewed.token;
      setSummary(reviewed);
    } catch (err) {
      setFileName(null);
      // Each of this card's three failures states its own outcome, because one shared lead-in was
      // wrong for two of them (#178). Nothing was staged here: the file never got read.
      setError(
        err instanceof Error
          ? t("backup.restore.chooseFailed", { message: err.message })
          : t("backup.restore.chooseFailedFallback"),
      );
    } finally {
      setPreparing(false);
      setBusy(false);
      if (prepareRef.current === prepared) prepareRef.current = null;
    }
  };

  const onPick = (e: ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0];
    // Clear the input so choosing the same file name twice still fires a change.
    e.target.value = "";
    if (file) void choose(file);
  };

  const onDrop = (e: DragEvent) => {
    e.preventDefault();
    setDragging(false);
    const file = e.dataTransfer.files?.[0];
    if (file) void choose(file);
  };

  const restore = async () => {
    if (!summary) return;
    setError(null);
    setBusy(true);
    // The one place the two paths meet: whichever string this is, it reaches `restoreConfirm`
    // as the operator's password and the server checks it exactly as it always did.
    const sending = useHeld ? heldPassword : password;
    const confirming = api.restoreConfirm(sending, summary.token);
    confirmRef.current = confirming;
    try {
      await confirming;
      // Said here rather than after the refetch, because THIS is what armed it: the server has
      // staged the swap whether or not the read that redraws the card lands. It was the one
      // outcome in this panel with no signal at all -- the card simply became a different card,
      // which is an absence, and the operator hears nothing when a full restore is armed (#173).
      announce(`${RESTORE_ARMED_LEAD} ${RESTORE_ARMED_REST}`);
      // The confirm armed the swap; refetch so `armed` flips on and this card shows the
      // restart prompt. Drop the staged summary from local state either way.
      reset();
      await refreshInfo();
    } catch (err) {
      setPassword("");
      // A held password that the server would not take is retired here, which puts the box on
      // screen for the next attempt. It is not put back on a later success: the operator has
      // typed one by then, and silently reverting to the held string would send something other
      // than what is in the box in front of them.
      if (useHeld) setHeldRejected(true);
      // The only one of the three the old shared lead-in was right for.
      setError(
        err instanceof Error
          ? t("backup.restore.restoreFailed", { message: err.message })
          : t("backup.restore.restoreFailedFallback"),
      );
    } finally {
      setBusy(false);
      if (confirmRef.current === confirming) confirmRef.current = null;
    }
  };

  // The last step of a restore, as a button rather than as a shell command in another window
  // (#386). The server refuses unless a restore is armed and unless a reap is in flight, so both
  // of this card's ways to be told "no" arrive as an error on the notice below.
  const restartNow = async () => {
    setError(null);
    setBusy(true);
    try {
      await api.restoreRestart();
      // Said on the 200, which is the whole of what settled: the server took the stop. Whether it
      // comes back is not this page's to know or to claim (rule 85).
      setStopping(true);
      announce(`${RESTORE_STOPPING_LEAD} ${RESTORE_STOPPING_REST}`);
    } catch (err) {
      // Its own lead, like the other three: nothing was stopped, and the staged restore is exactly
      // where it was.
      setError(
        err instanceof Error
          ? t("backup.restore.restartFailed", { message: err.message })
          : t("backup.restore.restartFailedFallback"),
      );
    } finally {
      setBusy(false);
    }
  };

  // `token` scopes the discard to the staging this card is showing. `Remove` on a reviewed
  // summary has one and passes it, so it discards the file named on the card and never one a
  // second card staged since (#387); the armed Cancel has no summary, so it passes nothing and
  // discards whatever is there, which is what "cancel this restore" has to mean when the thing
  // being canceled was armed from somewhere else entirely.
  //
  // **An unscoped discard asks the server what it is about to discard.** `armed` is a cached
  // read nothing refreshes from another client -- the query sets no `refetchInterval` and
  // `main.tsx` turns `refetchOnWindowFocus` off app-wide -- so this card can still be drawing
  // the armed branch long after that restore was canceled somewhere else and a fresh archive
  // staged in its place. Sending then destroys the new one and leaves the card that staged it
  // holding a reviewed summary with nothing behind it, which is #387 reached through the one
  // discard a token cannot scope. So the same server read the unmount reclaim does, in the
  // moment before the send: not armed any more means there is nothing here to cancel, and the
  // card redraws as whatever is true now. The sub-second window after the read is not closed
  // by this and is not claimed to be.
  const cancel = async (token?: string) => {
    setBusy(true);
    setError(null);
    try {
      if (token === undefined && !(await api.backupInfo()).restore_armed) {
        reset();
        await refreshInfo();
        return;
      }
      await api.restoreCancel(token);
      reset();
      await refreshInfo();
    } catch (err) {
      // A cancel that fails used to report itself as a restore that did not start, which points
      // the operator at the wrong button: the file is still staged on the server, so what they
      // need to know is that it is still there.
      setError(
        err instanceof Error
          ? t("backup.restore.cancelFailed", { message: err.message })
          : t("backup.restore.cancelFailedFallback"),
      );
    } finally {
      setBusy(false);
    }
  };

  // What this card would LOSE, reported up through `BackupPanel` to `Settings` so leaving the
  // section can stop and ask first. It is the costliest draft in Settings by some way: the archive
  // is already uploaded and staged on the SERVER, and the admin password was typed against that
  // exact file.
  //
  // Rule 146: declared above the `armed` early return, and false inside it. An armed restore is
  // server state that survives a reload, this browser, and this card -- there is nothing here to
  // lose, and the card in that branch offers its own Cancel. Reporting a draft there would demand
  // a discard for a decision already stored. The password is not read separately because it
  // cannot outlive the summary: every path that drops one drops the other (`choose`, `reset`, and
  // the failed confirm), which is S-5's fix and is what keeps this signal to one fact.
  //
  // `preparing` is the other half of the same fact, and reading `summary` alone missed it: the
  // upload is already on its way to the server while the card still shows "Checking <file>", so a
  // switch during that window left with no confirm at all and landed a staged archive nobody
  // could see. Rule 146 asks what the parent is told while this component renders "loading" --
  // this is that answer.
  const staged = !armed && (summary !== null || preparing);
  useEffect(() => {
    onDirtyChange?.(staged);
  }, [staged, onDirtyChange]);
  useEffect(() => () => onDirtyChange?.(false), [onDirtyChange]);

  // A prepare that is never confirmed leaves the archive staged on the server, and nothing else
  // ever clears it: an un-armed stage has no surface anywhere in the app, so it would sit there
  // until the next prepare replaced it. Leaving takes it with us.
  //
  // Deliberately its own effect with `[]` deps, unlike the report above, because this one SENDS
  // something: hung off `onDirtyChange` it would re-run whenever that prop changed identity and
  // cancel a staged restore while the card was still on screen. It reads `stagedRef` for the same
  // reason -- the guard has to be the value at unmount, not at mount.
  //
  // **It reclaims what THIS card staged, never whatever is staged.** A tokenless cancel is an
  // unscoped discard, and two of these cards can be live at once -- Settings in one tab, the
  // wizard's restore door in another -- each holding its own reviewed summary while only the
  // later stage survives on the server, since `stage_upload` replaces the staging directory
  // rather than adding to it. The earlier card leaving at any later moment then discarded the
  // other's archive, and that operator was left looking at a validated summary whose Restore
  // had nothing behind it (#387). So the cancel carries the token minted for our own staging,
  // and the server discards nothing when it no longer matches. Rule 73 binds the *arm* to the
  // content that was reviewed; this is the same binding on the discard.
  //
  // `restore_cancel` discards a staged OR ARMED restore (`api/backup.py:restore_cancel`), so the
  // three ways this can send a cancel it should not are each held off explicitly:
  //
  //   - An upload still in flight has no summary yet and stages its archive AFTER this runs, so
  //     the decision waits on the prepare rather than reading state that has not arrived. A
  //     prepare that REJECTED staged nothing, and a cancel then would be reclaiming someone
  //     else's stage rather than ours.
  //   - A confirm still in flight was already authorized with the admin password, so its outcome
  //     is a decision, not a draft. We wait for it and leave whatever it armed.
  //   - `armed` is a React Query cache nothing refreshes from another client: the query sets no
  //     `refetchInterval`, and `main.tsx` turns `refetchOnWindowFocus` off app-wide, so a restore
  //     armed from a second tab still reads false here. Asking the cache would disarm exactly the
  //     restore the guard exists to protect, so the SERVER is asked instead, immediately before
  //     the send.
  //
  // Best effort throughout: if any leg fails the archive stays staged, which is exactly where it
  // stood before this existed, and there is no longer a card to say so on.
  const stagedRef = useRef(false);
  stagedRef.current = staged;
  useEffect(
    () => () => {
      const reclaimStagedArchive = async () => {
        let token = tokenRef.current;
        if (prepareRef.current) {
          try {
            token = (await prepareRef.current).token;
          } catch {
            return;
          }
        } else if (!stagedRef.current && !confirmRef.current) {
          return;
        }
        await confirmRef.current?.catch(() => {});
        // No token, nothing to reclaim BY: the only send left would be the unscoped one this
        // guard exists to stop. Reached from a confirm we waited on that succeeded and dropped
        // the summary, and from a cancel that had just cleared one -- in both, `stagedRef` is a
        // render behind and says yes while there is nothing of ours left to reclaim. Returning
        // leaves whatever is on the server for the surface that owns it, which is the direction
        // that loses nothing.
        if (!token) return;
        if ((await api.backupInfo()).restore_armed) return;
        await api.restoreCancel(token);
      };
      void reclaimStagedArchive().catch(() => {});
    },
    [],
  );

  if (armed && stopping) {
    return (
      <>
        <Notice tone="warn" className="restore-armed" as="div">
          <span>
            <strong>{RESTORE_STOPPING_LEAD}</strong> {RESTORE_STOPPING_REST}
          </span>
        </Notice>
        <p className="help">{t("backup.restore.stoppingHelp")}</p>
        <div className="backup-actions">
          {/* The operator decides when Reaper is back, because the page cannot: a poll would be
              asking the connection that just went. Pressing this early costs a browser error page
              and another press. */}
          <button
            type="button"
            ref={afterStop.ref as RefObject<HTMLButtonElement>}
            onClick={() => window.location.reload()}
          >
            {t("backup.restore.reloadButton")}
          </button>
        </div>
      </>
    );
  }

  if (armed) {
    return (
      <>
        {/* `standing`: `armed` is the server's READY marker, so this card is what Backup looks
            like on every visit until Cancel or a restart, not a reply to anything pressed here.
            The press that arms it is already covered -- `confirm` announces these same two
            sentences and moves focus onto Cancel -- so the flag takes away a repeat, never the
            only signal. The `stopping` twin above is the opposite: `stopping` is local state set
            by Restart now, so it answers a press and stays an alert. */}
        <Notice tone="warn" className="restore-armed" as="div" standing>
          <span>
            <strong>{RESTORE_ARMED_LEAD}</strong> {RESTORE_ARMED_REST}
          </span>
          <button
            type="button"
            className="link"
            ref={afterArm.ref as RefObject<HTMLButtonElement>}
            onClick={() => void cancel()}
            disabled={busy}
          >
            {t("backup.restore.cancelButton")}
          </button>
        </Notice>
        <div className="backup-actions">
          <button
            type="button"
            className="primary"
            onClick={() => {
              afterStop.arriving();
              void restartNow();
            }}
            disabled={busy}
          >
            {busy ? t("backup.restore.restartingButton") : t("backup.restore.restartButton")}
          </button>
        </div>
        <p className="help">{t("backup.restore.restartHelp")}</p>
        {/* Only the wizard needs telling what happens next, because only there is the rest of
            setup about to be overwritten by what is now staged. */}
        {heldPassword != null && <p className="help">{t("backup.restore.wizardHelp")}</p>}
        {/* The armed card had no failure surface at all, so a Cancel that failed set a sentence
            nothing rendered and the operator watched the button re-enable and nothing else. Both
            of its buttons can be refused, so the notice lives here as well as on the form below
            (rule 17/36). */}
        {error && <Notice tone="error">{error}</Notice>}
      </>
    );
  }

  return (
    <>
      <p className="help">{t("backup.restore.idleHelp")}</p>

      <input ref={inputRef} type="file" accept=".reaper" hidden onChange={onPick} />

      {!summary && (
        <div
          className={`dropzone${dragging ? " dropzone-over" : ""}`}
          onDragOver={(e) => {
            e.preventDefault();
            setDragging(true);
          }}
          onDragLeave={() => setDragging(false)}
          onDrop={onDrop}
        >
          {busy && fileName ? (
            <div className="muted">{t("backup.restore.checkingFile", { fileName })}</div>
          ) : (
            <>
              <div>
                <Trans
                  i18nKey="backup.restore.dropzonePrompt"
                  components={{
                    btn: (
                      <button
                        type="button"
                        className="link"
                        onClick={() => inputRef.current?.click()}
                        disabled={busy}
                      />
                    ),
                  }}
                />
              </div>
              <div className="dz-hint">{t("backup.restore.dropzoneHint")}</div>
            </>
          )}
        </div>
      )}

      {summary && (
        <>
          <div className="chosen">
            <div className="chosen-head">
              <span className="chosen-file">{fileName}</span>
              <button
                type="button"
                className="link chosen-x"
                onClick={() => void cancel(summary.token)}
                disabled={busy}
              >
                {t("common.remove")}
              </button>
            </div>
            <div className="chosen-body">
              <div className="kv2">
                <span>{t("backup.restore.fromLabel")}</span>
                <span>
                  {summary.app_version
                    ? t("backup.restore.fromVersion", { version: summary.app_version })
                    : t("backup.restore.fromVersionUnknown")}
                  {summary.created_at
                    ? t("backup.restore.fromSavedSuffix", { when: since(summary.created_at) })
                    : ""}
                </span>
              </div>
              <div className="kv2">
                <span>{t("backup.insideLabel")}</span>
                <span>{t("backup.insideValue")}</span>
              </div>
              <div className="verdict-ok">
                {/* Decorative here, unlike the test badge: both branches of the sentence below
                    say the verdict in words, so the glyph would only interrupt it with a stray
                    character. Hidden rather than given a text twin (rule 144 -- a second copy
                    of a claim that gates a restore is the copy that goes wrong). */}
                <span className="dot" aria-hidden="true">
                  ✓
                </span>
                {summary.verdict === "current"
                  ? t("backup.restore.verdictCurrent")
                  : t("backup.restore.verdictOutdated")}
              </div>
            </div>
          </div>

          {!summary.key_in_backup && (
            <Notice tone="warn">{t("backup.restore.noKeyWarning")}</Notice>
          )}

          {/* Either the box, or the sentence that replaces it. Never both, and never a
              disabled box holding dots: a field the operator cannot edit invites them to try,
              and the thing it would be showing is their password. */}
          {useHeld ? (
            <p className="help restore-with-held">{t("backup.restore.usingHeldPassword")}</p>
          ) : (
            <label className="field-sm restore-pw">
              <span className="field-label">{t("common.adminPassword")}</span>
              <input
                type="password"
                value={password}
                onChange={(e) => {
                  setError(null);
                  setPassword(e.target.value);
                }}
                autoComplete="current-password"
                maxLength={128}
                placeholder={t("backup.restore.passwordPlaceholder")}
              />
            </label>
          )}

          <div className="backup-actions">
            <button
              type="button"
              className="danger"
              onClick={() => {
                afterArm.arriving();
                void restore();
              }}
              disabled={busy || (!useHeld && !password)}
            >
              {busy ? t("backup.restore.restoringButton") : t("backup.restore.restoreButton")}
            </button>
          </div>
          {/* Read before arming, and a sibling of the armed pair above (rule 144): it named the
              container back when a shell command was the only way to finish, so it now says the
              same thing the armed card does, in the same words, one state early. */}
          <p className="help restore-when">{t("backup.restore.whenHelp")}</p>

          {/* `standing`, same as the download warning above: part of the restore card whenever
              it is on screen, not a reply to a press. */}
          <Notice tone="warn" standing>
            {t("backup.restore.noUndoWarning")}
          </Notice>
        </>
      )}

      {/* The lead-in belongs to whichever step failed, so it is written where the failure is
          caught (`choose`, `restore`, `cancel`) rather than here. One lead for all three said a
          restore had not started when the real news was an unreadable file, or a staged archive
          still sitting on the server (#178). */}
      {error && <Notice tone="error">{error}</Notice>}
    </>
  );
}
