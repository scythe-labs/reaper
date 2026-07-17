# SPDX-License-Identifier: AGPL-3.0-or-later
"""The requester rule: "you asked for this and never watched it".

The rule is evaluated **per media item, not per request** -- a distinction found
by running it against a real Seerr instance, where the same title is frequently
requested by several people. Judged per request, a film that Alice requested and
watched would still be condemned on Bob's row, because Bob never played it. The
file is shared; the verdict must be too.

So: gather every requester of an item, and the rule only fires when **none** of
them has watched it.

Three ways it abstains, all of which protect the file:

* **Someone else is watching it.** The requesters ignored it, but the library did
  not. Deleting it would punish the wrong people.
* **We cannot tell.** No ``ratingKey`` (Plex has not matched it), no
  ``mediaAddedAt`` (we do not know when the clock started), or a requester with no
  ``plexId`` (we cannot look up their history). This is not rare: on a live instance a
  small but real percentage of available requests had no rating key and no added-at.
  Every one of them abstains, and abstaining protects.
* **It has not been available long enough.** The clock starts at ``mediaAddedAt``,
  not ``createdAt``: you cannot fail to watch something that did not exist yet.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime

from reaper.clients.seerr import MediaRequest
from reaper.clock import days_since


@dataclass(frozen=True)
class WatchEvidence:
    """What Tautulli knows about who played an item."""

    plays_by_user: dict[int, int] = field(default_factory=dict)
    """Tautulli user_id -> play count."""

    distinct_watchers: int = 0

    def plays_by(self, plex_user_id: int | None) -> int:
        if plex_user_id is None:
            return 0
        return self.plays_by_user.get(plex_user_id, 0)

    def others_watching(self, exclude: set[int]) -> int:
        """How many people *other than* the requesters have played it."""
        return sum(
            1 for uid, plays in self.plays_by_user.items() if uid not in exclude and plays > 0
        )


class Verdict(str):
    """Kept as a plain string for readability in the why-panel."""


CONDEMN = Verdict("condemn")
PROTECT = Verdict("protect")
ABSTAIN = Verdict("abstain")


@dataclass(frozen=True)
class RequesterFinding:
    """The rule's output, carrying everything the why-panel must show.

    Note ``checks_performed``: the protections that were evaluated and did *not*
    fire, with their actual numbers. That block is what makes the verdict
    trustworthy -- it shows the work, not just the conclusion.
    """

    verdict: Verdict
    reason: str
    checks_performed: list[str]
    requesters: list[str]
    days_available: float | None
    requester_plays: int
    other_watchers: int


@dataclass(frozen=True)
class RequesterPolicy:
    unwatched_days: int = 90
    """How long an item may sit unwatched by its requesters before it is flagged."""

    requester_engaged_plays: int = 1
    """The LOW bar. Any real play means the requester engaged -- do not condemn.
    Deliberately low: someone who watched half a film has not ignored it."""

    others_watching_plays: int = 1
    """The bar for "someone else values this". Kept separate from the above so the
    two can be tuned independently: a 30-second accidental start should not
    immortalise a 60 GB file."""


def group_by_media(requests: list[MediaRequest]) -> dict[str, list[MediaRequest]]:
    """Collapse requests onto the media they point at.

    Keyed by Plex rating key, which is what the file actually is. Requests with no
    rating key are grouped under a synthetic key so they remain visible -- they
    must be protected, not dropped on the floor.
    """
    grouped: dict[str, list[MediaRequest]] = defaultdict(list)
    for request in requests:
        key = request.plex_rating_key or f"unmatched:{request.request_id}"
        grouped[key].append(request)
    return dict(grouped)


def evaluate(
    requests: list[MediaRequest],
    evidence: WatchEvidence,
    policy: RequesterPolicy,
    *,
    now: datetime | None = None,
    horizon: datetime | None = None,
) -> RequesterFinding:
    """Judge one media item, given every request that points at it.

    ``horizon`` is how far back the watch mirror reaches (see ``history_sync.horizon``);
    the availability clock is clamped to it, exactly as the scan path clamps dormancy.
    """
    requesters = [r.requester for r in requests]
    names = [r.display_name or r.username or f"user:{r.seerr_user_id}" for r in requesters]
    requester_ids = {r.plex_id for r in requesters if r.plex_id is not None}

    requester_plays = sum(evidence.plays_by(r.plex_id) for r in requesters)
    other_watchers = evidence.others_watching(exclude=requester_ids)

    checks: list[str] = []

    # -- abstentions first. Unknown may only protect, never condemn. ------------

    if any(not r.is_mappable for r in requesters):
        unmappable = [n for n, r in zip(names, requesters, strict=True) if not r.is_mappable]
        return RequesterFinding(
            verdict=ABSTAIN,
            reason=(
                f"Cannot tell whether {', '.join(unmappable)} watched this: no Plex account "
                "is linked to their Seerr user, so their history is invisible to Reaper."
            ),
            checks_performed=checks,
            requesters=names,
            days_available=None,
            requester_plays=requester_plays,
            other_watchers=other_watchers,
        )

    if not any(r.plex_rating_key for r in requests):
        return RequesterFinding(
            verdict=ABSTAIN,
            reason=(
                "Plex has not matched this item, so it has no rating key and no watch "
                "history can be looked up."
            ),
            checks_performed=checks,
            requesters=names,
            days_available=None,
            requester_plays=requester_plays,
            other_watchers=other_watchers,
        )

    available_at = min(
        (r.available_at for r in requests if r.available_at is not None),
        default=None,
    )
    if available_at is None:
        return RequesterFinding(
            verdict=ABSTAIN,
            reason=(
                "Seerr does not record when this item became available, so there is no "
                "clock to measure against. The request date is not a substitute: you "
                "cannot fail to watch something that did not exist yet."
            ),
            checks_performed=checks,
            requesters=names,
            days_available=None,
            requester_plays=requester_plays,
            other_watchers=other_watchers,
        )

    age = days_since(available_at, now=now)
    if horizon is not None:
        # The mirror only reaches back to ``horizon``: an item available for longer than
        # the mirror is deep cannot honestly read "never watched since it arrived", only
        # "not watched within the mirror's reach". Clamping the clock keeps a shallow
        # mirror from inflating every aged request into a reclaimable one -- the exact
        # mass-wrong-verdict the scan path stores its horizon to prevent.
        age = min(age, days_since(horizon, now=now))

    # -- protections ----------------------------------------------------------

    if requester_plays >= policy.requester_engaged_plays:
        return RequesterFinding(
            verdict=PROTECT,
            reason=(
                f"The requester watched it: {requester_plays} "
                f"{'play' if requester_plays == 1 else 'plays'} by {', '.join(names)}."
            ),
            checks_performed=checks,
            requesters=names,
            days_available=age,
            requester_plays=requester_plays,
            other_watchers=other_watchers,
        )
    checks.append(
        f"checked: requester watched it -- {requester_plays} plays by "
        f"{', '.join(names)}, threshold is {policy.requester_engaged_plays}"
    )

    if other_watchers >= policy.others_watching_plays:
        return RequesterFinding(
            verdict=PROTECT,
            reason=(
                f"The requester never watched it, but {other_watchers} other "
                f"{'person is' if other_watchers == 1 else 'people are'} watching it. "
                "Deleting it would punish them for someone else's request."
            ),
            checks_performed=checks,
            requesters=names,
            days_available=age,
            requester_plays=requester_plays,
            other_watchers=other_watchers,
        )
    checks.append(
        f"checked: someone else is watching it -- {other_watchers} other watchers, "
        f"threshold is {policy.others_watching_plays}"
    )

    if age < policy.unwatched_days:
        return RequesterFinding(
            verdict=PROTECT,
            reason=(
                f"Only available for {age:.0f} days; your floor is {policy.unwatched_days}. "
                "Not yet ignored, just new."
            ),
            checks_performed=checks,
            requesters=names,
            days_available=age,
            requester_plays=requester_plays,
            other_watchers=other_watchers,
        )
    checks.append(
        f"checked: too recent to judge -- available {age:.0f} days, "
        f"floor is {policy.unwatched_days}"
    )

    # -- condemn --------------------------------------------------------------

    return RequesterFinding(
        verdict=CONDEMN,
        reason=(
            f"Requested by {', '.join(names)}, available for {age:.0f} days, and never "
            "played by anyone -- not the requester, not anyone else."
        ),
        checks_performed=checks,
        requesters=names,
        days_available=age,
        requester_plays=requester_plays,
        other_watchers=other_watchers,
    )
