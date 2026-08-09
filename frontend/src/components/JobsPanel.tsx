// SPDX-License-Identifier: AGPL-3.0-or-later
//
// Settings -> Jobs: what Reaper does on a timer, when each job last ran, and when each runs
// next. Also the Leaving Soon shelf, which is a job in the same list.
//
// A schedule is edited in `ScheduleModal`, a `ModalShell` whose scrim covers the section rail,
// so this panel reports no draft upward (`dirtyPanels` in Settings.tsx says so). The ids in
// `JOB_META` are the server's own: `tests/test_repo_hygiene.py` holds them to the set the
// scheduler builds, so a renamed job fails there rather than rendering a row with no label.

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { type RefObject, useEffect, useRef, useState } from "react";
import { announce } from "../announce";
import { api, type Schedule, type ScheduledJob } from "../api";
import { useBackGuard } from "../backnav";
import { count } from "../format";
import { shelfSkipIsCurrent } from "../shelfStatus";
import { JobStatus, useJobFlash } from "./JobStatus";
import { ModalShell } from "./ModalShell";
import { ScanRow } from "./ScanBar";
import { type StaleReadPlan, StaleReadSlot, collapseStaleReads } from "./StaleReadNotice";
import { Notice } from "./Notice";

const SCAN_ID = "scheduled_scan";

/** The one upkeep job whose result a DIFFERENT query already renders: the About row, the
 *  version pill and the account chip's light all read `["update"]`. Named here so the row can
 *  refresh that query when the job finishes. The job LIST is still the server's (rule 66);
 *  this is a behavior hook on one id, like `SCAN_ID` above. */
const UPDATE_CHECK_ID = "check_for_updates";

interface JobMeta {
  title: string;
  desc: string;
  /** The schedule editor's intro; falls back to `desc`. */
  modalDesc?: string;
  /** Shown in the editor while the job is being turned off. */
  offWarning?: string;
}

// The display copy for every job, in one place, so the row and its editor never drift.
const JOB_META: Record<string, JobMeta> = {
  [SCAN_ID]: {
    title: "Update library and apply policy",
    desc: "Checks what changed since the last scan and re-scores it against your policy. A quick pass, not a full re-read.",
    modalDesc: "Reaper can scan on its own to keep the queue fresh.",
  },
  refresh_ratings: {
    title: "Refresh IMDb ratings",
    desc: "Downloads the latest IMDb ratings so scores use current numbers.",
    offWarning:
      "With this off, ratings won't refresh on a schedule. Reaper still refreshes them once at startup if they're over two weeks old.",
  },
  refresh_curated_lists: {
    // The job id is a stored schedule key and predates the registry, so it keeps its old
    // spelling; what it refreshes is every list on Settings -> Lists, whatever its source
    // (scheduler.refresh_curated_lists).
    title: "Refresh your lists",
    desc: "Re-checks every list on Settings, Lists, so a tag or a collection you edited starts protecting without waiting for a scan.",
    offWarning:
      "This only affects the standalone daily refresh. Every scan already re-checks your lists, and you can check one on Settings, Lists.",
  },
  full_history_sweep: {
    title: "Full watch-history update",
    desc: "Re-reads your whole watch history, not just new plays, so imported or backdated views still count and a wiped history is caught.",
    offWarning:
      "With this off, Reaper stops re-reading your full history. Imported or backdated plays won't be counted, and a wiped history won't be caught.",
  },
  check_for_updates: {
    title: "Check for updates",
    desc: "Asks GitHub whether a newer Reaper is available.",
    offWarning: "With this off, Reaper only checks when you open it.",
  },
};

/** The copy for a job id. Every scheduled job has an entry; the fallback only exists so the
 *  lookup is total for the type checker. */
function jobMeta(id: string): JobMeta {
  return JOB_META[id] ?? { title: id, desc: "" };
}

const SCAN_PRESETS: { label: string; cron: string | null }[] = [
  { label: "Off (scan by hand)", cron: null },
  { label: "Every night at 2 AM", cron: "0 2 * * *" },
  { label: "Every Sunday at 3 AM", cron: "0 3 * * 0" },
  { label: "First of the month, 3 AM", cron: "0 3 1 * *" },
];

/** The upkeep presets. "Every day" carries the job's own default time (staggered off peak),
 *  so choosing it keeps the natural setting exactly what it was. */
function maintenancePresets(defaultCron: string): { label: string; cron: string | null }[] {
  return [
    { label: "Off (don't run)", cron: null },
    { label: "Every day", cron: defaultCron },
    { label: "Every 12 hours", cron: "0 */12 * * *" },
    { label: "Every 6 hours", cron: "0 */6 * * *" },
    { label: "Every hour", cron: "0 * * * *" },
  ];
}

/** Picker sentinels that are not cron lines: "off" and "type your own". */
const OFF_VALUE = "__off__";
const CUSTOM_VALUE = "__custom__";

function whenText(iso: string | null): string {
  if (!iso) return "not scheduled";
  const ms = new Date(iso).getTime() - Date.now();
  if (ms <= 0) return "any moment";
  const mins = Math.round(ms / 60000);
  if (mins < 60) return `in ${mins} min`;
  const hours = Math.round(mins / 60);
  if (hours < 24) return `in ${hours} hr`;
  return new Date(iso).toLocaleString();
}

const WEEKDAYS = ["Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"];

function clockLabel(hour: number, minute: number): string {
  const period = hour < 12 ? "AM" : "PM";
  const hour12 = hour % 12 === 0 ? 12 : hour % 12;
  return `${hour12}:${String(minute).padStart(2, "0")} ${period}`;
}

function ordinal(n: number): string {
  const tens = n % 100;
  if (tens >= 11 && tens <= 13) return `${n}th`;
  const ones = n % 10;
  return `${n}${ones === 1 ? "st" : ones === 2 ? "nd" : ones === 3 ? "rd" : "th"}`;
}

/** A cron line in plain words, for the shapes the presets and defaults produce. Anything
 *  outside those reads as its raw line rather than a confident wrong guess. */
function describeCron(cron: string): string {
  const parts = cron.trim().split(/\s+/);
  if (parts.length !== 5) return `Custom (${cron})`;
  const [m = "", h = "", dom = "", mon = "", dow = ""] = parts;
  const numeric = (x: string) => /^\d+$/.test(x);
  const everyDay = dom === "*" && mon === "*" && dow === "*";

  const hourStep = /^\*\/(\d+)$/.exec(h);
  if (numeric(m) && hourStep && everyDay) return `Every ${hourStep[1]} hours`;
  if (numeric(m) && h === "*" && everyDay) return "Every hour";
  if (!numeric(m) || !numeric(h)) return `Custom (${cron})`;

  const at = clockLabel(Number(h), Number(m));
  if (everyDay) return `Every day at ${at}`;
  if (dom === "*" && mon === "*" && numeric(dow)) {
    return `Every ${WEEKDAYS[Number(dow) % 7]} at ${at}`;
  }
  if (numeric(dom) && mon === "*" && dow === "*") {
    return `Monthly on the ${ordinal(Number(dom))} at ${at}`;
  }
  return `Custom (${cron})`;
}

function scanScheduleText(job: ScheduledJob | undefined, failed: boolean): string {
  // A failed load is not "still checking": say so, so the row doesn't claim to be checking
  // forever after the schedule query errored (U-6). It only costs the schedule when there is no
  // last good row to fall back on, though: React Query keeps the previous jobs and raises the
  // failure beside them, so an undivided `failed` blanked this line while the sibling JobRows
  // went on printing next-run times off that same held row, under a panel notice saying the rows
  // are kept but stale. The panel's 1.5s self-poll reaches that state with nobody touching
  // anything (rule 72: the same split `JobsPanel`'s own `StaleReadSlot` takes).
  if (failed && !job) return "Couldn't check the schedule.";
  if (!job) return "Automatic scan: checking…";
  if (job.cron === null) return "Automatic scan is off. It runs when you ask.";
  return `Automatic scan: ${describeCron(job.cron)}, next ${whenText(job.next_run_at)}`;
}

function maintenanceScheduleText(job: ScheduledJob): string {
  if (job.cron === null) return "Off. Run it by hand.";
  return `${describeCron(job.cron)}, next ${whenText(job.next_run_at)}`;
}

/** The one schedule editor, for the scan and every upkeep job. Presets plus "off" plus a
 *  cron line of your own; turning an upkeep job off carries a plain warning of what stops. */
function ScheduleModal({
  job,
  onClose,
  savePendingRef,
}: {
  job: ScheduledJob;
  onClose: () => void;
  // Set by JobsPanel so its Back guard can read the same canClose the scrim/Escape/✕ use (B-11).
  savePendingRef?: RefObject<boolean>;
}) {
  const queryClient = useQueryClient();
  // The effective server time zone every timed job runs on, so the help names the real zone
  // instead of guessing "UTC in Docker" (U-1, rule 86). Shares GeneralPanel's cache.
  const zone = useQuery({ queryKey: ["general-settings"], queryFn: api.general }).data?.timezone;
  const meta = jobMeta(job.id);
  const presets =
    job.id === SCAN_ID ? SCAN_PRESETS : maintenancePresets(job.default_cron ?? "0 4 * * *");
  const isKnownPreset = presets.some((p) => p.cron !== null && p.cron === job.cron);

  const [choice, setChoice] = useState<string>(
    job.cron === null ? OFF_VALUE : isKnownPreset ? job.cron : CUSTOM_VALUE,
  );
  const [custom, setCustom] = useState(job.cron && !isKnownPreset ? job.cron : "");
  const [error, setError] = useState<string | null>(null);

  const save = useMutation({
    mutationFn: (cron: string | null) => api.saveJobSchedule(job.id, cron),
    onSuccess: () => {
      // The modal closing was the entire success signal, the same shape `ServiceModal`'s save
      // was fixed for -- and it takes the focused button with it.
      announce("Schedule saved.");
      void queryClient.invalidateQueries({ queryKey: ["schedule"] });
      onClose();
    },
    onError: (e: Error) => setError(e.message),
  });

  // Mirror the save's pending state up to JobsPanel's Back guard, and clear it on unmount so a
  // stale true never lingers after the modal closes (B-11).
  useEffect(() => {
    if (savePendingRef) savePendingRef.current = save.isPending;
    return () => {
      if (savePendingRef) savePendingRef.current = false;
    };
  }, [save.isPending, savePendingRef]);

  const chosenCron =
    choice === OFF_VALUE ? null : choice === CUSTOM_VALUE ? custom.trim() || null : choice;
  const turningOff = chosenCron === null;
  const saveDisabled = save.isPending || (choice === CUSTOM_VALUE && custom.trim() === "");

  return (
    <ModalShell title={meta.title} onClose={onClose} canClose={!save.isPending}>
      <div className="service-form">
        <p className="help">{meta.modalDesc ?? meta.desc}</p>

        <label className="field-sm">
          <span className="field-label">How often</span>
          <select
            value={choice}
            aria-label="How often"
            disabled={save.isPending}
            onChange={(e) => setChoice(e.target.value)}
          >
            {presets.map((p) => (
              <option key={p.label} value={p.cron ?? OFF_VALUE}>
                {p.label}
              </option>
            ))}
            <option value={CUSTOM_VALUE}>Your own schedule…</option>
          </select>
          {job.default_cron && (
            <span className="help">
              Default: {describeCron(job.default_cron)}. You can Run now anytime.
            </span>
          )}
          {/* The clock times above run on the server's configured time zone, not this browser's.
              Name the real zone so "2 AM" is not read as local time, and the operator is not left
              to guess (U-1, rule 86). Falls back to the generic phrasing only while it loads. */}
          <span className="help">
            {zone
              ? `Times use your server time zone: ${zone}. Change it in Settings, General.`
              : "Times use your server time zone. Change it in Settings, General."}
          </span>
        </label>

        {choice === CUSTOM_VALUE && (
          <label className="field-sm">
            <span className="field-label">Your own schedule</span>
            <input
              type="text"
              value={custom}
              placeholder="30 4 * * *"
              aria-label="Your own schedule"
              onChange={(e) => setCustom(e.target.value)}
            />
            <span className="help">
              A cron line, for when none of the presets fit. 30 4 * * * runs at 4:30 AM every day.
            </span>
          </label>
        )}

        {turningOff && meta.offWarning && <Notice tone="warn">{meta.offWarning}</Notice>}
        {error && <Notice tone="error">{error}</Notice>}

        <div className="add-actions">
          <span className="flex-spacer" />
          <button className="ghost" onClick={onClose} disabled={save.isPending}>
            Cancel
          </button>
          <button
            className="primary"
            onClick={() => save.mutate(chosenCron)}
            disabled={saveDisabled}
          >
            {save.isPending ? "Saving…" : "Save"}
          </button>
        </div>
      </div>
    </ModalShell>
  );
}

/** One upkeep job: what it is, when it runs, and Edit + Run now. It shows an honest
 *  "running now" while it works; none of these can delete anything. */
function JobRow({ job, onEdit }: { job: ScheduledJob; onEdit: () => void }) {
  const queryClient = useQueryClient();
  const meta = jobMeta(job.id);
  const run = useMutation({
    mutationFn: () => api.runJob(job.id),
    onSuccess: () => {
      // Optimistically mark the job running so the spinner shows at once and the finish is
      // seen as a running->done transition (the flash) even for a job that completes inside
      // one poll interval. The real state, and the fresh last-run fields, land on the next
      // poll: the schedule query's own refetchInterval reacts to this optimistic flag right
      // away (nothing here needs to force an earlier refetch), so there is no fixed delay to
      // race against a scheduler that is slow to submit the job.
      queryClient.setQueryData<Schedule>(["schedule"], (prev) =>
        prev
          ? { ...prev, jobs: prev.jobs.map((j) => (j.id === job.id ? { ...j, running: true } : j)) }
          : prev,
      );
    },
  });
  const running = job.running || run.isPending;
  // The flash keys on the server's own running flag (which the mutation seeds optimistically),
  // never on `run.isPending` -- that would fire a stale flash the instant the POST returns,
  // before the job has even run. Compared with `!== null`, not truthiness: an empty (but
  // present) result must still flash, unlike a job that has simply never run.
  const flash = useJobFlash(
    job.running,
    job.last_result !== null ? { ok: job.last_ok !== false, text: job.last_result } : null,
  );

  // A finished update check has replaced the answer `["update"]` holds, and that query is
  // half an hour stale-free (updateStatus.ts) with nothing else to invalidate it -- so the
  // row would flash "Reaper 2026.9.2 is out" while the version pill, the About row and the
  // chip light all went on asserting the answer it just replaced (rule 79). Keyed on the same
  // running -> done edge the flash watches, so it fires when the ANSWER changed rather than
  // when the button was pressed.
  const wasRunning = useRef(false);
  useEffect(() => {
    if (wasRunning.current && !job.running && job.id === UPDATE_CHECK_ID) {
      void queryClient.invalidateQueries({ queryKey: ["update"] });
    }
    wasRunning.current = job.running;
  }, [job.running, job.id, queryClient]);

  return (
    <div className="jobrow">
      <div className="jobrow-main">
        <div className="jobrow-title">{meta.title}</div>
        <div className="jobrow-desc">{meta.desc}</div>
        <JobStatus
          running={running}
          runningLabel="Running now…"
          lastRunAt={job.last_run_at}
          lastOk={job.last_ok}
          lastResult={job.last_result}
          flash={flash}
        />
        <div className="jobrow-sched">{maintenanceScheduleText(job)}</div>
        {run.error && (
          <Notice tone="error" inline>
            The job didn't start: {run.error.message}
          </Notice>
        )}
      </div>
      {/* The Jobs page stacks this row once per server-returned job, above the scan row and the
          Leaving Soon row, so "Edit" and "Run now" appear several times over with the job's name
          sitting in `.jobrow-title` where no control referenced it. */}
      <div className="jobrow-actions">
        <span className="slot-edit">
          <button className="ghost" aria-label={`Edit ${meta.title}`} onClick={onEdit}>
            Edit
          </button>
        </span>
        <span className="slot-act">
          <button
            className="primary"
            // Same shape as the connection test above: the state is in the name, and the
            // visible words lead so voice control still reaches it. "Run now" is not a
            // contiguous part of "Run Trash sweep now", which is what the fixed name was.
            aria-label={running ? `Running…, ${meta.title}` : `Run now, ${meta.title}`}
            onClick={() => run.mutate()}
            disabled={running}
          >
            {running ? "Running…" : "Run now"}
          </button>
        </span>
      </div>
    </div>
  );
}

/** The Leaving Soon shelf update, moved here from Plex settings. Its on/off toggle still
 *  lives on the Plex tab, so this row links there; when off, it grays out and can't run. */
function LeavingSoonRow({
  onGoToPlex,
  plan,
}: {
  onGoToPlex: () => void;
  /** The Jobs panel's stale-read decision. This row draws its own line only while it is the
   *  only read that failed; when the panel's read failed too, the panel says it once, above
   *  these rows (#198). */
  plan: StaleReadPlan;
}) {
  const queryClient = useQueryClient();
  const ls = useQuery({ queryKey: ["leaving-soon-settings"], queryFn: api.leavingSoonSettings });
  const runSync = useMutation({
    mutationFn: api.syncLeavingSoon,
    onSuccess: () => void queryClient.invalidateQueries({ queryKey: ["leaving-soon-settings"] }),
  });
  // The manual-run confirmation for this row: the sync is a synchronous mutation, so its
  // result is read straight off the mutation when it settles (unlike the polled upkeep jobs).
  // Called before the early returns below, so the hook order never changes.
  //
  // Both halves are the server's, which words the pass once and stores that same sentence on
  // the row below (rule 104). This used to re-derive them from `problems`, `applied` and the
  // counts, and the flash then contradicted the row it sat on: with no libraries turned on it
  // said the shelves had failed while the row rested green (#555).
  const syncResult = runSync.data
    ? { ok: runSync.data.ok, text: runSync.data.result }
    : runSync.error
      ? { ok: false, text: "It didn't update" }
      : null;
  const flash = useJobFlash(runSync.isPending, syncResult);

  // One declaration behind both the row heading and the button's spoken name, so they cannot
  // drift apart (rule 144). The name has to carry the visible words "Update now" first, and
  // `title` already opens with the verb, so pasting the two together says "Update" twice.
  const shelf = "Leaving Soon shelf";
  const title = `Update ${shelf}`;
  const desc =
    'Pushes the current countdown set to the Plex "Leaving Soon" shelf, so people get a heads-up before anything goes.';

  if (ls.isPending) {
    return (
      <div className="jobrow">
        <div className="jobrow-main">
          <div className="jobrow-title">{title}</div>
          <div className="jobrow-desc">{desc}</div>
          <div className="jobrow-sched">Loading…</div>
        </div>
      </div>
    );
  }
  // Two states, not one (rule 17/36, and rule 72: the same split JobsPanel takes below). React
  // Query keeps the last good row through a failed refetch and raises `isError` beside it, so the
  // undivided test here threw that row away -- and the trigger was this row's OWN success path,
  // since a finished "Update now" invalidates this query. One blinked refetch after a shelf
  // update that WORKED reported the shelf status as unknown and took the "N added, M cleared"
  // confirmation down with it, before it had ever painted. The never-loaded sentence stays for
  // the read that really never landed, the only case it is true in.
  if (!ls.data) {
    return (
      <div className="jobrow">
        <div className="jobrow-main">
          <div className="jobrow-title">{title}</div>
          <div className="jobrow-desc">{desc}</div>
          <Notice tone="error" inline>
            Couldn't load the shelf status. Reload to try again.
          </Notice>
        </div>
      </div>
    );
  }

  const { enabled, last, last_skip: skip } = ls.data;
  // The row is still the best answer there is, so it renders -- and says it could not be
  // confirmed, above everything in the row the failed read could have changed.
  const stale = <StaleReadSlot plan={plan} slot="the shelf status" inline />;
  // A scan that skipped the shelf writes no pass, so this row re-read the last COMPLETED
  // pass and answered for the scan with its green dot, its timestamp and its counts -- under
  // a line reading "Runs after every scan". `after_scan` records the skip separately; prefer
  // it only while it is actually newer, so a pass that later completes wins on its own
  // timestamp with nothing to clear. Every clause of this is ScanRow's treatment of a
  // scheduled scan that crashed and wrote no snapshot, at its sibling (rule 72). The
  // comparison itself is shared with the Plex panel's status line (`shelfStatus.ts`), which
  // is the other surface that has to make it.
  const currentSkip = shelfSkipIsCurrent(ls.data) ? skip : null;

  if (!enabled) {
    return (
      <div className="jobrow dimmed">
        <div className="jobrow-main">
          <div className="jobrow-title">{title}</div>
          <div className="jobrow-desc">{desc}</div>
          {stale}
          <div className="jobrow-sched">
            Off.{" "}
            <button className="link" onClick={onGoToPlex}>
              Turn it on in Plex → Leaving Soon
            </button>
          </div>
        </div>
        <div className="jobrow-actions">
          <span className="slot-edit" />
          <span className="slot-act">
            <button className="primary" disabled>
              Update now
            </button>
          </span>
        </div>
      </div>
    );
  }

  const running = runSync.isPending;
  return (
    <div className="jobrow">
      <div className="jobrow-main">
        <div className="jobrow-title">{title}</div>
        <div className="jobrow-desc">{desc}</div>
        {stale}
        <JobStatus
          running={running}
          runningLabel="Updating…"
          lastRunAt={currentSkip ? currentSkip.at : (last?.at ?? null)}
          lastOk={currentSkip ? false : last ? last.ok : null}
          lastResult={currentSkip ? currentSkip.result : (last?.result ?? null)}
          flash={flash}
        />
        {last && (
          // The counts survive a skip, because a skipped pass wrote nothing: the shelf still
          // holds what the last completed pass put there, and these are the only true numbers
          // anyone has. Past tense is the whole correction -- they stop reading as the outcome
          // of the most recent scan, which is the one thing about them that stopped being true.
          <div className="jobrow-meta">
            <strong>{count(last.movies)}</strong> movie{last.movies === 1 ? "" : "s"} and{" "}
            <strong>{count(last.seasons)}</strong> season{last.seasons === 1 ? "" : "s"}{" "}
            {currentSkip ? "were on the shelves at the last update" : "on the shelves"}
          </div>
        )}
        <div className="jobrow-sched">Runs after every scan</div>
        <div className="jobrow-link">
          <button className="link" onClick={onGoToPlex}>
            Manage in Plex → Leaving Soon
          </button>
        </div>
        {runSync.error && (
          <Notice tone="error" inline>
            The shelves didn't update: {runSync.error.message}
          </Notice>
        )}
      </div>
      <div className="jobrow-actions">
        <span className="slot-edit" />
        <span className="slot-act">
          <button
            className="primary"
            aria-label={running ? `Updating…, ${shelf}` : `Update now, ${shelf}`}
            onClick={() => runSync.mutate()}
            disabled={running}
          >
            {running ? "Updating…" : "Update now"}
          </button>
        </span>
      </div>
    </div>
  );
}

export function JobsPanel({ onGoToPlex }: { onGoToPlex: () => void }) {
  const { data: snapshot } = useQuery({
    queryKey: ["snapshot"],
    queryFn: api.latestSnapshot,
    retry: false,
  });
  const schedule = useQuery({
    queryKey: ["schedule"],
    queryFn: api.schedule,
    // Poll only while something is actually running, so the "running now" states and the
    // next-run lines stay live without hammering the endpoint the rest of the time.
    refetchInterval: (query) => (query.state.data?.jobs.some((j) => j.running) ? 1500 : false),
  });
  const [editing, setEditing] = useState<ScheduledJob | null>(null);
  // The modal's save lives inside ScheduleModal; it mirrors its pending state here so the Back
  // guard can refuse a close mid-save exactly as the scrim/Escape/✕ do (canClose={!save.isPending}
  // below). Without this, Back would tear the modal down while the save is in flight, dropping
  // the error it would have shown (B-11, rule 80).
  const savePendingRef = useRef(false);
  // Back closes the schedule editor instead of leaving Reaper -- unless a save is in flight.
  useBackGuard(
    editing !== null,
    () => setEditing(null),
    () => !savePendingRef.current,
  );

  // The shelf row owns this read and renders inside this panel, so the panel has to know whether
  // it failed to decide whether both lines collapse into one (#198). A second `useQuery` on the
  // same key rather than a signal threaded up out of the row: React Query hands both observers
  // the one cache entry, so there is no second request and no state to keep in step -- and the
  // row's own early returns cannot leave a lifted flag asserting something its surface no longer
  // shows, which is the trap rule 146 is about.
  const shelf = useQuery({
    queryKey: ["leaving-soon-settings"],
    queryFn: api.leavingSoonSettings,
  });
  const stale = collapseStaleReads("these jobs", [
    { what: "these jobs", stale: schedule.isError && !!schedule.data },
    { what: "the shelf status", stale: shelf.isError && !!shelf.data },
  ]);

  const jobsById = new Map<string, ScheduledJob>((schedule.data?.jobs ?? []).map((j) => [j.id, j]));
  const scanJob = jobsById.get(SCAN_ID);

  return (
    <div className="panel">
      <h2>Jobs</h2>
      <p className="blurb">
        Everything Reaper runs on a timer lives here, and you can run any of it now without waiting.
        None of these can delete a thing. A scan just refreshes the review queue; the rest is
        upkeep.
      </p>

      {schedule.isPending && <p className="muted">Loading the upkeep jobs…</p>}
      {/* The rows below render from the last good row either way (`schedule.data?.jobs ?? []`),
          so a failed refetch already keeps them on screen -- only the sentence about them was
          wrong, and it read worst here: every row carries a next-run time and a running flag,
          and this query polls itself every 1.5s while anything runs, so it reaches the failed
          state with the operator doing nothing at all. The never-loaded line stays for the read
          that really never landed, which is the only case it is true in.

          ABOVE the rows, because the line says what's BELOW may be out of date and `.panel` is
          plain block flow, so DOM order is reading order: sat after `.set-rows` it pointed at the
          schedule editor and nothing else. Every other call site puts it over its content. */}
      {schedule.isError && !schedule.data && (
        <Notice tone="error">Couldn't load the upkeep jobs. Reload to try again.</Notice>
      )}
      {/* Both reads on this panel say the same thing when they fail together, so they say it
          once, here, above the rows (#198). Unlike Plex's four these are independent polls
          that can fail apart, which is why the rule counts the lines that would draw rather
          than grouping by invalidation: either one alone still speaks in its own words. */}
      <StaleReadSlot plan={stale} slot="these jobs" />

      <div className="set-rows">
        <ScanRow
          snapshot={snapshot}
          scanJob={scanJob}
          title={jobMeta(SCAN_ID).title}
          desc={jobMeta(SCAN_ID).desc}
          scheduleText={scanScheduleText(scanJob, schedule.isError)}
          onEdit={() => scanJob && setEditing(scanJob)}
          canEdit={!!scanJob}
        />
        <LeavingSoonRow onGoToPlex={onGoToPlex} plan={stale} />
        {/* Render the upkeep jobs from the server's own list (scan aside; it has its own
            row), in its order, so a job added server-side appears here without a frontend
            edit. jobMeta falls back to the raw id for a job with no copy yet. */}
        {(schedule.data?.jobs ?? [])
          .filter((job) => job.id !== SCAN_ID)
          .map((job) => (
            <JobRow key={job.id} job={job} onEdit={() => setEditing(job)} />
          ))}
      </div>

      {editing && (
        <ScheduleModal
          job={editing}
          onClose={() => setEditing(null)}
          savePendingRef={savePendingRef}
        />
      )}
    </div>
  );
}
