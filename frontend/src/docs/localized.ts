// SPDX-License-Identifier: AGPL-3.0-or-later
//
// The manual in the operator's language, fetched only when the docs open and only for that
// language. To add a locale: write `content/<tag>/index.ts` exporting `DOCS: Doc[]`, the whole
// manual, with every doc, section id, table and diagram the English one has.
// `manual.locales.test.ts` holds it to that shape, and nothing imports it by name: the glob
// below finds it, Vite gives it its own chunk, and the modal reads it through `manualFor`.
//
// The fallback is the English manual entire, never a per-doc mix. Half a translated manual is
// worse than an English one (docs/history/I18N_PLAN.md, Stage 5). English stays a static import, so the
// default locale costs no second fetch and every existing test reads it synchronously.

import type { Doc } from "./blocks";
import { DOCS } from "./registry";

/** A manual and the tag it is written in, which is what the reading pane's `lang` says. */
export type Manual = { lng: string; docs: Doc[] };
type Loader = () => Promise<{ DOCS: Doc[] }>;

/** Every translated manual the build ships, by module path, each behind its own chunk. */
export const LOCALE_MODULES: Record<string, Loader> = import.meta.glob<{ DOCS: Doc[] }>(
  "./content/*/index.ts",
);

export const ENGLISH: Manual = { lng: "en", docs: DOCS };

/** The tags a locale can be served under: the exact tag, then its language alone, so `de-CH`
 *  reads `content/de/` when no Swiss manual ships. */
function candidates(lng: string): string[] {
  const language = lng.split("-")[0] ?? lng;
  return language === lng ? [lng] : [lng, language];
}

const modulePath = (tag: string) => `./content/${tag}/index.ts`;

/** A loader over one set of modules. The app's is `manualFor`; tests build their own over a
 *  fake set, each with its own cache. */
export function manualLoader(modules: Record<string, Loader>) {
  const cache = new Map<string, Promise<Manual>>();
  /** The manual for `lng`: English synchronously when no module serves the tag, else a promise
   *  for React's `use()`. One promise per tag, so `use()` sees the same thenable on every render
   *  and suspends once. A module that fails to load serves English, the same answer a missing
   *  one gives. */
  return (lng: string): Manual | Promise<Manual> => {
    const tag = candidates(lng).find((c) => modulePath(c) in modules);
    if (tag === undefined) return ENGLISH;
    let pending = cache.get(tag);
    if (pending === undefined) {
      const load = modules[modulePath(tag)] as Loader;
      pending = load()
        .then((m) => ({ lng: tag, docs: m.DOCS }))
        .catch(() => ENGLISH);
      cache.set(tag, pending);
    }
    return pending;
  };
}

export const manualFor = manualLoader(LOCALE_MODULES);
