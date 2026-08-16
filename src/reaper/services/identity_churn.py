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

#: The in-app help page the notice links to, by its id in `frontend/src/docs/registry.ts`. A
#: backend module naming a frontend declaration needs the guard rule 103 asks for, and it is
#: ``tests/test_identity_churn.py::TestTheHelpPage``: a renamed doc leaves this link opening
#: nothing, and nothing else would notice.
HELP_DOC = "plex-rebuild"


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
    # The repair, in the order an operator has to do it. Tautulli keeps every play filed under
    # the id the file had when it was watched, so a rebuild leaves its history pointing at
    # copies that are gone, and that is fixed at the source. Reaper's own mirror is corrected
    # by the full sweep, which replaces each row by its id rather than only adding new ones
    # (`scheduler.full_history_sweep`), and the name here is the job's own title in
    # `JobsPanel.tsx` (rule 144).
    #
    # Deliberately NOT the Forget button on Settings, Plex. That one discards the record that
    # tells "the plays went unreadable" from "nobody ever watched it", which withdraws three
    # protections from every title at once. It is where an operator lands when the source
    # cannot be repaired, and a notice that offers it first turns the fallback into the
    # instruction.
    return (
        f"Plex is listing {changed:,} of the {known:,} titles Reaper knows as brand new, "
        "which usually means a library was rebuilt or moved. Repair the watch history in "
        "Tautulli, then run a full watch-history update on Settings → Jobs"
    )
