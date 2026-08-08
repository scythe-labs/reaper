# SPDX-License-Identifier: AGPL-3.0-or-later
"""The REST surface.

Almost entirely read-only. The single exception is ``POST /runs/{id}/execute``, which is
gated hard -- deletion must be enabled on the host and the caller must echo the plan's
content-bound confirmation phrase -- and even then ``GuardedTransport`` refuses any call
that was not journalled first. The tests below exercise those gates; the delete mechanics
themselves live in ``test_reap_loop`` against fakes.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine as sa_create_engine
from sqlalchemy import select
from sqlalchemy.orm import Session

from reaper import logbuffer
from reaper.clock import utcnow
from reaper.config import Settings
from reaper.crypto import SecretBox
from reaper.db.base import Base
from reaper.db.models import (
    ActionStep,
    Candidate,
    FirstFlagged,
    Instance,
    InstanceKind,
    PlexServer,
    Profile,
    Snapshot,
    StepState,
)
from reaper.db.models import Policy as PolicyModel
from reaper.engine.policy import (
    DEFAULT_MOVIE_POLICY,
    DEFAULT_TV_POLICY,
    GateSetting,
    PolicyBody,
    SignalSetting,
    combine_hashes,
)
from reaper.main import create_app
from reaper.secrets import resolve_kdf_salt, resolve_old_keys, resolve_secret_key
from reaper.services import retention
from reaper.services.history_sync import SCHEMA

from ._auth import login
from ._lists import seeded_fingerprint

# No "whitelisted" row: the gate is retired (list membership protects through ``on_list``
# keep rules), and the save boundary refuses it -- test_simulate_hardening pins that.
DEFAULT_GATES = [
    {"gate": "min_dormancy", "threshold": 1095},
    {"gate": "rating_floor", "threshold": 75},
    {"gate": "server_popularity", "threshold": 3},
]
#: One signal carrying the whole 100-point budget (PolicyBody._weights_total_one_hundred).
DEFAULT_SIGNALS = [{"signal": "unwatched", "weight": 100, "saturate_at": 1825, "floor": 365}]


def _policy(condemn_at: int = 70, **overrides: object) -> dict[str, Any]:
    return {
        "condemn_at": condemn_at,
        "gates": DEFAULT_GATES,
        "signals": DEFAULT_SIGNALS,
        **overrides,
    }


def _fixture_scoring_hash() -> str:
    """The scoring hash of the policies the fixture snapshot was 'scored' with.

    The simulator refuses to re-decide a snapshot whose scores came from a *different* set of
    signals and gates, so the fixture has to be self-consistent -- exactly as a real snapshot
    is. Movies and TV are scored under separate policies now, so the stored hash is the
    combination of both (movie first, then the default TV policy, since none is saved here).
    """
    movie = PolicyBody(
        condemn_at=70,
        gates=tuple(GateSetting.model_validate(g) for g in DEFAULT_GATES),
        signals=tuple(SignalSetting.model_validate(s) for s in DEFAULT_SIGNALS),
    )
    return combine_hashes(movie.scoring_hash(), DEFAULT_TV_POLICY.scoring_hash())


def _fixture_policy_hash() -> str:
    """The POLICY hash of the same pair, for the same reason the scoring hash exists.

    The executor refuses to run a plan whose policy hash is not the one in force -- an
    operator who tightens their policy after approving a plan must not have it delete the
    files they just protected -- so a fixture snapshot has to carry the hash of the policies
    actually in force in that test app (no saved rows, so both defaults) or every dry run
    against it is (correctly) refused as out of date.
    """
    return combine_hashes(DEFAULT_MOVIE_POLICY.policy_hash(), DEFAULT_TV_POLICY.policy_hash())


# ``MinDormancyGate``'s ABSTAIN branch, verbatim, kept on one line so the guard in
# ``test_repo_hygiene.test_the_documented_checked_example_is_one_a_gate_emits`` can find it in
# the source text. It used to be an invented "checked: <label> -- <numbers>" shape no gate has
# ever emitted (#419), and asserting THAT survived the wire is what made the format read as
# load-bearing.
CHECKED_DETAIL = "Untouched for 5 years, 7 months, past the 3 years it has to sit unwatched first."


def _explanation(score: float) -> str:
    return json.dumps(
        {
            "score": score,
            "threshold": 70,
            "coverage": 1.0,
            "signals": [
                {
                    "id": "unwatched",
                    "contribution": score,
                    "weight": 70,
                    "detail": "not watched in 5 years, 7 months",
                    "evaluated": True,
                }
            ],
            "protections_fired": [],
            "protections_checked": [{"gate": "min_dormancy", "detail": CHECKED_DETAIL}],
            "protections_unknown": [],
        }
    )


@pytest.fixture
def client(tmp_path: Path) -> Iterator[TestClient]:
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
            policy_hash=_fixture_policy_hash(),
            scoring_hash=_fixture_scoring_hash(),
            list_config_hash=list_hash,
            horizon_at=now,
            item_count=4,
            degraded=False,
        )
        session.add(snapshot)
        session.flush()

        # The instances and Plex server the why-panel's jump links are built from. The
        # radarr row's id must be the instance id inside the fixtures' media_key.
        session.add_all(
            [
                Instance(
                    id=1,
                    kind=InstanceKind.RADARR,
                    name="hd",
                    base_url="https://radarr.example",
                    api_key_enc="enc",
                    # This Radarr blocks re-download, so the plan body below carries the
                    # exclusion. The flag is per-instance and off by default; the plan
                    # mirrors whatever this row says (see planner.build_plan).
                    add_import_exclusion=True,
                    created_at=now,
                ),
                Instance(
                    id=2,
                    kind=InstanceKind.TAUTULLI,
                    name="main",
                    base_url="https://tautulli.example",
                    api_key_enc="enc",
                    created_at=now,
                ),
                Instance(
                    id=3,
                    kind=InstanceKind.SEERR,
                    name="requests",
                    base_url="https://seerr.example",
                    api_key_enc="enc",
                    created_at=now,
                ),
                PlexServer(
                    machine_identifier="abc123",
                    name="Example Server",
                    connection_uri="http://plex.local:32400",
                    token_enc="enc",
                    created_at=now,
                ),
            ]
        )

        # A condemned item, a protected one, and one we could not judge.
        session.add_all(
            [
                Candidate(
                    snapshot_id=snapshot.id,
                    media_key="radarr:1:10",
                    title="Example Movie",
                    media_type="movie",
                    size_bytes=5_900_000_000,
                    year=2013,
                    genres_json=json.dumps(["Documentary", "Drama"]),
                    quality="Bluray-1080p",
                    # The display metadata a scan freezes for the panel head + card badges.
                    plex_rating_key=555,
                    tmdb_id=603,
                    imdb_id="tt0000001",
                    video_resolution="1080",
                    content_rating="PG-13",
                    runtime_minutes=95,
                    library_title="Movies",
                    ratings_json=json.dumps(
                        {
                            "imdb": 59,
                            "imdb_votes": 35_072,
                            "rotten_tomatoes_critic": 77,
                            "rotten_tomatoes_audience": 71,
                            "tmdb": 61,
                        }
                    ),
                    verdict="condemn",
                    score=91,
                    coverage_bp=10_000,
                    explanation_json=_explanation(91),
                    created_at=now,
                ),
                Candidate(
                    snapshot_id=snapshot.id,
                    media_key="radarr:1:11",
                    title="Example Classic",
                    media_type="movie",
                    size_bytes=8_000_000_000,
                    genres_json=json.dumps(["Drama"]),
                    quality="WEBDL-1080p",
                    library_title="Classics",
                    verdict="protect",
                    score=90,
                    coverage_bp=10_000,
                    explanation_json=json.dumps(
                        {
                            **json.loads(_explanation(90)),
                            # ``RatingFloorGate``'s PROTECT branch, verbatim. A fired
                            # protection is a lowercase fragment with no trailing period,
                            # where a checked one is a whole sentence (``gates.py``, above
                            # the PROTECT return); the invented string here had neither shape.
                            "protections_fired": [
                                {
                                    "gate": "rating_floor",
                                    "detail": "well rated: 8.0 on IMDb from 250,000 votes",
                                }
                            ],
                        }
                    ),
                    created_at=now,
                ),
                Candidate(
                    snapshot_id=snapshot.id,
                    media_key="radarr:1:12",
                    title="Unmatched",
                    media_type="movie",
                    size_bytes=1_000_000_000,
                    verdict="abstain",
                    score=50,
                    coverage_bp=2_000,  # below the coverage floor
                    # The shape production writes for an item Plex could not be matched
                    # to: a match block, and one "could not check" per blocked gate, all
                    # sharing the same cause.
                    explanation_json=json.dumps(
                        {
                            **json.loads(_explanation(50)),
                            "base_score": 50.0,
                            "keep_discount": 0.0,
                            "match": {
                                "status": "unmatched",
                                "by": None,
                                "detail": "No Plex item matched this title",
                                "rating_key": None,
                            },
                            "keeps": [
                                {
                                    "name": "well rated",
                                    "discount": 0.0,
                                    "max_discount": 15,
                                    "detail": "could not check the IMDb rating",
                                    "evaluated": False,
                                }
                            ],
                            "protections_unknown": [
                                {
                                    "gate": "min_dormancy",
                                    "detail": (
                                        "could not check when it was last watched: "
                                        "Plex has not matched this item"
                                    ),
                                },
                                {
                                    "gate": "data_horizon",
                                    "detail": (
                                        "could not check the watch horizon: "
                                        "Plex has not matched this item"
                                    ),
                                },
                                {
                                    "gate": "server_popularity",
                                    "detail": (
                                        "could not check watch history: "
                                        "Plex has not matched this item"
                                    ),
                                },
                            ],
                        }
                    ),
                    created_at=now,
                ),
                # A cleanly-abstained item: every protection was checked (none blocked),
                # full coverage, score simply below the threshold. This is the only kind
                # of abstained row a draft threshold may pull in -- the simulator tests
                # lean on the contrast with "Unmatched" above, whose protections could
                # not be checked and which must stay abstained at ANY threshold.
                Candidate(
                    snapshot_id=snapshot.id,
                    media_key="radarr:1:13",
                    title="Example Oldie",
                    media_type="movie",
                    size_bytes=500_000_000,
                    verdict="abstain",
                    score=45,
                    coverage_bp=10_000,
                    explanation_json=_explanation(45),
                    created_at=now,
                ),
            ]
        )
        session.add(
            FirstFlagged(media_key="radarr:1:10", first_flagged_at=now, last_seen_condemned_at=now)
        )
        session.commit()
    engine.dispose()

    with TestClient(create_app(settings)) as c:
        login(c, settings)
        yield c


class TestTheRunsApi:
    """Building, reviewing and dry-running a plan through the API. Nothing deletes."""

    def test_an_unbounded_selection_is_refused_at_the_edge(self, client: TestClient) -> None:
        """``media_keys`` is the destructive path's list input, and it had no length bound at
        all: an authenticated caller (or a leaked API key -- POST /api/runs is in the write
        allow-list) could push an arbitrarily large list straight into a plan build and the
        whitelist queries under it. Note the two are different requests: OMITTING the field
        still means the whole condemned set, so the bound never truncates a real reap."""
        refused = client.post("/api/runs", json={"media_keys": ["radarr:1:1"] * 5001})

        assert refused.status_code == 422

    def test_an_oversized_media_key_is_refused_at_the_edge(self, client: TestClient) -> None:
        """The key columns are 100 characters. A multi-megabyte one is not a key; it is a
        payload, and it should never reach a query."""
        long_key = "radarr:1:" + "9" * 200

        assert client.post("/api/whitelist", json={"media_key": long_key}).status_code == 422
        assert (
            client.post(
                "/api/override", json={"media_key": long_key, "decision": "spare"}
            ).status_code
            == 422
        )

    def test_an_unknown_key_is_refused_for_what_reaper_can_actually_know(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``_resolve_title`` used to answer "No scanned item has that identity", which
        ``services.retention`` made false: the sweep keeps only the newest
        ``KEEP_SNAPSHOTS``, so a scan may well have held the item and Reaper discarded the
        row. The refusal now claims only what survives -- no record -- and names the window
        as the scan COUNT it is, since 30 scans is a month of nightly scanning and no fixed
        time at all on an install scanned by hand (#326).

        The count is patched off its default so an assertion cannot pass against a
        transcribed 30: the copy has to read the constant the sweep itself honors, and a
        hardcoded one fails here (rule 141).
        """
        monkeypatch.setattr(retention, "KEEP_SNAPSHOTS", 7)

        for route, payload in (
            ("/api/whitelist", {"media_key": "radarr:1:never-scanned"}),
            ("/api/override", {"media_key": "radarr:1:never-scanned", "decision": "spare"}),
        ):
            refused = client.post(route, json=payload)
            assert refused.status_code == 404, refused.text
            assert refused.json()["detail"] == (
                "Reaper has no record of that item. It keeps only the last 7 scans."
            )

    def test_a_plan_shows_the_literal_steps_and_the_confirmation_phrase(
        self, client: TestClient
    ) -> None:
        """The plan is the why-panel's third block made real: the exact request each
        deletion would issue, plus the content-bound confirmation the owner approves."""
        run = client.post("/api/runs").json()

        assert run["state"] == "planned"
        assert run["item_count"] == 1  # the single condemned movie
        # The confirmation is bound to what would be deleted: 1 item, ~5.5 GiB.
        assert run["confirmation_phrase"].startswith("REAP 1 SOUL")

        step = run["steps"][0]
        assert step["method"] == "DELETE"
        assert step["path"] == "/api/v3/movie/10"  # radarr:1:10 -> movie 10
        assert step["is_canary"] is True
        # The body mirrors this Radarr's own setting (the fixture's row blocks re-download).
        assert step["body"] == {"deleteFiles": True, "addImportExclusion": True}
        # No credential is ever in a journalled step.
        assert "api_key" not in json.dumps(step).lower()

    def test_a_failed_step_reads_back_why_it_failed(
        self, client: TestClient, tmp_path: Path
    ) -> None:
        """The journal's reason survives the process that wrote it (#260).

        `action_step.error` was durable and on no response schema, so the live reason lived
        only in memory on `app.state`: a restart left the plan table saying a step failed and
        nothing saying why, while the row held the sentence the whole time. This drives that
        exact shape -- the state and the reason are written straight to the row, then read
        back over HTTP through a response built from nothing but the database.

        The reason is already operator copy; the executor writes one sentence and uses it for
        this column and for the live report both.
        """
        run = client.post("/api/runs").json()
        reason = "Radarr accepted the delete; not confirmed. Reaper could not reach it again."

        engine = sa_create_engine(Settings(data_dir=tmp_path, secret_key="k").sync_database_url)
        with Session(engine) as session:
            step = session.execute(
                select(ActionStep).where(ActionStep.run_id == run["id"])
            ).scalar_one()
            step.state = StepState.FAILED
            step.error = reason
            session.commit()
        engine.dispose()

        read_back = client.get(f"/api/runs/{run['id']}").json()["steps"][0]
        assert read_back["state"] == "failed"
        assert read_back["error"] == reason

    def test_a_step_that_has_not_run_carries_no_reason(self, client: TestClient) -> None:
        """The other direction, so the field above cannot pass by always being populated:
        a freshly planned step has nothing to explain and says nothing."""
        run = client.post("/api/runs").json()
        assert run["steps"][0]["error"] is None

    def test_a_dry_run_walks_the_plan_and_deletes_nothing(self, client: TestClient) -> None:
        run = client.post("/api/runs").json()

        report = client.post(f"/api/runs/{run['id']}/dry-run").json()

        assert report["dry_run"] is True
        assert report["state"] == "completed"
        assert report["would_delete_items"] == 0  # nothing actually deleted
        assert report["outcomes"][0]["state"] == "skipped"
        assert "would DELETE /api/v3/movie/10" in report["outcomes"][0]["detail"]

    def test_a_plan_appears_in_the_run_list(self, client: TestClient) -> None:
        created = client.post("/api/runs").json()
        listed = client.get("/api/runs").json()
        assert any(r["id"] == created["id"] for r in listed)

    def test_the_run_list_limit_is_bounded_at_both_ends(self, client: TestClient) -> None:
        """``?limit=-1`` rendered LIMIT -1, which SQLite reads as NO limit.

        One request then returned every run ever made. The rows are cheap now (see the
        test below), which lowers the cost of that but does not bound it, so the limit is
        still refused at the boundary in both directions rather than clamped in silence.
        """
        client.post("/api/runs")
        assert client.get("/api/runs", params={"limit": -1}).status_code == 422
        assert client.get("/api/runs", params={"limit": 0}).status_code == 422
        assert client.get("/api/runs", params={"limit": 201}).status_code == 422
        assert client.get("/api/runs", params={"limit": 200}).status_code == 200
        assert client.get("/api/runs", params={"limit": 1}).status_code == 200

    def test_the_run_list_carries_only_what_is_stored(self, client: TestClient) -> None:
        """The history is stored rows, nothing derived.

        Deriving a run's counts, totals and phrase means re-reading the whitelist, the
        profile and the whole condemned set of that run's snapshot -- per run, so a list of
        fifty read the same thousands of rows fifty times on every visit to the Reap page
        (P-3). It was dishonest as well as slow: a finished run's phrase was recomputed
        against TODAY's overrides, describing a plan nobody ever approved. Opening a run
        goes to the detail route, which derives them for that one.
        """
        client.post("/api/runs")
        row = client.get("/api/runs").json()[0]
        assert set(row) == {
            "id",
            "snapshot_id",
            "state",
            "approved_by",
            "approved_at",
            "aborted_reason",
            "held_back_unknown_size",
        }
        # ...and the detail route still answers with the whole thing.
        full = client.get(f"/api/runs/{row['id']}").json()
        assert full["confirmation_phrase"].startswith("REAP ")
        assert isinstance(full["steps"], list)

    def test_dry_running_a_missing_run_is_a_404(self, client: TestClient) -> None:
        assert client.post("/api/runs/9999/dry-run").status_code == 404

    def test_execute_is_refused_while_deletion_is_off(self, client: TestClient) -> None:
        """The default client is read-only. Even the correct confirmation phrase cannot
        execute a real reap while deletion is disabled -- the arm gate comes first."""
        run = client.post("/api/runs").json()
        resp = client.post(
            f"/api/runs/{run['id']}/execute",
            json={"confirmation_phrase": run["confirmation_phrase"]},
        )
        assert resp.status_code == 403
        assert "deletion is turned off" in resp.json()["detail"].lower()


@pytest.fixture
def selection_client(tmp_path: Path) -> Iterator[TestClient]:
    """A client whose snapshot holds THREE condemned movies and one protected one.

    The main ``client`` fixture has a single condemned item, which makes "planned just the
    one I asked for" and "planned the whole set" the same number -- so the selection could
    not be told from the fall-through. Three is the smallest count that distinguishes them.
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
            policy_hash=_fixture_policy_hash(),
            scoring_hash=_fixture_scoring_hash(),
            list_config_hash=list_hash,
            horizon_at=now,
            item_count=4,
            degraded=False,
        )
        session.add(snapshot)
        session.flush()
        session.add(
            Instance(
                id=1,
                kind=InstanceKind.RADARR,
                name="hd",
                base_url="https://radarr.example",
                api_key_enc="enc",
                created_at=now,
            )
        )
        session.add_all(
            [
                Candidate(
                    snapshot_id=snapshot.id,
                    media_key=f"radarr:1:{n}",
                    title=f"Example Movie {n}",
                    media_type="movie",
                    size_bytes=1_000_000_000 * n,
                    verdict="condemn",
                    score=91,
                    coverage_bp=10_000,
                    explanation_json=_explanation(91),
                    created_at=now,
                )
                for n in (10, 11, 12)
            ]
            + [
                Candidate(
                    snapshot_id=snapshot.id,
                    media_key="radarr:1:13",
                    title="Example Keeper",
                    media_type="movie",
                    size_bytes=4_000_000_000,
                    verdict="protect",
                    score=10,
                    coverage_bp=10_000,
                    explanation_json=_explanation(10),
                    created_at=now,
                )
            ]
        )
        session.add_all(
            FirstFlagged(
                media_key=f"radarr:1:{n}", first_flagged_at=now, last_seen_condemned_at=now
            )
            for n in (10, 11, 12)
        )
        session.commit()
    engine.dispose()

    with TestClient(create_app(settings)) as c:
        login(c, settings)
        yield c


class TestTheRunSelectionIsExplicit:
    """``POST /api/runs`` is where "nothing selected" is translated for the planner.

    Golden rule 1 lives on this one ternary: an OMITTED ``media_keys`` means the whole
    condemned set, an explicit ``[]`` means nothing, and the two must never collapse into
    each other. ``[]`` is falsy, so the obvious-looking ``if payload and payload.media_keys``
    turns a select-nothing request into a plan over every condemned item in the library --
    which is the deletion this codebase exists to prevent. The planner's own side of the
    contract is covered directly (test_review_reap), but this route is the only place the
    empty list is ever translated, so it is tested here at the edge.
    """

    def test_an_empty_selection_plans_nothing_rather_than_everything(
        self, selection_client: TestClient
    ) -> None:
        """The one that catches the falsy-collapse rewrite: refused, not expanded."""
        refused = selection_client.post("/api/runs", json={"media_keys": []})

        assert refused.status_code == 422
        assert "no items were selected" in refused.json()["detail"].lower()
        # And nothing was journalled: a refused selection leaves no plan behind at all.
        assert selection_client.get("/api/runs").json() == []

    def test_an_omitted_selection_plans_the_whole_condemned_set(
        self, selection_client: TestClient
    ) -> None:
        """A body-less POST, and an explicit ``null``, both mean "reap everything condemned"
        -- the distinction the empty list above must not be folded into."""
        bare = selection_client.post("/api/runs").json()
        explicit_null = selection_client.post("/api/runs", json={"media_keys": None}).json()

        assert bare["item_count"] == 3
        assert explicit_null["item_count"] == 3
        assert {s["media_key"] for s in bare["steps"]} == {
            "radarr:1:10",
            "radarr:1:11",
            "radarr:1:12",
        }

    def test_a_named_selection_plans_exactly_what_was_named(
        self, selection_client: TestClient
    ) -> None:
        """One key of three, so a plan that quietly fell through to the whole set is a
        different number rather than the same one."""
        run = selection_client.post("/api/runs", json={"media_keys": ["radarr:1:11"]}).json()

        assert run["item_count"] == 1
        assert {s["media_key"] for s in run["steps"]} == {"radarr:1:11"}
        # The confirmation phrase is bound to the selection, not to the condemned set.
        assert run["confirmation_phrase"].startswith("REAP 1 SOUL")

    def test_a_selection_naming_something_not_condemned_is_refused_whole(
        self, selection_client: TestClient
    ) -> None:
        """Refuse loudly rather than plan the subset that happens to be valid: a plan that
        silently covers fewer items than asked is how an operator approves one deletion and
        gets another."""
        refused = selection_client.post(
            "/api/runs", json={"media_keys": ["radarr:1:10", "radarr:1:13"]}
        )

        assert refused.status_code == 422
        assert selection_client.get("/api/runs").json() == []


@pytest.fixture
def armed_client(tmp_path: Path) -> Iterator[TestClient]:
    """A client armed at the host default (``destructive_actions_enabled=True``), with one
    condemned movie. The movie's Radarr is configured so ``build_plan`` can resolve it (a plan
    refuses a movie whose Radarr is gone), but no Plex or Tautulli is, so the execute
    endpoint's client-presence gate still refuses before any live service is touched. Enough
    to exercise the confirmation and client-presence gates without any live service."""
    settings = Settings(data_dir=tmp_path, secret_key="k", destructive_actions_enabled=True)
    # The box the app decrypts the Radarr key with at execute time. Built exactly as main.py
    # builds it, off the same settings, so the api_key below opens under app.state.secret_box;
    # resolve_kdf_salt mints the per-install salt here and create_app reads the same one.
    box = SecretBox(
        resolve_secret_key(settings),
        *resolve_old_keys(settings),
        salt=resolve_kdf_salt(settings),
    )
    engine = sa_create_engine(settings.sync_database_url)
    Base.metadata.create_all(engine)
    # What a scan records about the lists it gathered membership under: without it the
    # simulator refuses, which is right for a snapshot that cannot say and useless here.
    list_hash = seeded_fingerprint(settings)

    now = utcnow()
    with Session(engine) as session:
        snapshot = Snapshot(
            created_at=now,
            policy_hash=_fixture_policy_hash(),
            scoring_hash=_fixture_scoring_hash(),
            list_config_hash=list_hash,
            horizon_at=now,
            item_count=1,
            degraded=False,
        )
        session.add(snapshot)
        session.flush()
        session.add(
            Instance(
                id=1,
                kind=InstanceKind.RADARR,
                name="hd",
                base_url="https://radarr.example",
                api_key_enc=box.encrypt("test-key"),
                created_at=now,
            )
        )
        session.add(
            Candidate(
                snapshot_id=snapshot.id,
                media_key="radarr:1:10",
                title="Worthless Movie",
                media_type="movie",
                size_bytes=5_900_000_000,
                verdict="condemn",
                score=91,
                coverage_bp=10_000,
                explanation_json=_explanation(91),
                created_at=now,
            )
        )
        session.add(
            FirstFlagged(media_key="radarr:1:10", first_flagged_at=now, last_seen_condemned_at=now)
        )
        session.commit()
    engine.dispose()

    with TestClient(create_app(settings)) as c:
        login(c, settings)
        yield c


class TestExecuteGates:
    """The four gates on the one endpoint that deletes. None of these tests reaches a live
    service: the confirmation gate refuses first, and the client-presence gate refuses
    before any HTTP is attempted."""

    def test_a_wrong_confirmation_phrase_is_refused(self, armed_client: TestClient) -> None:
        run = armed_client.post("/api/runs").json()
        resp = armed_client.post(
            f"/api/runs/{run['id']}/execute",
            json={"confirmation_phrase": "REAP 999 SOULS 999 GB"},
        )
        assert resp.status_code == 409
        assert "does not match" in resp.json()["detail"].lower()

    def test_the_right_phrase_but_no_plex_is_refused_before_any_delete(
        self, armed_client: TestClient
    ) -> None:
        """Armed and the phrase matches -- but with no Plex configured the streaming veto
        cannot run, so the executor refuses rather than deleting blind. Proves the happy
        path reaches the executor and stops at the right interlock."""
        run = armed_client.post("/api/runs").json()
        resp = armed_client.post(
            f"/api/runs/{run['id']}/execute",
            json={"confirmation_phrase": run["confirmation_phrase"]},
        )
        assert resp.status_code == 409
        assert "without plex" in resp.json()["detail"].lower()

    def test_executing_a_missing_run_is_a_404(self, armed_client: TestClient) -> None:
        resp = armed_client.post(
            "/api/runs/9999/execute", json={"confirmation_phrase": "REAP 0 SOULS 0 GB"}
        )
        assert resp.status_code == 404

    def test_the_reap_status_is_idle_before_any_run(self, armed_client: TestClient) -> None:
        """The browser polls this to follow a reap and to re-attach to one already running.
        With nothing in flight it reads idle, never a stale 'running'."""
        body = armed_client.get("/api/runs/execute/status").json()
        assert body["running"] is False
        assert body["run_id"] is None

    def test_stopping_when_nothing_is_running_is_a_409(self, armed_client: TestClient) -> None:
        """Stop is only meaningful for the run actually in flight. With none running it is a
        clear 409, not a silent no-op that might read as 'stopped'."""
        run = armed_client.post("/api/runs").json()
        resp = armed_client.post(f"/api/runs/{run['id']}/stop")
        assert resp.status_code == 409
        assert "not currently running" in resp.json()["detail"].lower()

    def test_a_reap_refused_because_one_is_running_says_so_in_the_log(
        self, armed_client: TestClient
    ) -> None:
        """The refusal an operator hits by pressing Reap in a second tab.

        It is raised before the slot is claimed, so it never reaches the handler that logs
        every other synchronous refusal, and the operator dismisses a toast the server kept
        no record of. Asserted against the ring, because that is what a downloaded log holds.
        """
        from reaper.api.runs import _reap_status

        run = armed_client.post("/api/runs").json()
        # The production accessor, which creates the status lazily: a hand-built one would
        # not be the object the route reads (rule 119).
        status = _reap_status(armed_client.app)  # type: ignore[arg-type]
        status.running = True
        status.run_id = run["id"]
        before = logbuffer.RING.last_seq()
        try:
            resp = armed_client.post(
                f"/api/runs/{run['id']}/execute",
                json={"confirmation_phrase": run["confirmation_phrase"]},
            )
        finally:
            status.running = False
            status.run_id = None

        assert resp.status_code == 409
        refusals = [
            line
            for line in logbuffer.RING.since(before, limit=logbuffer.RING_SIZE)
            if "reap.refused" in line.text
        ]
        assert len(refusals) == 1
        assert "409" in refusals[0].text

    def test_a_stop_refused_because_the_run_ended_says_so_in_the_log(
        self, armed_client: TestClient
    ) -> None:
        """The sibling of the refusal above (rule 72): the success path logged and the
        refusal did not, so "I pressed Stop and it kept going" left the same nothing."""
        run = armed_client.post("/api/runs").json()
        before = logbuffer.RING.last_seq()

        resp = armed_client.post(f"/api/runs/{run['id']}/stop")

        assert resp.status_code == 409
        refusals = [
            line
            for line in logbuffer.RING.since(before, limit=logbuffer.RING_SIZE)
            if "reap.stop_refused" in line.text
        ]
        assert len(refusals) == 1

    def test_a_non_http_failure_starting_a_reap_releases_the_slot(
        self, armed_client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A non-HTTP failure between claiming the single reap slot and starting the task must
        release the slot. Catching only HTTPException would wedge the one deletion endpoint at
        a permanent 409 'already running' until restart. Fails closed (nothing deleted), but it
        must never get permanently stuck."""
        from reaper.api import runs as runs_module

        async def _boom(*_a: object, **_k: object) -> object:
            raise RuntimeError("clients unavailable")

        monkeypatch.setattr(runs_module, "build_reap_gateway", _boom)
        run = armed_client.post("/api/runs").json()
        body = {"confirmation_phrase": run["confirmation_phrase"]}

        with pytest.raises(RuntimeError):
            armed_client.post(f"/api/runs/{run['id']}/execute", json=body)

        # The slot is released, not wedged: the status reads idle, and a retry fails the SAME
        # way (a RuntimeError, having passed the slot guard), not a spurious 409 'already
        # running' that would prove the slot stayed claimed.
        assert armed_client.get("/api/runs/execute/status").json()["running"] is False
        with pytest.raises(RuntimeError):
            armed_client.post(f"/api/runs/{run['id']}/execute", json=body)


class TestTheProfileControlsTheCaps:
    """The reap caps are the owner's decision, read from the profile -- not a hardcoded
    default. This is what lets a real condemned set be simulated at all."""

    def test_it_opens_on_cautious_defaults(self, client: TestClient) -> None:
        body = client.get("/api/profile").json()
        assert body["max_items_per_run"] == 10  # the cautious built-in
        assert body["caps_enabled"] is True

    def test_settings_round_trip_and_persist(self, client: TestClient) -> None:
        current = client.get("/api/profile").json()
        current["max_items_per_run"] = 25
        current["grace_days"] = 21

        saved = client.put("/api/profile", json=current)
        assert saved.status_code == 200
        assert saved.json()["max_items_per_run"] == 25

        # Read back in a fresh request -- it was persisted, not just echoed.
        assert client.get("/api/profile").json()["grace_days"] == 21

    def test_the_dry_run_uses_the_saved_cap(self, client: TestClient) -> None:
        """The executor's cap must come from the profile. With one condemned item and a
        cap of 1, the dry run completes -- proving the saved cap is what it obeys (a
        hardcoded larger default would also pass, so the abort case is covered by the
        executor's own unit tests, where a multi-item plan can be built)."""
        settings = client.get("/api/profile").json()
        settings["max_items_per_run"] = 1
        settings["max_items_per_30d"] = 1
        client.put("/api/profile", json=settings)

        run = client.post("/api/runs").json()
        report = client.post(f"/api/runs/{run['id']}/dry-run").json()
        assert report["state"] == "completed"  # 1 item, cap 1

    def test_an_invalid_cap_combination_is_a_422(self, client: TestClient) -> None:
        """A per-run cap above the rolling 30-day cap makes the rolling cap meaningless.
        The domain refuses it, with the reason -- not a silent clamp."""
        settings = client.get("/api/profile").json()
        settings["max_items_per_run"] = 500
        settings["max_items_per_30d"] = 100  # smaller than per-run: nonsensical

        response = client.put("/api/profile", json=settings)
        assert response.status_code == 422

    def test_a_grace_period_under_a_week_is_refused(self, client: TestClient) -> None:
        settings = client.get("/api/profile").json()
        settings["grace_days"] = 3
        assert client.put("/api/profile", json=settings).status_code == 422


class TestLimitsNobodySavedDoNotBoundAReap:
    """``active_profile`` never raises on purpose: the settings page an operator would use
    to repair a broken blob reads the same function. It falls back to the SHIPPED DEFAULTS
    and flags it, and those defaults can be LOOSER than what was saved (a run cap of 5
    becomes 10, a grace of 30 becomes 14). Every route that acted on the numbers read them
    through ``active_profile_settings``, which drops the flag, so a profile that stopped
    validating left the deleting route bounded by numbers nobody chose and said nothing.
    The scan path already refuses in this state (rules 65/91); these are the rest."""

    @staticmethod
    def _break_the_saved_limits(tmp_path: Path) -> None:
        """Corrupt the stored blob out of band, which is how this really happens: a value
        that stopped validating across an upgrade, or a hand-edited row."""
        settings = Settings(data_dir=tmp_path, secret_key="k")
        engine = sa_create_engine(settings.sync_database_url)
        with Session(engine) as session:
            row = session.execute(select(Profile).order_by(Profile.id.asc()).limit(1)).scalar_one()
            row.settings_json = "not json at all"
            session.commit()
        engine.dispose()

    def test_planning_and_previewing_refuse(self, client: TestClient, tmp_path: Path) -> None:
        """Planning is covered as well as previewing because a plan built under the wrong
        allowance is the one an operator then approves."""
        run = client.post("/api/runs").json()  # planned while the profile still read
        saved = client.get("/api/profile").json()
        assert client.put("/api/profile", json=saved).status_code == 200
        self._break_the_saved_limits(tmp_path)

        assert client.post("/api/runs").status_code == 409
        assert client.post(f"/api/runs/{run['id']}/dry-run").status_code == 409

    def test_the_reap_itself_refuses(self, armed_client: TestClient, tmp_path: Path) -> None:
        """The one that counts. Armed, with the right phrase for a plan built while the
        profile still read, so nothing else in the gate chain can be what refuses."""
        run = armed_client.post("/api/runs").json()
        saved = armed_client.get("/api/profile").json()
        assert armed_client.put("/api/profile", json=saved).status_code == 200
        self._break_the_saved_limits(tmp_path)

        reaped = armed_client.post(
            f"/api/runs/{run['id']}/execute",
            json={"confirmation_phrase": run["confirmation_phrase"]},
        )

        assert reaped.status_code == 409
        assert "limits you saved" in reaped.json()["detail"]

    def test_the_refusal_says_what_to_fix(self, client: TestClient, tmp_path: Path) -> None:
        """Rule 21. The operator has to be able to act on this without reading the code, so
        it names the page and the section, not the field that failed to validate."""
        saved = client.get("/api/profile").json()
        client.put("/api/profile", json=saved)
        self._break_the_saved_limits(tmp_path)

        detail = client.post("/api/runs").json()["detail"]
        assert "couldn't read the limits you saved" in detail
        assert "Pace and limits" in detail

    def test_the_run_list_still_renders(self, client: TestClient, tmp_path: Path) -> None:
        """The deliberate exception. Listing shows numbers, it does not act on them, and
        taking out the page an operator reads to see what is pending would be the wrong
        blast radius for a fault the deleting route already refuses."""
        client.post("/api/runs")
        saved = client.get("/api/profile").json()
        client.put("/api/profile", json=saved)
        self._break_the_saved_limits(tmp_path)

        assert client.get("/api/runs").status_code == 200


class TestSnapshot:
    def test_it_reports_the_split_and_the_reclaimable_bytes(self, client: TestClient) -> None:
        body = client.get("/api/snapshots/latest").json()

        assert body["condemned"] == 1
        assert body["protected"] == 1
        assert body["abstained"] == 2
        assert body["reclaimable_bytes"] == 5_900_000_000  # condemned only

    def test_the_horizon_is_exposed(self, client: TestClient) -> None:
        """Media older than this has no watch evidence either way. The owner needs to
        see it -- a fresh Tautulli install would make the whole library look abandoned."""
        assert client.get("/api/snapshots/latest").json()["horizon_at"]


class TestTheWhyPanel:
    def test_a_condemned_item_shows_the_protections_that_did_not_fire(
        self, client: TestClient
    ) -> None:
        """The block no competitor shows. Every protection evaluated, with the ACTUAL
        NUMBERS -- not just which rules matched."""
        candidates = client.get("/api/candidates?verdict=condemn").json()
        detail = client.get(f"/api/candidates/{candidates[0]['id']}").json()

        checked = detail["explanation"]["protections_checked"]
        assert checked
        assert checked[0]["detail"] == CHECKED_DETAIL

    def test_a_protected_item_explains_the_keep(self, client: TestClient) -> None:
        """A tool that only explains its deletions cannot be trusted about its keeps.

        The protected fixture scores 90 -- it would be deleted on score alone -- and
        the panel must say both: why it scored that, and why it is kept anyway."""
        protected = client.get("/api/candidates?verdict=protect").json()
        detail = client.get(f"/api/candidates/{protected[0]['id']}").json()

        assert detail["verdict"] == "protect"
        assert detail["score"] == 90  # the score it is overriding
        fired = detail["explanation"]["protections_fired"]
        assert fired
        assert "well rated: 8.0 on IMDb from 250,000 votes" in fired[0]["detail"]

    def test_the_grace_clock_is_exposed(self, client: TestClient) -> None:
        candidates = client.get("/api/candidates?verdict=condemn").json()

        assert candidates[0]["first_flagged_at"]

    def test_the_match_block_and_keeps_survive_the_wire(self, client: TestClient) -> None:
        """Regression: the stored explanation always carried ``match`` and ``keeps``, but
        the wire schema did not declare them, so pydantic silently DROPPED both -- and the
        panel's "kept to be safe" notice could never render. Every key the UI reads must
        be named in the schema."""
        abstained = client.get("/api/candidates?verdict=abstain").json()
        detail = client.get(f"/api/candidates/{abstained[0]['id']}").json()

        assert detail["explanation"]["match"]["status"] == "unmatched"
        assert detail["explanation"]["base_score"] == 50.0
        keeps = detail["explanation"]["keeps"]
        assert keeps and keeps[0]["evaluated"] is False

    def test_an_unmatched_item_leads_with_the_plain_cause(self, client: TestClient) -> None:
        """The card's one-liner. Three gates each report "could not check X: Plex has not
        matched this item"; the owner should read the shared cause once, in plain words,
        not the first gate's engineer-speak."""
        abstained = client.get("/api/candidates?verdict=abstain").json()

        assert abstained[0]["reason"] == "Kept to be safe: it couldn't be found in Plex."

    def test_a_missing_candidate_is_a_404(self, client: TestClient) -> None:
        assert client.get("/api/candidates/9999").status_code == 404


class TestPanelHeadFields:
    """The panel head's display metadata and jump links, and the card's badge + pill.

    Every field degrades to null/hidden rather than erroring: an old row, an unmatched
    item or a removed instance must never take the panel down with it.
    """

    def test_the_card_carries_the_badge_and_the_dormancy_pill(self, client: TestClient) -> None:
        row = client.get("/api/candidates?verdict=condemn").json()[0]

        assert row["video_resolution"] == "1080"
        assert row["dormant_for"] == "5 years, 7 months"

    def test_a_row_without_the_metadata_hides_both(self, client: TestClient) -> None:
        """The abstained fixture predates the capture (no columns, and its dormancy came
        from an unmatched item) -- both fields must be null, not an error."""
        row = client.get("/api/candidates?verdict=abstain").json()[0]

        assert row["video_resolution"] is None
        # Its explanation says "not watched in ..." but with evaluated=True from the
        # shared helper; the unmatched item's dormancy is still the helper's phrasing, so
        # dormant_for emits. The protect row exercises the same path.
        assert row["dormant_for"] == "5 years, 7 months"

    def test_the_detail_carries_links_ratings_and_the_meta_line(self, client: TestClient) -> None:
        candidates = client.get("/api/candidates?verdict=condemn").json()
        detail = client.get(f"/api/candidates/{candidates[0]['id']}").json()

        assert detail["links"] == {
            "plex": (
                "https://app.plex.tv/desktop/#!/server/abc123"
                "/details?key=%2Flibrary%2Fmetadata%2F555"
            ),
            "tautulli": "https://tautulli.example/info?rating_key=555",
            "seerr": "https://seerr.example/movie/603",
            "radarr": "https://radarr.example/movie/603",
            "sonarr": None,
            "imdb": "https://www.imdb.com/title/tt0000001/",
            "tmdb": "https://www.themoviedb.org/movie/603",
            "rotten_tomatoes": "https://www.rottentomatoes.com/search?search=Example%20Movie",
            "trakt": "https://trakt.tv/search/imdb/tt0000001",
            # Empty on a bound row: these are the rows an ABSTAIN could not choose between.
            "match_candidates": [],
        }
        assert detail["ratings"] == {
            "imdb": 5.9,
            "imdb_votes": 35_072,
            "rt_critic": 77,
            "rt_audience": 71,
            "tmdb": 61,
            "trakt": None,
        }
        assert detail["content_rating"] == "PG-13"
        assert detail["runtime_minutes"] == 95
        assert detail["genres"] == ["Documentary", "Drama"]

    def test_an_external_url_redirects_the_service_links_but_not_plex(
        self, client: TestClient
    ) -> None:
        """A service's external URL, when set, is the address its jump link opens; a service
        left blank still uses its connect address, and Plex (its own web address) is
        untouched."""
        # Radarr and Tautulli get a public address; Seerr is deliberately left blank.
        assert (
            client.put(
                "/api/settings/instances/1",
                json={"external_url": "https://movies.example.com/"},
            ).status_code
            == 200
        )
        assert (
            client.put(
                "/api/settings/instances/2",
                json={"external_url": "https://history.example.com"},
            ).status_code
            == 200
        )

        candidates = client.get("/api/candidates?verdict=condemn").json()
        links = client.get(f"/api/candidates/{candidates[0]['id']}").json()["links"]

        # The link opens at the external address (trailing slash stripped), not base_url.
        assert links["radarr"] == "https://movies.example.com/movie/603"
        assert links["tautulli"] == "https://history.example.com/info?rating_key=555"
        # Seerr had no external URL, so it still uses the connect address.
        assert links["seerr"] == "https://seerr.example/movie/603"
        # Plex keeps its own web address, unaffected by the instance external URLs.
        assert links["plex"].startswith("https://app.plex.tv/desktop/")

    def test_an_unmatched_pre_rescan_row_offers_no_links_and_no_ratings(
        self, client: TestClient
    ) -> None:
        """No rating key -> no Plex/Tautulli link; no tmdb id -> no Radarr link. The
        panel hides them all rather than rendering a broken jump."""
        abstained = client.get("/api/candidates?verdict=abstain").json()
        detail = client.get(f"/api/candidates/{abstained[0]['id']}").json()

        assert detail["links"] == {
            "plex": None,
            "tautulli": None,
            "seerr": None,
            "radarr": None,
            "sonarr": None,
            "imdb": None,
            "tmdb": None,
            # Empty here too, and for a different reason worth keeping apart: this row is
            # UNMATCHED, so Reaper found nothing to offer. An ambiguous or conflicted row
            # has candidates and does offer them (test_display_meta.TestBuildLinks).
            "match_candidates": [],
            # A title always exists, so the RT search still works for an unmatched row.
            "rotten_tomatoes": "https://www.rottentomatoes.com/search?search=Unmatched",
            "trakt": None,
        }
        assert detail["ratings"] is None
        assert detail["content_rating"] is None
        assert detail["runtime_minutes"] is None


class TestPlexWebUrlSetting:
    def test_the_default_is_the_hosted_plex_web(self, client: TestClient) -> None:
        assert client.get("/api/settings/plex").json()["web_url"] == "https://app.plex.tv"

    def test_saving_strips_the_trailing_slash_and_feeds_the_links(self, client: TestClient) -> None:
        saved = client.put("/api/settings/plex", json={"web_url": "https://plex.example/"})
        assert saved.status_code == 200
        assert saved.json()["web_url"] == "https://plex.example"

        candidates = client.get("/api/candidates?verdict=condemn").json()
        detail = client.get(f"/api/candidates/{candidates[0]['id']}").json()
        # A self-hosted address serves the web client under /web, not /desktop (which 403s).
        assert detail["links"]["plex"].startswith("https://plex.example/web#!/server/")

    def test_a_non_http_address_is_refused_in_plain_words(self, client: TestClient) -> None:
        refused = client.put("/api/settings/plex", json={"web_url": "plex.example"})
        assert refused.status_code == 422
        # The sentence names the box and shows the shape. It said "must start with https:// or
        # http://" while the check behind it was a `startswith` pair that let a bare scheme with
        # no host through; both moved to the shared check together (#255, rule 84). The sibling
        # cases live in test_settings_api.py's TestPlexStatus.
        assert "full web address" in refused.json()["detail"]

    def test_clearing_resets_to_the_default(self, client: TestClient) -> None:
        client.put("/api/settings/plex", json={"web_url": "https://plex.example"})
        cleared = client.put("/api/settings/plex", json={"web_url": ""})
        assert cleared.json()["web_url"] == "https://app.plex.tv"


class TestTheSimulator:
    """Re-scores the last snapshot under a candidate policy with ZERO API calls, so the
    knob and its blast radius sit in the same viewport."""

    def _simulate(self, client: TestClient, condemn_at: int) -> dict[str, Any]:
        body: dict[str, Any] = client.post(
            "/api/policy/simulate",
            json={
                "condemn_at": condemn_at,
                "gates": DEFAULT_GATES,
                "signals": DEFAULT_SIGNALS,
            },
        ).json()
        return body

    def test_lowering_the_threshold_condemns_more(self, client: TestClient) -> None:
        strict = self._simulate(client, 95)
        loose = self._simulate(client, 40)

        assert loose["condemned"] > strict["condemned"]

    def test_it_reports_what_a_change_would_newly_condemn(self, client: TestClient) -> None:
        """The number the owner actually needs before saving: not the total, but the
        delta from what they have already reviewed."""
        result = self._simulate(client, 95)  # stricter than the stored 91

        assert result["no_longer_condemned"] == 1
        assert result["newly_condemned"] == 0

    def test_a_protection_wins_at_every_threshold(self, client: TestClient) -> None:
        """The protected fixture scores 90. No threshold, however low, may condemn it
        -- a protection always beats the score."""
        for threshold in (1, 50, 100):
            assert self._simulate(client, threshold)["protected"] == 1

    def test_an_item_below_the_coverage_floor_is_never_condemned(self, client: TestClient) -> None:
        """We can barely see it. Judging it on fragments is how you delete something you
        know nothing about."""
        result = client.post(
            "/api/policy/simulate",
            json={
                "condemn_at": 1,  # would condemn anything
                "coverage_floor_bp": 5000,
                "gates": DEFAULT_GATES,
                "signals": DEFAULT_SIGNALS,
            },
        ).json()

        # The 20%-coverage item stays out, even at a threshold of 1. The two condemned
        # are the full-coverage rows: the stored-condemned one and the clean abstainer.
        assert result["condemned"] == 2

    def test_the_histogram_covers_every_item(self, client: TestClient) -> None:
        result = self._simulate(client, 70)

        assert sum(result["histogram"]) == 4

    def test_a_threshold_only_change_is_exact(self, client: TestClient) -> None:
        """Moving condemn_at re-compares a STORED score against a new number, which
        needs no API call and is not an approximation."""
        assert self._simulate(client, 50)["exact"] is True

    def test_it_names_what_a_change_would_newly_condemn(self, client: TestClient) -> None:
        """A count is abstract; a title the owner recognizes is what stops a bad
        threshold. Dropping the threshold pulls in the cleanly-abstained item -- and the
        example names it. The blocked "Unmatched" row scores higher, yet must not appear:
        its protections could not be checked, so no threshold may condemn it."""
        result = client.post(
            "/api/policy/simulate",
            json=_policy(condemn_at=40, coverage_floor_bp=0),
        ).json()

        assert result["newly_condemned"] == 1
        assert result["examples_newly_condemned"] == [
            {"title": "Example Oldie", "year": None, "score": 45}
        ]

    def test_examples_are_empty_when_nothing_new_is_flagged(self, client: TestClient) -> None:
        result = self._simulate(client, 95)  # stricter than the stored 91: nothing new
        assert result["examples_newly_condemned"] == []

    def test_it_tallies_which_protection_spared_the_kept_items(self, client: TestClient) -> None:
        """The protected fixture was saved by its rating. The simulator says so in
        aggregate, from the same stored explanation the why-panel renders."""
        result = self._simulate(client, 70)
        assert result["protected_by"] == [{"gate": "rating_floor", "count": 1}]

    def test_a_blocked_row_is_never_simulated_as_condemned(self, client: TestClient) -> None:
        """The "Unmatched" fixture abstained because its protections could not be
        checked -- not because of its score (50) or coverage. Even the loosest possible
        draft (threshold 1, no coverage floor) must not count it as a deletion: the scan
        would keep abstaining on it, and a simulator that counts it is telling the owner
        a plausible wrong number at the very moment they pick a threshold."""
        result = client.post(
            "/api/policy/simulate",
            json=_policy(condemn_at=1, coverage_floor_bp=0),
        ).json()

        # The stored-condemned row and the clean abstainer, never the blocked row.
        assert result["condemned"] == 2
        named = [e["title"] for e in result["examples_newly_condemned"]]
        assert "Unmatched" not in named

    def test_a_hand_reaped_row_keeps_its_stored_verdict(self, client: TestClient) -> None:
        """The owner hand-reaped the condemned fixture. A draft threshold above its
        score must NOT report it "no longer condemned": every scan will keep condemning
        it while the override stands, and the simulator must agree with the scan."""
        response = client.post(
            "/api/override", json={"media_key": "radarr:1:10", "decision": "reap"}
        )
        assert response.status_code == 200, response.text

        result = self._simulate(client, 95)  # stricter than the stored 91

        assert result["condemned"] == 1
        assert result["no_longer_condemned"] == 0

    def test_a_hand_reap_on_a_protected_row_counts_as_condemned(self, client: TestClient) -> None:
        """The rating floor is a cautious judgment the owner may overrule: once they
        hand-reap the protected fixture, the simulator must count it condemned at any
        threshold, exactly as the plan and the counts now do (services.condemned). At a
        draft of 95 the stored condemn (91) legitimately drops out, so the hand-reaped
        row is the ONLY deletion left -- and the protected tally goes to zero."""
        response = client.post(
            "/api/override", json={"media_key": "radarr:1:11", "decision": "reap"}
        )
        assert response.status_code == 200, response.text

        result = self._simulate(client, 95)

        assert result["condemned"] == 1  # the hand-reap, pinned at any threshold
        assert result["protected"] == 0
        assert result["no_longer_condemned"] == 1  # the stored 91 still drops at 95

    def test_a_hand_reap_on_an_unmatched_row_is_still_refused(self, client: TestClient) -> None:
        """The "Unmatched" fixture is a row Reaper could not tie to a Plex entry. A hand
        reap does not beat "we do not know WHICH FILE this is": the engine keeps refusing,
        so the simulator must not count it as a deletion either. At a draft of 91 the stored
        condemn still counts, and it must stay the only one.

        Named for the match, because the match is the whole hold. The fixture also carries
        three blocked gates -- an unmatched item has no rating key, so every Plex-dependent
        gate blocks -- and this test used to be named for THOSE, which stopped refusing
        anything when a blocked gate stopped holding a hand reap. Emptying
        `protections_unknown` leaves it green, so under the old name it pinned nothing it
        claimed to.

        **What this test cannot discriminate, said plainly (rule 118).** It passes whether or
        not a blocked gate holds a reap, because its row is held by the match either way. The
        opposite direction -- a row Reaper CAN identify whose protections merely could not be
        checked is the operator's to remove -- needs a fixture with blocked gates and a clean
        match, which this shared snapshot does not have and which cannot be added without
        moving the counts every sibling here asserts. It is covered instead where it can be
        driven directly, with mutation proofs -- in `test_override_truth.py`
        (`test_an_unchecked_protection_no_longer_holds_the_reap`, four gates), in
        `test_verdict_agreement.py`
        (`test_a_reap_override_condemns_past_any_gate_that_could_not_be_checked`), and in
        `test_engine_invariants.py`
        (`test_the_block_stops_every_automatic_path_but_not_the_owners_own_hand`).
        """
        response = client.post(
            "/api/override", json={"media_key": "radarr:1:12", "decision": "reap"}
        )
        assert response.status_code == 200, response.text

        result = self._simulate(client, 91)

        assert result["condemned"] == 1  # only the stored condemn; the unmatched row stays out

    def test_the_exact_threshold_boundary_condemns_at_and_above(self, client: TestClient) -> None:
        """condemn_at is "at or above". The stored 91 must count as condemned at a
        threshold of exactly 91 and drop out at 92 -- the route must decide through the
        same shared function as the scan, so a `>` typo here can never pass."""
        assert self._simulate(client, 91)["condemned"] == 1
        assert self._simulate(client, 92)["condemned"] == 0


class TestASnapshotWithNoFrozenFactsRefusesToGuess:
    """The trap this class exists to close, and the premise it actually rests on.

    The simulator re-decides a snapshot by re-comparing **stored** scores and verdicts
    against new thresholds. That is exact for ``condemn_at`` and ``coverage_floor_bp``.
    It is simply wrong for anything else: change a signal weight or a gate, and every
    stored score was produced by the *old* ones. A policy editor that let you drag a
    weight and then showed a confident count would be the single most dangerous screen in
    the product, because the number would look exactly as authoritative as the true one.

    **This fixture's snapshot froze no Facts**, which is what every case below is really
    driven by: ``api.routes.simulate``'s replay tier needs every governed row to carry a
    ``facts_json``, so a pre-facts-freeze snapshot falls to the refusal whatever the edit
    was. That is a real state -- every snapshot taken before the freeze shipped is in it,
    and it is exactly the state the refusal must survive -- but it is not the same claim as
    "this edit is unanswerable", and the class used to be named and documented as though it
    were. ``test_the_premise`` pins the premise so a fixture that later starts freezing
    Facts fails here rather than quietly turning every case below into a tautology
    (rules 118, 119).

    Which edits are genuinely unanswerable over a snapshot that DID freeze its evidence is
    ``tests/test_simulate_hardening.py``'s question, and a gate is no longer one of them.
    """

    def _simulate(self, client: TestClient, policy: dict[str, Any]) -> dict[str, Any]:
        body: dict[str, Any] = client.post("/api/policy/simulate", json=policy).json()
        return body

    def test_the_premise(self, client: TestClient) -> None:
        """No row here froze its Facts, so the replay tier is unreachable by construction."""
        rows = client.get("/api/candidates?verdict=condemn").json()
        assert rows, "the fixture seeded no candidates, so nothing below is exercised"
        # A threshold-only edit still answers: it never needed the frozen evidence, which is
        # what makes the refusals below about the evidence rather than about the route.
        assert self._simulate(client, _policy())["exact"] is True

    def test_changing_a_signal_weight_refuses_to_report_numbers(self, client: TestClient) -> None:
        result = self._simulate(
            client,
            _policy(
                signals=[
                    # A reallocation, not a reduction: the total stays 100, but the
                    # scoring mix moved, so the stored scores no longer apply.
                    {"signal": "unwatched", "weight": 60, "saturate_at": 1825},
                    {"signal": "few_watchers", "weight": 40, "saturate_at": 3},
                ]
            ),
        )

        assert result["exact"] is False
        assert result["condemned"] == 0
        assert result["reclaimable_bytes"] == 0
        assert sum(result["histogram"]) == 0
        # No examples and no spared-by tally either: stale names would be acted on
        # exactly like stale counts.
        assert result["examples_newly_condemned"] == []
        assert result["protected_by"] == []

    def test_the_refusal_names_which_one_it_is_on_the_wire(self, client: TestClient) -> None:
        """The typed half, which is what lets the panel name the control at fault.

        Every test around this one asserts on ``exact`` alone, and ``exact: false`` is the
        same for all three refusals -- so without this the discriminator could be dropped
        from the response and nothing here would notice, while the panel silently fell back
        to the general heading for every cause (#495).

        Driven by the popularity WINDOW, which is the span ``distinct_watchers`` is counted
        over and so the one gate field a scan reads before it freezes an item's facts. It
        replaced a keep-tag edit, which stopped gathering differently when the keep tags left
        the policy body for Settings -> Lists: a list now protects through an ``on_list`` rule
        reading membership the scan gathered whether or not any rule named it.
        """
        widened = [
            {**gate, "window_days": 180} if gate["gate"] == "server_popularity" else gate
            for gate in DEFAULT_GATES
        ]
        result = self._simulate(client, _policy(gates=widened))

        assert result["exact"] is False
        assert result["stale_kind"] == "gathers_differently"
        # Both halves, always: the sentence is what an API reader gets and what the panel
        # renders, so a typed kind with no words behind it is half a refusal.
        assert result["stale_reason"]

    def test_an_exact_answer_carries_no_refusal(self, client: TestClient) -> None:
        """The other direction, so ``stale_kind`` cannot collapse to a constant."""
        result = self._simulate(client, _policy())

        assert result["exact"] is True
        assert result["stale_kind"] is None
        assert result["stale_reason"] is None

    def test_the_refusal_says_what_to_do_about_it(self, client: TestClient) -> None:
        """An error the owner cannot act on is only marginally better than a wrong
        answer."""
        result = self._simulate(
            client,
            _policy(
                signals=[
                    # A reallocation, not a reduction: the total stays 100, but the
                    # scoring mix moved, so the stored scores no longer apply.
                    {"signal": "unwatched", "weight": 60, "saturate_at": 1825},
                    {"signal": "few_watchers", "weight": 40, "saturate_at": 3},
                ]
            ),
        )

        assert "scan" in str(result["stale_reason"]).lower()

    def test_moving_a_gate_threshold_refuses_over_this_snapshot(self, client: TestClient) -> None:
        """Not because a bar edit is unanswerable -- it is answerable, and
        ``test_simulate_hardening.py`` proves it replays -- but because this snapshot has no
        evidence to replay over. That is the state every install was in before the freeze."""
        loosened = [
            {"gate": "min_dormancy", "threshold": 1095},
            {"gate": "rating_floor", "threshold": 60},  # was 75
            {"gate": "server_popularity", "threshold": 3},
        ]
        result = self._simulate(client, _policy(gates=loosened))

        assert result["exact"] is False

    def test_disabling_a_protection_refuses_over_this_snapshot(self, client: TestClient) -> None:
        """The most dangerous edit of all, and the one most likely to be made while
        watching the condemned count. Same reason as above: no frozen Facts here."""
        without = [g for g in DEFAULT_GATES if g["gate"] != "rating_floor"]
        result = self._simulate(client, _policy(gates=without))

        assert result["exact"] is False


class TestVocabularyValues:
    """Seen-value suggestions for the rule editors. Suggestions, never a gate: typing a
    value that is not in the list stays valid, so this endpoint fails open to empty."""

    def test_genres_come_from_the_latest_scan_most_common_first(self, client: TestClient) -> None:
        body = client.get("/api/vocabulary/values", params={"field": "genre"}).json()

        # Drama is on two candidates, Documentary on one.
        assert body == {"field": "genre", "values": ["Drama", "Documentary"]}

    def test_quality_values_break_frequency_ties_alphabetically(self, client: TestClient) -> None:
        body = client.get("/api/vocabulary/values", params={"field": "quality"}).json()

        assert body["values"] == ["Bluray-1080p", "WEBDL-1080p"]

    def test_libraries_come_from_the_latest_scan(self, client: TestClient) -> None:
        body = client.get("/api/vocabulary/values", params={"field": "library"}).json()

        assert body["field"] == "library"
        # Both matched movies' libraries; the unmatched row has no library and adds nothing.
        assert set(body["values"]) == {"Movies", "Classics"}

    def test_an_unknown_field_is_empty_not_an_error(self, client: TestClient) -> None:
        """A numeric or unheard-of field has nothing to suggest. That is not a fault --
        the input keeps working with no suggestions at all."""
        response = client.get("/api/vocabulary/values", params={"field": "days_unwatched"})

        assert response.status_code == 200
        assert response.json()["values"] == []


class TestPolicyPersistence:
    """The editor needs something to open on, and saving must actually take effect."""

    def test_it_opens_on_the_built_in_default_before_anything_is_saved(
        self, client: TestClient
    ) -> None:
        body = client.get("/api/policy").json()

        assert body["name"] == "default"
        assert len(body["policy_hash"]) == 64
        assert body["body"]["gates"]

    def test_a_stored_policy_that_no_longer_validates_still_opens_the_editor(
        self, client: TestClient, tmp_path: Path
    ) -> None:
        """The editor is the one page an operator cannot afford to lose.

        ``active_policy`` re-parses stored JSON through ``PolicyBody``, so any validator
        added after a row was written turns GET /api/policy into a 500 and locks them out
        of the very screen that fixes it. Simulated here by writing a body straight to the
        table with weights that do not total 100, which the budget rule refuses.
        """
        stored = json.loads(DEFAULT_MOVIE_POLICY.model_dump_json())
        stored["signals"] = [{"signal": "unwatched", "weight": 42, "saturate_at": 1825, "floor": 0}]
        settings = Settings(data_dir=tmp_path, secret_key="k")
        engine = sa_create_engine(settings.sync_database_url)
        with Session(engine) as session:
            session.add(
                PolicyModel(
                    name="stale",
                    media_type="movie",
                    body_json=json.dumps(stored),
                    policy_hash="0" * 64,
                    created_at=utcnow(),
                )
            )
            session.commit()
        engine.dispose()

        response = client.get("/api/policy")

        assert response.status_code == 200
        out = response.json()
        # Their own policy, rescaled -- not the shipped default, which would silently
        # replace tuning they chose with numbers they never picked.
        assert out["name"] == "stale"
        assert sum(s["weight"] for s in out["body"]["signals"]) == 100
        # ...and handed over as an unsaved draft, so nothing is written until they look.
        # The list is what the editor counts to open dirty, so an empty one here is the
        # limbo #516 was: a degraded scan pointing at a page holding no Save.
        assert out["repairs"] == ["rescaled"]

    def test_a_stored_policy_we_cannot_repair_falls_back_and_says_so(
        self, client: TestClient, tmp_path: Path
    ) -> None:
        """Rescaling only fixes the budget. Anything else unreadable opens on the default,
        which must announce itself: a silent default reads as "this is what you configured"
        and is the one way this fallback could cause a deletion nobody chose."""
        settings = Settings(data_dir=tmp_path, secret_key="k")
        engine = sa_create_engine(settings.sync_database_url)
        with Session(engine) as session:
            session.add(
                PolicyModel(
                    name="broken",
                    media_type="movie",
                    body_json=json.dumps({"condemn_at": "not a number"}),
                    policy_hash="0" * 64,
                    created_at=utcnow(),
                )
            )
            session.commit()
        engine.dispose()

        out = client.get("/api/policy").json()

        assert out["name"] == "default"
        assert out["repairs"] == ["fell_back"]

    def test_a_saved_policy_is_what_loads_next(self, client: TestClient) -> None:
        client.post("/api/policy", json=_policy(condemn_at=55, name="mine"))

        body = client.get("/api/policy").json()

        assert body["name"] == "mine"
        assert body["body"]["condemn_at"] == 55

    def test_saving_a_gate_no_policy_can_build_is_refused_in_plain_language(
        self, client: TestClient
    ) -> None:
        """``GateId`` is wider than what a policy row can build: it also carries retired ids
        and ids the engine emits on its own. Storing one used to succeed and then break every
        subsequent scan at ``build_gates``, with no self-heal and nothing naming the save that
        did it. It is refused at the boundary now, and the refusal has to be readable: the SPA
        renders ``detail[].msg`` verbatim, so pydantic's "Value error," prefix would land in
        front of the sentence (rule 21).
        """
        bad = [*DEFAULT_GATES, {"gate": "season_progression", "enabled": True}]

        response = client.post("/api/policy", json=_policy(gates=bad))

        assert response.status_code == 422
        message = " ".join(d["msg"] for d in response.json()["detail"])
        assert "season_progression" in message
        assert "Value error" not in message

        # And the stored policy is untouched, so the install still scans.
        assert client.get("/api/policy").status_code == 200

    def test_the_still_downloading_toggle_survives_a_save(self, client: TestClient) -> None:
        """protect_incomplete_seasons rides the whole PolicyIn -> PolicyBody -> PolicyIn path.
        A non-default value must load back exactly, or the toggle silently resets on reload."""
        client.post("/api/policy", json=_policy(protect_incomplete_seasons=False))

        body = client.get("/api/policy").json()["body"]

        assert body["protect_incomplete_seasons"] is False

    def test_saving_is_append_only_and_idempotent(self, client: TestClient) -> None:
        """The hash is the identity. Opening the editor and saving without changing
        anything must not fork the audit trail -- snapshots and approvals point at
        these rows and have to stay interpretable."""
        first = client.post("/api/policy", json=_policy(condemn_at=55)).json()
        second = client.post("/api/policy", json=_policy(condemn_at=55)).json()

        assert first["policy_hash"] == second["policy_hash"]

    def test_reverting_to_an_earlier_policy_takes_effect(self, client: TestClient) -> None:
        """Save A, then B, then A again: the last save must put A back in force.

        The duplicate-save check used to match A's *older* row anywhere in history and
        skip the write -- 200, reverted body in the response, and B still active. Only
        re-saving the policy currently in force may no-op; a revert is a real change."""
        saved_a = client.post("/api/policy", json=_policy(condemn_at=55)).json()
        saved_b = client.post("/api/policy", json=_policy(condemn_at=80)).json()
        assert saved_b["policy_hash"] != saved_a["policy_hash"]

        reverted = client.post("/api/policy", json=_policy(condemn_at=55))

        assert reverted.status_code == 200, reverted.text
        assert reverted.json()["policy_hash"] == saved_a["policy_hash"]

        active = client.get("/api/policy").json()
        assert active["policy_hash"] == saved_a["policy_hash"]
        assert active["body"]["condemn_at"] == 55

    def test_an_invalid_policy_is_never_persisted(self, client: TestClient) -> None:
        """A dormancy floor under 5 days is refused by the domain, and the refusal must
        happen before the row is written -- not after."""
        response = client.post("/api/policy", json=_policy(gates=[{"gate": "min_dormancy"}]))

        assert response.status_code == 422
        assert client.get("/api/policy").json()["name"] == "default"  # nothing saved

    def test_a_protect_condition_round_trips(self, client: TestClient) -> None:
        """A user-authored protection saves, comes back intact, and changes the policy hash."""
        cond = {"field": "imdb_votes", "op": "gte", "value": 1_000_000}
        before = client.get("/api/policy").json()["policy_hash"]
        saved = client.post("/api/policy", json=_policy(protect_conditions=[cond]))
        assert saved.status_code == 200, saved.text
        assert saved.json()["body"]["protect_conditions"] == [cond]
        assert saved.json()["policy_hash"] != before  # it changes what Reaper decides

    def test_a_protect_condition_with_a_bad_operator_is_refused(self, client: TestClient) -> None:
        # "whitelisted" is a yes/no field -- ">=" is not one of its operators, so this
        # condition is unconstructable, not merely wrong.
        body = _policy(protect_conditions=[{"field": "whitelisted", "op": "gte", "value": True}])
        assert client.post("/api/policy", json=body).status_code == 422

    def test_a_condition_value_of_the_wrong_type_is_a_422_not_a_saved_landmine(
        self, client: TestClient
    ) -> None:
        """A JSON string on a numeric field used to save and hash cleanly, then crash
        every subsequent scan inside the judge. It must be refused at save time, naming
        the field, and nothing may be persisted."""
        body = _policy(protect_conditions=[{"field": "size_bytes", "op": "gte", "value": "500"}])
        response = client.post("/api/policy", json=body)

        assert response.status_code == 422
        assert "whole number" in response.text
        assert client.get("/api/policy").json()["name"] == "default"  # nothing saved

    def test_a_custom_condemn_rule_with_a_string_value_is_refused_too(
        self, client: TestClient
    ) -> None:
        rule = {
            "kind": "boolean",
            "name": "big files",
            "field": "size_bytes",
            "op": "gte",
            "value": "5000000000",
            "weight": 20,
        }
        assert client.post("/api/policy", json=_policy(custom_condemn=[rule])).status_code == 422


_TV_REQUESTED_ONLY = {
    "media_type": "tv",
    "condemn_at": 70,
    "keep_last_seasons": 2,
    "keep_last_scope": "requested",
    "gates": DEFAULT_GATES,
    "signals": DEFAULT_SIGNALS,
}


class TestRequestedOnlyScopeNeedsSeerr:
    """The editor calls /policy/validate as you type, so that is where this warning has
    to land. It is the one warning that depends on something outside the policy: whether
    a Seerr is connected to answer "was this show requested?".

    The client fixture seeds an enabled Seerr, so the connected case is the default here.
    """

    def _scope_warnings(self, client: TestClient) -> list[dict[str, str]]:
        body = client.post("/api/policy/validate", json=_TV_REQUESTED_ONLY).json()
        return [w for w in body["warnings"] if w["field"] == "keep_last_scope"]

    def test_it_is_quiet_while_a_seerr_is_connected(self, client: TestClient) -> None:
        """The scope does exactly what it says, so there is nothing to report."""
        assert self._scope_warnings(client) == []

    def test_it_warns_once_the_seerr_is_switched_off(
        self, client: TestClient, tmp_path: Path
    ) -> None:
        """A disabled Seerr is not one Reaper can ask, so the floor silently covers every
        show. Switching it off, rather than deleting the row, is the case a configured-vs-
        usable mix-up would miss."""
        settings = Settings(data_dir=tmp_path, secret_key="k")
        engine = sa_create_engine(settings.sync_database_url)
        with Session(engine) as session:
            seerr = session.query(Instance).filter(Instance.kind == InstanceKind.SEERR).one()
            seerr.enabled = False
            session.commit()
        engine.dispose()

        flagged = self._scope_warnings(client)
        assert len(flagged) == 1
        assert flagged[0]["severity"] == "warn"
        assert "Seerr" in flagged[0]["message"]


class TestAPopularityWindowLongerThanTheWatchHistory:
    """The other end of the same window, and the wiring that carries it.

    The engine's half is pinned in ``test_policy``. What this pins is that a route
    actually READS the mirror and hands the reach to ``inspect`` -- the warning is dead
    the moment a ``_policy_out`` call site forgets to pass it, and every assertion in
    ``test_policy`` would stay green through that.
    """

    def _seed_mirror(self, tmp_path: Path, days_back: int) -> None:
        """One play ``days_back`` ago, which is exactly how far the mirror then reaches."""
        conn = sqlite3.connect(tmp_path / "cache.db")
        try:
            conn.executescript(SCHEMA)
            conn.execute(
                "INSERT INTO watch_event (rating_key, user_id, watched_at, watched_status, "
                "percent_complete, media_type) VALUES (1, 1, ?, 1.0, 100, 'movie')",
                (int((utcnow() - timedelta(days=days_back)).timestamp()),),
            )
            conn.commit()
        finally:
            # `with sqlite3.connect(...)` commits but never closes the connection, so the
            # handle leaked to teardown as a ResourceWarning (#541).
            conn.close()

    def _policy_body(self, **overrides: object) -> dict[str, Any]:
        """``DEFAULT_GATES`` with the dormancy floor lowered beneath the mirror seeded here.

        ``inspect`` stays silent while the floor alone empties the list, because there the
        popularity window decides nothing and lowering it is inert advice. So a fixture on
        the shipped 1095-day floor would assert nothing about the wiring: it would read as
        green with the reach never fetched at all (rule 141).
        """
        gates = [
            {**g, "threshold": 30} if g["gate"] == "min_dormancy" else g for g in DEFAULT_GATES
        ]
        return _policy(gates=gates, **overrides)  # type: ignore[arg-type]

    def _window_warnings(self, client: TestClient) -> list[dict[str, str]]:
        # The server_popularity gate takes the default 365-day window.
        body = client.post("/api/policy/validate", json=self._policy_body()).json()
        return [w for w in body["warnings"] if w["field"] == "gates.server_popularity.window_days"]

    def test_a_mirror_shorter_than_the_window_is_flagged(
        self, client: TestClient, tmp_path: Path
    ) -> None:
        self._seed_mirror(tmp_path, days_back=90)

        flagged = self._window_warnings(client)

        assert len(flagged) == 1
        assert flagged[0]["severity"] == "warn"
        assert "Nothing will be flagged for removal" in flagged[0]["message"]

    def test_the_shipped_dormancy_floor_moves_the_warning_to_the_floor(
        self, client: TestClient, tmp_path: Path
    ) -> None:
        """The same short mirror, against the gates Reaper actually ships.

        ``min_dormancy`` holds every item at 1095 days and dormancy is clamped to the
        mirror, so on a 90-day history nothing can be condemned whatever the popularity
        window says. Warning on the WINDOW would send the operator to shorten a keep
        protection that changes nothing, so that anchor stays silent.

        This used to say the detector said nothing at all, and that was the defect: the page
        went quietest exactly where the list was emptiest (#217). The floor speaks for itself
        now, on its own control, and it is the only thing on the page here -- which is also
        what proves the two cannot stack, since one fires on the negation of the other.
        """
        self._seed_mirror(tmp_path, days_back=90)

        body = client.post("/api/policy/validate", json=_policy()).json()

        assert [
            w for w in body["warnings"] if w["field"] == "gates.server_popularity.window_days"
        ] == []
        [floor] = [w for w in body["warnings"] if w["field"] == "gates.min_dormancy.threshold"]
        assert floor["severity"] == "warn"
        assert "Nothing will be flagged for removal" in floor["message"]
        # Through the route, so this pins the reach actually reaching `inspect` here too: with
        # the mirror unread the branch cannot fire at all (rule 141).
        assert "3 years" in floor["message"]

    def test_a_mirror_that_covers_the_window_is_quiet(
        self, client: TestClient, tmp_path: Path
    ) -> None:
        self._seed_mirror(tmp_path, days_back=800)

        assert self._window_warnings(client) == []

    def test_an_empty_mirror_says_nothing(self, client: TestClient) -> None:
        """A first run has no history at all, so there is no reach to compare and nothing
        the operator could act on yet. Silence, not a warning about a window that may well
        be right by the time they have any history."""
        assert self._window_warnings(client) == []

    def test_the_gate_off_case_answers_on_the_rule_that_blocks(
        self, client: TestClient, tmp_path: Path
    ) -> None:
        """Switching the protection off does not switch off the span it was measured over.

        ``popularity_window_days`` falls back to 365 and ``build_gates`` hands that to the
        operator's own keep-outright rule either way, so the block survives the switch. The
        ANCHOR is the half a route can break on its own: it decides where the SPA renders
        this, and with the gate off the window picker it would otherwise point at is not on
        the page at all.
        """
        self._seed_mirror(tmp_path, days_back=90)
        payload = self._policy_body(
            protect_conditions=[{"field": "recent_watchers", "op": "gte", "value": 1}],
        )
        payload["gates"] = [
            {**g, "enabled": False} if g["gate"] == "server_popularity" else g
            for g in payload["gates"]
        ]

        warnings = client.post("/api/policy/validate", json=payload).json()["warnings"]

        flagged = [w for w in warnings if w["field"] == "protect_conditions"]
        assert len(flagged) == 1
        assert "Nothing will be flagged for removal" in flagged[0]["message"]
        # And nothing on the picker that is not rendered while the gate is off.
        assert [w for w in warnings if w["field"] == "gates.server_popularity.window_days"] == []

    def test_saving_a_policy_answers_with_the_same_warning(
        self, client: TestClient, tmp_path: Path
    ) -> None:
        """All three policy routes share ``_policy_out``, and each passes the reach
        separately, so each needs its own proof."""
        self._seed_mirror(tmp_path, days_back=90)

        body = client.post("/api/policy", json=self._policy_body(condemn_at=71)).json()

        assert [
            w for w in body["warnings"] if w["field"] == "gates.server_popularity.window_days"
        ] != []

    def test_saving_a_policy_that_is_already_in_force_answers_with_it_too(
        self, client: TestClient, tmp_path: Path
    ) -> None:
        """``save_policy`` returns through TWO ``_policy_out`` calls, and the second one is
        easy to miss: a body content-identical to the policy in force writes nothing and
        returns early. Deleting ``history_reach_days=`` from that call site left the whole
        suite green, because the test above only ever reaches the normal return (rule 118).
        """
        self._seed_mirror(tmp_path, days_back=90)
        payload = self._policy_body(condemn_at=71)
        client.post("/api/policy", json=payload)

        body = client.post("/api/policy", json=payload).json()

        assert [
            w for w in body["warnings"] if w["field"] == "gates.server_popularity.window_days"
        ] != []

    def test_reading_the_policy_back_answers_with_it_too(
        self, client: TestClient, tmp_path: Path
    ) -> None:
        self._seed_mirror(tmp_path, days_back=90)
        client.post("/api/policy", json=self._policy_body(condemn_at=71))

        body = client.get("/api/policy").json()

        assert [
            w for w in body["warnings"] if w["field"] == "gates.server_popularity.window_days"
        ] != []


class TestPolicyValidation:
    def test_a_valid_policy_is_hashed(self, client: TestClient) -> None:
        body = client.post(
            "/api/policy/validate",
            json={"condemn_at": 70, "gates": DEFAULT_GATES, "signals": DEFAULT_SIGNALS},
        ).json()

        assert len(body["policy_hash"]) == 64

    def test_a_high_imdb_bar_is_warned_about(self, client: TestClient) -> None:
        """An IMDb bar of 9.6 protects almost nothing. A validator cannot tell that from
        a genuine choice, so it warns instead of pretending to know."""
        body = client.post(
            "/api/policy/validate",
            json={
                "condemn_at": 70,
                "gates": [
                    {"gate": "rating_floor"},
                    {"gate": "min_dormancy", "threshold": 1095},
                ],
                "signals": DEFAULT_SIGNALS,
                "keep_rating_rules": [{"source": "imdb", "floor": 96, "min_votes": 1000}],
            },
        ).json()

        assert any("protect almost nothing" in w["message"] for w in body["warnings"])

    def test_a_zero_vote_floor_is_refused_outright(self, client: TestClient) -> None:
        """Provably wrong, so it is a 422 rather than a warning: an IMDb bar with no
        vote floor protects an 8.3 drawn from 388 votes."""
        response = client.post(
            "/api/policy/validate",
            json={
                "condemn_at": 70,
                "gates": [{"gate": "rating_floor"}],
                "signals": DEFAULT_SIGNALS,
                "keep_rating_rules": [{"source": "imdb", "floor": 75, "min_votes": 0}],
            },
        )

        assert response.status_code == 422
        # ...and the owner is TOLD WHY, not handed an Internal Server Error.
        assert "vote floor of 0" in json.dumps(response.json())


class TestUnknownSizeWarningTracksTheDraft:
    """B-26: the unknown-size warning renders directly beneath the box that sets it, and that
    box shows an UNSAVED value. Every other warning the editor renders describes the draft, so
    this one read the saved profile while sitting under the changed one: raise it and no
    warning appeared until after a save, lower it and the old warning kept naming the old
    number. The editor sends the drafted value; omitting it keeps the stored reading."""

    def _unmeasured(self, client: TestClient, **extra: object) -> list[str]:
        body = client.post(
            "/api/policy/validate",
            json={
                "condemn_at": 70,
                "gates": DEFAULT_GATES,
                "signals": DEFAULT_SIGNALS,
                **extra,
            },
        ).json()
        return [w["message"] for w in body["warnings"] if w["field"] == "max_unmeasured_per_run"]

    def test_the_stored_zero_is_quiet(self, client: TestClient) -> None:
        """The shipped profile keeps every unmeasured item, so there is nothing to warn about."""
        assert self._unmeasured(client) == []

    def test_a_drafted_allowance_warns_before_it_is_saved(self, client: TestClient) -> None:
        (message,) = self._unmeasured(client, draft_max_unmeasured_per_run=5)
        assert "up to 5 items" in message

    def test_drafting_it_back_to_zero_clears_the_warning(self, client: TestClient) -> None:
        """Explicit 0 is a value, not an omission: it must not fall back to the stored one."""
        assert self._unmeasured(client, draft_max_unmeasured_per_run=0) == []

    def test_the_allowance_cannot_be_widened_past_what_a_save_accepts(
        self, client: TestClient
    ) -> None:
        """The bound is on the wire, so a draft cannot talk the checker into describing an
        allowance the profile route would refuse to store."""
        response = client.post(
            "/api/policy/validate",
            json={
                "condemn_at": 70,
                "gates": DEFAULT_GATES,
                "signals": DEFAULT_SIGNALS,
                "draft_max_unmeasured_per_run": 26,
            },
        )
        assert response.status_code == 422


class TestVocabularyIsFilteredServerSide:
    """A protect-only field is never even OFFERED to the condemn editor, so a dangerous
    condition is not merely rejected -- it is unconstructable."""

    def test_the_condemn_lane_hides_protect_only_fields(self, client: TestClient) -> None:
        keys = {f["key"] for f in client.get("/api/vocabulary?lane=condemn").json()["fields"]}

        assert "watchers_all_time" not in keys
        assert "imdb_votes" not in keys
        assert "whitelisted" not in keys

    def test_the_protect_lane_is_a_superset(self, client: TestClient) -> None:
        condemn = {f["key"] for f in client.get("/api/vocabulary?lane=condemn").json()["fields"]}
        protect = {f["key"] for f in client.get("/api/vocabulary?lane=protect").json()["fields"]}

        assert condemn < protect

    def test_every_field_carries_its_units(self, client: TestClient) -> None:
        """A bare number is how a 7.5 rating floor meets a Tomatometer of 96."""
        for field in client.get("/api/vocabulary?lane=protect").json()["fields"]:
            assert field["label"]
            assert field["help_text"]

    def test_a_movie_policy_is_not_offered_a_tv_only_field(self, client: TestClient) -> None:
        """The editor asks with the policy's media type; a TV-only reason ("the show has
        ended") is filtered server-side, so a movie policy cannot even build it."""
        movie = {
            f["key"]
            for f in client.get("/api/vocabulary?lane=condemn&media_type=movie").json()["fields"]
        }
        tv = {
            f["key"]
            for f in client.get("/api/vocabulary?lane=condemn&media_type=tv").json()["fields"]
        }

        assert "show_ended" not in movie
        assert "show_ended" in tv

    def test_omitting_media_type_keeps_every_lane_field(self, client: TestClient) -> None:
        unfiltered = {f["key"] for f in client.get("/api/vocabulary?lane=condemn").json()["fields"]}

        assert "show_ended" in unfiltered


class TestNothingCanDelete:
    def test_there_is_no_execution_route(self, client: TestClient) -> None:
        """Read-only by construction. If this ever fails, a delete path was added and
        every safety property in the engine needs re-examining."""
        from fastapi.routing import APIRoute

        for route in client.app.routes:  # type: ignore[attr-defined]
            if isinstance(route, APIRoute):
                assert route.methods is not None
                assert route.methods <= {"GET", "POST", "HEAD", "OPTIONS"}
                assert "delete" not in route.path.lower()
                assert "execute" not in route.path.lower()
