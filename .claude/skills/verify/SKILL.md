---
name: verify
description: Boot Reaper locally against a real data dir and drive the UI headlessly to observe a change end-to-end.
---

# Verifying a Reaper change end-to-end

Runtime observation, not tests. Boot the app against a `data/` dir (the dev DB is
disposable), drive the real UI, capture what you see.

## Quick boot

`scripts/dev-local.sh` does the whole Boot section below in one step: migrates, starts both
auto-reloading servers against the shared real `data/` (derived from git), waits for health,
prints the URLs. `scripts/dev-local.sh down | status | logs` manage it. Both servers
auto-update — backend edits reload the API, frontend edits hot-swap — so you rarely restart.
Use it, then skip to "Log in" and "Drive headlessly". The manual Boot below is the fallback
when you need a non-default data dir or want to watch startup by hand.

**Beside a running instance, pass BOTH ports** — they move together, because Vite's `/api`
proxy target reads `REAPER_PORT` (`frontend/vite.config.ts`), so moving only the web port
leaves the second UI talking to the first instance's API:

```
REAPER_PORT=8421 REAPER_WEB_PORT=5174 scripts/dev-local.sh up
```

Those same two ports name its logs and scope `down` / `status` / `logs`, so pass them there
too or those commands answer for the default instance. A successful `up` prints the spelling.

## Boot

Backup/real data goes in `data/` (needs `reaper.db`, `cache.db`, **`secret.key` + `secret.salt`**
together, or stored secrets won't decrypt). Confirm schema first:

```
uv run python -m reaper.preflight   # applies a staged restore; run BEFORE migrations
uv run alembic current              # must equal `uv run alembic heads`
```

Preflight is what applies a restore staged in the UI. A boot that skips it does not fail, it
just never finishes the restore, and the banner keeps asking for a restart (#381).

Start both servers (background), wait for readiness in their logs:

```
REAPER_SERVE_SPA=false uv run uvicorn reaper.main:create_app --factory --no-proxy-headers --port 8420   # "Application startup complete"
npm --prefix frontend run dev                                                        # "ready in" / localhost:5173
```

Vite proxies `/api` → 8420, and refuses to start if 5173 is taken rather than sliding to
another port (`strictPort`), so a second UI can never end up on the first instance's API.
`.env`/`.env.local` auto-load (seed vars); seeding is idempotent by `(kind, name)`, so it
won't duplicate configured instances.

**Both files resolve against the cwd**, so a manual boot from a worktree reads neither and
falls back to `data/secret.key` — a different key than the one that `data/` was encrypted
under. If stored credentials won't decrypt, do NOT re-enter them: that overwrites the good
ciphertext under the wrong key. Boot with `scripts/dev-local.sh`, which loads the dotenv
beside the data dir it serves.

## Log in (no need to touch a real account)

Mint a throwaway local admin; it prints a generated password once:

```
uv run reaper-admin create-admin --username local-test
```

## Drive headlessly

Two ways in, and **which one is available is a property of the box, so check before writing the
script.** The claude-in-chrome extension needs a real Chrome with a logged-in profile, so on a
headless host it is not merely unavailable, it cannot exist; Playwright is then the only route
rather than the fallback.

**Against a system Chrome** (a desktop machine that already has one), installed in a scratch dir
so the repo stays clean:

```
PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1 npm install playwright-core   # in a scratch dir
```

Then `chromium.launch({ channel: "chrome", headless: true })`.

**Against Playwright's own chromium** (a headless VM, or any box with no Google Chrome). Drop the
channel — `chromium.launch({ headless: true })` — because `channel: "chrome"` names the *Google
Chrome* binary and fails outright where only Playwright's build exists, which is the ordinary
shape of a fresh Linux host. Install `playwright` rather than `playwright-core` there, or bring
your own browsers with `npx playwright install chromium`.

Two things a minimal Linux host will not have, both of which fail in ways that read as a bug in
the app:

- `npx playwright install-deps chromium` — the shared libraries (`libnss3`, `libatk-bridge2.0`,
  `libgbm1` and the rest). Without them the launch itself dies.
- Fonts. A netinst Debian ships next to none, so every string screenshots as an empty box and the
  capture is worthless for the one thing it is for. `fonts-liberation` plus
  `fonts-noto-color-emoji` covers the UI.

**WebKit is a lane, not an extra.** Chromium answers "does it work"; it cannot answer "does it
work on the operator's phone", and this UI has already shipped a defect no Chromium run could
see — rule 46's shrinking button left a red ghost in the region it vacated, in Safari alone.
WebKit is the only engine outside a Mac that renders what an iPhone or a Safari desktop will, so
a change to the queue's action grammar, an anchored popover (rule 138), or anything laid out with
`dvh` gets driven there too: `npx playwright install webkit`, `install-deps webkit`, then
`webkit.launch({ headless: true })` and the same script. Reach for
`devices["iPhone 15"]` as the context when the change is about a phone, since viewport and touch
are half of what those rules are about.

No display server is needed for any of them: Playwright's headless mode does not use one, so Xvfb is
not part of this.

Login = click `.auth-alt`, fill
`input[autocomplete="username"]` + `input[type="password"]`, submit `.local-form button[type="submit"]`,
wait for `nav.views button`. Start a scan with `fetch("/api/scan/start", {method:"POST",
headers:{"X-Reaper-CSRF":"1"}, body:"{}"})`, then `page.reload()` so the shell picks up
`running:true`. To force a determinate UI state, `page.route("**/api/scan/status", ...)`.

## Gotchas

- A scan is **read-only** but really reaches the configured Plex/*arr/Tautulli hosts — expect
  real reads if the box can reach them. Never drive `POST /api/runs/{id}/execute`.
- Screenshot on **Policy** or **Settings** (config, no library titles) to keep identifying
  content out of captures — the golden rule applies to screenshots too.
