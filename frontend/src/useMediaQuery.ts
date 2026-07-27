// SPDX-License-Identifier: AGPL-3.0-or-later
import { useEffect, useState } from "react";

/** The width at or below which this is a phone, for the JS that has to agree with the CSS.
 *
 *  900px is where index.css already turns the review queue into one column, brings up the
 *  bottom nav bar, and makes `main.split .why` a full-screen sheet. Both JS readers of that
 *  boundary take it from here rather than spelling it again (rule 67): App's `fullSheet`,
 *  which decides whether the window scroll still tracks the list, and the review queue's
 *  "expand seasons by default" mode, which opens a show's season list on one screen size and
 *  not the other. Change this and change the `900px` blocks in index.css with it. */
export const NARROW_SCREEN_QUERY = "(max-width: 900px)";

/** Read a CSS media query from JS and keep it in step as the viewport crosses the query.
 *
 *  Where `matchMedia` is missing -- jsdom under the tests, and any engine too old to have it
 *  -- this reports `false`, so a component gated on the query falls back to its non-matching
 *  branch instead of throwing. That is the safe default for our caller: the scroll-keeping in
 *  App treats a wide screen (list visible beside the panel) as the fallback, never the phone's
 *  full-screen sheet, so it tracks rather than freezes. */
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
