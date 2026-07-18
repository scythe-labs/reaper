# SPDX-License-Identifier: AGPL-3.0-or-later
"""User-authored custom condemn rules -- unsigned, normalized, and fail-closed.

A custom "remove" rule is a signal, never a gate: it can only ever add condemnation
pressure, its weight joins the same fixed denominator as the built-in signals, and an
Unknown input contributes nothing while keeping its weight -- so a custom rule can only
push a score DOWN on missing data, exactly like a built-in one.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from reaper.engine.fields import Condition, Op
from reaper.engine.gates import Facts
from reaper.engine.observation import Absent, Known, Unknown
from reaper.engine.policy import (
    BooleanCondemnSpec,
    GradedCondemnSpec,
    GradedKeepSpec,
    PolicyBody,
    PolicyWarning,
    ProfileSettings,
    SignalSetting,
    inspect,
)
from reaper.engine.signals import (
    CustomSignalConfig,
    KeepConfig,
    SignalConfig,
    SignalId,
    evaluate_custom,
    evaluate_keep,
    score,
)


def _facts(**overrides: object) -> Facts:
    base: dict[str, object] = {
        "title": "A Film",
        "days_observed_unwatched": Known(value=400.0, source="tautulli"),
        "distinct_watchers": Known(value=0, source="tautulli"),
        "distinct_watchers_all_time": Known(value=6, source="tautulli"),
        "size_bytes": Known(value=8_000_000_000, source="radarr"),
        "imdb_rating_tenths": Known(value=73, source="imdb"),
        "imdb_votes": Known(value=900_000, source="imdb"),
        "season_rank": Absent(source="radarr"),
        "is_streaming_now": Known(value=False, source="tautulli"),
        "is_managed": Known(value=True, source="radarr"),
        "in_curated_list": Absent(source="lists"),
        "is_whitelisted": Known(value=False, source="plex"),
        "others_watching": Absent(source="tautulli"),
    }
    return Facts(**{**base, **overrides})  # type: ignore[arg-type]


def _policy(**over: object) -> PolicyBody:
    base: dict[str, object] = {
        "media_type": "movie",
        "condemn_at": 70,
        "gates": (),
        "signals": (
            SignalSetting(signal=SignalId.UNWATCHED, weight=70, saturate_at=1825, floor=365),
        ),
    }
    return PolicyBody(**{**base, **over})  # type: ignore[arg-type]


def _boolean(**over: object) -> CustomSignalConfig:
    params: dict[str, object] = {
        "name": "junk",
        "weight": 30,
        "kind": "boolean",
        "field": "genre",
        "condition": Condition(field="genre", op=Op.CONTAINS, value="reality"),
    }
    return CustomSignalConfig(**{**params, **over})  # type: ignore[arg-type]


class TestEvaluateCustom:
    def test_a_matched_boolean_rule_adds_its_full_weight(self) -> None:
        result = evaluate_custom(_boolean(), _facts(genres=Known("reality,comedy", "radarr")))
        assert result.evaluated is True
        assert result.pressure == 30.0
        assert result.weight == 30

    def test_an_unmatched_boolean_rule_adds_nothing_but_is_evaluated(self) -> None:
        result = evaluate_custom(_boolean(), _facts(genres=Known("drama", "radarr")))
        assert result.evaluated is True
        assert result.pressure == 0.0
        assert result.weight == 30

    def test_an_unknown_input_adds_no_pressure_but_keeps_its_weight(self) -> None:
        result = evaluate_custom(_boolean(), _facts(genres=Unknown("could not read", "radarr")))
        assert result.evaluated is False  # weight still in the denominator, drags score down
        assert result.pressure == 0.0
        assert result.weight == 30

    def test_a_graded_rule_ramps_a_numeric_field(self) -> None:
        config = CustomSignalConfig(
            name="old", weight=40, kind="graded", field="release_age", floor=0, saturate_at=1000
        )
        result = evaluate_custom(config, _facts(release_age_days=Known(500.0, "radarr")))
        assert result.pressure == pytest.approx(20.0)  # 500/1000 * 40

    def test_a_disabled_rule_contributes_nothing_and_leaves_the_denominator(self) -> None:
        result = evaluate_custom(_boolean(weight=0), _facts(genres=Known("reality", "radarr")))
        assert result.weight == 0
        assert result.pressure == 0.0


class TestScoreComposition:
    def _builtins(self) -> list[SignalConfig]:
        return [SignalConfig(signal=SignalId.UNWATCHED, weight=70, saturate_at=1825, floor=365)]

    def test_a_matched_rule_raises_the_score(self) -> None:
        facts = _facts(
            days_observed_unwatched=Known(1825.0, "tautulli"), genres=Known("reality", "radarr")
        )
        composed = score(self._builtins(), facts, custom_condemn=[_boolean()])
        assert composed.value == pytest.approx(100.0)  # 70 + 30 over a denominator of 100

    def test_adding_a_rule_rebalances_the_normalized_score(self) -> None:
        # The architecture the operator chose: a custom weight joins the fixed denominator,
        # so adding an UNMATCHED rule lowers every other signal's share of 100.
        facts = _facts(
            days_observed_unwatched=Known(1825.0, "tautulli"), genres=Known("drama", "radarr")
        )
        without = score(self._builtins(), facts)
        with_rule = score(self._builtins(), facts, custom_condemn=[_boolean()])
        assert without.value == pytest.approx(100.0)
        assert with_rule.value == pytest.approx(70.0)  # 70 pressure over a denominator of 100

    def test_an_unknown_custom_rule_only_lowers_the_score_and_coverage(self) -> None:
        matched = _facts(
            days_observed_unwatched=Known(1825.0, "tautulli"), genres=Known("reality", "radarr")
        )
        unknown = _facts(
            days_observed_unwatched=Known(1825.0, "tautulli"), genres=Unknown("x", "radarr")
        )
        s_matched = score(self._builtins(), matched, custom_condemn=[_boolean()])
        s_unknown = score(self._builtins(), unknown, custom_condemn=[_boolean()])
        assert s_unknown.value < s_matched.value
        assert s_unknown.coverage < s_matched.coverage  # missing data can only fail safe


class TestPolicyValidation:
    def test_a_protect_only_field_cannot_be_a_custom_condemn_rule(self) -> None:
        with pytest.raises(ValidationError, match="cannot be used to"):
            BooleanCondemnSpec(name="x", field="watchers_all_time", op=Op.LTE, value=1, weight=10)

    def test_a_graded_rule_on_a_non_numeric_field_is_rejected(self) -> None:
        with pytest.raises(ValidationError, match="not a number"):
            GradedCondemnSpec(name="x", field="genre", weight=10, saturate_at=5)

    def test_two_custom_rules_may_not_share_a_name(self) -> None:
        with pytest.raises(ValidationError, match="share a name"):
            _policy(
                custom_condemn=(
                    BooleanCondemnSpec(
                        name="dup", field="genre", op=Op.CONTAINS, value="a", weight=10
                    ),
                    BooleanCondemnSpec(
                        name="dup", field="genre", op=Op.CONTAINS, value="b", weight=10
                    ),
                )
            )

    def test_a_policy_with_only_a_custom_condemn_weight_is_valid(self) -> None:
        # Every built-in signal at weight 0, but a custom rule carries weight -> still a scorer.
        policy = _policy(
            signals=(
                SignalSetting(signal=SignalId.UNWATCHED, weight=0, saturate_at=1825, floor=365),
            ),
            custom_condemn=(
                BooleanCondemnSpec(name="r", field="genre", op=Op.CONTAINS, value="x", weight=25),
            ),
        )
        assert policy.custom_condemn[0].weight == 25

    def test_custom_signal_configs_translate_both_flavors(self) -> None:
        policy = _policy(
            custom_condemn=(
                BooleanCondemnSpec(
                    name="r", field="genre", op=Op.CONTAINS, value="reality", weight=25
                ),
                GradedCondemnSpec(name="old", field="release_age", weight=40, saturate_at=1000),
            )
        )
        configs = policy.custom_signal_configs()
        assert configs[0].kind == "boolean" and configs[0].condition is not None
        assert configs[1].kind == "graded" and configs[1].saturate_at == 1000

    def test_a_size_based_custom_rule_earns_a_danger_warning(self) -> None:
        policy = _policy(
            custom_condemn=(
                GradedCondemnSpec(name="big", field="size_bytes", weight=30, saturate_at=50),
            )
        )
        warnings = inspect(policy, ProfileSettings())
        assert any(w.field == "custom_condemn" and w.severity == "danger" for w in warnings)


class TestARuleIsWorthLessThanItsNumber:
    """A custom rule joins the same fixed denominator as the built-in signals, so its
    weight is not points added on top: it raises the total the score is measured against.
    An owner reading "+40 against a threshold of 70" reads it as far more than it is, and
    nothing else in the policy says so."""

    #: The one phrase the warning is identified by, so a reworded message fails loudly here
    #: rather than silently matching some other warning about the same field.
    LEAD = "shares one total"

    def _flagged(self, policy: PolicyBody) -> list[PolicyWarning]:
        return [
            w
            for w in inspect(policy, ProfileSettings())
            if w.field == "custom_condemn" and self.LEAD in w.message
        ]

    def test_the_stated_worth_is_the_one_the_scorer_produces(self) -> None:
        policy = _policy(
            custom_condemn=(
                BooleanCondemnSpec(
                    name="reality", field="genre", op=Op.CONTAINS, value="reality", weight=40
                ),
            )
        )

        flagged = self._flagged(policy)

        assert len(flagged) == 1
        warning = flagged[0]
        assert warning.severity == "warn"
        # Not a transcribed number: score an item where this rule is the only thing firing
        # and check the message quotes what the real scorer gives it.
        scored = score(
            [SignalConfig(signal=SignalId.UNWATCHED, weight=70, saturate_at=1825, floor=365)],
            _facts(
                genres=Known("reality", "radarr"),
                days_observed_unwatched=Known(0.0, "tautulli"),
            ),
            custom_condemn=[_boolean(name="reality", weight=40)],
        )
        assert f"about {int(scored.value)} points" in warning.message
        assert int(scored.value) < 40  # the whole point: less than the number on the rule
        assert "not 40" in warning.message

    def test_it_is_silent_while_the_weights_still_total_100(self) -> None:
        """At or under 100, a point of weight is still worth a point of score, so there
        is nothing misleading to report and saying it anyway would be noise."""
        policy = _policy(
            custom_condemn=(
                BooleanCondemnSpec(name="r", field="genre", op=Op.CONTAINS, value="x", weight=30),
            )
        )

        assert self._flagged(policy) == []

    def test_a_policy_with_no_rules_of_its_own_says_nothing(self) -> None:
        assert self._flagged(_policy()) == []

    def test_many_rules_still_earn_exactly_one_message(self) -> None:
        """One message however many rules, naming the heaviest, so the section cannot
        fill up with a line per rule."""
        policy = _policy(
            custom_condemn=(
                BooleanCondemnSpec(
                    name="a small one", field="genre", op=Op.CONTAINS, value="x", weight=10
                ),
                BooleanCondemnSpec(
                    name="a big one", field="genre", op=Op.CONTAINS, value="y", weight=45
                ),
            )
        )

        flagged = self._flagged(policy)

        assert len(flagged) == 1
        warning = flagged[0]
        assert "a big one" in warning.message
        assert "a small one" not in warning.message

    def test_a_rule_switched_off_neither_counts_nor_is_named(self) -> None:
        """Weight 0 leaves the denominator alone, so it changes nothing and cannot be
        the rule the message points at."""
        policy = _policy(
            custom_condemn=(
                BooleanCondemnSpec(name="off", field="genre", op=Op.CONTAINS, value="x", weight=0),
                BooleanCondemnSpec(name="on", field="genre", op=Op.CONTAINS, value="y", weight=40),
            )
        )

        flagged = self._flagged(policy)

        assert len(flagged) == 1
        warning = flagged[0]
        assert '"on"' in warning.message
        # 70 + 40, not 70 + 40 + 0: a disabled rule is out of the denominator entirely.
        assert f"about {100 * 40 // 110} points" in warning.message

    def test_the_message_stays_in_plain_language(self) -> None:
        policy = _policy(
            custom_condemn=(
                BooleanCondemnSpec(name="r", field="genre", op=Op.CONTAINS, value="x", weight=40),
            )
        )

        message = self._flagged(policy)[0].message

        assert "--" not in message and "—" not in message
        for jargon in ("denominator", "weight", "normali", "coverage"):
            assert jargon not in message.lower()


def _keep(**over: object) -> KeepConfig:
    params: dict[str, object] = {
        "name": "loyal",
        "max_discount": 20,
        "field": "watchers_all_time",
        "floor": 0,
        "saturate_at": 10,
        "direction": "high_keeps",
    }
    return KeepConfig(**{**params, **over})  # type: ignore[arg-type]


class TestGradedKeep:
    def test_a_high_value_keep_discounts_the_score(self) -> None:
        result = evaluate_keep(_keep(), _facts(distinct_watchers_all_time=Known(10, "tautulli")))
        assert result.evaluated is True
        assert result.discount == pytest.approx(20.0)  # ramp(10, 0, 10) == 1.0

    def test_an_unknown_keep_takes_the_maximum_discount(self) -> None:
        # Fail-closed: could not check -> keep the file fully. Missing data pushes toward keeping.
        result = evaluate_keep(_keep(), _facts(distinct_watchers_all_time=Unknown("x", "plex")))
        assert result.evaluated is False
        assert result.discount == 20.0

    def test_an_absent_value_gives_no_discount(self) -> None:
        # Absence is real evidence (we looked, there is none), not a reason to keep.
        result = evaluate_keep(_keep(), _facts(distinct_watchers_all_time=Absent("plex")))
        assert result.evaluated is True
        assert result.discount == 0.0

    def test_low_keeps_inverts_the_ramp(self) -> None:
        result = evaluate_keep(
            _keep(direction="low_keeps"), _facts(distinct_watchers_all_time=Known(0, "tautulli"))
        )
        assert result.discount == pytest.approx(20.0)  # 0 watchers, low-keeps -> full keep

    def test_a_keep_lowers_the_final_score_but_not_the_base(self) -> None:
        builtins = [SignalConfig(signal=SignalId.UNWATCHED, weight=70, saturate_at=1825, floor=365)]
        facts = _facts(
            days_observed_unwatched=Known(1825.0, "tautulli"),
            distinct_watchers_all_time=Known(10, "tautulli"),
        )
        composed = score(builtins, facts, keeps=[_keep()])
        assert composed.base_value == pytest.approx(100.0)
        assert composed.value == pytest.approx(80.0)  # 100 - 20
        assert composed.keep_discount == pytest.approx(20.0)

    def test_a_keep_never_drives_the_score_negative(self) -> None:
        builtins = [SignalConfig(signal=SignalId.UNWATCHED, weight=70, saturate_at=1825, floor=365)]
        facts = _facts(
            days_observed_unwatched=Known(1000.0, "tautulli"),
            distinct_watchers_all_time=Known(10, "tautulli"),
        )
        composed = score(builtins, facts, keeps=[_keep(max_discount=100)])
        assert composed.value == 0.0
        assert composed.value <= composed.base_value  # a keep can only ever lower the score

    def test_a_graded_keep_on_a_non_numeric_field_is_rejected(self) -> None:
        with pytest.raises(ValidationError, match="not a number"):
            GradedKeepSpec(name="x", field="genre", max_discount=10, floor=0, saturate_at=5)

    def test_a_keep_may_use_a_protect_only_field(self) -> None:
        # watchers_all_time is protect-only, but a keep may use it -- it only lowers a score.
        spec = GradedKeepSpec(
            name="loyal", field="watchers_all_time", max_discount=20, floor=0, saturate_at=10
        )
        assert _policy(graded_keeps=(spec,)).keep_configs()[0].max_discount == 20

    def test_keeps_that_can_erase_the_threshold_earn_a_warning(self) -> None:
        policy = _policy(
            condemn_at=70,
            graded_keeps=(
                GradedKeepSpec(
                    name="a", field="watchers_all_time", max_discount=70, floor=0, saturate_at=10
                ),
            ),
        )
        warnings = inspect(policy, ProfileSettings())
        assert any(w.field == "graded_keeps" and w.severity == "warn" for w in warnings)
