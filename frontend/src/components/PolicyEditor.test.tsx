// SPDX-License-Identifier: AGPL-3.0-or-later
// The policy page's two dead ends, and its control grammar.
//
// Both dead ends were states the operator could not get out of from the page that exists
// to fix them: a policy that could not be read showed no way to replace it, and a preset
// click left the removal lane over budget with Save disabled. Each test here fails if
// either fix is reverted.
//
// The warning-anchor walk (rule 42) is `PolicyEditor.warnings.test.tsx`, split out because vitest
// runs a file serially and the walk alone was more than half of this file's time. The page boot
// both files share is `src/test/policyEditor.tsx`.
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

// The probe under each signal's range fires on a 250ms debounce, so most tests here finish
// before it runs. Seeded anyway: rule 135's gate only catches a queryFn that actually ran, so
// an unmocked one would sit silent until the first test that waits long enough. Seeded HERE
// rather than inside `renderEditor`, which runs after a test's own mock and would overwrite it.
beforeEach(() => {
  apiMock.probePolicy.mockResolvedValue({ points: 0.8 });
});

describe("a policy the server had to repair", () => {
  // The invariant, driven for EVERY repair the app knows and one it does not: a repaired
  // policy always offers the Save that replaces it. That is the whole exit from a degraded
  // scan -- the incomplete-scan notice says "open the policy page, check X, and save" and
  // nothing else clears it -- so a repair the editor stays clean on strands the operator
  // with a permanent banner and no control that answers it (#516).
  //
  // Keys, not a hand-written list, so a repair added to REPAIR_NOTICES is driven the day it
  // lands. The count is pinned because a walk cannot tell a member that complies from one
  // that dropped out of it (rule 145); `tests/test_policy_repairs.py` reconciles this same
  // number against `PolicyRepair` on the server, which is the declaration both sides mirror.
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

    // The lists conversion is the repair that shipped with no copy at all, so it is the one
    // pinned by its sentence rather than only by the savebar above.
    expect(await screen.findByText(/Your protected lists moved to Settings/)).toBeInTheDocument();
    // And it does NOT borrow the rescale's sentence, which is what the degradation used to
    // tell the operator to go and check.
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
  // The rules that condemn files are written here, on the longest form in the app: thresholds,
  // weights, and protections, many of them a number beside a switch. A control that reads out as
  // its neighbor is how an operator sets a threshold they never meant to set.
  it("has no accessibility violations", async () => {
    const { container } = renderEditor({ body: body() });
    await screen.findByRole("heading", { name: "Movies policy" });
    // The page is stitched from seven reads, and the rule editors and the deletion switch settle
    // without changing anything a query can wait on. axe reads the DOM directly, so it has to be
    // the settled one (rule 136).
    await act(async () => {});
    await expectNoA11yViolations(container);
  });

  it("says so on the load it happened, with nothing else dirty", async () => {
    // `fell_back` arrives alone: nothing of the stored body survived for a second repair to
    // be about (services/profiles.py). The notice used to live inside the savebar, which
    // only renders when something is dirty, so it was invisible in exactly the state it
    // explains.
    const { container } = renderEditor({ body: body(), repairs: ["fell_back"] });

    expect(await screen.findByText(/Your saved policy couldn't be read/)).toBeInTheDocument();
    // And the way out is offered: the savebar renders, so the fallback can be replaced.
    const savebar = container.querySelector(".savebar");
    expect(savebar).not.toBeNull();
    expect(screen.getByRole("button", { name: "Save changes" })).toBeEnabled();
    // The notice itself is not inside that savebar: it hangs off the response flag alone,
    // so no dirty gate can hide it.
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
    // Applying rescales the whole removal lane, so the built-ins end up as the shipped mix
    // times a factor -- and the old exact-equality badge read that as "Custom" on the very
    // click that applied the preset, while the line below said "Staged, not saved" (U-8).
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
    // The badge only stops being honest if it claims a preset the draft is not. A draft
    // whose built-ins no longer carry the mix's shape is Custom, with or without rules of
    // the operator's own.
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

    // Applying a preset re-enables caps (B-10): the warning clears, so the profile it would
    // save is capped, not uncapped.
    await waitFor(() => expect(screen.queryByText(/No cap on run size/)).not.toBeInTheDocument());
  });
});

describe("the caps switch and the copy that reads it", () => {
  it("the intent band drops the per-run limit claim when caps are off", async () => {
    renderEditor({ body: body() }, { ...pace, caps_enabled: false });

    // With caps off the executor skips the per-run checks, so the summary must not assert a
    // hard bound (B-2); it says the limit is gone until turned back on.
    expect(
      await screen.findByText(/no per-run limit until you turn limits back on/),
    ).toBeInTheDocument();
    expect(screen.queryByText(/removes at most/)).not.toBeInTheDocument();
  });

  it("shows a recovery notice when the stored settings couldn't be read", async () => {
    const { container } = renderEditor({ body: body() }, { ...pace, settings_recovered: true });

    // The shipped defaults can be looser than what was saved, so the Pace page says so
    // rather than silently swapping them (PR-1).
    expect(
      await screen.findByText(/Your saved caps and grace couldn't be read/),
    ).toBeInTheDocument();

    // And the notice's own instruction ("a scan won't remove anything until you check these
    // and save") is followable: the savebar renders with a live Save. The pace half had no
    // equivalent of the policy half's forced-dirty, so this told the operator to save while
    // offering nothing to press, and the only escape was to change a value on purpose (B-6).
    const savebar = container.querySelector(".savebar");
    expect(savebar).not.toBeNull();
    expect(screen.getByRole("button", { name: "Save changes" })).toBeEnabled();
    expect(savebar?.textContent ?? "").toContain("pace and limits");
  });

  it("claims nothing about caps when the profile couldn't be read at all", async () => {
    // B-29, rule 53: the fallback wording ("removes only within your caps") also covered the
    // FAILED read, so the one sentence an operator scans before arming asserted caps were in
    // force directly above a section saying the settings behind them could not be loaded.
    renderEditor({ body: body() }, new Error("profile unreadable"));

    const line = (await screen.findByText(/Right now Reaper flags a movie/)).textContent ?? "";
    expect(line).not.toContain("caps");
    expect(line).not.toContain("removes");
    // Still a sentence, just one that stops at what it can vouch for.
    expect(line).toContain("70 / 100");
    // And the contradiction it used to sit above is on screen at the same time.
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
    // B-26, rule 42: the warning renders directly beneath this box, but the server computed
    // it from the SAVED profile. Drag it from 0 to 5 and no warning appeared until after a
    // save; drag it back down and the old warning kept naming the old number. Every other
    // warning on this page describes the draft, so this one was the odd one out.
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
    // PR-7: one blocked half held the other hostage. Pace and limits are un-hashed and have
    // nothing to do with the removal budget -- this file's own header says tightening a cap
    // never voids an approval -- yet a grace edit could not be saved until an unrelated
    // weight was fixed. One save affordance still (rule 43), gated per half.
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
    // PR-3: both editors took only `data` off the query, so a failed fetch rendered an empty
    // field dropdown, no error and no retry. "Reasons to remove" then looked like a feature
    // with nothing to configure, and the operator concluded there were no fields rather than
    // that the fetch had failed.
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

/** The editor opens on Movies, so a TV assertion has to walk the switch the operator
 *  walks. The draft is clean on load, so the switch does not stop for a confirm.
 *
 *  `shape` is applied after renderEditor (which seeds an empty one) and before the switch,
 *  which is when the season card -- and so the advisory's query -- first mounts. */
describe("the TV one-line summary", () => {
  it("names each season protection only while its own switch is on", async () => {
    await renderTvEditor();

    const line = (await screen.findByText(/Right now Reaper flags a/)).textContent ?? "";
    expect(line).toContain(
      "always keeps the newest 2 seasons of a show, a show's first season, and anyone's mid-binge",
    );
  });

  it("claims nothing when every season protection is off", async () => {
    // The line an operator scans before arming used to assert two of these flat, so a
    // policy with the floor at 0 and mid-binge holding OFF still read "always keeps the
    // newest 0 seasons of a show and anyone's mid-binge" -- both false (U-3).
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
    // The endpoint counts every show in the snapshot, while the floor under this scope
    // only reaches shows someone asked for (plus the ones Reaper can't tell about). The
    // exact set needs the live request index, so the figure is stated as the bound it is
    // rather than printed as though the scope were off (U-7).
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
    // `apiMock` is module-level, so its call counts carry across this file. Counted from zero
    // here, or the premise below reads whatever the preceding tests happened to leave.
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
    expect(screen.getByLabelText("Watched again by anyone at least")).toBeInTheDocument();
    // Same grammar as movies (#554): a TV body carries its own rewatch_odds gate row now,
    // so the hold half renders and fetches the TV lane's own fit.
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

    expect(screen.getByLabelText("Watched by anyone at least")).toBeInTheDocument();
    expect(screen.getByLabelText("Most recently within")).toBeInTheDocument();
    expect(screen.getByLabelText("Lowers the score by")).toBeInTheDocument();

    await user.click(toggle);

    expect(screen.queryByLabelText("Watched by anyone at least")).not.toBeInTheDocument();
    expect(screen.queryByLabelText("Most recently within")).not.toBeInTheDocument();
    expect(screen.queryByLabelText("Lowers the score by")).not.toBeInTheDocument();
  });

  it("writes each control's edit into the draft, read off the same body the Save button posts", async () => {
    // `body()` seeds 15/8/365 -- off the server defaults (rule 141) -- so a box merely
    // echoing the default back could not be told apart from one wired to nothing. Each
    // assertion below picks a THIRD value, different again, and reads it back through
    // `validatePolicy`'s own argument rather than assuming the write landed.
    const user = userEvent.setup();
    renderEditor({ body: body() });
    await screen.findByRole("switch", { name: SWITCH_NAME });

    const viewings = screen.getByLabelText("Watched by anyone at least");
    await user.clear(viewings);
    await user.type(viewings, "42");
    await waitFor(() =>
      expect(apiMock.validatePolicy.mock.calls.at(-1)?.[0].rewatch_min_viewings).toBe(42),
    );

    const discount = screen.getByLabelText("Lowers the score by");
    await user.clear(discount);
    await user.type(discount, "33");
    await waitFor(() =>
      expect(apiMock.validatePolicy.mock.calls.at(-1)?.[0].rewatch_keep_discount).toBe(33),
    );

    // The recency box is a unit picker seeded at 365 days, which draws as "1 year" (the
    // friendliest unit `bestUnit` finds) -- so typing "9" here writes 9 years, not 9 days.
    const recent = screen.getByLabelText("Most recently within");
    await user.clear(recent);
    await user.type(recent, "9");
    await waitFor(() =>
      expect(apiMock.validatePolicy.mock.calls.at(-1)?.[0].rewatch_recent_days).toBe(9 * 365),
    );
  });
});

describe("the rewatch-odds hold, the grouped card's second half (#554 stage 2)", () => {
  const HOLD_SWITCH_NAME = "Keep titles most likely to be rewatched above a percentage";
  const PERCENT_LABEL = "Kept when the chance is at least";

  // Off the server default (25, rule 141): a fixture pinning 25 could not prove an edit
  // reaches the draft.
  function holdGate(over: Partial<PolicyBody["gates"][number]> = {}) {
    return { gate: "rewatch_odds", enabled: true, threshold: 30, window_days: 0, ...over };
  }

  // Three rungs, monotone decreasing, chosen so the cleared set actually changes between
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
    // No reload advice (#195): the savebar elsewhere on this page can be holding unsaved edits.
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
    // The simulator's fallback is one shared string, and there it is right: an id appears
    // once in a tally, where "Another protection, 7" reads correctly. A switch is the other
    // case. Two unknown ids sharing that string would draw two controls with one name and no
    // help, and turning a protection off is not a choice anyone can make blind.
    //
    // This is the assertion that fails on reverting to the shared constant (rule 118): the
    // labels have to differ from EACH OTHER, so a per-id fallback is the only thing that
    // satisfies it. Reachable only from a stale bundle -- the SPA ships inside the server's
    // own image -- which is why the bar is "distinguishable", not rule 21's nicer sentence.
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
    // Rule 40: a number with a changeable unit is `QuantityInput`, never a bare number box
    // beside loose unit text. Both knobs are durations, so both take it and neither is new
    // control code.
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
    // The hold is exactly this long, not a minimum of it. "at least 1.5 years" is the wrong
    // sentence and it is the one every other days-unit row wants.
    renderEditor({ body: returnedBody });

    await screen.findByLabelText("Keep a title that came back threshold");
    expect(screen.getByText("keep it for")).toBeInTheDocument();
    expect(screen.queryByText("at least")).not.toBeInTheDocument();
  });

  it("binds the absence help to the control it explains, not to the row", async () => {
    // Rule 45: help text binds to exactly one control, directly beneath it. The row's own
    // help is about the hold; the absence needs its own or the operator reads one paragraph
    // covering two numbers.
    renderEditor({ body: returnedBody });

    await screen.findByLabelText("How long an absence counts");
    expect(screen.getByText(/left your library and was fetched again/)).toBeInTheDocument();
    expect(
      screen.getByText(/How long a title has to be missing before its return counts/),
    ).toBeVisible();
  });

  it("hides both durations while the protection is off", async () => {
    // Rule 41: a settings-bearing group's sub-controls render only while its toggle is on,
    // hidden rather than disabled.
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
    // The server warns on gates.server_popularity.window_days and advises a year; until
    // this control existed the advice named a value with no control on the page (U-9).
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
    // The warning gives an operator whose reap list is empty somewhere to read why, so "the
    // server emits it" is not the claim worth pinning -- "it reaches the page, beside the
    // control it tells them to change" is.
    //
    // Both halves are needed, and the second is the one that costs an assertion. An
    // unanchored warning still renders, in the catch-all stack at the foot of the page, so
    // a bare findByText passes with the `gates.` anchor renamed to anything at all -- which
    // is what it did until this test asserted a POSITION (rule 118).
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
    // The twin of the test above, for the case where the control the other one points at
    // is not on the page. With `server_popularity` off, `PolicyBody.popularity_window_days`
    // still hands 365 to the operator's own keep-outright rules, so they block library-wide
    // on a window whose picker this editor deliberately hides -- and the rule itself is the
    // one thing they can act on.
    //
    // The DECOY is what makes the position assertion mean anything, and without it this test
    // passed with both halves of the anchor reverted (rule 118). The catch-all stack sits a
    // few lines below the keep card's own block, and `invalidMessage`/`validatorDown` render
    // nothing here, so with one warning on the page the two slots are the same DOM position
    // and the assertion cannot tell an anchored warning from an unclaimed one. A second
    // warning no anchor claims fills the catch-all, and it goes FIRST because
    // `unanchoredWarnings` preserves payload order: drop the anchor and the keep card's next
    // sibling becomes the decoy instead.
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
          // The real id and params `policy_warnings.inspect`'s gate-off arm emits (rule 144):
          // the catalog composes the sentence, so a drifted catalog entry fails the anchor
          // test on its own rather than reading as the shipped copy silently.
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
// The reconciliation between an anchor and its renderer, walked over the EXPORTED list the
// editor renders from (`WARNING_ANCHORS`). Claiming a field is what keeps it out of the
// bottom catch-all stack, so an anchor with no block behind it renders its warning nowhere
// at all -- which is what took the unknown-size warning off the page (#145, rule 42).
//
// The walk used to be a hand-written copy of the anchor list living in this file, so the one
// thing it existed to catch, a new anchor with no renderer, was invisible to it: appending
// one and running this suite stayed green (rule 103). Reading the declaration itself is what
// makes these cases a proof -- an anchor with no `warningsAt` call site has no block to paint
// its probe, and claiming has already taken that probe out of the catch-all, so the field
// appears nowhere and the case below fails.
describe("the controls a screen reader has to tell apart", () => {
  // The two thresholds sit in a `<label className="field">` that wraps the label span, the live
  // value AND the `.help` paragraph, so every one of those became the control's accessible name:
  // dragging the condemn slider announced the whole "The higher you set this..." sentence on each
  // step, and the operator never heard which of the two sliders they were on. An exact-match
  // `getByLabelText` is what pins it -- before the fix the name was the label plus the help, so
  // the short string matched nothing.
  //
  // Rule 145: this walks a population, so it counts. Asserting "every slider I collected has a
  // name" reads green when the walk collects nothing. The count below is every `range` this
  // fixture renders -- the two thresholds plus one weight per built-in signal in `body()`
  // (3) -- reconciled by hand against the source. Its honest limit: a slider added to a
  // section this fixture does not mount is missing from both the table and the count, and
  // the two absences hide each other.
  //
  // It went 5 -> 8 -> 5 across this branch: a probe slider per signal, then none. The guard
  // caught the arrival, and the reason they left is the same thing it is watching for -- a
  // second range control under a weight reads as another setting, and the operator cannot
  // tell which track changes their policy.
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

  // The keep-tags card left Policy: tags are a LIST now, defined on Settings -> Lists and
  // protecting through an `on_list` keep rule. A stored draft can still carry the retired
  // gate, though -- the loader keeps an enabled row whose target list could not be created
  // rather than silently withdrawing cover -- so the editor renders it as a plain protection
  // row from its `GATE_META` copy instead of dropping it or crashing. Rule 66's fallback
  // beneath that no longer title-cases the id (#551): an id this build has no copy for reads
  // "Another protection", never a slug.
  it("tolerates a stored draft still carrying the retired whitelisted gate", async () => {
    renderEditor({
      body: {
        ...body(),
        gates: [{ gate: "whitelisted", enabled: true, threshold: 0, window_days: 0 }],
      },
    });

    // Named in the operator's words, not by the `titleCase` fallback. The gate is retired as a
    // switch, but a stored body from before the upgrade still carries its id, so its copy stays
    // in `GATE_META` and the row reads as a sentence rather than as "Whitelisted".
    //
    // And it says what THIS gate meant: tags and the "Never Reap" collection, the lists the
    // operator curates by hand. The two retired labels were taken from `engine/fields.py` in
    // source order and landed on the wrong ids, so this one wore the other's words while
    // `curated_list` -- the IMDb Top 250 -- read "On a list you curate yourself".
    expect(
      await screen.findByRole("switch", { name: "On a list you curate yourself" }),
    ).toBeChecked();
    // The card, its tag boxes and its own copy are gone with the feature.
    expect(screen.queryByText("Spare titles you've tagged")).not.toBeInTheDocument();
    expect(screen.queryByLabelText("Add a keep tag")).not.toBeInTheDocument();
  });

  // The other half of #627. The notice on that row tells the operator to turn the leftover
  // off, and turning it off has to produce a body a save accepts -- the boundary refuses the
  // id in EITHER switch position (`GateSettingIn._must_be_authorable`), so storing
  // `enabled: false` would leave every validate, simulate and save 422-ing with the page
  // still saying the switch is the way out. Off means the row leaves the body.
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
    // The control that was pressed went with the row, so focus has to be put somewhere or it
    // falls to `<body>` and the next Tab restarts at the top of a ~1,900-line form (#173).
    // Save, because the removal is a draft edit and pressing it is what makes it real.
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
    // Two lean rules on one field is a supported setup -- `addLean` runs the name through
    // `uniqueName` precisely because they collide -- and the rows differ on screen. Named by
    // the field alone, both Remove buttons announce the same words, and what gets removed by
    // mistake is a keep rule, so the next scan condemns titles the operator believed were held.
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
  // Three composers refused to add a rule with nothing on the page naming the box they were
  // waiting on: the operator pressed Add, nothing happened, and the form did not say why. The
  // condemn composer is the sharpest case, because its OTHER refusal -- a backwards ramp -- has
  // had a sentence beside the boxes all along, so two of its three arms spoke and one did not
  // (#188).
  //
  // Each sentence binds to the empty BOX rather than to the button, because a `disabled` button
  // is out of the Tab order and a description hung on it is unreachable by the operator it is
  // for.
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
    // Wait for the OPTION, not the control (rule 137). This select is enabled before the
    // vocabulary read fills it -- it holds only its "when…" placeholder until then -- so
    // `toBeEnabled` is satisfied one turn early and `selectOptions` throws "Value \"genre\" not
    // found in options". It is the exact shape rule 137 names, and it failed a full-suite run.
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
    // Rule 72's sibling: the same guard, the same silence, a different card. Reached through the
    // keep card's own Field picker, which is the second one on the page.
    const user = renderWithFields();
    const fields = await screen.findAllByLabelText("Field");
    const keepField = fields[fields.length - 1]!;
    // The same one-turn-early wait as the composer above (rule 72).
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
    // The third (rule 72), and the one whose box is a number rather than a suggest input, so the
    // branch carrying the description is a different element. A lean has no yes/no arm: it always
    // ramps to a number, which is why its sentence asks for one.
    const user = renderWithFields();
    await waitFor(async () =>
      expect((await screen.findAllByLabelText("Field")).length).toBeGreaterThan(1),
    );
    await user.click(screen.getByRole("button", { name: "Leans toward keeping" }));

    const fields = await screen.findAllByLabelText("Field");
    const leanField = fields[fields.length - 1]!;
    await user.selectOptions(leanField, "quality_score");

    const box = screen.getByLabelText("Full effect at");
    expect(box).toHaveAccessibleDescription("Enter a number to add this rule.");
    expect(screen.getAllByRole("button", { name: "Add rule" }).at(-1)!).toBeDisabled();

    await user.type(box, "70");

    expect(box).toHaveAccessibleDescription("");
    expect(screen.getAllByRole("button", { name: "Add rule" }).at(-1)!).toBeEnabled();
  });
});

// "Up to 10 points" cannot be read without knowing what earns them: ten points on a library
// of well-rated titles is ten points that can never be earned, and until now the range lived
// only in the stored body (#410). How many boxes a signal gets is arithmetic, not taste --
// see `signalRamp`'s two shapes.
/** The strip drawn under one named signal.
 *
 *  Scoped to its own row on purpose: every signal draws one, so an unscoped
 *  `querySelector` silently answers for whichever sits highest on the page -- which is how
 *  this pair of tests first read green against the dormancy ramp's geometry.
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
    // The range is drawn now rather than restated: the strip charges everything BELOW the
    // bar, so its fill starts at the left edge and stops where the bar sits (7.0 of 10).
    const { fill } = stripFor("How low it's rated");
    expect(fill.style.left).toBe("0%");
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
    // adding all of it. Full color therefore belongs at 10%, not at the edge the fill happens
    // to stop on. Drawing one gradient edge to edge put it at 3650 days, ten times the bound,
    // while the key underneath says "deepest where it adds them all" -- the picture and the
    // words disagreeing about the one fact the picture exists to carry.
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

    // Stored in tenths, and the floor goes back to zero with it: the pair carries one degree
    // of freedom, so a stale floor would leave a second number in the body that nothing reads
    // and nobody can see. The strip is what shows it landed -- the bar moves to 5.5 of 10.
    await waitFor(() => expect(stripFor("How low it's rated").bar.style.left).toBe("55%"));
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

    // The number is the server's, and it is deliberately NOT what this ramp would produce:
    // a component doing its own arithmetic would overwrite it, and this is what notices.
    // Read off the bolded element rather than the sentence, which is split across nodes
    // precisely so the two numbers can be picked out of it.
    //
    // Scoped to the row. Unscoped this was a FLAKE, and it went green locally and red on CI:
    // one mock answers every signal's probe, so all three rows end up showing 3.5, and
    // whether `getByText` found one or three came down to how many had settled by the time
    // the assertion ran. A query that depends on which requests have landed is not a test of
    // the thing it names.
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
    // Silence here would read as "this rule earns nothing", which is a claim about the
    // operator's policy that a failed request never made (rule 17/36).
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

// "Full points at 5 years" is unreachable on a mirror that only goes back one, and until the
// probe opened at the mirror's edge nothing on the page said so. Deliberately NOT a warning:
// the shipped far end is five years, almost nobody's history is that deep, and a warning
// firing for everyone teaches the page to be ignored.
describe("what the dormancy ramp can actually reach", () => {
  // The example has to MOVE with the setting or it teaches nothing about the control under
  // it. This one opened at the watch mirror's edge, and a mirror deeper than the far end put
  // it past the point the signal already adds in full: it froze at "70 of these 70 points"
  // whatever either box said. So the edge is used only where it BINDS.
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
    // True but consequence-free: a mirror deeper than the far end bounds this signal not at
    // all, and the card is long enough without a sentence that changes nothing.
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

// Making the ramp editable made it losable. 1825 is the measured point the rewatch curve
// flattens, not a number anyone remembers, and the presets restore weights only -- so before
// this there was no way back from a typo except knowing the answer.
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
      expect(stripFor("How long it's gone unwatched").bar.style.left).toBe("10%"),
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

// Rule 40 splits the two number controls on whether the unit can change, and the dormancy
// gate two controls up already offers days/weeks/months/years for the same quantity. A bound
// spelled "1825 days" beside a gate spelling the same span "5 years" was the app disagreeing
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
  // `status: "success"` throughout, so "is this an edit" (read off the new draft, synchronously
  // with the query key) and "what did that edit do" (the previous draft's numbers) describe two
  // different bodies for one round trip. The untouched policy always answers
  // `changed_titles: 0`, which made the FIRST edit of every session meet a categorical "your
  // changes do nothing" before anything had scored it. Rule 85.
  const INERT = "Your changes leave every title as it is.";

  it("does not call an edit inert while the numbers on screen answer the previous draft", async () => {
    // `apiMock` is module-level, so its call counts carry across this file. Counted from zero
    // here, or the premise below reads whatever the preceding tests happened to leave.
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
  // Which section is being read is `App` state now, so the address bar can name it
  // (`/policy/tv/deletion`, navUrl.ts). What this page owes that owner is a click reported
  // upward, and a rail drawn from what it is handed back rather than from a second copy of its
  // own.
  //
  // Scrolling is measured from four rects and the document's height, and jsdom answers 0 for
  // both, so the three tests below STATE a geometry rather than read one (rule 119). The numbers
  // are the shape #795 reproduces at in Chromium: a click on "Pace and limits" scrolls to the end
  // of the document, so the page is bottomed out with all four headings on screen. That is the
  // same position scrolling down to read Deletion leaves you in, which is why no rule reading
  // rects can separate the two, and why the click has to be remembered rather than re-measured.
  // `AppUrl.test.tsx` carries the shell's half.
  const VIEWPORT = 900;
  const PAGE = 3000;
  const BOTTOM = PAGE - VIEWPORT;
  /** Each heading's top, in the order they sit on the page: flags, kept, pace, deletion. */
  const LAST_SCREENFUL = [-2000, -1000, 0, 400];
  const MIDWAY = [-2000, 0, 400, 890];

  // Rule 133: `innerHeight` and `scrollY` outlive a render tree, so every stub is handed back.
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
  // The other half of where the operator is, and the one that used to be lost. The Movies/TV
  // switch lived here, unpersisted, so a reload on a policy link reopened the right section with
  // the other media type's caps, budget and weights under it. `App` owns it now and hands it
  // down, the way it hands down the section.
  it("opens on the policy it is handed, with no click to get there", async () => {
    renderEditor({ body: tvBody() }, pace, null, [], "deletion", "tv");

    expect(await screen.findByRole("heading", { name: "TV policy" })).toBeInTheDocument();
    // The read that decides every number on the page. `api.policy` defaults to "movie", so a
    // call carrying "tv" is the prop arriving and cannot be an omission (rule 141).
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
