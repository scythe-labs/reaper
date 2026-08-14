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
  library looks like, and it is why the *Leaving Soon* warning and the human approval gate
  are not decoration.
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

### 5. A play after deletion is a regret — **not if the live check would spare it**

A play shortly after condemnation is often a rescue rather than a failure, and counting
rescues as failures slanders the policy.

⇒ The replay split regrets from rescues at the grace boundary.

**Superseded in reasoning, and the code is gone.** The justification above ("still in
quarantine") described a hold that does not exist: nothing on the deletion path reads the
grace window, so an item is deletable from the moment it is condemned. What actually spares
a late play is the executor's live per-item vetoes, which fire on a play *after the plan was
approved* — not on a boundary N days out. The replay never stopped splitting on the boundary,
so every `rescued` figure it produced was a best case. It was deleted rather than corrected
(M3c, dropped), so **the rescue split is not a number Reaper reports anywhere**; #553 is the
successor and it weighs a returned title down instead of counting it.

### 6. SQLite stores timezones — **it does not**

`DateTime(timezone=True)` is a silent no-op. Aware in, naive out.

⇒ Timestamps are integer epoch. The instant *is* the value.

### 7. The rewatch curve is a constant — **it is a property of an audience**

Shipping one library's rewatch rates as a hardcoded prior makes every lift number on
every *other* library meaningless.

⇒ **Intended, never shipped, and now not planned.** A module existed that could fit the
prior from the operator's own history, and another that could label which prior was used;
neither ever had a caller in `src/`, and both were deleted rather than wired (M3c and M3g,
dropped). Every number in use is the one library's curve tabulated in `docs/SIGNALS.md`, and
nothing in the app can tell you it is borrowed. #554 is the successor.

### 8. The simulator can re-decide a snapshot under any policy — **only what the frozen evidence still answers**

The zero-API-call simulator re-compares **stored** scores against new numbers. That is
exact for `condemn_at` and `coverage_floor_bp` and simply *wrong* for anything else:
change a signal weight or a gate, and the stored scores were produced by the old ones.
A policy editor that let you drag a weight and then showed a confident count would be
the most dangerous screen in the product — the stale number looks exactly as
authoritative as the true one.

⇒ `PolicyBody.scoring_hash()` covers the signals and gates but not the thresholds. The
snapshot records it, and while it matches, re-comparing stored scores is exact.

**Superseded in part — freezing the Facts bought back most of what this gave up.** The
stale number was the danger, not re-scoring itself: once a scan froze each item's
evidence (`Candidate.facts_json`), a weight edit could be answered *exactly* by replaying
the real `score` / `evaluate_all` / `decide_verdict` over that frozen evidence, still with
zero API calls. So `simulate` is now three tiers (`api.simulate.simulate`): stored-score
re-compare while `scoring_hash` matches; **replay** while `evidence_hash` matches, which
covers weights, rating bars, custom condemn rules, graded keeps and protect conditions —
`PolicyBody._EVIDENCE_REPLAYABLE_FIELDS` is the authority, not this sentence; and only then
the refusal.

⇒ The refusal is now scoped to edits that change what a scan would *gather* — a keep tag,
the popularity window — where the frozen evidence really is stale. The
replayable set is an **allow-list**, so a field nobody classified falls into the refusal
rather than into a plausible wrong preview. The lesson survives its own fix: the rule was
never "only thresholds are safe," it was "never show a number you cannot derive."

**And the allow-list cost a year of previews by being coarse.** "Any gate" was the first
scoping, on the strength of one true sentence: the popularity gate's window is the span the
frozen watcher counts were taken over. That is one scalar on one gate, and it excluded the
entire `gates` list, so the rating *bar* previewed while the switch above it blanked the
panel — one card, two answers. Every fact a gate reads is gathered whether or not the gate
is on (no fact builder branches on the list, and `evaluate_all` reads nothing but `Facts`),
so a toggle was always exactly replayable. Measured rather than argued: two full scans whose
policies differ only in a gate freeze byte-identical `facts_json` while their verdicts
differ (`test_scan_pipeline.py`). ⇒ `evidence_hash` folds in `popularity_window_days()`
instead of the list, and a drift guard fails on a gate field nobody classified. **The
conservative default was right and its granularity was not**, which is the failure a
fail-closed choice hides best: nothing breaks, the feature is just quietly useless for the
edit people make most.

Measured on a live library's movie lane, the three toggles behaving three different ways:
switching the rating bars off roughly **doubles** what the panel flags (×2.2 on the count,
×2.4 on the bytes); dropping the mid-dormancy floor moves about a sixth of the spared set
into "not judged" while the headline count does not move at all; and the keep-list gate
moves nothing whatsoever. The last two are #488's complaint, and the useful part is that all
three are now *answerable* rather than blank — an operator can see that a protection is
carrying nothing, which is a fact about their library and not a broken preview.

**Re-scoping this hash costs every stored snapshot one scan**, since a snapshot carries the
hash its own scan computed. On that install `policy_hash` and `scoring_hash` both still
matched while the evidence hash could not, so until the next scan even a weight edit refuses
where it used to replay. It heals on that scan, which is exactly what `schema_version` could
not do — and that difference is the one to check before re-scoping either hash again.

**The season card needed the other half: freeze the inputs, not the answer.** The nine season
rules were the last controls to blank, and no hash change could reach them — `facts_json`
freezes the season guard's *output*, while `plan_series_prune` decides per show from Sonarr's
season statistics, who is part-way through, and each season's watcher count with the mirror's
bound on it. None of that was on `Facts`, so there was nothing to re-decide from. ⇒ The scan
now freezes the plan's inputs per show (`db.models.SeasonPruneEvidence`, one row per show per
snapshot) and the replay calls `season_evidence.plan_from_frozen` — the same function the scan
itself derives its plan through, so the two cannot drift by construction rather than by
agreement test.

⇒ **Two of the three remaining refusals are not hash questions at all**, which is the part
worth keeping. A bundle can be absent, unreadable, or describe a different set of seasons than
the rows being judged; a draft holding the mid-binge seasons over a scan that recorded no
episode map gathers identically and cannot place a viewer. Both are asked of the stored
evidence rather than of the policy, and each carries its own typed reason, so the panel names
one control instead of blanking nine. A hash cannot say *why* it mismatched (§13); it also
cannot say anything about evidence two identical policies disagree about.

⇒ **And that second refusal was written about one of its two producers**, which is the same
error as the paragraph below it and was made in the same commit. The map is unread after a
scan that ran with the hold off, because the fan-out is gated on the guard; it is *also*
unread after a scan that ran with the hold ON and got no answer from Sonarr for some show,
which `season_scan._episodes_for` logs rather than degrading on, since falling back to
whole-season protection can only keep more (rule 28's sanctioned exception). The copy named
the first, so the second operator was told their hold was off while it was on, and the only
action the sentence suggested was turning a protection off. **A refusal derived from the
absence of a thing must state the absence, because a cause is a guess about which producer
left it absent** — and the frozen bundle records the absence, never the producer.

⇒ **The obvious reading of that is wrong, and the review caught it in six places.** "A
snapshot predating the table has a matching hash and no bundle" is a tempting sentence and it
describes nobody: re-scoping the hash to make the season fields replayable *changed the
formula*, so no snapshot written by an earlier build can match it, and every edit lands on the
generic refusal until the next scan. The specific refusals describe the state after that scan.
The general lesson is the one §13 already carries in a different costume: **a change to what a
hash covers silently re-partitions the population every sentence about that hash describes**,
and prose written about the new mechanism will keep describing the old population unless
somebody computes both formulas and compares them.

⇒ **Naming the cause in the generic refusal was wrong more often than it helped**, for the
same reason. It read "a keep tag, a season rule, or how far back watching counts reads
differently from your policy now" — three causes it could not distinguish between, at an
operator who, on the first Policy page after any upgrade that moves the formula, had changed
none of them. It states the condition and the remedy now, and nothing about the cause.

⇒ **A perf skip became a correctness hole once the evidence was frozen.** The episode read was
skipped for a show whose every season was already kept — true reasoning for a scan, since
mid-binge precision cannot change "keep everything", and exactly wrong for a preview: a show
fully kept under today's keep-last is *the* show that becomes prunable when the operator lowers
it. The skip is gone, at one Sonarr `episodes()` call per fully-kept show while the guard is on.
The general shape: **an optimization justified by "this cannot change the answer" expires the
moment something else starts asking a different question of the same data.**

The exactness proof is a sweep, not a sample. Each of the nine settings is edited alone, two
real scans are run, and the replayed guard for every season must equal the second scan's stored
one; a combined edit was the first draft and it proved three of the nine, because the settings
mask each other (keep-last already holds the season `protect_incomplete_seasons` would).

**23 mutations were re-run mechanically, and 22 of them failed.** Eighteen are one per setting
on each of the two roads a `PolicyBody` then took to the planner, `SeasonPolicy.from_body` and
`season_scan.gather`'s own repack of nine loose parameters, each pinned to its shipped default,
which is exactly "this road drops the operator's edit". Every one reds its OWN parametrization,
so the sweep discriminates all nine settings rather than catching them in a bundle. The repack
is gone since and `gather` takes the carrier, so nine of the eighteen have no site left to
mutate. What that costs the sweep is worth writing down: with one road, a value dropped in
`from_body` is dropped on the scan and the replay alike, so the replay still reproduces the
scan and the *scan-against-scan* assertion is the one that reds. **Comparing guard reasons rather than
verdict tallies is what made that possible**: a setting routinely changes why a season is kept
without changing whether it is. Four more fail too — restoring the skip, collapsing the
three-state to `{}`, returning the frozen guard instead of re-deriving it, and dropping the
missing-map refusal.

**The one that survived is the finding.** Replacing `now=inp.now` with a live `utcnow()` in
`plan_from_frozen` — the mid-binge expiry measuring against whenever the editor was opened
rather than the scan instant — passed all 3,547 tests. The exactness sweep freezes its clock ten
days from the wall, and ten days moves no viewer across a 180-day hold, so the fixture best
placed to catch it was shaped not to. The behavior was always right; nothing held it there.
`TestTheReplayExpiresAgainstTheScansOwnClock` does now, off a bundle scanned ten years back so
the drift dwarfs any hold, with a negative control proving the assertion reads the guard at all.

The general shape: **a mutation count is worth nothing without the harness that produced it.**
This claim previously read "26 mutations … all 26 fail", written by hand from an enumeration
that does not add to 26 either way, and it was wrong in the direction that flatters — asserting
coverage of exactly the case that had none. A count nobody can re-run is a number, not a proof.

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
`api.simulate.simulate` were unreachable, so the panel an operator tunes a deletion threshold
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

### 14. `sizeOnDisk` measures a folder — **wrong, in both \*arrs, and it cost a planned feature**

First settled by reading Sonarr's and Radarr's own source on 2026-07-30, then confirmed on the
*release tags* an operator runs and **measured against two live instances** (#263, the ratio
below). Sonarr's `seasons[].statistics.sizeOnDisk` comes from
`SeriesStatisticsRepository.EpisodeFilesBuilder()` as `SUM(COALESCE("Size", 0))` selected `FROM
"EpisodeFiles"` and grouped by series and season. Radarr's movie `sizeOnDisk` is the same shape:
`MovieStatisticsRepository.MovieFilesBuilder()`, `SUM(COALESCE("Size", 0))` over `MovieFiles`.
Neither ever stats a directory — in fact `IDiskProvider.GetFolderSize` has no production call
site in either tree, its only reference outside the declaration being a test that asserts it is
never called. Unchanged in shape across every Sonarr stable tag from v3 to `v4.0.19.2995`, and
in Radarr since `v5.3.6.8612`; **before that Radarr had no statistics repository at all** and
served `MovieResource.ToResource`'s `model.MovieFile?.Size ?? 0`, a single row rather than a
sum, which is a real difference for a multi-file movie on Radarr below 5.3 and still not a
folder walk.

That kills a premise the tree had adopted in five places and `STATUS.md` had ranked as its
sharpest open item: that a season's frozen size is a *folder* while the executor's live re-read
sums episode *files*, so the growth interlock was comparing two quantities and had been silently
desensitized. Sonarr's statistic and `GET /api/v3/episodefile?seriesId=` read the same table and
the same column, so the two sides always matched and the interlock's blind band was only the
tolerance `_grew_materially` declares. The size-truth plan's Stage 5 existed to repair that and
would have changed no number; Stage 6 rested on the same error for movies, where `sizeOnDisk`
and `movieFile.size` are equal for the ordinary one-file movie, so its rung 2 could recover
nothing by construction. Both were dropped.

⇒ **The ratio, measured.** Every season on two live Sonarr 4.0.18 instances was read both ways
— `seasons[].statistics.sizeOnDisk` against the summed sizes from `GET
/api/v3/episodefile?seriesId=`, using the executor's own `_payload_size` and `_season_number`.
**Every season matched to the byte, all several thousand of them**, ratio exactly 1.000000, no
season differing by so much as one byte and none where the statistic was missing. On Radarr 6.4.0
the same read over every movie holding a file gave `sizeOnDisk == movieFile.size` for every one,
which is #317's premise from the other side: the number tracks file rows, so folder bytes no row
tracks are not in it. The one edge the probe could not exercise is Sonarr's `COALESCE("Size", 0)`
on a NULL-size file, which no season in either library had; it resolves toward keeping, because
`_payload_size` refuses a zero and the season is held back.

⇒ **The error survived seven review passes because the claim was checked against the field
name and then copied.** Nobody had to be careless: each of the five sites was written by
someone reading one of the other four, and an adversarial pass that re-reads the tree finds
five agreeing statements, which is what agreement looks like when it is wrong. Six of seven
audit agents confirmed it on this evidence, several citing the executor comment that *makes*
the claim as the proof of it. **A fact about someone else's service is verified at that
service, or it is not verified** — vendored spec, upstream source, or a live probe. This is
rule 144's failure mode with an external subject: the copies vouch for each other.

⇒ The direction that survives is the opposite one, filed as **#317**: Radarr's number sums
tracked `MovieFiles` rows while `MoviesDeletedEvent` removes `movie.Path` recursively, so extras
and artwork are freed and never counted, and a byte cap that under-counts does not fire.

### 14b. That gap, measured: 0.02% at the median, 44% at the worst (2026-07-30)

Radarr's own `/api/v3/filesystem` walks a folder over the API and reports a size per file, so the
folder can be measured without a mount. Two hundred movie folders on one live library and every
sized folder on a second were walked recursively and compared against `sizeOnDisk`, after
confirming no movie path was shared with, or nested inside, another (which would double-count) and
that every tracked file sat under its own movie folder.

**The folder held more than the reported number in 221 of 221 folders. It never held less.** On
the main library: median ratio 1.0002, p75 1.0005, p90 1.012, p99 1.041, max 1.111, and 1.002
aggregated over the sample. Twenty-three folders in two hundred were 1% or more, two were 5% or
more. On the second, smaller library one folder measured 1.44 — tens of gigabytes of untracked
bytes in a single file.

⇒ **The excess is not artwork rounding, which is why the median is the wrong summary.** By count
the untracked files are overwhelmingly images, about three in four in the sample, but by bytes
they are **74% video**: 26 untracked video files carried three quarters of the excess, and 12%
of the excess sat in a subfolder rather than beside the movie. Artwork was 25%, subtitles 1.4%,
metadata 0.06%. The distribution is a floor of a few hundred kilobytes of posters on nearly every
folder, plus a heavy tail wherever an extra, a trailer or an untracked rip sits in the folder. A
margin sized to the median buys nothing and a margin sized to the tail would eat most of the
operator's cap, which is why neither was added.

⇒ **The season side has no counterpart, and the reason is the delete's shape, not the number's.**
A season prune deletes `EpisodeFiles` rows one at a time and the statistic sums those same rows,
so the two are one quantity by construction (learning 14). Measured anyway: a season folder held
0.008% more than its files at the median and 0.3% at the worst, all of it sidecars the prune never
touches. The asymmetry in one line: **for a season the number counts what the delete removes; for
a movie the delete removes a folder and the number counts files.**

⇒ **What was accepted, and what it costs.** The byte caps are charged in tracked bytes, so a run
frees a little more than the cap admitted. Nothing unapproved is deleted by this — the operator
approves items, and the caps only bound how many approved items proceed — so the failure is that
a byte budget behaves like a slightly larger one, measured at 0.2%. That is accepted; what was
not acceptable was the claim, stated in three places, that the rolling byte cap made a
multi-terabyte incident *arithmetically unreachable* and that **no sequence of runs can exceed
it**. Absolute claims were corrected to bounds; the approximate copy ("500 GB per run") was
deliberately left alone, since at the resolution an operator reads it the number is right and
hedging twenty strings would cost more than the 0.2% it describes. `docs/DECISIONS.md` under
*Size acquisition* carries that call and the folder walk that was declined with it.

### 15. `noopener` costs you the close — **only the opener's half of it**

Reaper opens the Plex sign-in window with `noopener`, so plex.tv holds no `window.opener`
handle on the page that takes the operator's Reaper password. That was recorded as buying the
protection at the price of the auto-close, on reasoning that sounds airtight: a browser permits
`close()` on a cross-origin window *because* you are still its opener, so severing the opener
drops the close along with it.

Half of that is right. The **opener** does lose its handle, unavoidably: with `noopener`,
`window.open` returns `null`, so there is nothing to call `close()` on, and no amount of care
recovers it. What does not follow is that the window cannot be closed. A window opened by a
script may close **itself**, opener or not — Chromium gates `close()` on `opened_by_dom ||
history.length <= 1`, and `window.open` sets `opened_by_dom` whether or not `noopener` was
passed.

Measured rather than reasoned: a `noopener` popup that reported `opener === null` and had
pushed its session history to two entries closed itself on `window.close()`. The control, the
same page with `close()` swapped for a no-op, reported back that it was still open — so the
silence in the first run was the window going away, not the report failing. Reaper now has
plex.tv forward that window to a page of its own whose only job is to close it, and keeps the
protection.

The generalizable shape: **"A and B cannot both be had" is a claim about a mechanism, and it is
worth asking which side of the mechanism the constraint actually sits on.** Here it sat on the
opener's handle, and the fix was to stop needing one. This one had been settled prose in a
commit message for months, and it took an operator asking "we opened it, why can we not close
it?" to get it tested.

### 16. An external id names one copy. **It names one or two** (measured 2026-08-14)

Roughly one movie entry in 150 shares its TMDb id with a second entry on the measured library,
one per copy, each binding a different Plex listing. A 4K alongside an HD, or two instances
managing the same title.

This was found while designing #553, whose first shape was a ledger keyed on external id
recording the Plex rating key last seen for that id. Keyed that way, the two copies overwrite
each other every scan, so the ledger reads a changed key forever and every one of those titles
takes a permanent hold nothing can clear.

It is worth being precise about why the code review would not have caught it. `identity.py`
already documents that one id can name several Plex items, at length, and narrows among them
with four corroborators. The mistake was on the other side: assuming the **\*arr** holds one
entry per id. Nothing said otherwise, and nothing was wrong, so there was nothing to read.

⇒ A ledger keyed on an id holds a **set**, and identity churn is only real when the keys it
displaced are gone from the index too. More generally: an id-keyed store wants its cardinality
measured against real data before it is keyed, not reasoned about from the code that produces
the ids.

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

### A regular-gap "rewatch cycle" does not exist here (measured 2026-08-12)

The tempting protect signal, and the one #554 originally asked for: a title played at steady
intervals (every October, every ~12 months) is due again. Backtested out-of-sample at three
cutoffs over 8+ years of history, it is chance, and the eyeballed examples that motivated it
(a horror film landing in the same month several years running) are what chance produces
across a few thousand titles.

- With exactly two gaps, max-deviation, MAD, and CV all reduce to the same statistic, and
  random play times pass a spread threshold `t` with probability exactly `t`. Three-viewing
  titles were ~3/4 of everything the detector found.
- The ablation: titles passing the regularity test replayed **no more often** than titles
  failing it, matched on dormancy and viewing count. Every lift CI straddled 1, at every
  parameter setting tried, including the annual-cycle subcase.
- The phase test: among detected titles the next play landed around 1.3x the supposed period,
  with only a minority inside a generous window. There is no phase structure to detect.
- A positive control on the same harness (a heavy viewing count) showed strong lift, so the
  null is the data's answer, not the harness's.

What does carry signal is **frequency plus recency, with no periodicity claim**: many
qualified viewings plus a recent play replays at roughly 1.5-2x the dormancy-matched base
rate, across hundreds of titles and all three cutoffs. And the count must be of *qualified*
plays: unfiltered, over half of the apparently cyclic titles owed their pattern to abandoned
sub-50%-complete plays.

⇒ Reaper's rewatch protection says "watched again and again, and recently," never "on a
cycle, due again." The first sentence is plainly true; the second is unsupported. Measured on
one heavy-rewatch library, so the constants ship as starting values (`docs/history/REWATCH_PLAN.md`).
Re-opening periodicity means clearing the lift bar in `docs/SIGNALS.md` first.

### Stage 1 implementation verification: an exact reproduction (measured 2026-08-13)

Read-only pass against the live mirror, independent of the committed module: an outside
reimplementation of the play filter and viewing clustering was compared against
`services.rewatch`, and the shipped module's qualified-viewing count and last-play date
matched it exactly on every movie key checked.

- The play filter is not a rounding error: it excluded about a third of raw movie
  `watch_event` rows, in line with the abandoned-play finding above.
- About 7% of played movies carried no qualified play at all -- the never-watched shape
  (`Known(0)` viewings paired with an `Absent` last play).
- Of movies with at least one qualified play, roughly one in six cleared the shipped
  10-viewing default bar, and about four in five of those also cleared the 730-day
  recency window.
- `watched_status` was never `NULL` anywhere on this source, so the play filter's
  `percent_complete` fallback arms are exercised by the unit suite alone here, not by any
  live data seen so far.

⇒ Stage 2 must not assume `percent_complete` is a well-populated input before leaning on it
in any fit: on this source it has never been the deciding field, and its coverage on a
library where `watched_status` is sometimes unset is still unmeasured.

### Stage 2: the fitted curve tracks the borrowed one (measured 2026-08-13)

Read-only pass against the live mirror after the stage 2 estimator landed: an independent
reimplementation of the bucketing, eligibility, and monotone merge agreed with the shipped
`fit_blocks` exactly, block for block, over the current candidate population.

- **The library's own curve is nearly the borrowed default.** Every band's fitted rate sits
  within about 3 points of the `docs/SIGNALS.md` ground-truth table, in both directions.
  The display is honest and the shipped scoring defaults were never far off here.
- **The merge earns its keep on real data**: the raw curve inverted once (the one-to-one-
  and-a-half-year band sat just under the band after it), and pool-adjacent-violators pooled
  exactly that pair. Nothing else moved.
- **The floor and the withhold never fired on this population**: the thinnest block held
  a couple hundred titles, and the mirror reaches far past the deepest bucket. Both arms
  live on unit coverage here, like the play filter's fallback arms.
- **The hold is coarse by construction and the echo matters**: at a 25% threshold about four
  in five movie candidates are protected; at 40%, about half. A percentage without the
  consequence echo would read as a fine-grained dial while acting as a dormancy cliff.
- A title played the day of the scan has dormancy exactly 0 and fell through the strict
  first bucket edge (a fraction of a percent of candidates, live). The first bucket is
  closed at zero now, in the fit and the lookup both.

⇒ Stage 2 ships as designed. The named limit from the TV backtest below (matched-control
lift compresses near a shared ceiling) does not arise here: the estimator states cohort
rates, never lift.

### TV: the replay-period formulation clears the lift bar (measured 2026-08-13)

Read-only, the plan's TV harness exactly: detection from pre-cutoff plays only, qualified
plays through the shipped filter, 30-day show-level periods, the quarter-replayed
discriminator, controls matched half-to-double on dormancy and period count, bootstrap CIs.
Four cutoffs: three a year apart plus one interleaved six months before the newest, added
after the first pass to widen the newest leg's support.

- **Two or more replay periods, plus a qualified play inside two years, cleared the bar at
  every cutoff** (lifts about 1.13 to 1.28, every CI clear of 1), and three replay periods
  cleared it too. One replay period alone failed at half the cutoffs: the two-period
  threshold carries the signal, not replay activity as such.
- **The discriminator separates rewatching from following**: about three fifths of the
  qualifying shows' outcome-window plays were replays of already-seen episodes, rising
  toward seven tenths as the bar tightens. The keep would protect actual rewatching.
- **A named harness limit**: the frequency-plus-recency positive control failed at the
  cutoff nearest the data's edge, and diagnosis showed a ceiling, not thin pools. Tripling
  the matched-control pool barely moved its lift (still astride 1) while both arms sat
  around two and a half times the population base rate. Matched-control lift compresses
  when both arms share a ceiling; the formulation's own pools at that cutoff stayed
  healthy, and its pass stands on the other three legs regardless.
- Episode rows lacking a show key were under 2% and excluded. The episode-identity
  fallback (parent key plus index) was never needed on this mirror: every episode row
  carried its own key.

⇒ A TV keep is unblocked per `docs/SIGNALS.md`'s bar, as a period-based, replay-discriminated
condition, never a play count alone. It ships as its own stage with its own mockups, and
nothing in the movie lane moves for it.

### TV: the shipped derivation reproduces the validated formulation (measured 2026-08-14)

Read-only, after the TV stage was implemented: `services.rewatch.show_rewatch_stats` over
every show in the mirror, cross-checked by an independent SQL pull through the same play
filter fed to the pure `replay_period_count`. Five of five sampled shows agree, spread from
a zero-replay show to the sample's heaviest rewatcher. Episode rows lacking a show key held
under the validation's 2% figure.

- Half of watched shows never clear one replay period, and roughly a fifth sit at exactly
  one: a single burst of activity, the release-following the discriminator is there to
  exclude.
- The shipped bar (2 re-watches, last qualified play inside 2 years) covers about a fifth
  of watched shows on this heavy-rewatch library, the top of the plausible band; a bar of
  3 covers about a sixth.
- A TV body stored before this stage keeps the class default of 10, and that bar fires for
  under 4% of watched shows here. Conservative, visible on the policy card, and not inert.

### TV: the fitted curve clears its own bar, with a cliff movies do not have (measured 2026-08-14)

Read-only, the movie stage-2 fit's exact semantics applied to shows (any-play outcome
within 365 days, dormancy at cutoff, the shipped `fit_blocks`), at four cutoffs a year
apart on the same mirror. Run before wiring the TV percent hold, per `docs/SIGNALS.md`'s
bar for any new displayed figure.

- **Fittable and monotone at every cutoff**: one pool-adjacent-violators merge fired across
  all four fits, on a pair of thin deep buckets; everything else was monotone raw.
- **Strongly discriminating**: the least-dormant block's Wilson lower bound sat far above
  the deepest block's upper bound at every cutoff, roughly 55 to 60 points against 3 to 4.
- **Stable within sampling noise**: per-band rates spread 2.5 to 6.9 points across
  cutoffs, about double the movie fit's spread on the middle bands, within the bands' own
  confidence widths. Thinnest block 76, against the floor of 30.
- **The finding: TV rewatch cliffs where movies plateau.** Shows dormant under a year got
  watched again at rates around three in five; by two years the rate is near one in ten,
  and past three years low single digits, where the movie curve stays double-digit past
  five years. The added-date fallback arm was unmeasurable from the mirror alone (no added
  dates there); the shipped fit anchors it on the scan's own added dates.

⇒ The TV hold ships with the same grammar as movies, and the same threshold protects a
shorter dormancy range on TV. The ladder and the consequence echo carry that difference to
the operator; nothing in the copy needs to.

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
the delta (a couple hundred rows against a six-figure history), and `INSERT OR REPLACE` on
the stable `row_id` makes a one-day overlap free. Backfill is then caught by a **nightly
full sweep**. Per-scan
history sync went from ~3 minutes to sub-second, verified live.

Two things the probe also settled, each a latent bug:
- **`row_id` is null only for live/in-progress sessions** (they cluster at the newest
  end; on the order of ten across the whole history). Skipping them is correct -- they are
  not history yet.
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
`mypy --strict` was perfectly happy — an unused parameter is legal — and the replay engine
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
| 0–365d | ~61% |
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

⇒ This curve is a property of *an audience*, not a constant, so never ship someone else's
rewatch curve as if it were physics. **Reaper still does, and now deliberately**: the module
that could have fitted one per operator never had a caller and was deleted with the replay it
served (M3g, dropped). The curve in `docs/SIGNALS.md` is what every default reads, it is one
library's, and #554 is the successor if a fitted answer is ever wanted.

### A title does not leave an active library by accident (measured 2026-08-14)

Over 35 snapshots spanning 24 days, across roughly 3,500 movies and 2,500 seasons, **nothing
disappeared and came back**. One movie and two seasons left and stayed gone. The library only
grew, every scan.

A **negative result, and the one that sizes #553.** Its whole worry was that an innocent cause
would drag a title back, an import list or a pack, and produce a false regret. On an active
library the base rate for a title leaving at all is near zero, so a return is not something that
happens by mistake. It is also why the design needs no exclusion-list round trip to prove a
re-add was deliberate: there is nothing to tell it apart from.

A floor, not a proof. One library, 24 days, an operator who adds rather than removes.

### The Plex rating key is stable enough to detect a return (measured 2026-08-14)

Same window. Holding the \*arr entry fixed, roughly **one movie entry in a thousand** changed the
Plex rating key it was bound to, and **no season did**.

**Every one of those changes was fast.** Between the last scan showing the old key and the first
showing the new one: 2.5, 4.2, 18 and 30 hours. Each is an upper bound on the true absence.

⇒ Mechanical churn and a regret are separable **by duration**, and they are not close. A file
replaced in place, or a title deleted by mistake and put straight back, resolves in hours. A
regret takes as long as it takes someone to notice they miss it. So a detector for "this left and
came back" earns its precision from a **minimum absence**, not from working out what caused the
change, and a multi-day bar removes the whole measured noise floor without trading away
sensitivity.

**A clock alone does not survive an irregular scan cadence**, and the same library shows it. The
interval between scans averaged 17 hours and reached **202**. A last sighting records when Reaper
last *looked*, not when a title left, so a measured absence overstates the real one by up to one
scan interval, and an eight-day pause turns a minutes-long file swap into an eight-day absence.
The fix is to require that Reaper actually **ran** while the title was missing, by counting the
scans between the two timestamps. It costs nothing, needs no per-item state, and holds at both
extremes: a dense cadence leans on the clock, a sparse one leans on the count.

**Measured with a query, not a rebuild**: `candidate` keeps `plex_rating_key` and the external
ids per scan, and `snapshot` keeps the timings, so any install with snapshot history can re-run
all of this against itself before trusting the detector.

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
mattered.

The discriminator that *was* available had been thrown away one function earlier:
`clients/plex.py` reduced each Location to `to_basename(path)` and discarded the rest of
the path. Measured across all three: leaf identical, **parent folder different, shared
suffix depth exactly 1**. Keeping the full path and comparing *trailing segments* (never
whole paths — the mount roots differ, which is why `to_basename` exists) separates them
cleanly. Re-running the real resolver over the live library: **ambiguous 6 → 0, all six
moving into the matched set, and the unmatched count unchanged.**

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
corroborator that stands aside is not neutral: it
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
comparison form for a label, and removal re-reads the item to remove the tag under the
exact spelling Plex is using. It delegates to `reaper.text.fold` now, which is the same
derivation for every name comparison in the tree and keeps its own docstring for the
Plex-specific reason.

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
same-content only, and caught by supervised execution (a person reads the list and types
the phrase). Note the grace window is **not** part of that guard: nothing on the deletion
path reads it.

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

**The Rotten Tomatoes half of that sweep is thin, and #244 turns on exactly it.**
Re-measured 2026-08-03 with the values tallied by source: the listing slots ran ~78%
IMDb, ~22% TMDb, **under 0.1% Rotten Tomatoes and zero Metacritic** — a handful of
readings, not a distribution. So the 0-10 contract is measured; that a *percentage*
source never exceeds it is not. The same sweep found the `rating` slot all but unused
(effectively every item carried `audienceRating`, one carried `rating`), which makes the
Tomatometer the least sampled path of the three. Reading the empty `(10, 15]` band as
proof the rescale is unnecessary would be reading an absent population as a negative
result.

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

For calibration, as of 2026-07 and at default settings: Maintainerr shipped **no auth of
its own**, and Seerr trusts whoever logs in first.

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

Autogenerate rendered a custom type as `reaper.db.types.EpochDateTime()`, named `TZDateTime`
at the time, **without emitting
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
`explanation_json` + `watch_event` + the IMDb mirror. The why-panel's record contains
everything that decided an item's fate. Two extraction subtleties bit before fidelity
reached 100%, both notes for anyone parsing
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
  not a per-library setting: it is what a stock install does.
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

## One grid line took the whole Policy page off a narrow phone (2026-07-26)

Reported from Safari on a phone: the Policy page's context band, section rail and cards all ran
past the right edge. Measured in WebKit against the real page at a range of widths.

- **`1fr` is `minmax(auto, 1fr)`, and that `auto` is a floor, not a preference.** Both
  two-column grids that collapse on a phone (`.editor`, `main.split`) were written
  `minmax(0, 1fr) minmax(340px, 440px)` at desktop and then dropped to a bare `1fr` in the
  collapse, losing the `minmax(0, ...)` exactly where it matters most. The `auto` floor is the
  widest descendant's min-content, so one un-shrinkable control set the column to 362px on a
  344px screen and **stretched every sibling to match**. Measured at 320px: 143 elements
  overflowed, of which **141 were collateral** — they were not too wide, they were stretched.
  Relaxing the one track took it to 2. The lesson generalizes past this bug: a single-column
  collapse should always be `minmax(0, 1fr)`, because a bare `1fr` converts any one child's
  overflow into the whole page's.
- **A flex row cannot get narrower than its labels laid end to end.** `.tabs` and `.segmented`
  floored at 300px and 304px at *every* viewport, so the lane switcher ran off a narrow phone
  on its own, with no grid to blame. Same shape for `.pace-matrix`, whose three `max-content`
  tracks are fixed by construction (324px), and `.pace-extra` (331px).
- **Negative result: an overflow count can read clean while a control is unreadable.** The
  first fix passed "nothing extends past the viewport" while rendering the size unit as "ME"
  instead of "MB" — `.qty` is `overflow: hidden`, so a unit squeezed out of its box is
  *clipped*, and a clipped element measures as fitting. This is rule 138's failure (content
  past an edge that cannot be scrolled to) reached from inside a control rather than from the
  page. Any check for this needs to measure a `select` against its **widest option**, not
  against its own rendered text, which is by then already the truncated one.
- **Following that instruction to the service modal's mapping pickers, the clip turned out to be
  total, not partial.** `.plex-map-grid` is `minmax(0, max-content) minmax(0, 1fr)`: the folder
  path takes the width it needs and the picker takes the remainder. Measured against the shipped
  stylesheets at a 390px phone panel, the remainder is **41.4px** — the control's own border,
  padding and arrow, and **not one character** of the chosen library, against the 186.2px that
  name needs. Two libraries sharing a prefix do not read alike there; they read as nothing at
  all, on the screen that decides which folder Reaper matches to which library. Stacking the grid
  below 640px gives the picker the whole row (220.1px) and the names come back.
  **The generalizable part: `minmax(0, ...)` on a track does not make its content shrink
  gracefully, it makes the failure invisible.** It buys the page out of overflow by pushing the
  loss inside the control, and when that control is a `<select>` there is no wrap to fall back on
  and no scroll to reach — so an overflow count reads 0 while the value is gone. Pair the floor
  with a check that the `1fr` neighbor of a `max-content` track still has room for its own widest
  content, or the two rules cancel: one hides what the other would have caught.
- **Font boosting is testable in a simulator and invisible to headless WebKit.** Turning the
  phone landscape put one notice in two type sizes, which is WebKit's text autosizing scaling
  a block by *that block's* own width. Headless desktop WebKit implements neither the bug nor
  its opt-out: it drops `-webkit-text-size-adjust` *and* the unprefixed `text-size-adjust` at
  parse time (a probe rule kept its `color` and lost both), because autosizing is mobile-only.
  So the fix was written blind, against the standard remedy alone, and then confirmed on an
  iOS simulator, where the oversized clause came back to the size of the sentence it finishes.
  The opt-out belongs on `html` and reads `100%`, not `none`, which would also take away the
  reader's own text-size setting and pinch zoom. The general lesson outlives this bug: for
  anything mobile-WebKit-only, Playwright can tell you nothing either way, and a simulator is
  the cheapest engine that can. The overflow fixes above were confirmed the same way.
- **The fix surfaced two defects the bug had been hiding, and the review caught them, not the
  sweep.** Capping `.editor` narrows the column from an over-stretched 324px to a true 280px at
  a 320px viewport, and two controls that had been riding on the over-stretch stopped fitting.
  The section rail's four labels stop pairing two to a line below 329px, so it takes a *third*
  line (137.8px) while `.policy-section`'s scroll-margin stayed 96px: every rail click parked
  the heading 42px *inside* the sticky rail. And the preset picker, floored at its own labels,
  ran 20px past `.intent-band`'s content box with its rounded end 3px outside the card border.
  Both measured absent on `dev`. **The generalizable lesson: after a change that makes something
  narrower, "does anything still overflow" is the wrong question on its own — ask what now
  WRAPS that did not.** Line counts are load-bearing anywhere a sticky header and a
  scroll-margin have to agree, and neither one announces the coupling.
- **Measure against the container, not only the viewport.** The overflow sweep read 0 at every
  width while the picker hung outside its own card, because nothing had left the *screen*. This
  is the same shape as the clipped "MB" above, and the second time in one change that a check
  measuring one boundary was blind to a control failing at a different one. A layout assertion
  should name the box it is asserting against.
- **A `max-content` track ignores soft wrap opportunities, so `white-space: normal` on its child
  does nothing.** `.pace-matrix .row-h` was relaxed to `normal` to let "Disk freed" take two
  lines and give the quantity boxes the column width back. Measured, the column is an identical
  62.4x19.8px either way: max-content sizing is defined over the max-content contribution, which
  does not consider wrapping. Wrapping that header means giving up the `max-content` track, not
  changing `white-space`.
- **Two engines disagreed on where the rail wraps, so the breakpoint follows the one the bug is
  in.** WebKit takes the third line through 328px, headless Chrome through about 326px. The
  breakpoint is set from WebKit, because that is the engine the report came from; in Chrome the
  cost of the mismatch is a slightly over-generous jump offset on a 327px screen, which nobody
  can see. Where engines disagree on a pixel, the reported one wins and the other absorbs the
  rounding.
- **The seven deferred twins are done, and one of them did overflow.** `.add-grid`, `.set-row`,
  `.about-kv`, `.backup-facts`, `.kv2`, `.safety-row.pw-row` and `.docs-body` were recorded here
  as bare-`1fr` collapses to fix the moment any commit touched them. A commit touched `.set-row`,
  and the deferral came due: the note's "none is known to overflow today" had already stopped
  being true. Plex's connection picker holds a full `plex.direct` address as its only option
  while the server list is still loading, and a `<select>`'s min-content is its widest option, so
  the row's `auto` floor forced the track to **593px inside a 350px card** — the select and the
  help text sharing that track both ran off a page with no sideways scroll. `min-width: 0` on the
  select does not help: the floor belongs to the *track*, and to the grid item above it, so both
  `.set-control` and the flex container need it too. Every column is now `minmax(0, 1fr)`,
  including each twin's phone variant, which is where this bites and where **all seven** still
  carried the bare form. Four of them (`.add-grid`, `.about-kv`, `.backup-facts`, `.kv2`) carried
  it at the base rule too; the other three were already `minmax` there and bare only on the
  phone, which is the direction that hides — the wide layout looks right and nobody re-drives
  the narrow one.
- **A width cap can hide a collapse.** This one was invisible until the mobile `max-width: 22rem`
  came off the control, because the cap was clamping the blown-out track back to something that
  nearly fit. Removing a cap does not create an overflow; it reveals the one the grid was already
  computing. Re-drive the narrow widths whenever a `max-width` leaves a layout.
- **In a wrapping flex row, the basis decides who yields, and `auto` means nobody does.** Line
  breaking runs *before* shrinking, so a box with `flex: 1 1 auto` asks for its full content width
  first and the sibling beside it is pushed to the next line — the box never gives way, because it
  was never asked to. Measured with a 37-character server name beside a Refresh button: `auto`
  wrapped the button at all of 641–1400px, `flex-basis: 0` at none of them, with the box settling
  at exactly what the button leaves. The trap is that `1 1 auto` reads like the accommodating
  option and is the rigid one. **But the fix does not generalize down the page:** where a row
  stacks and the wrap *is* the intended layout, a zero basis makes every box split the line evenly
  instead, which took a hostname field to 84px — six characters — on a 390px screen. So the basis
  is `0` where boxes share a track with a button, and `auto` again inside the stacked breakpoint.
  Two declarations, opposite values, for the same elements at different widths.
- **`scrollWidth` is the wrong instrument for a grid whose overflow goes leftward.** A cluster row
  ends its control with `justify-self: end`, so when its `auto` track is squeezed the control
  overhangs *into the label column*, never off the page: `scrollWidth − clientWidth` stayed 0 at
  every column floor from 12rem to 24rem while the label column was being crushed to nothing. The
  label column had in fact reached 0px wide and a row 4,895px tall — one word per line — with the
  page reporting no overflow at all. Measure the gutter between the two columns, not the document
  width, wherever a track can be squeezed from one side.
- **A fixed track and an `auto` one are visually identical for a right-aligned control, so the
  track's width is spent entirely on the column beside it.** `.set-row` reserves a 352px control
  column, but `.set-control` is `justify-self: stretch` with `justify-content: flex-end`, so the
  control already rests on the row's right edge whatever the track measures. Releasing the track to
  `auto` with `justify-self: end` moved a 40px `Switch`'s right edge **0.00px at 641, 768, 900 and
  1200px**, and left its size untouched; the only thing that changed was the help paragraph, which
  stopped wrapping — the reverse-proxy row went 291.9px → 153.1px at 641px, 11 lines to 4. Above
  900px it changes no height at all, because by then the label column already clears `.help`'s 62ch
  cap (499.9px), so the whole cost lives at ≤768px. "The boxes line up on one track" is therefore a
  claim about *boxes*: a switch, a button or a link on that track pays up to 312px of label width
  for an alignment it was getting for free. **The general form: before accepting a layout cost as
  the price of an alignment, measure whether the alignment survives without it.** This one sat in
  `unproven.md` as a design judgment waiting on a mockup, and it was a two-minute measurement.

## Dormancy cannot bound an all-time count, by construction (2026-07-27)

Qualifying the two watcher counts by the mirror's reach needs a *span* to compare the reach
against. Recent watchers has an obvious one, the policy's popularity window. All-time
watchers needs the item's whole life on the server, and the tempting shortcut —
`Facts.days_observed_unwatched`, which is already there and needs no plumbing — cannot serve.

**It can never exceed the reach.** `dormancy.reference_instant` measures from `last_played`,
else `max(added_at, horizon)`, else nothing at all (no number, so no bound to exceed). A
never-played item is therefore measured from the
mirror's edge whenever it predates the mirror, and a played one is measured from a play the
mirror by definition holds. Either way `days_observed_unwatched <= history_reach_days` for
every item, so `reach >= dormancy` is true always and a guard written that way fires never.
The clamp is deliberate and correct — it is what stops a year-deep mirror claiming a file has
been ignored for five — and it is exactly what makes the value useless as this bound.

The general form: **a value already clamped to X cannot be used to test whether X is big
enough.** Reuse of a nearby field is cheap right up until the field's own safety clamp is the
thing you are trying to measure. What the bound needs is a number free to exceed the reach,
which is why `Facts.days_since_added` is measured from the arrival date itself.

The shortcut was written down as the fix and reads entirely plausible;
it survives until you ask what `reference_instant` does with the horizon.

## A span passed to one consumer and defaulted at the next (2026-07-27)

Measured while reviewing the rule 140 sweep, in the replay engine since deleted. It computed
the operator's popularity window once, handed it to the fact builder to count
`distinct_watchers` over, and then called `score()` without it — so the count was built over
the operator's span and validated against the shipped 365-day default. The shape is what
survives, and rule 141 is where it lives. On a 730-day window over a 400-day mirror:

| policy weights | window passed | window defaulted |
| --- | --- | --- |
| all 100 on FEW_WATCHERS | 0.00/100, coverage 0.00 | 100.00/100, coverage 1.00 |
| shipped 70/20/10 | 1.68/100, coverage 0.80 | 21.68/100, coverage 1.00 |

**The direction is the counter-intuitive part, and it is worth stating twice: understating the
window charges MORE, not less.** A shorter window is *easier* for the mirror to cover, so the
shortfall check stops firing and the signal takes full pressure at full coverage on a count the
true window could not establish. The first draft of the fix wrote this backwards in a docstring
— reasoning that "a window the history cannot cover withdraws pressure" and then concluding that
understating it must therefore withdraw pressure too, when understating it *removes* the
withholding. A reviewer caught it by sweeping the value instead of reading the sentence.

Two general forms, both cheap to check and both missed by a green suite of 2,578 tests:

- **A fixture that pins the same value as the production default cannot prove the value was
  passed.** Every fixture on that path used 365, which is exactly `score()`'s default, so an
  omitted argument and a correct one produced identical output. Now rule 141.
- **One span with two consumers needs both pinned.** The first fix spied on `score` alone, which
  proved the repaired line and nothing about the sibling call four lines above it: hardcoding
  `facts_as_of(popularity_window_days=365)` left the entire suite green while withdrawing a
  PROTECT and adding 20 points of pressure at coverage 1.0.

## The lab's own fixture decides what a 440-vector sweep can see (2026-07-27)

Stating `Facts.days_since_added` as `Unknown` in `tests/_policy_lab.py` — the only honest
reading, since a vector records plays and not an arrival date — takes half the outcome matrix
for `watchers_all_time` out of reach, measured over all 440 vectors:

| rule | outcome |
| --- | --- |
| graded keep | `{40.0: 440}` — every vector at the maximum discount |
| protect `gte 1` | 415 matched, 25 blocked, **0** checked-and-did-not-fire |
| protect `lte 5` | 268 blocked, 172 checked-and-did-not-fire, **0** matched |

The two surviving arms are exactly the outcomes `fields._survives_more_history` lets through as
already earned. That is the guard working. But "a keep never raises a score" now holds against a
constant, and would stay green if the ramp broke outright.

**The tempting repair is fail-open and must be refused.** A floor on the age derived from the
oldest play (the mirror of `mirror_reach_days`, which is sound) makes `reach >= age` pass *more*
readily, so it would hand the lab counts the real scan refuses. Understating a reach blocks
more; understating an age blocks less. **A lower bound is only safe on the side of the
comparison where more evidence can only tighten the answer** — check which side you are on
before reusing the trick.

## Only `:dev` outlives a week in the registry (2026-07-28)

The install docs offered the short commit sha as a tag to pin ("if you would rather pin
one"). The org-level container cleanup rule keeps versions matching `dev` — plus `latest`,
always kept for container packages — and removes everything else older than **7 days**. A
short-sha tag matches neither keep rule.

Measured, with the obvious confound ruled out. CI run 1090 on 2026-07-17 shows `Push image:
success` with its "Warn when publishing is not configured" step *skipped*, so the registry
token was set and that build really was published. Paginating the packages API that same
week returned **749 container versions, oldest `created_at` 2026-07-21** — nothing from
07-15 through 07-20 survived. Published, then deleted: the rule working as configured.

Inventory at the time: `dev`, 213 short-sha tags, 72 `pr-<n>` tags, 464 untagged digests,
all inside the 7-day window. Neither `latest` nor `main` exists, because no release has been
cut — which sharpens it rather than softening it: **`dev` is currently the only tag in the
registry that outlives a week.**

Two things worth keeping. First, the failure surfaces at the worst possible moment: a pinned
short-sha keeps working until something *re-pulls* on a host with no layers cached, so a
running container is fine and a recovery or a host migration meets `manifest-unknown`.
Second, nothing was lost by removing the offer — identifying which build is running never
depended on the tag surviving, because the About page reads `REAPER_GIT_SHA` off the running
image.

Resolved by dropping the pinning offer from the README, `docker-compose.yml` and the Unraid
template rather than by widening the keep pattern, which would retain roughly one image per
commit with no bound. Whoever reconsiders that is changing registry policy, not docs.

**Where the rule lives now (2026-07-31).** The measurement above was taken against the
previous forge, whose registry applied this as an org-level setting. GitHub has no equivalent
setting, so the same policy is a scheduled job, `.github/workflows/registry-retention.yml`:
`:dev` and `:latest` are kept, everything else goes after a week, and **untagged** digests are
swept too, which the old rule left to accumulate. It ships as a dry run, because its selection
could not be checked against a registry that did not exist yet.

**The old rule had a hole, and it was inherited before anyone noticed.** A pull request's image
is published so a reviewer can pull and run the exact change. Aging it out on a timer means a
review left open past the cut-off loses the artifact it exists for, which costs more than the
disk it saves. The keep set therefore reads the tracker: every `pr-<n>` whose pull request is
still **open** is protected regardless of age, and only a closed PR's image ages out.

**A retention filter can fail by not filtering, which reads as success.** The obvious action for
this, `snok/container-retention-policy`, documents that its `!` and `*` operators do not work
with the automatic `GITHUB_TOKEN` — a temporal token cannot reach the list-packages endpoint
they need. An `image-tags: "!dev !latest"` written against that token does not error. It stops
excluding, so the two tags it names become deletion candidates and the job reports success
while doing the one thing it was added to prevent. That is why the keep set is computed in the
workflow instead of expressed as filters, and it is the same shape as rules 38/117 and 93: a
guard that reads as live while covering nothing.

## The Tautulli library cache lags Plex in BOTH directions (2026-07-29)

The Plex index a scan matches against is a union: Tautulli's cached `get_library_media_info`
listing (the spine, which alone gives rating_key / title / year / added_at cheaply) enriched
by a plexapi sweep. The lag we knew about was one-way — an item added to Plex since Tautulli's
last library refresh is missing from the listing — and the builder handles it by appending any
sweep row the spine did not list.

The other direction was never named, and it is the dangerous one. **Measured on a live
library**: a movie re-identified in Radarr, where the spine still lists the *retired* rating
key under the *arr's current title and year, `/library/metadata/<key>` on Plex itself returns
404, and the row Plex actually holds is absent from the spine entirely — it enters only
through the sweep. So one file, two index rows, one of which does not exist.

The shape of the harm follows from what a spine-only row carries. It has a title, a year and
an added-at, and no ids and no file name, because there was nothing to enrich it from. That is
exactly the shape the resolver's title+year tier binds on, and nothing else can reach it. Two
outcomes, and the second is worse than the one that was reported:

* **It vetoes a good bind.** The file name names the real row, the phantom's title names
  itself, the tiers disagree and the item abstains. Kept — but the operator is told Plex holds
  more than one copy of a file it holds one copy of.
* **It originates a bind.** With no id hit and a file name the real row does not carry (an
  ordinary *arr rename), title+year is the last tier standing. The item binds a rating key
  Plex 404s and reads as *matched*, so the fact layer takes its affirmative branch:
  `watchers_window.get(rating_key, 0)` is `Known(0)` — a measurement, not `Unknown` — and
  dormancy anchors on the phantom's stale added-at. Nobody can have watched a row that does
  not exist, so a live file collects maximum condemn pressure at full coverage from an item
  that is gone, with every watch-history protection reporting checked-and-did-not-fire.

Proportions on that library, for calibration rather than alarm: roughly two items in a thousand
abstained as ambiguous, 14 of them, of which 1 was the disagreement shape, and movies were about
three fifths of the spine. So the veto arm is rare; the originating arm is invisible from the
queue by construction, since a
phantom bind produces an ordinary-looking condemned row.

The negative result worth recording: **"absent from the sweep" is not by itself evidence of
anything.** A failed sweep and an unconfigured Plex both return an empty map, so the prune has
to be gated on the sweep having actually spoken, or a transient Plex error retires the whole
library. Dropping a row is the keep direction (the item resolves unmatched), so the change
cannot over-condemn — but it can blind a library, which is why a gap past 10% of the spine
degrades the snapshot instead: at that size the likelier story is a section the sweep never
walked, not a stale cache.
## A live region blanked inside 150 ms is never announced at all (2026-07-29)

The number that decides whether two `aria-live` messages both get announced is **Blink's 150 ms
serialization window, not one 16.67 ms rendering frame** — and the mistake is not conservative,
because the real window is nine times wider than the folklore one.

Measured on the policy page's Save with both halves dirty, which fires two mutations whose
`onSuccess` callbacks each `announce()`. Ten clean runs per engine, against a throwaway database,
with a `MutationObserver` on both regions plus frame boundaries and a wrapped `fetch`:

| | Chrome (Blink) | WebKit |
| --- | --- | --- |
| The two HTTP responses land apart | **0.1–0.2 ms** | 3–10 ms |
| First sentence's dwell in the DOM | median **13.7 ms** | median **4 ms** |
| Click → second write | 65–74 ms | 44–67 ms |
| Both writes inside one frame | 10/10 | 10/10, no frame boundary between them |

**This was never a network race.** The two responses arrive 0.1 ms apart; the ~14 ms gap is React's
commit scheduling and is the same every run. There is no deployment where the two saves are far
enough apart to be safe.

Three mechanism facts, from engine source, that a plausible-sounding argument got wrong:

- **Blink emits no live-region event itself** (`NOTREACHED() << "Event not expected from Blink"`).
  They are generated in the browser process by `ui::AXEventGenerator`, which **diffs successive
  accessibility trees** — so a region going `"" → text → ""` inside one batch is a net-zero diff
  and produces nothing. The AT's read-at-drain behavior never comes into play; there is nothing
  to drain.
- **`CommitAXUpdates` is rate-limited by `kDelayForDeferredUpdatesAfterPageLoad = 150`.** The
  per-frame hook is real but early-returns until the delay elapses, so a batch spans many frames.
  A click forces an immediate serialization and starts that clock; both writes complete by 74 ms.
- **WebKit is the opposite, and the common folklore about it is stale.** Its
  `m_liveRegionChangedPostTimer` is `startOneShot(0_s)` — it was `20_ms` until ~2022. Writes in
  separate tasks get separate flushes, so WebKit does *not* coalesce them away.

⇒ **Chrome + NVDA/JAWS reliably loses the first message; Safari + VoiceOver is unverifiable from
public source** (the shipping notification carries a target, not text, so it reduces to VoiceOver's
read-back timing against a 4 ms dwell). One stack proven lost is enough. `announce.tsx` queues
sentences 400 ms apart, measured at a real 386–389 ms gap, which clears 150 ms nearly three times
over. **Reproducing this needs no screen reader and no device**: a `MutationObserver` on
`[role=status][aria-live=polite]`, `performance.now()` at the click, and one Save — and the number
to compare against is 150 ms.

## The frontend suite's "expensive role query" is jsdom's first `getComputedStyle` (2026-07-29)

#149, #228 and #234 read a family of intermittent CI reds as *query cost*: a first
`await findByRole(..., { name })` re-computing accessible names across the whole tree on every
50 ms poll, sharing one 1000 ms `findBy` budget with the read that makes the control exist.
#236 swept ~50 more sites on that reading. **The reading is wrong in a way that inverted the
inventory.** Timed with `findByRole` re-implemented as the `waitFor(getByRole)` it
already is (`query-helpers.js:makeFindQuery`), so every matcher evaluation could be counted and
timed separately from the waiting; 8 full-suite runs per arm.

| | mean | worst |
| --- | --- | --- |
| First `*ByRole` await in a file — time inside the matcher | **61.9 ms** | 253.8 ms |
| Every later `role`+`name` await — same | **5.8 ms** | 54.9 ms |

The cliff is at the *first* one, and it is not about names, tree size, or polling. On a
**four-element** DOM: a bare `getAllByRole("button")` with no name matcher costs **60.1 ms** cold
and **0.6 ms** warm; one `window.getComputedStyle(el)` beforehand drops that cold query to
**7.8 ms**. So the cost is **jsdom building its CSSOM on the first `getComputedStyle` call**, which
every `*ByRole` query makes because `queryAllByRole` filters inaccessible elements by computed
visibility. It is paid once per test file (vitest isolates the module registry and the jsdom per
file) by whichever role query runs first — and when that query is a test's first `await`, it is
spent inside `findBy`'s fixed 1000 ms budget.

Two things the mechanism ruled out, both of which had looked like the answer:

- **The accessible-name matcher is nearly free.** Warm, `getAllByRole(role)` is 0.9 ms and
  `getAllByRole(role, { name })` is 1.9 ms, against trees up to the whole app shell.
- **A missed poll does *not* build the expensive "here are the accessible roles" diagnostic.**
  `role.js:getMissingError` does enumerate every role with its name — 20.5 ms mean, 248.8 ms worst
  — but `wait-for.js:124` wraps each poll in `runWithExpensiveErrorDiagnosticsDisabled`, so a
  `findBy` never pays it. Measuring that path outside `waitFor` is how it gets mistaken for a
  per-poll cost.

⇒ `src/test/setup.ts` pays the one `getComputedStyle` per file, where nothing is timing it. Summed
across the ten files whose first await is a role query, that first wait goes **723 ms → 273 ms**;
seven of them gain 43–85 ms each, and the three that gain nothing are the ones already warmed by
something earlier in the file. Total suite wall clock is unchanged (18.07 s → 17.84 s over three
runs): the cost is relocated, not removed.

**What this does not fix, and the honest limit of the whole family.** 52 ms of a 1000 ms budget
cannot by itself explain #228's timeout — its dumped DOM showed the two reads simply had not
landed. The dominant term is read and scheduler latency under runner load, and it survives:
`AppStaleRead.test.tsx:178`'s first wait still has a 258 ms worst case warm. Converting a site
from `findByRole` to `findByText` + a synchronous `getByRole` does not make the wait cheaper
either — it **relocates** the same work outside the timed window. Measured both orderings on the
app shell: expensive-first paid 105.5 ms in the wait; cheap-first paid 49.3 ms in the wait plus
52.4 ms in the synchronous take, for the same 101.6 ms of work. Which is why the per-site sweep is
the wrong shape for this: it helps at most one site per file, the ~40 others are already warm at
~6 ms, and a new test written in the old shape re-introduces it.

## Mutation testing the two policy repair shims (2026-07-29)

Sixty mutants over `policy_migrations.rebalance` and `policy_migrations.recover_rating_rules` — operator flips, ±1
constants, dropped `not`s, `and`/`or` swaps, and ten single-statement deletions — run against
`tests/test_policy.py` plus `tests/test_profiles.py`. Baseline 2.6 s, whole sweep about three
minutes. **48 killed, 10 survived, 2 unparseable.** Scoping by *function* rather than by file
is what makes that a coffee break: the same operators over all of `policy.py` would be well
over a thousand mutants.

The ten survivors were three different things, and the count alone said none of it:

| survivors | what it was |
| --- | --- |
| 1 | **a test that could not fail** |
| 6 | correct behavior with nothing defending it, all one direction |
| 2 | equivalent mutants — the answer cannot change |
| 1 | the same test defect, reached by a second mutant |

**The real defect was a fixture that misrepresented its own population.**
`_legacy_rating_body()` dumped the *current* `schema_version`, while every body the shim
actually repairs carries the previous one — production's own docstring says so. So
`body["schema_version"] = SCHEMA_VERSION` was a no-op for the fixture, and deleting that line
from production left the suite green. The assertion read as a proof of the restamp and proved
nothing (rule 141, found mechanically rather than by argument).

**The six undefended branches all failed the same way, which is the part worth keeping.** Each
one, mutated, made the shim *refuse a legal repair*: `floor` at 1, `floor` at 100, `min_votes`
at 1, and a gate row with no `enabled` key. The existing tests covered the outside of the range
(0, 101, zero votes) and none of the inclusive edges. Refusing any of them leaves the
operator's rating bar empty, `RatingFloorGate` abstaining on every item, and a protection they
can still see configured holding nothing — on a healthy, executable snapshot. That is rule 105's
worst outcome, and no mutant pointed the other way. **Direction is the finding; the score is
not.** A survivor list sorted by "does this widen what gets deleted" is readable, and one
sorted by mutation score is not.

**Defense in depth reads as a test gap.** `total <= 0` in `rebalance` survives as `total == 0`
because the only input where they differ, a negative total, produces weights
`PolicyBody.model_validate` refuses anyway — both paths return `None`. The guard is redundant
with the validation behind it and should stay; the equivalent mutant is the architecture
working, reported in the same column as a bug. Same for `strict=True` in the `zip`, whose
lengths cannot differ by construction. Neither earns a test (rule 118: a test that cannot
discriminate must not read as a proof).

**It generalizes, and the second target says the suite is mostly fine.** Pointed at
`MinDormancyGate.evaluate` (8 mutants, five test files that could kill them), exactly one
survived: `dormant.value < floor` reads the same as `<=` because no test drives a dormancy at
the floor — only 400 days against a 1,095-day floor, and 1,200 and 1,500 well past it. The
other seven died, including all three inverted comparisons. **Which way the one survivor
points matters**: `<=` protects an item dormant exactly at the floor, so the undefended
direction keeps a file rather than deleting one. A real gap, and a low-severity one, and the
distinction is only visible because the direction was asked about.

**A bare function name is not a unique target.** Scoping the runner by name silently kept the
last match, and `engine/gates.py` holds nine `evaluate` methods — so the first attempt at a
second zone would have mutated whichever gate came last and reported it as the one asked for,
which reads exactly like a real answer. Methods are named `Class.method` now, and an ambiguous
name raises instead of binding, the way the rest of this codebase refuses to guess on ambiguity.

**A probe corpus built from the test's own fixture inherits its blind spots.** The harness
classifies each survivor by re-running both functions over a fixed corpus and diffing against
baseline. It labeled the schema-stamp survivor "no observable change" — wrong, and wrong for
exactly the reason the test was wrong, because the corpus imported `_legacy_rating_body` and so
carried the same current-version stamp. The mutant surfaced the defect; the classifier missed
it. Reuse a fixture and you reuse whatever it cannot see, so a differential probe needs at
least one case built from the *spec* rather than from the existing helper.

## Mutation testing the policy save boundary (2026-07-29)

Zone 2: the twelve validators that decide what an operator is allowed to store, 78 mutants
against five test files. **64 killed, 14 survived**, and unlike zone 1 the survivors were
mostly real — twelve of the fourteen earned a test.

**The dangerous direction inverts between the two zones, which is the whole reason to record
direction rather than a count.** A repair shim fails badly by *refusing* a legal repair. A
validator fails badly by *accepting* a policy it should refuse, because the save boundary is
the last place anything can say no. Seven survivors loosened the boundary, five tightened it:

| survivors | validator | what the mutant let through |
| --- | --- | --- |
| 5 | the three `floor >= saturate_at` copies | a ramp with no width, and a ramp running backwards |
| 3 | `RatingRuleSpec._vote_floor_matches_the_source` | a vote floor on a source that has no votes |
| 4 | `ProfileSettings._run_cap_within_rolling_cap` | equality refused on all three cap relations |
| 1 | `_weights_total_one_hundred` | "Give out the other **-1**" |
| 1 | `_weights_total_one_hundred` | nothing — genuinely unreachable |

**One rule, three copies, and the two newer ones were the weak ones.** `floor >= saturate_at`
is rule 72's shape: `SignalSetting` had a test, and the `GradedCondemnSpec` and `GradedKeepSpec`
copies had none. Worse than the missing equality case, `>=` mutated to `==` survived on both,
which means **an inverted ramp could be saved** — floor above saturate_at, the rule running
backwards — and only the original copy refused it. The sibling sweep that rule 72 asks for was
never done on the tests, only on the code.

**A probe only sees what it records, and this is the second zone to prove it.** The first pass
called the weights-remedy mutant "no observable change," because the probe recorded
accepted-or-rejected and nothing else. But a validator's job is *both* halves: refuse the
policy, and say what to change. The mutant left the refusal intact and turned the remedy into
"Give out the other -1" — a negative number in operator copy, rule 21's floor. Recording the
message alongside the verdict caught it. Zone 1's blind spot was an inherited fixture; this one
was a missing dimension. Same lesson twice: **the probe has to record the thing the zone is
responsible for**, and "no observable change" should be read as a question, not an answer.

**Twelve validators, but only ten produce mutants.** `ConditionSpec` and `BooleanCondemnSpec`
hand the whole decision to the `fields` registry, so there is nothing in them to flip. They are
still named in the zone: a zone calling itself "the save boundary" while quietly omitting two of
its members is the flag-shaped coverage claim rule 145 is about.

**And the static read that fed this was wrong about one thing, which is why it was filed as a
question.** A hand sweep predicted `GateSetting._protective_floors` (`threshold < 1`,
`threshold < 5`) was unpinned. All fifteen of its mutants died, both boundary constants
included. Reading tests to guess what they pin is unreliable in both directions; running the
mutants is not.

## A mutation run is worth parallelizing, and the copy is nearly free (2026-07-29)

The first runner wrote each mutant into the real source file and restored it in a `finally`,
which forces the run sequential and leaves the tree modified if it is interrupted. Giving each
worker its own copy fixes both, and the copy costs about as much as one mutant:

| step | cost |
| --- | --- |
| `cp -Rc` of `src`, `tests`, `alembic`, `frontend/src` and the manifests | **0.09 s** (APFS clone) |
| `uv sync --all-extras` against a warm cache | **0.65 s** |
| eight workers, end to end | **3.9 s** |

Measured, same mutant counts and identical verdicts before and after:

| zone | sequential | 8 workers | speedup |
| --- | --- | --- | --- |
| repair shims (60) | ~2 min | 52 s | 2.3x |
| save boundary (78) | ~6 min | 48 s | 7.5x |
| gates (88) | ~24 min | 3 min | **7.9x** |

**`uv sync` inside the copy is what makes the isolation real**, not the copy itself: it installs
the project editable against the COPY's `src`, so `import reaper` there cannot reach back to the
original. Copying alone would leave every worker importing the same unmutated module through the
main venv's editable install, and every mutant would survive -- a green run that means nothing.

**The cost is the test set, not the mutant count.** The gates zone has 47% more mutants than the
shims zone and took twelve times as long sequentially, purely because it runs eight whole test
files where the shims zone runs two test classes. Naming classes instead of files is the other
lever, and the one to reach for second: being generous about what may kill a mutant is what
makes a survivor trustworthy, and a survivor that only survived because the killing test was
not in the list is a false finding.

**Two things the copy has to carry that are not obvious.** `README.md`, because
`[project] readme` points at it and the build backend reads it during `uv sync`; and
`frontend/src`, because some backend tests reconcile a Python vocabulary against the TSX that
renders it (`test_review_chips.py` opens `WhyPanel.tsx`). Listed as a nested path so that
copying it does not drag `node_modules` into every worker. Both were found by the baseline
check refusing to start, which is the cheapest possible place to find them.

## Mutation testing the gates (2026-07-29)

Zone 3: twelve gate functions, 88 mutants, eight test files that could kill them. **69 killed,
19 survived**, and the survivors sorted by one predicate -- *does this result still hold the
file?* A gate holds it by protecting outright or by blocking because it could not check, so a
survivor flipping holds to lets-go has withdrawn a protection library-wide.

**Two withdrew a protection.** Both are comparisons whose covering cases all sat on one side:

- `RatingFloorGate` compares a rating against `rule.floor / 10`, converting the policy's
  tenths to the 0-10 scale ratings arrive on. **Nothing drove a rating at the bar or a tenth
  under it**, so the divisor was free to move: at `/ 9` a configured 7.5 silently becomes 8.33
  and every title between them loses a protection the operator still sees configured; at `/ 11`
  it becomes 6.8 and keeps titles nobody asked to keep. A unit conversion is only pinned by a
  case that lands differently under it.
- `ServerPopularityGate` protects on `count >= floor`, and every case sat AT the floor or under
  it, so `>=` was free to become `==`. The protection would then hold for a title watched by
  exactly three people and withdraw from one watched by five hundred -- un-protecting the
  most-watched titles on the server while the suite stayed green. **A floor makes the "well
  over" case easy to forget, and it is the one that catches this.**

**The rest were operator copy, and one of them was a live defect rather than a gap.** Three
copies of the vote clause -- `RatingRule.describe_bar`, `Rating.describe`,
`Rating.describe_for_user` -- all rendered "from 1 votes", because every case that drove them
used a count in the thousands. A vote floor of 1 is a legal policy and a title with one vote is
ordinary, so all three were reachable. They derive from `ratings.describe_votes` now (rule 104).
The popularity gate pluralizes on two separate lines and neither was pinned at a count of one,
and deleting the not-kept line's assignment outright raises, which nothing noticed either.

**Fixing a copy defect can delete the mutants instead of defending them.** Collapsing
`describe_bar`'s `if min_votes > 0` branch into the shared helper took the zone from 88 mutants
to 81: seven mutable tokens stopped existing. Worth knowing when reading a survivor count
across runs -- the denominator moves, so the ratio is not comparable and the list is.

**And the honest gap it opened.** `describe_votes` now carries 4 mutants in `ratings.py`, which
no zone names. The tests written here kill them, but nothing *checks* that any more, which is
exactly the flag-shaped coverage claim rule 145 is about. `Rating.meets` -- the function that
decides whether a bar clears at all -- is in the same unzoned module.

## The rating bar's own arithmetic: 12 survivors, 10 of them the inclusive edge (2026-07-29)

`ratings.py` was the gap the section above named, and zoning it (75 mutants over 12 functions,
eight test files) left **12 survivors, now 2**. It is the layer *under* the bar: `RatingFloorGate`
holds a file only where `Rating.meets` says a bar cleared, and only over ratings the two parsers
managed to interpret at all — so the direction inverts once more. A validator fails by accepting;
a repair shim fails by refusing; here a mutant fails by making a readable rating **unreadable**,
which does not refuse anything, it withdraws a protection and hands the file to the reap list.

**Ten of the twelve were one shape: an inclusive boundary probed only from the outside.** The
third zone in a row to say this, so it is the finding rather than an anecdote — the suite reaches
for a value comfortably past a bound and almost never for the bound itself.

| the edge | mutated | what stopped being protected |
| --- | --- | --- |
| `min_votes > 0` → `>= 0` | the vote check applies when no vote floor was asked for | **every Plex-sourced rating in the library at once** — `from_plex` returns `votes=None` for all of them |
| `min_votes > 0` → `> 1` | a vote floor of exactly 1 stops applying | the other way: a 9.5 from one vote protects, which keeps junk |
| `votes < min_votes` → `<=` | a count exactly at the floor is refused | the titles an operator asking for 1,000 votes meant to include |
| `number > 10` → `>= 10` (Plex) | a percentage source at exactly 10 is rescaled to 1.0 | a perfect 100% Tomatometer, filed as 10% and let go |
| `0.0 <= n <= 10.0`, three of its four ends | 0.0 and 10.0 fall outside the scale | the best- and worst-rated titles, dropped to "no rating" |

The value floor itself (`value >= floor`) was already defended, which is the useful contrast: the
vote floor is the half nobody drove, because it is the half that is usually a four-figure number.

**"No change on the probe corpus" was a missing case twice more, in the same run.** Both were the
`from_radarr` vote conversion, and neither is an equivalent:

- `raw_votes and source not in _PERCENTAGE_SOURCES` → `or` changes nothing *unless* a percentage
  source arrives carrying a real vote count. The corpus had Rotten Tomatoes with no votes and the
  suite had `votes: 0` (which is what Radarr actually sends), and a zero cannot tell "the count
  was dropped" from "the count was read and was zero."
- Deleting `votes = None` from the malformed-count recovery changes nothing *unless* an unreadable
  count follows a readable one — because `votes` is unassigned that iteration, so the recovery
  silently attributes the **previous source's** count to this rating. Nothing drove that `except`
  at all, and its comment cites rule 32.

That is now three runs where a survivor reported "no observable change" and the corpus was at
fault, against two where the mutant was genuinely equivalent. **The prior should be a missing
case, not an equivalence.**

**One survivor was left deliberately, and filed rather than pinned.** Mutating the rescale's
boundary constant from `10` to `11` only changes values in `(10, 11]`: `10.1` reads as 10.1% and
becomes `1.01` today, and becomes dropped under the mutant. Both readings withdraw the
protection, they differ in what the panel tells the operator, and which is right depends on what
real Plex agents serve in that band — which nobody has measured. A test either way would read as
a proof that the reading is correct (rule 118), so it went to the tracker as a question instead.
The remaining survivor is a genuine equivalent: `split("://", 1)[0]` is `split("://", 2)[0]`.

## The accessibility guard: axe-core over a lint plugin (measured 2026-07-29)

Two candidates for a standing accessibility gate, compared before choosing.

**`eslint-plugin-jsx-a11y` stays out**, re-measured: unreleased since October 2024, capped at
eslint 9, 116 lockfile entries, three advisories, and **15 of its 36 findings here were false**.
It reads source text, so it cannot see through a wrapper component, which is where this
project's accessibility bugs actually lived.

**`axe-core` went in instead** — one lockfile entry, dev-only, absent from every bundle chunk —
because it reads the tree the browser *built*. `src/test/a11y.ts` fails on what it finds **and on
what axe could not decide**: jsdom files an unsettled rule under `incomplete`, which is where
`aria-hidden-focus` lands, so asserting on violations alone passed silently over an invariant
held by hand at 113 sites.

⇒ Prefer a guard that reads the rendered tree over one that reads source text, and fail the run
on `incomplete` as well as on `violations` — an undecided rule is not a passing one.

**Where a source-text scan still earns its place**: a control no fixture mounts. Measured, an
`<input>` scan ran at 94% false positives and was dropped; a `<button>`'s accessible name is
ambiguous-but-present, which no scanner can judge. A brace-aware scan over `<select>` elements
was kept, because that population is small, enumerable, and pinned by count.

## A Plex rating key moves, and Tautulli's history does not follow it (2026-07-30)

Reaper reads its watch mirror by the rating key an item carries *now*. Whether that is safe
turns on two questions, both answered here rather than assumed.

- **Tautulli never remaps a historical rating key on its own.** Across the whole source tree,
  the only code that rewrites one is `datafactory.update_metadata` /
  `update_metadata_details`, and it is reachable from exactly two places: the "Fix Metadata"
  button on the item info page, and the API command of the same name. No scheduled job calls
  it (the eleven are update check, DB optimize, two backups, server URLs, Plex update check,
  refresh users, refresh libraries, server response, websocket ping, token expiry), and
  neither does any websocket handler or library refresh. Tautulli's own maintainer ships a
  **standalone script** for the repair, which is the strongest evidence it is not automatic.
- **Playing the item does not fix it either.** `activity_handler.process` compares a guid, but
  `last_guid` comes from the *temp session* row and the re-read is gated to live TV: it decides
  "same item, keep this session" versus "force-stop and start a new one".
  `activity_processor.write_session_history` writes the new play under the *current* key and
  consults guid only to set `reference_id` for grouping, looking back **one day** per user, so
  it will not even group a re-added item's new plays with its pre-removal ones.

⇒ A re-added file's earlier plays stay filed under the old key indefinitely, and re-syncing
cannot help: the mirror is faithfully copying rows that are themselves stale.

**Why the fix is not "key the mirror on the guid".** Measured against a live library: live movie
items outnumber their distinct guids, and **about one guid in twenty-five sits on more than one
live rating key** — the same title held twice, HD and [redacted]. A guid does not identify one item, so a
guid-keyed mirror would pool two separate candidates' plays. `media_key` does separate them:
of 21 titles present twice, all 21 carry two distinct `media_key`s *and* two distinct rating
keys. History rows do carry a guid (100% of a 5,000-row window, from
`session_history_metadata.guid`), and they are almost all `plex://`, so the ids cannot be
parsed out of them without Plex's side anyway.

**Measured against a live library, and it bites.** The instrument: for each movie guid in a
sample of history rows, ask whether the rating key Plex holds for that guid *right now* is among
the keys those plays were recorded under. If it is not, every one of those plays is invisible to
Reaper. It is robust to duplicate copies, because the comparison is against **every** live key
the guid has, so an HD play still matches when the 4K listing is the other key.

Sampled three pages of 500 at each decile of a six-figure history, newest to oldest:

| age of play (0% = newest) | movies still in Plex | current key not among their own plays |
| --- | --- | --- |
| 0–30% | 654 | **0** |
| 40% | 133 | 2 (1.5%) |
| 50% | 131 | 2 (1.5%) |
| 60% | 134 | 6 (4.5%) |
| 80–90% | 6 | 3 (small denominator: the oldest pages are nearly all episodes) |

Zero among recent plays, rising with age. That gradient is the signature: a key churns when a
file leaves the library and comes back, so exposure accumulates, and a play recorded last week
has had no time to be orphaned. Roughly one mid-history movie in twenty is affected here, on a
server whose operator has never run a deletion through Reaper at all.

**The instrument that found nothing, and why it was the wrong one.** Across 31 stored snapshots
no `media_key` changed its `plex_rating_key` — zero, across every movie and every season the
library holds. Read as reassurance that would have been wrong: those snapshots span **6.7 days**
and every `reap_run` in
that window is still `PLANNED`, so nothing was deleted and no file came back. The window
contained none of the trigger. Reaper's own scan history can only see churn it was running
across; Tautulli's history reaches back years, which is why it could answer.

⇒ Hence the shape of the guard: not a remap, which would have to trust a key Plex may have
reissued to something else, but the one invariant that needs no key at all. All-time watch
evidence cannot fall, so a count that drops to zero (or a last play that moves earlier) is a
transition no library can perform, and the honest answer is `Unknown` rather than a measured
zero. A never-watched item reads zero on every scan, so it never trips it.

## A missing arrival date is a reachable branch nobody reaches (2026-07-30)

Two scan lanes thawed one derived value two ways: the movie lane took `Unknown` dormancy the
moment Plex reported no `added_at`, while the season lane measured from the last play and
judged the season (#272, #257). Joining them means the movie lane judges items it used to
keep, which is the condemn direction, so the question was how many items that moves.

**Measured before changing anything, read-only against a live library.** Every stored snapshot,
counting rows whose dormancy came back unreadable and splitting them by cause:

| population | rows | unreadable dormancy | of those, missing `added_at` |
| --- | --- | --- | --- |
| movies, 41 snapshots | six figures | 977 | **0** |
| seasons, 41 snapshots | five figures | 372 | **0** |

⇒ **The arm is reachable and never reached.** Every unreadable dormancy across both populations,
hundreds of thousands of rows between them, came from an item with no Plex rating key at all (an
ambiguous or unmatched title), not from a
matched item missing its date — a correlation that is exact: zero rows held an
unreadable dormancy *and* a clean Plex bind. The `scan.no_added_at` warning, which exists to
name this state, has never fired in the log either.

So the consolidation moved no observed verdict, and the branch it removed was costing what a
branch costs — two lanes to keep in agreement, and a why-panel telling the operator dormancy
could not be measured with a play for that item in scope.

**The transferable part is the instrument, not the zero.** The count came from
`candidate.facts_json` and `explanation_json` on stored snapshots rather than from a fresh scan,
so a question about how often a branch fires is answerable in seconds over months of history
without touching the live servers. `facts_json` is sparse on older rows (~58% of movie rows
carry it), which is what made the `explanation_json` cross-check worth running: it covers every
row, and agreeing with the sparse measurement is what rules out a sampling artifact.

## A keystroke is a render, and twenty-six of them is what times out a test (2026-07-30)

`SettingsNav.test.tsx > stops warning once the draft is discarded` failed CI at vitest's 5000 ms
default. No bug under it, and no hang: the same commit was green an hour earlier, and the file's
other 18 tests were untouched.

**The failing run carried its own control.** Two tests in that family differ by one statement —
one types a draft into the Application URL box before clicking away, the other clicks away with
the box empty. Same fixture, same panel, same two renders:

| `SettingsNav.test.tsx`, one CI run | |
| --- | --- |
| `switches straight through when there is nothing to lose` (types nothing) | 824 ms |
| `holds the switch and says what leaving would cost` (types 26 chars) | 4713 ms |
| `stops warning once the draft is discarded` (types 26 chars) | **5063 ms, failed** |

So 3889 ms of a 4713 ms test is 26 keystrokes, about 150 ms each on a loaded runner.
`userEvent.type(box, s)` dispatches one keystroke per character and each one re-renders the panel
around the box, so the cost is per character and the test that types the most is the one that goes
over. Unloaded, the same family runs 70–400 ms, which is where this hides: **the amplification is
invisible until the machine is busy, and then it is multiplied by the same factor as everything
else.**

**The fix is the tail, not the median.** `clear` + `paste` produces one input event where `type`
produced 26. Five runs of the family, 25 samples each arm:

| filling one 26-char box | median | worst |
| --- | --- | --- |
| `userEvent.type` | 116 ms | **405 ms** |
| `clear` + `paste` (`src/test/forms.ts`) | 37 ms | **39 ms** |

The medians are a 3x win and the worst cases are a 10x one, because one event has almost no
spread to have. A test goes red on its worst case, so the number that decides a run stops moving.
Swept over 47 sites in 7 files: the suite's slowest test went 1742 ms → 675 ms, and summed test
time 78.8 s → 54.7 s.

**A silent paste is the trap this opens, so the helper closes it.** `paste` lands on
`document.activeElement` and does nothing at all if the box is disabled or unfocused, exactly the
way a click on a disabled control does (rule 137) — and several converted tests assert that some
control stays **disabled**, which an empty box satisfies just as well as the half-typed one they
mean. So `fill` asserts the box really holds the value. Removing the paste from the helper fails
34 tests across 4 files; without that line it failed none of them.

**Where it does not apply, measured rather than assumed.** 76 `.type(` sites were classified;
13 must stay keystrokes — 9 append onto text already in the box (`clear` would destroy the
half-typed hex or the sent webhook the test is about) and 4 carry user-event's `{backspace}`
syntax, which a paste delivers as literal text. None exceeds 20 characters, so none of them is a
timeout contributor anyway.

**What this does not fix.** It is a second term in the family
[the `getComputedStyle` entry](#the-frontend-suites-expensive-role-query-is-jsdoms-first-getcomputedstyle-2026-07-29)
above ends on, not a replacement for that entry's conclusion: the dominant term is still runner
latency, and it survives. The two axe audits type nothing and cannot be helped this way — the
General panel's ran 3988 ms on that runner, a fifth under the old ceiling with nothing wrong with
it, which is why `testTimeout` moved to 15000 ms in the same change. Of that audit, only ~139 ms
is the 418-option time-zone `<select>` (axe over 418 options is 177 ms against 38 ms over one), so
trimming the tree would not have bought the margin either.

## The operator set could not express a guard deletion, so a clean zone meant less than it read (2026-07-30)

Three zones had run clean, and the reason was partly that no operator in the set could write the
edit that matters most here. Token swaps need a token with an opposite and the statement operator
walks assignments only, so a guard on `isinstance` or `in` generated **nothing at all** — 23 of
the 78 `if`s inside the four zones' functions produced zero mutants. The report had no third
state for "nothing was tried," so a function holding no mutable token printed exactly like one
whose mutants all died.

**The fix is one more byte-precise splice**: rewrite `if <test>:` as `if (<test>) and False:`.
Two details are load-bearing. Leaving the test *evaluated* keeps a walrus binding alive
(`if blocked := _blocked(...)`), so the mutant fails on the missing branch rather than on a
`NameError` two lines down — it reports the guard as undefended for the right reason. And the
parentheses are not cosmetic: `if a or b:` spliced without them binds as `a or (b and False)`,
still live down the `a` arm, which would report a killed guard as surviving.

**It covers 77 of the 78, and the runner now says so.** A test wrapped across lines is the one
spelling the splice rejects (rule 147); there is exactly one, in `ratings.py`. Printing the count
is the point — "27 guards mutated" beside a silent 1 skipped reads as a complete sweep.

**The gates zone, re-run: 81 mutants to 108, and 102 killed / 6 survived.** Five of the six
survivors were reachable only by the new operator. Sorted by the predicate this zone has used
since its first run — *does the result still hold the file?*

| survivor | what deleting the guard did | direction |
| --- | --- | --- |
| `CuratedListGate` PROTECT branch | "on a protected list" becomes "Not on any protected list" | **withdraws a protection** |
| `RatingFloorGate` no-rules-configured | the whole sentence becomes `"."` | operator copy |
| `RatingFloorGate` partial-clear branch | the "cleared one bar, not the others" sentence is lost | operator copy |
| `RatingRule.describe_bar` percentage arm | a percentage bar renders in the other word order | operator copy |
| `MinDormancyGate` non-`Known` arm | raises `AttributeError` instead of protecting | defense in depth |
| `progress_is_establishable` `<= 0` → `== 0` | nothing, absent a negative hold | equivalent |

**The one that matters is the first, and it is the shape the zone was built to find.** Nothing
in 3,000 tests drove a curated-list *hit*, so the branch that fires the protection was free to
stop firing — a protection withdrawn library-wide behind a green suite. The three copy survivors
do not change any file's fate; they degrade the sentence explaining it, which is rule 21's floor
rather than rule 2's. Recording direction is what keeps those two findings in different rows.

**A zero now reads as a zero.** The per-function table is keyed on the functions the *zone*
declares rather than on the mutants generated, so `evaluate_all` prints `0 mutants -- nothing was
tried here` instead of dropping out of the report and reading as defended. That is rule 145's
flag-shaped coverage claim, and it was living in the tool written to catch it.

**A hand-listed test corpus errs toward false survivors.** The gates zone omitted
`test_policy_permutations.py`, the only file that kills a deletion of either `RatingFloorGate`
fail-closed guard: the zone called both survivors while the full suite failed on both. A false
survivor is expensive out of proportion to its count, because it costs the reader the trust the
real ones need.

**What the fail-closed guards themselves cost to defend: one test.** All seven `gates._blocked`
call sites now die, against five that survived the entire suite before. The property that was
supposed to cover them reads `for result in evaluation.results: if result.blocked:`, so deleting
a guard yields *fewer* blocked results and the conditional over the smaller set still holds —
green, while the gate stopped failing closed. Rule 118's case exactly: where the sweep upstream
cannot discriminate a branch, drive the interlock directly. The table of guards is reconciled
against the source by AST, so a gate that gains a guard fails until it gains a case.

**The rule 72 sweep off that fix came back clean, which is worth recording as a negative.** The
seven other places in `src/` that construct a result with `blocked=True` were each flipped to
`False` against the full suite: six failed it. The seventh,
`condemned.reap_override_verdict_decoded`'s unreadable-explanation arm, survives and is an
**equivalent mutant** — the same call passes `safety_protected=True`, which returns `protect`
whatever `blocked` says. It is defense in depth and earns no test (rule 118: a test that cannot
discriminate must not read as a proof). Third time a survivor here has turned out to be a
redundant guard rather than a gap, and the check that settles it costs one call to
`decide_verdict` with both values.

## A number input's `step` is advice to the spinner, not a gate on the value (2026-07-30)

`<input type="number">` with no `step` is a whole-number box by definition — HTML defaults the
attribute to 1 — and seven policy boxes were written that way over fields declared `int`. The
open question in #296 was whether the browser therefore refused a typed `1.5` before it could
reach React, which would have made the issue a non-defect. **It does not.**

Driven in headless Chrome 150 against three boxes, typing `1.5` into each:

| box | `input` event value | `.value` | `stepMismatch` |
| --- | --- | --- | --- |
| no `step` | `"1.5"` | `"1.5"` | true |
| `step="1"` | `"1.5"` | `"1.5"` | true |
| `step="any"` | `"1.5"` | `"1.5"` | false |

So the value sanitization algorithm keeps `1.5` — it is a valid floating-point number, which is
all that algorithm asks — and the step is checked separately, by **constraint validation**, which
runs at form submission and colors `:invalid`. A control that never submits a form and reads
`e.target.value` sees the fraction either way.

**The consequence for a fix: adding `step={1}` would have changed nothing.** The middle row is
what running all three is for — the obvious remedy is the one measurement that rules it
out, and without the row it stays plausible. The coercion has to be in the component, which is
where it now is (`useTypedNumber`'s `decimals`). Reaper reaches for `stepMismatch` nowhere, so
none of this is reachable through validation either.

## Driving a branch is not discriminating it: two of four undefended arms had a test on them (2026-07-30)

Closing the four rows above cost six cases, and the surprise was where they had to go. The
expected shape held for two of them: nothing anywhere drove a curated-list *hit* or a rating
policy with no bars set, so the arm was undefended because the state was never built. The other
two were not that. **`test_all_of_matching_needs_every_bar` sat directly on the partial-clear
arm** — an ALL policy, one bar cleared, one missed, exactly the state — and asserted
`outcome == ABSTAIN`, which *both* arms return. The case reached the branch, ran it, and could
not tell it from its neighbor. Same for the percentage bar: a parametrized sweep four cases wide
pinned `describe_bar`, all four on IMDb, so the arm that reorders the words for a percentage
source was exercised by nothing.

So coverage measured as "a test reaches this line" is the wrong instrument twice over, and the
second miss is the quiet one. A line nothing reaches at least looks empty in a report. A line
reached by a test that asserts the value both arms share looks *covered*, and reads to the next
author as settled. **The discriminating question is not which branch the case takes, it is
whether the assertion changes when the branch does** — which is what a mutant asks and a
coverage percentage cannot.

The cheap consequence: when a case lands on a branch whose arms agree on the outcome, assert
the thing they disagree about. Three of the four here disagree only in the *sentence*, so the
assertion is an exact string rather than an enum. That reads as brittle and is the opposite: the
string is the whole difference between the two arms, and rule 21 already says the operator has
to be able to read it.

## What a frozen snapshot actually costs on disk (2026-07-30)

Freezing every item's evidence before scoring is what makes a verdict honest, and the price of it
had never been measured. It is **about 4 KB per item per scan**, and it is paid on every item,
not only the condemned ones: a scan writes one row per movie and per season whatever the verdict.
So the whole library's storage cost is charged again on every scan, and with nothing deleting old
scans the database grew without limit — measured at roughly **24 MB per scan for a
six-thousand-item library**, which is ~8 GB a year scanning nightly and ~200 GB scanning hourly.

**Two JSON columns are two thirds of it**, in equal halves: the frozen `facts_json` and the
why-panel's `explanation_json`, each averaging about 1.5 KB. The rest is small, with one
avoidable member — the *arr `overview` blurb is copied verbatim into every generation, ~275 B
per item per scan for a string that essentially never changes.

Three shapes worth knowing before anyone optimizes this:

- **The redundancy is real but not total.** Distinct `(item, facts_json)` pairs were about a
  third of all rows, so evidence genuinely does change between scans — roughly a 3× win from
  content-addressing, not the 30× a naive "it's the same library" intuition suggests.
- **The blobs compress ~24× with plain gzip** (~19× more with zstd at level 19). They are
  repetitive structured JSON, so compression is far and away the cheapest lever available.
- **Retention beats both.** Compression and dedup change the *slope*; only a bound changes the
  shape. `services.retention` keeps the newest 30, so the cost stops being a function of how long
  the install has existed and becomes one of library size alone — about 700 MB for six thousand
  items, about 2.4 GB for twenty thousand. That is the number to weigh before raising the window,
  and it is why 30 is a constant rather than an operator setting.

The negative result worth recording: **`VACUUM` after every sweep is not worth its lock.** In the
steady state a sweep frees one snapshot of thirty, some 3%, and SQLite reuses those pages on the
next scan anyway — so compaction is gated behind both a share and an absolute floor, and in
practice fires once, on the first sweep after an install that had been keeping every scan
upgrades.

**And `VACUUM` on its own returns nothing, in WAL mode, while the process is up.** The rewrite
lands in `reaper.db-wal` and the main file holds its high-water mark until the last connection
closes — which for a pooled engine means process exit. Measured on the real engine: 23.2 MB of
data directory before, 23.2 MB after a vacuum that reported success, 0.7 MB once the engine was
disposed. `PRAGMA wal_checkpoint(TRUNCATE)` after the vacuum reclaimed 22.6 MB of it at once,
with a reader open. The trap is that every *logical* measure agrees the compaction worked:
`page_count` falls, `freelist_count` reaches zero, and the vacuum raises nothing, so only a
`stat().st_size` over the directory can tell the two apart — which is why the test asserts bytes.

**The vacuum starves a concurrent app write past about 1.2 GB, and that is local NVMe.** Measured
with the real `_compact_sync`, the real engine and its connect listener, databases built at ~4 KB
a row with 35% on the freelist so the true gate opens, and the app's connection pooled before the
sweep fires: 519 MB vacuums in 1.5 s and the write lands after 1.3 s; 1.2 GB takes 5.0 s and the
write lands after 5.3 s; 2.4 GB takes 8.0 s and the write fails with `database is locked` at
5.7 s. Ordinary API traffic fails alongside it, because `_configure_sqlite`'s first statement is
`PRAGMA journal_mode=WAL`, which wants the lock too. The number that matters is not 2.4 GB but
where it sits: `KEEP_SNAPSHOTS` documents 700 MB and 2.4 GB as the *steady state* for a six- and
a twenty-thousand-item library, so the larger of the module's own two figures already loses on
the fastest storage an operator could have, and the upgrade drain vacuums a file larger still.
Hence the caller-side gate in `scheduler.sweep_old_snapshots` (#325).

Two negative results from the same harness, both worth not re-deriving. The failure is a plain
`busy_timeout` expiry and **not** `SQLITE_BUSY_SNAPSHOT`: a session that reads and then writes
waits its 5 s like any other, because pysqlite opens no transaction for a `SELECT` and so nothing
pins a read snapshot. And when the app writes *first* it holds the lock and the vacuum is the one
that waits, so the asymmetric timeouts (30 s for the vacuum, 5 s for the app) are not themselves
the defect — the duration is.

**Gating a job on "is something else busy" turns an unjittered interval into a permanent skip.**
Measured against the pinned APScheduler 3.11.3: an `IntervalTrigger` computes the next fire from
the previous *scheduled* time, so a twelve-hour interval fires at the same second of the same two
hours forever, and a run that lands 47 minutes late still produces the same next time. The phase
is therefore fixed for the life of the process, and since twelve hours is an exact multiple of an
hour, a scan on a cron whose period divides twelve hours collides with every firing or with none.
Once the compaction was gated (above), a collision stopped meaning "runs twelve hours later" and
started meaning "never runs again" — the failure a skip-and-retry design is supposed to rule out.
`jitter` is the fix and is applied as `uniform(0, jitter)` on top of the previous *jittered* time,
so the phase walks forward rather than settling; the cost is that the effective interval is the
nominal one plus half the spread on average. The general shape: **a conditional skip needs a
firing schedule that is not correlated with what it skips on**, and an exact-multiple interval is
correlated with everything on a cron.

## A threshold driven well inside its region cannot say where its edge is (2026-07-30)

`inspect`, the dangerous-config detector, is one 900-line function because every warning the
policy editor prints is a branch in it: 321 mutants, **244 killed, 77 survived**. It is the
largest zone here by a distance and worth the ceiling, because the ceiling is per *answer*
rather than per file — splitting it by warning would report slice kill rates that no longer
add up to a statement about the detector.

The four boundary rows left on #243 were all real, and so was a fifth nobody had listed. What
makes them worth writing down is that **four of the five already had a test on them**, and
every one of those tests drove the threshold from well inside its own region:

| the branch | what the suite drove | what nothing could tell |
| --- | --- | --- |
| `rule.floor >= 90` | 96 | 89 from 90 from 91, or `>=` from `>` |
| `rule.floor <= 20` | 7 | 19 from 20 from 21 |
| `window_days < 30` | 7 | 29 from 30 from 31 |
| `condemn_at <= 30` | 20 | 29 from 30 from 31 |

96 proves a warning exists somewhere above 90 and says nothing about where. So all three
mutants on each line survived — the constant a point either way, and the operator relaxed —
and the operator this costs is the one setting a bar of exactly 9.0: dead center of the
mistake the sentence exists to catch, and one point outside every case that was driven. **An
example is evidence a region is non-empty; only a pair either side of the value is evidence
about the edge.** Coverage cannot see this at all, and neither can a reading of the tests: the
line is reached, the assertion is real, and the number it drives is the whole defect.

Two other shapes came out of the same run, both already familiar:

- **The undriven arm.** `is_percentage_source(rule.source)`'s true branch — the Rotten
  Tomatoes mix-up, where typing 8 for 80% sets a bar that keeps the library — had no test at
  all. Its two sentences appeared nowhere in `tests/`, and all seven of its mutants survived,
  including deleting the guard outright. It sits four lines above a sibling that *is* tested,
  which is the rule 72 shape: the copy someone read is the copy someone pinned.
- **The divisor, for the third time.** `rule.floor / 10` converts stored tenths for display
  and survived becoming `/ 9`, `/ 11` and `* 10` at both of its sites, so a configured 7.5
  could render as 8.3 while the gate went on enforcing 7.5. #241 found the identical shape one
  layer down in `RatingFloorGate`. A unit conversion is where a boundary corpus pays twice:
  the same case that locates the edge also pins the number the operator reads.

**The static read's record, now that every row has been run.** #243 was filed as a question
(`Status/Need More Info`) rather than a defect, on the grounds that reading tests to guess what
they pin is unreliable. It was: nine of eleven rows were real, two were refuted — and *both*
misses were in the reassuring direction, predicting an undefended branch that turned out
defended. That is the safer way to be wrong, and it is still the reason the label was right.
The row that moved the other way is the sharpest of the set: the IMDb fail-closed guard was
reported closed once, on a run whose operators could not express deleting an `if` at all, and
came back real the moment they could.

## A failed commit takes the whole identity map with it (2026-07-30)

Measured while fixing the wedge in #327, where one failed step commit left a reap `EXECUTING`
on disk forever. Four things about async SQLAlchemy that a reading of the code does not
suggest, each confirmed against a real engine with Reaper's own pragmas:

- **A failed `commit()` does not just fail.** It leaves a transaction that is neither
  committed nor rolled back, so every later `commit()` on that session raises
  `PendingRollbackError` whether or not the original fault has cleared. The wedge was
  therefore never a race on how long the write lock was held: the terminal write failed
  because of the *first* failure, not because of any contention of its own.
- **It expires every row the session ever loaded, not the one that failed.** Reading a
  previously-loaded attribute afterwards raises rather than returning the value it still had a
  line earlier. So the code path after a failed commit is not merely unable to write, it
  cannot read either, and handlers that build an error record out of the row they were acting
  on fail inside the `except`.
- **`rollback()` is the only way back, and it expires everything too.** Under asyncio that is
  a harder state than under sync, where the same attribute read would quietly re-`SELECT`: an
  implicit lazy load cannot be awaited from attribute access, so it raises `MissingGreenlet`.
  Anything a run still needs after a rollback must therefore be held as plain values, or
  reloaded explicitly.
- **A `SELECT` repopulates expired rows in place; `Session.get()` does not.** Two queries
  revive a whole run's rows, which is far cheaper than refreshing object by object. `get()`
  returns the expired instance from the identity map and the first attribute read on it
  raises, which reads as a bug in the caller.

**A negative result worth keeping: `expire_all()` is the wrong tool for syncing after a Core
`UPDATE`.** Writing the terminal state as `update(...).values(...)` goes around the identity
map, so the session keeps answering with the pre-run state. `expire_all()` fixes that and
breaks the *caller*, whose own handle on the row is now expired and raises on the next read.
Setting the columns on the instance after the write costs nothing, does no IO even when the
instance is expired, and touches nothing the caller did not already own.

**And a clobber that only the mutation test found.** Once a mark recovers by rolling back and
replaying its `UPDATE`, the loaded row disagrees with the database, because the rollback
discarded the assignment and the replay bypassed the identity map. A later capture of that row
then writes the stale value back over the recovered one, so a `file_removed_at` that had just
been rescued came out NULL. Deleting the replay entirely left the suite green until a test was
added for the recovered-and-continued case specifically: the tests that existed all drove the
*unrecoverable* path, where nothing replays anyway. **A recovery path needs a test that
recovers and carries on, not only one that gives up** (rule 118).

**The mirror of that clobber, found by a review pass and not by any test: mixing a Core
`UPDATE` with a dirty ORM row in one commit lets the commit undo the statement.** Syncing the
instance after a write (the paragraph above) leaves that row *dirty*, and the session is built
`autoflush=False`, so nothing flushes it until some later `commit()` does. The next write's
`commit()` is that later one — and its flush runs AFTER its own `UPDATE` has executed, so the
unit of work re-issues the previous values over the column just written, inside the same
transaction. Measured: a step committed as VERIFIED sat on disk reading SENT with a
`verified_at` beside it, a row that contradicts itself in the journal that is the only record
of what was removed. It is repaired by the following commit, so the whole exposure is a process
death inside a one-commit window — invisible to every test that reads the row at the end, and
to the run's own session, which answers from memory where the row was always right. **The order
is the fix: flush the pending ORM writes first, then execute the Core statements, then commit.**
Two ways to write one row will interleave; deciding which goes first is not optional.

## Two lanes with no test look exactly like eight with a weak one (2026-07-31)

Closing #337's rows took `inspect` from **53 survivors of 341 to 9**, and the interesting part
is what the 53 were made of. Two of the eleven lanes had no test at any point — nothing in
`tests/` named the "delete up to N items it can't measure" sentence or either of the two size
footguns — while the other nine had a test that reached the branch and could not discriminate
it. **In a coverage report those two states are the same green.** They are also the same green
in a survivor count, which is why the count alone was never the finding.

**The fixture that agrees with itself is the recurring shape.** Every case in the condemn lane
built `recent_watchers` with a `FEW_WATCHERS` weight beside it, so three separate things went
undriven at once: the span filter (no rule on a field that reads no watchers), the blocked
boolean standing alone (no case with `withheld == 0`), and the anchor that decides which editor
card the operator is sent to — its three fixtures were 5/50, 50/5 and 25/25, splits that agree
whether the comparison doubles the signals share or triples it. One fixture family, chosen once
for the first test in the lane, and every later test inherited its blind spots.

**Half of a two-armed decision can hide behind the other.** `decide_verdict` is asked for score
*and* coverage here, and no case ever let coverage be the deciding one: every fixture put the
score under the threshold, so turning the coverage share into a product changed nothing. The
case that discriminates is the awkward one — 40 points left against a threshold of 31, clearing
the threshold while the coverage those withheld points took with them falls under the floor.

**Nine survivors are the right number to stop at, and saying why is the work.** All nine are
unreachable rather than untested: `weight` and `in_progress_hold_days` are `ge=0` at the save
boundary, so `> 0`/`>= 0` and `<= 0`/`== 0` are the same test over every saveable value; both
condemn specs refuse an unknown field at construction, so the `is None` arm beside them never
decides anything; and one initializer is dead because every branch that reads it assigns it
first. Each is classified in the test docstring that owns it (rule 118), because a survivor
list with no classification reads as work left undone, and the next person re-derives it.

## A shared edge is the seam, and four rasterizers disagree about which kind (2026-08-01)

The brand mark shipped as three shapes that touched: a hood whose cowl opening was an evenodd
hole with its bottom edge lying exactly on the hood's own, and a second path of blocks abutting
that same edge from below. Both joins seamed on a phone, in opposite directions — a faint LIGHT
line across an opening that is empty, and a DARK line under the shoulders — and both only at
some zoom levels.

**Only at some zoom levels is the whole finding.** A seam appears when the shared edge falls
*between* two device pixels; land it on a boundary and every renderer is clean. So the variable
to sweep is the edge's sub-pixel phase, and **sweeping the render scale does not sweep it**. The
first two sweeps here stepped scale by a constant, which steps the cut's position by a constant
too: 900 renders reached five distinct phases and reported the mark spotless. Sweeping a
fractional offset at a fixed scale found the seam in 20 of 96 combinations.

**Reproduction is per-rasterizer, and the four available locally do not agree.** The abutment
seam (two paths, source-over, ~25% dip) reproduces in resvg/tiny-skia, in CoreGraphics driven
directly, and in WebKit rendering a page offscreen — but *not* in Skia through Chrome's canvas.
The coincident-edge light line reproduces in none of them, at any size or phase tried, while
being plainly present in a screenshot from the device. Measuring the device capture is what
settled it: anchor on the accent eyes, whose box is exact geometry, because anchoring on the
figure's own edges bakes the artifact into the coordinates you are measuring it with.

**So "I could not reproduce it locally" is not evidence about a mark.** The fix is not to chase
the renderer but to remove what renderers are entitled to disagree about: the figure is now one
closed contour, with the cowl opening spliced into the hood's bottom edge as a notch and the two
blocks that meet the cut spliced in as detours. That needs no boolean — everything meeting the
cut meets it on one straight horizontal edge, so ordering by x unions them exactly — and it
needs no fill rule. The regression test renders and samples rather than reading the path string,
since the path text looked reasonable the whole time it was wrong.

## What frozen season evidence costs, and where the cost is paid (2026-08-03)

Making a season rule previewable meant freezing `plan_series_prune`'s inputs per show
(`db.models.SeasonPruneEvidence`). The Sonarr fan-out that fills it was budgeted in a comment;
the payload and the per-request decode were not, so both were measured.

**The payload is O(viewers × seasons)** — three of its members are per-user-per-season maps.
Measured through the real codec: **1,743 B** for a five-season show with five viewers, **8,987 B**
for a ten-season show with 25 viewers. On the larger shape at a thousand shows that is 9.0 MB a
scan and **270 MB** across the 30-snapshot retention window, which sits beside the ~700 MB the
section above measures for a six-thousand-item library rather than dwarfing it.

**The decode is 0.21 ms per show at that shape**, so ~205 ms for a thousand shows — and the
simulate request it fronted arrives on a 250 ms debounce while a control is being dragged, on top
of the 275 ms the movie lane's replay already costs at 3,468 rows. That projection is a ceiling
rather than an estimate, and the section below measures the real distribution at a fraction of
it: the lab shape was the large one.

The lever was not the total. Every show with a candidate row gets thawed either way, so decoding
lazily per show saves almost nothing on a healthy library — what it changes is *when*: 205 ms in
one uninterrupted block ahead of the loop becomes the same work spread across the yields that
loop already takes, a refusal costs one show's decode instead of the library's, and a show with
no candidate row is never decoded at all. The general shape: **an eager prefetch and a lazy one
can have identical totals and completely different tail latency**, so measure the block, not the
sum.

## Both simulator tiers, timed across a 64x range of library sizes (2026-08-03)

`POST /api/policy/simulate` answers one question two ways: tier 1 re-compares the stored scores
against the new thresholds, tier 2 replays the real engine over the frozen Facts. Tier 2 is exact
for everything tier 1 covers, so tier 1 is an optimization and #493 asked whether it earns its
keep. Settled by driving the real route against a `.backup` clone of a live snapshot — 3,469
movie rows, 2,498 season rows over 985 shows, best of five, warm.

| request | tier 1 | tier 2 |
| --- | --- | --- |
| movie | 59 ms | 305 ms |
| TV, season-rule edit | 56 ms | 366 ms |

**Tier 2 is linear with no knee: 81 µs/row on movies and 133 µs/row on seasons, flat to within
6% from 433 rows to 27,752.** So the ceiling is set by library size and nothing else, which is
what makes it decidable rather than arguable: at 4x this install a threshold drag settles in
~1.2 s, at 8x in ~2.3 s, on top of the 250 ms debounce it already waits out.

Measured apart rather than subtracted, the row query is 29 ms and 24 ms and the bundle query is
1.5 ms, which puts the **tier-1 loop at ~9 and ~12 µs/row — 9x and 11x cheaper per row.** The
gap is per-row, so it widens with the library instead of amortizing.

**Tier 1 stays**, and two of the three reasons are not about perceived latency. The replay is a
Python loop on the event loop, so a 2.3 s replay is 2.3 s of contention for every other request
on the box; and tier 1 is still the only path that answers a snapshot which froze no Facts at
all. `tests/test_verdict_agreement.py` therefore stays load-bearing: it is what stands between
the two paths and another §9.

**The season lane's extra cost is the work, not overhead in front of it.** The bundle query is
1.5 ms of a 366 ms request, 0.4%, so there is nothing left to defer that #502's lazy per-show
thaw did not already take. Every season-specific cost combined — decode, plan derivation and
guard replay across 985 shows — is ~130 ms, or ~0.13 ms per show, against a lab estimate for the
decode *alone* that was 1.6x that. The general shape: **a cost measured on the worst shape you
can construct projects a ceiling, and reading it as a forecast argues for optimizations the real
distribution does not need.**

## An invented XML element renders nothing and reports nothing (2026-08-04)

The Unraid channel picker was built against two beliefs about Community Applications, both
wrong, and a hygiene test was written that enforced them. **Read against the plugin source
instead** (`Squidly271/community.applications`):

- **CA does not discard a `<Branch>` whose `<Tag>` matches the tag on `<Repository>`.**
  `include/exec.php` expands one sub-template per branch; the only branch it skips is one that
  spells `<Tag>` twice inside a single `<Branch>`, which arrives as an array. **539 of the 695
  branch-using templates in the live app feed list their repository's own tag as a branch**, the
  count that was read as 539 broken templates and is in fact the convention working. The
  denominator was inherited as 585 and is wrong too: a number quoted to support a claim is worth
  re-running, because whoever wrote it was not testing it.
- **`<DefaultTagDescription>` is read by nothing.** The name appears in no file of the plugin;
  the Default row's text is hardcoded in `include/helpers.php` as "Install Using The Template's
  Default Tag (`:<tag>`)". Eight templates in the feed ship the field, so the invention is
  copied, not original.

Together they cost the release channel its description entirely: no `<Branch>` row to hold one,
and the field written in its place rendering nowhere. **Nothing reported it.** The template
parsed, the container installed, the picker appeared, and the hygiene test was green — because
the test asserted the belief rather than the plugin. An element a vendor does not read is
indistinguishable from one it does, from inside this repository.

**A vendor contract is verified against the vendor's code or its live data, never against what
its docs imply.** The forum schema post documents `<Branch>` in one line and says nothing about
either question. Both answers took one `grep` of a shallow clone and one pass over
`applicationFeed.json`, which is public and carries every template's parsed elements.

**The feed is the delivery mechanism, and it is not the merge.** `assets.ca.unraid.net` is
rebuilt on CA's schedule, commonly every few hours but observed with gaps over two days. A
template change is live on `dev` immediately and reaches an operator's install page only at the
next rebuild, so "the picker is not there" is the expected reading for hours after a merge, and
the feed's own `last_updated` against the merge time is what separates that from a defect.

## `overflow-wrap: anywhere` is a break opportunity AND a min-content of one glyph (2026-08-05)

The per-server tag counts on Settings, Lists were one comma-joined line per server, which turns
unreadable as tags and servers multiply, so they became a matrix (tags down the side, servers
across the top). Driven narrow on a phone, the first cut broke a tag name **one letter per line**,
stacking `reaper-keep` into an eleven-row-tall cell.

- **`overflow-wrap: anywhere` lowers an element's MIN-content to a single character.** That is
  what rule 139 wants for prose in a page that scrolls, and exactly wrong for a table cell under
  width pressure: the column can now shrink to one glyph, so the browser takes it. The tag cell
  carried `anywhere` (copied from the old `.srv`) AND the table was forced to `width: 100%`, so on
  a narrow pane the fixed table width squeezed the pinned Tag column down to that one-glyph floor.
  Two changes fixed it: the cell is `white-space: nowrap` (no break to crush), and the table is
  `width: max-content; min-width: 100%` — it fills the box when the content is narrow and
  **overflows and scrolls** when it is wide, rather than being sized to the box and crushing a
  column. ⇒ For a data matrix, do not wrap cells; let the table size to content and scroll inside
  an `overflow-x: auto` box. Wrapping belongs to prose, not to a grid of values.
- **A box that scrolls with nothing focusable inside it is unreadable by keyboard.** The matrix
  holds no focusable cell, so the `.matrix-scroll` box takes `tabIndex={0}` itself — the same
  answer `.table-scroll` and the docs tables already carry (WCAG 2.1.1, enforced by
  `a11y-scroll-reachable.test.ts`). And a cell reached by horizontal scroll is not clipped, so its
  `nowrap` is a recorded rule-139 exemption in `index-outside-text.test.ts`, not a violation:
  scrolling is the remedy, where for a clipped container it would be the bug.

## A verdict triple is a coarse reading of the conclusion it pins (2026-08-07)

The policy lab pinned `(verdict, score, coverage_bp)` per vector, which reads as a complete
engine trip-wire and is not: a great deal of what the scan concludes rounds away before it
reaches those three numbers. Measured by mutating production and running the other 4,035 tests
with the pinned baseline deselected:

| mutation | what the triple saw | what the rest of the suite saw |
| --- | --- | --- |
| `checked_and_did_not_fire` truncated to one entry | **0 of 440 vectors moved** | one invariant test |
| a below-floor `ARGUES_KEEP` folded to `NOT_APPLICABLE` | no move | one state test |
| `checked_and_did_not_fire` **reordered**, membership identical | no move | **nothing** |

The third is the one worth keeping. It is a legal-looking refactor, it changes the order the
operator reads their protections in, and 4,035 tests are green through it. The first is worse in
what it removes and only survived because a separate invariant happens to count gate reports:
the block that carries the product promise — every protection that was checked and did *not*
fire — is downstream of the why-panel, and nothing else asserted on it per real shape.

⇒ **Pin the conclusion, not the decision.** The baseline block now carries the three gate lists
by gate id, the explanation's `base_score`, `keep_discount`, `threshold`, `coverage_floor_bp`
and `watch_blind`, and per signal its `id`, `contribution`, `state` and `evaluated`. 27 pinned
leaves per block where there were 3.

- **Read it off the serialized explanation, never off the score object.** Every number above
  also exists on `Score` and the policy, and recomputing them in the lab would be a second
  implementation of `_explain`'s rounding whose output the fixture then pins as ground truth
  (rule 119). Reading production's own payload also makes the pin cover the serialization: a key
  dropped from `_explain` raises rather than thawing to `None` and comparing equal.
- **`detail` is deliberately not pinned, and that is a cost accepted.** It is roughly 60% of the
  payload by bytes and it is rule 21 operator copy, so pinning it turns every wording edit into a
  baseline stop — and a stop that fires on rewording is one that stops being read. The fixture
  grew 754 KB → 1,574 KB without it.
- **Report the diff leaf by leaf.** Twelve signal rows compared as one blob say only that
  `signals` moved. The failure a reader can act on is
  `v0007 baseline.signals[2].contribution: 12.0 -> 9.0`, and the comparison that produces it is
  shared by the test and the regeneration script so the two cannot disagree about what a
  baseline is.
- **One field cannot move and is pinned anyway.** `watch_blind` comes from the watch mirror
  going blind between scans, which is not a property of a fact vector — every replayed value is
  `None`. It is pinned for the key's presence and nothing else; rule 141 governs the rest.

## A plan is larger than the condemned set, and a real database is behind head (2026-08-07)

Two findings from building the whole-library capture, which reads one stored snapshot and runs
`build_plan` against a throwaway copy of it.

- **The plan covered more items than the scan condemned**: 543 condemned, 592 planned. Not a
  bug, and the direction is the surprising part. `effective_condemned` applies the operator's
  overrides on top of the frozen verdicts, so hand reaps *add* to the set while spares subtract,
  and on this library the reaps outnumbered the spares. ⇒ A baseline that pinned only the
  scan's verdicts would miss the whole override layer, which is a live input to what gets
  deleted. Capture the plan, not just the verdicts.
- **The operator's own database was three migrations behind head**, because it was last written
  by a build older than the checkout. Anything reading a real library through the ORM fails
  outright on that — one missing column fails the whole select, since SQLAlchemy names every
  mapped column. Raw SQL naming its own columns survives it, which is why the policy-lab
  extractor never hit this. ⇒ A tool that reads a tester's database through models migrates its
  own copy first and records the revision it reached, or it is answering under a schema it
  cannot name.

Also recorded: **`build_plan` writes**, so a read-only capture cannot call it in place. The
source is opened `mode=ro` and `sqlite3`-backed up to a temp directory; the plan is built
against the copy and rolled back on top of that. Verified by digesting the source file before
and after — unchanged. A rollback alone would have been enough in theory and is not the trade
to make here: a bug in a capture script must not be able to leave an approved run in the
operator's database.

## On SQLite, the transaction a migration runs in is not the one env.py opens (2026-08-07)

Fixing #564 (a failed batch recreate strands `_alembic_tmp_<table>` and wedges every later
boot) is two lines of SQLAlchemy's documented pysqlite recipe. Proving it was the part with a
trap in it.

- **`context.begin_transaction()` at env.py's call site is a no-op on SQLite.**
  `SQLiteImpl.transactional_ddl` is `False`, and Alembic reads that to mean "wrap each
  migration, not the whole run": the outer call returns a `nullcontext()` and the real
  transaction is opened per migration, inside `run_migrations`. So the obvious test shape —
  patch `context.run_migrations` and do a batch recreate where a migration body would run —
  probes **no transaction at all**. It went green on the unfixed tree by committing the whole
  recreate, and the assertion that caught it was not "nothing was stranded" (that passes too,
  since a completed recreate renames the temp table away) but the one checking the rollback was
  total. ⇒ A test standing in for a migration body is not a migration. Drive the real runner
  over a real migration, or the transaction under test is one nothing opened.
- **Only the FIRST recreate in a migration is exposed**: pysqlite opens no transaction for DDL,
  so the first `CREATE TABLE _alembic_tmp_X` autocommits and the `INSERT INTO … SELECT` after it
  opens the implicit transaction that every later statement joins.
- **A migration does not have to look like it recreates, and counting the ones that do by eye
  undercounts.** Recorded off the live statement stream, a fresh `alembic upgrade head` performs
  **three** batch recreates, each the first in its own migration and so each exposed — and
  **two of the three are `add_column` calls**. Alembic rebuilds the table for an `add_column`
  whose `server_default` is a ClauseElement, and `sa.false()` is one where `"0"` would not be.
  The issue's hand-written list of exposed sites named neither, and named only the three
  migrations that visibly `alter_column`. ⇒ Ask the statement stream which migrations recreate;
  reading `batch_op.` calls answers a different question.
- **The failure needs no crash.** Any exception does it, and the one to expect is the ordinary
  authoring slip rule 148 now warns about: dropping a column before the index sitting on it,
  which raises *after* the temp table is committed and is invisible against a fresh database,
  because a fresh database has no index to trip over.

## Three survivors on one predicate, and not one of them a defect (2026-08-09)

The `engine-gates` zone does not declare `RatingFloorGate._miss_phrase` (#598), so reaching it
took a supplementary invocation of the same runner. It left **3 survivors out of 14 mutants**,
all on one three-clause predicate: `min_votes > 0` widened to `>= 0`, the same token narrowed to
`> 1`, and `votes < min_votes` widened to `<=`.

**All three read as live operator-copy defects, and not one of them was.** Under the mutants the
why-panel prints "too few to trust (you need 0)" for a bar with no vote floor, and drops the "too
few" clause entirely under a floor of 1. Driven at those three states the shipped code prints
neither: it returns the right sentence every time. A survivor describes what the *mutant* does,
and where the mutated function returns an operator string, that string is unusually easy to read
as a bug report about the original — it is fluent, specific, and exactly the kind of sentence
rule 21 exists to stop. All three were carried into a fix task as defects to fix, and all three
were cases to write. **Read a survivor as a missing case until the original has been driven and
its output printed.**

**The finding underneath was real, and it was rule 104.** `Rating.meets` decides whether a bar
clears; `_miss_phrase` tells the operator why it did not. Each spelled out the same three clauses
in a different clause order, and they agree on all 288 source/votes/floor combinations — which is
the state the rule is about, because two copies that agree are not one derivation, and the next
edit to either is what makes the panel contradict the decision. Hoisting both onto one
`Rating.short_of_vote_floor` took `_miss_phrase` from 14 mutants to 2 and killed the three
survivors by **deleting** them, the same trade `describe_bar` made against this zone in July.

**Which re-opens the gap that trade opened last time.** Mutants hoisted out of a declared function
land in a helper no zone names, so the `ratings` zone declares `short_of_vote_floor` beside
`meets`. The general form is worth stating once: **a rule 104 hoist moves mutable surface, so the
zone that owned it owes the helper a declaration in the same change.**

**One of the three states cannot be reached from a saved policy at all.** `RatingRuleSpec` refuses
a vote floor below 1 on a source that counts votes, so `min_votes=0` on an IMDb bar exists only as
the `RatingRule` dataclass's own default. That argues for pinning the boundary rather than against
it: the dataclass default is 0, every percentage bar carries 0, and one validator is the only
thing standing between them.

## Two frontend extractions saved no lines, and were worth building anyway (2026-08-10)

Wave 11's W11-22 and W11-24 were both scoped as duplications with a line saving. Measured across
the built diff, splitting comment lines from code: **W11-22 is code +29 / -29, W11-24 is
+54 / -52.** The plan's `-23` for W11-24 is wrong. W11-22 never carried a figure. Both were built
regardless, for the same reason in each case: **what came out was a divergence.**

W11-22 is three modals mirroring their `canClose` into a ref their parent's Back guard reads.
Two mirrored the whole predicate. `ScheduleModal` mirrored one term of it, `save.isPending`,
against a shell handed `!save.isPending`. Those agree, and agree only while `canClose` has one
term. A second reason to stay open leaves browser Back the one dismissal ignoring a guard every
other dismissal honors, which is rule 80. `ServiceModal` already has two reasons, having grown
its second at this same shape once before. `useBackCloseMirror` takes the whole predicate as an
argument, so the one-term spelling has nowhere to live.

W11-24 is two panel fallbacks that were the same component twice, with three comments saying as
much. The structure was identical and one string was not: the why panel's failure added "The item
itself is unaffected" and the Scales panel's did not. Neither copy was wrong on its own, which is
why it survived three passes.

**The generalizable part is the search, not the two results.** A duplication found by counting
lines is scored on the lines. A duplication found by *diffing the copies against each other*
tells you which of them is wrong, and that is a defect whatever the extraction costs. Both of
these came out of the second reading. So a wave row's line estimate is a locator, and a zero-net
extraction is a kill only when the copies also agree.

**W11-3's type found a fixture nobody had read.** `VocabField.type` was a bare `string`; it is now
a six-member union in `api.ts`, pinned against `engine.fields.FieldType`. Typing it failed the
build on `StaleReadSweep.test.tsx`, which composed a rule on a `runtime_minutes` field of type
`"int"`. The server can serve neither. It compiled for as long as the field was a `string`, which
is rule 119's invented expectation caught by the type system rather than by a test.

## What the callee already enforces is not what the duplication costs (2026-08-10)

Six hand-written `PlexClient(...)` constructions, four of them passing the same four arguments
in the same order. The finding proposing a helper had already answered itself. `safety` is
keyword-only and required, so no copy can drop the transport guard. Measured, the helper takes
20 lines out and costs about 14, and it reaches four of the six sites. Six net lines against a
new indirection on the client that reads a stored credential is the wrong trade.

**The argument the signature cannot enforce is the one worth a gate.** `verify` carries the
operator's per-instance TLS switch and defaults to `True`. An omission there widens nothing, so
it reads as harmless. The cost is agreement: an operator whose server carries a self-signed
certificate gets one surface that cannot reach it while every other surface can, with nothing
announcing the difference. The same shape had already happened once in this tree with
`api_path_prefix` on the *arr clients. **So when judging a "written N times" finding, sort the
repeated arguments by what the callee can refuse on its own.** The required ones are already
bound; the defaulted ones are the population a gate has to cover, and it should cover every class
with that parameter, not the one class the finding named.

## A module at 93% can have every failure arm of a safety step unreached (2026-08-10)

`services/restore.py`'s arm step runs three prepare functions before it writes the marker that
lets the next boot swap. Each maps its failure to one operator sentence. Behind 4,283 passing
tests and 93% line coverage for the module, **no test drove any of the three**, and no test
anywhere asserted the sentence. Line coverage said so plainly and nobody had read it: the three
`except` bodies were sitting in the missing-line list.

The reason generalizes past this file. A staging flow's happy path is heavily tested because
every other test needs it as setup, and the happy path is what the percentage is measuring. The
arms that only a failure reaches are cheap to leave, and they are also where the operator-facing
sentence lives, so a reword lands with nothing to catch it. **Read the missing-line list, not the
percentage, on any module whose job is to refuse.**

The reword is also what found a second thing the arms were hiding. Saying "nothing was restored"
forces the question of whether that is true at every raise, and at one of them it was not: a
confirm retried after a client-side timeout re-ran the prepare steps over a staging that was
already armed. **A claim about state is a cheap way to audit the states a function can be called
in**, and it fails toward reassurance if nobody checks.

## A staleness comparison is satisfied at either end, and only one of them is honest (2026-08-10)

Four surfaces show a connection-test badge only while the result still describes what is on
screen. Each stores `{ result, of }`, where `of` is a fingerprint of everything the test was sent,
and each renders the badge on `test.of === testedWith()`. That is one derivation written four
times, and it was measured for a dedup. The dedup is a kill. What the measurement found instead
is that **three of the four computed the fingerprint at the wrong end, and the comparison cannot
tell.**

`testedWith()` called inside `onSuccess` runs after the response lands. It fingerprints whatever
the operator typed while the request was out. The stored `of` and the live `testedWith()` then
match **by construction**, whatever moved, and the badge vouches for an address that was never
tried. Computed at issuance instead (React Query's `onMutate`, whose return reaches `onSuccess` as
context) the two disagree and the badge withdraws, which is the honest answer.

**The comparison is what made this invisible.** Three tests already drove the staleness direction
at the one site that had it right. Two edit the host and the key after a pass and assert the badge
goes; the third types back to the tested value and asserts it returns. All three pass with the
fingerprint computed at either end, because they change the boxes while nothing is in flight. The
discriminating case is the retype *during* the request, and no test in the suite drove it. So the
one site written correctly was correct by its author's reasoning alone, and its three siblings
copied the shape without it.

**A gate over the family took three drafts, and the first two were fail-open.** The obvious
question is "was a call evaluated here", and it is the wrong one twice over. A regex bounded at
the first comma reads `of: [kind, baseUrl()].join(" ")` as innocent. Bounding it properly by
bracket depth fixes that case and still passes the same fingerprint inlined as a template literal,
which is the identical defect with no call in it at all. Both drafts read green over a working
demonstration of the bug. The version that holds inverts the question: `of:` may be handed a name
or a path of names, and everything else fails. **A gate that hunts for the shape of the defect is
open by omission; one that allows the shape of the fix is closed by default.**

## The five-times duplication with nothing behind it (2026-08-10)

Five surfaces report a draft upward through the same four lines, three settings panels and two
children of panels, and rule 146 is written about exactly that signal. The count was the only
figure in its finding that needed no correction, and it was the sub-item worth the least: 20 lines
out against 29 back, and what the rule asks is per-site anyway. The signal must be declared above
every early return, and every early-return state re-read as one the report still fires in, which
is a claim about each surface's own branches. All five satisfy it, and the five answers differ:
two name the branches their report survives, two say they have no early return above it, and one
has three returns that all sit below. **A hook cannot carry an obligation whose subject is the
call site**, so the shared four lines are the part with no leverage in them.

## A line count and a drift surface are two measurements, and a dedup usually pays in the second (2026-08-10)

Phase 8 of the simplification plan killed fifteen findings. Ten of the kills rested on a line
count, seven of them citing the plan's S5 ("a parameter object that nets to zero") by name. The
arithmetic held everywhere it was re-measured. A dedup's usual payoff is that one declaration
cannot drift from itself, and a line count does not measure that. The same phase found five fresh
drifts inside the code those extractions would have covered.

Thirteen of the fifteen were re-read with the second question added. Two moved and eleven stood,
so the line test is incomplete rather than wrong. The two that moved:

- **The measured shape was the wrong shape.** Two components carried the same image-fallback
  ladder, and the kill measured extracting a shared *component*: 33 lines out, 35 back, because
  the two sites share the ladder and share none of their markup, so a component takes the markup
  as props. A **hook** takes the ladder alone and leaves the markup where it is: about zero on
  total lines, about -8 on code lines, and two comments pointing at each other retired. Same
  duplication, same arithmetic, different answer.
- **The hazard survives the kill.** Collapsing two hand-written record packs into one carrier
  removed no parameter and no line, which is a correct kill. Every field of the record defaults
  to `None`, so a field packed on one lane and forgotten on the other raises nothing and the type
  checker sees nothing. Three of the fifteen are cross-system join keys. A gate over the two packs
  costs 60 test lines and no production risk, and closes what the carrier would have closed by
  construction.

**Two rejections, both measured.** Shared `Annotated` bound aliases would state a wire model's and
its domain twin's seven validation bounds once. Rejected: these are the deletion caps, and moving
a bound off the line that declares it costs a reader more than the second spelling costs an
author. A test holding both to one answer and naming both files when they disagree was already
there. The second image ladder of two lines per site stayed two lines per site, because a two-line
ladder does not pay for a module.

**A kill also has to ask whether the divergence it preserves is correct.** Measuring two copies to
decide they are cheaper apart reads each copy for its size and neither for its behavior. One kill
recorded a pair as "one with a reset effect and one with neither" and did not ask whether the
absence is right. It is, and only because of a list key nothing had written down.

## A cap on the work is not a bound on the burst (2026-08-10)

`fairness._enrich_titles` looks bounded and is not. `_TITLE_LOOKUP_CAP = 80` caps how many
not-in-scan titles get a live name lookup per Scales load, and the docstring said the calls were
therefore bounded. They were bounded in total and unbounded in parallel: nothing bounded the
burst. httpx2's default connection pool holds 100, which sits above the cap, so one page load
could open 80 sockets to one portal.

**The cap reads as the safeguard, which is why nobody looked.** A cap answers "how much". A
semaphore answers "how much at once". A comment saying "bounded" without saying which one is
what let this sit. Measured with a portal that counts what is in flight: 24 targets peaked at
24 concurrent, and at 8 under `asyncio.Semaphore(8)`.

**Bounding the burst lengthens the tail, and that needed fixing in the same change.** A portal
that accepts connections and never answers costs one read timeout per wave. Unbounded that was
one wave; at 8 it is ten, so the bound multiplied a stalled page load by ten. The enrichment had
no deadline of its own because one wave never needed one. **A concurrency bound is a latency
change, so the wave count is part of the fix.**

**The load-shedder was the wrong tool, twice over.** The finding proposed reusing
`auth/ratelimit.ConcurrencyGate` and called it unused. It has three production callers, and its
`acquire` returns 0 when full so the caller sheds load rather than waiting, which for a page
enriching titles would drop names instead of pacing the reads.

## Removing a redundant parameter removes what its test could distinguish (2026-08-10)

Five scheduler functions took `data_dir` beside the `settings` it came from, and every production
call site passed `settings.data_dir`. Deleting the parameter is -14 lines for the parameter and
-10 landed, and it leaves one source for the folder. It also costs a test its distinguishing
power, and that is the part worth writing down.

`test_the_snapshot_sweep_is_handed_the_folder_the_database_is_in` handed `build_scheduler` a
folder that was *not* the engine's, on purpose, because the compaction opens
`data_dir / "reaper.db"` with a raw sqlite3 connection: a wrong folder creates an empty second
database and vacuums that while the real one is never compacted. Its docstring named rule 141
outright, saying that pinning the engine's own path "would hold just as well if the job derived
its own". Removing the parameter is exactly the change that makes the job derive its own, so the
divergent value the test relied on stops existing.

**The test's question was retired, not answered.** With one source the two cannot diverge. What
is left to pin is what the old assertion took on trust: that the folder the sweep vacuums is the
folder the engine opened. It is read back off the engine's own URL rather than recomputed from
`settings`, which would only restate the derivation under test.

**A test that pins a value's provenance is worth reading as an argument about the design.** The
intermediate option here kept the parameter on `build_scheduler` alone, at -13 lines, and
preserved the test verbatim. It was the worst of the three. It leaves the ratings download
reading `settings.data_dir` while the sweep reads the argument: two sources for one folder, with
a test proving only that one of them arrives.

## An extraction can be larger than the duplication it removes (2026-08-10)

`whitelist.overrides()` and `spare_expiries()` are two scans of one table, and a third function
in the same file already reads all three columns in one statement, so folding the pair into one
read looks like free subtraction. Built and measured: `whitelist.py` +14/-7, `review.py` +2/-4, a
**net +5**.

**The arithmetic is structural.** Each read being replaced is two statements, a `select` and a
`dict()`. What replaces them is a loop that splits one result set into two maps, which is ten
lines. Any extraction whose replacement must reshape data pays that. Collapsing N cheap reads
into one richer read adds the projection code the cheap reads did not need.

Two further figures in the finding were wrong. Only two of its four call sites are adjacent
pairs; the other two sit 40 and about 150 lines apart, so collapsing them relocates a read
instead of removing one. And one caller comes out worse, `spare_expiries` alone going from a
filtered two-column select to an unfiltered three-column one.

## A form control does not inherit its font, so five of six fields right looks right (2026-08-10)

Rule 40's control standard is six declarations, and ten CSS blocks type them out. Two blocks set
`font-size` and no `font: inherit`. A browser gives `<input>` and `<select>` their own
font-family in its UA stylesheet, and that beats inheritance, so those boxes rendered in the
browser's form font while every label beside them rendered in the app's. Measured in Chromium:
15px Arial at the Logs search box, 13.76px Arial at every box and dropdown in the Settings
control column, against system-ui everywhere else. On `dev` too.

**The shape is what makes it survive review.** The block declares five of the six fields
correctly, so a reader checking it against the standard finds four matches and a fifth line that
mentions the right property. And the symptom is a typeface, which reads at a glance as a size
difference and gets attributed to the `font-size` that IS there. Neither the diff nor the screen
says the word "family".

**The other five fields would have failed loudly, which is why only this one drifted.** A missing
border, radius, fill or padding is visible in one look. A missing focus ring falls back to
`01-base.css`'s `:focus-visible`, which draws a slightly different ring rather than none. The
font is the only field in the standard whose absence is silent, and it is the one that drifted at
two of ten sites.

**The extraction the finding asked for was killed on measurement.** Hoisting the six declarations
into one grouped base rule in `01-base.css` nets about -56 lines of 10,614, and it moves
`.set-row .set-control input` (specificity 0,2,1) from file 27 to file 1, which inverts its tie
with `.swatch-wrap input[type="color"]` at 27-settings-rows.css:218. That tie is decided by
source order today and the later rule wins; the evidence is `:271`, a third rule written to
re-override the pair inside `.hex-join`. Reachable only through one DOM shape at this tip, so
latent. A gate landed instead, and it caught the drift the extraction was supposed to prevent.

**Putting the standard back costs more than the family, because `font` is a shorthand.** A
CSS-wide keyword sets every longhand it owns, so `font: inherit` also took the inherited 1.55
line height, where those controls had the UA's `normal`. The boxes grew about 6px. That one is
the standard arriving rather than a side effect: in a cluster row the box had been sitting
8.25px shorter than the button beside it, and it now sits 1.92px off. The other two were
regressions, and both were controls whose own rule declared one font detail at (0,1,0) under a
(0,2,1) `font: inherit`: the API key's monospace and the accent hex field's tabular figures. A
revealed API key came out in the app's sans, against a comment on its own rule saying monospace
is there so the key reads unambiguously.

**A block declaring only `font-size` hides that hazard, which is why the repair is the risky
half.** The old block set no family, so nothing it could outrank was at stake, and the two
lower-specificity rules had been correct for as long as the drift existed. The fix is what put
them under a shorthand. So the sweep after adding a CSS-wide keyword is not "what did this
element look like before", it is **"what other rule declares any longhand this keyword owns, at
lower specificity, on anything this selector matches"**. That is `font-family`,
`font-variant-numeric`, `font-weight`, `font-style`, `font-stretch` and `line-height`, and a
computed-style dump over the real markup is the only way to see it: the diff shows one added
line and the failure is two files away.

## Two counts of one diff, and only one of them belongs in the table (2026-08-10)

**`SIMPLIFICATION_PLAN.md`'s line figures are code net: non-comment, non-blank, docstrings
excluded.** That is the unit in every verdict cell and every *Landed* row. A total-line count put
in the same column is not a rougher measurement of the same thing. It is a different quantity in
the same units, and it is normally larger, because what a dedup adds back is mostly the docstring
or comment carrying the rule the copies had split between them.

Six wave 11 backend rows, measured both ways. **Total lines: -2, -2, +2, -1, +4 with a 42-line
revision, +7. Code net: -3, -5, -3, -3, +4 with an 11-line revision, -2.** Stated in the plan:
-3, -7, -5, -3, index-only, +2.

**The two readings disagree about the outcome, not about the size.** On total lines, five of the
six cost more than stated and none cost less, which reads as a systematic optimism in how the wave
estimates. On code net, two are exact, two are short in the same direction, and one beats its
estimate. The first reading was published, with a general claim built on it about estimates
counting the deletion and never the declaration. **The claim was an artifact of the unit.**

**This is the second time the same confusion moved a verdict, in the opposite direction.** One
pull request earlier a getters row read at +2 by scoring a helper's docstring as code and was
corrected to -6, which flipped it from a marginal build to a clear one. Here the flip ran the
other way: W11-43 read as +7 against a stated +2, which is a row that overran, and it is actually
-2, a row that beat its estimate.

**So print the unit next to the number, and check the unit before writing the lesson.** A figure
disagreeing with a document is the moment to ask what the document is counting, not the moment to
explain why the document was optimistic. The write-up is the expensive half to get wrong: the six
numbers are an instance a later reader can re-derive in a minute, and a wrong general claim about
how the wave estimates is what they will take as settled and never re-measure.

**The row that moved most is the one whose verdict depended on it.** W11-43 was offered as "build
at +2, or write the gate", with the gate the better answer if the consolidation cost lines. It
costs -2, so there was no trade. The gate loses on its own merits anyway: after the consolidation
`src/` holds one multi-parent walk, so a ban would scan a population of one, and before it the
ban would have to exempt the two sites it exists to catch.

**The one item decided by something other than a line count was the only defect in the six.**
`ActionStep.run_id` was unindexed and `EXPLAIN QUERY PLAN` returned `SCAN action_step` for the
executor's own filter, against a table retention never sweeps. Its test asserts the query plan
rather than the index's presence, so an index that exists and is not chosen still fails.

## A rendering test cannot see a duplicate that renders identically (2026-08-10)

A dedup replaced two byte-identical copies of one operator sentence with a declaration, and
shipped a test asserting both routes answer with that declaration's halves. **The test cannot
fail for the thing it was written to prevent.** The copies it replaced produced identical output,
so an arm that re-inlines the sentence renders exactly what the declaration renders, and every
assertion over the two responses stays green. Rule 144's gap is a source-text question, and only
a source-text assertion closes it.

**The same test was fail-open a second way, found by driving it rather than reading it.** Raising
the template unformatted passes every check: the raw string starts with the prefix and ends with
the suffix being compared, because those are literally its own halves, and the eight characters of
`{reason}` satisfy a "longer than prefix plus suffix" bound. That ships `{reason}` to the operator,
which is a rule 21 defect the gate was standing in front of.

**Neither hole is visible from the assertions; both are visible from a mutation.** Three mutations
were needed to cover one three-line change: reword an arm, re-inline an arm verbatim, drop the
`.format()`. No single assertion catches more than one of them. **When a test's expectation is
derived from the declaration it is testing, ask what the declaration and the code under test can
be wrong about together** — a derived expectation moves with the thing it should be pinning.

## A prop hand-off is not drift if the type checker sees it (2026-08-10)

A finding said six navigation callbacks were drilled three to four levels through the React tree
over a destination type that is already one value. Re-derived from `App.tsx`: **nine distinct
prop names over ten hand-offs, seven consumed by `App`'s own child and three going exactly one
level further.** Nothing goes past depth 2. The six was the `onGoTo*`-prefixed subset, and the
plan leaves out the three cross-page jumps named `onOpen*`.

**Depth is what the finding rested on, and it decides the answer.** A prop that stops being
forwarded is a TypeScript error at the intermediary and again at the leaf, so the hand-off has no
drift surface: it fails at build time or it is correct. Bundling the three depth-2 props into one
destination prop trades about nine lines of pass-through for a bundle type and a second
indirection, and removes no place a future author has to remember something. The part that could
drift had already been fixed, in `navIntent.ts`: `goTo` is one entry point over one `NavIntent`
union, replacing four per-destination setters that each had their own idea of what to reset.

**The measurement that mattered was not the count.** Two earlier passes both got the count
roughly right and neither wrote down the depth, which is the only figure the row's argument uses.

## The cascade moves when a declaration moves to an earlier file (2026-08-10)

Five controls draw one bare, pill-shaped ✕, and three of them carried the same thirteen
declarations in three stylesheets. Sharing them means moving the declarations to a file that loads
**earlier**, which is a cascade change: any rule of equal specificity in between now wins where it
used to lose. Nothing in the diff shows it and no existing test reads it.

**Read the shape back off the cascade rather than arguing it.** jsdom parses the concatenated
stylesheet and does the cascade, so `getComputedStyle` on each control, rendered in the ancestry
it really has, answers which declaration won. The values were captured **before** the shared rule
existed, so a green run is the claim that nothing moved. Two limits are worth knowing: `var()` is
left unresolved, which is what a test like this wants because it is asking which declaration won
rather than what pixels it became; and `border-width` is reported from the keyword rather than
from `border-style: none`, so `border: none` and `border: 0` disagree there and agree on screen.

**Doing it found a control that was not the shape it claimed.** One of the five sizes itself
`width: var(--tap-min)` and never resets the global `button` padding, so under `box-sizing:
border-box` its used width is 29.2px against the 24px token it names. The `width` declaration does
nothing. It was left as it renders and the shared rule excludes it, since folding it in would have
needed a `padding` declaration whose only job was to cancel a shared one.

## A shell gate can be green because the page is small (2026-08-10)

`binaries.yml`'s boot probes check the packaged build serves the SPA and not a JSON 404:

```
if curl -s http://127.0.0.1:8461/ | head -c 200 | grep -qi "<!doctype html>"; then spa=true; fi
```

Under `set -o pipefail` a pipeline reports its rightmost failing command. `head -c 200` exits
after 200 bytes and `grep -q` exits on the first match, so curl can take SIGPIPE and the pipeline
is non-zero with `grep` having matched. **Whether that happens depends on the page size.**
Measured with the same one-liner: a 4 KB body passes, a 200 KB body fails. Under the pipe buffer
curl writes the whole response and exits 0 before either reader closes.

The page the probe reads is the BUILT `index.html`, not the source one, and today it is under
5 KB. So all three copies of that line are green on a margin nobody chose, and it moves with
every build. The build that trips it is a healthy one whose page grew, and the log says the
bundle lost its SPA. Redirecting to a file and grepping the file removes the pipeline.

`tests/test_repo_hygiene.py` now bans the shape in any pipefail'd workflow step, so this cannot
come back in a workflow nobody thought to re-read.

The general shape: **`| head` inside a pipefail gate makes the verdict a function of how much the
writer produced**, not of what it produced. CLAUDE.md's rule 134 names `| head` for the sibling
case, a command dying partway and reporting that as its own result; this is the same mechanism
reaching the opposite conclusion, a command that succeeded reported as failed.

## Four dedups, and the line count answered four different ways (2026-08-10)

Wave 11's W11-42, W11-19, W11-18 and W11-10, built and measured in one pass. Code net,
non-comment and non-blank: **-8, -8, -3 and -6**, so **-25** over the four. Stated: ~85 (~16
believed removable), ~15, ~25, ~45. Only W11-19's -8 held exactly.

**The raw diff over the same files is +21.** Two copies each carry half an explanation, or one
carries it and the other carries a pointer to it; one shared declaration carries the whole
thing, and this repository writes that out. So the code falls and the file grows, and which of
those two numbers you quote decides the verdict.

**Counting is where this went wrong first, and the error inverted a verdict.** W11-10 was first
reported at **+2** because the counter scored the new helper's docstring body as code. Docstring
lines are the one prose form with no comment marker on them, so a diff filter written for `#`,
`//` and `*` reads them as source. It measures -6. The row would have concluded that the item
buys nothing on lines when it is in fact the second-largest saver of the four.

**What each one bought is a rule that had already been forgotten once.**

- **W11-10**: "no `await` between the read and the write" stops two requests installing two
  different per-app objects. It was written at one of four copies and depended on at three. A
  plain `def` cannot acquire an await without every call site turning async first.
- **W11-19**'s two copies had already cost a bug: one surface found that the notice must clear on
  a Save and not only on Discard, and the other then carried a comment *pointing at* that fix
  rather than sharing it. The extraction also found a fourth caller nobody had counted, by
  breaking it.
- **W11-18**'s no-clobber rule was unpinned across the whole suite, behind rule 141: the test set
  the saved value and the suggested value both to the same string.
- **W11-42**'s two copies shared the line above, so the defect would have had to be found and
  fixed twice.

The plan's S5 already carries the standing form, that a kill needs both halves said. What these
four add is that the line half is not one number: it is -25 or +21 on the same change, and the
question that did the work every time was **"is there a rule here that only one copy states"**.

## A helper measured at zero lines was measured in the wrong shape (2026-08-10)

Two settings findings were killed on "an extraction that nets to zero lines is not worth its
risk". Both were re-measured. One was killed on the wrong shape and is now built. The other loses on
every reading, including the partial nobody had priced.

**The shape decides the arithmetic, and the first shape measured was the expensive one.** The
helper priced for `app_settings.py`'s three identical switch getters swallowed the `await _get(...)`
call, which is what put `leaving_soon_unarmed`'s call at 104 columns against a 100-column limit
and made it wrap to three lines. A helper taking the value `_get` already returned is pure,
synchronous, typed `-> bool`, and leaves every call site between 76 and 79 columns. Measured
across both helpers: **+13 total lines, -9 code lines**, the difference being 18 docstring
lines and six blanks. The rule those docstrings hold used to be implied at three sites and
stated as prose at two of them.

**A test walk that matches on a call stops seeing any function that hands that call to a
helper.** `tests/test_app_settings_precedence.py` derives its population by AST: a function
that calls `_get` and takes a `Settings`. The swallowing helper would have dropped all three
switch getters out of it, leaving the count at four and the gate green while three sites went
uncovered. The value-taking helper keeps every `_get` call where the walk can see it. **Before
extracting anything, grep the tests for a walk that matches on the call you are about to move.**

**One declaration turns three mutations into one, and that is the payoff a line count cannot
show.** Swapping `stored is None` for `not stored` inside the shared helper fails three named
cases at once; before, the same defect had to be introduced three times to be caught three times.
The credential helper reaches a third caller the finding never counted, `get_api_key`, whose own
decrypt-failure path is pinned by nothing of its own: it stayed green under the mutation across
296 tests in `test_settings_api.py`, `test_general_and_logs.py`, `test_foundations.py` and
`test_api.py`. It is covered transitively now, by the one Discord case that drives the shared
declaration.

**The scheduler decorator loses even as a partial, and the partial had never been priced.** The
kill judged it over all seven jobs. Decorating only the four that fit was built for real,
formatted, mypy-clean and green on every scheduler test, 37 of them at the commit it was measured on: **+21 total lines, +13 non-comment
lines, +5 statements**, against an estimate of about zero. `inspect.signature().bind()` is still needed after the
narrowing, because three distinct argument positions survive it. And the drift question answers the
same way the line count does: each of the four still declares its own job id, log event and
result string at the decoration, so the only thing centralized is `ok=False`.

**Writing the rule down is what found the site that broke it.** The credential helper's docstring
says every caller has to agree that an undecryptable credential is absent. A review lane checked
that claim against the tree and found `GeneralSettingsOut.api_key_set` computing presence from row
existence, so a key written under a rotated secret reported as set while the reveal route 404s and
the header lane refuses it. Rule 76's shape, one level up from a file check, and it was on `dev`.
Fixed rather than filed, because the claim is this branch's. **The sweep found no second site**:
instance credentials have no absent-on-broken posture at all, every `decrypt` of one letting the
`ValueError` propagate, so `InstanceView.has_key` reading `bool(row.api_key_enc)` agrees with its
own runtime and is not the same defect.

**The general form is now in the plan's S5.** "Nets to zero" is a line test. How many places a
future author has to keep in step is a different question, and a kill that answers only the
first one is not finished.

## A gate's unit has to be the invariant's unit, and two gates got it wrong in opposite directions (2026-08-10)

Two wave 11 kills each said "no shape rescues this, write the gate instead". Building both found
the same mistake twice, from either side.

**Counting a word cannot see the sentence around it.**
`test_the_reload_advice_population_is_pinned_per_file` matches the bare word `reload`, case
insensitively, per shipped `.tsx`. Its subject is the advice "Reload to try again.", which four
panels print verbatim after a read never lands. Measured: rewriting one of the four as "Couldn't
load your settings. Reload to try again." leaves the word count identical and the gate green.
The word survives every rewording of its sentence, so a copy can leave the family and still
reconcile. A per-sentence pin over the same walk fails on that mutation. It also surfaced what the
finding had missed: the family is **25 distinct sentences at 32 sites**, five of them duplicated,
where the finding named two.

**Counting elements reads a ternary and a `.map()` backwards.** The rule behind `.field-sm` is
runtime cardinality: a `<label>` names exactly one control, so it wraps one, and anything else is
a `<div>`. Source text cannot answer that. Of 26 boxes, one holds a `<select>` and an `<input>` in
the two arms of a ternary and renders **one**, and two hold a single `<select>` inside a `.map()`
and render **many**. A gate counting control tags calls all three wrong, and each failure would be
against correct code. So the gate pins the population per file and per tag and leaves the
cardinality to the author, with the reason for each `<div>` written beside the pin.

The general shape: **a text gate can only assert over the unit its matcher collects.** Pick the
unit the invariant is stated in, or pin the population and say plainly what the walk cannot see.
Rule 147 says a matcher is bounded by the syntax it can parse; this is the same bound one level
up, at what the matcher is a matcher *of*.

**A count is only an independent half of a ban while the two read different populations.** Both
gates walked shipped `.tsx` and reconciled the members they read against the mentions they found
in the same files. A class name or an operator sentence exported from a `.ts` module and used
from a component is outside that walk, so it left the matcher and the count together and the two
figures still agreed. Measured: a 27th `.field-sm` box with two controls and no name read green,
and so did a 26th never-loaded sentence. Rule 145's failure with the count already in place.
Two more from the same review pass, both about where a run of text begins. Matching forward from
`could` left the front of the sentence open, so prepending a clause to one of a pinned pair moved
nothing. And splitting a line at the first `//` truncated it at any URL, taking the rest of that
line out of every walk in the file.

## A committed fixture's indent is 34% of its bytes and 100% of its lines (2026-08-11)

`policy_lab_vectors.json` and `whole_library_baseline.json` are written by their generators with
`json.dumps(..., indent=1)`, which puts every scalar of every nested array on its own line. A
season pair reads as four lines. Measured across both files: 133,690 lines and 748 KB of
whitespace, against 1.55 MB of content.

`separators=(",", ":")` in each generator and one re-dump of the files on disk. Both drop to a
single line, the suite is unaffected because every reader parses rather than reads text, and
`sort_keys=True` stays so a regenerated fixture still diffs deterministically.

**What the indent was buying was not review.** A 42,000-line fixture diff is not read either
way, and the two files are regenerated by a script rather than hand-edited, so the readable form
is `git show <rev>:<path> | python -m json.tool` on the rare occasion someone wants one. The
cost was carried by every clone, every checkout and every PR that touched a vector.

## A mirror can be complete, stale, or short, and only two of those were asked about (2026-08-11)

The scan tested the watch mirror for EMPTY (`horizon is None`) and for STALE (`last_synced_at`
past `MIRROR_STALE_AFTER`). A mirror that is populated, synced an hour ago, and holding a third
less than the source passed both and produced a snapshot marked `degraded = 0`.

Measured on a 425,604-row history restored from another instance: the mirror held 274,992 rows,
65%, and the missing rows were a clean cut at the old end rather than scattered gaps. Against the
previous scan, with `policy_hash`, `scoring_hash` and `evidence_hash` all identical, 245 titles
came off the condemned list, 2.17 TB. Coverage on them fell from 10000 bp to 1137 and 6408.

**The engine was right and the snapshot was wrong.** An unsigned score over a fixed denominator
can only fall as evidence goes missing, so a shorter mirror produces a smaller condemned set by
construction. Nothing in the scoring needed fixing. What was missing was the snapshot saying it
had judged on partial evidence.

**Three things conspire, and each is individually correct.** Backups exclude the mirror on
purpose (`backup.py` writes `"cache_db": False`), so a restore leaves it behind. Every sync after
that is incremental, and an incremental walk's paging total is the size of its own increment, so
it completes correctly against `of=266` while 150,604 older rows are absent. And `synced_at` is
stamped by `_check_regression` BEFORE the walk, so the staleness clock reads fresh the whole
time. No single component is at fault, which is why nothing caught it.

**The tolerance is measured, not guessed, and an equality would have been wrong.** A play in
progress is counted by the source and deliberately skipped by the ingest, so the mirror can never
equal the total: the full sweep ended `inserted=425596` against `of=425604`, with
`history.rows_skipped live=8`. The legitimate gap was 0.002% and the defect was 35%, three
orders of magnitude apart. A percent with an absolute floor sits between them, and the floor is
what keeps a handful of live plays from degrading a scan on a small history.

## A page is not free, and a bound in pages is not a bound in rows (2026-08-11)

The history walk asked Tautulli for 25,000 rows a page on a written claim that a 25k page costs
about what a 1k page costs, so large pages were strictly better: 17 requests instead of 422. The
claim is false. On a six-figure history each 25k page spent 60-80% of the client's 30s read
budget, and the same instance answering roughly 1.8x slower timed out on the first page. The
walk then aborted, and since nothing retries before the next cron slot, a persistently slower
instance never completed another sweep (#780).

**The request count was the wrong thing to optimize.** What a page costs is time, and the budget
it is spent against is fixed. Trading 422 cheap requests for 17 expensive ones bought nothing
measurable and spent the entire margin, so the sweep's survival came to rest on the source never
getting slower. `transient_retry` cannot help: it re-sends the identical oversized request
against the same budget.

**The second half only appeared after the fix.** A hard stop of 1,000 pages was written as "far
past any real library's history," which was true at 25k rows a page and is a different number at
every other page size. Adding a shrink-on-timeout floor of 1k rows silently moved that ceiling
to a million rows, roughly one to ten times a mature install, and the walk that reaches the
floor is by construction the slow one against the deepest history. A bound expressed in one unit
and read as a claim about another goes stale the moment either factor moves, and nothing in the
type system or the tests notices. State the reach at the SMALLEST page the code can choose.

## Prior art

Read as of 2026-07, at default settings. These are live projects and any of them may have
moved since; the point below is about a shape these designs share, not a scorecard.

- **Maintainerr** — shipped no auth of its own. Its `operator` field is overloaded
  (section-join vs rule-join), and rule evaluation is order-dependent set algebra with no
  precedence: `A OR B AND C` always means `(A OR B) AND C`.
- **Janitorr #234, "deleted half of library"** — a user wrote
  `movie-expiration: {100: 10d}` believing it meant *"only when 100% full"*. It means
  *"while free disk is below 100% — i.e. always — delete everything older than 10 days"*.
  **One config line. No rule builder involved.**
- **Deleterr #291** — "dry-run mutates state".

The common thread: **protections live inside the same boolean expression as the
condemnations**, so an unknown value, an API failure or a mis-set operator silently
*disarms* a protection. Hence Reaper's two-lane design: gates have no `CONDEMN`
constructor and cannot delete a file no matter how they are misconfigured.
