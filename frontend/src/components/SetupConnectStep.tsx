// SPDX-License-Identifier: AGPL-3.0-or-later
//
// The wizard's third step: the services Reaper reads from.
//
// One row per kind rather than the whole Settings services panel, which is what used to be
// inlined here. A row shows what it is for until it is connected, then lists the instances it
// holds as chips -- each chip being the control that edits that one -- so Radarr, Sonarr and
// Seerr can each take as many as the operator runs without the row growing a control per
// instance. Tautulli offers no second: it mirrors one Plex and Reaper connects to one Plex,
// which is the `singleton` flag `KINDS` already carries and the backend already refuses past.
//
// Continue is gated on `scan_ready` -- Tautulli plus at least one of Radarr or Sonarr -- which
// is the server's own predicate, not a second copy of it. Seerr sits under an Optional divider
// because a scan does not need it, and it is here rather than a step later so that every
// service connection is on one screen.
//
// The restore door is in the footer, and it is on this step for the same reason the rows are:
// restoring is the alternative to connecting services one at a time, so an operator moving an
// existing install meets it before doing the work it replaces rather than after (#385).

import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { api, type Instance, type SetupStatus } from "../api";
import { Notice } from "./Notice";
import { StaleReadNotice } from "./StaleReadNotice";
import { KINDS, ServiceModal } from "./ServiceModal";
import { SetupRestoreModal } from "./SetupRestoreModal";
import { StepCard } from "./SetupStepper";

/** The badge letters and the name a first instance of each kind arrives with.
 *
 *  The label, hint and port all come from `KINDS` (ServiceModal) rather than being restated
 *  here -- that table is the declaration, and a second copy of a service's hint is a sentence
 *  that drifts (rule 144). Only what `KINDS` does not carry lives here. */
const ROW_META: Record<string, { badge: string; names: readonly string[] }> = {
  tautulli: { badge: "TAU", names: ["Main"] },
  radarr: { badge: "RAD", names: ["HD", "4K"] },
  sonarr: { badge: "SON", names: ["HD", "4K"] },
  seerr: { badge: "SEER", names: ["Main", "Second"] },
};

/** Which kinds a scan needs, and which are merely worth having. Drives the divider, so the
 *  eye can stop at the required set instead of reading every row to find out which matter. */
const REQUIRED_KINDS = ["tautulli", "radarr", "sonarr"];

export function SetupConnectStep({
  setup,
  password,
  onBack,
  onNext,
}: {
  setup: SetupStatus;
  /** The password set on step one. Held for the restore door, which confirms with it rather
   *  than asking the operator to type it a second time. */
  password: string | null;
  onBack: () => void;
  onNext: () => void;
}) {
  const {
    data: instances,
    isPending,
    isError,
  } = useQuery({
    queryKey: ["instances"],
    queryFn: api.instances,
  });
  const [editing, setEditing] = useState<{ kind: string; instance: Instance | null } | null>(null);
  const [restoring, setRestoring] = useState(false);

  const of = (kind: string) => (instances ?? []).filter((i) => i.kind === kind);

  /** The name a new instance of this kind arrives with, stepping past the ones already taken
   *  so a second Radarr does not open on a name that will collide. */
  const nextName = (kind: string) => {
    const taken = of(kind).length;
    const pool = ROW_META[kind]?.names ?? [];
    return pool[taken] ?? `${kind} ${taken + 1}`;
  };

  const ready = setup.scan_ready;

  return (
    <StepCard step="connect" title="Connect your library">
      {/* "Scanning only reads", not "Reaper only reads": deletion goes THROUGH Radarr and
          Sonarr, so an unbounded claim is false about two of the four services whose keys are
          being typed on this screen, and the next step says so itself ("It removes files
          through them, never on its own"). The scan bound is the one every correct sibling
          already uses. Its twin is the services panel blurb in `Settings.tsx` (rule 72). */}
      <p className="blurb">Scanning only reads from these. Nothing here can delete a file.</p>

      {/* Divided, so a failed refetch does not trade a working list for one sentence while
          React Query still holds the last good answer (rule 17/36). */}
      {isError && !instances && <Notice tone="error">Couldn't load your connections.</Notice>}
      {isError && instances && <StaleReadNotice what="your connections" />}
      {isPending && <p className="muted">Loading…</p>}

      {instances && (
        <>
          <div className="conn-list">
            {KINDS.filter((k) => REQUIRED_KINDS.includes(k.value)).map((k) => (
              <ConnRow
                key={k.value}
                kind={k}
                rows={of(k.value)}
                required={k.value === "tautulli"}
                onAdd={() => setEditing({ kind: k.value, instance: null })}
                onEdit={(i) => setEditing({ kind: k.value, instance: i })}
              />
            ))}
          </div>
          <p className="conn-note">Connect at least one of Radarr or Sonarr.</p>

          <p className="conn-split">Optional</p>
          <div className="conn-list">
            {KINDS.filter((k) => !REQUIRED_KINDS.includes(k.value)).map((k) => (
              <ConnRow
                key={k.value}
                kind={k}
                rows={of(k.value)}
                required={false}
                onAdd={() => setEditing({ kind: k.value, instance: null })}
                onEdit={(i) => setEditing({ kind: k.value, instance: i })}
              />
            ))}
          </div>
        </>
      )}

      {!ready && instances && (
        // `standing`: on a fresh install this is true the moment `["instances"]` resolves at step
        // mount, which is what a wizard step opens on. Presses only ever make it go away. Its
        // twin on the scan step already declares this (rule 72).
        <Notice tone="warn" standing>
          Add Tautulli and one of Radarr or Sonarr, then you can run a scan.
        </Notice>
      )}

      <div className="step-actions">
        <button className="ghost" onClick={onBack}>
          Back
        </button>
        <span className="spacer" />
        <button className="ghost" onClick={onNext}>
          Skip for now
        </button>
        <button className="primary btn-lg" onClick={onNext} disabled={!ready}>
          Continue
        </button>
      </div>

      {/* The restore door. It sits on this step and not the first because
          `restore/confirm` refuses outright until a password exists, and the first step is
          what creates one. */}
      <div className="step-foot step-foot-door">
        <div>
          <strong>Moving an existing Reaper here?</strong> Restore a backup and your connections,
          settings and decisions come back with it. You can skip the rest of setup.
        </div>
        <button type="button" className="ghost" onClick={() => setRestoring(true)}>
          Restore a backup
        </button>
      </div>

      {editing && (
        <ServiceModal
          key={editing.instance ? editing.instance.id : `add-${editing.kind}`}
          kind={editing.kind}
          instance={editing.instance}
          // Only a NEW instance gets a starting name; editing keeps the one it was saved
          // under. Spread rather than passed as undefined, which `exactOptionalPropertyTypes`
          // treats as a different thing from absent.
          {...(editing.instance ? {} : { defaultName: nextName(editing.kind) })}
          onClose={() => setEditing(null)}
        />
      )}

      {restoring && <SetupRestoreModal password={password} onClose={() => setRestoring(false)} />}
    </StepCard>
  );
}

/** One kind's row: what it is for until it is connected, then the instances it holds. */
function ConnRow({
  kind,
  rows,
  required,
  onAdd,
  onEdit,
}: {
  kind: (typeof KINDS)[number];
  rows: Instance[];
  required: boolean;
  onAdd: () => void;
  onEdit: (i: Instance) => void;
}) {
  const connected = rows.length > 0;
  // A singleton that already has one offers no second, and its slot collapses rather than
  // holding a dead control: the chip beside it already carries the name and the connected
  // state. Same predicate the services panel uses, off the same `KINDS` flag.
  const canAdd = !kind.singleton || rows.length === 0;

  return (
    <div className={connected ? "conn-row on" : "conn-row"}>
      <span className={`conn-badge kind-${kind.value}`} aria-hidden="true">
        {ROW_META[kind.value]?.badge ?? kind.label.slice(0, 3).toUpperCase()}
      </span>
      <div>
        <div className="conn-name">
          {kind.label}
          {required && <span className="conn-tag required">Required</span>}
        </div>
        {connected ? (
          <div className="conn-instances">
            {rows.map((i) => (
              // Keyed on the server id, never the display name (rule 19, rule 63).
              <button key={i.id} type="button" className="inst-chip" onClick={() => onEdit(i)}>
                <span className="tick" aria-hidden="true">
                  ✓
                </span>
                {/* The visible text is the name alone; the reader hears what pressing it
                    does, so the chip is not just a label to a screen reader. */}
                <span className="sr-only">Edit </span>
                {i.name}
              </button>
            ))}
          </div>
        ) : (
          <div className="conn-why">{kind.hint}</div>
        )}
      </div>
      {canAdd ? (
        <button type="button" className={connected ? "conn-add" : ""} onClick={onAdd}>
          {connected ? "+ Add another" : "Connect"}
        </button>
      ) : (
        <span />
      )}
    </div>
  );
}
