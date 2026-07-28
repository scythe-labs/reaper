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

### A vendor bump inside the caret silently puts the 403 back on every try-it-out write

Raised at `96b8114` (2026-07-28, reviewing PR #123) by all three lanes independently, each
ranking it low.

`/api/docs` can send writes only because `main.py`'s `api_docs` passes an `onBeforeRequest` hook
that sets the CSRF header on Scalar's request builder. The vendor stamps that API **experimental
in three places** — the config `typeComment` in the shipped bundle, the `.d.ts`, and the builder
type ("still experimental and may change in minor releases") — and `frontend/package.json` carries
`"@scalar/api-reference": "^1.63.0"`. `package-lock.json` pins 1.63.0 today, so rule 15's
lockfile install keeps CI and the container on a known-good bundle.

If a lock refresh inside the caret renames the hook or reshapes its argument, every try-it-out
write 403s again while the `Session` scheme goes on promising "reads and writes as you" — the
exact rule 144 drift the PR exists to close, arriving through a dependency rather than an edit.
**No test can currently see it.** The guard now pins the exact call string the page must contain,
built from `CSRF_HEADER`/`CSRF_VALUE`, which catches a Reaper-side edit and cannot catch a
Scalar-side rename: the assertion is against Python's own output. `frontend/public/vendor/` is
gitignored, so rule 68's drift test has nothing to hash either.

**Why it is unproven rather than confirmed:** nobody demonstrated a version inside `^1.63.0` that
breaks it. The direction of failure is friendly (writes refuse, nothing is lost), which is why all
three lanes ranked it low rather than fixing it.

**What would settle it:** install the newest release satisfying the caret into a scratch tree, run
the vendored bundle's `map-config-plugins` resolution over the real config, and check whether the
hook still receives a mutable builder. If it does not, the answer is a behavioral guard rather than
a string assertion — the cheapest shape is a headless page load that drives one write and asserts
the header on the wire, which is the one thing the current test's docstring already says it cannot
do.

### The spare-length Segmented stays live during a save that is writing its own field

Raised at `57c11c5` (2026-07-28, reviewing PR #127) by the `seam` lane, ranked low.

`Segmented` (`components/Segmented.tsx`) takes no `disabled` prop at all, so the spare-length mode
control is the one thing in that row still pressable while a save is in flight. Every neighbor
carries `disabled={save.isPending}`: the day box, the expand-seasons select, the reverse-proxy
switch. The candidate: from a stored 365 with the box set to 7, press **Save changes** and then
**Forever** before the response lands, and `onSuccess`'s `setSpareForever(data.default_spare_days
=== 0)` silently reverts the press.

**Why it is unproven rather than confirmed:** the race was reasoned, not driven. The window is one
network round trip and the outcome is a mode press being dropped, not a value being written, so it
fails toward doing nothing.

**Why it is not simply the class refuted at `ef0278d`:** that entry covered *text inputs* losing
keystrokes to an in-flight re-seed and was accepted as pre-existing. This shape is new to PR #127 —
before it, the press fired its own mutation instead of racing one.

**What would settle it:** a test resolving `saveGeneral` from a deferred promise with the Forever
click in the gap, asserting the mode after the response lands. If confirmed, the fix is a
`disabled` prop on `Segmented`, which both settings panels and the policy editor use, so rule 72
binds it to every consumer at once.

### Neither section-switch confirm announces itself to a screen reader

Raised at `57c11c5` (2026-07-28, reviewing PR #127) by the `seam` lane, ranked low.

`Settings`' new pending-switch notice renders as `.notice.notice-warn` with no `role="alert"` and
no `aria-live`, and focus stays on the rail button that appears to have done nothing; pressing it
again just re-sets the same `pendingSwitch`. The `PolicyEditor` twin has the identical gap and
predates this PR, and the repo has only four `aria-live` uses, all loading spinners.

**Why it is unproven rather than confirmed:** nobody drove it with a screen reader, and the notice
does render in the document immediately after the control, which some readers will reach on the
next navigation step anyway.

**What would settle it:** drive both confirms with VoiceOver and record whether the notice is
announced without a manual re-read. If confirmed, it is one fix across both (rule 72), not a
regression in this PR.

## Settled

Newest first. "Settled" is the commit that closed it; read that commit for the reasoning.

| Candidate | Raised | Settled | Outcome |
| --- | --- | --- | --- |
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

`8ff0a3e` and `394cc3a` no longer resolve: both were rewritten by a rebase before the hash was
recorded. The rows are kept for the record, but those two bindings cannot be checked — when
recording a row, take the hash after any rebase, not before.
