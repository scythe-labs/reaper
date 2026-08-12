# Open-issue plan

Every open issue, re-verified against `dev` at `6d06f07` rather than trusted from its body,
sorted into what an operator can hit and what only a developer can. Then a route through them
in six pull requests, plus one issue that closed with no code at all.

Two open issues are outside this plan and stay open untouched: **#553** and **#554**, the two
feature requests. They are named here so a reader can see they were left out on purpose rather
than missed.

Status of this file: live. Delete a row when its issue closes, and delete the file when the
last one does.

## What the re-verification changed

Three issues moved before any work was planned.

**#576 is stale and closes now.** Both halves are gone. `tests/test_calibration.py` was deleted
with `engine/calibration.py`, and `tests/test_list_config.py`'s named test was rewritten onto the
`session` fixture and opens no raw connection. Measured: `uv run pytest tests/test_list_config.py
-W error::ResourceWarning` exits 0, 65 passed.

**#550 shrinks from five items to one.** Four of the five comments describe code that no longer
exists after the simplification landed: `ListConfig.built_in` is gone from `db/models.py` (it
survives only in the alembic chain), `grace.unknown_size_in_grace`, `seerr.Requester.is_mappable`
and `fields.evaluate_rules` all return nothing on a tree-wide grep. What is left is
`services/planner.py:172`, whose `_movie_steps` docstring still says "delete-with-exclusion, then
verify, then refresh" over a body that returns `[delete]`.

**#566 stopped being hypothetical.** `20260808_1200_release_m_for_six_retired_columns.py` is in
the chain and is the first half of a two-release column removal. The second half is the migration
that actually drops, and it has no snapshot under it. This has to land before that revision does.

## Legitimacy, one line each

### An operator hits this

| # | Pri | Verified on `dev` | Reachable how |
| --- | --- | --- | --- |
| 780 | High | `history_sync.PAGE_SIZE` 25,000 against `clients/base.DEFAULT_TIMEOUT` read=30s, no per-client override | Any Tautulli whose history needs paging and answers slower than it used to. Measured on a real instance. |
| 709 | High | Raw `imdbId` written at 8 sites; `item.imdb_id or item.plex_imdb_id` at `snapshot.py:440` and `:1091` | A source emitting the `tt0000000` sentinel. Mechanism confirmed, trigger unproven. |
| 566 | High | `launcher.py:254`, `docker-entrypoint.sh:40`, `dev-local.sh:300` all call upgrade with nothing between it and preflight | The release that ships the `drop_column`. Not today. |
| 700 | Med | `config.py:126` `Literal` accepts ERROR, `logbuffer.py:44` `LEVELS` omits it, `contrib/unraid/my-Reaper.xml:87` advertises it | Copying the Unraid template's own documented value. |
| 657 | Med | `fields.py:857` uses `.lower()`, its two sibling arms use `.strip().casefold()` | Narrow through the UI, which trims client-side. Open through the API-key policy route, and open from the UI for a non-ASCII case pair. Fails toward deleting. |
| 623 | Med | `gates.py:451` `describe_bar` renders a floor through `describe_votes`, which two other callers use for a real count | Any rating bar plus a title with no rating. The why-panel says "from 1,000 votes" for both meanings. |
| 718 | Low | `PolicyEditor.tsx:2543` savebar notices carry no `standing`; `:1816` top notices do | A screen reader, on any policy body the server repaired. |
| 688 | Low | `PolicyEditor.tsx:2475` help text never says a non-zero value permits a delete | Anyone reading the control before moving it off 0. Narrow: the warning fires the moment it moves. |

### Only a developer or CI hits this

| # | Pri | Verified on `dev` | Why it still earns a fix |
| --- | --- | --- | --- |
| 651 | Low | Reproduced: the file alone exits 1, one test failed, 3.0s | A test whose result depends on which files ran before it is not evidence. |
| 557 | Low | `api/scan.py:192` and `api/runs.py:729` have no done callback; `main.py:430` has one | A raise from the `finally` arm leaves the UI showing a run that stopped, with no log line. |
| 589 | Low | `ci.yml:149` puts `*.md` in the first arm, above `manual/*|website/*` at `:155` | One `.md` added to either tree publishes without the site build having run. |
| 661 | Low | `snapshot.py:1492` takes `grace_days`; the next read of the name is `:1796`, a different function | It reads as though grace participates in the per-item judgment. |
| 629 | Low | `gates.py:101-103` sends the reader to `custom_condemn`; `GateId.CUSTOM` is carried by `fields.CustomProtectGate` | Next author reasoning about what the id tallies reasons off the removal lane for a keep gate. |
| 550 | Med | `planner.py:172` only, see above | Someone auditing the journal for a verify step will not find one. |

### Nothing hits this, and that is the argument against doing it

| # | What it is | Recommendation |
| --- | --- | --- |
| 606 | Lift `QueueFilterBar` out of a 2,671-line `ReviewQueue.tsx` | Defer. The stated benefit is that sessions run out of context inside the file, which is an agent-ergonomics cost and not a product one. The extraction creates three upward-reported signals at once, so it carries real regression risk against zero operator gain. Do it opportunistically, when someone is already rewriting that block. The two dead re-export lines can go in any passing PR. |
| 607 | Split three banner regions out of `PlexPanel.tsx` (1,272 lines) | Defer, same reasoning, and worse: the banners mark regions of hooks, so this is three new components with state re-threaded, and an extracted child can introduce an early return that outlives the guard satisfying it. |
| 779 | Move the `.notice` base rule out of `16-simulator.css` | Defer. Real cascade hazard, 143 render sites, and the move has to be proven with before-and-after computed shapes rather than argued. Worth doing the next time anyone touches the stylesheet order. |
| 576 | Two tests leaking SQLite connections | **Closed as stale**, `Reviewed/Invalid`, with the measurement above written into it. |

### A feature, not a defect

| # | What it is | Recommendation |
| --- | --- | --- |
| 774 | A URL per section | Keep open. Blocked on one design decision, whether `backnav` grows section entries itself or a router is added beside it. The first is smaller and is probably the answer. Not part of this plan. |

## The route: six pull requests

Grouped so each one runs the CI code lane once. A pull request touching any `.py` or `.tsx`
already pays for the backend, frontend and docker jobs, so a second unrelated one-line fix in the
same lane is free, and a separate pull request for it is not. Grouping stops where a story stops.

### PR 1 — housekeeping: seven confirmed corrections, none over three lines

Closes **#557, #589, #661, #629, #550, #718, #651**. Also closes **#576** with no code.

Land this first. It is the cheapest resource per issue closed by a wide margin, and nothing in it
depends on anything else.

- `api/scan.py:192` and `api/runs.py:729`: `add_done_callback(_report_background_failure)`. The
  callback exists and already ignores cancellation.
- `ci.yml`'s `changes` case: move `manual/*|website/*` above the prose arm. Then either correct
  `docs-deploy.yml`'s comment claiming the site job already built the tree, or add
  `.github/workflows/docs-deploy.yml` to the site arm, because editing that workflow fires it
  while `ci.yml` classifies it as code.
- `snapshot.py:1492`: drop `grace_days` from `_judge_item` and its two call sites.
- `gates.py:101-103`: name `protect_conditions` and cite `fields.CustomProtectGate`.
- `planner.py:172`: correct `_movie_steps` to say what it returns. Verification and the Plex
  refresh belong to the executor and are not journalled steps.
- `PolicyEditor.tsx:2543`: pass `standing` on the savebar notices so both halves match the reason
  written above the top half, and move `_EXPECTED_STANDING` in `tests/test_repo_hygiene.py` by
  one. The alternative, narrowing the comment instead, is a judgment about the savebar half; the
  first is the one that matches the reason already given.
- `AppStaleRead.test.tsx:189`: give the `findByText` its own timeout, or warm the wizard boundary
  earlier in the file. Testing Library's `asyncUtilTimeout` is what is being blown, so the vitest
  `testTimeout` knob does not reach it.

If a reviewer wants this narrower, the clean split is backend (`scan`, `runs`, `snapshot`,
`gates`, `planner`) and everything else. It doubles the CI cost and closes the same seven.

### PR 2 — the Tautulli sweep survives a slower instance

Closes **#780**. Priority High, and the only High an operator can hit today.

Give the sweep its own read timeout well above the page cost, or halve `PAGE_SIZE` on a
`ReadTimeout` and keep paging rather than aborting. The retry helper cannot rescue this on its
own: it re-issues the identical oversized request against the same budget. Whichever is chosen,
the comment under `PAGE_SIZE` that claims a 25k page costs about what a 1k page costs is the
assumption that removed the headroom, so it carries the timeout margin it depends on.

Alone, because it changes the shape of a network read against a real instance and wants to be
readable as one change if a sweep later misbehaves.

### PR 3 — external ids are cleaned at the write, not at the read

Closes **#709**. Priority High, Area/Safety.

Route the eight raw `imdbId` writes through the cleaning door: `lists.py:280`, `:461`, `:473`;
`snapshot.py:2041`, `:2126`, `:2163`; `season_scan.py:1076`, `:1352`; `seerr.py:192`. Cleaning at
the write fixes both directions at once and the two `or` sites then need no change.

Start with the test the issue names, because it is the cheap half and it settles whether the
trigger is real: build a `RawItem` carrying the sentinel and a clean `plex_imdb_id` that is on a
protection list, run the movie path, assert the membership is found. Fix regardless of the answer,
since a truthy sentinel shadowing a cleaned id fails toward deleting a keep-listed title and the
fix is eight call sites.

Two things ride along because they are the same story. `identity.py:209` claims its door is the
only safe way in while nine callers walk around it, so that sentence becomes true here. And the
`fairness.py:322` direction is the one place the argument above does not cover, so trace what a
mis-attributed request does to a candidate's fairness result and say in the pull request body what
was found, even if the answer is that nothing changes.

Alone, because it is the only change in this plan that alters what the protection lookup matches.

### PR 4 — a snapshot before a destructive migration

Closes **#566**. Priority High. Must land before the release that drops the six retired columns.

Take a copy before the upgrade when the pending revisions include a destructive one, keep the last
few and prune the rest. `services/backup.py` already writes the artifact, so this is a call site
and a retention bound. Mark the revision itself, as an attribute on the module, so the ordinary
additive upgrade skips the snapshot and the one that needs it cannot forget it.

Three call sites move together: `launcher.py:254`, `docker-entrypoint.sh:40`,
`scripts/dev-local.sh:300`. The development path is not optional here. It is the path that proves
the container path works before an operator runs it.

### PR 5 — two folds that were upgraded on some siblings only

Closes **#657, #658**.

One story: a normalization applied to two of three branches, twice, in two files.

- `fields.py:857`: make the `contains` arm fold the way its `is` and `is one of` siblings do.
  This changes what a stored rule matches, so it carries a mixed-case test and a baseline
  re-capture. Measured divergence to pin: a list named `Kids` against `contains "Kids "` returns
  false where both siblings return true, and `Straße` against `STRASSE` diverges the same way.
- `config.py`'s `parse_instance_seeds`: fold `slot` the way `kind` and `field` are folded. The
  pattern carries `re.IGNORECASE`, so the regex absorbs case for matching and the grouping has to
  absorb it too. Two of three groups do. Second half, and the judgment call in this pull request:
  the skip at the end of that loop says nothing, and a required field missing is the one case
  where the operator has clearly tried to configure something. A log line there is cheap.

### PR 6 — four claims that disagree with the code under them

Closes **#700, #623, #688**.

One story: an operator-facing sentence and the declaration it describes have drifted apart, in
four places.

- **#700, log level.** Let the env path carry ERROR through `LEVELS` while the UI keeps offering
  three. Dropping ERROR from the `Literal` is the smaller diff and turns an existing
  `REAPER_LOG_LEVEL=ERROR` into a boot refusal, which is a worse trade against an operator who
  set it because the Unraid template told them to. Correct `contrib/unraid/my-Reaper.xml:87`
  either way.
- **#623, the vote floor.** Give `describe_bar` its own clause naming the number as a floor, and
  leave `describe_votes` to the two callers rendering a real count. It cannot be a bare `+`
  append: at a floor of 1 the clause reads `from 1+ votes` where `describe_votes` correctly
  renders `from 1 vote`. Point the new test's failure message at `PolicyEditor.tsx`'s `describeBar`
  by name, since that is the copy this one has to keep agreeing with.
- **#688, the unknown-size allowance.** One clause in the help text saying that above zero, Reaper
  removes that many titles it could not measure. The existing warning stays as the confirmation
  once the value moves. This issue is filed `Status/Need More Info` and what would settle it is a
  walkthrough with someone who has not read the code. That evidence is not worth waiting for
  against a one-clause fix in the same file two other rows here already touch.

## How it runs: one orchestrator, six workers

Each pull request is one worker in its own git worktree on its own branch. The orchestrator holds
the wave order and the merge queue and writes nothing itself, so no worker inherits another
worker's half-finished tree.

### What every worker does, in order

1. `git fetch origin`, then `git checkout -b <branch> origin/dev`. Branch off the remote ref, and
   confirm `git log --oneline origin/dev..HEAD` is empty before starting. A worktree opens on
   whatever branch it was cut for, which is how someone else's commits arrive in your diff.
2. Read the rules file governing the tree it is about to touch, before editing anything in it.
3. Do the work. Commit freely; the branch is squashed on the way in, so the care goes into the
   pull request title and body.
4. Run the gates individually and read each exit code. Never chain them behind a pipe.
5. Run `/reaper-review` on its own diff and act on what comes back. A worker that finds a defect
   its own branch introduced fixes it on the branch. It does not file it.
6. Update `docs/STATUS.md` in the same commit if what the app does changed, and delete this file's
   row for every issue the branch closes.
7. Rebase onto `origin/dev` again immediately before pushing, then re-run the gates. `dev` moves
   while a worker works, and a branch that was current when it was cut can still merge into a tree
   its tests never ran against.
8. Open the pull request with its `Kind/` and `Priority/` labels inherited from the issues it
   closes, `Closes #n` for each, and the co-authorship trailer in the description, because the
   description becomes the squash commit's body.
9. Report back to the orchestrator: branch, PR number, what the review found, what is still open.

### Reviews, and which ones are not optional

- **Every worker runs `/reaper-review` on its own diff.** That is the normal pass and it is the
  one that catches most of what a reviewer would.
- **PR 3 runs `/reaper-safety-review` as well, before it opens.** It is the one change here that
  alters what the protection lookup matches, on a path whose failure direction is deleting a
  keep-listed title. A normal diff review is not the right instrument for that.
- **PR 4 runs its review against a restore, not a diff.** The value of a pre-migration snapshot is
  entirely in whether it can be read back, so the worker takes one, drops a column by hand in a
  scratch copy, restores, and says in the pull request body what it observed. A snapshot nobody
  has restored from is a file, not a recovery path.
- **After the last of the six lands, one `/reaper-safety-review` runs on merged `dev`.** A
  squash-merge replays each branch onto whatever `dev` has become, so six green per-branch runs
  are not evidence about the tree they produced.

### Waves

Two waves, split on file overlap rather than on priority. Everything inside a wave can run at
once.

**Wave A, four workers in parallel.** No two touch the same file.

| PR | Branch | Files it owns |
| --- | --- | --- |
| PR 1 | `fix/housekeeping-corrections` | `api/scan.py`, `api/runs.py`, `services/snapshot.py`, `engine/gates.py`, `services/planner.py`, `ci.yml`, `PolicyEditor.tsx`, `AppStaleRead.test.tsx`, `test_repo_hygiene.py` |
| PR 2 | `fix/history-sweep-timeout` | `services/history_sync.py`, `clients/base.py` |
| PR 4 | `feat/pre-migration-snapshot` | `launcher.py`, `services/backup.py`, `docker-entrypoint.sh`, `scripts/dev-local.sh`, a revision module |
| PR 5 | `fix/sibling-folds` | `engine/fields.py`, `config.py` |

**Wave B, two workers, after PR 1 has merged.** Both collide with PR 1 and neither collides with
the other.

| PR | Branch | Collides with PR 1 on |
| --- | --- | --- |
| PR 3 | `fix/clean-external-ids-at-the-write` | `services/snapshot.py` |
| PR 6 | `fix/claims-that-disagree-with-the-code` | `engine/gates.py`, `PolicyEditor.tsx` |

PR 3 is High and safety-lane, so if PR 1 stalls, PR 3 goes first and PR 1 rebases behind it. The
overlap is textually distant in `snapshot.py` (a signature near line 1492 against writes past
2041), so either order merges. The wave exists to keep a rebase from landing in the middle of a
safety review, not because git cannot handle it.

### What the orchestrator holds

- The wave gate: no Wave B worker starts until PR 1 (or PR 3, if the order flipped) has merged and
  `origin/dev` carries it.
- The merge queue: `gh pr checks <n>` before every merge, `gh pr merge --squash <n>` after. A
  fresh pull request sits pending for minutes, so `--watch` is the cheap way to wait.
- The re-run after the last merge: the gates on merged `dev`, plus the safety review above.
- The closing pass: confirm all 16 issues closed, close **#576** as stale citing the measurement
  in this file, and leave #606, #607, #779 and #774 open with the recommendation from the tables
  above written into each as a comment. **#553 and #554 are not touched at all**, not commented
  on and not relabeled, since they are outside this plan rather than judged by it.
- Anything a worker finds and is not fixing becomes an issue before that worker finishes, with the
  branch it lives on named in the body if it is not on `dev`.

## Order, and why

1. **PR 1** first. Eight issues, nothing over three lines, no dependencies. It clears more than
   half the board for one CI run and makes the remaining list readable. It also unblocks both
   Wave B workers.
2. **PR 2** and **PR 4** run beside it. Both are High, neither touches anything PR 1 does.
   PR 4 is the only item with an external deadline: it has to land before the release that ships
   the column drop.
3. **PR 5** runs beside them too. Small, isolated, no dependencies.
4. **PR 3** and **PR 6** in Wave B. PR 3 is the one that wants the most reading, and putting it
   after PR 1 means it is reviewed against a tree that is not moving under it.

Six pull requests close 16 of the 20 issues this plan covers. Of the remaining four, two are
refactors with no operator-visible gain and real regression risk (#606, #607), one is a cascade
move that wants a measurement rather than a diff (#779), and one is a feature blocked on a design
decision (#774). #553 and #554 sit outside the plan entirely.
