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

The Unraid route is the container: Community Applications carries the template (the
submitted repository is this one, so the listing reads `contrib/unraid/my-Reaper.xml`),
and it points at the GHCR image above. **One entry covers both channels.** Installing opens
a picker: a Default row that pulls `<Repository>` as written (`:latest`), then one row per
`<Branch>`, showing its `<Tag>` beside its `<TagDescription>`. The selected tag replaces the
one on `<Repository>`, so every channel gets a `<Branch>`, including the one the repository
already names: CA expands that branch like any other, and it is the only row that can carry
a description of the release channel. `test_the_unraid_template_offers_every_channel_it_declares`
holds that, and holds the invented `<DefaultTagDescription>` out (docs/LEARNINGS.md).

The picker reaches an operator through the app feed, which is rebuilt on Community
Applications' own schedule, so a template change shows up hours after it lands on `dev`, not
on merge.

Releases are CalVer (`vYYYY.M.N`), cut automatically by `release.yml` on every push to
`main`, once ci.yml's gate reports green on that sha. Dev builds are the rolling `dev-build` prerelease, refreshed nightly by
`binaries.yml` whenever `dev` has moved; the `:dev` container tag keeps coming from
`ci.yml`. The nightly also builds the arm64 image (`:dev-arm64`) and folds it into
`:dev` as a multi-arch manifest; every later amd64 push re-stitches the manifest, so
`:dev` serves both architectures with amd64 fresh per push and arm64 fresh per night. Every artifact carries a `buildinfo.json` (version, commit, channel, home
repository) written by `scripts/write_buildinfo.py`; `reaper.launcher` exports it into
the environment at boot, and the in-app update check reads it to know what to compare
against.

Every binary and snap is booted once in CI against its own health probe before it may
ship. PRs and dev pushes build only the amd64 docker gate in `ci.yml`; to prove a
packaging change before merge, run the workflow against the branch by hand:
`gh workflow run binaries.yml --ref <branch>` (artifacts land on the run, and the
dev-build prerelease is only refreshed when `publish` is set).

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

A binary built from a feature branch shows "Newer dev build" from its first boot: the
dev channel's update check compares the baked commit against the tip of `dev`, and a
branch commit is never that tip. It clears on a build cut from `dev` itself.

## Running the desktop builds

**The tray icon is the app's presence.** While the server runs, a menu-bar icon
(macOS) / tray icon (Windows) offers Open Reaper and Quit; the launcher owns it, and
uvicorn serves from a worker thread because AppKit demands the main one. The .app is
a menu-bar app (`LSUIElement`), so there is no Dock icon unless `REAPER_DOCK_ICON=true`
puts it back; the Windows exe is windowed, so nothing else shows a double-click
worked. `REAPER_TRAY=false` turns the icon off — on macOS that leaves the app with
no visible presence at all, and quitting means Activity Monitor.

**Configuration.** An install nobody can hand an environment variable to reads
`launcher.conf` from the data folder (written as a commented template on first run):
`~/Library/Application Support/Reaper/` on macOS, `%LOCALAPPDATA%\Reaper` on Windows,
`/var/snap/scythe-labs-reaper/common` on the snap. The snap qualifies for its own reason:
snapd starts it at boot and `snapcraft.yaml` declares no configure hook, so `snap set`
reaches nothing. `launcher.reads_launcher_conf` is the one place that decides, and the
container is deliberately outside it. Any `REAPER_`-prefixed key is honored; the template
offers `REAPER_PORT`, `REAPER_HOST`, `REAPER_LAUNCH_BROWSER`, `REAPER_UPDATE_CHECK`,
`REAPER_TRAY`, `REAPER_DOCK_ICON`, and `REAPER_RECOVERY`. Real environment variables still
win. The desktop builds also show a Desktop app group in Settings, General, which edits the
tray and Dock icon keys in this file and applies them at the next start.

**Getting back in.** `REAPER_RECOVERY=true` mints a single-use sign-in code at the next
start and writes it to `recovery.txt` beside the conf, because these builds have no console
for the banner to print to. `reaper-admin` is not in them at all, so recovery mode is the
whole route, and the session it opens may set a new admin password without the old one.
Deletion is held off while the flag is set. Operator steps: `manual/operating/locked-out.mdx`.

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

**VirusTotal.** Mint a key at <https://www.virustotal.com/gui/my-apikey> and store it
as the `VT_API_KEY` secret. `virustotal.yml` then scans every published asset on every
release. The free tier covers a release: it allows four requests a minute, and the
workflow sends two assets a minute because an asset over 32 MB costs two requests.
Without the secret it skips, green.

## Antivirus false positives, and what is wired

Unsigned Windows installers start with zero reputation, so some scanners flag them.
Four remedies, in order of effect:

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
4. **VirusTotal links in the release body — wired.** One analysis link per published
   asset, appended to the release notes, so an operator who hits a warning reads every
   engine's verdict on the exact file they downloaded instead of taking one vendor's
   word. It clears no flag; it is the evidence a flag gets confirmed or dismissed
   against, and it is also how *we* find out a release is being flagged at all.
