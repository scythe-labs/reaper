// SPDX-License-Identifier: AGPL-3.0-or-later
// @vitest-environment node
//
// The queue's card-head rules stay on the queue's show card.
//
// The Policy page's protection cards use the same `card-head` class for their header row. A queue
// rule on a bare `.card-head` reaches those cards too, so every queue selector names the show card
// first.

import { describe, expect, it } from "vitest";

import { blocksOf, siteOf } from "./test/stylesheet";

const QUEUE_FILES = [
  "styles/21-queue-cards.css",
  "styles/24-overrides.css",
  "styles/25-select-mode.css",
];

/** The top-level members of a selector list. A comma inside `:has()` or `:not()` stays put. */
function members(selector: string): string[] {
  const out: string[] = [];
  let depth = 0;
  let start = 0;
  for (let at = 0; at < selector.length; at++) {
    const ch = selector[at];
    if (ch === "(") depth++;
    else if (ch === ")") depth--;
    else if (ch === "," && depth === 0) {
      out.push(selector.slice(start, at).trim());
      start = at + 1;
    }
  }
  out.push(selector.slice(start).trim());
  return out;
}

/** Whether one selector's leading compound carries the `card-head` class.
 *
 *  Parenthesized arguments are blanked first, so a combinator inside `:has()` does not end the
 *  compound and a class named inside it does not count. */
function startsBare(selector: string): boolean {
  let flat = selector;
  while (/\([^()]*\)/.test(flat)) flat = flat.replace(/\([^()]*\)/g, "");
  const lead = flat.trim().split(/[\s>+~]/)[0] ?? "";
  return /\.card-head(?![\w-])/.test(lead);
}

/** Every selector in the three queue files, one per list member, with its site. */
function queueSelectors(): { selector: string; site: string }[] {
  return blocksOf()
    .map((b) => ({ selector: b.selector, site: siteOf(b.at) }))
    .filter((b) => QUEUE_FILES.some((f) => b.site.startsWith(`${f}:`)))
    .flatMap((b) => members(b.selector).map((selector) => ({ selector, site: b.site })));
}

describe("the bare card-head matcher", () => {
  it.each([
    ".card-head",
    ".card-head:hover",
    ".clickable.card-head",
    ".card-head > .card-side",
    ".card-head:has(> .card-body .card-open)",
  ])("reads %s as bare", (selector) => {
    expect(startsBare(selector)).toBe(true);
  });

  it.each([
    ".card-show > .card-head",
    ".card-list-selecting .card-head",
    ".card-header",
    ".card-side:has(> .card-head)",
  ])("reads %s as scoped", (selector) => {
    expect(startsBare(selector)).toBe(false);
  });

  it("splits a list on its top-level commas only", () => {
    expect(members(".a:has(.b, .c), .card-head")).toEqual([".a:has(.b, .c)", ".card-head"]);
  });
});

describe("the queue's card-head selectors", () => {
  it("never start with a bare .card-head", () => {
    const all = queueSelectors();
    // Every selector in the three files: 61, 72 and 57. A rule added there moves this.
    expect(all).toHaveLength(190);
    // Twelve scoped to the show card, and the select-mode cursor under `.card-list-selecting`.
    expect(all.filter((s) => /\.card-head(?![\w-])/.test(s.selector))).toHaveLength(13);

    const bare = all.filter((s) => startsBare(s.selector)).map((s) => `${s.site} ${s.selector}`);
    expect(bare).toEqual([]);
  });
});
