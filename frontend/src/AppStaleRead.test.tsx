// SPDX-License-Identifier: AGPL-3.0-or-later
// The two gates in `App` that sit above the whole signed-in UI, driven through a failed refetch.
//
// React Query keeps the last good data through a failed refetch and raises `isError` beside
// it. So a gate that checks only `isError`, without asking whether a value is still in hand,
// can render the wrong screen on a blink: a configured install's setup wizard over the
// Dashboard, or the login screen at a signed-in operator. The same failure applies to the
// settings panels, at the two places whose blast radius is the whole app: everything below
// them unmounts, so Settings' unsaved-edits guard never runs.
//
// Each gate is pinned in both directions, because a fix that simply deleted the `isError` arm
// would pass a one-sided test while dropping a fresh install onto an empty Dashboard with no
// way back to the wizard. The never-loaded case is the reason those arms exist.
import { act, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { QueryClient } from "@tanstack/react-query";
import { beforeAll, beforeEach, describe, expect, it, vi } from "vitest";
import type { AuthUser, CandidatePage, SetupStatus } from "./api";
import {
  DEFAULT_GENERAL,
  DEFAULT_PROFILE,
  DEFAULT_UPDATE,
  DEFAULT_WATCH_EVIDENCE,
  IDLE_SCAN,
} from "./test/apiFixtures";
import { testQueryClient } from "./test/queryClient";
import { renderWithProviders } from "./test/renderWithProviders";
import { expectNoA11yViolations } from "./test/a11y";
import { App } from "./App";

// This mock answers everything the tree mounts, not only the gate under test. Rendering `App`
// all the way through mounts the shell, the review queue it opens on, and the hooks each of
// those reads on its own. A gap in any of them renders that subtree's failed-read branch,
// which would leave this file asserting on the wrong screen for the wrong reason.
const { apiMock } = await vi.hoisted(async () => ({
  apiMock: (await import("./test/apiMock")).makeApiMock(),
}));

vi.mock("./api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("./api")>()),
  api: apiMock,
}));

class NoopObserver {
  observe() {}
  unobserve() {}
  disconnect() {}
}

const USER: AuthUser = {
  id: 1,
  username: "owner",
  provider: "local",
  thumb_url: null,
  via_recovery: false,
};

/** A configured install: a password set, everything wired, and scanned once, which is what
 *  `complete` means. */
const COMPLETE_SETUP: SetupStatus = {
  admin_exists: true,
  has_password: true,
  plex_linked: true,
  instances: { radarr: 1, sonarr: 1, tautulli: 1 },
  has_radarr: true,
  has_sonarr: true,
  // Wired, so the row is an install that could actually exist: `scan_ready` and `reap_ready`
  // both require a Tautulli, and a fixture asserting them beside `has_tautulli: false`
  // describes a state the server never returns.
  has_tautulli: true,
  has_seerr: false,
  has_scanned: true,
  scan_ready: true,
  reap_ready: true,
  complete: true,
};

const EMPTY_PAGE: CandidatePage = {
  items: [],
  groups: [],
  total: 0,
  total_bytes: 0,
  unknown_size: 0,
  offset: 0,
  snapshot_id: 1,
};

// The Dashboard's own marker, rendered above the lazy section boundary, so it is on screen
// whichever section is loading. The wizard and the login screen each render neither this nav nor
// each other, which is what makes the three screens tellable apart with one assertion each.
const dashboardNav = () => screen.queryByRole("navigation", { name: "Sections" });
// The wizard's own marker: the progress row appears on every step and nowhere else. Each step
// provides its own way out through its own actions.
const wizardSteps = () => screen.queryByRole("list", { name: "Setup progress" });
const WIZARD_UNREADABLE = /Couldn't check what's set up yet/;
const WIZARD_WAY_OUT = /Go to the app/;

// `App` puts the wizard behind `React.lazy`, so the first render through that boundary in this
// process pays Vite's cold transform of the wizard's whole module graph, inside Testing
// Library's `asyncUtilTimeout`, which the suite's `testTimeout` does not reach. Warming it here
// avoids making which test pays that cost an accident of file order: without this, the next
// wizard test written above this one in the file would inherit the failure. The transform is
// real work and does not belong inside a wait.
beforeAll(async () => {
  await import("./components/SetupWizard");
});

beforeEach(() => {
  apiMock.update.mockResolvedValue(DEFAULT_UPDATE);
  vi.stubGlobal("IntersectionObserver", NoopObserver);
  vi.clearAllMocks();
  apiMock.me.mockResolvedValue(USER);
  apiMock.setupStatus.mockResolvedValue(COMPLETE_SETUP);
  apiMock.safety.mockResolvedValue({ destructive_enabled: false, dry_run: true, reason: null });
  apiMock.latestSnapshot.mockResolvedValue(null);
  apiMock.scanStatus.mockResolvedValue(IDLE_SCAN);
  apiMock.general.mockResolvedValue(DEFAULT_GENERAL);
  apiMock.profile.mockResolvedValue(DEFAULT_PROFILE);
  apiMock.candidates.mockResolvedValue(EMPTY_PAGE);
  apiMock.vocabularyValues.mockResolvedValue({ values: [] });
  apiMock.group.mockResolvedValue(null);
  apiMock.reapBreakdown.mockResolvedValue({ has_snapshot: true, will_reap: 0, condemned_by: [] });
  apiMock.reapStatus.mockResolvedValue({ running: false });
  apiMock.plexTrash.mockResolvedValue({ enabled: false, count: 0 });
  apiMock.fairness.mockResolvedValue({
    total_requests: 0,
    total_reclaimable_bytes: 0,
    total_reclaimable_items: 0,
    not_in_scan: 0,
    unmatched: [],
    no_snapshot: false,
    horizon_at: null,
    rows: [],
  });
  apiMock.authContext.mockResolvedValue({
    setup_needed: false,
    plex_linked: true,
    local_login_available: true,
  });
  apiMock.instances.mockResolvedValue([]);
  // Not linked, so the Plex panel's library grid and Leaving Soon group do not render here at
  // all. Their own stale-read behavior is driven in PlexPanel.test.tsx, where the panel is the
  // subject rather than a passenger.
  apiMock.plexStatus.mockResolvedValue({
    linked: false,
    name: null,
    connection_uri: null,
    last_ok_at: null,
    verify_tls: true,
    web_url: "https://app.plex.tv",
  });
  apiMock.plexResources.mockResolvedValue({ source: "plex.tv", servers: [], owner_username: null });
  apiMock.plexLibraries.mockResolvedValue([]);
  apiMock.watchEvidence.mockResolvedValue(DEFAULT_WATCH_EVIDENCE);
  apiMock.leavingSoonSettings.mockResolvedValue({
    enabled: false,
    allow_unarmed: false,
    name: "Leaving Soon",
    applied_name: "Leaving Soon",
    last: null,
  });
});

function renderApp(): QueryClient {
  const queryClient = testQueryClient();
  renderWithProviders(<App />, { client: queryClient });
  return queryClient;
}

describe("the setup gate through a failed refetch", () => {
  // The only test that mounts the whole shell, so it is the only one that can answer for the
  // landmarks: a screen reader user navigates a page by them, and a panel test cannot see them
  // because they live above the panel. `pageLevel` turns the `region` rule back on here.
  it("has no accessibility violations, landmarks included", async () => {
    renderApp();
    await screen.findByRole("navigation", { name: "Sections" });
    // The audit is itself an await long enough for the shell's remaining reads to land, so it
    // must run inside `act`, or those arrivals become updates outside one.
    await act(async () => {
      await expectNoA11yViolations(document.body, { pageLevel: true });
    });
  });

  it("keeps the Dashboard when a configured install's status read blinks", async () => {
    const queryClient = renderApp();
    expect(await screen.findByRole("navigation", { name: "Sections" })).toBeInTheDocument();

    // Every trigger is an ordinary in-app action that invalidates this key on its SUCCESS path:
    // linking or unlinking Plex, saving a service, saving general settings. So the operator gets
    // this by doing something that worked.
    apiMock.setupStatus.mockRejectedValue(new Error("boom"));
    await queryClient.invalidateQueries({ queryKey: ["setup"] });
    await waitFor(() => expect(apiMock.setupStatus).toHaveBeenCalledTimes(2));

    expect(dashboardNav()).toBeInTheDocument();
    // The claim that matters: the wizard did not come back over a configured install.
    expect(wizardSteps()).toBeNull();
  });

  it("still shows the wizard when the first status read is the one that fails", async () => {
    apiMock.setupStatus.mockRejectedValue(new Error("boom"));
    renderApp();

    // Nothing was ever read, so the install cannot be shown to be configured, and dropping a
    // genuinely-fresh one onto an empty Dashboard with no way back is the worse failure. Skip is
    // the promise that makes routing here safe, so it is asserted rather than assumed.
    expect(await screen.findByText(WIZARD_UNREADABLE)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: WIZARD_WAY_OUT })).toBeEnabled();
    expect(dashboardNav()).toBeNull();
  });

  it("still shows the wizard for an install the status says is unfinished", async () => {
    apiMock.setupStatus.mockResolvedValue({
      ...COMPLETE_SETUP,
      has_scanned: false,
      complete: false,
    });
    renderApp();

    // This half of the check was never in question on its own, but keeping it here means a fix
    // that narrowed the gate to `setup === undefined && isError` would still fail this file.
    expect(await screen.findByRole("list", { name: "Setup progress" })).toBeInTheDocument();
    // The way out is still there on every step past the password, which is what makes
    // routing an unfinished install here safe.
    expect(screen.getByRole("button", { name: WIZARD_WAY_OUT })).toBeEnabled();
    expect(dashboardNav()).toBeNull();
  });
});

describe("the sign-in gate through a failed refetch", () => {
  it("keeps the signed-in app when the ['me'] refetch fails with a user still held", async () => {
    const queryClient = renderApp();
    expect(await screen.findByRole("navigation", { name: "Sections" })).toBeInTheDocument();

    // `signOut.onSettled` refetches this key, so a sign-out that failed on a flaky network can
    // still reach here and sign the operator out of the UI anyway. That is the opposite of what
    // the failure means.
    apiMock.me.mockRejectedValue(new Error("boom"));
    await queryClient.invalidateQueries({ queryKey: ["me"] });
    await waitFor(() => expect(apiMock.me).toHaveBeenCalledTimes(2));

    expect(dashboardNav()).toBeInTheDocument();
    expect(screen.queryByText(/Sign in with Plex/)).toBeNull();
  });

  it("still shows the login screen when the first ['me'] read is the one that fails", async () => {
    apiMock.me.mockRejectedValue(new Error("boom"));
    renderApp();

    expect(await screen.findByText(/Sign in with Plex/)).toBeInTheDocument();
    expect(dashboardNav()).toBeNull();
  });

  it("shows the login screen when a 401 writes the user away", async () => {
    const queryClient = renderApp();
    expect(await screen.findByRole("navigation", { name: "Sections" })).toBeInTheDocument();

    // A session that expired under an ordinary read. `setUnauthorizedHandler` writes
    // `["me"] = null` rather than refetching (main.tsx), because answering a 401 with a request
    // that itself 401s is a loop. So that expiry arrives as data, not as an error, and the gate
    // must still catch it.
    //
    // This covers every 401 except those under `/api/auth/`, which are exempt from that
    // handler as a whole prefix. It stands in for the reads that do write this way; the
    // sign-out test below drives one that does not.
    queryClient.setQueryData(["me"], null);

    expect(await screen.findByText(/Sign in with Plex/)).toBeInTheDocument();
    expect(dashboardNav()).toBeNull();
  });

  it("shows the login screen after a sign-out that worked", async () => {
    const user = userEvent.setup();
    renderApp();
    expect(await screen.findByRole("navigation", { name: "Sections" })).toBeInTheDocument();

    // This is the success path, not a failure: the session is gone server-side, so
    // `/api/auth/me` 401s from here on. That path is exempt from the unauthorized handler
    // (api.ts), so nothing writes `["me"] = null` for it. A refetch simply errors while React
    // Query still holds the user, and the gate reads a held user as signed in. So the sign-out
    // itself has to write the answer, or the operator would stay on their own dashboard until
    // an unrelated poll happened to 401.
    apiMock.logout.mockResolvedValue({ ok: true });
    apiMock.me.mockRejectedValue(new Error("401"));

    await user.click(screen.getByRole("button", { name: /owner/ }));
    const out = screen.getByRole("button", { name: "Sign out" });
    await waitFor(() => expect(out).toBeEnabled());
    await user.click(out);

    expect(await screen.findByText(/Sign in with Plex/)).toBeInTheDocument();
    expect(dashboardNav()).toBeNull();
    // Written, not asked: the read that would have answered a dead session with an error never
    // happens. Without this the test passes on "lands on Login" and says nothing about how.
    expect(apiMock.me).toHaveBeenCalledTimes(1);
  });

  it("stays signed in and asks again when the sign-out fails", async () => {
    const user = userEvent.setup();
    renderApp();
    expect(await screen.findByRole("navigation", { name: "Sections" })).toBeInTheDocument();

    // The other half of the split, and the reason the failure path still invalidates: nothing was
    // revoked, so the session may well still be live and the answer is whatever `/api/auth/me`
    // says. Deleting `signOut.onError` leaves the whole suite green without this.
    apiMock.logout.mockRejectedValue(new Error("the network went away"));

    await user.click(screen.getByRole("button", { name: /owner/ }));
    const out = screen.getByRole("button", { name: "Sign out" });
    await waitFor(() => expect(out).toBeEnabled());
    await user.click(out);

    expect(await screen.findByText("Couldn't sign you out. Try again.")).toBeInTheDocument();
    await waitFor(() => expect(apiMock.me).toHaveBeenCalledTimes(2));
    expect(dashboardNav()).toBeInTheDocument();
  });
});

describe("the wizard's own last step finishing", () => {
  // `complete` is `has_password and scan_ready and has_scanned`, and the wizard's last step is
  // what makes `has_scanned` true. If the gate derives live from that flag, the moment
  // `has_scanned` flips true unmounts the wizard before its own finish panel can render, so the
  // screen that reports the result of setup would never show. The wizard is latched instead:
  // once it has been needed, it stays open until the operator leaves it.
  it("keeps the wizard up when the first scan flips complete", async () => {
    // Everything done but the scan, which is where the wizard opens for this install and the
    // only state the finish panel is written for.
    const FRESH = { ...COMPLETE_SETUP, has_scanned: false, complete: false };
    apiMock.setupStatus.mockResolvedValue(FRESH);
    const client = renderApp();

    await waitFor(() => expect(wizardSteps()).toBeInTheDocument());

    // The scan lands: the step invalidates every key, and the server now calls setup complete.
    apiMock.setupStatus.mockResolvedValue({ ...FRESH, has_scanned: true, complete: true });
    await act(async () => {
      await client.invalidateQueries({ queryKey: ["setup"] });
    });

    expect(wizardSteps()).toBeInTheDocument();
    expect(dashboardNav()).toBeNull();
    // The panel that only this state renders is now on screen. Without the latch, "You're all
    // set" would be unreachable through the app in every state: an empty blocker list implies
    // has_password and scan_ready, which together with has_scanned implies complete, and
    // deriving the gate from that would unmount the panel drawing it.
    expect(await screen.findByText(/you're all set/i)).toBeInTheDocument();
  });

  // The other direction, and the reason the latch is a latch rather than "never unmount": a
  // configured install must still go straight to the Dashboard, and a blinked read must not
  // drag it into the wizard. Both are pinned above; this asserts the latch did not break them.
  it("does not latch an install that never needed the wizard", async () => {
    renderApp();
    expect(await screen.findByRole("navigation", { name: "Sections" })).toBeInTheDocument();
    expect(wizardSteps()).toBeNull();
  });
});
