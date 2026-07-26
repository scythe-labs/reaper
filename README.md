# Reaper

Explainable media library pruning for Plex.

Reaper finds media nobody watches — things requested and never played, shows whose
old seasons no one returns to, low-rated files quietly eating disk — **explains why
it thinks each item is expendable**, and removes it safely through Sonarr and Radarr.

It integrates with **Tautulli** (watch history), **Sonarr** and **Radarr** (the only
components allowed to delete anything), **Seerr** (requests), and **Plex**.

> **Status: in development.** Deletion is implemented and tested, but it ships **off**: a
> new install can only scan, score and explain until you deliberately arm it. Expect rough
> edges, and read [Running it](#running-it) before you point it at a library you care about.

## Why another one of these?

[Maintainerr](https://github.com/Maintainerr/Maintainerr) and
[Reclaimerr](https://github.com/jessielw/Reclaimerr) already do rule-based Plex
cleanup, and do it well. Reaper's reason to exist is the thing they don't do:

- **A score that shows its work.** Not just *which* rules matched, but every
  protection that was *checked and didn't fire*, with the actual numbers:
  `✓ checked: recently watched — last play 612d ago, your floor is 730d`.
  It explains the **keeps** too, not only the deletes.
- **Curated lists as protection.** Never reap anything in the IMDb Top 250.
  No other tool ingests curated lists as a protection source.
- **A countdown your users can see**, surfaced as a *Leaving Soon* label in Plex.
  Watching a title keeps it; so does sparing it by hand.

## Safety

Reaper deletes irreplaceable data from a server other people depend on.
**Every ambiguity resolves toward keeping the file.**

- **Off until you turn it on.** A new install starts read-only: it can scan, score and
  explain, and nothing else. The refusal lives at the HTTP transport, not in scattered
  `if dry_run:` checks — while deletion is off, a mutating request is blocked *before it
  is sent*, whatever the calling code believes it is doing. Turning it on is a
  deliberate, password-gated act — see [the deletion switch](#the-deletion-switch).
- **Unknown never condemns.** A missing rating, an unmappable user, or a degraded
  data source can only ever *protect* an item. This is enforced by the type system.
- **Nothing is deleted while it is being streamed.** The active-session veto is
  re-checked immediately before every single delete.
- **Reaper only acts through Sonarr and Radarr.** It has no filesystem delete path.
  Media that no *arr manages cannot be deleted, only reported.

### The Plex token

When you link Plex, Reaper stores the access token plex.tv returns for your server.
Be aware of what that is:

> **This token grants full administrative control of your Plex account, including
> permanent deletion. Treat Reaper's database as equivalent to your Plex password.**

This is not caution for its own sake. We verified against a live account that the
per-resource `accessToken` plex.tv hands out for an *owned* server is **the same
string** as the account credential — it is not scoped to that one server and is not a
mitigation. Reaper encrypts it at rest (Fernet) and redacts it from logs, but it cannot
make a full-power credential into a narrow one, and it does not pretend to.

## Running it

Reaper ships as **one container**, and it needs no access to your media — it has no
filesystem delete path, only Sonarr and Radarr do. The only volume is its own small
database. If you find yourself mounting your library, something is wrong.

CI publishes an image on every push, so there is nothing to build:

```bash
docker login <registry>    # this registry refused anonymous pulls
docker compose up -d                # serves on http://localhost:8420
```

Use **`:dev`** for now: it tracks the `dev` branch, where the work lands. `:latest` follows
`main` once a release is cut, and every build is also tagged with its short commit sha if
you would rather pin one. To build from source instead, uncomment `build:` in
[`docker-compose.yml`](docker-compose.yml) and run `docker compose up -d --build`.

Migrations run on every start, before the app accepts a connection — a half-migrated
schema must never serve traffic for a tool that deletes media.

On **Unraid**, use [`contrib/unraid/my-Reaper.xml`](contrib/unraid/my-Reaper.xml) instead:
copy it to `/boot/config/plugins/dockerMan/templates-user/`, then add the container from
the template dropdown.

> **Ownership is handled for you.** The container starts as root only to chown its data
> folder, then drops to an unprivileged user before it opens the database, so a bind mount
> created owned by root just works. Set `PUID`/`PGID` to choose that user (default 1000);
> on Unraid, set them to `99`/`100` to keep the appdata folder owned by `nobody:users`. The
> app process itself never runs as root. To keep it off root entirely, pin
> `user: "1000:1000"` and create the folder owned by 1000 yourself.

### First run, in order

1. **Open the web UI and Sign in with Plex.** The first account to sign in claims the
   install and creates its admin in the same step — *provided it owns the server*. An
   account that merely has access is refused: Reaper is about to be handed permission to
   delete media, so it is linked by the owner or not at all.
2. **Create a local admin as well, straight away:**

   ```bash
   docker compose exec reaper reaper-admin create-admin --username <name>
   ```

   Reaper logs a warning on every start until one exists. Plex sign-in is additive
   convenience, never the only key — a plex.tv outage, a revoked token, or a rebuilt
   server (which changes the `machineIdentifier` the ownership check reads) would
   otherwise lock you out of your own tool. See [Not getting locked out](#not-getting-locked-out).
3. **Add your services** in the UI: Sonarr, Radarr, Tautulli, Seerr. Keys are entered
   there, stored encrypted, and never come back out of the API. The wizard calls you
   *scan-ready* once **Tautulli plus at least one of Radarr or Sonarr** exist — a
   movie-only or TV-only deployment is a real deployment. Plex is not required to scan.
4. **Scan, and just read it.** Deletion is off. Every candidate shows its score and, more
   usefully, every protection that was checked and did *not* fire.
5. **Turn deletion on only once you trust it**, under Policy → Deletion, with your admin
   password. See [the deletion switch](#the-deletion-switch).

> **Own more than one server?** Reaper never guesses which library to point a deletion
> tool at — it asks. Pick the server during sign-in (the CLI takes
> `reaper-admin link-plex --server <name>`), and change it later by unlinking in Settings.

### Back this up

The `/data` volume holds two things you cannot regenerate:

| File | Why it matters |
|---|---|
| `reaper.db` | Your policies, decisions, whitelist and audit trail. |
| `secret.key` | The **only** thing that can decrypt your stored API keys. |

`cache.db` sits next to them and is disposable by design: it is other people's data
mirrored locally (watch history, the IMDb dataset), and Reaper rebuilds it. Separating
them states the invariant out loud — nothing in `cache.db` is a source of truth.

## Configuration

Three tiers, with a deliberate rule: **`.env` holds only what must exist *before*
the database, or be changeable *without* the web UI.** Everything else lives in the
database, encrypted, and is edited in the UI.

| Where | What | Why there |
|---|---|---|
| `.env` | `REAPER_SECRET_KEY`, `REAPER_SECRET_KEY_OLD` *(both optional)* | They decrypt the database's secrets, so they cannot live inside it |
| `.env` | data dir, host, port, logging | Needed before anything else exists |
| `.env` | `REAPER_RECOVERY` | Must work when the UI has locked you out, so it has no setting in the UI |
| `.env` | `REAPER_ALLOW_UNARMED_LEAVING_SOON` | Seeds the first run only; after that the UI owns it, under **Settings → Plex → Leaving Soon**, as "Update while read-only" |
| `.env` | `REAPER_DESTRUCTIVE_ACTIONS_ENABLED` | Seeds the first run only; the UI owns it after that ([details](#the-deletion-switch)) |
| `.env` | `REAPER_SERVE_SPA` | Development only, so Vite can serve the UI instead. Production leaves it on |
| **DB** (encrypted) | Instance URLs + API keys, the Discord webhook, policies, schedules, whitelist | Editable and rotatable without a redeploy |

### The encryption key

You don't have to supply one. If `REAPER_SECRET_KEY` is unset, Reaper generates a
key on first boot and saves it to `<data_dir>/secret.key` (mode `0600`), reusing it
on every subsequent boot.

**Back that file up alongside your database.** It is the only thing that can decrypt
your stored API keys — lose it and every integration must be re-entered. The key is
generated *once* and never rotated *automatically*: a key that changed on restart would
silently render every stored credential unreadable, and you would only find out the
next time a scan tried to reach Sonarr.

Setting `REAPER_SECRET_KEY` explicitly always wins, and Reaper then writes no key
file at all — so a secret manager stays the single source. An existing generated key
file is never overwritten, since your database may still be encrypted with it.

**Rotating it deliberately is supported.** Put the new key in `REAPER_SECRET_KEY` and the
retired one in `REAPER_SECRET_KEY_OLD` — comma-separated for a chain of them. Reaper then
encrypts under the new key while still decrypting whatever the old ones wrote, so nothing
is bricked halfway through; drop the old value once every credential has been re-saved.

Honest limitation: a key file next to the database it protects is no defense against
an attacker who already has your filesystem. It defends against the ordinary way
these leak — a database copied into a backup, an issue report, a support thread. For
real separation, supply the key from a secret manager.

Instances may be **seeded** from the environment for declarative deployments:

```bash
REAPER_SONARR_HD_URL=https://sonarr.example.net
REAPER_SONARR_HD_API_KEY=...
REAPER_SONARR_4K_URL=https://sonarr-4k.example.net
REAPER_SONARR_4K_API_KEY=...
```

Each one is imported and encrypted the first time Reaper sees it, and Reaper then logs
that the variable can be removed. The import runs on *every* start, not only the first,
so you can add a service to the environment later and it will be picked up on the next
restart. **The database is the source of truth regardless** — an instance that already
exists is never overwritten from the environment, so rotating a key in the UI cannot be
silently clobbered by a stale `.env`.

Plex is *not* configured here. It is added via **Sign in with Plex** in the setup
wizard, which discovers your server and its token for you.

### The deletion switch

`REAPER_DESTRUCTIVE_ACTIONS_ENABLED` seeds the **first run only**. On a fresh install,
with nothing stored yet, it decides whether Reaper comes up armed — so a declarative
deployment can ship ready to delete. After that the stored value wins and this variable
is ignored, so a key rotated in the UI can never be silently clobbered by a stale `.env`.

The live control is the web UI, under **Policy → Deletion**, and it is deliberately
asymmetric:

- **Turning deletion on** requires the admin password. A stray click or a stale tab
  cannot arm the tool: the password is what stands between a browser and an armed Reaper.
- **Turning deletion off** requires nothing. Making Reaper safer should never be gated.

One function assembles the answer (`services/app_settings.runtime_safety`), and every
client and health check is built from it, so there is no second place for the effective
permission to drift.

## Not getting locked out

If Plex OAuth were the only way in, then a plex.tv outage, a revoked token, or a
rebuilt Plex server (which changes the `machineIdentifier` the ownership check
depends on) would lock you out of your own tool. None of those are hypothetical.

So **Reaper always keeps at least one working local admin.** The last local admin
cannot be deactivated, and Plex OAuth is additive convenience, never the sole key.

Three escape hatches, each requiring host access — so none of them weakens the
web-facing security:

```bash
# 1. The CLI. Works even if the UI is completely broken. The full command set:
docker exec -it reaper reaper-admin list             # warns if lockout is possible
docker exec -it reaper reaper-admin reset-password --username you
docker exec -it reaper reaper-admin create-admin --username backup
docker exec -it reaper reaper-admin deactivate --username someone
docker exec -it reaper reaper-admin link-plex        # link Plex without the web wizard

# 2. Recovery mode. Set REAPER_RECOVERY=true, restart, then:
docker compose logs reaper | grep -A2 RECOVERY

# 3. The startup warning, if no local admin exists at all.
```

`reset-password` re-enables local login even on a Plex-only account — which is the
realistic recovery when Plex is exactly what has broken. It and `create-admin` take an
optional `--password`; omit it and a strong one is generated for you, which is the
recommended path.

Recovery mode prints a **URL and a separate code**, not a magic link: you open `/recover`
and paste the code there. It is single-use and expires in 15 minutes. The split is
deliberate — a code baked into the URL would be recorded verbatim in every reverse proxy's
access log, which is precisely where you do not want the key to your admin account.

## Development

Requires [uv](https://docs.astral.sh/uv/) and Python 3.13+.

```bash
uv venv && source .venv/bin/activate
uv pip install -e ".[dev]"

cp .env.example .env.local      # no key needed; one is generated on first boot

alembic upgrade head
uvicorn reaper.main:create_app --factory --reload --port 8420
```

```bash
ruff check . && ruff format --check .
mypy src/reaper
pytest
```

**Never commit credentials.** `.env.local` is gitignored. API keys entered in the web
UI are Fernet-encrypted at rest and redacted from logs.

### The web UI

```bash
cd frontend
npm install
npm run dev          # http://localhost:5173, proxies /api to the app on :8420
```

```bash
npm run lint && npm run format:check
npm run test
npm run build        # tsc --noEmit, then vite build
```

Both languages are machine-formatted and both are checked in CI: `ruff format` for Python,
prettier for `frontend/`, sharing one width (100). Run `npm run format` and `ruff format .`
before committing rather than matching style by hand. `.git-blame-ignore-revs` lists the
whitespace-only commits; `git config blame.ignoreRevsFile .git-blame-ignore-revs` once per
clone keeps `git blame` pointing at the commit that changed a line's meaning.

In production there is no separate server: `npm run build` emits `frontend/dist`, and
FastAPI serves it via `app.frontend()` as low-priority routes — the `/api` routes are
matched first, and anything else falls back to `index.html`. Both dev and production
therefore talk to a *same-origin* `/api`, so there is no CORS configuration anywhere
and nothing to accidentally loosen.

Eight dependencies, deliberately: React, React-DOM, TanStack Query, and the build
toolchain. No component library and no CSS framework. This is a tool that can delete a
media library, and every transitive package is something that ends up in the bundle
that renders the delete button.

### Time

Every timestamp is a **UTC instant stored as an integer unix epoch**
(`src/reaper/db/types.py`), presented to Python as a timezone-aware `datetime`.

This is not a micro-optimization — it removes a bug class rather than guarding
against one. SQLite stores no timezone, so `DateTime(timezone=True)` is silently a
no-op there: aware datetimes go in, naive ones come back, and a naive/aware
comparison is either a `TypeError` or — worse — quietly wrong by your UTC offset.
Since every deletion decision rests on *when was this last watched*, quietly wrong
is the failure that matters. An integer cannot carry that ambiguity. It is also the
format Tautulli and Plex already speak.

Reading the database by hand:

```sql
SELECT datetime(first_flagged_at, 'unixepoch') FROM first_flagged;
```

`reaper.clock` is the only sanctioned boundary. Note `from_epoch()` maps `0` and
`""` to `None`: Tautulli and Plex use them for *never played*, and coercing that to
1970 would read as *maximally stale* — condemning exactly the media that must not
be touched.

### Two things that cannot be changed later

The metadata naming convention (`src/reaper/db/base.py`) and Alembic's
`render_as_batch=True` (`alembic/env.py`). SQLite cannot drop an unnamed
constraint, so without both, future migrations fail and the only fix is rewriting
the entire migration history.

`tests/test_migrations.py` guards both halves. It imports the real `NAMING_CONVENTION` and
proves a named constraint can be dropped under batch mode, and it runs the real
`alembic/env.py` to capture what that file actually passes to `context.configure()`, at
both the offline and online call sites. Flipping `render_as_batch` to `False` fails that
test today, rather than surfacing years from now in the first migration that needs it.

## License

AGPL-3.0-or-later.
