// SPDX-License-Identifier: AGPL-3.0-or-later
// The always-visible bits of the shell that must not go quiet when a request fails.
//
// ScanFreshness is the only staleness signal on the review screen, so a failed fetch has to
// read as a failure rather than as "no scan has run yet" (which is a positive claim, and the
// wrong one). UserMenu's sign-out failure notice has to survive the focus move that
// disabling its own button causes. SectionNav keeps its section names when the phone bar drops
// to icons, and its armed mark must not read as "off" when the safety state is unreadable.
import { QueryClientProvider } from "@tanstack/react-query";
import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { DEFAULT_GENERAL, DEFAULT_PROFILE, DEFAULT_UPDATE, IDLE_SCAN } from "./test/apiFixtures";
import { expectNoA11yViolations } from "./test/a11y";
import { testQueryClient } from "./test/queryClient";
import { App, ReapBar, ScanFreshness, SectionNav, UserMenu, WhyPanelFallback } from "./App";
import { ScanLine } from "./components/ScanLine";
import { announce, Announcer } from "./announce";
import {
  ApiError,
  type AuthUser,
  type FairnessReport,
  type PersonDetail,
  type Safety,
  type Snapshot,
} from "./api";

const { apiMock } = vi.hoisted(() => ({
  apiMock: {
    logout: vi.fn(),
    update: vi.fn(),
    safety: vi.fn(),
    me: vi.fn(),
    authContext: vi.fn(),
    reapStatus: vi.fn(),
    stopRun: vi.fn(),
    setupStatus: vi.fn(),
    scanStatus: vi.fn(),
    latestSnapshot: vi.fn(),
    fairness: vi.fn(),
    candidates: vi.fn(),
    general: vi.fn(),
    profile: vi.fn(),
    reapBreakdown: vi.fn(),
    person: vi.fn(),
    candidate: vi.fn(),
  },
}));

// Partial mock: ApiError and the types stay real, because ScanFreshness branches on a real
// instance of it.
vi.mock("./api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("./api")>()),
  api: apiMock,
}));

// Answered file-wide: UserMenu rides along in every shell mount, and rule 135's gate
// cannot see a mock fn that exists and returns undefined. Describes that vary the
// answer set their own value after their reset.
beforeEach(() => {
  apiMock.update.mockResolvedValue(DEFAULT_UPDATE);
});

const snapshot: Snapshot = {
  id: 1,
  created_at: "2026-01-01T00:00:00+00:00",
  policy_hash: "p",
  horizon_at: "2025-01-01T00:00:00+00:00",
  item_count: 12,
  degraded: false,
  degraded_reason: null,
  condemned: 3,
  protected: 4,
  abstained: 5,
  unknown_size_items: 0,
  reclaimable_bytes: 0,
};

describe("ScanFreshness", () => {
  it("says it is still checking while the query is pending", () => {
    render(
      <ScanFreshness snapshot={undefined} isPending error={undefined} onGoToJobs={() => {}} />,
    );
    expect(screen.getByText(/checking the last scan/i)).toBeInTheDocument();
  });

  it("reads a 404 as the genuine no-scan-yet state", () => {
    render(
      <ScanFreshness
        snapshot={undefined}
        isPending={false}
        error={new ApiError(404, "not found")}
        onGoToJobs={() => {}}
      />,
    );
    expect(screen.getByText(/no scan has run yet/i)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /settings/i })).toBeInTheDocument();
  });

  it("says the last scan could not be read on any other failure", () => {
    const { container } = render(
      <ScanFreshness
        snapshot={undefined}
        isPending={false}
        error={new ApiError(500, "boom")}
        onGoToJobs={() => {}}
      />,
    );
    expect(screen.queryByText(/no scan has run yet/i)).not.toBeInTheDocument();
    expect(screen.getByText(/couldn't read the last scan/i)).toBeInTheDocument();
    expect(container.querySelector(".notice.notice-error")).not.toBeNull();
  });

  it("shows the incomplete-scan warning when a scan did come back degraded", () => {
    render(
      <ScanFreshness
        snapshot={{ ...snapshot, degraded: true }}
        isPending={false}
        error={undefined}
        onGoToJobs={() => {}}
      />,
    );
    expect(screen.getByText(/came back incomplete/i)).toBeInTheDocument();
  });
});

describe("ScanLine", () => {
  it("is hidden and announces nothing while idle", () => {
    render(<ScanLine running={false} percent={0} />);
    // aria-hidden takes it out of the accessibility tree, so it is only found with hidden.
    const bar = screen.getByRole("progressbar", { hidden: true });
    expect(bar).toHaveAttribute("aria-hidden", "true");
    expect(bar).not.toHaveAttribute("aria-valuenow");
  });

  it("shows and reports the percent while a scan runs", () => {
    render(<ScanLine running percent={42} />);
    const bar = screen.getByRole("progressbar");
    expect(bar).toHaveAttribute("aria-hidden", "false");
    expect(bar).toHaveAttribute("aria-valuenow", "42");
    expect(bar).toHaveAccessibleName(/scanning your library/i);
  });

  it("clamps an out-of-range percent to the fill width", () => {
    const { container, rerender } = render(<ScanLine running percent={150} />);
    expect(container.querySelector(".scanline-fill")).toHaveStyle({ width: "100%" });
    rerender(<ScanLine running percent={-10} />);
    expect(container.querySelector(".scanline-fill")).toHaveStyle({ width: "0%" });
  });
});

const user: AuthUser = {
  id: 1,
  username: "owner",
  provider: "local",
  email: null,
  thumb_url: null,
};

function renderMenu(onGoToAbout: () => void = () => {}) {
  const queryClient = testQueryClient();
  return render(
    <QueryClientProvider client={queryClient}>
      <UserMenu user={user} onGoToAbout={onGoToAbout} />
    </QueryClientProvider>,
  );
}

describe("UserMenu", () => {
  beforeEach(() => {
    apiMock.logout.mockReset();
    apiMock.update.mockReset();
    apiMock.update.mockResolvedValue(DEFAULT_UPDATE);
  });

  it("keeps the panel open so a failed sign-out can be read", async () => {
    apiMock.logout.mockRejectedValue(new ApiError(500, "boom"));
    const person = userEvent.setup();
    renderMenu();

    await person.click(screen.getByRole("button", { name: /owner/i }));
    await person.click(screen.getByRole("button", { name: /sign out/i }));
    expect(await screen.findByText(/couldn't sign you out/i)).toBeInTheDocument();

    // Disabling the focused button moves focus off it, and a click elsewhere is the other
    // way the panel used to close. Neither may take the failure off screen.
    await person.click(document.body);
    expect(screen.getByText(/couldn't sign you out/i)).toBeInTheDocument();
  });

  it("does not show a stale failure the next time the menu opens", async () => {
    apiMock.logout.mockRejectedValue(new ApiError(500, "boom"));
    const person = userEvent.setup();
    renderMenu();

    await person.click(screen.getByRole("button", { name: /owner/i }));
    await person.click(screen.getByRole("button", { name: /sign out/i }));
    expect(await screen.findByText(/couldn't sign you out/i)).toBeInTheDocument();

    await person.keyboard("{Escape}");
    await person.click(screen.getByRole("button", { name: /owner/i }));
    expect(screen.queryByText(/couldn't sign you out/i)).not.toBeInTheDocument();
  });

  it("wears the light and offers the jump to About while an update exists", async () => {
    apiMock.update.mockResolvedValue({
      ...DEFAULT_UPDATE,
      latest: "2026.9.1",
      update_available: true,
    });
    const goToAbout = vi.fn();
    const person = userEvent.setup();
    renderMenu(goToAbout);

    // The light itself is aria-hidden decoration; the words ride the chip's
    // accessible name, which is the one contract a reader and this test share.
    const chip = await screen.findByRole("button", { name: /update available/i });
    await person.click(chip);
    await person.click(screen.getByRole("button", { name: "Update available" }));
    expect(goToAbout).toHaveBeenCalledTimes(1);
    // Taking the jump closes the menu behind it.
    expect(screen.queryByRole("button", { name: /sign out/i })).not.toBeInTheDocument();
  });

  it("stays plain while there is nothing newer to offer", async () => {
    const person = userEvent.setup();
    renderMenu();
    // Settle the read first: an unanswered check must render exactly nothing, and
    // asserting absence before the query lands would pass against the pending state.
    await waitFor(() => expect(apiMock.update).toHaveBeenCalled());
    await person.click(screen.getByRole("button", { name: /owner/i }));
    expect(screen.getByRole("button", { name: /sign out/i })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /update available/i })).not.toBeInTheDocument();
  });
});

const SAFETY: Safety = { destructive_enabled: false, has_password: true, note: null };

function renderNav(view: "review" | "reap" = "review") {
  return render(
    <QueryClientProvider client={testQueryClient()}>
      <SectionNav view={view} onChange={() => {}} />
    </QueryClientProvider>,
  );
}

/** The section nav is the one control that exists at every width, and under 900px it is drawn
 *  as icons alone. Everything below is about what survives losing the words: the names a screen
 *  reader still has, and the safety mark that must never read as "off" when it is really
 *  "couldn't tell". */
describe("SectionNav", () => {
  beforeEach(() => {
    apiMock.safety.mockReset();
  });

  it("names every section, which is all the phone's icon-only bar has to go on", async () => {
    apiMock.safety.mockResolvedValue(SAFETY);
    renderNav();
    for (const label of ["Review", "Policy", "Reap", "Scales", "Settings"]) {
      expect(screen.getByRole("button", { name: label })).toBeInTheDocument();
    }
    await act(async () => {});
  });

  it("states which section you are on rather than only coloring it", async () => {
    apiMock.safety.mockResolvedValue(SAFETY);
    renderNav("reap");
    expect(screen.getByRole("button", { name: "Reap" })).toHaveAttribute("aria-current", "page");
    expect(screen.getByRole("button", { name: "Review" })).not.toHaveAttribute("aria-current");
    await act(async () => {});
  });

  it("marks Reap, and only Reap, while deletion is armed", async () => {
    apiMock.safety.mockResolvedValue({ ...SAFETY, destructive_enabled: true });
    const { container } = renderNav();
    await waitFor(() => expect(container.querySelector(".view-armed-armed")).toBeInTheDocument());
    expect(
      screen.getByRole("button", { name: "Reap" }).querySelector(".view-armed"),
    ).toBeInTheDocument();
    expect(container.querySelectorAll(".view-armed")).toHaveLength(1);
  });

  it("draws no mark while deletion is off", async () => {
    apiMock.safety.mockResolvedValue(SAFETY);
    const { container } = renderNav();
    await act(async () => {});
    expect(container.querySelector(".view-armed")).not.toBeInTheDocument();
  });

  // No mark means "not armed", so falling through to no mark on a failed read would be the
  // fail-open direction on a safety surface: an unreadable state has to look different from a
  // known-safe one (rule 17/36), in the same amber the banner uses to say it could not look.
  it("shows the unknown mark when the safety state cannot be read", async () => {
    apiMock.safety.mockRejectedValue(new ApiError(500, "boom"));
    const { container } = renderNav();
    await waitFor(() => expect(container.querySelector(".view-armed-unknown")).toBeInTheDocument());
    expect(container.querySelector(".view-armed-armed")).not.toBeInTheDocument();
  });

  it("draws nothing on the very first fetch, which knows nothing yet", () => {
    apiMock.safety.mockReturnValue(new Promise(() => {}));
    const { container } = renderNav();
    expect(container.querySelector(".view-armed")).not.toBeInTheDocument();
  });
});

// The why panel's loading/error column is one of the six surfaces rendering WhyShell, so it owes
// the same contract as the panel it stands in for: a name, and an Escape that works. Its loading
// branch has no heading, so the lead line carries the name.
describe("WhyPanelFallback", () => {
  it("names its failure branch", () => {
    render(<WhyPanelFallback error onClose={vi.fn()} />);
    expect(screen.getByRole("complementary", { name: "Something went wrong" })).toBeInTheDocument();
  });

  it("names its loading branch from the lead, having no heading to point at", () => {
    render(<WhyPanelFallback error={false} onClose={vi.fn()} />);
    expect(
      screen.getByRole("complementary", { name: "Fetching what Reaper saw\u2026" }),
    ).toBeInTheDocument();
  });

  it("closes on Escape while it is still loading", async () => {
    const onClose = vi.fn();
    render(<WhyPanelFallback error={false} onClose={onClose} />);
    await userEvent.keyboard("{Escape}");
    expect(onClose).toHaveBeenCalledTimes(1);
  });
});

describe("the announcer's mount point", () => {
  // `announce()` writes to a module store that only speaks through the ONE `<Announcer />` the
  // app root mounts, and nothing else in the suite renders `App` -- the two tests that call
  // `announce` mount their own region. So deleting the line from `App` silenced every success
  // sentence in the product with all 611 tests, lint and build still green. That is the whole
  // feature resting on an unpinned line (#175, rule 118).
  //
  // Driven through the loading and logged-out branches because those are the two the gate can
  // reach without the whole authed tree, and the claim at the render site is that the region is
  // a sibling of EVERY branch (rule 72) -- one branch would not pin that.
  beforeEach(() => {
    apiMock.me.mockReset();
    apiMock.authContext.mockReset();
    apiMock.authContext.mockResolvedValue({ plex_configured: true, local_account: false });
  });

  /** The live regions themselves: `role="status"` is not enough to find them, because the
   *  loading branch's own wrapper carries it too. `aria-live` is what only these two have. */
  const regions = () =>
    screen.getAllByRole("status").filter((n) => n.getAttribute("aria-live") === "polite");

  it("mounts two polite regions while the gate is still deciding", async () => {
    apiMock.me.mockReturnValue(new Promise(() => {}));
    render(
      <QueryClientProvider client={testQueryClient()}>
        <App />
      </QueryClientProvider>,
    );

    await screen.findByText("Loading Reaper…");
    expect(regions()).toHaveLength(2);

    // And they are reachable from the store, which is the half a presence check alone misses:
    // a region rendered but never subscribed would satisfy the count and stay silent.
    act(() => announce("Policy saved."));
    expect(regions().map((n) => n.textContent)).toContain("Policy saved.");
  });

  it("keeps them mounted on the signed-out branch", async () => {
    apiMock.me.mockRejectedValue(new ApiError(401, "nope"));
    render(
      <QueryClientProvider client={testQueryClient()}>
        <App />
      </QueryClientProvider>,
    );

    await waitFor(() => expect(regions()).toHaveLength(2));
    act(() => announce("Settings saved."));
    expect(regions().map((n) => n.textContent)).toContain("Settings saved.");
  });
});

describe("the app-wide reap bar", () => {
  // The one Stop on every screen but the reap sheet, and the only sign of a deletion once the
  // sheet is closed -- which is what it is designed for. It mounted, ran and reached its end,
  // "Reap failed." included, in total silence (#170).
  const idle = {
    running: false,
    run_id: null,
    stopping: false,
    phase: "idle",
    done: 0,
    total: 0,
    deleted_items: 0,
    deleted_bytes: 0,
    skipped: 0,
    title: "",
    error: null,
    report: null,
  };
  const runningAt = (done: number, total: number) => ({
    ...idle,
    running: true,
    run_id: 7,
    phase: "reaping",
    done,
    total,
  });

  function renderBar() {
    const queryClient = testQueryClient();
    queryClient.setQueryData(["reapStatus"], runningAt(1, 4));
    render(
      <QueryClientProvider client={queryClient}>
        <Announcer />
        <ReapBar onView={() => {}} />
      </QueryClientProvider>,
    );
    return queryClient;
  }

  beforeEach(() => {
    apiMock.reapStatus.mockResolvedValue(runningAt(1, 4));
  });

  it("states its progress as a progressbar, in words rather than a bare number", async () => {
    renderBar();

    const bar = await screen.findByRole("progressbar", { name: "Reaping" });
    expect(bar).toHaveAttribute("aria-valuenow", "25");
    expect(bar).toHaveAttribute("aria-valuetext", "25%, 1 of 4 removed");
  });

  it("keeps Stop out of the progressbar, so a reader can still halt the run", async () => {
    // `progressbar` carries ARIA's Children Presentational: True. With the role on the bar's
    // CONTAINER, View, Stop and the alert that reports a failed Stop were all pruned out of the
    // accessibility tree -- a reader watching a live deletion heard the percentage and had no
    // way to halt it. Same pruning `CardOpen` exists to undo on the queue's four cards, so the
    // bar nearest deletion was the one sibling the sweep missed (rule 72).
    //
    // RTL's role queries do not emulate the pruning, so asking for the Stop button proves
    // nothing. What discriminates is whether the progressbar CONTAINS it.
    renderBar();

    const bar = await screen.findByRole("progressbar", { name: "Reaping" });
    const stop = screen.getByRole("button", { name: "Stop" });
    expect(bar).not.toContainElement(stop);
    expect(bar).not.toContainElement(screen.getByRole("button", { name: "View" }));
  });

  it("says out loud that the reap finished, and how much went", async () => {
    const queryClient = renderBar();
    await screen.findByRole("progressbar", { name: "Reaping" });

    act(
      () =>
        void queryClient.setQueryData(["reapStatus"], {
          ...idle,
          run_id: 7,
          phase: "complete",
          deleted_items: 4,
          deleted_bytes: 4 * 1024 ** 3,
        }),
    );

    expect(await screen.findByText(/Reap finished\. 4 souls removed/)).toBeInTheDocument();
  });

  it("says out loud that a reap FAILED, which is the state that matters most", async () => {
    const queryClient = renderBar();
    await screen.findByRole("progressbar", { name: "Reaping" });

    act(
      () =>
        void queryClient.setQueryData(["reapStatus"], {
          ...idle,
          run_id: 7,
          phase: "error",
          error: "Deletion was switched off mid-run.",
          deleted_items: 2,
        }),
    );

    expect(
      await screen.findByText(/Reap failed\. 2 souls removed before it stopped/),
    ).toBeInTheDocument();
  });

  it("says nothing about a run that ended before this tab was open", async () => {
    // News, not a recap. A page opened onto a finished run must not announce it as if it had
    // just happened -- the same running-to-ended edge the cache invalidation keys on.
    const queryClient = testQueryClient();
    const finished = {
      ...idle,
      run_id: 7,
      phase: "complete",
      deleted_items: 4,
      deleted_bytes: 4 * 1024 ** 3,
    };
    apiMock.reapStatus.mockResolvedValue(finished);
    queryClient.setQueryData(["reapStatus"], finished);
    render(
      <QueryClientProvider client={queryClient}>
        <Announcer />
        <ReapBar onView={() => {}} />
      </QueryClientProvider>,
    );

    await screen.findByText(/Reaped\./);
    expect(screen.queryByText(/Reap finished\./)).not.toBeInTheDocument();
  });
});

describe("the authenticated app's heading outline", () => {
  // There was no `h1` at all: `App` rendered the brand as a `<span>` and every view opened at
  // `h2`, so heading navigation (H, 1) had no top-level landing point. `Login.tsx` already used
  // `<h1 className="brand-word">` -- the same class, promoted -- and the pattern was not carried
  // into the shell (#177). A missing root, not a broken outline: no level is skipped below it.
  beforeEach(() => {
    apiMock.me.mockReset();
    apiMock.authContext.mockReset();
    apiMock.authContext.mockResolvedValue({ plex_configured: true, local_account: false });
  });

  /** The whole authed shell, because the masthead exists nowhere else. Every read the tree makes
   *  is answered, or rule 135's gate fails the run rather than letting a failed-read branch
   *  render as if it were the app. */
  function mountTheShell() {
    apiMock.me.mockResolvedValue(user);
    apiMock.safety.mockResolvedValue(SAFETY);
    apiMock.setupStatus.mockResolvedValue({ complete: true, steps: [] });
    apiMock.scanStatus.mockResolvedValue(IDLE_SCAN);
    apiMock.latestSnapshot.mockResolvedValue(snapshot);
    apiMock.fairness.mockResolvedValue({ generated_at: null, horizon_at: null, requesters: [] });
    apiMock.candidates.mockResolvedValue({
      items: [],
      total: 0,
      totalBytes: 0,
      unknownSize: 0,
      offset: 0,
      snapshotId: 1,
    });
    apiMock.general.mockResolvedValue(DEFAULT_GENERAL);
    apiMock.profile.mockResolvedValue(DEFAULT_PROFILE);
    apiMock.reapBreakdown.mockResolvedValue({ has_snapshot: true, will_reap: 0, condemned_by: [] });
    return render(
      <QueryClientProvider client={testQueryClient()}>
        <App />
      </QueryClientProvider>,
    );
  }

  it("opens with an h1 naming the app", async () => {
    mountTheShell();
    expect(await screen.findByRole("heading", { level: 1, name: "Reaper" })).toBeInTheDocument();
  });

  it("has no accessibility violations across the masthead, landmarks included", async () => {
    // The section nav, the user menu and the status strip, which is the half of the shell no
    // panel audit reaches. `pageLevel` because this IS the page: `region` only answers here and
    // in the two sign-in screens, so the landmarks the shell owns are checked nowhere else.
    mountTheShell();
    await screen.findByRole("heading", { level: 1, name: "Reaper" });
    await expectNoA11yViolations(document.body, { pageLevel: true });
  });

  const GB = 1024 ** 3;
  const HORIZON = "2018-01-11T00:00:00+00:00";

  /** One requester on the board, so Scales has a card to open. */
  const scalesReport: FairnessReport = {
    total_requests: 52,
    total_reclaimable_bytes: 0,
    total_reclaimable_items: 0,
    not_in_scan: 0,
    unmatched: [],
    no_snapshot: false,
    horizon_at: HORIZON,
    rows: [
      {
        identity: "plex:7",
        plex_id: 7,
        name: "marlow",
        requests_made: 52,
        gb_granted_bytes: 549 * GB,
        played_by_them: 30,
        reclaimable_items: 0,
        reclaimable_bytes: 0,
      },
    ],
  };

  /** Their one title, and the fact the whole test turns on: it is on the ABSTAIN lane, which is
   *  deliberately not the lane the queue opens on. A `condemn` here would agree with the default
   *  tab, so a lane that never travelled would be indistinguishable from one that did (rule 141). */
  const personDetail: PersonDetail = {
    plex_id: 7,
    name: "marlow",
    seerr_total: 52,
    requests_in_scan: 1,
    gb_granted_bytes: 6 * GB,
    played_by_them: 0,
    reclaimable_items: 0,
    reclaimable_bytes: 0,
    not_in_scan: 0,
    quota: null,
    titles: [
      {
        title: "Nightferry",
        year: 2021,
        media_type: "movie",
        is_4k: false,
        size_bytes: 6 * GB,
        requested_at: null,
        available_at: null,
        watched_by_them: 0,
        verdict: "abstain",
        item_id: 101,
        group_key: null,
        co_requesters: [],
        poster_url: null,
      },
    ],
    unmatched: [],
    horizon_at: HORIZON,
    profile_url: null,
  };

  // Opening a title from Scales means seeing it in Review -- and the queue there is one lane of
  // three. Landing on whichever lane the operator last used put the title's panel above a list
  // the title is not in, so the card they came to see was simply absent, and the two ways back to
  // it (the scroll to the open card, the j/k step) both no-op off-lane. Driven through the real
  // shell rather than the callback, because the lane, the selection and the view are three
  // pieces of state set together and only the assembled app proves they agree.
  it("lands a Scales title on the lane it lives in, not the one the queue was left on", async () => {
    const person = userEvent.setup();
    mountTheShell();
    // Set after the mount, which is safe and deliberate: all three of these reads are gated on
    // state the operator has not reached yet (`enabled: view === "fairness"`, `scalesUser !== null`,
    // `selectedId !== null`), so none of them has fired against the shell's own stubs.
    apiMock.fairness.mockResolvedValue(scalesReport);
    apiMock.person.mockResolvedValue(personDetail);
    // The panel's CONTENTS are WhyPanel's business and need a whole detail fixture to render;
    // this test is about the list behind it, so the read is deliberately failed and the panel
    // shows its fallback. Answered rather than left out, so rule 135's gate still means something.
    apiMock.candidate.mockRejectedValue(new ApiError(503, "not this test's subject"));

    // Where the operator starts: the queue's default lane, which is the one the title is NOT on.
    await waitFor(() =>
      expect(screen.getByRole("button", { name: "Condemned" })).toHaveAttribute(
        "aria-current",
        "page",
      ),
    );

    await person.click(screen.getByRole("button", { name: "Scales" }));
    await person.click(await screen.findByRole("button", { name: /marlow/i }));
    await person.click(await screen.findByRole("button", { name: /Nightferry/i }));

    // Review, on Limbo -- stated by the tab, and asked of the server, which is what actually
    // decides the rows: the lane is a server-side filter, so a tab that merely looked right
    // over a Condemned page would be the same bug wearing the right label.
    expect(await screen.findByRole("button", { name: "Review" })).toHaveAttribute(
      "aria-current",
      "page",
    );
    expect(screen.getByRole("button", { name: "Limbo" })).toHaveAttribute("aria-current", "page");
    expect(screen.getByRole("button", { name: "Condemned" })).not.toHaveAttribute("aria-current");
    // Read the lane out of the call the queue actually made, rather than matching a whole
    // argument list whose paging tail this test has no opinion about (rule 141).
    await waitFor(() => expect(apiMock.candidates.mock.calls.at(-1)?.[0]).toBe("abstain"));
  });

  // The other half of that jump: it also seeds the queue's search box, and a seeded box is state
  // the operator did not type. Backing out of the jump has to take it with them. Back restores
  // the view through the raw setter and runs no handler, so the nav click's `clearFocus` never
  // fires on this route -- and the queue UNMOUNTS on the way out, taking its once-per-nonce ref
  // with it, so the next mount reads the same focus again and seeds from a jump that was undone.
  // Driven through the real shell for the same reason as the test above: the focus, the view and
  // the queue's own state are set in three places and only the assembled app proves they agree.
  it("takes the jump's search back out of the queue when the operator backs out of it", async () => {
    // jsdom carries one session history across a whole file, and the test above ends with layers
    // still open, so its teardown hands those entries back one deferred step at a time. Those are
    // real popstates: arriving after this test's provider is up, they read as Back presses and eat
    // the ones below. Drain them while nothing is listening, then clear the marker the provider's
    // mount-time reconcile keys on, the same reset `backnav.test.tsx` does between its tests.
    for (let i = 0; i < 10; i++) await new Promise((resolve) => setTimeout(resolve, 0));
    history.replaceState(null, "");

    const person = userEvent.setup();
    mountTheShell();
    apiMock.fairness.mockResolvedValue(scalesReport);
    apiMock.person.mockResolvedValue(personDetail);
    apiMock.candidate.mockRejectedValue(new ApiError(503, "not this test's subject"));

    await waitFor(() =>
      expect(screen.getByRole("button", { name: "Condemned" })).toHaveAttribute(
        "aria-current",
        "page",
      ),
    );

    await person.click(screen.getByRole("button", { name: "Scales" }));
    await person.click(await screen.findByRole("button", { name: /marlow/i }));
    await person.click(await screen.findByRole("button", { name: /Nightferry/i }));

    // The jump landed and the box is seeded with the title as the queue prints it. Asserted so a
    // regression that stopped seeding at all could never pass this test by the back door.
    const box = await screen.findByRole("searchbox", { name: /search titles/i });
    await waitFor(() => expect(box).toHaveValue("Nightferry 2021"));

    // Leaving Scales took the person panel's Back layer with it (its condition names the view),
    // and handing that entry back is a real `history.back()`. jsdom delivers its popstate on a
    // later task and the provider swallows exactly one for it, so settle the tick first -- press
    // Back before it lands and the press itself is what gets swallowed.
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 0));
    });

    // A Back press, once the sentinel is parked, arrives as a popstate -- the same way
    // `backnav.test.tsx` drives one. Four of them unwind this jump: the why-panel, the jump
    // itself (back to Scales), the person panel, and the nav step that left Review.
    const back = async () => {
      await act(async () => {
        window.dispatchEvent(new PopStateEvent("popstate"));
      });
    };
    await back();
    await back();
    await back();
    await back();

    // Home again, on the list they started from: the lane they left, and an empty box with no
    // chip over it. Without the fix the queue remounts still holding "Nightferry 2021".
    expect(await screen.findByRole("button", { name: "Review" })).toHaveAttribute(
      "aria-current",
      "page",
    );
    const boxAfter = await screen.findByRole("searchbox", { name: /search titles/i });
    expect(boxAfter).toHaveValue("");
    expect(screen.queryByRole("button", { name: /Stop searching for/i })).toBeNull();
  });
});
