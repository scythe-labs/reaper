// SPDX-License-Identifier: AGPL-3.0-or-later
import { useEffect, useState } from "react";

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
