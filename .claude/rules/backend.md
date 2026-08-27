---
paths:
  - "src/reaper/**/*.py"
  - "alembic/**/*.py"
---

# Backend blockers — `src/reaper/`

Blockers, not suggestions, distilled from six adversarial review passes. **Rule numbers are
permanent** (tests and the archived review passes cite them); where two overlap, the more
specific governs. Code comments state constraints in plain language and do not cite rule numbers.
Rules binding every file are in the root `CLAUDE.md`; the SPA's are in
`.claude/rules/frontend.md`. Holds 1–6, 8–10, 13, 22, 23, 26–35, 38, 52, 55–59, 63, 65, 70, 71, 73, 77, 78, 81, 82, 87–97, 102–117, 124, 127–129, 131, 140, 142, 143, 148.

## The safety / deletion path

**1. Omitted field ≠ explicit empty collection.** On any destructive or filtering path, treat
`None` and `[]` differently, and make an empty selection **fail closed** — it must never
expand to "everything."

**2. Never fail open.** When a whitelist/keep-list sync, a protection source, or an optional
dependency (Plex) fails, degrade the snapshot to **un-executable** rather than proceed on
empty or unverifiable protection data. One deliberate exception: a failed whitelist refresh
may coast on stored membership from a *recent* successful sync
(`snapshot.WHITELIST_STALE_AFTER`, 48h) — the stored copy still protects everything on it.
Past that bound, or with no record of a successful sync, degrade.

**3 / 22. One decision function.** Condemn/abstain/protect lives only in
`engine/verdict.decide_verdict`, and four callers import it: `snapshot`, `simulate`,
`condemned`, and `policy_warnings.inspect` — which is the one every list of them has forgotten,
and whose "nothing will be flagged for removal" warning rests on the decision being monotone in
score and coverage. Never
write `score >= threshold` or `coverage_bp >= floor` inline outside it, and never reimplement
scoring, coverage, rounding, or floors anywhere they can drift.

**4 / 71. Reset time-window clocks on re-entry, and clear them when an item leaves the set.**
Never let a stale first-flagged timestamp skip a safety window. When an override that kept an
item off the reap list is removed, the FirstFlagged row is deleted before
`record_first_flagged_bulk` runs, **unconditionally** — never trust `last_seen_condemned_at`
continuity across a period when the item was invisible to the operator.

**5 / 30. Rank, cap, and count over the exact set acted on.** Filter first (content-bearing
seasons, non-spared deletable items, fetched-vs-all groups), then rank or count. Never expand
caps or counts over items that will later be filtered out; any number shown beside a
destructive button derives from the same set the server will act on.

**23. Every re-decision surface handles every stored verdict state.** Adding or consuming a
Candidate verdict means enumerating all states at every consumer — protect, abstain-blocked,
abstain-by-score, condemn, overrides, expired — and adding the blocked/override cases to the
simulator test in the same change.

**26. Journal and state-transition writes on the deletion path are durably committed at each
step.** Never rely on `flush()` inside a run-long transaction for anything described as an
audit record, and make every state-transition guard an atomic
`UPDATE … WHERE state = :expected`.

**31. Derived condemn-lane values round toward keeping.** When precision is reduced on any
field that can add deletion pressure (dates from years, sizes, ages), choose the bound that
produces *less* pressure.

**38 / 117. A gate or option the operator can enable must be able to fire.** If every fact
builder sets its input `Absent`, either wire the input or retire the gate: remove it from
`GATE_TYPES`, refuse it in `build_gates`, keep its `GateId` so stored explanations still
decode, and refuse to scan under a policy that enables it. Dead safety-adjacent code is
deleted, not stockpiled — a method "for when the interlock lands" ships with its interlock and
tests, or does not exist.

**73. A password-gated destructive confirm is content-bound.** The confirm request carries a
server-verified token derived from the exact content the operator reviewed (recomputed or
stored server-side at stage time), and the action refuses if the content changed since review.
The execute route's phrase is the model; any new stage-review-confirm flow (restore, import,
bulk apply) carries the same binding.

**97. Anything counting what was deleted counts the file's removal, not the bookkeeping after
it.** A live re-resolve returning no files is `_mark_skipped` ("no files resolved; kept"),
never an approved size counted as deleted, and it never overwrites an already-VERIFIED step.
Conversely a step whose file is confirmed gone but whose follow-up (exclusion, refresh) failed
stays FAILED — marking it VERIFIED would make the journal claim a verification that explicitly
failed — and still charges the rolling caps via the durable `file_removed_at` column.

**111. The executor's send loop and `execute()` each carry a catch-all that records terminal
state.** An unmapped exception after a file is already deleted must not leave the step `SENT`,
the run `EXECUTING`, and the report `None` with nothing able to reconcile it. Per-item
surprises funnel through `_fail`; run-level surprises record `ABORTED` and return the report
rather than re-raising into a caller that will not persist it.

**112. The executor re-reads the operator's spare decisions before every item.** A decision map
loaded once at run start means a Spare clicked during a multi-minute reap is ignored and the
file is deleted. Refresh only the per-item spare and effective-set checks, intersected with the
frozen run-start set so the refresh can only ever *remove* items, never add one the operator
never approved; cap math stays on the run-start set (rule 5/30). Route it through the
production `condemned.effective_verdict`, never a second membership copy.

**113. A run's approval is bound to the policy it was planned under.** `run.policy_hash` is
recorded *and enforced at execute time*, with operator copy telling them to re-scan, since a
policy edit does not trigger a scan on its own. A plan built after the edit is refused too, not
just one built before it. Never leave a hash recorded and unread.

**124. A protection derived from viewing position anchors on the most recent play, never the
highest ordinal.** Anchoring the mid-binge guard on the highest season *number* a viewer
touched gives a re-watcher or an out-of-order viewer no protection at all, and a position
derived that way can land outside the set that actually exists — protecting nothing while
reading as a live guard. Derive the anchor from the latest play, and check that the anchored
position is a member of the set before treating it as cover. Specials count as a season.

**116. A degraded snapshot's side effects are gated with its plan.** Un-plannable also means
un-announced: grace clocks, the Leaving Soon shelf, and Discord all read the condemned set, and
none may act on evidence the scan itself declared untrustworthy.

## Evidence, degradation, and the fact layer

**27 / 90. A protection container that cannot be found is an error, never an empty result.**
Before any atomic `DELETE` + reinsert of protection membership, distinguish three cases from a
genuine empty: container-missing, malformed body, and a populated container that filters down
to zero *usable* items. Any of them with members already stored must raise, so the previous
membership survives and the snapshot degrades.

**28. Failure of any evidence source degrades the snapshot.** Every `except` around a source
read in the scan pipeline appends to `pre_scan_degradations` or calls `context.degrade`; a bare
`log.warning` on a source failure is a review-blocker. Watch history is a source. A success
response carrying a null or malformed body is not a genuine empty either.
- *Sanctioned exception:* a **per-item** missing size does not degrade. Degradation is
  snapshot-global and a degraded snapshot is un-plannable outright, so one unsizable movie
  would block the operator's entire run — the wrong blast radius. The compensating control is
  narrower and stronger: that item alone is held back from every plan (`planner.build_plan`)
  and refused again at send (`executor._may_send_unmeasured`, which also requires the frozen
  size to measure the same quantity the live re-read will), and the operator is told the count
  and which items. **Neither refusal is unconditional** — both yield to the owner's allowance
  (`ProfileSettings.max_unmeasured_per_run`): above zero a bounded number are planned, are
  charged to the item caps, and are sent with the growth interlock unavailable. `size_confirmed`
  is the *counting*-side predicate (`_deletable`, `api.runs._planned_candidates`) and is not the
  send refusal; collapsing the send back to it drops the comparable-quantity half. This covers
  the item's own size only; a source that fails to *respond* still degrades.
- *Sanctioned exception:* a source that can only ever *add* condemn evidence (the batch
  enrichment in `season_scan`) may log instead of degrading, because losing it can only lower
  pressure, which is the keep direction; the comment must say so. A source whose loss can
  *withdraw* a protection never qualifies.

**35. New `Facts` fields are populated — or explicitly `Absent` with a comment — in every fact
builder.** Two build from evidence — `snapshot.build_facts` and `season_scan.build_season_facts` —
and one populated in a single path silently changes scores and coverage in the other. Two more
construct a `Facts` and are just as easy to miss: `facts_codec.facts_from_dict`, which thaws only
what a builder already wrote, and `preview._bare_facts`, whose `Facts(**observations)` raises at
runtime for a field with no default and whose `# type: ignore` on the unpack hides that from mypy.
Grep for `Facts(`, not for the two you remember.

**65 / 91. Silent recovery on operator-configured safety values is forbidden, and a config read
*failure* is not "nothing configured."** A fallback replacing saved profile/policy values
surfaces a flag the UI renders and degrades the scan, following the `ActivePolicy` pattern; a
log line alone is a blocker. On any safety-scoping path a read error degrades the snapshot, and
only a *successful* read finding nothing may fall back to the permissive default. A transient
error must never silently widen what can be reaped — and copy calling the wider scope the
"safe" fallback is wrong, because widening is the condemn direction.

**70. Time-bounded state has exactly one durable realization point, shipped with the feature.**
Any stored decision that expires (a timed spare, a deadline, a TTL) is realized by code that
WRITES the transition — an in-memory filter at read time is not a realization — and every live
consumer converges after it. A docstring saying "the next scan realizes it" requires the scan
to actually persist that realization in the same change.

**92. Degradation is detected by a typed flag on the context, never by substring-matching a
free-text reason.** Any `"some-source" in " ".join(reasons)` coupling between producer and
consumer is a blocker: the reason string is operator copy and will be reworded. Carry an
explicit boolean (`activity_degraded`).

**93. `Absent` means "we looked and there is genuinely nothing"; a source that could not be read
is `Unknown`.** Never route a read failure to `Absent` — it withdraws the protection
library-wide and prints a why-panel asserting a check that never ran, while `Unknown` blocks
the gate and takes the full keep discount. Conversely a genuine `Absent` on a numeric signal
routes to `NOT_APPLICABLE` (evaluated, weight retained, coverage intact), as `SEASON_RANK` and
the graded custom path already do, never to the `UNREADABLE` branch. Degrading the snapshot is
necessary but not sufficient, and a comment claiming degradation already prevents this is the
bug.

**104. A value derived two ways in two modules is derived once in a shared helper, and the
helper defines what a record lacking it thaws as.** Dormancy days (`engine/dormancy.py`),
condemn/score/coverage, and any parallel field list (`_OBS_FIELDS`) have exactly one
derivation; prefer `dataclasses.fields(...)` over a hand-maintained parallel list. Moving a
derivation to the write side moves the problem to the read side, so state the thaw explicitly:
a key a stored snapshot predates is `Unknown`, never `Absent` and never a `KeyError`.

**105. A stored policy body that gains a protection-bearing field ships a loader shim in the
same change, and the shim degrades the scan.** When a field moves out of a gate row into the
body (as the rating bars did), a body written before the move is migrated on load, keyed on the
raw key being *absent* (an explicit `[]` is an operator who cleared it deliberately, rule 1),
never on `schema_version`, which cannot discriminate across a change that did not bump it.
Recover only where something actually was protecting: a *disabled* gate is left alone. The
migrated body sets `ActivePolicy.repaired`, degrades the scan, and opens the editor on it as an
unsaved draft. A protection that silently evaluates to "nothing configured" is the worst
outcome this codebase has.

**107. A field offered in the policy vocabulary is populated by the fact builder for every media
type it is offered on.** A `FieldSpec` with no `media_types=` is offered on both policies; if
the season builder hardcodes it `Absent`, restrict the spec in the same change, on *both* lanes.
Removal weights sum to a fixed 100, so a condemn rule on an always-`Absent` field permanently
depresses every score in that media type rather than merely never firing. Operators holding a
stored rule that just became unofferable are warned, not silently dropped.

## Identity, joins, and keys

**6. Disambiguate cross-system joins by a stable identifier** (year + title, not title alone),
and refuse to bind on ambiguity (return Unknown / ABSTAIN). Never silently last-write-wins into
a `dict[title, row]` map.

**29 / 106. Every id the item carries — every kind and every spelling — goes into every
lookup.** Pass imdb+tmdb+tvdb together to `membership_index.lookup` or any cross-system join,
and an item holding both `imdb_id` and `plex_imdb_id` is looked up with
`item.imdb_id or item.plex_imdb_id`, on the movie path exactly as on the TV path. Passing one
where two exist is a fail-open protection bug. Adding a new id kind to storage means grepping
and updating every lookup call site in the same change.

**52. A bare tmdb id is not a stable key across media kinds.** Movie and TV tmdb ids share one
integer space, so every map, index, or lookup keyed on a tmdb id carries the media kind
alongside the number, on both the write and the read side.

**63. Rows are keyed and aggregated by a stable server id, never a display name.** If the schema
lacks an id, add one in the same change; user-level roll-ups key on the always-present per-user
id, not an optional linked-account id. This binds membership indexes and path tables as much as
display rows: any dict whose key can collide is a bug, and a display name always can.

**109. An identity tier that can corroborate a bind is computed even when an earlier tier already
bound — as a cross-check only, never as an originator.** Pass the binding ids explicitly rather
than reordering a priority tuple, so a corroborating id kind can add an abstain but can never
originate a bind. A `tier1 is None` guard in front of a corroborating tier makes the documented
contradiction veto structurally undetectable. A multi-hit tier is silence, not a contradiction,
and a hit inside the earlier tier's merged group is agreement.

## Policy evaluation

**32. Typed condition values validate against the field's type at the boundary, and evaluation
never raises out of a scan.** Rule evaluation errors degrade that item as blocked; a stored
policy must not be able to crash `score()` or `evaluate_all`.

**108. A text condition value is rejected at the save boundary when it strips to empty, or
when it carries the separator its fact is joined on.** `contains ""` matches every item and
lands the rule's full weight library-wide; `in ""` can never match and reports as a green
"checked, did not fire." Reject `value.strip() == ""` for CONTAINS/IN, and reject an IN target
whose split yields no elements, so a comma-only list cannot pass.

**The separator half is the same defect reached from the other end, and it starts somewhere a
condition validator cannot see.** A multi-valued fact is one string with its elements joined by
a comma (`lists.on_list_fact`), and `fields._compare` splits it back on commas to test
membership — so a value containing one is never an element of its own fact. Where that value is
an operator-typed NAME the app later turns into a condition (a list name became an `on_list`
rule), the refusal belongs on the name, at the boundary that stores it, not on the rule built
from it: by the time a validator sees the condition the operator is on another screen, and the
rule reads to every surface as a live protection covering nothing. It fails in both directions at
once (a list named `Kids, Holiday` protects nothing, and a *different* list named `Holiday`
protects everything on it). So when a stored string will become an element of a joined fact,
refuse the separator where it is typed, and say so in the operator's words.

## Clients & HTTP

**9 / 110. All external HTTP goes through the shared client's error-mapping and retry layer, and
every client method maps its failures to the client's domain error type.** Never call
`self._client.request` directly. One read that lets a raw transport exception escape defeats
every `except <Domain>Error` in the call chain; a method documented "never fatal" catches
`Exception`, not one mapped type. Ensure `@retry` predicates match the exceptions actually
thrown — don't convert-then-fail-to-retry.

**10. Report the accurate error/status.** Map a name-clash to `409` (not `404`), report the
actual timeout kind (not a hardcoded budget), and honor upstream retry signals instead of
dropping them.

**33. All HTTP lives in `clients/`.** A raw `httpx`/`requests` usage outside
`src/reaper/clients/` is a blocker unless named here as a sanctioned exception. GET-shaped
mutating endpoints are classified and gated by path in the guard, never assumed safe by method.
Public unauthenticated GETs go through `clients/public.py`.
- *Sanctioned exception:* `notify/discord.py`'s webhook POST. The webhook URL embeds a
  per-operator secret path the guard's exact-path allow-list cannot express; the URL is
  validated to Discord's hosts at the API edge, and the client only sends outbound
  notifications.
- *Sanctioned exception:* `launcher.py`'s loopback self-probe (stdlib urllib against this
  process's own health route, before opening the operator's browser). It asks Reaper
  itself, carries no credentials, and can mutate nothing; the container HEALTHCHECK is
  the same probe.

**34. Every constructed client has an owner that closes it.** A client constructed outside an
exit stack (or without entering one in the same scope) is a leak; add the close path in the same
diff as the construction. Every branch counts, including early returns and exceptions raised
before the `try`: construct the client only *after* the guard that can return early, or wrap
construction-through-use in one `try/finally` or `AsyncExitStack`. When the caller's own stack
already entered the client for real, register the close with `push_async_callback`, never a
second `enter_async_context`.

**56 / 89. Every windowed list read goes through the complete-or-raise paging helper.** Page
through `clients/plex.py`'s `_iter_pages` or assert `totalSize` completeness; raw
`server.query(...)` and raw multi-id metadata reads that can silently truncate are banned, and
any unbounded loop gets a page backstop (`MAX_HISTORY_PAGES` is the model). Pagination advances
and terminates on the **raw** page count, never a filtered one: a defensive filter that can
shrink a page raises on anomaly rather than silently resizing it, and a total-size fallback
never defaults to the page size. A complete-or-raise docstring is a contract — violating input
raises, it never returns a partial result. A truncated read of a protection source is a
protection that quietly stopped covering most of the library.

**57. Plex removals address the stored spelling, and sections resolve by key.** Group items by
the exact stored tag spelling (casefold-matched, following `remove_label`) and resolve sections
via `sectionByID`. `library.section(title)` is **banned in `src/` outright**, and this binds
every call — trash, refresh, count, and refresh-status, not just label and collection writes.
Where only a title is known and it is ambiguous, ask each same-titled library in turn
(`lists.PlexCollection` is the model), never the first match.

**88. Case-fold both sides of every label, tag, collection, or list-name match, through
`reaper.text.fold`.** When one side of a lookup is lower-cased, the other must be too.
Lower-casing the source but not the operator's configured value is a fail-open protection bug:
the protection stops matching and nothing announces it. Every new name-matching path ships a
mixed-case test. `test_repo_hygiene.py` bans spelling `.strip().casefold()` again in `src/`.
Folding a
PATH is a different job with a different answer (`identity.to_basename`, `to_segments`), and so
is a token that is not a name.

**114. A sleep, retry budget, or allocation whose size comes from a remote server is clamped** —
to a ceiling *and* to the caller's remaining deadline. `notify/discord.py`'s `_MAX_RETRY_AFTER`
is the pattern; any other site honoring `Retry-After` matches it.

**58. A check-then-write re-reads inside the write transaction.** Splitting a state check into a
read connection is fine only if the write transaction re-reads the state it acts on; DDL or
destructive writes driven by pre-lock reads are a blocker.

**59. Multi-key JSON settings update per key or under a guarded merge.** A read-modify-write of a
whole settings dict across an `await` is a blocker.

**81. A baseline edit — even one reverted within hours — obligates a guarded migration.** If the
frozen baseline was ever wrong in a merged commit, every additive migration covering that window
carries the heal migration's reflection guard, so in-window databases upgrade instead of
boot-looping. Never edit the baseline; when the rule is broken anyway, the follow-up is guarded,
not plain.

**94. Every `WHERE col IN :keys` over a scan-sized set is chunked on `db.KEY_CHUNK`.** An
unchunked expanding bindparam overflows SQLite's variable ceiling and aborts the scan; chunk it,
or express the filter as an anti-join. **The bound is one declaration and the sweep is a gate**:
this rule spelled the number in prose and nowhere in code, five sites spelled it five ways, and
the grace report shipped reading the whole condemned set in one statement, so
`test_repo_hygiene.py` collects the three spellings `src/` uses — the ORM operators, an
`expanding` bindparam, a hand-built placeholder list — and fails on one carrying no written
classification. A **fourth** spelling is invisible to it and has no count to go missing from
(rule 147), so a new form ships with the walk that reads it. A new `parent_rating_key`-style filter also needs its covering index.
Reconcile a `cache.db` index by name and create the missing one in place; never bump the
column-shape tuple to force it, which drops the whole mirror.

**95. Every numeric API bound is validated at the boundary with `ge`/`le`, and every
destructive-path list or string carries `max_length`.** A `min()` cap with no floor lets
`limit=-1` become `LIMIT -1`, which is unbounded.

**131. A bound enforced by a consumer derives from the same declaration the producer honors.**
A backup writer that can exceed the member cap its own restore refuses produces an artifact
only it can read. Where a producer and a consumer must agree on a limit, both read one
constant, and a test writes at the limit and reads it back. This is rule 67 generalized off
the TSX/CSS pair to any producer/consumer bound.

**13. Never put secrets in URL query strings or path components that get logged.** Keep them in
request bodies/headers, default `verify=True` for TLS, and derive at-rest keys with a salted KDF
plus an entropy floor on operator-supplied keys.
## Jobs, notifications, and side effects

**8. Notifications and side-effecting writes are idempotent across repeated calls,** keyed on
durably-persisted state (an announced-set), not on a diff that is never persisted. Gate
announcements so preview / read-only mode cannot re-spam.

**55. A job's off switch governs every path that runs the job.** Startup catch-ups, recovery
paths, and other side entrances honor the stored off value, or the off-warning copy explicitly
names the exception. That copy states the real, code-verified consequence of turning the job
off, including degradation that blocks runs, never a guessed softer one.

**82. A persistent sink degrades loudly, once.** Any always-on writer (log file mirror, export
stream) that can fail after setup carries a one-shot degradation flag surfaced where its output
is consumed. A bare `suppress(Exception)` around a steady-state write whose output is documented
as an audit trail is a blocker.

**87. A guarded startup replay is mirrored on every runtime replay of the same data.** When
startup wraps a stored-value replay in a tolerant guard (malformed cron, bad zone), every
settings-save or reschedule path replaying the same stored values carries the same guard, so a
save can never 500-and-half-apply what boot survives.

**102. A task created with `create_task` has a done-callback that logs its exception.** A
fire-and-forget startup or maintenance task must not swallow a raise at GC time.

**128. Cancellation does bounded work.** Never run a network settle-wait, a poll loop, or a
purge inside a `CancelledError` handler or its `finally`: a hard cancel is usually the
container going down, and holding shutdown open for tens of seconds can also leave the remote
half-tidied. Make the state durable there, and defer the cosmetic tidy-up to the next run.
`executor._commit_and_finalize`'s `canceled` branch is the model.

**129. State with a TTL has a sweeper, not only lazy expiry on presentation.** Expired auth
sessions pruned only when the same token is presented again grow the table forever. Pair every
expiry rule with the job that enforces it, and honor rule 55: the sweeper's off switch governs
every path that runs it.

**115. A protection-list slug that changes shape disables its predecessor in the same
transaction.** Slugs derived from operator settings (match mode, instance id) leave orphaned rows
that `enabled = 1` keeps protecting forever, so a tightening the operator saved never takes
effect. Either disable every slug not produced by the current run, or keep the setting out of the
slug. Retire only on a *successful* sync, and only for a family whose source was actually
reachable: a failed sync's slug is exactly the membership that must survive.

## Reporting, attribution, and stored explanations

**77. Backend reporting surfaces consult effective overrides.** Any service summarizing items as
removable/reclaimable/kept (Scales, breakdowns, exports) merges live override state the same way
the review routes do, or its copy explicitly states it shows scan verdicts only.

**78. Attribution honors the request's scope.** When a request carries a season (or any partial)
scope, per-person figures bind only the scoped subset; whole-title binding is allowed only for
unscoped requests, or with the granularity stated in the copy beside the number.

**96. A why-panel extractor never raises a row off the queue, and its fallback resolves toward
keeping.** Guard every `json.loads` plus model construction on a stored explanation with
`(ValueError, TypeError)`. The fallback value is the *conservative* one, never the permissive
one: a match record that is present but unreadable is a BAD match that holds the reap, not an
absent one that clears it. Genuinely absent stays permissive; unreadable does not. Surface the
unreadable state to the operator rather than printing a fabricated number in its place.

**103. A hardcoded list that mirrors the model or schema set carries a drift guard.** The restore
auth-purge list, generated-asset manifests, and server-defined id lists either derive from one
declaration or are covered by a test that fails when the set changes. When the guard flags a
member, classify it in writing as considered-and-kept rather than silencing it.

## Sweeping the siblings — rule 72's four backend instances

All four are one obligation, rule 72's: **grep the siblings, then fix each or defer it in
writing.** They differ only in what counts as a sibling. Finding that one applies is a reason
to check the other three, because a change rarely creates just one kind of sibling.

**127. An interlock whose docstring says it covers *every* path is invoked on every path.**
"Every" is a grep trigger: check each route into that interlock and either wire the missing
ones or narrow the claim, in the same change. Season pruning once never refreshed Plex, so the
end-of-run trash interlock never ran for TV libraries at all while its docstring claimed every
deletion routed through an *arr. Rule 117 is this same failure for a gate that cannot fire,
rule 7/24 for the claim itself.

**140. Sweep every reader of a *value* you re-qualified, not only the reader that motivated
the change.** When a new fact bounds another fact's validity (a history reach, a coverage span,
an as-of instant), grep every `read=` and `facts.<field>` site for the bounded fact — built-in
gates, signals, graded keeps, the operator-authored rule registry (`engine/fields.py`), the
season roll-up — and make each one consult the bound or record in writing why it need not.
**Sweep the value, not the field name**: a reader that holds the re-qualified number in a local
variable is invisible to a grep for the field it came from. A bound honored by one reader is a
protection that holds in one lane and silently does not in the next, which is indistinguishable
from the bug the bound was added to fix. `Facts.history_reach_days` is the case.

**142. Replacing an inferred discriminator with a typed one is not done until every consumer
that made the same inference reads the type**, and where a consumer sits across a serialization
boundary, shipping the field there is part of the fix rather than a follow-up. A wording test, a
substring, a prefix (rule 92's shape) is usually copied, because the producer's sentence is the
only thing every reader can see. So grep the retired test's
exact spelling across **both** trees, then for each surviving site either carry the field to it
(`api/schemas.py` → `api.ts` → the component, rule 64's supply chain) or defer it in writing
*naming that supply chain*, so the deferral is actionable. **Carry the three-state where old
records lack the key**: `bool` with a `False` default silently asserts one shape about every
legacy row, and `bool | None` is what lets it assert neither.
`GateResult.defers_to_owner` is the case.

**143. Sweep every consumer that iterates a set whose membership you changed.** This is the
instance that looks safest, because moving items from `prunable` to `protected` is
unambiguously the keep direction and reads as strictly safer at the diff. It is not, when a
second protection is defined *over* the set you drained: `_detect_conflicts` iterates
`prunable`, so holding every season also stopped raising the keep-rule conflict, and the net
effect of a change that added protection was that a season a hand reap had been refused on
became one a hand reap deletes. Grep every iteration of the set (`for … in plan.prunable`,
every comprehension and membership test over it) before closing, and ask what stops firing
when it is empty.

**The corollary is about encoding.** A hold standing in for "we could not answer this" is
`blocked`, never a bare PROTECT: rule 93's Known/Absent/Unknown distinction applies to gate
results exactly as it does to facts, and conflating them tells the operator a check passed when
it never ran. `engine/verdict.py` carries what a block does and does not hold today; do not
infer it from the encoding.

## Schema removal

**148. Schema leaves in two releases, never in one, and never by accumulating instead.**
Release M removes the reads, the writes and the ORM attribute, and ships whatever revision the
column needs to keep working without Python holding it up — for a `NOT NULL` column with no
server default, an `alter_column` adding one, because the Python-side `default=` dies with the
attribute and the next `INSERT` omits the column. Release M+1 drops the column. One release
where both images *can read the same schema* is what makes the operator's rollback
survivable, and it is the only thing that does: a `downgrade()` recreating the column does not
bring its data back.

The reasoning behind what "rollback" means here, and why the cost is measured against the
alternative rather than against zero, is recorded in `docs/DECISIONS.md`'s Migrations section.

**Three obligations on the M+1 revision.** One `batch_alter_table` block per table with every
one of that table's drops inside it, since each block is another full copy of the table (#564).
`drop_index` before `drop_constraint` before
`drop_column`: Alembic reflects an index on a column being dropped and recreates it against a
column that is gone, a two-line slip invisible on a fresh database and fatal on a populated one.
Assert the surviving indexes and named constraints in a test rather than by eye: a batch recreate
silently drops expression indexes, triggers and `AUTOINCREMENT`, and Alembic's docs record that
unnamed CHECK constraints do not survive it.

**Leave `recreate` at its default.** On SQLite the default already recreates for a drop;
`recreate="always"` adds nothing here and forces a rebuild on backends that would not need one.

**A column holding anything the operator chose is not dead because no code reads it.** Prove it
write-only across `src/`, `tests/` and `frontend/src/` before release M, and say in the PR body
what you grepped. Rule 143's sweep applies to a column exactly as it does to a set.
