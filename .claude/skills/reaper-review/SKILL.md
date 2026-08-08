---
name: reaper-review
description: Review Reaper code for defects, ranked by how close they sit to the deletion path. Use for any code review of this repo — a diff, a lane (safety/seam/backend/frontend), or a whole-tree pass before promoting dev to main.
---

# Reviewing Reaper

**The prime directive is the ranking function.** Reaper deletes irreplaceable data from
someone else's server. A defect matters in proportion to how close it sits to a file being
removed, not to how it would score on a generic severity rubric. A crash in a settings form
is an annoyance; a protection that evaluates to "nothing configured" is the worst outcome
this codebase has, and it is usually silent.

## Argument

The argument may name a lane from the table below, optionally followed by a target — a commit,
a branch, or paths. **Both parts are optional, and the lane normally should be.** With no lane,
derive it from the diff (see *Selecting the lane*); with no target, review the working diff.
Naming a lane explicitly overrides the derivation and runs only that lane. Positional `$1`
substitution is deliberately not used here, because it does not reliably resolve to the first
word of a multi-word argument.

Adding `fix` anywhere in the argument (`/reaper-review fix`, `/reaper-review safety fix`) means
apply the findings after reporting them — see *Applying the fixes* below.

## Selecting the lane

**Derive it; do not ask.** The changed paths determine the lanes mechanically, so read the diff
first (`git diff --name-only`, or `git diff --name-only dev...HEAD` on a branch) and match:

| A changed path matching… | fires |
| --- | --- |
| `src/reaper/engine/{gates,verdict,signals,policy}.py`, `src/reaper/services/{executor,planner,snapshot,season_pruning}.py`, `src/reaper/clients/{base,plex}.py`, `src/reaper/api/runs.py`, `frontend/src/components/{DeletionToggle,ReapConfirm}.tsx`, `frontend/src/useSafety.ts` | `safety` |
| `src/reaper/api/*.py` (any router or `schemas.py`), `frontend/src/api.ts`, or a changed component prop/query key | `seam` |
| anything else under `src/reaper/**`, `alembic/**`, or `frontend/src/**` | `diff` |

**The lanes are additive and run as separate passes.** A change touching the executor and
adding a route fires `safety`, `seam`, and `diff` — three passes, because they are three
different questions and a reviewer holding all three at once checks each side and skips the
contract between them, which is the exact blind spot `seam` exists to close. The extra cost
lands only on changes that genuinely cross the risk boundaries, which are the changes that
warrant it.

Run them concurrently, as separate subagents, not as one widened prompt.

**Say which lanes fired and why**, in one line, before reporting findings: `safety (executor.py),
seam (api/runs.py, api.ts) — 2 lanes`. If the operator disagrees they can name a lane explicitly
next time, and a wrong derivation is then visible instead of silent.

A diff touching only docs, tests, or config fires nothing. Say so and stop rather than inventing
a pass.

## The lanes

| Lane | Scope |
| --- | --- |
| `diff` | the changed lines themselves, and what they reach. The baseline pass — it runs for any code change, alongside whatever else fired. |
| `safety` | `engine/{gates,verdict,signals,policy}.py`, `services/{executor,planner,snapshot,season_pruning}.py`, both transport guards (`clients/base.py`'s `GuardedTransport` and its `GuardedSession` sibling in `clients/plex.py` — rule 72 means a fix to one is reviewed against any other), the execute route at `api/runs.py:399`, and the arming UI. Spans both trees on purpose. |
| `seam` | route ↔ `api/schemas.py` ↔ frontend client method ↔ props ↔ query keys, for every surface the change touches. |
| `backend` | `src/reaper/**`, `alembic/**` |
| `frontend` | `frontend/src/**` |

`backend` and `frontend` are whole-tree passes. They are expensive and belong on a cadence —
before a `dev` → `main` promotion — not on a normal change.

For the deep pre-promotion pass on the deletion path, run the saved workflow rather than doing
it by hand: `Workflow({name: 'reaper-safety-review'})`. It fans out 7 reviewers over the safety
files and verifies only the tier 1–2 candidates, which holds it near 16–19 agents. The first,
uncapped version of that same pass spawned 43, because every candidate got a verifier and
nothing bounded the second stage. **Ask before running it** — it is a large, billable fan-out,
and a normal change never needs it.

**Lanes are not a partition of the files, they are a partition of the *risk*.** Reviewing
backend and frontend separately leaves the seam unreviewed by construction: the confirmation
phrase rendered client-side against the one recomputed server-side, query-key invalidation
after execute, a route returning a shape the SPA misreads. Each single-tree reviewer sees half
of that and neither is wrong to skip it. That is why `seam` is a lane the diff can fire on its
own, rather than something anyone has to remember to ask for.

## Before you look for anything

**Grep `references/refuted.md` per candidate, before you report it — do not read the file.**
It indexes candidates a previous pass raised and an independent verifier killed, keyed on the
same fingerprint as an issue (`<path>:<symbol> — <slug>`). Nobody holds that many refutations in
mind while reviewing, and the check only matters once you have something to report, so run it
then, once per candidate, against the path and the symbol you are about to name:

```
grep -in 'season_pruning\|plan_series_prune' .claude/skills/reaper-review/references/refuted.md
```

A hit means read the row and either accept it or beat it — re-raising one costs a full verify
cycle and returns nothing. No hit means report. Each row records the commit it was refuted at:
if the cited code has changed since, the refutation is stale and the candidate is live again.
Two sections after the index are *not* refutations and bind nothing — the file says which.

**Up front, list the open questions: `gh issue list --label "Status/Need More Info"`.** Those are the
other outcome — candidates a previous pass raised and could not prove, so they were filed as
questions rather than as defects. Each body carries a *What would settle it* section naming the
evidence it wants. If this pass can supply that evidence cheaply, do: settling one is worth more
than a fresh tier-4 finding, and it costs one label edit either way. Same staleness rule as
`refuted.md` — each is pinned to the commit it was raised at, so a changed citation means
re-derive rather than trust it.

**Do not restate the numbered rules.** `.claude/rules/*.md` holds them, distilled from
adversarial passes, and they load automatically when you read a file they govern — reading
`src/reaper/engine/gates.py` pulls in `backend.md` before you can edit it. They are already in
your context. Never paste them into a prompt, never re-derive them, and never close a review
with a summary of them. **Cite by number** (`violates rule 93`) — that is the whole point of
the numbers being permanent.

## What counts as a finding

Report a defect only when you can state the concrete trigger: the input or state that produces
the wrong output, and what the operator loses. "This could be fragile" is not a finding.

Rank by blast radius, in this order:

1. **A protection that cannot fire, or fires and does not protect** — the silent class. Rules
   38/117, 93, 105, 115 all exist because a guard read as live while covering nothing.
2. **A path that widens what gets deleted** — fail-open on a read error, an empty selection
   expanding to everything, a cap or count computed over a different set than the one acted on.
3. **Loss of the audit trail or the operator's ability to intervene** — a journal write that is
   not durable, a spare that arrives too late to matter.
4. **Everything else** — correctness, security, performance, production readiness.

Within a lane, order findings by that scale, not by a critical/high/medium label alone.

Also sweep for, and fold into the ranking above: hacks and workarounds (`TODO`, `FIXME`,
`temp`, `workaround`, and comments claiming a safeguard — rule 7/24 makes an uncited safety
claim a blocker in itself), missing error handling that fails silently, hardcoded values that
belong in config, and duplication that has already drifted between its copies. Flag a refactor
only when a real defect lives in the duplication; "this could be tidier" is noise.

## Verify before reporting

Every candidate gets an adversarial pass whose job is to **refute** it: open the cited lines,
grep the real call sites, and check that the trigger can actually occur. Default to refuted
when uncertain. This pass has historically killed roughly one candidate in five — the whole
value of the review is that what survives is real.

Correct the line numbers during verification. A finding with a stale line reference costs the
fixing agent a search and earns a plausible wrong edit.

## Output

Report findings with the **`ReportFindings` tool**, most severe first. Do not also write them
out as prose, and do not produce a standalone markdown report — the fixing agent consumes the
structured list directly, and a document in between costs a full write and a full read to
convey the same thing.

The one exception is a whole-tree `backend` or `frontend` pass, which is an event worth
archiving: those land in `docs/history/` under the freeze banner that directory requires.

Two things go in the working tree, not the report:

- **Every refuted candidate** appends to `references/refuted.md`, with the commit it was
  refuted at. This is the only reason the next pass does not re-raise it.
- **A finding that represents a *class* the numbered rules do not cover** proposes exactly one
  new rule, appended to the scoped file in `.claude/rules/` that governs it and numbered from
  where `CLAUDE.md`'s index says the list continues. One rule, in the file, in the same change.
  Do not emit a list of "agent rules" — that duplicates `.claude/rules/` and puts pressure on
  numbers that are permanent.

## Applying the fixes

Only when the argument said `fix`. Report first, then apply — never silently.

Apply only findings whose verdict is CONFIRMED, and only where the fix is the one the finding
names. A PLAUSIBLE finding is not edited on: its trigger was never proven, so touching the
deletion path on the strength of it trades a hypothetical bug for a real diff. It is filed as a
question instead, per *Opening issues*. Say which ones you skipped and why.

Run the gates the change touches before you hand back — for backend edits at minimum
`uv run ruff format .`, `uv run ruff check .`, `uv run mypy src/reaper tests/`, and the test files
covering what you changed. A fix that breaks a test means the finding was wrong about the
mechanism: return the corrected variant, or withdraw the finding to `references/refuted.md`.

Commit each fix as its own story, with the test that pins it, per `CLAUDE.md`.

## Opening issues

Every finding this run does not fix goes to the tracker, so a session that dies does not take
the work with it — confirmed ones as defects, unproven ones as questions, and nothing to a
reference file. GitHub, via `gh`. The remote used to be a private forge that `gh` could not
reach, which is why this section once banned it and why `/code-review --comment` was unusable;
both now work, so a finding can also land as an inline PR comment where that fits better than
an issue.

**One issue per fix, not per finding.** This is `CLAUDE.md`'s commit rule pointed at the
tracker: one commit tells one story, so one issue describes one commit. If two findings would
be closed by the same edit, they are one issue.

**A class is what one edit closes, not what shares a subject.** That test is the whole boundary,
and it cuts both ways. The screen-reader findings of #169–#176 are one *theme* and eight
different classes — a card's name, a live region, focus containment, a menu's keyboard path —
each needing its own edit, so eight issues was right. #203 and #204 are the opposite: one
mechanism, a cached value used as the input to a write or a gate, filed as strangers.

**Sweep the class before you file, so the issue states the extent and not just the first site
you hit.** Rule 72 binds a *fix* to every sibling; this binds the *filing* the same way, because
a defect found at one site is a question about the tree. Grep for the rest before writing the
body. The bar is thoroughness, not perfection: a later pass may legitimately find a site the
sweep missed, and raising it is right. What is being avoided is the *unswept* file, where a
class is rediscovered a site at a time because nobody looked.

**But the class is the unit of knowledge, and the issue is still the unit of work.** Measured on
this tracker: issues citing one or two sites close 78% of the time, median 2.9 hours; issues
citing five or more close 52%, median 10.6. #190 is the warning, because it is exactly the sweep
this section asks for — twenty sites, filed as one issue — and it is the oldest still-open issue
in its class. **An issue nobody closes has not fixed the problem entirely, which was the whole
point of sweeping.** So a class stays one issue only while one commit can close it. Past about
eight sites, or across two `Kind/` values or two `Priority/` tiers, it is filed as several
closable issues that share one `class:` line. One issue carries one `Kind/` and one `Priority/`,
so folding a `Security`/`High` site into a `Bug`/`Medium` class issue drops it out of the filter
the backlog is triaged from — the same failure as not filing it.

**A later pass that finds a new site** appends it to the class issue when that issue is open and
the new site does not outrank its `Priority/`. When the issue is closed, file a new one carrying
the same `class:` line and citing it — a closed issue is read by nobody. When the new site
outranks the class, it is its own issue at its own priority.

The other measured over-split is **#140 and #166** — the same two-branch split on the same shared
component, filed three hours apart. Like #203 and #204, it went uncaught because the duplicate
check ran on site-shaped fingerprints.

Two things are *not* over-splitting, and a sweep must not force them together. Findings that
share a subject but need different edits are different classes, however similar they read. And a
finding that did not exist until an earlier fix shipped could not have been swept for: #196's
false premise entered the tree in the very commit that fixed #166, and #195 exists only because
#153 removed a clause from the shared notice and left its copies behind. A later pass finding
those is the process working, not failing.

| Findings | Grouping |
| --- | --- |
| one class, up to ~8 sites, one `Kind/` and one `Priority/`, one verdict | **one** issue; sites are checklist rows under *Where* |
| one class, but a site outranks the others, or takes a different `Kind/`, or is undemonstrated while they are proven | that site is **its own** issue at its own labels, carrying the same `class:` line |
| one class, more than ~8 sites | a tracking issue that **lists** per-file issues, never one issue that **is** them — past eight nobody closes it |
| siblings under rule 72 (the same defect in sibling functions) | **one** issue, because the rule requires them fixed together |
| tier 1–2, each a genuinely distinct class | one issue each — each earns its own commit, and each is individually worth doing |
| tier 3–4 | grouped by shared root cause, or by shared file and theme; the body lists each as a checklist row |

**When two rows disagree, the one that keeps the issue closable governs, and `Priority/` is never
averaged down to fit.** Splitting a class costs a `class:` line; burying a `Critical` inside a
cosmetic bundle costs the file.

**An unproven candidate is filed too, as a question rather than as a defect.** It does not go
in a reference file and it does not stay in `.claude/review-findings/`, which is gitignored
interim scratch that dies with the worktree. The labels carry the distinction, so nothing has to
be read to see it: an issue asserts a defect exists, so one whose trigger a verifier declined to
demonstrate takes `Status/Need More Info` and **no `Reviewed/` label at all**.

| Verdict | Third label | The tracker reads it as |
| --- | --- | --- |
| CONFIRMED | `Reviewed/Confirmed` | a defect exists — fix it |
| PLAUSIBLE, or never verified | `Status/Need More Info` | a trigger nobody has shown — prove it or kill it |

An unproven body carries one extra section, above the fingerprint line:

```
**What would settle it.** The test to write, the call site to grep, the journey to drive.
```

Write it for someone who has never seen the finding, because they are the only one who will read
it. "Needs verification" settles nothing and leaves the issue open forever.

It leaves that state in one command, which is the whole reason it is an issue and not prose:

- **Proven.** `gh issue edit <n> --add-label "Reviewed/Confirmed" --remove-label "Status/Need
  More Info"`. One call does both, verified against this tracker; the old forge's client needed
  two, because there an add silently beat a remove in the same invocation.
  Nothing is re-filed; the issue keeps its number, its body, and its history. **Promoting it
  edits the body too, naming what settled it** — the test that was written, the journey that
  was driven — and strikes the sentence that said nobody had. A body still reading "was not
  demonstrated" under a `Reviewed/Confirmed` label is a false statement about the work, which
  is rule 134's standard pointed at the tracker. Two issues promoted in the same second were
  not both demonstrated.
- **Refuted, or stale because the cited code moved.** `gh issue edit <n> --add-label
  "Reviewed/Invalid"`, then `gh issue close <n>`, then append the reasoning to
  `references/refuted.md` naming the issue number. Leave `Status/Need More Info` on it as the
  record of how it arrived; the list above filters open issues, so a closed one drops out on its
  own. That file, not the closed issue, is what stops the next pass re-raising it — a pass reads
  `refuted.md` before it looks at anything and never reads closed issues.

**Cap at 8 confirmed issues per session, and 4 unproven ones** — a session, not a review run,
because most findings arrive while building something else, and a cap binding only
`/reaper-review` does not bind where the volume comes from. Unproven is lower because such an
issue asserts less and still costs someone a judgment call to close. Past either cap the
remainder goes
into a single tracking issue that lists them all, labeled like the ones it carries, so nothing is
lost and the tracker is not flooded. Say in the run summary how many were rolled up — a cap that
hides what it dropped reads as "that was everything."

**The cap merges by class, never by coincidence.** Sites of one defect belong in one issue
however many there are, and the cap is no reason to put two *unrelated* defects in one — a
bundle nobody can close in one commit hides its contents from every filter. So the remainder
goes into a tracking issue that *lists* them, never into a single issue that *is* them. If the
cap is biting, the usual cause is a class that was filed a site at a time; merge it and the
count falls on its own.

**Check for duplicates first** (`gh issue list --state all --search "<class-slug>"`, which
searches bodies and so matches the fingerprint below). Re-running a review must not
re-file what is already open. Each body carries a stable fingerprint line to match on:

```
finding: <path>:<symbol> — <short-slug>
class:   <mechanism-slug>
```

Match on those, never on the line number, which drifts with every edit above it. `finding:`
identifies this issue; **`class:` names the mechanism and is what makes the duplicate check
work across sites** — grep it first, and reuse an existing class slug verbatim rather than
coining a synonym.

A site-shaped fingerprint alone is why #203 and #204 were never linked:
`Settings.tsx:api-key-row — stale-api-key-set-drops-the-confirm` and
`PlexPanel.tsx:saveVerify — absolute-write-from-cached-value` are one mechanism, a cached value
used as the input to a write or a gate, and share no matchable text. A shared
`class: cached-value-drives-a-write` would have collided where neither `finding:` line could,
since a class spanning several files has no single path to match on.

**A fingerprint that hits an open `Status/Need More Info` issue is not a duplicate to skip — it
is the question this run just answered.** Upgrade it or close it with the two commands above.
Filing a fresh issue for a candidate an earlier pass already asked about leaves the question
sitting open beside its own answer, and the next pass has to settle it twice.

Create with the reviewed commit pinned, so a finding read against a stale tree is detectable,
and **labeled**, so it is reachable by the views the tracker is actually read through. `gh` has
no field for the reviewed sha, so it goes in the body beside the fingerprint — a `reviewed:`
line, which the duplicate search above can also match on:

```
gh issue create --title "<outcome, in plain language>" \
  --body "<body>

finding:  <path>:<symbol> — <short-slug>
class:    <mechanism-slug>
reviewed: $(git rev-parse --short HEAD)" \
  --label "Kind/Bug,Priority/Critical,Reviewed/Confirmed"      # unproven: "…,Status/Need More Info"
```

**Three labels, one from each axis, on every issue filed.** An unlabeled issue is missing from
every "what is critical" and "what is a bug" filter, which is where the backlog gets triaged
from — so it is findable only by someone already scrolling past it, which is the same failure
as not filing it. `gh label list` lists what exists; never invent one, and if nothing fits, say so
in the run summary rather than filing bare. A label name with spaces survives the comma split,
so `Status/Need More Info` needs no escaping beyond the quotes already there.

| Axis | Pick |
| --- | --- |
| `Kind/` | `Bug` for a defect; `Enhancement` for a rough edge that works as built; `Security` where the loss is confidentiality, auth, or a secret; `Testing` for a gap in the suite; `Documentation` for a doc-only correction |
| `Priority/` | `Critical` — a file can be lost, or a protection silently stops covering. `High` — a wrong decision the operator can still catch on the panel. `Medium` — a surface that misleads without costing a file. `Low` — cosmetic |
| the verdict | `Reviewed/Confirmed` where the trigger was demonstrated, `Status/Need More Info` where it was not. Exactly one, never both, and never neither |

**Priority ranks the operator's loss, never the size of the fix.** A protection that quietly
stops protecting is `Critical` even when the patch is three lines, because the prime directive
is what it breaks. Judging it by effort is how a one-line fail-open ends up under a font bug.

**On an unproven issue, priority ranks that same loss as if the trigger is real** — the doubt is
already carried by `Status/Need More Info`, and discounting the priority for it too prices the
uncertainty twice, which is how a possible fail-open lands at `Low` and is never looked at again.

The title says what the operator loses, not where the code is wrong: *"A play made after
approval no longer rescues the file"* beats *"unreadable history body coerced to empty list."*
Rule 21 governs the title; the body is for engineers and may use the internal vocabulary.

**Keep the body short — it is read to decide what to do, not to relive the review.** Five
short sections, in this order and no others, or six on an unproven one. If a section needs more
than three sentences, the finding is really two issues.

```
**What breaks.** One or two sentences: the trigger, and what the operator loses. Say the loss
in the present tense where there is one, and where there is not — a redundant interlock whose
partner still catches it, a shape that costs nothing until someone writes the next one — say
exactly that and file it as a question. "Nothing today" is a statement about reachability and
never a reason to stay quiet: rule 38/117 wants a protection that cannot fire retired, and
`executor.py` trusts neither layer alone.

**Where.** `path/to/file.py:123` — `function_name`. A class covering more than one site lists
them here as `- [ ]` rows, one line each, path and symbol and trigger only. This is the one
section that may run past three sentences, and the only place a site list belongs.

**Why it happens.** Two or three sentences of mechanism. Cite the rule number if one applies.

**Fix.** The cheapest change that removes the loss, concretely. One or two sentences. Proposing
a redesign where a corrected comment or a one-line guard would do is how a `Low` finding becomes
a week nobody starts. This governs what you *propose*, never whether you file or how you rank:
priority still ranks the operator's loss and never the size of the fix.

**What would settle it.** Unproven issues only. The evidence that would prove or kill it.

finding: <path>:<symbol> — <short-slug>
```

On an unproven issue, *What breaks* says what would break **if** the trigger fires, and *Fix* is
the repair that becomes right once it does. Neither may state as fact what this pass could not
show.

No transcript of the verification, no quoted diffs, no reasoning chain — those live in
`.claude/review-findings/`, and the issue can say "verifier notes in the run artifact" if
anyone needs them. An issue nobody finishes reading is an issue nobody acts on.

**File them, then show what you filed — do not ask first.** The standing authorization is the
`CLAUDE.md` golden rule: a defect leaves the session fixed or filed, and a round trip for
permission is how the second option quietly becomes neither. Report the list with issue numbers
in the run summary, split into confirmed and unproven and grouped the way you filed it, so a
grouping the operator would have made differently is still visible and still cheap to change —
an issue is edited or closed in one command, where a lost finding is gone.

Two things still hold the gate. **The verdict is in the labels on every issue**, never only in
the prose, because an issue with neither `Reviewed/Confirmed` nor `Status/Need More Info` is a
claim nobody can weigh and nobody can filter for. And **the duplicate check runs before every
create**, because re-running a review must not re-file what is already open; that check, not an
approval prompt, is what keeps the tracker from flooding.

## Do not

- Re-review stable code that the diff did not touch, on a `diff` lane.
- Comment on code that is fine.
- Report the same defect in two places because it has two aspects.
- Propose a fix you have not checked against the tests that cover the code. The frozen review
  records a verifier who applied a proposed fix, watched 11 tests fail, and returned a
  corrected variant; that is the standard.
