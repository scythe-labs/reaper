# SPDX-License-Identifier: AGPL-3.0-or-later
"""Time.

Reaper has exactly one notion of time: a **UTC instant**. There is no local time
anywhere in the domain, and no naive datetime is ever valid -- every deletion
decision rests on watch-history recency, so an ambiguous instant is a correctness
bug that mis-deletes media.

Timestamps are stored as integer unix epoch (see ``reaper.db.types``) and the
upstream APIs speak epoch too: Tautulli's ``date`` / ``started`` / ``stopped`` /
``last_played`` / ``added_at`` and Plex's ``addedAt`` / ``lastViewedAt`` are all
unix ints. These helpers are the only sanctioned way across that boundary.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta


def utcnow() -> datetime:
    """The current instant, timezone-aware and UTC."""
    return datetime.now(UTC)


def from_epoch(value: int | float | str | None) -> datetime | None:
    """Convert an upstream unix timestamp into an aware UTC datetime.

    Returns ``None`` for null, empty, zero, or unparseable input.

    Zero is treated as absent deliberately. Tautulli and Plex use ``0`` (and
    sometimes ``""``) to mean "never played" -- and a 1970 date would be read by
    the scoring engine as *extremely stale*, which is the exact opposite of the
    truth. Absent must stay absent, so that an unknown value can only protect an
    item, never condemn it.
    """
    if value is None or value == "":
        return None
    try:
        seconds = int(float(value))
    except (TypeError, ValueError):
        return None
    if seconds <= 0:
        return None
    return datetime.fromtimestamp(seconds, tz=UTC)


def from_iso(value: str | None) -> datetime | None:
    """Parse an ISO-8601 timestamp into an aware UTC datetime.

    Seerr speaks ISO, not epoch: ``mediaAddedAt`` arrives as
    ``"2026-07-13T15:54:47.000Z"``. (Tautulli and Plex speak epoch. The two
    boundaries are genuinely different, so they get different parsers rather than
    one that guesses.)

    A timestamp without an offset is *rejected*, not assumed to be UTC. Guessing
    is how a deletion clock ends up hours out.
    """
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(UTC)


def to_epoch(value: datetime) -> int:
    """Convert an aware datetime to a unix timestamp."""
    if value.tzinfo is None:
        raise ValueError("Refusing to convert a naive datetime. Use utcnow().")
    return int(value.timestamp())


def days_since(value: datetime, *, now: datetime | None = None) -> float:
    """Whole and fractional days between ``value`` and now.

    The workhorse of the scoring engine: "not watched in 612 days".
    """
    return ((now or utcnow()) - value).total_seconds() / 86_400


def humanize_days(days: float) -> str:
    """A day count as a phrase a person reads without doing arithmetic.

    ``2060`` becomes ``"5 years, 7 months"``; ``90`` becomes ``"3 months"``; ``5`` becomes
    ``"5 days"``. Kept to the two most-significant units on purpose -- past "years, months"
    the extra precision is noise in a decision measured in years, and it reads worse.

    Approximate by construction (a month is 30 days, a year 365): these are the phrases the
    why-panel shows next to a dormancy floor, not accounting. Sub-day rounds to ``"today"``.
    """
    whole = round(days)
    if whole <= 0:
        return "today"

    years, remainder = divmod(whole, 365)
    months, day = divmod(remainder, 30)
    units = [(years, "year"), (months, "month"), (day, "day")]
    present = [(n, name) for n, name in units if n]
    if not present:
        return "today"
    return ", ".join(f"{n} {name}" if n == 1 else f"{n} {name}s" for n, name in present[:2])


def humanize_window(days: float) -> str:
    """A window length phrased for "in the last <window>".

    Same as :func:`humanize_days`, but a single-unit window drops the redundant "1": you say
    "in the last year", not "in the last 1 year". A multi-unit window ("6 months", "3 years")
    is left as-is.
    """
    text = humanize_days(days)
    return text[2:] if text.startswith("1 ") and "," not in text else text


def expiry(ttl: timedelta, *, now: datetime | None = None) -> datetime:
    return (now or utcnow()) + ttl
