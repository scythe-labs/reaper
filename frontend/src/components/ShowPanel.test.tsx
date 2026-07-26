// SPDX-License-Identifier: AGPL-3.0-or-later
// The show panel is where a whole-show Spare or Reap gets made. What is load-bearing here:
//   - a show carries BOTH buttons, because a whole-show reap covers the seasons the scan
//     kept, unlike a condemned movie where reap is a no-op -- but Reap falls away once every
//     season is already condemned;
//   - the decision acts on the show's group key, and a failed save says so.
import { QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { Candidate, Group, Links, Verdict } from "../api";
import { DEFAULT_GENERAL, seedSettings } from "../test/apiFixtures";
import { testQueryClient } from "../test/queryClient";
import { ShowPanel } from "./ShowPanel";

const { apiMock } = vi.hoisted(() => ({
  apiMock: { override: vi.fn(), clearOverride: vi.fn(), general: vi.fn() },
}));

vi.mock("../api", () => ({ api: apiMock }));

const NO_LINKS: Links = {
  plex: null,
  tautulli: null,
  seerr: null,
  radarr: null,
  sonarr: null,
  imdb: null,
  tmdb: null,
  rotten_tomatoes: null,
  trakt: null,
};

function season(n: number, verdict: Verdict, extra: Partial<Candidate> = {}): Candidate {
  const c: Candidate = {
    id: n,
    media_key: `sonarr:1:${n}`,
    title: "Example Show",
    media_type: "season",
    size_bytes: 1024 ** 3,
    verdict,
    score: 80,
    coverage_bp: 10_000,
    first_flagged_at: null,
    year: 2011,
    summary: null,
    poster_url: null,
    requested_by: null,
    group_key: "sonarr:show:1",
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
    season_number: n,
    group_seasons: null,
    ...extra,
  };
  if (extra.override_own === undefined) c.override_own = c.override;
  return c;
}

function group(seasons: Candidate[]): Group {
  return {
    group_key: "sonarr:show:1",
    title: "Example Show",
    year: 2011,
    poster_url: null,
    summary: null,
    size_bytes: seasons.reduce((sum, s) => sum + (s.size_bytes ?? 0), 0),
    unknown_size_seasons: 0,
    reason: null,
    library: null,
    chip: null,
    show_override: null,
    show_spare_expires_at: null,
    links: NO_LINKS,
    show_status: "ended",
    seasons,
  };
}

function renderPanel(g: Group) {
  const queryClient = seedSettings(testQueryClient());
  return render(
    <QueryClientProvider client={queryClient}>
      <ShowPanel group={g} onOpenSeason={() => {}} onClose={() => {}} />
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  vi.clearAllMocks();
  // The Spare control reads the default spare length (["general-settings"]) on its own, so the
  // mock has to answer it or the panel renders a failed read. Rule 135.
  apiMock.general.mockResolvedValue(DEFAULT_GENERAL);
});

describe("the show panel's whole-show buttons", () => {
  it("offers both Spare and Reap for a part-condemned show", () => {
    renderPanel(group([season(1, "condemn"), season(2, "protect")]));
    expect(screen.getByRole("button", { name: "Spare" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Reap" })).toBeInTheDocument();
  });

  it("drops Reap once every season is condemned", () => {
    renderPanel(group([season(1, "condemn"), season(2, "condemn")]));
    expect(screen.getByRole("button", { name: "Spare" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /^Reap$/ })).not.toBeInTheDocument();
  });

  it("reaps the whole show through its group key", async () => {
    const { userEvent } = await import("@testing-library/user-event");
    const user = userEvent.setup();
    apiMock.override.mockResolvedValue(undefined);
    renderPanel(group([season(1, "condemn"), season(2, "protect")]));

    await user.click(screen.getByRole("button", { name: "Reap" }));
    // A reap carries no length (spareDays 0, ignored server-side for a reap).
    await waitFor(() =>
      expect(apiMock.override).toHaveBeenCalledWith("sonarr:show:1", "reap", undefined, 0),
    );
  });

  it("says so when the save fails", async () => {
    const { userEvent } = await import("@testing-library/user-event");
    const user = userEvent.setup();
    apiMock.override.mockRejectedValue(new Error("boom"));
    renderPanel(group([season(1, "condemn"), season(2, "protect")]));

    await user.click(screen.getByRole("button", { name: "Spare" }));
    expect(await screen.findByText("Couldn't save that. Try again.")).toBeInTheDocument();
  });
});
