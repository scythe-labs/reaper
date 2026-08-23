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
    ProfileSettings,
    SignalSetting,
)
from reaper.engine.policy_warnings import inspect
from reaper.engine.signals import (
    REWATCH_KEEP,
    CustomSignalConfig,
    KeepConfig,
    SignalConfig,
    SignalId,
    evaluate_custom,
    evaluate_keep,
    score,
)
from tests._reasons import text


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
        # Deep enough that a watcher count is the answer rather than a lower bound, so
        # these tests measure the rule they are about and not the reach bound
        # (``fields.reach_shortfall``); the twin in tests/test_fields.py says the rest.
        "history_reach_days": Known(value=4000.0, source="tautulli"),
        "days_since_added": Known(value=800.0, source="plex"),
    }
    return Facts(**{**base, **overrides})  # type: ignore[arg-type]


def _policy(**over: object) -> PolicyBody:
    """A policy whose removal weights total exactly 100, whatever rules the test adds.

    The built-in signal absorbs whatever the custom rules do not spend, because that is
    what the budget rule (``PolicyBody._weights_total_one_hundred``) requires and what
    the editor will make an operator do by hand. Tests that care about the split pass
    ``signals`` explicitly.
    """
    rules: tuple[object, ...] = over.get("custom_condemn", ()) or ()  # type: ignore[assignment]
    spent = sum(getattr(r, "weight", 0) for r in rules)
    base: dict[str, object] = {
        "media_type": "movie",
        "condemn_at": 70,
        "gates": (),
        "signals": (
            SignalSetting(
                signal=SignalId.UNWATCHED, weight=100 - spent, saturate_at=1825, floor=365
            ),
        ),
    }
    return PolicyBody(**{**base, **over})


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

    def test_a_policy_scored_entirely_by_your_own_rules_is_valid(self) -> None:
        """Every built-in signal at 0 and one rule holding the whole budget. Allowed, and
        deliberately loud: spending all 100 points on one rule is the only way to make a
        rule condemn on its own, and it costs every built-in signal to do it."""
        policy = _policy(
            signals=(
                SignalSetting(signal=SignalId.UNWATCHED, weight=0, saturate_at=1825, floor=365),
            ),
            custom_condemn=(
                BooleanCondemnSpec(name="r", field="genre", op=Op.CONTAINS, value="x", weight=100),
            ),
        )
        assert policy.custom_condemn[0].weight == 100

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


# TestARuleIsWorthLessThanItsNumber lived here. It pinned the dilution warning: a rule
# written as 40 really adding about 22. `PolicyBody._weights_total_one_hundred` makes
# that unrepresentable, so both the warning and its tests are gone rather than reworded.
# What replaces them is test_policy.py's test_weights_that_do_not_total_one_hundred_are_refused.


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
            GradedKeepSpec(name="x", field="whitelisted", max_discount=10, floor=0, saturate_at=5)

    def test_a_membership_keep_is_flat_and_case_folded(self) -> None:
        """The four arms of the membership branch, driven together because the split is
        the claim (rule 93): a member takes the FULL discount, a non-member none, a
        genuine "on no list" none, and a membership that could not be read the full one,
        evaluated False. The name match is case-folded on both sides (rule 88) and per
        element of the comma-joined fact."""
        keep = KeepConfig(
            name="on my list",
            max_discount=25,
            field="on_list",
            value="  MY LIST ",
            floor=0,
            saturate_at=1,
        )

        member = evaluate_keep(keep, _facts(on_lists=Known("my list, Another", "lists")))
        assert member.evaluated is True
        assert member.discount == 25.0

        elsewhere = evaluate_keep(keep, _facts(on_lists=Known("Another", "lists")))
        assert elsewhere.evaluated is True
        assert elsewhere.discount == 0.0

        # Absent is real evidence: on none of the operator's lists, so no lean.
        unlisted = evaluate_keep(keep, _facts(on_lists=Absent(source="lists")))
        assert unlisted.evaluated is True
        assert unlisted.discount == 0.0
        assert text(unlisted.detail) == "on none of your lists"

        # Unknown fails closed: could not check the lists, keep fully.
        unreadable = evaluate_keep(keep, _facts(on_lists=Unknown("sync failed", "lists")))
        assert unreadable.evaluated is False
        assert unreadable.discount == 25.0

    def test_a_membership_keep_must_say_which_list(self) -> None:
        """The membership form (a multi-text field like ``on_list``) is flat, so the one
        thing it cannot do without is the list's name -- a nameless one would have nothing
        to match and read as a live lean covering nothing."""
        with pytest.raises(ValidationError, match="which list"):
            GradedKeepSpec(name="x", field="on_list", max_discount=10, floor=0, saturate_at=5)
        with pytest.raises(ValidationError, match="which list"):
            GradedKeepSpec(
                name="x", field="on_list", value="   ", max_discount=10, floor=0, saturate_at=5
            )

    def test_a_membership_keep_with_a_name_validates_and_ignores_the_ramp(self) -> None:
        """Flat, so the ramp fields are inert: a floor at or above saturate_at, which a
        numeric keep refuses, is not validated here because nothing reads it."""
        spec = GradedKeepSpec(
            name="x", field="on_list", value="My list", max_discount=10, floor=5, saturate_at=5
        )
        assert spec.value == "My list"

    def test_only_on_list_is_a_membership_field(self) -> None:
        """The arm keyed on the field's SHAPE (``TEXT and multi``), which also describes
        ``genre``: that validated, granted the flat keep, and explained itself as 'on your
        list "Comedy"' (#505). Keep-only, so nothing widened, but the operator was told a
        genre is a list (rule 21). The editor offers the box for ``on_list`` alone, so the
        reachable paths were the API and an imported or restored body."""
        with pytest.raises(ValidationError, match="not a list"):
            GradedKeepSpec(
                name="Comedy stays",
                field="genre",
                value="Comedy",
                max_discount=40,
                floor=0,
                saturate_at=1,
            )

    def test_a_numeric_keep_refuses_a_list_name(self) -> None:
        """The other direction: ``value`` means "which list", so a numeric field carrying
        one is a rule whose author meant something the ramp cannot express."""
        with pytest.raises(ValidationError, match="does not take a list name"):
            GradedKeepSpec(
                name="x",
                field="watchers_all_time",
                value="My list",
                max_discount=10,
                floor=0,
                saturate_at=5,
            )

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


def _rewatch_keep(**over: object) -> KeepConfig:
    """Both bars off the shipped 10/730 default throughout this class (rule 141): a fixture
    pinned to the default cannot tell a caller that read the config's own bars from one that
    silently fell back to them. Two different bar pairs are exercised below for the same
    reason -- one pair alone could not tell a caller that reads the RIGHT bar from one that
    reads a constant."""
    params: dict[str, object] = {
        "name": REWATCH_KEEP,
        "max_discount": 22,
        "field": REWATCH_KEEP,
        "floor": 0,
        "saturate_at": 1,
        "min_viewings": 6,
        "recent_days": 400,
    }
    return KeepConfig(**{**params, **over})  # type: ignore[arg-type]


class TestBuiltinRewatchKeep:
    """The built-in habitual-rewatch keep (``docs/history/REWATCH_PLAN.md`` Stage 1): a flat arm of
    ``evaluate_keep`` keyed on ``field == REWATCH_KEEP``, deciding the condition over two
    frozen observations against the config's own two bars rather than a ramp."""

    def test_the_condition_met_takes_the_full_discount_and_states_the_figures(self) -> None:
        keep = _rewatch_keep(min_viewings=6, recent_days=400)
        facts = _facts(
            rewatch_viewings=Known(value=8, source="tautulli"),
            rewatch_last_play_days=Known(value=30.0, source="tautulli"),
        )

        result = evaluate_keep(keep, facts)

        assert result.evaluated is True
        assert result.discount == pytest.approx(22.0)
        assert (
            text(result.detail)
            == "Watched 8 times, most recently 1 month ago. Likely to be watched again."
        )

    def test_the_condition_met_at_a_second_bar_pair(self) -> None:
        """A different bar pair from the test above, so this class does not merely prove
        one pair of numbers works."""
        keep = _rewatch_keep(max_discount=15, min_viewings=9, recent_days=550)
        facts = _facts(
            rewatch_viewings=Known(value=12, source="tautulli"),
            rewatch_last_play_days=Known(value=100.0, source="tautulli"),
        )

        result = evaluate_keep(keep, facts)

        assert result.evaluated is True
        assert result.discount == pytest.approx(15.0)

    def test_too_few_viewings_discounts_nothing(self) -> None:
        keep = _rewatch_keep(min_viewings=6, recent_days=400)
        facts = _facts(
            rewatch_viewings=Known(value=3, source="tautulli"),
            rewatch_last_play_days=Known(value=10.0, source="tautulli"),
        )

        result = evaluate_keep(keep, facts)

        assert result.evaluated is True
        assert result.discount == 0.0
        assert text(result.detail) == "Watched 3 times in total."

    def test_a_stale_last_play_discounts_nothing(self) -> None:
        """Enough viewings, but the most recent one is outside the window: read-and-not-met,
        never Unknown, so the miss detail states both figures honestly."""
        keep = _rewatch_keep(min_viewings=6, recent_days=400)
        facts = _facts(
            rewatch_viewings=Known(value=8, source="tautulli"),
            rewatch_last_play_days=Known(value=401.0, source="tautulli"),
        )

        result = evaluate_keep(keep, facts)

        assert result.evaluated is True
        assert result.discount == 0.0
        assert text(result.detail) == "Watched 8 times, but not in the last 1 year, 1 month."

    def test_recency_exactly_at_the_window_fires(self) -> None:
        keep = _rewatch_keep(min_viewings=6, recent_days=400)
        facts = _facts(
            rewatch_viewings=Known(value=6, source="tautulli"),
            rewatch_last_play_days=Known(value=400.0, source="tautulli"),
        )

        result = evaluate_keep(keep, facts)

        assert result.evaluated is True
        assert result.discount == pytest.approx(float(keep.max_discount))

    def test_viewings_exactly_at_the_bar_fires(self) -> None:
        keep = _rewatch_keep(min_viewings=9, recent_days=550)
        facts = _facts(
            rewatch_viewings=Known(value=9, source="tautulli"),
            rewatch_last_play_days=Known(value=1.0, source="tautulli"),
        )

        result = evaluate_keep(keep, facts)

        assert result.evaluated is True
        assert result.discount == pytest.approx(float(keep.max_discount))

    def test_unknown_viewings_takes_the_full_discount_and_is_not_evaluated(self) -> None:
        keep = _rewatch_keep()
        facts = _facts(
            rewatch_viewings=Unknown(reason="mirror down", source="tautulli"),
            rewatch_last_play_days=Known(value=10.0, source="tautulli"),
        )

        result = evaluate_keep(keep, facts)

        assert result.evaluated is False
        assert result.discount == float(keep.max_discount)
        assert text(result.detail) == "kept fully: could not check your watch history"

    def test_unknown_last_play_takes_the_full_discount_and_is_not_evaluated(self) -> None:
        keep = _rewatch_keep()
        facts = _facts(
            rewatch_viewings=Known(value=8, source="tautulli"),
            rewatch_last_play_days=Unknown(reason="mirror down", source="tautulli"),
        )

        result = evaluate_keep(keep, facts)

        assert result.evaluated is False
        assert result.discount == float(keep.max_discount)
        assert text(result.detail) == "kept fully: could not check your watch history"

    def test_never_watched_here_discounts_nothing(self) -> None:
        """``Known(0)`` viewings paired with an ``Absent`` last play: the never-watched shape
        ``snapshot.build_facts`` freezes when the mirror was read and holds no qualified play
        at all (rule 93), never ``Unknown``."""
        keep = _rewatch_keep()
        facts = _facts(
            rewatch_viewings=Known(value=0, source="tautulli"),
            rewatch_last_play_days=Absent(source="tautulli"),
        )

        result = evaluate_keep(keep, facts)

        assert result.evaluated is True
        assert result.discount == 0.0
        assert text(result.detail) == "Never watched here."

    def test_ungathered_facts_discount_nothing(self) -> None:
        """``rewatch_viewings`` Absent means the caller never gathered watch history
        (hand-built ``Facts``; both live builders now write the observation), never a
        failed read, so the keep withholds no discount over evidence that was never
        gathered rather than fail-closing on it."""
        keep = _rewatch_keep()
        facts = _facts(
            rewatch_viewings=Absent(source="tautulli"),
            rewatch_last_play_days=Absent(source="tautulli"),
        )

        result = evaluate_keep(keep, facts)

        assert result.evaluated is True
        assert result.discount == 0.0
        assert text(result.detail) == "Does not apply here."


class TestBuiltinRewatchKeepTvWording:
    """The same flat arm on a TV config (``KeepConfig.media_type == "tv"``): the count is
    whole re-watches of the show (``services.rewatch.replay_period_count``), so every
    detail that states it says "again" (the approved TV mockup's copy). Condition
    arithmetic is the movie class above; only the wording forks."""

    def test_the_condition_met_states_rewatches(self) -> None:
        keep = _rewatch_keep(media_type="tv", min_viewings=2, recent_days=500)
        facts = _facts(
            rewatch_viewings=Known(value=3, source="tautulli"),
            rewatch_last_play_days=Known(value=30.0, source="tautulli"),
        )

        result = evaluate_keep(keep, facts)

        assert result.evaluated is True
        assert result.discount == pytest.approx(22.0)
        assert (
            text(result.detail)
            == "Watched again 3 times, most recently 1 month ago. Likely to be watched again."
        )

    def test_too_few_rewatches_states_the_count(self) -> None:
        keep = _rewatch_keep(media_type="tv", min_viewings=2, recent_days=500)
        facts = _facts(
            rewatch_viewings=Known(value=1, source="tautulli"),
            rewatch_last_play_days=Known(value=10.0, source="tautulli"),
        )

        result = evaluate_keep(keep, facts)

        assert result.discount == 0.0
        assert text(result.detail) == "Watched again 1 time in total."

    def test_a_stale_last_play_states_the_window(self) -> None:
        keep = _rewatch_keep(media_type="tv", min_viewings=2, recent_days=500)
        facts = _facts(
            rewatch_viewings=Known(value=3, source="tautulli"),
            rewatch_last_play_days=Known(value=501.0, source="tautulli"),
        )

        result = evaluate_keep(keep, facts)

        assert result.discount == 0.0
        assert text(result.detail) == "Watched again 3 times, but not in the last 1 year, 4 months."

    def test_zero_rewatches_says_never_again_not_never(self) -> None:
        """A show at zero was possibly watched once through -- its count is re-watches --
        so the movie lane's "Never watched here." would overclaim about a show someone
        followed to the end."""
        keep = _rewatch_keep(media_type="tv", min_viewings=2, recent_days=500)
        facts = _facts(
            rewatch_viewings=Known(value=0, source="tautulli"),
            rewatch_last_play_days=Known(value=40.0, source="tautulli"),
        )

        result = evaluate_keep(keep, facts)

        assert result.evaluated is True
        assert result.discount == 0.0
        assert text(result.detail) == "Never watched again here."
