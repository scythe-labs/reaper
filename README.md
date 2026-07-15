# Reaper

Explainable media library pruning for Plex.

Reaper finds media nobody watches — things requested and never played, shows whose
old seasons no one returns to, low-rated files quietly eating disk — **explains why
it thinks each item is expendable**, and removes it safely through Sonarr and Radarr.

It integrates with **Tautulli** (watch history), **Sonarr** and **Radarr** (the only
components allowed to delete anything), **Seerr** (requests), and **Plex**.

> **Status: in development.** Reaper cannot currently delete anything.

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
- **A grace period you can cancel**, surfaced to your users as a
  *Leaving Soon* collection in Plex.

## Safety

Reaper deletes irreplaceable data from a server other people depend on.
**Every ambiguity resolves toward keeping the file.**

- **Off by default.** `REAPER_DESTRUCTIVE_ACTIONS_ENABLED=false` is enforced at the
  HTTP transport, not by scattered `if dry_run:` checks. While it is false, Reaper
  is *structurally incapable* of deleting anything — it can only scan, score and explain.
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

## Configuration

Three tiers, with a deliberate rule: **`.env` holds only what must exist *before*
the database, or be changeable *without* the web UI.** Everything else lives in the
database, encrypted, and is edited in the UI.

| Where | What | Why there |
|---|---|---|
| `.env` | `REAPER_SECRET_KEY` *(optional)* | It encrypts the database's secrets, so it cannot live inside the database |
| `.env` | data dir, host, port, logging | Needed before anything else exists |
| `.env` | `DESTRUCTIVE_ACTIONS_ENABLED`, `RECOVERY` | Must work when the UI is broken or untrusted |
| **DB** (encrypted) | Instance URLs + API keys, policies, schedules, whitelist | Editable and rotatable without a redeploy |

### The encryption key

You don't have to supply one. If `REAPER_SECRET_KEY` is unset, Reaper generates a
key on first boot and saves it to `<data_dir>/secret.key` (mode `0600`), reusing it
on every subsequent boot.

**Back that file up alongside your database.** It is the only thing that can decrypt
your stored API keys — lose it and every integration must be re-entered. The key is
generated *once*, never rotated automatically: a key that changed on restart would
silently render every stored credential unreadable, and you would only find out the
next time a scan tried to reach Sonarr.

Setting `REAPER_SECRET_KEY` explicitly always wins, and Reaper then writes no key
file at all — so a secret manager stays the single source. An existing generated key
file is never overwritten, since your database may still be encrypted with it.

Honest limitation: a key file next to the database it protects is no defence against
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

On first boot these are imported and encrypted, and Reaper logs that the variables
can be removed. **The database is the source of truth thereafter** — an instance that
already exists is never overwritten from the environment, so rotating a key in the UI
cannot be silently clobbered by a stale `.env`.

Plex is *not* configured here. It is added via **Sign in with Plex** in the setup
wizard, which discovers your server and its token for you.

### The kill switch is one-way

`REAPER_DESTRUCTIVE_ACTIONS_ENABLED` is a **ceiling**, not a default. The effective
permission is `env_enabled AND NOT emergency_stop`, so the web UI can only ever move
in the safe direction: the emergency-stop button can *disable* deletions, but nothing
reachable from a browser can *enable* them. Turning deletion on requires host access.

## Not getting locked out

If Plex OAuth were the only way in, then a plex.tv outage, a revoked token, or a
rebuilt Plex server (which changes the `machineIdentifier` the ownership check
depends on) would lock you out of your own tool. None of those are hypothetical.

So **Reaper always keeps at least one working local admin.** The last local admin
cannot be deactivated, and Plex OAuth is additive convenience, never the sole key.

Three escape hatches, each requiring host access — so none of them weakens the
web-facing security:

```bash
# 1. The CLI. Works even if the UI is completely broken.
docker exec -it reaper reaper-admin list             # warns if lockout is possible
docker exec -it reaper reaper-admin reset-password --username you
docker exec -it reaper reaper-admin create-admin --username backup

# 2. Recovery mode. Prints a single-use, 15-minute login link to the log.
#    Set REAPER_RECOVERY=true, restart, then:
docker compose logs reaper | grep -A2 RECOVERY

# 3. The startup warning, if no local admin exists at all.
```

`reset-password` re-enables local login even on a Plex-only account — which is the
realistic recovery when Plex is exactly what has broken.

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

In production there is no separate server: `npm run build` emits `frontend/dist`, and
FastAPI serves it via `app.frontend()` as low-priority routes — the `/api` routes are
matched first, and anything else falls back to `index.html`. Both dev and production
therefore talk to a *same-origin* `/api`, so there is no CORS configuration anywhere
and nothing to accidentally loosen.

Seven dependencies, deliberately: React, React-DOM, TanStack Query, and the build
toolchain. No component library and no CSS framework. This is a tool that can delete a
media library, and every transitive package is something that ends up in the bundle
that renders the delete button.

### Time

Every timestamp is a **UTC instant stored as an integer unix epoch**
(`src/reaper/db/types.py`), presented to Python as a timezone-aware `datetime`.

This is not a micro-optimisation — it removes a bug class rather than guarding
against one. SQLite stores no timezone, so `DateTime(timezone=True)` is silently a
no-op there: aware datetimes go in, naive ones come back, and a naive/aware
comparison is either a `TypeError` or — worse — quietly wrong by your UTC offset.
Since every deletion decision rests on *when was this last watched*, quietly wrong
is the failure that matters. An integer cannot carry that ambiguity. It is also the
format Tautulli and Plex already speak.

Reading the database by hand:

```sql
SELECT datetime(last_played, 'unixepoch') FROM media_item;
```

`reaper.clock` is the only sanctioned boundary. Note `from_epoch()` maps `0` and
`""` to `None`: Tautulli and Plex use them for *never played*, and coercing that to
1970 would read as *maximally stale* — condemning exactly the media that must not
be touched.

### Two things that cannot be changed later

The metadata naming convention (`src/reaper/db/base.py`) and Alembic's
`render_as_batch=True` (`alembic/env.py`). SQLite cannot drop an unnamed
constraint, so without both, future migrations fail and the only fix is rewriting
the entire migration history. `tests/test_migrations.py` guards this.

## Licence

AGPL-3.0-or-later.
