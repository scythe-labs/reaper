// SPDX-License-Identifier: AGPL-3.0-or-later
// The scoring receipt. Three things are load-bearing here:
//
//   1. The shares ADD UP to the number beside them. An operator who cannot add the rows up
//      cannot check the score, which is the whole complaint this panel answers.
//   2. The four row states read differently on screen. "We could not look" is not "we
//      looked and it was fine", and a row with nothing recorded must never be drawn as if
//      it argued for keeping the file.
//   3. The rules that did not apply are tucked away, never dropped.
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import type { CandidateDetail, SignalContribution } from "../api";
import { WhyPanel, allocateShares } from "./WhyPanel";

vi.mock("../api", () => ({ api: { override: vi.fn(), clearOverride: vi.fn() } }));

function signal(over: Partial<SignalContribution> & { id: string }): SignalContribution {
  return { contribution: 0, weight: 10, detail: "a reason", evaluated: true, ...over };
}

/** The six rows the operator was looking at: 75/75, 30/80, 20/20, 20/20, 10/60, 0/25.
 *  Total weight 280, total pressure 155, so the score is 55. */
const WORKED_ROWS: SignalContribution[] = [
  signal({ id: "unwatched", contribution: 75, weight: 75, detail: "not watched in 5 years", state: "adds" }),
  signal({ id: "season_rank", contribution: 30, weight: 80, detail: "a later season", state: "adds" }),
  signal({ id: "few_watchers", contribution: 20, weight: 20, detail: "1 person watched it", state: "adds" }),
  signal({ id: "size", contribution: 20, weight: 20, detail: "takes 40 GiB", state: "adds" }),
  signal({ id: "low_rating", contribution: 10, weight: 60, detail: "rated 6.4", state: "adds" }),
  signal({ id: "my rule", weight: 25, detail: "not on the shelf", state: "not_applicable" }),
];

function detail(
  signals: SignalContribution[],
  over: Partial<CandidateDetail> = {},
): CandidateDetail {
  return {
    id: 1,
    media_key: "sonarr:1:2:3",
    title: "Example Show",
    media_type: "season",
    size_bytes: 1024 ** 3,
    verdict: "abstain",
    score: 55,
    coverage_bp: 10_000,
    first_flagged_at: null,
    year: 2012,
    summary: null,
    poster_url: null,
    requested_by: null,
    group_key: null,
    group_title: null,
    group_condemned_count: null,
    group_condemned_bytes: null,
    group_unknown_size: null,
    video_resolution: null,
    dormant_for: null,
    reason: null,
    spared: false,
    override: null,
    override_effective: null,
    chip: null,
    season_number: 3,
    group_seasons: null,
    show_status: null,
    content_rating: null,
    runtime_minutes: null,
    genres: [],
    ratings: null,
    links: {
      plex: null,
      tautulli: null,
      seerr: null,
      radarr: null,
      sonarr: null,
      imdb: null,
      tmdb: null,
      rotten_tomatoes: null,
      trakt: null,
    },
    explanation: {
      score: 55,
      base_score: 55,
      keep_discount: 0,
      threshold: 70,
      coverage: 10_000,
      signals,
      protections_fired: [],
      protections_checked: [],
      protections_unknown: [],
    },
    ...over,
  };
}

function show(item: CandidateDetail) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <WhyPanel item={item} onClose={() => {}} />
    </QueryClientProvider>,
  );
}

/** The group box a heading sits in, so a row's points can be read in context. */
function groupOf(title: string): HTMLElement {
  const box = screen.getByRole("heading", { name: title }).closest(".sig-group");
  expect(box).not.toBeNull();
  return box as HTMLElement;
}

/** The sum sentence, which must be its OWN element to be its own flex item: as a bare text
 *  node it merges with the label before it into one anonymous item, the row's gap never
 *  lands between them, and the operator reads "Couldn't checkPoints add up to the score."
 *  An element-scoped query is what catches that; a contains-check over the line's
 *  textContent cannot, because the concatenated string is identical either way. */
function legendSum(text: string): HTMLElement {
  const el = screen.getByText(text);
  expect(el.closest(".sig-legend")).not.toBeNull();
  return el;
}

/** The rows a group shows without opening anything: its own list, not the disclosure's. */
function visibleRows(group: HTMLElement): number {
  return group.querySelectorAll(":scope > ul.signals > li").length;
}

/** N rows that all push, so a group runs past the six-row limit. */
function pushRows(n: number, weight = 10): SignalContribution[] {
  return Array.from({ length: n }, (_, i) =>
    signal({
      id: `rule ${String(i).padStart(2, "0")}`,
      contribution: weight,
      weight,
      detail: `reason ${i}`,
      state: "adds",
    }),
  );
}

describe("allocateShares", () => {
  it("hands out points so the rows sum to the score, not to 56", () => {
    // Rounding each row on its own gives 27+11+7+7+4 = 56 against a 55.
    const shares = allocateShares([75, 30, 20, 20, 10, 0], 55);
    expect(shares).toEqual([27, 11, 7, 7, 3, 0]);
    expect(shares.reduce((a, b) => a + b, 0)).toBe(55);
  });

  it("gives every row nothing when nothing pushed", () => {
    expect(allocateShares([0, 0, 0], 0)).toEqual([0, 0, 0]);
    // A target with no pressure behind it cannot be split, so no row claims it.
    expect(allocateShares([0, 0], 12)).toEqual([0, 0]);
  });

  it("gives a lone row the whole score", () => {
    expect(allocateShares([42], 70)).toEqual([70]);
  });

  it("breaks a tie the same way on every render", () => {
    // Three equal rows over a target that does not divide by three: the leftover point
    // goes to the earliest row, and it goes there every single time.
    const once = allocateShares([10, 10, 10], 10);
    expect(once).toEqual([4, 3, 3]);
    expect(allocateShares([10, 10, 10], 10)).toEqual(once);
  });
});

describe("the scoring receipt", () => {
  it("shows each row's share, adding up to the group total", () => {
    show(detail(WORKED_ROWS));

    const pushed = groupOf("Pushed to remove");
    expect(within(pushed).getByText("+55")).toBeTruthy();
    for (const points of ["+27", "+11", "+3"]) {
      expect(within(pushed).getByText(points)).toBeTruthy();
    }
    // The raw-weight form the operator had to divide by hand is gone.
    expect(screen.queryByText("/80")).toBeNull();
    expect(legendSum("Points add up to the score.")).toBeTruthy();
  });

  it("tucks the rules that did not apply behind a disclosure", () => {
    show(detail(WORKED_ROWS));

    expect(screen.getByText("1 didn't apply here").closest("details")).not.toBeNull();
    // Tucked away, never dropped: the row is still in the document.
    expect(screen.getByText("not on the shelf")).toBeTruthy();
  });

  it("renders the four states apart", () => {
    show(
      detail([
        signal({ id: "unwatched", contribution: 40, weight: 40, detail: "not watched in 5 years", state: "adds" }),
        signal({ id: "few_watchers", weight: 30, detail: "4 people watched it", state: "argues_keep" }),
        signal({
          id: "low_rating",
          weight: 20,
          detail: "could not read its rating",
          evaluated: false,
          state: "unreadable",
        }),
        signal({ id: "my rule", weight: 10, detail: "not on the shelf", state: "not_applicable" }),
      ]),
    );

    expect(groupOf("Pushed to remove")).toBeTruthy();
    expect(groupOf("Argued to keep")).toBeTruthy();
    // "Couldn't check" keeps its own group and its own words, never folded in with the
    // rows Reaper did read.
    expect(groupOf("Couldn't check")).toBeTruthy();
    expect(screen.getByText("could not read its rating").closest("li")).toHaveClass("sig-unreadable");
    expect(screen.getByText("4 people watched it").closest("li")).toHaveClass("sig-argues_keep");
    expect(screen.getByText("not on the shelf").closest("li")).toHaveClass("sig-not_applicable");
  });

  it("reads a row with no recorded state as one that did not apply", () => {
    // A snapshot taken before the backend recorded a state. Claiming this row argued for
    // keeping would overstate the case for keeping the file.
    show(
      detail([
        signal({ id: "unwatched", contribution: 40, weight: 40, detail: "not watched in 5 years" }),
        signal({ id: "few_watchers", weight: 30, detail: "5 people watched it" }),
        signal({ id: "low_rating", weight: 20, detail: "could not read its rating", evaluated: false }),
      ]),
    );

    expect(screen.queryByRole("heading", { name: "Argued to keep" })).toBeNull();
    expect(screen.getByText("1 didn't apply here")).toBeTruthy();
    // An old unreadable row still reads as unreadable, on `evaluated` alone.
    expect(groupOf("Couldn't check")).toBeTruthy();
  });

  it("says the points come before the keeps when a keep lowered the score", () => {
    const base = detail(WORKED_ROWS);
    show(
      detail(WORKED_ROWS, {
        score: 40,
        explanation: {
          ...base.explanation,
          score: 40,
          base_score: 55,
          keep_discount: 15,
          keeps: [
            {
              name: "asked for lately",
              discount: 15,
              max_discount: 20,
              detail: "requested 30 days ago",
              evaluated: true,
            },
          ],
        },
      }),
    );

    // The rows add to 55, the score reads 40, and the legend says which is which. The
    // sentence sits on the legend line itself, after the three swatch keys.
    expect(document.querySelector(".sig-legend")?.textContent).toContain("Couldn't check");
    expect(legendSum("Points before the keep rules.")).toBeTruthy();
    expect(within(groupOf("Pushed to remove")).getByText("+55")).toBeTruthy();
  });

  it("adds up to the heading when the base score sits on a rounding boundary", () => {
    // A base of 54.464 stores as score 54 and base_score 54.5. Rounding the stored
    // 1-decimal value a second time gives 55, so the rows would claim to add up to a
    // number the heading never shows. The receipt has to match what is printed.
    const base = detail(WORKED_ROWS);
    show(
      detail(WORKED_ROWS, {
        score: 54,
        explanation: { ...base.explanation, score: 54, base_score: 54.5, keep_discount: 0 },
      }),
    );

    expect(screen.getByRole("heading", { name: "Why it scored 54" })).toBeTruthy();
    expect(within(groupOf("Pushed to remove")).getByText("+54")).toBeTruthy();
    expect(legendSum("Points add up to the score.")).toBeTruthy();
  });

  it("never reads as a confident keep when nothing could be checked", () => {
    const rows = WORKED_ROWS.map((r) => ({
      ...r,
      contribution: 0,
      evaluated: false,
      state: "unreadable" as const,
    }));
    const base = detail(rows);
    show(
      detail(rows, {
        score: 0,
        explanation: { ...base.explanation, score: 0, base_score: 0, keep_discount: 0 },
      }),
    );

    expect(groupOf("Couldn't check")).toBeTruthy();
    expect(screen.queryByRole("heading", { name: "Pushed to remove" })).toBeNull();
    expect(screen.queryByRole("heading", { name: "Argued to keep" })).toBeNull();
  });

  it("says what an unread reason means once for the group, not once per row", () => {
    const rows = [0, 1, 2].map((i) =>
      signal({
        id: `unread ${i}`,
        weight: 20,
        detail: `could not read reason ${i}`,
        evaluated: false,
        state: "unreadable" as const,
      }),
    );
    const base = detail(rows);
    show(
      detail(rows, {
        score: 0,
        explanation: { ...base.explanation, score: 0, base_score: 0, keep_discount: 0 },
      }),
    );

    // Three unread rows, one sentence. The old per-row paragraph printed it three times.
    expect(screen.getAllByText("These pull the score down, never up.")).toHaveLength(1);
    expect(screen.queryByText(/so it added nothing/)).toBeNull();
  });

  it("folds a long group past six rows, without moving its total", async () => {
    const { userEvent } = await import("@testing-library/user-event");
    const user = userEvent.setup();
    show(detail(pushRows(12)));

    const pushed = groupOf("Pushed to remove");
    // Twelve equal rows over a 55 give seven 5s and five 4s. The six shown are the six
    // biggest; the six folded away are the six that mattered least, and add to 25.
    expect(visibleRows(pushed)).toBe(6);
    expect(within(pushed).getByText("+55")).toBeTruthy();

    const summary = within(pushed).getByText("6 more, adding +25");
    const disclosure = summary.closest("details") as HTMLDetailsElement;
    expect(disclosure.open).toBe(false);

    await user.click(summary);

    expect(disclosure.open).toBe(true);
    expect(pushed.querySelectorAll("li.sig-row")).toHaveLength(12);
    // Opening the disclosure reveals rows, never changes the arithmetic.
    expect(within(pushed).getByText("+55")).toBeTruthy();
  });

  it("names only the count when the folded rows added nothing", () => {
    // One row carries the whole score and eleven carry a rounding crumb each, so every
    // hidden row's share is a flat zero. "6 more, adding +0" would read as a bug.
    const rows = [
      signal({ id: "unwatched", contribution: 1000, weight: 1000, detail: "the reason", state: "adds" }),
      ...pushRows(11, 1),
    ];
    show(detail(rows));

    const pushed = groupOf("Pushed to remove");
    expect(within(pushed).getByText("6 more")).toBeTruthy();
    expect(screen.queryByText(/adding \+0/)).toBeNull();
  });

  it("never folds away an unread reason, however many there are", () => {
    const rows = Array.from({ length: 12 }, (_, i) =>
      signal({
        id: `unread ${String(i).padStart(2, "0")}`,
        weight: 20,
        detail: `could not read reason ${i}`,
        evaluated: false,
        state: "unreadable" as const,
      }),
    );
    const base = detail(rows);
    show(
      detail(rows, {
        score: 0,
        explanation: { ...base.explanation, score: 0, base_score: 0, keep_discount: 0 },
      }),
    );

    // More reasons Reaper could not read is more cause to look, so all twelve stay on
    // screen and there is nothing to expand.
    const unread = groupOf("Couldn't check");
    expect(visibleRows(unread)).toBe(12);
    expect(unread.querySelector("details")).toBeNull();
  });
});
