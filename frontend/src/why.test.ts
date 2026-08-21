// SPDX-License-Identifier: AGPL-3.0-or-later
//
// The composer, proven against the real catalog. The backend suite asserts the same
// sentences through its own test-side twin (`tests/_reasons.py`), so the expectations
// here are what keep the two composers honest with each other: both must render these
// exact strings from the same `ui.json`.

import { describe, expect, it } from "vitest";
import i18next from "./i18n";
import { blockedParts, cardReason, composeIn, composeReason, dormantSpan } from "./why";

describe("composeReason", () => {
  it("renders the worked example the repo quotes", () => {
    // The same sentence README.md and DECISIONS.md quote, off the same catalog entry
    // (tests/test_repo_hygiene.py derives it through the backend twin).
    expect(composeReason({ k: "dormancy_past_floor", p: { days: 2059, floor_days: 1095 } })).toBe(
      "Unwatched for 5 years, 7 months, past the 3 years Reaper waits.",
    );
  });

  it("pluralizes counts through the catalog's ICU, not the caller", () => {
    expect(composeReason({ k: "popularity_watched", p: { count: 1, window_days: 365 } })).toBe(
      "watched here: 1 person in the last year",
    );
    expect(composeReason({ k: "popularity_watched", p: { count: 3, window_days: 90 } })).toBe(
      "watched here: 3 people in the last 3 months",
    );
  });

  it("orders a season's place with the selectordinal", () => {
    expect(composeReason({ k: "signal_season_rank", p: { rank: 1 } })).toBe(
      "the newest season on disk",
    );
    expect(composeReason({ k: "signal_season_rank", p: { rank: 2 } })).toBe(
      "the second-newest season on disk",
    );
    expect(composeReason({ k: "signal_season_rank", p: { rank: 8 } })).toBe(
      "the 8th-newest season on disk",
    );
  });

  it("composes a blocked check whole, and splits it for the left-for-you box", () => {
    const key = {
      k: "blocked",
      p: {
        check: { k: "check.watch_history" },
        cause: { k: "cause.plex_unmatched" },
      },
    };
    expect(composeReason(key)).toBe(
      "could not check your watch history: This title couldn't be found in Plex.",
    );
    expect(blockedParts(key)).toEqual({
      check: "your watch history",
      cause: "This title couldn't be found in Plex.",
    });
  });

  it("joins the rating gate's clause list", () => {
    expect(
      composeReason({
        k: "rating_cleared",
        p: {
          clauses: [
            { k: "rating_value_votes", p: { source: "imdb", value: 8.2, votes: 120000 } },
            { k: "rating_value_pct", p: { source: "rotten_tomatoes_critic", pct: 84 } },
          ],
        },
      }),
    ).toBe("well rated: 8.2 on IMDb from 120,000 votes; Rotten Tomatoes critics 84%");
  });

  it("nests the conflict's because clause and carries a legacy shortfall verbatim", () => {
    expect(
      composeReason({
        k: "conflict.shortfall",
        p: {
          pruned_season: 3,
          kept_season: 1,
          because: { k: "because.keep_last" },
          cause: { k: "legacy", p: { text: "your watch history only goes back 12 months" } },
        },
      }),
    ).toBe(
      "Reaper cannot tell whether Season 3 is watched more than Season 1, since your watch " +
        "history only goes back 12 months. Season 1 is kept because it is one of the newest " +
        "seasons your rule keeps.",
    );
  });

  it("renders a legacy row's stored prose untouched", () => {
    expect(composeReason({ k: "legacy", p: { text: "you spared this by hand" } })).toBe(
      "you spared this by hand",
    );
  });

  it("degrades an unknown id to what identifies it, never a blank row", () => {
    // A stored legacy sentence riding in a cause slot keeps reading as itself.
    expect(composeReason({ k: "cause.the source is down" })).toBe("the source is down");
    // A future id this build has no entry for still names itself.
    expect(composeReason({ k: "sturdier_reason" })).toBe("sturdier_reason");
  });
});

describe("composeIn", () => {
  // Fixture entries, added to i18next at test time rather than reading real catalog
  // content -- chip.text and warning have no production keys yet (Phase 1 only
  // generalizes the composer). tests/_reasons.py's twin test uses the same two
  // messages, so a passing pair here and there proves `text`'s namespace argument
  // walks the catalog the same way this one does (rule 119).
  i18next.addResourceBundle(
    "en-US",
    "ui",
    {
      chip: { text: { fixture_count: "{n, plural, one {# stray file} other {# stray files}}" } },
      warning: { fixture_nested: "blocked because {cause}" },
      why: { cause: { fixture_cause: "the fixture reason fired" } },
    },
    true,
    true,
  );

  it("composes a chip.text entry under its own namespace", () => {
    expect(composeIn("chip.text", { k: "fixture_count", p: { n: 3 } })).toBe("3 stray files");
  });

  it("composes a warning entry, whose nested reason still resolves under why", () => {
    expect(
      composeIn("warning", {
        k: "fixture_nested",
        p: { cause: { k: "cause.fixture_cause" } },
      }),
    ).toBe("blocked because the fixture reason fired");
  });

  it('composeReason is composeIn under "why"', () => {
    const key = { k: "dormancy_past_floor", p: { days: 2059, floor_days: 1095 } };
    expect(composeIn("why", key)).toBe(composeReason(key));
  });
});

describe("the card helpers", () => {
  it("prefers the typed key and falls back to legacy prose", () => {
    expect(cardReason({ reason: null, reason_key: { k: "kept_safe.unmatched" } })).toBe(
      "Kept to be safe: it couldn't be found in Plex.",
    );
    expect(cardReason({ reason: "an old stored line", reason_key: null })).toBe(
      "an old stored line",
    );
    expect(cardReason({ reason: null, reason_key: null })).toBeNull();
  });

  it("composes the dormancy span from the raw days, or serves the baked legacy span", () => {
    expect(dormantSpan({ dormant_for: null, dormant_days: 2059 })).toBe("5 years, 7 months");
    expect(dormantSpan({ dormant_for: "3 years", dormant_days: null })).toBe("3 years");
    expect(dormantSpan({ dormant_for: null, dormant_days: null })).toBeNull();
  });
});
