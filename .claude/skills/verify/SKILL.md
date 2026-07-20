---
name: verify
description: Boot Reaper locally against a real data dir and drive the UI headlessly to observe a change end-to-end.
---

# Verifying a Reaper change end-to-end

Runtime observation, not tests. Boot the app against a `data/` dir (the dev DB is
disposable), drive the real UI, capture what you see.

## Boot

Backup/real data goes in `data/` (needs `reaper.db`, `cache.db`, **`secret.key` + `secret.salt`**
together, or stored secrets won't decrypt). Confirm schema first:

```
uv run alembic current   # must equal `uv run alembic heads`
```

Start both servers (background), wait for readiness in their logs:

```
REAPER_SERVE_SPA=false uv run uvicorn reaper.main:create_app --factory --port 8420   # "Application startup complete"
npm --prefix frontend run dev                                                        # "ready in" / localhost:5173
```

Vite proxies `/api` → 8420. `.env`/`.env.local` auto-load (seed vars); seeding is
idempotent by `(kind, name)`, so it won't duplicate configured instances.

## Log in (no need to touch a real account)

Mint a throwaway local admin; it prints a generated password once:

```
uv run reaper-admin create-admin --username local-test
```

## Drive headlessly (system Chrome, no download)

The claude-in-chrome extension may be unavailable (OAuth account mismatch). Fall back to
`playwright-core` against system Chrome, installed in a scratch dir so the repo stays clean:

```
PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1 npm install playwright-core   # in a scratch dir
```

`chromium.launch({ channel: "chrome", headless: true })`. Login = click `.auth-alt`, fill
`input[autocomplete="username"]` + `input[type="password"]`, submit `.local-form button[type="submit"]`,
wait for `nav.views button`. Start a scan with `fetch("/api/scan/start", {method:"POST",
headers:{"X-Reaper-CSRF":"1"}, body:"{}"})`, then `page.reload()` so the shell picks up
`running:true`. To force a determinate UI state, `page.route("**/api/scan/status", ...)`.

## Gotchas

- A scan is **read-only** but really reaches the configured Plex/*arr/Tautulli hosts — expect
  real reads if the box can reach them. Never drive `POST /api/runs/{id}/execute`.
- Screenshot on **Policy** or **Settings** (config, no library titles) to keep identifying
  content out of captures — the golden rule applies to screenshots too.
