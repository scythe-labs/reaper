# SPDX-License-Identifier: AGPL-3.0-or-later
"""The whole-library capture's guard, and the sets it hand-writes.

``scripts/baseline_capture.py`` needs a real library, so almost none of it runs here. Two
things do, and they are the two that decide whether the committed file is safe to commit:

* **The de-identification guard**, which refuses every string that is not an item id, a
  digest, or a term this capture is allowed to use. Three of ``build_plan``'s refusals name
  the media keys they refused on, so a message written into the file rather than printed
  would leak exactly what the golden rule forbids.
* **The two hand-written sets** the guard allows through. A hardcoded list mirroring a
  declaration usually carries a drift guard, and neither of these has a declaration to
  derive from yet, so the guard itself is the test that fails when the source set moves.

The capture itself is not replayed by anything, and this file must not read as coverage of
a run nobody performs. It is diffed by hand at phase boundaries, which
``docs/history/SIMPLIFICATION_PLAN.md``'s S8 governs.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from tests._generators import functions_that_can_write, keyword_string_values, load_script

REPO = Path(__file__).resolve().parents[1]
_SCRIPT = REPO / "scripts" / "baseline_capture.py"
_PLANNER = REPO / "src" / "reaper" / "services" / "planner.py"

capture = load_script(_SCRIPT)


def _committed() -> dict[str, Any]:
    parsed: dict[str, Any] = json.loads(capture.OUT.read_text())
    return parsed


class TestTheCommittedCaptureCarriesNothingIdentifying:
    """The golden rule binds a committed artifact exactly as it binds code. This is the file
    on disk, checked with the same guard that let it be written, so a capture edited by hand
    after the fact is held to the same bar as one the script produced."""

    def test_the_guard_finds_nothing_to_object_to(self) -> None:
        assert capture.offending_strings(_committed()) == []

    def test_no_identifying_key_survives(self) -> None:
        """Keys as well as values, because a leak can arrive as either. ``media_key`` is the
        one that matters: it is the coordinate every plan is built in, and expressing the plan
        in item ids instead is the whole of this file's de-identification."""
        forbidden = {"title", "media_key", "group_key", "rating_key", "host", "path", "user"}

        def walk(obj: Any) -> None:
            if isinstance(obj, dict):
                for key, value in obj.items():
                    assert key not in forbidden, f"identifying key {key!r} in the capture"
                    walk(value)
            elif isinstance(obj, list):
                for value in obj:
                    walk(value)

        walk(_committed())

    def test_every_item_id_is_positional_and_dense(self) -> None:
        """The ids are the file's whole coordinate system: a plan is a list of them, and a
        diff between two captures is a comparison of them. A gap or a repeat means the
        mapping is no longer a bijection with the snapshot's rows, and every set-membership
        reading after that is wrong in a way the eye cannot catch."""
        items = _committed()["items"]

        assert [item["id"] for item in items] == [f"i{index:04d}" for index in range(len(items))]

    def test_the_plan_names_only_items_the_capture_carries(self) -> None:
        """A planned id with no item is a dangling reference. It would read as a plan
        covering something the snapshot does not hold."""
        data = _committed()
        known = {item["id"] for item in data["items"]}
        planned = data["plan"].get("items_in_ordinal_order", [])

        assert not set(planned) - known


class TestTheGuardRefusesWhatItMustRefuse:
    """The guard scans values, so it is proven against the forms a leak arrives in, not
    only against the file that already passes. Every case here is a real shape, a media
    key, a title, a filesystem path, a host, rather than an invented one."""

    @pytest.mark.parametrize(
        "leaked",
        [
            "radarr:1:4821",
            "sonarr:3:707:1",
            "/mnt/media/movies/Some Film (2011)/file.mkv",
            "plex.example.internal",
            "Some Film",
            "someone@example.com",
        ],
    )
    def test_a_leaked_string_is_reported_wherever_it_sits(self, leaked: str) -> None:
        payload = {"plan": {"items_in_ordinal_order": ["i0000", leaked]}}

        offenders = capture.offending_strings(payload)

        assert offenders == [f"plan.items_in_ordinal_order[1]: {leaked!r}"]

    def test_the_report_names_the_path_that_produced_it(self) -> None:
        """A refusal saying only "something is wrong" sends the operator to read an 800 KB
        file by eye. The path is what makes the refusal actionable."""
        payload = {"snapshot": {"nested": [{"deep": "a real title"}]}}

        assert capture.offending_strings(payload) == ["snapshot.nested[0].deep: 'a real title'"]

    @pytest.mark.parametrize(
        "allowed",
        ["i0000", "i9999", "i10000", "692672ba5d67", "movie", "season", "condemn", "planned"],
    )
    def test_the_vocabulary_and_the_shapes_pass(self, allowed: str) -> None:
        assert capture.offending_strings({"k": allowed}) == []

    @pytest.mark.parametrize("allowed", [0, 5965, -1, 1.5, True, False, None])
    def test_numbers_booleans_and_nulls_are_not_inspected(self, allowed: object) -> None:
        """The capture is counts by construction, so refusing them would refuse the file."""
        assert capture.offending_strings({"k": allowed}) == []

    def test_a_short_hex_string_is_not_taken_for_a_digest(self) -> None:
        """The digest shape is what lets an arbitrary-looking string through, so it is bounded
        at both ends. ``abc`` is hex and is not a digest. A title that happens to be all
        hex characters and long enough would pass, which is the residual hole and is named
        here rather than left for someone to discover."""
        assert capture.offending_strings({"k": "abc"}) == ["k: 'abc'"]

    def test_the_writer_refuses_rather_than_writing_and_warning(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The interlock's own test. A guard that printed and wrote anyway would put the
        leak on disk and in the commit, which is the only place it matters."""
        out = tmp_path / "capture.json"
        monkeypatch.setattr(capture, "OUT", out)

        with pytest.raises(SystemExit) as caught:
            capture.write_capture({"items": [{"id": "i0000", "leaked": "radarr:1:4821"}]})

        assert "refusing to write" in str(caught.value)
        assert not out.exists()

    def test_a_clean_payload_is_written(self) -> None:
        """The other direction: a guard that refused everything would be indistinguishable
        from one that works, until the day someone needs a capture."""
        assert capture.offending_strings({"items": [{"id": "i0000", "verdict": "protect"}]}) == []


class TestOneWriterRunsTheGuard:
    """The guard lives inside the writer, so a second writer would be a second path where it
    is not. That path is also the one needing a real library, so no test could reach it. The
    sibling script rests on the same argument."""

    def test_the_capture_has_exactly_one_writer(self) -> None:
        assert functions_that_can_write(_SCRIPT.read_text()) == {"write_capture"}

    def test_the_matcher_sees_the_forms_a_second_writer_would_use(self) -> None:
        """Proven here as well as on the sibling, because the two files could diverge in
        which spelling they reach for and this is the shared matcher for both."""
        alias = 'def sneak(f):\n    writer = OUT.write_text\n    writer("x")\n'

        assert functions_that_can_write(alias) == {"sneak"}


class TestTheHandWrittenSetsAgreeWithTheirSource:
    """The step kinds have no declaration to derive from, so they are reconciled against
    the source that produces them, count as well as membership, because a matcher that
    silently collected nothing would agree with an emptied set.

    The verdicts used to be reconciled here the same way. They are not any more.
    ``Verdict`` is a ``Literal`` in ``engine.verdict`` and the capture reads it, so mypy
    fails the engine if a ``return`` in ``decide_verdict`` leaves the set. That declaration
    is the real source of truth, and a test walking the AST beside it would be a second
    answer to a settled question.
    """

    def test_the_step_kinds_are_the_ones_the_planner_writes(self) -> None:
        """The capture publishes the kinds a plan produced. A kind added to the planner and
        not here refuses the next capture outright, which is the guard working but is a
        confusing way to find out. A kind removed leaves a term the file may carry that the
        planner never writes."""
        from_planner = keyword_string_values(_PLANNER.read_text(), "kind")

        assert from_planner == capture._STEP_KINDS
        assert len(from_planner) == 4

    def test_the_committed_capture_uses_only_terms_the_sets_allow(self) -> None:
        """The population the guard scans, not the population the sets describe. The two
        agree today. A capture carrying a verdict the engine no longer returns would show
        up here and nowhere else."""
        data = _committed()
        assert {item["verdict"] for item in data["items"]} <= capture._VERDICTS
        assert set(data["plan"].get("step_kinds", [])) <= capture._STEP_KINDS


class TestTheSnapshotChoiceRefusesAMeaninglessDiff:
    """Item ids are positional, so a capture of a different snapshot moves every line at
    once, and S8 reads unexplained movement as a stop. Defaulting to the committed
    snapshot is what keeps a re-capture comparable. The refusal is what keeps a silent
    re-base from looking like one."""

    @pytest.fixture
    def db(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(":memory:")
        conn.execute("CREATE TABLE snapshot (id INTEGER PRIMARY KEY, degraded INTEGER)")
        conn.executemany("INSERT INTO snapshot VALUES (?, ?)", [(41, 0), (42, 1), (43, 0)])
        yield conn
        # Closed here rather than left to the garbage collector, which reports an unclosed
        # database during whichever test it happens to run in.
        conn.close()

    def test_the_committed_snapshot_is_the_default(
        self, db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(capture, "committed_snapshot_id", lambda: 41)

        assert capture.choose_snapshot(db, None) == 41

    def test_a_snapshot_this_database_lacks_is_refused(
        self, db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The tester who restored a database, or moved to another machine. Capturing the
        newest snapshot instead would produce a file that looks like a regression in every
        line and is a comparison of two different libraries."""
        monkeypatch.setattr(capture, "committed_snapshot_id", lambda: 99)

        with pytest.raises(SystemExit) as caught:
            capture.choose_snapshot(db, None)

        assert "not in this database" in str(caught.value)

    def test_re_basing_is_available_and_deliberate(
        self, db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The audit outlives a scan schedule, so moving to a newer snapshot has to be
        possible. It is asked for by id rather than arrived at by default."""
        monkeypatch.setattr(capture, "committed_snapshot_id", lambda: 41)

        assert capture.choose_snapshot(db, 43) == 43

    def test_a_first_capture_takes_the_newest_undegraded_snapshot(
        self, db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """43 rather than 42: a degraded snapshot missed a source, so what it concluded is
        not a baseline anything should be judged against."""
        monkeypatch.setattr(capture, "committed_snapshot_id", lambda: None)

        assert capture.choose_snapshot(db, None) == 43


class TestTheCommandLineCannotBeMissedByOneCharacter:
    def test_no_flags_captures_the_committed_snapshot(self) -> None:
        assert capture.parse_argv([]) is None

    @pytest.mark.parametrize("argv", [["--snapshot", "42"], ["--snapshot=42"]])
    def test_both_spellings_of_the_id_flag_are_read(self, argv: list[str]) -> None:
        assert capture.parse_argv(argv) == 42

    @pytest.mark.parametrize("argv", [["--snapshots=1"], ["-snapshot", "1"], ["42"], ["--latest"]])
    def test_a_near_miss_is_refused_rather_than_run_as_the_default(self, argv: list[str]) -> None:
        """The default answers about a different snapshot than the one asked for, silently.
        Same posture as the sibling script, and for the same reason: a typo happens exactly
        when someone is retyping a command a refusal asked them to retype."""
        with pytest.raises(SystemExit) as caught:
            capture.parse_argv(argv)

        assert "unrecognized argument" in str(caught.value)

    @pytest.mark.parametrize("raw", ["latest", "-1", "4.2", ""])
    def test_a_snapshot_id_that_is_not_a_number_is_refused(self, raw: str) -> None:
        with pytest.raises(SystemExit) as caught:
            capture.parse_argv(["--snapshot", raw])

        assert "snapshot id" in str(caught.value)

    def test_the_space_form_with_nothing_after_it_is_refused(self) -> None:
        with pytest.raises(SystemExit) as caught:
            capture.parse_argv(["--snapshot"])

        assert "needs an id" in str(caught.value)
