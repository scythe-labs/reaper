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

/** Put `url` on the entry the app is standing on, keeping that entry's state.
 *
 *  Never `pushState`: Back belongs to `backnav`, and an entry per nav would double every step
 *  it parks, while an entry per keystroke in the search box would bury the app under them. */
export function writeUrl(url: string): void {
  if (url === window.location.pathname + window.location.search) return;
  history.replaceState(history.state, "", url);
}
