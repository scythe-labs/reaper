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

## Open

### The `in_progress_hold_days` help text carries the 0 case's new meaning in four trailing words

Raised at `8ff0a3e` (2026-07-27), reviewing PR #97.

`0` changed meaning in that commit: it was "a viewer's place never expires" and is now "no TV
season on the server is ever removable", because an unbounded claim no finite mirror supports
makes the guard un-establishable and holds everything. `PolicyEditor.tsx:1526` carries the
inversion as `"0 holds forever, so it always does."` — the first clause is byte-compatible with
the meaning an operator already holds, and the consequence is four words with two anaphors
(`it` = Reaper, `does` = "keeps every season", one sentence back). The control offers
`min={0}`.

**Why it is here and not an issue.** Nothing in it is false, and rule 25 is satisfied — the
mechanism is wired. This is the "read at a glance, never twice" bar in `CLAUDE.md`, on a
paragraph that grew from two sentences to four, and whether it clears that bar is a judgment
the operator should make about their own surface rather than one a review should assert. No
test asserts the string (repo-wide grep), so a copy edit stands alone.

**What would settle it:** the operator reading the row and saying whether `"0 holds forever, so
it always does"` lands. If it does not, give `0` its own sentence naming the outcome instead of
the anaphor (`"0 never lets go, so Reaper keeps every season."`) and cut the now-least-
consequential sentence. Related and separately noted: `"Set this longer than your watch history
goes back"` asks for a comparison against a number that appears only on the Scales page
(`Fairness.tsx:277`), which is not linked from here — that half is subsumed by the Scales
horizon issue filed from this run.

### `openapi_with_api_key` is never wired as `app.openapi`, so one stock call poisons the cache

Raised at `2c3752a` (2026-07-28), reviewing PR #102.

`main.py:336-369` defines `openapi_with_api_key` and the `/api/openapi.json` route at `:371-374`
is its only caller. `app.openapi` remains `FastAPI.openapi`. Both write the same
`app.openapi_schema`, and `openapi_with_api_key` short-circuits on `if app.openapi_schema is
None` — so whichever runs first wins for the life of the process. Driven: calling `app.openapi()`
once before the first request, then GETting `/api/openapi.json`, serves `root tags: False |
x-tagGroups: False | ApiKey: False`. The reference degrades to flat sections with no
descriptions, no headings, and an auth box that does not authenticate.

**Why it is here and not an issue.** No caller exists. `openapi_url`, `docs_url` and `redoc_url`
are all `None` (`main.py:292-298`), so FastAPI registers no route that calls it, and a repo-wide
grep finds no `app.openapi()` in `src/`, `tests/`, `frontend/`, CI or `scripts/`. Two lanes
reached it independently and split: `seam` called it PLAUSIBLE on the demonstrated mechanism,
`diff` refuted it as having no live trigger. Both are right about what they checked. The shape is
pre-existing — the `ApiKey` scheme has had the same exposure since `/api/docs` landed — and this
PR only enlarges what a future caller would silently discard.

**What would settle it:** a caller appearing, or the one-line fix landing on its own merits.
`app.openapi = openapi_with_api_key  # type: ignore[method-assign]` after the definition makes
every producer of the schema go through the same function and costs nothing. That is cheap enough
that the honest question is not "is this a defect" but "why is the function not simply wired the
way FastAPI expects" — which is a question for whoever wrote it, not a finding.

### The Scans section promises frozen evidence that is filed under Review

Raised at `2c3752a` (2026-07-28), reviewing PR #102.

`api/tags.py:57` describes Scans as "Start a scan, watch it run, and read the evidence it froze."
The section holds three operations: `POST /api/scan/start`, `GET /api/scan/status`, and `GET
/api/snapshots/latest`. The last is a totals row — `item_count`, `condemned`, `protected`,
`abstained`, `reclaimable_bytes`, `degraded`, `policy_hash`, `horizon_at`. The frozen *per-item*
evidence, which is what "the evidence it froze" most naturally names in this codebase, is `GET
/api/candidates/{candidate_id}` and is filed under Review.

**Why it is here and not an issue.** Nothing in it is false and rule 25 is satisfied — the
mechanism is wired and a snapshot's totals genuinely are frozen scan output. Whether a totals row
answers "read the evidence it froze" is a copy judgment about the operator's own surface, of the
same kind as the `in_progress_hold_days` entry above it, and the `diff` lane raised it at
PLAUSIBLE for exactly that reason. The three other re-filings this review found were all settled
by opening the app beside the reference; this one is not, because both readings survive that
test.

**What would settle it:** the operator reading the Scans section and saying whether they would
expect a per-title why-panel behind it. If they would, the fix is a reword rather than a re-file
— the section is right, the sentence over-promises — and "Start a scan, watch it run, and see
what the last one found." is the candidate. No test asserts the string beyond the em-dash and
non-empty guards, so a copy edit stands alone.

## Settled earlier

**The greppable per-show record entry, raised at `8ff0a3e`, was settled at `89c197d`
(2026-07-28) — confirmed as a defect, resolved as the comment fix it had itself named.** The
entry made its own verdict conditional on one question: is `season_scan.series_decision`
load-bearing for operator support? It is not. A repo-wide grep across `docs/`, `frontend/src/`
and `.claude/` (excluding `docs/history/`) finds the string in exactly one place — `unproven.md`
itself. Nothing in the operator docs, the UI, or any support text points anyone at it, so by the
entry's own stated criterion this is "a comment fix on `_log_series_decision`'s docstring, and
the claim should be narrowed rather than the code changed" — tier 4, not the tier-3 audit
finding, and the one-line `progress_is_establishable` change it offered as the alternative was
not warranted. The staleness check ran first: the call site moved from `:1133` to `:1171` but is
byte-unchanged, so the reasoning stood as written.

PR #101 gave the same docstring a **second** reason to be narrowed, which is why the fix widened
past the original entry: a season held by a `shortfall` conflict is logged as `prunable` with no
reason, and on any mirror shallower than the seasons' ages that is now *every* prunable season of
the show, where before it took a rare out-ranking count. The divergence was never only about
`progress_established`. The narrowed docstring now says what the line actually answers — what
Sonarr reported and whether the show reached the evidence pass — and disclaims a season's fate
outright.

**The Scales per-person figures entry, raised at `d9bd6db`, was settled at `8ff0a3e`
(2026-07-27) — confirmed, and filed.** It asked one question: does any Scales copy claim a
completeness the mirror cannot support? It does. `services/fairness.py:901-913` and `:939-943`
query `watch_event` with **no time predicate at all**, so every per-person figure is truncated
at the mirror's horizon — and the backend already knows it, deriving `horizon_at` at `:1042`
and rendering "Watch history reaches back to {date}, so older plays are invisible here" on the
board (`Fairness.tsx:275-280`). `PersonDetailOut` never carries it, so the person drawer, the
surface making the flattest claim, is the one that cannot name it: `ScalesPanel.tsx:132` prints
`"not watched"` and `:296-297` a red `They watched {n}%  of what they asked for`. The entry's
own named contrast, `ServerPopularityGate`, refuses the negative outright past the reach
(`gates.py:558`). The staleness check ran first and came back clean — zero commits since
`d9bd6db` touched `fairness.py`, the route, or the panel.

**Empty is the intended resting state of the Open section above, not a gap** — an entry sitting
there across many passes is itself the finding. The four entries raised at `394cc3a` and
`ef0278d` were settled in one pass at `b33bff1` (2026-07-27), each by the evidence it had itself
named:

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
