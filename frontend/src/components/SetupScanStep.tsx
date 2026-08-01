// SPDX-License-Identifier: AGPL-3.0-or-later
//
// The wizard's last step: run the first scan.
//
// Two progress surfaces, one number. The bar on the card and the line pinned to the top of the
// window both read `ScanStatus.percent` -- the monotonic 0-100 the API already returns for
// exactly this, unlike done/total whose denominator changes meaning between phases. The wizard
// had neither before: it showed a spinner and a phase line, and told the operator to watch a
// bar at the top of the app that does not exist until they leave, because `ScanLine` renders
// inside `Dashboard` and the setup branch returns above it. Drawing the line here makes the cue
// familiar before they get to the app, and stops a scan looking like it stopped when they go.
//
// Discord sits here rather than earlier on purpose: it is optional, and optional work belongs
// after the finish line, while the operator is already waiting on a scan that can run for
// minutes. Putting it before the scan is what makes a wizard feel long.

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useRef, useState } from "react";
import { announce } from "../announce";
import { api, type SetupStatus } from "../api";
import { DiscordModal } from "./DiscordModal";
import { Notice } from "./Notice";
import { phaseLabel } from "./ScanBar";
import { ScanLine } from "./ScanLine";
import { StepCard } from "./SetupStepper";

export function SetupScanStep({
  setup,
  onBack,
  onDone,
}: {
  setup: SetupStatus;
  onBack: () => void;
  /** Leave the wizard for the app. */
  onDone: () => void;
}) {
  const queryClient = useQueryClient();
  const [discordOpen, setDiscordOpen] = useState(false);

  const { data: scan } = useQuery({
    queryKey: ["scanStatus"],
    queryFn: api.scanStatus,
    refetchInterval: (query) => (query.state.data?.running ? 1000 : false),
  });
  const running = scan?.running ?? false;
  const percent = scan?.percent ?? 0;

  // Read at the transition rather than depended on: the message lands in the same poll that
  // turns `running` off, and a later change to it must not re-fire the effect.
  const wasRunning = useRef(false);
  const latestError = useRef(scan?.error ?? null);
  latestError.current = scan?.error ?? null;
  useEffect(() => {
    if (wasRunning.current && !running) {
      void queryClient.invalidateQueries();
      // The whole panel changes when the poll flips `running` off, on nothing the operator
      // did. It branches for the same reason `useScanSettled` does, and this is its sibling
      // (rule 72): `api/scan.py` clears `running` in a `finally`, so a crashed scan arrives at
      // this exact edge and the panel behind it does NOT become "all set".
      announce(
        latestError.current === null
          ? "Your first scan finished. Reaper has scanned your library."
          : "Your first scan stopped before it finished. You can start it again.",
      );
    }
    wasRunning.current = running;
  }, [running, queryClient]);

  const start = useMutation({
    mutationFn: () => api.startScan(),
    onSuccess: (started) => {
      queryClient.setQueryData(["scanStatus"], started);
      // The press replaces this half of the card with a spinner and a bar, with nothing said,
      // for an operation the copy itself warns can take a while. The wording carries the
      // permission to walk away, because that is the useful part of a wait this long.
      announce("Your first scan is running. You don't have to wait here.");
    },
  });

  // Progress and failure are separate channels: the phase line is status, a failed scan is an
  // error notice leading with the outcome. The phase goes through ScanBar's shared table, so
  // this can never print a raw phase id (rule 66).
  const phase = running
    ? `${phaseLabel(scan!.phase)}${scan!.detail ? `, ${scan!.detail}` : ""}`
    : null;

  return (
    <>
      {/* The app's own top-of-window cue, drawn here too so the scan the operator just
          started does not appear to stop when they leave for the app. */}
      <ScanLine running={running} percent={percent} />

      <StepCard step="scan" title="Run your first scan">
        {running ? (
          <>
            <div className="setup-scanning">
              <span className="spinner" aria-hidden="true" />
              <h3>Your first scan is running</h3>
            </div>
            <div
              className="bar"
              role="progressbar"
              aria-label="Scanning your library"
              aria-valuemin={0}
              aria-valuemax={100}
              aria-valuenow={Math.round(percent)}
            >
              <div className="bar-fill" style={{ width: `${percent}%` }} />
            </div>
            <p className="blurb">
              You can leave this page; it keeps running. The line at the very top of the window
              follows it, here and in the app, and the review queue fills in the moment it finishes.
            </p>
            {phase && <p className="muted setup-scanmsg">{phase}</p>}

            <div className="setup-later">
              <h3>While that runs</h3>
              <p className="blurb">Optional, and you can add it any time from Settings.</p>
              <DiscordRow onOpen={() => setDiscordOpen(true)} />
            </div>

            <div className="step-actions">
              <span className="spacer" />
              <button className="primary btn-lg" onClick={onDone}>
                Go to the app
              </button>
            </div>
          </>
        ) : setup.has_scanned ? (
          <>
            <h3>You're all set</h3>
            <p className="blurb">
              Reaper has scanned your library. Nothing has been touched: open the queue to see what
              it would remove, and why it picked each one.
            </p>
            <div className="step-actions">
              <button className="ghost" onClick={onBack}>
                Back
              </button>
              <span className="spacer" />
              <button className="primary btn-lg" onClick={onDone}>
                Go to the review queue
              </button>
            </div>
          </>
        ) : (
          <>
            <p className="blurb">
              Reaper reads your library and shows what it would remove, and why. You approve every
              deletion by hand.
            </p>
            {/* The one thing a new operator most needs to believe on this screen, in the
                shared banner the rest of the app states safety in (rule 18). */}
            <div className="banner banner-safe">
              <span className="banner-dot" aria-hidden="true" />
              Deletion is off. It stays off until you turn it on with the password you set.
            </div>
            <div className="step-actions">
              <button className="ghost" onClick={onBack}>
                Back
              </button>
              <span className="spacer" />
              {/* The wizard's standing promise: every step past the password can be left for
                  the app. Without it an install that has not scanned yet is held here, since
                  the only other button starts a scan. */}
              <button className="ghost" onClick={onDone}>
                Go to the app
              </button>
              <button
                className="primary btn-lg"
                onClick={() => start.mutate()}
                disabled={start.isPending}
              >
                {start.isPending ? "Starting…" : "Run first scan"}
              </button>
            </div>
          </>
        )}

        {start.error && <Notice tone="error">The scan didn't start: {start.error.message}</Notice>}
        {scan?.error && <Notice tone="error">The scan hit a problem: {scan.error}</Notice>}
      </StepCard>

      {discordOpen && <DiscordModal onClose={() => setDiscordOpen(false)} />}
    </>
  );
}

/** Discord in the same row grammar as the service connections one step back, so a connection
 *  looks like a connection wherever the operator meets it. */
function DiscordRow({ onOpen }: { onOpen: () => void }) {
  const { data } = useQuery({ queryKey: ["notifications"], queryFn: api.notifications });
  const connected = data?.has_webhook ?? false;
  return (
    <div className="conn-list">
      <div className={connected ? "conn-row on" : "conn-row"}>
        <span className="conn-badge kind-discord" aria-hidden="true">
          DIS
        </span>
        <div>
          <div className="conn-name">Discord</div>
          <div className="conn-why">Tells your users what's leaving before it goes.</div>
        </div>
        <button type="button" className="conn-add" onClick={onOpen}>
          {connected ? "Edit" : "Add"}
        </button>
      </div>
    </div>
  );
}
