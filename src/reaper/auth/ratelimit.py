# SPDX-License-Identifier: AGPL-3.0-or-later
"""In-process brute-force throttling and CPU-shedding for the login endpoints.

The login routes (``/api/auth/local``, ``/recover``, and the Plex sign-in pair) are
the only unauthenticated, state-establishing surface Reaper exposes, and every one
of them is covered here. ``POST /api/auth/local`` runs a full Argon2id verification on every
call (deliberately, even for a nonexistent user, so timing does not enumerate
usernames -- see :mod:`reaper.services.login`). Argon2id is *meant* to be
expensive, which turns that endpoint into two problems for an internet- or
LAN-exposed instance:

* **Credential brute-forcing.** Nothing slows a scripted dictionary attack down
  except Argon2's per-attempt cost, and attempts can be issued concurrently.
* **CPU exhaustion.** Each request forces a heavy hash, so a flood pins the CPU
  and denies the legitimate operator service.

The Plex sign-in endpoints are unauthenticated too, and cost differently: they
have no password to guess, but ``POST /api/auth/plex/start`` writes a pending row
and fires an outbound request to plex.tv. A flood of *successful* calls is the
problem there, so a consecutive-failure lockout never trips -- what bounds it is a
plain rate limit (S-1).

This module answers all three, dependency-free and in-process (no Redis, no
slowapi):

* :class:`Throttle` tracks consecutive failures per key -- we key on both the
  client IP and the attempted username -- and, past a threshold, refuses further
  attempts for a growing back-off window. That is the brute-force lock.
* :class:`RateLimiter` caps how many calls one key may make in a window, whether
  or not they succeed. That is the flood and amplification bound.
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
    "RateLimiter",
    "Throttle",
    "argon2_gate",
    "login_throttle",
    "password_throttle",
    "plex_poll_limit",
    "plex_start_limit",
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


@dataclass
class _Window:
    started: float = 0.0
    calls: int = 0


@dataclass
class RateLimiter:
    """A fixed-window call cap per key, counting every call rather than failures.

    :class:`Throttle` above locks out a key that keeps guessing WRONG, which is the right
    shape for a password. It is the wrong shape for an endpoint whose calls all succeed:
    ``/api/auth/plex/start`` inserts a pending row and asks plex.tv for a PIN every time,
    so a script can flood the table and get the install's egress address rate-limited by
    plex.tv -- locking the real operator out of Plex sign-in -- without ever failing once.

    A fixed window (rather than a sliding one) is deliberate: it is a handful of numbers
    per key, it cannot be gamed into more than ``2 * limit`` calls across a window
    boundary, and that bound is far below what the flood needs. Idle keys are swept the
    same way :class:`Throttle` sweeps its buckets.
    """

    limit: int
    window: float
    clock: Callable[[], float] = time.monotonic
    _sweep_at: int = 2048
    _windows: dict[str, _Window] = field(default_factory=dict)

    def retry_after(self, key: str) -> float:
        """Seconds until ``key`` may call again, or 0.0. Counts this call when allowed."""
        now = self.clock()
        self._maybe_sweep(now)
        window = self._windows.get(key)
        if window is None or now - window.started >= self.window:
            self._windows[key] = _Window(started=now, calls=1)
            return 0.0
        if window.calls < self.limit:
            window.calls += 1
            return 0.0
        return max(0.0, window.started + self.window - now)

    def reset(self) -> None:
        """Forget all state. For tests and for a clean process start."""
        self._windows.clear()

    def _maybe_sweep(self, now: float) -> None:
        if len(self._windows) < self._sweep_at:
            return
        stale = [k for k, w in self._windows.items() if now - w.started >= self.window]
        for k in stale:
            del self._windows[k]


class ConcurrencyGate:
    """A non-blocking cap on how many HASHES may be in flight at once.

    Used to bound in-flight Argon2 verifications: when the gate is full, a new
    login sheds load (the caller returns a fast "busy" rather than queuing more
    expensive hashing). A plain integer counter is correct here because
    :meth:`acquire` and :meth:`release` never await -- on the single event loop
    they are atomic -- and the awaits between them (the login's DB work) do not
    touch the counter.

    Slots are hashes, not requests. ``admin_password.verify`` runs one Argon2
    verification per local admin, so a single gated request used to occupy one slot
    while doing N verifications' worth of work -- the bound rule 11 asks for was
    counting the wrong thing, and a multi-admin install multiplied the CPU behind
    every slot (S-4). Callers now take as many slots as hashes they intend to run.
    """

    def __init__(self, limit: int) -> None:
        self._limit = max(1, limit)
        self._active = 0

    @property
    def limit(self) -> int:
        return self._limit

    def acquire(self, slots: int = 1) -> int:
        """Take ``slots`` if that many are free; return how many were taken, else 0.

        The count is clamped to the gate's own limit, so a caller wanting more hashes
        than the gate has slots waits for a quiet moment rather than being refused
        forever -- the clamp is why the return value matters: pass it back to
        :meth:`release` rather than assuming what was taken.
        """
        want = max(1, min(slots, self._limit))
        if self._active + want > self._limit:
            return 0
        self._active += want
        return want

    def release(self, slots: int = 1) -> None:
        # Guard against an underflow from a mispaired release; the count must
        # never drop below zero or the gate would over-admit forever after.
        self._active = max(0, self._active - max(1, slots))

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
# The settings endpoints that verify the admin password (arming deletion, changing the
# password itself) share this lockout. They are authenticated, but the admin password is
# still a guessable secret behind them: a borrowed session cookie or an unattended tab
# must not get an unthrottled Argon2 oracle. Same strictness as login.
password_throttle = Throttle(threshold=5, base_delay=2.0, max_delay=300.0, decay=900.0)
argon2_gate = ConcurrencyGate(_ARGON2_MAX_CONCURRENCY)

# The Plex sign-in pair. Sized off what the real flow does, with room to spare, so a
# person signing in never meets either one:
#
# * ``start`` mints a PIN. One sign-in needs one, and a few retries after a mistyped
#   password or a closed tab; 15 in 5 minutes is generous for a human and a hard stop
#   for a script pumping pending rows and plex.tv requests.
# * ``poll`` runs on a 2-second timer for up to the 5-minute deadline, so ONE sign-in is
#   already ~150 calls. The cap has to clear that comfortably or it would break the very
#   flow it protects; a poll is also much cheaper than a start (it reaches plex.tv only
#   for a pin id that has a pending row of its own).
#
# Both key on the client address alone: there is no account to key on before sign-in.
plex_start_limit = RateLimiter(limit=15, window=300.0)
plex_poll_limit = RateLimiter(limit=400, window=300.0)
