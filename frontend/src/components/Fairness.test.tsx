// SPDX-License-Identifier: AGPL-3.0-or-later
// Scales reads the last scan, but it is the screen an operator scans before a run, so its
// states have to be honest: a reclaimable card names the disk; a clean card says so plainly;
// and either one opens the person's full breakdown. The page says out loud when it is
// loading, could not load, or has no scan to sit on.
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactElement } from "react";
import { describe, expect, it, vi } from "vitest";
import type { FairnessReport, RequesterRow } from "../api";
import { Fairness, PersonCard } from "./Fairness";

const { apiMock } = vi.hoisted(() => ({
  apiMock: { fairness: vi.fn() },
}));

vi.mock("../api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../api")>()),
  api: apiMock,
}));

const GB = 1024 ** 3;

function row(over: Partial<RequesterRow> = {}): RequesterRow {
  return {
    identity: "plex:7",
    name: "marlow",
    requests_made: 52,
    gb_granted_bytes: 549 * GB,
    played_by_them: 30,
    reclaimable_items: 3,
    reclaimable_bytes: 13 * GB,
    reclaimable: [
      { title: "The Long Shoreline", size_bytes: 6 * GB, item_id: 101, group_key: null },
      { title: "Nightferry", size_bytes: 4 * GB, item_id: 102, group_key: null },
      { title: "Paper Harbor", size_bytes: 3 * GB, item_id: 103, group_key: null },
    ],
    seerr_total: 88,
    movie_at_limit: false,
    tv_at_limit: false,
    ...over,
  };
}

function renderWithClient(ui: ReactElement) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(<QueryClientProvider client={client}>{ui}</QueryClientProvider>);
}

describe("PersonCard", () => {
  it("leads with the reclaimable disk and opens the person's breakdown", async () => {
    const onSelect = vi.fn();
    render(<PersonCard row={row()} selected={false} onSelect={onSelect} />);

    expect(screen.getByText(/earning its keep/i)).toBeInTheDocument();
    expect(screen.getByText(/to reclaim · 3 titles/i)).toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: /marlow/i }));
    expect(onSelect).toHaveBeenCalledWith("plex:7");
  });

  // The bug this whole change fixes: a requester with nothing reclaimable must still open.
  it("opens a clean requester too, and says it is clean", async () => {
    const onSelect = vi.fn();
    render(
      <PersonCard
        row={row({ reclaimable_items: 0, reclaimable_bytes: 0, reclaimable: [] })}
        selected={false}
        onSelect={onSelect}
      />,
    );

    expect(screen.getByText(/nothing to reclaim/i)).toBeInTheDocument();
    const card = screen.getByRole("button", { name: /marlow/i });
    expect(card).toBeInTheDocument();
    await userEvent.click(card);
    expect(onSelect).toHaveBeenCalledWith("plex:7");
  });

  it("opens on Enter and Space, for keyboard users", async () => {
    const onSelect = vi.fn();
    render(<PersonCard row={row()} selected={false} onSelect={onSelect} />);
    const card = screen.getByRole("button", { name: /marlow/i });
    card.focus();
    await userEvent.keyboard("{Enter}");
    await userEvent.keyboard(" ");
    expect(onSelect).toHaveBeenCalledTimes(2);
  });

  it("wears the selection bar when it is the open card", () => {
    const { container } = render(<PersonCard row={row()} selected onSelect={vi.fn()} />);
    expect(container.querySelector(".fair-card.selected")).not.toBeNull();
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
    horizon_at: "2018-01-11T00:00:00+00:00",
    rows,
    ...over,
  });

  it("says it is loading rather than rendering nothing", () => {
    apiMock.fairness.mockReturnValue(new Promise(() => {}));
    renderWithClient(<Fairness />);
    expect(screen.getByRole("status")).toHaveTextContent(/gathering requests/i);
  });

  it("surfaces a load failure explicitly", async () => {
    apiMock.fairness.mockRejectedValue(new Error("Seerr unreachable"));
    renderWithClient(<Fairness />);
    expect(await screen.findByText(/Couldn't load Scales/i)).toBeInTheDocument();
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

  it("asks App to open a person when their card is clicked", async () => {
    const onSelectPerson = vi.fn();
    apiMock.fairness.mockResolvedValue(report([row()]));
    renderWithClient(<Fairness onSelectPerson={onSelectPerson} />);
    await userEvent.click(await screen.findByRole("button", { name: /marlow/i }));
    expect(onSelectPerson).toHaveBeenCalledWith("plex:7");
  });
});
