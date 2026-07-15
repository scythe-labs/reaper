// SPDX-License-Identifier: AGPL-3.0-or-later
//
// The first-run setup flow.
//
// A fresh Reaper knows nothing about your library. Rather than drop you on an empty screen,
// this walks the two things that actually matter -- connect Radarr and Tautulli, then run a
// first (read-only) scan -- and gets out of the way the moment they're done. Sonarr, Seerr
// and Plex are offered but optional. You can skip to the full app at any time and finish
// from Settings; the checklist keeps nagging gently until everything's in place.

import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useRef } from "react";
import { api } from "../api";
import { PlexPanel, ServicesPanel } from "./Settings";

function Check({ done, children }: { done: boolean; children: React.ReactNode }) {
  return (
    <li className={`setup-check ${done ? "done" : ""}`}>
      <span className="check-mark" aria-hidden="true">
        {done ? "✓" : "○"}
      </span>
      {children}
    </li>
  );
}

export function SetupWizard({ onSkip }: { onSkip: () => void }) {
  const queryClient = useQueryClient();
  const { data: setup } = useQuery({ queryKey: ["setup"], queryFn: api.setupStatus });

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

  const scanMsg = scanState?.error
    ? scanState.error
    : scanning
      ? `${scanState!.phase}${scanState!.detail ? ` — ${scanState!.detail}` : ""}`
      : null;

  const runFirstScan = async () => {
    const started = await api.startScan();
    queryClient.setQueryData(["scanStatus"], started);
  };

  if (!setup) {
    return (
      <div className="setup">
        <div className="setup-head">
          <h1>Setting things up…</h1>
        </div>
      </div>
    );
  }

  return (
    <div className="setup">
      <div className="setup-head">
        <div className="brand">
          <svg className="brand-mark" viewBox="0 0 48 48" fill="none" aria-hidden="true">
            <path d="M31 9C17 9 9 17 9 29c8-8 16-12 26-10-1-5-2-8-4-10Z" fill="currentColor" />
            <path d="M31 9 19 40" stroke="currentColor" strokeWidth="3.5" strokeLinecap="round" />
          </svg>
          <div>
            <h1>Welcome to Reaper</h1>
            <p className="muted">
              Two quick things and you're running. Everything here only reads your library —
              nothing can be deleted until you say so.
            </p>
          </div>
        </div>
        <button className="ghost" onClick={onSkip}>
          Skip to the app
        </button>
      </div>

      <ol className="setup-checklist">
        <Check done={setup.has_radarr}>
          <strong>Connect Radarr</strong> — where your movies live <em>(required)</em>
        </Check>
        <Check done={setup.has_tautulli}>
          <strong>Connect Tautulli</strong> — your watch history <em>(required)</em>
        </Check>
        <Check done={setup.has_seerr || setup.has_scanned}>
          <strong>Connect Sonarr / Seerr</strong> — for TV pruning and requests <em>(optional)</em>
        </Check>
        <Check done={setup.has_scanned}>
          <strong>Run your first scan</strong>
        </Check>
      </ol>

      <ServicesPanel />
      <PlexPanel />

      <div className="setup-finish">
        {setup.scan_ready ? (
          <>
            <div>
              <h2>Ready to scan</h2>
              <p className="muted">
                Radarr and Tautulli are connected. Run a first scan to see what Reaper would
                reap — it only reads, and you approve every deletion by hand later.
              </p>
            </div>
            <button className="primary btn-lg" onClick={runFirstScan} disabled={scanning}>
              {scanning ? "Scanning…" : "Run first scan"}
            </button>
          </>
        ) : (
          <p className="muted">
            Add at least a Radarr and a Tautulli above, then you'll be able to run your first scan.
          </p>
        )}
      </div>
      {scanMsg && <p className="muted setup-scanmsg">{scanMsg}</p>}

      {setup.complete && (
        <div className="setup-done">
          <strong>You're all set.</strong> Reaper has scanned your library.
          <button className="primary" onClick={onSkip}>
            Go to the review queue
          </button>
        </div>
      )}
    </div>
  );
}
