# SPDX-License-Identifier: AGPL-3.0-or-later
"""``reaper-admin`` -- the out-of-band escape hatch.

Run it against a live container:

    docker exec -it reaper reaper-admin list
    docker exec -it reaper reaper-admin reset-password --username admin
    docker exec -it reaper reaper-admin create-admin --username backup-admin

Using it requires the ability to exec into the container -- that is, host access.
So it grants nothing an attacker with the host could not already do by opening
the SQLite file, while giving *you* a guaranteed way back in when Plex OAuth
fails or a password is forgotten.

This module deliberately prints to stdout: it is a terminal tool, and its output
(a freshly generated password) must never go to the structured log, which is
persisted and shipped elsewhere.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections.abc import Sequence

from sqlalchemy import select

from reaper.auth.admins import (
    LastAdminError,
    count_local_admins,
    create_local_admin,
    deactivate,
    set_password,
)
from reaper.config import RuntimeSafety, get_settings
from reaper.crypto import SecretBox
from reaper.db.models import AppUser
from reaper.db.session import create_engine, create_session_factory
from reaper.secrets import resolve_kdf_salt, resolve_old_keys, resolve_secret_key
from reaper.services import plex_link
from reaper.services.plex_link import PlexLinkError, PlexServerChoiceNeededError


def _out(message: str = "") -> None:
    """Print, and **flush**.

    The flush is not decoration. ``link-plex`` prints a sign-in URL and then blocks for
    up to several minutes waiting for the owner to use it. Python line-buffers stdout
    only when it is a TTY, so piped or captured -- which is exactly how you run it under
    `docker exec ... | tee`, or from any supervisor -- the URL would sit unseen in the
    buffer while the command waited for a sign-in that could never happen.
    """
    sys.stdout.write(message + "\n")
    sys.stdout.flush()


def _banner(password: str, username: str) -> None:
    _out()
    _out("  " + "-" * 58)
    _out(f"  Username:  {username}")
    _out(f"  Password:  {password}")
    _out("  " + "-" * 58)
    _out("  Shown once, and never written to the log. Save it now.")
    _out()


async def _cmd_list() -> int:
    engine = create_engine(get_settings())
    factory = create_session_factory(engine)
    try:
        async with factory() as session:
            users = (await session.execute(select(AppUser).order_by(AppUser.id))).scalars().all()
            if not users:
                _out(
                    "No admin accounts exist yet. Reaper will run its setup wizard on first visit."
                )
                return 0
            _out(f"{'ID':<4} {'USERNAME':<24} {'PROVIDER':<10} {'ACTIVE':<7} LAST LOGIN")
            for u in users:
                last = u.last_login_at.isoformat(timespec="minutes") if u.last_login_at else "never"
                _out(f"{u.id:<4} {u.username:<24} {u.provider:<10} {u.is_active!s:<7} {last}")

            locals_ = await count_local_admins(session)
            _out()
            _out(f"Local admins able to log in without Plex: {locals_}")
            if locals_ == 0:
                _out(
                    "WARNING: none. If Plex OAuth fails you will be locked out.\n"
                    "         Run: reaper-admin create-admin --username <name>"
                )
        return 0
    finally:
        await engine.dispose()


async def _cmd_create(username: str, password: str | None) -> int:
    engine = create_engine(get_settings())
    factory = create_session_factory(engine)
    try:
        async with factory() as session:
            try:
                _, plaintext = await create_local_admin(session, username, password)
            except ValueError as exc:
                _out(f"error: {exc}")
                return 1
            await session.commit()
        _banner(plaintext, username)
        return 0
    finally:
        await engine.dispose()


async def _cmd_reset(username: str, password: str | None) -> int:
    engine = create_engine(get_settings())
    factory = create_session_factory(engine)
    try:
        async with factory() as session:
            try:
                plaintext = await set_password(session, username, password)
            except ValueError as exc:
                _out(f"error: {exc}")
                return 1
            await session.commit()
        _banner(plaintext, username)
        return 0
    finally:
        await engine.dispose()


async def _cmd_deactivate(username: str) -> int:
    engine = create_engine(get_settings())
    factory = create_session_factory(engine)
    try:
        async with factory() as session:
            try:
                await deactivate(session, username)
            except (ValueError, LastAdminError) as exc:
                _out(f"error: {exc}")
                return 1
            await session.commit()
        _out(f"Deactivated {username!r}.")
        return 0
    finally:
        await engine.dispose()


async def _cmd_link_plex(server: str | None) -> int:
    """Link the Plex server by signing in on plex.tv.

    Headless-friendly: it prints a URL, you open it anywhere, and the *backend* polls
    for the token. Nothing is ever pasted, and the browser never handles a credential.

    ``server`` picks one server by exact name or machine identifier when the account
    owns several; without it, a multi-server account gets the list and instructions.
    """
    settings = get_settings()
    engine = create_engine(settings)
    factory = create_session_factory(engine)
    try:
        box = SecretBox(
            resolve_secret_key(settings),
            *resolve_old_keys(settings),
            salt=resolve_kdf_salt(settings),
        )
        safety = RuntimeSafety(destructive_enabled=settings.destructive_actions_enabled)

        def prompt(url: str) -> None:
            _out()
            _out("  Open this, and sign in to Plex:")
            _out()
            _out(f"  {url}")
            _out()
            _out("  Waiting...")

        try:
            linked = await plex_link.link(
                factory, box, safety=safety, on_prompt=prompt, choice=server
            )
        except PlexServerChoiceNeededError as exc:
            _out()
            _out("  This account owns more than one Plex server:")
            _out()
            for candidate in exc.candidates:
                _out(f"    {candidate.name}  ({candidate.machine_identifier})")
            _out()
            _out("  Run this again with --server and one of those names or identifiers,")
            _out("  so Reaper is pointed at exactly the library you mean.")
            return 1
        except PlexLinkError as exc:
            _out(f"error: {exc}")
            return 1

        _out()
        _out(f"  Linked {linked.name!r}.")
        _out(
            f"  Reaching it {'locally' if linked.local else 'remotely'}"
            f"{' via the Plex relay' if linked.relay else ''}."
        )
        _out()
        _out("  NOTE: the token Reaper just stored is your Plex ACCOUNT token.")
        _out("  Plex does not issue a narrower, server-scoped one -- this was verified")
        _out("  against a live account. Treat Reaper's database as equivalent to your")
        _out("  Plex password. It is encrypted at rest and redacted from logs.")
        _out()
        return 0
    finally:
        await engine.dispose()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="reaper-admin",
        description="Manage Reaper admin accounts from outside the web UI.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("list", help="List admin accounts and warn if lockout is possible.")

    create = sub.add_parser("create-admin", help="Create a local admin account.")
    create.add_argument("--username", required=True)
    create.add_argument(
        "--password",
        help="Omit to have a strong password generated (recommended).",
    )

    reset = sub.add_parser(
        "reset-password",
        help=(
            "Reset a password. Re-enables local login, so it works even if the "
            "account was Plex-only."
        ),
    )
    reset.add_argument("--username", required=True)
    reset.add_argument("--password", help="Omit to generate one.")

    deact = sub.add_parser(
        "deactivate",
        help="Deactivate an admin (refused for the last local admin).",
    )
    deact.add_argument("--username", required=True)

    link = sub.add_parser(
        "link-plex",
        help="Link the Plex server by signing in on plex.tv. Prints a URL; nothing is pasted.",
    )
    link.add_argument(
        "--server",
        help=(
            "If the account owns several servers: the exact name or machine identifier "
            "of the one Reaper should manage. Omit it to be shown the list."
        ),
    )

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    match args.command:
        case "list":
            return asyncio.run(_cmd_list())
        case "create-admin":
            return asyncio.run(_cmd_create(args.username, args.password))
        case "reset-password":
            return asyncio.run(_cmd_reset(args.username, args.password))
        case "deactivate":
            return asyncio.run(_cmd_deactivate(args.username))
        case "link-plex":
            return asyncio.run(_cmd_link_plex(args.server))
        case _:  # pragma: no cover -- argparse enforces this
            return 2


if __name__ == "__main__":
    raise SystemExit(main())
