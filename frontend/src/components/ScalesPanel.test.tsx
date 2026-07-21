// SPDX-License-Identifier: AGPL-3.0-or-later
// The Scales person panel explains one requester without deciding anything. It opens for
// every requester -- including one with nothing reclaimable, whose panel is still their whole
// request story -- and each title row jumps to that item's real card in Review.
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import type { PersonDetail, PersonTitle } from "../api";
import { ScalesPanel } from "./ScalesPanel";

const GB = 1024 ** 3;

function title(over: Partial<PersonTitle> = {}): PersonTitle {
  return {
    title: "The Long Shoreline",
    year: 2021,
    media_type: "movie",
    is_4k: false,
    size_bytes: 6 * GB,
    requested_at: "2023-11-02T00:00:00+00:00",
    available_at: "2023-11-03T00:00:00+00:00",
    watched_by_them: 0,
    verdict: "condemn",
    item_id: 101,
    group_key: null,
    co_requesters: [],
    poster_url: null,
    ...over,
  };
}

function detail(over: Partial<PersonDetail> = {}): PersonDetail {
  return {
    plex_id: 3,
    name: "marlow",
    seerr_total: 88,
    requests_in_scan: 4,
    gb_granted_bytes: 100 * GB,
    played_by_them: 3,
    reclaimable_items: 1,
    reclaimable_bytes: 6 * GB,
    not_in_scan: 0,
    quota: {
      seerr_total: 88,
      movie: { limit: 1, days: 14, at_limit: false },
      tv: { limit: null, days: null, at_limit: false },
    },
    titles: [title()],
    ...over,
  };
}

describe("ScalesPanel", () => {
  it("names the person and their request count", () => {
    render(
      <ScalesPanel
        detail={detail()}
        onClose={vi.fn()}
        onOpenItem={vi.fn()}
        onOpenGroup={vi.fn()}
      />,
    );
    expect(screen.getByRole("heading", { name: "marlow" })).toBeInTheDocument();
    expect(screen.getByText(/4 requests in the last scan/i)).toBeInTheDocument();
  });

  it("reads request limits in plain words, and flags an at-limit type", () => {
    render(
      <ScalesPanel
        detail={detail({
          quota: {
            seerr_total: 88,
            movie: { limit: 1, days: 14, at_limit: false },
            tv: { limit: 2, days: 30, at_limit: true },
          },
        })}
        onClose={vi.fn()}
        onOpenItem={vi.fn()}
        onOpenGroup={vi.fn()}
      />,
    );
    expect(screen.getByText("1 per 14 days")).toBeInTheDocument();
    expect(screen.getByText(/2 per 30 days · at limit/)).toBeInTheDocument();
  });

  it("opens a movie by its item id from its row", async () => {
    const onOpenItem = vi.fn();
    render(
      <ScalesPanel
        detail={detail()}
        onClose={vi.fn()}
        onOpenItem={onOpenItem}
        onOpenGroup={vi.fn()}
      />,
    );
    await userEvent.click(screen.getByRole("button", { name: /The Long Shoreline/i }));
    expect(onOpenItem).toHaveBeenCalledWith(101);
  });

  it("opens a show by its group key, not one season", async () => {
    const onOpenGroup = vi.fn();
    render(
      <ScalesPanel
        detail={detail({
          titles: [title({ title: "A Show", media_type: "season", item_id: null, group_key: "tv:7" })],
        })}
        onClose={vi.fn()}
        onOpenItem={vi.fn()}
        onOpenGroup={onOpenGroup}
      />,
    );
    await userEvent.click(screen.getByRole("button", { name: /A Show/i }));
    expect(onOpenGroup).toHaveBeenCalledWith("tv:7");
  });

  it("shows a title's fate and whether they watched it", () => {
    render(
      <ScalesPanel
        detail={detail({
          titles: [
            title({ verdict: "protect", watched_by_them: 4 }),
            title({ title: "Nightferry", verdict: "abstain", item_id: 102, size_bytes: null }),
          ],
        })}
        onClose={vi.fn()}
        onOpenItem={vi.fn()}
        onOpenGroup={vi.fn()}
      />,
    );
    expect(screen.getByText("Kept")).toBeInTheDocument();
    expect(screen.getByText("Left to decide")).toBeInTheDocument();
    // A movie shows its raw plays.
    expect(screen.getByText(/watched 4×/)).toBeInTheDocument();
    expect(screen.getByText(/not watched/)).toBeInTheDocument();
    // A title the arr would not size reads "Size unknown", never a false 0 B.
    expect(screen.getByText("Size unknown")).toBeInTheDocument();
  });

  it("reads a series' watch figure as distinct episodes, not raw plays", () => {
    render(
      <ScalesPanel
        detail={detail({
          titles: [
            title({ title: "A Show", media_type: "season", group_key: "tv:7", item_id: null, watched_by_them: 62 }),
            title({ title: "One Season", media_type: "season", item_id: 5, watched_by_them: 1 }),
          ],
        })}
        onClose={vi.fn()}
        onOpenItem={vi.fn()}
        onOpenGroup={vi.fn()}
      />,
    );
    expect(screen.getByText("62 episodes watched")).toBeInTheDocument();
    expect(screen.getByText("1 episode watched")).toBeInTheDocument();
  });

  // The clean case the whole change exists for: nothing reclaimable still opens, and says so.
  it("says None reclaimable when the scan condemns nothing of theirs", () => {
    render(
      <ScalesPanel
        detail={detail({
          reclaimable_items: 0,
          reclaimable_bytes: 0,
          titles: [title({ verdict: "protect", watched_by_them: 2 })],
        })}
        onClose={vi.fn()}
        onOpenItem={vi.fn()}
        onOpenGroup={vi.fn()}
      />,
    );
    expect(screen.getByText("None")).toBeInTheDocument();
    expect(screen.getByText(/all still earning their keep/i)).toBeInTheDocument();
  });

  it("does not print the year twice when the title already carries it", () => {
    render(
      <ScalesPanel
        detail={detail({ titles: [title({ title: "Couples Therapy (2019)", year: 2019 })] })}
        onClose={vi.fn()}
        onOpenItem={vi.fn()}
        onOpenGroup={vi.fn()}
      />,
    );
    expect(screen.getByText(/Couples Therapy \(2019\)/)).toBeInTheDocument();
    expect(screen.queryByText(/\(2019\) \(2019\)/)).not.toBeInTheDocument();
  });

  it("names how many of their requests the scan has not seen", () => {
    render(
      <ScalesPanel
        detail={detail({ not_in_scan: 2 })}
        onClose={vi.fn()}
        onOpenItem={vi.fn()}
        onOpenGroup={vi.fn()}
      />,
    );
    expect(screen.getByText(/2 of their requests aren't in the last scan/i)).toBeInTheDocument();
  });

  it("closes on Escape and on the close button", async () => {
    const onClose = vi.fn();
    render(
      <ScalesPanel detail={detail()} onClose={onClose} onOpenItem={vi.fn()} onOpenGroup={vi.fn()} />,
    );
    await userEvent.click(screen.getByRole("button", { name: /close/i }));
    await userEvent.keyboard("{Escape}");
    expect(onClose).toHaveBeenCalledTimes(2);
  });
});
