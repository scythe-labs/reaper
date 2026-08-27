# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for the foundations that are painful or impossible to fix later."""

from __future__ import annotations

import pytest
from sqlalchemy import Column, Integer, MetaData, Table, UniqueConstraint

from reaper.crypto import SecretBox
from reaper.db.base import NAMING_CONVENTION, Base
from reaper.logging import REDACTED, redact_secrets


class TestNamingConvention:
    """SQLite cannot drop an unnamed constraint. Alembic's batch mode rebuilds the
    table to work around that, but it can only drop a constraint it can name. If
    the convention is missing, a future migration dies with 'Constraint must have
    a name' and the only fix is rewriting every migration."""

    def test_metadata_carries_the_convention(self) -> None:
        assert Base.metadata.naming_convention == NAMING_CONVENTION

    def test_anonymous_constraints_get_deterministic_names(self) -> None:
        md = MetaData(naming_convention=NAMING_CONVENTION)
        table = Table(
            "widget",
            md,
            Column("id", Integer, primary_key=True),
            Column("code", Integer),
            UniqueConstraint("code"),  # deliberately unnamed
        )
        names = {c.name for c in table.constraints}
        assert "pk_widget" in names
        assert "uq_widget_code" in names

    def test_every_model_constraint_is_named(self) -> None:
        for table in Base.metadata.tables.values():
            for constraint in table.constraints:
                assert constraint.name, f"{table.name} has an unnamed constraint"


class TestSecretBox:
    def test_roundtrip(self) -> None:
        box = SecretBox("test-key")
        assert box.decrypt(box.encrypt("hunter2")) == "hunter2"

    def test_ciphertext_is_not_plaintext(self) -> None:
        box = SecretBox("test-key")
        assert "hunter2" not in box.encrypt("hunter2")

    def test_wrong_key_is_a_clear_error_not_garbage(self) -> None:
        token = SecretBox("original").encrypt("hunter2")
        with pytest.raises(ValueError, match="REAPER_SECRET_KEY"):
            SecretBox("different").decrypt(token)

    def test_old_key_still_decrypts_after_rotation(self) -> None:
        old_token = SecretBox("old-key").encrypt("hunter2")
        rotated = SecretBox("new-key", "old-key")
        assert rotated.decrypt(old_token) == "hunter2"

    def test_rotate_reencrypts_under_the_current_key(self) -> None:
        old_token = SecretBox("old-key").encrypt("hunter2")
        rotated = SecretBox("new-key", "old-key")
        new_token = rotated.rotate(old_token)
        # The new token must stand alone under the new key.
        assert SecretBox("new-key").decrypt(new_token) == "hunter2"

    def test_empty_key_is_refused(self) -> None:
        with pytest.raises(ValueError, match="secret key is required"):
            SecretBox("")


class TestSecretRedaction:
    """Every credential Reaper holds is destructive-capable, and Tautulli and MDBList both
    take their key as a *query parameter*. So a logged URL is a logged credential."""

    def test_secret_keys_are_redacted(self) -> None:
        out = redact_secrets(None, "info", {"event": "call", "api_key": "abc123"})
        assert out["api_key"] == REDACTED

    def test_plex_token_header_is_redacted(self) -> None:
        out = redact_secrets(None, "info", {"event": "call", "X-Plex-Token": "xyz"})
        assert out["X-Plex-Token"] == REDACTED

    @pytest.mark.parametrize(
        "url",
        [
            "http://tautulli:8181/api/v2?apikey=SUPERSECRET&cmd=get_history",
            "https://api.mdblist.com/imdb/movie/?apikey=SUPERSECRET",
            "http://plex:32400/library/sections?X-Plex-Token=SUPERSECRET",
        ],
    )
    def test_query_string_credentials_are_redacted(self, url: str) -> None:
        out = redact_secrets(None, "info", {"event": "http", "url": url})
        assert "SUPERSECRET" not in out["url"]
        assert REDACTED in out["url"]

    def test_redaction_preserves_the_rest_of_the_url(self) -> None:
        out = redact_secrets(
            None,
            "info",
            {"url": "http://tautulli:8181/api/v2?apikey=SECRET&cmd=get_history"},
        )
        assert "cmd=get_history" in out["url"]

    def test_the_httpx_logger_is_quieted_so_it_cannot_leak_urls(
        self, _restore_logging: None
    ) -> None:
        """The redaction processor only sees *structlog* events. httpx logs every request
        URL, with the apikey/token in the query string, through the stdlib at INFO, where
        the processor never runs. So configure_logging must lift those loggers above INFO,
        or the credential goes to the log in cleartext.

        ``_restore_logging`` (conftest) is not optional here. ``configure_logging`` is
        entirely process-global: root level, a ring handler on the root logger, every
        noisy library logger, and structlog's own configuration. This test used to call it
        with no cleanup, leaving all of that behind for whatever the xdist worker picked up
        next.
        """
        import logging

        from reaper.logging import configure_logging

        configure_logging(level="INFO")

        for name in ("httpx", "httpcore", "urllib3", "plexapi"):
            assert logging.getLogger(name).getEffectiveLevel() >= logging.WARNING, (
                f"{name} logs at INFO; it will print request URLs with credentials in them"
            )
