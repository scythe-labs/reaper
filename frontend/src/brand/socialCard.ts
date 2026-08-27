// SPDX-License-Identifier: AGPL-3.0-or-later
//
// The 1280x640 card GitHub shows when a link to the repository is shared. It is not an app
// asset: nothing imports it at runtime and nothing serves it. It exists so the card that
// represents Reaper is drawn from the same brand mark and the same evidence copy as the app,
// and so it can be redrawn when either moves.
//
// GitHub stores the uploaded image on its own servers and exposes no API for it, so shipping
// the PNG is not the delivery. A maintainer uploads it by hand, under Settings > General >
// Social preview. What is committed here is the source, which is the part that goes stale.
//
// `npm run social-card` rasterizes this through @resvg/resvg-js. socialCard.test.ts compares
// the committed PNG's recorded source hash to what this module returns now, so a mark or a
// string that changed without a regeneration fails the suite. Text is laid out by hand,
// because SVG does not wrap: every line break below is a decision, and changing a string
// usually means re-checking the line it sits on.
//
// The evidence rows are real gate output, quoted from engine/gates.py. Inventing plausible
// copy here is the failure this card is most exposed to, since a marketing image is written
// far from the code it describes. Anything shown here has to be greppable in gates.py.

import { appIconSvg } from "./appIcon";

export const SOCIAL_CARD_WIDTH = 1280;
export const SOCIAL_CARD_HEIGHT = 640;

/** The app's dark-theme tokens, from styles/00-tokens.css. Duplicated as literals because the
 *  card is rasterized outside the browser, where no custom property resolves. */
const BG = "#0e0f13";
const SURFACE = "#191b21";
const BORDER = "#2b2e37";
const TEXT = "#edeef1";
const MUTED = "#9ba1af";
const CONDEMN = "#ff8086";
const CONDEMN_SOFT = "#2c1618";
const PROTECT = "#5fce97";

/** Rasterized outside the browser, so `system-ui` resolves to nothing and the stack has to name
 *  real families. resvg takes the first one installed. */
const SANS = "Helvetica Neue, Helvetica, Arial, sans-serif";

/** The wordmark, spelled and weighted the way the app spells and weights it. A card-specific
 *  lockup (a different case or tracking than the app uses) would give the name a third
 *  spelling alongside the app and the README, and a mismatch between them reads as an error,
 *  not a style choice.
 *
 *  Literals, because this module is rasterized outside the browser and has no `node:fs`.
 *  `socialCard.test.ts` reads them back out of the app and fails naming the file, the same
 *  arrangement `CLEARED_ROWS` has with the en catalog. */
export const WORDMARK = {
  word: "Reaper",
  size: 47,
  weight: 700,
  /** The sign-in card's `-0.03em` at `size`, that card being the lockup this reproduces. */
  tracking: -1.41,
} as const;

/** Protections that were evaluated and did not hold the file, which is the block the app is
 *  built around and the one no comparable tool shows. Rows 1 and 2 are the active-session and
 *  popularity gates, row 3 the dormancy floor. It cannot quote the protected-list gate: lists
 *  protect through the operator's own keep rules now, whose copy is per-rule, not a gate
 *  sentence this card can quote.
 *
 *  `source` is the literal each row is quoted from in the English catalog's `why` entries
 *  (locales/en/ui.json, where the gates' sentences live), and socialCard.test.ts reads that
 *  file and fails, naming any row it can no longer find. A claim about what the app says,
 *  written this far from the copy that says it, drifts silently and in the flattering
 *  direction. `lines` is where it wraps, since SVG will not wrap it. */
export const CLEARED_ROWS: readonly { lines: readonly string[]; source: string }[] = [
  { lines: ["Nobody is watching it right now."], source: "Nobody is watching it right now." },
  {
    // "year" is the shipped window, filled into the catalog message's slot. The head is
    // the literal.
    lines: ["Nobody here watched it in the last year."],
    source: "Nobody here watched it in the last",
  },
  {
    lines: ["Unwatched for 5 years, 7 months, past the 3 years", "Reaper waits."],
    // The catalog message opens with slots, so only this tail is a literal there.
    source: "Reaper waits.",
  },
];

const escape = (s: string) => s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");

/** The mark, positioned. `appIconSvg` returns a whole document, which nests as-is. The fixed
 *  `clipPath` id it carries stays unique because the card embeds exactly one. A changed opening
 *  tag would otherwise drop the mark into the corner at full size, so this throws instead. */
function positionedMark(accent: string, x: number, y: number, size: number): string {
  const open = '<svg xmlns="http://www.w3.org/2000/svg"';
  const svg = appIconSvg(accent);
  if (!svg.startsWith(open)) {
    throw new Error("appIconSvg no longer opens with the tag socialCard.ts positions it by");
  }
  return `${open} x="${x}" y="${y}" width="${size}" height="${size}"${svg.slice(open.length)}`;
}

/** The card as a standalone SVG string, for a given accent. */
export function socialCardSvg(accent: string): string {
  const parts: string[] = [];
  const push = (s: string) => parts.push(s);

  push(
    `<svg xmlns="http://www.w3.org/2000/svg" width="${SOCIAL_CARD_WIDTH}" ` +
      `height="${SOCIAL_CARD_HEIGHT}" viewBox="0 0 ${SOCIAL_CARD_WIDTH} ${SOCIAL_CARD_HEIGHT}">`,
  );

  push(
    `<defs>` +
      `<linearGradient id="bar" x1="0" y1="0" x2="1" y2="0">` +
      `<stop offset="0" stop-color="${accent}"/>` +
      `<stop offset="0.78" stop-color="${accent}" stop-opacity="0"/>` +
      `</linearGradient>` +
      `<radialGradient id="bloom">` +
      `<stop offset="0" stop-color="${accent}" stop-opacity="0.16"/>` +
      `<stop offset="0.72" stop-color="${accent}" stop-opacity="0"/>` +
      `</radialGradient>` +
      `</defs>`,
  );

  push(`<rect width="${SOCIAL_CARD_WIDTH}" height="${SOCIAL_CARD_HEIGHT}" fill="${BG}"/>`);
  push(`<ellipse cx="190" cy="610" rx="390" ry="330" fill="url(#bloom)"/>`);
  push(`<rect width="${SOCIAL_CARD_WIDTH}" height="5" fill="url(#bar)"/>`);

  // Left column: mark, wordmark, headline, tagline, the services it talks to.
  push(positionedMark(accent, 72, 84, 86));
  push(
    `<text x="178" y="143" font-family="${SANS}" font-size="${WORDMARK.size}" ` +
      `font-weight="${WORDMARK.weight}" letter-spacing="${WORDMARK.tracking}" ` +
      `fill="${TEXT}">${escape(WORDMARK.word)}</text>`,
  );

  const headline = [
    { text: "Find what nobody", y: 253, fill: TEXT },
    { text: "watches.", y: 313, fill: TEXT },
    { text: "See why.", y: 373, fill: accent },
  ];
  for (const line of headline) {
    push(
      `<text x="72" y="${line.y}" font-family="${SANS}" font-size="53" font-weight="700" ` +
        `letter-spacing="-1" fill="${line.fill}">${escape(line.text)}</text>`,
    );
  }

  const tagline = [
    "Explainable media library pruning for Plex,",
    "removed safely through Sonarr and Radarr.",
  ];
  tagline.forEach((line, i) => {
    push(
      `<text x="72" y="${437 + i * 36}" font-family="${SANS}" font-size="25" fill="${MUTED}">` +
        `${escape(line)}</text>`,
    );
  });

  // Placed one by one because SVG collapses runs of whitespace, so a padded string renders as
  // single spaces. Both columns are measured off the rendered card rather than computed: there
  // is no text metric here to compute them from. The dots are decoration between chips only,
  // never a separator carrying a fact.
  const services: readonly [string, number][] = [
    ["Plex", 72],
    ["Sonarr", 141],
    ["Radarr", 231],
    ["Tautulli", 321],
    ["Seerr", 415],
  ];
  const dots = [124, 214, 304, 398];
  for (const [label, x] of services) {
    push(
      `<text x="${x}" y="551" font-family="${SANS}" font-size="20" fill="${MUTED}">${label}</text>`,
    );
  }
  for (const x of dots) {
    push(`<text x="${x}" y="551" font-family="${SANS}" font-size="13" fill="${BORDER}">·</text>`);
  }

  // Right column: one review-queue verdict, drawn the way the why panel draws it.
  const px = 696;
  const py = 148;
  const pw = 512;
  const ph = 344;
  push(
    `<rect x="${px}" y="${py}" width="${pw}" height="${ph}" rx="16" fill="${SURFACE}" ` +
      `stroke="${BORDER}"/>`,
  );

  push(
    `<rect x="${px + 30}" y="${py + 32}" width="137" height="33" rx="16.5" ` +
      `fill="${CONDEMN_SOFT}" stroke="${CONDEMN}" stroke-opacity="0.34"/>`,
  );
  push(
    `<text x="${px + 46}" y="${py + 54}" font-family="${SANS}" font-size="14" font-weight="700" ` +
      `letter-spacing="1.1" fill="${CONDEMN}">CONDEMNED</text>`,
  );
  push(
    `<text x="${px + pw - 30}" y="${py + 58}" font-family="${SANS}" text-anchor="end" ` +
      `font-size="26" font-weight="700" fill="${TEXT}">55<tspan dx="5" font-size="16" ` +
      `font-weight="400" fill="${MUTED}">/100</tspan></text>`,
  );
  push(
    `<line x1="${px + 30}" y1="${py + 88}" x2="${px + pw - 30}" y2="${py + 88}" ` +
      `stroke="${BORDER}"/>`,
  );

  push(
    `<text x="${px + 30}" y="${py + 126}" font-family="${SANS}" font-size="14" ` +
      `font-weight="650" letter-spacing="1" fill="${MUTED}">PROTECTIONS IT CLEARED</text>`,
  );
  push(
    `<text x="${px + 30}" y="${py + 155}" font-family="${SANS}" font-size="16" fill="${MUTED}">` +
      `The numbers are the ones it actually used.</text>`,
  );

  let rowY = py + 200;
  for (const { lines } of CLEARED_ROWS) {
    push(
      `<text x="${px + 30}" y="${rowY}" font-family="${SANS}" font-size="19" fill="${PROTECT}">` +
        `✓</text>`,
    );
    lines.forEach((line, i) => {
      push(
        `<text x="${px + 59}" y="${rowY + i * 25}" font-family="${SANS}" font-size="17.5" ` +
          `fill="${MUTED}">${escape(line)}</text>`,
      );
    });
    rowY += lines.length > 1 ? 65 : 40;
  }

  push(`</svg>`);
  return parts.join("");
}
