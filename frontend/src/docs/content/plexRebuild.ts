// SPDX-License-Identifier: AGPL-3.0-or-later

import { callout, type Doc, h2, p, steps, ul } from "../blocks";

export const plexRebuild: Doc = {
  id: "plex-rebuild",
  group: "Operating",
  title: "Rebuilding a Plex library",
  summary:
    "Rebuilding your library makes every title look unwatched. This page explains why that happens and how you can fix it.",
  body: [
    p(
      "If you rebuild or move a Plex library, every title gets a new id. Your watch history stays where it is under the old ids, so Reaper looks up the plays for a title and finds none.",
    ),
    h2("What Reaper does about it", "what-reaper-does"),
    p("Both of these keep your files."),
    ul([
      "**The scan comes back incomplete.** Reaper marks a scan incomplete if a large share of the titles it already knew show up under new ids. You can still look at it, but you can't build a plan from it.",
      "**Every watched title is held back.** Reaper tracks the highest play count it's ever seen for a title. If that count drops to zero, it means the plays stopped being readable rather than nobody watching it.",
    ]),
    p("The scan clears itself the next time you run it, but your watch history stays."),
    h2("If a few titles moved", "a-few-titles"),
    steps([
      {
        title: "Fix them in Tautulli.",
        text: "Open the item in Tautulli and use Fix Metadata. This is the only way to move old plays onto the new id.",
      },
      {
        title: "Update Reaper's copy of the history.",
        text: "Go to Settings, then Jobs and press Run now on Full watch-history update. This re-reads your whole history instead of only new plays so the corrected ids reach Reaper. It also runs on its own every three days.",
      },
    ]),
    h2("If the whole library was rebuilt", "whole-library"),
    p(
      "Fixing thousands of items one at a time isn't realistic, so use this path to clear everything at once. Go to Settings, then Plex, find Recorded watch history, and press Forget. It'll ask for your admin password.",
    ),
    callout(
      "caution",
      "**When you press Forget, you give something up.** Reaper keeps a record of the most watch history it has ever seen for each title, and forgetting all the history throws it away. Without it, a title whose plays went unreadable reads exactly like one nobody ever watched. The protections that were holding those titles stop, and the next scan can flag them for removal. Read that scan carefully before you approve anything.",
    ),
    p("Run a scan again afterward. Reaper starts from what Tautulli can see today."),
  ],
};
