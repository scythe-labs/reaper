# SPDX-License-Identifier: AGPL-3.0-or-later
"""The Scales endpoints.

Read-only. Join the available Seerr requests to the latest scan's candidates and return
the per-person roll-up (``/fairness``) or one person's full breakdown (``/fairness/people``).
Delete nothing, plan nothing -- they are reports.

Scales sits on the last scan (see ``services.fairness``), so it needs no live Radarr,
Sonarr or Tautulli read: sizes, titles and verdicts come from the stored candidates, and
watches from the mirror. It still requires **Seerr** (who requested what) and a configured
**Tautulli** (without a watch mirror every title would read as never-played) -- if either is
missing it says so with a 400 rather than returning a report that looks complete.

Seerr's own request counts and per-type limits are folded in best-effort (``services.
fairness._enrich_accounts``): a portal whose user list is unreadable drops those extras,
never the whole report.
"""

from __future__ import annotations

from contextlib import AsyncExitStack

from fastapi import APIRouter, HTTPException, Request
from sqlalchemy import select

from reaper.api.schemas import (
    FairnessReportOut,
    PersonDetailOut,
    PersonQuotaOut,
    PersonTitleOut,
    QuotaLineOut,
    ReclaimableTitleOut,
    RequesterRowOut,
    UnmatchedRequestOut,
)
from reaper.clients.base import IntegrationError
from reaper.clients.seerr import SeerrClient
from reaper.config import RuntimeSafety
from reaper.crypto import SecretBox
from reaper.db.models import Instance, InstanceKind
from reaper.services import fairness

router = APIRouter(prefix="/api")


def _request_cache(request: Request) -> fairness.RequestCache:
    """The one short-TTL request cache the board and the drawer share, lazily created on
    ``app.state`` (P-1). One per app, so a fresh test app starts with an empty cache and the
    board's read is reused by a drawer opened seconds later, never re-paging every portal."""
    cache = getattr(request.app.state, "fairness_request_cache", None)
    if cache is None:
        cache = fairness.RequestCache()
        request.app.state.fairness_request_cache = cache
    return cache


def _unmatched_out(u: fairness.UnmatchedTitle) -> UnmatchedRequestOut:
    """Map one not-in-scan title to its wire shape, dropping the internal join keys (tmdb id,
    portal) so no id ever reaches the UI."""
    return UnmatchedRequestOut(
        title=u.title,
        year=u.year,
        media_type=u.media_type,
        is_4k=u.is_4k,
        requested_at=u.requested_at.isoformat() if u.requested_at else None,
        available_at=u.available_at.isoformat() if u.available_at else None,
        reason=u.reason,
        requested_by=u.requested_by,
        request_count=u.request_count,
    )


_NEEDS_BOTH = (
    "Scales needs a Seerr and a Tautulli instance: Seerr for who requested what, Tautulli "
    "for who watched it. Configure them in Settings."
)


async def _seerr_rows(request: Request) -> list[Instance]:
    """The enabled Seerr instances, or a 400 when Scales cannot run (no Seerr, or no
    Tautulli to read watches from). Seerr is multi-instance, so every portal is returned."""
    async with request.app.state.session_factory() as session:
        rows = (
            (await session.execute(select(Instance).where(Instance.enabled.is_(True))))
            .scalars()
            .all()
        )
    seerr_rows = [r for r in rows if r.kind is InstanceKind.SEERR]
    tautulli_row = next((r for r in rows if r.kind is InstanceKind.TAUTULLI), None)
    if not seerr_rows or tautulli_row is None:
        raise HTTPException(400, _NEEDS_BOTH)
    return seerr_rows


async def _open_seerrs(
    stack: AsyncExitStack, rows: list[Instance], box: SecretBox, safety: RuntimeSafety
) -> list[SeerrClient]:
    """One client per portal, each owned by the stack (rule 34). The decrypted key travels
    on the connection; TLS verification is relaxed only where the operator turned it off for
    that instance."""
    seerrs: list[SeerrClient] = []
    for row in rows:
        seerr = SeerrClient(
            row.base_url,
            box.decrypt(row.api_key_enc),
            safety=safety,
            verify=row.verify_tls,
            # The stable per-portal id, stamped onto every request this client reads, so a
            # Seerr user id (unique only within a portal) can be told apart across portals.
            instance_key=str(row.id),
            # The operator's external address for the panel's profile link, when set -- the
            # same external_url the why-panel jump links use. Blank leaves the link on the
            # connect address (``base_url``).
            link_base_url=row.external_url,
        )
        await stack.enter_async_context(seerr)
        seerrs.append(seerr)
    return seerrs


@router.get("/fairness")
async def get_fairness(request: Request) -> FairnessReportOut:
    box: SecretBox = request.app.state.secret_box
    # Read-only: Scales only ever GETs -- from Seerr, and from the local snapshot and mirror.
    safety = RuntimeSafety(destructive_enabled=False)
    seerr_rows = await _seerr_rows(request)

    try:
        async with AsyncExitStack() as stack:
            seerrs = await _open_seerrs(stack, seerr_rows, box, safety)
            report = await fairness.build_report(
                session_factory=request.app.state.session_factory,
                seerrs=seerrs,
                cache_engine=request.app.state.cache_engine,
                cache=_request_cache(request),
            )
    except IntegrationError as exc:
        # Any unreachable Seerr is a 502 with the reason -- never a partial leaderboard that
        # looks complete. (Seerr's user list / quota are best-effort inside build_report and
        # do not reach here.)
        raise HTTPException(502, f"Could not build Scales: {exc}") from exc

    return FairnessReportOut(
        total_requests=report.total_requests,
        total_reclaimable_bytes=report.total_reclaimable_bytes,
        total_reclaimable_items=report.total_reclaimable_items,
        not_in_scan=report.not_in_scan,
        unmatched=[_unmatched_out(u) for u in report.unmatched],
        no_snapshot=report.no_snapshot,
        horizon_at=report.horizon_at.isoformat() if report.horizon_at else None,
        rows=[_row_out(row) for row in report.rows],
    )


def _row_out(row: fairness.RequesterRow) -> RequesterRowOut:
    """One board row on the wire. Its own function so a test can assert what the board is
    actually sent without transcribing this mapping (rule 119)."""
    return RequesterRowOut(
        identity=row.identity,
        # Carried so the card can guard it. `played_by_them` below is only ever counted for
        # a linked account (`fairness._roll_up`), so without this the board cannot tell a
        # measured zero from a person whose history Reaper cannot see, and it drew both the
        # same (rule 93).
        plex_id=row.plex_id,
        name=row.name,
        requests_made=row.requests_made,
        gb_granted_bytes=row.gb_granted_bytes,
        played_by_them=row.played_by_them,
        reclaimable_items=row.reclaimable_items,
        reclaimable_bytes=row.reclaimable_bytes,
        # Capped for transport: the count above stays exact, the list is the heaviest
        # 25 (sorted in the service) so the view can name a few and say "+N more".
        reclaimable=[
            ReclaimableTitleOut(
                title=t.title,
                size_bytes=t.size_bytes,
                item_id=t.item_id,
                group_key=t.group_key,
            )
            for t in row.reclaimable[:25]
        ],
        seerr_total=row.seerr_total,
        movie_at_limit=row.movie_at_limit,
        tv_at_limit=row.tv_at_limit,
    )


@router.get("/fairness/people/{identity}")
async def get_person(request: Request, identity: str) -> PersonDetailOut:
    """One person's full request story: every title they asked for that the scan still has,
    when it arrived, whether they watched it, its fate, who else asked, and their Seerr
    account totals and caps. ``identity`` is a Scales row's ``identity`` -- the cross-portal
    person key, not a bare Seerr user id (which collides across portals)."""
    box: SecretBox = request.app.state.secret_box
    safety = RuntimeSafety(destructive_enabled=False)
    seerr_rows = await _seerr_rows(request)

    try:
        async with AsyncExitStack() as stack:
            seerrs = await _open_seerrs(stack, seerr_rows, box, safety)
            detail = await fairness.build_person_detail(
                session_factory=request.app.state.session_factory,
                seerrs=seerrs,
                cache_engine=request.app.state.cache_engine,
                identity=identity,
                cache=_request_cache(request),
            )
    except IntegrationError as exc:
        raise HTTPException(502, f"Could not build Scales: {exc}") from exc

    if detail is None:
        raise HTTPException(404, "No one by that id is in the last scan.")

    quota = detail.quota
    return PersonDetailOut(
        plex_id=detail.plex_id,
        name=detail.name,
        seerr_total=detail.seerr_total,
        requests_in_scan=detail.requests_in_scan,
        gb_granted_bytes=detail.gb_granted_bytes,
        played_by_them=detail.played_by_them,
        reclaimable_items=detail.reclaimable_items,
        reclaimable_bytes=detail.reclaimable_bytes,
        not_in_scan=detail.not_in_scan,
        quota=(
            PersonQuotaOut(
                seerr_total=quota.seerr_total,
                movie=QuotaLineOut(
                    limit=quota.movie.limit, days=quota.movie.days, at_limit=quota.movie.at_limit
                ),
                tv=QuotaLineOut(
                    limit=quota.tv.limit, days=quota.tv.days, at_limit=quota.tv.at_limit
                ),
            )
            if quota is not None
            else None
        ),
        titles=[
            PersonTitleOut(
                title=t.title,
                year=t.year,
                media_type=t.media_type,
                is_4k=t.is_4k,
                size_bytes=t.size_bytes,
                requested_at=t.requested_at.isoformat() if t.requested_at else None,
                available_at=t.available_at.isoformat() if t.available_at else None,
                watched_by_them=t.watched_by_them,
                verdict=t.verdict,
                item_id=t.item_id,
                group_key=t.group_key,
                co_requesters=list(t.co_requesters),
                poster_url=t.poster_url,
            )
            for t in detail.titles
        ],
        unmatched=[_unmatched_out(u) for u in detail.unmatched],
        profile_url=detail.profile_url,
    )
