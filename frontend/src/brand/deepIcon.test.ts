// SPDX-License-Identifier: AGPL-3.0-or-later
//
// The committed icon files (public/favicon.svg and the rasterized PNGs) are snapshots of
// deepIconSvg at the default accent. This guards them against drift: if deepIcon.ts changes,
// favicon.svg must be regenerated from deepIconSvg and the PNGs re-rasterized from the two SVG
// variants (rounded rx=143 for favicon/touch/192/512, full-bleed {radius:0} for maskable). The
// vector match below guards the SVG; the size checks guard that every raster artifact is
// present at its declared dimensions, so a brand tweak can't leave a stale PNG unnoticed.
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

import { describe, expect, it } from "vitest";

import { DEFAULT_ACCENT } from "../accent";
import { deepDiskColors, deepIconSvg } from "./deepIcon";

const here = dirname(fileURLToPath(import.meta.url));
const publicDir = join(here, "..", "..", "public");
const faviconPath = join(publicDir, "favicon.svg");

/** A PNG's pixel size from its IHDR chunk: two big-endian uint32s right after the 8-byte
 *  signature, the 4-byte length and the "IHDR" tag. No image library needed. */
function pngSize(name: string): { width: number; height: number } {
  const buf = readFileSync(join(publicDir, name));
  return { width: buf.readUInt32BE(16), height: buf.readUInt32BE(20) };
}

describe("deep icon", () => {
  it("committed favicon.svg matches the generator at the default accent", () => {
    const committed = readFileSync(faviconPath, "utf8").trim();
    expect(committed).toBe(deepIconSvg(DEFAULT_ACCENT));
  });

  it("the platter follows the accent while the dark shell stays fixed", () => {
    const { light, dark, glow } = deepDiskColors(DEFAULT_ACCENT);
    expect(glow).toBe(DEFAULT_ACCENT);
    expect(dark).toBe("#146b8c"); // accent x 0.55
    expect(light).toBe("#3bc9ff"); // accent + 10% white
    // the shell is not derived from the accent: it is absent from the color output entirely
    expect(deepIconSvg(DEFAULT_ACCENT)).toContain("#0f3040");
  });

  it("offers a full-bleed square for masked surfaces (iOS, maskable)", () => {
    expect(deepIconSvg(DEFAULT_ACCENT, { radius: 0 })).toContain('<rect width="512" height="512" fill="url(#s)"/>');
    expect(deepIconSvg(DEFAULT_ACCENT)).toContain('rx="143"');
  });

  it("every committed raster icon is present at its declared size", () => {
    // Guards the generated artifacts the vector check can't: a brand tweak that regenerates
    // favicon.svg but forgets to re-rasterize a PNG (or drops one) fails here (PR-6, rule 17).
    const expected: Record<string, { width: number; height: number }> = {
      "favicon-32.png": { width: 32, height: 32 },
      "apple-touch-icon.png": { width: 180, height: 180 },
      "icon-192.png": { width: 192, height: 192 },
      "icon-512.png": { width: 512, height: 512 },
      "icon-maskable-512.png": { width: 512, height: 512 },
    };
    for (const [name, size] of Object.entries(expected)) {
      expect(pngSize(name), name).toEqual(size);
    }
  });
});
