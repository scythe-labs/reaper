---
name: reaper-review
description: Review Reaper code for defects, ranked by how close they sit to the deletion path. Use for any code review of this repo — a diff, a lane (safety/seam/backend/frontend), or a whole-tree pass before promoting dev to main.
---

# Reviewing Reaper

**The prime directive sets the rank.** Reaper deletes irreplaceable data from someone else's
server. Rank a defect by how close it sits to a file being removed, never by a generic severity
rubric. A crash in a settings form is an annoyance. A protection that evaluates to "nothing
configured" is the worst outcome this codebase has, and it is usually silent.

## Argument

The argument may name a lane from the table below. A target may follow the lane. A target is a
commit, a branch, or paths. Both parts are optional, and normally you leave the lane out. With
no lane, derive it from the diff (see *Selecting the lane*). With no target, review the working
diff. A lane named in the argument overrides the derivation and runs alone.

Do not use positional `$1` substitution in this skill. It does not reliably resolve to the first
word of a multi-word argument.

With `fix` anywhere in the argument (`/reaper-review fix`, `/reaper-review safety fix`), apply
the findings after you report them. *Applying the fixes* below has the steps.

## Selecting the lane

**Derive the lane. Do not ask.** The changed paths determine the lanes mechanically. Read the
diff first (`git diff --name-only`, or `git diff --name-only origin/dev...HEAD` on a branch) and
match each changed path against this table.

| A changed path matching… | fires |
| --- | --- |
| `src/reaper/engine/{gates,verdict,signals,policy}.py`, `src/reaper/services/{executor,planner,snapshot,season_pruning}.py`, `src/reaper/clients/{base,plex}.py`, `src/reaper/api/runs.py`, `frontend/src/components/{DeletionToggle,ReapConfirm}.tsx`, `frontend/src/useSafety.ts` | `safety` |
| `src/reaper/api/*.py` (any router or `schemas.py`), `frontend/src/api.ts`, or a changed component prop/query key | `seam` |
| anything else under `src/reaper/**`, `alembic/**`, or `frontend/src/**` | `diff` |

**The lanes are additive, and each runs as its own pass.** A change that touches the executor
and adds a route fires `safety`, `seam`, and `diff`, so it gets three passes. Each lane asks a
different question. A reviewer who holds all three at once checks each side and skips the
contract between them, and `seam` exists to check that contract.

Run the lanes concurrently, as separate subagents, never as one widened prompt. Each lane hands
its findings back as text. The session that spawned the lanes makes the one `ReportFindings`
call (see *Output*).

**Name the lanes that fired.** Before you report findings, say in one line which lanes fired and
why, for example `safety (executor.py), seam (api/runs.py, api.ts), 2 lanes`. If the operator
disagrees, they can name a lane in the argument next time. The line makes a wrong derivation
visible.

A diff that touches only docs, tests, or config fires no lane. Say so and stop. Do not invent a
pass.

## The lanes

| Lane | Scope |
| --- | --- |
| `diff` | the changed lines themselves, and what they reach. The baseline pass. It runs for any code change, beside whatever else fired. |
| `safety` | `engine/{gates,verdict,signals,policy}.py`, `services/{executor,planner,snapshot,season_pruning}.py`, both transport guards, the execute route `POST /runs/{run_id}/execute` in `api/runs.py`, and the arming UI. The guards are `clients/base.py`'s `GuardedTransport` and its `GuardedSession` sibling in `clients/plex.py`. Under rule 72, review a fix to one guard against any other. The lane spans both trees on purpose. |
| `seam` | route ↔ `api/schemas.py` ↔ frontend client method ↔ props ↔ query keys, for every surface the change touches. |
| `backend` | `src/reaper/**`, `alembic/**` |
| `frontend` | `frontend/src/**` |

`backend` and `frontend` are whole-tree passes. They are expensive. Run them before a `dev` →
`main` promotion, never on a normal change.

For the deep pre-promotion pass on the deletion path, run the saved workflow
`Workflow({name: 'reaper-safety-review'})` instead of doing it by hand. It fans out 7 reviewers
over the safety files and verifies only the tier 1–2 candidates, which holds it near 16–19
agents.

**Ask before you run that workflow.** It is a large, billable fan-out. A normal change never
needs it.

**Lanes divide the review by risk.** Separate backend and frontend passes leave the seam
unreviewed. Seam defects include the confirmation phrase rendered client-side against the one
recomputed server-side, query-key invalidation after execute, and a route that returns a shape
the SPA misreads. Each single-tree reviewer sees half of such a defect and is right to skip it.
So the diff can fire `seam` on its own, and nobody has to remember to ask for it.

## Before you look for anything

**Grep `references/refuted.md` for each candidate before you report it. Do not read the
file.** The file indexes candidates that a previous pass raised and an independent verifier
killed. Its key is the same fingerprint an issue carries, `<path>:<symbol> — <slug>`. The check
matters only once you have something to report. So run it then, once per candidate, against the
path and the symbol you are about to name.

```
grep -in 'season_pruning\|plan_series_prune' .claude/skills/reaper-review/references/refuted.md
```

On a hit, read the row, then accept it or beat it. Re-raising a refuted candidate costs a full
verify cycle and returns nothing. On no hit, report. Each row records the commit it was refuted
at. If the cited code has changed since that commit, the refutation is stale and the candidate
is live again. Two sections after the index are not refutations and bind nothing. The file says
which two.

**List the open questions before you start.** Run
`gh issue list --label "Status/Need More Info"`. These issues hold candidates that a previous
pass raised and could not prove, so it filed them as questions. Each body has a *What would
settle it* section that names the evidence it wants. If this pass can supply that evidence
cheaply, supply it. Settling one question is worth more than a fresh tier-4 finding, and it
costs one label edit either way. The staleness rule from `refuted.md` applies here too. Each
question is pinned to the commit it was raised at. If its citation changed, re-derive the
question instead of trusting it.

**Do not restate the numbered rules.** `.claude/rules/*.md` holds them. They load automatically
when you read a file they govern. For example, reading `src/reaper/engine/gates.py` loads
`backend.md` before you can edit it. So the rules are already in your context. Never paste them
into a prompt. Never re-derive them. Never close a review with a summary of them. Cite a rule by
number (`violates rule 93`). The numbers are permanent, so a citation stays valid.

## What counts as a finding

Report a defect only when you can state its concrete trigger and what the operator loses. The
trigger is the input or state that produces the wrong output. "This could be fragile" is not a
finding.

Rank by blast radius, in this order.

1. **A protection that cannot fire, or fires and does not protect.** This is the silent class.
   Rules 38/117, 93, 105 and 115 each cover a guard that read as live while it covered nothing.
2. **A path that widens what gets deleted.** Examples are a fail-open on a read error, an empty
   selection that expands to everything, and a cap or count computed over a different set than
   the one acted on.
3. **Loss of the audit trail, or of the operator's ability to intervene.** Examples are a
   journal write that is not durable, and a spare that arrives too late to matter.
4. **Everything else.** This covers correctness, security, performance, and production
   readiness.

Within a lane, order findings by that scale, never by a critical/high/medium label alone.

Also sweep for the following, and rank what you find on the scale above.

- Hacks and workarounds, such as `TODO`, `FIXME`, `temp`, and `workaround`.
- Comments that claim a safeguard. Under rule 7/24 an uncited safety claim is a blocker on its
  own.
- Comments that argue for the code instead of stating what it must do. Under rule 7/24 this is a
  Low finding, and its fix cuts the argument.
- Missing error handling that fails silently.
- Hardcoded values that belong in config.
- Duplication that has already drifted between its copies. Flag a refactor only when a real
  defect lives in the duplication. "This could be tidier" is noise.

## Verify before reporting

Give every candidate an adversarial pass that tries to refute it. Open the cited lines, grep the
real call sites, and check that the trigger can occur. When uncertain, default to refuted.

Correct the line numbers during verification. A stale line reference costs the fixing agent a
search and can lead to a plausible wrong edit.

## Output

Report findings with the `ReportFindings` tool, most severe first. Do not also write them out as
prose. Do not produce a standalone markdown report. The fixing agent reads the structured list
directly, and a document in between costs a full write and a full read for the same content.

**Only the session that holds the conversation can call `ReportFindings`.** A subagent cannot
hold the tool, so naming it in an agent definition's `tools:` grants nothing. A lane hands its
findings to the session that spawned it, as text, one block per finding. Each block carries the
fields `ReportFindings` takes, which are `file`, `line`, `category`, `short_summary`, `summary`,
`failure_scenario`, and `verdict`. That session merges the lanes, orders all findings by blast
radius, and makes one call.

A whole-tree `backend` or `frontend` pass is the one exception to the standalone-report ban. Its
report goes in `docs/history/`, under the freeze banner that directory requires.

Two things go in the working tree instead of the report.

- **Every refuted candidate.** Append it to `references/refuted.md`, with the commit it was
  refuted at. Without that row, the next pass re-raises it.
- **A finding whose class no numbered rule covers.** Propose exactly one new rule for it. Append
  the rule to the scoped file in `.claude/rules/` that governs the class, and number it from
  where `CLAUDE.md`'s index says the list continues. Write one rule, in the file, in the same
  change. Do not emit a list of "agent rules". That list duplicates `.claude/rules/` and spends
  numbers that are permanent.

## Applying the fixes

Apply fixes only when the argument said `fix`. Report first, then apply. Never apply silently.

Apply only findings whose verdict is CONFIRMED, and only the fix the finding names. Do not edit
on a PLAUSIBLE finding. Its trigger was never proven, and an edit to the deletion path on that
basis trades a hypothetical bug for a real diff. File it as a question instead, per *Opening
issues*. Say which findings you skipped and why.

Before you hand back, run the gates the change touches. For backend edits, run at minimum
`uv run ruff format .`, `uv run ruff check .`, `uv run mypy src/reaper tests/`, and the test files
that cover what you changed. If a fix breaks a test, the finding was wrong about the mechanism.
Return the corrected variant, or withdraw the finding to `references/refuted.md`.

Commit each fix with the test that pins it, per `CLAUDE.md`.

## Opening issues

Every finding this run does not fix goes to the tracker, so the work survives the session. File
confirmed findings as defects and unproven ones as questions. Nothing goes to a reference file.
The tracker is GitHub, reached with `gh`. A finding can also land as an inline PR comment with
`/code-review --comment` where that fits better than an issue.

**First, check that the finding exists on `dev`.** Do this before anything else about the
finding, including severity, the class, and the duplicate check. Establish which tree the
finding lives on.

```
git show origin/dev:<path> | grep -n '<the thing>'
```

**Fix a defect that the diff under review introduced in that diff. Never file it.** The tracker
describes what an operator can hit, and nobody can hit an unlanded branch. Filing it ships a
known-broken change and asks someone else to notice.

**A verdict holds only for the tree it was measured on.** When you refute a candidate, name the
ref you read. The index's third column, `refuted at <sha>`, holds it. Check that the ref is the
one the candidate was raised against. A refutation measured on a different tree does not count.

**When a branch defect needs an issue anyway.** That happens when the fix is deferred, or spans
work someone else holds. The title says `on <branch>`. The body opens with
`on <branch>, not on dev`, the base commit, and the `git show` output that proves the contrast.
Without that opening, a verifier checks the issue against `dev` and closes it, correctly.

**File one issue per fix, never one per finding.** One issue describes one edit. If two findings
would be closed by the same edit, they are one issue.

**A class is what one edit closes, never what shares a subject.** The test applies both ways. One
theme, such as screen-reader support, can hold several classes, for example a card's name, a
live region, focus containment, and a menu's keyboard path. Each needs its own edit, so each is
its own issue. In the other direction, two sites can look unrelated and share one mechanism,
such as a cached value used as the input to a write or a gate. Those two sites are one class.

**Sweep the class before you file, so the issue states the full extent.** Rule 72 binds a fix to
every sibling. This binds the filing the same way, because a defect at one site is a question
about the whole tree. Grep for the other sites before you write the body. The sweep must be
thorough, and it need not be perfect. A later pass may still find a site the sweep missed, and
raising it then is right.

**A class stays one issue only while one commit can close it.** An issue that cites more than
about eight sites rarely gets closed, and an issue nobody closes leaves the problem in place.
Past about eight sites, or across two `Kind/` values or two `Priority/` tiers, file the class as
several closable issues that share one `class:` line. One issue carries one `Kind/` and one
`Priority/`. Folding a `Security`/`High` site into a `Bug`/`Medium` class issue drops it out of
the filter the backlog is triaged from, which is the same as not filing it.

**A new site in a filed class.** If the class issue is open and the new site does not outrank its
`Priority/`, append the site to that issue. If the class issue is closed, file a new one that
carries the same `class:` line and cites the closed one. Nobody reads a closed issue. If the new
site outranks the class, file it as its own issue at its own priority.

**Two cases are not over-splitting, and a sweep must not force them together.** Findings that
share a subject but need different edits are different classes, however similar they read. A
finding that did not exist until an earlier fix shipped could not have been swept for. A later
pass that finds one has not caught a sweep failure.

| Findings | Grouping |
| --- | --- |
| one class, up to ~8 sites, one `Kind/` and one `Priority/`, one verdict | one issue, with each site a checklist row under *Where* |
| one class, but a site outranks the others, or takes a different `Kind/`, or is undemonstrated while they are proven | that site gets its own issue with its own labels, carrying the same `class:` line |
| one class, more than ~8 sites | a tracking issue that lists per-file issues, never one issue that holds every site. Past eight sites nobody closes it |
| siblings under rule 72 (the same defect in sibling functions) | one issue, because the rule requires them fixed together |
| tier 1–2, each a distinct class | one issue each. Each needs its own commit and is worth doing on its own |
| tier 3–4 | grouped by shared root cause, or by shared file and theme. The body lists each as a checklist row |

**When two rows disagree, follow the one that keeps the issue closable.** Never average
`Priority/` down to fit. Splitting a class costs a `class:` line. Burying a `Critical` inside a
cosmetic bundle can cost the operator a file.

**File an unproven candidate too, as a question.** Never put it in a reference file. Never leave
it in `.claude/review-findings/`, which is gitignored scratch that is deleted with the worktree.
The labels carry the difference, so a reader sees it without opening the issue. An issue asserts
that a defect exists. So an issue whose trigger a verifier declined to demonstrate takes
`Status/Need More Info` and no `Reviewed/` label at all.

| Verdict | Third label | The tracker reads it as |
| --- | --- | --- |
| CONFIRMED | `Reviewed/Confirmed` | a defect exists. Fix it |
| PLAUSIBLE, or never verified | `Status/Need More Info` | a trigger nobody has shown. Prove it or kill it |

An unproven body carries one extra section, above the fingerprint line.

```
**What would settle it.** The test to write, the call site to grep, the journey to drive.
```

Write that section for someone who has never seen the finding. That person is the only one who
will read it. "Needs verification" settles nothing and leaves the issue open forever.

A question leaves the unproven state in one command.

- **Proven.** Run `gh issue edit <n> --add-label "Reviewed/Confirmed" --remove-label "Status/Need
  More Info"`. One call does both. Do not re-file. The issue keeps its number, its body, and its
  history. Edit the body too, and name what settled it, such as the test that was written or the
  journey that was driven. Strike the sentence that said nobody had demonstrated it. A body that
  still reads "was not demonstrated" under a `Reviewed/Confirmed` label is a false statement
  about the work. Rule 134's standard applies to the tracker too. Two issues promoted in the
  same second were not both demonstrated.
- **Refuted, or stale because the cited code moved.** Run `gh issue edit <n> --add-label
  "Reviewed/Invalid"`, then `gh issue close <n>`. Then append the reasoning to
  `references/refuted.md`, naming the issue number. Leave `Status/Need More Info` on the issue as
  the record of how it arrived. The list above filters open issues, so a closed one drops out on
  its own. The `refuted.md` row stops the next pass from re-raising the candidate. A pass reads
  `refuted.md` before it looks at anything, and it never reads closed issues.

**Cap each session at 8 confirmed issues and 4 unproven ones.** The cap counts per session, never
per review run, because most findings arrive while building something else. A cap that binds
only `/reaper-review` misses where most findings come from. The unproven cap is lower because
such an issue asserts less and still costs someone a judgment call to close. Past either cap, put
the remainder into a single tracking issue that lists them all, labeled like the issues it
carries. Nothing is lost, and the tracker does not flood. In the run summary, say how many
findings were rolled up. A cap that hides what it dropped reads as "that was everything."

**The cap merges findings by class, never by coincidence.** Sites of one defect belong in one
issue, up to the site limit above. The cap is no reason to put two unrelated defects in one issue. A
bundle that nobody can close in one commit hides its contents from every filter. So the remainder
goes into a tracking issue that lists the findings, never into a single issue that holds them
all. If the cap is binding, the usual cause is a class filed one site at a time. Merge that class
and the count falls.

**Check for duplicates first.** Run `gh issue list --state all --search "<class-slug>"`. It
searches bodies, so it matches the fingerprint below. Re-running a review must not re-file what
is already open. Each body carries a stable fingerprint to match on.

```
finding: <path>:<symbol> — <short-slug>
class:   <mechanism-slug>
```

Match on those lines, never on the line number, which drifts with every edit above it.
`finding:` identifies this issue. `class:` names the mechanism, and it lets the duplicate check
work across sites. Grep for the class slug first. Reuse an existing class slug verbatim, never a
new synonym.

A site-shaped `finding:` line cannot link two sites of one mechanism. The `class:` line does. For
example, `GeneralPanel.tsx:api-key-row — stale-api-key-set-drops-the-confirm` and
`PlexPanel.tsx:saveVerify — absolute-write-from-cached-value` are one mechanism, a cached value
used as the input to a write or a gate. The two `finding:` lines share no matchable text. A
shared `class: cached-value-drives-a-write` matches where neither `finding:` line can, because a
class that spans several files has no single path to match on.

**A fingerprint that hits an open `Status/Need More Info` issue is the question this run just
answered. Do not skip it as a duplicate.** Upgrade it or close it with the two commands above. Do
not file a fresh issue for a candidate an earlier pass already asked about. That leaves the
question open beside its own answer, and the next pass has to settle it twice.

Create the issue with the reviewed commit pinned, so a reader can detect a finding read against a
stale tree. Label it, so it shows up in the views people read the tracker through. `gh` has no
field for the reviewed sha, so put it in the body beside the fingerprint as a `reviewed:` line.
The duplicate search above can match on that line too.

```
gh issue create --title "<outcome, in plain language>" \
  --body "<body>

finding:  <path>:<symbol> — <short-slug>
class:    <mechanism-slug>
reviewed: $(git rev-parse --short HEAD)" \
  --label "Kind/Bug,Priority/Critical,Reviewed/Confirmed"      # unproven: "…,Status/Need More Info"
```

**Put three labels on every issue you file, one from each axis.** An unlabeled issue is missing
from every "what is critical" and "what is a bug" filter, and the backlog is triaged from those
filters. Only someone already scrolling past it will find it, which is the same as not filing it.
`gh label list` lists the labels that exist. Never invent one. If nothing fits, say so in the run
summary instead of filing the issue bare. A label name with spaces survives the comma split, so
`Status/Need More Info` needs no escaping beyond the quotes already there.

| Axis | Pick |
| --- | --- |
| `Kind/` | `Bug` for a defect. `Enhancement` for a rough edge that works as built. `Security` where the loss is confidentiality, auth, or a secret. `Testing` for a gap in the suite. `Documentation` for a doc-only correction |
| `Priority/` | `Critical` where a file can be lost, or a protection silently stops covering. `High` for a wrong decision the operator can still catch on the panel. `Medium` for a surface that misleads without costing a file. `Low` for something cosmetic |
| the verdict | `Reviewed/Confirmed` where the trigger was demonstrated, `Status/Need More Info` where it was not. Exactly one, never both, and never neither |

**Priority ranks the operator's loss, never the size of the fix.** A protection that quietly
stops protecting is `Critical` even when the patch is three lines, because it breaks the prime
directive. Ranked by effort, a one-line fail-open ends up below a font bug.

**On an unproven issue, rank the loss as if the trigger is real.** `Status/Need More Info`
already carries the doubt. Lowering the priority for it too counts the doubt twice, and a
possible fail-open then lands at `Low` where nobody looks at it again.

The title says what the operator loses, never where the code is wrong. Write *"A play made after
approval no longer rescues the file"*, never *"unreadable history body coerced to empty list."*
Rule 21 governs the title.

**Write the body in plain English too.** The body names files, symbols and rule numbers freely,
which the title may not. Everything around those names is ordinary sentences. Say what the
operator sees and what the code does, in the words you would use out loud. Write "a show card
reads 'titles' where the rest of the panel says 'shows'", never "the `mediaType` select is absent
from three sibling `Reason` calls". Use short sentences, active voice, no em dashes, and one
clause per fact. Rewrite a sentence that needed a second read. Never defend it.

Someone other than the filer usually picks up the issue weeks later. Their first question is
"what actually goes wrong". A body written in the review's shorthand answers that question last,
so the issue gets skipped and re-derived instead of fixed.

**This binds an issue you edit as much as one you file.** When you touch an issue body already in
the tracker for anything else, rewrite it then.

**Keep the body short.** A reader uses it to decide what to do. Use five short sections, in this
order and no others, or six on an unproven issue. If a section needs more than three sentences,
the finding is two issues.

```
**What breaks.** One or two sentences: the trigger, and what the operator loses. Say the loss
in the present tense where there is one. Where there is none, for example a redundant interlock
whose partner still catches it, or a shape that costs nothing until someone writes the next one,
say exactly that and file it as a question. "Nothing today" is a statement about reachability and
never a reason to stay quiet: rule 38/117 wants a protection that cannot fire retired, and
`executor.py` trusts neither layer alone.

**Where.** `path/to/file.py:123` in `function_name`. A class covering more than one site lists
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

On an unproven issue, *What breaks* says what would break if the trigger fires. *Fix* is the
repair that becomes right once it does. Neither section may state as fact what this pass could
not show.

Leave out the verification transcript, quoted diffs, and the reasoning chain. Those live in
`.claude/review-findings/`, and the issue can say "verifier notes in the run artifact" if anyone
needs them.

**File the issues, then show what you filed. Do not ask first.** The `CLAUDE.md` golden rule is
the standing authorization. A defect leaves the session fixed or filed, and a round trip for
permission tends to leave it neither. In the run summary, report the list with issue numbers.
Split it into confirmed and unproven, and group it the way you filed it. The operator can then
see a grouping they would have made differently, and change it cheaply. An issue is edited or
closed in one command, and a lost finding is gone.

Two things still gate filing. Put the verdict in the labels on every issue, never only in the
prose. An issue with neither `Reviewed/Confirmed` nor `Status/Need More Info` is a claim nobody
can weigh or filter for. Run the duplicate check before every create. Re-running a review must
not re-file what is already open, and the duplicate check is what keeps the tracker from
flooding.

## Do not

- On a `diff` lane, re-review stable code that the diff did not touch.
- Report the same defect in two places because it has two aspects.
- Propose a fix you have not checked against the tests that cover the code. Checked means you
  applied the fix and ran those tests.
