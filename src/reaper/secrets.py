# SPDX-License-Identifier: AGPL-3.0-or-later
"""Resolving the key that encrypts every stored credential.

Reaper must not demand that you generate a key before it will start. But the key
cannot simply be minted at boot either: it decrypts credentials written on the
*previous* boot, so a fresh key on every start would render the entire database
of integration keys permanently unreadable -- silently, and only noticed the next
time a scan tried to talk to Sonarr.

So auto-generation *persists*. Precedence:

1. ``REAPER_SECRET_KEY`` in the environment -- always wins, never overwritten.
2. ``<data_dir>/secret.key`` -- generated on first boot, reused forever after.

Honest limitation: a key file sitting beside the database it protects does not
defend against an attacker who already has your filesystem. What it does defend
against is the ordinary way these leak -- a database copied into a backup, an
issue report, or a support thread. For real separation, set ``REAPER_SECRET_KEY``
from a secret manager and Reaper will never write the file at all.

Rotation, honestly: ``REAPER_SECRET_KEY`` decrypts data written on previous boots,
so switching it to a *fresh* value bricks every stored credential. To rotate, put
the new key in ``REAPER_SECRET_KEY`` and the *old* one in ``REAPER_SECRET_KEY_OLD``
(comma-separated for a chain); Reaper encrypts under the new key and still decrypts
what the old one wrote. Only drop the old key once everything has been re-saved.
"""

from __future__ import annotations

import math
import os
import secrets as pysecrets
import stat
from collections import Counter
from pathlib import Path

import structlog

from reaper.config import Settings, generate_secret_key

log = structlog.get_logger(__name__)

KEY_FILENAME = "secret.key"
SALT_FILENAME = "secret.salt"
_OWNER_READ_WRITE = 0o600
_SALT_BYTES = 16

# A floor below which an operator-supplied REAPER_SECRET_KEY is almost certainly a
# memorable passphrase rather than a generated key. We warn rather than refuse:
# refusing would brick an existing install that already runs on a short key, and the
# scrypt KDF (see reaper.crypto) already makes an offline attack expensive. The
# auto-generated token_urlsafe(32) key is ~43 chars and never trips this.
_WEAK_KEY_MIN_LENGTH = 24
_WEAK_KEY_MIN_ENTROPY_BITS = 80.0


class SecretMaterialError(RuntimeError):
    """Key or salt material is present but unusable, so Reaper refuses to start.

    The alternative -- what this used to do -- is to mint a replacement and carry on
    (S-5). That reads as recovery and is the opposite: the file is what decrypts every
    stored Sonarr/Radarr/Plex credential, so replacing it makes all of them permanently
    unreadable, silently, and the operator finds out on the next scan. A boot that stops
    with an explanation is recoverable; one that quietly re-keys is not.

    Missing material is a different thing entirely and still generates: an install with
    no key file yet has nothing to lose.
    """


def key_file_path(settings: Settings) -> Path:
    return settings.data_dir / KEY_FILENAME


def env_key_active(settings: Settings) -> bool:
    """Whether the operator supplies the encryption key from the environment.

    Mirrors :func:`resolve_secret_key`'s precedence exactly: a non-empty
    ``REAPER_SECRET_KEY`` always wins and is never written to disk, so a lingering
    ``secret.key`` file is inactive when the env key is set. Anything reporting where the
    key comes from, or whether a backup is self-sufficient, must read this rather than a
    bare ``key_file_path(settings).is_file()`` -- provenance follows runtime precedence,
    not file existence (rule 76).
    """
    if settings.secret_key is None:
        return False
    return bool(settings.secret_key.get_secret_value().strip())


def salt_file_path(settings: Settings) -> Path:
    return settings.data_dir / SALT_FILENAME


def resolve_kdf_salt(settings: Settings) -> bytes:
    """The per-install KDF salt, generated and persisted on first use.

    A random salt per install means a dictionary attack against one leaked database
    cannot be precomputed or reused against another install -- each guess must be
    stretched per target. The salt is not itself a secret (its job is uniqueness, not
    concealment), but it lives beside ``secret.key`` at 0600 all the same, and it is
    minted even when ``REAPER_SECRET_KEY`` comes from the environment: the KEY is the
    operator's to manage, the salt is install state. A salt file that has never existed
    is survivable -- :class:`~reaper.crypto.SecretBox` keeps the fixed v1 salt registered
    decrypt-only, so a fresh salt only re-keys what is written next -- but back it up
    with the key anyway so old and new data share one derivation.

    A salt file that exists and cannot be read is NOT survivable, and raises
    :class:`SecretMaterialError` rather than being replaced: everything written under the
    salt it held would stop decrypting, and the fixed-v1 fallback only covers data from
    before this install had a salt at all (S-5).

    Stored as hex so the file is inspectable and diffable like ``secret.key``.
    """
    settings.ensure_data_dir()
    path = salt_file_path(settings)

    if path.is_file():
        raw = path.read_text(encoding="utf-8").strip()
        try:
            salt = bytes.fromhex(raw)
        except ValueError:
            salt = b""
        if salt:
            _ensure_owner_only(path)
            return salt
        log.error("kdf_salt.unreadable_file", path=str(path))
        raise SecretMaterialError(
            f"{path} exists but could not be read as a salt. Reaper will not start, "
            "because generating a new one would make every credential saved under the "
            "old one unreadable. Restore this file from a backup, or delete it and "
            "re-enter your service keys."
        )

    salt = pysecrets.token_bytes(_SALT_BYTES)
    _write_key_atomically(path, salt.hex())
    log.info(
        "kdf_salt.generated",
        path=str(path),
        detail=(
            "A per-install salt for the credential encryption key was generated and "
            "saved. Back it up together with secret.key."
        ),
    )
    return salt


def resolve_old_keys(settings: Settings) -> list[str]:
    """Prior encryption keys, for decrypting data written before a key rotation.

    ``REAPER_SECRET_KEY_OLD`` is comma-separated so a multi-step rotation can list a
    chain of retired keys. Blank entries are ignored. These are threaded into
    :class:`~reaper.crypto.SecretBox` as decrypt-only keys.
    """
    if settings.secret_key_old is None:
        return []
    raw = settings.secret_key_old.get_secret_value()
    return [part.strip() for part in raw.split(",") if part.strip()]


def _shannon_entropy_bits(value: str) -> float:
    """A rough entropy estimate: Shannon entropy per character times the length.

    Deliberately crude -- it is only used to decide whether to *warn*. It flags
    'myplexserver2024' (repetitive, low per-char entropy) while passing a generated
    token, which is all it needs to do.
    """
    if not value:
        return 0.0
    counts = Counter(value)
    length = len(value)
    per_char = -sum((n / length) * math.log2(n / length) for n in counts.values())
    return per_char * length


def _warn_if_weak(key: str) -> None:
    """Warn (never refuse) when an operator-supplied key looks low-entropy."""
    if (
        len(key) >= _WEAK_KEY_MIN_LENGTH
        and _shannon_entropy_bits(key) >= _WEAK_KEY_MIN_ENTROPY_BITS
    ):
        return
    log.warning(
        "secret_key.weak",
        detail=(
            "REAPER_SECRET_KEY looks short or low-entropy. If the database ever leaks "
            "(a backup, an issue report), a weak key can be brute-forced offline even "
            "with key stretching. Generate a strong one with: "
            'python -c "import secrets; print(secrets.token_urlsafe(32))"'
        ),
    )


def resolve_secret_key(settings: Settings) -> str:
    """Return the encryption key, generating and persisting one if needed.

    Raises :class:`SecretMaterialError` when a key file exists but holds nothing. That
    used to regenerate, which bricks every credential in the database it sits beside
    (S-5); an empty file is far more likely a crashed write or a truncated volume than an
    install with nothing to lose, and the operator can still choose to delete it.
    """
    # 1. Explicit configuration always wins, and is never written to disk.
    if settings.secret_key is not None:
        value = settings.secret_key.get_secret_value().strip()
        if value:
            _warn_if_weak(value)
            return value

    settings.ensure_data_dir()
    path = key_file_path(settings)

    # 2. A key we generated on an earlier boot.
    if path.is_file():
        existing = path.read_text(encoding="utf-8").strip()
        if existing:
            _ensure_owner_only(path)
            return existing
        log.error("secret_key.empty_file", path=str(path))
        raise SecretMaterialError(
            f"{path} exists but is empty. Reaper will not start, because generating a "
            "new key would make every saved service credential unreadable. Restore this "
            "file from a backup, set REAPER_SECRET_KEY to the old value, or delete the "
            "file and re-enter your service keys."
        )

    # 3. First boot: mint one and persist it before anything is encrypted with it.
    key = generate_secret_key()
    _write_key_atomically(path, key)

    log.warning(
        "secret_key.generated",
        path=str(path),
        detail=(
            "No REAPER_SECRET_KEY was set, so one was generated and saved. "
            "BACK UP THIS FILE with your database. Losing it means every stored "
            "integration credential must be entered again. To manage the key "
            "yourself instead, set REAPER_SECRET_KEY and delete this file."
        ),
    )
    return key


def _write_key_atomically(path: Path, key: str) -> None:
    """Create the key file owner-only *from the outset*.

    ``Path.open('x'|'w')`` takes no mode, so the file is born world-readable under a
    typical 0022 umask and only tightened afterwards -- a window in which any local
    user can read the master key. ``os.open`` with an explicit mode closes that
    window: the file exists at 0600 the instant it appears. ``O_EXCL`` refuses to
    follow a symlink or clobber a pre-existing file, and we clamp the umask so the
    process default cannot loosen the requested mode.

    The callers only reach here when there is no readable material to lose, so the
    already-exists branch is for the odd leftover (a path that exists but is not a
    regular file, say). It tightens *before* writing rather than after, for the same
    reason the create path clamps the umask.
    """
    if path.exists():
        _ensure_owner_only(path)
        path.write_text(key + "\n", encoding="utf-8")
        return

    old_umask = os.umask(0o077)
    try:
        fd = os.open(str(path), os.O_CREAT | os.O_WRONLY | os.O_EXCL, _OWNER_READ_WRITE)
    finally:
        os.umask(old_umask)
    try:
        os.write(fd, (key + "\n").encode("utf-8"))
    finally:
        os.close(fd)
    # Belt and braces: guarantee 0600 even if a restrictive-but-not-identical umask
    # (or a prior file we did not expect) left it otherwise.
    _ensure_owner_only(path)


def _ensure_owner_only(path: Path) -> None:
    """Tighten the key file to 0600 if it is readable by anyone else."""
    try:
        mode = stat.S_IMODE(path.stat().st_mode)
        if mode != _OWNER_READ_WRITE:
            path.chmod(_OWNER_READ_WRITE)
    except OSError as exc:  # pragma: no cover -- e.g. an exotic bind mount
        log.warning(
            "secret_key.permissions",
            path=str(path),
            error=str(exc),
            detail="Could not restrict the key file to owner-only.",
        )
