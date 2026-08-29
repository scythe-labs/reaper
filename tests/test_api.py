# SPDX-License-Identifier: AGPL-3.0-or-later
"""The REST surface.

Almost entirely read-only. The single exception is ``POST /runs/{id}/execute``, which is
gated hard. Deletion must be enabled on the host, and the caller must echo the plan's
content-bound confirmation phrase. Even then, ``GuardedTransport`` refuses any call that was
not journalled first. The tests below exercise those gates. The delete mechanics themselves
live in ``test_reap_loop`` against fakes.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, select
from sqlalchemy import create_engine as sa_create_engine
from sqlalchemy.orm import Session

from reaper import logbuffer
from reaper.api.schemas import ProfileSettingsIO, ReapBreakdownOut, SignalCountOut
from reaper.clock import utcnow
from reaper.config import Settings
from reaper.crypto import SecretBox
from reaper.db.base import Base
from reaper.db.models import (
    ActionStep,
    AppSetting,
    Candidate,
    FirstFlagged,
    Instance,
    InstanceKind,
    ListConfig,
    PlexServer,
    Profile,
    ReapRun,
    RunState,
    Snapshot,
    StepState,
)
from reaper.db.models import Policy as PolicyModel
from reaper.engine.gates import wilson_upper
from reaper.engine.policy import (
    DEFAULT_MOVIE_POLICY,
    DEFAULT_TV_POLICY,
    GateSetting,
    PolicyBody,
    ProfileSettings,
    SignalSetting,
    combine_hashes,
)
from reaper.engine.reason import Reason, from_wire, to_stored
from reaper.main import create_app
from reaper.secrets import resolve_kdf_salt, resolve_old_keys, resolve_secret_key
from reaper.services import retention, run_totals
from reaper.services.history_sync import SCHEMA

from ._auth import login
from ._lists import seeded_fingerprint
from ._reasons import refusal_text
from ._reasons import text as reason_text


def _msg(warning: dict[str, Any]) -> str:
    """A policy-warning dict's composed English, from the real catalog. The test-side twin
    of ``PolicyEditor.tsx``'s ``composeIn("warning", w.reason)``, over the API's wire shape
    (``{"field", "reason": {"k", "p"}, "severity"}``)."""
    return reason_text(from_wire(warning["reason"]), namespace="warning")


# No "whitelisted" row. The gate is retired (list membership protects through ``on_list``
# keep rules), and the save boundary refuses it. test_simulate_hardening pins that.
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

    The simulator refuses to re-decide a snapshot whose scores came from a different set of
    signals and gates, so the fixture has to be self-consistent, exactly as a real snapshot
    is. Movies and TV are scored under separate policies now, so the stored hash is the
    combination of both, movie first, then the default TV policy, since none is saved here.
    """
    movie = PolicyBody(
        condemn_at=70,
        gates=tuple(GateSetting.model_validate(g) for g in DEFAULT_GATES),
        signals=tuple(SignalSetting.model_validate(s) for s in DEFAULT_SIGNALS),
    )
    return combine_hashes(movie.scoring_hash(), DEFAULT_TV_POLICY.scoring_hash())


def _fixture_policy_hash() -> str:
    """The policy hash of the same pair, for the same reason the scoring hash exists.

    The executor refuses to run a plan whose policy hash is not the one in force. An operator
    who tightens their policy after approving a plan must not have it delete the files they
    just protected, so a fixture snapshot has to carry the hash of the policies actually in
    force in that test app (no saved rows, so both defaults). Otherwise every dry run against
    it is, correctly, refused as out of date.
    """
    return combine_hashes(DEFAULT_MOVIE_POLICY.policy_hash(), DEFAULT_TV_POLICY.policy_hash())


# ``MinDormancyGate``'s ABSTAIN branch, verbatim, kept on one line so the guard in
# ``test_repo_hygiene.test_the_documented_checked_example_is_one_a_gate_emits`` can find it in
# the source text. That guard checks this string against the sentence the real gate emits, so
# every other place documenting this example has to match it too.
CHECKED_DETAIL = "Unwatched for 5 years, 7 months, past the 3 years Reaper waits."


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
                    "detail_key": {"k": "signal_unwatched", "p": {"days": 2059}},
                    "evaluated": True,
                }
            ],
            "protections_fired": [],
            "protections_checked": [{"gate": "min_dormancy", "detail": CHECKED_DETAIL}],
            "protections_unknown": [],
        }
    )


def _boot(tmp_path: Path) -> tuple[Settings, Engine, str, datetime]:
    """A fresh throwaway install, migrated, plus the list fingerprint a scan records. Without
    it the simulator refuses, which is right for a snapshot that cannot say and useless here.
    The caller seeds its own rows on the returned engine and commits before handing both to
    :func:`_client`."""
    settings = Settings(data_dir=tmp_path, secret_key="k")
    engine = sa_create_engine(settings.sync_database_url)
    Base.metadata.create_all(engine)
    return settings, engine, seeded_fingerprint(settings), utcnow()


def _client(settings: Settings, engine: Engine) -> Iterator[TestClient]:
    """Close the seeding engine and hand back a client logged in on the same install."""
    engine.dispose()
    with TestClient(create_app(settings)) as c:
        login(c, settings)
        yield c


@pytest.fixture
def client(tmp_path: Path) -> Iterator[TestClient]:
    settings, engine, list_hash, now = _boot(tmp_path)
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
                    # exclusion. The flag is per-instance and off by default. The plan
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
                            # protection is a lowercase fragment with no trailing period, where
                            # a checked one is a whole sentence (``gates.py``, above the
                            # PROTECT return). This string must match one of those two real
                            # shapes, not an invented one.
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
                # A cleanly-abstained item. Every protection was checked (none blocked), full
                # coverage, score simply below the threshold. This is the only kind of
                # abstained row a draft threshold may pull in. The simulator tests lean on
                # the contrast with "Unmatched" above, whose protections could not be
                # checked and which must stay abstained at any threshold.
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
    yield from _client(settings, engine)


class TestTheRunsApi:
    """Building, reviewing and dry-running a plan through the API. Nothing deletes."""

    def test_an_unbounded_selection_is_refused_at_the_edge(self, client: TestClient) -> None:
        """``media_keys`` is the destructive path's list input, and it had no length bound at
        all. An authenticated caller, or a leaked API key (POST /api/runs is in the write
        allow-list), could push an arbitrarily large list straight into a plan build and the
        whitelist queries under it. Note the two are different requests. Omitting the field
        still means the whole condemned set, so the bound never truncates a real reap."""
        refused = client.post("/api/runs", json={"media_keys": ["radarr:1:1"] * 5001})

        assert refused.status_code == 422

    def test_an_oversized_media_key_is_refused_at_the_edge(self, client: TestClient) -> None:
        """The key columns are 100 characters. A multi-megabyte one is not a key. It is a
        payload, and it should never reach a query."""
        long_key = "radarr:1:" + "9" * 200

        refused = client.post("/api/override", json={"media_key": long_key, "decision": "spare"})

        assert refused.status_code == 422, refused.text

    def test_an_unknown_key_is_refused_for_what_reaper_can_actually_know(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The refusal can only claim what still survives, not what actually happened.
        ``services.retention`` keeps only the newest ``KEEP_SNAPSHOTS`` scans, so a scan may
        well have held this item before Reaper discarded the row. The message names the
        window as a count of scans, not a fixed time span, since 30 scans is a month of
        nightly scanning and no fixed time at all on an install scanned by hand.

        The count is patched off its default so an assertion cannot pass against a
        transcribed number. The test has to read the constant the sweep itself honors, since
        a hardcoded number would still pass here even if the real constant changed.
        """
        monkeypatch.setattr(retention, "KEEP_SNAPSHOTS", 7)

        refused = client.post(
            "/api/override", json={"media_key": "radarr:1:never-scanned", "decision": "spare"}
        )

        assert refused.status_code == 404, refused.text
        body = refused.json()
        assert body["code"] == "error.whitelist.unknown_item"
        assert body["params"] == {"keep_scans": 7}
        assert body["detail"] == refusal_text("error.whitelist.unknown_item", keep_scans=7)

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

    def test_a_stored_reason_thaws_as_legacy_prose_or_as_its_code(
        self, client: TestClient, tmp_path: Path
    ) -> None:
        """The journal's reason survives the process that wrote it, decoded by
        `engine.reason.from_stored`. A step's `error` and a run's `aborted_reason` are the
        same shape. A row written before the typed reason format existed holds a bare
        English sentence and thaws as a `legacy` reason. One written since holds
        `to_stored`'s JSON and thaws its code and params. This drives both shapes straight
        into the row, the way a snapshot frozen before that change and every write since
        actually reach it, then reads them back over HTTP through a response built from
        nothing but the database.
        """
        run = client.post("/api/runs").json()
        legacy_text = "Radarr accepted the delete, not confirmed. Reaper could not reach it again."

        def _write(session: Session, *, step_value: str, run_value: str) -> None:
            step = session.execute(
                select(ActionStep).where(ActionStep.run_id == run["id"])
            ).scalar_one()
            run_row = session.get(ReapRun, run["id"])
            assert run_row is not None
            step.state = StepState.FAILED
            step.error = step_value
            run_row.aborted_reason = run_value
            session.commit()

        db_url = Settings(data_dir=tmp_path, secret_key="k").sync_database_url
        engine = sa_create_engine(db_url)
        with Session(engine) as session:
            _write(session, step_value=legacy_text, run_value=legacy_text)
        engine.dispose()

        read_back = client.get(f"/api/runs/{run['id']}").json()
        assert read_back["steps"][0]["state"] == "failed"
        assert read_back["steps"][0]["error_reason"] == {
            "k": "legacy",
            "p": {"text": legacy_text},
        }
        summary = next(r for r in client.get("/api/runs").json()["runs"] if r["id"] == run["id"])
        assert summary["aborted_reason"] == {"k": "legacy", "p": {"text": legacy_text}}

        engine = sa_create_engine(db_url)
        with Session(engine) as session:
            _write(
                session,
                step_value=to_stored(Reason("error.reap.step.no_plex_match")),
                run_value=to_stored(Reason("error.reap.canceled")),
            )
        engine.dispose()

        read_back = client.get(f"/api/runs/{run['id']}").json()
        assert read_back["steps"][0]["error_reason"] == {
            "k": "error.reap.step.no_plex_match",
            "p": None,
        }
        summary = next(r for r in client.get("/api/runs").json()["runs"] if r["id"] == run["id"])
        assert summary["aborted_reason"] == {"k": "error.reap.canceled", "p": None}

    def test_a_step_that_has_not_run_carries_no_reason(self, client: TestClient) -> None:
        """The other direction, so the field above cannot pass by always being populated:
        a freshly planned step has nothing to explain and says nothing."""
        run = client.post("/api/runs").json()
        assert run["steps"][0]["error_reason"] is None

    def test_a_dry_run_walks_the_plan_and_deletes_nothing(self, client: TestClient) -> None:
        run = client.post("/api/runs").json()

        report = client.post(f"/api/runs/{run['id']}/dry-run").json()

        assert report["dry_run"] is True
        assert report["state"] == "completed"
        # Proven, not deleted: the count says what the run would remove, and nothing a
        # check kept, so a practice run can tell the operator both numbers honestly.
        assert report["would_delete_items"] == 1
        assert report["skipped"] == 0
        assert report["outcomes"][0]["state"] == "skipped"
        assert report["outcomes"][0]["is_canary"] is True  # the sole item is ordinal 0
        detail = report["outcomes"][0]["detail_reason"]
        assert detail["k"] == "error.reap.step.dry_run"
        assert "DELETE /api/v3/movie/10" in detail["p"]["plan"]

    def test_a_plan_appears_in_the_run_list(self, client: TestClient) -> None:
        created = client.post("/api/runs").json()
        listed = client.get("/api/runs").json()
        assert any(r["id"] == created["id"] for r in listed["runs"])

    def test_the_run_list_limit_is_bounded_at_both_ends(self, client: TestClient) -> None:
        """``?limit=-1`` rendered as SQL `LIMIT -1`, which SQLite reads as no limit at all.

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

    def test_the_run_list_offset_pages_the_whole_history(self, client: TestClient) -> None:
        """Nothing bounds how many runs a long-lived install has executed, unlike a
        scan's 30-snapshot retention, so the history needs a way past the first page."""
        first = client.post("/api/runs").json()
        second = client.post("/api/runs").json()

        assert client.get("/api/runs", params={"offset": -1}).status_code == 422
        page = client.get("/api/runs", params={"limit": 1, "offset": 0}).json()
        assert [r["id"] for r in page["runs"]] == [second["id"]]  # newest first
        assert page["total"] == 2
        next_page = client.get("/api/runs", params={"limit": 1, "offset": 1}).json()
        assert [r["id"] for r in next_page["runs"]] == [first["id"]]
        assert next_page["total"] == 2

    def test_the_run_list_carries_only_what_is_stored(self, client: TestClient) -> None:
        """The history is stored rows, nothing derived.

        Deriving a run's counts, totals, and phrase means re-reading the whitelist, the
        profile, and the whole condemned set of that run's snapshot, per run. A list of fifty
        would read the same thousands of rows fifty times on every visit to the Reap page.
        Deriving it live would also be dishonest: a finished run's phrase would be recomputed
        against today's overrides, describing a plan nobody ever approved. Opening a run goes
        to the detail route instead, which derives them for that one.
        """
        client.post("/api/runs")
        row = client.get("/api/runs").json()["runs"][0]
        assert set(row) == {
            "id",
            "state",
            "approved_at",
            "finished_at",
            "aborted_reason",
            "deleted_items",
            "deleted_bytes",
            "deleted_unmeasured",
            "skipped",
        }
        # A freshly planned run has not reached a terminal state, so its totals are
        # unknown, never zero.
        assert row["finished_at"] is None
        assert row["deleted_items"] is None
        # ...and the detail route still answers with the whole thing.
        full = client.get(f"/api/runs/{row['id']}").json()
        assert full["confirmation_phrase"].startswith("REAP ")
        assert isinstance(full["steps"], list)

    def test_executed_only_excludes_a_planned_run_from_rows_and_total(
        self, client: TestClient, tmp_path: Path
    ) -> None:
        """A plan built and never executed (the head Reap button, a standalone practice
        run) is not a past reap, so the history view's paging and its "Showing N of M"
        count must agree on what to leave out: filtering only the page, after the fact,
        would leave the two disagreeing."""
        planned = client.post("/api/runs").json()
        executed = client.post("/api/runs").json()

        db_url = Settings(data_dir=tmp_path, secret_key="k").sync_database_url
        engine = sa_create_engine(db_url)
        with Session(engine) as session:
            run_row = session.get(ReapRun, executed["id"])
            assert run_row is not None
            run_row.state = RunState.COMPLETED
            session.commit()
        engine.dispose()

        everything = client.get("/api/runs").json()
        assert {r["id"] for r in everything["runs"]} == {planned["id"], executed["id"]}
        assert everything["total"] == 2

        executed_only = client.get("/api/runs", params={"executed_only": True}).json()
        assert {r["id"] for r in executed_only["runs"]} == {executed["id"]}
        assert executed_only["total"] == 1

    def test_a_missing_runs_outcomes_are_a_404(self, client: TestClient) -> None:
        assert client.get("/api/runs/9999/outcomes").status_code == 404

    def test_a_freshly_planned_runs_outcomes_are_empty(self, client: TestClient) -> None:
        """Nothing has been decided for a plan that has not been dry-run or executed, so
        the read that answers a run 'so far' has nothing to report yet, never a row for
        a step that is merely PENDING."""
        run = client.post("/api/runs").json()

        body = client.get(f"/api/runs/{run['id']}/outcomes").json()

        assert body == {"outcomes": [], "outcome_count": 0, "offset": 0}

    def test_the_outcomes_page_is_bounded_at_both_ends(self, client: TestClient) -> None:
        run = client.post("/api/runs").json()
        assert (
            client.get(f"/api/runs/{run['id']}/outcomes", params={"offset": -1}).status_code == 422
        )
        assert client.get(f"/api/runs/{run['id']}/outcomes", params={"limit": 0}).status_code == 422
        assert (
            client.get(f"/api/runs/{run['id']}/outcomes", params={"limit": 501}).status_code == 422
        )
        assert (
            client.get(f"/api/runs/{run['id']}/outcomes", params={"limit": 500}).status_code == 200
        )

    def test_an_outcomes_row_says_whether_the_file_was_removed(
        self, client: TestClient, tmp_path: Path
    ) -> None:
        """A FAILED step whose delete landed before the failure (the exclusion poll, the
        Plex refresh) carries the journal's durable stamp onto the wire, so the browser
        can say "removed" for it instead of telling the operator a file that is off disk
        was kept."""
        run = client.post("/api/runs").json()

        db_url = Settings(data_dir=tmp_path, secret_key="k").sync_database_url
        engine = sa_create_engine(db_url)
        with Session(engine) as session:
            step = session.scalars(
                select(ActionStep)
                .where(
                    ActionStep.run_id == run["id"],
                    ActionStep.kind.in_(run_totals.TERMINAL_DELETE_KINDS),
                )
                .order_by(ActionStep.ordinal, ActionStep.id)
            ).first()
            assert step is not None, "the plan fixture planned nothing, so nothing is exercised"
            step.state = StepState.FAILED
            session.commit()

            unstamped = client.get(f"/api/runs/{run['id']}/outcomes").json()["outcomes"][0]
            assert unstamped["state"] == "failed"
            assert unstamped["file_removed"] is False

            step.file_removed_at = utcnow()
            session.commit()
        engine.dispose()

        stamped = client.get(f"/api/runs/{run['id']}/outcomes").json()["outcomes"][0]
        assert stamped["state"] == "failed"
        assert stamped["file_removed"] is True

    def test_dry_running_a_missing_run_is_a_404(self, client: TestClient) -> None:
        assert client.post("/api/runs/9999/dry-run").status_code == 404

    def test_execute_is_refused_while_deletion_is_off(self, client: TestClient) -> None:
        """The default client is read-only. Even the correct confirmation phrase cannot
        execute a real reap while deletion is disabled. The arm gate comes first."""
        run = client.post("/api/runs").json()
        resp = client.post(
            f"/api/runs/{run['id']}/execute",
            json={"confirmation_phrase": run["confirmation_phrase"]},
        )
        assert resp.status_code == 403
        assert resp.json()["code"] == "error.safety.deletion_off"


@pytest.fixture
def selection_client(tmp_path: Path) -> Iterator[TestClient]:
    """A client whose snapshot holds three condemned movies and one protected one.

    The main ``client`` fixture has a single condemned item, which makes "planned just the
    one I asked for" and "planned the whole set" the same number. The selection could not be
    told from the fall-through that way. Three is the smallest count that distinguishes them.
    """
    settings, engine, list_hash, now = _boot(tmp_path)
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
    yield from _client(settings, engine)


class TestTheRunSelectionIsExplicit:
    """``POST /api/runs`` is where "nothing selected" is translated for the planner.

    This one ternary decides that: an omitted ``media_keys`` means the whole condemned set,
    an explicit ``[]`` means nothing, and the two must never collapse into each other. ``[]``
    is falsy, so the obvious-looking ``if payload and payload.media_keys`` turns a
    select-nothing request into a plan over every condemned item in the library, which is the
    deletion this codebase exists to prevent. The planner's own side of the contract is
    covered directly (test_review_reap), but this route is the only place the empty list is
    ever translated, so it is tested here at the edge.
    """

    def test_an_empty_selection_plans_nothing_rather_than_everything(
        self, selection_client: TestClient
    ) -> None:
        """The one that catches the falsy-collapse rewrite. It must refuse, not expand."""
        refused = selection_client.post("/api/runs", json={"media_keys": []})

        assert refused.status_code == 422
        body = refused.json()
        # ``PlanError`` is a ``Refusal`` subclass of its own. The code names the condition
        # directly, with no params, rather than wrapping planner.py's English in a generic
        # pass-through.
        assert body["code"] == "error.plan.selection_empty"
        assert "no items were selected" in body["detail"].lower()
        # Nothing was journalled. A refused selection leaves no plan behind at all.
        assert selection_client.get("/api/runs").json()["runs"] == []

    def test_an_omitted_selection_plans_the_whole_condemned_set(
        self, selection_client: TestClient
    ) -> None:
        """A body-less POST, and an explicit ``null``, both mean "reap everything condemned."
        This is the distinction the empty list above must not be folded into."""
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
        assert selection_client.get("/api/runs").json()["runs"] == []


@pytest.fixture
def armed_client(tmp_path: Path) -> Iterator[TestClient]:
    """A client armed at the host default (``destructive_actions_enabled=True``), with one
    condemned movie. The movie's Radarr is configured so ``build_plan`` can resolve it (a plan
    refuses a movie whose Radarr is gone), but no Plex or Tautulli is, so the execute
    endpoint's client-presence gate still refuses before any live service is touched. Enough
    to exercise the confirmation and client-presence gates without any live service."""
    settings = Settings(data_dir=tmp_path, secret_key="k", destructive_actions_enabled=True)
    # The box the app decrypts the Radarr key with at execute time. Built exactly as main.py
    # builds it, off the same settings, so the api_key below opens under app.state.secret_box.
    # resolve_kdf_salt mints the per-install salt here, and create_app reads the same one.
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
        assert resp.json()["code"] == "error.runs.confirmation_mismatch"

    def test_the_right_phrase_but_no_plex_is_refused_before_any_delete(
        self, armed_client: TestClient
    ) -> None:
        """Armed and the phrase matches, but with no Plex configured the streaming veto
        cannot run, so the executor refuses rather than deleting blind. This proves the happy
        path reaches the executor and stops at the right interlock."""
        run = armed_client.post("/api/runs").json()
        resp = armed_client.post(
            f"/api/runs/{run['id']}/execute",
            json={"confirmation_phrase": run["confirmation_phrase"]},
        )
        assert resp.status_code == 409
        assert resp.json()["code"] == "error.runs.preflight_no_plex"

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
        assert resp.json()["code"] == "error.runs.not_running"

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
        # The production accessor, which creates the status lazily. A hand-built one would
        # not be the object the route reads.
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
        """The sibling of the refusal above. The success path logged and the refusal did
        not, so "I pressed Stop and it kept going" left the same nothing."""
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

        # The slot is released, not wedged. The status reads idle, and a retry fails the same
        # way (a RuntimeError, having passed the slot guard), not a spurious 409 'already
        # running' that would prove the slot stayed claimed.
        assert armed_client.get("/api/runs/execute/status").json()["running"] is False
        with pytest.raises(RuntimeError):
            armed_client.post(f"/api/runs/{run['id']}/execute", json=body)


class TestTheProfileControlsTheCaps:
    """The reap caps are the owner's decision, read from the profile, not a hardcoded
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

        # Read back in a fresh request. It was persisted, not just echoed.
        assert client.get("/api/profile").json()["grace_days"] == 21

    def test_the_dry_run_uses_the_saved_cap(self, client: TestClient) -> None:
        """The executor's cap must come from the profile. With one condemned item and a cap
        of 1, the dry run completes, proving the saved cap is what it obeys. A hardcoded
        larger default would also pass, so the abort case is covered by the executor's own
        unit tests instead, where a multi-item plan can be built."""
        settings = client.get("/api/profile").json()
        settings["max_items_per_run"] = 1
        settings["max_items_per_30d"] = 1
        client.put("/api/profile", json=settings)

        run = client.post("/api/runs").json()
        report = client.post(f"/api/runs/{run['id']}/dry-run").json()
        assert report["state"] == "completed"  # 1 item, cap 1

    def test_an_invalid_cap_combination_is_a_422(self, client: TestClient) -> None:
        """A per-run cap above the rolling 30-day cap makes the rolling cap meaningless. The
        domain must refuse it, with the reason, not a silent clamp."""
        settings = client.get("/api/profile").json()
        settings["max_items_per_run"] = 500
        settings["max_items_per_30d"] = 100  # smaller than per-run: nonsensical

        response = client.put("/api/profile", json=settings)
        assert response.status_code == 422

    def test_a_grace_period_under_a_week_is_refused(self, client: TestClient) -> None:
        settings = client.get("/api/profile").json()
        settings["grace_days"] = 3
        assert client.put("/api/profile", json=settings).status_code == 422

    def test_the_wire_and_the_domain_state_the_same_bounds(self) -> None:
        """The caps are declared twice, ``ProfileSettingsIO`` on the wire and
        ``ProfileSettings`` in the domain, with every ``ge``/``le`` transcribed rather than
        derived from the other. The two do not collapse into one model, because the wire
        requires the five fields the domain defaults. That is what makes the test below a
        422, and ``settings_recovered`` is wire-only. So the bounds are held to one answer
        here instead."""
        shared = [
            name for name in ProfileSettingsIO.model_fields if name in ProfileSettings.model_fields
        ]
        assert len(shared) == 7, "a cap arrived or left; these seven are the population"
        assert set(ProfileSettingsIO.model_fields) - set(shared) == {"settings_recovered"}

        for name in shared:
            wire = ProfileSettingsIO.model_fields[name].metadata
            domain = ProfileSettings.model_fields[name].metadata
            assert wire == domain, (
                f"{name} is bounded {wire} in api/schemas.py's ProfileSettingsIO and "
                f"{domain} in engine/policy.py's ProfileSettings"
            )

    def test_a_body_missing_the_caps_is_refused_rather_than_reset(self, client: TestClient) -> None:
        """A partial save is a 422 because the wire model gives those five fields no
        default. A route taking ``ProfileSettings`` instead would answer 200 and write the
        shipped defaults over whatever the operator narrowed, taking a tightened
        ``max_items_per_run`` of 3 back to 10 and forcing ``caps_enabled`` on.
        ``/api/profile`` sits in the API-key write allowlist (``api/middleware.py``), so
        reaching it needs no browser."""
        response = client.put("/api/profile", json={})

        assert response.status_code == 422
        missing = {detail["loc"][-1] for detail in response.json()["detail"]}
        assert missing == {
            "max_items_per_run",
            "max_bytes_per_run",
            "max_items_per_30d",
            "max_bytes_per_30d",
            "grace_days",
        }


class TestLimitsNobodySavedDoNotBoundAReap:
    """``active_profile`` never raises on purpose. The settings page an operator would use to
    repair a broken blob reads the same function. It falls back to the shipped defaults and
    flags it, and those defaults can be looser than what was saved (a run cap of 5 becomes
    10, a grace of 30 becomes 14). Every route that acted on the numbers read them through
    ``active_profile_settings``, which drops the flag, so a profile that stopped validating
    left the deleting route bounded by numbers nobody chose and said nothing. The scan path
    already refuses in this state. These are the rest."""

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
        assert reaped.json()["code"] == "error.runs.limits_unreadable"

    def test_the_refusal_says_what_to_fix(self, client: TestClient, tmp_path: Path) -> None:
        """The operator has to be able to act on this without reading the code, so it names
        the page and the section, not the field that failed to validate."""
        saved = client.get("/api/profile").json()
        client.put("/api/profile", json=saved)
        self._break_the_saved_limits(tmp_path)

        body = client.post("/api/runs").json()
        assert body["code"] == "error.runs.limits_unreadable"
        detail = body["detail"]
        assert detail == refusal_text("error.runs.limits_unreadable")
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


class TestTheReapBreakdownRoute:
    """``test_reap_breakdown.py`` pins every number this route reports, against the service.
    Nothing read the route, so what it actually sends was uncovered at both levels: the
    envelope and the nested per-signal counts."""

    def test_the_body_carries_the_whole_model_and_the_nested_counts(
        self, client: TestClient
    ) -> None:
        body = client.get("/api/reap/breakdown").json()

        assert set(body) == set(ReapBreakdownOut.model_fields)
        assert body["condemned_by"], "the seeded condemned rows tally at least one signal"
        for entry in body["condemned_by"]:
            assert set(entry) == set(SignalCountOut.model_fields)


class TestSnapshot:
    def test_it_reports_the_split_and_the_reclaimable_bytes(self, client: TestClient) -> None:
        body = client.get("/api/snapshots/latest").json()

        assert body["condemned"] == 1
        assert body["protected"] == 1
        assert body["abstained"] == 2
        assert body["reclaimable_bytes"] == 5_900_000_000  # condemned only

    def test_collection_sizes_is_none_when_nothing_was_recorded(self, client: TestClient) -> None:
        # The fixture snapshot carries no ``collection_sizes_json``. Unrecorded, not empty
        # (test_candidate_filters.TestCollectionSizesOnTheSnapshot pins the populated case).
        assert client.get("/api/snapshots/latest").json()["collection_sizes"] is None

    def test_the_horizon_is_exposed(self, client: TestClient) -> None:
        """Media older than this has no watch evidence either way. The owner needs to see
        it. A fresh Tautulli install would make the whole library look abandoned."""
        assert client.get("/api/snapshots/latest").json()["horizon_at"]


class TestTheWhyPanel:
    def test_a_condemned_item_shows_the_protections_that_did_not_fire(
        self, client: TestClient
    ) -> None:
        """The block no competitor shows. Every protection evaluated, with the actual
        numbers, not just which rules matched."""
        candidates = client.get("/api/candidates?verdict=condemn").json()["items"]
        detail = client.get(f"/api/candidates/{candidates[0]['id']}").json()

        checked = detail["explanation"]["protections_checked"]
        assert checked
        assert checked[0]["detail_key"] == {"k": "legacy", "p": {"text": CHECKED_DETAIL}}

    def test_a_protected_item_explains_the_keep(self, client: TestClient) -> None:
        """A tool that only explains its deletions cannot be trusted about its keeps.

        The protected fixture scores 90. On score alone, it would be deleted. The panel must
        say both, why it scored that, and why it is kept anyway."""
        protected = client.get("/api/candidates?verdict=protect").json()["items"]
        detail = client.get(f"/api/candidates/{protected[0]['id']}").json()

        assert detail["verdict"] == "protect"
        assert detail["score"] == 90  # the score it is overriding
        fired = detail["explanation"]["protections_fired"]
        assert fired
        assert "well rated: 8.0 on IMDb from 250,000 votes" in fired[0]["detail_key"]["p"]["text"]

    def test_the_grace_clock_is_exposed(self, client: TestClient) -> None:
        candidates = client.get("/api/candidates?verdict=condemn").json()["items"]

        assert candidates[0]["first_flagged_at"]

    def test_the_match_block_and_keeps_survive_the_wire(self, client: TestClient) -> None:
        """This is a regression check. The stored explanation always carried ``match`` and
        ``keeps``, but the wire schema did not declare them, so pydantic silently dropped
        both, and the panel's "kept to be safe" notice could never render. Every key the UI
        reads must be named in the schema."""
        abstained = client.get("/api/candidates?verdict=abstain").json()["items"]
        detail = client.get(f"/api/candidates/{abstained[0]['id']}").json()

        assert detail["explanation"]["match"]["status"] == "unmatched"
        assert detail["explanation"]["base_score"] == 50.0
        keeps = detail["explanation"]["keeps"]
        assert keeps and keeps[0]["evaluated"] is False

    def test_an_unmatched_item_leads_with_the_plain_cause(self, client: TestClient) -> None:
        """The card's one-liner. Three gates each report "could not check X: Plex has not
        matched this item." The owner should read the shared cause once, in plain words, not
        the first gate's engineer-speak."""
        abstained = client.get("/api/candidates?verdict=abstain").json()["items"]

        assert abstained[0]["reason_key"] == {"k": "kept_safe.unmatched", "p": None}

    def test_a_missing_candidate_is_a_404(self, client: TestClient) -> None:
        assert client.get("/api/candidates/9999").status_code == 404


class TestPanelHeadFields:
    """The panel head's display metadata and jump links, and the card's badge + pill.

    Every field degrades to null/hidden rather than erroring: an old row, an unmatched
    item or a removed instance must never take the panel down with it.
    """

    def test_the_card_carries_the_badge_and_the_dormancy_pill(self, client: TestClient) -> None:
        row = client.get("/api/candidates?verdict=condemn").json()["items"][0]

        assert row["video_resolution"] == "1080"
        assert row["dormant_days"] == 2059

    def test_a_row_without_the_metadata_hides_both(self, client: TestClient) -> None:
        """The abstained fixture predates the capture (no columns), so the resolution
        must be null, not an error."""
        row = client.get("/api/candidates?verdict=abstain").json()["items"][0]

        assert row["video_resolution"] is None
        # Its explanation carries the same typed unwatched signal the condemned and
        # protect rows share, so its dormancy still reads.
        assert row["dormant_days"] == 2059

    def test_the_detail_carries_links_ratings_and_the_meta_line(self, client: TestClient) -> None:
        candidates = client.get("/api/candidates?verdict=condemn").json()["items"]
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
        """A service's external URL, when set, is the address its jump link opens. A service
        left blank still uses its connect address, and Plex (its own web address) is
        untouched."""
        # Radarr and Tautulli get a public address. Seerr is deliberately left blank.
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

        candidates = client.get("/api/candidates?verdict=condemn").json()["items"]
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
        """No rating key -> no Plex/Tautulli link, no tmdb id -> no Radarr link. The panel
        hides them all rather than rendering a broken jump."""
        abstained = client.get("/api/candidates?verdict=abstain").json()["items"]
        detail = client.get(f"/api/candidates/{abstained[0]['id']}").json()

        assert detail["links"] == {
            "plex": None,
            "tautulli": None,
            "seerr": None,
            "radarr": None,
            "sonarr": None,
            "imdb": None,
            "tmdb": None,
            # Empty here too, for a different reason worth keeping apart. This row is
            # unmatched, so Reaper found nothing to offer. An ambiguous or conflicted row has
            # candidates and does offer them (test_display_meta.TestBuildLinks).
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

        candidates = client.get("/api/candidates?verdict=condemn").json()["items"]
        detail = client.get(f"/api/candidates/{candidates[0]['id']}").json()
        # A self-hosted address serves the web client under /web, not /desktop (which 403s).
        assert detail["links"]["plex"].startswith("https://plex.example/web#!/server/")

    def test_a_non_http_address_is_refused_in_plain_words(self, client: TestClient) -> None:
        refused = client.put("/api/settings/plex", json={"web_url": "plex.example"})
        assert refused.status_code == 422
        # The sentence names the box and shows the shape. The message and the validation both
        # go through the same shared check now, so they can't drift apart again. The sibling
        # cases live in test_settings_api.py's TestPlexStatus.
        body = refused.json()
        assert body["code"] == "error.plex.web_url_invalid"
        assert "full web address" in body["detail"]

    def test_clearing_resets_to_the_default(self, client: TestClient) -> None:
        client.put("/api/settings/plex", json={"web_url": "https://plex.example"})
        cleared = client.put("/api/settings/plex", json={"web_url": ""})
        assert cleared.json()["web_url"] == "https://app.plex.tv"


class TestTheSimulator:
    """Re-scores the last snapshot under a candidate policy with zero API calls, so the knob
    and its blast radius sit in the same viewport."""

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
        """The number the owner actually needs before saving is the delta from what they
        have already reviewed, not the total."""
        result = self._simulate(client, 95)  # stricter than the stored 91

        assert result["no_longer_condemned"] == 1
        assert result["newly_condemned"] == 0

    def test_a_protection_wins_at_every_threshold(self, client: TestClient) -> None:
        """The protected fixture scores 90. No threshold, however low, may condemn it. A
        protection always beats the score."""
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
        """Moving condemn_at re-compares a stored score against a new number, which needs no
        API call and is not an approximation."""
        assert self._simulate(client, 50)["exact"] is True

    def test_it_names_what_a_change_would_newly_condemn(self, client: TestClient) -> None:
        """A count is abstract. A title the owner recognizes is what stops a bad threshold.
        Dropping the threshold pulls in the cleanly-abstained item, and the example names it.
        The blocked "Unmatched" row scores higher, yet must not appear. Its protections could
        not be checked, so no threshold may condemn it."""
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
        """The "Unmatched" fixture abstained because its protections could not be checked,
        not because of its score (50) or coverage. Even the loosest possible draft (threshold
        1, no coverage floor) must not count it as a deletion. The scan would keep abstaining
        on it, and a simulator that counts it is telling the owner a plausible wrong number at
        the very moment they pick a threshold."""
        result = client.post(
            "/api/policy/simulate",
            json=_policy(condemn_at=1, coverage_floor_bp=0),
        ).json()

        # The stored-condemned row and the clean abstainer, never the blocked row.
        assert result["condemned"] == 2
        named = [e["title"] for e in result["examples_newly_condemned"]]
        assert "Unmatched" not in named

    def test_a_hand_reaped_row_keeps_its_stored_verdict(self, client: TestClient) -> None:
        """The owner hand-reaped the condemned fixture. A draft threshold above its score
        must not report it "no longer condemned." Every scan will keep condemning it while
        the override stands, and the simulator must agree with the scan."""
        response = client.post(
            "/api/override", json={"media_key": "radarr:1:10", "decision": "reap"}
        )
        assert response.status_code == 200, response.text

        result = self._simulate(client, 95)  # stricter than the stored 91

        assert result["condemned"] == 1
        assert result["no_longer_condemned"] == 0

    def test_a_hand_reap_on_a_protected_row_counts_as_condemned(self, client: TestClient) -> None:
        """The rating floor is a cautious judgment the owner may overrule. Once they
        hand-reap the protected fixture, the simulator must count it condemned at any
        threshold, exactly as the plan and the counts now do (services.condemned). At a draft
        of 95 the stored condemn (91) legitimately drops out, so the hand-reaped row is the
        only deletion left, and the protected tally goes to zero."""
        response = client.post(
            "/api/override", json={"media_key": "radarr:1:11", "decision": "reap"}
        )
        assert response.status_code == 200, response.text

        result = self._simulate(client, 95)

        assert result["condemned"] == 1  # the hand-reap, pinned at any threshold
        assert result["protected"] == 0
        assert result["no_longer_condemned"] == 1  # the stored 91 still drops at 95

    def test_a_hand_reap_on_an_unmatched_row_is_still_refused(self, client: TestClient) -> None:
        """The "Unmatched" fixture is a row Reaper could not tie to a Plex entry. A hand reap
        does not beat "we do not know which file this is." The engine keeps refusing, so the
        simulator must not count it as a deletion either. At a draft of 91 the stored condemn
        still counts, and it must stay the only one.

        This is named for the match, because the match is the whole hold. The fixture also
        carries three blocked gates, since an unmatched item has no rating key and every
        Plex-dependent gate blocks on that. Naming the test for the blocked gates instead
        would pin the wrong thing. Emptying `protections_unknown` alone would also leave this
        test green, without the match hold actually being exercised.

        This test cannot tell whether a blocked gate alone would hold a reap, because its row
        is held by the match either way. The opposite case, a row Reaper can identify whose
        protections merely could not be checked, is the operator's to remove. That needs a
        fixture with blocked gates and a clean match, which this shared snapshot does not
        have, and which cannot be added without moving the counts every sibling test here
        asserts. That case is covered instead where it can be driven directly, with mutation
        proofs, in `test_override_truth.py`
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

        assert result["condemned"] == 1  # only the stored condemn, not the unmatched row

    def test_the_exact_threshold_boundary_condemns_at_and_above(self, client: TestClient) -> None:
        """condemn_at is "at or above." The stored 91 must count as condemned at a threshold
        of exactly 91 and drop out at 92. The route must decide through the same shared
        function as the scan, so a `>` typo here can never pass."""
        assert self._simulate(client, 91)["condemned"] == 1
        assert self._simulate(client, 92)["condemned"] == 0


class TestASnapshotWithNoFrozenFactsRefusesToGuess:
    """The trap this class exists to close, and the premise it actually rests on.

    The simulator re-decides a snapshot by re-comparing stored scores and verdicts against
    new thresholds. That is exact for ``condemn_at`` and ``coverage_floor_bp``. It is simply
    wrong for anything else. Change a signal weight or a gate, and every stored score was
    produced by the old ones. A policy editor that let you drag a weight and then showed a
    confident count would be the single most dangerous screen in the product, because the
    number would look exactly as authoritative as the true one.

    This fixture's snapshot froze no Facts, which is what every case below is really driven
    by. ``api.simulate.simulate``'s replay tier needs every governed row to carry a
    ``facts_json``, so a pre-facts-freeze snapshot falls to the refusal whatever the edit
    was. That is a real state. Every snapshot taken before the freeze shipped is in it, and
    it is exactly the state the refusal must survive. But it is not the same claim as "this
    edit is unanswerable." ``test_the_premise`` pins the premise, so a fixture that later
    starts freezing Facts fails here rather than quietly turning every case below into a
    tautology.

    Which edits are genuinely unanswerable over a snapshot that did freeze its evidence is
    ``tests/test_simulate_hardening.py``'s question, and a gate is no longer one of them.
    """

    def _simulate(self, client: TestClient, policy: dict[str, Any]) -> dict[str, Any]:
        body: dict[str, Any] = client.post("/api/policy/simulate", json=policy).json()
        return body

    def test_the_premise(self, client: TestClient) -> None:
        """No row here froze its Facts, so the replay tier is unreachable by construction."""
        rows = client.get("/api/candidates?verdict=condemn").json()["items"]
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
        same for all three refusals. Without this, the discriminator could be dropped from
        the response and nothing here would notice, while the panel silently fell back to the
        general heading for every cause.

        Driven by the popularity window, which is the span ``distinct_watchers`` is counted
        over, and so the one gate field a scan reads before it freezes an item's facts. This
        case replaced an earlier one based on a keep-tag edit. Keep tags stopped gathering
        differently once they left the policy body for Settings -> Lists, since a list now
        protects through an ``on_list`` rule reading membership the scan gathers regardless of
        whether any rule named it.
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

        assert result["stale_reason"]["k"] == "gathers_differently"

    def test_moving_a_gate_threshold_refuses_over_this_snapshot(self, client: TestClient) -> None:
        """Not because a bar edit is unanswerable. It is answerable, and
        ``test_simulate_hardening.py`` proves it replays. But this snapshot has no evidence
        to replay over. That is the state every install was in before the freeze."""
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
    """Seen-value suggestions for the rule editors. Suggestions, never a gate. Typing a value
    that is not in the list stays valid, so this endpoint fails open to empty."""

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
        # Both matched movies' libraries. The unmatched row has no library and adds nothing.
        assert set(body["values"]) == {"Movies", "Classics"}

    def test_an_unknown_field_is_empty_not_an_error(self, client: TestClient) -> None:
        """A numeric or unheard-of field has nothing to suggest. That is not a fault. The
        input keeps working with no suggestions at all."""
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
        # Their own policy, rescaled, not the shipped default, which would silently replace
        # tuning they chose with numbers they never picked.
        assert out["name"] == "stale"
        assert sum(s["weight"] for s in out["body"]["signals"]) == 100
        # ...and handed over as an unsaved draft, so nothing is written until they look. The
        # list is what the editor counts to open dirty, so an empty one here would leave a
        # degraded scan pointing at a page holding no Save.
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

    @pytest.mark.parametrize(
        ("gate", "keep_tags", "lists"),
        [
            pytest.param(
                "whitelisted",
                ["reaper-keep"],
                [("Saturday movie night", "arr_tag", {"tags": ["movie-night"]})],
                id="the tag list their tags became is gone",
            ),
            pytest.param(
                "curated_list",
                [],
                [("IMDb Top 250", "plex_collection", {})],
                id="a collection of their own already holds the shipped list's name",
            ),
        ],
    )
    def test_a_leftover_list_protection_still_opens_the_editor(
        self,
        settings: Settings,
        gate: str,
        keep_tags: list[str],
        lists: list[tuple[str, str, dict[str, Any]]],
    ) -> None:
        """This covers the pair of routes that reach a leftover list-protection gate.

        ``convert_list_protections`` leaves an enabled ``whitelisted``/``curated_list`` row in
        the body when its replacement keep rule cannot be named, so the cover stands and
        ``build_gates`` refuses the scan out loud rather than withdrawing the protection in
        silence. Both halves are reachable on a real upgrade. The tag list the conversion
        looks for is the operator's to rename, retag, or delete, and the shipped IMDb list is
        skipped by the upgrade migration when a list of that name already exists under any
        source. Either row can be missing on its own.

        This is seeded and flagged before the app boots, which is the state that migration
        leaves. The seed is guarded by ``lists_seeded`` rather than by the rows, so an install
        that has been through it never gains a second shipped copy.

        The editor is the only place this state can be cleared. Rebuilding every loaded row
        through the save boundary on read would answer 500 for exactly the bodies it exists to
        repair, so loading must not do that. Nothing here relaxes the boundary on save. The
        row is still refused there, and it leaves only when the operator takes it off.
        """
        self._seed_upgraded_install(settings, gate=gate, keep_tags=keep_tags, lists=lists)

        with TestClient(create_app(settings)) as client:
            login(client, settings)
            loaded = client.get("/api/policy")

            assert loaded.status_code == 200, loaded.text
            out = loaded.json()
            # Served as it is stored, still on: the editor's leftover-row notice renders off
            # this row, and it is the switch beside it that the operator turns off.
            row = next(g for g in out["body"]["gates"] if g["gate"] == gate)
            assert row["enabled"] is True
            # The savebar is up either way, so there is a Save to press once they have.
            assert out["repairs"] == ["lists_migrated"]

            # Serving it did not make it savable. Handing the body straight back is refused,
            # in the plain sentence the SPA renders verbatim.
            refused = client.post("/api/policy", json=out["body"])

            assert refused.status_code == 422
            items = refused.json()["detail"]
            assert any(item.get("code") == "error.policy.retired_gate" for item in items)
            message = " ".join(d["msg"] for d in items)
            assert "left over from an older version" in message
            # Not the gate id. This is the editor's "Can't save this" banner on arrival, and
            # the row it names is labeled in the operator's words on the same screen.
            assert gate not in message
            assert "Value error" not in message

            # The exit the row's notice names: take it off, save, and the install scans again.
            cleared = dict(out["body"])
            cleared["gates"] = [g for g in cleared["gates"] if g["gate"] != gate]
            saved = client.post("/api/policy", json=cleared)

            assert saved.status_code == 200, saved.text
            reloaded = client.get("/api/policy").json()
            assert gate not in {g["gate"] for g in reloaded["body"]["gates"]}
            assert reloaded["repairs"] == []
            # And it really is gone. No keep rule took its place, which is the cost the row's
            # notice and the scan's sentence both have to state rather than imply.
            assert not [
                c for c in reloaded["body"]["protect_conditions"] if c["field"] == "on_list"
            ]

    @staticmethod
    def _seed_upgraded_install(
        settings: Settings,
        *,
        gate: str,
        keep_tags: list[str],
        lists: list[tuple[str, str, dict[str, Any]]],
    ) -> None:
        """A database an upgrade left carrying a leftover list protection.

        Rows and the ``lists_seeded`` flag go in before the app boots, because that is the
        state the migration leaves. The seed is guarded by the flag rather than by the rows,
        so an install that has been through it never gains a second shipped copy.
        """
        stored = json.loads(DEFAULT_MOVIE_POLICY.model_dump_json())
        stored["protect_conditions"] = []
        stored["keep_tags"] = keep_tags
        stored["keep_tags_match"] = "any"
        stored["gates"] = [{"gate": gate, "enabled": True}, *stored["gates"]]

        engine = sa_create_engine(settings.sync_database_url)
        with Session(engine) as session:
            for name, source, config in lists:
                session.add(
                    ListConfig(
                        name=name,
                        source=source,
                        config_json=json.dumps(config),
                        enabled=True,
                        created_at=utcnow(),
                    )
                )
            session.add(AppSetting(key="lists_seeded", value_json="true", updated_at=utcnow()))
            session.add(
                PolicyModel(
                    name="upgraded",
                    media_type="movie",
                    body_json=json.dumps(stored),
                    policy_hash="0" * 64,
                    created_at=utcnow(),
                )
            )
            session.commit()
        engine.dispose()

    def test_adding_the_list_back_first_is_what_keeps_the_protection(
        self, settings: Settings
    ) -> None:
        """The order ``build_gates`` tells the operator to work in, driven end to end.

        Telling the operator to open Policy and save first, then add the list, would lose the
        protection outright. The test above is the proof: the only way to reach a save is to
        take the row off, and the saved body then carries neither the gate nor
        ``keep_tags``, so the conversion can never fire again and a list added afterwards
        attaches no rule of its own (``api/lists.py``'s ``add_list``).

        In the order the sentence actually names, the same install keeps its cover. The
        registry row resolves ``tag_list_name``, so the next load converts the gate into an
        outright ``on_list`` keep rule naming that list, and the save that follows writes it.
        This test and the sentence in ``build_gates`` state the same fact, so a change to one
        must update the other.
        """
        self._seed_upgraded_install(
            settings,
            gate="whitelisted",
            keep_tags=["reaper-keep"],
            lists=[("Saturday movie night", "arr_tag", {"tags": ["movie-night"]})],
        )

        with TestClient(create_app(settings)) as client:
            login(client, settings)
            blocked = client.get("/api/policy").json()
            assert "whitelisted" in {g["gate"] for g in blocked["body"]["gates"]}

            # Step one of the sentence: the list goes back on Settings, Lists. Resolved by
            # the tags it holds, never by its name or its age, so the operator may call it
            # anything.
            added = client.post(
                "/api/lists/configured",
                json={
                    "name": "Titles I tagged",
                    "source": "arr_tag",
                    "config": {"tags": ["reaper-keep"], "match": "any"},
                },
            )
            assert added.status_code == 201, added.text

            # Step two: open Policy. The conversion now has a list to name, so the gate leaves
            # as a keep rule instead of leaving as nothing.
            out = client.get("/api/policy").json()

            assert "whitelisted" not in {g["gate"] for g in out["body"]["gates"]}
            assert out["repairs"] == ["lists_migrated"]
            assert [
                c["value"] for c in out["body"]["protect_conditions"] if c["field"] == "on_list"
            ] == ["Titles I tagged"]

            # Step three: save. The body is savable now, and the protection is what was saved.
            saved = client.post("/api/policy", json=out["body"])

            assert saved.status_code == 200, saved.text
            reloaded = client.get("/api/policy").json()
            assert reloaded["repairs"] == []
            assert "Titles I tagged" in [
                c["value"]
                for c in reloaded["body"]["protect_conditions"]
                if c["field"] == "on_list"
            ]

    def test_a_saved_policy_is_what_loads_next(self, client: TestClient) -> None:
        client.post("/api/policy", json=_policy(condemn_at=55, name="mine"))

        body = client.get("/api/policy").json()

        assert body["name"] == "mine"
        assert body["body"]["condemn_at"] == 55

    def test_saving_a_gate_no_policy_can_build_is_refused_in_plain_language(
        self, client: TestClient
    ) -> None:
        """``GateId`` is wider than what a policy row can build. It also carries retired ids
        and ids the engine emits on its own. Storing one here must fail immediately, not
        succeed and then break every subsequent scan at ``build_gates`` with no self-heal and
        nothing naming the save that did it. This is refused at the boundary instead, and the
        refusal has to be readable. The SPA renders ``detail[].msg`` verbatim, so pydantic's
        "Value error," prefix would land in front of the sentence.

        The gate id is the part that must not be in it. The editor renders this message as
        its "Can't save this" banner, so an upgraded install whose response is validated
        through this same model meets the banner on arrival, beside a row labeled in the
        operator's own words. Which row it is still comes back in ``loc``, asserted below, so
        nothing an API caller needs was traded for the plain sentence.
        """
        bad = [*DEFAULT_GATES, {"gate": "season_progression", "enabled": True}]

        response = client.post("/api/policy", json=_policy(gates=bad))

        assert response.status_code == 422
        detail = response.json()["detail"]
        message = " ".join(d["msg"] for d in detail)
        assert "season_progression" not in message
        assert "Value error" not in message
        assert "Turn it off, then save." in message
        assert ["body", "gates", "3", "gate"] in [d["loc"] for d in detail]
        assert any(d.get("code") == "error.policy.retired_gate" for d in detail)

        # And the stored policy is untouched, so the install still scans.
        assert client.get("/api/policy").status_code == 200

    def test_the_still_downloading_toggle_survives_a_save(self, client: TestClient) -> None:
        """protect_incomplete_seasons rides the whole PolicyIn -> PolicyBody -> PolicyIn path.
        A non-default value must load back exactly, or the toggle silently resets on reload."""
        client.post("/api/policy", json=_policy(protect_incomplete_seasons=False))

        body = client.get("/api/policy").json()["body"]

        assert body["protect_incomplete_seasons"] is False

    def test_saving_is_append_only_and_idempotent(self, client: TestClient) -> None:
        """The hash is the identity. Opening the editor and saving without changing anything
        must not fork the audit trail. Snapshots and approvals point at these rows and have
        to stay interpretable."""
        first = client.post("/api/policy", json=_policy(condemn_at=55)).json()
        second = client.post("/api/policy", json=_policy(condemn_at=55)).json()

        assert first["policy_hash"] == second["policy_hash"]

    def test_reverting_to_an_earlier_policy_takes_effect(self, client: TestClient) -> None:
        """Save A, then B, then A again. The last save must put A back in force.

        The duplicate-save check must only match against the policy currently in force,
        never an older row found anywhere in history. Matching an older row would let the
        save silently no-op: status 200, the reverted body in the response, but B still
        active underneath. Only re-saving the policy currently in force may no-op. A revert
        is a real change."""
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
        happen before the row is written, not after."""
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
        # "whitelisted" is a yes/no field, and ">=" is not one of its operators, so this
        # condition is unconstructable, not merely wrong.
        body = _policy(protect_conditions=[{"field": "whitelisted", "op": "gte", "value": True}])
        assert client.post("/api/policy", json=body).status_code == 422

    def test_a_condition_value_of_the_wrong_type_is_a_422_not_a_saved_landmine(
        self, client: TestClient
    ) -> None:
        """A JSON string on a numeric field must be refused at save time, naming the field,
        with nothing persisted. Left unchecked, it would save and hash cleanly, then crash
        every subsequent scan inside the judge."""
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
        assert "Seerr" in _msg(flagged[0])


class TestAPopularityWindowLongerThanTheWatchHistory:
    """The other end of the same window, and the wiring that carries it.

    The engine's half is pinned in ``test_policy``. What this pins is that a route actually
    reads the mirror and hands the reach to ``inspect``. The warning is dead the moment a
    ``_policy_out`` call site forgets to pass it, and every assertion in ``test_policy``
    would stay green through that.
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
            # handle would leak into teardown as a ResourceWarning without this.
            conn.close()

    def _policy_body(self, **overrides: object) -> dict[str, Any]:
        """``DEFAULT_GATES`` with the dormancy floor lowered beneath the mirror seeded here.

        ``inspect`` stays silent while the floor alone empties the list, because there the
        popularity window decides nothing and lowering it is inert advice. So a fixture on
        the shipped 1095-day floor would assert nothing about the wiring. It would read as
        green with the reach never fetched at all.
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
        assert "Nothing will be flagged for removal" in _msg(flagged[0])

    def test_the_shipped_dormancy_floor_moves_the_warning_to_the_floor(
        self, client: TestClient, tmp_path: Path
    ) -> None:
        """The same short mirror, against the gates Reaper actually ships.

        ``min_dormancy`` holds every item at 1095 days and dormancy is clamped to the
        mirror, so on a 90-day history nothing can be condemned whatever the popularity
        window says. Warning on the window would send the operator to shorten a keep
        protection that changes nothing, so that anchor stays silent instead.

        The floor speaks for itself, on its own control, and it is the only warning on the
        page here. That also proves the two warnings cannot stack, since one fires exactly
        when the other does not.
        """
        self._seed_mirror(tmp_path, days_back=90)

        body = client.post("/api/policy/validate", json=_policy()).json()

        assert [
            w for w in body["warnings"] if w["field"] == "gates.server_popularity.window_days"
        ] == []
        [floor] = [w for w in body["warnings"] if w["field"] == "gates.min_dormancy.threshold"]
        assert floor["severity"] == "warn"
        assert "Nothing will be flagged for removal" in _msg(floor)
        # Through the route, so this pins the reach actually reaching `inspect` here too.
        # With the mirror unread, the branch cannot fire at all.
        assert "3 years" in _msg(floor)

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
        anchor is the half a route can break on its own. It decides where the SPA renders
        this, and with the gate off, the window picker it would otherwise point at is not on
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
        assert "Nothing will be flagged for removal" in _msg(flagged[0])
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
        """``save_policy`` returns through two ``_policy_out`` calls, and the second one is
        easy to miss. A body content-identical to the policy in force writes nothing and
        returns early. Deleting ``history_reach_days=`` from that call site would leave the
        whole suite green, because the test above only ever reaches the normal return.
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


def _rewatch_explanation(score: float, rewatch_odds: dict[str, Any]) -> str:
    """A candidate's explanation JSON, carrying a ``rewatch_odds`` context block on top of
    the ordinary shape (docs/history/REWATCH_PLAN.md, Stage 2, "Storage and display")."""
    payload = json.loads(_explanation(score))
    payload["rewatch_odds"] = rewatch_odds
    return json.dumps(payload)


@pytest.fixture
def rewatch_odds_client(tmp_path: Path) -> Iterator[TestClient]:
    """A snapshot whose movie candidates carry every ``rewatch_odds`` state GET
    /api/policy/rewatch-odds aggregates: two "measured" rows sharing one block (so
    aggregation is provable, not just present), one "thin" row, one explicit "no_history"
    row, and one row whose explanation JSON cannot be parsed at all. Two season rows carry a
    second "measured" block with numbers distinguishable from the movie block, so a
    ``media_type=tv`` query can be told apart from the movie-only default rather than merely
    returning a non-empty answer.
    """
    settings = Settings(data_dir=tmp_path, secret_key="k")
    engine = sa_create_engine(settings.sync_database_url)
    Base.metadata.create_all(engine)
    list_hash = seeded_fingerprint(settings)

    now = utcnow()
    with Session(engine) as session:
        snapshot = Snapshot(
            created_at=now,
            policy_hash=_fixture_policy_hash(),
            scoring_hash=_fixture_scoring_hash(),
            list_config_hash=list_hash,
            horizon_at=now,
            item_count=5,
            degraded=False,
        )
        session.add(snapshot)
        session.flush()

        measured_block = {"state": "measured", "lo_days": 365.0, "hi_days": 548.0, "n": 40, "k": 12}
        season_measured_block = {
            "state": "measured",
            "lo_days": 180.0,
            "hi_days": 270.0,
            "n": 20,
            "k": 9,
        }
        session.add_all(
            [
                Candidate(
                    snapshot_id=snapshot.id,
                    media_key="radarr:1:30",
                    title="Measured One",
                    media_type="movie",
                    verdict="abstain",
                    score=10,
                    coverage_bp=10_000,
                    explanation_json=_rewatch_explanation(10, measured_block),
                    created_at=now,
                ),
                Candidate(
                    # Same fitted block as the row above. One scan freezes one fit for every
                    # candidate, so both real rows in a block always agree on n/k.
                    snapshot_id=snapshot.id,
                    media_key="radarr:1:31",
                    title="Measured Two",
                    media_type="movie",
                    verdict="abstain",
                    score=10,
                    coverage_bp=10_000,
                    explanation_json=_rewatch_explanation(10, measured_block),
                    created_at=now,
                ),
                Candidate(
                    snapshot_id=snapshot.id,
                    media_key="radarr:1:32",
                    title="Thin One",
                    media_type="movie",
                    verdict="abstain",
                    score=10,
                    coverage_bp=10_000,
                    explanation_json=_rewatch_explanation(
                        10, {"state": "thin", "lo_days": 1825.0, "hi_days": None, "n": 4, "k": 1}
                    ),
                    created_at=now,
                ),
                Candidate(
                    snapshot_id=snapshot.id,
                    media_key="radarr:1:33",
                    title="No History One",
                    media_type="movie",
                    verdict="abstain",
                    score=10,
                    coverage_bp=10_000,
                    explanation_json=_rewatch_explanation(
                        10, {"state": "no_history", "lo_days": 0.0, "hi_days": None, "n": 0, "k": 0}
                    ),
                    created_at=now,
                ),
                Candidate(
                    snapshot_id=snapshot.id,
                    media_key="radarr:1:34",
                    title="Unreadable Row",
                    media_type="movie",
                    verdict="abstain",
                    score=10,
                    coverage_bp=10_000,
                    # Malformed JSON entirely, not merely a missing key. This is the "we
                    # could not read this row at all" case, which resolves toward the
                    # conservative reading (api.policy.rewatch_odds_fit).
                    explanation_json="{not valid json",
                    created_at=now,
                ),
                Candidate(
                    snapshot_id=snapshot.id,
                    media_key="sonarr:1:30",
                    title="Season Measured One",
                    media_type="season",
                    verdict="abstain",
                    score=10,
                    coverage_bp=10_000,
                    explanation_json=_rewatch_explanation(10, season_measured_block),
                    created_at=now,
                ),
                Candidate(
                    # Shares the season block above. This proves tv aggregation the same
                    # way the two movie rows prove it.
                    snapshot_id=snapshot.id,
                    media_key="sonarr:1:31",
                    title="Season Measured Two",
                    media_type="season",
                    verdict="abstain",
                    score=10,
                    coverage_bp=10_000,
                    explanation_json=_rewatch_explanation(10, season_measured_block),
                    created_at=now,
                ),
            ]
        )
        session.commit()
    engine.dispose()

    with TestClient(create_app(settings)) as c:
        login(c, settings)
        yield c


class TestTheRewatchOddsFitEndpoint:
    """GET /api/policy/rewatch-odds: the Policy page's fitted ladder and consequence echo
    (docs/history/REWATCH_PLAN.md, Stage 2). Aggregated from the newest snapshot's frozen
    ``rewatch_odds`` explanation blocks, never refit here."""

    def test_no_snapshot_returns_the_empty_shape(self, tmp_path: Path) -> None:
        settings = Settings(data_dir=tmp_path, secret_key="k")
        engine = sa_create_engine(settings.sync_database_url)
        Base.metadata.create_all(engine)
        engine.dispose()

        with TestClient(create_app(settings)) as c:
            login(c, settings)
            body = c.get("/api/policy/rewatch-odds").json()

        assert body == {"blocks": [], "total_items": 0}

    def test_blocks_aggregate_by_identity_and_unusable_rows_reach_no_block(
        self, rewatch_odds_client: TestClient
    ) -> None:
        # All five rows count toward total_items, including the thin one, the no_history one,
        # and the one whose explanation JSON cannot be read at all. Only the two measured
        # rows reach a block.
        body = rewatch_odds_client.get("/api/policy/rewatch-odds").json()

        assert body["total_items"] == 5
        assert len(body["blocks"]) == 1
        block = body["blocks"][0]
        assert block["lo_days"] == 365.0
        assert block["hi_days"] == 548.0
        assert block["n"] == 40
        assert block["k"] == 12
        assert block["items"] == 2  # both measured rows share this one identity
        assert block["upper_bound_pct"] == round(wilson_upper(12, 40) * 100, 1)

    def test_media_type_tv_aggregates_season_rows_and_ignores_movie_rows(
        self, rewatch_odds_client: TestClient
    ) -> None:
        # A TV policy scores seasons (_candidate_media_type), so ?media_type=tv reads only
        # the two season rows and none of the five movie rows above.
        body = rewatch_odds_client.get("/api/policy/rewatch-odds?media_type=tv").json()

        assert body["total_items"] == 2
        assert len(body["blocks"]) == 1
        block = body["blocks"][0]
        assert block["lo_days"] == 180.0
        assert block["hi_days"] == 270.0
        assert block["n"] == 20
        assert block["k"] == 9
        assert block["items"] == 2
        assert block["upper_bound_pct"] == round(wilson_upper(9, 20) * 100, 1)

    def test_media_type_defaults_to_movie(self, rewatch_odds_client: TestClient) -> None:
        # No media_type param reads the same rows as an explicit ?media_type=movie, and
        # never the season rows.
        default_body = rewatch_odds_client.get("/api/policy/rewatch-odds").json()
        movie_body = rewatch_odds_client.get("/api/policy/rewatch-odds?media_type=movie").json()

        assert default_body == movie_body
        assert default_body["total_items"] == 5

    def test_media_type_rejects_an_unknown_value(self, rewatch_odds_client: TestClient) -> None:
        response = rewatch_odds_client.get("/api/policy/rewatch-odds?media_type=book")

        assert response.status_code == 422


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

        assert any("protect almost nothing" in _msg(w) for w in body["warnings"])

    def test_a_zero_vote_floor_is_refused_outright(self, client: TestClient) -> None:
        """Provably wrong, so it is a 422 rather than a warning. An IMDb bar with no vote
        floor protects an 8.3 drawn from 388 votes."""
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
        # ...and the owner is told why, not handed an Internal Server Error.
        assert "vote floor of 0" in json.dumps(response.json())


class TestUnknownSizeWarningTracksTheDraft:
    """The unknown-size warning renders directly beneath the box that sets it, and that box
    shows an unsaved draft value. Every other warning the editor renders describes the draft,
    so this one must too. Reading the saved profile instead, while sitting under the changed
    box, would raise the allowance with no warning until after a save, or lower it while the
    old warning kept naming the old number. The editor sends the drafted value. Omitting it
    keeps the stored reading."""

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
        return [_msg(w) for w in body["warnings"] if w["field"] == "max_unmeasured_per_run"]

    def test_the_stored_zero_is_quiet(self, client: TestClient) -> None:
        """The shipped profile keeps every unmeasured item, so there is nothing to warn about."""
        assert self._unmeasured(client) == []

    def test_a_drafted_allowance_warns_before_it_is_saved(self, client: TestClient) -> None:
        (message,) = self._unmeasured(client, draft_max_unmeasured_per_run=5)
        assert "up to 5 items" in message

    def test_drafting_it_back_to_zero_clears_the_warning(self, client: TestClient) -> None:
        """Explicit 0 is a value, not an omission. It must not fall back to the stored one."""
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
    """A protect-only field is never even offered to the condemn editor, so a dangerous
    condition is not merely rejected. It is unconstructable."""

    def test_the_condemn_lane_hides_protect_only_fields(self, client: TestClient) -> None:
        keys = {f["key"] for f in client.get("/api/vocabulary?lane=condemn").json()["fields"]}

        assert "watchers_all_time" not in keys
        assert "imdb_votes" not in keys
        assert "whitelisted" not in keys

    def test_the_protect_lane_is_a_superset(self, client: TestClient) -> None:
        condemn = {f["key"] for f in client.get("/api/vocabulary?lane=condemn").json()["fields"]}
        protect = {f["key"] for f in client.get("/api/vocabulary?lane=protect").json()["fields"]}

        assert condemn < protect

    def test_ships_no_english(self, client: TestClient) -> None:
        """The browser says the words now. A field's label, help paragraph and unit come from
        the catalog by its key (`why.field.*`, `policyRules.fieldHelp.*`,
        `policyRules.fieldUnit.*`), never off the wire."""
        for field in client.get("/api/vocabulary?lane=protect").json()["fields"]:
            assert "label" not in field
            assert "help_text" not in field
            assert "unit_suffix" not in field

    def test_a_movie_policy_is_not_offered_a_tv_only_field(self, client: TestClient) -> None:
        """The editor asks with the policy's media type. A TV-only reason ("the show has
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
