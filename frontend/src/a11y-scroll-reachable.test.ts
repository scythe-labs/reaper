// SPDX-License-Identifier: AGPL-3.0-or-later
//
// A container that scrolls is reachable from a keyboard, or it is recorded here as one that
// does not need to be (WCAG 2.1.1).
//
// A `overflow: auto` box with nothing focusable inside it cannot be scrolled by keyboard at
// all. There is no key that scrolls "the thing under the mouse." The arrow keys scroll
// whatever holds focus, so a pane of pure prose, a log of `<span>` rows, or a table too wide
// for the screen is unreadable past its first screenful unless something in it can take focus.
//
// **This is a test rather than a rule because a rule cannot see a new one arrive.** The
// population is discovered from the stylesheet, not from a list somebody remembers to extend,
// so adding an `overflow-y: auto` anywhere fails this file until the author says which of the
// two states the new box is in. The decision is cheap while the markup is in front of you
// and expensive to reconstruct later.
//
// **What it can and cannot do** (a check that cannot discriminate must not read as a proof):
//   - It DOES pin the population, so a new scroll container cannot ship unclassified, and a
//     renamed or deleted one fails rather than dropping out of the walk.
//   - It DOES read the real opening tag for a `reachable` site, brace-aware, so `tabIndex={0}`
//     being deleted or moved to a different element fails.
//   - It does NOT prove an `exempt` site really holds a focusable child in every state it can
//     render. The `why` string is a human argument, and someone still has to re-read it.
//   - It does NOT know whether a focusable container has a sensible accessible name. The axe
//     audits each panel carries cover that.
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

import { describe, expect, it } from "vitest";

import { CSS, siteOf } from "./test/stylesheet";

const HERE = dirname(fileURLToPath(import.meta.url));

/** How a scrolling container lets a keyboard in. */
type Reach =
  /** The container itself is a Tab stop. `tabIndex={0}` sits on the element carrying the class. */
  | "focusable"
  /** It always contains a real control, so Tab lands inside it and the arrows scroll it. */
  | "has-controls"
  /** Focus is moved onto it programmatically when it opens, so `tabIndex={-1}` is enough. */
  | "focus-moved";

type Container = {
  /** The file rendering the class, relative to `src/`. */
  file: string;
  reach: Reach;
  /** Why that is the right answer here, in the words of someone reading a failure. */
  why: string;
};

/** Every selector in the stylesheet that declares `overflow` as `auto` or `scroll`, and how a
 *  keyboard operator moves it. Keyed by the selector exactly as the stylesheet writes it, so a
 *  selector edited in the CSS fails here rather than quietly matching nothing. */
const CONTAINERS: Record<string, Container> = {
  ".docs-content": {
    file: "docs/DocsModal.tsx",
    reach: "focusable",
    why: "a doc is prose, so there is no link or button to tab onto and carry the scroll with",
  },
  ".docs-content .doc-table": {
    file: "docs/DocBody.tsx",
    reach: "focusable",
    why: "a docs table has no focusable cell, and scrolls sideways on a narrow pane",
  },
  ".docs-content .doc-diagram": {
    file: "docs/DocBody.tsx",
    reach: "focusable",
    why: "a flowchart is captions and boxes, none of them focusable; it named itself as a group before it could be reached, which advertised a stop the Tab order never visited",
  },
  ".log-console": {
    file: "components/LogsPanel.tsx",
    reach: "focusable",
    why: "log rows are <span>s; nothing in the console can take focus",
  },
  ".matrix-scroll": {
    file: "components/ListsPanel.tsx",
    reach: "focusable",
    why: "the per-server counts matrix scrolls sideways on a narrow pane and holds no focusable cell, so the box itself takes the tabIndex to carry the scroll",
  },
  ".why": {
    file: "components/WhyShell.tsx",
    reach: "focusable",
    why: "the reasoning body is long, and two of the six panels (the WhyPanel and ScalesPanel fallbacks) render no control at all; above the overlay boundary nothing moves focus here either",
  },
  ".modal": {
    file: "components/ModalShell.tsx",
    reach: "focus-moved",
    why: "useDialogFocus puts focus on the panel itself when the dialog opens",
  },
  ".step-card": {
    file: "components/SetupStepper.tsx",
    reach: "has-controls",
    why: "every step of the wizard ends in its own actions -- Back, Skip, Continue -- and the steps before them hold inputs, service rows or library switches, so Tab lands inside the card and carries the scroll with it. It wears `.modal`'s chrome but is NOT a dialog and moves no focus, so it is classified on its controls rather than on `useDialogFocus`. The one branch that renders this class with no control is SetupWizard's own 'Setting things up…', which is a heading alone and cannot overflow",
  },
  ".sheet": {
    file: "components/Login.tsx",
    reach: "focus-moved",
    why: "useDialogFocus focuses the sheet, and it holds the sign-in form's own fields besides",
  },
  ".docs-index": {
    file: "docs/DocsModal.tsx",
    reach: "has-controls",
    why: "one <button> per doc, plus a button per section of the open doc",
  },
  ".filter-menu": {
    file: "components/FilterMenu.tsx",
    reach: "has-controls",
    why: "every row of the menu is a <button>; the queue's filter pickers and the list modal's presets both render this one component",
  },
  ".bulk-bar": {
    file: "components/ReviewQueue.tsx",
    reach: "has-controls",
    why: "the select-mode bar is buttons, and it carries role=region with a name",
  },
  ".suggest-pop": {
    file: "components/PolicyRuleEditors.tsx",
    reach: "has-controls",
    why: "an aria-activedescendant listbox: the <input> keeps DOM focus and the arrow keys move the active option, so the operator drives it without the list ever taking focus. It holds no focusable child of its own, which is why it reads as an exception here rather than as one of the buttons-inside cases",
  },
  ".feed-scroll": {
    file: "components/ReapPlan.tsx",
    reach: "focusable",
    why: "the reaping card's item-status log is icon, title and size spans, none of them focusable",
  },
  ".reap-runs": {
    file: "components/ReapPlan.tsx",
    reach: "has-controls",
    why: "every past run is a button opening its detail sheet, and a list long enough to scroll holds several: only the one run executing right now is a plain row",
  },
  ".run-detail-scroll": {
    file: "components/ReapPlan.tsx",
    reach: "focusable",
    why: "the run detail sheet's outcomes feed is the same spans as .feed-scroll, with nothing focusable in a row",
  },
};

/** The count is pinned so a scroll container that leaves the walk fails as loudly as one that
 *  arrives without a classification. A flag-shaped assertion cannot tell a member that
 *  complies from one the matcher stopped collecting. */
const EXPECTED_CONTAINERS = 16;

/** `overflow`, `overflow-x` or `overflow-y` set to a value that makes a box scroll. `hidden`,
 *  `visible` and `clip` do not, and `overflow: auto hidden` (the two-value form) is read by the
 *  same matcher because the value is scanned for the keyword rather than compared whole. */
const SCROLLS = /overflow(?:-x|-y)?\s*:\s*[^;}]*\b(?:auto|scroll)\b/;

/** Every rule in the stylesheet, as `{ selector, body }`, comments already stripped. Flat: no
 *  at-rule here nests a block, so a brace pair is a rule. */
function rules(): { selector: string; body: string; at: number }[] {
  const code = CSS.replace(/\/\*[\s\S]*?\*\//g, (m) => " ".repeat(m.length));
  const out: { selector: string; body: string; at: number }[] = [];
  const re = /([^{}]+)\{([^{}]*)\}/g;
  let m: RegExpExecArray | null;
  while ((m = re.exec(code)) !== null) {
    const selector = (m[1] ?? "").trim().replace(/\s+/g, " ");
    // A media query's own prelude opens a block whose "body" is more rules. The flat walk sees
    // the inner rules on their own, so an `@media` prelude reaching here has an empty body.
    if (selector.startsWith("@")) continue;
    out.push({ selector, body: m[2] ?? "", at: m.index });
  }
  return out;
}

/** The scrolling rules, in stylesheet order. */
function scrollers(): { selector: string; at: number }[] {
  return rules()
    .filter((r) => SCROLLS.test(r.body))
    .map((r) => ({ selector: r.selector, at: r.at }));
}

/** The JSX opening tag that carries `className`-with-this-class, read whole.
 *
 *  Anchoring on a delimiter would not survive this tree. Three forms here defeat the obvious
 *  scan, and each of them is load-bearing in a real component:
 *    - `.log-console`'s class is a ternary, not a literal, so a matcher wanting `className="`
 *      never sees it.
 *    - `.why`'s element carries an `onKeyDown` arrow function, whose `=>` includes a `>` a
 *      forward scan would take for the end of the tag.
 *    - `.why`'s element also holds a comment discussing `<aside>` and `<header>`, so a scan
 *      that treats `<` as "this was never a tag" would abandon the real tag halfway through,
 *      and report "no element renders that class" for a surface that is right there.
 *  So the scan tracks brace depth, quote state, and comments, and only a `>` at depth 0
 *  outside both closes the tag. Returns every matching tag, since a class can be rendered at
 *  more than one site and each has to carry the attribute. */
function openingTagsWithClass(source: string, cls: string): string[] {
  const found: string[] = [];
  // The class as it appears inside the className value: bounded by quote, brace, space, or the
  // string's own edge, so `doc-table` does not match `doc-table-wide`. The value is carved out
  // by `classValueSpan` below rather than searched for in the whole tag. A windowed search over
  // the tag would misclassify `.why-close`'s button as carrying `.why` whenever a nearby
  // aria-label holds a dotted catalog key like `why.panel.shell.close`, since the key spells
  // the bare word the window is looking for.
  const needle = new RegExp(`(?<![\\w-])${cls}(?![\\w-])`);
  // The className value's own text. Either the literal's contents, or the balanced braced
  // expression's, which covers the ternary and template spellings the tree uses.
  const classValueSpan = (tag: string): string | null => {
    const at = tag.indexOf("className=");
    if (at === -1) return null;
    const rest = tag.slice(at + "className=".length);
    if (rest[0] === '"') {
      const close = rest.indexOf('"', 1);
      return close === -1 ? rest : rest.slice(1, close);
    }
    if (rest[0] === "{") {
      let depth = 0;
      for (let k = 0; k < rest.length; k++) {
        if (rest[k] === "{") depth++;
        else if (rest[k] === "}" && --depth === 0) return rest.slice(1, k);
      }
      return rest.slice(1);
    }
    return null;
  };
  for (let i = 0; i < source.length; i++) {
    if (source[i] !== "<") continue;
    let depth = 0;
    let quote: string | null = null;
    let end = -1;
    for (let j = i + 1; j < source.length; j++) {
      const c = source[j];
      if (quote !== null) {
        if (c === quote) quote = null;
        continue;
      }
      // Comments first: their contents are prose, and prose in this repo routinely holds
      // markup, apostrophes and backticks that would otherwise open a quote that never closes.
      if (c === "/" && source[j + 1] === "/") {
        const nl = source.indexOf("\n", j);
        if (nl === -1) break;
        j = nl;
        continue;
      }
      if (c === "/" && source[j + 1] === "*") {
        const close = source.indexOf("*/", j + 2);
        if (close === -1) break;
        j = close + 1;
        continue;
      }
      if (c === '"' || c === "'" || c === "`") {
        quote = c;
        continue;
      }
      if (c === "{") depth++;
      else if (c === "}") depth--;
      else if (c === ">" && depth === 0) {
        end = j;
        break;
      }
      // A `<` at depth 0, outside a comment and outside quotes, means the `<` this scan
      // started from was not a tag opener (a comparison, or JSX text). Give it up.
      else if (c === "<" && depth === 0) break;
    }
    if (end === -1) continue;
    const tag = source.slice(i, end + 1);
    const value = classValueSpan(tag);
    if (value !== null && needle.test(value)) found.push(tag);
    i = end;
  }
  return found;
}

const read = (file: string) => readFileSync(join(HERE, file), "utf8");

/** The last simple class in a selector is the element the rule is about. `.docs-content
 *  .doc-table` styles the table wrapper, not the pane around it. */
function classOf(selector: string): string {
  const parts = selector.split(/[\s>+~]+/).filter(Boolean);
  const last = parts[parts.length - 1] ?? selector;
  return last.replace(/^\./, "");
}

describe("a scrolling container is reachable from a keyboard", () => {
  it("classifies every scroll container the stylesheet declares", () => {
    const seen = scrollers();
    const unclassified = seen
      .filter((s) => CONTAINERS[s.selector] === undefined)
      .map((s) => `${s.selector} (${siteOf(s.at)})`);
    expect(
      unclassified,
      "a container that scrolls but is not classified in CONTAINERS: say how a keyboard " +
        "operator moves it -- 'focusable' (give the element tabIndex={0}), 'has-controls', " +
        "or 'focus-moved' -- and record why in its `why`",
    ).toEqual([]);
  });

  it("still finds every container it was written against", () => {
    const seen = scrollers().map((s) => s.selector);
    const missing = Object.keys(CONTAINERS).filter((sel) => !seen.includes(sel));
    expect(
      missing,
      "classified here but no longer scrolling in the stylesheet: if the selector was renamed, " +
        "rename it here; if the container is gone, delete its row (rule 64)",
    ).toEqual([]);
    // Pins the size of the walk itself, which the two lists above cannot. They agree with each
    // other perfectly while both describing a population the matcher stopped collecting.
    expect(seen.length, "the number of scrolling containers in the stylesheet").toBe(
      EXPECTED_CONTAINERS,
    );
  });

  it("gives every container classified `focusable` a real tabIndex={0}", () => {
    const wrong: string[] = [];
    for (const [selector, c] of Object.entries(CONTAINERS)) {
      if (c.reach !== "focusable") continue;
      const tags = openingTagsWithClass(read(c.file), classOf(selector));
      if (tags.length === 0) {
        wrong.push(`${selector}: no element in ${c.file} renders that class`);
        continue;
      }
      for (const tag of tags) {
        if (!/tabIndex=\{0\}/.test(tag)) {
          wrong.push(
            `${selector}: an element in ${c.file} carries the class without tabIndex={0}, so ` +
              `the container cannot be reached by Tab and its scroll is unreachable -- ${c.why}`,
          );
        }
      }
    }
    expect(wrong).toEqual([]);
  });

  it("keeps a `focus-moved` container's tabIndex at -1, so it is not two stops", () => {
    const wrong: string[] = [];
    for (const [selector, c] of Object.entries(CONTAINERS)) {
      if (c.reach !== "focus-moved") continue;
      const tags = openingTagsWithClass(read(c.file), classOf(selector));
      if (tags.length === 0) {
        wrong.push(`${selector}: no element in ${c.file} renders that class`);
        continue;
      }
      // -1 is what makes `useDialogFocus` able to focus it at all; a dialog that is ALSO a Tab
      // stop inside its own trap is a stop that reads the whole dialog again.
      for (const tag of tags) {
        if (!/tabIndex=\{-1\}/.test(tag)) {
          wrong.push(`${selector}: ${c.file} no longer carries tabIndex={-1} -- ${c.why}`);
        }
      }
    }
    expect(wrong).toEqual([]);
  });

  it("still renders the class for a container exempted on its contents", () => {
    // The `why` is a human argument and this cannot check it. What it does hold is that the
    // surface still exists. An exemption whose element is gone is a stale row vouching for a
    // decision nobody has re-made.
    const missing: string[] = [];
    for (const [selector, c] of Object.entries(CONTAINERS)) {
      if (c.reach !== "has-controls") continue;
      if (openingTagsWithClass(read(c.file), classOf(selector)).length === 0) {
        missing.push(`${selector}: no element in ${c.file} renders that class`);
      }
    }
    expect(missing).toEqual([]);
  });
});
