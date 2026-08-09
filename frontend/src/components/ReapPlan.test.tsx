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
// ["setup"] to say what would turn a real run away. Without them it rendered its "we could not
// read the allowance" branch, which drops the title count from the headline -- silently, under
// tests that pass. Rule 135.
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
  step_count: 2,
  // Two steps, because `item_count` is 2: a run carrying a count with no steps under it is a
  // plan the planner cannot build, and the page renders `steps` unconditionally, so an empty
  // one drew a `<thead>` over an empty `<tbody>` -- a headers-only data table, which axe files
  // as undecided rather than as a failure. The audit below was certifying that shape.
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
    error: null,
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
      // Both `_unknown` shares are required, and omitting them is invisible: `ReapBreakdown`
      // subtracts them from the totals beside it, so `undefined` rendered "NaN movies, NaN TV
      // seasons" behind ten passing tests. Rule 135, reached through the arrow the mock hands
      // React Query rather than through a missing method.
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
    // The response carries a WINDOW of the journal, so the rows this line is counting are not
    // in it. Subtracting the page from itself reads zero, the line disappears, and the operator
    // sees 50 rows with nothing saying the plan is 500 -- the copy failing in the reassuring
    // direction, beside a destructive action (rule 62). Driven: `steps.length` here renders
    // "NaN more steps" against this fixture, since a windowed response is exactly the case
    // where the two numbers differ.
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

  // #375. Both of these turn true without a press -- a newer scan landing under a plan already
  // on screen, or a read that would not answer -- so both are read in document order rather
  // than interrupting whoever is working through the ledger above them.
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
    // This caption used to be the pending branch of the notice below it, so the first thing
    // that notice said was "Warning: Checking whether this plan came from the latest scan…" --
    // a severity claim over a spinner -- and it spoke a second time when the query settled and
    // swapped its own children in place. A loading affordance is markup (#332).
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
    // #260: `error` is durable and the live report is not, so this is the state a restart
    // leaves -- the run is read back from the database alone. Before, the row said "failed"
    // and the operator had no way to reach the reason at all.
    const reason = "Radarr accepted the delete; not confirmed. Reaper could not reach it again.";
    const failed = {
      ...run,
      state: "completed",
      steps: [{ ...run.steps[0]!, state: "failed", error: reason }, run.steps[1]!],
    } as Run;
    apiMock.createRun.mockResolvedValue(failed);
    apiMock.run.mockResolvedValue(failed);

    await buildPlan();

    expect(await screen.findByText(reason)).toBeInTheDocument();
  });

  it("leaves the reason out of a step that has not run", async () => {
    // The other direction, so the assertion above cannot pass by rendering a reason on every
    // row: the ordinary planned fixture carries `error: null` and draws nothing.
    const { container } = await buildPlan();
    expect(container.querySelector(".step-why")).toBeNull();
  });

  it("says why a stopped run stopped, on the row that outlives the report", async () => {
    // The durable reason's only surface. The report panel is dry-run state and the reap
    // sheet reads the in-memory status, so a reload leaves this row holding it alone (#342).
    const reason = "Over the size cap for one run. Nothing was sent.";
    apiMock.runs.mockResolvedValue([{ ...run, state: "aborted", aborted_reason: reason }]);
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
    // The plan is read through the cache, not captured: a run that has since completed
    // must not keep a live Execute over it (B-15).
    apiMock.run.mockResolvedValue({ ...run, state: "completed" });
    const { summary } = await buildPlan();
    await waitFor(() =>
      expect(within(summary).queryByRole("button", { name: /execute/i })).not.toBeInTheDocument(),
    );
  });

  it("says so when the plan behind a history row can't be loaded", async () => {
    // Everything about a plan -- the phrase, the count, Execute, the steps -- hangs off this
    // one query, so a failed fetch used to unmount all of it with no message and no retry, and
    // clicking a history row simply looked like it did nothing (rule 36).
    apiMock.run.mockRejectedValue(new Error("the server dropped it"));
    renderWithProviders(
      <ReapPlan onGoToDeletion={() => {}} onGoToPlexSettings={() => {}} onGoToReview={() => {}} />,
    );
    const person = userEvent.setup();
    await person.click(await screen.findByRole("button", { name: `#${run.id}` }));

    // By ROLE, not by text: the message was there before and still said nothing out loud. It
    // was the one hand-rolled `.notice` the sweep missed, because it is written with a ternary
    // className and the hygiene ban could parse only a quoted literal (rule 145).
    expect(await screen.findByRole("alert")).toHaveTextContent(/couldn't load this plan/i);
  });

  it("won't offer to build a plan from a scan that came back incomplete", async () => {
    // The planner refuses a degraded snapshot outright, so the page says so up front rather
    // than trading the click for a 422 (PR-8).
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
      { media_key: "a", kind: "movie", state: "verified", detail: "one", title: "", checks: [] },
      { media_key: "b", kind: "movie", state: "verified", detail: "two", title: "", checks: [] },
    ],
    ...patch,
  });

  it("says what was walked, and never leads with a zero it fixes by construction", async () => {
    // The old summary led with "0 souls were actually reaped", which is zero for every
    // practice run there has ever been and so proves nothing, then called the per-item
    // outcomes "steps" -- a count that disagreed with the journalled steps below it (I-1).
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

// #383. Until this shipped, the ONLY place in the app that said a run needs Plex was the 409
// the execute route answers with -- which fires after the operator has picked what to delete,
// armed the host and typed the whole confirmation phrase. The refusal is correct and stays;
// these pin that the same fact is now on screen above the button instead of only behind it.
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
    // Beside the control that fixes it (rule 42), and inside the summary that carries Execute
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
    // Rule 17/36. Silence here reads as "nothing is missing" over a run the server is about
    // to refuse, which is the failure this whole feature exists to remove.
    apiMock.setupStatus.mockRejectedValue(new Error("nope"));
    const { summary } = await buildPlan();
    expect(summary).toHaveTextContent(/couldn't check whether Plex and Tautulli are connected/i);
  });
});
