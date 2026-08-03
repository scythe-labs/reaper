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

/** The first line of every generated page. It is what marks a file as machine-written, so
 *  `manual.pages.test.ts` can tell a generated page from a hand-written one sitting beside it in
 *  the same directory. Identifying them by directory instead would force the manual's shape to
 *  follow the app's grouping, and install notes belong next to the overview whether or not the
 *  app happens to render one of them. */
export const GENERATED_MARKER = "{/* GENERATED FILE. Do not edit.";

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
  // The backslash is in the class so a literal one cannot pair with the next character
  // as an MDX escape; one pass keeps the escaping characters from re-escaping each other.
  return text
    .split(/(`[^`]*`)/g)
    .map((part) => (part.startsWith("`") ? part : part.replace(/[\\{}<>]/g, (c) => `\\${c}`)))
    .join("");
}

/** A double-quoted YAML scalar for the front matter. JSON's quoting rules are a subset of
 *  YAML's, so this is safe for any title or summary. */
function yaml(value: string): string {
  return JSON.stringify(value);
}

/** A JSX attribute, written as an expression rather than a quoted literal.
 *
 *  `title="Read a few \"why\" panels"` is NOT valid JSX: an attribute delimited by quotes has no
 *  backslash escape, so MDX stops on the backslash and reports an unexpected character in an
 *  attribute name, pointing at a column in a generated file nobody wrote. Inside `{…}` the value
 *  is ordinary JavaScript, where JSON quoting is exactly right. One of the five docs already
 *  carries a quoted phrase in a step title, so this is the shipped case, not a hypothetical. */
function jsxAttr(value: string): string {
  return `{${JSON.stringify(value)}}`;
}

/** A block-level child of a JSX element, blank-line padded so MDX parses the markdown inside it
 *  rather than treating it as raw text. */
function wrap(open: string, body: string, close: string): string {
  return `${open}\n\n${body}\n\n${close}`;
}

/** One table cell. A pipe would end the column, so it is escaped.
 *
 *  Outside a code span that is all it takes, because `escapeText` has already doubled every
 *  backslash and the inline parser undoes exactly that. Inside one it is not: a row is split
 *  into cells BEFORE code spans are parsed, the splitter consumes one backslash and the code
 *  span keeps the rest verbatim, so a run of `c` backslashes written before a pipe arrives
 *  intact only while `c` is even. An odd run leaves the pipe unescaped, and the row silently
 *  becomes two columns.
 *
 *  No encoding renders an odd run — `c` and `c + 2` are the neighbors GFM can spell, never
 *  `c + 1` — so this refuses the cell instead of publishing a table that lost a column. Same
 *  call `manualPath` makes on an unmapped group: reject over invent.
 *
 *  CodeQL reads the `replace` below as the sanitizer and reports the backslash it leaves alone
 *  (`js/incomplete-sanitization`, dismissed as a false positive). The backslash was escaped one
 *  call earlier, by `escapeText`; escaping it again here is not the missing half of that pass
 *  but a second one, measured against remark-gfm as 14 of 18 round-trips broken. The query also
 *  assumes untrusted input, and every cell here is typed doc content compiled into the app. */
function cell(text: string): string {
  for (const part of text.split(/(`[^`]*`)/g)) {
    if (!part.startsWith("`")) continue;
    for (const match of part.matchAll(/(\\*)\|/g)) {
      if ((match[1] ?? "").length % 2 === 1) {
        throw new Error(
          `Table cell ${JSON.stringify(text)} has a code span with an odd run of backslashes ` +
            "before a pipe. A GFM table cannot carry that: the row would split into two " +
            "columns. Reword the cell, or move the pipe out of the code span.",
        );
      }
    }
  }
  return escapeText(text).replace(/\|/g, "\\|");
}

function tableToMdx(b: TableBlock): string {
  // A markdown table stays greppable and diffable, which a JSON prop would not. `hi` marks the
  // shipped-default column, and markdown has no column emphasis, so it lands on that header
  // cell: the same fact the app paints, in the one place markdown can carry it.
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
      return wrap(`<Callout tone=${jsxAttr(b.tone)}>`, escapeText(b.text), "</Callout>");
    case "list":
      return b.items
        .map((it, i) => `${b.ordered === true ? `${i + 1}.` : "-"} ${escapeText(it)}`)
        .join("\n");
    case "steps":
      return wrap(
        "<Steps>",
        b.items
          .map((s) => wrap(`<Step title=${jsxAttr(s.title)}>`, escapeText(s.text), "</Step>"))
          .join("\n\n"),
        "</Steps>",
      );
    case "table":
      return tableToMdx(b);
    case "defs":
      return wrap(
        "<Definitions>",
        b.items
          .map((d) => wrap(`<Def term=${jsxAttr(d.term)}>`, escapeText(d.text), "</Def>"))
          .join("\n\n"),
        "</Definitions>",
      );
    case "diagram":
      return diagramToMdx(b);
  }
}

/** The whole page: Docusaurus front matter, a banner naming the generator, then the blocks.
 *
 *  `position` orders the page within its sidebar category. The site's sidebar is autogenerated
 *  from the directory, so a new doc appears there with no second list to update (rule 103's
 *  hazard, avoided rather than guarded); the caller passes the doc's place in `DOCS`, which is
 *  the order the app's own index already uses. Hand-written pages carry their own
 *  `sidebar_position` and interleave with these. */
export function docToMdx(doc: Doc, position = 1): string {
  const front = [
    "---",
    `id: ${yaml(doc.id)}`,
    `title: ${yaml(doc.title)}`,
    `description: ${yaml(doc.summary)}`,
    `sidebar_position: ${position}`,
    "---",
  ].join("\n");

  // Rule 68: an artifact that says it is generated names the committed script that makes it.
  // The doc is named rather than its file, because the two do not match and deriving one from
  // the other would be a guess: `how-a-delete-is-kept-safe` lives in `deletionSafety.ts`.
  const banner = [
    GENERATED_MARKER,
    `    Source:     the ${JSON.stringify(doc.id)} doc in frontend/src/docs/content/`,
    "    Regenerate: npm --prefix frontend run gen-manual */}",
  ].join("\n");

  const body = doc.body.map(blockToMdx).join("\n\n");
  return `${front}\n\n${banner}\n\n${body}\n`;
}
