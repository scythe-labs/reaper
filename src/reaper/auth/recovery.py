# SPDX-License-Identifier: AGPL-3.0-or-later
"""Recovery mode: the last resort when you cannot sign in at all.

Set ``REAPER_RECOVERY=true`` and restart. Reaper mints one single-use code, valid for
15 minutes, and delivers it two ways, because no single channel reaches every install:

* **The console.** ``docker compose logs reaper | grep -A2 RECOVERY`` on the
  container, ``snap logs scythe-labs-reaper`` on the snap.
* **A file in the data folder**, ``recovery.txt``, beside ``launcher.conf``.

The file exists because the console does not always exist. A windowed Windows build
and a Finder-launched macOS ``.app`` get no console at all: PyInstaller leaves
``sys.stdout`` as ``None`` and ``packaging/pyinstaller/entry.py`` stands ``devnull`` in
for it, so the console banner below would go nowhere an operator could read it. The
in-app Logs tab cannot substitute either, because it sits behind the sign-in the
operator has lost.

Redeeming the code opens a normal admin session, bypassing both Plex OAuth and the
local password. That session is marked ``via_recovery`` (see
:func:`auth.sessions.open_session`), which is what lets Settings -> Security set a new
admin password without the current one: an operator who has forgotten the password has
nothing to type there, and forgetting it is why recovery was used. The flag then turns
back off.

Recovery adds no new capability. Getting the code requires setting an environment
variable and then reading either the console or a 0600 file in the folder that already
holds ``secret.key``. Anyone who can do both already has host access, and could just
open the SQLite file and rewrite the password hash directly. Recovery only makes the
legitimate path convenient instead of requiring surgery on the database.

What keeps it bounded: the code is single-use, expires in 15 minutes, is minted only
at boot with the flag set, and every redemption is written to the audit trail. The file
is owner-only from creation, replaced on every mint, deleted by ``api.auth.recover``
the moment the code is redeemed, and swept at the next boot with the flag off
(``main.lifespan``).

The token is delivered as a code to paste, not embedded in the link's query string:
the banner prints a bare ``/recover`` URL plus the code on its own line, and the
browser sends the code in the ``POST /recover`` body. Nothing carries the token in a
request line, so a fronting reverse proxy's access log never records it. A
``GET /recover?token=...`` link would have left that exposure open.

The URL's host comes from :func:`recovery_base_url`, not from the bind address; see
there for why.
"""

from __future__ import annotations

import os
from pathlib import Path

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from reaper.auth.tokens import RECOVERY_TTL, generate_token, hash_token
from reaper.clock import expiry, utcnow
from reaper.db.models import RecoveryToken

log = structlog.get_logger(__name__)

#: The code's second delivery channel, in the data folder beside ``launcher.conf``. One
#: name on every install shape, so the manual gives one instruction rather than five.
RECOVERY_FILE_NAME = "recovery.txt"

#: Bind addresses that are not somewhere a browser can go. ``0.0.0.0`` (the default) and
#: ``::`` mean "every interface" to the socket layer and nothing at all to a person.
_UNROUTABLE_BINDS = frozenset({"", "0.0.0.0", "::", "[::]", "*"})  # noqa: S104

#: Stands in for the bind address when it is one of the above. An operator in a lockout
#: knows their own address; what they need from the banner is the port and the path.
_HOST_PLACEHOLDER = "<your-reaper-address>"


def recovery_base_url(host: str, port: int) -> str:
    """Where to tell the operator to open the recovery page.

    ``settings.host`` is a bind address, not necessarily a place a browser can reach:
    interpolating it directly would print ``http://0.0.0.0:8420/recover`` on a default
    install. A bind that names every interface is replaced by a placeholder the operator
    fills in themselves; one that names a real address is kept, because then it is the
    answer.
    """
    cleaned = (host or "").strip()
    shown = _HOST_PLACEHOLDER if cleaned in _UNROUTABLE_BINDS else cleaned
    return f"http://{shown}:{port}"


def recovery_file_path(data_dir: Path) -> Path:
    """Where the code is written for an operator with no console to read."""
    return data_dir / RECOVERY_FILE_NAME


def _write_owner_only(path: Path, text: str) -> None:
    """Create ``path`` owner-only from creation, replacing whatever was there.

    Writes through a same-directory sibling opened with ``O_EXCL`` at 0600 and moves it
    into place, so the bytes are never on disk under a wider mode, not even for an
    instant. A later ``chmod`` cannot buy that guarantee, since the file would already
    have existed at the wider mode before it ran. The move also means a crash mid-write
    leaves the previous file intact rather than a truncated one, so a half-written code
    can never be shown as the whole code.

    On Windows the mode bits are largely inert. The file inherits the ACL of the
    per-user data folder, the same protection ``secret.key`` beside it already relies on.
    """
    tmp = path.with_name(path.name + ".tmp")
    tmp.unlink(missing_ok=True)
    fd = os.open(tmp, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    tmp.replace(path)


def clear_recovery_file(data_dir: Path) -> None:
    """Delete the recovery file if one is there. Never fatal, never noisy.

    Called on a successful redemption (``api.auth.recover``), at every boot
    (``main.lifespan``), and by :func:`_write_recovery_file` before it writes, so a spent
    or stale code never sits in the data folder after it stops being a way in.

    The half-written sibling is deleted too. A process killed between ``os.open`` and the
    rename leaves a ``.tmp`` file holding a live code that neither the redemption sweep
    nor the boot sweep would otherwise look at, and a file nothing ever deletes is
    exactly what this channel must not leave behind.
    """
    path = recovery_file_path(data_dir)
    for target in (path, path.with_name(path.name + ".tmp")):
        try:
            target.unlink(missing_ok=True)
        except OSError as exc:
            log.warning("recovery.file_not_removed", detail=str(exc))


def _write_recovery_file(
    data_dir: Path, *, recover_url: str, code: str, minutes: int
) -> Path | None:
    """Put the code where a desktop operator can read it. Returns the path, or ``None``.

    A failure here is never fatal: the console banner is still a live channel on the
    container and the snap, and the token is already in the database.

    The old file is deleted before the new one is written, so a failure leaves nothing
    rather than the previous code. Minting has already invalidated that old code by the
    time this runs, so leaving it in place would hand the operator a file that reads
    exactly like a working one but cannot sign them in. On the builds where this file is
    the only channel, and where the warning below reaches only the in-app Logs tab they
    cannot get to, an empty folder at least matches what the manual says to expect.
    """
    clear_recovery_file(data_dir)
    path = recovery_file_path(data_dir)
    body = (
        "Reaper recovery code\n"
        "\n"
        f"Open:  {recover_url}\n"
        f"Code:  {code}\n"
        "\n"
        f"Paste the code on that page. It works once and expires {minutes} minutes after\n"
        "Reaper started. Reaper deletes this file the moment the code is used.\n"
        "\n"
        "Signing in with it lets you set a new password without the old one, in\n"
        "Settings, Security. Then set REAPER_RECOVERY=false and restart.\n"
    )
    try:
        data_dir.mkdir(parents=True, exist_ok=True)
        _write_owner_only(path, body)
    except OSError as exc:
        log.warning("recovery.file_not_written", path=str(path), detail=str(exc))
        return None
    return path


async def mint_recovery_token(session: AsyncSession, *, base_url: str, data_dir: Path) -> str:
    """Create a single-use recovery token, print it to the console, and write it to a file.

    This goes to the console, not the log: the banner below is written through
    ``print``, deliberately (see the comment on it), so it never reaches structlog, the
    in-app Logs tab, or the rotating files the Logs tab downloads. ``docker logs`` is
    where a container operator finds it, and the recovery screen's copy has to say so.

    The file (:func:`_write_recovery_file`) is the channel for builds that have no
    console to print to at all; see the module docstring. It carries the same code, is
    owner-only from creation, and is removed on redemption by ``api.auth.recover``.
    """
    # Invalidate any earlier unredeemed tokens: only one may be live at a time.
    stale = (
        (await session.execute(select(RecoveryToken).where(RecoveryToken.used_at.is_(None))))
        .scalars()
        .all()
    )
    now = utcnow()
    for token in stale:
        token.used_at = now

    plaintext = generate_token()
    session.add(
        RecoveryToken(
            token_hash=hash_token(plaintext),
            created_at=now,
            expires_at=expiry(RECOVERY_TTL),
        )
    )
    await session.flush()

    recover_url = f"{base_url.rstrip('/')}/recover"
    minutes = int(RECOVERY_TTL.total_seconds() // 60)
    written = _write_recovery_file(
        data_dir, recover_url=recover_url, code=plaintext, minutes=minutes
    )

    # Deliberately not a structlog event: the token must not be shipped to a log
    # aggregator as a structured, searchable field. It goes to the console only.
    # The code is printed on its own line rather than baked into the URL, so it is
    # never carried in a request line that a reverse proxy would log.
    also = f"  Also in: {written}\n" if written is not None else ""
    banner = (
        "\n"
        "  ============================ RECOVERY ============================\n"
        f"  Open:  {recover_url}\n"
        f"  Code:  {plaintext}\n"
        f"{also}"
        f"  Paste the code on that page. Single use, expires in {minutes} minutes.\n"
        "  The code is not in the URL, so a reverse proxy's access log never sees it.\n"
        "  Signing in with it lets you set a new password without the old one.\n"
        "  Set REAPER_RECOVERY=false and restart once you are back in.\n"
        "  =================================================================\n"
    )
    print(banner)  # noqa: T201 -- console-only by design; see above
    return plaintext


async def redeem_recovery_token(session: AsyncSession, plaintext: str) -> bool:
    """Consume a recovery token. False if unknown, expired, or already used."""
    token = await session.scalar(
        select(RecoveryToken).where(RecoveryToken.token_hash == hash_token(plaintext))
    )
    if token is None:
        log.warning("recovery.rejected", reason="unknown token")
        return False
    if token.used_at is not None:
        log.warning("recovery.rejected", reason="token already used")
        return False
    if token.expires_at <= utcnow():
        log.warning("recovery.rejected", reason="token expired")
        return False

    token.used_at = utcnow()
    await session.flush()
    # No audit line here. flush() stamps used_at inside the caller's open transaction, and
    # the no-admin 409 path rolls that back on purpose to leave the code usable. The "was
    # used to gain admin access" line records an outcome, so it fires after the caller's
    # commit, where the redemption is durable and a session is actually open.
    return True
