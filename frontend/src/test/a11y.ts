// SPDX-License-Identifier: AGPL-3.0-or-later
// Runs axe-core against the tree a test just rendered, and FAILS on what it finds.
//
// Why a runtime audit rather than a lint rule: every accessibility bug this project has
// actually shipped lived in the *computed* tree, not in the JSX. A name assembled from
// props, a role that prunes its own children from the accessibility tree, a control that
// only exists after a query resolves -- a static linter reads the source and sees none of
// them. `eslint-plugin-jsx-a11y` was measured against the real tree and caught none of the
// filed bugs (docs/STATUS.md), because it cannot see through <Switch> to the <input> it
// renders. axe reads the DOM the browser built, so a custom component is not a blind spot.
//
// It fails rather than warns, for the reason the whole of setup.ts fails rather than warns
// (rule 135): vitest drops console output on some Node versions, so a warning is invisible
// exactly where someone could act on it.
import axe from "axe-core";

// jsdom builds a DOM but never lays it out or paints it, so every rule that needs geometry
// or a computed color reports against a fiction. `color-contrast` is the one that matters
// here: it reads pixels, gets none, and would report every control on the page. These are
// covered by looking at the running app, not by this gate -- which is why the list is short
// and each entry says what covers it instead.
const NEEDS_A_REAL_BROWSER = [
  // Reads rendered pixels; jsdom paints nothing. Checked against the running app.
  "color-contrast",
  // Both need layout to know what is on screen and what is behind it.
  "scrollable-region-focusable",
  "target-size",
];

export type A11yOptions = {
  /** Rules to skip for this call, each with the reason it cannot apply. */
  skip?: Record<string, string>;
};

/** Audits `container` and returns axe's violations, worst first. */
export async function findA11yViolations(
  container: HTMLElement = document.body,
  options: A11yOptions = {},
): Promise<axe.Result[]> {
  const skipped = [...NEEDS_A_REAL_BROWSER, ...Object.keys(options.skip ?? {})];
  const results = await axe.run(container, {
    rules: Object.fromEntries(skipped.map((id) => [id, { enabled: false }])),
    // The tags axe itself maps to WCAG 2.1 A and AA, plus its best-practice set. Anything
    // outside them is advisory and would make this gate argue about taste.
    runOnly: { type: "tag", values: ["wcag2a", "wcag2aa", "wcag21a", "wcag21aa", "best-practice"] },
  });
  const order = { critical: 0, serious: 1, moderate: 2, minor: 3 } as const;
  return [...results.violations].sort(
    (a, b) =>
      (order[a.impact as keyof typeof order] ?? 4) - (order[b.impact as keyof typeof order] ?? 4),
  );
}

/** Formats violations the way setup.ts formats its own: what broke, where, and what to do. */
export function describeA11yViolations(violations: axe.Result[]): string {
  return violations
    .map((violation) => {
      const nodes = violation.nodes
        .slice(0, 3)
        .map((node) => `      ${node.html}`)
        .join("\n");
      const more =
        violation.nodes.length > 3 ? `\n      ...and ${violation.nodes.length - 3} more` : "";
      return (
        `  ${violation.impact ?? "unknown"}: ${violation.help} (${violation.id})\n` +
        `${nodes}${more}\n      ${violation.helpUrl}`
      );
    })
    .join("\n\n");
}

/**
 * Fails the test if the rendered tree has any accessibility violation.
 *
 * Pass `skip` only with a reason, so a suppressed rule stays arguable later:
 * `await expectNoA11yViolations(container, { skip: { "aria-required-children": "why" } })`.
 */
export async function expectNoA11yViolations(
  container: HTMLElement = document.body,
  options: A11yOptions = {},
): Promise<void> {
  const violations = await findA11yViolations(container, options);
  if (violations.length === 0) return;
  throw new Error(
    `axe found ${violations.length} accessibility violation` +
      `${violations.length === 1 ? "" : "s"} in the rendered tree:\n\n` +
      `${describeA11yViolations(violations)}\n\n` +
      `Each one is something an operator using a screen reader or the keyboard hits for real. ` +
      `Fix the markup; suppress a rule only with expectNoA11yViolations(container, ` +
      `{ skip: { "<rule-id>": "<why it cannot apply here>" } }).`,
  );
}
