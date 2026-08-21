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
import { Trans, useTranslation } from "react-i18next";
import { announce } from "../announce";
import { api, type Schedule, type ScheduledJob } from "../api";
import { useBackCloseMirror, useBackGuard } from "../backnav";
import { count, weekday } from "../format";
import i18next from "../i18n";
import { shelfSkipIsCurrent } from "../shelfStatus";
import { useGeneralSettings } from "../useGeneralSettings";
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

/** The display copy for every job, read from the catalog so the row and its editor never
 *  drift. A plain function rather than a frozen table (`jobs.meta.*` below): each id gets its
 *  own literal `t()` calls, since a computed key is unreadable to the missing-key gate. */
function jobMeta(id: string): JobMeta {
  switch (id) {
    case SCAN_ID:
      return {
        title: i18next.t("jobs.meta.scan.title"),
        desc: i18next.t("jobs.meta.scan.desc"),
        modalDesc: i18next.t("jobs.meta.scan.modalDesc"),
      };
    case "refresh_ratings":
      return {
        title: i18next.t("jobs.meta.refreshRatings.title"),
        desc: i18next.t("jobs.meta.refreshRatings.desc"),
        offWarning: i18next.t("jobs.meta.refreshRatings.offWarning"),
      };
    case "refresh_curated_lists":
      // The job id is a stored schedule key and predates the registry, so it keeps its old
      // spelling; what it refreshes is every list on Settings -> Lists, whatever its source
      // (scheduler.refresh_curated_lists).
      return {
        title: i18next.t("jobs.meta.refreshCuratedLists.title"),
        desc: i18next.t("jobs.meta.refreshCuratedLists.desc"),
        offWarning: i18next.t("jobs.meta.refreshCuratedLists.offWarning"),
      };
    case "full_history_sweep":
      return {
        title: i18next.t("jobs.meta.fullHistorySweep.title"),
        desc: i18next.t("jobs.meta.fullHistorySweep.desc"),
        offWarning: i18next.t("jobs.meta.fullHistorySweep.offWarning"),
      };
    case UPDATE_CHECK_ID:
      return {
        title: i18next.t("jobs.meta.checkForUpdates.title"),
        desc: i18next.t("jobs.meta.checkForUpdates.desc"),
        offWarning: i18next.t("jobs.meta.checkForUpdates.offWarning"),
      };
    default:
      // Every scheduled job has a case above; this only exists so the lookup is total for the
      // type checker, for a job the frontend has not shipped copy for yet.
      return { title: id, desc: "" };
  }
}

/** The scan's own presets, read at call time (not a frozen module constant) so a language
 *  change is picked up the next time the editor opens. */
function scanPresets(): { label: string; cron: string | null }[] {
  return [
    { label: i18next.t("jobs.presets.scanOff"), cron: null },
    { label: i18next.t("jobs.presets.scanNightly"), cron: "0 2 * * *" },
    { label: i18next.t("jobs.presets.scanWeekly"), cron: "0 3 * * 0" },
    { label: i18next.t("jobs.presets.scanMonthly"), cron: "0 3 1 * *" },
  ];
}

/** The upkeep presets. The first is the job's own default, staggered off peak, so choosing it
 *  keeps the natural setting exactly what it was.
 *
 *  Its label is DESCRIBED from that cron rather than written here. It read "Every day" for
 *  every job, which was true of all four until the history sweep moved to every three days,
 *  and a hardcoded label beside a value it does not own is wrong the moment the value moves
 *  (rule 144). Describing it means the option cannot disagree with what picking it does. */
function maintenancePresets(defaultCron: string): { label: string; cron: string | null }[] {
  return [
    { label: i18next.t("jobs.presets.maintenanceOff"), cron: null },
    { label: describeCron(defaultCron), cron: defaultCron },
    // Dropped when the default already IS one of these. A described label can equal a written
    // one, where the old fixed "Every day" never could, and the pair would render as two
    // identical options sharing a React key (rule 19). No shipped default collides today;
    // this is here so the next one cannot.
    ...[
      { label: i18next.t("jobs.presets.every12Hours"), cron: "0 */12 * * *" },
      { label: i18next.t("jobs.presets.every6Hours"), cron: "0 */6 * * *" },
      { label: i18next.t("jobs.presets.everyHour"), cron: "0 * * * *" },
    ].filter((p) => p.cron !== defaultCron),
  ];
}

/** Picker sentinels that are not cron lines: "off" and "type your own". */
const OFF_VALUE = "__off__";
const CUSTOM_VALUE = "__custom__";

function whenText(iso: string | null): string {
  if (!iso) return i18next.t("jobs.when.unscheduled");
  const ms = new Date(iso).getTime() - Date.now();
  if (ms <= 0) return i18next.t("jobs.when.anyMoment");
  const mins = Math.round(ms / 60000);
  if (mins < 60) return i18next.t("jobs.when.inMinutes", { mins });
  const hours = Math.round(mins / 60);
  if (hours < 24) return i18next.t("jobs.when.inHours", { hours });
  return new Date(iso).toLocaleString();
}

/** The clock as US 12-hour text, whatever the locale; the cron messages take it as a ready
 *  string. */
function clockLabel(hour: number, minute: number): string {
  const period = hour < 12 ? i18next.t("jobs.time.am") : i18next.t("jobs.time.pm");
  const hour12 = hour % 12 === 0 ? 12 : hour % 12;
  return `${hour12}:${String(minute).padStart(2, "0")} ${period}`;
}

/** A cron line in plain words, for the shapes the presets and defaults produce. Anything
 *  outside those reads as its raw line rather than a confident wrong guess.
 *
 *  Extracted in Stage 2 (docs/history/I18N_PLAN.md): the `jobs.cron.*` keys are frozen and unchanged
 *  by this extraction, per this surface's binding note. */
function describeCron(cron: string): string {
  const parts = cron.trim().split(/\s+/);
  if (parts.length !== 5) return i18next.t("jobs.cron.custom", { cron });
  const [m = "", h = "", dom = "", mon = "", dow = ""] = parts;
  const numeric = (x: string) => /^\d+$/.test(x);
  const everyDay = dom === "*" && mon === "*" && dow === "*";

  const hourStep = /^\*\/(\d+)$/.exec(h);
  if (numeric(m) && hourStep && everyDay) {
    return i18next.t("jobs.cron.everyNHours", { n: Number(hourStep[1]) });
  }
  if (numeric(m) && h === "*" && everyDay) return i18next.t("jobs.cron.everyHour");
  if (!numeric(m) || !numeric(h)) return i18next.t("jobs.cron.custom", { cron });

  const at = clockLabel(Number(h), Number(m));
  if (everyDay) return i18next.t("jobs.cron.everyDayAt", { at });
  const dayStep = /^\*\/(\d+)$/.exec(dom);
  if (dayStep && mon === "*" && dow === "*") {
    return i18next.t("jobs.cron.everyNDaysAt", { n: Number(dayStep[1]), at });
  }
  if (dom === "*" && mon === "*" && numeric(dow)) {
    return i18next.t("jobs.cron.weeklyAt", { day: weekday(Number(dow) % 7), at });
  }
  if (numeric(dom) && mon === "*" && dow === "*") {
    // The ordinal suffix lives inside the message (ICU selectordinal), so a locale can
    // decline it with the noun instead of receiving a ready-made English "21st".
    return i18next.t("jobs.cron.monthlyAt", { n: Number(dom), at });
  }
  return i18next.t("jobs.cron.custom", { cron });
}

function scanScheduleText(job: ScheduledJob | undefined, failed: boolean): string {
  // A failed load is not "still checking": say so, so the row doesn't claim to be checking
  // forever after the schedule query errored (U-6). It only costs the schedule when there is no
  // last good row to fall back on, though: React Query keeps the previous jobs and raises the
  // failure beside them, so an undivided `failed` blanked this line while the sibling JobRows
  // went on printing next-run times off that same held row, under a panel notice saying the rows
  // are kept but stale. The panel's 1.5s self-poll reaches that state with nobody touching
  // anything (rule 72: the same split `JobsPanel`'s own `StaleReadSlot` takes).
  if (failed && !job) return i18next.t("jobs.scan.scheduleUnknown");
  if (!job) return i18next.t("jobs.scan.scheduleChecking");
  if (job.cron === null) return i18next.t("jobs.scan.scheduleOff");
  return i18next.t("jobs.scan.scheduleText", {
    cron: describeCron(job.cron),
    when: whenText(job.next_run_at),
  });
}

function maintenanceScheduleText(job: ScheduledJob): string {
  if (job.cron === null) return i18next.t("jobs.maintenance.off");
  return i18next.t("jobs.maintenance.scheduleText", {
    cron: describeCron(job.cron),
    when: whenText(job.next_run_at),
  });
}

/** The one schedule editor, for the scan and every upkeep job. Presets plus "off" plus a
 *  cron line of your own; turning an upkeep job off carries a plain warning of what stops. */
function ScheduleModal({
  job,
  onClose,
  blockCloseRef,
}: {
  job: ScheduledJob;
  onClose: () => void;
  /** Set by JobsPanel so its Back guard reads the same `canClose` the scrim, Escape and the ✕
   *  read (B-11, rule 80). Written by `useBackCloseMirror`, which takes the whole predicate. */
  blockCloseRef?: RefObject<boolean>;
}) {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  // The effective server time zone every timed job runs on, so the help names the real zone
  // instead of guessing "UTC in Docker" (U-1, rule 86). Shares GeneralPanel's cache.
  const zone = useGeneralSettings().data?.timezone;
  const meta = jobMeta(job.id);
  const presets =
    job.id === SCAN_ID ? scanPresets() : maintenancePresets(job.default_cron ?? "0 4 * * *");
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
      announce(t("jobs.schedule.savedAnnounce"));
      void queryClient.invalidateQueries({ queryKey: ["schedule"] });
      onClose();
    },
    onError: (e: Error) => setError(e.message),
  });

  /** Whether a dismissal is allowed, computed once and handed to every path that can dismiss:
   *  a close mid-save unmounts the only place that failure is ever shown (B-11). */
  const canClose = !save.isPending;

  // Up to JobsPanel's Back guard, whole rather than by term (rule 80).
  useBackCloseMirror(blockCloseRef, canClose);

  const chosenCron =
    choice === OFF_VALUE ? null : choice === CUSTOM_VALUE ? custom.trim() || null : choice;
  const turningOff = chosenCron === null;
  const saveDisabled = save.isPending || (choice === CUSTOM_VALUE && custom.trim() === "");

  return (
    <ModalShell title={meta.title} onClose={onClose} canClose={canClose}>
      <div className="service-form">
        <p className="help">{meta.modalDesc ?? meta.desc}</p>

        <label className="field-sm">
          <span className="field-label">{t("jobs.schedule.howOftenLabel")}</span>
          <select
            value={choice}
            aria-label={t("jobs.schedule.howOftenLabel")}
            disabled={save.isPending}
            onChange={(e) => setChoice(e.target.value)}
          >
            {presets.map((p) => (
              <option key={p.label} value={p.cron ?? OFF_VALUE}>
                {p.label}
              </option>
            ))}
            <option value={CUSTOM_VALUE}>{t("jobs.schedule.customOption")}</option>
          </select>
          {job.default_cron && (
            <span className="help">
              {t("jobs.schedule.defaultHelp", { cron: describeCron(job.default_cron) })}
            </span>
          )}
          {/* The clock times above run on the server's configured time zone, not this browser's.
              Name the real zone so "2 AM" is not read as local time, and the operator is not left
              to guess (U-1, rule 86). Falls back to the generic phrasing only while it loads. */}
          <span className="help">
            {zone ? t("jobs.schedule.zoneHelp", { zone }) : t("jobs.schedule.zoneHelpUnknown")}
          </span>
        </label>

        {choice === CUSTOM_VALUE && (
          <label className="field-sm">
            <span className="field-label">{t("jobs.schedule.customLabel")}</span>
            <input
              type="text"
              value={custom}
              placeholder="30 4 * * *"
              aria-label={t("jobs.schedule.customLabel")}
              onChange={(e) => setCustom(e.target.value)}
            />
            <span className="help">{t("jobs.schedule.customHelp")}</span>
          </label>
        )}

        {turningOff && meta.offWarning && <Notice tone="warn">{meta.offWarning}</Notice>}
        {error && <Notice tone="error">{error}</Notice>}

        <div className="add-actions">
          <span className="flex-spacer" />
          <button className="ghost" onClick={onClose} disabled={save.isPending}>
            {t("jobs.schedule.cancel")}
          </button>
          <button
            className="primary"
            onClick={() => save.mutate(chosenCron)}
            disabled={saveDisabled}
          >
            {save.isPending ? t("jobs.schedule.saving") : t("jobs.schedule.save")}
          </button>
        </div>
      </div>
    </ModalShell>
  );
}

/** One upkeep job: what it is, when it runs, and Edit + Run now. It shows an honest
 *  "running now" while it works; none of these can delete anything. */
function JobRow({ job, onEdit }: { job: ScheduledJob; onEdit: () => void }) {
  const { t } = useTranslation();
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
          runningLabel={t("jobs.row.runningLabel")}
          lastRunAt={job.last_run_at}
          lastOk={job.last_ok}
          lastResult={job.last_result}
          flash={flash}
        />
        <div className="jobrow-sched">{maintenanceScheduleText(job)}</div>
        {run.error && (
          <Notice tone="error" inline>
            {t("jobs.row.startFailed", { message: run.error.message })}
          </Notice>
        )}
      </div>
      {/* The Jobs page stacks this row once per server-returned job, above the scan row and the
          Leaving Soon row, so "Edit" and "Run now" appear several times over with the job's name
          sitting in `.jobrow-title` where no control referenced it. */}
      <div className="jobrow-actions">
        <span className="slot-edit">
          <button
            className="ghost"
            aria-label={t("jobs.row.editAria", { title: meta.title })}
            onClick={onEdit}
          >
            {t("jobs.row.edit")}
          </button>
        </span>
        <span className="slot-act">
          <button
            className="primary"
            // Same shape as the connection test above: the state is in the name, and the
            // visible words lead so voice control still reaches it. "Run now" is not a
            // contiguous part of "Run Trash sweep now", which is what the fixed name was.
            aria-label={
              running
                ? t("jobs.row.runningAria", { title: meta.title })
                : t("jobs.row.runNowAria", { title: meta.title })
            }
            onClick={() => run.mutate()}
            disabled={running}
          >
            {running ? t("jobs.row.running") : t("jobs.row.runNow")}
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
  const { t } = useTranslation();
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
      ? { ok: false, text: t("jobs.leavingSoon.didNotUpdate") }
      : null;
  const flash = useJobFlash(runSync.isPending, syncResult);

  // One declaration behind both the row heading and the button's spoken name, so they cannot
  // drift apart (rule 144). The name has to carry the visible words "Update now" first, and
  // `title` already opens with the verb, so pasting the two together says "Update" twice.
  const shelf = t("jobs.leavingSoon.shelfName");
  const title = t("jobs.leavingSoon.title", { shelf });
  const desc = t("jobs.leavingSoon.desc");

  if (ls.isPending) {
    return (
      <div className="jobrow">
        <div className="jobrow-main">
          <div className="jobrow-title">{title}</div>
          <div className="jobrow-desc">{desc}</div>
          <div className="jobrow-sched">{t("jobs.leavingSoon.loading")}</div>
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
            {t("jobs.leavingSoon.loadFailed")}
          </Notice>
        </div>
      </div>
    );
  }

  const { enabled, last, last_skip: skip } = ls.data;
  // The row is still the best answer there is, so it renders -- and says it could not be
  // confirmed, above everything in the row the failed read could have changed.
  const stale = <StaleReadSlot plan={plan} slot={t("jobs.staleRead.shelfNoun")} inline />;
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
            <Trans
              i18nKey="jobs.leavingSoon.offNotice"
              components={{ btn: <button className="link" onClick={onGoToPlex} /> }}
            />
          </div>
        </div>
        <div className="jobrow-actions">
          <span className="slot-edit" />
          <span className="slot-act">
            <button className="primary" disabled>
              {t("jobs.leavingSoon.updateNow")}
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
          runningLabel={t("jobs.leavingSoon.updating")}
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
            {currentSkip ? (
              <Trans
                i18nKey="jobs.leavingSoon.countsAtLastUpdate"
                values={{
                  movies: last.movies,
                  movieCount: count(last.movies),
                  seasons: last.seasons,
                  seasonCount: count(last.seasons),
                }}
                components={{ movieNum: <strong />, seasonNum: <strong /> }}
              />
            ) : (
              <Trans
                i18nKey="jobs.leavingSoon.counts"
                values={{
                  movies: last.movies,
                  movieCount: count(last.movies),
                  seasons: last.seasons,
                  seasonCount: count(last.seasons),
                }}
                components={{ movieNum: <strong />, seasonNum: <strong /> }}
              />
            )}
          </div>
        )}
        <div className="jobrow-sched">{t("jobs.leavingSoon.runsAfterScan")}</div>
        <div className="jobrow-link">
          <button className="link" onClick={onGoToPlex}>
            {t("jobs.leavingSoon.manageLink")}
          </button>
        </div>
        {runSync.error && (
          <Notice tone="error" inline>
            {t("jobs.leavingSoon.updateFailed", { message: runSync.error.message })}
          </Notice>
        )}
      </div>
      <div className="jobrow-actions">
        <span className="slot-edit" />
        <span className="slot-act">
          <button
            className="primary"
            aria-label={
              running
                ? t("jobs.leavingSoon.updatingAria", { shelf })
                : t("jobs.leavingSoon.updateNowAria", { shelf })
            }
            onClick={() => runSync.mutate()}
            disabled={running}
          >
            {running ? t("jobs.leavingSoon.updating") : t("jobs.leavingSoon.updateNow")}
          </button>
        </span>
      </div>
    </div>
  );
}

export function JobsPanel({ onGoToPlex }: { onGoToPlex: () => void }) {
  const { t } = useTranslation();
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
  // ScheduleModal owns the reasons a dismissal is refused and mirrors its whole `canClose` here,
  // so Back refuses what the scrim, Escape and the ✕ refuse. Without this, Back would tear the
  // modal down mid-save and drop the error it would have shown (B-11, rule 80).
  const blockCloseRef = useRef(false);
  // Back closes the schedule editor instead of leaving Reaper, unless the modal says it can't.
  useBackGuard(
    editing !== null,
    () => setEditing(null),
    () => !blockCloseRef.current,
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
  const jobsNoun = t("jobs.staleRead.jobsNoun");
  const stale = collapseStaleReads(jobsNoun, [
    { what: jobsNoun, stale: schedule.isError && !!schedule.data },
    { what: t("jobs.staleRead.shelfNoun"), stale: shelf.isError && !!shelf.data },
  ]);

  const jobsById = new Map<string, ScheduledJob>((schedule.data?.jobs ?? []).map((j) => [j.id, j]));
  const scanJob = jobsById.get(SCAN_ID);

  return (
    <div className="panel">
      <h2>{t("jobs.panel.heading")}</h2>
      <p className="blurb">{t("jobs.panel.blurb")}</p>

      {schedule.isPending && <p className="muted">{t("jobs.panel.loadingJobs")}</p>}
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
        <Notice tone="error">{t("jobs.panel.loadFailed")}</Notice>
      )}
      {/* Both reads on this panel say the same thing when they fail together, so they say it
          once, here, above the rows (#198). Unlike Plex's four these are independent polls
          that can fail apart, which is why the rule counts the lines that would draw rather
          than grouping by invalidation: either one alone still speaks in its own words. */}
      <StaleReadSlot plan={stale} slot={jobsNoun} />

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
          blockCloseRef={blockCloseRef}
        />
      )}
    </div>
  );
}
