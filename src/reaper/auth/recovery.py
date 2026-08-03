# SPDX-License-Identifier: AGPL-3.0-or-later
"""Recovery mode -- the last resort when you cannot log in at all.

Set ``REAPER_RECOVERY=true`` and restart. Reaper mints one single-use code, valid
for 15 minutes, and delivers it two ways, because no single one reaches every
install:

* **The console.** ``docker compose logs reaper | grep -A2 RECOVERY`` on the
  container, ``snap logs scythe-labs-reaper`` on the snap.
* **A file in the data folder**, ``recovery.txt``, beside ``launcher.conf``.

The file exists because the console does not. A windowed Windows build and a
Finder-launched macOS ``.app`` are handed no console at all, so PyInstaller leaves
``sys.stdout`` as ``None`` and ``packaging/pyinstaller/entry.py`` stands devnull in
for it: the banner below went nowhere an operator could look, and the in-app Logs
tab cannot substitute, because it sits behind the sign-in they have lost (#433).

Redeeming the code grants a normal admin session, bypassing both Plex OAuth and the
local password. That session is marked ``via_recovery`` (see
:func:`auth.sessions.open_session`), which is what lets Settings -> Security set a
new admin password without the current one -- an operator who has forgotten it has
nothing to type there, and forgetting it is why recovery was used. Then turn the
flag back off.

Why this is safe: obtaining the code requires setting an environment variable *and*
reading either the console or a 0600 file in the folder that already holds
``secret.key``. Anyone who can do both already has host access, and could simply open
the SQLite file and rewrite the password hash. Recovery mode adds no new capability
-- it only makes the legitimate path convenient instead of requiring surgery on the
database.

Why it is still bounded: single-use, 15 minutes, minted only at boot with the flag
set, and every redemption is written to the audit trail. The file is owner-only from
creation, replaced on every mint, deleted by ``api.auth.recover`` the moment the code
is redeemed, and swept at the next boot with the flag off (``main.lifespan``).

The token is delivered as a **code to paste**, not embedded in the link's query
string: the banner prints a bare ``/recover`` URL plus the code on its own line, and
the browser sends the code in the ``POST /recover`` body. Nothing carries the token in
a request line, so a fronting reverse proxy's access log never records it -- closing
the one residual exposure a ``GET /recover?token=...`` link would have left open.

The URL's host comes from :func:`recovery_base_url`, not from the bind address --
see there for why.
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

    The banner used to interpolate ``settings.host`` straight in, which prints
    ``http://0.0.0.0:8420/recover`` on a default install -- a bind address, not a place
    (B-12). A bind that names every interface is replaced by a placeholder the operator
    fills in; one that names a real address is kept, because then it IS the answer.
    """
    cleaned = (host or "").strip()
    shown = _HOST_PLACEHOLDER if cleaned in _UNROUTABLE_BINDS else cleaned
    return f"http://{shown}:{port}"


def recovery_file_path(data_dir: Path) -> Path:
    """Where the code is written for an operator with no console to read."""
    return data_dir / RECOVERY_FILE_NAME


def _write_owner_only(path: Path, text: str) -> None:
    """Create ``path`` owner-only from creation, replacing whatever was there.

    Written through a same-directory sibling opened ``O_EXCL`` at 0600 and moved into
    place: the bytes are never on disk under a wider mode for even an instant, which is
    what rule 14/83 forbids buying with a later ``chmod``. The move also means a crash
    mid-write leaves the previous file intact rather than a truncated one, so a half-written
    code can never be presented as the whole code.

    On Windows the mode bits are largely inert; the file inherits the ACL of the per-user
    data folder, which is the same protection ``secret.key`` beside it already relies on.
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

    Called on a successful redemption (``api.auth.recover``) and at every boot that does
    not mint one (``main.lifespan``), so a spent or stale code does not sit in the data
    folder after it has stopped being the way in.
    """
    try:
        recovery_file_path(data_dir).unlink(missing_ok=True)
    except OSError as exc:
        log.warning("recovery.file_not_removed", detail=str(exc))


def _write_recovery_file(
    data_dir: Path, *, recover_url: str, code: str, minutes: int
) -> Path | None:
    """Put the code where a desktop operator can read it. Returns the path, or ``None``.

    A failure here is never fatal: the console banner is still a live channel on the
    container and the snap. It is logged, without the code, so an operator who finds
    nothing in the data folder can see why.
    """
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
        "Once you are back in, set REAPER_RECOVERY=false and restart.\n"
    )
    try:
        data_dir.mkdir(parents=True, exist_ok=True)
        _write_owner_only(path, body)
    except OSError as exc:
        log.warning("recovery.file_not_written", path=str(path), detail=str(exc))
        return None
    return path


async def mint_recovery_token(session: AsyncSession, *, base_url: str, data_dir: Path) -> str:
    """Create a single-use recovery token, print it to the CONSOLE, and write it to a file.

    Not "to the log": the banner below goes out through ``print``, deliberately (see the
    comment on it), so it never reaches structlog, the in-app Logs tab, or the rotating
    files the Logs tab downloads. ``docker logs`` is where a container operator finds it,
    and the recovery screen's copy has to say so (U-11).

    The file (:func:`_write_recovery_file`) is the channel for the builds that have no
    console to print to at all -- see the module docstring. It carries the same code, is
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
    log.warning("recovery.redeemed", detail="A recovery link was used to gain admin access.")
    return True
