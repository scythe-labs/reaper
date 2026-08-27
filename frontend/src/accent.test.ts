// SPDX-License-Identifier: AGPL-3.0-or-later
import { readFileSync } from "node:fs";
import { DISSOLVE_BONE, DISSOLVE_INK } from "./brand/dissolve";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

import { beforeEach, describe, expect, it, vi } from "vitest";
import {
  accentInk,
  ACCENT_INK_STORAGE_KEY,
  ACCENT_STORAGE_KEY,
  ACCENT_TEXT_STORAGE_KEY,
  accentText,
  applyAccent,
  DEFAULT_ACCENT,
  FAVICON_STORAGE_KEY,
  isHexColor,
} from "./accent";
import { appIconDataUri } from "./brand/appIcon";

/** Computes WCAG contrast directly, instead of importing it from accent.ts. Reusing the
 *  module's own math would let these tests pass even if that math were wrong. */
function ratio(a: string, b: string): number {
  const lum = (hex: string) => {
    const ch = (i: number) => {
      const v = parseInt(hex.slice(i, i + 2), 16) / 255;
      return v <= 0.03928 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4);
    };
    return 0.2126 * ch(1) + 0.7152 * ch(3) + 0.0722 * ch(5);
  };
  const la = lum(a);
  const lb = lum(b);
  return (Math.max(la, lb) + 0.05) / (Math.min(la, lb) + 0.05);
}

function stubLocalStorage(): void {
  const store = new Map<string, string>();
  vi.stubGlobal("localStorage", {
    getItem: (k: string) => (store.has(k) ? store.get(k)! : null),
    setItem: (k: string, v: string) => void store.set(k, v),
    removeItem: (k: string) => void store.delete(k),
    clear: () => store.clear(),
  });
}

describe("accentInk", () => {
  it("puts dark ink on a bright accent, so button text stays readable", () => {
    // The default sky blue, amber, and white are all bright colors, so white text on them would fail WCAG.
    expect(accentInk(DEFAULT_ACCENT)).toBe("#06202c");
    expect(accentInk("#f59e0b")).toBe("#06202c");
    expect(accentInk("#ffffff")).toBe("#06202c");
  });

  it("puts light ink on a dark accent", () => {
    expect(accentInk("#4f46e5")).toBe("#ffffff"); // indigo
    expect(accentInk("#000000")).toBe("#ffffff");
  });
});

describe("accentText", () => {
  it("agrees with the stylesheet's fallback for the built-in accent", () => {
    // styles/00-tokens.css falls back to color-mix(in srgb, var(--accent), #000 42%) when no measured
    // ink is set. If this search and that fallback ever disagreed, a page would change color
    // the moment the module loaded. That color mix works out to #157194.
    expect(accentText(DEFAULT_ACCENT, "light")).toBe("#157194");
  });

  it("leaves an accent that already reads alone", () => {
    // The sky blue clears 8.47:1 on the dark surface, so darkening or lightening it would be
    // a change for its own sake.
    expect(accentText(DEFAULT_ACCENT, "dark")).toBe(DEFAULT_ACCENT);
  });

  it("pushes a pale accent past what a flat darken would manage", () => {
    // A flat 42% darken of this yellow lands at 3.56:1 on white, a case the stylesheet
    // fallback cannot cover. That is why the search exists.
    const ink = accentText("#ffee00", "light");
    expect(ratio(ink, "#ffffff")).toBeGreaterThanOrEqual(4.5);
    expect(ratio("#948a00", "#ffffff")).toBeLessThan(4.5);
  });

  it("lightens a dark accent for the dark theme instead of darkening it", () => {
    const ink = accentText("#003366", "dark");
    expect(ratio(ink, "#191b21")).toBeGreaterThanOrEqual(4.5);
    expect(ratio("#003366", "#191b21")).toBeLessThan(4.5);
  });

  it("clears AA on the tightest ground -- the accent's own tint, not the plain surface", () => {
    // Every accent-colored chip sits on a tint of its own accent. The docs index's selected
    // row is the strongest, at 22%. Clearing that also clears --surface and --bg behind it.
    for (const accent of [DEFAULT_ACCENT, "#ffee00", "#4f46e5", "#24b26b", "#c62630"]) {
      for (const theme of ["light", "dark"] as const) {
        const surface = theme === "light" ? "#ffffff" : "#191b21";
        const tint = (i: number) => {
          const a = parseInt(accent.slice(i, i + 2), 16);
          const s = parseInt(surface.slice(i, i + 2), 16);
          return Math.round(a * 0.22 + s * 0.78)
            .toString(16)
            .padStart(2, "0");
        };
        const ground = `#${tint(1)}${tint(3)}${tint(5)}`;
        expect(ratio(accentText(accent, theme), ground)).toBeGreaterThanOrEqual(4.5);
      }
    }
  });
});

describe("isHexColor", () => {
  it("accepts #rrggbb only", () => {
    expect(isHexColor("#25c3ff")).toBe(true);
    expect(isHexColor("#ABC123")).toBe(true);
    expect(isHexColor("25c3ff")).toBe(false); // no hash
    expect(isHexColor("#fff")).toBe(false); // shorthand not allowed
    expect(isHexColor("#gggggg")).toBe(false);
    expect(isHexColor("")).toBe(false);
  });
});

describe("applyAccent", () => {
  beforeEach(() => {
    document.documentElement.removeAttribute("style");
    // jsdom exposes no localStorage on an opaque origin. This test stubs in a real in-memory
    // one so the pre-paint cache can be checked. The code itself tolerates a missing cache.
    stubLocalStorage();
  });

  it("sets --accent and a contrast-safe --accent-ink, and caches the color for pre-paint", () => {
    applyAccent(DEFAULT_ACCENT);
    const s = document.documentElement.style;
    expect(s.getPropertyValue("--accent")).toBe("#25c3ff");
    expect(s.getPropertyValue("--accent-ink")).toBe("#06202c");
    expect(localStorage.getItem(ACCENT_STORAGE_KEY)).toBe("#25c3ff");
  });

  it("lower-cases the color and flips the ink for a dark one", () => {
    applyAccent("#4F46E5");
    const s = document.documentElement.style;
    expect(s.getPropertyValue("--accent")).toBe("#4f46e5");
    expect(s.getPropertyValue("--accent-ink")).toBe("#ffffff");
  });

  it("sets BOTH themes' accent-text inks, and caches the pair for pre-paint", () => {
    // Both themes, not just the current one: the "Match my device" setting can flip
    // mid-session, and the stylesheet then picks by media query without asking this module
    // again.
    applyAccent("#ffee00");
    const s = document.documentElement.style;
    const light = s.getPropertyValue("--accent-text-light");
    const dark = s.getPropertyValue("--accent-text-dark");
    expect(light).toBe(accentText("#ffee00", "light"));
    expect(dark).toBe(accentText("#ffee00", "dark"));
    expect(light).not.toBe(dark);
    expect(localStorage.getItem(ACCENT_TEXT_STORAGE_KEY)).toBe(`${light} ${dark}`);
  });

  it("ignores a missing or malformed color, leaving the stylesheet default in place", () => {
    applyAccent(undefined);
    applyAccent("nope");
    applyAccent("#fff");
    expect(document.documentElement.style.getPropertyValue("--accent")).toBe("");
    expect(localStorage.getItem(ACCENT_STORAGE_KEY)).toBeNull();
  });

  it("paints the browser's own chrome at the accent", () => {
    const meta = document.createElement("meta");
    meta.id = "theme-color";
    meta.name = "theme-color";
    meta.content = DEFAULT_ACCENT;
    document.head.appendChild(meta);
    applyAccent("#4f46e5");
    expect(meta.content).toBe("#4f46e5");
    meta.remove();
  });

  it("caches the ink that rides on the accent, so the pre-paint never recomputes it", () => {
    // #009050 sits almost exactly on the boundary between the two ink colors, at a relative
    // luminance of 0.2055, so a second, slightly different formula could round the wrong way
    // here. The pre-paint script reads this cached value instead of recomputing it, so it
    // always agrees with whatever accent.ts computed.
    applyAccent("#009050");
    expect(accentInk("#009050")).toBe("#ffffff");
    expect(localStorage.getItem(ACCENT_INK_STORAGE_KEY)).toBe("#ffffff");
    expect(document.documentElement.style.getPropertyValue("--accent-ink")).toBe("#ffffff");
  });
});

describe("the pre-paint script in index.html", () => {
  // index.html runs before the JavaScript bundle loads, so it cannot import accent.ts. Any
  // value the two files must agree on is computed once, here in accent.ts, and read back from
  // storage in index.html rather than recomputed there. These assertions check that
  // index.html does exactly that.
  const html = readFileSync(
    join(dirname(fileURLToPath(import.meta.url)), "..", "index.html"),
    "utf8",
  );

  it("reads every accent value back from the cache accent.ts writes", () => {
    for (const key of [
      ACCENT_STORAGE_KEY,
      ACCENT_INK_STORAGE_KEY,
      ACCENT_TEXT_STORAGE_KEY,
      FAVICON_STORAGE_KEY,
    ]) {
      expect(html).toContain(`localStorage.getItem("${key}")`);
    }
  });

  it("ships the theme-color tag at the built-in accent and rewrites it from the cache", () => {
    // The static content= here is a second copy of DEFAULT_ACCENT, since index.html cannot
    // import it from accent.ts. This assertion is the link between them, so a retune of the
    // built-in accent in accent.ts fails here instead of shipping a browser tab color stuck
    // on the old value.
    expect(html).toContain(`<meta id="theme-color" name="theme-color" content="${DEFAULT_ACCENT}"`);
    expect(html).toContain('document.getElementById("theme-color")');
  });

  it("repeats none of the color maths", () => {
    // If any of the luminance coefficients or the sRGB transfer-curve constants appear here,
    // some derivation was copied into the file instead of being read back out of storage.
    for (const constant of ["0.2126", "0.7152", "0.0722", "0.03928", "12.92", "1.055"]) {
      expect(html).not.toContain(constant);
    }
    // ...and neither ink is spelled out as a literal to choose between.
    expect(html).not.toContain("#06202c");
  });
});

describe("applyAccent favicon", () => {
  beforeEach(() => {
    stubLocalStorage();
    document.getElementById("favicon")?.remove();
    const link = document.createElement("link");
    link.id = "favicon";
    link.rel = "icon";
    document.head.appendChild(link);
  });

  it("redraws the tab favicon at the accent and caches it for pre-paint", () => {
    applyAccent("#24b26b");
    const link = document.getElementById("favicon") as HTMLLinkElement;
    const expected = appIconDataUri("#24b26b");
    expect(link.getAttribute("href")).toBe(expected);
    expect(expected.startsWith("data:image/svg+xml,")).toBe(true);
    expect(localStorage.getItem(FAVICON_STORAGE_KEY)).toBe(expected);
  });

  it("tints the eyes to the accent while the shell and figure stay fixed", () => {
    applyAccent("#24b26b");
    const svg = decodeURIComponent(appIconDataUri("#24b26b"));
    expect(svg.match(/#24b26b/g)).toHaveLength(2); // both eyes ride the accent
    expect(svg).toContain(DISSOLVE_INK); // the shell does not
    expect(svg).toContain(DISSOLVE_BONE); // nor the figure
  });

  it("does not touch the favicon for a malformed color", () => {
    applyAccent("nope");
    expect(document.getElementById("favicon")?.getAttribute("href")).toBeNull();
    expect(localStorage.getItem(FAVICON_STORAGE_KEY)).toBeNull();
  });
});
