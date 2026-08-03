# SPDX-License-Identifier: AGPL-3.0-or-later
"""Things this codebase derived twice, or transcribed once and left to drift.

Four of the same shape, each low severity on its own and each a way for two parts of the
engine to disagree about one number:

* the frozen-evidence field list was hand-maintained beside the dataclass it mirrors (R-1);
* "days dormant" was floored in the calibration and floated in the backtest, so an item
  could bucket one side of a threshold and score the other (R-2);
* the streamed dataset download opted out of the retry every other read gets (I-5);
* a whole second condemn/protect/abstain decision function sat in the engine, reachable
  only from its own tests (H-2).
"""

from __future__ import annotations

import dataclasses
import importlib.util
from datetime import datetime, timedelta
from pathlib import Path

import httpx
import httpx2
import pytest
import respx

from reaper.clients.base import IntegrationError
from reaper.clients.public import PublicClient
from reaper.clock import utcnow
from reaper.engine import facts_codec
from reaper.engine.backtest import Item, facts_as_of
from reaper.engine.dormancy import dormancy_days, reference_instant
from reaper.engine.gates import Facts
from reaper.engine.observation import Absent, Known, Observation, Unknown
from reaper.services.fairness import WatchEvidence

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

        assert isinstance(thawed.requested, Unknown)
        assert not isinstance(thawed.requested, Absent)
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

    def test_the_backtest_scores_the_same_number_the_calibration_buckets(self) -> None:
        """The dual derivation this fixes: the backtest computed
        ``total_seconds() / 86_400`` while the calibration used ``.days``, so at a bucket
        boundary the same item bucketed one way and scored the other."""
        cutoff = NOW - timedelta(days=365)
        horizon = NOW - timedelta(days=3000)
        added = cutoff - timedelta(days=9, hours=23)
        item = Item(
            rating_key=1,
            title="A Film",
            size_bytes=8_000_000_000,
            added_at=added,
            imdb_rating_tenths=73,
            imdb_votes=500_000,
        )

        facts = facts_as_of(item, [], cutoff=cutoff, horizon=horizon)

        assert facts is not None
        assert isinstance(facts.days_observed_unwatched, Known)
        scored = facts.days_observed_unwatched.value
        assert scored == dormancy_days(
            reference_instant(last_played=None, added_at=added, horizon=horizon), now=cutoff
        )
        assert scored == 9, "9.96 days must read as 9, the bound that argues for keeping"

    def test_the_backtest_reads_plays_as_bare_id_and_instant(self) -> None:
        """A guard, not a proof: ``_plays`` returned a third element documented as a friendly
        name and populated with ``str(user_id)``, which ``run`` never used -- it resolves
        names from the Tautulli user list. Dropping it is what mypy enforces; this pins that
        the reader is happy with the two-element shape."""
        plays: list[tuple[int, datetime]] = [(7, NOW - timedelta(days=30))]
        facts = facts_as_of(
            Item(
                rating_key=1,
                title="A Film",
                size_bytes=1,
                added_at=NOW - timedelta(days=900),
                imdb_rating_tenths=None,
                imdb_votes=None,
            ),
            plays,
            cutoff=NOW,
            horizon=NOW - timedelta(days=3000),
        )
        assert facts is not None
        assert facts.days_observed_unwatched == Known(value=30, source="tautulli")


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
