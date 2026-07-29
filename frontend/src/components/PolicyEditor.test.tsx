// SPDX-License-Identifier: AGPL-3.0-or-later
// The policy page's two dead ends, and its control grammar.
//
// Both dead ends were states the operator could not get out of from the page that exists
// to fix them: a policy that could not be read showed no way to replace it, and a preset
// click left the removal lane over budget with Save disabled. Each test here fails if
// either fix is reverted.
import { QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import type { CustomCondemn, Policy, PolicyBody, PolicyWarning, ProfileSettings } from "../api";
import { DocsProvider } from "../docs/DocsContext";
import { testQueryClient } from "../test/queryClient";
import type { WarningAnchor, WarningAnchorId, WarningGuard } from "./PolicyEditor";
import { PolicyEditor, WARNING_ANCHORS, anchorClaims } from "./PolicyEditor";

const { apiMock } = vi.hoisted(() => ({
  apiMock: {
    policy: vi.fn(),
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
    const { userEvent } = await import("@testing-library/user-event");
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
    const { userEvent } = await import("@testing-library/user-event");
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
    const { userEvent } = await import("@testing-library/user-event");
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
    const { userEvent } = await import("@testing-library/user-event");
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
    const { userEvent } = await import("@testing-library/user-event");
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
  const { userEvent } = await import("@testing-library/user-event");
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
            secondary: 0,
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
    // The whole point of the warning is that an operator whose reap list is empty has
    // somewhere to read why, so "the server emits it" is not the claim worth pinning --
    // "it reaches the page, beside the control it tells them to change" is.
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
              secondary: 0,
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
              secondary: 0,
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
          message:
            "Nothing will be flagged for removal. Your keep rule counts who watched a title " +
            "in the last year, and your watch history only goes back 3 months. Wait for it to " +
            "build up, or remove that rule.",
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
            secondary: 0,
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
    gates: [{ gate: "rating_floor", enabled, threshold: 70, secondary: 1000, window_days: 365 }],
  });

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

  /** Claimed by no anchor, so it proves the stack is rendering at all in the same render.
   *  Without it, a page showing no warnings whatsoever would fail the cases below for a
   *  reason none of their messages name. */
  const catchAll: PolicyWarning = {
    field: "some_field_no_anchor_claims",
    severity: "warn",
    message: "anchor probe: the catch-all stack",
  };

  it("declares the eight anchors these cases walk", () => {
    // Pinned because the walk is flag-shaped: an anchor deleted from the list takes its own
    // case away with it, and every assertion that remains still passes (rule 145). Move this
    // number in the same commit that adds or removes an anchor.
    expect(WARNING_ANCHORS).toHaveLength(8);
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

  // The anchors whose warning has ONE control that fixes it, and that control's accessible
  // name. The other five warn about a LIST or a card -- the signal sliders, the gate rows, the
  // rule editors -- where there is no single box to point at and binding every child would
  // read the whole card's complaints at each of them. Deliberately partial, and named here so
  // the boundary is a decision on the page rather than an omission (#174).
  // Keyed by FIELD, not by anchor: `keep_last` claims two, and each is fixed from a different
  // control. Bound to the anchor as a whole, the seasons box spoke the scope control's
  // complaint and the scope control said nothing.
  const BOUND: { anchor: WarningAnchorId; controls: Record<string, string> }[] = [
    { anchor: "condemn_at", controls: { condemn_at: "Put a title on the list once it scores" } },
    {
      anchor: "keep_last",
      controls: {
        keep_last_seasons: "Newest seasons to always keep",
        keep_last_scope: "Keep-last scope",
      },
    },
    {
      anchor: "max_unmeasured_per_run",
      controls: { max_unmeasured_per_run: "Items with an unknown size" },
    },
  ];

  for (const { anchor: id, controls } of BOUND) {
    it(`lets each ${id} control speak the warning that is about IT`, async () => {
      // The warning was rendered beside the box and the box never mentioned it, so reaching it
      // meant leaving the control to go looking. Asserted as the accessible DESCRIPTION, which
      // is what a reader computes: an `aria-describedby` naming an id that is not on the page
      // would satisfy an attribute check and still say nothing.
      const anchor = WARNING_ANCHORS.find((a) => a.id === id)!;
      const mine = probes(anchor);

      // Every field this anchor claims has a control named above, so widening an anchor
      // without giving its new field somewhere to speak from fails here rather than silently
      // hanging the warning off whichever control was already bound.
      expect(Object.keys(controls).sort()).toEqual([...anchor.fields].sort());

      await (anchor.guard ? GUARDS[anchor.guard].held : unguarded)([...mine, catchAll]);

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
      const blocking = probes(anchor).map((w) => ({ ...w, severity: "danger" as const }));
      await (anchor.guard ? GUARDS[anchor.guard].held : unguarded)([...blocking, catchAll]);
      await screen.findByText(blocking[0]!.message);

      for (const name of Object.values(controls)) {
        expect(screen.getByLabelText(name)).not.toHaveAttribute("aria-invalid");
      }
    });
  }

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
  // fixture renders -- the two thresholds plus one per built-in signal in `body()` (3) --
  // reconciled by hand against the source. Its honest limit: a slider added to a section this
  // fixture does not mount is missing from both the table and the count, and the two absences
  // hide each other.
  it("names both thresholds for their label, never for the help text under it", async () => {
    renderEditor({ body: body() });

    const condemn = await screen.findByLabelText("Put a title on the list once it scores");
    const floor = screen.getByLabelText("Judge a title only when there's enough to go on");
    expect(condemn).toHaveAttribute("type", "range");
    expect(floor).toHaveAttribute("type", "range");

    const sliders = document.querySelectorAll<HTMLInputElement>('input[type="range"]');
    expect(sliders).toHaveLength(5);
    for (const s of sliders) expect(s.getAttribute("aria-label")).toBeTruthy();
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
        gates: [{ gate: "whitelisted", enabled: true, threshold: 0, secondary: 0, window_days: 0 }],
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
