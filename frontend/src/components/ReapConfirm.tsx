// SPDX-License-Identifier: AGPL-3.0-or-later
//
// The reap confirmation — the one place in the UI that deletes.
//
// A deliberate gauntlet, and every gate resolves toward NOT deleting:
//   1. A dry run walks the whole plan and sends nothing. Execute stays disabled until it
//      completes cleanly — you cannot reap a plan that hasn't proven itself.
//   2. Deletion must be armed on the host (Policy → Deletion). If it's off, we say so and
//      point there; there is no way to arm it from here. A safety state we could not read
//      is never reported as "off": pending says we're checking, a failed read says we
//      couldn't look. Execute stays disabled through all three.
//   3. You must type the exact content-bound phrase ("REAP 1 ITEMS 0 GB"). It carries the
//      count and size, so muscle memory can't carry you through and a stale plan reads as
//      obviously different. The server recomputes it and refuses anything else.
// Only when all three are satisfied does the Execute button light up.

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { api, type Run, type RunReport } from "../api";
import { bytes, count } from "../format";
import { ModalShell } from "./ModalShell";

/** The Reaper's tally: "1 soul", "7 souls". */
function souls(n: number): string {
  return `${n.toLocaleString()} ${n === 1 ? "soul" : "souls"}`;
}

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
  const [report, setReport] = useState<RunReport | null>(null);

  const safety = useQuery({ queryKey: ["safety"], queryFn: api.safety });
  const armed = safety.data?.destructive_enabled === true;

  const dry = useMutation({
    mutationFn: () => api.dryRun(run.id),
    onSuccess: setReport,
  });

  const exec = useMutation({
    mutationFn: () => api.executeRun(run.id, run.confirmation_phrase),
    onSuccess: (r) => {
      setReport(r);
      onDone?.();
    },
    // onSettled, not onSuccess: even a run that errored or aborted partway may have
    // deleted items (the canary, the first few steps), so the queue and run history must
    // refresh either way. queryClient comes from useQueryClient() and is stable.
    onSettled: () => {
      void queryClient.invalidateQueries({ queryKey: ["runs"] });
      void queryClient.invalidateQueries({ queryKey: ["candidates"] });
    },
  });

  // Prove the plan the moment the sheet opens. Nothing is sent; this only walks interlocks.
  useEffect(() => {
    dry.mutate();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [run.id]);

  const executed = report?.dry_run === false;
  const dryClean = report?.dry_run === true && report.state === "completed";
  const phraseOk = typed.trim() === run.confirmation_phrase;
  const canExecute = armed && dryClean && phraseOk && !exec.isPending;

  // While the real reap is in flight the sheet must stay open: unmounting would lose the
  // per-item report of what was just deleted. Cancel goes through here, and the shell's
  // scrim, ✕ and Escape go through the same predicate as `canClose`.
  const close = () => {
    if (!exec.isPending) onClose();
  };

  return (
    <ModalShell
      title={`Reap ${count(run.item_count)} items`}
      onClose={close}
      canClose={!exec.isPending}
      className="reap-confirm"
    >
      <p className="reap-confirm-phrase">{run.confirmation_phrase}</p>
      <p className="muted small">
        {count(run.item_count)} items · {bytes(run.total_bytes)} · smallest first, and the first
        item is a test: if it doesn't go exactly as planned, the run stops. This removes the
        files through Sonarr/Radarr and adds an import exclusion so they won't silently
        re-download.
      </p>

      {/* Said again here, not only on the plan screen: this is the last surface before
          the files go, and the count above is smaller than the queue's for a reason the
          owner is entitled to know while deciding. */}
      {run.held_back_unknown_size > 0 && (
        <p className="notice notice-warn">
          {count(run.held_back_unknown_size)}{" "}
          {run.held_back_unknown_size === 1 ? "item is" : "items are"} held back. Reaper couldn't
          measure {run.held_back_unknown_size === 1 ? "its" : "their"} size, so it won't delete{" "}
          {run.held_back_unknown_size === 1 ? "it" : "them"}.
        </p>
      )}

      {/* Stage 1 — the dry run */}
      {dry.isPending && <p className="blurb">Checking every safety stop with a practice run…</p>}
      {dry.error && (
        <p className="notice notice-error">
          The dry run failed, so nothing can be executed: {dry.error.message}
        </p>
      )}
      {report?.dry_run && report.state === "aborted" && (
        <div className="sim sim-info">
          <strong>The plan aborted. Nothing would be touched.</strong>
          <p>{report.aborted_reason}</p>
        </div>
      )}
      {dryClean && (
        <p className="dry-ok">
          <span className="gate-mark">✓</span> Dry run passed: the plan is sound, and it sent
          nothing.
        </p>
      )}

      {/* Stage 2 — arm + typed confirmation, shown once the dry run is clean */}
      {dryClean && !executed && (
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
            <button className="ghost" onClick={close} disabled={exec.isPending}>
              Cancel
            </button>
            <button className="danger" disabled={!canExecute} onClick={() => exec.mutate()}>
              {exec.isPending ? "Reaping…" : `Reap ${count(run.item_count)} items`}
            </button>
          </div>
        </div>
      )}

      {/* Stage 3 — the result of a real run, as a per-item checklist */}
      {executed && report && (
        <div className="reap-result">
          <div className="reap-tally">
            <strong className="reap-souls">
              {souls(report.would_delete_items)} reclaimed
            </strong>
            <span className="muted">
              {bytes(report.deleted_bytes)} freed
              {report.skipped > 0 && ` · ${count(report.skipped)} spared at the last moment`}
            </span>
          </div>
          {report.state === "aborted" && report.aborted_reason && (
            <p className="reap-halt">Run halted: {report.aborted_reason}</p>
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
