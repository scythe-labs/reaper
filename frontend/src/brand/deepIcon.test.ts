// SPDX-License-Identifier: AGPL-3.0-or-later
//
// The committed brand assets are generated from deepIcon.ts by scripts/gen-icons.mjs
// (`npm run icons`). This file is what makes regenerating them non-optional.
//
// favicon.svg is the easy half: it is SVG text, so the check is a text comparison. The PNGs are
// the hard half, and the reason there is a PNG reader below. A raster made from last month's
// drawing is a valid PNG at the right size with entirely plausible pixels -- there is nothing
// structurally wrong with it to notice. So the generator stamps each one with the sha256 of the
// exact SVG text it rendered, and the check is that the stamp still matches what deepIconSvg
// produces today. This file used to assert only each PNG's width and height, under a comment
// claiming that caught a stale asset; it could not, because dimensions are the one thing a brand
// change never touches (PR-9).
//
// Then three things about the pixels: the corner, which is the only way to tell the rounded badge
// from the full-bleed square (icon-512 and icon-maskable-512 are the same size and differ
// nowhere else, and putting the rounded one in the maskable slot has Android round an
// already-rounded badge and clip the drawing); the platter, which must be the accent; and the
// blade, which must be white.
import { createHash } from "node:crypto";
import { readdirSync, readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { inflateSync } from "node:zlib";

import { describe, expect, it } from "vitest";

import { DEFAULT_ACCENT } from "../accent";
import { BADGE_RADIUS, ICON_SOURCE_KEYWORD, ICON_TARGETS, deepDiskColors, deepIconSvg } from "./deepIcon";

const here = dirname(fileURLToPath(import.meta.url));
const publicDir = join(here, "..", "..", "public");
const faviconPath = join(publicDir, "favicon.svg");

/** Points on the drawing, as fractions of the image so one point means the same place at every
 *  size. All three were read off the committed rasters rather than reasoned about: the blade was
 *  first guessed a little high and landed on an antialiased edge, which reads as white at 512px
 *  and as pale blue at 32px. The platter point sits on plain gradient, inside the disk and
 *  outside the innermost ring, clear of the scythe. */
const CORNER: [number, number] = [0.012, 0.012];
const PLATTER: [number, number] = [0.79, 0.5];
const BLADE: [number, number] = [0.65, 0.35];

/** Room for antialiasing and the glow over the disk gradient's own two stops. */
const SLACK = 12;

interface Png {
  width: number;
  height: number;
  /** tEXt chunks, keyword to value. */
  text: Map<string, string>;
  at(fx: number, fy: number): [number, number, number, number];
}

/** Enough of a PNG reader to check what the generator wrote: the chunk walk, the five scanline
 *  filters, and node's zlib for the decompression. No image library, so this costs nothing to
 *  keep. Chrome drops the alpha channel when nothing in the image is transparent, so the
 *  full-bleed variants arrive as RGB and the rounded ones as RGBA; both come back RGBA here. */
function readPng(name: string): Png {
  const buf = readFileSync(join(publicDir, name));
  if (buf.subarray(0, 8).toString("latin1") !== "\x89PNG\r\n\x1a\n") throw new Error(`${name} is not a PNG`);

  const text = new Map<string, string>();
  const packed: Buffer[] = [];
  let width = 0;
  let height = 0;
  let channels = 0;
  for (let i = 8; i < buf.length; i += 12 + buf.readUInt32BE(i)) {
    const length = buf.readUInt32BE(i);
    const tag = buf.subarray(i + 4, i + 8).toString("latin1");
    const body = buf.subarray(i + 8, i + 8 + length);
    if (tag === "IHDR") {
      width = body.readUInt32BE(0);
      height = body.readUInt32BE(4);
      const [depth, colorType] = [body[8], body[9]];
      if (depth !== 8 || (colorType !== 2 && colorType !== 6)) {
        throw new Error(`${name}: expected 8-bit RGB or RGBA, got depth ${depth}, color type ${colorType}`);
      }
      channels = colorType === 6 ? 4 : 3;
    } else if (tag === "tEXt") {
      const split = body.indexOf(0);
      text.set(body.subarray(0, split).toString("latin1"), body.subarray(split + 1).toString("latin1"));
    } else if (tag === "IDAT") {
      packed.push(body);
    } else if (tag === "IEND") {
      break;
    }
  }
  if (!width || !packed.length) throw new Error(`${name}: no header or no pixel data`);

  // One filter byte then one scanline, per row. Checking the total up front is worth doing for
  // its own sake -- a truncated IDAT is real corruption -- and it also puts every index below in
  // range, which is why reading a byte can carry a fallback it will never reach.
  const stride = width * channels;
  const raw = inflateSync(Buffer.concat(packed));
  const expected = height * (stride + 1);
  if (raw.length !== expected) {
    throw new Error(`${name}: ${raw.length} bytes of pixel data, expected ${expected}`);
  }
  const byte = (buf: Buffer, i: number) => buf[i] ?? 0;

  const flat = Buffer.alloc(height * stride);
  for (let y = 0; y < height; y++) {
    const filter = byte(raw, y * (stride + 1));
    if (filter > 4) throw new Error(`${name}: unknown scanline filter ${filter} on row ${y}`);
    const source = y * (stride + 1) + 1;
    const row = y * stride;
    for (let x = 0; x < stride; x++) {
      const value = byte(raw, source + x);
      const left = x >= channels ? byte(flat, row + x - channels) : 0;
      const up = y > 0 ? byte(flat, row - stride + x) : 0;
      const upLeft = x >= channels && y > 0 ? byte(flat, row - stride + x - channels) : 0;
      let predicted = 0;
      if (filter === 1) predicted = left;
      else if (filter === 2) predicted = up;
      else if (filter === 3) predicted = (left + up) >> 1;
      else if (filter === 4) {
        // Paeth: whichever of left / up / up-left is closest to left + up - upLeft.
        const toLeft = Math.abs(up - upLeft);
        const toUp = Math.abs(left - upLeft);
        const toUpLeft = Math.abs(left + up - 2 * upLeft);
        predicted = toLeft <= toUp && toLeft <= toUpLeft ? left : toUp <= toUpLeft ? up : upLeft;
      }
      flat[row + x] = (value + predicted) & 0xff;
    }
  }

  return {
    width,
    height,
    text,
    at(fx, fy) {
      const x = Math.min(width - 1, Math.floor(fx * width));
      const y = Math.min(height - 1, Math.floor(fy * height));
      const i = y * stride + x * channels;
      return [byte(flat, i), byte(flat, i + 1), byte(flat, i + 2), channels === 4 ? byte(flat, i + 3) : 255];
    },
  };
}

function rgb(hex: string): [number, number, number] {
  return [parseInt(hex.slice(1, 3), 16), parseInt(hex.slice(3, 5), 16), parseInt(hex.slice(5, 7), 16)];
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
    expect(deepIconSvg(DEFAULT_ACCENT)).toContain(`rx="${BADGE_RADIUS}"`);
  });

  it("ships exactly the rasters ICON_TARGETS declares, at their declared sizes", () => {
    const shipped = readdirSync(publicDir)
      .filter((name) => name.endsWith(".png"))
      .sort();
    expect(shipped).toEqual(ICON_TARGETS.map((target) => target.file).sort());
    for (const { file, size } of ICON_TARGETS) {
      const { width, height } = readPng(file);
      expect({ file, width, height }).toEqual({ file, width: size, height: size });
    }
  });

  it("every raster was made from the drawing this module produces now", () => {
    // The drift check, and why the generator stamps a hash at all: a PNG rendered from an older
    // deepIconSvg is a valid image at the right size, so neither its pixels nor its header can
    // give it away. Change the drawing (here or in ./scythe) without running `npm run icons`
    // and every one of these fails.
    for (const { file, radius } of ICON_TARGETS) {
      const stamped = readPng(file).text.get(ICON_SOURCE_KEYWORD);
      const current = createHash("sha256").update(deepIconSvg(DEFAULT_ACCENT, { radius })).digest("hex");
      expect(stamped, `${file} was made from a different drawing: run npm run icons`).toBe(current);
    }
  });

  it("takes the rounded badge or the full-bleed square, as its target says", () => {
    for (const { file, radius } of ICON_TARGETS) {
      const [red, green, blue, alpha] = readPng(file).at(...CORNER);
      if (radius > 0) {
        expect(alpha, `${file} is the rounded badge, so its corner is outside the drawing`).toBe(0);
      } else {
        expect(alpha, `${file} is full-bleed, so its corner is painted`).toBe(255);
        expect(Math.max(red, green, blue), `${file}'s corner should be the dark shell`).toBeLessThan(80);
      }
    }
  });

  it("draws the accent on the platter and white on the blade, in every raster", () => {
    const { light, dark } = deepDiskColors(DEFAULT_ACCENT);
    const [low, high] = [rgb(dark), rgb(light)];
    for (const { file } of ICON_TARGETS) {
      const png = readPng(file);
      const platter = png.at(...PLATTER);
      const blade = png.at(...BLADE);
      expect(platter[3], `${file}: the platter is opaque`).toBe(255);
      const bounds: [string, number, number, number][] = [
        ["red", platter[0], low[0], high[0]],
        ["green", platter[1], low[1], high[1]],
        ["blue", platter[2], low[2], high[2]],
      ];
      for (const [channel, value, darkest, lightest] of bounds) {
        const floor = Math.min(darkest, lightest) - SLACK;
        const ceiling = Math.max(darkest, lightest) + SLACK;
        const where = `${file}: ${channel} on the platter is ${value}, outside the accent's ${floor}..${ceiling}`;
        expect(value, where).toBeGreaterThanOrEqual(floor);
        expect(value, where).toBeLessThanOrEqual(ceiling);
      }
      expect(Math.min(blade[0], blade[1], blade[2]), `${file}: the blade is white`).toBeGreaterThan(235);
      expect(blade[3], `${file}: the blade is opaque`).toBe(255);
    }
  });
});
