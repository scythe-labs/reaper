# SPDX-License-Identifier: AGPL-3.0-or-later
"""In-process brute-force throttling and CPU-shedding for the login endpoints.

The login routes are the only unauthenticated, state-establishing surface Reaper
exposes, and ``POST /api/auth/local`` runs a full Argon2id verification on every
call (deliberately, even for a nonexistent user, so timing does not enumerate
usernames -- see :mod:`reaper.services.login`). Argon2id is *meant* to be
expensive, which turns that endpoint into two problems for an internet- or
LAN-exposed instance:

* **Credential brute-forcing.** Nothing slows a scripted dictionary attack down
  except Argon2's per-attempt cost, and attempts can be issued concurrently.
* **CPU exhaustion.** Each request forces a heavy hash, so a flood pins the CPU
  and denies the legitimate operator service.

This module answers both, dependency-free and in-process (no Redis, no slowapi):

* :class:`Throttle` tracks consecutive failures per key -- we key on both the
  client IP and the attempted username -- and, past a threshold, refuses further
  attempts for a growing back-off window. That is the brute-force lock.
* :class:`ConcurrencyGate` caps how many Argon2 verifications may be in flight at
  once, shedding load (a fast refusal) rather than piling on more hashing.

Both are process-local. Under multiple worker processes each keeps its own
counters; that weakens the global bound but never the fail-closed direction --
the worst case is an attacker gets N processes' worth of threshold, still finite,
still far short of unthrottled. A single-worker deployment (the default) gets the
full guarantee.

Nothing here blocks on I/O or awaits, so every method is atomic with respect to
the single event loop: there is no read-modify-write race to guard against.
"""

from __future__ import annotations

import os
import time
from collections.abc import Callable
from dataclasses import dataclass, field

__all__ = [
    "ConcurrencyGate",
    "Throttle",
    "argon2_gate",
    "login_throttle",
    "recover_throttle",
]


@dataclass
class _Bucket:
    failures: int = 0
    locked_until: float = 0.0
    last_seen: float = 0.0


@dataclass
class Throttle:
    """Consecutive-failure lockout with exponential back-off, keyed by a string.

    A key (an IP, a username) accumulates failures; once it reaches ``threshold``
    consecutive failures the key is locked for ``base_delay`` seconds, and each
    further failure doubles that window up to ``max_delay``. A success clears the
    key outright. Idle keys decay after ``decay`` seconds so the table cannot grow
    without bound and an attacker who walks away is forgiven in time.
    """

    threshold: int = 5
    base_delay: float = 2.0
    max_delay: float = 300.0
    decay: float = 900.0
    # Injectable so tests can drive time without sleeping. Monotonic, not wall
    # clock, so an NTP step or DST change cannot shorten or extend a lockout.
    clock: Callable[[], float] = time.monotonic
    # Above this many tracked keys we sweep out the stale ones. A safety valve
    # against memory growth from many distinct source IPs; normal operation never
    # approaches it.
    _sweep_at: int = 2048
    _buckets: dict[str, _Bucket] = field(default_factory=dict)

    def retry_after(self, key: str) -> float:
        """Seconds the caller must wait before this key may try again, or 0.0."""
        bucket = self._buckets.get(key)
        if bucket is None:
            return 0.0
        now = self.clock()
        remaining = bucket.locked_until - now
        return remaining if remaining > 0.0 else 0.0

    def record_failure(self, key: str) -> float:
        """Note a failed attempt for ``key``; return the resulting lockout seconds.

        The return value is 0.0 until the threshold is crossed, then the length of
        the back-off window now in force -- the caller can log a warning the first
        time it becomes non-zero.
        """
        now = self.clock()
        self._maybe_sweep(now)
        bucket = self._buckets.get(key)
        if bucket is None or now - bucket.last_seen > self.decay:
            bucket = _Bucket()
            self._buckets[key] = bucket
        bucket.failures += 1
        bucket.last_seen = now
        if bucket.failures >= self.threshold:
            # Exponential back-off from the moment of the threshold-crossing
            # failure: 1st over-threshold failure -> base_delay, then doubling.
            steps = bucket.failures - self.threshold
            delay = min(self.base_delay * (2.0**steps), self.max_delay)
            bucket.locked_until = now + delay
            return delay
        return 0.0

    def record_success(self, key: str) -> None:
        """Clear ``key`` -- a genuine login forgives its own prior failures."""
        self._buckets.pop(key, None)

    def reset(self) -> None:
        """Forget all state. For tests and for a clean process start."""
        self._buckets.clear()

    def _maybe_sweep(self, now: float) -> None:
        if len(self._buckets) < self._sweep_at:
            return
        stale = [
            k
            for k, b in self._buckets.items()
            if now - b.last_seen > self.decay and b.locked_until <= now
        ]
        for k in stale:
            del self._buckets[k]


class ConcurrencyGate:
    """A non-blocking cap on how many callers may hold the gate at once.

    Used to bound in-flight Argon2 verifications: when the gate is full, a new
    login sheds load (the caller returns a fast "busy" rather than queuing more
    expensive hashing). A plain integer counter is correct here because
    :meth:`acquire` and :meth:`release` never await -- on the single event loop
    they are atomic -- and the awaits between them (the login's DB work) do not
    touch the counter.
    """

    def __init__(self, limit: int) -> None:
        self._limit = max(1, limit)
        self._active = 0

    @property
    def limit(self) -> int:
        return self._limit

    def acquire(self) -> bool:
        """Take a slot if one is free. Returns False when the gate is full."""
        if self._active >= self._limit:
            return False
        self._active += 1
        return True

    def release(self) -> None:
        # Guard against an underflow from a mispaired release; the count must
        # never drop below zero or the gate would over-admit forever after.
        if self._active > 0:
            self._active -= 1

    def reset(self) -> None:
        self._active = 0


# Concurrency ceiling for Argon2 verifications. One per core lets the hashes run
# in parallel without letting a flood spawn unbounded, memory-hungry hashing at
# once; a couple of spare slots keep a legitimate operator from being turned away
# by a little contention.
_ARGON2_MAX_CONCURRENCY = max(2, (os.cpu_count() or 2))

# Process-wide singletons the auth router shares. The local-login throttle is the
# strict one (it guards the Argon2 path); recovery redeems a random single-use
# token and does no hashing, so it gets a looser cap that still stops a flood.
login_throttle = Throttle(threshold=5, base_delay=2.0, max_delay=300.0, decay=900.0)
recover_throttle = Throttle(threshold=10, base_delay=1.0, max_delay=120.0, decay=600.0)
argon2_gate = ConcurrencyGate(_ARGON2_MAX_CONCURRENCY)
