// SPDX-License-Identifier: AGPL-3.0-or-later
//
// One `Doc` becomes one MDX page for the manual site. The typed blocks stay the source: the app
// renders them through `DocBody`, and this renders the same blocks as MDX so a second copy of the
// words never has to be written by hand. Every block kind here has a case in `DocBody`, and a
// kind added to `blocks.ts` without a case here fails `manual.gen.test.ts` rather than silently
// dropping out of the manual.
//
// The site supplies the components this emits (`Callout`, `Steps`, `Diagram`, …) through
// Docusaurus's `src/theme/MDXComponents.tsx`, which resolves a capitalized tag with no import in
// the page. That is what lets one set of words carry the app's chrome in one place and the
// site's in the other.

import type { Block, Doc, DiagramBlock, TableBlock } from "./blocks";

/** The directory a group files under, so the site's sidebar mirrors the app's index. */
const GROUP_DIR: Record<string, string> = {
  "Getting started": "getting-started",
  Policy: "policy",
  Safety: "safety",
};

/** Where a doc's page lives, relative to the manual root. Throws on an unmapped group rather
 *  than inventing a directory: a new group is a sidebar decision, not a slugify call. */
export function manualPath(doc: Doc): string {
  const dir = GROUP_DIR[doc.group];
  if (dir === undefined) {
    throw new Error(
      `No manual directory for group ${JSON.stringify(doc.group)} (doc ${doc.id}). ` +
        `Add it to GROUP_DIR in toMdx.ts.`,
    );
  }
  return `${dir}/${doc.id}.mdx`;
}

/** MDX reads `{` as an expression and `<` as a tag. Neither appears in the docs today, and both
 *  are one ordinary sentence away, so escape them before they become a build failure nobody can
 *  place. Code spans are left alone: MDX does not interpolate inside them, and escaping there
 *  would put a backslash on the operator's screen. */
function escapeText(text: string): string {
  return text
    .split(/(`[^`]*`)/g)
    .map((part) => (part.startsWith("`") ? part : part.replace(/[{}<>]/g, (c) => `\\${c}`)))
    .join("");
}

/** A JSX attribute value. JSON quoting handles the quotes and backslashes; the text is a plain
 *  string prop, so the MDX escaping above does not apply inside it. */
function attr(value: string): string {
  return JSON.stringify(value);
}

/** A block-level child of a JSX element, blank-line padded so MDX parses the markdown inside it
 *  rather than treating it as raw text. */
function wrap(open: string, body: string, close: string): string {
  return `${open}\n\n${body}\n\n${close}`;
}

function tableToMdx(b: TableBlock): string {
  // A markdown table stays greppable and diffable, which a JSON prop would not. `hi` marks the
  // shipped-default column, and markdown has no column emphasis, so it lands on that header
  // cell: the same fact the app paints, in the one place markdown can carry it.
  const cell = (text: string) => escapeText(text).replace(/\|/g, "\\|");
  const head = b.head.map((h, i) => (i === b.hi ? `**${cell(h)}**` : cell(h)));
  const rows = b.rows.map((r) => `| ${b.head.map((_, i) => cell(r[i] ?? "")).join(" | ")} |`);
  return [`| ${head.join(" | ")} |`, `| ${b.head.map(() => "---").join(" | ")} |`, ...rows].join(
    "\n",
  );
}

function diagramToMdx(b: DiagramBlock): string {
  // The one block with no markdown equivalent: shapes, tones and branch labels are structure, not
  // prose. It goes over as a prop, and the site draws it with the same vocabulary `DocBody` uses.
  // The specs are a few hundred bytes, well under the size where a JSON prop slows MDX down.
  const { kind: _kind, ...spec } = b;
  return `<Diagram spec={${JSON.stringify(spec)}} />`;
}

function blockToMdx(b: Block): string {
  switch (b.kind) {
    case "h": {
      // Docusaurus's explicit heading id, so a deep link the app already uses (`openDoc(id,
      // anchor)`) lands on the same section here.
      const anchor = b.id === undefined ? "" : ` {#${b.id}}`;
      return `${b.sub === true ? "###" : "##"} ${escapeText(b.text)}${anchor}`;
    }
    case "p":
      return escapeText(b.text);
    case "callout":
      return wrap(`<Callout tone=${attr(b.tone)}>`, escapeText(b.text), "</Callout>");
    case "list":
      return b.items
        .map((it, i) => `${b.ordered === true ? `${i + 1}.` : "-"} ${escapeText(it)}`)
        .join("\n");
    case "steps":
      return wrap(
        "<Steps>",
        b.items
          .map((s) => wrap(`<Step title=${attr(s.title)}>`, escapeText(s.text), "</Step>"))
          .join("\n\n"),
        "</Steps>",
      );
    case "table":
      return tableToMdx(b);
    case "defs":
      return wrap(
        "<Definitions>",
        b.items
          .map((d) => wrap(`<Def term=${attr(d.term)}>`, escapeText(d.text), "</Def>"))
          .join("\n\n"),
        "</Definitions>",
      );
    case "diagram":
      return diagramToMdx(b);
  }
}

/** The whole page: Docusaurus front matter, a banner naming the generator, then the blocks. */
export function docToMdx(doc: Doc): string {
  const front = [
    "---",
    `id: ${attr(doc.id)}`,
    `title: ${attr(doc.title)}`,
    `description: ${attr(doc.summary)}`,
    "---",
  ].join("\n");

  // Rule 68: an artifact that says it is generated names the committed script that makes it.
  // The doc is named rather than its file, because the two do not match and deriving one from
  // the other would be a guess: `how-a-delete-is-kept-safe` lives in `deletionSafety.ts`.
  const banner = [
    "{/* GENERATED FILE. Do not edit.",
    `    Source:     the ${JSON.stringify(doc.id)} doc in frontend/src/docs/content/`,
    "    Regenerate: npm --prefix frontend run gen-manual */}",
  ].join("\n");

  const body = doc.body.map(blockToMdx).join("\n\n");
  return `${front}\n\n${banner}\n\n${body}\n`;
}
