# Reaper — Living Plan

> Updated as work proceeds. Records not just what is done, but **which assumptions
> turned out to be wrong** — because on this project, most of them did.
>
> Reaper is written as software other people will run against libraries we have never
> seen. No number in this repo describes anyone's actual server; findings from live
> testing are recorded as ratios and shapes, never as fingerprints.

Last updated: 2026-07-23 (httpx -> httpx2 migration STARTED: first isolated slice shipped,
scope corrected against the real code, sequencing rethought)

### Newest — httpx is unmaintained; httpx2 migration started (first slice shipped)

Test-suite CI work (sleep-patched tests, xdist, a sqlite-engine-leak fix) surfaced a
`StarletteDeprecationWarning` pointing at `httpx2`. Checked it out: upstream `httpx` (encode/httpx)
has had no release since 2024 and is effectively unmaintained. Pydantic Services Inc. has taken over
stewardship under a **new package name**, `httpx2` -- not a `httpx>=2` bump, because a same-name
major version would silently break every pinned install. Same design, same maintainer lineage
(explicitly "a continuation of the wonderful work started by" the original author), not a fork.
Starlette itself migrated in 2026-05; that migration is what put the warning in our CI log. Fixed
cheaply: `httpx2` is now a **dev-only** dependency so Starlette's `TestClient` stops falling back to
the deprecated path (`pyproject.toml`, `dev` extra) -- that part is done and shipped.

**Shipped (first slice): `notify/discord.py` moved to `httpx2`.** It is the one production HTTP
client that is genuinely isolable (see the decomposition finding below), so it is the safe place to
prove the whole round trip -- production on `httpx2`, tests on `pytest-httpx2` -- without touching
the deletion path. In the same change: `logging.py`'s `_NOISY_LOGGERS` gained `"httpx2"` and
`"httpcore2"` (this is a **security fix, not a cleanup** -- see below), `pytest-httpx2>=1.0.0` was
added to the `dev` extra, `test_discord.py` was rewritten onto the `httpx2_mock` fixture, and a new
`tests/test_transport_httpx2_parity.py` pins that httpx2's `AsyncBaseTransport` extension point (the
mechanism `GuardedTransport` depends on) behaves identically -- proved before the guard itself is
ported. `discord.py` does NOT go through `GuardedTransport` (it posts to Discord, it mutates no
library), which is exactly why migrating it first is safe.

**Findings that corrected the earlier scope (verified against the installed packages and the code):**

- **The client layer does NOT decompose the way the first scoping assumed.** `clients/base.py`'s
  `BaseClient` is the shared transport for **seven** client classes (`PublicClient`, `TautulliClient`,
  `PlexTvClient`, `ArrClient` -> `SonarrClient`/`RadarrClient`, `SeerrClient`), so the moment
  `base.py` builds an `httpx2.AsyncClient`, every one of those clients is on httpx2 at once -- and
  respx, which speaks only httpx, can no longer intercept any of them, so **every respx test that
  touches any client breaks simultaneously.** `services/instances.py` must move in lockstep: its
  connectivity classifier walks the exception `__cause__` chain with `isinstance(e, httpx.*)` over
  eight httpx exception types, and an httpx2 exception matches none of them, silently misclassifying
  every failure into the wrong operator message. And `test_guarded_transport.py` -- the test that
  proves the safety model -- constructs `RadarrClient`/`SonarrClient`/`TautulliClient` directly, so
  it cannot be rewritten onto httpx2 in isolation either. **Net: `base.py` + the six client files it
  backs + `instances.py` + ~10 of the 11 respx test files are ONE atomic unit**, not the
  "base.py first, then the other five, then the tests" sequence first written here. The only
  production files that migrate independently are `notify/discord.py` (its own client, shipped) and
  `services/imdb_dataset.py` (its own client, its own test).
- **`_NOISY_LOGGERS` is a cleartext-secret-leak control, not noise suppression.** `logging.py`
  pins the HTTP libraries' stdlib loggers to WARNING because at INFO httpx logs
  `HTTP Request: GET https://host/...?apikey=SECRET`, and Tautulli/Plex/MDBList carry their key in
  the query string while a **Discord webhook carries its token in the URL path**. httpx2 renames its
  loggers to `httpx2`/`httpcore2`; move a client to httpx2 without adding those names to the tuple
  and the operator's credentials start writing to the log in cleartext. So the tuple update is a
  same-commit obligation for **every** client that crosses over, not a follow-up. Done for the two
  httpx2 names now; pinned by `test_logging_quiet.py`.
- **`pytest-httpx2` is respx-flavored, as the earlier scope said.** Its `httpx2_mock` fixture is a
  real `respx.Router` (`.post(url).mock(...)`, `.calls.last.request`, `side_effect=`), wired to
  httpx2/httpcore2 by a plugin-provided transport mocker. One sharp edge learned by doing it: the
  mocked `return_value` must still be an **`httpx.Response`** (respx's own currency, which the plugin
  hands to the httpx2 client), while a `side_effect` exception must be an **`httpx2`** one (so the
  client raises the type the code under test catches), and the request recorded in `route.calls` is
  an `httpx.Request`. So per file the rewrite is lighter than "port to a different mocking library":
  mostly `@respx.mock` decorator -> `httpx2_mock` fixture param, plus swapping exception types. Still
  11 files touched, but the Router API itself is unchanged.
- **`clients/plex.py` is not a code site.** Its three `httpx` mentions are all in comments
  explaining that `GuardedSession`/plexapi does *not* speak httpx; the file imports no httpx symbol.
  `GuardedSession` stays out of scope -- plexapi sits on `requests`, untouched by any of this.
- **TLS verification default changes** from certifi's bundled CA list to the OS trust store: httpx2
  hard-depends on `truststore>=0.10` (and `httpcore2==2.9.0`). Every client's `verify` parameter
  (`clients/arr.py`, `clients/plex.py`, `clients/base.py`, ...) is a plain `bool`, never a
  custom-CA-bundle path, so the only two states an operator can be in today are "verify against
  public CAs" or "don't verify at all" (the self-signed-instance escape hatch, `verify_tls=False`).
  That narrows the real risk to one case: an operator behind a corporate MITM proxy whose CA is
  OS-installed but not in certifi's bundle -- for them the OS-trust-store default is arguably *more*
  correct, but it is a real behavior change and needs a real end-to-end check against a live
  self-hosted instance before it ships, not just a reading of the docs.

**Corrected sequencing for the rest (the big, atomic piece):** the `TestTlsVerificationReachesTheTransport`
test in `test_guarded_transport.py` monkeypatches `reaper.clients.base.httpx.AsyncHTTPTransport`, so
it also moves in lockstep. When the BaseClient cluster is done in one pass: port `base.py` +
`instances.py` + the six client files together; rewrite all their respx tests onto `httpx2_mock` in
the same change (the parity probe already shipped proves the transport subclass holds); the
`_NOISY_LOGGERS` names are already in place; then `services/imdb_dataset.py` can go on its own; then
a real end-to-end TLS check against a live self-hosted instance; and only then drop `httpx`/`respx`
once nothing imports them. `httpx` 0.28.1 still installs and runs correctly today, so the remaining
work is about closing off future security-update coverage, not an active break -- there is no
deadline pressure to rush the atomic cluster.

### Newest — Backup & Restore, phase 2: restore + install-from-backup (shipped)

Restore is a deliberate **stage-and-restart**, never a live swap. `POST .../restore/prepare` streams
the uploaded archive to disk (raw request body, not multipart, so no new dependency and it never
buffers whole in memory), unpacks only the four known members to fixed names (a crafted tar can't
escape the staging dir; a decompression bomb hits a per-member cap), and runs the **schema gate**:
the backup's Alembic revision must be one this binary ships (walked from `alembic/versions` via
`ScriptDirectory`), else it is refused 409 as "made by a newer Reaper" -- fail closed, because this
build could not serve a schema it doesn't have. An accepted upload is staged **un-armed** (no READY
marker). `POST .../restore/confirm` verifies the admin password behind the *same* lockout + Argon2
gate as arming deletion, then forces `destructive_enabled=false` inside the staged DB and writes the
READY marker **last** (the marker is the arm; a crash before it leaves the staging inert). The swap
runs in `preflight` (before `alembic upgrade head`): if READY is present and the staged DB reads as
SQLite, the live DB + WAL + key + salt move into a timestamped `pre-restore-*` recovery dir and the
staged files take their place; `alembic upgrade head` then brings an older backup current. The
operator restarts the container to finish -- true for every install type, so the copy just says so.
Every ambiguity keeps the live data: an unreadable staged DB is discarded, not swapped.

The whole chain was driven end-to-end in throwaway dirs: a credential encrypted on a *source* host
decrypted on a *different target* host after restore (the portability claim, proven, not assumed),
the target's own key could not read it beforehand, deletion came back OFF, and the old data survived
in the recovery dir. Then driven in a real browser against a real backup: upload -> validated
summary ("Matches this server") -> password -> armed banner -> cancel clears the staging. Restore
routes are off the API-key lane (unsafe methods outside the automation allowlist). New:
`services/restore.py`, three routes in `api/backup.py`, the `preflight` swap, `RestoreCard` in
`BackupPanel`, `restorePrepare/Confirm/Cancel` in `api.ts`. No schema change: the last-backup time
is still a value row in `app_setting`, so `alembic check` stays clean.

**Restart decision (resolved):** manual container restart, not auto-exit. Reaper is one container
with no assumption about the operator's restart policy; "restart to finish" is honest and works for
any future install type. An auto-restart button relying on `restart: unless-stopped` was considered
and declined.

### Newest — Backup & Restore, phase 1: the download that is actually restorable

The ask was backup *and* restore, and the feasibility hinge was encryption: if the at-rest key were
machine-bound, a restored database on a new host could not decrypt a single credential. It is not.
The key is two portable files (`data/secret.key` + `data/secret.salt`, see `reaper.secrets`) with no
hostname or fingerprint, so a backup that carries them decrypts anywhere. That makes restore worth
building, and shaped the bundle.

**Phase 1 (shipped): the download.** `GET /api/settings/backup/download` streams one gzip tar --
`manifest.json`, a `reaper.db` snapshot, and `secret.key`/`secret.salt` when they exist as files
(absent when `REAPER_SECRET_KEY` is env-supplied; the manifest records `key_source` and the panel
warns). The cache database is left out: it is not a source of truth and rebuilds on the next scan.
The snapshot is `VACUUM INTO` -- a consistent read inside one transaction, so a scan writing
mid-backup cannot tear it, and it compacts: a real live DB became an archive roughly **11x**
(~11x), which is why the panel shows no size number (the honest compressed size is not knowable
without building, and showing the uncompressed one misleads by 10x). The manifest stamps the Alembic
revision (`alembic_version`) for the restore side to gate on. `manifest.json` is written first in the
tar so a restore can read it from the front of the stream without unpacking the database behind it.

Fenced hard: the download hands over the whole database **and** the master key, so it is on the
API-key read-denylist (`api.middleware._API_KEY_READ_DENY`) -- a leaked automation key can read
plenty but never pull the keys to everything -- and, like every `/api` route, needs a session.
Built off the event loop (`asyncio.to_thread`); the archive streams from a temp dir removed when the
stream ends; downloading records `last_backup_at` for the panel. New UI: a `Backup` settings tab and
`BackupPanel`, mocked as an artifact and approved before code, then driven end-to-end (real [redacted]
DB -> valid [redacted] archive, all four members, manifest revision == head, key+salt bundled, light +
dark). New: `services/backup.py`, `api/backup.py`, `app_settings.get/set_last_backup_at`. The About
page's DB sizing now delegates to the one `backup.db_size_on_disk` so the two never drift.

**Phase 2 (shipped):** see the entry above. Restore now stages an uploaded backup and swaps it in on
the next restart, gated behind the admin password. The "same path serves first-time setup" idea
holds -- the routes are install-state-agnostic -- but the setup-wizard entry point is not yet wired;
today restore lives on the Backup tab. Follow-up if setup-time restore is wanted.

**Gates:** ruff/mypy clean, 1965 backend tests (+23 restore), 220 frontend tests, alembic check
clean, frontend lint/build clean. Both phases driven end-to-end in a real browser.

### Earlier — the HD/4K library map tells a duplicated title's two copies apart

The reported regression turned out not to be one: the third review pass (`4a52576`) deliberately
removed the path-based show disambiguation after 60k randomized cases showed it bound the *wrong*
copy, and documented that a duplicated show must abstain. The scanner optimization was not
involved. The fix is a new signal, not a revert: each Sonarr/Radarr **root folder** is mapped, in
the edit-instance modal, to the **Plex library** its content lands in (suggested from the folder
paths, operator confirms). Keyed on the root folder, not the instance, so one instance feeding many
libraries just maps each folder; many instances feeding one library is many-to-one.

Stored as `instance.plex_library_map` (a nullable JSON column, additive revision
`add_instance_plex_library_map`; NULL reads as "no map" and keeps the old abstain-and-keep). At
resolve time the map narrows an ambiguous id to the mapped library **before** the folder and size
corroborators (`identity._narrow_among_id_hits`), because it is the operator's declaration, not an
inference. It only ever narrows the id's own candidates and stands down -- keeping the file -- on
every untrustworthy shape (a candidate whose library is unknown, byte-identical twins, or a mapped
library holding none of the copies); a movie's size still vetoes a contradicting map. A stale or
renamed mapping is surfaced as a `scan.stale_library_map` log warning, never a silent mis-bind. Two
copies in the *one* mapped library still abstain. Endpoint `GET /instances/{id}/root-folders`
returns the folders with a suggested library each; the map rides `updateInstance`.

### One filter control, a library label, and additive migrations

Two operator asks plus a policy change. **The filter row stopped growing.** The review queue's
four fixed dropdowns (type, Seerr, genre, override) collapsed into one **＋ Filter** control: a
menu adds any dimension, each active filter is an editable, removable chip, and adding a future
filter is one entry in a registry (`FilterDimension` in `ReviewQueue.tsx`), never another toolbar
button. Sort stays its own control -- it orders, it hides nothing. The popover is a proper
anchored menu (absolute inside a relative wrapper, so it follows its chip on scroll) that closes on
outside-click or Escape; the mockup's viewport-anchored version floated on scroll, which the React
one does not. **The Plex library is now shown.** Every card, season, and info panel carries a quiet
neutral chip (the res-badge family, never a verdict color) naming the library the file lives in, so
movies and seasons read the same; it is also one of the filters.

The library title is captured at scan time onto `Candidate.library_title`, riding the same rails as
`content_rating` -- per-item for movies via `Display`, show-level for seasons via `show_library` --
sourced from the plexapi sweep's section title with the Tautulli spine's `section_name` as the
fallback. Exposed as `library` on the candidate/group API with a `library` query filter, plus
`vocabulary/values?field=library` for the picker.

**Migrations are additive from here.** Testers now run Reaper with real data, so the single-baseline
"rewrite in place, rebuild the DB" rule is retired. The baseline (`22777b2b5015`) is frozen; every
schema change is its own additive, non-breaking revision chained onto head. This feature's column
shipped as the first such revision (`add_candidate_library_title`, a nullable `ADD COLUMN`): an
existing row reads NULL as "unknown library" and the next scan backfills it, so nobody rebuilds.
The rule is updated in CLAUDE.md and the migrations row below.

### The caps are switchable, and a toggle that did nothing is gone

Two operator questions in one pass. First: a dry run aborted at "569 over the per-run cap of 10"
even after the operator thought they had turned limits off. They had not: the only toggle in
*Pace and limits* was **"Ask me before every run deletes anything"** (`require_approval`), which is
a different control. Second, following from that: what does that toggle even do?

**The assumption that turned out wrong.** `require_approval` looked like a safety gate. It gated
nothing. Traced across the whole codebase (adversarially, twice): its entire footprint was one
policy-editor warning when off, plus being stored and echoed. The execute route gates on the host
being **armed** (password) and the **typed confirmation phrase**, never on `require_approval`; the
executor, planner, and scheduler never read it. Its own docstring admitted it was a placeholder for
an "earned AutonomyGrant" flow that is not wired. So the toggle described behavior that is *always*
true and cannot be turned off (Reaper never deletes on its own), and implied an "off" (unattended
deletion) that does not exist. Removed entirely (rule 25/38).

**The caps became optional, on by default.** New `ProfileSettings.caps_enabled` (un-hashed, like
the caps themselves). Off, `executor._check_caps` and `_check_rolling_caps` stop enforcing the four
run-size caps -- for a big first cleanup -- while every other gate stands: the deletion password,
the confirmation phrase, the manifest re-check, the canary, the live per-item vetoes, and the
**separate** unknown-size allowance, which the switch never touches (its check runs before the caps
early-return). The four volume caps live only in the executor (the planner enforces just the
unknown-size allowance), so gating there disables them completely; dry-run and execute agree
because both walk the same `_check_caps`. The abort copy now points to *Policy → Pace and limits*
and names the off switch, instead of only "raise the cap." In the UI the caps matrix is hidden (not
disabled) while the toggle is off, with an amber "no cap on run size" notice; grace and unknown-size
stay put. Drove it end to end: on the real 569-item set, a dry run aborts with caps on and
**completes** with caps off; the old approval toggle is gone.

**Upgrade-safe, without a migration.** Removing a field from a Pydantic model with
`extra="forbid"` would otherwise crash every read of an *existing* profile, whose stored
blob still carries `require_approval` -- and `active_profile_settings` feeds scans, execute,
grace, the shelf, and the very settings page used to fix it. So it now degrades exactly like
`active_policy` already did: it drops unknown keys and re-validates (keeping the operator's
caps and grace, defaulting `caps_enabled` on), and falls back to the cautious built-ins on an
otherwise-unreadable blob. No Alembic change (the column is unchanged; `alembic check` clean).
Proven live: a legacy blob returns 200 with values preserved, not a 500. Found by an
adversarial review pass over the diff, which otherwise confirmed the caps gating fails closed
on every axis (dry-run/execute agree, the unknown-size guard survives caps-off, no other
interlock weakened).

### The Reap page stopped being a grace list, and became a breakdown

The operator pushed back on the Reap page's grace panel: it listed only 20 of ~570 counting-down
titles, each with a *cancel* link -- an arbitrary slice, and *cancel* was just "spare this file",
the exact action the review queue already owns. Two questions: why a partial list you can manage,
and why that ability here at all.

**The assumption that turned out wrong.** The grace countdown reads like a gate -- "waits out 14
days before it is eligible to be reaped" -- but `build_plan` and the executor never check
`first_flagged_at`. Grace is a wall-clock heads-up (and what drives the Leaving Soon shelf), not a
code gate on the plan. Rather than wire the gate, the operator chose to keep grace informational
and stop the copy implying otherwise.

**The rebuild.** The grace panel is gone from the Reap page. In its place, `ReapBreakdown` shows
what a reap would remove: the ledger (condemned by policy, minus hand-spares, plus hand-reaps, the
net), the movie/season split, and *why the policy condemned them* -- a participation tally over
each condemned row's frozen `signals` (a title trips several at once, so the counts overlap; never
a partition). New read-only endpoint `GET /api/reap/breakdown` over `services/breakdown.py`, built
from the same `effective_condemned` set the planner acts on, so the ledger total matches the
confirmation phrase. The by-signal tally mirrors the simulator's `_fired_gates`/`protected_by`
precedent, reading `state == "adds"` (with a positive-contribution fallback for pre-`state` rows).

**Leaving Soon's manual update moved to Settings → Plex.** Its on/off toggles already lived there;
the "Update now" action and status followed, and a library that fails to sync is written to the
logs (`leaving_soon.problems`), not shown inline. The Reap page keeps one line pointing to it. The
`/api/grace` endpoint and `services/grace` stay (the shelf still uses the service); only the Reap
page's use of them is gone. Drove it end to end: on real data, 595 condemned − 26 hand-spared =
569 net, and the by-reason bars render from stored signals.

### You could not un-decide a TV season, only movies

The operator reported it plainly: on TV shows you could no longer un-select a spared or reaped
item; on movies it still worked. An audit (five parallel readers, each finding adversarially
verified) confirmed the shape and found its twins.

**The bug, one sentence:** a season's Spare/Reap control DISPLAYED the *effective* override --
own, or inherited from a whole-show spare -- but its clear/set acted on the season's OWN key, so
when a show-level spare was what kept the season, clearing the season key changed nothing. A
movie has no show, so its effective override IS its own key; that is why movies always worked.
The real data had 5 whole-show spares, so the operator hit it immediately.

**The twins (same class), all fixed together:**

- The whole-show control on the card *and* the show panel had the mirror bug: it lit from an
  *aggregate* of the seasons' effective overrides but cleared only the show key, so a show whose
  seasons were all overridden one by one lit "Spared" and could not be cleared.
- The bulk "Clear override" on a selected show read the same aggregate; it now agrees with the
  card once the card reads the show's own decision.

**The fix — a control reflects and acts on its own level** (new rule 50). Three views ride on
every candidate, built once in `_candidate_out` / `GroupOut` from the one
`whitelist.effective_override` + `show_key`: `override` (in effect -- colors the row),
`override_own` (the item's own decision -- the only value a control toggles), and `show_override`
(the show's own decision -- lights the whole-show control). A season control clears the season
key; a whole-show control clears the show key; each can only ever reverse what it lit. When a
whole-show decision keeps or reaps a season, `KeptByShowNote` names it beside the season's
control -- its wording turning on whether the season's own decision is absent, the same, or
opposite (the last one caught in the mockup: a season reaped against a spared show now reads
"you reaped this season, so it will be removed," not "also spared"). A season-level clear never
un-decides the whole show: that would strip protection from every other season (fail-open), so
it resolves toward keeping the file, and the operator undoes a whole-show decision on the show.
`groupOverride` (the old aggregate) is deleted.

The wrong assumption this time: "effective state is what a control shows, so it is what a
control clears." Display and action were reading different keys.

**Plus one safety-adjacent grace bug (from the same audit).** `_sync_grace_clocks` kept a
scan-condemned item's grace clock even while it was spared, and never reset it on un-spare -- so
un-sparing an old condemnation re-entered it with a weeks-old clock that dropped it straight past
grace with no Leaving Soon warning. Now the clock is keyed on the *effective* reap-list
membership: a spare takes the item off the list and deletes its clock, so a later un-spare earns
a FRESH window (rule 4). The deliberate spare is a real departure, not a transient scan outage,
so the delete forces the reset that `_apply_first_flag`'s gap heuristic would not.

Verified against a copy of the operator's real DB through the production `_candidate_out`: 58
inherited seasons read effective=spare / own=None / show=spare, and the seasons spared on their
own key read own=spare. Two audit findings deferred by choice: two lesser grace-clock edges and
the bulk "N selected" count (a show card standing for many seasons). All gates green: ruff,
mypy, 1707 pytest, alembic no-drift, eslint, 122 vitest, vite build.

### The show card judged half a show

Three things the operator caught on the Condemned lane, mocked as an approved artifact then
driven live in their own browser.

1. **The whole-show controls read only the condemned lane, not the whole show.** The card
   computed `showReapIsNoop`, `groupOverride`, and `groupReapEffective` over `group.items` --
   the tab-filtered page, which on the Condemned lane holds *only* a show's reaped/condemned
   seasons. So they all agreed "reap/condemn," which (a) hid the whole-show Reap even though the
   show had kept seasons a whole-show reap would take, and (b) lit the whole-show control as
   "Reaping" when only *some* seasons were reaped. The panel had it right all along (it passes
   `group.seasons`). Fix: the card judges over `group_seasons` (the strip marks, `showSeasons`),
   every lane. The wrong assumption: "the seasons on this tab are the show." They are not.
2. **One amber meant two different things.** A reap the engine can't honor yet ("held") wore the
   same amber as "left for you to decide" -- the refused-reap chip literally reused the
   `status-look` class. The operator read a held reap as undecided. Fix: held reaps are now
   **dashed red with a scythe** (still in the reap family they chose, hollow and dashed so it
   never reads as a done removal), and amber is reserved for "left for you to decide" alone.
   Consistent across movies (resting `OverrideMark` scythe) and seasons (strip corner-mark);
   rule 49 updated.
3. **The season-square hover was a hairline** and vanished on the solid fills (a spared
   show's green squares especially). Now the square lifts, grows, and gets a full accent ring
   with a soft shadow, so it reads on every fill. Transform/outline/filter sit outside layout,
   so nothing reflows. Keyboard focus matches it.

All gates green: eslint, 115 vitest, tsc, vite build.

### Earlier — one spelling, American, everywhere

Swept the whole tree from British to American spelling (color, behavior, honor, normalize,
judgment, canceled, labeled, gray, license, defense, center, …), 404 edits across 99 files,
including identifiers (`normalize_label`, `labeled_in_section`, `SeasonJudgment`). Two tokens
were left British because their spelling is owned elsewhere: `asyncio.CancelledError` (stdlib)
and the ARIA `aria-labelledby` attribute. Word-boundary matching protected both automatically
(the British stem is glued to a following letter, so no whole word matched). The convention is
now a golden rule in `CLAUDE.md`, so future work stays consistent. All gates green after the
sweep: ruff, mypy, 1702 pytest, eslint, 112 vitest, vite build, no alembic drift.

### The number never followed the hand

Two display bugs the operator caught on one card, same root cause. The score badge and one
strip square colored themselves from Reaper's *scan verdict* and never looked at a hand
override. So a row could read "Reaped by hand · will be removed" while its number stayed
green (a scan `protect`), and a reap the engine *held* ("Reap requested · kept for now")
left its strip square plain gray, indistinguishable from a season nobody touched. Mocked as
a rendered artifact, approved, then driven end-to-end in a real browser.

- **A cell's color now follows the item's fate, not just the first read.** One shared helper,
  `handFate` (`components/ReviewQueue.tsx`), maps override + effectiveness + verdict to a color
  token, and both the score badge (`Score`, used on cards and in `ShowPanel`) and the season
  strip (`SeasonStrip`) go through it, so the two can never disagree with each other or with
  the row's chip.
- **Solid for a decision that takes effect, amber for a reap that was held.** An effective hand
  reap is solid red, a hand spare solid green (the "you chose this" solid language the chips and
  strip overrides already spoke). A reap the engine refused is amber — the same `--unknown` tone
  the row's `chip-reap-refused` wears — because the file is *not* going anywhere, so it must never
  wear the solid red that means "removed." New `.score-reap/.score-spare/.score-refused` and
  `.strip-ov-reap-refused` classes carry it, defined after the scan-verdict classes so they win.
- **The assumption that turned out wrong:** that the score number *is* the score, so the scan
  verdict alone should color it. On a screen where a hand decision overrides the verdict, the
  number that stays the old color reads as a contradiction, not as information. The honest cell
  states the outcome.

### The whole-show Spare/Reap the show panel never had

A gap the action-grammar pass left: the show panel (`ShowPanel`) listed every season but
offered no way to act on the show, the one override-bearing surface with no buttons at all.
Mocked as a rendered artifact and approved before code.

- **The show panel now carries the whole-show Spare/Reap**, pinned to its own bottom
  `.why-actions` footer, the same placement the movie/season panel already used ("normalize
  the buttons to the bottom"). It acts on the show's `group_key` through the shared
  `useOverrideMutations`, so the panel and the queue refresh as one.
- **A show shows BOTH buttons where a movie shows only Spare.** The action-grammar rule hid
  Reap on the Condemned lane because an already-condemned item makes a hand Reap a no-op. That
  is true for a *movie* (atomic), but a *show* is on that lane because *some* season is
  condemned — "6 of 9 would be removed" — and a whole-show Reap still takes the kept seasons.
  So Reap stays for a show until *every* season is condemned. The assumption that turned out
  wrong: that the tab verdict alone decides `hideReap`. It does for an atomic item; a show
  needed its own no-op test.
- **One test, `showReapIsNoop`** (`components/ReviewQueue.tsx`), shared by the show card's
  whole-show control and the show panel — never a second inline copy (rule 48). It also keeps
  Reap while a hand-spared-condemned season exists (a whole-show reap would flip it) and while
  any hand reap is in force (so it stays toggleable). The card's per-season `SeasonList` and
  movies keep the plain tab test.
- **Deliberately out of scope:** the bulk bar still drops Reap by tab verdict alone. Its
  selection is heterogeneous (movies and shows together), so a show-aware rule there is a
  separate decision, recorded as a follow-up rather than a silent gap.

### The review queue's action grammar (and a bug only Safari drew)

Operator-driven UI work on how a queue row offers its decisions. Each change was mocked as a
rendered artifact and approved before any code, then driven end-to-end in a real browser.

- **Reap is gone wherever the item is already condemned.** On the Condemned lane an item is
  already on the block, so a hand Reap override changed nothing. It is dropped from the card,
  the item panel, and the bulk bar there; Spare (rescue) stays. Reap remains on Sanctuary and
  Limbo, where forcing an item onto the list means something. The real deletion, "Reap now",
  is a different control and is untouched. One prop, `hideReap` from the tab verdict, threads
  the rule through `OverrideControls` everywhere.
- **The row rests quiet; the actions come on hover.** The Spare/Reap buttons are hidden until
  a row is hovered or focused, so a long list reads as scores, not a wall of buttons. A row you
  have already decided rests instead as a small icon of that decision (∞ spared, scythe reaped)
  in the buttons' place; the same hover fades it out as the buttons arrive.
- **Hover is drawn in the accent, additive on the open card.** Every card's hover is the accent
  edge now, not the old gray. On the open (selected) card the hover is additive: the accent
  selection bar stays and deepens, so hovering the open row can no longer read as a click that
  would deselect it — the complaint that started this.
- **Keep-first buttons.** Spare invites in green; Reap stays the quiet gray of a plain button
  and only reddens as you reach for it, so a delete stays deliberate. A chosen decision is the
  solid hand-decision chip (green spare, red reap), the same solid-means-yours language the
  override chips use. A found-along-the-way defect: the chosen button dropped its solid fill
  back to the outline hover the moment the pointer was over it, because `:hover:not(:disabled)`
  outranks `.active`; the active state now re-asserts at hover.
- **A bug only a real browser drew, and only Safari.** Live testing found a thin red bar just
  right of the Reap button in the item panel after switching a decision. It reproduced in neither
  Chrome nor headless WebKit. The first guess — a sticky-footer repaint failing over the colored
  signal bars behind it — was wrong, and its fix (`will-change` on the footer) did nothing; the
  operator's second screenshot showed the real cause. The buttons changed width when their label
  toggled (Reap↔Reaping, Spare↔Spared) and the chosen state is solid, so when the button shrank
  back Safari left the vacated solid-red region unpainted. Fixing the trigger instead of the paint
  — a stable button width (min-width, verified identical across every label state in both engines)
  leaves no vacated region — removed it, confirmed by the operator in Safari. Two lessons worth
  keeping: a green headless pass in one engine is not a paint-bug check (compositing bugs live on
  the GPU you did not test), and the honest fix removes the condition, not the symptom.
- Shipped earlier in the same session: the accent color is operator-configurable (General
  settings), and the first-run wizard lets you head into the app while the first scan runs.

### The full ratings row, and the rescan that never applied

Three operator reports, each of which turned out to be a data or sequencing gap rather
than a display bug.

- **The audience score was missing because no source ever supplied it.** The why-panel
  has rendered a popcorn chip since the ratings row shipped, but on a library whose
  Plex agent fills both listing slots with IMDb, no integration ever produced a Rotten
  Tomatoes audience value: Radarr's object has critic-only RT, and the sweep read only
  the listing's two slots. The wrong assumption was that the listing carries what Plex
  knows. It does not — the full metadata's typed `Rating` children carry every provider
  score, critic and audience separately, at ~97% audience coverage on the sampled
  library (see LEARNINGS "two rating slots"). The sweep now batch-reads metadata for
  movies as well as shows (~1 request per 100 items; shows already had the batch for
  folder paths) and merges children through the same `from_plex` provenance rules,
  slots winning ties. This also makes the policy's "Rotten Tomatoes audience" keep bar
  real on slot-poor servers — it could never fire there before.
- **Trakt is on the card now** (movies): Radarr hands it over at ~99% coverage for
  free, so it is frozen into the display row (`display_meta._STORED_SOURCES`) and
  rendered as the percentage Trakt itself displays, linking through
  `trakt.tv/search/imdb/{id}`. TV has no Trakt source (Sonarr is flat TVDB), so the
  chip hides there. The RT/TMDb chip hovers no longer claim "from Plex" — with
  children + Radarr fill the provider varies, and the row does not record it.
- **"Needs a fresh scan" persisted because the rescan never actually ran.** The
  auto-rescan fired by a policy save is idempotent with the running scan — and a scan
  reads the library under the policies in force when it *began*. Save mid-scan, and
  the "rescan" silently became the old scan; its snapshot landed with the old hashes
  and the notice never cleared, however many times it completed (verified against the
  operator's DB: a snapshot finishing 37s after the save carried the pre-save hashes).
  The fix is a queued follow-up: `POST /scan/start` during a run sets
  `followup_queued`, the runner starts one fresh scan when the current one drains
  (`running` never flaps false between them), an errored run drops the queue, and the
  stale notice says honestly that the changes ride the second scan. Verified live on a
  DB copy: mid-scan save produced snapshot N (old hashes, mismatched) then snapshot
  N+1 (matching) with no operator action.
- Also: the no-rating-sources policy warning and the rating gate's abstain detail are
  reworded in plain language ("rating bars" was editor jargon that leaked).
- **The Policy-tab consistency pass, mocked, approved, and implemented** (six findings,
  operator-approved via the artifact mockup), then the same grammar swept across Reap,
  Fairness and Settings (Review deliberately untouched, per the operator).
  - **One quantity control.** `QuantityInput` is now a single fused box (number + unit
    dropdown), and a new `FixedQuantity` renders unchangeable units ("days", "people",
    "seasons", "/ 10", "%", "+ votes") as a quiet suffix in the same chrome. Everything
    that takes a number-with-a-unit uses one of the two: dormancy, watcher floor, caps,
    grace, season keeps, hold days, lookahead, rating bars. The old third and fourth
    input sizes (`.rule-control`'s smaller boxes, the rating-bar selects,
    `.inline-number`) are gone; every box sits on the documented control standard.
  - **Warnings render beside their fix.** Policy warnings anchor by `field` to the rule
    they describe (rating rules, gates, thresholds, season keeps, custom rules, the
    approval toggle); an unanchored field still falls through to the bottom stack, so a
    new warning can never be silently dropped. The rating card shows its warning in
    place of the summary line.
  - **One save bar.** The two Save buttons (policy, pace) became a single sticky bar
    that appears while anything is dirty, names what changed, states when each part
    takes effect, and saves both with one click; Discard resets both drafts. The
    policy-hash line moved to a quiet line at the end of the policy sections.
  - **One card grammar.** Protections with settings (rating bars, keep-tags) are cards
    with the switch in the card header; the TV season card matches the same container
    and its rows use the same rule-row grammar (name, sentence control, help under its
    own control). The rating bars themselves are rows of one aligned grid (shared
    source-name column), with a single circled-x remove affordance.
  - **Segmented for two-option choices.** A new shared `Segmented` component backs
    Movies/TV, presets, keep-last scope, tags any/all and rating any/all; dropdowns
    remain only for open lists (sources, fields, units, servers, log levels; every
    Settings select was checked and has 3+ options).
  - **One error voice.** Every action failure app-wide (scan start, plan build, dry
    run, execute, spare, shelf update) now renders as the shared `notice-error`, with
    a plain-language lead ("The scan didn't start: …"); bare red `.error` text remains
    only inside the simulator's dedicated failure panel and the review surfaces.
  - Deferred, deliberately: merging the near-duplicate `.warn` banner into
    `.notice-warn` would churn the review card (excluded from this pass); left for the
    next review-UI round.

### Previous — hand-override truth, Settings General + Logs, the API key lane

Round three of the settings pass, mocked first (artifact v3, approved) and implemented.
Four fixes the operator asked for, two new settings tabs, and one honesty bug that ran
deeper than its symptom.

- **A hand reap now takes effect the moment it is clicked — and tells the truth.** The
  reported symptom was cosmetic (a hand-reaped season kept its green pill and square);
  the real finding was that a reap override changed *nothing* until the next scan: not
  the counts, not grace, not the plan. One new module, `services/condemned.py`, now
  assembles the **effective condemned set** (scan-condemned minus hand-spares plus
  hand-reaps that `decide_verdict` honors — rule 22: the reap branch is the engine's,
  only the plumbing is new), and every acting surface reads it: `grace_report` (and
  through it the Leaving Soon shelf), `build_plan`'s step expansion, the confirmation
  phrase (`_planned_candidates`), the executor's per-item keep-set and caps, the
  per-show rollups behind the card counts, and the simulator. Structural stops
  (streaming now, unmanaged) and unchecked protections still refuse, and the UI says
  so: solid red squares/pills for honored hand decisions, an amber "Reap requested ·
  kept for now: {why}" for refusals (`override_effective` on rows and season marks).
  The override routes start the grace clock through the scan's own decision
  (`snapshot.record_first_flagged_bulk`, now public) and remove it again when a reap is
  withdrawn from a not-scan-condemned row, so a stale hand clock can never shorten a
  later real condemnation's window (rule 4). The executor keeps withdrawn-reap items in
  its walk and skips them *visibly*, mirroring the spare skip.
- **Settings → General.** Application name (Discord sender name + browser tab title)
  and application URL (an "Open {name}" link at the end of Discord notices; empty means
  no link). A **theme** row: Match my device / Light / Dark — the app was already
  token-based with light defaults and a dark media query, so forced themes are two
  mirrored token blocks under `:root[data-theme=…]` plus a pre-paint boot script in
  index.html; the choice is per-browser (localStorage), deliberately not server state.
  **Reverse proxy trust**, off by default and fail-closed: forwarded addresses are
  honored only when the direct peer is one of the operator-listed proxies
  (`middleware.client_ip` walks X-Forwarded-For right-to-left past trusted hops), which
  keeps the per-IP login lockout accurate behind Nginx/Traefik/Caddy instead of
  collapsing every visitor into the proxy's address. Applied live via
  `app.state.trusted_proxies`; no restart.
- **The API key lane.** One instance key, generated in the UI, stored encrypted like
  every credential, compared as a SHA-256 digest in constant time on `app.state` (the
  hot path never touches the DB). `X-Api-Key` requests are judged on the key alone —
  never the cookie, and no CSRF check (no cookie, no CSRF risk; the custom header
  itself is preflight-protected). Bad keys back off per address on the same throttle
  shape as login. The key is **fenced** off the three irreversible authorities:
  `PUT /settings/safety` (the deletion switch, either direction), `POST /runs/…/execute`,
  and sign-in/key management (`admin-password`, `general/api-key`) — a leaked key can
  read, scan and plan, but can never delete a file or mint its own replacement.
- **The docs lockdown (found, fixed).** FastAPI's stock `/docs`, `/redoc` and
  `/openapi.json` sit outside `/api`, which the AuthGuard deliberately passes through —
  so the whole API description was readable without signing in. The stock routes are
  disabled; the reference now lives at `/api/docs` (session or API key), rendered by a
  **Scalar** bundle vendored at build time (`frontend/scripts/copy-scalar.mjs`, predev/
  prebuild hooks, gitignored `public/vendor/`) — shipped in the container, no CDN,
  works offline. `/api/openapi.json` carries an `ApiKey` security scheme so Scalar's
  test client authenticates with the real header.
- **Settings → Logs.** A 2,000-line in-memory ring (`reaper/logbuffer.py`) fed by a
  structlog processor placed *after* `redact_secrets` and by a stdlib bridge handler
  (uvicorn/apscheduler land in the same stream, query-string credentials scrubbed).
  The UI polls `GET /api/logs?after=<seq>` every 2 s while Live, pauses cleanly,
  filters by text and by minimum level, and follows the newest line. The recording
  level is a stored setting applied instantly — the structlog pipeline filters per
  event against the live level (`make_filtering_bound_logger(DEBUG)` + a dynamic drop
  processor), so no reconfigure and no restart; `REAPER_LOG_LEVEL` remains only the
  first-boot seed. Debug/Info/Warning only: hiding errors is not a choice we sell.
- **Full-width settings.** The `.panel` 760px / `.panel-wide` 1040px caps are gone;
  every tab fills the same page frame as Review and Reap, and the set-row grid keeps
  labels left / controls right at any width.
- **Ratings chips compacted.** The tomato and popcorn now stand alone (`🍅 68%`,
  `🍿 74%`) with the words in hover text and accessible labels. The audience chip
  itself already existed; whether it shows depends on Plex supplying an RT audience
  value in the `audience_rating` slot (agent ratings-source setting), not on Reaper.
- **Wrong assumption logged:** "the pill fix is display-only." The lane pills were the
  visible symptom of the deletion path ignoring hand reaps entirely; fixing the pill
  without the plan/grace/counts would have painted a red promise the server would not
  keep — the exact class of dishonesty rule 30 exists to prevent. Also confirmed live:
  the lane header ("N items · X would be freed") counts the *listed lane rows*, while
  the card count, the plan and the phrase all count the effective set — the numbers
  beside destructive buttons are the ones bound to the acted-on set.

### Earlier — the Leaving Soon shelf, the Plex pickers, and the settings standard

The Reap workflow and the Plex settings surface were reworked in one pass, mocked first
(two artifact rounds, approved) and implemented against the approved mockups.

- **Execute is gated on arming.** The Reap page reads the same safety query as the
  banner; while deletion is off the button is disabled, with the reason and a jump
  straight to Policy → Deletion beside it (the policy editor takes a `focus` prop and
  scrolls to a section once its heading exists). The server-side arming check stays the
  backstop; the UI just stops inviting a click that must fail.
- **Leaving Soon became a shelf, not a label.** What was a movie-only label, reconciled
  only by hand and gated by a host env var, is now a "Leaving Soon" **collection plus
  label** per enabled library — movies in movie libraries, seasons in TV libraries —
  reconciled automatically after every scan and on demand from the Reap page. A label
  alone was invisible to the household; a collection is a real row on the library's
  Recommended page. An empty shelf vanishes because removing the last item deletes the
  collection server-side, so `DELETE /library/metadata/{key}` — the shape that can
  delete files — is never issued and never permitted: the guard's benign branch
  (`_benign_shape`) allows exactly the label batch edit and the three collection-edit
  shapes, verb+path exact, with a pinned negative test that metadata deletion can never
  ride the shelf opt-in.
- **The switches moved into the product.** `leaving_soon_enabled` and the read-only
  opt-in are `app_setting` rows edited under Settings → Plex; the env var seeds the
  first run only and the stored value wins after (rule 16 honored, `.env.example`
  reworded). Turning the feature off — or any single library off — runs one last empty
  reconcile so no stale shelf lingers.
- **Server, connection, and library pickers.** Settings → Plex lists the account's
  servers and every address each can be reached at, live from plex.tv (falling back to
  the connections stored at link time, marked as possibly stale), probes any address
  before saving it, and lets the operator pick exactly which video libraries Reaper may
  touch (new libraries default on). Switching servers reuses `complete_link`, which now
  also enforces the one-linked-server invariant and clears the library and announced
  state keyed to the old server.
- **Assumptions that turned out wrong, found live.** (1) A pass across several
  libraries reported "preview only" while its writes had landed: a library whose shelf
  already matched scored `applied=False` and poisoned the aggregate. Nothing-to-write
  plus permission-to-write IS the applied state; pinned by test. (2) A library toggled
  off kept its shelf forever, because the reconcile never visits a disabled library
  again — turning a library off now gives it the same goodbye reconcile the feature-off
  path gets.
- **The settings standard.** One shared `Switch` component and one settings-row layout
  (name left, help beneath, control right) are the app-wide pattern now. Boolean-state
  checkboxes were swept (Plex cert check, service modal SSL/verify/enabled, policy
  editor gates, season rules, pace approval); checkboxes remain only where the user
  picks items from a list. A new About tab reports version, license, data folder, and
  database sizes from a read-only endpoint.

All gates green: the backend suite grew by the shelf and settings tests, mypy/ruff/
eslint/vitest/build all clean, and alembic reports no drift (the new settings are
`app_setting` rows — no schema change, the single baseline stands). Driven end to end
against the running app: pickers fed by the live account, shelves reconciled against the
live server with a clean no-op on re-run, and the disabled Execute jumping to the
deletion switch.

### The last two findings, and the review is closed

Review pass 2 is fully discharged: all 55 findings, plus the one raised after it (L-1),
are fixed. Two remained, and both needed a fact the code could not see from where it was
standing.

- **I-3: "Requested only" now admits when it is doing nothing.** The keep-last-N floor
  can be scoped to shows someone requested, which needs Seerr to tell a requested show
  from an unrequested one. With no Seerr, `season_scan._keep_last_applies` never gets a
  Known answer and falls back to protecting, so the floor quietly covers the whole
  library: the setting reads narrower than it behaves. That outcome is safe, which is
  exactly why nothing surfaced it. `engine/policy.inspect` now takes a
  `requests_app_configured` fact and warns. The reason this sat open is worth recording:
  the warning is read on `POST /api/policy/validate` (what the editor calls as you type),
  and that route had no session, so it could not know what was configured. It takes one
  now. The warning stays quiet when the floor is off (at 0 seasons the scope decides
  nothing), on movie policies, and when a caller cannot tell -- telling an operator to
  connect a service they already have is worse than silence.
- **L-1: the guard refuses what it cannot authenticate.** `AuthGuard` returned early on
  any scope that was not `http`, handing it to the app with no CSRF check and no
  `resolve_session`. Latent (Reaper declares no websocket route, so only `lifespan`
  arrived, and that must pass), but it failed *open* on the branch nobody reads: the
  first websocket added would have been born unauthenticated with nothing failing at the
  point of the mistake. `_refuse_scope` now passes `lifespan` and refuses the rest,
  closing a websocket with 1008 the way Starlette's own router does. A handshake that
  belongs here must authenticate the cookie and validate `Origin` itself, since the
  browser WebSocket API cannot send `X-Reaper-CSRF`. This was the prerequisite named in
  the deferred deletion-progress design note below.

Both were driven against the running app, not just tests: the editor's warnings still
render and `/api/policy/validate` still answers with the added session (the negative case
is live, since a Seerr is connected here), and the app boots and serves behind the
rewritten guard, which is `lifespan` and `http` both proving themselves every request.

### Review pass 2: the deliberately-held items, closed

The items held open at the end of review pass 2 (`docs/CODE_REVIEW.md`, "Held,
deliberately") are now implemented, each with regression tests. The decisions they were
waiting on were made, in every case toward keeping the file:

- **Size-drift re-read at delete time (H-1).** The executor re-reads the item's live
  size immediately before anything is sent (`sizeOnDisk` for a movie; the season's
  episode files, before even the reversible unmonitor) and compares it against the
  frozen approved size. Growth past one tenth of the approved size (256 MiB floor for
  small items, `_grew_materially`) means the file was upgraded since approval -- the
  caps and the typed phrase counted a smaller file -- so the item is skipped and kept,
  as is an item whose current size cannot be read. The interlock list and the planner's
  manifest comments claim the re-read again, now truthfully.
- **The keep_history gap is closed (H-1).** `TautulliClient.users()` is back, wired
  this time (rule 38): every scan reads the user list, and an *active* user with
  Tautulli's per-user Keep History off degrades the snapshot -- everything only they
  watch looks never-played, so "nobody watched it" cannot be trusted. Fail-closed both
  ways: an unreadable user list, or a row whose flag cannot be read, also degrades.
- **P-6 decided: staleness is bounded, not ignored.** A failed whitelist sync with
  stored members keeps protecting (the atomic swap preserved the membership) for at
  most `WHITELIST_STALE_AFTER` (48 hours, two nightly cycles plus slack) past the last
  *successful* sync -- `protection_list.last_synced_at`, already written only on
  success, is the record. Beyond the bound, or with no success ever recorded, the
  snapshot degrades until a sync succeeds. Engineering rule 2 now states the bounded
  exception.
- **Disarm mid-run halts the run (P-10).** The executor re-reads the arm switch before
  every item of a real run, through a fresh session injected by the execute route (the
  run's own session caches rows across its per-item commits). The moment the switch
  reads off -- or cannot be read -- the run aborts before its next item; a real run
  without the recheck wired is refused outright. The module docstring's "deliberately
  not here yet" section is gone; mid-run disarm is interlock 5.
- **One scan at a time (P-10).** The one-scan claim lives inside `run_scan` itself, so
  the browser's button and the scheduler share it wherever the scan started: the
  scheduler skips (logged) when a scan is running, the scan route surfaces the refusal,
  and the claim releases however the scan ends. The grace-clock recorder's inserts are
  additionally conflict-tolerant (`INSERT ... ON CONFLICT DO NOTHING`, the winner's
  clock survives).
- **Per-install KDF salt (P-10).** `secret.salt` (16 random bytes, hex, 0600, atomic
  create) is minted beside `secret.key` on first boot and threaded into `SecretBox`, so
  the at-rest credential key derivation is unique per install and a dictionary attack
  cannot be precomputed across installs. Nothing breaks on upgrade: the fixed v1 salt
  and the legacy SHA-256 derivations stay registered decrypt-only, so pre-salt data
  opens and ages out as it is re-saved. The entropy floor on operator keys is
  documented as advisory in `.env.example`.
- **Plex TLS opt-out (P-10).** `PlexServer.verify_tls` (default on) mirrors the
  per-instance setting: chosen in the link form (it rides the link polls, so a
  self-signed HTTPS server can be probed and linked at all), stored on the server row,
  read by every client construction (scan, reap gateway, Leaving Soon), and editable in
  Settings > Plex with the same warning copy as the instance modal. `.env.example`
  documents that TLS choices live in the UI.
- **B-13: show cards state what "Reap now" will plan.** Season rows in the candidates
  API carry `group_condemned_count`/`group_condemned_bytes`, computed over the WHOLE
  snapshot's condemned members minus hand-spares -- using `whitelist.effective_override`,
  the same function the planner filters with (rule 30) -- so a show straddling unfetched
  pages can no longer understate its plan. The card uses the server totals on the
  "Would reap" tab and keeps fetched sums elsewhere (those tabs describe what is
  listed, not a plan).
- **H-4 decided: ESLint and vitest are build gates.** Flat-config ESLint with the two
  classic react-hooks rules as errors (the plugin's newer compiler-era rules are
  deliberately off: they flag long-standing legitimate patterns, and a gate must stay
  green to mean anything); the three pre-existing inline disables are finally truthful.
  Vitest + Testing Library component tests pin the reap confirmation's client-side
  execute gate (exact-phrase unlock, disarmed state, aborted dry run, cannot dismiss
  mid-reap, the per-item checklist) plus the size formatter. Both run in CI before the
  build, and CLAUDE.md's gate list includes them.

Still open after this pass: I-3's "keep_last_scope=requested with no requests app"
warning (needs instance knowledge inside `inspect()`), and the policy-revert
uniqueness quirk below (its own session). The `docker build` gate runs in CI.

### Review pass 2: the medium and low findings, cleared

Every remaining finding from `docs/CODE_REVIEW.md` that did not need a product decision
is now fixed, almost all with regression tests. Green: 1,201 backend tests, ruff, mypy,
the frontend build, and `alembic upgrade head` + `alembic check` against a fresh DB
(the long-lived local dev DB still shows the pre-existing `verify_tls` default drift
noted below; fresh DBs are clean).

- **Fail-closed corrections.** A vanished keep tag or deleted "Never Reap" collection is
  a missing *container*, never an empty membership: with members stored, `lists.sync`
  keeps them and records the failure (B-7), and an *arr `/tag` endpoint answering
  200-with-not-a-list is an error, not an empty whitelist. A movie with no TMDB id is
  refused *before* the delete is sent (B-18). `stopped=0` in a Tautulli row no longer
  reads as epoch 0 in the played-since-approval check (B-14). Release age from a bare
  year rounds to Dec 31, the keeping direction (PE-8). The grace view excludes seasons
  spared through their show (B-15).
- **The guard closed its GET-shaped hole (B-8, R-2).** `GuardedSession` classifies
  `GET /library/sections/{key}/refresh` as a mutation (armed + declared, like
  `emptyTrash`), and the benign-label branch is structurally confined to
  `PUT /library/sections/{key}/all`.
- **Redirects cannot carry credentials away (P-5, H-2).** `follow_redirects` is off;
  reads follow at most three same-origin hops; a redirected mutation is an error, never
  a replay. The two public GET fetchers (IMDb Top 250, the ratings dataset) moved into
  `clients/public.py` (credential-less, so cross-origin CDN hops are allowed there);
  Discord's webhook POST is documented in CLAUDE.md rule 33 as the sanctioned exception.
- **Matching that silently never fired now fires (PE-7).** `in`/`eq` trim and casefold
  both sides, and multi-valued facts (genres, curated lists) match per element.
- **Engine seams tightened.** The popularity window reads from one `PolicyBody` method
  and ignores disabled gates (PE-9/R-3); scoring configs are hoisted out of the judge
  loops (PE-13); the backtest's expected-regret rate survives empty prior buckets,
  reporting mixed provenance (PE-4); TV calibration joins episode history through the
  grandparent key (PE-6); graded rules read `Absent` as evaluated-with-zero-pressure,
  matching the boolean path (PE-11); and a second id kind resolving away from a tier-1
  bind is a contradiction that abstains (PE-12).
- **Claimed safeguards now exist or are no longer claimed (H-1).** The dry-run docstring
  names exactly what a dry run proves; "size re-reads" are no longer claimed anywhere;
  the planner's no-delete-path skip logs; the execute route hands its own RuntimeSafety
  snapshot to `build_reap_gateway`, so guard and executor read one switch state; the
  Tautulli `users()` method is deleted (gap recorded below); the vite proxy comment
  stopped claiming SSE.
- **Dead safety-adjacent surface deleted (PE-10/R-1, rule 38).** `engine/custom_gate.py`,
  `PlexClient.labels`, `SeerrClient.users`, `TautulliClient.users` and its allow-list
  entry, `Facts.unknowns`, the unused observation helpers, and
  `whitelist.spared_keys`/`reaped_keys`.
- **Auth hardening (P-3).** Arming deletion and changing the admin password run behind
  a dedicated lockout (`password_throttle`) plus the Argon2 concurrency gate, exactly
  like login; changing an existing password requires the current one (UI field added);
  every field that reaches Argon2 is length-bounded.
- **Client lifecycle (P-4, rule 34).** `build_sources` enters every client into the
  caller's exit stack the moment it is constructed; `PlexClient` gained
  `aclose()`/context-manager support and is closed by the scan, the reap gateway, and
  the Leaving Soon route.
- **Fairness honesty (P-7, B-21).** The judging clock is clamped to the watch mirror's
  horizon (surfaced as `horizon_at` in the payload and the caption); unjudgeable
  requests are counted per request; a request that names its seasons is charged those
  seasons' on-disk sizes, not the whole series.
- **Supply chain and container (P-9, I-2).** Base images digest-pinned; CI actions
  pinned to commit SHAs; `.dockerignore` uses `**/` forms; `python-dotenv` declared;
  the entrypoint starts as root only to chown `/data` to `PUID`/`PGID` (default 1000)
  and drops to that user via gosu before anything opens the database, so a root-owned
  bind mount needs no manual chown and the app process itself never runs as root; a
  `reaper.preflight` write-probe (via `Settings.ensure_data_dir`) turns an unwritable
  data folder into a plain, actionable message instead of SQLite's opaque traceback;
  the entrypoint honors `REAPER_HOST`/`REAPER_PORT` and the healthcheck follows the port.
- **Assorted P-10.** `/api/health` is a bare liveness probe (the banner reads the
  authenticated safety route, sharing the `["safety"]` query the deletion toggle
  invalidates); `IN :keys` queries chunk at 500; the IMDb dataset parse runs off the
  event loop; 429s honor Retry-After; the incremental history overlap widened to two
  days with its comment softened; `inspect()` warns on a very short popularity window.
- **Frontend (B-10/B-11/B-12/B-20, P-8, I-1).** Dirty-guarded media-type switch; the
  reap sheet stays up until its report renders and invalidates in `onSettled`; the
  editor un-dirties after save by re-seeding from the response; the six B-20
  one-liners; explicit loading/error states on the always-visible safety surfaces; the
  verdict headline speaks the tab vocabulary; `.select-tick.on` uses `--accent-ink`.

**Still open, deliberately** (as of this entry; every item except I-3 and the
policy-revert quirk is closed by the pass above):

- B-13 (show cards vs. planner counts) needs a per-group condemned-totals API.
- H-4: adding ESLint + vitest as build gates is an infra decision.
- The size-drift re-read at delete time, and any real `keep_history` protection, are
  features. **Gap recorded:** a household member with Tautulli history recording off is
  invisible in the history table, so everything only they watch looks never-played, and
  nothing abstains on it today.
- P-6 (whether a failed whitelist sync with stored members should degrade after some
  bound) is a test-pinned tradeoff awaiting a product decision.
- P-10's disarm-mid-run and shared scan lock change run semantics; the Plex TLS opt-out
  needs the DB-backed setting surface (rule 16); the per-install KDF salt would break
  existing at-rest credentials without a migration.
- I-3's "keep_last_scope=requested with no requests app" warning needs instance
  knowledge inside `inspect()`; not wired.
- Found while verifying B-12 (not a review finding): `POST /api/policy` dedups a save
  against every historical row's hash, and `policy_hash` is UNIQUE, so reverting a
  policy to previously-saved content returns 200 without becoming active (the active
  policy is the newest row). Fixing it is a schema decision (drop the unique constraint
  so a revert can append, or add an activation pointer); being fixed in its own session.

### The second review pass's critical and high findings, fixed

The second whole-codebase review (`docs/CODE_REVIEW.md`, dev @ `5b885f5`) surfaced 1
critical and 9 high findings; all ten are now fixed, plus the three mediums the review
bundled with them (B-6, B-9, PE-5). The tree is green: 1,150 backend tests, ruff, mypy,
`alembic check` against a fresh DB, and the frontend build.

- **The four fail-open protection holes are closed.** TV membership lookups now pass
  every id the show carries (a tvdb-only keep tag protects again — B-1); fileless
  seasons no longer consume keep-last slots (`rank_seasons` ranks only content-bearing
  seasons — B-2); a failed watch-history sync degrades the snapshot instead of scoring
  quietly on a stale mirror (B-3); and season ranks and the scoring fact stay aligned
  because both read the same ranker.
- **The action journal is crash-durable (B-4/B-9).** The PLANNED→EXECUTING claim is an
  atomic `UPDATE … WHERE state='planned'` committed before the first send; every step
  mark (SENT before dispatch, VERIFIED after re-read, per-item skips/fails) commits as
  it happens; the run's final state commits before the Plex cleanup. A killed process
  now leaves EXECUTING with the exact in-flight step SENT, never a rollback to PLANNED,
  and the SQLite writer lock is no longer held for the whole run.
- **The trash purge earned its promised interlock (B-5/B-6).** `_finalize_plex` now
  refuses to purge unless the section's item count — captured before the first delete —
  shrank by at least one and by no more than what this run deleted under it, on top of
  the existing mount check; section mapping matches at path-component boundaries, so
  `/media/movies` can never claim `/media/movies-4k`. On servers where trashed items
  still count toward the section size the delta never confirms and the purge simply
  never runs (cosmetic loss, the safe direction).
- **The 30-day rolling caps are enforced (P-1).** `Executor._check_rolling_caps` sums
  the trailing 30 days of VERIFIED terminal deletions (whatever state their run ended
  in) and aborts — never truncates — any run that would exceed either rolling cap, in
  dry-run and real runs alike.
- **One verdict function (PE-1/PE-5).** The condemn/abstain/protect decision now lives
  in `engine/verdict.py`; the scan, the simulator and the backtest all import it. The
  simulator no longer re-decides rows whose protections could not be checked (they stay
  abstained at any threshold) or rows under a hand override (the stored verdict is
  pinned), and the agreement tests call the real functions instead of a transcription.
- **Operator-authored condition values are typed (PE-2).** `Condition.validate_for`
  rejects a value that does not match the field's type at save time (422), and a bad
  *stored* value degrades that one item as blocked instead of crashing the whole scan.
- **The backtest is honestly labeled (PE-3).** It remains engine-complete and tested
  but unreachable; every operator-facing string that told people to "run a backtest"
  is reworded, the dead `BacktestOut` schema is gone, and the milestone table below now
  says 🟡 not ✅. Wiring `POST /api/policy/backtest` (+ the calibration prior + minimal
  UI) is open work — note PE-4 (`expected_regret_rate` crash) and PE-6 (TV calibration
  join) must land with it.
- **Tests are hermetic (P-2).** An autouse conftest fixture stubs the lifespan's env
  seeding and startup catch-up and clears `Settings`' dotenv sources, so no test reads
  a developer's `.env` or reaches the network.

Note for local runs: `alembic check` against the long-lived dev database fails on a
pre-existing `instance.verify_tls` server-default quirk of that database; against a
fresh database (what CI builds) it passes clean.

### Earlier — the scan's wall clock was sequential waiting, and most of it is gone

Measured end to end against a real dual-instance library (movies + TV, all six
integrations live): the scan dropped to roughly **a fifth** of its previous wall time,
with byte-identical verdict counts before and after and a non-degraded snapshot both
ways. Two distinct fixes carried it, and the second only became visible after honestly
measuring the first:

1. The structural work below (concurrent gather, batched per-item database work)
   barely moved the total at first, because both the old and new pipelines were
   dominated by one shared bottleneck neither had touched;
2. that bottleneck was the Plex GUID sweep: plexapi silently issues **one metadata
   request per item** whenever an accessed attribute is ``None`` on a listing row
   (roughly ~80% of the old scan's wall time on the measured library). The sweep now
   parses the listing container XML directly (plus batched ``/library/metadata/{ids}``
   reads for show folder paths), which took the sweeps from minutes to seconds -- see
   ``clients/plex.py`` and the reload-trap entry in ``docs/LEARNINGS.md``. With the
   sweeps fixed, the concurrency work is what keeps everything else off the critical
   path.

The scan was slow for structural reasons, not compute: every source was read in
series, and three per-item patterns multiplied round trips by the size of the library.
What changed (all read-only paths; the freeze-then-judge contract is untouched —
nothing is scored until every source has completed or degraded, exactly as before;
only the *waiting* overlaps):

- **The gather fans out.** After the active-streams read, the movie index (Plex GUID
  sweep + Tautulli spine, themselves now concurrent), each Radarr's movie list, and
  the whole TV season gather run concurrently; `run_scan` also overlaps the
  protection-list refresh with the two Seerr reads. The gather now costs its slowest
  source, not the sum. The two plexapi sweeps are serialized by a client-level lock
  (one `requests.Session` under plexapi; not promised thread-safe) but overlap with
  everything else.
- **TV resolution was the longest sequential stretch**: two HTTP round trips per
  prunable show (Tautulli `get_children_metadata` + Sonarr episodes), one show at a
  time. Now one call per show with several shows in flight under a small per-service
  bound (`RESOLVE_CONCURRENCY = 4` — a stampede against self-hosted services is the
  failure mode to avoid), deduplicated per distinct matched show.
- **Per-item SQLite left the judge loop.** `lists.memberships` ran an ensure-schema
  (7 DDL statements) plus a SELECT *per movie and per show*; it is now one bulk load
  into an in-memory `MembershipIndex` whose lookups are proven (test) to answer
  exactly like the SQL. The grace clock writes are batched the same way: condemned
  keys are collected and applied in chunked `IN` reads through the same single
  decision function (`_apply_first_flag`), instead of a `session.get` + autoflush per
  condemned item. `build_facts` is now a pure function.
- **The keep-tag provider re-downloaded the library once per configured tag** (tags()
  + the full movie/series list, per tag, per scan). It now reads the tag list and the
  library once per rule; list providers in `sync_protection_lists` refresh
  concurrently (each already fails soft on its own; the atomic per-list swap is
  unchanged).

An adversarial review pass over the concurrency change (eight independent reviewers,
findings verified before acting) surfaced and fixed:

- **A pre-existing fail-open on the keep-tag whitelist**: `ArrTagRule`'s slug carried
  only the service name, so with two same-service instances each sync atomically
  replaced the OTHER instance's keep-list, last writer wins, titles tagged only on the
  losing instance silently unprotected. The slug now carries the instance id (each
  instance is its own list), the display name carries the instance name, and a
  regression test pins two instances protecting simultaneously.
- **Cancellation discipline in one place**: every concurrent fan-out goes through
  `reaper.aio.gather_reaped` / `reap` -- on the first failure the surviving branches
  are canceled, drained and logged before the failure propagates, so nothing keeps
  reading an operator's services for a scan that is already dead, at any nesting
  level. `scan()`'s fan-out creates every task through one `_spawn` helper so the reap
  list cannot miss a future branch.
- The twin movie/TV index builders collapsed into one shared
  `services.library_index.build_index` (they had already started drifting).
  `memberships()` now delegates to the same loader the scan uses: one SQL source of
  truth for "what protects this item".
- `PlexClient` serializes both the GUID sweeps AND first connect (two concurrent
  sweeps on an unconnected client would have built two sessions, voiding the sweep
  lock's premise).
- The judge loop yields to the event loop at its progress-emit points: with the
  per-item queries gone it had become pure computation, which would have frozen the
  very progress endpoint it feeds for the whole scoring phase.
- Deliberately declined, recorded here so the next reader knows they were weighed:
  collapsing the two Seerr paginations into one (the server-side "available" filter
  and client-side availability are not provably the same predicate; display-only win),
  overlapping the history sync with the list refresh (same SQLite writer, first-run-only
  win), and concurrent Tautulli spine pages (politeness; the spine overlaps the Plex
  sweep already). The remaining known redundancy: each *arr library is still downloaded
  twice per scan (once by the keep-tag rule, once by the gather).

Wrong assumption recorded: "a full scan completes in tens of seconds" (below) was
written before season pruning added per-show resolution; on a large TV library the
sequential per-show reads dominated everything else the scan did. Also: list
membership is now frozen once at gather start (it was previously re-read per item
DURING judging, after the freeze -- the one evidence source that could change mid-run,
which is exactly what the freeze exists to prevent).

### An ambiguous id narrows by file name, then exact size (the split-library fix)

### Services settings redesign, and the per-service TLS opt-out

The Settings → Services tab moved from a flat stacked list with an always-visible add form
to one section per service kind (Radarr / Sonarr / Tautulli / Seerr), each a responsive
grid of compact cards plus a dashed "Add a …" card. Add and edit now live in a modal
(`ServiceModal.tsx`): the address is edited as hostname / port / SSL / optional URL base,
composed back into the single stored `base_url` in the frontend only (the wire format and
backend are unchanged), pasting a full URL into the hostname field distributes it across
the fields, and the scheme's default port is left off so existing URLs round-trip
unchanged. Enable/disable moved into the edit modal as an "Enabled" checkbox; the card
footer is Test / Edit / Remove with the existing two-step confirm. The Setup Wizard renders
the same panel unchanged.

The long-deferred self-signed TLS opt-out landed with it: a per-instance `verify_tls`
column (default on), threaded from the row into the clients' existing `verify=` transport
parameter at every construction site (scan sources, reap gateway, fairness, poster,
scheduler sweep, and both connection tests). In the modal it is "Check the server's
certificate" — shown only when SSL is on, with a caution note while off — and the card
wears a "certificate check off" chip so the choice is visible at a glance. On update an
omitted `verify_tls` means "leave it alone" while an explicit `false` sticks; create and
test default to on. Pinned by tests at three layers (API round-trip including
omitted-vs-explicit-false, client `verify` → `httpx.AsyncHTTPTransport` kwarg,
`build_sources` passing each row's own flag) and proven live against a scratch self-signed
HTTPS server: with the check on the test fails with a certificate error, with it off it
connects, and a saved instance's test uses its stored choice. The baseline migration was
edited in place (pre-release rule). One sharp edge worth remembering: an *existing* dev DB
can't take a plain `ALTER TABLE … ADD COLUMN verify_tls BOOLEAN NOT NULL DEFAULT 1` —
SQLite requires the default, but the resulting server-default then trips `alembic check`
as model/migration drift — so an existing dev DB gets a one-off instance-table rebuild
(create the new shape, copy rows with `verify_tls = 1`, drop, rename).

### Earlier — an ambiguous id narrows by file name, then exact size (the split-library fix)

On a split library (HD + 4K sections, curated sections re-listing titles), one external id
names 2+ Plex items — ~3% of items on a live scan — and every one abstained *forever*:
watch history invisible, unjudgeable, a wall of "kept to be safe" notices. The duplicate-id
hits are the same content in several copies, and the *arr item's own file evidence
identifies which copy the entry manages — so the resolver now narrows, in two steps, always
inside the id's candidate set (never the wider library; that would be a guess):

1. **File name.** Compared against **all** of a candidate's files (`PlexItem.files` — a
   merged multi-edition row compared by only its first file makes a re-list of its second
   file look "unique"), with every candidate's files known. Exactly one match binds
   (`id_and_basename`).
2. **Exact byte size**, when the name matches several. Radarr's `movieFile.size` against
   each matched listing's part size. A size singling out one listing binds it
   (`id_and_basename`, detail says size took part). Several listings at exactly that size
   are **byte-identical twins of the *arr's own file** — verified live: a curated section
   re-lists the very file under its own rating key at a different path, same name, same
   parent folder name, byte-equal size, listed years apart — and they bind as a **group**
   (`merged_listings`): canonical key = the earliest listing (the original row; poster,
   honest dormancy floor), with every listing's key carried in the match block. Watch
   reads then cover the whole group: the scan folds last-played/watchers as an exact
   union (one person through two listings counts once), the streaming fact checks every
   key, and the executor's two live interlocks (streaming veto, played-since-approval)
   re-read the stored group so a play through the file's other listing spares it. Merging
   can only ADD evidence of watching, which errs toward keeping; the delete still routes
   by the *arr's own file, untouched.

Anything else keeps abstaining, with an audit detail that says why the copies could not be
told apart ("a copy's file name is unknown" / "matches none" / "matches N of them, and it
has no file size to tell them apart" / "none of those files is the same size"). Shows never
merge (a folder has no one size). Adversarially reviewed before building; accepted
residuals: an *arr rename landing exactly on the sibling copy's file name mis-picks until
Plex rescans (transient, same-content only, gated by grace + supervised execution), and a
byte-identical *different* rip sharing name+size+id would merge (practically impossible,
and merging errs toward keep). The contradiction veto treats a tier-3 hit on another
listing of the same group as agreement, and is otherwise byte-identical; Tiers 2 and 3 are
untouched. Verified live end to end: a real scan first bound about a quarter of the
previously-stuck items by name alone (per-copy scores immediately diverging), and the size
merge then resolved the remaining same-file re-lists.

### Earlier — operator-authored weighting rules, and a season-keep rework (backend done, UI pending)

The policy grew a Radarr-style **custom rules** surface, mapped onto Reaper's two-lane safety
model rather than Radarr's signed sum. Positive "reasons to remove" become **unsigned custom
condemn signals** (`BooleanCondemnSpec` = match → full weight; `GradedCondemnSpec` = a numeric
field ramped like a built-in), and "reasons to keep" become either a hard protect condition
(already existed) or a new **graded lean-to-keep** — a strictly-subtractive discount applied
*after* the normalized score (`value = max(0, base − Σdiscount)`). Both are provably fail-closed:
an Unknown condemn rule adds 0 pressure while keeping its weight in the fixed denominator, and an
Unknown keep takes its *maximum* discount — so, as with the built-in signals, missing data can
only push a score *down*. All the new arithmetic lives inside the one `score()` both the live scan
and the backtest call, so the lift gate measures the composed formula (it catches a size-based
custom rule the way it catches built-in SIZE) and there is no second implementation to drift.
`_verdict` is untouched, so a keep can turn a condemn into an abstain but can never un-protect a
gated item. `SCORER_VERSION` was bumped, so the simulator correctly refuses stale numbers.

Five new **Unknown-safe metadata fields** back the rules: `requested` (three-state, from a new
`build_request_index` over *all* Seerr requests — joined on tmdb/tvdb so a non-admin key can't
break it, and Unknown whenever Seerr can't be fully read, so a missing requests app never reads as
"not requested"), `genre`, `release_age`, `quality`, and `show_ended`. The operator explicitly
accepted elevating requested-status to a decision input, given the fail-closed handling.

Season-keep gained: the keep-last-N knob is now scopeable (`keep_last_scope: "all" | "requested"`,
fail-closed — Unknown-requested still gets the floor); an `inspect()` warning and a new
`GET /api/snapshot/season-shape` endpoint so the editor can show live "X of Y shows have no
removable season"; clearer over-count "why" copy; and **episode-precise mid-binge** protection —
the guard now protects the season a viewer is *actually* mid-way through (or the true next one if
they finished), from a new `media_index` column on the rebuildable `watch_event` cache (idempotent
add, nightly-backfilled) plus a scan-time Sonarr `episodes()` read for each season's last on-disk
episode. Fail-closed throughout: any missing episode data drops to the old season-level `{m, m+1}`.

The **unified rules-editor UI** then landed on the approved mockup: a "Your custom rules" panel with
"Reasons to remove" (offering exactly the new metadata fields) and "Lean toward keeping" (numeric
fields), each routed to the correct lane; the season slider became an uncapped number input with a
live "X of Y shows have no removable season" advisory (fed by `GET /api/snapshot/season-shape`), a
`keep_last_scope` segmented control, and a `season_lookahead` input; and the why-panel gained a
"leaning toward keeping" block. Verified end to end in a logged-in browser against a real scan:
authored a `genre contains …` remove rule → validate/save/reload round-tripped 200, the advisory
read live off 222 shows, and the scorer-version bump correctly made the simulator refuse stale
numbers ("needs a fresh scan"); no console errors; the demo rule was then removed to restore the
policy.

Green: ruff, mypy, **905 backend tests** (new `test_custom_condemn.py` plus extended season/history
coverage), `alembic check` clean (no new drift — no ORM columns changed; the policy is JSON and the
cache is off-Alembic), and the frontend build (tsc + vite). Assumption that held: reusing the engine
specs directly as the API DTOs (rather than parallel `*In` models) kept the lane/numeric validation
on the wire. Remaining follow-ups (deferred): compound AND rules within one custom rule; backtest
reconstruction of the new metadata fields (they read Absent in the backtest today, so a genre/quality
custom rule is inert there); and a first *live* season reap to exercise the episode-precise guard
against a real Sonarr.

### Earlier — matching by external ID, not by name

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
admin-password floor rose 8 → 12. Deferred at the time — a self-signed TLS opt-out (verify
defaulting on, fail-closed) — has since landed as the per-instance certificate toggle (see
the newest entry above). Note: `alembic check` fails on a **pre-existing**
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
  *during the grace window* (exactly what the executor exists to honor) would have been
  deleted, because a spare changes neither the frozen `condemn` verdict nor the manifest
  hash. Fixed: the planner now hashes the **whole** condemned set (so a later spare does not
  void the run), builds steps only for non-spared items, and the executor re-checks the
  override **per item** in dry-run and for real. Pinned by
  `tests/test_reap_loop.py::TestASpareIsHonoredAtExecuteTime`.

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

- **"Honor your keep list" became "Spare titles you've tagged."** The `reaper-keep` tag was
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
- **The planner honors it in the gap before a re-scan** (`effective_override` on each condemned
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
  requested became labeled dropdown pills beside it.
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
  configurable automatic-scan schedule does not touch it. *(Superseded: the upkeep jobs are
  now individually schedulable and switchable off. See "Jobs page, rebuilt as one job list"
  below.)*

---

## Status

| Milestone | State |
|---|---|
| **M0** Skeleton — uv, ruff, mypy strict, Alembic (batch + naming), Docker, CI | ✅ done |
| **M1** Auth + clients — Plex OAuth + owner check, Tautulli, Sonarr, Radarr, Seerr | ✅ done — **now actually enforced**: session gate + CSRF in front of the whole API, login UI wired |
| **M2a** IMDb ratings dataset | ✅ done |
| **M2b** Curated lists (IMDb Top 250, *arr tags, Plex collections) | ✅ done |
| **M3a** Scoring engine — gates, signals, observations | ✅ done |
| **M3b** Policy persistence — immutable rows, hash, caps, autonomy grants | 🟡 rows/hash/caps done; the autonomy-grant *flow* is unwired (nothing can create a grant until the backtest ships a route) |
| **M3c** Backtest — replay against the operator's own watch history | 🟡 engine complete and tested, **not reachable**: no route, CLI or UI calls it yet; operator copy no longer references it until it ships |
| **M3d** Field registry + authorable protect rules | ✅ done |
| **M3e** Snapshot pipeline + REST API + polled progress | ✅ done |
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
| **Scales** — the fairness view, renamed and rebuilt as per-requester cards (a balance bar weighing granted disk that is *earning its keep* vs *to reclaim*, the reclaimable summary leading with the disk not the count, each reclaimable title a chip that opens its real card). Nav label + heading say "Scales"; the internal `view` id, `["fairness"]` query key, and `/api/fairness` route are unchanged. | ✅ done |
| **Scales sits on the last scan** (was a live requester-rule re-evaluation). It joins Seerr requests to the latest snapshot's candidates by tmdb/imdb, so it can never disagree with Review: a title is reclaimable only when the scan **condemns** it (`verdict == condemn`), watches are keyed on the candidate's own rating key (fixes the stale-Seerr-key false "nobody watched"), and a request the scan hasn't seen reads *not in the last scan*, never unwatched. The old live path re-derived the scan's title resolution (a second copy of the app's most delicate matching); reusing the snapshot keeps one resolution. `/api/fairness` rows now carry `reclaimable: [{title, size_bytes, item_id, group_key}]` (heaviest first, capped 25); report adds `not_in_scan` and `no_snapshot`. Root cause it fixes: the watch log has no content id, so the old single-key lookup missed plays on a title's other copies (4K/HD, merged listings, re-match) that the scan folds. Verified end-to-end: the reclaimable count agrees with Review, and a chip click lands on the item's Why panel. | ✅ done |
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
  `services/scan_runner.py` so the scan route and the timer run the identical path.
- **The emergency stop is finally wired.** It was defined on `RuntimeSafety` but never read
  from anywhere — every construction site passed only `env_enabled`. Now
  `services/app_settings.runtime_safety` assembles the effective permission at every site,
  and the Safety panel surfaces the arm-state plainly. *(Superseded: the two-switch model
  described here was later collapsed into a single stored toggle. See §12.)*
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
  a streaming or long-lived response): every `/api` route needs a session, except the
  health probe and `/api/auth`. CSRF on every unsafe method — a custom header plus
  `Sec-Fetch-Site`.
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

---

## Decisions locked

| Decision | Choice |
|---|---|
| Condemn logic | **Flat AND** of typed conditions. No OR, no nesting, no NOT. |
| Protections | **Gates with no CONDEMN constructor** — structurally cannot delete |
| Protect authoring | **Catalog + user-authored protect rules** (safe: worst case is nothing deletes) |
| Signals | **Unsigned**, fixed denominator including unknown weights |
| Observations | **Known / Absent / Unknown** — never conflated |
| Delete mode | DB-only grace period → cancellable → then irreversible |
| Autonomy | An **earned grant keyed to `policy_hash`** — any edit reverts to approval-required |
| Caps | **Four**: items + bytes, per-run + rolling 30-day |
| Kill switch | **One-way**: the UI can disable deletion, never enable it |
| Backtest | Engine in v1, **surface still unwired** — a `POST /api/policy/backtest` + minimal UI is open work; until then the live simulator is the threshold-tuning surface |
| Auth | Plex OAuth + `owned == true` check, local fallback that cannot be removed |
| Migrations | **Baseline `22777b2b5015` is frozen** (testers have real data). Every schema change is its own additive, non-breaking revision chained onto head — nullable `ADD COLUMN`, new table, backfill — never an edit to the baseline. `cache.db` stays disposable. |

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

A full scan of a large library completes in tens of seconds again (measured live
after the concurrent-gather and sweep fixes above; it had crept to minutes as
features landed), streaming progress while it runs, and produces a candidate list
partitioned into condemn / protect / abstain. The gather is concurrent across
sources: it costs roughly its slowest source plus the judge loop, which is now
in-memory per item.

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
  and grace (a canceled item leaves the countdown at once). Spare/un-spare on every
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

## The policy workspace rework (M8) — one page, in decision order

The policy configuration UI was scattered: caps edited in two near-duplicate places (Reap
and Settings → Limits), keep-tags buried inside a gate row, custom rules split from the
signals they extend, and the arming switch two tabs from the policy it governs. The rework
consolidates everything that shapes a decision onto the Policy tab, ordered the way Reaper
decides: what flags a title → what's always kept → pace and limits → deletion, with a
sticky section rail and the live simulator alongside. Iterated as a rendered mockup (eight
revisions) before any code.

- **Three saves stay separate on purpose**: the hashed policy (per media type, labeled
  "Save movie policy" / "Save TV policy"), the un-hashed pace/limits (its own button; a cap
  change never voids an approval; the ask-first toggle leads the section, above the caps),
  and the password-gated deletion switch (a shared `DeletionToggle` in Policy → Deletion,
  the only arming surface: password to enable, none to disable). Settings → **Security**
  (renamed from Safety) holds only the admin password.
- **Presets** (Cautious / Balanced / Aggressive) stage the threshold + caps and reset
  weights to the shipped mix; nothing saves until both save buttons are pressed, and the
  badge only claims a preset while the draft actually matches one. Threshold-only presets
  keep the simulator exact.
- **Rule editors speak sentences.** The remove card has one form whose condition dropdown
  carries ramp phrases ("the older it is") that map to `GradedCondemnSpec`; the keep card
  pairs outright keeps with graded leans. Text-value inputs suggest what the library
  already contains via `GET /api/vocabulary/values` (genre/quality captured on `Candidate`
  at scan time), and free entry always stays valid. The suggestions are an in-app popover,
  not a native `<datalist>` — Safari only reveals a datalist after typed text
  prefix-matches, so an empty click showed nothing there, and embedded panes can misplace
  the native popup. The rule-form controls share one pinned height, because Safari's
  native select/input/stepper chrome otherwise staggers the row.
- **The simulator now names names**: a saved-vs-draft compare line, a threshold marker on
  the histogram, the top newly-flagged titles (`examples_newly_condemned`), and a per-gate
  spared-by tally (`protected_by`), all only when exact; the stale refusal is unchanged.
- **Copy rule**: no em dashes in operator-facing strings, either side of the wire (rule 21
  extended); season titles compose with a middot now, and the queue strips both separators
  so old snapshots still render.
- *Verified live* against a real library (a fresh several-thousand-item scan, presets
  restored, the two saves proven independent with the policy hash unmoved, autocomplete
  serving real genres). Found in passing: smooth `scrollIntoView` can silently no-op, so
  the rail jumps instantly; the deletion "why blocked" note pointed at the old Settings
  location and was fixed.

## The identity join and the why-panel honesty pass

An operator report ("this item IS matched in Plex, but Reaper says it isn't") led to a
cluster of related fixes, all live-verified against real services:

- **The Tautulli media-info listing is a cache, and it lags.** `build_movie_index` /
  `build_tv_index` used it as the *exclusive* spine, so an item added to Plex since
  Tautulli's last library refresh never entered the index at all — every resolver tier
  whiffed and the item was reported "unmatched" while Plex demonstrably had it (verified
  live: a day-old movie absent from `get_library_media_info` while Tautulli's own
  `get_metadata` served it). Fix: `library_guid_index` now returns full `PlexItem` rows
  (title/year/added-at ride along in the same plexapi listing, zero extra calls), and the
  builders union in any rating key the spine did not list. Spine rows keep Tautulli's
  `added_at` (dormancy byte-identical); plexapi-only rows carry Plex's own. A failed sweep
  still degrades; no Plex still means no enrichment.
- **Unmatched and ambiguous are different stories, and the panel now tells the right
  one.** `build_facts` wrote "Plex has not matched this item" for *any* missing rating
  key — but most keyless items on a split 1080p/4K library are AMBIGUOUS (one TMDB id
  naming two or three Plex items; the resolver correctly refuses to guess whose watch
  history to read). The Unknown reason now says "more than one Plex item matches this
  title/show" for those, threaded through the season path too.
- **The wire schema silently dropped `match`, `keeps` and `base_score`.** The stored
  explanation always carried them and the frontend always typed them, but pydantic's
  `extra="ignore"` stripped all three at the API boundary — so the "kept to be safe"
  notice and the keep breakdown could never render. Declared on `Explanation` now, with a
  regression test. Lesson recorded: a wire schema must name every key the UI reads;
  optional frontend types hide this class of bug completely.
- **"Left for you to decide" groups by cause.** Three amber boxes each ending "Plex has
  not matched this item" told the owner the same thing three times. The panel now parses
  the fixed "could not check {check}: {cause}" vocabulary, states each cause once in
  plain words, and lists the checks it blocked; anything unparseable (season-order
  conflicts, named custom rules) keeps its own verbatim box. The card's one-line reason
  prefers the match status over the first gate's engineer-speak.
- **The dormancy gate no longer claims "last watched N days ago" for never-played
  items** — the clock runs from arrival, so it says "untouched for just N".

## The folder corroborator: binding a title kept in two libraries

A second operator report of the same family as the round above ("there IS history in
Tautulli, but the item shows none, and it has no Plex link"). The investigation confirmed
the safety model held perfectly and found one honest gap plus one unrelated UI bug.

**What was actually happening.** The item was `AMBIGUOUS`, not unmatched: one TVDB id
naming two Plex listings, because the operator keeps that title in *both* an HD and a [redacted]
show section. On a **movie** the resolver breaks that tie with the file's exact byte size.
On a **show** it never could: a show is bound by its folder, both sections name the folder
identically, and Plex reports no size for a folder. Every such title abstained forever.
Measured live: 6 of [redacted] series, 13 of [redacted] season rows, all 3 titles the operator keeps
in both libraries. Details and the measurement in `docs/LEARNINGS.md`.

**The safety model was never in question — worth stating plainly.** An ambiguous item does
*not* read as "nobody watched it". `season_scan` writes `Unknown` (not `0`) for dormancy,
watchers and streaming; `signals.score()` is unsigned, so those weights stay in the
denominator and the missing evidence can only *lower* the score; the four dependent gates
land in `protections_unknown`, and `decide_verdict` abstains on a blocked protection before
the score is read, with the coverage floor as a second backstop. All 13 rows scored 7-18
against a threshold of 70 and stored `protect`. Fail-closed, exactly as designed.

**The fix.** The discriminator existed and was being thrown away one function earlier:
`clients/plex.py` reduced each Plex Location to its leaf and discarded the path. It now
keeps the full path on `PlexFile.path`, the *arr side passes its own full path, and
`identity._narrow_by_path_depth` compares **trailing segments** — never whole paths, since
the mount roots differ. A listing binds only by being *strictly* deeper than every other;
a tie, an unreadable path, or a leaf-only match still abstains. Verified end to end against
the live library: ambiguous 6 → 0, matched 972 → 978, unmatched unchanged at 112.

**Narrowed 2026-07-19 by code review B-2, so those numbers no longer hold.** The comparison
may not consume either side's mount root: two *arr instances mapped to the same container
path made the *arr's own root name look like evidence, and it bound the wrong copy.

**Narrowed again the same day, because stripping a fixed one segment is not removing a
root.** Adversarial verification broke the first attempt in the layout the popular
single-mount guides recommend, where each container maps its host directory to a
*two*-segment root: one segment came off, the second stayed, it happened to name one
library's folder, and the 4K entry bound the HD listing without the exact byte size ever
being consulted. Path length cannot say where a root ends — a container root may be one
segment or three.

**What the corroborator now is.** Each *arr instance's real root folders are fetched once
per scan (`ArrClient.root_folders`, parsed by `identity.root_folder_paths`) and passed to
`resolve_movie` / `resolve_show`. The longest reported root that prefixes an item's path is
that item's root, and only what sits strictly below it is compared
(`identity._below_arr_root`). With no root reported, or a path under none of them, the step
**stands down** — never a fixed strip.

**Narrowed a third time, and this one changed the shape of the answer.** Keeping the
one-segment strip on the *Plex* side was itself a wrong bind, and a regression against the
committed baseline: the strip is not uniform, so a candidate whose path the match fully
consumes loses depth while a deeper rival does not, turning a tie (abstain, keep) into a
strict win for the wrong copy. Ranking by depth is unsound whenever two copies' Plex roots
differ in depth. So there is no ranking and no strip on either side: the *arr's below-root
segments are the item's path *relative to its library*, and the one candidate whose Plex
path **ends with** those exact segments is the copy (`identity._ends_with`). Plex's root
never has to be known. Three guards, each from a reproduced wrong bind: the below-root
depth must match the *arr's own layout (`arr_layout_depth`), so a stale or over-broad root
cannot pass mount segments off as folders; an unreadable path on any candidate stands the
step down; and a winner whose size cannot be checked stands it down, since another
candidate may match the byte count exactly. Folder and exact size naming *different* copies
is a positive contradiction and **abstains** (`identity._size_contradicts`).

**A fourth pass, on the arbitration rather than the paths.** Adversarial verification found
two more wrong binds, both in how the folder step's answer was *used*. First, the
byte-identical-twins gate ran before a folder winner existed, so whenever any twin pair was
present the folder's answer was discarded unexamined and the contradiction veto became
unreachable: a folder answer naming a copy outside the twin group bound the group anyway.
The gate now runs after, and stands aside only when the folder's winner is *inside* the
group; a winner from outside is the same contradiction and abstains. Second, a failed
`/rootfolder` read returned an empty tuple, which is indistinguishable from an instance that
reports no roots. That is not a harmless loss of a bind: the folder step is the only thing
that produces the contradiction veto, so without it a stale Plex size binds the copy the
folder would have disputed. A failed read now yields `None`, which refuses the whole id
narrowing (`identity._narrow_among_id_hits`) and keeps the file. It still does not degrade
the snapshot, and that exception to rule 28 now names the refusal as its compensating
control rather than claiming the loss is free.

**What that costs, plainly.** Two instances mapped alike now tie and abstain; a movie
recovers by exact byte size, a show does not and is kept. Sonarr puts a series directly
under its root, so below the root a show has only the leaf both copies already matched on:
a show never gets folder evidence at all. That was the sharpest finding of the third pass.
The step's entire reach for shows had been paths *deeper* than Sonarr's layout, which only
happens when the reported root is wrong, so the feature's reach was co-extensive with its
failure mode and it failed toward a bind. A distinction living in the
root paths themselves is unrecoverable, and reading the roots' leaf names would not
recover it (both instances in the two-instance case report the same root leaf, so that
rule would bind both to one copy). Every loss is an abstain, which keeps the file. A failed
root-folder fetch does not degrade the snapshot: it can only stand the corroborator down,
and that can only cost a bind. The ratios above must be re-measured before they are
restated anywhere.

**The unrelated bug the same report surfaced.** Posters were reported missing everywhere.
The backend was healthy (every `/api/poster/*` returned 200 when driven), but
`ReviewQueue.Poster` held a `broken` flag with **no reset on `url`** while its sibling
`Backdrop` had one — so a single failed image latched the placeholder onto every item that
row was later reused for, until a full remount. A rule-19 miss, now fixed with a test that
fails without it. Artwork failures also logged rather than silently swallowed
(`api/poster.py`, `clients/tautulli.py`), so a placeholder is no longer indistinguishable
from an item that genuinely has no art.

**Still open, deliberately deferred.** `GroupOut` carries no `match` field, so the *show*
panel cannot show the "kept to be safe" notice the *season* panel already renders. Worth
doing next time the review surfaces are touched, alongside the deferred `.warn` → `.notice-
warn` merge. Also noted: the duplicate cards for one title in two instances are **correct**
(two instances, two file sets, two delete coordinates) and must not be merged — summing
their sizes beside a destructive button would violate rule 30 — but nothing yet tells the
operator the two cards are the same title.

## First-run honesty fixes (server picker, startup banner, TV-only scans)

Three first-run dead ends found while writing the deployment docs, each fixed with the
refusal semantics kept intact:

- **An account owning several Plex servers can now finish setup.** `complete_link` used to
  refuse outright with "select one explicitly" — and nothing anywhere let you select one:
  no web picker, no CLI flag, no parameter on any path. It now raises
  `PlexServerChoiceNeededError` carrying `{name, machine id}` candidates; both web flows
  (the login-time claim and the Settings re-link) render a picker and re-poll the
  still-valid PIN with the pick, and `reaper-admin link-plex` takes `--server
  <name-or-machine-id>`. Fail-closed properties preserved: no choice + several owned
  servers never guesses; a choice resolves only against the *owned* list, so no string a
  browser sends can land on an unowned server; two owned servers sharing the chosen name
  are refused with their ids rather than guessed between. The pending PIN is consumed on
  final outcomes only — the choice-needed state leaves it intact, exactly like the
  transient-probe path, so the owner is never dragged through a second OAuth round-trip.
- **The startup banner now reads the *effective* deletion switch.** It logged the env var,
  which only seeds the first run — so an install armed from the web UI booted saying
  "Nothing can be deleted", the exact false-safety-claim shape rule 7 exists for. The
  lifespan now reads `app_settings.runtime_safety` (as `/api/health` already did) and logs
  `reaper.armed` (a warning, deliberately: it is the line an operator whose `.env` still
  says disabled will look for) or `reaper.safe_mode`, truthfully. Verified live: armed via
  the UI with the env var false, restarted, watched the warning appear.
- **A TV-only deployment can scan.** `build_sources` hard-required a Radarr — a leftover
  from before season scanning existed, contradicted by its own "a movie-only deployment
  runs with no Sonarr" symmetry. The gate is now Tautulli plus at least one of
  Radarr/Sonarr, mirrored in `/api/setup/status` (`scan_ready`, new `has_sonarr`) and the
  wizard checklist ("Connect Radarr or Sonarr … at least one").

Also corrected in passing: `api/settings.py`'s module docstring still promised the removed
env-ceiling model ("*enabling* deletion still requires host access"), the same stale claim
family as §12's aftermath.

## The review-view context pass (badges, deep links, ratings, policy visibility)

The operator annotated the Review view asking for more context per item, with one
standing decision: **the data source priority is Plex first, then Tautulli, then the
*arrs — for everything.** All shipped and live-verified against real services (two full
scans on a copy of the dev DB):

- **Free capture from the sweep.** The plexapi section listing the identity sweep already
  iterates carries ratings-with-provenance, certification, runtime, and per-media
  `videoResolution`. `PlexItem` now carries them; both Tautulli-spine rebuild loops copy
  them (that double copy site is regression-tested); nothing costs an extra call. A strict
  stub test proves the sweep reads only listing attributes — plexapi's `__getattr__`
  auto-reload (a per-item HTTP call for unknown attributes) can never fire.
- **Six display-only Candidate columns** (`tmdb_id`, `title_slug`, `video_resolution`,
  `content_rating`, `runtime_minutes`, `ratings_json`), display-only by construction:
  Facts/gates/signals/policy hashing untouched. Baseline migration edited in place.
- **`ratings.from_plex`/`from_radarr` are wired into production for the first time** —
  and the audience slot now resolves a Rotten Tomatoes image to the audience score (both
  RT populations arrive as `rottentomatoes://` images; only the slot tells them apart).
  The displayed IMDb number is the SAME dataset entry the scoring signal froze
  (`dataset_entry` is shared by `build_facts` and the ratings assembly), so the panel can
  never show one IMDb value beside a signal that used another. TV inherits the show's
  ratings; seasons get no resolution badge v1 (show listings carry no media).
- **Deep links are server-computed** (`services/deep_links.py`, reusing `MediaRef.parse`):
  the panel title opens Plex Web. The client is served at two paths, chosen by host
  (`_plex_web_link`): the plex.tv-hosted app uses `/desktop/#!/…` and a self-hosted server
  uses `/web#!/…` (a Plex Media Server 403s on `/desktop`). Pills open Tautulli
  (`/info?rating_key=`) and the managing *arr — Radarr routes by **tmdbId**, Sonarr by
  **titleSlug** (the internal id in media_key does not resolve in their web UIs), and the
  arr link resolves the instance from `MediaRef.instance_id`, never "the first Radarr"
  (verified live: a 4K-instance item opens its 4K Radarr). Every missing coordinate hides
  that one link; nothing renders broken.
- **A `plex_web_url` AppSetting** (default `https://app.plex.tv`), editable under
  Settings → Plex, feeds the Plex link. Plain-language 422 on a non-http(s) value.
- **Cards**: resolution badge (4K/HD/SD + exact p-value), the dormancy line as a compact
  amber pill ("Not watched in 5y 9m", extracted from the stored UNWATCHED signal detail —
  serializer-only, old snapshots render it immediately), teal Movie chip
  (`--media-movie` token pair; TV keeps accent), scythe icon on Reap actions (close
  buttons keep ✕).
- **Policy visibility**: every operator-authored row wears a "Your rule" tag — custom
  protect rules in the protections lists (accent tint; wording rendered as "Kept by your
  rule: …" / "Your rule didn't match: …" while the stored detail stays the audit record),
  custom condemn rows in "Why it scored", and every "Leaning toward keeping" row. Fixed a
  pre-existing React key collision in `Gates` (all custom rules share `gate="custom"`).
- **Named custom rules are deferred** — protect conditions have no name field, and adding
  one churns the policy hash. Rows show their condition text; revisit post-release.
- **Outbound links (follow-up ask):** a "Seerr ↗" pill after Tautulli (Seerr item pages
  key on the tmdb id, `/movie/{id}` / `/tv/{id}`), and the ratings chips open their
  sites — IMDb and TMDb as true item pages (a new `imdb_id` column; seasons now also
  stamp the show's `tmdb_id`), Rotten Tomatoes as an honest **title search**, because RT
  slugs are hand-curated and no integration provides them. A season searches by its
  show's title, not "Show · Season 3". Chips without a link stay plain spans, never dead
  anchors. Also fixed in passing: "the first enabled Tautulli/Seerr" lookups are now
  ordered by id — the previous unordered `.first()` was nondeterministic, exposed by
  env-seeded instances landing beside fixture rows in a test database.

## Policy reverts were silently ignored (found live, fixed)

Saving a policy whose content matched **any** historical row was treated as a
content-identical no-op — but "active" means *newest row per media type*, so reverting to
an earlier policy (save A, save B, save A again) returned 200 with the reverted body while
the newer policy stayed in force. The wrong assumption: "hash already exists ⇒ nothing to
do" — true only when the match **is** the active row. Fix: `save_policy` short-circuits
only against the active row (via `_active_policy_row`, the one place the newest-row rule
now lives) and otherwise appends; `policy.policy_hash` dropped its unique constraint
(plain index now) so a revert can append a fresh row carrying the earlier hash. The hash
stays content identity — same hash, byte-identical canonical body — so snapshots and
approvals stay unambiguous, and the table now reads as the complete save history,
reverts included. Regression-tested (API test proven failing pre-fix) and verified
end-to-end against a live scratch server: rows A, B, A with the re-save of active A
appending nothing.

## The review-queue readability pass (one chip per lane, whole-show views)

The operator annotated all three queue tabs. Iterated as a rendered mockup first (four
options approved: seasons-pill expander, strip + full all-seasons list, kept-chip with
the number, overlay panel on narrow windows), then built and driven end-to-end against
a copy of the dev DB on an alt port.

- **One status chip per card, colored by lane.** Sanctuary cards drop the amber
  dormancy pill and the reason paragraph for a single green chip naming the protection
  that fired, with its number ("Kept · well rated: 6.8 on IMDb"). Limbo cards get a
  quiet gray chip for the low-stakes causes (under threshold, unmatched, checks that
  couldn't run) and an amber-outline "Needs a look" chip for the deliberate
  left-for-you flags (the season keep-rule conflict). Condemned cards are unchanged.
  The chip is derived server-side in the display layer (`routes._chip`) from the stored
  explanation — never a re-decision — by parsing the gates' own closed detail
  vocabularies (the WhyPanel CHECK_COPY precedent); every stored verdict state is
  enumerated and unit-tested (`tests/test_review_chips.py`).
- **Cards fold instead of wrapping raggedly.** The type chip moved out of the title row
  into the meta line; `.card-list` is a CSS container and narrow cards switch to a
  compact layout (small poster, actions on their own row, score pinned to the corner).
  Below 1100px the reasoning panel rides over the list as a right-hand sheet instead of
  stealing a column — the tablet-width split was what crushed the cards.
- **TV cards: the card opens the show, a labeled pill expands it.** The left chevron
  gutter is gone (posters sit flush on movies and shows alike); a "N seasons" pill with
  the chevron in the meta line toggles the season list; clicking the card opens a new
  show-level panel.
- **The whole show, everywhere.** `GET /api/groups/{group_key}` returns every season row
  in the latest snapshot across all lanes; the expanded card lists them all (other-lane
  rows dimmed, act from their own tab), and a per-season verdict strip on the card face
  (from compact `group_seasons` marks on the list payload, one rollup query with the
  reap-plan totals) shows kept vs condemned at a glance. Each strip square is a button
  that opens *that* season's reasoning (the mark carries the season's candidate id); the
  card head still opens the show. Reap-tab counts stay scoped to what "Reap now" would
  plan (rule 30): "3 of 12 would be removed · 41.9 GiB".
- **The show info panel.** Art, links, synopsis, the show's status chip, the FULL
  left-for-you sentence (finally somewhere it fits), and the season list; clicking a
  season flips to its complete reasoning with a back-to-show link.
- **Copy sweep:** six operator-facing backend strings still carried a literal `--`
  (rating-floor warning and vote-floor error in `engine/policy.py`, two executor
  errors, two degrade reasons, the Plex-link owner error). All reworded per rule 21.

## Season-rule knobs, queue filters, and panel actions (operator feedback round)

Everything here came from one annotated-screenshot review of the queue against live data.

- **The mid-binge hold now expires, and is configurable.** The sequential-progression
  guard held a viewer's place on all-time history: someone who abandoned a show years ago
  pinned their season (and the next) forever. Live data made the cost visible: ~23% of the
  snapshot's TV rows were kept by that one guard. Three new TV-policy fields, all applied
  through the one decision path: `keep_in_progress` (the guard's on/off, default on),
  `in_progress_hold_days` (viewer-inactivity expiry, default 180; `0` = the old
  hold-forever; unreadable last-watch keeps the hold), and the pure
  `season_pruning.active_progress` helper that applies the expiry to per-viewer progress
  before the guard sees it. The evidence side reads each viewer's most recent play per
  season (`user_season_last`, same query that fed the old DISTINCT pairs), rolled up per
  show fail-closed: any unreadable timestamp makes the viewer count as active.
- **Two hardwired season behaviors became policy toggles**: `keep_specials` (off =
  specials judged like any other season; they still never occupy a keep-last slot, and
  airing/still-downloading still protect them) and `flag_keep_conflicts` (off = the
  more-watched-than-kept detector stays quiet and the keep rule is simply followed).
  Both default to the old behavior. All four fields ride the policy hash, so the
  simulator honestly refuses stale numbers until the next scan.
- **The "request filter does not work" report was honest data, badly presented.** The
  filter worked; on the condemned tab zero items carried a requester (requested media
  gets watched; watched media is not condemned), so "Requested" showed a bare empty page
  and "Not requested" changed nothing. Fixes: every active filter renders as a removable
  chip above the list, and a filtered-empty queue now says how many items the filters
  hide, with a one-tap clear. The requested data itself was fine (~6% of the snapshot
  carried a requester, concentrated in the protected lane).
- **The queue filter bar grew up**: media filter says "TV shows" (was "TV seasons"),
  new genre filter (values from the existing `vocabulary/values` endpoint), new
  hand-override filter (spared / reaped / none) resolved through the one
  `whitelist.effective_override` function (keys resolved in Python, then an `IN` over the
  same conditions the totals use, so count/bytes/page describe one set), and filters are
  remembered per tab on the device (`loadFilters`/`saveFilters`, sanitized field-by-field
  on the way back in). Note for tests: bare `localStorage` resolves to Node's experimental
  global under vitest, and this jsdom ships without storage entirely; the helpers use
  `window.localStorage` and degrade to defaults when it is absent.
- **The why panel acts**: a sticky Spare/Reap bar (the queue's `OverrideControls`,
  exported) with pending/error states, invalidating `candidates`, `group`, and
  `candidate` so every surface refreshes together (the queue's own invalidation now
  includes `candidate` too, closing a stale-open-panel gap).
- **One chip family**: every chip is 1.35rem tall on one type scale; the status chip
  centers by construction (line box + padding = the family height) instead of a loose
  line-height; the show panel's season sizes no longer wrap to two lines. This closed the
  "pill spacing inconsistent on several views" and "bad padding, not centered" reports.
- **One status line per condemned card, enforced.** The reason paragraph used to stand
  down only when it would repeat the dormancy pill's wording; when the fresh scan made a
  different signal the leading reason, cards grew a second line under the pill. The reason
  now stands down whenever the pill is present, on both card kinds; the full sentences
  stay in the panel.

## The policy permutation lab (real-data sweep + standing harness)

The policy engine's option surface — nine gates with thresholds/windows, five weighted
signals, custom condemn rules over every field × operator, protect conditions, graded
keeps, two decision thresholds, eight season toggles — was swept against real library
data, then the sweep became a permanent test harness.

**Method.** Every candidate of the latest non-degraded snapshot (several thousand items) was
reconstructed offline into engine `Facts` from the local mirrors (watch events, IMDb
dataset, stored explanations) and replayed through the production code path
(`build_gates` → `evaluate_all` → `score` → `decide_verdict`). The reconstruction was
gated on **fidelity**: the replay had to reproduce the stored verdict, score, and
coverage of every single item before any permutation ran. It did (100% after two
extraction fixes — both extractor bugs, not engine bugs). On top of that base, ~30k
season-option combinations, randomized valid policies, the full field×operator custom-rule
matrix, threshold grids, and Unknown-degradation metamorphics all held their invariants.

**Two real bugs found, both fixed:**

1. **Custom-rule names could shadow built-in signal ids** (`policy._no_duplicates` now
   rejects them). A rule named `unwatched` produced two rows with one id in the stored
   explanation: the why-panel keyed rows on that id (duplicate React keys) and its
   "Your rule" tag test (`BUILTIN_SIGNAL_IDS`) mis-tagged the owner's rule as built-in —
   an attribution lie in the one panel whose job is honesty.
2. **The keep-rule conflict detector compared prunable seasons against specials**
   (`season_pruning._detect_conflicts` now excludes Season 0 from both sides, as its
   docstring already claimed). A kept-but-unwatched Season 0 flagged every watched
   prunable season as "Needs a look" — spurious refusals that train the operator to
   ignore the flag.

**The standing harness** (`tests/test_policy_permutations.py`, 75 tests, ~2s) replays a
de-identified fixture of real shapes (`tests/fixtures/policy_lab_vectors.json`: 440
stratified vectors + 100 show shapes, states and day/watcher counts only — no titles,
keys, or precise sizes) and enforces the invariants as properties: missing data never
condemns and never raises a score; every protection knob moves the condemned set one way
only; anything that validates must score; `decide_verdict` matches its spec at every
boundary; season plans stay consistent under all 32 toggle combinations per show. A
**pinned baseline** (every vector judged under the shipped default policies at extraction
time) turns the fixture into an engine-drift trip-wire: any behavioral change to
scoring shows up as a failing diff and must be acknowledged by regenerating the fixture
(`scripts/policy_lab_extract.py`, which any operator checkout can re-run against its own
data) in the same change.

**The other half of the loop: ingest validation against the sources.** The sweep proves
the engine is faithful to the mirrors; `scripts/validate_ingest.py` (read-only) proves
the mirrors are faithful to the *sources* — live Tautulli history (row counts,
last-played, watchers, and the mid-binge guard's episode-index/completion inputs), the
raw IMDb TSV (byte-exact full copy), and live Radarr/Sonarr (sizes, quality, ids,
genres, content-season sets, independently recomputed ranks). Run against the six live
dev instances: zero ingest bugs; the only diffs were plays since the last sync (a scan
re-syncs first) and upstream genre edits after the scan, plus one validation artifact
worth knowing: `get_history` prepends live sessions but excludes them from
`recordsTotal`, so naive last-page pagination hides the oldest rows. See LEARNINGS,
"The ingest is faithful to the sources".

## The whole-frontend UI/UX review pass (all 82 findings)

`docs/UI_REVIEW.md` swept every component plus the backend strings that surface in the UI,
and all 82 findings are now fixed and checked off. What it changed, in shape rather than
item by item: one critical contrast failure (`--faint` carried real text at 2.41:1, now
decorative-only, with those uses on `--muted` at 4.89-7.40:1); four responsive breakages
that only existed on a phone, including a why-panel whose full-screen rule never applied
because a 1100px `main.split .why` outranked the 900px bare `.why`; a sweep making every
gating query state its unknown state instead of asserting a definite claim, and every
mutation render its failure; a shared `ModalShell` giving both modals real dialog
semantics; the copy pass that took engine vocabulary and repr-quoting out of operator
strings; and the consolidation of a dozen twinned implementations.

Four lessons are recorded at the top of `docs/UI_REVIEW.md` because they generalize beyond
this pass: a fix can be worse than the bug it replaces (Cp1 turned a bad API key into
"Couldn't connect."), a half-finished sweep is its own defect (`fields.py` fixed,
`policy.py`'s twin not), paired findings must land together (Cp7 shipped without Cp6 and
briefly made two surfaces disagree about the same run), and reworded copy can start lying
about state it does not track (M1's "still selected" outliving the selection).

The `.warn` banner exception in rule 45 is still open: ScanBar and the review card keep
their own banner rather than `.notice-warn`, to be merged when the review UI is next
touched.

## Show status on the wire, and rules that say what they matched

Sonarr's series status was already frozen into every season's Facts, but it never left the
blob: an operator could write a rule against it and never see it anywhere. It now has a
column and reaches three surfaces. It is stored as a **string, not a nullable bool**, because
the value carries four states and a bool would force two of them together: `ended`,
`continuing`, `unknown` (the server did not say), and null (a movie, where the question does
not apply). Collapsing "we could not check" into "still going" is exactly the conflation the
observation model exists to prevent.

The card marks only ended and unknown; no chip means still going, which keeps the common case
quiet. The season panel and the show panel name all three. `unknown` wears the same amber
dashed treatment as an unchecked protection, and the label for `Known(False)` is "Still going"
rather than "Continuing", because that arm also covers upcoming shows.

Custom-rule explanations no longer print operator syntax. `Genre: <list> eq <one>` and
`True eq True` are now "Genre includes X" and "The show has ended", across the whole
operator and field-type matrix, in both the matched and unmatched readings.

Three defects fixed alongside, all found while answering an operator's question about a score:

- The season-rank signal called rank 1 "an older season" when rank 1 is the *newest* season
  on disk, and charged it deletion pressure under that description.
- The policy editor's "% of the score" divided by the built-in signal weights only, while the
  engine's denominator also includes custom rules, so every share was overstated and the
  rules themselves showed none. A rule joins the same denominator, so a rule worth 20 points
  moved a real item's score by about 3.5; there is now a warning saying so in plain words.
- `humanize_days` returned "today" for sub-day values, but every caller drops it into a slot
  that wants a *length*: "not watched in today". It is "less than a day" now.

Known and deliberately not changed: `humanize_days(729)` reads "1 year, 12 months". Rolling
12 months up to a year would make 364 days read as "1 year", which overstates dormancy, and
rule 31 says derived condemn-lane values round toward keeping. Left alone pending a decision.

## Weights became points, and the fact layer stopped inventing answers

Started as an operator question — "why can't my custom rules move the score?" — and the
answer turned out to be a unit that was never stated, sitting on top of three defects in
the layer underneath.

### The unit

A condemn `weight` and a keep `max_discount` were the same integer meaning different
things. `score()` normalizes by the sum of enabled weights, so a condemn weight is a
**share of a running total**: adding a second rule silently shrinks the first. A keep's
discount was always **literal points** off the finished score. Same visual grammar in the
editor, different unit, and nothing said so. A real tuned TV policy was found summing to 240,
so a rule written as 20 delivered 8. The drift is what real tuning produces, not an edge case.

Removal weights now total exactly `MAX_SCORE`, enforced in `PolicyBody`. At a total of 100
the normalization `100·P/D` collapses to `P`, so the number typed is the number the score
moves by, and it matches the keep lane. **The arithmetic is unchanged.** Both shipped
defaults already summed to 100, and a proportional rescale is score-preserving by
construction — verified on a real library: every movie re-scored with zero change; every season
seasons with a worst change under a point from integer rounding, four of which crossed the
threshold, all four toward sparing.

Equality, not `<=`. Under-allocating stretches the lane exactly as over-allocating shrinks
it, and one outage touching both lanes can net *upward* because keeps stay absolute while
the condemn side is attenuated. So the editor blocks Save in both directions.

Side effect worth naming: setting a signal to 0 used to drop it from the denominator and
**raise** every remaining score, which bites hardest on the signals arguing to keep. Under
a pinned total its points must go somewhere, so the denominator cannot move. The old
dilution warning is deleted rather than reworded (its state is now unrepresentable), and
`_at_least_one_signal` went with it as unreachable code.

Stored policies written before the rule are **rescaled, not discarded** — the operator's
own tuning in new units, handed to the editor as an unsaved draft, with nothing written and
no approval voided until they look. A scan on a rescaled policy **degrades the snapshot**:
the rescale cannot move a score, but the body was never saved by anyone, and an approval
names a policy hash.

### The invariant nobody had written down

```
base <= MAX_SCORE * coverage
```

Every unevaluated signal contributes zero pressure while keeping its weight in the
denominator, so a score cannot exceed the share of evidence that could be read. **`condemn_at`
is therefore itself a coverage floor**: an item cannot reach 70 without 70% of the policy's
weight being readable, whatever `coverage_floor_bp` says. This killed three of six
scoring-model proposals that would have let rules add points outside the denominator. Now
stated in `signals.py` and pinned by property test.

### The fact layer

`Absent` is a **privileged** state: it means "we looked, there is genuinely none", and the
keep lane acts on it by withdrawing protection. That is correct (an unrated title is not a
well-rated one), which is exactly why a builder must never manufacture one. Three did:

- **A rating we never asked for.** `dataset_entry` returned `None` both when the dataset had
  no row and when there was no id to look one up with. Recorded as `Absent`, a title whose
  *arr has no imdbId and which Plex could not match lost every rating-based keep, with
  coverage still reading 100% and nothing degrading. Split into `dataset_lookup`. Measured on
  a real library: of the items carrying `Absent` ratings, **~86% were this bug** rather than
  genuinely unrated.
- **A size we could not read.** `int(x or 0)` on both the Radarr and Sonarr paths turned a
  partial payload into an affirmative zero: maximum pressure on a size signal, and any "keep
  large files" rule silently stops holding the item. `has_content` already read the file
  *count* rather than the size, which is what let "it holds files" survive an unknown size.
- **A mirror that stopped moving.** Watch stats come from the local cache, not a live call,
  so a stalled ingest raises nothing and looks identical to a quiet library while every
  item's dormancy climbs. `horizon()` answers the opposite question, so `latest()` was added
  and the snapshot degrades past `MIRROR_STALE_AFTER` (48h, matching the whitelist bound).

And one on the delete path, found by auditing the above rather than by the above: **zero was
a confirmed measurement to the executor and an unreadable one to the parsers.** The same
partial payload produces both halves, so a stored 0 against a live 0 was no growth at all,
the "couldn't confirm its size" branch did not fire (`0 is not None`), and real files were
deleted with both numbers fabricated. Symmetric on movies and seasons. An **empty** live
file list passed the same way, marking a step verified having proven nothing while consuming
the canary, since the plan is ordered smallest-first.

### The fixture was rebuilding facts by parsing operator copy

`policy_lab_extract.py` never read `facts_json`. It reconstructed every fact from
`explanation_json` and the gate details — a second implementation of the fact layer, drifted
from the first in two ways at once. It carried its own copy of the Absent/Unknown collapse,
so the fixture **could not contain an Unknown rating** however many scans it read. And it
recovered season rank by regexing a signal's detail string for "number N counting back" — a
sentence reworded this same week, which silently deleted 210 known ranks from the sweep with
nothing failing.

It reads the frozen facts now. Regenerated: ratings gained the state they never had, season
rank came back, and dormancy's seven Unknowns turned out to be reconstruction artifacts. The
baseline moved, and that is the fixture becoming accurate rather than the engine moving —
proven by re-judging all 440 *old* vectors with the current engine to the *old* baseline,
zero mismatches.

**Standing lesson: a generator that rebuilds facts by parsing operator-facing prose breaks
silently on a copy edit.** It happened twice in one week. Nothing enforces this yet.

### Known defects, not fixed

Found by a fan-out audit after the size work. Labeled by what an operator would care
about, not by where they live.

One of the six is now closed: `watched_status` was coerced to `0` on ingest, which made
"Tautulli did not say" indistinguishable from "started it, did not finish". The sequential
guard reads a completed episode as `watched_status = 1`, so an unreported completion
*above* a viewer's known position put them further back than they were, and
`sequential_protections` then protected the season they had finished instead of the one
they were about to start. `SEQUENTIAL_LOOKAHEAD` is 0, so nothing cushioned it. Proven
against the real function, not argued: correct position protects S3, understated protects
S2 only, fully-unknown protects both. The column is nullable now, a genuine `0.0` still means "did
not finish", and a pair whose unknown episodes sit above its known ones drops to the
season-level fallback. The cache table is **rebuilt, not migrated** -- `ensure_schema`
compares the live columns against `SCHEMA` and drops the table when they differ, which is
what the baseline revision already said cache tables do. It costs one full re-sync (an
empty table makes the next sync a full one automatically) and it is the right trade twice
over: a migration is code that runs once against a shape nobody has locally, and the rows
it would have carried over are the untrustworthy ones. The blast radius on existing data is **unmeasurable by
construction**: the distinction was destroyed at write time, so the ~12% of rows sitting at
exactly `0.0` cannot be separated into reported and invented. The nightly sweep re-fetches
them truthfully.

**Can change what gets deleted**

1. **The canary lands on the item Reaper knows least about.** `planner.py` orders
   smallest-first so ordinal 0 is "the least costly possible mistake" (its words). A
   fabricated `0` sorts to the front, so the run's one real proof that deletion works is
   spent on the item whose size was never read. The comment and the behavior disagree.

   **Planned in full: `docs/SIZE_TRUTH_PLAN.md`.** The canary is one of four consequences
   of the same root cause (a fabricated zero on the accounting column); the others are both
   byte caps, the typed confirmation phrase, and the growth interlock, which does *not*
   catch a stored `0` below the 256 MiB drift floor -- so the claim at `snapshot.py:645-653`
   and `season_scan.py:1134-1143` that a fabricated zero "cannot reach a delete" is false as
   written. That plan supersedes the two "still open" size threads below.
**Says something untrue** — both fixed, dev @ this commit.

2. ~~**The queue renders "0 B" for a season that plainly holds files.**~~ Fixed. The
   argument that makes the fix safe is that a candidate always holds files: the movie scan
   skips anything without one (`hasFile`) and the season scan skips anything without one
   (`SeasonStats.has_content`). So a stored `0` on a single item is never an empty item, it
   is a size the *arr declined to report, and `format.itemBytes` renders it "Size unknown".
   Five per-item call sites moved to it (review queue movie card and season row, show panel
   season row, grace list, why panel). **Totals stay on `bytes`**: a sum can be genuinely
   zero, and one unreadable item makes a total low rather than unknown — the show panel's
   whole-show figure and the reclaim totals are still understated by exactly this, which is
   the nullable-column thread below, not this one.
3. ~~**`gates.py` claims an unset field "cannot change any verdict."**~~ Fixed as a comment,
   not a behavior change: `_UNSET` now says `Absent` is inert on the condemn and gate lanes
   and *not* on the keep lane, cites `signals.evaluate_keep`, and says why the live builders
   must set every field explicitly. The behavior was already pinned
   (`tests/test_engine_invariants.py`, `tests/test_custom_condemn.py`); only the prose was
   stale. Rule 7 — the comment was true when written and is not now.

**Latent**

4. **A season missing from the watcher map reads as zero watchers** (`season_pruning.py`),
   suppressing a keep-conflict warning. Both sides of the comparison default to 0 and an
   unmatched season usually abstains on coverage anyway, so it rarely flips an outcome. It
   is a warning, not a protection.

### Still open

- **The ceiling the operator originally hit is still there.** A rule is capped at the points
  given to it, and the score is still bounded by what actually fired. What changed is that
  the cap is a number they set and can read. If the real demand is "one rule that condemns
  on its own", nothing here delivers it — only an operator-authored condemn veto would, and
  `ConditionSpec` is protect-only by construction. Not recommended.
- **Both size threads are now planned in `docs/SIZE_TRUTH_PLAN.md`**, which corrects two
  assumptions recorded here. (a) Radarr's `movieFile.size` is *not* a substitute for
  `sizeOnDisk` on the accounting lane: the movie delete removes the **folder**, and
  `movieFile.size` measures only the **file**, so it is a lower bound. A lower bound
  under-counts a byte cap, and a cap that under-counts does not fire, which deletes *more* --
  the opposite direction from rule 31. It is display-only. (b) Sonarr *does* have per-season
  file sizes without a new endpoint: `episodefile?seriesId=` is already wired
  (`clients/arr.py:107-120`) and already summed live by `executor._send_season`. Sourcing the
  frozen size from it is a **bug fix**, not just acquisition: today the scan stores the season
  *folder* and the executor compares it against the summed *files*, so the season growth
  interlock has been comparing two different quantities since it was written.
- **The lab cannot cover the fact builders.** It starts from frozen facts, which is one
  layer downstream of everything the fact-layer work changed. `tests/test_fact_layer_states.py`
  is the only coverage there.

## Immediate next steps

1. **The live send** — wire `_send_for_real` + the exclusion-verify + the Plex refresh
   and trash interlock, then delete one hand-picked worthless file under supervision.
2. **Verify M7b against a real server** — the Leaving Soon reconcile and the Discord
   notifier are built and unit-tested, but neither has run against a live Plex/webhook
   (Plex is unlinked in the dev DB, no webhook configured). The live label write also needs
   a decision: it is a benign, reversible mutation currently gated as strictly as a delete.
3. **Plex settings UI** — `reaper-admin link-plex` works from the CLI; the web setup
   wizard still needs the same flow. (Plex is not linked in the dev DB.)

### Carried forward from the third review pass (dev @ `aa0417d`, closed 2026-07-19)

All 40 findings in `docs/CODE_REVIEW.md` are implemented. Five things were deliberately
left, each because the correct fix is wider than the finding and wants its own change:

1. **`Candidate.size_bytes` is still a non-null int, and the writers still say `or 0`**
   (B-5, agent rule 4). The safety hole is closed: an item whose size was never confirmed
   is now kept by `executor.size_confirmed` and excluded from the caps and the typed
   confirmation total, so a `0` can no longer be deleted against or under-count a cap.
   But `0` is still a sentinel for Unknown, which rule 4 calls a blocker. The honest fix
   is a nullable column, and it reaches 17 backend and 8 frontend files. **Closed
   2026-07-19** by `docs/SIZE_TRUTH_PLAN.md` Stages 1, 2, 3 and 3b:

   - `Candidate.size_source` (`db/models.SizeSource`) records which measurement the size
     is, so the executor compares like with like instead of a folder against a sum of
     files, and the scan emits `scan.size_source_tally` — the first measurement Reaper has
     ever taken of how often a size is simply never reported.
   - `Candidate.size_bytes` is nullable and the scan no longer fabricates a zero.
   - Three independent layers refuse an unmeasured item: `planner.build_plan` holds it
     back, `executor.size_confirmed` refuses it again per item, and `manifest_hash`
     encodes an unknown as JSON `null` so a size later measured voids the approval.
   - The operator is told: a count on the plan and confirm screens, "Held back: size
     unknown" on the card, the reason in the why-panel, and totals that read
     "4.2 TiB · 3 sizes unknown" rather than quietly summing low. Every count is hidden at
     zero.

   **Two findings worth keeping.** The plan's Stage 1/2 split could not work as written:
   widening the column's type turns mypy red at 22 consumer sites at once, whether or not
   a null is ever written, so the flip had to land with its consumers — where mypy's error
   list becomes the exhaustive checklist rather than something to silence with a coercion
   at each site. And the manifest hash turned out to be a third layer nobody had counted:
   taking a size away after approval voids the run on its own, which is why the executor's
   per-item test needs a deliberately re-approved plan to reach at all.

   Stage 3c also landed: `ProfileSettings.max_unmeasured_per_run` (0 by default, capped at
   25) lets an operator reap a bounded number of unmeasured items anyway. A count and not a
   switch, because the byte caps cannot bound that population at all. It never relaxes the
   test-item rule, they still count against the item caps, they never ride a show-level
   "Reap now", and a plan over the allowance aborts rather than trimming. The typed phrase
   gains `+ N UNSIZED` so the GB figure stays exact for what it covers.

   **The browser pass earned its cost.** Driving a seeded snapshot end to end found three
   things the 1689 tests did not: the queue header rendered a bare sum (a local variable
   shadowed the formatter), that header needed its own wording rather than the shared
   middot form (which put "would be freed" after a count of items that would not be), and
   policy warnings were inspected against a stand-in `ProfileSettings()` rather than the
   operator's own. That last one was pre-existing and made EVERY settings-based warning
   unreachable, including the danger about running without approval.

   **An adversarial review pass then found a critical defect neither the gates nor the
   browser caught, and it is worth remembering how.** Using the allowance once bricked
   every run for thirty days: the rolling 30-day cap raised on any past verified deletion
   whose candidate row had a NULL size, which was correct until the allowance made such
   deletions happen by design. The row keeps its NULL forever, so from the first allowed
   deletion every later run, dry runs included, aborted with no way out. Three more from
   the same pass: the canary rule held only relatively (with nothing measured, the
   unmeasured tail *was* the plan, so ordinal 0 had unknown cost); the executor read the
   allowance as a boolean, so lowering 25 to 1 was ignored; and the held-back count was
   always 0 with the allowance on, making the setting *reduce* what the operator was told.

   Every one sat at the seam between the allowance and a rule written before it existed.
   The generalisable lesson: **when a stage relaxes an invariant an earlier stage
   established, the docstrings asserting that invariant are where the bugs are, not merely
   where the prose is stale.** Four are now pinned by regression tests, each teeth-checked.

   Still open: Stage 4 (the operator's real-data pass) and Stages 5 to 7, which are gated
   on what Stage 4's tally shows.
2. **The refused-reap clause is a frontend map keyed on strings the server emits**
   (B-11, agent rule 12). Two modules holding the same literal is exactly what that rule
   forbids. The clean shape is for the server to send the refusal reason on the candidate
   payload, computed where `reap_override_verdict` already decides.
3. **`BOOL_OPS = (Op.EQ,)` renders a comparison box with one option that cannot be
   changed** (`fields.py:69`), in both the remove-rule and hard-keep forms. Either widen
   the tuple or have the editor render a single-op field as static text.
4. ~~**The folder corroborator runs before size and can contradict it.**~~ **Closed.**
   Found while fixing B-1/B-2, not raised by either. Two changes closed it: the step no
   longer *ranks* candidates by shared-suffix depth (which let a Plex copy whose own root
   is shallower lose on depth alone, binding the wrong copy) but tests for an exact suffix
   match on the item's library-relative path; and where it still points at a listing whose
   known byte size disagrees with the *arr's, it yields to the size
   (`identity._size_contradicts`), because the byte count is exact and a folder name is
   circumstantial. This is the module's own corroborate-or-silent rule, applied between
   corroborators rather than only between tiers.
5. **A stale Plex size can make the exact-size corroborator bind the wrong copy.** Found
   by the third adversarial pass on B-2, not raised by the review. An *arr upgrades a file
   in place and Plex has not rescanned, so Plex still reports the old size for the true
   copy while a copy in another library happens to carry the *arr's new size. The size
   branch then binds the stranger. This is not the folder step (it correctly ties and
   stands down); it is the size corroborator trusting a number Plex may not have
   refreshed. The old depth-ranking happened to bind correctly on one such input, by luck
   rather than by reasoning, and reinstating ranking would bring back the wrong binds it
   caused. Worth its own look: a Plex size older than the *arr's own file mtime is
   arguably unknown rather than known.
6. **The live folder-corroborator measurement must be re-run.** B-2's three narrowings
   (below each mount root, then strictly below the *arr's own reported root folder) mean
   the recorded "ambiguous 6 → 0" no longer holds, and the expected direction is now
   *more* abstains, not fewer. See the note in that section above and in
   `docs/LEARNINGS.md`.

### Open questions / decisions to make

- **Should the planner gate on grace?** Today a plan is built from *all* condemned items;
  a grace-aware planner would include only cleared ones. That is the honest end state, but
  it empties the plan until items age out of grace, so it changes the current demo. Held
  deliberately — it is a behavior decision, not an oversight.
- **`FEW_WATCHERS` and `LOW_RATING` earn nothing.** Consider dropping them; they add hash
  surface and bug surface for no measured skill. (A scoring change — delete-adjacent.)
- **Tests are not type-checked.** CI runs `mypy src/reaper` only; `mypy tests` reports ~190
  errors, almost all "missing py.typed marker" noise. A PEP-561 `py.typed` marker plus a
  handful of real fixes would let the suite be strict too.

## A TV show wrongly "on the IMDb Top 250" — cross-media-type id join (found live 2026-07-19, fixed)

A second instance showed TV shows protected as "on the IMDb Top 250", a movie-only list.
Nothing was written upstream — the Top 250 is read-only from Radarr's public mirror. The
bug was in our join: `MembershipIndex` matched a library item to a list row on the bare id,
and **TMDb numbers movies and shows in separate id spaces**, so a show whose TMDb id equalled
a Top 250 film's inherited that film's protection. Safe direction (a false *keep*), but the
why-panel stated a reason that was not true.

- **Fix:** the join key is `(media_type, id)`. `media_type` was already stored on every row;
  the in-memory index just dropped it. Threaded through `MembershipIndex.lookup` /
  `memberships()` (now required) and both call sites (`snapshot` movie = "movie",
  `season_scan` show = "tv"). Regression test reproduces the exact collision. See
  `LEARNINGS.md` → "TMDb ids are namespaced by media type".
- **While there:** removed `RadarrImportList` and `RadarrClient.import_list_movies()` — a
  fourth "provider" that was defined but never wired into any sync (rule 38). The three live
  sources are unchanged: IMDb Top 250, the `reaper-keep` *arr tag, and the Plex "Never Reap"
  collection. Import lists that apply a tag still protect via the tag reader, so no capability
  is lost.

## Deferred — watching a deletion run happen (detached execute + re-fetchable status)

Came out of a "would the web interface benefit from websockets?" review. Short answer to
that question: **no** — the interface's realtime needs are one-way (server → browser) and
single-operator, which is the WebSocket anti-pattern, and a socket would additionally
bypass our auth (see the guard note at the end). But the review surfaced one surface with
real value that we are choosing to **defer**, recorded here so it is ready to pick up.

**The gap.** `POST /api/runs/{id}/execute` (`api/runs.py`, the one route that deletes) is a
single **blocking** request: the executor walks the manifest re-check, the canary, then
every item's streaming veto / played-since check / *arr delete / Plex refresh, and only
returns the whole `RunReport` at the very end (`services/executor.py`, `execute` →
`_run_deletes`). The UI (`ReapConfirm.tsx`) shows a bare "Reaping…" spinner for the entire
run and paints the per-item checklist only on success. So the operator watches the
highest-stakes, minutes-long, irreversible action in the product **blind**: a stalled *arr
call looks identical to normal progress, and if the tab closes or the request times out the
after-action report is lost (it lives only in the response, and is never re-fetchable).

**Why deferred, not done now.** The safe fix touches the most safety-sensitive code in the
app. The prime directive resolves that ambiguity toward not touching it until per-item
deletion visibility is an actual requirement.

**The design when it is prioritized.** Reuse the pattern the scan already proves
(`api/scan.py`: a detached `asyncio` task + `app.state.scan_status`, polled over
`GET /api/scan/status`):

1. **Detach the run.** `execute` kicks off the deletion as a background task and returns a
   handle immediately, instead of holding the request open for the whole run.
2. **Add a read-only `GET` run-status endpoint** the browser polls on the existing 1s
   cadence. It reads per-item outcomes that are **already durably committed mid-run**
   (`executor.py`, the per-item `_mark_sent` / `_mark_verified` + per-item `session.commit`),
   so no new instrumentation is needed — only a read model over rows we already write.
3. **Survives disconnect.** Because status comes from committed rows, closing the tab and
   returning shows the run's real state and the final report; nothing is lost.

**Guardrails (non-negotiable).** The channel is **observational only** — telemetry out,
never a control channel. Deletion still flows *only* through the password-armed `execute`
POST with the exact confirmation phrase and `GuardedTransport`. Detaching the run must not
weaken any interlock: the canary-first ordering, per-item vetoes, caps that abort-not-
truncate, and the "a run executes once" atomic guard (`executor.py`) stay exactly as they
are. The initiating POST keeps its armed-host + confirmation-phrase gate; only the
*reporting* moves off the request.

**Transport ladder.** (1) Polling a status endpoint is the whole win and needs no new
transport machinery or operator proxy config — do this first. (2) SSE is an optional later
polish to shave the ~1s latency, justified only if runs grow large; it is a plain
authenticated `GET`, so it inherits the existing cookie auth. (3) **WebSockets: never** for
this — one-way data gains nothing from bidirectionality, and a duplex channel would be a
second, unaudited mutation path at odds with the safety model.

**Blocker to clear before *any* push endpoint (SSE or WS).** `AuthGuard`
(`api/middleware.py`) short-circuits every non-`http` scope, so a WebSocket handshake
reaches the app with **no** `resolve_session` and **no** CSRF check — it would be born
unauthenticated. SSE avoids this (it is a `GET` on the `http` scope and inherits auth), but
if a WebSocket is ever added it must authenticate the session cookie itself and validate
`Origin` at the handshake (the browser WS API cannot send our `X-Reaper-CSRF` header). Worth
fixing the guard's non-`http` branch to fail closed regardless, so a future endpoint cannot
slip through.

---

## Jobs page, rebuilt as one job list

The Jobs settings tab was three stacked sections (a Library scan card, a "Run automatically"
schedule block, and a bare "Background upkeep" run-now list). It is now **one card, one row
per job**, each row carrying its own schedule editor (`Edit`) and a run button, the pattern
operators know from Overseerr/Jellyseerr. Mocked as a self-contained HTML artifact, iterated
to approval, then built backend-first and driven end-to-end in the real app.

- **Every upkeep job is now individually schedulable, and switchable off.** The three
  background jobs (`refresh_ratings`, `refresh_curated_lists`, `full_history_sweep`) were
  hardcoded to a daily cron in `build_scheduler`. They now read a per-job override from a new
  `app_setting` key (`maintenance_schedules`, `{job_id: cron|null}`): a stored cron wins, a
  stored `null` turns the job off, and an absent key falls back to the code default
  (`scheduler.DEFAULT_MAINTENANCE_CRONS`) — the present-null-vs-absent distinction is what
  lets "off" survive a default-time change. `build_scheduler` wires the defaults; startup
  reconciles any stored override (a malformed stored cron leaves the default in place). One
  route, `PUT /api/settings/jobs/{id}/schedule`, edits the scan and the upkeep jobs alike;
  a bad cron is a 422, an unknown id a 404. `GET /api/settings/schedule` now returns *every*
  schedulable job (scan first) with `cron`/`default_cron`/`next_run_at`/`running`, so an
  off job is still listed rather than vanishing.
- **Run now works even when a job is off.** `run_maintenance_now` nudges a scheduled job in
  place (cron untouched) or, when it is off, runs it once as a self-removing one-shot — so
  "run now" never quietly turns the schedule back on.
- **Honest "running now" feedback.** APScheduler `SUBMITTED`/`EXECUTED`/`ERROR` events are
  mirrored into a live set (`track_running_jobs`, held on `app.state`); the schedule payload
  carries `running` per job, and the page polls only while something runs.
- **Frontend.** The scan keeps its live progress + last-scan line (now with the *time*),
  every run button is the accent primary with a same-size `Edit` beside it in fixed columns
  (rule 51), and the schedule editor is one `ModalShell` for all jobs (presets + off +
  custom cron, with a plain warning when an upkeep job is turned off). The Leaving Soon
  "Update now" moved here as its own row (with its shelf stats and a link to the Plex toggle
  that gates it; greyed and disabled when off); its status line stays on the Plex tab.
- **Copy corrections from the review of the mockup:** "Library scan" → "Update library and
  apply policy" (a quick diff, not a full re-read); "Full watch-history sweep" → "Full
  watch-history update". Verified live: render, both modals, custom-cron reveal, a schedule
  save round-tripping through the API, and the running state.

## A reap you can stop from anywhere, and that tidies up after itself (mocked, approved, built)

The one deletion endpoint used to run **inline in its request** and hold the reap modal open
for the whole run, with Cancel disabled: a long reap was trapped in the sheet, closing or
navigating away had no handle on it, and there was no discoverable emergency stop. The reap
now runs **detached from the request, like a scan**, and Stop is reachable from any screen.
Mocked as a self-contained HTML artifact (both themes, the modal and the "navigated away"
bar), iterated to approval, then built backend-first with the full gate suite green.

- **The reap is backgrounded.** `POST /runs/{id}/execute` runs its synchronous gates (armed,
  the content-bound phrase, and a new client-presence pre-check that mirrors the executor's
  own no-Plex / no-Tautulli refusals so a misconfigured run still 409s immediately), then
  starts the run on `app.state.reap_task` reporting to `app.state.reap_status`, and returns
  that initial status — not the finished report. `GET /runs/execute/status` is polled to
  follow a run and to re-attach to one already in flight; the report lands on the status when
  the run ends. Only one reap runs at a time.
- **Stop is graceful, never a hard kill.** `POST /runs/{id}/stop` sets a flag the executor
  reads before its next item (alongside the arm-recheck), halting after the item in flight
  via the same `ExecutionError` path a mid-run disarm uses — so the abort path runs and still
  tidies Plex. It leaves deletion armed (stops one run, does not disarm the host); turning
  deletion off stays the separate, independent kill switch. Stop is a convenience, not a
  fail-closed interlock: an unreadable stop flag resolves toward *continuing* (the arm-recheck
  is the fail-closed guard), so a blip never halts a healthy run.
- **A stopped run still cleans up Plex.** `executor.execute` now runs its final state commit
  and `_finalize_plex` in a `finally` (catching `CancelledError`), so whatever was removed
  before the halt — a graceful Stop, a canary abort, or even a hard cancel on shutdown — has
  its stale Plex entries refreshed and purged, and the run never lingers in `EXECUTING`. This
  was already true for a graceful abort; the `finally` makes it hold under backgrounding too.
- **A finished run rebuilds the stale queue.** Removing files leaves the last snapshot's
  review queue and policy preview stale, so on any real run that removed at least one file
  (completed *or* stopped) the executor kicks a fresh scan through the one shared
  `scan.launch_scan` — the same one scan mechanism, not a parallel copy.
- **The executor gained two injected hooks, both additive:** `stop_recheck` (the graceful
  Stop) and `progress` (a cumulative `ReapProgress` emitted after each item, feeding the
  polled status). A dry run and any headless run pass neither.
- **Frontend.** `ReapConfirm` no longer traps the sheet open: once a reap starts it shows a
  live progress bar and a green **Stop** (keep-first grammar — the safe action invites in
  green like Spare, never red), the sheet closes freely, and the report renders from the
  polled status when the run ends. A new app-wide **`ReapBar`** (companion to `SafetyBanner`,
  armed-red while running, safe-green when just stopped) carries the count, a progress fill,
  **Stop**, and **View** to every screen, so a run you navigated away from is still stoppable;
  View reopens the sheet by run id (`ReapSheetLoader`).
- **"Souls", not "items", on every reap surface**, singular-aware (`1 soul` / `N souls` via a
  shared `format.souls`), including the server-recomputed confirmation phrase
  (`REAP 1 SOUL 0 GB` / `REAP 42 SOULS 118 GB`) — `planner.confirmation_phrase` changed in
  lockstep with the UI so execute still validates.
- **Tests.** New executor tests: Stop halts before the next item, a stopped run still
  refreshes and purges Plex, an unreadable stop flag keeps running, and progress is emitted
  per item. New API tests: the status reads idle before any run, and Stop with nothing running
  is a 409. `ReapConfirm.test` rewritten for the detached contract (progress + Stop, the sheet
  closes freely, the report arrives via the status). Not driven against a live armed instance:
  a real reap deletes real files, so end-to-end confirmation of an actual deletion is left to
  the operator in a controlled setting.

**Adversarial review (deletion path), all fixed.** A focused multi-agent review of the diff
surfaced six confirmed issues, each fixed and covered by a test:
- **[high] shutdown had no owner for the detached reap.** The lifespan cancelled `scan_task`
  but never `reap_task`, so on shutdown the engines were disposed under an in-flight deletion
  and the executor's `CancelledError` cleanup never ran — the run could linger `EXECUTING` with
  Plex un-tidied. Now the lifespan cancels **and awaits** `reap_task` (bounded 20s) before
  disposing engines, so the abort+finalize runs against live resources. New executor test:
  a hard cancel marks the run ABORTED and still refreshes+purges Plex.
- **[med] the reap slot could wedge.** `execute_run` claims the single slot synchronously,
  but the release handler caught only `HTTPException`; a crypto/DB error out of
  `build_reap_gateway` left `running=True` with no task to clear it, jamming the one deletion
  endpoint at a permanent 409. Now it releases on **any** exception. New API test.
- **[med] an errored reap vanished from the app-wide bar** (`ReapBar` treated only
  complete/aborted as ended). Now `error` is a terminal state with its own amber tone and the
  error text, and the backend re-scans on the error path too when files were removed.
- **[low] reopening a live run showed stale/contradictory stages** — the arm+Reap stage lit
  while a *different* run held the slot, and the dry-run "practice run" blurb rendered over
  live progress. Both gated on `!running && !report` (and a "another reap is running" notice);
  the dry run is skipped entirely when reopening an in-flight or finished run.
Also fixed a same-diff race I caught first: the slot's check-then-set spanned an `await`, so
two concurrent executes could both pass — now an atomic synchronous claim.

## Multi-instance correctness (Seerr merge + Tautulli singleton)

A multi-agent audit of every service's multi-instance path, prompted by an operator running
two Seerr portals who found the second portal's requesters missing from Scales.

**The model, now consistent and enforced:**
- **Radarr, Sonarr, Seerr are genuinely multi.** Every enabled instance is read and merged.
  Radarr/Sonarr were already correct (a client per `RadarrSource`/`SonarrSource`, the
  instance id baked into `media_key = radarr:{id}:{...}` so an HD + 4K split never collides,
  deletion routed back to the owning instance).
- **Tautulli and Plex are singletons.** Tautulli mirrors one Plex server's watch history
  (keyed by that server's rating keys) and Reaper connects to exactly one Plex, so a second
  Tautulli has no working setup.

**Seerr was silently first-only.** `scan_runner.build_sources` and the Scales endpoint each
picked the Seerr with `next(...)` — the very anti-pattern the `build_sources` docstring warns
against for the *arr. Two harms: Scales omitted the second portal's requesters while looking
complete; and worse, on the scoring path the *reachable* first Seerr made the request index
`available=True`, so a title requested only in the second portal resolved to a confident
`Known(value=False)` "not requested" instead of `Unknown` — a fail-open that stripped the
keep-last "requested" protection and could add condemn pressure. Fix: `build_map` /
`build_request_index` / `fairness.build_report` now take a **list** of clients and merge.
Three deliberately different failure policies: the display map is soft (best-effort, a broken
portal contributes nothing); the Scales report is fail-hard (any unreachable portal → 502,
never a partial-looking leaderboard); and the **scoring index is fail-closed to the whole
set** — if *any* one of several Seerr is unreachable, the entire index degrades to
`available=False` (all `Unknown`), because "requested nowhere" can only be asserted once every
store has been read (rule 2). Empty list stays `_EMPTY_INDEX`, not `available=True` over empty
keys.

**Tautulli singleton is now a real invariant, enforced at both creation paths:** the UI
create route (409, and the "Add a Tautulli" card hides once one exists) and env-seeding
(`seed_instances` skips a second, in-batch set covers an unflushed session). The poster reader
became ordered-first instead of `scalar_one_or_none` so it can never 500 even if the invariant
were violated. The scan's `next(...)` for Tautulli is now correct by that invariant.

The deep-link "View in Seerr" base URL is left as first-only (cosmetic, noted).

## Scales details drawer + Seerr request quota (in progress)

Operator-driven feature: a per-person **details drawer** on Scales (opened from a card),
plus surfacing Seerr's own **request counts and per-type limits**, plus a copy fix (the
watched line now names its denominator: "they watched 25% of what they asked for").
Design settled over a mock-first iteration (self-contained HTML artifact, approved).

**Backend: done and verified (full suite green).**
- `clients/seerr.py`: new read-only `users()` (paged, requestCount + plexId join) and
  `quota(user_id)` (`GET /user/{id}/quota`), with `SeerrUser`, `UserQuota`, `QuotaStatus`
  types. Movies and series are **separate** limits with their own window and unit; a
  zero/absent limit is unlimited; `restricted` is forced false without a real limit.
- `services/fairness.py`: `build_person_detail` (a person's whole request breakdown over
  the last scan — title, year, quality, requested/arrived, watched-by-them, fate,
  co-requesters, sorted reclaimable → limbo → kept), plus `_enrich_accounts` (best-effort
  quota fold across portals: **tightest finite limit wins, `at_limit` OR-ed**, per type)
  and list-row enrichment (`seerr_total`, `movie_at_limit`, `tv_at_limit`). Quota is
  display-only, so best-effort: an unreadable user list drops the extras, never the report.
- `api/fairness.py`: enriched `/fairness` rows and a new `GET /fairness/people/{user_id}`
  route. `api/schemas.py`: `QuotaLineOut`, `PersonQuotaOut`, `PersonTitleOut`,
  `PersonDetailOut`. Keyed on the Seerr `user_id` (the row identity upstream's review pass
  moved rows onto, rule 6/12), not a synthesized key; `build_person_detail` and the drawer
  open by it. Sizes are nullable end to end (a title with no measured size reads "size
  unknown", never a false 0 B), and the tmdb join is namespaced by media kind.

**Key decision (multi-portal quota):** limits are per portal in Overseerr. Rather than a
per-portal UI, the aggregate shows the *tightest* finite limit and flags `at_limit` if
capped on any portal — the honest "most constrained" reading. The list card names only
*which* type is capped (movie / series / both), never a made-up window; the exact
"1 per 14 days" numbers live in the drawer.

**Remaining:** frontend (api.ts types, `ScalesDrawer` reusing `ModalShell`, `Fairness.tsx`
card enrichment + sort control + watched-line reorder, CSS, vitest); the rating-key
display-join refinement for per-file `requested_by` precision (improvement #1); an
adversarial-review pass; and an end-to-end browser drive.

## Per-copy "requested by" via a Seerr service -> instance map (done, gates green)

**The problem.** "Requested by" joins Seerr to a candidate on tmdb/tvdb, which is a *union*:
a title kept in two libraries (a main one and a restricted one for a specific group) shares
one tmdb, so every copy showed everyone who asked for the title, not who asked for *that* copy.
The rating-key join can't fix it (Overseerr has one non-4K `ratingKey` slot, so it collapses),
and Seerr stores no file path. What a request *does* carry is the *arr's own item id
(`externalServiceId`) and the portal-local `serviceId` -- and `externalServiceId` equals the
`movie_id`/`series_id` in Reaper's `media_key`. The one missing dimension is *which* Reaper
instance a portal's `serviceId` points at (serviceId numbering is local to each Seerr).

**The fix: three tiers, best-first.** `build_map` files each request under up to three keys and
`snapshot`/`season_scan` read them in order:
1. **Service map** (declared) -- the exact copy's `media_key`, when a nullable
   `instance.service_instance_map` JSON (`{"{kind}:{serviceId}": reaper_instance_id}` -- kind
   namespaced because Seerr numbers Sonarr and Radarr services separately, additive migration
   `4d5e6f708192`, NULL = no map, no rebuild) resolves the request's `serviceId`. Copy-true
   whatever Plex sync saw, because `externalServiceId` names the *arr the request was routed to
   (`movie_instance_key`/`season_instance_key`/`show_instance_key`, equal by construction to the
   candidate's own key).
2. **Rating key** (zero-config) -- the request's Plex `ratingKey` (`rating_key_key`), which equals
   the candidate's `plex_rating_key` on the same Plex server. Correct with no setup whenever a
   portal scans only its own library; mis-points only when a portal scans several libraries
   (Overseerr's one non-4K slot then kept a different copy), which tier 1 overrides. Movies match
   the movie key; TV matches the *show* key (Seerr stores a TV request's ratingKey show-level).
3. **tmdb/tvdb union** -- today's behavior, the last fallback.

So a scoped setup just works from tier 2 with no mapping; the service map is the escape hatch for
a portal whose ratingKey collapses. Still display-only, never a gate; `build_request_index` (the
scoring FACT) is untouched.

**Config UI.** `GET /instances/{id}/seerr-services` lists the portal's Sonarr/Radarr services
(`/settings/{sonarr,radarr}`, admin key) each with a suggested Reaper instance (host:port match,
exactly-one-or-none). Edit-Seerr modal grows a "Requested-by instances" section in the library-map
grammar (per-service instance select, "suggested" tag that clears on pick, save sends what's shown,
unreadable list -> notice not empty). `SeerrService`/`decode_service_instance_map` treat a bad body
as `{}` (rule 32). Serves the real 2-Seerr / 2-Sonarr, main-vs-restricted-library setup: the clean
1:1:1 case where the map is tiny and unambiguous. **Incorporated improvement #1** (the rating-key
join) as tier 2 rather than discarding it: the Plex `ratingKey` is unique per server and copy-true
for a portal scanning only its own library, so it earns the zero-config default; it fails only
when a portal scans several libraries (Overseerr keeps one non-4K slot per title per portal, so it
collapses to whichever copy it synced last -- see LEARNINGS), which is exactly the case the
declared service map overrides. The rarer multi-Plex-server ratingKey id-space collision keeps the
rating-key tier below the declared map and above only the union.

**Gates:** ruff/mypy clean, 1858 backend tests (+22), 187 frontend tests (+4), alembic upgrade+check
clean, docker build clean. Not yet driven end-to-end against a live multi-Seerr instance.

## Review queue keeps step with the latest scan (dev @ TBD)

**The gap.** A scan finishing while the review queue was open left it showing the previous
snapshot: the backend served fresher rows, but the mounted `["candidates"]` query kept the stale
page until a manual filter change, navigation, or override. The `["scanStatus"]` poll was already
global (it feeds the shell's scan line on every screen) and already carried the finish signal
(`running` false-edge + the new `snapshot_id`), but nothing on the review page consumed it -- the
only "on scan completion, invalidate" reflex lived in `ScanRow`, which mounts on Jobs alone.

**The fix (frontend, one additive backend header).** `list_candidates` now returns
`X-Snapshot-Id` (the snapshot the page was drawn from; no schema change, no migration). The queue
compares it against the newest completed scan (`scanStatus.snapshot_id`, passed to `ReviewQueue`
as `latestScanSnapshotId`) in one derived hook, `useReviewFreshness`: the whole state machine
hangs off one fact -- *the list is behind* -- so ANY refetch that pulls the latest snapshot (a
hand override, a filter change, Show latest) clears it on its own, with no dismiss flag the app
has to remember to reset. Decided once per newer snapshot, from the busy state at that instant:
idle at the top refreshes quietly and a brief bottom-center toast ("Updated to the latest scan.")
confirms the swap, so the numbers never change with no acknowledgment; mid-review (scrolled, a
panel open, a selection or write in flight) holds the reviewer's place and raises a sticky nudge
built from the app's own accent tokens. Dismiss defers to a slim "one scan behind" marker rather
than a silent stale list -- resolving toward never leaving the operator unknowingly on an old
scan. An override applied on a stale row was already safe (keyed on the durable `media_key`,
snapshot-independent) and already refetched the queue, so "defer, then decide anyway" lands
correctly and pulls the view current in one action; the nudge derivation makes that fall out for
free.

**The toast that got left out (dev @ 2026-07-23).** The approved mockup had *two* silent-path
signals -- the mid-review nudge and, on the idle path, a bottom-center confirmation toast -- but
the first cut shipped only the nudge and made the idle refresh fully silent, so an operator idle
at the top saw the list's numbers change with nothing to say a scan had landed. Restored the
toast: a `.scan-toast` pill (`position: fixed`, viewport bottom, self-clearing after 2.6s) raised
by a tick the queue bumps whenever `useReviewFreshness` fires its silent refresh. The hook stayed
pure -- the toast is a rendering concern on the component, so its unit contract ("idle refreshes
quietly, no nudge/marker") is unchanged; "quietly" now means "without moving the reviewer's
place," not "without a word."

**Gates:** ruff/mypy clean, 1973 backend tests (+1), frontend tests +1 (a queue toast test),
alembic upgrade+check clean (no drift), frontend lint/build clean. Driven end-to-end in a real
browser against real data (snapshot 26, 193 condemned): baseline quiet, nudge on a mid-review
scan, Show latest clears it, idle-at-top refreshes silently *with the toast* (pinned 18px off the
viewport bottom, centered, self-clearing), dismiss to the marker -- all verified.
