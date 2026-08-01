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
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

import { describe, expect, it } from "vitest";

import { DEFAULT_ACCENT } from "../accent";
import { APP_ICON_RADIUS, appIconSvg } from "./appIcon";
import { DISSOLVE_BONE, DISSOLVE_INK } from "./dissolve";
import manifest from "./icons.generated.json";

const here = dirname(fileURLToPath(import.meta.url));
const frontendDir = join(here, "..", "..");
const publicDir = join(frontendDir, "public");

const sha256 = (data: BinaryLike) => createHash("sha256").update(data).digest("hex");

/** A PNG's pixel size from its IHDR chunk: two big-endian uint32s right after the 8-byte
 *  signature, the 4-byte length and the "IHDR" tag. No image library needed. */
function pngSize(name: string): { width: number; height: number } {
  const buf = readFileSync(join(publicDir, name));
  return { width: buf.readUInt32BE(16), height: buf.readUInt32BE(20) };
}

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
