# SPDX-License-Identifier: AGPL-3.0-or-later
"""The dangerous-config detector: settings that are legal but probably not what the owner meant.

``PolicyBody`` refuses configuration that is provably wrong. ``inspect`` catches the rest,
which a validator cannot: an IMDb floor of ``96`` (meaning 9.6) is as legal as ``75``, and
only one of them is what someone typing a Rotten Tomatoes number intended. Every warning here
carries a typed reason, a catalog id under ``warning.*`` plus raw parameters
(docs/history/I18N_PLAN.md §5), so the frontend can compose what the setting actually does and
show its effect in the operator's own words.

Split out of ``engine/policy.py``, which holds the model and the hash over it. A warning never
reaches a verdict: ``inspect`` reads a body and returns typed reasons, and nothing feeds the
result back into scoring. This module imports the model from ``policy``, and ``policy``
imports nothing back, so the two modules cannot form an import cycle.
"""

from __future__ import annotations

from typing import Literal, assert_never

from reaper.engine.fields import (
    BY_KEY,
    Op,
    ReachSpan,
    can_add_pressure_under_a_shortfall,
)
from reaper.engine.gates import GateId, history_shortfall, progress_is_establishable
from reaper.engine.observation import Known
from reaper.engine.policy import (
    ConditionSpec,
    Frozen,
    GradedCondemnSpec,
    GradedKeepSpec,
    PolicyBody,
    ProfileSettings,
)
from reaper.engine.reason import Reason, ReasonParam
from reaper.engine.signals import MAX_SCORE, SignalId
from reaper.engine.verdict import decide_verdict
from reaper.ratings import is_percentage_source

#: How loudly a warning reads. Declared once so ``inspect``'s local ``warn`` helper, which
#: defaults to the quieter of the two, does not need its own copy of the pair.
Severity = Literal["warn", "danger"]


class PolicyWarning(Frozen):
    """A config that is legal but probably not what the owner meant."""

    field: str
    reason: Reason
    severity: Severity


def _protect_blocks_on_reach(cond: ConditionSpec) -> ReachSpan | None:
    """Which span's shortfall would block this rule on every item, or ``None`` for neither.

    Reads the span off the field registry (``FieldSpec.reach_span``), so a field that gains
    or loses that bound moves this answer with it. There is no second, hand-maintained list
    to drift out of sync. An unknown field key returns ``None``: a rule that no longer
    validates cannot be blocking anything.

    Every place in this file that checks a field's span handles each span it knows about by
    name, with no catch-all branch, so a new span added to the registry cannot silently fall
    through unnoticed and get checked against the wrong one.
    ``tests.test_policy.TestEveryReachSpanIsRoutedByName`` fails if the set of spans changes
    without every one of those sites being updated.

    This must answer with the span itself, never a plain yes or no: the two spans need
    different world facts to decide whether the shortfall is actually live, and the caller
    has to tell them apart. ``watchers_all_time`` carries the item-lifetime span and is
    protect-only.

    The span alone does not decide whether the rule is blocked: ``fields._survives_more_history``
    also checks the operator, because a watcher count drawn from a mirror that does not reach
    far enough back is a lower bound, and only two of the four operator/outcome combinations
    can be overturned once more history arrives. Under ``gte``, every item either fires the
    protection or is blocked, so nothing can be condemned and it is accurate to say nothing
    will be flagged. Under ``lte`` it flips: an item already over the bar has an outcome more
    history cannot change, so it is reported as a plain checked abstain and stays
    condemnable. Reporting the list as empty there would be wrong, and telling the operator
    to remove the rule would strip a live protection from the items that are genuinely
    blocked.
    """
    spec = BY_KEY.get(cond.field)
    if spec is None or spec.reach_span is None or cond.op is not Op.GTE:
        return None
    return spec.reach_span


def inspect(
    body: PolicyBody,
    settings: ProfileSettings,
    *,
    requests_app_configured: bool = True,
    history_reach_days: float | None = None,
) -> list[PolicyWarning]:
    """The dangerous-config detector.

    Validation refuses what is provably wrong. This catches what is merely probably wrong.
    A validator cannot tell the two apart, because the values are legal either way.

    The archetype: an IMDb floor is stored in tenths, so ``75`` means 7.5. A user thinking
    in Rotten Tomatoes types ``96``, which is legal (it means 9.6) and protects almost
    nothing. No validator can tell that apart from someone who genuinely wants a 9.6 floor,
    so this says so, loudly, and shows the effect next to it.

    ``requests_app_configured`` is one thing a policy cannot know about itself: whether the
    operator has a request service like Seerr connected. It defaults to ``True``, meaning
    "assume they do, and stay quiet". A caller that cannot tell should not guess, because the
    warning it gates says a setting is doing nothing, and telling someone to connect a
    service they already have is worse than saying nothing.

    ``history_reach_days`` is the second such fact: how far back the watch history mirror
    goes (``dormancy.history_reach_days``, the same number ``services.snapshot.ScanContext``
    uses). Same posture, for the same reason: ``None`` means "could not tell, stay quiet",
    because guessing the window is too short would tell an operator their setup is broken
    when it is fine.
    """
    warnings: list[PolicyWarning] = []

    def warn(field: str, reason: Reason, severity: Severity = "warn") -> None:
        warnings.append(PolicyWarning(field=field, severity=severity, reason=reason))

    rating_on = any(g.gate is GateId.RATING_FLOOR and g.enabled for g in body.gates)
    if rating_on:
        if not body.keep_rating_rules:
            warn("keep_rating_rules", Reason("rating_no_sources"))
        for rule in body.keep_rating_rules:
            # Names the bar this warning is about, not the whole card, so the editor can
            # render it beside the exact control that fixes it. ``PolicyBody`` refuses two
            # rules on the same source, so the source alone identifies the row. The
            # empty-list warning above names the bare field instead, since there is no bar
            # yet for it to be about.
            bar = f"keep_rating_rules.{rule.source.value}.floor"
            params: dict[str, ReasonParam] = {"floor": rule.floor, "source": rule.source.value}
            if is_percentage_source(rule.source):
                # A percentage source read on the 0-10 scale is the usual mix-up: typing 8
                # meaning "80%" sets an 8% bar that keeps everything.
                if rule.floor <= 20:
                    warn(bar, Reason("rating_bar_percent", params))
            else:
                if rule.floor >= 90:
                    warn(bar, Reason("rating_bar_high", params))
                if rule.floor <= 20:
                    warn(bar, Reason("rating_bar_low", params))

    # The span every reader of a watcher count is measured against.
    # ``PolicyBody.popularity_window_days`` falls back to 365 days when the gate
    # is off or absent, and ``services.scan_runner.build_gates`` hands that same fallback to
    # every reader regardless of the switch. So the window below must come from here, never
    # from the gate row alone: otherwise an operator's own keep-outright rule could be blocked
    # library-wide against a year they never set and, with the gate off, cannot even see.
    window_days = body.popularity_window_days()
    popularity = next(
        (g for g in body.gates if g.gate is GateId.SERVER_POPULARITY and g.enabled), None
    )
    # Only an enabled gate can set a window this short. A disabled gate falls back to the
    # 365-day default, which never trips this check.
    very_short = popularity is not None and window_days < 30

    # A mirror shorter than the window being asked about cannot answer the question, so
    # ``gates.ServerPopularityGate.evaluate`` fails closed: a three-month count cannot say
    # who watched something in the last year. The reach is a property of the operator's
    # watch-history data, so the block holds for the whole library
    # for as long as the shortfall lasts.
    #
    # "Nothing will be flagged" is true only for the path that can keep a title, never
    # remove one. A blocked protection abstains every item, library-wide, until the
    # shortfall clears. Two things sit on that path: the built-in popularity gate, and an
    # operator's own keep-outright rule on a popularity-window field, and both read the
    # same window whether or not the gate itself is switched on.
    #
    # The softer keep-discount path gets its own warning further down: a graded keep takes
    # its full discount on a shortfall, for every item, and enough discount can floor every
    # score at zero just as surely as a blocked protection can.
    #
    # The path that can lead to deletion gets its own warning too, further down: a blocked
    # condemn rule adds no pressure but keeps its weight in the denominator, so it cannot
    # empty the list by itself, but it can drag enough items below the coverage floor to
    # abstain them.
    #
    # These fire as ``warn``: the outcome is that Reaper deletes nothing,
    # which is the safe direction. Every ``danger`` warning in this file marks a config that
    # removes more than intended.
    #
    # The shortfall reason must be nested as a ``Reason`` parameter, never written out here:
    # this way the why-panel and this warning quote the exact same sentence from
    # ``gates.history_shortfall``, with no second copy to drift out of sync.
    #
    # Guarded on ``reach_clears_dormancy`` below, which is true only when the block is what
    # is actually holding the list back. The dormancy gate protects anything younger than
    # its own threshold ahead of any other check, so while the watch history is shorter than
    # that threshold, every item is already kept on age alone and the popularity window
    # decides nothing. Without this guard the warning would fire for any operator whose
    # history is under the dormancy floor, over a remedy that could not move a single
    # verdict.
    dormancy_floor = next(
        (g for g in body.gates if g.gate is GateId.MIN_DORMANCY and g.enabled), None
    )
    reach_clears_dormancy = dormancy_floor is None or (
        history_reach_days is not None and history_reach_days >= dormancy_floor.threshold
    )
    # Kept as (condition, span) pairs rather than reduced to a set of spans, because the
    # message below has to name and count the specific rules that are blocked.
    blocking = [
        (c, span)
        for c in body.protect_conditions
        if (span := _protect_blocks_on_reach(c)) is not None
    ]
    # The dormancy floor is the root cause of this whole family of warnings, so it must earn
    # its own warning here, never merely silence the other five. Dormancy is clamped to the
    # watch-history mirror: an item's dormancy can never read as more dormant than the
    # mirror's own reach. So a dormancy floor set above the reach keeps the entire library on
    # age alone until the mirror catches up. On the shipped 1095-day floor, that is every
    # operator holding under three years of history, which is most new installs.
    #
    # This fires exactly when ``reach_clears_dormancy`` is false, the same condition that
    # silences the other five warnings in this family below. Without this warning, the page
    # would go quiet exactly where the deletion list is emptiest, with nothing telling the
    # operator why.
    if dormancy_floor is not None and history_reach_days is not None and not reach_clears_dormancy:
        floor_short = history_shortfall(
            Known(value=history_reach_days, source="tautulli"), float(dormancy_floor.threshold)
        )
        floor_params: dict[str, ReasonParam] = {"days": dormancy_floor.threshold}
        if floor_short is not None:
            floor_params["shortfall"] = floor_short
        warn(
            f"gates.{GateId.MIN_DORMANCY.value}.threshold",
            Reason("dormancy_beyond_history", floor_params),
        )

    window_blockers = [c for c, span in blocking if span is ReachSpan.POPULARITY_WINDOW]
    # Kept as a list, never reduced to a count yet: the branch below has to count these
    # rules to phrase the message in the singular or the plural correctly.
    lifetime_blockers = [c for c, span in blocking if span is ReachSpan.ITEM_LIFETIME]
    owner_protect_on_window = bool(window_blockers)
    # Computed once and read by both warnings below. The one that can only keep titles also
    # needs a reader on the window (``window_short`` below); the softer keep-discount warning
    # does not, because a graded keep on a windowed field is discounted whether the popularity
    # gate is on or off.
    window_short: Reason | None = None
    if history_reach_days is not None and reach_clears_dormancy:
        window_short = history_shortfall(
            Known(value=history_reach_days, source="tautulli"), float(window_days)
        )
    short = window_short if (popularity is not None or owner_protect_on_window) else None
    if short is not None:
        if popularity is not None:
            # The window's own control is visible on the page whenever the gate is on, so the
            # message may point at it directly.
            #
            # Unless the window is also too short in the other direction (below the
            # very-short floor), where "lower it" would fix this warning while making that
            # one worse. Both faults are real and their fixes genuinely conflict, so one
            # message must cover both, never stack two that contradict each other.
            # Shortening the window to fit the reach does clear this shortfall, but only by
            # making the window so short it counts almost nothing as watched. Waiting for the
            # mirror to grow is the only fix that does not trade one fault for the other,
            # which is why it is offered first.
            warn(
                f"gates.{GateId.SERVER_POPULARITY.value}.window_days",
                Reason(
                    "popularity_beyond_history",
                    {
                        "window_days": window_days,
                        "shortfall": short,
                        "remedy": "wait" if very_short else "lower",
                    },
                ),
            )
        else:
            # The gate is off, so the window falls back to 365 days and its control is not
            # even rendered. Naming it would point at a box that is not on the page, so this
            # message anchors on the rule that is actually blocked instead: it can be deleted
            # directly, and waiting for the mirror to grow always works too.
            #
            # The blocking rule is named and counted, because more than one rule can share
            # the same field: removing only one would leave the warning unchanged while
            # silently dropping a live protection. The single-rule case names the field; the
            # plural case does not list every blocking field individually.
            warn(
                "protect_conditions",
                Reason(
                    "popularity_rules_beyond_history",
                    {
                        "window_days": window_days,
                        "shortfall": short,
                        "rules": len(window_blockers),
                        "field": window_blockers[0].field,
                    },
                ),
            )

    # The other span. ``watchers_all_time`` is protect-only and carries the item-lifetime
    # span, so ``fields.evaluate`` blocks it for every item the mirror does not reach back to
    # the arrival of. Under ``gte``, a blocked protection abstains while a matched one keeps,
    # so none of those items can be condemned either.
    #
    # This claims only that the affected set is "everything added before the history
    # starts": the span here tracks each item's own age. ``inspect`` has only one reach
    # figure, never a list of arrival dates, so it cannot size that set. The message must
    # name the affected set, never claim an empty list, unlike the window warning above:
    # claiming "nothing will be flagged" would be wrong for a young library the mirror
    # already covers.
    #
    # The dormancy guard still applies for the same reason as above: under the floor, every
    # item is already kept on age alone, so this warning would be deciding nothing.
    #
    # Removing the rule is the only remedy offered. On this span the reach and the item's age
    # both advance one day at a time, so the shortfall holds for exactly as long as the item
    # is younger than the history horizon, and waiting never clears it.
    #
    # The count matters because more than one rule can carry this span, since the span comes
    # from the field itself, shared by every rule that uses it. Removing only one of several
    # such rules would leave the warning unchanged while a live protection silently
    # disappears. No field is named, unlike the window warning: every condition here is
    # already on the one lifetime-bound field, so naming it would repeat the same word
    # without narrowing anything.
    if lifetime_blockers and history_reach_days is not None and reach_clears_dormancy:
        warn(
            "protect_conditions",
            Reason("added_before_history", {"rules": len(lifetime_blockers)}),
        )

    # The path that can lead to deletion. A blocked condemn rule adds no pressure but keeps
    # its weight in the denominator, so it cannot empty the list through pressure alone. The
    # weight it leaves behind does lower both figures the final verdict checks: how much of
    # the evidence was readable, and the highest score any item can reach. That comparison
    # must be left to the real decision function, never repeated here, so there is one place
    # that decides where the threshold sits.
    #
    # Summed only over rules whose block is library-wide. The
    # built-in FEW_WATCHERS signal and a graded custom rule are withheld from every item the
    # mirror does not reach far enough back for, because neither has an outcome that survives
    # a truncated count. ``watchers_all_time`` cannot appear here at all: it is protect-only,
    # so the window span is the only one this lane ever sees.
    #
    # A boolean rule lowers only one of the two figures, which is why it is summed
    # separately. It can keep an item's outcome that a truncated count already settles, so
    # such an item stays evaluated and keeps the rule's weight in the coverage figure. But a
    # boolean rule is all-or-nothing, and under ``lte`` the outcome that gets blocked is a
    # match: an item over the bar earns nothing because the rule did not fire, and one under
    # it earns nothing because the rule was blocked. So the weight leaves every item's score
    # ceiling at once while staying in the coverage figure, and no item can reach a threshold
    # that needed it. Under ``gte`` a matched item does earn the weight, so the list is
    # genuinely not empty and counting it here would overstate how safe the setting is.
    # ``fields.can_add_pressure_under_a_shortfall`` makes that call.
    #
    # Where the remaining weight can still reach the threshold, the list is genuinely not
    # empty, so no "nothing will be flagged" claim applies. An ``lte`` rule in that case
    # abstains exactly the titles nobody watched recently, while the popular ones it was
    # never meant to flag are judged normally. The second warning below covers that case: it
    # must name the affected set, never claim an empty list, the same way the
    # item-lifetime warning above does, because ``inspect`` cannot size it from one reach
    # figure alone.
    withheld = 0
    never_earned = 0
    # Only used to phrase the second warning below in the singular or the plural. Rules are
    # not named individually there, for the same reason as above: more than one rule can
    # share a field, so naming just one would be misleading.
    never_earned_rules = 0
    # Kept apart from the totals so the warning below can tell whether the built-in signal
    # controls or the custom-rules controls contributed most, and point at the right editor
    # card. The built-in slider is the only reach-bounded signal, so this figure is entirely
    # its share.
    on_the_signals_card = 0
    if window_short is not None:
        for signal in body.signals:
            if signal.signal is SignalId.FEW_WATCHERS and signal.weight > 0:
                withheld += signal.weight
                on_the_signals_card += signal.weight
        for condemn in body.custom_condemn:
            condemn_spec = BY_KEY.get(condemn.field)
            if (
                condemn.weight <= 0
                or condemn_spec is None
                or condemn_spec.reach_span is not ReachSpan.POPULARITY_WINDOW
            ):
                continue
            if isinstance(condemn, GradedCondemnSpec):
                withheld += condemn.weight
            elif not can_add_pressure_under_a_shortfall(condemn.op):
                never_earned += condemn.weight
                never_earned_rules += 1
    if (withheld > 0 or never_earned > 0) and window_short is not None:
        # The best any item can do once that weight is gone. Every removal weight sums to
        # exactly 100 points, so a weight is already its own share of the total, and the two
        # figures below differ only by the boolean rules' weight, which stays evaluated.
        covered = MAX_SCORE - withheld
        ceiling = covered - never_earned
        # Each figure is a genuine upper bound taken on its own, and the verdict only gets
        # more permissive as either rises, so passing the best possible reading of each is
        # the most generous case this warning can test. It can only fire too late, never too
        # early.
        best_case = decide_verdict(
            protected=False,
            blocked=False,
            score=ceiling,
            coverage_bp=round(covered / MAX_SCORE * 10_000),
            condemn_at=body.condemn_at,
            coverage_floor_bp=body.coverage_floor_bp,
        )
        if best_case != "condemn":
            warn(
                "signals"
                if on_the_signals_card * 2 >= withheld + never_earned
                else "custom_condemn",
                Reason(
                    "watcher_points_beyond_history",
                    {
                        "points": withheld + never_earned,
                        "window_days": window_days,
                        "shortfall": window_short,
                        "ceiling": ceiling,
                    },
                ),
            )
        else:
            # The partial case: the list is not empty, but the titles missing from it are
            # exactly the ones the affected rule was written to find.
            #
            # ``covered`` above already counts the item that sits above the bar: under
            # ``lte``, an item already past the bar keeps a boolean rule's weight in the
            # coverage figure, since more history could not overturn that outcome anyway. An
            # item at or under the bar loses the weight from coverage too. Either item's
            # score ceiling is the same, since no item can earn that weight, so the two
            # differ only in coverage. That asymmetry is why such a policy returns a full
            # list of popular titles and none of the unwatched ones.
            #
            # Sized the same way as the warning above, never from a real distribution:
            # ``inspect`` has only one reach figure, never a list of watcher counts, so the
            # message must name the affected set, never count it. With no boolean rule,
            # ``held_covered`` equals ``covered``, so this reads ``condemn`` exactly as
            # ``best_case`` did above, and no warning fires here.
            held_covered = covered - never_earned
            held_case = decide_verdict(
                protected=False,
                blocked=False,
                score=ceiling,
                coverage_bp=round(held_covered / MAX_SCORE * 10_000),
                condemn_at=body.condemn_at,
                coverage_floor_bp=body.coverage_floor_bp,
            )
            if held_case != "condemn":
                warn(
                    "custom_condemn",
                    Reason(
                        "custom_rules_cannot_fire",
                        {
                            "rules": never_earned_rules,
                            "window_days": window_days,
                            "shortfall": window_short,
                        },
                    ),
                )

    # The softer keep-discount path. A graded keep takes its full discount on a shortfall,
    # on every item it reaches, with no way to earn a smaller outcome back. Since the final
    # score can never go below zero, a single keep worth more than the remaining headroom
    # above the threshold holds every affected item below it, as surely as a blocked
    # protection does, on a path the operator was told only ever lowers a score gently.
    #
    # Anchored on ``graded_keeps``, and it can name the specific rule, unlike the warnings
    # above: a graded keep carries a name the operator typed. Summed per span, never per
    # rule, because each blocked keep takes its full discount and the total is what
    # subtracts from the score: two keeps of 20 against a headroom of 30 empty the list
    # exactly as one keep of 40 does.
    #
    # The two spans are kept apart because they bound different things. A window shortfall
    # is a property of the operator's watch-history data, so a window keep's discount lands
    # on every item. A lifetime shortfall is a property of each item's own age, and
    # ``inspect`` has only one reach figure, never a list of arrival dates. So only window
    # keeps crossing the headroom on their own may claim an empty list; the combined case
    # names the affected set instead.
    headroom = MAX_SCORE - body.condemn_at
    window_keeps: list[GradedKeepSpec] = []
    lifetime_keeps: list[GradedKeepSpec] = []
    if history_reach_days is not None and reach_clears_dormancy:
        for keep in body.graded_keeps:
            keep_spec = BY_KEY.get(keep.field)
            if keep_spec is None or keep_spec.reach_span is None:
                continue
            # Matched member by member, for the same reason ``fields.reach_shortfall`` is: a
            # catch-all branch would file a future third span under the wrong bound and print
            # the wrong sentence about it.
            match keep_spec.reach_span:
                case ReachSpan.POPULARITY_WINDOW:
                    if window_short is not None:
                        window_keeps.append(keep)
                case ReachSpan.ITEM_LIFETIME:
                    lifetime_keeps.append(keep)
                case _:
                    assert_never(keep_spec.reach_span)

    windowed_total = sum(k.max_discount for k in window_keeps)
    combined_total = windowed_total + sum(k.max_discount for k in lifetime_keeps)
    contributors: list[GradedKeepSpec] = []
    scope: Literal["window", "combined"] | None = None
    total = 0
    if windowed_total > headroom:
        contributors, total, scope = window_keeps, windowed_total, "window"
    elif combined_total > headroom:
        contributors, total, scope = window_keeps + lifetime_keeps, combined_total, "combined"
    if contributors and scope is not None:
        # "Wait for the mirror to grow" is offered only when a window keep is one of the
        # contributors. A window shortfall clears as the watch-history mirror deepens, which
        # can drop those keeps out of the total and bring it back under the headroom. A
        # lifetime keep never clears this way: the reach and the item's age advance together,
        # so waiting moves nothing and only editing the rule can. A discount cannot be set to
        # zero, so at a headroom of zero the only remedy left is to remove the rule.
        move = ("wait_" if window_keeps else "") + ("remove" if headroom < 1 else "set")
        keep_params: dict[str, ReasonParam] = {
            "scope": scope,
            # Each name is the operator's own text, quoted through the catalog and joined by
            # the frontend composer, so no English joiner is built here.
            "names": tuple(Reason("rule_name", {"name": k.name}) for k in contributors),
            "total": total,
            "rules": len(contributors),
            "move": move,
            "headroom": headroom,
        }
        if scope == "window" and window_short is not None:
            keep_params["window_days"] = window_days
            keep_params["shortfall"] = window_short
        warn("graded_keeps", Reason("graded_keeps_beyond_history", keep_params))

    # Fires only when the earlier window warning did not already cover this control. That
    # warning already carries both faults together; stacking this one beside it would tell
    # the operator to raise and lower the same number in adjacent sentences.
    if very_short and short is None:
        warn(
            f"gates.{GateId.SERVER_POPULARITY.value}.window_days",
            Reason("popularity_window_short", {"window_days": window_days}),
        )

    # The season path's member of this same family. The mid-binge guard holds back a season
    # for any viewer whose last play falls inside ``in_progress_hold_days``. Where the watch
    # history mirror does not span that hold, an invisible viewer and one whose hold has
    # genuinely expired look identical, so the guard cannot tell them apart. It must then hold
    # every season on disk, never guess, and nothing is left for scoring to judge.
    #
    # This must be guarded on ``progress_is_establishable``, never on a shortfall directly,
    # because the two disagree at ``hold_days = 0``. Zero means "hold a place forever", which no
    # finite mirror can support, so the predicate always answers false there, while a plain
    # shortfall check would see a zero-day span as trivially covered and stay quiet. The
    # predicate decides whether to warn at all; the shortfall below only supplies the reason
    # why, for every hold length except zero.
    #
    # Computed once here because the warning below is guarded on the opposite of this same
    # condition, so both need to agree on one answer to "is the mid-binge guard holding the
    # whole disk".
    mid_binge_holds_everything = (
        body.media_type == "tv"
        and body.keep_in_progress
        and history_reach_days is not None
        and not progress_is_establishable(
            reach_days=int(history_reach_days), hold_days=body.in_progress_hold_days
        )
    )
    # ``history_reach_days`` is checked again here, separately from the flag above,
    # because the branch body below needs the real number to build a ``Known`` value from.
    if mid_binge_holds_everything and reach_clears_dormancy and history_reach_days is not None:
        hold_params: dict[str, ReasonParam] = {"hold_days": body.in_progress_hold_days}
        if body.in_progress_hold_days > 0:
            hold_short = history_shortfall(
                Known(value=history_reach_days, source="tautulli"),
                float(body.in_progress_hold_days),
            )
            if hold_short is not None:
                hold_params["shortfall"] = hold_short
        warn("in_progress_hold_days", Reason("in_progress_unreadable", hold_params))

    # The fifth member of this family. ``services.season_pruning._detect_conflicts`` compares
    # two all-time season watcher counts, so a season the mirror does not reach back to the
    # arrival of reports only a lower bound, which more history can always
    # raise. Every prunable season of such a show conflicts against every kept
    # season no matter what either count says, so automatic TV pruning stays inert on that
    # show until the mirror catches up.
    #
    # This must name the affected set of shows, never claim an empty list, for the same
    # reason the item-lifetime warning above does: the span here tracks each item's own age,
    # and this function has only one reach figure, never a list of arrival dates. Each
    # conflict is marked as a comparison Reaper could not make, and the show
    # waits for a manual look, where a hand review can still remove it.
    #
    # The dormancy guard applies for the same reason as the other four warnings in this
    # family, and the mid-binge hold silences it too: where that guard cannot be
    # established, every season is held on disk, nothing is prunable, and this warning is
    # never reached at all.
    #
    # No remedy is offered: on this span the reach and the item's age both advance together,
    # so the shortfall holds for exactly as long as the item is younger than the history
    # horizon, and waiting cannot move it.
    #
    # This also requires the policy's own keep rules to be able to produce a season to
    # compare against. The conflict check compares seasons kept by rule against seasons that
    # could be pruned, so a policy that keeps no season on age alone raises no conflict no
    # matter how short the mirror is. ``keep_last_seasons`` and ``keep_first_season`` are the
    # two rules that keep a season on age alone, so they are what this checks.
    if (
        body.media_type == "tv"
        and body.flag_keep_conflicts
        and (body.keep_last_seasons > 0 or body.keep_first_season)
        and history_reach_days is not None
        and reach_clears_dormancy
        and not mid_binge_holds_everything
    ):
        warn("flag_keep_conflicts", Reason("season_conflicts_before_history"))

    disabled = {g.gate for g in body.gates if not g.enabled}
    # Each warning below states only what turning that switch off actually changes. The
    # executor's own veto against removing a title mid-play is unconditional and does not
    # consult this gate at all, so turning the gate off cannot delete a file someone is
    # watching. What it does is let the title be condemned, listed, and approved, and then
    # skipped only at the last moment. The dormancy clamp in
    # fact derivation runs the same way regardless of this switch, so it still protects
    # against an unread dormancy reading either way.
    if GateId.STREAMING_NOW in disabled:
        warn(f"gates.{GateId.STREAMING_NOW.value}.enabled", Reason("streaming_check_off"), "danger")
    if GateId.DATA_HORIZON in disabled:
        warn(f"gates.{GateId.DATA_HORIZON.value}.enabled", Reason("horizon_off"), "danger")

    if body.condemn_at <= 30:
        warn("condemn_at", Reason("threshold_low", {"threshold": body.condemn_at}), "danger")

    if settings.max_unmeasured_per_run > 0:
        # Legal, and probably not what most operators mean, which is exactly what this
        # detector exists for. The size caps cannot cover these items at all, so saying so
        # is the one fact that makes the setting understandable.
        warn(
            "max_unmeasured_per_run",
            Reason("unmeasured_allowance", {"count": settings.max_unmeasured_per_run}),
        )

    for spec in body.custom_condemn:
        if spec.field == "size_bytes" and spec.weight > 0:
            warn(
                "custom_condemn",
                Reason("custom_rule_size", {"rule_name": spec.name}),
                "danger",
            )

    # The same danger through the built-in signal instead of a hand-written rule. Neither
    # shipped default enables it.
    size_signal = next(
        (s for s in body.signals if s.signal is SignalId.SIZE and s.weight > 0), None
    )
    if size_signal is not None:
        warn("signals", Reason("size_points", {"points": size_signal.weight}), "danger")

    # A rule written on a field this media type cannot read. Saving a condition checks the
    # field's lane, operator and value type, leaving whether the field applies to this
    # policy's media type to this warning instead. A rule saved before a field was narrowed
    # to one media type (a season, for instance, has no single release date and mixes
    # episode qualities) keeps
    # validating and simply stops appearing in the editor. Left unsaid, a protection would
    # read as "checked, did not fire" forever, and a removal rule is worse than inert: its
    # points still count toward the fixed 100-point total, so it holds down every score in
    # this policy.
    for anchor, kind, rules in (
        ("protect_conditions", "protection", [(c.field, "") for c in body.protect_conditions]),
        ("custom_condemn", "rule", [(c.field, c.name) for c in body.custom_condemn]),
        ("graded_keeps", "keep_rule", [(k.field, k.name) for k in body.graded_keeps]),
    ):
        for field_key, name in rules:
            field_spec = BY_KEY.get(field_key)
            if field_spec is None or body.media_type in field_spec.media_types:
                continue
            warn(
                anchor,
                Reason(
                    "field_unreadable_for_media",
                    {
                        "kind": kind,
                        "named": "yes" if name else "no",
                        "rule_name": name,
                        "field": field_key,
                        "media_type": body.media_type,
                    },
                ),
                "danger",
            )

    # Every removal weight sums to exactly 100 points, so a rule's declared weight always
    # equals the points it actually adds. There is nothing to warn about here.

    if body.media_type == "tv" and body.keep_last_seasons >= 10:
        warn(
            "keep_last_seasons",
            Reason("keep_last_too_many", {"keep_last": body.keep_last_seasons}),
        )

    # "Requested only" needs a connected request service like Seerr to tell a requested
    # show from an unrequested one. Without it, the check never gets a definite answer and
    # falls back to protecting every show, since an unknown answer counts as "might be
    # requested". That is the safe outcome, but an invisible one: the setting behaves more
    # broadly than it reads. Only worth saying while the floor is above zero seasons, since
    # at zero the scope decides nothing.
    if (
        body.media_type == "tv"
        and body.keep_last_scope == "requested"
        and body.keep_last_seasons > 0
        and not requests_app_configured
    ):
        warn(
            "keep_last_scope",
            Reason("keep_last_all_shows", {"keep_last": body.keep_last_seasons}),
        )

    total_keep = sum(k.max_discount for k in body.graded_keeps)
    if total_keep >= body.condemn_at:
        warn(
            "graded_keeps",
            Reason(
                "graded_keeps_exceed_threshold",
                {"points": total_keep, "threshold": body.condemn_at},
            ),
        )

    return warnings
