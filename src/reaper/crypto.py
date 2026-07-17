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

# scrypt cost. n must be a power of two; 2**14 with r=8 needs ~16 MiB and a few ms,
# which is negligible at boot (a handful of keys) yet a real per-guess cost offline.
_SCRYPT_N = 2**14
_SCRYPT_R = 8
_SCRYPT_P = 1
_SCRYPT_MAXMEM = 64 * 1024 * 1024


def _derive_fernet_key(secret: str, salt: bytes) -> bytes:
    """Stretch the secret into the 32 url-safe base64 bytes Fernet expects, with scrypt."""
    digest = hashlib.scrypt(
        secret.encode("utf-8"),
        salt=salt,
        n=_SCRYPT_N,
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
    under it, while the fixed v1 salt and the legacy SHA-256 derivation remain
    registered decrypt-only so everything written before still opens.
    """

    def __init__(self, current_key: str, *old_keys: str, salt: bytes | None = None) -> None:
        if not current_key:
            raise ValueError("A secret key is required to encrypt credentials at rest.")
        keys = [current_key, *old_keys]
        # For every key, register every derivation this install has ever written
        # under, newest first: the per-install scrypt key (what MultiFernet encrypts
        # and rotates to), then the fixed-salt scrypt key, then the legacy SHA-256
        # key -- the latter two decrypt-only, so pre-upgrade data still opens with no
        # migration and ages out as anything is re-saved or rotated.
        fernets: list[Fernet] = []
        for k in keys:
            if salt is not None:
                fernets.append(Fernet(_derive_fernet_key(k, salt)))
            fernets.append(Fernet(_derive_fernet_key(k, _DEFAULT_KDF_SALT)))
            fernets.append(Fernet(_derive_legacy_fernet_key(k)))
        self._fernet = MultiFernet(fernets)

    def encrypt(self, plaintext: str) -> str:
        return self._fernet.encrypt(plaintext.encode("utf-8")).decode("ascii")

    def decrypt(self, token: str) -> str:
        try:
            return self._fernet.decrypt(token.encode("ascii")).decode("utf-8")
        except InvalidToken as exc:
            raise ValueError(
                "Could not decrypt a stored credential. REAPER_SECRET_KEY has probably "
                "changed; restore the original key or re-enter the credential."
            ) from exc

    def rotate(self, token: str) -> str:
        """Re-encrypt an existing token under the current key."""
        return self._fernet.rotate(token.encode("ascii")).decode("ascii")
