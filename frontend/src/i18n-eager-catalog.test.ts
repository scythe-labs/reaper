// SPDX-License-Identifier: AGPL-3.0-or-later
// @vitest-environment node
//
// A module in the EAGER bundle may not read the catalog in its own body.
//
// `main.tsx` awaits `applyStoredLanguage()` before it renders, so by first paint i18next serves
// the operator's language. It does not await it before the app's modules are EVALUATED: a static
// `import` runs the imported body first, so everything main.tsx reaches without a dynamic
// `import()` has already run by the time that await is even reached. A `const` built from
// `i18next.t()` up there resolves against the init's `en-US` and stays that string for the life
// of the page, which is how a Spanish browser got a Spanish queue with English tabs (#897).
//
// So the eager half of the tree resolves late -- a function called per render, the way
// `queueFilters.tsx` does it. The lazily imported half may keep its module-scope tables:
// its chunks load when the operator opens the screen, which is after the language is served.
// This gate is what holds that split, and it fails the moment a lazy module is pulled into the
// eager graph by a new static import.
//
// Named limits (rule 118). It reads one spelling, `<default import of ./i18n>.t(...)`, at module
// scope. A catalog read reached some other way -- a destructured `t`, a helper in another module
// called from a module-scope initializer -- is invisible to it. The fixtures below are the
// accepted and rejected forms, run against the detector itself (rule 147).

import { existsSync, readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import ts from "typescript";
import { describe, expect, it } from "vitest";

import { SRC, srcRelative } from "./test/sources";

const scriptKind = (fileName: string) =>
  fileName.endsWith(".tsx") ? ts.ScriptKind.TSX : ts.ScriptKind.TS;

const parse = (fileName: string, text: string) =>
  ts.createSourceFile(fileName, text, ts.ScriptTarget.Latest, true, scriptKind(fileName));

/** The file a relative specifier names, or `null` for one this walk does not follow: a bare
 *  package, a stylesheet, a JSON catalog. TypeScript's own resolver wants a whole program and a
 *  tsconfig; the SPA writes extensionless relative specifiers and nothing else, so the four
 *  candidates below are the whole vocabulary. */
function resolve(fromFile: string, specifier: string): string | null {
  if (!specifier.startsWith(".")) return null;
  const base = join(dirname(fromFile), specifier);
  for (const candidate of [
    `${base}.ts`,
    `${base}.tsx`,
    join(base, "index.ts"),
    join(base, "index.tsx"),
  ]) {
    if (existsSync(candidate)) return candidate;
  }
  return null;
}

/** Every specifier `text` pulls in EAGERLY: a static `import`/`export ... from` that carries at
 *  least one value binding. Type-only forms are erased by the compiler and a dynamic `import()`
 *  is a separate chunk, so neither runs the imported body when this one is evaluated. */
function staticSpecifiers(fileName: string, text: string): string[] {
  const sf = parse(fileName, text);
  const out: string[] = [];
  for (const statement of sf.statements) {
    if (ts.isImportDeclaration(statement)) {
      const clause = statement.importClause;
      // No clause at all is a side-effect import (`import "./x"`), which runs the body.
      if (clause) {
        if (clause.isTypeOnly) continue;
        const bindings = clause.namedBindings;
        if (
          !clause.name &&
          bindings &&
          ts.isNamedImports(bindings) &&
          bindings.elements.every((e) => e.isTypeOnly)
        ) {
          continue;
        }
      }
      if (ts.isStringLiteral(statement.moduleSpecifier)) out.push(statement.moduleSpecifier.text);
    } else if (
      ts.isExportDeclaration(statement) &&
      statement.moduleSpecifier &&
      ts.isStringLiteral(statement.moduleSpecifier)
    ) {
      if (statement.isTypeOnly) continue;
      const clause = statement.exportClause;
      if (clause && ts.isNamedExports(clause) && clause.elements.every((e) => e.isTypeOnly))
        continue;
      out.push(statement.moduleSpecifier.text);
    }
  }
  return out;
}

/** Every module evaluated before `main.tsx`'s own body runs, absolute paths. */
function eagerModules(): string[] {
  const seen = new Set<string>();
  const stack = [join(SRC, "main.tsx")];
  while (stack.length > 0) {
    const file = stack.pop()!;
    if (seen.has(file)) continue;
    seen.add(file);
    for (const specifier of staticSpecifiers(file, readFileSync(file, "utf8"))) {
      const target = resolve(file, specifier);
      if (target) stack.push(target);
    }
  }
  return [...seen].sort();
}

/** The 1-based lines where `text` reads the catalog in its own body, outside every function.
 *  A function-like node and a class both defer what is inside them to a later call, so the walk
 *  stops at each rather than descending: an arrow in an object literal (`say: (n) => t(...)`) is
 *  a late read sitting in an early table, and only the table is the problem. */
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

describe("the eager bundle's catalog reads", () => {
  it("no module evaluated before the language is served reads the catalog in its body", () => {
    const offenders: string[] = [];
    for (const file of eagerModules()) {
      for (const line of moduleScopeCatalogReads(file, readFileSync(file, "utf8"))) {
        offenders.push(`${srcRelative(file)}:${line}`);
      }
    }
    expect(
      offenders,
      "these run before `applyStoredLanguage` serves the operator's catalog, so each freezes " +
        "in English for the life of the page (#897). Resolve them in a function called per " +
        `render, the way queueFilters.tsx does:\n${offenders.join("\n")}`,
    ).toEqual([]);
  });

  it("the walk follows static imports and stops at dynamic ones", () => {
    const eager = new Set(eagerModules().map(srcRelative));
    // Reached statically from main.tsx -> App.tsx, so their bodies run before the await.
    for (const file of [
      "main.tsx",
      "App.tsx",
      "components/ReviewQueue.tsx",
      "components/QuantityInput.tsx",
      "components/ScanLine.tsx",
      "components/PlexPin.tsx",
    ]) {
      expect(eager, `${file} is in the eager bundle`).toContain(file);
    }
    // Behind `lazy(() => import(...))`, so their tables may stay constants. A static import
    // added to any of these puts it in the set above and the first test starts failing.
    for (const file of ["components/Settings.tsx", "components/policyMeta.ts"]) {
      expect(eager, `${file} is behind a dynamic import`).not.toContain(file);
    }
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
