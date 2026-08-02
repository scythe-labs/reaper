// SPDX-License-Identifier: AGPL-3.0-or-later
//
// The committed icon files (public/favicon.svg and the rasterized PNGs) are snapshots of
// appIconSvg at the default accent, produced by `npm run icons`. This guards them against
// drift.
//
// The previous version of this file checked only each PNG's width and height, and said in its
// own comment that it caught a forgotten re-rasterization. It did not: a redraw of the mark
// never changes a PNG's dimensions, so all five would have sailed through carrying the old
// picture while favicon.svg alone was updated (PR9, rule 68). What closes that is the source
// hash in icons.generated.json: the generator records the sha256 of the exact SVG string each
// asset was rasterized from, and the check below re-derives that string from the CURRENT
// appIconSvg. Change the mark without regenerating and every stale asset fails by name.
//
// The rasterizer is deliberately not invoked here. Re-rendering to compare pixels would make
// the suite depend on resvg producing byte-identical output on every machine and version; the
// recorded hashes are committed alongside the files they describe, so the test needs nothing
// but fs and crypto.
import { type BinaryLike, createHash } from "node:crypto";
import { readFileSync, writeFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

import { Resvg } from "@resvg/resvg-js";
import { describe, expect, it } from "vitest";

import { DEFAULT_ACCENT } from "../accent";
import { APP_ICON_RADIUS, appIconSvg } from "./appIcon";
import { DISSOLVE_BONE, DISSOLVE_CUT, DISSOLVE_INK } from "./dissolve";
import manifest from "./icons.generated.json";

const here = dirname(fileURLToPath(import.meta.url));
const frontendDir = join(here, "..", "..");
const publicDir = join(frontendDir, "public");
const brandDir = dirname(fileURLToPath(import.meta.url));

const sha256 = (data: BinaryLike) => createHash("sha256").update(data).digest("hex");

/** A PNG's pixel size from its IHDR chunk: two big-endian uint32s right after the 8-byte
 *  signature, the 4-byte length and the "IHDR" tag. No image library needed. */
function pngSize(name: string): { width: number; height: number } {
  const buf = readFileSync(join(publicDir, name));
  return { width: buf.readUInt32BE(16), height: buf.readUInt32BE(20) };
}

describe("the flattened mark", () => {
  // ./dissolve.generated.ts is the finished shape, flattened from the recipe in ./dissolve.ts
  // by scripts/gen-mark.mjs. Nothing composes the recipe at render time any more, so an edit to
  // the recipe that is not regenerated ships a mark that no longer matches its own source --
  // and it ships silently, because both files are individually valid. Rule 68: a generated
  // asset is covered by a drift test, and this is that asset's.
  //
  // The generator is re-run rather than its output re-parsed, so this compares geometry to the
  // recipe it claims to come from rather than to a second copy of itself.
  it("still matches the recipe it was generated from", async () => {
    const { execFileSync } = await import("node:child_process");
    const before = readFileSync(join(brandDir, "dissolve.generated.ts"), "utf8");
    execFileSync("node", [join(brandDir, "..", "..", "scripts", "gen-mark.mjs")], {
      cwd: join(brandDir, "..", ".."),
      stdio: "pipe",
    });
    const after = readFileSync(join(brandDir, "dissolve.generated.ts"), "utf8");
    if (before !== after) {
      writeFileSync(join(brandDir, "dissolve.generated.ts"), before);
    }
    expect(after).toBe(before);
  }, 60_000);

  // The mark draws on the DEFAULT nonzero rule, and the blocks depend on it: they overlap on
  // purpose, and evenodd would turn every overlap into a hole. Nothing needs evenodd any more --
  // the cowl opening is a notch in the silhouette's one contour rather than a hole beside it --
  // so a fill rule reappearing here means someone split that contour back apart.
  it("draws on the default fill rule", () => {
    expect(appIconSvg(DEFAULT_ACCENT)).not.toContain("fill-rule");
  });

  // The cut is where the mark used to seam, in both directions at once. The cowl opening's
  // bottom edge lay exactly on the hood's, and two coincident edges under evenodd can be painted
  // as a faint LIGHT line across an opening that is empty; the two blocks below the cut abutted
  // the hood from a second path, and two shapes antialiased separately then composited leave a
  // DARK one. Both showed only where the cut fell between device pixels, which is why this
  // sweeps widths instead of picking one, and why it reads a rendering rather than the `d`
  // string: what seams is the rasterization, and the path text looked reasonable throughout.
  //
  // Mutation-checked against the two-path geometry this replaced, which fails it at four of
  // these eight widths. The other four land the cut on a pixel boundary and seam either way;
  // which four that is belongs to the geometry, so all eight stay and the sampled-pixel counts
  // are asserted -- a box that rounds down to nothing passes every assertion made about it.
  it("draws no seam where the figure crosses the cut", () => {
    const svg = appIconSvg(DEFAULT_ACCENT);
    const red = (hex: string) => parseInt(hex.slice(1, 3), 16);
    const cut = DISSOLVE_CUT.y;

    for (const width of [96, 121, 137, 180, 211, 256, 301, 384]) {
      const { pixels, width: rendered } = new Resvg(svg, {
        fitTo: { mode: "width", value: width },
      }).render();
      const scale = rendered / 64;
      /** The red channel over a box given on the 64 grid: bone reads 0xed, ink 0x14. */
      const box = (x0: number, y0: number, x1: number, y1: number) => {
        let min = 255,
          max = 0,
          count = 0;
        for (let y = Math.ceil(y0 * scale); y < Math.floor(y1 * scale); y++) {
          for (let x = Math.ceil(x0 * scale); x < Math.floor(x1 * scale); x++) {
            const v = pixels[(y * rendered + x) * 4];
            // Thrown rather than defaulted: either default silently relaxes one of the two
            // assertions below, in the direction that makes a seam look clean.
            if (v === undefined) throw new Error(`sampled outside the rendering at ${x},${y}`);
            count++;
            if (v < min) min = v;
            if (v > max) max = v;
          }
        }
        return { min, max, count };
      };

      // Under the right-hand block the figure is solid on both sides of the cut, so every pixel
      // between them is bone. A seam is a dip.
      const join = box(46.5, cut - 0.8, 49.5, cut + 0.8);
      expect(join.count, `no pixels sampled at the join, width ${width}`).toBeGreaterThan(0);
      expect(join.min, `the figure is seamed at the cut, width ${width}`).toBe(red(DISSOLVE_BONE));

      // Inside the cowl opening the figure is absent on both sides of it, so every pixel is the
      // shell showing through. Anything brighter is paint where the mark has no shape.
      const open = box(27, cut - 1.4, 37, cut + 1.4);
      expect(open.count, `no pixels sampled in the opening, width ${width}`).toBeGreaterThan(0);
      expect(open.max, `the cowl opening is painted at the cut, width ${width}`).toBe(
        red(DISSOLVE_INK),
      );
    }
  });
});

describe("app icon", () => {
  it("committed favicon.svg matches the generator at the default accent", () => {
    const committed = readFileSync(join(publicDir, "favicon.svg"), "utf8").trim();
    expect(committed).toBe(appIconSvg(DEFAULT_ACCENT));
  });

  it("the eyes follow the accent while the shell and the figure stay fixed", () => {
    const svg = appIconSvg("#ff0000");
    // Both eyes, and nothing else, are painted in the passed color.
    expect(svg.match(/#ff0000/g)).toHaveLength(2);
    // The shell and figure are absent from the accent's influence entirely. Read from the
    // drawing rather than transcribed: these two literals sat here as "#14161C" and "#EDE7DA",
    // so recoloring the figure failed HERE, in a test about the accent, naming a color it was
    // never about (rule 119).
    expect(svg).toContain(DISSOLVE_INK);
    expect(svg).toContain(DISSOLVE_BONE);
    expect(DISSOLVE_BONE).not.toBe(DISSOLVE_INK);
    expect(appIconSvg("#00ff00")).not.toContain("#ff0000");
  });

  it("offers a full-bleed square for masked surfaces (iOS, maskable)", () => {
    expect(appIconSvg(DEFAULT_ACCENT, { radius: 0 })).toContain(
      `<rect width="64" height="64" rx="0" fill="${DISSOLVE_INK}"/>`,
    );
    expect(appIconSvg(DEFAULT_ACCENT)).toContain(`rx="${APP_ICON_RADIUS}"`);
  });

  it("every committed asset was generated from the CURRENT drawing", () => {
    // The check the old size assertion only claimed to make. If the mark is edited and
    // `npm run icons` is not re-run, the re-derived source no longer hashes to what the
    // manifest recorded, and each stale asset fails here by name.
    expect(manifest.assets.length).toBeGreaterThan(0);
    for (const asset of manifest.assets) {
      const source = appIconSvg(DEFAULT_ACCENT, { radius: asset.radius });
      expect(sha256(source), `${asset.file} is stale: re-run \`npm run icons\``).toBe(
        asset.sourceSha256,
      );
    }
  });

  it("every committed raster matches its recorded bytes and declared size", () => {
    const pngs = manifest.assets.filter((a) => a.kind === "png");
    expect(pngs.length).toBe(5);
    for (const asset of pngs) {
      const bytes = readFileSync(join(publicDir, asset.file));
      expect(sha256(bytes), `${asset.file} was edited by hand`).toBe(asset.sha256);
      expect(pngSize(asset.file), asset.file).toEqual({ width: asset.size, height: asset.size });
    }
  });

  it("the manifest covers every icon the app actually references", () => {
    // Rule 103: the generator's asset list is a hardcoded set, so something has to fail when
    // an icon is added to index.html or the web manifest and not to the generator. Referencing
    // a file nobody generates is how a 404 favicon ships.
    const html = readFileSync(join(frontendDir, "index.html"), "utf8");
    const webmanifest = readFileSync(join(publicDir, "site.webmanifest"), "utf8");

    const referenced = new Set<string>();
    for (const match of html.matchAll(/(?:href|src)="\/([^"]+\.(?:svg|png|ico))"/g)) {
      if (match[1]) referenced.add(match[1]);
    }
    for (const match of webmanifest.matchAll(/"src":\s*"\/([^"]+)"/g)) {
      if (match[1]) referenced.add(match[1]);
    }

    expect(referenced.size).toBeGreaterThan(0);
    const generated = new Set(manifest.assets.map((a) => a.file));
    for (const file of referenced) {
      expect(generated, `${file} is referenced but not generated`).toContain(file);
    }
  });
});
