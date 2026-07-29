// SPDX-License-Identifier: AGPL-3.0-or-later
import { useEffect, useState } from "react";

/** The width at or below which this is a phone, for the JS that has to agree with the CSS.
 *
 *  900px is where index.css moves the section rail off the masthead into a nav bar pinned to
 *  the bottom of the screen (and lifts the three foot-anchored surfaces clear of it), and
 *  where `main.split .why` stops being a right-hand sheet and covers the screen. It is NOT
 *  where the queue becomes one column: `main.split` collapses at 1100px and `.card-list` is
 *  always a column, so the queue is already single-column for 200px above this. All THREE JS
 *  readers of the boundary take it from here rather than spelling it again (rule 67): App's
 *  `fullSheet`, which decides whether the window scroll still tracks the list; the review
 *  queue's "expand seasons by default" mode, which opens a show's season list on one screen
 *  size and not the other; and Settings, which swaps its nine-section rail for a single
 *  `.settings-picker` select. Change this and change the `900px` blocks in index.css with it
 *  -- it is also the stored meaning of the operator's "Mobile" and "Desktop" choices, which is
 *  why the panel's dialog contract moved OFF it to `PANEL_OVERLAY_QUERY` below rather than
 *  dragging this number up (#184). */
export const NARROW_SCREEN_QUERY = "(max-width: 900px)";

/** The width at or below which the `.why` panel stops being a side column and starts covering
 *  the list, for the JS that has to agree with the CSS.
 *
 *  1100px is where index.css collapses `main.split` to one full-width track and makes
 *  `main.split .why` `position: fixed; right: 0; z-index: 40` -- the panel rides OVER the cards
 *  from there down, first as a right-hand sheet and, once `NARROW_SCREEN_QUERY` also matches, as
 *  a full-screen one. `WhyShell` is the only reader: it decides whether the panel claims to be a
 *  dialog, takes focus, and keeps Tab inside itself, and that has to key on where the covering
 *  starts, not where it becomes total. Keyed on the 900 instead, 200px of viewport width left
 *  the panel floating over the right of the cards with no `role="dialog"`, no focus move and no
 *  Tab trap, so a keyboard operator tabbed into cards hidden underneath it and could press Spare
 *  or Reap on an item they could not see (#184). Change this and change the `1100px` block in
 *  index.css with it (rule 67). */
export const PANEL_OVERLAY_QUERY = "(max-width: 1100px)";

/** Read a CSS media query from JS and keep it in step as the viewport crosses the query.
 *
 *  Where `matchMedia` is missing -- jsdom under the tests, and any engine too old to have it
 *  -- this reports `false`, so a component gated on the query falls back to its non-matching
 *  branch instead of throwing. That is the safe default for all four callers, because a
 *  non-match is the wide screen: App's scroll-keeping treats the list as visible beside the
 *  panel rather than under a full-screen sheet, so it tracks rather than freezes, the queue's
 *  expand-seasons mode reads "desktop" rather than the phone, and Settings draws the rail, which
 *  puts every section on screen instead of behind a closed dropdown. `WhyShell`'s
 *  `PANEL_OVERLAY_QUERY` is the one to think about rather than assume: its non-match is the
 *  plain side panel, no dialog and no Tab trap, which is right for an engine with no
 *  `matchMedia` (it is a wide desktop) but is also exactly the pre-fix bug if this ever reports
 *  `false` under 1100px. A wrong `false` here is silent and costs a keyboard operator the whole
 *  panel. */
export function useMediaQuery(query: string): boolean {
  const [matches, setMatches] = useState(() => {
    if (typeof window === "undefined" || typeof window.matchMedia !== "function") return false;
    return window.matchMedia(query).matches;
  });

  useEffect(() => {
    if (typeof window === "undefined" || typeof window.matchMedia !== "function") return;
    const mql = window.matchMedia(query);
    const onChange = () => setMatches(mql.matches);
    onChange(); // sync in case the query resolved differently between the render and this effect
    mql.addEventListener("change", onChange);
    return () => mql.removeEventListener("change", onChange);
  }, [query]);

  return matches;
}
