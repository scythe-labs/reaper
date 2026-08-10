// SPDX-License-Identifier: AGPL-3.0-or-later
//
// Where in the app to land, as one value.
//
// The app has no router (see backnav.tsx): where you are is plain React state in `App`, and a
// cross-page jump used to be a handful of one-off setters per destination -- "Turn it on in
// Policy → Deletion" pushed a section, "Settings → Plex" pushed a panel, Scales pushed an item
// and a lane, and each grew its own function, its own back-nav frame and its own idea of what
// else to reset. Four spellings of one thing, so a fifth destination meant a fifth spelling and
// nothing checked that they agreed.
//
// A `NavIntent` is that one thing: a whole destination, named by the caller, handed to `App`'s
// single `goTo`. Every jump in the app is one of these, so what a destination can carry is
// declared here rather than argued per call site, and anything that can build one of these
// values can drive the app -- a button, a keyboard shortcut, or a reader turning a URL into a
// destination, which the app does not have today.
//
// The three-state optionals are the load-bearing part, and they are why a jump cannot be a bag
// of setters: `undefined` means "leave this as the operator left it" and is not the same as a
// value. Landing on Review from the section nav must not disturb the open panel; landing on it
// from a lane tab must clear it. Both are `select`.

import type { Verdict } from "./api";
import type { PolicySectionId } from "./components/PolicyEditor";
import type { Panel } from "./components/Settings";

/** The app's five sections. The section nav renders these in order; `App` holds which one is up. */
export type View = "review" | "policy" | "reap" | "fairness" | "settings";

/** What the review screen's side panel is showing: one item's reasoning, one whole
 *  show, or nothing. A single slot -- opening either closes the other. */
export type Selection = { kind: "item"; id: number } | { kind: "group"; key: string } | null;

/** A destination. One variant per section, carrying only what that section can be aimed at. */
export type NavIntent =
  | {
      view: "review";
      /** Which of the three lanes to show. Omitted leaves the operator on the lane they were on.
       *
       *  A jump that opens something names the lane rather than deriving it, because only the
       *  caller knows which lane it means: a show sits in every lane one of its seasons does, so
       *  there is no single answer to derive for a group. */
      lane?: Verdict;
      /** What to open beside the queue. `null` closes whatever is open; omitted leaves it. */
      select?: Selection;
      /** What to put in the queue's search box. `""` empties it; omitted leaves whatever is
       *  typed there, which is what a jump from inside Review wants.
       *
       *  Spelled `| undefined` because callers forward their own optional straight through, and
       *  `exactOptionalPropertyTypes` counts an explicit `undefined` as a value. */
      search?: string | undefined;
    }
  | { view: "policy"; section?: PolicySectionId }
  | { view: "reap" }
  | { view: "fairness" }
  | { view: "settings"; panel?: Panel };

/** A one-shot instruction to a mounted view: what the app is currently aimed at, and which view
 *  the aim is for.
 *
 *  A destination is not a prop a view can just read: `App` holds it until the view is mounted
 *  and the view acts on it once, so revisiting that page later never replays the jump that got
 *  you there the first time. The nonce is what "once" is counted with, and the view remembers
 *  the last one it handled.
 *
 *  **One value, keyed on `view`, and that is what makes the clearing structural.** `App` used
 *  to hold three of these in parallel, one per aimable view, and an aim then had to be dropped
 *  by name on the way off screen. Both incidents in `App`'s own clearing effect are that list
 *  going stale. A focus that names its view cannot be read by another view, and it outlives its
 *  own by exactly the commit that changes `view`. The drop runs in an effect, so `App` reads
 *  every aim through a check on the name rather than trusting the state to be clean. A fourth
 *  destination is a member here and nothing else. */
export type Focus =
  | { view: "review"; search: string; nonce: number }
  | { view: "policy"; section: PolicySectionId; nonce: number }
  | { view: "settings"; panel: Panel; nonce: number };
