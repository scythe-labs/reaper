// SPDX-License-Identifier: AGPL-3.0-or-later
// The scoring receipt. Three things are load-bearing here:
//
//   1. The shares ADD UP to the number beside them. An operator who cannot add the rows up
//      cannot check the score, which is the whole complaint this panel answers.
//   2. The four row states read differently on screen. "We could not look" is not "we
//      looked and it was fine", and a row with nothing recorded must never be drawn as if
//      it argued for keeping the file.
//   3. The rules that did not apply are tucked away, never dropped.
import { QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  api,
  type CandidateDetail,
  type GateOutcome,
  type Match,
  type SignalContribution,
} from "../api";
import { Announcer } from "../announce";
import { expectNoA11yViolations } from "../test/a11y";
import { DEFAULT_GENERAL, DEFAULT_PROFILE, seedSettings } from "../test/apiFixtures";
import { testQueryClient } from "../test/queryClient";
import { CSS } from "../test/stylesheet";
import { Synopsis, WhyPanel, allocateShares } from "./WhyPanel";

vi.mock("../api", () => ({
  api: {
    override: vi.fn(),
    clearOverride: vi.fn(),
    profile: vi.fn(),
    general: vi.fn(),
    forgetWatchEvidenceFor: vi.fn(),
  },
}));

// The panel reads two settings on its own, through hooks no test here names: the unmeasured
// allowance (["profile"], via useHoldsBackUnmeasured) and the default spare length
// (["general-settings"], via the Spare control). Rule 135.
beforeEach(() => {
  vi.mocked(api.profile).mockResolvedValue(DEFAULT_PROFILE);
  vi.mocked(api.general).mockResolvedValue(DEFAULT_GENERAL);
  vi.mocked(api.forgetWatchEvidenceFor).mockResolvedValue({ removed: true });
});

function signal(over: Partial<SignalContribution> & { id: string }): SignalContribution {
  return { contribution: 0, weight: 10, detail: "a reason", evaluated: true, ...over };
}

/** The six rows the operator was looking at: 75/75, 30/80, 20/20, 20/20, 10/60, 0/25.
 *  Total weight 280, total pressure 155, so the score is 55. */
const WORKED_ROWS: SignalContribution[] = [
  signal({
    id: "unwatched",
    contribution: 75,
    weight: 75,
    detail: "not watched in 5 years",
    state: "adds",
  }),
  signal({
    id: "season_rank",
    contribution: 30,
    weight: 80,
    detail: "a later season",
    state: "adds",
  }),
  signal({
    id: "few_watchers",
    contribution: 20,
    weight: 20,
    detail: "1 person watched it",
    state: "adds",
  }),
  signal({ id: "size", contribution: 20, weight: 20, detail: "takes 40 GiB", state: "adds" }),
  signal({ id: "low_rating", contribution: 10, weight: 60, detail: "rated 6.4", state: "adds" }),
  signal({ id: "my rule", weight: 25, detail: "not on the shelf", state: "not_applicable" }),
];

function detail(
  signals: SignalContribution[],
  over: Partial<CandidateDetail> = {},
): CandidateDetail {
  const d: CandidateDetail = {
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
      match_candidates: [],
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
  if (over.override_own === undefined) d.override_own = d.override;
  return d;
}

function show(item: CandidateDetail) {
  const client = seedSettings(testQueryClient());
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
  // This is the explanation an operator reads before overruling a verdict, and it is mostly
  // numbers sitting beside the reason that earned them. A share read out without its row proves
  // nothing, which is the one thing this panel exists to do.
  it("has no accessibility violations", async () => {
    const { container } = show(detail(WORKED_ROWS));
    await screen.findByRole("heading", { name: "Pushed to remove" });
    await expectNoA11yViolations(container);
  });

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
        signal({
          id: "unwatched",
          contribution: 40,
          weight: 40,
          detail: "not watched in 5 years",
          state: "adds",
        }),
        signal({
          id: "few_watchers",
          weight: 30,
          detail: "4 people watched it",
          state: "argues_keep",
        }),
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
    expect(screen.getByText("could not read its rating").closest("li")).toHaveClass(
      "sig-unreadable",
    );
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
        signal({
          id: "low_rating",
          weight: 20,
          detail: "could not read its rating",
          evaluated: false,
        }),
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
      signal({
        id: "unwatched",
        contribution: 1000,
        weight: 1000,
        detail: "the reason",
        state: "adds",
      }),
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

// The three protection blocks read at different volumes. What spared the file is the reason it
// lives, so it stays open. The checks that came back clear are the quiet "nothing to see here"
// block, so they rest folded behind one disclosure the operator opens only to read the list.
describe("the protection blocks", () => {
  const CHECKED = [
    { gate: "whitelist", detail: "Not on your keep list." },
    { gate: "streaming", detail: "Nobody is watching it right now." },
    { gate: "arr", detail: "Managed by Sonarr or Radarr." },
  ];

  it("rests the cleared list folded, and opens it on click", async () => {
    const user = userEvent.setup();
    const base = detail(WORKED_ROWS);
    show(
      detail(WORKED_ROWS, { explanation: { ...base.explanation, protections_checked: CHECKED } }),
    );

    expect(screen.getByRole("heading", { name: "Protections it cleared" })).toBeTruthy();
    const label = screen.getByText("Show all 3");
    const disclosure = label.closest("details") as HTMLDetailsElement;
    const summary = label.closest("summary") as HTMLElement;
    expect(disclosure.open).toBe(false);

    await user.click(summary);
    // Opening reveals the checks; it never renames the section.
    expect(disclosure.open).toBe(true);
    expect(screen.getByText("Nobody is watching it right now.")).toBeTruthy();
  });

  it("never folds what spared the file", () => {
    const base = detail(WORKED_ROWS);
    show(
      detail(WORKED_ROWS, {
        explanation: {
          ...base.explanation,
          protections_fired: [{ gate: "whitelist", detail: "On your keep list." }],
        },
      }),
    );

    const spared = screen.getByRole("heading", { name: "What spared it" });
    // A fired protection is the reason the file lives: its block is always open, never a fold.
    expect(spared.closest("section")?.querySelector("details")).toBeNull();
    expect(screen.getByText("On your keep list.")).toBeTruthy();
  });
});

// The Spare/Reap footer decides the SEASON, never the show above it. It must read the season's
// OWN decision (so a click always reverses something you can see) and, when a whole-show
// decision is what really keeps or reaps the season, say so -- clearing a season key cannot
// clear a show-level choice, and a dead "Spared" toggle was the bug this fixes.
// The headline reads the EFFECTIVE decision, not the frozen scan verdict (rule 61): a hand
// reap the engine honors reads as a removal, a reap it can't honor yet reads as held (never
// "Sanctuary"), a spare says the owner kept it, and "Sanctuary" is claimed only when a
// protection actually fired. This is the exact confusion the change fixes: a reaped item that
// the old panel labeled a protected Sanctuary.
describe("the verdict headline", () => {
  const fired = (gate: string, d = "a reason") => ({ gate, detail: d });

  it("labels an honored hand reap a removal, never a Sanctuary", () => {
    show(detail(WORKED_ROWS, { verdict: "protect", override: "reap", override_effective: true }));
    expect(screen.getByText("Reaped by hand")).toBeInTheDocument();
    expect(
      screen.getByText(/it will be removed\. Nothing is holding it back/i),
    ).toBeInTheDocument();
    expect(screen.queryByText("Sanctuary")).not.toBeInTheDocument();
    expect(screen.queryByText(/the score doesn't matter/i)).not.toBeInTheDocument();
  });

  it("labels a held reap 'Kept for now' and names why, in dashed red", () => {
    const { container } = show(
      detail(WORKED_ROWS, {
        verdict: "protect",
        override: "reap",
        override_effective: false,
        explanation: {
          ...detail(WORKED_ROWS).explanation,
          protections_fired: [fired("streaming_now", "being watched right now")],
        },
      }),
    );
    expect(screen.getByText("Kept for now")).toBeInTheDocument();
    expect(screen.getByText(/someone is watching it right now/i)).toBeInTheDocument();
    // Dashed red, never the solid "Sanctuary" green and never amber (rule 49).
    expect(container.querySelector(".verdict-held")).not.toBeNull();
    expect(container.querySelector(".verdict-protect")).toBeNull();
    expect(screen.queryByText("Sanctuary")).not.toBeInTheDocument();
  });

  it("names an unmanaged hold and an unidentifiable row distinctly", () => {
    show(
      detail(WORKED_ROWS, {
        override: "reap",
        override_effective: false,
        explanation: {
          ...detail(WORKED_ROWS).explanation,
          protections_fired: [fired("unmanaged")],
        },
      }),
    );
    expect(screen.getByText(/no app manages the file/i)).toBeInTheDocument();

    // Was a blocked `rating_floor`, which the engine no longer holds anything for -- so the
    // fixture pinned a held-reap state the backend cannot produce, and green-lit the wrong
    // sentence (rule 132: the fixture is the claim). The hold this row really has is the
    // Plex match.
    show(
      detail(WORKED_ROWS, {
        override: "reap",
        override_effective: false,
        explanation: {
          ...detail(WORKED_ROWS).explanation,
          match: { status: "unmatched", detail: null, rating_key: null },
        },
      }),
    );
    expect(screen.getByText(/couldn't find it in your Plex/i)).toBeInTheDocument();
  });

  it("blames the Plex match, not a blocked check, when both are present on a held reap", () => {
    // The ordering bug, pinned. An item Plex could not match has no rating key, so every
    // Plex-dependent gate blocks: `protections_unknown` is never empty for exactly the rows
    // the match is holding. `heldReapNote` used to test the blocked list first, so it was
    // wrong every single time it fired -- and it sent the operator after their watch-history
    // depth when the fix is a re-match. The card chip beside it has always said "couldn't be
    // found in Plex", so the panel contradicted the chip on one screen.
    show(
      detail(WORKED_ROWS, {
        override: "reap",
        override_effective: false,
        explanation: {
          ...detail(WORKED_ROWS).explanation,
          match: { status: "unmatched", detail: null, rating_key: null },
          protections_unknown: [
            fired("server_popularity", "could not check who watched it"),
            fired("min_dormancy", "could not check when it was last played"),
          ],
        },
      }),
    );
    expect(screen.getByText(/couldn't find it in your Plex/i)).toBeInTheDocument();
    expect(screen.queryByText(/a protection couldn't be checked/i)).not.toBeInTheDocument();
  });

  it("names an ambiguous match as its own thing, since the remedy differs", () => {
    show(
      detail(WORKED_ROWS, {
        override: "reap",
        override_effective: false,
        explanation: {
          ...detail(WORKED_ROWS).explanation,
          match: { status: "ambiguous", detail: null, rating_key: null },
          protections_unknown: [fired("server_popularity", "could not check who watched it")],
        },
      }),
    );
    // `KeptNotice` says "more than one thing in your Plex" too, which is right -- both
    // surfaces should speak. Assert the held-reap sentence specifically, by its own clause.
    expect(
      screen.getByText(/You asked to remove this, but it looks like more than one/i),
    ).toBeInTheDocument();
  });

  it("tells a disagreement apart from a duplicate, and names the app to go fix", () => {
    // The two used to share one status and one sentence, so an operator whose Plex holds
    // exactly one copy was sent hunting for a second. `media_type` picks the app: this
    // fixture is a season, so Sonarr.
    show(
      detail(WORKED_ROWS, {
        override: "reap",
        override_effective: false,
        explanation: {
          ...detail(WORKED_ROWS).explanation,
          match: { status: "conflicted", detail: null, rating_key: null },
          protections_unknown: [fired("server_popularity", "could not check who watched it")],
        },
      }),
    );
    // Both surfaces say it, so each is asserted by its own distinguishing tail rather than
    // the shared clause -- the ambiguous test above has the same shape.
    expect(screen.getByText(/so we couldn't tell which Plex entry it is/i)).toBeInTheDocument();
    expect(
      screen.getByText(/You asked to remove this, but Plex and Sonarr describe this show/i),
    ).toBeInTheDocument();
    // The claim that was false: no surface may say this library holds several copies.
    expect(screen.queryByText(/more than one thing/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/two different things/i)).not.toBeInTheDocument();
  });

  it("names Radarr on a movie, since the app to go fix differs by media type", () => {
    // rule 141: the season fixture above would pass against a hardcoded "Sonarr".
    show(
      detail(WORKED_ROWS, {
        media_type: "movie",
        explanation: {
          ...detail(WORKED_ROWS).explanation,
          match: { status: "conflicted", detail: null, rating_key: null },
        },
      }),
    );
    expect(
      screen.getByText(/Plex and Radarr describe this file differently, so we couldn't/i),
    ).toBeInTheDocument();
  });

  it("offers a way into each Plex row it could not choose between", () => {
    // Every other jump link on the panel is built from the item's own rating key, which is
    // null on exactly these rows -- so before this the panel named a problem in Plex and
    // gave no way to open it. Numbered, because Reaper knows nothing about these rows but
    // their keys, and each number must pair the same row's two apps.
    show(
      detail(WORKED_ROWS, {
        links: {
          ...detail(WORKED_ROWS).links,
          match_candidates: [
            { rating_key: 555, plex: "https://plex.example/555", tautulli: "https://taut/555" },
            { rating_key: 777, plex: "https://plex.example/777", tautulli: "https://taut/777" },
          ],
        },
        explanation: {
          ...detail(WORKED_ROWS).explanation,
          match: { status: "ambiguous", detail: null, rating_key: null },
        },
      }),
    );
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

  // The next two are branches of `MatchCandidates` itself, so they are driven here, where it
  // lives. `ShowPanel`'s pair above and below cover the other thing -- that the show panel
  // wires the same component to the same links -- which is a different claim (rule 72).
  it("says match, not matches, when there was only one row to choose between", () => {
    // The component already asserts this case exists: `numbered` drops the "1" from the pill
    // labels for it, while the lead sentence beside it read "1 possible matches" (#209).
    show(
      detail(WORKED_ROWS, {
        links: {
          ...detail(WORKED_ROWS).links,
          match_candidates: [{ rating_key: 555, plex: "https://plex.example/555", tautulli: null }],
        },
        explanation: {
          ...detail(WORKED_ROWS).explanation,
          match: { status: "ambiguous", detail: null, rating_key: null },
        },
      }),
    );
    expect(screen.getByText(/Reaper saw 1 possible match:/i)).toBeInTheDocument();
    expect(screen.queryByText(/possible matches/i)).not.toBeInTheDocument();
    // One candidate, so the pill is "Plex" -- "Plex 1" alone reads as a label for a row.
    expect(screen.getByRole("link", { name: /^Plex/ })).toHaveAttribute(
      "href",
      "https://plex.example/555",
    );
  });

  it("says nothing at all when it has no row it could offer to open", () => {
    // Neither app reachable at render time: no Plex server row (or no plex_web_url) and no
    // enabled Tautulli, for an item scanned when at least one was configured. `JumpPill`
    // renders null for a null href, so the lead used to stand over an empty row, ending on a
    // colon -- the dead end this component exists to close, reached one step later (#209).
    show(
      detail(WORKED_ROWS, {
        links: {
          ...detail(WORKED_ROWS).links,
          match_candidates: [
            { rating_key: 555, plex: null, tautulli: null },
            { rating_key: 777, plex: null, tautulli: null },
          ],
        },
        explanation: {
          ...detail(WORKED_ROWS).explanation,
          match: { status: "ambiguous", detail: null, rating_key: null },
        },
      }),
    );
    // The "kept to be safe" notice still explains why the file was kept; what goes is the
    // sentence promising rows to open.
    expect(
      screen.getByText(/This looks like more than one thing in your Plex/i),
    ).toBeInTheDocument();
    expect(screen.queryByText(/possible match/i)).not.toBeInTheDocument();
  });

  it("shows no candidate row on an item Reaper did tie to one Plex entry", () => {
    show(detail(WORKED_ROWS));
    expect(screen.queryByText(/possible matches/i)).not.toBeInTheDocument();
  });

  it("labels a hand spare as the owner's keep, not a Sanctuary", () => {
    show(detail(WORKED_ROWS, { verdict: "protect", override: "spare" }));
    expect(screen.getByText("Spared by hand")).toBeInTheDocument();
    expect(screen.getByText(/you chose to keep this/i)).toBeInTheDocument();
    expect(screen.queryByText("Sanctuary")).not.toBeInTheDocument();
  });

  it("never promises a re-judgment a longer show spare makes moot", () => {
    // A season spared 10 days inside a show spared forever. Reading the season's own clock,
    // the panel said "10 days left, then Reaper judges it again" -- and Reaper does no such
    // thing: that spare lapses and the show's goes on keeping the file. The sentence leads
    // with the outcome and still accounts for the operator's own decision.
    show(
      detail(WORKED_ROWS, {
        verdict: "condemn",
        override: "spare",
        override_own: "spare",
        show_override: "spare",
        spare_expires_at: new Date(Date.now() + 10 * 86_400_000).toISOString(),
        spare_covers_until: null,
      }),
    );
    expect(screen.getByText(/the whole show is spared, so this won't be removed/i)).toBeVisible();
    expect(screen.getByText(/your spare on this season has 10 days left/i)).toBeVisible();
    expect(screen.queryByText(/then Reaper judges it again/i)).not.toBeInTheDocument();
  });

  it("still says a spent spare hands the item back when nothing else covers it", () => {
    // The other side of the same branch: with no show spare, the two fields agree and the
    // plain expired sentence stands. Losing this would make every expired spare read as
    // covered, which is the fail-open direction for the sentence (though not for the file).
    const spent = new Date(Date.now() - 3 * 86_400_000).toISOString();
    show(
      detail(WORKED_ROWS, {
        verdict: "condemn",
        override: "spare",
        override_own: "spare",
        spare_expires_at: spent,
        spare_covers_until: spent,
      }),
    );
    expect(screen.getByText(/still kept until the next scan judges it again/i)).toBeVisible();
    expect(screen.queryByText(/the whole show is spared/i)).not.toBeInTheDocument();
  });

  it("claims Sanctuary only when a protection actually fired", () => {
    show(
      detail(WORKED_ROWS, {
        verdict: "protect",
        explanation: {
          ...detail(WORKED_ROWS).explanation,
          protections_fired: [fired("min_dormancy")],
        },
      }),
    );
    expect(screen.getByText("Sanctuary")).toBeInTheDocument();
    expect(screen.getByText(/the score doesn't matter/i)).toBeInTheDocument();
  });

  // A Sanctuary is only as absolute as the gate holding it, and this panel renders a working
  // Reap button below the sentence either way (`reapIsNoop` is false on a protect row). A hand
  // reap condemns past every cautious protection and is refused only by a FIRED member of
  // `verdict.STRUCTURAL_GATES` -- so "nothing can change that" was true of one of these two
  // rows and false of the other, and the panel said it about both.
  it("says a cautious protection can be overruled by hand", () => {
    show(
      detail(WORKED_ROWS, {
        verdict: "protect",
        explanation: {
          ...detail(WORKED_ROWS).explanation,
          protections_fired: [fired("min_dormancy")],
        },
      }),
    );
    expect(screen.getByText(/kept unless you Reap it yourself/i)).toBeInTheDocument();
    expect(screen.queryByText(/a Reap won't remove it/i)).not.toBeInTheDocument();
  });

  it("keeps the absolute wording for a structural stop, which a hand reap cannot overrule", () => {
    show(
      detail(WORKED_ROWS, {
        verdict: "protect",
        explanation: {
          ...detail(WORKED_ROWS).explanation,
          protections_fired: [fired("streaming_now")],
        },
      }),
    );
    expect(screen.getByText(/a Reap won't remove it/i)).toBeInTheDocument();
    expect(screen.queryByText(/unless you Reap it yourself/i)).not.toBeInTheDocument();
  });

  it("reads a stale protect-with-nothing-fired row as left-for-you, not protected", () => {
    // A held-reap row frozen as "protect" before this shipped, its override since cleared: no
    // protection fired, so it must not claim Sanctuary. A rescan resolves it to abstain.
    show(detail(WORKED_ROWS, { verdict: "protect", override: null }));
    expect(screen.queryByText("Sanctuary")).not.toBeInTheDocument();
    expect(screen.queryByText(/the score doesn't matter/i)).not.toBeInTheDocument();
  });

  // Verbatim from `services.season_pruning.PruneConflict.message`. The fixture here used to
  // be an invented sentence ("5 people watched this, more than a season your keep rule
  // protects") that the producer has never emitted, so the predicate under test could not be
  // discriminated by it: any wording rule at all passed. Copy real messages, or pin nothing
  // (rule 119).
  const SETTLEABLE_CONFLICT =
    "9 people watched Season 1, more than watched Season 3, which Reaper is keeping " +
    "because it is one of the newest seasons your rule keeps. Left for you to decide " +
    "instead of removing it.";
  const REFUSED_CONFLICT =
    "Reaper cannot tell whether Season 1 is watched more than Season 3, since your watch " +
    "history only goes back 12 months. Season 3 is kept because it is one of the newest " +
    "seasons your rule keeps. Left for you to decide instead of removing it.";

  // The message and the flag come from the same conflict, exactly as the producer emits them
  // (`season_scan.guard_result`): a settleable conflict carries `defers_to_owner: true`, a
  // refused one `false`. Passing them independently would let a test pin a pairing the backend
  // cannot produce.
  //
  // The third shape -- a row frozen before the field shipped -- reaches the panel as `null`,
  // NOT as an absent key. The STORED row carries no key; the RESPONSE always carries one,
  // because `GateOutcomeOut` declares `defers_to_owner: bool | None = None` and nothing on the
  // model or the route sets `exclude_none`, so `_explanation_out` serializes the missing key as
  // `"defers_to_owner": null`. Pass that, and pass it explicitly (rule 119): an omitted key is
  // the payload nothing produces, and a fixture built on one leaves the only pre-flag shape the
  // panel can ever be sent untested. `api.ts`'s `?` is defense against a shape the server does
  // not emit, so a test resting on it pins nothing.
  //
  // Required rather than defaulted, so every call site names the generation it means. Reading
  // an absent key as a *guess* in either direction is the whole defect (#86), and a defaulted
  // parameter is how a fixture stops saying which one it tests.
  const conflictDetail = (
    message: string,
    defersToOwner: boolean | null,
    others: GateOutcome[] = [],
  ) =>
    detail(WORKED_ROWS, {
      verdict: "abstain",
      explanation: {
        ...detail(WORKED_ROWS).explanation,
        protections_unknown: [
          ...others,
          { gate: "season_progression", detail: message, defers_to_owner: defersToOwner },
        ],
      },
    });

  it("tells a keep-rule conflict what it is and how to resolve it", () => {
    show(conflictDetail(SETTLEABLE_CONFLICT, true));
    expect(screen.getByText("Needs a look")).toBeInTheDocument();
    expect(screen.getByText(/This was watched more than a season your keep rule/i)).toBeVisible();
    expect(screen.getByText(/Spare it to keep it, or Reap it to remove it/i)).toBeInTheDocument();
  });

  it("offers a reap on a conflict Reaper could not settle, and the engine now honors it", () => {
    // The reap half of #86 is fixed, from the backend side: no blocked gate holds a hand
    // reap any more, so this promise is kept for every conflict shape. Pinned here because
    // it is the half that used to be a safety divergence, and a future change that makes a
    // block hold the reap again must fail a test rather than quietly re-break the panel.
    show(conflictDetail(REFUSED_CONFLICT, false));
    expect(screen.getByText(/Spare it to keep it, or Reap it to remove it/i)).toBeInTheDocument();
  });

  it("never asserts a comparison its own reason block denies (#86)", () => {
    // The copy half of #86, and what the retired wording test could not do: `REFUSED_CONFLICT`
    // is a non-"could not check" season_progression row, so any wording rule at all read it as
    // a made comparison and the headline asserted one -- while `LeftForYou` printed the
    // producer's "Reaper cannot tell whether ..." two blocks below, about a season that may
    // have no recorded plays at all. Only the typed flag separates them.
    show(conflictDetail(REFUSED_CONFLICT, false));
    expect(screen.getByText("Needs a look")).toBeInTheDocument();
    expect(screen.getByText(/Reaper couldn't check who watched these seasons/i)).toBeVisible();
    expect(screen.queryByText(/This was watched more than/i)).not.toBeInTheDocument();
    // The reason block still carries the producer's own account of it.
    expect(screen.getByText(/Reaper cannot tell whether Season 1/i)).toBeInTheDocument();
  });

  it("claims neither shape for a row frozen before the flag shipped", () => {
    // Nothing in such a row can tell a made comparison from a refused one, so the panel says
    // neither (rule 142's three-state). Reading it as `false` would be a guess in the other
    // direction; reading it as `true` is the bug this issue is.
    //
    // `null` is what the server actually sends for this row -- see `conflictDetail` above. A
    // fixture omitting the key instead would leave the branch every real pre-flag row takes
    // untested, and the natural refactor to `defersToOwner === undefined` would then pass green
    // while telling every one of them "Reaper couldn't check who watched these seasons".
    show(conflictDetail(SETTLEABLE_CONFLICT, null));
    expect(screen.getByText("Needs a look")).toBeInTheDocument();
    expect(screen.getByText(/Reaper couldn't settle this one on its own/i)).toBeVisible();
    expect(screen.queryByText(/This was watched more than/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/couldn't check who watched these seasons/i)).not.toBeInTheDocument();
  });

  it("reads the conflict's own flag, not whatever else could not be checked", () => {
    // A season on a short mirror routinely blocks several gates at once, so the conflict is
    // rarely alone in the list. The old predicate scanned it with `.some()` and could be
    // satisfied by any entry; the note must come from the season row itself.
    // Verbatim from `engine.gates.ServerPopularityGate`, whose blocked detail is
    // f"could not check who watched it in the last {window}: {shortfall}" -- the same rule 119
    // the conflict messages above are copied for. A short mirror blocks this gate on nearly
    // every row, which is exactly why the conflict is rarely alone in the list.
    show(
      conflictDetail(SETTLEABLE_CONFLICT, true, [
        fired(
          "server_popularity",
          "could not check who watched it in the last 12 months: your watch history only goes " +
            "back 3 months",
        ),
      ]),
    );
    expect(screen.getByText(/This was watched more than a season your keep rule/i)).toBeVisible();
  });

  it("keeps the plain 'Limbo' note for an ordinary abstain", () => {
    show(detail(WORKED_ROWS, { verdict: "abstain" }));
    expect(screen.getByText("Limbo")).toBeInTheDocument();
    expect(screen.getByText(/not confident enough to judge/i)).toBeInTheDocument();
  });
});

describe("the season footer's own-vs-show decision", () => {
  it("rests un-lit for a season kept only because the whole show is spared, and says why", () => {
    show(detail(WORKED_ROWS, { override: "spare", override_own: null, show_override: "spare" }));
    // Effective spare, but nothing of the season's own to undo: the button is not pressed.
    expect(screen.getByRole("button", { name: /Spare/ })).toHaveAttribute("aria-pressed", "false");
    expect(screen.getByText(/this season is kept/i)).toBeInTheDocument();
    expect(screen.getByText(/Undo it on the show/i)).toBeInTheDocument();
  });

  it("clears the season's OWN key when its lit button is pressed, even under a show spare", async () => {
    const user = userEvent.setup();
    // Own reap against a whole-show spare: the season's own decision wins and it will go.
    show(
      detail(WORKED_ROWS, {
        verdict: "abstain",
        override: "reap",
        override_own: "reap",
        show_override: "spare",
      }),
    );
    expect(screen.getByText(/will be removed/i)).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: /Reaping/ }));
    // The season key, never the show key -- the whole-show spare is untouched.
    expect(api.clearOverride).toHaveBeenCalledWith("sonarr:1:2:3");
  });
});

// One file listed several times in Plex (#260). `merged_rating_keys` was populated, crossed the
// wire, and the TS `Match` type never declared it, so no component could read it -- while the
// executor re-reads the same list, meaning every listing really is protected together and the
// operator was told none of it.
//
// The count is deliberately absent below two, and the two silent cases are different: the server
// sends `null` on an ordinary single-listing bind, and a record stored before the field shipped
// has no key at all. Both must draw nothing, because "Listed 1 time" on every item in the library
// is noise on a line that is scanned rather than read.
describe("the merged-listing count", () => {
  const withMatch = (match: Partial<Match>) =>
    detail(WORKED_ROWS, {
      explanation: {
        ...detail(WORKED_ROWS).explanation,
        match: { status: "matched", detail: null, rating_key: 900, ...match },
      },
    });

  it("says how many listings a merged bind covers", () => {
    show(withMatch({ merged_rating_keys: [900, 901, 902] }));
    const chip = screen.getByText(/Listed 3/);
    expect(chip).toBeInTheDocument();
    // The consequence rides on the title, the way `LibraryChip` carries its library name: the
    // number alone does not say that a watch on any listing counts for the file.
    expect(chip).toHaveAttribute("title", expect.stringMatching(/go together/i));
  });

  it("stays quiet on an ordinary single-listing bind", () => {
    show(withMatch({ merged_rating_keys: null }));
    expect(screen.queryByText(/^Listed /)).not.toBeInTheDocument();
  });

  it("stays quiet on a record stored before the field shipped", () => {
    // The key is absent entirely, not null -- which is the shape an older row really has, and
    // a different code path from the one above (rule 142's three-state).
    show(withMatch({}));
    expect(screen.queryByText(/^Listed /)).not.toBeInTheDocument();
  });

  it("stays quiet on a single merged key, which says nothing worth a chip", () => {
    show(withMatch({ merged_rating_keys: [900] }));
    expect(screen.queryByText(/^Listed /)).not.toBeInTheDocument();
  });
});

// The per-title escape from a hold nothing else on this screen can lift (#275). Reaper keeps the
// most watch evidence it has ever measured for a title, so plays that stop being readable hold it
// back on every scan -- and until this the only way out was Settings' whole-library Forget, which
// discards the record for every title at once.
//
// It renders on ONE of the field's three states. `true` is the positive claim that this row's
// recorded plays went unreadable. `false` is a reading the scan took and trusted. `null` is a row
// that cannot say either way -- a scan older than the field -- and it is what the server actually
// sends for one, since `Explanation` defaults it to `None` and nothing sets `exclude_none`. Both
// of the last two must show nothing: discarding a watch record on a guess is a guess in the
// direction that lets a file be deleted.
describe("the watch-record escape", () => {
  const BODY = /Plays Reaper recorded earlier are no longer readable/i;
  const PRESS = "Use what Reaper sees now";

  /** The panel for a row whose `watch_blind` is exactly this. Required rather than defaulted,
   *  so every call site names the state it means (the `conflictDetail` reasoning above). */
  const blindDetail = (watchBlind: boolean | null) =>
    detail(WORKED_ROWS, {
      explanation: { ...detail(WORKED_ROWS).explanation, watch_blind: watchBlind },
    });

  it("offers the escape on a title whose recorded plays went unreadable", () => {
    show(blindDetail(true));

    expect(screen.getByText(BODY)).toBeVisible();
    expect(screen.getByRole("button", { name: PRESS })).toBeInTheDocument();
    // The help binds to that one button and says what pressing it uses (rule 45).
    expect(screen.getByText("Use the plays visible today for this title only.")).toBeVisible();
  });

  it("offers nothing when the scan took a reading and it was honest", () => {
    show(blindDetail(false));

    expect(screen.queryByText(BODY)).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: PRESS })).not.toBeInTheDocument();
  });

  it("offers nothing on a row that cannot say either way", () => {
    // The case that matters, and the one a truthy test passes on by accident in the other
    // direction: every row frozen before the field shipped arrives here as an explicit `null`.
    // Offering to discard the record for one of them would act on nothing anybody measured.
    show(blindDetail(null));

    expect(screen.queryByText(BODY)).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: PRESS })).not.toBeInTheDocument();
  });

  it("offers nothing when the key never arrived at all", () => {
    // The shape the server does not emit, which `api.ts` types with `?` as defense only. Pinned
    // apart from the `null` case so a refactor to `=== undefined` cannot pass on one of them.
    show(detail(WORKED_ROWS));

    expect(screen.queryByText(BODY)).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: PRESS })).not.toBeInTheDocument();
  });

  it("forgets the record under this item's own key", async () => {
    const user = userEvent.setup();
    show(blindDetail(true));

    const press = screen.getByRole("button", { name: PRESS });
    // Rule 137: user-event reports a press on a disabled control as a success, so a test that
    // acts one turn early dispatches nothing and then fails on the state it never produced.
    await waitFor(() => expect(press).toBeEnabled());
    await user.click(press);

    await waitFor(() => expect(api.forgetWatchEvidenceFor).toHaveBeenCalledWith("sonarr:1:2:3"));
    // Rule 85: the confirmation appears only once the write has settled, and it REPLACES the
    // control. The panel reads the scan's frozen explanation, so the warning above it still
    // says "held" until a rescan re-judges the row -- leaving the button at its resting label
    // there would read as "nothing happened" and invite a second press on a record that is
    // already gone.
    expect(
      await screen.findByText("Reaper will judge this title on what it can see now."),
    ).toBeVisible();
    expect(screen.queryByRole("button", { name: PRESS })).not.toBeInTheDocument();
  });

  /** The same panel with the app's live regions above it, as `App.tsx` mounts them.
   *
   *  Not folded into `show()`: the announced sentence is deliberately the SAME string as the
   *  visible replacement (rule 144, one fact one wording), so mounting the regions for every
   *  test would make `findByText` ambiguous in the one above that reads it off the page. Here
   *  the region is read as a region, which is the idiom the other announcement tests use. */
  function showSpeaking(item: CandidateDetail) {
    return render(
      <QueryClientProvider client={seedSettings(testQueryClient())}>
        <Announcer />
        <WhyPanel item={item} onClose={() => {}} />
      </QueryClientProvider>,
    );
  }

  const spoken = () =>
    [...document.querySelectorAll('[aria-live="polite"]')].map((n) => n.textContent).join("");

  it("says out loud that the record is gone, since the only sign on screen is a button leaving", async () => {
    // #377. Success here is signalled by something DISAPPEARING, and it happens inside the
    // `standing` notice above -- correctly not a live region -- so nothing was said at all.
    // The button also carries `disabled={isPending}`, so the browser drops focus at the press
    // and the sentence that replaces it is ordinary page text nobody is pointed at.
    const user = userEvent.setup();
    showSpeaking(blindDetail(true));

    const press = screen.getByRole("button", { name: PRESS });
    await waitFor(() => expect(press).toBeEnabled());
    expect(spoken()).toBe("");

    await user.click(press);

    await waitFor(() =>
      expect(spoken()).toContain("Reaper will judge this title on what it can see now."),
    );
  });

  it("leaves the operator standing on the sentence, not on the document root (#393)", async () => {
    // The press removes the control it is on, so without a handoff focus falls to `<body>` and
    // the next Tab restarts above the whole application: above 900px `WhyShell` is not modal and
    // traps nothing, so there is no panel boundary to catch it. The replacement paragraph is the
    // only successor there is, which is why it carries `tabIndex={-1}`.
    const user = userEvent.setup();
    show(blindDetail(true));

    const press = screen.getByRole("button", { name: PRESS });
    await waitFor(() => expect(press).toBeEnabled());
    await user.click(press);

    const landed = await screen.findByText("Reaper will judge this title on what it can see now.");
    await waitFor(() => expect(landed).toHaveFocus());
    expect(document.body).not.toHaveFocus();
  });

  it("says a failed write failed, rather than swallowing it", async () => {
    vi.mocked(api.forgetWatchEvidenceFor).mockRejectedValue(new Error("no"));
    const user = userEvent.setup();
    show(blindDetail(true));

    const press = screen.getByRole("button", { name: PRESS });
    await waitFor(() => expect(press).toBeEnabled());
    await user.click(press);

    expect(await screen.findByText("That didn't save. Try again.")).toBeVisible();
    // The warning and its control stay put: the record is still there to discard.
    expect(screen.getByRole("button", { name: PRESS })).toBeEnabled();
  });
});

// The panel is a full-screen dialog under 900px, so it has to say what it is. Its name comes from
// its own <h2> rather than a second copy of the title in an aria-label (rule 144). One of these
// per panel: six surfaces render WhyShell, and the name is the one part of the contract the shell
// cannot supply for them. The fallbacks are included because they are two of the six -- and the
// loading branch has no heading at all, so its lead line carries the name instead.
describe("the why panel's accessible name", () => {
  it("names itself from the title it is explaining", () => {
    show(detail([]));
    expect(screen.getByRole("complementary", { name: /Example Show/ })).toBeInTheDocument();
  });
});

// Only one thing can lower coverage -- a reason Reaper could not read -- and those get their own
// group with their own note. So at full coverage the old clause announced the absence of a group
// that was already absent, which is what made an owner ask "100% of WHAT evidence" (#410). Neither
// branch had a test, and every fixture in the suite sits at 10,000, so the whole clause was
// unexercised in both directions.
describe("the coverage clause", () => {
  it("says nothing when every reason was readable", () => {
    show(detail(WORKED_ROWS, { coverage_bp: 10_000 }));

    expect(screen.getByText(/Reasons to believe nobody will watch it again/)).toBeVisible();
    expect(screen.queryByText(/of what it scores on/)).toBeNull();
    expect(screen.queryByText(/100%/)).toBeNull();
  });

  it("says how much it could read when something was missed", () => {
    // 76%, not a round number: a fixture equal to the full-coverage constant could not tell
    // a working branch from a missing one (rule 141).
    show(detail(WORKED_ROWS, { coverage_bp: 7_550 }));

    expect(screen.getByText(/Reaper could read 76% of what it scores on/)).toBeVisible();
  });
});

// A row states the line it was measured against, from the ramp the SCAN froze onto it. The
// panel must never fill this in from the live policy: the item was scored under the policy as
// it stood at scan time, and rule 113 refuses a reap across exactly that gap.
describe("what a signal row was measured against", () => {
  const RATED = signal({
    id: "low_rating",
    contribution: 0,
    weight: 10,
    detail: "IMDb 6.4",
    state: "argues_keep",
    floor: 0,
    saturate_at: 60,
  });

  it("states the ramp and what this row did against it", async () => {
    const user = userEvent.setup();
    show(detail([RATED]));

    const row = screen.getByRole("button", { name: /IMDb 6.4/ });
    expect(row).toHaveAttribute("aria-expanded", "false");
    await user.click(row);

    expect(
      screen.getByText(
        "Pays nothing at IMDb 6.0 or above, and all 10 points at IMDb 0.0. " + "This one added 0.",
      ),
    ).toBeVisible();
    expect(row).toHaveAttribute("aria-expanded", "true");
  });

  it("closes again on a second press, which is the whole interaction on a phone", async () => {
    const user = userEvent.setup();
    show(detail([RATED]));

    const row = screen.getByRole("button", { name: /IMDb 6.4/ });
    await user.click(row);
    await user.click(row);

    expect(row).toHaveAttribute("aria-expanded", "false");
  });

  it("says nothing for a row whose line the scan never recorded", () => {
    // A row frozen before the ramp shipped, and a yes/no rule of your own, both arrive as
    // null and both mean the same thing: there is no line to state. Inventing one would put
    // a ramp on a rule that provably has none.
    show(detail([signal({ id: "low_rating", detail: "IMDb 6.4", state: "argues_keep" })]));

    expect(screen.queryByRole("button", { name: /IMDb 6.4/ })).toBeNull();
    expect(screen.queryByText(/Pays nothing/)).toBeNull();
  });

  it("says nothing for a reason it could not read", () => {
    // There is a line, but nothing was compared to it. "This one added 0" would describe
    // arithmetic that never ran, on the one row state that must stay distinct from a zero.
    show(
      detail([
        signal({
          id: "low_rating",
          detail: "could not read the IMDb rating",
          evaluated: false,
          state: "unreadable",
          floor: 0,
          saturate_at: 60,
        }),
      ]),
    );

    expect(screen.queryByRole("button", { name: /could not read/ })).toBeNull();
  });
});

// Whether the synopsis needs a "more" is a question about how wide the panel is, and it used to
// be answered by counting characters -- right at the width that number was picked for, and wrong
// everywhere else. On a phone two lines hold about 120 characters against a test asking for 150,
// so a synopsis in between was cut with nothing on screen to open it (#407).
describe("the synopsis disclosure", () => {
  /** The panel's 0.88rem text at line-height 1.5, rounded to the integer the DOM reports. */
  const LINE = 21;
  const originals = new Map<string, PropertyDescriptor | undefined>();

  /** jsdom computes no layout, so every element answers 0 to both heights and a disclosure
   *  decided by measurement could never appear -- a test written against that would pass having
   *  proven nothing. Model what a clamp does instead: an element reports every line it holds,
   *  and a clamped one reports the two it shows. That difference is the whole of what `Synopsis`
   *  reads, so it is the boundary to stub rather than to inherit from the environment
   *  (rule 119). Restored after every test (rule 133). */
  function reportLines(lines: number) {
    for (const prop of ["scrollHeight", "clientHeight"] as const) {
      if (!originals.has(prop)) {
        originals.set(prop, Object.getOwnPropertyDescriptor(Element.prototype, prop));
      }
      Object.defineProperty(Element.prototype, prop, {
        configurable: true,
        get(this: Element) {
          const clamped = prop === "clientHeight" && this.classList.contains("clamp-2");
          return (clamped ? Math.min(lines, 2) : lines) * LINE;
        },
      });
    }
  }

  afterEach(() => {
    for (const [prop, descriptor] of originals) {
      if (descriptor) Object.defineProperty(Element.prototype, prop, descriptor);
      else delete (Element.prototype as unknown as Record<string, unknown>)[prop];
    }
    originals.clear();
  });

  it("offers the control for a synopsis the panel cut, however short the string", () => {
    const text = "a plain sentence. ".repeat(6).trim();
    // Under the 150 the old rule asked for, and over what two lines hold on a phone.
    expect(text.length).toBeLessThan(150);
    reportLines(3);

    render(<Synopsis text={text} />);

    expect(screen.getByRole("button", { name: "more" })).toBeVisible();
  });

  it("offers no control for a long synopsis the panel is showing whole", () => {
    const text = "a plain sentence. ".repeat(12).trim();
    // Past the 150 the old rule asked for, and inside the two lines. A control here opens
    // nothing, which reads as a broken control rather than as a full synopsis.
    expect(text.length).toBeGreaterThan(150);
    reportLines(2);

    render(<Synopsis text={text} />);

    expect(screen.queryByRole("button")).toBeNull();
  });

  it("keeps the control once the synopsis is open", async () => {
    const user = userEvent.setup();
    reportLines(4);
    render(<Synopsis text={"a plain sentence. ".repeat(8).trim()} />);

    const open = screen.getByRole("button", { name: "more" });
    expect(open).toHaveAttribute("aria-expanded", "false");
    await user.click(open);

    // An open synopsis is not clamped, so it measures as "nothing is hidden". A control that
    // re-read its own state would take away the only way to close what it just opened.
    const close = screen.getByRole("button", { name: "less" });
    expect(close).toHaveAttribute("aria-expanded", "true");

    await user.click(close);
    expect(screen.getByRole("button", { name: "more" })).toBeVisible();
  });

  it("gives the text its own line whether the synopsis is open or closed", async () => {
    const user = userEvent.setup();
    reportLines(3);
    const { container } = render(<Synopsis text={"a plain sentence. ".repeat(8).trim()} />);
    const span = container.querySelector("span");

    expect(span).toHaveClass("why-synopsis");
    await user.click(screen.getByRole("button", { name: "more" }));
    expect(span).toHaveClass("why-synopsis");
  });

  it("carries the declaration that class stands for", () => {
    // jsdom computes no layout, so the class above is one half of a contract spanning the
    // component and the stylesheet (rule 67). This is the other half, and it is the whole of
    // what the operator sees: without it the open state is an inline span and the control
    // lands after the last word, hundreds of pixels from where it had just been.
    expect(CSS).toMatch(/\.why-synopsis:not\(\.clamp-2\)\s*\{[^}]*display:\s*block/);
  });
});
