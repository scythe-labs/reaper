// SPDX-License-Identifier: AGPL-3.0-or-later
// Scales reads the last scan, but it is the screen an operator scans before a run, so its
// states have to be honest: a reclaimable card names the disk; a clean card says so plainly;
// and either one opens the person's full breakdown. The page says out loud when it is
// loading, could not load, or has no scan to sit on.
import { QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { act, type ReactElement } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { Announcer } from "../announce";
import { ApiError, type FairnessReport, type RequesterRow } from "../api";
import { expectNoA11yViolations } from "../test/a11y";
import { testQueryClient } from "../test/queryClient";
import { Fairness, PersonCard } from "./Fairness";

const { apiMock } = vi.hoisted(() => ({
  apiMock: { fairness: vi.fn() },
}));

vi.mock("../api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../api")>()),
  api: apiMock,
}));

const GB = 1024 ** 3;

/** How far back the watch mirror reaches, as the report carries it. Every watched figure is
 *  counted over this span, so a card may only state one when it has one. */
const HORIZON = "2018-01-11T00:00:00+00:00";

function row(over: Partial<RequesterRow> = {}): RequesterRow {
  return {
    identity: "plex:7",
    plex_id: 7,
    name: "marlow",
    requests_made: 52,
    gb_granted_bytes: 549 * GB,
    played_by_them: 30,
    reclaimable_items: 3,
    reclaimable_bytes: 13 * GB,
    ...over,
  };
}

function renderWithClient(ui: ReactElement) {
  const client = testQueryClient();
  return render(<QueryClientProvider client={client}>{ui}</QueryClientProvider>);
}

/** What an operator would hear from the app's shared region. Empty when it has said nothing. */
const spokenText = () =>
  screen
    .queryAllByRole("status")
    .map((r) => r.textContent)
    .join("");

// Rule 133: fake timers left standing are inherited by the next test in the file, and by every
// file after it in the same worker.
afterEach(() => {
  vi.useRealTimers();
});

describe("PersonCard", () => {
  it("leads with the reclaimable disk and opens the person's breakdown", async () => {
    const onSelect = vi.fn();
    render(<PersonCard row={row()} selected={false} onSelect={onSelect} horizonAt={HORIZON} />);

    expect(screen.getByText(/earning its keep/i)).toBeInTheDocument();
    expect(screen.getByText(/to reclaim, 3 titles/i)).toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: /marlow/i }));
    expect(onSelect).toHaveBeenCalledWith("plex:7");
  });

  // The bug this whole change fixes: a requester with nothing reclaimable must still open.
  it("opens a clean requester too, and says it is clean", async () => {
    const onSelect = vi.fn();
    render(
      <PersonCard
        row={row({ reclaimable_items: 0, reclaimable_bytes: 0 })}
        selected={false}
        onSelect={onSelect}
        horizonAt={HORIZON}
      />,
    );

    // Two separate statements of the clean state, and the card does not always carry both:
    // the chip is the one below 640px drops (styles/18-scales.css, the .fair-card grid block), so the
    // legend is all a phone has left. jsdom applies no media queries and cannot tell them
    // apart, so pin the wording of each -- otherwise the only assertion here is the one a
    // phone never renders.
    expect(screen.getByText(/nothing to reclaim/i)).toBeInTheDocument();
    expect(screen.getByText(/nothing reclaimable/i)).toBeInTheDocument();
    const card = screen.getByRole("button", { name: /marlow/i });
    expect(card).toBeInTheDocument();
    await userEvent.click(card);
    expect(onSelect).toHaveBeenCalledWith("plex:7");
  });

  it("opens on Enter and Space, for keyboard users", async () => {
    const onSelect = vi.fn();
    render(<PersonCard row={row()} selected={false} onSelect={onSelect} horizonAt={HORIZON} />);
    const card = screen.getByRole("button", { name: /marlow/i });
    card.focus();
    await userEvent.keyboard("{Enter}");
    await userEvent.keyboard(" ");
    expect(onSelect).toHaveBeenCalledTimes(2);
  });

  it("reads its balance and its watched share, not just the person's name", async () => {
    // The card was a `role="button"`, whose Children Presentational pruned everything inside it
    // out of the accessibility tree: a reader heard the name and nothing else, on the screen
    // that says whose files are candidates (#169). The control is the name now, and the body
    // beside it is ordinary content again.
    render(<PersonCard row={row()} selected={false} onSelect={vi.fn()} horizonAt={HORIZON} />);

    expect(screen.getByRole("button", { name: /marlow/i })).toBeInTheDocument();
    expect(screen.getByText(/requests/i)).toBeInTheDocument();
    expect(screen.getByText(/earning its keep/i)).toBeInTheDocument();
    expect(screen.getByText(/to reclaim, 3 titles/i)).toBeInTheDocument();
    // The balance bar states itself for a reader through `role="img"`; under the pruned card
    // that name was unreachable too.
    expect(screen.getByRole("img", { name: /kept,.*reclaim/i })).toBeInTheDocument();
  });

  it("wears the selection bar when it is the open card", () => {
    const { container } = render(
      <PersonCard row={row()} selected onSelect={vi.fn()} horizonAt={HORIZON} />,
    );
    expect(container.querySelector(".fair-card.selected")).not.toBeNull();
  });

  it("states the watched share as a share when the account is linked", () => {
    render(<PersonCard row={row()} selected={false} onSelect={vi.fn()} horizonAt={HORIZON} />);
    expect(screen.getByText("58%")).toBeInTheDocument();
    expect(screen.getByText(/they watched/i)).toBeInTheDocument();
  });

  // A requester whose Seerr account nobody linked to a Plex account has no history Reaper
  // can read: `fairness._roll_up` counts plays only inside `if pid is not None`, so
  // `played_by_them` is a structural 0, not a measured one. The card drew that as a red 0%,
  // a confident zero about somebody nobody looked at, on the screen where the operator
  // decides whose files to delete.
  it("says the history is unreadable rather than showing a red zero", () => {
    render(
      <PersonCard
        row={row({ identity: "local:portal:4", plex_id: null, played_by_them: 0 })}
        selected={false}
        onSelect={vi.fn()}
        horizonAt={HORIZON}
      />,
    );

    expect(screen.getByText(/no plex account/i)).toBeInTheDocument();
    expect(screen.queryByText("0%")).not.toBeInTheDocument();
    expect(screen.queryByText(/they watched/i)).not.toBeInTheDocument();
  });

  // The same confident zero reached the other way, and the one the account guard let past: a
  // mirror with nothing in it. Every play is invisible, so every card counted 0 out of a real
  // request total and printed a red 0% about a person whose history was never read at all.
  it("says the history is unreadable when the mirror holds nothing", () => {
    render(
      <PersonCard
        row={row({ played_by_them: 0 })}
        selected={false}
        onSelect={vi.fn()}
        horizonAt={null}
      />,
    );

    expect(screen.getByText(/no watch history/i)).toBeInTheDocument();
    expect(screen.queryByText("0%")).not.toBeInTheDocument();
    expect(screen.queryByText(/they watched/i)).not.toBeInTheDocument();
  });
});

describe("Fairness", () => {
  const report = (rows: RequesterRow[], over: Partial<FairnessReport> = {}): FairnessReport => ({
    total_requests: 344,
    total_reclaimable_bytes: 211 * GB,
    total_reclaimable_items: 16,
    not_in_scan: 7,
    unmatched: [],
    no_snapshot: false,
    horizon_at: HORIZON,
    rows,
    ...over,
  });

  // The page an operator reads to decide whose files to delete, so it is audited with rows in it
  // rather than in the loading state a bare mock hands you (rule 145). The failed read is driven
  // too: it replaces the whole table with one paragraph, and rule 17/36 makes that branch as
  // much a shipped surface as the table it stands in for.
  it("has no accessibility violations once the requesters have landed", async () => {
    apiMock.fairness.mockResolvedValue(report([row()]));
    const { container } = renderWithClient(<Fairness />);
    await screen.findByText(/marlow/);
    await expectNoA11yViolations(container);
  });

  it("has none when the report could not be read", async () => {
    apiMock.fairness.mockRejectedValue(new Error("unreachable"));
    const { container } = renderWithClient(<Fairness />);
    await screen.findByText(/Couldn't load Scales/i);
    await expectNoA11yViolations(container);
  });

  it("says it is loading rather than rendering nothing", () => {
    apiMock.fairness.mockReturnValue(new Promise(() => {}));
    renderWithClient(<Fairness />);
    expect(screen.getByText(/gathering requests/i)).toBeInTheDocument();
  });

  // The affordance no longer carries a live region of its own: it was mounted in the same
  // commit as its text, which several readers never announce, so it read as correct and said
  // nothing (#332). What replaced it is a wait spoken through the always-mounted region only
  // once the wait has been one, so both halves are pinned -- silent while it is quick, spoken
  // when it drags. Asserting only the second would pass against a spinner that announces on
  // every mount, which is the noise this shape exists to avoid.
  it("stays silent while the wait is short", () => {
    vi.useFakeTimers();
    apiMock.fairness.mockReturnValue(new Promise(() => {}));
    renderWithClient(
      <>
        <Announcer />
        <Fairness />
      </>,
    );

    act(() => void vi.advanceTimersByTime(1999));

    expect(spokenText()).toBe("");
  });

  it("says the wait out loud once it has run long", () => {
    vi.useFakeTimers();
    apiMock.fairness.mockReturnValue(new Promise(() => {}));
    renderWithClient(
      <>
        <Announcer />
        <Fairness />
      </>,
    );

    act(() => void vi.advanceTimersByTime(2000));

    expect(spokenText()).toMatch(/still gathering requests/i);
  });

  it("surfaces a load failure explicitly", async () => {
    // The fallback branch: a failure Reaper did not word (a dropped connection) has no
    // sentence worth showing, and a raw fetch message is not operator copy (rule 21).
    apiMock.fairness.mockRejectedValue(new Error("Failed to fetch"));
    renderWithClient(<Fairness />);
    expect(await screen.findByText(/Couldn't load Scales/i)).toBeInTheDocument();
    expect(screen.queryByText(/Failed to fetch/i)).not.toBeInTheDocument();
  });

  it("says what Scales needs when the server explained itself", async () => {
    // An install with Tautulli plus an *arr and no Seerr is scan-ready by the wizard's own
    // account, and Scales is visible and clickable there, so its 400 is the DEFAULT reading of
    // this tab. The server names the services to add; the page dropped that on the floor and
    // said "Couldn't load Scales.", which is a dead tab naming nothing actionable (#412).
    apiMock.fairness.mockRejectedValue(
      new ApiError(400, "Scales needs a Seerr and a Tautulli instance. Add them in Settings."),
    );
    renderWithClient(<Fairness />);

    expect(await screen.findByText(/needs a Seerr and a Tautulli/i)).toBeInTheDocument();
    expect(screen.queryByText(/Couldn't load Scales/i)).not.toBeInTheDocument();
  });

  it("tells the operator to scan first when there is no snapshot", async () => {
    apiMock.fairness.mockResolvedValue(report([], { no_snapshot: true }));
    renderWithClient(<Fairness />);
    expect(await screen.findByText(/run a scan first/i)).toBeInTheDocument();
  });

  it("renders the summary strip and a card per person", async () => {
    apiMock.fairness.mockResolvedValue(report([row()]));
    renderWithClient(<Fairness />);
    expect(await screen.findByText(/16 titles the scan would remove/i)).toBeInTheDocument();
    expect(screen.getByText(/across 1 person/i)).toBeInTheDocument();
    expect(screen.getByText("marlow")).toBeInTheDocument();
  });

  // B-27: this is the state the tile exists for. A fresh portal, or ids the scan has not
  // backfilled, leaves every request unmatched, so there are no people to show and the only
  // thing explaining the empty page is the count of what did not line up. The tile used to be
  // nested inside the has-people branch, so it was hidden exactly when it was needed.
  it("still offers the not-in-the-last-scan tile when nobody is on the board", async () => {
    const onOpenUnmatched = vi.fn();
    apiMock.fairness.mockResolvedValue(report([], { not_in_scan: 40 }));
    renderWithClient(<Fairness onOpenUnmatched={onOpenUnmatched} />);

    expect(
      await screen.findByText(/no available requests are in the last scan/i),
    ).toBeInTheDocument();
    const tile = screen.getByRole("button", { name: /not in the last scan/i });
    expect(tile).toHaveTextContent("40");
    // The chevron drawn on this tile sat inside its text, so it landed in the accessible name and
    // a reader said the punctuation as part of the control (#177). Hidden, not removed -- it is
    // drawn exactly as before. Asserted as the WHOLE name: the substring matcher above was
    // already satisfied by the broken one, which is how this sat here.
    // The four spans carry explicit separators, so the name reads as four things rather than
    // one run-on word -- and the leading count is heard as a number instead of fusing to the
    // label (#284). Asserted as the WHOLE name for the same reason as the chevron above: the
    // substring matcher was already satisfied by the broken string.
    expect(tile).toHaveAccessibleName(
      "40 Not in the last scan requested since, or filtered out See what these are",
    );
    await userEvent.click(tile);
    expect(onOpenUnmatched).toHaveBeenCalled();
  });

  it("says nothing about unmatched requests when there are none", async () => {
    apiMock.fairness.mockResolvedValue(report([], { not_in_scan: 0 }));
    renderWithClient(<Fairness />);
    await screen.findByText(/no available requests are in the last scan/i);
    expect(screen.queryByRole("button", { name: /not in the last scan/i })).toBeNull();
  });

  // Every card's percentage is counted over the mirror's span, so the board names it. It used
  // to be printed only when there WAS one, which left the state with the least evidence -- a
  // mirror that has never synced -- as the one carrying no caveat at all.
  it("names the span the watched figures are counted over, in both directions", async () => {
    apiMock.fairness.mockResolvedValue(report([row()]));
    const { unmount } = renderWithClient(<Fairness />);
    expect(await screen.findByText(/watch history reaches back to/i)).toBeInTheDocument();
    unmount();

    apiMock.fairness.mockResolvedValue(report([row()], { horizon_at: null }));
    renderWithClient(<Fairness />);
    expect(await screen.findByText(/no watch history has been read yet/i)).toBeInTheDocument();
  });

  it("asks App to open a person when their card is clicked", async () => {
    const onSelectPerson = vi.fn();
    apiMock.fairness.mockResolvedValue(report([row()]));
    renderWithClient(<Fairness onSelectPerson={onSelectPerson} />);
    await userEvent.click(await screen.findByRole("button", { name: /marlow/i }));
    expect(onSelectPerson).toHaveBeenCalledWith("plex:7");
  });
});
