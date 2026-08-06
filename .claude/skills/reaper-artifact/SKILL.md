---
name: reaper-artifact
description: Build an HTML mockup or artifact in Reaper's look and feel, pulling the real, current tokens and component styles from source so it matches the app without re-researching them. Load before any UI-touching change (the CLAUDE.md mock-up rule) and whenever asked for a Reaper-styled artifact.
---

# Reaper's look and feel for an artifact

Load this before you mock up UI, so you do not re-derive Reaper's design language every time.
It tells you where the live tokens and component styles are, then hands off to the general
`artifact-design` skill for layout and the publish flow. Do not type token values from memory;
read them from the files named here, so the artifact tracks the app instead of a snapshot of it.

## Get the stylesheet, one of two ways

**Whole grammar (default).** Reuse the app's own CSS, live from source. Read
`frontend/src/index.css`, follow its `@import "./..."` list in order, read each of those files,
concatenate them, and inline the result in one `<style>`. The import order is the cascade and is
load-bearing, so keep it. This gives you cards, chips, buttons, and the verdict language exactly
as the app paints them.

**Tokens only (lighter).** For a simple artifact, inline just the design tokens: copy the
`:root`, the `@media (prefers-color-scheme: dark)`, and both `:root[data-theme=...]` blocks from
`frontend/src/styles/00-tokens.css` verbatim, then build with the variables below.

Both stay current because you read the current file. Never hand-type a hex value.

## The variables you will reach for

The verdict colors carry meaning the copy would otherwise have to repeat. Use them, do not
reinvent them:

- `--condemn`, `--condemn-soft`: red. Expendable, the delete side.
- `--protect`, `--protect-soft`: green. Kept, a protection that holds.
- `--abstain`: gray. Undecided.
- `--unknown`, `--unknown-soft`: amber. "Could not be checked," which must never look like
  "checked and fine." This distinction is the whole reason the palette exists.
- `--accent`, `--accent-soft`, `--accent-text`: the operator's sky-blue tint. `--accent-text`
  is the AA-safe ink for accent-colored text.
- Neutrals: `--bg`, `--surface`, `--surface-2`, `--border`, `--text`, `--muted`. Scales:
  `--text-*`, `--space-*`, `--radius-*`, `--shadow-*`.

Both themes are defined in the token file, so an artifact built on these variables themes
itself. Style both, and let `@media (prefers-color-scheme: dark)` and `:root[data-theme=...]`
win in both directions, the way the app does.

## Copy grammar (rule 21)

Operator copy is scanned while deciding what to delete, not read twice. Lead with the outcome. A
phrase over a sentence.

- Plain language only: no rating keys, no tmdb/imdb/tvdb ids, no internal jargon (guard,
  coverage, abstain-as-a-word).
- No em dashes. Reword with a period, a comma, or a colon.
- No middot or dash separating two facts in running text: a screen reader voices it or drops it,
  so the separator is a comma. A middot that is pure decoration carries `aria-hidden`.
- Arrows ("Policy → Deletion") are fine.

## Then

Hand off to `artifact-design` for design investment, layout, and the publish flow. This skill is
only Reaper's look; that one is how to build a good artifact. The `Artifact` tool already
requires it before publishing.

## What keeps this current

`tests/test_repo_hygiene.py` fails if a file named here goes missing or a variable named above
leaves `00-tokens.css`. The tokens moved once already (from `index.css` to
`styles/00-tokens.css`), so this guard is why a rename breaks the test, not an operator's mockup.
