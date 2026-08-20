# SPDX-License-Identifier: AGPL-3.0-or-later
"""The scoring surface, pinned to the ``SCORER_VERSION`` it was cut against.

An approved run is bound to its snapshot's ``policy_hash`` and the executor refuses to send
when the live hash no longer matches (rule 113). That hash digests the *policy body*, so an
upgrade the body cannot express is invisible to it: add a ``Facts`` field, a signal, a
protection, or re-declare what an operator's stored rule means, and every stored body hashes
exactly as before. Nothing marks the snapshot as predating the build, and the plan already
sitting on the Reap page executes on evidence the old code gathered.

The mechanism that closes that already exists and works. ``scorer_version`` is a
``PolicyBody`` field, ``policy_hash`` digests it with the rest of the body, and
``_pin_to_the_running_scorer`` rewrites a stored body's value to the running constant on
load, so bumping ``SCORER_VERSION`` moves every policy's hash and voids every pending
approval. What did not exist was anything making the author bump it. It was discipline, on
the change most likely to forget: the one that adds evidence rather than editing a threshold.

So this file records the surface, and the version it was recorded at, and fails when the two
disagree. Recorded as a literal list rather than a digest because the record is read by a
reviewer as much as by the suite: an added field shows up in the diff as one line beside a
``SCORER_VERSION`` that did or did not move, where a hex string shows a hex string.

**A stored rule is a string, and its meaning lives here rather than in the body.** A body
carries ``{"field": "on_list", "op": "eq", "value": "..."}`` and nothing more; what
``on_list`` reads, whether the fact is comma-joined, which lanes and operators are legal, and
whether a short watch mirror withholds it are all declared in ``engine.fields.REGISTRY``.
Flipping ``multi`` on one spec makes a saved keep rule stop protecting an item that sits on
two lists -- a protection withdrawn, with the body byte-identical. That is why the registry
is on the record and not only the ``Facts`` field behind it. Same reasoning puts the rating
provenance tables here: ``_PLEX_IMAGE_PREFIXES`` decides whether a Plex-served rating reaches
``Facts.ratings`` at all, so dropping an entry silently narrows the rating-floor keep.

**What it does not catch, stated exactly, because a wrong boundary here is worse than none.**

*Arithmetic reachable from the shipped defaults* is caught elsewhere, not here:
``test_policy_permutations``'s ``TestPinnedBaseline`` replays de-identified fact shapes
through those defaults and goes red when a verdict, score or coverage moves. It asks for the
bump in its failure text, and ``policy_lab_extract.rebaseline`` is what enforces it.

*Arithmetic in the operator-authored lanes* used to be caught by nothing at all, because both
shipped defaults carry ``custom_condemn: ()`` and ``graded_keeps: ()``, so no vector reached
``signals.evaluate_custom`` or ``signals.evaluate_keep``. It is covered now by a second pinned
baseline judged under ``_policy_lab.lane_policy``, whose four rules are chosen to reach the
fail-closed ARMS rather than only the ramps -- a keep on a field that is Known on every vector
pins the ramp and leaves the branch that matters as dead as before.

*What remains uncovered* is the part no fixture can reach: a policy shape nobody has recorded.
The lab replays real shapes, so a rule over a fact combination this library does not contain
is still un-pinned by anything here.
"""

from __future__ import annotations

import dataclasses
import enum
from collections.abc import Iterable

import pytest

from reaper.engine.fields import REGISTRY, FieldSpec, FieldType, Lane, Op
from reaper.engine.gates import POLICY_AUTHORABLE_GATES, Facts, GateId
from reaper.engine.observation import Absent, Known, Unknown
from reaper.engine.policy import SCORER_VERSION
from reaper.engine.signals import SignalId
from reaper.ratings import _PERCENTAGE_SOURCES, _PLEX_IMAGE_PREFIXES, RatingSource

#: The ``SCORER_VERSION`` the surface below was recorded against. Both move together or
#: neither does -- re-recording the list without touching this leaves every operator's
#: pending approval bound to a scorer that no longer exists.
_RECORDED_AT_SCORER_VERSION = 5

#: Every declaration below is one a stored policy body dereferences without carrying, so a
#: change to it re-interprets that body while ``policy_hash`` holds still. Spelled by
#: ``_scoring_surface``.
_RECORDED_SURFACE: tuple[str, ...] = (
    "facts.days_observed_unwatched: Observation[float] = required",
    "facts.days_since_added: Observation[float] = Absent(unset)",
    "facts.distinct_watchers: Observation[int] = required",
    "facts.distinct_watchers_all_time: Observation[int] = required",
    "facts.genres: Observation[str] = Absent(unset)",
    "facts.history_reach_days: Observation[float] = Absent(unset)",
    "facts.imdb_rating_tenths: Observation[int] = required",
    "facts.imdb_votes: Observation[int] = required",
    "facts.in_curated_list: Observation[str] = required",
    "facts.is_managed: Observation[bool] = required",
    "facts.is_streaming_now: Observation[bool] = required",
    "facts.is_whitelisted: Observation[bool] = required",
    "facts.on_lists: Observation[str] = Absent(unset)",
    "facts.quality: Observation[str] = Absent(unset)",
    "facts.ratings: tuple[Rating, ...] = ()",
    "facts.release_age_days: Observation[float] = Absent(unset)",
    "facts.requested: Observation[bool] = Absent(unset)",
    "facts.returned_by_reaper: Observation[bool] = Absent(unset)",
    "facts.returned_days_ago: Observation[float] = Absent(unset)",
    "facts.rewatch_cohort_k: Observation[int] = Absent(unset)",
    "facts.rewatch_cohort_n: Observation[int] = Absent(unset)",
    "facts.rewatch_last_play_days: Observation[float] = Absent(unset)",
    "facts.rewatch_viewings: Observation[int] = Absent(unset)",
    "facts.season_rank: Observation[int] = required",
    "facts.show_ended: Observation[bool] = Absent(unset)",
    "facts.size_bytes: Observation[int] = required",
    "facts.title: str = required",
    "field.days_unwatched: days, lanes=condemn/protect, ops=gte/lte, media=movie/tv",
    "field.days_unwatched: read=days_observed_unwatched, multi=0, reach=-",
    "field.genre: read=genres, multi=1, reach=-",
    "field.genre: text, lanes=condemn/protect, ops=eq/in/contains, media=movie/tv",
    "field.imdb_rating: rating_tenths, lanes=condemn/protect, ops=gte/lte, media=movie/tv",
    "field.imdb_rating: read=imdb_rating_tenths, multi=0, reach=-",
    "field.imdb_votes: count, lanes=protect, ops=gte/lte, media=movie/tv",
    "field.imdb_votes: read=imdb_votes, multi=0, reach=-",
    "field.on_list: read=on_lists, multi=1, reach=-",
    "field.on_list: text, lanes=protect, ops=eq/in/contains, media=movie/tv",
    "field.quality: read=quality, multi=0, reach=-",
    "field.quality: text, lanes=condemn/protect, ops=eq/in/contains, media=movie",
    "field.recent_watchers: count, lanes=condemn/protect, ops=gte/lte, media=movie/tv",
    "field.recent_watchers: read=distinct_watchers, multi=0, reach=popularity_window",
    "field.release_age: days, lanes=condemn/protect, ops=gte/lte, media=movie",
    "field.release_age: read=release_age_days, multi=0, reach=-",
    "field.requested: bool, lanes=condemn/protect, ops=eq, media=movie/tv",
    "field.requested: read=requested, multi=0, reach=-",
    "field.season_rank: count, lanes=condemn/protect, ops=gte/lte, media=tv",
    "field.season_rank: read=season_rank, multi=0, reach=-",
    "field.show_ended: bool, lanes=condemn/protect, ops=eq, media=tv",
    "field.show_ended: read=show_ended, multi=0, reach=-",
    "field.size_bytes: bytes, lanes=condemn/protect, ops=gte/lte, media=movie/tv",
    "field.size_bytes: read=size_bytes, multi=0, reach=-",
    "field.streaming_now: bool, lanes=protect, ops=eq, media=movie/tv",
    "field.streaming_now: read=is_streaming_now, multi=0, reach=-",
    "field.watchers_all_time: count, lanes=protect, ops=gte/lte, media=movie/tv",
    "field.watchers_all_time: read=distinct_watchers_all_time, multi=0, reach=item_lifetime",
    "field.whitelisted: bool, lanes=protect, ops=eq, media=movie/tv",
    "field.whitelisted: read=is_whitelisted, multi=0, reach=-",
    "gate.curated_list: not-authorable",
    "gate.custom: not-authorable",
    "gate.data_horizon: authorable",
    "gate.min_dormancy: authorable",
    "gate.others_watching: not-authorable",
    "gate.rating_floor: authorable",
    "gate.returned: authorable",
    "gate.rewatch_odds: authorable",
    "gate.season_progression: not-authorable",
    "gate.server_popularity: authorable",
    "gate.streaming_now: authorable",
    "gate.unmanaged: not-authorable",
    "gate.whitelisted: not-authorable",
    "rating.plex-prefix.imdb: imdb",
    "rating.plex-prefix.rottentomatoes: rotten_tomatoes_critic",
    "rating.plex-prefix.themoviedb: tmdb",
    "rating.plex-prefix.tmdb: tmdb",
    "rating.source.imdb: average",
    "rating.source.metacritic: percent",
    "rating.source.rotten_tomatoes_audience: percent",
    "rating.source.rotten_tomatoes_critic: percent",
    "rating.source.tmdb: average",
    "rating.source.trakt: average",
    "rating.source.tvdb: average",
    "rating.source.unknown: average",
    "signal.few_watchers",
    "signal.low_rating",
    "signal.season_rank",
    "signal.size",
    "signal.unwatched",
)

_SURFACE_MOVED = (
    "The scoring surface moved. Something a stored policy body dereferences without carrying "
    "now means something else, and that body hashes exactly as it did -- so a plan approved "
    "under the old meaning would still execute (rule 113). Read the diff of this list before "
    "deciding, because the right answer depends on which line moved.\n"
    "  * A NEW field, signal, protection, rule-vocabulary entry or rating source, or a "
    "changed read/multi/lanes/ops/reach: an operator's saved rules are re-interpreted. Bump "
    "SCORER_VERSION in engine/policy.py, or show what else already moves every stored body's "
    "policy_hash -- removing a PolicyBody field does it unconditionally, a loader shim does "
    "it for the bodies it rewrites. See this file's docstring for the worked example.\n"
    "  * A type annotation RE-SPELLED with no change of meaning (Observation[float] -> "
    "Observation[int | float]) moves this record and nothing else. Re-record with no bump. "
    "That is not a loophole: the line you edit is in the diff beside the one you did not.\n"
    "Then set _RECORDED_AT_SCORER_VERSION to the running SCORER_VERSION."
)

_VERSION_MOVED = (
    "SCORER_VERSION and this record disagree about which scorer is running. This says "
    "nothing about whether the surface moved -- check the sibling assertion for that. If you "
    "just bumped the constant, set _RECORDED_AT_SCORER_VERSION to match and re-run "
    "`uv run python scripts/policy_lab_extract.py --rebaseline` so the baseline fixture "
    "carries the same stamp."
)


def _gate_role(gate: GateId) -> str:
    """Whether a policy body may still carry this gate.

    Two answers rather than four, and the reason is scope, not convenience. Which mechanism
    took a gate away -- retired outright, converted into a keep rule by a shim, emitted by
    the engine with no policy row -- is pinned by ``test_policy.py``
    (``test_every_unbuildable_gate_id_is_declared_retired``), which asserts the full
    partition. Recording it a second time here would be a copy that can disagree. What this
    record needs, and what ``POLICY_AUTHORABLE_GATES`` states directly, is only whether the
    operator can still name it.
    """
    return "authorable" if gate in POLICY_AUTHORABLE_GATES else "not-authorable"


#: ``FieldSpec`` attributes that are wording, not meaning. Every other attribute changes how
#: a stored rule is evaluated and goes on the record. Split out rather than listing the
#: behavioral ones, so a new attribute defaults to being *recorded*: forgetting to classify
#: a display string costs a re-record, forgetting to classify a behavioral one costs a
#: protection. ``test_every_field_spec_attribute_is_classified`` fails until it is decided.
_DISPLAY_ONLY_SPEC_ATTRS = frozenset(
    {
        "label",
        "help_text",
        "unit_suffix",
    }
)

#: A ``Facts`` whose every observation announces its own field name, so ``spec.read`` can be
#: asked which fact it dereferences instead of the answer being transcribed beside it. A read
#: that stops being a bare attribute access still fingerprints, as whatever it returns.
_PROBE_FACTS = Facts(
    title="probe",
    ratings=(),
    **{  # type: ignore[arg-type]
        field.name: Known(value=field.name, source="probe")
        for field in dataclasses.fields(Facts)
        if field.name not in ("title", "ratings")
    },
)


def _fact_read_by(spec: FieldSpec) -> str:
    got = spec.read(_PROBE_FACTS)
    if isinstance(got, Known) and isinstance(got.value, str):
        return got.value
    return f"unrecognized:{got!r}"


def _default_of(field: dataclasses.Field[object]) -> str:
    """How a ``Facts`` field reads when nobody set it.

    On the record because the default is the whole fail-safe direction and the annotation
    does not show it: ``_UNSET`` is an ``Absent``, which is fail-closed on the condemn and
    gate lanes and fail-OPEN on the keep lane, where ``Unknown`` takes the full discount.
    Swapping one for the other changes every score built by a fact builder that skips the
    field, and leaves both the name and the type exactly as they were.
    """
    if field.default is dataclasses.MISSING and field.default_factory is dataclasses.MISSING:
        return "required"
    default = field.default
    if isinstance(default, Absent):
        return f"Absent({default.source})"
    if isinstance(default, Unknown):
        return f"Unknown({default.source})"
    if isinstance(default, Known):
        return f"Known({default.value!r})"
    return repr(default)


def _scoring_surface(
    *,
    facts_cls: type = Facts,
    signal_ids: Iterable[object] = tuple(SignalId),
    gate_ids: Iterable[GateId] = tuple(GateId),
    field_specs: Iterable[FieldSpec] = REGISTRY,
    rating_sources: Iterable[object] = tuple(RatingSource),
) -> tuple[str, ...]:
    """Every declaration a stored policy body leans on without carrying.

    Derived from the declarations themselves rather than transcribed beside them, so the only
    hand-maintained copy is the recorded one this is compared against -- which is the copy a
    reviewer reads.

    Each population is a parameter so a test can hand it a stand-in for some future commit's
    version and prove the walk renders a member it has never seen (rule 145). An earlier
    version took only ``facts_cls``, which left four of the five walks pinned by nothing:
    filtering the signal and gate comprehensions down to the already-recorded members kept
    the whole file green.
    """
    lines = [
        f"facts.{field.name}: {field.type} = {_default_of(field)}"
        for field in dataclasses.fields(facts_cls)
    ]
    lines += [f"signal.{signal.value}" for signal in signal_ids]  # type: ignore[attr-defined]
    lines += [f"gate.{gate.value}: {_gate_role(gate)}" for gate in gate_ids]
    for spec in field_specs:
        lines.append(
            f"field.{spec.key}: {spec.type.value}, "
            f"lanes={'/'.join(lane.value for lane in spec.lanes)}, "
            f"ops={'/'.join(op.value for op in spec.ops)}, "
            f"media={'/'.join(spec.media_types)}"
        )
        lines.append(
            f"field.{spec.key}: read={_fact_read_by(spec)}, multi={int(spec.multi)}, "
            f"reach={spec.reach_span.value if spec.reach_span else '-'}"
        )
    lines += [
        f"rating.source.{source.value}: "  # type: ignore[attr-defined]
        f"{'percent' if source in _PERCENTAGE_SOURCES else 'average'}"
        for source in rating_sources
    ]
    lines += [
        f"rating.plex-prefix.{prefix}: {source.value}"
        for prefix, source in _PLEX_IMAGE_PREFIXES.items()
    ]
    return tuple(sorted(lines))


class TestTheSurfaceAndTheScorerVersionMoveTogether:
    def test_the_recorded_surface_is_the_one_the_code_declares(self) -> None:
        """The gate. It fails on the commit that adds the field, not on the server that
        deletes under it."""
        assert _scoring_surface() == _RECORDED_SURFACE, _SURFACE_MOVED

    def test_the_record_names_the_running_scorer(self) -> None:
        """The other half. Re-recording the surface while leaving this behind would swallow
        the failure above and leave every pending approval bound to the superseded scorer,
        which is the bug this file exists for.

        It carries its own message rather than sharing one with the assertion above. They
        fail for opposite reasons -- that one when the code moved, this one when it did not
        -- and a shared message opened by "the scoring surface moved" is a false statement on
        the honest path where an author bumps the constant for an arithmetic change.
        """
        assert _RECORDED_AT_SCORER_VERSION == SCORER_VERSION, _VERSION_MOVED


class TestTheWalkSeesWhatItClaimsTo:
    """Rule 118 and rule 145: a matching record proves nothing about a walk that stopped
    collecting, and a walk is only proven against a member it did not already hold. One
    probe per population, because four of the five used to be pinned by nothing."""

    def test_a_facts_field_this_commit_has_never_seen_reaches_the_surface(self) -> None:
        added = "surface_walk_probe"
        assert added not in {f.name for f in dataclasses.fields(Facts)}, (
            f"{added} is a real field now; the stand-in needs a name Facts does not carry, or "
            "make_dataclass raises on the duplicate and this reads as a walk that broke"
        )
        future_facts = dataclasses.make_dataclass(
            "Facts",
            [
                *(
                    (f.name, f.type, dataclasses.field(default=None))
                    for f in dataclasses.fields(Facts)
                ),
                (added, "Observation[int]", dataclasses.field(default=None)),
            ],
            frozen=True,
        )

        surface = _scoring_surface(facts_cls=future_facts)

        assert f"facts.{added}: Observation[int] = None" in surface
        assert surface != _RECORDED_SURFACE

    def test_a_signal_this_commit_has_never_seen_reaches_the_surface(self) -> None:
        # The functional StrEnum form builds a member list mypy cannot read, which is exactly
        # why it is used here: a signal id the enum does NOT ship yet.
        future = enum.StrEnum(  # type: ignore[misc]
            "SignalId", {m.name: m.value for m in SignalId} | {"PROBE": "surface_walk_probe"}
        )

        surface = _scoring_surface(signal_ids=tuple(future))

        assert "signal.surface_walk_probe" in surface

    def test_a_gate_this_commit_has_never_seen_reaches_the_surface(self) -> None:
        # The functional StrEnum form builds a member list mypy cannot read, which is exactly
        # why it is used here: a signal id the enum does NOT ship yet.
        future = enum.StrEnum(  # type: ignore[misc]
            "GateId", {m.name: m.value for m in GateId} | {"PROBE": "surface_walk_probe"}
        )

        surface = _scoring_surface(gate_ids=tuple(future))

        # Not in POLICY_AUTHORABLE_GATES, so the role resolves without a second declaration.
        assert "gate.surface_walk_probe: not-authorable" in surface

    def test_a_rule_vocabulary_entry_this_commit_has_never_seen_reaches_the_surface(
        self,
    ) -> None:
        """The population the first version of this file missed entirely, and the one a
        stored rule dereferences by string."""
        probe = FieldSpec(
            key="surface_walk_probe",
            label="Probe",
            help_text="Probe",
            type=FieldType.COUNT,
            lanes=(Lane.CONDEMN,),
            ops=(Op.GTE,),
            read=lambda f: f.size_bytes,
        )

        surface = _scoring_surface(field_specs=(*REGISTRY, probe))

        assert "field.surface_walk_probe: read=size_bytes, multi=0, reach=-" in surface
        assert "field.surface_walk_probe: count, lanes=condemn, ops=gte, media=movie/tv" in surface

    def test_a_rating_source_this_commit_has_never_seen_reaches_the_surface(self) -> None:
        # The functional StrEnum form builds a member list mypy cannot read, which is exactly
        # why it is used here: a signal id the enum does NOT ship yet.
        future = enum.StrEnum(  # type: ignore[misc]
            "RatingSource",
            {m.name: m.value for m in RatingSource} | {"PROBE": "surface_walk_probe"},
        )

        surface = _scoring_surface(rating_sources=tuple(future))

        assert "rating.source.surface_walk_probe: average" in surface

    def test_a_read_that_stops_pointing_at_its_fact_moves_the_record(self) -> None:
        """``read=`` is a lambda, so the record cannot hold its source. It holds the answer
        instead, discovered by probe -- and the probe has to be the thing that moves, not the
        spec's key, or a re-pointed read would record identically."""
        repointed = FieldSpec(
            key="on_list",
            label="On a protected list",
            help_text="Probe",
            type=FieldType.TEXT,
            lanes=(Lane.PROTECT,),
            ops=(Op.EQ,),
            read=lambda f: f.title,  # type: ignore[arg-type,return-value]
        )

        surface = _scoring_surface(field_specs=(repointed,))

        assert not any(line.startswith("field.on_list: read=on_lists") for line in surface)


class TestTheRecordCoversEveryDeclarationItWalks:
    def test_every_field_spec_attribute_is_classified(self) -> None:
        """A new ``FieldSpec`` attribute is either wording or meaning, and the author decides
        which. Unclassified defaults to meaning, so this fails rather than letting a new
        behavioral knob join the vocabulary unrecorded (the shape ``test_policy.py``'s
        ``test_every_body_field_is_classified`` uses on the body).
        """
        recorded = {"key", "type", "lanes", "ops", "read", "media_types", "multi", "reach_span"}
        declared = {f.name for f in dataclasses.fields(FieldSpec)}

        assert not (recorded & _DISPLAY_ONLY_SPEC_ATTRS), (
            "an attribute is classified as both recorded and display-only, which makes this "
            "test blind to a change of set"
        )
        assert recorded | _DISPLAY_ONLY_SPEC_ATTRS == declared, (
            f"unclassified FieldSpec attributes: "
            f"{sorted(declared - recorded - _DISPLAY_ONLY_SPEC_ATTRS)}. Decide whether each "
            "changes how a stored rule evaluates (record it in _scoring_surface) or is only "
            "wording (add it to _DISPLAY_ONLY_SPEC_ATTRS)."
        )

    def test_a_facts_annotation_is_the_string_the_record_was_cut_from(self) -> None:
        """``field.type`` is source text under ``from __future__ import annotations`` and the
        type object without it, and the record was cut from the first.

        Deliberately not claiming what dropping that import from ``engine.gates`` would do.
        Measured: it does not silently re-spell the record, it raises ``NameError`` at import
        because ``Gate.evaluate`` annotates ``Facts`` before the class exists. And in a module
        where it *is* reachable, the ``Observation[...]`` lines render identically anyway --
        only ``title`` and ``ratings`` move. So this pins the form the record holds, which is
        the assumption that matters, and asserts nothing about a scenario it cannot reach
        (rule 118: a test that cannot discriminate must not read as a proof).
        """
        annotation = {f.name: f.type for f in dataclasses.fields(Facts)}["size_bytes"]

        assert isinstance(annotation, str)
        assert annotation == "Observation[int]"

    @pytest.mark.parametrize(
        ("default", "expected"),
        [
            (Absent(source="unset"), "Absent(unset)"),
            (Unknown(reason="could not read", source="plex"), "Unknown(plex)"),
            (Known(value=3, source="sonarr"), "Known(3)"),
            ((), "()"),
        ],
    )
    def test_a_default_renders_the_arm_it_actually_is(self, default: object, expected: str) -> None:
        """``_default_of`` discriminates the three observation arms, and ``Absent`` versus
        ``Unknown`` is the distinction the whole field is recorded for. A fallthrough to
        ``repr`` would still produce a record and still re-cut cleanly, so the arms are
        pinned by value rather than by the record agreeing with itself."""
        made = dataclasses.make_dataclass(
            "Probe", [("f", "object", dataclasses.field(default=default))]
        )

        assert _default_of(dataclasses.fields(made)[0]) == expected

    def test_a_field_with_no_default_is_recorded_as_required(self) -> None:
        """The other arm of the same branch, which the parametrize above cannot reach: a
        required field and a field defaulting to ``None`` must not record identically."""
        made = dataclasses.make_dataclass("Probe", [("f", "object")])

        assert _default_of(dataclasses.fields(made)[0]) == "required"
