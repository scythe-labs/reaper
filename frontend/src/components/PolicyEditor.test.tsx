// SPDX-License-Identifier: AGPL-3.0-or-later
// The policy page's two dead ends, and its control grammar.
//
// Both dead ends were states the operator could not get out of from the page that exists
// to fix them: a policy that could not be read showed no way to replace it, and a preset
// click left the removal lane over budget with Save disabled. Each test here fails if
// either fix is reverted.
import { QueryClientProvider } from "@tanstack/react-query";
import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { CustomCondemn, Policy, PolicyBody, PolicyWarning, ProfileSettings } from "../api";
import { DocsProvider } from "../docs/DocsContext";
import { expectNoA11yViolations } from "../test/a11y";
import { testQueryClient } from "../test/queryClient";
import type { WarningAnchor, WarningAnchorId, WarningGuard } from "./PolicyEditor";
import { PolicyEditor, WARNING_ANCHORS, anchorClaims } from "./PolicyEditor";

const { apiMock } = vi.hoisted(() => ({
  apiMock: {
    policy: vi.fn(),
    probePolicy: vi.fn(),
    profile: vi.fn(),
    safety: vi.fn(),
    scanStatus: vi.fn(),
    seasonShape: vi.fn(),
    simulate: vi.fn(),
    validatePolicy: vi.fn(),
    vocabulary: vi.fn(),
    vocabularyValues: vi.fn(),
    savePolicy: vi.fn(),
    saveProfile: vi.fn(),
    setDeletion: vi.fn(),
    startScan: vi.fn(),
  },
}));

vi.mock("../api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api")>();
  return { ...actual, api: apiMock };
});

// The probe under each signal's range fires on a 250ms debounce, so most tests here finish
// before it runs. Seeded anyway: rule 135's gate only catches a queryFn that actually ran, so
// an unmocked one would sit silent until the first test that waits long enough. Seeded HERE
// rather than inside `renderEditor`, which runs after a test's own mock and would overwrite it.
beforeEach(() => {
  apiMock.probePolicy.mockResolvedValue({ points: 0.8, detail: "a value" });
});

function body(custom: CustomCondemn[] = []): PolicyBody {
  // A saved body is always on budget: the built-ins plus the operator's own rules total
  // exactly 100, which is what the server enforces.
  const builtIn = 100 - custom.reduce((sum, c) => sum + c.weight, 0);
  return {
    name: "default",
    media_type: "movie",
    condemn_at: 70,
    coverage_floor_bp: 5000,
    keep_last_seasons: 1,
    keep_first_season: false,
    keep_last_scope: "all",
    season_lookahead: 0,
    keep_in_progress: true,
    in_progress_hold_days: 30,
    keep_specials: true,
    protect_incomplete_seasons: true,
    flag_keep_conflicts: false,
    gates: [],
    signals: [
      { signal: "unwatched", weight: Math.round(builtIn * 0.7), saturate_at: 365, floor: 0 },
      { signal: "few_watchers", weight: Math.round(builtIn * 0.2), saturate_at: 3, floor: 0 },
      {
        signal: "low_rating",
        weight: builtIn - Math.round(builtIn * 0.7) - Math.round(builtIn * 0.2),
        saturate_at: 70,
        floor: 0,
      },
    ],
    protect_conditions: [],
    custom_condemn: custom,
    graded_keeps: [],
    keep_tags: [],
    keep_tags_match: "any",
    keep_rating_rules: [],
    keep_rating_match: "any",
  };
}

/** The TV half of the same policy: same shape, the season protections in play. */
function tvBody(patch: Partial<PolicyBody> = {}): PolicyBody {
  return {
    ...body(),
    media_type: "tv",
    keep_last_seasons: 2,
    keep_first_season: true,
    keep_in_progress: true,
    signals: [
      { signal: "unwatched", weight: 60, saturate_at: 365, floor: 0 },
      { signal: "few_watchers", weight: 15, saturate_at: 3, floor: 0 },
      { signal: "season_rank", weight: 15, saturate_at: 5, floor: 0 },
      { signal: "low_rating", weight: 10, saturate_at: 70, floor: 0 },
    ],
    ...patch,
  };
}

const pace: ProfileSettings = {
  max_items_per_run: 10,
  max_bytes_per_run: 500_000_000_000,
  max_items_per_30d: 100,
  max_bytes_per_30d: 2_000_000_000_000,
  caps_enabled: true,
  grace_days: 14,
  max_unmeasured_per_run: 0,
};

function renderEditor(
  policy: Partial<Policy> & { body: PolicyBody },
  /** An Error renders against a profile read that FAILED; "pending" against one still in
   *  flight. The two are deliberately different states on this page (B-29). */
  paceSettings: ProfileSettings | Error | "pending" = pace,
  /** Pass an Error to render the editors against a vocabulary fetch that failed. */
  vocabulary: Error | null = null,
  /** What /policy/validate answers with. The GET's warnings are never rendered, so this
   *  is the only way a warning reaches the page. */
  validationWarnings: PolicyWarning[] = [],
) {
  apiMock.policy.mockResolvedValue({
    policy_hash: "hash",
    name: "default",
    warnings: [],
    ...policy,
  });
  if (paceSettings === "pending") apiMock.profile.mockReturnValue(new Promise(() => {}));
  else if (paceSettings instanceof Error) apiMock.profile.mockRejectedValue(paceSettings);
  else apiMock.profile.mockResolvedValue(paceSettings);
  apiMock.safety.mockResolvedValue({
    destructive_enabled: false,
    has_password: true,
    note: null,
  });
  apiMock.scanStatus.mockResolvedValue({
    running: false,
    phase: "idle",
    done: 0,
    total: 0,
    percent: 0,
    detail: "",
    error: null,
    snapshot_id: null,
    followup_queued: false,
  });
  apiMock.seasonShape.mockResolvedValue({ total_shows: 0, season_counts: {} });
  if (vocabulary) apiMock.vocabulary.mockImplementation(() => Promise.reject(vocabulary));
  else apiMock.vocabulary.mockResolvedValue({ lane: "condemn", fields: [] });
  apiMock.vocabularyValues.mockResolvedValue({ field: "", values: [] });
  apiMock.validatePolicy.mockResolvedValue({
    policy_hash: "hash",
    name: "default",
    body: policy.body,
    warnings: validationWarnings,
  });
  apiMock.simulate.mockResolvedValue({
    exact: true,
    stale_reason: null,
    condemned: 0,
    protected: 0,
    abstained: 0,
    reclaimable_bytes: 0,
    unknown_size_items: 0,
    newly_condemned: 0,
    no_longer_condemned: 0,
    histogram: [],
    examples_newly_condemned: [],
    protected_by: [],
  });
  const queryClient = testQueryClient();
  return render(
    <QueryClientProvider client={queryClient}>
      <DocsProvider>
        <PolicyEditor />
      </DocsProvider>
    </QueryClientProvider>,
  );
}

describe("a policy that couldn't be read", () => {
  // The rules that condemn files are written here, on the longest form in the app: thresholds,
  // weights, and protections, many of them a number beside a switch. A control that reads out as
  // its neighbor is how an operator sets a threshold they never meant to set.
  it("has no accessibility violations", async () => {
    const { container } = renderEditor({ body: body() });
    await screen.findByText("Movies policy");
    // The page is stitched from seven reads, and the rule editors and the deletion switch settle
    // without changing anything a query can wait on. axe reads the DOM directly, so it has to be
    // the settled one (rule 136).
    await act(async () => {});
    await expectNoA11yViolations(container);
  });

  it("says so on the load it happened, with nothing else dirty", async () => {
    // fell_back and needs_save are mutually exclusive on the server: a body that could
    // not be read at all never carries needs_save. The notice used to live inside the
    // savebar, which only renders when something is dirty, so it was invisible in
    // exactly the state it explains.
    const { container } = renderEditor({ body: body(), needs_save: false, fell_back: true });

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

    await screen.findByText("Movies policy");
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
async function renderTvEditor(
  patch: Partial<PolicyBody> = {},
  shape?: { total_shows: number; season_counts: Record<number, number> },
  /** What /policy/validate answers with, for the cases that drive a TV-only warning. */
  validationWarnings: PolicyWarning[] = [],
) {
  const user = userEvent.setup();
  const rendered = renderEditor({ body: tvBody(patch) }, pace, null, validationWarnings);
  if (shape) apiMock.seasonShape.mockResolvedValue(shape);
  await user.click(await screen.findByRole("button", { name: "TV" }));
  await screen.findByText("TV policy");
  return rendered;
}

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
          message:
            "Nothing will be flagged for removal. Reaper can't say who watched a title in the " +
            "last year from a shorter history, and your watch history only goes back 3 months. " +
            "Lower this window to match your history, or wait for it to build up.",
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
          message: "A warning no anchor claims, so it lands in the catch-all stack.",
        },
        {
          field: "protect_conditions",
          severity: "warn",
          // Verbatim from `policy.inspect`'s gate-off arm (rule 144): this is a payload the
          // anchor test hands in, so a drifted copy here reads as the shipped sentence
          // without failing anything.
          message:
            'Nothing will be flagged for removal. Your keep rule on "People who watched it ' +
            'recently" counts the last year, and your watch history only goes back 3 months. ' +
            "Wait for it to build up, or remove that rule.",
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
describe("PolicyEditor warning anchors", () => {
  /** Puts the page in one state and hands it `warnings` back from /policy/validate, which is
   *  the only route a warning reaches the page by. */
  type Drive = (warnings: PolicyWarning[]) => Promise<void>;

  const unguarded: Drive = async (warnings) => {
    renderEditor({ body: body() }, pace, null, warnings);
  };

  const ratingBody = (enabled: boolean): PolicyBody => ({
    ...body(),
    gates: [{ gate: "rating_floor", enabled, threshold: 70, window_days: 365 }],
  });

  /** The rating card holding two bars, on the two scales it draws differently: IMDb is the
   *  ten-point scale with a vote floor beside it, Rotten Tomatoes critics is a bare
   *  percentage. Two rather than one because the complaint this binding exists to stop is a
   *  bar reading its NEIGHBOUR's -- a card with one bar cannot fail that way. */
  const barsBody: PolicyBody = {
    ...ratingBody(true),
    keep_rating_rules: [
      { source: "imdb", floor: 65, min_votes: 5000 },
      { source: "rotten_tomatoes_critic", floor: 75, min_votes: 0 },
    ],
  };

  /** The four protections the server warns about, each in the switch position its warning
   *  actually fires in: the two thresholds are warned about only while their gate is ON (a
   *  gate that is off holds nothing, and its box is not rendered), and the two switches only
   *  while theirs is OFF. So this is not an arrangement chosen to make boxes appear -- it is
   *  the one page state all four warnings can reach an operator in at once.
   *
   *  The default fixture ships no gates at all, which is why the `gates` binding needs its
   *  own state rather than the unguarded one: the block under the list renders either way,
   *  but the rows carrying its boxes do not. */
  const gatesBody: PolicyBody = {
    ...body(),
    gates: [
      { gate: "min_dormancy", enabled: true, threshold: 1095, window_days: 365 },
      { gate: "server_popularity", enabled: true, threshold: 2, window_days: 365 },
      { gate: "streaming_now", enabled: false, threshold: 0, window_days: 365 },
      { gate: "data_horizon", enabled: false, threshold: 0, window_days: 365 },
    ],
  };

  /** The two page states each guard is checked in, plus the accessible name of a control that
   *  exists on the held branch ONLY. That control is what pins a guard's name to the mount
   *  condition it claims to be: without it these cases would assert a guard against itself.
   *  Exhaustive over `WarningGuard` by its type, so a guard added to the editor does not
   *  compile until the states that hold and drop it are written here (rule 145). */
  const GUARDS: Record<WarningGuard, { control: string; held: Drive; absent: Drive }> = {
    pace: {
      control: "Items with an unknown size",
      held: unguarded,
      // A profile read that FAILED, which is how #145 reached an operator: the whole pace
      // section is one error paragraph, and the box this warning sits under is gone with it.
      absent: async (warnings) => {
        renderEditor({ body: body() }, new Error("boom"), null, warnings);
      },
    },
    tv: {
      control: "Newest seasons to always keep",
      held: async (warnings) => {
        await renderTvEditor({}, undefined, warnings);
      },
      // The movies policy the editor opens on: the season card is not rendered at all.
      absent: unguarded,
    },
    ratingGate: {
      control: "Add a rating source",
      held: async (warnings) => {
        renderEditor({ body: ratingBody(true) }, pace, null, warnings);
      },
      // The card is on the page either way. Its bars, and everything warned about them, live
      // under its own switch.
      absent: async (warnings) => {
        renderEditor({ body: ratingBody(false) }, pace, null, warnings);
      },
    },
  };

  /** One warning per field the anchor claims. The claimed fields ARE the probe fields, since
   *  both read the same array in the declaration. */
  const probes = (anchor: WarningAnchor): PolicyWarning[] =>
    anchor.fields.map((field) => ({
      field,
      severity: "warn",
      message: `anchor probe: ${field}`,
    }));

  /** The same probe, one per field the binding table below names -- which for a `prefix`
   *  anchor is wider than the anchor's own `fields`, and for every other one is the same list
   *  by the assertion in that case. Same message shape either way, so a probe reads the same
   *  wherever it turns up on the page. */
  const boundProbes = (controls: Record<string, string>): PolicyWarning[] =>
    Object.keys(controls).map((field) => ({
      field,
      severity: "warn",
      message: `anchor probe: ${field}`,
    }));

  /** Claimed by no anchor, so it proves the stack is rendering at all in the same render.
   *  Without it, a page showing no warnings whatsoever would fail the cases below for a
   *  reason none of their messages name. */
  const catchAll: PolicyWarning = {
    field: "some_field_no_anchor_claims",
    severity: "warn",
    message: "anchor probe: the catch-all stack",
  };

  it("declares the nine anchors these cases walk", () => {
    // Pinned because the walk is flag-shaped: an anchor deleted from the list takes its own
    // case away with it, and every assertion that remains still passes (rule 145). Move this
    // number in the same commit that adds or removes an anchor.
    expect(WARNING_ANCHORS).toHaveLength(9);
  });

  for (const anchor of WARNING_ANCHORS) {
    it(`renders every warning the ${anchor.id} anchor claims, exactly once`, async () => {
      const mine = probes(anchor);
      // The fields it claims are the fields it is probed with -- one list in the declaration,
      // so a claim cannot quietly go unprobed.
      for (const w of mine) expect(anchorClaims(anchor, w.field)).toBe(true);

      await (anchor.guard ? GUARDS[anchor.guard].held : unguarded)([...mine, catchAll]);

      expect(await screen.findByText(catchAll.message)).toBeInTheDocument();
      if (anchor.guard) {
        expect(screen.getByLabelText(GUARDS[anchor.guard].control)).toBeInTheDocument();
      }
      for (const w of mine) {
        // Claiming took this out of the catch-all, so anything found here came from the
        // anchor's own block -- and finding nothing means it rendered nowhere at all.
        expect(await screen.findByText(w.message)).toBeInTheDocument();
        // Exactly once: claiming AND still reaching the catch-all is the other way to be
        // wrong, and it prints the same sentence twice on one page.
        expect(screen.getAllByText(w.message)).toHaveLength(1);
      }
    });
  }

  // The anchors whose warnings each have ONE control that fixes them, and that control's
  // accessible name. SIX of the nine are here; the three that are not warn about a LIST or a
  // card -- the signal sliders and the two rule editors -- where there is no single box to
  // point at and binding every child would read the whole card's complaints at each of them.
  // Deliberately partial, and named here so the boundary is a decision on the page rather than
  // an omission (#174).
  //
  // Three of the six earned their place rather than arriving with one obvious box:
  //
  // `in_progress` claims one field and its remedy ("lower this to match your history") names
  // one box, so leaving it unbound would have added an item to the backlog above rather than a
  // considered boundary.
  //
  // `gates` looks like a list and is not one: every warning in that family names one setting
  // of one protection, so each already has the single owning box the other three lack, and
  // binding them needed no judgment about what a group should say (#189).
  //
  // `keep_rating_rules` was the same shape in disguise, and joined on the server rather than
  // here: three of its four producers sit inside the loop over the bars and name one bar each
  // through `source_label(rule.source)`, so giving them a `keep_rating_rules.{source}.floor`
  // field was all they needed (#189). Its fourth is the card's own -- the protection on with no
  // sources -- and that one stays unbound, below.
  //
  // Keyed by FIELD, not by anchor: `keep_last` claims three, and each is fixed from a different
  // control. Bound to the anchor as a whole, the seasons box spoke the scope control's
  // complaint and the scope control said nothing.
  const BOUND: {
    anchor: WarningAnchorId;
    controls: Record<string, string>;
    /** Fields this anchor DECLARES that deliberately reach no single control, with the reason
     *  at the entry. Without this the check below would read a mixed anchor as an unfinished
     *  one, and the only way to quiet it would be to stop claiming the field -- which drops
     *  the warning off the page rather than down to the catch-all (#145, rule 42). Declared
     *  rather than omitted, and the whole population is pinned by the count case below, so a
     *  binding cannot be given up by quietly moving its field into here. */
    unbound?: readonly string[];
    /** The page state these boxes exist in, where the anchor's own guard does not name it.
     *  `gates` is one: its block is unguarded and renders on the default fixture, but that
     *  fixture ships no protections, so the rows holding its boxes are not there. */
    drive?: Drive;
  }[] = [
    { anchor: "condemn_at", controls: { condemn_at: "Put a title on the list once it scores" } },
    {
      anchor: "gates",
      // The four the server sends today, each named by `engine/policy.py` as
      // `gates.<protection>.<setting>`. `PolicyEditor` binds them generically off the gate id
      // in the served body, so a fifth protection warning about one of these three settings
      // binds with no frontend change -- which is also why this list cannot prove the binding
      // covers every BOX a row draws. The walk below that one does.
      controls: {
        "gates.min_dormancy.threshold": "Give every title time to be rewatched threshold",
        "gates.server_popularity.window_days": "How far back recent plays count",
        "gates.streaming_now.enabled": "Never touch something playing right now",
        "gates.data_horizon.enabled": "Stop if the unwatched time can't be read",
      },
      drive: async (warnings) => {
        renderEditor({ body: gatesBody }, pace, null, warnings);
      },
    },
    {
      anchor: "keep_rating_rules",
      // The three the server sends today all name one bar's number, so each lands on the box
      // holding that number. Named per SOURCE, which is what makes them distinct: `PolicyBody`
      // refuses two rules on one source, so the source keys the row the way the gate id keys a
      // protection row.
      controls: {
        "keep_rating_rules.imdb.floor": "IMDb score out of 10",
        "keep_rating_rules.rotten_tomatoes_critic.floor": "Rotten Tomatoes critics percentage",
      },
      // The card's own complaint, and the one warning here that is genuinely about the card:
      // it fires when the protection is on with no bars at all, so there is no bar row for it
      // to sit on, and the two remedies it offers ("Add a rating source to it, or turn the
      // protection off") are two different controls. It renders in the block under the list,
      // where it is beside both of them.
      unbound: ["keep_rating_rules"],
      drive: async (warnings) => {
        renderEditor({ body: barsBody }, pace, null, warnings);
      },
    },
    {
      anchor: "keep_last",
      controls: {
        keep_last_seasons: "Newest seasons to always keep",
        keep_last_scope: "Keep-last scope",
        // The switch is the third, and it is bound rather than left to the card: the warning
        // it carries (#224) has no remedy this family will recommend, so the one control it
        // can point at is the one whose own help text offers the other way out.
        flag_keep_conflicts: "Ask me first when a removal looks unusual",
      },
    },
    {
      anchor: "max_unmeasured_per_run",
      controls: { max_unmeasured_per_run: "Items with an unknown size" },
    },
    {
      anchor: "in_progress",
      controls: {
        in_progress_hold_days: "Days without watching before a held place is released",
      },
    },
  ];

  it("binds six of the nine anchors, and walks each one it binds", () => {
    // Rule 145: this table is flag-shaped the same way the anchor list is. An entry deleted
    // takes both of its cases away with it and every assertion left still passes, so the count
    // is what says an anchor stopped being bound. Move it in the same commit that binds one or
    // gives one up, and move the sentence above it too -- the three it describes are 9 minus
    // this.
    expect(BOUND).toHaveLength(6);
    expect(new Set(BOUND.map((b) => b.anchor)).size).toBe(BOUND.length);
    // And the exemptions, by name and in full. `unbound` excuses a declared field from the
    // description walk below, so it is the one place a binding could be given up without the
    // count noticing: pinning the whole population is what closes that (rule 145).
    expect(BOUND.flatMap((b) => b.unbound ?? [])).toEqual(["keep_rating_rules"]);
  });

  /** The state a bound anchor's boxes are driven in: its own entry says so where the default
   *  and the guard states do not put them on the page. */
  const driveBound = (entry: (typeof BOUND)[number], anchor: WarningAnchor): Drive =>
    entry.drive ?? (anchor.guard ? GUARDS[anchor.guard].held : unguarded);

  for (const entry of BOUND) {
    const { anchor: id, controls } = entry;
    it(`lets each ${id} control speak the warning that is about IT`, async () => {
      // The warning was rendered beside the box and the box never mentioned it, so reaching it
      // meant leaving the control to go looking. Asserted as the accessible DESCRIPTION, which
      // is what a reader computes: an `aria-describedby` naming an id that is not on the page
      // would satisfy an attribute check and still say nothing.
      const anchor = WARNING_ANCHORS.find((a) => a.id === id)!;
      const mine = boundProbes(controls);

      // Every field this anchor DECLARES is accounted for above -- bound to a control, or
      // named as deliberately card-level -- so widening `fields` without giving the new one
      // somewhere to speak from fails here rather than silently hanging its warning off
      // whichever control was already bound.
      const accounted = [...Object.keys(controls), ...(entry.unbound ?? [])];
      for (const field of anchor.fields) expect(accounted).toContain(field);
      // And nothing here is bound or exempted that the anchor does not claim: a field
      // misspelled above points its box at an id no notice on this page ever carries, which is
      // the failure the description assertions below would report as a missing binding rather
      // than a typo.
      for (const field of accounted) expect(anchorClaims(anchor, field)).toBe(true);
      // Exactly the declared fields, for every anchor that can state them. A `prefix` anchor
      // claims an open family and its `fields` holds one member as a probe, so the table names
      // more of the family than the declaration can (`gates`, four settings across four
      // protections; `keep_rating_rules`, one bar per source); the two checks above are what
      // still holds it to the claim.
      if (anchor.prefix === undefined) {
        expect(accounted.sort()).toEqual([...anchor.fields].sort());
      }

      await driveBound(entry, anchor)([...mine, catchAll]);

      // Wait for the WARNING, not for the box: validation is debounced, so the control is on
      // the page a beat before there is anything for it to describe itself with, and asserting
      // on the box alone would read the empty description every time (rule 137's shape).
      await screen.findByText(mine[0]!.message);

      for (const w of mine) {
        const box = screen.getByLabelText(controls[w.field]!);
        expect(box).toHaveAccessibleDescription(new RegExp(w.message));
        // And ONLY that one: a control speaking a sibling field's complaint sends the operator
        // to fix it here, where it cannot be fixed.
        for (const other of mine) {
          if (other.field === w.field) continue;
          expect(box).not.toHaveAccessibleDescription(new RegExp(other.message));
        }
        // Never invalid. A policy warning does not block a save -- the save gate is a 422 from
        // body validation plus the points budget, and `severity` reaches neither -- so a box
        // marked invalid states a refusal that will not happen, which is the same lie as one
        // that hides the complaint.
        expect(box).not.toHaveAttribute("aria-invalid");
      }
    });

    it(`never marks a ${id} control invalid, even when the warning is danger`, async () => {
      // The severity a `warn` probe cannot reach. The first version of #174 read "danger" as
      // "blocks a save" and marked three controls invalid over values the app saves happily,
      // so this arm is what keeps the encoding from coming back.
      const anchor = WARNING_ANCHORS.find((a) => a.id === id)!;
      const blocking = boundProbes(controls).map((w) => ({ ...w, severity: "danger" as const }));
      await driveBound(entry, anchor)([...blocking, catchAll]);
      await screen.findByText(blocking[0]!.message);

      for (const name of Object.values(controls)) {
        expect(screen.getByLabelText(name)).not.toHaveAttribute("aria-invalid");
      }
    });
  }

  it("leaves no box in a rating bar's row that its own warning cannot reach", async () => {
    // The `gates` walk below, for the family that joined it (#189). Same reason it is needed:
    // the `keep_rating_rules` entry above names the two fields the SERVER sends on this
    // fixture, and those cannot see a third control added to `RatingBarRow` with no binding,
    // because a field nobody sends yet has no probe to go missing (rule 145).
    //
    // IMDb is the row that draws all of them: the ten-point score, the vote floor beside it,
    // and the × that removes the bar. `min_votes` carries no warning today and is bound
    // anyway, off the same generic helper the score uses, so the walk is over what the row
    // RENDERS rather than over what the server currently complains about.
    const BOXES: Record<string, string | null> = {
      "IMDb score out of 10": "keep_rating_rules.imdb.floor",
      "IMDb vote floor": "keep_rating_rules.imdb.min_votes",
      // Deliberately unbound, and the one exemption on this row. It is the way OUT of the bar,
      // not the way to fix its number, and it already says what it does; a complaint about a
      // score read from the remove button would offer deleting the protection as the remedy.
      "Remove the IMDb bar": null,
    };
    const probed: PolicyWarning[] = Object.values(BOXES)
      .filter((field): field is string => field !== null)
      .map((field) => ({ field, severity: "warn", message: `anchor probe: ${field}` }));
    // The neighboring bar's complaint, driven in the same render. This is the misattribution
    // the issue was filed on -- a warning about IMDb spoken by the Rotten Tomatoes row -- so it
    // is probed rather than argued.
    const neighbor: PolicyWarning = {
      field: "keep_rating_rules.rotten_tomatoes_critic.floor",
      severity: "warn",
      message: "anchor probe: the neighboring bar",
    };

    renderEditor({ body: barsBody }, pace, null, [...probed, neighbor, catchAll]);
    await screen.findByText(probed[0]!.message);

    // Typed, unlike the `closest("li")` in the walk below: TypeScript resolves a TAG selector
    // to that tag's element type and a CLASS selector only to `Element`, which `within` will
    // not take.
    const row = screen.getByLabelText("IMDb score out of 10").closest<HTMLElement>(".bar-line")!;
    // A container query rather than a role query, deliberately: the point is to collect boxes
    // this walk does not already know about, and a role query only finds the roles it was told
    // to ask for. `<button>` is in the list for the same reason and matches the × here.
    const boxes = Array.from(row.querySelectorAll("input, select, textarea, button"));
    const named = Object.keys(BOXES).map((name) => within(row).getByLabelText(name));
    expect(boxes).toHaveLength(named.length);
    for (const box of boxes) expect(named).toContain(box);

    for (const [name, field] of Object.entries(BOXES)) {
      const box = within(row).getByLabelText(name);
      if (field === null) {
        expect(box).not.toHaveAccessibleDescription(/anchor probe/);
        continue;
      }
      expect(box).toHaveAccessibleDescription(new RegExp(`anchor probe: ${field}`));
      for (const other of [...probed, neighbor]) {
        if (other.field === field) continue;
        expect(box).not.toHaveAccessibleDescription(new RegExp(other.message));
      }
    }
  });

  it("leaves no box in a protection's row that its own warning cannot reach", async () => {
    // The `gates` entry above names the four fields the SERVER sends today, and rule 145 is
    // exactly why that is not the whole proof: those four cannot see a fifth control added to
    // `GateRow` with no binding, because a field nobody sends yet has no probe to go missing.
    // So the population walked here is the other one -- every box one row draws -- counted and
    // reconciled by hand against what that row renders.
    //
    // `server_popularity` is the row that draws all of them: the switch, a threshold, and the
    // look-back window with its unit picker. The three probes are driven together, which the
    // server never does (each fires on a different fault), because the claim under test is
    // about the row's wiring and not about which faults co-occur.
    //
    // Names, not just a count: a count alone passes when a box is renamed out of one binding
    // and into another's.
    const BOXES: Record<string, string | null> = {
      "Keep what your users actually watch": "gates.server_popularity.enabled",
      "Keep what your users actually watch threshold": "gates.server_popularity.threshold",
      "How far back recent plays count": "gates.server_popularity.window_days",
      // Deliberately unbound, and the one exemption on this row. `QuantityInput` says why: the
      // unit is a control that announces itself and its own value, so binding the pair's
      // complaint to it as well would say the same sentence twice on the way through.
      "How far back recent plays count unit": null,
    };
    const probed: PolicyWarning[] = Object.values(BOXES)
      .filter((field): field is string => field !== null)
      .map((field) => ({ field, severity: "warn", message: `anchor probe: ${field}` }));

    renderEditor({ body: gatesBody }, pace, null, [...probed, catchAll]);
    await screen.findByText(probed[0]!.message);

    const row = screen.getByLabelText("Keep what your users actually watch").closest("li")!;
    // A container query rather than a role query, deliberately: the point is to collect boxes
    // this walk does not already know about, and a role query only finds the roles it was
    // told to ask for. `<button>` is in the list for the same reason and matches nothing here.
    const boxes = Array.from(row.querySelectorAll("input, select, textarea, button"));
    const named = Object.keys(BOXES).map((name) => within(row).getByLabelText(name));
    expect(boxes).toHaveLength(named.length);
    for (const box of boxes) expect(named).toContain(box);

    for (const [name, field] of Object.entries(BOXES)) {
      const box = within(row).getByLabelText(name);
      if (field === null) {
        expect(box).not.toHaveAccessibleDescription(/anchor probe/);
        continue;
      }
      expect(box).toHaveAccessibleDescription(new RegExp(`anchor probe: ${field}`));
      for (const other of probed) {
        if (other.field === field) continue;
        expect(box).not.toHaveAccessibleDescription(new RegExp(other.message));
      }
    }
  });

  for (const anchor of WARNING_ANCHORS) {
    const guard = anchor.guard;
    if (guard === undefined) continue;
    it(`sends ${anchor.id}'s warnings to the catch-all when its control is not mounted`, async () => {
      // #145 as a property of every guarded anchor rather than of the one that was found:
      // on the branch where the block is not there, claiming has to stop, or the warning is
      // dropped from the page instead of falling to the bottom.
      const mine = probes(anchor);
      await GUARDS[guard].absent(mine);

      for (const w of mine) {
        expect(await screen.findByText(w.message)).toBeInTheDocument();
        expect(screen.getAllByText(w.message)).toHaveLength(1);
      }
      // Read after the warnings, not before: the control is trivially absent from a page that
      // has not rendered yet, and this has to say something about the settled one (rule 137).
      expect(screen.queryByLabelText(GUARDS[guard].control)).toBeNull();
    });
  }

  // The two walks above prove a guard an anchor DECLARES, and they are blind to one it should
  // have declared and did not -- which is #145 itself, not a variant of it. The first walk
  // drives an unguarded anchor in one state, and `pace` is held there, so a block sitting
  // inside the pace section had a mount to paint its probe on and the failed-read branch was
  // never rendered at all; the second walk skips the anchor for having no guard to drive.
  // Dropping `guard: "pace"` from `max_unmeasured_per_run` therefore put the unknown-size
  // warning back off the page and this file went green (#167) -- at 40 cases rather than 41,
  // because the mutation deletes the one case that mentions it and every case left still
  // passes, which is the flag shape rule 145 is about. The two guards that DO fail on that
  // mutation only do so by luck of the default state being their absent branch, so the walk
  // below is what actually holds this, not them.
  //
  // So every anchor is also driven through every branch it does NOT name, where rule 42's
  // sentence is the whole invariant: a claimed field lands at its own block or in the bottom
  // catch-all, and either way renders EXACTLY ONCE. Zero is the silent drop; twice is a claim
  // that also fell through. This needs no assertion about which of the two it was -- an anchor
  // is free to be unmounted here, and pinning the location would only re-state the declaration.
  const GUARD_NAMES = Object.keys(GUARDS) as WarningGuard[];

  for (const anchor of WARNING_ANCHORS) {
    for (const guard of GUARD_NAMES) {
      // Its own guard's absent branch is the case above, which asserts more than this one.
      if (anchor.guard === guard) continue;
      it(`still renders ${anchor.id}'s warnings, exactly once, with ${guard} not mounted`, async () => {
        const mine = probes(anchor);
        await GUARDS[guard].absent([...mine, catchAll]);

        expect(await screen.findByText(catchAll.message)).toBeInTheDocument();
        for (const w of mine) {
          expect(await screen.findByText(w.message)).toBeInTheDocument();
          expect(screen.getAllByText(w.message)).toHaveLength(1);
        }
      });
    }
  }

  it("renders both of two warnings that are byte-identical", async () => {
    // #146. Two protect conditions on the same movie-only field produce the same sentence,
    // because `ConditionSpec` carries nothing an operator named. Keyed on field+message they
    // collided, and React logged "two children with the same key" -- both still painted under
    // React 19, but the reconciliation guarantee is what rule 19 is about.
    const twin: PolicyWarning = {
      field: "protect_conditions",
      severity: "danger",
      message: "anchor probe: one sentence, two rules behind it",
    };
    renderEditor({ body: body() }, pace, null, [twin, twin]);

    expect(await screen.findAllByText(twin.message)).toHaveLength(2);
  });
});

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

  // A placeholder is an accessible name of last resort, so this box announced itself as the
  // example text inside it -- and lost even that the moment anything was typed. The tags entered
  // here are a protection: they are what stops a title being removed.
  it("names the keep-tag box for what it does, not for its placeholder", async () => {
    // The box lives inside the keep-tags card, which renders only while its own gate is on
    // (rule 41), so the gate has to be present and enabled for the control to exist at all.
    renderEditor({
      body: {
        ...body(),
        gates: [{ gate: "whitelisted", enabled: true, threshold: 0, window_days: 0 }],
      },
    });

    const tagBox = await screen.findByLabelText("Add a keep tag");
    expect(tagBox.tagName.toLowerCase()).toBe("input");
    expect(screen.queryByLabelText("add a tag…")).not.toBeInTheDocument();
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
    label: "Genre",
    help_text: "",
    type: "text",
    unit_suffix: "",
    ops: ["eq", "contains"],
  };
  const NUMBER_FIELD = {
    key: "quality_score",
    label: "Quality score",
    help_text: "",
    type: "int",
    unit_suffix: "",
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
    expect(await screen.findByLabelText('Where "How low it\'s rated" stops paying')).toBeVisible();
    expect(screen.queryByLabelText('Where "How low it\'s rated" pays in full')).toBeNull();
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
      await screen.findByLabelText('Where "How long it\'s gone unwatched" starts paying'),
    ).toBeVisible();
    expect(
      screen.getByLabelText('Where "How long it\'s gone unwatched" pays in full'),
    ).toBeVisible();
  });

  it("colors a direct ramp deepest at the bound it pays in full at", async () => {
    renderEditor({ body: body() });
    await screen.findByLabelText('Where "How long it\'s gone unwatched" pays in full');

    // This fixture pays in full at 365 days on a 3650-day track, so the flat top starts one
    // tenth along and the fill still runs to the end: past the far bound the signal keeps
    // paying all of it. Full color therefore belongs at 10%, not at the edge the fill happens
    // to stop on. Drawing one gradient edge to edge put it at 3650 days, ten times the bound,
    // while the key underneath says "deepest where it pays in full" -- the picture and the
    // words disagreeing about the one fact the picture exists to carry.
    const { fill } = stripFor("How long it's gone unwatched");
    expect(fill.style.background).toBe(
      "linear-gradient(to right, color-mix(in srgb, var(--condemn) 6%, transparent), var(--condemn) 10%)",
    );
  });

  it("writes a shortfall edit back as the gap, floor and all", async () => {
    const user = userEvent.setup();
    renderEditor({ body: body() });

    const box = await screen.findByLabelText('Where "How low it\'s rated" stops paying');
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
    expect(screen.queryByLabelText('Where "How low it\'s rated" stops paying')).toBeNull();
  });
});

// The probe is a round trip for a number a slider could have computed locally, and that is
// the point: a local copy of the ramp beside the control that tunes deletions is a second
// scorer, free to drift from the one that decides. So what matters here is that the sentence
// only ever shows what the server said, and says so plainly when it has not said it yet.
describe("trying a value against a signal's range", () => {
  it("shows what the engine answered, not a number worked out here", async () => {
    apiMock.probePolicy.mockResolvedValue({ points: 3.5, detail: "IMDb 3.0" });
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
      screen.getAllByText(/Your setting is fine, this is just the preview/).length,
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
  // it past the point the signal already pays in full: it froze at "70 of these 70 points"
  // whatever either box said. So the edge is used only where it BINDS.
  it("describes a title the history caps, when the history is the shorter of the two", async () => {
    // 200 days of history against a far end of 365: nothing can present more than 200, so
    // that is both a moving example and the ceiling the mirror imposes.
    apiMock.probePolicy.mockResolvedValue({ points: 38.4, detail: "not watched in 6 months" });
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

    const far = await screen.findByLabelText('Where "How long it\'s gone unwatched" pays in full');
    // 1825 days is stored; "5 years" is drawn. The policy body never sees the unit.
    expect(far).toHaveValue(5);
    expect(within(far.closest(".qty") as HTMLElement).getByRole("combobox")).toHaveValue("years");
  });

  it("leaves a rating on the fixed suffix, which has no larger unit to offer", async () => {
    renderEditor({ body: body() });

    const box = await screen.findByLabelText('Where "How low it\'s rated" stops paying');
    // A suffix, not a picker: there is no unit above IMDb to switch to.
    expect(within(box.closest(".qty") as HTMLElement).queryByRole("combobox")).toBeNull();
  });
});
