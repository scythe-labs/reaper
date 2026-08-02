# SPDX-License-Identifier: AGPL-3.0-or-later
"""An unreadable stored explanation degrades one row. It never errors a page.

Every display extractor over ``explanation_json`` shares one contract: it reads what it
can and returns nothing for what it cannot. Reaper writes these documents through a
pydantic model, so the malformed shapes below need a corrupted or hand-edited database --
but the contract is what keeps a single bad row from blanking the whole review queue, and
in the reap-override path it is also what keeps the file (an explanation nobody can read
is not permission to delete).

The shapes tested here are the ones that used to raise rather than degrade: a top level
that is not an object, protection entries that are not objects, entries missing ``detail``,
and a truthy non-dict ``match``.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine as sa_create_engine
from sqlalchemy.orm import Session

from reaper.api.routes import (
    _chip,
    _decode_explanation,
    _dormant_for,
    _explanation_out,
    _primary_reason,
)
from reaper.clock import utcnow
from reaper.config import Settings
from reaper.db.base import Base
from reaper.db.models import Candidate, Snapshot, WhitelistEntry
from reaper.main import create_app
from reaper.services.condemned import reap_override_verdict

from ._auth import login

#: Stored explanations that are shaped wrong in every way the extractors index into.
#: Each maps to the access that used to raise on it.
MALFORMED = {
    # json.loads succeeds but the top level is not an object -> exp.get(...) was an
    # AttributeError on a list, and on None.
    "radarr:1:1": "[1, 2, 3]",
    "radarr:1:2": "null",
    # Not JSON at all.
    "radarr:1:3": "not json at all",
    # An object, but the protection list holds strings -> fired[0]["detail"] was a
    # TypeError.
    "radarr:1:4": json.dumps(
        {
            "score": 80.0,
            "threshold": 70,
            "coverage": 1.0,
            "signals": [],
            "protections_fired": ["a bare string"],
            "protections_checked": [],
            "protections_unknown": [],
        }
    ),
    # An object whose protection entry has no detail key -> fired[0]["detail"] was a
    # KeyError.
    "radarr:1:5": json.dumps(
        {
            "score": 80.0,
            "threshold": 70,
            "coverage": 1.0,
            "signals": [],
            "protections_fired": [{"gate": "whitelisted"}],
            "protections_checked": [],
            "protections_unknown": [],
        }
    ),
    # A truthy non-dict match -> ("x" or {}).get("status") was an AttributeError, in both
    # the queue's reason line and the reap-override read.
    "radarr:1:6": json.dumps(
        {
            "score": 40.0,
            "threshold": 70,
            "coverage": 1.0,
            "signals": [],
            "protections_fired": [],
            "protections_checked": [],
            "protections_unknown": [],
            "match": "x",
        }
    ),
}

#: One row that is perfectly fine, as the control: every assertion about degrading has to
#: be paired with proof that a healthy row is NOT degraded, or "it degrades" is
#: indistinguishable from "it always degrades".
HEALTHY = json.dumps(
    {
        "score": 80.0,
        "threshold": 70,
        "coverage": 1.0,
        "signals": [],
        "protections_fired": [{"gate": "whitelisted", "detail": "you spared it by hand"}],
        "protections_checked": [],
        "protections_unknown": [],
    }
)

#: The verdict each malformed row is stored with, so the extractors are exercised on
#: every lane rather than just one.
VERDICTS = {
    "radarr:1:1": "condemn",
    "radarr:1:2": "protect",
    "radarr:1:3": "abstain",
    "radarr:1:4": "protect",
    "radarr:1:5": "protect",
    "radarr:1:6": "abstain",
    "radarr:1:7": "protect",
}


@pytest.fixture
def client(tmp_path: Path) -> Iterator[TestClient]:
    """A snapshot in which every row's stored explanation is broken in some way."""
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
            item_count=len(MALFORMED) + 1,
            degraded=False,
        )
        session.add(snapshot)
        session.flush()
        rows = {**MALFORMED, "radarr:1:7": HEALTHY}
        for index, (media_key, explanation) in enumerate(rows.items(), start=1):
            session.add(
                Candidate(
                    snapshot_id=snapshot.id,
                    media_key=media_key,
                    title=f"Example Movie {index}",
                    media_type="movie",
                    size_bytes=1_000_000_000,
                    verdict=VERDICTS[media_key],
                    score=80,
                    coverage_bp=10_000,
                    explanation_json=explanation,
                    created_at=now,
                )
            )
        # A hand reap on one of them: the reap-override read runs over a broken
        # explanation, which is the path that must resolve to "kept", not raise.
        session.add(
            WhitelistEntry(
                media_key="radarr:1:6",
                title="Example Movie 6",
                decision="reap",
                created_at=now,
            )
        )
        session.commit()
    engine.dispose()

    with TestClient(create_app(settings)) as c:
        login(c, settings)
        yield c


class TestTheQueueSurvivesABrokenRow:
    def test_every_lane_renders(self, client: TestClient) -> None:
        """One unreadable row used to 500 the whole page, not just its own reason line."""
        seen: set[str] = set()
        for verdict in ("condemn", "protect", "abstain"):
            response = client.get("/api/candidates", params={"verdict": verdict})
            assert response.status_code == 200
            seen.update(str(row["media_key"]) for row in response.json())
        assert seen == set(MALFORMED) | {"radarr:1:7"}

    def test_a_broken_row_shows_no_reason_rather_than_a_wrong_one(self, client: TestClient) -> None:
        rows = client.get("/api/candidates", params={"verdict": "protect"}).json()
        by_key = {str(r["media_key"]): r for r in rows}
        # Nothing readable to lead with, so the card leads with nothing.
        assert by_key["radarr:1:2"]["reason"] is None
        assert by_key["radarr:1:4"]["reason"] is None
        assert by_key["radarr:1:5"]["reason"] is None
        assert by_key["radarr:1:2"]["chip"] is None
        assert by_key["radarr:1:4"]["chip"] is None

    def test_a_broken_match_does_not_honor_the_hand_reap(self, client: TestClient) -> None:
        """The reap-override read is documented as "unreadable means kept". A non-dict
        match used to raise straight past that promise.

        The row is stored ``abstain`` but rides the KEPT lane, because a reap the engine
        will not honor keeps the file -- which is the whole point.
        """
        rows = client.get("/api/candidates", params={"verdict": "protect"}).json()
        row = next(r for r in rows if r["media_key"] == "radarr:1:6")
        assert row["override"] == "reap"
        assert row["override_effective"] is False


class TestTheWhyPanelSurvivesABrokenRow:
    def test_it_degrades_and_says_so(self, client: TestClient) -> None:
        rows = client.get("/api/candidates", params={"verdict": "condemn"}).json()
        detail = client.get(f"/api/candidates/{rows[0]['id']}")
        assert detail.status_code == 200
        body = detail.json()
        assert body["explanation_unreadable"] is True
        # The row's own score survives -- it is a column, not part of the document.
        assert body["explanation"]["score"] == 80.0
        # And no threshold is invented: the panel drops that clause rather than state a
        # policy setting that is not the operator's.
        assert body["explanation"]["threshold"] is None

    def test_a_readable_row_is_not_flagged(self, client: TestClient) -> None:
        """The flag means what it says: only a row that really would not read back.

        Without this the degradation would be untestable -- a panel that always claimed
        the reasons were missing would pass every assertion above it.
        """
        rows = client.get("/api/candidates", params={"verdict": "protect"}).json()
        row = next(r for r in rows if r["media_key"] == "radarr:1:7")
        body = client.get(f"/api/candidates/{row['id']}").json()
        assert body["explanation_unreadable"] is False
        assert body["explanation"]["threshold"] == 70
        assert body["explanation"]["protections_fired"][0]["detail"] == "you spared it by hand"


class TestThePanelAndTheReapAgreeOnUnreadable:
    """One definition of "unreadable", checked from both ends (rule 104, #142).

    These two used to answer it separately. The panel's test was ``Explanation(**decoded)``
    raising, which is any bad field anywhere in the document; the reap path's was a non-dict
    test over the two protections lists alone. ``radarr:1:5`` in the table above is a live
    instance of the gap -- an entry missing ``detail`` -- and it degraded the panel while a
    hand Reap on it still condemned, which is the one thing ``engine.verdict`` says in writing
    it will not do. Both now call ``engine.explanation.read_explanation``.

    The implication is only worth asserting if BOTH classes are populated, so the counts are
    pinned rather than left to the fixture (rule 118): a table that drifted to all-readable
    would satisfy "unreadable implies protect" vacuously and read as a proof.
    """

    def _rows(self) -> dict[str, Candidate]:
        now = utcnow()
        return {
            media_key: Candidate(
                snapshot_id=1,
                media_key=media_key,
                title="Example Movie",
                media_type="movie",
                size_bytes=1_000_000_000,
                verdict=VERDICTS[media_key],
                score=80,
                coverage_bp=10_000,
                explanation_json=explanation,
                created_at=now,
            )
            for media_key, explanation in {**MALFORMED, "radarr:1:7": HEALTHY}.items()
        }

    def test_a_panel_that_cannot_render_never_lets_the_reap_through(self) -> None:
        rows = self._rows()
        degraded = {k for k, row in rows.items() if _explanation_out(row).unreadable}
        reaps = {
            k: reap_override_verdict(str(row.explanation_json), score=int(row.score))
            for k, row in rows.items()
        }

        # Pin both populations by hand (rule 145): five of the seven stored rows here cannot
        # be rendered. Two can, and they are the ones that make this test discriminate --
        # ``radarr:1:6`` carries a non-dict ``match``, which ``Explanation._thaw_match`` reads
        # as an absent block rather than failing the document, so the panel renders it and its
        # reap is held by the bad match instead. If either set moves, the table changed and
        # the implication below may have gone vacuous.
        assert len(rows) == 7
        assert degraded == {f"radarr:1:{n}" for n in range(1, 6)}
        assert rows.keys() - degraded == {"radarr:1:6", "radarr:1:7"}

        condemned_but_blank = sorted(k for k in degraded if reaps[k] == "condemn")
        assert not condemned_but_blank, (
            "these rows render an empty why panel and a hand Reap on them still condemns, so "
            "the operator consents to reasons nobody showed them (#142). api.routes."
            "_explanation_out and services.condemned.reap_override_verdict_decoded must both "
            f"read engine.explanation.read_explanation: {condemned_but_blank}"
        )

    def test_the_readable_control_is_still_reapable(self) -> None:
        """Or "unreadable holds the reap" is indistinguishable from "nothing is reapable"."""
        row = self._rows()["radarr:1:7"]
        assert _explanation_out(row).unreadable is False
        assert reap_override_verdict(str(row.explanation_json), score=int(row.score)) == "condemn"


class TestTheExtractorsThemselves:
    @pytest.mark.parametrize("verdict", ["protect", "condemn", "abstain"])
    def test_a_non_object_top_level_yields_nothing(self, verdict: str) -> None:
        for raw in ("[1, 2]", "null", "not json", '"a string"', "7"):
            exp = _decode_explanation(raw)
            assert exp is None
            assert _primary_reason(exp, verdict, 50) is None
            assert _chip(exp, verdict, 50) is None
            assert _dormant_for(exp) is None

    def test_entries_that_are_not_objects_are_dropped(self) -> None:
        exp = _decode_explanation(MALFORMED["radarr:1:4"])
        assert exp is not None
        assert _primary_reason(exp, "protect", 50) is None

    def test_an_entry_without_a_detail_yields_nothing(self) -> None:
        exp = _decode_explanation(MALFORMED["radarr:1:5"])
        assert exp is not None
        assert _primary_reason(exp, "protect", 50) is None

    def test_a_non_dict_match_says_it_could_not_be_read(self) -> None:
        """A match block that is THERE but unreadable holds a hand reap
        (``condemned.match_state``), so the card has to say that.

        It used to fall through to "Scored below your threshold" -- a confident sentence
        asserting the opposite of the decision in force on the very same row: the queue
        said the item merely scored low while the reap-override read was holding it as a
        bad match (rule 61).
        """
        exp = _decode_explanation(MALFORMED["radarr:1:6"])
        assert exp is not None
        reason = _primary_reason(exp, "abstain", 50)
        assert reason == "Kept to be safe: Reaper couldn't read what this matched in Plex."
        # Still not an AttributeError, and still not claiming a status Plex reported.
        assert "threshold" not in (reason or "")

    def test_a_signals_block_that_is_not_a_list_hides_the_pill(self) -> None:
        exp = _decode_explanation(json.dumps({"signals": "nope"}))
        assert exp is not None
        assert _dormant_for(exp) is None


class TestTheReapOverrideReadKeepsTheFile:
    def test_a_non_dict_match_keeps_it(self) -> None:
        """A match block that is THERE but unreadable is a match we could not check.

        It holds the reap, exactly as "unmatched" and "ambiguous" do. Reading it as a
        clean match instead would turn evidence nobody could read into evidence that
        nothing was wrong, which is the one direction this codebase never fails.
        """
        assert reap_override_verdict(MALFORMED["radarr:1:6"], score=99) == "protect"

    def test_an_absent_match_block_is_not_treated_as_a_bad_one(self) -> None:
        """A row scored before the match block shipped carries none, and was judged fine
        without it. Absent is not the same as unreadable."""
        clean = json.dumps(
            {
                "score": 40.0,
                "threshold": 70,
                "coverage": 1.0,
                "signals": [],
                "protections_fired": [],
                "protections_checked": [],
                "protections_unknown": [],
            }
        )
        assert reap_override_verdict(clean, score=40) == "condemn"
        assert reap_override_verdict(
            json.dumps({**json.loads(clean), "match": None}), score=40
        ) == ("condemn")

    @pytest.mark.parametrize("raw", ["[1, 2]", "null", "not json", '"a string"'])
    def test_an_unreadable_document_keeps_it(self, raw: str) -> None:
        assert reap_override_verdict(raw, score=99) == "protect"
