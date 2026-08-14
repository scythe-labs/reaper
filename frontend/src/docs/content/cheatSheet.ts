// SPDX-License-Identifier: AGPL-3.0-or-later

import { callout, type Doc, h2, p, table, ul } from "../blocks";

export const cheatSheet: Doc = {
  id: "cheat-sheet",
  group: "Policy",
  title: "Tuning cheat sheet",
  summary: "The defaults and the habits that keep you safe, at a glance.",
  body: [
    callout(
      "tip",
      "**Start cautious. Tighten one nudge at a time.** You can always remove more later. You can never un-delete.",
    ),

    h2("Signals, default points", "signals"),
    table(
      ["Reason to remove", "Movie", "TV"],
      [
        ["Gone unwatched", "70", "60"],
        ["Few watchers", "20", "15"],
        ["Old season", "not used", "15"],
        ["Low rating", "10", "10"],
        ["Size on disk", "off", "off"],
      ],
    ),
    p(
      "Points must total **100** or Save is blocked. Never weight size: it aims at your biggest, most-loved files.",
    ),

    h2("Protections", "protections"),
    table(
      ["Protection", "Default"],
      [
        ["Give every title time to be rewatched", "3 years (min 5 days)"],
        ["Keep what your users actually watch", "3 people, last year"],
        ["Keep well-rated titles", "IMDb 7.5, 1,000 votes"],
        ["Never touch something playing right now", "On"],
        ["Stop if the unwatched time can't be read", "On"],
        ["Keep titles most likely to be rewatched above a percentage", "Off by default"],
      ],
    ),
    p(
      'Your lists (Settings, Lists) protect through **keep rules** here: a list you add protects nothing until you give it a rule, and you pick whether it keeps every title outright or only leans that way. Shipped lists come with a keep-everything rule: "Titles you\'ve tagged" (the `reaper-keep` tag), and IMDb Top 250.',
    ),

    h2("Pace and limits, defaults", "pace"),
    table(
      ["Limit", "Default"],
      [
        ["Per run", "10 titles / 500 GB"],
        ["Per 30 days (rolling)", "100 titles / 2 TB"],
        // Named as the control is labeled in Policy, Pace and limits. What it actually does
        // is carried by the glossary and the deletion-safety page: it shows a title as
        // leaving, it does not hold it back.
        ["Grace period", "14 days (min 7)"],
        ["Unknown-size items", "0 (held back)"],
      ],
    ),
    // "stop", matching understandingPolicy's line about the same mechanism. "Abort" was the
    // only place in the product an operator met that word (U-15).
    p(
      'Caps stop the whole run when crossed. They never remove just the part that fits. Leave "Limit how much each run removes" on: switching it off drops both rows above. Unknown-size items are still held back, and the countdown still runs.',
    ),

    h2("Habits that keep you safe", "habits"),
    ul([
      "To remove more, lower the line. Don't switch off a protection.",
      "Pace, grace, and Leaving Soon first. Arm deletion last.",
      "Keep a vote floor on ratings. A high score from few votes is noise.",
      "Never weight size.",
    ]),
  ],
};
