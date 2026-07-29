# SPDX-License-Identifier: AGPL-3.0-or-later
"""Policy: the hash, the floors, and the things that cannot be spelled.

An approval is bound to a policy hash. So the hash must change when the *meaning*
changes and must not change when it does not -- otherwise approvals either void
themselves at random, or (far worse) silently survive an edit the human never saw.
"""

from __future__ import annotations

import itertools
import json
from collections.abc import Sequence
from dataclasses import replace

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from pydantic import ValidationError

from reaper.api.schemas import GateSettingIn
from reaper.engine import policy as policy_module
from reaper.engine.fields import RECENT_WATCHERS, Op, ReachSpan
from reaper.engine.gates import (
    POLICY_AUTHORABLE_GATES,
    Facts,
    GateConfig,
    GateId,
    ServerPopularityGate,
    history_shortfall,
    progress_is_establishable,
)
from reaper.engine.observation import Absent, Known
from reaper.engine.policy import (
    DEFAULT_MOVIE_POLICY,
    DEFAULT_TV_POLICY,
    SCHEMA_VERSION,
    SCORER_VERSION,
    BooleanCondemnSpec,
    ConditionSpec,
    GateSetting,
    GradedCondemnSpec,
    GradedKeepSpec,
    PolicyBody,
    PolicyWarning,
    ProfileSettings,
    RatingRuleSpec,
    SignalSetting,
    inspect,
    rebalance,
    recover_rating_rules,
)
from reaper.engine.signals import Score, SignalConfig, SignalId, score
from reaper.engine.verdict import decide_verdict
from reaper.ratings import RatingSource
from reaper.services.scan_runner import GATE_TYPES, build_gates

#: Every gate ``build_gates`` can construct from a policy row. RATING_FLOOR is not in
#: ``GATE_TYPES`` because it takes a set of per-source bars rather than one GateConfig, so
#: ``build_gates`` builds it explicitly -- it is buildable, just not by lookup.
_BUILDABLE_GATES = set(GATE_TYPES) | {GateId.RATING_FLOOR}

#: Ids the ENGINE emits as gate results without any policy row behind them: the season guard
#: comes from the season judgment, CUSTOM tags an operator-authored rule's result. ``GATE_TYPES``
#: has no entry for either, so ``build_gates`` refuses them exactly as it refuses a retired id,
#: and ``GateSettingIn`` refuses to save one.
_ENGINE_ONLY_GATES = {GateId.SEASON_PROGRESSION, GateId.CUSTOM}


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


class TestSimulatorHashesCoverOnlyBehavior:
    """Neither simulator hash may move for a field that cannot change an answer.

    A field that is pure bookkeeping, folded into these hashes, does not merely cost one
    extra scan: it makes the simulator refuse *permanently*, because the scan records the
    stored body's hash while the route computes the round-tripped one. ``schema_version``
    did exactly that on any install whose policy predated a version bump.
    """

    def test_a_stored_schema_version_does_not_disable_the_simulator(self) -> None:
        old = _policy(schema_version=SCHEMA_VERSION - 1)
        current = _policy(schema_version=SCHEMA_VERSION)
        assert old.scoring_hash() == current.scoring_hash()
        assert old.evidence_hash() == current.evidence_hash()
        # It is still part of the body an approval is bound to (rule 113).
        assert old.policy_hash() != current.policy_hash()

    def test_a_scorer_bump_invalidates_stored_scores_but_replays_exactly(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The one version field that IS behavior: a new scorer means the stored scores are
        not comparable (scoring_hash must move), but a replay runs the new scorer over the
        frozen Facts, so the evidence is still good (evidence_hash must not).

        Driven by bumping the CONSTANT rather than by building two bodies that disagree
        about it, because ``_pin_to_the_running_scorer`` makes the second impossible: the
        field tracks the running code, so a body claiming the superseded scorer cannot
        exist. Bumping the constant is also the thing that really happens.
        """
        before_scoring = _policy().scoring_hash()
        before_evidence = _policy().evidence_hash()

        monkeypatch.setattr(policy_module, "SCORER_VERSION", SCORER_VERSION + 1)

        assert _policy().scoring_hash() != before_scoring
        assert _policy().evidence_hash() == before_evidence

    def test_a_stored_scorer_pin_does_not_outlive_the_scorer(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """What binds an approval is ``policy_hash``, and it has to be able to MOVE when the
        scorer does. The field took its value from the stored JSON, so a row written under
        scorer N still read back as N after the constant was bumped: the hash was
        byte-identical either side, ``live_policy_hash`` still matched a plan approved under
        the superseded scorer, and the executor deleted on its numbers with no re-scan
        refusal (rule 113). Two snapshots scored by different scorers hashed the same too.
        """
        stored = _policy().model_dump(mode="json")
        assert stored["scorer_version"] == SCORER_VERSION
        approved_under = PolicyBody.model_validate(stored).policy_hash()

        monkeypatch.setattr(policy_module, "SCORER_VERSION", SCORER_VERSION + 1)

        # The same stored row, read by the newer code. It must not hash to the approval.
        reloaded = PolicyBody.model_validate(stored)
        assert reloaded.scorer_version == SCORER_VERSION + 1
        assert reloaded.policy_hash() != approved_under

    def test_the_pin_holds_however_the_body_was_built(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A pin that held for a body loaded from the database and not for one built in code
        would be the half-fix, and it is the easy one to write: a frozen model's top-level
        "after" validator is silently ignored when it returns a copy rather than mutating
        through ``object.__setattr__``, and only on the ``__init__`` path."""
        stored = _policy().model_dump(mode="json")
        monkeypatch.setattr(policy_module, "SCORER_VERSION", SCORER_VERSION + 1)

        assert PolicyBody.model_validate(stored).scorer_version == SCORER_VERSION + 1
        assert PolicyBody(**stored).scorer_version == SCORER_VERSION + 1
        assert (
            PolicyBody.model_validate_json(json.dumps(stored)).scorer_version == SCORER_VERSION + 1
        )

    def test_a_body_from_a_newer_reaper_is_still_refused(self) -> None:
        """The pin overwrites what the row said, so the bound that refuses a body this code
        cannot interpret has to keep running first, not be papered over by it."""
        stored = _policy().model_dump(mode="json")
        stored["scorer_version"] = SCORER_VERSION + 1

        with pytest.raises(ValidationError, match="less than or equal"):
            PolicyBody.model_validate(stored)

    def test_every_body_field_is_classified(self) -> None:
        """A new field lands in one of the three sets, or in the evidence-bearing list here.

        The allow-list makes "unclassified" mean "needs a fresh scan", which is the safe
        default for evidence but the wrong one for bookkeeping. This fails when a field is
        added so the author has to decide which it is, rather than discovering it on a live
        server the way ``schema_version`` was found. (``name`` is deliberately absent: a
        policy's name lives on the row, not in the hashed body.)
        """
        known = (
            PolicyBody._POST_SCORE_FIELDS
            | PolicyBody._EVIDENCE_REPLAYABLE_FIELDS
            | PolicyBody._NON_BEHAVIORAL_FIELDS
        )
        # Everything else is evidence-bearing: it changes what a scan gathers.
        evidence_bearing = {
            "media_type",
            "keep_last_seasons",
            "keep_first_season",
            "keep_last_scope",
            "season_lookahead",
            "keep_in_progress",
            "in_progress_hold_days",
            "keep_specials",
            "protect_incomplete_seasons",
            "flag_keep_conflicts",
            "gates",
            "keep_tags",
            "keep_tags_match",
        }
        actual = set(DEFAULT_TV_POLICY.model_dump(mode="json"))
        assert actual == known | evidence_bearing, (
            "A PolicyBody field is unclassified. Decide whether it changes an answer "
            "(leave it out of _NON_BEHAVIORAL_FIELDS) or is bookkeeping (add it), then "
            "list it here."
        )


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
        assert settings_.caps_enabled is True

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
        assert GateId.CURATED_LIST in enabled
        assert GateId.MIN_DORMANCY in enabled

    def test_no_shipped_protection_is_one_that_cannot_fire(self) -> None:
        """Rule 38/117, as a standing check rather than a one-off. Every gate a default
        policy carries must be one ``build_gates`` can actually construct, or the operator is
        shown a switch that does nothing (and, since ``build_gates`` refuses an unknown gate
        rather than skipping it, a scan that will not run).

        Deliberately NOT filtered to ``g.enabled``. A disabled unbuildable gate still renders
        a row in the editor, and flipping it on is what takes the scan offline -- so the
        moment it ships in a default body the damage is one click away, not zero clicks.
        """
        for body in (DEFAULT_MOVIE_POLICY, DEFAULT_TV_POLICY):
            shipped = {g.gate for g in body.gates}
            assert shipped <= _BUILDABLE_GATES, f"{body.media_type}: {shipped - _BUILDABLE_GATES}"

    def test_every_unbuildable_gate_id_is_declared_retired(self) -> None:
        """The drift guard rule 103 asks for, and the one that would have caught ``rule 72``
        here. ``RETIRED_GATES`` is a hardcoded set mirroring a schema set: any ``GateId``
        ``build_gates`` cannot construct MUST be listed, or a stored body naming it survives
        load and takes that install's scans offline permanently with no self-heal.

        ``OTHERS_WATCHING`` was exactly that gap -- retired before ``UNMANAGED``, refused by
        ``build_gates``, and missing from the first version of the set.
        """
        unbuildable = set(GateId) - _BUILDABLE_GATES - _ENGINE_ONLY_GATES

        assert unbuildable == PolicyBody.RETIRED_GATES, (
            f"not declared retired: {unbuildable - PolicyBody.RETIRED_GATES}; "
            f"declared retired but buildable: {PolicyBody.RETIRED_GATES - unbuildable}"
        )

    def test_the_save_boundary_allows_exactly_what_the_builder_can_build(self) -> None:
        """Rule 131: the producer and the consumer of this bound read one declaration.

        ``POLICY_AUTHORABLE_GATES`` lives in ``engine.gates`` because ``api.schemas`` is a
        leaf that must not import the scan stack, so it cannot derive the set from
        ``GATE_TYPES`` at runtime. This is what keeps the copy honest: add a gate to
        ``GATE_TYPES`` and forget the authorable list and this fails, rather than the new
        protection quietly becoming unsavable.
        """
        assert POLICY_AUTHORABLE_GATES == _BUILDABLE_GATES, (
            f"buildable but not authorable: {_BUILDABLE_GATES - POLICY_AUTHORABLE_GATES}; "
            f"authorable but not buildable: {POLICY_AUTHORABLE_GATES - _BUILDABLE_GATES}"
        )

    @pytest.mark.parametrize("gate", sorted(set(GateId) - _BUILDABLE_GATES))
    def test_a_gate_no_policy_row_can_build_is_refused_at_the_save_boundary(
        self, gate: GateId
    ) -> None:
        """The hole the retirement shim did not cover. ``GateSettingIn.gate`` was a bare
        ``GateId``, so a hand-crafted save could store a retired id OR an engine-only one
        (``season_progression``, ``custom``); ``build_gates`` then refused it on every
        subsequent scan and the install went offline with no self-heal.

        Covers both classes at once, so a future retirement or a new engine-only id is
        refused here the moment it stops being buildable.
        """
        with pytest.raises(ValidationError) as caught:
            GateSettingIn(gate=gate, enabled=True)

        assert gate.value in str(caught.value)

    @pytest.mark.parametrize("gate", sorted(PolicyBody.RETIRED_GATES))
    def test_a_retired_gate_cannot_take_an_install_offline(self, gate: GateId) -> None:
        """Every retired id, not just the one whose retirement prompted the shim. A stored
        body naming one loads clean and its gates all build, so the scan still runs."""
        stored = DEFAULT_MOVIE_POLICY.model_dump(mode="json")
        stored["gates"] = [{"gate": gate.value, "enabled": True}, *stored["gates"]]

        loaded = PolicyBody.model_validate(stored)

        assert gate not in {g.gate for g in loaded.gates}
        assert loaded.policy_hash() == DEFAULT_MOVIE_POLICY.policy_hash()
        build_gates(loaded)  # would raise ScanConfigError if the drop had not happened

    def test_a_retired_gate_cannot_be_reintroduced_by_hand(self) -> None:
        """Not only the stored path: a body built in code cannot carry one either, so nothing
        can put the dead switch back in front of an operator."""
        revived = DEFAULT_MOVIE_POLICY.model_copy(
            update={"gates": (*DEFAULT_MOVIE_POLICY.gates, GateSetting(gate=GateId.UNMANAGED))}
        )

        assert GateId.UNMANAGED in {g.gate for g in revived.gates}  # model_copy does not validate
        assert GateId.UNMANAGED not in {
            g.gate for g in PolicyBody.model_validate(revived.model_dump()).gates
        }

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

        assert any(w.severity == "danger" and "someone is watching" in w.message for w in warnings)

    def test_disabling_the_data_horizon_gate_is_dangerous(self) -> None:
        """Still a danger, but for the reason this switch actually owns.

        The pre-install-history problem is answered by the dormancy CLAMP in
        ``snapshot.build_facts``, which runs whatever this switch says, so the warning may
        not promise that titles would start looking never-watched. ``DataHorizonGate`` can
        only fail closed on an Unknown dormancy, and ``MinDormancyGate`` does that too, so
        what is lost is one of two checks (rules 7/24, 25).
        """
        body = _policy(gates=(GateSetting(gate=GateId.DATA_HORIZON, enabled=False),))

        warnings = inspect(body, ProfileSettings())

        assert any(w.severity == "danger" for w in warnings)
        message = next(w.message for w in warnings if w.field.endswith("data_horizon.enabled"))
        assert "never-watched" not in message
        assert "could not read" in message

    def test_the_streaming_warning_names_what_the_switch_can_actually_do(self) -> None:
        """The executor's active-stream veto is unconditional: ``_reap_one`` calls
        ``_being_watched_now`` before every real send without consulting the policy gate,
        and ``execute`` refuses a real run with no Plex at all. So turning this gate off
        cannot delete a file mid-play, and the warning must not say it can (rule 7/24).
        What it does is let the title be condemned, listed and approved, then skipped."""
        body = _policy(gates=(GateSetting(gate=GateId.STREAMING_NOW, enabled=False),))

        message = next(
            w.message
            for w in inspect(body, ProfileSettings())
            if w.field.endswith("streaming_now.enabled")
        )

        assert "delete" not in message
        assert "reap list" in message

    def test_a_very_low_threshold_is_dangerous(self) -> None:
        warnings = inspect(_policy(condemn_at=20), ProfileSettings())

        assert any(w.field == "condemn_at" and w.severity == "danger" for w in warnings)

    def test_caps_off_relaxes_the_invariant_and_raises_no_danger(self) -> None:
        """Turning the caps off is a deliberate first-run choice, not a misconfiguration.
        The run-cap-vs-rolling-cap invariant must not fire (it constrains nothing when
        nothing is enforced, so this combination must construct at all), and inspect must
        not flag the profile as dangerous."""
        settings_ = ProfileSettings(caps_enabled=False, max_items_per_run=1000, max_items_per_30d=1)

        assert settings_.caps_enabled is False
        assert not any(w.severity == "danger" for w in inspect(_policy(), settings_))

    def test_the_shipped_default_raises_no_warnings(self) -> None:
        """A user who changes nothing should see a clean policy."""
        assert inspect(DEFAULT_MOVIE_POLICY, ProfileSettings()) == []


class TestAPopularityWindowLongerThanTheWatchHistory:
    """The window in the direction nothing warned about, and the reason it needed one.

    ``gates.ServerPopularityGate.evaluate`` fails closed when the mirror is shorter than
    the window it is asked about: a count over three months cannot answer "who watched
    this in the last year", so the gate blocks. The reach is a property of the operator's
    DATA, not of any one title, so it blocks library-wide for as long as the shortfall
    lasts.

    Most blocks clear on the next scan, which is why nothing was ever obliged to name a
    remedy for one. The ones that do not are all the same family, a mirror shallower than
    the question, and the others are held on the season path. This is the member with a
    control the operator can turn, so it is the one the editor speaks for.
    """

    #: Longer than any reach used here, so a test that does not say otherwise is
    #: exercising the shortfall rather than some other warning.
    WINDOW = 365

    def _pop(self, **overrides: object) -> PolicyBody:
        base = {"gate": GateId.SERVER_POPULARITY, "window_days": self.WINDOW, "threshold": 2}
        return _policy(gates=(GateSetting(**{**base, **overrides}),))  # type: ignore[arg-type]

    def _pop_with_dormancy_floor(self, threshold: int) -> PolicyBody:
        """The same gate beside a dormancy floor, which decides whether the window matters."""
        return _policy(
            gates=(
                GateSetting(gate=GateId.SERVER_POPULARITY, window_days=self.WINDOW, threshold=2),
                GateSetting(gate=GateId.MIN_DORMANCY, threshold=threshold),
            )
        )

    def _owner_rule_only(self, *, op: Op = Op.GTE, **overrides: object) -> PolicyBody:
        """The gate OFF, with the operator's own keep-outright rule on the same count.

        ``build_gates`` hands ``CustomProtectGate`` the window whether the gate is on or
        off, so this rule blocks on exactly the span the gate would have used -- here the
        365-day fallback, which no control on the page shows.

        ``op`` because the operator decides whether the block is total: the field alone
        does not settle it (``_protect_blocks_on_reach``).
        """
        base = {"gate": GateId.SERVER_POPULARITY, "window_days": self.WINDOW, "threshold": 2}
        return _policy(
            gates=(GateSetting(**{**base, "enabled": False, **overrides}),),  # type: ignore[arg-type]
            protect_conditions=(ConditionSpec(field="recent_watchers", op=op, value=1),),
        )

    def _window_warnings(self, body: PolicyBody, reach: float | None) -> list[PolicyWarning]:
        return [
            w
            for w in inspect(body, ProfileSettings(), history_reach_days=reach)
            if w.field == f"gates.{GateId.SERVER_POPULARITY.value}.window_days"
        ]

    def _rule_warnings(self, body: PolicyBody, reach: float | None) -> list[PolicyWarning]:
        """Warnings anchored on the operator's own keep rules, where the gate-off case has
        to speak: with the gate off the window control is not rendered at all."""
        return [
            w
            for w in inspect(body, ProfileSettings(), history_reach_days=reach)
            if w.field == "protect_conditions"
        ]

    def test_it_is_flagged_when_the_window_outruns_the_history(self) -> None:
        flagged = self._window_warnings(self._pop(), reach=90.0)

        assert len(flagged) == 1
        assert flagged[0].severity == "warn"
        assert "Nothing will be flagged for removal" in flagged[0].message

    def test_it_is_silent_when_the_history_covers_the_window(self) -> None:
        """The gate answers the question it is asked, so there is nothing to report."""
        assert self._window_warnings(self._pop(), reach=float(self.WINDOW)) == []
        assert self._window_warnings(self._pop(), reach=800.0) == []

    def test_a_caller_that_cannot_tell_stays_quiet(self) -> None:
        """Same posture as ``requests_app_configured``: a caller that cannot read the
        mirror must not guess, because guessing short tells an operator their window is
        useless when it is fine. The warning gates nothing destructive, so silence costs
        only advice."""
        assert self._window_warnings(self._pop(), reach=None) == []

    def test_the_window_control_stays_silent_while_the_protection_is_off(self) -> None:
        """The editor hides the window control with the gate (``PolicyEditor.tsx``, pinned
        by ``PolicyEditor.test.tsx``), so a warning anchored THERE would name a control that
        is not on the page. Nothing else about the gate-off case is settled by this: the
        span is still in force, and the two tests below are where it is spoken for.
        """
        assert self._window_warnings(self._pop(enabled=False), reach=90.0) == []

    def test_the_fallback_window_is_flagged_where_the_owners_own_rule_blocks_on_it(self) -> None:
        """A disabled gate does NOT mean no reader of a watcher count blocks.

        ``PolicyBody.popularity_window_days`` falls back to 365 with the gate off, and
        ``build_gates`` hands that span to ``CustomProtectGate`` regardless of the switch,
        so the owner's own keep-outright rule fails closed library-wide over a mirror
        shorter than a year. The editor invites exactly this: ``KeepRulesEditor`` only
        hides a field whose gate is ON, so ``recent_watchers`` becomes authorable the
        moment the protection is switched off.

        It has to say so somewhere the operator can act, which is the rule itself: the
        window control is not rendered, so the year in force is unreachable from the page.
        """
        flagged = self._rule_warnings(self._owner_rule_only(), reach=90.0)

        assert len(flagged) == 1
        assert flagged[0].severity == "warn"
        assert "Nothing will be flagged for removal" in flagged[0].message
        # The span they never set, named, and the remedy that does not need the hidden box.
        assert "the last year" in flagged[0].message
        assert "remove that rule" in flagged[0].message
        # And nothing on the window control, which is not on the page to be fixed.
        assert self._window_warnings(self._owner_rule_only(), reach=90.0) == []

    def _two_rules_on_the_same_field(self) -> PolicyBody:
        """Two keep rules on the same count, which nothing refuses.

        ``PolicyRuleEditors``' ``addHard`` appends unconditionally and filters candidate
        fields only by the enabled gate, and ``PolicyBody`` validates the pair -- so this
        is a policy an operator can build in the editor, not a hand-edited row.
        """
        base = {"gate": GateId.SERVER_POPULARITY, "window_days": self.WINDOW, "threshold": 2}
        return _policy(
            gates=(GateSetting(**{**base, "enabled": False}),),  # type: ignore[arg-type]
            protect_conditions=(
                ConditionSpec(field="recent_watchers", op=Op.GTE, value=1),
                ConditionSpec(field="recent_watchers", op=Op.GTE, value=5),
            ),
        )

    def test_the_blocking_rule_is_named_rather_than_left_to_be_guessed(self) -> None:
        """ "Remove that rule" has to say which one, and the old message did not.

        The field is named from the registry the editor renders from, so the operator is
        given the string already on the card in front of them (rule 144). Without it the
        only discriminator was "counts who watched a title in the last year", and the span
        half of that is unreachable: this branch fires precisely when the window control is
        not rendered, so nothing on the page carries the year. The field half was no better
        beside a "People who have ever watched it" rule, which also counts who watched a
        title (issue #157).
        """
        [flagged] = self._rule_warnings(self._owner_rule_only(), reach=90.0)

        assert f'"{RECENT_WATCHERS.label}"' in flagged.message
        # The label is the registry's, not a second spelling of it: the editor renders this
        # exact string through GET /api/vocabulary, so a copy here would drift from the card.
        assert RECENT_WATCHERS.label == "People who watched it recently"

    def test_two_rules_on_one_field_are_counted_and_the_remedy_goes_plural(self) -> None:
        """A singular remedy is factually wrong once a second rule blocks on the same span.

        Removing one of a pair leaves the warning byte-identical while a live protection is
        gone, and nothing tells the operator the pick was wrong. So the count is what makes
        the remedy honest, and it is the count of rules BLOCKING, not of protect conditions:
        the discriminator below adds a rule the shortfall does not block and the message
        stays singular.
        """
        [flagged] = self._rule_warnings(self._two_rules_on_the_same_field(), reach=90.0)

        assert "Your 2 keep rules" in flagged.message
        assert "remove them." in flagged.message
        assert "remove that rule" not in flagged.message

    def test_a_rule_the_shortfall_does_not_block_is_left_out_of_the_count(self) -> None:
        """The count ranges over the rules that are blocking, never over the rule list.

        A ``size_bytes`` rule reads a fact the mirror does not bound and an ``lte`` rule on
        the same count keeps an already-earned outcome (``fields._survives_more_history``),
        so neither is holding anything back. Counting either would tell the operator to
        remove one of two rules when only one of them is the problem -- the same wrong-pick
        failure the naming clause exists to prevent, one step further on.
        """
        base = {"gate": GateId.SERVER_POPULARITY, "window_days": self.WINDOW, "threshold": 2}
        with_bystanders = _policy(
            gates=(GateSetting(**{**base, "enabled": False}),),  # type: ignore[arg-type]
            protect_conditions=(
                ConditionSpec(field="recent_watchers", op=Op.GTE, value=1),
                ConditionSpec(field="recent_watchers", op=Op.LTE, value=9),
                ConditionSpec(field="size_bytes", op=Op.GTE, value=1),
            ),
        )

        [flagged] = self._rule_warnings(with_bystanders, reach=90.0)

        assert "Your keep rule on" in flagged.message
        assert "remove that rule." in flagged.message
        assert "2 keep rules" not in flagged.message

    def test_the_fallback_window_is_silent_with_no_rule_reading_it(self) -> None:
        """The gate off and no owner rule on a watcher count is the ordinary case: the
        fallback governs a span nothing in the PROTECT lane asks about, so nothing blocks
        and there is nothing to report. This is the discriminator for the test above --
        without it, a warning that fired on every gate-off policy would pass it."""
        assert self._rule_warnings(self._pop(enabled=False), reach=90.0) == []
        # A rule on a field the mirror does not bound is silent for the same reason.
        unbounded = _policy(
            gates=(GateSetting(gate=GateId.SERVER_POPULARITY, enabled=False, threshold=2),),
            protect_conditions=(ConditionSpec(field="size_bytes", op=Op.GTE, value=1),),
        )
        assert self._rule_warnings(unbounded, reach=90.0) == []

    def test_a_keep_rule_the_shortfall_does_not_block_outright_is_not_claimed(self) -> None:
        """ "Nothing will be flagged" is a claim about EVERY item, and only ``gte`` earns it.

        A truncated watcher count is a lower bound, so ``fields._survives_more_history``
        blocks only the outcomes more history could overturn. Under ``gte`` those are the
        unmatched ones, and the matched ones fire a PROTECT, so every item is kept or
        blocked and nothing is condemned. Under ``lte`` it inverts: an item already OVER
        the bar is an outcome no amount of history changes, so it comes back a plain
        checked ABSTAIN and is scored and condemned like any other.

        Driven rather than reasoned -- a ``recent_watchers lte 2`` rule against a 90-day
        mirror and the 365-day fallback returns ABSTAIN unblocked for a 5-watcher item, so
        the list is not empty. Saying it was would be false in the reassuring direction,
        and the remedy that rides with it ("remove that rule") would strip the protection
        off the items that ARE blocked.
        """
        assert self._rule_warnings(self._owner_rule_only(op=Op.LTE), reach=90.0) == []
        # The discriminator: the same rule one operator over is claimed, so this is the op
        # being read and not the branch having gone quiet.
        assert self._rule_warnings(self._owner_rule_only(op=Op.GTE), reach=90.0) != []

    def test_the_dormancy_floor_silences_the_owners_rule_too(self) -> None:
        """The gate-off arm needs the same guard as the gate-on one, for the same reason.

        Below the floor every item is kept on age alone (``MinDormancyGate`` PROTECTs and
        PROTECT beats blocked), so the window decides nothing and neither does a keep rule
        measured over it. Without this the warning would fire for every operator on the
        shipped 1095-day floor holding under a year of history, telling them to remove a
        keep rule that is changing no verdict -- the regression the gate-on twin above
        exists to prevent, one branch over (rule 118).
        """
        floored = self._owner_rule_only()
        floored = floored.model_copy(
            update={
                "gates": (*floored.gates, GateSetting(gate=GateId.MIN_DORMANCY, threshold=1095))
            }
        )

        assert self._rule_warnings(floored, reach=90.0) == []

    def test_the_dormancy_floor_silences_it_while_it_alone_empties_the_list(self) -> None:
        """The remedy has to be able to work, and under the floor it cannot.

        ``MinDormancyGate`` PROTECTs anything younger than its threshold and
        ``decide_verdict`` puts PROTECT ahead of blocked, while dormancy is clamped to the
        mirror (``dormancy.reference_instant``). So below the floor every item is kept on
        age alone and the popularity window decides nothing: telling the operator to lower
        it would shorten a real keep protection for no effect.

        On both shipped policies the two ranges are disjoint -- floor 1095, window 365 --
        so this is every operator holding under a year of history, which is exactly the
        install the warning was written for.
        """
        with_floor = self._pop_with_dormancy_floor(1095)

        assert self._window_warnings(with_floor, reach=90.0) == []
        assert self._window_warnings(with_floor, reach=float(self.WINDOW) - 1) == []
        # Lower the floor beneath the reach and the window is the binding constraint again.
        assert self._window_warnings(self._pop_with_dormancy_floor(30), reach=90.0) != []

    def test_the_span_is_named_before_the_clause_that_points_at_it(self) -> None:
        """``history_shortfall``'s in-margin arm is "your watch history does not go back
        that far", which only reads if the span has already been said. Both other tests
        here sit outside ``_REACH_NAMEABLE_MARGIN_DAYS`` and take the arm that names a
        number, so this is the branch that would go out ungrammatical unnoticed."""
        message = self._window_warnings(self._pop(), reach=float(self.WINDOW) - 15)[0].message

        assert message.index("in the last year") < message.index("does not go back that far")

    def test_the_cause_clause_is_the_one_the_why_panel_prints(self) -> None:
        """Rule 144: this sentence has a sibling. ``ServerPopularityGate.evaluate`` puts
        the same shortfall in front of the same operator on every blocked row, off the
        same ``gates.history_shortfall`` helper. Restating it here in different words
        would let the editor and the why panel describe one mirror two ways.

        If this fails, the two copies have drifted: fix them together, in
        ``engine/policy.py:inspect`` and ``engine/gates.py:ServerPopularityGate.evaluate``.
        """
        reach = 90.0
        expected = history_shortfall(Known(value=reach, source="tautulli"), float(self.WINDOW))
        assert expected is not None  # the fixture is a genuine shortfall

        message = self._window_warnings(self._pop(), reach=reach)[0].message
        blocked = ServerPopularityGate(
            GateConfig(gate=GateId.SERVER_POPULARITY, threshold=2, window_days=self.WINDOW)
        ).evaluate(
            replace(
                _evidence(days=900, watchers=0, rank=1, rating=70, size_gb=1),
                history_reach_days=Known(value=reach, source="t"),
            )
        )

        assert blocked.blocked is True  # the state the warning is describing
        assert expected in message
        assert expected in blocked.detail

    def test_a_window_shorter_than_the_history_keeps_its_own_warning(self) -> None:
        """The opposite end of the same field, which had no test at all. A very short
        window is legal and reads as watched-by-nobody across the library, so it warns
        whatever the mirror reaches."""
        flagged = self._window_warnings(self._pop(window_days=7), reach=800.0)

        assert len(flagged) == 1
        assert "very short" in flagged[0].message

    def test_the_two_ends_merge_into_one_message_instead_of_opposing_each_other(self) -> None:
        """A 7-day window under a 3-day mirror is short AND outrun, and the two remedies
        genuinely oppose: one end says a year is usual, the other said to lower the window
        to match the history. Stacked on one control they told the operator to raise and to
        lower the same number in adjacent sentences, with nothing saying which applied.

        So the shortfall speaks for the control alone and carries the other end's fault in
        its remedy clause. This names both messages rather than counting severities: a
        count cannot tell two warnings from two copies of one (rule 118).
        """
        flagged = self._window_warnings(self._pop(window_days=7), reach=3.0)

        assert len(flagged) == 1
        assert flagged[0].severity == "warn"
        message = flagged[0].message
        # The shortfall is the one that survives -- it names the live outcome, and the
        # short-window advice describes pressure that cannot land while nothing is flagged.
        assert "Nothing will be flagged for removal" in message
        assert "A 7-day watch window is very short" not in message
        # Its remedy no longer points the way the other end pushes back on.
        assert "Lower this window" not in message
        assert "a shorter window would leave almost nothing counted as watched" in message

    def test_each_end_keeps_its_own_message_where_the_other_does_not_hold(self) -> None:
        """The merge above is only for the overlap. Apart, each fault is real on its own
        and says the thing it always said, remedy included -- which is what makes the
        merged message discriminable from either of them."""
        outrun_only = self._window_warnings(self._pop(), reach=90.0)[0].message
        assert "Lower this window to match your history" in outrun_only
        assert "very short" not in outrun_only

        short_only = self._window_warnings(self._pop(window_days=7), reach=800.0)[0].message
        assert "A 7-day watch window is very short" in short_only
        assert "A year is the usual setting." in short_only
        assert "Nothing will be flagged for removal" not in short_only


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
        # Deeper than the popularity window, so FEW_WATCHERS scores the count instead of
        # reporting it unreadable. These tests are about weight arithmetic, and a signal
        # withheld for a short mirror would move every number in them.
        history_reach_days=Known(value=4000.0, source="t"),
        days_since_added=Known(value=800.0, source="p"),
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


def _legacy_rating_body(**gate: object) -> dict[str, object]:
    """A stored body from before the rating bar moved off the RATING_FLOOR gate row.

    The shape that matters: no ``keep_rating_rules`` key at all, and the bar still on the
    gate as ``threshold`` (tenths) + ``secondary`` (minimum votes).
    """
    raw = _policy(
        gates=(GateSetting(gate=GateId.RATING_FLOOR),),
        keep_rating_rules=(RatingRuleSpec(source=RatingSource.IMDB, floor=75, min_votes=1000),),
    ).model_dump(mode="json")
    del raw["keep_rating_rules"]
    raw["gates"] = [{**raw["gates"][0], "threshold": 75, "secondary": 1000, **gate}]
    return raw


class TestRestoringALostRatingBar:
    """The rating bar moved off the gate row into ``keep_rating_rules`` with no backfill.

    A body written before that move still VALIDATES -- the gate keeps its now-meaningless
    numbers and the new field defaults to empty -- and an empty rule set makes the gate
    abstain on every item. So the operator's bar silently protects nothing, on a healthy,
    executable snapshot. These pin what is restored and, just as importantly, what is not.
    """

    def test_the_bar_comes_back_as_an_imdb_rule(self) -> None:
        restored = recover_rating_rules(_legacy_rating_body())

        assert restored is not None
        assert restored["keep_rating_rules"] == [
            {"source": RatingSource.IMDB.value, "floor": 75, "min_votes": 1000}
        ]
        # It loads, and the restored bar is what the gate will actually read.
        body = PolicyBody.model_validate(restored)
        assert body.rating_rules()[0].floor == 75
        assert body.rating_rules()[0].min_votes == 1000

    def test_the_restored_body_is_stamped_with_the_current_schema(self) -> None:
        """So a body that has been through the editor since can be told apart, and this
        shim can eventually retire."""
        restored = recover_rating_rules(_legacy_rating_body())

        assert restored is not None
        assert restored["schema_version"] == SCHEMA_VERSION

    def test_an_explicitly_empty_rule_list_is_left_alone(self) -> None:
        """Rule 1: omitted is not the same as explicitly empty. An operator who cleared
        their bars must keep an empty set, or a protection they removed comes back."""
        raw = _legacy_rating_body()
        raw["keep_rating_rules"] = []

        assert recover_rating_rules(raw) is None

    def test_a_disabled_gate_is_left_alone(self) -> None:
        """Nothing was protecting anything either way, so there is nothing to restore -- and
        no reason to degrade a scan over it."""
        assert recover_rating_rules(_legacy_rating_body(enabled=False)) is None

    @pytest.mark.parametrize(
        "gate",
        [
            {"threshold": 0, "secondary": 1000},
            {"threshold": 101, "secondary": 1000},
            {"threshold": 75, "secondary": 0},
            {"threshold": True, "secondary": 1000},
            {"threshold": 75, "secondary": True},
            {"threshold": "75", "secondary": 1000},
            {"threshold": None, "secondary": None},
        ],
        ids=[
            "no-floor",
            "floor-over-100",
            "no-vote-floor",
            "floor-is-a-bool",
            "votes-is-a-bool",
            "floor-is-a-string",
            "both-missing",
        ],
    )
    def test_numbers_the_old_validator_would_have_refused_are_not_restored(
        self, gate: dict[str, object]
    ) -> None:
        """Only a bar the old gate would have accepted is put back. Anything else would be
        inventing a protection value on the operator's behalf, and ``RatingRuleSpec`` would
        refuse it anyway -- ``bool`` is an ``int`` subclass, so ``true`` must not read as 1."""
        assert recover_rating_rules(_legacy_rating_body(**gate)) is None

    def test_a_body_with_no_rating_gate_at_all_is_left_alone(self) -> None:
        raw = _policy().model_dump(mode="json")
        del raw["keep_rating_rules"]

        assert recover_rating_rules(raw) is None

    @pytest.mark.parametrize("raw", [[], 42, "a string", None, True, {}, {"gates": "text"}])
    def test_anything_unreadable_returns_none_rather_than_raising(self, raw: object) -> None:
        """``services.profiles.active_policy`` must not raise on anything a hand-edited row
        can hold: it keeps the one page that fixes a broken policy reachable."""
        assert recover_rating_rules(raw) is None


class TestEveryReachSpanIsRoutedByName:
    """A new ``ReachSpan`` member has to be handled at four sites, and this is the alarm.

    Two of them ROUTE, and both now match member by member behind ``assert_never``:
    ``fields.reach_shortfall`` picks which bound the mirror is measured against, and
    ``inspect``'s lean loop files a graded keep under the span that blocks it. mypy fails
    on those first, so this test is for the author who skips that gate -- and for its own
    failure message, which is the one place all four sites are written down.

    The other two are membership tests, one per warning: an owner protect rule on the
    window (``owner_protect_on_window``) and the all-time rule below it. Each carries copy
    written for its own span, so neither can be generated from the set. A third member
    there is silence rather than a wrong answer, which is why they are named here instead
    of guarded in code.

    Rule 103's drift guard, and it was absent. Issue #168's probe measured what that cost:
    a third member added locally took ``reach_shortfall``'s ``else``, answered
    ITEM_LIFETIME's question instead of its own, and returned "no shortfall" for an item
    younger than the mirror against a window the mirror does not span -- the permissive
    direction, from the helper whose whole job is withholding unsupported counts. Nothing
    in the suite went red.
    """

    def test_the_two_spans_every_consumer_handles_are_the_two_that_exist(self) -> None:
        assert set(ReachSpan) == {ReachSpan.POPULARITY_WINDOW, ReachSpan.ITEM_LIFETIME}, (
            "A ReachSpan member was added or removed. Four sites decide what a span means "
            "and none of them can infer it: fields.reach_shortfall (which bound the mirror "
            "is measured against), policy.inspect's lean loop (which span a graded keep's "
            "discount is charged to), and inspect's two protect membership tests "
            "(owner_protect_on_window, and the ITEM_LIFETIME branch under it), each of "
            "which needs operator copy written for that span. Handle all four, then update "
            "this test."
        )


class TestTheOtherReachShortfallLanes:
    """The two readers that could empty the list with nothing on the page saying why.

    ``_protect_blocks_on_reach`` answers with a SPAN, because the registry carries two and
    only one of them had a warning. The operator test is span-agnostic
    (``fields._survives_more_history`` reads the op alone), so scoping the detector to the
    popularity window was never the registry speaking. ``watchers_all_time`` carries the
    other span, is PROTECT-only, and blocks through ``gates.lifetime_shortfall`` for every
    item the mirror does not reach back to the arrival of.

    The lean lane is the other one, and it does not block at all: ``signals.evaluate_keep``
    grants the FULL ``max_discount`` on a shortfall with no earned-outcome test, and
    ``score()`` is ``max(0, base - keep_discount)`` over a base bounded by ``MAX_SCORE``, so
    keeps worth more than the headroom hold every affected item under the threshold as
    provably as a block does. That is summed per SPAN, not per rule: each blocked keep takes
    its full discount and ``score()`` subtracts the sum, so two keeps of 20 against a headroom
    of 30 empty the list exactly as one keep of 40 does. The pre-existing ``graded_keeps``
    warning fires on the keeps TOTALLING at least ``condemn_at`` -- 70 against a headroom of
    30 on shipped values -- so a keep at 40 sat in a dead zone that warned about nothing
    (rule 140).
    """

    def _all_time_rule(self, *, op: Op = Op.GTE) -> PolicyBody:
        return _policy(
            protect_conditions=(ConditionSpec(field="watchers_all_time", op=op, value=1),)
        )

    def _lean(self, field: str, discount: int) -> PolicyBody:
        return _policy(
            graded_keeps=(
                GradedKeepSpec(
                    name="my lean", field=field, max_discount=discount, floor=0, saturate_at=10
                ),
            )
        )

    def _warnings_on(
        self, body: PolicyBody, anchor: str, reach: float | None
    ) -> list[PolicyWarning]:
        return [
            w
            for w in inspect(body, ProfileSettings(), history_reach_days=reach)
            if w.field == anchor
        ]

    # --- the all-time protect rule ------------------------------------------------

    def test_an_all_time_keep_rule_is_flagged(self) -> None:
        flagged = self._warnings_on(self._all_time_rule(), "protect_conditions", reach=90.0)

        assert len(flagged) == 1
        # `warn`, not `danger`: the outcome is that Reaper deletes nothing, the keep direction.
        assert flagged[0].severity == "warn"

    def test_it_names_the_set_rather_than_claiming_an_empty_list(self) -> None:
        """The span this lane needs is the ITEM's age, which ``inspect`` is never handed.

        So the affected set is "everything added before the history starts" and its size is
        not knowable here. Claiming "nothing will be flagged" would be false in the
        reassuring direction for a young library the mirror covers outright, which is the
        direction rule 144 says a rounded claim always fails in.
        """
        [flagged] = self._warnings_on(self._all_time_rule(), "protect_conditions", reach=90.0)

        assert flagged.message.startswith("Titles added before your watch history starts")
        assert "Nothing will be flagged" not in flagged.message

    def test_the_op_decides_it_here_exactly_as_it_does_on_the_window(self) -> None:
        """``lte`` leaves an item already over the bar settled, so it stays condemnable.

        Same asymmetry ``_survives_more_history`` applies to the window, reached through the
        same predicate -- which is the point of answering with the span rather than a bool.
        """
        assert (
            self._warnings_on(self._all_time_rule(op=Op.LTE), "protect_conditions", reach=90.0)
            == []
        )
        # The discriminator: the same rule one operator over IS claimed, so this is the op
        # being read and not the branch having gone quiet.
        assert self._warnings_on(self._all_time_rule(), "protect_conditions", reach=90.0) != []

    def test_a_caller_that_cannot_read_the_mirror_stays_quiet(self) -> None:
        assert self._warnings_on(self._all_time_rule(), "protect_conditions", reach=None) == []

    def test_the_dormancy_floor_silences_it(self) -> None:
        """Under the floor every item is kept on age alone, so this rule decides nothing.

        Same guard the window lane uses, for the same reason: the remedy would move no
        verdict, and firing anyway would tell every operator with a shallow mirror to delete
        a protection that is not what is holding their list back.
        """
        body = _policy(
            gates=(GateSetting(gate=GateId.MIN_DORMANCY, threshold=1095),),
            protect_conditions=(ConditionSpec(field="watchers_all_time", op=Op.GTE, value=1),),
        )
        assert self._warnings_on(body, "protect_conditions", reach=90.0) == []

    # --- the lean lane ------------------------------------------------------------

    def test_a_lean_bigger_than_the_headroom_is_flagged(self) -> None:
        """40 points against a headroom of 30, which the old total-based warning let past."""
        flagged = self._warnings_on(self._lean("recent_watchers", 40), "graded_keeps", reach=90.0)

        assert len(flagged) == 1
        assert flagged[0].severity == "warn"
        # It names the rule, which the protect lanes cannot: a graded keep carries a name the
        # operator typed, a `ConditionSpec` does not.
        assert '"my lean"' in flagged[0].message

    def test_a_lean_inside_the_headroom_is_not(self) -> None:
        """The bound is ``MAX_SCORE - condemn_at``: at 30 the item can still reach 70."""
        assert (
            self._warnings_on(self._lean("recent_watchers", 30), "graded_keeps", reach=90.0) == []
        )
        # One point either side of the bound, so this pins the boundary rather than the
        # branch merely being reachable.
        assert (
            self._warnings_on(self._lean("recent_watchers", 31), "graded_keeps", reach=90.0) != []
        )

    def test_a_lean_on_a_field_the_mirror_does_not_bound_is_not_flagged(self) -> None:
        """Size carries no ``reach_span``, so no amount of missing history changes it.

        40 rather than something larger on purpose: it clears the headroom (30) so the check
        under test is reached, while staying under ``condemn_at`` (70) so the pre-existing
        total-based warning on this same anchor cannot answer for it.
        """
        assert self._warnings_on(self._lean("size_bytes", 40), "graded_keeps", reach=90.0) == []

    def test_the_lean_lane_covers_both_spans(self) -> None:
        """Both reach-bounded fields are offered as leans: ``leanFields`` is not lane-filtered
        and ``GradedKeepSpec`` accepts any numeric field, protect-only ones included."""
        windowed = self._warnings_on(self._lean("recent_watchers", 40), "graded_keeps", reach=90.0)
        lifetime = self._warnings_on(
            self._lean("watchers_all_time", 40), "graded_keeps", reach=90.0
        )

        assert len(windowed) == 1
        assert len(lifetime) == 1
        # Each says what is true of ITS span: the window empties the list outright, the
        # lifetime one only for titles older than the mirror.
        assert windowed[0].message.startswith("Nothing will be flagged for removal.")
        assert lifetime[0].message.startswith("Titles added before your watch history starts")

    def test_a_windowed_lean_the_history_covers_is_not_flagged(self) -> None:
        """No shortfall, no full discount, so nothing to say."""
        assert (
            self._warnings_on(self._lean("recent_watchers", 40), "graded_keeps", reach=400.0) == []
        )

    def test_it_says_which_number_to_change_and_the_number_is_the_bound(self) -> None:
        """The remedy names the headroom, derived from the same expression the check uses,
        so a policy on a different threshold cannot be told to set a figure that still fires."""
        body = _policy(
            condemn_at=80,
            graded_keeps=(
                GradedKeepSpec(
                    name="my lean",
                    field="recent_watchers",
                    max_discount=40,
                    floor=0,
                    saturate_at=10,
                ),
            ),
        )
        [flagged] = self._warnings_on(body, "graded_keeps", reach=90.0)

        # MAX_SCORE - condemn_at == 20 here, not the 30 the shipped threshold gives, so a
        # transcribed constant would fail this (rule 141). The message used to restate the
        # threshold in a third sentence too; that sentence said again what the lead already
        # says and was cut, so the derived number is the only thing left to pin -- which is
        # the half that could actually be wrong.
        assert "20 points or less" in flagged.message

    def test_a_caller_that_cannot_read_the_mirror_stays_quiet_on_the_lean_lane(self) -> None:
        """The lean twin of the protect-lane guard above (rules 118, 72).

        Both arms were pinned on the protect lane and neither was pinned here: neutering the
        guard passed all 119 tests.

        **The lifetime span is the arm that actually needs it, so it is the one that must be
        driven.** A window keep is held back a second time by ``window_short``, which is None
        with no mirror, so a window-only case passes with the guard gone and reads as a proof
        it is not (rule 118). A lifetime keep has no second test: without the guard it warns
        that titles older than the history are held, while nothing knows how far the history
        reaches.
        """
        for field in ("recent_watchers", "watchers_all_time"):
            assert self._warnings_on(self._lean(field, 40), "graded_keeps", None) == []

    def test_the_dormancy_floor_silences_the_lean_lane_too(self) -> None:
        """The other arm, same reason as every other lane: under the floor every item is kept
        on age alone, so lowering the keep would move no verdict.

        Both spans for the reason given above: only the lifetime one discriminates here.
        """
        for field in ("recent_watchers", "watchers_all_time"):
            body = _policy(
                gates=(GateSetting(gate=GateId.MIN_DORMANCY, threshold=1095),),
                graded_keeps=(
                    GradedKeepSpec(
                        name="my lean", field=field, max_discount=40, floor=0, saturate_at=10
                    ),
                ),
            )
            assert self._warnings_on(body, "graded_keeps", reach=90.0) == []

    def test_the_message_names_the_operators_window_not_the_fallback(self) -> None:
        """Every lean test above runs on a policy with no ``SERVER_POPULARITY`` row, so the
        window is the 365-day fallback in all of them and a hardcoded ``humanize_window(365)``
        passed the suite. 180 is not the default, so only the wiring can produce it (rule 141).
        """
        body = _policy(
            gates=(GateSetting(gate=GateId.SERVER_POPULARITY, threshold=2, window_days=180),),
            graded_keeps=(
                GradedKeepSpec(
                    name="my lean",
                    field="recent_watchers",
                    max_discount=40,
                    floor=0,
                    saturate_at=10,
                ),
            ),
        )
        [flagged] = self._warnings_on(body, "graded_keeps", reach=90.0)

        assert "6 months" in flagged.message
        assert "year" not in flagged.message

    # --- the total is per span, never per rule ------------------------------------

    def test_two_keeps_each_inside_the_headroom_still_empty_the_list(self) -> None:
        """The dead zone one arity up, and the reason the check is summed.

        ``evaluate_keep`` grants each blocked keep its FULL ``max_discount`` and ``score()``
        subtracts the sum, so two keeps of 20 against a headroom of 30 hold every item under
        70 exactly as one keep of 40 does. Tested per rule, both sat under the bar and the
        page said nothing -- while the pre-existing total-based warning needs 70.
        """
        body = _policy(
            graded_keeps=(
                GradedKeepSpec(
                    name="one", field="recent_watchers", max_discount=20, floor=0, saturate_at=10
                ),
                GradedKeepSpec(
                    name="two", field="recent_watchers", max_discount=20, floor=0, saturate_at=10
                ),
            )
        )
        [flagged] = self._warnings_on(body, "graded_keeps", reach=90.0)

        assert flagged.message.startswith("Nothing will be flagged for removal.")
        # Both contributors are named: the operator cannot act on a total alone, because
        # neither rule looks wrong on its own.
        assert '"one" and "two"' in flagged.message
        assert "all 40 of their points" in flagged.message
        assert "set their total to 30 points or less" in flagged.message

    def test_a_window_keep_and_a_lifetime_keep_name_the_affected_set(self) -> None:
        """Mixed spans do NOT claim an empty list, and that asymmetry is deliberate.

        A window shortfall is a property of the operator's data, so it reaches every item; a
        lifetime shortfall is a property of each item's age, and ``inspect`` is handed one
        reach and never a list of arrival dates. So 20 + 20 crossing the headroom is only
        provable for titles older than the mirror, and claiming the whole library would be
        false in the reassuring direction for a young one (rule 144).
        """
        body = _policy(
            graded_keeps=(
                GradedKeepSpec(
                    name="recent", field="recent_watchers", max_discount=20, floor=0, saturate_at=10
                ),
                GradedKeepSpec(
                    name="ever", field="watchers_all_time", max_discount=20, floor=0, saturate_at=10
                ),
            )
        )
        [flagged] = self._warnings_on(body, "graded_keeps", reach=90.0)

        assert flagged.message.startswith("Titles added before your watch history starts")
        assert "Nothing will be flagged" not in flagged.message
        assert '"recent" and "ever"' in flagged.message

    def test_a_threshold_with_no_headroom_names_a_move_the_editor_accepts(self) -> None:
        """``condemn_at`` may be 100, which is the cautious direction, and ``max_discount`` is
        ``ge=1`` -- so the headroom is 0 and every settable value is too high. Naming a number
        sent the operator to a control that refuses it ("set that rule to 0 points or less").
        """
        body = _policy(condemn_at=100, graded_keeps=self._lean("recent_watchers", 40).graded_keeps)
        [flagged] = self._warnings_on(body, "graded_keeps", reach=90.0)

        assert "remove that rule" in flagged.message
        assert "0 points or less" not in flagged.message

    def test_it_is_not_the_total_based_warning_and_both_can_fire(self) -> None:
        """They answer different questions on one anchor, and neither covers the other.

        The pre-existing one is about the keeps TOTALLING at least ``condemn_at`` whatever the
        mirror says; this one is about a SINGLE keep taking its full discount because the
        mirror cannot support the field. A keep at 40 against the shipped 70 fires only the
        second, which is the dead zone this closed.
        """
        only_mirror = self._warnings_on(
            self._lean("recent_watchers", 40), "graded_keeps", reach=90.0
        )
        assert len(only_mirror) == 1
        assert "subtract up to" not in only_mirror[0].message

        # At 70 both hold, and the operator gets both sentences rather than one standing in
        # for the other.
        both = self._warnings_on(self._lean("recent_watchers", 70), "graded_keeps", reach=90.0)
        assert len(both) == 2
        assert sum("subtract up to" in w.message for w in both) == 1


class TestTheCondemnLanesCoverage:
    """The fourth lane, and the one ``inspect`` used to rule harmless in a comment.

    A blocked condemn rule withholds its pressure and keeps its weight in the denominator, so
    it cannot empty the list through PRESSURE -- which is what the old comment said, and it
    stopped there. The weight it leaves behind lowers both bounds ``decide_verdict`` reads:
    coverage falls under ``coverage_floor_bp`` and every item abstains, and the score ceiling
    falls with it so nothing can reach ``condemn_at`` either. Weights need only total 100, so
    a split that does this is a legal policy nothing refused and nothing announced (#164).

    Claimed only over the readers whose block is library-wide, which is not every reader of
    the field -- see the boolean case below, which is why this is a sum over two of the three
    and not over the field.
    """

    def _condemn_warnings(
        self, body: PolicyBody, reach: float | None = 90.0
    ) -> list[PolicyWarning]:
        return [
            w
            for w in inspect(body, ProfileSettings(), history_reach_days=reach)
            if w.field in ("signals", "custom_condemn")
        ]

    def _gate_off(self, **overrides: object) -> dict[str, object]:
        """The gate off, so the window is the 365-day fallback, beside a dormancy floor the
        reach clears. Without the second the floor keeps every item on age alone and every
        warning in this family is correctly silent."""
        return {
            "gates": (
                GateSetting(gate=GateId.SERVER_POPULARITY, enabled=False, threshold=2),
                GateSetting(gate=GateId.MIN_DORMANCY, threshold=60),
            ),
            **overrides,
        }

    def test_weight_parked_on_a_blocked_signal_empties_the_list(self) -> None:
        """Issue #164's second measured split, driven: 30 unwatched / 60 few-watchers / 10
        rating against a 90-day mirror and the 365-day fallback gives coverage 0.40 under the
        shipped floor of 0.50, so ``decide_verdict`` abstains for every item."""
        body = _policy(**self._gate_off(signals=_split(40, 60)))

        [flagged] = self._condemn_warnings(body)

        assert flagged.severity == "warn"
        assert flagged.message.startswith("Nothing will be flagged for removal.")
        # The number that makes it actionable is the weight to move, not a coverage ratio.
        assert "60 of your 100 removal points" in flagged.message
        assert "40 points are left to judge on" in flagged.message

    def test_a_graded_custom_rule_adds_to_the_same_sum(self) -> None:
        """#164's first split: 20 on the built-in alone clears both bounds, and the operator's
        own graded rule on the same count is what carries it under. Summed rather than tested
        one at a time, for the reason the lean lane is: each is withheld in full and the
        shortfall in the denominator is their total."""
        alone = _policy(**self._gate_off(signals=_split(80, 20)))
        assert self._condemn_warnings(alone) == []

        with_rule = _policy(
            **self._gate_off(
                signals=(
                    SignalSetting(signal=SignalId.UNWATCHED, weight=35, saturate_at=730),
                    SignalSetting(signal=SignalId.FEW_WATCHERS, weight=20, saturate_at=3),
                    SignalSetting(signal=SignalId.LOW_RATING, weight=10, saturate_at=60),
                ),
                custom_condemn=(
                    GradedCondemnSpec(
                        name="hardly watched", field="recent_watchers", weight=35, saturate_at=3
                    ),
                ),
            )
        )

        [flagged] = self._condemn_warnings(with_rule)

        assert "55 of your 100 removal points" in flagged.message

    def test_a_boolean_rule_is_left_out_because_its_block_is_per_item(self) -> None:
        """The discriminator for the sum, and the reason it is not simply "weight on the field".

        A boolean rule goes through ``fields.evaluate``, which keeps
        ``_survives_more_history``'s earned outcomes, so an item the truncated count already
        settles is judged normally. Measured at a 90-day reach against the 365-day fallback:
        the same 35-point rule leaves coverage at 0.45 for a title with 0 recent watchers and
        0.80 for one with 50, where the graded arm holds 0.45 for both. Counting it would
        claim an empty list that is not empty, which is rule 144's reassuring direction.
        """
        boolean = _policy(
            **self._gate_off(
                signals=(
                    SignalSetting(signal=SignalId.UNWATCHED, weight=35, saturate_at=730),
                    SignalSetting(signal=SignalId.FEW_WATCHERS, weight=20, saturate_at=3),
                    SignalSetting(signal=SignalId.LOW_RATING, weight=10, saturate_at=60),
                ),
                custom_condemn=(
                    BooleanCondemnSpec(
                        name="hardly watched",
                        field="recent_watchers",
                        op=Op.LTE,
                        value=1,
                        weight=35,
                    ),
                ),
            )
        )

        assert self._condemn_warnings(boolean) == []

    def test_both_bounds_the_verdict_reads_are_covered(self) -> None:
        """The floor is not the only way this empties the list, so neither is tested alone.

        ``decide_verdict`` abstains under ``coverage_floor_bp`` AND fails to reach
        ``condemn_at``, and withheld weight lowers both at once. Each case below clears one
        bound and fails the other, so a fix reading only one of them leaves a case silent.
        Asking the real decision function is what makes that free (rule 3/22).
        """
        # Coverage alone: 60 withheld leaves a ceiling of 40, which clears a threshold of 20
        # and still sits under the 0.50 floor.
        coverage_only = _policy(**self._gate_off(signals=_split(40, 60), condemn_at=20))
        assert len(self._condemn_warnings(coverage_only)) == 1

        # The threshold alone: 45 withheld leaves 55, which clears the 0.50 floor and cannot
        # reach the shipped threshold of 70.
        threshold_only = _policy(**self._gate_off(signals=_split(55, 45)))
        assert len(self._condemn_warnings(threshold_only)) == 1

        # Neither: 20 withheld leaves 80, over the floor and over the threshold, so items are
        # still condemned and there is no empty list to announce.
        neither = _policy(**self._gate_off(signals=_split(80, 20)))
        assert self._condemn_warnings(neither) == []

    def test_the_shipped_movie_policy_is_not_flagged(self) -> None:
        """The population this must not fire on. ``FEW_WATCHERS`` carries 20 of the shipped
        100, which leaves 80 against a threshold of 70 and a floor of 0.50, so a title is
        still condemnable on a short mirror and claiming otherwise would be false."""
        assert [
            w
            for w in inspect(DEFAULT_MOVIE_POLICY, ProfileSettings(), history_reach_days=90.0)
            if w.field in ("signals", "custom_condemn")
        ] == []

    def test_a_history_that_covers_the_window_is_silent(self) -> None:
        """Nothing is withheld once the mirror spans the window, so the weight is doing its
        job and there is nothing to say."""
        body = _policy(**self._gate_off(signals=_split(40, 60)))

        assert self._condemn_warnings(body, reach=365.0) == []
        assert self._condemn_warnings(body, reach=800.0) == []

    def test_a_caller_that_cannot_read_the_mirror_stays_quiet(self) -> None:
        """The ``requests_app_configured`` posture, same as every other lane here: a caller
        that cannot tell must not guess, since guessing short condemns a policy that is fine."""
        assert self._condemn_warnings(_policy(**self._gate_off(signals=_split(40, 60))), None) == []

    def test_the_dormancy_floor_silences_it(self) -> None:
        """Under the floor every item is kept on age alone and PROTECT beats the coverage
        check in ``decide_verdict``, so the weights decide nothing and the remedy would move
        no verdict. The same guard the three other lanes take, for the same reason."""
        floored = _policy(
            gates=(
                GateSetting(gate=GateId.SERVER_POPULARITY, enabled=False, threshold=2),
                GateSetting(gate=GateId.MIN_DORMANCY, threshold=1095),
            ),
            signals=_split(40, 60),
        )

        assert self._condemn_warnings(floored) == []

    def test_the_warning_lands_on_the_card_holding_the_points(self) -> None:
        """Rule 42: the built-in's slider and the operator's own rules are different cards, so
        the anchor follows the weight rather than defaulting to one of them."""
        on_the_signal = _policy(**self._gate_off(signals=_split(40, 60)))
        assert self._condemn_warnings(on_the_signal)[0].field == "signals"

        on_the_rule = _policy(
            **self._gate_off(
                signals=(SignalSetting(signal=SignalId.UNWATCHED, weight=40, saturate_at=730),),
                custom_condemn=(
                    GradedCondemnSpec(
                        name="hardly watched", field="recent_watchers", weight=60, saturate_at=3
                    ),
                ),
            )
        )
        assert self._condemn_warnings(on_the_rule)[0].field == "custom_condemn"


class TestAHoldTheWatchHistoryCannotEstablish:
    """The season path's member of the family, and the last of the four lanes (#154).

    The mid-binge guard holds a viewer whose last play falls inside ``in_progress_hold_days``.
    Where the mirror does not span that hold, an invisible viewer and an expired one are the
    same viewer, so ``season_pruning`` calls the set un-establishable rather than empty and
    ``plan_series_prune`` holds every season on disk. The removal list is empty and nothing on
    the page said why: ``in_progress_hold_days`` appeared in ``policy`` only as a declaration.

    Rule 72's twin one field down the same editor card, and the journey is what makes it bite.
    An operator on a short mirror gets the popularity-window warning, follows it, lowers the
    window to match their history, and clears it -- leaving a page with no warnings and a list
    that is still empty, because the warning they just cleared was the only surface that ever
    named their history reach.
    """

    #: Under the shipped 1095-day floor every item is kept on age alone (dormancy is clamped to
    #: the mirror), so the hold decides nothing and this family is correctly silent. 30 is the
    #: issue's own journey: low enough that the reach clears it, so the hold is what binds.
    DORMANCY = 30

    def _tv(self, **overrides: object) -> PolicyBody:
        base: dict[str, object] = {
            "media_type": "tv",
            "gates": (GateSetting(gate=GateId.MIN_DORMANCY, threshold=self.DORMANCY),),
        }
        return _policy(**{**base, **overrides})

    def _hold_warnings(self, body: PolicyBody, reach: float | None = 90.0) -> list[PolicyWarning]:
        return [
            w
            for w in inspect(body, ProfileSettings(), history_reach_days=reach)
            if w.field == "in_progress_hold_days"
        ]

    def test_a_hold_longer_than_the_history_is_flagged(self) -> None:
        """The shipped 180-day hold against a 90-day mirror, which is the reported case."""
        [flagged] = self._hold_warnings(self._tv(in_progress_hold_days=180))

        assert flagged.severity == "warn"
        assert flagged.message.startswith("No TV season will be flagged for removal.")
        # The hold is named before the cause clause, so the in-margin arm's "that far" has a
        # span to point at, and the remedy names the box the operator is looking at.
        assert "6 months" in flagged.message
        assert "lower this to match your history" in flagged.message

    def test_the_journey_that_used_to_end_on_a_silent_page(self) -> None:
        """The whole point of the issue: clearing the window warning must not clear this one.

        Both warnings hold at the start. The operator follows the first, lowers the window to
        their reach, and that one goes -- and this one stays, because the hold is a different
        span and lowering the window did nothing to it. Before, the page went silent here
        while the list stayed empty.
        """
        window_anchor = f"gates.{GateId.SERVER_POPULARITY.value}.window_days"

        def fields_at(window: int) -> list[str]:
            body = self._tv(
                in_progress_hold_days=180,
                gates=(
                    GateSetting(gate=GateId.MIN_DORMANCY, threshold=self.DORMANCY),
                    GateSetting(gate=GateId.SERVER_POPULARITY, threshold=3, window_days=window),
                ),
            )
            return [w.field for w in inspect(body, ProfileSettings(), history_reach_days=90.0)]

        assert fields_at(365) == [window_anchor, "in_progress_hold_days"]
        assert fields_at(90) == ["in_progress_hold_days"]

    def test_a_hold_of_zero_gets_its_own_cause(self) -> None:
        """The measured trap, and why the predicate decides this rather than the shortfall.

        ``0`` means "hold a place forever", which no finite mirror supports, so
        ``progress_is_establishable`` answers False at any reach at all. ``history_shortfall``
        disagrees: handed a span of zero days it finds the mirror covers it and returns None,
        so a branch guarded on the shortfall would go silent on the one setting that can never
        be established, and a message built from it would have had no cause clause to print.
        """
        assert progress_is_establishable(reach_days=10_000, hold_days=0) is False
        assert history_shortfall(Known(value=10_000.0, source="tautulli"), 0.0) is None

        [flagged] = self._hold_warnings(self._tv(in_progress_hold_days=0), reach=10_000.0)

        assert "held forever" in flagged.message
        assert "Set a number of days, or turn this protection off." in flagged.message

    def test_a_history_that_spans_the_hold_is_silent(self) -> None:
        """The guard can answer, so it is doing its job and there is nothing to say. 180 is the
        exact boundary: establishable AT the hold, not one day under it."""
        body = self._tv(in_progress_hold_days=180)

        assert self._hold_warnings(body, reach=180.0) == []
        assert self._hold_warnings(body, reach=800.0) == []
        assert len(self._hold_warnings(body, reach=179.0)) == 1

    def test_the_protection_being_off_silences_it(self) -> None:
        """``season_pruning`` reads ``keep_in_progress and not progress_established``, so a
        guard that is switched off is holding no seasons and there is no empty list to explain.
        The control is not on the page either."""
        assert (
            self._hold_warnings(self._tv(in_progress_hold_days=180, keep_in_progress=False)) == []
        )

    def test_the_movies_policy_never_speaks(self) -> None:
        """Movies have no seasons and ignore the field outright, so a warning here would name a
        control the movie editor does not render."""
        assert self._hold_warnings(_policy(in_progress_hold_days=0)) == []

    def test_a_caller_that_cannot_read_the_mirror_stays_quiet(self) -> None:
        assert self._hold_warnings(self._tv(in_progress_hold_days=180), reach=None) == []

    def test_the_dormancy_floor_silences_it(self) -> None:
        """The fourth lane takes the same guard as the other three, for the same reason: under
        the floor every item is kept on age alone, dormancy being clamped to the mirror, so the
        hold decides nothing and its remedy would move no verdict. Without this it would fire
        on both shipped policies for every operator holding under three years of history."""
        floored = self._tv(
            in_progress_hold_days=180,
            gates=(GateSetting(gate=GateId.MIN_DORMANCY, threshold=1095),),
        )

        assert self._hold_warnings(floored) == []
