// SPDX-License-Identifier: AGPL-3.0-or-later
//
// The address bar, as where a cold load lands.
//
// The app has no router (see backnav.tsx): where you are is plain React state, so a reload or a
// pasted link used to land on the review queue whatever the operator was looking at. This module
// is the one place that turns a location into a landing, and a landing back into a location.
//
// **The URL is written on every nav and read only at mount**, which is what keeps this and
// `backnav` from fighting. `backnav` stays the authority for Back: its undo restores the view
// and the lane directly, and nothing here listens for popstate or re-derives a view from the
// URL. This module has no state of its own.
//
// Every write is a `replaceState` onto the entry `backnav` has already parked, and it hands
// `history.state` straight back to the browser. That object carries the `__reaperBack` sentinel
// `backnav`'s `onOwnEntry()` asks for before stepping off an entry, so replacing it with `null`
// would turn a live Back step into a dead one, or walk the operator out of Reaper with a panel
// still open.

import type { Verdict } from "./api";
import type { View } from "./navIntent";

/** The path each section answers to. A `Record` keyed on the union, so a sixth section has to
 *  name its path here before the app compiles.
 *
 *  These are their own words rather than the nav's labels: a bookmark is a contract, and
 *  renaming a tab must not break one. */
const SECTION_PATHS: Record<View, string> = {
  review: "review",
  policy: "policy",
  reap: "reap",
  fairness: "scales",
  settings: "settings",
};

/** The review queue's three lanes, same shape and same reason. */
const LANE_PATHS: Record<Verdict, string> = {
  condemn: "condemned",
  protect: "sanctuary",
  abstain: "limbo",
};

// Read back off those same two declarations, so a hand-edited or stale link naming a section or
// a lane that no longer exists simply misses the map and falls back below.
const VIEW_BY_PATH = new Map(
  Object.entries(SECTION_PATHS).map(([view, path]) => [path, view as View]),
);
const LANE_BY_PATH = new Map(
  Object.entries(LANE_PATHS).map(([lane, path]) => [path, lane as Verdict]),
);

/** Where the app opens with nothing to go on: the review queue, on the condemned lane. */
export const DEFAULT_LANDING: { view: View; lane: Verdict } = { view: "review", lane: "condemn" };

/** The section and lane this URL names, or the defaults. Never throws: an unknown section, an
 *  unknown lane, or a lane on a section that has none each fall back, so a mangled link opens
 *  the app rather than breaking it. */
export function readLanding(): { view: View; lane: Verdict } {
  const [section, lane] = window.location.pathname.split("/").filter(Boolean);
  const view = VIEW_BY_PATH.get(section ?? "");
  if (view === undefined) return DEFAULT_LANDING;
  const named = view === "review" ? LANE_BY_PATH.get(lane ?? "") : undefined;
  return { view, lane: named ?? DEFAULT_LANDING.lane };
}

/** The review queue's URL: the lane in the path, its filters already built into `query`
 *  (`queueFilters.filtersToQuery`). */
export const reviewUrl = (lane: Verdict, query: string) =>
  `/${SECTION_PATHS.review}/${LANE_PATHS[lane]}${query}`;

/** Any other section's URL. Review is the only one carrying more than its name. */
export const sectionUrl = (view: View) => `/${SECTION_PATHS[view]}`;

//: The last URL this module wrote. Held here because the entry it was written onto does not
//: survive: `backnav` gives an entry back with a real `history.back()` whenever a layer closes
//: by anything other than a Back press (`unpark`), and again for each step of its mount walk
//: over sentinels a reload left parked (`reconcileStep`). Those traversals land AFTER React's
//: passive effects, so the address bar reverts to the entry underneath and no render follows to
//: notice. Re-asserting from a render cannot fix it: there is no render.
let written: string | null = null;

/** Put `url` on the entry the app is standing on, keeping that entry's state.
 *
 *  Never `pushState`: Back belongs to `backnav`, and an entry per nav would double every step
 *  it parks, while an entry per keystroke in the search box would bury the app under them. */
export function writeUrl(url: string): void {
  written = url;
  if (url === window.location.pathname + window.location.search) return;
  history.replaceState(history.state, "", url);
}

/** Put the last written URL back after `backnav` has stepped off the entry carrying it.
 *
 *  Called from the one branch that knows the traversal was the app's own, never from the branch
 *  that handles a user's Back press: a real Back is the operator asking to go somewhere, and the
 *  state it restores writes its own URL on the next render. Without this the ＋ Filter menu's own
 *  close discarded every filter it had just put in the address bar, and a section reached by
 *  clicking the nav reloaded onto whichever one the previous entry named. */
export function reassertUrl(): void {
  if (written === null || written === window.location.pathname + window.location.search) return;
  history.replaceState(history.state, "", written);
}

/** Forget what was written. For the test setup only: this module's `written` outlives a render
 *  tree, so without this one file's last URL would be re-asserted over the next test's own
 *  (rule 133). Production never calls it, and never wants to. */
export function forgetWrittenUrl(): void {
  written = null;
}
