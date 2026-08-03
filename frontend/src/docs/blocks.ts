// SPDX-License-Identifier: AGPL-3.0-or-later
//
// The docs are structured content, not markdown. A doc is a list of typed blocks, each
// rendered by DocBody with the app's own classes, so a table or a callout in the docs looks
// exactly like one in the app and no markdown dependency has to be vetted or pinned.
//
// Adding a doc is one object in a content file plus one line in registry.ts. Adding a NEW
// kind of content is one variant here and one case in DocBody. That is the whole extension
// story: keep authoring terse (the constructors below), keep rendering in one place.
//
// Inline emphasis inside any string is a deliberately tiny subset: `**bold**` and
// `` `code` ``. DocBody parses it into React nodes (never innerHTML), so a doc string can
// never inject markup. Anything richer is a new block, not new inline syntax.

export type CalloutTone = "tip" | "note" | "caution";

/** A heading. `sub` renders an h3 (a subsection); otherwise an h2 that the index lists as a
 *  jump target. An `id` makes it linkable with openDoc(docId, id). */
export type Heading = { kind: "h"; text: string; id?: string; sub?: boolean };
export type Para = { kind: "p"; text: string };
export type Callout = { kind: "callout"; tone: CalloutTone; text: string };
export type ListBlock = { kind: "list"; ordered?: boolean; items: string[] };
export type StepItem = { title: string; text: string };
export type Steps = { kind: "steps"; items: StepItem[] };
/** A table. `hi` is the zero-based column to emphasize (the shipped-default column). */
export type TableBlock = { kind: "table"; head: string[]; rows: string[][]; hi?: number };
export type DefItem = { term: string; text: string };
export type Defs = { kind: "defs"; items: DefItem[] };

/** A flowchart, drawn from the app's own tokens (no Mermaid / vendor dependency). It is a
 *  vertical spine of nodes with optional side branches, so a safety flow reads at a glance
 *  and re-themes in light/dark like everything else. `tone` speaks Reaper's verdict language:
 *  `keep` = the file is kept (green), `stop` = the run stops (red). Endpoints stay neutral. */
export type DiagramTone = "keep" | "stop";
export type DiagramShape = "process" | "decision" | "terminal";
export type DiagramNode = { text: string; sub?: string; shape?: DiagramShape; tone?: DiagramTone };
/** The connector leading INTO a step from the one above: `label` rides the arrow (yes / no /
 *  ok), `phase` prints a divider first (e.g. "checked once, before any file"). */
export type DiagramEnter = { label?: string; phase?: string };
export type DiagramBranch = { label?: string; node: DiagramNode };
export type DiagramStep = { node: DiagramNode; enter?: DiagramEnter; branch?: DiagramBranch };
export type DiagramBlock = {
  kind: "diagram";
  title?: string;
  legend?: { tone: DiagramTone; text: string }[];
  steps: DiagramStep[];
};

export type Block = Heading | Para | Callout | ListBlock | Steps | TableBlock | Defs | DiagramBlock;

export type Doc = {
  id: string;
  /** The group the index files it under, e.g. "Policy". Order comes from registry.ts. */
  group: string;
  title: string;
  summary: string;
  body: Block[];
};

// ----- terse constructors, so a content file reads like prose -----

// The conditional spreads keep id / hi genuinely absent (not present-as-undefined) when
// omitted, which exactOptionalPropertyTypes requires.
export const h2 = (text: string, id?: string): Heading => ({
  kind: "h",
  text,
  ...(id !== undefined ? { id } : {}),
});
export const h3 = (text: string, id?: string): Heading => ({
  kind: "h",
  text,
  sub: true,
  ...(id !== undefined ? { id } : {}),
});
export const p = (text: string): Para => ({ kind: "p", text });
export const callout = (tone: CalloutTone, text: string): Callout => ({
  kind: "callout",
  tone,
  text,
});
export const ul = (items: string[]): ListBlock => ({ kind: "list", items });
export const ol = (items: string[]): ListBlock => ({ kind: "list", ordered: true, items });
export const steps = (items: StepItem[]): Steps => ({ kind: "steps", items });
export const table = (head: string[], rows: string[][], hi?: number): TableBlock => ({
  kind: "table",
  head,
  rows,
  ...(hi !== undefined ? { hi } : {}),
});
export const defs = (items: DefItem[]): Defs => ({ kind: "defs", items });
export const diagram = (spec: Omit<DiagramBlock, "kind">): DiagramBlock => ({
  kind: "diagram",
  ...spec,
});

/** The h2 headings a doc exposes as jump targets, for the index sub-list and deep links. */
export function docSections(doc: Doc): { id: string; text: string }[] {
  return doc.body
    .filter((b): b is Heading => b.kind === "h" && !b.sub && typeof b.id === "string")
    .map((b) => ({ id: b.id as string, text: b.text }));
}
