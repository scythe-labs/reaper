# SPDX-License-Identifier: AGPL-3.0-or-later
"""The simulator, swept over the policy space against real library shapes.

``test_simulate_hardening.py`` pins the simulator's *shapes* on four hand-built rows;
``test_policy_permutations.py`` sweeps the policy space against the ENGINE. Neither drives
the route across that space, which is where a preview can be confidently wrong: the panel
has two paths to an answer, they are chosen by a hash, and they are read by an operator
about to delete files.

Three oracles, none of which re-implements a decision (rule 119):

* **The two tiers agree.** For one draft over one snapshot, the stored-score path and the
  frozen-Facts replay must return the same panel. They share no arithmetic -- one re-compares
  integers a scan wrote, the other re-runs the engine over frozen evidence -- so a
  disagreement is a bug in whichever one moved.
* **The replay is what the next scan will do.** For a draft the route says it can preview
  exactly, every count must equal the one obtained by scoring the same vectors under that
  draft with the scan's own ``judge_facts``. This is the panel's actual promise.
* **The panel adds up.** Every answer, from either tier, satisfies the arithmetic the UI
  reads off it -- the lanes partition the library, the histogram holds every row, and the
  saved-policy count the panel states is the one its two deltas imply.

And one that needs no snapshot at all: a policy field that does not survive the wire round
trip changes the hash the route compares, so the panel refuses every edit forever. That is
the shape of the upgraded-install bug in ``test_simulate_hardening.py``, reached through a
field rather than through a version.

**This is the movie lane, and only the movie lane** (rule 132). A season's guard is replayed
from a per-show bundle the scan freezes beside the Facts, which is a second input this
fixture does not build; the same oracle for that input already exists, one setting at a time,
in ``test_scan_pipeline.TestASeasonRuleReplaysExactlyOffTheFrozenBundle``. The panel
arithmetic below is shared by both lanes, so it is swept once here rather than twice.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine as sa_create_engine
from sqlalchemy import delete
from sqlalchemy.orm import Session

from reaper.api.policy import _policy_out, _to_body
from reaper.api.schemas import PolicyIn, SimulationOut
from reaper.clock import utcnow
from reaper.config import Settings
from reaper.db.base import Base
from reaper.db.models import Candidate, Snapshot, WhitelistEntry
from reaper.engine import facts_codec
from reaper.engine.gates import GateId, GateResult
from reaper.engine.observation import Known
from reaper.engine.policy import (
    DEFAULT_MOVIE_POLICY,
    DEFAULT_TV_POLICY,
    BooleanCondemnSpec,
    ConditionSpec,
    GradedCondemnSpec,
    GradedKeepSpec,
    PolicyBody,
    ProfileSettings,
    RatingRuleSpec,
    combine_hashes,
)
from reaper.engine.signals import SignalConfig
from reaper.main import create_app
from reaper.ratings import RatingSource
from reaper.services.scan_runner import build_gates
from reaper.services.snapshot import effective_fate, judge_facts

from . import _policy_lab as lab
from ._auth import login
from ._lists import seeded_fingerprint

#: How many of the lab's movie shapes each snapshot carries: all 220 of them. The cap is
#: stated rather than left implicit so a regenerated fixture that grows cannot quietly shrink
#: the sweep, and `TestTheSweepIsWide` pins that the rows actually reach every lane the panel
#: reports rather than assuming a wide fixture is a wide sweep (rule 145).
SAMPLE = 220


def _known(vector: dict[str, Any], field: str, value: Any) -> dict[str, Any]:
    return {**vector, "facts": {**vector["facts"], field: {"state": "known", "value": value}}}


def _rating_blind_condemned(vectors: list[dict[str, Any]], count: int = 3) -> frozenset[int]:
    """The rows whose rating is knocked out to make the coverage floor reachable at all.

    A fixed stride cannot get there. Every fact a signal reads is read by a gate too, so a
    row that loses coverage abstains on the unchecked gate long before the floor is
    consulted -- unless the row was CONDEMNED to start with and the gate reading that fact is
    off, which is the shape `coverage_floor_bites` builds. Choosing the rows off the
    baseline judgment rather than off an index is what makes the floor bite three rows
    instead of none.
    """
    from reaper.services.snapshot import judge_facts as _judge  # local: BASE is defined below

    gates = build_gates(DEFAULT_MOVIE_POLICY)
    found: list[int] = []
    for index, vector in enumerate(vectors, start=1):
        judged = _judge(
            lab.to_facts(vector),
            gates,
            DEFAULT_MOVIE_POLICY,
            signals=[
                SignalConfig(
                    signal=s.signal, weight=s.weight, saturate_at=s.saturate_at, floor=s.floor
                )
                for s in DEFAULT_MOVIE_POLICY.signals
            ],
            custom_condemn=DEFAULT_MOVIE_POLICY.custom_signal_configs(),
            keeps=DEFAULT_MOVIE_POLICY.keep_configs(),
            window_days=DEFAULT_MOVIE_POLICY.popularity_window_days(),
        )
        if judged.verdict == "condemn":
            found.append(index)
        if len(found) == count:
            break
    return frozenset(found)


def _enriched(index: int, vector: dict[str, Any]) -> dict[str, Any]:
    """One recorded shape, with a fact or two knocked out.

    The library behind the fixture is uniform in ways that matter here: every vector is
    fully observed, every size is known, and nothing was playing when it was taken. Swept as
    recorded, the panel's coverage floor never bites, ``unknown_size_items`` is 0 in all 220
    rows, and no row reaches the branch where a protection could not be CHECKED -- the one
    the stored-score path treats specially, and the one an operator is most exposed by.
    Every knockout below is a state ``Facts`` already models and the scan already produces
    (rule 93's Unknown), applied on a fixed stride so the sample is the same on every run.
    """
    if index % 5 == 0:
        # Withheld: the popularity gate cannot be checked, and its signal leaves the
        # numerator while staying in the denominator, so coverage falls below the floor.
        vector = lab.degraded(vector, ["distinct_watchers"])
    if index % 9 == 0:
        vector = lab.degraded(vector, ["imdb_rating_tenths", "imdb_votes"])
    if index % 6 == 0:
        # Size drives no signal, so this moves the panel's byte total and nothing else.
        vector = lab.degraded(vector, ["size_bytes"])
    if index % 23 == 0:
        vector = _known(vector, "is_streaming_now", True)
    return vector


def _movie_vectors() -> list[dict[str, Any]]:
    raw = [v for v in lab.load_fixture()["vectors"] if v.get("media_type", "movie") == "movie"][
        :SAMPLE
    ]
    blind = _rating_blind_condemned(raw)
    return [
        lab.degraded(_enriched(i, v), ["imdb_rating_tenths", "imdb_votes"])
        if i in blind
        else _enriched(i, v)
        for i, v in enumerate(raw, start=1)
    ]


VECTORS = _movie_vectors()


#: The hand decisions laid over the sample, by row index. The lab's vectors carry none, and a
#: sweep without them would leave the branch that outranks every threshold untested on both
#: paths -- the two implement it separately, so it is also the one place they can disagree
#: without either being obviously wrong. Deterministic, and the two strides are coprime with
#: each other so neither decision ever lands on the same row twice.
def override_for(index: int) -> str | None:
    # The streaming rows are reaped FIRST, and deliberately: a reap the engine refuses is
    # decided by different code on each path (`reap_is_effective` reads the stored
    # explanation, `effective_fate` re-derives it), so with only honored reaps in the sample
    # the two could disagree about a refusal and nothing here would notice.
    if index % 23 == 0:
        return "reap"
    if index % 7 == 0:
        return "spare"
    if index % 11 == 0:
        return "reap"
    return None


class Judged:
    """One vector as the scan would store it, plus the fate the scan would act on.

    The stored verdict is pure policy and the hand decision is NOT frozen into it: the scan
    stores what the policy decided and derives the fate live from the override map, which is
    what both simulator tiers then have to reproduce (``services.snapshot._judge_item``).
    """

    __slots__ = (
        "coverage_bp",
        "explanation",
        "facts_json",
        "fate",
        "override",
        "score",
        "size",
        "verdict",
    )

    def __init__(
        self,
        vector: dict[str, Any],
        policy: PolicyBody,
        gates: list[Any],
        *,
        override: str | None = None,
    ) -> None:
        facts = lab.to_facts(vector)
        extra: list[GateResult] = []
        if (guard := lab.guard_result(vector)) is not None:
            extra.append(guard)
        judged = judge_facts(
            facts,
            gates,
            policy,
            signals=[
                SignalConfig(
                    signal=s.signal, weight=s.weight, saturate_at=s.saturate_at, floor=s.floor
                )
                for s in policy.signals
            ],
            custom_condemn=policy.custom_signal_configs(),
            keeps=policy.keep_configs(),
            window_days=policy.popularity_window_days(),
            extra_results=extra,
        )
        self.verdict = judged.verdict
        self.score = judged.score
        self.coverage_bp = judged.coverage_bp
        self.explanation = judged.explanation
        self.facts_json = json.dumps(facts_codec.facts_to_dict(facts, extra_results=tuple(extra)))
        self.size = facts.size_bytes.value if isinstance(facts.size_bytes, Known) else None
        self.override = override
        # Through the scan's own derivation, so a reap the engine refuses is refused here too.
        self.fate = effective_fate(judged, override)


def judged_under(policy: PolicyBody) -> list[Judged]:
    """Every sampled vector, judged by the scan's own pipeline under `policy`."""
    gates = build_gates(policy)
    return [
        Judged(v, policy, gates, override=override_for(i)) for i, v in enumerate(VECTORS, start=1)
    ]


def truth_of(rows: list[Judged]) -> dict[str, Any]:
    """The aggregate a scan under that policy would produce, in the panel's own terms."""
    histogram = [0] * 10
    condemned = protected = abstained = 0
    reclaimable = 0
    unknown_size = 0
    for row in rows:
        histogram[min(row.score // 10, 9)] += 1
        if row.fate == "condemn":
            condemned += 1
            if row.size is None:
                unknown_size += 1
            else:
                reclaimable += int(row.size)
        elif row.fate == "protect":
            protected += 1
        else:
            abstained += 1
    return {
        "condemned": condemned,
        "protected": protected,
        "abstained": abstained,
        "reclaimable_bytes": reclaimable,
        "unknown_size_items": unknown_size,
        "histogram": histogram,
    }


def wire(policy: PolicyBody) -> dict[str, Any]:
    """The policy as the editor sends it -- through the route's own serializer."""
    out = _policy_out(
        policy,
        "Movies",
        requests_app_configured=True,
        settings=ProfileSettings(),
    ).body
    dumped: dict[str, Any] = out.model_dump(mode="json")
    return dumped


# ---------------------------------------------------------------------------
# The battery
# ---------------------------------------------------------------------------

BASE = DEFAULT_MOVIE_POLICY

#: The drag, at the stops an operator actually stops on.
THRESHOLDS = (1, 30, 50, 58, 70, 85, 95)

#: The scan every draft below is previewed against, judged once. Each fixture writes these
#: same rows, because what varies across the battery is the request body and not the scan.
BASE_ROWS = judged_under(BASE)


def _without(gate: GateId) -> PolicyBody:
    return BASE.model_copy(update={"gates": tuple(g for g in BASE.gates if g.gate != gate)})


def _without_list(condition: ConditionSpec) -> PolicyBody:
    """`BASE` with one shipped list protection removed.

    The successor to `_without` for the two retired list gates: membership protects through
    a ``protect_conditions`` rule per list now, so dropping one drops one list's cover
    rather than every list's at once.
    """
    return BASE.model_copy(
        update={"protect_conditions": tuple(c for c in BASE.protect_conditions if c != condition)}
    )


def _with_threshold(gate: GateId, **changes: Any) -> PolicyBody:
    return BASE.model_copy(
        update={
            "gates": tuple(
                g.model_copy(update=changes) if g.gate == gate else g for g in BASE.gates
            )
        }
    )


def _reweighted(first: int) -> PolicyBody:
    """The weights moved between the built-in signals, still summing to 100."""
    rest = 100 - first
    others = BASE.signals[1:]
    share, remainder = divmod(rest, len(others))
    return BASE.model_copy(
        update={
            "signals": (
                BASE.signals[0].model_copy(update={"weight": first}),
                *(
                    s.model_copy(update={"weight": share + (remainder if i == 0 else 0)})
                    for i, s in enumerate(others)
                ),
            )
        }
    )


def _funded(rule: BooleanCondemnSpec | GradedCondemnSpec) -> PolicyBody:
    """`BASE` plus one operator-authored condemn rule, funded from the heaviest signal.

    Removal weights total exactly 100, so a rule cannot be bolted on: its points come out of
    a built-in, which is the trade the editor makes an operator make by hand.
    """
    heaviest = max(BASE.signals, key=lambda s: s.weight)
    return BASE.model_copy(
        update={
            "custom_condemn": (rule,),
            "signals": tuple(
                s.model_copy(update={"weight": s.weight - rule.weight}) if s is heaviest else s
                for s in BASE.signals
            ),
        }
    )


#: Every draft the sweep runs, as (name, policy). Each is a SINGLE edit off the shipped
#: movie policy, because that is what an operator makes between two previews, and a sweep of
#: combinations would hide which field moved the panel.
DRAFTS: list[tuple[str, PolicyBody]] = [
    ("base", BASE),
    # --- the two post-score fields, which is the whole tuning loop -------------------
    *[(f"condemn_at={t}", BASE.model_copy(update={"condemn_at": t})) for t in (1, 30, 58, 70, 95)],
    *[
        (f"coverage_floor_bp={c}", BASE.model_copy(update={"coverage_floor_bp": c}))
        for c in (0, 2_500, 7_500, 10_000)
    ],
    # --- every shipped gate, dropped ------------------------------------------------
    *[(f"drop:{g.gate.value}", _without(g.gate)) for g in BASE.gates],
    # --- every shipped list protection, dropped -------------------------------------
    # Where `drop:whitelisted` and `drop:curated_list` used to sit. Those gates retired and
    # list membership now protects through an `on_list` condition per list, so the lane is
    # swept by dropping conditions; generated off the shipped policy, so a seeded list added
    # later is swept without editing this.
    *[(f"drop:on_list={c.value}", _without_list(c)) for c in BASE.protect_conditions],
    # --- gate thresholds ------------------------------------------------------------
    *[
        (f"min_dormancy={d}", _with_threshold(GateId.MIN_DORMANCY, threshold=d))
        for d in (5, 365, 3_650)
    ],
    ("server_popularity=1", _with_threshold(GateId.SERVER_POPULARITY, threshold=1)),
    ("server_popularity=10", _with_threshold(GateId.SERVER_POPULARITY, threshold=10)),
    # --- signals --------------------------------------------------------------------
    *[(f"unwatched_weight={w}", _reweighted(w)) for w in (0, 40, 100)],
    (
        "unwatched_ramp",
        BASE.model_copy(
            update={
                "signals": (
                    BASE.signals[0].model_copy(update={"floor": 30, "saturate_at": 400}),
                    *BASE.signals[1:],
                )
            }
        ),
    ),
    # --- the rating bars ------------------------------------------------------------
    (
        "rating_bar_low",
        BASE.model_copy(
            update={
                "keep_rating_rules": (
                    RatingRuleSpec(source=RatingSource.IMDB, floor=10, min_votes=1),
                )
            }
        ),
    ),
    (
        "rating_bar_high",
        BASE.model_copy(
            update={
                "keep_rating_rules": (
                    RatingRuleSpec(source=RatingSource.IMDB, floor=99, min_votes=1_000_000),
                )
            }
        ),
    ),
    # --- an operator-authored condemn rule, both shapes ------------------------------
    (
        "custom_boolean",
        _funded(
            BooleanCondemnSpec(
                name="a genre", field="genre", op="contains", value="Drama", weight=20
            )
        ),
    ),
    (
        "custom_graded",
        _funded(GradedCondemnSpec(name="older", field="release_age", weight=20, saturate_at=3_650)),
    ),
    # --- a graded keep, which may only ever lower a score ----------------------------
    (
        "graded_keep",
        BASE.model_copy(
            update={
                "graded_keeps": (
                    GradedKeepSpec(
                        name="watched by many",
                        field="watchers_all_time",
                        max_discount=40,
                        floor=0,
                        saturate_at=5,
                    ),
                )
            }
        ),
    ),
    # The one shape that reaches the coverage floor, and the one draft here that edits two
    # fields. Every fact a signal reads is also read by a gate, so a row that lost coverage
    # has a protection that could not be checked and abstains before the floor is consulted:
    # the floor can only bite where the gate reading that fact is OFF. Turning the rating gate
    # off is what lets the rows with no rating be condemned at 90% coverage, and the floor
    # above them is then the only thing keeping them.
    (
        "coverage_floor_bites",
        BASE.model_copy(
            update={
                "gates": tuple(g for g in BASE.gates if g.gate != GateId.RATING_FLOOR),
                "coverage_floor_bp": 9_500,
            }
        ),
    ),
    (
        "coverage_floor_open",
        BASE.model_copy(
            update={
                "gates": tuple(g for g in BASE.gates if g.gate != GateId.RATING_FLOOR),
                "coverage_floor_bp": 0,
            }
        ),
    ),
    # --- a protect condition, which may only ever keep -------------------------------
    (
        "protect_condition",
        BASE.model_copy(
            update={
                "protect_conditions": (ConditionSpec(field="watchers_all_time", op="gte", value=1),)
            }
        ),
    ),
]


def _client_over(tmp: Path, *, stored_scores_usable: bool) -> Iterator[TestClient]:
    """A booted app over a database that already holds the scan this sweep replays.

    **Function-scoped, and that is load-bearing** (rule 37). ``conftest``'s ``_hermetic``
    fixture is autouse at function scope, so a fixture declared at module or session scope
    is set up outside it: ``Settings`` reads the developer's dotenv, and the lifespan runs
    the real startup catch-up. This fixture was module-scoped for exactly one commit, and
    what that bought was a CI run whose test step sat for 71 minutes against the previous
    commit's 5, downloading a dataset in a job with no route to it.

    The snapshot is written BEFORE the app boots and never touched again while it is up. A
    second engine writing to the same SQLite file under a live async pool is a lock waiting
    to happen, and every draft in the battery replays the same stored scan anyway -- only
    the request body varies -- so there is nothing to rewrite mid-flight.
    """
    # The hermetic fixture has to be in effect by now, or this boots against a real dotenv
    # and a real network. Cheap to assert, and it is the thing that actually went wrong.
    assert Settings.model_config.get("env_file") is None, (
        "conftest's _hermetic fixture is not in effect: this fixture must be function-scoped"
    )
    tmp.mkdir(parents=True, exist_ok=True)
    settings = Settings(data_dir=tmp, secret_key="k")
    engine = sa_create_engine(settings.sync_database_url)
    Base.metadata.create_all(engine)
    engine.dispose()
    load_snapshot(tmp, BASE, BASE_ROWS)
    if not stored_scores_usable:
        break_scoring_hash(tmp)
    with TestClient(create_app(settings)) as client:
        login(client, settings)
        yield client


@pytest.fixture
def stored(tmp_path: Path) -> Iterator[TestClient]:
    """The tier that re-compares the scores the scan wrote."""
    yield from _client_over(tmp_path / "stored", stored_scores_usable=True)


@pytest.fixture
def replay(tmp_path: Path) -> Iterator[TestClient]:
    """The tier that re-runs the engine over the frozen Facts."""
    yield from _client_over(tmp_path / "replay", stored_scores_usable=False)


def load_snapshot(tmp: Path, policy: PolicyBody, rows: list[Judged]) -> None:
    """Replace the stored scan with one taken under `policy`."""
    tmp.mkdir(parents=True, exist_ok=True)
    settings = Settings(data_dir=tmp, secret_key="k")
    # Before the sync session opens, for the reason the fixture's own docstring gives: two
    # engines on one SQLite file is a lock waiting to happen.
    list_hash = seeded_fingerprint(settings)
    engine = sa_create_engine(settings.sync_database_url)
    now = utcnow()
    with Session(engine) as session:
        session.execute(delete(Candidate))
        session.execute(delete(Snapshot))
        session.execute(delete(WhitelistEntry))
        snapshot = Snapshot(
            created_at=now,
            policy_hash=combine_hashes(policy.policy_hash(), DEFAULT_TV_POLICY.policy_hash()),
            scoring_hash=combine_hashes(policy.scoring_hash(), DEFAULT_TV_POLICY.scoring_hash()),
            evidence_hash=combine_hashes(policy.evidence_hash(), DEFAULT_TV_POLICY.evidence_hash()),
            # What a scan records about the lists it gathered membership under. Omitting it
            # would make every draft below refuse, which is the correct answer for a snapshot
            # that cannot say (#512) and a useless one for a battery about the tiers.
            list_config_hash=list_hash,
            horizon_at=now,
            item_count=len(rows),
            degraded=False,
        )
        session.add(snapshot)
        session.flush()
        for index, row in enumerate(rows, start=1):
            media_key = f"radarr:1:{index}"
            session.add(
                Candidate(
                    snapshot_id=snapshot.id,
                    media_key=media_key,
                    title=f"Example Movie {index}",
                    year=2000 + index % 25,
                    media_type="movie",
                    size_bytes=row.size,
                    verdict=row.verdict,
                    score=row.score,
                    coverage_bp=row.coverage_bp,
                    explanation_json=row.explanation,
                    facts_json=row.facts_json,
                    created_at=now,
                )
            )
            if row.override is not None:
                session.add(
                    WhitelistEntry(
                        media_key=media_key,
                        title=f"Example Movie {index}",
                        decision=row.override,
                        created_at=now,
                    )
                )
        session.commit()
    engine.dispose()


def break_scoring_hash(tmp: Path) -> None:
    """Make the stored scores unusable, which is what routes an edit to the replay."""
    settings = Settings(data_dir=tmp, secret_key="k")
    engine = sa_create_engine(settings.sync_database_url)
    with Session(engine) as session:
        snapshot = session.query(Snapshot).one()
        snapshot.scoring_hash = "f" * 64
        session.commit()
    engine.dispose()


def simulate(client: TestClient, policy: PolicyBody) -> dict[str, Any]:
    response = client.post("/api/policy/simulate", json=wire(policy))
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


def assert_panel_adds_up(answer: dict[str, Any], rows: int) -> None:
    """The arithmetic the UI reads off one answer, whichever tier produced it."""
    assert answer["exact"] is True, answer.get("stale_reason")
    lanes = answer["condemned"] + answer["protected"] + answer["abstained"]
    assert lanes == rows, f"lanes hold {lanes} of {rows} rows"
    assert sum(answer["histogram"]) == rows, "the histogram lost a row"
    # `condemned_before` is sent rather than derived, so the two must still agree: the panel
    # prints it beside the deltas as one sentence.
    implied = answer["condemned"] - answer["newly_condemned"] + answer["no_longer_condemned"]
    assert answer["condemned_before"] == implied, "the saved-policy count contradicts the deltas"
    # A lane move is counted once and the two deltas are disjoint subsets of it.
    assert answer["changed_titles"] >= answer["newly_condemned"] + answer["no_longer_condemned"]
    assert answer["changed_titles"] <= rows
    assert len(answer["examples_newly_condemned"]) <= 5
    assert len(answer["examples_newly_condemned"]) <= answer["newly_condemned"]
    scores = [e["score"] for e in answer["examples_newly_condemned"]]
    assert scores == sorted(scores, reverse=True), "the examples are not the highest-scoring first"
    for gate in answer["protected_by"]:
        assert 0 < gate["count"] <= answer["protected"], f"{gate['gate']} attributes a phantom row"
    assert answer["unknown_size_items"] <= answer["condemned"]


NUMBERS = (
    "condemned",
    "protected",
    "abstained",
    "reclaimable_bytes",
    "unknown_size_items",
    "newly_condemned",
    "no_longer_condemned",
    "condemned_before",
    "changed_titles",
    "histogram",
    "protected_by",
    "examples_newly_condemned",
)


def test_every_field_of_the_answer_is_compared_across_the_two_tiers() -> None:
    """``NUMBERS`` mirrors ``SimulationOut``'s field list by hand (rule 103). A 16th field
    populated at one ``return SimulationOut(`` and not the other leaves the parity test
    below green, which is the drift the two hand-written constructors can produce.

    The three names added here are covered a different way rather than skipped:
    ``assert_panel_adds_up`` asserts ``exact``, and ``stale_kind`` / ``stale_reason`` are
    written only by ``api/simulate.py``'s ``_refused``, which is the refusal shape and
    shares no keyword set with the two answering sites.
    """
    compared = set(NUMBERS) | {"exact", "stale_kind", "stale_reason"}
    assert compared == set(SimulationOut.model_fields), (
        "api/schemas.py's SimulationOut and NUMBERS disagree. A new field belongs in "
        "NUMBERS above and must be populated at BOTH api/simulate.py return sites, "
        "_replay_simulation and simulate."
    )


@pytest.mark.parametrize("name,draft", DRAFTS, ids=[n for n, _ in DRAFTS])
class TestTheSimulatorAnswersTheSameWhicheverPathItTakes:
    """The panel is reached two ways, and an operator cannot tell which one answered."""

    def test_both_tiers_agree_and_the_panel_adds_up(
        self, name: str, draft: PolicyBody, stored: TestClient, replay: TestClient
    ) -> None:
        by_score = simulate(stored, draft)
        assert_panel_adds_up(by_score, len(BASE_ROWS))

        by_replay = simulate(replay, draft)
        assert_panel_adds_up(by_replay, len(BASE_ROWS))

        for key in NUMBERS:
            assert by_score[key] == by_replay[key], (
                f"{name}: the two tiers disagree about {key} -- "
                f"stored path says {by_score[key]}, the replay says {by_replay[key]}"
            )

    def test_the_replay_previews_what_the_next_scan_will_decide(
        self, name: str, draft: PolicyBody, replay: TestClient
    ) -> None:
        """The panel's promise, against the scan's own judgment of the same evidence."""
        answer = simulate(replay, draft)
        truth = truth_of(judged_under(draft))
        for key, expected in truth.items():
            assert answer[key] == expected, (
                f"{name}: the preview says {key}={answer[key]}, "
                f"a scan under the same draft decides {expected}"
            )


class TestTheSweepReachesEveryLaneItClaimsTo:
    """What the 220 shapes actually exercise, counted rather than assumed (rule 145).

    A sweep is only as wide as its rows: every assertion above is flag-shaped, and a fixture
    that condemned nothing, protected nothing, or carried no hand decision would satisfy all
    of them while covering none of the branches they are written for.

    **These counts are properties of the sample, so a regeneration moves them** and each one
    is re-reconciled by hand rather than pasted (``scripts/policy_lab_extract.py``). What is
    being checked is never the number itself: it is that each lane, each arm and each
    quiet branch is still populated at all. The last regeneration moved two of them --
    the lane split, and how many rows the coverage floor holds back -- both toward wider
    coverage, and left the rest untouched.
    """

    def test_the_rows_land_in_every_lane_and_carry_every_hand_decision(self) -> None:
        rows = judged_under(BASE)
        assert len(rows) == 220
        lanes = {
            lane: sum(1 for r in rows if r.fate == lane)
            for lane in ("condemn", "protect", "abstain")
        }
        assert lanes == {"condemn": 29, "protect": 170, "abstain": 21}, lanes

        spares = [r for r in rows if r.override == "spare"]
        reaps = [r for r in rows if r.override == "reap"]
        assert len(spares) == 30
        assert len(reaps) == 27
        # Both arms of the reap branch, because each path decides it with different code --
        # the stored one reads the frozen explanation (`reap_is_effective`), the replay
        # re-derives it (`effective_fate`). With only honored reaps in the sample the two
        # could disagree about a refusal and every assertion here would stay green.
        assert sum(1 for r in reaps if r.fate == "condemn") == 18, "no hand reap is honored"
        assert sum(1 for r in reaps if r.fate != "condemn") == 9, "no hand reap is refused"
        assert all(r.fate == "protect" for r in spares), "a hand spare failed to protect"

        # The panel's byte total has an unmeasured arm, and a library of known sizes never
        # reaches it.
        assert sum(1 for r in rows if r.size is None) == 36

    def test_the_evidence_is_uneven_enough_to_reach_the_coverage_floor(self) -> None:
        """Partial coverage exists, and one draft in the battery is actually held by it.

        Coverage is the quietest of the panel's inputs: on a fully observed library the floor
        is inert whatever it is set to, so a sweep can carry four settings of it and exercise
        the branch zero times. `coverage_floor_bites` is the shape that reaches it, and the
        assertion is the difference it makes rather than its presence.
        """
        spread = {r.coverage_bp for r in judged_under(BASE)}
        assert spread == {7_000, 8_000, 9_000, 10_000}, sorted(spread)

        drafts = dict(DRAFTS)
        held = sum(
            1 for r in judged_under(drafts["coverage_floor_bites"]) if r.verdict == "condemn"
        )
        free = sum(1 for r in judged_under(drafts["coverage_floor_open"]) if r.verdict == "condemn")
        assert free - held == 10, f"the floor holds {free - held} rows back"

    def test_the_drafts_move_the_answer_they_are_swept_for(self) -> None:
        """The drafts that shift no verdict are named, so none of them can vouch for a path.

        Every oracle above compares two answers, and two answers that are both the baseline
        agree trivially. A draft on this list still earns its place -- it exercises the
        ROUTE, whose tier is chosen by a hash and not by whether anything moved -- but it
        proves nothing about the arithmetic, and a battery that quietly became all-inert
        would go on passing. If this set grows, the fixture stopped reaching a branch.
        """
        baseline = [r.fate for r in judged_under(BASE)]
        inert = {
            name for name, draft in DRAFTS if [r.fate for r in judged_under(draft)] == baseline
        }
        assert inert == {
            # The control, and the two shipped values written out again.
            "base",
            "condemn_at=70",
            "coverage_floor_bp=0",
            # A floor with the rating gate ON: every row that lost coverage lost it by losing
            # a fact some gate reads, so it abstains on the unchecked gate before the floor is
            # ever consulted. `coverage_floor_bites` is the same field reached the one way it
            # can bite.
            "coverage_floor_bp=2500",
            "coverage_floor_bp=7500",
            "coverage_floor_bp=10000",
            # Protections whose rows another protection already covers. Real, and the reason
            # the sweep drops protections one at a time rather than trusting any single one
            # to move the library. The two list rules are the retired whitelist and curated
            # gates, and they cover the same rows those gates did: 14 of the sampled 220
            # carry a readable membership, and each is also held by a gate -- dormancy for
            # 12 of them, the rating floor alone for the other 2.
            "drop:data_horizon",
            "drop:on_list=Titles you've tagged",
            "drop:on_list=IMDb Top 250",
            # The popularity gate's threshold, which on this library changes who it protects
            # without changing anyone's fate: dormancy covers the same rows.
            "server_popularity=1",
            "server_popularity=10",
            # All 100 points on dormancy moves scores without moving one across the line.
            "unwatched_weight=100",
        }, sorted(inert)


@pytest.mark.parametrize("name,draft", DRAFTS, ids=[n for n, _ in DRAFTS])
class TestEveryDraftSurvivesTheRoundTripTheRouteCompares:
    """A field the wire drops changes the hash, and the panel then refuses forever.

    No snapshot needed: the route hashes what it parses back out of the request, and compares
    that against what the scan recorded. A field the editor cannot carry therefore does not
    merely fail to apply -- it makes every edit look like a different policy, which is the
    upgraded-install failure reached through a field instead of a version.
    """

    def test_the_wire_preserves_every_hash_the_route_reads(
        self, name: str, draft: PolicyBody
    ) -> None:
        again = _to_body(PolicyIn.model_validate(wire(draft)))
        assert again.scoring_hash() == draft.scoring_hash(), (
            f"{name}: scoring hash lost on the wire"
        )
        assert again.evidence_hash() == draft.evidence_hash(), (
            f"{name}: evidence hash lost on the wire"
        )
        assert again.policy_hash() == draft.policy_hash(), f"{name}: policy hash lost on the wire"


#: Edits that change what a scan GATHERS rather than what it makes of what it gathered. The
#: frozen evidence cannot answer these, and the panel's contract is to say so rather than to
#: report a confident number off stale facts.
#:
#: `keep_tags` and `keep_tags_match` were the other two, and they left the policy rather than
#: this list: the tags a title carries are gathered into `Facts.on_lists` now, and the rule
#: naming them is `protect_conditions`, which replays. So the window is the only *policy*
#: field that still moves the gathering half, and a second case cannot be invented -- swept
#: at two windows instead, since one draft cannot show the refusal tracks the value.
NEEDS_A_SCAN: list[tuple[str, PolicyBody]] = [
    (
        "popularity_window=90",
        _with_threshold(GateId.SERVER_POPULARITY, window_days=90),
    ),
    (
        "popularity_window=730",
        _with_threshold(GateId.SERVER_POPULARITY, window_days=730),
    ),
]


@pytest.mark.parametrize("name,draft", NEEDS_A_SCAN, ids=[n for n, _ in NEEDS_A_SCAN])
class TestAnEditTheFrozenEvidenceCannotAnswerIsRefused:
    def test_it_refuses_instead_of_reporting_stale_numbers(
        self, name: str, draft: PolicyBody, stored: TestClient
    ) -> None:
        answer = simulate(stored, draft)
        assert answer["exact"] is False, f"{name}: previewed an edit the frozen evidence predates"
        assert answer["stale_reason"], f"{name}: refused without saying why"


class TestTheThresholdBehavesLikeAThresholdThroughTheRoute:
    """Monotonicity, asserted where the operator meets it: the drag itself.

    ``test_policy_permutations`` pins this against the engine. The panel reaches the same
    answer through arithmetic of its own -- a re-comparison on one path, a re-decision on the
    other -- and it is the panel's number the operator drags against.
    """

    def test_raising_the_threshold_never_grows_the_removal_list(
        self, stored: TestClient, replay: TestClient
    ) -> None:
        by_score = [
            simulate(stored, BASE.model_copy(update={"condemn_at": t}))["condemned"]
            for t in THRESHOLDS
        ]
        by_replay = [
            simulate(replay, BASE.model_copy(update={"condemn_at": t}))["condemned"]
            for t in THRESHOLDS
        ]
        assert by_score == sorted(by_score, reverse=True), by_score
        assert by_replay == sorted(by_replay, reverse=True), by_replay
        # And the two paths agree at every stop, not merely in shape.
        assert by_score == by_replay
        # A sweep that never crosses anything would satisfy both of the above.
        assert by_score[0] > by_score[-1], "the threshold moves nothing across this library"
