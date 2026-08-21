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
// covers exactly three populations, each in VALUE position only -- the expression itself,
// the branches of a ternary, the operands of `||`/`??`/`+`, the right side of `&&`:
//   1. JSX text with at least one letter.
//   2. String-ish literals whose value a visible attribute (VISIBLE_ATTRS) renders. JSX
//      passed through such an attribute as a component prop is scanned as JSX, not as a
//      string, so a className inside a `title={<span .../>}` prop is not copy.
//   3. String-ish literals whose value an `announce()` call speaks, since a live region
//      that speaks the wrong language is worse than one that says nothing.
// Everything outside value position is data, not copy: `===` comparisons, call arguments
// (a t() key, an ICU discriminant param like `is4k: on ? "yes" : "no"`).
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
const CONVERTED = [
  "App.tsx",
  "components/AboutPanel.tsx",
  "components/BackupPanel.tsx",
  "components/BalanceBar.tsx",
  "components/CollectionChip.tsx",
  "components/DeletionToggle.tsx",
  "components/DiscordModal.tsx",
  "components/Fairness.tsx",
  "components/GeneralPanel.tsx",
  "components/JobStatus.tsx",
  "components/JobsPanel.tsx",
  "components/ListModal.tsx",
  "components/ListsPanel.tsx",
  "components/Login.tsx",
  "components/LogsPanel.tsx",
  "components/ModalShell.tsx",
  "components/NotInScanPanel.tsx",
  "components/Notice.tsx",
  "components/NotificationsPanel.tsx",
  "components/OverrideControls.tsx",
  "components/PlexPanel.tsx",
  "components/PlexPin.tsx",
  "components/PlexTrashNotice.tsx",
  "components/PolicyEditor.tsx",
  "components/PolicyRuleEditors.tsx",
  "components/PolicySimulator.tsx",
  "components/QuantityInput.tsx",
  "components/ReapBar.tsx",
  "components/ReapBreakdown.tsx",
  "components/ReapConfirm.tsx",
  "components/ReapPlan.tsx",
  "components/RestoreCard.tsx",
  "components/ReviewQueue.tsx",
  "components/SafetyBanner.tsx",
  "components/ScalesPanel.tsx",
  "components/ScanBar.tsx",
  "components/ScanLine.tsx",
  "components/ScanFreshness.tsx",
  "components/SectionNav.tsx",
  "components/SecurityPanel.tsx",
  "components/ServiceModal.tsx",
  "components/ServicesPanel.tsx",
  "components/Settings.tsx",
  "components/SetupConnectStep.tsx",
  "components/SetupPasswordStep.tsx",
  "components/SetupPlexStep.tsx",
  "components/SetupRestoreModal.tsx",
  "components/SetupScanStep.tsx",
  "components/SetupStepper.tsx",
  "components/SetupWizard.tsx",
  "components/ShowPanel.tsx",
  "components/StaleReadNotice.tsx",
  "components/StatusChip.tsx",
  "components/SwitchConfirm.tsx",
  "components/TagsEditor.tsx",
  "components/UnmatchedList.tsx",
  "components/UserMenu.tsx",
  "components/WhyPanel.tsx",
  "components/WhyPanelFallback.tsx",
  "components/WhyShell.tsx",
  "components/policyMeta.ts",
  "components/policyPresets.ts",
  "docs/DocBody.tsx",
  "docs/DocsModal.tsx",
  "useScanSettled.ts",
];

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

const hasLetter = (s: string) => /\p{L}/u.test(s);

// The product's name is a name, not operator copy: it stays a source literal so the
// README-banner generator and the social card can parse it out of the masthead, and no
// translator is ever handed it.
const NAMES = new Set(["Reaper"]);
const isCopy = (s: string) => hasLetter(s) && !NAMES.has(s.trim());

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

  // The expressions whose VALUE lands in front of the operator. A literal anywhere else
  // (a `===` comparison, a call argument) is data, not copy.
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

  const flagValues = (e: ts.Expression) => {
    for (const v of valuePositions(e)) {
      if (ts.isStringLiteral(v) || ts.isNoSubstitutionTemplateLiteral(v)) {
        if (isCopy(v.text)) flag(v, v.text);
      } else if (ts.isTemplateExpression(v)) {
        if (isCopy(templateText(v))) flag(v, templateText(v));
      }
    }
  };

  const visit = (node: ts.Node): void => {
    if (ts.isJsxText(node)) {
      if (isCopy(node.text)) flag(node, node.text);
    } else if (ts.isJsxAttribute(node)) {
      if (VISIBLE_ATTRS.has(node.name.getText(sf)) && node.initializer) {
        const init = node.initializer;
        if (ts.isStringLiteral(init)) {
          if (isCopy(init.text)) flag(init, init.text);
        } else if (ts.isJsxExpression(init) && init.expression) {
          flagValues(init.expression);
        }
        // No early return: JSX handed through the attribute as a prop still gets the
        // JSX walk below, so a literal text node inside it is caught as JSX text.
      }
    } else if (
      ts.isJsxExpression(node) &&
      node.expression &&
      (ts.isJsxElement(node.parent) || ts.isJsxFragment(node.parent))
    ) {
      flagValues(node.expression);
    } else if (ts.isCallExpression(node) && node.expression.getText(sf) === "announce") {
      node.arguments.forEach(flagValues);
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

    // Accepted spellings: each of the three populations, in the forms components use.
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
    expect(
      flagged(`const a = <input placeholder={k === "tautulli" ? t("a.b") : t("c.d")} />;`),
    ).toEqual([]);
    expect(
      flagged(`const a = <select aria-label={t("a.b", { is4k: on ? "yes" : "no" })} />;`),
    ).toEqual([]);
    expect(
      flagged(`const a = <Shell title={<span className="kind-badge">{t("a.b")}</span>} />;`),
    ).toEqual([]);

    // JSX handed through a prop is still JSX: a literal text node inside it is caught.
    expect(flagged(`const a = <Shell title={<span>Legend</span>} />;`)).toEqual(["Legend"]);

    // A .ts module: generics parse as generics, never as phantom JSX, and announce()
    // copy is still read.
    expect(flagged(`const a = fn<Item>(rows); announce("Saved.");`, "probe.ts")).toEqual([
      "Saved.",
    ]);

    // The product's name is a name, not copy, and stays a parseable source literal.
    expect(flagged(`const a = <h1 className="brand-word">Reaper</h1>;`)).toEqual([]);

    // The named limit, pinned so nobody reads more coverage into the gate than it has:
    // a literal that reaches the operator through a plain call is not seen.
    expect(flagged(`const a = <span>{fmt("literal the gate cannot see")}</span>;`)).toEqual([]);
  });
});
