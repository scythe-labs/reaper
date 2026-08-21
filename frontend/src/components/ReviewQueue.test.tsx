// SPDX-License-Identifier: AGPL-3.0-or-later
// The bulk paths of the review queue, where one press writes the override that decides
// whether many files are reaped or spared. Three behaviors are load-bearing:
//   - a bulk override that partly fails refreshes anyway, keeps ONLY the failed items
//     picked, and says how many failed (a clean-up that cleared the whole selection would
//     silently drop them from the next plan);
//   - "select everything matching" that cannot reach the end of the list selects nothing
//     and says so, rather than quietly meaning "the first page";
//   - nothing else can be pressed while a bulk write is in flight.
// The compact dormancy span is pinned here too: it rewrites a string the server writes.
import { useQuery } from "@tanstack/react-query";
import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { Announcer } from "../announce";
import {
  api,
  type Candidate,
  type CandidatePage,
  type GroupRollup,
  type GroupSeasonMark,
  type Snapshot,
  type Verdict,
} from "../api";
import { expectNoA11yViolations } from "../test/a11y";
import { DEFAULT_GENERAL } from "../test/apiFixtures";
import { renderWithProviders } from "../test/renderWithProviders";
import { NARROW_SCREEN_QUERY } from "../useMediaQuery";
import { filtersKey } from "./queueFilters";
import { shouldExpandSeasons } from "./queueSettings";
import {
  compactSpan,
  DEFAULT_FILTERS,
  KeptByShowNote,
  OverrideControls,
  ReviewQueue,
  ShowStatusChip,
} from "./ReviewQueue";

const { apiMock } = await vi.hoisted(async () => ({
  apiMock: (await import("../test/apiMock")).makeApiMock(),
}));

vi.mock("../api", () => ({ api: apiMock }));

function movie(n: number, extra: Partial<Candidate> = {}): Candidate {
  const c: Candidate = {
    id: n,
    media_key: `radarr:1:${n}`,
    title: `Example Movie ${n}`,
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
    ...extra,
  };
  // Default an item's own decision to its effective one unless a test sets them apart (to
  // exercise a season kept only by its show).
  if (extra.override_own === undefined) c.override_own = c.override;
  return c;
}

/** A season row of one show. Shared `group_key` folds them into a single show card. */
function season(n: number, verdict: Verdict, extra: Partial<Candidate> = {}): Candidate {
  return movie(n, {
    media_key: `sonarr:1:${n}`,
    media_type: "season",
    title: "Example Show",
    group_key: "sonarr:show:1",
    group_title: "Example Show",
    season_number: n,
    verdict,
    ...extra,
  });
}

/** One show's whole-snapshot rollup, which the server sends once per show beside the rows.
 *
 *  The three figures default to zero rather than being derived from `seasons`: deriving them
 *  would re-implement the server's actable-season rule here, and every test that asserts one
 *  of these numbers would then pass whether or not it was ever sent (rules 119 and 141). A
 *  test that is about a count states it. */
function rollup(seasons: GroupSeasonMark[], extra: Partial<GroupRollup> = {}): GroupRollup {
  return {
    group_key: "sonarr:show:1",
    condemned_count: 0,
    condemned_bytes: 0,
    unknown_size: 0,
    seasons,
    ...extra,
  };
}

/** One page of candidates, with `total` deciding whether another page is claimed to exist and
 *  `snapshotId` naming which scan the page came from (so a refetch can land a newer one).
 *
 *  Annotated, so a field added to or renamed on the envelope fails the build here rather than
 *  reaching 57 call sites as `undefined`: `apiMock.candidates` is a bare `vi.fn()`, which
 *  checks nothing about what it is handed. */
function page(
  items: Candidate[],
  groups: GroupRollup[] = [],
  total = items.length,
  offset = 0,
  snapshotId = 1,
): CandidatePage {
  return {
    items,
    groups,
    total,
    total_bytes: items.reduce((sum, i) => sum + (i.size_bytes ?? 0), 0),
    unknown_size: items.reduce((n, i) => n + (i.size_bytes === null ? 1 : 0), 0),
    offset,
    snapshot_id: snapshotId,
  };
}

/** Stands in for the Reap page's breakdown: the net an override changes, mounted so the
 *  test can see whether saving one refreshes it. */
function BreakdownProbe() {
  useQuery({ queryKey: ["reap-breakdown"], queryFn: api.reapBreakdown });
  return null;
}

function renderQueue(verdict: Verdict = "condemn", latestScanSnapshotId: number | null = null) {
  return renderWithProviders(
    <>
      <BreakdownProbe />
      <ReviewQueue
        verdict={verdict}
        onVerdictChange={() => {}}
        selectedId={null}
        selectedGroupKey={null}
        onSelect={() => {}}
        onSelectGroup={() => {}}
        latestScanSnapshotId={latestScanSnapshotId}
      />
    </>,
  );
}

/** What the bulk bar says is picked -- "N selected", or "N cards, M items" once a show card
 *  stands for more than itself. Scoped to the bar: the queue header carries a count of its
 *  own, and the two are different numbers. */
function pickedCount(): string {
  const bar = screen.getByRole("region", { name: "Bulk actions" });
  return within(bar).getByText(/selected|Tap or drag|cards?,/).textContent ?? "";
}

/** Enter Select mode (the bulk bar only exists there) and pick every drawn card. */
async function selectAllDrawn() {
  const { userEvent } = await import("@testing-library/user-event");
  const user = userEvent.setup();
  await user.click(await screen.findByRole("button", { name: "Select" }));
  await user.click(await screen.findByRole("button", { name: "Select all" }));
  return user;
}

// The queue watches a sentinel to reveal more cards; jsdom has no observer, and the list
// under test is short enough that never firing is the honest behavior.
class NoopObserver {
  observe() {}
  unobserve() {}
  disconnect() {}
  takeRecords() {
    return [];
  }
}

// A test that stubs matchMedia must not leave it stubbed for the next one (rule 133); the
// beforeEach re-stubs IntersectionObserver either way.
afterEach(() => {
  vi.unstubAllGlobals();
});

/** The queue REMEMBERS its filters (`saveFilters` -> window.localStorage, keyed per tab), so a
 *  test that adds one hands it to every test after it in this file: the next render starts with a
 *  chip already on the toolbar and one fewer dimension addable, which reads as a component bug in
 *  whichever test trips first (rule 133). Drop that one key, not the whole store -- the season
 *  expansion preference lives there too and the tests around it seed it themselves. */
function forgetFilters(verdict: Verdict = "condemn") {
  try {
    window.localStorage.removeItem(filtersKey(verdict));
  } catch {
    // Same reading as production: an unavailable store simply never remembered anything.
  }
}

beforeEach(() => {
  vi.stubGlobal("IntersectionObserver", NoopObserver);
  vi.clearAllMocks();
  apiMock.vocabularyValues.mockResolvedValue({ values: [] });
  apiMock.group.mockResolvedValue(null);
  apiMock.profile.mockResolvedValue({ max_unmeasured_per_run: 0 });
  apiMock.general.mockResolvedValue(DEFAULT_GENERAL);
  apiMock.reapBreakdown.mockResolvedValue({
    has_snapshot: true,
    will_reap: 0,
    condemned_by: [],
  });
  // Read unconditionally now, by every card's collection picker as well as the collection
  // screen's own header (#816 phase 4/5) -- referencing `baseSnapshot`, declared further down,
  // is safe: this callback only runs once the whole module (including that declaration) has
  // finished loading.
  apiMock.latestSnapshot.mockResolvedValue(baseSnapshot);
});

describe("keeping the list in step with the latest scan", () => {
  // Every keep-or-remove decision is made on these cards, one at a time. What a reader makes of a
  // card is the whole of what an operator knows before they accept the verdict on it.
  it("has no accessibility violations", async () => {
    apiMock.candidates.mockResolvedValue(page([movie(1), movie(2)]));
    const { container } = renderQueue();
    await screen.findByText("Example Movie 1");
    await expectNoA11yViolations(container);
  });

  it("confirms a quiet refresh with a toast only once the swap has landed", async () => {
    // This view loads snapshot 1; the newest completed scan is snapshot 2, so the list is a scan
    // behind. Idle at the top (jsdom scrollY 0, nothing open or selected): it refreshes quietly,
    // the refetch lands snapshot 2, and only THEN does a toast say so -- never at issuance (PR-5).
    apiMock.candidates
      .mockResolvedValueOnce(page([movie(1), movie(2)], [], 2, 0, 1))
      .mockResolvedValue(page([movie(1), movie(2)], [], 2, 0, 2));
    renderQueue("condemn", 2);
    expect(await screen.findByText("Updated to the latest scan.")).toBeInTheDocument();
    // Quiet means quiet: no mid-review nudge, no "one scan behind" marker.
    expect(screen.queryByText("A newer scan just finished")).not.toBeInTheDocument();
    expect(screen.queryByText(/One scan behind/)).not.toBeInTheDocument();
  });

  it("does not claim a swap when the refetch fails to catch up; it nudges instead", async () => {
    // The list is a scan behind and the reviewer is idle, so a silent refresh is attempted -- but
    // the refetch errors, so the list never reaches snapshot 2. The toast must not lie that it
    // did; a nudge appears so the reviewer is not left silently stale (PR-5).
    apiMock.candidates
      .mockResolvedValueOnce(page([movie(1), movie(2)], [], 2, 0, 1))
      .mockRejectedValue(new Error("network blip"));
    renderQueue("condemn", 2);
    expect(
      await screen.findByText(/A newer scan just finished|One scan behind/),
    ).toBeInTheDocument();
    expect(screen.queryByText("Updated to the latest scan.")).not.toBeInTheDocument();
  });

  it("Show latest closes an open why-panel, whose row belongs to the replaced scan", async () => {
    // A panel open is a busy condition, so a newer scan raises the nudge instead of swapping
    // quietly. Pressing Show latest must close the panel: its candidate id is from the old
    // snapshot, so keeping it open would leave the operator deciding from stale evidence (B-7).
    const onClearItemSelection = vi.fn();
    apiMock.candidates.mockResolvedValue(page([movie(1), movie(2)], [], 2, 0, 1));
    renderWithProviders(
      <ReviewQueue
        verdict="condemn"
        onVerdictChange={() => {}}
        selectedId={1}
        selectedGroupKey={null}
        onSelect={() => {}}
        onSelectGroup={() => {}}
        onClearItemSelection={onClearItemSelection}
        latestScanSnapshotId={2}
      />,
    );
    const user = userEvent.setup();
    // Settle the two states this control's EXISTENCE depends on, in order, before reaching
    // for it (#149). The nudge is not rendered with the list: `nudging` is set by an effect
    // that runs only once the candidates read has resolved and the snapshot mismatch has been
    // observed, so the button is two commits behind the render. A single
    // `findByRole("button", { name })` had to cover that whole chain on one 1000ms budget,
    // while re-computing accessible names across both cards and the open panel on every poll,
    // which is the most expensive query Testing Library has. It lost the race on a loaded CI
    // runner about one run in a few, on branches that do not touch this file at all, and the
    // dumped DOM showed exactly this: both cards rendered, no nudge yet.
    //
    // Two cheap text waits instead, each with its own 1000ms budget, so a genuine regression
    // in this chain fails naming the step that broke rather than the button at the end of it.
    // Rule 137's margin sweep is how a step with no headroom is found: hold every React Query
    // notification back 200ms and re-run. That is a diagnostic to apply and revert, not
    // something the suite carries, so nothing here is running behind a delay.
    await screen.findByText("Example Movie 1");
    await screen.findByText("A newer scan just finished");
    await user.click(screen.getByRole("button", { name: "Show latest" }));
    expect(onClearItemSelection).toHaveBeenCalledTimes(1);
  });
});

describe("a bulk override", () => {
  it("refreshes, keeps only the failed item picked, and says how many failed", async () => {
    const items = [movie(1), movie(2), movie(3)];
    apiMock.candidates.mockResolvedValue(page(items));
    apiMock.override.mockImplementation((key: string) =>
      key === "radarr:1:2" ? Promise.reject(new Error("boom")) : Promise.resolve(undefined),
    );
    renderQueue();
    const user = await selectAllDrawn();
    expect(pickedCount()).toContain("3");

    const listFetches = apiMock.candidates.mock.calls.length;
    await user.click(screen.getByRole("button", { name: "Spare" }));

    // All three were attempted: Promise.all would have skipped the two after the failure.
    await waitFor(() => expect(apiMock.override).toHaveBeenCalledTimes(3));
    expect(apiMock.override.mock.calls.map((c) => c[0]).sort()).toEqual([
      "radarr:1:1",
      "radarr:1:2",
      "radarr:1:3",
    ]);
    // The queue refreshed even though part of the write failed.
    await waitFor(() => expect(apiMock.candidates.mock.calls.length).toBeGreaterThan(listFetches));
    // The failed one, alone, is still picked, and the notice counts it.
    expect(
      await screen.findByText(/1 item could not be updated. It is still selected/),
    ).toBeInTheDocument();
    expect(pickedCount()).toContain("1");
    // And it is the one that failed, not just any one: a clean-up that cleared the whole
    // selection and left an unrelated card picked would still count to one.
    expect(screen.getByRole("button", { name: "Select Example Movie 2" })).toHaveAttribute(
      "aria-pressed",
      "true",
    );
    for (const title of ["Example Movie 1", "Example Movie 3"]) {
      expect(screen.getByRole("button", { name: `Select ${title}` })).toHaveAttribute(
        "aria-pressed",
        "false",
      );
    }
  });

  it("refreshes the reap breakdown, which the override changes too", async () => {
    // A spare drops an item out of the net and a hand reap adds one, so the Reap page's
    // breakdown must not go on serving the numbers it fetched before the decision.
    apiMock.candidates.mockResolvedValue(page([movie(1)]));
    apiMock.override.mockResolvedValue(undefined);
    renderQueue();
    const user = await selectAllDrawn();
    await waitFor(() => expect(apiMock.reapBreakdown).toHaveBeenCalledTimes(1));

    await user.click(screen.getByRole("button", { name: "Spare" }));
    await waitFor(() => expect(apiMock.reapBreakdown).toHaveBeenCalledTimes(2));
  });

  it("blocks the other actions while it is in flight", async () => {
    apiMock.candidates.mockResolvedValue(page([movie(1), movie(2)]));
    // Both writes hang until the test lets them go; one then fails, so a pick survives and
    // the buttons have something to be enabled for once the write is no longer in flight.
    const settle: (() => void)[] = [];
    apiMock.override.mockImplementation(
      (key: string) =>
        new Promise<void>((resolve, reject) => {
          settle.push(() =>
            key === "radarr:1:2" ? reject(new Error("boom")) : resolve(undefined),
          );
        }),
    );
    renderQueue();
    const user = await selectAllDrawn();
    await user.click(screen.getByRole("button", { name: "Spare" }));

    await waitFor(() => expect(screen.getByRole("button", { name: /Reap now/ })).toBeDisabled());
    expect(screen.getByRole("button", { name: "Clear override" })).toBeDisabled();
    await waitFor(() => expect(settle).toHaveLength(2));
    settle.forEach((s) => s());
    await waitFor(() =>
      expect(screen.getByRole("button", { name: /Reap now/ })).not.toBeDisabled(),
    );
  });
});

describe("the per-card override buttons", () => {
  it("offers Spare but not Reap on the Condemned lane, since the item is already on the block", async () => {
    apiMock.candidates.mockResolvedValue(page([movie(1)]));
    renderQueue();
    // The card's own Spare (rescue) is here; there is no per-card Reap on this lane -- it
    // would force onto a list the item is already on. The bulk bar's Reap is another surface.
    expect(await screen.findByRole("button", { name: "Spare" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /^Reap$/ })).not.toBeInTheDocument();
  });

  it("offers both Spare and Reap off the Condemned lane, where forcing a reap means something", async () => {
    apiMock.candidates.mockResolvedValue(page([movie(1, { verdict: "protect" })]));
    renderQueue("protect");
    expect(await screen.findByRole("button", { name: "Spare" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Reap" })).toBeInTheDocument();
  });
});

describe("the whole-show override buttons", () => {
  it("keeps Reap on a part-condemned show on the Condemned lane, unlike a condemned movie", async () => {
    // The show is here because SOME season is condemned; a whole-show Reap still takes the
    // seasons the scan kept, so it is not the movie's no-op and both buttons stay.
    apiMock.candidates.mockResolvedValue(
      page([season(1, "condemn"), season(2, "condemn"), season(3, "protect")]),
    );
    renderQueue("condemn");
    expect(await screen.findByRole("button", { name: "Spare" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Reap" })).toBeInTheDocument();
  });

  it("keeps Reap when the kept seasons are on other lanes, absent from the Condemned page", async () => {
    // The real shape on the Condemned lane: every FETCHED row is condemned (the kept seasons
    // sit on other lanes and never load here), but the show's rollup still carries the whole
    // show, including a kept one. A whole-show Reap takes that kept season, so Reap must stay
    // -- the card judges over the rollup's seasons, not the tab-filtered page.
    const marks: GroupSeasonMark[] = [
      {
        id: 1,
        season: 1,
        verdict: "condemn",
        override: null,
        override_effective: null,
        size_bytes: 1024 ** 3,
        spare_expires_at: null,
        spare_covers_until: null,
      },
      {
        id: 2,
        season: 2,
        verdict: "condemn",
        override: null,
        override_effective: null,
        size_bytes: 1024 ** 3,
        spare_expires_at: null,
        spare_covers_until: null,
      },
      {
        id: 3,
        season: 3,
        verdict: "protect",
        override: null,
        override_effective: null,
        size_bytes: 1024 ** 3,
        spare_expires_at: null,
        spare_covers_until: null,
      },
    ];
    apiMock.candidates.mockResolvedValue(
      page([season(1, "condemn"), season(2, "condemn")], [rollup(marks)]),
    );
    renderQueue("condemn");
    expect(await screen.findByRole("button", { name: "Spare" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Reap" })).toBeInTheDocument();
  });

  it("does not light the whole-show Reap when only some seasons are reaped", async () => {
    // On the Condemned lane the fetched rows are the reaped/condemned seasons, which all agree
    // "reap". But across the whole show (the rollup) the override is mixed -- other seasons
    // are untouched -- so the whole-show control must NOT read as "Reaping" (its active state).
    const marks: GroupSeasonMark[] = [
      {
        id: 5,
        season: 5,
        verdict: "abstain",
        override: "reap",
        override_effective: false,
        size_bytes: 1024 ** 3,
        spare_expires_at: null,
        spare_covers_until: null,
      },
      {
        id: 8,
        season: 8,
        verdict: "condemn",
        override: "reap",
        override_effective: true,
        size_bytes: 1024 ** 3,
        spare_expires_at: null,
        spare_covers_until: null,
      },
      {
        id: 10,
        season: 10,
        verdict: "protect",
        override: null,
        override_effective: null,
        size_bytes: 1024 ** 3,
        spare_expires_at: null,
        spare_covers_until: null,
      },
    ];
    apiMock.candidates.mockResolvedValue(
      page([season(8, "condemn", { override: "reap", override_effective: true })], [rollup(marks)]),
    );
    renderQueue("condemn");
    await screen.findByText("Example Show");
    // Both buttons present and inactive: the Reap button says "Reap", never the active "Reaping".
    expect(screen.getByRole("button", { name: "Reap" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Reaping" })).not.toBeInTheDocument();
  });

  it("drops Reap once every season of the show is condemned", async () => {
    // Now a whole-show Reap would change nothing, so it falls away just as the movie's does.
    // Every season condemned in the rollup too, so the whole-show view agrees.
    const marks: GroupSeasonMark[] = [
      {
        id: 1,
        season: 1,
        verdict: "condemn",
        override: null,
        override_effective: null,
        size_bytes: 1024 ** 3,
        spare_expires_at: null,
        spare_covers_until: null,
      },
      {
        id: 2,
        season: 2,
        verdict: "condemn",
        override: null,
        override_effective: null,
        size_bytes: 1024 ** 3,
        spare_expires_at: null,
        spare_covers_until: null,
      },
    ];
    apiMock.candidates.mockResolvedValue(
      page([season(1, "condemn"), season(2, "condemn")], [rollup(marks)]),
    );
    renderQueue("condemn");
    expect(await screen.findByRole("button", { name: "Spare" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /^Reap$/ })).not.toBeInTheDocument();
  });
});

describe("the expand-seasons-by-default preference", () => {
  it("starts a show's season list collapsed when the preference is off", async () => {
    // The beforeEach default is off, so the card rests collapsed until the pill is clicked.
    apiMock.candidates.mockResolvedValue(page([season(1, "condemn"), season(2, "condemn")]));
    renderQueue("condemn");
    const expander = await screen.findByRole("button", { name: "2 seasons" });
    expect(expander).toHaveAttribute("aria-expanded", "false");
  });

  // Every combination of the "expand seasons by default" preference and the screen it is read
  // on, written from the spec rather than from the branch (rule 119). The decision is pure, so
  // the table is exhaustive and instant; the two renders below prove it is actually wired.
  it.each([
    { mode: "off", narrow: false, expanded: false },
    { mode: "off", narrow: true, expanded: false },
    { mode: "desktop", narrow: false, expanded: true },
    { mode: "desktop", narrow: true, expanded: false },
    { mode: "both", narrow: false, expanded: true },
    { mode: "both", narrow: true, expanded: true },
    { mode: "mobile", narrow: false, expanded: false },
    { mode: "mobile", narrow: true, expanded: true },
  ] as const)(
    "'$mode' on a narrow=$narrow screen starts the season list expanded=$expanded",
    ({ mode, narrow, expanded }) => {
      expect(shouldExpandSeasons(mode, narrow)).toBe(expanded);
    },
  );

  // The two renders that prove the table above is actually consulted, and consulted with BOTH
  // of its arguments. Each asserts an expansion that only happens if its argument arrived, so
  // neither can pass against a preference that never loaded -- which a "stays collapsed"
  // assertion would do quite happily.
  it("opens a show's season list when the preference covers this screen", async () => {
    // jsdom has no matchMedia, so useMediaQuery reports false: the wide screen, which
    // "desktop" covers. This pins that the stored mode reaches the card.
    apiMock.general.mockResolvedValue({ ...DEFAULT_GENERAL, expand_seasons_mode: "desktop" });
    apiMock.candidates.mockResolvedValue(page([season(1, "condemn"), season(2, "condemn")]));
    renderQueue("condemn");
    // The preference resolves on its own query, so the card may mount collapsed and expand a
    // tick later; wait for the expanded state rather than asserting the first frame.
    const expander = await screen.findByRole("button", { name: "2 seasons" });
    await waitFor(() => expect(expander).toHaveAttribute("aria-expanded", "true"));
  });

  it("opens it on a narrow screen when the preference is the phone one", async () => {
    // The same card under "mobile", with the viewport reporting narrow. It can only expand if
    // the media query reached the decision too -- ignore that argument and this stays shut.
    vi.stubGlobal("matchMedia", () => ({
      matches: true,
      media: NARROW_SCREEN_QUERY,
      addEventListener: () => {},
      removeEventListener: () => {},
    }));
    apiMock.general.mockResolvedValue({ ...DEFAULT_GENERAL, expand_seasons_mode: "mobile" });
    apiMock.candidates.mockResolvedValue(page([season(1, "condemn"), season(2, "condemn")]));
    renderQueue("condemn");
    const expander = await screen.findByRole("button", { name: "2 seasons" });
    await waitFor(() => expect(expander).toHaveAttribute("aria-expanded", "true"));
  });
});

describe("the bulk Reap override", () => {
  it("is dropped on Condemned, where the real deletion (Reap now) stays instead", async () => {
    apiMock.candidates.mockResolvedValue(page([movie(1)]));
    renderQueue();
    await selectAllDrawn();
    // The redundant bulk Reap override is gone on this lane; Reap now (the real delete) stays.
    expect(screen.queryByRole("button", { name: /^Reap$/ })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Reap now/ })).toBeInTheDocument();
  });

  it("keeps the bulk Reap override off the Condemned lane", async () => {
    apiMock.candidates.mockResolvedValue(page([movie(1, { verdict: "protect" })]));
    renderQueue("protect");
    await selectAllDrawn();
    expect(screen.getByRole("button", { name: /^Reap$/ })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Reap now/ })).not.toBeInTheDocument();
  });
});

describe("the bulk bar's count", () => {
  it("states the items a show card stands for, not just the card", async () => {
    // "1 selected" beside a Reap now that plans ten seasons is not the set the server acts
    // on (U-13, rule 30). The show's count is the server's own actable total.
    const marks: GroupSeasonMark[] = [
      {
        id: 1,
        season: 1,
        verdict: "condemn",
        override: null,
        override_effective: null,
        size_bytes: 1024 ** 3,
        spare_expires_at: null,
        spare_covers_until: null,
      },
      {
        id: 2,
        season: 2,
        verdict: "condemn",
        override: null,
        override_effective: null,
        size_bytes: 1024 ** 3,
        spare_expires_at: null,
        spare_covers_until: null,
      },
    ];
    apiMock.candidates.mockResolvedValue(
      page([season(1, "condemn"), season(2, "condemn")], [rollup(marks, { condemned_count: 10 })]),
    );
    renderQueue("condemn");
    await selectAllDrawn();
    expect(pickedCount()).toMatch(/1 card, 10 items/);
  });

  it("says plain 'selected' when every picked card is one item", async () => {
    apiMock.candidates.mockResolvedValue(page([movie(1), movie(2)]));
    renderQueue("condemn");
    await selectAllDrawn();
    expect(pickedCount()).toMatch(/2 selected/);
  });
});

describe("select everything matching", () => {
  it("selects nothing and says so when the rest of the list won't load", async () => {
    const first = [movie(1), movie(2)];
    apiMock.candidates.mockImplementation((_verdict, _filters, _limit, offset: number) =>
      offset === 0 ? Promise.resolve(page(first, [], 4)) : Promise.reject(new Error("boom")),
    );
    renderQueue();
    const user = await selectAllDrawn();
    // Two drawn and picked, four matching: the button that reaches past the drawn cards.
    await user.click(await screen.findByRole("button", { name: /Select everything matching/ }));

    expect(
      await screen.findByText(/Couldn't load the rest of the list, so nothing was selected/),
    ).toBeInTheDocument();
    // The picks are exactly what they were: the two drawn cards, not the four claimed.
    expect(pickedCount()).toContain("2");
  });

  it("gives a show first seen on a later page the rollup that arrived with it", async () => {
    // A show's rollup rides the page its rows ride. Reading `pages[0].groups` would leave every
    // show past the first page with none, and the card would then draw no strip and print the
    // seasons this page happened to fetch under "would be removed", beside the control that
    // reaps the whole show (rule 30). Six seasons across the show, two of them on this page.
    const marks: GroupSeasonMark[] = [1, 2, 3, 4, 5, 6].map((n) => ({
      id: n,
      season: n,
      verdict: n > 4 ? "protect" : "condemn",
      override: null,
      override_effective: null,
      size_bytes: 1024 ** 3,
      spare_expires_at: null,
      spare_covers_until: null,
    }));
    apiMock.candidates.mockImplementation((_verdict, _filters, _limit, offset: number) =>
      Promise.resolve(
        offset === 0
          ? page([movie(1), movie(2)], [], 4)
          : page(
              [season(1, "condemn"), season(2, "condemn")],
              [rollup(marks, { condemned_count: 4, condemned_bytes: 4 * 1024 ** 3 })],
              4,
              offset,
            ),
      ),
    );
    // The queue pulls the next page itself once the drawn set is within one render page of the
    // loaded one, so the second page arrives with no interaction.
    const { container } = renderQueue("condemn");

    // From the rollup that came with page two, not from the two rows on it, which read "2 of 2".
    expect(await screen.findByText(/4 of 6 would be removed, 4\.0 GiB/)).toBeInTheDocument();
    expect(container.querySelectorAll(".strip-sq")).toHaveLength(marks.length);
  });
});

describe("a reap the engine refused", () => {
  it("reads as one sentence whichever lane kept the item", async () => {
    // Two lanes refuse a hand reap: a FIRED safety stop, and a row Reaper cannot identify
    // (`StatusChip.chipWhy`). Their chips read nothing alike -- one leads "Kept, ", the
    // other is a capitalized sentence of its own -- and both have to land in the same
    // sentence. A protection that merely could not be CHECKED is not one of the lanes: it
    // stopped refusing a reap in the #96 reversal, so an abstain row carrying its conflict
    // chip is reaped, not held, and pinning it here would pin a pairing the server can no
    // longer send (rule 119).
    //
    // The clause comes from the chip's own `why`, which the server words (H-1). These
    // fixtures deliberately give `text` and `why` different words, so a test that passed by
    // re-parsing the text would fail here: this asserts the field is read, not the prose.
    const streaming = movie(1, {
      override: "reap",
      override_effective: false,
      chip: { tone: "kept", text: "Kept, playing right now", why: "playing right now" },
    });
    const unidentifiable = movie(2, {
      override: "reap",
      override_effective: false,
      chip: {
        tone: "quiet",
        text: "Couldn't read its Plex match",
        why: "Reaper couldn't read what it matched in Plex",
      },
    });
    apiMock.candidates.mockResolvedValue(page([streaming, unidentifiable]));
    renderQueue();

    expect(
      await screen.findByText("Reap requested, kept for now: playing right now"),
    ).toBeInTheDocument();
    expect(
      screen.getByText(
        "Reap requested, kept for now: Reaper couldn't read what it matched in Plex",
      ),
    ).toBeInTheDocument();
    // The chip's own display wording never lands mid-sentence: the clause is the server's
    // `why`, never the chip text with its "Kept, " lead sliced off (H-1).
    expect(screen.queryByText(/kept for now: Kept,/)).not.toBeInTheDocument();
    expect(
      screen.queryByText(/kept for now: Couldn't read its Plex match/),
    ).not.toBeInTheDocument();
  });
});

describe("the score badge's color follows the fate", () => {
  // The number cannot keep the scan verdict while the row says "will be removed": it wears
  // the fate a hand decision forces. Solid reap/spare when it takes effect, dashed red when
  // a reap is held (never amber -- amber means "left for you to decide").
  async function scoreClassFor(item: Candidate): Promise<string> {
    apiMock.candidates.mockResolvedValue(page([item]));
    const { container } = renderQueue();
    await screen.findByText("Example Movie 1");
    return container.querySelector(".score")!.className;
  }

  it("goes solid red when the item is reaped by hand", async () => {
    // A protect item (green by the scan) that a hand reap will actually remove.
    const cls = await scoreClassFor(
      movie(1, { verdict: "protect", override: "reap", override_effective: true }),
    );
    expect(cls).toContain("score-reap");
    expect(cls).not.toContain("score-protect");
  });

  it("goes dashed red when the reap is held for now", async () => {
    const cls = await scoreClassFor(
      movie(1, { verdict: "abstain", override: "reap", override_effective: false }),
    );
    expect(cls).toContain("score-refused");
    expect(cls).not.toContain("score-abstain");
  });

  it("goes solid green when the item is spared by hand", async () => {
    const cls = await scoreClassFor(movie(1, { verdict: "condemn", override: "spare" }));
    expect(cls).toContain("score-spare");
    expect(cls).not.toContain("score-condemn");
  });

  it("keeps the scan verdict's color when nothing was overridden", async () => {
    const cls = await scoreClassFor(movie(1, { verdict: "condemn" }));
    expect(cls).toContain("score-condemn");
  });

  it("goes dashed green once the spare's clock has passed", async () => {
    // Neither of its neighbors. Not solid green -- the decision has run out. Not the scan's
    // red either -- only a scan realizes a spare's expiry, so until one runs the item really
    // is still kept and nothing will reap it. Painting it condemned would tell the operator
    // it is back on the block when it is not (rule 61).
    const expired = new Date(Date.now() - 3 * 86_400_000).toISOString();
    const cls = await scoreClassFor(
      movie(1, {
        verdict: "condemn",
        override: "spare",
        spare_expires_at: expired,
        // No show above a movie, so its own spare is the one covering it.
        spare_covers_until: expired,
      }),
    );
    expect(cls).toContain("score-spare-expired");
    expect(cls).not.toContain("score-condemn");
  });

  it("stays solid green when a longer spare still covers the spent one", async () => {
    // A season spared 3 days ago inside a show spared forever. Its OWN spare has run out, and
    // the badge used to read that key and draw the dashed "your decision ran out" green -- a
    // warning about a file the show spare keeps regardless, and one no scan will change. The
    // server answers the fate question in `spare_covers_until`; the badge must read THAT and
    // nothing else, so the two fields are set in opposition here.
    const cls = await scoreClassFor(
      movie(1, {
        verdict: "condemn",
        override: "spare",
        override_own: "spare",
        show_override: "spare",
        spare_expires_at: new Date(Date.now() - 3 * 86_400_000).toISOString(),
        spare_covers_until: null, // the show's forever spare outlasts it
        show_spare_expires_at: null,
      }),
    );
    expect(cls).toContain("score-spare");
    expect(cls).not.toContain("score-spare-expired");
  });
});

describe("the season strip's colors follow the fate", () => {
  // A show card's strip draws one square per season from the show's rollup. Each square must
  // agree with its row: solid for an effective hand decision, dashed red (with a scythe
  // mark) for a reap the engine can't honor yet, the scan verdict otherwise. Amber is never
  // used here -- it means only "left for you to decide".
  function mark(
    id: number,
    verdict: Verdict,
    extra: Partial<GroupSeasonMark> = {},
  ): GroupSeasonMark {
    return {
      id,
      season: id,
      verdict,
      override: null,
      override_effective: null,
      size_bytes: 1024 ** 3,
      spare_expires_at: null,
      spare_covers_until: null,
      ...extra,
    };
  }

  async function stripRender(marks: GroupSeasonMark[]) {
    const rows = marks.map((m) =>
      season(m.id, m.verdict, {
        override: m.override,
        override_effective: m.override_effective,
      }),
    );
    apiMock.candidates.mockResolvedValue(page(rows, [rollup(marks)]));
    const { container } = renderQueue();
    await screen.findByText("Example Show");
    const squares = Array.from(container.querySelectorAll(".strip-sq"));
    return { squares, classes: squares.map((el) => el.className) };
  }

  it("paints a held reap dashed red, not solid and not its plain scan color", async () => {
    const { classes } = await stripRender([
      mark(14, "condemn"),
      mark(19, "abstain", { override: "reap", override_effective: false }),
      mark(20, "abstain", { override: "reap", override_effective: true }),
    ]);
    // 14 condemned, 19 reap-held (dashed red), 20 reap-effective (solid).
    expect(classes[0]).toContain("strip-condemn");
    expect(classes[1]).toContain("strip-ov-reap-refused");
    expect(classes[1]).not.toContain("strip-ov-reap ");
    expect(classes[2]).toContain("strip-ov-reap");
    expect(classes[2]).not.toContain("strip-ov-reap-refused");
  });

  it("paints an expired spare dashed green, apart from a live one and from its scan color", async () => {
    // The strip and the score badge both route through handFate, so the square must draw the
    // same three-way distinction the badge does -- otherwise a show's strip and its rows
    // disagree about what is keeping a season (rule 49).
    const past = new Date(Date.now() - 3 * 86_400_000).toISOString();
    const future = new Date(Date.now() + 30 * 86_400_000).toISOString();
    const { classes } = await stripRender([
      // No show-level spare in this strip, so each season's own spare is what covers it.
      mark(1, "condemn", {
        override: "spare",
        spare_expires_at: future,
        spare_covers_until: future,
      }),
      mark(2, "condemn", { override: "spare", spare_expires_at: past, spare_covers_until: past }),
      mark(3, "condemn", { override: "spare", spare_expires_at: null, spare_covers_until: null }),
      mark(4, "condemn"),
    ]);
    // Live and forever spares stay solid; only the expired one goes dashed. The lookahead is
    // what separates the two class names, since one is the other's prefix.
    const solid = /strip-ov-spare(?!-)/;
    expect(classes[0]).toMatch(solid);
    expect(classes[0]).not.toContain("strip-ov-spare-expired");
    expect(classes[1]).toContain("strip-ov-spare-expired");
    expect(classes[1]).not.toMatch(solid);
    expect(classes[2]).toMatch(solid);
    expect(classes[2]).not.toContain("strip-ov-spare-expired");
    // Every square keeps its scan-verdict base class and the override paints over it in CSS,
    // so "not condemned-looking" is the ABSENCE of an override class, which is what the plain
    // condemned square below has. The expired one must not look like that.
    expect(classes[3]).not.toContain("strip-ov-");
    expect(classes[1]).not.toBe(classes[3]);
  });

  it("marks the held reap with a scythe so it never reads as the plain condemned square", async () => {
    const { squares } = await stripRender([
      mark(14, "condemn"),
      mark(19, "abstain", { override: "reap", override_effective: false }),
    ]);
    // Only the held reap carries the mark; the plain condemned square must not.
    expect(squares[0]!.querySelector(".strip-mark")).toBeNull();
    expect(squares[1]!.querySelector(".strip-mark")).not.toBeNull();
  });
});

describe("the show-status chip", () => {
  it("announces the long form, not the bare label", async () => {
    // The visible words on their own are ambiguous ("Ended" reads as a fact about the
    // file, "Status unknown" says nothing), so the chip carries a sentence. A name only
    // reaches a screen reader on an element whose role can take one.
    render(<ShowStatusChip status="unknown" />);
    expect(
      screen.getByRole("img", { name: "We couldn't check whether this show has ended" }),
    ).toBeInTheDocument();
  });
});

describe("the dormancy span", () => {
  it("shortens a unit that follows a number and leaves a wordy span alone", () => {
    expect(compactSpan("5 years, 9 months")).toBe("5y 9m");
    expect(compactSpan("1 day")).toBe("1d");
    // The server's sub-day phrase carries no number, so nothing in it is a unit to shorten.
    expect(compactSpan("less than a day")).toBe("less than a day");
  });

  it("renders the sub-day phrase whole on the card", async () => {
    apiMock.candidates.mockResolvedValue(page([movie(1, { dormant_for: "less than a day" })]));
    renderQueue();
    expect(await screen.findByText(/Not watched in less than a day/)).toBeInTheDocument();
  });

  it("says on the card when an item will not be reaped for want of a size", async () => {
    // No plan will include it, which outranks every other reason the card could show:
    // an owner reading "Not watched in 5 years" would reasonably expect it to go.
    apiMock.candidates.mockResolvedValue(
      page([movie(1, { size_bytes: null, dormant_for: "5 years" })]),
    );
    renderQueue();

    expect(await screen.findByText("Held back: size unknown")).toBeInTheDocument();
    expect(screen.getByText("Size unknown")).toBeInTheDocument();
  });

  it("shows the ordinary reason when the size is known", async () => {
    // The hold-back line must not become permanent furniture: it appears only for the
    // items it is about.
    apiMock.candidates.mockResolvedValue(page([movie(1, { dormant_for: "less than a day" })]));
    renderQueue();

    await screen.findByText(/Not watched in less than a day/);
    expect(screen.queryByText("Held back: size unknown")).not.toBeInTheDocument();
  });
});

// The collection chip (#816 phase 4): navigation only, never a verdict input, so these tests
// are about reachability and honesty, not fate. The screen the picker will eventually open is
// phase 5's; here the caret only has to open, list every collection, and stay accessible.
describe("the collection chip", () => {
  it("renders no chip when the scan recorded no collections", async () => {
    apiMock.candidates.mockResolvedValue(page([movie(1, { collections: null })]));
    renderQueue();
    await screen.findByText("Example Movie 1");
    expect(screen.queryByTitle(/^In the collection/)).not.toBeInTheDocument();
  });

  it("wears one plain chip, no caret, when the item is in exactly one collection", async () => {
    apiMock.candidates.mockResolvedValue(page([movie(1, { collections: ["Example Franchise"] })]));
    renderQueue();
    await screen.findByText("Example Movie 1");
    expect(screen.getByRole("button", { name: "Example Franchise" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Show the other/ })).not.toBeInTheDocument();
  });

  it("opens the picker on the caret, listing every collection reachably", async () => {
    const names = ["Example Franchise", "Director Spotlight", "4K"];
    apiMock.candidates.mockResolvedValue(page([movie(1, { collections: names })]));
    renderQueue();
    await screen.findByText("Example Movie 1");
    const caret = screen.getByRole("button", { name: "Show the other 2 collections" });
    const user = userEvent.setup();
    await user.click(caret);
    expect(caret).toHaveAttribute("aria-expanded", "true");
    // Scoped to the picker: the smallest collection's name is on the card's own chip too, and
    // the picker lists the full array including it (rule 138's sibling clamp, not this test's
    // concern), so an unscoped query would match both.
    const picker = screen.getByRole("list", { name: "Collections" });
    for (const name of names) {
      expect(within(picker).getByRole("button", { name })).toBeInTheDocument();
    }
  });

  it("closes on Escape and hands focus back to the caret", async () => {
    apiMock.candidates.mockResolvedValue(
      page([movie(1, { collections: ["Example Franchise", "Director Spotlight"] })]),
    );
    renderQueue();
    await screen.findByText("Example Movie 1");
    const caret = screen.getByRole("button", { name: "Show the other 1 collection" });
    const user = userEvent.setup();
    await user.click(caret);
    expect(screen.getByRole("list", { name: "Collections" })).toBeInTheDocument();
    await user.keyboard("{Escape}");
    expect(screen.queryByRole("list", { name: "Collections" })).not.toBeInTheDocument();
    expect(caret).toHaveFocus();
  });

  it("carries the show's collections onto every season, like the library chip", async () => {
    apiMock.candidates.mockResolvedValue(
      page([
        season(1, "condemn", { collections: ["Example Show Universe", "Studio Vault"] }),
        season(2, "condemn", { collections: ["Example Show Universe", "Studio Vault"] }),
      ]),
    );
    renderQueue();
    await screen.findByText("Example Show");
    expect(screen.getByRole("button", { name: "Example Show Universe" })).toBeInTheDocument();
  });

  it("has no accessibility violations with the picker open", async () => {
    apiMock.candidates.mockResolvedValue(
      page([movie(1, { collections: ["Example Franchise", "Director Spotlight"] })]),
    );
    renderQueue();
    await screen.findByText("Example Movie 1");
    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: "Show the other 1 collection" }));
    // No `container` argument: the picker is portaled to <body>, outside the render's own
    // container, so the default (document.body) is the only scope that sees it.
    await expectNoA11yViolations();
  });

  it("opens the collection screen when the chip's name is pressed", async () => {
    apiMock.candidates.mockResolvedValue(page([movie(1, { collections: ["Example Franchise"] })]));
    apiMock.latestSnapshot.mockResolvedValue(baseSnapshot);
    renderQueue();
    await screen.findByText("Example Movie 1");
    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: "Example Franchise" }));
    expect(await screen.findByRole("heading", { name: "Example Franchise" })).toBeInTheDocument();
  });

  // A collection-name search hit (#816 phase 3b, `search_rank === 2`) carries the collection
  // that actually matched -- an operator who typed "Director" cannot explain a chip reading
  // "Example Franchise" (the smallest one, unrelated to what they typed). The frontend end of
  // the comment on `CandidateOut.matched_collection` (`src/reaper/api/schemas.py`).
  it("renders the collection that matched a search, not the smallest one", async () => {
    apiMock.candidates.mockResolvedValue(
      page([
        movie(1, {
          collections: ["Example Franchise", "Director Spotlight"],
          search_rank: 2,
          matched_collection: "Director Spotlight",
        }),
      ]),
    );
    renderQueue();
    await screen.findByText("Example Movie 1");
    expect(screen.getByRole("button", { name: "Director Spotlight" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Example Franchise" })).not.toBeInTheDocument();
  });

  it("still renders the smallest collection for a title match", async () => {
    apiMock.candidates.mockResolvedValue(
      page([
        movie(1, {
          collections: ["Example Franchise", "Director Spotlight"],
          search_rank: 1,
          matched_collection: null,
        }),
      ]),
    );
    renderQueue();
    await screen.findByText("Example Movie 1");
    expect(screen.getByRole("button", { name: "Example Franchise" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Director Spotlight" })).not.toBeInTheDocument();
  });

  // The picker's counts (#816 phase 4/5): Plex's own member count, read off the same snapshot
  // the collection screen's header already trusts. A collection the scan never got a count for
  // (`_collection_membership` leaves it out of `collection_sizes` rather than folding it to 0,
  // because unknown and empty are different facts) must render no number, never a false "0".
  it("shows each collection's known size beside its name in the picker", async () => {
    const names = ["Example Franchise", "Director Spotlight"];
    apiMock.candidates.mockResolvedValue(page([movie(1, { collections: names })]));
    apiMock.latestSnapshot.mockResolvedValue({
      ...baseSnapshot,
      collection_sizes: { "Example Franchise": 3, "Director Spotlight": 14 },
    });
    renderQueue();
    await screen.findByText("Example Movie 1");
    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: "Show the other 1 collection" }));
    const picker = screen.getByRole("list", { name: "Collections" });
    // Scoped by the name span, not the row's accessible name: the size sits in the same
    // button, so a role query for "Example Franchise" alone would miss a row whose name now
    // reads "Example Franchise 3" to a screen reader.
    expect(within(picker).getByText("Example Franchise").closest("li")).toHaveTextContent("3");
    expect(within(picker).getByText("Director Spotlight").closest("li")).toHaveTextContent("14");
  });

  it("renders no number for a collection whose size the scan never reported", async () => {
    const names = ["Example Franchise", "Director Spotlight"];
    apiMock.candidates.mockResolvedValue(page([movie(1, { collections: names })]));
    apiMock.latestSnapshot.mockResolvedValue({
      ...baseSnapshot,
      // Only one of the two is known -- the other is genuinely absent, not zero.
      collection_sizes: { "Example Franchise": 3 },
    });
    renderQueue();
    await screen.findByText("Example Movie 1");
    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: "Show the other 1 collection" }));
    const picker = screen.getByRole("list", { name: "Collections" });
    const unknownRow = within(picker).getByText("Director Spotlight").closest("li");
    expect(unknownRow?.querySelector(".coll-pop-n")).toBeNull();
    expect(unknownRow?.textContent).toBe("Director Spotlight");
  });
});

/** An ordinary finished scan, no collection sizes known -- the `beforeEach` above seeds every
 *  test in this file with it, since every card's collection picker reads `["snapshot"]`
 *  unconditionally now (#816 phase 4/5), not just a test that opens the collection screen. A
 *  test about a collection's own size (the fate-summary block below, or the picker's counts
 *  above) sets its own `collection_sizes` on top of this. */
const baseSnapshot: Snapshot = {
  id: 1,
  created_at: "2026-01-01T00:00:00+00:00",
  policy_hash: "p",
  horizon_at: "2025-01-01T00:00:00+00:00",
  item_count: 4,
  degraded: false,
  degraded_reason: null,
  degraded_doc: null,
  condemned: 0,
  protected: 0,
  abstained: 0,
  reclaimable_bytes: 0,
  unknown_size_items: 0,
};

// The collection screen (#816 phase 5): a jump names a collection, the queue drops the lane
// tabs for a back link and a fate summary, and the bulk bar -- a selection spanning three
// fates is not one decision (rule 48) -- never renders there at all.
describe("the collection screen", () => {
  const openOnCollection = (name: string) => (
    <ReviewQueue
      verdict="condemn"
      onVerdictChange={() => {}}
      selectedId={null}
      selectedGroupKey={null}
      onSelect={() => {}}
      onSelectGroup={() => {}}
      focus={{ search: "", collection: name, nonce: 1 }}
    />
  );

  /** Every scanned member of "Example Franchise", split across all three fates -- what makes
   *  a collection screen mixed rather than the single-lane shape every other queue test drives. */
  function mixedFateFixture() {
    return {
      condemned: [movie(1), movie(2)],
      protected: [movie(3, { verdict: "protect" })],
      abstained: [movie(4, { verdict: "abstain" })],
    };
  }

  function mockMixedFates({ plexCount = 8 }: { plexCount?: number } = {}) {
    const { condemned, protected: protectedItems, abstained } = mixedFateFixture();
    apiMock.candidates.mockImplementation((verdict: string) => {
      if (verdict === "any")
        return Promise.resolve(page([...condemned, ...protectedItems, ...abstained]));
      if (verdict === "condemn") return Promise.resolve(page(condemned));
      if (verdict === "protect") return Promise.resolve(page(protectedItems));
      if (verdict === "abstain") return Promise.resolve(page(abstained));
      return Promise.resolve(page([]));
    });
    apiMock.latestSnapshot.mockResolvedValue({
      ...baseSnapshot,
      collection_sizes: { "Example Franchise": plexCount },
    });
  }

  it("never renders the bulk bar, even with rows on screen", async () => {
    mockMixedFates();
    renderWithProviders(openOnCollection("Example Franchise"));
    await screen.findByText("Example Movie 1");
    expect(screen.queryByRole("region", { name: "Bulk actions" })).not.toBeInTheDocument();
    // The toggle that would open it is gone too, not merely a bar with nothing to press.
    expect(screen.queryByRole("button", { name: "Select" })).not.toBeInTheDocument();
  });

  it("drops the lane tabs for a back link naming the collection", async () => {
    mockMixedFates();
    renderWithProviders(openOnCollection("Example Franchise"));
    await screen.findByText("Example Movie 1");
    expect(screen.getByRole("heading", { name: "Example Franchise" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Condemned" })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Review queue/ })).toBeInTheDocument();
  });

  it("summarizes the mixed fates once every lane has answered", async () => {
    mockMixedFates();
    const { container } = renderWithProviders(openOnCollection("Example Franchise"));
    // Each fate's count sits in its own <b>, split from the sibling text -- scoped by class the
    // way `pickedCount` above scopes the bulk bar's own split count, rather than a text query
    // that can't see across the element boundary.
    // The lane names, not a fourth vocabulary for the same three sets: this is the only screen
    // showing all three at once, so it reads them off the tabs' own declaration.
    await waitFor(() =>
      expect(container.querySelector(".coll-fate-condemn")).toHaveTextContent("2 condemned"),
    );
    expect(container.querySelector(".coll-fate-protect")).toHaveTextContent("1 in Sanctuary");
    expect(container.querySelector(".coll-fate-abstain")).toHaveTextContent("1 in Limbo");
  });

  it("says how many the last scan found beside how many Plex reports", async () => {
    mockMixedFates();
    renderWithProviders(openOnCollection("Example Franchise"));
    await screen.findByText(/8 in the collection, 4 in the last scan\./);
  });

  // The sentence exists for the GAP: Plex can hold titles this scan never saw, in an unscanned
  // library or unmatched. With nothing missing it restates the "N items" line under the search
  // box, so it does not render at all.
  it("says nothing about counts when the scan saw the whole collection", async () => {
    mockMixedFates({ plexCount: 4 });
    renderWithProviders(openOnCollection("Example Franchise"));
    await screen.findByText("Example Movie 1");
    expect(screen.queryByText(/in the last scan\./)).not.toBeInTheDocument();
  });

  it("the back link returns to a plain lane view", async () => {
    mockMixedFates();
    renderWithProviders(openOnCollection("Example Franchise"));
    await screen.findByText("Example Movie 1");
    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: /Review queue/ }));
    expect(await screen.findByRole("button", { name: "Condemned" })).toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "Example Franchise" })).not.toBeInTheDocument();
  });

  // Found on a real library. A card's chip set the collection directly and left the lane's
  // search applied, so the screen opened on a NARROWED subset under a fate summary that counts
  // the whole collection, wearing a search chip the operator set for the lane. The why panel's
  // chip routed through App's jump, which clears the search, so one chip did two things. Both
  // doors go through `openCollection` now; this drives the one that was wrong.
  it("drops the lane's search when a card's chip opens a collection, and puts it back", async () => {
    // Members that actually carry the chip, which is the control this test presses.
    const members = [
      movie(1, { collections: ["Example Franchise"] }),
      movie(2, { collections: ["Example Franchise"] }),
    ];
    apiMock.candidates.mockImplementation((verdict: string) =>
      Promise.resolve(page(verdict === "protect" || verdict === "abstain" ? [] : members)),
    );
    apiMock.latestSnapshot.mockResolvedValue({
      ...baseSnapshot,
      collection_sizes: { "Example Franchise": 2 },
    });
    const user = userEvent.setup();
    renderWithProviders(
      <ReviewQueue
        verdict="condemn"
        onVerdictChange={() => {}}
        selectedId={null}
        selectedGroupKey={null}
        onSelect={() => {}}
        onSelectGroup={() => {}}
      />,
    );
    const box = await screen.findByRole("searchbox", { name: /search titles/i });
    await user.type(box, "Example");
    await waitFor(() => expect(box).toHaveValue("Example"));

    // The card's own chip, the door that used to keep the search.
    await user.click((await screen.findAllByRole("button", { name: "Example Franchise" }))[0]!);

    expect(await screen.findByRole("heading", { name: "Example Franchise" })).toBeInTheDocument();
    expect(screen.getByRole("searchbox", { name: /search titles/i })).toHaveValue("");
    expect(screen.queryByRole("button", { name: /Stop searching for/i })).toBeNull();

    // ...and the lane is handed back exactly as it was left, which is what the exit promises.
    await user.click(screen.getByRole("button", { name: /Review queue/ }));
    await waitFor(() =>
      expect(screen.getByRole("searchbox", { name: /search titles/i })).toHaveValue("Example"),
    );
  });

  // The exit takes the lane tabs' own slot, so it reads as a control rather than as the tabs
  // having gone missing. Pinned by what it is NOT: a `.link-btn` is the lighter treatment this
  // replaced, and the tabs must be gone from the row it now occupies.
  it("puts a real control where the lane tabs were, not a lighter link", async () => {
    mockMixedFates();
    renderWithProviders(openOnCollection("Example Franchise"));
    const back = await screen.findByRole("button", { name: /Review queue/ });
    expect(back).toHaveClass("back-to-lane");
    expect(back).not.toHaveClass("link-btn");
    expect(screen.queryByRole("button", { name: "Condemned" })).not.toBeInTheDocument();
  });

  // A swap nothing focuses and nothing says is invisible to a keyboard or screen reader
  // operator: the rows just become different rows. Neither half shows up in a rendered diff,
  // so both are pinned here.
  it("moves focus to the collection's heading and says the list changed", async () => {
    mockMixedFates();
    renderWithProviders(
      <>
        <Announcer />
        {openOnCollection("Example Franchise")}
      </>,
    );
    const heading = await screen.findByRole("heading", { name: "Example Franchise" });
    await waitFor(() => expect(heading).toHaveFocus());
    expect(
      await screen.findByText("Showing the Example Franchise collection."),
    ).toBeInTheDocument();
  });

  // Rule 17/36: `isPending` alone clears on an ERROR exactly as it does on a success, so a fate
  // lane that exhausted its retries must not read as loaded with its count defaulted to 0 --
  // that undercounts "N in the last scan" and silently states a false zero for the failed lane.
  it("says the counts could not be read, rather than a false zero, when a lane's read fails", async () => {
    const { condemned, abstained } = mixedFateFixture();
    apiMock.candidates.mockImplementation((verdict: string) => {
      if (verdict === "any") return Promise.resolve(page([...condemned, ...abstained]));
      if (verdict === "condemn") return Promise.resolve(page(condemned));
      if (verdict === "protect") return Promise.reject(new Error("boom"));
      if (verdict === "abstain") return Promise.resolve(page(abstained));
      return Promise.resolve(page([]));
    });
    apiMock.latestSnapshot.mockResolvedValue({
      ...baseSnapshot,
      collection_sizes: { "Example Franchise": 8 },
    });
    renderWithProviders(openOnCollection("Example Franchise"));
    await screen.findByText("Example Movie 1");
    expect(
      await screen.findByText("Couldn't read the counts for this collection."),
    ).toBeInTheDocument();
    // Not "2 in the last scan" (an undercount of the real 3), and not one fate's real count
    // sitting beside the failed lane's missing one -- the whole summary is withheld together.
    expect(screen.queryByText(/in the last scan\./)).not.toBeInTheDocument();
    expect(screen.queryByText(/condemned/)).not.toBeInTheDocument();
    expect(screen.queryByText(/in Limbo/)).not.toBeInTheDocument();
  });
});

// The note that keeps a season's Spare/Reap honest when a WHOLE-SHOW decision is what really
// governs it: the control toggles the season's own decision, and this note says what the show
// is doing so the operator never fights a toggle that cannot reach the show-level choice. Its
// wording turns on the relationship between the season's own decision and its show's.
describe("the kept-by-the-whole-show note", () => {
  const render1 = (
    own: "spare" | "reap" | null,
    show: "spare" | "reap" | null,
    effective: boolean | null = null,
  ) => render(<KeptByShowNote own={own} showOverride={show} effective={effective} />);

  it("says nothing when no whole-show decision covers the season", () => {
    const { container } = render1(null, null);
    expect(container).toBeEmptyDOMElement();
    // A season with only its own decision (a movie's shape) shows no note either.
    render1("spare", null);
    expect(screen.queryByText(/whole show/i)).not.toBeInTheDocument();
  });

  it("explains an inherited spare and points to the show", () => {
    render1(null, "spare");
    expect(screen.getByText(/this season is kept/i)).toBeInTheDocument();
    expect(screen.getByText(/Undo it on the show/i)).toBeInTheDocument();
  });

  it("warns that clearing an own spare won't help while the show also spares it", () => {
    render1("spare", "spare");
    expect(screen.getByText(/clearing this one won't remove it/i)).toBeInTheDocument();
  });

  it("says a season reaped against a spared show will still be removed", () => {
    render1("reap", "spare");
    expect(screen.getByText(/will be removed/i)).toBeInTheDocument();
    expect(screen.getByText(/even though the whole show is spared/i)).toBeInTheDocument();
  });

  it("says a season spared against a reaped show stays, and what clearing it does", () => {
    render1("spare", "reap");
    expect(screen.getByText(/stays/i)).toBeInTheDocument();
    expect(screen.getByText(/even though the whole show is set to reap/i)).toBeInTheDocument();
    // The clause this note exists for: the control beside it is the ONLY thing keeping the
    // file, so clearing it drops the season onto the reap list. Without this the note warned
    // the operator in the harmless direction and went quiet in the destructive one.
    expect(screen.getByText(/goes back on the list/i)).toBeInTheDocument();
  });

  it("names the consequence of clearing in BOTH directions, never only the safe one", () => {
    // The asymmetry this pins: when clearing is harmless the note said so, and when clearing
    // put a file on the block it said nothing. Whichever way the sentences are later reworded,
    // neither clearable direction may go silent about what the click does.
    const safe = render1("spare", "spare");
    expect(safe.container.textContent).toMatch(/clear/i);
    safe.unmount();

    const destructive = render1("spare", "reap");
    expect(destructive.container.textContent).toMatch(/clear/i);
  });

  it("never promises removal for a reap the engine is holding (U-1)", () => {
    // A season reaped against a spared show, but the engine can't honor the reap yet
    // (streaming now): the note must say "kept for now", not "will be removed".
    render1("reap", "spare", false);
    expect(screen.getByText(/kept for now/i)).toBeInTheDocument();
    expect(screen.queryByText(/will be removed/i)).not.toBeInTheDocument();
  });

  it("says an inherited reap the engine can't honor yet is kept for now (U-1)", () => {
    render1(null, "reap", false);
    expect(screen.getByText(/kept for now/i)).toBeInTheDocument();
    expect(screen.queryByText(/will be removed/i)).not.toBeInTheDocument();
  });
});

// The row and card that hold OverrideControls also handle Enter/Space themselves (to open the
// why-panel). Keydown from the buttons must not bubble into that handler, whose preventDefault
// would cancel the button's own activation and open the panel instead of saving the override.
describe("what a screen reader hears on a queue card", () => {
  // Every card WAS its own control -- `<article role="button" aria-label="Why … scored …">` --
  // and ARIA gives `role="button"` Children Presentational: True, so everything inside was
  // pruned from the accessibility tree and replaced by that one label. The card's evidence IS
  // the case for deleting the file (#169). These drive the tree the way a reader reads it:
  // `getByRole`/`getByText` under Testing Library resolve against the accessibility tree, so a
  // pruned chip is a query that finds nothing.

  it("reads the card's evidence, not just its title and score", async () => {
    apiMock.candidates.mockResolvedValue(
      page([
        movie(1, {
          library: "Films",
          video_resolution: "1080",
          requested_by: "someone",
          reason: "Nobody has watched it since it arrived",
          chip: { tone: "look", text: "Nobody watched it" },
        }),
      ]),
    );
    renderQueue();

    // The control the card opens through, named as the card used to name itself.
    expect(
      await screen.findByRole("button", { name: "Why Example Movie 1 scored 80" }),
    ).toBeInTheDocument();
    // ...and everything the old label replaced. Each of these is a signal the operator is
    // deciding on; under the pruned card a reader reached none of them.
    expect(screen.getByText("Films")).toBeInTheDocument();
    expect(screen.getByText("1080p")).toBeInTheDocument();
    expect(screen.getByText(/someone/)).toBeInTheDocument();
    expect(screen.getByText(/Nobody has watched it since it arrived/)).toBeInTheDocument();
  });

  it("leaves no button nested inside another button", async () => {
    // `nested-interactive`: the real Spare and Reap controls sat inside the card's
    // `role="button"`, which is invalid and which leaves what a reader does with them undefined.
    apiMock.candidates.mockResolvedValue(page([movie(1)]));
    renderQueue();
    await screen.findByRole("button", { name: "Why Example Movie 1 scored 80" });

    // Rule 145: every button the card renders, counted rather than sampled -- a control that
    // dropped out of the walk is missing from the proof as surely as from the guard.
    const buttons = screen.getAllByRole("button");
    expect(buttons.length).toBeGreaterThan(3);
    for (const b of buttons) {
      expect(b.parentElement?.closest("button, [role='button']")).toBeNull();
    }
  });

  it("keeps the season list a list, so it announces how many seasons there are", async () => {
    // `<li role="button">` strips `listitem`, and a list of no items announces no count.
    apiMock.candidates.mockResolvedValue(page([season(1, "condemn"), season(2, "condemn")]));
    apiMock.group.mockResolvedValue({
      group_key: "sonarr:show:1",
      title: "Example Show",
      year: 2011,
      poster_url: null,
      summary: null,
      size_bytes: 2 * 1024 ** 3,
      unknown_size_seasons: 0,
      reason: null,
      library: null,
      chip: null,
      show_override: null,
      show_spare_expires_at: null,
      links: {},
      show_status: null,
      seasons: [season(1, "condemn"), season(2, "condemn")],
    });
    renderQueue();
    await userEvent.click(await screen.findByRole("button", { name: "2 seasons" }));

    const list = await screen.findByRole("list");
    expect(within(list).getAllByRole("listitem")).toHaveLength(2);
  });

  it("opens a card from the keyboard through its title control", async () => {
    apiMock.candidates.mockResolvedValue(page([movie(7)]));
    const onSelect = vi.fn();
    renderWithProviders(
      <ReviewQueue
        verdict="condemn"
        onVerdictChange={() => {}}
        selectedId={null}
        selectedGroupKey={null}
        onSelect={onSelect}
        onSelectGroup={() => {}}
        latestScanSnapshotId={null}
      />,
    );
    (await screen.findByRole("button", { name: "Why Example Movie 7 scored 80" })).focus();
    await userEvent.keyboard("{Enter}");

    // Exactly once. The control cancels the key's default action, so the click a `<button>`
    // would otherwise synthesize never fires and one press is one open.
    expect(onSelect).toHaveBeenCalledTimes(1);
    expect(onSelect).toHaveBeenCalledWith(7);
  });

  it("picks a card in Select mode from the keyboard, exactly once", async () => {
    // The other half of that: in Select mode the CARD's own pointerdown acts on a press, so the
    // control stands its click down and only the key path acts. Both firing toggled the card
    // straight back off, which is a selection that silently refuses to happen.
    apiMock.candidates.mockResolvedValue(page([movie(1), movie(2)]));
    renderQueue();
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "Select" }));

    const pick = screen.getByRole("button", { name: "Select Example Movie 1" });
    expect(pick).toHaveAttribute("aria-pressed", "false");
    pick.focus();
    await user.keyboard("{Enter}");

    expect(screen.getByRole("button", { name: "Select Example Movie 1" })).toHaveAttribute(
      "aria-pressed",
      "true",
    );
  });

  it("picks a card in Select mode with the mouse, exactly once", async () => {
    // And with a pointer, where the card's pointerdown is the thing that acts. A click on the
    // title must land on one net toggle, not two.
    apiMock.candidates.mockResolvedValue(page([movie(1), movie(2)]));
    renderQueue();
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "Select" }));

    await user.click(screen.getByRole("button", { name: "Select Example Movie 1" }));

    expect(screen.getByRole("button", { name: "Select Example Movie 1" })).toHaveAttribute(
      "aria-pressed",
      "true",
    );
  });
});

describe("keyboard activation of a revealed Spare/Reap button", () => {
  // These buttons used to sit INSIDE a `role="button"` card whose own key handler called
  // preventDefault, cancelling their activation -- so each carried a `stopPropagation` guard
  // (B-7, rule 60). The cards are plain containers now (#169) and the guards are gone with the
  // handler they guarded against; what these still pin is that the buttons work from a keyboard
  // and that pressing one never opens the panel behind it.
  it("saves the override", async () => {
    const onSet = vi.fn();
    // OverrideControls reads the default spare length from the general-settings query, so it
    // needs a client even in isolation; unresolved, the default reads as 0 (forever).
    renderWithProviders(
      <OverrideControls override={null} onSet={onSet} onClear={vi.fn()} pending={false} />,
    );
    screen.getByRole("button", { name: "Spare" }).focus();
    await userEvent.keyboard("{Enter}");

    // A plain Spare press carries the operator's default length; unknown settings read as 0
    // (forever), the safe default.
    expect(onSet).toHaveBeenCalledWith("spare", 0);
  });

  it("spares from the keyboard without also opening the card's reasons", async () => {
    // The whole failure B-7 named, driven through the real card rather than a stand-in row: a
    // press on Spare must save the decision and leave the operator where they are.
    apiMock.candidates.mockResolvedValue(page([movie(1)]));
    const onSelect = vi.fn();
    renderWithProviders(
      <ReviewQueue
        verdict="condemn"
        onVerdictChange={() => {}}
        selectedId={null}
        selectedGroupKey={null}
        onSelect={onSelect}
        onSelectGroup={() => {}}
        latestScanSnapshotId={null}
      />,
    );
    const spare = await screen.findByRole("button", { name: "Spare" });
    spare.focus();
    await userEvent.keyboard("{Enter}");

    await waitFor(() => expect(apiMock.override).toHaveBeenCalled());
    expect(onSelect).not.toHaveBeenCalled();
  });

  it("expands a show from the keyboard", async () => {
    // Enter on the season pill expands the show. It used to open the show panel instead,
    // because the card head canceled the pill's activation.
    apiMock.candidates.mockResolvedValue(page([season(1, "condemn"), season(2, "condemn")]));
    renderQueue("condemn");
    const expander = await screen.findByRole("button", { name: "2 seasons" });
    expect(expander).toHaveAttribute("aria-expanded", "false");

    expander.focus();
    await userEvent.keyboard("{Enter}");
    await waitFor(() => expect(expander).toHaveAttribute("aria-expanded", "true"));
  });
});

// An item-level control asks the ITEM, never the tab: lane membership is the effective
// verdict, so a row can sit on Condemned with a stored verdict that is not "condemn", and a
// spared condemnation has to stay flippable.
describe("a per-row control on the lane it does not match", () => {
  it("keeps Reap on a spared condemned movie, so the decision can be reversed", async () => {
    apiMock.candidates.mockResolvedValue(
      page([movie(1, { override: "spare", override_own: "spare" })]),
    );
    renderQueue("condemn");
    // `reapIsNoop` is false here (a spare is not already-condemned), so Reap stays. The tab
    // test hid it and stranded the spare with nothing to undo it (B-1).
    expect(await screen.findByRole("button", { name: "Spared" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /^Reap$/ })).toBeInTheDocument();
  });

  it("keeps the Reap control on a movie the lane holds by an honored hand reap", async () => {
    // Stored verdict abstain, on Condemned because the hand reap is honored. Hiding Reap here
    // left a resting scythe with no control to clear it.
    apiMock.candidates.mockResolvedValue(
      page([
        movie(1, {
          verdict: "abstain",
          override: "reap",
          override_own: "reap",
          override_effective: true,
        }),
      ]),
    );
    renderQueue("condemn");
    expect(await screen.findByRole("button", { name: "Reaping" })).toBeInTheDocument();
  });
});

// A card's prose follows the decision in EFFECT. A whole-show decision settles the card's
// story before its seasons' verdicts do, and a per-item spare must not silently drop a line.
describe("what a card says after a hand decision", () => {
  it("stops asserting removal the moment the whole show is spared", async () => {
    const marks: GroupSeasonMark[] = [
      {
        id: 1,
        season: 1,
        verdict: "condemn",
        override: null,
        override_effective: null,
        size_bytes: 1024 ** 3,
        spare_expires_at: null,
        spare_covers_until: null,
      },
      {
        id: 2,
        season: 2,
        verdict: "condemn",
        override: null,
        override_effective: null,
        size_bytes: 1024 ** 3,
        spare_expires_at: null,
        spare_covers_until: null,
      },
      {
        id: 3,
        season: 3,
        verdict: "protect",
        override: null,
        override_effective: null,
        size_bytes: 1024 ** 3,
        spare_expires_at: null,
        spare_covers_until: null,
      },
    ];
    apiMock.candidates.mockResolvedValue(
      page(
        [season(1, "condemn"), season(2, "condemn")],
        [rollup(marks, { condemned_count: 2, condemned_bytes: 2 * 1024 ** 3 })],
      ),
    );
    apiMock.override.mockResolvedValue({});
    const user = userEvent.setup();
    renderQueue("condemn");
    await screen.findByText("Example Show");
    expect(screen.getByText(/2 of 3 would be removed/)).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Spare" }));

    // The whole-show patch touches `show_override` only, by design, so a card reading its
    // seasons' verdicts kept promising removal under a "will be kept" chip, all session
    // (B-12, rule 61).
    await waitFor(() => expect(screen.queryByText(/would be removed/)).not.toBeInTheDocument());
  });

  it("counts the whole show the moment it is reaped by hand", async () => {
    // The reap direction of the case above. A Sanctuary show's rollup is a real 0, and the
    // whole-show patch refetches nothing by design, so reading the rollup here printed
    // "0 of 3 would be removed, 0 B" beneath a "will be removed" chip for the rest of the
    // session, while the server would in fact plan every season it honors (B-1).
    const marks: GroupSeasonMark[] = [
      {
        id: 1,
        season: 1,
        verdict: "protect",
        override: null,
        override_effective: null,
        size_bytes: 1024 ** 3,
        spare_expires_at: null,
        spare_covers_until: null,
      },
      {
        id: 2,
        season: 2,
        verdict: "protect",
        override: null,
        override_effective: null,
        size_bytes: 1024 ** 3,
        spare_expires_at: null,
        spare_covers_until: null,
      },
      {
        id: 3,
        season: 3,
        verdict: "protect",
        override: null,
        override_effective: null,
        size_bytes: 1024 ** 3,
        spare_expires_at: null,
        spare_covers_until: null,
      },
    ];
    apiMock.candidates.mockResolvedValue(
      page([season(1, "protect"), season(2, "protect"), season(3, "protect")], [rollup(marks)]),
    );
    apiMock.override.mockResolvedValue({});
    const user = userEvent.setup();
    renderQueue("protect");
    await screen.findByText("Example Show");
    expect(screen.queryByText(/would be removed/)).not.toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Reap" }));

    // The whole show, sized from its own marks, not the stale rollup's 0.
    expect(await screen.findByText(/3 of 3 would be removed, 3\.0 GiB/)).toBeInTheDocument();
    expect(screen.queryByText(/0 of 3 would be removed/)).not.toBeInTheDocument();
  });

  it("counts only the seasons a whole-show reap actually reaches", async () => {
    // The settled state, once the refetch has put the inherited reap and its refusals on the
    // marks. A whole-show decision is not atomic: a season the operator spared individually
    // keeps its own decision (rule 50), and one the engine refuses comes back
    // override_effective false and is dropped from the server rollup AND the planner's
    // expansion. Counting the show whole printed "3 of 3 would be removed" above a chip
    // reading "kept for now" and dashed-red refused squares, and left it there (rules 49/61).
    const gb = 1024 ** 3;
    const marks: GroupSeasonMark[] = [
      // Honored: the only one that will actually go.
      {
        id: 1,
        season: 1,
        verdict: "protect",
        override: "reap",
        override_effective: true,
        size_bytes: gb,
        spare_expires_at: null,
        spare_covers_until: null,
      },
      // Refused by the engine -- a hand reap it cannot honor yet.
      {
        id: 2,
        season: 2,
        verdict: "protect",
        override: "reap",
        override_effective: false,
        size_bytes: gb,
        spare_expires_at: null,
        spare_covers_until: null,
      },
      // The operator's own opposing spare.
      {
        id: 3,
        season: 3,
        verdict: "protect",
        override: "spare",
        override_effective: null,
        size_bytes: gb,
        spare_expires_at: null,
        spare_covers_until: null,
      },
    ];
    const extra = { show_override: "reap" as const };
    apiMock.candidates.mockResolvedValue(
      page(
        [season(1, "protect", extra), season(2, "protect", extra), season(3, "protect", extra)],
        [rollup(marks, { condemned_count: 1, condemned_bytes: gb })],
      ),
    );
    renderQueue("protect");

    expect(await screen.findByText(/1 of 3 would be removed, 1\.0 GiB/)).toBeInTheDocument();
    expect(screen.queryByText(/3 of 3 would be removed/)).not.toBeInTheDocument();
  });

  it("still counts out a season whose own spare has expired", async () => {
    // An expired spare keeps the season exactly as a live one does: the server drops it from
    // the show's rollup on "is it spared", and the planner reads the same live whitelist,
    // where the row survives until a scan purges it. Counting it as removable printed a
    // number the reap would not act on, one line under the dashed-green square that says the
    // season is kept (rules 30/62). The card's count and its strip must agree.
    const gb = 1024 ** 3;
    const past = new Date(Date.now() - 3 * 86_400_000).toISOString();
    const marks: GroupSeasonMark[] = [
      {
        id: 1,
        season: 1,
        verdict: "protect",
        override: "reap",
        override_effective: true,
        size_bytes: gb,
        spare_expires_at: null,
        spare_covers_until: null,
      },
      // Spared by hand for a set time, and that time has run out.
      {
        id: 2,
        season: 2,
        verdict: "protect",
        override: "spare",
        override_effective: null,
        size_bytes: gb,
        spare_expires_at: past,
        spare_covers_until: past,
      },
    ];
    const extra = { show_override: "reap" as const };
    apiMock.candidates.mockResolvedValue(
      page(
        [season(1, "protect", extra), season(2, "protect", extra)],
        [rollup(marks, { condemned_count: 1, condemned_bytes: gb })],
      ),
    );
    renderQueue("protect");

    expect(await screen.findByText(/1 of 2 would be removed, 1\.0 GiB/)).toBeInTheDocument();
    expect(screen.queryByText(/2 of 2 would be removed/)).not.toBeInTheDocument();
  });

  it("keeps the dormancy line when a condemned movie is spared", async () => {
    // A condemned row carries no chip by construction, so the spare flipped the card to the
    // chip branch and it lost a line and reflowed under the cursor (B-24).
    apiMock.candidates.mockResolvedValue(page([movie(1, { dormant_for: "3 years 2 months" })]));
    apiMock.override.mockResolvedValue({});
    const user = userEvent.setup();
    renderQueue("condemn");
    await screen.findByText(/Not watched in/);

    await user.click(screen.getByRole("button", { name: "Spare" }));

    await waitFor(() => expect(screen.getByRole("button", { name: "Spared" })).toBeInTheDocument());
    expect(screen.getByText(/Not watched in/)).toBeInTheDocument();
  });
});

// The Spare chevron opens a length menu: quick day-presets, Forever, and a Custom entry. Each
// pick spares at that length, so the menu is the action, not a form.
describe("the Spare length menu", () => {
  function renderControls(onSet = vi.fn()) {
    renderWithProviders(
      <OverrideControls override={null} onSet={onSet} onClear={vi.fn()} pending={false} />,
    );
    return onSet;
  }

  it("spares for a chosen preset length", async () => {
    const user = userEvent.setup();
    const onSet = renderControls();
    await user.click(screen.getByRole("button", { name: "Choose how long to keep it" }));
    await user.click(screen.getByRole("button", { name: /90 days/ }));
    expect(onSet).toHaveBeenCalledWith("spare", 90);
  });

  it("spares forever when Forever is picked", async () => {
    const user = userEvent.setup();
    const onSet = renderControls();
    await user.click(screen.getByRole("button", { name: "Choose how long to keep it" }));
    await user.click(screen.getByRole("button", { name: /Forever/ }));
    expect(onSet).toHaveBeenCalledWith("spare", 0);
  });

  it("spares for a custom number of days", async () => {
    const user = userEvent.setup();
    const onSet = renderControls();
    await user.click(screen.getByRole("button", { name: "Choose how long to keep it" }));
    await user.click(screen.getByRole("button", { name: /Custom length/ }));
    // "Custom spare length", not "...in days": the box is `FixedQuantity` now, which binds the
    // visible "days" suffix as its description, so the unit is spoken after the value instead of
    // being folded into the name.
    const box = screen.getByLabelText("Custom spare length");
    await user.clear(box);
    await user.type(box, "45");
    // Scope to the menu: the row's own split Spare button is also named "Spare".
    await user.click(
      within(screen.getByRole("group", { name: "Spare this item for" })).getByRole("button", {
        name: "Spare",
      }),
    );
    expect(onSet).toHaveBeenCalledWith("spare", 45);
  });
});

// One ＋ Filter control replaces the old row of fixed dropdowns: any filter is added from a
// menu, shows as an editable chip, and is removed from the chip. A new filter is a registry
// entry, so this one flow covers them all.
describe("the unified filter bar", () => {
  it("adds a filter from the ＋ Filter menu, edits its value, then removes it", async () => {
    apiMock.vocabularyValues.mockImplementation((field: string) =>
      Promise.resolve({ field, values: field === "library" ? ["Movies", "4K Movies"] : [] }),
    );
    apiMock.candidates.mockResolvedValue(page([movie(1)]));
    const user = userEvent.setup();
    renderQueue();
    await screen.findByText("Example Movie 1");

    // Add the Library filter. Its options come from the scan's vocabulary, so the menu entry
    // appears once that query resolves.
    await user.click(screen.getByRole("button", { name: "Filter" }));
    await user.click(await screen.findByRole("button", { name: "Library" }));

    // The candidates query now carries the library, and the filter is a removable chip.
    await waitFor(() =>
      expect(apiMock.candidates).toHaveBeenCalledWith(
        "condemn",
        expect.objectContaining({ library: "Movies" }),
        expect.anything(),
        expect.anything(),
      ),
    );
    expect(screen.getByRole("button", { name: "Remove the Library filter" })).toBeInTheDocument();

    // Clicking the chip opens its value picker; choosing another value re-filters in place.
    await user.click(screen.getByRole("button", { name: "Movies" }));
    await user.click(await screen.findByRole("button", { name: "4K Movies" }));
    await waitFor(() =>
      expect(apiMock.candidates).toHaveBeenCalledWith(
        "condemn",
        expect.objectContaining({ library: "4K Movies" }),
        expect.anything(),
        expect.anything(),
      ),
    );

    // Removing the chip drops the filter entirely.
    await user.click(screen.getByRole("button", { name: "Remove the Library filter" }));
    await waitFor(() =>
      expect(
        screen.queryByRole("button", { name: "Remove the Library filter" }),
      ).not.toBeInTheDocument(),
    );
    expect(apiMock.candidates).toHaveBeenLastCalledWith(
      "condemn",
      expect.objectContaining({ library: "" }),
      expect.anything(),
      expect.anything(),
    );
  });
});

describe("switching tabs", () => {
  // Each tab remembers its own filters, and the new tab's set is adopted during the render
  // that brings the new verdict in. When that adoption lived in an effect, the new verdict
  // was paired with the OLD tab's filters for one commit: the queue drew a wrong list and
  // the server answered the same switch twice (B-30).
  const store = new Map<string, string>();

  function installStorage(): void {
    Object.defineProperty(window, "localStorage", {
      configurable: true,
      value: {
        getItem: (key: string) => store.get(key) ?? null,
        setItem: (key: string, value: string) => void store.set(key, String(value)),
        removeItem: (key: string) => void store.delete(key),
        clear: () => store.clear(),
      },
    });
  }

  /** The queue alone, so `rerender` re-wraps it in the providers the first render mounted it
   *  under. The tab switch this describe is about happens in one app against one cache, and
   *  handing the re-render a fresh client would drop every read the old tab had made. */
  function queue(verdict: Verdict) {
    return (
      <ReviewQueue
        verdict={verdict}
        onVerdictChange={() => {}}
        selectedId={null}
        selectedGroupKey={null}
        onSelect={() => {}}
        onSelectGroup={() => {}}
      />
    );
  }

  it("never asks the server for the new tab with the old tab's filters", async () => {
    store.clear();
    installStorage();
    // Condemned remembers a genre; Sanctuary remembers nothing.
    store.set(
      "reaper.queue.filters.condemn",
      JSON.stringify({ ...DEFAULT_FILTERS, genre: "Example Genre" }),
    );
    apiMock.candidates.mockResolvedValue(page([movie(1)]));

    const { rerender } = renderWithProviders(queue("condemn"));
    await waitFor(() =>
      expect(apiMock.candidates).toHaveBeenCalledWith(
        "condemn",
        expect.objectContaining({ genre: "Example Genre" }),
        expect.anything(),
        expect.anything(),
      ),
    );

    rerender(queue("protect"));
    await waitFor(() =>
      expect(apiMock.candidates).toHaveBeenLastCalledWith(
        "protect",
        expect.objectContaining({ genre: "" }),
        expect.anything(),
        expect.anything(),
      ),
    );
    // The wrong pair is never requested at all -- not even for the one render it used to
    // survive: it would have both drawn a list and cost the server a query.
    const wrongPair = apiMock.candidates.mock.calls.filter(
      (c) => c[0] === "protect" && (c[1] as { genre: string }).genre === "Example Genre",
    );
    expect(wrongPair).toEqual([]);
  });
});

// Both filter popovers used to render `role="menu"`/`menuitem` and `role="listbox"`/`option`
// while implementing neither contract: no arrow keys, no roving focus, no `aria-activedescendant`,
// and every option its own Tab stop, which is not the listbox pattern at all. A listbox is
// ANNOUNCED as an arrow-key widget, so the role told an operator to press keys that did nothing.
describe("the filter popovers' keyboard contract", () => {
  // These add filters on purpose, and the queue remembers them. Runs even on a failing test.
  // Wrapped, not passed bare: vitest hands the hook its test context, which would arrive as the
  // `verdict` and clear a key spelled after an object.
  afterEach(() => forgetFilters());

  it("hands focus back to the trigger when Escape closes the ＋ Filter menu", async () => {
    apiMock.candidates.mockResolvedValue(page([movie(1)]));
    const user = userEvent.setup();
    renderQueue();
    await screen.findByText("Example Movie 1");

    const trigger = screen.getByRole("button", { name: "Filter" });
    await user.click(trigger);
    expect(screen.getByRole("list", { name: "Add a filter" })).toBeInTheDocument();

    await user.keyboard("{Escape}");

    expect(screen.queryByRole("list", { name: "Add a filter" })).not.toBeInTheDocument();
    // Without this the next Tab restarts at the top of the document, above the whole queue.
    expect(trigger).toHaveFocus();
  });

  it("consumes Escape rather than letting an open reasons panel close too", async () => {
    // The popover's Escape sits on `document`, which bubbles on to `window`, where an open `.why`
    // panel's own Escape sits (WhyShell) -- and the queue and that panel are on screen together in
    // split view. One press must not close both layers (rule 72: the spare-length menu stops the
    // same key for the same reason).
    apiMock.candidates.mockResolvedValue(page([movie(1)]));
    const user = userEvent.setup();
    renderQueue();
    await screen.findByText("Example Movie 1");

    await user.click(screen.getByRole("button", { name: "Filter" }));
    const onWindow = vi.fn();
    window.addEventListener("keydown", onWindow);
    await user.keyboard("{Escape}");
    window.removeEventListener("keydown", onWindow);

    expect(screen.queryByRole("list", { name: "Add a filter" })).not.toBeInTheDocument();
    expect(onWindow).not.toHaveBeenCalled();
  });

  it("moves focus to the new chip when the last filter takes the ＋ Filter button with it", async () => {
    // The button renders only while something is still addable, so adding the LAST dimension
    // unmounts the very control the focus return aims at: `.focus()` lands on a node React removes
    // in the next commit and the operator is dropped on <body>. The chip the press just created is
    // the successor. Escape's exit above cannot catch this -- it never removes the trigger.
    apiMock.candidates.mockResolvedValue(page([movie(1)]));
    const user = userEvent.setup();
    renderQueue();
    await screen.findByText("Example Movie 1");

    // Add every addable dimension, whatever the vocabulary leaves addable, until the button goes.
    for (;;) {
      const trigger = screen.queryByRole("button", { name: "Filter" });
      if (trigger === null) break;
      await user.click(trigger);
      const menu = screen.getByRole("list", { name: "Add a filter" });
      await user.click(within(menu).getAllByRole("button")[0]!);
    }

    const focused = document.activeElement as HTMLElement;
    expect(focused).not.toBe(document.body);
    expect(focused.className).toContain("fchip-body");
  });

  it("promises no ARIA menu or listbox contract, and says what it controls instead", async () => {
    apiMock.vocabularyValues.mockImplementation((field: string) =>
      Promise.resolve({ field, values: field === "library" ? ["Movies", "4K Movies"] : [] }),
    );
    apiMock.candidates.mockResolvedValue(page([movie(1)]));
    const user = userEvent.setup();
    renderQueue();
    await screen.findByText("Example Movie 1");

    const trigger = screen.getByRole("button", { name: "Filter" });
    expect(trigger).not.toHaveAttribute("aria-haspopup");
    await user.click(trigger);
    expect(screen.queryByRole("menu")).not.toBeInTheDocument();
    expect(screen.queryAllByRole("menuitem")).toHaveLength(0);
    expect(trigger.getAttribute("aria-controls")).toBe(
      screen.getByRole("list", { name: "Add a filter" }).id,
    );

    // The chip's value picker, the one that claimed to be a listbox.
    await user.click(await screen.findByRole("button", { name: "Library" }));
    const chip = screen.getByRole("button", { name: "Movies" });
    expect(chip).not.toHaveAttribute("aria-haspopup");
    await user.click(chip);
    // Scoped to the popover: the toolbar's real `<select>`s expose native `option`s, which are
    // correct and are a different population from the buttons this popover renders.
    const picker = screen.getByRole("list", { name: "Library" });
    expect(screen.queryByRole("listbox")).not.toBeInTheDocument();
    expect(within(picker).queryAllByRole("option")).toHaveLength(0);
    // Which value is in force is still stated -- as `aria-current`, which promises no arrow keys.
    expect(screen.getByRole("button", { name: "Movies", current: true })).toBeInTheDocument();
  });
});

describe("one row's write does not disable another row's controls", () => {
  // `pending` was the OR of every in-flight override mutation, so a spare on ANY row disabled
  // the Spare and Reap on ALL of them. Disabling the focused element drops focus to `<body>` in
  // every major browser, and the `aria-pressed` flip on Spare is the app's only announcement
  // that a spare succeeded -- so by the time the state settled there was no focused element to
  // announce it, and the press that KEEPS a file confirmed itself to nobody (#173).
  it("keeps the pressed row's own button focused, so its aria-pressed flip is announced", async () => {
    let settle: (v: unknown) => void = () => {};
    apiMock.candidates.mockResolvedValue(page([movie(1), movie(2)]));
    apiMock.override.mockImplementation(() => new Promise((r) => (settle = r)));
    renderQueue();
    const user = userEvent.setup();

    const spares = await screen.findAllByRole("button", { name: "Spare" });
    const mine = spares[0]!;
    mine.focus();
    await user.click(mine);

    // Disabling the pressed button is what drops focus to `<body>`, and the row's own write is
    // exactly when that happens -- so scoping `pending` per row does not cover this case and
    // `OverrideControls` re-issues the focus when its own wait ends.
    await act(async () => {
      settle({});
      await Promise.resolve();
    });
    await waitFor(() => expect(mine).toHaveAttribute("aria-pressed", "true"));
    // Standing on the control whose state just changed, which is what makes the flip audible.
    expect(mine).toHaveFocus();
  });

  it("leaves the OTHER row's controls pressable while one row is writing", async () => {
    let settle: (v: unknown) => void = () => {};
    apiMock.candidates.mockResolvedValue(page([movie(1), movie(2)]));
    apiMock.override.mockImplementation(() => new Promise((r) => (settle = r)));
    renderQueue();
    const user = userEvent.setup();

    const spares = await screen.findAllByRole("button", { name: "Spare" });
    await user.click(spares[0]!);

    // One row's in-flight spare has nothing to say about another row's decision.
    expect(spares[1]!).toBeEnabled();
    await act(async () => {
      settle({});
      await Promise.resolve();
    });
  });

  // Scoping `pending` per row keyed it on the key each surface WRITES -- and the season rows
  // inside a show card were handed the SHOW's boolean, which their own `media_key` can never
  // equal. So the one row where a per-season keep-or-delete decision is made was the one row
  // whose buttons stayed live through their own round trip, and a second press sent a second,
  // contradicting decision (rule 72: `MovieCard` and the whole-show control were both keyed).
  it("disables a season row's own controls while that season's write is in flight", async () => {
    let settle: (v: unknown) => void = () => {};
    apiMock.candidates.mockResolvedValue(page([season(1, "condemn"), season(2, "condemn")]));
    apiMock.group.mockResolvedValue({
      group_key: "sonarr:show:1",
      title: "Example Show",
      year: 2011,
      poster_url: null,
      summary: null,
      size_bytes: 2 * 1024 ** 3,
      unknown_size_seasons: 0,
      reason: null,
      library: null,
      chip: null,
      show_override: null,
      show_spare_expires_at: null,
      links: {},
      show_status: null,
      seasons: [season(1, "condemn"), season(2, "condemn")],
    });
    apiMock.override.mockImplementation(() => new Promise((r) => (settle = r)));
    renderQueue();
    const user = userEvent.setup();

    await user.click(await screen.findByRole("button", { name: "2 seasons" }));
    const rows = within(await screen.findByRole("list")).getAllByRole("listitem");
    const mine = within(rows[0]!).getByRole("button", { name: "Spare" });
    const other = within(rows[1]!).getByRole("button", { name: "Spare" });
    await user.click(mine);

    // The acting row says it is acting, and cannot be asked a second time. `user-event` reports
    // a click on a disabled control as success (rule 137), so the write count is what proves it.
    expect(mine).toBeDisabled();
    await user.click(mine);
    expect(apiMock.override).toHaveBeenCalledTimes(1);
    // The show's other season is not writing and stays pressable, which is what scoping the
    // wait buys over restoring the old list-wide `pending`.
    expect(other).toBeEnabled();

    await act(async () => {
      settle({});
      await Promise.resolve();
    });
  });
});

describe("what a reviewer hears when a scan lands under an open review", () => {
  // The nudge and the catch-up toast were bare `role="status"` nodes mounted in the same commit as
  // their own text. Several readers only watch regions that were already there, so both looked
  // correct and said nothing -- the bug `Notice` reached for `role="alert"` to avoid. The role is
  // gone (it was also wrapping two focusable buttons) and the sentence goes through the shared
  // region instead (#177).
  const spoken = () =>
    [...document.querySelectorAll('[aria-live="polite"]')].map((n) => n.textContent).join("");

  function renderWithAnnouncer() {
    apiMock.candidates.mockResolvedValue(page([movie(1)]));
    renderWithProviders(
      <>
        <Announcer />
        <ReviewQueue
          verdict="condemn"
          onVerdictChange={() => {}}
          selectedId={null}
          selectedGroupKey={null}
          onSelect={() => {}}
          onSelectGroup={() => {}}
          latestScanSnapshotId={2}
        />
      </>,
    );
  }

  it("says a newer scan finished, in the nudge's own words", async () => {
    renderWithAnnouncer();

    await screen.findByText("A newer scan just finished");

    await waitFor(() => expect(spoken()).toContain("A newer scan just finished."));
    expect(spoken()).toContain("You're viewing the previous scan.");
  });

  it("does not leave a live-region role on a node mounted with its text", async () => {
    // The role is what made this read as solved. Pinned by absence, with the reason in the
    // comment above, so re-adding it fails here rather than shipping a second silent region.
    renderWithAnnouncer();
    await screen.findByText("A newer scan just finished");

    expect(document.querySelector(".scan-nudge")).not.toBeNull();
    expect(document.querySelector('.scan-nudge[role="status"]')).toBeNull();
    expect(document.querySelector('.scan-toast[role="status"]')).toBeNull();
  });
});

describe("the search box a jump aims at this queue", () => {
  // A jump from another section (Scales today) names a whole destination -- lane, what to open,
  // and what to search for -- and this queue is the half that reads the search. The lane alone
  // can be thousands of rows deep, so seeding the box is what puts the opened title's own card
  // on screen behind its panel instead of leaving the operator to find it.
  const focused = (search: string, nonce: number) => (
    <ReviewQueue
      verdict="condemn"
      onVerdictChange={() => {}}
      selectedId={null}
      selectedGroupKey={null}
      onSelect={() => {}}
      onSelectGroup={() => {}}
      focus={{ search, nonce }}
    />
  );

  /** The `search` every call to the candidates endpoint carried, oldest first. */
  const searches = () =>
    apiMock.candidates.mock.calls.map((c) => (c[1] as { search?: string } | undefined)?.search);

  it("asks for the searched list and never the whole lane first", async () => {
    // The load-bearing part, and why the term seeds `useState` rather than arriving in an
    // effect: this queue is UNMOUNTED while the operator is on Scales, so a jump mounts it.
    // An effect runs after the first render has already fired its query, which means one
    // request for the whole condemned lane -- and one paint of it -- before the seeded one
    // replaces it. The list must arrive filtered.
    apiMock.candidates.mockResolvedValue(page([movie(1)]));
    renderWithProviders(focused("Example Movie 1 2011", 7));
    await screen.findByText("Example Movie 1");
    expect(searches()).toEqual(["Example Movie 1 2011"]);
    // And the box shows what it searched for, so the operator can widen it.
    expect(screen.getByRole("searchbox", { name: /search titles/i })).toHaveValue(
      "Example Movie 1 2011",
    );
  });

  it("applies a jump that arrives while the queue is already on screen", async () => {
    apiMock.candidates.mockResolvedValue(page([movie(1)]));
    const { rerender } = renderWithProviders(focused("", 1));
    await screen.findByText("Example Movie 1");
    rerender(focused("Example Movie 1 2011", 2));
    await waitFor(() => expect(searches()).toContain("Example Movie 1 2011"));
  });

  it("does not re-seed the box on a re-render carrying the same jump", async () => {
    // The nonce is what "once" is counted with. Without it every unrelated re-render would
    // put the jump's term back, and typing over it would be undone a keystroke later.
    apiMock.candidates.mockResolvedValue(page([movie(1)]));
    const { rerender } = renderWithProviders(focused("Example Movie 1 2011", 7));
    const box = await screen.findByRole("searchbox", { name: /search titles/i });
    await userEvent.clear(box);
    await userEvent.type(box, "something else");
    rerender(focused("Example Movie 1 2011", 7));
    expect(box).toHaveValue("something else");
  });
});

describe("what the search box calls itself", () => {
  // The placeholder is this box's only visible label -- there is no <label> and no visible
  // heading naming it -- so the accessible name has to repeat it word for word. Someone driving
  // the page by voice says what they can read, and a name reading "and years" where the screen
  // reads "years" is a control they cannot ask for (WCAG 2.5.3 Label in Name). Derived from the
  // element rather than spelled twice here, so the two can only be changed together; LogsPanel's
  // box is the sibling that already pairs this way.
  it("names itself with the words on screen, so it can be asked for by voice", async () => {
    apiMock.candidates.mockResolvedValue(page([movie(1)]));
    renderWithProviders(
      <ReviewQueue
        verdict="condemn"
        onVerdictChange={() => {}}
        selectedId={null}
        selectedGroupKey={null}
        onSelect={() => {}}
        onSelectGroup={() => {}}
      />,
    );
    const box = await screen.findByRole("searchbox", { name: /search titles/i });
    // The ellipsis is the one difference the visible copy is allowed: it says "keep typing",
    // and a screen reader has no use for it.
    expect(box.getAttribute("aria-label")).toBe(
      (box.getAttribute("placeholder") ?? "").replace(/…$/, ""),
    );
  });
});
