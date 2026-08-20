// SPDX-License-Identifier: AGPL-3.0-or-later
// @vitest-environment node
//
// Stage 4's extraction gate (docs/I18N_PLAN.md): once a file is declared extracted, no
// user-visible English literal may sit in it again. Rule 144 is why the unit is the whole
// file and never "the easy strings": a converted file vouches for a consistency a
// half-converted one does not have, so CONVERTED grows only when a surface is done
// completely, and this gate is what holds it done.
//
// What the scan reads is the TypeScript AST, not a regex over the text (rule 147), and it
// covers exactly four populations:
//   1. JSX text with at least one letter.
//   2. String-ish literals under a visible attribute (VISIBLE_ATTRS), whatever the
//      spelling: a bare literal, a template, either branch of a ternary, `||`/`??`
//      fallbacks.
//   3. String-ish literals a JSX child renders as a VALUE: `{"text"}`, the branches of
//      `{cond ? "day" : "days"}`, the operands of `||`/`??`/`+`, the right side of `&&`.
//   4. Every argument of an `announce()` call, since a live region that speaks the wrong
//      language is worse than one that says nothing.
// The first argument of `t()`/`i18next.t()` is a catalog id, not copy, and is skipped
// wherever it appears.
//
// Named limits (rule 118: a check that cannot discriminate must not read as a proof):
// copy that reaches the operator through a plain function call (`fmt("literal")`, a
// string built in a `.ts` module and rendered elsewhere) and copy passed through props
// outside VISIBLE_ATTRS are invisible to this scan. The workflow's verify agent reads
// for those; this gate holds the mechanical majority against regression.

import ts from "typescript";
import { describe, expect, it } from "vitest";

import { sourceText } from "./test/sources";

// Files fully extracted to the catalog, src-relative. Grown by the Stage 4 merge step,
// one surface at a time, never by an extraction agent (the list is shared state).
const CONVERTED = ["components/SafetyBanner.tsx"];

// Attributes whose value the operator sees or hears.
const VISIBLE_ATTRS = new Set([
  "alt",
  "aria-description",
  "aria-label",
  "aria-placeholder",
  "aria-roledescription",
  "aria-valuetext",
  "label",
  "placeholder",
  "title",
]);

const T_CALLEES = new Set(["t", "i18next.t", "i18n.t"]);

const hasLetter = (s: string) => /\p{L}/u.test(s);

// A `.ts` file parsed as TSX misreads generics (`foo<T>(x)`) as JSX and invents phantom
// text nodes: api.ts alone "gained" 187 of them when this gate was first drafted.
const scriptKind = (fileName: string) =>
  fileName.endsWith(".tsx") ? ts.ScriptKind.TSX : ts.ScriptKind.TS;

type Leftover = { line: number; text: string };

export function leftoverCopy(fileName: string, text: string): Leftover[] {
  const sf = ts.createSourceFile(
    fileName,
    text,
    ts.ScriptTarget.Latest,
    true,
    scriptKind(fileName),
  );
  const out: Leftover[] = [];
  const flag = (node: ts.Node, copy: string) => {
    const { line } = sf.getLineAndCharacterOfPosition(node.getStart(sf));
    out.push({ line: line + 1, text: copy.trim() });
  };

  // The static text of a template, spans elided: `Removed ${n} items` -> "Removed  items".
  const templateText = (node: ts.TemplateExpression) =>
    [node.head.text, ...node.templateSpans.map((s) => s.literal.text)].join(" ");

  // Every string-ish literal under `node`, whatever the spelling, minus t() keys.
  const literalsUnder = (root: ts.Node) => {
    const visit = (n: ts.Node): void => {
      if (ts.isCallExpression(n) && T_CALLEES.has(n.expression.getText(sf))) {
        n.arguments.slice(1).forEach(visit);
        return;
      }
      if (ts.isStringLiteral(n) || ts.isNoSubstitutionTemplateLiteral(n)) {
        if (hasLetter(n.text)) flag(n, n.text);
      } else if (ts.isTemplateExpression(n)) {
        if (hasLetter(templateText(n))) flag(n, templateText(n));
        n.templateSpans.forEach((s) => visit(s.expression));
      } else {
        n.forEachChild(visit);
      }
    };
    visit(root);
  };

  // The expressions whose VALUE a `{...}` child renders. A literal anywhere else in the
  // expression (a `===` comparison, a call argument) is data, not copy.
  const valuePositions = (e: ts.Expression): ts.Expression[] => {
    if (ts.isParenthesizedExpression(e)) return valuePositions(e.expression);
    if (ts.isConditionalExpression(e))
      return [...valuePositions(e.whenTrue), ...valuePositions(e.whenFalse)];
    if (ts.isBinaryExpression(e)) {
      const op = e.operatorToken.kind;
      if (
        op === ts.SyntaxKind.QuestionQuestionToken ||
        op === ts.SyntaxKind.BarBarToken ||
        op === ts.SyntaxKind.PlusToken
      )
        return [...valuePositions(e.left), ...valuePositions(e.right)];
      if (op === ts.SyntaxKind.AmpersandAmpersandToken) return valuePositions(e.right);
    }
    return [e];
  };

  const visit = (node: ts.Node): void => {
    if (ts.isJsxText(node)) {
      if (hasLetter(node.text)) flag(node, node.text);
    } else if (ts.isJsxAttribute(node)) {
      if (VISIBLE_ATTRS.has(node.name.getText(sf)) && node.initializer) {
        literalsUnder(node.initializer);
        return;
      }
    } else if (
      ts.isJsxExpression(node) &&
      node.expression &&
      (ts.isJsxElement(node.parent) || ts.isJsxFragment(node.parent))
    ) {
      for (const v of valuePositions(node.expression)) {
        if (ts.isStringLiteral(v) || ts.isNoSubstitutionTemplateLiteral(v)) {
          if (hasLetter(v.text)) flag(v, v.text);
        } else if (ts.isTemplateExpression(v)) {
          if (hasLetter(templateText(v))) flag(v, templateText(v));
        }
      }
    } else if (ts.isCallExpression(node) && node.expression.getText(sf) === "announce") {
      node.arguments.forEach(literalsUnder);
      return;
    }
    node.forEachChild(visit);
  };
  visit(sf);
  return out;
}

describe("the i18n extraction gate", () => {
  it("every converted file is free of user-visible literals", () => {
    const offenders: string[] = [];
    for (const file of CONVERTED) {
      for (const hit of leftoverCopy(file, sourceText(file))) {
        offenders.push(`${file}:${hit.line}: "${hit.text}"`);
      }
    }
    expect(
      offenders,
      `hardcoded operator copy in files CONVERTED declares extracted:\n${offenders.join("\n")}`,
    ).toEqual([]);
  });

  it("every converted file exists and actually uses the catalog", () => {
    // A rename would otherwise drop the file out of the walk while the gate stays green
    // (rule 145), and a file listed here that reads no catalog is a claim with nothing
    // under it.
    expect(new Set(CONVERTED).size).toBe(CONVERTED.length);
    for (const file of CONVERTED) {
      const text = sourceText(file); // throws on a stale path
      expect(
        /react-i18next|from "\.{1,2}\/i18n"/.test(text),
        `${file} is listed as converted but imports neither react-i18next nor the i18n module`,
      ).toBe(true);
    }
  });

  it("catches every spelling the tree writes copy in (rule 147)", () => {
    const flagged = (src: string, name = "probe.tsx") => leftoverCopy(name, src).map((l) => l.text);

    // Accepted spellings: each of the four populations, in the forms components use.
    expect(flagged(`const a = <p>Deletion is on.</p>;`)).toEqual(["Deletion is on."]);
    expect(flagged(`const a = <button aria-label="Close" />;`)).toEqual(["Close"]);
    expect(flagged(`const a = <div title={busy ? "Working" : "Idle"} />;`)).toEqual([
      "Working",
      "Idle",
    ]);
    expect(flagged("const a = <div aria-label={`Remove ${name}`} />;")).toEqual(["Remove"]);
    expect(flagged(`const a = <span>{n === 1 ? "day" : "days"}</span>;`)).toEqual(["day", "days"]);
    expect(flagged(`const a = <span>{label || "None yet"}</span>;`)).toEqual(["None yet"]);
    expect(flagged("announce(`Scan finished`);")).toEqual(["Scan finished"]);
    expect(flagged(`announce("Saved.");`)).toEqual(["Saved."]);
    expect(flagged(`const a = <p>{ok && "All good"}</p>;`)).toEqual(["All good"]);

    // Rejected spellings: catalog reads, ids, decoration, and comparisons.
    expect(flagged(`const a = <span>{t("why.disabled")}</span>;`)).toEqual([]);
    expect(flagged(`const a = <Trans i18nKey="safetyBanner.armed" />;`)).toEqual([]);
    expect(flagged(`const a = <div className="notice notice-error" />;`)).toEqual([]);
    expect(flagged(`const a = <span aria-hidden>{" · "}</span>;`)).toEqual([]);
    expect(flagged(`const a = <span>{kind === "movie" ? one : two}</span>;`)).toEqual([]);
    expect(flagged(`announce(t("queue.saved", { n }));`)).toEqual([]);
    expect(flagged(`const a = <div title={t("a.b") + ":"} />;`)).toEqual([]);

    // A .ts module: generics parse as generics, never as phantom JSX, and announce()
    // copy is still read.
    expect(flagged(`const a = fn<Item>(rows); announce("Saved.");`, "probe.ts")).toEqual([
      "Saved.",
    ]);

    // The named limit, pinned so nobody reads more coverage into the gate than it has:
    // a literal that reaches the operator through a plain call is not seen.
    expect(flagged(`const a = <span>{fmt("literal the gate cannot see")}</span>;`)).toEqual([]);
  });
});
