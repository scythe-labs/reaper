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
// So the composition happens ONCE, here, and what ships is geometry: one path, on the default
// nonzero rule. The silhouette is a single closed contour -- the hood, with the cowl opening
// spliced into its bottom edge as a notch and the two blocks that meet the cut spliced in as
// detours -- followed by the remaining blocks, which only ever overlap what is already drawn.
//
// One contour, because an edge shared between two outlines is the one thing a rasterizer is
// free to disagree about, and two of them disagreed. The opening used to be an evenodd hole
// whose bottom edge lay exactly on the hood's, and the blocks used to abut the hood from a
// second path: the first showed on iOS as a faint LIGHT line across an opening that is empty,
// the second as a DARK line under the shoulders, each only at the zoom levels where the cut
// fell between two device pixels. Nothing shares an edge now, and no fill rule is needed.
//
// The only geometry operations involved are splitting a cubic at a parameter (de Casteljau) and
// ordering x along a horizontal line. There is no boolean here and none is needed, which is
// worth stating because it looks like there should be: everything that meets the cut meets it
// on the hood's own straight bottom edge, so splicing by x unions them exactly; below the cut,
// the right-hand and scattered blocks sit clear of the cavity, and only the left column is
// trimmed -- by a single bezier segment.
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

const reverseSeg = (s) =>
  s.t === "L" ? { t: "L", p0: s.p3, p3: s.p0 } : { t: "C", p0: s.p3, p1: s.p2, p2: s.p1, p3: s.p0 };

/** The index of the straight edge `truncateAbove` closed an outline off with, along `y`. Exactly
 *  one is expected: a second edge lying on the cut would mean the outline touches it somewhere
 *  else too, and splicing into the wrong one would turn the figure inside out. */
function bottomEdge(segs, y) {
  const found = segs.flatMap((s, i) =>
    s.t === "L" && Math.abs(s.p0[1] - y) < 1e-6 && Math.abs(s.p3[1] - y) < 1e-6 ? [i] : [],
  );
  if (found.length !== 1) {
    throw new Error(`gen-mark: ${found.length} edges lie along y=${y}, expected 1`);
  }
  return found[0];
}

/** A closed outline reopened into a walk between its two corners on the cut, with the edge along
 *  the cut itself dropped. Oriented to run from the higher-x corner to the lower-x one, which is
 *  the direction the hood's own bottom edge is walked, so the piece splices straight in. */
function excursion(segs, idx) {
  const rotated = [...segs.slice(idx + 1), ...segs.slice(0, idx)];
  const walk =
    segs[idx].p0[0] > segs[idx].p3[0] ? rotated.reverse().map(reverseSeg) : rotated.slice();
  return { start: walk[0].p0, end: walk[walk.length - 1].p3, segs: walk };
}

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

// Below the cut. A block is trimmed by the cowl opening only where it reaches into it; the
// rest are rectangles. Which is which is DERIVED, never assumed -- the opening's edges are
// sampled across each block's own height, so moving a block changes what comes out.
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

/** A block's closed boundary, clockwise, with its TOP edge first. Splitting it out that way is
 *  what lets a block that meets the cut be reopened into a detour: drop the top edge and the
 *  rest is already a walk from the block's top-right corner around to its top-left one. */
function blockBoundary(b, trimmed) {
  const { x, y, s } = b;
  if (!trimmed) {
    return [
      { t: "L", p0: [x, y], p3: [x + s, y] },
      { t: "L", p0: [x + s, y], p3: [x + s, y + s] },
      { t: "L", p0: [x + s, y + s], p3: [x, y + s] },
      { t: "L", p0: [x, y + s], p3: [x, y] },
    ];
  }
  const edge = edgeBetween(face, y, y + s, "left");
  const top = cubicAt(edge, 0);
  const bot = cubicAt(edge, 1);
  return [
    { t: "L", p0: [x, y], p3: top },
    { t: "C", p0: top, p1: edge.p1, p2: edge.p2, p3: bot },
    { t: "L", p0: bot, p3: [x, y + s] },
    { t: "L", p0: [x, y + s], p3: [x, y] },
  ];
}

const pieces = [];
const detours = [];
const notes = [];
for (const b of blocks) {
  // Only the upper blocks are cut by the opening: the scattered ones are laid down after it.
  const span = b.upper ? [openingAt(b.y + 0.001), openingAt(b.y + b.s - 0.001)] : [null, null];
  const left = span.filter(Boolean).map((o) => o[0]);
  const right = span.filter(Boolean).map((o) => o[1]);
  const opensLeft = left.length ? Math.min(...left) : Infinity;
  const opensRight = right.length ? Math.max(...right) : -Infinity;

  let trimmed = false;
  if (b.upper && b.x + b.s > opensLeft && b.x < opensRight) {
    if (b.x >= opensLeft && b.x + b.s <= opensRight) {
      notes.push(`  dropped ${JSON.stringify([b.x, b.y, b.s])}: wholly inside the cowl opening`);
      continue;
    }
    // Trimmed on the right by the opening's left edge.
    trimmed = b.x < opensLeft;
  }

  // A block whose top edge lies ON the cut is not a separate shape at all: it is the hood
  // continuing downward. Hand it to the splice below as a detour rather than drawing it beside
  // the hood, because two shapes that share an edge are antialiased independently and
  // composited one over the other, which leaves a hairline of the background between them.
  if (b.y === CUT_Y) {
    const boundary = blockBoundary(b, trimmed);
    detours.push({ start: boundary[0].p3, end: boundary[0].p0, segs: boundary.slice(1) });
    notes.push(`  joined ${JSON.stringify([b.x, b.y, b.s])} into the hood: it meets the cut`);
    continue;
  }
  if (trimmed) {
    // The closing edge is dropped: `toPath` closes with Z.
    pieces.push(toPath(blockBoundary(b, true).slice(0, -1)));
    notes.push(`  trimmed ${JSON.stringify([b.x, b.y, b.s])} against the opening's left edge`);
    continue;
  }
  pieces.push(`M${n(b.x)} ${n(b.y)}H${n(b.x + b.s)}V${n(b.y + b.s)}H${n(b.x)}Z`);
}

// The figure as ONE closed contour, plus the blocks that touch nothing but each other.
//
// The hood's bottom edge is a single straight line along the cut, and everything that meets the
// cut meets it there: the cowl opening reaches it from above, the two blocks below it reach it
// from below. So each is spliced into that edge as a detour and the whole silhouette closes in
// one walk -- which needs no boolean, only the ordering of x along a horizontal line.
//
// That is the difference between a shape and a stack of shapes. Before this, the opening was an
// evenodd hole whose bottom edge lay exactly on top of the hood's, and the blocks abutted the
// hood from a second path; both are edges shared between two outlines, and a rasterizer is free
// to disagree about them. It did: the shared bottom edge showed as a faint LIGHT line across
// the opening on iOS, and the abutting blocks as a DARK one, each appearing only at the zoom
// levels where the cut fell between two device pixels. A single outline has no shared edge to
// disagree about, and needs no fill rule either.
const outer = truncateAbove(hood, CUT_Y);
const bottomIdx = bottomEdge(outer, CUT_Y);
const bottom = outer[bottomIdx];
if (bottom.p0[0] <= bottom.p3[0]) {
  throw new Error("gen-mark: the hood's cut edge is walked left to right, not right to left");
}

const openingSegs = truncateAbove(face, CUT_Y);
detours.push(excursion(openingSegs, bottomEdge(openingSegs, CUT_Y)));
detours.sort((p, q) => q.start[0] - p.start[0]);

const spliced = [];
let cur = bottom.p0;
for (const d of detours) {
  if (d.start[0] > cur[0] + 1e-9 || d.end[0] > d.start[0] + 1e-9) {
    throw new Error(`gen-mark: detours along y=${CUT_Y} overlap or run backwards`);
  }
  if (Math.abs(d.start[0] - cur[0]) > 1e-9) spliced.push({ t: "L", p0: cur, p3: d.start });
  spliced.push(...d.segs);
  cur = d.end;
}
if (cur[0] < bottom.p3[0] - 1e-9) {
  throw new Error(`gen-mark: a detour ran past the end of the cut edge`);
}
if (Math.abs(cur[0] - bottom.p3[0]) > 1e-9) spliced.push({ t: "L", p0: cur, p3: bottom.p3 });

const figure = [...outer.slice(0, bottomIdx), ...spliced, ...outer.slice(bottomIdx + 1)];
const FIGURE_D = toPath(figure) + pieces.join("");

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
/** The whole figure as one shape, for the DEFAULT \`nonzero\` fill rule.
 *
 *  The silhouette -- hood, cowl opening, and the two blocks that carry it past the cut -- is a
 *  single closed contour, so nothing in it shares an edge with anything else. The remaining
 *  blocks follow as their own contours and only ever OVERLAP what is already drawn, which
 *  nonzero unions and evenodd would turn into holes. Do not add a fill rule: an edge shared
 *  between two outlines is what a rasterizer seams, and there is no longer one to seam. */
export const DISSOLVE_FIGURE_D =
  "${FIGURE_D}";
`,
);

console.log("  src/brand/dissolve.generated.ts");
notes.forEach((l) => console.log(l));
console.log(`\n  figure ${FIGURE_D.length} chars, ${pieces.length} loose blocks`);
