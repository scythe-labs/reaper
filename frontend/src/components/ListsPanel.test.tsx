// SPDX-License-Identifier: AGPL-3.0-or-later
// Settings -> Lists: what a protection list's row actually says (#475).
//
// The point of the screen is that a list which stopped protecting reads DIFFERENTLY from one
// that is simply not on this title's side, so each state is driven here and asserted on its
// sentence, not only on its chip. The chip is four words; the sentence is what tells the
// operator whether to go and fix something now or at the weekend.
//
// The state itself is the server's (`lists.ListHealth`) and is not recomputed here -- that is
// the whole reason it is decided once. What is pinned is the copy each state produces, and the
// one branch this component does own: `item_count` on a failing list, which is the difference
// between "your titles are still covered" and "nothing on this list is protected".
import { QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { expectNoA11yViolations } from "../test/a11y";
import { testQueryClient } from "../test/queryClient";
import type { ProtectionList } from "../api";
import { ListsPanel } from "./ListsPanel";

const { apiMock } = vi.hoisted(() => ({ apiMock: { lists: vi.fn() } }));
vi.mock("../api", () => ({ api: apiMock }));

const WORKING: ProtectionList = {
  slug: "imdb-top-250",
  name: "IMDb Top 250",
  source: "curated",
  state: "working",
  item_count: 250,
  last_checked_at: new Date(Date.now() - 8 * 60_000).toISOString(),
  error: null,
};

function renderPanel() {
  return render(
    <QueryClientProvider client={testQueryClient()}>
      <ListsPanel />
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  apiMock.lists.mockReset();
});

describe("the Lists panel", () => {
  it("has no accessibility violations", async () => {
    apiMock.lists.mockResolvedValue([WORKING]);
    renderPanel();
    expect(await screen.findByText("IMDb Top 250")).toBeInTheDocument();
    await expectNoA11yViolations();
  });

  it("says what a working list is protecting, and when it last checked", async () => {
    apiMock.lists.mockResolvedValue([WORKING]);
    renderPanel();

    expect(await screen.findByText("IMDb Top 250")).toBeInTheDocument();
    expect(screen.getByText("Working")).toBeInTheDocument();
    expect(screen.getByText(/Protecting 250 titles\./)).toBeInTheDocument();
    expect(screen.getByText(/Last checked 8 minutes ago\./)).toBeInTheDocument();
  });

  it("shows the service's own words for a list that is not working", async () => {
    // The issue, in one assertion. This message names the thing to go and fix, and until this
    // screen existed it was written to `last_error` on every failed sync and read by nothing.
    apiMock.lists.mockResolvedValue([
      {
        ...WORKING,
        slug: "plex-collection-never-reap",
        name: 'Plex collection: "Never Reap"',
        source: "plex_collection",
        state: "failing",
        item_count: 0,
        last_checked_at: null,
        error: "there is no library called 'Movies'",
      },
    ]);
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
    apiMock.lists.mockResolvedValue([
      { ...WORKING, state: "failing", item_count: 37, error: "Sonarr refused the request" },
    ]);
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
    apiMock.lists.mockResolvedValue([
      {
        ...WORKING,
        source: "plex_collection",
        name: 'Plex collection: "Never Reap"',
        state: "working",
        item_count: 0,
      },
    ]);
    renderPanel();

    expect(await screen.findByText("Nothing on it")).toBeInTheDocument();
    expect(screen.getByText(/protecting nothing/)).toBeInTheDocument();
    expect(screen.queryByText("Working")).not.toBeInTheDocument();
  });

  it("does not call an empty stale list out of date either", async () => {
    // Same trap one state over: "still protecting 0 titles" is a sentence about coverage that
    // does not exist, and rule 72 wants the sibling fixed in the same change.
    apiMock.lists.mockResolvedValue([{ ...WORKING, state: "stale", item_count: 0 }]);
    renderPanel();

    expect(await screen.findByText("Nothing on it")).toBeInTheDocument();
    expect(screen.queryByText("Out of date")).not.toBeInTheDocument();
  });

  it("warns that a stale list does not cover what was added since", async () => {
    apiMock.lists.mockResolvedValue([{ ...WORKING, state: "stale", item_count: 37 }]);
    renderPanel();

    expect(await screen.findByText("Out of date")).toBeInTheDocument();
    expect(screen.getByText(/not covered yet/)).toBeInTheDocument();
  });

  it("says a list that has never run is protecting nothing yet", async () => {
    apiMock.lists.mockResolvedValue([
      { ...WORKING, state: "never_checked", item_count: 0, last_checked_at: null },
    ]);
    renderPanel();

    expect(await screen.findByText("Not checked yet")).toBeInTheDocument();
    expect(screen.getByText(/Nothing on it is protected until it does\./)).toBeInTheDocument();
  });

  it("does not read silence as 'your lists are fine' when the read fails", async () => {
    // Rule 17/36. The failure mode this screen exists to prevent is an operator concluding
    // nothing is wrong, so an unreadable answer says exactly that it could not tell them.
    apiMock.lists.mockRejectedValue(new Error("nope"));
    renderPanel();

    expect(await screen.findByText(/Couldn't load your lists/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Try again" })).toBeInTheDocument();
  });

  it("retries the read on Try again", async () => {
    const user = userEvent.setup();
    apiMock.lists.mockRejectedValueOnce(new Error("nope")).mockResolvedValue([WORKING]);
    renderPanel();

    await user.click(await screen.findByRole("button", { name: "Try again" }));
    expect(await screen.findByText("IMDb Top 250")).toBeInTheDocument();
  });

  it("tells a fresh install where its lists will come from", async () => {
    apiMock.lists.mockResolvedValue([]);
    renderPanel();

    expect(await screen.findByText(/No lists yet\./)).toBeInTheDocument();
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
    ...over,
  });

  it("shows one row for four servers, and sums what they protect", async () => {
    apiMock.lists.mockResolvedValue([
      tag("radarr-1-keeptags-any", "Radarr (HD) tag: reaper-keep", { item_count: 1 }),
      tag("radarr-2-keeptags-any", "Radarr (4k) tag: reaper-keep", { item_count: 2 }),
      tag("sonarr-3-keeptags-any", "Sonarr (HD) tag: reaper-keep", { item_count: 8 }),
      tag("sonarr-4-keeptags-any", "Sonarr (4k) tag: reaper-keep", { item_count: 3 }),
    ]);
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
    apiMock.lists.mockResolvedValue([
      tag("radarr-1-keeptags-any", "Radarr (HD) tag: reaper-keep", { item_count: 9 }),
      tag("sonarr-3-keeptags-any", "Sonarr (4k) tag: reaper-keep", {
        state: "failing",
        item_count: 0,
        error: "Sonarr refused the request",
      }),
    ]);
    renderPanel();

    expect(await screen.findByText("Not working")).toBeInTheDocument();
    expect(screen.getByText(/Sonarr \(4k\)/)).toBeInTheDocument();
    expect(screen.queryByText(/Radarr \(HD\)/)).not.toBeInTheDocument();
    expect(screen.getByText("Sonarr refused the request")).toBeInTheDocument();
  });

  it("leaves the other sources as their own rows", async () => {
    apiMock.lists.mockResolvedValue([
      WORKING,
      tag("radarr-1-keeptags-any", "Radarr (HD) tag: reaper-keep"),
    ]);
    renderPanel();

    expect(await screen.findByText("IMDb Top 250")).toBeInTheDocument();
    expect(screen.getByText("Titles you've tagged")).toBeInTheDocument();
  });
});
