# SPDX-License-Identifier: AGPL-3.0-or-later
"""The requester rule.

Cases drawn from a live Seerr instance and its Tautulli history. The per-media
grouping in particular exists because real data has the same title requested by
several different people -- something a spec-derived fixture would never have
shown, and which changes the answer.
"""

from __future__ import annotations

from datetime import timedelta

from reaper.clients.seerr import MediaRequest, Requester
from reaper.clock import utcnow
from reaper.engine.requester import (
    ABSTAIN,
    CONDEMN,
    PROTECT,
    RequesterPolicy,
    WatchEvidence,
    evaluate,
    group_by_media,
)

POLICY = RequesterPolicy(unwatched_days=90)
NOW = utcnow()


def _request(
    *,
    request_id: int = 1,
    plex_id: int | None = 100,
    name: str = "Alice",
    rating_key: str | None = "555",
    days_available: float | None = 400,
    media_type: str = "movie",
) -> MediaRequest:
    return MediaRequest(
        request_id=request_id,
        media_type=media_type,
        is_4k=False,
        status=5,
        requested_at=NOW - timedelta(days=(days_available or 0) + 10),
        requester=Requester(
            seerr_user_id=request_id,
            plex_id=plex_id,
            username=name.lower(),
            display_name=name,
            email=None,
        ),
        tmdb_id=1,
        tvdb_id=None,
        imdb_id="tt1",
        plex_rating_key=rating_key,
        arr_id=1,
        arr_instance_id=0,
        available_at=(NOW - timedelta(days=days_available)) if days_available is not None else None,
    )


class TestTheRuleIsPerMediaNotPerRequest:
    """Found by running against real data: the same title is often requested by
    several people. Judged per request, a film Alice requested and watched would
    still be condemned on Bob's row -- and they share one file, so it is deleted
    out from under her."""

    def test_two_requests_for_the_same_item_are_grouped(self) -> None:
        grouped = group_by_media(
            [
                _request(request_id=1, plex_id=100, name="Alice", rating_key="555"),
                _request(request_id=2, plex_id=200, name="Bob", rating_key="555"),
            ]
        )
        assert list(grouped) == ["555"]
        assert len(grouped["555"]) == 2

    def test_one_requester_watching_protects_the_item_for_everyone(self) -> None:
        """Alice asked and watched; Bob asked and did not. The file stays."""
        requests = [
            _request(request_id=1, plex_id=100, name="Alice"),
            _request(request_id=2, plex_id=200, name="Bob"),
        ]
        evidence = WatchEvidence(plays_by_user={100: 3}, distinct_watchers=1)

        finding = evaluate(requests, evidence, POLICY, now=NOW)

        assert finding.verdict is PROTECT
        assert "Alice" in finding.reason
        assert finding.requester_plays == 3

    def test_condemned_only_when_no_requester_watched(self) -> None:
        requests = [
            _request(request_id=1, plex_id=100, name="Alice"),
            _request(request_id=2, plex_id=200, name="Bob"),
        ]
        finding = evaluate(requests, WatchEvidence(), POLICY, now=NOW)

        assert finding.verdict is CONDEMN
        assert "Alice" in finding.reason and "Bob" in finding.reason

    def test_unmatched_requests_are_not_silently_merged(self) -> None:
        """Two items Plex has not matched must stay separate, not collapse into
        one bucket and get a single verdict."""
        grouped = group_by_media(
            [
                _request(request_id=1, rating_key=None),
                _request(request_id=2, rating_key=None),
            ]
        )
        assert len(grouped) == 2


class TestOthersWatchingProtects:
    """Your headline requirement: the requester ignored it, but if other people are
    watching it, it stays."""

    def test_others_watching_protects(self) -> None:
        """Verbatim from the live probe: Jesse Bickel never watched a title he
        requested 477 days ago, but 5 other people had."""
        finding = evaluate(
            [_request(plex_id=100, name="Jesse", days_available=477)],
            WatchEvidence(plays_by_user={200: 4, 300: 2, 400: 1, 500: 9, 600: 3}),
            POLICY,
            now=NOW,
        )

        assert finding.verdict is PROTECT
        assert finding.other_watchers == 5
        assert "punish" in finding.reason

    def test_nobody_watching_condemns(self) -> None:
        """Also verbatim: TJ Norton, 625 days, zero plays by anyone."""
        finding = evaluate(
            [_request(plex_id=100, name="TJ", days_available=625)],
            WatchEvidence(),
            POLICY,
            now=NOW,
        )

        assert finding.verdict is CONDEMN
        assert finding.other_watchers == 0

    def test_the_requester_is_not_counted_among_the_others(self) -> None:
        """Otherwise a requester's own plays would 'protect' the item twice and the
        others-watching count would be inflated in the UI."""
        finding = evaluate(
            [_request(plex_id=100, name="Alice")],
            WatchEvidence(plays_by_user={100: 5}),
            POLICY,
            now=NOW,
        )
        assert finding.other_watchers == 0


class TestAbstentionsProtect:
    """Unknown may only protect, never condemn."""

    def test_an_unmappable_requester_abstains(self) -> None:
        """No plexId means their history is invisible -- which looks exactly like
        never having watched anything. Measured on the live instance, all 337
        requesters were mappable, but a single Plex Home managed user would not be."""
        finding = evaluate(
            [_request(plex_id=None, name="Ghost")],
            WatchEvidence(),
            POLICY,
            now=NOW,
        )
        assert finding.verdict is ABSTAIN
        assert "no Plex account" in finding.reason

    def test_no_rating_key_abstains(self) -> None:
        """A real fraction of live requests carry no ratingKey -- Plex never matched
        them. Unmatched is Unknown, and Unknown may only protect."""
        finding = evaluate(
            [_request(rating_key=None)],
            WatchEvidence(),
            POLICY,
            now=NOW,
        )
        assert finding.verdict is ABSTAIN
        assert "not matched" in finding.reason

    def test_no_available_at_abstains(self) -> None:
        """Without mediaAddedAt there is no clock. Falling back to the request date
        would start the countdown before the file existed."""
        finding = evaluate(
            [_request(days_available=None)],
            WatchEvidence(),
            POLICY,
            now=NOW,
        )
        assert finding.verdict is ABSTAIN
        assert "did not exist yet" in finding.reason


class TestTheClock:
    def test_recently_available_is_protected(self) -> None:
        finding = evaluate(
            [_request(days_available=30)],
            WatchEvidence(),
            POLICY,
            now=NOW,
        )
        assert finding.verdict is PROTECT
        assert "just new" in finding.reason

    def test_the_clock_starts_when_it_arrived_not_when_it_was_asked_for(self) -> None:
        """The request was made 410 days ago but the file only landed 30 days ago.
        Using createdAt would condemn a file nobody has had a chance to watch."""
        request = _request(days_available=30)
        assert request.requested_at is not None
        assert (NOW - request.requested_at).days == 40  # asked well before it arrived

        finding = evaluate([request], WatchEvidence(), POLICY, now=NOW)

        assert finding.verdict is PROTECT
        assert finding.days_available is not None
        assert round(finding.days_available) == 30


class TestExplainability:
    def test_checks_that_did_not_fire_are_recorded_with_their_numbers(self) -> None:
        """The block no competitor shows: every protection that was evaluated and
        did not fire, with the actual figures behind it."""
        finding = evaluate(
            [_request(plex_id=100, name="TJ", days_available=625)],
            WatchEvidence(),
            POLICY,
            now=NOW,
        )

        assert finding.verdict is CONDEMN
        joined = " | ".join(finding.checks_performed)
        assert "requester watched it -- 0 plays" in joined
        assert "someone else is watching it -- 0 other watchers" in joined
        assert "available 625 days, floor is 90" in joined

    def test_a_protected_item_still_explains_itself(self) -> None:
        """A tool that only explains deletions cannot be trusted about keeps."""
        finding = evaluate(
            [_request(plex_id=100, name="Alice")],
            WatchEvidence(plays_by_user={100: 2}),
            POLICY,
            now=NOW,
        )
        assert finding.verdict is PROTECT
        assert finding.reason
