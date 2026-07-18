// SPDX-License-Identifier: AGPL-3.0-or-later
//
// The reap plan: build it, read every step, dry-run it, and — through the confirmation
// sheet — execute it.
//
// This is where the owner sees exactly what Reaper *would* do, down to the literal HTTP
// request each deletion would issue. Building a plan and dry-running it delete nothing; the
// dry run walks every interlock and sends nothing. Executing goes through ReapConfirm, which
// requires deletion armed on the host and the exact typed confirmation phrase before it
// deletes. While deletion is off the Execute button is disabled outright, with the shortest
// path to the switch beside it — the server would refuse anyway; the UI just stops inviting
// a click that must fail. A plan built here covers the whole condemned set (capped); to reap
// a hand-picked few, select them in the review queue and use "Reap now".

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { api, type Run, type RunReport } from "../api";
import { bytes, count, date } from "../format";
import { GracePanel } from "./GracePanel";
import { ReapConfirm } from "./ReapConfirm";

function Steps({ run }: { run: Run }) {
  return (
    // The Request column holds a full API path and a JSON body, so the table has a wider
    // minimum than a phone. The wrapper keeps that scroll sideways inside the table instead
    // of pushing the whole page sideways.
    <div className="table-scroll">
      <table className="plan-steps">
        <thead>
          <tr>
            <th>#</th>
            <th>Action</th>
            <th>Request</th>
            <th>State</th>
          </tr>
        </thead>
        <tbody>
          {run.steps.map((step) => (
            // A TV season emits three steps sharing one media_key AND ordinal (unmonitor,
            // verify, delete-files), so media_key alone collides. kind is unique within a
            // season, so media_key+kind is stable per row and reconciles states correctly.
            <tr key={`${step.media_key}-${step.kind}`}>
              <td className="num">
                {step.ordinal}
                {step.is_canary && <span className="canary-tag">test item</span>}
              </td>
              <td>{step.kind.replace(/_/g, " ")}</td>
              <td>
                <code>
                  {step.method} {step.path}
                </code>
                {step.body && <code className="step-body">{JSON.stringify(step.body)}</code>}
              </td>
              <td className="muted">{step.state}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function Report({ report }: { report: RunReport }) {
  if (report.state === "aborted") {
    return (
      <div className="sim sim-info">
        <h3>The run aborted. Nothing was touched</h3>
        <p>{report.aborted_reason}</p>
      </div>
    );
  }
  return (
    <div className="sim">
      <p className="blurb">
        Dry run complete. Every safety check ran; <strong>{count(report.would_delete_items)}</strong>{" "}
        items were actually deleted (it is a dry run, so this is zero), and{" "}
        {count(report.outcomes.length)} steps were walked.
      </p>
      <ul className="dryrun-outcomes">
        {report.outcomes.slice(0, 50).map((o, i) => (
          // Outcomes can repeat a media_key (a season walks several steps), so pair it with
          // the index to keep sibling keys unique.
          <li key={`${o.media_key}-${i}`}>
            <span className="gate-mark">✓</span>
            <code>{o.detail}</code>
          </li>
        ))}
      </ul>
      {report.outcomes.length > 50 && (
        <p className="muted">…and {count(report.outcomes.length - 50)} more.</p>
      )}
    </div>
  );
}

export function ReapPlan({
  onGoToDeletion,
  onGoToPlexSettings,
  onOpenReasons,
}: {
  /** Jump to Policy → Deletion, where the switch lives. */
  onGoToDeletion: () => void;
  /** Jump to Settings → Plex, where the Leaving Soon switches live. */
  onGoToPlexSettings: () => void;
  /** Open one item's reasoning on the review screen, for the countdown list below. */
  onOpenReasons: (candidateId: number) => void;
}) {
  const queryClient = useQueryClient();
  const [run, setRun] = useState<Run | null>(null);
  const [report, setReport] = useState<RunReport | null>(null);
  const [confirming, setConfirming] = useState(false);

  // The same query key the deletion toggle and the banner use, so arming updates all
  // three in one render pass. Unknown must gate like off: a plan must not offer Execute
  // on a safety state we could not read.
  const safety = useQuery({ queryKey: ["safety"], queryFn: api.safety });
  const armed = safety.data?.destructive_enabled ?? false;

  const plan = useMutation({
    mutationFn: () => api.createRun(),
    onSuccess: (r) => {
      setRun(r);
      setReport(null);
      void queryClient.invalidateQueries({ queryKey: ["runs"] });
    },
  });

  const dry = useMutation({
    mutationFn: (id: number) => api.dryRun(id),
    onSuccess: setReport,
  });

  const { data: history } = useQuery({ queryKey: ["runs"], queryFn: api.runs });

  // Shares the app's snapshot query, so this costs no extra request. A plan is frozen
  // against the scan it was built from; if a newer scan has landed since, the plan's
  // list is out of date and the owner should be told before they act on it.
  // A 404 is the normal no-scan-yet state, not a failure worth retrying.
  const { data: latestSnapshot } = useQuery({
    queryKey: ["snapshot"],
    queryFn: api.latestSnapshot,
    retry: false,
  });

  return (
    <section className="reap">
      <div className="reap-head">
        <h2>Reap plan</h2>
        <button className="primary" onClick={() => plan.mutate()} disabled={plan.isPending}>
          {plan.isPending ? "Planning…" : "Build a plan from the last scan"}
        </button>
      </div>
      <p className="blurb">
        A plan records exactly what a reap <em>would</em> do: the literal request behind every
        deletion. It can be dry-run end to end. Nothing here deletes anything.
      </p>

      <GracePanel onGoToPlexSettings={onGoToPlexSettings} onOpenReasons={onOpenReasons} />

      {plan.error && <p className="notice notice-error">{plan.error.message}</p>}

      {run && (
        <>
          <div className="plan-summary">
            <span className="confirm-phrase">{run.confirmation_phrase}</span>
            <span className="muted">
              {count(run.item_count)} items · {bytes(run.total_bytes)} · smallest first, and the
              first item is a test: if it doesn't go exactly as planned, the run stops.
            </span>
            <button onClick={() => dry.mutate(run.id)} disabled={dry.isPending}>
              {dry.isPending ? "Dry-running…" : "Dry run"}
            </button>
            {run.state === "planned" && (
              <button
                className="danger"
                disabled={!armed}
                title={armed ? undefined : "Turn deletion on first"}
                onClick={() => setConfirming(true)}
              >
                Execute…
              </button>
            )}
            {run.state === "planned" && !armed && (
              <span className="exec-note">
                {safety.isPending ? (
                  "Checking whether deletion is on…"
                ) : (
                  <>
                    {safety.isError || !safety.data
                      ? "Reaper couldn't confirm whether deletion is on, so this plan can't run from here."
                      : "Deletion is off, so this plan can't run."}{" "}
                    <button className="link" onClick={onGoToDeletion}>
                      Turn it on in Policy → Deletion
                    </button>
                  </>
                )}
              </span>
            )}
          </div>
          {dry.error && <p className="notice notice-error">{dry.error.message}</p>}
          {report && <Report report={report} />}
          <Steps run={run} />
        </>
      )}

      {confirming && run && (
        <ReapConfirm
          run={run}
          onClose={() => setConfirming(false)}
          onDone={() => void queryClient.invalidateQueries({ queryKey: ["runs"] })}
        />
      )}

      {history && history.length > 0 && (
        <div className="run-history">
          <h3>Recent plans</h3>
          <ul>
            {history.map((r) => {
              // Clicking a row swaps the plan shown above, so the row that is open says
              // so rather than leaving the swap silent.
              const open = run?.id === r.id;
              const olderScan = latestSnapshot != null && r.snapshot_id !== latestSnapshot.id;
              const openNote = olderScan
                ? " · open above, built from an older scan"
                : " · open above";
              return (
                <li key={r.id} className={open ? "open" : undefined}>
                  <button
                    className="link"
                    onClick={() => setRun(r)}
                    aria-current={open ? "true" : undefined}
                  >
                    #{r.id}
                  </button>{" "}
                  <span className="muted">
                    {date(r.approved_at)} · {r.state} · {r.confirmation_phrase}
                    {open && openNote}
                  </span>
                </li>
              );
            })}
          </ul>
        </div>
      )}
    </section>
  );
}
