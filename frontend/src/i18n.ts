// SPDX-License-Identifier: AGPL-3.0-or-later
//
// The one i18n init, run by the app (main.tsx) and by every test file (test/setup.ts), so
// what a test asserts on is byte-identical to what the app renders (docs/history/I18N_PLAN.md §4).
//
// Three decisions live here and all come from the plan:
//
//   * Init pins `en-US`, for the app and for every test, so dates and numbers in a test stay
//     US-formatted instead of following the machine they run on. The app then moves to the
//     operator's chosen language, or the browser's when they have not chosen one, in
//     `applyStoredLanguage`, before its first paint, and only to a language whose catalog
//     shipped: `format.ts` formats through whatever tag i18next serves, so a German browser
//     with no German catalog keeps English numbers under English strings.
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

/** Where Settings -> General keeps the operator's own choice of language. localStorage, beside
 *  the theme and for the same reason: it is a preference for the screen in front of them, not a
 *  server setting every other browser signed into Reaper would inherit. Absent means no choice
 *  has been made and the browser decides. */
const LANGUAGE_KEY = "reaper-language";

/** This browser's stored choice, or `undefined` when none was made. */
export function storedLanguage(): string | undefined {
  try {
    return localStorage.getItem(LANGUAGE_KEY) ?? undefined;
  } catch {
    // Storage can be unavailable (private windows); the browser's own language decides.
    return undefined;
  }
}

/** Every language Reaper can be served in, for the Settings picker: English, then each shipped
 *  catalog's tag. Read from the same glob the loader uses, so a translation becomes choosable
 *  the moment its catalog ships and stops being offered if one is ever dropped (rule 66). */
export const LANGUAGES: readonly string[] = ["en", ...[...SHIPPED_TAGS].sort()];

/** A BCP 47 tag's name in its own language ("de" -> "Deutsch"), which is what an operator
 *  scanning a language list looks for. `inLanguage` writes the name in that language instead.
 *  From the browser's own list rather than a hand-kept map that would need an entry added every
 *  time a translation ships (rule 66). */
export function languageName(tag: string, inLanguage: string = tag): string {
  try {
    const name = new Intl.DisplayNames([inLanguage], { type: "language" }).of(tag) ?? tag;
    // Spanish writes "espanol" lowercase in running text; as a menu entry beside "English" it
    // reads as a stray. Raising the first letter is a no-op in a script that has no case.
    return name.charAt(0).toLocaleUpperCase(inLanguage) + name.slice(1);
  } catch {
    return tag;
  }
}

/** Serve `tag`, loading its catalog first. A tag with no shipped catalog is English, which the
 *  init above already holds, so every "en" variant lands here too. */
async function serve(tag: string): Promise<void> {
  if (!SHIPPED_TAGS.has(tag)) {
    await i18next.changeLanguage("en-US");
    return;
  }
  const { default: catalog } = await (LOCALE_MODULES[modulePath(tag)] as Loader)();
  i18next.addResourceBundle(tag, "ui", catalog);
  await i18next.changeLanguage(tag);
}

/** What this browser asks for, of the catalogs that shipped: the operator's stored choice, else
 *  the best match for `navigator.languages`, else English.
 *
 *  Named because it is read twice now. `applyStoredLanguage` paints with it, and `App` sends it
 *  to the server the first time it finds no language stored there, which is what makes the
 *  browser's own preference the seed for a fresh install rather than a standing mode.
 *
 *  It ends in `"en"`, a CATALOG tag, and never `"en-US"`, the tag the init above pins. The two
 *  are the same language and not the same value: `LANGUAGES` is what the Settings picker offers,
 *  so seeding the server `"en-US"` gave that `<select>` a value none of its options carry and it
 *  rendered blank. Painting is unaffected -- `serve` sends every tag with no shipped catalog,
 *  `"en"` included, to `changeLanguage("en-US")`, which is where the US number and date formats
 *  come from. */
export function preferredLanguage(): string {
  return storedLanguage() ?? shippedTag(navigator.languages) ?? "en";
}

/** Move the app onto the operator's chosen language, the browser's when they have not chosen,
 *  or English when neither ships a catalog. Run before the first render, so no screen paints in
 *  one language and repaints in another. A catalog that fails to load serves English, the same
 *  answer a missing one gives.
 *
 *  It reads localStorage rather than the server on purpose: the login screen paints before any
 *  authenticated call can be made (`AuthGuard` opens only `/api/health` and `/api/auth/`), so
 *  the stored copy is what keeps sign-in in the operator's language. The server holds the
 *  durable value, and `App` reconciles the two once signed in. */
export async function applyStoredLanguage(): Promise<void> {
  try {
    await serve(preferredLanguage());
  } catch {
    // The init's English is still in place.
  }
}

/** Remember `tag` for this browser and reload onto it.
 *
 *  This is the paint half only. The durable copy lives on the server, because a notification is
 *  composed there with no browser to ask (`app_settings.LANGUAGE_KEY`), so the caller writes
 *  that first and calls this once it lands. Keeping the API out of here is what lets every test
 *  that switches language run without a server.
 *
 *  It reloads rather than switching in place because not every surface subscribes to a language
 *  change. `useTranslation` re-renders its component; a module reading the catalog through the
 *  plain `i18next` import does not, so a screen holding still would keep the old words while the
 *  ones around it changed. A reload paints the whole app once, in one language.
 *
 *  What it no longer covers for is a frozen table. Every string in the tree resolves in a
 *  function now, and `i18n-module-scope.test.ts` keeps it that way (#897). */
export async function setLanguage(tag: string): Promise<void> {
  try {
    localStorage.setItem(LANGUAGE_KEY, tag);
  } catch {
    // Storage can be unavailable (private windows); nothing to reload onto, so switch in place
    // and accept the frozen tables for this page.
    await serve(tag);
    return;
  }
  location.reload();
}

/** Which way `tag` is read, from the browser's own locale data rather than a list kept here
 *  that would need an entry the day Arabic or Hebrew ships (rule 66).
 *
 *  Anything unrecognized reads left to right. That is the safe answer both ways: it is right
 *  for every language Reaper ships today, and a tag the browser cannot place is far more
 *  likely to be a typo than to be Persian. */
export function textDirection(tag: string): "ltr" | "rtl" {
  try {
    // Two spellings of one thing: `getTextInfo()` is the current one, `textInfo` the older
    // getter still shipping in some browsers, and TypeScript's lib carries neither yet. Typed
    // here as what is actually read, so a browser with neither falls through to the catch.
    const locale = new Intl.Locale(tag) as Intl.Locale & {
      getTextInfo?: () => { direction?: string };
      textInfo?: { direction?: string };
    };
    const info = locale.getTextInfo?.() ?? locale.textInfo;
    return info?.direction === "rtl" ? "rtl" : "ltr";
  } catch {
    return "ltr";
  }
}

// index.html ships `lang="en"` as the pre-JS default; from here on the attribute follows
// the locale actually serving strings, and `dir` follows the language's own reading order so
// the layout mirrors with it (#861). Guarded: test/setup.ts imports this file for the
// node-environment test files too, where there is no document at all.
if (typeof document !== "undefined") {
  const setLang = () => {
    const tag = i18next.resolvedLanguage ?? i18next.language;
    document.documentElement.lang = tag;
    document.documentElement.dir = textDirection(tag);
  };
  setLang();
  i18next.on("languageChanged", setLang);
}

export default i18next;
