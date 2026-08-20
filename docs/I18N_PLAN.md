# Translating Reaper: what it would take

> **PROPOSAL — nothing here is landed, and no stage is committed to.** Written 2026-07-30 against
> `frontend/src/` at 26,554 lines and `src/reaper/` at 100 files. It exists to be argued with
> before any code moves.
>
> **Reviewed 2026-08-20 against a tree a third larger** (123 non-test frontend files, 36,287
> lines). Every load-bearing claim held: the three parse sites, the copy maps, the §4
> test-pinning property, and Vite 8. §1's counts are refreshed. Stage 3 shrank, because the
> backend's reason strings have since been centralized into named constants. Open question 2 is
> answered. §10 is new: it says how each stage is run, by which model, and where a session ends.
>
> The short answer to "one file with all the strings": **the instinct is right, the granularity
> is nearly right, and the ownership is the part that matters.** Two catalogs, not one, and only
> the English one is ever hand-edited. See §3.
>
> **The load-bearing finding is that Reaper already made the hard architectural choice**, twice,
> for other reasons. Rule 92 bans reading operator prose as protocol because "the reason string
> is operator copy and will be reworded." `WhyPanel.tsx` says "Read the gate, never the sentence
> (rule 142)." Bug #86 was what happened when it did. Translation is the same constraint arriving
> a third time, and most of §5's work is finishing a sweep the repo already started.

## 1. What is actually there

Measured, not estimated. Frontend counts exclude tests unless the row says otherwise.
Re-measured 2026-08-20 except the rows marked †, which keep the 2026-07-30 figure and
undercount a tree a third larger.

| Surface | Count |
| --- | --- |
| Frontend source files (non-test) | 123 files, 36,287 lines |
| `aria-label=` / `title=` / `placeholder=` literals | 122 / 70 / 29 |
| JSX text nodes † | ~322 |
| Multi-word quoted string literals † | ~446 |
| `announce()` call sites (live-region copy) | 56 |
| In-app manual (`frontend/src/docs/content/*.ts`) | 6,509 words |
| **Frontend test queries bound to English copy (`getBy*`/`findBy*`/`queryBy*` by Text/Role/Label)** | **2,434** |
| Backend prose-bearing kwargs (`detail=`, `why=`, `text=`) | 61 |
| Backend helpers that compose English (`humanize_*`, `describe_*`) | 6 |
| `raise HTTPException` sites | 83 |
| Named `*_REASON` constants (were 14 hand-typed `Unknown(reason="…")` literals) | 22 |
| Python test assertions on `detail` † | ~47 |
| CSS physical direction properties (`margin-left`, `left:`) | 101 |
| CSS logical properties (`margin-inline`, `inset-inline`) | **1** |

Roughly **1,200 to 1,500 translatable strings** in the app proper, plus the manual. That is
still small. Uptime Kuma ships 60-odd locales off a single `src/lang/en.json` at comparable
size.

Two numbers dominate the cost and neither is the string count. **The test suite is the largest
consumer of English copy in the repository** at 2,434 copy-coupled queries, and **RTL is 101
physical properties against 1 logical**, which is why §7 keeps it out.

## 2. The state of the practice, as of mid-2026

Two cautions before the recommendation, because both change how the evidence reads.

**The comparison literature is vendor-captured.** Seven of the top-ranking "best React i18n
library" articles are published by companies selling i18n tooling and ranking themselves
(Tolgee, Intlayer, SimpleLocalize, locize, Lovalingo, plus Lingui's and inlang's own pages).
Intlayer discloses the conflict; most do not. There is also **no survey instrument in this
category at all** — State of JS 2025 and State of React 2025 both lack an internationalization
section, so any claim of "survey data" on i18n consensus is invented.

**Adoption, from the npm registry rather than from blogs** (weekly downloads, week of
2026-07-23):

| Package | Weekly | YoY |
| --- | --- | --- |
| `i18next` | 20,225,512 | ×2.25 |
| `react-i18next` | 14,520,121 | ×2.59 |
| `next-intl` | 4,831,733 | ×6.83 |
| `react-intl` | 3,295,399 | ×1.78 |
| `@lingui/react` | 960,495 | ×3.80 |
| `@inlang/paraglide-js` | 454,679 | ×10.88 |

Every package roughly doubled, which implies registry-wide inflation; read the ranking, not the
absolute growth. **Consensus has not moved.** Lingui and Paraglide together are under 10% of
react-i18next's volume. The genuine movement is next-intl, which is App-Router-driven and
irrelevant to a Vite SPA.

**What comparable self-hosted projects actually ship** (verified against their repos, not their
marketing): Grafana uses i18next + react-i18next with Crowdin. Gitea uses Crowdin and **migrated
its locale files from INI to JSON in v1.26.0, April 2026**. Paperless-ngx uses Crowdin (Angular
XLIFF + Django `.po`). Audiobookshelf and Uptime Kuma use Weblate with flat JSON, and
Audiobookshelf's in-progress React rewrite has newly adopted i18next. Navidrome uses POEditor.
Nextcloud is the lone Transifex/gettext holdout.

The convergent pattern across all of them, and it is unanimous: **English lives in the repo as
the single hand-edited source of truth, a bot opens a PR with completed translations, and the
contributing docs carry an explicit "do not edit the non-English files, they will be
overwritten" warning.** Grafana goes furthest and rejects translation PRs outright.

### Library: `i18next` + `react-i18next`, and the reason is Vite 8

Not because it is fashionable — because the alternatives each cost Reaper something specific.

**Lingui — rejected on a concrete, current tax.** Its macros need a Babel or SWC transform.
**Reaper is on Vite 8, which uses Rolldown and needs neither**, so adopting Lingui means
reinstating a transform pipeline the build has already shed. Rolldown has no native plugin
support yet (`js-lingui#2283`, `#2477` are open on exactly this). Its adoption is the
best-verified of the challengers — Bluesky, Documenso and Twenty confirmed in their actual
`package.json` — but the tax grows as the ecosystem leaves Babel.

**Paraglide — rejected on unverified claims.** Its headline "up to 70% smaller" is
**unreproduced by any neutral party**, and it is contradicted in two independent settings: a
user's production build (`paraglide-js#668`, closed) and a competitor's pinned-version benchmark,
both finding tree-shaking fails under the *documented* barrel-import pattern on multi-entry apps.
Two open issues in its own tracker (`#321`, `#219`) ask for a lint rule because breaking
tree-shaking is a known-common user error. It also adds a build-time codegen step and a
`project.inlang/` directory. inlang has meanwhile become "Opral (inlang & lix)", with i18n one
application of a change-control platform rather than the company.

**`react-intl-universal` — rejected.** Bus factor 1, a seven-month silent stretch ending in
mid-2026, no hooks or Context (so a locale change does not re-render), untyped keys, and it
augments the global `String` interface in every consuming file.

**FormatJS / `react-intl` — viable, second choice.** Real ICU, healthy. Heavier, and its
component-centric API fits a codebase being retrofitted less comfortably than a hook.

**i18next's one real weakness is its non-ICU message format**, which is the substantive critique
and the reason to pair it with **`i18next-icu`** so plurals and selects are written in standard
ICU MessageFormat. That keeps the catalog portable to any platform and any future library, which
matters more than the runtime does. Note that i18next's *library* weight (~6 KB) is routinely
conflated in marketing with *message* payload; the two are different arguments.

I did not finish verifying the MessageFormat 2.0 standardization status, and this plan does not
depend on it. Writing ICU today is the portable choice either way.

## 3. Answering the "one file with all the strings" question directly

The instinct is right and it is what most comparable projects do. Three refinements:

**Two catalogs, not one.** `frontend/src/locales/<lang>/ui.json` for the app, and the in-app
manual kept separate and lazy-loaded — 6,509 words that most sessions never open should not sit
in the initial bundle. Reaper is one container serving one bundle, so finer namespacing buys
lazy-loading it does not need. **Start at two and split only when a file becomes painful to
review**; a per-route split now is speculative structure.

**Only `en.json` is hand-edited.** Every other locale file is written by the translation platform
and overwritten on every sync. This is the unanimous convention above, and the CONTRIBUTING note
saying so is part of the work, not an afterthought.

**Keys are explicit and namespaced, not the English sentence.** `whyPanel.cause.plexUnmatched`,
not `"This title couldn't be found in Plex."` Natural-language keys are ergonomic until the day
you fix a typo in the source string and silently orphan every translation of it. Reaper has
already been bitten by exactly this class of coupling twice (rule 92, bug #86), and its copy is
actively reworded — the golden rule tells authors to cut every operator string once more after
writing it.

## 4. The property that makes this tractable

**The 2,434 copy-coupled test queries do not have to be rewritten.**

Configure i18next in the Vitest setup with the real English catalog and `en-US` pinned. Every
`getByText("Nothing to review")` keeps passing, because the rendered output is byte-identical to
what it is today. Tests only need touching where a *string is restructured* — §5's typed-reason
conversion, and any place a hand-built plural becomes an ICU one.

This is what turns the project from "rewrite the test suite" into "a scaffold, a mechanical
extraction, and one real refactor." It should be proven in Stage 1 on a single component before
anything else is attempted, because the entire cost model rests on it.

## 5. The backend, which is the only genuinely hard part

Reaper's backend composes English sentences and ships them to a browser that renders them
verbatim — and in three places **parses** them. The parse sites are all in `WhyPanel.tsx`:

- two `startsWith` checks, on `"your rule: "` and `"checked your rule: "`
- the regex that splits `could not check {check}: {cause}` into its two halves

That regex feeds `CHECK_COPY` and `CAUSE_COPY`, two maps in the same file that turn backend
phrases into operator sentences. **Those maps are a message catalog that nobody called one.** The
backend already documents its side that way — `snapshot.py` says the value "is a KEY into
`WhyPanel`'s `CAUSE_COPY`" — and `test_review_chips.py` already walks the contract in **both**
directions, with a written excuse ledger for every entry that cannot round-trip (#302 closed
with the second direction).

So the boundary exists. It is keyed on English prose instead of on identifiers, and that is the
whole defect.

**Half of Stage 3's prep has since happened on its own.** The 14 hand-typed
`Unknown(reason="…")` literals are gone: the backend's reasons are now 22 named module
constants (`NOT_RECORDED_REASON`, `PROGRESS_UNMATCHED_REASON`, `NO_TMDB_REQUEST_REASON`, …).
The id enumeration Stage 3 needs already exists as names. What remains is making the *values*
stable ids instead of prose, and moving the sentences into the catalog.

### What the standards say, and they agree

| Source | Position on localizing an API error `message` |
| --- | --- |
| Microsoft REST API Guidelines §7.10.2 | **"Services SHOULD NOT localize `message`"** — it becomes unreadable to the developer logging it and unsearchable |
| Google AIP-193 | `message` stays English and developer-facing; localization goes in a **separate** `LocalizedMessage`, never in `message` |
| RFC 9457 | `type` URI is the stable identifier, and **"Consumers SHOULD NOT parse the `detail` member for information"** |
| Stripe, Twilio, GitHub, Slack, Shopify | All silent on localizing errors; all ship a stable machine `code`. Slack ships no prose at all |

Stripe localizes error strings in **Stripe.js, in the browser**, never in the API. The one
recommendation this converges on is the one Reaper's own rule 92 already states.

### The conversion

`GateResult.detail` and `SignalResult.detail` become a typed reason id plus typed params, and the
frontend composes the sentence from the catalog. `defers_to_owner` on `GateResult` is the in-repo
precedent: a wording test replaced by a typed flag, shipped, and read by `api.review._chip`.

`HTTPException.detail` stays **English**, per the table above, and the frontend maps a stable
error code to translated copy — the same split Stripe ships.

This is the stage with real risk and it is where the effort goes. It is also worth doing on its
own merits: rule 92 already forbids the pattern, and three live parse sites are three live bugs
waiting for the next rewording.

## 6. What must never be translated

**The deletion confirmation phrase.** `planner.confirmation_phrase` generates `REAP 7 SOULS
214 GB`, the operator types it, and `runs.py` compares it byte-exact server-side before
anything is deleted. It is not operator copy, it is a protocol token that happens to be
readable. **Translate it and either the comparison fails and nobody can ever arm deletion, or the
comparison is translated too and now the safety interlock depends on a volunteer's catalog
entry.** The instruction around it gets translated. The phrase does not.

Two more, for the same reason: **log lines** (`logbuffer.py`, surfaced in `LogsPanel`) stay
English so an operator can paste one into a search engine or a bug report, which is Microsoft's
stated rationale. And **service and field identifiers** are names, not copy.

## 7. Stages

Each is independently landable, and each has a gate that is a command rather than a judgment.
§10 says how a stage is run and by which model. The Status line under each heading is the live
state, and the session that changes a stage updates its line in the same PR.

### Stage 1 — the scaffold, and the test proof

**Status:** not started.

Add `i18next`, `react-i18next`, `i18next-icu`. Wire the provider, add `en.json`, pin `en-US` in
the Vitest setup, and convert **one** component end to end. Set `<html lang>` from the active
locale.

**Gate:** that component's existing test file passes **unchanged**. If it does not, §4 is wrong
and the rest of this plan needs re-costing before anything else moves.

### Stage 2 — `format.ts` and the Intl layer

**Status:** not started.

The single highest-value file: it already centralizes every number, byte, date and plural.
`souls()`, `totalBytes()` and `spareRemaining()` hand-roll English plurals (`value === 1 ?
"soul" : "souls"`); `since()` hand-rolls relative time; `joinChecks()` in `WhyPanel.tsx` builds
`"a, b, and c"` by hand. These become ICU plurals, `Intl.RelativeTimeFormat` and
`Intl.ListFormat`.

Note an inconsistency this fixes on the way: `date()` and `time()` already pass `undefined` as
the locale, so **a German browser gets German dates inside English sentences today**.

Also here: `ordinal()` hand-builds English ordinals (`st`/`nd`/`rd`/`th`) and now exists
**twice**, in `JobsPanel.tsx` and `signalRamp.ts`, so the rule 72 sweep covers both; the
replacement is `Intl.PluralRules` with `type: "ordinal"`. `JobsPanel.tsx`'s `describeCron()`
renders a schedule as an English sentence and is the one surface in this stage that cannot be
reduced to a formatter, so it becomes ICU messages per cron shape.

**Gate:** `npm --prefix frontend run test`, plus `run build` read by exit code (rule 134).

### Stage 3 — the backend typed-reason conversion

**Status:** not started.

§5. Convert `GateResult.detail` and `SignalResult.detail` to id + params; delete the three parse
sites; retire `CHECK_COPY`/`CAUSE_COPY` into the catalog keyed by id. The `*_REASON` constants
already name the ids; their values become stable identifiers. Re-key
`test_review_chips.py`'s two-direction walk from prose to ids.

Stored snapshots carry prose reasons (§9 question 2, answered), so the codec maps stored prose
to ids on read. Rows from before the conversion must still render, which is precisely why the
dead `"season has no rank"` entry is deliberately kept today.

**Gate:** `uv run pytest`, `uv run mypy src/reaper`, and a stored pre-conversion snapshot still
opens in the why-panel.

### Stage 4 — extract the UI catalog

**Status:** not started.

The mechanical bulk: ~1,200 strings, `Settings.tsx` and the seven panels split out of it (142
between them, measured before the split) and `WhyPanel.tsx` (70) heaviest. The shell kept a share
of the 142 — the ten `PANELS` labels, three `aria-label`s and the two switch-confirm templates — so
the panels do not hold all of it. The file count that used to sit here, 76, moved with the split
and is not restated: re-measure it rather than adding seven. Attributes (`aria-label`, `title`, `placeholder`) and all 56 `announce()` sites
included — a live region that announces in the wrong language is worse than one that says
nothing.

**Rule 144 governs the order here, and it inverts the intuition.** Partial extraction is more
dangerous than none: a generated string is demonstrably correct and vouches for a consistency
that does not exist across its ungenerated siblings. So extract **by surface, completely**, never
by "the easy strings first."

**Gate:** an extraction check that fails on any user-visible literal left in a converted file,
and a missing-key check. Both must run in CI (rule 68: a generated asset ships with its
generator and a drift test). **Both gates land before the first surface is extracted**, because
they are what makes §10's fan-out safe to delegate.

### Stage 5 — the in-app manual

**Status:** not started.

6,509 words in `frontend/src/docs/content/*.ts`, typed blocks already: the app renders them
through `DocBody`, and `toMdx.ts` generates the manual site's pages from the same blocks
(#842). Per-locale block modules, lazy-loaded, **falling back to English wholesale** rather than
mixing: half a translated manual is worse than an English one. The MDX export keeps reading the
English blocks; translating the site is out of scope.

### Stage 6 — the translation platform

**Status:** not started.

> **The tie-breakers below were inverted by the move to GitHub (2026-07-31), so this
> recommendation is no longer settled and is left standing only until someone re-runs it.** Two
> of the three reasons Weblate won were about *not* being on GitHub: Crowdin's sync would have
> had to be rebuilt on the old forge's Actions, and Weblate's native support for that forge
> avoided a self-hosted instance. Reaper now runs on GitHub Actions, where Crowdin's ecosystem
> already fits and where Weblate's GitHub backend is at least as good as the one that won it
> this slot. Only the third reason survives the move untouched. Nothing here is wrong about
> Weblate; the comparison simply no longer discriminates the way it did, and re-deciding is
> cheaper before the stage is taken up than after a sync exists.

**Hosted Weblate, on the Libre plan**, on the Audiobookshelf and Uptime Kuma precedent, which is
where community-scale self-hosted projects land. Crowdin is the alternative and is what the
larger projects use.

Three things this depends on, all checked rather than assumed:

- **The Libre plan is gratis for public projects** and carries the 160k-string limit. Reaper's
  ~1,500 source strings are two orders of magnitude under it.
- **Reaper is AGPL-3.0-or-later**, an OSI-approved license.
- **Weblate speaks GitHub natively**, as it did the previous forge, so the unanimous workflow
  from §2 — bot opens a PR with completed translations — works without a self-hosted Weblate.

**Reaper being public at release is what makes this the answer**, and that is settled. One
practical requirement remains and is the thing to confirm when the stage is actually taken up:
hosted Weblate needs its own account with push rights on a translations branch. Reaching the
host over the internet was the other, and the move to GitHub retired it.

Also this stage: the CONTRIBUTING note that non-English files are overwritten, the CI sync, and
the hygiene-gate scoping from §8, which lands beside the first non-English file.

### Stage 7 — RTL, and it is optional

**Status:** not started, and not scheduled.

101 physical direction properties against 1 logical. This is a real stylesheet migration to
logical properties plus a `dir` pass, and it should be judged on its own, after at least one LTR
locale has actually shipped and taught us something. `docs/history/CSS_SPLIT_PLAN.md`'s remaining stages
overlap this and should be sequenced with it rather than against it.

## 8. What this plan does not do

- **No behavior change through Stage 2.** English output must stay byte-identical; that is what
  makes the existing tests a proof rather than a cost.
- **No new numbered rule proposed.** Rules 21, 68, 92, 134, 142 and 144 already govern this, and
  the two new checks in Stage 4 are gates, which `CLAUDE.md` asks for ahead of appending prose.
- **No hygiene gate is loosened, but two must be scoped.**
  `test_american_english_everywhere` and the middot family in `tests/test_repo_hygiene.py` scan
  all source text and would fire on a non-English catalog. They must bind the **`en-US` catalog
  only** — the American-English rule is about Reaper's source copy, not about French. That
  scoping is Stage 6 work: it is needed the day the first non-English file lands, and not
  before.
- **No translated string is trusted with a safety decision.** §6 is a hard boundary.
- **No claim that translation quality is free.** Rule 21 asks for copy a maintainer can verify at
  a glance, and nobody here can verify Polish. That is an accepted, stated cost of shipping
  translations at all, and it is the reason §6 exists.
- **Stages 5 through 7 are genuinely optional.** Stages 1 through 4 deliver a translatable app.

## 9. Open questions

1. **Which locales first?** Affects nothing structural, but a first non-English locale is the
   only real test of Stages 1 through 4.
2. ~~Is any `Unknown(reason=…)` value persisted in a frozen snapshot in a released build?~~
   **Answered 2026-08-20: yes.** `facts_codec.py` round-trips `reason` strings through stored
   snapshots, and decodes a missing one to `NOT_RECORDED_REASON`. Stage 3 therefore maps stored
   prose to ids on read, which is the back-compat §5 already planned for. #302 is closed.
3. **MessageFormat 2.0 status.** Unfinished research; writing ICU today is portable regardless.

**Settled 2026-07-30:** the repository will be public at release, which is what makes hosted
Weblate available and closes the only question that gated a stage.

## 10. How the stages run

Written 2026-08-20. The stages above say what changes. This section says who changes it, where
a session ends, and what survives between sessions. The design goal is that a fresh Fable
session can pick up any stage from this file alone.

### One stage, one session, one pull request

All stages land on one integration branch, `i18n`, cut from `origin/dev` when Stage 1 starts.
Stage PRs merge into `i18n`, so `dev` never carries a half-translated app. When the operator
calls the work done, one final PR squash-merges `i18n` into `dev`.

Each stage is run by a fresh Fable session, called the orchestrator below. The kickoff prompt
is `Run Stage N of docs/I18N_PLAN.md`. The session:

1. Branches off `origin/i18n` and reads this plan, plus the rule files that govern the trees
   it will touch.
2. Does the stage's work, with subagents only where the table below says so.
3. Runs the stage's gate and the full verification set, each read by exit code (rule 134).
4. Updates this plan in the same branch: the stage's Status line, and any §1 count the change
   invalidated.
5. Opens the labeled PR against `i18n`, confirms CI green (`gh pr checks`), and squash-merges
   it itself. Only the final `i18n` to `dev` PR waits for the operator.

Stage 4 repeats these five steps once per surface group, so it lands as several PRs.

**Stage PR titles and bodies are short, concise, natural language.** A Conventional Commit
title a person would say out loud, then a body of a few plain sentences: what changed and why,
one block per change, nothing a reader has to read twice. CONTRIBUTING's conventions apply
unchanged. The final `i18n` to `dev` PR is the only text that survives on `dev`, so it gets
the most care: it summarizes the stages from this plan's Status lines, in the same plain
voice.

The stages run in numbered order. Stage 4 must follow Stage 3: `WhyPanel.tsx`'s copy maps
retire into the catalog keyed by id, and extracting them as prose first would send strings to
translators that Stage 3 deletes.

`dev` moves while `i18n` lives. Sync by merging `origin/dev` into `i18n` at each stage
boundary where `dev` has moved, and always before the final PR, then re-run the gates on the
result. Merge, never rebase: the branch is shared.

### Where a session ends, and what a handoff is

- **Context is cleared at every merged PR.** The next stage starts a fresh session. A
  squash-merge lands a sha CI never tested, so the next session's gates must run on the merged
  `i18n` regardless, and a fresh session starts there by construction. Nothing in the old
  transcript is load-bearing once the PR exists.
- **Never clear between gates-green and PR-open.** Those two belong to one sitting.
- **A long session hands off through this file, never through its transcript.** Commit working
  state to the branch, write the exact resume point into the stage's Status line, then stop.
  The next session resumes from the branch and that line.
- **Subagent output enters the orchestrator as findings or structured output only.** A key map
  or a verdict, never a file dump. That is what keeps the orchestrator's context small enough
  to finish a stage in one sitting.

### Which model runs what

| Stage | Runs it | Why |
| --- | --- | --- |
| 1 | Fable, no subagents | The stage is a proof. If its gate fails, the plan is re-costed, and that judgment belongs to the orchestrator. |
| 2 | Fable, no subagents | One file plus a handful of call sites. Delegating costs more than the work. |
| 3 | Fable, no subagents | Evidence-path change, governed by rules 92, 127, 140, 142, 143. A `/reaper-review` pass runs on the diff before the PR opens; this is the one stage that earns it. |
| 4 | Fable orchestrates, Sonnet extracts and verifies | Mechanical, high-volume, and verifiable by the two CI gates. The workflow below. |
| 5 | Sonnet converts, Fable reviews the diff | The manual is typed data, and `manual.gen.test.ts` already guards its shape. |
| 6 | Fable, with the operator | The platform re-decision and the Weblate account are the operator's. The CI sync, the CONTRIBUTING note, and the gate scoping are the session's. |
| 7 | Nobody yet | Judged after a shipped locale. |

The dividing line: Sonnet gets work that is mechanical and whose gate already binds. Fable
keeps work where a wrong choice is expensive to notice: the safety path, the reason-id schema,
the cost-model proof. Never delegate work whose gate does not exist yet. That is why Stage 4's
two checks land before its first surface is extracted.

### The Stage 4 workflow file

One workflow earns a committed file: Stage 4's per-surface fan-out. The other stages are
sequential single-agent work, where a workflow adds ceremony and no safety. The file is written
at Stage 4 kickoff, because writing it earlier would pin a surface list that keeps moving, and
it is committed to `.claude/workflows/` so every Stage 4 session runs the same harness. The
shape, per surface, as a pipeline:

1. **Extract** (Sonnet). Convert the surface's files in place: keys per §3, attributes and
   `announce()` included, the surface done completely (rule 144). Return the surface's
   key-to-English map as structured output. Extraction agents never edit `en.json`. It is the
   one file every surface shares, and parallel writers would clobber it.
2. **Verify** (Sonnet, fresh context). Read only the converted files and report any
   user-visible literal left. The verifier has no investment in the extraction it is checking.
3. **Merge** (orchestrator). Write the returned namespaces into `en.json` in one pass, run the
   full gates, commit the group.

The missing-key gate is what makes the split write safe: a key an agent used but did not return
fails CI instead of rendering as a raw key on an operator's screen. Surfaces are grouped into
PRs an operator can review in one sitting, and a run's parallelism stays inside the session's
agent guideline.
