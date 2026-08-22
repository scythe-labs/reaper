// SPDX-License-Identifier: AGPL-3.0-or-later
// @vitest-environment node
//
// No module reads the catalog in its own body. Every string resolves in a function.
//
// A module body runs once, when something first imports it, and whatever `i18next.t()` returns
// there is that string for the life of the page. `main.tsx` serves the operator's language in
// `applyStoredLanguage`, which it awaits before rendering -- but a static `import` runs the
// imported body FIRST, so everything the entry reaches has already been evaluated by the time
// that await is reached. That is how a Spanish browser got a Spanish queue with English tabs
// (#897). The lazily imported half of the tree escaped it by accident of load order, which is
// not a property worth resting on: a new static import moves a module from one half to the
// other with nothing to notice.
//
// So the rule is flat, and every table is a function called per render, the way
// `queueFilters.tsx` has always done it.
//
// Named limits (rule 118). It reads one spelling, `<default import of ./i18n>.t(...)`, at module
// scope. A catalog read reached another way -- a destructured `t`, a helper in another module
// called from a module-scope initializer -- is invisible to it; a sweep for the second shape
// found none. The fixtures below are the accepted and rejected forms, run against the detector
// itself (rule 147), and the population it walked is pinned for rule 145's reason.

import { readFileSync } from "node:fs";
import ts from "typescript";
import { describe, expect, it } from "vitest";

import { shippedSource, srcRelative } from "./test/sources";

//: Every `.ts`/`.tsx` the SPA ships, which is the population the ban below scans. Pinned
//: because the ban's expected result is EMPTY, so a walk that stopped reading the tree agrees
//: with a clean one exactly (rule 147). Bump it when you add or delete a module.
const EXPECTED_SHIPPED_MODULES = 127;

const parse = (fileName: string, text: string) =>
  ts.createSourceFile(
    fileName,
    text,
    ts.ScriptTarget.Latest,
    true,
    fileName.endsWith(".tsx") ? ts.ScriptKind.TSX : ts.ScriptKind.TS,
  );

/** The 1-based lines where `text` reads the catalog in its own body, outside every function.
 *  A function-like node and a class both defer what is inside them to a later call, so the walk
 *  stops at each rather than descending: an arrow inside an object literal
 *  (`signalRamp.ts`'s `say: (n) => t(...)`) is a late read sitting in an early table, and only
 *  the table is the problem. */
export function moduleScopeCatalogReads(fileName: string, text: string): number[] {
  const sf = parse(fileName, text);

  // The local name of `./i18n`'s default export, which is the only `t` this gate claims to read.
  let i18n: string | undefined;
  for (const statement of sf.statements) {
    if (
      ts.isImportDeclaration(statement) &&
      ts.isStringLiteral(statement.moduleSpecifier) &&
      /(^|\/)i18n$/.test(statement.moduleSpecifier.text) &&
      statement.importClause?.name
    ) {
      i18n = statement.importClause.name.text;
    }
  }
  if (!i18n) return [];

  const lines: number[] = [];
  const visit = (node: ts.Node): void => {
    if (ts.isFunctionLike(node) || ts.isClassLike(node)) return;
    if (
      ts.isCallExpression(node) &&
      ts.isPropertyAccessExpression(node.expression) &&
      ts.isIdentifier(node.expression.expression) &&
      node.expression.expression.text === i18n &&
      node.expression.name.text === "t"
    ) {
      lines.push(sf.getLineAndCharacterOfPosition(node.getStart(sf)).line + 1);
    }
    node.forEachChild(visit);
  };
  sf.forEachChild(visit);
  return lines;
}

describe("the catalog is never read in a module body", () => {
  it("walks every module the SPA ships", () => {
    expect(
      shippedSource().length,
      "the shipped-source walk found a different number of modules. If you added or deleted " +
        "one, bump the number. If you did not, the walk lost part of the tree, and an empty " +
        "offender list is green on a tree it never read.",
    ).toBe(EXPECTED_SHIPPED_MODULES);
  });

  it("finds no module-scope catalog read anywhere in the tree", () => {
    const offenders: string[] = [];
    for (const file of shippedSource()) {
      for (const line of moduleScopeCatalogReads(file, readFileSync(file, "utf8"))) {
        offenders.push(`${srcRelative(file)}:${line}`);
      }
    }
    expect(
      offenders,
      "each of these resolves once, when the module is first imported, and then keeps that " +
        "string whatever language the operator is served (#897). Move it into a function " +
        `called per render, the way queueFilters.tsx does:\n${offenders.join("\n")}`,
    ).toEqual([]);
  });

  it("reads every spelling of a module-scope catalog read, and no deferred one", () => {
    const head = 'import i18next from "./i18n";\n';
    const found = (body: string) => moduleScopeCatalogReads("fixture.tsx", head + body).length;

    // Early: evaluated when the module body runs.
    expect(found('const a = i18next.t("k");')).toBe(1);
    expect(found('const a = { label: i18next.t("k") };')).toBe(1);
    expect(found('export const a: X[] = [{ label: i18next.t("k") }];')).toBe(1);
    expect(found('const a = on ? i18next.t("x") : i18next.t("y");')).toBe(2);
    expect(found('const a = i18next.t("x") ?? i18next.t("y");')).toBe(2);

    // Late: nothing here runs until something calls it.
    expect(found('const a = () => i18next.t("k");')).toBe(0);
    expect(found('function a() { return i18next.t("k"); }')).toBe(0);
    expect(found('const a = { say: (n: number) => i18next.t("k", { n }) };')).toBe(0);
    expect(found('class A { m() { return i18next.t("k"); } }')).toBe(0);

    // Not the catalog: a `t` off anything but this file's `./i18n` default import.
    expect(found('const a = other.t("k");')).toBe(0);
    expect(moduleScopeCatalogReads("fixture.tsx", 'const a = i18next.t("k");')).toEqual([]);
  });
});
