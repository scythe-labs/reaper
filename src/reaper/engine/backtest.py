# SPDX-License-Identifier: AGPL-3.0-or-later
"""Backtesting -- what this policy would have done, and what you'd have regretted.

Threshold-tuning is otherwise guesswork. A number like "condemn at 70" means
nothing until you can see what it *would have done*, and the only honest way to
show that is to rewind.

The method:

1. Pick a cutoff -- say a year ago.
2. Rebuild each item's facts **as they were on that day**: how long it had gone
   unwatched *then*, how many people had watched it *by then*. Deliberately not
   today's facts; the policy is judged with the information it would have had at
   the time.
3. Score, and see what the policy would have deleted.
4. Then look at what actually happened *afterwards*. Anyone who played a condemned
   item after the cutoff is a **regret** -- a real person who would have gone
   looking for something Reaper had thrown away.

The output is titles, users and dates. Not a percentage. "9% false-positive rate"
tells the owner nothing; *"you would have deleted <film>, and <user> watched it four
months later"* -- naming the real title and the real person -- tells them exactly what
a threshold of 60 costs.

**Engine-complete, not yet reachable.** No route, CLI or UI calls :func:`run` today, so
nothing an operator can click runs a backtest; operator-facing copy must not tell them
to run one until it ships. docs/STATUS.md tracks the wiring (a ``POST /api/policy/backtest``
plus the calibration prior) as open work.

## The honesty problem

A backtest flatters itself if you are not careful. Two rules:

* **Ratings are used as they are today.** IMDb scores move slowly and we have no
  historical archive; pretending otherwise would be fiction. Stated, not hidden.
* **An item added *after* the cutoff cannot be judged.** It did not exist. Skipped,
  and counted, so the coverage of the backtest is visible rather than assumed.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta

import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from reaper.clock import from_epoch
from reaper.engine.calibration import NotCalibratedError, RewatchPrior
from reaper.engine.dormancy import dormancy_days, history_reach_days, reference_instant
from reaper.engine.gates import Evaluation, Facts, Gate, evaluate_all
from reaper.engine.observation import Absent, Known, Observation, Unknown
from reaper.engine.policy import PolicyBody
from reaper.engine.signals import SignalConfig, score
from reaper.engine.verdict import decide_verdict

log = structlog.get_logger(__name__)

#: Why ``days_since_added`` is unreadable for a record Plex reports no arrival date for.
#: The same state the live movie lane calls ``snapshot.NO_ADDED_AT_REASON``, spelled again
#: here because ``engine/`` imports nothing from ``services/`` and this module is on the
#: engine side of that line. Deliberately NOT a copy of an operator-facing sentence (rule
#: 144): no panel renders a backtest, so this string reaches a reader only through a stored
#: explanation that does not exist. If a route ever renders one, it wants a CAUSE_COPY entry
#: and this comment is the wrong answer.
NO_ADDED_AT_REASON = "no added-at date"


@dataclass(frozen=True)
class Regret:
    """Someone watched an item the policy would have deleted -- *after* it was gone.

    A play during the grace period is **not** a regret. The item is still sitting in
    quarantine, and the live re-check immediately before deletion would spare it. So
    those are rescues, and counting them as failures would slander the policy and
    push the owner toward a threshold stricter than they need.
    """

    title: str
    watched_by: str
    watched_at: datetime
    days_after_cutoff: int
    size_bytes: int
    score: float


# THE ONLY PRIOR ANYTHING ACTUALLY USES TODAY. reaper.engine.calibration.derive can
# compute one from the owner's own history, and should, but it has no caller anywhere in
# src/ -- not even this module, which imports only RewatchPrior and NotCalibratedError
# from there -- and nothing calls this module either. So a backtest run from a test or a
# REPL is measured against these figures unless the caller passes its own.
# They are what one real library happened to show, and a different library will have a
# different curve: audience size and viewing habits shape it, so a three-viewer library
# and a hundred-user server land in different places.
#
# They are retained so the engine has a shape to reason about in tests and in the
# absence of history, and NOT because they are meaningful anywhere else.
#
# ⚠️ THE POPULATION MATTERS, AND GETTING IT WRONG INVERTS THE CONCLUSION. These were
# measured over exactly the population the scorer judges. An earlier version computed
# them over every rating_key in the watch log -- which also contains items long since
# deleted, whose structural zeroes dragged every bucket down two- to six-fold and made
# a WORKING scorer look like it had -120% lift.
#
# The other surprise: there is NO CLIFF. A film dormant for five years still had a 13%
# chance of being watched. On an active library nothing is ever free to delete.
FALLBACK_REWATCH_PRIOR: tuple[tuple[int, int, float], ...] = (
    (0, 365, 0.61),
    (365, 548, 0.31),
    (548, 730, 0.32),
    (730, 1095, 0.30),
    (1095, 1825, 0.19),
    (1825, 10**9, 0.13),
)


def rewatch_prior(days_dormant: float) -> float:
    """The fallback curve. Prefer a prior derived from the owner's own history."""
    for low, high, probability in FALLBACK_REWATCH_PRIOR:
        if low <= days_dormant < high:
            return probability
    return 0.13


@dataclass
class BacktestResult:
    cutoff: datetime
    condemn_at: int

    considered: int = 0
    skipped_not_yet_added: int = 0

    skipped_nothing_to_measure: int = 0
    """Items with neither a play nor an arrival date, so dormancy has no instant to count
    from (``dormancy.reference_instant`` returns None).

    Counted apart from ``skipped_not_yet_added`` because they are not a statement about the
    cutoff: the item may well have existed, and a later scan that learns its arrival date
    judges it normally. Folding the two together would report a policy's coverage as bounded
    by the replay date when it is really bounded by the evidence."""

    blocked_unreadable: int = 0
    """Items a protection could not be *checked* on, so no verdict was earned.

    Distinct from ``protected`` (a protection fired) and from an abstain on the score:
    these are rehearsals that could not run at all. It is a whole-run condition more often
    than a per-item one -- a mirror shallower than ``cutoff`` plus the popularity window
    blocks every item, since the windowed watcher count is then a lower bound everywhere
    (``gates.ServerPopularityGate``) -- and without a count of its own the run reports as a
    policy that would delete nothing, which reads identically to a policy that is simply
    cautious. Whoever wires M3c must be able to tell those apart."""

    grace_days: int = 14

    condemned: list[tuple[str, float, int]] = field(default_factory=list)
    """(title, score, size_bytes)"""

    protected: int = 0
    regrets: list[Regret] = field(default_factory=list)

    rescued: list[Regret] = field(default_factory=list)
    """Played DURING the grace window, so the file survives here.

    **A divergence to close before this ships.** The backtest models the window as a delay
    before deletion (``deleted_at = cutoff + grace_days``), but production does not: nothing
    on the deletion path reads the window (see the module docstring on ``services.grace``),
    and the owner may reap on day one. So this count is an upper bound on what grace itself
    rescues. Production's real rescuers are the live per-item vetoes, which catch a play
    after the plan was approved rather than anywhere inside a window. Whoever wires the
    backtest (M3c) either models those vetoes instead, or labels this figure as the
    best case it is. Harmless today only because nothing calls this module."""

    @property
    def condemned_bytes(self) -> int:
        return sum(size for _, _, size in self.condemned)

    @property
    def regret_bytes(self) -> int:
        return sum(r.size_bytes for r in self.regrets)

    condemned_dormancy: list[float] = field(default_factory=list)
    """Days-dormant of each condemned item, for the age-matched baseline."""

    prior: RewatchPrior | None = None
    """The baseline, derived from THIS server's history. When absent, the fallback
    curve is used and the lift figure is only as meaningful as that curve is."""

    @property
    def regret_rate(self) -> float:
        return len(self.regrets) / len(self.condemned) if self.condemned else 0.0

    @property
    def expected_regret_rate(self) -> float:
        """What we would expect by picking RANDOMLY among films of the same age.

        The scorer's whole claim to exist is that it beats this.

        Uses the prior derived from this server's own history where one is available.
        Falls back to a hardcoded curve otherwise -- and a lift figure computed against
        somebody else's library is worth exactly nothing, which is why
        ``prior_is_derived`` is reported alongside it.
        """
        if not self.condemned_dormancy:
            return 0.0
        rates, _ = self._expected_rates()
        return sum(rates) / len(rates)

    @property
    def expected_rate_borrowed_items(self) -> int:
        """How many condemned items had to borrow the fallback curve for their baseline."""
        return self._expected_rates()[1]

    def _expected_rates(self) -> tuple[list[float], int]:
        """Per-item baseline rates, plus how many fell back to the borrowed curve.

        Use the derived prior ONLY when it is fully calibrated -- a thin bucket's rate is
        noise that looks like measurement, so ``prior_is_derived`` refuses the whole
        curve then. But a calibrated prior can still hold EMPTY buckets (nothing in the
        history is that old), and ``RewatchPrior.rate_for`` deliberately raises
        ``NotCalibratedError`` for one rather than guess. A single condemned item landing
        in an empty bucket must not crash lift/beats_random/summary, so that ONE ITEM
        borrows the shared fallback curve, and the count of such items keeps the mixed
        provenance visible in ``summary()``.
        """
        derived = self.prior.rate_for if self.prior is not None and self.prior_is_derived else None
        rates: list[float] = []
        borrowed = 0
        for days in self.condemned_dormancy:
            if derived is not None:
                try:
                    rates.append(derived(days))
                    continue
                except NotCalibratedError:
                    borrowed += 1
            rates.append(rewatch_prior(days))
        return rates, borrowed

    @property
    def prior_is_derived(self) -> bool:
        """Was the baseline measured on THIS library, or borrowed from another?"""
        return self.prior is not None and self.prior.calibrated

    @property
    def lift(self) -> float:
        """How much better than age alone. **The number that decides a signal's fate.**

        Positive: the scorer picks better than an age-matched coin-flip; the signals
        earn their keep.

        Zero: the scorer is dormancy in a trenchcoat.

        **Negative: the signals are WORSE THAN NOTHING.** They displace dormancy in
        the ranking and select films *more* likely to be watched than their age
        implies. That is not a tuning problem, it is a broken scorer, and the first
        version of this engine scored -50%: `SIZE` was condemning 4K blockbusters
        precisely because they were large.

        This number is what the earned-autonomy flow (AutonomyGrant) is designed to
        gate on: a negative lift must refuse the grant. That flow -- like this whole
        module -- is not wired to a route or UI yet, so nothing enforces it today.
        """
        expected = self.expected_regret_rate
        if expected == 0:
            return 0.0
        return (expected - self.regret_rate) / expected

    @property
    def beats_random(self) -> bool:
        return self.lift > 0.05

    def summary(self) -> str:
        gb = self.condemned_bytes / 1_000_000_000
        lines = [
            f"As of {self.cutoff.date()}, at a threshold of {self.condemn_at}:",
            f"  would have deleted   {len(self.condemned):,} items  ({gb:,.0f} GB)",
            f"  protected            {self.protected:,} items",
            f"  could not judge      {self.skipped_not_yet_added:,} (not yet added at the cutoff)",
        ]
        if self.skipped_nothing_to_measure:
            lines += [
                f"  no date to count from {self.skipped_nothing_to_measure:,} "
                "(never played, and no arrival date, so dormancy has no start)",
            ]
        if self.blocked_unreadable:
            share = self.blocked_unreadable / self.considered if self.considered else 0.0
            lines += [
                f"  could not check      {self.blocked_unreadable:,} "
                f"({share:.0%} of them -- a protection was unreadable, so these were "
                "not rehearsed)",
            ]
        lines += [
            f"  rescued in grace     {len(self.rescued):,} "
            f"(played within {self.grace_days}d, so the pre-delete check spares them)",
        ]
        if self.condemned:
            verdict = (
                "the scorer earns its keep"
                if self.beats_random
                else "NOT BEATING AGE ALONE -- do not arm this policy"
            )
            source = (
                "measured on your library"
                if self.prior_is_derived
                else "⚠ borrowed from another library -- not meaningful here"
            )
            borrowed = self.expected_rate_borrowed_items
            if self.prior_is_derived and borrowed:
                source = (
                    f"measured on your library; {borrowed} of "
                    f"{len(self.condemned_dormancy)} items are older than its coverage "
                    "and borrow the fallback curve"
                )
            lines += [
                "",
                f"  regret rate          {self.regret_rate:.0%}",
                f"  age alone predicts   {self.expected_regret_rate:.0%}   ({source})",
                f"  LIFT                 {self.lift:+.0%}   ({verdict})",
            ]
        if self.regrets:
            lines += [
                "",
                f"  REGRETS: {len(self.regrets)} were watched AFTER the grace period expired "
                f"({self.regret_rate:.0%} of deletions)",
            ]
        else:
            lines += ["", "  No regrets: nobody watched any of them after they were gone."]
        return "\n".join(lines)


@dataclass(frozen=True)
class Item:
    """One candidate, with everything the backtest needs to rebuild its past."""

    rating_key: int
    title: str
    size_bytes: int
    added_at: datetime | None
    imdb_rating_tenths: int | None
    imdb_votes: int | None
    is_whitelisted: bool = False
    in_curated_list: str = ""


async def _plays(
    engine: AsyncEngine, rating_key: int, *, media_type: str
) -> list[tuple[int, datetime]]:
    """Every play of an item: ``(user_id, when)``.

    Deliberately no name: the watch mirror stores ids, and ``run`` resolves a display name
    from the Tautulli user list (``names``) at the one place it needs one. A third element
    carrying ``str(user_id)`` and documented as a friendly name read as though regret names
    came from here, which would have shown an operator a bare id where they expect a person.

    For TV we match on ``grandparent_rating_key``: Seerr and the library index
    store the *show*'s key, but history rows are per-episode, so filtering on
    ``rating_key`` finds nothing and reports a well-watched show as never played.
    """
    column = "grandparent_rating_key" if media_type == "tv" else "rating_key"
    async with engine.connect() as conn:
        result = await conn.execute(
            text(
                f"SELECT user_id, watched_at FROM watch_event "  # noqa: S608 -- column is from a literal
                f"WHERE {column} = :key ORDER BY watched_at"
            ),
            {"key": rating_key},
        )
        rows = list(result)

    out: list[tuple[int, datetime]] = []
    for row in rows:
        when = from_epoch(row.watched_at)
        if when is not None:
            out.append((int(row.user_id), when))
    return out


def facts_as_of(
    item: Item,
    plays: list[tuple[int, datetime]],
    *,
    cutoff: datetime,
    horizon: datetime,
    popularity_window_days: int = 365,
    watch_blind_reason: str | None = None,
) -> Facts | None:
    """Rebuild an item's facts as they stood on ``cutoff``.

    Returns None when the item cannot honestly be judged at that date.

    ``watch_blind_reason`` is the rehearsal's copy of ``snapshot.build_facts``'s parameter of
    the same name, and it exists for the same reason: the plays this replay reads are the
    item's history only if the mirror can still see them. When the caller knows they fell
    away -- a re-added file carries a fresh rating key while its earlier plays stay filed
    under the old one -- the reading here is not a quiet zero but an unreadable one, and the
    replay must refuse it exactly as the live scan does. Left unset the replay trusts the
    mirror, which is right for every item whose history never churned.
    """
    if item.added_at is not None and item.added_at > cutoff:
        return None  # It did not exist yet. Judging it would be fiction.

    past = [p for p in plays if p[1] <= cutoff]
    last_played = max((p[1] for p in past), default=None)
    all_time_watchers = {p[0] for p in past}

    # WINDOWED, not all-time. An all-time count protects a film five people watched
    # in 2019 and nobody has touched since, which disables the whole scorer.
    window_start = cutoff - timedelta(days=popularity_window_days)
    recent_watchers = {p[0] for p in past if p[1] >= window_start}

    dormancy: Observation[float]
    recent: Observation[int]
    all_time: Observation[int]
    if watch_blind_reason is not None:
        # Checked BEFORE the measurement, exactly where `snapshot.build_facts` checks it and
        # for the same reason: every input below still looks readable when the plays behind
        # them are not, so measuring first would write a confident Known(0) watchers and
        # maximum dormancy -- the precise reading the live scan now refuses to trust. Both
        # counts go with it, because one mirror answered all three.
        dormancy = Unknown(reason=watch_blind_reason, source="tautulli")
        recent = Unknown(reason=watch_blind_reason, source="tautulli")
        all_time = Unknown(reason=watch_blind_reason, source="tautulli")
    else:
        # The derived field, through the one shared derivation (engine/dormancy.py) the live
        # scan and the prior calibration also use. It used to be computed here as a float
        # while calibration floored, so the same item could bucket one side of a threshold
        # and score the other -- and a backtest that does not measure dormancy the way the
        # scan does is not a rehearsal of anything. Since #277 the *thaw* is shared too: this
        # lane used to drop a record with no arrival date before asking, which quietly held
        # its own second thaw rule and kept a whole population out of the rehearsal.
        reference = reference_instant(
            last_played=last_played, added_at=item.added_at, horizon=horizon
        )
        if reference is None:
            # Neither a play nor an arrival date, so there is nothing to measure dormancy
            # from and no honest rehearsal to run. `run` counts it separately from an item
            # that simply did not exist yet: the two are both skips, but only one of them is
            # a statement about the date.
            return None
        days_unwatched = dormancy_days(reference, now=cutoff)
        if days_unwatched < 0:
            return None
        dormancy = Known(value=days_unwatched, source="tautulli")
        recent = Known(value=len(recent_watchers), source="tautulli")
        all_time = Known(value=len(all_time_watchers), source="tautulli")

    return Facts(
        title=item.title,
        days_observed_unwatched=dormancy,
        distinct_watchers=recent,
        distinct_watchers_all_time=all_time,
        # Measured from the cutoff, not from today: a replay standing at last April had
        # only the history that existed then, and the popularity gate must be as unable to
        # see past the horizon in rehearsal as it is in the live scan.
        history_reach_days=Known(value=history_reach_days(horizon, now=cutoff), source="tautulli"),
        # Measured from the cutoff too, for the same reason: at that date the item had
        # been on the server for exactly this long, and an all-time count needs the mirror
        # to span it (``Facts.days_since_added``). Unknown without an arrival date, the same
        # reading `snapshot.build_facts` gives it: this is the one field a play cannot stand
        # in for, since how long the mirror must reach back is a question about the arrival
        # and nothing else answers it. Unknown withholds the all-time count's protection
        # rather than asserting a span nobody measured.
        days_since_added=(
            Known(value=dormancy_days(item.added_at, now=cutoff), source="plex")
            if item.added_at is not None
            else Unknown(reason=NO_ADDED_AT_REASON, source="plex")
        ),
        size_bytes=Known(value=item.size_bytes, source="radarr"),
        # Stated plainly: today's ratings. IMDb scores move slowly and there is no
        # historical archive, so pretending to know the 2025 value would be fiction.
        imdb_rating_tenths=(
            Known(value=item.imdb_rating_tenths, source="imdb (today's value)")
            if item.imdb_rating_tenths is not None
            else Absent(source="imdb")
        ),
        imdb_votes=(
            Known(value=item.imdb_votes, source="imdb (today's value)")
            if item.imdb_votes is not None
            else Absent(source="imdb")
        ),
        season_rank=Absent(source="radarr"),
        # We cannot know who was streaming a year ago, and it does not matter: the
        # backtest asks what the policy would have SELECTED, and the live run
        # re-checks streaming immediately before deleting anyway.
        is_streaming_now=Known(value=False, source="backtest"),
        is_managed=Known(value=True, source="radarr"),
        in_curated_list=(
            Known(value=item.in_curated_list, source="lists")
            if item.in_curated_list
            else Absent(source="lists")
        ),
        is_whitelisted=Known(value=item.is_whitelisted, source="plex"),
        # Not applicable outside the requester rule: with no requester, "others" is
        # everyone, and the gate would protect anything ever played.
        # The custom-rule fields, spelled out rather than left to their defaults: every
        # builder names every field, so adding one cannot silently change scores here.
        # The historical reconstruction carries none of them -- the dataset is the movie
        # index plus watch history, with no *arr payload to rebuild genres, quality or a
        # release date from. ``Absent`` is the fail-safe reading: a boolean rule does not
        # match, and a graded one adds no pressure while keeping its weight in the
        # denominator.
        requested=Absent(source="backtest"),
        genres=Absent(source="backtest"),
        release_age_days=Absent(source="backtest"),
        quality=Absent(source="backtest"),
        # Movies only, so there is no series status to rebuild -- the same reading the
        # live movie path gives it, and the ``season_rank`` precedent above.
        show_ended=Absent(source="backtest"),
        # No multi-source ratings in the historical reconstruction: the dataset carries a
        # single IMDb value (imdb_rating_tenths above), not Radarr's/Plex's rating objects.
        # Empty means the multi-source keep gate simply does not fire here -- it only ever
        # removes a protection from a read-only simulation, never adds delete pressure.
        ratings=(),
    )


async def run(
    engine: AsyncEngine,
    items: list[Item],
    policy: PolicyBody,
    gates: list[Gate],
    *,
    cutoff: datetime,
    horizon: datetime,
    users: dict[int, str] | None = None,
    grace_days: int = 14,
    prior: RewatchPrior | None = None,
    watch_blind_reasons: Mapping[int, str] | None = None,
) -> BacktestResult:
    """Replay ``policy`` as of ``cutoff`` and measure what it would have cost.

    ``watch_blind_reasons`` maps a rating key to why its plays are unreadable, for the items
    whose history the mirror can no longer show in full (``services.watch_evidence``). It is
    the caller's to supply because the high-water marks it is derived from live in the
    database of record, which this layer does not open; unsupplied, every item is read as
    honestly measured. Passing it is what keeps the rehearsal from scoring a churned item on
    a confident zero that the live scan would refuse -- and refusing there matters twice
    over, because the same churn hides the LATER plays, so a replay that condemns such an
    item records no regret for it either and reports the policy as safer than it is.

    ``grace_days`` matters here because this replay **models the window as a delay before
    deletion** (``deleted_at = cutoff + grace_days``) and counts a play inside it as a
    rescue rather than a regret. **Production does not work that way** -- nothing on the
    deletion path reads the window (see the module docstring on ``services.grace``) and the
    owner may reap on day one, so this split flatters the policy. See ``rescued`` for what
    has to change before these figures are shown to anyone.
    """
    result = BacktestResult(
        cutoff=cutoff,
        condemn_at=policy.condemn_at,
        grace_days=grace_days,
        prior=prior,
    )
    names = users or {}
    blind = watch_blind_reasons or {}
    deleted_at = cutoff + timedelta(days=grace_days)

    popularity_window = policy.popularity_window_days()

    signals = [
        SignalConfig(signal=s.signal, weight=s.weight, saturate_at=s.saturate_at, floor=s.floor)
        for s in policy.signals
    ]
    # Hoisted out of the loop exactly like ``signals``: pure functions of the frozen policy.
    custom_condemn = policy.custom_signal_configs()
    keeps = policy.keep_configs()

    for item in items:
        plays = await _plays(engine, item.rating_key, media_type=policy.media_type)

        facts = facts_as_of(
            item,
            plays,
            cutoff=cutoff,
            horizon=horizon,
            popularity_window_days=popularity_window,
            watch_blind_reason=blind.get(item.rating_key),
        )
        if facts is None:
            # Split on the item's own arrival date rather than re-deriving the branch inside
            # `facts_as_of`: an item with a date was refused for something about the cutoff,
            # and one without was refused for having no instant to measure from at all.
            if item.added_at is not None:
                result.skipped_not_yet_added += 1
            else:
                result.skipped_nothing_to_measure += 1
            continue

        result.considered += 1

        evaluation: Evaluation = evaluate_all(gates, facts)

        # Reach the verdict through the ONE decision function production uses
        # (engine.verdict.decide_verdict, via services.snapshot._verdict): decide on the
        # STORED, ROUNDED integers, not the raw floats. An honest replay must not diverge
        # from the engine at the exact boundary the owner is tuning: an item scoring 69.6
        # rounds to 70 and IS condemned by production, so the backtest must condemn it
        # too (or it under-counts regret at the threshold); and a low-coverage item
        # production would abstain on must not be counted as a deletion here.
        # Custom condemn rules are scored here too, so the lift gate measures the composed
        # formula (this is what catches a size-based custom rule the way it catches built-in
        # SIZE). Metadata fields the historical reconstruction does not populate (genre,
        # quality, ...) read Absent: a boolean rule simply does not match, and a graded
        # rule adds zero pressure while keeping its weight in the denominator -- the same
        # fail-safe reading a live scan gives Absent. A known v1 limitation.
        # The SAME window the count was taken over, at the top of this function:
        # ``facts_as_of`` counted ``distinct_watchers`` over ``popularity_window``, and every
        # reader of that count now checks the mirror's reach against the span the count
        # claims to cover (rule 140, ``fields.reach_shortfall``). Omitting it scores against
        # ``score()``'s 365-day default while the fact was built over the operator's own
        # window, so the rehearsal charges pressure the live scan withholds.
        #
        # Measured on a 730-day window over a 400-day mirror, stating the policy each figure
        # needs, because the headline number is not reachable under the shipped weights:
        #   all 100 weight on FEW_WATCHERS: 100.00/100 coverage 1.00, against 0.00/100
        #     coverage 0.00 when the window is passed.
        #   shipped 70/20/10 weights:        21.68/100 coverage 1.00, against 1.68/100
        #     coverage 0.80.
        # Under the shipped weights neither figure crosses ``condemn_at`` 70, so what the
        # default costs there is the coverage floor's sight of the hole (1.00 vs 0.80), not
        # a condemnation outright. The pressure is charged either way.
        item_score = score(
            signals,
            facts,
            custom_condemn=custom_condemn,
            keeps=keeps,
            window_days=popularity_window,
        )
        verdict = decide_verdict(
            protected=evaluation.protected,
            blocked=evaluation.blocked,
            score=round(item_score.value),
            coverage_bp=round(item_score.coverage * 10_000),
            condemn_at=policy.condemn_at,
            coverage_floor_bp=policy.coverage_floor_bp,
        )
        if verdict == "protect":
            result.protected += 1
            continue
        if verdict != "condemn":
            # An abstain has two very different causes and the summary must not blur them:
            # a protection that could not be checked means the rehearsal did not run on
            # this item, while a low score means it ran and the policy kept the file.
            if evaluation.blocked:
                result.blocked_unreadable += 1
            continue

        result.condemned.append((item.title, item_score.value, item.size_bytes))

        # Record its age, so lift can be measured against an age-matched baseline.
        if isinstance(facts.days_observed_unwatched, Known):
            result.condemned_dormancy.append(facts.days_observed_unwatched.value)

        # Did a real person go looking for this? And -- in this model -- before or after the
        # window expired? A play inside it counts as a RESCUE here, which is the modeling
        # divergence documented on `rescued` and in `run`: production has no such hold, so
        # this is the best case rather than what would happen.
        for user_id, when in plays:
            if when <= cutoff:
                continue

            event = Regret(
                title=item.title,
                watched_by=names.get(user_id, f"user {user_id}"),
                watched_at=when,
                days_after_cutoff=(when - cutoff).days,
                size_bytes=item.size_bytes,
                score=item_score.value,
            )
            if when <= deleted_at:
                result.rescued.append(event)
            else:
                result.regrets.append(event)
            break  # The first play after the cutoff is the one that decides.

    log.info(
        "backtest.done",
        condemned=len(result.condemned),
        regrets=len(result.regrets),
        considered=result.considered,
    )
    return result
