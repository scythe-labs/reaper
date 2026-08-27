# SPDX-License-Identifier: AGPL-3.0-or-later
"""Seerr's user list and per-type request quota.

These power the Scales details drawer: ``requestCount`` for the "total in Seerr" figure,
and the quota's per-type ``restricted`` flag for "which limit are they at". Movies and
series are separate limits with their own window and unit, so nothing here assumes one
window or collapses the two.
"""

from __future__ import annotations

from typing import Any

import pytest

from reaper.clients.base import IntegrationError
from reaper.clients.seerr import SeerrClient
from reaper.config import RuntimeSafety


def _client() -> SeerrClient:
    return SeerrClient("http://seerr.local", "key", safety=RuntimeSafety())


class TestQuota:
    async def test_a_missing_limit_is_unlimited_and_never_restricted(self) -> None:
        client = _client()

        async def fake(path: str, **kwargs: Any) -> object:
            # Overseerr omits limit/days/remaining entirely when there is no cap.
            return {"movie": {"used": 4}, "tv": {"used": 1}}

        client.get_json = fake  # type: ignore[method-assign]
        try:
            q = await client.quota(1)
        finally:
            await client.aclose()

        assert q.movie.unlimited and q.movie.limit is None and q.movie.days is None
        assert q.movie.restricted is False
        assert q.tv.unlimited and q.tv.restricted is False

    async def test_a_zero_limit_is_treated_as_unlimited(self) -> None:
        """Overseerr uses 0 and absent interchangeably for 'no limit'. A literal zero
        must not read as 'zero allowed', which would flag an at-cap block that isn't real."""
        client = _client()

        async def fake(path: str, **kwargs: Any) -> object:
            return {"movie": {"limit": 0, "used": 9, "restricted": True}, "tv": {"used": 0}}

        client.get_json = fake  # type: ignore[method-assign]
        try:
            q = await client.quota(1)
        finally:
            await client.aclose()

        assert q.movie.unlimited is True
        # restricted is forced false without a real limit, whatever the payload claimed.
        assert q.movie.restricted is False

    async def test_per_type_windows_and_units_are_kept_apart(self) -> None:
        """Movies "1 per 14 days" and series "1 per 60 days" are separate limits with
        separate windows. Both values are carried through, and neither is assumed from
        the other."""
        client = _client()

        async def fake(path: str, **kwargs: Any) -> object:
            return {
                "movie": {"limit": 1, "days": 14, "used": 1, "remaining": 0, "restricted": True},
                "tv": {"limit": 1, "days": 60, "used": 0, "remaining": 1, "restricted": False},
            }

        client.get_json = fake  # type: ignore[method-assign]
        try:
            q = await client.quota(7)
        finally:
            await client.aclose()

        assert (q.movie.limit, q.movie.days, q.movie.restricted) == (1, 14, True)
        assert (q.tv.limit, q.tv.days, q.tv.restricted) == (1, 60, False)

    async def test_a_non_object_body_is_an_integration_error(self) -> None:
        client = _client()

        async def fake(path: str, **kwargs: Any) -> object:
            return ["nope"]

        client.get_json = fake  # type: ignore[method-assign]
        try:
            with pytest.raises(IntegrationError):
                await client.quota(1)
        finally:
            await client.aclose()


class TestUsers:
    async def test_pages_through_and_carries_the_join_and_count(self) -> None:
        client = _client()
        page_size = 100

        async def fake(path: str, **kwargs: Any) -> object:
            skip = kwargs["params"]["skip"]
            total = 150
            rows = [
                {
                    "id": i,
                    "plexId": 1000 + i,
                    "plexUsername": f"u{i}",
                    "displayName": f"User {i}",
                    "email": f"u{i}@example.net",
                    "requestCount": i,
                }
                for i in range(skip, min(skip + page_size, total))
            ]
            return {"pageInfo": {"results": total}, "results": rows}

        client.get_json = fake  # type: ignore[method-assign]
        try:
            users = await client.users()
        finally:
            await client.aclose()

        assert len(users) == 150
        first = users[0]
        assert first.seerr_user_id == 0 and first.plex_id == 1000
        assert first.username == "u0" and first.request_count == 0
        assert users[149].request_count == 149

    async def test_rows_without_a_total_refuse_rather_than_truncate(self) -> None:
        """Uses the same envelope guard as requests(). Rows present with no pageInfo
        total would stop after one page and undercount every account."""
        client = _client()

        async def fake(path: str, **kwargs: Any) -> object:
            return {"pageInfo": {}, "results": [{"id": 1, "plexId": 5}]}

        client.get_json = fake  # type: ignore[method-assign]
        try:
            with pytest.raises(IntegrationError) as exc:
                await client.users()
            assert exc.value.code == "error.integration.unexpected_shape"
        finally:
            await client.aclose()

    async def test_username_falls_back_from_plex_to_local(self) -> None:
        client = _client()

        async def fake(path: str, **kwargs: Any) -> object:
            return {
                "pageInfo": {"results": 1},
                "results": [{"id": 3, "username": "local-only", "requestCount": 2}],
            }

        client.get_json = fake  # type: ignore[method-assign]
        try:
            users = await client.users()
        finally:
            await client.aclose()

        assert users[0].username == "local-only" and users[0].plex_id is None
