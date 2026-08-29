# SPDX-License-Identifier: AGPL-3.0-or-later
"""The scan-level check on how much of the library changed Plex identity at once.

``tests/test_scan_pipeline.py::TestALibraryWideIdentityChangeDegradesTheSnapshot`` drives
the same guard through a whole scan, which is what pins the call site. These cases pin what
it counts.
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

from reaper.clock import utcnow
from reaper.services import identity_churn
from reaper.services.library_seen import Seen

REPO = Path(__file__).resolve().parents[1]

NOW = utcnow().replace(microsecond=0)

#: The keys the ledger holds start here, and the ones a rebuild issues start far away, so a
#: "changed" title in these fixtures can never collide with an unchanged one's key.
_OLD = 1_000
_REISSUED = 9_000_000

#: Deliberately above the shipped floor rather than equal to it. A fixture sitting on the
#: production number cannot tell a floor that is read from one that is ignored. The floor's
#: own cases name it directly.
LIBRARY = 300


def _ledger(count: int, *, keys_each: int = 1) -> dict[str, Seen]:
    """``count`` titles the ledger has seen before, each under ``keys_each`` keys."""
    return {
        f"movie:tmdb:{i}": Seen(
            rating_keys=frozenset(_OLD + i * 10 + k for k in range(keys_each)),
            last_seen_at=NOW - timedelta(days=3),
            returned_at=None,
            returned_by_reaper=None,
        )
        for i in range(count)
    }


def _bound(count: int, *, changed: int) -> dict[str, set[int]]:
    """The same titles, the first ``changed`` of them bound to a key never recorded."""
    return {
        f"movie:tmdb:{i}": {_REISSUED + i if i < changed else _OLD + i * 10} for i in range(count)
    }


class TestWhatCountsAsChanged:
    def test_a_rebuilt_library_is_named_for_the_operator(self) -> None:
        said = identity_churn.wholesale_change(_ledger(LIBRARY), _bound(LIBRARY, changed=LIBRARY))
        assert said is not None
        # The counts, and the repair in the order it has to happen. Tautulli holds the
        # plays under the ids the files used to have, and the full sweep is what pulls the
        # corrected ids into Reaper's mirror (`scheduler.full_history_sweep`).
        assert "300 of the 300" in said
        assert "Tautulli" in said
        assert "Settings → Jobs" in said

    def test_ordinary_key_churn_is_not_an_event(self) -> None:
        """About one entry in a thousand moves on a healthy library (docs/LEARNINGS.md)."""
        assert identity_churn.wholesale_change(_ledger(LIBRARY), _bound(LIBRARY, changed=1)) is None

    def test_a_key_the_ledger_already_holds_is_not_a_change(self) -> None:
        """A title listed twice, or one whose bind moved back. Recorded means recorded."""
        ledger = _ledger(LIBRARY, keys_each=3)
        # Every title binds the *last* of the three keys recorded for it, not the first.
        bound = {f"movie:tmdb:{i}": {_OLD + i * 10 + 2} for i in range(LIBRARY)}
        assert identity_churn.wholesale_change(ledger, bound) is None

    def test_a_title_with_no_recorded_key_counts_nowhere(self) -> None:
        """An unreadable ``rating_keys_json`` reads as an empty set (``library_seen.recall_all``).

        It cannot be shown to have changed, so it must not sit in the denominator either, where
        it would dilute a real event toward the threshold.
        """
        ledger = _ledger(LIBRARY)
        for id_key in list(ledger)[:100]:
            ledger[id_key] = Seen(
                rating_keys=frozenset(),
                last_seen_at=NOW,
                returned_at=None,
                returned_by_reaper=None,
            )
        # 200 titles left with a key, every one of them reissued. Still the whole
        # population.
        assert identity_churn.wholesale_change(ledger, _bound(LIBRARY, changed=LIBRARY)) is not None


class TestWhatCannotManufactureAnEvent:
    def test_titles_the_ledger_has_never_seen_do_not_count(self) -> None:
        """A bulk import is not an identity event, however large it is."""
        bound = _bound(LIBRARY, changed=0) | {
            f"movie:tmdb:{i}": {_REISSUED + i} for i in range(9000, 12000)
        }
        assert identity_churn.wholesale_change(_ledger(LIBRARY), bound) is None

    def test_an_unreadable_plex_records_no_sighting_at_all(self) -> None:
        """No bind, no entry in ``bound``. An outage shrinks the population, never the
        share."""
        assert identity_churn.wholesale_change(_ledger(5000), _bound(3, changed=3)) is None


class TestTheHelpPage:
    """This module names a frontend declaration, so a rename must fail here.

    ``Snapshot.degraded_doc`` carries this id to the browser, where the notice looks it up in the
    docs registry and renders nothing if it is not there. That is the right failure for a stored
    scan written by another version, and the wrong one for a doc renamed in this tree, where it
    would silently take the link off a notice nobody is watching.
    """

    def test_the_id_the_notice_carries_is_a_doc_that_exists(self) -> None:
        content = REPO / "frontend" / "src" / "docs" / "content"
        declared = {
            line.split('"')[1]
            for path in content.glob("*.ts")
            for line in path.read_text().splitlines()
            if line.strip().startswith('id: "')
        }
        # The population, not just the member. One id per content file, so a walk that
        # stopped reading the tree cannot pass this by finding nothing.
        assert len(declared) == len(list(content.glob("*.ts")))
        assert identity_churn.HELP_DOC in declared


class TestTheFloor:
    def test_a_small_library_never_fires(self) -> None:
        """Every title reissued on a 199-title library still says nothing.

        Below the floor the share is meaningless, and the direction is the tolerable one. This
        guard withholds a scan, so a wrong fire on a small library costs its whole run.
        """
        small = identity_churn._APPLIES_ABOVE - 1
        assert identity_churn.wholesale_change(_ledger(small), _bound(small, changed=small)) is None

    def test_the_smallest_event_it_can_report(self) -> None:
        """At the floor, the share needs 20 titles. An ordinary rate predicts 0.2."""
        floor = identity_churn._APPLIES_ABOVE
        allowed = int(floor * identity_churn.WHOLESALE_SHARE) - 1
        assert (
            identity_churn.wholesale_change(_ledger(floor), _bound(floor, changed=allowed)) is None
        )
        assert (
            identity_churn.wholesale_change(_ledger(floor), _bound(floor, changed=allowed + 1))
            is not None
        )
