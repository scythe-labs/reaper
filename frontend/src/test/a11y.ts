// SPDX-License-Identifier: AGPL-3.0-or-later
// Runs axe-core against the tree a test just rendered, and FAILS on what it finds.
//
// This uses a runtime audit instead of a lint rule because every accessibility bug this
// project has actually shipped lived in the computed tree, not in the JSX. A name assembled
// from props, a role that prunes its own children from the accessibility tree, a control that
// only exists after a query resolves. A static linter reads the source and sees none of them.
// `eslint-plugin-jsx-a11y` was measured against the real tree and caught none of the filed bugs
// (docs/LEARNINGS.md), because it cannot see through <Switch> to the <input> it renders. axe
// reads the DOM the browser built, so a custom component is not a blind spot.
//
// This fails rather than warns for the same reason setup.ts does: vitest drops console output
// on some Node versions, so a warning is invisible exactly where someone could act on it.
import { act } from "@testing-library/react";
import axe from "axe-core";

// jsdom builds a DOM but never lays it out or paints it, so every rule that needs geometry or a
// computed color reports against a fiction. None of these three rules can fail here.
// `color-contrast` reads pixels, gets none, and defers. `scrollable-region-focusable` does not
// apply with no scroll box. `target-size` is off by default in axe-core and outside the tags
// below as well. They are listed here anyway, because a rule this gate cannot answer should say
// so by name rather than be silently absent, and because turning any of them on for a real
// browser is then one line. What actually covers them is looking at the running app.
//
// This list is short, and it is not the ledger of every rule jsdom cannot decide. That larger
// ledger is `incomplete`, read below. Treating this short list as the whole ledger would let a
// serious rule pass unnoticed.
const NEEDS_A_REAL_BROWSER = [
  // Reads rendered pixels. jsdom paints nothing. Checked against the running app.
  "color-contrast",
  // Both need layout to know what is on screen and what is behind it.
  "scrollable-region-focusable",
  "target-size",
];

// Rules a panel cannot satisfy on its own, because what satisfies them is the shell above it.
// `region` wants every node inside a landmark, and the landmark is `App`'s `<main>`/`<nav>`/
// `<header>`, so mounting `SecurityPanel` alone reports a defect the running app does not have.
// Suppressing it everywhere would then hide it in the one tree where it is answerable, so it is
// off by default and back on for `pageLevel` callers. Two tests pass `pageLevel`.
// `AppStaleRead.test.tsx` mounts the whole authenticated shell, and `Login.test.tsx`'s sign-in
// card is the whole page. Removing the flag from either retires the only guard this suite ships
// on a landmark, so both are named here on purpose.
const ANSWERED_BY_THE_APP_SHELL = ["region"];

export type A11yOptions = {
  /** Rules to skip for this call, each with the reason it cannot apply. */
  skip?: Record<string, string>;
  /** True when the tree under test is the whole app shell, not a panel inside it. */
  pageLevel?: boolean;
};

/** One thing the gate reports, either a rule axe failed or one it could not rule out. */
export type A11yFinding = axe.Result & {
  /** True when axe filed this under `incomplete`. It could not decide, so this function can't
   *  either. */
  deferred: boolean;
};

/**
 * Audits `container` and returns what axe found, worst first.
 *
 * "Found" covers both buckets axe reports. axe files a rule it cannot settle under
 * `incomplete` rather than `violations`, and under jsdom that is where a whole class of real
 * failures lands. `aria-hidden-focus` catches a focusable element inside `aria-hidden`, an
 * invariant the app maintains by hand at every `aria-hidden` site. That check reports
 * `incomplete` here and `violations` in a real browser. A gate that reads only `violations`
 * would pass `<div aria-hidden="true"><button>Reap</button></div>`, a serious WCAG 4.1.2
 * failure, without a word. So the deferred set fails the test too, and a caller that has
 * looked at one and decided it cannot apply says so through `skip`, with its reason, like any
 * other rule.
 */
export async function findA11yViolations(
  container: HTMLElement = document.body,
  options: A11yOptions = {},
): Promise<A11yFinding[]> {
  for (const [id, why] of Object.entries(options.skip ?? {})) {
    // `skip` is a map rather than a list so it carries the reason. A suppressed rule has to
    // stay arguable by whoever reads it next. A type cannot ask for a reason that means
    // something, but it can refuse an empty one, so the discipline is carried by code, not
    // only by the comment above.
    if (!why.trim()) {
      throw new Error(
        `expectNoA11yViolations was told to skip "${id}" with no reason. Say why the rule ` +
          `cannot apply to this tree, so the next reader can argue with it.`,
      );
    }
  }
  const skipped = [
    ...NEEDS_A_REAL_BROWSER,
    ...(options.pageLevel ? [] : ANSWERED_BY_THE_APP_SHELL),
    ...Object.keys(options.skip ?? {}),
  ];
  // The audit runs inside `act`, which is not tidiness. It is the longest await most of these
  // tests contain, so every read still in flight when it starts lands during it. Outside
  // `act`, those arrivals are state updates outside one, which `setup.ts` fails the test for,
  // naming components the test never touched, so the failure reads as a bug in the tree rather
  // than in the wait. Doing it here means no caller has to know that.
  let results!: axe.AxeResults;
  await act(async () => {
    results = await axe.run(container, {
      rules: Object.fromEntries(skipped.map((id) => [id, { enabled: false }])),
      // The tags axe itself maps to WCAG 2.1 A and AA, plus its best-practice set. Anything
      // outside them is advisory and would make this gate argue about taste.
      runOnly: {
        type: "tag",
        values: ["wcag2a", "wcag2aa", "wcag21a", "wcag21aa", "best-practice"],
      },
      // `passes` and `inapplicable` are never read, and collecting them means building a node
      // report for every passing element on the page. On the settings panel that took 5.1
      // seconds, over vitest's 5-second default, so the test failed as a timeout rather than
      // on anything it found. Excluding them brings it to well under a second. `incomplete` is
      // cheap by comparison. Across every audit in this suite it holds a single node.
      resultTypes: ["violations", "incomplete"],
    });
  });
  const order = { critical: 0, serious: 1, moderate: 2, minor: 3 } as const;
  const found: A11yFinding[] = [
    ...results.violations.map((r) => ({ ...r, deferred: false })),
    ...results.incomplete.map((r) => ({ ...r, deferred: true })),
  ];
  return found.sort(
    (a, b) =>
      (order[a.impact as keyof typeof order] ?? 4) - (order[b.impact as keyof typeof order] ?? 4),
  );
}

/** Formats findings the same way setup.ts formats its own, stating what broke, where, and
 *  what to do. */
export function describeA11yViolations(violations: A11yFinding[]): string {
  return violations
    .map((violation) => {
      const nodes = violation.nodes
        .slice(0, 3)
        .map((node) => `      ${node.html}`)
        .join("\n");
      const more =
        violation.nodes.length > 3 ? `\n      ...and ${violation.nodes.length - 3} more` : "";
      // A deferred rule is not a lesser one. It is an undecided one, and saying which is which
      // is the difference between "fix this" and "go and look". Reading a deferred entry as a
      // failed assertion sends someone hunting for a defect axe never claimed to find.
      const lead = violation.deferred
        ? `${violation.impact ?? "unknown"}, axe could not decide`
        : (violation.impact ?? "unknown");
      return (
        `  ${lead}: ${violation.help} (${violation.id})\n` +
        `${nodes}${more}\n      ${violation.helpUrl}`
      );
    })
    .join("\n\n");
}

/**
 * Fails the test if the rendered tree has any accessibility violation, or any rule axe could
 * not rule out (see `findA11yViolations` for why the undecided ones count).
 *
 * Pass `skip` only with a reason, so a suppressed rule stays arguable later:
 * `await expectNoA11yViolations(container, { skip: { "aria-required-children": "why" } })`.
 * An empty reason throws rather than suppressing.
 */
export async function expectNoA11yViolations(
  container: HTMLElement = document.body,
  options: A11yOptions = {},
): Promise<void> {
  const violations = await findA11yViolations(container, options);
  if (violations.length === 0) return;
  const deferred = violations.filter((v) => v.deferred).length;
  throw new Error(
    `axe found ${violations.length} accessibility problem` +
      `${violations.length === 1 ? "" : "s"} in the rendered tree` +
      `${deferred ? ` (${deferred} it could not decide)` : ""}:\n\n` +
      `${describeA11yViolations(violations)}\n\n` +
      `Each one is something an operator using a screen reader or the keyboard hits for real; ` +
      `one axe could not decide is one to go and look at, in a browser if need be. ` +
      `Fix the markup; suppress a rule only with expectNoA11yViolations(container, ` +
      `{ skip: { "<rule-id>": "<why it cannot apply here>" } }).`,
  );
}
