// SPDX-License-Identifier: AGPL-3.0-or-later
// @vitest-environment node
//
// Stage 4's extraction gate (docs/history/I18N_PLAN.md): once a file is declared extracted, no
// user-visible English literal may sit in it again. The unit is the whole file, never just the
// easy strings, because a converted file vouches for a consistency a half-converted one does
// not have. CONVERTED grows only when a surface is done completely, and this gate is what
// keeps it done.
//
// The scan reads the TypeScript AST rather than matching text with a regex, and it covers
// exactly three populations, each in value position only: the expression itself, the branches
// of a ternary, the operands of `||`/`??`/`+`, and the right side of `&&`.
//   1. JSX text with at least one letter.
//   2. String-ish literals whose value a visible attribute (VISIBLE_ATTRS) renders. JSX
//      passed through such an attribute as a component prop is scanned as JSX, not as a
//      string, so a className inside a `title={<span .../>}` prop is not copy.
//   3. String-ish literals whose value an `announce()` call speaks, since a live region
//      that speaks the wrong language is worse than one that says nothing.
// Everything outside value position is data, not copy: `===` comparisons, call arguments
// (a t() key, an ICU discriminant param like `is4k: on ? "yes" : "no"`).
//
// This scan cannot see everything: copy that reaches the operator through a plain function
// call (`fmt("literal")`, a string built in a `.ts` module and rendered elsewhere), and copy
// passed through props outside VISIBLE_ATTRS, are invisible to it. The workflow's verify agent
// checks for those by hand; this gate catches the mechanical majority and holds it against
// regression.

import ts from "typescript";
import { describe, expect, it } from "vitest";

import { shippedSource, sourceText, srcRelative } from "./test/sources";

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
  // Phase 9 (docs/history/I18N_PLAN.md): the rest of the tree outside tests, so the list is
  // the tree and a new file fails this gate until it joins CONVERTED.
  "accent.ts",
  "announce.tsx",
  "api.ts",
  "backnav.tsx",
  "errors.ts",
  "focus.ts",
  // Most of the file is localized through Intl (unit-style numbers, RelativeTimeFormat); see
  // format.ts's own header. humanDays/humanWindow route their unit words through the catalog
  // too (format.span.*), since Intl has no built-in "N years, M months" form. This is proven by
  // humanDays.test.ts and why.test.ts against the real catalog, not by this scan, since a
  // literal built from a plain function's return value is invisible to it.
  "format.ts",
  "i18n.ts",
  "main.tsx",
  "navIntent.ts",
  "navUrl.ts",
  "pageScrollLock.ts",
  "plexServerQueries.ts",
  "reapReadiness.ts",
  "shelfStatus.ts",
  "updateStatus.ts",
  "useGeneralSettings.ts",
  "useMediaQuery.ts",
  "useOverrideMutations.ts",
  "usePlexLibraries.ts",
  "usePlexTrash.ts",
  "usePolicyProbe.ts",
  "useReviewFreshness.ts",
  "useSafety.ts",
  "useScanStatus.ts",
  "why.ts",
  "brand/BrandBadge.tsx",
  "brand/BrandMark.tsx",
  "brand/appIcon.ts",
  "brand/dissolve.generated.ts",
  "brand/dissolve.ts",
  "brand/scythe.ts",
  // The GitHub social-preview card's source: rasterized by `npm run social-card`, never
  // imported at runtime and never served to an operator (its own header says so). Not a UI
  // surface the catalog reaches.
  "brand/socialCard.ts",
  "components/CardOpen.tsx",
  "components/FilterMenu.tsx",
  "components/PosterFallback.tsx",
  "components/ProgressBar.tsx",
  "components/ScytheGlyph.tsx",
  "components/Segmented.tsx",
  "components/SetRow.tsx",
  "components/Switch.tsx",
  "components/artFallback.ts",
  "components/navIcons.tsx",
  "components/popoverFit.ts",
  "components/queueFilters.tsx",
  "components/queueIcons.tsx",
  "components/queueSettings.tsx",
  "components/reviewFate.ts",
  "components/signalRamp.ts",
  "components/watchReach.ts",
  "docs/DocLink.tsx",
  "docs/DocsContext.tsx",
  "docs/blocks.ts",
  // The in-app manual's English source. Each has an established translation route:
  // `docs/content/<tag>/index.ts`, proven by `manual.locales.test.ts` and loaded by
  // `docs/localized.ts`, the same route CONTRIBUTING.md documents for the manual. There are no
  // catalog reads to check for here; the English is what a translated directory replaces.
  "docs/content/arming.ts",
  "docs/content/cheatSheet.ts",
  "docs/content/deletionSafety.ts",
  "docs/content/overview.ts",
  "docs/content/plexRebuild.ts",
  "docs/content/understandingPolicy.ts",
  "docs/localized.ts",
  "docs/registry.ts",
  "docs/toMdx.ts",
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

// A `.ts` file parsed as TSX misreads generics (`foo<T>(x)`) as JSX and invents phantom text
// nodes, which is why picking the correct script kind matters here.
const scriptKind = (fileName: string) =>
  fileName.endsWith(".tsx") ? ts.ScriptKind.TSX : ts.ScriptKind.TS;

type Leftover = { line: number; text: string };

// The expressions whose VALUE lands in front of the operator. A literal anywhere else
// (a `===` comparison, a call argument) is data, not copy. Module-level (not just
// `leftoverCopy`'s helper) so `hasVisibleSurface` below can walk the same positions.
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

/** Whether an expression's value position is copy this FILE composed: a literal with a
 *  letter (still-unconverted copy) or a call shaped like a translation lookup
 *  (`t(...)`, `i18next.t(...)`, `someAlias.t(...)`). A bare identifier or member access
 *  (`{title}`, `{units.nearLabel}`) is neither, since the file is relaying a value that was
 *  translated somewhere else (a caller's prop, another module's function). Proving that
 *  translation is that other site's job, not this file's. */
const isOwnTranslatedSurface = (e: ts.Expression, sf: ts.SourceFile): boolean =>
  valuePositions(e).some((v) => {
    if (ts.isStringLiteral(v) || ts.isNoSubstitutionTemplateLiteral(v)) return hasLetter(v.text);
    if (ts.isTemplateExpression(v)) return true;
    if (ts.isCallExpression(v)) {
      const callee = v.expression.getText(sf);
      return (
        callee === "t" || callee === "i18next.t" || callee === "i18n.t" || callee.endsWith(".t")
      );
    }
    return false;
  });

/** Whether `text` renders JSX text with a letter, or a VISIBLE_ATTRS attribute this file
 *  itself translates (`isOwnTranslatedSurface`), never a passed-through prop, since that is
 *  someone else's literal or someone else's call and proves nothing about THIS file. Used
 *  only to decide which converted files owe the catalog-import check below. It is not a copy
 *  scan: a converted file's copy is gone by definition, which is what the call-shape half of
 *  this check is actually for. */
export function hasVisibleSurface(fileName: string, text: string): boolean {
  const sf = ts.createSourceFile(
    fileName,
    text,
    ts.ScriptTarget.Latest,
    true,
    scriptKind(fileName),
  );
  let found = false;
  const visit = (node: ts.Node): void => {
    if (found) return;
    if (ts.isJsxText(node) && hasLetter(node.text)) {
      found = true;
      return;
    }
    if (ts.isJsxAttribute(node) && VISIBLE_ATTRS.has(node.name.getText(sf)) && node.initializer) {
      const init = node.initializer;
      if (ts.isStringLiteral(init) && hasLetter(init.text)) found = true;
      else if (
        ts.isJsxExpression(init) &&
        init.expression &&
        isOwnTranslatedSurface(init.expression, sf)
      )
        found = true;
      if (found) return;
    } else if (
      ts.isJsxExpression(node) &&
      node.expression &&
      (ts.isJsxElement(node.parent) || ts.isJsxFragment(node.parent)) &&
      isOwnTranslatedSurface(node.expression, sf)
    ) {
      found = true;
      return;
    }
    node.forEachChild(visit);
  };
  visit(sf);
  return found;
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
    // A rename would otherwise drop the file out of the walk while the gate stays green:
    // every listed file must exist, whatever else is true of it.
    //
    // Whether it must also import the catalog is narrower (phase 9): a file with nothing the
    // operator can see, a pure `.ts` utility, or an icon component with no VISIBLE_ATTRS
    // attribute, has no copy to have translated, and demanding an import from it would be a
    // claim with nothing under it in the other direction (`focus.ts`, `brand/BrandMark.tsx`).
    // So the import is required only of a file that renders JSX text or a VISIBLE_ATTRS
    // attribute, the same two populations `leftoverCopy` scans for copy, checked here for
    // presence rather than for English, since after conversion the attribute's value is an
    // expression (`{t(...)}`) and no longer a literal `leftoverCopy` itself would see.
    expect(new Set(CONVERTED).size).toBe(CONVERTED.length);
    for (const file of CONVERTED) {
      const text = sourceText(file); // throws on a stale path
      if (!hasVisibleSurface(file, text)) continue;
      expect(
        /react-i18next|from "\.{1,2}\/i18n"/.test(text),
        `${file} renders JSX text or a visible attribute but imports neither react-i18next nor the i18n module`,
      ).toBe(true);
    }
  });

  it("CONVERTED is the tree, in both directions (phase 9, rule 145)", () => {
    // Reuses the one shared walk (`shippedSource`) rather than a second one of its own. A
    // second, slightly different walk could quietly exclude a directory this one does not, or
    // the reverse, so the two counts would agree with each other while disagreeing with the
    // tree.
    const tree = new Set(shippedSource().map((p) => srcRelative(p)));
    const converted = new Set(CONVERTED);
    const missing = [...tree].filter((f) => !converted.has(f)).sort();
    const stale = [...converted].filter((f) => !tree.has(f)).sort();
    expect(
      missing,
      `shipped files outside CONVERTED (the list must be the tree):\n${missing.join("\n")}`,
    ).toEqual([]);
    expect(
      stale,
      `CONVERTED entries no longer in the tree (a rename or a delete left these behind):\n${stale.join("\n")}`,
    ).toEqual([]);
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
