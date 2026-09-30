// A title that leaves the newest scan closes its side panel. A refetch that fails for any other
// reason keeps the panel and its last data.
import { act, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import {
  ApiError,
  type AuthUser,
  type CandidateDetail,
  type Group,
  type Candidate,
  type Safety,
  type SetupStatus,
  type Snapshot,
} from "./api";
import {
  DEFAULT_GENERAL,
  DEFAULT_PROFILE,
  DEFAULT_RECYCLE_BINS_OFF,
  DEFAULT_UPDATE,
  IDLE_SCAN,
} from "./test/apiFixtures";
import { renderWithProviders } from "./test/renderWithProviders";
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
      {/* The rail and the Movies/TV switch, in the one shape jsdom can drive, a click that
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
  dormant_days: null,
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
  collections: null,
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
// process pays Vite's cold transform of ten panels' worth of modules, which can exceed Testing
// Library's `asyncUtilTimeout` even where `testTimeout` is longer. This warms it the same way
// `AppStaleRead.test.tsx` warms the wizard, rather than lengthening one assertion's timeout,
// since which test pays the cost would otherwise depend on file order. The test timeout is
// 5000ms (`src/test/setup.ts`), which is headroom for the whole suite, not a reason to stop
// warming here. The transform is real work, and paying it here keeps it out of a wait either way.
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
  apiMock.recycleBinsOff.mockResolvedValue(DEFAULT_RECYCLE_BINS_OFF);
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
  // The queue's two filter suggesters go through an arrow function, so an unanswered mock here
  // would not be caught by the usual check for unanswered mocks.
  apiMock.vocabularyValues.mockImplementation((field: string) =>
    Promise.resolve({ field, values: [] }),
  );
});

const GROUP_KEY = "sonarr:show:1";
const SEASON: Candidate = {
  ...CARD,
  id: 7,
  media_key: "sonarr:1:1:1",
  title: "Example Show",
  media_type: "season",
  group_key: GROUP_KEY,
  group_title: "Example Show",
  season_number: 1,
};

const NO_LINKS = {
  plex: null,
  tautulli: null,
  seerr: null,
  radarr: null,
  sonarr: null,
  imdb: null,
  tmdb: null,
  rotten_tomatoes: null,
  trakt: null,
  match_candidates: [],
};

const GROUP: Group = {
  group_key: GROUP_KEY,
  title: "Example Show",
  year: 2011,
  poster_url: null,
  summary: null,
  size_bytes: SEASON.size_bytes ?? 0,
  unknown_size_seasons: 0,
  library: null,
  chip: null,
  show_override: null,
  show_spare_expires_at: null,
  links: NO_LINKS,
  show_status: "ended",
  seasons: [SEASON],
};

function itemDetail(over: Partial<CandidateDetail> = {}): CandidateDetail {
  return {
    ...CARD,
    content_rating: null,
    runtime_minutes: null,
    genres: [],
    ratings: null,
    links: NO_LINKS,
    in_latest_scan: true,
    explanation: {
      score: 80,
      base_score: 80,
      keep_discount: 0,
      threshold: 70,
      coverage: 10_000,
      coverage_floor_bp: null,
      watch_blind: null,
      signals: [],
      protections_fired: [],
      protections_checked: [],
      protections_unknown: [],
      match: null,
    },
    ...over,
  };
}

function queuePage(items: Candidate[]) {
  return {
    items,
    groups: items.some((c) => c.group_key)
      ? [
          {
            group_key: GROUP_KEY,
            condemned_count: 1,
            condemned_bytes: SEASON.size_bytes ?? 0,
            unknown_size: 0,
            seasons: [
              {
                id: SEASON.id,
                season: 1,
                verdict: "condemn" as const,
                override: null,
                override_effective: null,
                size_bytes: SEASON.size_bytes,
              },
            ],
          },
        ]
      : [],
    total: items.length,
    total_bytes: 0,
    unknown_size: 0,
    offset: 0,
    snapshot_id: 1,
  };
}

/** Lets a settled refetch reach the render before a "stays open" claim is read. */
const settled = () =>
  act(async () => {
    await new Promise((resolve) => setTimeout(resolve, 50));
  });

async function openApp() {
  history.replaceState(null, "", "/review/condemned");
  return renderWithProviders(<App />);
}

describe("the show panel", () => {
  beforeEach(() => {
    apiMock.candidates.mockResolvedValue(queuePage([SEASON]));
    apiMock.group.mockResolvedValue(GROUP);
  });

  async function openShow() {
    const view = await openApp();
    await userEvent.setup().click(await screen.findByRole("button", { name: /example show/i }));
    await screen.findByRole("button", { name: "Close" });
    return view;
  }

  it("closes when a refetch says the show left the scan", async () => {
    const { client } = await openShow();
    apiMock.group.mockRejectedValue(new ApiError(404, "gone", "error.review.show_not_in_scan"));
    await client.invalidateQueries({ queryKey: ["group"] });
    await waitFor(() => expect(screen.queryByRole("button", { name: "Close" })).toBeNull());
  });

  it("stays open with its data when a refetch fails on the network", async () => {
    const { client } = await openShow();
    apiMock.group.mockRejectedValue(new ApiError(0, "offline", "error.transport.network"));
    await client.invalidateQueries({ queryKey: ["group"] });
    await waitFor(() => expect(apiMock.group.mock.calls.length).toBeGreaterThan(1));
    await settled();
    expect(screen.getByRole("button", { name: "Close" })).toBeInTheDocument();
  });
});

describe("the item panel", () => {
  beforeEach(() => {
    apiMock.candidates.mockResolvedValue(queuePage([CARD]));
    apiMock.candidate.mockResolvedValue(itemDetail());
  });

  async function openItem() {
    const view = await openApp();
    await userEvent
      .setup()
      .click(await screen.findByRole("button", { name: /^why example title scored/i }));
    await screen.findByRole("button", { name: "Close" });
    return view;
  }

  it("closes when a new scan no longer holds the item", async () => {
    const { client } = await openItem();
    apiMock.candidate.mockResolvedValue(itemDetail({ in_latest_scan: false }));
    await client.invalidateQueries({ queryKey: ["candidate"] });
    await waitFor(() => expect(screen.queryByRole("button", { name: "Close" })).toBeNull());
  });

  it("stays open when the new scan still holds the item", async () => {
    const { client } = await openItem();
    await client.invalidateQueries({ queryKey: ["candidate"] });
    await waitFor(() => expect(apiMock.candidate.mock.calls.length).toBeGreaterThan(1));
    await settled();
    expect(screen.getByRole("button", { name: "Close" })).toBeInTheDocument();
  });
});
