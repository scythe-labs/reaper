// SPDX-License-Identifier: AGPL-3.0-or-later
//
// The background-scan cue: a thin line pinned to the top of the window.
//
// It lived in App.tsx, beside the only place that drew it. The setup wizard draws it too now
// -- the wizard replaces the app shell, so a scan started there had no top line at all until
// the operator left, and the wizard's own copy told them to watch for one -- and a component
// importing App would close a cycle, since App is what renders the wizard. So it moved here,
// which is the same answer rule 18 gives: one implementation, imported twice, rather than a
// second copy that drifts.

import i18next from "../i18n";

/** What a running scan is called, wherever one is announced or named for a screen reader: this
 *  line, the scan bar's own track and its start announcement, and the wizard's first-run track.
 *  The scan bar's two shared a constant; this line and the wizard each wrote it out (rule 144).
 *
 *  A function, not a constant: this module is in the eager bundle, so a string resolved in its
 *  body would stay English for the life of the page (`i18n-module-scope.test.ts`). */
export const scanningLabel = () => i18next.t("shell.scanLine.scanningLabel");

/** A thin accent line pinned to the very top of the window while a scan runs in the
 *  background, filling to the scan's real percent and gone the moment it finishes. Ambient
 *  by design: it only says "a scan is working"; the phase, counts and controls live on the
 *  scan bar (Settings → Jobs), which is where you go to actually read them.
 *
 *  Unlike SafetyBanner this is not a safety surface, so it may show nothing when it knows
 *  nothing: an absent line reads as "idle", the calm and correct default, and a dropped
 *  status poll must not paint a scan that may not be running. (The armed-state banner is the
 *  surface that must never fail quiet.) Kept mounted so it can fade rather than pop, and
 *  aria-hidden while idle so a screen reader hears it only when there is activity. */
export function ScanLine({ running, percent }: { running: boolean; percent: number }) {
  const pct = Math.max(0, Math.min(100, percent));
  return (
    <div
      className={running ? "scanline on" : "scanline"}
      role="progressbar"
      aria-label={scanningLabel()}
      aria-hidden={!running}
      aria-valuemin={0}
      aria-valuemax={100}
      aria-valuenow={running ? Math.round(pct) : undefined}
    >
      <div className="scanline-fill" style={{ width: `${pct}%` }} />
    </div>
  );
}
