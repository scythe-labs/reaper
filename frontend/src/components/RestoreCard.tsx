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
import { announce } from "../announce";
import { api, type RestoreSummary } from "../api";
import { useSuccessorFocus } from "../focus";
import { since } from "../format";
import { Notice } from "./Notice";

/** What an armed restore says, in one place because it is said twice: the warn notice on the card
 *  and the sentence spoken into the live region when the confirm lands. Two constants rather than
 *  one so the notice can bold the lead without the announcement inheriting markup -- and so
 *  neither copy can be reworded without the other (rule 144). */
export const RESTORE_ARMED_LEAD = "A restore is ready.";
export const RESTORE_ARMED_REST = "Restart Reaper's container to finish. Nothing has changed yet.";

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
  return (
    <section className="rules-card">
      <h3>Restore from a backup</h3>
      <RestoreFlow {...props} />
    </section>
  );
}

// The restore flow: choose a backup file, confirm with the admin password, then restart the
// container to finish. A restore never happens live -- the upload is validated and staged, the
// password arms the swap, and the entrypoint does the swap on the next boot before migrations.
// Three states: idle (choose a file), chosen (a validated summary + password), and armed (a
// restore is staged and waiting for a restart). `armed` is server state (the READY marker), so
// it survives a reload and shows even if this browser never did the confirm.
//
// No heading and no container of its own, so a modal can draw them: `RestoreCard` above adds
// the Settings chrome, and `SetupRestoreModal` lets `ModalShell` carry the title. Both render
// the same three states, in the same words.
export function RestoreFlow({ armed, heldPassword, onDirtyChange }: RestoreProps) {
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
  const useHeld = heldPassword != null && heldPassword !== "" && !heldRejected;
  // Pressing Restore replaces this whole card with the restart prompt, two hops later: the staged
  // summary is dropped first, then the refetch flips `armed`. Focus lands on "Cancel restore",
  // which is the only control the armed card has and the only way back out of it -- and it stays
  // `disabled` until `busy` clears in the `finally`, one commit after it mounts, which is why the
  // move waits on being actable rather than on being present (#173).
  const afterArm = useSuccessorFocus();

  // The two sends that outlive a render, held so the unmount cleanup below can wait on them
  // instead of deciding against state that has not settled yet. A prepare stages its archive on
  // the SERVER when it resolves, which can be after this card is gone; a confirm carries the
  // admin password and arms the swap.
  const prepareRef = useRef<Promise<RestoreSummary> | null>(null);
  const confirmRef = useRef<Promise<unknown> | null>(null);

  const refreshInfo = () => qc.invalidateQueries({ queryKey: ["backup-info"] });

  const reset = () => {
    setSummary(null);
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
    setPassword("");
    setFileName(file.name);
    setBusy(true);
    setPreparing(true);
    const prepared = api.restorePrepare(file);
    prepareRef.current = prepared;
    try {
      setSummary(await prepared);
    } catch (err) {
      setFileName(null);
      // Each of this card's three failures states its own outcome, because one shared lead-in was
      // wrong for two of them (#178). Nothing was staged here: the file never got read.
      setError(
        err instanceof Error
          ? `Reaper couldn't read that file: ${err.message}`
          : "Reaper couldn't read that file.",
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
          ? `The restore didn't start: ${err.message}`
          : "The restore didn't start.",
      );
    } finally {
      setBusy(false);
      if (confirmRef.current === confirming) confirmRef.current = null;
    }
  };

  const cancel = async () => {
    setBusy(true);
    try {
      await api.restoreCancel();
      reset();
      await refreshInfo();
    } catch (err) {
      // A cancel that fails used to report itself as a restore that did not start, which points
      // the operator at the wrong button: the file is still staged on the server, so what they
      // need to know is that it is still there.
      setError(
        err instanceof Error
          ? `That file is still waiting to be restored: ${err.message}`
          : "That file is still waiting to be restored.",
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
        if (prepareRef.current) {
          try {
            await prepareRef.current;
          } catch {
            return;
          }
        } else if (!stagedRef.current && !confirmRef.current) {
          return;
        }
        await confirmRef.current?.catch(() => {});
        if ((await api.backupInfo()).restore_armed) return;
        await api.restoreCancel();
      };
      void reclaimStagedArchive().catch(() => {});
    },
    [],
  );

  if (armed) {
    return (
      <>
        <Notice tone="warn" className="restore-armed" as="div">
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
            Cancel restore
          </button>
        </Notice>
        {/* Only the wizard needs telling what happens next, because only there is the rest of
            setup about to be overwritten by what is now staged. */}
        {heldPassword != null && (
          <p className="help">
            You can close this. When Reaper comes back it will have everything from the backup, and
            setup will be behind you.
          </p>
        )}
      </>
    );
  }

  return (
    <>
      <p className="help">
        Replace everything here with a saved backup. Deletion stays off after a restore until you
        turn it back on.
      </p>

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
            <div className="muted">Checking {fileName}…</div>
          ) : (
            <>
              <div>
                Drop a backup file here, or{" "}
                <button
                  type="button"
                  className="link"
                  onClick={() => inputRef.current?.click()}
                  disabled={busy}
                >
                  choose one
                </button>
                .
              </div>
              <div className="dz-hint">Reaper backup file (.reaper)</div>
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
                onClick={() => void cancel()}
                disabled={busy}
              >
                Remove
              </button>
            </div>
            <div className="chosen-body">
              <div className="kv2">
                <span>From</span>
                <span>
                  {summary.app_version ? `Reaper ${summary.app_version}` : "an earlier setup"}
                  {summary.created_at ? `, saved ${since(summary.created_at)}` : ""}
                </span>
              </div>
              <div className="kv2">
                <span>Inside</span>
                <span>Decisions, settings, credentials</span>
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
                  ? "Matches this server. Safe to restore."
                  : "Saved on an older version. Reaper will update it when it restarts."}
              </div>
            </div>
          </div>

          {!summary.key_in_backup && (
            <Notice tone="warn">
              This backup doesn't include the encryption key. Set REAPER_SECRET_KEY on this server
              to the value it was saved with, or your saved credentials can't be read after the
              restore.
            </Notice>
          )}

          {/* Either the box, or the sentence that replaces it. Never both, and never a
              disabled box holding dots: a field the operator cannot edit invites them to try,
              and the thing it would be showing is their password. */}
          {useHeld ? (
            <p className="help restore-with-held">
              Reaper will confirm with the password you just set.
            </p>
          ) : (
            <label className="field-sm restore-pw">
              <span className="field-label">Admin password</span>
              <input
                type="password"
                value={password}
                onChange={(e) => {
                  setError(null);
                  setPassword(e.target.value);
                }}
                autoComplete="current-password"
                maxLength={128}
                placeholder="Confirm to restore"
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
              {busy ? "Restoring…" : "Restore"}
            </button>
          </div>
          <p className="help restore-when">
            Restoring takes effect the next time the container restarts. Nothing changes until then.
          </p>

          {/* `standing`, same as the download warning above: part of the restore card whenever
              it is on screen, not a reply to a press. */}
          <Notice tone="warn" standing>
            Restoring replaces your current decisions and settings. There is no undo, so download a
            backup first.
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
