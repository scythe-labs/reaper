// SPDX-License-Identifier: AGPL-3.0-or-later
// Scales reads the last scan, but it is the screen an operator scans before a run, so its
// states have to be honest: a reclaimable card names the disk and links each title to its
// real card; a clean card says so plainly; and the page says out loud when it is loading,
// could not load, or has no scan to sit on.
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
    ...over,
  };
}

function renderWithClient(ui: ReactElement) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(<QueryClientProvider client={client}>{ui}</QueryClientProvider>);
}

describe("PersonCard", () => {
  it("leads with the reclaimable disk and opens each title's real card", async () => {
    const onOpenItem = vi.fn();
    render(<PersonCard row={row()} onOpenItem={onOpenItem} onOpenGroup={vi.fn()} />);

    expect(screen.getByText(/earning its keep/i)).toBeInTheDocument();
    expect(screen.getByText(/to reclaim · 3 titles/i)).toBeInTheDocument();

    // Titles are hidden until the card is opened.
    expect(screen.queryByText("The Long Shoreline")).not.toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: /marlow/i }));

    expect(screen.getByText("The Long Shoreline")).toBeInTheDocument();
    expect(screen.getAllByText(/^Reclaimable ·/).length).toBe(3);

    // The chip opens that exact item, no tab hunting. (Query by title text, not the button's
    // accessible name -- the whole card is also a button and would match too.)
    const chip = screen.getByText("The Long Shoreline").closest("button");
    await userEvent.click(chip!);
    expect(onOpenItem).toHaveBeenCalledWith(101);
  });

  it("opens a whole show by its group, not one season", async () => {
    const onOpenGroup = vi.fn();
    render(
      <PersonCard
        row={row({
          reclaimable_items: 1,
          reclaimable_bytes: 5 * GB,
          reclaimable: [{ title: "A Show", size_bytes: 5 * GB, item_id: null, group_key: "tv:7" }],
        })}
        onOpenItem={vi.fn()}
        onOpenGroup={onOpenGroup}
      />,
    );
    await userEvent.click(screen.getByRole("button", { name: /marlow/i }));
    await userEvent.click(screen.getByText("A Show").closest("button")!);
    expect(onOpenGroup).toHaveBeenCalledWith("tv:7");
  });

  it("says a clean requester is clean, and does not offer to expand", () => {
    render(
      <PersonCard
        row={row({ reclaimable_items: 0, reclaimable_bytes: 0, reclaimable: [] })}
        onOpenItem={vi.fn()}
        onOpenGroup={vi.fn()}
      />,
    );
    expect(screen.getByText(/nothing to reclaim/i)).toBeInTheDocument();
    expect(screen.queryByRole("button")).not.toBeInTheDocument();
  });

  it("names the shortfall when the reclaimable list is capped", async () => {
    render(
      <PersonCard row={row({ reclaimable_items: 40 })} onOpenItem={vi.fn()} onOpenGroup={vi.fn()} />,
    );
    await userEvent.click(screen.getByRole("button", { name: /marlow/i }));
    expect(screen.getByText("+37 more not shown")).toBeInTheDocument();
  });
});

describe("Fairness", () => {
  const report = (rows: RequesterRow[], over: Partial<FairnessReport> = {}): FairnessReport => ({
    total_requests: 344,
    total_reclaimable_bytes: 211 * GB,
    total_reclaimable_items: 16,
    not_in_scan: 7,
    no_snapshot: false,
    horizon_at: "2018-01-11T00:00:00+00:00",
    rows,
    ...over,
  });

  it("says it is loading rather than rendering nothing", () => {
    apiMock.fairness.mockReturnValue(new Promise(() => {}));
    renderWithClient(<Fairness />);
    expect(screen.getByText(/loading/i)).toBeInTheDocument();
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
    expect(screen.getByText(/across 1 people/i)).toBeInTheDocument();
    expect(screen.getByText("marlow")).toBeInTheDocument();
  });
});
