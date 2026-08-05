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
//
// The same courtesy is now extended to the *other* thing the server refuses on: a run without
// Plex or Tautulli, whose 409 used to be the only place in the app that requirement was
// written down, and which fired after the confirmation phrase had already been typed (#383).

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { ApiError, api, type Run, type RunReport } from "../api";
import { bytes, count, date, souls } from "../format";
import { reapBlockers } from "../reapReadiness";
import { usePlexTrash, trashWarning } from "../usePlexTrash";
import { useSafety } from "../useSafety";
import { PlexTrashNotice } from "./PlexTrashNotice";
import { ReapBreakdown } from "./ReapBreakdown";
import { ReapConfirm } from "./ReapConfirm";
import { Notice } from "./Notice";

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
    //
    // And nothing inside the table is focusable, so that sideways scroll could not be reached
    // from a keyboard at all (WCAG 2.1.1, #177): no cell to tab onto and carry it with.
    // `tabIndex={0}` makes the wrapper its own stop, named so it is worth stopping on. Same
    // sweep as `.docs-content`, `.log-console`, `.dryrun-outcomes` and `docs/DocBody.tsx`'s
    // two (rule 72).
    <div className="table-scroll" tabIndex={0} role="region" aria-label="Plan steps">
      <table className="plan-steps">
        <thead>
          <tr>
            {/* `scope` explicitly, on the journalled record of what a run will send. A
                single-row `<thead>` is one browsers infer reliably, so the real-world harm is
                low -- but inferring is not the same as being told, and this is the table an
                operator reads to decide (#177). `docs/DocBody.tsx` is the twin (rule 72). */}
            <th scope="col">#</th>
            <th scope="col">Action</th>
            <th scope="col">Request</th>
            <th scope="col">State</th>
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
              {/* The stored reason sits under the state it explains, not in a column of its
                  own: this table already has a wider minimum than a phone (see .table-scroll
                  above) and a fifth column would push it further, for a cell that is empty on
                  every step that went fine. `error` is already operator copy -- the executor
                  writes one sentence and shows the same one live -- so the only thing new here
                  is that it survives a restart, which is exactly when the live copy is gone
                  and the table used to say "failed" and nothing else (#260). */}
              <td className="muted">
                {step.state}
                {step.error && <span className="step-why">{step.error}</span>}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      {more > 0 && (
        <p className="muted">
          …and {count(more)} more {more === 1 ? "step" : "steps"}, not shown. The run still covers
          every one of them.
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
      {/* Every row is text, so this list scrolls with nothing to tab onto (WCAG 2.1.1, #177).
          `tabIndex={0}` on the list itself keeps its `listitem`s intact where a wrapper with
          `role="region"` would not, and it is named for what it holds. Same sweep as the plan
          table above (rule 72). */}
      <ul className="dryrun-outcomes" tabIndex={0} aria-label="What the practice run walked">
        {report.outcomes.slice(0, LIST_CAP).map((o) => (
          // One outcome per item, never more: executor._run_deletes records exactly one
          // StepOutcome per delete, so the item's own key is unique among siblings.
          <li key={o.media_key}>
            {/* Decoration: every row in this list is a pass, so the tick adds nothing a
                  reader needs and lands mid-sentence as a stray character (#177). Where a
                  list can hold BOTH outcomes -- the reap report's per-item checks -- the
                  glyph is hidden and a word carries it instead (#170). */}
            <span className="gate-mark" aria-hidden="true">
              ✓
            </span>
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
  /** Jump to Settings → Plex: where the Leaving Soon shelf lives, and where an install with
   *  no Plex at all connects one. */
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

  // Only asked once a plan exists: with nothing to reap there is nothing to warn about, and
  // this read costs a round trip to Plex.
  const plexTrash = usePlexTrash(run != null);
  const planTrash = trashWarning(plexTrash.data, plexTrash.isError);

  // The planner refuses a degraded snapshot outright, so offering Build over one only trades
  // a click for a 422. Said here, in the same words the scan job uses, before the ledger
  // below reads as a list Reaper is ready to act on.
  const degraded = latestSnapshot?.degraded === true;

  // Whether a real run could go ahead at all. Without Plex or Tautulli the execute route
  // refuses with a 409 (`api/runs._preflight_refusal`), and until this was read here the ONLY
  // place that requirement appeared was inside the refusal -- which lands after the operator
  // has picked what to delete, armed the host and typed the whole confirmation phrase (#383).
  // The refusal is correct and stays; this is the same fact, four steps earlier.
  const setup = useQuery({ queryKey: ["setup"], queryFn: api.setupStatus });
  // Shared with the wizard's finish panel, so the two screens cannot come to describe the same
  // refusal differently (rule 144). Empty on a read we could not make -- the branch below says
  // so out loud rather than letting silence read as "nothing is missing" (rule 17/36).
  const blockers = setup.data ? reapBlockers(setup.data) : [];

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
        // `standing`: `["snapshot"]` carries the last scan's own verdict on itself, so this is
        // true before the page loads and stays true until a clean scan replaces it. Its other
        // route in is `useScanSettled` invalidating that key off the shell's 15s poll, which a
        // scheduled scan reaches with nothing pressed. `ScanBar` says the same thing about the
        // same field and moves with it (rule 72).
        <Notice tone="warn" standing>
          <strong>This scan came back incomplete.</strong> {latestSnapshot?.degraded_reason} You can
          still look at it, but Reaper won't act on it, so a plan can't be built. Fix the source and
          scan again.
        </Notice>
      )}

      <ReapBreakdown onGoToPlexSettings={onGoToPlexSettings} onGoToReview={onGoToReview} />

      {plan.error && <Notice tone="error">{plan.error.message}</Notice>}

      {/* A plan is asked for but not in hand. Never render nothing here: the whole block below
          -- phrase, count, Execute, steps -- hangs off this one query, so a failed fetch used to
          unmount all of it silently and a click on a history row simply looked like it did
          nothing. Same two branches, same words, as the reap sheet's loader (App.tsx, rule 36). */}
      {runId != null &&
        !run &&
        (runPending ? (
          <p className="help">Loading the plan…</p>
        ) : (
          <Notice tone="error">
            {runError instanceof ApiError && runError.status === 404
              ? "That plan is no longer available."
              : "Reaper couldn't load this plan. Reload the page to try again."}
          </Notice>
        ))}

      {run && (
        <>
          <div className="plan-summary">
            <span className="confirm-phrase">{run.confirmation_phrase}</span>
            <span className="muted">
              {souls(run.item_count)}, {bytes(run.total_bytes)}, smallest first, and the first is a
              test: if it doesn't go exactly as planned, the run stops.
            </span>
            {/* The plan is smaller than the queue implied, and this is where the owner
                finds out. Silence here reads as "that was everything". */}
            {run.held_back_unknown_size > 0 && (
              <Notice tone="warn">
                {souls(run.held_back_unknown_size)}{" "}
                {run.held_back_unknown_size === 1 ? "is" : "are"} held back. Reaper couldn't measure{" "}
                {run.held_back_unknown_size === 1 ? "its" : "their"} size, so it won't delete{" "}
                {run.held_back_unknown_size === 1 ? "it" : "them"}.
              </Notice>
            )}
            {/* Informational here, and acknowledged in the sheet. Execute only opens the
                sheet, so nothing irreversible is one click from this row. */}
            {planTrash.show && (
              <PlexTrashNotice known={planTrash.known} unreadable={planTrash.unreadable} />
            )}
            {staleRun && (
              // `standing`: this turns true when a newer scan lands under a plan already on
              // screen, which is a background event with no press to attach it to, and it is
              // then part of the page until a new plan is built.
              <Notice tone="warn" standing>
                This plan came from an older scan, so it can list titles you have since protected.{" "}
                <button className="link" onClick={() => plan.mutate()} disabled={plan.isPending}>
                  Build a new plan
                </button>
              </Notice>
            )}
            {/* The check is in flight, so there is nothing to warn about yet. This used to be the
                pending branch of the notice below, which made the first thing that notice said
                "Warning: Checking whether this plan came from the latest scan…" -- a severity
                claim over a spinner caption, and then a second utterance when the query settled
                and swapped its own children in place. A loading affordance is markup and speaks
                only once the wait has been one (#332, `useSlowWait`), so it is the page's plain
                help line here, the same one the plan loader above uses. */}
            {staleUnknown && snapshot.isPending && (
              <p className="help">Checking whether this plan came from the latest scan…</p>
            )}
            {staleUnknown && !snapshot.isPending && (
              // `standing`: a snapshot read that would not answer is the state of this page until
              // the read succeeds, not a reply to anything pressed.
              <Notice tone="warn" standing>
                Reaper couldn't check whether this plan came from the latest scan.{" "}
                <button className="link" onClick={() => plan.mutate()} disabled={plan.isPending}>
                  Build a new plan
                </button>
              </Notice>
            )}
            {/* Above Execute, because that is the button the refusal fires from. The Plex one
                carries the way to fix it (rule 42); the others are fixed from Settings →
                Connections, which the notice names rather than links, since this page has no
                jump to it. */}
            {blockers.map((b) => (
              <Notice key={b.key} tone="warn">
                {b.sentence}
                {b.key === "plex" && (
                  <>
                    {" "}
                    <button className="link" onClick={onGoToPlexSettings}>
                      Connect Plex in Settings
                    </button>
                  </>
                )}
              </Notice>
            ))}
            {setup.isError && !setup.data && (
              <Notice tone="warn">
                Reaper couldn't check whether Plex and Tautulli are connected. Without either one a
                real run is refused.
              </Notice>
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
          {dry.error && <Notice tone="error">{dry.error.message}</Notice>}
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
                    {date(r.approved_at)}, {runState(r.state)}
                    {open && ", open above"}
                    {/* The stored reason, under the state it explains, in the step table's
                        `.step-why` box. Its only surface: the report panel is dry-run state
                        and the reap sheet reads the in-memory status, so a reload leaves
                        "stopped" and nothing else (#342). */}
                    {r.aborted_reason && <span className="step-why">{r.aborted_reason}</span>}
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
