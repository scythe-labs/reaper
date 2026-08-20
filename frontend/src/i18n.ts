// SPDX-License-Identifier: AGPL-3.0-or-later
//
// The one i18n init, run by the app (main.tsx) and by every test file (test/setup.ts), so
// what a test asserts on is byte-identical to what the app renders (docs/I18N_PLAN.md §4).
//
// Two decisions live here and both come from the plan:
//
//   * `en-US` is pinned. There is no language detector yet, so app and tests resolve the
//     same locale deterministically -- and when Stage 2 moves formatting onto Intl, dates
//     and numbers stay US-formatted in tests instead of following the machine they run on.
//   * Messages are ICU MessageFormat (i18next-icu), not i18next's own format, so the
//     catalog stays portable to any translation platform and any future library.
//
// `frontend/src/locales/en/ui.json` is the only hand-edited catalog. Every other locale
// file will be written by the translation platform and overwritten on sync (§3).

import i18next from "i18next";
import ICU from "i18next-icu";
import { initReactI18next } from "react-i18next";
import ui from "./locales/en/ui.json";

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
    // Resources are inline, so init synchronously: no first paint of raw keys, and no
    // pending timer for a test with fake timers to trip over.
    initAsync: false,
  });

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
