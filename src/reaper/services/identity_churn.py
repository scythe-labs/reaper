# SPDX-License-Identifier: AGPL-3.0-or-later
"""Did Plex hand a large share of the library a new identity at once?

Reaper reads two systems, and either one can be rebuilt underneath it. Tautulli's side is
watched: ``history_sync._check_regression`` aborts the scan when its reported history total
falls, before anything is judged. The Plex side had nothing of that shape, and a library
rebuilt from scratch reissues every rating key at once.

**A slow rebuild is what this catches.** A rebuild finished in an afternoon already trips
nothing worth saying, because its other effects fail safe on their own: a title with prior
plays reads ``Unknown`` rather than never-watched (``WatchHighWater``), and a reset
``added_at`` lowers dormancy, which lowers deletion pressure. A rebuild that leaves the
library unreadable for days while Reaper keeps scanning has no other guard: a staged
migration, or a storage outage, shows up as a whole library binding to keys Reaper has never
recorded.

**This degrades the scan instead of aborting it.** ``_check_regression`` runs before the
gather and has nothing to keep. This check can only answer once both lanes have bound, and a
viewable snapshot beats a lost one. A degraded snapshot cannot be planned
(``planner.build_plan``), and its grace clocks and its Leaving Soon shelf are skipped too, so
nothing can be deleted from it.

**The ledger is still written**, which is what leaves the next scan clean instead of stuck.
``library_seen.record`` adds keys to the row rather than replacing them, so writing loses no
memory, and the returns a rebuild would otherwise manufacture are refused separately by
``library_seen.RETURN_POPULATION_CAP``. Skipping the ledger write here would leave the share
at 100% forever, and every later scan unplannable until an operator cleared it by hand.
"""

from __future__ import annotations

from collections.abc import Mapping

import structlog

from reaper.services.library_seen import Seen

log = structlog.get_logger(__name__)

#: The share of titles Reaper already has a key for that may bind to a brand new key in one
#: scan before the scan stops counting as evidence.
#:
#: Measured churn on an active library is about one movie in a thousand and no seasons over
#: 24 days (``docs/LEARNINGS.md``), where a rebuild is 100%. 10% sits far enough above that
#: ordinary rate to still catch one library out of three being migrated, without being a
#: delicate number. It is deliberately not ``library_seen.RETURN_POPULATION_CAP``'s 2%: that
#: one prices a feature's own inputs, where this one decides whether to trust the whole scan,
#: so it sits further from the ordinary rate.
WHOLESALE_SHARE = 0.10

#: The floor below which no share is computed, because a share is meaningless over a handful
#: of titles. ``library_seen._CAP_APPLIES_ABOVE`` uses the same number for the same reason.
#: The two constants are declared separately on purpose: they price different decisions, and
#: either one can change without moving the other.
#:
#: This also sets the smallest count this guard can flag: 10% of 200 is 20 titles changing
#: identity in one scan, against an ordinary rate that predicts about 0.2.
_APPLIES_ABOVE = 200

#: The in-app help page the notice links to, by its id in `frontend/src/docs/registry.ts`. A
#: backend module naming a frontend declaration needs a test guarding against drift, and
#: ``tests/test_identity_churn.py::TestTheHelpPage`` is that test: a renamed doc would leave
#: this link opening nothing, and nothing else would notice.
HELP_DOC = "plex-rebuild"


def wholesale_change(recorded: Mapping[str, Seen], bound: Mapping[str, set[int]]) -> str | None:
    """What to tell the operator when identity moved wholesale this scan, or ``None``.

    ``bound`` is every Plex key this scan bound per ledger key, from both lanes, as
    ``library_seen.note_sighting`` folds them. ``recorded`` is the ledger. A title counts as
    changed when what it binds now shares nothing with any key ever recorded for it, the same
    test ``library_seen.is_return``'s second condition makes for one title at a time.

    **Only titles the ledger already knows are counted.** A bulk import of new titles cannot
    move the share, a first scan measures nothing, and a title Reaper saw once but could not
    bind this time is absent from ``bound`` and counts nowhere. An unreadable Plex records no
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
        # Logged every scan, so the ordinary rate on a second library can be read off a
        # log instead of measured with new code.
        log.info("scan.identity_churn", changed=changed, known=known)
        return None
    log.warning("scan.identity_churn_wholesale", changed=changed, known=known)
    # This names the repair in the order an operator needs to do it. Tautulli keeps every
    # play filed under the id the file had when it was watched, so a rebuild leaves its
    # history pointing at copies that are gone, and that gets fixed at the source. Reaper's
    # own mirror is corrected by the full sweep, which replaces each row by its id instead
    # of only adding new ones (`scheduler.full_history_sweep`), named here the same way the
    # job is named in `JobsPanel.tsx`.
    #
    # Never suggest the Forget button on Settings, Plex, as the first step. It discards the
    # record that tells "the plays went unreadable" apart from "nobody ever watched it,"
    # which withdraws three protections from every title at once. It is the right move only
    # when the source cannot be repaired, and naming it first would make it look like the
    # main fix rather than a fallback.
    return (
        f"Plex is listing {changed:,} of the {known:,} titles Reaper knows as brand new, "
        "which usually means a library was rebuilt or moved. Repair the watch history in "
        "Tautulli, then run a full watch-history update on Settings → Jobs"
    )
