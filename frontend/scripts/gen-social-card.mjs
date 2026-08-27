// SPDX-License-Identifier: AGPL-3.0-or-later
//
// Rasterizes the repository's social preview card from the drawing in src/brand/socialCard.ts.
//
//   npm run social-card
//
// Run this after any change to that drawing or to the brand mark it embeds, then commit what
// it writes and upload the PNG under Settings > General > Social preview. GitHub has no API
// for that upload, so the last step is a human with a browser. Committing the PNG lets the
// next person see what is currently published without going and looking.
//
// socialCard.test.ts is the backstop: it re-derives the source SVG and compares it to the hash
// recorded here, so a card generated from a drawing that has since moved fails the suite.
//
// The rasterized text depends on which fonts the generating machine has. resvg takes the first
// family in the stack that is installed, so regenerating on a different machine can shift the
// type without any source change. That is why the test pins the committed bytes to a recorded
// hash rather than re-rendering and comparing: a regeneration is a deliberate act that updates
// both, and CI never has to reproduce a raster it has no fonts for.
//
// Separate from gen-icons.mjs on purpose. That script's manifest is cross-checked against the
// icons index.html and site.webmanifest actually reference, and this card is referenced by
// neither. Folding it in would mean weakening that check to admit an asset it was written to
// catch.

import { createHash } from "node:crypto";
import { mkdir, writeFile } from "node:fs/promises";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

import { Resvg } from "@resvg/resvg-js";
import { createServer } from "vite";

const HERE = dirname(fileURLToPath(import.meta.url));
const ROOT = join(HERE, "..");
const REPO = join(ROOT, "..");
const OUT_DIR = join(REPO, ".github");
const OUT_FILE = "social-preview.png";
const MANIFEST = join(ROOT, "src", "brand", "socialCard.generated.json");

const sha256 = (data) => createHash("sha256").update(data).digest("hex");

async function loadBrand() {
  // Middleware mode keeps this from binding a port. Only the module runner is wanted, so the
  // script resolves imports the way the app does and needs no second bundler.
  const server = await createServer({
    root: ROOT,
    logLevel: "warn",
    appType: "custom",
    server: { middlewareMode: true },
  });
  try {
    const { socialCardSvg, SOCIAL_CARD_WIDTH, SOCIAL_CARD_HEIGHT } = await server.ssrLoadModule(
      "/src/brand/socialCard.ts",
    );
    const { DEFAULT_ACCENT } = await server.ssrLoadModule("/src/accent.ts");
    return { socialCardSvg, SOCIAL_CARD_WIDTH, SOCIAL_CARD_HEIGHT, DEFAULT_ACCENT };
  } finally {
    await server.close();
  }
}

const { socialCardSvg, SOCIAL_CARD_WIDTH, SOCIAL_CARD_HEIGHT, DEFAULT_ACCENT } = await loadBrand();

const svg = socialCardSvg(DEFAULT_ACCENT);
const png = new Resvg(svg, {
  font: { loadSystemFonts: true },
  fitTo: { mode: "width", value: SOCIAL_CARD_WIDTH },
})
  .render()
  .asPng();

await mkdir(OUT_DIR, { recursive: true });
await writeFile(join(OUT_DIR, OUT_FILE), png);

await writeFile(
  MANIFEST,
  JSON.stringify(
    {
      // Read by socialCard.test.ts. Regenerate with `npm run social-card`. Never hand-edit,
      // because the hashes are the only thing tying the PNG to the drawing it came from.
      generator: "frontend/scripts/gen-social-card.mjs",
      accent: DEFAULT_ACCENT,
      file: `.github/${OUT_FILE}`,
      width: SOCIAL_CARD_WIDTH,
      height: SOCIAL_CARD_HEIGHT,
      sourceSha256: sha256(svg),
      sha256: sha256(png),
      bytes: png.length,
    },
    null,
    2,
  ) + "\n",
);

// GitHub rejects an upload over 1 MB, and it is the one limit that turns a regeneration into a
// broken card rather than an ugly one.
const LIMIT = 1_000_000;
if (png.length > LIMIT) {
  console.error(`\n.github/${OUT_FILE} is ${png.length} bytes, over GitHub's ${LIMIT} limit.`);
  process.exit(1);
}

console.log(
  `  .github/${OUT_FILE}  ${SOCIAL_CARD_WIDTH}x${SOCIAL_CARD_HEIGHT}  ${png.length} bytes`,
);
console.log(`  src/brand/socialCard.generated.json`);
console.log(`\nUpload it under Settings > General > Social preview.`);
