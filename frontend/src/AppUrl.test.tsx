// SPDX-License-Identifier: AGPL-3.0-or-later
// Every section has a URL, and a cold load lands on the one the URL names.
//
// The app has no router: the URL is written on every nav and read only at mount (navUrl.ts),
// and `backnav` stays the authority for Back. That split is what these tests are mostly about.
// Two of them are the reason it is written that way:
//
//   - a URL write must keep `history.state.__reaperBack`, the marker `backnav` asks the browser
//     for before stepping off an entry. Replacing it with `null` leaves a parked entry that
//     looks like somebody else's, so a Back press either does nothing or walks the operator out
//     of Reaper with a panel still open. It is asserted directly, in the state a real write
//     happens in (a lane change has parked one).
//   - Back still closes an open layer before it moves sections, and still moves sections after.
//
// The pages the shell lands on stand in for themselves, as in `AppFocus.test.tsx`: what each
// one renders is its own tests' subject, and stubbing them keeps four heavy trees out of a file
// about the shell. The review queue is real, because the filters a link carries are its state.
import { act, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import {
  ApiError,
  type AuthUser,
  type Candidate,
  type CandidateQuery,
  type Safety,
  type SetupStatus,
  type Snapshot,
  type Verdict,
} from "./api";
import { DEFAULT_GENERAL, DEFAULT_PROFILE, DEFAULT_UPDATE, IDLE_SCAN } from "./test/apiFixtures";
import { renderWithProviders } from "./test/renderWithProviders";
import { filtersKey } from "./components/queueFilters";
import { App } from "./App";

const { apiMock } = await vi.hoisted(async () => ({
  apiMock: (await import("./test/apiMock")).makeApiMock(),
}));

vi.mock("./api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("./api")>()),
  api: apiMock,
}));

vi.mock("./components/PolicyEditor", () => ({ PolicyEditor: () => <p>policy page</p> }));
vi.mock("./components/Settings", () => ({ Settings: () => <p>settings page</p> }));
vi.mock("./components/ReapPlan", () => ({ ReapPlan: () => <p>reap page</p> }));
vi.mock("./components/Fairness", () => ({ Fairness: () => <p>scales page</p> }));
vi.mock("./components/SetupWizard", () => ({ SetupWizard: () => <p>setup wizard</p> }));

const USER: AuthUser = {
  id: 1,
  username: "owner",
  provider: "local",
  thumb_url: null,
  via_recovery: false,
};

const SAFETY: Safety = {
  destructive_enabled: false,
  has_password: true,
  recovery_mode: false,
  note: null,
};

const SNAPSHOT: Snapshot = {
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

/** One condemned movie, so the queue has a card to open a side panel from. */
const CARD: Candidate = {
  id: 1,
  media_key: "radarr:1:1",
  title: "Example Title",
  media_type: "movie",
  size_bytes: 1024 ** 3,
  verdict: "condemn",
  score: 80,
  coverage_bp: 10_000,
  first_flagged_at: null,
  year: 2011,
  summary: null,
  poster_url: null,
  requested_by: null,
  group_key: null,
  group_title: null,
  video_resolution: null,
  library: null,
  dormant_for: null,
  reason: null,
  override: null,
  override_own: null,
  show_override: null,
  override_effective: null,
  spare_expires_at: null,
  spare_covers_until: null,
  show_spare_expires_at: null,
  chip: null,
  show_status: null,
  season_number: null,
};

const SETUP_DONE: SetupStatus = {
  admin_exists: true,
  has_password: true,
  plex_linked: true,
  instances: {},
  has_radarr: true,
  has_sonarr: true,
  has_tautulli: false,
  has_seerr: false,
  has_scanned: true,
  scan_ready: true,
  reap_ready: true,
  complete: true,
};

beforeEach(() => {
  window.localStorage.clear();
  apiMock.me.mockResolvedValue(USER);
  apiMock.safety.mockResolvedValue(SAFETY);
  apiMock.setupStatus.mockResolvedValue(SETUP_DONE);
  apiMock.scanStatus.mockResolvedValue(IDLE_SCAN);
  apiMock.latestSnapshot.mockResolvedValue(SNAPSHOT);
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
  apiMock.update.mockResolvedValue(DEFAULT_UPDATE);
  apiMock.reapBreakdown.mockResolvedValue({ has_snapshot: true, will_reap: 0, condemned_by: [] });
  apiMock.reapStatus.mockResolvedValue({ running: false });
  // The shell's own read for the Scales panel, made on that section whether or not the page
  // below asks for anything.
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
  // The queue's two filter suggesters, which go through an arrow and so are rule 135's
  // documented blind spot rather than its gate.
  apiMock.vocabularyValues.mockImplementation((field: string) =>
    Promise.resolve({ field, values: [] }),
  );
});

// jsdom carries one session history across a whole file, and a test that ends with back-nav
// layers still open has its entries handed back one deferred task at a time. Those are real
// popstates: arriving after the next test's provider is up, they read as Back presses and eat
// the ones it means to send. Drain them while nothing is listening, then park the app at the URL
// this test means to open on. `setup.ts` has already put the location back to "/".
async function open(path: string) {
  for (let i = 0; i < 10; i++) await new Promise((resolve) => setTimeout(resolve, 0));
  history.replaceState(null, "", path);
  renderWithProviders(<App />);
}

/** A Back press, the way `backnav.test.tsx` drives one: the provider parks a sentinel entry per
 *  layer and listens for the popstate that handing one back produces. */
async function back() {
  await act(async () => {
    window.dispatchEvent(new PopStateEvent("popstate"));
  });
}

/** Let jsdom's deferred history work land before the next press. */
async function settle() {
  await act(async () => {
    await new Promise((resolve) => setTimeout(resolve, 0));
  });
}

/** What the server was last asked for: the lane, and the filters with it.
 *
 *  The one-row probe behind the filtered-empty state ("filters are hiding N") asks for the same
 *  lane with no filters at all, and it lands last. Skipped by its page size, or every assertion
 *  below reads that call instead of the list's. */
const lastAsk = () => {
  const asks = apiMock.candidates.mock.calls.filter((c) => c[2] !== 1);
  const call = asks.at(-1) as [Verdict, CandidateQuery] | undefined;
  return { verdict: call?.[0], query: call?.[1] ?? {} };
};

const here = () => window.location.pathname + window.location.search;
const laneTab = (label: string) => screen.getByRole("button", { name: label });
const queue = () => screen.findByRole("searchbox", { name: /search titles/i });

describe("a cold load", () => {
  // Every section, because a map with a missing entry is exactly the failure this shape is
  // meant to make impossible, and only driving each one proves the map is the one being read
  // (rule 145). Review is in the list too: it is the default, so it is the one that would still
  // pass with the whole feature deleted.
  it.each([
    ["/policy", "policy page"],
    ["/reap", "reap page"],
    ["/scales", "scales page"],
    ["/settings", "settings page"],
  ])("lands on the section %s names", async (path, marker) => {
    await open(path);
    expect(await screen.findByText(marker)).toBeInTheDocument();
  });

  it("lands on the review queue, on the lane the URL names", async () => {
    await open("/review/limbo");
    await queue();
    expect(laneTab("Limbo")).toHaveAttribute("aria-current", "page");
    await waitFor(() => expect(lastAsk().verdict).toBe("abstain"));
  });

  it("opens a link's search and filters, and shows them as chips", async () => {
    await open("/review/condemned?q=alpha&mediaType=movie&genre=Comedy");
    await queue();
    await waitFor(() =>
      expect(lastAsk()).toMatchObject({
        verdict: "condemn",
        query: { search: "alpha", media_type: "movie", genre: "Comedy" },
      }),
    );
    // The list is visibly filtered, so the operator can see it is not the whole lane.
    expect(screen.getByRole("searchbox", { name: /search titles/i })).toHaveValue("alpha");
    expect(screen.getByRole("button", { name: "Remove the Type filter" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Remove the Genre filter" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /stop searching for alpha/i })).toBeInTheDocument();
  });

  it("takes a link's filters over the ones this device remembered", async () => {
    // A link naming any filter brings the whole set. Merging the two would hide rows the
    // sender was looking at, which is the direction that must never happen by accident.
    window.localStorage.setItem(
      filtersKey("condemn"),
      JSON.stringify({ mediaType: "season", requested: "yes", sort: "size", order: "asc" }),
    );
    await open("/review/condemned?genre=Comedy");
    await queue();
    await waitFor(() => expect(lastAsk().query).toMatchObject({ genre: "Comedy" }));
    expect(lastAsk().query).toMatchObject({ media_type: "", requested: "any" });
    // Sort is not a filter and does not travel in a link, so this device keeps its own.
    expect(lastAsk().query).toMatchObject({ sort: "size", order: "asc" });
  });

  it("falls back to what this device remembered when the link names no filter", async () => {
    window.localStorage.setItem(filtersKey("condemn"), JSON.stringify({ requested: "yes" }));
    await open("/review/condemned");
    await queue();
    await waitFor(() => expect(lastAsk().query).toMatchObject({ requested: "yes" }));
  });

  it("opens the app on a mangled URL, and never on a narrower list", async () => {
    // A stale link naming a section that does not exist, a lane that does not exist, and a
    // value outside its vocabulary. Every one of them resolves to showing MORE.
    await open("/rubbish/deeper?requested=banana&genre=Comedy");
    await queue();
    await waitFor(() => expect(lastAsk().query).toMatchObject({ genre: "Comedy" }));
    expect(lastAsk().verdict).toBe("condemn");
    expect(lastAsk().query).toMatchObject({ requested: "any" });
    expect(screen.queryByRole("button", { name: "Remove the Requested filter" })).toBeNull();
    // And the address bar is corrected to where the app actually is.
    await waitFor(() => expect(here()).toBe("/review/condemned?genre=Comedy"));
  });

  it("leaves the recovery path alone", async () => {
    // `/recover` is a path the signed-out app already reads, and no section may take it over or
    // rewrite it: whoever is standing there is locked out of their own install.
    apiMock.me.mockRejectedValue(new ApiError(401, "signed out"));
    apiMock.authContext.mockResolvedValue({
      setup_needed: false,
      plex_linked: true,
      local_login_available: true,
    });
    await open("/recover");
    expect(
      await screen.findByRole("button", { name: /redeem recovery code/i }),
    ).toBeInTheDocument();
    expect(here()).toBe("/recover");
  });

  it("does not let a deep link walk past the setup gate", async () => {
    apiMock.setupStatus.mockResolvedValue({ ...SETUP_DONE, has_scanned: false, complete: false });
    await open("/settings");
    expect(await screen.findByText("setup wizard")).toBeInTheDocument();
    expect(screen.queryByText("settings page")).toBeNull();
    expect(screen.queryByRole("navigation", { name: "Sections" })).toBeNull();
  });
});

describe("the address bar", () => {
  it("follows every nav, and takes no history entry per keystroke", async () => {
    const person = userEvent.setup();
    await open("/review/condemned");
    await queue();
    await waitFor(() => expect(here()).toBe("/review/condemned"));

    await settle();
    await person.click(screen.getByRole("button", { name: "Policy" }));
    await screen.findByText("policy page");
    expect(here()).toBe("/policy");

    await settle();
    await person.click(screen.getByRole("button", { name: "Review" }));
    await queue();
    expect(here()).toBe("/review/condemned");

    await settle();
    await person.click(laneTab("Limbo"));
    await waitFor(() => expect(here()).toBe("/review/limbo"));

    // Typing is a filter change: it replaces the entry rather than adding one, or a search
    // term would cost as many Back presses as it has letters.
    const entries = history.length;
    await person.type(await queue(), "alpha");
    await waitFor(() => expect(here()).toBe("/review/limbo?q=alpha"));
    expect(history.length).toBe(entries);
  });

  it("keeps the Back sentinel when it writes, and Back still unwinds layers before sections", async () => {
    const person = userEvent.setup();
    await open("/review/condemned");
    await queue();

    // A lane change parks an entry, so from here every URL write lands on one of `backnav`'s.
    await settle();
    await person.click(laneTab("Limbo"));
    await waitFor(() => expect(here()).toBe("/review/limbo"));
    expect((history.state as { __reaperBack?: boolean } | null)?.__reaperBack).toBe(true);

    // The write that would break it: `replaceState(null, …)` here wipes the marker `backnav`
    // reads before it steps, and the two Back presses below stop working.
    await person.type(await queue(), "alpha");
    await waitFor(() => expect(here()).toBe("/review/limbo?q=alpha"));
    expect((history.state as { __reaperBack?: boolean } | null)?.__reaperBack).toBe(true);

    // An open layer goes first. The filter menu is the queue's own, registered with `backnav`
    // while it is open.
    await settle();
    await person.click(screen.getByRole("button", { name: /^filter$/i }));
    const menu = await screen.findByRole("list", { name: "Add a filter" });
    expect(within(menu).getByText("Type")).toBeInTheDocument();
    await back();
    await waitFor(() => expect(screen.queryByRole("list", { name: "Add a filter" })).toBeNull());
    // ...and the section did not move with it.
    expect(laneTab("Limbo")).toHaveAttribute("aria-current", "page");

    // Only then the lane, restored by the nav frame the tab click recorded.
    await settle();
    await back();
    await waitFor(() => expect(laneTab("Condemned")).toHaveAttribute("aria-current", "page"));
    await waitFor(() => expect(here()).toBe("/review/condemned?q=alpha"));
  });

  it("keeps the filters in it when a panel closes onto the entry underneath", async () => {
    // A real Back, not the synthetic popstate above, because the browser's own half is the
    // subject: an open panel parks its own entry, every write lands on that newest entry, and
    // stepping off it restores the URL the entry underneath was parked with. Anything filtered
    // while the panel was open is missing from that older URL, so the address bar would claim a
    // list the queue is not showing.
    const person = userEvent.setup();
    apiMock.candidate.mockRejectedValue(new Error("the panel's contents are not this test's"));
    apiMock.candidates.mockResolvedValue({
      items: [CARD],
      groups: [],
      total: 1,
      total_bytes: CARD.size_bytes ?? 0,
      unknown_size: 0,
      offset: 0,
      snapshot_id: 1,
    });
    await open("/review/condemned");
    await queue();

    await person.click(await screen.findByRole("button", { name: /^why example title scored/i }));
    const panel = await screen.findByText(/couldn't load the reasons for this item/i);
    await person.type(await queue(), "alpha");
    await waitFor(() => expect(here()).toBe("/review/condemned?q=alpha"));

    await act(async () => {
      history.back();
      await new Promise((resolve) => setTimeout(resolve, 0));
    });
    await waitFor(() => expect(panel).not.toBeInTheDocument());
    expect(here()).toBe("/review/condemned?q=alpha");
  });

  it("keeps the filters in it when a panel closes by its own control", async () => {
    // The other half, and the one that was broken. Above, the browser traverses FIRST and React
    // renders after, so the queue's effect rewrites the URL on its way past. Here the app closes
    // the panel itself, `backnav` gives the entry back with its own `history.back()`, and that
    // traversal lands AFTER every passive effect has run -- so nothing renders afterwards and no
    // effect, with or without a dependency list, can put the address bar back. `navUrl.
    // reassertUrl`, called from `backnav`'s self-pop branch, is what does. Deleting that call
    // turns this red and leaves the test above green.
    const person = userEvent.setup();
    apiMock.candidate.mockRejectedValue(new Error("the panel's contents are not this test's"));
    apiMock.candidates.mockResolvedValue({
      items: [CARD],
      groups: [],
      total: 1,
      total_bytes: CARD.size_bytes ?? 0,
      unknown_size: 0,
      offset: 0,
      snapshot_id: 1,
    });
    await open("/review/condemned");
    await queue();

    await person.click(await screen.findByRole("button", { name: /^why example title scored/i }));
    const panel = await screen.findByText(/couldn't load the reasons for this item/i);
    await person.type(await queue(), "alpha");
    await waitFor(() => expect(here()).toBe("/review/condemned?q=alpha"));
    // The panel's entry is parked and the search was written onto it. Both facts matter: the
    // marker is what lets `backnav` step off this entry at all, and a write that dropped it
    // would stop the close below from working rather than only losing the URL.
    expect((history.state as { __reaperBack?: boolean } | null)?.__reaperBack).toBe(true);

    // Closed from inside the app, which is every route but a Back press. `backnav` unparks the
    // entry the URL was written onto and steps off it itself.
    await person.click(screen.getByRole("button", { name: "Close" }));
    await waitFor(() => expect(panel).not.toBeInTheDocument());
    await settle();
    expect(here()).toBe("/review/condemned?q=alpha");
  });
});
