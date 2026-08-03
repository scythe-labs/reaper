// SPDX-License-Identifier: AGPL-3.0-or-later
//
// .github/social-preview.png is a snapshot of socialCardSvg at the default accent, produced by
// `npm run social-card`. This guards it against two different kinds of drift.
//
// The first is the one gen-icons.mjs already answers for the app icons: the generator records
// the sha256 of the exact SVG string it rasterized, and the check below re-derives that string
// from the CURRENT drawing, so a mark or a string that moved without a regeneration fails.
// The rasterizer is not invoked here, for the reason appIcon.test.ts gives and one more: this
// card has text, so its bytes depend on which fonts the generating machine had, and CI has no
// business reproducing that.
//
// The second is specific to this asset. The card quotes gate output as evidence, and it is the
// furthest thing in the tree from the code it quotes -- nobody editing gates.py opens a
// marketing image. README.md carries an example of exactly this copy that no gate has ever
// produced (#419), which is the failure this file exists to keep off the card: every row
// declares the literal it came from, and the walk below reads gates.py and fails naming any row
// that is no longer there. Rule 144.

import { type BinaryLike, createHash } from "node:crypto";
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

import { describe, expect, it } from "vitest";

import { DEFAULT_ACCENT } from "../accent";
import { BRAND_WEIGHT, BRAND_WORD, signInCardSpellsTheWord, trackingAt } from "../test/appLockup";
import {
  CLEARED_ROWS,
  SOCIAL_CARD_HEIGHT,
  SOCIAL_CARD_WIDTH,
  WORDMARK,
  socialCardSvg,
} from "./socialCard";
import manifest from "./socialCard.generated.json";

const here = dirname(fileURLToPath(import.meta.url));
const repoDir = join(here, "..", "..", "..");
const cardPath = join(repoDir, ".github", "social-preview.png");

const sha256 = (data: BinaryLike) => createHash("sha256").update(data).digest("hex");

/** A PNG's pixel size from its IHDR chunk: two big-endian uint32s after the 8-byte signature,
 *  the 4-byte length and the "IHDR" tag. No image library needed. */
function pngSize(buf: Buffer): { width: number; height: number } {
  return { width: buf.readUInt32BE(16), height: buf.readUInt32BE(20) };
}

describe("social preview card", () => {
  it("the committed PNG was generated from the CURRENT drawing", () => {
    expect(
      sha256(socialCardSvg(DEFAULT_ACCENT)),
      "card is stale: re-run `npm run social-card`",
    ).toBe(manifest.sourceSha256);
  });

  it("the committed PNG matches its recorded bytes", () => {
    expect(sha256(readFileSync(cardPath)), "social-preview.png was edited by hand").toBe(
      manifest.sha256,
    );
  });

  it("is the size and weight GitHub accepts for a social preview", () => {
    const buf = readFileSync(cardPath);
    // GitHub renders at 1280x640 and rejects an upload over 1 MB. The weight is the one that
    // turns a regeneration into a card that cannot be uploaded at all.
    expect(pngSize(buf)).toEqual({ width: SOCIAL_CARD_WIDTH, height: SOCIAL_CARD_HEIGHT });
    expect(SOCIAL_CARD_WIDTH / SOCIAL_CARD_HEIGHT).toBe(2);
    expect(buf.length).toBeLessThan(1_000_000);
    expect(buf.length).toBe(manifest.bytes);
  });

  it("every evidence row on the card is copy a gate actually produces", () => {
    const gates = readFileSync(join(repoDir, "src", "reaper", "engine", "gates.py"), "utf8");
    // Pin the population the walk covers, not just that each member it found passes: a row
    // dropped from CLEARED_ROWS leaves both the card and this check at once (rule 145).
    expect(CLEARED_ROWS.length).toBe(3);
    for (const row of CLEARED_ROWS) {
      expect(
        gates.includes(row.source),
        `"${row.source}" is on the social card but no longer in engine/gates.py`,
      ).toBe(true);
      // The wrapped lines are the same sentence the gate emits, split for the SVG. Joining them
      // has to reproduce the quoted literal, or the card is showing something else.
      expect(row.lines.join(" ")).toContain(row.source);
    }
  });

  it("writes the name the way the app writes it", () => {
    // The card carried its own lockup until #426 -- caps, tracked out -- and was the last
    // surface spelling the name a third way. These are literals in `socialCard.ts` because a
    // module the rasterizer loads has no `node:fs`, so the agreement is checked here instead,
    // against the app itself (rule 144, the same arrangement CLEARED_ROWS has with gates.py).
    expect(WORDMARK.word, "the masthead's brand word moved; the card copies it").toBe(BRAND_WORD);
    expect(WORDMARK.weight, "the weight scale moved under the card").toBe(BRAND_WEIGHT);
    // SVG letter-spacing is a length, so the app's em value is resolved at the card's size.
    expect(WORDMARK.tracking, "the sign-in card's tracking moved under this card").toBe(
      trackingAt(WORDMARK.size),
    );
    expect(signInCardSpellsTheWord(), "the sign-in card no longer spells it this way").toBe(true);
    expect(socialCardSvg(DEFAULT_ACCENT)).toContain(`>${WORDMARK.word}</text>`);
  });

  it("draws the mark at the size the layout reserves for it", () => {
    const svg = socialCardSvg(DEFAULT_ACCENT);
    expect(svg).toContain('x="72" y="84" width="86" height="86"');
    // The eyes follow the accent, so the card tracks whatever the app's default is.
    expect(svg).toContain(DEFAULT_ACCENT);
  });
});
