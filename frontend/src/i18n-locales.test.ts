// SPDX-License-Identifier: AGPL-3.0-or-later
// @vitest-environment node
//
// Checks the translated catalogs that Weblate writes. Weblate writes every
// `locales/<tag>/ui.json` except the English one, and opens its own pull request for each
// change, so this test reviews that pull request automatically: a translation that breaks an
// ICU plural, renames an argument, or adds a tag no page renders turns the check red with the
// key named, and nobody here has to read the language. `shippedTag` in i18n.ts decides what
// the app does with a passing catalog, and is tested below against a fake set of tags.
//
// Unlike the manual's drift gate, there is no pinned catalog count here: a catalog arrives in a
// pull request Weblate opens from its own fork, and nobody here can update a pinned count on
// it. Instead the checker itself is tested directly: it runs against a broken fixture for each
// problem it claims to catch, the message reader is tested against every ICU form the catalog
// can use, and the English catalog it compares against is checked to be non-empty.
//
// Argument and tag names are read from the parsed ICU message, never matched with a regex:
// `{n, plural, one {# file} other {files}}` has one argument, and a regex that just looks for
// braces would misread `files` as a second one.

import { TYPE, type MessageFormatElement } from "@formatjs/icu-messageformat-parser";
import { IntlMessageFormat } from "intl-messageformat";
import { describe, expect, it } from "vitest";

import { LANGUAGES, preferredLanguage, SHIPPED_TAGS, shippedTag } from "./i18n";
import en from "./locales/en/ui.json";
import { leaves } from "./test/catalog";

type Catalog = Record<string, string>;

/** Every translated catalog the build ships, keyed by tag, loaded eagerly. Uses the same glob
 *  pattern as the real loader in i18n.ts; the test below checks both globs match the same set
 *  of tags. */
const SHIPPED: Record<string, Catalog> = Object.fromEntries(
  Object.entries(
    import.meta.glob<{ default: unknown }>(["./locales/*/ui.json", "!./locales/en/ui.json"], {
      eager: true,
    }),
  ).map(([path, mod]) => [path.split("/")[2] ?? path, leaves(mod.default)]),
);

const EN: Catalog = leaves(en);

type Names = { args: Set<string>; tags: Set<string> };

/** Returns the argument names and tag names a parsed message uses, including names used
 *  inside branches and tag bodies. */
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

/** Lists what's wrong with `translated` as a stand-in for `source`, one line per problem, or
 *  an empty list. An empty message counts as untranslated and safely falls back to English
 *  (i18n.ts sets `returnEmptyString: false`), so the checker skips it. */
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
    // A translation may drop a tag or an argument. It may not invent a new one.
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
      // The directory name becomes the page's `lang` attribute, so it must be a tag Intl
      // accepts, spelled canonically: `pt-BR`, never `pt_BR` or `pt-br`.
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

describe("preferredLanguage", () => {
  it("only ever answers with a tag the Settings picker offers", () => {
    // This value is seeded to the server, and the picker's `<select>` renders blank if the
    // value isn't one of its own options. `i18next` initializes with the tag `"en-US"`, but the
    // picker spells English `"en"`, so returning the raw init value would show a blank picker.
    expect(LANGUAGES).toContain(preferredLanguage());
  });
});
