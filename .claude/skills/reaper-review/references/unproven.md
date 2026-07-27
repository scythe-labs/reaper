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

## Nothing open

Empty as of `b33bff1` (2026-07-27), and **empty is the intended resting state, not a gap.** The
four entries raised at `394cc3a` and `ef0278d` were settled in one pass, each by the evidence it
had itself named:

- Watcher pressure on an under-covered window — **confirmed**, folded into issue #83 as rule 140's
  third reader (`evaluate_signal` alongside `CustomProtectGate` and `evaluate_keep`).
- The policy lab's reach fallback — **refuted**; see `refuted.md` under `b33bff1`.
- The save bar calling a length unsaved that the switch beside it commits — **confirmed** as issue
  #90, on the operator's answer to the one question the entry said only they could answer.
- A fixed control track charging every row the width of the widest control — **confirmed** as issue
  #91, once measurement showed the released track moves the control 0.00px and so buys no
  alignment for the rows paying for it.

Two of those four had been sitting on questions nobody could answer from the code. Both were
answerable: one by asking the operator, one by measuring the thing the entry had assumed was a
tradeoff. That is the general lesson for this file — an entry that reads as "needs a judgment
call" is often a measurement nobody has taken yet.
