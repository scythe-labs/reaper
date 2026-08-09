# SPDX-License-Identifier: AGPL-3.0-or-later
"""An unreadable stored explanation degrades one row of the simulation, toward KEEPING.

The policy editor's live preview replays the last snapshot on a 250 ms debounce, so every
row of it is read on every keystroke of a threshold drag. Two of its readers used to guard
the ``json.loads`` and then call ``.get`` on whatever came back: a stored top level that is
a list, a null or a number raised an ``AttributeError`` out of ``POST /api/policy/simulate``
and 500ed the preview, while the review queue rendered the same row fine.

Guarding them is only half of it. The value they fall back to decides which way an
unreadable row lands, and the two fall opposite ways on purpose (rule 96):

* ``_has_blocked_protections`` is the only thing standing between a row and a score-based
  condemn, so an explanation nobody can read reads as BLOCKED. The row abstains and stays
  out of the previewed deletion count.
* ``_fired_gates`` only attributes a row the caller has ALREADY counted as protected, so an
  unreadable one contributes no attribution and cannot move anything toward deletion.

A third reader, ``_primary_reason``, sorted the condemned lane's signals on a raw stored
value; two entries of different types raised a ``TypeError`` and blanked the lane.

The last class covers the simulator's OTHER tier, the frozen-Facts replay, which had no
route-level test at all: it now judges through ``snapshot.judge_facts`` and applies the hand
override through ``snapshot.effective_fate`` rather than assembling that pipeline itself
(rules 3/22), and the case that matters is a hand reap the engine cannot honor.
"""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path
from typing import Any, NamedTuple

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine as sa_create_engine
from sqlalchemy import update as sa_update
from sqlalchemy.orm import Session

from reaper.api.policy import _policy_out, _to_body
from reaper.api.review import _contribution
from reaper.api.schemas import ConditionIn, GateSettingIn, PolicyIn, SignalSettingIn
from reaper.api.simulate import _fired_gates, _has_blocked_protections
from reaper.clock import utcnow
from reaper.config import Settings
from reaper.db.base import Base
from reaper.db.models import Candidate, ListConfig, Snapshot, WhitelistEntry
from reaper.engine import facts_codec
from reaper.engine.gates import Facts
from reaper.engine.observation import Absent, Known
from reaper.engine.policy import (
    DEFAULT_MOVIE_POLICY,
    DEFAULT_TV_POLICY,
    SCHEMA_VERSION,
    GateSetting,
    PolicyBody,
    ProfileSettings,
    SignalSetting,
    combine_hashes,
)
from reaper.main import create_app

from ._auth import login
from ._lists import seeded_fingerprint

#: The draft policy every simulation below is run under. Its scoring hash has to be the one
#: the fixture snapshot was "scored" with, or the route refuses to re-decide at all -- so
#: the gates and signals here and in ``_fixture_scoring_hash`` are the same list. The
#: ``whitelisted`` gate is retired (list membership protects through ``on_list`` keep rules
#: now), so it appears in stored EXPLANATIONS below but never in a draft: the save boundary
#: refuses it, which ``test_a_retired_gate_is_refused_at_the_save_boundary`` pins.
GATES: list[dict[str, Any]] = [
    {"gate": "min_dormancy", "threshold": 1095},
    {"gate": "rating_floor", "threshold": 75},
]
SIGNALS: list[dict[str, Any]] = [
    {"signal": "unwatched", "weight": 100, "saturate_at": 1825, "floor": 365}
]

#: The size on every row, so the reclaimable total is a plain multiple of the condemned
#: count rather than a number that needs its own arithmetic.
SIZE = 1_000_000_000


def _fixture_scoring_hash() -> str:
    """The combined movie+TV scoring hash of the policies the fixture was scored with."""
    movie = PolicyBody(
        condemn_at=70,
        gates=tuple(GateSetting.model_validate(g) for g in GATES),
        signals=tuple(SignalSetting.model_validate(s) for s in SIGNALS),
    )
    return combine_hashes(movie.scoring_hash(), DEFAULT_TV_POLICY.scoring_hash())


def _healthy(**overrides: Any) -> str:
    """A stored explanation shaped exactly as ``snapshot._explain`` writes one."""
    body: dict[str, Any] = {
        "score": 95.0,
        "base_score": 95.0,
        "keep_discount": 0.0,
        "threshold": 70,
        "coverage": 1.0,
        "match": {"status": "matched", "by": "tmdb", "detail": None, "rating_key": 1},
        "signals": [
            {
                "id": "unwatched",
                "contribution": 95.0,
                "weight": 100,
                "detail": "not watched in 5 years",
                "evaluated": True,
                "state": "adds",
            }
        ],
        "protections_fired": [],
        "protections_checked": [],
        "protections_unknown": [],
    }
    return json.dumps({**body, **overrides})


#: Signals whose contributions are stored as three different types. Only one is a number,
#: and it is deliberately not first: a sort that cannot compare the three raised a
#: TypeError, and one that silently kept the stored order would pick the wrong reason.
MIXED_SIGNALS: list[dict[str, Any]] = [
    {
        "id": "unwatched",
        "contribution": "not a number",
        "weight": 40,
        "detail": "not watched in 5 years",
        "evaluated": True,
        "state": "adds",
    },
    {
        "id": "few_watchers",
        "contribution": None,
        "weight": 30,
        "detail": "nobody here has watched it",
        "evaluated": True,
        "state": "adds",
    },
    {
        "id": "low_rating",
        "contribution": 12.5,
        "weight": 30,
        "detail": "poorly rated where it is rated",
        "evaluated": True,
        "state": "adds",
    },
]

BLOCKED_ENTRY = {
    "gate": "server_popularity",
    "detail": (
        "could not check who watched it in the last year: your watch history only goes back 90 days"
    ),
}


class Row(NamedTuple):
    """One fixture row, and what the simulator owes it.

    ``bucket`` is written from the contract in this module's docstring, not from the
    branches that implement it: an unreadable explanation keeps, a readable one is judged
    on its own merits.
    """

    media_key: str
    verdict: str
    explanation: str
    bucket: str
    why: str


ROWS: tuple[Row, ...] = (
    Row(
        "radarr:1:1",
        "abstain",
        "[1, 2, 3]",
        "abstained",
        "top level is a list: unreadable, so it holds",
    ),
    Row("radarr:1:2", "abstain", "null", "abstained", "top level is null: unreadable, so it holds"),
    Row(
        "radarr:1:3", "abstain", "7", "abstained", "top level is a number: unreadable, so it holds"
    ),
    Row(
        "radarr:1:4",
        "abstain",
        _healthy(),
        "condemned",
        "the control: readable, nothing blocking, and it scores over the draft threshold",
    ),
    Row(
        "radarr:1:5",
        "abstain",
        _healthy(protections_unknown=[BLOCKED_ENTRY]),
        "abstained",
        "genuinely blocked, and unchanged by any of this",
    ),
    Row(
        "radarr:1:6",
        "protect",
        "[1, 2, 3]",
        "protected",
        "a protection fired, but which one cannot be read",
    ),
    Row(
        "radarr:1:7",
        "protect",
        _healthy(
            protections_fired=[
                {"gate": "rating_floor", "detail": "well rated: 8.0 on IMDb from 250,000 votes"}
            ]
        ),
        "protected",
        "the attribution control: this is the one row the spared-by tally can name",
    ),
    Row(
        "radarr:1:8",
        "condemn",
        "null",
        "abstained",
        "stored condemned, but the explanation is unreadable, so the reap is given up",
    ),
    Row(
        "radarr:1:9",
        "condemn",
        _healthy(signals=MIXED_SIGNALS),
        "condemned",
        "readable, and its mixed-type contributions must not blank the lane",
    ),
)

#: The counts the table above adds up to, DERIVED from it, so ``Row.bucket`` is the one
#: statement of what each row is owed rather than dead data beside a second hand-kept copy.
#:
#: What this can and cannot discriminate, stated plainly (rule 118): the route reports bucket
#: TOTALS, not a bucket per row, so two rows swapping buckets is invisible here and would stay
#: invisible however these numbers were written. That is what
#: ``test_the_unreadable_rows_are_kept_and_the_readable_one_is_not`` is for -- it separates the
#: rows by the property under test instead of counting them.
EXPECTED = dict(Counter(r.bucket for r in ROWS))


@pytest.fixture
def client(tmp_path: Path) -> Iterator[TestClient]:
    """A snapshot carrying every explanation shape in ``ROWS``."""
    settings = Settings(data_dir=tmp_path, secret_key="k")
    engine = sa_create_engine(settings.sync_database_url)
    Base.metadata.create_all(engine)
    # What a scan records about the lists it gathered membership under: without it the
    # simulator refuses, which is right for a snapshot that cannot say and useless here.
    list_hash = seeded_fingerprint(settings)

    now = utcnow()
    with Session(engine) as session:
        snapshot = Snapshot(
            created_at=now,
            policy_hash=combine_hashes(
                DEFAULT_MOVIE_POLICY.policy_hash(), DEFAULT_TV_POLICY.policy_hash()
            ),
            scoring_hash=_fixture_scoring_hash(),
            list_config_hash=list_hash,
            horizon_at=now,
            item_count=len(ROWS),
            degraded=False,
        )
        session.add(snapshot)
        session.flush()
        for index, row in enumerate(ROWS, start=1):
            session.add(
                Candidate(
                    snapshot_id=snapshot.id,
                    media_key=row.media_key,
                    title=f"Example Movie {index}",
                    media_type="movie",
                    size_bytes=SIZE,
                    verdict=row.verdict,
                    score=95,
                    coverage_bp=10_000,
                    explanation_json=row.explanation,
                    created_at=now,
                )
            )
        session.commit()
    engine.dispose()

    with TestClient(create_app(settings)) as c:
        login(c, settings)
        yield c


def _simulate(client: TestClient, condemn_at: int = 70) -> dict[str, Any]:
    response = client.post(
        "/api/policy/simulate",
        json={
            "condemn_at": condemn_at,
            "coverage_floor_bp": 0,
            "gates": GATES,
            "signals": SIGNALS,
        },
    )
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


class TestBothTiersSendTheChangedCount:
    """#488: the panel must never have to know which tier answered.

    On the threshold path the count cannot exceed the two deltas: a protection wins at any
    threshold and a hand override wins at any threshold, so the only lane move left is across
    the threshold itself. That is precisely why the number is SENT rather than worked out in
    the browser from the deltas -- the arithmetic that holds here is wrong on the replay path
    (``TestTheFrozenFactsReplay``), and a browser cannot tell which one it is reading.

    Both expectations are read off ``ROWS``: at 70 the readable unblocked row (``radarr:1:4``)
    condemns while the stored-condemn row whose explanation will not parse gives its reap up,
    so one row moves each way. At 99 nothing clears the threshold, so the readable stored
    condemn (``radarr:1:9``) gives its up as well and both moves are in the same direction.
    """

    def test_the_threshold_path_counts_both_directions(self, client: TestClient) -> None:
        result = _simulate(client, condemn_at=70)

        assert result["exact"] is True
        assert result["changed_titles"] == 2
        assert result["newly_condemned"] == 1
        assert result["no_longer_condemned"] == 1

    def test_raising_the_threshold_past_every_score_moves_rows_one_way(
        self, client: TestClient
    ) -> None:
        result = _simulate(client, condemn_at=99)

        assert result["exact"] is True
        assert result["changed_titles"] == 2
        assert result["newly_condemned"] == 0
        assert result["no_longer_condemned"] == 2


class TestTheSimulationSurvivesABrokenRow:
    def test_the_route_answers_at_all(self, client: TestClient) -> None:
        """The bug, at its plainest: one legacy row 500ed the preview on every keystroke.

        ``_simulate`` asserts the 200, so this pins that the answer is a real simulation
        (``exact``) rather than the refusal the route returns when it cannot replay.
        """
        assert _simulate(client)["exact"] is True

    def test_every_row_lands_in_the_bucket_the_table_gives_it(self, client: TestClient) -> None:
        result = _simulate(client)
        assert {k: result[k] for k in EXPECTED} == EXPECTED, (
            "the simulator disagreed with the fixture table. What each row is owed:\n  "
            + "\n  ".join(f"{r.media_key} -> {r.bucket}: {r.why}" for r in ROWS)
        )

    def test_the_unreadable_rows_are_kept_and_the_readable_one_is_not(
        self, client: TestClient
    ) -> None:
        """The discriminating assertion, and the reason the control row exists.

        At a threshold of 1 with no coverage floor, every row here scores over the line.
        Only the two rows whose explanations are readable and clear may be previewed as
        deletions; the three unreadable ones and the genuinely blocked one must not be. A
        fallback of "nothing was blocking" would report six.
        """
        result = _simulate(client, condemn_at=1)
        assert result["condemned"] == 2
        assert result["reclaimable_bytes"] == 2 * SIZE

    def test_a_stored_condemn_it_cannot_read_is_reported_as_no_longer_condemned(
        self, client: TestClient
    ) -> None:
        """Giving up a reap is a change the owner is owed a number for.

        The row was condemned when they last reviewed it; the preview no longer condemns
        it, so it belongs in the delta, not silently missing from both counts.
        """
        assert _simulate(client)["no_longer_condemned"] == 1

    def test_an_unreadable_protection_still_counts_as_protected(self, client: TestClient) -> None:
        """The other direction. The row keeps its place in the protected count; all that is
        lost is the name of the protection that saved it, which is the cautious thing to
        lose. Dropping the row instead would under-report what the draft policy keeps."""
        result = _simulate(client)
        assert result["protected"] == 2
        assert result["protected_by"] == [{"gate": "rating_floor", "count": 1}]


class TestTheQueueSurvivesAMixedSignalBlock:
    def test_the_condemned_lane_renders_and_names_the_readable_signal(
        self, client: TestClient
    ) -> None:
        """Sorting a number against a string used to raise a TypeError, which blanked every
        condemned card, not just this one. The reason line now falls back to the entries it
        can read."""
        response = client.get("/api/candidates", params={"verdict": "condemn"})
        assert response.status_code == 200
        rows = {str(r["media_key"]): r for r in response.json()}
        assert rows["radarr:1:9"]["reason"] == "poorly rated where it is rated"


class TestTheHelpersThemselves:
    """The fallbacks, asserted directly. Each table is the spec: the shape, the value it
    must produce, and which direction that value resolves toward."""

    @pytest.mark.parametrize(
        ("stored", "expected"),
        [
            ("[1, 2, 3]", True),
            ("null", True),
            ("7", True),
            ('"a string"', True),
            ("not json at all", True),
            (json.dumps({"protections_unknown": [BLOCKED_ENTRY]}), True),
            (json.dumps({"protections_unknown": ["a bare string"]}), True),
            (json.dumps({"protections_unknown": "nope"}), True),
            (json.dumps({"protections_unknown": []}), False),
            (json.dumps({"protections_unknown": None}), False),
            (json.dumps({"score": 95.0}), False),
        ],
    )
    def test_unreadable_holds_and_genuinely_absent_does_not(
        self, stored: str, expected: bool
    ) -> None:
        """True keeps the file out of the previewed deletion count, so True is the
        cautious answer everywhere the record cannot be read -- including a block that is
        there but malformed, which is why the raw list is consulted and not ``_entries``
        (which drops the malformed entries and would read it as empty)."""
        assert _has_blocked_protections(stored) is expected

    @pytest.mark.parametrize(
        ("stored", "expected"),
        [
            ("[1, 2, 3]", []),
            ("null", []),
            ("7", []),
            ("not json at all", []),
            (json.dumps({"protections_fired": ["a bare string"]}), []),
            (json.dumps({"protections_fired": [{"detail": "no gate key"}]}), []),
            (json.dumps({"protections_fired": "nope"}), []),
            (
                json.dumps({"protections_fired": [{"gate": "rating_floor", "detail": "well"}]}),
                ["rating_floor"],
            ),
        ],
    )
    def test_an_unreadable_explanation_names_no_protection(
        self, stored: str, expected: list[str]
    ) -> None:
        assert _fired_gates(stored) == expected

    @pytest.mark.parametrize(
        ("stored", "expected"),
        [
            ({"contribution": 12.5}, 12.5),
            ({"contribution": 3}, 3.0),
            ({"contribution": "3.0"}, 0.0),
            ({"contribution": None}, 0.0),
            ({"contribution": [1]}, 0.0),
            ({"contribution": {"value": 1}}, 0.0),
            ({}, 0.0),
        ],
    )
    def test_a_contribution_that_is_not_a_number_sorts_last(
        self, stored: dict[str, Any], expected: float
    ) -> None:
        assert _contribution(stored) == expected

    def test_the_sort_key_orders_a_mixed_block_without_raising(self) -> None:
        """The coercion is there so the three types in one block are comparable."""
        ordered = sorted(MIXED_SIGNALS, key=_contribution, reverse=True)
        assert [s["id"] for s in ordered] == ["low_rating", "unwatched", "few_watchers"]


def test_the_draft_policy_matches_the_fixture(client: TestClient) -> None:
    """Without this, every simulation above could be silently answering the refusal path.

    A gates or signals list that drifts from ``_fixture_scoring_hash`` makes the route
    return ``exact: false`` and all-zero counts, which would pass a "nothing was condemned"
    assertion for entirely the wrong reason.
    """
    result = _simulate(client)
    assert result["stale_reason"] is None
    assert sum(int(n) for n in result["histogram"]) == len(ROWS)


def test_the_fixture_gates_and_signals_are_the_wire_shapes() -> None:
    """The two lists are dicts on the wire, so a typo in one is a 422, not a bad hash."""
    assert [GateSettingIn.model_validate(g).gate for g in GATES] == [
        "min_dormancy",
        "rating_floor",
    ]
    assert [SignalSettingIn.model_validate(s).weight for s in SIGNALS] == [100]


def test_a_retired_gate_is_refused_at_the_save_boundary() -> None:
    """A draft naming ``whitelisted`` is operator input asking for a protection that no
    longer exists as a gate, so the boundary says so. A STORED body naming it converts on
    load instead (``policy_migrations.convert_list_protections``); refusing the stored copy
    would take the install offline, and dropping the draft silently would hide the typo."""
    with pytest.raises(ValueError, match="whitelisted"):
        GateSettingIn.model_validate({"gate": "whitelisted"})


# ---------------------------------------------------------------------------
# The other tier: the frozen-Facts replay
# ---------------------------------------------------------------------------


#: The replay draft's gates: the shared list plus ``streaming_now``, which the other tier
#: does not need and this one cannot do without. The replay decides a hand reap off the
#: explanation it BUILDS from the frozen facts, so a gate the draft does not enable is a
#: protection the replay cannot see -- and ``streaming_now`` is the only kind of stop a hand
#: reap still cannot pass, which makes it the only way this tier can exercise a refused reap
#: at all. Kept off the shared ``GATES`` deliberately: that list is the stored-scores
#: fixture's hash input, and widening it there would move numbers in tests about something
#: else entirely.
REPLAY_GATES: list[dict[str, Any]] = [*GATES, {"gate": "streaming_now"}]

#: The draft the replay fixture is built for: the wire payload, the domain body it becomes,
#: and the evidence hash that body would gather under. Built through the route's own
#: ``_to_body`` so the fixture cannot drift from what the request produces. The keep list
#: protects through the operator's own rule now -- the ``whitelisted`` FIELD, since the gate
#: of that name is retired -- so the draft carries the rule the fixture's keep-list row
#: needs to stay held.
REPLAY_PAYLOAD = PolicyIn(
    condemn_at=70,
    coverage_floor_bp=0,
    gates=[GateSettingIn.model_validate(g) for g in REPLAY_GATES],
    signals=[SignalSettingIn.model_validate(s) for s in SIGNALS],
    protect_conditions=[ConditionIn(field="whitelisted", op="eq", value=True)],
)
REPLAY_BODY = _to_body(REPLAY_PAYLOAD)

#: The shipped movie policy as an UPGRADED install stores it: identical in every way that
#: decides anything, except that it was written before the current ``SCHEMA_VERSION``. The
#: wire schema does not carry that field, so this is the body whose round trip used to come
#: back with a different hash and kill the simulator outright.
STORED_OLD_BODY = DEFAULT_MOVIE_POLICY.model_copy(update={"schema_version": SCHEMA_VERSION - 1})


def _facts(**overrides: Any) -> Facts:
    """One item's evidence, readable end to end unless a test says otherwise."""
    base: dict[str, Any] = {
        "title": "item",
        "days_observed_unwatched": Known(value=2000.0, source="test"),
        "distinct_watchers": Known(value=0, source="test"),
        "distinct_watchers_all_time": Known(value=0, source="test"),
        "size_bytes": Known(value=SIZE, source="test"),
        "imdb_rating_tenths": Known(value=60, source="test"),
        "imdb_votes": Known(value=5_000, source="test"),
        "season_rank": Absent(source="test"),
        "is_streaming_now": Known(value=False, source="test"),
        "is_managed": Known(value=True, source="test"),
        "in_curated_list": Absent(source="test"),
        "is_whitelisted": Known(value=False, source="test"),
    }
    return Facts(**{**base, **overrides})


@pytest.fixture
def replay_client(tmp_path: Path) -> Iterator[TestClient]:
    """A snapshot the simulator must REPLAY: same evidence, different scoring.

    The stored scoring hash is deliberately not the draft's, so the route cannot answer from
    stored scores; the stored evidence hash is exactly the draft's, so the frozen Facts are
    still what a scan would gather and the replay is allowed.
    """
    settings = Settings(data_dir=tmp_path, secret_key="k")
    engine = sa_create_engine(settings.sync_database_url)
    Base.metadata.create_all(engine)
    # What a scan records about the lists it gathered membership under: without it the
    # simulator refuses, which is right for a snapshot that cannot say and useless here.
    list_hash = seeded_fingerprint(settings)

    now = utcnow()
    rows = {
        # Dormant, unprotected, fully observed: the draft condemns it.
        "radarr:1:1": _facts(),
        # On the keep list: protected, whatever it scores.
        "radarr:1:2": _facts(is_whitelisted=Known(value=True, source="test")),
    }
    with Session(engine) as session:
        snapshot = Snapshot(
            created_at=now,
            policy_hash=combine_hashes(
                DEFAULT_MOVIE_POLICY.policy_hash(), DEFAULT_TV_POLICY.policy_hash()
            ),
            scoring_hash="b" * 64,
            evidence_hash=combine_hashes(
                REPLAY_BODY.evidence_hash(), DEFAULT_TV_POLICY.evidence_hash()
            ),
            list_config_hash=list_hash,
            horizon_at=now,
            item_count=len(rows) + 2,
            degraded=False,
        )
        session.add(snapshot)
        session.flush()
        for index, (media_key, facts) in enumerate(rows.items(), start=1):
            session.add(
                Candidate(
                    snapshot_id=snapshot.id,
                    media_key=media_key,
                    title=f"Example Movie {index}",
                    media_type="movie",
                    size_bytes=SIZE,
                    verdict="abstain",
                    score=50,
                    coverage_bp=10_000,
                    explanation_json=_healthy(),
                    facts_json=json.dumps(facts_codec.facts_to_dict(facts)),
                    created_at=now,
                )
            )
        # Nothing about this one could be observed, so every protection is blocked -- and the
        # owner has hand-reaped it anyway. An empty body is what a row scored before a field
        # shipped thaws to: every fact Unknown. The engine honors that reap: a check that
        # could not run is a question the owner may answer.
        session.add(
            Candidate(
                snapshot_id=snapshot.id,
                media_key="radarr:1:3",
                title="Example Movie 3",
                media_type="movie",
                size_bytes=SIZE,
                verdict="abstain",
                score=95,
                coverage_bp=10_000,
                explanation_json=_healthy(protections_unknown=[BLOCKED_ENTRY]),
                facts_json="{}",
                created_at=now,
            )
        )
        # ...and one whose reap the engine still refuses, so the class can fail in both
        # directions. Somebody is playing it: a structural stop, not a judgment about whether
        # it is wanted, and the one thing a hand reap has never been able to pass.
        session.add(
            Candidate(
                snapshot_id=snapshot.id,
                media_key="radarr:1:4",
                title="Example Movie 4",
                media_type="movie",
                size_bytes=SIZE,
                verdict="protect",
                score=95,
                coverage_bp=10_000,
                explanation_json=_healthy(
                    protections_fired=[
                        {"gate": "streaming_now", "detail": "someone is watching it right now"}
                    ]
                ),
                facts_json=json.dumps(
                    facts_codec.facts_to_dict(
                        _facts(is_streaming_now=Known(value=True, source="t"))
                    )
                ),
                created_at=now,
            )
        )
        for key, title in (("radarr:1:3", "Example Movie 3"), ("radarr:1:4", "Example Movie 4")):
            session.add(
                WhitelistEntry(
                    media_key=key,
                    title=title,
                    decision="reap",
                    created_at=now,
                )
            )
        session.commit()
    engine.dispose()

    with TestClient(create_app(settings)) as c:
        login(c, settings)
        yield c


class TestTheFrozenFactsReplay:
    """The tier that re-runs the real engine over each row's frozen evidence.

    It judges through the scan's own ``judge_facts`` and applies the hand override through
    the scan's own ``effective_fate``, so what it previews is what a scan would do.
    """

    def _replay(self, client: TestClient) -> dict[str, Any]:
        response = client.post("/api/policy/simulate", json=REPLAY_PAYLOAD.model_dump())
        assert response.status_code == 200, response.text
        body: dict[str, Any] = response.json()
        # Without this the class would pass on the refusal path's all-zero counts.
        assert body["exact"] is True, body.get("stale_reason")
        return body

    def test_it_replays_rather_than_refusing(self, replay_client: TestClient) -> None:
        assert sum(int(n) for n in self._replay(replay_client)["histogram"]) == 4

    def test_the_dormant_row_is_condemned_and_the_kept_ones_are_named(
        self, replay_client: TestClient
    ) -> None:
        """Two removals: the dormant row on score, and the hand reap the engine now honors.
        Two keeps, each named by the gate that earned it -- the tally comes off the live
        replay's protectors, so a keep that stops firing under the draft stops being counted.
        """
        result = self._replay(replay_client)
        assert result["condemned"] == 2
        assert result["reclaimable_bytes"] == 2 * SIZE
        assert result["protected_by"] == [
            {"gate": "custom", "count": 1},
            {"gate": "streaming_now", "count": 1},
        ]

    def test_a_hand_reap_the_engine_cannot_honor_is_not_previewed_as_a_deletion(
        self, replay_client: TestClient
    ) -> None:
        """The reap-held case, and the one the refactor turns on.

        Counting a refused reap as a deletion would promise the owner a removal the planner
        holds back, at the exact moment they are choosing a threshold. The row that carries
        it is now the one being played right now: a structural stop, the only kind a hand
        reap has never passed. It used to be a row whose every protection was merely
        *blocked*, which stopped refusing anything -- so left as it was, this test would have
        gone on passing while covering nothing, which is the failure rule 118 is about.

        The blocked row is still in the fixture and is asserted here too, from the other
        side: the simulator must not hold back a reap the planner will honor either. Both
        errors mislead an operator choosing a threshold, and only one of them is cautious.
        """
        result = self._replay(replay_client)
        assert result["condemned"] == 2  # the dormant row and the honored hand reap
        assert result["protected"] == 2  # the keep-list row AND the refused hand reap
        assert result["abstained"] == 0

    def test_it_counts_the_lane_moves_neither_delta_can_see(
        self, replay_client: TestClient
    ) -> None:
        """#488: ``changed_titles`` is a superset of the two deltas, and the gap is the point.

        Read off the fixture rather than off the branches: two rows land in a different lane
        than the one they are stored in. ``radarr:1:1`` goes abstain -> condemn, which crosses
        the threshold and so appears in ``newly_condemned``. ``radarr:1:2`` goes abstain ->
        protect on the keep-list fact, and **no delta on the panel counts that move** -- the
        rows beside them report ``protected`` and ``abstained`` as absolute totals with no
        before to read them against. The other two rows carry hand overrides, which pin a lane
        at any threshold, so they cannot move.

        So the two deltas describe this draft as "+1 / -0" while it moves two titles, and the
        superset is what separates that screen from one where nothing happened at all.
        """
        result = self._replay(replay_client)
        assert result["changed_titles"] == 2
        assert result["newly_condemned"] == 1
        assert result["no_longer_condemned"] == 0


@pytest.fixture
def keep_rule_client(tmp_path: Path) -> Iterator[TestClient]:
    """A snapshot of stored condemns, two of which the draft's protections now keep.

    The scenario an operator reported: they add an outright keep, most of the removal list
    leaves it, and both deltas read zero. Every row here is stored ``condemn`` with no hand
    override, so ``was`` is condemn for all three and the only thing that moves them is the
    draft's own protections firing on the frozen facts.
    """
    settings = Settings(data_dir=tmp_path, secret_key="k")
    engine = sa_create_engine(settings.sync_database_url)
    Base.metadata.create_all(engine)
    # What a scan records about the lists it gathered membership under: without it the
    # simulator refuses, which is right for a snapshot that cannot say and useless here.
    list_hash = seeded_fingerprint(settings)

    now = utcnow()
    # Two ways out of the removal list and one row that stays on it, so the count cannot come
    # out right by counting a single protection, or by counting every row.
    rows = {
        "radarr:1:1": _facts(is_whitelisted=Known(value=True, source="test")),
        "radarr:1:2": _facts(is_streaming_now=Known(value=True, source="test")),
        "radarr:1:3": _facts(),
    }
    with Session(engine) as session:
        snapshot = Snapshot(
            created_at=now,
            policy_hash=combine_hashes(
                DEFAULT_MOVIE_POLICY.policy_hash(), DEFAULT_TV_POLICY.policy_hash()
            ),
            scoring_hash="b" * 64,
            evidence_hash=combine_hashes(
                REPLAY_BODY.evidence_hash(), DEFAULT_TV_POLICY.evidence_hash()
            ),
            list_config_hash=list_hash,
            horizon_at=now,
            item_count=len(rows),
            degraded=False,
        )
        session.add(snapshot)
        session.flush()
        for index, (media_key, facts) in enumerate(rows.items(), start=1):
            session.add(
                Candidate(
                    snapshot_id=snapshot.id,
                    media_key=media_key,
                    title=f"Example Movie {index}",
                    media_type="movie",
                    size_bytes=SIZE,
                    verdict="condemn",
                    score=95,
                    coverage_bp=10_000,
                    explanation_json=_healthy(),
                    facts_json=json.dumps(facts_codec.facts_to_dict(facts)),
                    created_at=now,
                )
            )
        session.commit()
    engine.dispose()

    with TestClient(create_app(settings)) as c:
        login(c, settings)
        yield c


class TestATitleAProtectionRescuesLeavesTheRemovalList:
    """A title going condemn -> protect is one the operator no longer has to review.

    ``no_longer_condemned`` was counted in the replay's abstain arm alone, so it meant
    condemn -> **abstain** and nothing else. A protection edit is the one that takes a title
    straight from condemn to protect, which is also the edit the delta exists to describe, so
    the panel answered an outright keep rule with -0 and a compare line that put the draft's
    own count on both sides of a sentence built to contrast it.

    Expectations are read off the fixture: ``radarr:1:1`` is kept by the keep list and
    ``radarr:1:2`` by the streaming stop, both from stored condemn; ``radarr:1:3`` has nothing
    to keep it and stays condemned.
    """

    def _replay(self, client: TestClient) -> dict[str, Any]:
        response = client.post("/api/policy/simulate", json=REPLAY_PAYLOAD.model_dump())
        assert response.status_code == 200, response.text
        body: dict[str, Any] = response.json()
        assert body["exact"] is True, body.get("stale_reason")
        return body

    def test_the_rescued_titles_land_in_the_protected_lane(
        self, keep_rule_client: TestClient
    ) -> None:
        """The premise. Without this the delta assertion could pass on rows that never moved."""
        result = self._replay(keep_rule_client)
        assert result["condemned"] == 1
        assert result["protected"] == 2
        assert result["abstained"] == 0

    def test_both_are_counted_as_no_longer_condemned(self, keep_rule_client: TestClient) -> None:
        result = self._replay(keep_rule_client)
        assert result["no_longer_condemned"] == 2
        assert result["newly_condemned"] == 0
        assert result["changed_titles"] == 2

    def test_the_saved_policy_count_is_sent_rather_than_derived(
        self, keep_rule_client: TestClient
    ) -> None:
        """Rule 144: the panel is told what the saved policy flags, never left to rebuild it.

        The identity is asserted alongside the value because the browser used to depend on it
        and could not check it: a delta that misses a direction breaks the derivation while
        every number it is built from still reads as a plausible count.
        """
        result = self._replay(keep_rule_client)
        assert result["condemned_before"] == 3
        assert result["condemned_before"] == (
            result["condemned"] - result["newly_condemned"] + result["no_longer_condemned"]
        )


class TestSwitchingAProtectionIsPreviewedRatherThanRefused:
    """The edit an operator makes most often on this page, answered off the last scan.

    Toggling a protection used to fall to the refusal: the whole ``gates`` list sat in the
    evidence hash, so unticking a box blanked every number and asked for a scan, while the
    rating *bar* inside that same box previewed instantly. The frozen Facts were always good
    enough for both -- a gate reads them, it does not decide what gets gathered.
    """

    @staticmethod
    def _draft_with_dormancy_off() -> dict[str, Any]:
        gates = [dict(g) for g in REPLAY_GATES]
        for gate in gates:
            if gate["gate"] == "min_dormancy":
                gate["enabled"] = False
        payload = REPLAY_PAYLOAD.model_copy(
            update={"gates": [GateSettingIn.model_validate(g) for g in gates]}
        )
        return payload.model_dump()

    def test_the_panel_answers_instead_of_asking_for_a_scan(
        self, replay_client: TestClient
    ) -> None:
        response = replay_client.post("/api/policy/simulate", json=self._draft_with_dormancy_off())

        assert response.status_code == 200, response.text
        body = response.json()
        assert body["exact"] is True, body.get("stale_reason")
        # An answer, not the refusal's all-zero shape.
        assert sum(int(n) for n in body["histogram"]) == 4

    def test_the_draft_still_needs_a_scan_when_it_moves_the_popularity_window(
        self, replay_client: TestClient
    ) -> None:
        """The exclusion that survives, from the route rather than from the hash alone.

        The watcher counts on those rows were counted over one span; a draft asking about a
        different one gets the honest blank, because no arithmetic over the frozen numbers
        can answer it.
        """
        gates = [
            *(dict(g) for g in REPLAY_GATES),
            {"gate": "server_popularity", "threshold": 3, "window_days": 30},
        ]
        payload = REPLAY_PAYLOAD.model_copy(
            update={"gates": [GateSettingIn.model_validate(g) for g in gates]}
        )

        response = replay_client.post("/api/policy/simulate", json=payload.model_dump())

        assert response.status_code == 200, response.text
        body = response.json()
        assert body["exact"] is False
        assert body["condemned"] == 0
        assert "scan" in body["stale_reason"].lower()


class TestTheWireRoundTripPreservesBothHashes:
    """A body that survives ``PolicyBody -> PolicyIn -> _to_body`` keeps both simulator hashes.

    This is the test whose absence let the simulator die silently. ``_policy_out`` builds the
    wire body field by field, so a ``PolicyBody`` field it forgets is not merely missing from
    the response: the route reconstructs it from the code default, and if that field feeds
    either simulator hash the reconstructed hash can never match the one the scan recorded
    from the stored body. The simulator then answers "Needs a fresh scan" forever, and
    scanning does not help, because each scan records the stored value again.

    Asserting on the hashes rather than on a field list is deliberate: it fails for ANY future
    field dropped from the wire, without anyone remembering to extend a list (rule 103).
    """

    @staticmethod
    def _round_trip(body: PolicyBody) -> PolicyBody:
        """Through the real response builder and the real request parser, not a copy."""
        out = _policy_out(body, "Movies", requests_app_configured=True, settings=ProfileSettings())
        return _to_body(out.body)

    def test_a_body_stored_under_an_older_schema_version_still_simulates(self) -> None:
        stored = DEFAULT_MOVIE_POLICY.model_copy(update={"schema_version": SCHEMA_VERSION - 1})
        assert self._round_trip(stored).scoring_hash() == stored.scoring_hash()
        assert self._round_trip(stored).evidence_hash() == stored.evidence_hash()

    @pytest.mark.parametrize("policy", [DEFAULT_MOVIE_POLICY, DEFAULT_TV_POLICY])
    def test_the_shipped_defaults_round_trip_to_the_same_hashes(self, policy: PolicyBody) -> None:
        assert self._round_trip(policy).scoring_hash() == policy.scoring_hash()
        assert self._round_trip(policy).evidence_hash() == policy.evidence_hash()


@pytest.fixture
def upgraded_install_client(tmp_path: Path) -> Iterator[TestClient]:
    """An install whose stored policy predates a ``SCHEMA_VERSION`` bump.

    The hashes are recorded exactly as a scan records them -- from the STORED body -- while
    the request will carry that same policy back through the wire schema. Nothing here is
    contrived except the version number: this is what every upgraded install looks like.
    """
    settings = Settings(data_dir=tmp_path, secret_key="k")
    engine = sa_create_engine(settings.sync_database_url)
    Base.metadata.create_all(engine)
    # What a scan records about the lists it gathered membership under: without it the
    # simulator refuses, which is right for a snapshot that cannot say and useless here.
    list_hash = seeded_fingerprint(settings)

    now = utcnow()
    with Session(engine) as session:
        snapshot = Snapshot(
            created_at=now,
            policy_hash=combine_hashes(
                STORED_OLD_BODY.policy_hash(), DEFAULT_TV_POLICY.policy_hash()
            ),
            scoring_hash=combine_hashes(
                STORED_OLD_BODY.scoring_hash(), DEFAULT_TV_POLICY.scoring_hash()
            ),
            evidence_hash=combine_hashes(
                STORED_OLD_BODY.evidence_hash(), DEFAULT_TV_POLICY.evidence_hash()
            ),
            list_config_hash=list_hash,
            horizon_at=now,
            item_count=1,
            degraded=False,
        )
        session.add(snapshot)
        session.flush()
        session.add(
            Candidate(
                snapshot_id=snapshot.id,
                media_key="radarr:1:1",
                title="Example Movie 1",
                media_type="movie",
                size_bytes=SIZE,
                verdict="condemn",
                score=90,
                coverage_bp=10_000,
                explanation_json=_healthy(),
                facts_json=json.dumps(facts_codec.facts_to_dict(_facts())),
                created_at=now,
            )
        )
        session.commit()
    engine.dispose()

    with TestClient(create_app(settings)) as c:
        login(c, settings)
        yield c


class TestAnUpgradedInstallStillGetsNumbers:
    """The operator-visible symptom, pinned at the route.

    The three hash tests above pin the mechanism; this one pins what an operator actually
    saw. On an install whose stored policy predated a ``SCHEMA_VERSION`` bump, the simulator
    answered "Needs a fresh scan" for **every** edit and no scan could clear it, because each
    scan recorded the stored version again while the route hashed the round-tripped one. The
    panel that exists to make a threshold a decision rather than a guess was simply dead.

    Asserting through ``POST /api/policy/simulate`` rather than on the hashes is the point: a
    future regression in either hash, in the wire schema, or in the tier order fails here.
    """

    @staticmethod
    def _simulate(client: TestClient, body: PolicyIn) -> dict[str, Any]:
        r = client.post(
            "/api/policy/simulate",
            json=body.model_dump(mode="json"),
            headers={"X-Reaper-CSRF": "1"},
        )
        assert r.status_code == 200, r.text
        result: dict[str, Any] = r.json()
        return result

    def test_the_stored_policy_round_trips_to_an_exact_answer(
        self, upgraded_install_client: TestClient
    ) -> None:
        """The reproduction: hand the server back the policy it just gave you."""
        wire = _policy_out(
            STORED_OLD_BODY, "Movies", requests_app_configured=True, settings=ProfileSettings()
        ).body
        result = self._simulate(upgraded_install_client, wire)

        assert result["exact"] is True
        assert result["stale_reason"] is None
        # ...and it reports real numbers, not an all-zero "exact" answer: the stored score of
        # 90 is past the shipped threshold of 70.
        assert result["condemned"] == 1
        assert result["reclaimable_bytes"] == SIZE

    def test_moving_only_the_threshold_stays_exact_and_moves_the_count(
        self, upgraded_install_client: TestClient
    ) -> None:
        """Tier 1 on an upgraded install: the tuning loop the whole panel exists for."""
        wire = _policy_out(
            STORED_OLD_BODY, "Movies", requests_app_configured=True, settings=ProfileSettings()
        ).body
        above = self._simulate(upgraded_install_client, wire.model_copy(update={"condemn_at": 95}))
        assert above["exact"] is True
        assert above["condemned"] == 0  # the stored 90 no longer clears the line

        below = self._simulate(upgraded_install_client, wire.model_copy(update={"condemn_at": 60}))
        assert below["exact"] is True
        assert below["condemned"] == 1


# ---------------------------------------------------------------------------
# The replay's watch-history reach
# ---------------------------------------------------------------------------


#: The replay draft with the keep-what-people-watch protection switched on. The fixture
#: above leaves that gate out entirely, so nothing there reaches the reach check.
REACH_PAYLOAD = PolicyIn(
    condemn_at=70,
    coverage_floor_bp=0,
    gates=[
        GateSettingIn.model_validate(g)
        for g in [*GATES, {"gate": "server_popularity", "threshold": 3, "window_days": 365}]
    ],
    signals=[SignalSettingIn.model_validate(s) for s in SIGNALS],
)
REACH_BODY = _to_body(REACH_PAYLOAD)


def _reach_client(
    tmp_path: Path, *, horizon_days: int, frozen_reach: float | None = None
) -> Iterator[TestClient]:
    """One dormant, unwatched, unprotected row a replay would condemn on score alone.

    ``horizon_days`` is how deep the SNAPSHOT says the watch history went.
    ``frozen_reach`` is what the ROW itself recorded: ``None`` drops the key entirely,
    which is exactly what a row frozen before ``Facts.history_reach_days`` existed looks
    like on disk.
    """
    settings = Settings(data_dir=tmp_path, secret_key="k")
    engine = sa_create_engine(settings.sync_database_url)
    Base.metadata.create_all(engine)
    # What a scan records about the lists it gathered membership under: without it the
    # simulator refuses, which is right for a snapshot that cannot say and useless here.
    list_hash = seeded_fingerprint(settings)

    now = utcnow()
    # Encoded by production's own codec rather than transcribed (rule 119); only the
    # deletion is done by hand, because no Facts can express "this key was never written".
    frozen = facts_codec.facts_to_dict(
        _facts()
        if frozen_reach is None
        else _facts(history_reach_days=Known(value=frozen_reach, source="test"))
    )
    if frozen_reach is None:
        del frozen["obs"]["history_reach_days"]
    with Session(engine) as session:
        snapshot = Snapshot(
            created_at=now,
            policy_hash=combine_hashes(
                DEFAULT_MOVIE_POLICY.policy_hash(), DEFAULT_TV_POLICY.policy_hash()
            ),
            scoring_hash="b" * 64,
            evidence_hash=combine_hashes(
                REACH_BODY.evidence_hash(), DEFAULT_TV_POLICY.evidence_hash()
            ),
            list_config_hash=list_hash,
            horizon_at=now - timedelta(days=horizon_days),
            item_count=1,
            degraded=False,
        )
        session.add(snapshot)
        session.flush()
        session.add(
            Candidate(
                snapshot_id=snapshot.id,
                media_key="radarr:1:1",
                title="Example Movie 1",
                media_type="movie",
                size_bytes=SIZE,
                verdict="abstain",
                score=50,
                coverage_bp=10_000,
                explanation_json=_healthy(),
                facts_json=json.dumps(frozen),
                created_at=now,
            )
        )
        session.commit()
    engine.dispose()

    with TestClient(create_app(settings)) as c:
        login(c, settings)
        yield c


class TestTheReplayKnowsHowFarTheStoredHistoryReached:
    """A row frozen before the reach was a fact still replays against a real reach.

    ``facts_from_dict`` thaws a field the snapshot predates as ``Unknown`` (rule 104), and
    ``ServerPopularityGate`` blocks on that -- correct for a fact nobody recorded, but it
    would report every pre-upgrade row as held and break this tier's promise to be
    bit-identical to a fresh scan. The snapshot row carries ``horizon_at``, which is the
    same instant a re-scan would measure from, so the gap is filled from stored evidence.
    """

    def _condemned(self, client: TestClient) -> int:
        response = client.post("/api/policy/simulate", json=REACH_PAYLOAD.model_dump())
        assert response.status_code == 200, response.text
        body: dict[str, Any] = response.json()
        assert body["exact"] is True, body.get("stale_reason")
        return int(body["condemned"])

    def test_a_deep_stored_horizon_lets_an_older_row_be_judged(self, tmp_path: Path) -> None:
        """Two years of history behind a one-year window: the count is a real answer, so
        the row is condemned exactly as it was before the reach existed."""
        for client in _reach_client(tmp_path, horizon_days=800):
            assert self._condemned(client) == 1

    def test_a_shallow_stored_horizon_holds_the_row(self, tmp_path: Path) -> None:
        """Three months of history behind a one-year window. The zero watcher count is a
        lower bound, so the protection cannot be reported as checked and the row is held --
        exactly what the fix is for, arriving through the simulator too."""
        for client in _reach_client(tmp_path, horizon_days=90):
            assert self._condemned(client) == 0

    def test_a_row_that_froze_its_own_reach_keeps_it(self, tmp_path: Path) -> None:
        """The fill never overwrites. This row recorded a 90-day reach; the snapshot's
        horizon says 800. Replaying it on the snapshot's number would re-judge frozen
        evidence against something the scan did not see, so the row's own value wins."""
        for client in _reach_client(tmp_path, horizon_days=800, frozen_reach=90.0):
            assert self._condemned(client) == 0


# ---------------------------------------------------------------------------
# What spared a hand-spared row
# ---------------------------------------------------------------------------


#: Rows for the attribution fixture: a hand spare over a row that ALSO cleared a protection.
#: The middle row is the double-count case -- spared by hand and on the keep list, which is
#: one gate reported twice, once from the stored explanation and once from the hand spare.
SPARED_ROWS: tuple[tuple[str, str, str], ...] = (
    (
        "radarr:9:1",
        _healthy(
            protections_fired=[
                {"gate": "rating_floor", "detail": "well rated: 8.0 on IMDb from 250,000 votes"}
            ]
        ),
        "the rating floor would have kept it on its own, and the owner spared it as well",
    ),
    (
        "radarr:9:2",
        _healthy(
            protections_fired=[{"gate": "whitelisted", "detail": "on your keep list, never reaped"}]
        ),
        "on the keep list AND spared by hand: one gate, reported by two routes",
    ),
)


@pytest.fixture
def spared_client(tmp_path: Path) -> Iterator[TestClient]:
    """A snapshot whose every row carries a hand spare, on the stored-score tier."""
    settings = Settings(data_dir=tmp_path, secret_key="k")
    engine = sa_create_engine(settings.sync_database_url)
    Base.metadata.create_all(engine)
    # What a scan records about the lists it gathered membership under: without it the
    # simulator refuses, which is right for a snapshot that cannot say and useless here.
    list_hash = seeded_fingerprint(settings)

    now = utcnow()
    with Session(engine) as session:
        snapshot = Snapshot(
            created_at=now,
            policy_hash=combine_hashes(
                DEFAULT_MOVIE_POLICY.policy_hash(), DEFAULT_TV_POLICY.policy_hash()
            ),
            scoring_hash=_fixture_scoring_hash(),
            list_config_hash=list_hash,
            horizon_at=now,
            item_count=len(SPARED_ROWS),
            degraded=False,
        )
        session.add(snapshot)
        session.flush()
        for index, (media_key, explanation, _why) in enumerate(SPARED_ROWS, start=1):
            session.add(
                Candidate(
                    snapshot_id=snapshot.id,
                    media_key=media_key,
                    title=f"Example Movie {index}",
                    media_type="movie",
                    size_bytes=SIZE,
                    verdict="protect",
                    score=95,
                    coverage_bp=10_000,
                    explanation_json=explanation,
                    created_at=now,
                )
            )
            session.add(
                WhitelistEntry(
                    media_key=media_key,
                    title=f"Example Movie {index}",
                    decision="spare",
                    created_at=now,
                )
            )
        session.commit()
    engine.dispose()

    with TestClient(create_app(settings)) as c:
        login(c, settings)
        yield c


class TestAHandSpareIsOneProtectionAmongTheRest:
    """The spared-by tally credits every protection that fired, not the hand spare alone.

    The stored-score tier used to answer a hand-spared row with ``["whitelisted"]`` and throw
    the row's other protections away, while the replay tier counts them all -- so making ANY
    scoring edit, which is what moves the route from the first tier to the second, jumped the
    "why titles were spared" list by whatever those discarded protections came to, beside a
    headline count and byte total that correctly did not move at all. An edit appears to
    change which protections fired, when a graded keep cannot change that: it is subtracted
    after normalization, and the verdict reads protection before it ever reads the score.

    The two tiers are separate fixtures because they need different stored hashes, so this
    pins the tier that was wrong against the answer the other one already gave.
    """

    def _tally(self, client: TestClient) -> dict[str, int]:
        response = client.post(
            "/api/policy/simulate",
            json={
                "condemn_at": 70,
                "coverage_floor_bp": 0,
                "gates": GATES,
                "signals": SIGNALS,
            },
        )
        assert response.status_code == 200, response.text
        body: dict[str, Any] = response.json()
        assert body["exact"] is True, body.get("stale_reason")
        return {g["gate"]: g["count"] for g in body["protected_by"]}

    def test_the_other_protection_on_a_spared_row_is_still_named(
        self, spared_client: TestClient
    ) -> None:
        """The bug, at its plainest. Both rows are spared by hand, so the hand-spare id is
        owed 2; the first row also cleared the rating floor, which used to vanish from the
        tally entirely because the hand spare was counted in its place.

        The hand spare tallies under its OWN id, not under ``whitelisted``. It rode in on the
        retired gate's id, and the simulator names a tally row by that id's copy -- so on a
        fresh install, where no gate emits ``whitelisted`` at all, every hand spare was
        reported to the operator as membership of a list (rule 144).

        The second row's stored ``whitelisted`` explanation is now its own entry beside the
        hand spare, where the two used to collapse into one. That is the split doing its job:
        the list is still holding that title, and the operator also spared it by hand.
        """
        assert self._tally(spared_client) == {
            "hand_spare": 2,
            "rating_floor": 1,
            "whitelisted": 1,
        }

    def test_a_row_spared_by_hand_and_on_the_keep_list_counts_once(
        self, spared_client: TestClient
    ) -> None:
        """A gate spares a row once. The second row reports its stored ``whitelisted``
        explanation and is also spared by hand, and a tally that added both would claim more
        spared titles than the fixture has rows -- so the fix that restores the other
        protections must not restore this one twice.

        The two are separate rows now, which is the point: one says what a list is holding,
        the other says what the operator did by hand.
        """
        tally = self._tally(spared_client)
        assert tally["hand_spare"] == len(SPARED_ROWS)


class TestAListChangedSinceTheScanIsRefusedRatherThanReplayed:
    """The protection lists are not policy, and every tier reads evidence derived from them.

    While the keep tags lived on the policy body they sat inside ``PolicyBody.evidence_hash``,
    so retagging them made the panel ask for a scan. Moving membership into gathered evidence
    (``Facts.on_lists``) moved them out from under that hash and put nothing back: an operator
    could retag, repoint or rename a list and the panel went on reporting the membership the
    scan froze, labeled exact (#512). ``Snapshot.list_config_hash`` is what put it back.

    **The operator action here is a tag edit that leaves the name alone**, and the choice is
    what makes these proofs rather than coincidences. Adding a list attaches a keep rule and
    renaming one rewrites the rules that spell it (``api.lists``), so both edit the stored
    policies -- and the route mixes the OTHER media type's stored policy into every hash it
    compares (``_combined``), so either action refuses through the policy hash whether this
    interlock exists or not. Verified by deleting the interlock: the add-a-list and rename
    versions of these tests stayed green, which is rule 118's undiscriminating test exactly.
    Changing only ``config`` touches no policy, so the fingerprint is the single variable.

    Both tiers are driven, because the refusal sits above the split and each goes stale by its
    own route -- the replay through the frozen fact, the threshold path through the stored
    verdict that fact produced. Gating tier 2 alone would leave the identical wrong number
    reachable by moving a threshold, which is the edit this page exists for (rule 72).
    """

    @staticmethod
    def _retag_a_list(client: TestClient) -> None:
        """Change what one list matches, and nothing else. No policy is touched."""
        listed = client.get("/api/lists/configured").json()
        tag_list = next((row for row in listed if row["source"] == "arr_tag"), None)
        assert tag_list is not None, "the fixture's registry has no tag list to retag"

        patched = client.patch(
            f"/api/lists/configured/{tag_list['id']}",
            json={"config": {"tags": ["a-different-tag"], "match": "any"}},
        )
        assert patched.status_code == 200, patched.text

    def test_the_threshold_path_refuses_once_the_lists_move(self, client: TestClient) -> None:
        before = _simulate(client, 40)
        assert before["exact"] is True, before.get("stale_reason")

        self._retag_a_list(client)

        after = _simulate(client, 40)
        assert after["exact"] is False, "previewed a threshold against pre-edit list membership"
        assert after["condemned"] == 0
        assert "scan" in after["stale_reason"].lower()

    def test_the_replay_refuses_once_the_lists_move(self, replay_client: TestClient) -> None:
        draft = REPLAY_PAYLOAD.model_dump()
        before = replay_client.post("/api/policy/simulate", json=draft).json()
        assert before["exact"] is True, before.get("stale_reason")

        self._retag_a_list(replay_client)

        after = replay_client.post("/api/policy/simulate", json=draft).json()
        assert after["exact"] is False, "replayed a scoring edit against pre-edit list membership"
        assert after["condemned"] == 0
        assert "scan" in after["stale_reason"].lower()

    def test_a_snapshot_that_never_recorded_its_lists_refuses(
        self, client: TestClient, tmp_path: Path
    ) -> None:
        """The pre-upgrade shape: ``None`` reads as unknown, never as "no lists" (rule 104).

        Driven by clearing the recorded value rather than by building a second fixture, so
        what is pinned is the NULL itself and not some other difference between two
        snapshots. ``tmp_path`` is the one the ``client`` fixture built this database in --
        pytest hands the same directory to a fixture and the test that asks for it.
        """
        assert _simulate(client, 40)["exact"] is True

        settings = Settings(data_dir=tmp_path, secret_key="k")
        engine = sa_create_engine(settings.sync_database_url)
        with Session(engine) as session:
            session.execute(sa_update(Snapshot).values(list_config_hash=None))
            session.commit()
        engine.dispose()

        after = _simulate(client, 40)
        assert after["exact"] is False, "answered off a snapshot that cannot say its lists"
        assert "scan" in after["stale_reason"].lower()

    def test_two_unknowns_are_not_a_match(self, client: TestClient, tmp_path: Path) -> None:
        """Both sides ``None`` is the pairing an equality test reads as agreement.

        ``None`` means "unknown" on each side and they are different unknowns: the snapshot's
        is a scan that degraded for a registry it could not read, and the live one is a
        registry that cannot be read right now. Comparing them answered "exact" off a scan
        that had itself given up on the lists, which is the one case where the frozen
        membership is least trustworthy (rules 93, 104).

        Both sides are driven to ``None`` at once, because either alone already refuses
        through the inequality and would leave this green with the fix removed (rule 118).
        """
        assert _simulate(client, 40)["exact"] is True

        settings = Settings(data_dir=tmp_path, secret_key="k")
        engine = sa_create_engine(settings.sync_database_url)
        with Session(engine) as session:
            session.execute(sa_update(Snapshot).values(list_config_hash=None))
            # A body that will not parse makes `definitions(strict=True)` raise, which is
            # what `current_fingerprint` answers `None` to. The row stays in the table, so
            # this is the live registry being unreadable rather than being empty.
            session.execute(
                sa_update(ListConfig)
                .where(ListConfig.source == "arr_tag")
                .values(config_json="{not json")
            )
            session.commit()
        engine.dispose()

        after = _simulate(client, 40)
        assert after["exact"] is False, "previewed as exact with neither side able to say"
        assert "scan" in after["stale_reason"].lower()
