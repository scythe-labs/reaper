# SPDX-License-Identifier: AGPL-3.0-or-later
"""Load a policy body an older version of Reaper wrote.

A stored body may be JSON that ``PolicyBody`` refuses to validate. Each shim here reads the
raw stored dict and tries to fix it: it returns a dict that validates, or ``None`` when it
cannot. The caller (``services.profiles.active_policy``) falls back to the shipped default
and says so when a shim returns ``None``. ``PolicyRepair`` lists every way a shim can change
a stored body, and every surface that tells the operator about a repair reads that list.

Split out of ``engine/policy.py``, which holds the model and the hash over it. Every shim here
runs once, when a policy loads, and none of them run during scoring. One exception:
``LIST_GATES_NOW_KEEP_RULES`` is read by ``scan_runner.build_gates`` on every scan, so this
file stays even after every install has migrated. This module imports ``PolicyBody`` and
``SCHEMA_VERSION`` from ``policy``, and ``policy`` imports nothing back, so the two modules
cannot import each other in a loop.
"""

from __future__ import annotations

import copy
import enum
import json
from collections.abc import Mapping, Sequence
from typing import Any

from pydantic import ValidationError

from reaper.engine.fields import Op
from reaper.engine.gates import GateId
from reaper.engine.policy import SCHEMA_VERSION, PolicyBody
from reaper.engine.signals import MAX_SCORE
from reaper.ratings import RatingSource
from reaper.text import fold


class PolicyRepair(enum.StrEnum):
    """One way ``active_policy`` changed a stored body so it would load.

    This is the single declaration every surface reads from. It must be an enum,
    never four separate booleans on ``ActivePolicy``. Each repair has to reach four
    places at once: the flag on ``PolicyOut``, the editor's dirty flag, the notice text, and
    the scan's degradation message. A boolean can be wired to three of those four and still
    look correct; a member of this enum cannot, because ``tests/test_policy_repairs.py``
    walks every member and fails on one that is missing copy anywhere.

    Add a shim by adding a member here, then follow the test to every place it still needs
    wiring.
    """

    RESCALED = "rescaled"
    """Removal weights rescaled to total 100 (``rebalance``). The operator's own tuning, in
    the new units."""

    FELL_BACK = "fell_back"
    """The body could not be repaired, so this is the shipped default instead. The loudest
    of the four repairs: these are numbers the operator never chose, and they can protect
    less than what was saved."""

    RATING_RULES_RESTORED = "rating_rules_restored"
    """The rating bar was put back from an older saved setting (``recover_rating_rules``).
    The unrepaired body still validates, but protects nothing, which is why it counts as a
    repair anyway."""

    LISTS_MIGRATED = "lists_migrated"
    """List protections rewritten as ``on_list`` keep rules (``convert_list_protections``).
    The rewrite keeps every verdict the same. It must still be shown to the operator, never
    applied silently, since the stored body and the body now in force say different things."""


#: A gate key some stored bodies still carry that the model no longer declares. ``secondary``
#: held the rating gate's vote floor before that bar moved to ``keep_rating_rules``. Some
#: stored bodies still carry it because it is the only surviving copy of the operator's bar.
_RETIRED_GATE_KEYS = frozenset({"secondary"})


def drop_retired_gate_keys(body: dict[str, Any]) -> None:
    """Strip keys the model no longer declares, in place, from every gate row.

    ``PolicyBody`` refuses any extra key, so a body still carrying one of these will not
    load. Every shim that reads the raw stored dict calls this first: without it,
    ``rebalance`` would return ``None`` and throw away the operator's tuning, and
    ``recover_rating_rules`` would raise inside a handler that only logs, so a rating bar
    would silently stop being recovered.
    """
    gates = body.get("gates")
    if not isinstance(gates, list):
        return
    for gate in gates:
        if isinstance(gate, dict):
            for key in _RETIRED_GATE_KEYS:
                gate.pop(key, None)


def rebalance(raw: object) -> dict[str, Any] | None:
    """Rescale a stored policy body so its removal weights total exactly 100.

    Older bodies, written before ``PolicyBody._weights_total_one_hundred`` existed, could
    total anything. Such a body no longer loads, and falling back to the shipped default
    would throw away the operator's tuning and show them numbers they never chose.

    An exact rescale never changes the score: the score is already
    ``100 * Σpressure / Σweight``, so dividing every weight by the same factor leaves it
    unchanged. Rounding the rescaled weights to integers can still move a score by more than
    a point, even though largest-remainder rounding keeps each weight's own error to at most
    1: those per-weight errors do not cancel out in the score, because a rule that gained a
    point from rounding may be carrying pressure on an item while a rule that lost a point is
    not. Weights ``(1, 1, 1, 5)`` round to ``(13, 13, 12, 62)`` and can move a score by a full
    point; six equal weights round to ``17, 17, 17, 17, 16, 16`` and can move one by 1.33,
    enough to cross a condemn threshold. The largest this drift can be is half the number of
    weighted rules, and no other way of assigning the rounding error does better, because
    which rules will actually carry pressure on a given item cannot be known while rescaling.

    So a rescaled body is never adopted silently. The caller flags it
    (``PolicyRepair.RESCALED``), which marks ``profiles.ActivePolicy.repaired``, degrades the
    scan, and opens the editor on the rescaled body as an unsaved draft for the operator to
    review and save themselves. ``tests/test_policy.py`` checks the rounding bound above and
    that a verdict near the threshold can move.

    Returns ``None`` when the body cannot be read for any other reason, including valid JSON
    that is not an object at all, so the caller can tell "needs rescaling" apart from
    "genuinely broken" and never show a repaired body it does not understand. Never raises:
    ``services.profiles.active_policy`` depends on that to keep the policy editor reachable.
    """
    try:
        if not isinstance(raw, dict):
            return None
        body: dict[str, Any] = copy.deepcopy(raw)
        drop_retired_gate_keys(body)
        parts: list[dict[str, Any]] = [
            *(body.get("signals") or []),
            *(body.get("custom_condemn") or []),
        ]
        total = sum(int(p["weight"]) for p in parts)
        if total <= 0:
            return None
        exact = [int(p["weight"]) * MAX_SCORE / total for p in parts]
        floors = [int(x) for x in exact]
        order = sorted(range(len(parts)), key=lambda i: exact[i] - floors[i], reverse=True)
        for i in order[: MAX_SCORE - sum(floors)]:
            floors[i] += 1
        for part, weight in zip(parts, floors, strict=True):
            part["weight"] = weight
        PolicyBody.model_validate(body)  # only hand back something that actually loads
    except (AttributeError, KeyError, TypeError, ValueError, ValidationError):
        # AttributeError covers a body whose "signals"/"custom_condemn" entries are not
        # objects either, so a `.get`/`["weight"]` on the wrong shape returns None here.
        # This function must never raise.
        return None
    return body


def recover_rating_rules(raw: object) -> dict[str, Any] | None:
    """Restore a rating bar lost when that bar moved off the gate row.

    The bar used to live on the RATING_FLOOR gate setting, as ``threshold`` (tenths) plus
    ``secondary`` (minimum votes). It now lives in ``keep_rating_rules``, one spec per rating
    source, and the move shipped no backfill for bodies written before it. Such a body still
    validates: the gate keeps its now-unused numbers, and ``keep_rating_rules`` defaults to
    empty. But an empty rule set makes ``RatingFloorGate`` abstain on every item, so an
    operator's "keep anything at 7.5 from 1,000 votes" ends up protecting nothing, on a
    snapshot that otherwise runs and executes normally. Every install seeded before the move
    is in this state, whether or not anyone opened the editor, because ``services.profiles``
    writes the shipped default as a real row the first time a profile is saved.

    Returns the body with the equivalent IMDb bar added back, or ``None`` when there is
    nothing to recover. The caller flags it (``PolicyRepair.RATING_RULES_RESTORED``), which
    marks ``repaired``, degrades the scan, and opens the editor on the restored body as an
    unsaved draft: this must never silently swap in a value the operator never chose.

    ``secondary`` is read here off the raw stored dict; it is not a field on ``GateSetting``
    any more, but some stored rows still carry it as the only surviving copy of their bar. So
    the body handed back goes through ``drop_retired_gate_keys`` first: ``PolicyBody``
    refuses any extra key, and a body returned with one would raise inside a caller that only
    logs, stopping the recovery silently.

    This must never trigger off ``schema_version``: an affected body's schema version does
    not change across the move, so it cannot tell old and new bodies apart. It instead fires
    when the raw ``keep_rating_rules`` key is absent (an explicit empty list means the operator
    deliberately cleared their bars, and that choice is kept) and the ``rating_floor`` gate
    is enabled with numbers the old validator would have accepted (``1 <= threshold <= 100``,
    ``secondary >= 1``). A disabled gate is left alone, since nothing was protecting anything
    either way. IMDb is the source to recover into because it is the only one the old
    single-source gate ever read.

    Never raises: ``services.profiles.active_policy`` depends on that to keep the policy
    editor reachable.
    """
    if not isinstance(raw, dict) or "keep_rating_rules" in raw:
        return None
    gates = raw.get("gates")
    if not isinstance(gates, list):
        return None
    for gate in gates:
        if not isinstance(gate, dict) or gate.get("gate") != GateId.RATING_FLOOR.value:
            continue
        if not gate.get("enabled", True):
            return None
        floor, min_votes = gate.get("threshold"), gate.get("secondary")
        # bool is an int subclass, so a body carrying `true` must not read as 1.
        if isinstance(floor, bool) or isinstance(min_votes, bool):
            return None
        if not isinstance(floor, int) or not isinstance(min_votes, int):
            return None
        if not 1 <= floor <= 100 or min_votes < 1:
            return None
        body = copy.deepcopy(raw)
        drop_retired_gate_keys(body)
        body["keep_rating_rules"] = [
            {"source": RatingSource.IMDB.value, "floor": floor, "min_votes": min_votes}
        ]
        # Write back at the current schema so a body that has been through the editor
        # since can be told apart, and this shim can eventually retire.
        body["schema_version"] = SCHEMA_VERSION
        return body
    return None


#: The two gates that became keep rules on Settings -> Lists. Retired as gates, but their
#: ``GateId`` members stay so a stored explanation can still decode. These stay listed
#: separately from ``RETIRED_GATES``, because each protected something while it was live: a
#: body naming one is converted to the equivalent rule, never simply stripped.
#: Three readers share this one declaration: the conversion, the code that strips a gate row
#: once its rule exists, and ``scan_runner.build_gates``, which tells an operator who still
#: hits one of these a different message than it gives for a gate that was never implemented.
LIST_GATES_NOW_KEEP_RULES: frozenset[GateId] = frozenset({GateId.WHITELISTED, GateId.CURATED_LIST})
_LEGACY_LIST_GATES = frozenset(gate.value for gate in LIST_GATES_NOW_KEEP_RULES)

#: What a fresh install's two seeded lists are made of: the tag the tag list carries, and
#: the preset the IMDb list names. ``policy.DEFAULT_TAG_LIST_NAME`` and
#: ``policy.DEFAULT_IMDB_LIST_NAME`` name the lists themselves. ``list_config.DEFAULT_LISTS``
#: must read these two constants, never respell them, so the row a fresh install seeds and
#: the row a converted upgrade looks for come from one declaration.
DEFAULT_KEEP_TAG = "reaper-keep"
DEFAULT_IMDB_PRESET = "top250"


def legacy_keep_tags(raw: object) -> tuple[str, ...]:
    """The *arr tags a legacy body was protecting on, with blanks dropped.

    A body carrying no ``keep_tags`` key ran on the shipped default, so this returns that
    default. A body with an explicit empty list had it cleared by the operator, and this
    returns nothing for it. So an empty result means "this body had no tag protection",
    which is exactly how ``convert_list_protections`` reads it, and also how the caller
    finds the registry row those tags became. Both callers read this one function so they
    can never disagree about which bodies had a tag protection to convert.
    """
    if not isinstance(raw, dict):
        return ()
    tags = raw.get("keep_tags", None)
    if not isinstance(tags, list):
        return (DEFAULT_KEEP_TAG,)
    return tuple(t for t in (str(x).strip() for x in tags) if t)


def _config_value(config_json: str | None, key: str) -> object:
    """One value out of a stored list config, or ``None`` when it will not parse.

    An unreadable config reads as "not this row." At worst that skips a conversion, and the
    part that does not convert keeps its gate, which then must stop the scan loudly: it must
    never run on a protection that silently vanished."""
    try:
        config = json.loads(config_json or "{}")
    except ValueError:
        return None
    return config.get(key) if isinstance(config, dict) else None


def conversion_list_names(
    rows: Sequence[tuple[str, str, str | None]],
    *,
    keep_tags: Sequence[str],
) -> tuple[str | None, str | None, tuple[str, ...]]:
    """Which registry lists ``convert_list_protections``'s rules must name.

    ``rows`` are ``(source, name, config_json)``, oldest first. Returns the tag list's
    current name, the shipped IMDb list's current name, and every list the operator curates
    on Plex. ``None`` for either of the first two means no such list exists, and the
    conversion then leaves that half's gate in place.

    A list is matched by what it holds, never by how old it is. ``keep_tags`` (the legacy
    body's own tags) is tested against each *arr-tag row's configured tags; the shipped
    preset is tested against each IMDb row's configured preset. Matching by age instead would
    misfire if the operator deletes the tag list this converts: the *arr-tag list they added
    for something else would then become the oldest of its source and inherit a keep-outright
    protection nobody gave it.

    Tag matching succeeds on any shared tag, because the upgrade migration
    merges both media types' tags into one list, and an operator adding a tag beside them
    does not stop it being the list their tags became.

    This function must stay pure, taking rows rather than a database session: the load
    shim reads them through SQLAlchemy and the upgrade migration reads them through raw SQL.
    Both must select lists the same way, so both call this one function.
    """
    wanted = {fold(t) for t in keep_tags if t.strip()}

    def carries_a_wanted_tag(config_json: str | None) -> bool:
        held = _config_value(config_json, "tags")
        if not isinstance(held, list):
            return False
        # Case-folded on both sides, the same comparison every reader of a tag makes.
        return bool(wanted & {fold(str(t)) for t in held})

    tag = next(
        (
            name
            for source, name, config in rows
            if source == "arr_tag" and carries_a_wanted_tag(config)
        ),
        None,
    )
    imdb = next(
        (
            name
            for source, name, config in rows
            if source == "imdb" and _config_value(config, "preset") == DEFAULT_IMDB_PRESET
        ),
        None,
    )
    # No such test for the operator's own Plex lists, and none is possible: the retired
    # WHITELISTED gate spared everything on every list they curate by hand and the body names
    # none of them, so the registry's own set is the whole answer the body has.
    own = tuple(name for source, name, _ in rows if source in ("plex_collection", "plex_watchlist"))
    return tag, imdb, own


#: A collection whose library kind Reaper cannot pin down keeps its rule on BOTH policies. A
#: name missing from ``own_list_media_scope``'s map reads as this too, so an empty map leaves
#: every rule exactly where it was before this scoping existed. Public so the load shim and
#: the upgrade migration share one constant.
BOTH_MEDIA_TYPES: frozenset[str] = frozenset({"movie", "tv"})

#: The policy media type each Plex library kind seeds a keep rule on. Plex names a TV library
#: "show"; a policy names that side "tv". Music and photo libraries never reach here, since
#: ``PlexClient.video_sections`` surfaces only these two kinds, and
#: ``app_settings.set_plex_libraries`` stores whatever it returns.
_PLEX_KIND_TO_MEDIA: dict[str, str] = {"movie": "movie", "show": "tv"}


def library_media_types(
    libraries: Sequence[Mapping[str, object]],
) -> dict[str, frozenset[str]]:
    """Map each case-folded library title to the media types libraries with that title span.

    Reads the synced ``plex_libraries`` setting rows, each a ``{"title", "kind", ...}`` dict.
    Two same-titled libraries of different kinds map to both types, which
    ``own_list_media_scope`` reads as "cannot tell" and keeps a collection under that title on
    both policies. A kind that is neither ``movie`` nor ``show`` is skipped; none is surfaced
    today. Titles are case-folded, the same comparison every reader of a Plex name makes."""
    out: dict[str, set[str]] = {}
    for lib in libraries:
        title = lib.get("title")
        media = _PLEX_KIND_TO_MEDIA.get(str(lib.get("kind")))
        if isinstance(title, str) and title.strip() and media is not None:
            out.setdefault(fold(title), set()).add(media)
    return {title: frozenset(media) for title, media in out.items()}


def own_list_media_scope(
    rows: Sequence[tuple[str, str, str | None]],
    library_types: Mapping[str, frozenset[str]],
) -> dict[str, frozenset[str]]:
    """For each Plex list the operator curates by hand, the media types a keep rule naming it
    may protect. Keyed by the list's name, case-folded, the same comparison every reader of a
    list name makes.

    A ``plex_collection`` lives in one Plex library, so it can only hold that library's one
    type. A ``plex_watchlist`` spans the whole account, so it can hold both. ``library_types``
    maps a case-folded library title to the media types libraries with that title span
    (``library_media_types``, from the synced ``plex_libraries`` setting): a library that was
    never synced, has since been renamed, or shares a title with a library of a different kind
    all leave the type unknown or spanning both.

    This function only narrows a collection's rule to one policy when its library's type is
    known and single. An unknown, ambiguous, or unsynced library keeps the rule on both: a
    rule on a type the collection cannot hold simply matches nothing there, which costs
    nothing, while dropping it for a library this function cannot read would withdraw a live
    protection. A watchlist always keeps both.

    Two rows can share the same case-folded key. Their types must be combined, never
    overwritten: overwriting could drop a rule's protection on the type the
    overwritten row needed."""
    scope: dict[str, set[str]] = {}
    for source, name, config_json in rows:
        key = fold(name)
        if source == "plex_watchlist":
            scope.setdefault(key, set()).update(BOTH_MEDIA_TYPES)
            continue
        if source != "plex_collection":
            continue
        library = _config_value(config_json, "library")
        held = (
            library_types.get(fold(str(library)))
            if isinstance(library, str) and library.strip()
            else None
        )
        # Narrow only when the library's type is known and single. ``held`` is None for an
        # unsynced or renamed library, and holds two types when a movie and a show library
        # share a title; either case keeps the rule on both policies.
        this = held if held is not None and len(held) == 1 else BOTH_MEDIA_TYPES
        scope.setdefault(key, set()).update(this)
    return {key: frozenset(media) for key, media in scope.items()}


def authorable_media_scope(
    source: str,
    config_json: str | None,
    observed: frozenset[str],
    synced: bool,
    library_types: Mapping[str, frozenset[str]],
) -> frozenset[str]:
    """The media types a keep rule on this list can be authored for.

    This is the set the Policy editor's list picker offers a new rule on. An empty set means
    offer it on neither type: the list's type is not known, and a rule on it could match
    nothing while still reading as a live protection.

    This decides whether to offer a NEW rule, never whether to keep an existing one, so it
    fails closed: a list whose type cannot be established must be withheld, never offered on
    a type it may not hold. ``own_list_media_scope`` makes the opposite choice, because it
    scopes a legacy protection being converted and must never drop a rule that is already
    live, so an unreadable library there keeps both types. The two functions agree whenever
    the type is actually known, and differ only when it is not.

    The type is known in layers:
    - ``plex_watchlist`` spans the whole account, so both types, synced or not.
    - ``imdb`` is stamped movie by every sync (``services.lists``), so movie, synced or not.
    - ``plex_collection`` lives in one library, and the library's kind gives the type without
      needing a sync (``library_types``). A library that is unknown, renamed, or shared
      between a movie and a show library leaves the type unsettled, and falls through to
      what a sync of the collection's own content observed.
    - ``arr_tag``, and a collection that fell through the case above, are known only from
      synced content: the types its members actually span, or both if a sync found it
      verified but empty, so a list the operator means to fill can be authored now. A list
      never synced is withheld until a sync runs."""
    if source == "plex_watchlist":
        return BOTH_MEDIA_TYPES
    if source == "imdb":
        return frozenset({"movie"})
    if source == "plex_collection":
        library = _config_value(config_json, "library")
        held = (
            library_types.get(fold(str(library)))
            if isinstance(library, str) and library.strip()
            else None
        )
        if held is not None and len(held) == 1:
            return held
        # Library unknown, renamed, or two-typed: what a sync saw is all that is left to go on.
    if synced:
        return observed or BOTH_MEDIA_TYPES
    return frozenset()


def has_legacy_list_protections(raw: object) -> bool:
    """Whether ``convert_list_protections`` would change this body.

    Exposed so a caller can decide whether it is worth resolving the target list names, which
    needs a database read, before running the same check the conversion itself uses."""
    if not isinstance(raw, dict):
        return False
    if "keep_tags" in raw or "keep_tags_match" in raw:
        return True
    gates = raw.get("gates")
    if isinstance(gates, list) and any(
        isinstance(g, dict) and g.get("gate") in _LEGACY_LIST_GATES for g in gates
    ):
        return True
    conditions = raw.get("protect_conditions")
    return isinstance(conditions, list) and any(
        isinstance(c, dict) and c.get("field") == "on_curated_list" for c in conditions
    )


def convert_list_protections(
    raw: object,
    *,
    media_type: str,
    tag_list_name: str | None,
    imdb_list_name: str | None,
    collection_list_names: tuple[str, ...] = (),
    collection_media_scope: Mapping[str, frozenset[str]] | None = None,
) -> dict[str, Any] | None:
    """Convert a stored body from before every list protected through its own keep rule.

    Converts three legacy shapes together, because they were saved together:

    * ``keep_tags`` / ``keep_tags_match``, the *arr tags configured on Policy. Both keys are
      removed from the body. An enabled ``whitelisted`` gate becomes an ``on_list`` rule that
      keeps that list outright. A disabled gate converts to no rule, since the operator had
      the protection off, and the Lists screen now shows it as not used by the policy.
    * the ``whitelisted`` and ``curated_list`` gate rows. Both gates are retired but were live
      protections while they existed, so each enabled one must convert to the equivalent rule,
      never simply be dropped with nothing in its place.
    * ``on_curated_list`` rules, re-spelled as ``on_list``. The field was renamed when it
      widened from the two shipped lists to every list; the value, which is the list's name
      either way, is kept as is.

    ``tag_list_name`` and ``imdb_list_name`` are the current names of the registry rows the
    new rules must point at. The caller reads them from the database, since the operator may
    have renamed either. ``None`` means no such list exists, and that half converts to no
    rule. The caller identifies those rows by what they
    hold, such as the matching tags or preset, never by which row is oldest, or a list the
    operator added for something unrelated could inherit the protection the moment the real
    one is deleted.

    ``media_type`` scopes the IMDb rule to the body it can hold. ``active_policy`` converts
    the movie and TV bodies separately, and the IMDb chart is movies only, so its rule lands
    on the movie body alone: a TV rule naming it could never match a season, and would read
    as a protection the operator never chose. The ``curated_list`` gate protected nothing on
    a TV body for the same reason, so it is stripped there with no replacement rule. The tag
    list spans both media types, so its rule lands on both bodies.

    ``collection_media_scope`` scopes each Plex collection's rule the same way, keyed by the
    collection name, case-folded. A collection lives in one Plex library, so its rule lands
    only on the policy for that library's type. This is fail-open: a collection name the map
    omits, or keeps at both types (an unsynced, renamed, or ambiguous library, and every
    watchlist), gets its rule on both bodies, which is also what happens when
    ``collection_media_scope`` is not given at all. A watchlist genuinely spans both types and
    always keeps both.

    Returns the converted body, or ``None`` when nothing here is in the legacy shape. The
    caller flags a real conversion (``PolicyRepair.LISTS_MIGRATED``), which marks
    ``repaired``, degrades the scan, and opens the editor on the converted body as an unsaved
    draft, since moving a protection between surfaces is a policy edit nobody has saved yet.
    Never raises, like every shim in this file.
    """
    if not has_legacy_list_protections(raw):
        return None
    assert isinstance(raw, dict)  # has_legacy_list_protections refused everything else
    gates = raw.get("gates")
    gate_rows = [g for g in gates if isinstance(g, dict)] if isinstance(gates, list) else []
    legacy_gates = {
        str(g.get("gate")): bool(g.get("enabled", True))
        for g in gate_rows
        if g.get("gate") in _LEGACY_LIST_GATES
    }

    body = copy.deepcopy(raw)
    # An explicit empty tag list means the operator cleared it, and converts to no rule. A
    # body with no key at all ran on the shipped default tag, which was a live protection to
    # carry over. Both readings live in ``legacy_keep_tags``, which the caller resolving
    # ``tag_list_name`` also reads.
    had_tags = bool(legacy_keep_tags(body))
    body.pop("keep_tags", None)
    body.pop("keep_tags_match", None)

    new_rules: list[dict[str, Any]] = []
    if legacy_gates.get("whitelisted"):
        # The gate covered every list the operator curates by hand: the keep tags, and the
        # Plex collection and watchlist definitions. Each one gets its own rule here, or an
        # upgrade would silently drop the collections' cover.
        if had_tags and tag_list_name:
            new_rules.append({"field": "on_list", "op": Op.EQ.value, "value": tag_list_name})
        scope = collection_media_scope or {}
        for name in collection_list_names:
            # A collection in a single-type library gets its rule only on that type's
            # policy. An unknown or ambiguous library keeps it on both.
            if media_type in scope.get(fold(name), BOTH_MEDIA_TYPES):
                new_rules.append({"field": "on_list", "op": Op.EQ.value, "value": name})
    # The IMDb chart is movies only, so its rule lands on the movie body alone.
    if legacy_gates.get("curated_list") and imdb_list_name and media_type == "movie":
        new_rules.append({"field": "on_list", "op": Op.EQ.value, "value": imdb_list_name})

    def converts(gate_id: str) -> bool:
        """Whether this gate row may leave the body.

        A disabled row always may. An enabled row may leave only once its replacement rule
        exists: stripping an enabled gate whose target list is missing would drop a live
        protection with nothing in its place, so the row stays instead, and ``build_gates``
        refuses the scan loudly."""
        if not legacy_gates.get(gate_id):
            return True
        if gate_id == "whitelisted":
            return not had_tags or tag_list_name is not None
        # curated_list on a TV body protected nothing, since the IMDb chart is movies only,
        # so it strips clean with no replacement rule needed. On a movie body it strips only
        # once its replacement rule exists.
        return media_type != "movie" or imdb_list_name is not None

    if isinstance(gates, list):
        body["gates"] = [
            g
            for g in body["gates"]
            if not (
                isinstance(g, dict)
                and g.get("gate") in _LEGACY_LIST_GATES
                and converts(str(g.get("gate")))
            )
        ]
    for row in body.get("protect_conditions") or []:
        if isinstance(row, dict) and row.get("field") == "on_curated_list":
            row["field"] = "on_list"

    if new_rules:
        rows = body.get("protect_conditions")
        if not isinstance(rows, list):
            rows = []
        spelled = {
            fold(str(r.get("value", "")))
            for r in rows
            if isinstance(r, dict) and r.get("field") == "on_list"
        }
        rows.extend(r for r in new_rules if fold(str(r["value"])) not in spelled)
        body["protect_conditions"] = rows
    return body
