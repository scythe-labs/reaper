// SPDX-License-Identifier: AGPL-3.0-or-later
//
// Vendor Scalar's self-contained browser bundle into public/vendor, where Vite serves
// it in dev and copies it into dist for the shipped container. The API reference page
// (GET /api/docs, see src/reaper/main.py) loads it from /vendor/scalar.js. No CDN is
// involved, it works offline, and the npm lockfile pins it like every other dependency.
//
// Runs from the predev/prebuild hooks so it can never be stale. public/vendor is
// gitignored because it is a build product.

import { copyFileSync, mkdirSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const source = join(
  here,
  "..",
  "node_modules",
  "@scalar",
  "api-reference",
  "dist",
  "browser",
  "standalone.js",
);
const target = join(here, "..", "public", "vendor", "scalar.js");

mkdirSync(dirname(target), { recursive: true });
copyFileSync(source, target);
console.log("vendored @scalar/api-reference -> public/vendor/scalar.js");
