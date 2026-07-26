// SPDX-License-Identifier: AGPL-3.0-or-later

import { callout, type Doc, h2, ol, p } from "../blocks";

export const arming: Doc = {
  id: "arming",
  group: "Safety",
  title: "Turning deletion on",
  summary: "Arming is a separate, deliberate, password-gated step. Do it last, after everything else is set.",
  body: [
    p(
      "Editing a policy changes nothing about whether Reaper can delete. A fresh install is read-only: it can scan, score, and explain, and nothing else. Turning deletion on is its own act, done in the app with your password. The one exception is a deploy that sets `REAPER_DESTRUCTIVE_ACTIONS_ENABLED=true`, which starts the first boot armed.",
    ),
    callout(
      "note",
      "**Arming and deleting are separate.** Turning the switch on removes nothing. A real run still needs a typed confirmation that matches the exact plan, recomputed on the server.",
    ),
    h2("When you're ready", "steps"),
    ol([
      "Set an admin password in Settings, Security. Arming is refused until one exists.",
      "Set your pace and grace, and turn on the Leaving Soon shelf so your household gets a warning. To see it before you arm anything, also turn on \"Update while read-only\" in Settings, Plex.",
      "In Policy, Deletion, turn deletion on. It asks for your admin password. Turning it back off never does.",
      "Keep your first run supervised, and read the exact list before you confirm the phrase.",
    ]),
    p(
      // "stop", not "abort" -- one word for one mechanism, across the docs and the app (U-15).
      "Even armed, Reaper re-checks safety at the moment of every delete: nothing that is being watched is removed, caps stop a run rather than trimming it while they are on, and the scheduler never deletes. A real reap is always a person typing the phrase.",
    ),
  ],
};
