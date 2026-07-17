# SPDX-License-Identifier: AGPL-3.0-or-later
"""ORM models.

Conventions that are deliberate, not incidental:

* Every timestamp is timezone-aware UTC. A naive datetime anywhere in this
  application is a bug -- watch-history comparisons are the whole product, and
  an off-by-one-timezone error silently condemns or spares the wrong media.
* Credentials are stored Fernet-encrypted (see ``reaper.crypto``) in ``*_enc``
  columns. The suffix is a reminder at every call site.
"""

from __future__ import annotations

import enum

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Enum,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from reaper.db.base import Base, UtcTimestamp


class InstanceKind(enum.StrEnum):
    SONARR = "sonarr"
    RADARR = "radarr"
    TAUTULLI = "tautulli"
    SEERR = "seerr"


class AuthProvider(enum.StrEnum):
    PLEX = "plex"
    LOCAL = "local"


class Instance(Base):
    """A configured *arr / Tautulli / Seerr instance.

    Modelled one-to-many from day one: a separate 4K Sonarr/Radarr pair alongside
    the HD pair is the common setup, and retrofitting multi-instance onto a
    single-instance schema means rewriting every foreign key.
    """

    __tablename__ = "instance"
    __table_args__ = (UniqueConstraint("kind", "name"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    kind: Mapped[InstanceKind] = mapped_column(Enum(InstanceKind, native_enum=False))
    name: Mapped[str] = mapped_column(String(100))
    base_url: Mapped[str] = mapped_column(String(500))
    api_key_enc: Mapped[str] = mapped_column(Text)

    # Discovered from GET /api/v3/system/status, never hardcoded: Sonarr's
    # v5-develop ships a real /api/v5 with a different SeriesResource shape.
    api_path_prefix: Mapped[str] = mapped_column(String(20), default="/api/v3")
    detected_version: Mapped[str | None] = mapped_column(String(50), default=None)

    enabled: Mapped[bool] = mapped_column(Boolean, default=True)

    # TLS verification is on unless the operator turned it off for this one instance
    # (a self-signed server they run themselves). Off is never a silent default.
    verify_tls: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[UtcTimestamp]
    last_ok_at: Mapped[UtcTimestamp | None] = mapped_column(default=None)
    last_error: Mapped[str | None] = mapped_column(Text, default=None)


class PlexServer(Base):
    """The Plex server, discovered via OAuth rather than a pasted token.

    ``machine_identifier`` is the ``clientIdentifier`` from
    ``GET https://plex.tv/api/v2/resources``. It is the anchor of the login
    authorization check: a user may sign in only if that endpoint, queried with
    *their own* token, reports this machine id as ``owned``.
    """

    __tablename__ = "plex_server"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    machine_identifier: Mapped[str] = mapped_column(String(100), unique=True)
    name: Mapped[str] = mapped_column(String(200))

    # The chosen connection, plus every alternative, so a failed URI can be
    # re-resolved without forcing the admin back through OAuth.
    connection_uri: Mapped[str] = mapped_column(String(500))
    connections_json: Mapped[str] = mapped_column(Text, default="[]")

    token_enc: Mapped[str] = mapped_column(Text)
    owner_plex_account_id: Mapped[int] = mapped_column(Integer)

    created_at: Mapped[UtcTimestamp]
    last_ok_at: Mapped[UtcTimestamp | None] = mapped_column(default=None)


class AppUser(Base):
    """An admin of Reaper. Plex-authenticated, or a local fallback account."""

    __tablename__ = "app_user"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    provider: Mapped[AuthProvider] = mapped_column(Enum(AuthProvider, native_enum=False))

    # Set for provider=plex. The identity we check ownership against.
    plex_account_id: Mapped[int | None] = mapped_column(Integer, unique=True, default=None)
    username: Mapped[str] = mapped_column(String(100), unique=True)
    email: Mapped[str | None] = mapped_column(String(255), default=None)
    thumb_url: Mapped[str | None] = mapped_column(String(500), default=None)

    # Set for provider=local. Argon2id via pwdlib.
    password_hash: Mapped[str | None] = mapped_column(Text, default=None)

    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[UtcTimestamp]
    last_login_at: Mapped[UtcTimestamp | None] = mapped_column(default=None)

    sessions: Mapped[list[AuthSession]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )


class AuthSession(Base):
    """An opaque server-side session.

    Not a JWT. There is exactly one server, so statelessness buys nothing, while
    instant revocation -- of a stolen cookie, or of every device after a password
    change -- is worth a great deal for a tool that can delete a media library.

    Only the SHA-256 of the token is stored, so a database leak does not hand out
    live sessions.
    """

    __tablename__ = "auth_session"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("app_user.id", ondelete="CASCADE"))

    created_at: Mapped[UtcTimestamp]
    expires_at: Mapped[UtcTimestamp]
    last_seen_at: Mapped[UtcTimestamp | None] = mapped_column(default=None)
    user_agent: Mapped[str | None] = mapped_column(String(300), default=None)

    user: Mapped[AppUser] = relationship(back_populates="sessions")


class PendingPlexLogin(Base):
    """An in-flight Plex PIN login.

    The PIN is created and polled entirely by the backend. The browser never
    handles a Plex authToken -- Overseerr posts the token from the browser to its
    own API, which needlessly exposes a full-power account credential to the page.
    """

    __tablename__ = "pending_plex_login"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    pin_id: Mapped[int] = mapped_column(Integer, unique=True, index=True)
    pin_code: Mapped[str] = mapped_column(String(20))
    purpose: Mapped[str] = mapped_column(String(20))  # "setup" | "login"
    created_at: Mapped[UtcTimestamp]
    expires_at: Mapped[UtcTimestamp]


class Policy(Base):
    """An immutable, content-addressed policy body.

    **Append-only. Never UPDATE.** Editing a policy in the UI writes a *new* row
    with a new hash; the old row survives because approvals, candidates and audit
    entries point at it and must remain interpretable years later.

    ``policy_hash`` covers the semantic fields plus the schema and scorer versions,
    and deliberately excludes ``id``, ``name`` and ``created_at`` -- so renaming a
    policy does not void every pending approval, but changing a threshold does.
    """

    __tablename__ = "policy"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    policy_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    body_json: Mapped[str] = mapped_column(Text)
    """The canonical JSON. Integers only -- floats do not canonicalise, and an
    unstable hash means approvals void themselves at random."""

    media_type: Mapped[str] = mapped_column(String(10))
    name: Mapped[str] = mapped_column(String(100))
    created_at: Mapped[UtcTimestamp]


class Profile(Base):
    """A named configuration: which policy, and how much it may do.

    The caps and the grace period live here rather than on the Policy so that
    tightening a limit is always safe and never voids a pending approval. Only the
    ``active_policy_id`` pointer moves.
    """

    __tablename__ = "profile"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(100), unique=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    """Ships disabled. A profile that starts running the moment it is created is
    how a starter template deletes a library."""

    active_policy_id: Mapped[int] = mapped_column(ForeignKey("policy.id"))

    settings_json: Mapped[str] = mapped_column(Text)
    """ProfileSettings: the four caps, grace_days, require_approval."""

    created_at: Mapped[UtcTimestamp]
    updated_at: Mapped[UtcTimestamp]

    policy: Mapped[Policy] = relationship()


class AutonomyGrant(Base):
    """Permission for one profile to act without a human, for one exact policy.

    Keyed on ``policy_hash``, not on a boolean column on the profile. That is the
    whole design: **any policy edit mints a new hash, the grant no longer joins, and
    the profile silently reverts to approval-required.** Autonomy cannot be
    inherited by a policy nobody has reviewed.

    It is *earned*, not set: it requires a passing backtest and a run of clean
    supervised executions, both recorded here.

    **Not wired yet.** No route or flow creates or consults grants today -- the
    backtest that must feed ``backtest_passed`` has no route or UI, so the check
    constraints below mean no row can honestly exist until that ships. The schema is
    kept (pre-release, single migration baseline) so the earned-autonomy design stays
    enforced in the database from day one, and PLAN.md tracks the wiring as open work.
    """

    __tablename__ = "autonomy_grant"
    __table_args__ = (
        UniqueConstraint("profile_id", "policy_hash"),
        # Enforced in the database, not merely in Python: autonomy is not something
        # a stray UPDATE should be able to confer.
        CheckConstraint("clean_supervised_runs >= 3", name="min_supervised_runs"),
        CheckConstraint("backtest_passed = 1", name="backtest_required"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    profile_id: Mapped[int] = mapped_column(ForeignKey("profile.id", ondelete="CASCADE"))
    policy_hash: Mapped[str] = mapped_column(String(64), index=True)

    backtest_passed: Mapped[bool] = mapped_column(Boolean)
    clean_supervised_runs: Mapped[int] = mapped_column(Integer)

    granted_at: Mapped[UtcTimestamp]
    granted_by: Mapped[str] = mapped_column(String(100))


class Snapshot(Base):
    """A frozen evidence set.

    Everything is gathered, frozen and hashed *before* anything is scored. That is the
    whole point: a transient Sonarr timeout halfway through a scan cannot flip an
    item's fate, because every item in a run is judged against the same evidence.

    Maintainerr #3125 is the bug this prevents: "collection items flip in/out when
    Sonarr API lookups fail transiently during rule runs". An item's fate should not
    depend on network luck.
    """

    __tablename__ = "snapshot"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    created_at: Mapped[UtcTimestamp]
    policy_hash: Mapped[str] = mapped_column(String(64), index=True)

    scoring_hash: Mapped[str] = mapped_column(String(64), default="")
    """The policy's scoring behaviour -- its signals and gates -- with the thresholds
    excluded. See ``PolicyBody.scoring_hash``.

    This is what lets the simulator re-decide this snapshot honestly. Moving
    ``condemn_at`` re-compares a *stored* score against a new number, which is exact.
    Changing a weight or a gate does not: the stored scores and verdicts were produced
    by the old ones, and the new answer cannot be recovered without re-reading the
    library. The simulator compares this hash and refuses to report numbers when it
    differs, rather than reporting confident, stale ones.
    """

    horizon_at: Mapped[UtcTimestamp]
    """The earliest watch history we hold. Persisted, not computed: media older than
    this has no evidence either way, and a fresh Tautulli install would otherwise make
    the whole library look abandoned."""

    item_count: Mapped[int] = mapped_column(Integer, default=0)
    degraded: Mapped[bool] = mapped_column(Boolean, default=False)
    """A source was unreachable, or the history regressed. No run may execute against a
    degraded snapshot -- but it may still be *viewed*, so the owner can see why."""

    degraded_reason: Mapped[str | None] = mapped_column(Text, default=None)


class Candidate(Base):
    """One item's verdict under one snapshot."""

    __tablename__ = "candidate"
    __table_args__ = (UniqueConstraint("snapshot_id", "media_key"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    snapshot_id: Mapped[int] = mapped_column(ForeignKey("snapshot.id", ondelete="CASCADE"))

    media_key: Mapped[str] = mapped_column(String(100), index=True)
    """Stable identity across snapshots: "{instance_id}:{arr_id}". Deliberately NOT the
    Plex rating key, which is not stable across library rebuilds or agent migrations."""

    plex_rating_key: Mapped[int | None] = mapped_column(Integer, default=None)
    """The Plex rating key at scan time, kept so downstream features (the "Leaving Soon"
    label) can address the item in Plex without re-resolving it. Nullable: an item Plex
    has not matched has none, and it is a snapshot-time snapshot -- rating keys move across
    library rebuilds, so this is never treated as stable identity (that is media_key)."""

    poster_rating_key: Mapped[int | None] = mapped_column(Integer, default=None)
    """The Plex rating key to draw the card's poster from. For a season this is the SHOW's
    key, not the season's -- a show always has a poster, whereas many seasons do not (the
    proxy 404s and the card falls back to a placeholder). Null for a movie, whose own
    ``plex_rating_key`` is its poster. Display only; never identity or a gate."""

    title: Mapped[str] = mapped_column(String(500))
    media_type: Mapped[str] = mapped_column(String(10))
    size_bytes: Mapped[int] = mapped_column(Integer, default=0)

    # --- display fields, captured at scan time from data already in hand -----------
    # These come off the *arr payload (and Seerr) during the scan, so the review queue
    # can show a poster, a plain-English blurb and a "requested by" without a second
    # sweep of Tautulli or a per-item fetch. All nullable: an item may have no poster,
    # no overview, or no matching request, and none of that changes its verdict.
    year: Mapped[int | None] = mapped_column(Integer, default=None)
    summary: Mapped[str | None] = mapped_column(Text, default=None)
    """A short description of the show or film -- the *arr's ``overview``."""

    poster_url: Mapped[str | None] = mapped_column(String(1000), default=None)
    """A poster image URL (the *arr's remote TMDb cover). Display only; may be absent."""

    requested_by: Mapped[str | None] = mapped_column(String(200), default=None)
    """Who asked for this via Seerr, if anyone. Never a gate -- see services.requested_by."""

    genres_json: Mapped[str | None] = mapped_column(Text, default=None)
    """The item's genres at scan time, as a JSON array. Feeds the rule editors' value
    suggestions (GET /api/vocabulary/values); suggestion only, never a verdict input."""

    quality: Mapped[str | None] = mapped_column(String(100), default=None)
    """The file's quality name at scan time (e.g. "Bluray-1080p"), same purpose."""

    tmdb_id: Mapped[int | None] = mapped_column(Integer, default=None)
    """The TMDb id at scan time: the movie's, or the SHOW's for a season row. Radarr's
    web routes and Seerr's item pages key on it, so it is what "open this in Radarr /
    Seerr / TMDb" links need -- the internal id in media_key does not resolve there.
    Display/link only; identity stays media_key."""

    imdb_id: Mapped[str | None] = mapped_column(String(20), default=None)
    """The IMDb id at scan time (the *arr's, else the Plex-matched one) -- the show's for
    a season row. What the "open on IMDb" link needs. Display/link only."""

    title_slug: Mapped[str | None] = mapped_column(String(200), default=None)
    """Sonarr's titleSlug for the show, the coordinate its web routes key on. The show's
    value is stamped on every season row. Display/link only."""

    video_resolution: Mapped[str | None] = mapped_column(String(10), default=None)
    """Canonical resolution of the file at scan time ("2160", "1080", ..., "sd"), from
    Plex's videoResolution first, else parsed from the *arr quality name. Movies only in
    v1 (show listings carry no media). Display only."""

    content_rating: Mapped[str | None] = mapped_column(String(20), default=None)
    """Plex's certification (e.g. a TV or film rating board label). Display only."""

    runtime_minutes: Mapped[int | None] = mapped_column(Integer, default=None)
    """Runtime in minutes from Plex (a show row carries its episode runtime). Display
    only."""

    ratings_json: Mapped[str | None] = mapped_column(Text, default=None)
    """The frozen external-ratings row, as canonical JSON of ints: every value stored as
    round(value_on_0_to_10 * 10) -- IMDb 5.9 -> 59, RT 77% -> 77 -- plus imdb_votes.
    Keys are reaper.ratings.RatingSource values. The imdb entry is the SAME dataset
    number the scoring signal froze, so the panel cannot show two IMDb values. Built by
    services.display_meta.build_ratings_json; display only."""

    group_key: Mapped[str | None] = mapped_column(String(100), index=True, default=None)
    """Ties rows that belong together in the queue -- every season of one show shares its
    show's key -- so the UI can collapse a show into a single expandable row. Null for a
    standalone item (a movie), which is its own group."""

    group_title: Mapped[str | None] = mapped_column(String(500), default=None)
    """The show's display title, for the collapsed group header."""

    verdict: Mapped[str] = mapped_column(String(10))  # condemn | protect | abstain
    score: Mapped[int] = mapped_column(Integer, default=0)
    """0-100, stored as an int. Floats do not canonicalise."""

    coverage_bp: Mapped[int] = mapped_column(Integer, default=0)
    """How much of the signal weight we could actually evaluate, in basis points."""

    explanation_json: Mapped[str] = mapped_column(Text)
    """The why-panel, exactly as the UI renders it and exactly as the audit log keeps
    it. One dict, two sinks -- so what the owner was shown and what actually happened
    cannot drift apart."""

    created_at: Mapped[UtcTimestamp]


class FirstFlagged(Base):
    """When an item was *first* condemned, ever.

    Lives outside the snapshot lifecycle on purpose. The grace clock starts here, and a
    transient Sonarr timeout that drops an item from one snapshot must not silently
    reset it -- otherwise an item can never age out, and the grace period becomes
    unreachable.

    Janitorr #172 is the inverse bug: starting the clock at the item's *age* means the
    entire back catalogue is already expired on day one.
    """

    __tablename__ = "first_flagged"

    media_key: Mapped[str] = mapped_column(String(100), primary_key=True)
    first_flagged_at: Mapped[UtcTimestamp]
    """Set once. Never moved forward while the item stays condemned."""

    last_seen_condemned_at: Mapped[UtcTimestamp]


class WhitelistEntry(Base):
    """An item the owner spared by hand -- "never reap this", from the review queue.

    Distinct from the *arr-tag and Plex-collection whitelists, which live in those
    systems and are re-read from them every scan. This is Reaper's *own* record, for the
    owner who wants to protect one file without round-tripping through Sonarr to tag it.

    Like ``FirstFlagged`` it lives outside the snapshot lifecycle: sparing is a durable
    decision about a file, not a property of the scan that happened to surface it. Keyed
    by the stable ``media_key`` -- never the Plex rating key, which moves.

    ``title`` is denormalised so the "spared" list reads without joining a snapshot
    (candidates are snapshot-scoped and churn); the media_key is the identity, the title
    is only ever display.
    """

    __tablename__ = "whitelist"

    media_key: Mapped[str] = mapped_column(String(100), primary_key=True)
    title: Mapped[str] = mapped_column(String(500))
    note: Mapped[str | None] = mapped_column(Text, default=None)
    """The owner's reason, shown back to them. Optional -- sparing needs no justification."""

    decision: Mapped[str] = mapped_column(String(10), default="spare")
    """``"spare"`` (never reap) or ``"reap"`` (force onto the reap list). A reap override beats
    the score-based protections, because the owner has looked and decided -- but never a hard
    safety gate (something streaming now, or a file no *arr manages), which still wins. For a
    TV show the key can be the show's (``sonarr:i:series``), which applies to all its seasons."""

    created_at: Mapped[UtcTimestamp]


class RecoveryToken(Base):
    """A single-use, short-lived login link for the anti-lockout path.

    Minted only when Reaper boots with ``REAPER_RECOVERY=true``, and printed to
    the container log -- never emailed, never shown in the UI. Obtaining it
    requires the ability to set an environment variable and read the logs, which
    is host access; so it grants an attacker nothing they could not already do
    by editing the database directly.
    """

    __tablename__ = "recovery_token"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    created_at: Mapped[UtcTimestamp]
    expires_at: Mapped[UtcTimestamp]
    # Set the moment it is redeemed. A used token is never valid again, even
    # if it is still inside its expiry window.
    used_at: Mapped[UtcTimestamp | None] = mapped_column(default=None)


class AppSetting(Base):
    """Singleton key/value config that the admin edits in the web UI.

    Distinct from ``Settings`` (environment/bootstrap concerns like the data
    directory and the Fernet key, which cannot live in the database they protect).
    """

    __tablename__ = "app_setting"

    key: Mapped[str] = mapped_column(String(100), primary_key=True)
    value_json: Mapped[str] = mapped_column(Text)
    updated_at: Mapped[UtcTimestamp]


class RunState(enum.StrEnum):
    """Where a reap run is in its lifecycle."""

    PLANNED = "planned"  # steps written, nothing sent
    EXECUTING = "executing"  # at least one step has been attempted
    COMPLETED = "completed"  # every step reached a terminal state
    ABORTED = "aborted"  # a cap, a canary failure, or the kill switch stopped it


class StepState(enum.StrEnum):
    """Where one step is in its lifecycle.

    The ordering matters: a step is journalled as ``PENDING`` when the plan is built, and
    is marked ``SENT`` and **committed** immediately before its HTTP call is dispatched --
    the executor commits every step-state change durably as it happens
    (``Executor._mark_sent`` / ``_mark_verified``), never inside a run-long transaction.
    A step found still ``SENT`` at startup was therefore in flight when the process died:
    a durable record of precisely what was outstanding, so a reconciler *could* re-probe
    the *arr to discover whether it actually landed. No such reconciler runs today -- a
    crashed run is re-planned from a fresh snapshot, not resumed -- so this is an audit
    trail, not a resume point.
    """

    PENDING = "pending"  # journalled, not yet sent
    SENT = "sent"  # request dispatched, outcome not yet confirmed
    VERIFIED = "verified"  # the world was re-read and matches what we intended
    FAILED = "failed"  # the call errored, or verification did not match
    SKIPPED = "skipped"  # a pre-flight re-check spared the item (someone watched it)


class ReapRun(Base):
    """One execution of a set of deletions: the durable record of what Reaper did.

    Bound to the exact snapshot and policy it was approved against. If either changed
    between approval and execution, the approval is void -- you cannot approve a plan and
    then have Reaper carry out a different one.
    """

    __tablename__ = "reap_run"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    snapshot_id: Mapped[int] = mapped_column(ForeignKey("snapshot.id", ondelete="RESTRICT"))
    policy_hash: Mapped[str] = mapped_column(String(64), index=True)
    """The exact policy approved. A snapshot carries this too; storing it on the run as
    well means the audit trail survives even if the snapshot is later pruned."""

    state: Mapped[RunState] = mapped_column(
        Enum(RunState, native_enum=False), default=RunState.PLANNED
    )

    # The content-bound confirmation the human actually approved, e.g. "REAP 7 ITEMS
    # 214 GB". Recomputed from the plan at execution time; a mismatch voids the run, so a
    # stale browser tab cannot replay yesterday's plan against today's library.
    approved_manifest_hash: Mapped[str] = mapped_column(String(64))
    approved_by: Mapped[str] = mapped_column(String(100))
    approved_at: Mapped[UtcTimestamp]

    started_at: Mapped[UtcTimestamp | None] = mapped_column(default=None)
    finished_at: Mapped[UtcTimestamp | None] = mapped_column(default=None)

    aborted_reason: Mapped[str | None] = mapped_column(Text, default=None)
    """Why the run stopped early. A cap is an ABORT, never a truncation: truncating makes
    what gets deleted depend on sort order, so Reaper stops the whole run instead."""

    steps: Mapped[list[ActionStep]] = relationship(
        back_populates="run", cascade="all, delete-orphan", order_by="ActionStep.id"
    )


class ActionStep(Base):
    """One mutating HTTP call, **written to the database before it is sent.**

    This is the heart of the safety model. The row exists, with its exact method, path
    and body, *before* the request leaves the process -- so a crash mid-run leaves a
    durable record of precisely what was in flight. That record is what a future
    reconciler *could* use to re-probe the *arr and converge; today it is an audit trail,
    not a resume point -- a crashed run is re-planned from a fresh snapshot, not resumed.

    The recorded ``method``/``path``/``body`` are also what the executor hands to the
    GuardedTransport as its declaration: the guard refuses any mutation that was not
    first written here, so "journalled before sent" is enforced by the transport, not
    merely intended by convention.
    """

    __tablename__ = "action_step"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("reap_run.id", ondelete="CASCADE"))

    media_key: Mapped[str] = mapped_column(String(100), index=True)
    """Which candidate this step acts on, so the journal can be read back per item."""

    ordinal: Mapped[int] = mapped_column(Integer)
    """Execution order within the run. The canary is ordinal 0 -- the single smallest
    item, executed and verified alone before anything else is allowed to proceed."""

    kind: Mapped[str] = mapped_column(String(40))
    """What sort of action: 'radarr_delete', 'sonarr_unmonitor_season', 'plex_refresh'..."""

    method: Mapped[str] = mapped_column(String(10))
    path: Mapped[str] = mapped_column(String(500))
    body_json: Mapped[str | None] = mapped_column(Text, default=None)
    """The literal request. Credentials are NOT stored here -- they are injected only at
    send time -- so the journal is safe to render in the UI and keep forever."""

    idempotency_key: Mapped[str] = mapped_column(String(64), index=True)
    """Stable across retries and restarts: it is the de-dup handle a future reconciler
    would key on so a recovered step cannot delete twice or double-apply an exclusion.
    No reconciler consumes it yet -- it is journalled now so that safety is available when
    one is built; today double-apply is prevented by re-planning a crashed run, not resuming
    it, and by the verify-after-send step being the source of truth."""

    state: Mapped[StepState] = mapped_column(
        Enum(StepState, native_enum=False), default=StepState.PENDING
    )

    verification_json: Mapped[str | None] = mapped_column(Text, default=None)
    """What we re-read after the call to confirm it landed. For a delete-with-exclusion
    this records that the exclusion is actually present: Sonarr and Radarr each accept
    the *other's* exclusion parameter and return 200 while doing nothing, so a 200 is
    not evidence -- the re-read is."""

    error: Mapped[str | None] = mapped_column(Text, default=None)

    created_at: Mapped[UtcTimestamp]
    sent_at: Mapped[UtcTimestamp | None] = mapped_column(default=None)
    verified_at: Mapped[UtcTimestamp | None] = mapped_column(default=None)

    run: Mapped[ReapRun] = relationship(back_populates="steps")
