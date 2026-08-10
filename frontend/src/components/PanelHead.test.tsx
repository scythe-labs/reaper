// SPDX-License-Identifier: AGPL-3.0-or-later
// The head the item panel and the show panel share. It was written twice and the two copies had
// drifted: the item panel's title carried the outbound arrow and the show panel's did not, and the
// two spelled the jump pills in different orders (Sonarr first on the show, last on the item). So
// what this file pins is not that each panel is right on its own, which is how both copies stayed
// wrong, but that the two AGREE -- every assertion below reads both panels and compares them.
import { within } from "@testing-library/react";
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { api, type CandidateDetail, type Group, type Links } from "../api";
import { expectNoA11yViolations } from "../test/a11y";
import { DEFAULT_GENERAL, DEFAULT_PROFILE, seedSettings } from "../test/apiFixtures";
import { testQueryClient } from "../test/queryClient";
import { renderWithProviders } from "../test/renderWithProviders";
import { ShowPanel } from "./ShowPanel";
import { WhyPanel } from "./WhyPanel";

vi.mock("../api", () => ({
  api: { override: vi.fn(), clearOverride: vi.fn(), profile: vi.fn(), general: vi.fn() },
}));

// Both panels read settings through hooks no assertion here names: the unmeasured allowance
// (["profile"]) and the Spare control's default length (["general-settings"]). Rule 135.
beforeEach(() => {
  vi.mocked(api.profile).mockResolvedValue(DEFAULT_PROFILE);
  vi.mocked(api.general).mockResolvedValue(DEFAULT_GENERAL);
});

/** The order the head declares, written from the decision rather than read back off the
 *  component: what was played, who asked for it, then the app to go change it. At most one of
 *  radarr/sonarr is ever set on a real row, so a real panel's row always ends on the app that
 *  manages the file (rule 119: an expectation from the spec, not a transcription). */
const PILL_ORDER = ["Tautulli", "Seerr", "Radarr", "Sonarr"];

/** Every link set at once. No route sends this -- `LinksOut` sets at most one of radarr/sonarr --
 *  and that is the point: it is the only shape in which the WHOLE declared order is observable on
 *  one render, so the two panels can be compared over all four rather than over the three each
 *  would show on its own. The realistic single-manager shapes are driven below it. */
const ALL_LINKS: Links = {
  plex: "https://plex.example/web/1",
  tautulli: "https://tautulli.example/1",
  seerr: "https://seerr.example/1",
  radarr: "https://radarr.example/1",
  sonarr: "https://sonarr.example/1",
  imdb: null,
  tmdb: null,
  rotten_tomatoes: null,
  trakt: null,
  match_candidates: [],
};

function item(links: Links): CandidateDetail {
  return {
    id: 1,
    media_key: "sonarr:1:2:3",
    title: "Example Title",
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
    video_resolution: null,
    library: null,
    dormant_for: null,
    reason: null,
    override: null,
    override_own: null,
    show_override: null,
    override_effective: null,
    spare_expires_at: null,
    spare_covers_until: null,
    show_spare_expires_at: null,
    chip: null,
    season_number: 3,
    show_status: null,
    content_rating: null,
    runtime_minutes: null,
    genres: [],
    ratings: null,
    links,
    explanation: {
      score: 55,
      base_score: 55,
      keep_discount: 0,
      threshold: 70,
      coverage: 10_000,
      signals: [],
      protections_fired: [],
      protections_checked: [],
      protections_unknown: [],
    },
  };
}

function show(links: Links): Group {
  return {
    group_key: "sonarr:show:1",
    title: "Example Title",
    year: 2012,
    poster_url: null,
    summary: null,
    size_bytes: 1024 ** 3,
    unknown_size_seasons: 0,
    reason: null,
    library: null,
    chip: null,
    show_override: null,
    show_spare_expires_at: null,
    links,
    show_status: null,
    seasons: [],
  };
}

/** Each panel's head. Every query below is scoped to the element this returns, so the two panels
 *  can be rendered in one test without either one answering for the other. */
function headOf(which: "item" | "show", links: Links): HTMLElement {
  const client = seedSettings(testQueryClient());
  const node =
    which === "item" ? (
      <WhyPanel item={item(links)} onClose={() => {}} />
    ) : (
      <ShowPanel group={show(links)} onOpenSeason={() => {}} onClose={() => {}} />
    );
  const { container } = renderWithProviders(node, { client });
  const heads = container.querySelectorAll(".why-head");
  // One head per panel, asserted before anything reads it: a second would leave every comparison
  // below ambiguous about which one it measured (rule 145).
  expect(heads, `${which}: expected exactly one panel head`).toHaveLength(1);
  return heads[0] as HTMLElement;
}

const pillsIn = (head: HTMLElement): string[] =>
  [...head.querySelectorAll(".jump-pill")].map((a) =>
    (a.textContent ?? "").replace("↗", "").trim(),
  );

describe("the panel head the two title panels share", () => {
  it("puts the jump pills in the same order on both", () => {
    // The old show panel led with Sonarr, so this fails on the tree before the extraction.
    expect(pillsIn(headOf("item", ALL_LINKS))).toEqual(PILL_ORDER);
    expect(pillsIn(headOf("show", ALL_LINKS))).toEqual(PILL_ORDER);
  });

  it("ends both rows on the app that manages the file, on the links a route really sends", () => {
    // A movie reaches Radarr and a show reaches Sonarr, so a fixture pinning one manager cannot
    // tell a shared order from a hardcoded one (rule 141): sweep both.
    expect(pillsIn(headOf("item", { ...ALL_LINKS, sonarr: null }))).toEqual([
      "Tautulli",
      "Seerr",
      "Radarr",
    ]);
    expect(pillsIn(headOf("show", { ...ALL_LINKS, radarr: null }))).toEqual([
      "Tautulli",
      "Seerr",
      "Sonarr",
    ]);
  });

  it("marks the linked title as going somewhere, on both", () => {
    for (const which of ["item", "show"] as const) {
      const link = within(headOf(which, ALL_LINKS)).getByRole("link", { name: /Example Title/ });
      expect(link).toHaveAttribute("href", ALL_LINKS.plex);
      const arrow = link.querySelector(".title-ext");
      expect(arrow, `${which}: the title link carries no outbound mark`).not.toBeNull();
      // Decoration inside a control's accessible name, so it is not part of that name.
      expect(arrow).toHaveAttribute("aria-hidden", "true");
    }
  });

  it("has no accessibility violations in either panel", async () => {
    // The state neither panel's own suite drives: both build their heads from links that are all
    // null, so the linked title, the mark inside its name and the four pills are audited here or
    // nowhere.
    for (const which of ["item", "show"] as const) {
      await expectNoA11yViolations(headOf(which, ALL_LINKS));
    }
  });

  it("leaves a title it could not match as plain text, on both", () => {
    for (const which of ["item", "show"] as const) {
      const head = headOf(which, { ...ALL_LINKS, plex: null });
      expect(within(head).queryByRole("link", { name: /Example Title/ })).toBeNull();
      expect(head.querySelector(".title-ext")).toBeNull();
      expect(within(head).getByRole("heading")).toHaveTextContent("Example Title 2012");
    }
  });
});

// The assertions above compare two renders, so they hold just as well if someone writes the head
// out a second time and happens to spell it the same way. That is how these two drifted in the
// first place, and this is what says the head is written once.
//
// It reads source text, so it is bounded by what a substring match can see (rule 147): it accepts
// the class and label tokens exactly as the tree spells them today, one per line, and it would
// not see either name assembled from a variable or a template literal. What it does hold is the
// shape that actually recurs here, a head pasted into a panel.
describe("one declaration, not two", () => {
  const HERE = dirname(fileURLToPath(import.meta.url));
  const sourceOf = (name: string) => readFileSync(join(HERE, name), "utf8");

  it("renders both heads from the shared component", () => {
    for (const file of ["WhyPanel.tsx", "ShowPanel.tsx"]) {
      expect(sourceOf(file), `${file} does not render <PanelHead`).toContain("<PanelHead");
    }
  });

  it("writes the arrow and the pill row in the shared component alone", () => {
    // Every token is the head's own markup: its container class, the title link and its outbound
    // mark, and the pill each order position renders. A panel spelling one of these itself has a
    // second head, whatever that head happens to look like today. Pill LABELS are deliberately
    // not in this list: "Sonarr" is an ordinary word in this panel's prose, so banning it would
    // fail on a comment about something else.
    for (const token of ["why-head", "title-link", "title-ext", "JumpPill"]) {
      expect(
        sourceOf("ShowPanel.tsx"),
        `ShowPanel.tsx spells "${token}" itself instead of taking it from PanelHead`,
      ).not.toContain(token);
    }
  });
});
