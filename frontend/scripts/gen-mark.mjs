// SPDX-License-Identifier: AGPL-3.0-or-later
//
// Flattens the brand mark's RECIPE into the finished shape it draws.
//
//   npm run mark
//
// `src/brand/dissolve.ts` describes the mark as a composition: take a hood, cut a band below
// the shoulders, lay blocks on the band, cut a face cavity through them, scatter more blocks.
// Every consumer used to perform that composition at render time, and `BrandMark` -- the
// shell-less form, whose cut-outs must be genuinely transparent -- could only do it with an SVG
// `mask`. A mask is rasterized into an offscreen buffer sized to the element's layout box, so
// pinch-zoom scales a bitmap: the figure went soft while the eyes, the only part outside the
// mask, stayed sharp.
//
// So the composition happens ONCE, here, and what ships is geometry. Two paths come out:
//
//   HEAD    the hood above the cut, with the cowl opening as an evenodd hole
//   BLOCKS  everything below the cut -- the trimmed left column, and the blocks that need no
//           trimming at all -- as one nonzero path, so overlapping blocks union instead of
//           cancelling
//
// Neither needs a mask, and both stay vector at any zoom.
//
// The only geometry operation involved is splitting a cubic at a parameter (de Casteljau).
// There is no boolean here and none is needed, which is worth stating because it looks like
// there should be: above the cut the cowl opening lies strictly inside the hood, so evenodd
// gives the hole for free; below it, the right-hand and scattered blocks sit clear of the
// cavity entirely, and only the left column is trimmed -- by a single bezier segment.
//
// `appIcon.test.ts`'s sibling drift test is what keeps this honest: it re-runs the flattening
// and fails if the committed output no longer matches the recipe.

import { writeFile } from "node:fs/promises";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

import { createServer } from "vite";

const HERE = dirname(fileURLToPath(import.meta.url));
const ROOT = join(HERE, "..");
const OUT = join(ROOT, "src", "brand", "dissolve.generated.ts");

/* ---------------------------------------------------------------- path parsing */

/** The path subset `dissolve.ts` actually uses. Anything else throws rather than being
 *  quietly skipped: a command this does not understand would silently drop part of the mark,
 *  and the drift test would then pin the wrong shape. */
function parsePath(d) {
  const tokens = d.match(/[MmCcHhVvLlZz]|-?\d*\.?\d+/g) ?? [];
  const segs = [];
  let i = 0;
  let cur = [0, 0];
  let start = [0, 0];
  let cmd = null;
  const num = () => Number(tokens[i++]);
  while (i < tokens.length) {
    if (/[A-Za-z]/.test(tokens[i])) cmd = tokens[i++];
    if (cmd === "M" || cmd === "m") {
      const p = cmd === "M" ? [num(), num()] : [cur[0] + num(), cur[1] + num()];
      cur = p;
      start = p;
      cmd = cmd === "M" ? "L" : "l";
      continue;
    }
    if (cmd === "C" || cmd === "c") {
      const rel = cmd === "c";
      const p1 = rel ? [cur[0] + num(), cur[1] + num()] : [num(), num()];
      const p2 = rel ? [cur[0] + num(), cur[1] + num()] : [num(), num()];
      const p3 = rel ? [cur[0] + num(), cur[1] + num()] : [num(), num()];
      segs.push({ t: "C", p0: cur, p1, p2, p3 });
      cur = p3;
      continue;
    }
    if (cmd === "H" || cmd === "h") {
      const x = cmd === "H" ? num() : cur[0] + num();
      segs.push({ t: "L", p0: cur, p3: [x, cur[1]] });
      cur = [x, cur[1]];
      continue;
    }
    if (cmd === "L" || cmd === "l") {
      const p = cmd === "L" ? [num(), num()] : [cur[0] + num(), cur[1] + num()];
      segs.push({ t: "L", p0: cur, p3: p });
      cur = p;
      continue;
    }
    if (cmd === "Z" || cmd === "z") {
      if (cur[0] !== start[0] || cur[1] !== start[1]) segs.push({ t: "L", p0: cur, p3: start });
      cur = start;
      continue;
    }
    throw new Error(`gen-mark: unsupported path command ${cmd} in ${d}`);
  }
  return segs;
}

/* ---------------------------------------------------------------- cubic splitting */

const lerp = (a, b, t) => [a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t];

function cubicAt(s, t) {
  const a = lerp(s.p0, s.p1, t),
    b = lerp(s.p1, s.p2, t),
    c = lerp(s.p2, s.p3, t);
  const d = lerp(a, b, t),
    e = lerp(b, c, t);
  return lerp(d, e, t);
}

/** The two halves of a cubic at `t`. de Casteljau, so both halves are exact. */
function splitCubic(s, t) {
  const a = lerp(s.p0, s.p1, t),
    b = lerp(s.p1, s.p2, t),
    c = lerp(s.p2, s.p3, t);
  const d = lerp(a, b, t),
    e = lerp(b, c, t);
  const f = lerp(d, e, t);
  return [
    { t: "C", p0: s.p0, p1: a, p2: d, p3: f },
    { t: "C", p0: f, p1: e, p2: c, p3: s.p3 },
  ];
}

/** Where a segment crosses `y`, as a parameter. Bisection: every segment this is asked about
 *  is monotonic in y over the range that matters, and bisection cannot diverge. */
function tAtY(s, y) {
  const at = (t) => (s.t === "L" ? lerp(s.p0, s.p3, t) : cubicAt(s, t))[1];
  let lo = 0,
    hi = 1;
  if ((at(0) - y) * (at(1) - y) > 0) return null;
  for (let k = 0; k < 60; k++) {
    const mid = (lo + hi) / 2;
    if ((at(lo) - y) * (at(mid) - y) <= 0) hi = mid;
    else lo = mid;
  }
  return (lo + hi) / 2;
}

const n = (v) => {
  const r = Math.round(v * 1000) / 1000;
  return String(r);
};

/** Segments to a `d` string, closed. */
function toPath(segs) {
  let out = `M${n(segs[0].p0[0])} ${n(segs[0].p0[1])}`;
  for (const s of segs) {
    out +=
      s.t === "L"
        ? `L${n(s.p3[0])} ${n(s.p3[1])}`
        : `C${n(s.p1[0])} ${n(s.p1[1])} ${n(s.p2[0])} ${n(s.p2[1])} ${n(s.p3[0])} ${n(s.p3[1])}`;
  }
  return out + "Z";
}

/** The part of a closed outline above `y`, closed off with a straight edge along `y`.
 *
 *  Both outlines this is used on cross `y` exactly twice, which is asserted rather than
 *  assumed: a redrawn hood that waisted in and out would cross four times, and silently
 *  keeping the first two would lose a piece of the figure. */
function truncateAbove(segs, y) {
  const cuts = [];
  segs.forEach((s, idx) => {
    const t = tAtY(s, y);
    if (t !== null && t > 1e-9 && t < 1 - 1e-9) cuts.push({ idx, t });
  });
  if (cuts.length !== 2) {
    throw new Error(`gen-mark: outline crosses y=${y} ${cuts.length} times, expected 2`);
  }
  const [first, second] = cuts;
  const out = [];
  for (let k = 0; k < segs.length; k++) {
    const s = segs[k];
    if (k < first.idx) out.push(s);
    else if (k === first.idx) {
      out.push(
        s.t === "C" ? splitCubic(s, first.t)[0] : { t: "L", p0: s.p0, p3: cubicOrLine(s, first.t) },
      );
      // Straight across to where the outline comes back up through y.
      const back = segs[second.idx];
      const rejoin = back.t === "C" ? cubicAt(back, second.t) : lerp(back.p0, back.p3, second.t);
      const leave = s.t === "C" ? cubicAt(s, first.t) : lerp(s.p0, s.p3, first.t);
      out.push({ t: "L", p0: leave, p3: rejoin });
    } else if (k === second.idx) {
      out.push(
        s.t === "C"
          ? splitCubic(s, second.t)[1]
          : { t: "L", p0: cubicOrLine(s, second.t), p3: s.p3 },
      );
    } else if (k > second.idx) out.push(s);
  }
  return out;
}

const cubicOrLine = (s, t) => (s.t === "C" ? cubicAt(s, t) : lerp(s.p0, s.p3, t));

/** One side of an outline between two heights, oriented top-to-bottom.
 *
 *  Both sides of the cowl opening span the heights a block covers, so the side is CHOSEN by x
 *  rather than by whichever segment the walk reaches first -- picking the wrong one puts the
 *  trim on the far side of the opening, which draws a wedge across the whole figure. And the
 *  opening's outline runs upward in path order, so the piece is reversed to run downward,
 *  matching the direction the caller draws in. */
function edgeBetween(segs, yTop, yBottom, side) {
  const spans = [];
  for (const s of segs) {
    if (s.t !== "C") continue;
    const a = tAtY(s, yTop);
    const b = tAtY(s, yBottom);
    if (a === null || b === null) continue;
    const [t0, t1] = a < b ? [a, b] : [b, a];
    const piece = splitCubic(splitCubic(s, t1)[0], t0 / t1)[1];
    const mid = cubicAt(piece, 0.5)[0];
    spans.push({ piece, mid });
  }
  if (spans.length === 0) {
    throw new Error(`gen-mark: no segment spans y=${yTop}..${yBottom}`);
  }
  spans.sort((p, q) => p.mid - q.mid);
  const chosen = (side === "left" ? spans[0] : spans[spans.length - 1]).piece;
  // Downward: first point above the last.
  return cubicAt(chosen, 0)[1] <= cubicAt(chosen, 1)[1] ? chosen : reverseCubic(chosen);
}

const reverseCubic = (s) => ({ t: "C", p0: s.p3, p1: s.p2, p2: s.p1, p3: s.p0 });

/* ---------------------------------------------------------------- the flattening */

const server = await createServer({
  root: ROOT,
  logLevel: "warn",
  appType: "custom",
  server: { middlewareMode: true },
});
let brand;
try {
  brand = await server.ssrLoadModule("/src/brand/dissolve.ts");
} finally {
  await server.close();
}

const {
  DISSOLVE_HOOD_D,
  DISSOLVE_FACE_D,
  DISSOLVE_CUT,
  DISSOLVE_BLOCKS_UPPER,
  DISSOLVE_BLOCKS_LOWER,
} = brand;

const CUT_Y = DISSOLVE_CUT.y;
const hood = parsePath(DISSOLVE_HOOD_D);
const face = parsePath(DISSOLVE_FACE_D);

// 1. The head: hood above the cut, with the cowl opening as an evenodd hole. The opening lies
//    strictly inside the hood there, so no boolean is involved.
const headOuter = truncateAbove(hood, CUT_Y);
const headHole = truncateAbove(face, CUT_Y);
const HEAD_D = toPath(headOuter) + toPath(headHole);

// 2. Below the cut. A block is trimmed by the cowl opening only where it reaches into it; the
//    rest are rectangles. Which is which is DERIVED, never assumed -- the opening's edges are
//    sampled across each block's own height, so moving a block changes what comes out.
const blocks = [...DISSOLVE_BLOCKS_UPPER, ...DISSOLVE_BLOCKS_LOWER].map(([x, y, s]) => ({
  x,
  y,
  s,
  upper: DISSOLVE_BLOCKS_UPPER.some((b) => b[0] === x && b[1] === y && b[2] === s),
}));

/** The cowl opening's left and right edge x at a height, or null above/below the opening. */
function openingAt(y) {
  const xs = [];
  for (const s of face) {
    const t = tAtY(s, y);
    if (t !== null) xs.push(cubicOrLine(s, t)[0]);
  }
  if (xs.length < 2) return null;
  return [Math.min(...xs), Math.max(...xs)];
}

const pieces = [];
const notes = [];
for (const b of blocks) {
  // Only the upper blocks are cut by the opening: the scattered ones are laid down after it.
  const span = b.upper ? [openingAt(b.y + 0.001), openingAt(b.y + b.s - 0.001)] : [null, null];
  const left = span.filter(Boolean).map((o) => o[0]);
  const right = span.filter(Boolean).map((o) => o[1]);
  const opensLeft = left.length ? Math.min(...left) : Infinity;
  const opensRight = right.length ? Math.max(...right) : -Infinity;

  if (b.upper && b.x + b.s > opensLeft && b.x < opensRight) {
    if (b.x >= opensLeft && b.x + b.s <= opensRight) {
      notes.push(`  dropped ${JSON.stringify([b.x, b.y, b.s])}: wholly inside the cowl opening`);
      continue;
    }
    if (b.x < opensLeft) {
      // Trimmed on the right by the opening's left edge.
      const edge = edgeBetween(face, b.y, b.y + b.s, "left");
      const top = cubicAt(edge, 0);
      const bot = cubicAt(edge, 1);
      pieces.push(
        `M${n(b.x)} ${n(b.y)}L${n(top[0])} ${n(top[1])}` +
          `C${n(edge.p1[0])} ${n(edge.p1[1])} ${n(edge.p2[0])} ${n(edge.p2[1])} ${n(bot[0])} ${n(bot[1])}` +
          `L${n(b.x)} ${n(b.y + b.s)}Z`,
      );
      notes.push(`  trimmed ${JSON.stringify([b.x, b.y, b.s])} against the opening's left edge`);
      continue;
    }
  }
  pieces.push(`M${n(b.x)} ${n(b.y)}H${n(b.x + b.s)}V${n(b.y + b.s)}H${n(b.x)}Z`);
}

const BLOCKS_D = pieces.join("");

const banner = `// SPDX-License-Identifier: AGPL-3.0-or-later
//
// GENERATED by frontend/scripts/gen-mark.mjs (\`npm run mark\`). Do not edit.
//
// The finished shape of the brand mark, flattened from the recipe in ./dissolve.ts so that
// nothing has to compose it at render time -- which is what forced BrandMark's SVG \`mask\`, and
// a mask rasterizes at layout size, so the figure blurred under zoom while the eyes stayed
// sharp. Edit ./dissolve.ts and re-run the generator; \`appIcon.test.ts\` fails if these drift.
`;

await writeFile(
  OUT,
  `${banner}
/** The hood above the cut, with the cowl opening as a hole. Needs \`fill-rule="evenodd"\`. */
export const DISSOLVE_FIGURE_HEAD_D =
  "${HEAD_D}";

/** Everything below the cut, as one path. Needs the DEFAULT \`nonzero\` fill rule: blocks
 *  deliberately overlap so no renderer can seam the joins, and evenodd would hole the overlaps. */
export const DISSOLVE_FIGURE_BLOCKS_D =
  "${BLOCKS_D}";
`,
);

console.log("  src/brand/dissolve.generated.ts");
notes.forEach((l) => console.log(l));
console.log(`\n  head ${HEAD_D.length} chars, blocks ${BLOCKS_D.length} chars`);
