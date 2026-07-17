# SPDX-License-Identifier: AGPL-3.0-or-later
"""Session-wide test configuration.

Two hermeticity guarantees, applied to every test:

**Cheap Argon2.** The hasher is patched to minimal cost parameters before any test
runs. Production defaults (time_cost=3, memory_cost=65536) are intentionally slow; on a
CI runner that can add several minutes to the suite for the 100+ tests that hash or
verify a password via fixtures. The patch is safe: tests care only that authentication
accepts the right password and rejects the wrong one, not about the hash's resistance
to offline cracking.

**No developer state, no network.** The autouse fixture below keeps every test off the
developer's real ``.env``/``.env.local`` and off the network, whether or not the test
boots the app. Without it, any test that constructs ``Settings`` silently reads the
repo-root ``.env`` (copying real service keys into throwaway test databases), and any
test that starts the app lifespan seeds instances from ``.env.local`` and kicks off the
~280 MB IMDb dataset download -- slow, flaky, and different from CI.
"""

import pytest
from pwdlib import PasswordHash
from pwdlib.hashers.argon2 import Argon2Hasher

import reaper.auth.passwords as _passwords
from reaper.auth.ratelimit import (
    argon2_gate,
    login_throttle,
    password_throttle,
    recover_throttle,
)
from reaper.config import Settings

_passwords._hasher = PasswordHash((Argon2Hasher(time_cost=1, memory_cost=8, parallelism=1),))


async def _no_catch_up(*_args: object, **_kwargs: object) -> None:
    return None


@pytest.fixture(autouse=True)
def _hermetic(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every test is hermetic: no ``.env`` reads, no startup seeding, no network.

    * ``Settings`` never reads the developer's dotenv files -- ``env_file`` is cleared
      for the duration of the test, so ``Settings(data_dir=tmp_path, ...)`` gets exactly
      the fields the test passes (and real environment variables, which CI controls).
    * The app lifespan's instance seeding (``load_raw_env``) and startup catch-up (the
      IMDb dataset download, scheduler catch-up) are stubbed out, exactly as the
      settings-API tests always did locally. Tests that exercise the real functions
      import them from their own modules, which this does not touch.
    """
    monkeypatch.setitem(Settings.model_config, "env_file", None)
    monkeypatch.setattr("reaper.main.load_raw_env", lambda _s: {})
    monkeypatch.setattr("reaper.main.catch_up_on_startup", _no_catch_up)
    # The auth throttles and the Argon2 gate are process-global singletons; a lockout
    # provoked by one test (every TestClient shares the same client address) must never
    # bleed into the next.
    login_throttle.reset()
    recover_throttle.reset()
    password_throttle.reset()
    argon2_gate.reset()
