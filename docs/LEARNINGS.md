# Learnings

> Findings from building Reaper against a real, large, actively-used Plex library.
> **Everything here was measured, and most of it contradicted a reasonable-sounding
> assumption.**
>
> Negative results are recorded too. "We tried X and it made things worse" is often
> more valuable than the fix, because it stops the next person re-trying X.
>
> Figures are given as ratios and orders of magnitude on purpose: the point is the
> *shape* of the finding, which generalizes, not one server's numbers, which do not.

---

## The headline findings
> Moved here from the living plan on 2026-07-26. The plan tracked *state*; these are
> *findings*, which do not go stale, so they belong with the measurements that produced
> them.


Validated against a large, active, multi-instance production setup. The findings are
recorded as shapes and ratios — see `docs/LEARNINGS.md` and `docs/SIGNALS.md` for the
detail.

- **An active library is not mostly dead weight.** The share of films *never* played at
  all was under one percent. A film watched in the past year is more likely than not to
  be watched again within the next one. This is the finding that most contradicts the
  intuition a pruning tool gets built on.
- **There is no cliff, and nothing is ever free to delete.** A film dormant for five
  years still has a double-digit chance of being watched next year. There is only
  *cheaper* and *dearer*.
- **The best policy measured still carries ~12% regret** — roughly one deletion in
  eight is a film someone comes back for. Not a tuning failure: it is what an active
  library looks like, and it is why the grace period and the human approval gate are
  not decoration.
- **Dormancy does essentially all the work.** `FEW_WATCHERS` and `LOW_RATING` add no
  measurable skill on top of it.
- **Removing `SIZE` from the score was right.** Size measures *reward*, not *risk*;
  big files are big *because* they are popular.
- Rating coverage for movies via Radarr is effectively free and effectively complete
  (IMDb ~99%, Rotten Tomatoes ~88%) — no extra key, no extra request.
- Real request data contains **the same title requested by several people**, which is
  why the requester rule is per-*media*, not per-*request*.

The population trap that produced two confidently wrong conclusions is treated in full in
`docs/SIGNALS.md`, and again below under what an active library looks like.


## Assumptions that were wrong
> Moved here from the living plan on 2026-07-26. The plan tracked *state*; these are
> *findings*, which do not go stale, so they belong with the measurements that produced
> them.


Each of these was in the original plan and was disproved by running against a real
library. They are the reason the plan is a living document.

### 1. `episodeCount` is the number of episodes in a season — **wrong**

It is Sonarr's *download intent*. An unmonitored-but-complete season reports
`episodeCount = 0` while holding a full `totalEpisodeCount` — and on a mature library
that is the *majority* of seasons, because finished shows get unmonitored.

⇒ Only `episodeFileCount` and `sizeOnDisk` describe reality. `totalEpisodeCount`
is the honest season length. `episodeCount` is display-only.

### 2. Plex's `audience_rating` is Rotten Tomatoes — **not necessarily**

On a probed server, every movie sampled had `rating_image` empty and
`audience_rating_image = imdb://image.rating`. It was **IMDb**. Both shapes exist in
the wild; the field means whatever the metadata agent decided.

⇒ Ratings carry *provenance*, read from the data, never inferred. An IMDb floor of
7.5 compared against a Tomatometer of 96 would protect nothing, silently, forever.

### 3. Signed weights with a neutral baseline — **actively dangerous**

Under a signed score (start at 50, subtract for "well rated"), an `Unknown` removes
a *negative* contribution and the score **rises**. An outage makes media *more*
condemned.

⇒ Signals are unsigned, `[0, weight]`. Unknown contributes 0, the floor. Property-
tested: making any input `Unknown` never increases a score; a total outage scores 0.

### 4. Server popularity counts distinct watchers — **needed a window**

Counting all-time watchers protected the overwhelming majority of the library and made
every threshold condemn almost nothing. On a long-lived server nearly everything has
been watched by *someone*, eventually; only a fraction still have watchers this year.

⇒ Popularity is windowed (365d default). There is deliberately no way to spell
"all time".

### 5. A play after deletion is a regret — **not if it's within grace**

An item played 2 days after condemnation is still in quarantine; the live pre-delete
check spares it. Counting rescues as failures slanders the policy.

⇒ Regrets and rescues are split at the grace boundary.

### 6. SQLite stores timezones — **it does not**

`DateTime(timezone=True)` is a silent no-op. Aware in, naive out.

⇒ Timestamps are integer epoch. The instant *is* the value.

### 7. The rewatch curve is a constant — **it is a property of an audience**

Shipping one library's rewatch rates as a hardcoded prior makes every lift number on
every *other* library meaningless.

⇒ `engine/calibration.py` derives the prior from the operator's own history, and the
backtest labels which prior it used in every summary it prints.

### 8. The simulator can re-decide a snapshot under any policy — **only the thresholds**

The zero-API-call simulator re-compares **stored** scores against new numbers. That is
exact for `condemn_at` and `coverage_floor_bp` and simply *wrong* for anything else:
change a signal weight or a gate, and the stored scores were produced by the old ones.
A policy editor that let you drag a weight and then showed a confident count would be
the most dangerous screen in the product — the stale number looks exactly as
authoritative as the true one.

⇒ `PolicyBody.scoring_hash()` covers the signals and gates but not the thresholds. The
snapshot records it, and the simulator **refuses to report any numbers** when it
differs, saying so and telling you to re-scan.

### 9. Rounding the score after deciding the verdict — **two answers to one question**

The scan compared the *float* score (69.7) against the threshold and abstained, but
persisted `round(69.7)` = 70. The simulator, which only ever sees the stored 70,
condemned it. The review queue and the policy editor disagreed about a real film, at
the same threshold, on the same snapshot — and the queue displayed "score 70, your
threshold is 70, **not judged**", which reads as a bug even to a careful user.

⇒ Round **first**, then decide, and store exactly what decided. There is one number and
everything compares against it (`tests/test_verdict_agreement.py` sweeps the grid).

### 10. Posters and blurbs must come from Tautulli — **not needed**

The instinct (and the original request) was to fetch poster art and descriptions from
Tautulli. But `get_library_media_info` is show/movie-level and has no overview, so that
would mean a per-item `get_metadata` call — thousands of extra requests on a large
library. The *arr payloads the scan **already** pulls carry all of it: `overview`, `year`,
and `images[].remoteUrl` (a TMDb CDN URL that resolves straight from the browser).

⇒ Display fields are captured at scan time from data already in hand and stored on the
candidate. Zero extra API calls, and they survive on the frozen snapshot like everything
else. A Tautulli image proxy stays an option for items the *arr has no poster for, not a
requirement.

### 11. Seerr's `serviceId` is Reaper's instance id — **different numbering schemes**

The tempting join for "who requested this" is Seerr's `serviceId` → Reaper's `Instance.id`.
They do not line up: Seerr indexes *its own* configured services, Reaper indexes its own
rows. The external ids (tmdb for movies, tvdb+season for TV) are present on both sides and
do line up.

⇒ The requested-by map keys on external ids. And it is treated as **display-only** — never
a gate — so a rare cross-edition id collision can at worst show the wrong name on a card,
never condemn or spare the wrong file. (The requester *rule*, which does affect the score,
still joins per-media through Plex, unchanged.)

### 12. The emergency stop worked — **it was never wired**

`RuntimeSafety.emergency_stop` existed, with a correct `destructive_allowed = env_enabled
AND NOT emergency_stop`, and a UI story around it. But every construction site built
`RuntimeSafety(env_enabled=...)` and nothing ever read the DB, so the switch controlled
nothing. A safety control that silently does nothing is worse than none.

⇒ One helper (`app_settings.runtime_safety`) now assembles the effective permission, used
everywhere a client or a health check is built.

**Superseded — the two-switch model is gone, and the docs did not notice for a long time.**
The host-ceiling / emergency-stop pair was collapsed into a single stored toggle:
`destructive_allowed` is now `RuntimeSafety.destructive_enabled` alone, sourced from the DB,
with `REAPER_DESTRUCTIVE_ACTIONS_ENABLED` seeding only the first run (`app_settings.
destructive_enabled` falls back to it solely when nothing is stored). There is no
`emergency_stop` field. So the old guarantee — *nothing reachable from a browser can arm
Reaper* — **no longer holds**: the password-gated `PUT /api/settings/safety` arms it, and
`tests/test_settings_api.py::TestSafety` pins exactly that, with the env var false. The
admin password is now the only thing between a browser and an armed Reaper.

⇒ The lesson is the drift, not the design: `README.md` and `.env.example` went on promising
the ceiling ("Turning deletion on requires host access") long after it was removed, and two
docstrings still described switches that no longer existed. A safety claim nobody re-checked
is the same failure as §12 itself, one level up — see engineering rule 7. Corrected to
describe the stored toggle. **If the host ceiling is wanted back, it is a code change, not a
doc change.**

### 13. The simulator answers or refuses — **it refused forever, and nobody could tell**

Found on a live install, not in a test (measured 2026-07-26). Every policy edit showed
"Needs a fresh scan", and **scanning did not clear it**. Both fast tiers of
`api.routes.simulate` were unreachable, so the panel an operator tunes a deletion threshold
with had been dead since the last `SCHEMA_VERSION` bump.

`schema_version` is the *storage shape* of a policy body, and the wire schema does not carry
it: `_policy_out` builds `PolicyIn` field by field. So a body that round-tripped through the
API came back stamped with the current code default, while the stored row kept the older
number. That field was folded into **both** simulator hashes, and the two sides are computed
from different bodies — the scan hashes the *stored* body, the route hashes the
*round-tripped* one. The mismatch was therefore permanent and self-renewing: each new scan
recorded the stored value again.

The tell, and the cheapest possible reproduction: POST the server's own
`GET /api/policy` body straight back to `/api/policy/simulate`. It answered `exact: false`
about a policy it had just handed out, while the snapshot's stored hashes matched the active
policies' computed hashes exactly — which rules out the snapshot and points at the round trip.

⇒ `schema_version` moved to `PolicyBody._NON_BEHAVIORAL_FIELDS`, excluded from both simulator
hashes and still covered by `policy_hash`, so an approval stays bound to the exact body it
was planned under. `scorer_version` stayed in `scoring_hash`, where it belongs — a new scorer
really does invalidate stored scores — and joined the replayable set, so a bump now routes to
the exact replay instead of a refusal.

⇒ **The general lesson is about the allow-list.** Defaulting an unclassified field to "needs
a fresh scan" is right for evidence and wrong for bookkeeping, and the failure is invisible:
a hash mismatch cannot say *why* it mismatched, so a permanently dead feature looks exactly
like a stale one. Anything that decides whether a feature answers **at all** must cover only
fields that change the answer, and a lossy round trip through a wire schema must be tested as
a round trip. `tests/test_simulate_hardening.py::TestTheWireRoundTripPreservesBothHashes`
asserts on the hashes rather than a field list, so it fails for any future dropped field.

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
than random" claim that originally motivated it was an artifact of the bad baseline.

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

### `Absent` keeps its weight in coverage: the one place the arithmetic got *less* conservative

Every other change in this codebase's history moved toward keeping the file. This one
moves the other way, in a narrow, bounded case, and it is worth writing down so nobody
"fixes" it back.

**What changed.** A signal whose input is genuinely `Absent` now returns
`SignalState.NOT_APPLICABLE` with `evaluated=True` and its weight retained: the shared
`Absent` tail in `evaluate_signal`, the `SEASON_RANK` special case, and
`evaluate_custom`'s graded arm all end there. Previously those fell through to the
`UNREADABLE` branch (`evaluated=False`), which is the only state that drops weight out
of the coverage numerator. `Unknown` still routes to `UNREADABLE` and still lowers
coverage: `Absent` and `Unknown` were being collapsed at exactly the point where the
engine's whole design says they must not be (rule 93).

**Why it is correct.** Coverage answers one question: how much of the condemnation
evidence did we actually manage to look at? For an unrated show, a special that is not
one of the numbered seasons, or a graded custom rule on a field a media type never
carries, we *did* look. There is nothing there. Reporting it as unreadable printed a
why-panel line asserting a check that never failed ("could not read the IMDb rating"
about a title that simply has none) and dragged every one of those items toward the
abstain floor for a value they were never going to have.

**Direction, and its exact bound.** Pressure stays 0.0 and the denominator is unchanged,
so *the score itself cannot move*. Only coverage rises, and coverage is consulted in
exactly one place (`verdict.decide_verdict`, `coverage_bp < coverage_floor_bp` to
abstain). So the sole reachable effect is: an item that previously abstained for thin
coverage can now be decided on its score. That is a condemn-lane loosening.

**When an operator would notice.** Not at shipped defaults. `coverage_floor_bp` ships at
5000, and `condemn_at` is itself a coverage floor (a score cannot exceed
`MAX_SCORE * coverage`), so the explicit floor decides nothing in a healthy scan, which
the live-data pass confirmed: zero abstains in the snapshot came from it. It becomes
visible only when the floor is raised above the share of weight a class of items
genuinely lacks. With the shipped weights that share is 10 (`LOW_RATING`) for an unrated
movie, so a floor above 9000 is where an unrated title flips from abstain to condemnable;
for TV, a special lacking both a rating and a season rank sat at 75, so a floor above
7500 reaches it. Below those, nothing changes.

**The tradeoff, stated plainly.** The old behavior bought a little extra caution with a
statement to the operator that was false. We took the honest arithmetic and kept the
caution where it belongs: on `Unknown`, which is the state that actually means we could
not see.

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
in `reaper.db`, didn't recognize it, and helpfully proposed to `create_table` it. That
migration then failed on a fresh database.

⇒ Split them. `reaper.db` is small, precious, migrated. `cache.db` is **orders of
magnitude larger**, reconstructible, and **never migrated**. The separation also states
the invariant out loud: *nothing in the cache is a source of truth.* There is now a test
asserting no migration ever creates a cache table.

### Editing the baseline's `CREATE TABLE` in place cannot heal a database that already ran it

Before the baseline was frozen, one commit corrected two columns by editing the baseline's
`CREATE TABLE` directly: `candidate.size_bytes` went NOT NULL → nullable, and
`reap_run.held_back_unknown_size` lost its `DEFAULT 0`, both to match the models. A
`CREATE TABLE` only ever runs against an **empty** database, so a fresh install picks up the
fix while any database built from the earlier baseline keeps the old shape — and there is no
ALTER anywhere to reconcile it. `alembic upgrade head` is a no-op on it (already at head), so
the drift is invisible until something reads it.

The size half was **correctness, not cosmetics**: the scan now writes `size_bytes = NULL`
for an item whose size can't be determined (the held-back-unknown-size path), which a
NOT NULL column rejects with an `IntegrityError` mid-scan. `alembic check` passed the whole
time — it compares the *models* against a *fresh* build, and both were correct; it never sees
the shape an old database is actually carrying.

⇒ **A schema correction is an additive ALTER migration, never a baseline edit** — the same
rule that freezes the baseline, learned the hard way. The heal (`708192a3b4c5`) reflects each
column first and only rewrites it when it's still the old shape, so a corrected database isn't
copied for nothing and an old one self-heals on the next `upgrade head`. And: **a green
`alembic check` proves models-vs-fresh agreement, not that any existing database matches** —
to catch a divergent old shape you have to migrate a database that carries it, which is what
the new test does.

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

⇒ Hash the policy's *scoring behavior* separately from its thresholds
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

### TMDb ids are namespaced by media type, so a bare-id join crosses movies and shows (found on a live instance 2026-07-19)

A second instance showed **TV shows reported "on the IMDb Top 250"** — a list Reaper
syncs as movies only. The Top 250 was never wrong and nothing was written upstream (the
mirror is read-only). The bug was ours: `MembershipIndex` joined a library item to a list
row on the **bare id value**, and **TMDb numbers movies and shows in separate id spaces** —
movie #1399 and show #1399 are unrelated titles. So a show whose TMDb id coincided with a
Top 250 film matched the film's row and inherited its protection.

- IMDb ids do **not** have this problem (globally unique across movies and TV); TVDb
  cannot (Top 250 rows carry none). **TMDb is the only crossing id**, and it crosses often
  because both id spaces are dense low integers.
- The direction was *safe* — a false PROTECT keeps a file rather than deleting one — but
  the why-panel was stating a reason that was not true, which is the one thing the panel
  exists to never do.
- Fix: the join key is `(media_type, id)`, not `id`. A movie only matches movie rows, a
  show only matches show rows. `media_type` was already stored on every row (even in the
  primary key); it was simply dropped when the in-memory index was built. Rule 6 in one
  line: disambiguate a cross-system join by a *stable* key, and an id is only stable
  *within its own namespace*.

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
fixed strip, which is the unsound behavior being removed.

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
outside the group was discarded in favor of the group. And a failed root-folder read was
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

**Resolved 2026-07-21: the operator's library map recovers the show case the path never could.**
The negative result above is about *inference from the path*, and it still holds: nothing
in a show's path can tell an HD copy from a 4K one. What changed is that the copy no longer
has to be *inferred*. Each *arr root folder is now mapped, in Settings, to the Plex library
its content lands in (`instance.plex_library_map`, a nullable JSON column; the UI is the edit
modal, with suggested matches the operator confirms). When one id names a title in two
libraries, the copy whose `library` equals the mapping is bound -- the strongest corroborator,
applied ahead of folder and size in `identity._narrow_among_id_hits`, because it is the
operator's declaration, not a guess. It only ever *narrows* the id's own candidates and stands
down (keeping the file) on every untrustworthy shape: a candidate whose library is unknown, a
byte-identical twins group, or a mapped library holding none of the copies. That last case --
a stale or renamed mapping -- is surfaced as a `scan.stale_library_map` log warning rather than
allowed to mis-bind. The path-based folder corroborator is untouched; the map sits above it.
Two same-name copies in the *one* mapped library (two instances feeding one Plex library) still
abstain: the library is not a fine enough key to split them, and no finer one is trustworthy.

### Plex title-cases label tags

Write `leaving-soon`, read back `Leaving-Soon`. So any case-sensitive comparison of
label tags silently fails to find a label that is *right there*. The failure mode is
nasty in both directions: "I can't find the Leaving-Soon mark I wrote" becomes either
"add it again" (duplicate) or "this item isn't flagged, so it's safe to act on" (acting
on something the owner meant to protect).

The probe that *discovered* this was itself bitten by it — its cleanup step matched the
tags case-sensitively, found nothing, and left two labels on a real item.

⇒ Casefold every label comparison. `normalize_label()` in `clients/plex.py` is the only
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
full path" can never recognize it; what does is the **exact byte size**: the two listings'
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

⇒ **Normalize per provider, never per source.** "Divide any Rotten Tomatoes value by
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

Also: `GET /request` defaults to `take=10`. Forget to pass it and you silently analyze
the ten most recent requests and conclude the rest do not exist.

### The requester rule must be per-*media*, not per-*request*

Real request data contains the same title requested by several different people. Judged
per request, a film Alice requested and watched is still condemned on Bob's row — and
they share one file, so it is deleted out from under her.

### Seerr models "quality" (HD vs 4K), not "library" — so per-copy "requested by" layers the rating key under the *arr id (2026-07-22)

An operator can keep the same title in two Plex libraries — a main one and a restricted one
for a specific group, fed by two different Sonarr/Radarr instances. Naming *who asked for that
copy* looks like it should join on the Plex `ratingKey` Seerr already carries.

The `ratingKey` **is** unique — it is Plex's own metadata id, the same id space Reaper resolves
against — so on a single Plex server it is a real, matchable key. That is not where it fails. It
fails because Overseerr stores **one** `Media` row per tmdb per portal, with a single non-4K
`ratingKey` slot (`ratingKey4k` is the only other, selected by `is4k`). So the ratingKey is a
*per-title-per-portal* pointer — "where is this title in Plex" — **not a per-request, per-copy
one.** When a portal's Plex sync can see the title in *both* libraries (the restricted-access
group's portal sees the main library too), its one slot holds whichever copy it synced **last**,
which need not be the copy the request was routed to. So a restricted request can carry the main
copy's `ratingKey`, and the join mis-attributes. (Where each portal sees only its own library the
ratingKey would name the right copy zero-config; it is specifically the see-both case that breaks
it. Seerr also stores no file path, so the library-map trick can't cross over.)

So the ratingKey earns a place, just not the only one. Reaper uses it as a **zero-config tier**:
a portal scanning only its own library records the right ratingKey, so most setups get per-copy
attribution with no operator action. The failure mode above — a portal that scans several
libraries collapsing to one slot — is covered by a higher tier that does not depend on Plex sync
at all: `externalServiceId`, the id of the *arr the request was **routed to**. A portal adding to
its own dedicated *arr always points at its own copy, whatever Plex saw. It equals the
`movie_id`/`series_id` in Reaper's `media_key`, so the join key is `(instance, externalServiceId)`
= the candidate's own `media_key`; the one missing piece, `serviceId -> Reaper instance` (serviceId
numbering is local to each Seerr), is an operator-declared map (`instance.service_instance_map`).

Final order, best-first: **declared service map → ratingKey → tmdb/tvdb union.** (A second, rarer
reason the ratingKey sits below the declared map: across *different* Plex servers rating keys are
small ints that repeat, so a bare ratingKey join could collide. Irrelevant on one Plex, real in a
multi-server setup — another reason to let the declared, server-independent id win when present.)

⇒ For a cross-system join, a pointer that is merely *unique* is not the same as one that names
*the specific thing you mean*. The Plex ratingKey is unique yet names "this portal's current view
of the title," so it is a good cheap default but collapses two copies to one; the *arr item id
(with its instance) names one file the request was actually routed to, so it is the reliable
override. Layer them: cheap-and-usually-right under declared-and-always-right.

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

### A Seerr user id is unique only within one portal (cross-instance collision)

Scales rolled every requester up under their bare Seerr user id. That id is minted **per
instance** -- each portal numbers its own users from 1 -- so on a two-portal setup (a
primary and a secondary for a specific group), user id 5 on one portal and user id 5 on
the other are different people. They collapsed into one row: one person's name over
another's combined requests, granted disk, plays, and reclaimable set. The per-person
drawer had the same flaw, matching an id across every portal at once.

The fix is a cross-portal person identity (`services.fairness._identity`): `plex:{id}` for
a Plex-linked account (one human across every portal -- watches and quota already fold by
Plex id), else `local:{portal}:{seerr_id}` for an unlinked local account (unique only on
its own portal, so it carries the portal). This is strictly better than both prior keys:
the original `plex_id` keying merged every unlinked local into one row, and the `seerr_id`
keying that replaced it merged across portals. The portal is stamped onto each request as
it is read (`SeerrClient.instance_key` -> `MediaRequest.portal_key`), so the pure roll-up
never needs a live client to tell them apart. General lesson (rule 6/29): an external id is
only a stable join key **within the system that mints it**; a multi-instance source needs
the instance folded into the key. The *arr side already knew this -- `requested_by` refuses
to bind on Seerr's `serviceId` for exactly this reason; the requester side had missed it.

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
  within hours of the scan. Frozen-at-scan facts are the correct behavior, but any
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
  floor pick up the slack. Defense-in-depth is real — single-gate misconfiguration is
  survivable in this library's shape.

## Plex's trash, measured against a live server (2026-07-25)

- **`autoEmptyTrash` is server-wide and ships ON** (read back with `default: True`). It is
  not a per-library setting and not an unusual choice: it is what a stock install does.
- **So the executor's trash interlock is largely decorative on a default server.**
  `_trash_delta_is_ours`, `_mount_is_up` and `_wait_for_scan` all gate `empty_trash`, but
  when Plex empties the trash itself after every scan, the path refresh Reaper fires per
  deleted item has already triggered that purge, inside Plex, before the gated call
  arrives. The interlock is still worth keeping (it is the only thing standing on a server
  where the setting is off), but it was never the whole defense we described it as.
- **The trash reads empty on every library of a default-configured server**, which is a
  consequence of the above rather than a coincidence, and it means a count-based warning
  stays quiet exactly where there is nothing to lose.
- **`?trash=1` is a real filter on `/library/sections/{key}/all`.** Established against a
  control rather than by assuming a 200 means agreement: an unknown parameter
  (`?zzznotareal=1`) comes back with the FULL library count, while `trash=1` narrows. That
  control is the only reason a zero can be read as "genuinely nothing there" instead of
  "the server ignored the question", and a server that does ignore it answers with the
  library size, which `api/plex_trash.py` detects by equality and reports as unreadable.
  `?unavailable=1` and `?deletedAt>=1` are both ignored.
- **The severity is the other way round from the intuition.** With the setting ON the trash
  never accumulates, so there is little to lose and Reaper could not stop the purge anyway;
  with it OFF the trash grows without bound and Reaper's own section-wide `emptyTrash`
  destroys all of it behind a gate that structurally cannot see it. The catastrophic case
  is the *non*-default one, which is why the blocking warning keys on a count rather than
  on the setting.

## iOS paints an edge-swipe back from a snapshot it took per history entry (2026-07-25)

Reported as "swipe back from the why card lands at the top of the list, then a few seconds
later it snaps back to where I was", only in the home-screen PWA.

- **Nothing was wrong with the app's state or its scroll.** Closing the same panel with its
  own ✕ always landed correctly, and the "wrong" screen was *frozen* -- touching it did
  nothing until it snapped. Driving the same open/close in a phone-sized browser showed the
  body un-freezing and the offset restored synchronously, before the popstate handler even
  returned. What the reviewer was looking at for those seconds was not the page.
- **It was a stale back-forward snapshot.** iOS keeps one picture per history entry, taken
  around the navigation that leaves it, and paints it during the interactive swipe (the
  incoming layer even parallaxes in, which is how it reads as a gesture layer rather than
  the live page). It is dropped when the gesture's watchdog fires, which is the delay.
- **The single shared sentinel is what made the picture wrong.** `backnav.tsx` parked one
  history entry for *all* open layers, so `park()` was a no-op once anything was open. Open
  a card with nothing else open and the entry was pushed right then, and the swipe painted
  the panel's own background -- unremarkable. Change a tab first and the tab change owned
  the only entry, so opening a card pushed nothing at all, and the swipe painted the list as
  it looked at the tab change: scrolled to the top. Which is exactly the sequence the
  operator found by hand ("I went to Sanctuary from Condemned and it stopped going back
  directly").
- **The fix is one entry per layer**, which unwinds identically (N layers, N Back presses)
  and costs the browser N entries instead of one.
- **Corrected: the leftovers must be chased, and it is cheap.** One entry per layer means a
  reload with several layers open leaves several stale sentinels, and the first attempt kept
  the old single step, on the theory that stepping off the reloaded entry crosses a document
  boundary and reloads the page, so a loop would be a loop over page loads walking the tab out
  of the app. **Measured, and it is not true.** Entries parked with `pushState` stay
  *same-document* with the reloaded page: the step reports a popstate, no load, and the walk
  ends by itself on the app's own first entry, whose state is not ours. Logged over a reload
  with two parked:

  ```
  LOAD  state={"__reaperBack":true}
    step -> popstate  state={"__reaperBack":true}    <- same document, no load
    step -> popstate  state=null                     <- the app's own entry. stop.
  ```

  Stopping after one step is what costs the operator: every sentinel past the first is a dead
  Back press, which is the bug the reconcile exists to prevent. It now walks the whole run, one
  settled step at a time.
- **The browser's own marker beats our count, and it is free.** Two measured browser behaviors
  make an in-memory count of parked entries unsafe to act on. A `history.back()` issued *before*
  a `pushState` in the same tick resolves against the entry that was current when it was called,
  so the entry just pushed is discarded and the count ends a step ahead of the stack -- and
  React runs every layout-effect cleanup before any setup, so a layer closing while another
  opens produces exactly that order. A long-press on Back jumps several entries and reports one
  popstate, which drifts the count the same way. Both end with a close stepping off an entry
  nobody parked, which leaves the app with a panel still open. Two fixes, and the second is the
  one that generalizes: an opening layer takes over the entry a closing one has not handed back
  yet (nothing moves, no race), and every step asks `history.state` for our own marker first, so
  a drift costs nothing instead of an exit.

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
