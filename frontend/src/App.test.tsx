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
import { DEFAULT_GENERAL, DEFAULT_PROFILE, IDLE_SCAN } from "./test/apiFixtures";
import { expectNoA11yViolations } from "./test/a11y";
import { testQueryClient } from "./test/queryClient";
import {
  App,
  ReapBar,
  ScanFreshness,
  ScanLine,
  SectionNav,
  UserMenu,
  WhyPanelFallback,
} from "./App";
import { announce, Announcer } from "./announce";
import { ApiError, type AuthUser, type Safety, type Snapshot } from "./api";

const { apiMock } = vi.hoisted(() => ({
  apiMock: {
    logout: vi.fn(),
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
  },
}));

// Partial mock: ApiError and the types stay real, because ScanFreshness branches on a real
// instance of it.
vi.mock("./api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("./api")>()),
  api: apiMock,
}));

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

function renderMenu() {
  const queryClient = testQueryClient();
  return render(
    <QueryClientProvider client={queryClient}>
      <UserMenu user={user} />
    </QueryClientProvider>,
  );
}

describe("UserMenu", () => {
  beforeEach(() => {
    apiMock.logout.mockReset();
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
});
