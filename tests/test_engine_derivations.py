# SPDX-License-Identifier: AGPL-3.0-or-later
"""Things this codebase derived twice, or transcribed once and left to drift.

Five of the same shape, each low severity on its own and each a way for two parts of the
engine to disagree about one number:

* the frozen-evidence field list was hand-maintained beside the dataclass it mirrors (R-1);
* "days dormant" was derived twice and the two disagreed at a boundary -- one floored, the
  other floated -- so an item could bucket one side of a threshold and score the other (R-2);
  both readers were the retired lab engines, and what R-2 left behind is the single
  ``engine.dormancy`` derivation every surviving lane takes;
* the streamed dataset download opted out of the retry every other read gets (I-5);
* a whole second condemn/protect/abstain decision function sat in the engine, reachable
  only from its own tests (H-2);
* the stored why-panel document is written by hand in ``services.snapshot._explain`` and
  declared as models in ``engine.explanation``, and the reader drops what it does not
  name (W5-1).
"""

from __future__ import annotations

import dataclasses
import importlib.util
import json
from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path
from typing import Any, get_args

import httpx
import httpx2
import pytest
import respx
from pydantic import BaseModel

from reaper.clients.base import IntegrationError
from reaper.clients.public import PublicClient
from reaper.clock import utcnow
from reaper.engine import facts_codec, identity
from reaper.engine.dormancy import dormancy_days, reference_instant
from reaper.engine.explanation import Explanation, read_explanation
from reaper.engine.gates import ABSTAIN, PROTECT, Evaluation, Facts, GateId, GateResult
from reaper.engine.observation import Absent, Known, Observation, Unknown
from reaper.engine.policy import DEFAULT_MOVIE_POLICY
from reaper.engine.reason import legacy
from reaper.engine.signals import KeepResult, Score, SignalId, SignalResult, SignalState
from reaper.services.fairness import WatchEvidence
from reaper.services.snapshot import _explain

NOW = utcnow()


# ---------------------------------------------------------------------------
# R-1: the frozen-evidence field list
# ---------------------------------------------------------------------------


class TestTheFrozenFieldListFollowsTheDataclass:
    """``_OBS_FIELDS`` used to be typed out beside ``Facts``. Forgetting to extend it did not
    raise and did not fail a test: the new field carries an ``Absent`` default, so it
    constructed fine and round-tripped as ``Absent`` -- silently dropping whatever keep
    discount the real value would have earned, on the lane where ``Absent`` fails OPEN."""

    def test_it_covers_every_observation_field_and_nothing_else(self) -> None:
        every = {f.name for f in dataclasses.fields(Facts)}
        assert set(facts_codec._OBS_FIELDS) == every - {"title", "ratings"}

    def test_a_field_added_to_facts_is_picked_up_without_being_listed(self) -> None:
        """A new field is picked up without anyone listing it, tested against a stand-in
        for a future ``Facts`` rather than waiting for the commit that adds one."""

        @dataclasses.dataclass(frozen=True)
        class _FutureFacts:
            title: str
            size_bytes: Observation[int]
            some_new_signal: Observation[bool]
            ratings: tuple[str, ...] = ()

        assert facts_codec._observation_fields(_FutureFacts) == ("size_bytes", "some_new_signal")

    def test_a_field_this_module_cannot_encode_is_loud(self) -> None:
        """Not an observation and not one of the two handled by hand: nothing here would
        serialize it, so it would vanish across the freeze in silence. Raised at import, which
        is a build failure, never a scan-time one."""

        @dataclasses.dataclass(frozen=True)
        class _BadFacts:
            title: str
            note: str

        with pytest.raises(RuntimeError, match="neither Observation-typed nor handled"):
            facts_codec._observation_fields(_BadFacts)

    def _facts(self) -> Facts:
        return Facts(
            title="A Film",
            days_observed_unwatched=Known(value=400, source="tautulli"),
            distinct_watchers=Known(value=0, source="tautulli"),
            distinct_watchers_all_time=Known(value=2, source="tautulli"),
            size_bytes=Known(value=8_000_000_000, source="radarr"),
            imdb_rating_tenths=Known(value=73, source="imdb"),
            imdb_votes=Known(value=1000, source="imdb"),
            season_rank=Absent(source="movie"),
            is_streaming_now=Known(value=False, source="tautulli"),
            is_managed=Known(value=True, source="radarr"),
            in_curated_list=Absent(source="lists"),
            is_whitelisted=Known(value=False, source="whitelist"),
        )

    def test_a_snapshot_written_before_a_field_existed_thaws_unknown(self) -> None:
        """Old snapshots outlive the code that wrote them, so deriving the list on the write
        side moves the problem to the read side. ``Unknown`` is the honest reading -- that
        scan never looked -- and the fail-safe one: gates abstain on it and the scorer adds no
        pressure. ``Absent`` would assert a real absence nobody observed."""
        frozen = facts_codec.facts_to_dict(self._facts())
        del frozen["obs"]["requested"]  # a scan from before the field was added

        thawed, _ = facts_codec.facts_from_dict(frozen)

        # `Unknown` and `Absent` are disjoint, so this one assert carries both halves:
        # a second `not isinstance(..., Absent)` beside it could never fail (rule 118).
        assert isinstance(thawed.requested, Unknown)
        # And the fields that ARE recorded still come back exactly as they went in.
        assert thawed.days_observed_unwatched == Known(value=400, source="tautulli")

    def test_an_empty_stored_body_does_not_crash_the_simulator(self) -> None:
        """``facts_json`` is nullable, and the simulator reads it as ``"{}"`` when it is. That
        used to be a ``KeyError`` a hundred frames into a re-decide; now it is an item with no
        evidence at all, which reads Unknown everywhere and therefore keeps."""
        thawed, extra = facts_codec.facts_from_dict({})
        assert extra == ()
        assert all(isinstance(getattr(thawed, name), Unknown) for name in facts_codec._OBS_FIELDS)


# ---------------------------------------------------------------------------
# R-2: one dormancy derivation
# ---------------------------------------------------------------------------


class TestDormancyIsDerivedOnce:
    def test_a_play_is_what_it_measures_from(self) -> None:
        played = NOW - timedelta(days=10)
        assert (
            reference_instant(
                last_played=played, added_at=NOW - timedelta(days=900), horizon=NOW - timedelta(1)
            )
            == played
        )

    def test_never_played_measures_from_the_later_of_arrival_and_the_horizon(self) -> None:
        """Never from epoch 0, which reads as ~20,600 days -- the maximum condemnation
        pressure the scale can express, for the item we know least about. And never from
        before our evidence begins: a mirror one year deep cannot say a file has been ignored
        for five, only that it was not watched within reach."""
        added = NOW - timedelta(days=900)
        horizon = NOW - timedelta(days=365)
        assert reference_instant(last_played=None, added_at=added, horizon=horizon) == horizon
        assert reference_instant(last_played=None, added_at=NOW, horizon=horizon) == NOW

    def test_a_play_alone_is_enough_with_no_arrival_date(self) -> None:
        """The thaw the two lanes used to spell differently (#272, #257).

        Dormancy *is* days since the last play, so a missing arrival date is not on its own a
        reason to refuse to measure. `snapshot.build_facts` used to take Unknown the moment
        `added_at` was missing whatever history it held, while `season_scan` measured from the
        play -- one derived value, two thaw rules. The helper owns the branch now (rule 104),
        so both lanes get this answer.
        """
        played = NOW - timedelta(days=10)
        assert (
            reference_instant(last_played=played, added_at=None, horizon=NOW - timedelta(days=365))
            == played
        )

    def test_neither_a_play_nor_an_arrival_date_measures_from_nothing(self) -> None:
        """The one state that genuinely cannot be measured, and the only one that may thaw to
        Unknown. Returning the horizon here instead would fabricate a Known dormancy out of a
        record we hold no evidence about -- which is the epoch-0 failure wearing a later date,
        and it condemns. `None` is what makes each lane render it as Unknown, which blocks both
        dormancy gates and abstains, so the item is kept."""
        assert (
            reference_instant(last_played=None, added_at=None, horizon=NOW - timedelta(days=365))
            is None
        )

    def test_it_floors_rather_than_rounds(self) -> None:
        """Dormancy sits on the condemn lane, so reducing its precision must move it toward
        LESS pressure (rule 31). An item an hour short of a 90-day floor stays kept."""
        reference = NOW - timedelta(days=89, hours=23, minutes=59)
        assert dormancy_days(reference, now=NOW) == 89

    def test_a_reference_in_the_future_is_negative_not_clamped(self) -> None:
        """A play after the cutoff means the evidence and the clock disagree. Callers treat
        that as "cannot be judged"; inventing a 0 would score an item on a contradiction."""
        assert dormancy_days(NOW + timedelta(days=2), now=NOW) < 0


# ---------------------------------------------------------------------------
# I-5: the streamed download
# ---------------------------------------------------------------------------


class TestTheStreamedDownloadIsRetried:
    """Every other read rides the shared retry; this one called ``self._client.stream``
    directly, so one transient blip two hundred megabytes into the ratings dataset aborted
    the whole transfer and forced a restart from zero."""

    async def test_a_transient_blip_is_retried_and_the_file_is_whole(
        self, httpx2_mock: respx.Router, tmp_path: Path
    ) -> None:
        route = httpx2_mock.get("https://mirror.test/data.tsv.gz").mock(
            side_effect=[
                httpx2.ConnectError("blip"),
                httpx.Response(200, content=b"the whole dataset"),
            ]
        )
        destination = tmp_path / "data.tsv.gz"
        async with PublicClient("https://mirror.test") as client:
            await client.stream_to("/data.tsv.gz", destination)

        assert destination.read_bytes() == b"the whole dataset"
        assert route.call_count == 2, "the first attempt was not retried"

    async def test_a_retry_replaces_the_partial_file_rather_than_appending(
        self, httpx2_mock: respx.Router, tmp_path: Path
    ) -> None:
        """An attempt restarts from the beginning, so the destination is reopened ``"wb"``.
        Appending would leave a body that still parses -- as a dataset of the wrong contents,
        which nothing downstream would notice."""
        destination = tmp_path / "data.tsv.gz"
        destination.write_bytes(b"leftovers from the last attempt")
        httpx2_mock.get("https://mirror.test/data.tsv.gz").mock(
            side_effect=[httpx2.ReadError("blip"), httpx.Response(200, content=b"fresh")]
        )
        async with PublicClient("https://mirror.test") as client:
            await client.stream_to("/data.tsv.gz", destination)

        assert destination.read_bytes() == b"fresh"

    async def test_a_persistent_failure_still_maps_to_the_domain_error(
        self, httpx2_mock: respx.Router, tmp_path: Path
    ) -> None:
        """The retry must not swallow the mapping: what survives every attempt is final, and
        the caller owns the temp-file-and-rename, so it has to be told."""
        route = httpx2_mock.get("https://mirror.test/data.tsv.gz").mock(
            side_effect=httpx2.ConnectError("down")
        )
        async with PublicClient("https://mirror.test") as client:
            with pytest.raises(IntegrationError, match="unreachable"):
                await client.stream_to("/data.tsv.gz", tmp_path / "data.tsv.gz")

        assert route.call_count == 3

    async def test_a_4xx_is_not_retried(self, httpx2_mock: respx.Router, tmp_path: Path) -> None:
        """A definite answer from the mirror is not a transport failure. Retrying a 404 wastes
        the budget and delays the error."""
        route = httpx2_mock.get("https://mirror.test/data.tsv.gz").mock(
            return_value=httpx.Response(404)
        )
        async with PublicClient("https://mirror.test") as client:
            with pytest.raises(IntegrationError, match="HTTP 404"):
                await client.stream_to("/data.tsv.gz", tmp_path / "data.tsv.gz")

        assert route.call_count == 1


# ---------------------------------------------------------------------------
# H-2: the second decision function
# ---------------------------------------------------------------------------


class TestThereIsOnlyOneDecisionFunction:
    def test_the_requester_rule_is_gone(self) -> None:
        """``engine/requester.py`` held a complete CONDEMN/PROTECT/ABSTAIN decision with its
        own ``Verdict`` type, parallel to ``engine.verdict.decide_verdict``. Nothing in
        production called it: Scales was rebuilt to sit on the last scan rather than re-judge
        requests live, and only its own tests were left. Rule 38 -- it lands with a consumer
        or it does not exist."""
        assert importlib.util.find_spec("reaper.engine.requester") is None

    def test_the_evidence_it_carried_lives_with_the_surface_that_reads_it(self) -> None:
        evidence = WatchEvidence(plays_by_user={7: 3}, distinct_watchers=1)
        assert evidence.plays_by(7) == 3
        assert evidence.plays_by(9) == 0

    def test_an_unlinked_account_reads_as_no_plays_not_as_an_error(self) -> None:
        """A Seerr account with no Plex link has history Reaper cannot see. Every caller
        guards the ``None`` itself before acting; here it is simply zero."""
        assert WatchEvidence(plays_by_user={7: 3}, distinct_watchers=1).plays_by(None) == 0


# ---------------------------------------------------------------------------
# W5-1: the stored explanation, written by hand and declared elsewhere
# ---------------------------------------------------------------------------


#: The two keys ``_explain`` writes on ``protections_unknown`` and on neither of the other
#: two protection lists. One model types all three, and ``GateOutcomeOut``'s own docstring
#: rests on the asymmetry: an entry in the other two reads ``None`` for a reason that is not
#: the third state, so a reader must take the flag off ``protections_unknown``. Stated here
#: as the one exception to "the writer names what the reader declares", so the walk below
#: has no hole in it.
_UNKNOWN_ONLY_GATE_FLAGS = frozenset({"defers_to_owner", "unestablishable"})


def _models_in(annotation: object) -> Iterator[type[BaseModel]]:
    """Every model reachable inside one annotation, at any nesting depth.

    Recursive rather than one level deep. This is rule 147's bound, read off an annotation
    instead of source text: the tree spells these two ways today, ``MatchOut | None`` and
    ``list[SignalContribution]``. Their combination ``list[X] | None`` is the natural spelling
    for a block added to a document that must still read old rows. One ``get_args`` pass sees
    ``(list[X], NoneType)``, and neither is a model, so that block would leave the walk in
    silence and its entries would never be compared.
    """
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        yield annotation
    for arg in get_args(annotation):
        yield from _models_in(arg)


def _nested_models(model: type[BaseModel]) -> dict[str, type[BaseModel]]:
    """Each field of ``model`` holding another model, or a list of one, as ``{key: model}``.

    Derived off the declaration rather than listed, so a block added to ``Explanation``
    enters the walk below without anyone extending it. A hand-written list is the failure
    rule 145 names: a member the walk never collected is missing from the guard and from
    the proof of the guard alike.
    """
    found: dict[str, type[BaseModel]] = {}
    for name, field in model.model_fields.items():
        for nested in _models_in(field.annotation):
            found.setdefault(name, nested)
    return found


def _written_explanation() -> dict[str, Any]:
    """One stored explanation from the real writer, with every block populated at once.

    **A shape, not a scan.** No single item produces a merged bind *and* a list of
    candidate rows *and* all three protection lists; the subject here is which keys are
    written, and a realistic vector leaves half of them unexercised. Every optional
    argument is passed a value that is not its default for the same reason (rule 141).
    """
    evaluation = Evaluation(
        results=[
            GateResult(
                gate=GateId.STREAMING_NOW,
                outcome=PROTECT,
                detail=legacy("someone is watching it right now"),
            ),
            GateResult(
                gate=GateId.MIN_DORMANCY,
                outcome=ABSTAIN,
                detail=legacy(
                    "untouched for 5 years, past the 3 years it has to sit unwatched first"
                ),
            ),
            GateResult(
                gate=GateId.SEASON_PROGRESSION,
                outcome=ABSTAIN,
                detail=legacy("watched more than a season your rule keeps"),
                blocked=True,
                defers_to_owner=True,
                unestablishable=False,
            ),
        ]
    )
    frozen = Score(
        value=52.0,
        coverage=0.875,
        results=[
            SignalResult(
                signal=SignalId.UNWATCHED,
                pressure=31.4,
                weight=40,
                detail=legacy("untouched for 5 years"),
                evaluated=True,
                state=SignalState.ADDS,
                floor=90,
                saturate_at=730,
            )
        ],
        base_value=67.0,
        keep_discount=15.0,
        keep_results=[
            KeepResult(
                name="Rated well",
                discount=15.0,
                max_discount=20,
                detail=legacy("rated 8.1 by 40,000 people"),
                evaluated=True,
            )
        ],
    )
    document: dict[str, Any] = json.loads(
        _explain(
            evaluation,
            frozen,
            DEFAULT_MOVIE_POLICY,
            plex_rating_key=4242,
            matched_by=identity.MatchedBy.MERGED_LISTINGS,
            match_detail="one file listed twice in Plex",
            match_status=identity.MatchStatus.MATCHED,
            merged_rating_keys=(4242, 4243),
            match_candidates=(4242, 4243),
            watch_blind=False,
            rewatch_odds={
                "n": 599,
                "k": 207,
                "lo_days": 365.0,
                "hi_days": 730.0,
                "state": "measured",
            },
        )
    )
    return document


def _blocks(document: dict[str, Any]) -> Iterator[tuple[str, set[str], type[BaseModel]]]:
    """Every object in the written document, as ``(label, its keys, the model typing it)``.

    A declared block the writer never wrote is skipped rather than raising here: the
    top-level comparison already names it, and a ``KeyError`` mid-walk would fail the run
    before the rest of the document was read.
    """
    yield "<top level>", set(document), Explanation
    for key, model in _nested_models(Explanation).items():
        value = document.get(key)
        if isinstance(value, list):
            for index, entry in enumerate(value):
                yield f"{key}[{index}]", set(entry), model
        elif isinstance(value, dict):
            yield key, set(value), model


#: The two lists the flags above are NOT written on. Matched against the whole block name
#: rather than as a prefix, so a field spelled `protections_fired_since` cannot inherit the
#: exemption by looking like one of these.
_LISTS_WITHOUT_THE_GATE_FLAGS = frozenset({"protections_fired", "protections_checked"})


def _declared(label: str, model: type[BaseModel]) -> set[str]:
    """The keys the writer owes this block: its model's fields, less the stated exceptions.

    On the signal, keep and protection rows, ``detail`` is declared and deliberately never
    written: it is the prose of a row frozen before details were typed (docs/history/I18N_PLAN.md
    §5), so only stored legacy rows carry it and every fresh row writes ``detail_key``
    instead. The ``match`` block's own ``detail`` is untouched audit prose and stays owed."""
    fields = set(model.model_fields)
    root = label.split("[")[0]
    if root in {
        "signals",
        "keeps",
        "protections_fired",
        "protections_checked",
        "protections_unknown",
    }:
        fields -= {"detail"}
    if root in _LISTS_WITHOUT_THE_GATE_FLAGS:
        return fields - _UNKNOWN_ONLY_GATE_FLAGS
    return fields


class TestTheStoredExplanationIsWrittenAsItIsDeclared:
    """``snapshot._explain`` builds the why-panel document by hand; ``engine.explanation``
    declares it as pydantic models. The declaration is the READ side, so it drops any key it
    does not name -- pydantic's ``extra="ignore"`` -- and that is exactly how ``keeps`` and
    ``match`` were written on every scan for months while the panel rendered neither.

    Nothing pinned the key set before this class. The baseline fixture reads nine of the
    thirteen top-level keys and four fields of a signal row (``tests/_policy_lab.py``). So a
    key added on one side alone was invisible to the whole suite, in both directions: a field
    declared and never written reads ``None`` forever.
    """

    def test_the_writer_and_the_declaration_name_the_same_keys(self) -> None:
        """Every object in the document, against the model that types it. Collected rather
        than asserted per block, so a failure names each disagreement instead of the first,
        and the message names both files, since whichever one a reader is holding the fix is
        usually in the other (rule 144)."""
        document = _written_explanation()

        wrong = [
            f"{label}: written-only {sorted(keys - declared)}, "
            f"declared-only {sorted(declared - keys)}"
            for label, keys, model in _blocks(document)
            if (declared := _declared(label, model)) != keys
        ]

        assert not wrong, (
            "services/snapshot.py's _explain and engine/explanation.py's models describe one "
            "document and disagree about it. A written-only key is DROPPED on read "
            '(pydantic\'s extra="ignore"); a declared-only key reads None forever.\n'
            + "\n".join(wrong)
        )

    def test_the_two_gate_flags_ride_on_the_unknown_list_alone(self) -> None:
        """The exception above, stated positively. ``_explain`` writes ``defers_to_owner``
        and ``unestablishable`` on every ``protections_unknown`` entry and on no other, which
        is what lets ``api.review._chip`` read a missing key as "this row cannot say" instead
        of as a gate with no opinion. One entry can appear in two lists -- an unestablishable
        season PROTECT does -- and only the ``protections_unknown`` copy carries the flags."""
        document = _written_explanation()

        assert all(
            set(entry) >= _UNKNOWN_ONLY_GATE_FLAGS for entry in document["protections_unknown"]
        )
        assert all(
            _UNKNOWN_ONLY_GATE_FLAGS.isdisjoint(entry)
            for key in _LISTS_WITHOUT_THE_GATE_FLAGS
            for entry in document[key]
        )

    def test_the_walk_covers_every_block_the_declaration_holds(self) -> None:
        """Rule 145: the assertions above can only fail on a block the walk collected, so the
        population it collected is pinned rather than assumed. Seven objects over five models,
        counted by hand against the fixture: the document, its ``match``, one signal, one keep,
        and one entry in each of the three protection lists.

        **Labels, never a total.** A fixture landing two entries in one protection list and
        none in another also totals seven. An empty list makes the ``all(...)`` above true over
        nothing, so the flags claim would go unproven with three tests green."""
        document = _written_explanation()

        assert set(_nested_models(Explanation)) == {
            "match",
            "rewatch_odds",
            "signals",
            "keeps",
            "protections_fired",
            "protections_checked",
            "protections_unknown",
        }
        assert {label for label, _, _ in _blocks(document)} == {
            "<top level>",
            "match",
            "rewatch_odds",
            "signals[0]",
            "keeps[0]",
            "protections_fired[0]",
            "protections_checked[0]",
            "protections_unknown[0]",
        }

    def test_the_reader_reads_back_what_the_writer_wrote(self) -> None:
        """The round trip the key sets exist to protect. ``read_explanation`` is the one
        definition of "unreadable" for this document (rule 104) and both the panel and the
        hand-reap path run it, so a writer the reader refuses holds every file. The two
        blocks the reader used to drop are asserted by value, not by presence.

        **The merged keys are asserted RAW as well, and that is the load-bearing half.**
        ``MatchOut.merged_rating_keys`` is a lax ``list[int] | None``, so a stored
        ``["4242", "4243"]`` reads back through it as ``[4242, 4243]`` and the model assertion
        holds. ``executor._equivalent_keys`` filters the raw list on ``isinstance(value, int)``
        and comes back with neither. That is the streaming veto and the played-since-approval
        check losing the second listing of a merged bind."""
        document = _written_explanation()
        body = read_explanation(document)

        assert document["match"]["merged_rating_keys"] == [4242, 4243]
        assert body is not None
        assert body.match is not None and body.match.merged_rating_keys == [4242, 4243]
        assert [keep.name for keep in body.keeps] == ["Rated well"]

    def test_a_row_without_rewatch_odds_reads_as_nothing_to_show(self) -> None:
        """#554 stage 2: a row stored before this field existed carries no key at all, not
        a null value written by a version that knows about it -- the same thaw rule 104
        gives every other optional block here (``match``, ``keeps``)."""
        document = _written_explanation()
        del document["rewatch_odds"]

        body = read_explanation(document)

        assert body is not None
        assert body.rewatch_odds is None

    def test_rewatch_odds_round_trips_the_written_block(self) -> None:
        """The state written by the scan (``services.snapshot._rewatch_odds_context``) is
        exactly the state the panel reads back."""
        document = _written_explanation()

        body = read_explanation(document)

        assert body is not None
        assert body.rewatch_odds is not None
        assert body.rewatch_odds.n == 599
        assert body.rewatch_odds.k == 207
        assert body.rewatch_odds.lo_days == 365.0
        assert body.rewatch_odds.hi_days == 730.0
        assert body.rewatch_odds.state == "measured"
