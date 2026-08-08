// SPDX-License-Identifier: AGPL-3.0-or-later
//
// Two contracts, one file: the value suggester's keyboard behavior, and the keep-rule
// composer's membership rules.
//
// The suggester is an aria-activedescendant listbox: the <input> keeps DOM focus and the
// arrow keys move which option is marked current. That is a contract the browser does not
// help with -- nothing has focus in the list, so nothing scrolls on the operator's behalf --
// and the app's other popups were deliberately de-roled rather than pay it (the comment in
// ReviewQueue.tsx says so by name). This is the one place that pays it, so this is the one
// place that has to prove it.
//
// The membership rules ("On one of your lists") are how every list protects: a hard rule
// keeps outright, a lean is a FLAT discount. What is pinned is the exact body each composer
// emits, because the server validates it (`GradedKeepSpec._valid_keep`) and a wrong shape is
// a keep rule that refuses to save.
import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, type MockInstance, vi } from "vitest";

import type { Condition, GradedKeep, VocabField } from "../api";
import { expectNoA11yViolations } from "../test/a11y";
import { renderWithProviders } from "../test/renderWithProviders";
import { KeepRulesEditor, RemoveRulesEditor } from "./PolicyRuleEditors";

const { apiMock } = vi.hoisted(() => ({
  apiMock: { vocabulary: vi.fn(), vocabularyValues: vi.fn(), listConfigs: vi.fn() },
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
  const { container } = renderWithProviders(
    <RemoveRulesEditor condemn={[]} onCondemn={() => {}} mediaType="movie" />,
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

// ---------------------------------------------------------------------------
// Membership keep rules: "On one of your lists", both strengths.
// ---------------------------------------------------------------------------

/** The field as the protect vocabulary serves it (`engine/fields.py`). */
const ON_LIST: VocabField = {
  key: "on_list",
  label: "On one of your lists",
  help_text: "Matches a list by the name it has on Settings → Lists.",
  type: "text",
  unit_suffix: "",
  ops: ["eq", "in", "contains"],
};

/** A numeric protect field beside it, so the composer's on_list branches are asserted against
 *  a sibling that keeps the ramp controls. */
const VOTES: VocabField = {
  key: "imdb_votes",
  label: "IMDb vote count",
  help_text: "",
  type: "count",
  unit_suffix: "votes",
  ops: ["gte", "lte"],
};

function renderKeepEditor(over: { conditions?: Condition[]; keeps?: GradedKeep[] } = {}) {
  const onConditions = vi.fn();
  const onKeeps = vi.fn();
  renderWithProviders(
    <KeepRulesEditor
      conditions={over.conditions ?? []}
      keeps={over.keeps ?? []}
      gateIds={[]}
      mediaType="movie"
      onConditions={onConditions}
      onKeeps={onKeeps}
    />,
  );
  return { onConditions, onKeeps, user: userEvent.setup() };
}

describe("a hard keep rule on a list", () => {
  beforeEach(() => {
    apiMock.vocabulary.mockResolvedValue({ lane: "protect", fields: [ON_LIST, VOTES] });
    apiMock.listConfigs.mockResolvedValue([
      { id: 1, name: "Never Reap", source: "plex_collection", config: {}, policy_use: [] },
      { id: 2, name: "IMDb Top 250", source: "imdb", config: {}, policy_use: [] },
    ]);
  });

  it("hides the comparison, offers the lists by name, and emits field/eq/name", async () => {
    const { onConditions, user } = renderKeepEditor();

    const picker = screen.getByRole("combobox", { name: "Field" });
    await waitFor(() =>
      expect(screen.getByRole("option", { name: "On one of your lists" })).toBeInTheDocument(),
    );
    await user.selectOptions(picker, "on_list");

    // A membership's one comparison is "is": no op select renders, only the list picker.
    expect(screen.queryByRole("combobox", { name: "Comparison" })).not.toBeInTheDocument();
    const lists = await screen.findByRole("combobox", { name: "Which list" });
    await waitFor(() =>
      expect(screen.getByRole("option", { name: "Never Reap" })).toBeInTheDocument(),
    );

    // Nothing picked yet: Add waits, and the sentence names the wait (rule 42's shape).
    expect(screen.getByRole("button", { name: "Add rule" })).toBeDisabled();
    expect(screen.getByText("Pick a list to add this rule.")).toBeInTheDocument();

    await user.selectOptions(lists, "Never Reap");
    await user.click(screen.getByRole("button", { name: "Add rule" }));

    expect(onConditions).toHaveBeenCalledWith([
      { field: "on_list", op: "eq", value: "Never Reap" },
    ]);
  });

  it("renders a stored rule as a sentence about the list, even one whose list is gone", async () => {
    apiMock.listConfigs.mockResolvedValue([]);
    renderKeepEditor({ conditions: [{ field: "on_list", op: "eq", value: "Old list" }] });

    expect(await screen.findByText(/Keep it when on your list Old list/)).toBeInTheDocument();
  });
});

describe("a lean keep rule on a list", () => {
  beforeEach(() => {
    apiMock.vocabulary.mockResolvedValue({ lane: "protect", fields: [ON_LIST, VOTES] });
    apiMock.listConfigs.mockResolvedValue([
      { id: 1, name: "Never Reap", source: "plex_collection", config: {}, policy_use: [] },
    ]);
  });

  async function openLean(user: ReturnType<typeof userEvent.setup>) {
    await user.click(await screen.findByRole("button", { name: "Leans toward keeping" }));
    const picker = screen.getByRole("combobox", { name: "Field" });
    await waitFor(() =>
      expect(screen.getByRole("option", { name: "On one of your lists" })).toBeInTheDocument(),
    );
    await user.selectOptions(picker, "on_list");
  }

  it("offers the membership field first, flat: no direction, no ramp end", async () => {
    const { user } = renderKeepEditor();
    await openLean(user);

    const options = screen
      .getAllByRole("option")
      .map((o) => o.textContent)
      .filter((t) => t !== "when…" && t !== "Pick a list…" && t !== "Never Reap");
    expect(options[0]).toBe("On one of your lists");

    // Membership isn't a number, so the ramp controls stay off the page for it.
    expect(screen.queryByRole("group", { name: "Which way it leans" })).not.toBeInTheDocument();
    expect(screen.queryByLabelText("Full effect at")).not.toBeInTheDocument();
    expect(screen.getByLabelText("Points this rule takes off")).toBeInTheDocument();
  });

  it("emits the flat lean the server validates: value set, inert ramp, points off", async () => {
    const { onKeeps, user } = renderKeepEditor();
    await openLean(user);

    await user.selectOptions(await screen.findByRole("combobox", { name: "Which list" }), [
      "Never Reap",
    ]);
    await user.click(screen.getByRole("button", { name: "Add rule" }));

    expect(onKeeps).toHaveBeenCalledWith([
      {
        name: "Never Reap",
        field: "on_list",
        value: "Never Reap",
        max_discount: 15,
        floor: 0,
        saturate_at: 1,
        direction: "high_keeps",
      },
    ]);
  });

  it("renders a stored membership lean by its stored name, with its points", async () => {
    apiMock.listConfigs.mockResolvedValue([]);
    renderKeepEditor({
      keeps: [
        {
          name: "Old list",
          field: "on_list",
          value: "Old list",
          max_discount: 20,
          floor: 0,
          saturate_at: 1,
          direction: "high_keeps",
        },
      ],
    });

    expect(await screen.findByText(/On your list Old list/)).toBeInTheDocument();
    // "by", not "up to": membership is not a number, so this rule pays its whole discount or
    // none of it. "up to" is this file's word for a ramp, and it was printed on both branches,
    // describing an all-or-nothing rule as a sliding one.
    expect(screen.getByText(/lowers the score by −20 points/)).toBeInTheDocument();
    expect(screen.queryByText(/up to −20 points/)).not.toBeInTheDocument();
  });
});

/** Choose the membership field, once the vocabulary that offers it has landed. user-event
 *  reports a select with no matching option as a no-op, so acting a turn early does nothing
 *  and fails later on a state the no-op never produced (rule 137). */
async function pickTheListField(user: ReturnType<typeof userEvent.setup>) {
  await waitFor(() =>
    expect(screen.getByRole("option", { name: "On one of your lists" })).toBeInTheDocument(),
  );
  await user.selectOptions(screen.getByRole("combobox", { name: "Field" }), "on_list");
}

describe("one list, one keep rule", () => {
  // Both strengths were offered whatever rules already existed, so a list could be kept
  // outright AND leaned on. The outright rule decides the item alone, so the lean could never
  // change an outcome, and the operator was tuning points that could not matter (#510).
  beforeEach(() => {
    apiMock.vocabulary.mockResolvedValue({ lane: "protect", fields: [ON_LIST, VOTES] });
    apiMock.listConfigs.mockResolvedValue([
      { id: 1, name: "Never Reap", source: "plex_collection", config: {}, policy_use: [] },
      { id: 2, name: "IMDb Top 250", source: "imdb", config: {}, policy_use: [] },
    ]);
  });

  it("does not offer a list an outright rule already names, at either strength", async () => {
    const { user } = renderKeepEditor({
      conditions: [{ field: "on_list", op: "eq", value: "Never Reap" }],
    });

    await pickTheListField(user);
    const hard = await screen.findByRole("combobox", { name: "Which list" });
    await waitFor(() =>
      expect(screen.getByRole("option", { name: "IMDb Top 250" })).toBeInTheDocument(),
    );
    expect(within(hard).queryByRole("option", { name: "Never Reap" })).not.toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Leans toward keeping" }));
    await pickTheListField(user);
    const lean = await screen.findByRole("combobox", { name: "Which list" });

    expect(within(lean).queryByRole("option", { name: "Never Reap" })).not.toBeInTheDocument();
    expect(within(lean).getByRole("option", { name: "IMDb Top 250" })).toBeInTheDocument();
  });

  it("does not offer a list a lean already names either, so the lean cannot double", async () => {
    // Two leans on one list both evaluate and `score()` subtracts the sum, so 15 points twice
    // is 30 off -- and `uniqueName` suffixed the second so the body validated.
    const { user } = renderKeepEditor({
      keeps: [
        {
          name: "Never Reap",
          field: "on_list",
          value: "Never Reap",
          max_discount: 15,
          floor: 0,
          saturate_at: 1,
          direction: "high_keeps",
        },
      ],
    });

    await user.click(screen.getByRole("button", { name: "Leans toward keeping" }));
    await pickTheListField(user);
    const lists = await screen.findByRole("combobox", { name: "Which list" });

    expect(within(lists).queryByRole("option", { name: "Never Reap" })).not.toBeInTheDocument();
  });

  it("matches the stored rule against the list name the way the scan does", async () => {
    // Case-folded on both sides (rule 88): a rule stored as the operator typed it then still
    // names the list the scan will match, so offering it again would compose the pair.
    const { user } = renderKeepEditor({
      conditions: [{ field: "on_list", op: "eq", value: "never reap" }],
    });

    await pickTheListField(user);
    const lists = await screen.findByRole("combobox", { name: "Which list" });

    expect(within(lists).queryByRole("option", { name: "Never Reap" })).not.toBeInTheDocument();
  });

  it("says why the picker is empty when every list already has a rule", async () => {
    // Distinct from "you have no lists yet", which sends the operator to add one they do not
    // need, and from a failed read, which is not their doing at all (rules 17/36).
    const { user } = renderKeepEditor({
      conditions: [
        { field: "on_list", op: "eq", value: "Never Reap" },
        { field: "on_list", op: "eq", value: "IMDb Top 250" },
      ],
    });

    await pickTheListField(user);

    expect(
      await screen.findByText(
        "Every list already has a keep rule. Remove one above to give it a different strength.",
      ),
    ).toBeInTheDocument();
    expect(screen.queryByText(/You have no lists yet/)).not.toBeInTheDocument();
  });

  it("still says you have no lists when there are none", async () => {
    apiMock.listConfigs.mockResolvedValue([]);
    const { user } = renderKeepEditor();

    await pickTheListField(user);

    expect(await screen.findByText(/You have no lists yet/)).toBeInTheDocument();
  });
});
