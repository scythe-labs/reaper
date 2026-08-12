# SPDX-License-Identifier: AGPL-3.0-or-later
"""Loading a policy body an older Reaper wrote.

A stored body is JSON that ``PolicyBody`` refused, and the shims here try to make it load
without losing what the operator configured. Each one takes the *raw* stored dict, returns a
dict that validates, and returns ``None`` when it cannot: the caller
(``services.profiles.active_policy``) then falls back to the shipped default and says so.
``PolicyRepair`` is the set of ways that can happen, and it is the declaration four surfaces
derive their copy from.

Split out of ``engine/policy.py``, which is the model and the hash over it. Every SHIM here
runs once, at load, and none is read by scoring. **One declaration is not a shim and is on
the live path**: ``LIST_GATES_NOW_KEEP_RULES`` is read by ``scan_runner.build_gates`` on
every scan, and is the membership the fail-closed abort tests -- so this file does not retire
when every install has migrated, and that constant is the reason. The dependency runs one
way -- this module imports ``PolicyBody`` and ``SCHEMA_VERSION`` from ``policy`` and
``policy`` imports nothing back -- so the pair cannot cycle whatever either gains later.
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
    """One way ``active_policy`` had to change a stored body to load it.

    **The set is the declaration every surface derives from**, and that is the whole reason
    it is an enum rather than four booleans on ``ActivePolicy``. Each repair obliges four
    things at once: the flag reaches ``PolicyOut``, the editor's savebar forces dirty on it,
    a notice says which repair happened, and the scan's degradation sentence names what to
    check. ``lists_migrated`` shipped with the first and skipped the rest, so a stored body
    from before the lists move degraded every scan with an incomplete-scan banner the
    operator could not clear: the editor never went dirty, so the page held no Save, and the
    one exit the degradation names did not exist (#516). A boolean can be forgotten at three
    of four sites and read correct at each one; a member of this enum cannot, because
    ``tests/test_policy_repairs.py`` walks it and fails on any member either side lacks copy
    for (rules 103, 144).

    Adding a shim means adding a member here, then following the test where it fails.
    """

    RESCALED = "rescaled"
    """Removal weights rescaled to total 100 (``rebalance``). Their tuning, in new units."""

    FELL_BACK = "fell_back"
    """Unrepairable, so the body in hand is the SHIPPED DEFAULT. The loudest of the four:
    these are numbers the operator never chose, and they can be looser than what was saved."""

    RATING_RULES_RESTORED = "rating_rules_restored"
    """The rating bar was put back from an older saved setting (``recover_rating_rules``).
    That body loads perfectly well while protecting nothing, which is why it is a repair."""

    LISTS_MIGRATED = "lists_migrated"
    """List protections re-expressed as ``on_list`` keep rules (``convert_list_protections``).
    Verdict-preserving by construction, and still not adopted silently, because the stored
    body says one thing and the body in force says another."""


#: Gate keys that stored bodies still carry and the model no longer declares. ``secondary``
#: held the rating gate's vote floor until that bar moved to ``keep_rating_rules``; the
#: migration ``e6f708192a3b`` rewrote every body where the number was inert, and deliberately
#: left the ones where it is still the only copy of an operator's bar, so those arrive here.
_RETIRED_GATE_KEYS = frozenset({"secondary"})


def drop_retired_gate_keys(body: dict[str, Any]) -> None:
    """Strip keys the model no longer declares, in place, from every gate row.

    ``PolicyBody`` is ``extra="forbid"``, so a body a shim hands back still carrying one of
    these does not load. Both shims read the *raw* stored dict and both must do this, or the
    repair they exist to perform is the thing that fails (rules 72, 104): ``rebalance`` would
    return ``None`` and throw away the operator's tuning, and ``recover_rating_rules`` would
    raise into a handler that only logs, so a rating bar would stop being recovered in silence.
    """
    gates = body.get("gates")
    if not isinstance(gates, list):
        return
    for gate in gates:
        if isinstance(gate, dict):
            for key in _RETIRED_GATE_KEYS:
                gate.pop(key, None)


def rebalance(raw: object) -> dict[str, Any] | None:
    """A stored policy body rescaled so its removal weights total exactly 100.

    For bodies written before ``PolicyBody._weights_total_one_hundred`` existed, which
    were free to total anything. Those cannot be loaded any more, and falling back to the
    shipped default would silently throw away an operator's tuning and show them numbers
    they never chose.

    Rescaling is the right migration because the *exact* rescale is score-preserving: the
    score is ``100 * Σpressure / Σweight`` already, so dividing every weight by the same
    factor cannot move it. **Integer rounding can, by more than a point.** Largest-remainder
    bounds each weight's own error at 1, but those errors do not cancel in the score, since
    a rule that gained a point may be carrying pressure while one that lost a point is not:
    ``score' - score = Σ (w'ᵢ - wᵢ·100/T)·fillᵢ``. Weights ``(1, 1, 1, 5)`` become
    ``(13, 13, 12, 62)`` and move a score by a full point; six equal weights become
    ``17,17,17,17,16,16`` and move one by 1.33, which is enough to cross a condemn line.
    The drift is bounded by half the number of weighted rules, and no allocation does
    better, because which rules will carry pressure is unknowable at rescale time (weighting
    the remainder toward the larger weights does nothing at all in the equal-weight case).

    So a rescaled body is never adopted silently. The caller flags it
    (``PolicyRepair.RESCALED``), which makes ``profiles.ActivePolicy.repaired``
    true, degrades the scan, and opens the editor on it as an unsaved draft the operator
    reviews and re-saves themselves. ``tests/test_policy.py`` pins both the bound and the
    fact that a verdict near the line can move.

    Returns ``None`` when the body is unreadable for any *other* reason -- including valid
    JSON that is not an object at all -- so the caller can tell "needs rebalancing" from
    "genuinely broken" and never present a repaired body it does not understand. This must
    not raise: ``services.profiles.active_policy`` relies on it to keep the policy editor
    reachable.
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
        # objects either, so a `.get`/`["weight"]` on the wrong shape returns None here
        # rather than escaping a function whose whole job is not to raise.
        return None
    return body


def recover_rating_rules(raw: object) -> dict[str, Any] | None:
    """A stored body whose rating bar was lost when the bar moved off the gate row.

    The bar used to live on the RATING_FLOOR gate setting as ``threshold`` (tenths) plus
    ``secondary`` (minimum votes). It now lives in ``keep_rating_rules`` as one spec per
    rating source, and the move shipped no backfill. A body written before it still
    **validates cleanly** -- the gate keeps its now-meaningless numbers, ``keep_rating_rules``
    defaults to empty -- and an empty rule set makes ``RatingFloorGate`` abstain on every
    item with "No rating is set that would keep a title." So the operator's "keep anything
    at 7.5 from 1,000 votes" silently protects nothing, on a healthy, executable snapshot.
    Every install seeded before that move is in this state, whether or not anyone opened
    the editor: ``services.profiles`` persists the shipped default as a real row the first
    time a profile is saved.

    Returns the body with the equivalent IMDb bar synthesized, or ``None`` when there is
    nothing to recover. The caller flags it (``PolicyRepair
    .RATING_RULES_RESTORED``), which makes ``repaired`` true, degrades the scan, and opens
    the editor on it as an unsaved draft -- never a silent substitution of an operator's
    own safety value (rule 65).

    ``secondary`` is read here off the raw stored dict and is no longer a field on
    ``GateSetting``; migration ``e6f708192a3b`` deliberately skipped exactly the rows this
    still fires on, because the number is the only surviving copy of their bar. So the body
    handed back goes through ``drop_retired_gate_keys`` first: ``Frozen`` forbids extra keys,
    and a body returned with one raises into a caller that only logs, which would stop the
    recovery in silence.

    What it keys on, and why not ``schema_version``: affected bodies already carry
    ``schema_version: 2`` (it was 2 before the move too), so the version cannot tell them
    apart. The trigger is the raw key ``keep_rating_rules`` being **absent** -- an explicit
    ``[]`` is an operator who deliberately cleared their bars and must keep an empty set
    (rule 1) -- plus an ENABLED ``rating_floor`` gate carrying numbers the old validator
    would have accepted (``1 <= threshold <= 100``, ``secondary >= 1``). A disabled gate is
    left alone: nothing was protecting anything either way, so there is nothing to restore
    and no reason to degrade a scan over it. IMDb is the right source because it is the
    only one the old single-source gate ever read.

    Must not raise: ``services.profiles.active_policy`` keeps the policy editor reachable.
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


#: The two gates that became keep rules on Settings -> Lists. Retired as gates and kept as
#: ``GateId`` members so a stored explanation still decodes, but NOT in ``RETIRED_GATES``:
#: each was a live protection, so a body naming one is converted rather than stripped.
#: Declared once, because three readers must agree on the membership -- the conversion, the
#: strip beside it (a body could otherwise lose a gate without gaining its rule), and
#: ``scan_runner.build_gates``, which owes an operator reaching it a different sentence from
#: the one an unimplemented gate gets.
LIST_GATES_NOW_KEEP_RULES: frozenset[GateId] = frozenset({GateId.WHITELISTED, GateId.CURATED_LIST})
_LEGACY_LIST_GATES = frozenset(gate.value for gate in LIST_GATES_NOW_KEEP_RULES)

#: What a fresh install's two lists are made of, beside ``policy.DEFAULT_TAG_LIST_NAME`` and
#: ``policy.DEFAULT_IMDB_LIST_NAME``: the tag the seeded tag list carries and the preset its
#: IMDb list names. Spelled here for the reason those names are, and read by
#: ``list_config.DEFAULT_LISTS`` rather than respelled there, so the
#: row a fresh install seeds and the row a converted upgrade looks for are one declaration
#: (rule 104).
DEFAULT_KEEP_TAG = "reaper-keep"
DEFAULT_IMDB_PRESET = "top250"


def legacy_keep_tags(raw: object) -> tuple[str, ...]:
    """The *arr tags a legacy body was protecting on, blanks dropped.

    A body carrying no ``keep_tags`` key ran on the shipped default, so that is what it
    returns; an explicit empty list is an operator who cleared it, and returns nothing
    (rule 1). Empty therefore means "this body had no tag protection", which is what
    ``convert_list_protections`` reads it as, and it is also how the caller finds the
    registry row those tags became -- one derivation, since a resolver disagreeing with
    the conversion would name a list for a body that has no tags to convert (rule 104).
    """
    if not isinstance(raw, dict):
        return ()
    tags = raw.get("keep_tags", None)
    if not isinstance(tags, list):
        return (DEFAULT_KEEP_TAG,)
    return tuple(t for t in (str(x).strip() for x in tags) if t)


def _config_value(config_json: str | None, key: str) -> object:
    """One value out of a stored list config, or ``None`` for a body that will not parse.
    Unreadable reads as "not this row": it can only cost a conversion, and the half that does
    not convert keeps its gate and stops the scan loudly (rule 96's direction)."""
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
    """Which lists ``convert_list_protections``'s rules must name, out of the registry.

    ``rows`` are ``(source, name, config_json)`` oldest first. Returns the tag list's current
    name, the shipped IMDb list's, and every list the operator curates on Plex -- the three
    arguments the conversion takes. ``None`` means no such list, and the conversion then leaves
    that half's gate in place rather than converting it away.

    **Resolved by what a row HOLDS, never by age.** ``keep_tags`` is the legacy body's own
    tags, tested against each *arr-tag row's configured ones; the shipped preset is tested
    against each IMDb row's. Age was the rule until it turned out to be the operator's to
    change as well: delete the tag list this converts and the *arr-tag list they added for
    something else becomes the oldest of its source, so it inherited an outright keep that
    nothing gave it. Settings -> Lists reads the same conversion (``list_rules.usage``), so
    that list read "Keeps every title on it" while no rule mentioned it, and Remove could not
    take the rule off -- ``list_rules.detach_list`` declines to write a repaired policy.

    Tag matching is an OVERLAP, not an exact set: the upgrade migration unions both media
    types' tags into the one list, and an operator who adds a tag beside them has not stopped
    it being the list their tags became.

    Pure, and takes rows rather than a session, because the load shim reads them through
    SQLAlchemy and the upgrade migration through raw SQL -- and a second copy of this
    selection is exactly what drifted (rule 104).
    """
    wanted = {fold(t) for t in keep_tags if t.strip()}

    def carries_a_wanted_tag(config_json: str | None) -> bool:
        held = _config_value(config_json, "tags")
        if not isinstance(held, list):
            return False
        # Case-folded on both sides, the comparison every reader of a tag makes (rule 88).
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


#: A collection whose library kind Reaper cannot pin down keeps a rule on BOTH policies. A name
#: absent from ``own_list_media_scope``'s map reads as this too, so an empty map leaves every
#: rule where it was before this scoping existed. Public so the load shim and the upgrade
#: migration read one constant (rule 104).
BOTH_MEDIA_TYPES: frozenset[str] = frozenset({"movie", "tv"})

#: The policy media type each Plex library kind seeds a keep rule on. Plex names a TV library
#: "show"; a policy names that side "tv". Music and photo libraries never reach here --
#: ``PlexClient.video_sections`` surfaces only these two, and ``app_settings.set_plex_libraries``
#: stores what it returns.
_PLEX_KIND_TO_MEDIA: dict[str, str] = {"movie": "movie", "show": "tv"}


def library_media_types(
    libraries: Sequence[Mapping[str, object]],
) -> dict[str, frozenset[str]]:
    """Casefolded library title -> the media types libraries of that title span, from the synced
    ``plex_libraries`` setting rows (each a ``{"title", "kind", ...}`` dict).

    Two same-titled libraries of different kinds union to both, which ``own_list_media_scope``
    reads as "cannot tell" and so keeps a collection under that title on both policies. A kind
    that is neither ``movie`` nor ``show`` (never surfaced today) is skipped. Case-folded, the
    comparison every reader of a Plex name makes (rule 88)."""
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
    """For each Plex list the operator curates by hand, the media types a keep rule naming it may
    protect -- keyed by the list's name casefolded, the spelling every reader of a list name
    compares on (rule 88).

    A ``plex_collection`` lives in ONE Plex library, so it can only hold that library's one type;
    a ``plex_watchlist`` spans the account and can hold both. ``library_types`` maps a casefolded
    library title to the media types libraries of that title span (``library_media_types``, off
    the synced ``plex_libraries`` setting): a library never synced, one since renamed, or two
    same-titled libraries of different kinds all leave the title unknown or two-typed.

    Fail-open, the direction rules 65/91 require: this NARROWS a collection's rule to one policy
    only when its library is known and single-typed. An unknown, ambiguous, or unsynced library
    keeps BOTH -- a rule on the media type the collection cannot hold matches no item, so leaving
    it costs nothing, while dropping it on a lookup that could not answer would withdraw a live
    protection. A watchlist keeps both always. This is the whole reason scoping a collection is
    "harder than the IMDb half" (#545): the IMDb chart is statically movies-only, a collection's
    one type is its library's and nothing in the registry carries it.

    Two rows can share a casefolded key, so the types UNION rather than overwrite (rule 63: a
    key that is a display name always can collide). ``list_config.name`` is unique, but under a
    ``NOCASE`` collation and a ``func.lower`` pre-check that both fold ASCII only, while this key
    is a full-Unicode ``casefold`` -- so a non-ASCII case pair ("strasse" vs "stra"+eszett) is
    admitted as two rows that collapse here. Overwriting would scope the loser to the other's
    type and drop its rule off the policy it keeps on; the union keeps both, fail-open."""
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
        # Narrow only a library that is known and single-typed. ``held`` is None for an unsynced
        # or renamed library, and carries two types for same-titled movie and show libraries;
        # both keep the rule on both policies.
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
    """The media types a keep rule on this list can be AUTHORED for, the set the Policy editor's
    list picker offers it on. An empty set means offer on neither: the list's type is not known,
    so a rule on it could match nothing while reading as a protection (rule 38, #549).

    This decides whether to OFFER a NEW rule and never gates a deletion, so it fails CLOSED: a
    list whose type it cannot establish is withheld, not offered on a type it may not hold. That
    is the opposite of ``own_list_media_scope``, its twin (rule 72), which scopes a legacy
    protection being converted and must never drop a live rule, so an unreadable lookup there
    keeps BOTH. Where the type IS known the two agree; they part only on the unknown.

    Layered by how the type is known:
    - ``plex_watchlist`` spans the account, so both, sync or not.
    - ``imdb`` is stamped movie by every sync (``services.lists``), so movies, sync or not.
    - ``plex_collection`` lives in one library, whose kind gives the type without a sync
      (``library_types``). A library that is unknown, renamed, or two-typed leaves the kind
      unsettled, so it falls through to what a sync observed.
    - ``arr_tag`` (and the fallen-through collection) is known only from synced content: the
      types its members span, or -- verified but empty -- both, so a list the operator means to
      fill is protectable now. Never synced, so never read: withheld until a check runs."""
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
    """Whether ``convert_list_protections`` would fire on this body. Exposed so callers can
    decide whether resolving the target list names (a database read) is worth it, against
    the same trigger the conversion itself uses."""
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
    """A stored body from before every list protected through its own keep rule.

    Three legacy shapes, converted together because they were saved together:

    * ``keep_tags`` / ``keep_tags_match`` -- the *arr tags configured on Policy. The
      upgrade migration turns them into a list on Settings -> Lists; this rewrites the
      body's half: the keys leave (``Frozen`` forbids them), and an ENABLED ``whitelisted``
      gate becomes a keeps-it-outright ``on_list`` rule naming that list. A disabled gate
      converts to no rule -- the operator had the protection off, and the Lists screen now
      says "not used by your policy" where the switch used to be.
    * the ``whitelisted`` and ``curated_list`` gate rows leave the body. Both gates are
      retired (``gates.GateId``), and unlike ``RETIRED_GATES`` they were LIVE protections,
      so each enabled one converts to the equivalent rule rather than being dropped --
      dropping alone would silently withdraw cover, the failure this codebase exists to
      avoid.
    * ``on_curated_list`` rules re-spell as ``on_list``: the field was renamed when it
      widened from the shipped lists to every list. The value is kept -- it is the list's
      name either way.

    ``tag_list_name`` / ``imdb_list_name`` are the CURRENT names of the registry rows the
    new rules must point at; the caller reads them from the database because the operator
    may have renamed either. ``None`` means no such list exists, and that half converts to
    no rule rather than to a rule naming nothing (rule 25). The caller identifies those
    rows by what they HOLD -- these tags, that preset -- never by position, or a list the
    operator added for something else inherits the protection the moment the real one is
    deleted (``profiles._conversion_list_names``).

    ``media_type`` scopes the IMDb rule to the body it can hold. ``active_policy`` converts
    the movie and TV bodies separately, and the IMDb chart is movies only (Radarr's mirror,
    ``services/lists.py``), so its rule lands on the movie body alone -- a TV rule naming it
    can never match a season and would read as a protection the operator never chose (#539,
    rule 38). The curated_list gate protected nothing on a TV body for the same reason, so
    it strips there with no replacement. The tag list spans both libraries and its rule
    lands on both.

    ``collection_media_scope`` scopes each Plex collection's rule the same way, keyed by the
    collection name casefolded (``own_list_media_scope``). A collection lives in one Plex
    library, so it holds that library's one type; its rule lands only on the policy for that
    type. Fail-open: a name the map omits, or one the map keeps at both (an unsynced, renamed,
    or ambiguous library, and every watchlist), lands on both -- so ``None`` here is the
    behavior before this scoping existed (#545). A watchlist genuinely spans both and stays on
    both.

    Returns the converted body, or ``None`` when nothing is legacy-shaped. The caller
    flags the conversion (``PolicyRepair.LISTS_MIGRATED``), which makes
    ``repaired`` true, degrades the scan, and opens the editor on it as an unsaved draft:
    a protection moved between surfaces is a policy edit nobody has saved yet (rule 105).
    Must not raise, for the same reason every shim here must not.
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
    # An explicit empty tag list is an operator who cleared it, and converts to no rule
    # (rule 1). A body carrying no key at all ran on the shipped default tag, so it had a
    # live protection to carry over. Both readings live in ``legacy_keep_tags``, which the
    # caller resolving ``tag_list_name`` reads too.
    had_tags = bool(legacy_keep_tags(body))
    body.pop("keep_tags", None)
    body.pop("keep_tags_match", None)

    new_rules: list[dict[str, Any]] = []
    if legacy_gates.get("whitelisted"):
        # The gate covered every list the operator curates by hand -- the keep tags AND
        # the Plex collection and watchlist definitions -- so each existing one gets its
        # own rule, or an upgrade would quietly withdraw the collections' cover.
        if had_tags and tag_list_name:
            new_rules.append({"field": "on_list", "op": Op.EQ.value, "value": tag_list_name})
        scope = collection_media_scope or {}
        for name in collection_list_names:
            # A single-library collection's rule lands only on the policy for its library's
            # type; an unknown/ambiguous library keeps it on both (#545, fail-open).
            if media_type in scope.get(fold(name), BOTH_MEDIA_TYPES):
                new_rules.append({"field": "on_list", "op": Op.EQ.value, "value": name})
    # The IMDb chart is movies only, so its rule lands on the movie body alone (#539).
    if legacy_gates.get("curated_list") and imdb_list_name and media_type == "movie":
        new_rules.append({"field": "on_list", "op": Op.EQ.value, "value": imdb_list_name})

    def converts(gate_id: str) -> bool:
        """Whether this gate row may leave the body. A disabled row always may, and an
        enabled one only once its replacement rule exists: stripping an enabled gate whose
        target list is missing would withdraw a live protection with nothing in its place,
        so the row stays and ``build_gates`` refuses the scan loudly instead (rule 38)."""
        if not legacy_gates.get(gate_id):
            return True
        if gate_id == "whitelisted":
            return not had_tags or tag_list_name is not None
        # curated_list: on a TV body the IMDb chart protected nothing (movies only), so the
        # gate strips clean with no replacement -- else a missing IMDb list would refuse the
        # TV scan over a protection that never fired there (#539). On the movie body it
        # strips only once its replacement rule exists.
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
