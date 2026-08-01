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
//
// **The finish panel does not say "all set" unless a run could go ahead.** Skipping Plex left
// an install that scans and shows a queue and then refuses the first real reap, and this was
// the screen that told the operator they were finished (#383). The refusal itself is right and
// stays; what was missing is that nothing said so until they had picked what to delete and
// pressed the button. `reapBlockers` is the shared list, read here and on the Reap page.

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useRef, useState } from "react";
import { announce } from "../announce";
import { api, type SetupStatus } from "../api";
import { reapBlockers, type ReapBlocker } from "../reapReadiness";
import { DiscordModal } from "./DiscordModal";
import { Notice } from "./Notice";
import { SafetyBanner } from "./SafetyBanner";
import { phaseLabel } from "./ScanBar";
import { ScanLine } from "./ScanLine";
import { StepCard } from "./SetupStepper";

export function SetupScanStep({
  setup,
  onBack,
  onGoToPlex,
  onDone,
}: {
  setup: SetupStatus;
  onBack: () => void;
  /** Back to the Plex step, offered inside the notice that says why a run would be refused
   *  without it. A warning naming a fix carries the control that applies it (rule 42), and
   *  here that control is two steps back rather than on this screen. */
  onGoToPlex: () => void;
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

  // What would still turn a real run away, in the same words the Reap page uses. Read here
  // rather than off `setup.reap_ready` directly, because "not ready" is not a sentence: the
  // operator on the last screen of setup needs to know what to go and do.
  const blockers = reapBlockers(setup);

  // Read at the transition rather than depended on: the message lands in the same poll that
  // turns `running` off, and a later change to it must not re-fire the effect.
  const wasRunning = useRef(false);
  const latestError = useRef(scan?.error ?? null);
  latestError.current = scan?.error ?? null;
  // Held the same way, and additionally because `blockers` is freshly allocated every render,
  // which must never be an effect dependency (rule 19).
  const latestBlockers = useRef<ReapBlocker[]>(blockers);
  latestBlockers.current = blockers;
  useEffect(() => {
    if (wasRunning.current && !running) {
      void queryClient.invalidateQueries();
      // The whole panel changes when the poll flips `running` off, on nothing the operator
      // did. It branches for the same reason `useScanSettled` does, and this is its sibling
      // (rule 72): `api/scan.py` clears `running` in a `finally`, so a crashed scan arrives at
      // this exact edge and the panel behind it does NOT become "all set".
      //
      // The blocker rides on this sentence rather than announcing itself, because it lands in
      // the same commit: its notice is `standing`, so it is read in document order like the
      // rest of the panel, and two alerts firing at one edge would talk over each other.
      const blocker = latestBlockers.current[0];
      announce(
        latestError.current !== null
          ? "Your first scan stopped before it finished. You can start it again."
          : blocker
            ? `Your first scan finished. ${blocker.sentence}`
            : "Your first scan finished. Reaper has scanned your library.",
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
            {/* "All set" is a claim about what the install can do, so it is only made when a
                real run could actually go ahead. It could not, before: an operator who
                skipped Plex was told they were all set and had their first reap refused at
                the button, four screens later, with nothing before that saying so (#383). */}
            <h3>{blockers.length === 0 ? "You're all set" : "Your library is scanned"}</h3>
            <p className="blurb">
              Nothing has been touched: open the queue to see what Reaper would remove, and why it
              picked each one.
            </p>
            {blockers.map((b) => (
              // `standing`: this is the state of the install whenever this panel is on
              // screen, not a reply to anything the operator just pressed.
              //
              // The fix rides INSIDE the notice, not as a third button in the row below
              // (rule 42, and rule 18). Two reasons, and the second is why it changed: a
              // link beside the sentence is anchored to the reason it answers, and the
              // action row is navigation -- Back, and the way onward -- so a repair
              // affordance sitting in it reads as a third way out of the step. It also
              // makes this the same shape the Reap page uses for the same job, which two
              // different treatments of one fix would not have been.
              <Notice key={b.key} tone="warn" standing>
                {b.sentence}
                {b.key === "plex" && (
                  <>
                    {" "}
                    <button className="link" onClick={onGoToPlex}>
                      Connect Plex
                    </button>
                  </>
                )}
              </Notice>
            ))}
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
            {/* The one thing a new operator most needs to believe on this screen, so it is
                read rather than asserted. This was a hardcoded sentence in the green tone,
                saying deletion was off and consulting nothing: a deploy carrying
                `REAPER_DESTRUCTIVE_ACTIONS_ENABLED=true` boots armed with `complete` still
                false, which routes here, and the wizard returns above `Dashboard` so this is
                the only place the regime is stated at all. Wrong in the reassuring direction,
                which is the direction rule 144 says a rounded claim fails in. The shared
                banner has the three honest branches, amber unknown included (rule 18). */}
            <SafetyBanner />
            {/* The same courtesy the finish panel pays `reap_ready`, paid to `scan_ready` on
                the button that consumes it (rule 72). Without it, skipping Connect with
                nothing wired left "Run first scan" live: the start route answers 200, the
                panel turns into a spinner, and the refusal arrives from inside the detached
                task pointing at Settings, which is behind the wizard the operator has not
                left. The fix rides inside the notice (rule 42), and it is one step back.

                `standing`: `scan_ready` is the state of the install, true before this step was
                reached, so it is read in document order like the banner above it. */}
            {!setup.scan_ready && (
              <Notice tone="warn" standing>
                Connect Tautulli and one of Radarr or Sonarr before your first scan.{" "}
                <button className="link" onClick={onBack}>
                  Connect services
                </button>
              </Notice>
            )}
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
                disabled={start.isPending || !setup.scan_ready}
              >
                {start.isPending ? "Starting…" : "Run first scan"}
              </button>
            </div>
          </>
        )}

        {start.error && <Notice tone="error">The scan didn't start: {start.error.message}</Notice>}
        {/* `standing`, unlike the Start refusal above it: a scan already running or already
            crashed server-side is on this step the moment it mounts, and once one is running the
            1s poll delivers the failure with nothing pressed. `ScanBar` says the same thing about
            the same field and moves with it (rule 72). */}
        {scan?.error && (
          <Notice tone="error" standing>
            The scan hit a problem: {scan.error}
          </Notice>
        )}
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
