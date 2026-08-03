// SPDX-License-Identifier: AGPL-3.0-or-later
//
// The scales hold, and the iOS zoom floor cannot be outranked.
//
// Every one of these guards a claim that is otherwise only a comment. The type and weight
// scales are worth nothing the moment somebody writes a literal beside them -- that is exactly
// how there came to be 38 sizes and 12 weights -- and the drift is invisible in review because
// one `font-size: 0.83rem` reads perfectly reasonable on its own line.
//
// The last one is the sharpest: `#root input { font-size: 16px }` in 01-base.css is the only
// thing standing between a phone operator and Safari zooming the page every time they focus a
// field, which unpins the fixed app shell and leaves it panning. Its comment claims the ID
// "covers future inputs" and until this file nothing checked that -- a reassuring sentence with
// no gate under it, on a bug that has already been fixed once (rule 144).

import { describe, expect, it } from "vitest";

import { CSS, FILES, siteOf } from "./test/stylesheet";

/** Comments stripped, so prose quoting a value is not read as a declaration. */
const CODE = CSS.replace(/\/\*[\s\S]*?\*\//g, (m) => m.replace(/[^\n]/g, " "));

/** Every `--name: value` on a :root rule, labeled with the at-rule wrapping it.
 *
 *  The dark palette lives at `@media (prefers-color-scheme: dark) { :root { … } }`, so the
 *  rule's own selector is a bare `:root` and says nothing about which theme it is. What
 *  distinguishes the three blocks is the context, and a matcher blind to it collapses them into
 *  one and then agrees with itself (rule 147). */
function rootBlocks(): { label: string; names: Set<string> }[] {
  const out: { label: string; names: Set<string> }[] = [];
  let context = "";
  let depth = 0;
  for (const m of CODE.matchAll(/([^{}]*)\{|\}/g)) {
    if (m[0] === "}") {
      depth--;
      if (depth === 0) context = "";
      continue;
    }
    const head = (m[1] ?? "").trim().replace(/\s+/g, " ");
    depth++;
    if (head.startsWith("@")) {
      context = head;
      continue;
    }
    if (!head.includes(":root")) continue;
    const bodyStart = (m.index ?? 0) + m[0].length;
    const body = CODE.slice(bodyStart, CODE.indexOf("}", bodyStart));
    const names = new Set<string>();
    for (const d of body.split(";")) {
      const name = d.split(":")[0]?.trim();
      if (name?.startsWith("--")) names.add(name);
    }
    out.push({ label: context ? `${context} { ${head} }` : head, names });
  }
  return out;
}

/** (id, class, type) counts, the three numbers CSS compares in order. */
function specificity(sel: string): [number, number, number] {
  const s = sel.replace(/\s*[>+~]\s*/g, " ").trim();
  const ids = (s.match(/#[\w-]+/g) ?? []).length;
  const classes = (s.match(/\.[\w-]+|\[[^\]]+\]|:(?!:)(?!not\b)[\w-]+(\([^)]*\))?/g) ?? []).length;
  const types = (s.match(/(^|\s)[a-z][\w-]*/g) ?? []).length;
  // `:not(...)` contributes its argument's specificity, not its own.
  let extra: [number, number, number] = [0, 0, 0];
  for (const n of s.matchAll(/:not\(([^)]*)\)/g)) {
    const inner = specificity(n[1] ?? "");
    extra = [extra[0] + inner[0], extra[1] + inner[1], extra[2] + inner[2]];
  }
  return [ids + extra[0], classes + extra[1], types + extra[2]];
}

function outranks(a: [number, number, number], b: [number, number, number]): boolean {
  for (let i = 0; i < 3; i++) {
    if ((a[i] ?? 0) !== (b[i] ?? 0)) return (a[i] ?? 0) > (b[i] ?? 0);
  }
  return false;
}

/** Every declaration of `prop`, as [selector, value, offset]. */
function declarationsOf(prop: string): [string, string, number][] {
  const out: [string, string, number][] = [];
  for (const m of CODE.matchAll(/([^{}]+)\{([^{}]*)\}/g)) {
    const selector = (m[1] ?? "").trim().replace(/\s+/g, " ");
    if (selector.startsWith("@")) continue;
    const body = m[2] ?? "";
    const bodyAt = (m.index ?? 0) + (m[1] ?? "").length + 1;
    for (const d of body.matchAll(new RegExp(`(^|;)\\s*${prop}\\s*:([^;]*)`, "g"))) {
      out.push([selector, (d[2] ?? "").trim(), bodyAt + (d.index ?? 0)]);
    }
  }
  return out;
}

describe("the type and weight scales", () => {
  it("has no font-size written as a bare rem literal", () => {
    // A literal here is not a smaller version of a token; it is the drift itself. The one
    // allowed non-token is `0.85em` on `code`, which is relative to whatever it sits inside --
    // a rem step would change what it means -- and the 16px iOS floor below.
    const stray = declarationsOf("font-size")
      .filter(([sel]) => !sel.includes(":root"))
      .filter(([, v]) => /[0-9]rem\b/.test(v))
      .map(([sel, v, at]) => `${siteOf(at)}  ${sel} { font-size: ${v} }`);
    expect(stray).toEqual([]);
  });

  it("has no font-weight written as a bare number", () => {
    const stray = declarationsOf("font-weight")
      .filter(([sel]) => !sel.includes(":root"))
      .filter(([, v]) => /^[0-9]+$/.test(v))
      .map(([sel, v, at]) => `${siteOf(at)}  ${sel} { font-weight: ${v} }`);
    expect(stray).toEqual([]);
  });

  it("declares every step the stylesheet reaches for", () => {
    const declared = new Set(
      [...CODE.matchAll(/(--(?:text|weight|space)-[a-z0-9]+)\s*:/g)].map((m) => m[1]),
    );
    const used = new Set(
      [...CODE.matchAll(/var\((--(?:text|weight|space)-[a-z0-9]+)/g)].map((m) => m[1]),
    );
    // An undefined custom property is rule 18's blocker and fails silently: the declaration is
    // dropped and the element inherits, which looks like a layout bug three files away.
    expect([...used].filter((u) => !declared.has(u ?? ""))).toEqual([]);
  });
});

describe("the space scale", () => {
  // Spacing here is a continuum: 0.3 through 0.6rem are all heavily used and 0.8px apart. So the
  // tokens went in only where a declaration already sat exactly on a step and nothing moved, and
  // the rest are still literals. This ratchet is what makes those a debt: snapping one to its
  // nearest step is a visual change wanting an eye on it, a file at a time, and the number comes
  // down each time that happens.
  //
  // It may only ever fall. Raising it means new drift was written, which the scale exists to
  // stop.
  //
  // Take the number by running this test. Seeding it from the migration script's count left 173
  // declarations of slack -- that script counted every declaration it declined to rewrite,
  // including the ones holding no rem at all -- and three freshly-written off-scale values then
  // changed nothing, so the ratchet sat green through a mutation planted to break it.
  const OFF_SCALE_CEILING = 294;

  it("does not grow the number of off-scale spacing literals", () => {
    const STEPS = [0.15, 0.25, 0.35, 0.5, 0.6, 0.7, 0.85, 1, 1.25, 1.5, 2];
    let offScale = 0;
    for (const prop of ["gap", "row-gap", "column-gap", "padding", "margin"]) {
      for (const [sel, value] of declarationsOf(prop)) {
        if (sel.includes(":root") || /var\(|calc\(|env\(/.test(value)) continue;
        const rems = [...value.matchAll(/([0-9]*\.?[0-9]+)rem/g)].map((m) => Number(m[1]));
        if (rems.length === 0) continue;
        if (rems.some((v) => !STEPS.some((s) => Math.abs(s - v) < 1e-9))) offScale++;
      }
    }
    expect(offScale).toBeLessThanOrEqual(OFF_SCALE_CEILING);
  });
});

describe("the theme blocks", () => {
  it("declare the same token set in all three", () => {
    // The palette is stated three times -- the prefers-color-scheme block and both data-theme
    // blocks -- because the attribute selector has to outrank the media query in BOTH
    // directions, which is what lets "Light" win on a dark device. A token added to one and
    // forgotten in another does not fail: it falls through to the :root default, so the app
    // renders a light-mode color on a dark page for exactly the one value nobody re-stated.
    // The file's own comment asks for the mirroring; this is what checks it.
    const blocks = rootBlocks();
    const themed = blocks.filter((b) =>
      /prefers-color-scheme: dark|data-theme="(dark|light)"/.test(b.label),
    );
    // Three: the media query, and one forced block per theme. Pinned as a count so a block
    // that stops matching the walk fails here rather than quietly leaving the comparison.
    expect(themed.map((b) => b.label)).toHaveLength(3);

    const union = new Set(themed.flatMap((b) => [...b.names]));
    const missing = themed.flatMap((b) =>
      [...union].filter((n) => !b.names.has(n)).map((n) => `${b.label} is missing ${n}`),
    );
    expect(missing).toEqual([]);
  });
});

describe("the iOS no-zoom floor", () => {
  // Safari zooms the page whenever a focused field is under 16px, and that zoom shrinks the
  // visual viewport below the layout width, which unpins the fixed app shell and leaves it
  // panning. 01-base.css holds the line for every control on a touch pointer.
  const FLOOR_PX = 16;

  it("is declared, on a coarse pointer, at the floor or above", () => {
    const guard = declarationsOf("font-size").filter(([sel]) => sel.includes("#root input"));
    expect(guard.length).toBeGreaterThan(0);
    for (const [, value] of guard) {
      expect(Number(value.replace("px", ""))).toBeGreaterThanOrEqual(FLOOR_PX);
    }
    expect(CODE).toMatch(/@media\s*\(\s*pointer:\s*coarse\s*\)/);
  });

  it("cannot be outranked by any other rule sizing a field", () => {
    // The guard wins on being an ID selector while every control rule is class-based: an ID
    // beats any number of classes, so file order and media queries are both irrelevant and it
    // can sit in the FIRST of 34 files and still beat the last. A future rule carrying an ID,
    // or a second `#root` qualifier, would take a field back under the floor in silence. This
    // is the half a reader cannot check by eye.
    const guard = declarationsOf("font-size").find(([sel]) => sel.includes("#root input"));
    expect(guard).toBeDefined();
    const guardSpec = specificity((guard as [string, string, number])[0].split(",")[0] ?? "");

    const beats: string[] = [];
    for (const [selector, value, at] of declarationsOf("font-size")) {
      if (selector.includes("#root input")) continue;
      for (const part of selector.split(",")) {
        if (!/\b(input|select|textarea)\b/.test(part)) continue;
        if (outranks(specificity(part), guardSpec)) {
          beats.push(`${siteOf(at)}  ${part.trim()} { font-size: ${value} }`);
        }
      }
    }
    expect(beats).toEqual([]);
  });

  it("names every field the React root does not contain", () => {
    // The guard is scoped to `#root`, so anything portaled to <body> is outside it and has to
    // be named alongside. Exactly one component portals today -- the spare-length menu -- and
    // it is named. A second portal that renders a field would be silently uncovered, so this
    // pins the count rather than the name (rule 145): a new portal fails here and has to be
    // looked at, whether or not it turns out to hold an input.
    const guard = declarationsOf("font-size").find(([sel]) => sel.includes("#root input"));
    expect((guard as [string, string, number])[0]).toContain(".dur-menu input");
  });
});

describe("the stylesheet walk itself", () => {
  it("reads every file the barrel loads", () => {
    // Rule 118's shape for a scanner: these guards are worth nothing if the text they scan is
    // empty, and a walk that reads nothing passes every assertion above.
    expect(FILES.length).toBeGreaterThan(30);
    expect(CODE.length).toBeGreaterThan(200_000);
    expect(declarationsOf("font-size").length).toBeGreaterThan(250);
  });
});
