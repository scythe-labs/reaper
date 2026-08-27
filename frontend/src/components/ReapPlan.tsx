// SPDX-License-Identifier: AGPL-3.0-or-later
//
// The reap plan: build it, read every step, dry-run it, and, through the confirmation sheet,
// execute it.
//
// This is where the owner sees exactly what Reaper *would* do, down to the literal HTTP
// request each deletion would issue. Building a plan and dry-running it delete nothing. The
// dry run walks every interlock and sends nothing. Executing goes through ReapConfirm, which
// requires deletion armed on the host and the exact typed confirmation phrase before it
// deletes. While deletion is off, the Execute button is disabled outright, with the shortest
// path to the switch beside it: the server would refuse anyway, so the UI just stops inviting
// a click that must fail. A plan built here covers the whole condemned set (capped); to reap
// a hand-picked few, select them in the review queue and use "Reap now".
//
// The same courtesy extends to the other thing the server refuses on: a run without Plex or
// Tautulli. The server's own refusal states that requirement too, but only after the
// confirmation phrase has already been typed, so this page states it earlier, beside Execute.

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { Trans, useTranslation } from "react-i18next";
import { ApiError, api, type Run, type RunReport } from "../api";
import { DegradedDocLink } from "../docs/DocLink";
import { describeError } from "../errors";
import { bytes, count, date, souls } from "../format";
import i18next from "../i18n";
import { reapBlockers } from "../reapReadiness";
import { usePlexTrash, trashWarning } from "../usePlexTrash";
import { useSafety } from "../useSafety";
import { composeError } from "../why";
import { PlexTrashNotice } from "./PlexTrashNotice";
import { ReapBreakdown } from "./ReapBreakdown";
import { ReapConfirm } from "./ReapConfirm";
import { Notice } from "./Notice";

/** A stored run state, in the words the rest of the app uses for it. "Stopped", never
 *  "aborted": one word for one mechanism, the same as the reap bar and the report above. An
 *  unknown state (an older or newer build) reads through unchanged rather than being hidden.
 *  A plain function, not a component, so it reads the catalog through the shared `i18next`
 *  instance rather than the `useTranslation` hook. */
function runState(state: string): string {
  if (state === "planned") return i18next.t("reapPlan.runState.notRun");
  if (state === "executing") return i18next.t("reapPlan.runState.running");
  if (state === "completed") return i18next.t("reapPlan.runState.done");
  if (state === "aborted") return i18next.t("reapPlan.runState.stopped");
  return state;
}

/** A stored step kind, said as the outcome it performs. The literal request the page's blurb
 *  promises is whole in the Request column beside this cell, so the kind is free to be plain
 *  words. An unknown kind (an older or newer build) reads through unchanged, the same
 *  contract as `runState` above. `test_api_type_mirror.py` holds these four to the kinds the
 *  planner emits, both directions. */
function stepKind(kind: string): string {
  if (kind === "radarr_delete") return i18next.t("reapPlan.steps.kind.radarrDelete");
  if (kind === "sonarr_unmonitor") return i18next.t("reapPlan.steps.kind.sonarrUnmonitor");
  if (kind === "sonarr_verify_unmonitor")
    return i18next.t("reapPlan.steps.kind.sonarrVerifyUnmonitor");
  if (kind === "sonarr_delete_files") return i18next.t("reapPlan.steps.kind.sonarrDeleteFiles");
  return kind;
}

/** A stored step state in plain words, same contract; the stored reason for a failed or
 *  skipped step renders beside it, so one word is enough here. `test_api_type_mirror.py`
 *  holds these five to ``StepState``, both directions. */
function stepState(state: string): string {
  if (state === "pending") return i18next.t("reapPlan.steps.state.pending");
  if (state === "sent") return i18next.t("reapPlan.steps.state.sent");
  if (state === "verified") return i18next.t("reapPlan.steps.state.verified");
  if (state === "failed") return i18next.t("reapPlan.steps.state.failed");
  if (state === "skipped") return i18next.t("reapPlan.steps.state.skipped");
  return state;
}

/** How many rows either long list on this page draws before saying how many more there are.
 *  One number for both, so the step table and the practice-run outcomes cut off together. */
const LIST_CAP = 50;

function Steps({ run }: { run: Run }) {
  // A first cleanup of 500 items is 1500 rows, each with a path and a stringified body:
  // rendering all of them synchronously, on plan build and again on every history-row click,
  // does not scale. The first 50 are the ones that matter: the plan is ordered smallest
  // first, so step 0 is the canary the whole run turns on.
  //
  // The server sends that window rather than the whole journal, so the slice below is a
  // second bound on a list already bounded, kept because `LIST_CAP` is what THIS table draws
  // and the two are not the same decision. `more` reads `step_count`, never `steps.length`:
  // the response no longer carries the rows it is counting, so subtracting the page from
  // itself would silently zero the line below and leave the operator reading 50 rows with
  // nothing saying the plan is 500.
  const { t } = useTranslation();
  const shown = run.steps.slice(0, LIST_CAP);
  const more = run.step_count - shown.length;
  return (
    // The Request column holds a full API path and a JSON body, so the table has a wider
    // minimum than a phone. The wrapper keeps that scroll sideways inside the table instead
    // of pushing the whole page sideways.
    //
    // And nothing inside the table is focusable, so that sideways scroll could not be reached
    // from a keyboard at all (WCAG 2.1.1): no cell to tab onto and carry it with. `tabIndex={0}`
    // makes the wrapper its own stop, named so it is worth stopping on. `.docs-content`,
    // `.log-console`, `.dryrun-outcomes` and `docs/DocBody.tsx`'s two wrappers use the same
    // pattern.
    <div
      className="table-scroll"
      tabIndex={0}
      role="region"
      aria-label={t("reapPlan.steps.regionLabel")}
    >
      <table className="plan-steps">
        <thead>
          <tr>
            {/* `scope` explicitly, on the journalled record of what a run will send. A
                single-row `<thead>` is one browsers infer reliably, so the real-world harm is
                low, but inferring is not the same as being told, and this is the table an
                operator reads to decide. `docs/DocBody.tsx` uses the same pattern. */}
            <th scope="col">{t("reapPlan.steps.headers.ordinal")}</th>
            <th scope="col">{t("reapPlan.steps.headers.action")}</th>
            <th scope="col">{t("reapPlan.steps.headers.request")}</th>
            <th scope="col">{t("reapPlan.steps.headers.state")}</th>
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
                {step.is_canary && (
                  <span className="canary-tag">{t("reapPlan.steps.testItem")}</span>
                )}
              </td>
              <td>{stepKind(step.kind)}</td>
              <td>
                <code>
                  {step.method} {step.path}
                </code>
                {step.body && <code className="step-body">{JSON.stringify(step.body)}</code>}
              </td>
              {/* The stored reason sits under the state it explains, not in a column of its
                  own: this table already has a wider minimum than a phone (see .table-scroll
                  above) and a fifth column would push it further, for a cell that is empty on
                  every step that went fine. `error` is already operator copy: the executor
                  writes one sentence and shows the same one live, so the only thing new here
                  is that it survives a restart, which is exactly when the live copy is gone. */}
              <td className="muted">
                {stepState(step.state)}
                {step.error_reason && (
                  <span className="step-why">{composeError(step.error_reason)}</span>
                )}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      {more > 0 && (
        <p className="muted">{t("reapPlan.steps.more", { n: more, count: count(more) })}</p>
      )}
    </div>
  );
}

function Report({ report }: { report: RunReport }) {
  const { t } = useTranslation();
  // "stopped", not "aborted": one word for one mechanism. The docs say caps stop the whole
  // run, and the app-wide reap bar already reports this exact state as "Stopped."
  // (`ReapBar.tsx`). "Abort" is operator vocabulary nowhere else in the product.
  if (report.state === "aborted") {
    return (
      <div className="sim sim-info">
        <h3>{t("reapPlan.report.stoppedTitle")}</h3>
        <p>{report.aborted_reason && composeError(report.aborted_reason)}</p>
      </div>
    );
  }
  return (
    <div className="sim">
      {/* What the practice run proved, which is the only thing an operator can act on. It
          never leads with "N souls were actually reaped": that number is zero by
          construction here, since nothing was deleted, and would say nothing. It also never
          calls the per-item outcomes "steps": a plan of 3 seasons has 9 journalled steps in
          the table below, so "steps" would count the wrong thing. The two branches below are
          separate whole messages, never a shared stem, so word order stays the translator's
          to choose. */}
      <p className="blurb">
        {report.skipped > 0 ? (
          <Trans
            i18nKey="reapPlan.report.walkedSkipped"
            values={{
              soulsCount: souls(report.outcomes.length),
              skippedCount: count(report.skipped),
            }}
            components={{ walkedNum: <strong />, skipNum: <strong /> }}
          />
        ) : (
          <Trans
            i18nKey="reapPlan.report.walkedAll"
            values={{ soulsCount: souls(report.outcomes.length) }}
            components={{ walkedNum: <strong /> }}
          />
        )}
      </p>
      {/* Every row is text, so this list scrolls with nothing to tab onto (WCAG 2.1.1).
          `tabIndex={0}` on the list itself keeps its `listitem`s intact where a wrapper with
          `role="region"` would not, and it is named for what it holds. The plan table above
          uses the same pattern. */}
      <ul className="dryrun-outcomes" tabIndex={0} aria-label={t("reapPlan.report.outcomesLabel")}>
        {report.outcomes.slice(0, LIST_CAP).map((o) => (
          // One outcome per item, never more: executor._run_deletes records exactly one
          // StepOutcome per delete, so the item's own key is unique among siblings.
          <li key={o.media_key}>
            {/* Decoration: every row in this list is a pass, so the tick adds nothing a
                  reader needs and lands mid-sentence as a stray character. Where a list can
                  hold both outcomes, the reap report's per-item checks, the glyph is hidden
                  and a word carries it instead. */}
            <span className="gate-mark" aria-hidden="true">
              ✓
            </span>
            <code>{composeError(o.detail_reason)}</code>
            {o.is_canary && <span className="canary-tag">{t("reapPlan.steps.testItem")}</span>}
          </li>
        ))}
      </ul>
      {report.outcomes.length > LIST_CAP && (
        <p className="muted">
          {t("reapPlan.report.moreOutcomes", { count: count(report.outcomes.length - LIST_CAP) })}
        </p>
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
  const { t } = useTranslation();
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
  // refuses with a 409 (`api/runs._preflight_refusal`), which otherwise is the only place
  // that requirement appears, and it lands after the operator has picked what to delete,
  // armed the host and typed the whole confirmation phrase. The refusal is correct and
  // stays; this is the same fact, four steps earlier.
  const setup = useQuery({ queryKey: ["setup"], queryFn: api.setupStatus });
  // Shared with the wizard's finish panel, so the two screens cannot come to describe the
  // same refusal differently. Empty on a read we could not make: the branch below says so out
  // loud rather than letting silence read as "nothing is missing".
  const blockers = setup.data ? reapBlockers(setup.data) : [];

  return (
    <section className="reap">
      <div className="reap-head">
        <h2>{t("reapPlan.page.title")}</h2>
        <button
          className="primary"
          onClick={() => plan.mutate()}
          disabled={plan.isPending || degraded}
        >
          {plan.isPending ? t("common.planning") : t("reapPlan.page.buildButton")}
        </button>
      </div>
      <p className="blurb">
        <Trans i18nKey="reapPlan.page.blurb" components={{ em: <em /> }} />
      </p>

      {degraded && (
        // `standing`: `["snapshot"]` carries the last scan's own verdict on itself, so this is
        // true before the page loads and stays true until a clean scan replaces it. Its other
        // route in is `useScanSettled` invalidating that key off the shell's 15s poll, which a
        // scheduled scan reaches with nothing pressed. `ScanBar` says the same thing about the
        // same field and moves with it.
        <Notice tone="warn" standing as="div" className="notice-doc">
          <span>
            {/* The stored reason is server-composed operator copy, already English, so it
                rides through as a value rather than being reworded here. `?? ""` keeps a null
                reason from interpolating as the literal text "null" instead of rendering
                nothing. */}
            <Trans
              i18nKey="reapPlan.degraded.notice"
              values={{ reason: latestSnapshot?.degraded_reason ?? "" }}
              components={{ strong: <strong /> }}
            />
          </span>
          {/* Nothing renders for a degradation with no page, which is most of them. `ScanBar`
              carries the same pair. */}
          <DegradedDocLink doc={latestSnapshot?.degraded_doc ?? null} />
        </Notice>
      )}

      <ReapBreakdown onGoToPlexSettings={onGoToPlexSettings} onGoToReview={onGoToReview} />

      {plan.error && <Notice tone="error">{describeError(plan.error)}</Notice>}

      {/* A plan is asked for but not in hand. Never render nothing here: the whole block
          below (phrase, count, Execute, steps) hangs off this one query, so a failed fetch
          with nothing rendered would unmount all of it silently, and a click on a history row
          would look like it did nothing. Same two branches, same words, as the reap sheet's
          loader (App.tsx). */}
      {runId != null &&
        !run &&
        (runPending ? (
          <p className="help">{t("reapPlan.plan.loading")}</p>
        ) : (
          <Notice tone="error">
            {runError instanceof ApiError && runError.status === 404
              ? t("reapPlan.plan.notFound")
              : t("reapPlan.plan.loadFailed")}
          </Notice>
        ))}

      {run && (
        <>
          <div className="plan-summary">
            <span className="confirm-phrase">{run.confirmation_phrase}</span>
            <span className="muted">
              {t("reapPlan.summary.planLine", {
                souls: souls(run.item_count),
                bytes: bytes(run.total_bytes),
              })}
            </span>
            {/* The plan is smaller than the queue implied, and this is where the owner
                finds out. Silence here reads as "that was everything". */}
            {run.held_back_unknown_size > 0 && (
              <Notice tone="warn">
                {t("reapPlan.summary.heldBackUnknownSize", {
                  n: run.held_back_unknown_size,
                  soulsCount: souls(run.held_back_unknown_size),
                })}
              </Notice>
            )}
            {/* Informational here, and acknowledged in the sheet. Execute only opens the
                sheet, so nothing irreversible is one click from this row. */}
            {planTrash.show && (
              <PlexTrashNotice
                known={planTrash.known}
                unreadable={planTrash.unreadable}
                autoEmpties={planTrash.autoEmpties}
              />
            )}
            {staleRun && (
              // `standing`: this turns true when a newer scan lands under a plan already on
              // screen, which is a background event with no press to attach it to, and it is
              // then part of the page until a new plan is built.
              <Notice tone="warn" standing>
                <Trans
                  i18nKey="reapPlan.summary.staleRun"
                  components={{
                    btn: (
                      <button
                        className="link"
                        onClick={() => plan.mutate()}
                        disabled={plan.isPending}
                      />
                    ),
                  }}
                />
              </Notice>
            )}
            {/* The check is in flight, so there is nothing to warn about yet. A warn-tone
                notice here would put "Warning: Checking whether this plan came from the
                latest scan…" on screen, a severity claim over a spinner caption, followed by a
                second utterance once the query settles and the notice's children swap in
                place. A loading affordance is markup and speaks only once the wait has been
                one (`useSlowWait`), so this stays the page's plain help line, the same one the
                plan loader above uses. */}
            {staleUnknown && snapshot.isPending && (
              <p className="help">{t("reapPlan.summary.staleCheckPending")}</p>
            )}
            {staleUnknown && !snapshot.isPending && (
              // `standing`: a snapshot read that would not answer is the state of this page until
              // the read succeeds, not a reply to anything pressed.
              <Notice tone="warn" standing>
                <Trans
                  i18nKey="reapPlan.summary.staleUnknown"
                  components={{
                    btn: (
                      <button
                        className="link"
                        onClick={() => plan.mutate()}
                        disabled={plan.isPending}
                      />
                    ),
                  }}
                />
              </Notice>
            )}
            {/* Above Execute, because that is the button the refusal fires from. The Plex one
                carries the way to fix it. The others are fixed from Settings → Connections,
                which the notice names rather than links, since this page has no jump to it. */}
            {blockers.map((b) => (
              <Notice key={b.key} tone="warn">
                {b.sentence}
                {b.key === "plex" && (
                  <>
                    {" "}
                    <button className="link" onClick={onGoToPlexSettings}>
                      {t("reapPlan.summary.connectPlex")}
                    </button>
                  </>
                )}
              </Notice>
            ))}
            {setup.isError && !setup.data && (
              <Notice tone="warn">{t("reapPlan.summary.setupUnknown")}</Notice>
            )}
            <button onClick={() => dry.mutate(run.id)} disabled={dry.isPending}>
              {dry.isPending ? t("common.checking") : t("reapPlan.summary.practiceRun")}
            </button>
            {run.state === "planned" && (
              <button
                className="danger"
                disabled={!armed}
                title={armed ? undefined : t("reapPlan.summary.executeDisabledTitle")}
                onClick={() => setConfirming(true)}
              >
                {t("reapPlan.summary.execute")}
              </button>
            )}
            {run.state === "planned" && !armed && (
              <span className="exec-note">
                {safety.isPending ? (
                  t("common.checkingDeletion")
                ) : (
                  <>
                    {safety.isError || !safety.data
                      ? t("reapPlan.summary.safetyUnknown")
                      : t("reapPlan.summary.deletionOff")}{" "}
                    <button className="link" onClick={onGoToDeletion}>
                      {t("reapPlan.summary.turnOnLink")}
                    </button>
                  </>
                )}
              </span>
            )}
          </div>
          {dry.error && <Notice tone="error">{describeError(dry.error)}</Notice>}
          {report && <Report report={report} />}
          <Steps run={run} />
        </>
      )}

      {/* No onDone: what a finished reap invalidates (this run, the history, the queue, the
          ledger) is refreshed by the app-wide reap bar, which cannot be closed mid-run. */}
      {confirming && run && <ReapConfirm run={run} onClose={() => setConfirming(false)} />}

      {history && history.length > 0 && (
        <div className="run-history">
          <h3>{t("reapPlan.history.heading")}</h3>
          <ul>
            {history.map((r) => {
              // Clicking a row swaps the plan shown above, so the row that is open says
              // so rather than leaving the swap silent. Whether that plan came from an
              // older scan is said once, up beside Execute, where the fix lives.
              const open = runId === r.id;
              return (
                <li key={r.id} className={open ? "open" : undefined}>
                  {/* Only the id: the row carries no plan of its own, so opening one asks the
                      server for it (the ["run", id] query above). A list that came with every
                      plan in full would cost a whole snapshot's candidates per row. */}
                  <button
                    className="link"
                    onClick={() => setRunId(r.id)}
                    aria-current={open ? "true" : undefined}
                  >
                    #{r.id}
                  </button>{" "}
                  <span className="muted">
                    {date(r.approved_at)}, {runState(r.state)}
                    {open && t("reapPlan.history.openAbove")}
                    {/* The stored reason, under the state it explains, in the step table's
                        `.step-why` box. Its only surface: the report panel is dry-run state
                        and the reap sheet reads the in-memory status, so a reload leaves
                        "stopped" and nothing else. */}
                    {r.aborted_reason && (
                      <span className="step-why">{composeError(r.aborted_reason)}</span>
                    )}
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
