// SPDX-License-Identifier: AGPL-3.0-or-later
//
// `dir` follows the language being served (#861), so a right-to-left catalog mirrors the
// layout the browser already knows how to mirror.
//
// The reading order comes from the browser's own locale data, never from a list kept in the
// repo (rule 66, the same call `languageName` makes two functions up in `i18n.ts`). So what is
// worth pinning is not "Arabic is rtl" -- that is the browser's fact, and asserting it here
// would only test the browser -- but the three things `i18n.ts` decides on top of it: that a
// right-to-left tag reaches the attribute, that a regional tag resolves the same as its bare
// language, and that anything unreadable falls to `ltr` rather than throwing on a screen the
// operator is trying to use.
import { describe, expect, it } from "vitest";

import i18next, { textDirection } from "./i18n";

describe("textDirection", () => {
  it("reads the right-to-left languages the issue names as right to left", () => {
    // Arabic, Hebrew and Persian: the three #861 was opened for.
    expect(textDirection("ar")).toBe("rtl");
    expect(textDirection("he")).toBe("rtl");
    expect(textDirection("fa")).toBe("rtl");
  });

  it("reads the languages Reaper ships today as left to right", () => {
    expect(textDirection("en")).toBe("ltr");
    expect(textDirection("en-US")).toBe("ltr");
    expect(textDirection("es")).toBe("ltr");
  });

  it("answers for a regional tag the way it answers for its language", () => {
    // Weblate serves regional tags (`pt-BR`), and a catalog shipping as `ar-EG` must not
    // quietly fall back to a left-to-right layout because the region was spelled out.
    expect(textDirection("ar-EG")).toBe("rtl");
    expect(textDirection("he-IL")).toBe("rtl");
  });

  it("falls to left-to-right on a tag it cannot read, rather than throwing", () => {
    // `new Intl.Locale` throws on a malformed tag. This runs while the app is deciding how to
    // paint, so the wrong answer is survivable and an exception is not.
    for (const bad of ["", "not a tag", "!!", "zzzz-zzzz-zzzz"]) {
      expect(textDirection(bad)).toBe("ltr");
    }
  });
});

describe("the document", () => {
  it("carries the language and its reading order on the html element", () => {
    // i18n.ts sets both at import time and again on every change. The suite inits at `en-US`.
    expect(document.documentElement.lang).toBe(i18next.resolvedLanguage ?? i18next.language);
    expect(document.documentElement.dir).toBe("ltr");
  });

  it("moves dir with the language, not just lang", () => {
    // The pair has to move together: `lang` alone would tell a screen reader the language
    // while leaving the layout unmirrored, which is the half-mirrored page #861 is about.
    // A bundle with something in it: i18next resolves an EMPTY one straight back to the
    // fallback, so an empty bundle here would test the fallback rather than the switch.
    i18next.addResourceBundle("ar", "ui", { "test.direction": "rtl" });
    return i18next.changeLanguage("ar").then(async () => {
      expect(document.documentElement.lang).toBe("ar");
      expect(document.documentElement.dir).toBe("rtl");
      await i18next.changeLanguage("en-US");
      expect(document.documentElement.dir).toBe("ltr");
    });
  });
});
