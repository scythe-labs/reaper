# SPDX-License-Identifier: AGPL-3.0-or-later
"""Encryption for integration credentials stored in the database.

Every API key Reaper holds is destructive-capable:

* the Tautulli key is full admin (it can ``delete_library`` and ``restart``)
* the Sonarr/Radarr keys can delete media
* the Plex token grants administrative control of the server

So none of them are stored in plaintext, and none of them are ever logged.
``MultiFernet`` lets us rotate the key without a downtime migration: put the new
key first, keep the old one, re-encrypt lazily, then drop the old key.

The Fernet key is *stretched* from the secret with scrypt, not derived with a bare
hash. The threat this module names -- a database copied into a backup, an issue
report, or a support thread -- is exactly the setting where an offline dictionary
attack applies, and a single unsalted SHA-256 would let an attacker try billions of
guesses a second against a low-entropy operator-supplied ``REAPER_SECRET_KEY``.
scrypt's work factor makes each guess expensive. The auto-generated key is already
256-bit random and needs none of this, but ``SecretBox`` cannot tell the two apart,
and the cost is paid once per key at construction -- never per encrypt/decrypt.
"""

from __future__ import annotations

import base64
import hashlib

from cryptography.fernet import Fernet, InvalidToken, MultiFernet

# The fixed, app-wide v1 salt for the credential KDF. Since the per-install salt landed
# (``reaper.secrets.resolve_kdf_salt``, a random salt minted beside secret.key and passed
# into SecretBox), this fixed value is kept for DECRYPT-ONLY compatibility: data written
# before an install had its own salt still opens, and new data is always written under
# the per-install derivation. A construction that passes no salt (tests, mostly) still
# encrypts under this fixed value, which defeats generic precomputed tables; the
# per-install salt additionally makes any dictionary attack non-reusable across installs.
_DEFAULT_KDF_SALT = b"reaper.at-rest.credential-key.v1"

# scrypt cost. n must be a power of two, and the working set is 128 * n * r bytes.
#
# The whole point of this KDF is the OFFLINE case: a leaked database plus a low-entropy
# operator-supplied REAPER_SECRET_KEY. That attacker runs guesses on their own hardware,
# so only the per-guess cost bounds them -- and the memory term is the part that resists
# GPUs and ASICs, which have arithmetic to spare and bandwidth to spare much less of. The
# cost is paid ONCE per key at process start, never per encrypt/decrypt and never per
# request, so what a login endpoint could never afford is free here.
#
# 2**16 with r=8 is 64 MiB and ~130 ms per guess per core. The original 2**14 (16 MiB, a
# few ms) left an attacker in the thousands of guesses per second per core, which is thin
# cover for a memorable passphrase (S-3).
_SCRYPT_N = 2**16
_SCRYPT_R = 8
_SCRYPT_P = 1
# 128 * n * r = 64 MiB, plus headroom for scrypt's own working buffers.
_SCRYPT_MAXMEM = 192 * 1024 * 1024

#: The cost every derivation written before that change used. Registered decrypt-only, so
#: credentials stored by an earlier Reaper still open with no migration and no re-entry.
#: Raising ``_SCRYPT_N`` again means adding the superseded value here, never dropping one.
_SUPERSEDED_SCRYPT_N = (2**14,)


def _derive_fernet_key(secret: str, salt: bytes, n: int = _SCRYPT_N) -> bytes:
    """Stretch the secret into the 32 url-safe base64 bytes Fernet expects, with scrypt."""
    digest = hashlib.scrypt(
        secret.encode("utf-8"),
        salt=salt,
        n=n,
        r=_SCRYPT_R,
        p=_SCRYPT_P,
        maxmem=_SCRYPT_MAXMEM,
        dklen=32,
    )
    return base64.urlsafe_b64encode(digest)


def _derive_legacy_fernet_key(secret: str) -> bytes:
    """The pre-scrypt derivation: a single unsalted SHA-256.

    Kept for *decryption only*, so credentials written before the KDF change still
    open on upgrade -- no migration, no re-entry. New data is always written under the
    scrypt key (which comes first in the MultiFernet below), so legacy tokens age out
    on their own as anything is re-saved or rotated.
    """
    digest = hashlib.sha256(secret.encode("utf-8")).digest()
    return base64.urlsafe_b64encode(digest)


class SecretBox:
    """Encrypts and decrypts credentials at rest.

    Pass the current key first; any older keys after it are accepted for
    decryption only. ``salt`` is the per-install KDF salt
    (:func:`reaper.secrets.resolve_kdf_salt`); when given, new data is encrypted
    under it, while the fixed v1 salt, every superseded scrypt cost, and the legacy
    SHA-256 derivation stay available decrypt-only so everything written before
    still opens.

    Only the CURRENT derivation is computed up front -- one scrypt per key. The
    superseded ones are built on the first token that does not open with it, so an
    install with nothing old to read never pays for them, and one that does pays
    once per process. That matters because the current cost is deliberately high
    (see ``_SCRYPT_N``): deriving every historical variant eagerly would multiply
    boot time by the number of derivations Reaper has ever shipped.
    """

    def __init__(self, current_key: str, *old_keys: str, salt: bytes | None = None) -> None:
        if not current_key:
            raise ValueError("A secret key is required to encrypt credentials at rest.")
        self._keys = [current_key, *old_keys]
        self._salt = salt
        # What new data is written under: this install's own salt when it has one, else
        # the fixed v1 salt. MultiFernet encrypts with the first entry and decrypts with
        # any, so the current key leads and retired keys follow.
        write_salt = salt if salt is not None else _DEFAULT_KDF_SALT
        self._fernet = MultiFernet([Fernet(_derive_fernet_key(k, write_salt)) for k in self._keys])
        self._superseded: MultiFernet | None = None

    def _superseded_fernet(self) -> MultiFernet:
        """Every derivation this install may have written under before the current one.

        Built once, on demand. Two threads racing here both build an equivalent object and
        one wins the assignment, which is harmless -- the result is a pure function of the
        keys and salt.
        """
        if self._superseded is None:
            fernets: list[Fernet] = []
            for k in self._keys:
                if self._salt is not None:
                    # Written under this install's salt at an older cost, and -- for data
                    # that predates the per-install salt entirely -- under the fixed one.
                    fernets += [
                        Fernet(_derive_fernet_key(k, self._salt, n)) for n in _SUPERSEDED_SCRYPT_N
                    ]
                    fernets.append(Fernet(_derive_fernet_key(k, _DEFAULT_KDF_SALT)))
                fernets += [
                    Fernet(_derive_fernet_key(k, _DEFAULT_KDF_SALT, n))
                    for n in _SUPERSEDED_SCRYPT_N
                ]
                fernets.append(Fernet(_derive_legacy_fernet_key(k)))
            self._superseded = MultiFernet(fernets)
        return self._superseded

    def encrypt(self, plaintext: str) -> str:
        return self._fernet.encrypt(plaintext.encode("utf-8")).decode("ascii")

    def decrypt(self, token: str) -> str:
        raw = token.encode("ascii")
        try:
            return self._fernet.decrypt(raw).decode("utf-8")
        except InvalidToken:
            pass
        try:
            return self._superseded_fernet().decrypt(raw).decode("utf-8")
        except InvalidToken as exc:
            raise ValueError(
                "Could not decrypt a stored credential. REAPER_SECRET_KEY has probably "
                "changed; restore the original key or re-enter the credential."
            ) from exc

    def rotate(self, token: str) -> str:
        """Re-encrypt an existing token under the current key and current derivation.

        Decrypt-then-encrypt rather than ``MultiFernet.rotate``, which can only re-key a
        token one of ITS OWN fernets opens -- and the superseded derivations deliberately
        live outside that set. Rotating is exactly the operation that should reach them:
        the tokens worth re-keying are the old ones.
        """
        return self.encrypt(self.decrypt(token))
