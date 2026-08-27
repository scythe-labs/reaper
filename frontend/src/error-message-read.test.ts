// SPDX-License-Identifier: AGPL-3.0-or-later
// @vitest-environment node
//
// This gate is modeled on i18n-extraction.test.ts's leftover-copy scan: every render of a
// caught error goes through errors.ts's `describeError`, which composes a coded `ApiError`
// under the catalog's `error.*` namespace and falls back to the English `message` only when
// there is no code at all. Reading `.message` straight off an error bypasses that composition
// silently, in every locale but the one the message happens to be written in. This is the same
// failure i18n-extraction.test.ts already guards for a hardcoded string literal, one hop
// further downstream.
//
// There is no type information available to a syntax-only scan, so "error-shaped" is a naming
// convention rather than a type check. A matcher like this is bounded by the population it can
// parse, so it must be proven against every spelling the tree actually writes this in, not just
// the ones a first draft happens to catch. This gate covers a bare identifier named `err`,
// `error`, or `e`, or one ending in "Error" (`keyError`, `invalidError`, `simError`,
// `webUrlError`, ...), and a property access whose own name is `error` (`mutation.error`,
// `save.error`, `check.error`, `testSaved.error`, ...), react-query's own field name for a
// failed query or mutation.

import ts from "typescript";
import { describe, expect, it } from "vitest";
import { shippedSource, sourceText, srcRelative } from "./test/sources";

const ERROR_LEAF = /^(err|error|e)$/;
const ERROR_SUFFIX = /Error$/;

function looksLikeError(expr: ts.Expression): boolean {
  if (ts.isIdentifier(expr)) return ERROR_LEAF.test(expr.text) || ERROR_SUFFIX.test(expr.text);
  if (ts.isPropertyAccessExpression(expr)) return expr.name.text === "error";
  return false;
}

/** Every line of `text` reading `.message` off something shaped like an error. */
export function errorMessageReadLines(fileName: string, text: string): number[] {
  const scriptKind = fileName.endsWith(".tsx") ? ts.ScriptKind.TSX : ts.ScriptKind.TS;
  const sf = ts.createSourceFile(fileName, text, ts.ScriptTarget.Latest, true, scriptKind);
  const lines: number[] = [];
  const visit = (node: ts.Node): void => {
    if (
      ts.isPropertyAccessExpression(node) &&
      node.name.text === "message" &&
      looksLikeError(node.expression)
    ) {
      const { line } = sf.getLineAndCharacterOfPosition(node.getStart(sf));
      lines.push(line + 1);
    }
    node.forEachChild(visit);
  };
  visit(sf);
  return lines;
}

// The one file allowed to read `.message` directly: `describeError`'s own body reads it as
// the fallback it provides to every other caller, `err.message` when there is no code at all.
// `api.ts`'s `ApiError` constructor and `parseFailure` build a `message` field rather than
// reading one off an error. Neither matches the naming convention above (their local is
// `failure`, `body`, never `err`/`error`/`e`/`*Error`), so today's tree needs no entry for it.
// If a future refactor there starts reading `something.message` off a genuine error, this gate
// is meant to catch that too.
const ALLOWLIST = new Set(["errors.ts"]);

describe("no component reads a caught error's .message directly (rule 147)", () => {
  it("every shipped file composes a caught error through describeError instead", () => {
    const offenders: string[] = [];
    for (const path of shippedSource()) {
      const rel = srcRelative(path);
      if (ALLOWLIST.has(rel)) continue;
      for (const line of errorMessageReadLines(rel, sourceText(rel))) {
        offenders.push(`${rel}:${line}`);
      }
    }
    expect(
      offenders,
      `.message read directly off an error-shaped value instead of describeError(err):\n${offenders.join("\n")}`,
    ).toEqual([]);
  });

  it("catches every spelling the tree wrote this in before the sweep (rule 147)", () => {
    const flagged = (src: string) => errorMessageReadLines("probe.tsx", src);

    // The bare identifiers the sweep found.
    expect(flagged(`const a = err.message;`)).toEqual([1]);
    expect(flagged(`const a = error.message;`)).toEqual([1]);
    expect(flagged(`onError: (e: Error) => setError(e.message),`)).toEqual([1]);
    expect(flagged(`const a = keyError.message;`)).toEqual([1]);
    expect(flagged(`const a = invalidError.message;`)).toEqual([1]);

    // The react-query shape: `<query-or-mutation>.error.message`, optional-chained or not.
    expect(flagged(`const a = save.error.message;`)).toEqual([1]);
    expect(flagged(`const a = mutation.error?.message;`)).toEqual([1]);
    expect(flagged(`const a = query.error.message;`)).toEqual([1]);

    // Rejected spellings: the fix itself, and unrelated `.message` reads on something that
    // is not error-shaped at all.
    expect(flagged(`const a = describeError(err);`)).toEqual([]);
    expect(flagged(`const a = response.message;`)).toEqual([]);
    expect(flagged(`const a = data.message;`)).toEqual([]);
    expect(flagged(`const a = failure.message;`)).toEqual([]);

    // Two on one line, both caught.
    expect(flagged(`const a = err.message + error.message;`)).toEqual([1, 1]);
  });
});
