# Packaged installs

How Reaper ships outside the container. The workflows do the work; this file is the
map, plus the pieces that need a human once.

## What ships, and from where

| Artifact | Built by | Published |
| --- | --- | --- |
| `Reaper-<v>-windows-x64-setup.exe` (Inno Setup) + portable `.zip` | `binaries.yml` | GitHub release assets |
| `Reaper-<v>-macos-arm64.dmg` (drag-to-Applications app) + portable `.zip` (macOS 14+, Apple Silicon) | `binaries.yml` | GitHub release assets |
| `scythe-labs-reaper_<v>_{amd64,arm64}.snap` | `binaries.yml` | GitHub release assets, Snap Store |
| `ghcr.io/…/reaper:<v>` and `:latest` (amd64 + arm64) | `release.yml` | GHCR |

Releases are CalVer (`vYYYY.M.N`), cut automatically by `release.yml` on every push to
`main`. Dev builds are the rolling `dev-build` prerelease, refreshed nightly by
`binaries.yml` whenever `dev` has moved; the `:dev` container tag keeps coming from
`ci.yml`. The nightly also builds the arm64 image (`:dev-arm64`) and folds it into
`:dev` as a multi-arch manifest; every later amd64 push re-stitches the manifest, so
`:dev` serves both architectures with amd64 fresh per push and arm64 fresh per night. Every artifact carries a `buildinfo.json` (version, commit, channel, home
repository) written by `scripts/write_buildinfo.py`; `reaper.launcher` exports it into
the environment at boot, and the in-app update check reads it to know what to compare
against.

Every binary and snap is booted once in CI against its own health probe before it may
ship. A packaging change can be proven on a pull request by adding the
`CI/Build-Binaries` label.

The macOS build runs on the `macos-14` runner **because the build image sets the
binary's minimum macOS**. Moving to a newer image silently raises that floor; don't,
without meaning to. No dependency currently forces anything newer than macOS 14.

## Building locally

```
(cd frontend && npm ci && npm run build)
uv sync --frozen --extra package
uv run python scripts/write_buildinfo.py --out packaging/pyinstaller/buildinfo.json \
    --commit "$(git rev-parse --short HEAD)" --repo scythe-labs/reaper
uv run pyinstaller packaging/pyinstaller/reaper.spec --noconfirm \
    --distpath build/pyinstaller/dist --workpath build/pyinstaller/work
./build/pyinstaller/dist/reaper/reaper
```

The snap builds with `snapcraft` on a Linux host (CI uses LXD on the runner).

## Running the desktop builds

**The tray icon is the app's presence.** While the server runs, a menu-bar icon
(macOS) / tray icon (Windows) offers Open Reaper and Quit; the launcher owns it, and
uvicorn serves from a worker thread because AppKit demands the main one. The .app is
a menu-bar app (`LSUIElement`), so there is no Dock icon unless `REAPER_DOCK_ICON=true`
puts it back; the Windows exe is windowed, so nothing else shows a double-click
worked. `REAPER_TRAY=false` turns the icon off — on macOS that leaves the app with
no visible presence at all, and quitting means Activity Monitor.

**Configuration.** A double-clicked app receives no environment variables, so the
launcher reads `launcher.conf` from the data folder (written as a commented template on
first run): `~/Library/Application Support/Reaper/` on macOS, `%LOCALAPPDATA%\Reaper` on
Windows. `REAPER_PORT`, `REAPER_HOST`, `REAPER_LAUNCH_BROWSER`, `REAPER_UPDATE_CHECK`,
`REAPER_TRAY`, and `REAPER_DOCK_ICON` belong there; real environment variables still
win.

**Reaching it from other machines.** The server binds all interfaces (the same
`0.0.0.0` default as the container); `127.0.0.1` is only the URL the local browser
opens. Other machines connect to `http://<host-ip>:8420`. First launch triggers the OS
firewall prompt (macOS "accept incoming connections", Windows Defender); until code
signing lands, macOS may re-ask on each update because the binary's identity changes.
The snap behaves the same: it binds all interfaces and carries `network-bind`.

**Starting at login.** The desktop builds register nothing: they run while open and
stop when quit. macOS: add Reaper.app under System Settings, General, Login Items.
Windows: put a shortcut in the Startup folder (`shell:startup`). The snap is the one
packaged install that IS a service: snapd starts it at boot and `snap stop/start
scythe-labs-reaper` controls it.

## One-time setup that needs a human

**Snap Store.** The registered name is `scythe-labs-reaper` (the bare name was not
available), so the yaml's `name:` and the store must always agree. Mint the CI
credential and store it as the `SNAPCRAFT_STORE_CREDENTIALS` secret:
`snapcraft export-login --snaps scythe-labs-reaper --channels edge,stable -`. Without
the secret the workflows build and attach the snap but skip the store, green.

**WinGet.** The first `ScytheLabs.Reaper` manifest must be submitted to
[microsoft/winget-pkgs](https://github.com/microsoft/winget-pkgs) by hand
(`wingetcreate new <installer-url>` walks through it). After that,
`submit-winget.yml` opens the version PR on every release, using the `WINGET_TOKEN`
secret (a PAT with `public_repo` scope). Without the secret it skips, green.

## Antivirus false positives, and what is wired

Unsigned Windows installers start with zero reputation, so some scanners flag them.
Three remedies, in order of effect:

1. **Code signing — not wired yet.** It needs a paid identity before any workflow can
   use it: for Windows an Azure Trusted Signing account (or an EV certificate) and a
   `signtool` step after the Inno build; for macOS a Developer ID certificate plus a
   `codesign`/`notarytool` step after PyInstaller (today the binary carries the
   ad-hoc signature arm64 requires, so macOS warns on first launch instead of
   refusing). When the accounts exist, both land in `binaries.yml` behind
   secrets-present guards.
2. **WinGet presence — wired.** Each release passes Microsoft's validation and
   scanning pipeline on its way into winget-pkgs.
3. **Per-release submission — manual.** When Defender specifically flags a release,
   submit the installer at
   <https://www.microsoft.com/en-us/wdsi/filesubmission> as a false positive;
   Microsoft typically clears verdicts within days, retroactively for everyone.
