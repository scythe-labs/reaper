# SPDX-License-Identifier: AGPL-3.0-or-later
"""Try a policy rule against one value, without running a scan.

The policy editor draws a probe under each signal's range. Moving it shows what a title at
that value would earn. Answering that in the browser would need a second copy of the
scoring curve, living beside the control an operator tunes deletions with, free to drift
from the real engine and confident while wrong. So the question comes here instead, and is
answered by the real ``evaluate_signal``, on the same code path a scan runs.

Built to take more than one kind of question. A probe carries a ``kind`` on the wire, a
member on ``api.schemas.PolicyProbeIn``, a function here that answers it by calling the
production evaluator, and an arm on the route's ``match``. Adding a new kind, such as "what
would this keep rule discount," costs those four things and nothing else: no new route, no
new response shape, and no change to what an existing client sends. The kind is a typed
value on the wire rather than something inferred from which fields happened to arrive,
which is cheaper to build in now than to retrofit later.

The route's ``assert_never`` arm makes mypy check that every kind is handled. A plain
``match`` with no such arm would let a forgotten kind reach the operator as a server error
instead of a refusal.

Only ``signal`` exists today, because a probe ships with the surface that asks for it.

This previews the RULE, not any real item. Every fact a signal reads is ``Unknown`` except
the one under the probe, so nothing else can move the answer. Fields that only the
custom-rule vocabulary reads are left at ``Facts``' own default, ``Absent``, since no
built-in signal reads them. A future probe for a keep rule would need to set its own field
to ``Known`` before asking, the way ``history_reach_days`` is set below for watcher counts:
``days_since_added`` would need the same treatment, or a lifetime shortfall would show the
full discount at every value and teach nothing about the actual curve. The check that
guards watcher counts against a short watch history is satisfied here rather than
exercised: a history too short for the window would otherwise withhold the watcher signal
entirely, reporting zero at every value and teaching the operator nothing about the shape
of the rule they are setting. That shortfall already has its own policy warning, which is
where it belongs, next to the history it is about, not inside a preview of a rule's shape.
"""

from __future__ import annotations

from dataclasses import dataclass

from reaper.engine.gates import Facts
from reaper.engine.observation import Known, Unknown
from reaper.engine.signals import SignalConfig, SignalId, evaluate_signal

#: The one fact each built-in signal reads. Mirrors ``SignalId``. A signal missing from this
#: map has no preview and would otherwise reach the route as a ``KeyError`` instead of a
#: refusal, so ``tests/test_signal_preview.py`` fails when the two disagree.
READS: dict[SignalId, str] = {
    SignalId.UNWATCHED: "days_observed_unwatched",
    SignalId.FEW_WATCHERS: "distinct_watchers",
    SignalId.SEASON_RANK: "season_rank",
    SignalId.LOW_RATING: "imdb_rating_tenths",
    SignalId.SIZE: "size_bytes",
}

#: Named as a constant rather than typed inline, so a tool that scans for reason strings can
#: see it. An ``Unknown`` reason can reach the why-panel as raw text, and one written inline
#: would be invisible to that scan. This reason should never actually reach a panel, since a
#: probe builds no candidate and writes no explanation, but that expectation alone is not a
#: guarantee, so it stays a named constant just in case.
NOT_PROBED_REASON = "not_probed"

_NOTHING = Unknown(source="preview", reason=NOT_PROBED_REASON)

#: Long enough that the watch history never withholds a watcher count here: a century, more
#: than any real history reaches. See the module docstring for why: the probe is about the
#: shape of the rule, and a short history has its own warning elsewhere.
#: ``history_shortfall`` returns ``None`` once ``reach >= needed``, so this only has to
#: out-reach the window the engine is given.
_REACH_DAYS = 36_500.0


def _bare_facts(field: str, value: float) -> Facts:
    """Every fact a signal reads is Unknown, except the one field under the probe, so only
    the probed value can move the answer. The rest keep ``Facts``' own default; the module
    docstring explains what that costs."""
    observations: dict[str, object] = {
        "title": "preview",
        "days_observed_unwatched": _NOTHING,
        "distinct_watchers": _NOTHING,
        "distinct_watchers_all_time": _NOTHING,
        "size_bytes": _NOTHING,
        "imdb_rating_tenths": _NOTHING,
        "imdb_votes": _NOTHING,
        "season_rank": _NOTHING,
        "is_streaming_now": _NOTHING,
        "is_managed": _NOTHING,
        "in_curated_list": _NOTHING,
        "is_whitelisted": _NOTHING,
        "history_reach_days": Known(value=_REACH_DAYS, source="preview"),
        "rewatch_viewings": _NOTHING,
        "rewatch_last_play_days": _NOTHING,
        "rewatch_cohort_n": _NOTHING,
        "rewatch_cohort_k": _NOTHING,
    }
    observations[field] = Known(value=value, source="preview")
    return Facts(**observations)  # type: ignore[arg-type]


@dataclass(frozen=True, slots=True)
class ProbeResult:
    """One answer, in the shape every probe kind returns.

    ``points`` is how much the rule moves the score, in its own direction: pressure toward
    deletion for a signal, or a discount for a keep rule once one exists. This lets the
    editor render any kind of probe the same way. It is the only field: ``signalRamp.ts``
    already writes both the editor's sentence and the panel's row from the number alone,
    so a separate wording sent over the wire would be a third copy instead of the thing
    that keeps the other two in sync.
    """

    points: float


class UnprobableSignalError(LookupError):
    """A signal with no fact mapped to it, so there is nothing to try a value against."""


def probe_signal(config: SignalConfig, value: float) -> ProbeResult:
    """What a title at ``value`` would earn from this signal, in ``[0, weight]``.

    Raises ``UnprobableSignalError`` for an id missing from ``READS``, which the route turns into
    a refusal rather than a 500: an id arriving here unmapped is a signal someone added
    without deciding what it reads, and guessing a fact for it would answer confidently
    about the wrong evidence.
    """
    field = READS.get(config.signal)
    if field is None:
        raise UnprobableSignalError(config.signal)
    result = evaluate_signal(config, _bare_facts(field, value))
    return ProbeResult(points=result.pressure)
