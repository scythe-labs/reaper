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

Empty, as of `03b707d`. The eight entries that stood here were settled in one pass: six
confirmed and filed, two refuted.

## Settled

Newest first. "Settled" is the commit that closed it; read that commit for the reasoning.

| Candidate | Raised | Settled | Outcome |
| --- | --- | --- | --- |
| The gate-off shortfall remedy says "remove that rule" without naming which one | `94f11fc` | `03b707d` | **Confirmed** — the "only one field can be the referent" defense holds at field level and fails at rule level; filed as issue #157 |
| `dirtyPanels` claims a population in prose, and nothing reconciles it | `adbc92b` | `03b707d` | **Confirmed** by construction, with a better fix than the one proposed; filed as issue #156 |
| The stale-read notice tells the operator to do the one thing that loses their draft | `adbc92b` | `03b707d` | **Confirmed** — the panel's own comment names the harm its replacement line reintroduces; filed as issue #153 |
| The mid-binge hold is the same shape as the window shortfall, and nothing warns about it | `f8592b3` | `03b707d` | **Confirmed** — the composed journey was driven end to end; filed as issue #154 |
| `horizon()` puts a `COUNT(*)` on the debounced policy-validate path | `f8592b3` | `03b707d` | **Refuted** — see `refuted.md` under `03b707d` |
| The spare-length Segmented stays live during a save that is writing its own field | `57c11c5` | `03b707d` | **Confirmed** — driven with the deferred promise the entry named; filed as issue #151 |
| Neither section-switch confirm announces itself to a screen reader | `57c11c5` | `03b707d` | **Confirmed** by construction against WCAG 2.2 SC 4.1.3; filed as issue #155 |
| A vendor bump inside the caret silently puts the 403 back on every try-it-out write | `96b8114` | `03b707d` | **Refuted** — see `refuted.md` under `03b707d` |
| A refetch failure leaves the panel reporting a draft it no longer renders | `57c11c5` | `ab7a9fc` | **Confirmed** — driven with a scratch test, fixed on the branch |
| A proxy list parked behind its switch is a draft the confirm cannot see | `57c11c5` | `ab7a9fc` | **Confirmed** — fixed on the branch |
| Discard leaves the day number staged when the stored default is Forever | `57c11c5` | `4f17180` | **Confirmed** — the `ef0278d` refutation was stale; fixed on the branch |
| The switch confirm covers General while Plex and Notifications also hold drafts | `57c11c5` | `4f17180` | **Confirmed** — filed as issue #128, deferred in writing per rule 72 |
| The Scans section promises frozen evidence that is filed under Review | `2c3752a` | `9f3f7c8` | **Confirmed** by the operator — reworded, not filed |
| The `in_progress_hold_days` help carries the 0 case in four trailing words | `8ff0a3e` | `9f3f7c8` | **Confirmed** by the operator — reworded, not filed |
| `openapi_with_api_key` is never wired as `app.openapi` | `2c3752a` | `e290130` | **Fixed** — no live trigger, but the one-line wiring was cheaper than the verdict |
| The greppable per-show record (`season_scan.series_decision`) | `8ff0a3e` | `89c197d` | **Confirmed** — docstring narrowed, code unchanged |
| Scales per-person figures claim a completeness the mirror cannot support | `d9bd6db` | `8ff0a3e` | **Confirmed** — filed as issue #99 |
| Watcher pressure on an under-covered window | `394cc3a` | `b33bff1` | **Confirmed** — folded into issue #83 (rule 140's third reader) |
| The policy lab's reach fallback | `394cc3a` | `b33bff1` | **Refuted** — see `refuted.md` under `b33bff1` |
| The save bar calls a length unsaved that the switch beside it commits | `ef0278d` | `b33bff1` | **Confirmed** — filed as issue #90 |
| A fixed control track charges every row the width of the widest control | `ef0278d` | `b33bff1` | **Confirmed** — filed as issue #91 |

**The eight rows settled at `03b707d` were closed by filing, not by fixing, so `03b707d` is the
tree they were settled *against* rather than a commit carrying the repair.** The reasoning lives
in the issue named in each Outcome cell, and in `refuted.md` for the two that died. That is the
one case where the "read that commit" instruction above points at an issue instead.

`8ff0a3e` and `394cc3a` no longer resolve: both were rewritten by a rebase before the hash was
recorded. The rows are kept for the record, but those two bindings cannot be checked — when
recording a row, take the hash after any rebase, not before. **`57c11c5` and `adbc92b` are a
third variant worth naming**, because they look fine and are not: both still resolve as objects,
so `git show` works, but neither is an ancestor of `dev` any more, so `git log` never reaches
them and a diff against them silently compares across a fork. Every citation raised at those two
was therefore re-derived against `03b707d` rather than trusted, and all of them held. A hash that
resolves is not the same as a hash that is on your branch; check with `git merge-base
--is-ancestor`, not with `git show`.
