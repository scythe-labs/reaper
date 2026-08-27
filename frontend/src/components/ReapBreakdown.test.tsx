// SPDX-License-Identifier: AGPL-3.0-or-later
//
// The reap breakdown: the ledger (policy verdict, hand changes, the net), the by-reason
// bars, the empty/error/no-scan states, and the two pointers off the page.
import { screen, waitFor } from "@testing-library/react";
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

function renderBreakdown(onPlex = () => {}, onReview = () => {}) {
  return renderWithProviders(<ReapBreakdown onGoToPlexSettings={onPlex} onGoToReview={onReview} />);
}

beforeEach(() => {
  vi.clearAllMocks();
  apiMock.reapBreakdown.mockResolvedValue(full());
  apiMock.profile.mockResolvedValue(profileWith(0));
  // Idle by default; the expired-spares notice reads this to know whether to offer a scan.
  apiMock.scanStatus.mockResolvedValue(idleScan);
  apiMock.startScan.mockResolvedValue({ ...idleScan, running: true });
});

describe("the ledger", () => {
  // This is the count an operator checks before reaping: what the policy condemned, what they
  // changed by hand, and how many files are actually going. A figure read out without the label
  // beside it is a number with no meaning, on the page that decides how much gets deleted.
  it("has no accessibility violations", async () => {
    const { container } = renderBreakdown();
    await screen.findByText("Condemned by your policy");
    await expectNoA11yViolations(container);
  });

  it("shows the policy verdict, the hand changes, and the net", async () => {
    renderBreakdown();
    expect(await screen.findByText("Condemned by your policy")).toBeInTheDocument();
    expect(screen.getByText("You spared by hand")).toBeInTheDocument();
    expect(screen.getByText("You marked to reap by hand")).toBeInTheDocument();
    expect(screen.getByText("Will be reaped")).toBeInTheDocument();
    expect(screen.getByText(/402 movies, 167 TV seasons/)).toBeInTheDocument();
  });

  it("collapses to just the net when there are no hand changes", async () => {
    apiMock.reapBreakdown.mockResolvedValue(
      full({ hand_spared: 0, hand_reaped: 0, will_reap: 543, policy_condemned: 543 }),
    );
    renderBreakdown();
    expect(await screen.findByText("Will be reaped")).toBeInTheDocument();
    // The funnel rows only appear once a hand change has moved the number.
    expect(screen.queryByText("Condemned by your policy")).not.toBeInTheDocument();
    expect(screen.queryByText("You spared by hand")).not.toBeInTheDocument();
  });

  it("names how many can't be measured and won't be removed", async () => {
    apiMock.reapBreakdown.mockResolvedValue(
      full({ will_reap: 569, will_reap_unknown: 4, movies_unknown: 3, seasons_unknown: 1 }),
    );
    renderBreakdown();
    expect(
      await screen.findByText(/4 titles can't be measured, so Reaper won't remove/),
    ).toBeInTheDocument();
    // With the allowance off, the planner drops those 4, so the headline and total count only
    // 565. The raw 569 never appears.
    expect(screen.getAllByText("565").length).toBeGreaterThan(0);
    expect(screen.queryByText("569")).not.toBeInTheDocument();
    // The split subtracts the same four, so the page states one number for one reap:
    // 399 + 166 = 565, never the raw 402 + 167.
    expect(screen.getByText(/399 movies, 166 TV seasons/)).toBeInTheDocument();
  });

  it("rewords the unmeasured line when the allowance admits them", async () => {
    apiMock.profile.mockResolvedValue(profileWith(10)); // allowance on
    apiMock.reapBreakdown.mockResolvedValue(
      full({ will_reap: 569, will_reap_unknown: 4, movies_unknown: 3, seasons_unknown: 1 }),
    );
    renderBreakdown();
    // Allowance on: the planner admits the unmeasured, so the line must not promise they are
    // kept, and the count includes them.
    expect(
      await screen.findByText(/Only as many as your Unknown-size items limit allows are removed/),
    ).toBeInTheDocument();
    expect(screen.queryByText(/won't remove them/)).not.toBeInTheDocument();
    // The unmeasured stay in the count when the allowance admits them, and in the split too,
    // which follows the same set.
    expect(screen.getAllByText("569").length).toBeGreaterThan(0);
    expect(screen.getByText(/402 movies, 167 TV seasons/)).toBeInTheDocument();
  });

  it("says a reap removes nothing when every condemned title is unmeasured", async () => {
    // Checking only `will_reap === 0` would miss this case: the ledger can still render fully,
    // totaling zero, when the whole list is held back for lacking a known size.
    apiMock.reapBreakdown.mockResolvedValue(
      full({
        will_reap: 4,
        will_reap_unknown: 4,
        movies: 3,
        movies_unknown: 3,
        seasons: 1,
        seasons_unknown: 1,
      }),
    );
    renderBreakdown();
    expect(await screen.findByText(/would remove nothing/)).toBeInTheDocument();
    expect(screen.getByText(/couldn't measure any of the titles/)).toBeInTheDocument();
    expect(screen.queryByText("Will be reaped")).not.toBeInTheDocument();
  });

  it("says it couldn't check the allowance rather than printing an adjusted count", async () => {
    apiMock.profile.mockRejectedValue(new Error("boom"));
    apiMock.reapBreakdown.mockResolvedValue(
      full({ will_reap: 569, will_reap_unknown: 4, movies_unknown: 3, seasons_unknown: 1 }),
    );
    renderBreakdown();
    const notice = await screen.findByText(
      /couldn't check how many titles with no known size it may remove/,
    );
    expect(notice).toHaveClass("notice-warn");
    // Neither number may be stated as fact here: not the held-back 565, not the raw 569.
    expect(screen.queryByText("565")).not.toBeInTheDocument();
    expect(screen.queryByText("569")).not.toBeInTheDocument();
    expect(screen.queryByText("Will be reaped")).not.toBeInTheDocument();
  });

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

  it("says it in the singular for one, and stays silent at zero", async () => {
    apiMock.reapBreakdown.mockResolvedValue(full({ spares_expired: 1 }));
    const { unmount } = renderBreakdown();
    expect(await screen.findByText(/1 title is kept by a spare that expired/)).toBeInTheDocument();
    unmount();

    apiMock.reapBreakdown.mockResolvedValue(full({ spares_expired: 0 }));
    renderBreakdown();
    await screen.findByText("Will be reaped");
    expect(screen.queryByText(/expired/)).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Scan now" })).not.toBeInTheDocument();
  });

  it("starts the scan it offers, and says so while one is running", async () => {
    // The notice offers exactly one action, so it has to work: a click starts a scan, and a
    // scan already running (this one, the scheduler's, another device's) replaces the offer
    // rather than inviting a second start. The refresh when it ends is the shell's job
    // (useScanSettled), which is why this component may be navigated away from mid-scan.
    apiMock.reapBreakdown.mockResolvedValue(full({ spares_expired: 2 }));
    const user = userEvent.setup();
    renderBreakdown();

    await user.click(await screen.findByRole("button", { name: "Scan now" }));

    expect(apiMock.startScan).toHaveBeenCalledTimes(1);
    // The started status is seeded into the shared cache, so the label flips without waiting
    // for a poll to come back around.
    await waitFor(() => expect(screen.getByRole("button", { name: "Scanning…" })).toBeDisabled());
  });

  it("says the scan didn't start, in the tone every other failure uses", async () => {
    // An action that fails silently reads as an action that did nothing, so it gets its own
    // notice in the shared error tone, not red text tucked inside the warning.
    apiMock.reapBreakdown.mockResolvedValue(full({ spares_expired: 2 }));
    apiMock.startScan.mockRejectedValue(new Error("nope"));
    const user = userEvent.setup();
    renderBreakdown();

    await user.click(await screen.findByRole("button", { name: "Scan now" }));

    const failure = await screen.findByText("The scan didn't start. Try again.");
    expect(failure).toHaveClass("notice-error");
  });

  it("reads the count in document order and the press failure as an interruption (#375)", async () => {
    // The count is scan-derived and already true before the page loaded, so it needs no live
    // announcement. The failure answers the "Scan now" press, so it does. Both are asserted
    // here, not just the first, because a flag that silences one would silence the other just
    // as quietly.
    apiMock.reapBreakdown.mockResolvedValue(full({ spares_expired: 2 }));
    apiMock.startScan.mockRejectedValue(new Error("nope"));
    const user = userEvent.setup();
    renderBreakdown();

    const standing = await screen.findByText(/2 titles are kept by spares that expired/);
    expect(standing.closest(".notice")).not.toHaveAttribute("role", "alert");

    await user.click(await screen.findByRole("button", { name: "Scan now" }));

    const failure = await screen.findByText("The scan didn't start. Try again.");
    expect(failure).toHaveAttribute("role", "alert");
  });

  it("shows the notice even when the reap would remove nothing", async () => {
    // The case that matters most: every condemned title was spared, and those spares have
    // since expired. The ledger collapses to its empty state, so if this line lived inside it
    // the operator would be told nothing at all.
    apiMock.reapBreakdown.mockResolvedValue(
      full({ will_reap: 0, will_reap_bytes: 0, hand_spared: 543, spares_expired: 4 }),
    );
    renderBreakdown();
    expect(await screen.findByText(/4 titles are kept by spares that expired/)).toBeInTheDocument();
  });
});

describe("the by-reason bars", () => {
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

describe("the states that are not a full ledger", () => {
  it("says nothing would be reaped when the net is empty", async () => {
    apiMock.reapBreakdown.mockResolvedValue(
      full({ will_reap: 0, policy_condemned: 0, hand_spared: 0, hand_reaped: 0 }),
    );
    renderBreakdown();
    expect(await screen.findByText(/would remove nothing/)).toBeInTheDocument();
  });

  it("prompts a scan before the first one", async () => {
    apiMock.reapBreakdown.mockResolvedValue(full({ has_snapshot: false }));
    renderBreakdown();
    expect(await screen.findByText(/No scan yet/)).toBeInTheDocument();
  });

  it("says it couldn't look, in the amber tone, on a failed load", async () => {
    apiMock.reapBreakdown.mockRejectedValue(new Error("boom"));
    renderBreakdown();
    const notice = await screen.findByText(/Couldn't load what a reap would remove/);
    expect(notice).toHaveClass("notice-warn");
  });
});

describe("the pointers off the page", () => {
  it("routes to Plex settings and to the review queue", async () => {
    const onPlex = vi.fn();
    const onReview = vi.fn();
    renderBreakdown(onPlex, onReview);
    const person = userEvent.setup();
    await person.click(await screen.findByRole("button", { name: /Settings → Plex/ }));
    await waitFor(() => expect(onPlex).toHaveBeenCalledTimes(1));
    await person.click(screen.getByRole("button", { name: /Review queue/ }));
    await waitFor(() => expect(onReview).toHaveBeenCalledTimes(1));
  });
});

describe("the arrows drawn on the pointers off this page", () => {
  // The arrow glyph must stay outside the accessible name: if it sits inside the button's text,
  // a screen reader reads the punctuation as part of the control, saying "See Review right
  // arrow" instead of "Review queue". It stays hidden from the name, not removed from the
  // screen.
  //
  // Checked against the whole string with `toHaveAccessibleName`, never a substring matcher:
  // a substring match like /Review queue/ would also pass against the broken name.
  it("keeps them out of what a reader calls the control", async () => {
    renderBreakdown();

    expect(await screen.findByRole("button", { name: /Review queue/ })).toHaveAccessibleName(
      "Review queue",
    );
  });
});
