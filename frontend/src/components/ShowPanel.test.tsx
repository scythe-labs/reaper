// SPDX-License-Identifier: AGPL-3.0-or-later
// The show panel is where a whole-show Spare or Reap gets made. What is load-bearing here:
//   - a show carries BOTH buttons, because a whole-show reap covers the seasons the scan
//     kept, unlike a condemned movie where reap is a no-op -- but Reap falls away once every
//     season is already condemned;
//   - the decision acts on the show's group key, and a failed save says so.
import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { Candidate, Group, Links, ReasonKey, Verdict } from "../api";
import { expectNoA11yViolations } from "../test/a11y";
import { DEFAULT_GENERAL, seedSettings } from "../test/apiFixtures";
import { testQueryClient } from "../test/queryClient";
import { renderWithProviders } from "../test/renderWithProviders";
import { ShowPanel } from "./ShowPanel";

const { apiMock } = await vi.hoisted(async () => ({
  apiMock: (await import("../test/apiMock")).makeApiMock(),
}));

vi.mock("../api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../api")>()),
  api: apiMock,
}));

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
  match_candidates: [],
};

/** A row frozen before details were typed: the stored sentence wrapped as the one legacy
 *  reason, composed verbatim (docs/history/I18N_PLAN.md §5, #899). */
function legacy(text: string): ReasonKey {
  return { k: "legacy", p: { text } };
}

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
    video_resolution: null,
    library: null,
    dormant_days: null,
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
    collections: null,
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
    library: null,
    chip: null,
    show_override: null,
    show_spare_expires_at: null,
    links: NO_LINKS,
    show_status: "ended",
    seasons,
  };
}

function renderPanel(g: Group, onOpenSeason: (id: number, lane: Verdict) => void = () => {}) {
  const queryClient = seedSettings(testQueryClient());
  return renderWithProviders(
    <ShowPanel group={g} onOpenSeason={onOpenSeason} onClose={() => {}} />,
    { client: queryClient },
  );
}

beforeEach(() => {
  vi.clearAllMocks();
  // The Spare control reads the default spare length (["general-settings"]) on its own, so the
  // mock has to answer it or the panel renders a failed read. Rule 135.
  apiMock.general.mockResolvedValue(DEFAULT_GENERAL);
});

describe("the show panel's whole-show buttons", () => {
  // One press here decides every season of a show at once, so this panel removes the most files
  // per click in the app. An operator who cannot hear which of the two buttons they are on reaps
  // a whole show meaning to keep it.
  it("has no accessibility violations", async () => {
    const { container } = renderPanel(group([season(1, "condemn"), season(2, "protect")]));
    await screen.findByRole("button", { name: "Spare" });
    await expectNoA11yViolations(container);
  });

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
    const user = userEvent.setup();
    apiMock.override.mockRejectedValue(new Error("boom"));
    renderPanel(group([season(1, "condemn"), season(2, "protect")]));

    await user.click(screen.getByRole("button", { name: "Spare" }));
    expect(await screen.findByText("Couldn't save that. Try again.")).toBeInTheDocument();
  });
});

// Opening a season from here crosses lanes as often as not: this list carries every season the
// show has, whatever Reaper decided, while the queue behind the panel is showing one of the three.
// So the row hands over the lane the season is really in, and the caller lands the operator on it.
// A raw `season.verdict` read is a different answer the moment a hand decision is on the season,
// and getting it wrong reopens the bug this closes -- a panel open above a list its subject is not
// in, with no card to scroll to and no j/k step that reaches it.
describe("the show panel's jump into a season", () => {
  // Written from what each state MEANS for the file (rule 119), not from laneOf's branches. The
  // last three are the cases a raw verdict read gets wrong, and they miss in BOTH directions:
  // two say "condemned" over a file that is kept, one says "left to decide" over one that is not.
  const cases: { what: string; row: Candidate; lane: Verdict }[] = [
    {
      what: "an untouched season stays on the lane the scan put it on",
      row: season(1, "abstain"),
      lane: "abstain",
    },
    {
      what: "a spared season is with the kept ones, whatever the scan said",
      row: season(2, "condemn", { override: "spare" }),
      lane: "protect",
    },
    {
      what: "a hand reap the engine honors is with the condemned",
      row: season(3, "abstain", { override: "reap", override_effective: true }),
      lane: "condemn",
    },
    {
      what: "a hand reap the engine will not honor yet is still kept",
      row: season(4, "abstain", { override: "reap", override_effective: false }),
      lane: "protect",
    },
  ];

  it.each(cases)("$what", async ({ row, lane }) => {
    const onOpenSeason = vi.fn();
    renderPanel(group([row]), onOpenSeason);
    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: /Season \d/ }));
    expect(onOpenSeason).toHaveBeenCalledWith(row.id, lane);
  });
});

// The panel is a full-screen dialog under 900px, so it has to say what it is. Its name comes from
// its own <h2> rather than a second copy of the title in an aria-label (rule 144). One of these
// per panel: six surfaces render WhyShell, and the name is the one part of the contract the shell
// cannot supply for them.
describe("the show panel's accessible name", () => {
  it("names itself from the show title it is showing", () => {
    renderPanel(group([season(1, "condemn")]));
    expect(screen.getByRole("complementary", { name: /Example Show/ })).toBeInTheDocument();
  });
});

// A show whose match Reaper refused to settle. The season why-panel offers a way into each
// Plex row it was choosing between; this panel is served the same list and used to drop it,
// so a conflicted SHOW named a problem in Plex with nothing to open -- its own header link is
// built from the show's rating key, which is null on exactly these rows (rule 72).
describe("the show panel's candidate Plex rows", () => {
  it("offers a way into each Plex row it could not choose between", () => {
    renderPanel({
      ...group([season(1, "abstain")]),
      reason_key: legacy("Kept to be safe: Plex and Sonarr describe this show differently."),
      links: {
        ...NO_LINKS,
        match_candidates: [
          { rating_key: 555, plex: "https://plex.example/555", tautulli: "https://taut/555" },
          { rating_key: 777, plex: "https://plex.example/777", tautulli: "https://taut/777" },
        ],
      },
    });
    expect(screen.getByText(/Reaper saw 2 possible matches/i)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /Plex 1/ })).toHaveAttribute(
      "href",
      "https://plex.example/555",
    );
    expect(screen.getByRole("link", { name: /Tautulli 2/ })).toHaveAttribute(
      "href",
      "https://taut/777",
    );
  });

  it("shows no candidate row on a show Reaper did tie to one Plex entry", () => {
    renderPanel(group([season(1, "condemn")]));
    expect(screen.queryByText(/possible matches/i)).not.toBeInTheDocument();
  });
});

// Which season names may wrap. The row is nowrap so its height holds still while the list
// narrows, and for the two labels the panel composes itself that costs nothing. The third branch
// is different: a season with no number falls back to the server's own title, which can be one
// long unbroken string with nothing to break on, and nowrap paints it out of the panel (rule
// 139, #220). So only that branch is marked, and both directions are driven -- the unmarked
// branch is the state the fixture hands you for free, which is exactly where a marker leaking
// onto every row would hide, taking the steady row height with it (rule 145).
describe("the show panel's season names", () => {
  it("lets the unnumbered season wrap, because that name is the server's own", () => {
    const { container } = renderPanel(
      group([season(1, "condemn", { season_number: null, title: "A_Long_Unbroken_Season_Title" })]),
    );
    const name = container.querySelector(".panel-season-name");
    expect(name).toHaveTextContent("A_Long_Unbroken_Season_Title");
    expect(name).toHaveClass("is-server-title");
  });

  it("holds the line for both labels it composes itself", () => {
    const { container } = renderPanel(
      group([season(3, "condemn"), season(0, "condemn", { id: 9 })]),
    );
    const names = [...container.querySelectorAll(".panel-season-name")];
    expect(names.map((n) => n.textContent)).toEqual(["Season 3", "Specials"]);
    for (const name of names) expect(name).not.toHaveClass("is-server-title");
  });
});
