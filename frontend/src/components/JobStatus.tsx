// SPDX-License-Identifier: AGPL-3.0-or-later
import { type ReactNode, useEffect, useRef, useState } from "react";
import { Trans, useTranslation } from "react-i18next";

import { announce } from "../announce";
import type { ReasonKey } from "../api";
import { date, since, time } from "../format";
import i18next from "../i18n";
import { composeIn } from "../why";

/** A background job's outcome, composed under `jobs.result.*`. The server states the fact (a
 *  typed reason), and the browser says the words. `null` in, `null` out: a job that has never
 *  run, or a shelf row's `last`/`last_skip` before either exists, carries no reason to
 *  compose. */
export function jobResultText(key: ReasonKey | null): string | null {
  return key ? composeIn("jobs.result", key) : null;
}

/** The short-lived confirmation shown for a few seconds after a job finishes by hand. */
export interface JobFlash {
  ok: boolean;
  text: string;
}

/** What a finished job's flash says, written once because two surfaces state it: the chip
 *  renders it, and `useJobFlash` announces it at the transition. The chip otherwise reaches
 *  only an operator who happens to navigate onto it inside its 4.2-second window, which for a
 *  job they pressed Run now on is nobody. This is a plain function, not a component: it reads
 *  the catalog through the shared `i18next` instance rather than a hook (docs/history/I18N_PLAN.md §3). */
export function flashSentence(flash: JobFlash): string {
  return i18next.t("jobs.status.flashSentence", { lead: flashLead(flash.ok), text: flash.text });
}

/** The word in front of a flash's own text, in both surfaces' hands. */
function flashLead(ok: boolean): string {
  return ok ? i18next.t("jobs.status.finished") : i18next.t("common.failed");
}

/** How long the manual-run confirmation lingers before the line settles back. */
const FLASH_MS = 4200;

/**
 * Turn a job's running-to-finished transition into a brief flash.
 *
 * `running` is the authoritative "executing now" signal (an upkeep job's server flag, the
 * scan-status flag, a sync mutation's pending flag). When it falls from true to false, this
 * reads the freshly-settled `result` and shows it for a few seconds, then clears, so the one
 * status line settles back to its resting "Last run ..." state. It only fires for a
 * transition it actually watched, so a page loaded on a just-finished job never flashes.
 *
 * `result` is read only at the moment of the transition (through a ref, so a later change to
 * it never re-arms the timer), which is why the effect depends on `running` alone.
 *
 * A restart while the flash is still showing (a quick re-click right after a run finishes)
 * clears it immediately on the not-running-to-running edge, so the spinner is never hidden
 * behind the previous run's stale confirmation for the rest of its window.
 */
export function useJobFlash(running: boolean, result: JobFlash | null): JobFlash | null {
  const [flash, setFlash] = useState<JobFlash | null>(null);
  const wasRunning = useRef(false);
  const timer = useRef<number | null>(null);
  const latest = useRef(result);
  latest.current = result;

  useEffect(() => {
    if (wasRunning.current && !running && latest.current) {
      setFlash(latest.current);
      // Said at the same moment it is shown, and only on a transition this mount watched, so
      // a page loaded onto a just-finished job announces nothing, exactly as it flashes
      // nothing. All three job rows and the scan bar reach this one line.
      announce(flashSentence(latest.current));
      if (timer.current !== null) window.clearTimeout(timer.current);
      timer.current = window.setTimeout(() => setFlash(null), FLASH_MS);
    } else if (!wasRunning.current && running) {
      if (timer.current !== null) window.clearTimeout(timer.current);
      setFlash(null);
    }
    wasRunning.current = running;
  }, [running]);

  useEffect(
    () => () => {
      if (timer.current !== null) window.clearTimeout(timer.current);
    },
    [],
  );

  return flash;
}

/**
 * The one status line every job wears, in a fixed-height slot so no state ever moves the
 * rows below it. In priority order: the manual-run `flash`, then the running spinner, then
 * the resting "Last run ..." line (green dot for a success, red for a failure), then
 * "Hasn't run yet". Each state is keyed so React remounts it on a change and it fades in on
 * opacity alone -- never a height or margin change. See `.jobrow-status` in `styles/26-settings.css`.
 */
export function JobStatus({
  running,
  runningLabel,
  lastRunAt,
  lastOk,
  lastResult,
  flash,
}: {
  running: boolean;
  runningLabel: string;
  lastRunAt: string | null;
  lastOk: boolean | null;
  /** A short plain-language reason for a failed run, shown beside the exact time once the
   *  flash clears. Otherwise that reason is only ever visible for the few seconds of the
   *  flash and is unrecoverable after a reload. Ignored for a run that did not fail. */
  lastResult?: string | null;
  flash: JobFlash | null;
}) {
  const { t } = useTranslation();
  let variant: string;
  let content: ReactNode;

  if (flash) {
    variant = "flash";
    content = (
      <div className={`jobrow-last ${flash.ok ? "is-flash" : "is-flash-fail"}`}>
        <span className="flash-chip">
          {/* The glyph is hidden and the class is a color, so whether the job worked was
              carried by nothing a reader can voice. `flashSentence` is both halves of the
              answer: this renders it, and `useJobFlash` announces the same string at the
              transition, so what is read and what is spoken cannot drift. What survives the
              4.2-second window either way is the resting line below, which spells a failure
              out in words and is where the result is recovered from. */}
          <span className="sr-only">{flashLead(flash.ok)}: </span>
          <span className="check" aria-hidden="true">
            {flash.ok ? "✓" : "✕"}
          </span>{" "}
          {flash.text}
        </span>
      </div>
    );
  } else if (running) {
    variant = "run";
    content = (
      <div className="jobrow-run">
        <span className="spin" aria-hidden="true" /> {runningLabel}
      </div>
    );
  } else if (lastRunAt) {
    variant = "rest";
    const failed = lastOk === false;
    const values = { since: since(lastRunAt), date: date(lastRunAt), time: time(lastRunAt) };
    const components = { exact: <span className="last-exact" /> };
    content = (
      <div className={`jobrow-last${failed ? " is-fail" : ""}`}>
        <span className={`last-dot ${failed ? "fail" : "ok"}`} aria-hidden="true" />
        <span>
          {failed && lastResult ? (
            <Trans
              i18nKey="jobs.status.lastRunFailedReason"
              values={{ ...values, reason: lastResult }}
              components={components}
            />
          ) : failed ? (
            <Trans i18nKey="jobs.status.lastRunFailed" values={values} components={components} />
          ) : (
            <Trans i18nKey="jobs.status.lastRunOk" values={values} components={components} />
          )}
        </span>
      </div>
    );
  } else {
    variant = "never";
    content = (
      <div className="jobrow-last is-never">
        <span className="last-dot never" aria-hidden="true" /> {t("jobs.status.neverRun")}
      </div>
    );
  }

  return (
    <div className="jobrow-status">
      <div key={variant}>{content}</div>
    </div>
  );
}
