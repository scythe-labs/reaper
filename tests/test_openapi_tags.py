# SPDX-License-Identifier: AGPL-3.0-or-later
"""The API reference's sections, the guard that keeps a new route out of the flat list, and
what the published schemas say for themselves.

Every operation carries exactly one section. Untagged, it renders at the bottom of
``/api/docs`` under "default", below every section an operator would think to look in,
which is the state this change exists to end -- so an untagged route has to fail here
rather than quietly rejoin the pile.

Everything here reads the schema the app actually serves at ``/api/openapi.json``, which
is the only copy Scalar ever sees. Rebuilding it with ``get_openapi`` in the test instead
would be a transcription of ``main.openapi_with_api_key`` (rule 119), and it misses
exactly what a transcription misses: ``x-tagGroups`` is attached after that call, so the
rebuilt copy has no headings at all and every assertion about them would be vacuous.

Rule 103: ``api/tags.GROUPS`` is a hand-written list mirroring what the routers declare,
so it carries a drift guard in both directions.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine as sa_create_engine

from reaper.api import tags as api_tags
from reaper.config import Settings
from reaper.db.models import Base
from reaper.main import create_app
from tests._auth import login


async def _no_catch_up(*_args: object, **_kwargs: object) -> None:
    return None


@pytest.fixture(scope="session")
def schema(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Any]:
    """The served schema, read the way the reference page reads it: signed in, over HTTP.

    Session-scoped, because the document is a pure function of the routers and all eleven
    consumers only read it. Per test it was eleven app boots, eleven logins and eleven fetches
    for one identical answer -- 3.80s of setup against 0.37s of assertions.

    **It boots hermetically on its own rather than under ``_hermetic``**, which is the part the
    scope change makes load-bearing. That fixture is function-scoped, and pytest sets a
    session-scoped fixture up *first*: measured here, this body would see the real
    ``catch_up_on_startup``, the real ``load_raw_env`` and ``env_file`` still pointing at
    ``.env``/``.env.local``, so the app would read the developer's own credentials and start the
    ~280 MB IMDb download (rule 37). On CI, where neither dotenv exists, it would do none of
    that and read green. The patches are undone before the yield, so nothing here outlives the
    boot -- what the tests get is a plain dict and no live app.
    """
    tmp_path = tmp_path_factory.mktemp("openapi")
    with pytest.MonkeyPatch.context() as patched:
        patched.setitem(Settings.model_config, "env_file", None)
        patched.setattr("reaper.main.load_raw_env", lambda _s: {})
        patched.setattr("reaper.main.catch_up_on_startup", _no_catch_up)
        settings = Settings(data_dir=tmp_path, secret_key="k")  # type: ignore[call-arg]
        engine = sa_create_engine(settings.sync_database_url)
        Base.metadata.create_all(engine)
        engine.dispose()
        with TestClient(create_app(settings)) as client:
            login(client, settings)
            response = client.get("/api/openapi.json")
            assert response.status_code == 200
            return response.json()


def _operations(schema: dict[str, Any]) -> list[tuple[str, str, dict[str, Any]]]:
    return [
        (method.upper(), path, operation)
        for path, methods in sorted(schema["paths"].items())
        for method, operation in sorted(methods.items())
    ]


def _tags_used(schema: dict[str, Any]) -> set[str]:
    return {tag for _, _, op in _operations(schema) for tag in op.get("tags", ())}


class TestEveryOperationIsFiled:
    def test_no_operation_is_untagged(self, schema: dict[str, Any]) -> None:
        loose = [f"{m} {p}" for m, p, op in _operations(schema) if not op.get("tags")]
        assert loose == [], f"untagged, so they land under 'default' in the reference: {loose}"

    def test_no_operation_carries_more_than_one_section(self, schema: dict[str, Any]) -> None:
        """Two tags puts one operation in two sidebar sections, so the same route reads as
        two. A router-level tag plus a route-level one is how that happens: FastAPI
        concatenates them rather than letting the route override its router."""
        doubled = {
            f"{m} {p}": op.get("tags", ())
            for m, p, op in _operations(schema)
            if len(op.get("tags", ())) > 1
        }
        assert doubled == {}, f"filed in two sidebar sections at once: {doubled}"

    def test_every_tag_used_is_declared(self, schema: dict[str, Any]) -> None:
        undeclared = _tags_used(schema) - set(api_tags.ALL)
        assert undeclared == set(), f"not declared in api/tags.GROUPS: {undeclared}"

    def test_every_declared_tag_has_an_operation(self, schema: dict[str, Any]) -> None:
        """A section with nothing in it renders as a heading over nothing. When the last
        route in a section moves or retires, the section goes with it."""
        empty = set(api_tags.ALL) - _tags_used(schema)
        assert empty == set(), f"declared but nothing is filed under it: {empty}"

    def test_the_policy_pages_routes_file_under_policy(self, schema: dict[str, Any]) -> None:
        """Three operations sit in files whose other routes belong elsewhere, so nothing
        about where they are declared suggests the right section. The operator edits all
        three on the Policy page, and that is what decides it: the two pace routes are
        Policy, "Pace and limits", and the season-shape advisory is the "from your last
        scan" line on the same page. ``runs.py``'s router files everything else it holds
        under Reap, so the pace routes ride a second router.

        Rule 119: the expectation is written from the UI, not read back off the
        decorators, so re-tagging them has to argue with this list."""
        filed = {(m, p): op.get("tags", ()) for m, p, op in _operations(schema)}
        assert filed[("GET", "/api/profile")] == [api_tags.POLICY]
        assert filed[("PUT", "/api/profile")] == [api_tags.POLICY]
        assert filed[("GET", "/api/snapshot/season-shape")] == [api_tags.POLICY]


class TestTheSectionListItself:
    def test_a_section_is_declared_once(self) -> None:
        """Also the only thing standing between a section and two headings. Naming it
        twice in ``GROUPS`` is how that happens, and every other guard here is blind to
        it: ``ALL`` is derived from ``GROUPS``, so a doubled entry doubles on both sides
        of any served-versus-declared comparison and they still match (rule 118 -- the
        test this replaced asserted exactly that, and could not fail)."""
        doubled = sorted({name for name in api_tags.ALL if api_tags.ALL.count(name) > 1})
        assert doubled == [], f"declared more than once, so it gets two headings: {doubled}"

    def test_the_served_schema_carries_the_sections_in_order(self, schema: dict[str, Any]) -> None:
        assert [t["name"] for t in schema["tags"]] == list(api_tags.ALL)

    def test_the_served_schema_carries_the_headings(self, schema: dict[str, Any]) -> None:
        """``x-tagGroups`` is what Scalar nests the sidebar by. It is attached outside
        ``get_openapi``, so it is the part most easily lost in a refactor."""
        assert "x-tagGroups" in schema, "the reference lost its sidebar headings"
        groups = schema["x-tagGroups"]
        assert [g["name"] for g in groups] == ["Start here", "Your library", "Settings"]
        assert [name for g in groups for name in g["tags"]] == list(api_tags.ALL)

    def test_every_section_says_what_is_in_it(self, schema: dict[str, Any]) -> None:
        """The description is the line under the section name; a section without one
        reads as a bare word."""
        bare = [t["name"] for t in schema["tags"] if not t.get("description", "").strip()]
        assert bare == [], f"renders as a bare word, with no line under it: {bare}"

    def test_section_copy_carries_no_em_dashes(self, schema: dict[str, Any]) -> None:
        """Rule 21. These strings are operator-facing: they render in the reference."""
        copy = json.dumps(schema["tags"])
        assert "—" not in copy


class TestThePublishedSchemasSayWhatTheyMean:
    def test_the_conflict_flag_names_all_three_of_its_states(self, schema: dict[str, Any]) -> None:
        """``defers_to_owner`` is true / false / null, and the reflex null-to-false coercion
        makes a client assert the comparison Reaper refused to make -- #86 reproduced outside
        the app, against the one contract we publish for it. The document is where a client
        learns that, and it said nothing at all: the three states were written as an attribute
        docstring, which Pydantic harvests only under ``use_attribute_docstrings``, set nowhere
        in ``src/reaper``.

        Read off the SERVED document, never ``GateOutcomeOut.__doc__``: that transcription
        could not have failed (rule 119), since the contract was there in full the whole time
        and the published schema still carried none of it. Either carrier passes -- the class
        description or the property's own -- because what is pinned is that a client can find
        the third state, not which mechanism puts it there."""
        gate_outcome = schema["components"]["schemas"]["GateOutcomeOut"]
        flag = gate_outcome.get("properties", {}).get("defers_to_owner", {})
        published = f"{gate_outcome.get('description', '')} {flag.get('description', '')}".lower()
        assert "defers_to_owner" in published, "the flag is unnamed in the published reference"
        for state in ("true", "false", "null"):
            assert state in published, f"the reference never says what {state} means"


class TestOneProducerOfTheSchema:
    def test_a_stock_openapi_call_cannot_poison_the_served_document(self, tmp_path: Path) -> None:
        """``FastAPI.openapi`` and ``main.openapi_with_api_key`` both cache into
        ``app.openapi_schema`` and both short-circuit on it, so whichever runs first wins
        for the life of the process -- and the stock one builds no ``tags``, no
        ``x-tagGroups`` and no ``ApiKey`` scheme. ``main`` assigns ``app.openapi`` to the
        enriched function so there is no second producer to lose the race to.

        No caller of the stock entry point exists today, which is what makes this a pin
        rather than a regression test: it fails the moment someone drops the assignment,
        instead of the reference silently degrading to a flat scroll under an auth box
        that does not authenticate. Driven the way the trap would spring -- build the
        schema before the first request, then read what the reference page is served."""
        settings = Settings(data_dir=tmp_path, secret_key="k")  # type: ignore[call-arg]
        engine = sa_create_engine(settings.sync_database_url)
        Base.metadata.create_all(engine)
        engine.dispose()
        app = create_app(settings)
        app.openapi()
        with TestClient(app) as client:
            login(client, settings)
            response = client.get("/api/openapi.json")
            assert response.status_code == 200
            served = response.json()
        assert [t["name"] for t in served.get("tags", ())] == list(api_tags.ALL)
        assert "x-tagGroups" in served
        assert "ApiKey" in served.get("components", {}).get("securitySchemes", {})
