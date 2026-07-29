// SPDX-License-Identifier: AGPL-3.0-or-later
import { useEffect, useState } from "react";

/** The width at or below which this is a phone, for the JS that has to agree with the CSS.
 *
 *  900px is where index.css moves the section rail off the masthead into a nav bar pinned to
 *  the bottom of the screen (and lifts the three foot-anchored surfaces clear of it), and
 *  where `main.split .why` stops being a right-hand sheet and covers the screen. It is NOT
 *  where the queue becomes one column: `main.split` collapses at 1100px and `.card-list` is
 *  always a column, so the queue is already single-column for 200px above this. All FOUR JS
 *  readers of the boundary take it from here rather than spelling it again (rule 67): App's
 *  `fullSheet`, which decides whether the window scroll still tracks the list; the review
 *  queue's "expand seasons by default" mode, which opens a show's season list on one screen
 *  size and not the other; Settings, which swaps its nine-section rail for a single
 *  `.settings-picker` select; and `WhyShell`, which decides whether the `.why` panel claims to
 *  be a dialog, takes focus, and keeps Tab inside itself. That last one is the reason to count
 *  carefully: the other three change what the operator sees, and it changes whether a panel
 *  covering the whole application is reachable from a keyboard at all. Change this and change
 *  the `900px` blocks in index.css with it -- it is also the stored meaning of the operator's
 *  "Mobile" and "Desktop" choices. */
export const NARROW_SCREEN_QUERY = "(max-width: 900px)";

/** Read a CSS media query from JS and keep it in step as the viewport crosses the query.
 *
 *  Where `matchMedia` is missing -- jsdom under the tests, and any engine too old to have it
 *  -- this reports `false`, so a component gated on the query falls back to its non-matching
 *  branch instead of throwing. That is the safe default for all four callers, because for
 *  `NARROW_SCREEN_QUERY` a non-match is the wide screen: App's scroll-keeping treats the list
 *  as visible beside the panel rather than under a full-screen sheet, so it tracks rather than
 *  freezes, the queue's expand-seasons mode reads "desktop" rather than the phone, and Settings
 *  draws the rail, which puts every section on screen instead of behind a closed dropdown.
 *  `WhyShell` is the one to think about rather than assume: its non-match is the plain side
 *  panel, no dialog and no Tab trap, which is right for an engine with no `matchMedia` (it is
 *  not a phone) but is also exactly the pre-fix bug if this ever reports `false` on a narrow
 *  screen. A wrong `false` here is silent and costs a keyboard operator the whole panel. */
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
