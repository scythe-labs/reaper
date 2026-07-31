# Retired: candidates that verification settled neither way

**Nothing is added to this file, and no pass needs to read it.** An unproven candidate — one
that survived a first read but whose trigger nobody proved — is filed as an issue labeled
`Status/Need More Info`, carrying no `Reviewed/` label and a *What would settle it* section
naming the evidence it wants. `SKILL.md`'s *Opening issues* section holds the mechanics, and
`gh issue list --label "Status/Need More Info"` is the list this file used to be.

Nothing was stranded by the switch: Open was already empty at `03b707d`, the pass that settled
the last eight entries.

## Why the tracker instead

The file existed because an issue asserts a defect exists and a verifier had declined to, so a
live-but-unproven candidate had nowhere to go. A label answers that better than a file does. The
question sits in the same views the backlog is triaged through instead of somewhere a reviewer
has to remember to open; proving it later is one label edit rather than a re-file, so it keeps
its number and its history; and killing it is a close as `Reviewed/Invalid`, which is visible,
rather than a deletion that is not. The one property the file had that the tracker lacks —
being read before every pass — was never the point here, and belongs to its sibling.

**`refuted.md` is unaffected and still binds.** Every refuted candidate appends there, including
one killed after being filed as a question, and a pass reads it before it looks at anything. A
closed issue is not read by anyone, so the file is still the only thing stopping the next pass
re-raising a dead candidate.

## The record

What this file held when it was retired. "Settled" is the commit that closed each candidate; read
that commit for the reasoning. Kept because `refuted.md` cites these rows, and because the
bindings underneath them are a standing lesson about pinning a candidate to a hash.

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
recording a hash, take it after any rebase, not before. **`57c11c5` and `adbc92b` are a third
variant worth naming**, because they look fine and are not: both still resolve as objects, so
`git show` works, but neither is an ancestor of `dev` any more, so `git log` never reaches them
and a diff against them silently compares across a fork. Every citation raised at those two was
therefore re-derived against `03b707d` rather than trusted, and all of them held. A hash that
resolves is not the same as a hash that is on your branch; check with `git merge-base
--is-ancestor`, not with `git show`. This outlives the file: an issue pinned with
`--referenced-version` carries exactly the same binding, and fails in exactly the same way.
