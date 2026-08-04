// SPDX-License-Identifier: AGPL-3.0-or-later
//
// The full policy guide. Every number here is a shipped default from engine/policy.py and
// the frontend presets (PolicyEditor.tsx). When a default changes, change it here too.

import { callout, defs, type Doc, h2, h3, p, steps, table, ul } from "../blocks";

export const understandingPolicy: Doc = {
  id: "understanding-policy",
  group: "Policy",
  title: "Understanding policy",
  summary: "Your policy is the rulebook Reaper follows when it decides what to remove.",
  body: [
    p(
      'A policy is how you tell Reaper what "nobody watches this" means for your server. It gathers reasons a title looks expendable, adds them into a single score, and flags anything that crosses your line. A whole separate set of protections can keep a title no matter how it scored.',
    ),
    callout(
      "tip",
      "**Start cautious, then tighten one nudge at a time.** You can always remove more later, and it is reversible up to the moment of deletion.",
    ),

    h2("The mental model", "mental-model"),
    p(
      "**The flag threshold is your line.** Every title gets a score from 0 to 100. At or above your line, it becomes a candidate for removal. Below it, Reaper leaves it alone. A higher line means Reaper has to be more sure.",
    ),
    p(
      "**Signals are soft pressure that adds up.** How long it has gone unwatched, how few people watch it, how low it is rated. Each pushes the score up by its weight. In practice, how long it has gone unwatched does almost all the work.",
    ),
    p(
      "**Protections are hard lines that always win.** They sit in their own lane and can only ever keep a file, never remove one. If any single protection fires, the title stays, whatever it scored.",
    ),
    p(
      "**Caps stop a run; grace is the heads-up.** Caps limit how much any one run, and any rolling 30 days, can remove, and a cap stops the whole run when it is crossed. Grace shows every flagged title as leaving for a set number of days. Watching a title or sparing it keeps it, and nothing goes until you run a reap yourself.",
    ),
    p(
      "**Missing information can only protect, never delete.** The score is pressure that adds up from zero. Anything Reaper cannot read, an outage, a stale ratings file, a title it cannot match, adds no pressure.",
    ),

    h2("The recommended workflow", "workflow"),
    p("Follow it in order the first time."),
    steps([
      {
        title: "Connect your watch history first.",
        // "Reads from your history source" was the same overstatement one step earlier:
        // the history source supplies the last play when there is one, and it also sets
        // how far back Reaper is willing to count. Neither is the whole answer for a title
        // nobody has ever played. See `engine/dormancy.py`.
        text: "Every score starts from how long a title has sat untouched, and your history source is what tells Reaper when it was last played and how far back it can look. Connect it and let one scan finish against real data before you tune anything.",
      },
      {
        title: "Start on the Cautious footing.",
        text: "Pick Cautious as your starting point. It sets a high line (82), small runs (5 titles or 250 GB), and a long grace window (30 days). Do not hand-edit the points yet.",
      },
      {
        title: "Leave every protection on.",
        text: "Above all, leave the time-to-be-rewatched line at its 3-year default. It is a hard line, so nothing can outvote it.",
      },
      {
        title: "Run a scan and let it finish.",
        // "incomplete" is the word the app itself shows (ScanBar, App.tsx). "degraded" is
        // the internal field name, which rules 21 and 25 both bar from operator copy (U-12).
        text: 'A scan freezes all the evidence, then scores, so a brief timeout can never flip a title’s fate mid-run. If it comes back "incomplete," a source failed and Reaper marked the run unusable on purpose. Fix the source and scan again.',
      },
      {
        title: 'Read a few "why" panels before touching anything.',
        text: "Open several flagged titles and read why each was flagged, and which protections were checked and did not fire. Notice how much of the pressure is just time unwatched.",
      },
      {
        title: "Tune with the simulator, one nudge at a time.",
        text: "Move the line down a step and watch the count, the space reclaimed, and the named titles change live. Tighten gradually over several scans.",
      },
      {
        title: "Set pace and grace, then arm deletion last.",
        text: 'Confirm your caps and grace, and turn on the Leaving Soon shelf so your users can rescue anything about to go. To see the shelf before you arm anything, also turn on "Update while read-only" in Settings, Plex. Only then arm deletion, which is password-gated and separate from all tuning.',
      },
    ]),

    h2("What's in a policy", "in-a-policy"),
    // "which starts by itself" is the same fact as `APPLIES_ON_NEXT_SCAN`
    // (components/PolicySimulator.tsx), the sentence the savebar and the simulator both show.
    // Kept as its own wording because this paragraph names which controls sit in that half,
    // and left saying only "take effect on the next scan" it read as a chore the operator has
    // to remember to start (rule 144).
    p(
      "A policy has two halves. The rules that change what Reaper decides (the line, signals, protections) take effect on the next scan, which starts by itself when you save. The limits on how much one run may remove, and how long a title shows as leaving (caps and grace), take effect immediately. Movies and TV are two separate policies, tuned on their own.",
    ),

    h3("Your starting point", "starting-point"),
    p(
      "Pick a starting point, which stages a line, the standard mix of points, and a set of caps into your draft. Review it, then save. The shipped default is the same as Balanced.",
    ),
    table(
      ["Setting", "Cautious", "Balanced (default)", "Aggressive"],
      [
        ["Flag threshold", "82", "70", "58"],
        ["Most titles per run", "5", "10", "25"],
        ["Most disk per run", "250 GB", "500 GB", "1 TB"],
        ["Most titles per 30 days", "50", "100", "150"],
        ["Most disk per 30 days", "1 TB", "2 TB", "4 TB"],
        ["Grace period", "30 days", "14 days", "7 days (minimum)"],
        ["Protections", "All on", "All on", "All on"],
      ],
      2,
    ),
    p(
      "A starting point only sets the line, the point mix, and those caps. It never changes your protections, keep rules, rating bars, or TV season rules.",
    ),

    h3("The flag threshold", "threshold"),
    p(
      "The threshold is a confidence line. A title is flagged only when its score reaches it, so a higher line means Reaper flags fewer titles and has to be more sure. Move it down one step at a time in the simulator and watch what appears.",
    ),

    h3("Signals: soft pressure", "signals"),
    p(
      "Each signal pushes the score up by up to its number of points. The default mix differs for movies and TV.",
    ),
    // The "What it adds" column is the fourth copy of a sentence the app now states three
    // other ways: the two bound boxes on the signal card, the strip drawn under them, and the
    // why-panel row that reports what a title scored. Generating those three and leaving this
    // one is exactly the trap rule 144 describes, and it fails in the reassuring direction: a
    // reader is told a signal is worth 10 points with nothing saying it adds none of them
    // above IMDb 6.0. `tests/test_repo_hygiene.py` holds these figures against the shipped
    // policies by name, because a comment asking the next author to remember does nothing.
    table(
      ["Signal", "What it means", "What it adds", "Movie points", "TV points"],
      [
        // Not "time since anyone last played it": `engine/dormancy.py` measures from the
        // last play, or from the day the file arrived when there has never been one, or
        // from the start of your history when that is later. The recipe further down this
        // page sends operators here for never-played requests, which is the second of those.
        [
          "How long it's gone unwatched",
          "How long it has sat untouched, counted from the last play or from the day it arrived",
          "Nothing until 1 year, all of it at 5 years",
          "70",
          "60",
        ],
        [
          "How few people watch it",
          "Fewer recent viewers means more pressure",
          "Nothing at 3 viewers or more, all of it at 0",
          "20",
          "15",
        ],
        [
          "How old a season is",
          "Older seasons carry more pressure (TV only)",
          "A little from the newest season on, all of it at the 6th-newest",
          "not used",
          "15",
        ],
        [
          "How low it's rated",
          "Lower ratings add a little pressure",
          "Nothing at IMDb 6.0 or above, all of it at IMDb 0.0",
          "10",
          "10",
        ],
        [
          "How big it is on disk",
          "Off by default",
          "Nothing, unless you turn it on",
          "0 (off)",
          "0 (off)",
        ],
      ],
    ),
    p(
      "**The points share one fixed budget.** Every signal's points, plus any of your own removal rules, must total exactly 100 before you can save. A point is literally how much that signal can add to the score. So lowering one signal frees points you must hand to another signal or removal rule first. Setting a signal to 0 turns it off and returns its points to the pot.",
    ),
    callout(
      "note",
      "**Size is off on purpose.** Weighting size aims Reaper at your biggest files, which are usually the 4K titles people chose to keep. Size measures how much space you would reclaim, not whether anyone still wants it. Leave it off, and instead sort the flagged list by size at review time and approve the big ones first.",
    ),

    h3("Protections: what's always kept", "protections"),
    p(
      "All of these are on by default, in both the movie and TV policies. Any one that fires keeps the title, no matter its score. A protection Reaper cannot check keeps the file.",
    ),
    table(
      ["Protection", "What it keeps", "Default"],
      [
        [
          "Give every title time to be rewatched",
          "Anything younger than the age line",
          "3 years (cannot go below 5 days)",
        ],
        [
          "Keep what your users actually watch",
          "Anything enough people played recently",
          "3 people, within the last year",
        ],
        [
          "Keep well-rated titles",
          "Anything clearing your rating bar",
          "IMDb 7.5, at least 1,000 votes",
        ],
        [
          "Never touch something playing right now",
          "Anything being watched at that moment",
          "On, re-checked live",
        ],
        // Not "Titles older than your watch history": that outcome comes from the dormancy
        // clamp in fact derivation, not from this switch, and the gate itself can only
        // abstain (see `components/policyMeta.ts`). The row has to say what it keeps,
        // because the sentence above this table promises every row keeps something.
        ["Stop if the unwatched time can't be read", "Anything Reaper couldn't measure", "On"],
      ],
    ),
    p(
      "Your lists protect through **keep rules**, below: each list on Settings, Lists acts through a rule here naming it, and a new list starts with a rule that keeps every title on it outright. Removing the list removes its rules with it.",
    ),

    h3("Pace and limits", "pace"),
    p(
      "These limit how much can happen, and are shared by movies and TV. They take effect the moment you save.",
    ),
    table(
      ["Limit", "What it bounds", "Default", "Floor"],
      [
        ["Most titles per run", "Titles one run may remove", "10", "1"],
        ["Most disk freed per run", "Space one run may remove", "500 GB", "Any amount"],
        ["Most titles per 30 days", "Titles over any rolling 30 days", "100", "the run cap"],
        ["Most disk freed per 30 days", "Space over any rolling 30 days", "2 TB", "the run cap"],
        ["Grace period", "Days a flagged title shows as leaving", "14 days", "7 days"],
        [
          "Unknown-size items per run",
          "Titles whose size Reaper cannot read",
          "0 (held back)",
          "0",
        ],
      ],
    ),
    p(
      'Caps stop the whole run when crossed, and the rolling 30-day limits count every run in the window. Turning off "Limit how much each run removes" drops the first four rows above. The unknown-size allowance and the countdown are unaffected, so items Reaper cannot measure are still held back.',
    ),

    h3("Movies and TV are tuned separately", "movies-tv"),
    p(
      'They are two policies behind one toggle, each with its own line, signals, and rating bars, plus TV’s season rules: keep the 2 newest seasons and the first season, hold an in-progress viewer’s place for 180 days (and optionally some seasons ahead of it, which starts at none), keep specials, and flag an unusual removal as "Needs a look." Switching the toggle with unsaved edits warns before it discards them.',
    ),

    h2("Using the simulator", "simulator"),
    p(
      'The right-hand "What this would do" panel re-decides your last scan under your draft, with zero calls to Sonarr, Radarr, or your history source. It shows how many titles would be removed, how much space that frees, how many titles your edit moves at all, every title’s score against your line, example titles newly flagged, and how this draft differs from the policy you already saved.',
    ),
    p(
      "Nudge one control, watch the number, repeat. If the count jumps more than you expected, put the control back and move it half as far.",
    ),
    p(
      'An edit can be real and still move nothing, and "Titles that change" is the row that tells you which one you are looking at. The two removal counts only move when a title crosses your line, so a protection can shuffle titles between spared and not judged while every other number holds still. When nothing moves at all the panel says "Your changes leave every title as it is." That is an answer rather than a failure: a protection can carry no weight on your library, which is worth knowing before you decide whether to keep it on.',
    ),
    p(
      "**The panel is live for the numbers.** Moving the flag threshold, how much is enough to go on, a signal’s points, a rating bar, one of your own rules, a protection's switch and its own numbers, or any TV season rule updates the panel instantly. Some edits still need a fresh scan, and the panel names which one is at fault and offers a Scan now button. A list or how far back watching counts changes what a scan reads. A season rule needs one scan on this version before it can preview, because the panel needs something the older scan never recorded. And keeping seasons someone is partway through needs one whenever the last scan didn't read where anyone had gotten to. An upgrade can ask for a scan too, without you having touched anything. A wrong number that looks live is worse than a blank one.",
    ),
    callout(
      "caution",
      'You have gone too far when the examples include titles you would keep, or you want to switch off a protection to "find more." Turning off a protection is the most effective way to remove more, and it looks like simplification. That is the warning sign. If a one-step drop nearly doubles the count, stop there.',
    ),

    h2("Recipes", "recipes"),
    p("Starting points for common goals."),
    h3("Reclaim space without risk"),
    p(
      "Use the Balanced defaults: line 70, the 3-year age line, keep well-rated (IMDb 7.5 with 1,000 votes), keep anything 3 or more people watched in the last year, caps on, 14-day grace. Turn on Leaving Soon. Do not add size to the score. At review time, sort by size and approve the big ones first.",
    ),
    h3("Very cautious setup"),
    p(
      "Use Cautious: line 82, 5 titles or 250 GB per run, 50 or 1 TB per 30 days, 30-day grace. Leave every protection on and the unknown-size allowance at 0. Consider raising the age line above 3 years, and live through a couple of grace cycles before loosening anything.",
    ),
    h3("Big backlog of never-played requests"),
    p(
      "Keep the defaults, especially the 3-year age line and keep well-rated. Old, never-watched, mediocre requests are exactly what Balanced already flags. For one supervised first cleanup you can raise the per-run cap for that run only, leave the 30-day cap in place, then turn it back down.",
    ),
    h3("Movies only, or TV only"),
    p(
      "The two are separate policies. Tune and scan the one you care about, leave the other on its defaults, and do not approve its items. TV’s season rules already keep the 2 newest seasons and the first, hold an in-progress viewer’s place for 180 days, and keep specials.",
    ),

    h2("Common mistakes", "mistakes"),
    ul([
      "**The points don't total 100, so the policy won't save.** Lowering one signal strands its points and holds the policy half back. Pace and grace still save. Safe habit: give the freed points to another signal or removal rule before saving.",
      "**Forgetting that saving starts a fresh scan.** A policy change only takes effect after a scan, which begins by itself when you save. Safe habit: expect the rescan. Pace and grace apply immediately while policy changes wait for it.",
      "**Treating arming like tuning.** Editing a policy changes nothing about whether Reaper can delete. Safe habit: set caps, grace, and Leaving Soon first, then arm, then keep the first run supervised.",
      "**Going aggressive on the first pass.** Removing more is one nudge away and reversible until deletion. Safe habit: start on Cautious and loosen only as far as each supervised cycle proves safe.",
      "**Switching off a protection to find more to delete.** This exposes titles the protection was keeping, whatever their score. Safe habit: to remove more, lower the line in the simulator and watch the number.",
      "**Setting a rating bar with no vote floor.** A high rating from a few hundred votes is unreliable and would protect titles forever. Safe habit: keep a vote floor on any source that counts votes (the default is 1,000).",
    ]),

    h2("Glossary", "glossary"),
    defs([
      {
        term: "Policy",
        text: "Your rulebook for one media type: the line, the signals, the protections.",
      },
      {
        term: "Flag threshold",
        text: "The score, out of 100, at or above which a title becomes a candidate for removal.",
      },
      {
        term: "Signal",
        text: "One reason to remove, adding up to its number of points to the score.",
      },
      {
        term: "Protection",
        text: "A hard line that keeps a title no matter its score, and can never remove one.",
      },
      {
        term: "Grace",
        text: "The days a flagged title shows as leaving, so someone can watch it or you can spare it. You still start every removal by hand.",
      },
      {
        term: "Cap",
        text: 'A limit on how much one run, or a rolling 30 days, may remove, while "Limit how much each run removes" is on. Crossing it stops the run.',
      },
    ]),

    callout(
      "tip",
      "**Start cautious, watch a few real runs, and tighten one nudge at a time.** You can always remove more later. You can never un-delete.",
    ),
  ],
};
