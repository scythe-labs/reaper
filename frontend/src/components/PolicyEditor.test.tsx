// SPDX-License-Identifier: AGPL-3.0-or-later
// The policy page's two dead ends, and its control grammar.
//
// Both dead ends were states the operator could not get out of from the page that exists
// to fix them: a policy that could not be read showed no way to replace it, and a preset
// click left the removal lane over budget with Save disabled. Each test here fails if
// either fix is reverted.
import { QueryClientProvider, QueryClient } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import type { CustomCondemn, Policy, PolicyBody, ProfileSettings } from "../api";
import { DocsProvider } from "../docs/DocsContext";
import { PolicyEditor } from "./PolicyEditor";

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
  paceSettings: ProfileSettings = pace,
  /** Pass an Error to render the editors against a vocabulary fetch that failed. */
  vocabulary: Error | null = null,
) {
  apiMock.policy.mockResolvedValue({
    policy_hash: "hash",
    name: "default",
    warnings: [],
    ...policy,
  });
  apiMock.profile.mockResolvedValue(paceSettings);
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
    warnings: [],
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
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
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

    expect(
      await screen.findByText(/Your saved policy couldn't be read/),
    ).toBeInTheDocument();
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
    await waitFor(() =>
      expect(screen.queryByText(/No cap on run size/)).not.toBeInTheDocument(),
    );
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
    renderEditor({ body: body() }, { ...pace, settings_recovered: true });

    // The shipped defaults can be looser than what was saved, so the Pace page says so
    // rather than silently swapping them (PR-1).
    expect(
      await screen.findByText(/Your saved caps and grace couldn't be read/),
    ).toBeInTheDocument();
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
) {
  const { userEvent } = await import("@testing-library/user-event");
  const user = userEvent.setup();
  const rendered = renderEditor({ body: tvBody(patch) });
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
          { gate: "server_popularity", enabled: true, threshold: 3, secondary: 0, window_days: 365 },
        ],
      },
    });

    const window = await screen.findByLabelText("How far back recent plays count");
    expect(window).toHaveValue(1);
    expect(screen.getByText(/counting plays from the last/)).toBeInTheDocument();
    expect(screen.getByText(/How far back .recently. reaches/)).toBeInTheDocument();
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
