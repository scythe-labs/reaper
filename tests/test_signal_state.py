# SPDX-License-Identifier: AGPL-3.0-or-later
"""What a row's zero actually means.

Four very different situations all end at a contribution of ``0``: a rule that pushed
nothing because the value argues for keeping, a yes/no rule that does not describe this
item, a field with nothing recorded, and an input we could not read at all. Only the
last lowers coverage, and only the last may be rendered as "we could not look".

``SignalState`` carries that distinction to the wire, because it is gone by the time
anything downstream sees the result.
"""

from __future__ import annotations

import json

from reaper.api.schemas import SignalContribution
from reaper.engine.fields import Condition, Op
from reaper.engine.gates import ABSTAIN, Evaluation, Facts, GateId, GateResult
from reaper.engine.observation import Absent, Known, Unknown
from reaper.engine.policy import PolicyBody, SignalSetting
from reaper.engine.signals import (
    CustomSignalConfig,
    SignalConfig,
    SignalId,
    SignalState,
    evaluate_custom,
    evaluate_signal,
    score,
)
from reaper.services.snapshot import _explain

EVALUATION = Evaluation(
    results=[
        GateResult(
            GateId.RATING_FLOOR,
            ABSTAIN,
            detail="5.4 on IMDb from 6,000 votes, below the 7.5 you keep.",
        )
    ]
)


def _facts(**overrides: object) -> Facts:
    base: dict[str, object] = {
        "title": "A Title",
        "days_observed_unwatched": Known(value=900.0, source="tautulli"),
        "distinct_watchers": Known(value=0, source="tautulli"),
        "distinct_watchers_all_time": Known(value=0, source="tautulli"),
        "size_bytes": Known(value=4_000_000_000, source="radarr"),
        "imdb_rating_tenths": Known(value=60, source="imdb"),
        "imdb_votes": Known(value=1_000, source="imdb"),
        "season_rank": Absent(source="radarr"),
        "is_streaming_now": Known(value=False, source="tautulli"),
        "is_managed": Known(value=True, source="radarr"),
        "in_curated_list": Absent(source="lists"),
        "is_whitelisted": Known(value=False, source="plex"),
    }
    return Facts(**{**base, **overrides})  # type: ignore[arg-type]


UNWATCHED = SignalConfig(signal=SignalId.UNWATCHED, weight=70, saturate_at=1825, floor=365)
SEASON_RANK = SignalConfig(signal=SignalId.SEASON_RANK, weight=15, saturate_at=6)


class TestTheFourStates:
    def test_a_signal_that_pushed_toward_removing_adds(self) -> None:
        result = evaluate_signal(UNWATCHED, _facts())

        assert result.pressure > 0
        assert result.state is SignalState.ADDS

    def test_a_measured_value_below_the_floor_argues_for_keeping(self) -> None:
        # Watched 30 days ago: read, real, and a reason to keep the file.
        result = evaluate_signal(UNWATCHED, _facts(days_observed_unwatched=Known(30.0, "tautulli")))

        assert result.pressure == 0
        assert result.evaluated is True
        assert result.state is SignalState.ARGUES_KEEP

    def test_a_yes_no_rule_that_did_not_match_does_not_apply(self) -> None:
        # It did not match, which is not the same as arguing for keeping: nothing about
        # the item was found to be worth keeping.
        rule = CustomSignalConfig(
            name="a rule",
            weight=30,
            kind="boolean",
            field="genre",
            condition=Condition(field="genre", op=Op.CONTAINS, value="nothing-like-this"),
        )
        result = evaluate_custom(rule, _facts(genres=Known("Drama", "plex")))

        assert result.pressure == 0
        assert result.evaluated is True
        assert result.state is SignalState.NOT_APPLICABLE

    def test_a_field_with_nothing_recorded_does_not_apply(self) -> None:
        rule = CustomSignalConfig(
            name="a rule", weight=30, kind="graded", field="season_rank", saturate_at=5
        )
        result = evaluate_custom(rule, _facts(season_rank=Absent(source="sonarr")))

        assert result.detail.endswith(": none recorded")
        assert result.evaluated is True
        assert result.state is SignalState.NOT_APPLICABLE

    def test_an_input_we_could_not_read_is_unreadable(self) -> None:
        result = evaluate_signal(
            UNWATCHED,
            _facts(days_observed_unwatched=Unknown(source="tautulli", reason="no watch history")),
        )

        assert result.evaluated is False
        assert result.state is SignalState.UNREADABLE

    def test_only_unreadable_lowers_coverage(self) -> None:
        # The three evaluated states all sit at zero pressure, and none of them may be
        # mistaken for "we could not look".
        keeping = evaluate_signal(
            UNWATCHED, _facts(days_observed_unwatched=Known(30.0, "tautulli"))
        )
        unreadable = evaluate_signal(
            UNWATCHED,
            _facts(days_observed_unwatched=Unknown(source="tautulli", reason="no watch history")),
        )

        assert keeping.evaluated is True
        assert unreadable.evaluated is False
        assert unreadable.state is SignalState.UNREADABLE

    def test_a_specials_absent_rank_does_not_apply_and_keeps_coverage(self) -> None:
        # A special (season 0) is deliberately left out of the season ranking, so its rank
        # is Absent -- we looked, and it genuinely has no rank slot. The built-in signal
        # must read that as NOT_APPLICABLE, evaluated, so it never lands in "Couldn't check"
        # and never drags the special's coverage down for a rank it was never meant to have,
        # exactly as the custom path already reads an Absent field.
        result = evaluate_signal(SEASON_RANK, _facts(season_rank=Absent(source="sonarr")))

        assert result.pressure == 0
        assert result.evaluated is True
        assert result.state is SignalState.NOT_APPLICABLE
        assert result.detail == "not one of the numbered seasons"

    def test_a_rank_we_could_not_read_is_still_unreadable(self) -> None:
        # A genuine Sonarr read failure is Unknown, not Absent, and stays "we could not
        # look" -- the one case that keeps the old wording and rightly lowers coverage.
        result = evaluate_signal(
            SEASON_RANK,
            _facts(season_rank=Unknown(source="sonarr", reason="Sonarr unreachable")),
        )

        assert result.evaluated is False
        assert result.state is SignalState.UNREADABLE
        assert result.detail == "could not tell which season this is"


def _policy() -> PolicyBody:
    return PolicyBody(
        media_type="movie",
        condemn_at=70,
        gates=(),
        signals=(
            SignalSetting(signal=SignalId.UNWATCHED, weight=100, saturate_at=1825, floor=365),
        ),
    )


class TestWhatReachesTheWire:
    def test_every_row_carries_a_state(self) -> None:
        item = score([UNWATCHED], _facts())
        rows = json.loads(_explain(EVALUATION, item, _policy()))["signals"]

        assert rows
        assert all(row["state"] in {s.value for s in SignalState} for row in rows)

    def test_a_turned_off_rule_never_reaches_the_wire(self) -> None:
        # Weight 0 is out of the denominator and worth no points, and its stored detail
        # is engine shorthand no owner should ever be shown.
        off = SignalConfig(signal=SignalId.SIZE, weight=0)
        item = score([UNWATCHED, off], _facts())
        rows = json.loads(_explain(EVALUATION, item, _policy()))["signals"]

        assert [row["id"] for row in rows] == [SignalId.UNWATCHED.value]
        assert all(row["detail"] != "disabled" for row in rows)
        assert all(row["weight"] > 0 for row in rows)

    def test_the_rows_add_up_to_the_score(self) -> None:
        # The panel shows each row's share of the score, so the shares must sum to the
        # score over the same denominator the engine used.
        item = score(
            [UNWATCHED, SignalConfig(SignalId.LOW_RATING, weight=30, saturate_at=70)], _facts()
        )
        explanation = json.loads(_explain(EVALUATION, item, _policy()))
        rows = explanation["signals"]

        denominator = sum(row["weight"] for row in rows)
        total = 100 * sum(row["contribution"] for row in rows) / denominator

        assert round(total) == explanation["score"]

    def test_the_score_is_the_whole_number_the_decision_used(self) -> None:
        item = score([UNWATCHED], _facts())
        explanation = json.loads(_explain(EVALUATION, item, _policy()))

        assert explanation["score"] == round(item.value)
        assert isinstance(explanation["score"], int)


class TestTheRampReachesTheWire:
    """A row carries the line it was measured against, so the panel can state the
    arithmetic without asking a policy that may have moved since the scan."""

    def test_a_built_in_row_carries_the_ramp_it_was_scored_under(self) -> None:
        # Neither bound is the shipped default for this signal (60/0) or `SignalConfig`'s
        # own (1/0), so a config that never reached the result cannot produce these
        # numbers by accident (rule 141).
        item = score(
            [SignalConfig(SignalId.LOW_RATING, weight=100, saturate_at=72, floor=8)],
            _facts(imdb_rating_tenths=Known(value=64, source="imdb")),
        )
        row = json.loads(_explain(EVALUATION, item, _policy()))["signals"][0]

        assert (row["floor"], row["saturate_at"]) == (8, 72)

    def test_a_graded_rule_of_your_own_carries_its_ramp_too(self) -> None:
        graded = CustomSignalConfig(
            name="big files",
            weight=100,
            kind="graded",
            field="size_bytes",
            floor=11,
            saturate_at=83,
        )
        item = score([], _facts(), custom_condemn=[graded])
        row = json.loads(_explain(EVALUATION, item, _policy()))["signals"][0]

        assert (row["floor"], row["saturate_at"]) == (11, 83)

    def test_a_yes_no_rule_of_your_own_carries_no_ramp(self) -> None:
        # It matched or it did not; it cannot land part-way, so there is no line to state
        # and inventing one would draw a ramp the rule does not have.
        boolean = CustomSignalConfig(
            name="the show ended",
            weight=100,
            kind="boolean",
            field="show_ended",
            condition=Condition(field="show_ended", op=Op.EQ, value=True),
        )
        item = score([], _facts(), custom_condemn=[boolean])
        row = json.loads(_explain(EVALUATION, item, _policy()))["signals"][0]

        assert row["floor"] is None
        assert row["saturate_at"] is None


class TestHistoricalRows:
    def test_a_row_stored_before_the_ramp_existed_asserts_no_line(self) -> None:
        # Two different things arrive as None here -- a rule with no ramp, and a row frozen
        # before these fields shipped -- and the panel treats both the same way, by saying
        # nothing. A `0` default would instead assert a line about every legacy row, which
        # is the failure rule 142 names.
        row = SignalContribution.model_validate(
            {
                "id": "low_rating",
                "contribution": 0.0,
                "weight": 10,
                "detail": "IMDb 6.4",
                "evaluated": True,
                "state": "argues_keep",
            }
        )

        assert row.floor is None
        assert row.saturate_at is None

    def test_a_row_stored_before_the_state_existed_still_parses(self) -> None:
        row = SignalContribution.model_validate(
            {
                "id": "unwatched",
                "contribution": 0.0,
                "weight": 70,
                "detail": "not watched in 2 years",
                "evaluated": True,
            }
        )

        # Absent, not guessed. The UI reads a missing state as "did not apply": claiming
        # an old row argued for keeping, when nothing recorded whether it did, would
        # overstate the case for keeping the file.
        assert row.state is None

    def test_a_stored_state_round_trips(self) -> None:
        row = SignalContribution.model_validate(
            {
                "id": "unwatched",
                "contribution": 0.0,
                "weight": 70,
                "detail": "watched 30 days ago",
                "evaluated": True,
                "state": "argues_keep",
            }
        )

        assert row.state is SignalState.ARGUES_KEEP
