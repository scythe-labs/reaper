// SPDX-License-Identifier: AGPL-3.0-or-later
//
// Generates every committed icon asset from the one drawing in src/brand/appIcon.ts.
//
//   npm run icons
//
// Run this after any change to the brand mark, then commit what it writes. appIcon.test.ts is
// the backstop: it re-derives each asset's source SVG and compares it to the hash recorded in
// icons.generated.json, so a mark that changed without a regeneration fails the suite instead
// of silently shipping stale rasters on iOS home screens and installed PWAs. A drift test that
// only checks each PNG's width and height cannot catch that, since a redraw never changes them.
//
// The TypeScript source is loaded through Vite's own module runner rather than a second
// bundler, so the script resolves imports exactly the way the app does and needs no dependency
// the frontend does not already declare.

import { createHash } from "node:crypto";
import { mkdir, writeFile } from "node:fs/promises";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

import { Resvg } from "@resvg/resvg-js";
import { createServer } from "vite";

const HERE = dirname(fileURLToPath(import.meta.url));
const ROOT = join(HERE, "..");
const PUBLIC = join(ROOT, "public");
const MANIFEST = join(ROOT, "src", "brand", "icons.generated.json");

/** Every generated asset, and the variant of the mark it is rasterized from.
 *
 *  `radius: 0` is the full-bleed square: iOS and the maskable manifest icon apply their own
 *  mask over the square, so baking our corners in would round it twice. Everything else wears
 *  the mark's own corner radius. This list is the single declaration of what ships; the test
 *  cross-checks it against what index.html and site.webmanifest actually reference, so an icon
 *  added to one and not the other fails rather than 404s. */
const ASSETS = [
  { file: "favicon.svg", kind: "svg", radius: undefined },
  { file: "favicon-32.png", kind: "png", size: 32, radius: undefined },
  { file: "apple-touch-icon.png", kind: "png", size: 180, radius: 0 },
  { file: "icon-192.png", kind: "png", size: 192, radius: undefined },
  { file: "icon-512.png", kind: "png", size: 512, radius: undefined },
  { file: "icon-maskable-512.png", kind: "png", size: 512, radius: 0 },
];

const sha256 = (data) => createHash("sha256").update(data).digest("hex");

async function loadBrand() {
  // `appType: "custom"` and middleware mode keep this from binding a port. Only the module
  // runner is wanted. Vite prints its own config-loading noise on stderr, which is harmless.
  const server = await createServer({
    root: ROOT,
    logLevel: "warn",
    appType: "custom",
    server: { middlewareMode: true },
  });
  try {
    const { appIconSvg, APP_ICON_RADIUS } = await server.ssrLoadModule("/src/brand/appIcon.ts");
    const { DEFAULT_ACCENT } = await server.ssrLoadModule("/src/accent.ts");
    return { appIconSvg, APP_ICON_RADIUS, DEFAULT_ACCENT };
  } finally {
    await server.close();
  }
}

const { appIconSvg, APP_ICON_RADIUS, DEFAULT_ACCENT } = await loadBrand();

await mkdir(PUBLIC, { recursive: true });

const entries = [];
for (const asset of ASSETS) {
  const opts = asset.radius === undefined ? {} : { radius: asset.radius };
  const svg = appIconSvg(DEFAULT_ACCENT, opts);
  const out = join(PUBLIC, asset.file);
  // Recorded so the test can re-derive this asset's exact source and compare hashes. The
  // default is spelled out rather than left implicit, so the manifest reads on its own.
  const radius = asset.radius === undefined ? APP_ICON_RADIUS : asset.radius;

  if (asset.kind === "svg") {
    await writeFile(out, svg + "\n");
    // The committed SVG is compared to `appIconSvg` byte for byte by the test, so its own hash
    // would be a second copy of the same fact. Record the source hash only, for symmetry.
    entries.push({ file: asset.file, kind: "svg", radius, sourceSha256: sha256(svg) });
    console.log(`  ${asset.file}`);
    continue;
  }

  const png = new Resvg(svg, { fitTo: { mode: "width", value: asset.size } }).render().asPng();
  await writeFile(out, png);
  entries.push({
    file: asset.file,
    kind: "png",
    size: asset.size,
    radius,
    sourceSha256: sha256(svg),
    sha256: sha256(png),
  });
  console.log(`  ${asset.file}  ${asset.size}x${asset.size}  ${png.length} bytes`);
}

await writeFile(
  MANIFEST,
  JSON.stringify(
    {
      // Read by appIcon.test.ts. Regenerate with `npm run icons`. Never hand-edit, because the
      // hashes are the only thing tying these files to the drawing they came from.
      generator: "frontend/scripts/gen-icons.mjs",
      accent: DEFAULT_ACCENT,
      assets: entries,
    },
    null,
    2,
  ) + "\n",
);

console.log(`  src/brand/icons.generated.json`);
console.log(`\n${entries.length} assets written at accent ${DEFAULT_ACCENT}.`);
