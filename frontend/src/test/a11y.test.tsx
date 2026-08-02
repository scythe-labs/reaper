// SPDX-License-Identifier: AGPL-3.0-or-later
// Proves the a11y gate can go red, and says what it goes red ABOUT.
//
// Rule 118's shape, for a guard rather than an interlock: a scan nobody has watched fail is
// indistinguishable from a scan that collects nothing, and this one is a single `axe.run` call
// away from silently auditing an empty container. So each test here renders markup with one
// known defect and asserts the gate names that defect -- not merely that it threw.
import { render } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { expectNoA11yViolations, findA11yViolations } from "./a11y";

describe("the accessibility gate", () => {
  it("names an image with no alternative text", async () => {
    const { container } = render(<img src="/poster.png" />);
    const violations = await findA11yViolations(container);
    expect(violations.map((v) => v.id)).toContain("image-alt");
  });

  it("names a button a screen reader cannot say", async () => {
    // The defect that actually ships: an icon-only control with nothing to read out. It is what
    // rule 21 is about at the level of a control rather than a sentence.
    const { container } = render(
      <button type="button">
        <svg aria-hidden="true" />
      </button>,
    );
    const violations = await findA11yViolations(container);
    expect(violations.map((v) => v.id)).toContain("button-name");
  });

  it("sees through a custom component to the input it renders", async () => {
    // This is a runtime gate because a static linter reads the JSX below, sees a <Field> it
    // knows nothing about, and reports nothing. axe reads the DOM React built.
    function Field() {
      return <input type="text" />;
    }
    const { container } = render(
      <form>
        <Field />
      </form>,
    );
    const violations = await findA11yViolations(container);
    expect(violations.map((v) => v.id)).toContain("label");
  });

  it("throws a message naming the rule, the element, and what to do", async () => {
    const { container } = render(<img src="/poster.png" />);
    await expect(expectNoA11yViolations(container)).rejects.toThrow(/image-alt/);
    await expect(expectNoA11yViolations(container)).rejects.toThrow(/<img/);
  });

  it("passes markup that is actually correct", async () => {
    const { container } = render(
      <>
        <img src="/poster.png" alt="Poster" />
        <label htmlFor="q">Search</label>
        <input id="q" type="text" />
      </>,
    );
    await expectNoA11yViolations(container);
  });

  it("skips only what it is told to, and only with a reason", async () => {
    const { container } = render(<img src="/poster.png" />);
    await expectNoA11yViolations(container, {
      skip: { "image-alt": "proving the escape hatch works, not excusing a real defect" },
    });
  });

  it("refuses a skip with no reason rather than granting it", async () => {
    // The reason is what keeps a suppression arguable by the next reader, so an empty one is
    // the shape to refuse: it reads in a diff exactly like a considered exemption.
    const { container } = render(<img src="/poster.png" />);
    await expect(
      expectNoA11yViolations(container, { skip: { "image-alt": "  " } }),
    ).rejects.toThrow(/no reason/i);
  });

  it("names a rule axe could not decide, which jsdom files apart from its failures", async () => {
    // The one that made this gate necessary AND nearly made it useless. A focusable element
    // inside `aria-hidden` is a serious WCAG 4.1.2 failure, and `Settings.tsx` maintains that
    // invariant by hand at every `aria-hidden` site in the app -- but under jsdom axe returns
    // it as `incomplete`, not `violations`. A gate reading only `violations` passes this markup
    // without a word, which is why the deferred bucket counts too.
    const { container } = render(
      <div aria-hidden="true">
        <button type="button">Reap</button>
      </div>,
    );
    const violations = await findA11yViolations(container);
    expect(violations.map((v) => v.id)).toContain("aria-hidden-focus");
    expect(violations.find((v) => v.id === "aria-hidden-focus")?.deferred).toBe(true);
    await expect(expectNoA11yViolations(container)).rejects.toThrow(/could not decide/i);
  });

  it("audits the tree the test rendered when it is handed no container", async () => {
    // Seven call sites pass nothing and rely on this default. Nothing used to fail if it were
    // repointed at another attached-but-empty node -- axe returns [] for one silently, and only
    // a DETACHED node throws -- so the default that carries those audits was itself unpinned.
    render(<img src="/poster.png" />);
    const violations = await findA11yViolations();
    expect(violations.map((v) => v.id)).toContain("image-alt");
  });

  it("answers `region` only for the caller that says it is the whole page", async () => {
    // `pageLevel` is the only thing that re-enables `region`, and `region` is the only guard on
    // the landmarks in `App` and `Login`. Neutering the flag used to leave every test green.
    //
    // Audited against `document.body`, the way both real `pageLevel` callers do it: `region`
    // asks what sits outside a landmark at the top of the PAGE, so scoping it to the render
    // container asks a question it does not answer, and it returns nothing either way.
    render(<p>Loose text in no landmark at all.</p>);
    expect(
      (await findA11yViolations(document.body, { pageLevel: true })).map((v) => v.id),
    ).toContain("region");
    expect((await findA11yViolations(document.body)).map((v) => v.id)).not.toContain("region");
  });
});
