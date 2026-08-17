#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Dump a Tautulli server's watch history for Reaper testing, with identity removed.

Run this against your own Tautulli and send the result to the Reaper developers. It writes
a gzipped JSON file holding the numbers Reaper's scoring reads, and none of the things that
say who you are: no titles, no usernames, no email addresses, no IP addresses, no file
paths, no server name.

Standalone by design. It imports nothing from Reaper and needs no pip install, so you can
read the whole thing before you run it. Python 3.11 or newer.

    python3 tautulli_anon_dump.py --url http://localhost:8181 --apikey YOUR_KEY
    python3 tautulli_anon_dump.py --url ... --apikey ... --dry-run   # show, write nothing

## What leaves your machine

Every record is built by copying named fields onto a fresh dict. Nothing is copied by
default, so a field a future Tautulli adds cannot leak by being forgotten. That matters
more than it sounds: a ``get_history`` row carries ``ip_address``, ``user``,
``friendly_name``, ``machine_id``, ``platform`` and ``player``; a ``get_users`` row carries
``email`` and ``username``; and an episode's ``media_info`` carries the full file path.

Identifiers become an HMAC token under a salt that stays on your machine (``--salt-file``,
default ``.tautulli_anon_salt.json`` beside the output). Nobody who receives the dump can
turn a token back into a rating key. You can, by running this tool again: the same salt
over the same library produces the same tokens. Keep the salt file if you might send a
second dump later. Reaper has checks that only show themselves across two scans, and they
need two dumps that agree on which item is which.

Timestamps all shift by one random whole-day offset, drawn once and stored with the salt.
Every interval between two events survives exactly, which is all the dormancy and rewatch
math reads, while the absolute clock is destroyed so the dump does not describe when you
sleep. Read ``reference_now`` from the dump rather than the current time when computing
"days since", because the offset is applied to it too.

## What is deliberately coarse

Sizes round to 100 MB and IMDb vote counts to two significant figures, because a precise
byte count or vote count identifies one file. Ratings, years and genres are kept as they
are, since none of them says anything about you.

## What this cannot carry, measured rather than assumed

**No TV sizes.** A show section's sweep rows report no ``file_size`` at all (0 of 200
populated on a real server, against 200 of 200 for movies), and an episode's size lives
only in a per-episode ``get_metadata`` call. Totalling one library's seasons that way cost
about 25,000 calls in testing. So a season carries a null size, which Reaper reads as
"could not measure" rather than as zero.

**No show status.** Tautulli exposes no continuing-or-ended flag, so rules resting on a
show having finished cannot be tested from this dump.

## The honest limit

This is pseudonymous, not anonymous. The dump still describes the shape of your library,
and a rare title with an unusual year, size and rating could be narrowed down by someone
determined who holds the full public catalog. Send it to people you are willing to trust
with that.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import hmac
import json
import secrets
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Lock
from typing import Any

FORMAT_VERSION = 1

IMDB_RATINGS_URL = "https://datasets.imdbws.com/title.ratings.tsv.gz"

#: Rows per page for a library sweep. Only a handful of calls either way, since a sweep row
#: is cheap and a library is thousands of items rather than hundreds of thousands.
PAGE = 1000

#: Rows per page of history, which is a different question with a different answer. What a
#: history page costs is almost entirely FIXED per request: measured against a live instance
#: at 425,983 rows, one page of 1,000 took 14.7s and one of 50,000 took 20.7s. So the page
#: size sets the whole runtime, and 1,000 spends 105 minutes where 25,000 spends 4.
#:
#: Not larger, though the curve keeps improving, because a page that times out is retried at
#: half the size (:meth:`Tautulli.paged`) and the reach that matters is the SMALLEST page
#: the code can end up choosing on a slower server than this one.
#:
#: Reaper's own sweep sits at 5,000 (``services.history_sync.PAGE_SIZE``) and that is not a
#: disagreement to reconcile: it allows 30s a request where this allows 120, and a 25k page
#: spent most of that smaller budget. The pair moves together or not at all.
HISTORY_PAGE = 25_000

#: Seconds a single request may take. Generous because of the fixed cost above: a 25,000-row
#: page took 13.9s here, and an instance answering three times slower still fits. This is
#: the number that lets HISTORY_PAGE be what it is.
TIMEOUT = 120

#: Concurrent requests. The per-item metadata sweep is thousands of independent GETs and is
#: otherwise the longest phase of a run. Eight is chosen to be unremarkable to a Tautulli
#: sharing a box with Plex, not to be fast on a big server; ``--jobs`` moves it.
JOBS = 8

#: How many items a dry run pulls per section. Small enough to finish in seconds, so
#: someone deciding whether to trust this tool can see real output before committing to a
#: full run.
DRY_RUN_ITEMS = 25

#: Half a year either way. Whole days, so weekday and hour-of-day patterns move together
#: rather than smearing into each other.
MAX_SHIFT_DAYS = 180


# --------------------------------------------------------------------------- transport


def http_open(target: str | urllib.request.Request, **kwargs: Any):
    """``urlopen`` with the scheme checked first.

    ``urlopen`` honors ``file:`` and every other scheme the opener knows, and the Tautulli
    address is typed by whoever runs this. The one place both fetches go through, so
    neither can be given a local path to read.
    """
    url = target if isinstance(target, str) else target.full_url
    if urllib.parse.urlparse(url).scheme not in ("http", "https"):
        raise ValueError(f"only http and https addresses are allowed, got {url.split(':')[0]!r}")
    return urllib.request.urlopen(target, **kwargs)  # noqa: S310 (scheme checked above)


class Tautulli:
    def __init__(self, base_url: str, api_key: str, *, insecure: bool = False) -> None:
        self.base = base_url.rstrip("/")
        self.key = api_key
        self.ctx: ssl.SSLContext | None = None
        if insecure:
            self.ctx = ssl.create_default_context()
            self.ctx.check_hostname = False
            self.ctx.verify_mode = ssl.CERT_NONE
        self.calls = 0
        self._counting = Lock()

    def __call__(self, cmd: str, *, timeout: int = TIMEOUT, **params: Any) -> Any:
        query = urllib.parse.urlencode({"apikey": self.key, "cmd": cmd, **params})
        url = f"{self.base}/api/v2?{query}"
        last: Exception | None = None
        for attempt in range(3):
            try:
                with http_open(url, timeout=timeout, context=self.ctx) as fh:
                    payload = json.loads(fh.read().decode("utf-8"))
                with self._counting:
                    self.calls += 1
                response = payload.get("response") or {}
                if response.get("result") != "success":
                    raise RuntimeError(f"{cmd}: {response.get('message') or 'no result'}")
                return response.get("data")
            except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
                last = exc
                time.sleep(1 + attempt)
        raise RuntimeError(f"{cmd} failed after 3 tries: {last}")

    def spread(self, work: Any, over: Any, *, jobs: int, note: Any = None) -> list[Any]:
        """``work`` applied to each of ``over``, in order, several requests in flight.

        Every call this fans out is an independent GET against a server that is normally on
        the same machine, and the serial version of this was the longest phase of a run.

        ``map`` returns in the order it was given, so the caller can zip the results back
        against its own list. The counter is only for the progress line, and it is read
        under the same lock it is written under because several workers reach it at once.
        """
        items = list(over)
        done = 0
        counting = Lock()

        def counted(item: Any) -> Any:
            nonlocal done
            result = work(item)
            with counting:
                done += 1
                at = done
            if note is not None and at % 250 == 0:
                note(f"    {at}/{len(items)}")
            return result

        with ThreadPoolExecutor(max_workers=jobs) as pool:
            return list(pool.map(counted, items))

    def paged(
        self,
        cmd: str,
        *,
        cap: int | None = None,
        page: int = PAGE,
        note: Any = None,
        **params: Any,
    ) -> list[dict[str, Any]]:
        """Every row of a paginated table endpoint, or the first ``cap`` of them.

        ``start`` advances by what came back rather than by what was asked for. A server
        answering with more rows than the page requested would otherwise leave the cursor
        behind the data and re-read the overlap forever.

        **A page that times out is retried at half the size, down to a floor**, so a server
        slower than the one ``HISTORY_PAGE`` was measured on degrades to a longer run rather
        than to no dump at all. The shrink is permanent for the rest of the walk: whatever
        made one page too slow is a property of the instance, not of that offset.
        """
        out: list[dict[str, Any]] = []
        start = 0
        while cap is None or len(out) < cap:
            length = page if cap is None else min(page, cap - len(out))
            try:
                data = self(cmd, start=start, length=length, **params)
            except RuntimeError:
                if page <= 250:
                    raise
                page = max(250, page // 2)
                if note is not None:
                    note(f"    a page took too long, retrying in {page}-row pages")
                continue
            rows = (data or {}).get("data") or []
            out.extend(rows)
            if note is not None:
                note(f"    {len(out)} rows so far")
            if len(rows) < length:
                break
            start += len(rows)
        return out

    def children(self, rating_key: int) -> list[dict[str, Any]]:
        """A show's seasons, or a season's episodes. Empty for an item with neither."""
        data = self("get_children_metadata", rating_key=rating_key)
        children = (data or {}).get("children_list") if isinstance(data, dict) else None
        return children if isinstance(children, list) else []


# --------------------------------------------------------------------------- masking


class Mask:
    """Tokens and the clock shift, both from a salt that never leaves the machine."""

    def __init__(self, path: Path) -> None:
        self.path = path
        if path.exists():
            saved = json.loads(path.read_text())
            self.salt = bytes.fromhex(saved["salt"])
            self.shift_days = int(saved["shift_days"])
            self.fresh = False
        else:
            self.salt = secrets.token_bytes(32)
            self.shift_days = secrets.randbelow(2 * MAX_SHIFT_DAYS + 1) - MAX_SHIFT_DAYS
            self.fresh = True
            path.write_text(
                json.dumps({"salt": self.salt.hex(), "shift_days": self.shift_days}, indent=2)
            )
            path.chmod(0o600)
        self.shift_seconds = self.shift_days * 86_400

    def token(self, kind: str, value: Any) -> str:
        digest = hmac.new(self.salt, f"{kind}:{value}".encode(), hashlib.sha256)
        return digest.hexdigest()[:12]

    def when(self, epoch: Any) -> int | None:
        seconds = as_int(epoch)
        if seconds is None or seconds <= 0:
            return None
        return seconds + self.shift_seconds


def as_int(value: Any) -> int | None:
    """Tautulli returns numbers as strings about half the time, and "" for absent."""
    if value is None or value == "":
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def as_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def round_size(size: int | None) -> int | None:
    """To the nearest 100 MB. A byte-exact size identifies one release."""
    return None if size is None else round(size / 100_000_000) * 100_000_000


def round_votes(votes: int) -> int:
    """Two significant figures, matching scripts/policy_lab_extract.py."""
    if votes < 100:
        return votes
    magnitude = 10 ** (len(str(votes)) - 2)
    return round(votes / magnitude) * magnitude


def imdb_id(meta: dict[str, Any]) -> str | None:
    """The IMDb id from the new-agent ``guids`` list, else out of the legacy ``guid``."""
    for guid in meta.get("guids") or []:
        if isinstance(guid, str) and guid.startswith("imdb://tt"):
            return guid.removeprefix("imdb://").split("?")[0]
    legacy = meta.get("guid")
    if isinstance(legacy, str) and "imdb://tt" in legacy:
        return "tt" + legacy.split("imdb://tt", 1)[1].split("?")[0].split("/")[0]
    return None


# --------------------------------------------------------------------------- collection


def collect_items(
    api: Tautulli,
    mask: Mask,
    sections: list[dict[str, Any]],
    *,
    cap: int | None,
    quick: bool,
    jobs: int,
    note,
) -> tuple[list[dict[str, Any]], dict[str, int], dict[str, str]]:
    """One record per movie and per show, the show rating keys, and the ids to enrich with.

    The sweep gives size, arrival and play counts in one paginated call. It gives neither
    genres nor the external id, so those cost one ``get_metadata`` per item, and on a real
    library that is thousands of requests and the longest phase of a run. They are
    independent of each other, so they go out several at a time. ``quick`` skips them
    entirely and gives up both fields.
    """
    items: list[dict[str, Any]] = []
    show_keys: dict[str, int] = {}
    wanted: dict[str, str] = {}
    for section in sections:
        section_id = as_int(section.get("section_id"))
        kind = section.get("section_type")
        if section_id is None:
            continue
        rows = api.paged(
            "get_library_media_info",
            cap=cap,
            section_id=section_id,
            order_column="added_at",
            order_dir="desc",
        )
        keyed = [(row, key) for row in rows if (key := as_int(row.get("rating_key"))) is not None]
        note(f"  {kind} section: {len(keyed)} items")

        records = []
        for row, key in keyed:
            token = mask.token(kind, key)
            records.append(
                {
                    "token": token,
                    "type": kind,
                    "section": mask.token("section", section_id),
                    "year": as_int(row.get("year")),
                    "size_bytes": round_size(as_int(row.get("file_size"))),
                    "added_at": mask.when(row.get("added_at")),
                    "last_played": mask.when(row.get("last_played")),
                    "play_count": as_int(row.get("play_count")) or 0,
                    "resolution": row.get("video_resolution") or None,
                }
            )
            if kind == "show":
                show_keys[token] = key

        if not quick and keyed:
            note(f"    fetching genres and ids, {jobs} at a time")
            fetched = api.spread(_metadata, [(api, key) for _, key in keyed], jobs=jobs, note=note)
            for record, meta in zip(records, fetched, strict=True):
                if meta is None:
                    record["metadata_failed"] = True
                    meta = {}
                record["genres"] = [g for g in (meta.get("genres") or []) if isinstance(g, str)]
                record["released"] = meta.get("originally_available_at") or None
                if tconst := imdb_id(meta):
                    wanted[record["token"]] = tconst
        items.extend(records)
    return items, show_keys, wanted


def _metadata(work: tuple[Tautulli, int]) -> dict[str, Any] | None:
    """One item's metadata, or ``None`` where the instance would not answer for it.

    Returning rather than raising keeps one unreadable item from ending the whole sweep,
    and the record it belongs to is marked so the gap is visible in the dump.
    """
    api, key = work
    try:
        return api("get_metadata", rating_key=key) or {}
    except RuntimeError:
        return None


def collect_seasons(
    api: Tautulli, mask: Mask, show_keys: dict[str, int], *, jobs: int, note
) -> list[dict[str, Any]]:
    """Season structure per show, with each season's episode count.

    Season pruning ranks seasons and holds the one a viewer is part-way through, and
    telling "part-way through season 2" from "finished it" needs the episode count. That
    count costs one call per season on top of one per show. Sizes are not here: see the
    module docstring for what that measurement cost.
    """
    note(f"  {len(show_keys)} shows, {jobs} at a time")
    walked = api.spread(
        _one_show,
        [(api, mask, token, key) for token, key in show_keys.items()],
        jobs=jobs,
        note=note,
    )
    return [show for show in walked if show is not None]


def _one_show(work: tuple[Tautulli, Mask, str, int]) -> dict[str, Any] | None:
    """One show's seasons and their episode counts, or ``None`` if it would not answer.

    A show is walked whole by one worker rather than fanning its seasons out separately.
    Two levels of pool nest badly for no gain: there are hundreds of shows to spread over
    already, and a per-season fan-out would multiply the requests in flight by whatever the
    widest show happens to be.
    """
    api, mask, token, key = work
    try:
        seasons = api.children(key)
    except RuntimeError:
        return None
    entries = []
    for child in seasons:
        season_key = as_int(child.get("rating_key"))
        if season_key is None:
            continue
        try:
            episodes = len(api.children(season_key)) or None
        except RuntimeError:
            episodes = None
        entries.append(
            {
                "token": mask.token("season", season_key),
                "number": as_int(child.get("media_index")),
                "added_at": mask.when(child.get("added_at")),
                "last_played": mask.when(child.get("last_viewed_at")),
                "episodes": episodes,
            }
        )
    return {"show": token, "seasons": entries}


def collect_plays(
    api: Tautulli, mask: Mask, *, cap: int | None, note
) -> tuple[list[dict[str, Any]], int | None]:
    """Every finished play, filtered and typed the way ``services.history_sync`` does it.

    Matching that mapping field for field is the point. A dump that keeps rows Reaper's own
    mirror drops, or types a missing value differently, replays into verdicts a real scan
    would not produce, and the difference would be read as an engine finding.

    **``grouping=0`` is the whole reason a play is a play here.** Tautulli groups consecutive
    plays of the same item by default, and the default is what a caller that says nothing
    gets: asking without it returned 309,013 rows on an instance holding 425,983, a quarter
    of the history folded away. Those are exactly the rows a rewatch is counted from
    (``services.rewatch.viewing_count`` clusters plays into viewings itself), so a grouped
    dump does not merely lose rows, it reports a habitual rewatcher as a single viewing.
    """
    # What the instance says it holds, asked for before the walk and compared with what the
    # walk got. A silent shortfall is how the grouping default hid: the run looked entirely
    # healthy and simply carried a quarter less history than the server had. Nothing here can
    # know WHY a walk came up short, so it records the pair and lets the reader see it.
    reported = None
    if cap is None:
        first = api("get_history", length=1, start=0, grouping=0) or {}
        reported = as_int(first.get("recordsFiltered")) or as_int(first.get("recordsTotal"))

    rows = api.paged("get_history", cap=cap, page=HISTORY_PAGE, grouping=0, note=note)
    if reported is not None and len(rows) < reported * 0.99:
        note(f"    WARNING: collected {len(rows)} of the {reported} rows this server reports")
    plays = []
    live = 0
    for row in rows:
        # A null row_id is a session still playing, verified against a real instance. Not
        # history yet, and history_sync skips it, so a dump that kept it would carry plays
        # Reaper never stores.
        if row.get("row_id") is None:
            live += 1
            continue
        started = mask.when(row.get("date") or row.get("started"))
        key = as_int(row.get("rating_key"))
        if started is None or key is None or row.get("user_id") is None:
            continue
        media_type = row.get("media_type")
        show_key = as_int(row.get("grandparent_rating_key"))
        season_key = as_int(row.get("parent_rating_key"))
        plays.append(
            {
                "item": mask.token("episode" if media_type == "episode" else "movie", key),
                "show": mask.token("show", show_key) if show_key is not None else None,
                "season": mask.token("season", season_key) if season_key is not None else None,
                "user": mask.token("user", row.get("user_id")),
                "at": started,
                "type": str(media_type or "unknown"),
                # Coerced to 0, matching history_sync, whose column is NOT NULL. Keeping a
                # null here would be more honest and would make the dump disagree with the
                # mirror, which is worse.
                "percent_complete": as_int(row.get("percent_complete")) or 0,
                # Null means Tautulli did not say, which is NOT 0.0. Coercing it makes a
                # viewer look further behind than they are, and costs a season the
                # protection it should have had.
                "watched_status": as_float(row.get("watched_status")),
                "episode": as_int(row.get("media_index")),
                "season_number": as_int(row.get("parent_media_index")),
            }
        )
    note(f"  {len(plays)} plays" + (f", {live} still playing and skipped" if live else ""))
    return plays, reported


def collect_users(api: Tautulli, mask: Mask) -> list[dict[str, Any]]:
    return [
        {
            "token": mask.token("user", row.get("user_id")),
            "keep_history": bool(row.get("keep_history")),
            "is_active": bool(row.get("is_active")),
            "is_home_user": bool(row.get("is_home_user")),
        }
        for row in (api("get_users") or [])
        if row.get("user_id") is not None
    ]


def add_ratings(items: list[dict[str, Any]], wanted: dict[str, str], *, note) -> int:
    """Join the public IMDb ratings dataset locally, then drop the ids that did the join.

    The ids are the one field that would make the dump reversible by anyone who receives
    it, so the lookup happens here and only the two numbers survive it.
    """
    needed = set(wanted.values())
    if not needed:
        return 0
    note(f"  looking up {len(needed)} titles in the IMDb ratings dataset")
    ratings: dict[str, tuple[int, int]] = {}
    request = urllib.request.Request(IMDB_RATINGS_URL, headers={"User-Agent": "reaper-dump"})
    with http_open(request, timeout=180) as raw, gzip.open(raw, "rt") as fh:
        next(fh, None)
        for line in fh:
            tconst, rating, votes = line.rstrip("\n").split("\t")
            if tconst in needed:
                ratings[tconst] = (round(float(rating) * 10), round_votes(int(votes)))
    hits = 0
    for item in items:
        found = ratings.get(wanted.get(item["token"], ""))
        if found is not None:
            item["imdb_rating_tenths"], item["imdb_votes"] = found
            hits += 1
    return hits


def orphans(items: list[dict[str, Any]], plays: list[dict[str, Any]]) -> dict[str, int]:
    """Distinct things played that the library no longer holds, beside the ones it does.

    A rating key in the history is not a promise the item still exists: Tautulli keeps a
    play after the file is gone, and a re-added file gets a new key while its old plays stay
    where they were (``services.watch_evidence``). Both read the same way from here, which
    is why this counts rather than explains.
    """
    movies = {i["token"] for i in items if i["type"] == "movie"}
    shows = {i["token"] for i in items if i["type"] == "show"}
    played_movies = {p["item"] for p in plays if p["type"] == "movie"}
    played_shows = {p["show"] for p in plays if p["show"]}
    return {
        "movies": len(played_movies - movies),
        "movies_played": len(played_movies),
        "shows": len(played_shows - shows),
        "shows_played": len(played_shows),
    }


# --------------------------------------------------------------------------- entry point


def build(
    api: Tautulli, mask: Mask, *, cap: int | None, quick: bool, jobs: int = JOBS, note
) -> dict[str, Any]:
    note("libraries")
    sections = [
        s for s in (api("get_libraries") or []) if s.get("section_type") in ("movie", "show")
    ]

    note("items" + (" (quick: no genres, no ratings)" if quick else ""))
    items, show_keys, wanted = collect_items(
        api, mask, sections, cap=cap, quick=quick, jobs=jobs, note=note
    )

    note("seasons")
    seasons = collect_seasons(api, mask, show_keys, jobs=jobs, note=note)

    note("history")
    plays, reported_rows = collect_plays(api, mask, cap=cap, note=note)

    note("users")
    users = collect_users(api, mask)

    rated = 0
    ratings_failed = None
    if wanted:
        try:
            rated = add_ratings(items, wanted, note=note)
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            ratings_failed = type(exc).__name__
            note(f"  could not reach the IMDb dataset ({ratings_failed}), continuing without it")

    starts = [p["at"] for p in plays]
    return {
        "format": FORMAT_VERSION,
        "reference_now": int(time.time()) + mask.shift_seconds,
        "history_begins_at": min(starts) if starts else None,
        # What the server said it held, beside what was collected. A reader can tell a dump
        # that saw the whole history from one that came up short, without trusting this tool.
        "history_rows_reported": reported_rows,
        "clock_shifted": True,
        "partial": cap is not None,
        "season_sizes": False,
        "ratings_failed": ratings_failed,
        "counts": {
            "items": len(items),
            "shows": len(seasons),
            "seasons": sum(len(s["seasons"]) for s in seasons),
            "plays": len(plays),
            "users": len(users),
            "rated": rated,
            "api_calls": api.calls,
            # How much of this history is about media the library no longer holds. Measured
            # at 39% of watched movies and 46% of watched shows on the first real library
            # this ran against, which is what a history outliving its library looks like and
            # not a fault. It belongs in the summary because it decides what the dump can be
            # asked: a signal read off items still present is answering a question about the
            # survivors, and the number here says how much of the past that leaves out.
            "played_but_gone": orphans(items, plays),
        },
        "genre_histogram": dict(
            Counter(g for i in items for g in i.get("genres") or []).most_common()
        ),
        "items": items,
        "seasons": seasons,
        "plays": plays,
        "users": users,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Dump Tautulli watch history for Reaper testing, with identity removed.",
    )
    parser.add_argument(
        "--url", required=True, help="Tautulli base URL, e.g. http://localhost:8181"
    )
    parser.add_argument("--apikey", required=True, help="Tautulli API key, from its settings")
    parser.add_argument("--out", default="reaper-dump.json.gz", help="where to write the dump")
    parser.add_argument("--salt-file", default=None, help="keep this to make a later dump match")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help=f"pull {DRY_RUN_ITEMS} items per library, print them, write nothing",
    )
    parser.add_argument(
        "--quick", action="store_true", help="skip per-item lookups: no genres, no IMDb ratings"
    )
    parser.add_argument(
        "--jobs", type=int, default=JOBS, help=f"requests in flight at once (default {JOBS})"
    )
    parser.add_argument("--insecure", action="store_true", help="accept a self-signed certificate")
    args = parser.parse_args(argv)

    out = Path(args.out)
    salt_file = Path(args.salt_file) if args.salt_file else out.parent / ".tautulli_anon_salt.json"
    mask = Mask(salt_file)

    def note(message: str) -> None:
        print(message, flush=True)

    # Said before the run, not after it. This covers what everyone on the server watched,
    # and the person typing the command is only one of them.
    note("This covers what everyone on your server watched, with the names taken off.")
    note("Nobody else's name, address or viewing times can be read back out of it.")
    note("")

    if mask.fresh:
        note(f"New salt written to {salt_file}. Keep it to send a matching dump later.")
    else:
        note(f"Reusing the salt in {salt_file}, so this dump matches your last one.")

    api = Tautulli(args.url, args.apikey, insecure=args.insecure)
    started = time.monotonic()
    dump = build(
        api,
        mask,
        cap=DRY_RUN_ITEMS if args.dry_run else None,
        quick=args.quick,
        jobs=max(1, args.jobs),
        note=note,
    )
    counts = dump["counts"]

    note("")
    note(f"{counts['items']} items, {counts['seasons']} seasons, {counts['plays']} plays")
    note(f"{counts['rated']} items matched an IMDb rating")
    note(f"{counts['api_calls']} API calls in {time.monotonic() - started:.0f}s")

    if args.dry_run:
        note("")
        note("Dry run. Nothing was written. One record of each kind:")
        for label in ("items", "seasons", "plays", "users"):
            sample = dump[label][:1]
            note(f"  {label}: {json.dumps(sample[0], indent=2) if sample else 'none'}")
        return 0

    with gzip.open(out, "wb") as fh:
        fh.write(json.dumps(dump, separators=(",", ":")).encode("utf-8"))
    note("")
    note(f"Wrote {out} ({out.stat().st_size / 1_000_000:.1f} MB)")
    note("It holds no titles, usernames, email addresses, IP addresses or file paths.")
    note("Check it yourself first: run again with --dry-run, or unzip this and read it.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
