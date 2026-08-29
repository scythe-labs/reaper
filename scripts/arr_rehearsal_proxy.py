# SPDX-License-Identifier: AGPL-3.0-or-later
"""A development-only proxy that rehearses a real Reaper reap with no file ever deleted.

It sits between Reaper and one real Sonarr or Radarr instance. Point Reaper's Radarr or
Sonarr URL at this proxy instead of the real host, arm deletion, and run a real reap: the
real executor send path, the real transport guard, the real interlocks, and the real
verification reads all run exactly as they would against the real server. This proxy
answers the handful of calls that actually remove something without ever forwarding them
upstream, so the upstream library never loses a file.

Run one proxy per upstream instance:

    uv run python scripts/arr_rehearsal_proxy.py --upstream http://<real-radarr>:7878 --port 7878
    uv run python scripts/arr_rehearsal_proxy.py --upstream http://<real-sonarr>:8989 --port 8989

Then point Reaper's Radarr or Sonarr URL, in Settings, at this proxy's host and port for
the session. The proxy does not need to be told which *arr it is fronting: Radarr's and
Sonarr's own endpoint paths never collide, so one script covers both.

The safety property: every GET is forwarded to the upstream untouched, so a scan behaves
exactly as it would against the real server. A write that actually removes something
(a movie delete, a season's episode-file delete, the Sonarr unmonitor that must precede
it) is never forwarded. It is faked in memory instead, and the fake is stateful enough
that a later read through this same proxy sees the fake removal, which is what lets the
executor's own post-delete verification reads pass. Any other write this proxy does not
recognize is refused outright, loudly, with a 501, rather than being guessed at and
possibly sent upstream by accident.

All state lives in memory and is gone when the proxy restarts. Reaper's own journal and
history rows, and its rolling-caps bookkeeping, still record the rehearsal run as if it
were real, because none of that lives here. Nothing was actually removed upstream, so
restarting the proxy (or pointing Reaper back at the real host directly) makes the next
scan see every "removed" item again, exactly as if the run had never happened.
"""

from __future__ import annotations

import argparse
import json
import re
from collections.abc import Awaitable, Callable, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import httpx
import uvicorn
from fastapi import FastAPI, Request, Response

# Methods that never change remote state, so they are always forwarded as-is.
_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})

# Headers that describe this one hop, never the payload, so they are never copied from an
# incoming request onto the outgoing one, or from the upstream's response onto ours.
# ``content-length`` and ``content-encoding`` are added to the classic hop-by-hop set on
# purpose: httpx already decompresses a gzipped upstream body before we ever see it, and
# Starlette recomputes a correct ``content-length`` for whatever body we actually send.
_HOP_BY_HOP = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailer",
        "trailers",
        "transfer-encoding",
        "upgrade",
        "content-length",
        "content-encoding",
    }
)

# Every path this proxy treats specially, matched against the tail of the request path so
# a custom ``api_path_prefix`` in front of it (Reaper lets an instance set one) never
# matters. Radarr's and Sonarr's own paths never collide, which is what lets one script
# front either kind with no configuration telling it which.
_RADARR_MOVIE_BY_ID = re.compile(r"/movie/(?P<id>\d+)/?$")
_RADARR_MOVIE_LIST = re.compile(r"/movie/?$")
_RADARR_EXCLUSIONS = re.compile(r"/exclusions/?$")

_SONARR_SERIES_BY_ID = re.compile(r"/series/(?P<id>\d+)/?$")
_SONARR_SERIES_LIST = re.compile(r"/series/?$")
_SONARR_SEASONPASS = re.compile(r"/seasonpass/?$")
_SONARR_EPISODEFILE_BULK = re.compile(r"/episodefile/bulk/?$")
_SONARR_EPISODEFILE_BY_ID = re.compile(r"/episodefile/(?P<id>\d+)/?$")
_SONARR_EPISODEFILE_LIST = re.compile(r"/episodefile/?$")

# Shared by both kinds: neither Radarr's nor Sonarr's client in this codebase calls it
# today (see the module docstring's report for the grep that checked), but it is the
# refresh/rescan endpoint an operator's own *arr UI can still reach while pointed at this
# proxy, so a POST to it is faked rather than left to fall through to the write refusal.
_ARR_COMMAND = re.compile(r"/command/?$")


@dataclass
class ProxyState:
    """Everything this proxy remembers about the run it is rehearsing.

    One instance per proxy process, built once at startup and read and written by every
    request after that. Nothing here is durable: a restart clears it, which is the
    intended reset (see the module docstring).
    """

    upstream: str
    verbose: bool
    client: httpx.AsyncClient

    # Radarr. Keyed by movie id, the id every call in this proxy's scope addresses a
    # movie by.
    radarr_deleted: dict[int, dict[str, Any]] = field(default_factory=dict)
    radarr_tmdb: dict[int, int] = field(default_factory=dict)
    """A movie id's tmdbId, learned opportunistically from any forwarded read that
    carried one. The executor always reads a movie before deleting it, so this is
    normally already filled by the time the delete arrives; see ``_probe_tmdb_id`` for
    the fallback when it is not."""
    radarr_exclusions: dict[int, dict[str, Any]] = field(default_factory=dict)
    """Fake import exclusions, keyed by tmdbId, appended to whatever the real upstream
    exclusion list answers, so the executor's post-delete exclusion poll passes."""

    # Sonarr. Keyed by episode file id and by series id respectively.
    sonarr_deleted_files: set[int] = field(default_factory=set)
    sonarr_monitored: dict[int, dict[int, bool]] = field(default_factory=dict)
    """series id -> {season number: monitored}, remembered from a faked unmonitor
    write and never actually sent upstream. Every later series read is patched with
    this before it goes back to Reaper."""

    next_command_id: int = 0


def _log(message: str) -> None:
    """Print one line, always. Used for a faked mutation and a refused write, both of
    which the operator needs to see whether or not ``--verbose`` was passed."""
    print(message, flush=True)


def _strip_hop_by_hop(headers: Mapping[str, str]) -> dict[str, str]:
    return {name: value for name, value in headers.items() if name.lower() not in _HOP_BY_HOP}


def _response_from(resp: httpx.Response) -> Response:
    """A verbatim response: status, body and content-type intact, hop-by-hop stripped."""
    return Response(
        content=resp.content, status_code=resp.status_code, headers=_strip_hop_by_hop(resp.headers)
    )


def _json_response(status_code: int, body: Any) -> Response:
    return Response(
        content=json.dumps(body).encode("utf-8"),
        status_code=status_code,
        media_type="application/json",
    )


def _query_bool(request: Request, name: str) -> bool:
    raw = request.query_params.get(name)
    return (raw or "").strip().lower() in {"1", "true", "yes"}


async def _json_body(request: Request) -> Any:
    raw = await request.body()
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except ValueError:
        return {}


def _remember_tmdb(state: ProxyState, movie_id: int, row: dict[str, Any]) -> None:
    tmdb = row.get("tmdbId")
    if isinstance(tmdb, int) and tmdb > 0:
        state.radarr_tmdb[movie_id] = tmdb


def _patch_monitored(state: ProxyState, series_id: int, series: dict[str, Any]) -> None:
    """Overlay this series' remembered per-season monitored flags onto a real read.

    The read itself always comes from the real upstream; only the ``monitored`` fields
    this proxy's own faked unmonitor write changed are overwritten, in place."""
    overrides = state.sonarr_monitored.get(series_id)
    seasons = series.get("seasons")
    if not overrides or not isinstance(seasons, list):
        return
    for season in seasons:
        if not isinstance(season, dict):
            continue
        number = season.get("seasonNumber")
        if isinstance(number, int) and number in overrides:
            season["monitored"] = overrides[number]


async def _forward(state: ProxyState, request: Request) -> httpx.Response:
    """Send one request upstream, verbatim: method, path, query string, body and headers."""
    body = await request.body()
    headers = _strip_hop_by_hop(dict(request.headers))
    headers.pop("host", None)
    # Let this proxy's own client negotiate compression; forwarding Reaper's original
    # Accept-Encoding would fight with the transparent decompression httpx already does
    # on the response before this function ever sees it.
    headers.pop("accept-encoding", None)
    return await state.client.request(
        request.method,
        request.url.path,
        params=request.query_params.multi_items(),
        content=body or None,
        headers=headers,
    )


async def _maybe_patch(method: str, path: str, resp: httpx.Response, state: ProxyState) -> Response:
    """Apply this proxy's read-side illusion to a forwarded GET, or pass it through.

    Every branch here answers a read the executor's own verification depends on: a
    deleted movie or episode file must now read as gone, an unmonitored season must
    read as unmonitored, and the exclusion list must show the exclusion this proxy
    faked adding. Anything else is returned exactly as the upstream sent it.
    """
    if (
        method != "GET"
        or resp.status_code >= 400
        or "json" not in resp.headers.get("content-type", "").lower()
    ):
        return _response_from(resp)
    try:
        data = resp.json()
    except ValueError:
        return _response_from(resp)

    if match := _RADARR_MOVIE_BY_ID.search(path):
        movie_id = int(match.group("id"))
        if isinstance(data, dict):
            _remember_tmdb(state, movie_id, data)
        if movie_id in state.radarr_deleted:
            return _json_response(404, {"message": "NotFoundException"})
        return _json_response(resp.status_code, data)

    if _RADARR_MOVIE_LIST.search(path) and isinstance(data, list):
        for row in data:
            if isinstance(row, dict) and isinstance(row.get("id"), int):
                _remember_tmdb(state, row["id"], row)
        kept = [
            row
            for row in data
            if not (isinstance(row, dict) and row.get("id") in state.radarr_deleted)
        ]
        return _json_response(resp.status_code, kept)

    if _RADARR_EXCLUSIONS.search(path) and isinstance(data, list):
        return _json_response(resp.status_code, [*data, *state.radarr_exclusions.values()])

    if match := _SONARR_SERIES_BY_ID.search(path):
        if isinstance(data, dict):
            _patch_monitored(state, int(match.group("id")), data)
        return _json_response(resp.status_code, data)

    if _SONARR_SERIES_LIST.search(path) and isinstance(data, list):
        for row in data:
            if isinstance(row, dict) and isinstance(row.get("id"), int):
                _patch_monitored(state, row["id"], row)
        return _json_response(resp.status_code, data)

    if match := _SONARR_EPISODEFILE_BY_ID.search(path):
        if int(match.group("id")) in state.sonarr_deleted_files:
            return _json_response(404, {"message": "NotFoundException"})
        return _json_response(resp.status_code, data)

    if _SONARR_EPISODEFILE_LIST.search(path) and isinstance(data, list):
        kept = [
            row
            for row in data
            if not (isinstance(row, dict) and row.get("id") in state.sonarr_deleted_files)
        ]
        return _json_response(resp.status_code, kept)

    return _response_from(resp)


async def _probe_tmdb_id(
    request: Request, match: re.Match[str], state: ProxyState, movie_id: int
) -> int | None:
    """A read-only upstream GET to learn a movie's tmdbId, when nothing cached it yet.

    The executor always reads the movie before deleting it (the tmdbId cannot be read
    after a real delete), so ``state.radarr_tmdb`` normally already has it by the time a
    delete arrives. This only runs when it does not, such as a DELETE fired straight from
    curl without that earlier read.
    """
    prefix = request.url.path[: match.start()]
    try:
        resp = await state.client.get(f"{prefix}/movie/{movie_id}")
    except httpx.HTTPError:
        return None
    if resp.status_code >= 400:
        return None
    try:
        body = resp.json()
    except ValueError:
        return None
    tmdb = body.get("tmdbId") if isinstance(body, dict) else None
    return tmdb if isinstance(tmdb, int) and tmdb > 0 else None


async def _fake_radarr_delete_movie(
    request: Request, match: re.Match[str], state: ProxyState
) -> Response:
    movie_id = int(match.group("id"))
    delete_files = _query_bool(request, "deleteFiles")
    add_exclusion = _query_bool(request, "addImportExclusion")
    tmdb_id = state.radarr_tmdb.get(movie_id)
    if add_exclusion and tmdb_id is None:
        tmdb_id = await _probe_tmdb_id(request, match, state, movie_id)
    state.radarr_deleted[movie_id] = {
        "delete_files": delete_files,
        "add_exclusion": add_exclusion,
        "tmdb_id": tmdb_id,
    }
    if add_exclusion and tmdb_id is not None:
        state.radarr_exclusions[tmdb_id] = {
            "id": -movie_id,
            "tmdbId": tmdb_id,
            "movieTitle": f"rehearsal-{movie_id}",
            "movieYear": 0,
        }
    _log(
        f"[FAKED] Radarr DELETE movie {movie_id}: deleteFiles={delete_files} "
        f"addImportExclusion={add_exclusion} tmdbId={tmdb_id} -- nothing sent upstream"
    )
    return _json_response(200, {})


async def _fake_sonarr_unmonitor(
    request: Request, match: re.Match[str], state: ProxyState
) -> Response:
    payload = await _json_body(request)
    series_entries = payload.get("series") if isinstance(payload, dict) else None
    touched: int | None = None
    if isinstance(series_entries, list):
        for entry in series_entries:
            if not isinstance(entry, dict):
                continue
            series_id, seasons = entry.get("id"), entry.get("seasons")
            if not isinstance(series_id, int) or not isinstance(seasons, list):
                continue
            touched = series_id
            overrides = state.sonarr_monitored.setdefault(series_id, {})
            for season in seasons:
                if isinstance(season, dict) and isinstance(season.get("seasonNumber"), int):
                    overrides[season["seasonNumber"]] = bool(season.get("monitored"))
    _log(
        f"[FAKED] Sonarr POST seasonpass series={touched} "
        f"seasons={state.sonarr_monitored.get(touched)} -- nothing sent upstream"
    )
    if touched is None:
        return _json_response(200, {})

    # Echo the series back the way Sonarr would, patched with what was just faked.
    prefix = request.url.path[: match.start()]
    body: Any = {}
    try:
        resp = await state.client.get(f"{prefix}/series/{touched}")
        if resp.status_code < 400:
            body = resp.json()
    except (httpx.HTTPError, ValueError):
        body = {}
    if isinstance(body, dict):
        _patch_monitored(state, touched, body)
    return _json_response(200, body)


async def _fake_sonarr_delete_files(
    request: Request, match: re.Match[str], state: ProxyState
) -> Response:
    del match
    payload = await _json_body(request)
    ids = payload.get("episodeFileIds") if isinstance(payload, dict) else None
    recorded = [value for value in ids if isinstance(value, int)] if isinstance(ids, list) else []
    state.sonarr_deleted_files.update(recorded)
    _log(f"[FAKED] Sonarr DELETE episodefile/bulk: ids={recorded} -- nothing sent upstream")
    return _json_response(200, {})


async def _fake_command(request: Request, match: re.Match[str], state: ProxyState) -> Response:
    del match
    payload = await _json_body(request)
    name = payload.get("name") if isinstance(payload, dict) else None
    state.next_command_id += 1
    _log(f"[FAKED] POST {request.url.path}: command name={name!r} -- nothing sent upstream")
    now = datetime.now(UTC).isoformat()
    return _json_response(
        201,
        {
            "id": state.next_command_id,
            "name": name or "unknown",
            "status": "completed",
            "queued": now,
            "started": now,
            "ended": now,
        },
    )


_MutationHandler = Callable[[Request, "re.Match[str]", ProxyState], Awaitable[Response]]

# Every write this proxy fakes instead of refusing, in the order they are tried. The
# regexes never overlap (a list path never also matches a by-id or bulk path), so order
# does not decide correctness, only which handler's log line an operator sees first.
_MUTATION_HANDLERS: list[tuple[frozenset[str], re.Pattern[str], _MutationHandler]] = [
    (frozenset({"DELETE"}), _RADARR_MOVIE_BY_ID, _fake_radarr_delete_movie),
    (frozenset({"POST"}), _SONARR_SEASONPASS, _fake_sonarr_unmonitor),
    (frozenset({"DELETE"}), _SONARR_EPISODEFILE_BULK, _fake_sonarr_delete_files),
    (frozenset({"POST"}), _ARR_COMMAND, _fake_command),
]


async def _handle(request: Request) -> Response:
    state: ProxyState = request.app.state.proxy
    method, path = request.method.upper(), request.url.path

    if method in _SAFE_METHODS:
        try:
            resp = await _forward(state, request)
        except httpx.HTTPError as exc:
            _log(f"[ERROR] {method} {path}: upstream unreachable: {exc}")
            return _json_response(502, {"detail": "arr-rehearsal-proxy: upstream unreachable"})
        if state.verbose:
            _log(f"[FORWARD] {method} {path} -> {resp.status_code}")
        return await _maybe_patch(method, path, resp, state)

    for methods, pattern, handler in _MUTATION_HANDLERS:
        if method in methods and (match := pattern.search(path)):
            return await handler(request, match, state)

    _log(f"[REFUSED] {method} {path}: not a rehearsed mutation, nothing sent upstream")
    return _json_response(
        501, {"detail": f"arr-rehearsal-proxy refuses this write: {method} {path}"}
    )


def build_app(upstream: str, *, verbose: bool = False) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):  # type: ignore[no-untyped-def]
        async with httpx.AsyncClient(base_url=upstream.rstrip("/"), timeout=30.0) as client:
            app.state.proxy = ProxyState(upstream=upstream, verbose=verbose, client=client)
            yield

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)

    @app.api_route(
        "/{full_path:path}", methods=["GET", "HEAD", "OPTIONS", "POST", "PUT", "PATCH", "DELETE"]
    )
    async def catch_all(full_path: str, request: Request) -> Response:
        del full_path  # request.url.path already carries the exact original path
        return await _handle(request)

    return app


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Development-only proxy that rehearses a real Reaper reap against Radarr or "
            "Sonarr with no file ever deleted upstream. See the module docstring."
        )
    )
    parser.add_argument(
        "--upstream", required=True, help="the real instance, e.g. http://radarr.local:7878"
    )
    parser.add_argument("--port", required=True, type=int, help="port this proxy listens on")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--verbose", action="store_true", help="also log every forwarded read")
    args = parser.parse_args()

    app = build_app(args.upstream, verbose=args.verbose)
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
