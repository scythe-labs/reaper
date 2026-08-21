// SPDX-License-Identifier: AGPL-3.0-or-later
// @vitest-environment node
//
// Stage 5's gate (docs/I18N_PLAN.md): a translated manual is the English manual with every
// word replaced and nothing else changed. The modal falls back to English entire rather than
// mixing, so the one thing a locale module can get wrong is its shape: a doc dropped, a
// section id respelled (a deep link from the app then lands nowhere), a table row lost, a
// diagram node's tone gone. None of those is a type error, so this is the check.
//
// `skeleton` keeps every identifier and every structural field and replaces every prose string
// with a mark, so two manuals compare equal exactly when they differ only in their words. Its
// switch is exhaustive over `Block` with no default: a kind added to `blocks.ts` fails to
// compile here before a manual can carry it unchecked.

import { describe, expect, it } from "vitest";

import type { Block, DiagramNode, Doc } from "./blocks";
import { ENGLISH, LOCALE_MODULES, manualLoader } from "./localized";
import { DOCS } from "./registry";
import { MANUALS } from "../test/manuals";

/** A prose string's mark: present, or empty. An empty translation is a lost sentence. */
const word = (s: string) => (s.trim() === "" ? "" : "_");

function nodeSkeleton(n: DiagramNode): DiagramNode {
  return { ...n, text: word(n.text), ...(n.sub === undefined ? {} : { sub: word(n.sub) }) };
}

function blockSkeleton(b: Block): Block {
  switch (b.kind) {
    case "h":
    case "p":
    case "callout":
      return { ...b, text: word(b.text) };
    case "list":
      return { ...b, items: b.items.map(word) };
    case "steps":
      return { ...b, items: b.items.map((s) => ({ title: word(s.title), text: word(s.text) })) };
    case "table":
      return { ...b, head: b.head.map(word), rows: b.rows.map((r) => r.map(word)) };
    case "diagram":
      return {
        ...b,
        ...(b.title === undefined ? {} : { title: word(b.title) }),
        ...(b.legend === undefined
          ? {}
          : { legend: b.legend.map((l) => ({ ...l, text: word(l.text) })) }),
        steps: b.steps.map((s) => ({
          node: nodeSkeleton(s.node),
          ...(s.enter === undefined
            ? {}
            : {
                enter: {
                  ...(s.enter.label === undefined ? {} : { label: word(s.enter.label) }),
                  ...(s.enter.phase === undefined ? {} : { phase: word(s.enter.phase) }),
                },
              }),
          ...(s.branch === undefined
            ? {}
            : {
                branch: {
                  ...(s.branch.label === undefined ? {} : { label: word(s.branch.label) }),
                  node: nodeSkeleton(s.branch.node),
                },
              }),
        })),
      };
  }
}

function skeleton(doc: Doc): Doc {
  return {
    ...doc,
    title: word(doc.title),
    summary: word(doc.summary),
    body: doc.body.map(blockSkeleton),
  };
}

describe("every translated manual is the English manual with its words replaced", () => {
  // Rule 145: the population the walk collects, pinned. A locale landing grows this list by
  // hand, which is the one moment someone confirms the glob saw it.
  it("finds the manuals the build ships", () => {
    expect(MANUALS.map((m) => m.lng)).toEqual(["en"]);
  });

  // The directory's name is the tag the pane's `lang` carries verbatim, and axe fails a tag
  // Intl does not know (valid-lang), so `content/german/` fails here instead of in the audit.
  it("names each manual by a language tag", () => {
    for (const m of MANUALS) expect(() => Intl.getCanonicalLocales(m.lng), m.lng).not.toThrow();
  });

  it("sees the same modules lazily in the app as eagerly here", () => {
    expect(Object.keys(LOCALE_MODULES).sort()).toEqual(
      MANUALS.filter((m) => m !== ENGLISH)
        .map((m) => `./content/${m.lng}/index.ts`)
        .sort(),
    );
  });

  it("keeps every doc, section id, table and diagram of the English one", () => {
    const english = DOCS.map(skeleton);
    for (const m of MANUALS) expect(m.docs.map(skeleton), m.lng).toEqual(english);
  });
});

// Rule 147: the check compares a shape it computes, so it is proven against a translation it
// must accept and against each change it must refuse, not only against English versus itself.
describe("the shape check", () => {
  const clone = (): Doc[] => structuredClone(DOCS);
  const english = DOCS.map(skeleton);
  const policy = (docs: Doc[]) => docs.find((d) => d.id === "understanding-policy") as Doc;
  const firstOf = <K extends Block["kind"]>(docs: Doc[], kind: K) =>
    docs.flatMap((d) => d.body).find((b): b is Extract<Block, { kind: K }> => b.kind === kind) as
      Extract<Block, { kind: K }> | undefined;

  it("accepts a manual that changed only its words", () => {
    const docs = clone();
    for (const doc of docs) {
      doc.title = `T ${doc.title}`;
      doc.summary = `S ${doc.summary}`;
    }
    const table = firstOf(docs, "table");
    const diagram = firstOf(docs, "diagram");
    const step = firstOf(docs, "steps");
    expect(table && diagram && step).toBeTruthy();
    table!.head[0] = "Kopf";
    table!.rows[0]![0] = "Zelle";
    diagram!.steps[0]!.node.text = "Knoten";
    step!.items[0]!.title = "Schritt";
    expect(docs.map(skeleton)).toEqual(english);
  });

  it.each<[string, (docs: Doc[]) => void]>([
    ["a doc is missing", (docs) => void docs.pop()],
    ["a doc moved groups", (docs) => void (policy(docs).group = "Safety")],
    [
      "a section id is respelled",
      (docs) => {
        const h = policy(docs).body.find((b) => b.kind === "h" && b.id === "in-a-policy");
        if (h?.kind === "h") h.id = "in-einer-policy";
      },
    ],
    ["a sentence is empty", (docs) => void (firstOf(docs, "p")!.text = "  ")],
    ["a table lost a row", (docs) => void firstOf(docs, "table")!.rows.pop()],
    ["a list lost an item", (docs) => void firstOf(docs, "list")!.items.pop()],
    ["a callout changed tone", (docs) => void (firstOf(docs, "callout")!.tone = "caution")],
    [
      "a diagram branch lost its tone",
      (docs) => {
        const toned = docs
          .flatMap((d) => d.body)
          .flatMap((b) => (b.kind === "diagram" ? b.steps : []))
          .find((s) => s.branch?.node.tone);
        delete toned!.branch!.node.tone;
      },
    ],
    [
      "steps became a list",
      (docs) => {
        const doc = docs.find((d) => d.body.some((b) => b.kind === "steps")) as Doc;
        doc.body = doc.body.map((b) =>
          b.kind === "steps" ? { kind: "list", items: b.items.map((s) => s.title) } : b,
        );
      },
    ],
  ])("refuses a manual where %s", (_name, mutate) => {
    const docs = clone();
    mutate(docs);
    expect(docs.map(skeleton)).not.toEqual(english);
  });
});

describe("the loader", () => {
  const de: Doc[] = [{ ...DOCS[0]!, title: "Überblick" }];
  const pt: Doc[] = [{ ...DOCS[0]!, title: "Visão geral" }];
  const load = manualLoader({
    "./content/de/index.ts": () => Promise.resolve({ DOCS: de }),
    "./content/pt-BR/index.ts": () => Promise.resolve({ DOCS: pt }),
    "./content/xx/index.ts": () => Promise.reject(new Error("chunk lost")),
  });

  it("serves English synchronously when no manual ships for the locale", () => {
    // Not a promise: the modal must never suspend for the default locale.
    expect(load("fr")).toBe(ENGLISH);
    expect(load("en-US")).toBe(ENGLISH);
  });

  it("serves the region's manual, else the language's", async () => {
    await expect(load("pt-BR")).resolves.toEqual({ lng: "pt-BR", docs: pt });
    await expect(load("de-CH")).resolves.toEqual({ lng: "de", docs: de });
  });

  it("hands use() the same promise on every render", () => {
    expect(load("de")).toBe(load("de"));
    expect(load("de")).toBe(load("de-AT"));
  });

  it("serves English when the module fails to load", async () => {
    await expect(load("xx")).resolves.toBe(ENGLISH);
  });
});
