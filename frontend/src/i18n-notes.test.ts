// SPDX-License-Identifier: AGPL-3.0-or-later
// @vitest-environment node
//
// #868 phase 6's gate over `locales/en/ui.notes.json`. A translator working from `ui.json`
// alone sees a dotted key and a message with bare `{params}`;
// the notes file is where they learn what a param holds, whether a string is a whole sentence
// or a fragment another key nests, and where the string shows in the app. This gate does not
// judge the WORDING of a note -- only that the required set of keys has exactly one.
//
// The required set is derived, never pasted (rule 145): every key under the eight namespaces a
// translator most needs guided (why., chip., warning., error., the two policyRules.field*
// families, policySim.staleReason., services.discord.testResult.), two one-off leaves outside
// them (services.modal.mapError, lists.plexError), and every OTHER catalog message carrying an
// ICU argument or a tag -- a plain sentence with nothing to interpolate needs no note. "error."
// joins the whole-namespace group rather than relying on the ICU-argument rule alone (phase
// 8b): a coded refusal with no param still needs the translator told which screen it fires on.
// "Carries an argument or a tag" is answered by parsing the message with the same ICU library
// the other two catalog gates use (i18n-keys.test.ts, i18n-locales.test.ts), not by a
// brace-matching regex: an escaped literal brace (`'{'text'}'`) parses as plain text, so a
// regex counting braces would over-collect exactly where rule 147 warns a hand-rolled matcher
// usually fails.

import { TYPE, type MessageFormatElement } from "@formatjs/icu-messageformat-parser";
import { IntlMessageFormat } from "intl-messageformat";
import { describe, expect, it } from "vitest";

import en from "./locales/en/ui.json";
import notes from "./locales/en/ui.notes.json";
import { leaves } from "./test/catalog";

type Catalog = Record<string, string>;
type Notes = Record<string, string>;

const EN: Catalog = leaves(en);
const NOTES: Notes = notes;

/** Namespaces where every key needs a note, whatever its own shape. */
const REQUIRED_NAMESPACES = [
  "why.",
  "chip.",
  "warning.",
  "error.",
  "policyRules.fieldHelp.",
  "policyRules.fieldUnit.",
  "policySim.staleReason.",
  "services.discord.testResult.",
  // Phase 11a: a background job's outcome (jobs.result.*) and a scan's live step
  // (shell.scanBar.step.*) are both typed reasons composed on the browser, the same shape
  // as error. -- a plain-sentence entry with no param still needs the translator told which
  // row or bar it fires on.
  "jobs.result.",
  "shell.scanBar.step.",
];

/** One-off leaves outside those namespaces that carry a raw server `error`/reason param and
 *  need a note for the same reason (api.ts's own docstrings on each field name this file). */
const REQUIRED_EXACT = new Set(["services.modal.mapError", "lists.plexError"]);

/** Whether a message has an ICU argument (simple, number, date, time, plural, select) or a
 *  tag anywhere in it. Every such element is a top-level element of the parsed AST -- ICU
 *  never buries one inside a literal segment -- so no recursion into plural/select branches
 *  is needed to find one. */
export function hasArgOrTag(message: string): boolean {
  if (message === "") return false;
  let ast: MessageFormatElement[];
  try {
    ast = new IntlMessageFormat(message, "en-US").getAst();
  } catch {
    // Unparseable ICU is i18n-keys.test.ts's finding ("every catalog message parses as
    // ICU"), not this gate's; treating it as "nothing to note" here never hides that failure.
    return false;
  }
  return ast.some((el) => el.type !== TYPE.literal && el.type !== TYPE.pound);
}

export function requiredKeysOf(catalog: Catalog): Set<string> {
  const required = new Set<string>();
  for (const [key, message] of Object.entries(catalog)) {
    if (REQUIRED_NAMESPACES.some((ns) => key.startsWith(ns)) || REQUIRED_EXACT.has(key)) {
      required.add(key);
    } else if (hasArgOrTag(message)) {
      required.add(key);
    }
  }
  return required;
}

const HOW_TO_UPDATE =
  "A required key is: every key under why., chip., warning., error., policyRules.fieldHelp., " +
  "policyRules.fieldUnit., policySim.staleReason., services.discord.testResult., " +
  "jobs.result. or shell.scanBar.step., plus the two exact keys services.modal.mapError and " +
  "lists.plexError, plus any OTHER catalog message carrying an ICU argument or a tag. Write " +
  "(or remove) the note in frontend/src/locales/en/ui.notes.json to match, then update " +
  "EXPECTED_REQUIRED_COUNT below.";

//: Pinned so a change to the required set is a diff here, not a silent pass (rule 145) -- the
//: three assertions below already name every individual key that moved, but a set that shrank
//: by exactly as much as it grew would leave them empty while this population changed under
//: everyone's feet.
const EXPECTED_REQUIRED_COUNT = 1149;

describe("translator notes (locales/en/ui.notes.json)", () => {
  it("classifies a plain literal as not required, and an ICU or tagged one as required (rule 145)", () => {
    expect(hasArgOrTag("Plain text.")).toBe(false);
    expect(hasArgOrTag("")).toBe(false);
    expect(hasArgOrTag("{n} files")).toBe(true);
    expect(hasArgOrTag("{n, plural, one {# file} other {# files}}")).toBe(true);
    expect(hasArgOrTag("{k, select, a {A} other {B}}")).toBe(true);
    expect(hasArgOrTag("Keep <btn>going</btn>")).toBe(true);
    // An escaped literal brace is text, not an argument: the ICU parser reads the whole
    // thing as one literal segment, which is exactly why this gate parses rather than
    // brace-matches (rule 147).
    expect(hasArgOrTag("a literal '{' brace '}' and nothing else")).toBe(false);
  });

  it("the required-set rule catches a namespaced key and an argued/tagged one alike, and only those (rule 145)", () => {
    // Seven of the ten required namespaces are exercised here; the other three
    // (services.discord.testResult., jobs.result., shell.scanBar.step.) are proven the same
    // way by the full-catalog tests below, since a literal "services.*.*" fixture string here
    // reads as a Python symbol citation to the repo's own hygiene gate
    // (test_a_dotted_symbol_citation_resolves_to_a_real_symbol) -- the membership check itself
    // is namespace-value-agnostic, so this loses no coverage.
    const fixture: Catalog = {
      "why.anything": "Plain sentence.",
      "chip.anything": "Plain sentence.",
      "warning.anything": "Plain sentence.",
      "error.anything": "Plain sentence.",
      "policyRules.fieldHelp.anything": "Plain sentence.",
      "policyRules.fieldUnit.anything": "days",
      "policySim.staleReason.anything": "Plain sentence.",
      "services.modal.mapError": "Plain sentence.",
      "lists.plexError": "Plain sentence.",
      "other.plain": "A plain sentence with nothing to interpolate.",
      "other.withArg": "Has {n} things.",
      "other.withTag": "Has <b>bold</b> text.",
    };
    expect(requiredKeysOf(fixture)).toEqual(
      new Set([
        "why.anything",
        "chip.anything",
        "warning.anything",
        "error.anything",
        "policyRules.fieldHelp.anything",
        "policyRules.fieldUnit.anything",
        "policySim.staleReason.anything",
        "services.modal.mapError",
        "lists.plexError",
        "other.withArg",
        "other.withTag",
      ]),
    );
  });

  it("pins the size of the required set", () => {
    const required = requiredKeysOf(EN);
    expect(
      required.size,
      `the required set is now ${required.size} keys, expected ${EXPECTED_REQUIRED_COUNT}.\n` +
        `${HOW_TO_UPDATE}\nThen set EXPECTED_REQUIRED_COUNT to ${required.size}.`,
    ).toBe(EXPECTED_REQUIRED_COUNT);
  });

  it("has exactly one note per required key, for keys that exist, in the required set", () => {
    const required = requiredKeysOf(EN);
    const noteKeys = new Set(Object.keys(NOTES));

    const missing = [...required].filter((k) => !noteKeys.has(k)).sort();
    const notInCatalog = [...noteKeys].filter((k) => !(k in EN)).sort();
    const notRequired = [...noteKeys].filter((k) => k in EN && !required.has(k)).sort();
    const empty = [...noteKeys].filter((k) => NOTES[k]!.trim() === "").sort();

    expect(
      missing,
      `required keys with no translator note:\n${missing.join("\n")}\n\n${HOW_TO_UPDATE}`,
    ).toEqual([]);
    expect(
      notInCatalog,
      `notes for keys that don't exist in locales/en/ui.json (a merge left these behind):\n${notInCatalog.join("\n")}`,
    ).toEqual([]);
    expect(
      notRequired,
      `notes for keys outside the required set -- drop them, or the set changed and they belong:\n${notRequired.join("\n")}\n\n${HOW_TO_UPDATE}`,
    ).toEqual([]);
    expect(empty, `notes that are blank:\n${empty.join("\n")}`).toEqual([]);
  });
});
