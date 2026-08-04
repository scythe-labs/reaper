// SPDX-License-Identifier: AGPL-3.0-or-later
// Settings -> Lists: what a protection list's row says, and what its buttons do (#475).
//
// The point of the screen is that a list which stopped protecting reads DIFFERENTLY from one
// that is simply not on this title's side, so each state is driven here and asserted on its
// sentence, not only on its chip. The chip is four words; the sentence is what tells the
// operator whether to go and fix something now or at the weekend.
//
// The state itself is the server's (`lists.ListHealth`) and is not recomputed here -- that is
// the whole reason it is decided once. What is pinned is the copy each state produces, and the
// branches this component does own: `item_count` on a failing list, which is the difference
// between "your titles are still covered" and "nothing on this list is protected"; the
// policy-use line, which is what tells the operator a defined list protects nothing; and the
// definition-to-membership join, which is what lets a row carry Edit and Check now at all.
import { QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { expectNoA11yViolations } from "../test/a11y";
import { testQueryClient } from "../test/queryClient";
import type { ListConfig, ListPolicyUse, ProtectionList } from "../api";
import { ListsPanel } from "./ListsPanel";

// Rule 135: every read the tree under test performs, including the modal's, or React Query
// renders a failed read and the test passes against the fallback.
const { apiMock } = vi.hoisted(() => ({
  apiMock: {
    lists: vi.fn(),
    listConfigs: vi.fn(),
    syncLists: vi.fn(),
    addList: vi.fn(),
    editList: vi.fn(),
    removeList: vi.fn(),
    plexLibraries: vi.fn(),
    syncPlexLibraries: vi.fn(),
  },
}));
vi.mock("../api", () => ({ api: apiMock }));

/** The default hard use: one keeps-it-outright rule per media type, the shape a fresh list
 *  gets server-side. Two entries, one sentence -- the panel deduplicates. */
const HARD_USE: ListPolicyUse[] = [
  { media_type: "movie", strength: "hard", points: null },
  { media_type: "tv", strength: "hard", points: null },
];

/** The shipped IMDb chart, as a definition. */
const IMDB_DEF: ListConfig = {
  id: 1,
  name: "IMDb Top 250",
  source: "imdb",
  config: { preset: "top250" },
  policy_use: HARD_USE,
};

/** Its membership row, joined on `list_id`. */
const WORKING: ProtectionList = {
  slug: "imdb-top-250-list1",
  name: "IMDb Top 250",
  source: "imdb",
  state: "working",
  item_count: 250,
  last_checked_at: new Date(Date.now() - 8 * 60_000).toISOString(),
  error: null,
  list_id: 1,
  tags: null,
  server: null,
};

/** A Plex collection the operator defined, which is the shape #483 was about. */
const PLEX_DEF: ListConfig = {
  id: 2,
  name: "Never Reap",
  source: "plex_collection",
  config: { library: "Films", collection: "Never Reap" },
  policy_use: HARD_USE,
};

/** A tag list, read once per *arr instance. */
const TAG_DEF: ListConfig = {
  id: 3,
  name: "Titles you've tagged",
  source: "arr_tag",
  config: { tags: ["reaper-keep", "keep-forever"], match: "any" },
  policy_use: HARD_USE,
};

function tagRow(slug: string, over: Partial<ProtectionList> = {}): ProtectionList {
  return {
    slug,
    name: "tag: reaper-keep",
    source: "arr_tag",
    state: "working",
    item_count: 4,
    last_checked_at: new Date(Date.now() - 60 * 60_000).toISOString(),
    error: null,
    list_id: 3,
    tags: null,
    server: null,
    ...over,
  };
}

function renderPanel(onGoToPolicy = vi.fn()) {
  render(
    <QueryClientProvider client={testQueryClient()}>
      <ListsPanel onGoToPolicy={onGoToPolicy} />
    </QueryClientProvider>,
  );
  return { onGoToPolicy };
}

/** Both reads answered. Every test needs both, because the screen renders one row per
 *  definition and reads its health from the other call. */
function seed(definitions: ListConfig[], rows: ProtectionList[]) {
  apiMock.listConfigs.mockResolvedValue(definitions);
  apiMock.lists.mockResolvedValue(rows);
}

beforeEach(() => {
  Object.values(apiMock).forEach((fn) => fn.mockReset());
  apiMock.plexLibraries.mockResolvedValue([]);
  apiMock.syncPlexLibraries.mockResolvedValue([]);
  apiMock.syncLists.mockResolvedValue({ checked: 1, failed: 0, plex_error: null });
});

describe("the Lists panel", () => {
  it("has no accessibility violations", async () => {
    seed([IMDB_DEF], [WORKING]);
    renderPanel();
    expect(await screen.findByText("IMDb Top 250")).toBeInTheDocument();
    await expectNoA11yViolations();
  });

  it("says what a working list is protecting, and when it last checked", async () => {
    seed([IMDB_DEF], [WORKING]);
    renderPanel();

    expect(await screen.findByText("IMDb Top 250")).toBeInTheDocument();
    expect(screen.getByText("Working")).toBeInTheDocument();
    expect(screen.getByText(/Protecting 250 titles\./)).toBeInTheDocument();
    expect(screen.getByText(/Last checked 8 minutes ago\./)).toBeInTheDocument();
  });

  it("shows the service's own words for a list that is not working", async () => {
    // The issue, in one assertion. This message names the thing to go and fix, and until this
    // screen existed it was written to `last_error` on every failed sync and read by nothing.
    seed(
      [PLEX_DEF],
      [
        {
          ...WORKING,
          slug: "plex-collection-never-reap-list2",
          name: "Never Reap",
          source: "plex_collection",
          state: "failing",
          item_count: 0,
          last_checked_at: null,
          error: "there is no library called 'Movies'",
          list_id: 2,
        },
      ],
    );
    renderPanel();

    expect(await screen.findByText("Not working")).toBeInTheDocument();
    expect(screen.getByText("there is no library called 'Movies'")).toBeInTheDocument();
    expect(screen.getByText(/protecting nothing/)).toBeInTheDocument();
    // Nothing has ever landed, so there is no "last checked" clause to render a bogus date in.
    expect(screen.queryByText(/Last checked/)).not.toBeInTheDocument();
  });

  it("distinguishes a failing list that is still covering its titles", async () => {
    // The branch this component owns. Same state, opposite urgency: the atomic swap in `sync`
    // left the previous membership in place, so those titles are still protected and the
    // operator does not have to drop everything.
    seed(
      [IMDB_DEF],
      [{ ...WORKING, state: "failing", item_count: 37, error: "Sonarr refused the request" }],
    );
    renderPanel();

    expect(await screen.findByText("Not working")).toBeInTheDocument();
    expect(
      screen.getByText(/37 titles from the last good check are still protected/),
    ).toBeInTheDocument();
    expect(screen.queryByText(/protecting nothing/)).not.toBeInTheDocument();
  });

  it("does not call an empty list working, however well the check went", async () => {
    // Found by driving a real install: three keep lists sat green at 0 titles, one of them a
    // "Never Reap" collection. The sync genuinely succeeded, so the server's `working` is
    // correct about the check -- and green there says "you are covered" to someone covered by
    // nothing, which is indistinguishable from Reaper reading the wrong library (#483) or from
    // a list whose entries it cannot identify (#474).
    seed([PLEX_DEF], [{ ...WORKING, source: "plex_collection", item_count: 0, list_id: 2 }]);
    renderPanel();

    expect(await screen.findByText("Nothing on it")).toBeInTheDocument();
    expect(screen.getByText(/protecting nothing/)).toBeInTheDocument();
    expect(screen.queryByText("Working")).not.toBeInTheDocument();
  });

  it("does not call an empty stale list out of date either", async () => {
    // Same trap one state over: "still protecting 0 titles" is a sentence about coverage that
    // does not exist, and rule 72 wants the sibling fixed in the same change.
    seed([IMDB_DEF], [{ ...WORKING, state: "stale", item_count: 0 }]);
    renderPanel();

    expect(await screen.findByText("Nothing on it")).toBeInTheDocument();
    expect(screen.queryByText("Out of date")).not.toBeInTheDocument();
  });

  it("warns that a stale list does not cover what was added since", async () => {
    seed([IMDB_DEF], [{ ...WORKING, state: "stale", item_count: 37 }]);
    renderPanel();

    expect(await screen.findByText("Out of date")).toBeInTheDocument();
    expect(screen.getByText(/not covered yet/)).toBeInTheDocument();
  });

  it("says a list that has never run is protecting nothing yet", async () => {
    // No membership row at all: the definition was saved and no scan has run since. The row
    // must still render, or a list the operator just added is invisible until a scan.
    seed([PLEX_DEF], []);
    renderPanel();

    expect(await screen.findByText("Not checked yet")).toBeInTheDocument();
    expect(screen.getByText(/Nothing on it is protected until it does\./)).toBeInTheDocument();
  });

  it("says a never-checked family holding titles is still protecting them", async () => {
    // A rolled-up family takes its state from its WORST member, and `never_checked` outranks
    // `working` -- so adding a second Radarr to a tag list that already holds titles lands
    // here with a non-zero count. The flat sentence then told the operator a live protection
    // was not protecting, which is the one question this screen exists to answer.
    seed(
      [TAG_DEF],
      [
        tagRow("radarr-1-keeptags-any-list3", { item_count: 40, server: "Radarr (HD)" }),
        tagRow("radarr-2-keeptags-any-list3", {
          state: "never_checked",
          item_count: 0,
          server: "Radarr (4k)",
        }),
      ],
    );
    renderPanel();

    expect(await screen.findByText("Not checked yet")).toBeInTheDocument();
    expect(
      screen.getByText(/Still protecting 40 titles from an earlier check\./),
    ).toBeInTheDocument();
    expect(screen.queryByText(/Nothing on it is protected until it does/)).not.toBeInTheDocument();
  });

  it("names where a list points, so two lists are told apart by more than their names", async () => {
    seed([PLEX_DEF], []);
    renderPanel();

    expect(await screen.findByText(/The "Never Reap" collection in Films\./)).toBeInTheDocument();
  });

  it("does not read silence as 'your lists are fine' when the read fails", async () => {
    // Rule 17/36. The failure mode this screen exists to prevent is an operator concluding
    // nothing is wrong, so an unreadable answer says exactly that it could not tell them.
    apiMock.listConfigs.mockResolvedValue([]);
    apiMock.lists.mockRejectedValue(new Error("nope"));
    renderPanel();

    expect(await screen.findByText(/Couldn't load your lists/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Try again" })).toBeInTheDocument();
  });

  it("says the same when it is the DEFINITIONS that could not be read", async () => {
    // The other read, which is the one that decides whether a row exists at all. Without this
    // branch a failure there renders a page with no rows and a cheerful Add button, which
    // reads as "you have no lists" to an operator who has several (rule 146's shape: the
    // panel must not report a state its own early returns contradict).
    apiMock.lists.mockResolvedValue([WORKING]);
    apiMock.listConfigs.mockRejectedValue(new Error("nope"));
    renderPanel();

    expect(await screen.findByText(/Couldn't load your lists/)).toBeInTheDocument();
  });

  it("retries both reads on Try again", async () => {
    const user = userEvent.setup();
    apiMock.listConfigs.mockResolvedValue([IMDB_DEF]);
    apiMock.lists.mockRejectedValueOnce(new Error("nope")).mockResolvedValue([WORKING]);
    renderPanel();

    await user.click(await screen.findByRole("button", { name: "Try again" }));
    expect(await screen.findByText("IMDb Top 250")).toBeInTheDocument();
  });
});

describe("the kind badges", () => {
  it("wears each service's own mark: Plex gold, IMDb yellow, and the Sonarr-Radarr gradient", async () => {
    seed([IMDB_DEF, PLEX_DEF, TAG_DEF], []);
    renderPanel();
    expect(await screen.findByText("IMDb Top 250")).toBeInTheDocument();

    expect(document.querySelector(".kind-badge.kind-imdb")).toHaveTextContent("IMDb");
    expect(document.querySelector(".kind-badge.kind-plex")).toHaveTextContent("Plex");
    // The two-color pill: one badge for a list read from both *arrs at once.
    const arr = document.querySelector(".kind-badge.kind-arr");
    expect(arr).not.toBeNull();
    expect(arr).toHaveTextContent(/Sonarr.*Radarr/);
    // A span per half, each carrying its own fill and padding, so the seam lands ON the gap
    // between the words instead of near it. The space between the halves is layout-inert but
    // stays in the text: run together the badge announces itself as one invented word.
    expect(arr?.querySelectorAll(":scope > span")).toHaveLength(2);
    expect(arr?.textContent).toBe("Sonarr Radarr");
  });
});

describe("the policy-use line", () => {
  it("says a hard rule keeps every title on it, once, and links to Policy", async () => {
    // Two hard rules (movie and TV) are one sentence: the same fact said twice would read as
    // two different protections.
    const user = userEvent.setup();
    seed([IMDB_DEF], [WORKING]);
    const { onGoToPolicy } = renderPanel();

    expect(await screen.findByText(/Keeps every title on it\./)).toBeInTheDocument();
    expect(screen.getAllByText(/Keeps every title on it/)).toHaveLength(1);

    await user.click(screen.getByRole("button", { name: "Change on Policy" }));
    expect(onGoToPolicy).toHaveBeenCalled();
  });

  it("says what a lean is worth, in points", async () => {
    seed(
      [
        {
          ...IMDB_DEF,
          policy_use: [{ media_type: "movie", strength: "lean", points: 15 }],
        },
      ],
      [WORKING],
    );
    renderPanel();

    // Named for the media type, because only movies carry the rule: `attach_list` writes both,
    // so one entry means the operator removed the other, and shows on this list have no rule
    // at all. The neutral noun below is for the case where both types agree.
    expect(
      await screen.findByText(/Leans toward keeping movies, up to 15 points off\./),
    ).toBeInTheDocument();
  });

  it("says both halves when the two policies use the list differently", async () => {
    // Hard-over-lean is right WITHIN one media type -- an outright rule decides the item, so a
    // lean beside it changes nothing (#510) -- and wrong across the pair: a movie rule decides
    // nothing about a season. Collapsed, a list keeping movies outright while only leaning on
    // TV read "Keeps every title on it" and dropped the lean entirely, on the screen built to
    // answer whether a list is protecting.
    seed(
      [
        {
          ...IMDB_DEF,
          policy_use: [
            { media_type: "movie", strength: "hard", points: null },
            { media_type: "tv", strength: "lean", points: 20 },
          ],
        },
      ],
      [WORKING],
    );
    renderPanel();

    expect(await screen.findByText(/Keeps every movie on it\./)).toBeInTheDocument();
    expect(
      screen.getByText(/Leans toward keeping shows, up to 20 points off\./),
    ).toBeInTheDocument();
  });

  it("says one sentence when both policies use the list the same way", async () => {
    seed(
      [
        {
          ...IMDB_DEF,
          policy_use: [
            { media_type: "movie", strength: "hard", points: null },
            { media_type: "tv", strength: "hard", points: null },
          ],
        },
      ],
      [WORKING],
    );
    renderPanel();

    expect(await screen.findByText(/Keeps every title on it\./)).toBeInTheDocument();
    expect(screen.queryByText(/Keeps every movie on it/)).not.toBeInTheDocument();
  });

  it("names only the outright rule when a stored body carries both strengths", async () => {
    // An outright rule decides the item on its own, so a lean beside it can never change an
    // outcome. Saying both described one list as doing two things, and sent the operator to
    // tune points that could not matter (#510). Policy will not compose the pair any more;
    // a body stored before that could, and this is what it reads as.
    seed(
      [
        {
          ...IMDB_DEF,
          policy_use: [
            { media_type: "movie", strength: "hard", points: null },
            { media_type: "movie", strength: "lean", points: 15 },
          ],
        },
      ],
      [WORKING],
    );
    renderPanel();

    expect(await screen.findByText(/Keeps every movie on it\./)).toBeInTheDocument();
    expect(screen.queryByText(/Leans toward keeping/)).not.toBeInTheDocument();
  });

  it("warns when no rule names the list, because it then protects nothing", async () => {
    const user = userEvent.setup();
    seed([{ ...IMDB_DEF, policy_use: [] }], [WORKING]);
    const { onGoToPolicy } = renderPanel();

    expect(
      await screen.findByText(/Not used by your policy yet, so it protects nothing\./),
    ).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Set it on Policy" }));
    expect(onGoToPolicy).toHaveBeenCalled();
  });
});

describe("the row actions", () => {
  it("offers Edit then Check now, rightmost, and never a row-level Remove", async () => {
    // Remove lives inside Edit, behind the form that names what it destroys, so a row's
    // actions are two buttons and the destructive one is never one slip away.
    seed([PLEX_DEF], []);
    renderPanel();
    expect(await screen.findByText("Never Reap")).toBeInTheDocument();

    const actions = document.querySelector(".jobrow-actions")!;
    const buttons = within(actions as HTMLElement).getAllByRole("button");
    expect(buttons.map((b) => b.getAttribute("aria-label"))).toEqual([
      "Edit Never Reap",
      "Check now, Never Reap",
    ]);
    expect(screen.queryByRole("button", { name: /^Remove / })).not.toBeInTheDocument();
  });

  it("checks one list without touching the others", async () => {
    // A narrowed pass retires nothing (see `sync_protection_lists`), which is the whole reason
    // the id is sent rather than the button just re-running everything.
    const user = userEvent.setup();
    seed([IMDB_DEF, PLEX_DEF], [WORKING]);
    renderPanel();

    await user.click(await screen.findByRole("button", { name: "Check now, Never Reap" }));
    await waitFor(() => expect(apiMock.syncLists).toHaveBeenCalledWith({ list_id: 2 }));
  });

  it("re-reads the health rows once a check settles, and not before", async () => {
    // Rule 85. The chip IS the result, so the button must go on saying "Checking…" until the
    // refetch has landed -- otherwise it reports done while the old answer is still on screen.
    const user = userEvent.setup();
    seed([IMDB_DEF], [WORKING]);
    renderPanel();
    await screen.findByText("IMDb Top 250");
    apiMock.lists.mockResolvedValue([{ ...WORKING, item_count: 249 }]);

    await user.click(screen.getByRole("button", { name: "Check now, IMDb Top 250" }));
    expect(await screen.findByText(/Protecting 249 titles\./)).toBeInTheDocument();
  });

  it("says on the row when its check could not run", async () => {
    // Rule 42: the failure renders beside the button that retries it, not in a page-level slot
    // where an operator with six lists cannot tell which one it is about.
    const user = userEvent.setup();
    seed([IMDB_DEF], [WORKING]);
    apiMock.syncLists.mockRejectedValue(new Error("Radarr refused the request"));
    renderPanel();

    await user.click(await screen.findByRole("button", { name: "Check now, IMDb Top 250" }));
    expect(await screen.findByText(/Radarr refused the request/)).toBeInTheDocument();
  });

  it("checks everything from the footer, whose last action is Add a list", async () => {
    const user = userEvent.setup();
    seed([IMDB_DEF], [WORKING]);
    renderPanel();

    await user.click(await screen.findByRole("button", { name: "Check all now" }));
    await waitFor(() => expect(apiMock.syncLists).toHaveBeenCalledWith({}));

    // Right-aligned pair: the ghost that touches every row, then the primary that adds one.
    const foot = document.querySelector(".list-foot")!;
    const footButtons = within(foot as HTMLElement).getAllByRole("button");
    expect(footButtons.map((b) => b.textContent)).toEqual(["Check all now", "Add a list"]);
  });

  it("shows every row as busy while Check all now runs", async () => {
    // Found by driving it: the footer said "Checking…" while every row still offered "Check
    // now", so a row invited a second check during the pass it was already part of. A button
    // reports the state it is in, and "all" really is checking that row.
    const user = userEvent.setup();
    let release: (v: unknown) => void = () => {};
    apiMock.syncLists.mockReturnValue(new Promise((r) => (release = r)));
    seed([IMDB_DEF, PLEX_DEF], [WORKING]);
    renderPanel();

    await user.click(await screen.findByRole("button", { name: "Check all now" }));

    expect(screen.getByRole("button", { name: "Checking…, IMDb Top 250" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Checking…, Never Reap" })).toBeDisabled();
    expect(screen.queryByRole("button", { name: /^Check now/ })).not.toBeInTheDocument();

    release({ checked: 2, failed: 0, plex_error: null });
    await waitFor(() =>
      expect(screen.getByRole("button", { name: "Check now, IMDb Top 250" })).toBeEnabled(),
    );
  });

  it("says so when Plex could not be reached, since no row can", async () => {
    // With no live server no collection provider is built, so nothing is synced for one and no
    // row carries an error explaining the silence. Without this the screen would report a
    // successful check that skipped every Plex list.
    const user = userEvent.setup();
    seed([PLEX_DEF], []);
    apiMock.syncLists.mockResolvedValue({
      checked: 0,
      failed: 0,
      plex_error: "Reaper couldn't reach Plex, so its collections were not checked: timed out",
    });
    renderPanel();

    await user.click(await screen.findByRole("button", { name: "Check all now" }));
    expect(await screen.findByText(/couldn't reach Plex/)).toBeInTheDocument();
  });
});

describe("a tag list's counts", () => {
  // One definition, one row per *arr instance. The pills carry the combined count per tag;
  // the per-server split folds under the row, so four servers stay one row until asked.
  const radarr = tagRow("radarr-1-list3", {
    server: "Radarr",
    item_count: 8,
    tags: { "reaper-keep": 8 },
  });
  const sonarr = tagRow("sonarr-2-list3", {
    server: "Sonarr",
    item_count: 6,
    tags: { "reaper-keep": 4, "keep-forever": 2 },
  });

  it("sums each tag's count across servers into one pill", async () => {
    seed([TAG_DEF], [radarr, sonarr]);
    renderPanel();

    expect(await screen.findByText("Titles you've tagged")).toBeInTheDocument();
    const pills = [...document.querySelectorAll(".tag-pill")];
    expect(pills.map((p) => p.textContent)).toEqual(["reaper-keep12", "keep-forever2"]);
    expect(screen.getByText(/Across 2 servers\./)).toBeInTheDocument();
    expect(screen.getByText(/Protecting 14 titles\./)).toBeInTheDocument();
  });

  it("folds the per-server counts under the row, named by instance", async () => {
    seed([TAG_DEF], [radarr, sonarr]);
    renderPanel();

    expect(await screen.findByText("Counts by server")).toBeInTheDocument();
    const grid = document.querySelector(".server-grid")!;
    expect([...grid.querySelectorAll(".srv")].map((s) => s.textContent)).toEqual([
      "Radarr",
      "Sonarr",
    ]);
    expect([...grid.querySelectorAll(".cnt")].map((s) => s.textContent)).toEqual([
      "reaper-keep 8",
      "reaper-keep 4, keep-forever 2",
    ]);
  });

  it("shows a bare pill for a tag no check has counted yet, never a zero", async () => {
    // The counts arrived with this screen, so a row synced before them has `tags: null` --
    // unknown. Zero would claim a check found nothing, which no check said.
    seed([TAG_DEF], [tagRow("radarr-1-list3")]);
    renderPanel();

    expect(await screen.findByText("Titles you've tagged")).toBeInTheDocument();
    const pills = [...document.querySelectorAll(".tag-pill")];
    expect(pills.map((p) => p.textContent)).toEqual(["reaper-keep", "keep-forever"]);
    expect(screen.queryByText("Counts by server")).not.toBeInTheDocument();
  });

  it("drops a server with nothing to say about these tags from the fold-out", async () => {
    // A stats body written before the counts existed, or for tags since renamed, has a
    // server name and an empty counts object. Rendering it put a bare instance name beside
    // an empty column, which reads as a broken row.
    seed([TAG_DEF], [radarr, tagRow("sonarr-2-list3", { server: "Sonarr", tags: {} })]);
    renderPanel();

    expect(await screen.findByText("Counts by server")).toBeInTheDocument();
    const grid = document.querySelector(".server-grid")!;
    expect([...grid.querySelectorAll(".srv")].map((s) => s.textContent)).toEqual(["Radarr"]);
  });

  it("says titles need every tag only when the list matches ALL of them", async () => {
    seed([{ ...TAG_DEF, config: { tags: ["reaper-keep"], match: "all" } }], []);
    renderPanel();

    expect(await screen.findByText(/Titles need every tag\./)).toBeInTheDocument();
  });

  it("does not say it for an ANY list, the wider net", async () => {
    seed([TAG_DEF], []);
    renderPanel();

    expect(await screen.findByText("Titles you've tagged")).toBeInTheDocument();
    expect(screen.queryByText(/Titles need every tag/)).not.toBeInTheDocument();
  });
});

describe("a list stored before the registry existed", () => {
  // An upgrade re-homes a stored list under a slug carrying its definition's id, and that
  // happens on its first successful check. Until then the old row has no definition to be
  // rendered from -- and it is still protecting, so hiding it would make this screen lie by
  // omission about the very thing it exists to show.
  const orphan: ProtectionList = {
    slug: "plex-collection-never-reap",
    name: 'Plex collection: "Never Reap"',
    source: "plex_collection",
    state: "working",
    item_count: 12,
    last_checked_at: new Date(Date.now() - 60 * 60_000).toISOString(),
    error: null,
    list_id: null,
    tags: null,
    server: null,
  };

  it("still shows what it is protecting, and that the next check re-homes it", async () => {
    seed([], [orphan]);
    renderPanel();

    expect(await screen.findByText('Plex collection: "Never Reap"')).toBeInTheDocument();
    expect(screen.getByText(/Protecting 12 titles\./)).toBeInTheDocument();
    expect(
      screen.getByText(/Your next check moves it onto a list you can edit\./),
    ).toBeInTheDocument();
  });

  it("offers no Edit, having nothing to edit, but can still be checked", async () => {
    seed([], [orphan]);
    renderPanel();

    expect(await screen.findByText('Plex collection: "Never Reap"')).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /^Edit / })).not.toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: 'Check now, Plex collection: "Never Reap"' }),
    ).toBeInTheDocument();
  });
});
