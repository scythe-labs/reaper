# Reaper — entry point for AI agents

Reaper is a self-hosted web tool that finds media nobody watches, explains why it thinks
each item is expendable, lets an operator review and approve, then removes it through
Sonarr and Radarr. Python 3.13 and FastAPI on the backend, React 19 and Vite on the front,
shipped as one container.

> **Prime directive: Reaper deletes irreplaceable data from a server other people depend
> on. Every ambiguity resolves toward keeping the file.** When in doubt, fail closed.

## Read these two, in this order

**[CONTRIBUTING.md](CONTRIBUTING.md)** holds the conventions, and they apply to you exactly
as they apply to a human contributor: how the project is set up, the verification gates and
how to read their exit codes, commit and pull request conventions, how operator-facing text
is written, and the AI policy. Nothing in this file restates it.

**[CLAUDE.md](CLAUDE.md)** holds the working instructions for this repository: the
architecture, the two-layer safety model, the documentation discipline, and the numbered
engineering rules that the code cites by number. Read it before you change anything. It is
named for the tool it was first written for, and it is not specific to that tool.

The numbered rules live in `.claude/rules/`, scoped so that a set loads when you open a file
it governs. CLAUDE.md's table says which file governs what. Read the governing file before
working in a tree you have not touched yet.

## The four things most likely to trip you

Rule 7/24: a comment may not claim a safeguard that isn't implemented. Rule 72: fix every
sibling of what you fixed, in the same change. Rule 134: a gate is judged by its exit code,
never the output you kept. The nothing-identifying golden rule: no real titles, hosts, paths,
usernames, or stats, anywhere in the tree. CLAUDE.md carries all four in full.

## Ground truth

`docs/STATUS.md` is what is true right now, and it is the place to start. `docs/DECISIONS.md`
says why each locked decision is what it is, and several were reversed once already, which is
the part a future reader needs most.
