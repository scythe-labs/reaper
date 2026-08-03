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

import ast
import json
import re
from collections.abc import Iterator
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine as sa_create_engine
from sqlalchemy.orm import Session

from reaper.api.routes import (
    _chip,
    _decode_explanation,
    _explanation_out,
    _kept_phrase,
    _primary_reason,
    _season_number,
)
from reaper.clock import utcnow
from reaper.config import Settings
from reaper.db.base import Base
from reaper.db.models import Candidate, Snapshot
from reaper.engine import identity
from reaper.engine.dormancy import dormancy_days, reference_instant
from reaper.engine.gates import (
    ABSTAIN,
    PROTECT,
    Evaluation,
    Facts,
    GateConfig,
    GateId,
    GateResult,
    MinDormancyGate,
    ServerPopularityGate,
)
from reaper.engine.observation import Absent, Known
from reaper.engine.policy import DEFAULT_MOVIE_POLICY
from reaper.engine.signals import Score
from reaper.main import create_app
from reaper.services.condemned import (
    BAD_MATCH_STATES,
    MATCH_UNREADABLE,
    reap_override_verdict_decoded,
)
from reaper.services.season_pruning import PruneConflict, SeriesPrunePlan
from reaper.services.season_scan import _NO_KEY_REASONS as SEASON_NO_KEY_REASONS
from reaper.services.season_scan import guard_result
from reaper.services.snapshot import _NO_KEY_REASONS as MOVIE_NO_KEY_REASONS
from reaper.services.snapshot import HAND_SPARE_DETAIL, _explain

from ._auth import login

#: A named reason that cannot reach the why panel's CAUSE slot, and why. ``CAUSE_COPY`` fills
#: the tail of "could not check {what}: {reason}", which only a BLOCKED gate produces, so a
#: reason the operator meets somewhere else is not a gap. Everything else the walk finds must
#: have panel copy. Each excuse is a claim about wiring, so
#: ``test_the_reason_with_no_panel_route_still_has_one_way_out`` checks it rather than
#: trusting it; classified in writing rather than silenced (rule 103).
_NO_PANEL_ROUTE = {
    "facts_codec.NOT_RECORDED_REASON": (
        "facts_from_dict has one caller, the policy simulator, which reads a re-decided "
        "score and verdict and never builds or stores an Explanation"
    ),
    "executor._NO_APPROVED_SIZE_REASON": (
        "a run step's own skip reason, already a finished sentence the report prints as it "
        "stands; it never rides on a gate outcome"
    ),
    "season_pruning.PROGRESS_UNESTABLISHABLE_REASON": (
        "a ProtectedSeason.reason, shown on the season list as why that season is kept, and "
        "already plain: it answers 'why kept', not 'what could not be checked'"
    ),
    "season_pruning.PROGRESS_UNREADABLE_REASON": ("its twin above, same surface and same shape"),
    "season_pruning.PROGRESS_UNMATCHED_REASON": (
        "the third of the same set, same surface and same shape (#472)"
    ),
    "preview.NOT_PROBED_REASON": (
        "the facts a policy probe leaves out. probe_signal answers one route with a number "
        "and a detail string and builds no candidate, stores no Explanation and touches no "
        "snapshot, so nothing carries this reason to a panel"
    ),
}

#: Panel copy whose backend producer is gone, and why the entry stays. ``CAUSE_COPY`` renders
#: STORED explanations, so an entry outliving its producer is back-compat rather than dead
#: code -- but only while a stored row can still reach it, which is a claim about wiring and is
#: written down here rather than assumed (rule 103), the same way ``_NO_PANEL_ROUTE`` above
#: writes down the other direction's excuses.
_PANEL_COPY_WITHOUT_A_PRODUCER = {
    "season has no rank": (
        "season_scan records a season with no rank as Absent now (a special genuinely has no "
        "rank slot), so no fresh scan can build this detail. A snapshot frozen before that "
        "fix survives an upgrade -- migrations only add -- and the queue renders the latest "
        "snapshot until the next scan replaces it, so an operator who upgrades before "
        "re-scanning can still open a row carrying it. Deleting the entry would print the raw "
        "backend phrase at exactly that operator, which is what #282 closed."
    ),
}


def _cause_copy_source() -> str:
    """The ``CAUSE_COPY`` map as it is spelled in ``WhyPanel.tsx``.

    Read as source text rather than imported, because the walks either side of this file's
    tree boundary have no other way to see a TSX literal. Three tests read it, so it is
    derived once (rule 119)."""
    panel = (
        Path(__file__).resolve().parents[1] / "frontend" / "src" / "components" / "WhyPanel.tsx"
    ).read_text(encoding="utf-8")
    return panel.split("const CAUSE_COPY", 1)[1].split("};", 1)[0]


def _reason_constants() -> dict[str, str]:
    """Every module-level ``*_REASON`` string under ``src/reaper``, as ``file.NAME -> value``.

    Discovered rather than listed, so naming a new reason is all it takes to be covered
    (rule 145: a hand-written list can only ever pin the members somebody remembered, which
    is the hole #282 came through). Walks the source rather than importing, so a module with
    a heavy import graph costs nothing here.
    """
    found: dict[str, str] = {}
    for path in sorted((Path(__file__).resolve().parents[1] / "src" / "reaper").rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in tree.body:
            targets = (
                [node.target] if isinstance(node, ast.AnnAssign) else getattr(node, "targets", [])
            )
            value = getattr(node, "value", None)
            for target in targets:
                if (
                    isinstance(target, ast.Name)
                    and target.id.endswith("_REASON")
                    and isinstance(value, ast.Constant)
                    and isinstance(value.value, str)
                ):
                    found[f"{path.stem}.{target.id}"] = value.value
    return found


def _conflict_detail(*, kept_watchers: int | None = 0, shortfall: str | None = None) -> str:
    """A real keep-rule conflict's stored detail, from the one producer that words it.

    Built rather than transcribed (rule 119): a hand-typed copy of this sentence had
    already drifted from ``PruneConflict.message``, which is how the chip below it went
    on asserting a comparison the message itself had stopped making.

    Two of the three shapes matter here, and neither made a comparison.
    ``kept_watchers=None`` is not "nobody watched the kept season", it is "that season's
    history could not be read at all"; ``shortfall`` is a count taken over a mirror that
    begins after the season arrived, so it is a lower bound rather than an answer. Both are
    raised as conflicts rather than let an unestablished number clear a protection, and the
    message states each in its own words.
    """
    return PruneConflict(
        pruned_season=3,
        kept_season=1,
        pruned_watchers=2,
        kept_watchers=kept_watchers,
        kept_reason="within the last 2 seasons (rank 1)",
        shortfall=shortfall,
    ).message


#: The counted shape: a comparison really was made, so the chip may state it.
CONFLICT_SENTENCE = _conflict_detail(kept_watchers=0)


def _drop_defers_key(explanation_json: str) -> str:
    """A frozen explanation aged back one generation: every ``defers_to_owner`` key removed.

    The pre-flag row is the state no writer can produce any more, and hand-building one would
    be a second copy of the writer's shape (rule 119) -- which is exactly how a test comes to
    pin a row the producer never emitted. Derived from a real frozen row instead, so it drifts
    with it.
    """
    exp = json.loads(explanation_json)
    for entry in exp["protections_unknown"]:
        entry.pop("defers_to_owner", None)
    return json.dumps(exp)


def _never_played_facts(days_dormant: int) -> Facts:
    """A title with no plays at all -- the shape the review queue's Sanctuary lane is full
    of, and the one the dormancy chip used to describe as recently watched.

    Both watcher counts are a genuine zero, not an unreadable source: this is a file the
    server has simply never served, so ``distinct_watchers_all_time`` is 0 as well.
    """
    return Facts(
        title="A Film",
        days_observed_unwatched=Known(value=float(days_dormant), source="tautulli"),
        distinct_watchers=Known(value=0, source="tautulli"),
        distinct_watchers_all_time=Known(value=0, source="tautulli"),
        size_bytes=Known(value=12_000_000_000, source="radarr"),
        imdb_rating_tenths=Known(value=72, source="imdb"),
        imdb_votes=Known(value=5000, source="imdb"),
        season_rank=Absent(source="radarr"),
        is_streaming_now=Known(value=False, source="plex"),
        is_managed=Known(value=True, source="radarr"),
        in_curated_list=Absent(source="lists"),
        is_whitelisted=Known(value=False, source="plex"),
    )


def _popularity_short_history_facts() -> Facts:
    """A title nobody played, judged against a watch mirror three months deep.

    The count is a lower bound rather than an answer, which is what makes the popularity
    gate report the protection as un-checked instead of un-fired.
    """
    return replace(
        _never_played_facts(900),
        history_reach_days=Known(value=90.0, source="tautulli"),
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
                "well rated: 6.8 on IMDb from 722,243 votes",
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
                "hasn't sat untouched long enough",
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
            ("season_progression", "episodes are missing from this season", "episodes are missing"),
            (
                "season_progression",
                "the newest season of a show that is still running",
                "the show is still running",
            ),
            (
                "season_progression",
                "the earliest season on disk, so there is somewhere to start",
                "the earliest season stays",
            ),
            # A snapshot stored before these three were reworded carries the retired
            # spelling, and degrades to the generic phrase rather than to a wrong one.
            (
                "season_progression",
                "Sonarr is still downloading this season",
                "your season rule keeps it",
            ),
            ("season_progression", "currently airing", "your season rule keeps it"),
            (
                "season_progression",
                "the first season is kept so the show can still be started",
                "your season rule keeps it",
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
            # Not a keep rule at all: the mirror is too short to answer who is mid-binge.
            # The generic phrase would name a control the operator can edit and that will
            # not move it, sending them to lower keep-last while the real lever is the
            # depth of their watch history (or the hold set against it).
            (
                "season_progression",
                "your watch history is too short to tell who is part-way through",
                "your watch history is too short to tell",
            ),
            ("season_progression", "some future wording", "your season rule keeps it"),
            ("custom", "your rule: genre is Documentary", "by your rule"),
            ("brand_new_gate", "whatever it says", "a protection applies"),
        ],
    )
    def test_phrase(self, gate: str, detail: str, phrase: str) -> None:
        assert _kept_phrase(gate, detail) == phrase


class TestTheKeptChipNeverClaimsAPlayThatDidNotHappen:
    """A title nobody has ever played reaches ``MinDormancyGate``'s fired branch, because
    that gate's clock runs from the day the file arrived whenever there is no play to run
    it from (``engine.dormancy.reference_instant``). The chip beside it used to read
    "watched too recently" -- a plain fabrication about a title with zero plays all time,
    printed three lines under the same panel's "nobody watched it in the last year".
    ``MinDormancyGate`` words its own detail "untouched" for exactly this reason; only the
    chip was still claiming the play.

    An agreement test, not a transcription (rule 119): it derives the dormancy through the
    real helper with no play, runs the real gate, and hands the real detail to the real
    chip, so rewording either half alone fails here rather than drifting quietly.
    """

    #: A file that arrived recently and was never once played, under an operator waiting a
    #: year. Its whole life on the server is shorter than the wait, so the gate fires.
    ARRIVED_DAYS_AGO = 89
    WAITS_DAYS = 365

    def _phrase_for_a_never_played_title(self) -> str:
        # One clock reading, not three: a test that re-samples can straddle a day boundary
        # between the reference and the count and drop a day (rule 133).
        now = utcnow()
        reference = reference_instant(
            last_played=None,
            added_at=now - timedelta(days=self.ARRIVED_DAYS_AGO),
            # The watch mirror reaches back further than the file has existed, so the
            # horizon is not what is being measured here -- the arrival date is.
            horizon=now - timedelta(days=400),
        )
        facts = _never_played_facts(dormancy_days(reference, now=now))
        result = MinDormancyGate(
            GateConfig(GateId.MIN_DORMANCY, threshold=self.WAITS_DAYS)
        ).evaluate(facts)

        assert result.outcome == PROTECT, "the gate must fire for this chip to exist at all"
        return _kept_phrase("min_dormancy", result.detail)

    def test_the_chip_asserts_no_play(self) -> None:
        """The regression itself. Nobody watched this, so the chip may not say anyone did."""
        phrase = self._phrase_for_a_never_played_title()

        assert "watched" not in phrase
        assert "played" not in phrase

    def test_the_chip_still_names_why_it_is_kept(self) -> None:
        """Truthful is not enough on its own -- the chip's whole job is to say why."""
        assert self._phrase_for_a_never_played_title() == "hasn't sat untouched long enough"


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
                        "detail": "well rated: 6.8 on IMDb from 722,243 votes",
                    }
                ],
            ),
            "protect",
            90,
        )
        assert chip is not None
        assert chip.tone == "kept"
        assert chip.text == "Kept, well rated: 6.8 on IMDb"

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

    @pytest.mark.parametrize(
        ("media_type", "app"), [("movie", "Radarr"), ("season", "Sonarr")], ids=["movie", "season"]
    )
    def test_a_conflicted_match_names_the_app_that_disagrees(
        self, media_type: str, app: str
    ) -> None:
        """A disagreement is NOT a duplicate, and saying so sends the operator hunting for a
        second copy that is not there. It gets its own chip, naming the app whose metadata
        has to change -- which differs by media type, so both are pinned (rule 141: the
        movie arm alone would pass on a hardcoded "Radarr")."""
        chip = _chip(_exp(50, match_status="conflicted"), "abstain", 50, media_type)
        assert chip is not None
        assert (chip.tone, chip.text) == ("quiet", f"Plex and {app} don't agree")
        assert chip.why == f"Plex and {app} don't agree about it"
        # And it must not fall through to the multiplicity wording it used to share.
        assert "more than one" not in chip.text
        assert "two different things" not in chip.text

    @pytest.mark.parametrize(
        ("media_type", "expected"),
        [
            ("movie", "Kept to be safe: Plex and Radarr describe this file differently."),
            ("season", "Kept to be safe: Plex and Sonarr describe this show differently."),
        ],
        ids=["movie", "season"],
    )
    def test_the_card_reason_for_a_conflicted_match(self, media_type: str, expected: str) -> None:
        """The Reap-page line, swept with the chip (rule 72): both read the same status."""
        assert _primary_reason(_exp(50, match_status="conflicted"), "abstain", 50, media_type) == (
            expected
        )

    def test_season_conflict_wants_eyes(self) -> None:
        """The keep-rule conflict is a deliberate left-for-you flag, not a plumbing
        failure -- it wears the amber-outline tone."""
        chip = _chip(
            _exp(
                82,
                unknown=[
                    {
                        "gate": "season_progression",
                        "detail": CONFLICT_SENTENCE,
                        "defers_to_owner": True,
                    }
                ],
            ),
            "abstain",
            82,
        )
        assert chip is not None
        assert chip.tone == "look"
        assert chip.text == "Needs a look, watched more than a season your rule keeps"

    def test_an_unreadable_kept_season_claims_no_comparison(self) -> None:
        """The twin of the dormancy chip's fabricated play, one gate over.

        A conflict is ALSO raised when the kept season's watcher count could not be read,
        and the operator is shown only this chip on a queue card. Claiming the season
        "watched more than" another states arithmetic against a number nobody took --
        the sentence ``detect_conflicts`` deliberately removed from the message, which
        the chip went on printing one line above the panel's own denial.
        """
        detail = _conflict_detail(kept_watchers=None)
        assert "could not check who watched" in detail, "the producer stopped saying this"

        chip = _chip(
            _exp(
                82,
                unknown=[
                    {"gate": "season_progression", "detail": detail, "defers_to_owner": False}
                ],
            ),
            "abstain",
            82,
        )

        assert chip is not None
        assert chip.tone == "look"
        assert "watched more than" not in chip.text
        assert chip.text == "Needs a look, couldn't check who watched these seasons"

    def test_a_conflict_the_mirror_could_not_settle_shares_that_chip(self) -> None:
        """The third shape, and why that chip stopped naming the KEPT season.

        A count taken over a mirror that begins after the season arrived is a lower bound,
        so no comparison against it settles anything -- and the season it could not
        establish is just as often the one being REMOVED as the one being kept. The two
        non-comparisons share one flag, so they share one chip, and its copy has to be true
        of both.
        """
        detail = _conflict_detail(shortfall="your watch history only goes back 12 months")
        assert "cannot tell whether" in detail, "the producer stopped saying this"

        chip = _chip(
            _exp(
                82,
                unknown=[
                    {"gate": "season_progression", "detail": detail, "defers_to_owner": False}
                ],
            ),
            "abstain",
            82,
        )

        assert chip is not None
        assert chip.tone == "look"
        assert "watched more than" not in chip.text
        assert chip.text == "Needs a look, couldn't check who watched these seasons"

    def test_a_row_frozen_before_the_flag_names_neither_shape(self) -> None:
        """A stored row that predates ``defers_to_owner`` carries nothing that can tell a
        made comparison from a refused one, so the chip claims neither and says the
        vague-but-true thing instead.

        Recovering it from the wording was tried and is exactly the bug: the chip read
        "more than watched Season" as a deferral while ``reap_override_verdict`` read the
        absent key as a hold, so the card offered the operator a conflict to settle and then
        refused the reap by quoting that same conflict back at them. The reap side of that
        pair has since stopped holding on any block at all, which closes the contradiction
        from the other end -- the chip's "left for you to decide" is now true of every shape.
        The chip must still not claim a comparison it has no evidence was made, which is what
        this pins, and the pair is swept in full below."""
        for kept_watchers in (1, None):
            detail = _conflict_detail(kept_watchers=kept_watchers)
            legacy = _exp(82, unknown=[{"gate": "season_progression", "detail": detail}])

            chip = _chip(legacy, "abstain", 82)

            assert chip is not None
            assert chip.tone == "look"
            assert chip.text == "Needs a look, left for you to decide"
            assert chip.why == "a check on it couldn't be settled"
            # ...and the invitation the chip extends is one the engine honors.
            assert reap_override_verdict_decoded(legacy, score=82) == "condemn"

    @pytest.mark.parametrize("junk", ['"true"', '"false"', '"0"', "1", "0", "[]", "{}", "null"])
    def test_only_a_real_json_true_claims_the_comparison_was_made(self, junk: str) -> None:
        """``_chip`` reads ``defers_to_owner`` with ``is True``, and that strictness is the
        whole guard on the one chip that ASSERTS something about the evidence.

        A stored row is written by whatever version was running, so this key can come back
        as a string, a number, or a container. Only a real JSON ``true`` means "Reaper made
        the comparison and the rule lost it"; everything else means the row cannot tell,
        and must fall to the vague-but-true chip rather than telling the operator a
        comparison happened. `"false"`, `"0"` and `0` are the sharp ones -- all three are
        *truthy* as strings or falsy as numbers in ways a loose test gets backwards.

        This sweep used to live on the reap decision, which read the same key the same way.
        That reader stopped reading it when a blocked gate stopped holding a hand reap, so
        the sweep became constant-true there and was moved here, to the consumer that
        actually inherited the strictness (rule 132: the citation must be a live one).
        """
        detail = json.dumps(_conflict_detail(kept_watchers=1))
        entry = json.loads(
            f'{{"gate": "season_progression", "detail": {detail}, "defers_to_owner": {junk}}}'
        )

        chip = _chip(_exp(82, unknown=[entry]), "abstain", 82)

        assert chip is not None
        assert chip.text == "Needs a look, left for you to decide"
        assert "watched more than" not in chip.text

    @pytest.mark.parametrize("junk", ['"true"', '"false"', '"0"', "1", "0", "[]", "{}", "null"])
    def test_the_panel_reads_the_junk_the_chip_reads_and_keeps_rendering(self, junk: str) -> None:
        """The same sweep as the chip's, against the panel, which had it too and disagreed (#112).

        Two readers of one stored byte, and two coercion rules (rule 104). The chip tests it
        with ``is True`` / ``is False``, so everything above falls to its vague chip. The panel
        read the same byte through Pydantic's LAX bool coercion, where ``1`` and ``"true"``
        become ``True``, ``"0"`` and ``0`` become ``False``, and ``[]``/``{}`` are refused
        outright -- and a refusal is the worst of the three, because it fails the enclosing
        ``Explanation`` rather than the one field: ``_explanation_out`` then serves its degraded
        body, and the operator gets a panel with no signals, no protections and no threshold,
        beside a chip that read the row perfectly well and a hand Reap that still condemns.

        So this asserts BOTH halves on one row: nothing is degraded, and the flag reads
        ``None`` -- the state the field already means "nothing here can tell a comparison
        Reaper made from one it refused" -- exactly where the chip declines to claim one.
        The three real writer shapes are swept in
        ``test_the_panel_is_served_the_flag_its_chip_reads`` below; this is everything else a
        row can carry.
        """
        detail = json.dumps(_conflict_detail(kept_watchers=1))
        entry = json.loads(
            f'{{"gate": "season_progression", "detail": {detail}, "defers_to_owner": {junk}}}'
        )
        exp = _exp(82, unknown=[entry])
        row = Candidate(
            media_key="sonarr:1:2:3",
            explanation_json=json.dumps(exp),
            score=82,
            coverage_bp=10_000,
        )

        panel = _explanation_out(row)

        # The whole explanation survives one unreadable byte, threshold included -- the panel
        # prints "your threshold is N" beside the score, and the degraded body has no N.
        assert not panel.unreadable
        assert panel.body.threshold == 70
        unknown = panel.body.protections_unknown
        assert [o.gate for o in unknown] == ["season_progression"]
        assert unknown[0].defers_to_owner is None

        # And the chip beside it says the same thing about the same row.
        chip = _chip(exp, "abstain", 82)
        assert chip is not None
        assert chip.text == "Needs a look, left for you to decide"

    @pytest.mark.parametrize("junk", ["70.5", '"abc"', "true", "[]", '{"a": 1}'])
    def test_an_illegible_threshold_costs_its_own_clause_and_nothing_else(self, junk: str) -> None:
        """``threshold`` is ``defers_to_owner``'s twin, and rule 72 binds the fix to both.

        Same shape exactly: one stored byte, three readers, two coercion rules (rule 104).
        ``_chip`` and ``_primary_reason`` each tested it with ``isinstance(value, int)`` and
        coped with anything else; ``Explanation.threshold`` read it through pydantic's lax
        ``int | None``, which REFUSES ``70.5`` and ``"abc"`` -- and the refusal fails the
        enclosing model rather than the one field, so the operator lost every signal, every
        protection and the match block too, beside a chip that read the same row fine.

        ``true`` is in the sweep because Python calls a ``bool`` an ``int``: it used to reach
        the panel as a threshold of 1, which is not a score, it is a row nobody can read.
        """
        exp = json.loads(
            f'{{"threshold": {junk}, "score": 82, "coverage": 1.0, "signals": [], '
            '"protections_fired": [], "protections_checked": [], '
            '"protections_unknown": []}'
        )
        row = Candidate(
            media_key="sonarr:1:2:3",
            explanation_json=json.dumps(exp),
            score=82,
            coverage_bp=10_000,
        )

        panel = _explanation_out(row)

        # The panel survives, and pays for the unreadable byte with exactly the one clause it
        # cannot honestly print -- never an invented figure, and never the whole body.
        assert not panel.unreadable
        assert panel.body.threshold is None
        assert panel.body.score == 82

        # And the chip beside it declines the same comparison rather than inventing one.
        chip = _chip(exp, "abstain", 82)
        assert chip is not None
        assert chip.text == "Below your threshold"

    @pytest.mark.parametrize("junk", ['"matched"', "5", "[]"])
    def test_a_match_block_of_the_wrong_shape_reads_as_absent(self, junk: str) -> None:
        """The other twin on the same model, and the same trade (rule 72).

        ``routes._match_status`` reads the stored match off the raw dict and copes with any
        shape. Refusing it at the wire boundary took every other block on the panel with it.
        ``None`` is a shape the panel already renders: it is what a row scanned before the
        match block existed carries.
        """
        exp = _exp(82, unknown=[])
        exp["match"] = json.loads(junk)
        row = Candidate(
            media_key="sonarr:1:2:3",
            explanation_json=json.dumps(exp),
            score=82,
            coverage_bp=10_000,
        )

        panel = _explanation_out(row)

        assert not panel.unreadable
        assert panel.body.match is None
        assert panel.body.threshold == 70

    def test_the_writer_and_the_chip_are_connected_by_a_real_frozen_row(self) -> None:
        """The producer -> consumer link for ``defers_to_owner``, end to end through the
        REAL writer (``snapshot._explain``) rather than a hand-built dict.

        Every other chip test builds its explanation by hand -- so ``_explain`` could stop
        writing the key entirely and nothing would fail, while every season-conflict chip
        silently degraded to the legacy "names neither shape" wording above. That is rule
        142's supply chain one link short, and rule 132's "the fixture is the claim". This
        is the link. The panel is the field's other reader and has its own link, in
        ``test_the_panel_is_served_the_flag_its_chip_reads`` below.
        """
        made = GateResult(
            GateId.SEASON_PROGRESSION,
            ABSTAIN,
            detail=_conflict_detail(kept_watchers=1),
            blocked=True,
            defers_to_owner=True,
        )
        refused = replace(made, defers_to_owner=False)

        for result, expected in ((made, "watched more than"), (refused, "couldn't check")):
            frozen = json.loads(
                _explain(
                    Evaluation(results=[result]),
                    Score(value=82.0, coverage=1.0, results=[]),
                    DEFAULT_MOVIE_POLICY,
                )
            )

            chip = _chip(frozen, "abstain", 82)

            assert chip is not None, expected
            assert expected in chip.text, chip.text

    def test_the_panel_is_served_the_flag_its_chip_reads(self) -> None:
        """The why panel's half of ``defers_to_owner``'s supply chain, end to end (#86).

        The chip and the panel it opens read the same stored row, but they read it across
        different boundaries: the chip off the raw dict, the panel through
        ``api.schemas.Explanation``, which drops any key it does not declare. It did not
        declare this one, so for the panel the flag did not exist and its predicate went on
        running the retired wording test -- promising a comparison the reason block below it
        denied. Rule 142: shipping the field to a consumer across a serialization boundary is
        part of the fix, not a follow-up.

        All three generations, because the third is the one a ``bool`` field would erase: a
        row frozen before the flag carries no key and must arrive as ``None``, not ``False``.
        """
        made = GateResult(
            GateId.SEASON_PROGRESSION,
            ABSTAIN,
            detail=_conflict_detail(kept_watchers=1),
            blocked=True,
            defers_to_owner=True,
        )
        frozen = {
            True: _explain(
                Evaluation(results=[made]),
                Score(value=82.0, coverage=1.0, results=[]),
                DEFAULT_MOVIE_POLICY,
            ),
            False: _explain(
                Evaluation(results=[replace(made, defers_to_owner=False)]),
                Score(value=82.0, coverage=1.0, results=[]),
                DEFAULT_MOVIE_POLICY,
            ),
            # No writer emits this any more; it is what is already on disk. Built by dropping
            # the key from a real frozen row, so it cannot drift from the shape above.
            None: _drop_defers_key(
                _explain(
                    Evaluation(results=[made]),
                    Score(value=82.0, coverage=1.0, results=[]),
                    DEFAULT_MOVIE_POLICY,
                )
            ),
        }

        for expected, explanation_json in frozen.items():
            row = Candidate(
                media_key="sonarr:1:2:3",
                explanation_json=explanation_json,
                score=82,
                coverage_bp=10_000,
            )

            panel = _explanation_out(row)

            assert not panel.unreadable, expected
            unknown = panel.body.protections_unknown
            assert [o.gate for o in unknown] == ["season_progression"], expected
            assert unknown[0].defers_to_owner is expected

    def test_a_check_that_never_ran_reaches_the_panel_saying_so(self) -> None:
        """The twin of the test above, for ``unestablishable`` (rule 72), and the seam the
        two halves of #486 meet at.

        The guard's own output, frozen by the real ``_explain`` and read back through the
        real ``Explanation``. Two things have to survive that trip and neither is visible
        from either side alone: the row must leave ``protections_checked``, where it was
        claiming a pass, and it must arrive carrying the flag ``WhyPanel.keepRuleConflict``
        reads. A key ``Explanation`` does not declare is dropped silently, and the panel then
        offers to settle a comparison nobody attempted.

        The pre-flag generation rides on ``defers_to_owner``'s test above: a row frozen
        before either key reads ``None`` for both.
        """
        never_ran = guard_result(
            SeriesPrunePlan(series_title="Show", prunable=[2]),
            2,
            progress_unknown_reason="Plex has not matched this season",
        )
        frozen = json.loads(
            _explain(
                Evaluation(results=[never_ran]),
                Score(value=10.0, coverage=0.37, results=[]),
                DEFAULT_MOVIE_POLICY,
                match_status=identity.MatchStatus.UNMATCHED,
            )
        )
        assert frozen["protections_checked"] == []

        panel = _explanation_out(
            Candidate(
                media_key="sonarr:1:2:3",
                explanation_json=json.dumps(frozen),
                score=10,
                coverage_bp=3_700,
            )
        )

        assert not panel.unreadable
        unknown = panel.body.protections_unknown
        assert [o.gate for o in unknown] == ["season_progression"]
        assert unknown[0].unestablishable is True
        assert unknown[0].detail == never_ran.detail

    def test_the_chip_and_the_reap_decision_never_disagree(self) -> None:
        """Rule 92 held end to end: no chip may promise a reap the engine will refuse.

        Both conflict shapes and all three row generations, against the real
        ``reap_override_verdict``. Every chip here opens with "Needs a look", which invites a
        decision, so every one of them must pair with ``condemn`` -- and it does now, because
        a block stopped holding a hand reap. The flag still picks WHICH sentence they read,
        and that is asserted in the tests above; what it may not do is pick a sentence the
        deletion path contradicts.

        The rows the reap still refuses are swept beside them, or this would only be able to
        fail in one direction: a bad Plex match and an unreadable protections list keep the
        file, and neither wears a chip that invites the operator to remove it."""
        for kept_watchers in (1, None):
            detail = _conflict_detail(kept_watchers=kept_watchers)
            for flag in ({"defers_to_owner": True}, {"defers_to_owner": False}, {}):
                exp = _exp(82, unknown=[{"gate": "season_progression", "detail": detail, **flag}])

                chip = _chip(exp, "abstain", 82)
                verdict = reap_override_verdict_decoded(exp, score=82)

                assert chip is not None
                assert chip.text.startswith("Needs a look"), f"{chip.text!r} for {flag}"
                assert verdict == "condemn", f"{chip.text!r} vs {verdict!r} for {flag}"

        # The other direction. A row the reap refuses must not be wearing an invitation.
        held = {
            "an unmatched row": _exp(82, match_status="unmatched"),
            "an ambiguous row": _exp(82, match_status="ambiguous"),
            "an unreadable protections list": {
                **_exp(82),
                "protections_unknown": ["a string where an object belongs"],
            },
        }
        for label, exp in held.items():
            chip = _chip(exp, "abstain", 82)

            assert reap_override_verdict_decoded(exp, score=82) == "protect", label
            assert chip is None or not chip.text.startswith("Needs a look"), label

    def test_any_future_deliberate_flag_still_wants_eyes(self) -> None:
        """A blocked detail that is a sentence of its own (not "could not check") is a
        deliberate flag whatever gate raised it -- fail toward showing it loudly."""
        chip = _chip(
            _exp(60, unknown=[{"gate": "custom", "detail": "A rule asked a human to look."}]),
            "abstain",
            60,
        )
        assert chip is not None
        assert (chip.tone, chip.text) == ("look", "Needs a look, left for you to decide")

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

    def test_a_history_too_short_for_the_window_reads_as_a_check_that_could_not_run(
        self,
    ) -> None:
        """The popularity gate's reach block, from the production gate rather than a
        retyped string, because the ``could not check`` prefix is what routes it here.

        A mirror shallower than the popularity window makes the watcher count a lower
        bound, so the gate reports the protection as un-checked. That is a plumbing
        failure, not a decision left to the owner, and the two must not wear the same
        chip. Nothing in ``verdict`` enforces the prefix for this gate -- the reap is held
        by the gate id -- so this chip and ``WhyPanel``'s check/cause split are the only
        places a reword shows up, which is why the assertion lives here.
        """
        result = ServerPopularityGate(
            GateConfig(GateId.SERVER_POPULARITY, threshold=3, window_days=365)
        ).evaluate(_popularity_short_history_facts())
        assert result.blocked is True

        chip = _chip(
            _exp(82, unknown=[{"gate": result.gate.value, "detail": result.detail}]),
            "abstain",
            82,
        )

        assert chip is not None
        assert (chip.tone, chip.text) == ("quiet", "Some checks couldn't run")
        assert "left for you to decide" not in chip.text

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


class TestTheReasonLineAgreesWithTheChip:
    """The card's chip and the reason line beneath it read the same explanation, so they
    must never describe two different decisions about one row.

    An abstain that reaches the end of ``_primary_reason`` was stopped either by the score
    or by the coverage floor, and the remedies are opposite: move the slider, or fix the
    evidence source. ``_chip`` made the distinction and ``_primary_reason`` did not, so the
    panel printed "Too little of it could be checked" directly above "Scored below your
    threshold." -- about a row whose score was over that threshold.

    Both halves call the real functions rather than a transcription (rule 119): the point
    is that the two agree, which a copy of either one cannot show.
    """

    def test_the_coverage_floor_is_not_reported_as_a_low_score(self) -> None:
        exp = _exp(82)  # threshold 70, so this abstain can only be the coverage floor
        reason = _primary_reason(exp, "abstain", 82)
        chip = _chip(exp, "abstain", 82)

        assert chip is not None
        assert chip.text == "Too little of it could be checked"
        assert reason == "Kept to be safe: too little of it could be checked."
        assert "threshold" not in (reason or "")

    def test_a_genuinely_low_score_still_says_so(self) -> None:
        """The other arm: below the threshold, the score really is the reason, and the
        line must not degrade to the coverage sentence for every abstain."""
        exp = _exp(42)
        reason = _primary_reason(exp, "abstain", 42)
        chip = _chip(exp, "abstain", 42)

        assert chip is not None
        assert chip.text == "Scored 42, under your 70"
        assert reason == "Scored below your threshold."

    def test_a_blocked_check_still_outranks_both(self) -> None:
        """The coverage branch sits last, so it can only be reached past every blocked
        case -- adding it must not swallow the unchecked protection above it."""
        exp = _exp(
            82,
            unknown=[{"gate": "min_dormancy", "detail": "could not check x: y"}],
        )
        assert _primary_reason(exp, "abstain", 82) == "could not check x: y"

    def test_an_explanation_with_no_threshold_falls_back_rather_than_guessing(self) -> None:
        """A stored row predating the threshold key, or carrying a malformed one, has no
        number to compare against. It reports the old line rather than asserting a
        coverage floor it cannot show fired."""
        for raw in ("{}", json.dumps({"threshold": "seventy"}), json.dumps({"threshold": None})):
            exp = _decode_explanation(raw)
            assert exp is not None
            assert _primary_reason(exp, "abstain", 82) == "Scored below your threshold."


class TestChipWhy:
    """The clause a chip carries for the refused-reap sentence.

    The frontend used to recover this by slicing "Kept, " off the chip text and looking
    the rest up in a transcribed copy of the strings above, so rewording one chip here
    silently dropped every held-reap explanation to a generic fallback -- and both sides
    of the contract were asserted from the same transcription, so nothing failed (H-1).
    The clause now ships beside the text, and these are the assertions that hold the two
    in step.
    """

    def test_a_fired_protection_says_the_same_words_without_the_lead(self) -> None:
        """ "Kept, playing right now" reads "kept for now: playing right now"."""
        chip = _chip(
            _exp(
                90, fired=[{"gate": "streaming_now", "detail": "someone is watching it right now"}]
            ),
            "protect",
            90,
        )
        assert chip is not None
        assert chip.text == f"Kept, {chip.why}"

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
                _exp(
                    82,
                    unknown=[
                        {
                            "gate": "season_progression",
                            "detail": CONFLICT_SENTENCE,
                            "defers_to_owner": True,
                        }
                    ],
                ),
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
            (
                _exp(
                    90,
                    fired=[{"gate": "curated_list", "detail": "on a protected list: imdb_top_250"}],
                ),
                "protect",
                90,
            ),
            (_exp(82, match_status="unmatched"), "abstain", 82),
            (_exp(50, match_status="ambiguous"), "abstain", 50),
            (
                _exp(
                    82,
                    unknown=[
                        {
                            "gate": "season_progression",
                            "detail": CONFLICT_SENTENCE,
                            "defers_to_owner": True,
                        }
                    ],
                ),
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
        capital lead, and none of the chip's own lead riding along inside the clause.

        The furniture used to be a middot and this asserted the clause held none. The
        separator is a comma now (#177), which made that assertion vacuous -- nothing
        produces a middot any more, so it could not fail. It names the leads instead.
        """
        chip = _chip(explanation, verdict, score)
        assert chip is not None
        assert chip.why is not None
        assert chip.why[0].islower()
        assert not chip.why.startswith(("Kept,", "Needs a look,"))


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
                title=f"Example Show, Season {number}",
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
                        unknown=[
                            {
                                "gate": "season_progression",
                                "detail": CONFLICT_SENTENCE,
                                "defers_to_owner": True,
                            }
                        ],
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
                                    "the earliest season on disk, so there is somewhere to start"
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
            "text": "Needs a look, watched more than a season your rule keeps",
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


class TestTheMatchStatusVocabulary:
    """The status is produced in one place and read in six, across two trees.

    Rule 144's shape: one fact about what happened to an item, written as a different
    sentence on every surface that shows it. Adding ``CONFLICTED`` widened the gap rather
    than closing it, because every ungenerated copy went on lumping it in with the wording
    it was split off from. These pin the whole vocabulary instead of the member that
    motivated the change.
    """

    def test_every_non_matched_status_holds_a_hand_reap(self) -> None:
        """The one that fails OPEN if it drifts. ``BAD_MATCH_STATES`` was a hand-written
        list of three, and a fourth status would have been reapable the moment it shipped:
        Reaper refusing to say what a file is, while offering to delete it. Derived from
        the enum now, so this asserts the derivation still covers the population."""
        for status in identity.MatchStatus:
            if status is identity.MatchStatus.MATCHED:
                assert status.value not in BAD_MATCH_STATES
            else:
                assert status.value in BAD_MATCH_STATES, (
                    f"{status.value!r} is a resolver outcome that is not a confident bind, so "
                    "it must hold a hand reap. See condemned.BAD_MATCH_STATES."
                )
        assert MATCH_UNREADABLE in BAD_MATCH_STATES

    @pytest.mark.parametrize(
        "status",
        ["garbage", "Ambiguous", "matched ", "{'a': 1}", "5"],
        ids=["unknown", "wrong-case", "trailing-space", "stringified-dict", "number"],
    )
    def test_a_status_this_build_does_not_know_still_holds_a_hand_reap(self, status: str) -> None:
        """The population the derivation above cannot cover, and the direction that matters.

        ``BAD_MATCH_STATES`` can only ever enumerate the statuses THIS build defines, so a
        stored value from outside that set -- a row frozen by a later build and then rolled
        back, or a corrupted explanation -- is not in it. Tested as membership that would
        read as a clean bind and let the reap through on a row the resolver never identified
        (rule 96). ``bad_match`` is phrased against the single clean value instead, so the
        unknown ones hold.
        """
        exp = {"match": {"status": status}, "protections_fired": [], "protections_unknown": []}
        assert status not in BAD_MATCH_STATES, "fixture must sit outside the derived set"
        assert reap_override_verdict_decoded(exp, score=99) == "protect", (
            f"a stored match status of {status!r} is not a confident bind, so a hand reap "
            "must be held. See condemned.MATCH_CLEAN."
        )

    def test_a_confident_bind_still_lets_a_hand_reap_through(self) -> None:
        """The other half of the pair, so the fix above cannot pass by holding everything.

        A complete document, because a reap is refused on any the why panel cannot render
        (#142) and a row carrying only the two protections lists is one of those -- which
        would hold this reap for a reason that has nothing to do with the bind, and quietly
        make the pair agree by holding everything after all."""
        exp = {
            "score": 99,
            "coverage": 1.0,
            "signals": [],
            "match": {"status": identity.MatchStatus.MATCHED.value},
            "protections_fired": [],
            "protections_checked": [],
            "protections_unknown": [],
        }
        assert reap_override_verdict_decoded(exp, score=99) == "condemn"

    @pytest.mark.parametrize(
        "reasons",
        [MOVIE_NO_KEY_REASONS, SEASON_NO_KEY_REASONS],
        ids=["movie", "season"],
    )
    def test_every_status_has_its_own_no_key_reason(
        self, reasons: dict[identity.MatchStatus | None, str]
    ) -> None:
        """Every non-matched status names its own cause, and no two share a string. A
        missing entry falls back to the ``unmatched`` wording, which is a wrong DEFINITE
        statement ("we couldn't find this in Plex") about an item Reaper did find."""
        wanted = {s for s in identity.MatchStatus if s is not identity.MatchStatus.MATCHED}
        assert set(reasons) == wanted
        assert len(set(reasons.values())) == len(wanted), "two statuses share one reason"

    def test_no_reason_is_typed_by_hand(self) -> None:
        """The gate under the copy check below, and the one that makes it complete.

        A reason written as a literal at its ``Unknown(...)`` site is invisible to every
        drift test, because there is no name to walk. That is exactly how five of them
        reached the owner in Reaper's own words while this file was green (#282): the list
        below could only ever check the reasons somebody remembered to name.

        So every reason is a constant, and this reads the call rather than the line
        (rule 147): an ``ast`` walk sees a reason split over two lines, wrapped in
        parentheses, or passed positionally, none of which a regex on ``Unknown(reason="``
        would catch. A local variable passes -- its value came from a constant or a map that
        the walk below covers -- and so does a stored value thawed off a dict.
        """
        walked = 0
        typed: list[str] = []
        for path in sorted((Path(__file__).resolve().parents[1] / "src" / "reaper").rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                if not (isinstance(node.func, ast.Name) and node.func.id == "Unknown"):
                    continue
                reason = next(
                    (kw.value for kw in node.keywords if kw.arg == "reason"),
                    node.args[0] if node.args else None,
                )
                if reason is None:
                    continue
                walked += 1
                if isinstance(reason, ast.Constant) and isinstance(reason.value, str):
                    typed.append(f"{path.name}:{reason.lineno}  {reason.value!r}")
        assert not typed, (
            "these reasons are typed at the site, so no drift test can see them and the\n"
            "why panel will print them raw at the owner. Name each one as a module\n"
            "constant ending in _REASON and it is covered automatically:\n  " + "\n  ".join(typed)
        )
        # The population this ban scans, reconciled by hand (rule 147): a reason that stops
        # being an ``Unknown(...)`` call leaves the walk silently, and a flag-shaped
        # assertion cannot tell that from a tree that complies. Bump it deliberately.
        # 42 -> 43: the placeholder a policy probe fills every unprobed fact with, so a
        # preview cannot quietly inherit a number from a fact it is not about.
        assert walked == 43, (
            f"the Unknown(reason=...) population moved to {walked}. If you added one, name\n"
            "its reason as a *_REASON constant and bump this count; if one left, check it\n"
            "did not take its only coverage with it."
        )

    def test_the_season_guard_names_a_check_the_panel_can_word(self) -> None:
        """Rule 144 for the other half of the sentence, run off the producer.

        ``CAUSE_COPY`` is walked wholesale above; ``CHECK_COPY`` cannot be, because its keys
        are composed at the call site rather than named as constants, and the map degrades
        into the backend's own label. That deferral is fine for a gate's short noun phrase
        and thin here: this key is the one a season guard writes, it sits in a list beside
        four gate labels, and a fallback would have the app answering one question in two
        registers.

        So the phrase is taken from ``guard_result`` itself rather than typed here (rule
        104). Rewording the producer without the panel fails, and the message says which file
        to open.
        """
        plan = SeriesPrunePlan(series_title="Show", prunable=[2])
        detail = guard_result(
            plan, 2, progress_unknown_reason="Plex has not matched this season"
        ).detail
        what = detail.removeprefix("could not check ").split(":", 1)[0]
        panel = (
            Path(__file__).resolve().parents[1] / "frontend" / "src" / "components" / "WhyPanel.tsx"
        ).read_text(encoding="utf-8")
        check_copy = panel.split("const CHECK_COPY", 1)[1].split("};", 1)[0]
        assert f'"{what}":' in check_copy, (
            f"the season guard writes 'could not check {what}: ...', and CHECK_COPY in\n"
            "frontend/src/components/WhyPanel.tsx has no entry for it, so the panel prints\n"
            "the backend's own phrase beside four gate labels written for the operator."
        )

    def test_every_backend_cause_has_operator_copy_in_the_panel(self) -> None:
        """Rule 144, across the tree boundary. ``CAUSE_COPY`` turns each backend reason into
        the sentence the owner reads; a key with no entry there renders the backend's raw
        phrasing instead, which is how internal wording reaches the screen. The failure
        message names the file to edit, because a comment asking the next author to remember
        does nothing.

        The two dormancy reasons were named for this in #278. They had been hand-typed five
        times in `src/` and twice more in `WhyPanel.tsx`, with a comment in `snapshot.py`
        citing rule 144 over the pair -- and nothing checking them, so rewording either side
        printed the raw backend phrase at the owner with the suite green.

        Naming them one at a time is what left #282 behind: five more reasons were reaching
        the panel raw, and a tuple of the constants somebody had thought to add could not
        say so. The population is **discovered** now -- every module-level ``*_REASON``
        string under ``src/reaper`` -- so a new reason is covered the moment it is named,
        and the only way out is an entry in ``_NO_PANEL_ROUTE`` that says why.
        """
        cause_copy = _cause_copy_source()
        discovered = _reason_constants()
        assert discovered, "the *_REASON walk found nothing, so this test proves nothing"
        checked = {name: value for name, value in discovered.items() if name not in _NO_PANEL_ROUTE}
        missing = [
            f"{name}  {value!r}"
            for name, value in sorted(
                {
                    **checked,
                    **{f"_NO_KEY_REASONS[{s.name}]": r for s, r in MOVIE_NO_KEY_REASONS.items()},
                    **{
                        f"season _NO_KEY_REASONS[{s.name}]": r
                        for s, r in SEASON_NO_KEY_REASONS.items()
                    },
                }.items()
            )
            if f'"{value}"' not in cause_copy
        ]
        assert not missing, (
            "these reasons reach the why-panel with no plain-language entry in CAUSE_COPY\n"
            "(frontend/src/components/WhyPanel.tsx), so the box prints the raw backend\n"
            "string at the owner:\n  " + "\n  ".join(missing)
        )

    def test_every_panel_cause_has_a_live_producer(self) -> None:
        """Rule 144's other direction, which nothing walked. The test above goes producer ->
        copy, so an entry whose backend reason was retired or reworded stayed in the map
        reading as a live translation of a sentence this build can no longer compose. #302
        found the one by hand, which is the point: a map only walked one way is only half
        pinned.

        An orphaned entry is not automatically wrong, and that is why the way out is a
        written claim rather than a deletion. The panel renders STORED explanations, so an
        entry may still be serving rows frozen before its producer changed -- and deleting it
        would print the raw backend phrase at the operator holding one, reopening #282.

        The population is the same one the forward walk trusts: every module-level
        ``*_REASON`` string under ``src/reaper``, plus both ``_NO_KEY_REASONS`` maps. A
        producer spelled some other way is therefore invisible here and reads as orphaned --
        the failure direction that costs a written line, not a wrong claim at the operator.

        Scoped to ``CAUSE_COPY``, with ``CHECK_COPY`` deferred in writing (rule 72): its keys
        are gate and field labels composed at the call site (``could not check {what}``), not
        module constants, so this discovery cannot see them and covering that map needs a
        different walk. ``CHECK_COPY`` also degrades harmlessly -- an unmatched check falls
        back to the backend's own label, which is already a plain noun phrase.
        """
        cause_copy = _cause_copy_source()
        keys = re.findall(r'^\s*"([^"]+)":', cause_copy, re.M)
        # Rules 145 and 147: pin what the matcher COLLECTS, not just that it found something.
        # It reads a double-quoted key at the start of a line followed by a colon -- which is
        # how prettier spells every entry, including the four whose value wraps to the next
        # line. It would not see a computed key or a key sharing a line with the entry before
        # it, and a flag-shaped assertion could not tell that from a map that complies.
        assert len(keys) == 20, (
            f"CAUSE_COPY parsed to {len(keys)} entries. If you added or removed one, bump this\n"
            "count; if it moved unexpectedly, the matcher above stopped seeing a spelling."
        )
        producers = set(_reason_constants().values())
        producers |= set(MOVIE_NO_KEY_REASONS.values()) | set(SEASON_NO_KEY_REASONS.values())
        orphaned = [
            k for k in keys if k not in producers and k not in _PANEL_COPY_WITHOUT_A_PRODUCER
        ]
        assert not orphaned, (
            "this why-panel copy translates a reason nothing in src/reaper writes any more\n"
            "(frontend/src/components/WhyPanel.tsx). Either delete the entry, or add it to\n"
            "_PANEL_COPY_WITHOUT_A_PRODUCER here saying which stored rows still reach it:\n  "
            + "\n  ".join(repr(k) for k in orphaned)
        )
        for key, why in _PANEL_COPY_WITHOUT_A_PRODUCER.items():
            assert why.strip(), f"{key!r} is exempt with no reason written"
            assert key in keys, (
                f"{key!r} is listed as panel copy outliving its producer, but CAUSE_COPY has "
                "no such entry. The exemption is stale."
            )
            assert key not in producers, (
                f"{key!r} is listed as having no producer, but one writes it again. Drop the "
                "exemption: the entry is live copy, not back-compat."
            )

    def test_the_reason_with_no_panel_route_still_has_one_way_out(self) -> None:
        """The exemption above is a claim about wiring, so it is checked rather than trusted
        (rule 25): a reason excused from needing panel copy must have no entry either, or the
        excuse is stale and the copy it does have is unreachable."""
        cause_copy = _cause_copy_source()
        discovered = _reason_constants()
        assert set(_NO_PANEL_ROUTE) <= set(discovered), (
            "_NO_PANEL_ROUTE names a constant that no longer exists: "
            f"{sorted(set(_NO_PANEL_ROUTE) - set(discovered))}"
        )
        for name, why in _NO_PANEL_ROUTE.items():
            assert why.strip(), f"{name} is exempt with no reason written"
            assert f'"{discovered[name]}"' not in cause_copy, (
                f"{name} is exempt from needing panel copy because {why}, but CAUSE_COPY has "
                "an entry for it. One of the two is wrong."
            )
