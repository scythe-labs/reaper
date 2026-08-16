# Collections plan (#816)

> **FROZEN 2026-08-15. Do not update this file; it is history, not state.**
>
> All six phases landed on `dev` as #816: the Plex collections read, the snapshot write, the
> API filter and `verdict=any`, search reaching collection names, the chip and its picker on
> all four surfaces, and the collection screen. Anything below that reads as outstanding work
> describes the moment it was written.
>
> **One of the two measurements the plan called for was taken, on a real library, before this
> merged.** "How many collections a real library has per title" (phase 2) is answered in
> `docs/LEARNINGS.md`: median one, 97% of covered titles in exactly one, and 387 distinct
> collections against tens of genres. It settled two of the open questions below and caught a
> suggestion cap that was half the size a real library needs. The partial-match search query's
> cost (phase 3b) was **not** timed and is still open.

Live. Nothing landed. The design is approved and mocked up; this file is how it gets built.

An operator deciding on one entry in a series wants the same decision, or the opposite one, on
its siblings. Today that means searching each title by name and remembering what the others
scored. This adds a collection chip to a title and a screen behind it that shows every member
whatever fate the scan gave it.

The design decisions and the approved mockups live in
[#816](https://github.com/scythe-labs/reaper/issues/816). This file does not restate them. It
holds the build: what lands in what order, who does it, and what has to be true before each
piece is done.

## The fence

**Collections are navigation, never protection.** No gate, no signal, no policy rule field, no
`FieldSpec`. A collection read that fails costs the chip and nothing else, and it never degrades
a snapshot. That single rule is what keeps this whole feature off the deletion path, and it is
the one thing a reviewer should check first.

It is also why a failed read is not a rule 28 violation: rule 28 binds *evidence* sources, and a
collection is not evidence. The comment at the read site says so, in those words, or a later
pass will read the missing `degrade()` as a bug.

**An operator who wants "protect everything in this collection" already has the Plex collection
keep-list.** That is the protection. Duplicating it here would put the same job on the wrong
side of the fail-closed line, and a failed read would then have to degrade the scan under rules
2 and 93. Any phase that starts reaching toward a policy field has gone wrong.

## One PR, not two

**Rule 25 rules out landing the backend first.** "A DB constraint or schema for an unwired
feature is a blocker, not a placeholder." So the column, the read, the API and the UI land
together on one branch, as one squash-merged PR. Phases below are commits on that branch, not
separate pull requests.

Branch: `feat/collections` cut from `origin/dev`.

## What gets stored

Two additive nullable columns, one Alembic revision chained onto the current head.

| Column | Holds | Read by |
|---|---|---|
| `candidate.collections_json` | this title's collection names, **already sorted** | the chip, the queue filter |
| `snapshot.collection_sizes_json` | each collection's name and its Plex member count | the picker's counts, the header |

`collections_json` mirrors `genres_json` exactly: a JSON array on the candidate, filtered with
`json_each`. A `NULL` reads as "not recorded for this scan," never as "in no collection," and
the UI says so rather than drawing an empty chip.

**The scan writes the array sorted, smallest collection first, ties broken alphabetically.** The
card takes element 0 and no counts ship for the chip itself. Sorting on the read side instead
would need the sizes in the browser for every row; sorting at write time is one `sorted()` and
the tie-break is what stops the chip renaming itself between scans.

`collection_sizes_json` is a small object, tens of entries, and Plex's own count. The number of
members *in this scan* is a different number and comes from the same `json_each` query the
filter uses. Both appear in the header, because a collection can hold titles in an unscanned
library or ones Plex has not matched, and showing only the second number is a lie by omission.

## Phases

Each phase is one commit. A phase is done when its gate passes, not when the code is written.

### 1. The Plex read

Add `PlexClient.list_collections(section_key)` returning each collection's rating key, title and
child count, paged through `_iter_pages` like every other listing (rule 56). `find_collection`
already walks `/library/sections/{key}/collections` and is the sibling to read first: if the two
end up with two paging loops, rule 72 has been broken in the same file.

Membership comes from `collection_children`, which exists. One call per collection per section.

**Done when:** a unit test drives a fake server with two sections, a collection whose listing
pages, and a section with none, and the membership map comes back complete or raises. A read
failure returns an empty map and logs; it does not raise into the scan and does not degrade.

### 2. Into the snapshot

Wire the read into `snapshot_service.scan`, which already receives the connected `plex` client
that the keep-list sync used. Build `dict[rating_key, list[str]]`, sort each list, and write
`collections_json` beside `genres_json` at persist time. Write `collection_sizes_json` on the
snapshot row.

**Done when:** a scan test asserts a candidate's stored array is sorted smallest-first with an
alphabetical tie-break, and a second test asserts a failed Plex collection read leaves the
snapshot un-degraded and every other field intact.

### 3. The API

- `list_candidates` takes `collection`, filtered with `json_each` over `collections_json`.
  **The genre predicate three lines above is the sibling** (rule 72): either both call one
  helper or the new one is a second copy that will drift.
- `list_candidates` accepts `verdict=any`, which is what makes the collection screen cross-lane.
  Rule 23 binds here: enumerate every stored verdict state at every consumer, not just the three
  lanes.
- `collections` joins `CandidateOut`, `CandidateDetail` and `GroupOut`, and rides through to
  `api.ts` (rule 64's supply chain).
- `vocabulary_values` gains `"collection": Candidate.collections_json`. Its `if field ==
  "genre"` branch is the JSON-array arm and now has two members, so it keys off the column
  rather than the field name. Its silent `[:50]` cap is fine for genres and not for
  collections: raise it, or make the picker type-to-search, and say which in the commit.

**Done when:** `test_api_type_mirror.py` passes, a filter test proves a title in three
collections is returned under all three, and a `verdict=any` test proves the page mixes fates.

### 3b. Search reaches collection names

`search` matches the title or the show name today, and understands a trailing release year. It
gains collection names, matched partially, so typing a franchise finds its members.

**The ranking is the hard part, and a relevance score is the wrong answer.** The queue has no
relevance order: search filters and the operator's chosen sort orders. "Titles first,
collections after" is a second ordering, and two orderings cannot both hold. So the server
returns one small rank column and the list renders three blocks, each internally in the
operator's chosen sort:

```
0  exact title match
1  partial title or show match
2  collection-name match          ← a labeled divider sits above this block
```

The divider is not decoration. A search that returned three rows now returns forty because one
collection is named similarly, and on a screen whose job is deciding what to delete, a set that
silently widened reads as a bug.

**A row in block 2 renders the collection that MATCHED, not the smallest one.** Everywhere else
the chip takes element 0; here that would put an unrelated name on a row the operator cannot
otherwise explain. The server says which one matched, and the chip renders it. This is the only
exception to the smallest-first rule, and it is worth a comment saying so at both ends.

**Cost is worth measuring, not assuming.** The genre predicate is an equality inside `json_each`.
A partial is a `LIKE` inside `json_each` across every candidate in the snapshot. Bounded, and
still a scan. Time it on a real library and put the number in `LEARNINGS.md`.

**Done when:** a test proves the three blocks order correctly under each sort key, a test proves
a collection-only match renders the matching name rather than the smallest, and the search
tests that already pin the year parse still pass.

### 4. The chip

One component, `CollectionChip`, rendered in **four** places: the movie card, the show card, the
why panel's `MetaLine`, and the rows inside a collection view. Four call sites, one component,
so a picker fix lands everywhere (rule 18).

Shape is the split control `OverrideControls` already ships for Spare: `.ov-split` /
`.split-main` / `.split-caret`. The name navigates, the caret opens the list. One collection
means no caret.

**The picker cannot be `position: absolute` inside the card.** `.card` sets `overflow: hidden`
for its backdrop art, so an absolute popover is clipped to the card and most of the list is
unreachable. This was caught in the mockup, on the first render. It takes its own clamped
`position: fixed` coordinates, the way `OverrideControls.toggleMenu` does, and it measures
against the viewport before drawing (rule 138).

**Done when:** a test opens the picker on a card and asserts every entry is reachable, and the
axe audit passes on all four surfaces.

### 5. The collection screen

`ReviewQueue` renders a collection instead of a lane when `NavIntent`'s review variant carries a
collection. The lane tabs give way to one back link, which returns to the lane the operator left
with its filters intact. The header carries both counts. The fate summary sits above the list.

**The bulk bar stays off**, because a selection spanning three fates is not one decision, and it
is the one control in the queue that keys on the tab verdict rather than the row's own (rule
48). Rule 62 binds anything that counts.

The search divider from phase 3b renders here too, on the ordinary lanes: one labeled row above
the collection-name block, in the operator's words, naming what they typed.

Everything else keeps working unchanged: the why panel is a sibling keyed on the selected row,
not on the lane, so a row here opens the same panel, and a TV row opens `ShowPanel` with all its
seasons.

**Done when:** the queue's existing tests still pass, a new test drives a collection with mixed
fates and asserts no bulk bar, and `/verify` drives the real app end to end against real data.

### 6. Docs

`STATUS.md` gets the line edited, not appended. `LEARNINGS.md` takes the `overflow: hidden`
clipping finding, which is a fact about this codebase that outlives this feature. This file
moves to `docs/history/` when the PR lands, and `docs/README.md`'s map moves with it.

## The build

An Opus orchestrator holds this file and the branch. Sonnet workers do the phases. Two review
lenses run after each half and the orchestrator adjudicates, because finding is parallel and
cheap while judgment is central and expensive.

```
Phase 1 ─ Phase 2 ─ Phase 3 ──┬─ reaper-review ─┐
        backend, serial       └─ ponytail-review┴─→ orchestrator decides ─ fix workers
                                                                                │
Phase 4 ─ Phase 5 ────────────┬─ reaper-review ─┐                               │
        frontend, serial      └─ ponytail-review┴─→ orchestrator decides ─ fix ─┤
                                                                                │
Phase 6 ─ docs ─ full gates ─ PR ───────────────────────────────────────────────┘
```

**Serial within a lane, because the phases collide on files.** Phases 1 to 3 all touch
`src/reaper/`, phases 4 and 5 both touch `ReviewQueue.tsx`. Running them in parallel buys a few
minutes and costs a merge conflict in the one file the whole feature routes through. The
parallelism that pays is the two review lenses, which are read-only and answer different
questions.

**`reaper-review` ranks by distance from the deletion path. `ponytail-review` only hunts
complexity.** They disagree by design: the first will ask for a guard the second calls
speculative. The orchestrator resolves that, and on this feature the tiebreak is the fence
above. A guard that keeps a file is worth its lines. A guard on a chip is not.

The build ran from a throwaway workflow script held outside the repository, one run per lane.
Twelve agents at most. The script is scaffolding for this feature and is not committed: it would
outlive the plan it reads and name a path that moves to `docs/history/` when this lands.

## What is not decided

- **How many collections a real library has per title.** The chip's caret assumes "usually one,
  sometimes a few." Measured on real data during phase 2, into `LEARNINGS.md`. If the median
  turns out to be five, the picker deserves search and the plan says so here first.
- **Smart collections.** They are collections to Plex and will appear. Whether a 400-title smart
  shelf should be offered at all, or filtered out below some size, is a phase 5 question that
  needs the phase 2 measurement to answer.
- **Where `← Review queue` goes after hopping A to B.** It returns to the queue, and browser
  back walks the chain. If that reads wrong in `/verify`, the fix is a breadcrumb, not history
  surgery.
