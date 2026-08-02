<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="docs/media/wordmark-dark.svg">
    <img src="docs/media/wordmark-light.svg" alt="Reaper" width="360">
  </picture>
</p>

<p align="center">
  <a href="https://github.com/scythe-labs/reaper/actions/workflows/ci.yml"><img alt="CI" src="https://github.com/scythe-labs/reaper/actions/workflows/ci.yml/badge.svg?branch=dev"></a>
  <a href="LICENSE"><img alt="License: AGPL-3.0-or-later" src="https://img.shields.io/badge/license-AGPL--3.0--or--later-blue"></a>
  <img alt="Python 3.13" src="https://img.shields.io/badge/python-3.13-blue">
  <img alt="React 19" src="https://img.shields.io/badge/react-19-blue">
</p>

<p align="center"><em>Grave decisions, clearly explained.</em></p>

**Reaper** finds media nobody watches: things requested and never played, shows whose old
seasons no one returns to, low-rated files quietly eating disk. It **explains why it thinks
each item is expendable**, and removes it safely through
[Sonarr](https://sonarr.tv) and [Radarr](https://radarr.video). It reads watch history from
[Tautulli](https://tautulli.com), requests from [Seerr](https://github.com/seerr-team/seerr),
and refreshes [Plex](https://plex.tv) when it is done.

📖 **[Read the manual](https://scythe-labs.github.io/reaper/)** for install, configuration,
policy tuning, and the safety model in full.

> **Status: in development.** Deletion is implemented and tested, but it ships **off**: a
> new install can only scan, score and explain until you deliberately arm it. Expect rough
> edges, and read the [install guide](https://scythe-labs.github.io/reaper/getting-started/install)
> before you point it at a library you care about.

## Current features

- **Per-season TV decisions**, never a whole show in one action. It keeps the newest seasons,
  the first season, specials, and the season each viewer is partway through, moving that hold
  forward the moment they finish one. Ask it to *also keep N seasons ahead* and every binge
  carries a cushion in front of it, so someone on season 1 keeps what they are about to reach.
- **A score that shows its work.** Not just *which* rules matched, but every protection that
  was *checked and didn't fire*, with the actual numbers:
  `✓ checked: recently watched, last play 612d ago, your floor is 730d`. It explains the
  **keeps** as well as the deletes.
- **Curated lists as protection.** Never reap anything in the IMDb Top 250.
- **Keep tags**, in Sonarr and Radarr or as a Plex collection you curate from your phone.
- **A countdown your users can see**, surfaced as a *Leaving Soon* collection and label in
  Plex. Watching a title keeps it; so does sparing it by hand.
- **A simulator** that re-decides your last scan under a draft policy, with no calls to your
  services, so you can move a number and watch the outcome change.
- **A test file first.** The smallest item goes alone and is verified before anything else is
  touched. If it misbehaves, the run halts.
- **Sensible defaults, and deletion logic pinned by tests.** Every shipped default
  errs toward keeping. The scoring and the protections are pinned by tests that fail the moment
  a verdict changes, and the season rules, the caps and the live checks each have their own.
- **One container**, no access to your media, and one small volume.

[What makes it different](https://scythe-labs.github.io/reaper/features) covers these in full,
including the ones that ship switched off.

## Getting started

Check out the documentation for instructions on how to install and run Reaper:

https://scythe-labs.github.io/reaper/getting-started/install

## Preview

![The review queue, with each candidate's score beside it and a panel breaking down why one
of them scored 91](docs/media/review-queue.png)

## Safety

Reaper deletes irreplaceable data from a server other people depend on.
**Every ambiguity resolves toward keeping the file.**

- **Off until you turn it on.** A new install starts read-only: it can scan, score and
  explain, and nothing else. The refusal lives at the HTTP transport: while deletion is
  off, a mutating request is blocked *before it is sent*, whatever the calling code
  believes it is doing.
- **Unknown never condemns.** A missing rating, an unmappable user, or a degraded
  data source can only ever *protect* an item. This is enforced by the type system.
- **Nothing is deleted while it is being streamed.** The active-session veto is
  re-checked immediately before every single delete.
- **Reaper only acts through Sonarr and Radarr.** It has no filesystem delete path.
  Media that no *arr manages cannot be deleted, only reported.

> **The Plex token Reaper stores grants full administrative control of your Plex
> account, including permanent deletion. Treat Reaper's database as equivalent to your
> Plex password.** It is encrypted at rest and redacted from logs.

[How a delete is kept safe](https://scythe-labs.github.io/reaper/safety/how-a-delete-is-kept-safe)
walks the whole path through.

## How Reaper is built

This is a hobby project, and you should know that before you point it at a library you
care about.

I work professionally in tech and have written Python for more than ten years. Software
engineering is not my job title, and Reaper exists because I wanted it to exist and had
the evenings to build it.

A large share of this codebase was written with AI assistance. I direct that work, read
what comes back, and decide what ships. The architecture, the safety model, and the
standard for what is good enough here are mine. The engineering rules the project
follows grew out of exactly this: they are the written-down result of reviewing that
output and finding every way it went wrong.

Two reasons to say so plainly. You are trusting this program with files you cannot get
back, so how it was made is your business. And it sets a fair expectation if you open a
pull request: I read what lands here and I can answer for it, and I am learning in the
open, so a well-argued disagreement is welcome and is often right.

## Contributing

Setup instructions, the verification gates, commit conventions, and the AI policy are in
[CONTRIBUTING.md](CONTRIBUTING.md).

Bug reports and questions are welcome:
[open an issue](https://github.com/scythe-labs/reaper/issues/new/choose) or start a
[discussion](https://github.com/scythe-labs/reaper/discussions). Security problems go
through [SECURITY.md](SECURITY.md), which opens a private report.

The manual's pages live in [`manual/`](manual/); the ones under Policy and Safety are
**generated** from the app's own help pages, so edit those in
[`frontend/src/docs/content/`](frontend/src/docs/content/) and run
`npm --prefix frontend run gen-manual`.

## License

AGPL-3.0-or-later.
