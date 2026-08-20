// SPDX-License-Identifier: AGPL-3.0-or-later

import { callout, diagram, type Doc, h2, p, ul } from "../blocks";

export const deletionSafety: Doc = {
  id: "how-a-delete-is-kept-safe",
  group: "Safety",
  title: "How a delete is kept safe",
  summary: "The path a file takes from candidate to deleted, and the safeties along the way.",
  body: [
    p(
      "Reaper removes irreplaceable files from a server other people depend on, so the app keeps any file that's unclear.",
    ),
    callout(
      "tip",
      "**Every file Reaper removes leaves through Sonarr or Radarr.** Reaper won't touch anything that neither one manages. Your new install stays read-only until you arm it with a password.",
    ),

    h2("The path a delete takes", "the-path"),
    p("Each step has its own gate. You can stop it at any point."),
    diagram({
      title: "The operator's path",
      steps: [
        { node: { text: "Scan the library", sub: "read only" } },
        {
          node: { text: "Scan complete?", shape: "decision" },
          branch: { label: "a source failed", node: { text: "View only", shape: "terminal" } },
        },
        {
          node: { text: "Review the three lists", sub: "Condemned, Sanctuary, Limbo" },
          enter: { label: "yes" },
        },
        { node: { text: "Read each item's reason", sub: "score, and every protection checked" } },
        {
          node: {
            text: "Build the plan",
            sub: "unknown size held back by default, smallest first",
          },
        },
        { node: { text: "Practice run", sub: "every check runs, nothing sent" } },
        {
          node: { text: "Deletion armed?", shape: "decision" },
          branch: { label: "off, the default", node: { text: "Delete off", shape: "terminal" } },
        },
        {
          node: { text: "Type the exact phrase", sub: "counts this plan's titles and size" },
          enter: { label: "on, needs password" },
        },
        { node: { text: "Reap, one item at a time", sub: "live progress, Stop anytime" } },
        {
          node: {
            text: "Confirm it's really gone",
            sub: "re-read from your servers, rescan",
            shape: "terminal",
          },
        },
      ],
    }),

    h2("Two locks keep it off", "two-locks"),
    p(
      "Every request to your servers travels through one connection. That connection carries a deletion only when both of these are true.",
    ),
    ul([
      "**Deletion is armed.** This is the switch you set in Policy, Deletion. It's stored in the database and defaults to off.",
      "**A plan must be made first.** Reaper writes every removal to a record before it's sent. It'll refuse any request that skips that step, even while armed.",
    ]),
    p(
      "The two are independent, so either one alone still refuses the delete. A brand-new feature is safe by existing, because it travels through that same guarded connection. Reaper reaches Plex through a separate connection with the same two locks, and it holds back even a library refresh, because on some servers a refresh empties the trash.",
    ),

    h2("Judged on frozen, complete evidence", "frozen"),
    p(
      "Everything's gathered and frozen before the scoring starts. This means no item's fate depends on a source timing out halfway through a scan. If a source that could condemn something was unreachable, the scan is marked incomplete. You can look at it, but you can't run anything against it.",
    ),
    callout(
      "note",
      "**Not knowing can only ever protect.** A missing rating, a viewer Reaper cannot match, or a source that failed won't count against a title.",
    ),

    h2("Checked again at the moment of deletion", "at-delete"),
    p(
      "You can reverse everything above, but the reap isn't reversible. Each file runs one last set of checks live right before it goes. Every check keeps the file when it's unsure.",
    ),
    diagram({
      title: "One file, at the moment of deletion",
      legend: [
        { tone: "keep", text: "the file is kept" },
        { tone: "stop", text: "the run stops" },
      ],
      steps: [
        { node: { text: "Approved run begins", shape: "terminal" } },
        {
          node: { text: "Approved list unchanged?", shape: "decision" },
          enter: { phase: "checked once, before any file" },
          branch: { label: "changed", node: { text: "Stop the run", tone: "stop" } },
        },
        {
          node: { text: "Within the caps?", shape: "decision" },
          enter: { label: "ok" },
          branch: { label: "over", node: { text: "Stop the run", tone: "stop" } },
        },
        {
          node: { text: "Still armed?", sub: "re-read per file", shape: "decision" },
          enter: { phase: "then, for each file in turn" },
          branch: { label: "off or unreadable", node: { text: "Stop the run", tone: "stop" } },
        },
        {
          node: { text: "Spared by hand?", shape: "decision" },
          enter: { label: "on" },
          branch: { label: "yes", node: { text: "Keep this file", tone: "keep" } },
        },
        {
          node: { text: "Being watched now?", shape: "decision" },
          enter: { label: "no" },
          branch: { label: "yes or unreadable", node: { text: "Keep this file", tone: "keep" } },
        },
        {
          node: { text: "Played since approval?", shape: "decision" },
          enter: { label: "no" },
          branch: { label: "yes or unreadable", node: { text: "Keep this file", tone: "keep" } },
        },
        {
          node: { text: "Still the same file?", shape: "decision" },
          enter: { label: "no" },
          branch: { label: "grew or unreadable", node: { text: "Keep this file", tone: "keep" } },
        },
        {
          node: { text: "Remove via Sonarr / Radarr", sub: "unmonitored first, then removed" },
          enter: { label: "ok" },
        },
        {
          node: { text: "Did it really go?", shape: "decision" },
          branch: {
            label: "first delete misbehaves",
            node: { text: "Halt the run", tone: "stop" },
          },
        },
        { node: { text: "Verified gone", shape: "terminal" }, enter: { label: "yes" } },
      ],
    }),
    ul([
      "**The test file goes first.** The smallest file with a known size goes alone and is verified before any other is touched. If it doesn't behave exactly as expected, the whole run halts. Files spared or still being watched are skipped first. If a later file misbehaves, it's recorded and the run carries on.",
      "**Removed so it stays gone.** Turn on the import exclusion for a Radarr instance so a movie it removes stays off your lists and won't quietly re-download. It's off by default until you set it per instance. Reaper unmonitors the season first, then confirms that it worked.",
      '**The caps hold, while they are on.** A run that would cross your per-run or 30-day limit stops here. Switching off "Limit how much each run removes" in Policy leaves the password, your typed confirmation, and every live check standing.',
    ]),

    h2("Grace: a window to catch it before it's gone", "grace"),
    p(
      "When a title is first condemned, a countdown starts. It's 14 days by default. Turn on the Leaving Soon shelf and Reaper marks it in Plex so your users can keep a title they still want by simply watching it. You can also send a Discord message.",
    ),
    callout(
      "caution",
      "**The countdown is the time your users have to catch it.** Once it ends, the title is ready for your next review, but you still start every deletion by hand. Watch it or spare it to keep a title.",
    ),

    h2("Sharp edges", "edges"),
    ul([
      "**A scripted deploy can start armed.** The password gate covers the switch in the app. You can turn deletion on at first boot without a password by setting `REAPER_DESTRUCTIVE_ACTIONS_ENABLED=true` in the environment. This is meant for infrastructure-as-code installs.",
      "**That environment setting is the default until you use the switch.** Once you turn deletion on or off in the app, the app's switch wins for good. Use Policy, Deletion to return a running install to read-only.",
      "**A few read failures keep files, and the scan carries on.** If Reaper can't list a service's folders, it won't match the items it's unsure about, so those files are kept. A small hiccup does not always raise an incomplete-scan banner. Check the logs if you think something is missing.",
      '**The Leaving Soon shelf is off until you turn it on.** To update it in Plex, you need deletion armed unless you also turn on "Update while read-only" in Settings, Plex. It only reaches people who browse or pinned that library. Wire up the Discord webhook to warn everyone else.',
    ]),
  ],
};
