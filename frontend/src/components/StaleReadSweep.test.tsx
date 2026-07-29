// SPDX-License-Identifier: AGPL-3.0-or-later
// The surfaces #190 found still testing a bare `isError`, one describe each. React Query keeps
// the last good value through a failed refetch and raises the failure beside it, so a branch that
// asks only "did the read fail" either REPLACES content the app is still holding or CONTRADICTS
// content it is still drawing. Both shapes are here.
//
// Every one is pinned in BOTH directions, which is the whole point: the never-loaded arm is the
// reason these branches exist, and a "fix" that showed the stale line in both cases -- or that
// simply deleted the `isError` test -- passes a one-sided test while breaking the case the branch
// was written for (#140, #166, rule 118). So each surface gets two its: one that blinks a
// successful read and demands the content survive, one that fails the FIRST read and demands the
// failure sentence.
//
// `AppStaleRead.test.tsx`, `SettingsStaleRead.test.tsx` and the describe appended to
// `PlexPanel.test.tsx` are the same pass over the setup gates and the settings panels.
import { QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import type { QueryClient } from "@tanstack/react-query";
import userEvent from "@testing-library/user-event";
import type { ReactElement } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type {
  Candidate,
  FairnessReport,
  Group,
  Instance,
  PlexLibrary,
  RootFolder,
  SeerrService,
  UnmatchedRequest,
  Vocabulary,
} from "../api";
import { DEFAULT_GENERAL, DEFAULT_PROFILE, IDLE_SCAN, seedSettings } from "../test/apiFixtures";
import { testQueryClient } from "../test/queryClient";
import { Fairness } from "./Fairness";
import { NotInScanPanel } from "./NotInScanPanel";
import { RemoveRulesEditor } from "./PolicyRuleEditors";
import { ReviewQueue } from "./ReviewQueue";
import { ServiceModal } from "./ServiceModal";
import { ServicesPanel } from "./Settings";

const { apiMock } = vi.hoisted(() => ({
  apiMock: {
    candidates: vi.fn(),
    fairness: vi.fn(),
    general: vi.fn(),
    group: vi.fn(),
    instanceRootFolders: vi.fn(),
    instanceSeerrServices: vi.fn(),
    instances: vi.fn(),
    plexLibraries: vi.fn(),
    profile: vi.fn(),
    scanStatus: vi.fn(),
    updateInstance: vi.fn(),
    vocabulary: vi.fn(),
    vocabularyValues: vi.fn(),
  },
}));

vi.mock("../api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../api")>()),
  api: apiMock,
}));

// Rule 135: the reads each tree makes that no test here names -- the unmeasured allowance, the
// queue's expand/spare preferences, and the filter menus' seen-values call, which goes through an
// arrow and so cannot trip the missing-queryFn gate.
beforeEach(() => {
  vi.clearAllMocks();
  apiMock.profile.mockResolvedValue(DEFAULT_PROFILE);
  apiMock.general.mockResolvedValue(DEFAULT_GENERAL);
  apiMock.scanStatus.mockResolvedValue(IDLE_SCAN);
  apiMock.vocabularyValues.mockResolvedValue({ field: "", values: [] });
});

/** The shared sentence with any noun in it. `what` is the one thing a caller varies, so each
 *  surface below also asserts its own noun: the loose form cannot tell a supplied noun from the
 *  component's default, and deleting `what` would leave a loose assertion green (rule 141). */
const STALE_ANY = /Couldn't check .* just now/;

// Rule 144: the sentence is written once, in StaleReadNotice.tsx. Naming the siblings in the
// failure message costs one line, where a comment asking a future author to remember does nothing.
const WHAT_HINT =
  "The stale line is StaleReadNotice.tsx, which owns the wording; `what` is the noun each " +
  "caller supplies. Callers added by #190: NotInScanPanel (the last scan), Fairness (Scales), " +
  "ServicesPanel (your connections), ServiceModal (this instance's folders, your Plex " +
  "libraries, this portal's services, your Sonarr and Radarr connections), ReviewQueue (the " +
  "queue) and its season list, which uses `staleReadLine` because rule 42 keeps `.notice` out " +
  "of the review surfaces. The settings panels are pinned in SettingsStaleRead.test.tsx and " +
  "PlexPanel.test.tsx.";

function renderWithClient(ui: ReactElement): QueryClient {
  const client = seedSettings(testQueryClient());
  render(<QueryClientProvider client={client}>{ui}</QueryClientProvider>);
  return client;
}

/** Rejects the next call of `fn`, invalidates `key`, and waits for the refetch to have landed.
 *  Waiting on the call count rather than on the DOM keeps the assertions that follow from
 *  passing before the failure has arrived (rule 137). */
async function blink(
  client: QueryClient,
  fn: { mock: { calls: unknown[] } } & ReturnType<typeof vi.fn>,
  key: unknown[],
): Promise<void> {
  const before = fn.mock.calls.length;
  fn.mockRejectedValue(new Error("boom"));
  await client.invalidateQueries({ queryKey: key });
  await waitFor(() => expect(fn.mock.calls.length).toBeGreaterThan(before));
}

// --- the not-in-the-last-scan panel -------------------------------------------------------
//
// Props, not a query: `items` arrives from the parent as `fairnessReport?.unmatched ?? []`, so
// the held list is on screen while `error` is true. This one takes no harness at all, which is
// why it is first.

const UNMATCHED: UnmatchedRequest[] = [
  {
    title: "Harbor Lights",
    year: 2011,
    media_type: "movie",
    is_4k: false,
    requested_at: null,
    available_at: null,
    reason: "after_scan",
    request_count: 2,
    requested_by: ["marlow"],
  },
];

describe("NotInScanPanel through a failed refetch", () => {
  it("keeps the list it is holding and says the read may be out of date", () => {
    render(<NotInScanPanel items={UNMATCHED} error onClose={vi.fn()} />);

    const stale = screen.getByText(STALE_ANY);
    expect(stale, WHAT_HINT).toHaveTextContent(/Couldn't check the last scan just now/);
    expect(screen.getByText("Harbor Lights")).toBeInTheDocument();
    // The sentence that would have replaced the list.
    expect(screen.queryByText(/couldn't read the last scan/i)).toBeNull();
  });

  it("still refuses the list when the read failed with nothing in hand", () => {
    render(<NotInScanPanel items={[]} error onClose={vi.fn()} />);

    expect(screen.getByText(/Reaper couldn't read the last scan/)).toBeInTheDocument();
    expect(screen.queryByText(STALE_ANY)).toBeNull();
    // And never the all-clear: an unread report and an empty one are indistinguishable here,
    // so claiming nothing was left out is the wrong direction to guess in.
    expect(screen.queryByText(/Every available request is in the last scan/)).toBeNull();
  });
});

// --- the Scales board ---------------------------------------------------------------------

function report(over: Partial<FairnessReport> = {}): FairnessReport {
  return {
    total_requests: 12,
    total_reclaimable_bytes: 4 * 1024 ** 3,
    total_reclaimable_items: 2,
    not_in_scan: 0,
    unmatched: [],
    no_snapshot: false,
    horizon_at: "2018-01-11T00:00:00+00:00",
    rows: [
      {
        identity: "plex:7",
        plex_id: 7,
        name: "marlow",
        requests_made: 12,
        gb_granted_bytes: 40 * 1024 ** 3,
        played_by_them: 9,
        reclaimable_items: 2,
        reclaimable_bytes: 4 * 1024 ** 3,
        reclaimable: [],
        seerr_total: 12,
        movie_at_limit: false,
        tv_at_limit: false,
      },
    ],
    ...over,
  };
}

describe("the Scales board through a failed refetch", () => {
  it("keeps the board and says the read may be out of date", async () => {
    apiMock.fairness.mockResolvedValue(report());
    const client = renderWithClient(<Fairness />);
    expect(await screen.findByText("marlow")).toBeInTheDocument();

    await blink(client, apiMock.fairness, ["fairness"]);

    const stale = await screen.findByText(STALE_ANY);
    expect(stale, WHAT_HINT).toHaveTextContent(/Couldn't check Scales just now/);
    expect(stale).toHaveClass("notice-warn");
    expect(screen.getByText("marlow")).toBeInTheDocument();
    expect(screen.queryByText("Couldn't load Scales.")).toBeNull();
  });

  it("still says it couldn't load when the first read is the one that fails", async () => {
    apiMock.fairness.mockRejectedValue(new Error("boom"));
    renderWithClient(<Fairness />);

    expect(await screen.findByText("Couldn't load Scales.")).toBeInTheDocument();
    expect(screen.queryByText(STALE_ANY)).toBeNull();
  });

  it("says neither in the exception's own words", async () => {
    // Rule 21: the sentence used to end in `error.message`, an upstream exception string read by
    // an operator deciding what to delete.
    apiMock.fairness.mockRejectedValue(new Error("ECONNREFUSED 10.0.0.9:5055"));
    renderWithClient(<Fairness />);

    expect(await screen.findByText("Couldn't load Scales.")).toBeInTheDocument();
    expect(screen.queryByText(/ECONNREFUSED/)).toBeNull();
  });
});

// --- the services list --------------------------------------------------------------------

function sonarr(over: Partial<Instance> = {}): Instance {
  return {
    id: 3,
    kind: "sonarr",
    name: "Main",
    base_url: "http://10.0.0.5:8989",
    external_url: null,
    enabled: true,
    verify_tls: true,
    add_import_exclusion: false,
    plex_library_map: {},
    service_instance_map: {},
    has_key: true,
    api_path_prefix: "/api/v3",
    detected_version: null,
    last_ok_at: null,
    last_error: null,
    ...over,
  };
}

describe("the services list through a failed refetch", () => {
  it("keeps the connections and says the read may be out of date", async () => {
    apiMock.instances.mockResolvedValue([sonarr()]);
    const client = renderWithClient(<ServicesPanel />);
    expect(await screen.findByText("Main")).toBeInTheDocument();

    await blink(client, apiMock.instances, ["instances"]);

    const stale = await screen.findByText(STALE_ANY);
    expect(stale, WHAT_HINT).toHaveTextContent(/Couldn't check your connections just now/);
    expect(screen.getByText("Main")).toBeInTheDocument();
  });

  it("still says it couldn't load when the first read is the one that fails, in plain words", async () => {
    apiMock.instances.mockRejectedValue(new Error("ECONNREFUSED 10.0.0.5:8989"));
    renderWithClient(<ServicesPanel />);

    expect(await screen.findByText("Couldn't load your connections.")).toBeInTheDocument();
    expect(screen.queryByText(STALE_ANY)).toBeNull();
    expect(screen.queryByText(/ECONNREFUSED/)).toBeNull();
  });
});

// --- the service editor's folder grid -----------------------------------------------------

const FOLDERS: RootFolder[] = [{ path: "/media/tv", suggested_library: null }];
const LIBRARIES: PlexLibrary[] = [{ key: 1, title: "TV", kind: "show", enabled: true }];

function renderModal(): QueryClient {
  apiMock.instanceRootFolders.mockResolvedValue(FOLDERS);
  apiMock.plexLibraries.mockResolvedValue(LIBRARIES);
  apiMock.updateInstance.mockResolvedValue(sonarr());
  return renderWithClient(
    <ServiceModal kind="sonarr" instance={sonarr()} onClose={vi.fn()} />, // editing, so both maps mount
  );
}

describe("the service editor's mapping grid through a failed refetch", () => {
  it("keeps the grid and says the folder read may be out of date", async () => {
    const client = renderModal();
    expect(await screen.findByText("/media/tv")).toBeInTheDocument();

    await blink(client, apiMock.instanceRootFolders, ["instance-root-folders"]);

    const stale = await screen.findByText(STALE_ANY);
    expect(stale, WHAT_HINT).toHaveTextContent(/Couldn't check this instance's folders just now/);
    // The row, and the select that maps it, are what the warning used to take away.
    expect(screen.getByText("/media/tv")).toBeInTheDocument();
    expect(
      screen.getByRole("combobox", { name: "Plex library for /media/tv" }),
    ).toBeInTheDocument();
    expect(screen.queryByText(/couldn't read this instance's folders/i)).toBeNull();
  });

  it("keeps the library options and says that read may be out of date", async () => {
    const client = renderModal();
    expect(await screen.findByText("/media/tv")).toBeInTheDocument();

    await blink(client, apiMock.plexLibraries, ["plex-libraries"]);

    const stale = await screen.findByText(STALE_ANY);
    expect(stale, WHAT_HINT).toHaveTextContent(/Couldn't check your Plex libraries just now/);
    // "couldn't read your Plex libraries" sat here beside a picker still full of them.
    expect(screen.getByRole("option", { name: "TV" })).toBeInTheDocument();
    expect(screen.queryByText(/couldn't read your Plex libraries/i)).toBeNull();
  });

  it("still refuses the grid when the folders never landed", async () => {
    apiMock.instanceRootFolders.mockRejectedValue(new Error("boom"));
    apiMock.plexLibraries.mockResolvedValue(LIBRARIES);
    apiMock.updateInstance.mockResolvedValue(sonarr());
    renderWithClient(<ServiceModal kind="sonarr" instance={sonarr()} onClose={vi.fn()} />);

    expect(
      await screen.findByText(/Reaper couldn't read this instance's folders/),
    ).toBeInTheDocument();
    expect(screen.queryByText(STALE_ANY)).toBeNull();
  });

  // The third state, and the one the first division missed: a list that landed EMPTY is not the
  // never-landed arm, because `[]` is truthy and `!rootFolders.data` is false. It fell through to
  // "This instance reports no root folders to map." -- a positive claim about the instance, made
  // from a read that had just failed, with no warning of any kind on screen.
  it("keeps the empty answer and says the folder read may be out of date", async () => {
    apiMock.instanceRootFolders.mockResolvedValue([]);
    apiMock.plexLibraries.mockResolvedValue(LIBRARIES);
    apiMock.updateInstance.mockResolvedValue(sonarr());
    const client = renderWithClient(
      <ServiceModal kind="sonarr" instance={sonarr()} onClose={vi.fn()} />,
    );
    expect(
      await screen.findByText("This instance reports no root folders to map."),
    ).toBeInTheDocument();

    await blink(client, apiMock.instanceRootFolders, ["instance-root-folders"]);

    const stale = await screen.findByText(STALE_ANY);
    expect(stale, WHAT_HINT).toHaveTextContent(/Couldn't check this instance's folders just now/);
    expect(screen.getByText("This instance reports no root folders to map.")).toBeInTheDocument();
    // Still not the never-landed sentence: this list DID land, and said there were none.
    expect(screen.queryByText(/Reaper couldn't read this instance's folders/)).toBeNull();
  });
});

// --- the service editor's requested-by grid -------------------------------------------------
//
// The seerr half of the same modal. Its two divisions were shipped with no case at all: reverting
// both to a bare `.error` left the whole suite green, while `WHAT_HINT` above already named them
// as covered here (rule 132).

const SERVICES: SeerrService[] = [
  { service_id: 0, kind: "sonarr", name: "Shows", is_4k: false, suggested_instance_id: null },
];

function seerr(over: Partial<Instance> = {}): Instance {
  return { ...sonarr(), id: 7, kind: "seerr", name: "Portal", api_path_prefix: "/api/v1", ...over };
}

function renderSeerrModal(): QueryClient {
  apiMock.instanceSeerrServices.mockResolvedValue(SERVICES);
  apiMock.instances.mockResolvedValue([sonarr()]);
  apiMock.updateInstance.mockResolvedValue(seerr());
  return renderWithClient(<ServiceModal kind="seerr" instance={seerr()} onClose={vi.fn()} />);
}

describe("the service editor's requested-by grid through a failed refetch", () => {
  it("keeps the service rows and says that read may be out of date", async () => {
    const client = renderSeerrModal();
    expect(await screen.findByText("Shows")).toBeInTheDocument();

    await blink(client, apiMock.instanceSeerrServices, ["instance-seerr-services"]);

    const stale = await screen.findByText(STALE_ANY);
    expect(stale, WHAT_HINT).toHaveTextContent(/Couldn't check this portal's services just now/);
    expect(screen.getByText("Shows")).toBeInTheDocument();
    expect(screen.queryByText(/Reaper couldn't read this portal's services/)).toBeNull();
  });

  it("still refuses the grid when the services never landed", async () => {
    apiMock.instanceSeerrServices.mockRejectedValue(new Error("boom"));
    apiMock.instances.mockResolvedValue([sonarr()]);
    apiMock.updateInstance.mockResolvedValue(seerr());
    renderWithClient(<ServiceModal kind="seerr" instance={seerr()} onClose={vi.fn()} />);

    expect(
      await screen.findByText(/Reaper couldn't read this portal's services/),
    ).toBeInTheDocument();
    expect(screen.queryByText(STALE_ANY)).toBeNull();
  });

  // `arrInstances` carries the only nontrivial predicate in the sweep -- `.some(...)` over the
  // service list decides staleness, `.every(...)` decides unreadable -- and had no case at all.
  it("keeps the connection options and says that read may be out of date", async () => {
    const client = renderSeerrModal();
    expect(await screen.findByRole("option", { name: "Main" })).toBeInTheDocument();

    await blink(client, apiMock.instances, ["instances"]);

    const stale = await screen.findByText(STALE_ANY);
    expect(stale, WHAT_HINT).toHaveTextContent(
      /Couldn't check your Sonarr and Radarr connections just now/,
    );
    expect(screen.getByRole("option", { name: "Main" })).toBeInTheDocument();
    expect(
      screen.queryByText(/Reaper couldn't read your Sonarr and Radarr connections/),
    ).toBeNull();
  });

  it("still says the connections are unreadable when none ever landed", async () => {
    apiMock.instanceSeerrServices.mockResolvedValue(SERVICES);
    apiMock.instances.mockRejectedValue(new Error("boom"));
    apiMock.updateInstance.mockResolvedValue(seerr());
    renderWithClient(<ServiceModal kind="seerr" instance={seerr()} onClose={vi.fn()} />);

    expect(
      await screen.findByText(/Reaper couldn't read your Sonarr and Radarr connections/),
    ).toBeInTheDocument();
    expect(screen.queryByText(STALE_ANY)).toBeNull();
  });

  it("keeps the empty answer and says the service read may be out of date", async () => {
    apiMock.instanceSeerrServices.mockResolvedValue([]);
    apiMock.instances.mockResolvedValue([sonarr()]);
    apiMock.updateInstance.mockResolvedValue(seerr());
    const client = renderWithClient(
      <ServiceModal kind="seerr" instance={seerr()} onClose={vi.fn()} />,
    );
    expect(
      await screen.findByText("This portal reports no Sonarr or Radarr services to map."),
    ).toBeInTheDocument();

    await blink(client, apiMock.instanceSeerrServices, ["instance-seerr-services"]);

    const stale = await screen.findByText(STALE_ANY);
    expect(stale, WHAT_HINT).toHaveTextContent(/Couldn't check this portal's services just now/);
    expect(
      screen.getByText("This portal reports no Sonarr or Radarr services to map."),
    ).toBeInTheDocument();
    expect(screen.queryByText(/Reaper couldn't read this portal's services/)).toBeNull();
  });
});

// --- the review queue and its season list -------------------------------------------------

function showSeason(): Candidate {
  return {
    id: 3,
    media_key: "sonarr:5:42:3",
    title: "Example Show · Season 3",
    media_type: "season",
    size_bytes: 1024 ** 3,
    verdict: "abstain",
    score: 82,
    coverage_bp: 10_000,
    first_flagged_at: null,
    year: 2012,
    summary: null,
    poster_url: null,
    requested_by: null,
    group_key: "sonarr:5:42",
    group_title: "Example Show",
    group_condemned_count: null,
    group_condemned_bytes: null,
    group_unknown_size: null,
    video_resolution: null,
    library: null,
    dormant_for: null,
    reason: null,
    spared: false,
    override: null,
    override_own: null,
    show_override: null,
    override_effective: null,
    spare_expires_at: null,
    spare_covers_until: null,
    show_spare_expires_at: null,
    chip: null,
    show_status: null,
    season_number: 3,
    group_seasons: [
      {
        id: 3,
        season: 3,
        verdict: "abstain",
        override: null,
        override_effective: null,
        size_bytes: 1024 ** 3,
        spare_expires_at: null,
        spare_covers_until: null,
      },
    ],
  };
}

const GROUP: Group = {
  group_key: "sonarr:5:42",
  title: "Example Show",
  year: 2012,
  poster_url: null,
  summary: null,
  size_bytes: 1024 ** 3,
  unknown_size_seasons: 0,
  reason: null,
  library: null,
  chip: null,
  show_override: null,
  show_spare_expires_at: null,
  links: {
    plex: null,
    tautulli: null,
    seerr: null,
    radarr: null,
    sonarr: null,
    imdb: null,
    tmdb: null,
    rotten_tomatoes: null,
    trakt: null,
  },
  show_status: null,
  seasons: [showSeason()],
};

function renderQueue(firstRead: "ok" | Error = "ok"): QueryClient {
  const items = [showSeason()];
  if (firstRead instanceof Error) apiMock.candidates.mockRejectedValue(firstRead);
  else
    apiMock.candidates.mockResolvedValue({
      items,
      total: items.length,
      totalBytes: 1024 ** 3,
      unknownSize: 0,
      offset: 0,
      snapshotId: 1,
    });
  return renderWithClient(
    <ReviewQueue
      verdict="abstain"
      onVerdictChange={() => {}}
      selectedId={null}
      selectedGroupKey={null}
      onSelect={() => {}}
      onSelectGroup={() => {}}
    />,
  );
}

describe("the review queue through a failed refetch", () => {
  it("keeps the rows and says the read may be out of date", async () => {
    const client = renderQueue();
    expect(await screen.findByText("Example Show")).toBeInTheDocument();

    await blink(client, apiMock.candidates, ["candidates"]);

    const stale = await screen.findByText(STALE_ANY);
    expect(stale, WHAT_HINT).toHaveTextContent(/Couldn't check the queue just now/);
    expect(screen.getByText("Example Show")).toBeInTheDocument();
  });

  it("still says it couldn't load when the first read is the one that fails, in plain words", async () => {
    renderQueue(new Error("ECONNREFUSED 10.0.0.5:8989"));

    expect(await screen.findByText("Couldn't load your review queue.")).toBeInTheDocument();
    expect(screen.queryByText(STALE_ANY)).toBeNull();
    // Rule 21 again: this one printed `error.message` straight onto the page.
    expect(screen.queryByText(/ECONNREFUSED/)).toBeNull();
  });
});

describe("the expanded season list through a failed refetch", () => {
  it("keeps every season row and says the read may be out of date", async () => {
    apiMock.group.mockResolvedValue(GROUP);
    const client = renderQueue();
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: /1 season/ }));
    expect(await screen.findByText("Season 3")).toBeInTheDocument();

    await blink(client, apiMock.group, ["group"]);

    const stale = await screen.findByText(STALE_ANY);
    expect(stale, WHAT_HINT).toHaveTextContent(/Couldn't check the seasons just now/);
    // Rule 42: the review surfaces speak in their own note grammar, not in `.notice`.
    expect(stale).toHaveClass("season-list-note");
    // The rows, each with its own Spare, are what the red line used to replace.
    expect(screen.getByText("Season 3")).toBeInTheDocument();
    expect(screen.queryByText(/Couldn't load the seasons/)).toBeNull();
  });

  it("still says it couldn't load the seasons when the first read is the one that fails", async () => {
    apiMock.group.mockRejectedValue(new Error("boom"));
    renderQueue();
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: /1 season/ }));

    expect(await screen.findByText(/Couldn't load the seasons/)).toBeInTheDocument();
    expect(screen.queryByText(STALE_ANY)).toBeNull();
  });
});

// --- the add-a-rule form ------------------------------------------------------------------

const VOCAB: Vocabulary = {
  lane: "condemn",
  fields: [
    {
      key: "runtime_minutes",
      label: "Runtime",
      help_text: "How long the movie runs.",
      type: "int",
      unit_suffix: "min",
      ops: ["gte", "lte"],
    },
  ],
};

describe("the add-a-rule form through a failed refetch", () => {
  it("keeps the form when the vocabulary is still held", async () => {
    apiMock.vocabulary.mockResolvedValue(VOCAB);
    const client = renderWithClient(
      <RemoveRulesEditor condemn={[]} onCondemn={() => {}} mediaType="movie" />,
    );
    expect(await screen.findByRole("option", { name: "Runtime" })).toBeInTheDocument();

    // `SetupWizard` fires a bare `invalidateQueries()`, so every key refetches: this arrives
    // without the operator touching the policy page at all.
    await blink(client, apiMock.vocabulary, ["vocabulary"]);

    expect(screen.getByRole("option", { name: "Runtime" })).toBeInTheDocument();
    expect(screen.queryByText(/couldn't load the things a rule can look at/i)).toBeNull();
  });

  it("still drops the form when the vocabulary never landed", async () => {
    apiMock.vocabulary.mockRejectedValue(new Error("boom"));
    renderWithClient(<RemoveRulesEditor condemn={[]} onCondemn={() => {}} mediaType="movie" />);

    expect(
      await screen.findByText(/Reaper couldn't load the things a rule can look at/),
    ).toBeInTheDocument();
    expect(screen.queryByRole("combobox", { name: "Field" })).toBeNull();
  });
});
