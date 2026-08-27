// SPDX-License-Identifier: AGPL-3.0-or-later
// Checks the policy page's control grammar, plus two states an operator could not escape:
// a policy that failed to load offered no way to replace it, and applying a preset could
// leave the removal budget over 100 with Save disabled. Every test here fails if either
// regresses.
//
// The warning-anchor walk lives in `PolicyEditor.warnings.test.tsx`, split out because it
// alone took over half of this file's run time. Both files share the page setup in
// `src/test/policyEditor.tsx`.
import { act, fireEvent, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { CustomCondemn, PolicyBody, RewatchOddsBlock, RewatchOddsFit } from "../api";
import { expectNoA11yViolations } from "../test/a11y";
import { body, pace, policyEditorKit, tvBody } from "../test/policyEditor";
import { REPAIR_NOTICES } from "./PolicyEditor";

const { apiMock } = await vi.hoisted(async () => ({
  apiMock: (await import("../test/apiMock")).makeApiMock(),
}));

vi.mock("../api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api")>();
  return { ...actual, api: apiMock };
});

const { renderEditor, renderTvEditor } = policyEditorKit(apiMock);

// The signal-range probe fires on a 250ms debounce, so most tests here finish before it runs.
// It is mocked anyway: the test harness only catches an API mock that is missing a return
// value once that mock actually gets called, so an unmocked probe would sit silent until some
// later test's wait happened to trigger it. Mocked HERE rather than inside `renderEditor`,
// which runs after a test's own mock and would overwrite it.
beforeEach(() => {
  apiMock.probePolicy.mockResolvedValue({ points: 0.8 });
});

describe("a policy the server had to repair", () => {
  // Every repair the app knows, plus one it does not, must open the editor already dirty so
  // Save is enabled. A repaired policy is the only way out of a degraded scan: the notice
  // says "open the policy page, check X, and save," and nothing else clears it. A repair
  // that leaves the editor clean strands the operator with a permanent banner and no control
  // that fixes it.
  //
  // Read from REPAIR_NOTICES' keys, not a hand-written list, so a new repair is tested the
  // day it is added. The count below is asserted because a test that only checks "every
  // repair I found passes" says nothing if a repair silently drops out of that list.
  // `tests/test_policy_repairs.py` checks the same count against `PolicyRepair` on the
  // server, so both sides stay in sync.
  const KNOWN = Object.keys(REPAIR_NOTICES);

  it("knows the four repairs the server can report", () => {
    expect(KNOWN).toHaveLength(4);
  });

  it.each([...KNOWN, "a_repair_this_build_has_never_heard_of"])(
    "opens dirty on %s, so the degraded scan's remedy is followable",
    async (repair) => {
      const { container } = renderEditor({ body: body(), repairs: [repair] });

      await screen.findByRole("heading", { name: "Movies policy" });
      const savebar = container.querySelector(".savebar");
      expect(savebar).not.toBeNull();
      expect(screen.getByRole("button", { name: "Save changes" })).toBeEnabled();
    },
  );

  it("says what happened, in words about the repair that happened", async () => {
    renderEditor({ body: body(), repairs: ["lists_migrated"] });

    // "lists_migrated" is the one repair that shipped with no notice text of its own, so this
    // checks its exact sentence instead of only the savebar above.
    expect(await screen.findByText(/Your protected lists moved to Settings/)).toBeInTheDocument();
    // And it must not show the "points were rescaled" sentence, which is a different repair.
    expect(screen.queryByText(/points were rescaled/)).not.toBeInTheDocument();
  });

  it("still says something for a repair id it does not know", async () => {
    renderEditor({ body: body(), repairs: ["invented_later"] });

    expect(await screen.findByText(/Reaper had to change your saved policy/)).toBeInTheDocument();
  });

  it("says both when a body needed two repairs", async () => {
    renderEditor({ body: body(), repairs: ["lists_migrated", "rescaled"] });

    expect(await screen.findByText(/Your protected lists moved to Settings/)).toBeInTheDocument();
    expect(screen.getByText(/points were rescaled/)).toBeInTheDocument();
  });
});

describe("a policy that couldn't be read", () => {
  // This page holds the rules that condemn files: thresholds, weights, and protections, many
  // of them a number beside a switch. It is the longest form in the app. A control that reads
  // out the same as its neighbor is how an operator sets a threshold they never meant to touch.
  it("has no accessibility violations", async () => {
    const { container } = renderEditor({ body: body() });
    await screen.findByRole("heading", { name: "Movies policy" });
    // The page loads from seven separate reads, and the rule editors and the deletion switch
    // finish settling without changing anything a query can wait on. The accessibility check
    // reads the DOM directly, so the render must be fully settled before it runs.
    await act(async () => {});
    await expectNoA11yViolations(container);
  });

  it("says so on the load it happened, with nothing else dirty", async () => {
    // `fell_back` arrives alone: nothing of the stored body survived for a second repair to
    // be about (`services/profiles.py`). The notice must render outside the savebar, which
    // only shows when something is dirty, or it would be invisible in exactly the state it
    // is meant to explain.
    const { container } = renderEditor({ body: body(), repairs: ["fell_back"] });

    expect(await screen.findByText(/Your saved policy couldn't be read/)).toBeInTheDocument();
    // And the way out is offered: the savebar renders, so the fallback can be replaced.
    const savebar = container.querySelector(".savebar");
    expect(savebar).not.toBeNull();
    expect(screen.getByRole("button", { name: "Save changes" })).toBeEnabled();
    // The notice itself sits outside that savebar: it renders from the response's repair flag
    // alone, so no dirty check can hide it.
    expect(savebar?.textContent ?? "").not.toContain("couldn't be read");
  });

  it("stays quiet on an ordinary load", async () => {
    renderEditor({ body: body() });

    await screen.findByRole("heading", { name: "Movies policy" });
    expect(screen.queryByText(/Your saved policy couldn't be read/)).not.toBeInTheDocument();
  });
});

describe("a preset", () => {
  it("fits the operator's own rules into the 100 points instead of overshooting", async () => {
    const user = userEvent.setup();
    const mine: CustomCondemn = {
      kind: "boolean",
      name: "My rule",
      field: "requested",
      op: "eq",
      value: false,
      weight: 15,
    };
    const { container } = renderEditor({ body: body([mine]) });

    await user.click(await screen.findByRole("button", { name: "Cautious" }));

    await waitFor(() =>
      expect(container.querySelector(".budget-line")?.textContent).toContain(
        "100 of 100 removal points used",
      ),
    );
    // The rule survives, scaled, rather than being dropped to make room.
    expect(container.querySelector(".budget-line")?.textContent).toContain("yours");
    expect(screen.getByRole("button", { name: "Save changes" })).toBeEnabled();
    expect(screen.queryByText(/before saving/)).not.toBeInTheDocument();
  });

  it("lights the segment it just applied, even with a rule of the operator's own", async () => {
    // Applying a preset rescales the whole removal lane, so the built-in weights end up as the
    // shipped mix times a factor. The badge must still read as the preset's name in that case,
    // not "Custom," even though the raw weights no longer match the mix exactly.
    const user = userEvent.setup();
    const mine: CustomCondemn = {
      kind: "boolean",
      name: "My rule",
      field: "requested",
      op: "eq",
      value: false,
      weight: 15,
    };
    renderEditor({ body: body([mine]) });

    const cautious = await screen.findByRole("button", { name: "Cautious" });
    await user.click(cautious);

    await waitFor(() => expect(cautious).toHaveAttribute("aria-pressed", "true"));
    expect(cautious.className).toContain("active");
    expect(screen.queryByText(/Custom: your own tuning/)).not.toBeInTheDocument();
    expect(screen.getByText(/Staged, not saved/)).toBeInTheDocument();
  });

  it("still reads as Custom once a weight is hand-tuned", async () => {
    // The badge is wrong only if it claims a preset the draft no longer matches. A draft whose
    // built-in weights no longer match a preset's ratio is Custom, whether or not the operator
    // added rules of their own.
    const skewed = body();
    skewed.condemn_at = 82; // Cautious's threshold, so only the weights can decide.
    skewed.signals = [
      { signal: "unwatched", weight: 40, saturate_at: 365, floor: 0 },
      { signal: "few_watchers", weight: 40, saturate_at: 3, floor: 0 },
      { signal: "low_rating", weight: 20, saturate_at: 70, floor: 0 },
    ];
    renderEditor({ body: skewed });

    expect(await screen.findByText(/Custom: your own tuning/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Cautious" })).toHaveAttribute(
      "aria-pressed",
      "false",
    );
  });

  it("turns the caps back on when they were off (its help promises enforcement)", async () => {
    const user = userEvent.setup();
    renderEditor({ body: body() }, { ...pace, caps_enabled: false });

    // Caps start off, so the caps-off warning shows.
    expect(await screen.findByText(/No cap on run size/)).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Cautious" }));

    // Applying a preset re-enables caps: the warning clears, so the profile it would save is
    // capped, not uncapped.
    await waitFor(() => expect(screen.queryByText(/No cap on run size/)).not.toBeInTheDocument());
  });
});

describe("the caps switch and the copy that reads it", () => {
  it("the intent band drops the per-run limit claim when caps are off", async () => {
    renderEditor({ body: body() }, { ...pace, caps_enabled: false });

    // With caps off, the app skips the per-run checks, so the summary must not assert a hard
    // bound. It says the limit is gone until caps are turned back on.
    expect(
      await screen.findByText(/no per-run limit until you turn limits back on/),
    ).toBeInTheDocument();
    expect(screen.queryByText(/removes at most/)).not.toBeInTheDocument();
  });

  it("shows a recovery notice when the stored settings couldn't be read", async () => {
    const { container } = renderEditor({ body: body() }, { ...pace, settings_recovered: true });

    // The shipped defaults can be looser than what was saved, so the Pace page says so rather
    // than silently swapping them in.
    expect(
      await screen.findByText(/Your saved caps and grace couldn't be read/),
    ).toBeInTheDocument();

    // And the notice's own instruction ("a scan won't remove anything until you check these
    // and save") must be followable: the savebar renders with a live Save button, so the
    // operator is never told to save with nothing to press.
    const savebar = container.querySelector(".savebar");
    expect(savebar).not.toBeNull();
    expect(screen.getByRole("button", { name: "Save changes" })).toBeEnabled();
    expect(savebar?.textContent ?? "").toContain("pace and limits");
  });

  it("claims nothing about caps when the profile couldn't be read at all", async () => {
    // The fallback wording ("removes only within your caps") must not also cover a failed
    // read. Otherwise the one sentence an operator scans before arming would claim caps are in
    // force, directly above a section saying the settings behind them could not load.
    renderEditor({ body: body() }, new Error("profile unreadable"));

    const line = (await screen.findByText(/Right now Reaper flags a movie/)).textContent ?? "";
    expect(line).not.toContain("caps");
    expect(line).not.toContain("removes");
    // Still a sentence, just one that stops at what it can vouch for.
    expect(line).toContain("70 / 100");
    // The failed-load notice renders at the same time, so nothing on screen claims what it denies.
    expect(screen.getByText(/Couldn't load these settings/)).toBeInTheDocument();
  });

  it("keeps the neutral wording while the profile is still loading", async () => {
    // The distinction the fix turns on: not-yet-known is not the same as could-not-be-read.
    renderEditor({ body: body() }, "pending");

    const line = (await screen.findByText(/Right now Reaper flags a movie/)).textContent ?? "";
    expect(line).toContain("removes only within your caps");
  });
});

describe("the unknown-size allowance", () => {
  it("is checked as it stands on screen, not as it was last saved", async () => {
    // The warning renders directly beneath this box, but the server computes it from the
    // SAVED profile. Every other warning on this page describes the draft as it stands on
    // screen, so this value must be checked against the live edit, not the last save.
    const user = userEvent.setup();
    renderEditor({ body: body() }, { ...pace, max_unmeasured_per_run: 0 });

    const box = await screen.findByLabelText("Items with an unknown size");
    await user.clear(box);
    await user.type(box, "5");

    await waitFor(() => expect(apiMock.validatePolicy).toHaveBeenCalledWith(expect.anything(), 5));
  });
});

describe("the one Save button, over two independent saves", () => {
  it("still writes pace when the policy half is off the point budget", async () => {
    // Pace and limits have nothing to do with the removal budget, yet a grace edit could not
    // be saved until an unrelated weight was fixed elsewhere on the page. There is still one
    // Save button, but it must be able to save the pace half even while the policy half is
    // blocked.
    const user = userEvent.setup();
    apiMock.saveProfile.mockResolvedValue({ ...pace, grace_days: 21 });
    renderEditor({ body: body() });

    // Take the removal lane off 100 by hand, which is what blocks the policy save.
    const weight = (await screen.findAllByLabelText(/How much .* matters/))[0]!;
    fireEvent.change(weight, { target: { value: "5" } });
    await screen.findByText(/before saving/);

    // Now edit the other half.
    const grace = screen.getByLabelText("Grace period");
    await user.clear(grace);
    await user.type(grace, "21");

    const save = screen.getByRole("button", { name: "Save changes" });
    await waitFor(() => expect(save).toBeEnabled());
    // ...and the bar says which half it is leaving behind, rather than just refusing.
    expect(screen.getByText(/Save writes pace and limits only/)).toBeInTheDocument();

    await user.click(save);
    await waitFor(() => expect(apiMock.saveProfile).toHaveBeenCalled());
    expect(apiMock.savePolicy).not.toHaveBeenCalled();
  });

  it("stays disabled when the blocked policy is the only thing dirty", async () => {
    renderEditor({ body: body() });

    const weight = (await screen.findAllByLabelText(/How much .* matters/))[0]!;
    fireEvent.change(weight, { target: { value: "5" } });

    await waitFor(() =>
      expect(screen.getByRole("button", { name: "Save changes" })).toBeDisabled(),
    );
    // The bar does NOT add a "held back" line here: nothing is being written, and the points
    // notice beside it already says why. One subject, one sentence.
    expect(screen.queryByText(/Save writes pace and limits only/)).not.toBeInTheDocument();
    expect(screen.getByText(/Give out the other/)).toBeInTheDocument();
  });
});

describe("the rule editors when their vocabulary can't be read", () => {
  it("says the fetch failed instead of offering a picker with nothing in it", async () => {
    // A failed fetch must show an error and a retry, not an empty field dropdown. An empty
    // dropdown looks like "no fields to configure" rather than "the fetch failed," so an
    // operator could mistake a network problem for a feature with nothing in it.
    renderEditor({ body: body() }, pace, new Error("unreachable"));

    // Both lanes say it: the remove editor and the keep editor read separate vocabularies,
    // and the whole page is one scroll, so both are on screen at once.
    await waitFor(() =>
      expect(screen.getAllByText(/couldn't load the things a rule can look at/i)).toHaveLength(2),
    );
    // ...and neither offers a dropdown with nothing in it.
    expect(screen.queryAllByLabelText("Field")).toHaveLength(0);
  });

  it("offers the picker normally when the vocabulary loads", async () => {
    renderEditor({ body: body() });

    await waitFor(() => expect(screen.getAllByLabelText("Field").length).toBeGreaterThan(0));
    expect(
      screen.queryByText(/couldn't load the things a rule can look at/i),
    ).not.toBeInTheDocument();
  });
});

/** The editor opens on Movies, so a TV test must click through the same Movies/TV switch an
 *  operator uses. The draft is clean on load, so the switch does not stop for a confirm.
 *
 *  `shape` is applied after `renderEditor` (which seeds an empty one) and before the switch,
 *  which is when the season card, and its advisory query, first mount. */
describe("the TV one-line summary", () => {
  it("names each season protection only while its own switch is on", async () => {
    await renderTvEditor();

    const line = (await screen.findByText(/Right now Reaper flags a/)).textContent ?? "";
    expect(line).toContain(
      "always keeps the newest 2 seasons of a show, a show's first season, and anyone's mid-binge",
    );
  });

  it("claims nothing when every season protection is off", async () => {
    // The line an operator scans before arming must not claim a protection that is off. With
    // the floor at 0 and mid-binge OFF, it must not read "always keeps the newest 0 seasons of
    // a show and anyone's mid-binge," since both claims would be false.
    await renderTvEditor({
      keep_last_seasons: 0,
      keep_first_season: false,
      keep_in_progress: false,
    });

    const line = (await screen.findByText(/Right now Reaper flags a/)).textContent ?? "";
    expect(line).not.toContain("always keeps");
    expect(line).not.toContain("mid-binge");
    expect(line).not.toContain("newest 0");
    // The rest of the sentence still reads as a sentence.
    expect(line).toContain("removes at most");
  });

  it("drops just the clause that was switched off", async () => {
    await renderTvEditor({ keep_in_progress: false });

    const line = (await screen.findByText(/Right now Reaper flags a/)).textContent ?? "";
    expect(line).toContain("always keeps the newest 2 seasons of a show and a show's first season");
    expect(line).not.toContain("mid-binge");
  });
});

describe("the keep-last advisory", () => {
  it("counts shows outright when the floor applies to all of them", async () => {
    const { container } = await renderTvEditor(
      { keep_last_seasons: 1 },
      { total_shows: 4, season_counts: { 1: 3, 6: 1 } },
    );

    const note = await screen.findByText(/no season eligible for removal/);
    expect(note.textContent).toContain("With this setting, 3 of 4 shows");
    expect(note.textContent).not.toContain("up to");
    expect(container.querySelector(".help-warn")).toBeNull();
  });

  it("states an upper bound under “Requested only”, and never the everything warning", async () => {
    // The endpoint counts every show in the snapshot, while the floor under this scope only
    // reaches shows someone requested (plus ones Reaper cannot tell were requested). The exact
    // count needs a live request index the page does not have, so the figure must read as an
    // upper bound, not as if the scope were off.
    const { container } = await renderTvEditor(
      { keep_last_seasons: 1, keep_last_scope: "requested" },
      { total_shows: 3, season_counts: { 1: 3 } },
    );

    const note = await screen.findByText(/no season eligible for removal/);
    expect(note.textContent).toContain("up to 3 of 3 shows");
    // Every show is covered, but an upper bound cannot assert "you protected everything".
    expect(container.querySelector(".help-warn")).toBeNull();
  });

  it("does warn when all shows really are covered and the scope is every show", async () => {
    const { container } = await renderTvEditor(
      { keep_last_seasons: 1, keep_last_scope: "all" },
      { total_shows: 3, season_counts: { 1: 3 } },
    );

    await screen.findByText(/no season eligible for removal/);
    expect(container.querySelector(".help-warn")).not.toBeNull();
  });
});

describe("the rewatch keep card", () => {
  const SWITCH_NAME = "Keep titles most likely to be rewatched";

  it("renders on the movie policy", async () => {
    renderEditor({ body: body() });
    expect(await screen.findByRole("switch", { name: SWITCH_NAME })).toBeInTheDocument();
  });

  it("renders on the TV policy too, with TV wording, the hold half, and a tv-scoped fetch", async () => {
    // `apiMock` is module-level, so its call counts carry across every test in this file.
    // Cleared here so the assertion below counts only this test's own call.
    apiMock.rewatchOddsFit.mockClear();
    const fit: RewatchOddsFit = {
      blocks: [{ lo_days: 0, hi_days: 365, n: 20, k: 10, upper_bound_pct: 50, items: 40 }],
      total_items: 40,
    };
    renderEditor(
      {
        body: tvBody({
          gates: [{ gate: "rewatch_odds", enabled: true, threshold: 30, window_days: 0 }],
        }),
      },
      pace,
      null,
      [],
      "flags",
      "tv",
      fit,
    );

    expect(
      await screen.findByRole("switch", { name: "Keep shows most likely to be rewatched" }),
    ).toBeInTheDocument();
    expect(screen.getByLabelText("watched again by anyone at least")).toBeInTheDocument();
    // Same layout as movies: a TV body carries its own rewatch_odds gate row, so the hold half
    // renders and fetches the TV lane's own fit.
    expect(
      await screen.findByRole("switch", {
        name: "Keep shows most likely to be rewatched above a percentage",
      }),
    ).toBeInTheDocument();
    expect(apiMock.rewatchOddsFit).toHaveBeenCalledWith("tv");
    expect(
      await screen.findByText(
        "At 30%, this protects shows unwatched under about 1 year, 40 of 40.",
      ),
    ).toBeInTheDocument();
  });

  it("hides its three controls while the switch is off, like every other settings card", async () => {
    const user = userEvent.setup();
    renderEditor({ body: body() });
    const toggle = await screen.findByRole("switch", { name: SWITCH_NAME });

    expect(screen.getByLabelText("watched by anyone at least")).toBeInTheDocument();
    expect(screen.getByLabelText("most recently within")).toBeInTheDocument();
    expect(screen.getByLabelText("lowers the score by")).toBeInTheDocument();

    await user.click(toggle);

    expect(screen.queryByLabelText("watched by anyone at least")).not.toBeInTheDocument();
    expect(screen.queryByLabelText("most recently within")).not.toBeInTheDocument();
    expect(screen.queryByLabelText("lowers the score by")).not.toBeInTheDocument();
  });

  it("writes each control's edit into the draft, read off the same body the Save button posts", async () => {
    // `body()` seeds 15/8/365, values that differ from the server defaults, so a box that
    // merely echoes the default back cannot be mistaken for one that is actually wired. Each
    // assertion below then types in a THIRD value and reads it back from `validatePolicy`'s
    // own argument, rather than assuming the write landed.
    const user = userEvent.setup();
    renderEditor({ body: body() });
    await screen.findByRole("switch", { name: SWITCH_NAME });

    const viewings = screen.getByLabelText("watched by anyone at least");
    await user.clear(viewings);
    await user.type(viewings, "42");
    await waitFor(() =>
      expect(apiMock.validatePolicy.mock.calls.at(-1)?.[0].rewatch_min_viewings).toBe(42),
    );

    const discount = screen.getByLabelText("lowers the score by");
    await user.clear(discount);
    await user.type(discount, "33");
    await waitFor(() =>
      expect(apiMock.validatePolicy.mock.calls.at(-1)?.[0].rewatch_keep_discount).toBe(33),
    );

    // The recency box is a unit picker seeded at 365 days, which draws as "1 year" (the
    // friendliest unit `bestUnit` finds) -- so typing "9" here writes 9 years, not 9 days.
    const recent = screen.getByLabelText("most recently within");
    await user.clear(recent);
    await user.type(recent, "9");
    await waitFor(() =>
      expect(apiMock.validatePolicy.mock.calls.at(-1)?.[0].rewatch_recent_days).toBe(9 * 365),
    );
  });
});

describe("the rewatch-odds hold, the grouped card's second half (#554 stage 2)", () => {
  const HOLD_SWITCH_NAME = "Keep titles most likely to be rewatched above a percentage";
  const PERCENT_LABEL = "kept when the chance is at least";

  // Off the server default of 25: a fixture pinning 25 could not prove an edit reaches the
  // draft, since an untouched default would look the same.
  function holdGate(over: Partial<PolicyBody["gates"][number]> = {}) {
    return { gate: "rewatch_odds", enabled: true, threshold: 30, window_days: 0, ...over };
  }

  // Three rungs, each lower than the last, chosen so the cleared set actually changes between
  // the two thresholds the recompute test drives.
  const RUNGS: RewatchOddsBlock[] = [
    { lo_days: 0, hi_days: 365, n: 200, k: 122, upper_bound_pct: 68, items: 900 },
    { lo_days: 365, hi_days: 1095, n: 150, k: 45, upper_bound_pct: 38, items: 600 },
    { lo_days: 1095, hi_days: null, n: 100, k: 13, upper_bound_pct: 20, items: 500 },
  ];

  function measuredFit(over: Partial<RewatchOddsFit> = {}): RewatchOddsFit {
    return { blocks: RUNGS, total_items: 2500, ...over };
  }

  it("renders the hold's toggle and the fitted ladder from a seeded fit", async () => {
    renderEditor(
      { body: { ...body(), gates: [holdGate()] } },
      pace,
      null,
      [],
      "flags",
      "movie",
      measuredFit(),
    );

    expect(await screen.findByRole("switch", { name: HOLD_SWITCH_NAME })).toBeInTheDocument();
    // Rate is round(100*k/n), and the range is plain words off lo/hi -- not the raw days.
    expect(await screen.findByText("sat under 1 year")).toBeInTheDocument();
    expect(screen.getByText("61%")).toBeInTheDocument();
    expect(screen.getByText("1 to 3 years")).toBeInTheDocument();
    expect(screen.getByText("30%")).toBeInTheDocument();
    expect(screen.getByText("over 3 years")).toBeInTheDocument();
    expect(screen.getByText("13%")).toBeInTheDocument();
  });

  it("recomputes the echo when the threshold edit lands", async () => {
    const user = userEvent.setup();
    renderEditor(
      { body: { ...body(), gates: [holdGate({ threshold: 30 })] } },
      pace,
      null,
      [],
      "flags",
      "movie",
      measuredFit(),
    );

    // At 30%, the first two rungs clear (68 and 38 both >= 30): 900 + 600 of 2,500, and the
    // range takes the second rung's upper edge.
    expect(
      await screen.findByText(
        "At 30%, this protects titles unwatched under about 3 years, 1,500 of 2,500.",
      ),
    ).toBeInTheDocument();

    const percent = screen.getByLabelText(PERCENT_LABEL);
    await user.clear(percent);
    await user.type(percent, "50");

    // At 50%, only the first rung clears (68 >= 50, 38 does not): the sentence AND the
    // draft the Save button would post both move off the initial value.
    expect(
      await screen.findByText(
        "At 50%, this protects titles unwatched under about 1 year, 900 of 2,500.",
      ),
    ).toBeInTheDocument();
    await waitFor(() => {
      const gates = apiMock.validatePolicy.mock.calls.at(-1)?.[0].gates as PolicyBody["gates"];
      expect(gates.find((g) => g.gate === "rewatch_odds")?.threshold).toBe(50);
    });
  });

  it("hides the percentage control while the switch is off, but keeps the ladder", async () => {
    const user = userEvent.setup();
    renderEditor(
      { body: { ...body(), gates: [holdGate({ enabled: false })] } },
      pace,
      null,
      [],
      "flags",
      "movie",
      measuredFit(),
    );

    const toggle = await screen.findByRole("switch", { name: HOLD_SWITCH_NAME });
    expect(await screen.findByText("sat under 1 year")).toBeInTheDocument();
    expect(screen.queryByLabelText(PERCENT_LABEL)).not.toBeInTheDocument();

    await user.click(toggle);
    expect(await screen.findByLabelText(PERCENT_LABEL)).toBeInTheDocument();
  });

  it("says the fit could not be read, beside the control, rather than nothing", async () => {
    renderEditor(
      { body: { ...body(), gates: [holdGate({ enabled: false })] } },
      pace,
      null,
      [],
      "flags",
      "movie",
      new Error("network error"),
    );

    expect(
      await screen.findByText("Couldn't read your library's rewatch numbers."),
    ).toBeInTheDocument();
    // No reload advice: the savebar elsewhere on this page can be holding unsaved edits, and
    // reloading would lose them.
    expect(screen.queryByText(/reload/i)).not.toBeInTheDocument();
  });

  it("says to run a scan first when no scan has ever populated the fit", async () => {
    renderEditor(
      { body: { ...body(), gates: [holdGate({ enabled: false })] } },
      pace,
      null,
      [],
      "flags",
      "movie",
      { blocks: [], total_items: 0 },
    );

    expect(
      await screen.findByText("Run a scan first to see your library's own numbers."),
    ).toBeInTheDocument();
  });

  it("does not render the rewatch-odds row a second time through the plain protections list", async () => {
    renderEditor(
      { body: { ...body(), gates: [holdGate()] } },
      pace,
      null,
      [],
      "flags",
      "movie",
      measuredFit(),
    );

    expect(await screen.findAllByRole("switch", { name: HOLD_SWITCH_NAME })).toHaveLength(1);
  });
});

describe("a protection this build has no copy for", () => {
  it("gives each unknown gate a switch the operator can tell apart", async () => {
    // The simulator's fallback label is one shared string, and that is correct there: an id
    // appears once in a tally, where "Another protection, 7" reads fine. A switch is
    // different. Two unknown ids sharing one label would draw two controls with the same name
    // and no help text, and turning a protection off should never be a choice made blind.
    //
    // The labels must differ from EACH OTHER, so each unknown gate needs its own per-id
    // fallback label. This case is reachable only from a stale frontend bundle, since the SPA
    // ships inside the server's own image, which is why the bar here is just
    // "distinguishable" rather than fully plain language.
    renderEditor({
      body: {
        ...body(),
        gates: [
          { gate: "brand_new_gate", enabled: true, threshold: 1, window_days: 30 },
          { gate: "another_new_gate", enabled: false, threshold: 1, window_days: 30 },
        ],
      },
    });

    const first = await screen.findByLabelText("Brand New Gate");
    const second = await screen.findByLabelText("Another New Gate");
    expect(first).not.toBe(second);
    expect(screen.queryAllByLabelText("Another protection")).toHaveLength(0);
  });
});

describe("the hold on a title that came back (#553)", () => {
  const returnedBody = {
    ...body(),
    gates: [{ gate: "returned" as const, enabled: true, threshold: 548, window_days: 7 }],
  };

  it("offers both durations, each on the shared picker", async () => {
    // A number with a changeable unit uses the shared `QuantityInput` control, never a bare
    // number box beside loose unit text. Both knobs here are durations, so both use it, and
    // neither needed new control code.
    renderEditor({ body: returnedBody });

    const hold = await screen.findByLabelText("Keep a title that came back threshold");
    const absence = screen.getByLabelText("How long an absence counts");
    // The picker draws each number in the largest unit that divides it cleanly and hands the
    // parent days either way: 548 reads "1.5 years", 7 reads "1 week". Both assertions are on
    // the DRAWN value, which is what an operator sees.
    expect(hold).toHaveValue(1.5);
    expect(absence).toHaveValue(1);
    const holdUnit = screen.getByLabelText("Keep a title that came back threshold unit");
    const absenceUnit = screen.getByLabelText("How long an absence counts unit");
    expect(holdUnit).toHaveValue("years");
    expect(absenceUnit).toHaveValue("weeks");
  });

  it("says keep it FOR, not at least", async () => {
    // The hold lasts exactly this long. "At least 1.5 years" is the wrong sentence here, even
    // though it is what every other days-unit row on the page uses.
    renderEditor({ body: returnedBody });

    await screen.findByLabelText("Keep a title that came back threshold");
    expect(screen.getByText("keep it for")).toBeInTheDocument();
    expect(screen.queryByText("at least")).not.toBeInTheDocument();
  });

  it("binds the absence help to the control it explains, not to the row", async () => {
    // Help text binds to exactly one control, directly beneath it. The row's own help text is
    // about the hold duration; the absence duration needs its own, or the operator reads one
    // paragraph that is supposed to cover two different numbers.
    renderEditor({ body: returnedBody });

    await screen.findByLabelText("How long an absence counts");
    expect(screen.getByText(/left your library and was fetched again/)).toBeInTheDocument();
    expect(
      screen.getByText(/How long a title has to be missing before its return counts/),
    ).toBeVisible();
  });

  it("hides both durations while the protection is off", async () => {
    // A settings group's sub-controls render only while its toggle is on. They are hidden,
    // not merely disabled.
    renderEditor({
      body: { ...returnedBody, gates: [{ ...returnedBody.gates[0]!, enabled: false }] },
    });

    await screen.findByText("Keep a title that came back");
    expect(screen.queryByLabelText("Keep a title that came back threshold")).toBeNull();
    expect(screen.queryByLabelText("How long an absence counts")).toBeNull();
  });
});

describe("the gate that counts recent watchers", () => {
  it("offers the window its own warning tells the operator to change", async () => {
    // The server warns on `gates.server_popularity.window_days` and advises a year. This
    // control must exist on the page, or the warning names a value with nothing to change it.
    renderEditor({
      body: {
        ...body(),
        gates: [
          {
            gate: "server_popularity",
            enabled: true,
            threshold: 3,
            window_days: 365,
          },
        ],
      },
    });

    const window = await screen.findByLabelText("How far back recent plays count");
    expect(window).toHaveValue(1);
    expect(screen.getByText(/counting plays from the last/)).toBeInTheDocument();
    expect(screen.getByText(/How far back .recently. reaches/)).toBeInTheDocument();
  });

  it("shows the server's warning beside the protections it is about", async () => {
    // What matters is that the warning reaches the page beside the control it names, not
    // merely that the server sends it. An operator whose reap list came back empty needs to
    // see why, right next to the setting that explains it.
    //
    // A plain text search is not enough: an unanchored warning still renders somewhere, in
    // the catch-all stack at the foot of the page, so a bare `findByText` would pass even if
    // the anchor were renamed to nothing that matches. This test also checks the warning's
    // POSITION, right after the protections list.
    renderEditor(
      {
        body: {
          ...body(),
          gates: [
            {
              gate: "server_popularity",
              enabled: true,
              threshold: 3,
              window_days: 365,
            },
          ],
        },
      },
      pace,
      null,
      [
        {
          field: "gates.server_popularity.window_days",
          severity: "warn",
          reason: {
            k: "popularity_beyond_history",
            p: {
              window_days: 365,
              shortfall: { k: "cause.history_reach_short", p: { reach_days: 90 } },
              remedy: "lower",
            },
          },
        },
      ],
    );

    const warning = await screen.findByText(/Nothing will be flagged for removal/);

    expect(warning).toBeInTheDocument();
    // WarnBlock renders a fragment, so the anchored warning is the protections list's own
    // next sibling. The unanchored stack sits far below it, past the rating card.
    const gateList = screen.getByText(/How far back .recently. reaches/).closest("ul.rule-list");
    expect(gateList).not.toBeNull();
    expect(gateList?.nextElementSibling).toBe(warning);
  });

  it("anchors the gate-off window warning on the keep rules, the only surface it can be fixed from", async () => {
    // The mirror case of the test above: the control the warning names is off the page. With
    // `server_popularity` off, `PolicyBody.popularity_window_days` still hands 365 to the
    // operator's own keep-outright rules, so they block library-wide on a window whose picker
    // this editor deliberately hides. The rule itself is the one thing left to act on.
    //
    // A DECOY warning is added so the position check means something: with only one warning
    // on the page, an anchored warning and an unclaimed one can land in the same DOM position,
    // and a position check alone cannot tell them apart. The decoy is a second, unclaimed
    // warning that fills the catch-all stack, and it must render FIRST there, since
    // `unanchoredWarnings` preserves payload order. If the real anchor is missing, the keep
    // card's next sibling becomes the decoy instead of the real warning.
    renderEditor(
      {
        body: {
          ...body(),
          gates: [
            {
              gate: "server_popularity",
              enabled: false,
              threshold: 3,
              window_days: 365,
            },
          ],
          protect_conditions: [{ field: "recent_watchers", op: "gte", value: 1 }],
        },
      },
      pace,
      null,
      [
        {
          field: "unanchored_probe",
          severity: "warn",
          // A legacy-shaped reason renders its text verbatim (why.ts's composeIn), which is
          // exactly what a decoy fixture with no real backend id needs.
          reason: {
            k: "legacy",
            p: { text: "A warning no anchor claims, so it lands in the catch-all stack." },
          },
        },
        {
          field: "protect_conditions",
          severity: "warn",
          // The real id and params `policy_warnings.inspect`'s gate-off code path emits: the
          // catalog composes the sentence from these, so a drifted catalog entry fails this
          // test on its own instead of silently rendering as the shipped copy.
          reason: {
            k: "popularity_rules_beyond_history",
            p: {
              window_days: 365,
              shortfall: { k: "cause.history_reach_short", p: { reach_days: 90 } },
              rules: 1,
              field: "recent_watchers",
            },
          },
        },
      ],
    );

    const warning = await screen.findByText(/Nothing will be flagged for removal/);
    const keepCard = screen.getByRole("heading", { name: "Your own keep rules" }).closest("div");

    expect(keepCard).not.toBeNull();
    expect(keepCard?.nextElementSibling).toBe(warning);
    // The decoy is on the page, so a payload that silently dropped it could not pass the
    // assertion above by leaving the catch-all empty again.
    expect(screen.getByText(/A warning no anchor claims/)).toBeInTheDocument();
  });

  it("hides the window with the gate, like every other sub-control", async () => {
    renderEditor({
      body: {
        ...body(),
        gates: [
          {
            gate: "server_popularity",
            enabled: false,
            threshold: 3,
            window_days: 365,
          },
        ],
      },
    });

    await screen.findByText("Keep what your users actually watch");
    expect(screen.queryByLabelText("How far back recent plays count")).not.toBeInTheDocument();
  });
});

// --- warning anchors ---------------------------------------------------------
//
// Checks that every anchor in the EXPORTED list the editor renders from (`WARNING_ANCHORS`)
// has a renderer behind it. Claiming a field keeps its warning out of the bottom catch-all
// stack, so an anchor with no renderer behind it makes that warning disappear entirely.
//
// The walk reads the declaration itself, rather than a hand-written copy of the anchor list,
// so a new anchor with no renderer fails this test instead of passing silently. An anchor
// with no `warningsAt` call site has no renderer to paint its warning, and claiming the field
// has already excluded it from the catch-all stack, so the warning appears nowhere and the
// case below fails.
describe("the controls a screen reader has to tell apart", () => {
  // The two thresholds sit in a `<label className="field">` that wraps the label span, the live
  // value AND the `.help` paragraph. Any of those can become the control's accessible name if
  // the label is not scoped, which would announce the whole help paragraph on every drag and
  // never say which slider is which. The exact-match `getByLabelText` below checks the
  // accessible name is exactly the short label, nothing more.
  //
  // This test walks every slider on the page, so it must also count them: "every slider I
  // found has a name" passes even if the walk found nothing. The count below is every `range`
  // this fixture renders (the two thresholds, plus one weight per built-in signal in `body()`,
  // three), checked by hand against the source. Its limit: a slider in a section this fixture
  // does not mount is missing from both the count and the check, and the two gaps hide each
  // other.
  it("names both thresholds for their label, never for the help text under it", async () => {
    renderEditor({ body: body() });

    const condemn = await screen.findByLabelText("Put a title on the list once it scores");
    const floor = screen.getByLabelText("Judge a title only when there's enough to go on");
    expect(condemn).toHaveAttribute("type", "range");
    expect(floor).toHaveAttribute("type", "range");

    const sliders = document.querySelectorAll<HTMLInputElement>('input[type="range"]');
    expect(sliders).toHaveLength(5);
    for (const s of sliders) expect(s.getAttribute("aria-label")).toBeTruthy();
    // The two per signal must not answer to the same name, which is the failure this whole
    // test is about and the one a truthiness check on each cannot see.
    const names = [...sliders].map((s) => s.getAttribute("aria-label"));
    expect(new Set(names).size).toBe(names.length);
  });

  // The keep-tags card left Policy: tags are now a LIST, defined on Settings -> Lists and
  // protecting through an `on_list` keep rule. A stored draft can still carry the retired
  // gate, since the loader keeps an enabled row whose target list could not be created rather
  // than silently dropping cover. The editor renders that row as a plain protection from its
  // `gateMeta` copy instead of dropping it or crashing. An id this build has no copy for reads
  // "Another protection," never a raw slug.
  it("tolerates a stored draft still carrying the retired whitelisted gate", async () => {
    renderEditor({
      body: {
        ...body(),
        gates: [{ gate: "whitelisted", enabled: true, threshold: 0, window_days: 0 }],
      },
    });

    // Named in the operator's words, not by the `titleCase` fallback. The gate is retired as
    // a switch, but a stored body from before the upgrade still carries its id, so its copy
    // stays in `gateMeta` and the row reads as a sentence rather than as "Whitelisted".
    //
    // And it names what THIS gate protected: tags and the "Never Reap" collection, the lists
    // the operator curates by hand.
    expect(
      await screen.findByRole("switch", { name: "On a list you curate yourself" }),
    ).toBeChecked();
    // The card, its tag boxes and its own copy are gone with the feature.
    expect(screen.queryByText("Spare titles you've tagged")).not.toBeInTheDocument();
    expect(screen.queryByLabelText("Add a keep tag")).not.toBeInTheDocument();
  });

  // The notice on this row tells the operator to turn the leftover off, and turning it off
  // must produce a body the server accepts. The server refuses this id in EITHER switch
  // position (`GateSettingIn._must_be_authorable`), so storing `enabled: false` would leave
  // every validate, simulate and save call failing, while the page still says the switch is
  // the way out. Turning it off must remove the row from the body instead.
  it("takes a leftover protection out of the body when it is turned off", async () => {
    const user = userEvent.setup();
    renderEditor({
      body: {
        ...body(),
        gates: [{ gate: "whitelisted", enabled: true, threshold: 0, window_days: 0 }],
      },
    });

    const leftover = await screen.findByRole("switch", { name: "On a list you curate yourself" });
    expect(screen.getByText(/A leftover from an older version/)).toBeInTheDocument();

    await user.click(leftover);

    // The whole row goes, not just the switch: there is no such protection to show off.
    expect(
      screen.queryByRole("switch", { name: "On a list you curate yourself" }),
    ).not.toBeInTheDocument();
    // The control that was pressed is removed along with its row, so focus must move
    // somewhere or it falls to `<body>` and the next Tab restarts at the top of a form with
    // nearly 1,900 lines. It moves to Save, since removing the row is a draft edit, and
    // pressing Save is what makes it real.
    await waitFor(() => {
      expect(document.activeElement).toBe(screen.getByRole("button", { name: "Save changes" }));
    });
    // ...and the draft the page is now working against is one the server accepts. Read off
    // the validate call because that is the same body the Save button posts.
    await waitFor(() => {
      expect(apiMock.validatePolicy.mock.calls.at(-1)?.[0].gates).toEqual([]);
    });
  });

  it("gives two lean keep rules on one field Remove buttons that answer to different names", async () => {
    // Two lean rules on one field is a supported setup: `addLean` runs the name through
    // `uniqueName` precisely because names can collide, and the rows differ on screen. If the
    // Remove buttons were named by field alone, both would announce the same words, and
    // removing the wrong one drops a keep rule, so the next scan could condemn titles the
    // operator believed were protected.
    const lean = (saturate_at: number, name: string) => ({
      name,
      field: "imdb_rating",
      max_discount: 10,
      floor: 0,
      saturate_at,
      direction: "high_keeps" as const,
    });
    renderEditor({ body: { ...body(), graded_keeps: [lean(60, "first"), lean(90, "second")] } });

    const removes = await screen.findAllByRole("button", { name: /^Remove rule: leans/ });
    expect(removes).toHaveLength(2);
    expect(new Set(removes.map((b) => b.getAttribute("aria-label"))).size).toBe(2);
  });
});

describe("why an 'Add rule' will not act", () => {
  // Each rule composer must say which empty box is blocking the "Add rule" button. Pressing
  // Add while a required box is empty must never fail silently. The condemn composer's other
  // refusal, a backwards ramp, already has a sentence beside its boxes, so this covers the arm
  // that did not.
  //
  // Each sentence binds to the empty BOX rather than to the button, because a `disabled`
  // button is out of the Tab order and a description on it is unreachable by the operator it
  // is meant for.
  const TEXT_FIELD = {
    key: "genre",
    type: "text",
    ops: ["eq", "contains"],
  };
  const NUMBER_FIELD = {
    key: "quality_score",
    type: "int",
    ops: ["gte", "lte"],
  };

  /** Both lanes given a real vocabulary, which `renderEditor` otherwise seeds empty. Deliberately
   *  fields that no built-in covers and that carry no ramp phrase, so `isRamp` stays false and
   *  the arm under test is the one an empty value reaches. */
  function renderWithFields() {
    renderEditor({ body: body() });
    apiMock.vocabulary.mockImplementation((lane: string) =>
      Promise.resolve(
        lane === "condemn"
          ? { lane, fields: [TEXT_FIELD] }
          : { lane, fields: [TEXT_FIELD, NUMBER_FIELD] },
      ),
    );
    return userEvent.setup();
  }

  it("names the empty value box in the remove composer, and lets go once it is filled", async () => {
    const user = renderWithFields();
    const field = (await screen.findAllByLabelText("Field"))[0]!;
    // Wait for the OPTION to appear, not just for the control to be enabled. This select is
    // enabled before the vocabulary read fills it (it holds only its "when…" placeholder until
    // then), so `toBeEnabled` passes one turn early and `selectOptions` throws "Value \"genre\"
    // not found in options."
    await within(field).findByRole("option", { name: "Genre" });
    await user.selectOptions(field, "genre");

    const add = await screen.findByRole("button", { name: "Add rule" });
    expect(add).toBeDisabled();
    const box = screen.getByLabelText("Genre value");
    expect(box).toHaveAccessibleDescription("Enter a value to add this rule.");

    await user.type(box, "comedy");

    expect(add).toBeEnabled();
    expect(box).toHaveAccessibleDescription("");
  });

  it("names it in the keep-outright composer too", async () => {
    // The same guard as the composer above, on a different card: reached through the keep
    // card's own Field picker, the second one on the page.
    const user = renderWithFields();
    const fields = await screen.findAllByLabelText("Field");
    const keepField = fields[fields.length - 1]!;
    // The same one-turn-early wait as the composer above.
    await within(keepField).findByRole("option", { name: "Genre" });
    await user.selectOptions(keepField, "genre");

    const box = screen.getByLabelText("Genre value");
    expect(box).toHaveAccessibleDescription("Enter a value to add this rule.");
    const adds = screen.getAllByRole("button", { name: "Add rule" });
    expect(adds[adds.length - 1]!).toBeDisabled();

    await user.type(box, "documentary");

    expect(box).toHaveAccessibleDescription("");
    expect(screen.getAllByRole("button", { name: "Add rule" }).at(-1)!).toBeEnabled();
  });

  it("names the empty 'full effect at' box in the lean composer", async () => {
    // The third composer, and the one whose box is a number rather than a text field, so the
    // branch carrying the description is a different element. A lean has no yes/no arm: it
    // always ramps to a number, which is why its sentence asks for one.
    const user = renderWithFields();
    await waitFor(async () =>
      expect((await screen.findAllByLabelText("Field")).length).toBeGreaterThan(1),
    );
    await user.click(screen.getByRole("button", { name: "Leans toward keeping" }));

    const fields = await screen.findAllByLabelText("Field");
    const leanField = fields[fields.length - 1]!;
    await user.selectOptions(leanField, "quality_score");

    const box = screen.getByLabelText("full effect at");
    expect(box).toHaveAccessibleDescription("Enter a number to add this rule.");
    expect(screen.getAllByRole("button", { name: "Add rule" }).at(-1)!).toBeDisabled();

    await user.type(box, "70");

    expect(box).toHaveAccessibleDescription("");
    expect(screen.getAllByRole("button", { name: "Add rule" }).at(-1)!).toBeEnabled();
  });
});

// "Up to 10 points" cannot be read without knowing what earns them: ten points on a library
// of well-rated titles is ten points that can never be earned. This strip shows the range
// visually instead of leaving it only in the stored body. How many boxes a signal gets is
// arithmetic, not a design choice: see `signalRamp`'s two shapes.
/** The strip drawn under one named signal.
 *
 *  Scoped to its own row on purpose: every signal draws one, so an unscoped `querySelector`
 *  would silently answer for whichever strip sits highest on the page instead of the one
 *  this test means to check.
 */
function stripFor(signalName: string): { fill: HTMLElement; bar: HTMLElement } {
  const row = screen.getByText(signalName).closest(".rule-row");
  expect(row).not.toBeNull();
  return {
    fill: (row as HTMLElement).querySelector(".ramp-strip-fill") as HTMLElement,
    bar: (row as HTMLElement).querySelector(".ramp-strip-bar") as HTMLElement,
  };
}

describe("where a signal starts earning", () => {
  it("gives a shortfall signal one box, because the second would do nothing", async () => {
    renderEditor({ body: body() });

    // low_rating measures how far BELOW its bound a rating sits, and the engine's fraction
    // works out to depend on the gap alone: (0,70), (10,80) and (30,100) score identically.
    // A "full points at" box here would be a control an operator could move for no effect.
    expect(
      await screen.findByLabelText('Where "How low it\'s rated" stops adding points'),
    ).toBeVisible();
    expect(screen.queryByLabelText('Where "How low it\'s rated" adds all its points')).toBeNull();
    // The strip charges everything BELOW the bar, so its fill starts at the reading edge and
    // stops where the bar sits (7.0 of 10). Read from the logical CSS property, which is what
    // the component sets so the strip mirrors along with the rest of the page; `style.left` is
    // empty and would assert nothing.
    const { fill } = stripFor("How low it's rated");
    expect(fill.style.insetInlineStart).toBe("0%");
    expect(fill.style.width).toBe("70%");
    // The label says what a title CLEARS, which is the whole of the backwards reading: a
    // higher number now visibly demands more rather than sounding more generous.
    expect(screen.getByText("Good enough to leave alone")).toBeVisible();
  });

  it("gives a direct signal both ends, because the engine honors both", async () => {
    renderEditor({ body: body() });

    expect(
      await screen.findByLabelText('Where "How long it\'s gone unwatched" starts adding points'),
    ).toBeVisible();
    expect(
      screen.getByLabelText('Where "How long it\'s gone unwatched" adds all its points'),
    ).toBeVisible();
  });

  it("colors a direct ramp deepest at the bound it adds all its points at", async () => {
    renderEditor({ body: body() });
    await screen.findByLabelText('Where "How long it\'s gone unwatched" adds all its points');

    // This fixture adds in full at 365 days on a 3650-day track, so the flat top starts one
    // tenth along and the fill still runs to the end: past the far bound the signal keeps
    // adding all of it. Full color must sit at that 10% mark, the bound itself, not stretched
    // across the whole track to the fill's far edge.
    const { fill } = stripFor("How long it's gone unwatched");
    expect(fill.style.background).toBe(
      "linear-gradient(to right, color-mix(in srgb, var(--condemn) 6%, transparent), var(--condemn) 10%)",
    );
  });

  it("writes a shortfall edit back as the gap, floor and all", async () => {
    const user = userEvent.setup();
    renderEditor({ body: body() });

    const box = await screen.findByLabelText('Where "How low it\'s rated" stops adding points');
    await user.clear(box);
    await user.type(box, "5.5");

    // Stored in tenths, and the floor resets to zero with it: the pair carries one degree of
    // freedom, so a stale floor would leave a second number in the body that nothing reads and
    // nobody can see. The strip shows the edit landed: the bar moves to 5.5 of 10.
    await waitFor(() =>
      expect(stripFor("How low it's rated").bar.style.insetInlineStart).toBe("55%"),
    );
  });

  it("says nothing about a range for a signal worth no points", async () => {
    const off = body();
    off.signals = off.signals.map((s) =>
      s.signal === "low_rating"
        ? { ...s, weight: 0 }
        : s.signal === "unwatched"
          ? { ...s, weight: s.weight + 10 }
          : s,
    );
    renderEditor({ body: off });

    await screen.findByText("How low it's rated");
    expect(screen.queryByLabelText('Where "How low it\'s rated" stops adding points')).toBeNull();
  });
});

// The probe is a round trip for a number a slider could have computed locally, and that is
// the point: a local copy of the ramp beside the control that tunes deletions is a second
// scorer, free to drift from the one that decides. So what matters here is that the sentence
// only ever shows what the server said, and says so plainly when it has not said it yet.
describe("trying a value against a signal's range", () => {
  it("shows what the engine answered, not a number worked out here", async () => {
    apiMock.probePolicy.mockResolvedValue({ points: 3.5 });
    renderEditor({ body: body() });

    await screen.findByText("How low it's rated");

    // The number is the server's, and it is deliberately NOT what this ramp would produce
    // locally: a component doing its own arithmetic would overwrite it, and this is what
    // would catch that. Read off the bolded element rather than the sentence, which is split
    // across nodes precisely so the two numbers can be picked out of it.
    //
    // Scoped to the row, not the page. One mock answers every signal's probe, so all three
    // rows show 3.5, and an unscoped query racing against which requests have settled would
    // pass or fail depending on timing rather than on what it names.
    const row = screen.getByText("How low it's rated").closest(".rule-row") as HTMLElement;
    await waitFor(() => expect(within(row).getByText("3.5")).toBeVisible());
    expect(within(row).getByText(/of these/)).toBeVisible();
    expect(apiMock.probePolicy).toHaveBeenCalledWith(
      expect.objectContaining({ kind: "signal", signal: "low_rating", weight: 10 }),
    );
  });

  it("says it is still working rather than showing a stale answer", async () => {
    // Never resolves: the state between a drag and an answer is a real state, and the honest
    // thing on screen is that nothing has come back yet.
    apiMock.probePolicy.mockReturnValue(new Promise(() => {}));
    renderEditor({ body: body() });

    expect((await screen.findAllByText(/Working out what/)).length).toBeGreaterThan(0);
  });

  it("owns up when the read fails instead of going quiet", async () => {
    // Silence here would read as "this rule earns nothing," a claim about the operator's
    // policy that a failed request never actually made.
    apiMock.probePolicy.mockRejectedValue(new Error("nope"));
    renderEditor({ body: body() });

    await waitFor(() =>
      expect(screen.getAllByText(/couldn't work that one out/).length).toBeGreaterThan(0),
    );
    // And it says the setting itself is fine, because a failed preview is not a failed save.
    expect(
      screen.getAllByText(/Your setting is fine. Only the preview failed/).length,
    ).toBeGreaterThan(0);
  });
});

// "Full points at 5 years" is unreachable on a mirror that only reaches back one year, so this
// card says so. Deliberately NOT a warning: the shipped far end is five years, almost nobody's
// history is that deep, and a warning that fires for everyone teaches the page to be ignored.
describe("what the dormancy ramp can actually reach", () => {
  // The example must MOVE with the setting or it teaches nothing about the control under it.
  // An example pinned to the watch mirror's edge would freeze at "70 of these 70 points"
  // whenever the mirror reaches past the signal's far bound, whatever either box says. So the
  // edge is used only where it actually BINDS.
  it("describes a title the history caps, when the history is the shorter of the two", async () => {
    // 200 days of history against a far end of 365: nothing can present more than 200, so
    // that is both a moving example and the ceiling the mirror imposes.
    apiMock.probePolicy.mockResolvedValue({ points: 38.4 });
    renderEditor({ body: body(), history_reach_days: 200 });

    await waitFor(() =>
      expect(apiMock.probePolicy).toHaveBeenCalledWith(
        expect.objectContaining({ kind: "signal", signal: "unwatched", value: 200 }),
      ),
    );
    // 200 days reads as "6 months, 20 days": two units, the way the server words it too.
    expect(
      screen.getByText(/watch history goes back 6 months, 20 days, and nothing can show/),
    ).toBeVisible();
  });

  it("describes a title half way up instead, when the history reaches past the far end", async () => {
    // 400 days against the same 365 far end. The edge earns full points here, so an example
    // pinned to it would read 70 of 70 for every setting the operator could type.
    renderEditor({ body: body(), history_reach_days: 400 });

    await waitFor(() =>
      expect(apiMock.probePolicy).toHaveBeenCalledWith(
        expect.objectContaining({ signal: "unwatched", value: 183 }),
      ),
    );
  });

  it("says nothing about history where the history caps nothing", async () => {
    // A mirror deeper than the far end has no effect on this signal, so the sentence would be
    // true but useless. The card is long enough without one that never changes.
    renderEditor({ body: body(), history_reach_days: 400 });

    await screen.findByText("How long it's gone unwatched");
    expect(screen.queryByText(/watch history goes back/)).toBeNull();
  });

  it("says nothing about history for a signal the mirror does not bound", async () => {
    // A rating is a rating however short the history, so claiming the mirror bounds it would
    // be a fact about the wrong signal.
    renderEditor({ body: body(), history_reach_days: 200 });

    await screen.findByText(/watch history goes back/);
    expect(screen.getAllByText(/watch history goes back/)).toHaveLength(1);
  });

  it("keeps its nerve when the scan never recorded a reach", async () => {
    // Null is "we don't know", not "no history": the example falls back to the ramp's
    // midpoint and the page claims nothing about the operator's history.
    renderEditor({ body: body() });

    await screen.findByText("How long it's gone unwatched");
    expect(screen.queryByText(/watch history goes back/)).toBeNull();
  });
});

// Making the ramp editable also makes it losable: 1825 is the measured point where the
// rewatch curve flattens, not a number anyone would remember, and the presets restore weights
// only. This "set to default" control is the way back from a typo without knowing the answer.
describe("putting a ramp back the way Reaper ships it", () => {
  const shipped = [
    { signal: "unwatched", weight: 70, saturate_at: 1825, floor: 365 },
    { signal: "few_watchers", weight: 20, saturate_at: 3, floor: 0 },
    { signal: "low_rating", weight: 10, saturate_at: 80, floor: 0 },
  ];

  it("offers nothing while the bounds are still Reaper's", async () => {
    const body_ = body();
    body_.signals = shipped.map((s) => ({ ...s }));
    renderEditor({ body: body_, default_signals: shipped });

    await screen.findByText("How long it's gone unwatched");
    expect(screen.queryByText(/Set to default/)).toBeNull();
  });

  it("offers the way back once a bound has moved", async () => {
    renderEditor({ body: body(), default_signals: shipped });

    // body()'s unwatched is 0 -> 365, against a shipped 365 -> 1825.
    expect(
      await screen.findByLabelText("Set to default: How long it's gone unwatched"),
    ).toBeVisible();
  });

  it("restores both bounds and leaves the weight alone", async () => {
    const user = userEvent.setup();
    renderEditor({ body: body(), default_signals: shipped });

    await user.click(await screen.findByLabelText("Set to default: How long it's gone unwatched"));

    // Both ends back, and the strip is what shows it: the bar moves to 365 of a 3650 track.
    await waitFor(() =>
      expect(stripFor("How long it's gone unwatched").bar.style.insetInlineStart).toBe("10%"),
    );
    // The weight is untouched. Removal weights total exactly 100, so putting one back on its
    // own would break the budget the save bar enforces.
    expect(screen.getByText("How long it's gone unwatched").closest(".rule-row")).toHaveTextContent(
      "up to 70 points",
    );
    // And the offer goes from THIS row once there is nothing left to undo. Scoped, because
    // the fixture's rating ramp also differs from shipped and keeps its own offer.
    const row = screen.getByText("How long it's gone unwatched").closest(".rule-row");
    expect(within(row as HTMLElement).queryByText(/Set to default/)).toBeNull();
  });

  it("offers nothing when the server sent no defaults", async () => {
    // An older server, or a response that lost the field: no invented "default" to go back to.
    renderEditor({ body: body() });

    await screen.findByText("How long it's gone unwatched");
    expect(screen.queryByText(/Set to default/)).toBeNull();
  });
});

// The two number controls split on whether the unit can change, and the dormancy gate two
// controls up already offers days/weeks/months/years for the same quantity. A bound spelled
// "1825 days" beside a gate spelling the same span "5 years" would have the app disagreeing
// with itself about one unit.
describe("which number control a bound gets", () => {
  it("gives a day bound the unit picker, and draws it in the friendliest one", async () => {
    const body_ = body();
    body_.signals = body_.signals.map((s) =>
      s.signal === "unwatched" ? { ...s, floor: 365, saturate_at: 1825 } : s,
    );
    renderEditor({ body: body_ });

    const far = await screen.findByLabelText(
      'Where "How long it\'s gone unwatched" adds all its points',
    );
    // 1825 days is stored; "5 years" is drawn. The policy body never sees the unit.
    expect(far).toHaveValue(5);
    expect(within(far.closest(".qty") as HTMLElement).getByRole("combobox")).toHaveValue("years");
  });

  it("leaves a rating on the fixed suffix, which has no larger unit to offer", async () => {
    renderEditor({ body: body() });

    const box = await screen.findByLabelText('Where "How low it\'s rated" stops adding points');
    // A suffix, not a picker: there is no unit above IMDb to switch to.
    expect(within(box.closest(".qty") as HTMLElement).queryByRole("combobox")).toBeNull();
  });
});

describe("the panel's verdict on an edit it has not simulated yet", () => {
  // `keepPreviousData` keeps the last answer rendered across a refetch and reports
  // `status: "success"` throughout. So "is this an edit" (read off the new draft, synchronously
  // with the query key) and "what did that edit do" (the previous draft's numbers) can
  // describe two different bodies during one round trip. The untouched policy always answers
  // `changed_titles: 0`, so the FIRST edit of a session must not be read as "your changes do
  // nothing" before anything has actually scored it.
  const INERT = "Your changes leave every title as it is.";

  it("does not call an edit inert while the numbers on screen answer the previous draft", async () => {
    // `apiMock` is module-level, so its call counts carry across every test in this file.
    // Cleared here so the premise below counts from zero.
    apiMock.simulate.mockClear();
    renderEditor({ body: body() });
    await screen.findByRole("heading", { name: "Movies policy" });
    // The premise: the mount simulate settled, and it is the inert-shaped answer. Without this
    // the assertion below would pass on a panel that never rendered a comparison at all.
    await waitFor(() => expect(apiMock.simulate).toHaveBeenCalledTimes(1));
    await screen.findByText("Titles that change");

    // The next one never answers. That is the window, held open.
    apiMock.simulate.mockReturnValue(new Promise(() => {}));

    fireEvent.change(screen.getByLabelText("Put a title on the list once it scores"), {
      target: { value: "42" },
    });
    // Past the 250 ms debounce, so the draft the flag reads has moved and the request it
    // describes is in flight rather than queued.
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 400));
    });
    await waitFor(() => expect(apiMock.simulate).toHaveBeenCalledTimes(2));

    expect(screen.queryByText(INERT)).not.toBeInTheDocument();
  });

  it("says it once the answer describes the draft it is about", async () => {
    // The other direction, so the test above cannot pass by the sentence being unreachable.
    renderEditor({ body: body() });
    await screen.findByRole("heading", { name: "Movies policy" });
    await screen.findByText("Titles that change");

    fireEvent.change(screen.getByLabelText("Put a title on the list once it scores"), {
      target: { value: "42" },
    });
    expect(await screen.findByText(INERT)).toBeInTheDocument();
  });
});

describe("the section rail", () => {
  // Which section is open is `App` state, so the address bar can name it (`/policy/tv/deletion`,
  // navUrl.ts). This page's job is to report a click upward and draw the rail from what it is
  // handed back, not from a second copy of its own.
  //
  // Scrolling is measured from four rects and the document's height, and jsdom always answers 0
  // for both, so the three tests below STATE a geometry rather than read a real one. The numbers
  // reproduce a real shape seen in Chromium: a click on "Pace and limits" scrolls to the end of
  // the document, so the page bottoms out with all four headings on screen. That is the same
  // position scrolling down to read Deletion leaves you in, so no rule reading rects alone can
  // tell the two apart, and the click has to be remembered rather than re-measured.
  // `AppUrl.test.tsx` covers the shell's half.
  const VIEWPORT = 900;
  const PAGE = 3000;
  const BOTTOM = PAGE - VIEWPORT;
  /** Each heading's top, in the order they sit on the page: flags, kept, pace, deletion. */
  const LAST_SCREENFUL = [-2000, -1000, 0, 400];
  const MIDWAY = [-2000, 0, 400, 890];

  // `innerHeight` and `scrollY` outlive a render tree, so every stub is restored afterward.
  // All three are configurable accessors under jsdom, which is what lets a getter spy stand in.
  afterEach(() => vi.restoreAllMocks());

  function state(scrollY: number, tops: number[]) {
    vi.spyOn(window, "innerHeight", "get").mockReturnValue(VIEWPORT);
    vi.spyOn(window, "scrollY", "get").mockReturnValue(scrollY);
    vi.spyOn(document.documentElement, "scrollHeight", "get").mockReturnValue(PAGE);
    document.querySelectorAll("h3.policy-section").forEach((heading, i) => {
      vi.spyOn(heading, "getBoundingClientRect").mockReturnValue({ top: tops[i] } as DOMRect);
    });
  }

  /** One scroll event, and the frame the listener coalesces it into. */
  const scrolled = async () =>
    await act(async () => {
      window.dispatchEvent(new Event("scroll"));
      await new Promise((settle) => requestAnimationFrame(() => settle(null)));
    });

  const railTab = (name: string) =>
    within(document.querySelector(".policy-rail") as HTMLElement).getByRole("button", { name });

  it("keeps the section a click asked for, where the click bottoms the page out", async () => {
    const person = userEvent.setup();
    renderEditor({ body: body() });
    await screen.findByRole("heading", { name: "Movies policy" });
    state(BOTTOM, LAST_SCREENFUL);

    await person.click(railTab("Pace and limits"));
    await scrolled();

    expect(railTab("Pace and limits")).toHaveAttribute("aria-current", "page");
    expect(railTab("Deletion")).not.toHaveAttribute("aria-current");
  });

  it("hands the rail back to the scroll once the operator moves the page", async () => {
    const person = userEvent.setup();
    renderEditor({ body: body() });
    await screen.findByRole("heading", { name: "Movies policy" });
    state(BOTTOM, LAST_SCREENFUL);
    await person.click(railTab("Pace and limits"));
    await scrolled();

    state(500, MIDWAY);
    await scrolled();

    expect(railTab("What's always kept")).toHaveAttribute("aria-current", "page");
    expect(railTab("Pace and limits")).not.toHaveAttribute("aria-current");
  });

  it("still marks the last section for someone who scrolls to the end", async () => {
    renderEditor({ body: body() });
    await screen.findByRole("heading", { name: "Movies policy" });
    state(BOTTOM, LAST_SCREENFUL);

    await scrolled();

    expect(railTab("Deletion")).toHaveAttribute("aria-current", "page");
  });

  it("reports the section a click asks for, and marks what it is handed back", async () => {
    const person = userEvent.setup();
    renderEditor({ body: body() });
    await screen.findByRole("heading", { name: "Movies policy" });

    const rail = document.querySelector(".policy-rail");
    expect(rail, "the policy rail is not on the page").not.toBeNull();
    const deletion = within(rail as HTMLElement).getByRole("button", { name: "Deletion" });
    expect(deletion).not.toHaveAttribute("aria-current");

    await person.click(deletion);
    expect(deletion).toHaveAttribute("aria-current", "page");
    expect(
      within(rail as HTMLElement).getByRole("button", { name: "What flags a title" }),
    ).not.toHaveAttribute("aria-current");
  });
});

describe("the policy the URL names", () => {
  // The other half of where the operator is: which media type's policy is open. `App` owns
  // this now and hands it down, the same way it hands down the section, so a reload on a
  // policy link opens the right section under the right media type's caps, budget and weights.
  it("opens on the policy it is handed, with no click to get there", async () => {
    renderEditor({ body: tvBody() }, pace, null, [], "deletion", "tv");

    expect(await screen.findByRole("heading", { name: "TV policy" })).toBeInTheDocument();
    // The read that decides every number on the page. `api.policy` defaults to "movie", so a
    // call carrying "tv" proves the prop actually arrived, rather than a default silently
    // matching.
    expect(apiMock.policy).toHaveBeenCalledWith("tv");
    // ...and the controls only the TV policy draws.
    expect(
      await screen.findByRole("heading", { name: "TV season protection" }),
    ).toBeInTheDocument();
  });

  it("holds the switch until the operator says the unsaved edits can go", async () => {
    // The confirm stays in this component while the value it commits lives in `App`, because
    // `dirty` and the draft it is about are both here. So the press must not reach the owner
    // ahead of the answer: the address bar would then name a policy that is not on screen, over
    // edits that are. Switching discards the draft, which is what the confirm says out loud and
    // what it is for.
    const person = userEvent.setup();
    renderEditor({ body: body() });
    await screen.findByRole("heading", { name: "Movies policy" });
    // The other policy, for the fetch the switch starts. Set after the mount fetch has gone, as
    // `renderTvEditor` sets the season shape.
    apiMock.policy.mockResolvedValue({
      policy_hash: "hash",
      name: "default",
      warnings: [],
      body: tvBody(),
    });

    fireEvent.change(screen.getByLabelText("Put a title on the list once it scores"), {
      target: { value: "42" },
    });
    await waitFor(() => expect(document.querySelector(".savebar")).not.toBeNull());

    await person.click(screen.getByRole("button", { name: "TV" }));
    expect(document.querySelector(".notice-warn")!.textContent).toContain(
      "You have unsaved movie policy changes. Switching to TV discards them.",
    );
    expect(screen.getByRole("heading", { name: "Movies policy" })).toBeInTheDocument();

    await person.click(screen.getByRole("button", { name: "Discard and switch" }));
    expect(await screen.findByRole("heading", { name: "TV policy" })).toBeInTheDocument();
    // The edits went with it: the draft re-seeded from the TV policy, so there is nothing left
    // to save and no bar offering to.
    await waitFor(() => expect(document.querySelector(".savebar")).toBeNull());
  });
});

describe("the delete threshold's consequence sentence", () => {
  const SCORE_NAME = "Put a title on the list once it scores";

  it("pins the sentence at a measured position", async () => {
    renderEditor(
      { body: body() }, // condemn_at 70
      pace,
      null,
      [],
      "flags",
      "movie",
      { blocks: [], total_items: 0 },
      { state: "measured", rows: [{ score: 70, flagged: 20, expected_mistakes: 2 }] },
    );
    await screen.findByRole("heading", { name: "Movies policy" });
    expect(
      await screen.findByText(
        "20 titles would be Condemned. About 2 of them may get watched again within a year if you kept them.",
      ),
    ).toBeInTheDocument();
  });

  it("uses the singular at exactly one flagged title", async () => {
    renderEditor(
      { body: body() },
      pace,
      null,
      [],
      "flags",
      "movie",
      { blocks: [], total_items: 0 },
      { state: "measured", rows: [{ score: 70, flagged: 1, expected_mistakes: 1 }] },
    );
    await screen.findByRole("heading", { name: "Movies policy" });
    expect(
      await screen.findByText(
        "1 title would be Condemned. About 1 of them may get watched again within a year if you kept them.",
      ),
    ).toBeInTheDocument();
  });

  it("shows the zero-flagged sentence when nothing on the last scan scores this high", async () => {
    // The curve's only row sits at 40, well under the draft's 70, so nothing here would ever
    // put a title in front of the operator at a threshold of 70.
    renderEditor(
      { body: body() },
      pace,
      null,
      [],
      "flags",
      "movie",
      { blocks: [], total_items: 0 },
      { state: "measured", rows: [{ score: 40, flagged: 5, expected_mistakes: 1 }] },
    );
    await screen.findByRole("heading", { name: "Movies policy" });
    expect(
      await screen.findByText(
        "0 titles would be Condemned. Nothing on the last scan scores this high.",
      ),
    ).toBeInTheDocument();
  });

  it("renders only the count when the scan has no trusted rewatch cohort anywhere", async () => {
    renderEditor(
      { body: body() },
      pace,
      null,
      [],
      "flags",
      "movie",
      { blocks: [], total_items: 0 },
      { state: "counts_only", rows: [{ score: 70, flagged: 12 }] },
    );
    await screen.findByRole("heading", { name: "Movies policy" });
    expect(await screen.findByText("12 titles would be Condemned.")).toBeInTheDocument();
    // Never a made-up comeback estimate with no cohort behind it.
    expect(screen.queryByText(/may get watched again/)).not.toBeInTheDocument();
  });

  // This is a readout, not a setting: the score slider works exactly the same whether or not
  // this sentence renders, whatever the curve read answers.
  const NOTHING_STATES: ReadonlyArray<[string, { state: "no_scan" } | Error | "pending"]> = [
    ["no scan yet", { state: "no_scan" }],
    ["a failed read", new Error("threshold-curve unreachable")],
    ["a still-loading read", "pending"],
  ];
  it.each(NOTHING_STATES)(
    "renders nothing for %s, and the score slider stays enabled",
    async (_label, curve) => {
      renderEditor(
        { body: body() },
        pace,
        null,
        [],
        "flags",
        "movie",
        { blocks: [], total_items: 0 },
        curve,
      );
      await screen.findByRole("heading", { name: "Movies policy" });
      expect(screen.getByLabelText(SCORE_NAME)).toBeEnabled();
      expect(screen.queryByText(/would be Condemned/)).not.toBeInTheDocument();
    },
  );
});
