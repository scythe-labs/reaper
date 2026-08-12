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
import type { PolicySectionId } from "./components/PolicyEditor";
import type { Panel } from "./components/Settings";
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

/** Settings' ten panels. Same shape again: an eleventh panel does not compile until it names a
 *  path here. */
const PANEL_PATHS: Record<Panel, string> = {
  general: "general",
  services: "services",
  plex: "plex",
  lists: "lists",
  jobs: "jobs",
  notifications: "notifications",
  security: "security",
  backup: "backup",
  logs: "logs",
  about: "about",
};

/** The policy editor's four sections. Its rail reads "What flags a title", so these are the
 *  short words rather than the labels, and a label reworded on the page keeps every link. */
const POLICY_PATHS: Record<PolicySectionId, string> = {
  flags: "flags",
  kept: "kept",
  pace: "pace",
  deletion: "deletion",
};

/** The two policies the editor edits. Movies and TV are tuned separately, so a policy URL that
 *  names only the section restores half a location: the caps, the byte budget and the weights on
 *  screen are the other media type's. Their own words again, so the Movies/TV switch can be
 *  relabelled without breaking a link. */
const MEDIA_PATHS: Record<"movie" | "tv", string> = {
  movie: "movies",
  tv: "tv",
};

/** The reverse of any of those five declarations: which key answers to this path, or nothing.
 *
 *  It searches the record's own entries and compares values, so `__proto__` and `constructor`
 *  are ordinary misses, and a hand-edited or stale link naming a section, a lane, a panel, a
 *  media type or a policy section that no longer exists falls back below. The longest record
 *  holds ten entries and this runs at mount alone. */
const keyFor = <T extends string>(paths: Record<T, string>, path: string): T | undefined =>
  (Object.entries(paths) as [T, string][]).find(([, p]) => p === path)?.[0];

/** Everywhere a cold load can land: the section, and what that section has open inside it. Each
 *  field is read by the one piece of state that owns it in `App`. */
type Landing = {
  view: View;
  lane: Verdict;
  panel: Panel;
  policyMedia: "movie" | "tv";
  policySection: PolicySectionId;
};

/** Where the app opens with nothing to go on: the review queue, on the condemned lane, with each
 *  section's own first panel behind it. */
export const DEFAULT_LANDING: Landing = {
  view: "review",
  lane: "condemn",
  panel: "general",
  policyMedia: "movie",
  policySection: "flags",
};

/** What this URL names, or the defaults. Never throws, and each value falls back on its own: an
 *  unknown section, an unknown segment under it, or a segment on a section that has none is a
 *  miss and nothing more. `/settings/nonsense` is Settings on General,
 *  `/policy/nonsense/deletion` is the Movies policy on Deletion, and a bare `/policy` is the
 *  default of both. The write below then puts the real location in the address bar. */
export function readLanding(): Landing {
  const [first, second = "", third = ""] = window.location.pathname.split("/").filter(Boolean);
  const view = keyFor(SECTION_PATHS, first ?? "");
  if (view === undefined) return DEFAULT_LANDING;
  // Policy is the one section whose location is two values, so it reads a third segment. Media
  // comes first, matching `/review/<lane>`: the broad split, then where you are inside it.
  const policy = view === "policy";
  return {
    view,
    lane: (view === "review" ? keyFor(LANE_PATHS, second) : undefined) ?? DEFAULT_LANDING.lane,
    panel: (view === "settings" ? keyFor(PANEL_PATHS, second) : undefined) ?? DEFAULT_LANDING.panel,
    policyMedia: (policy ? keyFor(MEDIA_PATHS, second) : undefined) ?? DEFAULT_LANDING.policyMedia,
    policySection:
      (policy ? keyFor(POLICY_PATHS, third) : undefined) ?? DEFAULT_LANDING.policySection,
  };
}

/** The review queue's URL: the lane in the path, its filters already built into `query`
 *  (`queueFilters.filtersToQuery`). */
export const reviewUrl = (lane: Verdict, query: string) =>
  `/${SECTION_PATHS.review}/${LANE_PATHS[lane]}${query}`;

/** Any other section's URL. Settings and Policy name what they have open, so a reload or a
 *  bookmark lands on it; Reap and Scales have no sub-navigation and are just their own name.
 *  Policy names two values, because half its location restores the wrong numbers.
 *
 *  `at` carries every one of them on every call, whichever section is being written, so `App` has
 *  one call site instead of a branch per section and this file stays the only reader of the three
 *  records. A section that grows a second value widens `at` rather than adding a call site. */
export const sectionUrl = (
  view: View,
  at: { panel: Panel; policyMedia: "movie" | "tv"; policySection: PolicySectionId },
): string => {
  if (view === "settings") return `/${SECTION_PATHS.settings}/${PANEL_PATHS[at.panel]}`;
  if (view === "policy") {
    return `/${SECTION_PATHS.policy}/${MEDIA_PATHS[at.policyMedia]}/${POLICY_PATHS[at.policySection]}`;
  }
  return `/${SECTION_PATHS[view]}`;
};

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

/** Put the last written URL back after a traversal has stepped off the entry carrying it.
 *
 *  Called from `backnav`'s `onPop` on EVERY pop, its own steps and the operator's alike. A pop
 *  that only closes a layer changes no state, so no effect ever runs to correct the address bar
 *  and this is the only thing that does. Restricting it to the app's own steps was tried and is
 *  wrong: a Back press that closes a panel is the operator's pop, and it reverted the URL
 *  exactly like the rest.
 *
 *  **A pop that genuinely MOVES the app writes the real URL over this one, and that comes from
 *  `App.goTo`, not from anything here.** `pushNav` has one call site, guarded on the view or the
 *  lane actually changing, so every frame's undo changes one of them and one of the two writers
 *  fires. Were a frame ever parked whose undo restores a value already current, React would bail
 *  out, no effect would run, and this re-assert would win against a real navigation. That is a
 *  property of the guard in `App.tsx`, so a second `pushNav` call site has to keep it.
 *
 *  Without this the ＋ Filter menu's own close discarded every filter it had just put in the
 *  address bar, and a section reached by clicking the nav reloaded onto whichever one the
 *  previous entry named. */
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
