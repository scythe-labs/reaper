// SPDX-License-Identifier: AGPL-3.0-or-later
// @vitest-environment node
//
// This gate checks `locales/en/ui.notes.json`. A translator working from `ui.json` alone sees
// a dotted key and a message with bare `{params}`. The notes file is where they learn what a
// param holds, whether a string is a whole sentence or a fragment another key nests, and where
// the string shows in the app. This gate does not judge the wording of a note, only that the
// required set of keys has exactly one.
//
// The required set is derived, never pasted by hand: every key under the eight namespaces a
// translator most needs guided (why., chip., warning., error., the two policyRules.field*
// families, policySim.staleReason., services.discord.testResult.), two one-off leaves outside
// them (services.modal.mapError, lists.plexError), and every other catalog message carrying an
// ICU argument or a tag. A plain sentence with nothing to interpolate needs no note. "error."
// joins the whole-namespace group rather than relying on the ICU-argument rule alone, because
// a coded refusal with no param still needs the translator told which screen it fires on.
// "Carries an argument or a tag" is answered by parsing the message with the same ICU library
// the other two catalog gates use (i18n-keys.test.ts, i18n-locales.test.ts), not by a
// brace-matching regex. An escaped literal brace (`'{'text'}'`) parses as plain text, so a
// regex counting braces would over-collect exactly where a hand-rolled matcher usually fails.

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
  // A background job's outcome (jobs.result.*) and a scan's live step
  // (shell.scanBar.step.*) are both typed reasons composed on the browser, the same shape
  // as error. A plain-sentence entry with no param still needs the translator told which
  // row or bar it fires on.
  "jobs.result.",
  "shell.scanBar.step.",
];

/** One-off leaves outside those namespaces that carry a raw server `error`/reason param and
 *  need a note for the same reason (api.ts's own docstrings on each field name this file). */
const REQUIRED_EXACT = new Set(["services.modal.mapError", "lists.plexError"]);

/** Whether a message has an ICU argument (simple, number, date, time, plural, select) or a
 *  tag anywhere in it. Every such element is a top-level element of the parsed AST, since ICU
 *  never buries one inside a literal segment, so no recursion into plural/select branches
 *  is needed to find one. */
export function hasArgOrTag(message: string): boolean {
  if (message === "") return false;
  let ast: MessageFormatElement[];
  try {
    ast = new IntlMessageFormat(message, "en-US").getAst();
  } catch {
    // Unparseable ICU is i18n-keys.test.ts's finding to make ("every catalog message parses
    // as ICU"), not this gate's. Treating it as "nothing to note" here never hides that
    // failure.
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

//: Pinned so a change to the required set shows up as a diff here, not a silent pass. The
//: three assertions below already name every individual key that moved, but a set that
//: shrank by exactly as much as it grew would leave them empty while the population changed
//: underneath everyone.
//:
//: The current count is 1172. lists.ago.days/hours/minutes left the required set along with
//: the hand-rolled `ago()` they fed. `since()` in format.ts now formats every relative time
//: through Intl.RelativeTimeFormat, which needs no catalog string and so no note. When this
//: count next needs to change, update EXPECTED_REQUIRED_COUNT below and describe what moved,
//: the same way.
//:
//: The Reap tab redesign (Phase 2) dropped 18 required keys (the on-page Steps table, the
//: dry-run Report panel, the phrase chip, and the stale-plan/execute-disabled notices it
//: replaced) and added 11 (the head Reap button's count, the summary card's tiles and help
//: sentence, the standalone practice run's result, the history row's freed/removed line, and
//: the confirm sheet's merged title), for a net change of -7.
//:
//: The Reap tab redesign (Phase 3) adds 6: the reaping card's "removed" tile and its "now
//: removing" line, the progress bar's accessible value text, the item-status log's heading
//: and its "kept: {reason}" fragment, and the history footer's "Showing N of M" count.
//:
//: Mission Control (Phase 4) drops 8: the confirm sheet no longer shows progress, a report, or
//: a failure of its own. It closes the moment a reap starts, and the Reap tab and app-wide bar
//: carry the run from there. Gone are the progress tick and item counter, the freed and spared
//: fragments, the result line's reclaimed/spared/unmeasured pieces, and the failure body.
const EXPECTED_REQUIRED_COUNT = 1173;

describe("translator notes (locales/en/ui.notes.json)", () => {
  it("classifies a plain literal as not required, and an ICU or tagged one as required (rule 145)", () => {
    expect(hasArgOrTag("Plain text.")).toBe(false);
    expect(hasArgOrTag("")).toBe(false);
    expect(hasArgOrTag("{n} files")).toBe(true);
    expect(hasArgOrTag("{n, plural, one {# file} other {# files}}")).toBe(true);
    expect(hasArgOrTag("{k, select, a {A} other {B}}")).toBe(true);
    expect(hasArgOrTag("Keep <btn>going</btn>")).toBe(true);
    // An escaped literal brace is text, not an argument. The ICU parser reads the whole
    // thing as one literal segment, which is exactly why this gate parses instead of
    // matching braces.
    expect(hasArgOrTag("a literal '{' brace '}' and nothing else")).toBe(false);
  });

  it("the required-set rule catches a namespaced key and an argued/tagged one alike, and only those (rule 145)", () => {
    // Seven of the ten required namespaces are exercised here. The other three
    // (services.discord.testResult., jobs.result., shell.scanBar.step.) are proven the same
    // way by the full-catalog tests below, since a literal "services.*.*" fixture string here
    // reads as a Python symbol citation to the repo's own hygiene gate
    // (test_a_dotted_symbol_citation_resolves_to_a_real_symbol). The membership check itself
    // does not care which namespace it is given, so this loses no coverage.
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
