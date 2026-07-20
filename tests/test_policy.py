# SPDX-License-Identifier: AGPL-3.0-or-later
"""Policy: the hash, the floors, and the things that cannot be spelled.

An approval is bound to a policy hash. So the hash must change when the *meaning*
changes and must not change when it does not -- otherwise approvals either void
themselves at random, or (far worse) silently survive an edit the human never saw.
"""

from __future__ import annotations

import itertools
from collections.abc import Sequence

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from pydantic import ValidationError

from reaper.engine.gates import Facts, GateId
from reaper.engine.observation import Absent, Known
from reaper.engine.policy import (
    DEFAULT_MOVIE_POLICY,
    DEFAULT_TV_POLICY,
    GateSetting,
    PolicyBody,
    ProfileSettings,
    RatingRuleSpec,
    SignalSetting,
    inspect,
    rebalance,
)
from reaper.engine.signals import Score, SignalConfig, SignalId, score
from reaper.engine.verdict import decide_verdict
from reaper.ratings import RatingSource


def _policy(**overrides: object) -> PolicyBody:
    base = {
        "media_type": "movie",
        "condemn_at": 70,
        "gates": (GateSetting(gate=GateId.WHITELISTED),),
        # Removal weights total exactly 100 (PolicyBody._weights_total_one_hundred), so a
        # single-signal policy carries the whole budget.
        "signals": (SignalSetting(signal=SignalId.UNWATCHED, weight=100, saturate_at=730),),
    }
    return PolicyBody(**{**base, **overrides})  # type: ignore[arg-type]


def _split(unwatched: int, few_watchers: int) -> tuple[SignalSetting, ...]:
    """Two signals sharing the 100-point budget, for tests that vary a weight."""
    return (
        SignalSetting(signal=SignalId.UNWATCHED, weight=unwatched, saturate_at=730),
        SignalSetting(signal=SignalId.FEW_WATCHERS, weight=few_watchers, saturate_at=3),
    )


class TestPopularityWindow:
    """The one place snapshot and backtest read the recent-watchers window from."""

    def test_reads_the_enabled_gates_window(self) -> None:
        body = _policy(
            gates=(
                GateSetting(gate=GateId.WHITELISTED),
                GateSetting(gate=GateId.SERVER_POPULARITY, threshold=2, window_days=30),
            )
        )
        assert body.popularity_window_days() == 30

    def test_a_disabled_gate_must_not_leak_its_window(self) -> None:
        """A stale short window on a switched-off gate would quietly raise FEW_WATCHERS
        pressure across the whole library -- the fact must fall back to the default."""
        body = _policy(
            gates=(
                GateSetting(gate=GateId.WHITELISTED),
                GateSetting(
                    gate=GateId.SERVER_POPULARITY, enabled=False, threshold=2, window_days=30
                ),
            )
        )
        assert body.popularity_window_days() == 365

    def test_no_popularity_gate_falls_back_to_a_year(self) -> None:
        assert _policy().popularity_window_days() == 365


class TestTheHash:
    def test_the_same_policy_hashes_the_same(self) -> None:
        assert _policy().policy_hash() == _policy().policy_hash()

    def test_changing_a_threshold_changes_the_hash(self) -> None:
        """It must. The threshold is what the human approved against."""
        assert _policy(condemn_at=70).policy_hash() != _policy(condemn_at=60).policy_hash()

    def test_changing_a_weight_changes_the_hash(self) -> None:
        """Points move BETWEEN signals rather than in and out of thin air: the total is
        pinned at 100, so a weight edit is always a reallocation."""
        a = _policy(signals=_split(60, 40))
        b = _policy(signals=_split(70, 30))
        assert a.policy_hash() != b.policy_hash()

    def test_disabling_a_gate_changes_the_hash(self) -> None:
        a = _policy(gates=(GateSetting(gate=GateId.WHITELISTED, enabled=True),))
        b = _policy(gates=(GateSetting(gate=GateId.WHITELISTED, enabled=False),))
        assert a.policy_hash() != b.policy_hash()

    def test_the_canonical_json_is_byte_stable(self) -> None:
        """Sorted keys, tight separators, integers only. The same policy must
        produce the same bytes on any machine and any Python."""
        assert _policy().canonical_json() == _policy().canonical_json()
        assert " " not in _policy().canonical_json()

    def test_the_body_contains_no_floats(self) -> None:
        """Floats do not canonicalise: 0.1 + 0.2 != 0.3, and json.dumps of a float
        is platform-dependent. A hash over a float is not a hash."""
        import json

        def has_float(node: object) -> bool:
            if isinstance(node, float):
                return True
            if isinstance(node, dict):
                return any(has_float(v) for v in node.values())
            if isinstance(node, list):
                return any(has_float(v) for v in node)
            return False

        assert not has_float(json.loads(DEFAULT_MOVIE_POLICY.canonical_json()))

    @given(condemn_at=st.integers(min_value=1, max_value=100))
    @settings(max_examples=50)
    def test_every_distinct_policy_hashes_distinctly(self, condemn_at: int) -> None:
        assert (
            _policy(condemn_at=condemn_at).policy_hash()
            == _policy(condemn_at=condemn_at).policy_hash()
        )


class TestEvidenceHash:
    """The evidence hash gates the zero-scan replay: it stays the same for edits a frozen-
    facts replay reproduces exactly, and changes for edits that alter what the scan gathers."""

    def test_a_weight_edit_keeps_the_evidence_hash(self) -> None:
        a = _policy(signals=_split(50, 50))
        b = _policy(signals=_split(80, 20))
        assert a.scoring_hash() != b.scoring_hash()  # scoring behavior moved
        assert a.evidence_hash() == b.evidence_hash()  # ...but the evidence is the same -> replay

    def test_a_rating_bar_edit_keeps_the_evidence_hash(self) -> None:
        a = _policy(
            keep_rating_rules=(RatingRuleSpec(source=RatingSource.IMDB, floor=75, min_votes=1000),)
        )
        b = _policy(
            keep_rating_rules=(RatingRuleSpec(source=RatingSource.IMDB, floor=70, min_votes=1000),)
        )
        assert a.scoring_hash() != b.scoring_hash()
        assert a.evidence_hash() == b.evidence_hash()

    def test_changing_the_popularity_window_changes_the_evidence_hash(self) -> None:
        # The window re-buckets the frozen watcher counts, so the frozen Facts are stale.
        a = _policy(
            gates=(GateSetting(gate=GateId.SERVER_POPULARITY, threshold=3, window_days=365),)
        )
        b = _policy(
            gates=(GateSetting(gate=GateId.SERVER_POPULARITY, threshold=3, window_days=90),)
        )
        assert a.evidence_hash() != b.evidence_hash()

    def test_changing_keep_tags_changes_the_evidence_hash(self) -> None:
        # The whitelist is re-synced from the *arr at scan time, so a new keep-tag needs a scan.
        a = _policy(keep_tags=("reaper-keep",))
        b = _policy(keep_tags=("keep-this",))
        assert a.evidence_hash() != b.evidence_hash()

    def test_changing_a_season_rule_changes_the_evidence_hash(self) -> None:
        # keep_last_seasons recomputes the frozen season-pruning guard, so it needs a scan.
        a = _policy(media_type="tv", keep_last_seasons=2)
        b = _policy(media_type="tv", keep_last_seasons=4)
        assert a.evidence_hash() != b.evidence_hash()


class TestFloorsThatCannotBeZero:
    """0 never means 'disabled' and blank never means 'unlimited'.

    Both idioms are how a half-finished config becomes an unbounded deletion.
    Janitorr's `movie-expiration: {100: 10d}` was read by its author as 'when 100%
    full' and by the code as 'always' -- and it deleted half a library.
    """

    def test_a_vote_floor_of_zero_is_refused(self) -> None:
        """A rating bar without a vote floor protects an 8.3 drawn from a few
        hundred votes -- a number that means nothing at all. IMDb counts votes, so a
        vote floor is required on it."""
        with pytest.raises(ValidationError, match="vote floor of 0"):
            RatingRuleSpec(source=RatingSource.IMDB, floor=75, min_votes=0)

    def test_a_vote_floor_on_a_percentage_source_is_refused(self) -> None:
        """Rotten Tomatoes is a percentage with no vote count, so a vote floor on it would
        silently do nothing -- refuse it rather than let the owner set a dead number."""
        with pytest.raises(ValidationError, match="no vote count"):
            RatingRuleSpec(source=RatingSource.ROTTEN_TOMATOES_CRITIC, floor=75, min_votes=500)

    def test_a_rating_floor_out_of_range_is_refused(self) -> None:
        """A percentage above 100, or an IMDb floor above 10, cannot even be spelled."""
        with pytest.raises(ValidationError):
            RatingRuleSpec(source=RatingSource.IMDB, floor=150, min_votes=1000)

    def test_a_watcher_floor_of_zero_is_refused(self) -> None:
        """It would protect every item on the server -- which looks safe, until the
        owner wonders why Reaper never finds anything and disables the gate."""
        with pytest.raises(ValidationError, match="protect your whole library"):
            GateSetting(gate=GateId.SERVER_POPULARITY, threshold=0)

    def test_a_disabled_gate_skips_the_floors(self) -> None:
        """Turning a protection OFF is legitimate and explicit. It is spelling
        'off' as a zero threshold that is banned."""
        assert GateSetting(gate=GateId.RATING_FLOOR, enabled=False, secondary=0)

    def test_condemn_at_zero_is_refused(self) -> None:
        """A threshold of 0 condemns everything the gates do not save."""
        with pytest.raises(ValidationError):
            _policy(condemn_at=0)

    def test_an_all_zero_weight_policy_is_refused(self) -> None:
        """Nothing would ever score above 0, so nothing would ever be a candidate.
        Caught by the budget rule now: 0 is not 100."""
        with pytest.raises(ValidationError, match="add up to 0 points"):
            _policy(signals=(SignalSetting(signal=SignalId.UNWATCHED, weight=0, saturate_at=730),))

    def test_weights_that_do_not_total_one_hundred_are_refused(self) -> None:
        """The rule that makes a weight mean points. Both directions fail: under-allocating
        stretches the lane exactly as over-allocating shrinks it."""
        with pytest.raises(ValidationError, match="Take 20 away"):
            _policy(signals=_split(70, 50))
        with pytest.raises(ValidationError, match="Give out the other 30"):
            _policy(signals=_split(50, 20))

    def test_both_shipped_defaults_already_balance(self) -> None:
        """The reason this change moves no score: the policies operators start on are
        already at exactly 100, so pinning the total is a relabeling, not a migration."""
        for body in (DEFAULT_MOVIE_POLICY, DEFAULT_TV_POLICY):
            total = sum(s.weight for s in body.signals) + sum(c.weight for c in body.custom_condemn)
            assert total == 100

    def test_a_duplicate_gate_is_refused(self) -> None:
        """Otherwise the second silently wins and the UI shows the first."""
        with pytest.raises(ValidationError, match="configured twice"):
            _policy(
                gates=(
                    GateSetting(gate=GateId.WHITELISTED),
                    GateSetting(gate=GateId.WHITELISTED, enabled=False),
                )
            )

    def test_a_signal_floor_above_saturation_is_refused(self) -> None:
        with pytest.raises(ValidationError, match="must be below saturate_at"):
            SignalSetting(signal=SignalId.UNWATCHED, weight=10, saturate_at=10, floor=20)


class TestCaps:
    """Four caps, not two. The rolling BYTE cap is what makes a multi-terabyte
    incident arithmetically unreachable: no sequence of runs can exceed it."""

    def test_defaults_are_conservative(self) -> None:
        settings_ = ProfileSettings()
        assert settings_.max_items_per_run == 10
        assert settings_.max_bytes_per_run == 500_000_000_000
        assert settings_.require_approval is True

    def test_a_run_cap_larger_than_the_rolling_cap_is_refused(self) -> None:
        """Otherwise the rolling cap is decorative."""
        with pytest.raises(ValidationError, match="rolling cap would be meaningless"):
            ProfileSettings(max_items_per_run=500, max_items_per_30d=100)

    def test_a_run_byte_cap_larger_than_the_rolling_byte_cap_is_refused(self) -> None:
        with pytest.raises(ValidationError, match="entire 30-day budget"):
            ProfileSettings(
                max_bytes_per_run=3_000_000_000_000,
                max_bytes_per_30d=2_000_000_000_000,
            )

    def test_a_grace_period_under_a_week_is_refused(self) -> None:
        """Shorter than a week is one your users cannot realistically act on."""
        with pytest.raises(ValidationError):
            ProfileSettings(grace_days=1)

    def test_caps_cannot_be_unlimited(self) -> None:
        """There is no way to spell 'no limit'. That is the point."""
        with pytest.raises(ValidationError):
            ProfileSettings(max_items_per_run=0)


class TestDefaultPolicy:
    def test_the_shipped_default_is_valid(self) -> None:
        assert DEFAULT_MOVIE_POLICY.policy_hash()

    def test_the_shipped_default_protects_before_it_condemns(self) -> None:
        """Every protection the owner asked for is on by default."""
        enabled = {g.gate for g in DEFAULT_MOVIE_POLICY.gates if g.enabled}

        assert GateId.WHITELISTED in enabled
        assert GateId.STREAMING_NOW in enabled
        assert GateId.RATING_FLOOR in enabled
        assert GateId.SERVER_POPULARITY in enabled
        assert GateId.DATA_HORIZON in enabled
        assert GateId.UNMANAGED in enabled

    def test_the_default_rating_gate_has_a_real_vote_floor(self) -> None:
        # The gate is enabled, and its one default bar is IMDb 7.5 from 1,000 votes.
        assert any(g.gate is GateId.RATING_FLOOR and g.enabled for g in DEFAULT_MOVIE_POLICY.gates)
        bars = DEFAULT_MOVIE_POLICY.keep_rating_rules
        assert len(bars) == 1
        assert bars[0].source is RatingSource.IMDB
        assert bars[0].floor == 75  # 7.5, in tenths
        assert bars[0].min_votes == 1000


class TestTheDangerousConfigDetector:
    """Validation refuses what is PROVABLY wrong. This catches what is merely
    PROBABLY wrong -- and no validator can tell them apart, because the values are
    legal either way."""

    def test_a_very_high_imdb_bar_is_flagged(self) -> None:
        """A user thinking in Rotten Tomatoes types 96 as an IMDb bar. That is a legal
        IMDb floor (9.6) which protects almost nothing, and it is indistinguishable, to a
        validator, from someone who genuinely wants 9.6. So we say so."""
        body = _policy(
            gates=(GateSetting(gate=GateId.RATING_FLOOR),),
            keep_rating_rules=(RatingRuleSpec(source=RatingSource.IMDB, floor=96, min_votes=1000),),
        )

        warnings = inspect(body, ProfileSettings())

        assert any("protect almost nothing" in w.message for w in warnings)

    def test_a_floor_typed_in_whole_points_is_flagged(self) -> None:
        """Typing 7 meaning 7.0 gives a floor of 0.7, which protects everything."""
        body = _policy(
            gates=(GateSetting(gate=GateId.RATING_FLOOR),),
            keep_rating_rules=(RatingRuleSpec(source=RatingSource.IMDB, floor=7, min_votes=1000),),
        )

        warnings = inspect(body, ProfileSettings())

        assert any("Did you mean 7.0" in w.message for w in warnings)

    def test_disabling_the_streaming_gate_is_dangerous(self) -> None:
        body = _policy(gates=(GateSetting(gate=GateId.STREAMING_NOW, enabled=False),))

        warnings = inspect(body, ProfileSettings())

        assert any(w.severity == "danger" and "watching it" in w.message for w in warnings)

    def test_disabling_the_data_horizon_gate_is_dangerous(self) -> None:
        """The #1 mass-deletion vector: Tautulli cannot import pre-install history,
        so everything watched before it looks never-watched."""
        body = _policy(gates=(GateSetting(gate=GateId.DATA_HORIZON, enabled=False),))

        warnings = inspect(body, ProfileSettings())

        assert any(w.severity == "danger" for w in warnings)

    def test_a_very_low_threshold_is_dangerous(self) -> None:
        warnings = inspect(_policy(condemn_at=20), ProfileSettings())

        assert any(w.field == "condemn_at" and w.severity == "danger" for w in warnings)

    def test_unattended_deletion_is_always_flagged(self) -> None:
        warnings = inspect(_policy(), ProfileSettings(require_approval=False))

        assert any(w.field == "require_approval" and w.severity == "danger" for w in warnings)

    def test_the_shipped_default_raises_no_warnings(self) -> None:
        """A user who changes nothing should see a clean policy."""
        assert inspect(DEFAULT_MOVIE_POLICY, ProfileSettings()) == []


class TestRequestedOnlyScopeWithoutSeerr:
    """ "Requested only" needs Seerr to tell a requested show from an unrequested one.

    With no Seerr, ``season_scan._keep_last_applies`` never gets a Known answer and
    falls back to protecting, so the floor quietly covers every show: the setting reads
    narrower than it behaves. The outcome is safe, which is exactly why nothing else
    surfaces it.
    """

    def _tv(self, **overrides: object) -> PolicyBody:
        base = {"media_type": "tv", "keep_last_seasons": 2, "keep_last_scope": "requested"}
        return _policy(**{**base, **overrides})

    def test_it_is_flagged_when_no_requests_app_is_connected(self) -> None:
        warnings = inspect(self._tv(), ProfileSettings(), requests_app_configured=False)

        flagged = [w for w in warnings if w.field == "keep_last_scope"]
        assert len(flagged) == 1
        assert flagged[0].severity == "warn"
        assert "Seerr" in flagged[0].message

    def test_it_is_silent_when_seerr_is_connected(self) -> None:
        """The scope does what it says, so there is nothing to report."""
        warnings = inspect(self._tv(), ProfileSettings(), requests_app_configured=True)

        assert not [w for w in warnings if w.field == "keep_last_scope"]

    def test_it_is_silent_under_the_all_shows_scope(self) -> None:
        """ "All shows" reads nothing about requests, so Seerr's absence changes nothing."""
        warnings = inspect(
            self._tv(keep_last_scope="all"), ProfileSettings(), requests_app_configured=False
        )

        assert not [w for w in warnings if w.field == "keep_last_scope"]

    def test_it_is_silent_when_the_floor_is_off(self) -> None:
        """At 0 seasons the floor never fires, so its scope decides nothing and saying
        the scope is being ignored would be noise about a setting that does nothing."""
        warnings = inspect(
            self._tv(keep_last_seasons=0), ProfileSettings(), requests_app_configured=False
        )

        assert not [w for w in warnings if w.field == "keep_last_scope"]

    def test_a_movie_policy_is_never_flagged(self) -> None:
        """Movies have no seasons; the keep-last floor is a TV notion."""
        warnings = inspect(
            _policy(keep_last_scope="requested", keep_last_seasons=2),
            ProfileSettings(),
            requests_app_configured=False,
        )

        assert not [w for w in warnings if w.field == "keep_last_scope"]

    def test_a_caller_that_cannot_tell_stays_quiet(self) -> None:
        """The default assumes a requests app exists. Telling an operator to connect
        something they already have is worse than silence, and the warning gates
        nothing destructive."""
        assert not [
            w for w in inspect(self._tv(), ProfileSettings()) if w.field == "keep_last_scope"
        ]


#: The signal shapes the rescale tests draw weights over, in a fixed order. Five of them, so
#: the drawn COUNT varies: drift depends on how many rules share the 100 points, which one
#: four-signal fixture cannot see. ``season_rank`` and ``size`` floor above their lowest
#: possible value so every signal here can be driven to zero pressure as well as to full.
_RESCALE_SHAPES: tuple[tuple[str, int, int], ...] = (
    ("unwatched", 1825, 365),
    ("few_watchers", 3, 0),
    ("season_rank", 6, 1),
    ("low_rating", 60, 0),
    ("size", 20, 1),
)


def _over_budget(weights: Sequence[int]) -> dict:
    """A legacy body: the first ``len(weights)`` signal shapes, totalling anything at all."""
    return {
        "media_type": "tv",
        "condemn_at": 70,
        "gates": [],
        "signals": [
            {"signal": name, "weight": w, "saturate_at": sat, "floor": floor}
            for w, (name, sat, floor) in zip(weights, _RESCALE_SHAPES, strict=False)
        ],
    }


def _signal_configs(body: dict) -> list[SignalConfig]:
    """The same translation ``services.snapshot`` and ``api.routes`` do, so these tests
    score through the real scorer rather than a transcription of it (rule 22)."""
    return [
        SignalConfig(
            signal=SignalId(s["signal"]),
            weight=s["weight"],
            saturate_at=s["saturate_at"],
            floor=s["floor"],
        )
        for s in body["signals"]
    ]


def _rounding_slack(before_body: dict, repaired: dict) -> float:
    """The most the rounding can move a score: the total weight it handed *upward*.

    ``score' - score = Σ (w'ᵢ - wᵢ·100/T)·fillᵢ`` with every ``fill`` in ``[0, 1]``, and the
    deltas sum to zero, so the drift cannot exceed their positive half. That is reached
    whenever the rules that gained weight are the ones carrying pressure and the rules that
    lost it are not, which is an ordinary shape, not a contrived one.
    """
    weights = [s["weight"] for s in before_body["signals"]]
    exact = [w * 100 / sum(weights) for w in weights]
    return sum(max(0.0, s["weight"] - e) for s, e in zip(repaired["signals"], exact, strict=True))


def _evidence(days: float, watchers: int, rank: int, rating: int, size_gb: float) -> Facts:
    return Facts(
        title="x",
        days_observed_unwatched=Known(value=days, source="t"),
        distinct_watchers=Known(value=watchers, source="t"),
        distinct_watchers_all_time=Known(value=watchers, source="t"),
        size_bytes=Known(value=int(size_gb * 1_000_000_000), source="r"),
        imdb_rating_tenths=Known(value=rating, source="i"),
        imdb_votes=Known(value=50_000, source="i"),
        season_rank=Known(value=rank, source="s"),
        is_streaming_now=Known(value=False, source="t"),
        is_managed=Known(value=True, source="r"),
        in_curated_list=Absent(source="l"),
        is_whitelisted=Known(value=False, source="l"),
        others_watching=Known(value=0, source="t"),
    )


class TestRebalancingAnOldPolicy:
    """Policies written before removal weights had to total 100 are rescaled rather than
    discarded. The exact rescale cannot move a score; integer rounding can, by a point or
    two, which is why a rescaled body is flagged and reviewed instead of adopted silently.
    These pin how far it can move and what that can do to a verdict."""

    @given(
        weights=st.lists(
            st.integers(min_value=1, max_value=200),
            min_size=1,
            max_size=len(_RESCALE_SHAPES),
        )
    )
    @settings(max_examples=200)
    def test_the_rescale_spends_all_hundred_points_and_keeps_the_order(
        self, weights: list[int]
    ) -> None:
        """Whatever the count, the repaired body totals exactly 100 and ranks the rules the
        same way. Reordering an operator's priorities would be a bigger betrayal than the
        rounding: it would say they meant something they never said."""
        repaired = rebalance(_over_budget(weights))
        assert repaired is not None

        after = [s["weight"] for s in repaired["signals"]]
        assert sum(after) == 100
        for i, j in itertools.combinations(range(len(weights)), 2):
            if weights[i] > weights[j]:
                assert after[i] >= after[j]

    @given(
        weights=st.lists(
            st.integers(min_value=1, max_value=200),
            min_size=1,
            max_size=len(_RESCALE_SHAPES),
        ),
        condemn_at=st.integers(min_value=1, max_value=100),
        days=st.floats(min_value=0, max_value=6000),
        watchers=st.integers(min_value=0, max_value=6),
        rank=st.integers(min_value=1, max_value=10),
        rating=st.integers(min_value=0, max_value=100),
        size_gb=st.floats(min_value=0, max_value=40),
    )
    @settings(max_examples=400, deadline=None)
    def test_the_rescale_moves_no_verdict_that_was_not_already_at_the_line(
        self,
        weights: list[int],
        condemn_at: int,
        days: float,
        watchers: int,
        rank: int,
        rating: int,
        size_gb: float,
    ) -> None:
        """What actually matters is the decision, so this asserts on ``decide_verdict``.

        The rescale can only ever change one by nudging a score across the condemn line, and
        only from within ``_rounding_slack`` of it. That slack is not under a point: it grows
        with the number of rules sharing the 100 points, which is why the count is drawn.
        Coverage is held out (floor 0, every fact Known) so the score is the only thing under
        test.
        """
        before_body = _over_budget(weights)
        repaired = rebalance(before_body)
        assert repaired is not None

        facts = _evidence(days, watchers, rank, rating, size_gb)
        before = score(_signal_configs(before_body), facts)
        after = score(_signal_configs(repaired), facts)

        def verdict(s: Score) -> str:
            return decide_verdict(
                protected=False,
                blocked=False,
                score=round(s.value),
                coverage_bp=round(s.coverage * 10_000),
                condemn_at=condemn_at,
                coverage_floor_bp=0,
            )

        slack = _rounding_slack(before_body, repaired)
        assert abs(before.value - after.value) <= slack + 1e-9
        if verdict(before) != verdict(after):
            # Plus the half point `round` can add on either side of the line.
            assert abs(before.value - condemn_at) <= slack + 0.5, (
                f"{before.value} -> {after.value} crossed {condemn_at} from outside the slack"
            )

    def test_the_rounding_can_move_a_score_a_full_point_and_flip_a_verdict(self) -> None:
        """The counterexample to the old ``abs(before - after) < 1.0`` tolerance, kept as a
        test so nobody restores the claim. Largest-remainder bounds each weight's error, not
        the score's: here the two rules that gained a point are the two carrying all the
        pressure, and the two that lost one carry none.

        Nothing exotic is needed. This is why a rescaled body is flagged ``repaired``
        (``services.profiles.ActivePolicy``), degrades the scan, and opens in the editor as
        an unsaved draft rather than being adopted as the operator's own.
        """
        legacy = _over_budget([1, 1, 1, 5])
        repaired = rebalance(legacy)
        assert repaired is not None
        assert [s["weight"] for s in repaired["signals"]] == [13, 13, 12, 62]
        assert _rounding_slack(legacy, repaired) == 1.0

        # Long unwatched and nobody watching: full pressure. Newest season on disk and a
        # rating above the bar: none.
        facts = _evidence(days=5000, watchers=0, rank=1, rating=60, size_gb=8)
        before = score(_signal_configs(legacy), facts).value
        after = score(_signal_configs(repaired), facts).value
        assert before == 25.0
        assert after == 26.0

        def verdict(value: float) -> str:
            return decide_verdict(
                protected=False,
                blocked=False,
                score=round(value),
                coverage_bp=10_000,
                condemn_at=26,
                coverage_floor_bp=0,
            )

        assert verdict(before) == "abstain"
        assert verdict(after) == "condemn"

    def test_a_body_broken_for_any_other_reason_is_not_repaired(self) -> None:
        """Rescaling fixes the budget and nothing else. Returning a 'repaired' body we do
        not understand would put invented values in front of an operator as their own."""
        assert rebalance({"signals": [{"signal": "unwatched", "weight": 0}]}) is None
        assert rebalance({"condemn_at": "not a number"}) is None
        assert rebalance({}) is None

    @pytest.mark.parametrize("raw", [[], ["signals"], 42, "a string", None, True])
    def test_a_body_that_is_not_an_object_returns_none_rather_than_raising(
        self, raw: object
    ) -> None:
        """``services.profiles.active_policy`` must not raise on anything a hand-edited or
        truncated row can hold, and it leans on this returning ``None``. Valid JSON that is
        not an object used to reach ``body.get`` and raise ``AttributeError``."""
        assert rebalance(raw) is None
