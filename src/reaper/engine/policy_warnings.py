# SPDX-License-Identifier: AGPL-3.0-or-later
"""The dangerous-config detector: what is legal but probably not what the owner meant.

``PolicyBody`` refuses what is provably wrong. ``inspect`` catches the rest, and a validator
cannot: an IMDb floor of ``96`` (meaning 9.6) is as legal as ``75``, and only one of them is
what someone typing a Rotten Tomatoes number intended. So every warning here says what the
setting will actually do and shows the blast radius beside it.

Split out of ``engine/policy.py``, which is the model and the hash over it. Nothing in a
warning reaches a verdict: ``inspect`` reads a body and returns prose, and no caller feeds
its result back into scoring. The dependency runs one way -- this module imports the model
from ``policy`` and ``policy`` imports nothing back -- so the pair cannot cycle whatever
either gains later.
"""

from __future__ import annotations

from typing import Literal, assert_never

from reaper.clock import humanize_days, humanize_window
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
    join_and,
)
from reaper.engine.signals import MAX_SCORE, SignalId
from reaper.engine.verdict import decide_verdict
from reaper.ratings import is_percentage_source, source_label

#: How loudly a warning reads. One declaration, because ``inspect``'s local ``warn`` defaults to
#: the quieter of the two and would otherwise re-spell the pair.
Severity = Literal["warn", "danger"]


class PolicyWarning(Frozen):
    """A config that is legal but probably not what the owner meant."""

    field: str
    message: str
    severity: Severity


def _protect_blocks_on_reach(cond: ConditionSpec) -> ReachSpan | None:
    """Which span's shortfall would block this rule on EVERY item, or ``None`` for neither.

    Two tests, and the second is the one that is easy to miss. The registry owns the span
    (``FieldSpec.reach_span``), so a FIELD that gains or loses that bound moves this with it
    rather than leaving a second list to drift (rule 103). An unknown key is ``None``: a rule
    that no longer validates cannot be blocking anything.

    That sentence used to be written as though it covered a new SPAN too, and it never did
    (rules 7/24, 103). The consumers hand-enumerate the members -- the two ``in
    protect_spans`` tests below, one per warning, and the lean loop's match -- so a third
    ``ReachSpan`` took the ``else`` and was scored against the wrong bound with nothing
    failing (issue #168). The two routing sites now match member by member and mypy holds
    them (``fields.reach_shortfall``, the lean loop). The membership tests cannot be closed
    that way, because each one carries copy written for its own span: a third member simply
    gets no warning, which is silence rather than a wrong answer. The condemn-lane sum is the
    fifth site and the exception to that: its totals are PRINTED, so a span it does not know
    about under-reports a rendered count instead of going quiet.
    ``tests.test_policy.TestEveryReachSpanIsRoutedByName`` is what fails when the set
    changes, and it names all five sites that have to grow a branch.

    It answers with the SPAN rather than a yes for one of them, because the two spans need
    different world-facts to decide whether the shortfall is live and the caller has to tell
    them apart (rule 140). The operator test below is span-agnostic -- ``_survives_more_history``
    reads only the op -- so scoping this to the popularity window was never the registry
    speaking, just the one lane somebody had reached for. ``watchers_all_time`` carries the
    other span and is PROTECT-only, so it was the one field this could not see and the only
    lane with no warning at all.

    But the span alone does not decide it -- ``fields._survives_more_history`` reads the
    OPERATOR, because a truncated watcher count is a lower bound and only two of the four
    outcomes can be overturned by history nobody has yet. Under ``gte`` every item is either
    a fired PROTECT or a block, so nothing is condemned and the caller's "nothing will be
    flagged" holds. Under ``lte`` it inverts: an item already OVER the bar has an outcome
    more history cannot change, so it comes back a plain checked ABSTAIN and stays
    condemnable. Claiming an empty list there would be false in the reassuring direction,
    and the remedy the caller offers -- remove the rule -- would drop a live protection off
    the items that ARE blocked (rules 7/24, 144).
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

    Validation refuses what is *provably* wrong. This catches what is merely
    *probably* wrong -- and a validator cannot tell the two apart, because the
    values are legal either way.

    The archetype: an IMDb floor is stored in tenths, so ``75`` means 7.5. A user
    thinking in Rotten Tomatoes types ``96``, which is legal (it means 9.6) and
    protects almost nothing. No validator can distinguish that from someone who
    genuinely wants a 9.6 floor. So we say so, loudly, and show the blast radius
    next to it rather than pretending to know.

    ``requests_app_configured`` is the one thing here a policy cannot know about
    itself: whether the operator has a Seerr connected. It defaults to True, meaning
    "assume they do, and stay quiet". A caller that cannot tell should not guess,
    because the only warning it gates says a setting is doing nothing -- and telling
    someone to connect a service they already have is worse than saying nothing.

    ``history_reach_days`` is the second such fact: how far back the watch mirror goes
    (``dormancy.history_reach_days`` off ``services.history_sync.horizon``, the one
    derivation ``services.snapshot.ScanContext`` uses for the number the gate reads).
    Same posture and same reason -- ``None`` means "could not tell, stay quiet", because
    a caller that guessed short would tell an operator their window is useless when it
    is fine.
    """
    warnings: list[PolicyWarning] = []

    def warn(field: str, message: str, severity: Severity = "warn") -> None:
        warnings.append(PolicyWarning(field=field, severity=severity, message=message))

    rating_on = any(g.gate is GateId.RATING_FLOOR and g.enabled for g in body.gates)
    if rating_on:
        if not body.keep_rating_rules:
            warn(
                "keep_rating_rules",
                "Keep well-rated titles is turned on, but it has no rating sources "
                "yet, so it is not keeping anything. Add a rating source to it, or "
                "turn the protection off.",
            )
        for rule in body.keep_rating_rules:
            label = source_label(rule.source)
            # Every warning below is about ONE bar's number, so it names that bar rather than
            # the card: the editor can then render it against the box that fixes it instead of
            # in a stack under the whole list, where reaching it meant browsing the page in
            # document order (#189). The source keys the row uniquely -- ``PolicyBody`` refuses
            # two rules on one source -- which is the same guarantee the gate id gives the
            # ``gates.{gate}.{setting}`` family this mirrors. The empty-list warning above keeps
            # the bare field, because there is no bar for it to be about.
            bar = f"keep_rating_rules.{rule.source.value}.floor"
            if is_percentage_source(rule.source):
                # A percentage source read on the 0-10 scale is the usual mix-up: typing 8
                # meaning "80%" sets an 8% bar that keeps everything.
                if rule.floor <= 20:
                    warn(
                        bar,
                        f"A bar of {rule.floor}% on {label} protects almost everything. "
                        "This field is a percentage: for 80% enter 80, not 8.",
                    )
            else:
                if rule.floor >= 90:
                    warn(
                        bar,
                        f"A bar of {rule.floor / 10:.1f} on {label} will protect almost "
                        "nothing: very few titles rate that highly.",
                    )
                if rule.floor <= 20:
                    warn(
                        bar,
                        f"A bar of {rule.floor / 10:.1f} on {label} protects essentially "
                        "everything. Did you mean 7.0?",
                    )

    # The span every reader of a watcher count is measured against -- NOT the enabled gate
    # row. ``PolicyBody.popularity_window_days`` falls back to 365 when the gate is off or
    # absent, and ``services.scan_runner.build_gates`` hands that fallback to
    # ``CustomProtectGate`` regardless of the switch, so one bound governs every reader
    # (rule 140). Reading the row here scoped both warnings below to one of those readers
    # and left an operator's own keep-outright rule blocking library-wide against a year
    # they never set and, with the gate off, cannot even see.
    window_days = body.popularity_window_days()
    popularity = next(
        (g for g in body.gates if g.gate is GateId.SERVER_POPULARITY and g.enabled), None
    )
    # Only an enabled gate can carry a window this short: the fallback the disabled case
    # resolves to is the 365-day default, which never trips it.
    very_short = popularity is not None and window_days < 30

    # The same window in the other direction, and the reason this detector needed a second
    # world-fact at all. ``gates.ServerPopularityGate.evaluate`` fails closed when the mirror
    # is shorter than the window it is being asked about: a count over three months cannot
    # answer "who watched this in the last year", so the gate blocks. The reach is a property
    # of the operator's DATA rather than of any one title, so it blocks library-wide for as
    # long as the shortfall lasts.
    #
    # Most blocks clear on the next scan (an unreachable Seerr, an unread session list, a
    # missing id), which is why no surface was ever obliged to name a remedy for one. The
    # ones that do not are all the same family, a mirror shallower than the question, and the
    # other members are held on the season path: ``season_scan``'s lifetime-shortfall
    # conflict and ``gates.progress_is_establishable``.
    #
    # This used to read "the member with a control the operator can turn, so the editor is
    # where it has to be said", which quietly justified saying nothing about the other two.
    # It was false of the mid-binge hold from the day that guard shipped:
    # ``in_progress_hold_days`` is a control on this same editor, one card down, and a hold
    # the mirror cannot span holds every season on disk (issue #154). That branch is at the
    # foot of this function now, and so is the lifetime-shortfall conflict, the fifth member
    # (issue #224). This comment used to close by declaring that one deliberately unwarned,
    # "the one member with no control behind it": ``flag_keep_conflicts`` is a switch on this
    # same editor, so the premise was false as written, and the branch that now speaks for it
    # explains what is true instead (rules 7/24, 72).
    #
    # WHO blocks on this window, which is what makes "nothing will be flagged" true. This
    # detector claims it for the PROTECT lane only: a blocked protect ABSTAINs every item
    # (``verdict.decide_verdict``), library-wide, for as long as the shortfall lasts. Two
    # readers sit in that lane and the built-in gate is only one of them -- an operator's
    # own keep-outright rule on a popularity-window field is the other, and ``build_gates``
    # hands it this same span whether the gate is on or off.
    #
    # **The lean lane is warned about too now, further down**, and this comment carried it as
    # a known gap for a while: a graded keep takes its FULL ``max_discount`` on a shortfall,
    # for every item (``signals.evaluate_keep``), and ``score()`` floors at zero under a
    # bounded base, so a keep worth more than ``MAX_SCORE - condemn_at`` empties the list just
    # as provably as a blocked protect does. The ``graded_keeps`` warning at the end of this
    # function is still not that warning: it fires on ``total_keep >= condemn_at``, a much
    # higher bar, and says nothing about the mirror. ``PolicyRuleEditors``' ``leanFields`` is
    # not gate-filtered, so the field is offered as a lean whichever way the switch is set,
    # which is why the lean check does not read the gate either (rules 7/24, 140).
    #
    # This branch remains about the PROTECT lane only. The three lanes and what each does under
    # a shortfall, since the asymmetry is why there are separate checks: a protect blocks and
    # abstains, a lean takes its full discount, and a condemn rule withholds its pressure
    # while keeping its weight in the denominator.
    #
    # That last one lowers scores without blocking anything, so it cannot empty the list
    # through PRESSURE -- but it can through COVERAGE, and **that lane is warned about now
    # too, further down** (issue #164 closed). This paragraph used to rule it safe and was
    # wrong to (rule 7/24): a blocked signal is unevaluated, so its weight leaves the
    # numerator and stays in the denominator, and enough weight on reach-bounded fields drops
    # coverage under ``coverage_floor_bp`` for every item at once.
    #
    # ``warn``, not ``danger``: the outcome is that Reaper deletes nothing, which is the
    # keep direction. Every ``danger`` here marks a config that removes MORE.
    #
    # The cause clause comes from ``gates.history_shortfall`` rather than being restated,
    # because the why-panel prints that same sentence off the same helper for the same
    # operator (rule 144), and it already decides when the gap is too small to name a number.
    #
    # ONLY where the block is what is actually holding the list back, which is what
    # ``reach_clears_dormancy`` tests. ``MinDormancyGate`` PROTECTs anything younger than its
    # threshold, ``verdict.decide_verdict`` puts PROTECT ahead of blocked, and dormancy is
    # clamped to the mirror (``dormancy.reference_instant`` measures from the horizon at the
    # earliest). So while the reach is under the floor, every item is kept on age alone and
    # the popularity window decides nothing. Without this test the warning fires on both
    # shipped policies -- floor 1095, window 365 -- for every operator holding under a year
    # of history, and the remedy it names cannot move a single verdict.
    dormancy_floor = next(
        (g for g in body.gates if g.gate is GateId.MIN_DORMANCY and g.enabled), None
    )
    reach_clears_dormancy = dormancy_floor is None or (
        history_reach_days is not None and history_reach_days >= dormancy_floor.threshold
    )
    # Kept as pairs rather than reduced straight to a set: the gate-off message below has to
    # name the rules doing the blocking and count them, and a set of spans cannot say which
    # conditions produced it (issue #157).
    blocking = [
        (c, span)
        for c in body.protect_conditions
        if (span := _protect_blocks_on_reach(c)) is not None
    ]
    # The floor itself, which is the ROOT of this family rather than another member of it, and
    # had no warning at all (issue #217). Dormancy is clamped to the mirror --
    # ``dormancy.reference_instant`` measures from ``last_played``, else ``max(added_at,
    # horizon)``, else nothing at all, and both measurable arms are at most the reach -- so the
    # most dormant any item can read IS the reach.
    # ``MinDormancyGate`` PROTECTs anything under its threshold and PROTECT beats everything in
    # ``decide_verdict``, so a floor above the reach keeps the entire library on age alone until
    # the mirror catches up. On the shipped 1095-day floor that is every operator holding under
    # three years of history, which is most new installs.
    #
    # It has to be said HERE because ``reach_clears_dormancy`` is read five times below to
    # SILENCE the other warnings in this family, each correctly: under the floor their remedies
    # would move no verdict. The aggregate was a page that went quietest exactly where the list
    # was emptiest, with nothing speaking for the condition that silenced everything. This
    # branch is that voice, and it cannot stack with the five, because it fires on precisely
    # the negation they are guarded on.
    if dormancy_floor is not None and history_reach_days is not None and not reach_clears_dormancy:
        floor_short = history_shortfall(
            Known(value=history_reach_days, source="tautulli"), float(dormancy_floor.threshold)
        )
        warn(
            f"gates.{GateId.MIN_DORMANCY.value}.threshold",
            "Nothing will be flagged for removal. Reaper waits "
            f"{humanize_days(dormancy_floor.threshold)} of no watching before "
            f"anything can go, and {floor_short}, so it can't yet show a title sitting "
            "untouched that long. Wait for it to build up, or lower this wait.",
        )

    window_blockers = [c for c, span in blocking if span is ReachSpan.POPULARITY_WINDOW]
    # Kept as a list for the same reason its window twin is (rule 72): the branch below has to
    # COUNT these, and a set of spans cannot say how many conditions produced it. It read
    # ``ReachSpan.ITEM_LIFETIME in protect_spans`` and printed a singular sentence for any
    # number of them, which is issue #157 surviving on the sibling lane.
    lifetime_blockers = [c for c, span in blocking if span is ReachSpan.ITEM_LIFETIME]
    owner_protect_on_window = bool(window_blockers)
    # Derived once and read by both lanes below (rule 104). The protect lane additionally
    # requires a reader on the window, which is what ``short`` adds; the lean lane does not,
    # because a graded keep on a window field is discounted whether the gate is on or off.
    window_short: str | None = None
    if history_reach_days is not None and reach_clears_dormancy:
        window_short = history_shortfall(
            Known(value=history_reach_days, source="tautulli"), float(window_days)
        )
    short = window_short if (popularity is not None or owner_protect_on_window) else None
    if short is not None:
        window_text = humanize_window(window_days)
        if popularity is not None:
            # The window control is on the page while the gate is on (``PolicyEditor``'s
            # ``GateRow`` renders it under ``gate.enabled``), so the remedy may name it.
            #
            # Except when the window is ALSO under the short-window floor, where "lower it"
            # is advice in the direction the other warning is pushing back on. Both faults
            # are real and their remedies genuinely oppose, so one message carries the pair
            # rather than two stacking on one control and cancelling out. Shortening to the
            # reach DOES clear the shortfall, it just buys the other fault to do it -- an
            # even shorter window counts almost nothing as watched. Waiting is the only move
            # that clears one without deepening the other, which is why it leads.
            remedy = (
                "Wait for it to build up: a shorter window would leave almost nothing "
                "counted as watched."
                if very_short
                else "Lower this window to match your history, or wait for it to build up."
            )
            field = f"gates.{GateId.SERVER_POPULARITY.value}.window_days"
            # The window is named BEFORE the cause clause, because ``history_shortfall``'s
            # in-margin arm is "your watch history does not go back that far" and "that far"
            # needs the span to have been said already.
            message = (
                "Nothing will be flagged for removal. Reaper can't say who watched a title "
                f"in the last {window_text} from a shorter history, and {short}. {remedy}"
            )
        else:
            # The gate is off, so the window is the 365-day fallback and its control is not
            # rendered at all. Anchoring on it would name a box that is not on the page, so
            # this rides with the rule that is actually blocking, where both remedies are in
            # reach: the rule can be deleted right there, and waiting always works. Naming
            # the protection they would have to switch back on to expose the window is
            # deliberately NOT done -- its label lives in ``frontend`` (``policyMeta.ts``)
            # and a second spelling here would drift from it (rule 144).
            #
            # The rule is NAMED, and counted, because neither was safe to leave implicit
            # (issue #157). Two rules on the same field are constructible -- ``addHard``
            # appends unconditionally and ``PolicyBody`` validates the pair -- so a singular
            # "remove that rule" was factually wrong there: removing one leaves the warning
            # byte-identical while a live protection is gone, with nothing saying the pick
            # was wrong. And "counts who watched a title" is not a discriminator when the
            # card beside it reads "People who have ever watched it", which also counts who
            # watched a title; the only thing telling them apart was the window, which is
            # unrendered here by this branch's own premise.
            #
            # The label comes from the registry the editor renders from -- backend-owned,
            # served verbatim through ``GET /api/vocabulary`` to ``describeCondition`` -- so
            # this names the string already on the operator's screen rather than a second
            # spelling of it (rule 144). Distinct labels joined, so a span that ever gains a
            # second field does not silently name one of them.
            field = "protect_conditions"
            labels = join_and(
                list(dict.fromkeys(f'"{BY_KEY[c.field].label}"' for c in window_blockers))
            )
            many = len(window_blockers) > 1
            subject = f"Your {len(window_blockers)} keep rules" if many else "Your keep rule"
            counts = "count" if many else "counts"
            message = (
                f"Nothing will be flagged for removal. {subject} on {labels} {counts} the "
                f"last {window_text}, and {short}. Wait for it to build up, or remove "
                f"{'them' if many else 'that rule'}."
            )
        warn(field, message)

    # The OTHER span, and the reader that had no warning at all. ``watchers_all_time`` is
    # PROTECT-only and carries ``ITEM_LIFETIME``, so ``fields.evaluate`` blocks it through
    # ``gates.lifetime_shortfall`` for every item the mirror does not reach back to the arrival
    # of -- and under ``gte`` a blocked protect abstains while a matched one keeps, so not one
    # of those items can be condemned.
    #
    # What is NOT claimed here is the whole library. The span this one needs is the ITEM's age,
    # not a policy setting, so the affected set is "everything added before the history starts"
    # and ``inspect`` cannot size it: it is handed one reach, never a list of arrival dates. So
    # the message names the set instead of asserting an empty list, which is the difference
    # between this and the window branch above. Saying "nothing will be flagged" would be false
    # in the reassuring direction for a young library the mirror covers outright (rules 7/24).
    #
    # The dormancy guard still applies for the same reason it does above -- under the floor
    # every item is kept on age alone, so this rule is deciding nothing and its remedy would
    # move no verdict.
    #
    # Removing the rule is the ONLY remedy, and the "wait for it to build up" this used to lead
    # with was false for the same reason it would be on the season branch at the foot of this
    # function (rule 72): on an ``ITEM_LIFETIME`` span the reach and the item's age both advance
    # one day per day, so the shortfall holds exactly while ``added_at < horizon`` and no amount
    # of waiting moves it. It survived because it reads as harmless boilerplate beside a remedy
    # that does work, which is precisely how an operator ends up taking the half that never
    # resolves.
    #
    # And it is COUNTED, for the reason the window branch above is (issue #157, rule 72): two
    # rules on this span are constructible -- one field carries it, ``ITEM_LIFETIME`` sits on
    # the spec and not on the value, and ``PolicyBody`` validates the pair -- so a singular
    # "remove that rule" was factually wrong for any number above one. Removing one leaves this
    # sentence byte-identical while a live protection is gone, with nothing saying the pick was
    # wrong. No label is joined the way the window branch joins its own: every condition here is
    # on the one field, so the label would be the same word repeated and discriminates nothing.
    # The titles are named "those" in the remedy rather than "them", which now has two plural
    # nouns in front of it to refer back to.
    if lifetime_blockers and history_reach_days is not None and reach_clears_dormancy:
        many = len(lifetime_blockers) > 1
        subject = f"Your {len(lifetime_blockers)} keep rules" if many else "Your keep rule"
        warn(
            "protect_conditions",
            "Titles added before your watch history starts won't be flagged for "
            f"removal. {subject} {'count' if many else 'counts'} everyone who has ever "
            "watched a title, and Reaper can't count plays from before your history "
            "begins, so it holds those titles instead of guessing. Remove "
            f"{'those rules' if many else 'that rule'} if you want those titles judged.",
        )

    # The CONDEMN lane, the third of the four and the one the comment above used to rule out.
    # A blocked condemn rule withholds its pressure and keeps its weight in the denominator
    # (``signals.score``), so it cannot empty the list through pressure. The weight it leaves
    # behind lowers BOTH bounds ``decide_verdict`` reads, though: coverage, which is what
    # issue #164 measured, and the score ceiling with it -- ``signals``' "``condemn_at`` is
    # itself a coverage floor" note. So the question is put to the real decision function
    # rather than answered here, which covers both bounds and keeps the floor comparison in
    # the one place allowed to make it (rule 3/22).
    #
    # Summed over the readers whose block is LIBRARY-WIDE, which is not every reader of the
    # field. Driven at a 90-day reach against the 365-day fallback, coverage per item:
    #
    #     built-in FEW_WATCHERS:   0.45 at 0 watchers, 0.45 at 50   -- always withheld
    #     a graded custom rule:    0.45 at 0 watchers, 0.45 at 50   -- always withheld
    #     a boolean custom rule:   0.45 at 0 watchers, 0.80 at 50   -- per item
    #
    # The built-in withholds on every observation it can take: a Known count fails the reach
    # check, an Absent one fails it too (rule 93's precondition is a GENUINE absence, which a
    # window the mirror does not span cannot establish), and an Unknown has no number to ramp.
    # The graded arm exempts an Absent input, which ``distinct_watchers`` never is -- every
    # builder writes Known or Unknown, none of them Absent -- so it too is withheld for every
    # item. ``watchers_all_time`` cannot appear on either: it is PROTECT-only, so
    # ``ITEM_LIFETIME`` never reaches the condemn lane and the window is the only span here.
    #
    # A BOOLEAN rule lowers ONE of the two bounds, which is why it is summed separately
    # rather than either counted with the rest or left out. It goes through
    # ``fields.evaluate``, which keeps ``_survives_more_history``'s earned outcomes, so an
    # item the truncated count already settles comes back EVALUATED and keeps its weight in
    # coverage -- the 0.80 row above. But a boolean rule is all-or-nothing, and under ``lte``
    # the outcome that gets blocked is the MATCH: an item over the bar earns nothing because
    # the rule did not fire, and one under it earns nothing because the rule was blocked. So
    # the weight leaves the score for every item at once while coverage keeps it, and no item
    # can reach a threshold that needs it. Under ``gte`` the reverse holds and a matched item
    # does earn the weight, so the list is genuinely not empty and counting it would be false
    # in the reassuring direction (rule 144). ``fields.can_add_pressure_under_a_shortfall``
    # is that discrimination, asked rather than restated (rule 104).
    #
    # Where the remaining weight CAN still reach the threshold the list is genuinely not
    # empty, so no "nothing will be flagged" claim is available -- and an ``lte`` rule is
    # then abstaining exactly the titles nobody watched recently while the popular ones it
    # was never meant to flag are judged normally. That is the second tier below (issue
    # #215): it names the set, as the ``ITEM_LIFETIME`` branch does, because ``inspect``
    # cannot size it from one reach.
    withheld = 0
    never_earned = 0
    # Only for the plural of the second tier below, which names no rule and so needs nothing
    # but the count. Rules are not named there for the reason the window branch gives above:
    # two rules on one field are constructible, so a singular reads as a wrong instruction.
    never_earned_rules = 0
    # Kept apart from the totals so the anchor below can weigh the two cards against each
    # other. The built-in slider is the only reach-bounded signal, so this IS the signals
    # card's share; everything else in the totals comes from the custom-rules card.
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
    if withheld > 0 or never_earned > 0:
        # The best any item can do once that weight is gone. The denominator is pinned at
        # ``MAX_SCORE`` (``_weights_total_one_hundred``), so a weight IS its share, and the
        # two bounds differ only by the boolean weight that stays evaluated.
        covered = MAX_SCORE - withheld
        ceiling = covered - never_earned
        # Each is a genuine upper bound on its own, and ``decide_verdict`` is monotone in
        # both, so passing the best of each independently is the most permissive reading
        # available. The warning can therefore only fire late, never falsely.
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
                f"Nothing will be flagged for removal. {withheld + never_earned} of "
                f"your {MAX_SCORE} removal points count who watched a title in the last "
                f"{humanize_window(window_days)}, and {window_short}, so only "
                f"{ceiling} points are left to judge on. Wait for it to build up, or "
                "move those points to a reason that doesn't count watchers.",
            )
        else:
            # The PARTIAL case, the other half of issue #215: the list is not empty, and the
            # titles missing from it are exactly the ones the rule was written to find.
            #
            # ``covered`` above is the item ABOVE the bar. Under ``lte`` that item keeps the
            # boolean weight in coverage -- ``_survives_more_history`` blocks only the outcome
            # more history could overturn, and a count already past the bar can only rise --
            # while the item AT OR UNDER it is blocked and loses the weight from coverage too.
            # Its score ceiling is ``ceiling`` either way, since no item can earn that weight
            # at all, so the two differ in coverage alone. That is the whole asymmetry, and it
            # is why one policy hands back a full list of popular titles and none of the
            # unwatched ones.
            #
            # Sized the same way as the branch above rather than from a distribution:
            # ``inspect`` is handed one reach and never a list of watcher counts, so the set is
            # NAMED, as the ``ITEM_LIFETIME`` branch names its own. With no boolean rule
            # ``held_covered`` is ``covered`` and this reads ``condemn`` exactly as
            # ``best_case`` did, so the guard is the arithmetic and not a second condition.
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
                many = never_earned_rules > 1
                warn(
                    "custom_condemn",
                    f"Your removal {'rules' if many else 'rule'} won't flag the titles "
                    f"{'they were' if many else 'it was'} written to find. "
                    f"{'They count' if many else 'It counts'} who watched a title in "
                    f"the last {humanize_window(window_days)}, and {window_short}, so "
                    "Reaper holds those titles back instead of guessing. Wait for it "
                    "to build up, or remove "
                    f"{'those rules' if many else 'that rule'}.",
                )

    # The lean lane, which the comment above used to name as a known gap. A graded keep takes
    # its FULL ``max_discount`` on a shortfall, on every item it reaches, with no
    # ``_survives_more_history`` test to earn an outcome back (``signals.evaluate_keep``) -- and
    # ``score()`` is ``max(0, base - keep_discount)`` over a base bounded by ``MAX_SCORE``. So a
    # single keep worth more than the headroom holds every affected item under the threshold as
    # provably as a blocked protect does, and it does it on a lane the operator was told was
    # safe.
    #
    # The existing ``graded_keeps`` warning is not this one and does not cover it: it fires on
    # the keeps TOTALLING at least ``condemn_at``, a much higher bar (70 against 31 on shipped
    # values), and says nothing about the mirror. A keep at 40 sits in that dead zone.
    #
    # Anchored on ``graded_keeps`` beside the rule doing it, and it can name the rule, which
    # the protect lanes above cannot: a ``GradedKeepSpec`` carries a name the operator typed.
    # Summed per span, never per rule. ``evaluate_keep`` grants each blocked keep its full
    # ``max_discount`` and ``score()`` subtracts the SUM, so two keeps of 20 against a headroom
    # of 30 empty the list exactly as one keep of 40 does. Testing each rule alone left that
    # case silent, which is the same dead zone this warning exists to close, one arity up.
    #
    # The two spans are kept apart because they bound different things. A window shortfall is a
    # property of the operator's DATA, so a window keep's discount lands on every item; a
    # lifetime shortfall is a property of each ITEM's age, and ``inspect`` is handed one reach
    # and never a list of arrival dates. So window keeps alone crossing the headroom is the only
    # case that may claim an empty list, and the combined case names the affected set instead
    # (rule 144's reassuring-direction failure, which is why the wider claim is not made here).
    headroom = MAX_SCORE - body.condemn_at
    window_keeps: list[GradedKeepSpec] = []
    lifetime_keeps: list[GradedKeepSpec] = []
    if history_reach_days is not None and reach_clears_dormancy:
        for keep in body.graded_keeps:
            keep_spec = BY_KEY.get(keep.field)
            if keep_spec is None or keep_spec.reach_span is None:
                continue
            # Matched member by member, ``fields.reach_shortfall``'s twin and for the same
            # reason: the ``else`` here filed any third span under lifetime, which would
            # print the "plays from before your history begins" copy about a span that is
            # not the one blocking them, and score it against the wrong bound (issue #168).
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
    scope = cause = ""
    total = 0
    if windowed_total > headroom:
        contributors, total = window_keeps, windowed_total
        scope = "Nothing will be flagged for removal."
        cause = (
            f"Reaper can't say who watched a title in the last "
            f"{humanize_window(window_days)}, and {window_short}, so "
        )
    elif combined_total > headroom:
        contributors, total = window_keeps + lifetime_keeps, combined_total
        scope = "Titles added before your watch history starts won't be flagged for removal."
    if contributors:
        named = join_and([f'"{k.name}"' for k in contributors])
        many = len(contributors) > 1
        rule_phrase = f"your keep rules {named} take" if many else f"your keep rule {named} takes"
        theirs = "their" if many else "its"
        # ``max_discount`` is ``ge=1``, so at ``condemn_at`` 100 the headroom is 0 and every
        # settable value is too high. Naming a number there sends the operator to a control
        # that refuses it, so the remedy drops to the one move that still works.
        if headroom < 1:
            remedy = f"remove {'those rules' if many else 'that rule'}"
        else:
            unit = "point" if headroom == 1 else "points"
            what = "their total" if many else "it"
            remedy = f"set {what} to {headroom} {unit} or less"
        # The cause clause leads only on the window branch; without it the sentence starts on
        # "your", so it is capitalized here rather than carried as a second literal that could
        # drift out of step with the one above it.
        said = (
            f"{cause}{rule_phrase} all {total} of {theirs} points off "
            f"{'every title' if cause else 'them'}."
        )
        if not cause:
            said = said[:1].upper() + said[1:]
        # "Wait for it to build up" is offered only where a WINDOW keep is one of the
        # contributors, which is the rule-72 sweep of the two branches that lead on the same
        # "added before your watch history starts" sentence. A window shortfall clears as the
        # mirror deepens, and clearing it drops those keeps out of ``window_keeps`` entirely,
        # which is what can bring the total back under the headroom. An ``ITEM_LIFETIME`` keep
        # never leaves this list: the reach and the item's age advance together, so waiting moves
        # nothing and only the remedy below can. Where the contributors are lifetime keeps alone,
        # the remedy leads and is capitalized for it.
        move = f"Wait for it to build up, or {remedy}." if window_keeps else f"{remedy}."
        if not window_keeps:
            move = move[:1].upper() + move[1:]
        warn("graded_keeps", f"{scope} {said} {move}")

    # Only where the shortfall is NOT already speaking for this control: it carries the pair
    # itself in that case, and stacking both told the operator to raise and to lower the same
    # number in adjacent sentences.
    if very_short and short is None:
        # "A {n}-day window" would read "A 8-day" at 8, 11 and 18, all reachable under 30.
        # The article governs "watch window" instead, so no value can disagree with it (#338).
        window_text = f"{window_days} day" if window_days == 1 else f"{window_days} days"
        warn(
            f"gates.{GateId.SERVER_POPULARITY.value}.window_days",
            f"A watch window of {window_text} is very short: almost nothing gets "
            "watched inside it, so the few-recent-watchers pressure applies to nearly "
            "your whole library. A year is the usual setting.",
        )

    # The season path's member of the same family, one field down the same editor card, and
    # the fourth of the five lanes a shallow mirror empties (rule 72, issue #154). The mid-binge
    # guard holds a viewer whose last play falls inside ``in_progress_hold_days``; where the
    # mirror does not span that hold, an invisible viewer and an expired one are the same
    # viewer, so the set is UN-ESTABLISHABLE rather than empty and ``plan_series_prune`` holds
    # every season on disk. Nothing is left for the scoring lane to judge, and until now the
    # page said nothing at all: before this warning, ``in_progress_hold_days`` appeared in the
    # policy layer only as a field declaration (``policy.PolicyBody``).
    #
    # The journey this closes is the one that reads as Reaper being broken. An operator on a
    # short mirror gets the popularity-window warning, follows it, lowers the window to match
    # their history, and clears it -- and the list is still empty, now with no warning on the
    # page at all, because the one surface that ever named their history reach was the warning
    # they just cleared.
    #
    # Guarded on ``progress_is_establishable`` rather than on a shortfall, because the two
    # disagree at ``hold_days = 0``: that means "hold a place forever", which no finite mirror
    # can support and the predicate answers False at any reach, while
    # ``history_shortfall(reach, 0.0)`` sees a span of zero days, finds it covered, and returns
    # None. So the predicate decides WHETHER to speak and the shortfall supplies the cause
    # clause only, and the zero arm needs its own cause. Asking the predicate is also what
    # keeps one derivation of "does the mirror span the hold" (rule 104); it moved to
    # ``engine.gates`` beside its two siblings so this could ask it without an engine module
    # importing a service.
    #
    # Hoisted, because the fifth member below is guarded on its negation and the two must read
    # one derivation of "is the mid-binge guard holding the whole disk" rather than two copies
    # of the predicate call (rule 104).
    mid_binge_holds_everything = (
        body.media_type == "tv"
        and body.keep_in_progress
        and history_reach_days is not None
        and not progress_is_establishable(
            reach_days=int(history_reach_days), hold_days=body.in_progress_hold_days
        )
    )
    # The reach is re-tested rather than left to the flag: narrowing does not survive being
    # folded into a bool, and the branch body builds a ``Known`` off it.
    if mid_binge_holds_everything and reach_clears_dormancy and history_reach_days is not None:
        if body.in_progress_hold_days <= 0:
            # No number to compare a reach against, so no shortfall sentence exists to
            # borrow. The editor's own help text under this control already says a 0 keeps
            # every season; this is the same fact at the moment it is true (rule 144).
            cause = (
                "At 0 days a viewer's place is held forever, and no watch history reaches "
                "back far enough to check that"
            )
            remedy = "Set a number of days, or turn this protection off."
        else:
            hold_short = history_shortfall(
                Known(value=history_reach_days, source="tautulli"),
                float(body.in_progress_hold_days),
            )
            # The hold is named BEFORE the cause clause, for the reason the window branch
            # names its span first: the in-margin arm is "does not go back that far".
            # ``humanize_days`` for the same reason as the dormancy branch above: "for year
            # after they last watched" has no article to carry the dropped "1" (rule 21).
            cause = (
                "Reaper holds a viewer's place for "
                f"{humanize_days(body.in_progress_hold_days)} after they last watched, and "
                f"{hold_short}"
            )
            remedy = "Wait for it to build up, or lower this to match your history."
        warn(
            "in_progress_hold_days",
            f"No TV season will be flagged for removal. {cause}, so it can't tell "
            f"who is partway through a show and holds every season. {remedy}",
        )

    # The FIFTH member of the family, and the one that was deferred rather than written
    # (issue #224). ``services.season_pruning._detect_conflicts`` compares two ALL-TIME season
    # watcher counts, so a season the mirror does not reach back to the arrival of reports a
    # lower bound, and more history can always lift a lower bound above anything. Every
    # prunable season of such a show therefore conflicts against every kept season whatever
    # either count says, ``auto_approvable`` goes False, and automatic TV pruning is inert on
    # that show until the mirror catches up.
    #
    # Driven on the shipped TV policy at a 1200-day reach -- deep enough that all four warnings
    # above and the #217 floor warning are correctly silent -- against a show 2000 days old:
    # ``inspect`` returned NO warning at all while every prunable season came back held. That
    # is the silent lane, and it is why this is a branch rather than a reworded deferral.
    #
    # The deferral it replaces said this was "the one member with no control behind it ... no
    # setting the operator can reach". That was wrong on its own terms: ``flag_keep_conflicts``
    # is a switch on this same editor ("Ask me first when a removal looks unusual"), and off
    # means the keep rule is simply followed. What is true is narrower and is why no remedy
    # naming that switch appears below: turning it off is the DELETE-MORE direction, on two
    # numbers Reaper knows are wrong, so this family will not recommend it. The switch carries
    # that option in its own help text, and this warning renders beside it, which is as close
    # to offering it as the prime directive allows.
    #
    # It names the AFFECTED SET rather than claiming an empty list, exactly as the
    # ``watchers_all_time`` branch above does and for the identical reason: the span this turns
    # on is each item's age, and ``inspect`` is handed one reach and never a list of arrival
    # dates. "Nothing will be flagged" would be false in the reassuring direction for a library
    # the mirror covers outright (rules 7/24). It also says where those shows go, because they
    # are not lost: every conflict carries ``shortfall``, so ``season_evidence.guard_result`` marks
    # each as a comparison Reaper did not make and the show waits in "Needs a look", where a
    # hand reap still condemns it. That is the string already on the operator's screen, from
    # the switch's own help text one row up (rule 144).
    #
    # The dormancy guard applies for the reason it does on all four above: under the floor
    # every item is kept on age alone, this decides nothing, and the #217 branch is the voice
    # that speaks there instead. The two cannot stack.
    #
    # It is silenced by the MID-BINGE hold as well, which is rule 143's shape rather than a
    # tidiness choice. Where that guard cannot be established ``plan_series_prune`` holds every
    # season ON DISK, so ``prunable`` is empty, ``_detect_conflicts`` iterates nothing, and this
    # lane is never reached at all -- naming it there would assert a cause that is not operative
    # while the branch above already claims the strictly stronger "no TV season will be flagged".
    # Two "wait for it to build up" sentences in adjacent paragraphs is also exactly the stacking
    # #134 removed from this family.
    #
    # It carries NO remedy, and the "wait for it to build up" every other member of this family
    # ends on would be a false one here. Those four turn on a FIXED span -- a popularity window,
    # a hold in days, the dormancy floor -- so a deepening mirror does eventually cover it. This
    # one turns on each item's own AGE, and both sides advance one day per day:
    # ``history_reach_days`` is ``(now - horizon).days`` and the item's age is
    # ``(now - added_at).days``, so the shortfall holds exactly while ``added_at < horizon``, a
    # comparison of two fixed instants that waiting cannot move. Telling the operator to wait
    # would be telling them to do nothing forever (rules 7/24, 21). The sentence instead ends on
    # where those shows go, which is true and is the only move that exists.
    #
    # The keep rule must also be able to PRODUCE a comparison partner, which is the difference
    # between a conflict and nothing at all. ``_detect_conflicts`` iterates ``prunable`` against
    # ``kept_seasons``, and that list drops specials, so a policy keeping no season on age alone
    # leaves it empty, the inner loop never runs, and no conflict is raised however short the
    # mirror is. Claiming the hold there would be false in the reassuring direction (rules 7/24):
    # the operator would read that old shows wait for them while every season of those shows is
    # condemnable on score. ``keep_last_seasons`` and ``keep_first_season`` are the two rules
    # that protect on age alone, so they are what this asks. An incomplete or airing season can
    # still supply a partner under a policy holding neither, but both are transient per-item
    # states, and this function is handed one reach and never a season list -- the same reason
    # the message below names the affected set rather than claiming an empty one.
    if (
        body.media_type == "tv"
        and body.flag_keep_conflicts
        and (body.keep_last_seasons > 0 or body.keep_first_season)
        and history_reach_days is not None
        and reach_clears_dormancy
        and not mid_binge_holds_everything
    ):
        warn(
            "flag_keep_conflicts",
            "Seasons of shows added before your watch history starts won't be removed "
            "automatically. Reaper compares how many people watched each season, and "
            "it can't count plays from before your history begins, so it marks those "
            'shows "Needs a look" instead of guessing.',
        )

    disabled = {g.gate for g in body.gates if not g.enabled}
    # Each of these states the consequence THIS switch has, verified against the code that
    # would deliver it (rules 7/24 and 25). Both used to name a consequence that cannot
    # occur, because the outcome each described is delivered somewhere the switch does not
    # reach:
    #
    # * The active-stream veto lives in the executor and is unconditional -- ``_reap_one``
    #   calls ``_being_watched_now`` on every real send without ever consulting the policy
    #   gate, and ``execute`` refuses a real run outright when Plex is missing. So turning
    #   the gate off cannot delete a file mid-play. What it does do is let the title be
    #   condemned, listed, and approved, and then skipped at the last moment.
    # * The horizon defense is the dormancy CLAMP in fact derivation
    #   (``services.snapshot.build_facts``, ``max(added_at, horizon)``), which runs whatever
    #   this switch says. ``DataHorizonGate`` can never PROTECT -- its own docstring says so,
    #   and ``evaluate`` has only a blocked branch and an abstain -- and its one independent
    #   job is failing closed on an Unknown dormancy, which ``MinDormancyGate`` also does.
    for gate, why in (
        (
            GateId.STREAMING_NOW,
            "A title someone is watching still reaches your reap list. Reaper skips it at "
            "the last moment, so a run removes less than it showed you.",
        ),
        (
            GateId.DATA_HORIZON,
            "Reaper drops one of the two checks that keep a title whose unwatched time it "
            "could not read.",
        ),
    ):
        if gate in disabled:
            warn(f"gates.{gate.value}.enabled", why, severity="danger")

    if body.condemn_at <= 30:
        warn(
            "condemn_at",
            f"A threshold of {body.condemn_at} condemns almost everything the "
            "protections do not save. Check the simulator's counts and review "
            "the flagged list carefully before arming this.",
            severity="danger",
        )

    if settings.max_unmeasured_per_run > 0:
        # Legal, and probably not what most operators mean: exactly what this detector is
        # for. The GB caps genuinely cannot cover these items, so saying so is not a
        # scare, it is the one fact that makes the setting understandable.
        warn(
            "max_unmeasured_per_run",
            f"Reaper will delete up to {settings.max_unmeasured_per_run} items it "
            "can't measure. The GB caps won't cover them.",
        )

    for spec in body.custom_condemn:
        if spec.field == "size_bytes" and spec.weight > 0:
            warn(
                "custom_condemn",
                f'Your rule "{spec.name}" removes things for being large. File size '
                "measures how much space you reclaim, not whether anyone wants the "
                "title, and big files are usually the popular 4K ones. Review what "
                "this rule flags over a few scans before arming it.",
                severity="danger",
            )

    # The same footgun through the built-in signal, which had no warning at all while the
    # hand-written equivalent above got a danger one -- and the SignalId.SIZE docstring
    # claimed the UI warned about it (rule 24). Neither shipped default enables it.
    size_signal = next(
        (s for s in body.signals if s.signal is SignalId.SIZE and s.weight > 0), None
    )
    if size_signal is not None:
        warn(
            "signals",
            f'"Large files" is adding {size_signal.weight} points toward removal. File '
            "size measures how much space you reclaim, not whether anyone wants the "
            "title, and big files are usually the popular 4K ones. Review what it "
            "flags over a few scans before arming it.",
            severity="danger",
        )

    # A rule written on a field this media type cannot read. `Condition.validate_for`
    # checks the lane, the operator and the type, but NOT the media type, so a rule saved
    # before a field was narrowed (``release_age`` and ``quality`` are movie-only: a season
    # has no single release date and mixes episode qualities) keeps validating and simply
    # stops being offered in the editor. Left unsaid, a protection reads as "checked, did
    # not fire" forever, and a removal rule is worse than inert -- its points still count
    # toward the fixed 100-point denominator, so it holds down every score in this policy.
    for anchor, kind, rules in (
        ("protect_conditions", "protection", [(c.field, "") for c in body.protect_conditions]),
        ("custom_condemn", "rule", [(c.field, c.name) for c in body.custom_condemn]),
        ("graded_keeps", "keep rule", [(k.field, k.name) for k in body.graded_keeps]),
    ):
        for field_key, name in rules:
            field_spec = BY_KEY.get(field_key)
            if field_spec is None or body.media_type in field_spec.media_types:
                continue
            called = f' "{name}"' if name else ""
            where = "seasons" if body.media_type == "tv" else "movies"
            warn(
                anchor,
                f"Your {kind}{called} uses {field_spec.label}, which Reaper cannot read "
                f"for {where}, so it never does anything. Remove it here, and set it "
                "on your other policy instead.",
                severity="danger",
            )

    # There was a dilution warning here, telling an owner that a rule written as 20 was
    # really adding about 14. `PolicyBody._weights_total_one_hundred` makes that state
    # unrepresentable: a body whose weights do not total exactly 100 no longer validates,
    # so a weight and the points it adds can never disagree. A warning for a condition
    # that cannot occur is worse than no warning, so it is gone rather than reworded.

    if body.media_type == "tv" and body.keep_last_seasons >= 10:
        warn(
            "keep_last_seasons",
            f"Keeping the last {body.keep_last_seasons} seasons protects every season of "
            "most shows, so TV pruning is effectively off: most series have fewer "
            "seasons than this.",
        )

    # "Requested only" needs Seerr to tell a requested show from an unrequested one.
    # Without it, season_scan._keep_last_applies never sees a Known answer, so it falls
    # back to protecting (Unknown counts as "might be requested") and the floor covers
    # the whole library. That is the safe outcome, and an invisible one: the setting
    # reads as narrower than it behaves. Only worth saying while the floor is on --
    # at 0 seasons the scope decides nothing.
    if (
        body.media_type == "tv"
        and body.keep_last_scope == "requested"
        and body.keep_last_seasons > 0
        and not requests_app_configured
    ):
        warn(
            "keep_last_scope",
            f"Reaper is keeping the last {body.keep_last_seasons} seasons of every "
            "show, not just requested ones: telling them apart needs Seerr, and no "
            'Seerr service is connected. Connect one, or switch this to "All shows" '
            "so the setting says what actually happens.",
        )

    total_keep = sum(k.max_discount for k in body.graded_keeps)
    if total_keep >= body.condemn_at:
        warn(
            "graded_keeps",
            f"Your keep rules can subtract up to {total_keep} points, at or above your "
            f"remove threshold of {body.condemn_at}. Together they could keep almost "
            "everything. Check the simulator still shows items to remove.",
        )

    return warnings
