<!--
  SPDX-License-Identifier: AGPL-3.0-or-later

  The body of the first release. workflows/release.yml ships this file when no
  v* tag exists yet: generated notes over an unbounded range would recite every
  pull request since the repository began. Every later cut generates its notes
  from the pull requests since the previous tag, sectioned by .github/release.yml,
  so this file is dead once v1 exists and safe to delete then.
-->

Reaper finds the media nobody watches in your Plex library, explains why it thinks each item
is expendable, lets you review and approve, then removes approved items through Sonarr and
Radarr and refreshes Plex when it is done.

This is the first release, so instead of a list of changes, here is what Reaper does today.

## 🚀 What it does

- Scans your library and scores every movie, show, and season against your policy, using
  watch history from Tautulli and requests from Seerr.
- Explains every decision: each signal that argued for removal, and each protection that was
  checked and did not fire.
- A review queue where you approve, spare, or whitelist each item before anything happens.
- Leaving Soon: a countdown label in Plex, and an optional Discord notice, so your users can
  object before a title goes.
- An update check in Settings, About that says when a newer release exists.

## 🛡️ How it stays safe

- Deletion ships off. Until you arm it, which asks for your password, Reaper can only scan,
  score, and explain. You can run it for weeks and just read the results.
- It never touches your files directly. Every removal goes through Sonarr and Radarr, so
  Reaper needs no access to your media shares.
- Before anything is removed, Reaper rechecks the plan it showed you, and it rechecks each
  item on the way out. A title someone started watching after you approved it stays. Every
  protection resolves toward keeping the file.

## 📦 Ways to run it

- Docker (amd64 and arm64), Unraid via Community Applications, a Windows installer, a macOS
  app, and a snap.

The manual walks through install and first run: https://scythe-labs.github.io/reaper/
