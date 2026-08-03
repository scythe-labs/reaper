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
// between "your titles are still covered" and "nothing on this list is protected"; a list
// switched off, which no server state describes; and the definition-to-membership join, which
// is what lets a row carry Edit and Remove at all.
import { QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { expectNoA11yViolations } from "../test/a11y";
import { testQueryClient } from "../test/queryClient";
import type { ListConfig, ProtectionList } from "../api";
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
  },
}));
vi.mock("../api", () => ({ api: apiMock }));

/** The shipped list, as a definition. Built-in, so it never offers Remove. */
const CURATED: ListConfig = {
  id: 1,
  name: "IMDb Top 250",
  source: "curated",
  config: { list: "imdb-top-250" },
  enabled: true,
  built_in: true,
};

/** Its membership row, joined on `list_id`. */
const WORKING: ProtectionList = {
  slug: "imdb-top-250-list1",
  name: "IMDb Top 250",
  source: "curated",
  state: "working",
  item_count: 250,
  last_checked_at: new Date(Date.now() - 8 * 60_000).toISOString(),
  error: null,
  list_id: 1,
};

/** A Plex collection the operator defined, which is the shape #483 was about. */
const PLEX_DEF: ListConfig = {
  id: 2,
  name: "Never Reap",
  source: "plex_collection",
  config: { library: "Films", collection: "Never Reap" },
  enabled: true,
  built_in: false,
};

function renderPanel() {
  return render(
    <QueryClientProvider client={testQueryClient()}>
      <ListsPanel />
    </QueryClientProvider>,
  );
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
  apiMock.syncLists.mockResolvedValue({ checked: 1, failed: 0, plex_error: null });
});

describe("the Lists panel", () => {
  it("has no accessibility violations", async () => {
    seed([CURATED], [WORKING]);
    renderPanel();
    expect(await screen.findByText("IMDb Top 250")).toBeInTheDocument();
    await expectNoA11yViolations();
  });

  it("says what a working list is protecting, and when it last checked", async () => {
    seed([CURATED], [WORKING]);
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
      [CURATED],
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
    seed([CURATED], [{ ...WORKING, state: "stale", item_count: 0 }]);
    renderPanel();

    expect(await screen.findByText("Nothing on it")).toBeInTheDocument();
    expect(screen.queryByText("Out of date")).not.toBeInTheDocument();
  });

  it("warns that a stale list does not cover what was added since", async () => {
    seed([CURATED], [{ ...WORKING, state: "stale", item_count: 37 }]);
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

  it("does not call a switched-off list working, however healthy its stored copy", async () => {
    // No server state describes this: `lists.ListHealth` reports the last CHECK, and the check
    // went fine. Saying "Working" about a list the operator switched off is the reassuring
    // direction, and its 250 stored titles are protecting nothing.
    seed([{ ...CURATED, enabled: false }], [WORKING]);
    renderPanel();

    expect(await screen.findByText("Off")).toBeInTheDocument();
    expect(screen.getByText(/nothing on this list is protected/)).toBeInTheDocument();
    expect(screen.queryByText("Working")).not.toBeInTheDocument();
    expect(screen.queryByText(/Protecting 250 titles/)).not.toBeInTheDocument();
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
    apiMock.listConfigs.mockResolvedValue([CURATED]);
    apiMock.lists.mockRejectedValueOnce(new Error("nope")).mockResolvedValue([WORKING]);
    renderPanel();

    await user.click(await screen.findByRole("button", { name: "Try again" }));
    expect(await screen.findByText("IMDb Top 250")).toBeInTheDocument();
  });
});

describe("the row actions", () => {
  it("checks one list without touching the others", async () => {
    // A narrowed pass retires nothing (see `sync_protection_lists`), which is the whole reason
    // the id is sent rather than the button just re-running everything.
    const user = userEvent.setup();
    seed([CURATED, PLEX_DEF], [WORKING]);
    renderPanel();

    await user.click(await screen.findByRole("button", { name: "Check now, Never Reap" }));
    await waitFor(() => expect(apiMock.syncLists).toHaveBeenCalledWith({ list_id: 2 }));
  });

  it("re-reads the health rows once a check settles, and not before", async () => {
    // Rule 85. The chip IS the result, so the button must go on saying "Checking…" until the
    // refetch has landed -- otherwise it reports done while the old answer is still on screen.
    const user = userEvent.setup();
    seed([CURATED], [WORKING]);
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
    seed([CURATED], [WORKING]);
    apiMock.syncLists.mockRejectedValue(new Error("Radarr refused the request"));
    renderPanel();

    await user.click(await screen.findByRole("button", { name: "Check now, IMDb Top 250" }));
    expect(await screen.findByText(/Radarr refused the request/)).toBeInTheDocument();
  });

  it("checks everything from the footer", async () => {
    const user = userEvent.setup();
    seed([CURATED], [WORKING]);
    renderPanel();

    await user.click(await screen.findByRole("button", { name: "Check all now" }));
    await waitFor(() => expect(apiMock.syncLists).toHaveBeenCalledWith({}));
  });

  it("shows every row as busy while Check all now runs", async () => {
    // Found by driving it: the footer said "Checking…" while every row still offered "Check
    // now", so a row invited a second check during the pass it was already part of. A button
    // reports the state it is in, and "all" really is checking that row.
    const user = userEvent.setup();
    let release: (v: unknown) => void = () => {};
    apiMock.syncLists.mockReturnValue(new Promise((r) => (release = r)));
    seed([CURATED, PLEX_DEF], [WORKING]);
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

  it("never offers Remove for a list Reaper ships with", async () => {
    // A keep rule in the default policy names it, and a rule naming a list that is gone reads
    // as a live protection covering nothing (rule 25). Switching it off stays available.
    seed([CURATED], [WORKING]);
    renderPanel();

    expect(await screen.findByText("IMDb Top 250")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Remove IMDb Top 250" })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Edit IMDb Top 250" })).toBeInTheDocument();
  });

  it("says what a removal costs before doing it, and only removes on confirm", async () => {
    const user = userEvent.setup();
    seed([PLEX_DEF], []);
    renderPanel();

    await user.click(await screen.findByRole("button", { name: "Remove Never Reap" }));
    expect(await screen.findByText(/can be deleted by the next scan/)).toBeInTheDocument();
    expect(apiMock.removeList).not.toHaveBeenCalled();

    await user.click(screen.getByRole("button", { name: "Remove list" }));
    await waitFor(() => expect(apiMock.removeList).toHaveBeenCalledWith(2));
  });

  it("keeps the list when the removal is refused, and says why", async () => {
    const user = userEvent.setup();
    seed([PLEX_DEF], []);
    apiMock.removeList.mockRejectedValue(new Error("A keep rule still points at this list."));
    renderPanel();

    await user.click(await screen.findByRole("button", { name: "Remove Never Reap" }));
    await user.click(await screen.findByRole("button", { name: "Remove list" }));
    expect(await screen.findByText(/A keep rule still points at this list\./)).toBeInTheDocument();
  });
});

describe("collapsing the per-instance keep-tag lists", () => {
  // Reaper stores one keep-tag list per *arr instance per match mode, so two Radarrs and two
  // Sonarrs are four stored rows for the single thing the operator did: tag some titles. A row
  // each grows the page with the server count, which is worst on the installs with the most to
  // lose, so the family collapses to one row.
  const tag = (slug: string, name: string, over: Partial<ProtectionList> = {}): ProtectionList => ({
    slug,
    name,
    source: "arr_tag",
    state: "working",
    item_count: 4,
    last_checked_at: new Date(Date.now() - 60 * 60_000).toISOString(),
    error: null,
    // The policy's keep tags have no definition row: they are configured on Policy, which is
    // exactly why they collapse into a row of their own rather than one per definition.
    list_id: null,
    ...over,
  });

  it("shows one row for four servers, and sums what they protect", async () => {
    seed(
      [],
      [
        tag("radarr-1-keeptags-any", "Radarr (HD) tag: reaper-keep", { item_count: 1 }),
        tag("radarr-2-keeptags-any", "Radarr (4k) tag: reaper-keep", { item_count: 2 }),
        tag("sonarr-3-keeptags-any", "Sonarr (HD) tag: reaper-keep", { item_count: 8 }),
        tag("sonarr-4-keeptags-any", "Sonarr (4k) tag: reaper-keep", { item_count: 3 }),
      ],
    );
    renderPanel();

    expect(await screen.findByText("Titles you've tagged")).toBeInTheDocument();
    expect(screen.getByText(/Protecting 14 titles\./)).toBeInTheDocument();
    expect(screen.getByText(/Across 4 servers\./)).toBeInTheDocument();
    // The individual list names are gone, which is the whole point.
    expect(screen.queryByText(/Radarr \(HD\) tag/)).not.toBeInTheDocument();
  });

  it("wears the worst member's state and names only the servers in it", async () => {
    // A family reported by its best member says "Working" while one server's keep tags protect
    // nothing. The operator needs to know WHICH server to go to, and naming all four would put
    // the row back at one line per server.
    seed(
      [],
      [
        tag("radarr-1-keeptags-any", "Radarr (HD) tag: reaper-keep", { item_count: 9 }),
        tag("sonarr-3-keeptags-any", "Sonarr (4k) tag: reaper-keep", {
          state: "failing",
          item_count: 0,
          error: "Sonarr refused the request",
        }),
      ],
    );
    renderPanel();

    expect(await screen.findByText("Not working")).toBeInTheDocument();
    expect(screen.getByText(/Sonarr \(4k\)/)).toBeInTheDocument();
    expect(screen.queryByText(/Radarr \(HD\)/)).not.toBeInTheDocument();
    expect(screen.getByText("Sonarr refused the request")).toBeInTheDocument();
  });

  it("offers no Edit or Remove on a row Policy owns", async () => {
    // The keep tags are not a definition, so there is nothing here to edit or delete. The row
    // says where they ARE set instead, rather than offering a control that would have to
    // explain itself.
    seed([], [tag("radarr-1-keeptags-any", "Radarr (HD) tag: reaper-keep")]);
    renderPanel();

    expect(await screen.findByText("Titles you've tagged")).toBeInTheDocument();
    expect(screen.getByText(/Set the tags on Policy\./)).toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: "Edit Titles you've tagged" }),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: "Remove Titles you've tagged" }),
    ).not.toBeInTheDocument();
  });

  it("checks the keep tags on their own, since no definition names them", async () => {
    const user = userEvent.setup();
    seed([], [tag("radarr-1-keeptags-any", "Radarr (HD) tag: reaper-keep")]);
    renderPanel();

    await user.click(
      await screen.findByRole("button", { name: "Check now, Titles you've tagged" }),
    );
    await waitFor(() => expect(apiMock.syncLists).toHaveBeenCalledWith({ keep_tags: true }));
  });

  it("leaves the other sources as their own rows", async () => {
    seed([CURATED], [WORKING, tag("radarr-1-keeptags-any", "Radarr (HD) tag: reaper-keep")]);
    renderPanel();

    expect(await screen.findByText("IMDb Top 250")).toBeInTheDocument();
    expect(screen.getByText("Titles you've tagged")).toBeInTheDocument();
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
  };

  it("still shows what it is protecting", async () => {
    seed([], [orphan]);
    renderPanel();

    expect(await screen.findByText('Plex collection: "Never Reap"')).toBeInTheDocument();
    expect(screen.getByText(/Protecting 12 titles\./)).toBeInTheDocument();
    expect(screen.getByText(/Set up before this screen existed\./)).toBeInTheDocument();
  });

  it("offers no Edit or Remove, having nothing to edit", async () => {
    seed([], [orphan]);
    renderPanel();

    expect(await screen.findByText('Plex collection: "Never Reap"')).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /^Edit / })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /^Remove / })).not.toBeInTheDocument();
  });
});
