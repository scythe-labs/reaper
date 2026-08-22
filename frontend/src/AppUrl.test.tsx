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
// Two of the five sections have sub-navigation of their own, and the URL names where you are
// inside them: `/settings/logs`, `/policy/tv/deletion`. Policy takes two segments because Movies
// and TV are separate policies, and a URL naming only the section reopened the right section with
// the other one's caps, budget and weights on it. `App` owns all three values, which is what lets
// one effect write them, so each is driven here from the address bar down to the page and back.
//
// The pages the shell lands on stand in for themselves, as in `AppFocus.test.tsx`: what each
// one renders is its own tests' subject, and stubbing them keeps the heavy trees out of a file
// about the shell. The review queue is real, because the filters a link carries are its state.
// **Settings is real too**, because its panel is the claim: "opens on Logs" is about the panel
// on screen, and a stub that printed the prop it was handed would assert the shell against
// itself. Its rail is also where the unsaved-edits confirm sits, which the lifted panel has to
// keep.
//
// **The policy editor is the one that stays stubbed, and its rail is why.** Which section is
// current there is measured from the scroll position of four headings, and jsdom has no layout:
// every rect is 0 and the document reports itself as too short to scroll, so the real rail
// answers "the first section" whatever it is handed. The stub takes the section and reports one
// back, which is `App`'s half of the loop; the editor's half is `PolicyEditor.test.tsx`, where a
// rail click is driven through the real component.
import { act, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeAll, beforeEach, describe, expect, it, vi } from "vitest";
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
import { fill } from "./test/forms";
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

vi.mock("./components/PolicyEditor", () => ({
  PolicyEditor: ({
    mediaType,
    onMediaTypeChange,
    section,
    onSectionChange,
  }: {
    mediaType: "movie" | "tv";
    onMediaTypeChange: (next: "movie" | "tv") => void;
    section: string;
    onSectionChange: (next: string) => void;
  }) => (
    <>
      <p>
        policy page, {mediaType}, at {section}
      </p>
      {/* The rail and the Movies/TV switch, in the one shape jsdom can drive: a click that
          reports one half of the location upward. The switch names where it goes, so a click
          on it cannot be confused for a click on the state it left. */}
      <button type="button" onClick={() => onSectionChange("pace")}>
        rail: Pace and limits
      </button>
      <button type="button" onClick={() => onMediaTypeChange(mediaType === "tv" ? "movie" : "tv")}>
        switch: {mediaType === "tv" ? "Movies" : "TV"}
      </button>
    </>
  ),
}));
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
  degraded_doc: null,
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

// `App` puts Settings behind `React.lazy`, so the first render through that boundary in this
// process pays Vite's cold transform of ten panels' worth of modules -- inside Testing Library's
// `asyncUtilTimeout`, which `testTimeout` does not reach. Warmed the way
// `AppStaleRead.test.tsx` warms the wizard, rather than by lengthening one assertion's timeout:
// which test pays the cost is otherwise an accident of file order (#651). The budget itself was
// 1000ms then and is 5000ms now (`src/test/setup.ts`, #887), which is headroom for the whole
// suite and not a reason to stop warming: the transform is real work, and paying it here keeps
// it out of a wait either way.
beforeAll(async () => {
  await import("./components/Settings");
});

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
  apiMock.saveGeneral.mockResolvedValue(DEFAULT_GENERAL);
  // The two settings panels this file opens. Settings is the real component here, so the panel
  // it lands on does its own reads, and an unanswered one renders a failed-read branch with no
  // rail tab to leave by.
  apiMock.logs.mockResolvedValue({ lines: [], last_seq: 0, level: "INFO", files_kept: 3 });
  apiMock.about.mockResolvedValue({
    version: "0.0.0-test",
    license: "AGPL-3.0-or-later",
    data_dir: "/data",
    reaper_db_bytes: 1024,
    cache_db_bytes: 1024,
  });
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
    // A bare section still works where the section has sub-navigation: it lands on the default
    // of every value below it.
    ["/policy", "policy page, movie, at flags"],
    ["/reap", "reap page"],
    ["/scales", "scales page"],
    // Settings is real, so its marker is a rail tab: the section is on screen, on the panel a
    // link that names none opens.
    ["/settings", "Backup & Restore"],
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

  // Both sections with sub-navigation, every panel driven off the same two declarations the
  // writer reads (`PANEL_PATHS`, `POLICY_PATHS` in navUrl.ts). A panel added without a path
  // does not compile; one whose path is wrong lands on the default, which is what these catch.
  it.each([
    ["/settings/logs", "Logs"],
    ["/settings/about", "About"],
    // Hand-edited, and it opens the section rather than throwing. General is where a link
    // naming no panel lands too, so the address bar is checked below rather than here.
    ["/settings/nonsense", "General"],
  ])("opens Settings on the panel %s names", async (path, heading) => {
    await open(path);
    expect(await screen.findByRole("heading", { name: heading })).toBeInTheDocument();
  });

  it("corrects the address bar when a link names a panel that does not exist", async () => {
    await open("/settings/nonsense");
    await screen.findByRole("heading", { name: "General" });
    await waitFor(() => expect(here()).toBe("/settings/general"));
  });

  // Both halves of the policy page, every member of both declarations driven (`MEDIA_PATHS`,
  // `POLICY_PATHS`), and each media type with two different sections so the two segments cannot
  // be read in the wrong order and still pass.
  it.each([
    ["/policy/movies/flags", "movie", "flags"],
    ["/policy/movies/kept", "movie", "kept"],
    ["/policy/tv/pace", "tv", "pace"],
    ["/policy/tv/deletion", "tv", "deletion"],
    // Each value validates on its own, so a hand-edited link keeps the half it got right.
    ["/policy/nonsense/deletion", "movie", "deletion"],
    ["/policy/tv/nonsense", "tv", "flags"],
  ])("opens the policy editor where %s says", async (path, media, section) => {
    await open(path);
    expect(await screen.findByText(`policy page, ${media}, at ${section}`)).toBeInTheDocument();
  });

  it("corrects the address bar when a link names a policy that does not exist", async () => {
    await open("/policy/nonsense/nonsense");
    await screen.findByText("policy page, movie, at flags");
    await waitFor(() => expect(here()).toBe("/policy/movies/flags"));
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
    await open("/settings/logs");
    expect(await screen.findByText("setup wizard")).toBeInTheDocument();
    expect(screen.queryByText("Backup & Restore")).toBeNull();
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
    await screen.findByText("policy page, movie, at flags");
    expect(here()).toBe("/policy/movies/flags");

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

  it("follows a click between the panels inside a section", async () => {
    const person = userEvent.setup();
    await open("/settings/general");
    await screen.findByRole("heading", { name: "General" });

    await settle();
    await person.click(screen.getByRole("button", { name: "Logs" }));
    expect(await screen.findByRole("heading", { name: "Logs" })).toBeInTheDocument();
    await waitFor(() => expect(here()).toBe("/settings/logs"));

    // The policy rail, through the stub: what `App` does with a section reported upward is the
    // half that lives here.
    await settle();
    await person.click(screen.getByRole("button", { name: "Policy" }));
    await screen.findByText("policy page, movie, at flags");
    await person.click(screen.getByRole("button", { name: "rail: Pace and limits" }));
    expect(await screen.findByText("policy page, movie, at pace")).toBeInTheDocument();
    await waitFor(() => expect(here()).toBe("/policy/movies/pace"));
  });

  it("moves one half of the policy page at a time", async () => {
    // The two values are independent, and that is the whole finding: the Movies/TV switch used
    // to live in the editor, so a reload restored the section and reset the policy under it to
    // Movies. Every number on the page is the other one's.
    const person = userEvent.setup();
    await open("/policy/movies/kept");
    await screen.findByText("policy page, movie, at kept");
    await waitFor(() => expect(here()).toBe("/policy/movies/kept"));

    await settle();
    await person.click(screen.getByRole("button", { name: "switch: TV" }));
    expect(await screen.findByText("policy page, tv, at kept")).toBeInTheDocument();
    await waitFor(() => expect(here()).toBe("/policy/tv/kept"));

    await settle();
    await person.click(screen.getByRole("button", { name: "rail: Pace and limits" }));
    expect(await screen.findByText("policy page, tv, at pace")).toBeInTheDocument();
    await waitFor(() => expect(here()).toBe("/policy/tv/pace"));
  });

  it("waits for the unsaved-edits confirm before it names the new panel", async () => {
    // The panel moved out of `Settings` and into `App`, and this is what that must not cost: a
    // rail click still runs the confirm, and neither the panel nor the address bar moves until
    // the operator says the draft can go.
    const person = userEvent.setup();
    await open("/settings/general");
    const url = await screen.findByLabelText("Application URL");
    await fill(person, url, "https://reaper.example.com");
    await waitFor(() => expect(document.querySelector(".savebar")).not.toBeNull());

    await person.click(screen.getByRole("button", { name: "Logs" }));
    expect(document.querySelector(".notice-warn")!.textContent).toContain(
      "You have unsaved General settings. Switching to Logs discards them.",
    );
    expect(screen.getByRole("heading", { name: "General" })).toBeInTheDocument();
    expect(here()).toBe("/settings/general");

    await person.click(screen.getByRole("button", { name: "Discard and switch" }));
    expect(await screen.findByRole("heading", { name: "Logs" })).toBeInTheDocument();
    await waitFor(() => expect(here()).toBe("/settings/logs"));
  });

  it("names the panel a jump lands on, and the one Back comes back to", async () => {
    // A panel is a place now, not a one-shot aim (navIntent.ts): Back into Settings lands on the
    // panel it was left on, the way Back into Review lands on the lane it was left on. The user
    // menu's update item is the app's one always-mounted jump into Settings.
    apiMock.update.mockResolvedValue({
      ...DEFAULT_UPDATE,
      update_available: true,
      latest: "0.2.0",
    });
    const person = userEvent.setup();
    await open("/review/condemned");
    await queue();

    await settle();
    await person.click(screen.getByRole("button", { name: /owner, update available/i }));
    await person.click(await screen.findByRole("button", { name: /^update available$/i }));
    await screen.findByRole("heading", { name: "About" });
    await waitFor(() => expect(here()).toBe("/settings/about"));

    await settle();
    await person.click(screen.getByRole("button", { name: "Review" }));
    await queue();

    await settle();
    await back();
    expect(await screen.findByRole("heading", { name: "About" })).toBeInTheDocument();
    await waitFor(() => expect(here()).toBe("/settings/about"));
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
    // The other close route: the app closes the panel itself and `backnav` gives the entry back
    // with its own `history.back()`, rather than the operator's Back doing it. Neither route
    // re-renders anything that writes a URL, so `navUrl.reassertUrl` is what carries the address
    // bar across both -- called unconditionally at the top of `backnav`'s `onPop`, above the
    // self-pop check, because the app's own steps and the operator's revert it alike. Deleting
    // that call turns BOTH this test and the one above red.
    //
    // The pair is worth keeping even so: they enter `onPop` by different doors (`selfPopRef` set
    // and unset), and a fix restricted to one of them passes the other. That was the first
    // attempt at this, and the test above is what caught it.
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
