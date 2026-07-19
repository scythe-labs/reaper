# Learnings

> Findings from building Reaper against a real, large, actively-used Plex library.
> **Everything here was measured, and most of it contradicted a reasonable-sounding
> assumption.**
>
> Negative results are recorded too. "We tried X and it made things worse" is often
> more valuable than the fix, because it stops the next person re-trying X.
>
> Figures are given as ratios and orders of magnitude on purpose: the point is the
> *shape* of the finding, which generalises, not one server's numbers, which do not.

---

## The scoring engine

### The population trap — this is the big one

**A base-rate baseline computed over the wrong population does not merely give an
imprecise answer. It gives one with the wrong sign.**

This was got wrong **twice** while building Reaper, and each time it produced a
confident, articulate, completely false conclusion.

1. **Baseline over films that had *ever been played*,** while the model condemned a
   set dominated by films that had *not*. Conclusion: *"the scorer is worse than
   random."* False.
2. **Baseline over every `rating_key` in Tautulli's history** — but that table also
   contains **items long since deleted from the library**, which by definition are
   never watched again. Their zeroes dragged every bucket down several-fold and made a
   *working* scorer look like it had strongly negative lift.

Correct answer: compute the baseline over **exactly** what the scorer scores.

The cost of this mistake was real: a working feature was nearly ripped out, and a
wrong conclusion was written into the docs before being caught.

> **Before believing any lift/AUC/skill number, ask: is the baseline population the
> same population the model scores?**

### Still: reward is not risk. Never mix them.

`SIZE` originally carried a fifth of the score. That is backwards.

Size measures *how much you gain* by deleting (reward). It says nothing about *whether
anyone wants the file* (risk). And on a real library, **big files are big because they
are popular** — the largest files are 4K features.

Removing it (and adding a dormancy gate) cut regret by a third and roughly doubled
skill over the age-matched baseline. So the reasoning was right even though the "worse
than random" claim that originally motivated it was an artefact of the bad baseline.

> Use a reward term to **rank** what the risk model has already selected. Never to
> select.

### Almost all the signal is in one variable

Dormancy alone scores marginally *better* lift than the full four-signal set.
`FEW_WATCHERS` and `LOW_RATING` contribute nothing measurable on top of it.

Most of the apparent sophistication of a weighted scorer is usually one variable
wearing a hat. Measure each one's marginal lift or you will carry four signals'-worth
of complexity and bug surface for one signal's-worth of skill.

### Unknown must be structurally incapable of condemning

Three states, never two: `Known` / `Absent` / `Unknown`.

- `Absent` — we looked, nobody watched it. **Real evidence.** May condemn.
- `Unknown` — we could not look (outage, stale data, unmatched item). **Never
  evidence.** May only protect.

An empty list from a *failed* call must be `Unknown`, never `[]`. Every competitor
that conflates them eventually deletes something during an API outage.

### `next(...)` over a multi-instance list is a silent half-scan

The scan route did `next(r for r in rows if r.kind is RADARR)` and got whichever
instance happened to come first — the small 4K one. It scanned a **tiny fraction** of
the library and reported a clean, confident, *non-degraded* result.

A silently-partial scan is worse than a failed one: the owner reviews a candidate list
they believe is complete. And it would never have surfaced in a single-instance test.

⇒ Where a list is the domain model (N Sonarr, N Radarr), `next()` and `[0]` are bugs.
Iterate, and **degrade** if one is unreachable rather than quietly shrinking the library.

### "Incremental" that still fetches everything isn't incremental

The history sync skipped rows it already held but still *paged through all of Tautulli's
history every scan* -- minutes on a large one -- to insert a handful of new rows. It was
incremental in what it wrote, not in what it fetched.

The instinct was to stop early at the first known row. That is wrong for this API: probed
live, Tautulli **cannot sort `get_history` by insertion id** (`order_column=id` is
silently ignored), so the only order is newest-first *by watched date* -- and a
backfilled old event (a history import, a delayed play) lands with an old date, behind
rows we already hold, where early termination would miss it.

The right lever was the **`after` date filter**: `after=<our newest day>` returns only
the delta (~200 rows against [redacted]), and `INSERT OR REPLACE` on the stable `row_id` makes
a one-day overlap free. Backfill is then caught by a **nightly full sweep**. Per-scan
history sync went from ~3 minutes to sub-second, verified live.

Two things the probe also settled, each a latent bug:
- **`row_id` is null only for live/in-progress sessions** (they cluster at the newest
  end; ~10 across a [redacted] history). Skipping them is correct -- they are not history yet.
- **The regression check was a silent no-op.** It compared our *own mirror's* row count
  before/after, but we write with `INSERT OR REPLACE` and never delete, so the mirror
  only grows -- the check could never fire. A Tautulli reset/prune (the #1 mass-deletion
  vector) would have sailed straight through. It now compares Tautulli's own reported
  `recordsTotal` against the last value we stored, which actually detects a shrink.

The lesson: "incremental" and "has a safety check" are claims to verify by measurement,
not to trust because the code has a loop that looks like it skips work and an `if` that
looks like it guards.

### A rebuildable cache must be readable when it is empty

The cache database is disposable by design -- delete it and it rebuilds. But the scan
read `watch_event` (the Tautulli history mirror) *directly*, and the table is only
created by the history sync. So on a fresh install, or any time the cache was cleared,
the very first scan crashed a hundred frames deep with `no such table: watch_event`.

It never showed up in development because the dev cache always had history in it from an
earlier run. It surfaced the instant the cache was cleared -- i.e. it would have hit
every real first-time user.

Two fixes, both needed:
- **Ensure the schema before every read**, not only before a write. A never-synced cache
  must read as "no history yet" (which degrades the snapshot loudly and leaves dormancy
  Unknown, and Unknown protects) rather than raising.
- **The scan must sync history itself**, at the start, before scoring. It had been
  assuming some external process populated the mirror -- so a fresh install would never
  have pulled any history at all, and would have judged the whole library on nothing.

The general lesson: a rebuildable cache is only rebuildable if *every* reader tolerates
it being empty. "It's always populated in practice" is how a fresh-install crash hides.

### Rebuildable caches must not share a database with migrated state

The Tautulli history mirror and the IMDb dataset take minutes to rebuild and lived in
the same SQLite file as the schema. A routine `rm reaper.db` during a migration fix
destroyed them — twice.

Worse: a cache table *leaked into a migration*. Alembic's autogenerate saw `imdb_rating`
in `reaper.db`, didn't recognise it, and helpfully proposed to `create_table` it. That
migration then failed on a fresh database.

⇒ Split them. `reaper.db` is small, precious, migrated. `cache.db` is **orders of
magnitude larger**, reconstructible, and **never migrated**. The separation also states
the invariant out loud: *nothing in the cache is a source of truth.* There is now a test
asserting no migration ever creates a cache table.

### An unused argument is invisible to the type checker

`run(..., prior=prior)` accepted the argument and then never passed it into the result.
`mypy --strict` was perfectly happy — an unused parameter is legal — and the backtest
quietly kept using the hardcoded fallback while reporting a lift number that *looked*
fine.

It was only caught because the summary line printed *"borrowed from another library"*
when it should have said *"measured on your library"*. Had that provenance label not
existed, the bug would have shipped invisibly.

⇒ **Wiring needs a test, not a type.** And a value that can come from two sources
should always carry a label saying which one it came from.

### Two code paths answering one question will drift, and the UI is where you find out

The scan decides an item's verdict with the full evidence in hand. The simulator
re-decides it later from nothing but the stored row. They are two implementations of
*"would this policy delete this?"*, and they disagreed:

- The scan compared the **float** score (69.7) against the threshold and abstained.
- It then persisted `round(69.7)` = **70**.
- The simulator, which only ever sees the stored 70, condemned it.

So the review queue listed a film under "not judged" while the policy editor counted it
among the deletions — at the same threshold, on the same snapshot. The queue rendered
*"score 70, your threshold is 70, not judged"*, which reads as a bug even to a careful
user, because the number that actually decided (69.7) is one the UI never shows.

Neither unit tests nor `mypy --strict` could see this: both paths were individually
correct, and the drift lived in the boundary between them. **It took building the UI
and putting both answers on one screen.**

⇒ Round **first**, then decide, and store exactly what decided. Not "fix the
comparison" — make the two paths share an input so they *cannot* diverge. Where two
components must agree, the cheapest guarantee is that they decide on the same bytes.

### A simulator must know the limits of what it can simulate

Re-deciding a stored snapshot at a new threshold is exact and free. Re-deciding it under
a new *weight* or a new *gate* is not possible at all — the stored scores were produced
by the old ones, and recovering the new answer would mean re-reading the library.

The dangerous part is that the arithmetic still *works*. You can compute a count from
stale scores and render it, and it will look exactly as authoritative as a true one. A
policy editor with a weight slider and a live count is therefore a trap unless it can
tell the two situations apart.

⇒ Hash the policy's *scoring behaviour* separately from its thresholds
(`PolicyBody.scoring_hash()`), record it on the snapshot, and **refuse to answer** when
they differ. A plausible wrong number is worse than a blank, because the owner acts on
it.

### Signed weights invert under failure

Under a signed score (baseline 50, subtract for "well rated"), an `Unknown` removes a
*negative* contribution and the score **rises** — so an outage makes media *more*
condemned. Signals must be **unsigned** (`[0, weight]`), with the denominator
including unknown weights, so missing data can only push the score *down*.

---

## What an active library actually looks like

The single most important empirical finding, and the one that should change how you
think about this whole category of tool:

> **A big media library is not mostly dead weight.** Nearly everything gets watched
> eventually, and a film watched in the past year has a *majority* chance of being
> watched again within the next one.

Intuitions built on "most of this is abandoned" are simply false on an active server —
and they are exactly the intuitions a pruning tool gets built on. On the library used
for development, the fraction of films that had **never** been played at all was well
under one percent.

### Rewatch probability by dormancy — the ground truth

Measured over the **correct** population (films actually in the library), on one active
server. The absolute numbers are that server's; the **shape** is the finding.

| Dormant for | Rewatched within the next year |
|---|---|
| 0–365d | ~60% |
| 365–548d | ~31% |
| 548–730d | ~32% |
| 730–1095d | ~30% |
| 1095–1825d | ~19% |
| 1825d+ | ~13% |

Two things fall out of this, and both are load-bearing:

**There is no cliff, and nothing is ever free to delete.** A film dormant for *five
years* still has a double-digit chance of being watched next year. Dormancy of one to
two years means almost nothing — people circle back on that timescale routinely.

**The best policy measured still carries ~12% regret**: roughly one deletion in eight
is a film someone comes back for. That is not a tuning failure — it is what an active
library looks like, and it is why the grace period and the human approval gate are not
decoration.

⇒ This curve is a property of *an audience*, not a constant. Reaper therefore **derives
it from the operator's own history** at calibration time and only falls back to a
documented default when there is too little history to fit one. Never ship someone
else's rewatch curve as if it were physics.

---

## API footguns (all verified against live instances)

### A section listing's two rating slots hide most of Plex's ratings (measured 2026-07-17)

The `/library/sections/{key}/all` listing carries exactly two rating slots
(`rating`/`audienceRating` + their images). On a live server whose agent fills the
audience slot with IMDb, the listing showed **imdb-only for 40/40 sampled movies** —
no Rotten Tomatoes value anywhere, so a "Rotten Tomatoes audience" protection or chip
built from the listing alone can never fire on that library shape.

The **full metadata** (`/library/metadata/{ids}`, batchable 100 ids per request)
carries one typed `Rating` child per provider score, critic and audience separately:
`type="critic" image="rottentomatoes://image.rating.ripe|rotten"` is the Tomatometer,
`type="audience" image="rottentomatoes://image.rating.upright|spilled"` the audience
score, plus `imdb://` and `themoviedb://` children (which arrive as `type="audience"`).
Coverage on the same sample: RT audience 39/40 movies, RT critic 36/40, IMDb and TMDb
40/40; shows carry the same children in the folder-path batch the scan already fetches.
Values are 0-10 like every Plex rating (measured range 2.6–8.9), attributes are exactly
`(image, type, value)` — no vote counts. `includeRatings=1` on a section listing
returns **zero** children; the batched metadata read is the only way in.

⇒ The sweep batch-reads metadata for movies too (~1 request per 100 items) and routes
children through `from_plex(..., audience=type=="audience")`, slots keeping precedence.

### Radarr's ratings object includes Trakt (measured 2026-07-17)

`ratings.trakt` is present with a value on ~99-100% of movies across two live Radarr
instances (alongside imdb/tmdb/metacritic/rottenTomatoes at their known ~86-100%
coverages). Sonarr still exposes only the flat TVDB pair, so Trakt on TV would need
another source; the why-panel simply hides the chip there.

### A show folder has no size, so a leaf-only match cannot separate two libraries (measured 2026-07-18)

The negative result that drove the folder corroborator in `engine/identity.py`.

An operator running a split library (an HD *arr instance and a 4K one, mirrored by two
enabled Plex show sections) keeps a handful of titles in **both**. Those titles are then
listed twice in Plex under one TVDB guid. The resolver's tier-1 narrowing had two
corroborators for that case, and on a show **neither can fire**:

- the *file name*, which for a show is the **folder leaf** — and both sections name the
  folder identically, because both were built by the same *arr naming rules;
- the *exact byte size*, which a show folder does not have at all (`resolve_show`
  passed `file_size=None` by construction, and Plex reports no size on a show Location).

So every such title abstained. Measured on a live library: **0.6% of series and the same
share of season rows, 6 and 13 of them, 3 titles × 2 instances** — every one of them the only
titles the operator keeps in both libraries. The failure set is exactly "titles
duplicated across two enabled show sections"; nothing about the titles themselves
mattered (two were documentaries and one carried a parenthesized year, all coincidence).

The discriminator that *was* available had been thrown away one function earlier:
`clients/plex.py` reduced each Location to `to_basename(path)` and discarded the rest of
the path. Measured across all three: leaf identical, **parent folder different, shared
suffix depth exactly 1**. Keeping the full path and comparing *trailing segments* (never
whole paths — the mount roots differ, which is why `to_basename` exists) separates them
cleanly. Re-running the real resolver over the live library: **ambiguous 6 → 0, matched
972 → 978, unmatched unchanged at 112.**

⇒ Two rules fall out. A discriminator must not be normalized away *before* the place
that needs it. And on any split library, the segment **above** the leaf is the only thing
that distinguishes two copies of one title — for shows it is the sole corroborator that
exists, so a tie there still abstains and always will.

**Narrowed 2026-07-19 (code review B-2). The measured win above overstates what the
corroborator can do**, and the numbers must not be restated without re-running it. The
comparison may no longer consume either side's **mount root**: two *arr instances that
each map their own host directory to the same container path (the common setup, both
reporting the same path) produce a shared-suffix win that is a coincidence between the
*arr's root name and one library's folder name, not evidence of identity — and it bound
the wrong copy, reading the other copy's watch history and added-at.

**Narrowed again, same day: a fixed one-segment strip does not remove a root.** The first
attempt cut exactly one leading segment off each side. Adversarial verification broke it
in the layout the popular single-mount guides recommend, where each container maps its
host directory to a **two**-segment root. One segment came off, the second root segment
stayed, it happened to name one library's folder, the strict margin fired on it, and the
4K entry bound the HD listing — the exact byte size that would have separated them never
reached. Path length cannot say where a root ends: a container root may be one segment or
three, and nothing in the shape distinguishes a leftover root piece from a real folder.

⇒ The root is now **read from the *arr, not inferred**. Each instance's `/rootfolder`
list is fetched once per scan and handed to `resolve_movie` / `resolve_show`; the longest
reported root that prefixes an item's path is that item's root, and only what sits
strictly below it is evidence (`identity._below_arr_root`). With no root reported, or a
path under none of them, the corroborator **stands down** and returns nothing — never a
fixed strip, which is the unsound behaviour being removed.

**Narrowed a third time, same day: ranking by depth is itself the bug.** Keeping the
one-segment strip on the *Plex* side, on the argument that Plex's root is unknown and one
segment is a cheap guard, was wrong in a way that took a third adversarial pass to see.
The strip is not uniform in effect: a candidate whose path is fully consumed by the match
loses a segment of depth while a deeper rival loses none, so a **tie** (abstain, keep the
file) becomes a strict win for the wrong copy. That was a regression against the committed
baseline, which tied and fell through to size. More generally, "deepest shared suffix
wins" compares two copies whose Plex roots may differ in *depth* as though the shallower
one running out of path were evidence against it.

⇒ There is no ranking and no strip on either side. The *arr's below-root segments are the
item's path **relative to its library**, and any Plex listing of that same file must *end
with* those exact segments (`identity._ends_with`), whatever root that section is mounted
at — so Plex's root never has to be known. Exactly one holder binds; several or none fall
through. Three further guards, each from a reproduced wrong bind: the below-root depth
must match the *arr's own layout, so a stale or over-broad root cannot pass mount segments
off as folders; any candidate whose path Plex did not report stands the step down, since
dropping it would turn a tie into a strict win; and a winner whose size cannot be checked
stands it down too, because another candidate may match the byte count exactly. Where the
folder names one copy and the exact size names another, that is a positive contradiction
and the narrowing **abstains** rather than letting either overrule the other.

**A fourth pass found the remaining two, and both were arbitration, not parsing.** A
corroborator that stands aside is not neutral, and that is the lesson worth carrying: it
hands the decision to whatever runs next. The twins gate ran *before* the folder winner was
computed, so any twin pair made the contradiction veto unreachable and a folder answer from
outside the group was discarded in favour of the group. And a failed root-folder read was
treated as "no roots", which silently removed the veto and let a stale Plex size bind the
copy the folder would have disputed. Both now resolve the same way: the folder's answer is
computed first and compared against the group, and an unreadable root list refuses the whole
narrowing instead of falling through to size alone.

⇒ The rule this produced, after four passes: **when a check declines, ask what decides
instead.** Three of the four wrong binds in this sequence came from a "safe" fallback that
was only safe if you did not follow it one step further.

The cost is real, larger than the first narrowing's, and was accepted deliberately:

- Two instances mapped alike now **tie** below the root and abstain. A movie recovers
  through its exact byte size; a show has none and is kept. That is the fix.
- Radarr puts a movie at `<root>/<Title>/<file>` and Sonarr a series at `<root>/<Show>`,
  so below the root a show has **only the leaf both copies already matched on**. A show
  therefore never gets folder evidence at all. This was the sharpest finding of the third
  pass: the step's entire reach for shows had been paths *deeper* than Sonarr's layout,
  which only happens when the reported root is wrong — so the feature's reach was
  co-extensive with its failure mode, and it failed toward a bind. Shows now abstain,
  which is what the negative result at the top of this section said they must do.
- Where the two libraries are told apart by their **root paths alone**, that information
  is not below the root and cannot be recovered. Comparing the roots' own leaf names is
  not a fix: in the two-instance case both instances report the *same* root leaf, so such
  a rule would bind both to one copy.

Every one of those losses is an abstain, and an abstain keeps the file. A root-folder
fetch that fails does **not** degrade the snapshot (unlike rule 28's evidence sources):
it can only stand the corroborator down, and standing down can only ever cost a bind.

### Plex title-cases label tags

Write `leaving-soon`, read back `Leaving-Soon`. So any case-sensitive comparison of
label tags silently fails to find a label that is *right there*. The failure mode is
nasty in both directions: "I can't find the Leaving-Soon mark I wrote" becomes either
"add it again" (duplicate) or "this item isn't flagged, so it's safe to act on" (acting
on something the owner meant to protect).

The probe that *discovered* this was itself bitten by it — its cleanup step matched the
tags case-sensitively, found nothing, and left two labels on a real item.

⇒ Casefold every label comparison. `normalise_label()` in `clients/plex.py` is the only
comparison form, and removal re-reads the item to remove the tag under the exact
spelling Plex is using.

### `batchMultiEdits().addLabel()` PRESERVES existing labels (verified)

The most dangerous open question about the "Leaving Soon" collection: if `addLabel`
*replaced* labels, Reaper's mark would silently wipe every label the owner had put on
their media. Tested directly against a live server by adding two labels in succession
and reading back both — it preserves. Good news, but asserted in code rather than
trusted, because a silent regression here destroys user data.

### plexapi is `requests`, not `httpx` — so it bypasses the transport guard

Every other integration goes through an `httpx` client wrapped in `GuardedTransport`,
which refuses a mutating call unless deletion is armed and the intent was journalled.
plexapi uses `requests`, so it sails straight past that — label writes, collection
edits, and `emptyTrash` would all be unguarded. `emptyTrash` in particular would have
been the single destructive call in the codebase with no interlock.

⇒ A `GuardedSession(requests.Session)` twin enforces the identical rule, handed to
`PlexServer(session=...)`. When you adopt a third-party client, check what HTTP library
it uses before assuming your safety layer covers it.

### A Plex resource token is the account token (verified)

`resource.accessToken == account.authToken`, checked against a live account. The
per-server token plex.tv hands out for an *owned* server is not scoped to that server —
it is the full account credential. Any tool storing it (Tautulli, Overseerr, Maintainerr,
this one) is storing something equivalent to the Plex password. Document that honestly;
do not imply a boundary that is not there.

### Tautulli's `get_library_media_info` is a cache, and it lags Plex (verified)

A movie added to Plex a day earlier was absent from the media-info listing (the section
reported its old count) while Tautulli's own `get_metadata` for the same rating key
served the item fine. The listing is served from Tautulli's media-info table, refreshed
on Tautulli's own schedule — it is *not* a live view of the library. Anything that treats
it as the authoritative item list will silently miss fresh additions.

⇒ The identity index unions the plexapi sweep over the same sections: spine rows keep
Tautulli's `added_at`, and rating keys the spine did not list enter from the sweep with
Plex's own added-at. Watch *history* is unaffected — that comes from the history table,
which is written per play, not from this cache.

### pydantic drops undeclared keys silently — a wire schema can hide a whole feature

The stored explanation carried `match`, `keeps` and `base_score`; the frontend typed and
rendered all three; and none of them ever reached the browser, because the API's
`Explanation` model didn't declare them and pydantic's default `extra="ignore"` stripped
them without a sound. Optional frontend types (`match?: Match`) made the absence
invisible too — no error anywhere, the notice simply never rendered.

⇒ A wire schema must name every key the UI reads, and a feature whose visible surface is
conditional ("renders only when X") needs a test that X actually crosses the wire.

### Plex GUIDs: a new-agent list, a legacy single string, and sentinels

Matching an *arr item to its Plex item by external id (imdb/tmdb/tvdb) is far more robust
than by title+year — but reading the id off Plex has three traps, each of which silently
mis-behaves rather than erroring:

1. **Two GUID shapes.** New Plex agents expose a `guids` *list* (`imdb://tt…`, `tmdb://…`,
   `tvdb://…`); legacy agents expose a single `guid` *string*
   (`com.plexapp.agents.imdb://tt1234567?lang=en`). Code that reads only `guids` silently
   sees *nothing* on a legacy-agent library — the "Never Reap" collection parser had exactly
   this gap, unprotecting every item in such a library while looking fine.
2. **The legacy string is not the new scheme.** It does not start with `imdb://`, and it
   carries a `?lang=…` suffix (tvdb can also carry a `/season/episode` path tail). A naive
   `startswith("imdb://")` misses it; a naive parse yields `tt1234567?lang=en`, which never
   equals the arr's `tt1234567`. Match the agent-qualified prefix, strip the `?`/`/` tail.
3. **Sentinels masquerade as ids.** `tmdb://0`, `tvdb://0`, and `tt0000000`/`tt0` are "no id"
   markers; a non-external agent guid is `plex://…`, `com.plexapp.agents.none://…`,
   `local://…`. Treated as real ids they collide across *every* sentinel-bearing item — a mass
   mis-bind. Null them before any comparison; "no id" must never match "no id".

All three now live in one parser (`engine/identity.parse_guids`), used by both the scan
matcher and the collection provider, so there is a single place to be right.

### A split library makes external ids ambiguous; the file name picks the copy

On a real library with separate HD and 4K sections (plus curated sections that re-list
titles), one tmdb/tvdb id names two or more Plex items for **~3% of items**. The resolver
abstained on every one — correctly fail-closed, but *permanently*: those items' watch
history stayed invisible, so they could never be judged at all.

The duplicate hits are the same *content* in several *copies*, and the *arr entry's own
file name identifies which copy it manages. So an ambiguous id may be narrowed by file
name — under rules that keep it corroboration rather than guessing:

- Compare **only among that id's candidates**. Consulting the wider library (a global
  basename or title match) answers a different question and can land outside the id set.
- Compare against **all** of a candidate's file locations, and require every candidate's
  locations to be known. A merged multi-edition Plex item indexed by only its first file
  makes a re-list of its *second* file look "unique" — and a candidate whose files could
  not be seen might be the very file in hand. "Could not look" is not "looked and it was
  different".
- Bind on **exactly one** match; none keeps abstaining, and several hands over to the
  size step below.
- Narrowing binds the row that *contains the arr's file*, which stays correct even when
  the shared id is an agent mis-tag: that row's history is the plays of that very file.

Accepted residual, weighed and documented rather than guarded: an *arr rename that lands
exactly on the sibling copy's file name mis-picks until Plex rescans — transient,
same-content only, and gated by the grace window plus supervised execution.

Verified on a live scan: **about a quarter of the stuck items bound by name alone** —
each one a split-section copy whose file name carries a quality marker, each to its own
rating key (the two instances of one title got *different* keys, and their scores
immediately diverged, one dormant and one active — per-copy history working). Every item
that stayed ambiguous gave the same reason: its file name matches two Plex items. None
abstained for a missing file name, so real libraries do carry full location data through
the sweep.

### The same file listed twice: exact byte size proves it, and the twins merge

The name-matches-two case turned out to be, on a real library, **every** remaining
ambiguity — and inspecting the listings showed the shape precisely: a curated section
(built years after the original) re-lists the *very same file* under its own rating key at
a **different full path** with the same leaf and the same parent-folder name. So "same
full path" can never recognise it; what does is the **exact byte size**: the two listings'
part sizes were byte-equal, and equal to Radarr's own `movieFile.size`. A curated re-list
farm points at the same bytes (links or a bit-exact copy), and no two different encodes
share an exact byte count in practice.

So when the file name matches several of an id's candidates, the *arr's exact size is the
one corroborator left, and it resolves the tie in one of three ways — every unknown
abstains ("could not look" is neither "different" nor "the same"):

- **Size singles out one listing** → bind it. A stale re-list of a since-upgraded file
  falls here: per-file judgment binds the *arr's actual file.
- **Several listings at exactly the arr's size** → byte-identical twins of the file in
  hand → bind them **as one group**. Reading only one listing's history would under-count
  the file's own watching (plays split across the listings) — the direction that
  condemns. So the canonical key is the earliest listing (the original row: its poster,
  its honest dormancy floor), the match block records every key, the scan folds
  last-played and distinct-watchers as an exact union (a person who played the file
  through both listings counts once), and the executor's live interlocks re-read the
  stored group so a stream or late play through *either* listing spares the file. The
  merge can only add evidence of watching; the delete still routes by the *arr's file.
- **No listing at the arr's size** → abstain; the file in hand matches nothing seen.

Shows never merge: a show binds by its folder, and a folder has no one size — two
same-name folder listings under one id keep abstaining. Residual accepted: a
byte-identical *different* rip sharing name, size, and id would merge — practically
impossible, and merging errs toward keeping.

Verified on a live scan: **every remaining ambiguity merged** — each one a two-listing
group whose sizes byte-matched the *arr's record — and the library ended *fully bound*
(no item left without a rating key). A rescued item's coverage went from a tenth of the
evidence to all of it, its dormancy read a play made through the second listing (invisible
before the fold), and every rescued item landed on protect: new evidence arriving means
*more* reasons to keep were suddenly visible, not a sudden condemnation. The why-panel's
"kept to be safe" notice disappeared for all of them, replaced by real signal rows.

### Don't hold a DB transaction across a human's sign-in

The first version of the Plex link opened an `AsyncSession`, then polled plex.tv inside
it for up to five minutes waiting for the owner to sign in. SQLite gives a writer the
database — so that open session held the write lock the entire time, blocking every
other writer (this was caught when a background maintenance write deadlocked against it).

⇒ Hold the lock for milliseconds, not minutes. Read what you need, close the session,
do the slow network-and-human part with nothing open, then reopen briefly to write.

### plex.tv rate-limits PIN polling

Polling `/api/v2/pins/{id}` once a second earns a `429` before the owner has finished
typing their password — and a naive client lets that 429 abort the whole sign-in. Poll
at ~2s, and treat a 429 as back-pressure (wait longer, keep going), never as a failure.

### Sonarr and Radarr disagree, and each ignores the other silently

| | Delete param | Exclusion route |
|---|---|---|
| **Radarr** | `addImportExclusion` | `/api/v3/exclusions` |
| **Sonarr** | `addImportListExclusion` | `/api/v3/importlistexclusion` |

Each **silently ignores the other's parameter and returns 200**. A 200 means nothing;
re-read the exclusion list and assert the id landed.

### `episodeCount` is not the number of episodes in a season

It is Sonarr's *download intent*. Three observed shapes, each common:

- `episodeCount > episodeFileCount` — episodes Sonarr wants but does not have.
- A monitored season that has not aired yet reports `episodeCount=0`.
- **An unmonitored season reports `episodeCount=0` even when complete**, with a full
  `totalEpisodeCount` and every episode long since aired. On a mature library this is
  the *majority* case, because finished shows get unmonitored.

⇒ Only `episodeFileCount` and `sizeOnDisk` describe reality. `totalEpisodeCount` is the
honest length of a season and is what ranking should use.

### Plex's `audience_rating` is not necessarily Rotten Tomatoes

The documentation says it is. On a probed server, every sampled movie had `rating_image`
empty and `audience_rating_image = imdb://image.rating` — it was **IMDb**. Both shapes
exist in the wild; the field means whatever the library's metadata agent decided.

⇒ **Read provenance from the data. Never infer it from the field name.** An IMDb floor
of 7.5 compared against a Tomatometer of 96 protects nothing, silently, forever.

### Rating scale is a property of the provider, not the source (verified)

Plex serves **every** rating slot on a 0-10 scale whatever the source: a 96%
Tomatometer arrives as `9.6`, never `96`. Swept live across every movie and show
section: thousands of rating values under `imdb://`, `themoviedb://` and
`rottentomatoes://` images, **not one above 10** — including Rotten Tomatoes values
that Plex's own UI renders as percentages. Radarr hands the very same Rotten
Tomatoes / Metacritic scores through as raw percentages (`96`), while its
IMDb/TMDb/Trakt values are 0-10 averages.

⇒ **Normalise per provider, never per source.** "Divide any Rotten Tomatoes value by
ten" is exactly right for Radarr and silently turned Plex's 84% into 0.84 — which the
review view would have displayed as 8%.

### A rating without a vote count is noise

Every library holds a handful of titles rating ≥ 7.5 on **fewer than 1,000 votes** — an
8.3 drawn from a few hundred people. A bare rating floor preserves all of them, forever.

### Tautulli's key is full admin, and its destructive commands are GETs

`GET /api/v2?cmd=delete_library` and `cmd=restart` are ordinary GETs. **HTTP-method
filtering cannot protect you.** An allow-list of read commands is required.

### Tautulli type inconsistency

`added_at` is a **string**; `last_played` is an **int or None**. Never-played is
`None`, not `0` — and coercing it to epoch 0 reads as five decades of dormancy, i.e.
**maximum condemnation pressure for the item you know least about.**

### Seerr's published OpenAPI spec is stale

It omits `ratingKey`, `externalServiceId`, `mediaAddedAt` and `status4k` — all of which
the API returns and all of which are load-bearing. **Do not codegen from it.**

Also: `GET /request` defaults to `take=10`. Forget to pass it and you silently analyse
the ten most recent requests and conclude the rest do not exist.

### The requester rule must be per-*media*, not per-*request*

Real request data contains the same title requested by several different people. Judged
per request, a film Alice requested and watched is still condemned on Bob's row — and
they share one file, so it is deleted out from under her.

### The IMDb Top 250 mirror carries no rank

`https://api.radarr.video/v1/list/imdb/top250` returns 250 items with `TmdbId` and
`ImdbId`, no auth — but **no rank, position or order field**, and the entries come back
in roughly *chronological* order (the oldest film in the chart is first).

Taking the array index as a chart position would report the earliest film as "#1 on the
IMDb Top 250" — false, and the why-panel would be confidently lying to the user.

⇒ Membership is binary. Never infer rank from array position.

### Plex `/api/v2/resources` (v1 XML) silently omits some owned servers

Fatal for a server picker. Use v2 JSON, which *requires* `X-Plex-Client-Identifier`.

### "Sign in with Plex" authenticates but does not authorize

plex.tv issues a valid token to **any Plex account on the internet**. Ownership must be
checked explicitly: query `/api/v2/resources` **with that user's own token** and require
`owned == true` for your `machineIdentifier`. (`owned` is relative to the requesting
token — which is what makes it a real check, and why it must never be your stored admin
token.)

For calibration: Maintainerr ships with **no auth at all**; Seerr trusts whoever logs in
first.

---

## Storage and time

### SQLite does not store timezones

`DateTime(timezone=True)` is a **silent no-op**. Aware datetimes go in; naive ones come
out. In a tool whose every decision rests on "when was this last watched", a naive/aware
comparison is either a `TypeError` or — worse — quietly wrong by your UTC offset.

⇒ Store integer epoch. The instant *is* the value. (Bonus: Tautulli and Plex already
speak epoch.)

### Alembic + SQLite needs two things set before the first migration

A `MetaData(naming_convention=...)` and `render_as_batch=True`. SQLite cannot drop an
unnamed constraint; Alembic rebuilds the table but can only drop a constraint it can
*name*. Add these late and the only fix is rewriting the entire migration history.

### Alembic will silently produce a migration that creates nothing

Autogenerate rendered a custom type as `reaper.db.types.TZDateTime()` **without emitting
the import**. `alembic upgrade head` reported success and created **zero tables**. Use a
`render_item` hook to emit a stdlib type.

---

## Scan wall clock

### The scan was slow because it *waited in series*, not because it computed

Every optimization that mattered removed sequential waiting; none of them touched the
scoring. Three patterns, each invisible in a small test library and each scaling with
the real one:

1. **Two HTTP round trips per prunable show, one show at a time.** The TV gather
   resolved each show's seasons (Tautulli) and episode list (Sonarr) sequentially. At
   hundreds of prunable shows and typical LAN latencies, this one loop dominated the
   entire scan. One call per show is required by the APIs; one show *at a time* was
   not. A small per-service concurrency bound (4) collapses the stretch by roughly
   the bound while keeping a self-hosted service at a handful of parallel reads.
2. **A database query per item inside the judge loop** — and worse, an
   ensure-schema pass (7 `CREATE ... IF NOT EXISTS` statements) *before every query*.
   Ensure-schema-before-read is the right habit at module boundaries (the cache is
   deletable), but calling it per item turns a judge loop into thousands of DDL
   transactions. Load once, look up in memory — and prove parity with the SQL it
   replaced with a test, or the two paths will drift.
3. **Re-downloading a whole library nobody asked for twice.** The keep-tag provider
   fetched the full movie/series list once *per configured tag*, and the scan gather
   fetched it again. The tag ids are one cheap call; the library is the expensive
   one. Read each once per rule.

⇒ The safety architecture and the concurrency are orthogonal: gather-freeze-judge
only requires that everything is gathered *before* anything is scored, not that
sources are read one after another. Overlapping the gather changed when evidence
arrives, never what is frozen. The one place concurrency was refused on purpose: the
two plexapi GUID sweeps share one `requests.Session`, which is not promised
thread-safe, so they serialize behind a lock and overlap with everything else
instead.

### Making syncs concurrent exposed a fail-open that was always there

The keep-tag whitelist synced one list PER SERVICE, not per instance: with two
same-service *arr instances, each sync atomically replaced the other's membership,
and whichever ran last silently erased the other instance's keep-tagged titles.
Sequentially the winner was at least deterministic, which is why it survived unseen;
running the syncs concurrently made the winner random, and the review of that change
is what finally surfaced the bug. Two lessons: a stored list's key must carry every
dimension that makes it distinct (service AND instance), and "we only made it
concurrent" is precisely when to re-audit which shared keys the now-racing writers
collide on.

### plexapi turns one sweep into one request per item, silently

The GUID sweep read listing rows via ``section.all()`` and plain attribute access,
with a comment promising "one sweep per section, never a per-item metadata call."
Measured live, the sweep cost ~35ms *per item* -- because plexapi reloads a partial
object the first time any accessed attribute is ``None``, and on a listing row some
attribute always is (an unrated title has no rating image; many rows lack a year).
One full metadata request per title, thousands per scan, invisible in any unit test
because fakes do not lazily reload. An attribute-allow-list guard fake could not
catch it either: the reload fires on *known* attributes whose value happens to be
None, not on unknown ones.

⇒ The sweep now parses the listing container XML directly (missing attribute =
honestly ``None``, never a hidden request), with batched ``/library/metadata/{ids}``
reads for the one thing listings never carry (show folder paths). The whole sweep
went from minutes to seconds, and the regression test counts requests -- one page,
one request -- rather than trusting attribute discipline.

⇒ The general lesson: after making a pipeline concurrent, measure the OLD code and
the NEW code on the same data before claiming victory. The first measurement here
showed near-zero total improvement -- both pipelines were pinned by this one shared
bottleneck, and the honest before/after is what exposed it.

### bare asyncio.gather leaves siblings running after a failure

`asyncio.gather` re-raises the first failure immediately but does not cancel the
other awaitables -- they keep running detached, keep issuing reads against the
operator's services for a scan that is already dead, and any later failure logs
"exception was never retrieved" at teardown. Every fan-out in the scan now goes
through one shared reap-on-failure helper (`reaper.aio`), at every nesting level, so
there is a single cancellation discipline instead of N hand-rolled ones. Relatedly:
removing all the per-item awaits from the judge loop made it pure computation, which
*starves the event loop* -- the progress endpoint it feeds would freeze for the whole
scoring phase without an explicit yield at the emit points.

---

## The policy permutation sweep (what held, what broke)

A full offline replay of the newest snapshot (several thousand items, reconstructed into `Facts`
from the local mirrors and stored explanations) against permutations of every
user-tunable policy option. Method and harness are in PLAN ("The policy permutation
lab"); these are the learnings.

### The stored explanation plus the mirrors is a complete record

Every item's verdict, score, and coverage could be reproduced bit-for-bit offline from
`explanation_json` + `watch_event` + the IMDb mirror. That is worth knowing on its own:
the why-panel's record really does contain everything that decided an item's fate. Two
extraction subtleties bit before fidelity reached 100%, both notes for anyone parsing
stored explanations:

- **A hand spare is a synthetic gate row.** The scan injects the spare as an extra
  `whitelisted` PROTECT result, so the explanation carries *two* whitelist rows (the
  synthetic fired one and the real gate's "checked"). Naive per-gate bucketing lets the
  real row overwrite the spare.
- **Gate details embed the threshold phrase.** "untouched for just 2 months, 10 days,
  less than the 1 year Reaper waits" contains two durations; inverting the humanized
  duration must cut at the comparator first or it reads 435 days instead of 70.

### Negative results: the invariants all held

Across threshold grids, gate subsets, window changes with watchers recomputed per
window, the whole field×operator custom-rule matrix, graded keeps in both directions,
Unknown-degradation of every fact singly and in pairs under randomized policies, ~30k
season-toggle combinations over real show shapes, and ~250 randomized valid policies:
no crash, no bounds violation, no monotonicity break, and no path where missing data
moved any item *toward* the condemned set. The unsigned-signal arithmetic ("an outage
can only lower a score") survived adversarial permutation, including the blackout case
(every fact Unknown at once scores exactly 0 under any legal policy).

### What broke (both fixed)

- **Name shadowing:** validation accepted a custom rule named `unwatched`; the stored
  explanation then held two signal rows with one id, the why-panel keyed rows on that id,
  and the "Your rule" tag logic mis-attributed the owner's rule as built-in. Rejected at
  the save boundary now. The general lesson repeats rule 19 from the other side: any
  *stored* identifier a UI keys on needs a uniqueness guarantee at the write boundary,
  not just among siblings of one kind.
- **Conflict detector vs specials:** `_detect_conflicts` compared prunable seasons
  against every protected season including Season 0, while its docstring claimed specials
  were excluded. On real data every show carries near-unwatched specials, so with
  specials kept, one watcher on any prunable season produced a spurious "Needs a look"
  refusal. The docstring was right and the code was wrong — rule 24's failure mode, found
  by testing the docstring's claim as an invariant.

### The ingest is faithful to the sources (validated outside Reaper)

The permutation sweep validated the engine against Reaper's own mirrors; a second pass
validated the mirrors against the **sources themselves** (`scripts/validate_ingest.py`,
read-only), closing the garbage-in half of the loop. Everything the engine decides on
was checked at least one system further out:

- **Watch history vs live Tautulli.** Sampled rating keys reproduce row counts,
  last-played, and distinct-watcher counts exactly under the sync's own skip rules;
  never-played items have no source history either; and the mid-binge guard's exact
  inputs (per-row episode index and completion status, and the per-user
  max-completed-episode aggregate) matched on every sampled season, 653 rows compared.
- **Dormancy derivation vs source `added_at`.** For never-played items, the stored
  why-panel phrase equals `(scan time - max(added_at, horizon))` recomputed from
  Tautulli's own metadata, within the two-unit humanize granularity.
- **IMDb vs the raw `title.ratings.tsv.gz`.** The mirror table is a byte-exact full
  copy: identical row counts, 500/500 sampled rows exact, all candidate ids exact.
  Candidate ids absent from the table were absent from the raw file too (correctly
  `Absent`, never a lookup failure).
- **Candidates vs live Radarr/Sonarr.** Every movie candidate joined back to its
  source instance (100% both ways); sizes, quality names, years, and ids matched with
  zero drift; content-season sets, per-season sizes, and independently recomputed
  season ranks matched on every sampled show.

Anomalies found, every one explained and none an ingest bug:

- **The mirror lags the live source by exactly the plays since its last sync.** The
  scan re-syncs before judging, so snapshots never see this lag.
- **Tautulli's `get_history` prepends live sessions to the list but excludes them from
  `recordsTotal`.** The paginated set is really `recordsFiltered` long (persisted rows
  plus current live sessions), so "fetch the last page at `recordsTotal - N`" lands
  short of the true end by the number of streams playing at that moment -- which
  *looks* like the oldest rows are missing from the source. A first pass here
  concluded Tautulli had deleted its three oldest rows and the mirror horizon led the
  source by 49 minutes; both claims were wrong. Paging past `recordsTotal` returned
  exactly those three rows, timestamps equal to the mirror's to the second, and each
  also appears under its item's own `rating_key` history. **The horizon matches the
  source exactly.** Corollary for the sync itself: page boundaries shift when a stream
  starts or ends mid-walk, so a row can slip between pages of one sweep; the 2-day
  incremental overlap and the nightly full re-walk are what absorb that, and the one
  stray absent old row observed (one row in a six-figure history) is consistent with this and heals
  on the next sweep.
- **Upstream metadata drifts under you.** Two titles changed genre lists at the source
  within hours of the scan. Frozen-at-scan facts are the correct behaviour, but any
  validation that diffs a snapshot against a live source must budget for source-side
  mutation, or it will cry wolf.
- **A validation tolerance is part of the claim.** The humanized dormancy phrase keeps
  two units, so a "years, months" phrase truncates up to 29 days; a 16-day tolerance
  mislabels perfectly-derived values as errors. The committed validator uses the bound
  the phrasing actually guarantees. The same discipline caught the pagination artifact
  above: verify the anomaly's mechanism before writing it down, because two of the
  four "anomalies" this pass found were artifacts of the validation itself.

### Shapes worth knowing (ratios, latest snapshot)

- **Unknown facts are real but thin: 0.6% of season rows, zero movie rows** carry an
  Unknown dormancy or popularity (unmatched/ambiguous in Plex). Real data *under-samples*
  the Unknown lane — which is exactly why the harness degrades facts synthetically
  instead of waiting for outages to happen in a fixture.
- **Every abstain in the snapshot is a plain below-threshold score.** Zero abstains came
  from a blocked protection and zero from the coverage floor: the items those would
  catch are protected first (Unknown dormancy PROTECTs via the dormancy gate before
  coverage is ever consulted). The coverage floor is a deeper backstop than it looks —
  in a healthy scan it decides nothing, and it only bites when a source fails while the
  dormancy gate is disabled.
- **Single-season shows dominate the TV library (~40% of shows)**; permutation tests
  over "keep last N" need the 1-season shape or they miss the `keep_last >= total`
  branch entirely.
- **Shrinking the popularity window from 365 to 7 days condemned nothing new** (with
  watchers honestly recomputed for the narrow window): the dormancy gate and rating
  floor pick up the slack. Defence-in-depth is real — single-gate misconfiguration is
  survivable in this library's shape.

## Prior art

- **Maintainerr** — no auth at all. Its `operator` field is overloaded (section-join vs
  rule-join), and rule evaluation is order-dependent set algebra with no precedence:
  `A OR B AND C` always means `(A OR B) AND C`.
- **Janitorr #234, "deleted half of library"** — a user wrote
  `movie-expiration: {100: 10d}` believing it meant *"only when 100% full"*. It means
  *"while free disk is below 100% — i.e. always — delete everything older than 10 days"*.
  **One config line. No rule builder involved.**
- **Deleterr #291** — "dry-run mutates state".

The common thread: **protections live inside the same boolean expression as the
condemnations**, so an unknown value, an API failure or a mis-set operator silently
*disarms* a protection. Hence Reaper's two-lane design: gates have no `CONDEMN`
constructor and cannot delete a file no matter how they are misconfigured.
