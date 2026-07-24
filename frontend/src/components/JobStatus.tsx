// SPDX-License-Identifier: AGPL-3.0-or-later
import { type ReactNode, useEffect, useRef, useState } from "react";

import { date, since, time } from "../format";

/** The short-lived confirmation shown for a few seconds after a job finishes by hand. */
export interface JobFlash {
  ok: boolean;
  text: string;
}

/** How long the manual-run confirmation lingers before the line settles back. */
const FLASH_MS = 4200;

/**
 * Turn a job's running -> finished transition into a brief flash.
 *
 * `running` is the authoritative "executing now" signal (an upkeep job's server flag, the
 * scan-status flag, a sync mutation's pending flag). When it falls from true to false we
 * read the freshly-settled `result` and show it for a few seconds, then clear -- so the one
 * status line settles back to its resting "Last run ..." state. It only fires for a
 * transition we actually watched, so a page loaded on a just-finished job never flashes.
 *
 * `result` is read only at the moment of the transition (through a ref, so a later change to
 * it never re-arms the timer), which is why the effect depends on `running` alone.
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
      if (timer.current !== null) window.clearTimeout(timer.current);
      timer.current = window.setTimeout(() => setFlash(null), FLASH_MS);
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
 * opacity alone -- never a height or margin change. See `.jobrow-status` in `index.css`.
 */
export function JobStatus({
  running,
  runningLabel,
  lastRunAt,
  lastOk,
  flash,
}: {
  running: boolean;
  runningLabel: string;
  lastRunAt: string | null;
  lastOk: boolean | null;
  flash: JobFlash | null;
}) {
  let variant: string;
  let content: ReactNode;

  if (flash) {
    variant = "flash";
    content = (
      <div className={`jobrow-last ${flash.ok ? "is-flash" : "is-flash-fail"}`}>
        <span className="flash-chip">
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
    content = (
      <div className={`jobrow-last${failed ? " is-fail" : ""}`}>
        <span className={`last-dot ${failed ? "fail" : "ok"}`} aria-hidden="true" />
        <span>
          Last run {failed ? "failed " : ""}
          {since(lastRunAt)}{" "}
          <span className="last-exact">
            · {date(lastRunAt)}, {time(lastRunAt)}
          </span>
        </span>
      </div>
    );
  } else {
    variant = "never";
    content = (
      <div className="jobrow-last is-never">
        <span className="last-dot never" aria-hidden="true" /> Hasn't run yet
      </div>
    );
  }

  return (
    <div className="jobrow-status">
      <div key={variant}>{content}</div>
    </div>
  );
}
