# SPDX-License-Identifier: AGPL-3.0-or-later
"""ORM models.

Conventions that are deliberate:

* Every timestamp is timezone-aware UTC. A naive datetime anywhere in this
  application is a bug: watch-history comparisons are the whole product, and an
  off-by-one-timezone error can silently mark the wrong media for deletion, or
  spare the wrong media from it.
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
    false,
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

    Modeled one-to-many from day one: a separate 4K Sonarr/Radarr pair alongside
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

    # The address the UI's jump links open, when it differs from the one Reaper connects to:
    # an operator reaches this service in the browser at a public address (a reverse proxy,
    # a domain), while Reaper talks to it over a LAN IP. Display only: nothing sends a
    # request to this address, so it needs no TLS verification and no key. Nullable and NULL
    # by default, since the additive migration only adds a nullable column. NULL means "no
    # external address," so links fall back to base_url and nothing changes for an operator
    # who leaves it blank (services.deep_links via api.review._deep_links). Plex has its own
    # web_url and is not an Instance, so it is untouched here.
    external_url: Mapped[str | None] = mapped_column(String(500), default=None)

    # The API path every request to this instance is built on. Held against the day an
    # *arr serves a different one: Sonarr's v5-develop ships a real /api/v5 with a
    # different SeriesResource shape. Passed to the client by both the scan and Test
    # Connection, so the two always probe the same path. Nothing writes this column
    # today: it only ever holds its default, and no UI or probe derives it.
    #
    # It is deliberately not sent to the browser. An unwritable field on the wire tells
    # an operator only a constant, never anything they could act on: the SPA typed it, no
    # component read it, and no route could change it. Retiring the column would need a
    # migration, and the schema baseline is frozen, so the column stays and only the wire
    # copy went. Wiring it later needs a writer on `instances.update_instance` (which is
    # where the connection-test result is cleared, so it joins that trio) and a per-kind
    # default in the client, because `clients/arr.py` reads `api_path_prefix or
    # self.default_prefix`: a stored "/api/v3" is truthy, so handing it to Seerr would
    # override Seerr's own /api/v1. Tautulli and Seerr are not passed the stored value
    # today, and that is load-bearing.
    api_path_prefix: Mapped[str] = mapped_column(String(20), default="/api/v3")
    detected_version: Mapped[str | None] = mapped_column(String(50), default=None)

    enabled: Mapped[bool] = mapped_column(Boolean, default=True)

    # TLS verification is on unless the operator turned it off for this one instance
    # (a self-signed server they run themselves). Off is never a silent default.
    verify_tls: Mapped[bool] = mapped_column(Boolean, default=True)

    # When Reaper deletes through this instance, ask the *arr to add an import (list)
    # exclusion so a list cannot re-add the title and re-download it. Off by default: the
    # operator opts in per instance. Wired only for Radarr movie deletes (see
    # planner._movie_steps and executor._send_movie). A Sonarr prune removes seasons, not
    # a whole series, so no Sonarr call carries the exclusion, and the setting stays
    # stored but inert there; the edit form says so. server_default so the additive
    # migration can add this NOT NULL column to an existing populated table (SQLite
    # requires a default), and existing rows backfill to "off".
    add_import_exclusion: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=false()
    )

    # HD/4K split-library disambiguation. A JSON object mapping this instance's root
    # folder paths to the Plex library each one lands in, e.g. {"/tv": "TV", "/tv-4k": "TV
    # 4K"}. When one id names the same title in two libraries, the copy in the mapped
    # library is the one bound. Nothing here ever expands a match, only narrows an
    # already-ambiguous id (engine.identity._narrow_among_id_hits). Nullable and NULL by
    # default, so the additive migration only adds a nullable column, and every existing
    # instance reads as "no map," which keeps the current behavior of leaving a
    # duplicated show alone.
    plex_library_map: Mapped[str | None] = mapped_column(Text, default=None)

    # Multi-Seerr requester attribution (SEERR rows only). A JSON object mapping this
    # Seerr portal's own services to the Reaper Sonarr/Radarr instance each one adds
    # media to, keyed "{kind}:{serviceId}" because Seerr numbers Sonarr and Radarr
    # services separately, both starting from 0, e.g. {"sonarr:0": 7, "radarr:0": 8}. A
    # Seerr request carries the *arr's item id (externalServiceId) and the portal-local
    # serviceId, so this map resolves serviceId to a Reaper instance, which lets
    # "requested by" bind the exact copy (main vs restricted library) a person asked for,
    # rather than the loose tmdb/tvdb union across every copy
    # (services.requested_by.build_map). Display only, never a gate. Nullable and NULL by
    # default, so the additive migration only adds a nullable column, and every existing
    # Seerr reads as "no map," which is today's union.
    service_instance_map: Mapped[str | None] = mapped_column(Text, default=None)

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

    # Verify the server's TLS certificate (mirrors Instance.verify_tls). Off is a
    # deliberate operator choice for a self-signed HTTPS server, never a silent default.
    verify_tls: Mapped[bool] = mapped_column(Boolean, default=True)

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

    Not a JWT. There is exactly one server, so a stateless token buys nothing, while
    instant revocation, of a stolen cookie or of every device after a password change,
    matters a great deal for a tool that can delete a media library.

    Only the SHA-256 of the token is stored, so a database leak does not hand out live
    sessions.
    """

    __tablename__ = "auth_session"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("app_user.id", ondelete="CASCADE"))

    created_at: Mapped[UtcTimestamp]
    expires_at: Mapped[UtcTimestamp]
    last_seen_at: Mapped[UtcTimestamp | None] = mapped_column(default=None)
    user_agent: Mapped[str | None] = mapped_column(String(300), default=None)

    # This session was opened by redeeming a recovery code, so its holder proved host
    # access but not knowledge of the admin password. It is what lets Settings -> Security
    # set a new password without the current one: an operator who has forgotten the
    # password has nothing to type there, and recovery exists for exactly that. Spent on
    # first use: ``api.settings.set_admin_password`` clears the mark in the same
    # transaction that writes the new hash, so the elevated permission never outlives the
    # reset it was for. server_default so the additive migration can add this NOT NULL
    # column to an existing populated table (SQLite requires a default), and every
    # session predating it backfills to "false," the fail-closed reading: still needs the
    # current password.
    via_recovery: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())

    user: Mapped[AppUser] = relationship(back_populates="sessions")


class PendingPlexLogin(Base):
    """An in-flight Plex PIN login.

    The PIN is created and polled entirely by the backend. The browser never handles a
    Plex authToken: Overseerr posts the token from the browser to its own API, which
    needlessly exposes a full-power account credential to the page.

    Only the pin id is stored, because only the id is polled. The human-readable code
    once rode along in a column nothing ever read back: this flow hands the operator an
    auth URL rather than a code to type, so the code had no reader. That attribute has
    since been removed.
    """

    __tablename__ = "pending_plex_login"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    pin_id: Mapped[int] = mapped_column(Integer, unique=True, index=True)
    # plex_link.PinPurpose: "login" | "link". Read the comment there before changing a
    # value; each poller filters on its own, and that filter is the fence between them.
    purpose: Mapped[str] = mapped_column(String(20))
    created_at: Mapped[UtcTimestamp]
    expires_at: Mapped[UtcTimestamp]


class Policy(Base):
    """An immutable, content-addressed policy body.

    Append-only. Never updated. Editing a policy in the UI writes a new row; the old row
    survives because approvals, candidates and audit entries point at it and must remain
    interpretable years later.

    ``policy_hash`` covers the semantic fields plus the schema and scorer versions, and
    deliberately excludes ``id``, ``name`` and ``created_at``, so renaming a policy does
    not void every pending approval, but changing a threshold does. The hash identifies
    content, not a row, and is deliberately not unique here: reverting to an earlier
    policy appends a fresh row carrying the earlier hash, so the newest row per media type
    is always the one in force, and the table reads as the complete save history. Rows
    sharing a hash are byte-identical, since the hash is over canonical JSON, so anything
    pointing at a hash stays unambiguous.
    """

    __tablename__ = "policy"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    policy_hash: Mapped[str] = mapped_column(String(64), index=True)
    body_json: Mapped[str] = mapped_column(Text)
    """The canonical JSON. Integers only: floats do not canonicalize, and an
    unstable hash means approvals void themselves at random."""

    media_type: Mapped[str] = mapped_column(String(10))
    name: Mapped[str] = mapped_column(String(100))
    created_at: Mapped[UtcTimestamp]


class Profile(Base):
    """A named configuration: which policy, and how much it may do.

    The caps and the grace period live here rather than on the Policy, so tightening a
    limit is always safe and never voids a pending approval.

    Two attributes are unused and scheduled for removal: ``enabled``, which nothing
    reads, and ``active_policy_id``, which points at a policy row while the policy
    actually in force is resolved by pure recency. Their columns survive one release so a
    rollback still works, and revision ``e6f7a8b9c0d1`` is what lets an INSERT omit them.
    """

    __tablename__ = "profile"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(100), unique=True)

    settings_json: Mapped[str] = mapped_column(Text)
    """ProfileSettings: the four caps, caps_enabled, grace_days, the unknown-size allowance."""

    created_at: Mapped[UtcTimestamp]
    updated_at: Mapped[UtcTimestamp]


class AutonomyGrant(Base):
    """Permission for one profile to act without a human, for one exact policy.

    Keyed on ``policy_hash``, not on a boolean column on the profile. That is the whole
    design: any policy edit mints a new hash, the grant no longer joins, and the profile
    silently reverts to approval-required. Autonomy cannot be inherited by a policy
    nobody has reviewed.

    A grant is earned, not set: it records a bar that was cleared, never a switch
    somebody flipped.

    This table is not wired yet, and one of its two bars has no way to be measured
    today. ``clean_supervised_runs`` can be measured from the journal.
    ``backtest_passed`` cannot: the replay engine that would have produced it was
    deleted rather than wired, and its successors answer different questions, so nothing
    can honestly write this column yet.

    Nothing constructs a grant today, which is the only reason the table stays empty:
    the CHECK constraint below only refuses a row that admits ``backtest_passed = 0``,
    and would accept one that simply asserts ``1``. Whoever wires this feature must
    decide what replaces the missing measurement rather than fake it: passing ``True``
    for a check nothing performed would grant a profile unattended deletion on a policy
    nobody measured, and the database would not stop them. Dropping the column is part
    of that future work.

    The schema is kept because this feature is still planned, and this docstring is the
    only full record of the design.
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

    Everything is gathered, frozen and hashed before anything is scored, so a transient
    Sonarr timeout halfway through a scan cannot flip an item's fate: every item in a run
    is judged against the same evidence. A tool without this guarantee can let collection
    items flip in and out of a run when an API lookup fails partway through a scan. An
    item's fate should not depend on network luck.
    """

    __tablename__ = "snapshot"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    created_at: Mapped[UtcTimestamp]
    policy_hash: Mapped[str] = mapped_column(String(64), index=True)

    scoring_hash: Mapped[str] = mapped_column(String(64), default="")
    """The policy's scoring behavior, its signals and gates, with the thresholds
    excluded. See ``PolicyBody.scoring_hash``.

    This is what lets the simulator re-decide this snapshot honestly. Moving
    ``condemn_at`` re-compares a stored score against a new number, which is exact.
    Changing a weight or a rating bar is exact too, because each Candidate freezes its
    Facts (``facts_json``). Only a change to what the scan gathered, the fields the
    evidence hash covers, truly needs a fresh scan.
    """

    evidence_hash: Mapped[str | None] = mapped_column(String(64), default=None)
    """The policy fields that decide what evidence the scan gathers and freezes: the
    popularity window and the media type. The keep-tags were here until the tags a title
    carries became gathered evidence (``Facts.on_lists``), and the season-pruning guard
    until the scan began freezing its inputs per show; ``PolicyBody.evidence_hash`` is the
    one place that set is declared, so read it there rather than trusting this list. Two
    policies sharing this produce the same frozen Facts for every item, so the simulator
    may replay the real engine over ``Candidate.facts_json`` and get an exact verdict for
    any change outside these fields (weights, rating bars, custom rules). When it
    differs, the frozen evidence is stale and a fresh scan is required. See
    ``PolicyBody.evidence_hash``."""

    list_config_hash: Mapped[str | None] = mapped_column(String(64), default=None)
    """The protection lists this scan gathered membership from
    (``services.list_config.fingerprint``). The lists are not policy, so the hash above
    cannot carry them, and every tier of the simulator reads evidence derived from them:
    the replay through the frozen ``Facts.on_lists``, the threshold path through the stored
    verdict that fact produced. When it differs from the registry as it stands now, the
    operator changed which titles their keep rules protect, and the panel refuses instead
    of reporting numbers that predate the change.

    ``None`` on a snapshot written before this column, and read as unknown rather than as
    "no lists": it matches no fingerprint, so the panel refuses until the next scan. That
    is the same one-scan cost ``PolicyBody.evidence_hash`` documents, and it heals the
    same way."""

    horizon_at: Mapped[UtcTimestamp]
    """The earliest watch history we hold. Persisted, not computed: media older than
    this has no evidence either way, and a fresh Tautulli install would otherwise make
    the whole library look abandoned."""

    item_count: Mapped[int] = mapped_column(Integer, default=0)
    degraded: Mapped[bool] = mapped_column(Boolean, default=False)
    """A source was unreachable, or the history regressed. No run may execute against a
    degraded snapshot, but it may still be viewed, so the owner can see why."""

    degraded_reason: Mapped[str | None] = mapped_column(Text, default=None)

    degraded_doc: Mapped[str | None] = mapped_column(Text, default=None)
    """The in-app help page that explains what to do about the reason above, by its
    ``frontend/src/docs/registry.ts`` id. ``NULL`` for a degradation with no page, which is most
    of them: an unreachable Radarr needs no guide.

    Stored rather than worked out when the notice renders, because the notice renders from a
    stored scan and the reason is prose by then. Reading the cause back out of that string
    would couple this code to operator copy that will be reworded."""

    watch_blind_items: Mapped[int | None] = mapped_column(Integer, default=None)
    """How many items this scan found had watch history it could no longer read
    (``services.watch_evidence``). Counted at scan time and stored, rather than derived
    later by matching the reason text on each stored explanation, which would couple this
    code to operator copy that will be reworded.

    This counts what was measured, never what was decided. Such an item is normally
    held, since three gates block on the ``Unknown`` this produces, but the operator can
    switch each of them off, and nothing here consults the verdict. Copy calling these
    items held back or kept must not claim a protection this number does not actually
    compute.

    ``NULL`` means "not recorded," which is every snapshot taken before this column
    existed. Read it as unknown, never as zero."""

    collection_sizes_json: Mapped[str | None] = mapped_column(Text, default=None)
    """Every Plex collection this scan saw, name to Plex's own member count, as a JSON
    object; see ``services.snapshot._collection_membership``. Feeds the collection
    picker's counts and the collection screen's header; navigation only, never a verdict
    input. NULL means no collection was read this scan, whether because none exist or
    because the read failed. The two are indistinguishable on purpose, since a failed
    collection read costs a missing chip and nothing else."""


class SizeSource(enum.StrEnum):
    """Which measurement ``Candidate.size_bytes`` holds.

    Load-bearing. The executor's growth interlock compares the frozen size
    against a live re-read, and without provenance it cannot tell whether the two measure
    the same thing; a size compared against a different quantity reads a real growth as a
    shrink. See ``executor._measures``, whose allow-lists are exhaustive and fail closed,
    which is why the members nothing writes are kept rather than deleted.

    Never exposed on the wire: the UI says "Size unknown", never a source key.
    """

    SONARR_FILES = "sonarr-files"
    """Summed episode files for one season, gathered file by file. Never written: Sonarr's
    season statistic below is already this same sum."""

    SONARR = "sonarr"
    """One season's episode files, summed by Sonarr. ``statistics.sizeOnDisk`` is a ``SUM``
    over the ``EpisodeFiles`` table, not a folder walk, so it is the quantity a season
    delete frees."""

    RADARR = "radarr"
    """One movie's tracked file rows, summed by Radarr. ``sizeOnDisk`` is likewise a ``SUM``
    over ``MovieFiles``. The delete removes the movie folder, which holds bytes no row
    tracks, so this understates what a delete frees. Measured always in that direction:
    of 221 sampled folders, every one held more than Radarr reported, by 0.02% at the
    median and 44% at the worst (see docs/LEARNINGS.md). Accepted as a close lower bound,
    which is why ``ProfileSettings``'s caps read as bounds rather than equalities."""

    RADARR_FILE = "radarr-file"
    """The movie file alone. Never written, and display only if it ever is: it reads one
    file where ``RADARR`` sums every row, so it can measure less, and a size that
    under-counts a byte cap stops the cap firing, which deletes more than was allowed."""

    PLEX = "plex"
    """The matched Plex listing's file. Never written, exactly as ``RADARR_FILE``."""


class Candidate(Base):
    """One item's verdict under one snapshot."""

    __tablename__ = "candidate"
    __table_args__ = (UniqueConstraint("snapshot_id", "media_key"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    snapshot_id: Mapped[int] = mapped_column(ForeignKey("snapshot.id", ondelete="CASCADE"))

    media_key: Mapped[str] = mapped_column(String(100), index=True)
    """Stable identity across snapshots: "{instance_id}:{arr_id}". Deliberately not the
    Plex rating key, which is not stable across library rebuilds or agent migrations."""

    plex_rating_key: Mapped[int | None] = mapped_column(Integer, default=None)
    """The Plex rating key at scan time, kept so downstream features (the "Leaving Soon"
    label) can address the item in Plex without re-resolving it. Nullable: an item Plex
    has not matched has none, and this is a snapshot-time value only. Rating keys move
    across library rebuilds, so this is never treated as stable identity (that is
    media_key)."""

    poster_rating_key: Mapped[int | None] = mapped_column(Integer, default=None)
    """The Plex rating key to draw the card's poster from. For a season this is the show's
    key, not the season's: a show always has a poster, whereas many seasons do not (the
    proxy 404s and the card falls back to a placeholder). Null for a movie, whose own
    ``plex_rating_key`` is its poster. Display only; never identity or a gate."""

    title: Mapped[str] = mapped_column(String(500))
    media_type: Mapped[str] = mapped_column(String(10))
    size_bytes: Mapped[int | None] = mapped_column(Integer, default=None)
    """What deleting this item would free, in bytes. NULL means Reaper could not measure it,
    which is not the same as zero: no file worth deleting is genuinely empty.

    For a season this is exact. For a movie it is a close lower bound: Radarr counts the
    file rows it tracks and the delete takes the whole folder, so untracked extras are
    freed without being counted here (``SizeSource.RADARR``). Every "would free" string
    downstream is copied from this line, so read it as the bound it is.

    Nullable rather than a sentinel value, because a sentinel is the same bug with a
    nicer number: every sum would accept it and quietly produce a wrong total, whereas a
    NULL makes an unsafe call site raise. This mirrors the same choice already made for
    ``watch_event.watched_status``.

    ``size_source`` below says which measurement this is, and is NULL exactly when this
    is. Both scan paths write a NULL when nothing reports a size. ``planner.build_plan``
    holds such an item out of a delete, and ``executor._may_send_unmeasured`` refuses it
    again per item at send time, unless the operator has raised
    ``ProfileSettings.max_unmeasured_per_run``, which admits a bounded number of them
    deliberately."""

    size_source: Mapped[str | None] = mapped_column(String(16), default=None)
    """Which measurement ``size_bytes`` holds, as a ``SizeSource`` value. NULL means no
    source reported one. Kept as a string column rather than a native enum for the same
    reason as every other enum here: SQLite has none, and a stored value this build does
    not recognize must be handled by the reader, not rejected by the driver.

    Read by the executor to compare like with like; never sent to the browser."""

    # --- display fields, captured at scan time from data already in hand -----------
    # These come off the *arr payload (and Seerr) during the scan, so the review queue
    # can show a poster, a plain-English blurb and a "requested by" without a second
    # sweep of Tautulli or a per-item fetch. All nullable: an item may have no poster,
    # no overview, or no matching request, and none of that changes its verdict.
    year: Mapped[int | None] = mapped_column(Integer, default=None)
    summary: Mapped[str | None] = mapped_column(Text, default=None)
    """A short description of the show or film, the *arr's ``overview``."""

    requested_by: Mapped[str | None] = mapped_column(String(200), default=None)
    """Who asked for this via Seerr, if anyone. Never a gate; see services.requested_by."""

    genres_json: Mapped[str | None] = mapped_column(Text, default=None)
    """The item's genres at scan time, as a JSON array. Feeds the rule editors' value
    suggestions (GET /api/vocabulary/values); suggestion only, never a verdict input."""

    collections_json: Mapped[str | None] = mapped_column(Text, default=None)
    """This item's Plex collection names at scan time, as a JSON array, sorted smallest
    collection first (ties alphabetical); see ``services.snapshot._collection_membership``.
    NULL means "not recorded for this scan," never "in no collection": a scan taken before
    this column existed and a scan whose Plex collection read failed are indistinguishable
    on purpose. Navigation only: no gate, signal, or policy field reads it, and it is
    never a verdict input."""

    quality: Mapped[str | None] = mapped_column(String(100), default=None)
    """The file's quality name at scan time (e.g. "Bluray-1080p"), same purpose."""

    tmdb_id: Mapped[int | None] = mapped_column(Integer, default=None)
    """The TMDb id at scan time: the movie's, or the show's for a season row. Radarr's
    web routes and Seerr's item pages key on it, so it is what "open this in Radarr /
    Seerr / TMDb" links need; the internal id in media_key does not resolve there.
    Display/link only; identity stays media_key."""

    imdb_id: Mapped[str | None] = mapped_column(String(20), default=None)
    """The IMDb id at scan time (the *arr's, else the Plex-matched one): the show's for
    a season row. What the "open on IMDb" link needs. Display/link only."""

    tvdb_id: Mapped[int | None] = mapped_column(Integer, default=None)
    """The TVDb id at scan time: the show's, stamped on every season row. NULL for a movie
    (Radarr is tmdb-native) and for older rows scanned before this column existed. Sonarr is
    tvdb-native, so this is often the only id a show and its Seerr request reliably share;
    Scales joins requests to candidates on it (see services.fairness). Display/join, not a
    verdict input; identity stays media_key."""

    title_slug: Mapped[str | None] = mapped_column(String(200), default=None)
    """Sonarr's titleSlug for the show, the coordinate its web routes key on. The show's
    value is stamped on every season row. Display/link only."""

    show_status: Mapped[str | None] = mapped_column(String(12), default=None)
    """Whether the show is finished, as one of "ended" / "continuing" / "unknown." The
    show's value is stamped on every season row. Null means the question does not apply
    (a movie), which is why this is a string and not a nullable bool: a bool only has
    room for two answers, and this field must carry four states. "unknown" is its own
    value because Sonarr reporting no status at all must never render as a definite
    "still going"; the UI draws it in the "we could not check" treatment. "continuing" is
    only the stored key; the UI label is "Still going," since a show that has not ended
    may also be one that has not started. Display only, never a verdict input (the
    custom-rule field reads the frozen Facts, not this column)."""

    video_resolution: Mapped[str | None] = mapped_column(String(10), default=None)
    """Canonical resolution of the file at scan time ("2160", "1080", ..., "sd"), from
    Plex's videoResolution first, else parsed from the *arr quality name. Movies only in
    v1 (show listings carry no media). Display only."""

    content_rating: Mapped[str | None] = mapped_column(String(20), default=None)
    """Plex's certification (e.g. a TV or film rating board label). Display only."""

    library_title: Mapped[str | None] = mapped_column(String(200), default=None)
    """The Plex library (section) the item lives in, as the operator named it: the show's
    for a season row, stamped on every season in the same scan. Captured at scan time from
    the section listing already in hand (services.library_index). Display and filter only;
    never identity or a verdict input. NULL for an item Plex could not place (unmatched, a
    sweep that did not list it, or a pre-rescan row), and then the UI shows no library
    chip."""

    runtime_minutes: Mapped[int | None] = mapped_column(Integer, default=None)
    """Runtime in minutes from Plex (a show row carries its episode runtime). Display
    only."""

    ratings_json: Mapped[str | None] = mapped_column(Text, default=None)
    """The frozen external-ratings row, as canonical JSON of ints: every value stored as
    round(value_on_0_to_10 * 10), so IMDb 5.9 becomes 59 and RT 77% becomes 77, plus
    imdb_votes. Keys are reaper.ratings.RatingSource values. The imdb entry is the same
    dataset number the scoring signal froze, so the panel cannot show two different IMDb
    values. Built by services.display_meta.build_ratings_json; display only."""

    group_key: Mapped[str | None] = mapped_column(String(100), index=True, default=None)
    """Ties rows that belong together in the queue: every season of one show shares its
    show's key, so the UI can collapse a show into a single expandable row. Null for a
    standalone item (a movie), which is its own group."""

    group_title: Mapped[str | None] = mapped_column(String(500), default=None)
    """The show's display title, for the collapsed group header."""

    verdict: Mapped[str] = mapped_column(String(10))  # condemn | protect | abstain
    score: Mapped[int] = mapped_column(Integer, default=0)
    """0-100, stored as an int. Floats do not canonicalize."""

    coverage_bp: Mapped[int] = mapped_column(Integer, default=0)
    """How much of the total signal weight could actually be checked, in basis points
    (hundredths of a percent)."""

    explanation_json: Mapped[str] = mapped_column(Text)
    """The explanation panel's content, exactly as the UI renders it and exactly as the
    audit log keeps it. One dict, two destinations, so what the owner was shown and what
    actually happened can never drift apart."""

    facts_json: Mapped[str | None] = mapped_column(Text, default=None)
    """The frozen scoring inputs for this item: its full three-state ``Facts`` plus the
    season-pruning guard result, serialized by ``engine.facts_codec``. This is what lets
    the simulator replay the real engine under an edited policy with zero API calls, exact
    for any change the evidence hash permits. Nullable for snapshots taken before the field
    existed; the simulator falls back to "needs a fresh scan" when it is absent."""

    created_at: Mapped[UtcTimestamp]


class SeasonPruneEvidence(Base):
    """What one show's season plan was decided from, frozen with the snapshot.

    ``Candidate.facts_json`` freezes the season guard's output: a ``GateResult`` saying
    which seasons were kept and why. That is enough to explain the scan but not enough to
    re-decide it. ``season_pruning.plan_series_prune``'s inputs are per-show (Sonarr's
    season statistics, who is part-way through, each season's watcher count and the bound
    on it), and never reached ``Facts``, which carries only ``season_rank``, ``size_bytes``
    and the season's own counts. So every season rule left a blank in the policy
    simulator, on the densest card in the editor.

    One row per show per snapshot, keyed by the show key every one of its seasons carries
    (``sonarr:{instance}:{series}``, ``whitelist.show_key`` of any season's media_key). Per
    show rather than per season deliberately: the bundle is per show, and copying it onto
    each season row would multiply the frozen-evidence cost of a TV library by its seasons
    per show for nothing (``docs/LEARNINGS.md``, "What a frozen snapshot actually costs").

    Swept with its snapshot: ``services.retention`` deletes ``Snapshot`` rows and the FK
    below cascades, exactly as ``Candidate`` does.
    """

    __tablename__ = "season_prune_evidence"
    __table_args__ = (UniqueConstraint("snapshot_id", "group_key"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    snapshot_id: Mapped[int] = mapped_column(ForeignKey("snapshot.id", ondelete="CASCADE"))

    group_key: Mapped[str] = mapped_column(String(100))
    """The show key, shared by every season of the show and equal to ``Candidate.group_key``
    for those rows.

    Carries no index of its own: the only read is ``simulate._season_payloads``' ``WHERE
    snapshot_id = ?``, which the unique constraint above already covers on its leading column,
    and nothing filters a show key across snapshots. A second B-tree here would be written
    once per show per scan to serve nobody, in the table whose per-row cost the paragraph
    above is about."""

    payload_json: Mapped[str] = mapped_column(Text)
    """``services.season_evidence.to_dict`` of the show's ``SeasonPruneInput``.

    Cost grows with viewers times seasons, since three of its members are
    per-user-per-season maps: measured at roughly 1.7 KB for a five-season show with five
    viewers, growing to about 9 KB for a ten-season show with 25 viewers (see
    docs/LEARNINGS.md, "What frozen season evidence costs"). Growth is bounded by the same
    30-snapshot sweep as everything else here.

    A snapshot with no row for a show is the honest "unknown": the simulator refuses the
    season card rather than replaying a partial bundle, and the next scan reads one back
    in. A snapshot taken before this table existed is covered twice over, since the same
    change also widened ``PolicyBody.evidence_hash``, which refuses every edit until the
    snapshot is re-scanned."""


class FirstFlagged(Base):
    """When an item was first flagged for deletion, ever.

    Lives outside the snapshot lifecycle on purpose. The grace clock starts here, and a
    transient Sonarr timeout that drops an item from one snapshot must not silently reset
    it, or an item could never age out and the grace period would become unreachable.
    Starting the clock from the item's age instead would have the opposite problem: the
    entire back catalog would already be expired on day one.
    """

    __tablename__ = "first_flagged"

    media_key: Mapped[str] = mapped_column(String(100), primary_key=True)
    first_flagged_at: Mapped[UtcTimestamp]
    """Set once. Never moved forward while the item stays flagged for deletion."""

    last_seen_condemned_at: Mapped[UtcTimestamp]


class WatchHighWater(Base):
    """The most watch evidence Reaper has ever measured for an item.

    A Plex rating key is not stable. Let a file go away and come back, and Plex issues a
    new one, while Tautulli keeps every earlier play filed under the old one. This is
    verified against Tautulli's source: only ``update_metadata_details``, reached by a
    human clicking "Fix Metadata," ever moves them, and no scheduled task calls it. Reaper
    reads the mirror by the key the item carries now, so those plays go invisible and the
    item reads as never watched, which pushes its deletion score as high as it can go on a
    title somebody actually watched.

    Nothing in the read path can tell that apart from a genuinely unwatched item: both are
    simply "no rows for this key." So the high-water mark is kept here instead. All-time
    watch evidence can only grow (the mirror never deletes, and a Tautulli prune is caught
    separately by ``history_sync._check_regression``), so a count that falls to zero from
    a positive one is a transition no library can make, and that is the signal.
    ``services.watch_evidence`` does the comparing.

    Outside the snapshot lifecycle on purpose: comparing against only the previous
    snapshot would let the first blind scan write zero as the new baseline, after which 0
    to 0 is no longer a fall and the blindness is never noticeable again.

    Keyed by the stable ``media_key``, never the Plex rating key, which is the thing that
    moved.
    """

    __tablename__ = "watch_high_water"

    media_key: Mapped[str] = mapped_column(String(100), primary_key=True)

    watchers_all_time: Mapped[int] = mapped_column(Integer, default=0)
    """The largest all-time distinct-watcher count ever measured for this item. Only ever
    raised, so it survives a scan that read the item blind."""

    last_played_at: Mapped[UtcTimestamp | None] = mapped_column(default=None)
    """The latest play ever measured. ``NULL`` means no play has ever been seen, which is
    the ordinary state of a never-watched item and never fires the check."""

    updated_at: Mapped[UtcTimestamp]
    """When the mark last moved. Diagnostic only: nothing reads it to make a decision."""


class LibrarySeen(Base):
    """Every Plex listing Reaper has ever seen for one external id, and whether it came back.

    A file that leaves the library and returns gets a new Plex rating key, while the
    earlier one stops existing. This table is the memory that makes a return detectable:
    without a record of which keys an id has held, a fresh key is indistinguishable from
    the only key it ever had.

    ``WatchHighWater`` above sets the pattern for every property here. Outside the
    snapshot lifecycle, keyed on something stable rather than on the Plex key that moves,
    upserted once per scan, never pruned. Roughly one row per title.

    Keyed on an external id, not on ``media_key``. A ``media_key`` is
    ``{radarr|sonarr}:{instance}:{arr_id}``, and deleting a movie removes Radarr's row, so
    a re-add gets a fresh internal id and the old key is retired. That is true whether
    Reaper or the operator did the deleting, which is exactly the case this table exists
    to notice.

    ``rating_keys`` is a set, and that is a measured requirement, not caution. About one
    movie entry in 150 shares its TMDb id with a second \\*arr entry, one per copy, each
    bound to a different Plex listing (see docs/LEARNINGS.md). Storing only the last key
    seen would read the second copy's key as a change on every scan and hold both copies
    forever.

    Written only on a confident Plex bind. No bind, no write, so a Plex outage, a partial
    \\*arr, or an item that did not resolve leaves this table untouched rather than
    recording an absence. Absence is never an input here: a return is only ever found by
    comparing two keys Reaper actually read, so missing data can delay a detection to the
    next scan but can never manufacture one.
    """

    __tablename__ = "library_seen"

    id_key: Mapped[str] = mapped_column(String(100), primary_key=True)
    """The item's external identity, media kind first: ``movie:tmdb:12345``,
    ``tv:tvdb:678``, ``tv:tvdb:678:s3``.

    The kind leads because movie and TV TMDb ids share one integer space, so a bare
    ``tmdb:12345`` can name two different titles. Built by
    ``services.library_seen.id_key``, off the same id ladders the Plex resolver binds on
    (``identity.MOVIE_ID_PRIORITY`` / ``SHOW_ID_PRIORITY``)."""

    rating_keys_json: Mapped[str] = mapped_column(Text, default="[]", server_default="[]")
    """Every Plex rating key ever recorded for this id, as a JSON array of ints. Only ever
    added to. An unreadable value is read as an empty set by
    ``services.library_seen.recall_all``, which costs a detection and never invents one."""

    first_seen_at: Mapped[UtcTimestamp]
    last_seen_at: Mapped[UtcTimestamp]
    """When Reaper last saw this id bound to a Plex listing. The clock a return is measured
    against, and it is the last time Reaper *looked*, not the moment the title left, which is
    why a minimum absence alone cannot settle a return (``docs/history/RETURN_PLAN.md``)."""

    returned_at: Mapped[UtcTimestamp | None] = mapped_column(default=None)
    """When Reaper recorded that this title had come back, or ``NULL`` for the ordinary state
    of a title that has sat in one place.

    Stored rather than derived, because a return is visible for exactly one scan: the
    moment the new key is written into ``rating_keys_json`` it stops looking new. The
    hold runs in months, so the fact has to outlive its own evidence.

    Only ever the instant Reaper detected the return, never the copy's own arrival date,
    which may be earlier. A later start means a longer hold, which favors keeping the
    file."""

    returned_by_reaper: Mapped[bool | None] = mapped_column(Boolean, default=None)
    """Whether Reaper's own journal shows it removed this title before the return.

    Decided once, at detection, by the ``ActionStep`` -> ``ReapRun`` -> ``Candidate`` join
    (``services.library_seen.removed_by_reaper``), because that join reads the whole journal
    and a stored answer costs one query per scan instead of one per item.

    Three states, and the third is not "no." ``NULL`` is a row with no return recorded at
    all; ``False`` is a return whose removal Reaper cannot claim. Both take the same
    hold, for the same length. Only the sentence the operator reads differs."""


class WhitelistEntry(Base):
    """An item the owner spared by hand, "never reap this," from the review queue.

    Distinct from the *arr-tag and Plex-collection whitelists, which live in those
    systems and are re-read from them every scan. This is Reaper's own record, for the
    owner who wants to protect one file without round-tripping through Sonarr to tag it.

    Like ``FirstFlagged`` it lives outside the snapshot lifecycle: sparing is a durable
    decision about a file, not a property of the scan that happened to surface it. Keyed
    by the stable ``media_key``, never the Plex rating key, which moves.

    ``title`` is denormalized so the "spared" list reads without joining a snapshot
    (candidates are snapshot-scoped and churn); the media_key is the identity, the title
    is only ever display.
    """

    __tablename__ = "whitelist"

    media_key: Mapped[str] = mapped_column(String(100), primary_key=True)
    title: Mapped[str] = mapped_column(String(500))
    note: Mapped[str | None] = mapped_column(Text, default=None)
    """The owner's reason, shown back to them. Optional: sparing needs no justification."""

    decision: Mapped[str] = mapped_column(String(10), default="spare")
    """``"spare"`` (never reap) or ``"reap"`` (force onto the reap list). A reap override
    beats the score-based protections, because the owner has looked and decided, but a
    hard safety gate (something streaming now, or a file no *arr manages) still wins
    regardless. For a TV show the key can be the show's (``sonarr:i:series``), which
    applies to all its seasons."""

    spare_expires_at: Mapped[UtcTimestamp | None] = mapped_column(default=None)
    """When a timed hand-spare stops protecting. ``NULL`` means kept forever, the original
    behavior and what every pre-existing row carries, so an omitted expiry is never an
    early reap. Only meaningful for a ``"spare"`` decision; a ``"reap"`` never expires and
    clears it.

    The clock runs in exactly one place, the scan, in two coupled steps of one
    transaction: it drops a spare whose expiry has passed from the map it judges on
    (``whitelist.overrides_effective_at``), so the item is re-judged from scratch and
    re-enters the reap flow on a fresh grace window, and it deletes the expired row
    (``whitelist.purge_expired_spares``), so every live consumer converges the moment the
    snapshot commits. Every live consumer keeps the spare in force until that next scan
    runs, failing toward keeping the file, never toward deleting it early."""

    created_at: Mapped[UtcTimestamp]


class RecoveryToken(Base):
    """A single-use, short-lived login link for the anti-lockout path.

    Minted only when Reaper boots with ``REAPER_RECOVERY=true``, and printed to the
    container log, never emailed, never shown in the UI. Obtaining it requires the
    ability to set an environment variable and read the logs, which is host access, so it
    grants an attacker nothing they could not already do by editing the database
    directly.
    """

    __tablename__ = "recovery_token"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    created_at: Mapped[UtcTimestamp]
    expires_at: Mapped[UtcTimestamp]
    # Set the moment it is redeemed. A used token is never valid again, even
    # if it is still inside its expiry window.
    used_at: Mapped[UtcTimestamp | None] = mapped_column(default=None)


#: Tables holding live proof of a sign-in: something that lets a caller in now, rather
#: than the account it belongs to. A restore replaces the whole database and brings the
#: backup's password hashes back with it, so everything here must be cleared out of the
#: staged copy first, or a session or reset link that was valid when the backup was taken
#: starts working again and defeats a later sign-out-everywhere.
#:
#: Declared here, beside the models, rather than as a literal inside the restore code that
#: consumes it (``services.restore._purge_auth_state``), so a new auth-bearing table added
#: over there is never carried silently forward. ``tests/test_restore.py`` fails when a
#: table that looks auth-bearing is missing from this tuple.
AUTH_BEARING_TABLES: tuple[str, ...] = (
    "auth_session",
    "recovery_token",
    "pending_plex_login",
)

#: The exceptions, spelled out so the drift test can tell "considered and excluded" from
#: "forgotten". Each of these holds a credential, and each is meant to come back:
#:
#: * ``app_user`` carries ``password_hash``, but it is the ACCOUNT, not a live session.
#:   Restoring a backup restores its admins; purging them would lock the operator out of
#:   the install they just restored.
#: * ``plex_server`` and ``instance`` carry credentials for OTHER systems (the Plex admin
#:   token, the *arr keys). Restoring them is the entire point of bundling the encryption
#:   key with the backup, and none of them lets anyone into Reaper.
NOT_AUTH_BEARING_TABLES: tuple[str, ...] = ("app_user", "plex_server", "instance")


class AppSetting(Base):
    """Singleton key/value config that the admin edits in the web UI.

    Distinct from ``Settings`` (environment/bootstrap concerns like the data
    directory and the Fernet key, which cannot live in the database they protect).
    """

    __tablename__ = "app_setting"

    key: Mapped[str] = mapped_column(String(100), primary_key=True)
    value_json: Mapped[str] = mapped_column(Text)
    updated_at: Mapped[UtcTimestamp]


class ListConfig(Base):
    """A protection list the operator authored, or one Reaper ships with.

    Definition lives here; membership lives in the cache. ``protection_list`` and
    ``protection_list_item`` hold what each list currently contains, and they are
    excluded from Alembic and from backups on purpose: they mirror somebody else's data,
    and the next sync rebuilds them. A list the operator named and pointed at something is
    not rebuildable from anything, so it lives in this database, is migrated, and is
    backed up. Storing it beside the membership would mean a restore drops every list the
    operator configured, and the next scan removes what those lists were protecting.

    The id is stable across every edit. A derived list is identified by a slug carrying
    the settings that produced it, so changing a setting mints a new slug and would
    strand the old row still protecting from a rule the operator already replaced. That
    is the whole reason ``lists.retire_absent`` exists. A row here keeps its id, so an
    edit is an UPDATE and there is nothing to retire.
    """

    __tablename__ = "list_config"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)

    name: Mapped[str] = mapped_column(String(100, collation="NOCASE"), unique=True)
    """The operator's own name, and what a policy rule names to use this list.

    Unique, because a rule naming a list has to mean exactly one list. Two rows answering to
    one name would make a protection point at whichever was written last, which is a
    protection that silently changes what it covers.

    Unique without regard to case, because that is how every reader compares it: matching
    lower-cases both sides of a name, so "Never Reap" and "never reap" would otherwise be
    two rows here but one list to `services.list_rules` and to the ``on_list`` field. Two
    rows sharing one name could each carry half of what looks like a single keep rule, so
    deleting one would silently stop the other from protecting anything. The collation is
    what makes the stored constraint agree with the comparison; `list_config._clean_name`
    refuses the collision first, so the operator reads a sentence rather than a failed
    write.
    """

    source: Mapped[str] = mapped_column(String(32))
    """Which kind of thing this reads: a Plex collection, an *arr tag rule, a curated list.
    See ``services.lists.ListSource``."""

    config_json: Mapped[str] = mapped_column(Text, default="{}")
    """That source's own settings: which collection in which library, which tags and
    whether they match ANY or ALL. JSON because each source needs different keys."""

    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    """Off keeps the row and its name, so a list switched off comes back with its rules
    intact. Deleting is a separate verb and is the operator's to choose."""

    created_at: Mapped[UtcTimestamp]


class RunState(enum.StrEnum):
    """Where a reap run is in its lifecycle."""

    PLANNED = "planned"  # steps written, nothing sent
    EXECUTING = "executing"  # at least one step has been attempted
    COMPLETED = "completed"  # every step reached a terminal state
    ABORTED = "aborted"  # a cap, a canary failure, or the kill switch stopped it


class StepState(enum.StrEnum):
    """Where one step is in its lifecycle.

    The ordering matters: a step is journalled as ``PENDING`` when the plan is built, and
    is marked ``SENT`` and committed immediately before its HTTP call is dispatched. The
    executor commits every step-state change durably as it happens
    (``Executor._mark_sent`` / ``_mark_verified``), never inside a run-long transaction. A
    step found still ``SENT`` at startup was therefore in flight when the process died: a
    durable record of precisely what was outstanding, so a reconciler could re-probe the
    *arr to discover whether it actually landed. No such reconciler runs today, since a
    crashed run is re-planned from a fresh snapshot rather than resumed, so this is an
    audit trail, not a resume point.
    """

    PENDING = "pending"  # journalled, not yet sent
    SENT = "sent"  # request dispatched, outcome not yet confirmed
    VERIFIED = "verified"  # the world was re-read and matches what we intended
    FAILED = "failed"  # the call errored, or verification did not match
    SKIPPED = "skipped"  # a pre-flight re-check spared the item (someone watched it)


class ReapRun(Base):
    """One execution of a set of deletions: the durable record of what Reaper did.

    Bound to the exact snapshot and policy it was approved against. If either changed
    between approval and execution, the approval is void: an operator cannot approve a
    plan and then have Reaper carry out a different one.
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

    # The content-bound confirmation the human actually approved, e.g. "REAP 7 SOULS
    # 214 GB". Recomputed from the plan at execution time; a mismatch voids the run, so a
    # stale browser tab cannot replay yesterday's plan against today's library.
    approved_manifest_hash: Mapped[str] = mapped_column(String(64))
    approved_by: Mapped[str] = mapped_column(String(100))
    approved_at: Mapped[UtcTimestamp]

    started_at: Mapped[UtcTimestamp | None] = mapped_column(default=None)
    finished_at: Mapped[UtcTimestamp | None] = mapped_column(default=None)

    aborted_reason: Mapped[str | None] = mapped_column(Text, default=None)
    """Why the run stopped early. A cap is an abort, never a truncation: truncating makes
    what gets deleted depend on sort order, so Reaper stops the whole run instead.

    Stored as JSON: ``engine.reason.to_stored`` of the run's
    :class:`~reaper.engine.reason.Reason`, so the browser can translate it. An older row
    can hold a bare English sentence instead. ``engine.reason.from_stored`` reads both
    shapes back, the bare sentence as a ``legacy`` reason."""

    held_back_unknown_size: Mapped[int] = mapped_column(Integer, default=0)
    """How many condemned items this plan left out because nothing would report their
    size. Stored on the run rather than recomputed, so a finished run can still say what
    it did not do: a plan smaller than the review queue implied has to be explainable
    after the fact, not only before it. Zero for a healthy library, and every surface
    suppresses it at zero."""

    steps: Mapped[list[ActionStep]] = relationship(
        back_populates="run", cascade="all, delete-orphan", order_by="ActionStep.id"
    )


class ActionStep(Base):
    """One mutating HTTP call, written to the database before it is sent.

    This is the heart of the safety model. The row exists, with its exact method, path
    and body, before the request leaves the process, so a crash mid-run leaves a durable
    record of precisely what was in flight. That record is what a future reconciler could
    use to re-probe the *arr and converge; today it is an audit trail, not a resume
    point, since a crashed run is re-planned from a fresh snapshot rather than resumed.

    The recorded ``method``/``path``/``body`` are also what the executor hands to the
    GuardedTransport as its declaration: the guard refuses any mutation that was not
    first written here, so "journalled before sent" is enforced by the transport, not
    merely intended by convention.
    """

    __tablename__ = "action_step"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("reap_run.id", ondelete="CASCADE"), index=True)
    """Which run this step belongs to. Indexed by revision ``f7a8b9c0d1e2``, which says why."""

    media_key: Mapped[str] = mapped_column(String(100), index=True)
    """Which candidate this step acts on, so the journal can be read back per item."""

    ordinal: Mapped[int] = mapped_column(Integer)
    """Execution order within the run. The canary is ordinal 0: the single smallest item,
    executed and verified alone before anything else is allowed to proceed."""

    kind: Mapped[str] = mapped_column(String(40))
    """Which action, one of the four the planner emits: 'radarr_delete',
    'sonarr_unmonitor', 'sonarr_verify_unmonitor', 'sonarr_delete_files'. The Plex refresh
    is not among them: it is ``executor._best_effort_refresh``, declared to the guard
    rather than journalled as a step, so nothing here records one."""

    method: Mapped[str] = mapped_column(String(10))
    path: Mapped[str] = mapped_column(String(500))
    body_json: Mapped[str | None] = mapped_column(Text, default=None)
    """The literal request. This must never store credentials: they are injected only at
    send time, so the journal is safe to render in the UI and keep forever."""

    idempotency_key: Mapped[str] = mapped_column(String(64), index=True)
    """Stable across retries and restarts: it is the de-duplication handle a future
    reconciler would key on, so a recovered step cannot delete twice or double-apply an
    exclusion. No reconciler consumes it yet; it is journalled now so that safety is
    available when one is built. Today double-apply is prevented by re-planning a
    crashed run rather than resuming it, and by the verify-after-send step being the
    source of truth."""

    state: Mapped[StepState] = mapped_column(
        Enum(StepState, native_enum=False), default=StepState.PENDING
    )

    verification_json: Mapped[str | None] = mapped_column(Text, default=None)
    """What we re-read after the call to confirm it landed. For a delete-with-exclusion
    this records that the exclusion is actually present: Sonarr and Radarr each accept
    the other's exclusion parameter and return 200 while doing nothing, so a 200 is not
    evidence. The re-read is."""

    error: Mapped[str | None] = mapped_column(Text, default=None)
    """Why this step failed or was skipped. NULL on a step that has not run or that
    succeeded.

    Stored as JSON: ``engine.reason.to_stored`` of the step's
    :class:`~reaper.engine.reason.Reason`, so the browser can translate it. An older row
    can hold a bare English sentence instead. ``engine.reason.from_stored`` reads both
    shapes back, the bare sentence as a ``legacy`` reason."""

    created_at: Mapped[UtcTimestamp]
    sent_at: Mapped[UtcTimestamp | None] = mapped_column(default=None)
    verified_at: Mapped[UtcTimestamp | None] = mapped_column(default=None)

    file_removed_at: Mapped[UtcTimestamp | None] = mapped_column(default=None)
    """When Reaper confirmed this step's file was really gone from disk. Recorded
    separately from ``state`` because the two can disagree.

    A movie deleted through Radarr whose import exclusion never appeared inside the poll
    window ends FAILED: the verification genuinely did fail, and the state must keep
    saying so. But the file is gone and its bytes are reclaimed, so the rolling 30-day
    budget has to charge it, or a flaky *arr would silently buy the operator unlimited
    deletions (``Executor._rolling_30d_deletions`` counts VERIFIED or this, never both
    twice). NULL on every row written before this column existed, which reads exactly as
    it did then: only its VERIFIED state counts it."""

    run: Mapped[ReapRun] = relationship(back_populates="steps")
