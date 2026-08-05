# SPDX-License-Identifier: AGPL-3.0-or-later
"""The field registry, and the asymmetry it enforces.

The condemn lane is locked down; the protect lane is open. That is not a
convenience -- it is the safety property. A badly written protect rule cannot delete
anything, so the owner can be trusted with real expressive power there. A badly
written condemn rule deletes 4 TB.
"""

from __future__ import annotations

import re

import pytest

from reaper.clock import humanize_days
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
        # A mirror deeper than any window or arrival age these tests use, so a watcher
        # count reads as the answer it is written to be rather than as a lower bound
        # (``fields.reach_shortfall``). Left at the ``_UNSET`` default every one of them
        # would block instead, and pass or fail for a reason that is not what it is
        # testing. ``tests/_policy_lab.py`` seeds the reach the same way; the tests that
        # are ABOUT the bound override these two.
        "history_reach_days": Known(value=4000.0, source="tautulli"),
        "days_since_added": Known(value=800.0, source="plex"),
    }
    return Facts(**{**base, **overrides})  # type: ignore[arg-type]


class TestTheLaneAsymmetry:
    """The registry makes a dangerous condition UNCONSTRUCTABLE, not merely
    rejected."""

    def test_an_all_time_watcher_count_cannot_be_used_to_condemn(self) -> None:
        """It is a fine reason to KEEP something and a terrible reason to delete it:
        condemning on it would make the recency signal meaningless."""
        condition = Condition(field="watchers_all_time", op=Op.LTE, value=1)

        with pytest.raises(ValueError, match="cannot be used to remove things"):
            condition.validate_for(Lane.CONDEMN)

    def test_the_same_field_is_fine_as_a_protection(self) -> None:
        condition = Condition(field="watchers_all_time", op=Op.GTE, value=5)
        condition.validate_for(Lane.PROTECT)  # does not raise

    def test_the_condemn_vocabulary_never_offers_a_protect_only_field(self) -> None:
        """The API filters by lane BEFORE serializing, so the browser is never even
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

    def test_a_tv_only_field_is_not_offered_on_a_movie_policy(self) -> None:
        """A movie has no show and no season, so a rule on one would silently never
        fire. The editor is narrowed by media type the same way it is by lane, so the
        field cannot even be picked."""
        movie_condemn = {s.key for s in vocabulary(Lane.CONDEMN, "movie")}
        movie_protect = {s.key for s in vocabulary(Lane.PROTECT, "movie")}

        assert "show_ended" not in movie_condemn
        assert "season_rank" not in movie_condemn
        assert "show_ended" not in movie_protect
        assert "season_rank" not in movie_protect

    def test_a_tv_only_field_is_offered_on_a_tv_policy(self) -> None:
        tv_condemn = {s.key for s in vocabulary(Lane.CONDEMN, "tv")}

        assert "show_ended" in tv_condemn
        # season_rank stays a condemn field for TV, even though a built-in signal covers
        # it in the editor -- the vocabulary offers it; the frontend hides the duplicate.
        assert "season_rank" in tv_condemn

    def test_a_field_that_applies_to_both_is_offered_either_way(self) -> None:
        for media in ("movie", "tv"):
            keys = {s.key for s in vocabulary(Lane.CONDEMN, media)}
            assert "genre" in keys
            assert "requested" in keys

    def test_omitting_media_type_keeps_every_field(self) -> None:
        """The default is unchanged behavior: no media filter, every lane field."""
        assert {s.key for s in vocabulary(Lane.CONDEMN)} == {
            s.key for s in vocabulary(Lane.CONDEMN, None)
        }
        assert "show_ended" in {s.key for s in vocabulary(Lane.CONDEMN)}

    def test_a_ruleset_validates_its_lane_on_construction(self) -> None:
        with pytest.raises(ValueError, match="cannot be used to remove things"):
            RuleSet(
                lane=Lane.CONDEMN,
                conditions=(Condition(field="whitelisted", op=Op.EQ, value=True),),
            )

    def test_an_unsupported_operator_is_refused(self) -> None:
        with pytest.raises(ValueError, match="cannot be compared with"):
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
        # The window the count was taken over: "nobody watched it in the last year" is
        # only a match the mirror can support once it reaches back a year (_facts does).
        assert evaluate_rules(rules, _facts(), window_days=365).matched is True

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
            outcome = (
                CustomProtectGate(Condition(field="imdb_votes", op=Op.GTE, value=value))
                .evaluate(_facts())
                .outcome
            )
            assert outcome in (PROTECT, ABSTAIN)


class TestSeasonPruningNeedsNoBooleanCleverness:
    def test_keep_the_last_two_seasons_is_one_condition(self) -> None:
        """The condemn lane needs no OR or nesting because this was never a logic
        problem, it is a derived field."""
        older_season = _facts(season_rank=Known(value=5, source="sonarr"))
        newest_two = _facts(season_rank=Known(value=2, source="sonarr"))

        rules = RuleSet(
            lane=Lane.CONDEMN,
            conditions=(Condition(field="season_rank", op=Op.GTE, value=3),),
        )

        assert evaluate_rules(rules, older_season).matched is True
        assert evaluate_rules(rules, newest_two).matched is False


class TestTextMatchingIsForgiving:
    """Plex title-cases stored text and owners type targets by hand, so ``in`` and
    ``eq`` must not fail on a capital letter or the space after a comma. And genres
    are comma-joined lists: eq/in evaluate per element there, or a multi-genre title
    could never match any single genre and the protection would silently never fire."""

    def test_in_survives_spaces_and_case(self) -> None:
        facts = _facts(quality=Known(value="Bluray-1080p", source="radarr"))
        cond = Condition(field="quality", op=Op.IN, value="bluray-1080p, SDTV")
        assert evaluate(cond, facts).matched is True

    def test_eq_on_text_is_case_insensitive(self) -> None:
        facts = _facts(quality=Known(value="SDTV", source="radarr"))
        assert evaluate(Condition(field="quality", op=Op.EQ, value="sdtv"), facts).matched is True

    def test_a_multi_genre_title_can_equal_a_single_genre(self) -> None:
        facts = _facts(genres=Known(value="Horror, Comedy", source="sonarr"))
        assert evaluate(Condition(field="genre", op=Op.EQ, value="horror"), facts).matched is True

    def test_in_on_genres_matches_any_shared_element(self) -> None:
        facts = _facts(genres=Known(value="Horror, Comedy", source="sonarr"))
        cond = Condition(field="genre", op=Op.IN, value="Anime, comedy")
        assert evaluate(cond, facts).matched is True

    def test_in_with_no_shared_element_does_not_match(self) -> None:
        facts = _facts(genres=Known(value="Horror, Comedy", source="sonarr"))
        cond = Condition(field="genre", op=Op.IN, value="Anime, Documentary")
        assert evaluate(cond, facts).matched is False


class TestAnExplanationSaysWhatItFound:
    """The why-panel quotes these sentences under "Kept by your rule:" and "Your rule
    didn't match:", so both readings have to be true English, and neither may leak the
    operator key ("gte", "eq") or a raw fact ("True") at the owner.

    The rule throughout: a match names what matched, a miss names what the rule wanted.
    """

    def test_a_boolean_states_the_fact_not_the_comparison(self) -> None:
        ended = _facts(show_ended=Known(value=True, source="sonarr"))
        going = _facts(show_ended=Known(value=False, source="sonarr"))
        cond = Condition(field="show_ended", op=Op.EQ, value=True)

        assert evaluate(cond, ended).detail == "The show has ended"
        # The miss must not read as though the show HAD ended. It says the opposite,
        # which is what we actually know.
        assert evaluate(cond, going).detail == "The show is still going"

    def test_a_boolean_reads_the_same_from_either_direction(self) -> None:
        """``eq false`` that matched and ``eq true`` that missed are the same world."""
        going = _facts(show_ended=Known(value=False, source="sonarr"))
        asked_true = evaluate(Condition(field="show_ended", op=Op.EQ, value=True), going)
        asked_false = evaluate(Condition(field="show_ended", op=Op.EQ, value=False), going)

        assert asked_true.matched is False
        assert asked_false.matched is True
        assert asked_true.detail == asked_false.detail == "The show is still going"

    def test_a_boolean_uses_the_words_the_owner_uses(self) -> None:
        cond = Condition(field="whitelisted", op=Op.EQ, value=True)
        assert evaluate(cond, _facts()).detail == "Not on any list you curate yourself"
        kept = _facts(is_whitelisted=Known(value=True, source="plex"))
        assert evaluate(cond, kept).detail == "On a list you curate yourself"

    def test_a_days_field_is_spelled_the_way_the_signals_spell_it(self) -> None:
        """One panel showing "900 days" beside "2 years, 5 months" reads as two
        different measurements of two different things."""
        facts = _facts(days_observed_unwatched=Known(value=900.0, source="tautulli"))
        detail = evaluate(Condition(field="days_unwatched", op=Op.GTE, value=730), facts).detail

        assert detail == "Not watched in 2 years, 5 months, past your 730 days"
        assert "900" not in detail

    def test_a_days_rule_echoes_the_number_the_owner_typed(self) -> None:
        """The measured span is humanized; the rule's own number is not. Rounding both
        sides makes a marginal title read as sitting under a number equal to itself."""
        facts = _facts(days_observed_unwatched=Known(value=396.0, source="tautulli"))
        detail = evaluate(Condition(field="days_unwatched", op=Op.GTE, value=400), facts).detail

        assert detail == "Not watched in 1 year, 1 month, within your 400 days"

    def test_a_release_age_rule_echoes_the_number_the_owner_typed(self) -> None:
        facts = _facts(release_age_days=Known(value=396.0, source="radarr"))
        detail = evaluate(Condition(field="release_age", op=Op.GTE, value=400), facts).detail

        assert detail == "Released 1 year, 1 month ago, within your 400 days"

    def test_a_days_value_and_its_bar_never_print_as_the_same_number(self) -> None:
        """Every day count from 395 to 424 humanizes to one phrase. If the bar is
        humanized too, the line asserts the value is on one side of itself."""
        for field in ("days_unwatched", "release_age"):
            for days in (395.0, 400.0, 410.0, 424.0):
                for bar in (396, 400, 420):
                    if days == bar:
                        continue
                    facts = _facts(
                        days_observed_unwatched=Known(value=days, source="tautulli"),
                        release_age_days=Known(value=days, source="radarr"),
                    )
                    detail = evaluate(Condition(field=field, op=Op.GTE, value=bar), facts).detail
                    value_phrase, _, bar_phrase = detail.rpartition(", ")
                    assert bar_phrase.endswith(f"your {bar:,.0f} days")
                    assert humanize_days(days) not in bar_phrase
                    assert humanize_days(days) in value_phrase

    def test_a_one_day_bar_is_not_pluralised(self) -> None:
        facts = _facts(days_observed_unwatched=Known(value=5.0, source="tautulli"))
        detail = evaluate(Condition(field="days_unwatched", op=Op.GTE, value=1), facts).detail

        assert detail == "Not watched in 5 days, past your 1 day"

    def test_a_size_leads_with_the_size(self) -> None:
        cond = Condition(field="size_bytes", op=Op.GTE, value=1_000_000_000)
        assert evaluate(cond, _facts()).detail == "8.0 GB on disk, over your 1.0 GB"

    def test_a_rating_below_its_bar_says_so_plainly(self) -> None:
        cond = Condition(field="imdb_rating", op=Op.GTE, value=75)
        assert evaluate(cond, _facts()).detail == "IMDb 7.3, under your 7.5"

    def test_large_counts_carry_thousands_separators(self) -> None:
        facts = _facts(imdb_votes=Known(value=2366, source="imdb"))
        detail = evaluate(Condition(field="imdb_votes", op=Op.GTE, value=5000), facts).detail

        assert detail == "2,366 votes on IMDb, under your 5,000"

    def test_a_count_of_one_is_not_pluralised(self) -> None:
        facts = _facts(distinct_watchers=Known(value=1, source="tautulli"))
        detail = evaluate(
            Condition(field="recent_watchers", op=Op.GTE, value=3), facts, window_days=365
        ).detail

        assert detail == "1 person watched it recently, under your 3"

    def test_a_count_lands_on_its_bar_often_enough_to_say_at_or(self) -> None:
        """A size or a rating never sits exactly on its number; a watcher count does it
        constantly, and "over your 2" with exactly 2 watchers is simply false."""
        facts = _facts(distinct_watchers=Known(value=2, source="tautulli"))
        detail = evaluate(Condition(field="recent_watchers", op=Op.GTE, value=2), facts).detail

        assert detail == "2 people watched it recently, at or over your 2"

    def test_season_rank_one_is_the_newest_season_never_an_older_one(self) -> None:
        """Rank 1 is the most recent season with files. The explanation may not call it
        an old season while the rule uses it to remove the season."""
        facts = _facts(season_rank=Known(value=1, source="sonarr"))
        detail = evaluate(Condition(field="season_rank", op=Op.LTE, value=2), facts).detail

        assert detail == "The newest season, within the 2 you keep"

    def test_a_deeper_season_counts_back_in_order(self) -> None:
        for rank, expected in ((2, "second-newest"), (3, "third-newest"), (7, "7th-newest")):
            facts = _facts(season_rank=Known(value=rank, source="sonarr"))
            detail = evaluate(Condition(field="season_rank", op=Op.LTE, value=2), facts).detail
            assert detail.startswith(f"The {expected} season, ")

    def test_a_gte_season_rule_does_not_claim_the_number_is_what_you_keep(self) -> None:
        """ "Remove rank 3 and older" keeps two seasons, not three. Phrasing the bar as
        "the 3 you keep" would misstate the owner's own rule by one season."""
        facts = _facts(season_rank=Known(value=5, source="sonarr"))
        detail = evaluate(Condition(field="season_rank", op=Op.GTE, value=3), facts).detail

        assert detail == "The 5th-newest season, at or past the 3 you set"
        assert "you keep" not in detail

    def test_a_list_valued_field_names_what_matched(self) -> None:
        """Not the whole fact on one side and the needle on the other: the owner should
        not have to intersect two lists by eye."""
        facts = _facts(genres=Known(value="Reality, Comedy", source="sonarr"))

        matched_eq = evaluate(Condition(field="genre", op=Op.EQ, value="Reality"), facts)
        matched_in = evaluate(Condition(field="genre", op=Op.IN, value="anime, comedy"), facts)

        assert matched_eq.detail == "Genre includes Reality"
        # Spelled the way the library spells it, not the way the comparison folded it.
        assert matched_in.detail == "Genre includes Comedy"

    def test_a_list_valued_miss_names_what_the_rule_wanted(self) -> None:
        facts = _facts(genres=Known(value="Reality, Comedy", source="sonarr"))

        missed_eq = evaluate(Condition(field="genre", op=Op.EQ, value="Anime"), facts)
        missed_in = evaluate(Condition(field="genre", op=Op.IN, value="Anime, Documentary"), facts)

        assert missed_eq.detail == "Genre does not include Anime"
        assert missed_in.detail == "Genre is none of Anime, Documentary"

    def test_a_single_valued_text_field_says_what_it_is(self) -> None:
        facts = _facts(quality=Known(value="Bluray-1080p", source="radarr"))

        assert (
            evaluate(Condition(field="quality", op=Op.CONTAINS, value="2160p"), facts).detail
            == "File quality does not contain 2160p"
        )
        assert (
            evaluate(Condition(field="quality", op=Op.CONTAINS, value="1080p"), facts).detail
            == "File quality contains 1080p"
        )
        assert (
            evaluate(Condition(field="quality", op=Op.EQ, value="SDTV"), facts).detail
            == "File quality is Bluray-1080p, not SDTV"
        )
        assert (
            evaluate(Condition(field="quality", op=Op.IN, value="SDTV, HDTV"), facts).detail
            == "File quality is Bluray-1080p, not one of SDTV, HDTV"
        )

    def test_no_explanation_leaks_an_operator_key_or_a_raw_bool(self) -> None:
        """The whole matrix, both readings. This is the regression that mattered: the
        old builder interpolated ``condition.op.value`` straight into the sentence."""
        facts = _facts(
            days_observed_unwatched=Known(value=900.0, source="tautulli"),
            distinct_watchers=Known(value=2, source="tautulli"),
            season_rank=Known(value=4, source="sonarr"),
            genres=Known(value="Reality, Comedy", source="sonarr"),
            quality=Known(value="Bluray-1080p", source="radarr"),
            in_curated_list=Known(value="A List", source="lists"),
            show_ended=Known(value=True, source="sonarr"),
            release_age_days=Known(value=1500.0, source="radarr"),
            requested=Known(value=False, source="seerr"),
        )
        targets: dict[str, int | str | bool] = {
            "days": 730,
            "bytes": 1_000_000_000,
            "count": 2,
            "rating_tenths": 75,
            "bool": True,
            "text": "Reality",
        }
        seen = 0
        for spec in BY_KEY.values():
            for op in spec.ops:
                # Stated, so the watcher fields explain their comparison rather than
                # reporting it unchecked -- this is the explanation matrix, and the
                # reach bound has its own tests.
                result = evaluate(
                    Condition(field=spec.key, op=op, value=targets[spec.type]),
                    facts,
                    window_days=365,
                )
                detail = result.detail
                seen += 1

                assert detail, f"{spec.key}/{op} explained nothing"
                # A sentence, so it never opens lower-case. Leading with a number
                # ("8.0 GB on disk") is the point, not an exception.
                assert not detail[0].islower(), f"{spec.key}/{op}: {detail}"
                assert "--" not in detail and "—" not in detail, f"{spec.key}/{op}: {detail}"
                assert "{" not in detail, f"{spec.key}/{op}: {detail}"
                assert "|" not in detail, f"{spec.key}/{op}: {detail}"
                # "in" and "contains" are ordinary English in the new copy ("Not
                # watched in 3 years"); the keys that could only be machine vocabulary
                # are not. The old shape -- "Label: value op target" -- is checked whole.
                for jargon in (" gte ", " lte ", " eq ", "True", "False"):
                    assert jargon not in detail, f"{spec.key}/{op} leaked {jargon!r}: {detail}"
                assert not re.search(r": \S+ (gte|lte|eq|in|contains) ", detail), detail
        assert seen > 20  # every field, every operator it accepts


class TestValueTypesAreValidatedAtTheBoundary:
    """A JSON string on a numeric field ("500" for a byte threshold) used to save and
    hash cleanly, then crash every subsequent scan inside score()/evaluate_all. The type
    check makes it a refusal at save time, naming the field."""

    def test_a_string_on_a_numeric_field_is_refused(self) -> None:
        with pytest.raises(ValueError, match="whole number"):
            Condition(field="size_bytes", op=Op.GTE, value="500").validate_for(Lane.PROTECT)

    def test_a_bool_on_a_numeric_field_is_refused(self) -> None:
        """bool is an int subclass in Python; it must not slip through as 0 or 1."""
        with pytest.raises(ValueError, match="whole number"):
            Condition(field="days_unwatched", op=Op.GTE, value=True).validate_for(Lane.PROTECT)

    def test_a_number_on_a_text_field_is_refused(self) -> None:
        """The quiet half of the same bug: contains/in with a non-text value never
        matches, so the protection the owner believes exists silently does nothing."""
        with pytest.raises(ValueError, match="expects text"):
            Condition(field="genre", op=Op.CONTAINS, value=7).validate_for(Lane.PROTECT)

    def test_a_string_on_a_boolean_field_is_refused(self) -> None:
        with pytest.raises(ValueError, match="true or false"):
            Condition(field="show_ended", op=Op.EQ, value="yes").validate_for(Lane.PROTECT)

    def test_a_well_typed_condition_still_validates(self) -> None:
        Condition(field="size_bytes", op=Op.GTE, value=500).validate_for(Lane.PROTECT)
        Condition(field="genre", op=Op.CONTAINS, value="horror").validate_for(Lane.PROTECT)


class TestARuleThatCouldNeverMatchIsRefusedAtSave:
    """Rule 108's three spellings of the same defect, all refused by
    ``Condition._validate_value_type``.

    Each one saves, hashes, and renders on Policy as an ordinary live protection while
    covering nothing, which is the state this codebase treats as worse than no rule at
    all: the operator counts on it. The loud sibling (``contains ""``, which matches the
    ENTIRE library) is here too, because one check refuses both directions.
    """

    def test_an_empty_text_value_is_refused(self) -> None:
        """``contains ""`` is ``"" in anything``, so a condemn rule written that way puts
        its full weight on every item whose text fact is Known."""
        with pytest.raises(ValueError, match="needs a value"):
            Condition(field="genre", op=Op.CONTAINS, value="").validate_for(Lane.CONDEMN)

    def test_a_whitespace_only_text_value_is_refused(self) -> None:
        with pytest.raises(ValueError, match="needs a value"):
            Condition(field="genre", op=Op.CONTAINS, value="   ").validate_for(Lane.CONDEMN)

    def test_a_comma_only_in_list_is_refused(self) -> None:
        """It survives the strip above and splits to no elements, so it can never match."""
        with pytest.raises(ValueError, match="at least one value"):
            Condition(field="genre", op=Op.IN, value=" , ").validate_for(Lane.PROTECT)

    def test_an_eq_target_carrying_the_separator_is_refused_on_a_multi_field(self) -> None:
        """The quietest of the three. ``on_lists`` is one comma-joined string and
        ``_compare`` splits it back to test membership, so a list named "Kids, Holiday"
        is never an element of its own fact -- the keep rule reads live and keeps nothing.
        ``list_config._clean_name`` refuses the comma where the name is typed; this is the
        boundary a hand-written or imported policy body arrives through."""
        with pytest.raises(ValueError, match="comma"):
            Condition(field="on_list", op=Op.EQ, value="Kids, Holiday").validate_for(Lane.PROTECT)

    def test_the_same_refusal_covers_the_other_multi_field(self) -> None:
        """``genre`` is the multi field on BOTH lanes, so the refusal is not on_list's alone."""
        with pytest.raises(ValueError, match="comma"):
            Condition(field="genre", op=Op.EQ, value="Anime, Comedy").validate_for(Lane.CONDEMN)

    def test_a_comma_is_still_fine_where_the_fact_is_not_joined_on_one(self) -> None:
        """``quality`` is the single-valued text field, compared whole, so a comma inside it
        is an ordinary character rather than a separator that would strand the rule."""
        Condition(field="quality", op=Op.EQ, value="Bluray-1080p, Remux").validate_for(Lane.PROTECT)

    def test_a_comma_is_still_fine_where_it_is_the_separator_doing_its_job(self) -> None:
        """``in`` splits its target on commas by design; only ``eq`` reads the whole string
        as one element."""
        Condition(field="on_list", op=Op.IN, value="Kids, Holiday").validate_for(Lane.PROTECT)
        Condition(field="on_list", op=Op.EQ, value="Holiday").validate_for(Lane.PROTECT)


class TestABadStoredValueDegradesInsteadOfCrashing:
    """Belt and suspenders under the boundary check: a rule that somehow carries an
    uncomparable value (stored before the type check existed) must degrade that item as
    blocked -- amber, could-not-check -- never raise out of a scan."""

    def test_evaluate_returns_blocked_not_an_exception(self) -> None:
        bad = Condition.__new__(Condition)  # bypass validation, as a legacy stored rule would
        object.__setattr__(bad, "field", "size_bytes")
        object.__setattr__(bad, "op", Op.GTE)
        object.__setattr__(bad, "value", "500")

        result = evaluate(bad, _facts())

        assert result.blocked is True
        assert result.matched is False
        assert "could not check" in result.detail

    def test_a_blocked_bad_value_cannot_protect_or_condemn(self) -> None:
        """Through the gate wrapper: the worst a corrupt stored rule can do is abstain
        with an amber detail. It can never fire, and it can never crash the judge."""
        bad = Condition.__new__(Condition)
        object.__setattr__(bad, "field", "size_bytes")
        object.__setattr__(bad, "op", Op.GTE)
        object.__setattr__(bad, "value", "500")

        outcome = CustomProtectGate(bad).evaluate(_facts())

        assert outcome.outcome is ABSTAIN
        assert outcome.blocked is True
