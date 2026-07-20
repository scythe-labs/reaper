// SPDX-License-Identifier: AGPL-3.0-or-later
import { beforeEach, describe, expect, it, vi } from "vitest";
import {
  accentInk,
  ACCENT_STORAGE_KEY,
  applyAccent,
  DEFAULT_ACCENT,
  FAVICON_STORAGE_KEY,
  isHexColor,
} from "./accent";
import { deepIconDataUri } from "./brand/deepIcon";

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
    // The default sky blue, amber and white are all bright: white text would fail WCAG.
    expect(accentInk(DEFAULT_ACCENT)).toBe("#06202c");
    expect(accentInk("#f59e0b")).toBe("#06202c");
    expect(accentInk("#ffffff")).toBe("#06202c");
  });

  it("puts light ink on a dark accent", () => {
    expect(accentInk("#4f46e5")).toBe("#ffffff"); // indigo
    expect(accentInk("#000000")).toBe("#ffffff");
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
    // jsdom on an opaque origin exposes no localStorage; give the test a real in-memory one
    // so the pre-paint cache can be asserted (the code itself tolerates its absence).
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

  it("ignores a missing or malformed color, leaving the stylesheet default in place", () => {
    applyAccent(undefined);
    applyAccent("nope");
    applyAccent("#fff");
    expect(document.documentElement.style.getPropertyValue("--accent")).toBe("");
    expect(localStorage.getItem(ACCENT_STORAGE_KEY)).toBeNull();
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
    const expected = deepIconDataUri("#24b26b");
    expect(link.getAttribute("href")).toBe(expected);
    expect(expected.startsWith("data:image/svg+xml,")).toBe(true);
    expect(localStorage.getItem(FAVICON_STORAGE_KEY)).toBe(expected);
  });

  it("tints the platter to the accent while the dark shell stays fixed", () => {
    applyAccent("#24b26b");
    const svg = decodeURIComponent(deepIconDataUri("#24b26b"));
    expect(svg).toContain("#24b26b"); // the glow rides the accent
    expect(svg).toContain("#0f3040"); // the shell does not
  });

  it("does not touch the favicon for a malformed color", () => {
    applyAccent("nope");
    expect(document.getElementById("favicon")?.getAttribute("href")).toBeNull();
    expect(localStorage.getItem(FAVICON_STORAGE_KEY)).toBeNull();
  });
});
