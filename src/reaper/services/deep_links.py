# SPDX-License-Identifier: AGPL-3.0-or-later
"""Deep links from a candidate to the tools that manage it, and to the rating sites.

The why-panel offers several jumps: the title opens the item in Plex Web, pills open it
in Tautulli, Seerr and the managing Radarr/Sonarr, and the ratings chips open the site
each number came from. Every URL is computed here, server-side, from coordinates frozen
at scan time -- the frontend never learns the media_key grammar, and the one parser
(``MediaRef.parse``) stays the only one.

Every link degrades to ``None`` when a coordinate is missing (an unmatched item has no
rating key; a pre-rescan row has no tmdb id; a deleted instance has no base URL). The
UI hides a missing link; it never renders a broken one.

Route notes, verified against the apps' own web UIs:

* Radarr details live at ``/movie/{tmdbId}`` -- the TMDb id, NOT Radarr's internal id
  (which is what media_key carries, and which does not resolve in the web UI).
* Sonarr details live at ``/series/{titleSlug}``.
* Tautulli's item page is ``/info?rating_key={rating_key}`` against the same Plex
  server Tautulli mirrors.
* Plex Web addresses a server by ``machineIdentifier`` and an item by its metadata
  key. The same single-page client is served at two different paths, so the path is
  chosen from the host (see ``_plex_web_link``): the plex.tv-hosted app answers under
  ``/desktop`` (``{web}/desktop/#!/server/{id}/details?key=%2Flibrary%2Fmetadata%2F{rk}``),
  and a Plex Media Server serves its own copy under ``/web`` (``{web}/web#!/server/...``).
* Seerr (Overseerr/Jellyseerr) item pages live at ``/movie/{tmdbId}`` / ``/tv/{tmdbId}``.
* IMDb: ``/title/{imdb_id}/``; TMDb: ``/movie/{id}`` or ``/tv/{id}``. Rotten Tomatoes
  URLs are hand-curated slugs no integration provides, so the RT link is an honest
  title search rather than a guessed page.
"""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import quote, urlsplit

from reaper.services.planner import MediaRef, PlanError


@dataclass(frozen=True, slots=True)
class DeepLinks:
    """Every jump the panel offers, each ``None`` when it cannot be built. At most one
    of ``radarr``/``sonarr`` is set -- whichever app the media_key routes to."""

    plex: str | None = None
    tautulli: str | None = None
    seerr: str | None = None
    radarr: str | None = None
    sonarr: str | None = None
    # The rating sites, for the chips in the ratings row.
    imdb: str | None = None
    tmdb: str | None = None
    rotten_tomatoes: str | None = None


def _base(url: str | None) -> str | None:
    """A usable base URL, trailing slashes stripped, or ``None``."""
    cleaned = (url or "").strip().rstrip("/")
    return cleaned or None


def _plex_web_link(web: str, machine_identifier: str, plex_rating_key: int) -> str:
    """The "open in Plex" URL for one metadata item.

    Plex ships the same single-page client at two different paths depending on who
    serves it, so the path is chosen from the host rather than hardcoded. The
    plex.tv-hosted app (the ``app.plex.tv`` default) answers under ``/desktop``. A
    Plex Media Server serves its own copy under ``/web`` and returns 403 for
    ``/desktop`` -- so an operator who points this at their own address gets ``/web``.
    (The hosted app also accepts ``/web`` by redirecting it to ``/desktop``, but the
    canonical path for each host is sent directly rather than leaning on that.)
    """
    metadata_key = quote(f"/library/metadata/{plex_rating_key}", safe="")
    host = (urlsplit(web).hostname or "").lower()
    hosted = host == "plex.tv" or host.endswith(".plex.tv")
    prefix = "desktop/#!" if hosted else "web#!"
    return f"{web}/{prefix}/server/{machine_identifier}/details?key={metadata_key}"


def build_links(
    media_key: str,
    *,
    plex_rating_key: int | None,
    tmdb_id: int | None,
    title_slug: str | None,
    arr_base_url: str | None,
    tautulli_base_url: str | None,
    machine_identifier: str | None,
    plex_web_url: str | None,
    seerr_base_url: str | None = None,
    imdb_id: str | None = None,
    media_type: str = "movie",
    title: str = "",
) -> DeepLinks:
    """Assemble every link the coordinates allow. Pure; no I/O.

    ``arr_base_url`` must belong to the instance ``media_key`` routes to (the caller
    resolves it by ``MediaRef.instance_id``); passing "the first Radarr" would send a
    multi-instance operator to the wrong app. ``media_type`` picks the movie-vs-tv
    route on Seerr and TMDb (a season row carries its show's ids, so both route to
    the show's page).
    """
    try:
        ref = MediaRef.parse(media_key)
    except PlanError:
        # A key we cannot route gets no arr link; the Plex/Tautulli links do not
        # depend on it and are still offered.
        ref = None

    plex = None
    web = _base(plex_web_url)
    if web and machine_identifier and plex_rating_key is not None:
        plex = _plex_web_link(web, machine_identifier, plex_rating_key)

    tautulli = None
    tautulli_base = _base(tautulli_base_url)
    if tautulli_base and plex_rating_key is not None:
        tautulli = f"{tautulli_base}/info?rating_key={plex_rating_key}"

    tv = media_type != "movie"

    seerr = None
    seerr_base = _base(seerr_base_url)
    if seerr_base and tmdb_id is not None:
        seerr = f"{seerr_base}/{'tv' if tv else 'movie'}/{tmdb_id}"

    radarr = None
    sonarr = None
    arr_base = _base(arr_base_url)
    if ref is not None and arr_base:
        if ref.kind == "radarr" and tmdb_id is not None:
            radarr = f"{arr_base}/movie/{tmdb_id}"
        elif ref.kind == "sonarr" and title_slug:
            sonarr = f"{arr_base}/series/{quote(title_slug, safe='')}"

    imdb = f"https://www.imdb.com/title/{quote(imdb_id, safe='')}/" if imdb_id else None
    tmdb = (
        f"https://www.themoviedb.org/{'tv' if tv else 'movie'}/{tmdb_id}"
        if tmdb_id is not None
        else None
    )
    rotten_tomatoes = (
        f"https://www.rottentomatoes.com/search?search={quote(title, safe='')}"
        if title.strip()
        else None
    )

    return DeepLinks(
        plex=plex,
        tautulli=tautulli,
        seerr=seerr,
        radarr=radarr,
        sonarr=sonarr,
        imdb=imdb,
        tmdb=tmdb,
        rotten_tomatoes=rotten_tomatoes,
    )
