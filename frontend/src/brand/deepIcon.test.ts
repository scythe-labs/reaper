// SPDX-License-Identifier: AGPL-3.0-or-later
//
// The committed icon files (public/favicon.svg and the rasterized PNGs) are snapshots of
// deepIconSvg at the default accent. This guards the vector against drift: if deepIcon.ts
// changes, favicon.svg must be regenerated (and the PNGs re-rasterized from it).
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

import { describe, expect, it } from "vitest";

import { DEFAULT_ACCENT } from "../accent";
import { deepDiskColors, deepIconSvg } from "./deepIcon";

const here = dirname(fileURLToPath(import.meta.url));
const faviconPath = join(here, "..", "..", "public", "favicon.svg");

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
});
