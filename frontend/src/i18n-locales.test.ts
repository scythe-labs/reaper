// SPDX-License-Identifier: AGPL-3.0-or-later
// @vitest-environment node
//
// Stage 6's gate over the translated catalogs (docs/history/I18N_PLAN.md). Weblate writes every
// `locales/<tag>/ui.json` but the English one and opens the pull request itself, so this is
// the review of that pull request: a translation that breaks an ICU plural, renames an
// argument, or adds a tag no render site draws turns the check red with the key named, and
// nobody here has to read the language. What the app does with a passing catalog is
// `shippedTag` in i18n.ts, driven below over a fake set of tags.
//
// No pinned catalog count, unlike the manual's gate: a catalog arrives in a pull request
// Weblate opens from its own fork, which nobody here can push a bumped pin onto. The walk is
// proven the other way (rule 145): the checker runs over a broken fixture for each thing it
// claims to catch, the reader over every ICU form the catalog writes (rule 147), and the
// English catalog it reads against is asserted non-empty.
//
// Names are read from the ICU parse, never from a regex: `{n, plural, one {# file} other
// {files}}` has one argument, and a brace matcher read `files` as a second (rule 147).

import { TYPE, type MessageFormatElement } from "@formatjs/icu-messageformat-parser";
import { IntlMessageFormat } from "intl-messageformat";
import { describe, expect, it } from "vitest";

import { SHIPPED_TAGS, shippedTag } from "./i18n";
import en from "./locales/en/ui.json";
import { leaves } from "./test/catalog";

type Catalog = Record<string, string>;

/** Every translated catalog the build ships, by tag, read eagerly. The pattern is the
 *  loader's (i18n.ts), and the test below holds the two globs to the same tags. */
const SHIPPED: Record<string, Catalog> = Object.fromEntries(
  Object.entries(
    import.meta.glob<{ default: unknown }>(["./locales/*/ui.json", "!./locales/en/ui.json"], {
      eager: true,
    }),
  ).map(([path, mod]) => [path.split("/")[2] ?? path, leaves(mod.default)]),
);

const EN: Catalog = leaves(en);

type Names = { args: Set<string>; tags: Set<string> };

/** The argument names and the tag names a parsed message uses, branches and tag bodies
 *  included. */
function namesIn(ast: MessageFormatElement[], into: Names = { args: new Set(), tags: new Set() }) {
  for (const el of ast) {
    switch (el.type) {
      case TYPE.literal:
      case TYPE.pound:
        break;
      case TYPE.tag:
        into.tags.add(el.value);
        namesIn(el.children, into);
        break;
      case TYPE.plural:
      case TYPE.select:
        into.args.add(el.value);
        for (const option of Object.values(el.options)) namesIn(option.value, into);
        break;
      default:
        into.args.add(el.value);
    }
  }
  return into;
}

const parse = (message: string, tag: string) => new IntlMessageFormat(message, tag).getAst();

/** What is wrong with `translated` standing in for `source`, one line per finding, or
 *  nothing. An empty message is an untranslated one and serves English (i18n.ts sets
 *  `returnEmptyString: false`), so it is not a finding. */
export function catalogProblems(source: Catalog, translated: Catalog, tag: string): string[] {
  const problems: string[] = [];
  for (const [key, message] of Object.entries(translated)) {
    const original = source[key];
    if (original === undefined) {
      problems.push(`${key}: not in the English catalog`);
      continue;
    }
    if (message === "") continue;
    let names: Names;
    try {
      names = namesIn(parse(message, tag));
    } catch (err) {
      problems.push(`${key}: ${String(err)}`);
      continue;
    }
    const known = namesIn(parse(original, "en-US"));
    const args = [...names.args].filter((n) => !known.args.has(n));
    if (args.length > 0) {
      problems.push(`${key}: argument {${args.join("}, {")}} is not in the English message`);
    }
    const tags = [...names.tags].filter((n) => !known.tags.has(n));
    if (tags.length > 0) {
      problems.push(`${key}: tag <${tags.join(">, <")}> is not in the English message`);
    }
  }
  return problems;
}

describe("the translated catalogs", () => {
  it("reads a non-empty English catalog", () => {
    expect(Object.keys(EN).length).toBeGreaterThan(1000);
  });

  it("fails on each thing the checker claims to catch (rule 145)", () => {
    const source = {
      a: "{n, plural, one {# file} other {files}}",
      b: "Keep <btn>going</btn>",
      c: "Plain",
    };
    expect(catalogProblems(source, source, "de")).toEqual([]);
    expect(catalogProblems(source, { c: "" }, "de")).toEqual([]);
    expect(catalogProblems(source, { d: "x" }, "de")).toEqual(["d: not in the English catalog"]);
    expect(catalogProblems(source, { a: "{n, plural" }, "de")).toHaveLength(1);
    expect(
      catalogProblems(source, { a: "{count, plural, one {# Datei} other {Dateien}}" }, "de"),
    ).toEqual(["a: argument {count} is not in the English message"]);
    expect(catalogProblems(source, { b: "Weiter <b>so</b>" }, "de")).toEqual([
      "b: tag <b> is not in the English message",
    ]);
    // A translation may drop a tag or an argument; it may not invent one.
    expect(catalogProblems(source, { a: "Dateien", b: "Weiter" }, "de")).toEqual([]);
  });

  it("reads every ICU form the catalog writes (rule 147)", () => {
    const read = namesIn(
      parse(
        "{n} {d, date, short} {n, plural, one {{x} day} other {# days}} " +
          "{k, select, a {A} other {B}} <btn>go {m}</btn> '{'literal'}'",
        "en-US",
      ),
    );
    expect(read.args).toEqual(new Set(["n", "d", "x", "k", "m"]));
    expect(read.tags).toEqual(new Set(["btn"]));
  });

  it("every shipped catalog is one the app can serve in place of English", () => {
    // The lazy glob the app loads from and the eager one read here name the same tags.
    expect(new Set(Object.keys(SHIPPED))).toEqual(SHIPPED_TAGS);
    for (const [tag, catalog] of Object.entries(SHIPPED)) {
      // The directory is the tag the page's `lang` will say, so it is one Intl accepts,
      // spelled canonically: `pt-BR`, never `pt_BR` or `pt-br` (valid-lang).
      expect(() => Intl.getCanonicalLocales(tag), tag).not.toThrow();
      expect(Intl.getCanonicalLocales(tag)[0], tag).toBe(tag);
      expect(catalogProblems(EN, catalog, tag), tag).toEqual([]);
    }
  });
});

describe("shippedTag", () => {
  const shipped = new Set(["de", "pt-BR"]);

  it("serves the exact tag, then the language, in the browser's order", () => {
    expect(shippedTag(["pt-BR"], shipped)).toBe("pt-BR");
    expect(shippedTag(["de-CH"], shipped)).toBe("de");
    expect(shippedTag(["pt-PT", "de"], shipped)).toBe("de");
    expect(shippedTag(["fr", "pt-BR"], shipped)).toBe("pt-BR");
  });

  it("keeps English when the browser ranks it first, or nothing shipped matches", () => {
    expect(shippedTag(["en-GB", "de"], shipped)).toBeUndefined();
    expect(shippedTag(["fr"], shipped)).toBeUndefined();
    expect(shippedTag([], shipped)).toBeUndefined();
    expect(shippedTag(["de"], new Set())).toBeUndefined();
  });
});
