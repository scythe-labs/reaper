// SPDX-License-Identifier: AGPL-3.0-or-later
// The two gates in `App` that sit above the whole signed-in UI, driven through a failed refetch.
//
// Both used to test `isError` without asking whether the value was still in hand. React Query
// keeps the last good data through a failed refetch and raises `isError` beside it, so on a
// configured install a blinked `["setup"]` read rendered the setup wizard over the Dashboard, and
// a blinked `["me"]` read rendered the login screen at a signed-in operator (#181). The same
// divided test #140 made in the settings panels, at the two places whose blast radius is the whole
// app: everything below unmounts, so Settings' unsaved-edits guard never runs (rule 146).
//
// Each gate is pinned in BOTH directions, because a fix that simply deleted the `isError` arm
// would pass a one-sided test while dropping a fresh install onto an empty Dashboard with no way
// back to the wizard. The never-loaded case is the reason those arms were written.
import { QueryClientProvider } from "@tanstack/react-query";
import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { QueryClient } from "@tanstack/react-query";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { AuthUser, CandidatePage, SetupStatus } from "./api";
import {
  DEFAULT_GENERAL,
  DEFAULT_PROFILE,
  DEFAULT_WATCH_EVIDENCE,
  IDLE_SCAN,
} from "./test/apiFixtures";
import { testQueryClient } from "./test/queryClient";
import { expectNoA11yViolations } from "./test/a11y";
import { App } from "./App";

// Rule 135: the mock answers everything the tree mounts, not only the gate under test. Rendering
// `App` all the way through means the shell, the review queue it opens on, and the hooks each of
// those reads on its own -- a gap in any of them renders that subtree's failed-read branch and
// would leave this file asserting on the wrong screen for the wrong reason.
const { apiMock } = vi.hoisted(() => ({
  apiMock: {
    me: vi.fn(),
    setupStatus: vi.fn(),
    safety: vi.fn(),
    latestSnapshot: vi.fn(),
    scanStatus: vi.fn(),
    general: vi.fn(),
    profile: vi.fn(),
    candidates: vi.fn(),
    vocabularyValues: vi.fn(),
    group: vi.fn(),
    reapBreakdown: vi.fn(),
    reapStatus: vi.fn(),
    plexTrash: vi.fn(),
    fairness: vi.fn(),
    authContext: vi.fn(),
    instances: vi.fn(),
    plexStatus: vi.fn(),
    plexResources: vi.fn(),
    plexLibraries: vi.fn(),
    watchEvidence: vi.fn(),
    leavingSoonSettings: vi.fn(),
    // Mutations. Never called on a render, but a missing name is still a hole in the module.
    logout: vi.fn(),
    override: vi.fn(),
    clearOverride: vi.fn(),
    createRun: vi.fn(),
    startScan: vi.fn(),
    stopRun: vi.fn(),
  },
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
  email: null,
  thumb_url: null,
};

/** A configured install: everything wired and scanned once, which is what `complete` means. */
const COMPLETE_SETUP: SetupStatus = {
  admin_exists: true,
  plex_linked: true,
  instances: { radarr: 1, sonarr: 1 },
  has_radarr: true,
  has_sonarr: true,
  has_tautulli: false,
  has_seerr: false,
  has_scanned: true,
  scan_ready: true,
  complete: true,
};

const EMPTY_PAGE: CandidatePage = {
  items: [],
  total: 0,
  totalBytes: 0,
  unknownSize: 0,
  offset: 0,
  snapshotId: 1,
};

// The Dashboard's own marker, rendered above the lazy section boundary, so it is on screen
// whichever section is loading. The wizard and the login screen each render neither this nav nor
// each other, which is what makes the three screens tellable apart with one assertion each.
const dashboardNav = () => screen.queryByRole("navigation", { name: "Sections" });
const WIZARD_SKIP = /Skip to the app/;
const WIZARD_UNREADABLE = /Couldn't check the setup state/;

beforeEach(() => {
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
    last: null,
  });
});

function renderApp(): QueryClient {
  const queryClient = testQueryClient();
  render(
    <QueryClientProvider client={queryClient}>
      <App />
    </QueryClientProvider>,
  );
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
    // runs inside `act` or those arrivals are updates outside one (rule 136).
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
    expect(screen.queryByText(WIZARD_SKIP)).toBeNull();
  });

  it("still shows the wizard when the first status read is the one that fails", async () => {
    apiMock.setupStatus.mockRejectedValue(new Error("boom"));
    renderApp();

    // Nothing was ever read, so the install cannot be shown to be configured, and dropping a
    // genuinely-fresh one onto an empty Dashboard with no way back is the worse failure. Skip is
    // the promise that makes routing here safe, so it is asserted rather than assumed.
    expect(await screen.findByText(WIZARD_UNREADABLE)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: WIZARD_SKIP })).toBeEnabled();
    expect(dashboardNav()).toBeNull();
  });

  it("still shows the wizard for an install the status says is unfinished", async () => {
    apiMock.setupStatus.mockResolvedValue({
      ...COMPLETE_SETUP,
      has_scanned: false,
      complete: false,
    });
    renderApp();

    // The half of the old expression that was never in question, kept so a fix that reduced the
    // gate to `setup === undefined && isError` cannot pass this file (rule 118).
    expect(await screen.findByRole("button", { name: WIZARD_SKIP })).toBeEnabled();
    expect(dashboardNav()).toBeNull();
  });
});

describe("the sign-in gate through a failed refetch", () => {
  it("keeps the signed-in app when the ['me'] refetch fails with a user still held", async () => {
    const queryClient = renderApp();
    expect(await screen.findByRole("navigation", { name: "Sections" })).toBeInTheDocument();

    // `signOut.onSettled` refetches this key, so a sign-out that failed on a flaky network reached
    // here and signed the operator out of the UI anyway -- the opposite of what that failure means.
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
    // that 401s is a loop -- so that expiry arrives as DATA, not as an error, and the gate must
    // still catch it. This is the case the deleted `isError ||` arm was mistaken for.
    //
    // It is every 401 except those under `/api/auth/`, exempt from that handler as a whole prefix,
    // so this stands in for the reads that do write and the sign-out below drives one that does
    // not.
    queryClient.setQueryData(["me"], null);

    expect(await screen.findByText(/Sign in with Plex/)).toBeInTheDocument();
    expect(dashboardNav()).toBeNull();
  });

  it("shows the login screen after a sign-out that worked", async () => {
    const user = userEvent.setup();
    renderApp();
    expect(await screen.findByRole("navigation", { name: "Sections" })).toBeInTheDocument();

    // The success path, not a failure: the session is gone server-side, so `/api/auth/me` 401s
    // from here on. That path is exempt from the unauthorized handler (api.ts), so nothing writes
    // `["me"] = null` for it -- a refetch simply errors while React Query still holds the user,
    // and the gate reads a held user as signed in. Asking left the operator on their own
    // dashboard until an unrelated poll happened to 401, so the sign-out writes the answer.
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
