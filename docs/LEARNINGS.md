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
