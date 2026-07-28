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

- **Confirmed** — the trigger was demonstrated. It becomes a fix or a tracker issue.
- **Refuted** — a verifier killed it. Move it to `refuted.md` under the commit it died at.
- **Stale** — the cited code changed and the reasoning no longer applies.

**A settled candidate is no longer unproven, so it does not stay here as prose.** It is deleted
from Open and recorded as one ledger row below; the reasoning that settled it lives in the
commit that closed it, which is the durable copy. An entry that sits in Open across many passes
untouched is itself the finding — either the trigger is worth proving or the candidate is worth
killing. Empty is the intended resting state, not a gap. Do not let this file become an inbox.

Verifier transcripts, reasoning chains, and per-agent run notes stay in the gitignored
`.claude/review-findings/`, which is interim by design. Only the candidate itself belongs here,
because only the candidate outlives the run.

## Open

_(empty)_

## Settled

Newest first. "Settled" is the commit that closed it; read that commit for the reasoning.

| Candidate | Raised | Settled | Outcome |
| --- | --- | --- | --- |
| The Scans section promises frozen evidence that is filed under Review | `2c3752a` | `9f3f7c8` | **Confirmed** by the operator — reworded, not filed |
| The `in_progress_hold_days` help carries the 0 case in four trailing words | `8ff0a3e` | `9f3f7c8` | **Confirmed** by the operator — reworded, not filed |
| `openapi_with_api_key` is never wired as `app.openapi` | `2c3752a` | `e290130` | **Fixed** — no live trigger, but the one-line wiring was cheaper than the verdict |
| The greppable per-show record (`season_scan.series_decision`) | `8ff0a3e` | `89c197d` | **Confirmed** — docstring narrowed, code unchanged |
| Scales per-person figures claim a completeness the mirror cannot support | `d9bd6db` | `8ff0a3e` | **Confirmed** — filed as issue #99 |
| Watcher pressure on an under-covered window | `394cc3a` | `b33bff1` | **Confirmed** — folded into issue #83 (rule 140's third reader) |
| The policy lab's reach fallback | `394cc3a` | `b33bff1` | **Refuted** — see `refuted.md` under `b33bff1` |
| The save bar calls a length unsaved that the switch beside it commits | `ef0278d` | `b33bff1` | **Confirmed** — filed as issue #90 |
| A fixed control track charges every row the width of the widest control | `ef0278d` | `b33bff1` | **Confirmed** — filed as issue #91 |

`8ff0a3e` and `394cc3a` no longer resolve: both were rewritten by a rebase before the hash was
recorded. The rows are kept for the record, but those two bindings cannot be checked — when
recording a row, take the hash after any rebase, not before.
