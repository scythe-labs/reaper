# Security policy

Reaper holds credentials for your media server and it deletes files. A vulnerability here
costs someone their library, so security reports are welcome and are treated as the most
important thing in the tracker.

## Reporting a vulnerability

Use GitHub's [private vulnerability reporting form](https://github.com/scythe-labs/reaper/security/advisories/new).
It opens a discussion visible only to you and the maintainer, and it lets a fix be prepared
before anything is public.

Please avoid opening a public issue for a security problem until there is a release that
fixes it.

A useful report says what an attacker can reach, what they need in order to reach it, and
what happens once they do. Proof-of-concept code is welcome and never required.

### What to expect

Reaper is a hobby project maintained by one person, so response times are best effort.
In practice that means an acknowledgement within a few days and
an honest estimate once the problem is understood. If a report sits without a reply for a
week, a nudge on the same advisory thread is entirely reasonable.

Credit is given in the release notes unless you would rather it was not.

## Supported versions

Fixes land on `dev` and reach operators in the next release. The supported versions are
the latest release and the current `dev` image. Nothing is backported, so running an older
tag means running without the fixes that followed it.

## Where the sensitive parts are

If you are looking for somewhere to start, these carry the weight:

- **Credentials at rest.** API keys for Plex, Tautulli, Sonarr, and Radarr are encrypted
  with a Fernet key that lives outside the database, and they are redacted from logs
  (`src/reaper/secrets.py`, `src/reaper/logbuffer.py`).
- **Authentication and sessions.** Cookie sessions, the login flow, and the recovery paths
  that exist so a locked-out operator can get back in (`src/reaper/auth/`).
- **The API key lane.** A request carrying `X-Api-Key` is judged on the key alone, with no
  session behind it. The key is fenced to reads outside the backup, the logs, the key
  itself and the per-person viewing routes, and to writes that scan, plan, or edit the
  policy (`src/reaper/api/middleware.py`).
- **Reverse-proxy trust.** `X-Forwarded-For` and `X-Forwarded-Proto` are honored only when
  the direct peer is a proxy the operator listed. They decide the rate-limit key and the
  session cookie's `Secure` flag (`src/reaper/auth/proxy.py`).
- **The deletion path.** Deletion is armed from the interface behind a password, and the
  one route that deletes requires both the armed host and an exact confirmation phrase that
  is recomputed on the server from the plan's contents.
- **The transport guard.** `GuardedTransport` refuses any mutating request unless deletion
  is armed on the host and the intent was written to the journal first. Arming is a
  property of the machine itself, which a browser has no way to reach
  (`src/reaper/clients/`).
- **Backup and restore.** A backup carries key material, and restore writes to the settings
  that control everything above (`src/reaper/services/backup.py`,
  `src/reaper/services/restore.py`).

## Especially interested in

Anything that lets a request cause a deletion without passing both independent layers: the
executor's interlocks and the transport guard. Those two are meant to be independent, so a
single flaw that defeats both is the highest-severity result this project has.

## Out of scope

- Reports that amount to "an operator with the admin password can delete media." That is
  the purpose of the program.
- Findings from an automated scanner with no described impact.
- Vulnerabilities in Plex, Sonarr, Radarr, or Tautulli themselves. Please report those
  upstream. If Reaper's handling of one makes it worse, that part is in scope here.
