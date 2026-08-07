# SPDX-License-Identifier: AGPL-3.0-or-later
"""The baseline fixture's regeneration steps, and the refusal they carry.

``TestPinnedBaseline`` goes red when the engine's output moves for real library shapes, and
its failure text asks for a ``SCORER_VERSION`` bump. Asking was the whole safeguard, and
asking is not a gate: regenerate the fixture, the suite goes green, the constant stays put,
and every pending approval stays bound to a ``policy_hash`` this build still computes -- so
the plan on the Reap page executes on scores this build would not produce (rule 113).

Only a regeneration step can enforce it, because only it holds the old baseline and the new
one at the same time. A test comparing them knows they disagree and nothing more.

**There are two regeneration paths and both are gated.** ``--rebaseline`` re-pins the
committed shapes; the full extract re-samples from a real library. The full extract was
exempt at first, on the reasoning that re-sampling makes "did the baseline move?" unanswerable
-- which is false, and was measured: the old fixture and its baselines are still on disk, so
re-judging *those* vectors answers it exactly, with no database. With a ramp change,
``--rebaseline`` refused 338 moved baselines while the full extract wrote all 338 and
re-stamped the scorer.

The escape hatch is load-bearing rather than decorative, so it is tested as carefully as the
refusal. A change to a shipped DEFAULT policy moves every baseline here and voids nothing,
because no operator's stored body changed; so does an edit to the harness that judges these
vectors, where the engine did not move at all. Refusing those unconditionally would teach
people to route around the refusal, which costs more than it buys.

Companion to ``test_scorer_surface``, which pins the other half: declarations that move
without the scorer moving.
"""

from __future__ import annotations

import copy
import json
import sys
from pathlib import Path
from typing import Any

import pytest

from reaper.engine.policy import SCORER_VERSION
from tests._generators import functions_that_can_write, load_script

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "policy_lab_extract.py"

extract = load_script(_SCRIPT)

#: A baseline no engine can produce, so "this vector moved" is guaranteed rather than
#: probable. A plausible-looking wrong baseline could coincide with the real answer for some
#: vector and quietly turn a refusal test into a no-op.
_IMPOSSIBLE = {"verdict": "protect", "score": -1, "coverage_bp": -1}

_REASON = "shipped default moved, no stored body changed"


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


def _write(path: Path, data: dict[str, Any]) -> None:
    path.write_text(json.dumps(data, indent=1, sort_keys=True) + "\n")


def _move_one_baseline(path: Path, **overrides: Any) -> None:
    data = _read(path)
    data["vectors"][0]["baseline"] = dict(_IMPOSSIBLE)
    data.update(overrides)
    _write(path, data)


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

    def test_a_bumped_scorer_lets_the_moved_baseline_through(self, fixture_path: Path) -> None:
        """The honest path. The author bumps first, so the stamp is behind the running
        constant, and the re-pin writes and re-stamps."""
        _move_one_baseline(fixture_path, scorer_version=SCORER_VERSION - 1)

        extract.rebaseline()

        written = _read(fixture_path)
        assert written["scorer_version"] == SCORER_VERSION
        assert written["vectors"][0]["baseline"] != _IMPOSSIBLE

    def test_an_unmoved_baseline_re_stamps_without_a_bump(self, fixture_path: Path) -> None:
        """Re-stamping after a declaration-only bump has to be free, or the step people are
        asked to run becomes one they cannot run. Nothing moved, so nothing is refused."""
        extract.rebaseline()

        assert _read(fixture_path)["scorer_version"] == SCORER_VERSION

    def test_a_fixture_with_no_vectors_is_refused_rather_than_re_stamped(
        self, fixture_path: Path
    ) -> None:
        """Nothing to compare is not the same as nothing moved. An emptied vector list would
        otherwise re-stamp clean and read as a scorer that was checked."""
        _write(fixture_path, {**_read(fixture_path), "vectors": []})

        with pytest.raises(SystemExit) as caught:
            extract.rebaseline()

        assert "no vectors" in str(caught.value)


class TestTheStampIsAVersionOrItIsNothing:
    """The refusal reads one number to decide whether the scorer moved, so every value that
    number can hold is a branch. ``!=`` accepted all of them as "moved" -- the fail-open
    direction -- and the first version of this class swept four types that already worked."""

    @pytest.mark.parametrize("value", [None, "2", 2.0, {}, [], True, False, 0, -5, float("nan")])
    def test_a_stamp_that_is_not_a_version_number_reads_as_no_stamp(self, value: object) -> None:
        """``True`` is the one that mattered: ``isinstance(True, int)`` is ``True`` in
        Python, so a stamp of ``true`` compared unequal to the running constant and read as
        a bump. ``0`` and negatives fail production's own ``ge=1`` on the same field."""
        assert extract.stamped_scorer({"scorer_version": value}) is None

    @pytest.mark.parametrize("value", [1, SCORER_VERSION, SCORER_VERSION + 7, 10**30])
    def test_a_stamp_that_is_a_version_number_is_returned_as_read(self, value: int) -> None:
        """Including one ahead of the running constant. Reading it is not accepting it --
        the refusal is what rejects that, and it can only do so if this hands it back."""
        assert extract.stamped_scorer({"scorer_version": value}) == value

    @pytest.mark.parametrize("stamp", [True, False, 0, -5, "2", 2.0])
    def test_an_unusable_stamp_refuses_a_moved_baseline(
        self, fixture_path: Path, stamp: object
    ) -> None:
        """Fail closed on "cannot tell". Reading any of these as "the scorer must have
        moved" lets exactly the commit this refusal exists for straight through."""
        _move_one_baseline(fixture_path, scorer_version=stamp)

        with pytest.raises(SystemExit) as caught:
            extract.rebaseline()

        assert "refusing to re-pin" in str(caught.value)

    def test_a_missing_stamp_refuses_a_moved_baseline(self, fixture_path: Path) -> None:
        data = _read(fixture_path)
        del data["scorer_version"]
        data["vectors"][0]["baseline"] = dict(_IMPOSSIBLE)
        _write(fixture_path, data)

        with pytest.raises(SystemExit) as caught:
            extract.rebaseline()

        assert "refusing to re-pin" in str(caught.value)

    @pytest.mark.parametrize("moved", [True, False])
    def test_a_stamp_ahead_of_this_build_is_refused_whether_or_not_anything_moved(
        self, fixture_path: Path, moved: bool
    ) -> None:
        """A fixture from a newer Reaper, which production refuses on the same value
        (``PolicyBody.scorer_version`` is ``le=SCORER_VERSION``). Writing would re-stamp it
        downward and destroy the only record of where it came from -- so the no-move case is
        the one that matters, and it is the one a moved-only guard would miss.
        """
        data = _read(fixture_path)
        data["scorer_version"] = SCORER_VERSION + 1
        if moved:
            data["vectors"][0]["baseline"] = dict(_IMPOSSIBLE)
        _write(fixture_path, data)
        before = fixture_path.read_text()

        with pytest.raises(SystemExit) as caught:
            extract.rebaseline()

        assert "newer Reaper" in str(caught.value)
        assert fixture_path.read_text() == before


class TestTheEscapeHatchIsStatedRatherThanAssumed:
    def test_a_stated_reason_lets_the_moved_baseline_through_and_is_recorded(
        self, fixture_path: Path
    ) -> None:
        """A shipped default moving is the ordinary case: every baseline here moves and no
        operator's stored body changed, so no approval is owed a void. The reason lands in
        the fixture so the diff carries it to the reviewer."""
        _move_one_baseline(fixture_path)

        extract.rebaseline(unbumped=_REASON)

        written = _read(fixture_path)
        assert written["scorer_note"] == _REASON
        assert written["scorer_version"] == SCORER_VERSION
        assert written["vectors"][0]["baseline"] != _IMPOSSIBLE

    def test_a_bump_clears_the_reason_the_last_cut_went_unbumped_on(
        self, fixture_path: Path
    ) -> None:
        """The note explains one decision. Left behind after a bump it would explain the
        wrong one, and it is the kind of stale sentence a reviewer trusts."""
        _move_one_baseline(
            fixture_path,
            scorer_version=SCORER_VERSION - 1,
            scorer_note="why the previous cut needed no bump",
        )

        extract.rebaseline()

        assert "scorer_note" not in _read(fixture_path)

    def test_a_run_that_moves_nothing_records_no_reason(self, fixture_path: Path) -> None:
        """A justification for a change that did not happen. The note is only meaningful
        about baselines that moved, so a clean re-stamp neither writes one nor drops the one
        already explaining the baselines still in the file."""
        _write(fixture_path, {**_read(fixture_path), "scorer_note": "an earlier cut"})

        extract.rebaseline(unbumped=_REASON)

        assert _read(fixture_path)["scorer_note"] == "an earlier cut"

    @pytest.mark.parametrize("raw", ["", "   ", "\t", "\n"])
    def test_an_empty_reason_is_refused_rather_than_read_as_absent(self, raw: str) -> None:
        with pytest.raises(SystemExit) as caught:
            extract.check_reason(raw)

        assert "needs a reason" in str(caught.value)

    @pytest.mark.parametrize("raw", ["x", ".", "-", "short", "no spaces here".replace(" ", "")])
    def test_a_token_is_not_a_reason(self, raw: str) -> None:
        """The flag exists to make someone state the case. One keystroke satisfied it."""
        with pytest.raises(SystemExit) as caught:
            extract.check_reason(raw)

        assert "too short to check" in str(caught.value)

    @pytest.mark.parametrize(
        "raw",
        [
            "moved 338 vectors on the big library",
            "see /mnt/media/movies for why",
            "asked bob@example.com about it",
            "the 4K library changed",
        ],
    )
    def test_a_reason_carrying_anything_identifying_is_refused(self, raw: str) -> None:
        """The fixture is committed, and the golden rule binds it as it binds code: never a
        real title, host, path, username or stat. This is the first free-text field a human
        types straight into that file, and ``TestFixtureStaysDeidentified`` inspects keys
        rather than values, so the charset is caught here at the boundary."""
        with pytest.raises(SystemExit) as caught:
            extract.check_reason(raw)

        assert "may not" in str(caught.value)

    def test_a_phrase_describing_the_kind_of_change_is_accepted(self) -> None:
        assert extract.check_reason(f"  {_REASON}  ") == _REASON
        assert extract.check_reason("keep_tags removed from PolicyBody") == (
            "keep_tags removed from PolicyBody"
        )


class TestTheCommandLineCannotBeMissedByOneCharacter:
    @pytest.mark.parametrize(
        "argv", [["--rebaseline=true"], ["--re-baseline"], ["-rebaseline"], ["--REBASELINE"]]
    )
    def test_a_near_miss_is_refused_rather_than_run_as_the_full_extract(
        self, argv: list[str]
    ) -> None:
        """Dispatch was an exact-string membership test with no unknown-flag rejection, so
        every near miss fell through to the path that re-extracts from a live library. The
        refusal's own message asks the author to retype this command line, which is exactly
        when a typo happens."""
        with pytest.raises(SystemExit) as caught:
            extract.parse_argv(argv)

        assert "unrecognized argument" in str(caught.value)

    def test_both_spellings_of_the_reason_flag_are_read(self) -> None:
        """The space form is how most CLIs work. Dropping it silently left the author
        believing they had recorded a justification when none was written."""
        assert extract.parse_argv(["--rebaseline", f"--unbumped={_REASON}"]) == (True, _REASON)
        assert extract.parse_argv(["--rebaseline", "--unbumped", _REASON]) == (True, _REASON)

    def test_the_space_form_with_nothing_after_it_is_refused(self) -> None:
        with pytest.raises(SystemExit) as caught:
            extract.parse_argv(["--rebaseline", "--unbumped"])

        assert "needs a reason" in str(caught.value)

    def test_no_flags_is_the_full_extract_with_no_reason(self) -> None:
        assert extract.parse_argv([]) == (False, None)


class TestMainWiresTheFlagsToTheInterlock:
    """The seam no test crossed. Three mutations of ``main`` -- dropping the parsed reason,
    replacing the branch with a bare return, and pinning the hatch open with
    ``unbumped_reason(...) or "auto"`` -- all left the file green."""

    def test_the_parsed_reason_reaches_rebaseline(self, monkeypatch: pytest.MonkeyPatch) -> None:
        seen: list[str | None] = []
        monkeypatch.setattr(extract, "rebaseline", lambda unbumped=None: seen.append(unbumped))
        monkeypatch.setattr(sys, "argv", ["prog", "--rebaseline", f"--unbumped={_REASON}"])

        extract.main()

        assert seen == [_REASON]

    def test_a_rebaseline_with_no_flag_passes_no_reason(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The mutation that pinned the hatch permanently open produced a reason here where
        none was given, and nothing noticed."""
        seen: list[str | None] = []
        monkeypatch.setattr(extract, "rebaseline", lambda unbumped=None: seen.append(unbumped))
        monkeypatch.setattr(sys, "argv", ["prog", "--rebaseline"])

        extract.main()

        assert seen == [None]

    def test_the_full_extract_is_guarded_before_it_touches_a_library(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Ordering, not just presence: the guard has to run before the database is opened,
        or a machine without one never reaches it."""
        order: list[str] = []
        monkeypatch.setattr(
            extract, "guard_the_full_extract", lambda unbumped: order.append("guarded")
        )
        monkeypatch.setattr(sys, "argv", ["prog"])

        def _stop(*_a: object, **_k: object) -> None:
            order.append("opened-db")
            raise RuntimeError("stop here")

        monkeypatch.setattr(extract.sqlite3, "connect", _stop)

        with pytest.raises(RuntimeError, match="stop here"):
            extract.main()

        assert order == ["guarded", "opened-db"]


class TestTheFullExtractIsHeldToTheSameBar:
    def test_an_engine_change_is_refused_before_a_re_sample_can_bury_it(
        self, fixture_path: Path
    ) -> None:
        """The path that laundered 338 moved baselines. It re-samples the vectors, so the
        evidence disappears into the new sample -- which is why the check has to happen
        against the OLD fixture, before the write."""
        _move_one_baseline(fixture_path)

        with pytest.raises(SystemExit) as caught:
            extract.guard_the_full_extract(None)

        assert "refusing to re-pin" in str(caught.value)

    def test_a_stated_reason_clears_it_here_too(self, fixture_path: Path) -> None:
        _move_one_baseline(fixture_path)

        extract.guard_the_full_extract(_REASON)

    def test_a_first_extract_with_no_fixture_yet_is_not_blocked(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Nothing to compare against, and refusing would make the fixture unbootstrappable."""
        monkeypatch.setattr(extract, "OUT", tmp_path / "absent.json")

        extract.guard_the_full_extract(None)


class TestOneWriterSetsTheStamp:
    """Rule 72's sibling here cannot be reached by a test: the full extract needs a real
    ``data/reaper.db``. So the stamp is set by the single writer both paths call, and what is
    pinned is that the single writer is still single."""

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
        untestable."""
        assert functions_that_can_write(_SCRIPT.read_text()) == {"write_fixture"}

    @pytest.mark.parametrize(
        "snippet",
        [
            'def sneak(f):\n    OUT.write_text("x")\n',
            'def sneak(f):\n    with open(OUT, "w") as fh:\n        json.dump(f, fh)\n',
            'def sneak(f):\n    writer = OUT.write_text\n    writer("x")\n',
            'def sneak(f):\n    OUT.write_bytes(b"x")\n',
            "def sneak(f):\n    tmp.replace(OUT)\n",
            'def sneak(f):\n    shutil.copyfile("a", OUT)\n',
        ],
    )
    def test_the_matcher_sees_the_forms_a_second_writer_would_use(self, snippet: str) -> None:
        """Rule 147: the guard scans source, so it is proven against every form the tree
        could spell the thing in, not only the one already there. The first version counted
        the string ``OUT.write_text`` and read two, because ``write_fixture``'s own docstring
        says it; the second parsed calls only, and the alias form walked straight past."""
        assert functions_that_can_write(snippet) == {"sneak"}

    def test_the_matcher_does_not_fire_on_serialization_alone(self) -> None:
        """``json.dumps`` produces a string and touches no disk. A matcher that flagged it
        would make the guard useless the moment anyone formatted JSON outside the writer."""
        assert functions_that_can_write("def calm(f):\n    return json.dumps(f)\n") == set()


class TestTheCommittedFixtureAgreesWithTheGenerator:
    def test_the_stamp_helper_reads_what_the_writer_wrote(self) -> None:
        """Rule 131: the producer and the consumer of this stamp read one declaration. The
        writer sets ``scorer_version``; ``stamped_scorer`` is what the refusal reads it back
        with, and a rename on one side alone would make every refusal see an unstamped
        fixture -- which fails closed, and so would never announce itself."""
        assert extract.stamped_scorer(json.loads(extract.OUT.read_text())) == SCORER_VERSION

    def test_the_committed_fixture_carries_no_free_text_the_charset_would_reject(self) -> None:
        """``scorer_note`` is the only human-typed prose in a committed fixture, and
        ``TestFixtureStaysDeidentified`` inspects keys and genre values rather than values at
        arbitrary keys -- so a note naming a host or carrying a count would commit clean past
        it. Rule 131: this reads the same declaration ``check_reason`` enforces at the
        boundary, so a note that arrived by hand is held to what the flag would have allowed.
        """
        note = json.loads(extract.OUT.read_text()).get("scorer_note")

        assert note is None or extract.check_reason(note) == note

    def test_the_committed_fixture_still_has_vectors_to_compare(self) -> None:
        """The refusal is only worth anything over a populated fixture: an emptied vector
        list re-pins clean and reads as a scorer that was checked (rule 145 -- a walk that
        collects nothing looks exactly like a walk that found nothing wrong)."""
        assert len(json.loads(extract.OUT.read_text())["vectors"]) > 0
