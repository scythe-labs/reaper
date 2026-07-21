// SPDX-License-Identifier: AGPL-3.0-or-later

import { callout, type Doc, h2, p, steps, ul } from "../blocks";

export const deletionSafety: Doc = {
  id: "how-a-delete-is-kept-safe",
  group: "Safety",
  title: "How a delete is kept safe",
  summary: "The path a file takes from candidate to deleted, and the safeties that guard every step of it.",
  body: [
    p(
      "Reaper removes irreplaceable files from a server other people depend on, so the whole design leans one way: every time something is unclear, the file is kept. Nothing is ever deleted on a schedule. A person drives every deletion, along a path that gets deliberately harder at each step.",
    ),
    callout(
      "tip",
      "**Reaper has no way to delete a file directly.** Its only removals go through Sonarr and Radarr; anything neither of them manages, it never touches. And a new install ships read-only until you arm it with a password.",
    ),

    h2("The path a delete takes", "the-path"),
    p("Seven steps, each with its own gate. You can stop at any point, and most of them change nothing on disk."),
    steps([
      {
        title: "Scan",
        text: "Reaper reads every source, freezes what it found, and only then scores. A scan never deletes. If a source it needed was unreachable, the whole scan is marked incomplete and cannot be acted on.",
      },
      {
        title: "Review",
        text: "You get three lists — condemned, protected, and left alone — and every item opens a plain explanation of its score and every protection that was checked. Spare anything by hand, or force something onto the list.",
      },
      {
        title: "Build a plan",
        text: "Reaper turns the condemned list into the exact set of removals. Anything whose size it could not measure is held back. The rest are ordered smallest-first.",
      },
      {
        title: "Dry run",
        text: "Reaper walks the whole plan and every safety check and sends nothing. You cannot reach a real run until a dry run comes back clean.",
      },
      {
        title: "Arm deletion",
        text: "A separate switch in Policy, Deletion, gated by your admin password and off by default. While it is off, the app physically cannot send a delete. Turning it back off never asks for anything.",
      },
      {
        title: "Confirm",
        text: "You type an exact phrase the server computes from this plan — `REAP 7 SOULS 214 GB`. It counts this plan's own titles and size, so a stale plan reads as obviously wrong and habit cannot carry you through.",
      },
      {
        title: "Reap",
        text: "The run goes one item at a time, with live progress and a Stop you can hit whenever. Each removal goes through Sonarr or Radarr, which also keeps the title from quietly re-downloading, and Reaper re-reads the world afterward to prove the file is actually gone.",
      },
    ]),

    h2("Two locks keep it off", "two-locks"),
    p(
      "Off is not a checkbox some future feature could forget. It is enforced at the lowest level Reaper controls: the connection every request to your servers travels through refuses to carry a deletion unless both of these are true.",
    ),
    ul([
      "**Deletion is armed.** The switch you set in Policy, Deletion. It lives in the database and defaults to off.",
      "**The delete was declared first.** Every removal is written to an action journal before it is sent. A request that skipped that step is refused, even while armed.",
    ]),
    p(
      "The two are independent on purpose. A bug that flipped the switch on would still be stopped by the missing declaration; a bug that skipped the journal would still find the switch off. A brand-new feature is safe simply by existing, because it still has to travel through that same guarded connection.",
    ),

    h2("Judged on frozen, complete evidence", "frozen"),
    p(
      "Everything is gathered and frozen before anything is scored, so no item's fate depends on a source timing out partway through a scan. If a source that could condemn something was unreachable, the scan is marked incomplete — you can look at it, but nothing can be run against it. Acting on half the evidence is how a tool deletes a loved film during an outage, so half the evidence is not allowed to delete at all.",
    ),
    callout(
      "note",
      '**Not knowing can only ever protect.** A missing rating, a user Reaper cannot map, a source that failed — none of them count against a title. "Nobody watched this" can condemn; "we could not find out whether anyone watched this" never can.',
    ),

    h2("Checked again at the moment of deletion", "at-delete"),
    p(
      "Everything above is reversible; the reap is not. So each file runs one last gauntlet, live, right before it goes — and every check keeps the file when it is unsure.",
    ),
    ul([
      "**Nobody is watching it.** Active streams are re-checked immediately before each delete, not once at the start. If Plex cannot be reached, the file is kept.",
      "**Nobody watched it since you approved.** A late view still rescues a title, and any doubt keeps it.",
      "**It is still the file you approved.** If it grew — upgraded since the scan — it is a different, bigger file than the one you confirmed, so it is kept for you to review again.",
      "**Deletion is still armed.** The switch is re-read before every single item, so turning it off in the app stops a run already in progress.",
      "**The test item goes first.** The smallest file is removed and verified alone; if it does not go exactly as expected, the whole run halts before anything else is touched.",
      "**The caps hold.** A run over its per-run or 30-day limit stops entirely rather than deleting the part that fits.",
    ]),

    h2("Grace is a warning, not a wall", "grace"),
    p(
      "When a title is first condemned, a countdown starts — 14 days by default. Turn on the Leaving Soon shelf and Reaper marks it in Plex so your household can object by simply watching it, and it can send a Discord heads-up too.",
    ),
    callout(
      "caution",
      "**Nothing is deleted when the countdown ends** — nothing deletes on its own at all. The countdown is time to notice and object. What actually saves a still-wanted file is you sparing it, or someone watching it, both of which the checks above honor at the moment of deletion.",
    ),
  ],
};
