# SPDX-License-Identifier: AGPL-3.0-or-later
"""Whether a show has finished, from Sonarr's payload to the wire.

The fact is three-state on purpose: ended, still going, and *we could not read it*.
Sonarr reporting no status at all must never arrive at the card as a definite answer,
so the tests below follow all three states -- plus the movie case, where the question
does not apply -- through the one mapping function and out of the API. A nullable bool
would collapse two of those four, which is exactly the conflation this field exists to
avoid.
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
from reaper.db.models import Candidate, Snapshot
from reaper.engine.observation import Absent, Known, Unknown
from reaper.main import create_app
from reaper.services.season_scan import series_ended, show_status_key

from ._auth import login


class TestTheObservationBecomesAKey:
    """The one place the three-state fact becomes a stored value."""

    def test_an_ended_show(self) -> None:
        assert show_status_key(Known(value=True, source="sonarr")) == "ended"

    def test_a_show_still_going(self) -> None:
        assert show_status_key(Known(value=False, source="sonarr")) == "continuing"

    def test_an_unreadable_status_keeps_its_own_value(self) -> None:
        """Never "continuing". A status we could not read is not a claim that the show
        is still running, and the card draws it as "we could not check"."""
        assert show_status_key(Unknown(reason="no status", source="sonarr")) == "unknown"

    def test_a_row_the_question_does_not_apply_to_carries_nothing(self) -> None:
        """Absent is the movie case (and any row nobody stamped): the card shows no
        status at all, rather than an unreadable one."""
        assert show_status_key(Absent(source="radarr")) is None


class TestSonarrPayloadsMapThroughUnchanged:
    """The production expression, on the payload shapes Sonarr actually sends."""

    @pytest.mark.parametrize(
        ("series", "expected"),
        [
            ({"status": "ended", "ended": True}, "ended"),
            ({"status": "deleted", "ended": True}, "ended"),
            ({"status": "continuing", "ended": False}, "continuing"),
            # An upcoming show has not ended, which is why the label is "Still going"
            # and not "Continuing": this arm covers a show that has not started.
            ({"status": "upcoming", "ended": False}, "continuing"),
            # Neither key present: the one case that must survive as its own state.
            ({"title": "Example Show"}, "unknown"),
        ],
    )
    def test_the_payload_reaches_the_right_key(
        self, series: dict[str, object], expected: str
    ) -> None:
        assert show_status_key(series_ended(series)) == expected


def _explanation(score: int) -> str:
    return json.dumps(
        {
            "score": score,
            "threshold": 70,
            "coverage": 1.0,
            "signals": [],
            "protections_fired": [],
            "protections_checked": [],
            "protections_unknown": [],
        }
    )


@pytest.fixture
def client(tmp_path: Path) -> Iterator[TestClient]:
    """A snapshot holding one show per status, plus a movie.

    The ended show also carries a season with no status stored -- the shape a snapshot
    taken before this field existed leaves behind -- so the show-level rollup is
    exercised against a group that is not uniformly filled in.
    """
    settings = Settings(data_dir=tmp_path, secret_key="k")  # type: ignore[call-arg]
    engine = sa_create_engine(settings.sync_database_url)
    Base.metadata.create_all(engine)

    now = utcnow()
    with Session(engine) as session:
        snapshot = Snapshot(
            created_at=now,
            policy_hash="a" * 64,
            scoring_hash="b" * 64,
            horizon_at=now,
            item_count=5,
            degraded=False,
        )
        session.add(snapshot)
        session.flush()

        def season(series_id: int, number: int, status: str | None) -> Candidate:
            return Candidate(
                snapshot_id=snapshot.id,
                media_key=f"sonarr:5:{series_id}:{number}",
                title=f"Example Show {series_id} · Season {number}",
                media_type="season",
                size_bytes=1_000_000_000,
                group_key=f"sonarr:5:{series_id}",
                group_title=f"Example Show {series_id}",
                show_status=status,
                verdict="condemn",
                score=80 + number,
                coverage_bp=10_000,
                explanation_json=_explanation(80 + number),
                created_at=now,
            )

        session.add_all(
            [
                season(1, 1, "ended"),
                season(1, 2, None),  # a row from before the field existed
                season(2, 1, "continuing"),
                season(3, 1, "unknown"),
                Candidate(
                    snapshot_id=snapshot.id,
                    media_key="radarr:1:10",
                    title="Example Movie",
                    media_type="movie",
                    size_bytes=5_000_000_000,
                    verdict="condemn",
                    score=91,
                    coverage_bp=10_000,
                    explanation_json=_explanation(91),
                    created_at=now,
                ),
            ]
        )
        session.commit()
    engine.dispose()

    with TestClient(create_app(settings)) as c:
        login(c, settings)
        yield c


def _by_key(client: TestClient) -> dict[str, dict[str, object]]:
    rows = client.get("/api/candidates", params={"verdict": "condemn"}).json()
    return {str(r["media_key"]): r for r in rows}


class TestTheQueueCarriesEveryState:
    def test_all_three_states_and_the_movie_survive_the_wire(self, client: TestClient) -> None:
        rows = _by_key(client)

        assert rows["sonarr:5:1:1"]["show_status"] == "ended"
        assert rows["sonarr:5:2:1"]["show_status"] == "continuing"
        assert rows["sonarr:5:3:1"]["show_status"] == "unknown"
        # A movie is not a series: nothing to say, and nothing said.
        assert rows["radarr:1:10"]["show_status"] is None

    def test_a_row_stored_before_the_field_existed_is_null_not_a_guess(
        self, client: TestClient
    ) -> None:
        assert _by_key(client)["sonarr:5:1:2"]["show_status"] is None


class TestThePanelCarriesEveryState:
    @pytest.mark.parametrize(
        ("media_key", "expected"),
        [
            ("sonarr:5:1:1", "ended"),
            ("sonarr:5:2:1", "continuing"),
            ("sonarr:5:3:1", "unknown"),
            ("radarr:1:10", None),
        ],
    )
    def test_the_detail_repeats_the_status(
        self, client: TestClient, media_key: str, expected: str | None
    ) -> None:
        row = _by_key(client)[media_key]
        detail = client.get(f"/api/candidates/{row['id']}").json()

        assert detail["show_status"] == expected


class TestTheShowCardCarriesTheStatus:
    @pytest.mark.parametrize(
        ("group_key", "expected"),
        [
            ("sonarr:5:1", "ended"),
            ("sonarr:5:2", "continuing"),
            ("sonarr:5:3", "unknown"),
        ],
    )
    def test_the_group_reads_the_show_level_value(
        self, client: TestClient, group_key: str, expected: str
    ) -> None:
        """Any season answers for the show: one reading of the series is stamped onto
        every one of its seasons in the same scan."""
        group = client.get(f"/api/groups/{group_key}").json()

        assert group["show_status"] == expected

    def test_a_partly_filled_group_still_reports_the_status(self, client: TestClient) -> None:
        """The ended show has a season with nothing stored. The rollup must skip it
        rather than let row order blank a status the group plainly has."""
        group = client.get("/api/groups/sonarr:5:1").json()

        assert [s["show_status"] for s in group["seasons"]] == ["ended", None]
        assert group["show_status"] == "ended"
