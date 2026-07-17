// SPDX-License-Identifier: AGPL-3.0-or-later
//
// The reap plan: build it, read every step, dry-run it, and — through the confirmation
// sheet — execute it.
//
// This is where the owner sees exactly what Reaper *would* do, down to the literal HTTP
// request each deletion would issue. Building a plan and dry-running it delete nothing; the
// dry run walks every interlock and sends nothing. Executing goes through ReapConfirm, which
// requires deletion armed on the host and the exact typed confirmation phrase before it
// deletes. A plan built here covers the whole condemned set (capped); to reap a hand-picked
// few, select them in the review queue and use "Reap now".

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { api, type Run, type RunReport } from "../api";
import { bytes, count } from "../format";
import { GracePanel } from "./GracePanel";
import { ReapConfirm } from "./ReapConfirm";

function Steps({ run }: { run: Run }) {
  return (
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
              {step.is_canary && <span className="canary-tag">canary</span>}
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
        Dry run complete. Every interlock ran; <strong>{count(report.would_delete_items)}</strong>{" "}
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

export function ReapPlan() {
  const queryClient = useQueryClient();
  const [run, setRun] = useState<Run | null>(null);
  const [report, setReport] = useState<RunReport | null>(null);
  const [confirming, setConfirming] = useState(false);

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

      <GracePanel />

      {plan.error && <p className="error">{plan.error.message}</p>}

      {run && (
        <>
          <div className="plan-summary">
            <span className="confirm-phrase">{run.confirmation_phrase}</span>
            <span className="muted">
              {count(run.item_count)} items · {bytes(run.total_bytes)} · smallest-first, canary
              leads
            </span>
            <button onClick={() => dry.mutate(run.id)} disabled={dry.isPending}>
              {dry.isPending ? "Dry-running…" : "Dry run"}
            </button>
            {run.state === "planned" && (
              <button className="danger" onClick={() => setConfirming(true)}>
                Execute…
              </button>
            )}
          </div>
          {dry.error && <p className="error">{dry.error.message}</p>}
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
            {history.map((r) => (
              <li key={r.id}>
                <button className="link" onClick={() => setRun(r)}>
                  #{r.id}
                </button>{" "}
                <span className="muted">
                  {r.state} · {r.confirmation_phrase}
                </span>
              </li>
            ))}
          </ul>
        </div>
      )}
    </section>
  );
}
