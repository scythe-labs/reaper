// SPDX-License-Identifier: AGPL-3.0-or-later
//
// The full policy guide. Every number here is a shipped default from engine/policy.py and
// the frontend presets (PolicyEditor.tsx). When a default changes, change it here too.

import { callout, defs, type Doc, h2, h3, p, steps, table, ul } from "../blocks";

export const understandingPolicy: Doc = {
  id: "understanding-policy",
  group: "Policy",
  title: "Understanding policy",
  summary:
    "Your policy is the rulebook Reaper follows when it decides what to remove. Here is how it thinks, and how to set it up safely the first time.",
  body: [
    p(
      'A policy is how you tell Reaper what "nobody watches this" means for your server. It gathers reasons a title looks expendable, adds them into a single score, and flags anything that crosses your line. A whole separate set of protections can keep a title no matter how it scored.',
    ),
    callout(
      "tip",
      "**Start cautious, then tighten one nudge at a time.** You can always remove more later, and it is reversible up to the moment of deletion. A deletion is not. This one habit is what the whole page is built around.",
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
      "**Caps and grace are your seatbelts.** Caps limit how much any one run, and any rolling 30 days, can remove, and they stop the whole run rather than trimming it. Grace holds every flagged title for a set number of days, cancellable, before anything is removed.",
    ),
    p(
      "**Missing information can only protect, never delete.** The score is pressure that adds up from zero. Anything Reaper cannot read, an outage, a stale ratings file, a title it cannot match, simply adds no pressure. The worst a failure can do is make Reaper more cautious.",
    ),

    h2("The recommended workflow", "workflow"),
    p("Follow it in order the first time."),
    steps([
      {
        title: "Let Reaper learn your audience first.",
        text: "Reaper judges dormant titles against how often your own viewers circle back to something they left alone, measured from your watch history. Make sure your history source is connected and a scan has run against real data.",
      },
      {
        title: "Start on the Cautious footing.",
        text: "Pick Cautious as your starting point. It sets a high line (82), small runs (5 titles or 250 GB), and a long grace window (30 days). Do not hand-edit the points yet.",
      },
      {
        title: "Leave every protection on.",
        text: "Above all, leave the time-to-be-rewatched line at its 3-year default. This is the single most important line in the whole tool, and it is a hard line so nothing can outvote it.",
      },
      {
        title: "Run a scan and let it finish.",
        text: 'A scan freezes all the evidence, then scores, so a brief timeout can never flip a title’s fate mid-run. If it comes back "degraded," a source failed and Reaper marked the run unusable on purpose. Fix the source and scan again.',
      },
      {
        title: 'Read a few "why" panels before touching anything.',
        text: "Open several flagged titles and read why each was flagged, and which protections were checked and did not fire. Notice how much of the pressure is just dormancy.",
      },
      {
        title: "Tune with the simulator, one nudge at a time.",
        text: "Move the line down a step and watch the count, the space reclaimed, and the named titles change live. Change one control, look at the number, repeat. Tighten gradually over several scans.",
      },
      {
        title: "Set pace and grace, then arm deletion last.",
        text: "Confirm your caps and grace, and turn on the Leaving Soon shelf so your household can rescue anything about to go. Only then arm deletion, which is password-gated and separate from all tuning.",
      },
    ]),

    h2("What's in a policy", "in-a-policy"),
    p(
      "A policy has two halves. The rules that change what Reaper decides (the line, signals, protections) take effect on the next scan. The limits on how much it may do and how long it waits (caps and grace) take effect immediately. Movies and TV are two separate policies, tuned on their own.",
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
      "A starting point only sets the line, the point mix, and those caps. It never changes your protections, keep tags, rating bars, or TV season rules.",
    ),

    h3("The flag threshold", "threshold"),
    p(
      'The threshold is a confidence line, not a "disk is full" gauge and not a "delete everything older than N" rule. A title is flagged only when its score reaches your line. Higher means Reaper flags fewer titles and has to be more sure; lower means it flags more. Move it down one step at a time in the simulator and watch what appears, rather than jumping. Because the score counts only what Reaper could actually read, a title cannot climb near your line on fragments of evidence.',
    ),

    h3("Signals: soft pressure", "signals"),
    p("Each signal pushes the score up by up to its number of points. The default mix differs for movies and TV."),
    table(
      ["Signal", "What it means", "Movie points", "TV points"],
      [
        ["How long it's gone unwatched", "Time since anyone last played it", "70", "60"],
        ["How few people watch it", "Fewer recent viewers means more pressure", "20", "15"],
        ["How old a season is", "Older seasons carry more pressure (TV only)", "not used", "15"],
        ["How low it's rated", "Lower ratings add a little pressure", "10", "10"],
        ["How big it is on disk", "Off by default", "0 (off)", "0 (off)"],
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
        ["Give every title time to be rewatched", "Anything younger than the age line", "3 years (cannot go below 5 days)"],
        ["Keep what your users actually watch", "Anything enough people played recently", "3 people, within the last year"],
        ["Keep well-rated titles", "Anything clearing your rating bar", "IMDb 7.5, at least 1,000 votes"],
        ["Spare titles you've tagged", "Anything with your keep tag", "Tag `reaper-keep`, plus a Never Reap collection"],
        ["Never touch something playing right now", "Anything being watched at that moment", "On, re-checked live"],
        ["Only touch what Sonarr or Radarr manages", "Files those apps do not manage", "On"],
        ["Don't judge what predates your history", "Titles older than your watch history", "On"],
        ["Honor protected lists", "Titles on a curated list", "On (IMDb Top 250)"],
      ],
    ),

    h3("Pace and limits", "pace"),
    p("These limit how much can happen, and are shared by movies and TV. They take effect the moment you save."),
    table(
      ["Limit", "What it bounds", "Default", "Floor"],
      [
        ["Most titles per run", "Titles one run may remove", "10", "1"],
        ["Most disk freed per run", "Space one run may remove", "500 GB", "1"],
        ["Most titles per 30 days", "Titles over any rolling 30 days", "100", "the run cap"],
        ["Most disk freed per 30 days", "Space over any rolling 30 days", "2 TB", "the run cap"],
        ["Grace period", "Days a flagged title waits, cancellable", "14 days", "7 days"],
        ["Unknown-size items per run", "No-size titles a run may remove", "0 (held back)", "0"],
      ],
    ),
    p(
      "Caps stop the whole run when crossed. They never remove just the part that fits. The rolling 30-day limits are what make a large runaway removal impossible across any sequence of runs. Items Reaper cannot measure are held back by default.",
    ),

    h3("Movies and TV are tuned separately", "movies-tv"),
    p(
      'They are two policies behind one toggle, each with its own line, signals, and rating bars, plus TV’s season rules: keep the 2 newest seasons and the first season, hold an in-progress viewer’s place for 180 days, keep specials, and flag an unusual removal as "Needs a look." Switching the toggle with unsaved edits warns before it discards them.',
    ),

    h2("Using the simulator", "simulator"),
    p(
      'The right-hand "What this would do" panel re-decides your last scan under your draft, with zero calls to Sonarr, Radarr, or your history source. It shows how many titles would be removed, how much space that frees, every title’s score against your line, example titles newly flagged, and how this draft differs from the policy you already saved.',
    ),
    p("**The loop:** nudge one control, watch the number, repeat. If the count jumps more than you expected, put the control back and move it half as far."),
    p(
      '**Live only for the threshold.** Moving the flag threshold updates the numbers instantly. Change a signal’s points, a protection, a watch window, a keep tag, or a season rule, and the panel says "Needs a fresh scan" and offers a Scan button. This is on purpose: a wrong number that looks live is worse than a blank one.',
    ),
    callout(
      "caution",
      'You have gone too far when the examples include titles you would keep, the count climbs steeply for a small threshold drop, or you want to switch off a protection to "find more." Turning off a protection is the most effective way to remove more, and it looks like simplification. That is the warning sign, not the fix. If a one-step drop nearly doubles the count, stop there.',
    ),

    h2("Recipes", "recipes"),
    p("Starting points for common goals. All of them are grounded in the shipped defaults."),
    h3("Reclaim space without risk"),
    p(
      "Use the Balanced defaults: line 70, the 3-year age line, keep well-rated (IMDb 7.5 with 1,000 votes), keep anything 3 or more people watched in the last year, caps on, 14-day grace. Turn on Leaving Soon. Do not add size to the score. At review time, sort by size and approve the big ones first.",
    ),
    h3("Very cautious household"),
    p(
      "Use Cautious: line 82, 5 titles or 250 GB per run, 50 or 1 TB per 30 days, 30-day grace. Leave every protection on and the unknown-size allowance at 0. Consider raising the age line above 3 years, and live through a couple of grace cycles before loosening anything.",
    ),
    h3("Big backlog of never-played requests"),
    p(
      "Keep the defaults, especially the 3-year age line and keep well-rated. Old, never-watched, mediocre requests are exactly what Balanced already flags. Because per-run caps stop rather than trim, for one supervised first cleanup you can raise the per-run cap for that run only, keep the 30-day cap and grace on, then turn it back down.",
    ),
    h3("Movies only, or TV only"),
    p(
      "The two are separate policies. Tune and scan the one you care about, leave the other on its defaults, and simply do not approve its items. TV’s season rules already keep the 2 newest seasons and the first, hold an in-progress viewer’s place for 180 days, and keep specials.",
    ),

    h2("Common mistakes", "mistakes"),
    ul([
      "**The points don't total 100, so Save is dead.** Lowering one signal strands its points and blocks the whole save. Safe habit: give the freed points to another signal or removal rule before saving.",
      "**Forgetting that saving starts a fresh scan.** A policy change only takes effect after a scan, which begins by itself when you save. Safe habit: expect the rescan, and know that pace and grace apply immediately while policy changes wait for it.",
      "**Treating arming like tuning.** Editing a policy changes nothing about whether Reaper can delete. Safe habit: set caps, grace, and Leaving Soon first, then arm, then keep the first run supervised.",
      "**Going aggressive on the first pass.** Removing more is one nudge away and reversible until deletion. A deletion is not. Safe habit: start on Cautious and loosen only as far as each supervised cycle proves safe.",
      "**Switching off a protection to find more to delete.** This raises every remaining score and aims the tool at borderline titles. Safe habit: to remove more, lower the line in the simulator and watch the number.",
      "**Setting a rating bar with no vote floor.** A high score from a few hundred votes is noise and would protect titles forever. Safe habit: keep a vote floor on any source that counts votes (the default is 1,000).",
    ]),

    h2("Glossary", "glossary"),
    defs([
      { term: "Policy", text: "Your rulebook for one media type: the line, the signals, the protections." },
      { term: "Flag threshold", text: "The score, out of 100, at or above which a title becomes a candidate for removal." },
      { term: "Signal", text: "One reason to remove, adding up to its number of points to the score." },
      { term: "Protection", text: "A hard line that keeps a title no matter its score, and can never remove one." },
      { term: "Grace", text: "The days a flagged title waits, cancellable, before anything is actually removed." },
      { term: "Cap", text: "A limit on how much one run, or a rolling 30 days, may remove. It stops the run rather than trimming it." },
    ]),

    callout(
      "tip",
      "**Start cautious, watch a few real runs, and tighten one nudge at a time.** You can always remove more later. You can never un-delete.",
    ),
  ],
};
