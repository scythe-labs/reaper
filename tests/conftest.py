# SPDX-License-Identifier: AGPL-3.0-or-later
"""Session-wide test configuration.

Patches the Argon2 hasher to minimal cost parameters before any test runs.
Production defaults (time_cost=3, memory_cost=65536) are intentionally slow;
on a CI runner that can add several minutes to the suite for the 100+ tests
that hash or verify a password via fixtures. The patch is safe: tests care
only that authentication accepts the right password and rejects the wrong one,
not about the hash's resistance to offline cracking.
"""

from pwdlib import PasswordHash
from pwdlib.hashers.argon2 import Argon2Hasher

import reaper.auth.passwords as _passwords

_passwords._hasher = PasswordHash((Argon2Hasher(time_cost=1, memory_cost=8, parallelism=1),))
