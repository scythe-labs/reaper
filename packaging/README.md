# Packaged installs

How Reaper ships outside the container. The workflows do the work; this file is the
map, plus the pieces that need a human once.

## What ships, and from where

| Artifact | Built by | Published |
| --- | --- | --- |
| `Reaper-<v>-windows-x64-setup.exe` (Inno Setup) + portable `.zip` | `binaries.yml` | GitHub release assets |
| `Reaper-<v>-macos-arm64.zip` (macOS 14+, Apple Silicon) | `binaries.yml` | GitHub release assets |
| `scythe-labs-reaper_<v>_{amd64,arm64}.snap` | `binaries.yml` | GitHub release assets, Snap Store |
| `ghcr.io/…/reaper:<v>` and `:latest` | `release.yml` | GHCR |

Releases are CalVer (`vYYYY.M.N`), cut automatically by `release.yml` on every push to
`main`. Dev builds are the rolling `dev-build` prerelease, refreshed nightly by
`binaries.yml` whenever `dev` has moved; the `:dev` container tag keeps coming from
`ci.yml`. Every artifact carries a `buildinfo.json` (version, commit, channel, home
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
