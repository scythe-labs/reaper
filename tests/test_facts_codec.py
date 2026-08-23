# SPDX-License-Identifier: AGPL-3.0-or-later
"""The frozen-facts codec must round-trip EXACTLY.

The simulator replays the real engine over these thawed facts, so any lossy field is a
wrong preview -- and a three-state arm that flips (an ``Unknown`` thawed as ``Absent``) is a
fail-OPEN regression, the exact bug the whole Observation type exists to prevent. These
tests assert the identity ``from_dict(to_dict(x)) == x`` across every arm and every field.
"""

from __future__ import annotations

import dataclasses
import json

from hypothesis import given, settings
from hypothesis import strategies as st

from reaper.engine.facts_codec import (
    _OBS_FIELDS,
    _result_to_dict,
    facts_from_dict,
    facts_to_dict,
)
from reaper.engine.gates import ABSTAIN, PROTECT, Facts, GateId, GateResult
from reaper.engine.observation import Absent, Known, Observation, Unknown
from reaper.engine.reason import Reason, from_wire, legacy, to_wire
from reaper.ratings import Rating, RatingSource

#: An ``Unknown.reason`` that carries params (the movie/season media select on the
#: match-status and no-added-at/no-size causes, ``gates.no_key_reason`` and friends) --
#: the other arm of ``Unknown.reason: str | Reason``, alongside the bare id below.
_UNKNOWN_REASON = st.builds(
    Reason,
    id=st.text(min_size=1, max_size=15),
    params=st.dictionaries(
        st.text(min_size=1, max_size=10),
        st.one_of(st.text(max_size=10), st.integers(-100, 100), st.booleans()),
        max_size=3,
    ),
)


def _obs[T](value_strategy: st.SearchStrategy[T]) -> st.SearchStrategy[Observation[T]]:
    return st.one_of(
        value_strategy.map(lambda v: Known(value=v, source="s")),
        st.just(Absent(source="s")),
        st.text(min_size=1, max_size=10).map(lambda r: Unknown(reason=r, source="s")),
        _UNKNOWN_REASON.map(lambda r: Unknown(reason=r, source="s")),
    )


_RATING = st.builds(
    Rating,
    source=st.sampled_from(list(RatingSource)),
    value=st.floats(0, 10, allow_nan=False, allow_infinity=False),
    votes=st.one_of(st.none(), st.integers(0, 5_000_000)),
    provider=st.sampled_from(["imdb-dataset", "radarr", "plex"]),
)


@st.composite
def _facts(draw: st.DrawFn) -> Facts:
    ints = st.integers(-1, 5_000_000)
    return Facts(
        title=draw(st.text(max_size=20)),
        days_observed_unwatched=draw(_obs(st.floats(0, 9000, allow_nan=False))),
        distinct_watchers=draw(_obs(ints)),
        distinct_watchers_all_time=draw(_obs(ints)),
        size_bytes=draw(_obs(st.integers(0, 10**12))),
        imdb_rating_tenths=draw(_obs(st.integers(0, 100))),
        imdb_votes=draw(_obs(ints)),
        season_rank=draw(_obs(st.integers(1, 40))),
        is_streaming_now=draw(_obs(st.booleans())),
        is_managed=draw(_obs(st.booleans())),
        in_curated_list=draw(_obs(st.text(max_size=15))),
        is_whitelisted=draw(_obs(st.booleans())),
        requested=draw(_obs(st.booleans())),
        genres=draw(_obs(st.text(max_size=15))),
        release_age_days=draw(_obs(st.floats(0, 40000, allow_nan=False))),
        quality=draw(_obs(st.text(max_size=15))),
        show_ended=draw(_obs(st.booleans())),
        rewatch_viewings=draw(_obs(st.integers(0, 5_000))),
        rewatch_last_play_days=draw(_obs(st.floats(0, 9000, allow_nan=False))),
        rewatch_cohort_n=draw(_obs(st.integers(0, 5_000))),
        rewatch_cohort_k=draw(_obs(st.integers(0, 5_000))),
        ratings=tuple(draw(st.lists(_RATING, max_size=5))),
    )


class TestFactsRoundTrip:
    @given(facts=_facts())
    @settings(max_examples=300)
    def test_facts_survive_a_json_round_trip(self, facts: Facts) -> None:
        thawed, extra = facts_from_dict(json.loads(json.dumps(facts_to_dict(facts))))
        assert thawed == facts
        assert extra == ()

    @given(facts=_facts())
    @settings(max_examples=100)
    def test_every_observation_arm_is_preserved(self, facts: Facts) -> None:
        thawed, _ = facts_from_dict(facts_to_dict(facts))
        for name in _OBS_FIELDS:
            before, after = getattr(facts, name), getattr(thawed, name)
            # The arm (Known vs Absent vs Unknown) must not change -- that is the fail-safe.
            assert type(before) is type(after), f"{name}: {before} -> {after}"

    def test_the_season_guard_result_round_trips(self) -> None:
        facts = _bare_facts()
        # ``defers_to_owner=True`` is deliberately NOT the default (rule 141): both fixtures
        # once left it at ``False``, so the codec dropping the field compared equal on both
        # sides of the round trip and this test could not see the loss.
        results = (
            GateResult(GateId.SEASON_PROGRESSION, PROTECT, detail=legacy("kept newest")),
            GateResult(
                GateId.SEASON_PROGRESSION,
                ABSTAIN,
                blocked=True,
                detail=legacy("conflict"),
                defers_to_owner=True,
            ),
        )
        _, extra = facts_from_dict(facts_to_dict(facts, extra_results=results))
        assert extra == results

    def test_the_codec_carries_every_gate_result_field(self) -> None:
        """Rule 103's drift guard over a hand-written serializer. ``_result_to_dict`` names
        ``GateResult``'s fields one at a time, so a field added to the dataclass and not
        added there is dropped in silence -- which is how ``defers_to_owner`` came to be
        frozen away, making the simulator replay disagree with the scan about whether a
        hand reap is honored. Equality above cannot catch the next one on its own, because
        a field both fixtures leave at its default compares equal either way."""
        frozen = _result_to_dict(
            GateResult(
                GateId.SEASON_PROGRESSION,
                ABSTAIN,
                detail=legacy("d"),
                blocked=True,
                defers_to_owner=True,
            )
        )
        dropped = {f.name for f in dataclasses.fields(GateResult)} - set(frozen)
        assert dropped == set(), f"GateResult fields the codec does not freeze: {sorted(dropped)}"


def _bare_facts() -> Facts:
    absent: Absent = Absent(source="t")
    return Facts(
        title="x",
        days_observed_unwatched=Known(value=900.0, source="t"),
        distinct_watchers=absent,
        distinct_watchers_all_time=absent,
        size_bytes=Known(value=1, source="t"),
        imdb_rating_tenths=absent,
        imdb_votes=absent,
        season_rank=Known(value=1, source="t"),
        is_streaming_now=Known(value=False, source="t"),
        is_managed=Known(value=True, source="t"),
        in_curated_list=absent,
        is_whitelisted=Known(value=False, source="t"),
    )


class TestTheWireDecoderNeverRaises:
    def test_a_blob_nested_past_any_real_reason_degrades_instead_of_recursing(self) -> None:
        """Rule 96 for the reason decoder. The engine writes at most three levels of
        nesting, so a deeply nested stored blob is corruption -- and an unbounded recursion
        raises ``RecursionError``, which no reader's except tuple names -- the season
        bundle reader above ``api.simulate._season_guard_replay`` chose its exceptions by
        hand -- so it would raise a row off the queue instead of degrading."""
        blob: dict[str, object] = {"k": "x"}
        for _ in range(2000):
            blob = {"k": "x", "p": {"inner": blob}}

        decoded = from_wire(blob)

        assert isinstance(decoded, Reason)

    def test_a_reason_at_the_engines_real_depth_still_round_trips(self) -> None:
        """The cap must sit far above anything the engine writes, or it would be the
        corruption. Three levels is the deepest live shape (a conflict's cause in its
        because-slot in the row)."""
        deep = Reason(
            "conflict.shortfall",
            {
                "because": Reason("because.keep_last"),
                "cause": Reason("cause.history_reach_short", {"reach_days": 90.0}),
            },
        )
        assert from_wire(to_wire(deep)) == deep
