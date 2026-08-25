// SPDX-License-Identifier: AGPL-3.0-or-later
//
// The full policy guide. Every number here is a shipped default from engine/policy.py and
// the frontend presets (PolicyEditor.tsx). When a default changes, change it here too.

import { callout, type Doc, h2, h3, p, steps, table, ul } from "../blocks";

export const understandingPolicy: Doc = {
  id: "understanding-policy",
  group: "Policy",
  title: "Understanding policy",
  summary: "Your policy is the rulebook Reaper follows to decide what to remove.",
  body: [
    p(
      'A policy tells Reaper what "nobody watches this" means for your server. It gathers reasons a title looks expendable, adds them into a single score, and flags anything that crosses your line. A separate set of protections can keep a title no matter how it scored.',
    ),
    callout(
      "tip",
      "**Start cautious, then tighten one nudge at a time.** You can always remove more later. You can never un-delete.",
    ),

    h2("The mental model", "mental-model"),
    p(
      "**The flag threshold is your line.** Reaper gives every title a score from 0 to 100. Titles at or above your line become candidates for removal, while Reaper leaves anything below it alone. If you set a higher line, Reaper has to be more sure.",
    ),
    p(
      "**Signals are soft pressure that adds up.** How long it has gone unwatched, how few people watch it, and how low it is rated each push the score up by its weight. In practice, how long it has gone unwatched does almost all the work.",
    ),
    p(
      "**Protections are hard lines that always win.** They only ever keep a file and can never remove one. If any single protection fires, the title stays regardless of its score.",
    ),
    p(
      "**Caps stop a run; grace is the heads-up.** [Pace and limits](understanding-policy#pace) sets both. Grace shows every flagged title as leaving for a set number of days. You keep a title by watching it or sparing it. Nothing goes until you run a reap yourself.",
    ),
    p(
      "**Missing information can only protect, never delete.** The score is pressure that adds up from zero. If Reaper can't read something, like an outage, a stale ratings file, or a title it cannot match, it adds no pressure.",
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
        text: "Scores are based on how long a title has sat unwatched. Your history source tells Reaper when a title was last played and how far back it can look. Connect it and let one scan finish against real data before you tune anything.",
      },
      {
        title: "Start on the Cautious footing.",
        text: "Start with Cautious. It sets a high line (82), small runs (5 titles or 250 GB), and a long grace window (30 days). Don't hand-edit the points yet.",
      },
      {
        title: "Leave every protection on.",
        text: "Leave the time-to-be-rewatched line at its 3-year default. It's a hard line that nothing can outvote.",
      },
      {
        title: "Run a scan and let it finish.",
        // "incomplete" is the word the app itself shows (ScanBar, ScanFreshness). "degraded" is
        // the internal field name, which rules 21 and 25 both bar from operator copy (U-12).
        text: "A scan freezes all the evidence and then scores it so a brief timeout can't flip a title's fate mid-run. If a scan comes back \"incomplete,\" a source failed and Reaper marked the run unusable. Fix the source and scan again.",
      },
      {
        title: 'Read a few "why" panels before touching anything.',
        text: "Open several flagged titles to see why they were flagged and which protections were checked and did not fire. You'll notice how much of the pressure is just time unwatched.",
      },
      {
        title: "Tune with the simulator, one nudge at a time.",
        text: "Move the line down a step. Watch the count, the space reclaimed, and the named titles change live. Tighten gradually over several scans.",
      },
      {
        title: "Set pace and grace, then arm deletion last.",
        text: 'Confirm your caps and grace, then turn on the Leaving Soon shelf so your users can rescue anything about to go. To see the shelf before you arm anything, turn on "Update while read-only" in Settings, Plex. Arm deletion only after that. It\'s password-gated and separate from all tuning.',
      },
    ]),

    h2("What's in a policy", "in-a-policy"),
    // "which starts by itself" is the same fact as `appliesOnNextScan`
    // (components/PolicySimulator.tsx), the sentence the savebar and the simulator both show.
    // Kept as its own wording because this paragraph names which controls sit in that half,
    // and left saying only "take effect on the next scan" it read as a chore the operator has
    // to remember to start (rule 144).
    p(
      "A policy has two parts. The rules that change what Reaper decides (the line, signals, protections) take effect on the next scan, which starts by itself when you save. The limits on how much one run may remove, and how long a title shows as leaving (caps and grace), take effect immediately. Movies and TV are two separate policies, tuned on their own.",
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
      "The threshold is a confidence line. Reaper only flags a title when its score reaches it, so a higher line means Reaper flags fewer titles and has to be more sure. Move it down one step at a time in the simulator and watch what appears.",
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
          "The time since the last play or the day it arrived",
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
      "**The points share one fixed budget.** Every signal's points and your removal rules must total exactly 100 before you can save. A point is how much a signal can add to the score. Lowering one signal frees points you must hand to another signal or removal rule first. Setting a signal to 0 turns it off and returns its points to the pot.",
    ),
    callout(
      "note",
      "**Size is off on purpose.** Weighting size targets your biggest files, which are usually the 4K titles you chose to keep. Size measures how much space you'll reclaim, not whether anyone still wants the file. Leave it off and sort the flagged list by size at review time to approve the big ones first.",
    ),

    h3("Protections: what's always kept", "protections"),
    p(
      "If a rule fires, Reaper keeps the title regardless of its score. A protection Reaper cannot check also keeps the file. These are on by default in both the movie and TV policies, but the last two stay off until you turn them on.",
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
        [
          "Keep titles most likely to be rewatched above a percentage",
          "Titles like it watched again above your percentage",
          "Off by default",
        ],
        [
          "Keep a title that came back",
          "Anything that left your library and was fetched again",
          "Off by default",
        ],
      ],
    ),
    p(
      "Your **keep rules** protect your files through your lists. Each list on Settings, Lists acts through a rule here naming it. A list you add starts with no rule, so it protects nothing until you give it one here. You choose whether it keeps every title outright or only leans that way. The lists Reaper ships come with a keep-everything rule already. Removing the list removes its rules with it.",
    ),

    h3("Titles most likely to be rewatched", "rewatch-keep"),
    p(
      "Some titles get watched over and over, like a comfort movie or a show you binge every winter. This rule lowers the score by up to 20 points so a genuine favorite is harder to flag.",
    ),
    p(
      "A movie needs at least 10 plays by anyone on the server, including at least one in the last 2 years. Getting halfway through counts as a play. Plays within a week of each other collapse into one, so watching something three times over a weekend only registers once.",
    ),
    p(
      "For a show, this means someone on the server has gone back to episodes already seen at least twice. Watching a new season as it airs is a first watch, so it doesn't count toward a rewatch.",
    ),
    p(
      "Turn the second switch on to protect anything that still has a real shot at being watched again, even if the score is high. Reaper figures out that shot by looking at how long a title has gone unwatched and checking what happened to other titles in your library that sat idle for about the same amount of time. It looks at what percentage of those titles got watched again within a year, and gives the title the benefit of the doubt: the smaller the group, the more generous the number, so a small group is never judged as a flat zero. If that meets or beats your threshold, the title stays.",
    ),
    p(
      "Imagine a movie hasn't been watched in 2 years. Reaper finds 100 other titles that also sat for 2 years, and 30 of them were watched again within a year. With the benefit of the doubt, that counts as up to a 40% chance. If your threshold is set to 25, Reaper keeps the movie. At 45, the score decides.",
    ),
    p(
      "Shows are measured as a whole. Any episode counts as activity, so keeping a show means keeping all its seasons. Imagine a show hasn't been touched for a year. Out of 60 other shows that sat for a year, 20 came back, up to a 46% chance. At a threshold of 25, the show is kept.",
    ),
    p(
      "Every scan, Reaper pretends today was a year ago and sorts every title by how long it sat there: under a year, one to two years, and so on. Since it rewound the clock, the following year is already in your history so it can count how many in each group got played again. Those counts fill the table on the card, one row per group. A group needs at least 30 titles before its number is used.",
    ),
    p(
      "It's like guessing if a kid will ride that old bike in the garage. You don't study the bike itself. You just think about what happened to every other bike that sat in the garage that long.",
    ),

    h3("A title that came back", "came-back"),
    p(
      "Turn this on so Reaper holds onto a title that was removed and added back for as long as you set. This prevents the same title from getting flagged again until that time is up. The default is 1.5 years.",
    ),
    // The absence is an operator setting (`RETURN_ABSENCE_DAYS`, the "counts as gone after"
    // control in `policyMeta.ts`), so name the control and give its default rather than
    // stating a bare number this page would then have to chase.
    p(
      'Reaper picks this up through Plex. A file that leaves and comes back shows up as a new entry in Plex even though it\'s the same film. It only counts as gone if the title was missing for longer than your "counts as gone after" setting, which starts at 7 days. A file swapped out for a better copy comes back within hours and is ignored.',
    ),
    // Two, not one: `library_seen._SCANS_INSIDE_THE_ABSENCE`.
    p(
      "Reaper needs at least two scans to run while a title is missing to account for its absence. A long gap between scans won't mark titles as coming back.",
    ),
    p(
      "The card shows you which titles came back, and a countdown protects each one based on your configured policy. Reaper tracks them through Plex even if it wasn't the one that removed the title.",
    ),
    p(
      "Reaper needs to see a title before it can spot it returning, so a fresh install won't have anything to compare against yet. It builds that picture over time as it watches your library.",
    ),

    h3("Pace and limits", "pace"),
    p("These limits apply to both movies and TV. They take effect as soon as you save."),
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
      'Caps stop the whole run when crossed, and Reaper never removes just the part that fits. The rolling 30-day limits count every run in the window. If you turn off "Limit how much each run removes", the first four rows above no longer apply. The unknown-size allowance and the countdown are unaffected, so items Reaper cannot measure are still held back.',
    ),

    h3("Movies and TV are tuned separately", "movies-tv"),
    p(
      "One toggle controls two policies. Each has its own line, signals, and rating bars. TV's season rules keep the 2 newest seasons and the first season, hold an in-progress viewer's place for 180 days (and optionally some seasons ahead of it, which starts at none), keep specials, and flag an unusual removal as \"Needs a look.\" Switching the toggle with unsaved edits warns you before it discards them.",
    ),

    h2("Using the simulator", "simulator"),
    p(
      'The "What this would do" panel on the right re-decides your last scan under your draft without making any calls to Sonarr, Radarr, or your history source. For your draft it shows:',
    ),
    ul([
      "number of titles that would be removed and how much space that would free",
      "how many titles your edit moves at all",
      "every title’s score against your line",
      "example titles newly flagged",
      "how this draft differs from the policy you already saved",
    ]),
    p(
      "Nudge one control, watch the number, and repeat. If the count jumps more than you expected, put the control back and move it half as far.",
    ),
    p(
      'An edit can be real and still move nothing. "Titles that change" is the row that tells you which of the two cases you\'re in. The two removal counts only move when a title crosses your line, so a protection can shuffle titles between spared and not judged while every other number holds still. When nothing moves at all, the panel says "Your changes leave every title as it is." That\'s an answer, not a failure. A protection can carry no weight on your library, which is worth knowing before you keep it on.',
    ),
    p(
      "**The panel is live for the numbers.** Moving the flag threshold, how much is enough to go on, a signal’s points, a rating bar, one of your own rules, a protection's switch and its own numbers, or any TV season rule updates the panel instantly.",
    ),
    p(
      "Some edits need a fresh scan first. The panel names which one and offers a Scan now button:",
    ),
    ul([
      "Changing your list or how far back watching counts changes what a scan reads.",
      "You need to run one scan on this version before you can preview a season rule because the panel needs data that the older scan didn't record.",
      "The setting to keep seasons someone is partway through needs a fresh scan whenever the last scan didn't read where anyone had gotten to.",
      "An upgrade might trigger a scan on its own without you doing anything.",
    ]),
    callout(
      "caution",
      "You've gone too far if the examples include titles you'd keep. If a one-step drop nearly doubles the count, stop there.",
    ),

    h2("Recipes", "recipes"),
    p("Starting points for common goals."),
    h3("Reclaim space without risk"),
    p(
      "Use the Balanced defaults: line 70, the 3-year age line, keep well-rated (IMDb 7.5 with 1,000 votes), keep anything 3 or more people watched in the last year, caps on, and 14-day grace. Turn on Leaving Soon.",
    ),
    h3("Very cautious setup"),
    p(
      "Use Cautious: line 82, 5 titles or 250 GB per run, 50 or 1 TB per 30 days, 30-day grace. Leave every protection on and set the unknown-size allowance to 0. You can raise the age line above 3 years, but live through a couple of grace cycles before you loosen anything.",
    ),
    h3("Big backlog of never-played requests"),
    p(
      "Stick with the defaults, especially the 3-year age line and keep well-rated. Balanced already flags old, never-watched, mediocre requests. If you're doing a supervised first cleanup, you can raise the per-run cap for that run only, leave the 30-day cap in place, and then turn it back down.",
    ),
    h3("Movies only, or TV only"),
    p(
      "These are two separate policies. Tune and scan the one you care about, leave the other on its defaults, and do not approve its items. TV's season rules keep the 2 newest seasons and the first, hold an in-progress viewer's place for 180 days, and keep specials.",
    ),

    h2("Common mistakes", "mistakes"),
    ul([
      "**The points don't total 100, so the policy won't save.** If you lower a signal, its points get stranded and the policy stays half finished. Pace and grace still save. To keep a safe habit, give those freed points to another signal or removal rule before you save.",
      "**Forgetting that saving starts a fresh scan.** A policy change only takes effect after a scan, which begins by itself when you save. Safe habit: expect the rescan. Pace and grace apply immediately while policy changes wait for it.",
      "**Treating arming like tuning.** Editing a policy changes nothing about whether Reaper can delete. Safe habit: set caps, grace, and Leaving Soon first, then arm, then keep the first run supervised.",
      "**Switching off a protection to find more to delete.** It is the most effective way to remove more, and it looks like simplification, but it exposes titles the protection was keeping regardless of their score. To remove more, lower the line in the simulator and watch the number.",
      "**Setting a rating bar with no vote floor.** A high rating from only a few hundred votes isn't reliable and could protect titles forever. It's a safe habit to keep a vote floor on any source that counts votes. The default is 1,000.",
    ]),
  ],
};
