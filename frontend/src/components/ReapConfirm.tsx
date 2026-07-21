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

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useRef, useState } from "react";
import { api, type ReapStatus, type Run, type RunReport } from "../api";
import { bytes, count, souls } from "../format";
import { ModalShell } from "./ModalShell";

export function ReapConfirm({
  run,
  onClose,
  onDone,
}: {
  run: Run;
  onClose: () => void;
  onDone?: () => void;
}) {
  const queryClient = useQueryClient();
  const [typed, setTyped] = useState("");
  const [dryReport, setDryReport] = useState<RunReport | null>(null);

  const safety = useQuery({ queryKey: ["safety"], queryFn: api.safety });
  const armed = safety.data?.destructive_enabled === true;

  const dry = useMutation({
    mutationFn: () => api.dryRun(run.id),
    onSuccess: setDryReport,
  });

  // The live reap status, shared with the app-wide reap bar (one cache key, one poll). Read
  // on open so this sheet re-attaches to a run already in flight, and polled while running.
  const reap = useQuery({
    queryKey: ["reapStatus"],
    queryFn: api.reapStatus,
    refetchInterval: (q) => (q.state.data?.running ? 1000 : false),
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

  const exec = useMutation({
    mutationFn: () => api.executeRun(run.id, run.confirmation_phrase),
    // Seed the shared status so "running" shows at once, without waiting for the first poll.
    onSuccess: (s) => queryClient.setQueryData(["reapStatus"], s),
  });

  const stop = useMutation({
    mutationFn: () => api.stopRun(run.id),
    onSuccess: (s) => queryClient.setQueryData(["reapStatus"], s),
  });

  // When the run ends, refresh the queue and history once. The server also kicks a fresh
  // scan (removed files leave the queue and policy stale); its progress shows on the scan
  // line. onDone lets the parent react (e.g. clear a selection).
  const endedRef = useRef(false);
  useEffect(() => {
    if (report && !endedRef.current) {
      endedRef.current = true;
      void queryClient.invalidateQueries({ queryKey: ["runs"] });
      void queryClient.invalidateQueries({ queryKey: ["candidates"] });
      onDone?.();
    }
  }, [report, onDone, queryClient]);

  // Prove the plan the moment the sheet opens. Nothing is sent; this only walks interlocks.
  // Skipped when reopening a run already in flight or finished (via the bar's View): the
  // executor refuses a dry run on a non-PLANNED run, and its "practice run" blurb must never
  // render over live progress or the report.
  useEffect(() => {
    const s = queryClient.getQueryData<ReapStatus>(["reapStatus"]);
    const active = s?.run_id === run.id && (s.running || s.report != null);
    if (!active) dry.mutate();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [run.id]);

  const dryClean = dryReport?.dry_run === true && dryReport.state === "completed";
  const phraseOk = typed.trim() === run.confirmation_phrase;
  const canExecute = armed && dryClean && phraseOk && !exec.isPending && !running;
  const pct = status && status.total > 0 ? Math.round((status.done / status.total) * 100) : 0;

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
        <p className="notice notice-warn">
          {souls(run.held_back_unknown_size)}{" "}
          {run.held_back_unknown_size === 1 ? "is" : "are"} held back. Reaper couldn't measure{" "}
          {run.held_back_unknown_size === 1 ? "its" : "their"} size, so it won't delete{" "}
          {run.held_back_unknown_size === 1 ? "it" : "them"}.
        </p>
      )}

      {/* Stage 1 — the dry run. Every block here is gated on !running && !report so none of
          it can render over live progress or the finished report (e.g. after reopening a live
          run from the app-wide bar). */}
      {!running && !report && (
        <>
          {dry.isPending && (
            <p className="blurb">Checking every safety stop with a practice run…</p>
          )}
          {dry.error && (
            <p className="notice notice-error">
              The dry run failed, so nothing can be executed: {dry.error.message}
            </p>
          )}
          {dryReport?.dry_run && dryReport.state === "aborted" && (
            <div className="sim sim-info">
              <strong>The plan aborted. Nothing would be touched.</strong>
              <p>{dryReport.aborted_reason}</p>
            </div>
          )}
          {dryClean && (
            <p className="dry-ok">
              <span className="gate-mark">✓</span> Dry run passed: the plan is sound, and it sent
              nothing.
            </p>
          )}
        </>
      )}

      {/* Another reap holds the single slot: say so, rather than lighting a Reap button the
          server would refuse. */}
      {!running && !report && otherRunning && (
        <p className="notice notice-warn">
          Another reap is running. Wait for it to finish, then reopen this to reap.
        </p>
      )}

      {/* Stage 2 — arm + typed confirmation, shown once the dry run is clean and nothing is
          running or finished (here or elsewhere) yet. */}
      {dryClean && !running && !report && !otherRunning && (
        <div className="reap-arm">
          {!armed ? (
            // Three states, never one definite claim: only a switch we actually read may
            // be reported as off.
            safety.isPending ? (
              <p className="reap-disarmed">Checking whether deletion is on…</p>
            ) : safety.isError || !safety.data ? (
              <p className="notice notice-warn">
                Reaper couldn't confirm whether deletion is on, so nothing can be reaped
                from here. Check <em>Policy → Deletion</em>, then reload this page.
              </p>
            ) : (
              <p className="reap-disarmed">
                Deletion is <strong>off</strong>. Turn it on in <em>Policy → Deletion</em>{" "}
                (it asks for your admin password), then come back here.
              </p>
            )
          ) : (
            <>
              <label className="reap-confirm-label" htmlFor="reap-phrase">
                Type <code>{run.confirmation_phrase}</code> to confirm:
              </label>
              <input
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
          {exec.error && <p className="notice notice-error">{exec.error.message}</p>}
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
            <div className="prog-track">
              <div className="prog-fill" style={{ width: `${pct}%` }} />
            </div>
          </div>
          {stop.error && <p className="notice notice-error">{stop.error.message}</p>}
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

      {/* Result — the after-action checklist, from the finished run's status. */}
      {report && (
        <div className="reap-result">
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
                  {o.checks.map((c, i) => (
                    <li key={i} className={c.ok ? "check-ok" : "check-bad"}>
                      <span className="gate-mark">{c.ok ? "✓" : "✗"}</span>
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
