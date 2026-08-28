// SPDX-License-Identifier: AGPL-3.0-or-later
// An element rendering text the operator did not choose (a media title, a portal handle, a path,
// a host, an upstream error) needs a break opportunity, because such a value can be one long
// unbroken string, and on a phone it would paint straight through the box holding it.
//
// This is a table checked by a test, not a rule stated in prose, because a rule in prose does not
// stay applied: a new site can be added without anyone re-reading it. The tell was three pairs
// where one half had the fix and the other did not: `.sig-detail`/`.signal-detail` 76 lines apart
// in one panel, `.instance-url`/`.test-badge` two lines apart on one card, and `.plan-steps
// code`/`.dryrun-outcomes li code` in two subtrees of one report. SITES below replaces the
// remembering.
//
// What this guard can and cannot do: whether an element renders text from outside the app is a
// fact about the TSX, not about the stylesheet, so no grep over the CSS can discover a new site;
// only a person adding a row here can. What the table does check, for every site already in it:
//   - the block still exists (a rename fails the test, rather than dropping the site out of the
//     walk while every other check still passes);
//   - the component still renders the class, so removing a surface without its style is caught;
//   - the break opportunity is still granted, and nothing declared after it takes it back;
//   - a site recorded as a deliberate exception still is one, so a stale exemption cannot sit
//     here vouching for a decision somebody has since reversed.
//
// Two limits are named here rather than left implicit, since a check that cannot tell one case
// from another must not read as proof of both. Selectors are compared whole, so an element also
// targeted by a compound or descendant selector is only covered if that selector is listed in its
// `selectors` too. And the cascade is resolved by source order alone, which is correct while a
// site's selectors are declared at equal or rising specificity, as they are today, but would miss
// a higher-specificity `nowrap` added above the block that grants the wrap.
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

import { describe, expect, it } from "vitest";

import { CSS, siteOf } from "./test/stylesheet";

const HERE = dirname(fileURLToPath(import.meta.url));

type Site = {
  /** The value that lands here, in the words of someone reading a failure message. */
  what: string;
  /** Every selector in the stylesheet styling this element, in the order it declares them. */
  selectors: string[];
  /** The class the components write, checked to still be there. */
  classInTsx: string;
  /** Files rendering it. Every one is checked, not just the first. */
  seenIn: string[];
  /** Set when the element takes its value from its parent instead of declaring one. */
  forwards?: string[];
  /** Set when this site deliberately does NOT wrap. The string is the reason, and the reason
   *  is what the field is for: an exception nobody wrote down reads as an omission. */
  exempt?: string;
  /** Set when the break opportunity is granted on a flex or grid CONTAINER rather than on the
   *  element holding the text. The string says why no sibling in that row is hurt by it. */
  onAFlexRow?: string;
};

// Every element known to render text from outside Reaper. Grouped by when each group was added,
// so the pre-existing dozen are visibly under the same guard as the ones added later.
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
    // each of them is a row here in its own right, including `.season-name`, whose recorded
    // exemption this button correctly forwards.
    forwards: [".card-title", ".fair-name", ".season-name"],
  },
  {
    what: "the base URL of a Sonarr, Radarr or Seerr instance as the operator typed it",
    selectors: [".instance-url"],
    classInTsx: "instance-url",
    seenIn: ["components/ServicesPanel.tsx"],
  },
  {
    // The grid around this is not a row of its own: `.plex-map-grid` renders no text, and sets
    // its folder column to `minmax(0, max-content)` precisely so this cell can shrink and wrap.
    // Under 640px that grid stacks and this cell heads its own picker, which only adds a margin.
    // It is listed here because a selector that reaches this element and is not listed would be
    // the coverage gap named above, not because that block does anything else yet.
    what: "a root folder's filesystem path, and a Seerr service's name",
    selectors: [".pl-root", ".plex-map-grid > .pl-root:not(:first-child)"],
    classInTsx: "pl-root",
    seenIn: ["components/ServiceModal.tsx"],
  },
  {
    what:
      "the chosen Plex library, and the chosen Sonarr or Radarr connection -- both of them " +
      "names the operator gave something on another server, shown at rest in the picker",
    selectors: [".field-sm select", ".pl-pick .pl-select", ".pl-select.unset"],
    classInTsx: "pl-select",
    seenIn: ["components/ServiceModal.tsx"],
    exempt:
      "a native <select> cannot wrap: the closed control clips its selected option to its own " +
      "width, and no property reaches inside it. So this is the one shape in the table where " +
      "rule 139's own remedy does not exist, and truncation is not a choice being made here. " +
      "The control's own width was widened as far as it goes -- the grid stacks below 640px so " +
      "the picker gets the whole row rather than the remainder after a long folder path " +
      "(measured on a 390px phone: 41.4px before, not one character of the library legible; " +
      "220.1px after, #250) -- and that bounded the harm without removing it, because the " +
      "failure is a ratio between the name and any finite width. The name itself is no longer " +
      "trapped in here: `.pl-echo` below states it as text that CAN wrap, which is where rule " +
      "139 is satisfied for this value. What still clips is this control, and only this " +
      "control. #306",
  },
  {
    // This is a second row rather than a wider `.pl-select` one because these are two elements
    // carrying one value, and only this one can wrap. Keeping them apart is what keeps the
    // exemption above narrow: it exempts a control, not a value.
    what:
      "the chosen Plex library and the chosen Sonarr or Radarr connection again, restated " +
      "under the picker as text, because the picker itself cannot wrap them",
    selectors: [".pl-echo"],
    classInTsx: "pl-echo",
    seenIn: ["components/ServiceModal.tsx"],
  },
  {
    what: "the name of the backup file the operator dropped or picked",
    selectors: [".chosen-file"],
    classInTsx: "chosen-file",
    // The restore flow moved out of Settings into its own module once the first-run wizard
    // grew a path to it; one component draws this in both places.
    seenIn: ["components/RestoreCard.tsx"],
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
    // `SetRow` writes this class for every settings row now, so it is the file this site lives
    // in. `PlexPanel.tsx` still spells `help` on a paragraph outside `.set-rows`, which is a
    // different element from the one this selector styles, so listing it here would check the
    // wrong element. What PlexPanel still supplies is the value: the linked row passes the
    // server's URL in as `help`.
    seenIn: ["components/SetRow.tsx"],
  },
  {
    what: "the About list, whose Data folder row is the server's configured path",
    selectors: [".about-kv dd"],
    classInTsx: "about-kv",
    seenIn: ["components/AboutPanel.tsx"],
  },

  // ---- eleven blocks, one declaration each ------------------------------------------------
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
    // One file: `PanelHead` renders this head for both the item panel and the show panel, so a
    // media title reaches this selector through `PanelHead` alone. `ScalesPanel.tsx` also puts
    // outside text under `.why-head h2`, a requester's display name through `ProfileName`, but
    // that is covered by its own row on `.scales-head-id h2` rather than here. The other two
    // `.why` heads carry app-written copy.
    seenIn: ["components/WhyPanel.tsx"],
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
    what: "a Plex server's name, from plex.tv",
    selectors: [".server-pick-row"],
    classInTsx: "server-pick-row",
    seenIn: ["components/PlexPin.tsx"],
  },
  {
    what: "a simulated media title",
    selectors: [".sim-example-title"],
    classInTsx: "sim-example-title",
    seenIn: ["components/PolicySimulator.tsx"],
  },
  {
    what: "the name the operator gave a library on their own Plex server",
    selectors: [".lib-card"],
    classInTsx: "lib-card",
    seenIn: ["components/PlexPanel.tsx"],
    onAFlexRow: "the name's only sibling is the Switch, which holds a size of its own",
  },
  {
    what: "the scan's live line, which names the library or path it is reading right now",
    selectors: [".jobrow-run"],
    classInTsx: "jobrow-run",
    seenIn: ["components/JobStatus.tsx"],
    onAFlexRow:
      "the line is a bare text node beside the spinner, so there is no element here to grant " +
      "it on, and the spinner carries no text to break",
  },

  // ---- five blocks that needed a layout decision ------------------------------------------
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

  // ---- Settings, Lists --------------------------------------------------------------------
  // A whole screen of outside text needed rows added here. Two of these needed the stylesheet
  // fixed too: `.jobrow-sched` and `.jobrow-desc` had no break opportunity at all, even though
  // the stylesheet comment beside them named "a Plex collection's name" as a value it was
  // protecting, while the wrap it actually granted only covered `.list-name`, the operator's
  // own words.
  {
    what:
      "the list's source line: a Plex collection and library name, or a pasted IMDb list id " +
      "(`sourceHint`). Shared with the Jobs tab's rows",
    selectors: [".jobrow-sched"],
    classInTsx: "jobrow-sched",
    seenIn: ["components/ListsPanel.tsx", "components/JobsPanel.tsx"],
  },
  {
    what:
      "the row's detail line, shared with the Jobs tab's rows, where a job states its own " +
      "status. On Lists it is now a plain count, so its outside text is the Jobs side",
    selectors: [".jobrow-desc"],
    classInTsx: "jobrow-desc",
    seenIn: ["components/ListsPanel.tsx", "components/JobsPanel.tsx"],
  },
  {
    what: "the list's name, which is the operator's own but may be pasted from anywhere",
    selectors: [".list-name"],
    classInTsx: "list-name",
    seenIn: ["components/ListsPanel.tsx"],
  },
  {
    what: "the upstream refusal from the last check of this list",
    selectors: [".list-error"],
    classInTsx: "list-error",
    seenIn: ["components/ListsPanel.tsx"],
  },
  {
    what: "a tag as it is spelled in Sonarr or Radarr, with its count",
    selectors: [".tag-pill"],
    classInTsx: "tag-pill",
    seenIn: ["components/ListsPanel.tsx"],
  },
  {
    what: "a tag as it is spelled in Sonarr or Radarr, down the side of the counts matrix",
    selectors: ['.tag-matrix th[scope="row"]'],
    classInTsx: "tag-matrix",
    seenIn: ["components/ListsPanel.tsx"],
    exempt:
      "a matrix cell in an overflow-x:auto box: it is reached by scrolling, not clipped, so " +
      "nowrap loses no tail. Wrapping crushed the pinned column one glyph per line, which is " +
      "rule 139's failure by the other road (#475).",
  },
  {
    what: "a Sonarr or Radarr instance's display name, across the top of the counts matrix",
    selectors: [".tag-matrix thead th"],
    classInTsx: "tag-matrix",
    seenIn: ["components/ListsPanel.tsx"],
    exempt:
      "same box as the Tag column: the matrix scrolls sideways rather than wrapping its " +
      "headers, so every server name is reachable and none is truncated (#475).",
  },
  {
    what: "a tag chip in the list editor, spelled on somebody else's keyboard",
    selectors: [".tag-chip"],
    classInTsx: "tag-chip",
    seenIn: ["components/TagsEditor.tsx"],
    onAFlexRow:
      "the chip's only sibling to the text is its remove button, whose whole content is one " +
      "character, so there is nothing there a break opportunity can act on",
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
  // `>=`, not `>`: a `nowrap` in the SAME block as the break opportunity defeats it just as
  // completely as one in a later block, and reads even more like a fix than it does.
  const defeatedAfterGrant =
    grantedAt === null ? [] : defeats.filter((d) => d.at >= (grantedAt as number));
  return { grantedAt, defeats, defeatedAfterGrant };
}

const named = (site: Site) => `${site.selectors.join(" / ")} (${site.what})`;

describe("the stylesheet: text the operator did not choose", () => {
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
    // while they all stayed green. If a selector was renamed, rename it here too.
    expect(missing).toEqual([]);
  });

  it("still renders every class the table names, in every file it names", () => {
    const gone: string[] = [];
    for (const site of SITES) {
      // A whole token, not a substring: `includes("gate-detail")` matches `"gate-detail-x"` too,
      // so a rename, the likeliest way a class and its style part company, would still read as
      // present. `\b` does not help here since the class name itself contains a hyphen.
      const token = new RegExp(String.raw`(?<![\w-])${site.classInTsx}(?![\w-])`);
      for (const file of site.seenIn) {
        const source = readFileSync(join(HERE, file), "utf8");
        if (!token.test(source)) {
          gone.push(`${file} no longer renders "${site.classInTsx}" (${site.what})`);
        }
      }
    }
    // A surface that is removed takes its style, and its row here, with it.
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
          `${named(site)} -- ${d.prop}: ${d.value} at ${siteOf(d.at)} defeats the ` +
            `break opportunity at ${siteOf(grantedAt as number)}`,
        );
      }
    }
    // A `nowrap` or an ellipsis makes `overflow-wrap` a no-op, so a site can carry the
    // declaration and still paint out of its box.
    expect(undone).toEqual([]);
  });

  it("grants it on the text, not on the flex row around the text", () => {
    const misplaced: string[] = [];
    const stale: string[] = [];
    for (const site of SITES) {
      const { grantedAt } = resolve(site);
      if (grantedAt === null) continue;
      const block = RULES.find((r) => r.at === grantedAt) as Rule;
      const display = declaredValue(block.body, "display") ?? "";
      const isRow = /flex|grid/.test(display);
      if (isRow && !site.onAFlexRow) {
        misplaced.push(
          `${named(site)} -- the break opportunity at ${siteOf(grantedAt)} is on a ` +
            `\`display: ${display}\` container, so every child inherits it. Move it to the ` +
            `element holding the text, or say here why no sibling is hurt`,
        );
      }
      if (!isRow && site.onAFlexRow) {
        stale.push(`${named(site)} is not a flex row now -- drop its \`onAFlexRow\` reason`);
      }
    }
    // `anywhere` counts its break opportunities in min-content sizing, unlike `break-word`, so a
    // flex item that inherits it gets an automatic minimum width of one character, and the row
    // is free to squeeze it down to that width.
    expect(misplaced).toEqual([]);
    expect(stale).toEqual([]);
  });

  it("keeps every recorded exception an exception", () => {
    const stale: string[] = [];
    for (const site of SITES.filter((s) => s.exempt)) {
      if (resolve(site).grantedAt !== null) {
        stale.push(`${named(site)} wraps now -- drop its \`exempt\` reason, it is no longer true`);
      }
    }
    // The table must be accurate in both directions: an exemption left in place after a site is
    // fixed vouches, in writing, for the opposite of what actually ships.
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
    // A flag-shaped assertion cannot tell a member that complies from one that fell out of the
    // walk, so the size of the walk is pinned by hand: 35 sites in the table, 4 of them exempt.
    expect(SITES.length).toBe(35);
    expect(SITES.filter((s) => s.exempt).length).toBe(4);
    // This also counts the CSS blocks each site actually resolves to. Most sites match one
    // block; a few match more than one, for a total of 43. If this number moves, a selector
    // joined or left a row: check that the change was intended, then update the number.
    const blocks = SITES.reduce((n, s) => n + matchesOf(s).length, 0);
    expect(blocks).toBe(43);
  });
});

// A guard reading source text is bounded by the syntax it can parse, so the parser is run
// against every spelling the stylesheet actually uses before it is trusted with the checks
// above.
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
