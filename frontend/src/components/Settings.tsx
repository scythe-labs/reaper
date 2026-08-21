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

import { useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import i18next from "../i18n";
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
import { SwitchConfirm, useSwitchConfirm } from "./SwitchConfirm";

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
  { id: "general", label: i18next.t("shell.settings.panels.general") },
  { id: "services", label: i18next.t("shell.settings.panels.services") },
  { id: "plex", label: i18next.t("shell.settings.panels.plex") },
  { id: "lists", label: i18next.t("shell.settings.panels.lists") },
  { id: "jobs", label: i18next.t("shell.settings.panels.jobs") },
  { id: "notifications", label: i18next.t("shell.settings.panels.notifications") },
  { id: "security", label: i18next.t("shell.settings.panels.security") },
  // Named for both halves, matching the panel's own heading: restoring is the half an operator
  // comes looking for under pressure, and a tab reading "Backup" alone hides it.
  { id: "backup", label: i18next.t("shell.settings.panels.backup") },
  { id: "logs", label: i18next.t("shell.settings.panels.logs") },
  { id: "about", label: i18next.t("shell.settings.panels.about") },
];

export function Settings({
  panel,
  onPanelChange,
  jump,
  onGoToPolicy,
}: {
  /** Which section is open. Owned by `App`, not by this file: the address bar names the open
   *  panel (`/settings/logs`), and the URL is written where every other nav is written. */
  panel: Panel;
  /** Where a rail click goes. Every path that changes the panel runs the confirm below first,
   *  so this is called with a destination the operator has already agreed to leave for. */
  onPanelChange: (next: Panel) => void;
  /** A panel asked for from outside, by the user menu's update item. An ask rather than an
   *  instruction: it goes through the same confirm the rail does, so edits on the panel it would
   *  close are not lost without a word (#794). The nonce makes each ask fire once. */
  jump?: { panel: Panel; nonce: number } | null;
  /** Jump to the Policy screen's keep-rules section, for the Lists rows' policy-use links.
   *  Optional the way `SafetyBanner`'s jump is: tests mount Settings without a navigator. */
  onGoToPolicy?: (() => void) | undefined;
}) {
  const { t } = useTranslation();
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

  // The refusal, the focus nonce, and the clear-on-clean are `useSwitchConfirm`'s, shared with
  // the policy editor's Movies/TV switch. This file supplies only what is local: which panel is
  // open, whether it holds anything, and what a yes does.
  //
  // A yes commits through `onPanelChange`, which is where the open panel lives now. EVERY switch
  // goes through this hook: the rail, the narrow-screen picker, the Jobs panel's link to Plex,
  // and the jump below, which is the one that used to go around it (#794).
  const confirmSwitch = useSwitchConfirm(panel, leavingDirty, onPanelChange);

  // A jump asks for its panel exactly once. `request` reads `dirty` and either moves or raises
  // the notice, so the ask and a rail click are the same press from here down. Handled once per
  // nonce, so a re-render never re-asks: the operator may have pressed "Keep editing", and
  // asking again would put the notice back under a rail they did not touch.
  const handledJump = useRef(0);
  useEffect(() => {
    if (!jump || jump.nonce === handledJump.current) return;
    handledJump.current = jump.nonce;
    confirmSwitch.request(jump.panel);
  }, [jump, confirmSwitch]);
  const pendingLabel = PANELS.find((p) => p.id === confirmSwitch.pending)?.label ?? "";
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
        <nav className="settings-picker" aria-label={t("shell.settings.sectionsLabel")}>
          <select
            value={panel}
            aria-label={t("shell.settings.sectionLabel")}
            onChange={(e) => confirmSwitch.request(e.target.value as Panel)}
          >
            {PANELS.map((p) => (
              <option key={p.id} value={p.id}>
                {p.label}
              </option>
            ))}
          </select>
        </nav>
      ) : (
        <nav className="settings-nav" aria-label={t("shell.settings.sectionsLabel")}>
          {PANELS.map((p) => (
            <button
              key={p.id}
              className={panel === p.id ? "settings-tab active" : "settings-tab"}
              // Reserve the bold (active) width so switching panels never shifts the rail.
              data-label={p.label}
              // The active panel is stated, not just colored, the same as the masthead.
              aria-current={panel === p.id ? "page" : undefined}
              onClick={() => confirmSwitch.request(p.id)}
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
      {confirmSwitch.pending !== null && (
        <SwitchConfirm
          nonce={confirmSwitch.nonce}
          message={
            panel === "backup"
              ? t("shell.settings.switchConfirmBackup", { pendingLabel })
              : t("shell.settings.switchConfirmGeneric", { leavingLabel, pendingLabel })
          }
          onDiscard={confirmSwitch.discard}
          onKeep={confirmSwitch.keep}
        />
      )}
      <div className="settings-body">
        {panel === "general" && <GeneralPanel onDirtyChange={setGeneralDirty} />}
        {panel === "services" && <ServicesPanel />}
        {panel === "plex" && <PlexPanel onDirtyChange={setPlexDirty} />}
        {panel === "lists" && <ListsPanel onGoToPolicy={onGoToPolicy} />}
        {panel === "jobs" && <JobsPanel onGoToPlex={() => confirmSwitch.request("plex")} />}
        {panel === "notifications" && <NotificationsPanel onDirtyChange={setWebhookDirty} />}
        {panel === "security" && <SecurityPanel onDirtyChange={setSecurityDirty} />}
        {panel === "backup" && <BackupPanel onDirtyChange={setBackupDirty} />}
        {panel === "logs" && <LogsPanel />}
        {panel === "about" && <AboutPanel />}
      </div>
    </div>
  );
}
