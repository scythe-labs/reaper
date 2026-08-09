# SPDX-License-Identifier: AGPL-3.0-or-later
"""Trying a policy rule against one value, without a scan.

The policy editor draws a probe under each signal's range: move it and the sentence says
what a title at that value would earn. Answering that in the browser would mean a second
copy of the ramp living beside the control an operator tunes deletions with -- free to
drift from the engine, and confident while wrong (rule 3/22). So the question comes here
and is answered by the real ``evaluate_signal``, on the same code path a scan runs.

**Built to take more than one question, and the extension point is the type rather than a
registry.** A probe is a ``kind`` on the wire, a member on ``api.schemas.PolicyProbeIn``, a
function here that answers it by calling the production evaluator, and an arm on the route's
``match``. Adding "what would this keep rule discount" or "what would this graded rule of my
own add" costs those four and nothing else: no new route, no new response shape, and no
change to what an existing client sends. The kind is carried as a typed discriminator rather
than inferred from which fields happened to arrive, which is what rule 142 is about, and it
is far cheaper to type now than to retrofit onto a wire format that shipped without it.

A dict of kind-to-callable was written here and removed: the kinds take different arguments,
so nothing could dispatch through it, and a registry nothing reads is scaffolding that
cannot fire (rule 38/117). ``match`` on the discriminator gives the same extension point, and
the route's ``assert_never`` arm is what makes mypy check the arms are exhaustive: a bare
``match`` does not, and the forgotten arm would have reached the operator as a 500 off an
unbound name rather than as a refusal.

Only ``signal`` exists today, because a probe ships with the surface that asks it.

**This is a preview of the RULE, not of any item.** Every fact a signal reads is ``Unknown``
except the one under the probe, so nothing else can move the answer. The fields only the
custom-rule vocabulary reads are left at ``Facts``' own default, which is ``Absent`` -- no
signal reads them, and a keep probe would set its own field ``Known`` before asking. Whoever
adds that arm sets ``days_since_added`` the way ``history_reach_days`` is set below, or a
lifetime shortfall hands back the full discount at every value and teaches nothing about the
ramp. The reach check that guards
watcher counts is satisfied rather than exercised: a mirror too short for the window
withholds ``FEW_WATCHERS`` entirely (rule 140), which would report zero at every value and
teach the operator nothing about the shape of the rule they are setting. That shortfall is
already the subject of its own policy warning, which is where it belongs -- beside the
history it is about, not inside a ramp preview.
"""

from __future__ import annotations

from dataclasses import dataclass

from reaper.engine.gates import Facts
from reaper.engine.observation import Known, Unknown
from reaper.engine.signals import SignalConfig, SignalId, evaluate_signal

#: The one fact each built-in signal reads. Mirrors ``SignalId``, and
#: ``tests/test_signal_preview.py`` fails when the two disagree: a signal missing here has
#: no preview, and would reach the route as a ``KeyError`` rather than as a refusal
#: (rule 103).
READS: dict[SignalId, str] = {
    SignalId.UNWATCHED: "days_observed_unwatched",
    SignalId.FEW_WATCHERS: "distinct_watchers",
    SignalId.SEASON_RANK: "season_rank",
    SignalId.LOW_RATING: "imdb_rating_tenths",
    SignalId.SIZE: "size_bytes",
}

#: Named rather than typed at the site, so the copy walk can see it: an `Unknown` reason can
#: reach the why panel verbatim, and one written inline is invisible to every drift test.
#: This one never should reach it -- a probe builds no candidate and writes no explanation --
#: but "should not" is not a mechanism, and the guard is the mechanism.
NOT_PROBED_REASON = "not part of this preview"

_NOTHING = Unknown(source="preview", reason=NOT_PROBED_REASON)

#: Long enough that the watch mirror never withholds a watcher count here. A century, above any
#: history anyone has. See the module docstring: the probe is about the ramp, and the shortfall
#: has its own warning elsewhere. ``history_shortfall`` returns ``None`` at ``reach >= needed``,
#: so this only has to out-reach the window the engine is handed.
#:
#: It used to be half of a pair, with a ``MAX_PROBE_WINDOW_DAYS`` the wire read as the ceiling on
#: a ``window_days`` a caller could send. The request field is gone, so the window is now the
#: engine's own default and there is one number here rather than two held together by a rule.
_REACH_DAYS = 36_500.0


def _bare_facts(field: str, value: float) -> Facts:
    """Every fact a signal reads is Unknown but one, so only the probed value can move the
    answer. The rest keep ``Facts``' default; the module docstring says what that costs."""
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
    }
    observations[field] = Known(value=value, source="preview")
    return Facts(**observations)  # type: ignore[arg-type]


@dataclass(frozen=True, slots=True)
class ProbeResult:
    """One answer, in the shape every probe kind returns.

    ``points`` is what the rule moves the score by in its own direction -- pressure for a
    signal, and a discount for a keep rule when one is added -- so the editor renders any
    kind the same way. It is the only field: the engine's own wording for the answer used to
    ride beside it and no surface ever rendered it, because ``signalRamp.ts`` words both the
    editor's sentence and the panel's row, and a second wording over the wire would be a
    third copy rather than the thing that reconciles them.
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
