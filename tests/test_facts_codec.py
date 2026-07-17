# SPDX-License-Identifier: AGPL-3.0-or-later
"""The frozen-facts codec must round-trip EXACTLY.

The simulator replays the real engine over these thawed facts, so any lossy field is a
wrong preview -- and a three-state arm that flips (an ``Unknown`` thawed as ``Absent``) is a
fail-OPEN regression, the exact bug the whole Observation type exists to prevent. These
tests assert the identity ``from_dict(to_dict(x)) == x`` across every arm and every field.
"""

from __future__ import annotations

import json

from hypothesis import given, settings
from hypothesis import strategies as st

from reaper.engine.facts_codec import _OBS_FIELDS, facts_from_dict, facts_to_dict
from reaper.engine.gates import ABSTAIN, PROTECT, Facts, GateId, GateResult
from reaper.engine.observation import Absent, Known, Observation, Unknown
from reaper.ratings import Rating, RatingSource


def _obs(value_strategy: st.SearchStrategy[object]) -> st.SearchStrategy[Observation[object]]:
    return st.one_of(
        value_strategy.map(lambda v: Known(value=v, source="s")),
        st.just(Absent(source="s")),
        st.text(min_size=1, max_size=10).map(lambda r: Unknown(reason=r, source="s")),
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
        others_watching=draw(_obs(ints)),
        requested=draw(_obs(st.booleans())),
        genres=draw(_obs(st.text(max_size=15))),
        release_age_days=draw(_obs(st.floats(0, 40000, allow_nan=False))),
        quality=draw(_obs(st.text(max_size=15))),
        show_ended=draw(_obs(st.booleans())),
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
        results = (
            GateResult(GateId.SEASON_PROGRESSION, PROTECT, detail="kept newest"),
            GateResult(GateId.SEASON_PROGRESSION, ABSTAIN, blocked=True, detail="conflict"),
        )
        _, extra = facts_from_dict(facts_to_dict(facts, extra_results=results))
        assert extra == results


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
        others_watching=absent,
    )
