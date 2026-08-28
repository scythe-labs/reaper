// SPDX-License-Identifier: AGPL-3.0-or-later
//
// The reap confirmation: the one place in the UI that starts a deletion.
//
// A deliberate gauntlet, and every gate resolves toward not deleting:
//   1. A dry run walks the whole plan and sends nothing. Execute stays disabled until it
//      completes cleanly, so nobody can reap a plan that hasn't proven itself.
//   2. Deletion must be armed on the host (Policy -> Deletion). If it's off, this sheet says
//      so and points there; there is no way to arm it from here. A safety state this sheet
//      could not read is never reported as "off": pending says it's checking, a failed read
//      says it couldn't look. Execute stays disabled through all three.
//   3. The operator must type the exact content-bound phrase ("REAP 1 SOUL 0 GB"). It carries
//      the count and size, so muscle memory can't carry anyone through it, and a stale plan
//      reads as obviously different. The server recomputes it and refuses anything else.
//
// The sheet's job ends when the reap begins. On Reap it seeds the shared status and closes, so
// the operator drops straight back into the app. The Reap tab (ReapPlan.tsx) is the live
// dashboard from there: progress, the item being removed, the per-item log, then the result read
// back from what the run persisted. The app-wide reap bar (ReapBar.tsx) carries the count, Stop,
// and the end announcement to every other screen. This sheet shows neither progress nor a report.
//
// Every stage announces itself out loud, because a poll can change what is on screen with
// nobody touching anything: practice run pending, then passed, then the arm block and the
// typed-phrase field appearing. Without an announcement, an operator could be asked to type a
// content-bound phrase into a box they were never told had arrived. The stages call
// `announce()` and the phrase field takes focus when it mounts.

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useCallback, useEffect, useRef, useState } from "react";
import { Trans, useTranslation } from "react-i18next";
import { announce } from "../announce";
import { ApiError, api, type Run, type RunReport } from "../api";
import { describeError } from "../errors";
import { bytes, souls } from "../format";
import { usePlexTrash, trashWarning } from "../usePlexTrash";
import { useSafety } from "../useSafety";
import { composeError } from "../why";
import { ModalShell } from "./ModalShell";
import { PlexTrashNotice } from "./PlexTrashNotice";
import { Notice } from "./Notice";

export function ReapConfirm({
  run: openedWith,
  onClose,
  onStarted,
}: {
  /** The plan to confirm, as the caller holds it. It seeds the shared cache entry below; only
   *  its id is relied on afterwards, so a caller holding a captured copy is not a problem. */
  run: Run;
  onClose: () => void;
  /** The reap began (Execute succeeded). Fired before the sheet closes, so a caller can react
   *  to the items now being deleted (the review queue clears its selection). Cancel and ✕ do
   *  not fire it. */
  onStarted?: () => void;
}) {
  const queryClient = useQueryClient();
  const { t } = useTranslation();
  // The plan, read through the one cache key every surface uses for it. The caller's copy seeds
  // it, so opening the sheet never waits on (or costs) a fetch.
  //
  // The observer is the point: the 409 recovery below invalidates ["run", id], and an
  // invalidation only reaches a component watching that key. The reap plan page happens to
  // hold its run through it, but "Reap now" in the review queue hands over a captured object
  // with no observer at all. Without an observer here, that path would keep measuring against
  // a phrase the server had already moved past, so typing the phrase the error itself quoted
  // would leave the button disabled. Watching the key here fixes both paths at once.
  const { data: run } = useQuery({
    queryKey: ["run", openedWith.id],
    queryFn: () => api.run(openedWith.id),
    initialData: openedWith,
    // Only an explicit invalidation refetches this: the phrase is re-derived server-side on
    // execute anyway, so there is nothing to poll for.
    staleTime: Infinity,
  });
  const [typed, setTyped] = useState("");
  const [dryReport, setDryReport] = useState<RunReport | null>(null);
  // Consent to Plex purging trash this run did not cause. Reset with the phrase below, so
  // it is always a decision about the plan actually on screen.
  const [trashAcked, setTrashAcked] = useState(false);

  const safety = useSafety();
  const armed = safety.data?.destructive_enabled === true;

  const dry = useMutation({
    mutationFn: () => api.dryRun(run.id),
    onSuccess: setDryReport,
  });

  // The live reap status, shared with the app-wide reap bar and the Reap tab (one cache key,
  // one poll). This sheet only confirms a fresh plan, so it never watches its own run here; it
  // reads this to learn whether a DIFFERENT run holds the single reap slot. Idle still polls,
  // slowly: a reap started from a phone or a second tab must reach an already-open sheet, which
  // is otherwise still offering Execute for a slot now taken.
  const reap = useQuery({
    queryKey: ["reapStatus"],
    queryFn: api.reapStatus,
    refetchInterval: (q) => (q.state.data?.running ? 1000 : 15000),
  });
  const status = reap.data;
  // A DIFFERENT run holds the single reap slot. The arm+confirm stage must not present itself
  // as ready to fire while it does (the server would 409 a second execute anyway).
  const otherRunning = !!status?.running && status.run_id !== run.id;

  const exec = useMutation({
    // What the operator typed, verbatim, never the phrase this sheet already holds. The
    // server re-derives the expected phrase live, so posting our own copy would reduce the
    // human check to a `disabled` attribute the server cannot tell from an echo, and would
    // deadlock the moment the expected phrase moved under an open sheet.
    mutationFn: () => api.executeRun(run.id, typed.trim()),
    onSuccess: (s) => {
      // Seed the shared status so the Reap tab and the app-wide bar show "running" at once,
      // without waiting for the first poll. Then hand the run to them and close: the run is
      // detached on the server, so this sheet has nothing left to show.
      queryClient.setQueryData(["reapStatus"], s);
      onStarted?.();
      onClose();
    },
    onError: (e) => {
      // The phrase moved while this sheet was open (a spare or reap elsewhere, a raised
      // unknown-size allowance). Pull the run again so the label, the placeholder, and the
      // typed check all measure against the phrase the server will actually accept. The query
      // above is what makes this land, on every path that opens the sheet.
      if (e instanceof ApiError && e.status === 409) {
        void queryClient.invalidateQueries({ queryKey: ["run", openedWith.id] });
      }
    },
  });

  // Prove the plan the moment the sheet opens. Nothing is sent; this only walks interlocks.
  // Keyed on the phrase, not just the run id. The phrase is content-bound, so the server
  // moving it means this plan now covers different items, which is exactly what the 409
  // recovery above refetches. Re-proving on that change stops the sheet from showing
  // "Practice run passed" for a plan the server has already rejected while every figure
  // beside it describes the new one: a green tick would otherwise assert a practice run that
  // never happened for this content. Keying on the run id alone would not catch this, since
  // the run id never changes here even though the phrase does.
  useEffect(() => {
    setDryReport(null);
    // The phrase is content-bound, so it moving means this sheet now covers different
    // items. A tick that survived that would be consent carried from a plan the operator
    // is no longer looking at.
    setTrashAcked(false);
    dry.mutate();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [run.id, run.confirmation_phrase]);

  const dryClean = dryReport?.dry_run === true && dryReport.state === "completed";
  const phraseOk = typed.trim() === run.confirmation_phrase;
  // Emptying Plex's trash takes the records of everything already in there, not just what
  // this run deleted, and the executor's count-delta gate structurally cannot see those
  // items. So the operator is told and must tick before Reap enables. The tick is dropped
  // whenever the plan's content changes (the effect above), so consent from one plan can
  // never carry into a different one.
  const trash = usePlexTrash();
  const warn = trashWarning(trash.data, trash.isError);
  const trashOk = !warn.show || trashAcked;
  const canExecute = armed && dryClean && phraseOk && trashOk && !exec.isPending && !otherRunning;

  // The typed-phrase field appears part-way through a dialog the operator is already standing
  // in, on a poll rather than on anything they did, so nothing carries them to it. Focus goes
  // there when it mounts: it is the only thing the stage asks for, and this is the last gate
  // before files are deleted.
  // Not while the Plex-trash consent is still outstanding: that notice renders ABOVE the phrase
  // field and `trashOk` is holding Reap disabled, so jumping the operator over it hides both the
  // disclosure and the reason Reap will not light. Left alone, a reader moving down the dialog
  // meets the notice in document order, which is what happened before this focus move existed.
  const phraseRef = useRef<HTMLInputElement>(null);
  const armStage = dryClean && !otherRunning && armed;
  useEffect(() => {
    if (armStage && trashOk) phraseRef.current?.focus();
  }, [armStage, trashOk]);

  // What the sheet has said so far, so a poll that changes nothing says nothing. `null` until
  // the first announcement.
  const spokenRef = useRef<string | null>(null);
  // `useCallback` with no dependencies, so the effect below can name it honestly in its own
  // deps rather than reaching for a disable directive: everything it reads is a ref.
  const say = useCallback((line: string) => {
    if (spokenRef.current === line) return;
    spokenRef.current = line;
    announce(line);
  }, []);
  // Stage announcements, in the order an operator meets them: the practice run settling, then
  // the arm block and phrase field arriving. The run's own progress and end are not announced
  // here: this sheet is closed by the time either happens, and ReapBar owns the end
  // announcement on every screen.
  useEffect(() => {
    if (!dryClean) return;
    // From here the spoken stage reads the same checks the screen does, rather than
    // re-deriving a weaker version of them. Reading them separately would risk two kinds of
    // mismatch: another reap holding the slot renders that notice with no phrase field, so a
    // separately-derived stage could tell the operator to type a phrase that isn't there, and
    // `say`'s dedupe could then swallow the correct sentence as a repeat once the field
    // actually arrived, leaving silence at the only moment they could act.
    if (otherRunning) return say(t("reapConfirm.otherRunning"));
    // Three states, never one definite claim, the same three the arm block below shows.
    // `armed` is `destructive_enabled === true`, so a switch nobody could read collapses into
    // "off", and saying that out loud is the reassuring direction to be wrong in, on the last
    // screen before files go.
    if (safety.isPending) return;
    if (safety.isError || !safety.data)
      return say(t("reapConfirm.practiceRun.announceUnknownSafety"));
    if (!armed) return say(t("reapConfirm.practiceRun.announceOff"));
    say(t("reapConfirm.practiceRun.announcePassed"));
  }, [dryClean, armed, otherRunning, safety.isPending, safety.isError, safety.data, say, t]);

  return (
    <ModalShell
      title={t("reapConfirm.title", {
        count: run.item_count,
        n: run.item_count,
        bytes: bytes(run.total_bytes),
      })}
      onClose={onClose}
      className="reap-confirm"
    >
      {/* Said again here, not only on the plan screen: this is the last surface before
          the files go, and the count above is smaller than the queue's for a reason the
          owner is entitled to know while deciding. */}
      {run.held_back_unknown_size > 0 && (
        <Notice tone="warn">
          {t("reapConfirm.heldBack", {
            n: run.held_back_unknown_size,
            souls: souls(run.held_back_unknown_size),
          })}
        </Notice>
      )}

      {/* Stage 1: the practice run ("dry run" is still the API's and the executor's word for
          it, but every operator-facing string in this flow says practice run). */}
      {dry.isPending && <p className="blurb">{t("reapConfirm.practiceRun.checking")}</p>}
      {dry.error && (
        <Notice tone="error">
          {t("reapConfirm.practiceRun.failed", { message: describeError(dry.error) })}
        </Notice>
      )}
      {dryReport?.dry_run && dryReport.state === "aborted" && (
        <div className="sim sim-info">
          {/* "stopped", the one word the product uses for this. */}
          <strong>{t("reapConfirm.practiceRun.stopped")}</strong>
          <p>{dryReport.aborted_reason && composeError(dryReport.aborted_reason)}</p>
        </div>
      )}
      {dryClean && (
        <p className="dry-ok">
          <span className="gate-mark" aria-hidden="true">
            ✓
          </span>{" "}
          {t("reapConfirm.practiceRun.passed")}
        </p>
      )}

      {/* Another reap holds the single slot: say so, rather than lighting a Reap button the
          server would refuse. `standing`: `otherRunning` is somebody ELSE's run, seen through
          the poll, so it inserts under a sheet that is already open with nothing pressed here. */}
      {otherRunning && (
        <Notice tone="warn" standing>
          {t("reapConfirm.otherRunning")}
        </Notice>
      )}

      {/* Plex's trash takes more than this reap deletes, so say so before the phrase field
          and hold Reap until it is acknowledged. */}
      {warn.show && !otherRunning && (
        <PlexTrashNotice
          known={warn.known}
          unreadable={warn.unreadable}
          autoEmpties={warn.autoEmpties}
          acked={trashAcked}
          onAck={setTrashAcked}
        />
      )}

      {/* Stage 2: arm + typed confirmation, shown once the practice run is clean and no other
          run holds the slot. */}
      {dryClean && !otherRunning && (
        <div className="reap-arm">
          {!armed ? (
            // Three states, never one definite claim: only a switch we actually read may
            // be reported as off.
            safety.isPending ? (
              <p className="reap-disarmed">{t("common.checkingDeletion")}</p>
            ) : safety.isError || !safety.data ? (
              <Notice tone="warn">
                <Trans i18nKey="reapConfirm.arm.unknown" components={{ em: <em /> }} />
              </Notice>
            ) : (
              <p className="reap-disarmed">
                <Trans
                  i18nKey="reapConfirm.arm.off"
                  components={{ strong: <strong />, em: <em /> }}
                />
              </p>
            )
          ) : (
            <>
              {/* The second passed-check line: the practice run above and arming here are
                  the two gates a dry run alone cannot prove. */}
              <p className="dry-ok">
                <span className="gate-mark" aria-hidden="true">
                  ✓
                </span>{" "}
                {t("reapConfirm.arm.armedLine")}
              </p>
              <label className="reap-confirm-label" htmlFor="reap-phrase">
                {t("reapConfirm.arm.typePhrase")}
              </label>
              <p className="reap-confirm-phrase">{run.confirmation_phrase}</p>
              <input
                ref={phraseRef}
                id="reap-phrase"
                className="reap-confirm-input"
                autoComplete="off"
                spellCheck={false}
                value={typed}
                placeholder={t("reapConfirm.arm.placeholder")}
                onChange={(e) => setTyped(e.target.value)}
              />
            </>
          )}
          {exec.error && <Notice tone="error">{describeError(exec.error)}</Notice>}
          <div className="reap-confirm-actions">
            <button className="ghost" onClick={onClose} disabled={exec.isPending}>
              {t("common.cancel")}
            </button>
            <button className="danger" disabled={!canExecute} onClick={() => exec.mutate()}>
              {exec.isPending ? t("reapConfirm.reapingLabel") : t("reapConfirm.execute")}
            </button>
          </div>
        </div>
      )}
    </ModalShell>
  );
}
