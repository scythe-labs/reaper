# SPDX-License-Identifier: AGPL-3.0-or-later
"""Time.

Reaper uses one notion of time: a UTC instant. The app never uses local time or a
naive datetime. Every deletion decision depends on how recently an item was
watched, so an unclear instant could delete the wrong media.

Timestamps are stored as integer unix epoch values (see ``reaper.db.types``), and
the upstream APIs use epoch too. Tautulli's ``date``, ``started``, ``stopped``,
``last_played``, and ``added_at`` fields, and Plex's ``addedAt`` and
``lastViewedAt`` fields, are all unix integers. These helpers are the only
sanctioned way to convert across that boundary.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta


def utcnow() -> datetime:
    """The current instant, timezone-aware and UTC."""
    return datetime.now(UTC)


def from_epoch(value: int | float | str | None) -> datetime | None:
    """Convert an upstream unix timestamp into an aware UTC datetime.

    Returns ``None`` for null, empty, zero, or unparseable input.

    Tautulli and Plex use ``0`` (and sometimes ``""``) to mean "never played."
    Treating that as absent is deliberate: a 1970 date would make the item look
    extremely stale, the opposite of the truth. An absent watch date must stay
    absent, so it can only protect an item and never add pressure toward
    deleting it.
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

    Seerr sends ISO timestamps, not epoch integers: ``mediaAddedAt`` arrives as
    ``"2026-07-13T15:54:47.000Z"``. Tautulli and Plex use epoch integers instead,
    so each source gets the parser that matches its format.

    Rejects a timestamp with no UTC offset instead of assuming UTC. Guessing the
    offset would let the deletion clock drift by hours.
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

    This is the number the scoring engine reports as "not watched in 612 days".
    """
    return ((now or utcnow()) - value).total_seconds() / 86_400


def humanize_days(days: float) -> str:
    """Turn a day count into a phrase a person reads without doing arithmetic.

    ``2060`` becomes ``"5 years, 7 months"``, ``90`` becomes ``"3 months"``, and ``5``
    becomes ``"5 days"``. Only the two largest units show, because past "years, months"
    extra precision only adds noise to a decision already measured in years.

    Treats a month as 30 days and a year as 365. These phrases feed the why-panel next
    to a dormancy floor, and are meant to read well, not to be exact.

    Returns ``"less than a day"`` for under a day, never ``"today"``. Every caller puts
    this phrase into a slot that wants a length ("not watched in ...", "untouched for
    ...", "released ... ago"), and a date word would read as broken English there.
    """
    whole = round(days)
    if whole <= 0:
        return "less than a day"

    years, remainder = divmod(whole, 365)
    months, day = divmod(remainder, 30)
    units = [(years, "year"), (months, "month"), (day, "day")]
    # ``whole >= 1`` past the early return, so at least one unit is always non-zero
    # and ``present`` is never empty. No second sub-day branch here: a dead one would
    # be a second place to keep the wording in step with.
    present = [(n, name) for n, name in units if n]
    return ", ".join(f"{n} {name}" if n == 1 else f"{n} {name}s" for n, name in present[:2])


def humanize_window(days: float) -> str:
    """Phrase a window length for "in the last <window>".

    Uses :func:`humanize_days`, but drops the redundant "1" from a single-unit window:
    "in the last year", not "in the last 1 year". A multi-unit window like "6 months" or
    "3 years" is left as-is.
    """
    text = humanize_days(days)
    return text[2:] if text.startswith("1 ") and "," not in text else text


def expiry(ttl: timedelta, *, now: datetime | None = None) -> datetime:
    return (now or utcnow()) + ttl
