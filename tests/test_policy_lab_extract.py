# SPDX-License-Identifier: AGPL-3.0-or-later
"""The baseline fixture's regeneration step, and the refusal it carries.

``TestPinnedBaseline`` goes red when the engine's output moves for real library shapes, and
its failure text asks for a ``SCORER_VERSION`` bump. Asking was the whole safeguard, and
asking is not a gate: regenerate the fixture, the suite goes green, the constant stays put,
and every pending approval stays bound to a ``policy_hash`` this build still computes -- so
the plan on the Reap page executes on scores this build would not produce (rule 113).

``policy_lab_extract.rebaseline`` is where that can be enforced and nowhere else, because it
is the only step holding the old baseline and the new one at the same time. A test comparing
them only knows they disagree; the regeneration step knows one is replacing the other.

The escape hatch is load-bearing rather than decorative, so it is tested as carefully as the
refusal. A change to a shipped DEFAULT policy moves every baseline here and voids nothing,
because no operator's stored body changed. Refusing that unconditionally would teach people
to route around the refusal, which costs more than it buys.

Companion to ``test_scorer_surface``, which pins the other half: declarations that move
without the scorer moving. Between them, a declaration change is enforced and an arithmetic
change is refused at the point of regeneration.
"""

from __future__ import annotations

import ast
import copy
import importlib.util
import json
from pathlib import Path
from typing import Any

import pytest

from reaper.engine.policy import SCORER_VERSION

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "policy_lab_extract.py"


def _load_script() -> Any:
    """Import the generator by path rather than by name.

    ``scripts/`` is not a package and is not on the path, and putting it there at import time
    is process-global state the next xdist worker inherits (rule 133). A spec loaded from the
    file leaves nothing behind.
    """
    spec = importlib.util.spec_from_file_location("policy_lab_extract", _SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


extract = _load_script()

#: A baseline no engine can produce, so "this vector moved" is guaranteed rather than
#: probable. A plausible-looking wrong baseline could coincide with the real answer for some
#: vector and quietly turn a refusal test into a no-op.
_IMPOSSIBLE = {"verdict": "protect", "score": -1, "coverage_bp": -1}


@pytest.fixture
def fixture_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A short copy of the real fixture, with ``OUT`` pointed at it.

    Real vectors rather than hand-built ones: ``judge`` reads the whole fact block, and a
    stand-in that scores differently would exercise a shape the harness never sees.
    """
    real = json.loads(extract.OUT.read_text())
    small = {**real, "vectors": copy.deepcopy(real["vectors"][:3])}
    path = tmp_path / "policy_lab_vectors.json"
    path.write_text(json.dumps(small, indent=1, sort_keys=True) + "\n")
    monkeypatch.setattr(extract, "OUT", path)
    return path


def _read(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text())


def _move_one_baseline(path: Path) -> None:
    data = _read(path)
    data["vectors"][0]["baseline"] = dict(_IMPOSSIBLE)
    path.write_text(json.dumps(data, indent=1, sort_keys=True) + "\n")


class TestARebaselineThatMovesNumbersNeedsTheScorerToMove:
    def test_a_moved_baseline_under_the_same_scorer_is_refused(self, fixture_path: Path) -> None:
        """Rule 118: the interlock's own test. Delete the refusal and this goes green while
        the fixture quietly re-pins under a scorer that never moved."""
        _move_one_baseline(fixture_path)

        with pytest.raises(SystemExit) as caught:
            extract.rebaseline()

        assert "refusing to re-pin" in str(caught.value)
        assert "SCORER_VERSION" in str(caught.value)

    def test_the_refusal_leaves_the_fixture_exactly_as_it_found_it(
        self, fixture_path: Path
    ) -> None:
        """A refusal that exits after writing is not a refusal. ``rebaseline`` mutates the
        loaded dict as it walks the vectors, so the thing that keeps the file honest is the
        order of the exit and the write, which nothing else here would catch."""
        _move_one_baseline(fixture_path)
        before = fixture_path.read_text()

        with pytest.raises(SystemExit):
            extract.rebaseline()

        assert fixture_path.read_text() == before

    def test_an_unstamped_fixture_is_refused_rather_than_waved_through(
        self, fixture_path: Path
    ) -> None:
        """Fail closed on "cannot tell". A fixture predating the stamp has no version to
        compare, and reading that as "the scorer must have moved" would let exactly the
        commit this refusal exists for through on its first run."""
        data = _read(fixture_path)
        del data["scorer_version"]
        data["vectors"][0]["baseline"] = dict(_IMPOSSIBLE)
        fixture_path.write_text(json.dumps(data, indent=1, sort_keys=True) + "\n")

        with pytest.raises(SystemExit) as caught:
            extract.rebaseline()

        assert "refusing to re-pin" in str(caught.value)

    def test_a_bumped_scorer_lets_the_moved_baseline_through(self, fixture_path: Path) -> None:
        """The honest path. The author bumps first, so the stamp is behind the running
        constant, and the re-pin writes and re-stamps."""
        data = _read(fixture_path)
        data["scorer_version"] = SCORER_VERSION - 1
        data["vectors"][0]["baseline"] = dict(_IMPOSSIBLE)
        fixture_path.write_text(json.dumps(data, indent=1, sort_keys=True) + "\n")

        extract.rebaseline()

        written = _read(fixture_path)
        assert written["scorer_version"] == SCORER_VERSION
        assert written["vectors"][0]["baseline"] != _IMPOSSIBLE

    def test_an_unmoved_baseline_re_stamps_without_a_bump(self, fixture_path: Path) -> None:
        """Re-stamping after a declaration-only bump has to be free, or the step people are
        asked to run becomes one they cannot run. Nothing moved, so nothing is refused."""
        extract.rebaseline()

        assert _read(fixture_path)["scorer_version"] == SCORER_VERSION


class TestTheEscapeHatchIsStatedRatherThanAssumed:
    def test_a_stated_reason_lets_the_moved_baseline_through_and_is_recorded(
        self, fixture_path: Path
    ) -> None:
        """A shipped default moving is the ordinary case: every baseline here moves and no
        operator's stored body changed, so no approval is owed a void. The reason lands in
        the fixture so the diff carries it to the reviewer."""
        _move_one_baseline(fixture_path)

        extract.rebaseline(unbumped="shipped default moved; no stored body changed")

        written = _read(fixture_path)
        assert written["scorer_note"] == "shipped default moved; no stored body changed"
        assert written["scorer_version"] == SCORER_VERSION
        assert written["vectors"][0]["baseline"] != _IMPOSSIBLE

    def test_a_bump_clears_the_reason_the_last_cut_went_unbumped_on(
        self, fixture_path: Path
    ) -> None:
        """The note explains one decision. Left behind after a bump it would explain the
        wrong one, and it is the kind of stale sentence a reviewer trusts."""
        data = _read(fixture_path)
        data["scorer_version"] = SCORER_VERSION - 1
        data["scorer_note"] = "why the previous cut needed no bump"
        data["vectors"][0]["baseline"] = dict(_IMPOSSIBLE)
        fixture_path.write_text(json.dumps(data, indent=1, sort_keys=True) + "\n")

        extract.rebaseline()

        assert "scorer_note" not in _read(fixture_path)

    @pytest.mark.parametrize("argv", [["--unbumped="], ["--unbumped=   "]])
    def test_an_empty_reason_is_refused_rather_than_read_as_absent(self, argv: list[str]) -> None:
        """``--unbumped=`` states nothing, and treating it as "no flag passed" would route
        it to the refusal with a confusing message about a flag the author did pass."""
        with pytest.raises(SystemExit) as caught:
            extract.unbumped_reason(argv)

        assert "needs a reason" in str(caught.value)

    def test_a_reason_is_read_off_the_flag_and_absence_reads_as_none(self) -> None:
        assert extract.unbumped_reason(["--rebaseline"]) is None
        assert extract.unbumped_reason(["--rebaseline", "--unbumped=defaults moved"]) == (
            "defaults moved"
        )


class TestOneWriterSetsTheStamp:
    """Rule 72's sibling here cannot be reached by a test: the full extract needs a real
    ``data/reaper.db``. So the stamp is set by the single writer both paths call, and what
    is pinned is that the single writer is still single."""

    def test_the_writer_stamps_whatever_it_is_handed(self, tmp_path: Path) -> None:
        path = tmp_path / "f.json"
        monkey = pytest.MonkeyPatch()
        monkey.setattr(extract, "OUT", path)
        try:
            extract.write_fixture({"vectors": []})
        finally:
            monkey.undo()

        assert json.loads(path.read_text())["scorer_version"] == SCORER_VERSION

    def test_the_writer_overwrites_a_stamp_from_an_older_cut(self, tmp_path: Path) -> None:
        """Handed a fixture still claiming the version it was read at, the writer must
        re-stamp rather than preserve -- otherwise the bump path writes new baselines under
        the old version and the next refusal reads the wrong answer."""
        path = tmp_path / "f.json"
        monkey = pytest.MonkeyPatch()
        monkey.setattr(extract, "OUT", path)
        try:
            extract.write_fixture({"vectors": [], "scorer_version": SCORER_VERSION - 1})
        finally:
            monkey.undo()

        assert json.loads(path.read_text())["scorer_version"] == SCORER_VERSION

    def test_the_fixture_has_exactly_one_writer(self) -> None:
        """The design rests on there being one write, and a second one added later would
        take the stamp with it on that path alone -- the very split that made the sibling
        untestable.

        Parsed rather than grepped, rule 147. The first draft counted the string
        ``OUT.write_text`` and read two, because this function's own docstring says it: a
        matcher anchored on a spelling cannot tell a call from a sentence about a call. An
        AST walk sees calls only, and it catches a second writer reaching the file under any
        other name rather than only under ``OUT``.
        """
        tree = ast.parse(_SCRIPT.read_text())
        writes = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "write_text"
        ]
        owners = {
            fn.name
            for fn in ast.walk(tree)
            if isinstance(fn, ast.FunctionDef)
            for node in ast.walk(fn)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "write_text"
        }

        assert len(writes) == 1, (
            f"{len(writes)} writes reach the fixture; route them through write_fixture, or "
            "the scorer stamp is set on one path and forgotten on the other"
        )
        assert owners == {"write_fixture"}, f"the fixture is also written by {owners}"


class TestTheCommittedFixtureAgreesWithTheGenerator:
    def test_the_stamp_helper_reads_what_the_writer_wrote(self) -> None:
        """Rule 131: the producer and the consumer of this stamp read one declaration. The
        writers set ``scorer_version``; ``stamped_scorer`` is what the refusal reads it back
        with, and a rename on one side alone would make every refusal see an unstamped
        fixture -- which fails closed, and so would never announce itself."""
        assert extract.stamped_scorer(json.loads(extract.OUT.read_text())) == SCORER_VERSION

    @pytest.mark.parametrize("value", [None, "2", 2.0, {}])
    def test_a_stamp_that_is_not_an_integer_reads_as_no_stamp(self, value: object) -> None:
        """Anything but an int is "cannot tell", which routes to the refusal. A string "2"
        comparing unequal to the int would have done the same thing by accident; this pins
        that it is the intent."""
        assert extract.stamped_scorer({"scorer_version": value}) is None
