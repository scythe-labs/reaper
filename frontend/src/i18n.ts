// SPDX-License-Identifier: AGPL-3.0-or-later
//
// The one i18n init, run by the app (main.tsx) and by every test file (test/setup.ts), so
// what a test asserts on is byte-identical to what the app renders (docs/I18N_PLAN.md §4).
//
// Three decisions live here and all come from the plan:
//
//   * Init pins `en-US`, for the app and for every test, so dates and numbers in a test stay
//     US-formatted instead of following the machine they run on. The app then moves to the
//     browser's language in `applyBrowserLanguage`, before its first paint, and only to a
//     language whose catalog shipped: `format.ts` formats through whatever tag i18next serves,
//     so a German browser with no German catalog keeps English numbers under English strings.
//   * Messages are ICU MessageFormat (i18next-icu), not i18next's own format, so the
//     catalog stays portable to any translation platform and any future library.
//   * An empty message serves the English one. A translator can leave a string blank, and a
//     blank label is worse than an English label.
//
// `frontend/src/locales/en/ui.json` is the only hand-edited catalog. Every other
// `locales/<tag>/ui.json` is written by Weblate and overwritten on its next sync (§3 and
// CONTRIBUTING). Nothing imports one by name: the glob below finds each, Vite gives it its own
// chunk, and `i18n-locales.test.ts` holds every one to the English catalog's keys, arguments
// and tags.

import i18next from "i18next";
import ICU from "i18next-icu";
import { initReactI18next } from "react-i18next";
import ui from "./locales/en/ui.json";

type CatalogModule = { default: Record<string, unknown> };
type Loader = () => Promise<CatalogModule>;

/** Every translated UI catalog the build ships, by module path, each behind its own chunk. */
const LOCALE_MODULES: Record<string, Loader> = import.meta.glob<CatalogModule>([
  "./locales/*/ui.json",
  "!./locales/en/ui.json",
]);

const modulePath = (tag: string) => `./locales/${tag}/ui.json`;

/** The tags with a shipped catalog: the directory names under `locales/`, English aside. */
export const SHIPPED_TAGS: ReadonlySet<string> = new Set(
  Object.keys(LOCALE_MODULES).map((path) => path.split("/")[2] ?? path),
);

void i18next
  .use(ICU)
  .use(initReactI18next)
  .init({
    lng: "en-US",
    fallbackLng: "en",
    defaultNS: "ui",
    ns: ["ui"],
    resources: { en: { ui } },
    // React escapes rendered strings itself; i18next escaping on top would show entities.
    interpolation: { escapeValue: false },
    // A "" in a translated catalog is an untranslated message; serve the English one.
    returnEmptyString: false,
    // Resources are inline, so init synchronously: no first paint of raw keys, and no
    // pending timer for a test with fake timers to trip over.
    initAsync: false,
  });

/** The shipped tag to serve a browser preferring `preferred`, in the browser's order: the
 *  exact tag (`pt-BR`), then its language alone (`pt`). An English entry met first ends the
 *  walk, since a browser ranking English above German gets English, which ships with the
 *  app. `undefined` keeps the init's `en-US`. */
export function shippedTag(
  preferred: readonly string[],
  shipped: ReadonlySet<string> = SHIPPED_TAGS,
): string | undefined {
  for (const tag of preferred) {
    const language = tag.split("-")[0] ?? tag;
    if (language === "en") return undefined;
    if (shipped.has(tag)) return tag;
    if (shipped.has(language)) return language;
  }
  return undefined;
}

/** Move the app onto the browser's language once its catalog has loaded, or stay English.
 *  Run before the first render, so no screen paints in one language and repaints in another.
 *  A catalog that fails to load serves English, the same answer a missing one gives. */
export async function applyBrowserLanguage(): Promise<void> {
  const tag = shippedTag(navigator.languages);
  if (tag === undefined) return;
  try {
    const { default: catalog } = await (LOCALE_MODULES[modulePath(tag)] as Loader)();
    i18next.addResourceBundle(tag, "ui", catalog);
    await i18next.changeLanguage(tag);
  } catch {
    // The init's English is still in place.
  }
}

// index.html ships `lang="en"` as the pre-JS default; from here on the attribute follows
// the locale actually serving strings. Guarded: test/setup.ts imports this file for the
// node-environment test files too, where there is no document at all.
if (typeof document !== "undefined") {
  const setLang = () => {
    document.documentElement.lang = i18next.resolvedLanguage ?? i18next.language;
  };
  setLang();
  i18next.on("languageChanged", setLang);
}

export default i18next;
