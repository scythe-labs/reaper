// SPDX-License-Identifier: AGPL-3.0-or-later
//
// The composer, proven against the real catalog. The backend suite asserts the same
// sentences through its own test-side twin (`tests/_reasons.py`), so the expectations
// here are what keep the two composers honest with each other: both must render these
// exact strings from the same `ui.json`.

import { describe, expect, it } from "vitest";
import i18next from "./i18n";
import {
  blockedParts,
  cardReason,
  composeError,
  composeIn,
  composeReason,
  dormantSpan,
} from "./why";

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

describe("the five media-typed why.cause pairs (movie/season merge)", () => {
  it("selects the movie or season wording off a fresh mediaType param", () => {
    expect(composeReason({ k: "cause.plex_unmatched", p: { mediaType: "movie" } })).toBe(
      "This title couldn't be found in Plex.",
    );
    expect(composeReason({ k: "cause.plex_unmatched", p: { mediaType: "season" } })).toBe(
      "This season couldn't be found in Plex.",
    );
    expect(composeReason({ k: "cause.plex_ambiguous", p: { mediaType: "season" } })).toBe(
      "This show looks like more than one thing in Plex.",
    );
    expect(composeReason({ k: "cause.radarr_plex_disagree", p: { mediaType: "movie" } })).toBe(
      "Plex and Radarr describe this file differently.",
    );
    expect(composeReason({ k: "cause.radarr_plex_disagree", p: { mediaType: "season" } })).toBe(
      "Plex and Sonarr describe this show differently.",
    );
    expect(composeReason({ k: "cause.no_added_at", p: { mediaType: "season" } })).toBe(
      "Plex didn't say when this season was added.",
    );
    expect(composeReason({ k: "cause.no_file_size", p: { mediaType: "season" } })).toBe(
      "Sonarr didn't report this season's size.",
    );
  });

  it("an old bare movie-side reason (no params at all, frozen before the merge) still renders words", () => {
    // Before this merge the movie side never carried a `mediaType` param -- ICU's `select`
    // leaves the raw template unparsed rather than falling to `other` when the variable is
    // entirely absent from params, so this pins the resolution-point default that keeps a
    // pre-merge row from printing broken syntax at the operator.
    expect(composeReason({ k: "cause.plex_unmatched" })).toBe(
      "This title couldn't be found in Plex.",
    );
    expect(composeReason({ k: "cause.no_file_size" })).toBe(
      "Radarr didn't report this file's size.",
    );
  });

  it("an old season-side id, retired by the merge, still renders the season wording", () => {
    // Every scan made before this merge that carried one of the five retired season-only
    // ids: the catalog has no entry for them any more, so why.ts's alias map is the only
    // thing standing between one of these rows and its raw id on screen.
    expect(composeReason({ k: "cause.plex_season_unmatched" })).toBe(
      "This season couldn't be found in Plex.",
    );
    expect(composeReason({ k: "cause.plex_show_ambiguous" })).toBe(
      "This show looks like more than one thing in Plex.",
    );
    expect(composeReason({ k: "cause.sonarr_plex_disagree" })).toBe(
      "Plex and Sonarr describe this show differently.",
    );
    expect(composeReason({ k: "cause.no_season_added_at" })).toBe(
      "Plex didn't say when this season was added.",
    );
    expect(composeReason({ k: "cause.no_season_size" })).toBe(
      "Sonarr didn't report this season's size.",
    );
  });

  it("a retired season id nested inside a blocked check still composes whole", () => {
    expect(
      composeReason({
        k: "blocked",
        p: { check: { k: "check.watch_history" }, cause: { k: "cause.plex_season_unmatched" } },
      }),
    ).toBe("could not check your watch history: This season couldn't be found in Plex.");
  });
});

describe("rewatch_thin (#906: merged with the why-panel's own rewatch-odds thin sentence)", () => {
  it("selects the movie or season wording off a fresh mediaType param", () => {
    expect(composeReason({ k: "rewatch_thin", p: { mediaType: "movie" } })).toBe(
      "Too few titles like this to say.",
    );
    expect(composeReason({ k: "rewatch_thin", p: { mediaType: "season" } })).toBe(
      "Too few shows like this to say.",
    );
  });

  it("an old bare reason (no params at all, frozen before the select shipped) still renders words", () => {
    // RewatchOddsGate wrote this bare, on both the movie and season lanes, before #906 --
    // proving the same missing-param behavior Task A's cause merge already guards against.
    expect(composeReason({ k: "rewatch_thin" })).toBe("Too few titles like this to say.");
  });
});

describe("composeIn", () => {
  // Fixture entries, added to i18next at test time rather than reading real catalog
  // content -- `warning` has no production keys yet (chip.text/chip.sentence do now, and
  // the two cases below read them for real). tests/_reasons.py's twin test uses the same
  // fixture message, so a passing pair here and there proves `namespace` walks the catalog
  // the same way this one does (rule 119).
  i18next.addResourceBundle(
    "en-US",
    "ui",
    {
      warning: { fixture_nested: "blocked because {cause}" },
      why: { cause: { fixture_cause: "the fixture reason fired" } },
    },
    true,
    true,
  );

  it("pluralizes a chip.text entry, off the real catalog", () => {
    expect(
      composeIn("chip.text", { k: "kept.popularity", p: { count: 1, window_days_window: "year" } }),
    ).toBe("Kept, 1 person watched it in the last year");
    expect(
      composeIn("chip.text", { k: "kept.popularity", p: { count: 3, window_days_window: "year" } }),
    ).toBe("Kept, 3 people watched it in the last year");
  });

  it("nests a chip.sentence into the override frame the same way t() would", () => {
    const why = composeIn("chip.sentence", { k: "kept.streaming_now" });
    expect(why).toBe("It's playing right now.");
    expect(i18next.t("shell.statusChip.reapRequestedKept", { why })).toBe(
      "Reap requested, kept for now. It's playing right now.",
    );
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

  // Phase 8b: the browser's half of a coded API refusal, off the real error.* catalog.
  it("pluralizes an error.* entry's count param", () => {
    expect(composeIn("error", { k: "password.too_short", p: { min_length: 1 } })).toBe(
      "Use at least 1 character.",
    );
    expect(composeIn("error", { k: "password.too_short", p: { min_length: 8 } })).toBe(
      "Use at least 8 characters.",
    );
  });

  it("derives an error.* entry's field_label through why.field.*, same as why.ts", () => {
    expect(
      composeIn("error", { k: "policy.field_needs_value", p: { field: "recent_watchers" } }),
    ).toBe('"People who watched it recently" needs a value.');
    // Unrecognized field: falls back to the raw key, same posture as why.ts.
    expect(
      composeIn("error", { k: "policy.field_needs_value", p: { field: "not_a_real_field" } }),
    ).toBe('"not_a_real_field" needs a value.');
  });

  it("composes a nested error.* param through the error namespace, not why", () => {
    // services.modal.mapError's own `{error}` param is exactly this shape: a non-error
    // outer key (ServiceModal.tsx's own namespace) carrying an IntegrationError/PlexError's
    // own code as a nested reason. `why.error.instance.auth_refused` is not a catalog entry
    // and never should be -- the sentence lives at `error.instance.auth_refused`, the same
    // entry the backend's own `english()` reads for the identical nested Reason.
    expect(
      composeIn("services.modal", {
        k: "mapError",
        p: { error: { k: "error.instance.auth_refused", p: { service: "Radarr" } } },
      }),
    ).toBe(
      "Reaper reached this service but couldn't read what to map. Radarr refused the API " +
        "key. Copy it again from its own settings.",
    );
  });
});

describe("composeError", () => {
  it("strips the wire code's leading 'error.' before composing, so the namespace isn't doubled", () => {
    expect(
      composeError({ k: "error.policy.field_needs_value", p: { field: "recent_watchers" } }),
    ).toBe('"People who watched it recently" needs a value.');
  });

  it("falls back to the bare code when this build's catalog has no entry for it", () => {
    expect(composeError({ k: "error.not_a_real.code" })).toBe("not_a_real.code");
  });

  it("still honors the legacy shape composeIn already handles", () => {
    expect(composeError({ k: "legacy", p: { text: "an old stored sentence" } })).toBe(
      "an old stored sentence",
    );
  });
});

describe("the card helpers", () => {
  it("composes the reason from its key: a fresh id, or a legacy sentence verbatim", () => {
    expect(cardReason({ reason_key: { k: "kept_safe.unmatched" } })).toBe(
      "Kept to be safe: it couldn't be found in Plex.",
    );
    expect(cardReason({ reason_key: { k: "legacy", p: { text: "an old stored line" } } })).toBe(
      "an old stored line",
    );
    expect(cardReason({ reason_key: null })).toBeNull();
  });

  it("composes the dormancy span from a fresh row's raw days; a legacy row shows no pill", () => {
    expect(dormantSpan({ dormant_days: 2059 })).toBe("5 years, 7 months");
    expect(dormantSpan({ dormant_days: null })).toBeNull();
  });
});
