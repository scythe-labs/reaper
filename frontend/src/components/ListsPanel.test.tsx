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
// branches this component does own: whether a keep rule uses the list at all, which is the
// difference between the green "In use" chip and the gray "Not in use"; the live-vs-cached
// count; and the definition-to-membership join, which is what lets a row carry Edit and Check
// now at all.
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
    startScan: vi.fn(),
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

/** What the server stores for a watchlist added through the modal: the form with no fields,
 *  so a test can walk the whole add flow without setting anything up. */
const WATCHLIST_DEF: ListConfig = {
  id: 4,
  name: "My watchlist",
  source: "plex_watchlist",
  config: {},
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
  apiMock.startScan.mockResolvedValue({ running: true, followup_queued: false });
});

describe("the Lists panel", () => {
  it("has no accessibility violations", async () => {
    seed([IMDB_DEF], [WORKING]);
    renderPanel();
    expect(await screen.findByText("IMDb Top 250")).toBeInTheDocument();
    await expectNoA11yViolations();
  });

  it("says a working, in-use list is in use, with its live count and last check", async () => {
    seed([IMDB_DEF], [WORKING]);
    renderPanel();

    expect(await screen.findByText("IMDb Top 250")).toBeInTheDocument();
    expect(screen.getByText("In use")).toBeInTheDocument();
    expect(screen.getByText(/250 titles on it\./)).toBeInTheDocument();
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
    expect(screen.getByText(/Nothing cached\./)).toBeInTheDocument();
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
    // Cached, not live: the count is the last good one, and the red chip and the error line
    // carry the failure. No "protecting" claim, which a lean rule would make false anyway.
    expect(screen.getByText(/37 titles cached\./)).toBeInTheDocument();
    expect(screen.getByText("Sonarr refused the request")).toBeInTheDocument();
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
    expect(screen.getByText(/Check it's the one you meant\./)).toBeInTheDocument();
    // The green "In use" verdict is exactly what an empty keep list must not wear.
    expect(screen.queryByText("In use")).not.toBeInTheDocument();
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
    // Cached count, no coverage prose: the amber chip says it is out of date.
    expect(screen.getByText(/37 titles cached\./)).toBeInTheDocument();
  });

  it("says a list that has never run has not been checked yet", async () => {
    // No membership row at all: the definition was saved and no scan has run since. The row
    // must still render, or a list the operator just added is invisible until a scan.
    seed([PLEX_DEF], []);
    renderPanel();

    expect(await screen.findByText("Not checked yet")).toBeInTheDocument();
    expect(screen.getByText(/Runs with your next scan\./)).toBeInTheDocument();
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
    // The family holds 40 titles from a member that has checked, so the count shows -- as
    // cached, not "on it", because the family's own state is not-checked-yet.
    expect(screen.getByText(/40 titles cached\./)).toBeInTheDocument();
    expect(screen.queryByText(/40 titles on it/)).not.toBeInTheDocument();
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
    // A span per painted half, each carrying its own fill and padding, so the seam lands ON
    // the gap between the words instead of near it. Both are hidden from a reader, and the
    // name is said once in a `.sr-only` sibling -- run together the badge announces itself as
    // one invented word, and whether whitespace between two flex items reaches the
    // accessibility tree is not a thing jsdom can answer, so the name does not rest on it.
    expect(arr?.querySelectorAll(':scope > span[aria-hidden="true"]')).toHaveLength(2);
    expect(arr?.querySelector(".sr-only")?.textContent).toBe("Sonarr and Radarr");
  });
});

describe("the policy link, and in use vs not", () => {
  it("an in-use list offers Change policy, and never restates the rule's strength", async () => {
    const user = userEvent.setup();
    seed([IMDB_DEF], [WORKING]);
    const { onGoToPolicy } = renderPanel();

    expect(await screen.findByText("In use")).toBeInTheDocument();
    // The row says one true thing: in use, and the count. It does NOT restate the strength,
    // where "Keeps every title on it" is wrong the moment the rule is a lean.
    expect(screen.queryByText(/Keeps every title/)).not.toBeInTheDocument();
    expect(screen.queryByText(/points off/)).not.toBeInTheDocument();
    // And there is no "Configure in Policy" here -- that is the not-in-use action.
    expect(screen.queryByRole("button", { name: "Configure in Policy" })).not.toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Change policy" }));
    expect(onGoToPolicy).toHaveBeenCalled();
  });

  it("reads a lean the same as any other use: in use, no false 'keeps every title'", async () => {
    // The case the operator raised: a rule that only leans toward keeping does not keep every
    // title, so the row must not say it does. It reads "In use" and the plain count, nothing
    // about strength.
    seed(
      [{ ...IMDB_DEF, policy_use: [{ media_type: "movie", strength: "lean", points: 15 }] }],
      [WORKING],
    );
    renderPanel();

    expect(await screen.findByText("In use")).toBeInTheDocument();
    expect(screen.getByText(/250 titles on it\./)).toBeInTheDocument();
    expect(screen.queryByText(/Keeps every title/)).not.toBeInTheDocument();
    expect(screen.queryByText(/Leans toward keeping/)).not.toBeInTheDocument();
    expect(screen.queryByText(/points off/)).not.toBeInTheDocument();
  });

  it("a list no rule names reads Not in use, and offers Configure in Policy", async () => {
    // A working check over a list nothing uses still protects nothing, so the chip must not
    // read green "In use" (rule 79's direction). Its action is to set it up, not to change it.
    const user = userEvent.setup();
    seed([{ ...IMDB_DEF, policy_use: [] }], [WORKING]);
    const { onGoToPolicy } = renderPanel();

    expect(await screen.findByText("Not in use")).toBeInTheDocument();
    expect(screen.getByText(/250 titles on it\./)).toBeInTheDocument();
    expect(screen.queryByText("In use")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Change policy" })).not.toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Configure in Policy" }));
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
    // A narrowed pass sweeps only the definition it checked (see `sync_protection_lists`),
    // which is the whole reason the id is sent rather than the button re-running everything.
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
    expect(await screen.findByText(/249 titles on it\./)).toBeInTheDocument();
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

  it("offers Add a list alone in the footer, with no second way to check everything", async () => {
    seed([IMDB_DEF], [WORKING]);
    renderPanel();

    // "Check all now" was here. Checking every list is the nightly upkeep job's whole
    // purpose and it has its own Run now on Settings, Jobs, so a button here meaning the
    // same thing is one job offered in two places (rule 18). Per-row Check now stays: it is
    // the one thing the job cannot do, which is check the single list you just edited.
    expect(await screen.findByRole("button", { name: "Add a list" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Check all now" })).not.toBeInTheDocument();
    const foot = document.querySelector(".list-foot")!;
    const footButtons = within(foot as HTMLElement).getAllByRole("button");
    expect(footButtons.map((b) => b.textContent)).toEqual(["Add a list"]);
  });

  it("shows every row as busy while a whole-pass check runs", async () => {
    // Found by driving it: the pass said "Checking…" on one row while every other still
    // offered "Check now", so a row invited a second check during the pass it was already
    // part of. A button reports the state it is in, and "all" really is checking that row.
    //
    // Driven from an ORPHAN row, which is the only button left that checks everything: a
    // stored row no definition owns has no id to check by. The footer's "Check all now" is
    // gone, and the nightly job is what checks every list now.
    const user = userEvent.setup();
    let release: (v: unknown) => void = () => {};
    apiMock.syncLists.mockReturnValue(new Promise((r) => (release = r)));
    seed([IMDB_DEF, PLEX_DEF], [WORKING, ORPHAN_ROW]);
    renderPanel();

    await user.click(await screen.findByRole("button", { name: `Check now, ${ORPHAN_ROW.name}` }));
    await waitFor(() => expect(apiMock.syncLists).toHaveBeenCalledWith({}));

    expect(screen.getByRole("button", { name: "Checking…, IMDb Top 250" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Checking…, Never Reap" })).toBeDisabled();
    expect(screen.queryByRole("button", { name: /^Check now/ })).not.toBeInTheDocument();

    release({ checked: 2, failed: 0, plex_error: null });
    await waitFor(() =>
      expect(screen.getByRole("button", { name: "Check now, IMDb Top 250" })).toBeEnabled(),
    );
  });

  it("checks a list as soon as it is added, without a second press", async () => {
    // A list is protecting nothing until something reads it, and the first read used to wait
    // for the next scan -- so an operator who had just told Reaper what to keep watched the
    // row say "Nothing on it is protected until it does" and had no way to know whether the
    // collection they named was even the right one.
    const user = userEvent.setup();
    seed([], []);
    apiMock.addList.mockResolvedValue(WATCHLIST_DEF);
    renderPanel();

    await user.click(await screen.findByRole("button", { name: "Add a list" }));
    await user.click(await screen.findByRole("button", { name: "Add a Plex watchlist" }));
    // The definition the save wrote is on screen by the time its check starts, so the row is
    // the one that reports it.
    apiMock.listConfigs.mockResolvedValue([WATCHLIST_DEF]);
    await user.click(await screen.findByRole("button", { name: "Add list" }));

    await waitFor(() => expect(apiMock.syncLists).toHaveBeenCalledWith({ list_id: 4 }));
    expect(await screen.findByRole("button", { name: "Check now, My watchlist" })).toBeEnabled();
  });

  it("says the new row is being checked, rather than to wait for the next scan", async () => {
    // The row has nothing stored yet, and its never-checked sentence is about the scan that
    // would have read it. Beside a button reading "Checking…", that is two answers to one
    // question, and it is what a save now puts on screen every time.
    const user = userEvent.setup();
    let release: (v: unknown) => void = () => {};
    apiMock.syncLists.mockReturnValue(new Promise((r) => (release = r)));
    seed([], []);
    apiMock.addList.mockResolvedValue(WATCHLIST_DEF);
    renderPanel();

    await user.click(await screen.findByRole("button", { name: "Add a list" }));
    await user.click(await screen.findByRole("button", { name: "Add a Plex watchlist" }));
    apiMock.listConfigs.mockResolvedValue([WATCHLIST_DEF]);
    await user.click(await screen.findByRole("button", { name: "Add list" }));

    expect(await screen.findByText("Checking it now.")).toBeInTheDocument();
    expect(screen.queryByText(/Runs with your next scan/)).not.toBeInTheDocument();

    release({ checked: 1, failed: 0, plex_error: null });
    await waitFor(() =>
      expect(screen.getByRole("button", { name: "Check now, My watchlist" })).toBeEnabled(),
    );
    // Once it settles the row is back to its stored state, which the substitution only ever
    // covered for: here the health read still answers empty.
    expect(await screen.findByText(/Runs with your next scan/)).toBeInTheDocument();
  });

  it("shows the new row as busy while that check runs, and says what came back", async () => {
    // The check is the panel's own, so it reports where every other check does: the row's
    // button, and the row's error line when the source refuses (rule 42).
    const user = userEvent.setup();
    seed([], []);
    apiMock.addList.mockResolvedValue(WATCHLIST_DEF);
    apiMock.syncLists.mockRejectedValue(new Error("Reaper isn't signed in to Plex"));
    renderPanel();

    await user.click(await screen.findByRole("button", { name: "Add a list" }));
    await user.click(await screen.findByRole("button", { name: "Add a Plex watchlist" }));
    apiMock.listConfigs.mockResolvedValue([WATCHLIST_DEF]);
    await user.click(await screen.findByRole("button", { name: "Add list" }));

    expect(await screen.findByText(/Reaper isn't signed in to Plex/)).toBeInTheDocument();
  });

  it("checks an edited list too, since an edit re-points what it reads", async () => {
    const user = userEvent.setup();
    seed([PLEX_DEF], []);
    apiMock.editList.mockResolvedValue({ ...PLEX_DEF, name: "Films worth keeping" });
    renderPanel();

    await user.click(await screen.findByRole("button", { name: "Edit Never Reap" }));
    await user.click(await screen.findByRole("button", { name: "Save" }));

    await waitFor(() => expect(apiMock.syncLists).toHaveBeenCalledWith({ list_id: 2 }));
  });

  it("re-scans after a save, so the queue stops showing fates from the old lists", async () => {
    // A check refreshes MEMBERSHIP, which is not the same thing: the queue's fates were
    // scored under the lists as they were, and nothing here re-scored them. So the operator
    // added a keep list, saw it protecting 40 titles, and the queue went on offering those
    // titles for deletion with no stale notice, until the executor's list interlock refused
    // the approved plan at the far end. `PolicyEditor` starts a scan on save for exactly this
    // class of change, and a list is the half of the policy that moved out of the policy body.
    const user = userEvent.setup();
    seed([PLEX_DEF], []);
    apiMock.editList.mockResolvedValue({ ...PLEX_DEF, name: "Films worth keeping" });
    renderPanel();

    await user.click(await screen.findByRole("button", { name: "Edit Never Reap" }));
    await user.click(await screen.findByRole("button", { name: "Save" }));

    await waitFor(() => expect(apiMock.startScan).toHaveBeenCalled());
  });

  it("does not scan when a list is added, since it names no rule yet", async () => {
    // A hand-added list writes no keep rule, so no fate moved and the whole-library scan an
    // edit of a used list warrants would be pointless work here. The membership check still
    // runs -- that is how the operator learns what is on the list.
    const user = userEvent.setup();
    seed([], []);
    apiMock.addList.mockResolvedValue({ ...WATCHLIST_DEF, policy_use: [] });
    apiMock.listConfigs.mockResolvedValue([{ ...WATCHLIST_DEF, policy_use: [] }]);
    renderPanel();

    await user.click(await screen.findByRole("button", { name: "Add a list" }));
    await user.click(await screen.findByRole("button", { name: "Add a Plex watchlist" }));
    await user.click(await screen.findByRole("button", { name: "Add list" }));

    await waitFor(() => expect(apiMock.syncLists).toHaveBeenCalled());
    expect(apiMock.startScan).not.toHaveBeenCalled();
  });

  it("re-scans after removing a list a rule named", async () => {
    // Removing a USED list takes the keep rules naming it with it, so it changes what is
    // protected and hands back no row to check. PLEX_DEF is used here, so its removal scans.
    const user = userEvent.setup();
    seed([PLEX_DEF], []);
    apiMock.removeList.mockResolvedValue(undefined);
    renderPanel();

    await user.click(await screen.findByRole("button", { name: "Edit Never Reap" }));
    await user.click(await screen.findByRole("button", { name: "Remove list…" }));
    await user.click(await screen.findByRole("button", { name: "Remove list" }));

    await waitFor(() => expect(apiMock.startScan).toHaveBeenCalled());
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

    await user.click(await screen.findByRole("button", { name: "Check now, Never Reap" }));
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
    // Separated in the TEXT, not by a flex gap: as adjacent flex items the tag and its count
    // had nothing between them, so the pill read and copied as "reaper-keep12".
    expect(pills.map((p) => p.textContent)).toEqual(["reaper-keep 12", "keep-forever 2"]);
    expect(screen.getByText(/Across 2 servers\./)).toBeInTheDocument();
    expect(screen.getByText(/14 titles on it\./)).toBeInTheDocument();
  });

  it("folds the per-server counts into a matrix: tags down, servers across, totals both ways", async () => {
    seed([TAG_DEF], [radarr, sonarr]);
    renderPanel();

    expect(await screen.findByText("Counts by server")).toBeInTheDocument();
    const table = document.querySelector(".tag-matrix")!;
    const rowOf = (tr: Element) => [...tr.querySelectorAll("th, td")].map((c) => c.textContent);
    // Servers across the top, the Total column last.
    expect(rowOf(table.querySelector("thead tr")!)).toEqual(["Tag", "Radarr", "Sonarr", "Total"]);
    // A tag reads across its row; a dash is a server that does not carry it, its Total sums the
    // servers shown.
    expect([...table.querySelectorAll("tbody tr")].map(rowOf)).toEqual([
      ["reaper-keep", "8", "4", "12"],
      ["keep-forever", "—", "2", "2"],
    ]);
    // The footer totals each server's column, and the corner is the grand total.
    expect(rowOf(table.querySelector("tfoot tr")!)).toEqual(["Total", "8", "6", "14"]);
  });

  it("shows a bare pill for a tag no check has counted yet, never a zero", async () => {
    // The counts arrived with this screen, so a row synced before them has `tags: null` --
    // unknown. Zero would claim a check found nothing, which no check said.
    seed([TAG_DEF], [tagRow("radarr-1-list3")]);
    renderPanel();

    expect(await screen.findByText("Titles you've tagged")).toBeInTheDocument();
    const pills = [...document.querySelectorAll(".tag-pill")];
    // A trailing space and no count: nothing has been counted, so the pill is bare.
    expect(pills.map((p) => p.textContent?.trim())).toEqual(["reaper-keep", "keep-forever"]);
    expect(screen.queryByText("Counts by server")).not.toBeInTheDocument();
  });

  it("drops a server with nothing to say about these tags from the fold-out", async () => {
    // A stats body written before the counts existed, or for tags since renamed, has a
    // server name and an empty counts object. Rendering it put a bare instance name beside
    // an empty column, which reads as a broken row.
    seed([TAG_DEF], [radarr, tagRow("sonarr-2-list3", { server: "Sonarr", tags: {} })]);
    renderPanel();

    expect(await screen.findByText("Counts by server")).toBeInTheDocument();
    const cols = [...document.querySelectorAll(".tag-matrix thead th")].map((t) => t.textContent);
    // Sonarr had an empty counts object, so it is not a column: Tag, the one real server, Total.
    expect(cols).toEqual(["Tag", "Radarr", "Total"]);
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

/** A stored row no definition owns: an upgrade re-homes it on its first successful check.
 *  It is also the only button left that checks EVERY list, having no id to check by. */
const ORPHAN_ROW: ProtectionList = {
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

describe("a list stored before the registry existed", () => {
  // An upgrade re-homes a stored list under a slug carrying its definition's id, and that
  // happens on its first successful check. Until then the old row has no definition to be
  // rendered from -- and it is still protecting, so hiding it would make this screen lie by
  // omission about the very thing it exists to show.
  it("still shows what it is protecting, and that the next check re-homes it", async () => {
    seed([], [ORPHAN_ROW]);
    renderPanel();

    expect(await screen.findByText('Plex collection: "Never Reap"')).toBeInTheDocument();
    // An orphan carries no definition to hold a policy_use, so it defaults to in use and shows
    // its live count -- hiding a row still holding titles is the lie this screen exists to fix.
    expect(screen.getByText(/12 titles on it\./)).toBeInTheDocument();
    expect(
      screen.getByText(/Your next check moves it onto a list you can edit\./),
    ).toBeInTheDocument();
  });

  it("offers no Edit, having nothing to edit, but can still be checked", async () => {
    seed([], [ORPHAN_ROW]);
    renderPanel();

    expect(await screen.findByText('Plex collection: "Never Reap"')).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /^Edit / })).not.toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: 'Check now, Plex collection: "Never Reap"' }),
    ).toBeInTheDocument();
  });
});
