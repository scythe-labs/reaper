// SPDX-License-Identifier: AGPL-3.0-or-later

import { callout, type Doc, h2, ol, p } from "../blocks";

export const arming: Doc = {
  id: "arming",
  group: "Safety",
  title: "Turning deletion on",
  summary:
    "Arming is a deliberate, password-gated step. Do this last, after everything else is set.",
  body: [
    p(
      "Editing a policy won't change whether Reaper can delete your files. A fresh install is read-only. It can scan, score, and explain, but nothing else. You turn deletion on within the app using your password. The only exception is a deploy that sets `REAPER_DESTRUCTIVE_ACTIONS_ENABLED=true`, which starts the first boot armed.",
    ),
    callout(
      "note",
      "**Arming and deleting are separate.** Turning the switch on removes nothing. A real run still needs you to type a confirmation phrase that's recomputed on the server to match your exact plan.",
    ),
    h2("When you're ready", "steps"),
    ol([
      'Set your pace and your grace, then turn on the Leaving Soon shelf so your users get a warning. You should also turn on "Update while read-only" in Settings, Plex to see the shelf before you arm anything.',
      "Go to Policy, Deletion and turn deletion on. It'll ask for the admin password you set during your first run. You can turn it back off whenever you want.",
      "Watch your first run yourself and read the exact list before you confirm the phrase.",
    ]),
    p(
      // "stop", not "abort" -- one word for one mechanism, across the docs and the app (U-15).
      "Even when you have it armed, Reaper checks everything again right before it deletes. It won't remove anything that's currently being watched, and it'll stop a run instead of trimming it if a cap is active. The scheduler never deletes on its own either. A real reap only happens when you type the phrase.",
    ),
  ],
};
