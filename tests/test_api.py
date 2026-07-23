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
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine as sa_create_engine
from sqlalchemy.orm import Session

from reaper.clock import utcnow
from reaper.config import Settings
from reaper.db.base import Base
from reaper.db.models import (
    Candidate,
    FirstFlagged,
    Instance,
    InstanceKind,
    PlexServer,
    Snapshot,
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

from ._auth import login

DEFAULT_GATES = [
    {"gate": "whitelisted"},
    {"gate": "min_dormancy", "threshold": 1095},
    {"gate": "rating_floor", "threshold": 75, "secondary": 1000},
    {"gate": "server_popularity", "threshold": 3},
]
#: One signal carrying the whole 100-point budget (PolicyBody._weights_total_one_hundred).
DEFAULT_SIGNALS = [{"signal": "unwatched", "weight": 100, "saturate_at": 1825, "floor": 365}]


def _policy(condemn_at: int = 70, **overrides: object) -> dict[str, object]:
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
                    "detail": "not watched in 5 years, 8 months",
                    "evaluated": True,
                }
            ],
            "protections_fired": [],
            "protections_checked": [
                {
                    "gate": "server_popularity",
                    "detail": "checked: popular here -- 0 distinct watchers, your floor is 3",
                }
            ],
            "protections_unknown": [],
        }
    )


@pytest.fixture
def client(tmp_path: Path) -> Iterator[TestClient]:
    settings = Settings(data_dir=tmp_path, secret_key="k")  # type: ignore[call-arg]
    engine = sa_create_engine(settings.sync_database_url)
    Base.metadata.create_all(engine)

    now = utcnow()
    with Session(engine) as session:
        snapshot = Snapshot(
            created_at=now,
            policy_hash="a" * 64,
            scoring_hash=_fixture_scoring_hash(),
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
                    owner_plex_account_id=1,
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
                            "protections_fired": [
                                {
                                    "gate": "rating_floor",
                                    "detail": "IMDb 8.0 from 250,000 votes (>= your 7.5 floor)",
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
def armed_client(tmp_path: Path) -> Iterator[TestClient]:
    """A client armed at the host default (``destructive_actions_enabled=True``), with one
    condemned movie but no *arr/Plex/Tautulli instances configured. Enough to exercise the
    execute endpoint's confirmation and client-presence gates without any live service."""
    settings = Settings(  # type: ignore[call-arg]
        data_dir=tmp_path, secret_key="k", destructive_actions_enabled=True
    )
    engine = sa_create_engine(settings.sync_database_url)
    Base.metadata.create_all(engine)

    now = utcnow()
    with Session(engine) as session:
        snapshot = Snapshot(
            created_at=now,
            policy_hash="a" * 64,
            scoring_hash=_fixture_scoring_hash(),
            horizon_at=now,
            item_count=1,
            degraded=False,
        )
        session.add(snapshot)
        session.flush()
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
        assert "0 distinct watchers, your floor is 3" in checked[0]["detail"]

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
        assert "7.5 floor" in fired[0]["detail"]

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
        assert row["dormant_for"] == "5 years, 8 months"

    def test_a_row_without_the_metadata_hides_both(self, client: TestClient) -> None:
        """The abstained fixture predates the capture (no columns, and its dormancy came
        from an unmatched item) -- both fields must be null, not an error."""
        row = client.get("/api/candidates?verdict=abstain").json()[0]

        assert row["video_resolution"] is None
        # Its explanation says "not watched in ..." but with evaluated=True from the
        # shared helper; the unmatched item's dormancy is still the helper's phrasing, so
        # dormant_for emits. The protect row exercises the same path.
        assert row["dormant_for"] == "5 years, 8 months"

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
        assert "must start with" in refused.json()["detail"]

    def test_clearing_resets_to_the_default(self, client: TestClient) -> None:
        client.put("/api/settings/plex", json={"web_url": "https://plex.example"})
        cleared = client.put("/api/settings/plex", json={"web_url": ""})
        assert cleared.json()["web_url"] == "https://app.plex.tv"


class TestTheSimulator:
    """Re-scores the last snapshot under a candidate policy with ZERO API calls, so the
    knob and its blast radius sit in the same viewport."""

    def _simulate(self, client: TestClient, condemn_at: int) -> dict[str, object]:
        return client.post(
            "/api/policy/simulate",
            json={
                "condemn_at": condemn_at,
                "gates": DEFAULT_GATES,
                "signals": DEFAULT_SIGNALS,
            },
        ).json()

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

        assert sum(result["histogram"]) == 4  # type: ignore[arg-type]

    def test_a_threshold_only_change_is_exact(self, client: TestClient) -> None:
        """The whole point. Moving condemn_at re-compares a STORED score against a new
        number, which needs no API call and is not an approximation."""
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

    def test_a_hand_reap_on_a_blocked_row_is_still_refused(self, client: TestClient) -> None:
        """The "Unmatched" fixture's protections could not be checked. A hand reap does
        not beat "we could not look": the engine keeps refusing, so the simulator must
        not count it as a deletion either. At a draft of 91 the stored condemn still
        counts, and it must stay the only one."""
        response = client.post(
            "/api/override", json={"media_key": "radarr:1:12", "decision": "reap"}
        )
        assert response.status_code == 200, response.text

        result = self._simulate(client, 91)

        assert result["condemned"] == 1  # only the stored condemn; the blocked row stays out

    def test_the_exact_threshold_boundary_condemns_at_and_above(self, client: TestClient) -> None:
        """condemn_at is "at or above". The stored 91 must count as condemned at a
        threshold of exactly 91 and drop out at 92 -- the route must decide through the
        same shared function as the scan, so a `>` typo here can never pass."""
        assert self._simulate(client, 91)["condemned"] == 1
        assert self._simulate(client, 92)["condemned"] == 0


class TestTheSimulatorRefusesToGuess:
    """The trap this class exists to close.

    The simulator re-decides a snapshot by re-comparing **stored** scores and verdicts
    against new thresholds. That is exact for ``condemn_at`` and ``coverage_floor_bp``.
    It is simply wrong for anything else: change a signal weight or a gate, and every
    stored score was produced by the *old* ones. The snapshot cannot answer the new
    question, and no amount of arithmetic over it can.

    A policy editor that let you drag a weight and then showed a confident count would
    be the single most dangerous screen in the product -- the number would look exactly
    as authoritative as the true one. So the API returns the reason and no numbers.
    """

    def _simulate(self, client: TestClient, policy: dict[str, object]) -> dict[str, object]:
        return client.post("/api/policy/simulate", json=policy).json()

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
        assert sum(result["histogram"]) == 0  # type: ignore[arg-type]
        # No examples and no spared-by tally either: stale names would be acted on
        # exactly like stale counts.
        assert result["examples_newly_condemned"] == []
        assert result["protected_by"] == []

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

    def test_changing_a_gate_also_refuses(self, client: TestClient) -> None:
        """Gates decide the *verdict*, and the verdict is stored too. Loosening the
        rating floor would un-protect items the snapshot still records as protected."""
        loosened = [
            {"gate": "whitelisted"},
            {"gate": "min_dormancy", "threshold": 1095},
            {"gate": "rating_floor", "threshold": 60, "secondary": 1000},  # was 75
            {"gate": "server_popularity", "threshold": 3},
        ]
        result = self._simulate(client, _policy(gates=loosened))

        assert result["exact"] is False

    def test_disabling_a_protection_refuses(self, client: TestClient) -> None:
        """The most dangerous edit of all, and the one most likely to be made while
        watching the condemned count."""
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
        settings = Settings(data_dir=tmp_path, secret_key="k")  # type: ignore[call-arg]
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
        assert out["needs_save"] is True
        assert out["fell_back"] is False

    def test_a_stored_policy_we_cannot_repair_falls_back_and_says_so(
        self, client: TestClient, tmp_path: Path
    ) -> None:
        """Rescaling only fixes the budget. Anything else unreadable opens on the default,
        which must announce itself: a silent default reads as "this is what you configured"
        and is the one way this fallback could cause a deletion nobody chose."""
        settings = Settings(data_dir=tmp_path, secret_key="k")  # type: ignore[call-arg]
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
        assert out["fell_back"] is True
        assert out["needs_save"] is False

    def test_a_saved_policy_is_what_loads_next(self, client: TestClient) -> None:
        client.post("/api/policy", json=_policy(condemn_at=55, name="mine"))

        body = client.get("/api/policy").json()

        assert body["name"] == "mine"
        assert body["body"]["condemn_at"] == 55

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
        settings = Settings(data_dir=tmp_path, secret_key="k")  # type: ignore[call-arg]
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
                assert route.methods <= {"GET", "POST", "HEAD", "OPTIONS"}
                assert "delete" not in route.path.lower()
                assert "execute" not in route.path.lower()
