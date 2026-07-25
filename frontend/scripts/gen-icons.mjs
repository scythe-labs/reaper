// SPDX-License-Identifier: AGPL-3.0-or-later
//
// Regenerate the committed brand assets from src/brand/deepIcon.ts, the one place the Deep icon
// is drawn: public/favicon.svg (the vector, verbatim) and every PNG in `ICON_TARGETS` (that
// vector rasterized). Run it after any change to deepIcon.ts or scythe.ts:
//
//     npm --prefix frontend run icons
//
// Nothing here decides what to draw or what sizes to write -- both come from deepIcon.ts, so
// this file cannot drift from the drawing or from the test. What it adds is provenance: each
// PNG gets a `reaper-source` tEXt chunk holding the sha256 of the exact SVG text that produced
// it, which deepIcon.test.ts recomputes. That is what makes a stale raster fail: its pixels are
// valid and its size is right, but it names a drawing that no longer exists.
//
// Why Chrome rather than an npm rasterizer: the tab favicon at runtime IS this SVG handed
// straight to the browser (deepIconDataUri), so rendering the PNGs in Blink means the
// home-screen icon and the tab icon are one drawing off one engine. It also keeps a native
// image binary out of package.json -- and therefore out of the lockfile and the container
// build -- for a script that runs when the brand changes. Chrome writes IHDR/IDAT/IEND and no
// timestamps, and the pixels are re-deflated here rather than left at whatever level the
// installed Chrome chose, so regenerating with no drawing change rewrites the same bytes and
// says "unchanged".

import { spawn, spawnSync } from "node:child_process";
import { createHash } from "node:crypto";
import { existsSync, mkdtempSync, openSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { constants, crc32, deflateSync, inflateSync } from "node:zlib";

import { createServer } from "vite";

const here = dirname(fileURLToPath(import.meta.url));
const frontend = join(here, "..");
const publicDir = join(frontend, "public");

/** Chrome, in the places it actually lives, with `CHROME=` as the override for everywhere
 *  else. A bare name is probed by running it, since PATH is the only thing that can say. */
function findChrome() {
  const candidates = [
    process.env.CHROME,
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
    "google-chrome",
    "google-chrome-stable",
    "chromium",
    "chromium-browser",
  ].filter(Boolean);
  for (const candidate of candidates) {
    if (candidate.includes("/")) {
      if (existsSync(candidate)) return candidate;
    } else if (spawnSync(candidate, ["--version"], { stdio: "ignore" }).status === 0) {
      return candidate;
    }
  }
  return null;
}

/** A PNG's terminator: a zero-length `IEND` chunk with its constant CRC. Nothing follows it, so
 *  finding it at the end of the file is proof the write finished -- not a guess about timing. */
const IEND = Buffer.from([0, 0, 0, 0, 0x49, 0x45, 0x4e, 0x44, 0xae, 0x42, 0x60, 0x82]);

/** Read the screenshot once Chrome has finished writing it, then let the caller stop Chrome.
 *  Polling looks crude next to waiting on exit, but waiting on exit does not work: given its
 *  own --user-data-dir, Chrome writes the screenshot and then stays up indefinitely (it exits
 *  on its own only when it is sharing the default profile). Rather than depend on that, we wait
 *  for the bytes we came for and shut it down ourselves. */
async function readWhenComplete(shot, child, timeoutMs = 60_000) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    if (existsSync(shot)) {
      const png = readFileSync(shot);
      if (png.length > IEND.length && png.subarray(png.length - IEND.length).equals(IEND)) return png;
    } else if (child.exitCode !== null) {
      throw new Error(`Chrome exited (${child.exitCode}) without writing ${shot}`);
    }
    await new Promise((resolve) => setTimeout(resolve, 50));
  }
  throw new Error(`Chrome never finished writing ${shot}`);
}

/** Stop Chrome and wait until it is actually gone. Signaling it is not enough: a shutting-down
 *  Chrome keeps writing to its profile directory for a moment, and removing the directory under
 *  it fails with ENOTEMPTY. SIGKILL is the backstop if it will not go quietly. */
async function stop(child) {
  if (child.exitCode !== null || child.signalCode !== null) return;
  child.kill();
  await new Promise((resolve) => {
    const hard = setTimeout(() => {
      child.kill("SIGKILL");
      resolve();
    }, 5_000);
    child.once("exit", () => {
      clearTimeout(hard);
      resolve();
    });
  });
}

/** Rasterize one SVG at one size. The wrapper page pins the drawing to an exact pixel box and
 *  removes the document margin, so the screenshot is the icon and nothing else. */
async function rasterize(chrome, svg, size, workDir, name) {
  const stem = join(workDir, name.replace(/\.png$/, ""));
  const page = `${stem}.html`;
  const shot = `${stem}.png`;
  writeFileSync(
    page,
    `<!doctype html><meta charset="utf-8">` +
      `<style>html,body{margin:0;padding:0;background:transparent}` +
      `svg{display:block;width:${size}px;height:${size}px}</style>${svg}`,
  );
  const child = spawn(
    chrome,
    [
      "--headless",
      "--disable-gpu",
      "--hide-scrollbars",
      "--force-device-scale-factor=1",
      // Transparent, or the rounded variants come back with white corners.
      "--default-background-color=00000000",
      // A throwaway profile: never touch the one the person running this is signed into, and
      // never fail because their Chrome is already open on it.
      `--user-data-dir=${join(workDir, "profile")}`,
      "--no-first-run",
      "--no-default-browser-check",
      `--window-size=${size},${size}`,
      `--screenshot=${shot}`,
      page,
    ],
    // Chrome's helper processes inherit stderr, so a pipe here would not reach EOF until the
    // last of them died. A file has no such wait, and keeps the output for a failure message.
    { stdio: ["ignore", "ignore", openSync(`${stem}.log`, "w")] },
  );
  try {
    return await readWhenComplete(shot, child);
  } catch (err) {
    const log = existsSync(`${stem}.log`) ? readFileSync(`${stem}.log`, "utf8").trim() : "";
    throw new Error(`${err.message}${log ? `\n${log}` : ""}`);
  } finally {
    await stop(child);
  }
}

/** One PNG chunk: big-endian length, four-byte tag, body, CRC over tag and body. */
function chunk(tagName, body) {
  const tag = Buffer.from(tagName, "latin1");
  const out = Buffer.alloc(body.length + 12);
  out.writeUInt32BE(body.length, 0);
  tag.copy(out, 4);
  body.copy(out, 8);
  out.writeUInt32BE(crc32(Buffer.concat([tag, body])), body.length + 8);
  return out;
}

/** Re-encode the pixel data at maximum compression, losslessly: inflate exactly what Chrome
 *  wrote and deflate the identical bytes again at level 9. Chrome's encoder favors speed, so
 *  this takes about a sixth off the committed files, and it puts the compression level under our
 *  control rather than the installed Chrome's. Every other chunk is copied through untouched, in
 *  order, and the one re-deflated IDAT lands where the first of Chrome's was. */
function recompress(png) {
  const before = [];
  const after = [];
  const pixels = [];
  let i = 8;
  while (i < png.length) {
    const length = png.readUInt32BE(i);
    const tag = png.subarray(i + 4, i + 8).toString("latin1");
    if (tag === "IDAT") pixels.push(png.subarray(i + 8, i + 8 + length));
    else (pixels.length ? after : before).push(png.subarray(i, i + 12 + length));
    if (tag === "IEND") break;
    i += 12 + length;
  }
  if (!pixels.length) throw new Error("the screenshot Chrome wrote has no pixel data");
  const packed = deflateSync(inflateSync(Buffer.concat(pixels)), {
    level: 9,
    strategy: constants.Z_FILTERED, // what libpng uses: the data is already filter-encoded
    memLevel: 9,
  });
  return Buffer.concat([png.subarray(0, 8), ...before, chunk("IDAT", packed), ...after]);
}

/** Insert a `tEXt` chunk immediately after IHDR, where the spec allows any ancillary chunk and
 *  a stream reader meets the provenance before the pixels. IHDR is fixed-length, so its end is
 *  the 8-byte signature plus 4 length + 4 tag + 13 data + 4 CRC. */
function stamp(png, keyword, text) {
  const body = Buffer.concat([Buffer.from(keyword, "latin1"), Buffer.from([0]), Buffer.from(text, "latin1")]);
  const afterIhdr = 8 + 25;
  return Buffer.concat([png.subarray(0, afterIhdr), chunk("tEXt", body), png.subarray(afterIhdr)]);
}

const chrome = findChrome();
if (!chrome) {
  console.error(
    "No Chrome or Chromium found, so there is nothing to rasterize with.\n" +
      "Install one, or point at it: CHROME=/path/to/chrome npm run icons",
  );
  process.exit(1);
}

// Load the drawing through Vite's own resolver, so the extensionless `./scythe` import resolves
// here exactly as it does in the bundle -- one drawing, resolved one way.
const server = await createServer({
  root: frontend,
  configFile: false,
  logLevel: "warn",
  server: { middlewareMode: true },
  appType: "custom",
});
let deepIconSvg, ICON_TARGETS, ICON_SOURCE_KEYWORD, DEFAULT_ACCENT;
try {
  ({ deepIconSvg, ICON_TARGETS, ICON_SOURCE_KEYWORD } = await server.ssrLoadModule("/src/brand/deepIcon.ts"));
  ({ DEFAULT_ACCENT } = await server.ssrLoadModule("/src/accent.ts"));
} finally {
  await server.close();
}

/** Write one asset, reporting whether that changed what was committed. */
function put(file, bytes) {
  const target = join(publicDir, file);
  const buffer = Buffer.from(bytes);
  const before = existsSync(target) ? readFileSync(target) : null;
  writeFileSync(target, buffer);
  return before?.equals(buffer) ? "unchanged" : "written";
}

const report = (file, shape, state) => console.log(`  ${file.padEnd(24)} ${shape.padEnd(18)} ${state}`);

console.log(`rasterizing with ${chrome}`);
report("favicon.svg", "vector", put("favicon.svg", `${deepIconSvg(DEFAULT_ACCENT)}\n`));

const workDir = mkdtempSync(join(tmpdir(), "reaper-icons-"));
try {
  for (const { file, size, radius } of ICON_TARGETS) {
    const svg = deepIconSvg(DEFAULT_ACCENT, { radius });
    const hash = createHash("sha256").update(svg).digest("hex");
    const png = stamp(recompress(await rasterize(chrome, svg, size, workDir, file)), ICON_SOURCE_KEYWORD, hash);
    report(file, `${size}px ${radius > 0 ? "rounded" : "full-bleed"}`, put(file, png));
  }
} finally {
  rmSync(workDir, { recursive: true, force: true, maxRetries: 10, retryDelay: 100 });
}
