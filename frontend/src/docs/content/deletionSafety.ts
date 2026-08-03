// SPDX-License-Identifier: AGPL-3.0-or-later

import { callout, diagram, type Doc, h2, p, ul } from "../blocks";

export const deletionSafety: Doc = {
  id: "how-a-delete-is-kept-safe",
  group: "Safety",
  title: "How a delete is kept safe",
  summary:
    "The path a file takes from candidate to deleted, and the safeties that protect every step of it.",
  body: [
    p(
      "Reaper removes irreplaceable files from a server other people depend on, so every time something is unclear, the file is kept. The path gets deliberately harder at each step.",
    ),
    callout(
      "tip",
      "**Every file Reaper removes leaves through Sonarr or Radarr.** Anything neither one manages stays untouched. A new install is read-only until you arm it with a password.",
    ),

    h2("The path a delete takes", "the-path"),
    p(
      "Each step has its own gate. You can stop at any point, and most of them change nothing on disk.",
    ),
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
        { node: { text: "Build the plan", sub: "unknown size held back, smallest first" } },
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
      "The connection every request to your servers travels through carries a deletion only when both of these are true.",
    ),
    ul([
      "**Deletion is armed.** The switch you set in Policy, Deletion. It lives in the database and defaults to off.",
      "**The delete was declared first.** Every removal is written to a record before it is sent. A request that skipped that step is refused, even while armed.",
    ]),
    p(
      "The two are independent on purpose, so either one alone still refuses the delete. A brand-new feature is safe by existing, because it travels through that same guarded connection. Plex is reached through a separate connection with the same two locks, and it holds back even a library refresh, because on some servers a refresh empties the trash.",
    ),

    h2("Judged on frozen, complete evidence", "frozen"),
    p(
      "Everything is gathered and frozen before anything is scored, so no item's fate depends on a source timing out partway through a scan. If a source that could condemn something was unreachable, the scan is marked incomplete. You can look at it, but nothing can be run against it. Acting on half the evidence is how a tool deletes a loved film during an outage.",
    ),
    callout(
      "note",
      "**Not knowing can only ever protect.** A missing rating, a viewer Reaper cannot match, a source that failed, none of them count against a title.",
    ),

    h2("Checked again at the moment of deletion", "at-delete"),
    p(
      "Everything above is reversible; the reap is not. So each file runs one last set of checks, live, right before it goes, and every check keeps the file when it is unsure.",
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
      "**The test file goes first.** The smallest file with a known size goes alone and is verified before any other is touched. If it does not behave exactly as expected, the whole run halts. Files spared or still being watched are skipped first. A later file misbehaving is recorded and the run carries on.",
      "**Removed so it stays gone.** Turn on the import exclusion for a Radarr instance and a movie it removes is kept off your lists, so it will not quietly re-download. It is off until you set it, and you set it per instance. A season is unmonitored first, and Reaper confirms that took before touching a file, so Sonarr will not pull it back.",
      '**The caps hold, while they are on.** A run over its per-run or 30-day limit stops the whole run. It never removes just the part that fits. Switching off "Limit how much each run removes" in Policy leaves the password, your typed confirmation, and every live check standing.',
    ]),

    h2("Grace: a window to catch it before it's gone", "grace"),
    p(
      "When a title is first condemned, a countdown starts, 14 days by default. Turn on the Leaving Soon shelf and Reaper marks it in Plex, so your users can keep a title they still want by simply watching it. A Discord heads-up can go out too.",
    ),
    callout(
      "caution",
      "**The countdown is the time your users have to catch it.** When it ends, the title is simply ready for your next review, and you still start every deletion by hand. To keep a title, watch it or spare it. Both win at the moment of deletion, through the live checks above.",
    ),

    h2("Sharp edges", "edges"),
    ul([
      "**A scripted deploy can start armed.** The password gate covers the switch in the app. Setting `REAPER_DESTRUCTIVE_ACTIONS_ENABLED=true` in the environment turns deletion on at first boot with no password. That is meant for infrastructure-as-code installs.",
      "**That environment setting is the default until you use the switch.** The moment you turn deletion on or off in the app, the app's switch wins for good. To return a running install to read-only, use Policy, Deletion.",
      "**A few read failures keep files, and the scan carries on.** If Reaper cannot list a service's folders, it refuses to match the items it is unsure about, and those files are kept. Not every hiccup shows as an incomplete-scan banner.",
      '**The Leaving Soon shelf is off until you turn it on,** and updating it in Plex needs deletion armed unless you also turn on "Update while read-only" in Settings, Plex. It only reaches people who browse or pinned that library. Wire up the Discord webhook to warn everyone else.',
    ]),
  ],
};
