// SPDX-License-Identifier: AGPL-3.0-or-later

import { callout, type Doc, h2, p, steps, ul } from "../blocks";

export const plexRebuild: Doc = {
  id: "plex-rebuild",
  group: "Operating",
  title: "Rebuilding a Plex library",
  summary: "A rebuilt library makes every title look unwatched. Here is why, and how to fix it.",
  body: [
    p(
      "Rebuilding or moving a Plex library gives every title in it a new id. Your watch history stays where it is, still filed under the old ids, so Reaper looks up the plays for a title and finds none.",
    ),
    h2("What Reaper does about it", "what-reaper-does"),
    p("Both of these keep your files."),
    ul([
      "**The scan comes back incomplete.** When a large share of the titles Reaper already knew turn up under new ids, it marks the scan incomplete. You can still look at it, but no plan can be built from it.",
      "**Every watched title is held back.** Reaper remembers the most plays it has ever seen for a title, so a count that falls to zero means the plays stopped being readable, not that nobody watched it.",
    ]),
    p("The scan clears itself the next time one runs. The watch history does not."),
    h2("If a few titles moved", "a-few-titles"),
    steps([
      {
        title: "Fix them in Tautulli.",
        text: "Open the item in Tautulli and use Fix Metadata. Nothing else moves old plays onto the new id.",
      },
      {
        title: "Update Reaper's copy of the history.",
        text: "Go to Settings, Jobs and press Run now on Full watch-history update. That re-reads your whole history instead of only the new plays, which is how the corrected ids reach Reaper. It also runs on its own every three days.",
      },
    ]),
    h2("If the whole library was rebuilt", "whole-library"),
    p(
      "Fixing thousands of items one at a time is not realistic, so tell Reaper to forget what it measured instead. Go to Settings, Plex, find Recorded watch history, and press Forget. It asks for your admin password.",
    ),
    callout(
      "caution",
      "**You give something up.** Reaper can no longer tell a title whose plays went missing from one nobody ever watched, so three protections stop holding those titles and the next scan can put them on the list.",
    ),
    p("Scan again afterwards. Reaper starts from what Tautulli can see today."),
  ],
};
