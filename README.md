# Reaper

Explainable media library pruning for Plex.

Reaper finds media nobody watches — things requested and never played, shows whose
old seasons no one returns to, low-rated files quietly eating disk — **explains why
it thinks each item is expendable**, and removes it safely through Sonarr and Radarr.

It integrates with **Tautulli** (watch history), **Sonarr** and **Radarr** (the only
components allowed to delete anything), **Seerr** (requests), and **Plex**.

📖 **[Read the manual](https://scythe-labs.github.io/reaper/)** — install, configuration,
policy tuning, and the safety model in full.

> **Status: in development.** Deletion is implemented and tested, but it ships **off**: a
> new install can only scan, score and explain until you deliberately arm it. Expect rough
> edges, and read the [install guide](https://scythe-labs.github.io/reaper/getting-started/install)
> before you point it at a library you care about.

## Why another one of these?

[Maintainerr](https://github.com/Maintainerr/Maintainerr) and
[Reclaimerr](https://github.com/jessielw/Reclaimerr) already do rule-based Plex
cleanup, and do it well. Reaper's reason to exist is the thing they don't do:

- **A score that shows its work.** Not just *which* rules matched, but every
  protection that was *checked and didn't fire*, with the actual numbers:
  `✓ checked: recently watched — last play 612d ago, your floor is 730d`.
  It explains the **keeps** too, not only the deletes.
- **Curated lists as protection.** Never reap anything in the IMDb Top 250.
  No other tool ingests curated lists as a protection source.
- **A countdown your users can see**, surfaced as a *Leaving Soon* label in Plex.
  Watching a title keeps it; so does sparing it by hand.

## Safety

Reaper deletes irreplaceable data from a server other people depend on.
**Every ambiguity resolves toward keeping the file.**

- **Off until you turn it on.** A new install starts read-only: it can scan, score and
  explain, and nothing else. The refusal lives at the HTTP transport, not in scattered
  `if dry_run:` checks — while deletion is off, a mutating request is blocked *before it
  is sent*, whatever the calling code believes it is doing.
- **Unknown never condemns.** A missing rating, an unmappable user, or a degraded
  data source can only ever *protect* an item. This is enforced by the type system.
- **Nothing is deleted while it is being streamed.** The active-session veto is
  re-checked immediately before every single delete.
- **Reaper only acts through Sonarr and Radarr.** It has no filesystem delete path.
  Media that no *arr manages cannot be deleted, only reported.

> **The Plex token Reaper stores grants full administrative control of your Plex
> account, including permanent deletion. Treat Reaper's database as equivalent to your
> Plex password.** It is encrypted at rest and redacted from logs, but a full-power
> credential cannot be made into a narrow one.

[How a delete is kept safe](https://scythe-labs.github.io/reaper/safety/how-a-delete-is-kept-safe)
walks the whole path through.

## Running it

Reaper ships as **one container**, and it needs no access to your media. The only volume
is its own small database.

```bash
docker compose up -d                # serves on http://localhost:8420
```

Use **`:dev`** for now: it tracks the `dev` branch, where the work lands. `:latest` follows
`main` once a release is cut.

Then follow **[First run](https://scythe-labs.github.io/reaper/getting-started/first-run)**,
in order. The first account to sign in claims the install, so it matters who does it.

Unraid, file ownership, environment variables, the encryption key and recovery are all in
the manual.

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
