// SPDX-License-Identifier: AGPL-3.0-or-later
// A plan is frozen against the scan it was built from. If a newer scan has landed since, the
// plan can list titles that scan would now protect, so the warning has to sit in the summary
// that carries Execute, next to the button that rebuilds it. These pin that it is there, and
// that "we could not check" is said out loud rather than rendering nothing.
import { act, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { Run, Snapshot } from "../api";
import { expectNoA11yViolations } from "../test/a11y";
import { DEFAULT_PROFILE, IDLE_SCAN, READY_SETUP } from "../test/apiFixtures";
import { renderWithProviders } from "../test/renderWithProviders";
import { ReapPlan } from "./ReapPlan";

const { apiMock } = await vi.hoisted(async () => ({
  apiMock: (await import("../test/apiMock")).makeApiMock(),
}));

vi.mock("../api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../api")>()),
  api: apiMock,
}));

// The breakdown inside the plan reads the unmeasured allowance (["profile"], via
// useHoldsBackUnmeasured) and the scan poll (["scanStatus"]) on its own; the page itself reads
// ["setup"] to say what would turn a real run away. Without these mocks it renders its "we
// could not read the allowance" branch, which drops the title count from the headline silently,
// even though the test still passes.
beforeEach(() => {
  apiMock.profile.mockResolvedValue(DEFAULT_PROFILE);
  apiMock.scanStatus.mockResolvedValue(IDLE_SCAN);
  apiMock.setupStatus.mockResolvedValue(READY_SETUP);
});

const run = {
  id: 3,
  snapshot_id: 1,
  state: "planned",
  item_count: 2,
  total_bytes: 1024 ** 3,
  held_back_unknown_size: 0,
  confirmation_phrase: "REAP 2 ITEMS 1 GB",
  // The history list formats this through `date()`. The server always sends this field, since
  // `approved_at` is non-nullable and serialized with `isoformat()`. Leaving it out here would
  // go unnoticed under the `as Run` cast, so it is filled in deliberately.
  approved_at: "2026-01-02T00:05:00+00:00",
  step_count: 2,
  // Two steps, because `item_count` is 2: a run carrying a count with no steps under it is a
  // plan the planner cannot build. The page renders `steps` unconditionally, so an empty list
  // would draw a `<thead>` over an empty `<tbody>`, a headers-only data table that axe reports
  // as undecided rather than as a failure. The accessibility check below depends on this
  // fixture having real rows.
  steps: [
    { media_key: "m:1", ordinal: 0, kind: "delete_files", state: "planned", is_canary: true },
    { media_key: "m:2", ordinal: 1, kind: "delete_files", state: "planned", is_canary: false },
  ].map((step) => ({
    ...step,
    method: "DELETE",
    path: `/api/v3/movie/${step.ordinal}`,
    body: null,
    // A planned step has not run, so it has nothing to explain. The failure shape is driven
    // separately below rather than from here, so this fixture keeps covering the ordinary
    // plan where no row carries a reason.
    error_reason: null,
  })),
} as Run;

const snapshot: Snapshot = {
  id: 2,
  created_at: "2026-01-02T00:00:00+00:00",
  policy_hash: "p",
  horizon_at: "2025-01-01T00:00:00+00:00",
  item_count: 10,
  degraded: false,
  degraded_reason: null,
  degraded_doc: null,
  condemned: 2,
  protected: 3,
  abstained: 5,
  unknown_size_items: 0,
  reclaimable_bytes: 0,
};

async function buildPlan() {
  const { container } = renderWithProviders(
    <ReapPlan onGoToDeletion={() => {}} onGoToPlexSettings={() => {}} onGoToReview={() => {}} />,
  );
  const person = userEvent.setup();
  await person.click(screen.getByRole("button", { name: /build a plan/i }));
  const summary = await screen.findByText(run.confirmation_phrase);
  return { person, container, summary: summary.closest(".plan-summary") as HTMLElement };
}

describe("ReapPlan staleness", () => {
  beforeEach(() => {
    apiMock.safety.mockResolvedValue({ destructive_enabled: false });
    apiMock.createRun.mockResolvedValue(run);
    apiMock.run.mockResolvedValue(run);
    apiMock.runs.mockResolvedValue([run]);
    // An empty, readable trash, so the Plex-trash warning stays out of the way of
    // every test that is about something else.
    apiMock.plexTrash.mockResolvedValue({
      configured: true,
      trashed: 0,
      sections_unreadable: 0,
      empties_after_scan: false,
    });
    apiMock.reapBreakdown.mockResolvedValue({
      has_snapshot: true,
      policy_condemned: 2,
      policy_condemned_bytes: 1024 ** 3,
      hand_spared: 0,
      hand_reaped: 0,
      hand_reaped_bytes: 0,
      will_reap: 2,
      will_reap_bytes: 1024 ** 3,
      will_reap_unknown: 0,
      movies: 2,
      // Both `_unknown` fields are required. Omitting one is invisible here because
      // `ReapBreakdown` subtracts them from the totals beside it, so a missing value renders
      // "NaN movies, NaN TV seasons" while other assertions keep passing.
      movies_unknown: 0,
      seasons: 0,
      seasons_unknown: 0,
      condemned_by: [],
    });
    apiMock.latestSnapshot.mockResolvedValue({ ...snapshot, id: run.snapshot_id });
  });

  // The plan summary carries Execute and, beside it, the warning that the plan came from an
  // older scan. An operator who does not hear that warning runs a list their library no longer
  // agrees with.
  it("has no accessibility violations", async () => {
    const { container } = await buildPlan();
    await expectNoA11yViolations(container);
  });

  it("counts the rows it is not showing off step_count, never off the page it was sent", async () => {
    // The response carries a window of the journal, so the rows this line counts are not all of
    // them. Subtracting the page's own length from itself would read zero, making the "more
    // steps" line disappear, so the operator would see 50 rows with nothing saying the plan is
    // actually 500. That is the wrong direction to fail beside a destructive action. Using
    // `steps.length` here instead renders "NaN more steps" against this fixture, since a
    // windowed response is exactly the case where the two numbers differ.
    apiMock.createRun.mockResolvedValue({ ...run, step_count: 500 });
    apiMock.run.mockResolvedValue({ ...run, step_count: 500 });
    await buildPlan();

    expect(await screen.findByText(/498 more steps, not shown/i)).toBeInTheDocument();
    expect(screen.getByText(/The run still covers every one of them/i)).toBeInTheDocument();
  });

  it("says nothing about hidden rows when the window holds the whole plan", async () => {
    await buildPlan();

    expect(screen.queryByText(/not shown/i)).not.toBeInTheDocument();
  });

  it("says nothing when the plan came from the newest scan", async () => {
    const { summary } = await buildPlan();
    expect(within(summary).queryByText(/older scan/i)).not.toBeInTheDocument();
  });

  it("warns beside Execute, with the rebuild, when a newer scan has landed", async () => {
    apiMock.latestSnapshot.mockResolvedValue(snapshot);
    const { summary } = await buildPlan();
    expect(await within(summary).findByText(/came from an older scan/i)).toBeInTheDocument();
    expect(within(summary).getByRole("button", { name: /build a new plan/i })).toBeInTheDocument();
  });

  it("says it could not check when the scan can't be read", async () => {
    apiMock.latestSnapshot.mockRejectedValue(new Error("boom"));
    const { summary } = await buildPlan();
    expect(await within(summary).findByText(/couldn't check/i)).toBeInTheDocument();
  });

  // Both of these can turn true without a press: a newer scan landing under a plan already on
  // screen, or a read that never answers. So both are read in document order rather than
  // interrupting whoever is working through the ledger above them.
  it("does not interrupt with the older-scan warning", async () => {
    apiMock.latestSnapshot.mockResolvedValue(snapshot);
    const { summary } = await buildPlan();
    const stale = await within(summary).findByText(/came from an older scan/i);
    expect(stale.closest(".notice")).not.toHaveAttribute("role", "alert");
  });

  it("does not interrupt with the could-not-check warning", async () => {
    apiMock.latestSnapshot.mockRejectedValue(new Error("boom"));
    const { summary } = await buildPlan();
    const unknown = await within(summary).findByText(/couldn't check/i);
    expect(unknown.closest(".notice")).not.toHaveAttribute("role", "alert");
  });

  it("does not dress the freshness check itself as a warning while it is still running", async () => {
    // If this caption were the pending branch of the notice below it, the notice would open by
    // announcing "Warning: Checking whether this plan came from the latest scan…", a severity
    // claim over a spinner, and then speak a second time once the query settles and swaps its
    // children in. A loading affordance should be plain markup, not a warning.
    let settle: (value: Snapshot) => void = () => {};
    apiMock.latestSnapshot.mockReturnValue(
      new Promise<Snapshot>((resolve) => {
        settle = resolve;
      }),
    );
    const { summary } = await buildPlan();

    const waiting = await within(summary).findByText(/checking whether this plan came from/i);
    expect(waiting.closest(".notice")).toBeNull();
    expect(within(summary).queryByText(/couldn't check/i)).not.toBeInTheDocument();

    await act(async () => {
      settle(snapshot);
    });
    await waitFor(() =>
      expect(within(summary).queryByText(/checking whether this plan came from/i)).toBeNull(),
    );
  });

  it("leaves the staleness wording out of the history list", async () => {
    apiMock.latestSnapshot.mockResolvedValue(snapshot);
    const { container } = await buildPlan();
    const history = container.querySelector(".run-history") as HTMLElement;
    expect(within(history).queryByText(/older scan/i)).not.toBeInTheDocument();
  });
});

describe("the plan the page is showing", () => {
  beforeEach(() => {
    apiMock.safety.mockResolvedValue({ destructive_enabled: true });
    apiMock.createRun.mockResolvedValue(run);
    apiMock.run.mockResolvedValue(run);
    apiMock.runs.mockResolvedValue([run]);
    // An empty, readable trash, so the Plex-trash warning stays out of the way of
    // every test that is about something else.
    apiMock.plexTrash.mockResolvedValue({
      configured: true,
      trashed: 0,
      sections_unreadable: 0,
      empties_after_scan: false,
    });
    apiMock.reapBreakdown.mockResolvedValue({ has_snapshot: false });
    apiMock.latestSnapshot.mockResolvedValue({ ...snapshot, id: run.snapshot_id });
  });

  it("says why a step failed, on a plan reloaded after the run that failed it", async () => {
    // `error_reason` is durable and the live report is not, so this is the state a restart
    // leaves: the run is read back from the database alone, with no in-memory report to fall
    // back on.
    const reason = "Radarr accepted the delete; not confirmed. Reaper could not reach it again.";
    const failed = {
      ...run,
      state: "completed",
      steps: [
        { ...run.steps[0]!, state: "failed", error_reason: { k: "legacy", p: { text: reason } } },
        run.steps[1]!,
      ],
    } as Run;
    apiMock.createRun.mockResolvedValue(failed);
    apiMock.run.mockResolvedValue(failed);

    await buildPlan();

    expect(await screen.findByText(reason)).toBeInTheDocument();
  });

  it("leaves the reason out of a step that has not run", async () => {
    // The other direction, so the assertion above cannot pass by rendering a reason on every
    // row: the ordinary planned fixture carries `error_reason: null` and draws nothing.
    const { container } = await buildPlan();
    expect(container.querySelector(".step-why")).toBeNull();
  });

  it("says why a stopped run stopped, on the row that outlives the report", async () => {
    // The durable reason's only surface: the report panel holds dry-run state, and the reap
    // sheet reads the in-memory status, so a reload leaves this row holding the reason alone.
    const reason = "Over the size cap for one run. Nothing was sent.";
    apiMock.runs.mockResolvedValue([
      { ...run, state: "aborted", aborted_reason: { k: "legacy", p: { text: reason } } },
    ]);
    const { container } = await buildPlan();
    const history = container.querySelector(".run-history") as HTMLElement;
    expect(within(history).getByText(reason)).toBeInTheDocument();
  });

  it("leaves a reason off a history row that did not stop", async () => {
    // The other direction, so the assertion above cannot pass by drawing one on every row.
    const { container } = await buildPlan();
    const history = container.querySelector(".run-history") as HTMLElement;
    expect(history.querySelector(".step-why")).toBeNull();
  });

  it("stops offering Execute once the run it shows has been spent", async () => {
    // The plan is read through the cache, not captured once: a run that has since completed
    // must not keep a live Execute button over it.
    apiMock.run.mockResolvedValue({ ...run, state: "completed" });
    const { summary } = await buildPlan();
    await waitFor(() =>
      expect(within(summary).queryByRole("button", { name: /execute/i })).not.toBeInTheDocument(),
    );
  });

  it("says so when the plan behind a history row can't be loaded", async () => {
    // Everything about a plan (the phrase, the count, Execute, the steps) hangs off this one
    // query, so a failed fetch would otherwise unmount all of it with no message and no retry,
    // and clicking a history row would look like it did nothing.
    apiMock.run.mockRejectedValue(new Error("the server dropped it"));
    renderWithProviders(
      <ReapPlan onGoToDeletion={() => {}} onGoToPlexSettings={() => {}} onGoToReview={() => {}} />,
    );
    const person = userEvent.setup();
    await person.click(await screen.findByRole("button", { name: `#${run.id}` }));

    // Checked by role, not by text, since this message is not announced out loud. It is written
    // with a ternary className, which a source-text ban on hand-rolled `.notice` classes cannot
    // parse, since the ban can only match a quoted literal.
    expect(await screen.findByRole("alert")).toHaveTextContent(/couldn't load this plan/i);
  });

  it("won't offer to build a plan from a scan that came back incomplete", async () => {
    // The planner refuses a degraded snapshot outright, so the page says so up front rather
    // than trading the click for a 422 response.
    apiMock.latestSnapshot.mockResolvedValue({
      ...snapshot,
      id: run.snapshot_id,
      degraded: true,
      degraded_reason: "A source didn't answer.",
    });
    renderWithProviders(
      <ReapPlan onGoToDeletion={() => {}} onGoToPlexSettings={() => {}} onGoToReview={() => {}} />,
    );
    expect(await screen.findByText(/came back incomplete/i)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /build a plan/i })).toBeDisabled();
    // No page for a source that did not answer, and the pair below is the other branch.
    expect(screen.queryByRole("button", { name: /rebuilding a plex library/i })).toBeNull();
  });

  it("offers the help page here too, when the scan named one", async () => {
    // `ScanBar` renders the same pair off the same field. This is pinned at both call sites
    // rather than only in the shared component, since what can go wrong here is this page
    // failing to pass the field, which a test of the shared component alone cannot see.
    apiMock.latestSnapshot.mockResolvedValue({
      ...snapshot,
      id: run.snapshot_id,
      degraded: true,
      degraded_reason: "Plex is listing 210 of the 240 titles Reaper knows as brand new.",
      degraded_doc: "plex-rebuild",
    });
    renderWithProviders(
      <ReapPlan onGoToDeletion={() => {}} onGoToPlexSettings={() => {}} onGoToReview={() => {}} />,
    );
    expect(
      await screen.findByRole("button", { name: /rebuilding a plex library/i }),
    ).toBeInTheDocument();
  });
});

describe("what a practice run reports", () => {
  beforeEach(() => {
    apiMock.safety.mockResolvedValue({ destructive_enabled: false });
    apiMock.createRun.mockResolvedValue(run);
    apiMock.run.mockResolvedValue(run);
    apiMock.runs.mockResolvedValue([run]);
    // An empty, readable trash, so the Plex-trash warning stays out of the way of
    // every test that is about something else.
    apiMock.plexTrash.mockResolvedValue({
      configured: true,
      trashed: 0,
      sections_unreadable: 0,
      empties_after_scan: false,
    });
    apiMock.reapBreakdown.mockResolvedValue({ has_snapshot: false });
    apiMock.latestSnapshot.mockResolvedValue({ ...snapshot, id: run.snapshot_id });
  });

  const report = (patch: Record<string, unknown> = {}) => ({
    run_id: run.id,
    dry_run: true,
    state: "completed",
    aborted_reason: null,
    would_delete_items: 0,
    deleted_bytes: 0,
    deleted_unmeasured: 0,
    skipped: 0,
    outcomes: [
      {
        media_key: "a",
        kind: "movie",
        state: "verified",
        detail_reason: { k: "legacy", p: { text: "one" } },
        title: "",
        checks: [],
        is_canary: false,
      },
      {
        media_key: "b",
        kind: "movie",
        state: "verified",
        detail_reason: { k: "legacy", p: { text: "two" } },
        title: "",
        checks: [],
        is_canary: false,
      },
    ],
    ...patch,
  });

  it("says what was walked, and never leads with a zero it fixes by construction", async () => {
    // A summary leading with "0 souls were actually reaped" would be zero for every practice
    // run there has ever been, so it proves nothing. Calling the per-item outcomes "steps"
    // would also disagree with the journalled step count shown below it.
    apiMock.dryRun.mockResolvedValue(report());
    const { person } = await buildPlan();

    await person.click(screen.getByRole("button", { name: "Practice run" }));

    const line = await screen.findByText(/Practice run complete/);
    expect(line.textContent).toContain("2 souls were walked end to end");
    expect(line.textContent).toContain("nothing was sent");
    expect(line.textContent).not.toContain("steps");
    expect(line.textContent).not.toContain("0 souls");
  });

  it("names the ones a check would hold back rather than counting them as walked", async () => {
    apiMock.dryRun.mockResolvedValue(report({ skipped: 1 }));
    const { person } = await buildPlan();

    await person.click(screen.getByRole("button", { name: "Practice run" }));

    const line = await screen.findByText(/Practice run complete/);
    expect(line.textContent).toContain("2 souls were walked");
    expect(line.textContent).toContain("1 of them would be skipped");
    expect(line.textContent).not.toContain("end to end");
  });
});

// The 409 the execute route answers with is the route's own refusal, and it fires only after
// the operator has picked what to delete, armed the host, and typed the whole confirmation
// phrase. That refusal is correct and stays. These tests pin that the same fact also appears on
// screen above the button, not only behind it.
describe("what would turn a real run away", () => {
  beforeEach(() => {
    apiMock.safety.mockResolvedValue({ destructive_enabled: false });
    apiMock.createRun.mockResolvedValue(run);
    apiMock.run.mockResolvedValue(run);
    apiMock.runs.mockResolvedValue([run]);
    apiMock.plexTrash.mockResolvedValue({
      configured: true,
      trashed: 0,
      sections_unreadable: 0,
      empties_after_scan: false,
    });
    apiMock.reapBreakdown.mockResolvedValue({ has_snapshot: false });
    apiMock.latestSnapshot.mockResolvedValue({ ...snapshot, id: run.snapshot_id });
  });

  it("says nothing extra when the install could reap", async () => {
    // `READY_SETUP` from the shared beforeEach. The quiet case is half the claim: a warning
    // that renders over a healthy install is the same defect as one that never renders.
    const { summary } = await buildPlan();
    expect(summary).not.toHaveTextContent(/can't remove anything/i);
  });

  it("names Plex above Execute, with the way to connect it", async () => {
    apiMock.setupStatus.mockResolvedValue({
      ...READY_SETUP,
      plex_linked: false,
      reap_ready: false,
    });
    const { summary } = await buildPlan();

    expect(summary).toHaveTextContent(/Reaper can't remove anything until Plex is connected/i);
    // This sits beside the control that fixes it, and inside the summary that carries Execute,
    // rather than somewhere further down the page.
    expect(
      within(summary).getByRole("button", { name: /connect plex in settings/i }),
    ).toBeInTheDocument();
  });

  it("names Tautulli too, which the executor refuses on just as hard", async () => {
    apiMock.setupStatus.mockResolvedValue({
      ...READY_SETUP,
      has_tautulli: false,
      scan_ready: false,
      reap_ready: false,
    });
    const { summary } = await buildPlan();
    expect(summary).toHaveTextContent(/until Tautulli is connected/i);
  });

  it("says it could not check, rather than going quiet, when the read fails", async () => {
    // Silence here would read as "nothing is missing" over a run the server is about to
    // refuse, which is the failure this whole feature exists to remove.
    apiMock.setupStatus.mockRejectedValue(new Error("nope"));
    const { summary } = await buildPlan();
    expect(summary).toHaveTextContent(/couldn't check whether Plex and Tautulli are connected/i);
  });
});
