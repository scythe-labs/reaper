# Candidates that verification settled neither way

Review candidates that **survived a first read but whose trigger was never proven**. They are
not findings — nothing was edited on their strength, and nothing was filed, because an issue
asserts a defect exists and a verifier declined to. They are not refuted either: no one showed
the trigger cannot occur. Read this before a review, alongside `refuted.md`.

The sibling file is the other half of the same record. `refuted.md` stops the next pass wasting
a verify cycle on a dead candidate; this file stops it *re-deriving from scratch* a live one,
and tells it what evidence would settle the question.

**An entry is bound to a commit, not to the repo forever** — same rule as `refuted.md`. Each
records where the code stood when it was written. If the cited lines have changed since,
re-derive rather than trusting either answer.

**Every entry leaves this file one of three ways**, and leaving is the point:

- **Confirmed** — the trigger was demonstrated. It becomes a fix or a tracker issue, and the
  entry is deleted with a note in the commit that closed it.
- **Refuted** — a verifier killed it. Move it to `refuted.md` under the commit it died at.
- **Stale** — the cited code changed and the reasoning no longer applies. Delete it, and say so
  in the commit.

An entry that sits here across many passes untouched is itself the finding: either the trigger
is worth proving or the candidate is worth killing. Do not let this file become an inbox.

Verifier transcripts, reasoning chains, and per-agent run notes stay in the gitignored
`.claude/review-findings/`, which is interim by design. Only the candidate itself belongs here,
because only the candidate outlives the run.

## Raised at `394cc3a` (2026-07-27, reviewing the history-reach commit, PR #81)

The confirmed findings from this pass were fixed in `1b1458c`, `951e2ef`, `69d69f8`, `e0c7ca5`
or filed as issues #83–#86. These two were not.

### 1. Watcher pressure is still full on an under-covered window

`src/reaper/engine/signals.py:320` — `evaluate_signal`, `SignalId.FEW_WATCHERS`. Tier 3.

The commit narrows the FEW_WATCHERS *wording* to the covered span but leaves the *pressure*
computed from a count the mirror cannot support. `raw = max(0.0, saturate_at - watchers)` is
unchanged and the signal is inverted, so an under-covered window under-counts watchers and
therefore over-charges deletion pressure. Because `facts.distinct_watchers` stays `Known`,
`coverage_bp` still reads 100% and the operator's `coverage_floor_bp` backstop does not fire
either.

Trigger requires `server_popularity` disabled (so nothing blocks, and
`policy.popularity_window_days()` falls back to 365) plus `min_dormancy` lowered below 365, on
a mirror shallower than 365 days. A title five people watched 200 days ago behind a 90-day
mirror reads `watchers=0`, takes full pressure, and its detail honestly says "in the last 3
months" while the score behaves as though the year had been checked.

**Why it is not confirmed:** never driven through a real scan, and the author explicitly
declined this direction in the diff's own comment ("this lane is soft pressure, not a
protection, so it narrows the claim rather than withholding it"). That reasoning is defensible
for the pressure; it does not address the coverage angle, which is the part worth deciding.

**What would settle it:** drive a scan with that configuration and read the resulting
`coverage_bp`. If it reports 100% on a window the mirror cannot cover, the coverage half is
real regardless of how the pressure question is decided.

**If accepted,** the named fix is to clamp `raw` to `None` when `covered < window_days`, routing
it through the existing UNREADABLE tail — weight retained, zero pressure, coverage discounted —
the same reading `Unknown` already gets. Rule 31.

### 2. The policy lab's reach fallback recreates the gap it documents

`tests/_policy_lab.py:87` — `mirror_reach_days`. Tier 4.

The docstring argues that leaving the field at its `Absent` default "would pin a baseline of
440 un-checkable rows and stop exercising the gate at all (rule 132)". The `default=0.0`
fallback produces that same outcome, so the docstring names a guard the code does not provide
(rule 7/24).

**Why it is not confirmed:** the strong form is refuted. Forcing the reach to `0.0` moves 45 of
440 vectors off their committed baseline, so a fixture that merely *loses* `play_recency_days`
fails loudly. What slips through is only a full regeneration via `scripts/policy_lab_extract.py`
from a library whose candidates have no plays at all — vectors and baseline are written
together, nothing mismatches, and the sweep silently stops exercising `ServerPopularityGate`
while reporting green. Such a scan would itself have degraded, which is why this stays
marginal.

**What would settle it:** regenerate the lab fixture from a play-free candidate set and check
whether the sweep still reports green. If it does, the docstring's claim is false in the one
case it exists to cover.

**If accepted,** `raise` instead of `default=0.0`: a fixture with no plays cannot support the
lab's popularity coverage, and saying so at import is what the docstring already claims.
