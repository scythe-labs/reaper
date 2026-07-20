// SPDX-License-Identifier: AGPL-3.0-or-later
//
// The one operator-chosen color, applied at runtime. Settings → General stores a #rrggbb
// accent on the server; the whole UI derives from --accent (see index.css), so applying it
// is just setting two custom properties on <html>:
//   --accent      the color itself
//   --accent-ink  the text that rides on a solid accent fill -- dark or light by luminance,
//                 so a bright accent never ends up with unreadable pale text on it.
// The hover / soft / ring shades are pure color-mix() of --accent in the stylesheet, so they
// follow along on their own.
//
// index.html applies these same two properties before first paint, from a localStorage
// cache, so a custom accent does not flash the sky-blue default on load. This module writes
// that cache and is the runtime applier once the saved value arrives.

import { deepIconDataUri } from "./brand/deepIcon";

const HEX = /^#[0-9a-fA-F]{6}$/;
const DARK_INK = "#06202c";
const LIGHT_INK = "#ffffff";
export const ACCENT_STORAGE_KEY = "reaper-accent";
/** Cache of the last favicon data URI, so index.html can pre-paint the tab icon at the saved
 *  accent before this module runs -- the same trick the accent color itself uses. */
export const FAVICON_STORAGE_KEY = "reaper-favicon";
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

/** Apply the accent to the document, and cache it so index.html can pre-paint it next load.
 *  A missing or malformed value is ignored: the stylesheet default stands, never a broken
 *  color. */
export function applyAccent(color: string | null | undefined): void {
  if (!color || !HEX.test(color)) return;
  const hex = color.toLowerCase();
  const root = document.documentElement;
  root.style.setProperty("--accent", hex);
  root.style.setProperty("--accent-ink", accentInk(hex));
  applyFavicon(hex);
  try {
    localStorage.setItem(ACCENT_STORAGE_KEY, hex);
  } catch {
    // storage unavailable (private window): the color still applies for this session.
  }
}

/** Redraw the browser-tab favicon (the Deep icon) at the accent, and cache it so the next
 *  load's pre-paint shows the operator's accent instead of the default sky. The dark shell is
 *  fixed; only the platter follows the accent (see deepIcon). Chrome/Firefox/Edge honor an
 *  SVG data-URI favicon; Safari keeps the static /favicon.svg default, which is still Deep. */
function applyFavicon(hex: string): void {
  const uri = deepIconDataUri(hex);
  const link = document.getElementById("favicon");
  if (link instanceof HTMLLinkElement) link.href = uri;
  try {
    localStorage.setItem(FAVICON_STORAGE_KEY, uri);
  } catch {
    // storage unavailable: the tab still shows the accent this session.
  }
}
