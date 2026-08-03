# Size truth: making an unreadable size stop being a number — archived

> **FROZEN 2026-07-30. Do not follow any instruction in this file.** Stages 1, 2, 3, 3b and 3c
> shipped and are in the code. Stages 4 through 7 were retired unbuilt: an audit read Sonarr's
> and Radarr's own source and found the premise under Stages 5 and 6 false. `sizeOnDisk` is not
> a folder walk in either service, it is a `SUM` over the file table, so the season growth
> interlock this plan set out to repair was already comparing one quantity against itself. The
> finding is learning 14 in `docs/LEARNINGS.md`; the decisions worth keeping are under **Size
> acquisition** in `docs/DECISIONS.md`. What survives as open work is issues **#317** (a byte
> cap that may under-count a movie folder's untracked bytes), **#320** and **#321**. Never edit
> this file to bring it up to date.
>
> **Four instructions here are actively unsafe now, which is why it is frozen rather than
> slimmed.** Stage 2 says to regenerate the Alembic baseline; that baseline is frozen and
> editing it silently skips every existing database. §4.5 and §4.7 say to hand-roll
> `.notice.notice-warn`; a repo-hygiene test now bans it, and every notice goes through the
> `Notice` component that announces itself. Stage 5 and §3 say to add a rule 28 exception to
> `CLAUDE.md`; rule 28 lives in `.claude/rules/backend.md` and already carries one. Four write
> targets point at `docs/PLAN.md`, deleted in `4eccb71`.
>
> The body also contradicts itself on a wired field name — §4.4 says `held_back_unknown_size`,
> which is what shipped, while §4.7 and Stage 3b say `held_back_unmeasured`, which is only a
> structlog event. Every cross-file `file:line` citation drifted long before the freeze.
>
> **What it is still good for**: §2 and §3 are the reasoning behind a column that is load-bearing
> on the deletion path, and the header lessons below — re-read every docstring asserting an
> invariant a later stage relaxes, and the browser pass earns its cost — are why the shipped
> stages landed correct.



Status: **Stages 1, 2, 3, 3b and 3c landed 2026-07-19.** Written 2026-07-19 for execution by
another agent session; §1 and the Stage 1/2 boundary have since been corrected against the
shipped code (see the notes marked **Corrected**).

Stage 3c was mocked as an HTML artifact and approved before any frontend code, per
`CLAUDE.md`'s golden rule. Stage 4 is the operator's own pass against real data, and
Stages 5 to 7 are gated on what its tally shows.

**An adversarial review pass ran over the whole change set** (six lenses, every finding
put to an independent refuter). It confirmed one CRITICAL defect the gates and the browser
both missed, and it is the most useful thing in this document for a later session:

> **Using the allowance once bricked every run for the next thirty days.**
> `_rolling_30d_deletions` raised if any past verified delete joined back to a NULL size.
> That was correct until §4.6 made such deletions happen by design — and the candidate row
> keeps its NULL forever, so from the first allowed deletion every later run, dry runs
> included, aborted with no way out. The docstring asserting it "cannot occur here" was
> written before the allowance existed and became a rule 7/24 violation in the same commit.
> The resolution: an unmeasured past deletion counts as an ITEM (it spends the monthly item
> budget, which is the only thing bounding that population) and contributes no bytes.
> Aborting is the wrong fail-closed; skipping the row is the dangerous one.

Three more the same pass confirmed: the canary rule held only *relatively* (unmeasured
items sorted last, but with nothing measured the tail was the whole plan, so ordinal 0 had
unknown cost — a plan with no measured item is now refused outright); the executor read the
allowance as a boolean, so lowering 25 to 1 was silently ignored; and `omitted` was decided
up front, which made `held_back_unknown_size` always 0 with the allowance on — so turning
the setting ON made the plan *less* honest than leaving it off, exactly inverting §4.7.

**Lesson for the staging.** Every one of these lives at the seam between §4.6 (the
allowance) and a §4.3 rule written before it. When a later stage relaxes an invariant an
earlier stage established, re-read every docstring that asserts the invariant — they are
where the bugs are, not merely where the prose is stale.

**Driven end to end in a browser** against a seeded snapshot mixing measured and
unmeasured items. Three defects surfaced there that no test caught, all now fixed: the
review queue header rendered a bare sum because a local variable shadowed the formatter;
that header also needed its own wording, since the shared middot form put "would be freed"
after a count of items that would NOT be freed; and policy warnings were being inspected
against a stand-in `ProfileSettings()` rather than the operator's own, which had made every
settings-based warning unreachable — including the pre-existing danger about running
without approval. Record this: the browser pass earns its cost.

> **CORRECTED 2026-07-26 — do not follow the paragraph this replaces.** It told you to delete
> `data/reaper.db` and upgrade from empty. That was written pre-release; testers now run Reaper
> on real data and **never rebuild their database**. The heal migration
> `alembic/versions/20260723_1000_heal_candidate_size_nullable.py` already upgrades an
> in-window database in place, and its own header ends "Testers never rebuild their database."
> Run `alembic upgrade head` and keep your data.

This plan removes the last place Reaper invents a number on the deletion path. It is the
"correct fix, no workarounds" version of the size problem, superseding the display-only
mitigation shipped in `406630b` (`format.itemBytes`), which this plan deletes.

Read `CLAUDE.md` first. Every rule number cited below is from it.

---

## 1. The defect

`Candidate.size_bytes` is `Mapped[int]`, NOT NULL, client-side `default=0`
(`src/reaper/db/models.py:350`). When Sonarr or Radarr do not report a size, the scan
stores `0` at exactly two lines:

- `src/reaper/services/snapshot.py:653` — `size_bytes=item.size_bytes or 0`
- `src/reaper/services/season_scan.py:1143` — `size_bytes=season.size_on_disk or 0`

So a stored `0` means both "genuinely zero" and "nobody would tell us". The scoring lane is
already honest about this: `Facts.size_bytes` is an `Observation[int]`
(`src/reaper/engine/gates.py:153`) and an `Unknown` correctly lowers the score and coverage.
The dishonesty is confined to the display and accounting column.

> **Corrected 2026-07-19.** This section originally listed four live defects on the
> deletion path. Three of them were closed by review finding B-5 (`executor.size_confirmed`)
> before this plan's first stage began. What follows now separates what is *fixed* from what
> is still open, because a plan that describes a defect no longer present sends a later
> session hunting for it.

**Closed by `executor.size_confirmed` (`executor.py:186`).** It reads a stored `0` as "never
confirmed" and refuses the item in two independent places: `_deletable` keeps it out of both
byte caps and out of the byte total behind the confirmation phrase, and `_send_movie` /
`_send_season` skip it per item before the growth check runs. So:

- **Both byte caps** are exact. An unconfirmed item is excluded, not counted as zero.
- **The typed confirmation phrase** is exact, and `api.runs._planned_candidates` applies the
  same filter, so the number shown and the number acted on describe one set (rule 30).
- **The growth interlock** is never reached with a fabricated baseline. This mattered because
  `_grew_materially(0, live)` reduces to `live > _SIZE_DRIFT_FLOOR`, so with
  `_SIZE_DRIFT_FLOOR = 256 * 1024**2` it does **not** trip for any live total at or under
  256 MiB. The check still has that hole; nothing with an unconfirmed size now reaches it.

The comments at both persist sites were rewritten in the same change and now cite
`size_confirmed` rather than claiming the growth check catches everything.

**Still open, and what this plan is for:**

1. **The canary.** The planner still orders smallest-first (`planner.py:18-20`, `:270-272`)
   over rows including fabricated zeros, so ordinal 0 can be an item whose size was never
   read. The executor then skips it, so no unmeasured file is deleted, but the run's stated
   test item is not the item actually attempted, and the docstring's "least costly possible
   mistake" is still aspirational. The planner refusal (§4.2) is what makes it true.
2. **Everything downstream of the column is a workaround, not a fix.** `size_confirmed`
   works by reading `0` as a sentinel, which is precisely the design §3 rejects: it holds
   only because every call site remembers to ask. A NULL makes the unsafe call sites raise
   instead of quietly summing. Its own docstring says so and defers to this plan.
3. **The operator is told nothing.** An item is silently dropped from the plan with no count,
   no per-item reason, and no way to find out which (§4.7).

---

## 2. The governing principle

> **The stored size must measure the quantity the delete would free. A bound is not a
> measurement. An item Reaper cannot measure is not plannable unless the operator has
> deliberately allowed it, and even then it is bounded by count and can never be the canary.**

This single rule resolves the whole design, and it is what makes the plan *correct* rather
than a patch. Its consequence is that every downstream number becomes exact **by
construction** rather than by patching six sums: because the plan can contain no unknowns,
`manifest_hash`, `confirmation_phrase`, both caps, `deleted_bytes` and the reclaim figure all
operate on plain ints, and rule 30 ("any number shown beside a destructive button must be
derived from the same set the server will act on") is satisfied structurally.

The refusal is not new. It formalises one the executor already makes: `_send_movie` keeps any
movie whose live size is unreadable (`executor.py:1090-1096`), and `_send_season` keeps any
season Sonarr will not fully size (`executor.py:1229-1235`). The only capability actually
lost is deleting an item against a fabricated baseline, which is the bug.

The allowance in §4.6 exists because "never" is the wrong answer for an operator who has a
handful of items their \*arr will not size and who wants them gone anyway. It defaults to off,
it is a **count** rather than a switch (because the byte caps cannot bound an unmeasured item,
so the count is the only bound there is), and it never relaxes the canary rule.

### 2.1 What "the quantity the delete would free" is, per media type

This is where an earlier draft of this design went wrong, and the correction is the most
important content in this document.

**Movies.** The delete is `DELETE /api/v3/movie/{id}` with `{"deleteFiles": true}`, which
removes the movie **folder**. The folder is what `sizeOnDisk` measures — and Reaper's own
docstring already says so, in as many words:

> `sizeOnDisk` covers the movie's folder (file plus extras), which is the number the reclaim
> estimate and the byte cap want. Distinct from `_movie_file_size`, which reads
> `movieFile.size` for file-to-file identity comparison.
> — `snapshot.py:1209-1219`

So for movies, **`sizeOnDisk` is the only qualifying measurement.** `movieFile.size` and
Plex's `Media/Part@size` measure the *file*, which is a **lower bound** on the folder. They
are excellent for identity (`_movie_file_size` exists for exactly that, and
`docs/LEARNINGS.md:455-461` records them byte-equal on a live library) and they are **not**
substitutes for the folder on the accounting lane.

**Seasons.** The delete is per-file: `DELETE /api/v3/episodefile/{id}` for each episode file.
So the quantity freed is the **summed episode-file bytes** — exactly what
`executor._send_season` already computes live at `executor.py:1220-1236`. Sonarr's
`seasons[].statistics.sizeOnDisk` is the season *folder*, a different quantity.

**This inverts the season ladder, and fixes a pre-existing bug.** Today the scan stores
`statistics.sizeOnDisk` (folder) and the executor compares it against the summed live episode
files (`executor.py:1237`). Those are two different measurements, so the season growth
interlock has been systematically desensitized since it was written — the folder side is
larger, so the comparison reads as "it shrank" and the check does not fire. Sourcing the
frozen size from the summed episode files makes both sides the same measurement for the first
time. **Stage 5 is therefore a bug fix, not merely an acquisition improvement.**

### 2.2 The corrected acquisition ladder

The operator's stated ordering — the \*arrs first, then Plex — is honored. What changes is
what each rung is *allowed to be used for*.

| Media | Rung | Source | Measures | Plannable? |
|---|---|---|---|---|
| Season | 1 | Sonarr `episodefile?seriesId=` summed for the season | files deleted | **yes** |
| Season | 2 | Sonarr `seasons[].statistics.sizeOnDisk` | season folder | **yes**, see note |
| Movie | 1 | Radarr `sizeOnDisk` | movie folder deleted | **yes** |
| Movie | 2 | Radarr `movieFile.size` | the file only | **no — lower bound** |
| Movie | 3 | Plex `Media/Part@size` on the matched listing | the file only | **no — lower bound** |
| either | — | nothing | — | held back |

Season rung 2 stays plannable because the folder is a close proxy for the files and it is
today's behavior; rung 1 is *preferred* when available because it is exact and it repairs the
interlock. Movie rungs 2 and 3 are recorded for **display only** (§4.5) and never make an item
plannable.

**Why a lower bound cannot be plannable.** A lower bound under-counts a byte cap, and a cap
that under-counts does not fire, which deletes more than the operator allowed. It also biases
the canary: a file-sourced size is systematically smaller than the folder-sourced sizes it
sorts against, so the items with the *weakest* measurement drift toward ordinal 0 —
reintroducing the exact defect this plan removes, with a plausible number instead of a zero.
Note the direction trap here: **rule 31 ("derived condemn-lane values round toward keeping")
governs scoring pressure, where under-stating is safe. For a cap it points the opposite way.**
Do not cite rule 31 to justify an under-stated size on the accounting lane.

---

## 3. Decisions already taken

Recorded so a later session does not relitigate or "restore" them. Each was argued through a
three-design panel and four adversarial critics.

| Decision | Why |
|---|---|
| Nullable column, **not** a sentinel (`-1`, or `0` + a `size_known` bool) | A sentinel is the same bug with a different number: every arithmetic site silently accepts it and produces a wrong total. A NULL makes unsafe call sites raise, which is the loud failure we want. The repo already chose nullability over a sentinel for this exact problem in `watch_event.watched_status` (`history_sync.py:114-119`, `:366-373` — "Deliberately NOT `float(value or 0)`"). |
| Refuse at the **planner**, not the verdict | Rule 22 keeps the condemn/abstain/protect decision in exactly one function. The scoring lane is already honest about size. Feeding an accounting column back into the verdict would put a second size rule in the decision path and make the score depend on which acquisition rung fired. The refusal is a property of what can be safely *acted on*, not of what the evidence says. |
| Store `size_source`; it is **load-bearing**, not decoration | The ladder mixes quantities, and `_grew_materially` compares stored against live. Without provenance the executor cannot pick the matching live quantity. |
| Do **not** expose `size_source` on the wire | Operator jargon (rule 21). The UI says "Size unknown", never a source enum. |
| Per-item size miss does **not** call `context.degrade` | Degradation is snapshot-global (`snapshot.py:591-592`) and a degraded snapshot is un-plannable outright (`planner.py:257-262`), so one unreadable movie would block the operator's entire run. Wrong blast radius. **This is a deliberate exception to rule 28 and must be written into `CLAUDE.md`'s rule text**, the way rule 2 names P-6 and rule 33 names `discord.py`. A code comment is not sufficient. The compensating control, named at the `except`, is the planner refusal. |
| No new Radarr movie-file endpoint | Unverifiable from this repo — no OpenAPI spec is vendored, and `RadarrClient` wraps no such route. Not needed: `movieFile` is already in the list payload, proven by four helpers that parse it off list elements (`snapshot.py:1189`, `:1202`, `:1230`, `:1249`). |
| No `includeEpisodeFile=true` on the episodes call | Same reason: unverified from this repo. `episodefile?seriesId=` is verified by production use in `executor._send_season`. |
| No Plex season/episode fetch | A show listing carries no `Media` (`plex.py:585-598`), `PlexClient` has no children method, season rating keys come from Tautulli, and doing it through plexapi objects would re-introduce the measured per-item reload blowup (`plex.py:501-512`). Sonarr's episode files are cheaper and authoritative. |
| No Tautulli `file_size` | Already known-bad and already decided against: it reports `0` for every show-level row and lags for movies (`docs/PLAN.md:995-1004`, `tests/test_fairness.py:110-115`). Adopting it reintroduces the zero-means-unknown trap. |
| The allowance is a **count** on `ProfileSettings`, not a boolean, and not on `Policy` | The byte caps cannot bound an unmeasured item, so a switch would leave the population unbounded. The count is the only bound available. `ProfileSettings` is "how much Reaper may do" and is out of the hash; `Policy` is hashed evidence-and-rules and would void every pending approval on a change. |
| The allowance never relaxes the canary rule | The canary is a first mistake of known cost. An unmeasured canary is the original defect wearing a setting. No configuration may reintroduce it. |
| Exceeding the allowance **aborts** the plan | Truncating to the first N would let sort order pick which unmeasured file dies. Matches `_check_caps`'s abort-not-truncate discipline (rule 5). |
| Do not read the truth from `facts_json` per consumer | It is nullable by design (`models.py:438-443`), so it cannot be a sole source, and it splits one fact across two stores that can disagree. The column is the accounting surface and should hold the accounting truth. |
| Never **sum** `PlexItem.files`, never sum across `merged_rating_keys` | Both over-state and push a number **upward** on the deletion lane. `_parse_sweep_element` flattens every Part of every Media into one tuple (`plex.py:108-121`), so an optimized copy inflates the sum; merged listings are byte-identical twins of one file (`identity.py:405-410`), so N listings gives N times the bytes. |

---

## 4. The design

### 4.1 Storage

`src/reaper/db/models.py`, `Candidate`:

- `size_bytes: Mapped[int | None] = mapped_column(Integer, default=None)` (from
  `Mapped[int] ... default=0` at `:350`). Docstring states NULL means Reaper could not
  measure what the delete would free, that it is **not** zero, and cites
  `planner.build_plan`'s refusal and `executor._send_movie` / `_send_season` as the two
  independent layers that keep an unknown out of a delete (rule 24).
- New `size_source: Mapped[str | None]`, NULL exactly when `size_bytes` is NULL.

**`size_source` must be a `StrEnum`, not a bare `String(16)`.** An unconstrained string on the
deletion path means the executor's comparator selection resolves an unrecognized value in an
unwritten `else` branch — either falsely skipping every such item, or bypassing the growth
interlock. Define the enum beside the column, and make the executor's selection an
**exhaustive match with a fail-closed default: an unrecognized or NULL source keeps the item**
(rule 2). Values:

| value | quantity | media |
|---|---|---|
| `sonarr-files` | summed episode files | season |
| `sonarr` | season folder statistics | season |
| `radarr` | movie folder | movie |
| `radarr-file` | movie file only — **lower bound, display only** | movie |
| `plex` | movie file only — **lower bound, display only** | movie |

Precedent for a nullable string whose NULL means "could not check": `Candidate.show_status`
(`models.py:388-397`).

> **CORRECTED 2026-07-26.** The paragraph below describes a baseline that no longer exists.
> The baseline is now **frozen** — its own header says so, and every schema change is its own
> additive revision chained onto head. Nothing here may be taken as license to edit it or to
> regenerate from empty. Kept only because the stage notes below refer back to it.

**Migration mechanics (as written 2026-07-19, now superseded).** The project is pre-release at
a single Alembic baseline that is **rewritten in place** — the baseline's own header says "edit
the models, delete `reaper.db`, delete this file, regenerate, and upgrade from empty"
(`alembic/versions/20260714_1840_baseline_schema.py:1-19`). No new revision file.
`tests/test_migrations.py:34-38` (one head) stays green.

**Corrected:** this originally said to do both column changes in ONE regeneration. They
belong to different stages (see Stage 1's note), so the baseline is regenerated twice, once
per column. Regenerating a rewritten-in-place baseline from a disposable DB is free; shipping
a stage with red gates is not. Stage 1's change was made by hand-editing the baseline and
proving it with `alembic upgrade head` then `alembic check` against a **fresh throwaway DB**
(`REAPER_DATA_DIR` pointed at a temp dir), which reported no drift.

Two traps:

- The Python-side `default=0` is client-side only, so the baseline renders just
  `nullable=False` today and the flip is a clean `nullable=True` with no `server_default` to
  fight — avoid re-introducing one.
- Run `alembic upgrade head` then `alembic check` **against a fresh database**. The long-lived
  dev DB already fails `alembic check` on a pre-existing `instance.verify_tls` server-default
  quirk (`docs/PLAN.md:462-464`, `:584-588`); do not misattribute that failure to this work,
  and **do not delete the operator's `data/reaper.db` to make it pass.**

Existing stored zeros are **not** reinterpreted or backfilled. They are irrecoverably
ambiguous, Candidate rows are snapshot-scoped, and the next scan regenerates them. This is the
same call the repo already made for `watch_event.watched_status` (`history_sync.py:194-197`).

### 4.2 Planner — where the governing rule lands

`src/reaper/services/planner.py`:

- **Partition after `effective_condemned` (`:288`):**
  `measured = [c for c in effective.values() if c.size_bytes is not None]` sorted by
  `int(c.size_bytes)`; `unmeasured = [...]` for the rest. With the allowance at its `0` default
  (§4.6) the unmeasured list is held back entirely and `plannable = measured`. Above zero,
  `plannable = measured + unmeasured` **in that order**, so the unmeasured tail always sorts
  last and ordinal 0 is a measured item under every configuration. The docstrings at
  `planner.py:18-20` and `executor.py:45-47` become true rather than aspirational.

  Write the ordering as one expression with a comment naming the invariant. Do **not** sort the
  combined list by a key that treats `None` as a number, and do not rely on a stable sort to
  keep the tail in place: the invariant is "no unmeasured item precedes a measured one", and it
  should be obvious from the code rather than emergent from sort behavior.
- **Delete the SQL `.order_by(Candidate.size_bytes.asc())` at `:273`; replace with
  `.order_by(Candidate.media_key)`.** `all_condemned` feeds only `manifest_hash` (which sorts
  internally) and a `condemned_keys` set, so the ascending sort is dead weight — and once the
  column is nullable it is an active trap, because **SQLite sorts NULL FIRST on ASC** (verified
  in this environment: `[NULL, 0, 1, 5]`), which would preserve the exact canary bug being
  removed. Do not bolt a nulls-last guard onto a dead sort. Move the smallest-first comment
  down to `:289`, where the ordinal is actually decided.
- **Group expansion must draw from the plannable set, not `effective`.** `:318-334` expands a
  requested `group_key` to its member seasons, then `spared = requested - plannable_keys`
  raises. Because `members_by_group` is built from `effective`, a held-back season would join
  the expansion and then trip that refusal — making "Reap now" fail outright on any show with
  one unmeasured season. Build `members_by_group` from the plannable set so held-back members
  are silently omitted from an expansion, exactly as spared members already are (the precedent
  and its reasoning are at `:325-331`).
- **An explicitly-named held-back key still refuses loudly**, in the same three-way shape as
  the existing unknown/spared refusals (`:333-344`), with its own message: `"Reaper couldn't
  measure the size of these items, so it won't reap them: {...}. Check them in Sonarr or
  Radarr, then run a new scan."` Never silently drop a key the operator named (rule 1).
- **`manifest_hash` (`:96-115`)** encodes an unknown as JSON `null`, distinct from `0`, so a
  size later measured voids the approval as it should. The payload sorts by the unique
  `media_key` first, so no `None`-vs-`int` comparison occurs. The docstring's claim "Integers
  only, like every other hash in Reaper" (`:111`) becomes false and must be corrected in the
  same commit (rule 7, rule 24). It keeps hashing **all** condemned rows, held-back included:
  the hash binds the frozen set's integrity, not the plan.
- **`confirmation_phrase` (`:116-124`) does not change shape**, and a comment must say why:
  it is exact *precisely because* unmeasured items were held back. A later contributor adding
  an unknown count to the phrase would break the byte-for-byte recompute at `runs.py:265` and
  **409 every execute**.
- `build_plan` returns the held-back count through to `ReapRun` / `RunOut`.

### 4.3 Executor — the second independent layer

`src/reaper/services/executor.py`:

1. **Defensive skip.** At the top of `_send_movie` (before the live re-read at `:1089`) and
   `_send_season` (before `:1220`): if `candidate.size_bytes is None` **and the allowance
   (§4.6) is `0`**, mark skipped with `"Reaper couldn't measure this item's size, so it can't
   confirm what would be removed. Kept."` The planner should already have held it back; this
   exists because the host-side layer must never depend on the plan being correct.

   **Re-read the allowance here, at execute time, not from the plan.** An operator who lowers
   it to `0` between approval and execute gets those items kept, which is the safe direction.
   Raising it after approval can add nothing, because the items were never planned. Both
   directions resolve toward keeping, which is why this is safe to read late.
2. **Like-for-like comparison.** `_grew_materially(approved, live)` must compare the same
   quantity, selected by `candidate.size_source` in an exhaustive match with a fail-closed
   default (§4.1). `sonarr-files` compares against the summed live episode files
   (`:1220-1236`); `sonarr` compares against the same sum **and this is the pre-existing
   mismatch described in §2.1** — Stage 5 removes it by preferring `sonarr-files`. `radarr`
   compares against live `sizeOnDisk`, today's behavior. When the live counterpart is
   unreadable, **keep**, exactly as the existing branch does (`:1090-1096`) — never fall
   through to a different measurement.
3. **Caps assert, they do not assume.** `_check_caps` (`:401-423`) and `_check_rolling_caps`
   (`:683-713`) sum over `_deletable(...)` (`:386-398`). With the allowance at `0` that set
   contains only measured sizes; raise `ExecutionError` if any deletable candidate carries a
   NULL size, aborting before anything is sent. This is operator-invisible and must not be
   dropped as dead code: it is the only check that catches a future regression in the planner
   filter. With the allowance above `0`, the assertion becomes "no more than the allowance",
   and unmeasured items are **excluded from the byte sums but included in the item counts**
   (§4.6 rule 2). Both cap functions must make that split explicit rather than letting a NULL
   fall through arithmetic.
4. **The rolling cap's past side.** `_rolling_30d_deletions` (`:715-745`) selects
   `Candidate.size_bytes` over verified terminal steps. A NULL cannot occur there, but if one
   does the cap is unenforceable — **abort, do not skip the row**. A silently skipped NULL
   spends past the budget.
5. `report.deleted_bytes += int(...)` (`:786`) and `_gb` (`:189-190`) need no unknown
   handling, because only measured sizes reach a delete. This is the payoff of refusing at the
   planner rather than patching each consumer.
6. **Interlock 8's module docstring (`:62-66`)** currently describes only the live size being
   unreadable. Extend it to name the approved-size case and cite the planner refusal, or it
   becomes a safeguard claimed only in prose (rule 24).
7. **Lift `_payload_size` (`:149-169`) and `_season_number` (`:172-186`) into a shared
   module** — `src/reaper/clients/arr_size.py` — with docstrings intact (they record why zero
   counts as unreadable), and import them back. The scan's season acquisition must use the
   identical zero-is-unknown and season-attribution rules, not a transcribed copy (rule 3,
   rule 22). Placement inside `clients/` keeps rule 33 satisfied.
8. **Note, do not change:** the UI's canary badge is `s.ordinal == 0` (`runs.py:124`) while
   the executor's real canary is the first item actually *attempted* (`:750-756`, `:781`).
   These can already disagree; this work does not widen the gap. Record it, leave it.

### 4.4 API

`src/reaper/api/schemas.py` — widen `CandidateOut.size_bytes` (`:183`),
`GroupSeasonMarkOut.size_bytes` (`:173`, drop the `= 0` default), `GraceItemOut.size_bytes`
(`:626`) to `int | None`.

**Aggregates keep a definite `int` and gain a companion count.** Do *not* widen
`GroupOut.size_bytes` to `int | None`: two signals for one fact forces every consumer to
handle both a null and a count. A known-only sum beside an unknown count is one pattern,
reused everywhere. Add `GroupOut.unknown_size_seasons`, `SnapshotOut.unknown_size_items`,
`SimulationOut.unknown_size_items`, `RunOut.held_back_unknown_size`, all `int = 0`.

**The SQL hazard, verified in this environment (SQLite 3.53.3): `SUM` silently skips NULL
rows, and `COALESCE(SUM(x), 0)` does not detect them.** The two coalesced aggregates at
`routes.py:130` and `routes.py:278` would go from honest-but-wrong to *silently* wrong. Each
gains a sibling `func.count().filter(Candidate.size_bytes.is_(None))` in the **same query over
the same conditions**, preserving the "built once, applied to both" discipline documented at
`routes.py:225-226`.

Python accumulators that must sum the measured members and carry an unknown count alongside:
`routes.py:867`, `routes.py:399-400` and `:417-418` (`_group_rollups`), `routes.py:1195`,
`:1344`, `:1381` (simulator), `api/runs.py:110`, `services/grace.py:116`, `:125`, `:130-131`.

**`_group_rollups` must filter the COUNT, not just the bytes.** It increments `count + 1` for
every condemned non-spared member with no size predicate. A show with 8 condemned seasons of
which 2 are unmeasured would render "Reap now (8 items)" while `build_plan` emits 6 steps. Its
own docstring at `routes.py:344-352` explicitly claims rule-30 compliance, so leaving the count
unfiltered also makes that a rule 7/24 comment that no longer describes the code.

**Sorting:** `routes.py:289` and `:299` sort by size on an operator-driven control. Place NULLs
explicitly on **both** directions; use the portable
`.order_by(Candidate.size_bytes.is_(None), ...)` form rather than `nullslast()`, so no SQLite
version assumption is baked in.

**List headers** cannot carry null: add `X-Unknown-Size-Count` beside `X-Total-Count` /
`X-Total-Bytes` (`routes.py:283-284`). The frontend already defends a missing header with
`?? 0` (`api.ts:861`).

Do **not** overload `CandidateOut.group_condemned_bytes` (`schemas.py:201`), which means "not
applicable, this is a movie"; otherwise the frontend's `?? fetchedSize` fallback
(`ReviewQueue.tsx:1013`) substitutes a partial page sum for an unknown.

Precedent worth following: the simulator already zeroes its reclaim on a stale result
(`routes.py:1314`) under a stated house rule — "Reaper would rather show nothing than show a
number it cannot stand behind" (`schemas.py:539-542`).

### 4.5 Frontend

`frontend/src/api.ts` — widen `Candidate.size_bytes` (`:57`), `GroupSeasonMark.size_bytes`
(`:49`), `GraceItem.size_bytes` (`:501`) to `number | null`; add the new count fields and read
`X-Unknown-Size-Count` with `?? 0`.

`frontend/src/format.ts`:

- `itemBytes(value: number | null)` returns `value === null ? "Size unknown" : bytes(value)`.
  **Delete the `value > 0` heuristic** at `:31-33` and rewrite the docstring — its sentence
  "one unreadable item in a sum makes the total low, not unknown" is exactly what this plan
  retires. After this change `itemBytes(0)` correctly renders `"0 B"` again.
- Add `totalBytes(known: number, unknown: number)`: `bytes(known)` when `unknown === 0`,
  otherwise the comma form **`"4.2 TiB, 3 sizes unknown"`**. This said "middots are the
  approved separator (rule 21)", which was true when it was written and is not now: a middot
  is decoration a screen reader may voice mid-sentence, so rule 21 takes a comma (#177). There
  is no "at least" precedent in the UI, and the comma form fits the nowrap size cells.

**Every unknown count is suppressed at zero.** An operator with a fully healthy library must
see no new pixels anywhere.

> **Stale citations, relocated.** `GracePanel.tsx` no longer exists anywhere in
> `frontend/src/`, and its three roles below landed in two different components. Every
> `file:line` number in this section has drifted too — treat them as names to grep for, never
> as positions.
>
> - The fifth per-item `itemBytes` render (`GracePanel.tsx:39`) → `ScalesPanel.tsx`. The
>   non-test set is still exactly five: `ReviewQueue.tsx` (twice), `ScalesPanel.tsx`,
>   `WhyPanel.tsx`, `ShowPanel.tsx`.
> - The aggregate byte renders (`:140`/`:148`) and the `.notice.notice-warn` pattern (`:126`)
>   → `ReapBreakdown.tsx`.
> - **No successor exists for the per-item grace view itself.** There is no per-item countdown
>   surface in the SPA at all: no `/api/grace` route, and `days_remaining` / `grace_ends_at`
>   appear nowhere in `frontend/src/`. `grace_days` reaches the frontend only as a policy
>   setting (`PolicyEditor`, `PolicySimulator`, `policyPresets`). Stage 6 must decide where that
>   render belongs rather than assume a home.
>
> Two items this section still lists as work are **already shipped**: `totalBytes` exists in
> `format.ts`, and the whole-show `bytes()` contradiction in `ShowPanel.tsx` is fixed — it
> calls `totalBytes` now.

Call sites: the five per-item renders already on `itemBytes` need no edit but must typecheck
(`ReviewQueue.tsx:863`, `:944`, `ShowPanel.tsx:113`, `WhyPanel.tsx:746`, `GracePanel.tsx:39`).
Fix the visible contradiction at `ShowPanel.tsx:73`, which renders a whole-show total with
`bytes()` directly above per-season `itemBytes`. The two in-browser reduces at
`ReviewQueue.tsx:1005-1006` must filter nulls and count them — a careless `?? 0` there restores
exactly the silent under-count being removed. Aggregate renders taking a count:
`ReviewQueue.tsx:1687`, `GracePanel.tsx:140`/`:148`, `ScanBar.tsx:133`,
`PolicySimulator.tsx:125`, `ReapPlan.tsx:167`, `ReapConfirm.tsx:91`.

`ScanBar.tsx:45` subtracts two totals whose unknown populations may differ. **Do not silently
drop the delta** — a line the operator is used to must never vanish without saying why. Render
it qualified: `"3.1 TiB less to free, 2 sizes unknown"`.

When `run.held_back_unknown_size > 0`, `ReapPlan.tsx` and `ReapConfirm.tsx` render the
**shared `.notice.notice-warn`** beside the reap control (the pattern at `GracePanel.tsx:126`,
`ReapConfirm.tsx:126`), never bare `.error` text (rule 42, rule 18). Copy:
**"N items held back. Reaper couldn't measure their size, so it won't delete them."**
Compact form where space is tight: **"2 kept, size unknown"**.

In `WhyPanel`, add the reason to the existing `.sig-unreadable` amber row — the **plain reason
only**, never the source enum and never a timestamp: **"Size unknown: Sonarr and Radarr had
none."**

No new control is introduced, so rules 40/41/44 are not engaged.

**Movie lower bounds (rungs 2 and 3) surface here and nowhere else.** If Stage 6 lands, a
movie with only a file-bytes bound renders its size qualified and is still held back from the
plan. Do not let a bound leak into any aggregate, cap, or the confirmation phrase.

### 4.6 The unmeasured allowance — a policy setting

**Where it lives.** `ProfileSettings` in `src/reaper/engine/policy.py:620-656`, beside the four
caps. That class is exactly "how much Reaper may do", and it is deliberately **out of the
policy hash** (`:623-626`).

**Read the out-of-hash rationale carefully before adding to it.** It says tightening a cap is
always safe, so voiding pending approvals over one would train operators to stop reading the
diff. That argument does **not** hold for a field that *loosens* what may be deleted. It is
still correct to keep this field out of the hash, but for a different reason, and the docstring
must say so: the allowance is consumed at **plan construction**, so raising it cannot add items
to an already-approved plan, and lowering it to `0` before execute causes the executor to keep
those items (§4.3.1). Both directions resolve toward keeping. Write that down or a later reader
will assume the tightening argument covers it.

**The field:**

```python
max_unmeasured_per_run: int = Field(default=0, ge=0, le=25)
```

`0` is the default and means today's behavior: an item Reaper cannot measure is never
plannable. Above zero, up to that many unmeasured items may be planned per run.

**Why a count and not a boolean.** An unmeasured item contributes nothing to
`max_bytes_per_run` or `max_bytes_per_30d`, because there is nothing honest to add. The byte
caps therefore cannot bound this population at all, and a plain on/off switch would leave it
unbounded. The count **is** the bound, which is also why the ceiling is low (`le=25`) and why
the help text has to say the GB caps do not cover these.

**Rules that do not relax, whatever the allowance is set to:**

1. **Never the canary.** Unmeasured items sort **last** within the plannable set, so ordinal 0
   is always a measured item. The canary's entire purpose is a first mistake whose cost is
   known in advance; an unmeasured canary is a contradiction in terms. This is the defect the
   plan exists to remove and no setting may reintroduce it.
2. **They still count as items.** `max_items_per_run` and `max_items_per_30d` include them.
   Only the byte caps cannot.
3. **They never join a group expansion.** "Reap now" on a show expands to its measured seasons
   only (§4.2). An unmeasured season enters a plan only through an explicit whole-set or named
   reap, never by riding a show-level click the operator did not aim at it.
4. **The confirmation phrase names them.** With unmeasured items in the plan the phrase gains a
   suffix: `REAP 6 ITEMS 41 GB + 2 UNSIZED`. The GB figure stays exact for the measured items,
   and the operator has to type an acknowledgment that the run contains items the figure does
   not cover. Both `runs.py:111` and `:265` call the same function, so the recompute stays
   byte-identical; update the `ReapConfirm.test.tsx:31` fixture (trap 4 in §6).
5. **A new validator**, in the shape of the existing `_run_cap_within_rolling_cap`
   (`policy.py:647-656`): `max_unmeasured_per_run` may not exceed `max_items_per_run`, with a
   plain-language message saying why the setting would otherwise be meaningless.
6. **Exceeding the allowance aborts, it does not truncate.** If a plan would contain more
   unmeasured items than allowed, refuse the whole plan with a message naming the count, in the
   same shape as `_check_caps` (`executor.py:412-422`). Silently planning the first N would let
   the choice of *which* unmeasured item gets deleted fall to sort order, which is exactly the
   accident this plan removes.

**Policy editor UI** (`PolicyEditor.tsx`, rules 40/41/44): the caps already render as a group,
and this joins them as a `FixedQuantity` with the unit `per run` in the same box. Not a
`Segmented`, not a bare number input, no new control. Copy, short enough to read at a glance:

- Label: **"Items with an unknown size"**
- Help, bound to that one control: **"Reaper keeps these by default. It can't measure them, so
  the GB caps won't limit them. Set 0 to always keep them."**

**Warning surface.** Add a `PolicyWarning` (`policy.py:659`) when the value is above zero,
anchored to the field so it renders beside the control (rule 42): **"Reaper will delete up to
N items it can't measure. The GB caps won't cover them."** This is a legal config that is
probably not what most operators mean, which is precisely what `PolicyWarning` is for.

### 4.7 Logging and operator feedback for a held-back item

A silent hold-back is its own kind of dishonesty: the operator sees a smaller plan than they
expected and is told nothing. Three surfaces, and none of them is optional.

**1. Structured logs.** Reaper uses `structlog` with dotted event names. Add, at the points
where the fact is first known:

| event | where | fields |
|---|---|---|
| `scan.size_unmeasured` | the two persist sites, once per item | `media_key`, `media_type`, `reason` (which rungs were tried and came back empty) |
| `scan.size_source_tally` | end of scan, once | count per rung, plus the unmeasured count |
| `planner.held_back_unmeasured` | `build_plan`, once per plan | `count`, `media_keys` |
| `planner.unmeasured_allowed` | `build_plan`, when the allowance admits items | `count`, `allowance` |
| `executor.skipped_unmeasured` | the defensive skip (§4.3.1) | `media_key` — this one should never fire, so it is a real alarm when it does |

The tally is the measurement Stage 4 depends on. Record ratios and shapes; never a title,
path or host (golden rule).

**2. Per-item, in the review queue and the why panel.** A count alone does not tell the
operator *which* items, and the queue is where they are already looking. The item keeps its
condemned verdict (the evidence still says delete) but carries a held-back state:

- Queue card: the existing status-line pattern, reading **"Held back: size unknown"**.
- Why panel: in the existing `.sig-unreadable` amber row, the plain reason only, never a
  source enum and never a timestamp: **"Size unknown: Sonarr and Radarr had none."**
- Both must be derived from `size_bytes is null`, not from a second stored flag that can drift.

**3. On the plan and confirm screens.** The `.notice.notice-warn` from §4.5, which is where an
operator about to reap finds out the plan is smaller than the queue implied:
**"2 items held back. Reaper couldn't measure their size, so it won't delete them."**
When the allowance admitted items instead, the same notice slot says:
**"2 items with an unknown size are included. The GB caps don't cover them."**

**4. The run report.** `RunOut.held_back_unmeasured` carries the count so a completed run can
say what it did not do. A run that deleted 6 of 8 condemned items must be able to explain the
other 2 after the fact, not only before it.

All copy above is plain language with no em dashes (rule 21), and every count is suppressed at
zero so a healthy library shows nothing new (§4.5).

---

## 5. Stages

Ordered so that **consumers learn to handle NULL before anything emits one**, and so that
acquisition (low risk, shrinks the held-back set) precedes the refusal wherever it can.

Each stage must leave every gate green: `uv run ruff check .`, `ruff format --check .`,
`mypy src/reaper`, `pytest`, `alembic upgrade head` + `alembic check` (fresh DB),
`npm --prefix frontend run lint`, `test`, `build`. Docker builds in CI only.

### Stage 1 — provenance and the tally, emitting nothing new — **DONE**

> **Corrected during execution.** This stage was written to flip `size_bytes` to nullable
> *and* add `size_source` in one baseline regeneration. That does not work: widening the
> column's type turns mypy red at **22 consumer sites immediately**, whether or not a NULL
> is ever written, so the stage cannot leave the gates green on its own. The three ways out
> were (a) add `or 0` at all 22 sites, which is trap 9's back door — unreachable today, but
> Stage 2 must then find and delete every one, and a single miss is a silent under-count on
> a byte cap; (b) merge Stages 1 and 2, losing the small reviewable commit; (c) **move the
> nullability flip into Stage 2, where its consumers are.** (c) was taken.
>
> It preserves this plan's actual intent — consumers learn NULL before one is emitted — and
> it turns mypy's error list into the exhaustive consumer checklist for Stage 2 rather than
> noise to silence. The instruction traded away is "both column changes in one regeneration",
> which existed to avoid churn; the baseline is rewritten in place from a disposable DB, so
> regenerating twice costs nothing.

What landed: `size_source` as a `StrEnum` (`db/models.SizeSource`) with a nullable
`String(16)` column, carried through `SeasonJudgment` and `_judge_item` to the two persist
sites. `size_bytes` is unchanged and still writes `or 0`. **`size_source` is already honest
while `size_bytes` is not**: it is NULL exactly when no source reported a size, which is what
makes the tally worth counting.

The tally is `scan.size_source_tally`, emitted once per scan with `sources={rung: count}`
plus an `unmeasured` bucket. Counts only, never a title or a path. This is the measurement
Stage 4 reads and the only way to size Stages 5 and 6 before writing them.

Pinned by `TestAStoredSizeSaysWhereItCameFrom` (`tests/test_scan_pipeline.py`), teeth-checked
by stamping the source unconditionally: both tests go red.

*Observable:* nothing. No NULL is written, and nothing on the wire changed.

### Stage 2 — flip the column and teach every consumer, while nothing emits a NULL

**Starts by flipping `size_bytes` to `Mapped[int | None]`** and regenerating the baseline a
second time. Mypy then names every consumer that must change; work the list to empty rather
than reaching for a coercion at any of them.

Planner partition + the `:273` sort replacement + group-expansion fix + `manifest_hash` null
encoding + the explicit-selection refusal. Executor defensive skips + comparator selection +
cap assertions. Retire `size_confirmed`'s `0`-as-sentinel reading in favor of the NULL check
in the same change (its docstring already defers to this plan). API schemas, aggregates,
counts, headers, sorts. TS types, `format.ts`, components. Tests construct NULL rows directly
to exercise these paths.

This is the large commit. It is safe to be large **because it emits no NULLs**: every new path
is exercised by constructed fixtures, and no scan can produce one yet.

*Observable:* nothing in normal operation.

### Stage 3 — stop fabricating zero

Drop `or 0` at `snapshot.py:653` and `season_scan.py:1143`, and widen the season carry chain
that the earlier draft missed and mypy will catch: **`SeasonJudgment.size_bytes`
(`season_scan.py:136`) → `_judge_item`'s `size_bytes` parameter (`snapshot.py:808`, fed at
`:710`) → the `Candidate` write (`snapshot.py:871`)**, all currently typed `int`. Rewrite both
long comments, which cite a growth check that has a 256 MiB hole (§1).

*Observable:* **this is the behavior-change commit.** An unmeasured item stops being
deletable and stops becoming the canary. Caps, the reclaim total and the confirmation phrase
become exact. The held-back notice appears. Small and reviewable precisely because Stage 2
already landed every consumer.

### Stage 3b — logging and per-item feedback

Everything in §4.7 that is not already carried by Stage 2's counts: the five structured log
events, the per-item "Held back: size unknown" status line in the queue, the why-panel reason
row, and `RunOut.held_back_unmeasured` on the completed-run surface. Lands immediately after
Stage 3 because that is the commit where hold-backs first happen, and an operator must never
meet a silently smaller plan.

*Observable:* the operator can see **which** items were held back and why, not just how many.

### Stage 3c — the unmeasured allowance — **DONE**

All of §4.6: the `ProfileSettings` field, the validator, the abort-not-truncate check, the
canary-last ordering, the phrase suffix, the `PolicyWarning`, and the `FixedQuantity` control
plus help text in the policy editor. Ships **after** the refusal and after the feedback, never
before: the escape hatch is only meaningful once the operator can see what it would let
through, and shipping it first would mean the default path was never exercised alone.

*Observable:* an operator who sets it above zero can reap a bounded number of unmeasured items,
with the phrase suffix and the warning notice making the trade explicit.

### Stage 4 — verify end to end against real data

Not a code stage. Run a real scan on the dev instance, drive the queue, plan and confirm
screens as a normal user (`verify` skill), read the Stage 1 tally, and record the ratios in
`docs/LEARNINGS.md`. **This is the measurement that tells you whether Stages 5 and 6 are worth
their diff.** Do not skip it to get to the code.

### Stage 5 — season sizes from the files themselves (a bug fix)

Prefer summed `episode_files(series_id)` over `statistics.sizeOnDisk`, making the frozen and
live sides of the growth interlock the same measurement for the first time (§2.1). Fold the
fetch into the existing per-show coroutine `_episodes_for` (`season_scan.py:906-916`) under the
existing per-instance semaphore `arr_bounds` (`:895-896`), so it takes no extra parallelism
slot and needs no new throttling. Cost: exactly one extra bulk GET per **prunable** series
(the `work` set at `:926`, already filtered at `:828` and `:846`), not per library series.

Two traps: an **empty file list is not a confirmation** — mirror `executor.py:1225-1235` and
leave the season unknown rather than storing a summed zero. A file whose `seasonNumber` is
unreadable is dropped by `_season_number`'s `-1` sentinel, which **under-states the season** —
and per §2.2 that is *not* safe for a cap. Either make an unreadable `seasonNumber` render the
whole season unknown, or document why not with a cap-side argument, not a rule 31 citation.

On `IntegrationError`: leave the season on rung 2 or unknown, log
`season_scan.size_fetch_unreachable`, and comment the rule 28 exception citing the planner
refusal as the compensating control. **Add the exception to `CLAUDE.md` rule 28's text in this
same commit.**

### Stage 6 — movie lower bounds, display only (optional, gated on Stage 4)

Record `movieFile.size`, then a single attributable Plex part, as `radarr-file` / `plex`.
Render qualified; never plannable. **Do this only if Stage 4's tally shows it would recover a
meaningful share.** The honest expectation, stated so nobody over-invests: rung 3 only fires
when Radarr gave neither `sizeOnDisk` nor `movieFile.size`, which usually means `movieFile` is
absent entirely and there is nothing to path-match against, so the realistic yield is low.

Plex attribution rules, all fail-closed: skip if `resolution.merged_rating_keys` names more
than one listing; never sum `matched.files`; prefer the single `PlexFile` whose path matches
`_movie_file_path` (`snapshot.py:1202-1206`) or basename matches `_movie_file_basename`
(`:1189`); failing that, use it only when the listing holds exactly one file with a known
size; otherwise unknown.

### Stage 7 — sweep the read-only lanes

`tests/_policy_lab.py:51-68` — add `"size_bytes"` to `DEGRADABLE`, bringing unknown-size under
the permutation sweep; it is currently one of only two observable fields omitted. Record the
design and the 256 MiB finding in `docs/PLAN.md` and `docs/LEARNINGS.md`. Delete
`services/snapshot.candidates()` (`:1664`) — verified test-only, and it carries a third
`ORDER BY` on the nullable column (rule 38: dead safety-adjacent code is deleted, not
stockpiled); update `tests/test_scan_pipeline.py:387`, `:500`.

**Split out, do not do here:** `services/fairness.py` (`:129`, `:192`, `:232-235`) and
`engine/backtest.py` (`:283`, `:359`, `:132-138`) are read-only lanes that delete nothing, and
backtest is verifiably unreachable (`backtest.py:25-28`). Record them in `docs/PLAN.md` with
citations as follow-up. For rule 35's grep, a comment at `backtest.py:359` stating the
unconditional `Known(...)` is correct by construction is sufficient.

---

## 6. Traps found by adversarial review

Do not rediscover these.

1. **SQLite sorts NULL FIRST on `ASC`** (verified: `[NULL, 0, 1, 5]`). Any `ORDER BY` left on
   the nullable column without an explicit NULL placement preserves the canary bug.
2. **`SUM` skips NULLs and `COALESCE(SUM(x), 0)` cannot detect it.** Every aggregate needs a
   sibling NULL count in the same query.
3. **The API seam is not a safe commit boundary.** `_candidate_out` constructs
   `CandidateOut(size_bytes=r.size_bytes)` where the field is `int`. A backend-first commit
   that writes NULL before the schema widens 500s `GET /api/candidates`, `/candidates/{id}`
   and `/groups/{key}` with a pydantic `ValidationError`. Hence the Stage 2 → Stage 3
   ordering: **widen consumers while nothing emits null.**
4. **`ReapConfirm.test.tsx:31` hardcodes `"REAP 1 ITEMS 1 GB"`**, and `runs.py:111` and `:265`
   must produce byte-identical phrases or every execute 409s. Add a regression test asserting
   the phrase is unchanged for an all-measured plan.
5. **`tests/test_review_reap.py`** is the reap-loop fixture whose helper docstring is literally
   "A snapshot with a set of condemned movie candidates: (media_key, size_bytes)", with
   size-bearing constructions at `:76`, `:115`, `:210`, `:226`. Plan membership, ordinals and
   the phrase are all asserted there. It is in scope for Stage 2 and Stage 3.
6. **`SonarrSource.client` is typed `Any`** (`season_scan.py:119`), so a fake missing the new
   method fails at runtime, not type-check. Update every fake in the same diff
   (`tests/test_reap_loop.py:1764` and the season_scan fakes).
7. **`tests/test_fact_layer_states.py` has no season counterpart** for the size case: a season
   with `size_on_disk is None` reaching `facts.size_bytes` as `Unknown` is uncovered. Add it.
8. **Do not reset `data/reaper.db`** to make `alembic check` pass. Use a fresh throwaway DB.
9. **The allowance must not leak into the byte caps.** The tempting shortcut once unmeasured
   items can be planned is to let them contribute `0` to `_check_caps` so the arithmetic keeps
   working. That is the original bug, restored by the back door. Exclude them from the byte
   sums explicitly and bound them by count.
10. **`ProfileSettings` is out of the policy hash for a reason that does not cover a loosening
    field.** Do not extend the existing docstring rationale to the allowance; give it its own
    (§4.6).

---

## 7. Verification

Beyond the gate set, each stage needs a **teeth-check**: remove the fix, confirm the test goes
red. Named ones:

- Remove the planner partition → the held-back, ordinal and explicit-refusal tests go red.
- Remove the NULL placement from a sort → a test asserting the first condemned row is a
  measured item goes red (SQLite sorts NULL first).
- Remove the null encoding from `manifest_hash` → the "NULL hashes differently from 0" test
  goes red.
- Restore `?? 0` in the `ReviewQueue` reduce → the `, N sizes unknown` suffix vanishes, test
  goes red.
- Revert the comparator to always read `sizeOnDisk` → a `radarr-file`-sourced item is falsely
  skipped as grown, test goes red.
- Remove Stage 5's empty-list guard → a season with no files starts storing a summed zero,
  test goes red.
- Make Stage 5's `IntegrationError` handler call `context.degrade` → the "one bad series does
  not degrade the snapshot" test goes red.
- Set the allowance above zero and let an unmeasured item sort first → the "ordinal 0 is always
  a measured item" test goes red. This is the single most important teeth-check in the plan.
- Let an unmeasured item contribute `0` to `_check_caps` → the "byte cap counts only measured
  items" test goes red.
- Plan more unmeasured items than the allowance permits → the abort test goes red if the
  implementation truncates instead.

Plus a real browser pass at Stage 4: the narrow-viewport overflow of the nowrap size cells
(`index.css:1188-1196`, `:2985-2987`) must be checked, not assumed.

---

## 8. Open questions to answer with data, not argument

- **How often is a size unmeasurable at all?** Nothing in the repo quantifies it. Stage 1's
  tally answers it, and it sizes the blast radius of Stage 3's refusal.
- **How often does a movie have `hasFile: true` and no `sizeOnDisk`?** This is the entire
  value of Stage 6. If it is near zero, skip Stage 6.
- **How far does a season's folder statistic diverge from its summed episode files?** This
  measures how desensitized the season growth interlock has been (§2.1).
