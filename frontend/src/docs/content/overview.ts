// SPDX-License-Identifier: AGPL-3.0-or-later

import { callout, type Doc, h2, p, ul } from "../blocks";

export const overview: Doc = {
  id: "overview",
  group: "Getting started",
  title: "What Reaper does",
  summary:
    "Reaper finds the movies and shows nobody watches, explains why, and removes them safely once you approve.",
  body: [
    p(
      "Your library grows faster than your disk. Reaper reads your watch history and gives you a reviewed list. Each item has a plain explanation of why it's a candidate and every protection Reaper checked. You skim, approve, and it removes the files through Sonarr and Radarr, then refreshes Plex.",
    ),
    callout(
      "tip",
      "**It ships read-only.** A new install can scan, score, and explain until you arm deletion with a password or a deploy sets it on at first boot.",
    ),
    h2("Why you can trust it", "trust"),
    ul([
      "**Missing evidence can only ever protect.** If Reaper couldn't read something, it adds no pressure to the score. If it couldn't check a protection, it keeps the file outright.",
      "**Never touches something being watched.** If a file is currently in play or has been played since you approved it, it drops off the list.",
      "**Only works through Sonarr and Radarr.** Every removal is a request sent to one of them.",
      "**Gives you time to change your mind.** Anything on its way out gets a countdown first. You still start every deletion by hand.",
      "**A reap is a person at the keyboard.** To delete, you need a signed-in browser, deletion armed, and a phrase you type that matches the exact plan. An API key is refused every time.",
      "**Your keys never come back out.** Reaper encrypts every credential before storing it and redacts it from the logs. The API only tells you whether a credential is set.",
    ]),
    h2("What it connects to", "connections"),
    ul([
      "**Tautulli** tells Reaper who watched what, and when.",
      "**Sonarr** manages your TV shows, and Reaper removes them through it.",
      "**Radarr** manages your movies, and Reaper removes them through it.",
      "**Seerr** (Overseerr or Jellyseerr) tells Reaper who requested a title. You can see the requester, filter by them, or write a rule to keep their requests.",
      "**Plex** is your library. Reaper reads it and refreshes it after you clean up.",
    ]),
    p("Tautulli plus Sonarr or Radarr is enough to start scanning."),
  ],
};
