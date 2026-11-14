---
paths:
  - "src/reaper/**/*.py"
  - "alembic/**/*.py"
---

# Backend blockers for `src/reaper/`

Every rule here is a blocker. Rule numbers are permanent, because existing tests and the
archived review passes in `docs/history/` cite them. Where two rules overlap, the more specific
one governs. A code comment states its constraint in plain language and does not cite a
rule number. The rules that bind every file are in the root `CLAUDE.md`. The SPA's rules are in
`.claude/rules/frontend.md`.
Holds 1–6, 8–10, 13, 22, 23, 26–35, 38, 52, 55–59, 63, 65, 70, 71, 73, 77, 78, 81, 82, 87–97, 102–117, 124, 127–129, 131, 140, 142, 143, 148.

## The safety / deletion path

**1. Omitted field ≠ explicit empty collection.** On any destructive or filtering path, treat
`None` and `[]` differently. An empty selection fails closed. It never expands to
"everything."

**2. Never fail open.** When a whitelist or keep-list sync, a protection source, or an optional
dependency (Plex) fails, degrade the snapshot to un-executable. Never proceed on empty or
unverifiable protection data. There is one exception. A failed whitelist refresh may use the
stored membership from a recent successful sync, within `snapshot.WHITELIST_STALE_AFTER` (48h),
because the stored copy still protects everything on it. Past that bound, or with no record of
a successful sync, degrade.

**3 / 22. One decision function.** The condemn/abstain/protect decision lives only in
`engine/verdict.decide_verdict`. Five callers import it: `snapshot`, `simulate`, `condemned`,
`policy_warnings.inspect`, and the threshold curve (`api/policy.threshold_curve_rows`). The
"nothing will be flagged for removal" warning in `policy_warnings.inspect` depends on the
decision staying monotone in score and coverage. The threshold curve depends on it staying
monotone in score. Never write `score >= threshold` or `coverage_bp >= floor` inline outside
it. Never reimplement scoring, coverage, rounding, or floors anywhere they can drift.

**4 / 71. Reset time-window clocks on re-entry, and clear them when an item leaves the set.**
Never let a stale first-flagged timestamp skip a safety window. When an override that kept an
item off the reap list is removed, delete the FirstFlagged row before
`record_first_flagged_bulk` runs, unconditionally. Never trust `last_seen_condemned_at`
continuity across a period when the item was invisible to the operator.

**5 / 30. Rank, cap, and count over the exact set acted on.** Filter first (content-bearing
seasons, non-spared deletable items, fetched-vs-all groups), then rank or count. Never expand
caps or counts over items that a later step filters out. Any number shown beside a destructive
button derives from the same set the server acts on.

**23. Every re-decision surface handles every stored verdict state.** When you add or consume a
Candidate verdict, enumerate every state at every consumer. The states are protect,
abstain-blocked, abstain-by-coverage, abstain-by-score, condemn, overrides, and expired. Add the
blocked and override cases to `tests/test_simulate_permutations.py` in the same change.

**26. Journal and state-transition writes on the deletion path are durably committed at each
step.** Never rely on `flush()` inside a run-long transaction for anything described as an
audit record. Make every state-transition guard an atomic `UPDATE … WHERE state = :expected`.

**31. Derived condemn-lane values round toward keeping.** When you reduce precision on any
field that can add deletion pressure (dates from years, sizes, ages), choose the bound that
produces less pressure.

**38 / 117. A gate or option the operator can enable must be able to fire.** If every fact
builder sets its input `Absent`, either wire the input or retire the gate. To retire a gate,
remove it from `GATE_TYPES` and add it to `PolicyBody.RETIRED_GATES`, which drops it from a
stored policy on load. A gate that was a live protection gets a load shim instead (rule 105).
Keep its `GateId` so stored explanations still decode. `build_gates` still refuses an unknown
id. `test_every_unbuildable_gate_id_is_declared_retired` fails on a gate retired neither way.
Delete dead safety-adjacent code. A method "for when the interlock lands" ships with its
interlock and its tests, or does not exist.

**73. A password-gated destructive confirm is content-bound.** The confirm request carries a
server-verified token derived from the exact content the operator reviewed. The server
recomputes the token, or stores it at stage time. The action refuses if the content changed
since review. The execute route's phrase is the model. Any new stage-review-confirm flow
(restore, import, bulk apply) carries the same binding.

**97. Anything counting what was deleted counts the file's removal, not the bookkeeping after
it.** A live re-resolve that returns no files goes to `_mark_skipped`, recorded under
`error.reap.step.season_files_vanished`. It never counts the approved size as deleted, and it
never overwrites an already-VERIFIED step. A step whose file is confirmed gone but whose
follow-up (exclusion, refresh) failed stays FAILED, because VERIFIED would make the journal
claim a verification that failed. That step still charges the rolling caps through the
durable `file_removed_at` column.

**111. The executor's send loop and `execute()` each carry a catch-all that records terminal
state.** An unmapped exception after a file is already deleted must not leave the step `SENT`,
the run `EXECUTING`, and the report `None`, with nothing able to reconcile them. An unexpected
per-item exception goes through `_fail`. An unexpected run-level exception records `ABORTED` and
returns the report. It never re-raises into a caller that does not persist the report.

**112. The executor re-reads the operator's spare decisions before every item.** A decision map
loaded once at run start ignores a Spare clicked during a multi-minute reap, and the file is
deleted. Refresh only the per-item spare and effective-set checks. Intersect them with the
frozen run-start set, so the refresh can only remove items and never adds one the operator did
not approve. Cap math stays on the run-start set (rule 5/30). Route the refresh through the
production `condemned.effective_verdict`, never through a second membership copy.

**113. A run's approval is bound to the policy it was planned under.** Record `run.policy_hash`
and enforce it at execute time. The refusal's operator copy tells the operator to re-scan,
because a policy edit does not trigger a scan on its own. Execute refuses a plan built after the
edit as well as one built before it. Never leave a hash recorded and unread.

**124. A protection derived from viewing position anchors on the most recent play as well as
the highest ordinal, never on the ordinal alone.** Anchoring the mid-binge guard on the highest
season number alone gives a re-watcher or an out-of-order viewer no protection at all.
Anchoring on the latest play alone drops the hold on the newest season when a viewer dips back
into an old one. Each viewer anchors on the season of their latest play and on the highest
season they played (`season_pruning.sequential_protections`). A position derived by arithmetic
can also land outside the set that exists. It then protects nothing while it reads as a live
guard. Before you treat the next season as cover, check that it exists on disk. Specials are an
anchor only when `keep_specials` is off, and a specials anchor holds the specials alone, never
season 1.

**116. A degraded snapshot's side effects are gated with its plan.** An un-plannable snapshot is
also un-announced. Grace clocks, the Leaving Soon shelf, and Discord all read the condemned set.
None of them may act on evidence the scan declared untrustworthy.

## Evidence, degradation, and the fact layer

**27 / 90. A protection container that cannot be found is an error, never an empty result.**
Before any atomic `DELETE` and reinsert of protection membership, tell a genuine empty apart
from three cases. They are a missing container, a malformed body, and a populated container
that filters down to zero usable items. When members are already stored, each of the three
must raise, so the previous membership survives and the snapshot degrades.

**28. Failure of any evidence source degrades the snapshot.** Every `except` around a source
read in the scan pipeline appends to `pre_scan_degradations` or calls `context.degrade`. A bare
`log.warning` on a source failure is a review-blocker. Watch history is a source. Treat a
success response that carries a null or malformed body as a failure, never as a genuine empty.
- *Sanctioned exception:* a per-item missing size does not degrade the snapshot. Degradation
  is snapshot-global, and a degraded snapshot cannot be planned at all, so one unsizable movie
  would block the operator's entire run. Instead, that item alone is held back from every plan
  (`planner.build_plan`) and refused again at send (`executor._may_send_unmeasured`). The send
  refusal also requires the frozen size to measure the same quantity the live re-read will. The
  operator is told the count and which items. Both refusals yield to the owner's allowance
  (`ProfileSettings.max_unmeasured_per_run`). Above zero, a bounded number of unmeasured items
  are planned, charged to the item caps, and sent with the growth interlock unavailable.
  `size_confirmed` is the counting-side predicate (`_deletable`, `api.runs._planned_candidates`).
  It is not the send refusal. Never collapse the send refusal back to it, because that drops the
  comparable-quantity check. This exception covers the item's own size only. A source that
  fails to respond still degrades.
- *Sanctioned exception:* a log-only `except` is allowed only where the fallback keeps the
  file. The comment beside it names the fallback: the item abstains, a protection widens, or a
  slower complete path runs. A source whose loss can withdraw a protection or add pressure
  always degrades.

**35. New `Facts` fields are populated, or explicitly `Absent` with a comment, in every fact
builder.** Two builders read evidence, `snapshot.build_facts` and
`season_scan.build_season_facts`. A field populated in only one of them silently changes scores
and coverage in the other. Two more sites construct a `Facts`. `facts_codec.facts_from_dict`
thaws only what a builder already wrote. `preview._bare_facts` calls `Facts(**observations)`,
which raises at runtime for a field with no default, and its `# type: ignore` on the unpack
hides that from mypy. Grep for `Facts(` to find every site.

**65 / 91. Silent recovery on operator-configured safety values is forbidden, and a config read
failure is not "nothing configured."** A fallback that replaces saved profile or policy values
surfaces a flag the UI renders, and degrades the scan, following the `ActivePolicy` pattern. A
log line alone is a blocker. On any safety-scoping path, a read error degrades the snapshot.
Only a successful read that finds nothing may fall back to the permissive default. A transient
error must never silently widen what can be reaped. Copy must never call the wider scope the
"safe" fallback, because widening is the condemn direction.

**70. Time-bounded state has exactly one durable realization point, shipped with the feature.**
Realize any stored decision that expires (a timed spare, a deadline, a TTL) with code that
writes the transition. An in-memory filter at read time is not a realization. Every live
consumer converges after the write. A docstring saying "the next scan realizes it" requires
the scan to persist that realization in the same change.

**92. Degradation is detected by a typed flag on the context, never by substring-matching a
free-text reason.** Any `"some-source" in " ".join(reasons)` coupling between producer and
consumer is a blocker. The reason string is operator copy, and it gets reworded. Carry an
explicit boolean (`activity_degraded`).

**93. `Absent` means "we looked and there is genuinely nothing." A source that could not be read
is `Unknown`.** Never route a read failure to `Absent`. `Absent` withdraws the protection
library-wide and prints a why-panel that asserts a check that never ran. `Unknown` blocks the
gate and takes the full keep discount. A genuine `Absent` on a numeric signal routes to
`NOT_APPLICABLE` (evaluated, weight retained, coverage intact), never to the `UNREADABLE`
branch. `SEASON_RANK` and the graded custom path already do this. Degrading the snapshot is
necessary but not sufficient. A comment that claims degradation already prevents this is a
defect to fix.

**104. A value derived two ways in two modules is derived once in a shared helper, and the
helper defines what a record lacking it thaws as.** Dormancy days (`engine/dormancy.py`),
condemn/score/coverage, and any parallel field list (`_OBS_FIELDS`) each have exactly one
derivation. Prefer `dataclasses.fields(...)` over a hand-maintained parallel list. Moving a
derivation to the write side moves the problem to the read side, so state the thaw explicitly.
A key that a stored snapshot predates thaws as `Unknown`, never as `Absent` and never as a
`KeyError`.

**105. A stored policy body that gains a protection-bearing field ships a loader shim in the
same change, and the shim degrades the scan.** When a field moves out of a gate row into the
body, the loader migrates a body written before the move. Key the migration on the raw key
being absent. An explicit `[]` is an operator who cleared it deliberately (rule 1). Never key
it on `schema_version`, which cannot discriminate across a change that did not bump it. Recover
only where something was protecting. Leave a disabled gate alone. The migrated body sets
`ActivePolicy.repaired`, degrades the scan, and opens the editor on it as an unsaved draft.
Without the shim, the protection silently evaluates to "nothing configured."

**107. A field offered in the policy vocabulary is populated by the fact builder for every media
type it is offered on.** A `FieldSpec` with no `media_types=` is offered on both policies. If
the season builder hardcodes it `Absent`, restrict the spec in the same change, on both lanes.
Removal weights sum to a fixed 100, so a condemn rule on an always-`Absent` field permanently
depresses every score in that media type. Warn an operator who holds a stored rule that just
became unofferable. Never drop the rule silently.

## Identity, joins, and keys

**6. Disambiguate cross-system joins by a stable identifier.** Bind on an external id first.
Year plus title is the last tier (`identity.title_year_match`). There a unique title binds
unless the known years conflict, and a duplicate title needs a year that singles out one item.
Refuse to bind on any other ambiguity (return Unknown / ABSTAIN). Never let the last write silently win
in a `dict[title, row]` map.

**29 / 106. Every id the item carries, of every kind and every spelling, goes into every
lookup.** Pass imdb, tmdb, and tvdb together to `membership_index.lookup` or any cross-system
join. Pass both `imdb_id` and `plex_imdb_id` to `membership_index.lookup`, on the movie path
exactly as on the TV path. Never pick one with `item.imdb_id or item.plex_imdb_id`, because the
two can differ. Passing one id where two exist is a fail-open protection bug. When you add a new
id kind to storage, grep for every lookup call site and update each one in the same change.

**52. A bare tmdb id is not a stable key across media kinds.** Movie and TV tmdb ids share one
integer space. Every map, index, or lookup keyed on a tmdb id carries the media kind with the
number, on both the write side and the read side, unless the map holds one media kind only.

**63. Rows are keyed and aggregated by a stable server id, never a display name.** If the schema
lacks an id, add one in the same change. Key a person on an id unique across every source: the
linked id when present, else the per-user id qualified by its portal (`fairness._identity`).
Never key on a value that can be None. This binds membership indexes and path tables as much
as display rows. Any dict whose key can collide is a bug, and a display name can always collide.

**109. An identity tier that can corroborate a bind is computed even when an earlier tier already
bound, as a cross-check only and never as an originator.** Pass the binding ids explicitly
instead of reordering a priority tuple, so a corroborating id kind can add an abstain but never
originate a bind. Never put a `tier1 is None` guard in front of a corroborating tier. That
guard makes the documented contradiction veto structurally undetectable. Treat a multi-hit tier
as silence, never as a contradiction. A hit inside the earlier tier's merged group is
agreement.

## Policy evaluation

**32. Typed condition values validate against the field's type at the boundary, and evaluation
never raises out of a scan.** `Condition.validate_for` refuses a mistyped value at save.
`fields.evaluate` holds the rest: an evaluation error marks that item blocked, and a stored
policy never crashes `score()` or `evaluate_all`.

**108. A text condition value is rejected at the save boundary when it strips to empty, or
when it carries the separator its fact is joined on.** `contains ""` matches every item and
applies the rule's full weight library-wide. `in ""` can never match and reports as a green
"checked, did not fire." Reject `value.strip() == ""` for CONTAINS and IN. Reject an IN target
whose split yields no elements, so a comma-only list cannot pass.

When a stored string will become an element of a joined fact, refuse the separator where the
name is typed as well as on the rule built from it, and say so in the operator's words.
`list_config._clean_name` and `Condition._validate_value_type` hold the detail.

## Clients & HTTP

**9 / 110. All external HTTP goes through the shared client's error-mapping and retry layer, and
every client method maps its failures to the client's domain error type.** Never call
`self._client.request` directly. One read that lets a raw transport exception escape defeats
every `except <Domain>Error` in the call chain. A method documented "never fatal" catches
`Exception`, never only one mapped type. Make each `@retry` predicate match the exceptions the
code actually raises. Never convert an exception into a type the retry predicate then fails to
match.

**10. Report the accurate error/status.** Map a name clash to `409`, never `404`. Report the
actual timeout kind, never a hardcoded budget. Honor upstream retry signals instead of dropping
them.

**33. All HTTP lives in `clients/`.** A raw `httpx2` or `requests` usage outside
`src/reaper/clients/` is a blocker unless this rule names it below as a sanctioned exception.
The guard classifies and gates GET-shaped mutating endpoints by path. It never assumes one is
safe by its method. Public unauthenticated GETs go through `clients/public.py`.
- *Sanctioned exception:* `notify/discord.py`'s webhook POST. The webhook URL embeds a
  per-operator secret path that the guard's exact-path allow-list cannot express. The API edge
  validates the URL to Discord's hosts. The client only sends outbound notifications.
- *Sanctioned exception:* `launcher.py`'s loopback self-probe, which uses stdlib urllib against
  this process's own health route before it opens the operator's browser. It asks Reaper
  itself, carries no credentials, and can mutate nothing. The container HEALTHCHECK is the same
  probe.

**34. Every constructed client has an owner that closes it.** A client constructed outside an
exit stack, or without entering one in the same scope, is a leak. Add the close path in the
same diff as the construction. Every branch counts, including early returns and exceptions
raised before the `try`. Construct the client only after the guard that can return early, or
wrap construction through use in one `try/finally` or `AsyncExitStack`. When the caller's own
stack has already entered the client, register the close with `push_async_callback`, never a
second `enter_async_context`.

**56 / 89. Every windowed list read goes through the complete-or-raise paging helper.** Page
through `clients/plex.py`'s `_iter_pages`, or assert `totalSize` completeness. Raw
`server.query(...)` and raw multi-id metadata reads that can silently truncate are banned. Any
unbounded loop gets a page backstop. `plex.SWEEP_MAX_PAGES` raises at the backstop and is the
model. `history_sync.MAX_HISTORY_PAGES` warns and stops instead. Pagination advances and
terminates on the raw page count, never a filtered one. A defensive filter that can shrink a
page raises on an anomaly instead of silently resizing the page. A total-size fallback never
defaults to the page size. A complete-or-raise docstring is a contract. Input that violates it
raises, and the function never returns a partial result. A truncated read of a protection
source silently stops that protection covering most of the library.

**57. Plex removals address the stored spelling, and sections resolve by key.** Group items by
the exact stored tag spelling, casefold-matched as `remove_label` does. Resolve sections with
`sectionByID`. `test_plex_sections_are_never_resolved_by_title` fails on any
`library.section(` call in `src/`. Where only a title is known and it is ambiguous, ask each
same-titled library in turn (`lists.PlexCollection` is the model), never only the first match.

**88. Case-fold both sides of every label, tag, collection, or list-name match, through
`reaper.text.fold`.** When one side of a lookup is lower-cased, lower-case the other too.
Lower-casing the source but not the operator's configured value is a fail-open protection bug.
The protection stops matching and nothing announces it. Every new name-matching path ships a
mixed-case test. `test_repo_hygiene.py` bans spelling `.strip().casefold()` again in `src/`.
This rule covers names only. A path takes a different fold (`identity.to_basename`,
`to_segments`), and so does a token that is not a name.

**114. A sleep, retry budget, or allocation whose size comes from a remote server is clamped**
to both a ceiling and the caller's remaining deadline. `PlexTvClient.wait_for_pin` in
`clients/plextv.py` is the model. `notify/discord.py` clamps to a ceiling only. Any other site
that honors `Retry-After` matches `wait_for_pin`.

**58. A check-then-write re-reads inside the write transaction.** A state check may run on a
separate read connection only if the write transaction re-reads the state it acts on. DDL or
destructive writes driven by pre-lock reads are a blocker.

**59. Multi-key JSON settings update per key or under a guarded merge.** A read-modify-write of a
whole settings dict across an `await` is a blocker.

**81. A baseline edit, even one reverted within hours, obligates a guarded migration.** Never
edit the baseline. `test_alembic_baseline_is_frozen` fails on any byte change to it. If the
baseline was ever wrong in a merged commit, every additive migration that covers that window
carries the heal migration's reflection guard, so in-window databases upgrade instead of
boot-looping.

**94. Every `WHERE col IN :keys` over a scan-sized set is chunked on `db.KEY_CHUNK`.**
`test_every_scan_sized_in_clause_is_chunked_or_classified` fails on any site with no written
classification, and a new spelling ships with the walk that reads it. A new
`parent_rating_key`-style filter also needs its covering index. Reconcile a `cache.db` index by
name and create the missing one in place, never by bumping the column-shape tuple, which drops
the whole mirror.

**95. Every numeric API bound is validated at the boundary with `ge`/`le`, and every
destructive-path list or string carries `max_length`.** A `min()` cap with no floor lets
`limit=-1` become `LIMIT -1`, which is unbounded.

**131. A bound enforced by a consumer derives from the same declaration the producer honors.**
A backup writer that can exceed the member cap of its own restore produces an artifact that
restore refuses. Where a producer and a consumer must agree on a limit, both read one constant.
A test writes at the limit and reads the result back. This is rule 67 generalized from the
TSX/CSS pair to any producer/consumer bound.

**13. Never log a URL that carries a secret.** Keep a secret in a request body or header
wherever the upstream accepts one. Tautulli, MDBList, and plexapi require the key in the query
string, so trace an outbound call by its path only (`base.trace_call`), never the URL with its
query. `logging.redact_secrets` scrubs a query string that reaches a log anyway. Default to
`verify=True` for TLS. Derive at-rest keys with a salted KDF. An operator-supplied key below
the entropy floor logs a warning (`secrets._warn_if_weak`).

## Jobs, notifications, and side effects

**8. Notifications and side-effecting writes are idempotent across repeated calls.** Key them on
durably persisted state (an announced-set), never on a diff that is never persisted. Gate
announcements so preview or read-only mode cannot send them again.

**55. A job's off switch governs every path that runs the job.** Startup catch-ups, recovery
paths, and other side entrances honor the stored off value, or the off-warning copy names the
exception explicitly. That copy states the real, code-verified consequence of turning the job
off, including degradation that blocks runs, never a guessed softer one.

**82. A persistent sink degrades loudly, once.** Any always-on writer (the log file mirror in
`logbuffer.py`) that can fail after setup carries a one-shot degradation flag, surfaced where
its output is consumed. A bare `suppress(Exception)` around a steady-state write whose output is
documented as an audit trail is a blocker.

**87. A guarded startup replay is mirrored on every runtime replay of the same data.** When
startup wraps a stored-value replay in a tolerant guard (malformed cron, bad zone), every
settings-save or reschedule path that replays the same stored values carries the same guard. A
save of values that boot survives then never returns a 500 with half of them applied.

**102. A task created with `create_task` has a done-callback that logs its exception.** A
fire-and-forget startup or maintenance task must not swallow a raise at GC time.

**128. Cancellation does bounded work.** Never run a network settle-wait, a poll loop, or a
purge inside a `CancelledError` handler or its `finally`. A hard cancel is usually the container
shutting down. Holding shutdown open for tens of seconds can also leave the remote half-tidied.
Make the state durable there, and defer the cosmetic tidy-up to the next run.
`executor._commit_and_finalize`'s `canceled` branch is the model.

**129. State with a TTL has a sweeper, not only lazy expiry on presentation.** Expired auth
sessions that are pruned only when the same token is presented again grow the table forever.
Pair every expiry rule with the job that enforces it. Rule 55 applies, so the sweeper's off
switch governs every path that runs it.

**115. A protection-list slug that changes shape disables its predecessor in the same sync
pass, after the replacing sync lands.** Slugs derived from operator settings (match mode,
instance id) leave orphaned rows that `enabled = 1` keeps protecting forever, so a tightening
the operator saved never takes effect. Either disable every slug the current run did not
produce, or keep the setting out of the slug. Retire a slug only on a successful sync, and only
for a family whose source was reachable. A failed sync's slug holds exactly the membership
that must survive.

## Reporting, attribution, and stored explanations

**77. Backend reporting surfaces consult effective overrides.** Any service that summarizes
items as removable, reclaimable, or kept (Scales, breakdowns) merges live override
state the same way the review routes do. Otherwise its copy states explicitly that it shows scan
verdicts only.

**78. Attribution honors the request's scope.** When a request carries a season scope, or any
partial scope, per-person figures bind only the scoped subset. Whole-title binding is allowed
only for unscoped requests, or when the copy beside the number states the granularity.

**96. A why-panel extractor never raises a row off the queue, and its fallback resolves toward
keeping.** Guard every `json.loads` and model construction on a stored explanation with
`(ValueError, TypeError)`. The fallback value is the conservative one, never the permissive
one. A match record that is present but unreadable is a BAD match that holds the reap. It is
never treated as absent, which would clear the reap. A genuinely absent record stays
permissive. Show the unreadable state to the operator, and never print a fabricated number in
its place.

**103. A hardcoded list that mirrors the model or schema set carries a drift guard.** The restore
auth-purge list, generated-asset manifests, and server-defined id lists either derive from one
declaration or are covered by a test that fails when the set changes. When the guard flags a
member, classify it in writing as considered-and-kept. Never silence the guard.

## Sweeping the siblings (rule 72's four backend instances)

The four rules below are one obligation, rule 72's. Grep the siblings, then fix each one or
defer it in writing. The rules differ only in what counts as a sibling. When one applies, check
the other three, because a change rarely creates only one kind of sibling.

**127. An interlock whose docstring says it covers every path is invoked on every path.** When a
docstring says "every", grep for each route into that interlock. In the same change, either
wire the missing routes or narrow the claim. Rule 117 is the same failure for a gate that
cannot fire, and rule 7/24 covers the claim itself.

**140. Sweep every reader of a value you re-qualified, not only the reader that motivated the
change.** When a new fact bounds another fact's validity (a history reach, a coverage span, an
as-of instant), grep every `read=` and `facts.<field>` site for the bounded fact. That covers
built-in gates, signals, graded keeps, the operator-authored rule registry (`engine/fields.py`),
and the season roll-up. Make each site consult the bound, or record in writing why it need not.
Sweep the value, never only the field name. A reader that holds the re-qualified number in a
local variable is invisible to a grep for the field it came from. A bound that only one reader
honors holds in one lane and silently fails in the next, which looks the same as the bug the
bound was added to fix. `Facts.history_reach_days` is the case.

**142. Replacing an inferred discriminator with a typed one is not done until every consumer
that made the same inference reads the type.** Where a consumer sits across a serialization
boundary, shipping the field there is part of the fix, never a follow-up. A wording check, a
substring check, or a prefix check (rule 92's shape) is usually copied, because the producer's
sentence is the only thing every reader can see. Grep the retired check's exact spelling across
both trees. For each surviving site, either carry the field to it (`api/schemas.py` → `api.ts`
→ the component, rule 64's supply chain) or defer it in writing and name that supply chain, so
the deferral is actionable. Carry the three-state where old records lack the key. A `bool` with
a `False` default silently asserts one shape about every legacy row, and `bool | None` lets it
assert neither. `GateResult.defers_to_owner` is the case.

**143. Sweep every consumer that iterates a set whose membership you changed.** Moving items
from `prunable` to `protected` is the keep direction and looks strictly safer in the diff. It
can still remove a protection when a second protection is defined over the set you drained.
`_detect_conflicts` iterates `prunable`, so when every season is held, the keep-rule conflict
stops being raised. Before closing,
grep every iteration of the set (`for … in plan.prunable`, and every comprehension and
membership test over it), and ask what stops firing when the set is empty.

A corollary covers how a hold is encoded. A hold that stands for "we could not answer this" is
`blocked`, never a bare PROTECT. Rule 93's Known/Absent/Unknown distinction applies to gate
results exactly as it does to facts. Conflating them tells the operator a check passed when it
never ran. `engine/verdict.py` records what a block does and does not hold today. Read it
there, and never infer it from the encoding.

## Schema removal

**148. Schema leaves in two releases, never in one, and never by accumulating instead.**
Release M removes the reads, the writes, and the ORM attribute. It also ships whatever revision
the column needs to keep working without Python holding it up. For a `NOT NULL` column with no
server default, that is an `alter_column` that adds one, because the Python-side `default=`
goes away with the attribute and the next `INSERT` omits the column. Release M+1 drops the
column. Only a release in which both images can read the same schema makes the operator's
rollback survivable. A `downgrade()` that recreates the column does not bring its data back.

`docs/DECISIONS.md`'s Migrations section records what "rollback" means here, and why the cost
is measured against the alternative rather than against zero.

The M+1 revision has three obligations. Put every drop for a table inside one
`batch_alter_table` block for that table, because each block is another full copy of the table
(#564). Order the calls `drop_index`, then `drop_constraint`, then `drop_column`. Alembic
reflects an index on a column being dropped and recreates it against a column that is gone.
That mistake is invisible on a fresh database and fatal on a populated one. Assert the
surviving indexes and named constraints in a test, never by eye. A batch recreate silently
drops expression indexes, triggers, and `AUTOINCREMENT`, and Alembic's docs record that unnamed
CHECK constraints do not survive it.

Leave `recreate` at its default. On SQLite the default already recreates the table for a drop.
`recreate="always"` adds nothing here and forces a rebuild on backends that would not need one.

A column that holds anything the operator chose is not dead just because no code reads it.
Prove it write-only across `src/`, `tests/`, and `frontend/src/` before release M, and say in
the PR body what you grepped. Rule 143's sweep applies to a column exactly as it does to a set.
