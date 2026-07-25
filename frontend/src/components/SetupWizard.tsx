// SPDX-License-Identifier: AGPL-3.0-or-later
//
// The first-run setup flow.
//
// A fresh Reaper knows nothing about your library. Rather than drop you on an empty screen,
// this walks the two things that actually matter -- connect Radarr and Tautulli, then run a
// first (read-only) scan -- and gets out of the way the moment they're done. Sonarr, Seerr
// and Plex are offered but optional. You can skip to the full app at any time and finish
// from Settings; the checklist keeps nagging gently until everything's in place.

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useRef } from "react";
import { api } from "../api";
import { phaseLabel } from "./ScanBar";
import { BrandMark } from "../brand/BrandMark";
import { PlexPanel, ServicesPanel } from "./Settings";

// The tick is the only thing that says whether a step is finished, so it carries the state
// as text too: color and a glyph alone leave a screen reader hearing the step with no
// state at all, on the one screen a new operator cannot skip past.
function Check({ done, children }: { done: boolean; children: React.ReactNode }) {
  return (
    <li className={`setup-check ${done ? "done" : ""}`}>
      <span className="check-mark" role="img" aria-label={done ? "Done" : "Not done yet"}>
        {done ? "✓" : "○"}
      </span>
      {children}
    </li>
  );
}

export function SetupWizard({ onSkip }: { onSkip: () => void }) {
  const queryClient = useQueryClient();
  const { data: setup, isError } = useQuery({ queryKey: ["setup"], queryFn: api.setupStatus });

  // The first scan is the same background job the rest of the app uses -- start it and poll.
  const { data: scanState } = useQuery({
    queryKey: ["scanStatus"],
    queryFn: api.scanStatus,
    refetchInterval: (query) => (query.state.data?.running ? 1000 : false),
  });
  const scanning = scanState?.running ?? false;
  const wasScanning = useRef(false);
  useEffect(() => {
    if (wasScanning.current && !scanning) void queryClient.invalidateQueries();
    wasScanning.current = scanning;
  }, [scanning, queryClient]);

  // Progress and failure are separate channels: the phase line is status, a failed scan
  // is an error notice that leads with the outcome, the same shape ScanBar uses. The
  // phase itself goes through ScanBar's shared table, so this never shows a raw phase id.
  const scanError = scanState?.error ?? null;
  const scanMsg = scanning
    ? `${phaseLabel(scanState!.phase)}${scanState!.detail ? ` · ${scanState!.detail}` : ""}`
    : null;

  // A mutation, not a fire-and-forget async onClick: on a fresh install a failed start
  // (a service just removed, the server restarting) must say so, not silently do nothing.
  const firstScan = useMutation({
    mutationFn: () => api.startScan(),
    onSuccess: (started) => queryClient.setQueryData(["scanStatus"], started),
  });

  // Still waiting and couldn't-read are different states, and neither may trap the owner:
  // App.tsx routes an unreadable setup status here on the promise that Skip still works.
  if (!setup) {
    return (
      <div className="setup">
        <div className="setup-head">
          <h1>{isError ? "Welcome to Reaper" : "Setting things up…"}</h1>
          <button className="ghost" onClick={onSkip}>
            Skip to the app
          </button>
        </div>
        {isError && (
          <p className="notice notice-error">
            Couldn't check the setup state. You can skip to the app and finish from Settings.
          </p>
        )}
      </div>
    );
  }

  return (
    <div className="setup">
      <div className="setup-head">
        <div className="brand">
          <BrandMark className="brand-mark" />
          <div>
            <h1>Welcome to Reaper</h1>
            <p className="muted">
              Two quick things and you're running. Everything here only reads your library.
              Nothing can be deleted until you say so.
            </p>
          </div>
        </div>
        <button className="ghost" onClick={onSkip}>
          Skip to the app
        </button>
      </div>

      <ol className="setup-checklist">
        <Check done={setup.has_radarr || setup.has_sonarr}>
          <strong>Connect Radarr or Sonarr</strong>: where your movies and shows live{" "}
          <em>(at least one)</em>
        </Check>
        <Check done={setup.has_tautulli}>
          <strong>Connect Tautulli</strong>: your watch history <em>(required)</em>
        </Check>
        <Check done={setup.has_seerr}>
          <strong>Connect Seerr</strong>: shows who requested each title <em>(optional)</em>
        </Check>
        <Check done={setup.has_scanned}>
          <strong>Run your first scan</strong>
        </Check>
      </ol>

      <ServicesPanel />
      <PlexPanel />

      <div className="setup-finish">
        {!setup.scan_ready ? (
          <p className="muted">
            Connect Tautulli plus at least one of Radarr or Sonarr above, then you'll be able
            to run your first scan.
          </p>
        ) : scanning ? (
          <>
            <div>
              <div className="setup-scanning">
                <span className="spinner" aria-hidden="true" />
                <h2>Your first scan is running</h2>
              </div>
              <p className="muted">
                Big libraries with a lot of watch history can take a while. You don't have to
                wait here: head into the app, and the bar at the top shows the scan's progress.
                The review queue fills in the moment it finishes.
              </p>
            </div>
            <button className="primary btn-lg" onClick={onSkip}>
              Go to the app
            </button>
          </>
        ) : setup.has_scanned ? (
          <>
            <div>
              <h2>You're all set</h2>
              <p className="muted">Reaper has scanned your library.</p>
            </div>
            <button className="primary btn-lg" onClick={onSkip}>
              Go to the review queue
            </button>
          </>
        ) : (
          <>
            <div>
              <h2>Ready to scan</h2>
              <p className="muted">
                Your library and watch history are connected. Run a first scan to see what
                Reaper would reap. It only reads, and you approve every deletion by hand later.
              </p>
            </div>
            <button
              className="primary btn-lg"
              onClick={() => firstScan.mutate()}
              disabled={firstScan.isPending}
            >
              {firstScan.isPending ? "Starting…" : "Run first scan"}
            </button>
          </>
        )}
      </div>
      {firstScan.error && (
        <p className="notice notice-error">The scan didn't start: {firstScan.error.message}</p>
      )}
      {scanError && <p className="notice notice-error">The scan hit a problem: {scanError}</p>}
      {scanMsg && <p className="muted setup-scanmsg">{scanMsg}</p>}
    </div>
  );
}
