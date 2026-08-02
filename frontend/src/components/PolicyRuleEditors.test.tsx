// SPDX-License-Identifier: AGPL-3.0-or-later
//
// The value suggester's keyboard contract.
//
// It is an aria-activedescendant listbox: the <input> keeps DOM focus and the arrow keys move
// which option is marked current. That is a contract the browser does not help with -- nothing
// has focus in the list, so nothing scrolls on the operator's behalf -- and the app's other
// popups were deliberately de-roled rather than pay it (the comment in ReviewQueue.tsx says so
// by name). This is the one place that pays it, so this is the one place that has to prove it.
import { QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, type MockInstance, vi } from "vitest";

import type { VocabField } from "../api";
import { expectNoA11yViolations } from "../test/a11y";
import { testQueryClient } from "../test/queryClient";
import { RemoveRulesEditor } from "./PolicyRuleEditors";

const { apiMock } = vi.hoisted(() => ({
  apiMock: { vocabulary: vi.fn(), vocabularyValues: vi.fn() },
}));

vi.mock("../api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../api")>()),
  api: apiMock,
}));

const GENRE: VocabField = {
  key: "genre",
  label: "Genre",
  help_text: "",
  type: "text",
  unit_suffix: "",
  ops: ["contains"],
};

/** Comfortably more than the popup's 14rem window holds, on purpose: the bug is invisible on
 *  a list that fits. Named by index so an assertion can say which one it meant. */
const VALUES = Array.from({ length: 40 }, (_, i) => `value-${String(i).padStart(2, "0")}`);

function optionNamed(name: string): HTMLElement {
  return screen.getByRole("option", { name });
}

async function openTheSuggester() {
  const user = userEvent.setup();
  const { container } = render(
    <QueryClientProvider client={testQueryClient()}>
      <RemoveRulesEditor condemn={[]} onCondemn={() => {}} mediaType="movie" />
    </QueryClientProvider>,
  );

  // Rule 137: the picker is a <select>, whose accessible name does not change while the
  // vocabulary loads, so it cannot gate itself -- wait for it to be enabled, not for the page.
  const picker = screen.getByRole("combobox", { name: "Field" });
  await waitFor(() => expect(picker).toBeEnabled());
  await waitFor(() => expect(screen.getByRole("option", { name: "Genre" })).toBeInTheDocument());
  await user.selectOptions(picker, "genre");

  const box = await screen.findByRole("combobox", { name: "Genre value" });
  await user.click(box);
  await screen.findByRole("listbox");
  return { user, box, container };
}

/** Held across the file and cleared per test. `vi.spyOn` hands back the EXISTING spy when the
 *  property is already one, so a spy created fresh inside each test carries the previous
 *  tests' calls with it -- which reads as the component scrolling when it did not, in exactly
 *  the case below that asserts it did not. There is no `restoreMocks` in vitest.config.ts. */
let scrollIntoView: MockInstance<Element["scrollIntoView"]>;

beforeEach(() => {
  apiMock.vocabulary.mockResolvedValue({ lane: "condemn", fields: [GENRE] });
  apiMock.vocabularyValues.mockResolvedValue({ field: "genre", values: VALUES });
  scrollIntoView = vi.spyOn(Element.prototype, "scrollIntoView");
  scrollIntoView.mockClear();
});

describe("arrowing through the value suggester", () => {
  it("scrolls the option it just marked into view", async () => {
    // The defect (#333): the popup is a 14rem scroll container holding roughly seven options,
    // and arrowing marked an option without moving the list, so past the seventh the operator
    // was choosing a value they could not see. `aria-activedescendant` moved and nothing else
    // did.
    const { user } = await openTheSuggester();

    await user.keyboard("{ArrowDown}{ArrowDown}{ArrowDown}");

    expect(optionNamed("value-02")).toHaveAttribute("aria-selected", "true");
    expect(scrollIntoView).toHaveBeenCalled();
    // "nearest" is the option that leaves an already-visible row where it is; "start" would
    // jerk the pane on every single press. Instant, so no `behavior` key at all.
    expect(scrollIntoView.mock.calls.at(-1)?.[0]).toEqual({ block: "nearest" });
    expect(scrollIntoView.mock.instances.at(-1)).toBe(optionNamed("value-02"));
  });

  it("follows the wrap from the first option round to the last", async () => {
    // The worst case for an unscrolled window, and the one a stepping test that only ever
    // presses ArrowDown never reaches: from the first option, ArrowUp jumps to option 40 of
    // 40, which is as far outside the visible window as this list goes.
    const { user } = await openTheSuggester();

    await user.keyboard("{ArrowDown}{ArrowUp}");

    expect(optionNamed("value-39")).toHaveAttribute("aria-selected", "true");
    expect(scrollIntoView.mock.instances.at(-1)).toBe(optionNamed("value-39"));
  });

  it("has no accessibility violations with the list open and an option marked", async () => {
    // The state the rest of this file is about, and the one a mounted-but-closed audit never
    // reaches: the listbox only exists while `show` is true, and `aria-activedescendant` only
    // points at anything once a step has happened. Auditing the resting box would report on a
    // tree that has neither.
    const { user, container } = await openTheSuggester();
    await user.keyboard("{ArrowDown}");

    await expectNoA11yViolations(container);
  });

  it("does not scroll when the pointer is what moved the mark", async () => {
    // `onMouseEnter` sets the same `highlight` the arrow keys do, so scrolling on every change
    // would drag the list out from under the pointer that is aiming at it -- the row under the
    // cursor moves away mid-hover, and the operator clicks whatever slid into its place. Only
    // a keyboard step scrolls, which is the guard `ReviewQueue.tsx` reached for first.
    const { user } = await openTheSuggester();
    scrollIntoView.mockClear();

    await user.hover(optionNamed("value-05"));

    expect(optionNamed("value-05")).toHaveAttribute("aria-selected", "true");
    expect(scrollIntoView).not.toHaveBeenCalled();
  });
});
