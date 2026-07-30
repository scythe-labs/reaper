// SPDX-License-Identifier: AGPL-3.0-or-later
// Rule 139, held as a table instead of a comment: an element rendering text the operator did not
// choose -- a media title, a portal handle, a path, a host, an upstream error -- carries a break
// opportunity, because such a value can be one long unbroken string and on a phone it paints
// straight through the box holding it.
//
// This is a test rather than prose because the rule already decayed once. It was applied at the
// twelve blocks that existed when it was written and never carried across (rule 72), and the tell
// was three pairs where one half had the fix and the other did not: `.sig-detail`/`.signal-detail`
// 76 lines apart in one panel, `.instance-url`/`.test-badge` two lines apart on one card, and
// `.plan-steps code`/`.dryrun-outcomes li code` in two subtrees of one report. #219 and #220 are
// what that cost; SITES below is what replaces the remembering.
//
// **What this guard can and cannot do.** Whether an element renders text from outside the app is
// a fact about the TSX, not about the stylesheet, so no grep over `index.css` can discover a NEW
// site -- only a person adding a row here can. What the table does hold, for every site already
// in it:
//   - the block still exists (a rename fails, rather than dropping the site out of the walk
//     while the flag-shaped assertions stay green -- rule 145);
//   - the component still renders the class, so removing a surface without its style is caught
//     (rule 64);
//   - the break opportunity is still granted, and nothing declared after it takes it back;
//   - a site recorded as a deliberate exception still IS one, so a stale exemption cannot sit
//     here vouching for a decision somebody has since reversed.
//
// **Two limits, named rather than implied** (rule 118: a check that cannot discriminate must not
// read as a proof). Selectors are compared whole, so an element also targeted by a compound or
// descendant selector is only covered if that selector is listed in its `selectors` too. And the
// cascade is resolved by source order alone, which is right while a site's selectors are declared
// at equal-or-rising specificity -- as they are today -- and would miss a higher-specificity
// `nowrap` added ABOVE the block that grants the wrap.
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

import { describe, expect, it } from "vitest";

const HERE = dirname(fileURLToPath(import.meta.url));
const CSS = readFileSync(join(HERE, "index.css"), "utf8");

type Site = {
  /** The value that lands here, in the words of someone reading a failure message. */
  what: string;
  /** Every selector in `index.css` styling this element, in the order the file declares them. */
  selectors: string[];
  /** The class the components write, checked to still be there. */
  classInTsx: string;
  /** Files rendering it. Every one is checked, not just the first (rule 72). */
  seenIn: string[];
  /** Set when the element takes its value from its parent instead of declaring one. */
  forwards?: string[];
  /** Set when this site deliberately does NOT wrap. The string is the reason, and it is the
   *  whole point of the field: an exception nobody wrote down reads as an omission. */
  exempt?: string;
};

// Every element known to render text from outside Reaper. Grouped by where the fix came from, so
// the pre-existing dozen are visibly under the same guard as the ones #219 and #220 added.
const SITES: Site[] = [
  // ---- already correct when the sweep started -------------------------------------------
  {
    what:
      "a custom rule's sentence, which embeds a Plex list or collection name, a genre or a " +
      "quality string, and on the could-not-check arm an upstream reason",
    selectors: [".sig-detail"],
    classInTsx: "sig-detail",
    seenIn: ["components/WhyPanel.tsx"],
  },
  {
    what: "the requester's display name from the request portal",
    selectors: [".fair-name"],
    classInTsx: "fair-name",
    seenIn: ["components/Fairness.tsx"],
  },
  {
    what: "the same person's display name, one surface over in the person panel",
    selectors: [".scales-head-id h2"],
    classInTsx: "scales-head-id",
    seenIn: ["components/ScalesPanel.tsx"],
  },
  {
    what: "the card's title worn as a control -- a media, show, season or person name",
    selectors: [".card-open"],
    classInTsx: "card-open",
    seenIn: ["components/CardOpen.tsx"],
    // It declares `inherit` rather than a value: the button must wrap exactly as the heading
    // around it does, whichever heading that is. So its guarantee is really its parents', and
    // each of them is a row here in its own right -- including `.season-name`, whose recorded
    // exemption this button correctly forwards.
    forwards: [".card-title", ".fair-name", ".season-name"],
  },
  {
    what: "the base URL of a Sonarr, Radarr or Seerr instance as the operator typed it",
    selectors: [".instance-url"],
    classInTsx: "instance-url",
    seenIn: ["components/Settings.tsx"],
  },
  {
    // The grid around this is not a row of its own: `.plex-map-grid` renders no text, and floors
    // its folder column at `minmax(0, max-content)` precisely so this cell can shrink and wrap.
    what: "a root folder's filesystem path, and a Seerr service's name",
    selectors: [".pl-root"],
    classInTsx: "pl-root",
    seenIn: ["components/ServiceModal.tsx"],
  },
  {
    what: "the name of the backup file the operator dropped or picked",
    selectors: [".chosen-file"],
    classInTsx: "chosen-file",
    seenIn: ["components/Settings.tsx"],
  },
  {
    what: "one raw log line, which routinely carries a path, a host or upstream error text",
    selectors: [".log-console.log-wrap .log-text"],
    classInTsx: "log-text",
    seenIn: ["components/LogsPanel.tsx"],
  },
  {
    what: "help text, app-authored except on the Plex row, which appends the server's URL",
    selectors: [".set-row .help"],
    classInTsx: "help",
    seenIn: ["components/PlexPanel.tsx"],
  },
  {
    what: "the About list, whose Data folder row is the server's configured path",
    selectors: [".about-kv dd"],
    classInTsx: "about-kv",
    seenIn: ["components/Settings.tsx"],
  },

  // ---- #219: eleven blocks, one declaration each ------------------------------------------
  {
    what: "a media title on a queue card",
    selectors: [".card-title"],
    classInTsx: "card-title",
    seenIn: ["components/ReviewQueue.tsx"],
  },
  {
    what: "the same media title at the head of a panel",
    selectors: [".why-head h2"],
    classInTsx: "why-head",
    seenIn: ["components/WhyPanel.tsx", "components/ShowPanel.tsx"],
  },
  {
    what: "the rule that argued a keep Reaper could not check",
    selectors: [".signal-detail"],
    classInTsx: "signal-detail",
    seenIn: ["components/WhyPanel.tsx"],
  },
  {
    what: "what a protection said, quoting an upstream error on the could-not-check branch",
    selectors: [".gate-detail"],
    classInTsx: "gate-detail",
    seenIn: ["components/WhyPanel.tsx"],
  },
  {
    what: "a failed connection test, which quotes the URL it tried",
    selectors: [".test-badge"],
    classInTsx: "test-badge",
    seenIn: ["components/ServiceModal.tsx"],
  },
  {
    what: "the title, or the media key when there is none, on the screen confirming a deletion",
    selectors: [".reap-item-title"],
    classInTsx: "reap-item-title",
    seenIn: ["components/ReapConfirm.tsx"],
  },
  {
    what: "the executor's own words for one item",
    selectors: [".dryrun-outcomes li code"],
    classInTsx: "dryrun-outcomes",
    seenIn: ["components/ReapPlan.tsx"],
  },
  {
    what: "a Plex server's name, from plex.tv",
    selectors: [".server-pick-row"],
    classInTsx: "server-pick-row",
    seenIn: ["components/PlexPin.tsx"],
  },
  {
    what: "a simulated media title",
    selectors: [".sim-examples li"],
    classInTsx: "sim-examples",
    seenIn: ["components/PolicySimulator.tsx"],
  },
  {
    what: "the name the operator gave a library on their own Plex server",
    selectors: [".lib-card"],
    classInTsx: "lib-card",
    seenIn: ["components/PlexPanel.tsx"],
  },
  {
    what: "the scan's live line, which names the library or path it is reading right now",
    selectors: [".jobrow-run"],
    classInTsx: "jobrow-run",
    seenIn: ["components/JobStatus.tsx"],
  },

  // ---- #220: the five that needed a layout decision ---------------------------------------
  {
    what: "a media title on the Scales card, which used to be cut with an ellipsis",
    selectors: [".scales-title-name"],
    classInTsx: "scales-title-name",
    seenIn: ["components/ScalesPanel.tsx", "components/UnmatchedList.tsx"],
  },
  {
    what: "the handle of whoever asked for the title",
    // `.chip` is listed because the element wears both, and it is the block the nowrap came from.
    selectors: [".chip", ".chip-requested"],
    classInTsx: "chip-requested",
    seenIn: ["components/ReviewQueue.tsx"],
  },
  {
    what: "the Plex library an item lives in",
    selectors: [".lib-chip"],
    classInTsx: "lib-chip",
    seenIn: ["components/ReviewQueue.tsx"],
  },
  {
    what: "a season with no number, where the name falls back to the server's own title",
    selectors: [".panel-season-name", ".panel-season-name.is-server-title"],
    classInTsx: "is-server-title",
    seenIn: ["components/ShowPanel.tsx"],
  },
  {
    what: "the season's name in the queue's expanded list",
    selectors: [".season-name"],
    classInTsx: "season-name",
    seenIn: ["components/ReviewQueue.tsx"],
    exempt:
      "the row height wins: wrapping grew the row taller when the list narrowed to seat the " +
      "open why panel, shifting every card below it. Unlike .panel-season-name there is no " +
      "branch to split, and truncating instead is what rule 139 forbids outright. #220",
  },
];

type Rule = { selectors: string[]; body: string; at: number };

/** Every rule in a stylesheet, in source order, with its selectors split out.
 *
 *  At-rules are stepped INTO rather than captured, so a rule inside `@media` is collected like
 *  any other. Comments are blanked rather than deleted, so a `{` in prose cannot open a block and
 *  every offset still points at the real file. */
function rulesOf(source: string): Rule[] {
  const src = source.replace(/\/\*[\s\S]*?\*\//g, (m) => m.replace(/[^\n]/g, " "));
  const out: Rule[] = [];
  let i = 0;
  let preludeAt = 0;
  while (i < src.length) {
    if (src[i] === "}") {
      i += 1;
      preludeAt = i;
      continue;
    }
    if (src[i] !== "{") {
      i += 1;
      continue;
    }
    const prelude = src.slice(preludeAt, i).trim();
    if (prelude.startsWith("@")) {
      i += 1;
      preludeAt = i;
      continue;
    }
    let depth = 1;
    let j = i + 1;
    while (j < src.length && depth > 0) {
      if (src[j] === "{") depth += 1;
      else if (src[j] === "}") depth -= 1;
      j += 1;
    }
    out.push({
      selectors: prelude
        .split(",")
        .map((s) => s.trim().replace(/\s+/g, " "))
        .filter(Boolean),
      body: src.slice(i + 1, j - 1),
      at: i,
    });
    i = j;
    preludeAt = i;
  }
  return out;
}

const RULES = rulesOf(CSS);

/** The last value a block gives a property, or null. */
function declaredValue(body: string, prop: string): string | null {
  const re = new RegExp(`(?:^|;)\\s*${prop}\\s*:\\s*([^;}]+)`, "g");
  let found: string | null = null;
  let m: RegExpExecArray | null;
  while ((m = re.exec(body)) !== null) {
    const value = m[1];
    if (value !== undefined) found = value.trim();
  }
  return found;
}

const matchesOf = (site: Site) =>
  RULES.filter((r) => r.selectors.some((s) => site.selectors.includes(s)));

const lineOf = (at: number) => CSS.slice(0, at).split("\n").length;

/** Where a site is granted a break opportunity, and where one is taken back, in source order.
 *  `inherit` counts as a grant: the element is forwarding a parent's value on purpose, and the
 *  parents are themselves rows in the table (see `forwards`). */
function resolve(site: Site) {
  let grantedAt: number | null = null;
  const defeats: { at: number; prop: string; value: string }[] = [];
  for (const rule of matchesOf(site)) {
    const wrap = declaredValue(rule.body, "overflow-wrap");
    if (wrap === "anywhere" || wrap === "inherit") grantedAt = rule.at;
    else if (wrap === "normal") grantedAt = null;
    for (const prop of ["white-space", "text-overflow"]) {
      const v = declaredValue(rule.body, prop);
      if (v === "nowrap" || v === "ellipsis") defeats.push({ at: rule.at, prop, value: v });
    }
  }
  // `>=`, not `>`: a `nowrap` sitting in the SAME block as the break opportunity defeats it just
  // as completely as one in a later block, and reads even more like a fix than it does. The first
  // version of this check compared strictly and let exactly that mutation through.
  const defeatedAfterGrant =
    grantedAt === null ? [] : defeats.filter((d) => d.at >= (grantedAt as number));
  return { grantedAt, defeats, defeatedAfterGrant };
}

const named = (site: Site) => `${site.selectors.join(" / ")} (${site.what})`;

describe("index.css: text the operator did not choose", () => {
  it("still declares every block the table names", () => {
    const missing: string[] = [];
    for (const site of SITES) {
      for (const selector of site.selectors) {
        if (!RULES.some((r) => r.selectors.includes(selector))) {
          missing.push(`${selector} -- listed for "${site.what}" but no block declares it`);
        }
      }
    }
    // A selector that stopped matching would otherwise drop its site out of every check below
    // while they all stayed green (rule 145). If one was renamed, rename it here too.
    expect(missing).toEqual([]);
  });

  it("still renders every class the table names, in every file it names", () => {
    const gone: string[] = [];
    for (const site of SITES) {
      // A whole token, not a substring: `includes("gate-detail")` is satisfied by
      // `"gate-detail-x"`, so a rename -- the likeliest way a class and its style part company --
      // read as still present. `\b` is no help when the name itself contains a hyphen.
      const token = new RegExp(String.raw`(?<![\w-])${site.classInTsx}(?![\w-])`);
      for (const file of site.seenIn) {
        const source = readFileSync(join(HERE, file), "utf8");
        if (!token.test(source)) {
          gone.push(`${file} no longer renders "${site.classInTsx}" (${site.what})`);
        }
      }
    }
    // Rule 64: a surface removed takes its style, and its row here, with it.
    expect(gone).toEqual([]);
  });

  it("gives every one of them somewhere to break", () => {
    const bare = SITES.filter((s) => !s.exempt && resolve(s).grantedAt === null).map(
      (s) => `${named(s)} -- add overflow-wrap: anywhere, or record why not (rule 139)`,
    );
    expect(bare).toEqual([]);
  });

  it("lets nothing take that break opportunity back", () => {
    const undone: string[] = [];
    for (const site of SITES) {
      if (site.exempt) continue;
      const { grantedAt, defeatedAfterGrant } = resolve(site);
      for (const d of defeatedAfterGrant) {
        undone.push(
          `${named(site)} -- ${d.prop}: ${d.value} at index.css:${lineOf(d.at)} defeats the ` +
            `break opportunity at index.css:${lineOf(grantedAt as number)}`,
        );
      }
    }
    // This is #220's whole shape: a `nowrap` or an ellipsis makes `overflow-wrap` a no-op, so a
    // site can carry the declaration and still paint out of its box.
    expect(undone).toEqual([]);
  });

  it("keeps every recorded exception an exception", () => {
    const stale: string[] = [];
    for (const site of SITES.filter((s) => s.exempt)) {
      if (resolve(site).grantedAt !== null) {
        stale.push(`${named(site)} wraps now -- drop its \`exempt\` reason, it is no longer true`);
      }
    }
    // The table has to be wrong in neither direction: an exemption left behind after somebody
    // fixed the site is a written decision vouching for the opposite of what ships.
    expect(stale).toEqual([]);
  });

  it("forwards an inherited break opportunity from parents that are themselves in the table", () => {
    const orphaned: string[] = [];
    for (const site of SITES.filter((s) => s.forwards)) {
      for (const parent of site.forwards as string[]) {
        if (!SITES.some((s) => s.selectors.includes(parent))) {
          orphaned.push(`${named(site)} inherits from ${parent}, which no row here governs`);
        }
      }
    }
    // `.card-open` declares `inherit`, so it is only as good as the headings it sits in. Each has
    // to be a row here, or the button's guarantee rests on nothing.
    expect(orphaned).toEqual([]);
  });

  it("walks the population it claims to, and says how big that is", () => {
    // Rule 145: a flag-shaped assertion cannot tell a member that complies from one that fell out
    // of the walk, so the size of the walk is pinned by hand. Reconciled against the table above:
    // 10 sites that already carried the fix, 11 from #219, 5 from #220 (one of them exempt).
    expect(SITES.length).toBe(26);
    expect(SITES.filter((s) => s.exempt).length).toBe(1);
    // And the blocks those 26 sites actually resolve to. Twenty-two sites resolve to one block
    // each; the other four are `.about-kv dd` (its own, plus a margin under 560px), the requested
    // chip (`.chip`, its own, and the rule it shares with `.lib-chip`), `.lib-chip` (its own and
    // that shared rule again), and the unnumbered season (the base and the modifier). If this
    // moves, a selector joined or left a row -- check it was meant to, then update the number.
    const blocks = SITES.reduce((n, s) => n + matchesOf(s).length, 0);
    expect(blocks).toBe(31);
  });
});

// Rule 147: a guard reading source text is bounded by the syntax it can parse, so the parser is
// run against every spelling `index.css` actually uses before it is trusted with the checks above.
describe("the stylesheet walk", () => {
  it("reads every selector spelling the file uses", () => {
    const fixture = `
      /* a comment holding a brace { and .a-decoy { color: red } */
      .plain { color: red; }
      .first,
      .second { color: red; }
      .parent .descendant { color: red }
      .compound.modifier { color: red }
      .parent > .child { color: red }
      @media (max-width: 600px) {
        .inside-at-rule { color: red }
      }
      @keyframes spin { from { opacity: 0 } to { opacity: 1 } }
    `;
    expect(rulesOf(fixture).map((r) => r.selectors)).toEqual([
      [".plain"],
      [".first", ".second"],
      [".parent .descendant"],
      [".compound.modifier"],
      [".parent > .child"],
      [".inside-at-rule"],
      ["from"],
      ["to"],
    ]);
  });

  it("reads the last value a block gives a property, and only that property", () => {
    // `overflow-wrap` must not be answered by a block that only declares `overflow`, which is the
    // one substring collision in the properties this file checks.
    expect(declaredValue("overflow: hidden; text-overflow: ellipsis", "overflow-wrap")).toBe(null);
    expect(declaredValue("overflow: hidden; overflow-wrap: anywhere", "overflow-wrap")).toBe(
      "anywhere",
    );
    expect(declaredValue("white-space: nowrap; white-space: normal", "white-space")).toBe("normal");
  });

  it("meets no CSS nesting, which it does not model", () => {
    // Native nesting would put a rule inside another rule's body, where the walk cannot see it.
    // The file uses none today; if that changes, teach `rulesOf` about it before trusting it.
    expect(RULES.filter((r) => r.selectors.some((s) => s.includes("&")))).toEqual([]);
  });
});
