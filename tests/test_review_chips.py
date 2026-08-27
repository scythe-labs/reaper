# SPDX-License-Identifier: AGPL-3.0-or-later
"""The card chips and the whole-show group view.

The chip is pure display extraction from the stored explanation. It never re-decides
anything, so the unit tests below enumerate every stored verdict state a card can be in.
That includes protect under each gate, and each abstain cause in decide_verdict's own
precedence (match trouble, a deliberate left-for-you flag, checks that couldn't run, the
coverage floor, the score). The API tests then check that a show can be read whole,
covering every season, every lane, in one response.
"""

from __future__ import annotations

import ast
import json
import re
from collections.abc import Iterator
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine as sa_create_engine
from sqlalchemy.orm import Session

from reaper.api.review import (
    _CHIP_IDS,
    _chip,
    _decode_explanation,
    _explanation_out,
    _kept_reason,
    _primary_reason,
    _season_number,
)
from reaper.api.schemas import ChipOut
from reaper.clock import utcnow
from reaper.config import Settings
from reaper.db.base import Base
from reaper.db.models import Candidate, Snapshot
from reaper.engine import fields as fields_registry
from reaper.engine import identity
from reaper.engine.dormancy import dormancy_days, reference_instant
from reaper.engine.gates import (
    ABSTAIN,
    NO_KEY_REASON_IDS,
    PROTECT,
    Evaluation,
    Facts,
    GateConfig,
    GateId,
    GateResult,
    MinDormancyGate,
    ReturnedGate,
    ServerPopularityGate,
)
from reaper.engine.observation import Absent, Known
from reaper.engine.policy import DEFAULT_MOVIE_POLICY
from reaper.engine.reason import Reason, from_wire, legacy, to_wire
from reaper.engine.signals import Score
from reaper.main import create_app
from reaper.services.condemned import (
    BAD_MATCH_STATES,
    MATCH_UNREADABLE,
    reap_override_verdict_decoded,
)
from reaper.services.season_evidence import guard_result
from reaper.services.season_pruning import PruneConflict, SeriesPrunePlan
from reaper.services.snapshot import HAND_SPARE_REASON, _explain

from ._auth import login
from ._reasons import catalog
from ._reasons import text as reason_text

#: A named reason that cannot reach a rendered surface, and why. The catalog's
#: ``why.cause.*`` entries compose both the blocked "could not check {check}: {cause}" tail
#: and a season's own "why kept" row. A reason the operator meets through neither is not a
#: gap, so it must not have an entry, which would claim a route that does not exist. Each
#: excuse is a claim about wiring, so
#: ``test_the_reason_with_no_panel_route_still_has_one_way_out`` checks it rather than
#: trusting it. Each one is classified in writing here, rather than silently skipped.
_NO_PANEL_ROUTE = {
    "facts_codec.NOT_RECORDED_REASON": (
        "facts_from_dict has one caller, the policy simulator, which reads a re-decided "
        "score and verdict and never builds or stores an Explanation"
    ),
    "preview.NOT_PROBED_REASON": (
        "the facts a policy probe leaves out. probe_signal answers one route with a number "
        "and a detail string and builds no candidate, stores no Explanation and touches no "
        "snapshot, so nothing carries this reason to a panel"
    ),
    "library_seen.NO_RETURN_RECORD_REASON": (
        "returned_days_ago/returned_by_reaper (#553) are read only by ReturnedGate, which "
        "abstains on Unknown without blocking -- the deviation its own docstring owns -- so "
        "this reason never reaches a 'could not check {what}: {reason}' detail. No signal and "
        "no rule-authoring field reads either observation either"
    ),
    "rewatch.NO_REWATCH_ESTIMATE_REASON": (
        "rewatch_cohort_n/rewatch_cohort_k (#554 stage 2) feed only the stored explanation's "
        "rewatch_odds context block, read by its typed `state` in "
        "{measured, thin, no_history}; no gate reads either field, so this reason never "
        "reaches a 'could not check {what}: {reason}' detail"
    ),
}

#: Catalog cause entries whose backend producer is gone, and why each stays anyway.
#: Empty today. A frozen row's prose reason no longer thaws forward to a catalog id at
#: all: it rides through as a ``legacy`` reason and renders raw, so an id with no current
#: producer has no producer at all any more, fresh or stored, and belongs deleted rather
#: than exempted. An exemption placed here is a claim about wiring, written down rather
#: than assumed, the same way ``_NO_PANEL_ROUTE`` above writes down the other direction's
#: excuses.
_PANEL_COPY_WITHOUT_A_PRODUCER: dict[str, str] = {}

#: Cause ids composed in code rather than named as ``*_REASON`` constants, each with its
#: producer. The constant walk below cannot discover these, because they are built at the
#: call site with params, so they are pinned here by hand and reconciled against the
#: catalog in both directions.
_COMPOSED_CAUSE_IDS = {
    "reach_not_recorded": "gates.history_shortfall, on a reach the scan did not record",
    "history_reach_short": "gates.history_shortfall, naming the reach",
    "history_not_that_far": "gates.history_shortfall, inside the naming margin",
    "added_at_not_recorded": "gates.lifetime_shortfall, with no arrival date",
    "window_not_recorded": "fields.reach_shortfall, with no popularity window stated",
    "error": "fields.evaluate, wrapping a comparison error verbatim",
}

#: Fields whose catalog entry carries a ``policyRules.fieldHelp.<key>`` string. Read off
#: ``engine.fields.REGISTRY`` by hand, rather than derived from ``BY_KEY``, so a field
#: losing its help cannot silently take its own guard's coverage with it. Today every
#: field carries one.
_FIELDS_WITH_HELP = frozenset(
    {
        "days_unwatched",
        "genre",
        "imdb_rating",
        "imdb_votes",
        "on_list",
        "quality",
        "recent_watchers",
        "release_age",
        "requested",
        "season_rank",
        "show_ended",
        "size_bytes",
        "streaming_now",
        "watchers_all_time",
        "whitelisted",
    }
)

#: Same, for ``unit_suffix`` -> ``policyRules.fieldUnit.<key>``. Only a field whose value is a
#: plain number with a unit carries one; a rank, a yes/no and a free-text field do not.
_FIELDS_WITH_UNIT = frozenset(
    {
        "days_unwatched",
        "imdb_rating",
        "imdb_votes",
        "recent_watchers",
        "release_age",
        "size_bytes",
        "watchers_all_time",
    }
)


def _catalog_causes() -> dict[str, str]:
    """The catalog's ``why.cause`` entries, the copy every cause id composes into.

    Read off the real ``ui.json`` (``tests/_reasons.catalog``), so the walks either side of
    this file's tree boundary see the copy the app really ships. Three tests read it, so it
    is derived once. A row frozen before reasons were typed renders its stored sentence
    verbatim now, through the panel's generic legacy fallback, never through copy of its
    own.
    """
    causes = catalog()["cause"]
    assert isinstance(causes, dict)
    return {k: v for k, v in causes.items() if isinstance(v, str)}


def _reason_constants() -> dict[str, str]:
    """Every module-level ``*_REASON`` string under ``src/reaper``, as ``file.NAME -> value``.

    Discovered rather than listed, so naming a new reason is all it takes to be covered. A
    hand-written list can only ever pin the members somebody remembered to add. Walks the
    source rather than importing, so a module with a heavy import graph costs nothing here.
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


def _conflict_reason(*, kept_watchers: int | None = 0, shortfall: str | None = None) -> Reason:
    """A real keep-rule conflict's typed detail, from the one producer that shapes it.

    Built rather than transcribed, so a hand-typed copy of this sentence cannot drift from
    ``PruneConflict.message`` and leave the chip below it asserting a comparison the message
    itself no longer makes.

    Two of the three shapes matter here, and neither made a comparison.
    ``kept_watchers=None`` does not mean "nobody watched the kept season." It means "that
    season's history could not be read at all." ``shortfall`` is a count taken over a mirror
    that begins after the season arrived, so it is a lower bound rather than an answer. Both
    are raised as conflicts rather than letting an unestablished number clear a protection,
    and the message states each in its own words.
    """
    return PruneConflict(
        pruned_season=3,
        kept_season=1,
        pruned_watchers=2,
        kept_watchers=kept_watchers,
        kept_reason=Reason("season_keep.keep_last", {"keep_last": 2, "rank": 1}),
        shortfall=None if shortfall is None else legacy(shortfall),
    ).message


def _conflict_detail(*, kept_watchers: int | None = 0, shortfall: str | None = None) -> str:
    """The same conflict as its composed English sentence, so a test asserting on the
    wording stays derived from the one producer rather than transcribed."""
    return reason_text(_conflict_reason(kept_watchers=kept_watchers, shortfall=shortfall))


#: The counted shape: a comparison really was made, so the chip may state it.
CONFLICT_SENTENCE = _conflict_detail(kept_watchers=0)

#: A hypothetical future gate's typed deliberate flag, any id other than ``blocked`` or
#: ``legacy``, standing in for a producer this codebase does not ship. This proves the
#: interlock against a shape nothing here currently emits, not only the one it does.
_FUTURE_GATE_REASON = Reason("cause.future_gate", {"text": "A rule asked a human to look."})


def _drop_defers_key(explanation_json: str) -> str:
    """A frozen explanation aged back one generation. Every ``defers_to_owner`` key is removed.

    The pre-flag row is a state no current writer can produce. Hand-building one would be a
    second copy of the writer's shape, which risks pinning a row the producer never actually
    emits. Derived from a real frozen row instead, so it drifts with it.
    """
    exp = json.loads(explanation_json)
    for entry in exp["protections_unknown"]:
        entry.pop("defers_to_owner", None)
    return json.dumps(exp)


def _never_played_facts(days_dormant: int) -> Facts:
    """A title with no plays at all, the shape the review queue's Sanctuary lane is full of.

    Both watcher counts are a genuine zero, not an unreadable source. This is a file the
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
    fired: list[dict[str, Any]] | None = None,
    unknown: list[dict[str, Any]] | None = None,
    match_status: str | None = None,
) -> dict[str, Any]:
    body: dict[str, Any] = {
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


def _chip_reason(chip: ChipOut) -> Reason:
    """A chip's wire ``reason`` thawed back to a comparable :class:`Reason`, the way an
    HTTP caller would read it off the JSON response. One derivation so every test compares
    the same way."""
    return from_wire(chip.reason.model_dump())


def _leaf_ids(node: dict[str, Any], prefix: str = "") -> set[str]:
    """Every dotted id reachable as a string leaf of a nested catalog section, for the
    two-way walk against ``_CHIP_IDS`` below."""
    ids: set[str] = set()
    for key, value in node.items():
        dotted = f"{prefix}.{key}" if prefix else key
        if isinstance(value, str):
            ids.add(dotted)
        elif isinstance(value, dict):
            ids |= _leaf_ids(value, dotted)
    return ids


class TestKeptChipWording:
    """One typed reason per protection, from the gate's own reason id.

    A fresh row's numbers come off the reason's own params. A legacy row's content is
    never read for this: any sentence, whatever it says, takes its gate's plain id. This is
    proven below once per gate rather than once per sentence, because nothing in
    ``_kept_reason`` branches on what a legacy row's prose actually says, so a table of many
    such sentences could only ever prove the same thing many times over. The why panel
    beneath the chip still shows the row's real stored sentence, verbatim, through
    ``detail_key``.
    """

    @pytest.mark.parametrize(
        ("gate", "expected"),
        [
            ("whitelisted", Reason("kept.whitelisted")),
            ("streaming_now", Reason("kept.streaming_now")),
            ("rating_floor", Reason("kept.rating_plain")),
            ("server_popularity", Reason("kept.popularity_plain")),
            ("curated_list", Reason("kept.curated_list")),
            ("min_dormancy", Reason("kept.dormancy")),
            ("unmanaged", Reason("kept.unknown")),
            ("season_progression", Reason("kept.season.rule")),
            ("custom", Reason("kept.custom")),
            ("brand_new_gate", Reason("kept.unknown")),
        ],
    )
    def test_a_legacy_row_takes_its_gates_plain_id(self, gate: str, expected: Reason) -> None:
        assert _kept_reason(gate, legacy("whatever this old row happened to say")) == expected

    def test_a_legacy_hand_spare_is_not_matched_by_text_any_more(self) -> None:
        # The exact sentence snapshot.py once froze for a hand spare no longer stands out
        # from any other legacy whitelisted keep. Only the typed id is matched now, proven
        # below on the shape a live scan actually freezes.
        assert _kept_reason("whitelisted", legacy("you spared this by hand")) == Reason(
            "kept.whitelisted"
        )

    def test_a_fresh_hand_spare_still_gets_its_own_id(self) -> None:
        assert _kept_reason("whitelisted", HAND_SPARE_REASON) == Reason("kept.hand_spare")

    def test_a_fresh_rating_row_carries_its_own_number(self) -> None:
        reason = Reason(
            "rating_cleared",
            {
                "clauses": (
                    Reason(
                        "rating_value_votes", {"source": "imdb", "value": 6.8, "votes": 722_243}
                    ),
                ),
            },
        )
        assert _kept_reason("rating_floor", reason) == Reason(
            "kept.rating", {"value": 6.8, "source": "imdb"}
        )

    def test_a_fresh_popularity_row_carries_its_own_number(self) -> None:
        reason = Reason("popularity_watched", {"count": 3, "window_days": 365})
        assert _kept_reason("server_popularity", reason) == Reason(
            "kept.popularity", {"count": 3, "window_days": 365}
        )

    def test_a_fresh_no_history_dormancy_row_gets_its_own_id(self) -> None:
        assert _kept_reason("min_dormancy", Reason("dormancy_unestablished")) == Reason(
            "kept.no_history"
        )

    @pytest.mark.parametrize(
        ("fresh_id", "expected"),
        [
            ("season_keep.specials", Reason("kept.season.specials")),
            ("season_keep.incomplete", Reason("kept.season.incomplete")),
            ("season_keep.airing", Reason("kept.season.airing")),
            ("season_keep.first", Reason("kept.season.first")),
            ("season_keep.keep_all", Reason("kept.season.keep_all")),
            ("season_keep.midbinge", Reason("kept.season.midbinge")),
            # NOT a keep rule: the lever is the depth of the watch history, and the
            # generic id would send the operator to edit a control that will not move it.
            ("cause.progress_history_short", Reason("kept.season.progress_history_short")),
        ],
    )
    def test_a_fresh_season_keep_row_carries_its_specific_id(
        self, fresh_id: str, expected: Reason
    ) -> None:
        assert _kept_reason("season_progression", Reason(fresh_id)) == expected

    def test_a_fresh_keep_last_row_carries_the_count(self) -> None:
        reason = Reason("season_keep.keep_last", {"keep_last": 2, "rank": 1})
        assert _kept_reason("season_progression", reason) == Reason(
            "kept.season.keep_last", {"keep_last": 2}
        )


class TestTheKeptChipNeverClaimsAPlayThatDidNotHappen:
    """A title nobody has ever played reaches ``MinDormancyGate``'s fired branch, because
    that gate's clock runs from the day the file arrived whenever there is no play to run
    it from (``engine.dormancy.reference_instant``).

    A chip that says "watched too recently" for a title with zero plays all time would be
    a plain fabrication, printed three lines under the same panel's "nobody watched it in
    the last year." ``MinDormancyGate`` words its own detail "untouched" for exactly this
    reason, and the chip must not claim the play either.

    This is an agreement test, not a transcription. It derives the dormancy through the
    real helper with no play, runs the real gate, and hands the real detail to the real
    chip, so rewording either half alone fails here rather than drifting quietly.
    """

    #: A file that arrived recently and was never once played, under an operator waiting a
    #: year. Its whole life on the server is shorter than the wait, so the gate fires.
    ARRIVED_DAYS_AGO = 89
    WAITS_DAYS = 365

    def _reason_for_a_never_played_title(self) -> Reason:
        # One clock reading, not three. A test that re-samples can straddle a day boundary
        # between the reference and the count and drop a day.
        now = utcnow()
        reference = reference_instant(
            last_played=None,
            added_at=now - timedelta(days=self.ARRIVED_DAYS_AGO),
            # The watch mirror reaches back further than the file has existed, so the
            # horizon is not what is being measured here. The arrival date is.
            horizon=now - timedelta(days=400),
        )
        assert reference is not None
        facts = _never_played_facts(dormancy_days(reference, now=now))
        result = MinDormancyGate(GateConfig(threshold=self.WAITS_DAYS)).evaluate(facts)

        assert result.outcome == PROTECT, "the gate must fire for this chip to exist at all"
        return _kept_reason("min_dormancy", result.detail)

    def test_the_chip_asserts_no_play(self) -> None:
        """The core check. Nobody watched this, so the chip may not say anyone did."""
        sentence = reason_text(self._reason_for_a_never_played_title(), namespace="chip.text")

        # Whole words. "unwatched" is the chip saying nobody did, which is the point.
        assert re.search(r"\bwatched\b", sentence) is None
        assert re.search(r"\bplayed\b", sentence) is None

    def test_the_chip_still_names_why_it_is_kept(self) -> None:
        """Truthful is not enough on its own. The chip's whole job is to say why."""
        assert self._reason_for_a_never_played_title() == Reason("kept.dormancy")


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
        # A legacy row's number is no longer read back out of its prose. The plain id is
        # what every rating-floor keep with no typed detail takes.
        assert _chip_reason(chip) == Reason("kept.rating_plain")

    def test_protect_with_nothing_fired_degrades_to_no_chip(self) -> None:
        """A stored row that claims protect but records no protection must not invent
        one. The card simply shows no chip rather than a wrong one."""
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
        assert (chip.tone, _chip_reason(chip)) == ("quiet", Reason("match.unmatched"))

    def test_ambiguous_match(self) -> None:
        chip = _chip(_exp(50, match_status="ambiguous"), "abstain", 50)
        assert chip is not None
        assert (chip.tone, _chip_reason(chip)) == ("quiet", Reason("match.ambiguous"))

    @pytest.mark.parametrize(
        ("media_type", "app"), [("movie", "Radarr"), ("season", "Sonarr")], ids=["movie", "season"]
    )
    def test_a_conflicted_match_names_the_media_type(self, media_type: str, app: str) -> None:
        """A disagreement is not a duplicate, and saying so would send the operator hunting
        for a second copy that is not there. It gets its own chip instead, carrying the
        media type so the catalog's ICU select names the app whose metadata has to change.
        That app differs by media type, so both are pinned here. The movie arm alone would
        pass on a hardcoded "Radarr".
        """
        chip = _chip(_exp(50, match_status="conflicted"), "abstain", 50, media_type)
        assert chip is not None
        expected = Reason("match.conflicted", {"media_type": media_type})
        assert (chip.tone, _chip_reason(chip)) == ("quiet", expected)
        # The catalog composes the right app here, distinct from the multiplicity wording
        # the ambiguous-match id uses.
        text = reason_text(expected, namespace="chip.text")
        assert text == f"Plex and {app} don't agree"
        assert "more than one" not in text
        assert "two different things" not in text

    @pytest.mark.parametrize(
        ("media_type", "expected"),
        [
            ("movie", "Kept to be safe: Plex and Radarr describe this file differently."),
            ("season", "Kept to be safe: Plex and Sonarr describe this show differently."),
        ],
        ids=["movie", "season"],
    )
    def test_the_card_reason_for_a_conflicted_match(self, media_type: str, expected: str) -> None:
        """The Reap-page line, swept alongside the chip. Both read the same status."""
        reason = _primary_reason(_exp(50, match_status="conflicted"), "abstain", 50, media_type)
        assert reason is not None
        assert reason_text(reason) == expected

    def test_season_conflict_wants_eyes(self) -> None:
        """The keep-rule conflict is a deliberate left-for-you flag, not a plumbing
        failure, so it wears the amber-outline tone."""
        assert "more than watched Season" in CONFLICT_SENTENCE, "the producer stopped saying this"

        chip = _chip(
            _exp(
                82,
                unknown=[
                    {
                        "gate": "season_progression",
                        "detail_key": to_wire(_conflict_reason(kept_watchers=0)),
                        "defers_to_owner": True,
                    }
                ],
            ),
            "abstain",
            82,
        )
        assert chip is not None
        assert chip.tone == "look"
        assert _chip_reason(chip) == Reason("look.comparable")

    def test_an_unreadable_kept_season_claims_no_comparison(self) -> None:
        """The twin of the dormancy chip's fabricated play, one gate over.

        A conflict is also raised when the kept season's watcher count could not be read,
        and the operator is shown only this chip on a queue card. Claiming the season
        "watched more than" another states arithmetic against a number nobody took. That
        sentence is exactly what ``detect_conflicts`` deliberately removed from the
        message, so the chip must not print it one line above the panel's own denial.
        """
        detail = _conflict_detail(kept_watchers=None)
        assert "could not check who watched" in detail, "the producer stopped saying this"

        chip = _chip(
            _exp(
                82,
                unknown=[
                    {
                        "gate": "season_progression",
                        "detail_key": to_wire(_conflict_reason(kept_watchers=None)),
                        "defers_to_owner": False,
                    }
                ],
            ),
            "abstain",
            82,
        )

        assert chip is not None
        assert chip.tone == "look"
        reason = _chip_reason(chip)
        assert "watched more than" not in reason_text(reason, namespace="chip.text")
        assert reason == Reason("look.unknowable")

    def test_a_conflict_the_mirror_could_not_settle_shares_that_chip(self) -> None:
        """The third shape, and why that chip does not name the kept season.

        A count taken over a mirror that begins after the season arrived is a lower bound,
        so no comparison against it settles anything. The season it could not establish is
        just as often the one being removed as the one being kept. The two non-comparisons
        share one flag, so they share one chip, and its copy has to be true of both.
        """
        detail = _conflict_detail(shortfall="your watch history only goes back 12 months")
        assert "cannot tell whether" in detail, "the producer stopped saying this"

        chip = _chip(
            _exp(
                82,
                unknown=[
                    {
                        "gate": "season_progression",
                        "detail_key": to_wire(
                            _conflict_reason(
                                shortfall="your watch history only goes back 12 months"
                            )
                        ),
                        "defers_to_owner": False,
                    }
                ],
            ),
            "abstain",
            82,
        )

        assert chip is not None
        assert chip.tone == "look"
        reason = _chip_reason(chip)
        assert "watched more than" not in reason_text(reason, namespace="chip.text")
        assert reason == Reason("look.unknowable")

    def test_a_row_frozen_before_the_flag_names_neither_shape(self) -> None:
        """A stored row that predates ``defers_to_owner`` carries nothing that can tell a
        made comparison from a refused one, so the chip claims neither and says the
        vague-but-true thing instead.

        Recovering the shape from the wording would not work: reading "more than watched
        Season" as a deferral would let the card offer the operator a conflict to settle,
        while a hand reap on that same row would have to be honored regardless, since the
        reap side never holds on this shape at all. The chip must not claim a comparison it
        has no evidence was made, which is what this pins, and the pair is swept in full
        below.
        """
        for kept_watchers in (1, None):
            detail_key = to_wire(_conflict_reason(kept_watchers=kept_watchers))
            row = _exp(82, unknown=[{"gate": "season_progression", "detail_key": detail_key}])

            chip = _chip(row, "abstain", 82)

            assert chip is not None
            assert chip.tone == "look"
            assert _chip_reason(chip) == Reason("look.unsettled")
            # ...and the invitation the chip extends is one the engine honors.
            assert reap_override_verdict_decoded(row, score=82) == "condemn"

    @pytest.mark.parametrize("junk", ['"true"', '"false"', '"0"', "1", "0", "[]", "{}", "null"])
    def test_only_a_real_json_true_claims_the_comparison_was_made(self, junk: str) -> None:
        """``_chip`` reads ``defers_to_owner`` with ``is True``, and that strictness is the
        whole guard on the one chip that asserts something about the evidence.

        A stored row is written by whatever version was running, so this key can come back
        as a string, a number, or a container. Only a real JSON ``true`` means "Reaper made
        the comparison and the rule lost it." Everything else means the row cannot tell, and
        must fall to the vague-but-true chip rather than telling the operator a comparison
        happened. `"false"`, `"0"`, and `0` are the sharp cases: all three are truthy as
        strings or falsy as numbers, in ways a loose test gets backwards.

        This sweep lives here because the chip is the consumer that actually needs the
        strictness now.
        """
        detail_key = json.dumps(to_wire(_conflict_reason(kept_watchers=1)))
        entry = json.loads(
            f'{{"gate": "season_progression", "detail_key": {detail_key}, '
            f'"defers_to_owner": {junk}}}'
        )

        chip = _chip(_exp(82, unknown=[entry]), "abstain", 82)

        assert chip is not None
        reason = _chip_reason(chip)
        assert reason == Reason("look.unsettled")
        assert "watched more than" not in reason_text(reason, namespace="chip.text")

    @pytest.mark.parametrize("junk", ['"true"', '"false"', '"0"', "1", "0", "[]", "{}", "null"])
    def test_the_panel_reads_the_junk_the_chip_reads_and_keeps_rendering(self, junk: str) -> None:
        """The same sweep as the chip's, against the panel, which reads the same byte
        differently.

        Two readers of one stored byte, with two different coercion rules. The chip reads
        it with ``is True`` / ``is False``, so everything above falls to its vague chip.
        The panel reads the same byte through Pydantic's lax bool coercion, where ``1``
        and ``"true"`` become ``True``, ``"0"`` and ``0`` become ``False``, and ``[]`` and
        ``{}`` are refused outright. A refusal is the worst of the three, because it fails
        the enclosing ``Explanation`` rather than the one field. ``_explanation_out`` then
        serves its degraded body, and the operator gets a panel with no signals, no
        protections, and no threshold, beside a chip that read the row perfectly well and
        a hand reap that still condemns.

        This asserts both halves on one row. Nothing is degraded, and the flag reads
        ``None``, the state that already means "nothing here can tell a comparison Reaper
        made from one it refused," exactly where the chip declines to claim one. The three
        real writer shapes are swept in ``test_the_panel_is_served_the_flag_its_chip_reads``
        below. This test covers everything else a row can carry.
        """
        detail_key = json.dumps(to_wire(_conflict_reason(kept_watchers=1)))
        entry = json.loads(
            f'{{"gate": "season_progression", "detail_key": {detail_key}, '
            f'"defers_to_owner": {junk}}}'
        )
        exp = _exp(82, unknown=[entry])
        row = Candidate(
            media_key="sonarr:1:2:3",
            explanation_json=json.dumps(exp),
            score=82,
            coverage_bp=10_000,
        )

        panel = _explanation_out(row)

        # The whole explanation survives one unreadable byte, threshold included. The panel
        # prints "your threshold is N" beside the score, and the degraded body has no N.
        assert not panel.unreadable
        assert panel.body.threshold == 70
        unknown = panel.body.protections_unknown
        assert [o.gate for o in unknown] == ["season_progression"]
        assert unknown[0].defers_to_owner is None

        # And the chip beside it says the same thing about the same row.
        chip = _chip(exp, "abstain", 82)
        assert chip is not None
        assert _chip_reason(chip) == Reason("look.unsettled")

    @pytest.mark.parametrize("junk", ["70.5", '"abc"', "true", "[]", '{"a": 1}'])
    def test_an_illegible_threshold_costs_its_own_clause_and_nothing_else(self, junk: str) -> None:
        """``threshold`` is ``defers_to_owner``'s twin, so the same fix binds both.

        The same shape exactly. One stored byte, three readers, two coercion rules. ``_chip``
        and ``_primary_reason`` each test it with ``isinstance(value, int)`` and cope with
        anything else. ``Explanation.threshold`` reads it through pydantic's lax
        ``int | None``, which refuses ``70.5`` and ``"abc"``. That refusal fails the
        enclosing model rather than the one field, so the operator would lose every signal,
        every protection, and the match block too, beside a chip that read the same row fine.

        ``true`` is in the sweep because Python calls a ``bool`` an ``int``. Reaching the
        panel as a threshold of 1 would not be a score. It would be a row nobody can read.
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

        # The panel survives, and pays for the unreadable byte with exactly the one clause
        # it cannot honestly print. Never an invented figure, and never the whole body.
        assert not panel.unreadable
        assert panel.body.threshold is None
        assert panel.body.score == 82

        # And the chip beside it declines the same comparison rather than inventing one.
        chip = _chip(exp, "abstain", 82)
        assert chip is not None
        assert _chip_reason(chip) == Reason("below")

    @pytest.mark.parametrize("junk", ['"matched"', "5", "[]"])
    def test_a_match_block_of_the_wrong_shape_reads_as_absent(self, junk: str) -> None:
        """The other twin on the same model, and the same trade.

        ``review._match_status`` reads the stored match off the raw dict and copes with any
        shape. Refusing it at the wire boundary would take every other block on the panel
        with it. ``None`` is a shape the panel already renders. It is what a row scanned
        before the match block existed carries.
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
        real writer (``snapshot._explain``) rather than a hand-built dict.

        Every other chip test builds its explanation by hand, so ``_explain`` could stop
        writing the key entirely and nothing would fail, while every season-conflict chip
        silently degraded to the legacy "names neither shape" wording above. This test is
        the missing link. The panel is the field's other reader and has its own link, in
        ``test_the_panel_is_served_the_flag_its_chip_reads`` below.
        """
        made = GateResult(
            GateId.SEASON_PROGRESSION,
            ABSTAIN,
            detail=_conflict_reason(kept_watchers=1),
            blocked=True,
            defers_to_owner=True,
        )
        refused = replace(made, defers_to_owner=False)

        for result, expected in (
            (made, Reason("look.comparable")),
            (refused, Reason("look.unknowable")),
        ):
            frozen = json.loads(
                _explain(
                    Evaluation(results=[result]),
                    Score(value=82.0, coverage=1.0, results=[]),
                    DEFAULT_MOVIE_POLICY,
                )
            )

            chip = _chip(frozen, "abstain", 82)

            assert chip is not None, expected
            assert _chip_reason(chip) == expected

    def test_the_panel_is_served_the_flag_its_chip_reads(self) -> None:
        """The why panel's half of ``defers_to_owner``'s supply chain, end to end.

        The chip and the panel it opens read the same stored row, but across different
        boundaries. The chip reads the raw dict. The panel reads through
        ``api.schemas.Explanation``, which drops any key it does not declare. If the model
        does not declare this key, the flag does not exist for the panel, promising a
        comparison the reason block below it denies. Shipping the field to a consumer
        across a serialization boundary is part of what this pins, not a separate concern.

        All three generations are covered here, because the third is the one a ``bool``
        field would erase. A row frozen before the flag carries no key and must arrive as
        ``None``, not ``False``.
        """
        made = GateResult(
            GateId.SEASON_PROGRESSION,
            ABSTAIN,
            detail=_conflict_reason(kept_watchers=1),
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
            # No writer emits this any more. It is what is already on disk. Built by
            # dropping the key from a real frozen row, so it cannot drift from the shape
            # above.
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
        """The twin of the test above, for ``unestablishable``.

        The guard's own output, frozen by the real ``_explain`` and read back through the
        real ``Explanation``. Two things have to survive that trip, and neither is visible
        from either side alone. The row must leave ``protections_checked``, where it was
        claiming a pass, and it must arrive carrying the flag ``WhyPanel.keepRuleConflict``
        reads. A key ``Explanation`` does not declare is dropped silently, and the panel
        would then offer to settle a comparison nobody attempted.

        The pre-flag generation rides on ``defers_to_owner``'s test above. A row frozen
        before either key reads ``None`` for both.
        """
        never_ran = guard_result(
            SeriesPrunePlan(series_title="Show", prunable=[2]),
            2,
            progress_unknown_reason="plex_unmatched",
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
        assert unknown[0].detail_key is not None
        assert unknown[0].detail_key.model_dump() == to_wire(never_ran.detail)

    def test_the_chip_and_the_reap_decision_never_disagree(self) -> None:
        """No chip may promise a reap the engine will refuse.

        Both conflict shapes and all three row generations, against the real
        ``reap_override_verdict``. Every chip here opens with "Needs a look," which invites
        a decision, so every one of them must pair with ``condemn``. The flag still picks
        which sentence they read, and that is asserted in the tests above. What it may not
        do is pick a sentence the deletion path contradicts.

        The rows the reap still refuses are swept beside them, or this test could only fail
        in one direction. A bad Plex match and an unreadable protections list keep the
        file, and neither wears a chip that invites the operator to remove it.
        """
        for kept_watchers in (1, None):
            detail_key = to_wire(_conflict_reason(kept_watchers=kept_watchers))
            for flag in ({"defers_to_owner": True}, {"defers_to_owner": False}, {}):
                exp = _exp(
                    82, unknown=[{"gate": "season_progression", "detail_key": detail_key, **flag}]
                )

                chip = _chip(exp, "abstain", 82)
                verdict = reap_override_verdict_decoded(exp, score=82)

                assert chip is not None
                reason = _chip_reason(chip)
                assert reason.id.startswith("look."), f"{reason!r} for {flag}"
                assert verdict == "condemn", f"{reason!r} vs {verdict!r} for {flag}"

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
            assert chip is None or not _chip_reason(chip).id.startswith("look."), label

    def test_any_future_deliberate_flag_still_wants_eyes(self) -> None:
        """A typed detail whose id is neither ``blocked`` nor ``legacy`` is a deliberate
        flag whatever gate raised it, so this fails toward showing it loudly. A legacy
        row's prose is never read for this: only the typed id says "decide this yourself."
        """
        chip = _chip(
            _exp(60, unknown=[{"gate": "custom", "detail_key": to_wire(_FUTURE_GATE_REASON)}]),
            "abstain",
            60,
        )
        assert chip is not None
        assert (chip.tone, _chip_reason(chip)) == ("look", Reason("look.unsettled"))

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
        assert (chip.tone, _chip_reason(chip)) == ("quiet", Reason("unknown_checks"))

    def test_a_history_too_short_for_the_window_reads_as_a_check_that_could_not_run(
        self,
    ) -> None:
        """The popularity gate's reach block, from the production gate rather than a
        retyped string, because the ``could not check`` prefix is what routes it here.

        A mirror shallower than the popularity window makes the watcher count a lower
        bound, so the gate reports the protection as un-checked. That is a plumbing
        failure, not a decision left to the owner, and the two must not wear the same
        chip. Nothing in ``verdict`` enforces the prefix for this gate. The reap is held
        by the gate id instead, so this chip and ``WhyPanel``'s check/cause split are the
        only places a reword shows up, which is why the assertion lives here.
        """
        result = ServerPopularityGate(GateConfig(threshold=3, window_days=365)).evaluate(
            _popularity_short_history_facts()
        )
        assert result.blocked is True

        chip = _chip(
            _exp(82, unknown=[{"gate": result.gate.value, "detail_key": to_wire(result.detail)}]),
            "abstain",
            82,
        )

        assert chip is not None
        reason = _chip_reason(chip)
        assert (chip.tone, reason) == ("quiet", Reason("unknown_checks"))
        assert not reason.id.startswith("look.")

    def test_coverage_floor(self) -> None:
        """Past the blocked cases, an abstain at or above the threshold can only be the
        coverage floor (decide_verdict's order)."""
        chip = _chip(_exp(82), "abstain", 82)
        assert chip is not None
        assert (chip.tone, _chip_reason(chip)) == ("quiet", Reason("coverage"))

    @pytest.mark.parametrize("stored_floor", [6000, 3000])
    def test_explain_freezes_the_coverage_floor_the_panel_restates(self, stored_floor: int) -> None:
        """The floor is the threshold's twin. It is a policy number the verdict is decided
        against, frozen so the panel restates the line coverage fell under rather than
        reading the live policy, which may have moved since the scan.

        This goes through the real writer (``_explain``) and reads back through the
        panel's own ``Explanation``, so the freeze cannot silently stop happening. Both
        floors differ from the 5000 default, so an omission that let the panel read a
        constant would fail here.
        """
        policy = DEFAULT_MOVIE_POLICY.model_copy(update={"coverage_floor_bp": stored_floor})
        frozen = _explain(
            Evaluation(results=[]),
            Score(value=82.0, coverage=0.4, results=[]),
            policy,
        )
        row = Candidate(
            media_key="sonarr:1:2:3",
            explanation_json=frozen,
            score=82,
            coverage_bp=4000,
        )

        panel = _explanation_out(row)

        assert not panel.unreadable
        assert panel.body.coverage_floor_bp == stored_floor

    @pytest.mark.parametrize("junk", ["70.5", '"abc"', "true", "[]", '{"a": 1}'])
    def test_an_illegible_coverage_floor_thaws_to_absent_and_nothing_else(self, junk: str) -> None:
        """``coverage_floor_bp`` is ``threshold``'s twin, thawed by the same helper.

        ``true`` is in the sweep because Python calls a ``bool`` an ``int``. A floor of
        ``True`` is not 1 bp. It is a row nobody can read. An illegible byte costs its own
        clause. The panel drops the floor sentence, never the whole panel, and never its
        twin's clause.
        """
        exp = json.loads(
            f'{{"coverage_floor_bp": {junk}, "threshold": 70, "score": 82, "coverage": 1.0, '
            '"signals": [], "protections_fired": [], "protections_checked": [], '
            '"protections_unknown": []}'
        )
        row = Candidate(
            media_key="sonarr:1:2:3",
            explanation_json=json.dumps(exp),
            score=82,
            coverage_bp=4000,
        )

        panel = _explanation_out(row)

        assert not panel.unreadable
        assert panel.body.coverage_floor_bp is None
        assert panel.body.threshold == 70

    def test_a_row_frozen_before_the_floor_shipped_reads_as_absent(self) -> None:
        """A row scanned before this field existed carries no key, and the panel drops the
        floor clause rather than invent a line. This is the same three-state a null
        threshold already has. Built by dropping the key from a real frozen row, so the
        "the writer emits it" half cannot drift from the shape the reader is handed.
        """
        frozen = json.loads(
            _explain(
                Evaluation(results=[]),
                Score(value=82.0, coverage=1.0, results=[]),
                DEFAULT_MOVIE_POLICY,
            )
        )
        assert "coverage_floor_bp" in frozen  # the writer emits it...
        del frozen["coverage_floor_bp"]  # ...and this is what an older scan left on disk
        row = Candidate(
            media_key="sonarr:1:2:3",
            explanation_json=json.dumps(frozen),
            score=82,
            coverage_bp=10_000,
        )

        panel = _explanation_out(row)

        assert not panel.unreadable
        assert panel.body.coverage_floor_bp is None

    def test_below_threshold_names_both_numbers(self) -> None:
        chip = _chip(_exp(42), "abstain", 42)
        assert chip is not None
        assert (chip.tone, _chip_reason(chip)) == (
            "quiet",
            Reason("below_threshold", {"score": 42, "threshold": 70}),
        )

    def test_malformed_explanation_never_errors_a_row_off_the_queue(self) -> None:
        # The parse happens once per row now (_decode_explanation), and anything that is
        # not a JSON object arrives here as None. Every extractor re-checks that itself,
        # so calling one directly is exactly as defensive as calling it through the queue.
        for raw in ("not json", "[1, 2]", "null", '"a string"'):
            assert _decode_explanation(raw) is None
            assert _chip(_decode_explanation(raw), "abstain", 50) is None
            assert _chip(_decode_explanation(raw), "protect", 50) is None
        assert _chip(None, "abstain", 50) is None


class TestTheCameBackChip:
    """The countdown chip a returned title wears.

    The hold runs in months against a date the operator cannot see, so the queue row
    states how long is left without anything being opened. Every case here drives the
    real ``_chip`` over a real ``ReturnedGate`` result, never a transcribed detail string,
    so a reworded gate fails these rather than silently dropping the chip to its fallback.
    """

    def _fired(self, *, days_ago: float, by_reaper: bool, hold: int = 400) -> dict[str, Any]:
        """One PROTECT result off the real gate, in the shape ``_explain`` freezes."""
        facts = Facts(
            title="x",
            days_observed_unwatched=Known(value=5000.0, source="t"),
            distinct_watchers=Known(value=0, source="t"),
            distinct_watchers_all_time=Known(value=0, source="t"),
            size_bytes=Known(value=1, source="t"),
            imdb_rating_tenths=Absent(source="t"),
            imdb_votes=Absent(source="t"),
            season_rank=Absent(source="t"),
            is_streaming_now=Known(value=False, source="t"),
            is_managed=Known(value=True, source="t"),
            in_curated_list=Absent(source="t"),
            is_whitelisted=Known(value=False, source="t"),
            returned_days_ago=Known(value=days_ago, source="reaper"),
            returned_by_reaper=Known(value=by_reaper, source="reaper"),
        )
        result = ReturnedGate(config=GateConfig(threshold=hold)).evaluate(facts)
        assert result.outcome == PROTECT
        return {"gate": result.gate.value, "detail_key": to_wire(result.detail)}

    def test_the_chip_states_how_long_is_left(self) -> None:
        chip = _chip(_exp(20, fired=[self._fired(days_ago=35, by_reaper=True)]), "protect", 20)
        assert chip is not None
        reason = _chip_reason(chip)
        assert reason.id == "came_back"
        assert "days_left" in reason.params

    def test_it_is_the_outlined_tone_and_not_the_filled_one(self) -> None:
        # Filled means the owner decided. This is Reaper's decision, and it expires.
        chip = _chip(_exp(20, fired=[self._fired(days_ago=35, by_reaper=False)]), "protect", 20)
        assert chip is not None
        assert chip.tone == "held"

    def test_it_takes_the_chip_from_a_protection_that_fired_first(self) -> None:
        """The one protection with an expiry wins the slot, wherever it sits in the list.

        Every other protection is re-decided next scan and has nothing to count down, so a
        card led by one of those would never tell the operator when this hold ends.
        """
        streaming = {"gate": "streaming_now", "detail": "someone is watching it right now"}
        chip = _chip(
            _exp(20, fired=[streaming, self._fired(days_ago=35, by_reaper=True)]), "protect", 20
        )
        assert chip is not None
        assert chip.tone == "held"

    def test_a_hand_spare_still_wins(self) -> None:
        # The owner's own decision, and it carries its own countdown already. Only the
        # typed id is matched now. A legacy row's frozen hand-spare sentence no longer
        # stands out from any other legacy whitelisted keep, so this is proven on the
        # shape every live scan actually freezes.
        spare = {"gate": "whitelisted", "detail_key": to_wire(HAND_SPARE_REASON)}
        chip = _chip(
            _exp(20, fired=[spare, self._fired(days_ago=35, by_reaper=True)]), "protect", 20
        )
        assert chip is not None
        assert chip.tone == "kept"
        assert _chip_reason(chip) == Reason("kept.hand_spare")

    def test_an_unparseable_detail_costs_the_number_and_not_the_chip(self) -> None:
        # A stored row from a build that worded the detail differently. Vague but true, and
        # the next scan restores the countdown (the fallback every parser here has).
        chip = _chip(
            _exp(20, fired=[{"gate": "returned", "detail": "something else entirely"}]),
            "protect",
            20,
        )
        assert chip is not None
        assert chip.tone == "held"
        assert _chip_reason(chip) == Reason("came_back_unknown")

    def test_the_sentence_form_is_a_full_sentence(self) -> None:
        # The sentence is what the season row prints, and what the override frame
        # (shell.statusChip.reapRequestedKept) nests as {why}. It is a full sentence now,
        # never a lowercase clause riding after a colon.
        chip = _chip(_exp(20, fired=[self._fired(days_ago=35, by_reaper=True)]), "protect", 20)
        assert chip is not None
        sentence = reason_text(_chip_reason(chip), namespace="chip.sentence")
        assert sentence[0].isupper()
        assert sentence.endswith(".")
        assert sentence == "It left your library and came back."

    def test_the_reason_helper_words_it_too(self) -> None:
        # A member with no arm falls to "kept.unknown", which is what makes a missing one
        # silent. This one has its own arm, reached only when this helper is called
        # directly. `_came_back_chip` always answers first on a live row.
        assert _kept_reason(
            "returned", legacy("this left your library and came back, 1 year left")
        ) == Reason("kept.returned")


class TestTheReasonLineAgreesWithTheChip:
    """The card's chip and the reason line beneath it read the same explanation, so they
    must never describe two different decisions about one row.

    An abstain that reaches the end of ``_primary_reason`` was stopped either by the score
    or by the coverage floor, and the remedies are opposite. Move the slider, or fix the
    evidence source. If ``_chip`` made the distinction and ``_primary_reason`` did not, the
    panel could print "Too little of it could be checked" directly above "Scored below
    your threshold," about a row whose score was actually over that threshold.

    Both halves call the real functions rather than a transcription. The point is that the
    two agree, which a copy of either one cannot show.
    """

    def test_the_coverage_floor_is_not_reported_as_a_low_score(self) -> None:
        exp = _exp(82)  # threshold 70, so this abstain can only be the coverage floor
        reason = _primary_reason(exp, "abstain", 82)
        chip = _chip(exp, "abstain", 82)

        assert chip is not None
        assert _chip_reason(chip) == Reason("coverage")
        assert reason is not None
        assert reason_text(reason) == "Kept to be safe: too little of it could be checked."
        assert "threshold" not in reason_text(reason)

    def test_a_genuinely_low_score_still_says_so(self) -> None:
        """The other arm. Below the threshold, the score really is the reason, and the
        line must not degrade to the coverage sentence for every abstain."""
        exp = _exp(42)
        reason = _primary_reason(exp, "abstain", 42)
        chip = _chip(exp, "abstain", 42)

        assert chip is not None
        assert _chip_reason(chip) == Reason("below_threshold", {"score": 42, "threshold": 70})
        assert reason is not None
        assert reason_text(reason) == "Scored below your threshold."

    def test_a_blocked_check_still_outranks_both(self) -> None:
        """The coverage branch sits last, so it can only be reached past every blocked
        case. Adding it must not swallow the unchecked protection above it."""
        exp = _exp(
            82,
            unknown=[{"gate": "min_dormancy", "detail": "could not check x: y"}],
        )
        blocked = _primary_reason(exp, "abstain", 82)
        assert blocked is not None
        assert reason_text(blocked) == "could not check x: y"

    def test_an_explanation_with_no_threshold_falls_back_rather_than_guessing(self) -> None:
        """A stored row predating the threshold key, or carrying a malformed one, has no
        number to compare against. It reports the old line rather than asserting a
        coverage floor it cannot show fired."""
        for raw in ("{}", json.dumps({"threshold": "seventy"}), json.dumps({"threshold": None})):
            exp = _decode_explanation(raw)
            assert exp is not None
            assert _primary_reason(exp, "abstain", 82) == Reason("below_threshold")


class TestTheChipSentence:
    """The chip's ``chip.sentence`` form. A full sentence, ready to stand alone on a
    season row or nest into ``shell.statusChip.reapRequestedKept``'s ``{why}`` slot.

    The sentence is composed from the typed ``reason``, so these are the assertions that
    hold ``chip.text`` and ``chip.sentence`` in step for every id the chip's typed reason
    can carry.
    """

    def test_a_fired_protection_composes_both_forms(self) -> None:
        chip = _chip(
            _exp(
                90, fired=[{"gate": "streaming_now", "detail": "someone is watching it right now"}]
            ),
            "protect",
            90,
        )
        assert chip is not None
        reason = _chip_reason(chip)
        assert reason_text(reason, namespace="chip.text") == "Kept, playing right now"
        assert reason_text(reason, namespace="chip.sentence") == "It's playing right now."

    @pytest.mark.parametrize(
        ("explanation", "verdict", "score", "sentence"),
        [
            (_exp(82, match_status="unmatched"), "abstain", 82, "It couldn't be found in Plex."),
            (
                _exp(50, match_status="ambiguous"),
                "abstain",
                50,
                "It looks like two different things in Plex.",
            ),
            (
                _exp(
                    82,
                    unknown=[
                        {
                            "gate": "season_progression",
                            "detail_key": to_wire(_conflict_reason(kept_watchers=0)),
                            "defers_to_owner": True,
                        }
                    ],
                ),
                "abstain",
                82,
                "Someone watched more than a season your rule keeps.",
            ),
            (
                _exp(60, unknown=[{"gate": "custom", "detail_key": to_wire(_FUTURE_GATE_REASON)}]),
                "abstain",
                60,
                "A check on it couldn't be settled.",
            ),
            (
                _exp(50, unknown=[{"gate": "min_dormancy", "detail": "could not check when: no"}]),
                "abstain",
                50,
                "Some checks couldn't run.",
            ),
        ],
    )
    def test_every_blocked_lane_composes_its_own_sentence(
        self, explanation: dict[str, Any], verdict: str, score: int, sentence: str
    ) -> None:
        chip = _chip(explanation, verdict, score)
        assert chip is not None
        assert reason_text(_chip_reason(chip), namespace="chip.sentence") == sentence

    @pytest.mark.parametrize(
        ("explanation", "verdict", "score"),
        [
            (_exp(82), "abstain", 82),  # the coverage floor
            (_exp(42), "abstain", 42),  # under the threshold
        ],
    )
    def test_a_chip_about_the_score_still_composes_a_sentence(
        self, explanation: dict[str, Any], verdict: str, score: int
    ) -> None:
        """Every id composes a sentence now, including these two: an item that merely
        scored low is reaped when the owner asks, since nothing is holding it. The
        frontend tells this apart from a real refusal by the reason's id and tone, not by
        a null sentence, since there is no null to read any more.
        """
        chip = _chip(explanation, verdict, score)
        assert chip is not None
        reason = _chip_reason(chip)
        assert reason.id in {"coverage", "below_threshold", "below"}
        assert reason_text(reason, namespace="chip.sentence")

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
                            "detail_key": to_wire(_conflict_reason(kept_watchers=0)),
                            "defers_to_owner": True,
                        }
                    ],
                ),
                "abstain",
                82,
            ),
            (
                _exp(60, unknown=[{"gate": "custom", "detail_key": to_wire(_FUTURE_GATE_REASON)}]),
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
    def test_every_sentence_is_a_capitalized_full_stop(
        self, explanation: dict[str, Any], verdict: str, score: int
    ) -> None:
        """A ``chip.sentence`` is a whole sentence, not a clause. It has a capital lead and
        a full stop, and never carries the chip's own "Kept," or "Needs a look," lead
        inside it."""
        chip = _chip(explanation, verdict, score)
        assert chip is not None
        sentence = reason_text(_chip_reason(chip), namespace="chip.sentence")
        assert sentence[0].isupper()
        assert sentence.endswith(".")
        assert not sentence.startswith(("Kept,", "Needs a look,"))


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
    lanes, plus a movie. This is the shape the group view exists to show whole."""
    settings = Settings(data_dir=tmp_path, secret_key="k")
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
                # Inserted out of season order on purpose. The group view must sort.
                season(
                    3,
                    "abstain",
                    82,
                    _exp_json(
                        82,
                        unknown=[
                            {
                                "gate": "season_progression",
                                "detail_key": to_wire(_conflict_reason(kept_watchers=0)),
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
        """A row in one lane still describes the whole show's shape. Its strip marks
        every season across every lane, so the card can show kept and condemned
        side by side."""
        page = client.get("/api/candidates", params={"verdict": "abstain"}).json()
        rows = page["items"]
        assert len(rows) == 1
        row = rows[0]
        assert row["season_number"] == 3
        # The typed reason travels with the chip, so a held reap on this row can compose
        # its sentence without the frontend parsing the text back apart. "p": None is
        # explicit, not omitted. Nothing on this route sets exclude_none.
        assert row["chip"] == {
            "tone": "look",
            "reason": {"k": "look.comparable", "p": None},
        }
        # The strip rides the show's own rollup, sent once beside the rows rather than
        # copied onto each of them.
        rollup = next(g for g in page["groups"] if g["group_key"] == row["group_key"])
        marks = rollup["seasons"]
        assert [(m["season"], m["verdict"]) for m in marks] == [
            (1, "protect"),
            (2, "condemn"),
            (3, "abstain"),
        ]
        # Each mark carries its season's own candidate id, so clicking a strip square
        # opens that season's reasoning. This row is season 3, so its mark points back
        # to it, and every mark's id is a real candidate.
        assert all(isinstance(m["id"], int) for m in marks)
        assert next(m["id"] for m in marks if m["season"] == 3) == row["id"]

    def test_movie_rows_bring_no_rollup(self, client: TestClient) -> None:
        page = client.get("/api/candidates", params={"verdict": "condemn"}).json()
        movie = next(r for r in page["items"] if r["media_type"] == "movie")
        assert movie["group_key"] is None
        assert movie["season_number"] is None
        assert movie["chip"] is None  # condemned cards keep the amber pill instead
        # No show, so nothing to roll up. Asserted as the exact set the page carries. The
        # rollup's own key is a required string, so "no entry is null" holds however many
        # entries a movie contributed.
        assert {g["group_key"] for g in page["groups"]} == {"sonarr:5:42"}


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
        assert group["reason_key"] == to_wire(_conflict_reason(kept_watchers=0))

    def test_unknown_show_is_a_404(self, client: TestClient) -> None:
        assert client.get("/api/groups/sonarr:5:999").status_code == 404

    def test_the_group_view_is_behind_auth(self, tmp_path: Path) -> None:
        authless_dir = tmp_path / "authless"
        authless_dir.mkdir()
        settings = Settings(data_dir=authless_dir, secret_key="k")
        engine = sa_create_engine(settings.sync_database_url)
        Base.metadata.create_all(engine)
        engine.dispose()
        with TestClient(create_app(settings)) as anonymous:
            assert anonymous.get("/api/groups/sonarr:5:42").status_code == 401


class TestTheMatchStatusVocabulary:
    """The status is produced in one place and read in six, across two trees.

    This is one fact about what happened to an item, written as a different sentence on
    every surface that shows it. Adding a new status widens the gap unless every one of
    those surfaces is updated with it, so these tests pin the whole vocabulary instead of
    just the member that motivated the change.
    """

    def test_every_non_matched_status_holds_a_hand_reap(self) -> None:
        """The one that fails open if it drifts.

        A hand-written list of match states would let a new status ship reapable by
        default: Reaper refusing to say what a file is, while still offering to delete it.
        ``BAD_MATCH_STATES`` is derived from the enum instead, so this asserts the
        derivation still covers the population.
        """
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

        ``BAD_MATCH_STATES`` can only ever enumerate the statuses this build defines, so a
        stored value from outside that set (a row frozen by a later build and then rolled
        back, or a corrupted explanation) is not in it. Testing it as plain membership
        would read that value as a clean bind and let the reap through on a row the
        resolver never identified. ``bad_match`` is phrased against the single clean value
        instead, so the unknown ones hold.
        """
        exp = {"match": {"status": status}, "protections_fired": [], "protections_unknown": []}
        assert status not in BAD_MATCH_STATES, "fixture must sit outside the derived set"
        assert reap_override_verdict_decoded(exp, score=99) == "protect", (
            f"a stored match status of {status!r} is not a confident bind, so a hand reap "
            "must be held. See condemned.MATCH_CLEAN."
        )

    def test_a_confident_bind_still_lets_a_hand_reap_through(self) -> None:
        """The other half of the pair, so the test above cannot pass by holding everything.

        This uses a complete document, because a reap is refused on any row the why panel
        cannot render, and a row carrying only the two protections lists is one of those.
        An incomplete document would hold this reap for a reason that has nothing to do
        with the bind, quietly making the pair agree by holding everything after all.
        """
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

    def test_every_status_has_its_own_no_key_reason(self) -> None:
        """Every non-matched status names its own cause, and no two share a string. A
        missing entry falls back to the ``unmatched`` wording, a wrong, definite statement
        ("we couldn't find this in Plex") about an item Reaper did find.

        Both the movie and season lanes read one shared table through
        ``gates.no_key_reason``. The panel's ICU ``mediaType`` select carries whatever
        wording difference each lane needs.
        """
        wanted = {s for s in identity.MatchStatus if s is not identity.MatchStatus.MATCHED}
        assert set(NO_KEY_REASON_IDS) == wanted
        assert len(set(NO_KEY_REASON_IDS.values())) == len(wanted), "two statuses share one reason"

    def test_no_reason_is_typed_by_hand(self) -> None:
        """The gate under the copy check below, and the one that makes it complete.

        A reason written as a literal at its ``Unknown(...)`` site is invisible to every
        drift test, because there is no name to walk. A hand-maintained list of reasons can
        only ever check the ones somebody remembered to name.

        So every reason is a constant, and this reads the call rather than the line. An
        ``ast`` walk sees a reason split over two lines, wrapped in parentheses, or passed
        positionally, none of which a regex on ``Unknown(reason="`` would catch. A local
        variable passes, since its value came from a constant or a map that the walk below
        covers, and so does a stored value thawed off a dict.
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
        # The population this ban scans, reconciled by hand. A reason that stops being an
        # ``Unknown(...)`` call leaves the walk silently, and a flag-shaped assertion
        # cannot tell that from a tree that complies. Bump this count deliberately when
        # the population changes, and check that a reason leaving the walk did not take
        # its only panel coverage with it.
        assert walked == 51, (
            f"the Unknown(reason=...) population moved to {walked}. If you added one, name\n"
            "its reason as a *_REASON constant and bump this count; if one left, check it\n"
            "did not take its only coverage with it."
        )

    def test_the_season_guard_names_a_check_the_panel_can_word(self) -> None:
        """The other half of the same sentence, run off the producer.

        The check id is taken from ``guard_result`` itself rather than typed here.
        Re-id the producer without updating the catalog, and this fails, with a message
        that says which file to open. The panel composes the check slot from
        ``why.check.*``, so an id with no entry renders as its bare id beside four labels
        written for the operator.
        """
        plan = SeriesPrunePlan(series_title="Show", prunable=[2])
        detail = guard_result(plan, 2, progress_unknown_reason="plex_unmatched").detail
        assert detail.id == "blocked"
        check = detail.params["check"]
        assert isinstance(check, Reason)
        what = check.id.removeprefix("check.")
        assert what in catalog()["check"], (
            f"the season guard writes check id {what!r}, and the catalog's why.check has no\n"
            "entry for it (frontend/src/locales/en/ui.json), so the panel prints the bare id."
        )

    def test_every_field_and_fixed_check_has_catalog_copy(self) -> None:
        """The check-slot drift guard.

        Check ids come from two closed sets. The per-field ids (``fields.BY_KEY``, whose
        blocked conditions and keeps pass ``check.<key>``), and the fixed ids the gates
        pass by hand. Every member of both needs a ``why.check.*`` entry, or a blocked row
        renders its bare id. The fixed list is pinned here and reconciled by hand against
        the ``blocked_reason``/``keep_unchecked`` call sites. Field ids also need their
        ``why.field.*`` subject, which the condition sentences compose, and the policy
        editor's own copy, ``policyRules.fieldHelp.*`` for a field that carries help and
        ``policyRules.fieldUnit.*`` for one that carries a unit. Neither ships over the
        wire any more. Both are checked in both directions against the pinned
        ``_FIELDS_WITH_HELP``/``_FIELDS_WITH_UNIT`` sets, so a field gaining or losing one
        is caught either way.
        """
        checks = set(catalog()["check"])
        field_entries = set(catalog()["field"])
        fixed = {
            "imdb_rating",
            "imdb_votes",
            "active_streams",
            "watch_history",
            "watch_horizon",
            "last_watched",
            "recent_watchers_window",
            "season_progress",
            "lists",
        }
        missing = (fixed | set(fields_registry.BY_KEY)) - checks
        assert not missing, f"check ids with no why.check entry: {sorted(missing)}"
        assert set(fields_registry.BY_KEY) - field_entries == set(), (
            "field keys with no why.field entry: "
            f"{sorted(set(fields_registry.BY_KEY) - field_entries)}"
        )
        help_entries = set(catalog("policyRules.fieldHelp"))
        assert help_entries == _FIELDS_WITH_HELP, (
            "policyRules.fieldHelp entries drifted from the pinned set of fields that carry "
            f"help text: missing {sorted(_FIELDS_WITH_HELP - help_entries)}, extra "
            f"{sorted(help_entries - _FIELDS_WITH_HELP)}"
        )
        unit_entries = set(catalog("policyRules.fieldUnit"))
        assert unit_entries == _FIELDS_WITH_UNIT, (
            "policyRules.fieldUnit entries drifted from the pinned set of fields that carry "
            f"a unit: missing {sorted(_FIELDS_WITH_UNIT - unit_entries)}, extra "
            f"{sorted(unit_entries - _FIELDS_WITH_UNIT)}"
        )

    def test_every_backend_cause_has_operator_copy_in_the_panel(self) -> None:
        """A ``why.cause.*`` catalog entry turns each backend reason into the sentence the
        owner reads. A key with no entry there renders the backend's raw phrasing instead
        (``why.ts``'s missing-entry fallback), which is how internal wording reaches the
        screen. The failure message names the file to edit, because a comment asking the
        next author to remember does nothing.

        The population is discovered, not hand-listed: every module-level ``*_REASON``
        string under ``src/reaper``. A new reason is covered the moment it is named, and
        the only way out is an entry in ``_NO_PANEL_ROUTE`` that says why. The walk reads
        ``why.cause`` from the catalog, which is where this copy lives now.
        """
        causes = _catalog_causes()
        discovered = _reason_constants()
        assert discovered, "the *_REASON walk found nothing, so this test proves nothing"
        checked = {name: value for name, value in discovered.items() if name not in _NO_PANEL_ROUTE}
        missing = [
            f"{name}  {value!r}"
            for name, value in sorted(
                {
                    **checked,
                    **{
                        f"NO_KEY_REASON_IDS[{s.name if s else None}]": r
                        for s, r in NO_KEY_REASON_IDS.items()
                    },
                }.items()
            )
            if value not in causes
        ]
        assert not missing, (
            "these reasons reach the why-panel with no plain-language entry under why.cause\n"
            "(frontend/src/locales/en/ui.json), so the box prints the raw id at the owner:\n  "
            + "\n  ".join(missing)
        )

    def test_every_panel_cause_has_a_live_producer(self) -> None:
        """The other direction the forward walk does not check. That test goes producer to
        copy, so an entry whose backend reason was retired or reworded can stay in the map,
        reading as a live translation of a sentence this build can no longer compose. A map
        walked only one way is only half pinned.

        An orphaned entry is not automatically wrong, which is why the way out is a written
        claim rather than a deletion. The panel renders stored explanations, so an entry
        may still be serving rows frozen before its producer changed. Deleting it would
        print the raw backend phrase at the operator holding one of those rows.

        The population is the same one the forward walk trusts. Every module-level
        ``*_REASON`` string under ``src/reaper``, plus the ``NO_KEY_REASON_IDS`` map. A
        producer spelled some other way is invisible here and reads as orphaned, which
        costs a written line rather than a wrong claim at the operator.

        The composed-cause family (``_COMPOSED_CAUSE_IDS``) is the population the constant
        discovery cannot see, because those ids are built at the call site with params. It
        is pinned by hand beside its producers and counted into this reconciliation.
        """
        keys = set(_catalog_causes())
        # Pin what the walk covers, reconciled by hand against the catalog. Bump this
        # count deliberately when a cause entry is added or removed.
        assert len(keys) == 24, (
            f"why.cause holds {len(keys)} entries. If you added or removed one, bump this\n"
            "count deliberately."
        )
        producers = set(_reason_constants().values())
        producers |= set(NO_KEY_REASON_IDS.values())
        producers |= set(_COMPOSED_CAUSE_IDS)
        orphaned = [
            k
            for k in sorted(keys)
            if k not in producers and k not in _PANEL_COPY_WITHOUT_A_PRODUCER
        ]
        assert not orphaned, (
            "this catalog cause copy translates a reason nothing in src/reaper writes any\n"
            "more (frontend/src/locales/en/ui.json). Either delete the entry, or add it to\n"
            "_PANEL_COPY_WITHOUT_A_PRODUCER here saying which stored rows still reach it:\n  "
            + "\n  ".join(repr(k) for k in orphaned)
        )
        for key, why in _PANEL_COPY_WITHOUT_A_PRODUCER.items():
            assert why.strip(), f"{key!r} is exempt with no reason written"
            assert key in keys, (
                f"{key!r} is listed as catalog copy outliving its producer, but why.cause has "
                "no such entry. The exemption is stale."
            )
            assert key not in producers, (
                f"{key!r} is listed as having no producer, but one writes it again. Drop the "
                "exemption: the entry is live copy, not back-compat."
            )

    def test_the_reason_with_no_panel_route_still_has_one_way_out(self) -> None:
        """The exemption above is a claim about wiring, so it is checked rather than
        trusted. A reason excused from needing panel copy must have no entry either, or
        the excuse is stale and the copy it does have is unreachable.
        """
        causes = _catalog_causes()
        discovered = _reason_constants()
        assert set(_NO_PANEL_ROUTE) <= set(discovered), (
            "_NO_PANEL_ROUTE names a constant that no longer exists: "
            f"{sorted(set(_NO_PANEL_ROUTE) - set(discovered))}"
        )
        for name, why in _NO_PANEL_ROUTE.items():
            assert why.strip(), f"{name} is exempt with no reason written"
            assert discovered[name] not in causes, (
                f"{name} is exempt from needing panel copy because {why}, but why.cause has "
                "an entry for it. One of the two is wrong (rule 25)."
            )


class TestTheChipVocabulary:
    """Every id ``_chip`` can emit, under the ``chip`` catalog namespace, checked in both
    directions, modeled on ``TestTheMatchStatusVocabulary`` above.

    ``_CHIP_IDS`` is hand-maintained rather than AST-discovered. Chip ids are inline
    literals across many branches of ``_kept_reason``, ``_chip``, and ``_came_back_chip``,
    not each a named module constant the way ``*_REASON`` is. For a hand-maintained
    population, the count is reconciled by hand and pinned, which is what the first test
    below does. The other two prove the catalog agrees with the population in both
    directions.
    """

    def test_the_chip_id_population_is_pinned(self) -> None:
        assert len(_CHIP_IDS) == 35, (
            f"_CHIP_IDS holds {len(_CHIP_IDS)}. If you added or removed a chip id, bump "
            "this count deliberately."
        )

    def test_every_chip_id_has_text_and_sentence_copy(self) -> None:
        texts = _leaf_ids(catalog("chip.text"))
        sentences = _leaf_ids(catalog("chip.sentence"))
        missing_text = _CHIP_IDS - texts
        missing_sentence = _CHIP_IDS - sentences
        assert not missing_text, f"chip ids with no chip.text entry: {sorted(missing_text)}"
        assert not missing_sentence, (
            f"chip ids with no chip.sentence entry: {sorted(missing_sentence)}"
        )

    def test_every_catalog_chip_entry_has_a_producer(self) -> None:
        texts = _leaf_ids(catalog("chip.text"))
        sentences = _leaf_ids(catalog("chip.sentence"))
        orphaned_text = texts - _CHIP_IDS
        orphaned_sentence = sentences - _CHIP_IDS
        assert not orphaned_text, f"chip.text entries with no producer: {sorted(orphaned_text)}"
        assert not orphaned_sentence, (
            f"chip.sentence entries with no producer: {sorted(orphaned_sentence)}"
        )
