# Doc cleanup 2026-08 — archived narrative

> **FROZEN 2026-08-26. Do not update this file; it is history, not state.**
>
> This file is frozen history cut from CLAUDE.md and `.claude/rules/` in the 2026-08 doc
> cleanup. Never edit it to bring it up to date; that is what makes an archive lie.

Each section below holds, verbatim, a multi-line incident narrative that a condense removed from
its source rule or paragraph, and opens with one line naming where the surviving clause (or bare
issue citation) now lives.

## CLAUDE.md's CI-lanes narrative (#589, #783)

Replaced by the condensed CI-lanes paragraph in CLAUDE.md's Verification gates section.

> **Asking whether CI is green** is far cheaper than reading a log: `gh pr checks <n>` lists one
> row per job with its conclusion, and it is the merge gate above. **Which jobs appear depends
> on what the commit touched.** `ci.yml`'s `changes` job classifies each changed path into one
> of three lanes, first match winning: `manual/*` and `website/*` are the site, `docs/*`,
> `.claude/*` and `*.md` are prose, everything else is code. The site arm is first because
> `*.md` matches at any depth, so with prose first a `manual/` page never reached the site build
> (#589). Every other job in that file reads the verdict rather than filtering itself, so a
> prose-only commit runs `hygiene` alone, a code-only commit runs `check`, `frontend` and
> `docker`, and a commit spanning lanes runs each lane it touched — which is not the same as
> everything.
> **A lane names a tree, not a job**, so a site-only commit runs `site`, `hygiene` *and*
> `frontend`: the guards that read `manual/` and `website/` live in those last two, and for as
> long as the site lane started only its own build, a hand-edited generated page compiled and
> published with nothing having read it (#783).
> **The manual publishes from Cloudflare Pages, and no workflow here does it** — Pages watches
> `dev`, builds `website/` itself, and asks GitHub nothing, so the `site` job is the only thing
> that reads a page before an operator does and it gates nothing. Its four build settings live
> in `website/docusaurus.config.ts`'s header, beside the `baseUrl` they have to agree with.
> **Two workflows outside it carry their own path lists and have to** — a `paths` filter decides
> whether a workflow starts, so it cannot read another one's output. `codeql.yml` restates the
> prose globs as `paths-ignore` once per trigger, in `**` spelling rather than the `case` globs
> above. `weblate-notes.yml` filters on the two files that can change what it does,
> `frontend/src/locales/en/ui.notes.json` and `src/reaper/locales/en/backend.notes.json`,
> unrelated to any lane here. `tests/test_repo_hygiene.py`
> pins all three by name, so neither a fourth nor a move between files can arrive quietly and
> leave this paragraph stale again. **A workflow skipped by its own
> path filter publishes no check run at all** — a *job* skipped by an `if:` does publish one,
> with conclusion `skipped`, which is what the next sentence relies on. That is why the required
> check is `CI gate`: it runs on every commit, counts a skipped lane as a pass and a cancelled
> one as a failure, and is the one job whose absence means something is genuinely wrong.
> `pr-validation.yml` is separate, runs on every pull request whatever the paths, and reads the
> title alone.

## CLAUDE.md's promotion backstory (#934)

Replaced by a one-clause caution inline in CLAUDE.md's `main`-promotion procedure.

> A bare `dev` → `main` PR worked exactly once: squash promotions never connect the two
> histories, so from the second promotion on, every file changed since the last release reads
> as an add/add conflict against the repository baseline.

> Without it the ours-strategy merge below carries this promotion into the following release's
> notes range, where it reads as a repository chore beside the changes that actually shipped
> (#934).

## CLAUDE.md's filing-off-a-branch story

Replaced by a one-clause caution inline in CLAUDE.md's issue-filing golden rule.

> **Filing it costs more than the fix.** Three defects filed off one branch drew a verification
> pass that measured `dev`, found all three absent, closed them `Reviewed/Invalid` and opened a
> PR adding three refutations to `references/refuted.md` — the file that stops a future pass
> re-raising a candidate, so the wrong row there is worse than no row. Two were real. The third
> was real for a reason the refutation had not reached, and its `Invalid` label would have
> buried a `Priority/High` lockout. Every verdict in that pass was correct about the tree it
> read.

## Rule 144's API-key-fence case

Replaced by a one-clause parenthetical inline in rule 144 (CLAUDE.md).

> The API key fence is the case: its auth box was generated from the allowlist, and all three
> ungenerated siblings were then wrong in the same reassuring direction — two denied
> capabilities a key actually has, and the third promised a try-it-out button that cannot send
> a write at all.

## Rule 148's rollback and cost-tradeoff paragraphs

Replaced by a one-clause pointer in rule 148 (`.claude/rules/backend.md`) to `docs/DECISIONS.md`'s
Migrations section, where this reasoning now lives, adapted to read as decision reasoning.

> **What "rollback" means here is putting the image back, not moving the database back**, and the
> distinction is the whole reason release M ships its revision rather than nothing. A database is
> only ever carried forward: it stays at whatever revision it reached, and the older image has to
> be able to serve *that*. So the sequence buys survivability exactly when M+1 shipped no new
> revision — which is the ordinary case, since M is where the schema work happens. When M+1 does
> ship one, its database sits at a revision M has never heard of, and `db/schema_gate.refusal`
> refuses the boot in plain words (#565); Alembic refused it before that too, with
> `Can't locate revision identified by …`. The way back is then M's own backup, not M's image, so
> say which of the two a removal costs when you write the release note.
>
> **Measure the cost against the alternative, not against zero.** Under `render_as_batch`, SQLite
> rebuilds the whole table for anything outside `add_column`/`create_index`/`drop_index` — so the
> `server_default` revision that lets you KEEP a dead column copies the table exactly as dropping
> it would. Where both cost the same rebuild, keeping it buys only a permanent `include_object`
> arm and a registry entry, and defers the drop forever. That symmetry is the reason this rule
> exists; it does not hold for a nullable column, which can be abandoned for free.

## Rule 148's #564 obligation incident

Replaced by a bare "(#564)" citation in rule 148's three-obligations paragraph
(`.claude/rules/backend.md`).

> This
> used to be the obligation that bricked an install, because each block was another chance to
> strand `_alembic_tmp_<table>` behind a failure; #564 put DDL back inside the migration's
> transaction, so a failed sweep now rolls the temp table back with everything else and the
> remaining cost is the copying.

## Rule 66's #551 incident (frontend.md)

Replaced by a bare "(#551)" citation in rule 66 (`.claude/rules/frontend.md`).

> and it is what four missing labels cost
> before it existed: two ids that fire on ordinary scans reached the fallback, so the simulator
> answered "why was this kept" with `season_progression` and `custom`, title-cased (#551, rule
> 21).

## Rule 42's #145 incident (frontend.md)

Replaced by a bare "(#145)" citation in rule 42 (`.claude/rules/frontend.md`).

> The warning lost that way was the one about
> a setting that lets deletions past the size caps (#145).

## Rule 146's incident (frontend.md)

Deleted entirely from rule 146 (`.claude/rules/frontend.md`); no issue number existed to
preserve as a citation.

> A panel that reported the bar's contents went quiet about
> a proxy list its own bar drops on purpose, so the field walked out with no confirm; the same
> panel, on a failed refetch, went on reporting a draft while every early return above the render
> had replaced the form with one error paragraph, so the guard demanded a discard for edits with
> no box, no bar and no Discard on screen. Neither was visible from the diff, because the signal
> and the surface read correct on their own lines.

## Rule 133's #346 incident (tests.md)

Replaced by a bare "(#346)" citation in rule 133 (`.claude/rules/tests.md`).

> `conftest.py` makes every `asyncio.sleep` instant,
> which returns an unclipped `sleep(86400)` exactly as fast as a clipped one: three pin-poll
> tests asserting the call came back quickly passed with the deadline interlock deleted, and a
> fourth read a real 200ms window that held ~1,073 polls idle and 11 under load (#346).

## Rule 141's incidents (tests.md)

Deleted entirely from rule 141 (`.claude/rules/tests.md`); no issue numbers existed to preserve.

> An omission and a correct pass produce identical output, so the assertion holds either
> way: a caller used `score()`'s 365-day default while every fixture pinned that same 365,
> behind 2,578 green tests, and the omission was found by reading the caller rather than by any
> test.

> **A sweep that lives on the wrong lane
> is the same hole** — that 365 sat on a lab engine, and deleting it left the live scan with no
> non-default window under test at all until one was written
> (`test_scan_pipeline.py::TestTheWindowScoredAgainstIsThePolicysOwn`).

## Rule 145's `--no-proxy-headers` incident (tests.md)

Deleted entirely from rule 145 (`.claude/rules/tests.md`); no issue number existed to preserve.

> the
> `--no-proxy-headers` guard went red on cue while blind to the one launch site still missing the
> flag.

## Rule 145's #167 incident (tests.md)

Replaced by a bare "(#167)" citation in rule 145 (`.claude/rules/tests.md`).

> The policy anchor walk pinned eight anchors and
> still read green when a mount-condition declaration was deleted from one of them, because the
> single page state it drove that anchor in happened to hold the condition, and the deletion
> took the only case naming the anchor away with it — 40 green instead of 41 red (#167).

## Rule 147's plan-loader-shipped-mute incident (tests.md)

Deleted entirely from rule 147 (`.claude/rules/tests.md`); no issue number existed to preserve.

> The hand-rolled-notice ban matched `className=` followed immediately by a quote, reading a
> literal but neither a ternary nor a template literal — both ordinary here — so a plan loader
> shipped mute past a green test. Its count could not catch that: the count was of a different
> population than the ban — `<Notice>` call sites versus `className` strings — so a site never
> converted is absent from both halves, and the two figures agree while disagreeing with the
> tree.

## One-line residues cut in the final sweep (CLAUDE.md)

A final pass removed the narrative that had survived the first two waves as single sentences
or clauses. Each line names its home section in the pre-cleanup CLAUDE.md.

> Rules index: the table, `Holds` lines, and count "had already drifted (rule 144)."

> A new rule earns its number: rules 127, 140, 142 and 143 each described rule 72's sweep at a
> different target, five rules where four instances do it.

> Golden rule, screenshots: the README and scythelabs.dev show the running app against a real
> library, because a picture of invented titles sells nothing and that server is theirs to
> publish; a crop cannot miss one name where painting over twelve rows can.

> Golden rule, operator copy: these surfaces are scanned while deciding what to delete.

> Golden rule, migrations: additive-by-default with no exit is how dead columns accumulate
> forever behind a growing exclusion list whose job is to hide a `drop_column` from a
> reviewer — the wrong direction for a repository that fails closed.

> Golden rule, branch defects: "Filing it costs more than the fix." A verification pass found
> three branch-created defects filed as issues; two were real, and the third was mislabeled
> `Invalid` over what was actually a `Priority/High` lockout. (Full story above.)

> Rule 7/24: a review pass once found six safeguards that existed only as prose.

> Rule 21: #177 found 49 middots sitting in running text.

> Rule 134: a piped build gate is exactly how a TypeScript error once reached a commit.

> Rule 144: the API key fence — one generated auth box correct, three ungenerated siblings
> wrong in the same reassuring direction. (Full story above.)

> Verification gates: 302 "no queryFn" warnings once piled up behind a green Vitest suite.

> Promotion: a bare `dev` → `main` PR worked exactly once; from the second promotion on, every
> file changed since the last release read as an add/add conflict. (Full story above.)

> Promotion, #934: without `Kind/Release`, the promotion PR read as a repository chore beside
> the changes that actually shipped in the next release's notes.
