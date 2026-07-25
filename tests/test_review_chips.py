# SPDX-License-Identifier: AGPL-3.0-or-later
"""The card chips and the whole-show group view.

The chip is pure display extraction from the stored explanation -- never a
re-decision -- so the unit tests below enumerate every stored verdict state a card
can be in (rule 23): protect under each gate, and each abstain cause in
decide_verdict's own precedence (match trouble, a deliberate left-for-you flag,
checks that couldn't run, the coverage floor, the score). The API tests then check
that a show can be read whole: every season, every lane, one response.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine as sa_create_engine
from sqlalchemy.orm import Session

from reaper.api.routes import _chip, _decode_explanation, _kept_phrase, _season_number
from reaper.clock import utcnow
from reaper.config import Settings
from reaper.db.base import Base
from reaper.db.models import Candidate, Snapshot
from reaper.main import create_app
from reaper.services.snapshot import HAND_SPARE_DETAIL

from ._auth import login

CONFLICT_SENTENCE = (
    "2 people watched Season 3, more than watched Season 1, which your keep rule "
    "protects. Reaper left it for you to decide instead of removing it."
)


def _exp(
    score: float,
    *,
    threshold: int = 70,
    fired: list[dict[str, str]] | None = None,
    unknown: list[dict[str, str]] | None = None,
    match_status: str | None = None,
) -> dict[str, object]:
    body: dict[str, object] = {
        "score": score,
        "threshold": threshold,
        "coverage": 1.0,
        "signals": [],
        "protections_fired": fired or [],
        "protections_checked": [],
        "protections_unknown": unknown or [],
    }
    if match_status is not None:
        body["match"] = {"status": match_status}
    return body


def _exp_json(*args: object, **kwargs: object) -> str:
    """The same explanation as a stored JSON string, for building candidate rows."""
    return json.dumps(_exp(*args, **kwargs))  # type: ignore[arg-type]


class TestKeptChipWording:
    """One green phrase per protection, from the gates' own closed detail vocabulary."""

    @pytest.mark.parametrize(
        ("gate", "detail", "phrase"),
        [
            ("whitelisted", "on your keep list, never reaped", "on your keep list"),
            # The scan's own constant, not a copy of it: the chip tells a hand spare apart
            # from a real keep-list entry by exact equality, so a test that retyped the
            # string would keep passing after one side was reworded.
            ("whitelisted", HAND_SPARE_DETAIL, "you spared it"),
            ("streaming_now", "someone is watching it right now", "playing right now"),
            (
                "rating_floor",
                "well rated: 6.8 on IMDb from 722,243 votes, at or above the 7.5 you keep",
                "well rated: 6.8 on IMDb",
            ),
            ("rating_floor", "some future wording", "well rated"),
            (
                "server_popularity",
                "watched here: 3 people in the last year",
                "3 people watched it in the last year",
            ),
            (
                "server_popularity",
                "watched here: 1 person in the last 90 days",
                "1 person watched it in the last 90 days",
            ),
            ("server_popularity", "some future wording", "people here still watch it"),
            ("curated_list", "on a protected list: A Curated List", "on a protected list"),
            (
                "min_dormancy",
                "untouched for just 1 year, 2 months, less than the 3 years Reaper waits",
                "watched too recently",
            ),
            (
                "min_dormancy",
                "no watch history, so its dormancy cannot be established",
                "no watch history, kept to be safe",
            ),
            (
                "unmanaged",
                "no Sonarr or Radarr manages this file, so Reaper cannot remove it",
                "not managed by Sonarr or Radarr",
            ),
            ("season_progression", "specials are never auto-pruned", "specials are never removed"),
            ("season_progression", "Sonarr is still downloading this season", "still downloading"),
            ("season_progression", "currently airing", "currently airing"),
            (
                "season_progression",
                "the first season is kept so the show can still be started",
                "the first season stays",
            ),
            (
                "season_progression",
                "within the last 2 seasons (rank 1)",
                "in the last 2 seasons you keep",
            ),
            (
                "season_progression",
                "this show has only 2 seasons on disk, so your keep-last-3 rule keeps all of them",
                "your keep rule keeps all its seasons",
            ),
            (
                "season_progression",
                "a viewer is part-way through the show",
                "someone is partway through",
            ),
            ("season_progression", "some future wording", "your season rule keeps it"),
            ("custom", "your rule: genre is Documentary", "by your rule"),
            ("brand_new_gate", "whatever it says", "a protection applies"),
        ],
    )
    def test_phrase(self, gate: str, detail: str, phrase: str) -> None:
        assert _kept_phrase(gate, detail) == phrase


class TestChip:
    """The one chip per card, across every stored verdict state."""

    def test_condemned_rows_carry_no_chip(self) -> None:
        """Condemned cards lead with the amber dormancy pill, not a chip."""
        assert _chip(_exp(91), "condemn", 91) is None

    def test_protect_names_the_protection_that_fired(self) -> None:
        chip = _chip(
            _exp(
                90,
                fired=[
                    {
                        "gate": "rating_floor",
                        "detail": (
                            "well rated: 6.8 on IMDb from 722,243 votes, "
                            "at or above the 6.0 you keep"
                        ),
                    }
                ],
            ),
            "protect",
            90,
        )
        assert chip is not None
        assert chip.tone == "kept"
        assert chip.text == "Kept · well rated: 6.8 on IMDb"

    def test_protect_with_nothing_fired_degrades_to_no_chip(self) -> None:
        """A stored row that claims protect but records no protection must not invent
        one -- the card simply shows no chip rather than a wrong one."""
        assert _chip(_exp(90), "protect", 90) is None

    def test_unmatched_beats_everything_else(self) -> None:
        chip = _chip(
            _exp(
                82,
                match_status="unmatched",
                unknown=[{"gate": "min_dormancy", "detail": "could not check x: y"}],
            ),
            "abstain",
            82,
        )
        assert chip is not None
        assert (chip.tone, chip.text) == ("quiet", "Couldn't be found in Plex")

    def test_ambiguous_match(self) -> None:
        chip = _chip(_exp(50, match_status="ambiguous"), "abstain", 50)
        assert chip is not None
        assert (chip.tone, chip.text) == ("quiet", "Looks like two different things in Plex")

    def test_season_conflict_wants_eyes(self) -> None:
        """The keep-rule conflict is a deliberate left-for-you flag, not a plumbing
        failure -- it wears the amber-outline tone."""
        chip = _chip(
            _exp(82, unknown=[{"gate": "season_progression", "detail": CONFLICT_SENTENCE}]),
            "abstain",
            82,
        )
        assert chip is not None
        assert chip.tone == "look"
        assert chip.text == "Needs a look · watched more than a season your rule keeps"

    def test_any_future_deliberate_flag_still_wants_eyes(self) -> None:
        """A blocked detail that is a sentence of its own (not "could not check") is a
        deliberate flag whatever gate raised it -- fail toward showing it loudly."""
        chip = _chip(
            _exp(60, unknown=[{"gate": "custom", "detail": "A rule asked a human to look."}]),
            "abstain",
            60,
        )
        assert chip is not None
        assert (chip.tone, chip.text) == ("look", "Needs a look · left for you to decide")

    def test_checks_that_could_not_run(self) -> None:
        chip = _chip(
            _exp(
                50,
                unknown=[
                    {
                        "gate": "min_dormancy",
                        "detail": "could not check when it was last watched: no history",
                    }
                ],
            ),
            "abstain",
            50,
        )
        assert chip is not None
        assert (chip.tone, chip.text) == ("quiet", "Some checks couldn't run")

    def test_coverage_floor(self) -> None:
        """Past the blocked cases, an abstain at or above the threshold can only be the
        coverage floor (decide_verdict's order)."""
        chip = _chip(_exp(82), "abstain", 82)
        assert chip is not None
        assert (chip.tone, chip.text) == ("quiet", "Too little of it could be checked")

    def test_below_threshold_names_both_numbers(self) -> None:
        chip = _chip(_exp(42), "abstain", 42)
        assert chip is not None
        assert (chip.tone, chip.text) == ("quiet", "Scored 42, under your 70")

    def test_malformed_explanation_never_errors_a_row_off_the_queue(self) -> None:
        # The parse happens once per row now (_decode_explanation), and anything that is
        # not a JSON object arrives here as None. Every extractor re-checks that itself,
        # so calling one directly is exactly as defensive as calling it through the queue.
        for raw in ("not json", "[1, 2]", "null", '"a string"'):
            assert _decode_explanation(raw) is None
            assert _chip(_decode_explanation(raw), "abstain", 50) is None
            assert _chip(_decode_explanation(raw), "protect", 50) is None
        assert _chip(None, "abstain", 50) is None


class TestChipWhy:
    """The clause a chip carries for the refused-reap sentence.

    The frontend used to recover this by slicing "Kept · " off the chip text and looking
    the rest up in a transcribed copy of the strings above, so rewording one chip here
    silently dropped every held-reap explanation to a generic fallback -- and both sides
    of the contract were asserted from the same transcription, so nothing failed (H-1).
    The clause now ships beside the text, and these are the assertions that hold the two
    in step.
    """

    def test_a_fired_protection_says_the_same_words_without_the_lead(self) -> None:
        """ "Kept · playing right now" reads "kept for now: playing right now"."""
        chip = _chip(
            _exp(90, fired=[{"gate": "streaming_now", "detail": "someone is watching"}]),
            "protect",
            90,
        )
        assert chip is not None
        assert chip.text == f"Kept · {chip.why}"

    @pytest.mark.parametrize(
        ("explanation", "verdict", "score", "why"),
        [
            (_exp(82, match_status="unmatched"), "abstain", 82, "it couldn't be found in Plex"),
            (
                _exp(50, match_status="ambiguous"),
                "abstain",
                50,
                "it looks like two different things in Plex",
            ),
            (
                _exp(82, unknown=[{"gate": "season_progression", "detail": CONFLICT_SENTENCE}]),
                "abstain",
                82,
                "watched more than a season your rule keeps",
            ),
            (
                _exp(60, unknown=[{"gate": "custom", "detail": "A rule asked a human to look."}]),
                "abstain",
                60,
                "a check on it couldn't be settled",
            ),
            (
                _exp(50, unknown=[{"gate": "min_dormancy", "detail": "could not check when: no"}]),
                "abstain",
                50,
                "some checks couldn't run",
            ),
        ],
    )
    def test_every_blocked_lane_words_its_own_clause(
        self, explanation: str, verdict: str, score: int, why: str
    ) -> None:
        chip = _chip(explanation, verdict, score)
        assert chip is not None
        assert chip.why == why

    @pytest.mark.parametrize(
        ("explanation", "verdict", "score"),
        [
            (_exp(82), "abstain", 82),  # the coverage floor
            (_exp(42), "abstain", 42),  # under the threshold
        ],
    )
    def test_a_chip_about_the_score_names_no_refusal(
        self, explanation: str, verdict: str, score: int
    ) -> None:
        """None is a real answer, not a gap. An item that merely scored low is reaped
        when the owner asks; nothing is holding it, so there is no clause to say."""
        chip = _chip(explanation, verdict, score)
        assert chip is not None
        assert chip.why is None

    @pytest.mark.parametrize(
        ("explanation", "verdict", "score"),
        [
            (_exp(90, fired=[{"gate": "unmanaged", "detail": "not managed"}]), "protect", 90),
            (_exp(90, fired=[{"gate": "curated_list", "detail": "on a list"}]), "protect", 90),
            (_exp(82, match_status="unmatched"), "abstain", 82),
            (_exp(50, match_status="ambiguous"), "abstain", 50),
            (
                _exp(82, unknown=[{"gate": "season_progression", "detail": CONFLICT_SENTENCE}]),
                "abstain",
                82,
            ),
            (
                _exp(60, unknown=[{"gate": "custom", "detail": "A rule asked a human to look."}]),
                "abstain",
                60,
            ),
            (
                _exp(50, unknown=[{"gate": "min_dormancy", "detail": "could not check when: no"}]),
                "abstain",
                50,
            ),
        ],
    )
    def test_a_clause_reads_mid_sentence(self, explanation: str, verdict: str, score: int) -> None:
        """It follows a colon, so it starts lowercase and carries no chip furniture: no
        capital lead, and no middot of its own to collide with the sentence's."""
        chip = _chip(explanation, verdict, score)
        assert chip is not None
        assert chip.why is not None
        assert chip.why[0].islower()
        assert "·" not in chip.why


class TestSeasonNumber:
    def test_season_key(self) -> None:
        assert _season_number("sonarr:5:42:16") == 16

    def test_movie_key(self) -> None:
        assert _season_number("radarr:1:10") is None

    def test_garbage_key_degrades_to_none(self) -> None:
        assert _season_number("not-a-key") is None


# ---------------------------------------------------------------------------
# The whole-show group view
# ---------------------------------------------------------------------------


@pytest.fixture
def client(tmp_path: Path) -> Iterator[TestClient]:
    """A snapshot holding one show whose three seasons landed in three different
    lanes, plus a movie -- the shape the group view exists to show whole."""
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
            item_count=4,
            degraded=False,
        )
        session.add(snapshot)
        session.flush()

        def season(
            number: int, verdict: str, score: int, explanation: str, **extra: object
        ) -> Candidate:
            return Candidate(
                snapshot_id=snapshot.id,
                media_key=f"sonarr:5:42:{number}",
                title=f"Example Show · Season {number}",
                media_type="season",
                size_bytes=1_000_000_000 * number,
                group_key="sonarr:5:42",
                group_title="Example Show",
                verdict=verdict,
                score=score,
                coverage_bp=10_000,
                explanation_json=explanation,
                created_at=now,
                **extra,
            )

        session.add_all(
            [
                # Inserted out of season order on purpose: the group view must sort.
                season(
                    3,
                    "abstain",
                    82,
                    _exp_json(
                        82,
                        unknown=[{"gate": "season_progression", "detail": CONFLICT_SENTENCE}],
                    ),
                    year=2014,
                ),
                season(
                    1,
                    "protect",
                    34,
                    _exp_json(
                        34,
                        fired=[
                            {
                                "gate": "season_progression",
                                "detail": (
                                    "the first season is kept so the show can still be started"
                                ),
                            }
                        ],
                    ),
                    year=2012,
                    summary="A placeholder synopsis.",
                ),
                season(2, "condemn", 88, _exp_json(88), year=2013),
                Candidate(
                    snapshot_id=snapshot.id,
                    media_key="radarr:1:10",
                    title="Example Movie",
                    media_type="movie",
                    size_bytes=5_000_000_000,
                    verdict="condemn",
                    score=91,
                    coverage_bp=10_000,
                    explanation_json=_exp_json(91),
                    created_at=now,
                ),
            ]
        )
        session.commit()
    engine.dispose()

    with TestClient(create_app(settings)) as c:
        login(c, settings)
        yield c


class TestCandidatesCarryTheGroupShape:
    def test_season_rows_carry_chip_number_and_the_whole_strip(self, client: TestClient) -> None:
        """A row in one lane still describes the WHOLE show's shape: its strip marks
        every season across every lane, so the card can show kept and condemned
        side by side."""
        rows = client.get("/api/candidates", params={"verdict": "abstain"}).json()
        assert len(rows) == 1
        row = rows[0]
        assert row["season_number"] == 3
        assert row["chip"] == {
            "tone": "look",
            "text": "Needs a look · watched more than a season your rule keeps",
            # The clause travels with the chip, so a held reap on this row can say why
            # without the frontend parsing the text back apart (H-1).
            "why": "watched more than a season your rule keeps",
        }
        marks = row["group_seasons"]
        assert [(m["season"], m["verdict"]) for m in marks] == [
            (1, "protect"),
            (2, "condemn"),
            (3, "abstain"),
        ]
        # Each mark carries its season's own candidate id, so clicking a strip square
        # opens that season's reasoning. This row IS season 3, so its mark points back
        # to it; every mark's id is a real candidate.
        assert all(isinstance(m["id"], int) for m in marks)
        assert next(m["id"] for m in marks if m["season"] == 3) == row["id"]

    def test_movie_rows_carry_no_strip(self, client: TestClient) -> None:
        rows = client.get("/api/candidates", params={"verdict": "condemn"}).json()
        movie = next(r for r in rows if r["media_type"] == "movie")
        assert movie["group_seasons"] is None
        assert movie["season_number"] is None
        assert movie["chip"] is None  # condemned cards keep the amber pill instead


class TestGroupDetail:
    def test_the_show_reads_whole(self, client: TestClient) -> None:
        group = client.get("/api/groups/sonarr:5:42").json()
        assert group["title"] == "Example Show"
        assert group["year"] == 2012
        assert group["summary"] == "A placeholder synopsis."
        assert group["size_bytes"] == 6_000_000_000
        # Sorted by season number, whatever order the rows were stored in.
        assert [s["season_number"] for s in group["seasons"]] == [1, 2, 3]
        assert [s["verdict"] for s in group["seasons"]] == ["protect", "condemn", "abstain"]

    def test_the_show_leads_with_the_season_that_wants_eyes(self, client: TestClient) -> None:
        """A deliberately-flagged season outranks a merely higher-scoring one for the
        show-level status line."""
        group = client.get("/api/groups/sonarr:5:42").json()
        assert group["chip"]["tone"] == "look"
        assert group["reason"] == CONFLICT_SENTENCE

    def test_unknown_show_is_a_404(self, client: TestClient) -> None:
        assert client.get("/api/groups/sonarr:5:999").status_code == 404

    def test_the_group_view_is_behind_auth(self, tmp_path: Path) -> None:
        authless_dir = tmp_path / "authless"
        authless_dir.mkdir()
        settings = Settings(data_dir=authless_dir, secret_key="k")  # type: ignore[call-arg]
        engine = sa_create_engine(settings.sync_database_url)
        Base.metadata.create_all(engine)
        engine.dispose()
        with TestClient(create_app(settings)) as anonymous:
            assert anonymous.get("/api/groups/sonarr:5:42").status_code == 401
