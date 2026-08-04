# SPDX-License-Identifier: AGPL-3.0-or-later
"""Policy: the hash, the floors, and the things that cannot be spelled.

An approval is bound to a policy hash. So the hash must change when the *meaning*
changes and must not change when it does not -- otherwise approvals either void
themselves at random, or (far worse) silently survive an edit the human never saw.
"""

from __future__ import annotations

import itertools
import json
from collections.abc import Callable, Sequence
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from pydantic import ValidationError

from reaper.api.schemas import GateSettingIn
from reaper.engine import policy as policy_module
from reaper.engine.dormancy import dormancy_days, reference_instant
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
    DEFAULT_IMDB_LIST_NAME,
    DEFAULT_MOVIE_POLICY,
    DEFAULT_TAG_LIST_NAME,
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
    convert_list_protections,
    has_legacy_list_protections,
    inspect,
    rebalance,
    recover_rating_rules,
)
from reaper.engine.signals import Score, SignalConfig, SignalId, score
from reaper.engine.verdict import decide_verdict
from reaper.ratings import RatingSource, is_percentage_source, source_label
from reaper.services.scan_runner import GATE_TYPES, ScanConfigError, build_gates
from reaper.services.season_pruning import SeasonStats, plan_series_prune

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

    def test_switching_a_protection_off_keeps_the_evidence_hash(self) -> None:
        """A gate decides what to make of an item; it does not decide what gets gathered.

        Every fact a gate reads is frozen whether or not the gate is switched on -- no fact
        builder branches on the gate list -- so the replay answers a toggle exactly. This is
        the edit an operator makes most often on this page, and it used to blank the panel.
        """
        a = _policy(gates=(GateSetting(gate=GateId.MIN_DORMANCY, threshold=1095),))
        b = _policy(gates=(GateSetting(gate=GateId.MIN_DORMANCY, threshold=1095, enabled=False),))
        assert a.scoring_hash() != b.scoring_hash()  # it changes the answer
        assert a.evidence_hash() == b.evidence_hash()  # ...off the same frozen bytes

    def test_moving_a_protection_threshold_keeps_the_evidence_hash(self) -> None:
        # Dormancy is measured from the item's own dates, never from the floor it is
        # compared against, so moving the floor re-reads nothing.
        a = _policy(gates=(GateSetting(gate=GateId.MIN_DORMANCY, threshold=1095),))
        b = _policy(gates=(GateSetting(gate=GateId.MIN_DORMANCY, threshold=30),))
        assert a.scoring_hash() != b.scoring_hash()
        assert a.evidence_hash() == b.evidence_hash()

    def test_switching_the_popularity_gate_off_keeps_the_evidence_hash(self) -> None:
        """The one gate whose settings reach the gather phase, in the direction that is
        still exact: a disabled popularity gate leaves the window at the 365-day default, so
        the watcher counts already frozen were counted over exactly that span."""
        a = _policy(
            gates=(GateSetting(gate=GateId.SERVER_POPULARITY, threshold=3, window_days=365),)
        )
        b = _policy(
            gates=(
                GateSetting(
                    gate=GateId.SERVER_POPULARITY, threshold=3, window_days=365, enabled=False
                ),
            )
        )
        assert a.evidence_hash() == b.evidence_hash()

    def test_switching_the_popularity_gate_on_at_a_new_window_changes_it(self) -> None:
        """And the direction that is not. A gate disabled at scan time was counted over the
        365-day fallback; enabling it at 90 asks a question of a count nobody took, so this
        has to fall through to the refusal however cheap a replay would be."""
        off = _policy(
            gates=(
                GateSetting(
                    gate=GateId.SERVER_POPULARITY, threshold=3, window_days=90, enabled=False
                ),
            )
        )
        on = _policy(
            gates=(GateSetting(gate=GateId.SERVER_POPULARITY, threshold=3, window_days=90),)
        )
        assert off.evidence_hash() != on.evidence_hash()

    def test_every_gate_field_is_classified_as_gathering_or_judging(self) -> None:
        """Rule 103's drift guard, and the reason the split is written out by name.

        A gate field added later would otherwise fall silently into the judging half and be
        replayed off frozen bytes that never covered it -- a confident wrong preview, which
        is worse than the blank panel this change removes. Classify the new field into one
        of the two sets, and if it is a gathering one, fold it into ``_gathering_evidence``.
        """
        declared = PolicyBody._GATHERING_GATE_FIELDS | PolicyBody._JUDGING_GATE_FIELDS
        actual = set(GateSetting.model_fields)

        assert actual == declared, (
            f"GateSetting fields changed: {actual ^ declared}. Classify each into "
            "PolicyBody._GATHERING_GATE_FIELDS or _JUDGING_GATE_FIELDS, and fold any "
            "gathering one into PolicyBody._gathering_evidence()."
        )

    def test_an_on_list_rule_previews_without_a_scan_but_still_voids_an_approval(self) -> None:
        """The keep tags left the body for Settings -> Lists, and a list now protects
        through an ``on_list`` rule reading the frozen ``Facts.on_lists``. That makes the
        rule REPLAYABLE -- the membership was gathered whether or not any rule named it --
        so the evidence hash holds still. The policy hash must still move: a plan approved
        before the rule was added targets files the new rule keeps (rule 113)."""
        a = _policy(protect_conditions=(ConditionSpec(field="on_list", op=Op.EQ, value="A"),))
        b = _policy(protect_conditions=(ConditionSpec(field="on_list", op=Op.EQ, value="B"),))
        assert a.evidence_hash() == b.evidence_hash()
        assert a.policy_hash() != b.policy_hash()

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

    def test_one_vote_is_the_least_a_counting_source_accepts(self) -> None:
        """The inclusive edge of the vote floor, which the refusal above cannot pin.

        ``min_votes >= 1`` is the whole rule, so 1 has to be allowed. Refusing it would make
        the smallest legal bar unsavable while the error text still told the operator to
        "use at least 1".
        """
        assert RatingRuleSpec(source=RatingSource.IMDB, floor=75, min_votes=1).min_votes == 1

    def test_a_single_vote_on_a_percentage_source_is_refused_too(self) -> None:
        """The other side reads `!= 0`, so it is 1 and not 500 that pins it: a probe at 500
        leaves the comparison free to become "anything but 1" and still pass."""
        with pytest.raises(ValidationError, match="no vote count"):
            RatingRuleSpec(source=RatingSource.ROTTEN_TOMATOES_CRITIC, floor=75, min_votes=1)

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
        'off' as a zero threshold that is banned.

        The same gate and the same zero the test above refuses, so this pins the
        ``not self.enabled`` early return rather than a value the validator never
        polices: RATING_FLOOR carries no threshold of its own any more, so a gate
        row spelled that way builds either way and could not fail (rule 141).
        """
        assert GateSetting(gate=GateId.SERVER_POPULARITY, enabled=False, threshold=0)

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

    def test_a_one_point_miss_names_one_point_and_never_a_negative(self) -> None:
        """The smallest miss in each direction, because it is the one that can go negative.

        The remedy picks its branch on `over > 0`, and `over` is 1 or -1 here. Only ever
        driving a miss of 20 or 30 left the comparison free to move: at `over > 1` a total of
        101 takes the under-allocated branch and tells the operator to "give out the other -1",
        which is rule 21's floor, not a rounding detail.
        """
        with pytest.raises(ValidationError, match=r"Take 1 away"):
            _policy(signals=_split(51, 50))
        with pytest.raises(ValidationError, match=r"Give out the other 1\b"):
            _policy(signals=_split(50, 49))

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

    @pytest.mark.parametrize(
        "build",
        [
            lambda floor: SignalSetting(
                signal=SignalId.UNWATCHED, weight=10, saturate_at=10, floor=floor
            ),
            lambda floor: GradedCondemnSpec(
                name="big", field="size_bytes", weight=10, saturate_at=10, floor=floor
            ),
            lambda floor: GradedKeepSpec(
                name="loved",
                field="watchers_all_time",
                max_discount=10,
                saturate_at=10,
                floor=floor,
            ),
        ],
        ids=["built-in signal", "graded condemn rule", "graded keep rule"],
    )
    @pytest.mark.parametrize("floor", [10, 20], ids=["floor-equals-saturate", "floor-above"])
    def test_a_ramp_that_cannot_ramp_is_refused_on_every_spec_that_has_one(
        self, build: Callable[[int], object], floor: int
    ) -> None:
        """``floor >= saturate_at`` is one rule with three copies (rule 72), and each has to
        refuse BOTH ways of collapsing the ramp.

        At equality the ramp has no width, so the rule is either always off or always at full
        pressure; above it the ramp runs backwards. Neither is a bound an operator can have
        meant. This was three copies deep and only one of them was tested, at only one of the
        two values -- so an operator flip on either newer copy let a degenerate rule be saved,
        and the save boundary is the last place that can refuse it.
        """
        with pytest.raises(ValidationError, match="below saturate_at"):
            build(floor)


class TestCaps:
    """Four caps, not two. The rolling BYTE cap is what keeps a multi-terabyte incident
    out of reach: no sequence of runs is admitted past it.

    Exact in ITEMS, a close bound in BYTES: the sizes it charges are what the *arr tracks,
    and a movie delete takes the whole folder (#317, learning 14b). Do not restore "no
    sequence of runs can exceed it" here -- it was one of three copies of a claim the
    measurement refuted."""

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

    def test_a_run_cap_equal_to_the_rolling_cap_is_allowed(self) -> None:
        """All three cap relationships are `>`, so equality is legal on each: one run may
        spend the whole 30-day budget, and every unmeasured item may be the run's whole
        allowance. Nothing pinned that, which left all three free to become `>=` and refuse a
        configuration the copy never warns about.
        """
        settings_ = ProfileSettings(
            max_items_per_run=10,
            max_items_per_30d=10,
            max_bytes_per_run=1_000_000_000,
            max_bytes_per_30d=1_000_000_000,
            max_unmeasured_per_run=10,
        )

        assert settings_.max_items_per_run == settings_.max_items_per_30d
        assert settings_.max_bytes_per_run == settings_.max_bytes_per_30d
        assert settings_.max_unmeasured_per_run == settings_.max_items_per_run

    def test_more_unmeasured_items_than_items_is_refused(self) -> None:
        """A run that may delete more unknown-size items than items in total is incoherent,
        and the allowance is the only bound on that population -- an unmeasured item adds
        nothing to either byte cap, so the byte caps cannot catch it."""
        with pytest.raises(ValidationError, match="unknown"):
            ProfileSettings(max_items_per_run=9, max_items_per_30d=10, max_unmeasured_per_run=10)

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
        """Every protection the owner asked for is on by default. List membership arrives
        as the two shipped ``on_list`` keep rules rather than as gates: one names the tag
        list, one the IMDb list, both keeping outright, and their names are the ones
        ``list_config.ensure_defaults`` seeds -- a rule naming a list that does not exist
        would render as a live protection covering nothing (rule 25)."""
        enabled = {g.gate for g in DEFAULT_MOVIE_POLICY.gates if g.enabled}

        assert GateId.STREAMING_NOW in enabled
        assert GateId.RATING_FLOOR in enabled
        assert GateId.SERVER_POPULARITY in enabled
        assert GateId.DATA_HORIZON in enabled
        assert GateId.MIN_DORMANCY in enabled

        for body in (DEFAULT_MOVIE_POLICY, DEFAULT_TV_POLICY):
            on_list = [c for c in body.protect_conditions if c.field == "on_list"]
            assert {str(c.value) for c in on_list} == {
                DEFAULT_TAG_LIST_NAME,
                DEFAULT_IMDB_LIST_NAME,
            }, body.media_type

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

        Two retirements are handled the other way, and that is deliberate: ``whitelisted``
        and ``curated_list`` WERE live protections, so ``convert_list_protections`` rewrites
        a stored body's row into the equivalent ``on_list`` rule rather than dropping it --
        putting them in ``RETIRED_GATES`` would silently withdraw cover. The set equality
        below is what forces a future retirement to pick one of the two mechanisms.
        """
        converted = {GateId.WHITELISTED, GateId.CURATED_LIST}
        unbuildable = set(GateId) - _BUILDABLE_GATES - _ENGINE_ONLY_GATES

        assert unbuildable == PolicyBody.RETIRED_GATES | converted, (
            f"neither declared retired nor converted by the shim: "
            f"{unbuildable - PolicyBody.RETIRED_GATES - converted}; "
            f"declared retired but buildable: {PolicyBody.RETIRED_GATES - unbuildable}"
        )
        assert not PolicyBody.RETIRED_GATES & converted
        # ...and the shim really does fire on a stored body naming either one.
        for gate in sorted(converted):
            stored = DEFAULT_MOVIE_POLICY.model_dump(mode="json")
            stored["gates"] = [{"gate": gate.value, "enabled": True}, *stored["gates"]]
            assert has_legacy_list_protections(stored), gate

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

    def test_the_rating_keep_says_so_when_it_has_no_sources_to_keep_on(self) -> None:
        """Rating keep on with an empty source list keeps nothing, and looks configured.

        This is the state the #241 shims exist to repair, so the warning about it has to
        hold. Nothing pinned it: a mutation run (#243) found that making the branch never
        fire survives the whole suite. The stock inversion mutant dies, but only because it
        makes the warning fire on healthy policies, which the shipped-default tests catch --
        the deleting direction, the one that costs the operator a protection they can see
        switched on, was undefended.

        Both discriminators are here so the assertion cannot be satisfied by a warning that
        fires unconditionally: the same gate turned off says nothing, and the same gate with
        a source says nothing.

        The third discriminator is the field, and it is now load-bearing rather than
        incidental. Since #189 a per-bar complaint carries ``keep_rating_rules.<source>.floor``
        and only this card-level one carries the bare name, so ``== "keep_rating_rules"`` is
        what separates the two. A ``startswith`` here would have gone on passing while the
        card-level warning stopped firing, answered by a bar's warning instead.
        """
        enabled_with_no_sources = _policy(gates=(GateSetting(gate=GateId.RATING_FLOOR),))

        warnings = inspect(enabled_with_no_sources, ProfileSettings())

        complaint = [w for w in warnings if w.field == "keep_rating_rules"]
        assert len(complaint) == 1
        assert complaint[0].severity == "warn"
        assert "not keeping anything" in complaint[0].message

        off = _policy(gates=(GateSetting(gate=GateId.RATING_FLOOR, enabled=False),))
        assert not [
            w for w in inspect(off, ProfileSettings()) if w.field.startswith("keep_rating_rules")
        ]

        stocked = _policy(
            gates=(GateSetting(gate=GateId.RATING_FLOOR),),
            keep_rating_rules=(RatingRuleSpec(source=RatingSource.IMDB, floor=75, min_votes=1000),),
        )
        assert not [
            w
            for w in inspect(stocked, ProfileSettings())
            if w.field.startswith("keep_rating_rules")
        ]

    def test_each_bar_warning_names_the_bar_it_is_about(self) -> None:
        """A complaint about one bar names that bar, so the page can put it on its own box.

        ``PolicyEditor``'s ``keep_rating_rules`` anchor claims this family by its
        ``keep_rating_rules.`` prefix, and each bar row points ``aria-describedby`` at
        ``keep_rating_rules.<source>.floor`` in full (#189). The sibling cases in this file
        assert on the MESSAGE, which carries the source's label and would stay green while the
        field went back to naming the card -- putting every complaint on every bar, which is
        the misattribution the issue was filed on.

        Two sources, on the two scales, warned about at once: with one bar a field that
        returned a constant would pass. The population assertion is the rule 145 half -- a
        third producer given the bare field is one this page has never bound, and a
        flag-shaped check cannot report a member it was never told to look for. The card's own
        complaint is absent by construction here, because the list is not empty.
        """
        body = _policy(
            gates=(GateSetting(gate=GateId.RATING_FLOOR),),
            keep_rating_rules=(
                RatingRuleSpec(source=RatingSource.IMDB, floor=96, min_votes=1000),
                RatingRuleSpec(source=RatingSource.ROTTEN_TOMATOES_CRITIC, floor=8),
            ),
        )
        expected = {
            "keep_rating_rules.imdb.floor",
            "keep_rating_rules.rotten_tomatoes_critic.floor",
        }

        fields = {w.field for w in inspect(body, ProfileSettings())}

        assert expected <= fields
        assert {f for f in fields if f.startswith("keep_rating_rules")} == expected

    @pytest.mark.parametrize(
        ("anchor", "kind", "build"),
        [
            (
                "protect_conditions",
                "protection",
                lambda field: {
                    "protect_conditions": (ConditionSpec(field=field, op=Op.GTE, value=1),)
                },
            ),
            (
                "custom_condemn",
                'rule "my rule"',
                lambda field: {
                    "custom_condemn": (
                        GradedCondemnSpec(name="my rule", field=field, weight=40, saturate_at=1000),
                    ),
                    # The removal budget still has to total 100 with the rule's 40 in it.
                    "signals": (
                        SignalSetting(signal=SignalId.UNWATCHED, weight=60, saturate_at=730),
                    ),
                },
            ),
            (
                "graded_keeps",
                'keep rule "my rule"',
                lambda field: {
                    "graded_keeps": (
                        GradedKeepSpec(
                            name="my rule", field=field, max_discount=10, floor=0, saturate_at=10
                        ),
                    )
                },
            ),
        ],
        ids=["a protection", "a removal rule", "a graded keep"],
    )
    @pytest.mark.parametrize(
        ("media_type", "unreadable", "label", "where", "readable"),
        [
            ("tv", "release_age", "Age since release", "seasons", "season_rank"),
            ("movie", "season_rank", "How far back the season is", "movies", "release_age"),
        ],
        ids=["a movie-only field on a TV policy", "a TV-only field on a movie policy"],
    )
    def test_a_rule_on_a_field_this_media_type_cannot_read_is_called_out(
        self,
        anchor: str,
        kind: str,
        build: Callable[[str], dict[str, object]],
        media_type: str,
        unreadable: str,
        label: str,
        where: str,
        readable: str,
    ) -> None:
        """Rule 107's warning, which nothing drove at all.

        ``Condition.validate_for`` checks the lane, the operator and the type but not the
        media type, so a rule saved before a field was narrowed keeps validating and simply
        stops being offered. A protection then reads as "checked, did not fire" forever, and
        a removal rule is worse than inert: its points still count toward the fixed
        100-point denominator, so it holds down every score in that policy.

        A mutation run (#243) found four survivors in this one branch, and the shape of the
        split is the proof: deleting the ``BY_KEY.get`` lookup DIES (NameError on the next
        line, for any rule at all), while deleting either statement past the ``continue``
        SURVIVES, still raising NameError. So the suite drove rules through the loop and
        never once drove one the media type cannot read -- ``inspect`` could be made to
        crash outright and stay green.

        Every arm is therefore driven here, because they fail separately: all three anchors
        (only the named two build the ``"name"`` clause), and both directions of the
        media-type test, whose ``==`` was free to invert and tell a TV operator their rule
        cannot be read "for movies".
        """
        body = _policy(media_type=media_type, **build(unreadable))

        warnings = inspect(body, ProfileSettings())

        complaint = [w for w in warnings if w.field == anchor]
        assert len(complaint) == 1
        assert complaint[0].severity == "danger"
        assert complaint[0].message.startswith(
            f"Your {kind} uses {label}, which Reaper cannot read for {where},"
        )

        # The same rule on the policy that CAN read the field is silent -- otherwise the
        # assertion above is satisfied by a warning that fires on every rule.
        fine = _policy(media_type=media_type, **build(readable))
        assert not [w for w in inspect(fine, ProfileSettings()) if w.field == anchor]

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

    def test_the_gate_off_warnings_carry_the_whole_gates_field(self) -> None:
        """The page routes on the whole string, not on the tail these siblings match.

        ``PolicyEditor``'s ``gates`` anchor claims this family by its ``gates.`` prefix,
        which is what renders the warning beside the protections list instead of at the foot
        of the page, and the row's own boxes point ``aria-describedby`` at
        ``gates.<gate>.<setting>`` in full (#189). The two assertions above match with
        ``endswith``, so dropping or renaming the prefix would leave them green while moving
        the warning off the switch it is about.

        Second assertion is the population, not a second spelling of the first (rule 145): a
        third protection warned about on its switch is a field this page has never bound, and
        a flag-shaped check cannot report one it was never told to look for.
        """
        body = _policy(
            gates=(
                GateSetting(gate=GateId.STREAMING_NOW, enabled=False),
                GateSetting(gate=GateId.DATA_HORIZON, enabled=False),
            )
        )
        expected = {"gates.streaming_now.enabled", "gates.data_horizon.enabled"}

        fields = {w.field for w in inspect(body, ProfileSettings())}

        assert expected <= fields
        assert {f for f in fields if f.endswith(".enabled")} == expected

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


class TestWhereEachDetectorThresholdActuallySits:
    """The same four warnings above, driven at the value they turn on rather than well
    inside it. Issue #243, and every row of it was real.

    The tests above drive 96 against ``>= 90``, 7 against ``<= 20``, 7 against ``< 30`` and
    20 against ``<= 30``. Each one proves the warning exists somewhere in its region and
    none of them can say where the edge is, so a mutation run moved every threshold a point
    in both directions, swapped ``>=`` for ``>``, and the suite stayed green through all of
    it. An operator setting a bar of exactly 9.0 is the one this costs: dead center of the
    misconfiguration these sentences exist to catch, and one point outside every case that
    was driven.

    So each threshold is driven at the value and one step either side. The rendered number
    is asserted too, not only the sentence: ``rule.floor / 10`` converts the stored tenths
    for display and survived becoming ``/ 9``, ``/ 11`` and ``* 10``, which is the same
    divisor drift #241 found one layer down in ``RatingFloorGate``.
    """

    #: Enough votes to build a rule on a source that counts them. Not a threshold under
    #: test here, and deliberately not the shipped 1000 (rule 141).
    VOTES = 250

    def _bar(self, source: RatingSource, floor: int, votes: int) -> PolicyBody:
        return _policy(
            gates=(GateSetting(gate=GateId.RATING_FLOOR),),
            keep_rating_rules=(RatingRuleSpec(source=source, floor=floor, min_votes=votes),),
        )

    def _said(self, body: PolicyBody, **kwargs: object) -> list[str]:
        return [w.message for w in inspect(body, ProfileSettings(), **kwargs)]  # type: ignore[arg-type]

    @pytest.mark.parametrize(("floor", "shown"), [(89, None), (90, "9.0"), (91, "9.1")])
    def test_the_bar_that_protects_almost_nothing_starts_at_nine_point_oh(
        self, floor: int, shown: str | None
    ) -> None:
        """8.9 is a high bar somebody may well mean. 9.0 is where we say so."""
        loud = self._said(self._bar(RatingSource.IMDB, floor, self.VOTES))

        if shown is None:
            assert loud == []
        else:
            assert len(loud) == 1
            assert "will protect almost nothing" in loud[0]
            assert f"bar of {shown} " in loud[0]

    @pytest.mark.parametrize(("floor", "shown"), [(19, "1.9"), (20, "2.0"), (21, None)])
    def test_the_bar_that_protects_everything_stops_at_two_point_oh(
        self, floor: int, shown: str | None
    ) -> None:
        """The mirror image, and the arm that catches someone typing 7 meaning 7.0."""
        loud = self._said(self._bar(RatingSource.IMDB, floor, self.VOTES))

        if shown is None:
            assert loud == []
        else:
            assert len(loud) == 1
            assert "protects essentially everything" in loud[0]
            assert f"bar of {shown} " in loud[0]

    @pytest.mark.parametrize(("floor", "shown"), [(19, "19%"), (20, "20%"), (21, None)])
    def test_a_percentage_bar_read_on_the_ten_point_scale_is_called_out(
        self, floor: int, shown: str | None
    ) -> None:
        """The arm nothing drove at all: neither of its two sentences appeared anywhere in
        ``tests/``, and all six of its mutants survived, including the whole comparison
        inverted. A Rotten Tomatoes bar of 8 means 8%, which keeps the library.

        The number is a percentage here and NOT divided, so this also pins that the two
        arms did not merge: the sibling above would render the same floor as ``2.0``.
        """
        loud = self._said(self._bar(RatingSource.ROTTEN_TOMATOES_CRITIC, floor, 0))

        if shown is None:
            assert loud == []
        else:
            assert len(loud) == 1
            assert "protects almost everything" in loud[0]
            assert f"bar of {shown} " in loud[0]

    def test_a_high_percentage_bar_is_an_ordinary_setting_and_stays_quiet(self) -> None:
        """91 is loud out of ten and unremarkable as a percentage, so it separates the two
        arms in the direction the case above cannot: a mutant merging them fires here."""
        assert self._said(self._bar(RatingSource.ROTTEN_TOMATOES_CRITIC, 91, 0)) == []
        assert self._said(self._bar(RatingSource.IMDB, 91, self.VOTES)) != []

    @pytest.mark.parametrize("source", list(RatingSource), ids=[s.value for s in RatingSource])
    def test_every_source_names_itself_after_a_preposition_never_after_an_article(
        self, source: RatingSource
    ) -> None:
        """Each of these sentences places a source label, and the label has to fit where it
        lands.

        The cases above drive IMDb and Rotten Tomatoes critics only, which is the population
        that made "A IMDb bar of 9.6" read as correct English to everyone who checked it
        (#338): "A Rotten Tomatoes bar" is fine, and IMDb was never rendered in a case that
        asserted the article. ``UNKNOWN`` is the sharper one -- its label is the phrase "an
        unknown source", so the same sentence doubled the article into "A an unknown source
        bar" -- and it is reachable through the API even though the editor offers five
        sources.

        So this sweeps the whole enum rather than the two members that motivated the fix
        (rule 145), and pins the position contract ``ratings.SOURCE_LABELS`` documents: the
        label follows a preposition, so the article governs "bar" and cannot disagree with
        whatever the label turns out to be. A revert to the article form drops "on <label>"
        and fails here for every source at once.
        """
        pct = is_percentage_source(source)
        loud = self._said(self._bar(source, 19, 0 if pct else self.VOTES))

        assert len(loud) == 1
        shown = "19%" if pct else "1.9"
        assert loud[0].startswith(f"A bar of {shown} on {source_label(source)} ")

    @pytest.mark.parametrize(
        ("days", "span"),
        [
            (1, "1 day"),
            (8, "8 days"),
            (11, "11 days"),
            (18, "18 days"),
            (29, "29 days"),
            (30, None),
            (31, None),
        ],
    )
    def test_a_watch_window_reads_as_very_short_below_one_month(
        self, days: int, span: str | None
    ) -> None:
        """A long reach is stated so this is the only window warning in play: the same
        control feeds a shortfall lane that fires when the history is shallower.

        The sweep used to drive 29, 30 and 31 alone, which is why "A 8-day watch window"
        shipped (#338): every value that needs "An" sits below 29 and none was reachable
        from this table. The article now governs "watch window", a literal noun no value
        can disagree with, so what these cases pin is that the number stays out of its
        scope -- and 1 pins the singular the plural spelling would have gotten wrong.
        """
        body = _policy(
            gates=(GateSetting(gate=GateId.SERVER_POPULARITY, threshold=2, window_days=days),)
        )

        said = self._said(body, history_reach_days=800.0)

        assert bool(said) is (span is not None)
        if span is not None:
            assert f"A watch window of {span} is very short" in said[0]

    def test_a_very_short_window_on_a_gate_that_is_off_says_nothing(self) -> None:
        """The switch, not only the number. With the gate off the window falls back to the
        365-day default, so there is nothing on the page to warn about."""
        off = _policy(
            gates=(
                GateSetting(
                    gate=GateId.SERVER_POPULARITY, threshold=2, window_days=29, enabled=False
                ),
            )
        )

        assert self._said(off, history_reach_days=800.0) == []

    @pytest.mark.parametrize(("condemn_at", "loud"), [(29, True), (30, True), (31, False)])
    def test_the_threshold_that_condemns_almost_everything_stops_at_thirty(
        self, condemn_at: int, loud: bool
    ) -> None:
        """The one ``danger`` on this list, so severity is asserted rather than presence
        alone: demoting it to a ``warn`` is the same protection lost, more quietly."""
        flagged = inspect(_policy(condemn_at=condemn_at), ProfileSettings())

        assert bool(flagged) is loud
        if loud:
            assert flagged[0].field == "condemn_at"
            assert flagged[0].severity == "danger"
            assert f"A threshold of {condemn_at} condemns almost everything" in flagged[0].message


class TestADormancyFloorDeeperThanTheWatchHistory:
    """The root of the shallow-mirror family, and the one member that had no warning (#217).

    Dormancy is clamped to the mirror, so the most dormant any item can read IS the reach:
    ``dormancy.reference_instant`` measures from ``last_played``, else ``max(added_at,
    horizon)``, else nothing at all, and both measurable arms are at most the reach.
    ``MinDormancyGate`` PROTECTs anything under its threshold and
    PROTECT beats everything in ``decide_verdict``, so a floor above the reach holds the whole
    library on age alone until the mirror catches up. On the shipped 1095-day floor that is
    every operator with under three years of history.

    It is the root rather than a fifth member because the other four are all guarded on
    ``reach_clears_dormancy`` and go silent below the floor -- correctly, since their remedies
    would move no verdict there. The aggregate was a page that went quietest exactly where the
    list was emptiest.
    """

    FLOOR = 1095

    def _floored(self, threshold: int = FLOOR, *, enabled: bool = True) -> PolicyBody:
        return _policy(
            gates=(GateSetting(gate=GateId.MIN_DORMANCY, threshold=threshold, enabled=enabled),)
        )

    def _floor_warnings(self, body: PolicyBody, reach: float | None) -> list[PolicyWarning]:
        anchor = f"gates.{GateId.MIN_DORMANCY.value}.threshold"
        return [
            w
            for w in inspect(body, ProfileSettings(), history_reach_days=reach)
            if w.field == anchor
        ]

    def test_the_clamp_that_makes_the_claim_true(self) -> None:
        """Driven against the real derivation rather than restated (rule 119): the most dormant
        reading possible is the reach, whichever arm ``reference_instant`` takes."""
        now = datetime(2026, 7, 29, tzinfo=UTC)
        horizon = now - timedelta(days=90)

        # Never played, added long before the mirror begins: measured from the horizon.
        added_early = reference_instant(
            last_played=None, added_at=now - timedelta(days=5000), horizon=horizon
        )
        assert dormancy_days(added_early, now=now) == 90
        # Added after the horizon: measured from arrival, which is nearer still.
        added_late = reference_instant(
            last_played=None, added_at=now - timedelta(days=10), horizon=horizon
        )
        assert dormancy_days(added_late, now=now) == 10

    def test_a_floor_the_history_cannot_reach_is_flagged(self) -> None:
        [flagged] = self._floor_warnings(self._floored(), reach=90.0)

        assert flagged.severity == "warn"
        assert flagged.message.startswith("Nothing will be flagged for removal.")
        assert "3 years" in flagged.message
        assert "lower this wait" in flagged.message

    def test_a_single_unit_floor_keeps_its_number(self) -> None:
        """The shipped floor is three years, so it never exercised ``humanize_window``'s
        dropped "1" -- and this slot has no article to carry it. An operator who lowered the
        floor to a year read "Reaper waits year of no watching" (rule 21).

        Both settable single-unit floors, and there is no third: ``_protective_floors`` refuses
        a threshold under 5 days, so "1 day" is unreachable here (rule 141).
        """
        for threshold, expected in ((365, "1 year"), (30, "1 month")):
            [flagged] = self._floor_warnings(self._floored(threshold), reach=1.0)
            assert f"waits {expected} of no watching" in flagged.message

    def test_the_boundary_is_exact(self) -> None:
        """At the floor a title CAN read as dormant enough, so the claim stops being true."""
        assert len(self._floor_warnings(self._floored(), reach=float(self.FLOOR) - 1)) == 1
        assert self._floor_warnings(self._floored(), reach=float(self.FLOOR)) == []
        assert self._floor_warnings(self._floored(), reach=5000.0) == []

    def test_a_caller_that_cannot_read_the_mirror_stays_quiet(self) -> None:
        """The posture every lane here takes: a caller that cannot tell must not guess."""
        assert self._floor_warnings(self._floored(), reach=None) == []

    def test_a_disabled_floor_holds_nothing(self) -> None:
        """No gate, no protection, so no empty list to explain."""
        assert self._floor_warnings(self._floored(enabled=False), reach=90.0) == []

    def test_it_speaks_where_the_rest_of_the_family_is_silenced(self) -> None:
        """The reason this is the root, driven on the shipped policy rather than argued.

        Below the floor every other reach warning is deliberately quiet, so before this branch
        the page carried nothing at all while nothing in the library could be flagged. This is
        also why the two cannot stack: it fires on exactly the negation they are guarded on.
        """
        shallow = inspect(DEFAULT_MOVIE_POLICY, ProfileSettings(), history_reach_days=90.0)

        assert [w.field for w in shallow] == [f"gates.{GateId.MIN_DORMANCY.value}.threshold"]


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
        the same shortfall in front of the same operator, off the same
        ``gates.history_shortfall`` helper. Restating it here in different words would let
        the editor and the why panel describe one mirror two ways.

        If this fails, the two copies have drifted: fix them together, in
        ``engine/policy.py:inspect`` and ``engine/gates.py:ServerPopularityGate.evaluate``.

        **What it pins, stated exactly, because it was read as more than it is** (#243
        asked whether this is a tautological oracle, since it computes its expectation by
        calling the helper both sides call). It is not: rule 119 requires an agreement test
        to call the real function rather than transcribe it, and the shared value here is a
        whole sentence, so a reworded copy in ``inspect`` fails on the spot. But it pins
        AGREEMENT only -- reword ``history_shortfall`` and both sides move together, silently
        -- and it drives ONE blocked row, so it says nothing about the others (rule 145).
        The claim was "every blocked row", which is a population proven against one member;
        it now says what it drives.
        """
        reach = 90.0
        expected = history_shortfall(Known(value=reach, source="tautulli"), float(self.WINDOW))
        # Truthy, not merely non-None: "" is a legal str that every ``in`` below accepts, so
        # a helper degrading to an empty sentence would satisfy this test vacuously.
        assert expected

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
        assert "A watch window of 7 days is very short" not in message
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
        assert "A watch window of 7 days is very short" in short_only
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

    @pytest.mark.parametrize("seasons", [1, 2])
    def test_the_floor_counts_as_on_from_its_very_first_season(self, seasons: int) -> None:
        """One season is where the floor starts deciding things, and it was never driven:
        the cases here run 0 and 2, so ``keep_last_seasons > 0`` was free to become ``> 1``
        and go silent at exactly one season, where the scope quietly behaves as "all shows"
        (#337)."""
        warnings = inspect(
            self._tv(keep_last_seasons=seasons), ProfileSettings(), requests_app_configured=False
        )

        spoken = [w for w in warnings if w.field == "keep_last_scope"]
        assert len(spoken) == 1
        assert f"keeping the last {seasons} seasons of every show" in spoken[0].message

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

    ``schema_version`` is 2 because that is what an affected body actually carries: it was
    2 before the bar moved and the move did not bump it, which is why the shim keys on the
    absent key instead. Dumping the current version here made the restamp a no-op, so the
    test asserting it could not fail (rule 141).
    """
    raw = _policy(
        gates=(GateSetting(gate=GateId.RATING_FLOOR),),
        keep_rating_rules=(RatingRuleSpec(source=RatingSource.IMDB, floor=75, min_votes=1000),),
    ).model_dump(mode="json")
    del raw["keep_rating_rules"]
    raw["schema_version"] = 2
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

    def test_a_gate_row_carrying_no_switch_at_all_is_still_recovered(self) -> None:
        """Absent means on, module-wide: every switch is an explicit ``enabled: false``.

        A row a hand edit stripped the key from is an enabled gate holding a real bar, so
        reading it as disabled would skip the recovery and leave the operator's bar empty --
        the one outcome this shim exists to prevent (rule 105). Every fixture above reaches
        the switch through ``model_dump``, which always writes it, so nothing exercised the
        default until this case.
        """
        raw = _legacy_rating_body()
        del raw["gates"][0]["enabled"]

        restored = recover_rating_rules(raw)

        assert restored is not None
        assert restored["keep_rating_rules"] == [
            {"source": RatingSource.IMDB.value, "floor": 75, "min_votes": 1000}
        ]

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

    @pytest.mark.parametrize(
        "gate",
        [
            {"threshold": 1, "secondary": 1000},
            {"threshold": 100, "secondary": 1000},
            {"threshold": 75, "secondary": 1},
        ],
        ids=["lowest-floor", "highest-floor", "one-vote"],
    )
    def test_the_bars_the_old_validator_accepted_are_restored(
        self, gate: dict[str, object]
    ) -> None:
        """The inclusive edges of the accepted range, which the refusals above cannot pin.

        The old validator took ``1 <= threshold <= 100`` and ``secondary >= 1``, so every one
        of these is a bar an operator really could have saved. Refusing one is not a cosmetic
        miss: the bar stays empty, ``RatingFloorGate`` abstains on every item, and a
        protection the operator can still see configured holds nothing, on a healthy and
        executable snapshot. Testing only 0 and 101 left all three edges free to move.
        """
        restored = recover_rating_rules(_legacy_rating_body(**gate))

        assert restored is not None
        assert restored["keep_rating_rules"] == [
            {
                "source": RatingSource.IMDB.value,
                "floor": gate["threshold"],
                "min_votes": gate["secondary"],
            }
        ]

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
    """A new ``ReachSpan`` member has to be handled at five sites, and this is the alarm.

    Two of them ROUTE, and both now match member by member behind ``assert_never``:
    ``fields.reach_shortfall`` picks which bound the mirror is measured against, and
    ``inspect``'s lean loop files a graded keep under the span that blocks it. mypy fails
    on those first, so this test is for the author who skips that gate -- and for its own
    failure message, which is the one place all five sites are written down.

    Two more are membership tests, one per warning: an owner protect rule on the window
    (``owner_protect_on_window``) and the all-time rule below it. Each carries copy
    written for its own span, so neither can be generated from the set. A third member
    there is silence rather than a wrong answer, which is why they are named here instead
    of guarded in code.

    The fifth is ``inspect``'s condemn-lane sum, and it is the one that argument does NOT
    cover: it decides which weights are withheld, and both figures it withholds are printed
    to the operator. A span it does not know about is left out of a rendered count, so the
    warning under-reports the points to move or goes quiet on a list that really is empty --
    a wrong number in the reassuring direction, not silence (rules 103, 145).

    Rule 103's drift guard, and it was absent. Issue #168's probe measured what that cost:
    a third member added locally took ``reach_shortfall``'s ``else``, answered
    ITEM_LIFETIME's question instead of its own, and returned "no shortfall" for an item
    younger than the mirror against a window the mirror does not span -- the permissive
    direction, from the helper whose whole job is withholding unsupported counts. Nothing
    in the suite went red.
    """

    def test_the_two_spans_every_consumer_handles_are_the_two_that_exist(self) -> None:
        assert set(ReachSpan) == {ReachSpan.POPULARITY_WINDOW, ReachSpan.ITEM_LIFETIME}, (
            "A ReachSpan member was added or removed. Five sites decide what a span means "
            "and none of them can infer it: fields.reach_shortfall (which bound the mirror "
            "is measured against), policy.inspect's lean loop (which span a graded keep's "
            "discount is charged to), inspect's two protect membership tests "
            "(owner_protect_on_window, and the ITEM_LIFETIME branch under it), each of "
            "which needs operator copy written for that span, and inspect's condemn-lane "
            "sum (which weights are withheld), whose totals are printed to the operator so "
            "a missed span under-reports a number rather than going silent. Handle all "
            "five, then update this test."
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

    def _all_time_rule(self, *, op: Op = Op.GTE, count: int = 1) -> PolicyBody:
        """``count`` rules on the one field carrying this span, which is constructible: the
        span sits on the ``FieldSpec``, not on the value, so every distinct bar is another
        blocker. Defaults to one, because every case below but the counting pair asks about
        the branch and not about how many rules reached it."""
        return _policy(
            protect_conditions=tuple(
                ConditionSpec(field="watchers_all_time", op=op, value=i + 1) for i in range(count)
            )
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

    def test_it_does_not_tell_the_operator_to_wait_for_a_span_that_never_closes(self) -> None:
        """The remedy has to be one the operator can actually reach the end of.

        This lane's span is each item's AGE, and the reach grows at exactly the rate the age
        does, so the shortfall holds while ``added_at < horizon`` however long anyone waits. The
        clause survived here because it sat beside a remedy that does work, which is how an
        operator ends up taking the half that never resolves (rules 7/24, 21, 72). The season
        branch at the foot of ``inspect`` is the sibling, and carries no remedy at all.
        """
        [flagged] = self._warnings_on(self._all_time_rule(), "protect_conditions", reach=90.0)

        assert "Wait for it to build up" not in flagged.message
        assert flagged.message.endswith("Remove that rule if you want those titles judged.")

    def test_it_counts_the_rules_it_is_asking_the_operator_to_remove(self) -> None:
        """Issue #157, on the lane that never got the fix (rule 72).

        The window branch 40 lines above says why a singular "remove that rule" is not safe to
        leave implicit: two rules on one span are constructible, so removing one leaves the
        sentence byte-identical while a live protection is gone, and nothing says the pick was
        wrong. This branch answered on ``ReachSpan.ITEM_LIFETIME in protect_spans`` -- a SET of
        spans, which cannot say how many conditions produced it -- and printed the singular for
        any number of them.

        Swept rather than pinned at one number: the singular is a real arm and the plural has
        to hold past two, so the count is read out of the sentence for each (rule 141, since a
        fixture at the one value the old code got right cannot fail).
        """
        for count in (1, 2, 3, 7):
            [flagged] = self._warnings_on(
                self._all_time_rule(count=count), "protect_conditions", reach=90.0
            )

            if count == 1:
                assert "Your keep rule counts" in flagged.message
                assert flagged.message.endswith("Remove that rule if you want those titles judged.")
            else:
                assert f"Your {count} keep rules count" in flagged.message
                assert flagged.message.endswith(
                    "Remove those rules if you want those titles judged."
                )
            # Never the other number's sentence: the bug was one arm serving both.
            assert ("keep rules" in flagged.message) is (count > 1)

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

    def test_a_lifetime_keep_alone_is_not_told_to_wait(self) -> None:
        """ "Wait for it to build up" is false on an ``ITEM_LIFETIME`` span, so it is not offered.

        The reach is ``(now - horizon).days`` and an item's age is ``(now - added_at).days``, so
        both advance one day per day and the shortfall holds exactly while ``added_at < horizon``.
        Waiting cannot move a comparison of two fixed instants. The four members of this family
        that DO say it turn on a fixed span -- a window, a hold, the dormancy floor -- which a
        deepening mirror really does cover (rules 7/24, 21, 72).
        """
        body = _policy(graded_keeps=self._lean("watchers_all_time", 40).graded_keeps)
        [flagged] = self._warnings_on(body, "graded_keeps", reach=90.0)

        assert "Wait for it to build up" not in flagged.message
        # The remedy that does work still leads, and reads as a sentence.
        assert flagged.message.endswith("Set it to 30 points or less.")

    def test_a_window_keep_in_the_set_is_still_told_to_wait(self) -> None:
        """The discriminator for the test above, and why this is a condition rather than a
        deletion. A window shortfall clears as the mirror deepens, and clearing it drops those
        keeps out of the contributor list entirely, which is what can bring the total back under
        the headroom. So the clause is true exactly while a window keep is contributing.
        """
        body = _policy(graded_keeps=self._lean("recent_watchers", 40).graded_keeps)
        [flagged] = self._warnings_on(body, "graded_keeps", reach=90.0)

        assert "Wait for it to build up, or set it to 30 points or less." in flagged.message

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

    def _boolean(self, op: Op) -> PolicyBody:
        """The measured split, with the rule's operator as the only variable."""
        return _policy(
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
                        op=op,
                        value=1,
                        weight=35,
                    ),
                ),
            )
        )

    def test_a_boolean_rule_that_can_never_fire_is_in_the_sum(self) -> None:
        """The bound a per-item block still moves, which is the score and not coverage.

        A boolean rule is all-or-nothing, and under ``lte`` the outcome ``fields.evaluate``
        blocks is the MATCH. So an item under the bar earns nothing because the rule was
        blocked and one over it earns nothing because the rule did not fire: the weight is
        out of every item's score at once, even though the second item keeps it in coverage.
        Driven through the real scorer at a 90-day reach, watcher counts 0 through 50 all
        return ``abstain`` on a ceiling of 45 against the shipped threshold of 70, and all of
        them condemn once the mirror is deep. The list is empty, and this used to be silent.
        """
        [flagged] = self._condemn_warnings(self._boolean(Op.LTE))

        assert "55 of your 100 removal points" in flagged.message
        assert "45 points are left to judge on" in flagged.message

    def test_a_boolean_rule_that_can_still_fire_is_left_out(self) -> None:
        """The discriminator, and the reason the sum is not simply "weight on the field".

        Same rule, same weights, ``gte`` instead: now the blocked outcome is the one that
        does NOT match, so an item over the bar is settled by the truncated count, comes back
        evaluated, and earns the full 35. The list is genuinely not empty and claiming
        otherwise would be rule 144's reassuring direction. Coverage alone cannot tell these
        two apart, which is why ``fields.can_add_pressure_under_a_shortfall`` reads the op.
        """
        assert self._condemn_warnings(self._boolean(Op.GTE)) == []

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

    def _partial(self, op: Op, floor_bp: int) -> PolicyBody:
        """The split where the two items part company, with the op and the floor as the
        variables. No weight on ``FEW_WATCHERS``, so the boolean rule is the whole shortfall:
        every item's score ceiling is 80, over the threshold of 70, and the only thing left
        that can separate them is coverage."""
        return _policy(
            **self._gate_off(
                coverage_floor_bp=floor_bp,
                signals=(
                    SignalSetting(signal=SignalId.UNWATCHED, weight=70, saturate_at=730),
                    SignalSetting(signal=SignalId.LOW_RATING, weight=10, saturate_at=60),
                ),
                custom_condemn=(
                    BooleanCondemnSpec(
                        name="hardly watched",
                        field="recent_watchers",
                        op=op,
                        value=1,
                        weight=20,
                    ),
                ),
            )
        )

    def test_a_rule_for_unwatched_titles_abstains_exactly_those_titles(self) -> None:
        """Issue #215's partial case: the list is not empty, and what is missing from it is
        precisely what the operator wrote the rule to find.

        Under ``lte`` an item ABOVE the bar keeps the rule's weight in coverage, because a
        count already past the bar can only rise and ``_survives_more_history`` blocks only
        the outcome more history could overturn. The item AT OR UNDER it is blocked and loses
        that weight from coverage as well. Both ceilings are 80, so at a floor of 0.90 the
        popular title is judged on coverage 1.00 and the unwatched one abstains on 0.80. The
        removal list comes back full of the titles the rule was never meant to flag.
        """
        [flagged] = self._condemn_warnings(self._partial(Op.LTE, floor_bp=9000))

        assert flagged.field == "custom_condemn"
        assert flagged.severity == "warn"
        assert flagged.message.startswith("Your removal rule won't flag the titles it was written")
        # The other tier's claim, which is false here and must not be borrowed: this list has
        # titles in it.
        assert "Nothing will be flagged" not in flagged.message

    def test_the_partial_case_is_read_off_the_floor_and_not_off_the_rule(self) -> None:
        """The discriminator, swept across the boundary the coverage floor actually sets.

        Same rule and same weights every time. The blocked item's coverage is 0.80, so the
        two verdicts part company only once the floor is above that, and below it the rule
        costs those titles nothing a warning should mention. Firing on the rule's mere
        presence would put a notice on the shipped floor of 0.50, where nothing is wrong.
        """
        # At and under the blocked item's own coverage, both items condemn. 5000 is the
        # shipped default and is swept deliberately: it is the case an operator is most
        # likely to be in, and the one a rule-shaped check would get wrong (rule 141).
        for floor_bp in (5000, 7500, 8000):
            assert self._condemn_warnings(self._partial(Op.LTE, floor_bp)) == []

        # One basis point past it, the unwatched titles drop off the list.
        assert len(self._condemn_warnings(self._partial(Op.LTE, floor_bp=8001))) == 1

    def test_a_rule_that_can_still_fire_leaves_its_own_candidates_judged(self) -> None:
        """The op discriminator again, on the second tier this time (rule 72).

        Under ``gte`` the blocked outcome is the one that does NOT match, so an item over the
        bar is settled by the truncated count and earns the weight in full. Nothing is
        withheld from anybody's coverage, both items are judged alike, and a warning here
        would be rule 144's reassuring direction pointed the other way: it would send the
        operator to delete a rule that is working.
        """
        assert self._condemn_warnings(self._partial(Op.GTE, floor_bp=9000)) == []

    def test_the_two_tiers_never_both_fire(self) -> None:
        """They make opposite claims about the same list, so exactly one may be on the page.

        The measured #215 split empties the list outright, which is the first tier's claim;
        the split above leaves it populated, which is the second's. Anchored on the message
        rather than the count, because both tiers can land on ``custom_condemn``.
        """
        empty = self._condemn_warnings(self._boolean(Op.LTE))
        partial = self._condemn_warnings(self._partial(Op.LTE, floor_bp=9000))

        assert [w.message.startswith("Nothing will be flagged") for w in empty] == [True]
        assert [w.message.startswith("Nothing will be flagged") for w in partial] == [False]

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

    def test_a_split_lands_on_the_card_holding_more_of_them(self) -> None:
        """The case the two above cannot discriminate: both put the whole withheld weight on
        one card, so presence and magnitude agree and either rule passes them. Split unevenly
        and they diverge -- 5 points on the built-in beside 50 on the operator's own rule sent
        them to the slider holding 5 of the 55, and the card that has to change got nothing.
        """

        def anchor(built_in: int, rule: int) -> str:
            body = _policy(
                **self._gate_off(
                    signals=(
                        SignalSetting(
                            signal=SignalId.UNWATCHED, weight=100 - built_in - rule, saturate_at=730
                        ),
                        SignalSetting(signal=SignalId.FEW_WATCHERS, weight=built_in, saturate_at=3),
                    ),
                    custom_condemn=(
                        GradedCondemnSpec(
                            name="hardly watched",
                            field="recent_watchers",
                            weight=rule,
                            saturate_at=3,
                        ),
                    ),
                )
            )
            return self._condemn_warnings(body)[0].field

        assert anchor(built_in=5, rule=50) == "custom_condemn"
        assert anchor(built_in=50, rule=5) == "signals"
        # A tie goes to the signals card, which is the one further up the page.
        assert anchor(built_in=25, rule=25) == "signals"


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

    def test_a_single_unit_hold_keeps_its_number(self) -> None:
        """The dormancy floor's twin (rule 72): 180 is two units, so the shipped case never
        exercised ``humanize_window``'s dropped "1", and "for year after they last watched"
        has no article to carry it (rule 21).

        Driven on the lowest floor the gate accepts, since this branch needs a reach that
        clears the floor and still falls short of the hold. That is also why "1 day" has no
        case: any reach clearing a 5-day floor already spans a 1-day hold (rule 141).
        """
        floored = (GateSetting(gate=GateId.MIN_DORMANCY, threshold=5),)
        for hold, expected in ((365, "1 year"), (30, "1 month")):
            body = self._tv(in_progress_hold_days=hold, gates=floored)
            [flagged] = self._hold_warnings(body, reach=10.0)
            assert f"place for {expected} after they last watched" in flagged.message

    def test_a_one_day_hold_still_takes_the_shortfall_copy(self) -> None:
        """The boundary between the two cause clauses, one step below every case here.

        ``in_progress_hold_days <= 0`` was free to become ``<= 1``, which prints "at 0 days a
        viewer's place is held forever" about a hold of one day. Nothing caught it, because
        the only mirror short enough to fall behind a one-day hold is shorter than a day, and
        no other case wants one (#337). No dormancy floor here for the same reason: any floor
        the gate accepts needs a reach that already spans a day.

        Narrowing that same guard to ``== 0`` is left untested and is not a gap (rule 118):
        ``in_progress_hold_days`` is ``ge=0``, so no saveable value tells the two apart.
        """
        body = self._tv(in_progress_hold_days=1, gates=(GateSetting(gate=GateId.WHITELISTED),))

        [flagged] = self._hold_warnings(body, reach=0.5)

        assert "place for 1 day after they last watched" in flagged.message
        assert "held forever" not in flagged.message

    def test_the_journey_that_used_to_end_on_a_silent_page(self) -> None:
        """Clearing the window warning must not clear this one, which is exactly what the
        issue reported.

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


class TestAKeepRuleConflictTheWatchHistoryCannotSettle:
    """The FIFTH member of the family, and the one that was deferred rather than written (#224).

    ``season_pruning._detect_conflicts`` compares two ALL-TIME season watcher counts. A season
    the mirror does not reach back to the arrival of reports a lower bound, and more history can
    always lift a lower bound above anything, so every prunable season of such a show conflicts
    against every kept season whatever either count says. ``auto_approvable`` goes False and
    automatic TV pruning is inert on that show until the mirror catches up.

    ``test_a_short_mirror_holds_every_prunable_season_of_an_old_show`` pins that half in
    ``test_season_pruning``. This class pins the other half, which did not exist: the page said
    nothing at all. Driven on the shipped TV policy at a 1200-day reach against a 2000-day-old
    show, ``inspect`` returned no warning while every prunable season came back held.

    The deferral this replaces called it "the one member with no control behind it ... no setting
    the operator can reach". ``flag_keep_conflicts`` is a switch on this same editor, so that was
    false as written; ``test_the_detector_being_off_silences_it`` is the discriminator. What is
    true is narrower and is why no remedy names that switch: turning it off is the delete-more
    direction, so this family will not recommend it, and rendering beside it is as far as the
    prime directive goes.
    """

    #: The shipped floor is 1095 and dormancy is clamped to the mirror, so under it every item is
    #: kept on age alone and this family is correctly silent. 30 keeps the reach clear of it, so
    #: the conflict detector is what binds.
    DORMANCY = 30

    def _tv(self, **overrides: object) -> PolicyBody:
        base: dict[str, object] = {
            "media_type": "tv",
            "gates": (GateSetting(gate=GateId.MIN_DORMANCY, threshold=self.DORMANCY),),
            # The 90-day reach these tests use spans this hold, so the mid-binge lane is
            # establishable and quiet. Left at the shipped 180 it would hold every season on
            # disk, `prunable` would be empty, and it would silence this branch by design
            # (`test_the_mid_binge_hold_silences_it`) -- so every assertion here would be
            # reading that guard rather than the one under test (rule 141's shape).
            "in_progress_hold_days": 30,
        }
        return _policy(**{**base, **overrides})

    def _conflict_warnings(
        self, body: PolicyBody, reach: float | None = 90.0
    ) -> list[PolicyWarning]:
        return [
            w
            for w in inspect(body, ProfileSettings(), history_reach_days=reach)
            if w.field == "flag_keep_conflicts"
        ]

    def test_the_lane_is_flagged(self) -> None:
        [flagged] = self._conflict_warnings(self._tv())

        # `warn`, not `danger`: the outcome is that Reaper removes LESS, the keep direction.
        assert flagged.severity == "warn"

    def test_it_names_the_set_rather_than_claiming_an_empty_list(self) -> None:
        """The span this turns on is each item's AGE, which ``inspect`` is never handed.

        Same reason the ``watchers_all_time`` branch names its set: handed one reach and no
        arrival dates, "nothing will be flagged" would be false in the reassuring direction for
        a library the mirror covers outright (rules 7/24, 144).
        """
        [flagged] = self._conflict_warnings(self._tv())

        assert flagged.message.startswith("Seasons of shows added before your watch history")
        assert "Nothing will be flagged" not in flagged.message
        assert "No TV season will be flagged" not in flagged.message

    def test_it_says_where_the_held_shows_go(self) -> None:
        """They are not lost, and a warning that did not say so would read far worse than the
        truth. Every conflict carries ``shortfall``, so ``season_scan.guard_result`` marks each
        as a comparison Reaper did not make and the show waits in "Needs a look", where a hand
        reap still condemns it. That is the string the switch's own help text one row up already
        puts on the operator's screen (rule 144)."""
        [flagged] = self._conflict_warnings(self._tv())

        assert '"Needs a look"' in flagged.message

    def test_the_detector_being_off_silences_it(self) -> None:
        """The control the deferral said did not exist.

        ``plan_series_prune`` takes ``flag_keep_conflicts`` and returns no conflicts at all when
        it is off (``season_pruning`` line 494), so the keep rule is simply followed and there is
        no hold to explain. This is what makes the branch above a reading of the switch rather
        than a sentence that fires for every TV operator forever.
        """
        assert self._conflict_warnings(self._tv(flag_keep_conflicts=False)) == []
        # The discriminator: same policy with it on IS claimed, so the silence is the switch
        # being read and not the branch having gone quiet for some other reason.
        assert len(self._conflict_warnings(self._tv(flag_keep_conflicts=True))) == 1

    def test_the_movies_policy_never_speaks(self) -> None:
        """Movies have no seasons and never reach ``_detect_conflicts``, so a warning here would
        name a control the movie editor does not render (rule 25)."""
        assert self._conflict_warnings(_policy()) == []

    def test_a_caller_that_cannot_read_the_mirror_stays_quiet(self) -> None:
        """``requests_app_configured``'s posture: a guess would tell an operator their history is
        too shallow when it is fine."""
        assert self._conflict_warnings(self._tv(), reach=None) == []

    def test_the_dormancy_floor_silences_it(self) -> None:
        """The fifth lane takes the same guard as the other four, for the same reason: under the
        floor every item is kept on age alone, so this decides nothing and the #217 branch is the
        voice that speaks there instead. The two cannot stack."""
        floored = self._tv(gates=(GateSetting(gate=GateId.MIN_DORMANCY, threshold=1095),))

        assert self._conflict_warnings(floored, reach=90.0) == []

    def test_the_mid_binge_hold_silences_it(self) -> None:
        """Rule 143's shape, not tidiness: that guard drains the set this one is defined over.

        Where the mirror cannot establish the mid-binge hold, ``plan_series_prune`` holds every
        season ON DISK, so ``prunable`` is empty, ``_detect_conflicts`` iterates nothing, and
        this lane is never reached. Naming it there would assert a cause that is not operative,
        beside a branch already claiming the strictly stronger "no TV season will be flagged" --
        and two "wait for it to build up" paragraphs is the stacking #134 took out of this
        family.
        """
        held = self._tv(in_progress_hold_days=180)  # 90-day reach cannot span it
        fields = [w.field for w in inspect(held, ProfileSettings(), history_reach_days=90.0)]

        assert "in_progress_hold_days" in fields
        assert "flag_keep_conflicts" not in fields

        # The discriminator: switch that guard OFF and the set is no longer drained, so this
        # lane is live again and speaks. Without this the assertion above would also hold if the
        # branch had simply stopped firing.
        assert (
            self._conflict_warnings(self._tv(in_progress_hold_days=180, keep_in_progress=False))
            != []
        )

    def test_it_offers_no_remedy_rather_than_one_that_never_resolves(self) -> None:
        """Every other member of this family ends on "wait for it to build up". Here that would
        be false: the span is each item's AGE, and ``history_reach_days`` is ``(now - horizon)``
        while the age is ``(now - added_at)``, so both advance a day per day and the shortfall
        holds exactly while ``added_at < horizon``. Waiting moves neither instant. The sentence
        ends on where those shows go instead, which is true and is the only move there is: the
        one control behind this lane is the switch itself, and turning it off is the delete-more
        direction on two numbers Reaper knows are wrong (rules 7/24, 21).
        """
        [flagged] = self._conflict_warnings(self._tv())

        assert "Wait for it to build up" not in flagged.message
        assert flagged.message.endswith('marks those shows "Needs a look" instead of guessing.')

    def test_a_keep_rule_holding_no_season_silences_it(self) -> None:
        """The hold this claims has to be one the keep rule can actually produce.

        ``_detect_conflicts`` iterates ``prunable`` against ``kept_seasons``, and that list drops
        specials, so a policy keeping no season on age alone leaves it empty and the inner loop
        never runs: no conflict is raised however short the mirror is, and every one of those
        seasons is condemnable on score. Claiming the hold there is false in the REASSURING
        direction (rules 7/24, 144) -- the operator reads that old shows are waiting for them
        while the run is about to remove exactly those seasons.

        Driven against the real planner rather than an argument about it (rule 119).
        """
        seasons = [
            SeasonStats(
                season_number=n,
                monitored=True,
                episode_file_count=10,
                size_on_disk=1_000,
                total_episode_count=10,
                wanted_episode_count=0,
            )
            for n in range(1, 6)
        ]
        plan = plan_series_prune(
            series_title="Placeholder Show",
            seasons=seasons,
            keep_last=0,
            keep_first_season=False,
            flag_keep_conflicts=True,
            watchers_by_season=dict.fromkeys(range(1, 6), 3),
            # Every season's count is a lower bound, which is the state this warning is about.
            shortfall_by_season=dict.fromkeys(
                range(1, 6), "mirror starts after this season arrived"
            ),
        )

        # The premise: nothing to compare against, so the detector raises nothing at all and the
        # whole show is auto-approvable despite every count being unsupported.
        assert [p.season_number for p in plan.protected] == []
        assert plan.conflicts == []
        assert plan.auto_approvable

        assert self._conflict_warnings(self._tv(keep_last_seasons=0, keep_first_season=False)) == []

        # The discriminators: either rule alone puts a season back in ``kept_seasons``, so the
        # comparison exists again and the lane is live. Without these the silence above would
        # also read green if the branch had simply stopped firing.
        assert self._conflict_warnings(self._tv(keep_last_seasons=1, keep_first_season=False)) != []
        assert self._conflict_warnings(self._tv(keep_last_seasons=0, keep_first_season=True)) != []

    def test_the_shipped_tv_policy_is_no_longer_silent_on_a_deep_mirror(self) -> None:
        """The reported state, driven on the shipped policy with nothing lowered.

        1200 days clears the shipped 1095-day floor, spans the 365-day popularity window and the
        180-day mid-binge hold, and carries no ``watchers_all_time`` rule or graded keep, so all
        four siblings and the #217 root warning are correctly silent. That is precisely the state
        in which ``inspect`` used to return NOTHING while every prunable season of any show older
        than the mirror came back held.
        """
        warnings = inspect(DEFAULT_TV_POLICY, ProfileSettings(), history_reach_days=1200.0)

        assert [w.field for w in warnings] == ["flag_keep_conflicts"]


class TestTheUnmeasuredAllowanceWarning:
    """The ``max_unmeasured_per_run`` lane, which had no test at any point (#337).

    Nothing in tests/ named this message, so ``> 0`` inverted, or the guard deleted, removed
    the warning outright and the mutation run never noticed. The direction that costs the
    operator is the warning going silent: an operator raising the allowance is then never
    told the size caps cannot cover those items, which is the one fact that makes the
    setting understandable.
    """

    def _spoken(self, allowance: int) -> list[PolicyWarning]:
        return [
            w
            for w in inspect(_policy(), ProfileSettings(max_unmeasured_per_run=allowance))
            if w.field == "max_unmeasured_per_run"
        ]

    def test_the_default_allowance_of_zero_says_nothing(self) -> None:
        """Nothing unmeasured is deleted, so there is nothing to warn about. This is the
        discriminator: without it the case below is satisfied by a warning that always
        fires."""
        assert self._spoken(0) == []

    @pytest.mark.parametrize("allowance", [1, 2, 10])
    def test_it_starts_at_the_first_item_allowed_through(self, allowance: int) -> None:
        """One is where it turns on, not some larger number, and the count is rendered.

        10 is the highest the default profile permits: the allowance may not exceed
        ``max_items_per_run``, which ships at 10. The sweep therefore runs from the first
        legal value to the last, and excludes the default 0 deliberately, since that is the
        silent arm above (rule 141).
        """
        spoken = self._spoken(allowance)

        assert len(spoken) == 1
        assert spoken[0].severity == "warn"
        assert f"delete up to {allowance} items it can't measure" in spoken[0].message
        assert "GB caps won't cover them" in spoken[0].message


class TestTheTwoSizeFootguns:
    """Removing titles for being large, through a hand-written rule and through the built-in
    signal. Both lanes had no test at any point (#337).

    Deleting either guard dropped a danger silently, and moving a weight comparison the other
    way raised a danger about a size rule at weight 0 -- which the field's own docstring says
    is off, its weight out of the denominator entirely.
    """

    def _rule(self, weight: int) -> PolicyBody:
        return _policy(
            signals=(
                SignalSetting(signal=SignalId.UNWATCHED, weight=100 - weight, saturate_at=730),
            ),
            custom_condemn=(
                GradedCondemnSpec(
                    name="the big ones", field="size_bytes", weight=weight, saturate_at=10
                ),
            ),
        )

    def _signal(self, weight: int) -> PolicyBody:
        return _policy(
            signals=(
                SignalSetting(signal=SignalId.UNWATCHED, weight=100 - weight, saturate_at=730),
                SignalSetting(signal=SignalId.SIZE, weight=weight, saturate_at=10),
            )
        )

    @pytest.mark.parametrize("weight", [1, 40])
    def test_a_hand_written_size_rule_is_a_danger_at_any_live_weight(self, weight: int) -> None:
        """One point is where it turns on. The rule is named, because the operator typed
        that name and it is what they have to find on the card."""
        spoken = [
            w for w in inspect(self._rule(weight), ProfileSettings()) if w.severity == "danger"
        ]

        assert len(spoken) == 1
        assert spoken[0].field == "custom_condemn"
        assert '"the big ones" removes things for being large' in spoken[0].message
        assert "not whether anyone wants the title" in spoken[0].message

    def test_a_size_rule_at_weight_zero_is_off_and_says_nothing(self) -> None:
        """Weight 0 removes the rule's weight from the denominator, so it adds no pressure at
        all. Warning about it sends the operator to a control that is already doing nothing."""
        assert [
            w for w in inspect(self._rule(0), ProfileSettings()) if w.severity == "danger"
        ] == []

    @pytest.mark.parametrize("weight", [1, 40])
    def test_the_built_in_size_signal_is_a_danger_at_any_live_weight(self, weight: int) -> None:
        """The same footgun through the slider, which had no warning at all while the
        hand-written equivalent got a danger one, and whose docstring claimed the UI warned
        about it (rule 24). The rendered weight is asserted, not just the sentence."""
        spoken = [
            w for w in inspect(self._signal(weight), ProfileSettings()) if w.severity == "danger"
        ]

        assert len(spoken) == 1
        assert spoken[0].field == "signals"
        assert f'"Large files" is adding {weight} points toward removal' in spoken[0].message

    def test_the_built_in_size_signal_at_weight_zero_says_nothing(self) -> None:
        assert [
            w for w in inspect(self._signal(0), ProfileSettings()) if w.severity == "danger"
        ] == []


class TestTheKeepLastSeasonsWarning:
    """ "Keeping the last N seasons ... TV pruning is effectively off". No test named this
    message, and all six of its mutants survived (#337)."""

    def _spoken(self, body: PolicyBody) -> list[PolicyWarning]:
        return [w for w in inspect(body, ProfileSettings()) if w.field == "keep_last_seasons"]

    @pytest.mark.parametrize(("seasons", "spoken"), [(9, False), (10, True), (11, True)])
    def test_ten_is_where_pruning_is_effectively_off(self, seasons: int, spoken: bool) -> None:
        """Nine is a floor somebody may well mean; ten is where most shows are covered
        entirely. ``>=`` to ``>`` moved that edge by one and nothing said so."""
        warnings = self._spoken(_policy(media_type="tv", keep_last_seasons=seasons))

        if not spoken:
            assert warnings == []
        else:
            assert len(warnings) == 1
            assert warnings[0].severity == "warn"
            assert f"Keeping the last {seasons} seasons" in warnings[0].message
            assert "TV pruning is effectively off" in warnings[0].message

    def test_a_movie_policy_never_raises_it(self) -> None:
        """The media-type test was free to invert: ``==`` to ``!=`` tells a movie operator
        their season floor has switched TV pruning off, on a policy with no seasons in it."""
        assert self._spoken(_policy(media_type="movie", keep_last_seasons=20)) == []


class TestTheKeepPointsTotalWarning:
    """ "Your keep rules can subtract up to N points". ``>=`` to ``==`` survived, because the
    only case that fired it sat exactly on the boundary: ``_lean(..., 70)`` against the
    shipped ``condemn_at`` of 70 (#337, and the same shape as the four rows of #243).
    """

    def _keeps(self, *discounts: int, condemn_at: int = 70) -> list[PolicyWarning]:
        return [
            w
            for w in inspect(
                _policy(
                    condemn_at=condemn_at,
                    graded_keeps=tuple(
                        GradedKeepSpec(
                            name=f"keep {i}",
                            field="imdb_rating",
                            max_discount=d,
                            floor=0,
                            saturate_at=10,
                        )
                        for i, d in enumerate(discounts)
                    ),
                ),
                ProfileSettings(),
            )
            if w.field == "graded_keeps"
        ]

    @pytest.mark.parametrize(("total", "spoken"), [(69, False), (70, True), (80, True)])
    def test_it_fires_at_the_threshold_and_above_it(self, total: int, spoken: bool) -> None:
        """80 against a threshold of 70 is the case ``==`` let through: keeps well past the
        threshold, warning about nothing. ``imdb_rating`` carries no reach span, so the lean
        lane cannot answer for this anchor instead."""
        warnings = self._keeps(total)

        if not spoken:
            assert warnings == []
        else:
            assert len(warnings) == 1
            assert f"subtract up to {total} points" in warnings[0].message
            assert "remove threshold of 70" in warnings[0].message

    def test_the_total_is_summed_across_rules(self) -> None:
        """Two keeps of 40 reach the same threshold one keep of 80 does: ``score()``
        subtracts the sum, so testing each rule alone leaves the arity-two case silent."""
        assert "subtract up to 80 points" in self._keeps(40, 40)[0].message


class TestWhoTheEmptyListWarningBlamesAndWhereItSendsThem:
    """The condemn lane under a short mirror: which weight counts as watcher-dependent, what
    happens when a blocked boolean rule stands alone, and which card the operator is sent to.

    Every fixture in this lane used ``recent_watchers`` with a ``FEW_WATCHERS`` weight beside
    it, so the span filter itself was never driven and the boolean-alone state was never
    built (#337). Three consequences, all of them silent: a rule on a field that reads no
    watchers counted toward an inflated point total, a blocked boolean rule on its own emptied
    the reap list and said nothing, and the anchor pointed at whichever card the fixtures
    happened to agree on.

    90 days against the 365-day window is the shortfall throughout, and no dormancy floor is
    set, so ``reach_clears_dormancy`` holds and the lane speaks.

    **What this lane deliberately leaves untested, and why** (rule 118). Three constructs
    here cannot be told apart by any input, so a case for them would be a proof of nothing:

    * ``weight`` is ``ge=0`` at the save boundary, so ``> 0`` widened to ``>= 0`` and
      ``<= 0`` narrowed to ``< 0`` or ``== 0`` are the same test over every value that can
      be saved.
    * ``condemn_spec is None`` is unreachable: both condemn specs refuse an unknown field at
      construction, so the ``or`` in front of it never decides anything.
    * ``withheld > 0 or never_earned > 0`` widened to ``>=`` admits only the all-zero case,
      where the ceiling is the full 100 points and the verdict is ``condemn`` regardless, so
      the branch it lets through is silent anyway.
    """

    REACH = 90.0

    def _spoken(self, body: PolicyBody) -> list[PolicyWarning]:
        return [
            w
            for w in inspect(body, ProfileSettings(), history_reach_days=self.REACH)
            if w.message.startswith("Nothing will be flagged for removal.")
        ]

    def _blocked_boolean(
        self, weight: int = 40, rules: int = 1, condemn_at: int = 70
    ) -> PolicyBody:
        """A boolean removal rule that can never earn its weight under a shortfall, and NO
        ``FEW_WATCHERS`` weight beside it -- which is the state no fixture built.

        ``lte`` is the operator that cannot: a match is exactly the outcome more history
        could overturn, so ``evaluate`` blocks it, and no item earns the weight at all.
        """
        each = weight // rules
        return _policy(
            condemn_at=condemn_at,
            signals=(
                SignalSetting(signal=SignalId.UNWATCHED, weight=100 - weight, saturate_at=730),
            ),
            custom_condemn=tuple(
                BooleanCondemnSpec(
                    name=f"quiet {i}", field="recent_watchers", op=Op.LTE, value=2 + i, weight=each
                )
                for i in range(rules)
            ),
        )

    def test_a_blocked_boolean_rule_on_its_own_still_empties_the_list_out_loud(self) -> None:
        """No withheld weight at all, and 40 points that no item can earn: the score ceiling
        is 60 against a threshold of 70, so the reap list is empty. Three mutants suppressed
        the warning for exactly this state, because the second half of ``withheld > 0 or
        never_earned > 0`` had nothing driving it."""
        spoken = self._spoken(self._blocked_boolean())

        assert len(spoken) == 1
        assert "40 of your 100 removal points" in spoken[0].message
        assert "only 60 points are left to judge on" in spoken[0].message

    def test_it_sends_a_blocked_boolean_rule_to_the_card_that_holds_it(self) -> None:
        """None of the weight is on the signals card, so the operator goes to the rules card.

        This is what ``withheld + never_earned`` is for: flipping the ``+`` to a ``-`` makes
        the comparison ``0 >= -40``, true, and sends the operator to a slider that holds none
        of the points they have to move.
        """
        assert self._spoken(self._blocked_boolean())[0].field == "custom_condemn"

    def test_the_plural_turns_on_at_the_second_blocked_rule(self) -> None:
        """One rule, then two, on the PARTIAL branch: the list is not empty, and the titles
        missing from it are the ones the rules were written to find.

        ``never_earned_rules > 1`` was free to become ``< 1`` or ``> 2`` because no case ever
        built the second rule. This branch names no rule -- two rules on one field are
        constructible, so a singular would read as a wrong instruction -- which leaves the
        count as the only thing carrying the plural.

        60 blocked points against a threshold of 40 is what reaches this arm: the score
        ceiling still clears the threshold, so the list is genuinely not empty, while the
        coverage those points take with them falls to 40% against the 50% floor.
        """

        def said(rules: int) -> list[str]:
            return [
                w.message
                for w in inspect(
                    self._blocked_boolean(weight=60, rules=rules, condemn_at=40),
                    ProfileSettings(),
                    history_reach_days=self.REACH,
                )
            ]

        assert any("Your removal rule won't flag the titles it was" in m for m in said(1))
        assert any("Your removal rules won't flag the titles they were" in m for m in said(2))

    def _split_across_both_cards(
        self, on_the_slider: int, on_a_rule: int, condemn_at: int = 70
    ) -> PolicyBody:
        return _policy(
            condemn_at=condemn_at,
            signals=(
                SignalSetting(
                    signal=SignalId.UNWATCHED,
                    weight=100 - on_the_slider - on_a_rule,
                    saturate_at=730,
                ),
                SignalSetting(signal=SignalId.FEW_WATCHERS, weight=on_the_slider, saturate_at=3),
            ),
            custom_condemn=(
                GradedCondemnSpec(
                    name="quiet ones", field="recent_watchers", weight=on_a_rule, saturate_at=5
                ),
            ),
        )

    def test_the_anchor_is_decided_by_the_share_and_not_by_an_offset(self) -> None:
        """One point below the tie, which is the only place the slider's running total can be
        read as a number rather than as a comparison.

        24 against 26 sits a single point under half: the rules card holds more, so that is
        where the operator goes. Starting the tally at 1 instead of 0 tips exactly this split
        and nothing else, which is why every other case here agrees with it.
        """
        spoken = self._spoken(self._split_across_both_cards(24, 26))

        assert len(spoken) == 1
        assert spoken[0].field == "custom_condemn"

    def test_the_list_empties_on_lost_coverage_as_well_as_on_lost_points(self) -> None:
        """The other half of what ``decide_verdict`` is asked here, and the half no case
        drove: 40 points left against a threshold of 31 CLEARS the threshold, and the reap
        list is still empty because the coverage those 60 withheld points took with them
        falls to 40% against the 50% floor.

        Only the coverage arm can say so. Turning the share into a product makes the coverage
        figure enormous, every item reads as fully covered, and the operator is told their
        rules merely miss some titles instead of that nothing will be flagged at all.
        """
        spoken = self._spoken(self._split_across_both_cards(60, 0, condemn_at=31))

        assert len(spoken) == 1
        assert spoken[0].field == "signals"
        assert "60 of your 100 removal points" in spoken[0].message
        assert "only 40 points are left to judge on" in spoken[0].message

    def test_a_single_withheld_point_is_still_a_withheld_point(self) -> None:
        """The lowest live weight there is, on both routes into the total.

        Every ``> 0`` guard in this lane was free to become ``> 1``, and a one-point weight
        is the only value that tells the two apart. It needs a threshold of 100 to be
        visible: below that, one point of ceiling is never what stops an item condemning, so
        the warning has nothing to say either way (rule 141).
        """
        slider = self._split_across_both_cards(1, 0, condemn_at=100)
        rule = self._blocked_boolean(weight=1, condemn_at=100)

        assert [w.field for w in self._spoken(slider)] == ["signals"]
        assert [w.field for w in self._spoken(rule)] == ["custom_condemn"]
        assert all("1 of your 100 removal points" in w.message for w in self._spoken(slider))
        assert all("only 99 points are left to judge on" in w.message for w in self._spoken(rule))

    def test_the_operator_is_sent_to_whichever_card_holds_more_of_the_weight(self) -> None:
        """25 on the built-in slider against 40 on a custom rule: the rules card holds more,
        so that is where the points have to move (rule 42).

        The shipped fixtures were 5/50, 50/5 and 25/25, which agree whether the comparison
        doubles the signals share or triples it. This split does not: doubling gives 50
        against 65 and points at the rules, tripling gives 75 and points at the slider.
        """
        body = _policy(
            signals=(
                SignalSetting(signal=SignalId.UNWATCHED, weight=35, saturate_at=730),
                SignalSetting(signal=SignalId.FEW_WATCHERS, weight=25, saturate_at=3),
            ),
            custom_condemn=(
                GradedCondemnSpec(
                    name="quiet ones", field="recent_watchers", weight=40, saturate_at=5
                ),
            ),
        )

        spoken = self._spoken(body)

        assert len(spoken) == 1
        assert spoken[0].field == "custom_condemn"
        assert "65 of your 100 removal points" in spoken[0].message

    def test_a_rule_that_reads_no_watchers_is_not_counted_as_watcher_dependent(self) -> None:
        """The span filter itself, which no fixture drove: every rule in this lane was on
        ``recent_watchers``, so dropping the ``ReachSpan.POPULARITY_WINDOW`` test changed
        nothing any test could see.

        Without it a positive-weight rule on ANY field counts toward the withheld total, and
        an operator whose rules never look at watchers is told their reap list is empty
        because of a watch window. ``days_unwatched`` carries no reach span at all, so no
        amount of missing history bounds it.
        """
        body = _policy(
            signals=(SignalSetting(signal=SignalId.UNWATCHED, weight=60, saturate_at=730),),
            custom_condemn=(
                GradedCondemnSpec(
                    name="long unwatched", field="days_unwatched", weight=40, saturate_at=900
                ),
            ),
        )

        assert self._spoken(body) == []


class TestTheGradedKeepRemedyReadsAsASentence:
    """The remedy the lean-lane warning ends on, and the sentence in front of it.

    The tests on this lane assert its first and last sentences, both literals, and never the
    middle one, so the capitalization that joins them was free to drop or to re-slice --
    rendering "...won't be flagged for removal. your keep rule ..." (or YOur, our, Yur) with
    nothing failing. The remedy's own boundary was undriven too: the only no-headroom case
    used ``condemn_at=100``, so ``headroom < 1`` and the point/points plural both sat one
    step outside every case (#337).

    The ``total`` initializer above these branches is left untested and is not a gap (rule
    118): every branch that reads it assigns it first, so its starting value is dead.
    """

    def _keep(self, field: str, discount: int, condemn_at: int = 70) -> PolicyBody:
        return _policy(
            condemn_at=condemn_at,
            graded_keeps=(
                GradedKeepSpec(
                    name="my lean", field=field, max_discount=discount, floor=0, saturate_at=10
                ),
            ),
        )

    def _said(self, body: PolicyBody) -> str:
        spoken = [
            w
            for w in inspect(body, ProfileSettings(), history_reach_days=90.0)
            if w.field == "graded_keeps"
        ]
        assert len(spoken) == 1
        return spoken[0].message

    def test_the_lifetime_only_branch_renders_as_three_whole_sentences(self) -> None:
        """The exact message, because the middle sentence is assembled rather than written.

        Its lead clause is absent on this branch -- the cause clause belongs to the window
        branch -- so the sentence starts on "your" and is capitalized in code. Asserting the
        whole string is what pins that: a substring check on either end holds while the join
        between them renders lowercase.
        """
        assert self._said(self._keep("watchers_all_time", 40)) == (
            "Titles added before your watch history starts won't be flagged for removal. "
            'Your keep rule "my lean" takes all 40 of its points off them. '
            "Set it to 30 points or less."
        )

    @pytest.mark.parametrize(
        ("condemn_at", "remedy"),
        [
            (100, "remove that rule."),
            (99, "set it to 1 point or less."),
            (98, "set it to 2 points or less."),
            (70, "set it to 30 points or less."),
        ],
    )
    def test_the_remedy_names_a_number_the_control_will_actually_take(
        self, condemn_at: int, remedy: str
    ) -> None:
        """``max_discount`` is ``ge=1``, so at a headroom of 0 every settable value is too
        high and the remedy has to drop to "remove that rule".

        One point of headroom is the case nothing drove, and it decides two things at once:
        whether a number may be named at all, and whether it is "1 point" or "1 points". At
        98 the operator was being told to set a rule to "2 point or less".
        """
        assert self._said(self._keep("recent_watchers", 40, condemn_at=condemn_at)).endswith(
            f"Wait for it to build up, or {remedy}"
        )


class TestConvertListProtections:
    """The load shim for a body from before every list protected through its own keep rule.

    Three legacy shapes leave together because they were saved together: ``keep_tags`` and
    its match mode, the ``whitelisted``/``curated_list`` gate rows, and rules spelled
    ``on_curated_list``. Each ENABLED gate was a LIVE protection, so it converts to the
    equivalent ``on_list`` rule rather than being dropped -- dropping alone would silently
    withdraw cover, and a target list that cannot be resolved keeps its row so the scan
    refuses loudly instead (rule 38).
    """

    @staticmethod
    def _legacy(**over: object) -> dict[str, object]:
        body = json.loads(DEFAULT_MOVIE_POLICY.model_dump_json())
        body["protect_conditions"] = []
        body["keep_tags"] = ["reaper-keep"]
        body["keep_tags_match"] = "any"
        body["gates"] = [
            {"gate": "whitelisted", "enabled": True},
            {"gate": "curated_list", "enabled": True},
            *body["gates"],
        ]
        body.update(over)
        return body

    @staticmethod
    def _convert(
        raw: object,
        *,
        tag: str | None = "Tagged",
        imdb: str | None = "Top",
        collections: tuple[str, ...] = (),
    ) -> dict[str, object] | None:
        return convert_list_protections(
            raw,
            tag_list_name=tag,
            imdb_list_name=imdb,
            collection_list_names=collections,
        )

    def test_every_curated_by_hand_list_gets_its_own_rule(self) -> None:
        """The whitelisted gate covered the Plex collection and watchlist definitions as
        much as the keep tags, so each existing one converts to its own rule -- or an
        upgrade would quietly withdraw the collections' cover (rule 118 for the new arm)."""
        converted = self._convert(self._legacy(), collections=("Never Reap", "My watchlist"))
        assert converted is not None
        assert self._rules(converted) == ["Tagged", "Never Reap", "My watchlist", "Top"]
        assert "whitelisted" not in self._gate_ids(converted)

    @staticmethod
    def _rules(body: dict[str, object]) -> list[str]:
        return [
            str(c["value"])
            for c in body.get("protect_conditions") or []  # type: ignore[union-attr]
            if isinstance(c, dict) and c.get("field") == "on_list"
        ]

    @staticmethod
    def _gate_ids(body: dict[str, object]) -> set[str]:
        return {str(g.get("gate")) for g in body.get("gates") or [] if isinstance(g, dict)}  # type: ignore[union-attr]

    def test_enabled_gates_become_rules_and_the_keys_leave(self) -> None:
        converted = self._convert(self._legacy())

        assert converted is not None
        assert "keep_tags" not in converted and "keep_tags_match" not in converted
        assert self._rules(converted) == ["Tagged", "Top"]
        assert not self._gate_ids(converted) & {"whitelisted", "curated_list"}
        # ...and the whole result still loads.
        assert PolicyBody.model_validate(converted)

    def test_a_disabled_gate_converts_to_no_rule_and_still_leaves(self) -> None:
        """The operator had the protection off; the Lists screen now says "not used by
        your policy" where the switch used to be."""
        legacy = self._legacy()
        for gate in legacy["gates"]:  # type: ignore[union-attr]
            if gate["gate"] in ("whitelisted", "curated_list"):
                gate["enabled"] = False

        converted = self._convert(legacy)

        assert converted is not None
        assert self._rules(converted) == []
        assert not self._gate_ids(converted) & {"whitelisted", "curated_list"}

    def test_an_explicitly_empty_keep_tag_list_converts_to_no_tag_rule(self) -> None:
        """Rule 1: an operator who cleared their tags had no live tag protection to carry
        over. The curated gate's rule still lands."""
        converted = self._convert(self._legacy(keep_tags=[]))

        assert converted is not None
        assert self._rules(converted) == ["Top"]

    def test_an_enabled_gate_with_no_target_list_keeps_its_row(self) -> None:
        """Stripping an enabled gate whose replacement cannot be named would withdraw a
        live protection with nothing in its place. The row stays, and ``build_gates``
        refuses the scan on it rather than silently skipping (rule 38)."""
        converted = self._convert(self._legacy(), tag=None, imdb=None)

        assert converted is not None
        assert self._rules(converted) == []
        assert {"whitelisted", "curated_list"} <= self._gate_ids(converted)
        with pytest.raises(ScanConfigError, match="whitelisted"):
            build_gates(PolicyBody.model_validate(converted))

    def test_on_curated_list_rules_respell_as_on_list(self) -> None:
        legacy = json.loads(DEFAULT_MOVIE_POLICY.model_dump_json())
        legacy["protect_conditions"] = [
            {"field": "on_curated_list", "op": "eq", "value": "Awards Shortlist"}
        ]

        converted = self._convert(legacy)

        assert converted is not None
        assert self._rules(converted) == ["Awards Shortlist"]
        assert PolicyBody.model_validate(converted)

    def test_a_rule_already_naming_the_list_is_not_doubled(self) -> None:
        """Matched case-folded (rule 88): a body that already carries the rule the gate
        would convert to gains nothing beside it."""
        legacy = self._legacy(
            protect_conditions=[{"field": "on_list", "op": "eq", "value": "  tagged "}]
        )

        converted = self._convert(legacy)

        assert converted is not None
        assert self._rules(converted) == ["  tagged ", "Top"]

    def test_it_is_idempotent_on_a_converted_body(self) -> None:
        converted = self._convert(self._legacy())
        assert converted is not None

        assert self._convert(converted) is None
        assert (
            convert_list_protections(
                json.loads(DEFAULT_MOVIE_POLICY.model_dump_json()),
                tag_list_name="Tagged",
                imdb_list_name="Top",
            )
            is None
        )

    @pytest.mark.parametrize("raw", [None, "policy", 7, ["keep_tags"]])
    def test_anything_but_an_object_converts_to_nothing(self, raw: object) -> None:
        assert self._convert(raw) is None

    def test_the_trigger_matches_the_conversion(self) -> None:
        """``has_legacy_list_protections`` is what callers use to decide whether resolving
        the list names is worth a database read, so it must fire exactly when the
        conversion would."""
        assert has_legacy_list_protections(self._legacy()) is True
        assert (
            has_legacy_list_protections(
                {"protect_conditions": [{"field": "on_curated_list", "op": "eq", "value": "A"}]}
            )
            is True
        )
        assert (
            has_legacy_list_protections(json.loads(DEFAULT_MOVIE_POLICY.model_dump_json())) is False
        )
        assert has_legacy_list_protections("policy") is False
