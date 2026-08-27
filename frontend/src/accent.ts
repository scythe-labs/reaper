// SPDX-License-Identifier: AGPL-3.0-or-later
//
// The one operator-chosen color, applied at runtime. Settings → General stores a #rrggbb
// accent on the server; the whole UI derives from --accent (see styles/00-tokens.css), so applying it
// is just setting a few custom properties on <html>:
//   --accent             the color itself
//   --accent-ink         the text that rides on a solid accent fill -- dark or light by
//                        luminance, so a bright accent never ends up with unreadable pale
//                        text on it.
//   --accent-text-light  the reverse question, once per theme: accent-COLORED text, read
//   --accent-text-dark   against a light or a dark ground. The stylesheet has a fallback
//                        tuned for the built-in accent; these carry the measured answer for
//                        a custom one. Both are set, not just the current theme's, because
//                        "Match my device" can flip mid-session and the CSS picks by media
//                        query without asking this module again.
// The hover / soft / ring shades are pure color-mix() of --accent in the stylesheet, so they
// follow along on their own. The tab favicon and the theme-color meta follow too, so a
// browser that reads that tag carries the install's color outside the page as well.
//
// index.html applies these same properties before first paint, from a localStorage cache, so
// a custom accent does not flash the sky-blue default on load. This module writes that cache
// and is the runtime applier once the saved value arrives.

import { appIconDataUri } from "./brand/appIcon";

const HEX = /^#[0-9a-fA-F]{6}$/;
const DARK_INK = "#06202c";
const LIGHT_INK = "#ffffff";
export const ACCENT_STORAGE_KEY = "reaper-accent";
/** Cache of the last favicon data URI, so index.html can pre-paint the tab icon at the saved
 *  accent before this module runs -- the same trick the accent color itself uses. */
export const FAVICON_STORAGE_KEY = "reaper-favicon";
/** Cache of the two accent-text inks, "#light #dark", for the same reason: index.html reads
 *  it back rather than repeating the search below, so the maths lives in one place. */
export const ACCENT_TEXT_STORAGE_KEY = "reaper-accent-text";
/** Cache of the ink that rides ON the accent, for the same reason again. index.html used to
 *  re-implement `accentInk` with the dark ink's luminance rounded to 0.012 where the real
 *  value is 0.0125359, so the two disagreed for a band of accents around L = 0.2055: the
 *  pre-paint set dark ink, this module then set light, and every accent-filled button's text
 *  flipped color at first paint -- the exact flash the pre-paint exists to prevent (B-34). */
export const ACCENT_INK_STORAGE_KEY = "reaper-accent-ink";
/** The built-in accent, mirrored from the backend default. The color Reset returns to. */
export const DEFAULT_ACCENT = "#25c3ff";

/** Whether a string is a valid #rrggbb color. */
export function isHexColor(value: string): boolean {
  return HEX.test(value.trim());
}

/** Relative luminance (WCAG) of a #rrggbb color, 0 (black) to 1 (white). */
function luminance(hex: string): number {
  const channel = (i: number) => {
    const v = parseInt(hex.slice(i, i + 2), 16) / 255;
    return v <= 0.03928 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4);
  };
  return 0.2126 * channel(1) + 0.7152 * channel(3) + 0.0722 * channel(5);
}

/** The readable ink for text on a solid fill of `hex`: whichever of dark or light has the
 *  higher contrast, so every accent clears WCAG AA on its own button. */
export function accentInk(hex: string): string {
  const l = luminance(hex);
  const contrastLight = 1.05 / (l + 0.05); // white text against the fill
  const contrastDark = (l + 0.05) / (luminance(DARK_INK) + 0.05);
  return contrastLight >= contrastDark ? LIGHT_INK : DARK_INK;
}

/** WCAG contrast ratio between two #rrggbb colors. */
function contrast(a: string, b: string): number {
  const la = luminance(a);
  const lb = luminance(b);
  return (Math.max(la, lb) + 0.05) / (Math.min(la, lb) + 0.05);
}

/** `hex` blended toward `toward` by `weight` (0-1) -- the same sRGB mix CSS color-mix does. */
function blend(hex: string, toward: string, weight: number): string {
  const channel = (i: number) => {
    const from = parseInt(hex.slice(i, i + 2), 16);
    const to = parseInt(toward.slice(i, i + 2), 16);
    return Math.round(from * (1 - weight) + to * weight)
      .toString(16)
      .padStart(2, "0");
  };
  return `#${channel(1)}${channel(3)}${channel(5)}`;
}

/** The AA floor for body text. Accent-colored text is small (chips, tags, log levels), so
 *  the 3:1 large-text allowance never applies to it. */
const AA_TEXT = 4.5;
/** The tightest ground accent text lands on is not the plain surface but the accent's own
 *  22% tint -- the docs index's selected row, and --accent-soft in dark mode. Clearing that
 *  clears every lighter ground behind it.
 *
 *  These three mirror declarations in styles/00-tokens.css, and the AA guarantee holds only while they
 *  agree (rule 67). Their twins carry a comment back to here, so a retune of either side is
 *  caught by the other:
 *    TINT          -- the dark `--accent-soft` (both dark blocks) and `.docs-index-item
 *                     .active:hover`, all `color-mix(... var(--accent) 22%, var(--surface))`
 *    LIGHT_SURFACE -- `--surface` in `:root` and `:root[data-theme="light"]`
 *    DARK_SURFACE  -- `--surface` in the dark media query and `:root[data-theme="dark"]`
 *  Nothing derives them at runtime on purpose: index.html pre-paints these inks before the
 *  stylesheet is applied, so the search cannot depend on computed CSS. */
const TINT = 0.22;
const LIGHT_SURFACE = "#ffffff";
const DARK_SURFACE = "#191b21";

/** Accent-COLORED text that clears WCAG AA on one theme's grounds.
 *
 *  `accentInk` above answers "what rides ON the accent"; this answers the reverse, which was
 *  never checked and which the built-in sky blue fails badly in light mode (2.03:1 on white).
 *  The accent is pushed toward the theme's extreme -- black on a light ground, white on a
 *  dark one -- until it clears 4.5:1 against the tinted ground above. Contrast rises
 *  monotonically with each step, so the first hit is the least-changed ink that reads, and an
 *  accent that already clears comes back untouched (which is what the built-in one does in
 *  dark mode).
 *
 *  The stylesheet's own fallback is a flat 42% darken, which is exactly what this returns for
 *  the built-in accent in light mode; the search exists for the accents a flat darken does
 *  not save, such as a pale yellow, which lands at 3.56:1 after 42%. */
export function accentText(hex: string, theme: "light" | "dark"): string {
  const surface = theme === "light" ? LIGHT_SURFACE : DARK_SURFACE;
  const ground = blend(hex, surface, 1 - TINT);
  const toward = theme === "light" ? "#000000" : "#ffffff";
  for (let step = 0; step <= 100; step += 2) {
    const ink = blend(hex, toward, step / 100);
    if (contrast(ink, ground) >= AA_TEXT) return ink;
  }
  // Unreachable in practice: pure black on the lightest tint, or pure white on the darkest,
  // always clears. Returning the extreme keeps the contract "never less readable".
  return toward;
}

/** Apply the accent to the document, and cache it so index.html can pre-paint it next load.
 *  A missing or malformed value is ignored: the stylesheet default stands, never a broken
 *  color. */
export function applyAccent(color: string | null | undefined): void {
  if (!color || !HEX.test(color)) return;
  const hex = color.toLowerCase();
  const root = document.documentElement;
  const light = accentText(hex, "light");
  const dark = accentText(hex, "dark");
  const ink = accentInk(hex);
  root.style.setProperty("--accent", hex);
  root.style.setProperty("--accent-ink", ink);
  root.style.setProperty("--accent-text-light", light);
  root.style.setProperty("--accent-text-dark", dark);
  applyFavicon(hex);
  applyThemeColor(hex);
  try {
    localStorage.setItem(ACCENT_STORAGE_KEY, hex);
    localStorage.setItem(ACCENT_INK_STORAGE_KEY, ink);
    localStorage.setItem(ACCENT_TEXT_STORAGE_KEY, `${light} ${dark}`);
  } catch {
    // storage unavailable (private window): the color still applies for this session.
  }
}

/** Redraw the browser-tab favicon at the accent, and cache it so the next load's pre-paint
 *  shows the operator's accent instead of the default sky. The ink shell and the bone figure
 *  are fixed; only the eyes follow the accent (see appIcon). Chrome/Firefox/Edge honor an SVG
 *  data-URI favicon; Safari keeps the static /favicon.svg, which is the same mark at the
 *  default accent. */
function applyFavicon(hex: string): void {
  const uri = appIconDataUri(hex);
  const link = document.getElementById("favicon");
  if (link instanceof HTMLLinkElement) link.href = uri;
  try {
    localStorage.setItem(FAVICON_STORAGE_KEY, uri);
  } catch {
    // storage unavailable: the tab still shows the accent this session.
  }
}

/** Paint the browser's own chrome at the accent. index.html's comment on the tag lists which
 *  browsers read it and which ignore it (#945). The browser picks its own readable ink for
 *  anything it draws there, so `accentInk` does not apply here. No cache key of its own:
 *  index.html pre-paints the tag from the accent it already reads back. */
function applyThemeColor(hex: string): void {
  const meta = document.getElementById("theme-color");
  if (meta instanceof HTMLMetaElement) meta.content = hex;
}
