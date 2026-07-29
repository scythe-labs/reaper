// SPDX-License-Identifier: AGPL-3.0-or-later
//
// The reap confirmation — the one place in the UI that starts a deletion.
//
// A deliberate gauntlet, and every gate resolves toward NOT deleting:
//   1. A dry run walks the whole plan and sends nothing. Execute stays disabled until it
//      completes cleanly — you cannot reap a plan that hasn't proven itself.
//   2. Deletion must be armed on the host (Policy → Deletion). If it's off, we say so and
//      point there; there is no way to arm it from here. A safety state we could not read
//      is never reported as "off": pending says we're checking, a failed read says we
//      couldn't look. Execute stays disabled through all three.
//   3. You must type the exact content-bound phrase ("REAP 1 SOUL 0 GB"). It carries the
//      count and size, so muscle memory can't carry you through and a stale plan reads as
//      obviously different. The server recomputes it and refuses anything else.
//
// Once started, the reap runs DETACHED on the server (like a scan): this sheet polls its
// status, shows live progress, and offers Stop. Closing the sheet no longer stops or loses
// anything — the run keeps going, the app-wide reap bar carries the count and Stop to every
// screen, and reopening shows the report. Stop is graceful: the run halts after the item in
// flight and still tidies Plex for what was removed.
//
// **Every stage says itself out loud, and none of them did.** `ModalShell` announces the dialog
// once at open, and from that instant the body mutated on a poll and said nothing: practice run
// pending, then passed, then the arm block and the typed-phrase field APPEARING, then progress,
// then the report. The operator was asked to type a content-bound phrase into a box they were
// never told had arrived, and a run that deleted their files finished in silence (#170). The
// stages now `announce()` and the phrase field takes focus when it mounts; the progress bar
// carries `role="progressbar"` with an `aria-valuetext` a person would say, and its ticks are
// throttled to phase changes and tenths, never per item — a run of 400 files polling every
// second would otherwise talk over everything else in the app.

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useCallback, useEffect, useRef, useState } from "react";
import { announce } from "../announce";
import { ApiError, api, type ReapStatus, type Run, type RunReport } from "../api";
import { bytes, count, souls } from "../format";
import { usePlexTrash, trashWarning } from "../usePlexTrash";
import { useSafety } from "../useSafety";
import { ModalShell } from "./ModalShell";
import { PlexTrashNotice } from "./PlexTrashNotice";
import { Notice } from "./Notice";

export function ReapConfirm({
  run: openedWith,
  onClose,
  onDone,
}: {
  /** The plan to confirm, as the caller holds it. It seeds the shared cache entry below; only
   *  its id is relied on afterwards, so a caller holding a captured copy is not a problem. */
  run: Run;
  onClose: () => void;
  onDone?: () => void;
}) {
  const queryClient = useQueryClient();
  // The plan, read through the one cache key every surface uses for it. The caller's copy seeds
  // it, so opening the sheet never waits on (or costs) a fetch.
  //
  // The observer is the point: the 409 recovery below invalidates ["run", id], and an
  // invalidation only reaches a component WATCHING that key. The reap plan page happens to hold
  // its run through it, but "Reap now" in the review queue hands over a captured object with no
  // observer at all, so on that path the recovery changed nothing -- the sheet kept measuring
  // against a phrase the server had already moved past, and typing the phrase the error itself
  // quoted left the button disabled. Watching the key here fixes both paths at once, instead of
  // one path getting the behavior the comment claims (rule 24).
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

  // The live reap status, shared with the app-wide reap bar (one cache key, one poll). Read
  // on open so this sheet re-attaches to a run already in flight, and polled while running.
  // Idle still polls, slowly: a reap started from a phone or a second tab must reach an
  // already-open sheet, which is otherwise still offering Execute for a plan now in flight.
  const reap = useQuery({
    queryKey: ["reapStatus"],
    queryFn: api.reapStatus,
    refetchInterval: (q) => (q.state.data?.running ? 1000 : 15000),
  });
  const status = reap.data;
  const mine = status?.run_id === run.id;
  const running = !!status?.running && mine;
  const stopping = !!status?.stopping && mine;
  // A DIFFERENT run holds the single reap slot. The arm+confirm stage must not present itself
  // as ready to fire while it does (the server would 409 a second execute anyway).
  const otherRunning = !!status?.running && !mine;
  // The after-action report lands on the status when the run ends; only this run's own.
  const report = mine && status && !status.running ? status.report : null;
  // The run raised instead of finishing -- deletion switched off mid-run, a cap breached, a
  // failed canary, a crash after N files were already gone. There is no report to show, and
  // files may already be deleted, so the confirm stage must NOT re-arm itself in silence:
  // say what happened and offer nothing but Done.
  const failed = mine && !!status && !status.running && status.phase === "error";

  const exec = useMutation({
    // What the operator typed, verbatim -- never the phrase this sheet already holds. The
    // server re-derives the expected phrase live, so posting our own copy would reduce the
    // human gate to a `disabled` attribute the server cannot tell from an echo, and would
    // deadlock the moment the expected phrase moved under an open sheet.
    mutationFn: () => api.executeRun(run.id, typed.trim()),
    // Seed the shared status so "running" shows at once, without waiting for the first poll.
    onSuccess: (s) => queryClient.setQueryData(["reapStatus"], s),
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

  const stop = useMutation({
    mutationFn: () => api.stopRun(run.id),
    onSuccess: (s) => queryClient.setQueryData(["reapStatus"], s),
  });

  // When the run ends, let the parent react (e.g. clear a selection). Refreshing the caches
  // the reap invalidated is NOT done here: this sheet is meant to be closed mid-run, so the
  // work lives on the always-mounted reap bar (App.tsx), which cannot be unmounted out of it.
  const endedRef = useRef(false);
  useEffect(() => {
    if ((report || failed) && !endedRef.current) {
      endedRef.current = true;
      onDone?.();
    }
  }, [report, failed, onDone]);

  // Prove the plan the moment the sheet opens. Nothing is sent; this only walks interlocks.
  // Skipped when reopening a run already in flight, finished, or failed (via the bar's View):
  // the executor refuses a dry run on a non-PLANNED run, and its "practice run" blurb must
  // never render over live progress, the report, or a failure.
  // Keyed on the PHRASE, not just the run id. The phrase is content-bound (rule 73), so the
  // server moving it is the server telling us this plan now covers different items -- which is
  // exactly what the 409 recovery above refetches. Re-proving on that change is what stops the
  // sheet showing "Practice run passed" from the plan the server has already rejected while
  // every figure beside it describes the new one: a green tick asserting a practice run that
  // never happened for this content (rule 85). The run id alone never changes here, so the
  // effect ran once and the stale report sat there through the recovery.
  useEffect(() => {
    const s = queryClient.getQueryData<ReapStatus>(["reapStatus"]);
    const active = s?.run_id === run.id && (s.running || s.report != null || s.phase === "error");
    setDryReport(null);
    // The phrase is content-bound, so it moving means this sheet now covers different
    // items. A tick that survived that would be consent carried from a plan the operator
    // is no longer looking at.
    setTrashAcked(false);
    if (!active) dry.mutate();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [run.id, run.confirmation_phrase]);

  const dryClean = dryReport?.dry_run === true && dryReport.state === "completed";
  const phraseOk = typed.trim() === run.confirmation_phrase;
  // Emptying Plex's trash takes the records of everything already in there, not just what
  // this run deleted, and the executor's count-delta gate structurally cannot see those
  // items. So the operator is told and must tick before Reap enables. The tick is dropped
  // whenever the plan's content changes (the effect below), so consent from one plan can
  // never carry into a different one.
  const trash = usePlexTrash();
  const warn = trashWarning(trash.data, trash.isError);
  const trashOk = !warn.show || trashAcked;
  const canExecute =
    armed && dryClean && phraseOk && trashOk && !exec.isPending && !running && !failed;
  const pct = status && status.total > 0 ? Math.round((status.done / status.total) * 100) : 0;

  // The typed-phrase field appears part-way through a dialog the operator is already standing
  // in, on a poll rather than on anything they did, so nothing carries them to it. Focus goes
  // there when it mounts: it is the only thing the stage asks for, and this is the last gate
  // before files are deleted.
  const phraseRef = useRef<HTMLInputElement>(null);
  const armStage = dryClean && !running && !report && !failed && !otherRunning && armed;
  useEffect(() => {
    if (armStage) phraseRef.current?.focus();
  }, [armStage]);

  // The dialog's purpose changes when the run ends -- from "confirm this" to "read what
  // happened" -- so focus moves to the outcome rather than leaving the operator standing on a
  // stage that is no longer on screen.
  const outcomeRef = useRef<HTMLDivElement>(null);
  const ended = !!report || failed;
  useEffect(() => {
    if (ended) outcomeRef.current?.focus();
  }, [ended]);

  // What the run has said so far, so a poll that changes nothing says nothing. `null` until the
  // first announcement, which is what keeps a sheet REOPENED onto a finished run from
  // announcing a report the operator has already heard once.
  const spokenRef = useRef<string | null>(null);
  // `useCallback` with no dependencies, so the effect below can name it honestly in its own
  // deps rather than reaching for a disable directive: everything it reads is a ref.
  const say = useCallback((line: string) => {
    if (spokenRef.current === line) return;
    spokenRef.current = line;
    announce(line);
  }, []);
  // Stage announcements, in the order an operator meets them. Tenths and not percent: the
  // status polls every second, so a per-item or per-percent line on a run of hundreds would
  // hold the app's one polite region for the length of the run. `Math.floor(pct / 10)` makes
  // "40% deleted." at most ten sentences whatever the run's size.
  //
  // The run's END is deliberately NOT here. `ReapBar` announces it, and that bar is mounted on
  // every screen where this sheet is only mounted while the operator has it open -- so the bar
  // is the one that always fires, and a copy here would say it twice to anyone still standing
  // in the sheet (rule 144: one fact, one place that states it). What this owns is the stages
  // the bar cannot see: the practice run, the phrase field arriving, and the progress ticks,
  // which belong to the surface the operator opened to watch them.
  const tenth = Math.floor(pct / 10);
  useEffect(() => {
    if (report || failed) return;
    if (running)
      return say(stopping ? "Stopping after the current one." : `${tenth * 10}% deleted.`);
    if (!dryClean) return;
    if (!armed) return say("Practice run passed, but deletion is off, so nothing can be reaped.");
    // `pct` is deliberately absent from the deps: `tenth` is the throttle, and depending on the
    // percent would re-run this on every poll to re-derive the same sentence.
    say("Practice run passed. Type the confirmation phrase to reap.");
  }, [failed, report, running, stopping, tenth, dryClean, armed, say]);

  return (
    <ModalShell title={`Reap ${souls(run.item_count)}`} onClose={onClose} className="reap-confirm">
      <p className="reap-confirm-phrase">{run.confirmation_phrase}</p>
      <p className="muted small">
        {souls(run.item_count)} · {bytes(run.total_bytes)} · smallest first, and the first is a
        test: if it doesn't go exactly as planned, the run stops. This removes the files through
        Sonarr/Radarr and adds an import exclusion so they won't silently re-download.
      </p>

      {/* Said again here, not only on the plan screen: this is the last surface before
          the files go, and the count above is smaller than the queue's for a reason the
          owner is entitled to know while deciding. */}
      {run.held_back_unknown_size > 0 && (
        <Notice tone="warn">
          {souls(run.held_back_unknown_size)} {run.held_back_unknown_size === 1 ? "is" : "are"} held
          back. Reaper couldn't measure {run.held_back_unknown_size === 1 ? "its" : "their"} size,
          so it won't delete {run.held_back_unknown_size === 1 ? "it" : "them"}.
        </Notice>
      )}

      {/* Stage 1 — the practice run ("dry run" is still the API's and the executor's word for
          it; every operator-facing string in this flow says practice run, U-15). Every block
          here is gated on !running && !report && !failed so
          none of it can render over live progress, the finished report, or a failure (e.g.
          after reopening a live run from the app-wide bar). */}
      {!running && !report && !failed && (
        <>
          {dry.isPending && (
            <p className="blurb">Checking every safety stop with a practice run…</p>
          )}
          {dry.error && (
            <Notice tone="error">
              The practice run failed, so nothing can be executed: {dry.error.message}
            </Notice>
          )}
          {dryReport?.dry_run && dryReport.state === "aborted" && (
            <div className="sim sim-info">
              {/* "stopped", the one word the product uses for this (U-15). */}
              <strong>The plan stopped. Nothing would be touched.</strong>
              <p>{dryReport.aborted_reason}</p>
            </div>
          )}
          {dryClean && (
            <p className="dry-ok">
              <span className="gate-mark" aria-hidden="true">
                ✓
              </span>{" "}
              Practice run passed: the plan is sound, and it sent nothing.
            </p>
          )}
        </>
      )}

      {/* Another reap holds the single slot: say so, rather than lighting a Reap button the
          server would refuse. */}
      {!running && !report && !failed && otherRunning && (
        <Notice tone="warn">
          Another reap is running. Wait for it to finish, then reopen this to reap.
        </Notice>
      )}

      {/* Plex's trash takes more than this reap deletes, so say so before the phrase field
          and hold Reap until it is acknowledged. Rendered in the same stage gate as the arm
          block: it is a decision about sending, so it must not sit over live progress, a
          finished report, or a failure. */}
      {warn.show && !running && !report && !failed && !otherRunning && (
        <PlexTrashNotice
          known={warn.known}
          unreadable={warn.unreadable}
          acked={trashAcked}
          onAck={setTrashAcked}
        />
      )}

      {/* Stage 2 — arm + typed confirmation, shown once the practice run is clean and nothing is
          running or finished (here or elsewhere) yet. */}
      {dryClean && !running && !report && !failed && !otherRunning && (
        <div className="reap-arm">
          {!armed ? (
            // Three states, never one definite claim: only a switch we actually read may
            // be reported as off.
            safety.isPending ? (
              <p className="reap-disarmed">Checking whether deletion is on…</p>
            ) : safety.isError || !safety.data ? (
              <Notice tone="warn">
                Reaper couldn't confirm whether deletion is on, so nothing can be reaped from here.
                Check <em>Policy → Deletion</em>, then reload this page.
              </Notice>
            ) : (
              <p className="reap-disarmed">
                Deletion is <strong>off</strong>. Turn it on in <em>Policy → Deletion</em> (it asks
                for your admin password), then come back here.
              </p>
            )
          ) : (
            <>
              <label className="reap-confirm-label" htmlFor="reap-phrase">
                Type <code>{run.confirmation_phrase}</code> to confirm:
              </label>
              <input
                ref={phraseRef}
                id="reap-phrase"
                className="reap-confirm-input"
                autoComplete="off"
                spellCheck={false}
                value={typed}
                placeholder={run.confirmation_phrase}
                onChange={(e) => setTyped(e.target.value)}
              />
            </>
          )}
          {exec.error && <Notice tone="error">{exec.error.message}</Notice>}
          <div className="reap-confirm-actions">
            <button className="ghost" onClick={onClose} disabled={exec.isPending}>
              Cancel
            </button>
            <button className="danger" disabled={!canExecute} onClick={() => exec.mutate()}>
              {exec.isPending ? "Reaping…" : `Reap ${souls(run.item_count)}`}
            </button>
          </div>
        </div>
      )}

      {/* Reaping — live progress and a graceful Stop, while this run is in flight. Closing
          the sheet here leaves the run going; the app-wide bar keeps the count and Stop. */}
      {running && status && (
        <div className="reap-arm">
          <div className="reap-progress">
            <div className="prog-head">
              <span className="prog-count">
                {count(status.done)} of {count(status.total)} souls
              </span>
              <span className="prog-note">
                {bytes(status.deleted_bytes)} freed
                {status.skipped > 0 && ` · ${count(status.skipped)} spared`}
              </span>
            </div>
            {/* A bare `<div>` with an inline width is a picture of a number and nothing else.
                `aria-valuetext` carries the counts a person would actually say, rather than
                leaving a reader to read out "62". A progressbar is not a live region, so this
                is read when the operator asks for it -- the announcing is throttled separately,
                above (`ScanLine` in App.tsx is the same shape, rule 72). */}
            <div
              className="prog-track"
              role="progressbar"
              aria-label="Reaping"
              aria-valuemin={0}
              aria-valuemax={100}
              aria-valuenow={pct}
              aria-valuetext={`${pct}%, ${count(status.done)} of ${count(status.total)} removed`}
            >
              <div className="prog-fill" style={{ width: `${pct}%` }} />
            </div>
          </div>
          {stop.error && <Notice tone="error">{stop.error.message}</Notice>}
          <div className="reap-confirm-actions">
            <span className={`reap-running ${stopping ? "stopping" : "deleting"}`}>
              <span className="spinner" aria-hidden="true" />
              {stopping ? "Stopping after the current one…" : "Reaping…"}
            </span>
            <button
              className="stop-btn"
              disabled={stopping || stop.isPending}
              onClick={() => stop.mutate()}
            >
              {stopping ? "Stopping…" : "Stop"}
            </button>
          </div>
        </div>
      )}

      {/* Stopped by a problem. There is no report, and files may already be gone, so the one
          thing on offer is Done: re-arming the phrase here would invite a second execute the
          server refuses just as silently. What was removed is on the reap bar's report. */}
      {failed && (
        // Focus lands here when the run ends: the dialog has stopped being a confirmation and
        // become the only account of what happened to files that are already gone.
        <div className="reap-arm" ref={outcomeRef} tabIndex={-1}>
          <Notice tone="error">
            <strong>The reap stopped on a problem.</strong>{" "}
            {status?.error ?? "Reaper couldn't say what went wrong."} Anything already removed is
            gone; check the Review queue after the next scan.
          </Notice>
          <div className="reap-confirm-actions">
            <button className="primary" onClick={onClose}>
              Done
            </button>
          </div>
        </div>
      )}

      {/* Result — the after-action checklist, from the finished run's status. */}
      {report && (
        // Same move as the failure branch above: the run is over, and this is what the operator
        // is here for now.
        <div className="reap-result" ref={outcomeRef} tabIndex={-1}>
          <div className="reap-tally">
            <strong className="reap-souls">{souls(report.would_delete_items)} reclaimed</strong>
            <span className="muted">
              {/* The count above covers every item; this covers only the ones with a
                  size. When they differ, say so, rather than letting the byte figure
                  read as the whole story. */}
              {bytes(report.deleted_bytes)} freed
              {report.deleted_unmeasured > 0 &&
                ` · ${count(report.deleted_unmeasured)} of unknown size`}
              {report.skipped > 0 && ` · ${count(report.skipped)} spared at the last moment`}
            </span>
          </div>
          {report.state === "aborted" && (
            <p className="reap-halt">
              {report.aborted_reason}
              {report.would_delete_items > 0 &&
                " Plex is refreshed for what was removed, and a fresh scan is running so the queue matches your library."}
            </p>
          )}
          <ul className="reap-checklist">
            {report.outcomes.map((o) => (
              <li key={o.media_key} className={`reap-item state-${o.state}`}>
                <span className="reap-item-title">{o.title || o.media_key}</span>
                <ul className="reap-checks">
                  {/* Pass and fail were a glyph and a color, and NVDA at its default symbol
                      level speaks neither ✓ nor ✗ -- so the two lines read out identically, in
                      the report for a run that has just deleted files. The word carries it and
                      the glyph goes decorative (the same split `Notice` makes for its tone). */}
                  {o.checks.map((c, i) => (
                    <li key={i} className={c.ok ? "check-ok" : "check-bad"}>
                      <span className="gate-mark" aria-hidden="true">
                        {c.ok ? "✓" : "✗"}
                      </span>
                      <span className="sr-only">{c.ok ? "Passed: " : "Failed: "}</span>
                      {c.label}
                    </li>
                  ))}
                </ul>
              </li>
            ))}
          </ul>
          <div className="reap-confirm-actions">
            <button className="primary" onClick={onClose}>
              Done
            </button>
          </div>
        </div>
      )}
    </ModalShell>
  );
}
