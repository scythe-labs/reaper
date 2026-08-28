// SPDX-License-Identifier: AGPL-3.0-or-later
// The always-visible bits of the shell that must not go quiet when a request fails.
//
// ScanFreshness is the only staleness signal on the review screen, so a failed fetch has to
// read as a failure rather than as "no scan has run yet" (which is a positive claim, and the
// wrong one). UserMenu's sign-out failure notice has to survive the focus move that
// disabling its own button causes. SectionNav keeps its section names when the phone bar drops
// to icons, and its armed mark must not read as "off" when the safety state is unreadable.
import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import {
  DEFAULT_FIELD_VALUES,
  DEFAULT_GENERAL,
  DEFAULT_PROFILE,
  DEFAULT_UPDATE,
  IDLE_SCAN,
} from "./test/apiFixtures";
import { expectNoA11yViolations } from "./test/a11y";
import { testQueryClient } from "./test/queryClient";
import { renderWithProviders } from "./test/renderWithProviders";
import { App } from "./App";
import { ReapBar } from "./components/ReapBar";
import { ACK_KEY } from "./components/runAck";
import { ScanFreshness } from "./components/ScanFreshness";
import { SectionNav } from "./components/SectionNav";
import { UserMenu } from "./components/UserMenu";
import { WhyPanelFallback } from "./components/WhyPanelFallback";
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

const { apiMock } = await vi.hoisted(async () => ({
  apiMock: (await import("./test/apiMock")).makeApiMock(),
}));

// Only `api` is mocked. ApiError and the types stay real, because ScanFreshness branches on a
// real instance of it.
vi.mock("./api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("./api")>()),
  api: apiMock,
}));

// Answered here for every test, since UserMenu mounts on every shell render. Describes that
// need a different answer set their own value after this reset.
beforeEach(() => {
  apiMock.update.mockResolvedValue(DEFAULT_UPDATE);
  // ReviewQueue's two filter suggesters call this, and every shell mount here renders the
  // queue. Left unanswered, both filters would silently render their failed-read branch. The
  // test suite's check for unanswered mocks cannot catch this one on its own, because the query
  // function here is wrapped in an arrow function, so the check sees a function and stops
  // looking.
  apiMock.vocabularyValues.mockResolvedValue(DEFAULT_FIELD_VALUES);
});

const snapshot: Snapshot = {
  id: 1,
  created_at: "2026-01-01T00:00:00+00:00",
  policy_hash: "p",
  horizon_at: "2025-01-01T00:00:00+00:00",
  item_count: 12,
  degraded: false,
  degraded_reason: null,
  degraded_doc: null,
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

  it("routes an incomplete scan to the page that rescans, and keeps the period out of the amber", async () => {
    const user = userEvent.setup();
    const onGoToJobs = vi.fn();
    const { container } = render(
      <ScanFreshness
        snapshot={{ ...snapshot, degraded: true }}
        isPending={false}
        error={undefined}
        onGoToJobs={onGoToJobs}
      />,
    );
    // The warning is the only one of the three that names no remedy, so it carries the link.
    await user.click(screen.getByRole("button", { name: /go to settings → jobs/i }));
    expect(onGoToJobs).toHaveBeenCalledTimes(1);

    // The amber span starts on the warning sentence's own first word, not on the period that
    // ends the sentence before it. The sentence begins with a visually hidden "Warning: " lead,
    // which carries the severity, since color is the only other way this page shows severity.
    // The sentence then says what an incomplete scan means before it says where to go.
    const warn = container.querySelector(".freshness-warn");
    expect(warn?.textContent?.trimStart()).toMatch(
      /^Warning: The last scan came back incomplete, so Reaper won't act on it\./,
    );
    // And the neutral half keeps a period of its own rather than trailing off mid-sentence.
    expect(container.querySelector(".scan-freshness")?.textContent).toMatch(
      /items\.\s*Warning: The last scan/,
    );
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
  thumb_url: null,
  via_recovery: false,
};

function renderMenu(onGoToAbout: () => void = () => {}) {
  return renderWithProviders(<UserMenu user={user} onGoToAbout={onGoToAbout} />);
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
    // way to close the panel. Both must leave the failure visible.
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

    // The light itself is aria-hidden decoration. The words ride the chip's accessible name,
    // which is the contract a screen reader and this test both rely on.
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
    // The read must settle first. An unanswered check must render nothing, so asserting
    // absence before the query resolves would only prove the pending state.
    await waitFor(() => expect(apiMock.update).toHaveBeenCalled());
    await person.click(screen.getByRole("button", { name: /owner/i }));
    expect(screen.getByRole("button", { name: /sign out/i })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /update available/i })).not.toBeInTheDocument();
  });
});

const SAFETY: Safety = {
  destructive_enabled: false,
  has_password: true,
  recovery_mode: false,
};

function renderNav(view: "review" | "reap" = "review") {
  return renderWithProviders(<SectionNav view={view} onChange={() => {}} />);
}

/** The section nav is the one control that exists at every width. Under 900px it draws icons
 *  alone, so these tests check what a screen reader still gets without the words, such as the
 *  section names and the safety mark. That mark must never read as "off" when the real state is
 *  "could not tell". */
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

  // No mark means the deletion switch is not armed. If a failed safety read also showed no
  // mark, an unreadable state would look the same as a known-safe one. This test pins that a
  // failed read draws the same amber the banner uses when it could not check.
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

// The why panel's loading and error column is one of six surfaces that render WhyShell, so it
// must offer the same contract as the panel it stands in for, a name and a working Escape key.
// Its loading branch has no heading, so the name comes from the lead line instead.
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
  // app root mounts. Nothing else in the suite renders `App`, so the two tests that call
  // `announce` elsewhere mount their own region. Removing this render call from `App` would
  // silence every success sentence in the app while every other test kept passing, because
  // nothing else checks that the mount is there.
  //
  // These tests use the loading and logged-out branches because those two do not need the
  // whole authenticated tree to render. The render site claims the announcer region exists on
  // every branch, so testing only one branch would not prove that claim.
  beforeEach(() => {
    apiMock.me.mockReset();
    apiMock.authContext.mockReset();
    apiMock.authContext.mockResolvedValue({ plex_configured: true, local_account: false });
  });

  /** The live regions the announcer writes to. `role="status"` alone is not enough to find
   *  them, because the loading branch's own wrapper carries that role too. `aria-live` is the
   *  attribute only these two elements have. */
  const regions = () =>
    screen.getAllByRole("status").filter((n) => n.getAttribute("aria-live") === "polite");

  it("mounts two polite regions while the gate is still deciding", async () => {
    apiMock.me.mockReturnValue(new Promise(() => {}));
    renderWithProviders(<App />);

    await screen.findByText("Loading Reaper…");
    expect(regions()).toHaveLength(2);

    // They are also reachable from the store. A presence check alone would miss a region that
    // renders but never subscribes, since that region would satisfy the count while staying
    // silent.
    act(() => announce("Policy saved."));
    expect(regions().map((n) => n.textContent)).toContain("Policy saved.");
  });

  it("keeps them mounted on the signed-out branch", async () => {
    apiMock.me.mockRejectedValue(new ApiError(401, "nope"));
    renderWithProviders(<App />);

    await waitFor(() => expect(regions()).toHaveLength(2));
    act(() => announce("Settings saved."));
    expect(regions().map((n) => n.textContent)).toContain("Settings saved.");
  });
});

describe("the app-wide reap bar", () => {
  // This bar is the only Stop control visible outside the reap-confirm sheet, and the only sign
  // a reap is running once that sheet is closed. It must announce progress, completion, and
  // failure out loud.
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
    renderWithProviders(
      <>
        <Announcer />
        <ReapBar onGoToReap={() => {}} />
      </>,
      { client: queryClient },
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
    // `progressbar`'s ARIA role implies Children Presentational: True. Putting that role on the
    // bar's container prunes View, Stop, and the failed-Stop alert out of the accessibility
    // tree, so a screen reader would hear the percentage with no way to reach Stop. `CardOpen`
    // avoids the same pruning on the queue's four cards, and this bar is another place it
    // applies.
    //
    // RTL's role queries do not emulate that pruning, so querying for the Stop button on its own
    // proves nothing. The real test is whether the progressbar element contains it.
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
    // This is news, not a recap. A page opened onto an already-finished run must not announce
    // it as if it just happened. This is the same running-to-finished transition the cache
    // invalidation keys on.
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
    renderWithProviders(
      <>
        <Announcer />
        <ReapBar onGoToReap={() => {}} />
      </>,
      { client: queryClient },
    );

    await screen.findByText(/Reaped\./);
    expect(screen.queryByText(/Reap finished\./)).not.toBeInTheDocument();
  });

  it("keeps a dismissed result dismissed across a reload", async () => {
    // The status poll reports the last run forever, so without a persisted ack every
    // refresh brought the ended bar back after the operator dismissed it.
    window.localStorage.removeItem(ACK_KEY);
    const finished = {
      ...idle,
      run_id: 7,
      phase: "complete",
      deleted_items: 4,
      deleted_bytes: 4 * 1024 ** 3,
    };
    apiMock.reapStatus.mockResolvedValue(finished);
    const client = testQueryClient();
    client.setQueryData(["reapStatus"], finished);
    const user = userEvent.setup();
    const first = renderWithProviders(<ReapBar onGoToReap={() => {}} />, { client });

    await user.click(await screen.findByRole("button", { name: "Dismiss" }));
    expect(screen.queryByText(/Reaped\./)).not.toBeInTheDocument();

    // A fresh mount stands in for the page reload.
    first.unmount();
    const client2 = testQueryClient();
    client2.setQueryData(["reapStatus"], finished);
    renderWithProviders(<ReapBar onGoToReap={() => {}} />, { client: client2 });
    expect(screen.queryByText(/Reaped\./)).not.toBeInTheDocument();
    window.localStorage.removeItem(ACK_KEY);
  });

  it("draws nothing on the Reap tab, where the page is the dashboard", () => {
    // The reaping card carries the count, the progress, and its own Stop, so the app-wide bar's
    // copies would only duplicate them. It shows on every OTHER tab.
    const queryClient = testQueryClient();
    queryClient.setQueryData(["reapStatus"], runningAt(1, 4));
    renderWithProviders(<ReapBar onGoToReap={() => {}} suppressed />, { client: queryClient });

    expect(screen.queryByRole("progressbar")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Stop" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "View" })).not.toBeInTheDocument();
  });

  it("still refreshes the app when a run ends while suppressed (rule 79)", async () => {
    // Suppressed hides the bar but keeps it mounted, because the post-run cache invalidation
    // lives here. Unmounting it on the Reap tab would drop that refresh for a run that finishes
    // while the operator is watching it there.
    //
    // The poll never settles here, so the cache is exactly what these two writes set and the
    // running-to-ended edge is not raced by a refetch landing back on "running".
    apiMock.reapStatus.mockImplementation(() => new Promise(() => {}));
    const queryClient = testQueryClient();
    queryClient.setQueryData(["reapStatus"], runningAt(1, 4));
    const invalidate = vi.spyOn(queryClient, "invalidateQueries");
    renderWithProviders(<ReapBar onGoToReap={() => {}} suppressed />, { client: queryClient });

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

    await waitFor(() => expect(invalidate).toHaveBeenCalledWith({ queryKey: ["candidates"] }));
  });
});

describe("the authenticated app's heading outline", () => {
  // `App` renders the brand as an `<h1>`, the same class `Login.tsx` uses, so heading
  // navigation (pressing H or 1) has a top-level landing point. No heading level is skipped
  // below it.
  beforeEach(() => {
    apiMock.me.mockReset();
    apiMock.authContext.mockReset();
    apiMock.authContext.mockResolvedValue({ plex_configured: true, local_account: false });
  });

  /** Mounts the whole authenticated shell, because the masthead exists nowhere else. Every read
   *  the tree makes is answered here, so a test suite check fails the run instead of letting a
   *  failed read silently render in its place. */
  function mountTheShell() {
    apiMock.me.mockResolvedValue(user);
    apiMock.safety.mockResolvedValue(SAFETY);
    apiMock.setupStatus.mockResolvedValue({ complete: true, steps: [] });
    apiMock.scanStatus.mockResolvedValue(IDLE_SCAN);
    apiMock.latestSnapshot.mockResolvedValue(snapshot);
    apiMock.fairness.mockResolvedValue({ generated_at: null, horizon_at: null, requesters: [] });
    apiMock.candidates.mockResolvedValue({
      items: [],
      groups: [],
      total: 0,
      total_bytes: 0,
      unknown_size: 0,
      offset: 0,
      snapshot_id: 1,
    });
    apiMock.general.mockResolvedValue(DEFAULT_GENERAL);
    apiMock.profile.mockResolvedValue(DEFAULT_PROFILE);
    apiMock.reapBreakdown.mockResolvedValue({ has_snapshot: true, will_reap: 0, condemned_by: [] });
    return renderWithProviders(<App />);
  }

  it("opens with an h1 naming the app", async () => {
    mountTheShell();
    expect(await screen.findByRole("heading", { level: 1, name: "Reaper" })).toBeInTheDocument();
  });

  it("has no accessibility violations across the masthead, landmarks included", async () => {
    // Checks the section nav, the user menu, and the status strip, the half of the shell no
    // panel-level audit reaches. `pageLevel` is used because this is the whole page. The
    // `region` landmark role is checked only here and on the two sign-in screens, so this is
    // the only place the shell's own landmarks get checked.
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

  /** Their one title. The fact this test turns on is that this title has the verdict `abstain`,
   *  which is deliberately not the lane the queue opens on by default. A title with `condemn`
   *  would agree with the default tab, so a bug that never actually changed the lane would look
   *  identical to a passing test. */
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

  // Opening a title from Scales must land on the Review lane that title is actually in, one of
  // three lanes. Landing on whichever lane the operator last used could put the title's panel
  // above a list that does not contain it, making the card the operator came to see absent,
  // with no way back to it. This is driven through the real shell instead of a callback,
  // because the lane, the selection, and the view are three separate pieces of state that only
  // the assembled app proves stay in agreement.
  it("lands a Scales title on the lane it lives in, not the one the queue was left on", async () => {
    const person = userEvent.setup();
    mountTheShell();
    // Set after the mount, on purpose. All three of these reads are gated on state the operator
    // has not reached yet (`enabled: view === "fairness"`, `scalesUser !== null`,
    // `selectedId !== null`), so none of them fires against the shell's own stubs before this
    // point.
    apiMock.fairness.mockResolvedValue(scalesReport);
    apiMock.person.mockResolvedValue(personDetail);
    // The panel's contents are WhyPanel's own concern and need a full detail fixture to render.
    // This test is about the list behind it, so the read is deliberately made to fail and the
    // panel shows its fallback. The mock is answered with a rejection rather than left unset, so
    // the test suite's unanswered-mock check still means something here.
    apiMock.candidate.mockRejectedValue(new ApiError(503, "not this test's subject"));

    // Where the operator starts is the queue's default lane, which is not the lane the title is on.
    await waitFor(() =>
      expect(screen.getByRole("button", { name: "Condemned" })).toHaveAttribute(
        "aria-current",
        "page",
      ),
    );

    await person.click(screen.getByRole("button", { name: "Scales" }));
    await person.click(await screen.findByRole("button", { name: /marlow/i }));
    await person.click(await screen.findByRole("button", { name: /Nightferry/i }));

    // Checks Review on the Limbo lane, both as stated by the tab and as asked of the server,
    // since the server decides which rows actually show. The lane is a server-side filter, so a
    // tab that merely looked right while the server still returned the Condemned page would be
    // the same bug with the right label on it.
    expect(await screen.findByRole("button", { name: "Review" })).toHaveAttribute(
      "aria-current",
      "page",
    );
    expect(screen.getByRole("button", { name: "Limbo" })).toHaveAttribute("aria-current", "page");
    expect(screen.getByRole("button", { name: "Condemned" })).not.toHaveAttribute("aria-current");
    // Reads the lane out of the actual API call the queue made, rather than matching a full
    // argument list. This test has no opinion about the paging arguments.
    await waitFor(() => expect(apiMock.candidates.mock.calls.at(-1)?.[0]).toBe("abstain"));
  });

  // The jump also seeds the queue's search box, and a seeded box holds state the operator never
  // typed. Backing out of the jump must remove that state too. Back restores the previous view
  // through a raw setter that runs no handler, and the queue unmounts on the way out, taking its
  // once-per-mount ref with it. Without care, the next mount would read the same focus value
  // again and re-seed the search box from a jump that was supposed to be undone. This is driven
  // through the real shell for the same reason as the test above: the focus, the view, and the
  // queue's own state are set in three separate places, and only the assembled app proves they
  // stay in agreement. `AppFocus.test.tsx` checks the same behavior on the Policy and Settings
  // routes, where those views are stubbed instead.
  it("takes the jump's search back out of the queue when the operator backs out of it", async () => {
    // jsdom keeps one session history across the whole file. The test above ends with layers
    // still open, so its teardown hands those history entries back one deferred step at a time.
    // Those are real popstates. Arriving after this test's provider is up, they read as Back
    // presses and consume the ones this test sends below. Drain them while nothing is
    // listening, then clear the marker the provider's mount-time reconcile checks, the same
    // reset `backnav.test.tsx` does between its own tests.
    // The path is part of that reset too: `App` reads its section from the path at mount
    // (navUrl.ts), and those deferred steps land jsdom on whichever entry's URL that happens to
    // be, often another section's. Left alone, this test mounts on Scales and never finds the
    // Condemned tab.
    for (let i = 0; i < 10; i++) await new Promise((resolve) => setTimeout(resolve, 0));
    history.replaceState(null, "", "/");

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

    // Confirms the jump landed and the box is seeded with the title exactly as the queue prints
    // it, so a regression that stops seeding entirely cannot pass this test unnoticed.
    const box = await screen.findByRole("searchbox", { name: /search titles/i });
    await waitFor(() => expect(box).toHaveValue("Nightferry 2021"));

    // Leaving Scales closes the person panel's Back layer along with it, since its open
    // condition names the view. Handing that history entry back runs a real `history.back()`.
    // jsdom delivers its popstate on a later task, and the provider swallows exactly one
    // popstate for it, so this settles that tick first. Pressing Back before it lands would get
    // the press itself swallowed instead.
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 0));
    });

    // Once the sentinel history entry is parked, a Back press arrives as a popstate, the same
    // way `backnav.test.tsx` drives one. Four presses unwind this jump. They are the why panel,
    // the jump itself back to Scales, the person panel, and the nav step that left Review.
    const back = async () => {
      await act(async () => {
        window.dispatchEvent(new PopStateEvent("popstate"));
      });
    };
    await back();
    await back();
    await back();
    await back();

    // The operator ends up back on the list they started from, the same lane they left, with an
    // empty search box and no chip over it.
    expect(await screen.findByRole("button", { name: "Review" })).toHaveAttribute(
      "aria-current",
      "page",
    );
    const boxAfter = await screen.findByRole("searchbox", { name: /search titles/i });
    expect(boxAfter).toHaveValue("");
    expect(screen.queryByRole("button", { name: /Stop searching for/i })).toBeNull();
  });
});
