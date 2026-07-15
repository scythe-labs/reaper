# SPDX-License-Identifier: AGPL-3.0-or-later
"""The field registry, and the asymmetry it enforces.

The condemn lane is locked down; the protect lane is open. That is not a
convenience -- it is the safety property. A badly written protect rule cannot delete
anything, so the owner can be trusted with real expressive power there. A badly
written condemn rule deletes 4 TB.
"""

from __future__ import annotations

import pytest

from reaper.engine.fields import (
    BY_KEY,
    Condition,
    CustomProtectGate,
    Lane,
    Op,
    RuleSet,
    evaluate,
    evaluate_rules,
    vocabulary,
)
from reaper.engine.gates import ABSTAIN, PROTECT, Facts
from reaper.engine.observation import Absent, Known, Unknown


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


class TestTheLaneAsymmetry:
    """The registry makes a dangerous condition UNCONSTRUCTABLE, not merely
    rejected."""

    def test_an_all_time_watcher_count_cannot_be_used_to_condemn(self) -> None:
        """It is a fine reason to KEEP something and a terrible reason to delete it:
        condemning on it would make the recency signal meaningless."""
        condition = Condition(field="watchers_all_time", op=Op.LTE, value=1)

        with pytest.raises(ValueError, match="cannot be used to condemn"):
            condition.validate_for(Lane.CONDEMN)

    def test_the_same_field_is_fine_as_a_protection(self) -> None:
        condition = Condition(field="watchers_all_time", op=Op.GTE, value=5)
        condition.validate_for(Lane.PROTECT)  # does not raise

    def test_the_condemn_vocabulary_never_offers_a_protect_only_field(self) -> None:
        """The API filters by lane BEFORE serialising, so the browser is never even
        shown a field it must not use. A condemn rule referencing one is not
        rejected -- it cannot be built."""
        condemn_keys = {spec.key for spec in vocabulary(Lane.CONDEMN)}

        assert "watchers_all_time" not in condemn_keys
        assert "whitelisted" not in condemn_keys
        assert "imdb_votes" not in condemn_keys
        assert "streaming_now" not in condemn_keys

    def test_the_protect_vocabulary_is_a_superset(self) -> None:
        """Protection can only ever be safe, so the owner gets more power there."""
        condemn = {s.key for s in vocabulary(Lane.CONDEMN)}
        protect = {s.key for s in vocabulary(Lane.PROTECT)}

        assert condemn < protect

    def test_a_ruleset_validates_its_lane_on_construction(self) -> None:
        with pytest.raises(ValueError, match="cannot be used to condemn"):
            RuleSet(
                lane=Lane.CONDEMN,
                conditions=(Condition(field="whitelisted", op=Op.EQ, value=True),),
            )

    def test_an_unsupported_operator_is_refused(self) -> None:
        with pytest.raises(ValueError, match="does not support"):
            Condition(field="days_unwatched", op=Op.CONTAINS, value="x").validate_for(Lane.CONDEMN)


class TestCondemnIsAFlatAnd:
    def test_every_condition_must_match(self) -> None:
        rules = RuleSet(
            lane=Lane.CONDEMN,
            conditions=(
                Condition(field="days_unwatched", op=Op.GTE, value=365),
                Condition(field="recent_watchers", op=Op.LTE, value=0),
            ),
        )
        assert evaluate_rules(rules, _facts()).matched is True

    def test_one_failing_condition_is_enough_to_spare_it(self) -> None:
        rules = RuleSet(
            lane=Lane.CONDEMN,
            conditions=(
                Condition(field="days_unwatched", op=Op.GTE, value=365),
                Condition(field="recent_watchers", op=Op.LTE, value=0),
                Condition(field="size_bytes", op=Op.GTE, value=50_000_000_000),  # 8GB < 50GB
            ),
        )
        assert evaluate_rules(rules, _facts()).matched is False

    def test_a_blocked_condition_never_condemns(self) -> None:
        """Unknown is not evidence. If we could not check one of the conditions, we
        cannot claim the item qualifies."""
        rules = RuleSet(
            lane=Lane.CONDEMN,
            conditions=(
                Condition(field="days_unwatched", op=Op.GTE, value=365),
                Condition(field="recent_watchers", op=Op.LTE, value=0),
            ),
        )
        blind = _facts(distinct_watchers=Unknown(reason="Tautulli timed out", source="t"))

        result = evaluate_rules(rules, blind)

        assert result.blocked is True
        assert result.matched is False  # ...and therefore not condemned


class TestProtectIsAnOr:
    """Any reason to keep a file is sufficient. Safe by construction -- which is
    precisely why this lane may be user-authored."""

    def test_a_single_matching_protection_is_enough(self) -> None:
        rules = RuleSet(
            lane=Lane.PROTECT,
            conditions=(
                Condition(field="imdb_rating", op=Op.GTE, value=90),  # 9.0 -- fails
                Condition(field="imdb_votes", op=Op.GTE, value=500_000),  # 900k -- fires
            ),
        )
        assert evaluate_rules(rules, _facts()).matched is True

    def test_the_owners_own_protection_rule(self) -> None:
        """The rule the field registry exists to make expressible: 'never delete
        anything with more than half a million IMDb votes, however unwatched it is
        here.'

        This is not hypothetical. Backtesting surfaced blockbusters -- famous, heavily
        rated, dormant on this particular server -- that the default policy condemned
        and that a user then watched months later. The item is globally beloved and
        locally quiet, which no built-in gate catches: the rating floor rejects it
        (its score is merely good, not great) and the popularity gate rejects it
        (nobody here watched it *recently*). Vote count is the signal that saves it,
        and only the owner knows to ask for it.
        """
        famous_but_dormant = _facts(
            imdb_rating_tenths=Known(value=73, source="imdb"),  # below the 7.5 floor
            imdb_votes=Known(value=800_000, source="imdb"),
            days_observed_unwatched=Known(value=700.0, source="tautulli"),
            distinct_watchers=Known(value=0, source="tautulli"),
        )
        rules = RuleSet(
            lane=Lane.PROTECT,
            conditions=(Condition(field="imdb_votes", op=Op.GTE, value=500_000),),
        )

        assert evaluate_rules(rules, famous_but_dormant).matched is True


class TestUnitsAreRendered:
    """A bare number is how a 7.5 rating floor ends up compared against a
    Tomatometer of 96."""

    def test_a_rating_is_shown_in_tenths_as_a_decimal(self) -> None:
        result = evaluate(Condition(field="imdb_rating", op=Op.GTE, value=75), _facts())
        assert "7.3" in result.detail
        assert "7.5" in result.detail

    def test_bytes_are_shown_as_gigabytes(self) -> None:
        result = evaluate(Condition(field="size_bytes", op=Op.GTE, value=1_000_000_000), _facts())
        assert "8.0 GB" in result.detail

    def test_every_field_carries_its_unit_and_help(self) -> None:
        for spec in BY_KEY.values():
            assert spec.label
            assert spec.help_text


class TestCustomProtectGate:
    """A user-authored condition, wearing the built-in Gate interface. It can only ever
    PROTECT or ABSTAIN -- there is no path to a delete -- which is what makes it safe."""

    def test_a_matched_condition_fires_protect(self) -> None:
        # "Keep anything with at least a million IMDb votes." This film has 900k, so raise it.
        gate = CustomProtectGate(Condition(field="imdb_votes", op=Op.GTE, value=500_000))
        result = gate.evaluate(_facts())
        assert result.outcome == PROTECT
        assert "your rule" in result.detail

    def test_an_unmatched_condition_abstains_and_is_checked(self) -> None:
        gate = CustomProtectGate(Condition(field="imdb_votes", op=Op.GTE, value=5_000_000))
        result = gate.evaluate(_facts())
        assert result.outcome == ABSTAIN
        assert result.blocked is False

    def test_an_unknown_input_is_blocked_never_assumed(self) -> None:
        gate = CustomProtectGate(Condition(field="recent_watchers", op=Op.GTE, value=1))
        result = gate.evaluate(_facts(distinct_watchers=Unknown(reason="no history", source="t")))
        assert result.outcome == ABSTAIN
        assert result.blocked is True  # amber, not green: we could not look

    def test_it_has_no_condemn_constructor(self) -> None:
        # The structural guarantee: whatever the condition, the outcome is only ever one of
        # these two. A mis-authored protection can at worst fail to keep something.
        for value in (0, 1, 999):
            outcome = CustomProtectGate(
                Condition(field="imdb_votes", op=Op.GTE, value=value)
            ).evaluate(_facts()).outcome
            assert outcome in (PROTECT, ABSTAIN)


class TestSeasonPruningNeedsNoBooleanCleverness:
    def test_keep_the_last_two_seasons_is_one_condition(self) -> None:
        """The whole reason the condemn lane needs no OR or nesting: this was never
        a logic problem, it is a derived field."""
        older_season = _facts(season_rank=Known(value=5, source="sonarr"))
        newest_two = _facts(season_rank=Known(value=2, source="sonarr"))

        rules = RuleSet(
            lane=Lane.CONDEMN,
            conditions=(Condition(field="season_rank", op=Op.GTE, value=3),),
        )

        assert evaluate_rules(rules, older_season).matched is True
        assert evaluate_rules(rules, newest_two).matched is False
