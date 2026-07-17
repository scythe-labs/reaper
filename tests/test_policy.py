# SPDX-License-Identifier: AGPL-3.0-or-later
"""Policy: the hash, the floors, and the things that cannot be spelled.

An approval is bound to a policy hash. So the hash must change when the *meaning*
changes and must not change when it does not -- otherwise approvals either void
themselves at random, or (far worse) silently survive an edit the human never saw.
"""

from __future__ import annotations

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from pydantic import ValidationError

from reaper.engine.gates import GateId
from reaper.engine.policy import (
    DEFAULT_MOVIE_POLICY,
    GateSetting,
    PolicyBody,
    ProfileSettings,
    RatingRuleSpec,
    SignalSetting,
    inspect,
)
from reaper.engine.signals import SignalId
from reaper.ratings import RatingSource


def _policy(**overrides: object) -> PolicyBody:
    base = {
        "media_type": "movie",
        "condemn_at": 70,
        "gates": (GateSetting(gate=GateId.WHITELISTED),),
        "signals": (SignalSetting(signal=SignalId.UNWATCHED, weight=50, saturate_at=730),),
    }
    return PolicyBody(**{**base, **overrides})  # type: ignore[arg-type]


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
        a = _policy(signals=(SignalSetting(signal=SignalId.UNWATCHED, weight=50, saturate_at=730),))
        b = _policy(signals=(SignalSetting(signal=SignalId.UNWATCHED, weight=40, saturate_at=730),))
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
        a = _policy(signals=(SignalSetting(signal=SignalId.UNWATCHED, weight=50, saturate_at=730),))
        b = _policy(signals=(SignalSetting(signal=SignalId.UNWATCHED, weight=80, saturate_at=730),))
        assert a.scoring_hash() != b.scoring_hash()  # scoring behaviour moved
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
        with pytest.raises(ValidationError, match="every item would score 0"):
            _policy(signals=(SignalSetting(signal=SignalId.UNWATCHED, weight=0, saturate_at=730),))

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
