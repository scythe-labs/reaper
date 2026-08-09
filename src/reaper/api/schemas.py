# SPDX-License-Identifier: AGPL-3.0-or-later
"""Wire formats.

Route return types must be **resolvable at runtime**. ``from __future__
import annotations`` turns them into strings, and FastAPI builds a response model by
resolving them -- so a type imported only under ``TYPE_CHECKING`` yields a 500 at
request time rather than an error at import time. There is a test that walks every
route and forces its response model to resolve.
"""

from __future__ import annotations

import enum
from datetime import datetime
from typing import Any, Literal
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field, field_validator

from reaper.engine.explanation import (
    Explanation,
    GateOutcomeOut,
    KeepContributionOut,
    MatchOut,
    SignalContribution,
    thaw_threshold,
)
from reaper.engine.fields import FieldType, Lane, Op
from reaper.engine.gates import POLICY_AUTHORABLE_GATES, GateId
from reaper.engine.policy import CustomCondemnSpec, GradedKeepSpec, RatingRuleSpec
from reaper.engine.policy_migrations import PolicyRepair
from reaper.engine.signals import SignalId
from reaper.engine.verdict import Override

# The why-panel document moved to ``engine.explanation`` so the reap path could run the same
# validation the panel does, which is the whole of #142; it may not import this layer. The
# names stay bound here because they are wire formats and half the tree refers to them as
# ``api.schemas.X`` -- and because the OpenAPI components are named off the classes, so
# nothing about the published document moves with them.
__all__ = [
    "Explanation",
    "GateOutcomeOut",
    "KeepContributionOut",
    "MatchOut",
    "SignalContribution",
    "thaw_threshold",
]


class OkOut(BaseModel):
    """A route that either did the thing or raised. ``ok`` is always ``True``.

    It carries no information and is not meant to: the routes below answer with a status code
    and a refusal body when they refuse, so a caller reads the code. What this exists for is
    the published document. A route annotated ``dict[str, bool]`` is typed there as a free-form
    map with no title, which tells a script author to expect keys nobody sends."""

    ok: bool


class RemovedOut(BaseModel):
    """Whether a record existed to remove. ``False`` is a normal answer, not a failure."""

    removed: bool


class RestoreCancelOut(BaseModel):
    """Clearing a staged restore. Its own model rather than :class:`OkOut`, because it answers
    a second question and a shared model would drop the answer off the wire."""

    ok: bool
    cleared: bool = Field(
        description=(
            "Whether a staging was actually removed. An ownership refusal and a call that "
            "found nothing both report false, and neither is an error."
        )
    )


class JobRunOut(BaseModel):
    """A maintenance job accepted for a run. ``status`` is always ``started``: the job is
    handed to the scheduler and this returns before it finishes, so this is an
    acknowledgement rather than a result."""

    status: str
    job: str


class CandidateLinkOut(BaseModel):
    """One Plex row an abstain could not choose between, with the ways to open it.

    ``rating_key`` is carried so the panel can label the entries apart without inventing
    names for rows it knows nothing else about. Either link may be ``None`` when the
    operator has not connected that app."""

    rating_key: int
    plex: str | None = None
    tautulli: str | None = None


class LinksOut(BaseModel):
    """Where this item can be opened. Each link is ``None`` when it cannot be built
    (unmatched in Plex, instance removed, a row scanned before the coordinates were
    captured) -- the UI hides a missing link, never renders a broken one. At most one
    of ``radarr``/``sonarr`` is set. The rating-site links back the chips in the
    ratings row; Rotten Tomatoes is a title search (no integration provides RT's
    hand-curated slugs)."""

    plex: str | None = None
    tautulli: str | None = None
    seerr: str | None = None
    radarr: str | None = None
    sonarr: str | None = None
    imdb: str | None = None
    tmdb: str | None = None
    rotten_tomatoes: str | None = None
    trakt: str | None = None
    match_candidates: list[CandidateLinkOut] = []
    """One entry per Plex row an abstain was choosing between, empty on every other row.

    ``plex``/``tautulli`` above are built from the item's own rating key and are therefore
    null for exactly these items, which left the operator reading "we couldn't tell which
    one this is" with nothing to open. These are the rows in question."""


class RatingsOut(BaseModel):
    """The external-ratings row. ``imdb`` is the same dataset number the scoring signal
    used (never a second source); the percentage fields are 0-100 ints. ``tmdb`` and
    ``trakt`` are 0-10 scores stored in tenths, shown as the percentages both sites
    themselves display."""

    imdb: float | None = None
    imdb_votes: int | None = None
    rt_critic: int | None = None
    rt_audience: int | None = None
    tmdb: int | None = None
    trakt: int | None = None


class ChipOut(BaseModel):
    """The one short status chip a card wears, display-ready.

    ``tone`` picks the color, ``text`` is the whole chip. Derived server-side from the
    stored explanation (never a re-decision): a Sanctuary card's chip names the protection
    that fired, a Limbo card's names what stopped Reaper short. Condemned cards carry no
    chip here -- their amber dormancy pill is built from ``dormant_for``."""

    tone: Literal["kept", "quiet", "look"]
    """``kept`` renders green (a protection fired), ``quiet`` gray (nothing to act on),
    ``look`` amber-outlined (deliberately left for the owner to decide)."""

    text: str

    why: str | None = None
    """The same fact as ``text``, worded as a lowercase clause that can follow
    "Reap requested, kept for now:" -- or None when this chip names no reason a reap
    would be refused (an item under the threshold is reaped on request, not held).

    It ships beside ``text`` because the frontend must never parse ``text`` to recover
    it: the clause is ours to word, and a reworded chip would silently drop every
    held-reap explanation back to the generic fallback (H-1)."""


class GroupSeasonMarkOut(BaseModel):
    """One square of a show card's season strip: the lightest possible per-season mark.

    ``season`` is None for a row whose media_key did not carry a season number -- the
    strip shows it unnumbered rather than dropping it (display extraction never errors
    a row off the queue)."""

    id: int
    """The candidate id for this season, so clicking its square opens that season's own
    reasoning (not the whole show's panel)."""
    season: int | None = None
    verdict: str
    override: str | None = None
    override_effective: bool | None = None
    """For a ``"reap"`` override: whether the engine honors it (True paints the square
    solid red), or refuses it for a safety stop, or for a row Reaper cannot identify -- a
    bad Plex match, or an explanation it could not read (False keeps the square in its scan
    color). None when there is no reap override."""
    size_bytes: int | None = None
    """The season's size on disk, so the card can state whole-show totals without a
    second fetch. None when nothing would report one, which is not zero: the strip still
    shows the square, and the show's totals leave it out and say so."""
    spare_expires_at: str | None = None
    """For a ``"spare"`` override: when it stops keeping this season, ISO-8601, or None for
    a forever spare. The strip square colors by the item's fate (rule 49), and a spare whose
    clock has PASSED is a fate of its own -- still keeping the file until a scan realizes the
    expiry, but no longer a live decision, so it wears the dashed green rather than the solid
    one. None when there is no spare override."""
    spare_covers_until: str | None = None
    """When the LAST spare covering this season stops keeping it, ISO-8601, or None for a
    forever one. What the square's COLOR reads, where ``spare_expires_at`` above is the spare a
    control on the row toggles. They differ when both levels spare a season and the season's own
    spare runs out first: kept for as long as the show's says, not expired. See
    ``CandidateOut.spare_covers_until``. None when there is no spare override."""


class CandidateOut(BaseModel):
    id: int
    media_key: str
    title: str
    media_type: str
    size_bytes: int | None
    """What deleting this would free. None when Reaper could not measure it, which is
    never zero: the UI says "Size unknown" rather than showing an empty item, and the
    item is held back from any plan."""

    verdict: str
    score: int
    coverage_bp: int
    first_flagged_at: str | None = None
    # Display fields captured at scan time. None of them change the verdict; they are
    # what the review queue draws around it.
    year: int | None = None
    summary: str | None = None
    poster_url: str | None = None
    requested_by: str | None = None
    group_key: str | None = None
    group_title: str | None = None
    group_condemned_count: int | None = None
    """How many seasons "Reap now" on this show group would actually plan: its actable
    seasons across the WHOLE snapshot (condemned minus hand-spares, plus effective hand
    reaps) -- not just the fetched pages, which on a long sorted list can hold only some
    of a show's seasons. None for movies."""
    group_condemned_bytes: int | None = None
    """The byte total over that same set. The show card must show the number the planner
    will act on, never a partial page sum."""
    group_unknown_size: int | None = None
    """How many of the show's actable seasons have no size. They are excluded from both
    numbers above, because the planner will not plan them, so the card can say what it is
    leaving out rather than appearing to shrink. None for movies, and hidden at zero."""
    video_resolution: str | None = None
    """Canonical file resolution ("2160", "1080", ..., "sd") for the card's quality
    badge. None hides the badge (TV seasons, unmatched items, pre-rescan rows)."""
    library: str | None = None
    """The Plex library (section) this item lives in, as the operator named it -- the
    show's for a season row. Powers the card/panel library chip and the library filter.
    None when unknown (unmatched, or a row from before this shipped); the chip is hidden."""
    dormant_for: str | None = None
    """How long the item has sat unwatched, as the humanized span from the dormancy
    signal ("5 years, 9 months") -- the card's amber pill. None when the signal could
    not be evaluated, and the pill is hidden."""
    reason: str | None = None
    """The one-line "why", drawn from the explanation: the protection that keeps a spared
    item, or the top reason a reaped one scored. It is what the card shows in place of a plot
    synopsis -- on the review queue you want to know why Reaper judged it, not what it is."""
    override: str | None = None
    """The owner's manual decision *in effect* on this item -- ``"spare"``, ``"reap"``, or
    ``None``. Set the moment they click, so the card can show the pending intent before the
    next scan bakes it into the stored verdict. This is the EFFECTIVE decision: a season with
    no decision of its own inherits its show's. It colors the row's chip and score, which show
    the item's real fate. To decide what a control can toggle, read ``override_own``."""
    override_own: str | None = None
    """This item's OWN manual decision, ignoring any it inherits from its show -- what a
    Spare/Reap control on this row can actually toggle. Equals ``override`` for a movie (no
    show to inherit from). ``None`` for a season kept only because the whole show is spared:
    its control rests un-lit, and ``show_override`` says why it is still kept."""
    show_override: str | None = None
    """The whole-show decision covering this season (its show key's ``"spare"``/``"reap"``),
    or ``None``. Drives the "kept because the whole show is spared" note beside a season's
    control, so the operator knows a season-level toggle will not change a show-level choice.
    Always ``None`` for a movie."""
    override_effective: bool | None = None
    """For a ``"reap"`` override: whether the engine honors it (it joins the counts, the
    grace countdown and the next plan), or refuses it for a safety stop, or for a row Reaper
    cannot identify -- a bad Plex match, or an explanation it could not read. None when there
    is no reap override. The UI shows red only on True."""
    spare_expires_at: str | None = None
    """When the spare *in effect* on this item stops keeping it, ISO-8601. ``None`` means the
    spare is forever -- read it only when ``override`` is ``"spare"``, where ``None`` is "kept
    for good" and a value drives the "N days left" countdown on the card. Mirrors ``override``:
    a season with no spare of its own carries the expiry of the show spare that keeps it."""
    spare_covers_until: str | None = None
    """When the LAST spare covering this item stops keeping it -- its own or its show's,
    whichever runs longer -- ISO-8601, ``None`` for a forever one.

    The fate question, where ``spare_expires_at`` above is the precedence question. That one
    names the spare Reaper is reading right now, which is what a control toggles and clears
    (rule 50). This one names when the file stops being kept, which is what a color or a
    sentence about its fate must say (rules 49/61). They differ whenever both levels spare an
    item and the higher-precedence spare runs out first: a season spared ten days inside a show
    spared forever is kept forever, and reading the own key alone drew "expired" over a file
    nothing would remove.

    A spare must be in force at a level for that level to count, so a season spare lapsing under
    a show set to REAP still reads as expired -- there, the file really is handed back. Read only
    when ``override`` is ``"spare"``."""
    show_spare_expires_at: str | None = None
    """When the whole-show spare covering this season stops keeping it, ISO-8601, or ``None``
    for a forever show-spare (or none at all). The show-level twin of ``spare_expires_at``,
    read only when ``show_override`` is ``"spare"`` -- the show card's countdown. Always ``None``
    for a movie."""
    chip: ChipOut | None = None
    """The card's one short status chip (Sanctuary and Limbo lanes). None on condemned
    rows, whose card leads with the amber dormancy pill instead."""
    season_number: int | None = None
    """The season this row is (from its media_key), for season rows. None for movies and
    for a key that did not parse -- display only, never identity."""
    group_seasons: list[GroupSeasonMarkOut] | None = None
    """The whole show's per-season verdict marks (every lane, whole snapshot), for the
    show card's season strip. Set on rows that belong to a group; None for movies."""
    show_status: str | None = None
    """Whether the show is finished: ``"ended"``, ``"continuing"`` or ``"unknown"``. None
    for a movie, where the question does not apply. Three states, not a bool, so "the
    server did not say" can never be drawn as a definite answer -- ``"unknown"`` renders
    in the "we could not check" treatment. ``"continuing"`` is labeled "Still going",
    because that arm also covers a show that has not started airing yet."""


class CandidateDetail(CandidateOut):
    explanation: Explanation
    explanation_unreadable: bool = False
    """True when ``explanation`` above is the degraded fallback rather than what the scan
    stored. The panel says so instead of rendering empty reason blocks, which would read as
    "nothing protected this" when the truth is that nothing could be read."""
    links: LinksOut = Field(default_factory=LinksOut)
    ratings: RatingsOut | None = None
    content_rating: str | None = None
    runtime_minutes: int | None = None
    genres: list[str] = Field(default_factory=list)


class CandidatePageOut(BaseModel):
    """One page of the review queue, plus the size of the whole filtered set.

    The totals are measured over every row the filters keep, *before* the page window, so
    the queue can head the list with a count and a byte total it has not loaded. They rode
    in four custom response headers until this model existed, which meant the one thing a
    reader of the published document could not see was the shape of the answer.
    """

    items: list[CandidateOut]
    total: int
    """How many rows the filters keep, across every page."""
    total_bytes: int
    """Summed over the rows that have a size. ``unknown_size`` counts the rest."""
    unknown_size: int
    """How many of those rows have no size at all. A SUM skips them without saying so, so
    the count is taken in the same query and reported beside the total -- otherwise an
    unmeasured library is indistinguishable from an empty one."""
    offset: int
    """Where this page starts. The queue asks for ``offset + len(items)`` next."""
    snapshot_id: int | None = None
    """Which snapshot the page was drawn from, or None before any scan has run. The queue
    compares it against the newest completed scan to notice when a fresher snapshot has
    landed under an open review."""


class GroupOut(BaseModel):
    """One show, whole: the show-level header the info panel draws, plus every season
    row in the latest snapshot regardless of verdict. Read-only display; the seasons
    are the same frozen candidate rows the queue lists, never a re-decision."""

    group_key: str
    title: str
    year: int | None = None
    """The earliest season year on record -- the year the show reads as."""
    poster_url: str | None = None
    summary: str | None = None
    size_bytes: int
    """Total on disk across every season row in the snapshot. Stays a definite ``int``
    rather than going nullable: two signals for one fact would force every reader to
    handle a null AND a count. It sums the seasons that have a size, and
    ``unknown_size_seasons`` says how many it could not."""

    unknown_size_seasons: int = 0
    """How many season rows have no size, and are therefore left out of the total above.
    Hidden at zero."""
    reason: str | None = None
    library: str | None = None
    """The show's Plex library (section), taken from its season rows (they all share it).
    None when no row carries one. Drives the show panel's library chip."""
    chip: ChipOut | None = None
    """The show-level status line and chip: those of its highest-scoring season, the
    same member the collapsed card leads with."""
    show_override: str | None = None
    """The show's own manual decision (``"spare"``/``"reap"`` on the show key), or ``None``.
    What the panel's whole-show control toggles, and what lights it -- never an aggregate of
    the seasons' own decisions, which the control cannot clear. Seasons overridden one by one
    keep their marks in the strip; this stays ``None`` until the whole show is decided."""
    show_spare_expires_at: str | None = None
    """When the whole-show spare stops keeping the show, ISO-8601, or ``None`` for a forever
    spare (read only when ``show_override`` is ``"spare"``). The panel's whole-show countdown."""
    links: LinksOut = Field(default_factory=LinksOut)
    show_status: str | None = None
    """Whether the show is finished, for the show card: ``"ended"``, ``"continuing"`` or
    ``"unknown"``. Taken from the season rows, which is safe because this is a show-level
    fact: one observation of the series is stamped onto every season of it in the same
    scan, so the rows of one group cannot disagree. None only if the group somehow holds
    no row carrying it (a pre-rescan snapshot), and the card then shows nothing."""
    seasons: list[CandidateOut] = Field(default_factory=list)
    """Every season, sorted by season number (unnumbered rows last)."""


class SnapshotOut(BaseModel):
    id: int
    created_at: str
    policy_hash: str
    horizon_at: str
    item_count: int

    degraded: bool
    """No run may execute against a degraded snapshot. It may still be VIEWED -- the
    owner should be able to see exactly what went wrong."""

    degraded_reason: str | None = None

    condemned: int = 0
    protected: int = 0
    abstained: int = 0
    reclaimable_bytes: int = 0
    unknown_size_items: int = 0
    """How many condemned items have no size. ``reclaimable_bytes`` is the total of what
    IS known, and this is carried beside it rather than folded in as zeros: a sum with an
    unmeasured item in it is quietly low, whereas a total plus a count is honest. Hidden
    at zero, so a healthy library shows nothing new."""


class ProfileSettingsIO(BaseModel):
    """The caps, whether they are enforced, grace, and the unknown-size allowance -- how
    much Reaper may do, and how long it waits. Deliberately not part of the policy hash:
    tightening a cap never voids a pending approval. Validation (a run cap above the
    rolling cap, a grace under a week) is enforced by the domain, so an out-of-range value
    comes back as a 422 with the reason, not a silent clamp.
    """

    max_items_per_run: int = Field(ge=1, le=1000)
    max_bytes_per_run: int = Field(ge=1)
    max_items_per_30d: int = Field(ge=1)
    max_bytes_per_30d: int = Field(ge=1)
    caps_enabled: bool = True
    grace_days: int = Field(ge=7)
    max_unmeasured_per_run: int = Field(default=0, ge=0, le=25)
    """How many items with no size one run may delete. The GB caps cannot bound them, so
    this count is the only bound there is. Defaults to 0: never."""

    settings_recovered: bool = False
    """Read-only (GET). True when the stored settings could not be read and these are the
    shipped defaults, which can be looser than what was saved. The Pace page shows a recovery
    notice; a scan degrades until the operator saves again. Ignored on save."""


class ActionStepOut(BaseModel):
    """One journalled action, exactly as it would be sent. No credentials.

    This is the third block of the why-panel made literal: the actual method, path and
    body the approval would issue. Safe to render because the api key is injected only at
    send time, never stored here.
    """

    media_key: str
    ordinal: int
    kind: str
    method: str
    path: str
    body: dict[str, object] | None = None
    state: str
    is_canary: bool

    error: str | None = None
    """Why this step failed or was skipped, as the executor recorded it. ``None`` on a step
    that has not run or that succeeded.

    Already operator copy: ``_fail`` and ``_skip`` write ONE sentence and use it for both this
    column and ``StepOutcome.detail``, which the after-action report already shows. The
    difference is only that this one is durable -- the report lives in memory on ``app.state``,
    so before this a restart left the table saying a step failed with nothing saying why, while
    the reason sat in the row the whole time (#260). Never add a message here that is not
    already fit for the operator to read (rule 21)."""


class RunOut(BaseModel):
    """A planned or executed reap run: the durable record of what Reaper would do."""

    id: int
    snapshot_id: int
    state: str

    item_count: int
    total_bytes: int
    confirmation_phrase: str
    """The content-bound typed confirmation, e.g. "REAP 7 SOULS 214 GB". Derived from the
    exact set this run would delete, so a stale plan reads as obviously different."""

    held_back_unknown_size: int = 0
    """How many condemned items this plan left out because nothing would report their
    size. Zero for a healthy library, and every surface hides it at zero, so an operator
    whose sources all answer never sees a new number anywhere."""

    step_count: int
    """How many journal rows this run holds in total. ``steps`` below carries a window of them,
    so this is what a surface counting them must read: ``len(steps)`` is the size of the page,
    never the size of the plan. It is NOT ``item_count`` either, which counts deduplicated
    candidates: a season is three steps sharing one key, so the two differ by 3x on a show."""

    # The approval audit -- ``policy_hash``, ``approved_manifest_hash``, ``approved_by`` and
    # ``approved_at`` -- is deliberately NOT on this response. Every interlock reads the stored
    # row rather than the wire model, so nothing enforcing an approval loses anything: the
    # manifest re-check and the policy re-check both read ``run.`` off the ORM object, and
    # ``approved_at`` reaches ``_watched_since_approval`` the same way. ``approved_by`` was the
    # constant string "api" on every response an operator could obtain.
    steps: list[ActionStepOut]
    """The first :data:`api.runs.STEP_PAGE` rows of the journal, not all of them. A plan of 500
    seasons is 1,500 rows carrying a path and a request body each, and the table draws 50.
    ``GET /api/runs/{id}/steps`` serves any window, and ``step_count`` above says how many there
    are, so a surface can say how many it is not showing."""


class RunStepsOut(BaseModel):
    """One window of a run's journal.

    Its own route rather than query parameters on the run detail, for two reasons. Building a
    ``RunOut`` re-reads the whole effective condemned set and re-derives the confirmation
    phrase, so paging through it would pay that per page. And the browser holds the run detail
    under one cache key with an infinite stale time, which is what lets the confirmation sheet
    keep the exact plan it opened with; an offset in that key would move the object under it.
    """

    steps: list[ActionStepOut]
    step_count: int
    """The whole journal's size, so a caller can page without a second request."""
    offset: int


class RunSummaryOut(BaseModel):
    """One line of the run history: what a list of past plans can honestly say cheaply.

    Every field is read straight off the stored row. The full ``RunOut`` is a different
    shape on purpose -- its ``item_count``, ``total_bytes`` and ``confirmation_phrase``
    are all derived from the effective condemned set, re-read NOW, which costs the whole
    candidate table of that run's snapshot per run. Producing them for a list of fifty
    loaded the same thousands of rows dozens of times on every visit to the Reap page
    (P-3). It was also dishonest for a finished run: recomputing the phrase against
    today's overrides describes a plan that never existed.

    Open a run to see what it holds; the detail route derives those numbers for one.
    """

    id: int
    state: str
    approved_at: str
    aborted_reason: str | None = None


class RunCheckOut(BaseModel):
    """One line in an item's after-action checklist: a step the reap did or verified, and
    whether it passed. Rendered as a ``✓``/``✗`` tick, like the why-panel's checks."""

    label: str
    ok: bool


class RunOutcomeOut(BaseModel):
    """What happened to one item in the run."""

    media_key: str
    title: str
    kind: str
    state: str  # verified | failed | skipped
    detail: str
    checks: list[RunCheckOut]


class RunReportOut(BaseModel):
    """What a run did, or -- in a dry run -- would do. Every mutating step in a dry run is
    proven and none is sent; in a real run each was issued and its result verified."""

    run_id: int
    dry_run: bool
    state: str
    aborted_reason: str | None = None

    would_delete_items: int
    """The count of items removed. In a real run this is what was actually deleted; in a
    dry run it is 0, because a dry run proves the plan by skipping every send."""

    deleted_bytes: int = 0
    """Bytes reclaimed by a real run. 0 for a dry run.

    Summed from the frozen ``Candidate.size_bytes``, so for movies it is a close lower
    bound on what the disk got back rather than an exact figure (#317). Under-stating is
    the harmless direction here: the operator is never told they freed more than they did."""

    deleted_unmeasured: int = 0
    """How many of the deleted items had no size, so are absent from ``deleted_bytes``.
    Above zero only when the operator allowed unmeasured items. Hidden at zero."""

    skipped: int
    outcomes: list[RunOutcomeOut]
    """Per item: what happened, with a plain-English checklist of the steps performed and
    which (if any) failed."""


#: A media_key's storage bound (``ActionStep.media_key`` / ``WhitelistEntry.media_key`` are
#: ``String(100)``), applied at the edge so an over-long key is a 422 rather than something
#: that reaches a plan build or a whitelist query first. A real key is well under it:
#: ``sonarr:<instance>:<series>:<season>``.
_MAX_MEDIA_KEY = 100

#: The most items one "reap selected" request may name. Far above any real selection -- a
#: first big cleanup is hundreds, not thousands -- and low enough that a leaked API key
#: cannot push a multi-megabyte list into a plan build and the whitelist queries under it.
#: The whole condemned set is requested by OMITTING the field, so this never truncates a
#: legitimate "reap everything".
_MAX_SELECTED_ITEMS = 5000


class CreateRunIn(BaseModel):
    """Optional body for building a plan. Omit for a plan over the whole condemned set;
    pass ``media_keys`` to reap just those items -- the safe path for a first, single,
    hand-picked deletion, and the future 'reap selected' action."""

    media_keys: list[str] | None = Field(default=None, max_length=_MAX_SELECTED_ITEMS)


class ExecuteRunIn(BaseModel):
    """The typed, content-bound confirmation required to execute a real reap.

    Not a checkbox: the phrase carries the exact count and size ("REAP 7 SOULS 214 GB"),
    derived from the plan, so muscle memory cannot carry someone through it and a stale
    tab's phrase no longer matches the current plan. The server recomputes the expected
    phrase and refuses anything else.
    """

    confirmation_phrase: str


class GateSettingOut(BaseModel):
    """One protection's row as it is SERVED, which is every id a stored body can hold.

    Wider than ``GateSettingIn`` below by exactly one thing: it does not ask whether the id
    is authorable. A stored body may carry ``whitelisted`` or ``curated_list`` long after
    both stopped being switches, because ``policy_migrations.convert_list_protections``
    leaves an ENABLED one in place when its replacement keep rule cannot be named -- keeping
    the cover and letting ``scan_runner.build_gates`` refuse the scan loudly, rather than
    withdrawing a live protection in silence (rule 38). That row has to reach the editor, or
    the one page that can clear it 500s on the way to rendering (#627).
    """

    gate: GateId
    enabled: bool = True
    threshold: int = 0
    window_days: int = Field(default=365, ge=1)


class GateSettingIn(GateSettingOut):
    """The same row on the way IN, where the id must be one a policy may carry."""

    @field_validator("gate")
    @classmethod
    def _must_be_authorable(cls, v: GateId) -> GateId:
        """Refuse a gate no policy row can build, at the boundary.

        ``GateId`` is wider than what a policy may carry: it also holds retired ids (so a
        stored explanation still decodes) and ids the engine emits on its own
        (``SEASON_PROGRESSION``, ``CUSTOM``). Typing this field as a bare ``GateId`` let a
        hand-crafted save store one of those, and ``build_gates`` then refused to construct
        it -- correctly, but on every subsequent scan, so the install went offline with no
        self-heal and nothing pointing at the save that did it.

        Rejecting here rather than dropping it: this is operator input asking for a
        protection that does not exist, so the honest answer is to say so. The load path
        deliberately still only drops ``PolicyBody.RETIRED_GATES``, because silently
        dropping an id from a *stored* body is safe only for a gate that could never keep a
        file, and widening that would put a real protection one typo away from vanishing
        (rule 38/117).

        **Input only, which is why the served body is typed off the parent.** This ran on
        the way out too, since ``PolicyOut.body`` was a ``PolicyIn``, so a stored row this
        refuses took ``GET /api/policy`` down with it -- and the operator's one exit is that
        page (#627). The refusal itself is unchanged: nothing may WRITE one of these ids.
        """
        if v not in POLICY_AUTHORABLE_GATES:
            # No gate id in the sentence. An operator reads this as "Can't save this: ..." on
            # the policy page, where the row it is about is labeled in their own words ("On a
            # list you curate yourself"), so the slug names nothing on screen (rule 21).
            # ``scan_runner.build_gates`` already refuses to print the same id for the same
            # state and says why; this is that decision applied to its sibling (rule 144).
            # It went from unreachable to routine when the response stopped being validated
            # through here: the editor re-validates the loaded draft on mount, so an upgraded
            # install meets this before touching anything. The 422's ``loc`` still carries
            # ``body.gates.<i>.gate``, so an API caller can still tell which row.
            raise ValueError(
                "That protection is left over from an older version and can't be saved. "
                "Turn it off, then save."
            )
        return v


class SignalSettingIn(BaseModel):
    signal: SignalId
    weight: int = Field(ge=0, le=100)
    saturate_at: int = Field(ge=1)
    floor: int = Field(default=0, ge=0)


class SignalProbeIn(SignalSettingIn):
    """Try one signal's settings against one value.

    Inherits the settings half rather than restating it, so a probe refuses exactly what a
    save refuses (rule 131): answering for a pair the editor could not then store would
    describe a policy that cannot exist.
    """

    kind: Literal["signal"] = "signal"

    value: float = Field(ge=0, le=1e15)
    """The value to try, in the units the signal is stored in: days, watchers, a season's
    rank, a rating in tenths, or bytes. The ceiling is a boundary bound rather than a real
    one (rule 95) -- above any file anyone has, and below where a float stops counting whole
    numbers."""


#: What ``POST /api/policy/probe`` accepts.
#:
#: One member today, and typed as a discriminated ``kind`` anyway, which is the whole point:
#: a second probe -- what a keep rule would discount, what a graded rule of the operator's
#: own would add -- becomes ``Annotated[SignalProbeIn | KeepProbeIn,
#: Field(discriminator="kind")]`` and every client that already sends ``kind`` keeps working.
#: Inferring the shape from which fields happened to be present is the thing rule 142 exists
#: to stop, and it is far cheaper to type it now than to add the discriminator to a wire
#: format that already shipped without one.
#:
#: No speculative members: a probe kind arrives with the surface that asks it and the tests
#: that pin it, or it does not exist (rule 38/117).
PolicyProbeIn = SignalProbeIn


class PolicyProbeOut(BaseModel):
    """What the engine answered. One shape for every probe kind, so the editor reads them
    the same way and a new kind needs no new rendering path."""

    points: float
    """What this rule would move the score by, in the rule's own direction: pressure for a
    signal, and a discount for a keep rule when one is added.

    The only field. The engine's own wording for the answer used to ride beside it and no
    client ever rendered it: ``signalRamp.ts`` words both the editor's sentence and the
    panel's row, which is where those two are held in step, so a second wording arriving
    over the wire would have been a third copy rather than the thing reconciling them."""


class ConditionIn(BaseModel):
    """One user-authored protect condition: keep a title when ``field op value``."""

    field: str
    op: Op
    value: int | str | bool


class PolicyBodyOut(BaseModel):
    """A policy body as it is SERVED: exactly what is loaded, gate rows included.

    ``PolicyIn`` below is this model with the gate ids narrowed to the ones a save may
    write, and that is the only difference between the two. The split exists because the
    response used to be typed as the request: ``_policy_out`` rebuilt every loaded row as a
    ``GateSettingIn``, so the one stored shape the loader deliberately preserves -- an
    enabled ``whitelisted`` or ``curated_list`` whose replacement keep rule cannot be named
    -- raised out of ``GET /api/policy`` and locked the operator out of the editor that
    clears it (#627). Serving it is what makes ``PolicyEditor``'s leftover-row notice
    reachable.

    Widening on the way out only. Saving that body back is still refused, so the row can
    leave a stored policy only by the operator's own act.
    """

    name: str = "default"
    media_type: str = "movie"
    condemn_at: int = Field(ge=1, le=100)
    coverage_floor_bp: int = Field(default=5000, ge=0, le=10_000)
    # Ceilings, not policy opinions (rule 95). `active_progress` computes
    # `now - timedelta(days=hold_days)` and `sequential_protections` builds
    # `range(lookahead + 1)` per anchor per viewer per show, so an unbounded value raises
    # OverflowError out of a scan and out of `/policy/simulate`, or allocates inside the
    # event loop. Each ceiling sits far above any real setting, so adding them cannot
    # invalidate a body an operator already saved. `engine.policy.PolicyBody` declares the
    # same three, and `test_policy.py` fails when the two stop agreeing (rule 131).
    keep_last_seasons: int = Field(default=2, ge=0, le=1_000)
    keep_first_season: bool = True
    keep_last_scope: Literal["all", "requested"] = "all"
    season_lookahead: int = Field(default=0, ge=0, le=1_000)
    keep_in_progress: bool = True
    in_progress_hold_days: int = Field(default=180, ge=0, le=36_500)
    keep_specials: bool = True
    protect_incomplete_seasons: bool = True
    flag_keep_conflicts: bool = True
    gates: list[GateSettingOut]
    signals: list[SignalSettingIn]
    protect_conditions: list[ConditionIn] = Field(default_factory=list)
    # The engine spec is reused directly (not a parallel *In model) so its lane/numeric
    # validation runs on the wire and the two cannot drift.
    custom_condemn: list[CustomCondemnSpec] = Field(default_factory=list)
    graded_keeps: list[GradedKeepSpec] = Field(default_factory=list)
    # The engine spec is reused directly (like custom_condemn/graded_keeps) so its
    # per-source vote-floor validation runs on the wire.
    keep_rating_rules: list[RatingRuleSpec] = Field(default_factory=list)
    keep_rating_match: Literal["any", "all"] = "any"


class PolicyIn(PolicyBodyOut):
    """A policy body on the way IN: every field above, with the gate ids narrowed.

    Narrowing, never widening, so anything that accepts a served body accepts this one and
    the save boundary is the strict end of the pair. ``list`` is invariant, which is the
    whole of the ignore below -- ``GateSettingIn`` is a subclass of ``GateSettingOut``, and
    a redeclaration is how Pydantic is told to run the stricter row model here.
    """

    gates: list[GateSettingIn]  # type: ignore[assignment]


class PolicyValidateIn(PolicyIn):
    """A policy draft to check, plus the one profile value whose warning is anchored to a
    control in the same editor.

    ``inspect`` reads the operator's SAVED profile, which is right nearly everywhere: a
    warning about a cap should describe what is in force. The unknown-size allowance is the
    exception, because its warning renders directly beneath the box that sets it and that box
    shows the DRAFT. Every other warning in the editor describes the draft, so this one read
    the saved value while sitting under the changed one: drag it from 5 to 0 and the warning
    kept saying Reaper would delete up to 5 (B-26, rule 42).

    Omitted (``None``) means "use the saved profile", which is what every other caller of
    ``inspect`` wants and what this route did before."""

    draft_max_unmeasured_per_run: int | None = Field(default=None, ge=0, le=25)


class PolicyWarningOut(BaseModel):
    field: str
    message: str
    severity: str


class SeasonShapeOut(BaseModel):
    """How many content-bearing seasons each show has in the latest snapshot, so the policy
    editor can show live how many shows a keep-last-N value fully protects -- without a new
    scan, since the season shape is independent of the keep-last value."""

    total_shows: int
    season_counts: dict[int, int]
    """season count -> number of shows that have exactly that many content-bearing seasons."""


class PolicyOut(BaseModel):
    policy_hash: str
    name: str
    body: PolicyBodyOut
    """The body the editor opens on, which is what was LOADED and not what a save accepts.

    Typed off the served model rather than the request one: a stored gate row the loader
    kept on purpose has to reach the page that removes it, and typing this as ``PolicyIn``
    made the response validate through the save boundary and 500 instead (#627).
    """

    default_signals: list[SignalSettingIn] = []
    """The SHIPPED bounds for this media type's signals, so the editor can offer a way back.

    Making the ramp bounds editable made them losable: nothing on the page said what 1825
    had been, and the presets restore weights only. Derived from ``DEFAULT_MOVIE_POLICY`` /
    ``DEFAULT_TV_POLICY`` on the way out rather than copied into the browser, so there is no
    second declaration of a number the scorer reads (rule 103).

    Weights are carried but the editor restores only the bounds: removal weights must total
    exactly 100, so putting one back on its own would break the budget the save bar enforces.
    """

    history_reach_days: float | None = None
    """How far back the watch mirror goes, for the editor to say beside the controls it
    bounds.

    The dormancy ramp is the one it bounds hard: `dormancy.reference_instant` anchors a
    never-played item at the LATER of its arrival and the mirror's edge, so the largest
    dormancy any item can present IS this number. Setting "full points" past it therefore
    caps what that signal can ever pay, and until the editor could say this, nothing on the
    page could tell the operator so.

    ``None`` when the scan did not record it, which the editor renders as not knowing rather
    than as a reach of zero."""

    repairs: list[PolicyRepair] = []
    """Every way this body had to be changed to load it, so it is NOT what is stored.

    One list rather than a boolean per repair, and the editor reads its LENGTH to decide
    whether to open dirty. That is the fix for the shape this used to have: three booleans,
    each of which had to be remembered at four sites, and the fourth repair remembered at
    one. A stored body from before the lists move then degraded every scan while the editor
    stayed clean, so the page held no Save and the degradation named an exit that was not
    there (#516). A member added to ``PolicyRepair`` now raises the savebar whether or not
    anyone wrote copy for it, and ``tests/test_policy_repairs.py`` fails until they do.

    A field rather than a ``warnings`` entry, like the flags before it: the editor builds
    its warning list by re-validating the *draft*, so anything attached to this response is
    never read -- a load-time warning put there is silently dropped.
    """
    warnings: list[PolicyWarningOut]
    """Things that are legal but probably not what you meant. A validator cannot tell
    an IMDb floor of 96 (meaning 9.6) from a Rotten Tomatoes 96 typed into the wrong
    box -- both are legal -- so it says so instead of pretending to know."""


class SimExampleOut(BaseModel):
    """One title the draft would newly flag."""

    title: str
    year: int | None = None
    score: int


class GateCountOut(BaseModel):
    """One protection and how many items it is keeping, for the simulator."""

    gate: str
    count: int


class SimStale(enum.StrEnum):
    """Why the simulator would not answer -- as a value, not as a sentence.

    Three refusals with three different remedies, and until this existed the panel showed
    one paragraph naming every cause at once, so nine season controls, the protection lists
    and the popularity window shared a sentence that could only be right about one of them. The
    operator's copy lives in ``PolicySimulator.tsx`` and branches on this; the ``stale_reason``
    beside it is the same fact as a sentence, for a reader of the API (rule 66: the frontend
    handles an id it does not know by falling back to that sentence, never by guessing).
    """

    GATHERS_DIFFERENTLY = "gathers_differently"
    """What a scan would collect no longer matches what this one did -- the span watching
    counts over, or the protection lists an ``on_list`` rule reads. The frozen evidence
    answers a different question, and only a scan fixes it.

    The lists reach this without any policy edit at all, which is why the sentence says the
    numbers do not match the last scan rather than naming something the operator changed."""

    SEASONS_NOT_RECORDED = "seasons_not_recorded"
    """This snapshot holds no per-show season-prune evidence, holds some it cannot read, or
    holds some that does not describe the rows being judged.

    Reached only *after* a scan that wrote the table: a snapshot older than it cannot match
    the re-scoped ``evidence_hash`` either, so it refuses one tier earlier as
    :attr:`GATHERS_DIFFERENTLY` (``api.simulate.simulate`` states the same thing at length).
    Like every refusal it zeroes the whole lane rather than the season card alone --
    ``simulate._SeasonEvidenceMissingError`` says why holding the rest at their scan-time
    verdicts would be worse."""

    IN_PROGRESS_NOT_READ = "in_progress_not_read"
    """The draft holds a season someone is part-way through, over a scan that recorded no
    episode map to place them in. Two scans leave it unread -- one that ran with the hold off,
    and one that ran with it on and got no answer from Sonarr for some show -- so the copy
    states the absence rather than either cause. Turning the hold OFF replays fine: the guard
    is short-circuited before the missing map is touched, which is why this names the one
    direction that cannot be answered rather than the whole control."""


class SimulationOut(BaseModel):
    """Re-deciding the last snapshot under a candidate policy. Zero API calls.

    The point is that the knob and its blast radius sit in the same viewport: move the
    threshold, watch the count and the byte total move with it.
    """

    exact: bool
    """Whether these numbers actually answer the question that was asked.

    The simulator re-compares **stored** scores and verdicts against new thresholds.
    That is exact for ``condemn_at`` and ``coverage_floor_bp``, and **wrong for
    anything else**: change a signal weight or a gate and the stored scores were
    produced by the old ones, so every count below would be confidently stale.

    When this is false the counts are zeroed, ``stale_kind`` says which refusal it is and
    ``stale_reason`` says the same thing in a sentence. Reaper would rather show nothing than
    show a number it cannot stand behind -- a plausible wrong answer is worse than a blank,
    because the owner acts on it.
    """

    stale_kind: SimStale | None = None
    """Which refusal this is, typed, so the panel can name the control at fault. ``None``
    exactly when ``exact`` is true."""

    stale_reason: str | None = None

    condemned: int
    protected: int
    abstained: int
    reclaimable_bytes: int
    unknown_size_items: int = 0
    """How many of the condemned have no size, and so are left out of the total above
    rather than folded in as zeros. Hidden at zero."""

    newly_condemned: int
    """Items this policy would condemn that the current one does not. The number the
    owner actually needs before saving."""

    no_longer_condemned: int

    condemned_before: int = 0
    """How many titles the LAST SCAN flags, so the panel can compare against it directly.

    The scan, not the saved policy: both tiers count it as
    ``effective_verdict(row, decisions) == "condemn"`` -- the stored verdicts, with live
    overrides applied. Saving a policy starts a scan rather than being one, so between the
    save and that scan finishing, or after it fails, the saved policy is not the policy these
    rows were judged under. ``changed_titles`` below names the same set the same way ("the
    lane they are in now"), and the panel's sentence follows both.

    Equal to ``condemned - newly_condemned + no_longer_condemned``, which is how the panel
    used to reconstruct it. That derivation is only sound while both deltas count every way
    into and out of the removal list, and it printed a confident wrong number the moment one
    of them did not: ``no_longer_condemned`` missed condemn -> protect, so an outright keep
    rule put the draft's own count on both sides of a sentence built to contrast them. The
    server counts the pre-edit lane per row either way, so sending it costs one line per tier
    and leaves the browser with no server fact to re-derive (rule 144).
    """

    changed_titles: int = 0
    """How many titles this draft puts in a different lane than the one they are in now.

    A superset of ``newly_condemned`` + ``no_longer_condemned``, and it exists because those
    two are blind to the move that prompted it: a protection edit can take a title from
    spared to not judged without going near the threshold, so both deltas stay at zero while
    a sixth of the spared set moves, and the panel shows the other rows as absolute counts
    rather than deltas -- two different outcomes, one indistinguishable screen (#488).

    Zero while the draft differs from the saved policy is the useful case, not the empty one:
    it is the only form in which the panel can say a rule carries no weight on THIS library,
    which is a true and load-bearing fact about a protection the operator is considering.
    """

    histogram: list[int]
    """Score distribution in 10-point buckets, so the threshold can be placed against
    the shape of the library rather than guessed."""

    examples_newly_condemned: list[SimExampleOut] = Field(default_factory=list)
    """The top few titles this draft would newly flag, highest score first -- the
    "New on the list" block. Populated only when ``exact``; a count is abstract, but
    a title the owner recognizes is what actually stops a bad threshold."""

    protected_by: list[GateCountOut] = Field(default_factory=list)
    """How many protected items each protection saved, busiest first -- the "Why
    titles were spared" block, aggregated from the stored explanations. Populated
    only when ``exact``."""


class FieldValuesOut(BaseModel):
    """Distinct values Reaper has already seen for one rule field, newest scan only.

    Suggestions for the rule editors' inputs, nothing more: an unknown field or a
    missing scan comes back as an empty list, never an error, and typing a value that
    is not listed stays valid -- validation is by type, not by membership here.
    """

    field: str
    values: list[str]


class FieldOut(BaseModel):
    key: str
    label: str
    help_text: str
    type: FieldType
    unit_suffix: str
    ops: list[Op]


class VocabularyOut(BaseModel):
    """The fields available in ONE lane.

    Filtered server-side, before serialization. A protect-only field is never even
    offered to the condemn editor, so a dangerous condition is not merely rejected --
    it is unconstructable.
    """

    lane: Lane
    fields: list[FieldOut]


class LeavingSoonOut(BaseModel):
    """The result of one shelf pass across every enabled library."""

    ok: bool
    """Whether the pass did what it set out to do. Preview is not a failure; no library
    turned on, or one that failed, is."""
    result: str
    """The one plain sentence describing this pass, worded by the service
    (``LeavingSoonResult.summary``) and stored on the Jobs row in the same breath. The
    browser renders it and never composes its own, which is how the row and this response
    came to say different things about one pass (#555)."""
    # `problems` used to ride here as a per-library list. It was only ever read as
    # `problems.length > 0`, never rendered, and the split that moved the wording into the
    # service took even that reader away -- so it shipped a field no operator could reach,
    # describing itself as "in plain words" while carrying `str(exc)`. `result` now names the
    # failing libraries, which is the part they needed; the raw cause stays in the
    # `leaving_soon.problems` log event, where a stack-shaped sentence belongs (rule 64).


class SignalCountOut(BaseModel):
    """How many condemned titles one signal pushed toward removal. ``id`` is a built-in signal
    id or a custom rule's name; the UI maps the built-ins to plain labels and shows a custom rule
    under its own name."""

    id: str
    count: int


class ReapBreakdownOut(BaseModel):
    """What a reap built right now would remove, and why. Read-only; deletes nothing.

    Counts are the reap decision (measured and unmeasured together); the byte figures sum
    only what has a size. Three of them say how much they left out, in ``will_reap_unknown``,
    ``movies_unknown`` and ``seasons_unknown``; the others do not, so a byte total beside a
    count is not a claim that the count is fully measured. ``has_snapshot`` is false before the
    first scan, when every figure is zero."""

    has_snapshot: bool
    policy_condemned: int
    policy_condemned_bytes: int
    hand_spared: int
    spares_expired: int = 0
    """The share of ``hand_spared`` a scan would hand back to policy: titles kept out of this
    plan by a spare whose clock has already passed. They are still being kept -- only a scan
    realizes a spare's expiry -- so they are absent from this plan with nothing on the page to
    explain it. The Reap page shows one line when nonzero, saying a scan judges them again.

    A count of TITLES, not of spares: one whole-show spare can be holding several condemned
    seasons. A title whose own clock has passed but which another spare still covers is not
    counted, because a scan would not release it."""
    hand_reaped: int
    hand_reaped_bytes: int
    hand_reaped_held: int = 0
    """Hand reaps the engine won't honor yet, so they are not in ``will_reap``. The page shows
    one line when nonzero so the operator's held marks are not silently dropped."""
    will_reap: int
    will_reap_bytes: int
    will_reap_unknown: int
    movies: int
    movies_unknown: int = 0
    """The unmeasured share of ``movies``, so the page can subtract exactly the rows the
    planner holds back and keep its split in step with its total."""
    seasons: int
    seasons_unknown: int = 0
    """The unmeasured share of ``seasons``; see ``movies_unknown``."""
    condemned_by: list[SignalCountOut]


class PlexTrashOut(BaseModel):
    """What Plex would remove besides the files a reap deletes.

    Reaper's end-of-run purge is section-wide, so it destroys the library records of
    everything already in the trash, not just what the run caused. Those items sit on both
    sides of the executor's before/after count and cancel out of its gate, so this read is
    the only thing that can see them. No file on disk is affected either way.
    """

    configured: bool
    """False when no Plex server is linked, in which case nothing purges and the page says
    nothing at all."""
    trashed: int = 0
    """Items in the trash across the libraries included in scans. A FLOOR: it counts only
    the libraries that answered, so read it with ``sections_unreadable``."""
    sections_unreadable: int = 0
    """Libraries whose trash could not be counted. Nonzero means ``trashed`` is incomplete,
    and the page warns rather than reading silence as zero (rule 93)."""
    empties_after_scan: bool | None = None
    """Plex's own ``autoEmptyTrash`` preference, which is server-wide and ships ON. When
    true, Plex purges the trash itself after every scan Reaper's refresh triggers, so the
    executor's trash interlock never gets a say. ``None`` when it could not be read."""


class RequesterRowOut(BaseModel):
    """One person's row in Scales."""

    identity: str
    """The cross-portal person key: ``plex:{id}`` when linked, else ``local:{portal}:{id}``.
    Stable and always present, unique across portals (a bare Seerr id collides), so the
    frontend keys cards on it and opens the drawer by it (GET /fairness/people/{identity})."""
    plex_id: int | None = None
    """Their Plex account, or None for a Seerr account nobody linked to one.

    On the wire so the card can tell "we looked and they watched nothing" apart from "we
    cannot see their history at all". ``played_by_them`` below is structurally 0 for a None
    (``fairness._roll_up`` only counts plays inside ``if pid is not None``), and a card that
    renders that as a definite 0% tells the operator a confident zero about someone Reaper
    never measured, while they decide whose files to delete. Never a key: rule 63 keys rows
    on ``identity``, which is always present."""
    name: str
    requests_made: int
    gb_granted_bytes: int
    played_by_them: int
    reclaimable_items: int
    reclaimable_bytes: int


class UnmatchedRequestOut(BaseModel):
    """One requested title the last scan didn't include, for the "not in the last scan"
    panel. Merged by title across co-requesters, and classified so the panel can say why."""

    title: str | None = None
    """The display name. Null when it couldn't be looked up (no id, or the lookup failed);
    the row then shows a generic label from the type and date, never an id."""
    year: int | None = None
    media_type: str
    """movie | tv. The row reads it as "Movie" / "Series"."""
    is_4k: bool = False
    requested_at: str | None = None
    available_at: str | None = None
    reason: str
    """Why it isn't in the scan: ``after_scan`` (added since the scan ran), ``set_aside``
    (present but not judged), or ``no_id`` (no id to line it up with)."""
    requested_by: list[str]
    """Distinct requester names behind this title."""
    request_count: int
    """How many requests this row stands for, so the panel and the card's count agree."""


class FairnessReportOut(BaseModel):
    total_requests: int
    total_reclaimable_bytes: int
    total_reclaimable_items: int
    not_in_scan: int
    """Requests the last scan has not seen, so the numbers read as most of the requests.
    Exactly the requests behind ``unmatched`` (sum of their ``request_count``)."""
    unmatched: list[UnmatchedRequestOut] = []
    """The not-in-scan requests themselves, named and grouped by reason, for the panel."""
    no_snapshot: bool = False
    """True when no scan has ever run; Scales has nothing to sit on."""
    horizon_at: str | None = None
    """How far back the watch history reaches; the watched figures read against it."""
    rows: list[RequesterRowOut]


class QuotaLineOut(BaseModel):
    """One media type's request cap for a person. ``limit is None`` is unlimited; the window
    (``days``) and unit differ per type, so movies and series each carry their own."""

    limit: int | None = None
    days: int | None = None
    at_limit: bool = False


class PersonQuotaOut(BaseModel):
    seerr_total: int
    movie: QuotaLineOut
    tv: QuotaLineOut


class PersonTitleOut(BaseModel):
    """One title a person requested that the last scan still has, for the details drawer."""

    title: str
    year: int | None = None
    media_type: str
    is_4k: bool
    size_bytes: int | None = None
    """None when nothing about the title is measured; the row says "size unknown"."""
    requested_at: str | None = None
    available_at: str | None = None
    watched_by_them: int
    """A movie's raw plays, or a series' distinct episodes watched. The row reads it per
    ``media_type`` ("watched 3x" vs "62 episodes watched")."""
    verdict: str
    """condemn (reclaimable), protect (kept), or abstain (left to decide)."""
    item_id: int | None = None
    group_key: str | None = None
    co_requesters: list[str]
    poster_url: str | None = None
    """A ``/api/poster/{key}`` URL, or null when the title has no poster key."""


class PersonDetailOut(BaseModel):
    """One person's full request story, behind a Scales row."""

    plex_id: int | None = None
    name: str
    seerr_total: int | None = None
    requests_in_scan: int
    gb_granted_bytes: int
    played_by_them: int
    reclaimable_items: int
    reclaimable_bytes: int
    not_in_scan: int
    quota: PersonQuotaOut | None = None
    titles: list[PersonTitleOut]
    unmatched: list[UnmatchedRequestOut] = []
    """This person's not-in-scan requests, named and grouped by reason, for the panel."""
    horizon_at: str | None = None
    """How far back the watch history reaches. ``played_by_them`` and every title's
    ``watched_by_them`` are counted with no lower time bound, so a zero is a lower bound
    against this span, not a measured never-watched; null is an empty mirror, where no watch
    figure here means anything at all."""
    profile_url: str | None = None
    """The requester's page on their request portal, or null when it can't be built. The
    panel links the name to it, and shows plain text otherwise."""


class WhitelistEntryOut(BaseModel):
    """One hand-overridden item, as ``POST /api/override`` hands it back.

    Pydantic ships a class docstring as the schema ``description``, so this renders in the
    API reference. It used to say "as the Spared / overrides list shows it", naming a list
    surface that retired with ``GET /api/whitelist`` -- a reference telling a script author
    to go looking for a view that is not there (rules 25, 64)."""

    media_key: str
    title: str
    note: str | None
    decision: str
    """``"spare"`` (never reap) or ``"reap"`` (force onto the reap list)."""
    spare_expires_at: str | None = None
    """When a timed spare stops keeping the item, ISO-8601. ``None`` means kept forever (and
    always ``None`` for a reap)."""
    created_at: str


#: The most days a hand-spare may be set for -- ten years, so a typo cannot set a nonsense
#: century-long clock. A ceiling only: the floor is ``ge=0`` below, and ``0`` is the default
#: and means forever, which is the keep direction. This used to claim a ``ge=1`` floor
#: "so a typo cannot reap the file tomorrow" -- never implemented, and it would not have done
#: that anyway, since ``ge=1`` admits the one-day spare it describes (rule 7/24).
_MAX_SPARE_DAYS = 3650


class OverrideIn(BaseModel):
    """Override an item's verdict by hand -- spare it (keep) or reap it (force onto the list).

    The title is resolved server-side from the item's latest candidate, so the client sends
    only the identity, the decision, and optionally a note. A ``media_key`` may be a whole
    show's, in which case the decision applies to every one of its seasons."""

    media_key: str = Field(max_length=_MAX_MEDIA_KEY)
    decision: Override
    note: str | None = Field(default=None, max_length=500)
    spare_days: int = Field(default=0, ge=0, le=_MAX_SPARE_DAYS)
    """For a spare, how long to keep it: ``0`` (default) forever, a positive count that many
    days. Ignored for a reap, which never expires."""


PLEX_FORWARD_PATH = "/plex-done.html"
"""Where plex.tv sends the sign-in window when it is finished.

A static page in ``frontend/public``, served from the SPA build, whose whole job is to
close the window it is loaded in. That is the only way Reaper can shut that window: it
is opened with ``noopener`` so plex.tv cannot reach the page holding the operator's
Reaper password, which also means ``window.open`` hands back no handle to close it with.
A script-opened window may still close *itself*, so the close moves into the window.

``tests/test_repo_hygiene.py`` pins this constant against the file that has to exist for
it and against the two callers that ask for it, because a rename here is silent: the
sign-in still works, the window just stops closing.
"""


class PlexStartIn(BaseModel):
    """Where to send the sign-in window afterward. Both Plex start routes take it.

    The browser names its own origin because the server cannot. Reaper sits behind
    whatever reverse proxy the operator runs, and in development behind Vite's ``/api``
    proxy, which rewrites ``Host`` to the API's own port -- so a URL built from the
    request would forward the window to an address the operator is not on. Only the
    browser knows where it is.

    Origin only, path appended here, so this can name a host but never a target. It
    carries no secret either way: plex.tv is told where to send the window, and the token
    is never in that URL. Absent means an older cached SPA is calling, and the sign-in
    works exactly as before with the window left open.
    """

    model_config = ConfigDict(frozen=True)

    forward_origin: str | None = None

    @field_validator("forward_origin")
    @classmethod
    def _bare_http_origin(cls, value: str | None) -> str | None:
        if value is None:
            return None
        parsed = urlparse(value)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            raise ValueError("expected an http or https origin")
        if parsed.path or parsed.params or parsed.query or parsed.fragment:
            raise ValueError("expected an origin with no path")
        return value

    def forward_url(self) -> str | None:
        """The full address to hand plex.tv, or None when the caller named no origin."""
        return None if self.forward_origin is None else self.forward_origin + PLEX_FORWARD_PATH


NO_PLEX_FORWARD = PlexStartIn()
"""The body a caller that named no origin gets: an older cached SPA, or a script.

Both start routes default to it, so the sign-in works with the window left open rather
than 422-ing on a missing body. Shared safely because the model is frozen.
"""


class AboutOut(BaseModel):
    """What's running and where its data lives. Facts only, for the About page and for
    bug reports; nothing here is editable."""

    version: str
    license: str
    data_dir: str
    reaper_db_bytes: int
    """Reaper's own database: decisions, audit trail, credentials. Small and precious."""
    cache_db_bytes: int
    """The rebuildable cache: watch history, ratings, lists. Large and disposable."""


class ReleaseChangeOut(BaseModel):
    """One release the operator has not taken yet, for the what-changed modal.
    ``notes`` is the release's own changelog, GitHub-flavored markdown."""

    version: str
    url: str | None
    notes: str | None


class UpdateOut(BaseModel):
    """Whether a newer Reaper exists, on this build's channel. Advisory only: nothing
    gates on it, and a check that could not answer reads as unknown, never as an error.

    ``update_available`` is three-state: ``None`` when the check is off, unreachable,
    or the two versions cannot be ordered. The surface renders that as nothing.
    ``changes`` lists every release newer than the running one, newest first; empty
    unless ``update_available`` is ``True``, and always empty on the dev channel.
    """

    channel: Literal["release", "dev"]
    enabled: bool
    current: str
    latest: str | None
    update_available: bool | None
    url: str | None
    checked_at: datetime | None
    changes: list[ReleaseChangeOut]


class ProtectionListOut(BaseModel):
    """One protection list, for the Lists screen. Read-only.

    ``name`` is the provider's own display name, which is what the operator configured it
    from ("Sonarr tag: reaper-keep", 'Plex collection: "Never Reap"'). It arrives from Plex
    or an *arr, so a surface rendering it wraps rather than truncates (rule 139).

    ``state`` is ``lists.ListHealth``, derived server-side so this screen and the degraded
    scan notice cannot disagree about what a failed check means (rule 144). ``item_count``
    is what the stored copy still protects: a ``failing`` list with members above zero went
    on covering them, because a failed refresh leaves the previous membership in place.
    """

    slug: str
    """The stable key rows are listed by. Not shown; a display name can collide (rule 63)."""

    name: str
    source: Literal["arr_tag", "plex_collection", "plex_watchlist", "imdb"]
    """Which family this belongs to, so the screen can group rather than print one row per
    *arr instance. Two Radarrs and two Sonarrs already make four rows for the single
    protection "titles I tagged reaper-keep", and every instance added multiplies them."""

    state: Literal["working", "stale", "failing", "never_checked"]
    item_count: int
    last_checked_at: datetime | None
    """When the last SUCCESSFUL check landed. Null when none ever has."""

    error: str | None
    """What the last failed check said, verbatim from the service that refused."""

    list_id: int | None
    """Which ``ListConfig`` this membership was synced for, so the screen can render one row
    per definition and put Edit and Check now on it. Several rows share one id -- a tag list
    is synced once per *arr instance -- and it is null for a row stored before its
    definition existed, which the next successful check re-homes. Derived from the slug in
    ``lists.list_id_of``, beside the spellings, never parsed in the browser (rule 63)."""

    tags: dict[str, int] | None = None
    """A tag list's per-tag counts from the last good check, by the operator's spelling of
    each tag. Null for every other source, and for a row that has not synced since the
    counts started being recorded -- unknown, never zero."""

    server: str | None = None
    """Which *arr instance a tag list's row was read from, for the per-server counts. The
    operator named the instance on Settings; null for every other source."""

    media_types: list[Literal["movie", "tv"]] = Field(default_factory=list)
    """Which media types this row's stored members span. Empty until the first sync. The
    screen compares it against the media types a keep rule names (``policy_use``) so a rule
    covering only one side of a mixed list reads as partial cover, not full (#533)."""


class ListConfigIn(BaseModel):
    """A list the operator is adding or editing.

    ``config`` is the source's own settings and is validated per source at the service
    boundary (``services.list_config``), because each source needs different keys and a
    shape that could never match anything is refused while the operator is looking at the
    box that is empty.
    """

    name: str = Field(min_length=1, max_length=100)
    source: Literal["arr_tag", "plex_collection", "plex_watchlist", "imdb"]
    config: dict[str, Any] = Field(default_factory=dict)


class ListConfigPatch(BaseModel):
    """An edit. Every field optional: omitted means "leave it", which is why none default
    to a value (rule 1 -- an omitted field and an explicit one are different requests)."""

    name: str | None = Field(default=None, min_length=1, max_length=100)
    config: dict[str, Any] | None = None


class ListSyncIn(BaseModel):
    """Which lists to check now. Omitted means all of them."""

    list_id: int | None = Field(default=None, ge=1)


class ListSyncOut(BaseModel):
    """What one check-now pass did."""

    checked: int
    """Lists whose check landed. Stored rows, so one tag list across two *arr counts twice."""

    failed: int
    """Lists whose check failed. Each one's own error is on its row, which the screen
    refetches; this is only what the button says when it settles."""

    plex_error: str | None
    """Set when Plex could not be reached, so its collections were not checked at all and no
    row carries an error explaining why. Null when Plex answered or none is linked."""


class ListPolicyUseOut(BaseModel):
    """One keep rule naming a list, summarized for the Lists screen's "how Policy uses
    it" line."""

    media_type: Literal["movie", "tv"]
    strength: Literal["hard", "lean"]
    points: int | None = None
    """The lean's discount. Null for a hard rule, which keeps outright."""


class ListConfigOut(BaseModel):
    """A list definition: what the operator named it and where it points.

    The DEFINITION, not the membership. ``ProtectionListOut`` above is the other half --
    what that definition is currently protecting -- and the two are joined in the browser
    on ``list_id`` rather than merged here: a definition exists from the moment it is
    saved, and its membership does not exist until a sync has run.
    """

    id: int
    name: str
    """The operator's own words. Free text, so a surface rendering it wraps (rule 139)."""

    source: Literal["arr_tag", "plex_collection", "plex_watchlist", "imdb"]
    config: dict[str, Any]
    """The source's own settings, shaped per source by ``list_config._clean_config``."""

    policy_use: list[ListPolicyUseOut] = Field(default_factory=list)
    """How the policies use this list right now: one entry per keep rule naming it
    (``services.list_rules.usage``). Empty means no rule does, which the screen renders as
    a warning -- a defined list that protects nothing."""
