// SPDX-License-Identifier: AGPL-3.0-or-later
//
// The reasons/ledger card: why the policy condemned them, and the folded ledger behind the
// total. The summary card beside this one (ReapPlan.tsx) owns every other state (loading, a
// failed read, no scan yet, and nothing to reap), so this card renders nothing at all in
// those states rather than saying the same thing twice.
import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { ReapBreakdown as Breakdown, ScanStatus } from "../api";
import { expectNoA11yViolations } from "../test/a11y";
import { renderWithProviders } from "../test/renderWithProviders";
import { ReapBreakdown } from "./ReapBreakdown";

const { apiMock } = await vi.hoisted(async () => ({
  apiMock: (await import("../test/apiMock")).makeApiMock(),
}));
vi.mock("../api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../api")>()),
  api: apiMock,
}));

const GB = 1024 ** 3;

/** A scan status with nothing running, the shape `api.scanStatus` returns. */
const idleScan: ScanStatus = {
  running: false,
  phase: "idle",
  done: 0,
  total: 0,
  percent: 0,
  detail_reason: null,
  error_reason: null,
  snapshot_id: 1,
  followup_queued: false,
};

// The component consults the profile (via useHoldsBackUnmeasured) to know whether the planner
// holds unmeasured items back. Default: allowance 0, so it does (the common case).
function profileWith(maxUnmeasured: number) {
  return {
    max_items_per_run: 10,
    max_bytes_per_run: 1,
    max_items_per_30d: 100,
    max_bytes_per_30d: 1,
    caps_enabled: true,
    grace_days: 14,
    max_unmeasured_per_run: maxUnmeasured,
  };
}

function full(overrides: Partial<Breakdown> = {}): Breakdown {
  return {
    has_snapshot: true,
    policy_condemned: 543,
    policy_condemned_bytes: 4400 * GB,
    hand_spared: 12,
    spares_expired: 0,
    hand_reaped: 38,
    hand_reaped_bytes: 300 * GB,
    hand_reaped_held: 0,
    will_reap: 569,
    will_reap_bytes: 4500 * GB,
    will_reap_unknown: 0,
    movies: 402,
    movies_unknown: 0,
    seasons: 167,
    seasons_unknown: 0,
    condemned_by: [
      { id: "unwatched", count: 521 },
      { id: "low_rating", count: 201 },
    ],
    ...overrides,
  };
}

function renderBreakdown(onReview = () => {}) {
  return renderWithProviders(<ReapBreakdown onGoToReview={onReview} />);
}

/** Opens the closed-by-default ledger fold and returns its panel. */
async function openLedger(user: ReturnType<typeof userEvent.setup>) {
  const summary = await screen.findByText("How this number was reached");
  const disclosure = summary.closest("details") as HTMLDetailsElement;
  expect(disclosure.open).toBe(false);
  await user.click(summary);
  expect(disclosure.open).toBe(true);
  return disclosure;
}

beforeEach(() => {
  vi.clearAllMocks();
  apiMock.reapBreakdown.mockResolvedValue(full());
  apiMock.profile.mockResolvedValue(profileWith(0));
  // Idle by default; the expired-spares notice reads this to know whether to offer a scan.
  apiMock.scanStatus.mockResolvedValue(idleScan);
  apiMock.startScan.mockResolvedValue({ ...idleScan, running: true });
});

describe("the by-reason bars", () => {
  it("has no accessibility violations", async () => {
    const { container } = renderBreakdown();
    await screen.findByText("Gone unwatched too long");
    await expectNoA11yViolations(container);
  });

  it("name each signal in plain words and carry the overlap note", async () => {
    renderBreakdown();
    expect(await screen.findByText("Gone unwatched too long")).toBeInTheDocument();
    expect(screen.getByText("Low rating")).toBeInTheDocument();
    expect(screen.getByText(/have more than one reason, so these add up/)).toBeInTheDocument();
  });

  it("shows a custom rule under its own name", async () => {
    apiMock.reapBreakdown.mockResolvedValue(
      full({ condemned_by: [{ id: "My weekend rule", count: 9 }] }),
    );
    renderBreakdown();
    expect(await screen.findByText("My weekend rule")).toBeInTheDocument();
  });
});

describe("the closed-by-default ledger fold", () => {
  it("stays closed until opened, then shows the policy verdict, the hand changes, and the net", async () => {
    const user = userEvent.setup();
    renderBreakdown();
    await screen.findByText("Gone unwatched too long");

    const disclosure = await openLedger(user);
    expect(within(disclosure).getByText("Condemned by your policy")).toBeInTheDocument();
    expect(within(disclosure).getByText("You spared by hand")).toBeInTheDocument();
    expect(within(disclosure).getByText("You marked to reap by hand")).toBeInTheDocument();
    expect(within(disclosure).getByText("Will be reaped")).toBeInTheDocument();
    expect(within(disclosure).getByText("569")).toBeInTheDocument();
  });

  it("still shows what was condemned when there are no hand changes to fold in", async () => {
    // The fold exists to answer "how was this number reached", and condemned is where the
    // number in front of the fold started, whether or not a hand change moved it since.
    apiMock.reapBreakdown.mockResolvedValue(
      full({ hand_spared: 0, hand_reaped: 0, will_reap: 543, policy_condemned: 543 }),
    );
    const user = userEvent.setup();
    renderBreakdown();
    await screen.findByText("Gone unwatched too long");

    const disclosure = await openLedger(user);
    expect(within(disclosure).getByText("Condemned by your policy")).toBeInTheDocument();
    expect(within(disclosure).getByText("Will be reaped")).toBeInTheDocument();
    // The spared/reaped rows only appear once a hand change has actually moved the number.
    expect(within(disclosure).queryByText("You spared by hand")).not.toBeInTheDocument();
    expect(within(disclosure).queryByText("You marked to reap by hand")).not.toBeInTheDocument();
  });

  it("shows the held-back row when unmeasured titles are actually held back", async () => {
    apiMock.reapBreakdown.mockResolvedValue(
      full({ will_reap: 569, will_reap_unknown: 4, movies_unknown: 3, seasons_unknown: 1 }),
    );
    const user = userEvent.setup();
    renderBreakdown();
    await screen.findByText("Gone unwatched too long");

    const disclosure = await openLedger(user);
    const row = within(disclosure)
      .getByText("Held back, size unknown")
      .closest(".rb-row") as HTMLElement;
    expect(within(row).getByText("− 4")).toBeInTheDocument();
  });

  it("leaves the held-back row off when nothing is held back", async () => {
    const user = userEvent.setup();
    renderBreakdown(); // the default fixture's will_reap_unknown is 0
    await screen.findByText("Gone unwatched too long");

    const disclosure = await openLedger(user);
    expect(within(disclosure).queryByText("Held back, size unknown")).not.toBeInTheDocument();
  });

  it("leaves the held-back row off once the allowance admits them instead", async () => {
    apiMock.profile.mockResolvedValue(profileWith(10)); // allowance on: nothing is held back
    apiMock.reapBreakdown.mockResolvedValue(
      full({ will_reap: 569, will_reap_unknown: 4, movies_unknown: 3, seasons_unknown: 1 }),
    );
    const user = userEvent.setup();
    renderBreakdown();
    await screen.findByText("Gone unwatched too long");

    const disclosure = await openLedger(user);
    expect(within(disclosure).queryByText("Held back, size unknown")).not.toBeInTheDocument();
  });

  it("subtracts the same unmeasured titles the allowance holds back", async () => {
    apiMock.reapBreakdown.mockResolvedValue(
      full({ will_reap: 569, will_reap_unknown: 4, movies_unknown: 3, seasons_unknown: 1 }),
    );
    const user = userEvent.setup();
    renderBreakdown();
    await screen.findByText("Gone unwatched too long");

    // With the allowance off (the default), the planner drops those 4, so the ledger's total
    // counts only 565. The raw 569 never appears in it.
    const disclosure = await openLedger(user);
    expect(within(disclosure).getByText("565")).toBeInTheDocument();
    expect(within(disclosure).queryByText("569")).not.toBeInTheDocument();
  });

  it("keeps the unmeasured in the total once the allowance admits them", async () => {
    apiMock.profile.mockResolvedValue(profileWith(10)); // allowance on
    apiMock.reapBreakdown.mockResolvedValue(
      full({ will_reap: 569, will_reap_unknown: 4, movies_unknown: 3, seasons_unknown: 1 }),
    );
    const user = userEvent.setup();
    renderBreakdown();
    await screen.findByText("Gone unwatched too long");

    const disclosure = await openLedger(user);
    expect(within(disclosure).getByText("569")).toBeInTheDocument();
  });

  it("follows a single title to Review from the footnote", async () => {
    const onReview = vi.fn();
    const user = userEvent.setup();
    renderBreakdown(onReview);
    await screen.findByText("Gone unwatched too long");

    const disclosure = await openLedger(user);
    await user.click(within(disclosure).getByRole("button", { name: "Review" }));
    expect(onReview).toHaveBeenCalledTimes(1);
  });
});

describe("the states this card leaves to the summary card beside it", () => {
  // Every one of these is already said once, in the summary card. Saying it again here would
  // tell the operator the same thing twice on one page.
  it("renders nothing while the read is pending", async () => {
    apiMock.reapBreakdown.mockReturnValue(new Promise(() => {}));
    const { container } = renderBreakdown();
    // The sibling profile and scan-status reads (useHoldsBackUnmeasured, the expired-spares
    // notice) still resolve, so this waits for them to settle before asserting the empty
    // read: reapBreakdown itself never does, so the card stays empty either way.
    await waitFor(() => expect(apiMock.profile).toHaveBeenCalled());
    expect(container).toBeEmptyDOMElement();
  });

  it("renders nothing on a failed read", async () => {
    apiMock.reapBreakdown.mockRejectedValue(new Error("boom"));
    const { container } = renderBreakdown();
    await waitFor(() => expect(apiMock.reapBreakdown).toHaveBeenCalled());
    await waitFor(() => expect(container).toBeEmptyDOMElement());
  });

  it("renders nothing before the first scan", async () => {
    apiMock.reapBreakdown.mockResolvedValue(full({ has_snapshot: false }));
    const { container } = renderBreakdown();
    await waitFor(() => expect(apiMock.reapBreakdown).toHaveBeenCalled());
    await waitFor(() => expect(container).toBeEmptyDOMElement());
  });

  it("renders nothing when a reap would remove nothing", async () => {
    apiMock.reapBreakdown.mockResolvedValue(
      full({ will_reap: 0, policy_condemned: 0, hand_spared: 0, hand_reaped: 0 }),
    );
    const { container } = renderBreakdown();
    await waitFor(() => expect(apiMock.reapBreakdown).toHaveBeenCalled());
    await waitFor(() => expect(container).toBeEmptyDOMElement());
  });
});

describe("the allowance read failing", () => {
  it("still shows why, but drops the ledger it cannot stand behind", async () => {
    apiMock.profile.mockRejectedValue(new Error("boom"));
    apiMock.reapBreakdown.mockResolvedValue(
      full({ will_reap: 569, will_reap_unknown: 4, movies_unknown: 3, seasons_unknown: 1 }),
    );
    renderBreakdown();
    // The reasons a reap would draw from are still real, even though the total is not.
    expect(await screen.findByText("Gone unwatched too long")).toBeInTheDocument();
    expect(screen.queryByText("How this number was reached")).not.toBeInTheDocument();
  });
});

describe("the pointers this card still owns", () => {
  it("reports held hand reaps rather than dropping them", async () => {
    apiMock.reapBreakdown.mockResolvedValue(full({ hand_reaped_held: 2 }));
    renderBreakdown();
    // The operator marked reaps the engine won't honor yet. Say so, and name the operation
    // actually holding them: this reap, never "a scan".
    expect(await screen.findByText(/2 reaps you marked are on hold/)).toBeInTheDocument();
    expect(screen.queryByText(/a scan won't remove/)).not.toBeInTheDocument();
  });

  it("says when expired spares are keeping titles out of the reap, and offers the scan", async () => {
    // Those titles are counted in "You spared by hand" and absent from the total. A spare's
    // clock only advances on a scan, so a scan is the remedy this notice offers, and the copy
    // must not claim the reap itself will take them.
    apiMock.reapBreakdown.mockResolvedValue(full({ hand_spared: 12, spares_expired: 3 }));
    renderBreakdown();
    const notice = (await screen.findByText(/3 titles are kept by spares that expired/)).closest(
      "p",
    )!;
    expect(notice).toHaveClass("notice-warn");
    expect(notice.textContent).toContain("This reap won't remove them");
    expect(notice.textContent).toContain("A new scan judges them again");
    expect(screen.getByRole("button", { name: "Scan now" })).toBeInTheDocument();
  });

  it("starts the scan it offers, and says so while one is running", async () => {
    apiMock.reapBreakdown.mockResolvedValue(full({ spares_expired: 2 }));
    const user = userEvent.setup();
    renderBreakdown();

    await user.click(await screen.findByRole("button", { name: "Scan now" }));

    expect(apiMock.startScan).toHaveBeenCalledTimes(1);
    await waitFor(() => expect(screen.getByRole("button", { name: "Scanning…" })).toBeDisabled());
  });

  it("says the scan didn't start, in the tone every other failure uses", async () => {
    apiMock.reapBreakdown.mockResolvedValue(full({ spares_expired: 2 }));
    apiMock.startScan.mockRejectedValue(new Error("nope"));
    const user = userEvent.setup();
    renderBreakdown();

    await user.click(await screen.findByRole("button", { name: "Scan now" }));

    const failure = await screen.findByText("The scan didn't start. Try again.");
    expect(failure).toHaveClass("notice-error");
  });

  it("shows the expired-spares notice even when the reap would remove nothing", async () => {
    // Every condemned title was spared, and those spares have since expired: the card would
    // otherwise render nothing at all (reapCount is 0), and the operator would be told nothing.
    apiMock.reapBreakdown.mockResolvedValue(
      full({ will_reap: 0, will_reap_bytes: 0, hand_spared: 543, spares_expired: 4 }),
    );
    renderBreakdown();
    expect(await screen.findByText(/4 titles are kept by spares that expired/)).toBeInTheDocument();
  });
});
