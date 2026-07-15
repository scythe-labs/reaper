# Reaper — Living Plan

> Updated as work proceeds. Records not just what is done, but **which assumptions
> turned out to be wrong** — because on this project, most of them did.
>
> Reaper is written as software other people will run against libraries we have never
> seen. No number in this repo describes anyone's actual server; findings from live
> testing are recorded as ratios and shapes, never as fingerprints.

Last updated: 2026-07-15 (external-ID identity resolver landed)

### Newest — matching by external ID, not by name

The fate-deciding join — *arr item → Plex item — was **name-based** (lowercased title + year,
degrading to title-alone whenever Plex omitted the year). That join produces the Plex rating
key every downstream check reads, so a mis-bind never deletes the *wrong* file (deletes route
by the arr's stable `media_key`) — it deletes the **right** file for the **wrong reasons**,
having read a stranger's "nobody's watching, long dormant." Both sides already carried stable
ids (Radarr `imdbId`/`tmdbId`, Sonarr `imdbId`/`tvdbId`, Plex `imdb://`/`tmdb://`/`tvdb://`
GUIDs), unused for this join.

Now there is **one** shared resolver (`engine/identity.py`, pure — rule #3) with a fail-closed
ladder: **external id → file basename → title+year**, plus a *contradiction veto* — a title
mismatch is silence (the id still binds, surviving renames/regional titles), but two tiers that
both resolve to *different* rating keys **abstain**. A duplicate id abstains too; every
ambiguity keeps the file. Plex ids come from one guarded plexapi `library_guid_index` sweep,
left-joined onto the Tautulli spine by rating key so `added_at`/dormancy stay byte-identical; a
failed sweep **degrades** the snapshot (never a silent fall back to title-only). Provenance
("bound by TMDB 1001" / "kept: two Plex items share this id") rides in the existing
`explanation_json` `match` block — no schema change, no executor change, approvals unaffected
(`manifest_hash` ignores rating keys). Assumptions that held: the executor already refuses
without Plex, so tying id-enrichment to the Plex connection strands nothing. Two latent bugs
fixed along the way: the "Never Reap" collection's GUID parser missed the legacy single-`guid`
string (and its `?lang=` suffix), silently unprotecting legacy-agent libraries; and `tmdb://0`
/ `tt0000000` sentinels were parsed as real ids. Green: 858 backend tests (new `test_identity.py`
+ scan-path/`build_movie_index` coverage), ruff, mypy, frontend build; no migration drift.
Follow-up (not built): render the `match` block in the React why-panel; delete-time
re-resolution to catch a rating key that moved between scan and execute.

### A whole-codebase review, found and fixed

A comprehensive review (`docs/CODE_REVIEW.md`) surfaced 64 adversarially-verified findings
across bugs, security, production-readiness, performance and UI/UX; all were implemented and
the tree is green (832 backend tests, ruff, mypy, and the frontend build). The load-bearing
fixes: the movie→Plex join now disambiguates duplicate titles by year and fails closed instead
of last-write-wins (a wrong-history delete risk); bulk "Reap now" on a TV show expands the show
group-key to its condemned seasons instead of erroring; an explicit empty selection fails closed
rather than planning the whole library; per-run caps count only what will actually be deleted;
the grace clock restarts when a rescued item is re-condemned after a real gap; auth grew per-IP
and per-account rate-limiting/lockout; the at-rest key uses a salted KDF (with transparent
backward-compat for keys written under the old derivation) and `secret.key` is created 0600
atomically; Seerr TLS verification defaults on; the client retry predicate actually retries.
The **Discord webhook** is now a first-class, DB-backed (Fernet-encrypted), UI-editable setting
— a new Settings → **Notifications** panel with a masked write-only input, Discord-host
validation, Save/Test/Remove, and a `has_webhook` status — closing the "config with no UI" gap;
the env var is demoted to a first-boot seed and documented in `.env.example`. Verified in a
browser end to end (validation rejects non-Discord hosts, the secret is never echoed and is
stored encrypted). Two follow-ups then landed: recovery now delivers a **code to paste**
rather than a `?token=` link (so the token never hits a reverse-proxy access log), and the
admin-password floor rose 8 → 12. Still deferred: a self-signed-Seerr TLS opt-out (verify now
defaults on, fail-closed). Note: `alembic check` fails on a **pre-existing**
`whitelist.decision` server-default drift, unrelated to this pass.

### Earlier — the delete is wired end to end (the real send, under supervision)

The executor's three stubs are gone: `_send_for_real`, `_being_watched_now` and
`_watched_since_approval` are now real, and there is a phrase-gated endpoint that executes.
Built bottom-up, each layer validated before the next:

- **Transport primitive.** `BaseClient._mutate` issues one mutating request, sets the
  `reaper_mutation_approved` extension the guard requires, and — unlike `_send` — is **not
  retried** (a retried DELETE can double-apply; the verify step, not the HTTP response, is
  the source of truth). Typed methods on top: `RadarrClient.delete_movie`,
  `SonarrClient.unmonitor_season` / `delete_episode_files`, `PlexClient.empty_trash`.
- **The real send.** Driven from the item's `media_key` through the *typed* client methods,
  never by replaying the journalled path string — replaying would re-open the
  exclusion-param footgun. A **movie**: read the tmdbId (before it's gone) → guarded delete
  with import-exclusion → re-read the exclusion list and assert the tmdbId is present *and*
  the movie 404s. A **season**: unmonitor → **verify monitored is false before touching a
  file** → resolve the season's episode-file ids live → bulk-delete → verify none remain.
  Steps go PENDING → SENT (journalled before the call) → VERIFIED (after re-reading the
  world), so a crash mid-item is recoverable.
- **The two live interlocks, fail-closed.** The streaming veto re-polls Plex per item (the
  veto set unions episode/season/show keys, so watching one episode protects the season);
  played-since-approval queries Tautulli with a one-day margin and a precise per-row
  timestamp compare. Any error, or an item with no Plex rating key, or an unreadable history
  row → **spare**.
- **The endpoint.** `POST /api/runs/{id}/execute` is the one route that deletes: deletion
  must be enabled on the host (403), the caller must echo the plan's exact content-bound
  phrase — recomputed server-side — ("REAP 7 ITEMS 214 GB", 409 on mismatch), and Plex +
  Tautulli must be present or it refuses. The scheduler never calls it.
- **A data-loss bug caught by adversarial review, before any live delete.** Two independent
  reviewers found that the executor did not re-check the manual whitelist — a spare added
  *during the grace window* (exactly what the executor exists to honour) would have been
  deleted, because a spare changes neither the frozen `condemn` verdict nor the manifest
  hash. Fixed: the planner now hashes the **whole** condemned set (so a later spare does not
  void the run), builds steps only for non-spared items, and the executor re-checks the
  override **per item** in dry-run and for real. Pinned by
  `tests/test_reap_loop.py::TestASpareIsHonouredAtExecuteTime`.

**The UI path.** The review queue's select mode grew a **"Reap now…"** action (on the "Would
reap" tab): pick items, and it builds a plan restricted to exactly those (`only_media_keys`,
`POST /api/runs {media_keys}`) and opens a confirmation sheet — a three-gate gauntlet that
auto-dry-runs (proof, sends nothing), checks deletion is armed on the host, and requires the
exact typed phrase before the Execute button lights. The Reap view got the same **Execute…**
button for the whole-condemned-set plan. One bug surfaced immediately and is fixed: the sheet
auto-dry-runs, and a dry run used to mark the run COMPLETED, so the follow-up real execute was
refused ("a run executes once"). A dry run is a *simulation* — it now mutates nothing, leaves
the run PLANNED and steps PENDING, and is repeatable; only a real run consumes the plan. Pinned
by `test_reap_loop.py::TestARunExecutesOnce`.

**First live reap, and the eventual-consistency fix it exposed.** The first real deletion —
one hand-picked movie, via the UI — *worked*: the file was removed and Radarr's import
exclusion was added (both confirmed directly against the live instance). But the run reported
ABORTED, because Radarr adds the exclusion a beat *after* the delete returns 200, and the
verification re-read the exclusion list once, immediately, and missed it — a false negative on
the canary. Fixed: `_exclusion_landed` now polls the exclusion list (a few reads with a short
backoff) before concluding it did not land; the movie-gone check stays immediate. The failure
message also now says the file *was* removed when only the exclusion is unverified, so an abort
never reads as "nothing happened." Pinned by `test_reap_loop.py` (an exclusion that lands a beat
late still verifies). Confirmed against a real Radarr: `GET /api/v3/exclusions` is a bare list of
`{tmdbId, movieTitle, movieYear, id}`, matching the client's parse.

**The after-action report is a checklist.** The run report now carries, per item, a title and a
plain-English checklist of the steps it performed — the two live interlocks (nobody watching,
not played since approval), the delete, and the exclusion verification — each a ``✓``/``✗``
rendered like the why-panel's ticks, so the owner sees every step and exactly which one failed.
A skip reads as a ``✓`` (the protection worked), not a cross. The tally is themed: *"N souls
reclaimed · X freed"*. `StepOutcome` grew `title` + `checks`, surfaced through
`RunOutcomeOut`/`RunCheckOut`; `test_reap_loop.py` pins the checklist for a clean reap and for a
delete-succeeded-but-exclusion-failed one (the "removed the file" line stays ✓ while "exclusion
confirmed" goes ✗). Also: the select-mode bulk bar is pinned to one line (nowrap + sideways
scroll on a narrow viewport) instead of wrapping.

**Post-reap Plex cleanup — no stale entries left.** The first live reap exposed this: after
Radarr removes the file, Plex keeps a stale entry until it rescans and empties its trash, and
Reaper's refresh only fired on full success — so an aborted run (file gone, but the exclusion
check failed) left a stale entry. Fixed on two fronts. (1) The refresh now fires
whenever the **file is gone**, on a completed *or* aborted run, and it is path-scoped (a Plex
section location is a prefix of the *arr path — verified true for this layout), so it can only
ever touch items under the deleted directory. (2) `emptyTrash` is now wired, at the end of the
run, doubly interlocked: it runs only when every *arr root folder reports `accessible` (the
mount is up — an unmounted volume is how a scan + emptyTrash loses a whole library), and only
on the sections a path-scoped refresh actually touched; it waits for the async scan to settle
first, and never raises (a lingering "unavailable" entry is cosmetic, a lost file is not).
Pinned by `test_reap_loop.py::TestPlexCleanup` (refresh + purge fire even on exclusion failure;
purge is skipped when a root folder is inaccessible). The one pre-existing stale entry (from
the first live reap) was removed out-of-band by a targeted, title-verified per-item delete.

`emptyTrash` is now wired (above). Still deliberately **not** wired: a **mid-run kill switch**
(arm state is read at run start; runs are kept small by the caps + first-run ratchet), and
post-reap cleanup for **TV/season** deletes (the refresh + trash purge is movies-only so far,
matching the rest of the movies-first reap loop). Both are documented in `executor.py` and are
the next follow-ups.

### This round — the keep-list is configurable

- **"Honour your keep list" became "Spare titles you've tagged."** The `reaper-keep` tag was
  hardcoded; now each policy carries `keep_tags` (a list) and `keep_tags_match` (ANY / ALL), and
  the editor shows removable tag chips + an add box + the match switch. A new `lists.ArrTagRule`
  provider fetches each tag and combines them (union for ANY, intersection for ALL). Movies read
  their Radarr tags, TV reads their Sonarr tags — and the Sonarr keep-tag sync, which never
  actually ran before, is now wired in `sync_protection_lists`.

### Newest — the review queue is paged (the whole library is visible again)

A library runs to thousands of protected titles, but `/api/candidates` returned at most 500
rows and the frontend fetched a single page — so a [redacted]-item scan showed as fewer than a
thousand (the Spared tab, ~[redacted] items, was silently truncated to 500). Fixed: the endpoint
now returns a page of `limit` rows at `offset` and reports the **full filtered set** — a count
and a byte total, measured before the page window — in the `X-Total-Count` / `X-Total-Bytes`
response headers. The frontend uses `useInfiniteQuery`, shows the server totals in the header
("[redacted] items · [redacted]"), and pulls the next page as the render window nears the end of what
it has. Grouping stays correct because seasons are grouped over the whole accumulated list, and
the score tiebreak keeps a show's seasons adjacent across a page boundary. Verified live:
scrolling grew the rendered list 40 → 301 → [redacted] with no stall, header count exact.
`tests/test_candidate_pagination.py` pins the header totals and offset paging.

### Earlier — the manual override list: spare *and* reap, by item or by whole show

The whitelist grew a `decision` column and became a general override: `"spare"` (keep, as
before) or `"reap"` (the inverse — the operator looked and wants it gone). A reap forces the
next scan's verdict to CONDEMN even when the score alone would spare it — but **never past a
hard safety gate**: `STREAMING_NOW` and `UNMANAGED` still win, as does any protection that
could not be checked. That rule lives in `snapshot._verdict(override=…)` and is pinned by
`tests/test_verdict_agreement.py::TestAReapOverrideForcesCondemnButNeverPastSafety`.

- **Whole-show overrides.** A key may be a show's (`sonarr:instance:series`) instead of a
  season's; `whitelist.effective_override` resolves a season to its own key first, then its
  show's — so "reap this whole show" covers every season, yet you can spare one season back and
  the season key wins. Verified live end to end.
- **The planner honours it in the gap before a re-scan** (`effective_override` on each condemned
  row), exactly as it already did for spares — a show spared after a snapshot froze is excluded,
  season by season.
- **UI.** Every card carries a paired **∞ Spare / ✕ Reap** toggle (click the lit one to clear);
  a red "Reap — will be removed" chip mirrors the green spared one. The override is reflected the
  instant you click — the stored verdict follows on the next scan.
- **Select mode (bulk).** A toolbar **Select** toggle arms the whole list: each card becomes a
  target you **tap to pick or press-and-drag across to paint a run** (the drag's direction is
  fixed by the first card — press an unpicked one to add, a picked one to rub out). Picked cards
  wear an accent ring and a filled tick where the checkbox used to be; the inline Spare/Reap
  buttons stand down and a floating bar carries **Select all / ∞ Spare / ✕ Reap / Clear override /
  Done**. Turning Select off (Done) is the one place selection is cleared, so it can never strand
  a hidden pick behind a bulk action. Replaced the old per-card checkboxes. Verified live: tap,
  drag-paint across movies, whole-show pick, bulk reap, and deactivate-clears.
- **Also fixed:** the keep-tags ANY/ALL dropdown now shows on the TV policy with a single tag
  (`tags.length >= 1`).

**Still open from this round (TV engine work, not part of the override list):** *(1)* always
emit every content-bearing season so their stats show, not just prunable ones; *(2)* make the
sequential-progression guard recency-aware (don't protect a "mid-binge" season if the last play
was years ago); *(3)* move the requested-but-unwatched **time limit** for TV into the scan's
scoring (today it lives only in the fairness view).

---

### Earlier — Movies and TV are tuned separately now

- **Two policies.** Movies are judged under a movie policy, seasons under a TV policy — the
  TV one carries the season-rank signal and the keep-last-N-seasons / keep-first-season floors,
  which are meaningless for film. `active_policy(session, media_type)`, a `DEFAULT_TV_POLICY`,
  and a **Movies | TV toggle** at the top of the editor. The scan applies each to its media type.
- **The snapshot records both.** Its `policy_hash` / `scoring_hash` are now the *combination*
  of the two (`policy.combine_hashes`, movie first). The simulator stays honest per media type:
  editing the movie policy recombines with the current TV policy, compares, and re-decides only
  the movie candidates — so "what this would do" shows the blast radius for the type you're
  editing, and goes stale (needs a scan) only when *that* policy's scoring changes.
- **The custom-conditions list no longer duplicates the built-ins.** "Your own rules" only
  offers fields with no built-in protection above (size, all-time watchers, vote count, season
  rank); dormancy/popularity/rating/curated/whitelist/streaming are already switches up top.

---

### Earlier — a real background scan, and rules the owner can write

- **The scan is a background job now.** It runs detached from the request that starts it
  (`POST /api/scan/start`, progress on `app.state.scan_status`, polled by `GET /api/scan/status`),
  so closing the tab or switching screens no longer stops it — verified live: started a scan, left
  the page, it kept running and progressing. The SSE endpoint is gone.
- **The policy sliders are quantified** — "a lot" became "70/100 · 70% of the score".
- **The simulator stopped flickering** (debounce + keepPreviousData) and its three states now wear
  the right clothes: a **validation failure is an error** (red, near the save button, save disabled);
  a probably-wrong-but-legal setting is a **warning** (amber); and "the last scan can't answer this"
  is **information** (neutral) — a short *Needs a fresh scan* card with a **Scan now** button that
  starts the background scan inline. "Set enabled=false" and other engineer-speak in the policy
  refusals is gone.
- **Authorable protect conditions.** This is the "conditions like Reclaimerr" ask, done the way that
  fits Reaper's thesis: **protections, not a delete engine.** The owner writes rules like *"Keep it
  when IMDb vote count is at least 1,000,000"* from the protect vocabulary; each becomes a
  `CustomProtectGate` that can only ever PROTECT or ABSTAIN (there is no CONDEMN constructor to
  reach), evaluated through the existing field registry, shown in the why-panel like any built-in
  protection, and part of the policy hash. Stored on `PolicyBody.protect_conditions`; the condemn-only
  fields aren't even offered. A mis-written rule can at worst fail to keep something.

---

### Earlier — one home for jobs, and a policy a person can read

- **Every job lives in Settings → Jobs now.** One panel: the library scan (with its progress
  bar and last-scan summary), the automatic-scan schedule, and the upkeep jobs (ratings, lists,
  history sweep) — each showing when it is next due and a **Run now** that fires it without
  changing its schedule (`POST /api/settings/jobs/{id}/run`, allow-listed to the read-only
  upkeep jobs; the scan stays on the streaming endpoint so it can show progress).
- **The Reap screen is just the plan** again — the scan control moved out; Review keeps a slim
  "Last scanned …" line so the queue's freshness is still visible.
- **The Policy editor reads like English.** "Condemn at or above 70" became "Put a title on the
  list once it scores 70/100"; the protections are "Give every title time to be rewatched",
  "Keep well-rated titles", "Keep what your users actually watch"; and every threshold is in the
  unit a person thinks in — dormancy as **"at least 3 years"**, the rating floor as **"IMDb 7.5
  from at least 1000 votes"**, popularity as **"at least 3 people"**. The live simulator is
  untouched.
- **The expand chevron went back to the left** (where an arrow reads as "this opens"), and a
  shared, fixed-width left gutter — empty on movies, holding the chevron on shows — keeps every
  poster aligned down the list. Movie cards gained a **"Movie"** tag to match the "TV" one.

*On "conditions like Reclaimerr":* Reaper's thesis is a weighted, explainable **score**, not a
boolean rule engine — that is the one thing Reclaimerr and Maintainerr already do, and the thing
Reaper deliberately does differently (see the top of this plan). Authorable **conditions belong
as protections** — "never reap if X" is exactly a gate, and the field registry already supports
authoring them (they just aren't surfaced in the UI yet). Conditions as the *primary delete*
mechanism would undermine the score that is the whole point. The right next step, if wanted, is a
UI over the existing authorable-protect rules, layered on the score — not a rule engine replacing
it.

---

### Earlier — the reasoning, said the way a person would

The why-panel and the cards were technically correct but read like an engineer wrote them.
This pass makes the *reasoning itself* legible, and adds the review controls a real list needs.

- **Durations are human now.** A dormancy of ``2060`` days no longer reads "2060 days (full
  pressure at 1825)" but **"not watched in 5 years, 7 months"**. A new ``clock.humanize_days``
  renders any day count as two significant units, and it is used everywhere a duration shows —
  the dormancy gate, the popularity window, the unwatched signal.
- **"0 distinct watchers" became "nobody watched it in the last year."** The few-watches signal
  now names the window in plain terms, and the popularity gate does too.
- **The "protections that were checked" list reads as plain statements** — "Not on your keep
  list.", "Nobody is watching it right now.", "Managed by Sonarr or Radarr." — instead of the
  "checked: …" log-line shorthand.
- **TV shows have IMDb ratings now.** Seasons were rating-blind because Sonarr's ratings are
  TVDB, but the IMDb dataset we already ingest carries a rating for the *series*; each season
  now inherits its show's IMDb rating, so a well-rated show gets the same rating-floor
  protection a well-rated film does.
- **The card leads with the *reason*, not the plot.** On the review queue the question is "why
  did Reaper judge this?", so the card shows the protection keeping it (spared) or its top
  reason (reaped), derived from the stored explanation. The synopsis moved to the slide-out,
  where it clamps to two lines with a "more".
- **A sort control, and filters as icon pills.** Score / Size / Year / Title with a direction
  toggle, server-side, with a score tiebreak so a show's seasons never scatter. Media-type and
  requested became labelled dropdown pills beside it.
- **Posters line up.** The show card's expand chevron moved off the left into the card's side,
  so every poster — movie and show — sits flush at the same left edge.
- **A whole show can be spared in one go**, and the Spare button wears an **∞** — because
  sparing means "keep this forever", and that is worth saying without words.

---

### Earlier — bugs a real look surfaced, and a card overhaul

A second review pass turned up genuine defects, not just polish. Each was confirmed against
the live library before fixing, and re-verified after.

- **Fairness sized TV as zero — and got *all* sizes from the wrong source.** The leaderboard
  read file sizes from Tautulli's `get_library_media_info`, which reports `file_size = 0` for
  every show-level row (the bytes live on the episodes), so every TV requester collapsed to
  0 GB and vanished from a board sorted by disk. The real fix was broader than TV: **the *arr
  is the authority on file sizes** — it is what downloaded and stores the file, and it is
  where the scan already reads them. Fairness now sizes movies from Radarr and shows from
  Sonarr (`statistics.sizeOnDisk`), joined to each Seerr request by the external id it
  carries (tmdb / tvdb) and keyed by the request's Plex rating key. Tautulli is only a
  fallback. Live: every requester row now carries real granted GB, movies and TV alike.
- **Snapshots never populated `group_key` or `requested_by`.** Both columns existed but every
  scan left them NULL — so TV seasons could not group under their show, and the
  "requested only" filter returned nothing. The wiring was correct in current code; the stored
  snapshots simply predated it. A fresh scan fills both: seasons now carry the show key and
  fold into one expandable card, and ~5% of movies + their requesters show a "requested by".
- **"Requested only" on the reap tab is legitimately empty — so it now says why.** Every
  requested title on this library was watched, so it is *protected*, not reaped; the filter was
  right, the blank was just confusing. The empty state now explains it and points at Spared.
- **The mobile why-panel silently never applied its own styles.** A `@media (max-width:900px)`
  override sat *before* the base `.why { position: sticky }` rule; media queries add no
  specificity, so source order let the desktop rule win and the panel stayed a cramped column
  pushed off the bottom. Moved after the base rule — on a phone it is now a full-screen sheet.

Alongside the fixes, the review card was reworked to the operator's spec:

- **The whole card is the click target** (the "Why?" button is gone), and each card carries its
  **backdrop art** — the wide Plex art (`/api/poster/{rk}?kind=art`, a new variant of the proxy)
  dimmed under a scrim, falling back to the poster where a title has no separate art.
- **The why-panel opens on a backdrop hero** that fades into the panel, with the **synopsis** at
  the top, then the verdict and the reasoning.
- **The requested filter is tri-state** (Anyone / Requested / Not requested) and stacks with
  search and media type — all ANDed, server-side.
- **Caps & grace got the unit pickers** the Limits panel already had, and the read-only banner
  no longer repeats itself.

---

### Earlier this round — acting on review feedback

A pass over the operator console fixed six things a real look surfaced:

- **Posters come from Plex, not the *arr.** The *arr artwork is stale and often will not load
  in a browser at all. Now a backend route (`api/poster.py`) proxies the current Plex poster
  through Tautulli (which holds the token), same-origin and cached a day; the candidate's
  `poster_url` is derived from its Plex rating key at read time, not stored.
- **The review list lazy-loads.** It renders a screenful and reveals more on scroll
  (IntersectionObserver), and posters are lazy — so a several-hundred-item queue stays light
  no matter the library size.
- **Deletion is turned on from the UI, gated by the admin password** — replacing the
  emergency-stop, which was a worse design of my own invention, not something the operator
  asked for. `RuntimeSafety` collapsed to one switch (`destructive_enabled`, DB-backed,
  env-seeded); turning it on verifies the local admin password, turning it off needs nothing.
  A Plex-only install can set that password in the Safety panel.
- **Numeric fields carry a unit picker** (`QuantityInput`): sizes in MB/GB/TB, durations in
  days/weeks/months/years, stored in the base unit. 14 days shows as "2 weeks".
- **The copy was audited for plain English** — the grace-clock note, the Limits blurb, the
  safety banner, the scan phases, the why-panel's unknown-signal note. No more "decoupled
  from the scan" or "caps abort a run"; it reads like a person wrote it.
- **The daily upkeep (IMDb ratings, lists) is stated as fixed** — it runs once a day and the
  configurable automatic-scan schedule does not touch it.

---

## Status

| Milestone | State |
|---|---|
| **M0** Skeleton — uv, ruff, mypy strict, Alembic (batch + naming), Docker, CI | ✅ done |
| **M1** Auth + clients — Plex OAuth + owner check, Tautulli, Sonarr, Radarr, Seerr | ✅ done — **now actually enforced**: session gate + CSRF in front of the whole API, login UI wired |
| **M2a** IMDb ratings dataset | ✅ done |
| **M2b** Curated lists (IMDb Top 250, *arr tags, Plex collections) | ✅ done |
| **M3a** Scoring engine — gates, signals, observations | ✅ done |
| **M3b** Policy persistence — immutable rows, hash, caps, autonomy grants | ✅ done |
| **M3c** Backtest — replay against the operator's own watch history | ✅ done |
| **M3d** Field registry + authorable protect rules | ✅ done |
| **M3e** Snapshot pipeline + REST API + SSE progress | ✅ done |
| **M3f** Signal quality — lift metric, size removed, dormancy gate | ✅ done |
| **M3g** Calibration — rewatch prior derived from the operator's own history | ✅ done |
| **M4** React SPA — review queue, why-panel, policy editor, live simulator | ✅ done |
| **M5** The reap loop — journal, planner, executor, canary, caps (dry-run) | 🟡 dry-run complete; live send is the last, supervised step |
| **M6** Season pruning | 🟡 guards + planner steps + **the TV scan wiring** built & tested (read-only, fail-closed) — seasons are now reviewable candidates and dry-run end to end; only the multi-step *live* execute is the supervised remainder |
| **M7a** Grace lifecycle — the cancellable countdown (DB-only) | ✅ done |
| **M7b** Leaving Soon label + Discord | ✅ done — reconcile, notifier, **and the live label write** (gated like a delete by default; `REAPER_ALLOW_UNARMED_LEAVING_SOON` opts in) |
| **M8** Profiles + scheduler | ✅ done |
| **Whitelist** — manual "spare this file" path, scan + planner + grace | ✅ done |
| **Fairness** — per-requester leaderboard (the orphaned requester rule, wired) | ✅ done |
| **Operator console** — service config, first-run setup, schedule, safety, redesigned review | ✅ done — the whole tool is now configurable from the browser |

### This round — the operator's console

Everything above could be *run* but almost none of it could be *set up* from the browser:
instances were env/CLI-only, the scheduler was invisible, there was no first-run flow, and
the review queue was a bare table. A fresh install was effectively dead on arrival. This
round closed that gap end to end, backend then frontend, and verified it live in the app.

- **Configuring services** (`api/settings.py`, `services/instances.py`): full CRUD for
  Sonarr/Radarr/Tautulli/Seerr with a **read-only connection test** that reaches the
  service and reports the reason on failure. The API key is **write-only** — encrypted on
  arrival, never returned; a view says only *whether* a key is set, and an update with a
  blank key keeps the stored one.
- **First-run setup** (`api/setup.py`, `SetupWizard.tsx`): `/api/setup/status` reports what
  is still missing, and a guided flow walks connect-Radarr → connect-Tautulli → first scan,
  offering Sonarr/Seerr/Plex as optional. It gets out of the way the moment the install is
  usable, and nags gently from a checklist until then.
- **The scheduler is now shown and configurable** (`services/scheduler.py`): the background
  upkeep jobs (ratings, lists, history) are listed with their next-run times, and an
  **optional automatic scan** can be scheduled by cron (presets + custom). A scan is
  read-only, so scheduling one is safe; the scan pipeline was extracted into
  `services/scan_runner.py` so the SSE route and the timer run the identical path.
- **The emergency stop is finally wired.** It was defined on `RuntimeSafety` but never read
  from anywhere — every construction site passed only `env_enabled`. Now
  `services/app_settings.runtime_safety` assembles both switches (host ceiling + DB stop) at
  every site, and the Safety panel surfaces the arm-state plainly. The asymmetry holds: the
  browser can only ever *subtract* — engaging the stop blocks deletion; clearing it cannot
  arm anything, because enabling deletion stays a host env flag.
- **Candidate enrichment + a redesigned review queue.** Candidates now carry a poster, a
  plain-English blurb, a year, a "requested by", and show-grouping — all captured at scan
  time from data already in hand (the *arr payloads and a Seerr join), no extra Tautulli
  sweep. The queue is now poster cards, with every season of a show collapsed into one
  expandable row, a search box, media-type and requested-only filters, and Why?/Spare as
  buttons. The copy throughout was rewritten to read like a person wrote it.

**Reaper still cannot delete anything.** `GuardedTransport` blocks every mutating
request; no execution path exists yet. The whitelist, grace and fairness work added this
round is all read-only or protective — nothing in it can remove a file.

### This round — the door had no lock

The auth *machinery* (Plex PIN client, ownership check, Argon2id local fallback, session
and recovery tables) had all been built under M1 — but nothing was wired to it. Every
`/api` route answered anyone who could reach the port, on a tool that deletes media. Now:

- **A gate in front of the whole API** (`api/middleware.py`, pure ASGI so it never buffers
  the SSE scan stream): every `/api` route needs a session, except the health probe and
  `/api/auth`. CSRF on every unsafe method — a custom header plus `Sec-Fetch-Site`.
- **The login flow** (`services/login.py`, `api/auth.py`): Plex sign-in with the ownership
  check, first-run setup that links the server and claims the owner in one step, and the
  local fallback. The browser never handles a Plex token; the backend polls.
- **Sessions** (`auth/sessions.py`): opaque, SHA-256 at rest, revocable on the spot. The
  cookie's `Secure`/`__Host-` follow the request scheme, so an HTTP-on-LAN deployment does
  not silently drop it.
- **The front end got a real front door and a face-lift**: a login screen with Plex
  prominent and a local form that slides up from the bottom, a user menu, and a refreshed
  design system (elevation, refined tokens, segmented nav) across light and dark.

Verified live: Plex OAuth end-to-end against plex.tv, and local sign-in/out, in the browser.

### Then — M6/M7/M8, and what "done" actually meant

Revisiting the three milestones a checklist had marked open showed they were in three
different states, and honesty demanded saying which:

- **M8 was already done** — the scheduler and profiles are wired. Nothing to build.
- **M7 had one real gap**: the reconcile and Discord notifier existed, but the endpoint
  hardcoded `apply=False`, so the "Leaving Soon" label never reached Plex. Now the label
  write is wired through a narrow benign-mutation path: gated exactly like a delete by
  default, with `REAPER_ALLOW_UNARMED_LEAVING_SOON` (host-only, never browser-reachable)
  to allow the reversible label to be written while read-only — because the warning must
  appear *during* the grace countdown, not once deletion is already on. Nine guard tests
  pin that the opt-in never widens what can be *deleted*.
- **M6 was genuinely unbuilt**, and it is a milestone, not a wire-up. The scan is
  movie-only; the planner skipped every non-movie. Built this round, as guarded logic
  mirroring M5's movie loop:
  - **The three guards** (`services/season_pruning.py`), pure and tested to death:
    keep-last-N by rank, keep-first-season, the sequential-progression guard (protect the
    season a viewer is mid-binge on *and* the next), never-touch-airing/downloading, and
    the keep-rule conflict detector that refuses to auto-approve "the old season is the
    good one".
  - **The planner's season steps** (`sonarr:i:series:season` keys → unmonitor → verify →
    bulk file-delete), journalled and inert.

Then the **TV scan wiring itself** (`services/season_scan.py`) — the part previously
deferred as needing live care. It reads Sonarr, runs the guards, resolves each prunable
season to its Plex season rating-key via Tautulli's `get_children_metadata`, reads
per-season watch history from the same local mirror the movie path uses, and judges each
season through the **same** gates + signals engine — the season-pruning guard merged in as
one more gate result, so a protected season is protected exactly as a streamed movie is.
The executor's dry-run now walks a season's three-step sequence as one delete unit. It is
**read-only and fail-closed**: a season Reaper cannot confidently resolve in Plex, or whose
arrival date it cannot read, gets `Unknown` facts and can only ever *abstain* — never
condemn; a duplicate show title or a duplicate season number refuses to guess.

An adversarial multi-agent review of the diff caught real fail-opens before commit — the
worst being that dormancy was first measured from the *show's* added date, so a freshly
backfilled season of an old show read as decades dormant and was condemnable under the
default policy. Fixed: dormancy is floored on each **season's own** arrival date, mirroring
the movie path's own-item discipline, with a missing date falling to `Unknown` (protect)
rather than the horizon. Five other confirmed findings (a lone-title year mismatch, a
duplicate-season-number collision, a per-step plan overcount, a season leaking into the
movie-scoped Leaving Soon label) were fixed the same round, each with a regression test.

- **Still deferred, because it needs a live server to get right**: the multi-step *live*
  execute (unmonitor → verify → bulk-delete against a real Sonarr). Building it blind is the
  exact class of mistake that deletes the wrong thing — so it waits for supervised,
  live-verified work, precisely as M5's live movie send does.

---

## Assumptions that were wrong

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

⇒ One helper (`app_settings.runtime_safety`) now assembles both switches, used everywhere a
client or a health check is built, with a test asserting an engaged stop actually flips the
effective permission (and that clearing it cannot, alone, arm).

---

## Decisions locked

| Decision | Choice |
|---|---|
| Condemn logic | **Flat AND** of typed conditions. No OR, no nesting, no NOT. |
| Protections | **Gates with no CONDEMN constructor** — structurally cannot delete |
| Protect authoring | **Catalogue + user-authored protect rules** (safe: worst case is nothing deletes) |
| Signals | **Unsigned**, fixed denominator including unknown weights |
| Observations | **Known / Absent / Unknown** — never conflated |
| Delete mode | DB-only grace period → cancellable → then irreversible |
| Autonomy | An **earned grant keyed to `policy_hash`** — any edit reverts to approval-required |
| Caps | **Four**: items + bytes, per-run + rolling 30-day |
| Kill switch | **One-way**: the UI can disable deletion, never enable it |
| Backtest | **In v1** — the only thing that makes threshold-tuning real |
| Auth | Plex OAuth + `owned == true` check, local fallback that cannot be removed |
| Migrations | **One baseline until first release.** The dev DB is disposable; after v1 ships, every schema change is additive. |

---

## What live testing established

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

### ⚠️ The population trap — got this wrong twice

A base-rate baseline computed over the wrong population does not give an imprecise
answer, it gives one with **the wrong sign**. Twice this produced a confident, wrong
conclusion ("the scorer is worse than random"), and it nearly caused a working feature
to be ripped out.

Compute the baseline over **exactly** what the scorer scores. See `docs/SIGNALS.md`.

## Where the pipeline stands

A full scan of a large library completes in tens of seconds, streaming progress over
SSE, and produces a candidate list partitioned into condemn / protect / abstain.

The why-panel renders for **keeps as well as deletes** — an item can score high enough
to be condemned on score alone and still be protected by a gate, and the panel says so
in as many words, with the numbers that produced the verdict:

```
Example Movie  (5.9 GB)
VERDICT: CONDEMN   score 91/100  (threshold 70)

  +70.0/70   unwatched for 2059 days (full pressure at 1825)
  +20.0/20   0 distinct watchers
  + 1.0/10   IMDb 5.4

  ✓ checked: dormant long enough -- 2059 days, your floor is 1095
  ✓ IMDb 5.4 from 6,000 votes -- below your 7.5 floor
  ✓ checked: popular here -- 0 distinct watchers in the last 365 days, your floor is 3
```

A tool that only explains its deletions cannot be trusted about its keeps.

## The three Plex facts — verified against a live server

Answered by a read-only probe over the real PIN flow. All three had to be settled
before the reap loop could be built on them.

1. **Does `batchMultiEdits().addLabel()` preserve existing labels? — YES.** Adding a
   second label leaves the first in place. So the "Leaving Soon" mark does not wipe the
   labels an owner put on their own media. This was the most dangerous open question and
   the answer is the safe one. Asserted in `clients/plex.py`, not assumed, because a
   future Plex change here would silently destroy user data.

2. **Is the stored token narrower than the account token? — NO.**
   `resource.accessToken == account.authToken`. The token plex.tv issues for an owned
   server *is* the account credential. The README says so plainly; it is not documented
   as a security control, because it is not one.

3. **Partial refresh by path is accepted**, and the library's section paths were read
   for the path-mapping table. The full refresh + `emptyTrash` *sequence* is deliberately
   **not** verified end to end: proving it means deleting a real file, and an unmounted
   library + scan + `emptyTrash` is how whole libraries are lost. That stays a
   hand-picked first delete under supervision, never an unattended probe.

Two findings fell out of the probe, both now encoded in the client:

- **Plex title-cases label tags** (`leaving-soon` → `Leaving-Soon`). Every label
  comparison is casefolded; a case-sensitive one silently fails to find a label that is
  present. (My own probe was bitten by exactly this — it left two labels behind because
  its cleanup matched case-sensitively.)
- **plexapi is `requests`, not `httpx`**, so it bypasses `GuardedTransport` entirely.
  A `GuardedSession` twin now enforces the same rule, or `emptyTrash` would have been the
  one destructive call with no interlock.

## The reap loop (M5) — dry-run complete

The planner and executor exist and were exercised end to end against the live library:
a plan of **419 delete steps** was built from the real condemned set, the canary
correctly ordered first, and a dry run walked every step — recording the exact Radarr
call each would make — while sending nothing and deleting nothing.

The interlocks, all built and tested:

- **Journal-before-send.** Every `ActionStep` row, with its exact method/path/body (no
  credentials), exists before any call. That row is also the executor's declaration to
  the transport guard, so "journalled before sent" is enforced, not intended.
- **Manifest re-check.** Approval binds to a content hash of the condemned set; if the
  library moved, the run voids rather than executing a plan nobody approved.
- **Caps ABORT, never truncate** — a breach stops the whole run, so which items die can
  never depend on sort order.
- **The canary** is ordinal 0, the smallest item, and a canary failure halts the run.
- **Two independent guards.** `dry_run` (the executor's own default) and the transport
  guard (a host property no browser can reach). Neither is trusted alone.

**What is deliberately not built:** the live send path raises `NotImplemented` rather
than being half-wired. Per the plan, the first real delete is a supervised, hand-picked
file — not something reached by flipping a flag. Wiring `_send_for_real`, the
active-stream re-poll, the watched-since-approval check, and the trash interlock to the
live clients is that step.

## What this round added (all read-only / protective, no delete path)

- **Manual whitelist.** The media-key "spare this file" path was an empty set passed into
  every evaluation; it is now a real `whitelist` table, consulted in the scan (a spared
  key judges PROTECT), the planner (excluded even from a frozen snapshot's condemned set),
  and grace (a cancelled item leaves the countdown at once). Spare/un-spare on every
  condemn row. Un-sparing deletes nothing — it lets the file be judged again.
- **Fairness view.** `engine/requester.py` was complete and tested but *orphaned*. It now
  powers a per-requester leaderboard: requests made, disk granted, share ever played, disk
  nobody watched. Every join hangs off the Plex rating key; the evidence query matches both
  `rating_key` (movies) and `grandparent_rating_key` (a show's episodes). Read-only —
  surfaces the rule's findings as information, not as a second condemn path.
- **Grace lifecycle.** The cancellable countdown, derived from `first_flagged_at +
  grace_days` (one source of truth, nothing to store). Items partition into counting-down
  and cleared; cancel = spare. Panel on the Reap view.
- **Leaving Soon + Discord (M7b).** During grace, Reaper announces titles two ways.
  *Discord* is the real channel: a best-effort webhook notifier (failures never raise into
  a scan; the URL is a credential and stays out of the logs). *Leaving Soon* is a Plex
  label reconcile that tracks the grace set — the reconcile is pure and tested, reading
  current labels is a read-only GET, but writing the label is a guarded mutation, inert
  until armed (and, unlike a delete, not journalled, so the live write is a supervised
  step ahead). `Candidate` now keeps `plex_rating_key` so grace items are addressable in
  Plex. POST `/api/leaving-soon/sync` + a button on the grace panel.

## Immediate next steps

1. **The live send** — wire `_send_for_real` + the exclusion-verify + the Plex refresh
   and trash interlock, then delete one hand-picked worthless file under supervision.
2. **Verify M7b against a real server** — the Leaving Soon reconcile and the Discord
   notifier are built and unit-tested, but neither has run against a live Plex/webhook
   (Plex is unlinked in the dev DB, no webhook configured). The live label write also needs
   a decision: it is a benign, reversible mutation currently gated as strictly as a delete.
3. **Plex settings UI** — `reaper-admin link-plex` works from the CLI; the web setup
   wizard still needs the same flow. (Plex is not linked in the dev DB.)

### Open questions / decisions to make

- **Should the planner gate on grace?** Today a plan is built from *all* condemned items;
  a grace-aware planner would include only cleared ones. That is the honest end state, but
  it empties the plan until items age out of grace, so it changes the current demo. Held
  deliberately — it is a behaviour decision, not an oversight.
- **`FEW_WATCHERS` and `LOW_RATING` earn nothing.** Consider dropping them; they add hash
  surface and bug surface for no measured skill. (A scoring change — delete-adjacent.)
- **Tests are not type-checked.** CI runs `mypy src/reaper` only; `mypy tests` reports ~190
  errors, almost all "missing py.typed marker" noise. A PEP-561 `py.typed` marker plus a
  handful of real fixes would let the suite be strict too.
