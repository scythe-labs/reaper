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

import type { Condition, GradedKeep, ListConfig, VocabField } from "../api";
import i18next from "../i18n";
import { expectNoA11yViolations } from "../test/a11y";
import { renderWithProviders } from "../test/renderWithProviders";
import { KeepRulesEditor, RemoveRulesEditor } from "./PolicyRuleEditors";

const { apiMock } = await vi.hoisted(async () => ({
  apiMock: (await import("../test/apiMock")).makeApiMock(),
}));

vi.mock("../api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../api")>()),
  api: apiMock,
}));

/** A field's label, read the way the component does now: off the real catalog by key, not
 *  the wire. The vocabulary fixtures below carry no English. */
const label = (key: string): string => i18next.t(`why.field.${key}`);

const GENRE: VocabField = {
  key: "genre",
  type: "text",
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

  // The picker is a <select>, whose accessible name does not change while the vocabulary
  // loads, so it cannot gate itself. Wait for it to be enabled, not for the page.
  const picker = screen.getByRole("combobox", { name: "Field" });
  await waitFor(() => expect(picker).toBeEnabled());
  await waitFor(() =>
    expect(screen.getByRole("option", { name: label("genre") })).toBeInTheDocument(),
  );
  await user.selectOptions(picker, "genre");

  const box = await screen.findByRole("combobox", {
    name: i18next.t("policyRules.suggestInput.valueAriaLabel", { field: label("genre") }),
  });
  await user.click(box);
  await screen.findByRole("listbox");
  return { user, box, container };
}

/** Held across the file and cleared per test. `vi.spyOn` hands back the existing spy when the
 *  property is already one, so a spy created fresh inside each test carries the previous
 *  tests' calls with it. That reads as the component scrolling when it did not, in exactly
 *  the case below that asserts it did not. There is no `restoreMocks` in vitest.config.ts. */
let scrollIntoView: MockInstance<Element["scrollIntoView"]>;

beforeEach(() => {
  apiMock.vocabulary.mockResolvedValue({ lane: "condemn", fields: [GENRE] });
  apiMock.vocabularyValues.mockResolvedValue({ field: "genre", values: VALUES });
  scrollIntoView = vi.spyOn(Element.prototype, "scrollIntoView");
  scrollIntoView.mockClear();
});

/** A list definition as the picker reads it (`/api/lists/configured`). `authorable_media` is
 *  the server's authoritative scope: the policies a keep rule on this list may protect. Empty
 *  means offer on neither, because the type is not known yet (an unsynced list). */
function listCfg(
  id: number,
  name: string,
  authorable_media: ("movie" | "tv")[],
  source: ListConfig["source"] = "plex_collection",
): ListConfig {
  return { id, name, source, config: {}, policy_use: [], authorable_media };
}

describe("arrowing through the value suggester", () => {
  it("scrolls the option it just marked into view", async () => {
    // The popup is a 14rem scroll container holding roughly seven options. Arrowing past the
    // seventh must move the list, or the operator ends up choosing a value they cannot see
    // while `aria-activedescendant` moves and nothing else does.
    const { user } = await openTheSuggester();

    await user.keyboard("{ArrowDown}{ArrowDown}{ArrowDown}");

    expect(optionNamed("value-02")).toHaveAttribute("aria-selected", "true");
    expect(scrollIntoView).toHaveBeenCalled();
    // "nearest" is the option that leaves an already-visible row where it is. "start" would
    // jerk the pane on every single press. This is instant, so no `behavior` key at all.
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
    // would drag the list out from under the pointer that is aiming at it. The row under the
    // cursor would move away mid-hover, and the operator would click whatever slid into its
    // place. Only a keyboard step scrolls, matching the guard `ReviewQueue.tsx` uses.
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
  type: "text",
  ops: ["eq", "in", "contains"],
};

/** A numeric protect field beside it, so the composer's on_list branches are asserted against
 *  a sibling that keeps the ramp controls. */
const VOTES: VocabField = {
  key: "imdb_votes",
  type: "count",
  ops: ["gte", "lte"],
};

function renderKeepEditor(
  over: { conditions?: Condition[]; keeps?: GradedKeep[]; mediaType?: "movie" | "tv" } = {},
) {
  const onConditions = vi.fn();
  const onKeeps = vi.fn();
  renderWithProviders(
    <KeepRulesEditor
      conditions={over.conditions ?? []}
      keeps={over.keeps ?? []}
      gateIds={[]}
      mediaType={over.mediaType ?? "movie"}
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
      listCfg(1, "Never Reap", ["movie", "tv"]),
      listCfg(2, "IMDb Top 250", ["movie"], "imdb"),
    ]);
  });

  it("hides the comparison, offers the lists by name, and emits field/eq/name", async () => {
    const { onConditions, user } = renderKeepEditor();

    const picker = screen.getByRole("combobox", { name: "Field" });
    await waitFor(() =>
      expect(screen.getByRole("option", { name: label("on_list") })).toBeInTheDocument(),
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
    apiMock.listConfigs.mockResolvedValue([listCfg(1, "Never Reap", ["movie", "tv"])]);
  });

  async function openLean(user: ReturnType<typeof userEvent.setup>) {
    await user.click(await screen.findByRole("button", { name: "Leans toward keeping" }));
    const picker = screen.getByRole("combobox", { name: "Field" });
    await waitFor(() =>
      expect(screen.getByRole("option", { name: label("on_list") })).toBeInTheDocument(),
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
    expect(options[0]).toBe(label("on_list"));

    // Membership isn't a number, so the ramp controls stay off the page for it.
    expect(screen.queryByRole("group", { name: "Which way it leans" })).not.toBeInTheDocument();
    expect(screen.queryByLabelText("full effect at")).not.toBeInTheDocument();
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
 *  and fails later on a state the no-op never produced. */
async function pickTheListField(user: ReturnType<typeof userEvent.setup>) {
  await waitFor(() =>
    expect(screen.getByRole("option", { name: label("on_list") })).toBeInTheDocument(),
  );
  await user.selectOptions(screen.getByRole("combobox", { name: "Field" }), "on_list");
}

describe("one list, one keep rule", () => {
  // A list must not be offered at both strengths at once: an outright rule decides the item
  // alone, so a lean on the same list could never change the outcome, leaving the operator
  // tuning points that cannot matter.
  beforeEach(() => {
    apiMock.vocabulary.mockResolvedValue({ lane: "protect", fields: [ON_LIST, VOTES] });
    apiMock.listConfigs.mockResolvedValue([
      listCfg(1, "Never Reap", ["movie", "tv"]),
      listCfg(2, "IMDb Top 250", ["movie"], "imdb"),
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
    // Two leans on one list would both evaluate, and `score()` subtracts their sum, so 15
    // points twice would be 30 off, even though `uniqueName` suffixes the second one so the
    // body still validates.
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
    // Matched case-insensitively on both sides: a rule stored exactly as the operator typed it
    // still names the list the scan will match, so offering it again would let the operator add
    // a second, duplicate rule.
    const { user } = renderKeepEditor({
      conditions: [{ field: "on_list", op: "eq", value: "never reap" }],
    });

    await pickTheListField(user);
    const lists = await screen.findByRole("combobox", { name: "Which list" });

    expect(within(lists).queryByRole("option", { name: "Never Reap" })).not.toBeInTheDocument();
  });

  it("says why the picker is empty when every list already has a rule", async () => {
    // Distinct from "you have no lists yet", which would send the operator to add a list they
    // do not need, and from a failed read, which is not their doing at all.
    const { user } = renderKeepEditor({
      conditions: [
        { field: "on_list", op: "eq", value: "Never Reap" },
        { field: "on_list", op: "eq", value: "IMDb Top 250" },
      ],
    });

    await pickTheListField(user);

    expect(
      await screen.findByText(
        "Every list this policy can keep already has a rule. Remove one above to give it a different strength.",
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

// ---------------------------------------------------------------------------
// The field types whose typed units are not their stored ones.
// ---------------------------------------------------------------------------

/** Three types as `engine/fields.py` serves them, one real field each.
 *
 *  Two convert. A size is typed in GB and stored in bytes, a rating is typed as 7.5 and stored
 *  as 75. Days are typed and stored alike, and are here because they reach `coerceValue` through
 *  its fall-through arm rather than a branch of their own. Nothing pinned any of the three, in
 *  either direction.
 *
 *  What a wrong one costs is a policy field off by a factor. A size rule storing the typed 50
 *  rather than 50 GB is a floor every title clears. A keep rule meant for the big files then
 *  keeps everything, and a remove rule meant for them flags everything. The number is wrong in
 *  the body the server stores, where nothing downstream can tell it from one the operator
 *  meant. */
const SIZE: VocabField = {
  key: "size_bytes",
  type: "bytes",
  ops: ["gte", "lte"],
};

const RATING: VocabField = {
  key: "imdb_rating",
  type: "rating_tenths",
  ops: ["gte", "lte"],
};

const DORMANCY: VocabField = {
  key: "days_unwatched",
  type: "days",
  ops: ["gte", "lte"],
};

/** Typed, stored, read back. `reads` is what the composed sentence must say: the number the
 *  operator typed, with its unit. A stored value rendering as anything else means one of the
 *  two directions is wrong, and only one of them writes the policy.
 *
 *  The rating reads `7.5/10`, with no space. Every other unit in the vocabulary is a word and
 *  keeps its space; `/10` is the one that is punctuation, and the three rows here hold both
 *  halves of that rule at once. */
const CONVERSIONS = [
  { field: SIZE, typed: "50", stored: 50_000_000_000, reads: "50 GB" },
  { field: RATING, typed: "7.5", stored: 75, reads: "7.5/10" },
  { field: DORMANCY, typed: "180", stored: 180, reads: "180 days" },
];

describe("a rule on a field whose stored units are not its typed ones", () => {
  beforeEach(() => {
    apiMock.vocabulary.mockResolvedValue({ lane: "protect", fields: [SIZE, RATING, DORMANCY] });
  });

  it.each(CONVERSIONS)(
    "stores a $field.key rule in the units the wire expects",
    async ({ field, typed, stored }) => {
      const { onConditions, user } = renderKeepEditor();

      // The picker's name holds still while the vocabulary loads, so it cannot gate itself, and
      // `selectOptions` against the empty list is a silent no-op.
      const picker = screen.getByRole("combobox", { name: "Field" });
      await waitFor(() =>
        expect(screen.getByRole("option", { name: label(field.key) })).toBeInTheDocument(),
      );
      await user.selectOptions(picker, field.key);

      const box = await screen.findByRole("spinbutton", {
        name: i18next.t("policyRules.suggestInput.valueAriaLabel", { field: label(field.key) }),
      });
      await user.type(box, typed);

      // Add gates itself on the value, so it is actable only once the typing has landed in
      // state, the same wait as the control above.
      const add = screen.getByRole("button", { name: "Add rule" });
      await waitFor(() => expect(add).toBeEnabled());
      await user.click(add);

      expect(onConditions).toHaveBeenCalledWith([{ field: field.key, op: "gte", value: stored }]);
    },
  );

  it.each(CONVERSIONS)(
    "reads a stored $field.key rule back as the number that was typed",
    async ({ field, stored, reads }) => {
      renderKeepEditor({ conditions: [{ field: field.key, op: "gte", value: stored }] });

      expect(
        await screen.findByText(`Keep it when ${label(field.key)} is at least ${reads}`),
      ).toBeInTheDocument();
    },
  );
});

describe("the list picker filtered to what the server says it may protect", () => {
  // `authorable_media` is the server's authoritative scope. A Movies policy offers a list whose
  // scope includes "movie": a movie or both-type list, or a synced-but-empty one the operator
  // may fill. It hides a shows-only list and an unsynced one whose type is unknown.
  beforeEach(() => {
    apiMock.vocabulary.mockResolvedValue({ lane: "protect", fields: [ON_LIST, VOTES] });
    apiMock.listConfigs.mockResolvedValue([
      listCfg(1, "Movie Night", ["movie"]),
      listCfg(2, "Saturday Cartoons", ["tv"]),
      listCfg(3, "Family Watchlist", ["movie", "tv"]),
      listCfg(4, "Empty Collection", ["movie", "tv"]), // synced but empty: offered on both
    ]);
  });

  it("offers movie, both, and synced-empty lists, and hides a shows-only one", async () => {
    const { user } = renderKeepEditor();
    await pickTheListField(user);
    const lists = await screen.findByRole("combobox", { name: "Which list" });
    await waitFor(() =>
      expect(within(lists).getByRole("option", { name: "Movie Night" })).toBeInTheDocument(),
    );

    expect(within(lists).getByRole("option", { name: "Family Watchlist" })).toBeInTheDocument();
    expect(within(lists).getByRole("option", { name: "Empty Collection" })).toBeInTheDocument();
    expect(
      within(lists).queryByRole("option", { name: "Saturday Cartoons" }),
    ).not.toBeInTheDocument();
    expect(
      screen.getByText(
        "One list isn't shown here: it holds only shows, which this policy can't keep.",
      ),
    ).toBeInTheDocument();
  });

  it("filters the lean picker the same way (rule 72)", async () => {
    const { user } = renderKeepEditor();
    await user.click(await screen.findByRole("button", { name: "Leans toward keeping" }));
    await pickTheListField(user);
    const lists = await screen.findByRole("combobox", { name: "Which list" });
    await waitFor(() =>
      expect(within(lists).getByRole("option", { name: "Movie Night" })).toBeInTheDocument(),
    );

    expect(
      within(lists).queryByRole("option", { name: "Saturday Cartoons" }),
    ).not.toBeInTheDocument();
  });

  it("flips the filter and the words on a shows policy", async () => {
    // The other direction, so a hardcoded "movie"/"shows" cannot pass: the movies-only list is
    // now the one that can never be kept, and the offered lists swap.
    const { user } = renderKeepEditor({ mediaType: "tv" });
    await pickTheListField(user);
    const lists = await screen.findByRole("combobox", { name: "Which list" });
    await waitFor(() =>
      expect(within(lists).getByRole("option", { name: "Saturday Cartoons" })).toBeInTheDocument(),
    );

    expect(within(lists).getByRole("option", { name: "Family Watchlist" })).toBeInTheDocument();
    expect(within(lists).queryByRole("option", { name: "Movie Night" })).not.toBeInTheDocument();
    expect(
      screen.getByText(
        "One list isn't shown here: it holds only movies, which this policy can't keep.",
      ),
    ).toBeInTheDocument();
  });

  it("hides an unsynced list and says to check it, while still offering a known one", async () => {
    // A synced-empty list is offered (verified, fillable); an unsynced one is withheld until a
    // check reads it. The two carry different notes because the fix differs.
    apiMock.listConfigs.mockResolvedValue([
      listCfg(1, "Movie Night", ["movie"]),
      listCfg(20, "Fresh List", []), // no sync has read it: offered on neither
    ]);
    const { user } = renderKeepEditor();
    await pickTheListField(user);
    const lists = await screen.findByRole("combobox", { name: "Which list" });
    await waitFor(() =>
      expect(within(lists).getByRole("option", { name: "Movie Night" })).toBeInTheDocument(),
    );

    expect(within(lists).queryByRole("option", { name: "Fresh List" })).not.toBeInTheDocument();
    expect(
      screen.getByText(
        "One list isn't shown here yet: it hasn't synced. Check it on Settings → Lists.",
      ),
    ).toBeInTheDocument();
  });

  it("says to add a movie list when the only list holds shows", async () => {
    apiMock.listConfigs.mockResolvedValue([listCfg(2, "Saturday Cartoons", ["tv"])]);
    const { user } = renderKeepEditor();

    await pickTheListField(user);

    expect(
      await screen.findByText(
        "None of your lists holds movies. Add a movie list on Settings → Lists.",
      ),
    ).toBeInTheDocument();
    // Not the "you have no lists" message: the operator has one, it just can't be kept here.
    expect(screen.queryByText(/You have no lists yet/)).not.toBeInTheDocument();
  });

  it("says to sync when the only list has never been read", async () => {
    apiMock.listConfigs.mockResolvedValue([listCfg(20, "Fresh List", [])]);
    const { user } = renderKeepEditor();

    await pickTheListField(user);

    expect(
      await screen.findByText(
        "Your lists haven't synced yet. Check them on Settings → Lists so Reaper knows what's on them.",
      ),
    ).toBeInTheDocument();
  });

  it("keeps the already-ruled copy true when an unsynced list is also hidden", async () => {
    // The keepable list is named and an unsynced one is hidden, so the picker is empty. The copy
    // is qualified ("this policy can keep") so it does not claim the unsynced list has a rule.
    apiMock.listConfigs.mockResolvedValue([
      listCfg(1, "Movie Night", ["movie"]),
      listCfg(20, "Fresh List", []),
    ]);
    const { user } = renderKeepEditor({
      conditions: [{ field: "on_list", op: "eq", value: "Movie Night" }],
    });

    await pickTheListField(user);

    expect(
      await screen.findByText(
        "Every list this policy can keep already has a rule. Remove one above to give it a different strength.",
      ),
    ).toBeInTheDocument();
  });
});
