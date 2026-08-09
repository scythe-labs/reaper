// SPDX-License-Identifier: AGPL-3.0-or-later
//
// Settings: everything you point Reaper at, and everything you let it do.
//
// This file is the shell. It owns the section rail, the confirm that stands between a switch
// and an unsaved edit, and the record classifying every section as holding one or not. Each
// section is its own `*Panel.tsx` beside this file, and the rail is the only thing that knows
// they are one screen.
//
// Nothing here can delete anything: the deletion switch lives in Policy -> Deletion, and the
// Security panel only manages the admin password that confirms it.

import { useEffect, useState } from "react";
import { NARROW_SCREEN_QUERY, useMediaQuery } from "../useMediaQuery";
import { AboutPanel } from "./AboutPanel";
import { BackupPanel } from "./BackupPanel";
import { GeneralPanel } from "./GeneralPanel";
import { JobsPanel } from "./JobsPanel";
import { ListsPanel } from "./ListsPanel";
import { LogsPanel } from "./LogsPanel";
import { NotificationsPanel } from "./NotificationsPanel";
import { PlexPanel } from "./PlexPanel";
import { SecurityPanel } from "./SecurityPanel";
import { ServicesPanel } from "./ServicesPanel";
import { SwitchConfirm } from "./SwitchConfirm";

// Two names that moved out of this file and are still imported FROM it: `DiscordModal` reads the
// webhook check, `SetupPasswordStep` the password floor. The name stays available at this path
// rather than sending two callers to two new ones.
//
// `PlexPanel` was a third until this split, re-exported for a `SetupWizard` that stopped reading
// it when #384 broke first-start into four steps; the export outlived its only caller by a year
// and the comment justifying it was still naming that caller.
export { isDiscordWebhook } from "./NotificationsPanel";
export { MIN_ADMIN_PASSWORD } from "./SecurityPanel";

export type Panel =
  | "general"
  | "services"
  | "plex"
  | "lists"
  | "jobs"
  | "notifications"
  | "security"
  | "backup"
  | "logs"
  | "about";

/** The ten sections, in rail order. Exported for the one test that owns the hand-written label
 *  table this must agree with (SettingsNav.test.tsx), so a section added here fails there naming
 *  what to do rather than as an unexplained label mismatch (rules 103, 144). */
export const PANELS: { id: Panel; label: string }[] = [
  { id: "general", label: "General" },
  { id: "services", label: "Services" },
  { id: "plex", label: "Plex" },
  { id: "lists", label: "Lists" },
  { id: "jobs", label: "Jobs" },
  { id: "notifications", label: "Notifications" },
  { id: "security", label: "Security" },
  // Named for both halves, matching the panel's own heading: restoring is the half an operator
  // comes looking for under pressure, and a tab reading "Backup" alone hides it.
  { id: "backup", label: "Backup & Restore" },
  { id: "logs", label: "Logs" },
  { id: "about", label: "About" },
];

export function Settings({
  initialPanel,
  onGoToPolicy,
}: {
  initialPanel?: Panel | undefined;
  /** Jump to the Policy screen's keep-rules section, for the Lists rows' policy-use links.
   *  Optional the way `SafetyBanner`'s jump is: tests mount Settings without a navigator. */
  onGoToPolicy?: (() => void) | undefined;
}) {
  const [panel, setPanel] = useState<Panel>(initialPanel ?? "general");
  // General's save bar can hold six unsaved fields at once, and switching section unmounts the
  // panel holding them. So the switch waits for a yes, the same two-step confirm the policy
  // editor's Movies/TV switch uses and in the same place: directly under the control that was
  // clicked, so that control does not move under the pointer.
  //
  // Five panels report: General's save bar; Plex's web address and manual connection rows; the
  // Discord webhook URL, a secret the operator has to go back to Discord to re-copy; Security's
  // three admin-password boxes; and Backup's staged restore, which is the only one whose loss also
  // strands something on the SERVER. The guard first landed on General alone and then on three, so
  // the rest went on unmounting silently while the app had already trained the operator to expect
  // to be asked (rule 72). Each reports through its own `onDirtyChange`; the five are `useState`
  // setters and so are stable, which that prop requires.
  //
  // The other five are spelled out below rather than left out, because `dirtyPanels` is a total
  // `Record<Panel, …>`: a panel missing from it does not compile, where an absent key used to read
  // as "holds nothing" and switch straight through. That is rule 103's one-declaration branch, and
  // it replaces a comment claiming these five "are the whole population" -- a claim nothing checked
  // against the ten in `PANELS`, so the next section added would have been unguarded and silent
  // (#156). `npm run build` runs `tsc --noEmit` and is a CI gate, so the compiler is the guard.
  //
  // The last two took a hop the first three did not: their drafts live in CHILD components
  // (`AdminPasswordForm`, `RestoreCard`), so the signal is declared there and passed up through
  // the panel. That hop is what rule 146 is about -- a child that unmounts on its parent's early
  // return takes the draft with it, so `SecurityPanel`'s failed-read branch had to change too.
  const [generalDirty, setGeneralDirty] = useState(false);
  const [plexDirty, setPlexDirty] = useState(false);
  const [webhookDirty, setWebhookDirty] = useState(false);
  const [securityDirty, setSecurityDirty] = useState(false);
  const [backupDirty, setBackupDirty] = useState(false);
  const [pendingSwitch, setPendingSwitch] = useState<Panel | null>(null);
  // Bumped on every refused press so `SwitchConfirm` can move focus even when the press changed
  // no state at all -- pressing the same section twice sets `pendingSwitch` to the value it
  // already holds, which React treats as nothing happening (see SwitchConfirm.tsx).
  const [switchNonce, setSwitchNonce] = useState(0);

  // Every panel classified, in `PANELS` order. A `false` here is a claim that the section has
  // nothing to lose on the way out, so each one says why -- verified in the tree.
  const dirtyPanels: Record<Panel, boolean> = {
    general: generalDirty,
    // Its drafts live in `ServiceModal`, inside a `ModalShell`, whose scrim
    // covers the rail and whose `trapTab` keeps Tab inside, so the switch cannot be reached while
    // one is open. A draft added to the panel BEHIND the modal would need to report.
    services: false,
    plex: plexDirty,
    // Same shape as services: a list's drafts live in `ListModal`, inside a `ModalShell`, so
    // the rail cannot be reached while one is open. This said the panel was read-only and
    // "a list is still configured where it always was" -- both untrue as of the Lists screen,
    // which is now the one place a list IS defined, and the next author to add an inline edit
    // here would have read that and left this entry alone (rule 146).
    lists: false,
    // Same shape as services: the job editor (`ScheduleModal`) is a `ModalShell` too.
    jobs: false,
    notifications: webhookDirty,
    security: securityDirty,
    backup: backupDirty,
    // Holds view filters, and its one stored setting saves the moment it changes
    // (`LogsPanel`'s `setLevel`), so there is never an unsaved edit to lose. That file carries the
    // other half of this note: a draft added there is invisible from here.
    logs: false,
    // Read-only.
    about: false,
  };
  const leavingDirty = dirtyPanels[panel];

  // The notice exists only because there are edits to lose, so it goes when they do -- by
  // Discard, or by a Save that stores them. Keyed on the draft rather than on the Discard
  // handler so the save path is covered too, which is the bug `PolicyEditor` fixed in its own
  // copy of this: it kept warning about changes that no longer existed.
  useEffect(() => {
    if (!leavingDirty) setPendingSwitch(null);
  }, [leavingDirty]);

  const switchPanel = (next: Panel) => {
    if (next === panel) return;
    if (leavingDirty) {
      setPendingSwitch(next);
      setSwitchNonce((n) => n + 1);
      return;
    }
    setPendingSwitch(null);
    setPanel(next);
  };
  const pendingLabel = PANELS.find((p) => p.id === pendingSwitch)?.label ?? "";
  // The section being LEFT, so one string serves every panel that raises the shared sentence.
  const leavingLabel = PANELS.find((p) => p.id === panel)?.label ?? "";
  // Ten labels stop fitting one line well above this, but the app already has exactly one
  // definition of a narrow screen and a second would be worse than swapping a little early:
  // below this width the section rail is a bottom bar, so a compact settings header is the
  // same shape. Rendered as one or the other, never both hidden by CSS, so only the control
  // in use is in the accessibility tree. jsdom has no matchMedia, so a test sees the rail.
  const narrow = useMediaQuery(NARROW_SCREEN_QUERY);
  return (
    <div className="settings">
      {narrow ? (
        <nav className="settings-picker" aria-label="Settings sections">
          <select
            value={panel}
            aria-label="Settings section"
            onChange={(e) => switchPanel(e.target.value as Panel)}
          >
            {PANELS.map((p) => (
              <option key={p.id} value={p.id}>
                {p.label}
              </option>
            ))}
          </select>
        </nav>
      ) : (
        <nav className="settings-nav" aria-label="Settings sections">
          {PANELS.map((p) => (
            <button
              key={p.id}
              className={panel === p.id ? "settings-tab active" : "settings-tab"}
              // Reserve the bold (active) width so switching panels never shifts the rail.
              data-label={p.label}
              // The active panel is stated, not just colored, the same as the masthead.
              aria-current={panel === p.id ? "page" : undefined}
              onClick={() => switchPanel(p.id)}
            >
              {p.label}
            </button>
          ))}
        </nav>
      )}
      {/* Directly under the rail that was clicked, so the rail does not move: the same slot and
          the same two buttons the policy editor's own switch confirm uses (rule 18).
          On General the save bar below names WHICH fields are unsaved, so this does not repeat
          them. The other four have no bar and this line is all they get: an inline Save button is
          the only other cue, and on Notifications and Security the box is a password field showing
          dots. Naming the field here is what those actually want.
          Backup gets its own sentence because the shared one would be false there: what is waiting
          is an uploaded file, not a setting, and switching does not merely forget it -- the card
          cancels the staged upload on its way out. */}
      {pendingSwitch !== null && (
        <SwitchConfirm
          nonce={switchNonce}
          message={
            panel === "backup"
              ? `The backup file you chose isn't restored yet. Switching to ${pendingLabel} drops it.`
              : `You have unsaved ${leavingLabel} settings. Switching to ${pendingLabel} discards them.`
          }
          onDiscard={() => {
            setPendingSwitch(null);
            setPanel(pendingSwitch);
          }}
          onKeep={() => setPendingSwitch(null)}
        />
      )}
      <div className="settings-body">
        {panel === "general" && <GeneralPanel onDirtyChange={setGeneralDirty} />}
        {panel === "services" && <ServicesPanel />}
        {panel === "plex" && <PlexPanel onDirtyChange={setPlexDirty} />}
        {panel === "lists" && <ListsPanel onGoToPolicy={onGoToPolicy} />}
        {panel === "jobs" && <JobsPanel onGoToPlex={() => switchPanel("plex")} />}
        {panel === "notifications" && <NotificationsPanel onDirtyChange={setWebhookDirty} />}
        {panel === "security" && <SecurityPanel onDirtyChange={setSecurityDirty} />}
        {panel === "backup" && <BackupPanel onDirtyChange={setBackupDirty} />}
        {panel === "logs" && <LogsPanel />}
        {panel === "about" && <AboutPanel />}
      </div>
    </div>
  );
}
