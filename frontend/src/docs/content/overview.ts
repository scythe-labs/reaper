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
      "Your library grows faster than your disk. Reaper reads your watch history and hands you a reviewed list. Each item carries a plain explanation of why it is a candidate, and every protection Reaper checked. You skim, approve, and it removes the files through Sonarr and Radarr, then refreshes Plex.",
    ),
    callout(
      "tip",
      "**It ships read-only.** A new install can scan, score, and explain, until you arm deletion with a password, or a deploy sets it on at first boot.",
    ),
    h2("Why you can trust it", "trust"),
    ul([
      "**Missing evidence can only ever protect.** Anything Reaper couldn't read adds no pressure to the score, and a protection it couldn't check keeps the file outright.",
      "**Never touches something being watched.** A file in play, or played since you approved, drops off the list.",
      "**Only works through Sonarr and Radarr.** Every removal is a request to one of them.",
      "**Gives you time to change your mind.** Anything on its way out gets a countdown first, and you still start every deletion by hand.",
      "**A reap is a person at the keyboard.** Deleting needs a signed-in browser, deletion armed, and a phrase you type that matches the exact plan. An API key is refused, every time.",
      "**Your keys never come back out.** Every credential is encrypted before it is stored and redacted from the logs, and the API will only ever tell you whether one is set.",
    ]),
    h2("What it connects to", "connections"),
    ul([
      "**Tautulli** tells Reaper who watched what, and when.",
      "**Sonarr** manages your TV shows. Reaper removes them through it.",
      "**Radarr** manages your movies. Reaper removes them through it.",
      "**Seerr** (Overseerr or Jellyseerr) tells Reaper who asked for a title, so you can see it, filter by it, and write a rule that keeps requests.",
      "**Plex** is your library. Reaper reads it, and refreshes it after a cleanup.",
    ]),
    p("Tautulli plus Sonarr or Radarr is enough to start scanning."),
  ],
};
