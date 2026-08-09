# SPDX-License-Identifier: AGPL-3.0-or-later
"""The frozen season bundle survives a round trip through JSON, exactly.

``services.season_evidence`` is what stands between the policy simulator and a confident
wrong preview of a season rule: the scan freezes ``plan_series_prune``'s inputs and the
replay thaws them and re-derives the plan. Every one of those inputs is evidence, so a value
that changes shape on the way through -- an int key arriving as a string, a ``None`` arriving
as a zero, a three-state collapsing to two -- is a number on the operator's screen that no
scan will produce.

``test_scan_pipeline.py`` proves the whole path against two real scans, which is the stronger
claim. This file pins the codec directly, because that test can only see a round-trip loss
that happens to change a plan on its fixture, and the fields most likely to be lost are the
ones a fixture is least likely to exercise.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from reaper.clients.sonarr_stats import SeasonStats
from reaper.clock import utcnow
from reaper.engine.policy import DEFAULT_TV_POLICY
from reaper.services import season_evidence

AT = datetime(2026, 7, 24, 12, 0, tzinfo=UTC)


def _seasons() -> tuple[SeasonStats, ...]:
    return (
        # Specials, and a season Sonarr reported without a size. `None` is not zero: no
        # season worth deleting is genuinely empty, and `_reported_size` maps an unreported
        # size here precisely so nothing downstream reads a measurement nobody took.
        SeasonStats(
            season_number=0,
            monitored=False,
            episode_file_count=3,
            size_on_disk=None,
            total_episode_count=3,
            wanted_episode_count=0,
        ),
        SeasonStats(
            season_number=1,
            monitored=True,
            episode_file_count=5,
            size_on_disk=1_000_000_000,
            total_episode_count=10,
            wanted_episode_count=10,
        ),
    )


def _bundle(**edits: object) -> season_evidence.SeasonPruneInput:
    """One show's evidence, with every awkward value a real library produces."""
    base: dict[str, object] = {
        "series_title": "A show, with a comma and an accent: é",
        "seasons": _seasons(),
        "airing_seasons": (1,),
        # A viewer part-way through a season, and one whose position in a season is unknown.
        "progress_by_user": {"7": {1: 3, 0: None}},
        # A viewer whose last-watched time could not be read keeps their hold, so `None` here
        # is load-bearing rather than absent data.
        "last_watched_by_user": {"7": AT, "9": None},
        "last_play_by_user": {"7": {1: AT, 0: None}},
        "season_final_episode": {0: 3, 1: None},
        "episodes_unreadable": False,
        # `None` is "on disk, but never resolved in Plex" -- not a measured zero (rule 93).
        "watchers_by_season": {0: None, 1: 2},
        "shortfall_by_season": {0: None, 1: "the mirror does not reach back that far"},
        "progress_unreadable": True,
        "progress_seasons_unmatched": False,
        "progress_unknown_reason": None,
        "requested_known_false": True,
        "reach_days": 2000,
        "now": AT,
    }
    return season_evidence.SeasonPruneInput(**{**base, **edits})  # type: ignore[arg-type]


def _round_trip(inp: season_evidence.SeasonPruneInput) -> season_evidence.SeasonPruneInput:
    """Through real JSON, not just the two dict functions.

    ``to_dict`` is only half the boundary: the payload is stored as text, and a dict with
    integer keys survives ``from_dict(to_dict(x))`` while `json.dumps` turns those keys into
    strings. Testing the pair alone would pass over exactly that.
    """
    return season_evidence.from_dict(json.loads(json.dumps(season_evidence.to_dict(inp))))


class TestTheBundleSurvivesTheFreeze:
    def test_every_field_comes_back_identical(self) -> None:
        inp = _bundle()
        assert _round_trip(inp) == inp

    def test_a_scan_that_never_read_the_episode_lists_stays_three_state(self) -> None:
        """The one field where two-state would be a preview of an answer nobody gathered.

        ``None`` means the mid-binge fan-out never ran; ``{}`` means it ran and the show has
        no episodes on disk. The planner reads both as "protect whole seasons", so nothing
        downstream can tell them apart once they are conflated -- which is why the refusal
        is decided off this value rather than off the plan it produces.
        """
        unread = _round_trip(_bundle(season_final_episode=None))
        assert unread.season_final_episode is None

        asked_and_empty = _round_trip(_bundle(season_final_episode={}))
        assert asked_and_empty.season_final_episode == {}

    def test_a_read_sonarr_refused_survives_as_its_own_state(self) -> None:
        """The third state, which shares ``None`` with the second and is not it.

        A show whose ``episodes()`` call failed carries no map and still had its plan made
        from the empty one. Losing this bit in the codec would leave the two ``None`` bundles
        indistinguishable, and the whole lane would refuse for whichever answer that collapsed
        to (#500).
        """
        failed = _round_trip(_bundle(season_final_episode=None, episodes_unreadable=True))
        assert failed.season_final_episode is None
        assert failed.episodes_unreadable is True

    def test_the_three_are_not_the_same_answer(self) -> None:
        """And the distinction reaches the decision, not just the dataclass."""
        policy = season_evidence.SeasonPolicy.from_body(
            DEFAULT_TV_POLICY.model_copy(update={"keep_in_progress": True})
        )
        assert season_evidence.missing_episode_map(
            _bundle(season_final_episode=None), policy=policy
        )
        assert not season_evidence.missing_episode_map(
            _bundle(season_final_episode={}), policy=policy
        )
        # Read and refused: the scan planned from the empty map, so replaying off it returns
        # the verdicts the snapshot holds. Refusing here took the whole TV lane down for one
        # show Sonarr would not answer for.
        assert not season_evidence.missing_episode_map(
            _bundle(season_final_episode=None, episodes_unreadable=True), policy=policy
        )

    def test_a_refused_read_plans_exactly_as_the_scan_did(self) -> None:
        """The property the refusal was traded for, stated against the planner.

        The claim is not that the empty map is as good as a real one. It is that the scan
        made this show's stored plan from the empty map, so a replay off the same bundle
        reproduces that plan rather than guessing at one.
        """
        policy = season_evidence.SeasonPolicy.from_body(
            DEFAULT_TV_POLICY.model_copy(update={"keep_in_progress": True})
        )
        refused = _bundle(season_final_episode=None, episodes_unreadable=True)

        assert season_evidence.plan_from_frozen(
            _round_trip(refused), policy=policy
        ) == season_evidence.plan_from_frozen(
            _bundle(season_final_episode={}, episodes_unreadable=True), policy=policy
        )

    def test_a_thawed_bundle_plans_the_same_way_as_the_live_one(self) -> None:
        """The property the simulator actually depends on, stated directly.

        Equality of the dataclass is necessary and not sufficient: what has to hold is that
        the planner cannot tell the two apart. Season numbers arrive as JSON strings and are
        coerced back, and a coercion that silently reordered or dropped one would produce a
        different plan while comparing equal on a field nobody checks.
        """
        inp = _bundle()
        policy = season_evidence.SeasonPolicy.from_body(DEFAULT_TV_POLICY)

        live = season_evidence.plan_from_frozen(inp, policy=policy)
        thawed = season_evidence.plan_from_frozen(_round_trip(inp), policy=policy)

        assert thawed == live
        # Not a plan that protects everything by accident, which two identical empty answers
        # would also satisfy (rule 118).
        assert live.protected, "the fixture produced no protected season, so this compares little"

    def test_a_payload_missing_a_field_raises_rather_than_defaulting(self) -> None:
        """No safe default exists here: every member is evidence, so absence is a refusal.

        The route catches this and declines to preview (``simulate._SeasonReplay``). Defaulting
        even one member would let a partial bundle produce a confident plan.
        """
        payload = season_evidence.to_dict(_bundle())
        del payload["finals"]

        with pytest.raises(KeyError):
            season_evidence.from_dict(payload)

    def test_a_payload_with_no_scan_instant_raises(self) -> None:
        """The mid-binge expiry compares against it, so a bundle without one cannot decide."""
        payload = season_evidence.to_dict(_bundle())
        payload["at"] = None

        with pytest.raises(ValueError, match="no scan instant"):
            season_evidence.from_dict(payload)


class TestTheReplayExpiresAgainstTheScansOwnClock:
    """The frozen ``now`` decides the mid-binge expiry, not whenever the editor was opened.

    ``SeasonPruneInput.now`` exists for this and says so, and nothing tested it: replacing
    ``now=inp.now`` with a live ``utcnow()`` inside :func:`plan_from_frozen` passed the whole
    suite, the two-real-scan exactness sweep included. That sweep freezes its clock ten days
    from the wall, and ten days moves no viewer across a 180-day hold, so the one fixture that
    could have caught it was shaped not to.

    What it would cost is the feature's own claim: a viewer inside the hold when the scan ran
    and outside it by the time the Policy page is opened keeps the season in the review queue
    and loses it on the panel, which is a preview no scan will reproduce.

    Its own seasons rather than the file's, because every season there reaches a guard that
    is checked BEFORE the mid-binge one -- season 0 is specials and season 1 is incomplete and
    airing -- so neither can ever show this arm.
    """

    #: Far enough back that the drift dwarfs any hold: the viewer is ten days into a 180-day
    #: hold at the scan instant and ten years past it against the wall. Taken from the wall
    #: clock rather than written down, so the gap cannot shrink as the date moves.
    LONG_AGO_DAYS = 3650

    @staticmethod
    def _plain_seasons() -> tuple[SeasonStats, ...]:
        """Three ordinary seasons: complete, monitored, none of them specials."""
        return tuple(
            SeasonStats(
                season_number=n,
                monitored=True,
                episode_file_count=5,
                size_on_disk=1_000_000_000,
                total_episode_count=5,
                wanted_episode_count=5,
            )
            for n in (1, 2, 3)
        )

    def _scanned_long_ago(self) -> season_evidence.SeasonPruneInput:
        scanned_at = utcnow() - timedelta(days=self.LONG_AGO_DAYS)
        watched = scanned_at - timedelta(days=10)
        return _bundle(
            seasons=self._plain_seasons(),
            airing_seasons=(),
            now=scanned_at,
            # Three episodes into season 2 of five, ten days before the scan.
            progress_by_user={"7": {2: 3}},
            last_watched_by_user={"7": watched},
            last_play_by_user={"7": {2: watched}},
            season_final_episode={1: 5, 2: 5, 3: 5},
            watchers_by_season={1: 1, 2: 1, 3: 1},
            shortfall_by_season={1: None, 2: None, 3: None},
            progress_unreadable=False,
        )

    @staticmethod
    def _policy(**edits: object) -> season_evidence.SeasonPolicy:
        """Every other season guard switched off, so only the mid-binge one can hold."""
        return season_evidence.SeasonPolicy.from_body(
            DEFAULT_TV_POLICY.model_copy(
                update={
                    "keep_last_seasons": 0,
                    "keep_first_season": False,
                    "protect_incomplete_seasons": False,
                    **edits,
                }
            )
        )

    def test_a_viewer_inside_the_hold_when_the_scan_ran_still_holds_their_season(self) -> None:
        plan = season_evidence.plan_from_frozen(self._scanned_long_ago(), policy=self._policy())

        held = {p.season_number: p.reason for p in plan.protected}
        assert held.get(2) == "a viewer is part-way through the show", (
            "season 2 lost its mid-binge hold, so the expiry was measured against something "
            f"other than the bundle's own scan instant. protected={held}"
        )

    def test_the_same_viewer_expires_once_the_hold_is_shorter_than_their_gap(self) -> None:
        """The negative control, on the same bundle and the same clock.

        Without it the test above passes on a plan that holds season 2 for some other reason,
        which is how a guard proves nothing (rule 118). Nine days of hold sits under the ten
        the viewer has been idle AT THE SCAN INSTANT, so the hold expires on its own terms and
        the comparison is demonstrably live.
        """
        plan = season_evidence.plan_from_frozen(
            self._scanned_long_ago(), policy=self._policy(in_progress_hold_days=9)
        )

        assert 2 in plan.prunable, (
            "season 2 stayed protected with the hold set under the viewer's own idle gap, so "
            "the assertion above is not reading the mid-binge guard at all"
        )
