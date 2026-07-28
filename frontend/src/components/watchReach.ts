// SPDX-License-Identifier: AGPL-3.0-or-later
//
// How far Reaper can see into one person's watching, for the two Scales surfaces that report
// it: the board's cards and the person drawer. One derivation, because the two surfaces make
// the same claim and drew it differently -- the board guarded an unlinked account while the
// drawer went on printing a red 0% and "not watched" past the mirror's horizon (rule 72).
//
// Every watch figure Scales carries is counted with NO lower time bound (`fairness
// ._evidence_index` and `_distinct_episodes` query `watch_event` unfiltered), and the mirror
// itself begins at its horizon. So a zero is a LOWER BOUND, and on a screen built to decide
// whose files to delete a lower bound must never be drawn as a verified zero -- the same
// reasoning `ServerPopularityGate` follows when it refuses the negative past its reach
// (`engine/gates.py`), arriving here down the reporting lane instead of the deletion one.

import { date } from "../format";

/** The mirror has history, and it is theirs to read: every figure counts from `since`, and
 *  nothing before it is visible. */
export type MeasuredReach = { kind: "measured"; since: string };

/** What Reaper can say about this person's watching, and why. */
export type WatchReach =
  | MeasuredReach
  /** Their request account has no Plex account behind it, so there is no history of theirs
   *  to read. `fairness._roll_up` fills the counts only inside `if pid is not None`, so a 0
   *  here is structural, never measured. */
  | { kind: "no_account" }
  /** The watch mirror holds nothing at all, so no play is visible for anyone. Distinct from a
   *  deep horizon: there is no span to count from, so no figure means anything. */
  | { kind: "no_history" };

/**
 * Which of the three a person is in.
 *
 * `horizonAt` is the mirror's earliest play (`services.history_sync.horizon`), null only when
 * the mirror is empty. The account check comes first: without a Plex id the span is beside the
 * point, because nothing would be attributed to them however deep the history went.
 */
export function watchReach(plexId: number | null, horizonAt: string | null): WatchReach {
  if (plexId == null) return { kind: "no_account" };
  if (horizonAt == null) return { kind: "no_history" };
  return { kind: "measured", since: horizonAt };
}

/** Whether a watch figure may be shown as a number at all. False means print no percentage
 *  and no "not watched": say what could not be read instead. A type guard, so the span a
 *  qualified zero must name is in hand wherever the number itself is allowed. */
export function reachIsMeasured(reach: WatchReach): reach is MeasuredReach {
  return reach.kind === "measured";
}

/**
 * What the mirror can see, in one line, for the surfaces reporting over everybody: the span
 * it reaches back to, or that it holds nothing yet.
 *
 * Always a sentence, never null. The board rendered this only when there WAS a span, which
 * left the worst case -- a mirror that has never synced, where every card reads a red 0% --
 * as the one case carrying no caveat at all.
 */
export function mirrorNote(horizonAt: string | null): string {
  return horizonAt == null
    ? "No watch history has been read yet, so no plays show here."
    : `Watch history reaches back to ${date(horizonAt)}, so older plays are invisible here.`;
}

/** The same line for one person, or null when there is no figure of theirs to bound: an
 *  unlinked account already says so on its own tile, and naming the mirror's span beside it
 *  would suggest the span is what stopped Reaper seeing them. The drawer renders this because
 *  on a phone it is a sheet OVER the board, so the board's copy is not on screen to borrow. */
export function reachNote(reach: WatchReach): string | null {
  if (reach.kind === "no_account") return null;
  return mirrorNote(reach.kind === "measured" ? reach.since : null);
}
