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
    // The whole reason this is a runtime gate. A static linter reads the JSX below, sees a
    // <Field> it knows nothing about, and reports nothing. axe reads the DOM React built.
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
});
