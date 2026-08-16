# SPDX-License-Identifier: AGPL-3.0-or-later
"""Did Plex hand a large share of the library a new identity at once? (#809)

Reaper reads two systems, and either one can be rebuilt underneath it. Tautulli's side is
watched: ``history_sync._check_regression`` aborts the scan when its reported history total
falls, before anything is judged. The Plex side had nothing of that shape, and a library
rebuilt from scratch reissues every rating key in one go.

**The slow rebuild is what this catches.** A rebuild finished in an afternoon already trips
nothing worth saying, because its other effects fail safe on their own: a title with prior
plays reads ``Unknown`` rather than never-watched (``WatchHighWater``), and a reset
``added_at`` lowers dormancy, which lowers deletion pressure. One that leaves the library
unreadable for days while Reaper keeps scanning is the case with no guard: a staged migration,
or a storage outage. It arrives as a whole library binding to keys Reaper has never recorded.

**This degrades the scan, and does not abort it.** ``_check_regression`` runs before the
gather and has nothing to keep, where this is only knowable once both lanes have bound, and a
viewable snapshot beats a lost one. A degraded snapshot is un-plannable (``planner.build_plan``)
and its grace clocks and shelf are skipped (rule 116), so nothing can be deleted out of it.

**The ledger is still written**, which is what makes the next scan clean rather than wedged.
``library_seen.record`` unions keys onto the row, so no memory is lost by writing, and the
returns that a rebuild manufactures are refused separately by
``library_seen.RETURN_POPULATION_CAP``. Freezing the ledger here instead would leave the share
at 100% forever and every later scan un-plannable until an operator went and cleared it.
"""

from __future__ import annotations

from collections.abc import Mapping

import structlog

from reaper.services.library_seen import Seen

log = structlog.get_logger(__name__)

#: The share of the titles Reaper already has a key for that may bind to an entirely new key in
#: one scan before the scan stops counting as evidence.
#:
#: 10% because the ordinary rate measured over 24 days on an active library is about one movie
#: entry in a thousand and zero seasons (``docs/LEARNINGS.md``), where a rebuild is 100%. There
#: is a lot of room between those two, so the number is not delicate, and 10% still catches one
#: library of three being migrated. Deliberately not ``library_seen.RETURN_POPULATION_CAP``'s
#: 2%: that one prices a feature's own inputs, where this one stops the whole scan being
#: believed, so it sits further from the ordinary rate.
WHOLESALE_SHARE = 0.10

#: The floor under which no share is computed, because a share is meaningless over a handful of
#: titles. ``library_seen._CAP_APPLIES_ABOVE`` is the same number for the same reason, and the
#: two are deliberately separate declarations: they price different decisions and either can
#: move without the other.
#:
#: It also sets the smallest fire this guard can have: 10% of 200 is 20 titles changing identity
#: in one scan, against an ordinary rate that predicts 0.2.
_APPLIES_ABOVE = 200


def wholesale_change(recorded: Mapping[str, Seen], bound: Mapping[str, set[int]]) -> str | None:
    """What to tell the operator when identity moved wholesale this scan, else ``None``.

    ``bound`` is every Plex key this scan bound per ledger key, both lanes, as
    ``library_seen.note_sighting`` folds them. ``recorded`` is the ledger. A title counts as
    changed when what it binds now shares nothing with every key ever recorded for it, the same
    disjoint test ``library_seen.is_return``'s condition 2 makes one title at a time.

    **Only titles the ledger already knows are counted.** A bulk import of new titles cannot
    move the share, a first scan measures nothing, and a title Reaper saw once but could not
    bind this time is absent from ``bound`` and counts nowhere: an unreadable Plex records no
    sighting at all, so an outage cannot manufacture this.
    """
    known = 0
    changed = 0
    for id_key, fresh in bound.items():
        seen = recorded.get(id_key)
        if seen is None or not seen.rating_keys:
            continue
        known += 1
        if seen.rating_keys.isdisjoint(fresh):
            changed += 1
    if known < _APPLIES_ABOVE or changed < known * WHOLESALE_SHARE:
        # Every scan, so the ordinary rate on a second library can be read off a log rather
        # than measured with new code (#809's "what would settle the threshold").
        log.info("scan.identity_churn", changed=changed, known=known)
        return None
    log.warning("scan.identity_churn_wholesale", changed=changed, known=known)
    return (
        f"Plex is listing {changed:,} of the {known:,} titles Reaper knows as brand new, "
        "which usually means a library was rebuilt or moved. If that was you, clear the "
        "recorded watch history in Settings → Plex, then scan again"
    )
