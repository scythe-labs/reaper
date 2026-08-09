// SPDX-License-Identifier: AGPL-3.0-or-later
//
// Settings -> Backup & Restore: download a copy of everything Reaper knows, or put one back.
//
// The restore half is `RestoreCard`, a child that owns the staged upload and reports it upward
// through this panel. A staged file is the one unsaved thing on this screen whose loss also
// strands something on the SERVER, so the card cancels the upload on its way out and the
// section switch asks first (rule 146).

import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { api } from "../api";
import { since } from "../format";
import { RestoreCard } from "./RestoreCard";
import { StaleReadNotice } from "./StaleReadNotice";
import { Notice } from "./Notice";

export function BackupPanel({
  /** Called whenever the restore card is holding a staged backup, so the section rail can hold a
   *  switch that would drop it. Pass a STABLE function: it is an effect dependency. */
  onDirtyChange,
}: {
  onDirtyChange?: ((dirty: boolean) => void) | undefined;
} = {}) {
  const qc = useQueryClient();
  const { data, isPending, isError } = useQuery({
    queryKey: ["backup-info"],
    queryFn: api.backupInfo,
  });
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const download = async () => {
    setError(null);
    setBusy(true);
    try {
      await api.downloadBackup();
      // The server stamps "last backup" as the file goes out; pick it up.
      await qc.invalidateQueries({ queryKey: ["backup-info"] });
    } catch (err) {
      setError(err instanceof Error ? err.message : "Something went wrong.");
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="panel">
      <h2>Backup &amp; Restore</h2>
      <p className="blurb">
        Save everything Reaper knows to one file, and put it all back if you ever need to.
      </p>
      {isPending && <p className="muted">Loading…</p>}
      {/* Backup became a draft-holding panel with #135, so it takes the same two-branch read the
          other three carry: the never-loaded sentence only where nothing ever landed, and the
          shared stale line beside a card that is still on screen. Saying "Couldn't load this page"
          over a staged file and a typed password sends the operator to reload, and a reload does
          not run the unmount cleanup -- so following this panel's own advice was the one exit that
          orphaned the archive. */}
      {isError && !data && (
        <Notice tone="error">Couldn't load this page. Reload to try again.</Notice>
      )}
      {isError && data && <StaleReadNotice />}
      {data && (
        <>
          <section className="rules-card">
            <h3>Download a backup</h3>
            <p className="help">
              One file with everything Reaper decided and everything you set up, plus the keys that
              unlock your saved credentials. The rebuildable cache is left out to keep the file
              small.
            </p>
            <dl className="backup-facts">
              <dt>Inside</dt>
              <dd>Decisions, settings, credentials</dd>
              <dt>Last backup</dt>
              <dd>{data.last_backup_at ? since(data.last_backup_at) : "Never"}</dd>
            </dl>
            <div className="backup-actions">
              <button className="primary" onClick={download} disabled={busy}>
                {busy ? "Preparing…" : "Download backup"}
              </button>
            </div>
            {error && <Notice tone="error">The download didn't start: {error}</Notice>}
            {!data.key_in_backup && (
              // `standing`: where the key is set is a property of the deployment, read from
              // `["backup-info"]`, so this is on the panel from its first paint. The download
              // failure directly above answers a press and stays an alert.
              <Notice tone="warn" standing>
                Your encryption key is set through the environment, so it is not inside this backup.
                Keep that key with the file, or a restore cannot read your saved credentials.
              </Notice>
            )}
            {/* `standing`: this sits under the download button whenever the panel is open. It
                is not a reaction to anything, so an alert would cut the reader off mid-heading
                on every visit to Backup, with text that never changed. */}
            <Notice tone="warn" standing>
              This file can unlock your Plex and Sonarr/Radarr credentials. Keep it as safe as a
              password.
            </Notice>
          </section>

          {/* Rule 146's second claim, for this panel: the card is what holds the staged backup and
              it renders only inside this `data` branch, so the report and the surface arrive and
              leave together. This panel has no early return to re-read -- a failed refetch adds a
              line above without taking the card away, and React Query keeps `data` on the last
              good row, so there is no state where the card is gone and the report survives it. */}
          <RestoreCard armed={data.restore_armed} onDirtyChange={onDirtyChange} />
        </>
      )}
    </div>
  );
}
