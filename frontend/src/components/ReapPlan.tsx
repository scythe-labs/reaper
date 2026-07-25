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
import { ApiError, api, type Run, type RunReport } from "../api";
import { bytes, count, date, souls } from "../format";
import { useSafety } from "../useSafety";
import { ReapBreakdown } from "./ReapBreakdown";
import { ReapConfirm } from "./ReapConfirm";

/** A stored run state, in the words the rest of the app uses for it. "Stopped", never
 *  "aborted" -- one word for one mechanism, the same as the reap bar and the report above
 *  (U-15). An unknown state (an older or newer build) reads through unchanged rather than
 *  being hidden. */
function runState(state: string): string {
  if (state === "planned") return "not run";
  if (state === "executing") return "running";
  if (state === "completed") return "done";
  if (state === "aborted") return "stopped";
  return state;
}

/** How many rows either long list on this page draws before saying how many more there are.
 *  One number for both, so the step table and the practice-run outcomes cut off together. */
const LIST_CAP = 50;

function Steps({ run }: { run: Run }) {
  // A first cleanup of 500 items is 1500 rows, each with a path and a stringified body, and
  // the table used to render every one of them synchronously -- on plan build, and again on
  // every history-row click (P-9). The first 50 are the ones that matter: the plan is ordered
  // smallest first, so step 0 is the canary the whole run turns on.
  const shown = run.steps.slice(0, LIST_CAP);
  const more = run.steps.length - shown.length;
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
          {shown.map((step) => (
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
      {more > 0 && (
        <p className="muted">
          …and {count(more)} more {more === 1 ? "step" : "steps"}, not shown. The run still
          covers every one of them.
        </p>
      )}
    </div>
  );
}

function Report({ report }: { report: RunReport }) {
  // "stopped", not "aborted": one word for one mechanism. The docs say caps stop the whole
  // run, and the app-wide reap bar already reports this exact state as "Stopped." (App.tsx).
  // "Abort" was operator vocabulary nowhere else in the product (U-15).
  if (report.state === "aborted") {
    return (
      <div className="sim sim-info">
        <h3>The run stopped. Nothing was touched</h3>
        <p>{report.aborted_reason}</p>
      </div>
    );
  }
  return (
    <div className="sim">
      {/* What the practice run PROVED, which is the only thing an operator can act on. It
          used to lead with "N souls were actually reaped", a number that is zero by
          construction here and so says nothing, and then called the per-item outcomes
          "steps" -- a plan of 3 seasons read "3 steps were walked" over the 9 journalled
          steps in the table below it (I-1). */}
      <p className="blurb">
        Practice run complete. Every safety check ran and nothing was sent.{" "}
        {report.skipped > 0 ? (
          <>
            <strong>{souls(report.outcomes.length)}</strong> were walked, and{" "}
            <strong>{count(report.skipped)}</strong> of them would be skipped.
          </>
        ) : (
          <>
            <strong>{souls(report.outcomes.length)}</strong> were walked end to end.
          </>
        )}
      </p>
      <ul className="dryrun-outcomes">
        {report.outcomes.slice(0, LIST_CAP).map((o) => (
          // One outcome per item, never more: executor._run_deletes records exactly one
          // StepOutcome per delete, so the item's own key is unique among siblings.
          <li key={o.media_key}>
            <span className="gate-mark">✓</span>
            <code>{o.detail}</code>
          </li>
        ))}
      </ul>
      {report.outcomes.length > LIST_CAP && (
        <p className="muted">…and {count(report.outcomes.length - LIST_CAP)} more.</p>
      )}
    </div>
  );
}

export function ReapPlan({
  onGoToDeletion,
  onGoToPlexSettings,
  onGoToReview,
}: {
  /** Jump to Policy → Deletion, where the switch lives. */
  onGoToDeletion: () => void;
  /** Jump to Settings → Plex, where the Leaving Soon shelf lives. */
  onGoToPlexSettings: () => void;
  /** Jump to the Review queue, where per-title decisions are made. */
  onGoToReview: () => void;
}) {
  const queryClient = useQueryClient();
  // The plan shown is held by ID and read through the cache, never captured as a local copy.
  // A captured run goes stale the moment the reap it describes finishes: its state stays
  // "planned" and its Execute button stays live over a run the server has already spent, and
  // clicking it dry-runs a completed run for no explanation but "the dry run failed".
  const [runId, setRunId] = useState<number | null>(null);
  const {
    data: run,
    isPending: runPending,
    error: runError,
  } = useQuery({
    queryKey: ["run", runId],
    queryFn: () => api.run(runId!),
    enabled: runId != null,
  });
  const [report, setReport] = useState<RunReport | null>(null);
  const [confirming, setConfirming] = useState(false);

  /** Show a plan we already hold in full: seed the cache, then point at it. */
  const showRun = (r: Run) => {
    queryClient.setQueryData(["run", r.id], r);
    setRunId(r.id);
  };

  // The same query the deletion toggle and the banner use, so arming updates all three in
  // one render pass. Unknown must gate like off: a plan must not offer Execute on a safety
  // state we could not read.
  const safety = useSafety();
  const armed = safety.data?.destructive_enabled ?? false;

  const plan = useMutation({
    mutationFn: () => api.createRun("all"),
    onSuccess: (r) => {
      showRun(r);
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
  const snapshot = useQuery({
    queryKey: ["snapshot"],
    queryFn: api.latestSnapshot,
    retry: false,
  });
  const latestSnapshot = snapshot.data;

  // What a superseded scan loses is the protection only the newer scan would have found: a
  // keep tag added in Sonarr or Radarr, a rating that moved. So the warning belongs beside
  // Execute, not in the history list below, and "we could not check" is said out loud
  // rather than rendering nothing.
  const staleRun = run != null && latestSnapshot != null && run.snapshot_id !== latestSnapshot.id;
  const staleUnknown = run != null && latestSnapshot == null;

  // The planner refuses a degraded snapshot outright, so offering Build over one only trades
  // a click for a 422. Said here, in the same words the scan job uses, before the ledger
  // below reads as a list Reaper is ready to act on.
  const degraded = latestSnapshot?.degraded === true;

  return (
    <section className="reap">
      <div className="reap-head">
        <h2>Reap plan</h2>
        <button
          className="primary"
          onClick={() => plan.mutate()}
          disabled={plan.isPending || degraded}
        >
          {plan.isPending ? "Planning…" : "Build a plan from the last scan"}
        </button>
      </div>
      <p className="blurb">
        A plan records exactly what a reap <em>would</em> do: the literal request behind every
        deletion. You can practice it end to end. Nothing here deletes anything.
      </p>

      {degraded && (
        <p className="notice notice-warn">
          <strong>This scan came back incomplete.</strong> {latestSnapshot?.degraded_reason} You
          can still look at it, but Reaper won't act on it, so a plan can't be built. Fix the
          source and scan again.
        </p>
      )}

      <ReapBreakdown onGoToPlexSettings={onGoToPlexSettings} onGoToReview={onGoToReview} />

      {plan.error && <p className="notice notice-error">{plan.error.message}</p>}

      {/* A plan is asked for but not in hand. Never render nothing here: the whole block below
          -- phrase, count, Execute, steps -- hangs off this one query, so a failed fetch used to
          unmount all of it silently and a click on a history row simply looked like it did
          nothing. Same two branches, same words, as the reap sheet's loader (App.tsx, rule 36). */}
      {runId != null && !run && (
        <p className={runPending ? "help" : "notice notice-error"}>
          {runPending
            ? "Loading the plan…"
            : runError instanceof ApiError && runError.status === 404
              ? "That plan is no longer available."
              : "Reaper couldn't load this plan. Reload the page to try again."}
        </p>
      )}

      {run && (
        <>
          <div className="plan-summary">
            <span className="confirm-phrase">{run.confirmation_phrase}</span>
            <span className="muted">
              {souls(run.item_count)} · {bytes(run.total_bytes)} · smallest first, and the first
              is a test: if it doesn't go exactly as planned, the run stops.
            </span>
            {/* The plan is smaller than the queue implied, and this is where the owner
                finds out. Silence here reads as "that was everything". */}
            {run.held_back_unknown_size > 0 && (
              <p className="notice notice-warn">
                {souls(run.held_back_unknown_size)}{" "}
                {run.held_back_unknown_size === 1 ? "is" : "are"} held back. Reaper couldn't measure{" "}
                {run.held_back_unknown_size === 1 ? "its" : "their"} size, so it won't delete{" "}
                {run.held_back_unknown_size === 1 ? "it" : "them"}.
              </p>
            )}
            {staleRun && (
              <p className="notice notice-warn">
                This plan came from an older scan, so it can list titles you have since
                protected.{" "}
                <button className="link" onClick={() => plan.mutate()} disabled={plan.isPending}>
                  Build a new plan
                </button>
              </p>
            )}
            {staleUnknown && (
              <p className="notice notice-warn">
                {snapshot.isPending ? (
                  "Checking whether this plan came from the latest scan…"
                ) : (
                  <>
                    Reaper couldn't check whether this plan came from the latest scan.{" "}
                    <button className="link" onClick={() => plan.mutate()} disabled={plan.isPending}>
                      Build a new plan
                    </button>
                  </>
                )}
              </p>
            )}
            <button onClick={() => dry.mutate(run.id)} disabled={dry.isPending}>
              {dry.isPending ? "Checking…" : "Practice run"}
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

      {/* No onDone: what a finished reap invalidates -- this run, the history, the queue, the
          ledger -- is refreshed by the app-wide reap bar, which cannot be closed mid-run. */}
      {confirming && run && <ReapConfirm run={run} onClose={() => setConfirming(false)} />}

      {history && history.length > 0 && (
        <div className="run-history">
          <h3>Recent plans</h3>
          <ul>
            {history.map((r) => {
              // Clicking a row swaps the plan shown above, so the row that is open says
              // so rather than leaving the swap silent. Whether that plan came from an
              // older scan is said once, up beside Execute, where the fix lives.
              const open = runId === r.id;
              return (
                <li key={r.id} className={open ? "open" : undefined}>
                  {/* Only the id: the row carries no plan of its own, so opening one asks
                      the server for it (the ["run", id] query above). A list that came with
                      every plan in full cost a whole snapshot's candidates per row (P-3). */}
                  <button
                    className="link"
                    onClick={() => setRunId(r.id)}
                    aria-current={open ? "true" : undefined}
                  >
                    #{r.id}
                  </button>{" "}
                  <span className="muted">
                    {date(r.approved_at)} · {runState(r.state)}
                    {open && " · open above"}
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
