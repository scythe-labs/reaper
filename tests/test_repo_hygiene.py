"""Repo-wide invariants that were previously only prose in the instruction files.

CLAUDE.md and ``.claude/rules/*.md`` are context, not enforcement: an agent that never
loads them, or a human who never reads them, breaks the rule silently. Every rule in here
is one that can be checked mechanically, so it costs nothing to enforce and catches humans
and agents alike. A rule that needs judgment stays prose; only the greppable ones live here.

These are filesystem checks over this checkout, and they reach no network. Two things they do
reach: ``reaper.engine.gates`` is imported for the one guard that derives its expectation by
running the gate, and ``git`` is run to list the files ``_repo_text_files`` walks.
"""

from __future__ import annotations

import ast
import dataclasses
import hashlib
import json
import os
import re
import subprocess
import xml.etree.ElementTree as ET
from collections import Counter
from datetime import timedelta
from functools import lru_cache
from pathlib import Path
from typing import Any, NamedTuple

import pytest
import yaml

# The one guard here that reads the app rather than the tree: the documented example of a
# checked protection is derived by running the gate that builds it, never transcribed
# (rule 119). The hygiene CI lane installs the whole package, so this costs nothing extra.
from reaper.engine.gates import (
    Facts,
    GateConfig,
    MinDormancyGate,
    ServerPopularityGate,
)
from reaper.engine.observation import Absent, Known
from reaper.services import plex_link
from reaper.services.scheduler import SCHEDULABLE_JOB_IDS
from reaper.services.season_scan import SeasonJudgment
from reaper.services.snapshot import Display, RawItem

REPO = Path(__file__).resolve().parents[1]
SELF = Path(__file__).resolve()
SRC = REPO / "src" / "reaper"
TESTS = REPO / "tests"
FRONTEND_SRC = REPO / "frontend" / "src"
DOCS = REPO / "docs"
HISTORY = DOCS / "history"

# docs/ splits by how long a statement stays true (see docs/README.md). Live docs are held to
# the same standards as code; docs/history/ is frozen and deliberately describes the past, so
# its stale dates, TBD placeholders and superseded rule wordings are correct as written.
STATUS_DOC = DOCS / "STATUS.md"
STATUS_MAX_LINES = 120
# A line budget alone is not a size budget. STATUS.md sat at exactly 200/200 lines for days, so
# every new fact had to be appended to a line that already existed -- and a markdown table row
# cannot wrap, so one "Decisions locked" cell reached 21,210 characters. The width cap is what
# makes the line cap honest, and it is the repo's one width (ruff line-length, prettier
# printWidth). Together they bound the file's total size, which is the thing that has to stay
# small.
STATUS_MAX_COLUMNS = 100
DECISIONS_DOC = DOCS / "DECISIONS.md"
# Rows of "Decisions locked" carrying the dagger, reconciled by hand against DECISIONS.md's
# sections (rule 145: a set-equality assertion cannot tell a member that complies from one that
# dropped out of the walk).
DECISION_SECTIONS = 18


def _live_docs() -> list[Path]:
    """Every doc that claims to describe the present. Excludes the frozen archive."""
    return sorted(p for p in DOCS.rglob("*.md") if HISTORY not in p.parents)


INSTRUCTION_FILES = [REPO / "CLAUDE.md", *sorted((REPO / ".claude" / "rules").glob("*.md"))]


# Tab, newline and carriage return are the only control characters source here is allowed to hold.
# Everything else in the C0 range, plus DEL, is invisible in an editor and in a diff.
_ALLOWED_CONTROL_BYTES = frozenset(b"\t\n\r")


# Cached because the two walks in this file are its whole runtime: eleven call sites re-globbed
# the tree, and seven of them re-read every byte of it. The staleness question is answered by
# what this file is -- filesystem checks over a checkout nothing here mutates, so the tree cannot
# change under the cache mid-session. Every caller builds a new list from the result and none
# sorts or appends in place, so the cached value needs no defensive copy. Cost: the text walk
# pins about 23 MiB per xdist worker for the session.
#
# One exception, and it is the reason it is written down. Both walks read the module global
# ``REPO``, and ``test_the_repo_walk_never_reads_a_gitignored_file`` repoints it at a synthetic
# tree, so there the cache CAN go stale. It calls ``cache_clear`` on both sides of the swap.
# A later swap that skips the clear serves that two-file walk to every gate downstream.
@lru_cache
def _source_files_to_scan() -> list[Path]:
    """Every hand-written source and instruction file, for the byte-level scan below."""
    trees = [
        *SRC.rglob("*.py"),
        *TESTS.rglob("*.py"),
        *FRONTEND_SRC.rglob("*.ts"),
        *FRONTEND_SRC.rglob("*.tsx"),
        *FRONTEND_SRC.rglob("*.css"),
        *_live_docs(),
        *INSTRUCTION_FILES,
    ]
    return sorted({p for p in trees if p.is_file()})


def test_no_source_file_holds_an_invisible_control_character() -> None:
    """A stray control byte blinds every grep-shaped gate in this file, silently.

    A NUL reached a string literal in ``ServiceModal.tsx`` during #178 -- as the separator in a
    ``.join()``, so it was syntactically fine and behaved correctly. ``tsc``, ``eslint``,
    ``prettier`` and 809 vitest tests all passed on it. What broke was **reading**: ``grep`` classes
    a file holding a NUL as binary and reports no matches at all rather than an error, and ``file``
    called it ``data``. So `grep -n ModalShell ServiceModal.tsx` came back empty on a file that
    imports it on line 21.

    That is the whole failure: this suite's guards are source-text scans, and rule 147 bounds them
    by the syntax they can parse. A file grep silently declines to read is absent from every one of
    them while they all stay green -- the same shape as a member dropping out of a walk (rule 145),
    reached by a route no matcher can see. Hence a byte-level check, which is the only kind that can
    catch it.
    """
    offenders: list[str] = []
    for path in _source_files_to_scan():
        blob = path.read_bytes()
        first = next(
            (
                i
                for i, b in enumerate(blob)
                if (b < 0x20 or b == 0x7F) and b not in _ALLOWED_CONTROL_BYTES
            ),
            None,
        )
        if first is not None:
            line = blob.count(b"\n", 0, first) + 1
            offenders.append(
                f"{path.relative_to(REPO).as_posix()}:{line} holds 0x{blob[first]:02x}"
            )
    assert not offenders, (
        "invisible control characters in source, which make grep treat the file as binary and "
        "report no matches -- so every text-scanning guard in this file goes quiet on it:\n"
        + "\n".join(offenders)
    )


# A middot written as anything except itself. Every one of these renders as the character, and
# not one of them is visible to ``grep '·'`` -- which is the entire reason this gate exists.
# The escape forms are pinned to their exact digit counts (``\u`` takes four, ``\U`` eight), so
# an ordinary escape that merely opens with those digits -- a four-digit U+B7C1 -- is not
# collected.
_MIDDOT_IN_DISGUISE = re.compile(
    r"&middot;|&#0*183;|&#x0*b7;|\\u00b7|\\U000000b7|\\u\{0*b7\}|\\xb7|\\N\{MIDDLE DOT\}",
    re.IGNORECASE,
)
# The CSS spelling has no ``u``: ``content: "\B7"``, up to four leading zeros. Scoped to
# stylesheets because the same shape is ``\b`` (a word boundary) followed by a 7 in a Python
# regex, and a gate with a false positive is a gate someone deletes.
_MIDDOT_CSS_ESCAPE = re.compile(r"\\0*b7(?![0-9a-f])", re.IGNORECASE)


def _middot_in_disguise(path: Path, line: str) -> bool:
    """Whether ``line`` writes a middot as something other than the character itself."""
    if _MIDDOT_IN_DISGUISE.search(line):
        return True
    return path.suffix == ".css" and _MIDDOT_CSS_ESCAPE.search(line) is not None


def test_a_middot_is_written_as_itself_everywhere() -> None:
    """A character spelled as an entity or an escape is invisible to the sweep that removes it.

    Rule 21 stopped blessing the middot as a separator between two facts: a reader either voices
    it ("40 titles *middle dot* 1.2 TB freed") or drops it and runs the two facts together, so
    the separator is a comma now. The sweep that converted 49 of them (#177) matched the literal
    character -- and four sites spelling it ``&middot;`` plus one spelling it ``\\u00b7`` came
    through untouched, still separating two facts in running text on the scan bar, the show panel
    and the why panel (#299). They were not missed by judgment. They were unreadable to the tool.

    So this does not police what a middot MEANS -- rule 21 owns that, and a decorative one is
    still fine where it carries ``aria-hidden``. It polices the one thing a matcher can settle:
    that the tree spells the character exactly one way, so the next person's ``grep '·'`` sees
    every site there is. That is rule 147 turned on its own population: the guard against a
    source-text scan being bounded by the syntax it can parse is to leave the tree only one
    syntax to parse.
    """
    # ``index.html`` joins the usual walk because the named entity is HTML's OWN spelling, so the
    # one hand-written HTML file here is the likeliest place for it and sits outside every other
    # scan in this module. This file itself drops out: its tables below hold every spelling.
    index_html = REPO / "frontend" / "index.html"
    scanned = [p for p in (*_source_files_to_scan(), index_html) if p != SELF]
    # A zero-offender assertion passes just as happily over an empty walk, which is how a
    # mis-rooted glob hides (rule 145). The frontend is the half the defect shipped in.
    assert any(p.suffix == ".tsx" for p in scanned), "the walk reached no components at all"

    offenders = [
        f"{path.relative_to(REPO).as_posix()}:{lineno} -> {line.strip()}"
        for path in scanned
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
        if _middot_in_disguise(path, line)
    ]
    assert not offenders, (
        "a middot written as an entity or an escape, which no grep for the character can find --\n"
        "write it as · so the next sweep of rule 21's separator ban can see it (#299):\n"
        + "\n".join(offenders)
    )


def test_the_middot_spelling_matcher_reads_every_spelling_it_claims() -> None:
    """Rule 147: the ban above is bounded by what its regex can parse, so prove the parse.

    The accepted list is every form that renders as a middot; the two that actually shipped are
    the first of the entities and the first of the escapes. The rejects are the near misses that
    must stay out -- above all the literal character, which is the spelling the ban is steering
    the tree TOWARD and would be an absurd thing to fire on.
    """
    css = Path("x.css")
    ts = Path("x.tsx")
    accepted = [
        (ts, "TV show &middot; {seasonLabel}"),
        (ts, "TV show &MIDDOT; {seasonLabel}"),
        (ts, "items &#183; more"),
        (ts, "items &#0183; more"),
        (ts, "items &#xB7; more"),
        (ts, "items &#x00b7; more"),
        (ts, "` \\u00b7 your threshold is ${n}`"),
        (ts, "` \\u00B7 `"),
        (ts, r'"\u{b7}"'),
        (ts, r'"\xb7"'),
        (Path("x.py"), r'"\N{MIDDLE DOT}"'),
        (Path("x.py"), r'"\U000000b7"'),
        (css, r'content: "\B7";'),
        (css, r'content: "\0000b7";'),
    ]
    rejected = [
        # The spelling the ban wants. Firing on this would ban the character outright.
        (ts, 'Keeps it, always<span aria-hidden="true"> · </span>'),
        # A four-digit escape that merely opens with b7, and a hex color that has no backslash.
        (ts, '"\\uB7C1"'),
        (ts, "color: #00b7ff;"),
        # A decimal entity whose digits continue past 183.
        (ts, "&#1830;"),
        # The CSS form is scoped to stylesheets: this is a word boundary in a Python regex.
        (Path("x.py"), r're.compile(r"\b7\b")'),
    ]
    missed = [line for path, line in accepted if not _middot_in_disguise(path, line)]
    assert not missed, "the matcher cannot read spellings the ban claims to cover:\n" + "\n".join(
        missed
    )
    false_positives = [line for path, line in rejected if _middot_in_disguise(path, line)]
    assert not false_positives, (
        "the matcher collects lines that do not hide a middot:\n" + "\n".join(false_positives)
    )


# A rule definition opens a line as ``**12.`` or ``**3 / 22.``
_RULE_DEF = re.compile(r"^\*\*(\d+(?:\s*/\s*\d+)*)\.\s")
# A citation in code: "rule 28", "rules 4/71", "rule #2".
_RULE_CITE = re.compile(r"\brules?\s*#?\s*(\d+(?:\s*/\s*\d+)*)", re.IGNORECASE)


def _strip_prose(line: str) -> str:
    """Drop backticked spans, so a comment explaining a ban is not read as breaking it.

    The banned-call checks below look for real calls. ``lists.py`` documents at length why
    ``library.section(title)`` is forbidden, and that prose must not trip the guard.
    """
    return re.sub(r"``[^`]*``|`[^`]*`", "", line)


def _code_files() -> list[Path]:
    out: list[Path] = []
    for root, globs in (
        (SRC, ("*.py",)),
        (TESTS, ("*.py",)),
        (FRONTEND_SRC, ("*.ts", "*.tsx", "*.css")),
    ):
        for pattern in globs:
            out.extend(p for p in root.rglob(pattern) if p.is_file())
    return out


def _code_and_live_docs() -> list[Path]:
    return [*_code_files(), *_live_docs()]


def _defined_rules() -> dict[int, list[Path]]:
    defined: dict[int, list[Path]] = {}
    for path in INSTRUCTION_FILES:
        for line in path.read_text(encoding="utf-8").splitlines():
            match = _RULE_DEF.match(line)
            if match:
                for number in re.findall(r"\d+", match.group(1)):
                    defined.setdefault(int(number), []).append(path)
    return defined


def test_instruction_files_exist() -> None:
    """The routing table in CLAUDE.md points at files that exist.

    Rule 64: removing a surface removes its whole supply chain. A rule file named by the
    index but absent from disk is the same failure as a dangling route.
    """
    missing = [p for p in INSTRUCTION_FILES if not p.is_file()]
    assert not missing, f"instruction files named but absent: {missing}"
    assert len(INSTRUCTION_FILES) >= 4, "expected CLAUDE.md plus the scoped rule files"


def test_rule_numbers_are_contiguous_and_unique() -> None:
    """Every rule number is defined exactly once, with no gaps.

    The numbers are permanent because code cites them. A duplicate means two rules answer
    to one citation; a gap means a citation can point at nothing.
    """
    defined = _defined_rules()
    duplicated = {n: [str(p.name) for p in v] for n, v in defined.items() if len(v) > 1}
    assert not duplicated, f"rule numbers defined more than once: {duplicated}"

    highest = max(defined)
    gaps = [n for n in range(1, highest + 1) if n not in defined]
    assert not gaps, f"rule numbers with no definition: {gaps}"


def _rules_by_file() -> dict[str, set[int]]:
    """Which rule numbers each ``.claude/rules/`` file actually defines."""
    out: dict[str, set[int]] = {}
    for path in INSTRUCTION_FILES:
        if path.name == "CLAUDE.md":
            continue
        numbers: set[int] = set()
        for line in path.read_text(encoding="utf-8").splitlines():
            match = _RULE_DEF.match(line)
            if match:
                numbers.update(int(n) for n in re.findall(r"\d+", match.group(1)))
        out[path.name] = numbers
    return out


def _expand(cell: str) -> set[int]:
    """``1-6, 8, 22-23`` -> the set it names. Accepts hyphen or en dash."""
    out: set[int] = set()
    for part in cell.split(","):
        bounds = re.findall(r"\d+", part)
        if len(bounds) == 2:
            out.update(range(int(bounds[0]), int(bounds[1]) + 1))
        elif len(bounds) == 1:
            out.add(int(bounds[0]))
    return out


def test_every_index_of_the_rules_matches_the_rules() -> None:
    """Every restatement of "which rules live where" is checked against the files.

    Rule 144: one fact about the app is normally written down in several places, and deriving
    one of them does not make the rest safe. This is that fact for the rule corpus itself --
    the count and the per-file ranges appear in CLAUDE.md's routing table, in each rule file's
    own "Holds" line, and in the review skill. They drifted exactly as rule 144 predicts: the
    skill claimed 133 blockers for 13 rules' worth of growth, in the same paragraph that tells
    a reviewer not to restate the rules.

    The failure message names every file to edit, because a test that only says "these
    disagree" costs the next author the search that this test exists to spare them.
    """
    actual = _rules_by_file()
    claude_md = (REPO / "CLAUDE.md").read_text(encoding="utf-8")
    problems: list[str] = []

    # 1. CLAUDE.md's routing table, one row per rule file.
    row = re.compile(r"^\|\s*`\.claude/rules/([\w.-]+\.md)`\s*\|(.*)\|([^|]*)\|\s*$")
    listed: set[str] = set()
    for line in claude_md.splitlines():
        match = row.match(line)
        if not match:
            continue
        name, claimed = match.group(1), _expand(match.group(3))
        listed.add(name)
        if name not in actual:
            problems.append(f"CLAUDE.md's table names {name}, which does not exist")
        elif claimed != actual[name]:
            problems.append(
                f"CLAUDE.md's table row for {name} lists {sorted(claimed - actual[name])} "
                f"it does not define and omits {sorted(actual[name] - claimed)}"
            )
    for name in sorted(set(actual) - listed):
        problems.append(f".claude/rules/{name} exists but CLAUDE.md's table has no row for it")

    # 2. Each rule file's own "Holds ..." line.
    for path in INSTRUCTION_FILES:
        if path.name == "CLAUDE.md":
            continue
        holds = re.search(r"Holds ([0-9,\u2013\-\s]+)\.", path.read_text(encoding="utf-8"))
        if holds is None:
            problems.append(f"{path.name} has no 'Holds ...' line naming the rules it carries")
        elif _expand(holds.group(1)) != actual[path.name]:
            problems.append(
                f"{path.name}'s 'Holds' line disagrees with the rules it defines: "
                f"claims {sorted(_expand(holds.group(1)) - actual[path.name])} extra, "
                f"omits {sorted(actual[path.name] - _expand(holds.group(1)))}"
            )

    # 3. Any prose that states the total, anywhere an agent reads instructions.
    total = len(_defined_rules())
    counted = re.compile(r"(\d+)\s+(?:numbered\s+)?(?:blockers|rules)\b")
    for path in [*INSTRUCTION_FILES, *sorted((REPO / ".claude" / "skills").rglob("*.md"))]:
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            for match in counted.finditer(line):
                if int(match.group(1)) != total:
                    rel = path.relative_to(REPO)
                    problems.append(
                        f"{rel}:{lineno} says {match.group(1)} rules; there are {total}"
                    )

    assert not problems, (
        "the rule corpus and its indexes disagree:\n  "
        + "\n  ".join(problems)
        + f"\n\nThere are {total} rules. Fix every file named above, not just the first."
    )


def test_every_rule_citation_in_code_resolves() -> None:
    """A comment may only cite a rule that exists.

    A review pass found 37 comments citing rules 70-87 while the list ended at 69, making
    every one of them unverifiable. This is the guard that makes that impossible.
    """
    defined = _defined_rules()
    dangling: list[str] = []
    for path in _code_and_live_docs():
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            for match in _RULE_CITE.finditer(line):
                for number in re.findall(r"\d+", match.group(1)):
                    if int(number) not in defined:
                        rel = path.relative_to(REPO)
                        dangling.append(f"{rel}:{lineno} cites rule {number}")
    assert not dangling, "citations with no rule behind them:\n" + "\n".join(dangling)


def test_plex_sections_are_never_resolved_by_title() -> None:
    """Rule 57: ``library.section(title)`` is banned in ``src/`` outright.

    Two libraries can share a title, and the first match silently wins -- so a removal can
    address the wrong library. ``sectionByID`` is the only resolver.
    """
    offenders = [
        f"{p.relative_to(REPO)}:{n}"
        for p in SRC.rglob("*.py")
        for n, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1)
        if "library.section(" in _strip_prose(line)
    ]
    assert not offenders, "resolve Plex sections by key, never by title:\n" + "\n".join(offenders)


def test_no_bare_exception_assertions_in_tests() -> None:
    """Rule 119: assert the domain error and its message, never ``pytest.raises(Exception)``.

    A bare Exception assertion passes when the code raises something entirely unrelated,
    including the AttributeError of a refactor that broke the path under test.
    """
    offenders = [
        f"{p.relative_to(REPO)}:{n}"
        for p in TESTS.rglob("*.py")
        for n, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1)
        if re.search(r"pytest\.raises\(\s*Exception\s*[,)]", _strip_prose(line))
    ]
    assert not offenders, "assert the domain error, not bare Exception:\n" + "\n".join(offenders)


def test_http_clients_are_only_constructed_in_clients() -> None:
    """Rule 33: all HTTP lives in ``clients/``.

    The transport guard is installed by the shared clients; a client built anywhere else
    carries no guard, so a mutating call could leave without an armed host or a journalled
    intent. ``notify/discord.py`` is the one sanctioned exception (its webhook URL embeds a
    per-operator secret path the guard's allow-list cannot express).
    """
    sanctioned = {SRC / "notify" / "discord.py"}
    constructors = re.compile(r"\b(?:httpx2?|requests)\.(?:Async)?(?:Client|Session)\s*\(")
    offenders = [
        f"{p.relative_to(REPO)}:{n}"
        for p in SRC.rglob("*.py")
        if "clients" not in p.parts and p not in sanctioned
        for n, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1)
        if constructors.search(_strip_prose(line))
    ]
    assert not offenders, "construct HTTP clients only in clients/:\n" + "\n".join(offenders)


def test_the_admin_password_lockout_is_reached_through_one_function() -> None:
    """Rules 11/98 as a gate: only ``api/deps.py`` reaches the lockout or the verify.

    Four routes ask for the admin password before doing something consequential, and each
    used to run the same four steps by hand: check the lockout, verify, record the failure,
    clear both keys on success. Copying them is how a fifth gate arrives with three of the
    four, and the step most easily dropped is the one no route-level test used to reach --
    ``record_success``, whose absence makes a near-miss cost the operator a real lockout.

    Prose cannot bind an author who never read it, so the ban is on the names instead: a route
    that can reach neither the throttle nor the verify cannot get the ritual wrong. Two names
    are banned rather than one, and the second is the one that matters. Banning
    ``password_throttle`` alone stops a gate with three of the four steps; a gate calling
    ``admin_password.verify`` straight is a gate with **none** of them, and every one of these
    routers already imports that module, so it is the shorter mistake to make.

    **Measured against the spellings, not assumed (rule 147).** The matcher is a substring test
    over `_strip_prose`, so it catches the dotted form
    (``ratelimit.password_throttle.record_failure(...)``), the fully qualified form, and an
    ``import ... as`` rebinding, which is caught at the import line. What it does not catch is a
    name assembled at runtime (``getattr(ratelimit, "password_" + "throttle")``). An earlier
    version of this docstring named the first two as blind spots and both were already covered;
    the escape it missed is the one now banned on the second line.
    """
    allowed = {
        "password_throttle": {SRC / "auth" / "ratelimit.py", SRC / "api" / "deps.py"},
        "admin_password.verify": {SRC / "api" / "deps.py"},
    }
    offenders = [
        f"{p.relative_to(REPO)}:{n}  {banned}"
        for banned, exempt in allowed.items()
        for p in SRC.rglob("*.py")
        if p not in exempt
        for n, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1)
        if banned in _strip_prose(line)
    ]
    assert not offenders, (
        "reach the admin-password gate through reaper.api.deps.require_admin_password, "
        "which is the whole ritual -- lockout check, verify, record the failure, clear both "
        "keys on success -- rather than spelling any part of it again (rule 11/98):\n"
        + "\n".join(sorted(offenders))
    )


def test_the_comparison_form_of_a_name_is_one_derivation() -> None:
    """Rule 88, as a gate. ``reaper.text.fold`` is the trim-then-casefold every name
    comparison takes, and it was spelled 37 times over 33 lines in 13 modules.

    **It bans the composite only.** A bare ``.casefold()`` on a value a line above already
    stripped is not an offender: ``fields._split_csv``, ``fields._shared`` and
    ``list_config._clean_config`` all read input their caller stripped, so folding again
    would be identical and an exemption entry for each would be a skip list nobody rereads.

    **What it cannot catch, stated rather than implied**: a new site written as a bare
    ``.casefold()`` on unstripped input, or as ``.strip().lower()``. Eleven of the latter
    exist at this tip and every one is deliberate -- env tokens, hostnames, media types, hex
    colors -- two of them folding PATHS (``identity.to_basename``, ``to_segments``), whose
    docstrings say why lower is the right answer there. That figure is measured at the tip
    rather than at ``dev``, where it is sixteen: #668 landed on this same branch and took
    six of them.

    ``alembic/`` is out of scope: those revisions are frozen and five of them carry the
    idiom.
    """
    offenders = [
        f"{p.relative_to(REPO)}:{n}"
        for p in SRC.rglob("*.py")
        if p != SRC / "text.py"
        for n, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1)
        if ".strip().casefold()" in _strip_prose(line)
    ]
    assert not offenders, (
        "call reaper.text.fold instead of spelling .strip().casefold() again -- one "
        "derivation, because a name folded on one side of a comparison and not the other "
        "stops matching and says nothing (rule 88):\n" + "\n".join(offenders)
    )


# The set form, ``PRAGMA journal_mode=WAL``, never the read at db/session.py:49. The read is
# how the boot log says which mode the database settled on; the set is what writes.
_JOURNAL_MODE_SET = re.compile(r"PRAGMA\s+journal_mode\s*=", re.IGNORECASE)
#: One site, ``db.session._configure_sqlite``. Pinned as a count as well as a file set, because
#: a file set alone cannot tell a second site inside the same module from none (rule 145).
_JOURNAL_MODE_SET_SITES = 1


def test_the_journal_mode_pragma_is_set_in_exactly_one_module() -> None:
    """``PRAGMA journal_mode=WAL`` WRITES the file it is pointed at.

    Header bytes 18 and 19 flip and persist, so the app's pragma set may only ever reach a
    database Reaper owns. Three sqlite connections in ``src/`` deliberately issue no journal
    pragma, and each reads or writes a file nobody has vouched for yet:
    ``db.schema_gate.stored_revision`` reads the database unpacked from an operator-supplied
    ``.reaper`` inside the rule 74 artifact gate, before the schema is checked and before the
    operator confirms, and ``restore``'s ``_force_destructive_off`` and ``_purge_auth_state``
    run on that same staged file a moment later. Adding the set to any of them turns a read of
    an unverified artifact into a write against it.

    **What the matcher accepts and refuses** (rule 147). It takes any casing and any spacing
    around the ``=``, and it reads the line with backticked spans stripped, so a comment
    quoting the pragma is not an offender: ``db/session.py`` itself has two occurrences and
    only one is code. It cannot see a pragma assembled at runtime (``"PRAGMA " + name``), and
    nothing in the tree spells one that way. ``tests/`` is out of scope on purpose:
    ``test_startup_log.py`` sets ``journal_mode=DELETE`` to drive the boot log's WAL check.
    """
    sites = [
        f"{p.relative_to(REPO)}:{n}"
        for root in (SRC, REPO / "alembic")
        for p in sorted(root.rglob("*.py"))
        for n, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1)
        if _JOURNAL_MODE_SET.search(_strip_prose(line))
    ]
    files = {site.rsplit(":", 1)[0] for site in sites}
    assert files == {"src/reaper/db/session.py"} and len(sites) == _JOURNAL_MODE_SET_SITES, (
        "PRAGMA journal_mode=WAL writes the database it is pointed at, so only "
        "db.session._configure_sqlite may issue it. Sites found:\n  "
        + "\n  ".join(sites)
        + "\n\nA connection that reads or writes an operator-supplied backup "
        "(db/schema_gate.py, services/restore.py) must not adopt the app's pragma set: that "
        "would write to an artifact the rule 74 gate has not verified yet."
    )


def _busy_timeout_files() -> list[Path]:
    """The two roots both busy-timeout walks read, the same pair the journal-mode gate reads.

    ``alembic/`` is in scope because the migration engine opens its own connections, and
    ``env.py``'s connect hook is the obvious place to answer a migration that hit a locked
    database. Scoped to ``src/`` alone, a declaration landing there passed both gates
    (rule 72: the journal-mode gate 60 lines up already decided this).
    """
    return [p for root in (SRC, REPO / "alembic") for p in sorted(root.rglob("*.py"))]


def _busy_timeout_prose(path: Path) -> str:
    """One line of prose per file: comment markers dropped, then whitespace collapsed.

    The markers come out first so a passage wrapped across two ``#`` lines still reads as one
    sentence. Leaving them in turned ``backup._build_into``'s "the 5s busy timeout" into
    "busy # timeout" and left that module one passage short of its count, which any reformat
    can do to any of them (rule 147).
    """
    return re.sub(r"\s+", " ", re.sub(r"(?m)^\s*#+:? ?", " ", path.read_text(encoding="utf-8")))


#: A quotation of a busy timeout is a seconds figure and an anchor -- the pragma's name, or a
#: ``db.session`` citation -- in either order, in the same sentence, within 120 characters. Both
#: anchors are needed and neither is enough alone: two copies never name the pragma
#: (``scan_runner.scan_running``, ``scheduler.sweep_old_snapshots``), and one never cites
#: ``db.session`` (``imdb_dataset.load``, which is about ``cache.db``, whose engine listens to
#: the same ``_configure_sqlite``).
#:
#: Spellings accepted, run and confirmed (rule 147). Six are forms the tree uses:
#: ``5s ``busy_timeout```, ```busy_timeout`` (5s``, ``5s for it (``db.session``)``,
#: ``5s busy timeout`` spaced, ``db/session.py`` as the citation, and a passage split across two
#: ``#`` lines. Two more are accepted against no passage: ``5s BUSY_TIMEOUT`` is what
#: ``re.IGNORECASE`` takes, the tree spelling it lowercase everywhere, and ``5-second`` is the
#: adjectival form, which no busy-timeout passage uses but 39 other durations in ``src/`` do
#: (``ratelimit``'s ``2-second``, and ``30-day`` 18 times over eight files) -- so it is the form
#: a new passage is most likely to arrive in, and it was a quiet pass until it was accepted.
#:
#: Rejected, run and confirmed: ``five seconds`` in words, ``5 s`` with a space
#: (``docs/LEARNINGS.md``'s), ``5000ms``, ``session.py`` without ``db``, a figure past 120
#: characters, and a figure on the far side of a sentence boundary. That last one is the gap's
#: own bound rather than a judgment, so it also rejects a period that ends no sentence: a passage
#: writing ``the ``busy_timeout``, i.e. 5s`` goes unread, and ``src/reaper/services/`` carries 11
#: ``e.g.``/``i.e.``/``vs.`` today.
#:
#: **The sentence bound is doing work the window alone did not.** The figure pattern reads every
#: status-code plural in ``src/`` (``404s``, ``409s``, ``429s``, ``500s``, ``502s``) and every
#: other seconds figure besides. Measured over every anchor in both roots: eleven figures fall
#: inside a 120-character window, and exactly one of them carries a sentence break in its gap --
#: ``scheduler``'s *measured* ``8s`` vacuum, 86 characters past a ``db.session`` it is not about.
#: Deleting ``scheduler``'s real copy showed what that costs: the window alone then reports
#: "quotes 8s", substituting an unrelated measurement for the passage that left, while the
#: sentence bound reports "no longer quotes it", which is the truth. Nearest status-code plural
#: to any anchor at this tip: 17,851 characters.
#:
#: It bounds proximity, not meaning, so a figure sharing a sentence with an anchor it is not
#: about is still collected. That lands as a red gate naming the module **when the module's real
#: passage is collected first**; where the spurious figure comes first and the module has one
#: anchor, the real match is the one that loses, and a module already at its count reads green.
#: ``scan_runner.scan_running`` is that shape today, one anchor with its figure before it.
_BUSY_TIMEOUT_ANCHOR = r"(?:db[./]session|busy[_ ]timeout)"
_BUSY_TIMEOUT_GAP = r"(?:(?!\.\s).){0,120}?"
_BUSY_TIMEOUT_FIGURE = r"\b(\d+)(?:s|-seconds?)\b"
_BUSY_TIMEOUT_QUOTED = re.compile(
    rf"{_BUSY_TIMEOUT_ANCHOR}{_BUSY_TIMEOUT_GAP}{_BUSY_TIMEOUT_FIGURE}"
    rf"|{_BUSY_TIMEOUT_FIGURE}{_BUSY_TIMEOUT_GAP}{_BUSY_TIMEOUT_ANCHOR}",
    re.IGNORECASE,
)
_BUSY_TIMEOUT_PRAGMA = re.compile(r"PRAGMA\s+busy_timeout\s*=\s*(\d+)")

#: Declaring file -> how many ``PRAGMA busy_timeout`` calls it makes. Pinned so a fourth
#: declaration cannot arrive without someone deciding which passages describe it (rule 145).
_BUSY_TIMEOUT_DECLARATIONS = {
    "src/reaper/db/session.py": 1,
    "src/reaper/services/backup.py": 1,
    "src/reaper/services/retention.py": 1,
}

#: File -> which declaration each of its passages quotes -> how many quote it. **The second
#: column is the point**: three declarations exist and they are not one fact.
#: ``db.session._configure_sqlite`` sets 5000 for every app connection, ``retention._compact_sync``
#: sets 30000 on the connection it opens for ``VACUUM``, and ``backup._build_into`` sets 5000 on
#: its own for ``VACUUM INTO``. Backup's figure equals the app's by coincidence, so a single
#: column would tie two values that have no reason to move together and read as a proof that
#: they do. Every figure is read out of the declaration named here, never written here.
#:
#: Ten passages over seven files, reconciled by hand against both roots (rule 145): seven quote
#: ``db/session.py``, two quote backup's own, one quotes retention's own. The count is the part a
#: set of file names cannot hold, since the walk collects passages and a second copy inside an
#: already-listed file would hide behind the first (rule 147).
_BUSY_TIMEOUT_PROSE: dict[str, dict[str, int]] = {
    "src/reaper/services/backup.py": {"src/reaper/services/backup.py": 2},
    "src/reaper/services/executor.py": {"src/reaper/db/session.py": 1},
    "src/reaper/services/imdb_dataset.py": {"src/reaper/db/session.py": 1},
    "src/reaper/services/retention.py": {
        "src/reaper/db/session.py": 2,
        "src/reaper/services/retention.py": 1,
    },
    "src/reaper/services/scan_runner.py": {"src/reaper/db/session.py": 1},
    "src/reaper/services/scheduler.py": {"src/reaper/db/session.py": 1},
    "src/reaper/services/snapshot.py": {"src/reaper/db/session.py": 1},
}


def test_every_prose_copy_of_the_busy_timeout_states_the_declared_value() -> None:
    """Rule 144: one fact in ten passages, and three declarations under them.

    ``PRAGMA busy_timeout=5000`` in ``db.session._configure_sqlite`` is how long every app
    connection waits for a write lock, and seven passages quote it as the reason something else
    is the way it is. The one on the deletion path is load-bearing: ``executor._commit_journal``
    gives the journal two attempts with no sleep between them *because* the timeout already
    waited inside each. Move the pragma and that reasoning is silently wrong, on the write that
    records what was deleted.

    **Two more declarations sit beside it, and they are not the same fact.**
    ``retention._compact_sync`` sets 30000 on the connection it opens for ``VACUUM``, and
    ``backup._build_into`` sets 5000 on its own for ``VACUUM INTO``. Backup's figure equals the
    app's by coincidence, so its two passages are checked against backup's own declaration.
    Checking them against ``db.session``'s would make two unrelated values move together forever
    and read as a proof that they must.

    Every figure is read out of the declaration its passage is about, so no number is written
    here, and the failure names each file so the sweep is the fix rather than a note asking the
    next author to remember.

    **What the walk cannot see, named rather than implied** (rule 147). A copy carrying no figure
    at all is invisible, and one survives deliberately: ``imdb_dataset.load`` says "wait out the
    timeout" three lines under its own "5s ``busy_timeout``", in the same sentence's subject, so
    it restates the figure it was just given rather than holding a second one. A copy spelling
    the value in words ("five seconds") is invisible the same way; the two roots hold none today,
    and the per-file count is what turns a rewording into a failure rather than a quiet pass.
    Within one file the comparison is a multiset, so two passages that swapped each other's
    figures would still balance: ``retention.py`` is the only file quoting two declarations, and
    its three sit apart, two docstrings and the ``SWEEP_BATCH`` comment.
    ``docs/LEARNINGS.md`` quotes both timeouts as ``5 s`` and
    ``30 s`` and is deliberately out of scope: it records what was measured on the tree of the
    day, and rewriting a measurement to match a later default would falsify it.
    """
    declared: dict[str, list[str]] = {}
    for path in _busy_timeout_files():
        seconds = [
            str(int(m.group(1)) // 1000)
            for m in _BUSY_TIMEOUT_PRAGMA.finditer(path.read_text(encoding="utf-8"))
        ]
        if seconds:
            declared[str(path.relative_to(REPO))] = seconds
    assert {rel: len(s) for rel, s in declared.items()} == _BUSY_TIMEOUT_DECLARATIONS, (
        "the busy timeout is declared in:\n  "
        + "\n  ".join(f"{rel}, {len(s)}x" for rel, s in sorted(declared.items()))
        + "\n\nEach declaration is its own value with its own prose, so add it to "
        "_BUSY_TIMEOUT_DECLARATIONS and say in _BUSY_TIMEOUT_PROSE which passages quote it."
    )

    unknown = {src for cols in _BUSY_TIMEOUT_PROSE.values() for src in cols} - declared.keys()
    assert not unknown, f"_BUSY_TIMEOUT_PROSE quotes declarations that do not exist: {unknown}"
    expected = {
        rel: sorted(declared[src][0] for src, count in cols.items() for _ in range(count))
        for rel, cols in _BUSY_TIMEOUT_PROSE.items()
    }

    quoted: dict[str, list[str]] = {}
    for path in _busy_timeout_files():
        figures = [
            before or after
            for before, after in _BUSY_TIMEOUT_QUOTED.findall(_busy_timeout_prose(path))
        ]
        if figures:
            quoted[str(path.relative_to(REPO))] = figures

    offenders = [
        f"{rel} quotes {', '.join(f + 's' for f in sorted(figures))}, expected "
        + ", ".join(
            f"{declared[src][0]}s x{count} from {src}"
            for src, count in sorted(_BUSY_TIMEOUT_PROSE[rel].items())
        )
        for rel, figures in sorted(quoted.items())
        if rel in _BUSY_TIMEOUT_PROSE and sorted(figures) != expected[rel]
    ]
    offenders += [
        f"{rel} no longer quotes it" for rel in sorted(_BUSY_TIMEOUT_PROSE.keys() - quoted.keys())
    ]
    offenders += [
        f"{rel} is a new copy" for rel in sorted(quoted.keys() - _BUSY_TIMEOUT_PROSE.keys())
    ]
    assert not offenders, (
        f"{len(declared)} modules declare a busy timeout ("
        + ", ".join(f"{rel} at {s[0]}s" for rel, s in sorted(declared.items()))
        + ") and these files restate one of them in prose:\n  "
        + "\n  ".join(offenders)
        + "\n\nCorrect every one in the same change, or give the new file its own row in "
        "_BUSY_TIMEOUT_PROSE naming which declaration it quotes. "
        "src/reaper/services/executor.py's copy is the reason the journal write takes two "
        "attempts with no sleep between them."
    )


# Reaper-owned identifiers and prose are American English. The allowlist covers names owned
# by someone else and spelled British at the source, which keep their real spelling.
_BRITISH = re.compile(
    r"\b(colour|behaviour|judgement|grey|licence|centre|normalise|serialise|recognise"
    r"|cancelled|labelled|artefact|defence|honour|analyse|catalogue)\w*",
    re.IGNORECASE,
)
_SANCTIONED = re.compile(r"CancelledError|\.cancelled\(\)|aria-label(?:ledby)?")


def test_american_english_everywhere() -> None:
    """Identifiers, comments, and operator copy are American English.

    The exceptions are names owned by someone else: ``asyncio.CancelledError`` and the ARIA
    ``aria-labelledby`` attribute keep their real spelling.
    """
    offenders: list[str] = []
    for path in _code_and_live_docs():
        # This file spells every banned word once, in the pattern above.
        if path.resolve() == SELF:
            continue
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            stripped = _SANCTIONED.sub("", line)
            for match in _BRITISH.finditer(stripped):
                offenders.append(f"{path.relative_to(REPO)}:{lineno} -> {match.group(0)}")
    assert not offenders, "use the American spelling:\n" + "\n".join(offenders)


def test_dynamic_favicon_link_is_declared_last() -> None:
    """Rule 69: the icon the app rewrites at runtime is the last icon link in index.html.

    When two icons are equally appropriate the HTML spec lets the browser take the last one,
    so a static fallback declared after ``#favicon`` pins the stale default forever.
    """
    html = (REPO / "frontend" / "index.html").read_text(encoding="utf-8")
    icon_links = [
        m.group(0) for m in re.finditer(r"<link[^>]*rel=\"[^\"]*icon[^\"]*\"[^>]*>", html)
    ]
    assert icon_links, "expected at least one icon link in index.html"
    assert 'id="favicon"' in icon_links[-1], (
        "the runtime-rewritten #favicon link must be declared last; found:\n"
        + "\n".join(icon_links)
    )


def test_dev_proxy_target_follows_the_api_port() -> None:
    """``REAPER_PORT`` moves the dev API, so it has to move the proxy sitting in front of it.

    ``scripts/dev-local.sh`` advertises ``REAPER_PORT`` as the way to run a SECOND instance --
    a parallel agent session, a PR test-drive -- and passes it to uvicorn. While
    ``frontend/vite.config.ts`` hardcoded its ``/api`` target, the API came up correctly on the
    new port and every call through Vite answered 502, so the UI was a dead shell that read as a
    crashed backend. ``REAPER_WEB_PORT`` did work, which is what made the pair read as supported
    with only half of it wired.

    Both halves are pinned here because either alone is silently useless: a config reading a
    variable nothing exports, and a script exporting a variable nothing reads, each look correct
    on their own line. Rule 144 -- this is one fact stated in two files, so neither may move
    without the other.
    """
    config = (REPO / "frontend" / "vite.config.ts").read_text(encoding="utf-8")
    assert "process.env.REAPER_PORT" in config, (
        "frontend/vite.config.ts must take its /api proxy target from REAPER_PORT, "
        "which scripts/dev-local.sh passes to uvicorn and to this process"
    )
    assert re.search(r"target:\s*`http://127\.0\.0\.1:\$\{", config), (
        "the /api proxy target must interpolate the port rather than hardcode one; "
        "a literal here defeats REAPER_PORT in scripts/dev-local.sh"
    )

    script = (REPO / "scripts" / "dev-local.sh").read_text(encoding="utf-8")
    lines = script.splitlines()
    launches = [i for i, line in enumerate(lines) if "npm --prefix frontend run dev" in line]
    assert launches, "expected scripts/dev-local.sh to launch the Vite dev server"
    for i in launches:
        window = "\n".join(lines[max(0, i - 3) : i + 1])
        assert "REAPER_PORT=" in window, (
            f"scripts/dev-local.sh:{i + 1} starts Vite without passing REAPER_PORT, so "
            "frontend/vite.config.ts cannot point the /api proxy at the API this script "
            "just started on that port"
        )


# Spellings that turn Vite's port strictness back off, written down before shipping the matcher
# (rule 147): the config field ``strictPort: false``, and the two CLI forms a launcher can pass,
# ``--strictPort false`` and ``--no-strictPort``. It reads the value, so quoting and spacing do
# not matter, and it cannot swallow ``--strictPort`` alone, which is the form that turns it ON.
_STRICT_PORT_OFF = re.compile(r"(?:--no-strictPort\b|strictPort[\"'\s:=]+false\b)")


def test_the_vite_dev_server_refuses_a_taken_port() -> None:
    """A dev server that cannot have the port it was told to use must say so, not pick another.

    Vite's ``strictPort`` defaults to false, so beside a running instance it slides to the next
    free port and says so only in its own log. A launcher that also omits ``REAPER_PORT`` then
    proxies ``/api`` to the FIRST instance's API, and the result is a UI serving this tree's
    code over someone else's backend with every request answering 200 -- an agent verifies a
    change end to end against a build that does not contain it (#239). The quiet inverse of the
    502 in ``test_dev_proxy_target_follows_the_api_port`` above, and the more dangerous one,
    because that one at least fails visibly.

    **Not a per-launch walk, unlike ``--no-proxy-headers``.** That flag lives on each uvicorn
    command line, so nothing but a walk can prove every launch carries it. This one lives in
    ``frontend/vite.config.ts``, which every launcher goes through -- ``.claude/launch.json``,
    the verify skill's manual boot, ``README.md``, ``scripts/dev-local.sh`` -- so the config is
    the whole population, and what a launcher can still do is override it back. Both halves are
    checked here; the ban runs over the same ``_repo_text_files`` walk the other gates use, so a
    launcher added anywhere in this checkout is inside it from the moment it is written.
    """
    config = (REPO / "frontend" / "vite.config.ts").read_text(encoding="utf-8")
    assert re.search(r"strictPort:\s*true", config), (
        "frontend/vite.config.ts must set `strictPort: true` on `server`, or a second dev "
        "server silently binds another port and proxies /api to the first instance's API"
    )

    overrides = [
        f"{path.relative_to(REPO)}:{lineno} -> {line.strip()}"
        for path, text in _repo_text_files()
        for lineno, line in enumerate(text.splitlines(), 1)
        if _STRICT_PORT_OFF.search(line)
    ]
    assert not overrides, (
        "a launcher turns Vite's port strictness back off, which re-opens #239 there even "
        "though frontend/vite.config.ts sets it:\n" + "\n".join(overrides)
    )


_PRESETS_TS = FRONTEND_SRC / "components" / "policyPresets.ts"
#: The ``Pick<ProfileSettings, ...>`` union naming the fields a preset writes. Read as the whole
#: declaration up to its ``>;`` and picked apart inside, rather than anchored on a delimiter one
#: spelling happens to put there: the union is comment-interleaved, so a per-line matcher would
#: be reading prose (rule 147). ``|`` is the only separator TypeScript allows here.
_PRESET_CAPS_TYPE = re.compile(r"PresetCaps\s*=\s*Pick<\s*ProfileSettings\s*,(.*?)>;", re.DOTALL)
_TS_LITERAL = re.compile(r'"(\w+)"')
#: ``caps: { ... }`` holds no nested braces, so the character class is the whole parser and a
#: nested one would fail loudly here rather than silently truncate the block (rule 147).
_PRESET_CAPS = re.compile(r"caps:\s*\{([^{}]*)\}")
_CAPS_FIELD = re.compile(r"^\s*(\w+):\s*([\w_]+),")
#: Cautious, Balanced, Aggressive. Pinned because a flag-shaped assertion cannot tell a preset
#: that validates from one this parser stopped collecting -- both read green (rule 145).
_EXPECTED_PRESETS = 3


def test_a_caps_preset_writes_every_field_its_validator_reads() -> None:
    """A starting point may not build a combination the operator is refused for choosing.

    Each preset sets ``caps_enabled: true``, and that switch is what activates
    ``policy._run_cap_within_rolling_cap`` -- it early-returns while the caps are off. The
    preset then MERGES its fields into the stored pace, so every cap it does not name keeps the
    operator's own value, and a hand-maintained subset can build a combination out of one they
    were allowed to store. ``max_unmeasured_per_run`` was the omission: the allowance accepts
    up to 25, Cautious sets 5 items per run, and an operator who had raised it clicked a button
    whose help text calls it a starting point and got a 422 (#256).

    **The omission is the defect, not the values**, which is what the first draft of this test
    got wrong: deleting a field from a preset leaves ``ProfileSettings`` supplying its default,
    so validating each preset alone passes on exactly the mutation it exists to catch. The
    field list is what has to be complete. So it is derived from the validator's own source
    (rule 144: generate the copy, or the ungenerated one drifts toward reassuring), and the
    values are then run through the real validator on top (rule 3/22) so a preset cannot also
    carry a combination that is illegal on its face.
    """
    import inspect

    from reaper.engine.policy import ProfileSettings

    validator = inspect.getsource(ProfileSettings._run_cap_within_rolling_cap)  # type: ignore[arg-type]
    reads = {
        name
        for name in re.findall(r"self\.(\w+)", validator)
        if name in ProfileSettings.model_fields
    }
    assert reads, "parsed no fields out of _run_cap_within_rolling_cap -- the matcher is stale"

    text = _PRESETS_TS.read_text(encoding="utf-8")
    declaration = _PRESET_CAPS_TYPE.search(text)
    assert declaration, f"no `PresetCaps = Pick<ProfileSettings, ...>` in {_PRESETS_TS.name}"
    written = set(_TS_LITERAL.findall(declaration.group(1)))

    missing = sorted(reads - written)
    assert not missing, (
        "ProfileSettings._run_cap_within_rolling_cap reads fields no preset writes, so "
        f"applying a preset can trip it on a stored value the operator was allowed to save: "
        f"{', '.join(missing)}.\nAdd each to PresetCaps in "
        f"{_PRESETS_TS.relative_to(REPO)} and give every preset a value."
    )

    blocks = _PRESET_CAPS.findall(text)
    assert len(blocks) == _EXPECTED_PRESETS, (
        f"expected {_EXPECTED_PRESETS} preset caps blocks in "
        f"{_PRESETS_TS.relative_to(REPO)}, found {len(blocks)}. If a preset was added, bump "
        "the number; if not, one stopped matching and is no longer being validated."
    )
    for block in blocks:
        caps: dict[str, object] = {}
        for line in block.splitlines():
            field = _CAPS_FIELD.match(line)
            if not field:
                continue  # a blank line or a `//` comment
            name, raw = field.group(1), field.group(2)
            caps[name] = raw == "true" if raw in ("true", "false") else int(raw.replace("_", ""))
        assert caps, f"parsed no fields out of a caps block in {_PRESETS_TS.relative_to(REPO)}"
        # A ValidationError here is the defect, stated by the server in its own words.
        ProfileSettings(**caps)


# ``pkill``/``killall`` select processes by PATTERN, which is machine-wide: nothing in the
# pattern distinguishes this dev instance from a parallel one. ``pgrep`` only counts when the
# same line goes on to kill (``pgrep -f x | xargs kill``); read-only, it is a status check and
# owes no scope.
#
# Spellings this accepts, written down before shipping the matcher (rule 147): ``pkill -f x``,
# ``pkill -9 -f x``, ``killall x``, a leading path (``/usr/bin/pkill``), a ``sudo`` prefix, and
# ``pgrep -f x | xargs kill``. It reads the command WORD, so flags, quoting and argument order
# do not matter. It does NOT read a kill assembled through a variable (``K=pkill; $K x``) or one
# routed through ``ps | awk | xargs kill``; neither is spelled anywhere in this tree, and both
# would need this matcher widened rather than the ban weakened.
# The lookbehind excludes word characters and ``-`` but NOT ``/``, so ``/usr/bin/pkill`` is
# still a kill while ``uv-pkill`` is not. Writing ``/`` into that class is what a first draft
# does, and it silently exempts every absolute invocation.
_PATTERN_KILL = re.compile(r"(?<![\w-])(?:pkill|killall)(?![\w-])")
_PGREP = re.compile(r"(?<![\w-])pgrep(?![\w-])")
_KILL_WORD = re.compile(r"(?<![\w-])kill(?![\w-])")
# A shell comment, so prose ABOUT a kill is not read as one. ``#`` must open a word, which is
# the shell's own rule. A ``#`` inside a quoted string that happens to follow a space is read as
# a comment here -- that can only hide a kill, never invent one, and the pinned count below is
# what notices a member going missing.
_SHELL_COMMENT = re.compile(r"(?:^|\s)#.*$")
# ``$API_PORT``, ``${WEB_PORT}``, ``$REAPER_PORT``.
_PORT_VAR = re.compile(r"\$\{?[A-Za-z_][A-Za-z0-9_]*PORT\b")


def _selects_processes_by_pattern(line: str) -> bool:
    """Whether this shell line kills processes chosen by pattern rather than by port or pid."""
    code = _SHELL_COMMENT.sub("", line)
    if _PATTERN_KILL.search(code):
        return True
    return bool(_PGREP.search(code) and _KILL_WORD.search(code))


#: ``docker-entrypoint.sh``, ``scripts/dev-local.sh``, ``scripts/log-instructions-loaded.sh``,
#: ``scripts/try-image.sh``.
_EXPECTED_SHELL_SCRIPTS = 4
#: Both in ``dev-local.sh``'s ``stop_all``: the TERM sweep, and the KILL for a survivor of it.
#: Pinned separately from the script count because the walk and the ban cover different
#: populations (rule 147): a script that drops out of the walk is absent from both, so a single
#: figure would agree with itself while disagreeing with the tree.
_EXPECTED_PATTERN_KILLS = 2


def test_a_dev_script_kills_only_its_own_ports() -> None:
    """A pattern is not a scope: a kill that selects by name must also name a port.

    ``scripts/dev-local.sh`` ended ``stop_all`` with ``pkill -f`` on a pattern that named the app
    and nothing else, so it matched every Reaper API on the machine. ``up`` calls ``stop_all``
    unconditionally, and the script advertises ``REAPER_PORT``/``REAPER_WEB_PORT`` as the
    supported way to run a second instance -- so following the documented workflow killed the
    instance it was meant to leave alone, before printing a line about what it was doing. Quiet
    in the worst way: only the API dies, the first instance's Vite keeps serving its own port, so
    the browser still loads the app and every request fails against a backend that is gone, which
    reads as an app bug rather than a dev-script one (#223).

    A port is the only thing in a dev script that tells this instance from a parallel one, so a
    pattern kill has to carry one. Killing by pid or by port is already scoped and is not
    collected here.
    """
    scripts = sorted(p for p, _ in _repo_text_files() if p.suffix == ".sh")
    assert len(scripts) == _EXPECTED_SHELL_SCRIPTS, (
        f"expected {_EXPECTED_SHELL_SCRIPTS} shell scripts, found {len(scripts)}:\n"
        + "\n".join(f"  {p.relative_to(REPO)}" for p in scripts)
        + "\n\nIf you ADDED one, bump the number. If you did not, one dropped out of the walk,\n"
        "and the ban below reads green on a script it can no longer see."
    )

    kills = [
        (path, lineno, line.strip())
        for path in scripts
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
        if _selects_processes_by_pattern(line)
    ]
    assert len(kills) == _EXPECTED_PATTERN_KILLS, (
        f"expected {_EXPECTED_PATTERN_KILLS} pattern-matching process kill(s), "
        f"found {len(kills)}:\n"
        + "\n".join(f"  {p.relative_to(REPO)}:{n} -> {t}" for p, n, t in kills)
        + "\n\nEvery one of them must name a port. Bump the number when you add one."
    )

    unscoped = [
        f"{path.relative_to(REPO)}:{lineno} -> {text}"
        for path, lineno, text in kills
        if not _PORT_VAR.search(text)
    ]
    assert not unscoped, (
        "a kill that selects processes by pattern must name the port it means, or it reaches\n"
        "every instance on the machine and takes a parallel session's API down with it:\n"
        + "\n".join(unscoped)
    )


def test_the_pattern_kill_matcher_reads_every_spelling_it_claims() -> None:
    """Rule 147: the ban above is bounded by what its regex can parse, so prove the parse.

    A matcher anchored on the literal ``pkill -f`` would read the one line this fixed and
    nothing else. The rejects are the near misses that must stay out: a kill already scoped by
    pid, and prose describing a kill. A false positive is a gate that gets deleted.
    """
    accepted = [
        'pkill -f "uvicorn reaper.main:create_app"',
        "pkill -9 -f uvicorn",
        "  killall uvicorn",
        "/usr/bin/pkill -f x",
        "sudo pkill -f x",
        "pgrep -f uvicorn | xargs kill",
    ]
    rejected = [
        "kill $pids 2>/dev/null || true",
        "  # an unscoped pkill would reach every instance on the machine",
        "pgrep -f uvicorn && log 'still running'",
        'log "stopping :$p ($pids)"',
    ]
    missed = [line for line in accepted if not _selects_processes_by_pattern(line)]
    assert not missed, "the matcher cannot read spellings the ban claims to cover:\n" + "\n".join(
        missed
    )
    false_positives = [line for line in rejected if _selects_processes_by_pattern(line)]
    assert not false_positives, (
        "the matcher collects lines that do not select processes by pattern:\n"
        + "\n".join(false_positives)
    )


# A FILE inside a log directory -- ``$LOG_DIR/``, ``${LOG_DIR}/``, ``$API_LOGDIR/``. The trailing
# separator is what makes this a file rather than the directory: ``mkdir -p "$LOG_DIR"`` and
# ``ls "$LOG_DIR"`` act on the directory, which is per-tree on purpose, and must not be collected.
# The leading class accepts ZERO characters before ``LOG``, which a first draft does not: written
# as ``[A-Za-z_][A-Za-z0-9_]*LOG_?DIR`` it requires a prefix and reads every spelling EXCEPT the
# bare ``$LOG_DIR`` this tree actually uses, i.e. it collects nothing and passes (rule 147).
_LOG_DIR_FILE = re.compile(r"\$\{?[A-Za-z0-9_]*LOG_?DIR\}?/")

#: ``API_LOG`` and ``WEB_LOG`` in ``scripts/dev-local.sh``. Pinned because the ban below cannot
#: tell a path that carries its port from one the walk no longer reaches -- both read green
#: (rule 147). The population is the log-path lines, not the scripts: dev-local.sh dropping out
#: of the walk takes this count to 0, so one figure covers both losses.
_EXPECTED_INSTANCE_LOG_PATHS = 2


def test_a_dev_script_writes_only_its_own_logs() -> None:
    """A per-instance file names the instance: a log path must carry the port that owns it.

    ``scripts/dev-local.sh`` keyed ``LOG_DIR`` to the TREE while keying every other per-instance
    resource to the port, so two instances started from one checkout shared one pair of log
    files. ``nohup ... > "$API_LOG"`` truncates on open, so the second instance's start emptied
    the log the first was still writing to, and the first one's uvicorn held its file offset
    across that truncation and kept appending at a stale one. ``logs`` then tailed whichever
    instance opened the file last, and nothing in either instance's output said so, which is the
    part that costs a debugging session: the output is a real instance's, just not the one the
    reader meant (#235).

    Same class as the unscoped kill above (#223) at a different resource -- a dev script acting
    outside its own scope -- and the same fix, since the port is the only thing that tells this
    instance from a parallel one.
    """
    scripts = sorted(p for p, _ in _repo_text_files() if p.suffix == ".sh")
    paths = [
        (path, lineno, line.strip())
        for path in scripts
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
        if _LOG_DIR_FILE.search(_SHELL_COMMENT.sub("", line))
    ]
    assert len(paths) == _EXPECTED_INSTANCE_LOG_PATHS, (
        f"expected {_EXPECTED_INSTANCE_LOG_PATHS} log path(s) inside a log dir, "
        f"found {len(paths)}:\n"
        + "\n".join(f"  {p.relative_to(REPO)}:{n} -> {t}" for p, n, t in paths)
        + "\n\nEvery one must name a port. Bump the number when you add one; if you did not add\n"
        "one, a script dropped out of the walk and the ban below no longer reads it."
    )

    unkeyed = [
        f"{path.relative_to(REPO)}:{lineno} -> {text}"
        for path, lineno, text in paths
        if not _PORT_VAR.search(text)
    ]
    assert not unkeyed, (
        "a file under the dev log dir must carry the port of the instance that writes it, or a\n"
        "second instance truncates the first one's log and every later read is of the wrong\n"
        "instance with nothing saying so:\n" + "\n".join(unkeyed)
    )


def test_the_log_path_matcher_reads_every_spelling_it_claims() -> None:
    """Rule 147: the ban above is bounded by what its regex can parse, so prove the parse.

    The rejects are the near misses that must stay out. Two are the log DIRECTORY, which is
    per-tree by design and would be a false positive; a gate that fires on ``mkdir`` is a gate
    someone deletes. The third is prose about a log path, which is not one.
    """
    accepted = [
        'API_LOG="$LOG_DIR/api-$API_PORT.log"',
        'WEB_LOG="${LOG_DIR}/web-${WEB_PORT}.log"',
        '  nohup cmd > "$LOG_DIR/api.log" 2>&1 &',
        "tail -f $LOG_DIR/*.log",
        'X="$API_LOGDIR/api.log"',
    ]
    rejected = [
        'mkdir -p "$LOG_DIR"',
        '      have="$(ls "$LOG_DIR" 2>/dev/null)"',
        "  # .dev-logs holds one file per port, so $LOG_DIR/api.log would collide",
        'log "data dir: $DATA_DIR"',
    ]
    missed = [line for line in accepted if not _LOG_DIR_FILE.search(_SHELL_COMMENT.sub("", line))]
    assert not missed, "the matcher cannot read spellings the ban claims to cover:\n" + "\n".join(
        missed
    )
    false_positives = [
        line for line in rejected if _LOG_DIR_FILE.search(_SHELL_COMMENT.sub("", line))
    ]
    assert not false_positives, (
        "the matcher collects lines that do not name a file inside the log dir:\n"
        + "\n".join(false_positives)
    )


# Cached for the reason stated above ``_source_files_to_scan``, and this is the walk that costs.
@lru_cache
def _repo_text_files() -> list[tuple[Path, str]]:
    """Every readable text file git considers part of THIS checkout, with its contents.

    The population comes from git, not from ``rglob``, which honors no ignore file. Gitignored
    directories sit inside the repo root and every gate below reads whatever this walk hands
    it. Three of them are the ones that bit. ``.claude/worktrees/`` holds agent worktrees,
    entire repo copies, so a raw walk judges other branches' files as if they were ours, and a
    worktree's ``.git`` is a *file*, so skipping that name does not stop the descent.
    ``.claude/review-findings/`` is session handoff scratch. ``.dev-logs/`` is whatever the dev
    servers last printed, and a stack trace echoing a uvicorn command line is collected there
    as a LAUNCH SITE. Each one fails ``uv run pytest`` in a checkout that has it on disk while
    CI, which has none, stays green. A gate nobody can turn green from their own branch is a
    gate that gets deleted.

    This replaced a hand-kept skip set, a mirror of ``.gitignore`` that needed an edit every
    time the ignore file grew (rule 103). It was four entries behind when it was deleted:
    ``.claude/review-findings/``, ``.hypothesis/``, ``.pytest_cache/`` and
    ``mutation-report-*.json``. Two of those four are created by running this very suite,
    which is why the set could not stay current by anyone's diligence.
    ``--others --exclude-standard`` keeps a file created but not yet staged, which is the
    state a gate is most useful in.

    ``cwd=REPO`` carries the weight, and it inherits a trap worth recording. The skip set was
    matched on the repo-RELATIVE path, because matching ``path.parts`` of the absolute one
    matches the worktree the suite is *running in* and skips every file in the tree.
    ``git ls-files`` prints paths relative to the process's own directory, so running it
    anywhere but ``REPO`` makes every ``REPO / name`` join name a file that does not exist,
    ``is_file()`` drops all of them, and the walk comes back empty and green. Same failure,
    one layer down.
    """
    # stderr is left inherited, so git's own "not a git repository" reaches whoever ran this.
    listing = subprocess.run(
        # S607: git is resolved off PATH, the same trust as the runner that started this.
        ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],  # noqa: S607
        cwd=REPO,
        stdout=subprocess.PIPE,
        check=True,
        # ``-z`` was chosen so an odd filename survives the split; a strict decode would give
        # that back, raising out of the walk and taking every gate built on it with it.
    ).stdout.decode(errors="surrogateescape")
    found: list[tuple[Path, str]] = []
    for name in listing.split("\0"):
        if not name:
            continue
        path = REPO / name
        # ``--cached`` also names a file deleted from the working tree but still in the index.
        if not path.is_file() or path.resolve() == SELF:
            continue
        try:
            found.append((path, path.read_text(encoding="utf-8")))
        except (UnicodeDecodeError, OSError):
            continue
    return found


def test_the_repo_walk_never_reads_a_gitignored_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Rule 145: the count is pinned over a tree whose membership is controlled.

    Counting the real checkout would pin a number that moves with every file added, so the
    population is five files here, two of them ignored, reconciled by hand. The ignored pair
    is shaped like what broke this: a directory of session scratch, and a log holding a line
    a gate would read as a launch site. Neither is reachable from a branch, so a walk that
    collects them is red in every checkout that has them and green in CI forever.

    **Each argument owns one member of the expected set**, or one can be dropped and this
    still reads green. ``kept.md`` is staged, so only ``--cached`` reaches it; ``.gitignore``
    is untracked and not ignored, so only ``--others`` does; dropping ``--exclude-standard``
    adds the ignored pair back; and the non-UTF8 name is what ``-z`` and the walk's
    ``surrogateescape`` decode are for, a strict decode raising out of the walk instead.

    **The git environment is scrubbed three ways, and each closes a hole that was measured
    rather than imagined** (rule 119). ``GIT_DIR`` and ``GIT_WORK_TREE`` beat ``cwd=``, so an
    inherited one makes ``git init`` a no-op against someone else's repository, ``git add``
    write into that repository's index, and this test pass having proved nothing. Clearing
    every ``GIT_*`` name is what stops that. ``GIT_CONFIG_GLOBAL`` and ``GIT_CONFIG_SYSTEM``
    then keep a developer's ``init.templateDir`` from seeding ``.git/info/exclude``. And
    ``core.excludesFile`` is pinned inside the repo, because its DEFAULT is
    ``$XDG_CONFIG_HOME/git/ignore``, which ``--exclude-standard`` reads with no config file
    involved and which a repo-level setting is the only thing that overrides.
    """
    for name in [key for key in os.environ if key.startswith("GIT_")]:
        monkeypatch.delenv(name)
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "absent-global"))
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", str(tmp_path / "absent-system"))
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)  # noqa: S607
    subprocess.run(  # noqa: S603 - the one non-literal argument is os.devnull
        ["git", "config", "core.excludesFile", os.devnull],  # noqa: S607
        cwd=tmp_path,
        check=True,
    )
    (tmp_path / ".gitignore").write_text("scratch/\n*.log\n", encoding="utf-8")
    (tmp_path / "kept.md").write_text("source\n", encoding="utf-8")
    (tmp_path / "scratch").mkdir()
    (tmp_path / "scratch" / "handoff.md").write_text("session scratch\n", encoding="utf-8")
    (tmp_path / "noisy.log").write_text(
        'uvicorn reaper.main:create_app --factory --port "8420"\n', encoding="utf-8"
    )
    odd = os.fsdecode(b"caf\xe9.md")
    (tmp_path / odd).write_text("a name git prints as raw bytes\n", encoding="utf-8")
    subprocess.run(["git", "add", "kept.md"], cwd=tmp_path, check=True)  # noqa: S607

    global REPO
    real, REPO = REPO, tmp_path
    _repo_text_files.cache_clear()
    try:
        found = {path.relative_to(tmp_path).as_posix() for path, _ in _repo_text_files()}
    finally:
        REPO = real
        _repo_text_files.cache_clear()

    assert found == {".gitignore", "kept.md", odd}, found


# Every real invocation of the app carries ``--factory``, because ``create_app`` IS a factory
# and uvicorn cannot boot it otherwise. That is what separates an invocation from a mention:
# dev-local.sh's ``pkill -f "uvicorn reaper.main:create_app"`` names the same string and is not
# a launch. Matching on the pair means there is no list of files here to remember to update.
#
# The separator class is load-bearing, and cost a launch to learn. ``\s+`` alone reads a shell
# command line and nothing else: an argv ARRAY spells the two tokens ``"uvicorn",
# "reaper.main:create_app"``, where what sits between them is a quote and a comma. So
# ``.claude/launch.json`` -- the launch CLAUDE.md tells interactive sessions to use, and the one
# that was still missing ``--no-proxy-headers`` -- was invisible to this test while the comment
# here claimed every invocation in the tree was covered. Accepting quotes and commas is what
# makes that claim true rather than reassuring. It cannot swallow the ``pkill`` line, which
# carries no ``--factory``.
_UVICORN_LAUNCH = re.compile(r"uvicorn[\"',\s]+reaper\.main:create_app\b")

#: The shipped ``CMD``, ``scripts/dev-local.sh``, ``CONTRIBUTING.md``, ``.claude/launch.json``
#: and ``.claude/skills/verify/SKILL.md``. It said ``README.md`` for the third, which carries no
#: uvicorn line at all -- the count was right and the file named was not, which is the drift a
#: pinned count cannot see (#389). Pinned because "every launch carries the flag" is only
#: worth as much as the walk that finds them: the flag assertion below cannot distinguish a
#: launch that complies from one this matcher no longer sees, and both read as green (rule 145).
_EXPECTED_LAUNCHES = 5


def _uvicorn_launches() -> list[tuple[Path, int, str]]:
    """Every line in one of THIS checkout's own text files that boots the app under uvicorn.

    The walk is ``_repo_text_files``, which is scoped to this checkout for reasons worth
    reading before changing either caller.
    """
    return [
        (path, lineno, line.strip())
        for path, text in _repo_text_files()
        for lineno, line in enumerate(text.splitlines(), 1)
        if _UVICORN_LAUNCH.search(line) and "--factory" in line
    ]


def test_every_uvicorn_launch_disables_proxy_headers() -> None:
    """Rule 101: peer trust is decided by ``reaper.auth.proxy``, so the server must abstain.

    uvicorn defaults to ``proxy_headers=True`` with ``forwarded_allow_ips="127.0.0.1"``, and
    its ``ProxyHeadersMiddleware`` rewrites ``scope["client"]`` and ``scope["scheme"]`` from
    ``X-Forwarded-For``/``-Proto`` before any application code exists. On an install whose
    caller really is loopback -- host networking, a same-host proxy published to
    ``127.0.0.1:8420``, another container sharing the netns, a dev server -- a caller could
    then rotate a fake address past the per-IP sign-in lockout, and hand itself a
    ``Secure``/``__Host-`` cookie its own browser drops on a plain-HTTP install. Both with
    reverse-proxy trust switched OFF, from a default the operator never set.

    So this is not style: dropping the flag from any launch re-opens it there. The check is on
    the invocation rather than on one named file, because the shipped ``CMD`` and the dev
    script are twins (rule 72) and a third launch would inherit the same defect silently.
    """
    launches = _uvicorn_launches()
    assert len(launches) == _EXPECTED_LAUNCHES, (
        f"expected {_EXPECTED_LAUNCHES} uvicorn launches, found {len(launches)}:\n"
        + "\n".join(f"  {p.relative_to(REPO)}:{n}" for p, n, _ in launches)
        + "\n\nIf you ADDED a launch, give it --no-proxy-headers and bump the number. If you\n"
        "did not, one dropped out of coverage -- most likely reworded or wrapped onto a\n"
        "second line, since the match is per-line. A count is the only thing that can see\n"
        "that: the assertion below passes happily on a launch it can no longer find."
    )
    missing = [
        f"{path.relative_to(REPO)}:{lineno} -> {line}"
        for path, lineno, line in launches
        if "--no-proxy-headers" not in line
    ]
    assert not missing, (
        "every uvicorn launch must pass --no-proxy-headers, or the forwarded headers it\n"
        "rewrites decide peer trust one layer above reaper.auth.proxy:\n" + "\n".join(missing)
    )


#: Where each uvicorn launch gets its preflight. The Dockerfile's ``CMD`` is exec'd by the
#: entrypoint, which preflights; every other launch preflights in its own file.
_PREFLIGHT_SOURCE = {
    "Dockerfile": "docker-entrypoint.sh",
    "scripts/dev-local.sh": "scripts/dev-local.sh",
    "CONTRIBUTING.md": "CONTRIBUTING.md",
    ".claude/skills/verify/SKILL.md": ".claude/skills/verify/SKILL.md",
}

#: The one launch that still does not, pinned so the gap cannot grow back quietly. It is a
#: launcher config that spawns a single executable from ``runtimeArgs``, so adding a step means
#: changing its shape rather than its arguments, and that is the repository owner's call and not
#: this test's (#389).
_PREFLIGHT_GAP = {".claude/launch.json"}


def test_every_uvicorn_launch_runs_preflight() -> None:
    """Rule 127: ``preflight``'s docstring says EVERY way of starting Reaper runs it.

    Nothing enforced that, and three developer recipes did not -- ``CONTRIBUTING.md``,
    ``.claude/skills/verify/SKILL.md`` and ``.claude/launch.json``. It is the shape that goes
    wrong quietly, because preflight is what applies a staged restore: a launch that skips it
    does not fail, it just never finishes the operator's restore, and the banner asks for a
    restart that cannot complete however many times it is given one (#381, #389).

    Every shipped path was and is fine. This binds the developer ones, and any launch added
    later by an author who never read the docstring, which is what prose cannot do.

    The membership assertion comes first for rule 145's reason: a "names preflight" check
    cannot tell a launch that complies from one this walk no longer sees, and both read green.
    """
    texts = {str(path.relative_to(REPO)): text for path, text in _repo_text_files()}
    found = {str(path.relative_to(REPO)) for path, _, _ in _uvicorn_launches()}
    assert found == set(_PREFLIGHT_SOURCE) | _PREFLIGHT_GAP, (
        "the set of files launching uvicorn moved:\n"
        f"  found:    {sorted(found)}\n"
        f"  expected: {sorted(set(_PREFLIGHT_SOURCE) | _PREFLIGHT_GAP)}\n\n"
        "If you ADDED a launch, run `python -m reaper.preflight` before it and name its\n"
        "source in _PREFLIGHT_SOURCE. A launch that skips preflight silently never applies\n"
        "a staged restore."
    )
    missing = [
        f"{launch} -> preflight expected in {source}"
        for launch, source in _PREFLIGHT_SOURCE.items()
        if "reaper.preflight" not in texts.get(source, "")
    ]
    assert not missing, (
        "every uvicorn launch runs `python -m reaper.preflight` first, before migrations,\n"
        "or a restore staged in the UI is never applied:\n" + "\n".join(missing)
    )


# Where the operator is told what REAPER_PORT does. The Dockerfile's CMD is the declaration;
# these are the prose copies of it, and rule 144 is that deriving one does not make the rest
# safe. docker-compose.yml carried the wrong one for a while: it said REAPER_PORT "does not
# move" the container's port, beside a CMD that has always read it, so an operator following
# the compose file published on a port the app was not serving and read the healthcheck's
# failure as a broken image.
_PORT_PROSE = ("docker-compose.yml", ".env.example", "scripts/try-image.sh")

# Said of REAPER_PORT, each of these is false. Matched against the flattened text, so a
# sentence wrapped across two comment lines is still read as the one sentence it is.
_PORT_DENIALS = (
    "reaper_port does not move",
    "reaper_port is ignored",
    "reaper_port has no effect",
    "right side is fixed",
    "container always serves on 8420",
)

#: A comment leader at the start of a line: ``#``, ``//``, or a YAML/shell run of them.
_COMMENT_LEADER = re.compile(r"^\s*(?:#+|//)\s?")


def _flatten_prose(text: str) -> str:
    """One lowercase line, with per-line comment leaders removed.

    Every one of these copies lives in a comment, so a sentence long enough to matter wraps,
    and each continuation line carries its own ``#``. Collapsing whitespace alone leaves that
    marker sitting mid-sentence ("reaper_port does not # move it"), which a substring ban
    reads straight past -- the exact shape of the claim this exists to catch (rule 147).
    """
    stripped = [_COMMENT_LEADER.sub("", line) for line in text.splitlines()]
    return " ".join(" ".join(stripped).split()).lower()


def test_no_file_denies_that_reaper_port_moves_the_container_port() -> None:
    """Rule 144: the CMD reads REAPER_PORT, so no prose copy may say it does not.

    The image's ``CMD`` passes ``--port "${REAPER_PORT:-8420}"`` to uvicorn and its
    ``HEALTHCHECK`` reads the same variable, so setting it genuinely moves the port the
    container serves on. That fact is written out again in three places an operator
    actually reads, and each was written by someone looking at a different one.

    The direction of the failure is the point: a wrong denial here reads as reassurance
    ("you cannot break it, the right side is fixed"), which is why it survived review.
    """
    cmd = (REPO / "Dockerfile").read_text(encoding="utf-8")
    assert "REAPER_PORT:-8420" in cmd, (
        "the Dockerfile CMD no longer reads REAPER_PORT. If the port really is fixed now,\n"
        "this test is inverted: the prose in " + ", ".join(_PORT_PROSE) + " must be\n"
        "rewritten to say so, and this check deleted with it."
    )

    wrong: list[str] = []
    for name in _PORT_PROSE:
        path = REPO / name
        assert path.is_file(), f"{name} is gone; update _PORT_PROSE beside this test."
        flat = _flatten_prose(path.read_text(encoding="utf-8"))
        wrong += [f"  {name}: says {denial!r}" for denial in _PORT_DENIALS if denial in flat]

    assert not wrong, (
        "REAPER_PORT moves the port the container serves on -- the Dockerfile CMD passes it\n"
        "to uvicorn and the healthcheck reads it -- so this text tells the operator the\n"
        "opposite of what the image does:\n" + "\n".join(wrong)
    )


def test_the_port_denial_matcher_reads_every_spelling_it_claims() -> None:
    """Rule 147: the ban above is a substring match, so prove what it does and does not read.

    The accepts include the form the whole design rests on -- a sentence broken across two
    comment lines, which is how the wrong one was actually written and how a matcher
    anchored per-line would have missed it.

    The rejects matter more here than usual. Every one of them is text that states the
    behavior *correctly*, and a matcher that trips on those gets deleted the first time it
    cries wolf, taking the real check with it.
    """
    accepted = [
        "# REAPER_PORT does not move it",
        "# always serves on 8420, and REAPER_PORT does not\n      # move it (the CMD is fixed)",
        "the right side is fixed: 8420",
        "The container always serves on 8420.",
        "note: REAPER_PORT is ignored by the image",
        "REAPER_PORT has no effect on the published port",
    ]
    rejected = [
        # The corrected compose comment, and the shape of the other two prose copies.
        "The right side is the port inside the container, which is 8420 unless you set\n"
        "REAPER_PORT below.",
        "Bind address and port. Honored by the container entrypoint.",
        "The port inside the container follows REAPER_PORT when you pass one.",
        "REAPER_PORT moves the port the container serves on.",
        # Near misses: the words are present, the denial is not.
        "the container serves on 8420 by default",
        "REAPER_PORT does not need to be set for a normal install",
    ]

    def denies(text: str) -> bool:
        # The same normalizer the ban runs, never a copy of it (rule 119).
        flat = _flatten_prose(text)
        return any(denial in flat for denial in _PORT_DENIALS)

    missed = [text for text in accepted if not denies(text)]
    assert not missed, "the matcher must read these as denials, and does not:\n" + "\n".join(
        f"  {text!r}" for text in missed
    )

    tripped = [text for text in rejected if denies(text)]
    assert not tripped, (
        "the matcher must leave correct prose alone, and these are correct:\n"
        + "\n".join(f"  {text!r}" for text in tripped)
    )


# The Alembic baseline is frozen: testers run Reaper on real data, and every schema change
# is a new revision chained onto the head. Editing the baseline makes an existing database
# un-upgradable. If this hash must change, that is a conversation, not a commit.
_BASELINE = "alembic/versions/20260714_1840_baseline_schema.py"
_BASELINE_SHA256 = "2542354a4b62ed7bc410ecf644b133c0dc7385594c37fbad8196c5cd755201d7"


def test_alembic_baseline_is_frozen() -> None:
    """The frozen baseline revision is byte-for-byte unchanged."""
    path = REPO / _BASELINE
    assert path.is_file(), f"the frozen baseline is missing: {_BASELINE}"
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    assert digest == _BASELINE_SHA256, (
        f"the Alembic baseline {_BASELINE} was edited (sha256 {digest}).\n"
        "It is frozen: add a new revision chained onto the current head instead."
    )


# The Unraid Community Applications repository profile, as the submission scanner reads it:
# https://ca.unraid.net/submit/help/repository-info-xml. Root <CommunityApplications>, with the
# repository description as the text of a <Profile> child.
_CA_PROFILE = "ca_profile.xml"
_CA_PROFILE_ROOT = "CommunityApplications"


def test_the_unraid_profile_is_shaped_the_way_the_submission_scanner_reads_it() -> None:
    """A ``<Profile>`` root parses, so no XML check the repo runs can see this one.

    The file shipped with ``<Profile>`` as its root element and the description in a
    ``<Description>`` child. That is well-formed XML naming every field the spec names, and
    Community Applications rejected the submission for a missing ``<Profile>`` field, because
    the field it wants is a child of ``<CommunityApplications>``. Parsing is the whole gate
    here, and parsing passed.
    """
    root = ET.parse(REPO / _CA_PROFILE).getroot()  # noqa: S314 - a committed file, not input
    assert root.tag == _CA_PROFILE_ROOT, (
        f"{_CA_PROFILE} has <{root.tag}> as its root element; Community Applications reads "
        f"<{_CA_PROFILE_ROOT}> and reports anything else as a missing <Profile> field."
    )
    profile = root.find("Profile")
    assert profile is not None and (profile.text or "").strip(), (
        f"{_CA_PROFILE} needs a non-empty <Profile> child holding the repository description; "
        "the submission is blocked without one."
    )


# The Unraid container template offers its two channels as <Branch> entries, which Community
# Applications renders as a dropdown and substitutes into <Repository>. Two things about that
# arrangement break quietly, so both are pinned here.
_UNRAID_TEMPLATES = REPO / "contrib" / "unraid"
_RETENTION = REPO / ".github" / "workflows" / "registry-retention.yml"
# One entry, not one per channel: a second template file is the split this replaced coming back.
_UNRAID_TEMPLATE_COUNT = 1


def test_the_unraid_template_offers_every_channel_it_declares() -> None:
    """Every channel the picker offers is a ``<Branch>``, described in a field CA reads.

    Community Applications expands one install row per ``<Branch>``, showing its ``<Tag>``
    beside its ``<TagDescription>``, above a Default row that installs ``<Repository>`` as
    written. The only branch ``include/exec.php`` skips is one that spells ``<Tag>`` twice
    inside a single ``<Branch>``; a tag matching ``<Repository>``'s is expanded like any other,
    and 539 templates in the live app feed list theirs that way.

    Both halves of that were once believed backwards, and this test enforced the belief. The
    release channel lost its ``<Branch>`` and was described by an invented
    ``<DefaultTagDescription>``, a name no file in the Community Applications source reads: the
    Default row's text is hardcoded in ``include/helpers.php``, so the release channel shipped
    with no description at all. An invented element parses, installs, and renders nothing.

    So the repository's own tag carries a ``<Branch>`` like every other channel, that invented
    field stays gone, and the tags are read off the registry retention job rather than copied
    into a literal here: everything outside its protected set is swept after a week, so offering
    a tag from outside it hands the operator a channel whose image stops resolving (rule 25).
    """
    templates = sorted(_UNRAID_TEMPLATES.glob("*.xml"))
    assert len(templates) == _UNRAID_TEMPLATE_COUNT, (
        f"expected {_UNRAID_TEMPLATE_COUNT} Unraid template in contrib/unraid, found "
        f"{[p.name for p in templates]}. The channels are <Branch> entries in the one "
        "template, so a second file is a second store listing for the same app."
    )
    root = ET.parse(templates[0]).getroot()  # noqa: S314 - a committed file, not input
    repository = (root.findtext("Repository") or "").strip()
    assert repository, f"{templates[0].name} needs a <Repository>"

    branches = root.findall("Branch")
    assert branches, (
        f"{templates[0].name} declares no <Branch>, so the install page offers no channel "
        "choice. Either restore them or drop this guard with the feature."
    )
    tags = [(b.findtext("Tag") or "").strip() for b in branches]
    assert all(tags), f"{templates[0].name} has a <Branch> with an empty <Tag>: {tags}"
    assert len(set(tags)) == len(tags), f"{templates[0].name} declares a tag twice: {tags}"
    for branch, tag in zip(branches, tags, strict=True):
        assert (branch.findtext("TagDescription") or "").strip(), (
            f"the {tag} branch has no <TagDescription>, so its dropdown row is blank"
        )

    # CA splits the repository on its FIRST colon, so a registry port ("host:5000/org/app")
    # would be read as the tag and the image name mangled. ghcr.io carries no port; this pins
    # that, because the failure would otherwise surface as a broken pull.
    assert repository.count(":") == 1, (
        f"<Repository> is {repository}, which does not hold exactly one colon. Community "
        "Applications splits on the first one to separate image from tag, so a registry port "
        "here is read as the tag."
    )
    image, default_tag = repository.split(":")
    assert "/" not in default_tag, (
        f"the colon in <Repository> ({repository}) sits in the host or path, not before a tag"
    )
    assert default_tag in tags, (
        f"{default_tag!r}, the tag on <Repository> ({repository}), has no <Branch>. Community "
        "Applications expands it like any other branch, and without one the release channel's "
        "only row is CA's Default row, whose text is hardcoded in include/helpers.php and which "
        "no field in the template can describe."
    )
    assert root.find("DefaultTagDescription") is None, (
        f"{templates[0].name} declares <DefaultTagDescription>, which no file in the Community "
        "Applications source reads: the Default row's text is hardcoded in include/helpers.php. "
        f"Describe {default_tag!r} in its own <Branch>/<TagDescription> instead."
    )
    assert image, f"<Repository> ({repository}) has no image path before its tag"

    # Every channel the operator can reach: the default plus one per branch.
    offered = {default_tag, *tags}
    protected = _RETENTION.read_text(encoding="utf-8")
    match = re.search(r"protected_tags\s*=\s*\{([^}]*)\}", protected)
    assert match, (
        f"{_RETENTION.name} no longer spells its keep set as `protected_tags = {{...}}`; this "
        "guard reads that line to know which tags outlive a week."
    )
    kept = set(re.findall(r'"([^"]+)"', match.group(1)))
    assert offered <= kept, (
        f"the template offers {sorted(offered - kept)}, which {_RETENTION.name} does not "
        f"protect (it keeps {sorted(kept)}). That tag is deleted a week after it is pushed, so "
        "the channel breaks on the next pull that finds no cached layers."
    )


@pytest.mark.parametrize("path", INSTRUCTION_FILES, ids=lambda p: p.name)
def test_scoped_rule_files_declare_their_paths(path: Path) -> None:
    """Every file under ``.claude/rules/`` carries ``paths:`` frontmatter.

    A rule file with no ``paths`` loads unconditionally in every session, which is the cost
    the split exists to avoid. The root CLAUDE.md is the one file that loads always.
    """
    if path.name == "CLAUDE.md":
        pytest.skip("the root index loads unconditionally by design")
    text = path.read_text(encoding="utf-8")
    assert text.startswith("---\n"), f"{path.name} needs YAML frontmatter"
    frontmatter = text.split("---", 2)[1]
    assert "paths:" in frontmatter, f"{path.name} must scope itself with a paths: list"


# --- the pins, and the one thing that moves them -------------------------------------------
#
# Rule 15 pins the shipped artifact: digest-pinned base images, sha-pinned action shas, an
# install from the committed lockfiles. `.github/dependabot.yml` is the only thing in the
# repository that moves any of them, and it fails silently in both directions. An ecosystem
# nobody added simply never raises a pull request, which looks exactly like a week with no
# updates; and a pull request it does raise can die on a required check whose reason lives in
# a different file nobody was editing. These three check both halves.

DEPENDABOT = REPO / ".github" / "dependabot.yml"
PR_TITLE_WORKFLOW = REPO / ".github" / "workflows" / "pr-validation.yml"

#: The manifest filenames Dependabot can watch, and the ecosystem each one demands. The
#: workflows directory is handled separately in the walk below, because `github-actions` is
#: declared at the repository root rather than at `.github/workflows`, so it is the one
#: ecosystem whose directory is not the manifest's own parent.
_ECOSYSTEM_BY_MANIFEST = {
    "uv.lock": "uv",
    "package-lock.json": "npm",
    "Dockerfile": "docker",
    "docker-compose.yml": "docker-compose",
}

#: Left unwatched on purpose, so that an unwatched manifest is either named here or a failure.
#: `docker-compose.yml` names one image, Reaper's own, on the moving `:dev` tag an operator is
#: meant to follow; pinning that to a digest would freeze their install on whatever happened to
#: be current the day the pull request landed.
_UNWATCHED_MANIFESTS = {"docker-compose.yml"}

#: Reconciled by hand against the tree: the two lockfiles, the Dockerfile, the compose file and
#: the workflows directory. Pinned because the two-way check below cannot tell a manifest that
#: complies from one the walk stopped seeing (rule 145) -- renaming `Dockerfile` to
#: `Containerfile` empties the discovered side and orphans nothing, so every other assertion
#: here passes while the image base is no longer watched by anything.
_EXPECTED_MANIFEST_KINDS = {
    ("github-actions", "/"),
    ("docker", "/"),
    ("docker-compose", "/"),
    ("uv", "/"),
    ("npm", "/frontend"),
    ("npm", "/website"),
}


def _dependabot_updates() -> list[dict[str, Any]]:
    """Every ``updates:`` entry in ``.github/dependabot.yml``.

    A parsed list, not a text scan, so unlike the walks elsewhere in this file its population
    cannot quietly shrink behind a matcher that stopped reading a spelling. What *can* shrink
    is the file itself, which is what the caller below reconciles against the tree.
    """
    assert DEPENDABOT.is_file(), (
        "nothing moves the pins: .github/dependabot.yml is missing, so the digests rule 15\n"
        "requires stay on whatever layer they were pinned to."
    )
    config = yaml.safe_load(DEPENDABOT.read_text(encoding="utf-8"))
    updates = config.get("updates") if isinstance(config, dict) else None
    assert isinstance(updates, list) and updates, (
        ".github/dependabot.yml parses but declares no updates:, which updates nothing."
    )
    return updates


def _manifest_kinds_in_tree() -> dict[str, tuple[str, str]]:
    """Each ``(ecosystem, directory)`` this checkout demands, keyed by the file demanding it.

    The walk is ``_repo_text_files``, which is scoped to this checkout and skips
    ``node_modules``; a vendored lockfile is not a manifest this repository maintains.
    """
    found: dict[str, tuple[str, str]] = {}
    for path, _ in _repo_text_files():
        relative = path.relative_to(REPO)
        if relative.parts[:2] == (".github", "workflows") and path.suffix in {".yml", ".yaml"}:
            found[relative.as_posix()] = ("github-actions", "/")
            continue
        ecosystem = _ECOSYSTEM_BY_MANIFEST.get(path.name)
        if ecosystem is None:
            continue
        parent = relative.parent.as_posix()
        found[relative.as_posix()] = (ecosystem, "/" if parent == "." else f"/{parent}")
    return found


def test_every_dependency_manifest_is_watched_by_dependabot() -> None:
    """A manifest this repository maintains is watched, or it is named as excused.

    This is the failure the config exists to prevent, recurring: a second lockfile arrives, a
    frontend workspace is added, and nothing raises a pull request for it. The absence looks
    identical to a quiet week, which is why it needs a test rather than attention.
    """
    in_tree = _manifest_kinds_in_tree()
    kinds = set(in_tree.values())
    assert kinds == _EXPECTED_MANIFEST_KINDS, (
        f"the manifest walk found {sorted(kinds)},\nexpected {sorted(_EXPECTED_MANIFEST_KINDS)}.\n"
        "If you ADDED a manifest, give it an updates: entry and add its kind to the set above.\n"
        "If you did not, one dropped out of the walk -- a rename that _ECOSYSTEM_BY_MANIFEST no\n"
        "longer spells. The two checks below cannot see that: they compare the config against\n"
        "this walk, so a manifest missing from both halves reads as agreement."
    )

    watched = {
        (str(u.get("package-ecosystem")), str(u.get("directory"))) for u in _dependabot_updates()
    }
    unwatched = sorted(
        f"  {name} needs a {ecosystem} entry at {directory}"
        for name, (ecosystem, directory) in in_tree.items()
        if (ecosystem, directory) not in watched and name not in _UNWATCHED_MANIFESTS
    )
    assert not unwatched, (
        "these manifests have nothing keeping them current:\n"
        + "\n".join(unwatched)
        + "\n\nAdd the entry, or name the file in _UNWATCHED_MANIFESTS with the reason."
    )

    orphaned = sorted(f"  {ecosystem} at {directory}" for ecosystem, directory in watched - kinds)
    assert not orphaned, (
        "these updates: entries watch a manifest that is not in the tree, so they update\n"
        "nothing and read as coverage:\n" + "\n".join(orphaned)
    )


#: How long a release is quarantined before Dependabot will propose it, per ecosystem. Pinned
#: rather than floored, because the interesting direction is *down*: a window quietly lowered to
#: nothing reads the same as a window that was never there, and both leave the config looking
#: like it has a quarantine. `docker` is deliberately the odd one, and its reason is in
#: `.github/dependabot.yml` beside the number. Changing either value here is the point at which
#: someone has to say why.
_EXPECTED_COOLDOWN_DAYS = {
    "github-actions": 14,
    "docker": 3,
    "uv": 14,
    "npm": 14,
}


def test_every_ecosystem_quarantines_a_release_before_proposing_it() -> None:
    """A compromised release looks exactly like a good one on the day it ships.

    The delay is the whole defense: it is what lets somebody else find the compromise before
    this repository merges it. So an ecosystem added later with no ``cooldown`` is not a
    smaller version of this protection, it is none of it, and nothing else in the tree would
    say so. Dependabot security updates ignore cooldown, so an advisory-backed fix is not
    delayed by any of these numbers.
    """
    declared = {
        str(u.get("package-ecosystem")): (u.get("cooldown") or {}).get("default-days")
        for u in _dependabot_updates()
    }
    assert declared == _EXPECTED_COOLDOWN_DAYS, (
        f"cooldown windows are {declared},\nexpected {_EXPECTED_COOLDOWN_DAYS}.\n"
        "A `None` means that ecosystem declares no cooldown at all and will take a release the\n"
        "morning it ships. If you meant to change a window, change it here too and say why in\n"
        "the pull request; if an ecosystem is new, give it one."
    )


#: typescript-eslint's declared ``typescript`` peer range, as locked in
#: ``frontend/package-lock.json``. TypeScript 7 falls outside it, which is the entire reason
#: ``.github/dependabot.yml`` ignores TypeScript majors.
#:
#: Pinned as an exact string rather than parsed, deliberately. A range parser here would be a
#: second semver implementation written to be believed, and the failure it could hide is the
#: one that matters: a widened range that this test reads as still-narrow leaves the deferral
#: in place forever with nobody looking. An exact match cannot do that. It is over-eager by
#: design, firing on any change to the range including one that still excludes 7, because the
#: cost of that is a minute of reading and the cost of the other direction is being stranded on
#: TypeScript 5 indefinitely.
_TS_ESLINT_PEER_ON_TYPESCRIPT = ">=4.8.4 <6.1.0"
FRONTEND_LOCK = REPO / "frontend" / "package-lock.json"
WEBSITE_LOCK = REPO / "website" / "package-lock.json"


def _npm_majors_ignored_in(directory: str) -> set[str]:
    """Names whose majors ``.github/dependabot.yml`` declines for one npm tree.

    Scoped by directory rather than by ecosystem. Two npm trees now defer TypeScript 7 for
    unrelated causes, so an unscoped read is satisfied by whichever entry still carries the
    ignore, and it would call the other one guarded while its deferral was being deleted.
    """
    return {
        entry.get("dependency-name")
        for update in _dependabot_updates()
        if update.get("package-ecosystem") == "npm" and update.get("directory") == directory
        for entry in (update.get("ignore") or [])
        if "version-update:semver-major" in (entry.get("update-types") or [])
    }


def test_the_typescript_deferral_still_has_a_reason() -> None:
    """A deferral that outlives its cause is just a dependency nobody updates any more.

    ``.github/dependabot.yml`` declines TypeScript majors because typescript-eslint pins a
    peer range that excludes TypeScript 7, so a lone ``typescript`` bump cannot install, let
    alone build. That is a fact about somebody else's package, it will stop being true without
    anyone here doing anything, and the config cannot notice. This is what notices.

    When it fails, read typescript-eslint's new range. If it admits 7, delete the ``ignore``
    for ``typescript`` and let the grouped bump through. If it does not, update the constant.
    """
    lock = json.loads(FRONTEND_LOCK.read_text(encoding="utf-8"))
    entries = [
        meta
        for name, meta in lock.get("packages", {}).items()
        if name.endswith("node_modules/typescript-eslint")
    ]
    assert len(entries) == 1, (
        f"expected exactly one locked typescript-eslint, found {len(entries)}. The lockfile's\n"
        "shape changed; fix this walk before trusting the assertion below."
    )
    peer = (entries[0].get("peerDependencies") or {}).get("typescript")
    assert peer == _TS_ESLINT_PEER_ON_TYPESCRIPT, (
        f"typescript-eslint's typescript peer range is now {peer!r},\n"
        f"was {_TS_ESLINT_PEER_ON_TYPESCRIPT!r} when TypeScript majors were deferred.\n\n"
        "If the new range admits 7.x, the block is gone: drop the `typescript` entry from\n"
        "`ignore` in .github/dependabot.yml and let the grouped toolchain bump through.\n"
        "If it still excludes 7.x, update the constant above and leave the ignore alone."
    )

    assert "typescript" in _npm_majors_ignored_in("/frontend"), (
        "typescript-eslint still pins a peer range that excludes TypeScript 7, so a lone\n"
        "`typescript` major cannot install. Dependabot will raise one every week and it will\n"
        "be red every week (#364, then #368). Keep the ignore until this test tells you the\n"
        "range moved."
    )


#: The ``@docusaurus/tsconfig`` release the manual site's TypeScript 7 deferral was measured
#: against. Its shipped ``tsconfig.json`` sets ``baseUrl``, which TypeScript 7 removed, so
#: ``npm run typecheck`` in ``website/`` fails on an inherited option before it compiles a line.
#:
#: The cause lives inside somebody else's package rather than in this repository, and node_modules
#: is not committed, so a version is the only handle the suite has on it. That makes this pin
#: fire on every Docusaurus release, which is the intended cadence: a release is exactly when to
#: re-read whether the option was dropped.
_DOCUSAURUS_TSCONFIG_WITH_BASEURL = "3.10.2"


def test_the_manual_sites_typescript_deferral_still_has_a_reason() -> None:
    """The site defers TypeScript 7 on its own cause, so it needs its own notice.

    TypeScript 7 removed ``baseUrl``. ``@docusaurus/tsconfig`` still sets one, and
    ``website/tsconfig.json`` extends it, so ``tsc --noEmit`` fails on the inherited option no
    matter what the local config says (#414). Escaping it means inlining upstream's compiler
    options and never tracking them again, which is a poor trade for a typecheck-only bump.

    When it fails, read the new ``@docusaurus/tsconfig``. If ``baseUrl`` is gone, drop the
    ``typescript`` entry from the ``/website`` ``ignore``. If it is still there, move the
    constant to the version you just read.
    """
    lock = json.loads(WEBSITE_LOCK.read_text(encoding="utf-8"))
    entries = [
        meta
        for name, meta in lock.get("packages", {}).items()
        if name.endswith("node_modules/@docusaurus/tsconfig")
    ]
    assert len(entries) == 1, (
        f"expected exactly one locked @docusaurus/tsconfig, found {len(entries)}. The\n"
        "lockfile's shape changed; fix this walk before trusting the assertion below."
    )
    version = entries[0].get("version")
    assert version == _DOCUSAURUS_TSCONFIG_WITH_BASEURL, (
        f"@docusaurus/tsconfig is now {version!r}, was "
        f"{_DOCUSAURUS_TSCONFIG_WITH_BASEURL!r} when TypeScript 7 was deferred for the site.\n\n"
        "Read its shipped tsconfig.json. If `baseUrl` is gone, the block is gone: drop the\n"
        "`typescript` entry from the `/website` `ignore` in .github/dependabot.yml.\n"
        "If it still sets `baseUrl`, update the constant above and leave the ignore alone."
    )

    assert "typescript" in _npm_majors_ignored_in("/website"), (
        "@docusaurus/tsconfig still sets `baseUrl`, which TypeScript 7 removed, so\n"
        "`npm run typecheck` in website/ cannot pass on 7 however the local tsconfig is\n"
        "written (#414). Keep the ignore until this test tells you the option was dropped."
    )


#: The advisory-fixed versions the manual site pins by hand. ``serialize-javascript`` 7.0.5
#: clears GHSA-5c6j-r48x-rmvq (code injection through a spoofed ``RegExp.flags`` or
#: ``Date.prototype.toISOString``) and GHSA-qj8w-gfj5-8c6v (CPU exhaustion on an array-like);
#: ``uuid`` 11.1.1 clears GHSA-w5hq-g745-h8pq.
_WEBSITE_OVERRIDES = {"serialize-javascript": "7.0.7", "uuid": "11.1.1"}

#: Why the pin has to be written by hand: each dependent declares a range that excludes its own
#: fix, so no semver-compatible upgrade exists and Dependabot raises an alert it cannot propose
#: a pull request for. Read as ``(dependent, dependency): declared range``, from the lockfile
#: rather than from upstream's repository, because node_modules is not committed and the
#: declaration is the only handle the suite has on somebody else's package.
#:
#: Each entry dies a different way. The two webpack plugins are the majors Docusaurus 3.10.2
#: pins; ``copy-webpack-plugin`` 14 and ``css-minimizer-webpack-plugin`` 8 already take
#: ``^7.0.3``, so a Docusaurus release that moves to them ends those two. ``sockjs`` 0.3.24 is
#: the newest there is and still wants ``uuid@^8``, so that one ends when
#: ``webpack-dev-server`` 6 arrives, having dropped ``sockjs`` for ``ws`` outright.
_WEBSITE_OVERRIDE_CAUSES = {
    ("copy-webpack-plugin", "serialize-javascript"): "^6.0.0",
    ("css-minimizer-webpack-plugin", "serialize-javascript"): "^6.0.1",
    ("sockjs", "uuid"): "^8.3.2",
}


def test_the_manual_sites_advisory_pins_still_have_a_reason() -> None:
    """An override that outlives its cause is a version nobody updates any more.

    ``website/package.json`` pins two transitive packages past the range their dependents ask
    for, because the fixed release sits in a major those dependents exclude. That is a fact
    about somebody else's package, it stops being true without anyone here doing anything, and
    ``package.json`` has no comment syntax to say so. This is what notices.

    Neither advisory is reachable in this tree, so the pins are hygiene rather than a fix:
    ``copy-webpack-plugin`` reaches ``serialize-javascript`` only for a pattern carrying
    ``transform`` or ``transformAll`` and the site's static-directory copy carries neither,
    ``css-minimizer-webpack-plugin`` serializes build configuration whose one file-derived
    member is a string that cannot reach the vulnerable branch, and ``sockjs`` calls
    ``uuid.v4()`` with no ``buf`` while the advisory needs ``buf`` on v3, v5 or v6. They are
    held anyway, because an open alert nobody can action is one everybody learns to scroll
    past, and the next reader cannot tell it apart from one that matters.

    When it fails, re-read the dependent named in the message. If its new range admits the
    fixed major, drop that package from ``overrides`` in ``website/package.json`` and let the
    ordinary resolution take it. If it does not, move the constant above to the range you just
    read and leave the pin alone.
    """
    manifest = json.loads((REPO / "website" / "package.json").read_text(encoding="utf-8"))
    assert manifest.get("overrides") == _WEBSITE_OVERRIDES, (
        f"website/package.json overrides are now {manifest.get('overrides')!r},\n"
        f"expected {_WEBSITE_OVERRIDES!r}. These pin advisory fixes that no dependent's range\n"
        "admits; if you moved one, move this constant with it, and if you dropped one, the\n"
        "test below tells you whether its cause is actually gone."
    )

    lock = json.loads(WEBSITE_LOCK.read_text(encoding="utf-8"))
    packages = lock.get("packages", {})

    for name, pinned in _WEBSITE_OVERRIDES.items():
        entries = [meta for path, meta in packages.items() if path.endswith(f"node_modules/{name}")]
        assert len(entries) == 1, (
            f"expected exactly one locked {name}, found {len(entries)}. An override resolves a\n"
            "tree to a single copy, so a second one means the pin stopped applying to part of\n"
            "it; fix this walk before trusting the assertion below."
        )
        assert entries[0].get("version") == pinned, (
            f"website/package-lock.json resolves {name} to {entries[0].get('version')!r},\n"
            f"but package.json pins {pinned!r}. The override was edited without reinstalling:\n"
            "run `npm install` in website/ and commit the lockfile it writes."
        )

    for (dependent, dependency), declared in _WEBSITE_OVERRIDE_CAUSES.items():
        entries = [
            meta for path, meta in packages.items() if path.endswith(f"node_modules/{dependent}")
        ]
        assert len(entries) == 1, (
            f"expected exactly one locked {dependent}, found {len(entries)}. The lockfile's\n"
            "shape changed; fix this walk before trusting the assertion below."
        )
        current = (entries[0].get("dependencies") or {}).get(dependency)
        assert current == declared, (
            f"{dependent} now asks for {dependency} {current!r}, was {declared!r} when the\n"
            f"{dependency} override was written.\n\n"
            f"If the new range admits {_WEBSITE_OVERRIDES[dependency]!r}, the block is gone:\n"
            f"drop {dependency!r} from `overrides` in website/package.json, reinstall, and\n"
            "commit the lockfile. If it still excludes the fix, update the constant above and\n"
            "leave the pin alone."
        )


def _accepted_title_types() -> set[str]:
    """The Conventional Commit types ``pr-validation.yml`` lets a pull request title carry."""
    workflow = yaml.safe_load(PR_TITLE_WORKFLOW.read_text(encoding="utf-8"))
    steps = workflow["jobs"]["title"]["steps"]
    declared = next(s["with"]["types"] for s in steps if "types" in (s.get("with") or {}))
    return set(declared.split())


def test_a_dependabot_pull_request_arrives_shaped_like_every_other_one() -> None:
    """Rule 144: one fact about how a pull request must look, written in two files.

    A squash-merge makes the pull request title the permanent commit message, so
    ``pr-validation.yml`` gates it as a Conventional Commit; Dependabot's own default title
    ("Bump x from a to b") does not parse, and ``commit-message.prefix`` is what fixes that.
    Neither file names the other, and the failure lands days later on a pull request nobody
    opened, so a type dropped from that workflow's list reads as Dependabot being broken.

    The labels are the same shape of fact: the queue is filtered by ``Kind/`` and
    ``Priority/``, and Dependabot applies only labels that already exist and drops the rest
    without a word, so a renamed label makes these pull requests invisible rather than red.
    """
    accepted = _accepted_title_types()
    assert accepted, (
        "no types parsed out of pr-validation.yml, so the check below would accept anything.\n"
        "The step's shape changed; fix _accepted_title_types before trusting this test."
    )

    problems: list[str] = []
    for update in _dependabot_updates():
        where = f"{update.get('package-ecosystem')} at {update.get('directory')}"
        prefix = (update.get("commit-message") or {}).get("prefix")
        if prefix not in accepted:
            problems.append(
                f"  {where}: commit-message.prefix {prefix!r} is not a type pr-validation.yml\n"
                f"    accepts ({', '.join(sorted(accepted))}), so every pull request it opens\n"
                "    fails the title check."
            )
        labels = update.get("labels") or []
        if not any(label.startswith("Kind/") for label in labels):
            problems.append(
                f"  {where}: no Kind/ label, so the pull request is missing from the queue"
            )
        priorities = [label for label in labels if label.startswith("Priority/")]
        if len(priorities) != 1:
            problems.append(f"  {where}: needs exactly one Priority/ label, has {priorities}")
    assert not problems, "dependabot.yml opens pull requests that do not fit here:\n" + "\n".join(
        problems
    )


CODEQL_WORKFLOW = REPO / ".github" / "workflows" / "codeql.yml"

#: Each language CodeQL analyzes, against the tree it is there for. Three cover the five names
#: the old settings page listed: ``javascript-typescript`` and ``typescript`` are aliases of
#: ``javascript`` in the action's own ``src/languages/builtin.json``, so the UI was naming one
#: extractor three times.
#:
#: The tree is half the pin and the reason this is not just a spelling check. A language whose
#: tree moved is analyzing nothing, and an empty analysis is reported as a clean one — the same
#: shape as a walk that silently collects no members (rule 145).
_CODEQL_LANGUAGES = {
    "actions": ".github/workflows",
    "javascript-typescript": "frontend/src",
    "python": "src/reaper",
}

#: What ``codeql.yml`` declines to scan, which is ``ci.yml``'s prose lane written in glob rather
#: than in a shell ``case``. The two spellings cannot be diffed, so they are pinned instead and
#: the failure below names the other file (rule 144).
_CODEQL_PATHS_IGNORE = ["docs/**", ".claude/**", "**/*.md"]


def test_the_codeql_analysis_still_covers_every_tree() -> None:
    """Code scanning is configuration, so it is held like the rest of it.

    This moved out of a settings page precisely because a settings page has no diff: deselecting
    a language there stops analyzing a tree, and it looks exactly like a quiet week. Here it is
    a deleted line, and this is what makes it a red test as well.

    Four facts, each of which fails silently rather than loudly if it drifts. The languages and
    the trees they point at, because an extractor aimed at a moved directory reports no findings
    the same way a clean tree does. The absence of a ``queries:`` override, because adding the
    extended suite is a decision about triage load and not a tuning knob to reach for quietly.
    ``category``, because without it each upload replaces the last and only the language that
    finished last keeps its results. And ``paths-ignore``, which is safe only while CodeQL is not
    a required check: a skipped workflow publishes no check run, so requiring one that a
    prose-only pull request never runs strands it forever.
    """
    workflow = yaml.safe_load(CODEQL_WORKFLOW.read_text(encoding="utf-8"))

    matrix = workflow["jobs"]["analyze"]["strategy"]["matrix"]["language"]
    assert set(matrix) == set(_CODEQL_LANGUAGES), (
        f"codeql.yml analyzes {sorted(matrix)}, expected {sorted(_CODEQL_LANGUAGES)}.\n"
        "Dropping one stops analyzing a whole tree and reports it as clean. If a language was\n"
        "added or genuinely retired, move the mapping above with it."
    )
    for language, tree in _CODEQL_LANGUAGES.items():
        assert (REPO / tree).exists(), (
            f"codeql.yml analyzes {language!r} for {tree}, which is not in this checkout any\n"
            "more. An extractor pointed at a directory that moved finds nothing, and nothing\n"
            "is what a clean tree looks like. Repoint the mapping above at the new path."
        )

    steps = workflow["jobs"]["analyze"]["steps"]
    init = [s for s in steps if "codeql-action/init" in (s.get("uses") or "")]
    analyze = [s for s in steps if "codeql-action/analyze" in (s.get("uses") or "")]
    assert len(init) == 1 and len(analyze) == 1, (
        f"expected one init and one analyze step, found {len(init)} and {len(analyze)}.\n"
        "The workflow's shape changed; fix this walk before trusting the assertions below."
    )

    assert "queries" not in (init[0].get("with") or {}), (
        "codeql.yml now sets `queries:`, so it is no longer running the `default` suite that\n"
        "default setup ran. The extended `security-and-quality` suite was declined against a\n"
        "measured ratio, not a preference: of eleven alerts this repository fixed seven and\n"
        "dismissed four. Re-read that ratio in the Security tab and, if it still holds, take\n"
        "the line back out."
    )

    assert (analyze[0].get("with") or {}).get("category") == "/language:${{ matrix.language }}", (
        "codeql.yml's analyze step lost its per-language `category`. Without one, each upload\n"
        "is read as replacing the last, and every language but the slowest appears to have\n"
        "found nothing."
    )

    for trigger in ("push", "pull_request"):
        declared = workflow[True][trigger]["paths-ignore"]
        assert declared == _CODEQL_PATHS_IGNORE, (
            f"codeql.yml's {trigger} paths-ignore is {declared}, expected "
            f"{_CODEQL_PATHS_IGNORE}.\n"
            "This is .github/workflows/ci.yml's prose lane (`docs/*|.claude/*|*.md` in its\n"
            "`changes` job) written as globs. Move both together, and remember the list is\n"
            "safe only while CodeQL is not a required status check."
        )


#: Both spellings of "which Node major" in this checkout: the image's ``FROM node:24-alpine``
#: and the workflow's ``node-version: "24"``. The quote is optional on the second because yaml
#: reads the bare form identically, and a matcher that only accepts quotes goes blind on an
#: edit that is not even a change (rule 147).
_NODE_MAJOR = re.compile(r"""(?:FROM\s+node:|node-version:\s*)["']?(\d+)""")

#: The Dockerfile, ci.yml twice (the `frontend` and `site` jobs), docs-deploy.yml, and
#: binaries.yml (the packaged builds bundle the SPA on the same Node the frontend job tested
#: it on). Pinned because the agreement assertion below is vacuously true on one site, or on
#: none (rule 145).
#:
#: The manual site's two are here for the same reason as the others rather than as bookkeeping:
#: it builds with `npm ci` against a committed lockfile, so a Node major that drifts from the
#: one the lockfile was resolved on is a publish that fails on a tree nothing else exercises.
_EXPECTED_NODE_SITES = 5


def test_the_node_major_is_one_supported_lts_line_in_the_image_and_in_ci() -> None:
    """The image builds the bundle on the Node the frontend job tested it on, and it is an LTS.

    Two files name a Node major and nothing held them together, which cost nothing while both
    only moved by hand. Dependabot moves the Dockerfile's: ``node``'s tag carries a version, so
    a major bump arrives as a pull request that edits one of the two. Left alone, ``npm run
    build`` in CI proves a bundle on one runtime and the shipped image builds it on another.

    The parity is the second half of the same fact, and it is checked rather than remembered
    because "take the newest major" is wrong for Node specifically: only even majors are
    promoted to LTS, and an odd one is end-of-life within about eight months. The first pull
    request ``.github/dependabot.yml`` ever opened proposed 24 to 25, two months after 25 had
    died. The config now declines to raise that; this declines to merge it however it arrives.
    """
    sites = [
        (path.relative_to(REPO), lineno, match.group(1))
        for path, text in _repo_text_files()
        for lineno, line in enumerate(text.splitlines(), 1)
        if (match := _NODE_MAJOR.search(line))
    ]
    assert len(sites) == _EXPECTED_NODE_SITES, (
        f"expected {_EXPECTED_NODE_SITES} declarations of the Node major, found {len(sites)}:\n"
        + "\n".join(f"  {p}:{n} -> {v}" for p, n, v in sites)
        + "\n\nIf you ADDED one, bump the number so it is covered. If you did not, one dropped\n"
        "out of the walk and the agreement below no longer reads it."
    )
    majors = {version for _, _, version in sites}
    assert len(majors) == 1, (
        "the Node major disagrees between the image and CI:\n"
        + "\n".join(f"  {p}:{n} -> {v}" for p, n, v in sites)
        + "\n\nMove both together, or CI tests the bundle on a runtime the image does not use."
    )
    (major,) = majors
    assert int(major) % 2 == 0, (
        f"Node {major} is an odd major, which Node never promotes to LTS and retires within\n"
        "about eight months. Move to the next EVEN major once it reaches LTS, rather than to\n"
        "the newest one that exists: https://github.com/nodejs/Release#release-schedule"
    )


#: Every spelling of the typecheck invocation: the workflow step, CONTRIBUTING's gate list, and
#: the review skill's apply-the-fixes list. Matched on the whole argument run rather than on a
#: fixed prefix, because a matcher anchored on ``mypy src/reaper`` reads a site that has NOT been
#: widened as agreeing (rule 147).
_MYPY_INVOCATION = re.compile(r"uv run mypy ((?:[\w./\[\]*-]+ ?)+?)(?=\s*(?:#|`|$))", re.M)

#: `.github/workflows/ci.yml`, `CONTRIBUTING.md`, `.claude/skills/reaper-review/SKILL.md`, and
#: `tests/_fakes.py`'s own docstring -- which is the copy most likely to go stale, since it is
#: the file arguing for its place on the gate. `docs/history/**` is frozen and records what the
#: gate was at the time, so it is skipped rather than counted; `docs/I18N_PLAN.md` proposes a
#: gate for a plan nothing has started, and `docs/SIMPLIFICATION_PLAN.md` records the command as
#: it stood when a change landed and moves to `docs/history/` when it retires.
_EXPECTED_MYPY_SITES = 4

#: Files that quote the command as a record rather than as the instruction to follow. A record
#: is pinned to its moment, so holding it to today's gate would ask a finished plan to be edited
#: every time the gate moves.
_MYPY_RECORDS = ("docs/history/", "docs/I18N_PLAN.md", "docs/SIMPLIFICATION_PLAN.md")


def test_the_typecheck_gate_names_the_same_targets_everywhere_it_is_written() -> None:
    """`tests/` rides on the mypy run, and four files say so independently.

    Two things have to hold, and each is checked below. **`tests/` is named**, or nothing in
    it is type-checked and the structural fakes that inherit their real client prove nothing
    by doing so (#580). And **`src/reaper` is named alongside it**, or mypy resolves `reaper`
    from site-packages, finds no py.typed marker, and reports 731 import errors while
    silently checking almost none of the tree -- a run that looks like it did the work.

    The invocation is written four times, by four authors reading each other. A developer
    running CONTRIBUTING's list would otherwise get a narrower check than CI runs and see a
    clean tree that CI rejects, or the reverse. Rule 144, and the direction is the dangerous
    one: a stale copy reads as the shorter, safer-looking command, so nothing about it looks
    wrong.
    """
    sites = [
        (path.relative_to(REPO), lineno, " ".join(match.group(1).split()))
        for path, text in _repo_text_files()
        if not any(rec in path.relative_to(REPO).as_posix() for rec in _MYPY_RECORDS)
        for lineno, line in enumerate(text.splitlines(), 1)
        if (match := _MYPY_INVOCATION.search(line))
    ]
    assert len(sites) == _EXPECTED_MYPY_SITES, (
        f"expected {_EXPECTED_MYPY_SITES} spellings of the typecheck gate, found "
        f"{len(sites)}:\n" + "\n".join(f"  {p}:{n} -> {t}" for p, n, t in sites) + "\n\n"
        "If you ADDED one, bump the number so it is covered. If you did not, one dropped out\n"
        "of the walk and the agreement below no longer reads it."
    )
    targets = {t for _, _, t in sites}
    assert len(targets) == 1, (
        "the typecheck gate names different targets in different files:\n"
        + "\n".join(f"  {p}:{n} -> {t}" for p, n, t in sites)
        + "\n\nMove them together. A developer running the narrow one sees a clean tree that\n"
        "CI rejects, or CI runs a check nobody can reproduce."
    )
    (targets_run,) = targets
    named = targets_run.split()
    assert any(target.startswith("tests") for target in named), (
        f"the typecheck gate runs `{targets_run}`, which no longer covers tests/.\n"
        "The structural fakes inherit their real client so that a signature change they stop\n"
        "matching fails the build; off the gate they are unchecked, and inheriting proves\n"
        "nothing (#580)."
    )
    assert "src/reaper" in named, (
        f"the typecheck gate runs `{targets_run}`, which no longer names src/reaper.\n"
        "Without it mypy resolves `reaper` from site-packages, finds no py.typed marker, and\n"
        "answers with import errors instead of checking the tree -- green-looking, and blind."
    )


#: Every `paths:` / `paths-ignore:` list under `.github/workflows/`, reconciled by hand and
#: **named, not counted**. `codeql.yml` filters both its triggers, `docs-deploy.yml` filters its
#: one, and `ci.yml` has none deliberately -- it runs on everything and classifies the diff
#: inside a job, so its verdict can be read by other jobs and a skipped lane still reports.
#:
#: This is here because two sentences describe the arrangement in prose and both were wrong:
#: `ci.yml`'s `changes` comment and CLAUDE.md's "which jobs appear" paragraph each said nothing
#: else in the repository restated the path list, while three lists sat in two files (rule
#: 7/24). It is a SET rather than a number because those sentences name which file holds which,
#: and a count cannot see a filter moving between files -- move `docs-deploy.yml`'s to
#: `release.yml` and a pinned `3` stays green while both sentences go false (rule 145: pin the
#: population, and a scalar is not one).
_WORKFLOW_PATH_FILTERS = frozenset(
    {
        "codeql.yml:push:paths-ignore",
        "codeql.yml:pull_request:paths-ignore",
        "docs-deploy.yml:push:paths",
    }
)


def test_the_workflows_that_filter_themselves_by_path_are_the_ones_the_prose_names() -> None:
    """A `paths` filter decides whether a workflow starts, so it cannot read `ci.yml`.

    That is the whole reason more than one list exists, and it is not going away: a workflow
    skipped by its own filter publishes no check run at all, which is safe here only while
    neither of these two is a required check. `ci.yml` answers the same question the other
    way, inside a job -- and a JOB skipped by an `if:` does report, with conclusion `skipped`,
    which is what lets `CI gate` count one as a pass.

    **Two prose copies say which file holds which list** -- `.github/workflows/ci.yml`'s
    `changes` comment and CLAUDE.md's "Which jobs appear depends on what the commit touched"
    paragraph. Both are named in the failure below, because a set that moves without them is
    the same falsehood arriving a second time (rule 144).
    """
    found: dict[str, list[str]] = {}
    workflows = REPO / ".github" / "workflows"
    # Both extensions, because GitHub reads both and the tree happens to use one. A walk
    # matching only what is there today reports a clean repository for a filter added in the
    # spelling it does not look for (rule 147).
    for path in sorted([*workflows.glob("*.yml"), *workflows.glob("*.yaml")]):
        workflow = yaml.safe_load(path.read_text(encoding="utf-8"))
        # `on` is YAML 1.1's boolean true, which is why the key is read both ways.
        triggers = workflow.get("on", workflow.get(True)) or {}
        for event, spec in triggers.items():
            if not isinstance(spec, dict):
                continue
            for key in ("paths", "paths-ignore"):
                if key in spec:
                    found[f"{path.name}:{event}:{key}"] = spec[key]

    assert frozenset(found) == _WORKFLOW_PATH_FILTERS, (
        "the workflow path filters moved.\n"
        f"  new:  {sorted(frozenset(found) - _WORKFLOW_PATH_FILTERS) or 'none'}\n"
        f"  gone: {sorted(_WORKFLOW_PATH_FILTERS - frozenset(found)) or 'none'}\n"
        "  now:  " + "\n        ".join(f"{w} -> {g}" for w, g in found.items()) + "\n\n"
        "Both prose copies name which file holds which, so either direction needs them\n"
        "corrected in the same commit: the `changes` job's comment in\n"
        ".github/workflows/ci.yml, and CLAUDE.md's paragraph on which jobs appear."
    )
    # What codeql's two lists CONTAIN is pinned by `_CODEQL_PATHS_IGNORE` above, which asserts
    # each trigger equals it by value -- strictly stronger than the two agreeing with each
    # other, so nothing is repeated here.


BINARIES_WORKFLOW = REPO / ".github" / "workflows" / "binaries.yml"


def test_binaries_publish_is_gated_to_the_dev_ref() -> None:
    """A hand dispatch of binaries.yml on a branch may build and boot-probe, never publish.

    The Decide step's publish output feeds three channels at once — the dev-build
    prerelease, the snap edge channel, and the ghcr :dev arm64 fold — and the workflow's
    own header recommends dispatching against a branch to prove a packaging change.
    Without the ref gate, ticking the publish input on that dispatch replaces all three
    with unmerged branch code described as dev until the next nightly. The gate is one
    line of shell the header's promise depends on (rule 7/24), so its presence is pinned.
    """
    text = BINARIES_WORKFLOW.read_text(encoding="utf-8")
    assert '"${GITHUB_REF}" != "refs/heads/dev"' in text, (
        "binaries.yml's Decide step no longer refuses to publish from a non-dev ref:\n"
        "a branch dispatch with publish ticked would replace the dev-build prerelease,\n"
        "the snap edge channel, and the ghcr :dev image with unmerged branch code."
    )


#: Every ``run:`` script under `.github/workflows/` that turns on `pipefail`, counted so a
#: block leaving the walk is visible rather than silently dropping out of the ban below
#: (rule 145). Reconciled against the walk: `binaries.yml` 8, `ci.yml` 3, `release.yml` 5,
#: `virustotal.yml` 2. The other five workflows set it nowhere, and a block without it is
#: out of scope on purpose: there the pipeline's status is the reader's, which is the answer
#: the step wanted. A first hand count said 14, and the walk is what corrected it.
_PIPEFAIL_RUN_BLOCKS = 18


def test_no_pipefail_gate_reads_its_verdict_through_a_short_circuiting_pipe() -> None:
    """Under `pipefail` a pipeline reports its rightmost failure, and `head` makes one.

    `head -c N` and `grep -q` both exit as soon as they have their answer, which can kill the
    writer with SIGPIPE. The pipeline's status is then the writer's, so the step fails while
    the thing it was testing succeeded. **Whether it fires depends on how much the writer
    produced**: under the pipe buffer the writer finishes and exits 0 before either reader
    closes. `binaries.yml`'s three boot probes read the served page through
    `curl … | head -c 200 | grep -qi`, and were measured passing on a 4 KB page and failing on
    a 200 KB one, so a healthy build whose page grew would have read as a bundle that lost its
    SPA. CLAUDE.md's rule 134 names the mechanism; this is the gate for it, because the prose
    binds an author who read it and a workflow is edited by one who did not.

    **Spellings accepted** (rule 147): `| head`, `|head`, and the same two for `tail`, anywhere
    in a script that sets `pipefail`. A reader that short-circuits under another name is out of
    reach and is named here rather than implied covered. Redirect to a file and read the file.
    """
    workflows = REPO / ".github" / "workflows"
    scripts: list[tuple[str, str]] = []
    for path in sorted([*workflows.glob("*.yml"), *workflows.glob("*.yaml")]):
        workflow = yaml.safe_load(path.read_text(encoding="utf-8"))
        for job_name, job in (workflow.get("jobs") or {}).items():
            for step in job.get("steps") or []:
                script = step.get("run")
                if isinstance(script, str) and "pipefail" in script:
                    scripts.append((f"{path.name}:{job_name}:{step.get('name', 'run')}", script))

    assert len(scripts) == _PIPEFAIL_RUN_BLOCKS, (
        f"the pipefail run blocks moved: {len(scripts)} found, "
        f"{_PIPEFAIL_RUN_BLOCKS} expected.\n"
        "Reconcile the count above by hand, then update _PIPEFAIL_RUN_BLOCKS. A block that\n"
        "left the walk is missing from the ban below as well as from this number.\n"
        "  " + "\n  ".join(name for name, _ in scripts)
    )

    offenders = [
        f"{name} -> {line.strip()}"
        for name, script in scripts
        for line in script.splitlines()
        if "| head" in line or "|head" in line or "| tail" in line or "|tail" in line
    ]
    assert not offenders, (
        "a pipefail'd workflow step decides on a pipeline whose reader short-circuits:\n  "
        + "\n  ".join(offenders)
        + "\n\nThe writer takes SIGPIPE once its output outgrows the pipe buffer, so the step\n"
        "fails on a healthy result. Redirect to a file and read the file instead:\n"
        '  curl -s "$url" > out.html || true\n'
        '  if grep -qi "<!doctype html>" out.html; then spa=true; fi'
    )


#: ``plan`` and the four jobs it fans out to. Pinned because the reconciliation below is
#: vacuously true against a walk that stopped finding jobs (rule 145): a publisher renamed
#: out of the scan is missing from the check and from its own count at the same time.
_EXPECTED_BINARIES_JOBS = 5


def test_every_nightly_publisher_gates_the_release_that_records_the_night() -> None:
    """The nightly's memory of "this sha is built" is only written when every target shipped.

    ``plan`` skips a whole scheduled run when the ``dev-build`` prerelease already targets
    dev's tip, and ``publish-dev`` is what moves that target. So the release doubles as the
    record of a finished night, and every job that publishes for one has to be a ``needs``
    of it: a target that failed must leave the sha unrecorded, or the next nightly reads the
    night as done and skips the retry (#457). ``docker-arm64`` was not a ``needs``, so a
    failed arm64 image was recorded as built and ``:dev`` kept serving an arm64 layer older
    than its amd64 half until dev moved again.

    A publisher is any job gated on the publish decision, wherever it reads it: ``snap``
    checks it on the step that pushes to the store, ``docker-arm64`` on the job. The parsed
    job is searched rather than the file text, so neither placement can hide one (rule 147).

    The second assertion is what makes the first safe. Adding a ``needs`` means ``publish-dev``
    now skips whenever that job skips, which is harmless only while ``docker-arm64`` cannot
    skip on its own: its ``if`` also reads ``build``, and that is redundant purely because
    ``plan`` never clears ``build`` without clearing ``publish`` in the same breath. Split
    those two assignments and a no-op night would stop refreshing the prerelease.
    """
    workflow = yaml.safe_load(BINARIES_WORKFLOW.read_text(encoding="utf-8"))
    jobs = workflow["jobs"]
    assert len(jobs) == _EXPECTED_BINARIES_JOBS, (
        f"expected {_EXPECTED_BINARIES_JOBS} jobs in binaries.yml, found {len(jobs)}: "
        f"{sorted(jobs)}\n\nIf you ADDED one, bump the number so the scan below covers it.\n"
        "If you did not, a job was renamed and dropped out of the publisher walk."
    )

    publishers = {
        name
        for name, job in jobs.items()
        if name not in {"plan", "publish-dev"}
        and "needs.plan.outputs.publish" in yaml.safe_dump(job)
    }
    assert publishers, (
        "no job in binaries.yml reads needs.plan.outputs.publish any more, so this test\n"
        "proves nothing. Either the publish decision was renamed, or the jobs that act on\n"
        "it were, and the walk above now returns an empty set that trivially passes."
    )
    recorded = set(jobs["publish-dev"]["needs"])
    assert publishers <= recorded, (
        f"binaries.yml publishes from {sorted(publishers - recorded)} without publish-dev\n"
        "depending on it, so a night where that target fails still moves the dev-build\n"
        "release onto the sha. plan reads the release to decide whether dev has moved, so\n"
        "the next nightly counts the night as done and never retries the failed target.\n"
        "Add the job to publish-dev's `needs` (#457)."
    )

    decide = next(s for s in jobs["plan"]["steps"] if s.get("id") == "decide")
    clears_build = [line for line in decide["run"].splitlines() if "build=false" in line]
    assert clears_build and all("publish=false" in line for line in clears_build), (
        "binaries.yml's Decide step turns off `build` without turning off `publish` in the\n"
        "same statement:\n" + "\n".join(f"  {line.strip()}" for line in clears_build) + "\n\n"
        "publish-dev needs docker-arm64, and docker-arm64's `if` also reads `build`. While\n"
        "those two are cleared together that extra condition is redundant and the two jobs\n"
        "skip as one. Split them and a build=false night skips docker-arm64 while publish\n"
        "stays true, which skips publish-dev with it and strands the prerelease."
    )


# --- the docs discipline -----------------------------------------------------------------
#
# These check the STRUCTURE of docs/, never the truth of a sentence. CI can prove that an
# archived file says it is frozen; it cannot prove a paragraph is still accurate. The point is
# to make the cheap failures impossible so the expensive ones stay visible.


def test_status_doc_stays_small() -> None:
    """``docs/STATUS.md`` is the live state, and it only works while it is small.

    The plan it replaced reached 3,508 append-only lines, at which point updating it meant
    first reading enough of it to find where the update went. Co-change with code commits fell
    to 24.7%. A doc you edit in place stays cheap to edit; this budget is what keeps it one.
    """
    lines = len(STATUS_DOC.read_text(encoding="utf-8").splitlines())
    assert lines <= STATUS_MAX_LINES, (
        f"docs/STATUS.md is {lines} lines, over its {STATUS_MAX_LINES}-line budget.\n"
        "Move reasoning to docs/DECISIONS.md, measured findings to docs/LEARNINGS.md, and the "
        "story of how a fix was chosen to docs/history/, then shorten what is left. Closed work "
        "leaves the file entirely. Do not raise this number to make the test pass."
    )


def test_status_doc_lines_stay_narrow() -> None:
    """No line of ``docs/STATUS.md`` is wider than the repo's one width.

    This is the other half of the line budget, and the half that was missing. With only a line
    cap, a file sitting at its limit can still absorb any amount of new text -- by lengthening
    a line that already exists. That is what happened: a "Decisions locked" cell reached 21,210
    characters, three cells held 66% of the file, and every agent editing a row had to rewrite a
    paragraph-length line to change a phrase.

    A markdown table row cannot be wrapped, so this cap is also what keeps narration out of a
    table cell: at 100 columns a cell holds a phrase and nothing longer. The reasoning belongs in
    ``docs/DECISIONS.md``, where prose can wrap.
    """
    offenders = [
        f"  line {n} is {len(line)} columns: {line[:60]}..."
        for n, line in enumerate(STATUS_DOC.read_text(encoding="utf-8").splitlines(), start=1)
        if len(line) > STATUS_MAX_COLUMNS
    ]
    assert not offenders, (
        f"docs/STATUS.md has lines over its {STATUS_MAX_COLUMNS}-column budget:\n"
        + "\n".join(offenders)
        + "\n\nA table row cannot wrap, so a cell this long is narration in the wrong file. "
        "Cut the row to a phrase and move the reasoning to docs/DECISIONS.md."
    )


def _dagger_rows() -> set[str]:
    """Keys of the "Decisions locked" rows that promise a section in ``docs/DECISIONS.md``."""
    keys: set[str] = set()
    in_table = False
    for line in STATUS_DOC.read_text(encoding="utf-8").splitlines():
        if line.startswith("## "):
            in_table = line == "## Decisions locked"
            continue
        if not in_table or not line.startswith("|"):
            continue
        cells = [cell.strip() for cell in line.strip("|").split("|")]
        if len(cells) == 2 and "†" in cells[1]:
            keys.add(cells[0])
    return keys


def test_the_documented_status_budget_matches_the_enforced_one() -> None:
    """The budget is stated in prose four times, so the prose is checked against the constants.

    Rule 144: one fact about how the tool works, written in several places by authors each
    reading a different one. Nothing here is generated, so a cap lowered in this file would leave
    four confident sentences quoting the old number -- and a reader who trusts them writes to a
    budget that is no longer enforced. Cheaper to name the files in a failure message than to ask
    the next author to remember them.
    """
    phrase = f"{STATUS_MAX_LINES} lines and {STATUS_MAX_COLUMNS} columns"
    sites = [REPO / "CLAUDE.md", DOCS / "README.md", STATUS_DOC]
    offenders = [
        str(p.relative_to(REPO)) for p in sites if phrase not in p.read_text(encoding="utf-8")
    ]
    assert not offenders, (
        f'these files no longer state the enforced budget as "{phrase}":\n'
        + "\n".join(offenders)
        + "\n\nCorrect the prose, or correct STATUS_MAX_LINES/STATUS_MAX_COLUMNS -- but do not "
        "raise a cap to make a test pass."
    )


#: The rewatch curve, as the two source comments quote it: dormancy band -> the percentage each
#: states. Both are prose justifying a shipped default (the 1,095-day dormancy floor, and the
#: 365/1825 UNWATCHED ramp), and the measurement behind them lives only in `docs/SIGNALS.md`.
#: The constant that used to tie the three together went with the engine that measured it.
_REWATCH_CURVE_CLAIMS = {
    "engine/gates.py": ("61%", "30%", "19%", "13%"),
    "engine/policy.py": ("61%", "13%"),
}


def test_the_rewatch_percentages_in_source_are_the_ones_signals_md_measured() -> None:
    """Rule 144, for the file's most-quoted table.

    Two docstrings in ``src/`` state percentages off the rewatch curve as the measured reason a
    shipped default is what it is, and neither is generated from anything. Nothing else in the
    tree carries those numbers: the constant that did was deleted with the replay engine, and
    the test that pinned the constant went with it. So a re-measurement that edits
    ``docs/SIGNALS.md`` alone leaves two confident sentences quoting the old curve as
    justification for a floor derived from it -- and the operator reading the why-panel is
    told a number nobody stands behind any more.

    Checked against the "Ground truth" table rather than against a copy here, so this test
    cannot drift from the measurement either. It names both source files in the failure, which
    is the whole of rule 144's remedy: a comment asking the next author to remember does
    nothing, and a failure message costs one line.
    """
    table = (DOCS / "SIGNALS.md").read_text(encoding="utf-8")
    heading = "## Ground truth: rewatch probability by dormancy"
    assert heading in table, f"{heading!r} is gone from docs/SIGNALS.md, so nothing anchors this"
    ground_truth = table.split(heading, 1)[1].split("###", 1)[0]

    offenders = []
    for rel, percentages in _REWATCH_CURVE_CLAIMS.items():
        source = (SRC / rel).read_text(encoding="utf-8")
        for percent in percentages:
            # `~61%` in the table, "about 61%" or "~61% of films" in the source.
            if percent not in source:
                offenders.append(f"{rel} no longer states {percent}")
            elif percent not in ground_truth:
                offenders.append(f"docs/SIGNALS.md's ground-truth table no longer states {percent}")
    assert not offenders, (
        "the rewatch curve is written in three places and they disagree:\n  "
        + "\n  ".join(sorted(set(offenders)))
        + "\n\nThe measurement is docs/SIGNALS.md's 'Ground truth' table. If it was re-measured, "
        "correct src/reaper/engine/gates.py's MinDormancyGate docstring and "
        "src/reaper/engine/policy.py's DEFAULT_MOVIE_POLICY signal comment in the same change -- "
        "both quote it as the reason a shipped default is the number it is."
    )


def test_every_decisions_section_matches_a_locked_decision_row() -> None:
    """``docs/STATUS.md`` and ``docs/DECISIONS.md`` name the same decisions, both ways.

    STATUS.md holds each choice as a phrase and marks with a dagger the ones whose reasoning
    lives in DECISIONS.md. That dagger is a promise to the reader, so it is checked: a section
    renamed on one side and not the other leaves either a pointer into nothing or reasoning
    nobody can find from the table. Rule 144 -- one fact written in two places, so the test names
    the other file rather than a comment asking the next author to remember.
    """
    sections = {
        line.removeprefix("## ").strip()
        for line in DECISIONS_DOC.read_text(encoding="utf-8").splitlines()
        if line.startswith("## ")
    }
    rows = _dagger_rows()
    assert len(rows) == DECISION_SECTIONS, (
        f"the dagger walk collected {len(rows)} rows of 'Decisions locked', expected "
        f"{DECISION_SECTIONS}. Reconcile by hand, then correct DECISION_SECTIONS: comparing two "
        "collections cannot tell a row that agrees from one that fell out of the walk (rule 145)."
    )
    assert sections == rows, (
        "docs/DECISIONS.md sections and the daggered rows of docs/STATUS.md disagree.\n"
        f"  sections with no daggered row: {sorted(sections - rows)}\n"
        f"  daggered rows with no section: {sorted(rows - sections)}"
    )


def _worked_example_facts(*, watchers: int) -> Facts:
    """The one title every worked example in the repo is written about."""
    return Facts(
        title="A Film",
        days_observed_unwatched=Known(value=2059, source="t"),
        distinct_watchers=Known(value=watchers, source="t"),
        distinct_watchers_all_time=Known(value=max(watchers, 1), source="t"),
        size_bytes=Known(value=5_900_000_000, source="radarr"),
        imdb_rating_tenths=Known(value=54, source="imdb"),
        imdb_votes=Known(value=6_000, source="imdb"),
        season_rank=Absent(source="radarr"),
        is_streaming_now=Known(value=False, source="t"),
        is_managed=Known(value=True, source="radarr"),
        in_curated_list=Absent(source="lists"),
        is_whitelisted=Known(value=False, source="plex"),
        history_reach_days=Known(value=3_000, source="t"),
    )


def _checked_examples() -> dict[str, list[Path]]:
    """Each real "checked, did not fire" sentence, against the files quoting that one.

    Derived by running the gate, never transcribed (rule 119). Two different gates, because
    the docs do not all illustrate the same lane: the panel's own worked example is the
    dormancy sentence, and the manual's is the popularity one.
    """
    dormancy = MinDormancyGate(GateConfig(threshold=1_095))
    popularity = ServerPopularityGate(GateConfig(threshold=3, window_days=365))
    examples = {
        dormancy.evaluate(_worked_example_facts(watchers=0)).detail: [
            REPO / "README.md",
            DECISIONS_DOC,
            SRC / "engine" / "explanation.py",
            FRONTEND_SRC / "components" / "WhyPanel.tsx",
            TESTS / "test_api.py",
        ],
        popularity.evaluate(_worked_example_facts(watchers=2)).detail: [
            REPO / "manual" / "features.mdx",
        ],
    }
    assert len(examples) == 2, "two gates produced the same sentence; the guard lost a lane"
    return examples


def test_the_documented_checked_example_is_one_a_gate_emits() -> None:
    """Every file illustrating a checked protection quotes a string the code really builds.

    Rule 144, and the case that prompted its "grep the sibling copies" clause. One fact about
    what the panel shows was written in six places by authors each reading a different copy,
    and every one drifted onto an invented ``"checked: <label> -- <numbers>"`` shape no gate has
    ever emitted (#419) -- including the README bullet naming the differentiator Reaper exists
    for, and the docstring on the field that carries the strings. The drift ran the direction
    that rule predicts: the invention was more specific and more reassuring than the real
    output, so it flattered the feature it documented.

    So the examples are *derived* here by running the gates, and this test names the other files
    rather than a comment asking the next author to remember. Reword a gate's ABSTAIN branch and
    this fails with the list of files to correct.
    """

    # Both sides collapse to single spaces, because a doc wraps its prose and a quoted
    # sentence lands across two lines as often as not. That bounds the matcher to a
    # continuation line carrying no prefix of its own (rule 147): a wrapped ``//`` comment
    # keeps its slashes and reads as a miss, which the message below tells the author to fix
    # by unwrapping. Only whitespace is normalized, never punctuation -- the em dash and the
    # trailing period are exactly what drifted.
    def flat(text: str) -> str:
        return " ".join(text.split())

    missing = [
        f"{path.relative_to(REPO)} no longer quotes {detail!r}"
        for detail, paths in _checked_examples().items()
        for path in paths
        if flat(detail) not in flat(path.read_text(encoding="utf-8"))
    ]
    assert not missing, (
        "a gate's wording moved and its documented copies did not follow:\n"
        + "\n".join(f"  {line}" for line in missing)
        + "\nQuote the new sentence verbatim in each file listed, in this same change."
    )


def test_no_file_invents_a_floor_the_operator_never_reads() -> None:
    """``your floor is`` is the tell of the invented example, and no gate says it.

    The positive test above pins the files that already quote the real string; it cannot see a
    NEW file inventing a new example, which is how the first six got written. This is the cheap
    half of that population (rule 145): the invented family all reached for "your floor is N"
    where every real string names the bar in the operator's own words ("past the 3 years it has
    to sit unwatched first", "below the 7.5 you keep"). A gate that legitimately starts saying
    this must retire the ban in the same change.
    """
    haystacks = [*_source_files_to_scan(), REPO / "README.md", REPO / "manual" / "features.mdx"]
    offenders = [
        f"{path.relative_to(REPO)}:{n}"
        for path in haystacks
        if path.is_file() and path != SELF
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
        if "your floor is" in line
    ]
    assert not offenders, (
        "no gate emits 'your floor is'; quote a real detail from src/reaper/engine/gates.py:\n"
        + "\n".join(offenders)
    )


#: The product tagline. One fact, stated on every surface that introduces Reaper.
TAGLINE = "Grave decisions, clearly explained"

#: The surfaces that state it. Written from the spec rather than scanned, because the slot is
#: not the fact: the recovery card fills the same ``auth-tagline`` element with "Single-use
#: admin access", which names that card and is correct, so a scan of the slot would demand the
#: tagline in the one place it does not belong.
TAGLINE_SITES = (
    "README.md",
    "frontend/src/App.tsx",
    "frontend/src/components/Login.tsx",
    "website/docusaurus.config.ts",
    "manual/index.mdx",
    "src/reaper/main.py",
    "scripts/gen_screenshot_mockup.py",
)

#: What the sign-in card said instead, until it was the only surface saying anything else.
RETIRED_TAGLINE = "Explainable pruning for Plex"


def test_every_surface_states_the_same_tagline() -> None:
    """The words under the mark are the same words wherever the app introduces itself.

    Rule 144. The masthead, the README, the manual's site header and the API's own description
    all carried this string; the sign-in card carried its own, and drifted alone for as long as
    nobody saw two of them at once. Each surface is written by someone reading a different one.

    A tagline is retired by editing this constant, which fails here naming every file still on
    the old words.
    """
    absent = [
        site for site in TAGLINE_SITES if TAGLINE not in (REPO / site).read_text(encoding="utf-8")
    ]
    assert not absent, (
        f"these surfaces no longer state the tagline {TAGLINE!r}:\n"
        + "\n".join(f"  {site}" for site in absent)
        + "\nSay the same words on every one, in this same change."
    )


def test_the_tagline_sites_all_exist() -> None:
    """A site renamed out from under the list above drops silently out of the guard.

    ``read_text`` would raise there, which fails for the wrong reason and reads as a broken
    test rather than a missing surface (rule 118).
    """
    gone = [site for site in TAGLINE_SITES if not (REPO / site).is_file()]
    assert not gone, (
        "TAGLINE_SITES names files that are not there; re-point it at where each surface "
        f"moved to: {gone}"
    )


def test_no_surface_keeps_the_retired_tagline() -> None:
    """The positive test cannot see a NEW surface inventing its own words.

    This is the cheap half of that population (rule 145): it catches the one wording already
    known to have drifted, including a revert, and it costs one grep.
    """
    haystacks = [
        *_source_files_to_scan(),
        REPO / "README.md",
        *(REPO / "manual").rglob("*.mdx"),
    ]
    offenders = [
        f"{path.relative_to(REPO)}:{n}"
        for path in haystacks
        if path.is_file() and path != SELF
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
        if RETIRED_TAGLINE in line
    ]
    assert not offenders, (
        f"{RETIRED_TAGLINE!r} was retired; the tagline is {TAGLINE!r}:\n" + "\n".join(offenders)
    )


def test_archived_docs_declare_they_are_frozen() -> None:
    """Every file in ``docs/history/`` says so in its own banner.

    An archived doc that reads like a live one is worse than no doc: the review it holds was
    remediated, so its "still open" claims actively mislead whoever reads it next.
    """
    offenders = [
        str(p.relative_to(REPO))
        for p in sorted(HISTORY.rglob("*.md"))
        if "FROZEN" not in "".join(p.read_text(encoding="utf-8").splitlines(keepends=True)[:30])
    ]
    assert not offenders, "archived docs must open with a FROZEN banner:\n" + "\n".join(offenders)


def test_live_docs_carry_no_unresolved_placeholders() -> None:
    """A live doc never ships a ``TBD``.

    The retired plan carried two unresolved ``(dev @ TBD)`` commit placeholders for days.
    Placeholders in ``docs/history/`` are fine: that file is a record of what was written then.
    """
    offenders = [
        f"{p.relative_to(REPO)}:{n}"
        for p in _live_docs()
        for n, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1)
        if "TBD" in line
    ]
    assert not offenders, "resolve or remove these placeholders:\n" + "\n".join(offenders)


def test_docs_referenced_from_code_exist() -> None:
    """A ``docs/…`` path named in code resolves to a real file.

    Rule 64: removing a surface removes its whole supply chain. Moving a doc without updating
    the comments that cite it leaves a reader chasing a path that is not there.
    """
    ref = re.compile(r"docs/[\w./-]+\.md")
    dangling: list[str] = []
    for path in [*_code_files(), REPO / "pyproject.toml"]:
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            for match in ref.finditer(line):
                if not (REPO / match.group(0)).is_file():
                    dangling.append(f"{path.relative_to(REPO)}:{lineno} -> {match.group(0)}")
    assert not dangling, "these doc paths do not exist:\n" + "\n".join(dangling)


#: A dotted citation of one of this repository's own symbols, as prose writes it:
#: ``api.review._chip``, ``services.snapshot.build_facts``, ``engine.gates.PROTECT``. Anchored on
#: the four layered packages plus ``db`` and ``auth``, because those are the names that appear
#: bare in a comment; a fully-qualified ``reaper.api.review._chip`` matches too, since the
#: pattern is not anchored at a word start on the left.
#: A leading ``/`` is what tells a citation from a URL or a file path, and nothing else does:
#: ``https://api.github.com``, ``https://api.radarr.video/v1/…`` and ``frontend/src/api.test.ts``
#: are all shaped exactly like ``api.review._chip``, and two of the three sit in the same double
#: backticks this repository cites code with. So the pattern refuses a match preceded by a slash.
#: The lookbehind refuses `/` and a word character but NOT a dot, so a fully-qualified
#: `reaper.api.review._chip` matches on its `api.` segment. Adding `.` to that class reads as
#: tighter and silently drops every `:func:`reaper.…`` cross-reference in the tree, which is the
#: spelling both pre-existing stale citations this guard first caught were written in.
_DOTTED_SYMBOL = re.compile(
    r"(?<![/\w])(api|services|clients|engine|db|auth)((?:\.[a-z_][\w]*)+)\.([A-Za-z_]\w{1,})\b"
)


def test_a_dotted_symbol_citation_resolves_to_a_real_symbol() -> None:
    """A comment naming ``package.module.symbol`` names one that exists.

    Rule 64's supply chain, for the citation form no other guard covers.
    ``test_docs_referenced_from_code_exist`` above does this for a ``docs/`` path; nothing did it
    for a symbol, so splitting a module left every comment pointing at its old address, and the
    only thing that found them was `docs/SIMPLIFICATION_PLAN.md` happening to warn that they
    existed. Splitting the API routes module into five moved **39** of these across 26 files, and
    the plan's own first estimate of that population was "roughly ten".

    **What it catches, stated as a bound rather than a boast.** A retired module named any of the
    three ways prose names one (a path, a bare module name, a dotted package path), and a
    symbol that is not reachable from the module cited. **What it does not catch: a symbol that
    moved to a sibling while the old module still imports it.** ``api.simulate._replayed_evidence``
    resolves green here, because `simulate` imports that name from `review` for its own use, and
    the guard cannot tell an import kept for use from one kept by accident. A stricter
    "defined here" test was written and withdrawn: it flags every monkeypatch target in the suite
    (`services.snapshot.utcnow` is imported into `snapshot`, which is exactly why patching it
    works), so it trades this hole for a much larger false-positive class.

    Deliberately not a count (rule 145). The population is every comment in the tree and it moves
    with ordinary writing, so a number here would be bumped without being read. What cannot drift
    is whether each one resolves.
    """
    import importlib
    import inspect

    #: A dotted name ending in one of these is a filename, not a symbol: `api.types.gen.ts`.
    suffixes = (".ts", ".tsx", ".py", ".md", ".mdx", ".json", ".css", ".html", ".yml", ".yaml")
    #: `docs/SIMPLIFICATION_PLAN.md` is exempt, and it is the one document that has to be. Its
    #: finding bodies quote the tree as it stood *before* each change, with `> Corrected:` and
    #: `Landed` blocks layered on top rather than edited in — so a citation that no longer
    #: resolves is often the record working, not rot. Re-pathing them against today's tree would
    #: destroy the history the plan exists to keep (its own preamble says so of `refuted.md`).
    exempt = {REPO / "docs" / "SIMPLIFICATION_PLAN.md"}
    #: This file names the retired module to DECLARE it, in `retired_modules` and in the prose
    #: explaining why the tombstone exists, so the tombstone check skips its own declaration.
    #: Scoped to that check alone -- the dotted check below still reads this file.
    declares_the_tombstone = REPO / "tests" / "test_repo_hygiene.py"

    #: Every module under `src/reaper/`, to check the OTHER two spellings prose uses for one.
    #: `api/routes.py` (a path) and `routes._chip` (a bare module name) are cited as often as the
    #: dotted form, and both survived the sweep this test was written for: 25 citations across
    #: `src/`, `tests/`, `frontend/src/`, a rules file and `CLAUDE.md`, found by a reviewer rather
    #: than by the guard, because the pattern above requires a package prefix and refuses a
    #: leading slash. A guard covering one spelling of three is rule 145's failure wearing the
    #: shape of the thing it checks.
    modules = {
        str(q.relative_to(REPO / "src" / "reaper")) for q in (REPO / "src" / "reaper").rglob("*.py")
    }
    #: The retired module's OTHER two spellings, tombstoned rather than derived. A "does this
    #: path exist" check cannot be general here: prose legitimately names files the tree does not
    #: have -- `api/deps.py` is phase 8's planned module, `engine/requester.py` and
    #: `engine/custom_gate.py` are proposals -- so a derived check flags the plan for planning.
    #: What is NOT legitimate is naming a module that used to exist and does not, which is
    #: exactly what a split leaves behind, and that is a list of one line per retirement.
    #: The bare form is undecidable in prose on its own -- `routes._chip` and a local
    #: `result._asdict` are the same shape, and one of the twelve real sites
    #: (`engine/explanation.py`) was not even in backticks -- so this is keyed on the retired
    #: name rather than derived. One entry per retirement: package, then module.
    retired_modules = {"routes": "api"}
    names = "|".join(retired_modules)
    packages = "|".join(dict.fromkeys(retired_modules.values()))
    cites = re.compile(rf"(?<![\w/.])(?:(?:{packages})/)?({names})\.(py\b|_?[A-Za-z]\w*)")
    assert modules, "the module walk found nothing, so every check below passes vacuously"

    dangling: list[str] = []
    for path in [p for p in (*_code_files(), *_live_docs()) if p not in exempt]:
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if path != declares_the_tombstone:
                for m in cites.finditer(line):
                    dangling.append(
                        f"{path.relative_to(REPO)}:{lineno} -> {m.group(0)} "
                        f"({m.group(1)}.py was retired; it is now several modules)"
                    )
            for match in _DOTTED_SYMBOL.finditer(line):
                if match.group(0).endswith(suffixes):
                    continue
                package, middle, symbol = match.groups()
                # Resolve by importing the longest prefix that IS a module, then walking what is
                # left with getattr. A citation is not always module-then-symbol: `httpx2` in
                # `clients.base.httpx2.AsyncHTTPTransport` is an alias bound inside a module, and
                # a monkeypatch target reads the same way. Splitting on the last dot instead
                # would flag every one of those as a missing module.
                parts = f"reaper.{package}{middle}.{symbol}".split(".")
                mod, depth = None, 0
                for i in range(len(parts), 1, -1):
                    try:
                        mod = importlib.import_module(".".join(parts[:i]))
                        depth = i
                        break
                    except ImportError:
                        continue
                if mod is None:
                    dangling.append(
                        f"{path.relative_to(REPO)}:{lineno} -> {match.group(0)} "
                        f"(no module under reaper.{package})"
                    )
                    continue
                rest = parts[depth:]
                if rest:
                    target: object | None = mod
                    for attr in rest:
                        target = getattr(target, attr, None)
                        if target is None:
                            break
                    if target is not None:
                        continue
                    symbol = rest[-1]
                    module = ".".join(parts[:depth])
                else:
                    continue
                # A method is cited as ``services.executor.execute``, without its class, because
                # the module has one obvious owner and the prose reads better for it. Accept the
                # symbol anywhere in the module's own classes.
                on_class = any(
                    inspect.isclass(member)
                    and member.__module__ == module
                    and hasattr(member, symbol)
                    for member in vars(mod).values()
                )
                if not on_class:
                    dangling.append(
                        f"{path.relative_to(REPO)}:{lineno} -> {match.group(0)} "
                        f"({symbol} is not in {module})"
                    )
    assert not dangling, (
        "these dotted citations do not resolve. A comment naming a symbol is part of that\n"
        "symbol's supply chain (rule 64): when it moves, the citation moves with it in the same\n"
        "change. Re-path each, or delete the citation if the thing it named is gone:\n  "
        + "\n  ".join(dangling)
    )


def test_live_docs_do_not_restate_the_numbered_rules() -> None:
    """The numbered rules have exactly one home: ``CLAUDE.md`` and ``.claude/rules/``.

    Two review passes each carried their own "Agent Rules" section, and those wordings were
    deliberately changed when they were merged into the rules files. A second copy in a live
    doc is a copy that will disagree.
    """
    heading = re.compile(r"^#+\s.*agent rules", re.IGNORECASE)
    offenders = [
        f"{p.relative_to(REPO)}:{n}"
        for p in _live_docs()
        for n, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1)
        if heading.match(line)
    ]
    assert not offenders, "the rules live in .claude/rules/, not in docs/:\n" + "\n".join(offenders)


# Every notice the app renders, counted once. Rule 145: the assertion below cannot tell a
# notice that complies from one that dropped out of the walk, and reads green for both.
#
# 108, and it does not line up with the 109 hand-rolled sites this replaced -- the two figures
# mean different things and are deliberately not derived from each other. The 109 is a fact about
# the past; this is a count of what is in the tree now, and several things moved it: the two
# draft-refusal notices were byte-identical twins in Settings and PolicyEditor and now render
# through the single Notice inside ``SwitchConfirm`` (rule 18); ``ReapPlan``'s plan loader was
# missed by the sweep entirely, because the ban could not parse a ternary ``className``; and the
# About, Jobs and Notifications panels each grew a second hand-rolled notice while this branch was
# in flight, when their failed-read handling was split into a never-loaded case and a stale case.
# Then 109: the service form's malformed-external-URL complaint became a notice of its own beside
# the box it is about, instead of a sentence written into the form's shared error slot 150 lines
# below it, which is where a failed save and a failed connection test also land (#174, rule 42).
# Then 112: the Plex panel's watch-history group added three at once, which is one group's full
# set -- a never-loaded error, an action failure, and a standing warning that says what pressing
# the control costs.
# Then 114: the why-panel's per-title twin of that control (#275) -- a standing warning carrying
# the button, and the action failure beside it.
# Then 125: telling the operator BEFORE the button what the execute route refuses after it
# (#383) -- one on the wizard's finish panel and one on the Reap page, each with the
# unreadable-setup branch beside it (rule 17/36) -- plus the wizard's restore door, whose modal
# says so too when it cannot tell whether a restore is already armed. The restore card's own
# four moved out of Settings into `RestoreCard.tsx` and are not part of that bump.
# Then 127: the armed restore card, which had a notice and no way to contradict it (#386). It
# gained the state after `Restart now` is pressed, and the failure slot that state made
# unavoidable -- both of the armed card's buttons can be refused by the server, and until now
# neither refusal rendered anywhere at all.
# Then 128: the wizard's scan step, which offered "Run first scan" whatever was connected. The
# start route answers 200 either way and the refusal is raised inside the detached task, so the
# operator watched a spinner for a scan that could never run and was then sent to Settings, which
# is behind the wizard they had not left. The notice carries the way back instead (rule 42).
# Then 132: the service editor and the wizard's Connect step, on the change that made a service
# prove its connection before it can be saved. Three of the four are states that could not arise
# before -- a folder list the test could not read (said apart from an instance that genuinely has
# none), the same for a Seerr portal's services, and the close guard's own sentence, which exists
# because `canClose` is a mute gate and a dismissal that silently does nothing is worse than one
# that says why. The fourth is a removal refused on the Connect step, which is new because the
# step had no Remove until now.
# Then 135, from the review of that change: a folder probe that failed while the grid it would
# have refreshed is still on screen (and its Seerr twin), and a Plex library list whose SYNC
# failed -- three states that each used to render as a positive claim about a service or a
# server nobody reached.
# Then 136 -> 137: Settings -> Lists, whose unreadable answer is a positive claim about every
# protection list at once if it stays silent (#475).
# Then 137 -> 142, the rest of that screen: a check that would not run (on the row whose button
# started it, rule 42), a Plex that could not be reached at all so no row can say why, a removal
# that was refused, and the add/edit form's own save failure and its switched-off warning.
# Then 141 -> 144, from that screen's UI review: a failed "Check all now", which on an install
# with no migrated rows had no sink at all and went back to rest saying nothing; the Review
# page's incomplete-scan line, which was a bare styled span so amber was its only severity
# signal; and a retired protection still switched on, which refuses every scan while reading as
# an ordinary healthy one.
# Then 144 -> 143: the policy editor's three hand-written recovery notices became two renders
# over `REPAIR_NOTICES`, one per placement, so a repair kind added later gets its sentence from
# the map instead of a fourth copy of the same JSX (#516). The population shrank; the number of
# notices an operator can see did not.
# Then 143 -> 142: the why panel's and the Scales panel's loading/error fallbacks became one
# `PanelFallback` the two hand three strings (W11-24). One fewer call site, the same two notices
# on screen.
# Re-derive it by running the test, never by arithmetic on this comment.
_EXPECTED_NOTICES = 142


def _shipped_tsx() -> list[Path]:
    """The .tsx the app actually ships: no tests, no test harness."""
    return [
        p
        for p in FRONTEND_SRC.rglob("*.tsx")
        if ".test." not in p.name and "test" not in p.relative_to(FRONTEND_SRC).parts
    ]


def test_every_notice_goes_through_the_one_component_that_announces_it() -> None:
    """A hand-rolled ``.notice`` is a notice no screen reader will read out (#155).

    There were 109 of these written by hand and seven live regions in the whole frontend, and
    not one of the seven was a notice. Nothing the app said after an operator pressed something
    -- a failed save, a refused switch, a wrong password on the control that arms deletion --
    was announced at all. ``Notice`` owns ``role="alert"`` so the answer is written once
    (rule 18); this is what stops the 110th copy being written by hand and shipping mute.

    Rule 144 is why the *count* is here rather than only the ban: the number 109 appears in
    issue #155, in ``Notice.tsx``'s own docblock and in ``Notice.test.tsx``. Deriving it in one
    place and leaving the others to drift is the failure that rule describes, so the message
    below names them.
    """
    component = FRONTEND_SRC / "components" / "Notice.tsx"
    # Read the WHOLE ``className`` value -- a quoted literal or a ``{...}`` expression -- and then
    # every quoted run inside it. A ternary and a template literal are ordinary ways to write this
    # attribute, and the quote-anchored pattern this replaces could parse neither: it required a
    # quote immediately after ``className=`` or after ``{``. ``ReapPlan``'s plan loader sat in that
    # blind spot as ``className={runPending ? "help" : "notice notice-error"}`` and shipped mute
    # while this test passed. The count below could not see it either -- a site that was never
    # converted is absent from both halves -- which is rule 145 exactly: the ban read green over
    # the one thing it existed to catch.
    #
    # The bare ``notice`` token, not a substring of one: ``budget-notice`` and ``kept-notice``
    # are layout classes that ride ON a Notice via its ``className`` prop, and a ``\bnotice\b``
    # match counts them as offenders because the hyphen is a word boundary.
    #
    # ``_strip_prose`` is deliberately NOT used here. It drops every backticked span, which in a
    # .tsx file is a template literal rather than prose, so it would blind this check to one of
    # the two forms it was just widened to catch. Whole-line comments are skipped instead.
    attr = re.compile(r"className=(\{(?:[^{}]|\{[^{}]*\})*\}|\"[^\"]*\"|`[^`]*`)")
    quoted = re.compile(r"\"([^\"]*)\"|`([^`]*)`")
    offenders = list(
        dict.fromkeys(
            f"{p.relative_to(REPO)}:{n}"
            for p in _shipped_tsx()
            if p != component
            for n, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1)
            if not line.lstrip().startswith(("//", "*", "/*", "{/*"))
            for m in attr.finditer(line)
            for q in quoted.finditer(m.group(1))
            for g in q.groups()
            if g and "notice" in g.split()
        )
    )
    assert not offenders, (
        "these write a .notice class by hand, so they render with no role and are silent to a\n"
        "screen reader. Use <Notice tone=...>, which carries role=alert; pass `standing` only\n"
        "for text that is part of the page rather than a reaction to something:\n"
        + "\n".join(offenders)
    )

    used = sum(
        len(re.findall(r"<Notice[\s/>]", p.read_text(encoding="utf-8")))
        for p in _shipped_tsx()
        if p != component
    )
    assert used == _EXPECTED_NOTICES, (
        f"expected {_EXPECTED_NOTICES} <Notice> call sites, found {used}.\n"
        "If you ADDED or REMOVED a notice, bump _EXPECTED_NOTICES. Do NOT touch the 109 in\n"
        "Notice.tsx's docblock and Notice.test.tsx: that is how many hand-rolled notices this\n"
        "replaced, a fact about the past, not a count of what is here now (rule 144).\n"
        "If you added nothing, one dropped out of the walk: the ban above passes happily on\n"
        "a notice it can no longer see."
    )


# Every notice that opts OUT of announcing itself. Rule 145 again, for the other direction: the
# count above proves a notice exists, and says nothing about whether it speaks.
#
# 16. Eight were the sweep in #375/#376 -- six notices that were page furniture and interrupted
# anyway (the armed restore card, the Reap page's expired-spares prompt, its two stale-plan
# notices, the Plex trash warning on both its surfaces, and the why-panel's "Kept to be safe"),
# plus the log's two failure notices, which a 2s poll re-announced on every flap.
# Then 31: #394 swept the rest of the tree against the sharper question, which is not what a
# notice's mount condition READS but what refetches the query under it -- `main.tsx` turns
# `refetchOnWindowFocus` off app-wide, so a read moves on a mount, an interval, or an
# invalidation, and only the first two reach a notice with nothing pressed. Fifteen did: a run
# started on another device, a scheduled scan crashing or finishing degraded (three sites off one
# 15s poll), five facts about the install that are true on first paint, three load-time recovery
# flags, and the password form's live complaint, whose `{pw.length} so far` mutated inside a live
# region on every keystroke.
# Then 34: the policy editor's other three readers of `["validate", debounced]`, which is keyed on
# the draft and so refires as the operator types -- the same query whose `WarnBlock` notices were
# already `standing` for that reason, and rule 72 for the two that were not.
# Then 35: About's dev-build banner, a fact about the install that is true on first paint
# and unchanged for the process's whole life.
# Then 36: the Review page's incomplete-scan line, which became a `Notice` so its severity is
# not amber alone. It is the age of the snapshot the queue below is built from, so it is page
# furniture for as long as that snapshot is the one on hand; the scan that produces it
# announces itself from `ScanBar`, where the transition actually happens.
# Then 36 -> 35: the policy editor's two standing recovery notices became one render over the
# `top` half of `REPAIR_NOTICES`. Still standing, and for the same reason -- a repair is carried
# by the fetch, so it is the state of the page from its first paint (#516).
# Then 36: the setup wizard's password step, the second drawing of `AdminPasswordForm` and the
# one place its live complaint was still announced. Same `{pw.length} so far` inside a live
# region on every keystroke, on the form that sets the key arming deletion; the sibling above has
# been standing since #394 and this copy was never swept (rule 72).
# Re-derive it by running the test, never by arithmetic on this comment.
_EXPECTED_STANDING = 36

# ``standing`` as a JSX attribute, never as a substring of a class name or a word in prose.
_STANDING_ATTR = re.compile(r"(?<![\w-])standing(?![\w-])")

# How far above a call site its justification may sit. Wide enough for the comment to precede a
# multi-line mount condition, narrow enough that the next notice down cannot borrow it.
_JUSTIFY_WINDOW = 12


def _notice_openings(text: str) -> list[tuple[int, str]]:
    """Every ``<Notice`` opening tag, as ``(line number, attribute text)``.

    Reads the whole tag rather than anchoring on a delimiter, which is rule 147's instruction
    after the quote-anchored ban shipped blind to a ternary. The tag ends at the first ``>`` at
    brace depth zero and outside a string, so ``key={b.key}``, ``className={cx ? a : b}`` and a
    tag broken across five lines all read the same way.
    """
    out: list[tuple[int, str]] = []
    for match in re.finditer(r"<Notice(?![\w])", text):
        i, depth, quote = match.end(), 0, ""
        while i < len(text):
            ch = text[i]
            if quote:
                if ch == quote:
                    quote = ""
            elif ch in "\"'`":
                quote = ch
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
            elif ch == ">" and depth == 0:
                break
            i += 1
        out.append((text.count("\n", 0, match.start()) + 1, text[match.end() : i]))
    return out


def test_the_matcher_for_standing_reads_every_spelling_the_tree_uses() -> None:
    """Rule 147: the spellings a source-text scan accepts are written down and driven, both ways.

    The ban this sits beside shipped green over the one site it existed to catch, because a
    ternary ``className`` was a spelling its pattern could not parse. So the accepted and
    rejected forms are a table here rather than a claim in a comment, and the walk below is only
    as good as this.
    """
    accepted = [
        '<Notice tone="warn" standing>',
        '<Notice tone="warn" standing as="div">',
        '<Notice standing tone="error">',
        '<Notice key={b.key} tone="warn" standing>',
        '<Notice tone="warn" className="kept-notice" standing>',
        '<Notice\n  tone="warn"\n  standing\n>',
        '<Notice tone={bad ? "error" : "warn"} standing>',
        "<Notice tone={t} standing />",
    ]
    rejected = [
        '<Notice tone="warn">',
        '<Notice tone="warn" className="standing-room">',
        '<Notice tone="warn" className="restore-armed" as="div">',
        # The word inside the CHILDREN is not the attribute, so the tag reader must stop at `>`.
        '<Notice tone="warn">A standing restore is armed.</Notice>',
        '<Notice\n  tone="warn"\n  as="div"\n>',
    ]
    for spelling in accepted:
        opens = _notice_openings(spelling)
        assert len(opens) == 1, f"did not read one tag out of {spelling!r}"
        assert _STANDING_ATTR.search(opens[0][1]), f"missed `standing` in {spelling!r}"
    for spelling in rejected:
        opens = _notice_openings(spelling)
        assert len(opens) == 1, f"did not read one tag out of {spelling!r}"
        assert not _STANDING_ATTR.search(opens[0][1]), f"read `standing` into {spelling!r}"


def test_every_silent_notice_says_why_it_is_silent() -> None:
    """``standing`` takes a notice out of the screen reader's path, so it argues for itself (#375).

    ``Notice`` announces by default and the flag is the opt-out, which makes the flag the only
    thing standing between an operator and a message they will never hear. Six notices were page
    furniture and interrupted anyway; the reverse mistake is the one this catches, because it is
    the one that is silent in both the app and the diff. ``Notice.tsx`` asks for the reason in a
    comment at the call site, and prose cannot bind an author who never read it.

    What this proves is that a reason was WRITTEN, never that it is true -- whether a mount
    condition is really furniture is a judgment no regex makes. It is deliberately the cheap half:
    an author who has to name the condition out loud has already done the thinking that the six
    skipped. The window can also be satisfied by a neighbour's comment where two standing notices
    sit within ``_JUSTIFY_WINDOW`` lines, which is a false pass and not a false failure.
    """
    component = FRONTEND_SRC / "components" / "Notice.tsx"
    silent: list[str] = []
    unjustified: list[str] = []
    for path in _shipped_tsx():
        if path == component:
            continue
        lines = path.read_text(encoding="utf-8").splitlines()
        for lineno, attrs in _notice_openings("\n".join(lines)):
            if not _STANDING_ATTR.search(attrs):
                continue
            site = f"{path.relative_to(REPO)}:{lineno}"
            silent.append(site)
            above = lines[max(0, lineno - 1 - _JUSTIFY_WINDOW) : lineno - 1]
            if not any("standing" in line for line in above):
                unjustified.append(site)
    assert not unjustified, (
        "these pass `standing`, so a screen reader never announces them, and nothing above them\n"
        "says why. Name the mount condition in a comment at the call site: what makes this text\n"
        "part of the page rather than a reply to something the operator pressed.\n"
        + "\n".join(unjustified)
    )
    assert len(silent) == _EXPECTED_STANDING, (
        f"expected {_EXPECTED_STANDING} `standing` notices, found {len(silent)}.\n"
        "If you silenced a notice or gave one its voice back, bump _EXPECTED_STANDING and say\n"
        "which in the comment above it. If you changed neither, one dropped out of the walk and\n"
        "the justification check above is passing on a site it can no longer see (rule 145).\n"
        + "\n".join(silent)
    )


# Every handle on a READ's failure the shipped app binds, by the file that binds it.
#
# The sweep for branches that test a query's failure without asking whether the value is still in
# hand has now run three times -- #140, then #166/#181, then the nine deferred into #190 -- and
# every pass found sites the previous one missed, including one inside the very file being edited.
# Nothing counted the population, so the next one was caught by the next human reviewer or not at
# all (#197). This is that count.
#
# **What a handle is.** One `error` or `isError` a component can branch on, resolved to the hook
# that produced it: `const { isError } = useQuery(…)`, `const { error: vocabError } = useQuery(…)`,
# and `libraries.isError` where `libraries` came from a `useQuery`. A file with three components
# each destructuring `isError` has three.
#
# **What is in the walk, and what is not.** Reads are in; a mutation's `error` is an action
# failure, a different population with a different rule (rule 42's `.notice.notice-error`), and is
# out. The two lists below are the whole of what the matcher will accept, and an initializer
# spelled `use*` that is in neither FAILS the test rather than being guessed at either way -- a new
# read hook silently landing in the mutation bucket is exactly how a site goes missing from a walk
# that reads green (rules 145, 147).
#
# **Roughly half of these are deliberately undivided, and that is the point of counting rather than
# banning.** Every safety indicator reads an unreadable state as *unknown* on purpose, which is
# fail-closed and the opposite of the #190 fix: `App`'s two `useSafety` gates, `DeletionToggle`,
# `ReapPlan` and `ReapConfirm`'s safety reads, both `usePlexTrash` call sites, `ReapBreakdown`'s
# unknown-size allowance AND its ledger (which states delete counts, so a held one would be a stale
# number shown as current), `queueSettings`' allowance, and `PolicyEditor`'s simulator column,
# which argues the same thing in as many words. A ban would have to exempt all of them; a count
# does not care which way a site resolved, only that nobody added one without deciding.
_QUERY_FAILURE_HANDLES = {
    "frontend/src/App.tsx": 7,
    # The seven settings panels below held one entry between them, ``Settings.tsx: 8``, until the
    # file became a shell and each panel got its own module. Nothing about any branch changed:
    # the 8 redistribute exactly, and Settings.tsx leaves the walk because the shell binds no
    # read at all.
    "frontend/src/components/AboutPanel.tsx": 1,
    "frontend/src/components/BackupPanel.tsx": 1,
    "frontend/src/components/DeletionToggle.tsx": 1,
    "frontend/src/components/Fairness.tsx": 1,
    "frontend/src/components/GeneralPanel.tsx": 1,
    "frontend/src/components/JobsPanel.tsx": 2,
    # Whether each protection list is still protecting anything (#475). Undivided on purpose,
    # like the safety reads above: this screen exists so an operator can tell a list that
    # stopped working from one that is simply not on a title's side, so an unreadable answer
    # must say it could not tell them. Keeping a previous good answer on screen would state
    # that the lists are fine at the one moment nobody knows whether they are, and that is the
    # direction a keep list fails in. Both arms are pinned in `ListsPanel.test.tsx`.
    #
    # 1 -> 2 when the screen gained the list DEFINITIONS beside the membership. Same question
    # asked of the second read, and it is the read that decides whether a row exists at all:
    # failing it silently would render a page with no rows and an Add button, which reads as
    # "you have no lists" to an operator who has several. Both reads share one failure branch
    # for that reason, and each is driven into it on its own in the tests.
    "frontend/src/components/ListsPanel.tsx": 2,
    "frontend/src/components/LogsPanel.tsx": 1,
    "frontend/src/components/NotificationsPanel.tsx": 1,
    # 5 -> 6 when the library list moved to ``usePlexLibraries``. No branch here changed: the
    # panel's JSX is untouched, and the extra handle is the walk seeing the bag's two members
    # where a directly-bound ``useQuery`` had been one. Counted rather than excused, because
    # the population is the thing this pins.
    "frontend/src/components/PlexPanel.tsx": 6,
    "frontend/src/components/PolicyEditor.tsx": 4,
    "frontend/src/components/PolicyRuleEditors.tsx": 3,
    "frontend/src/components/ReapBreakdown.tsx": 2,
    "frontend/src/components/ReapConfirm.tsx": 2,
    # 4th: the pre-flight read that says what would turn a real run away (#383). Deliberately
    # undivided in the same way as the safety reads above -- an unreadable setup status is
    # UNKNOWN, and the page says so rather than staying silent, because silence there reads as
    # "nothing is missing" over a run the server is about to refuse.
    "frontend/src/components/ReapPlan.tsx": 4,
    "frontend/src/components/ReviewQueue.tsx": 3,
    # 4 render branches, plus 2 in the save handler (#204). Those two are neither of the
    # questions the docstring below names: they ask "may I PRUNE against this list", where a
    # failed read means the list is merely out of date and pruning would delete a stored
    # mapping nothing confirmed is gone. The render branches beside them already keep their
    # grid and say it may be stale, which is why `.data` alone could not answer this.
    # Moved out of App.tsx with the component rather than added: the banner's amber
    # "we could not look" branch is the same handle it always had, now in its own module so
    # the wizard states the regime from the same declaration.
    "frontend/src/components/SafetyBanner.tsx": 1,
    "frontend/src/components/SectionNav.tsx": 1,
    "frontend/src/components/SecurityPanel.tsx": 1,
    # 6 -> 7: the library pickers now consult the SYNC's failure as well as the query's. A read
    # that lands empty while the sync that would fill it fails is the ordinary answer when Plex
    # is not linked at all, and with only the query consulted the panel stated as fact that the
    # server holds no libraries of this kind -- about a server nobody reached.
    "frontend/src/components/ServiceModal.tsx": 7,
    "frontend/src/components/ServicesPanel.tsx": 1,
    "frontend/src/components/SetupConnectStep.tsx": 1,
    # 1 -> 2: the same sync failure, on the step that renders the Libraries grid. It drew an
    # empty grid and said nothing at all, so "no libraries" and "we never got to look" were one
    # picture (rule 93).
    "frontend/src/components/SetupPlexStep.tsx": 2,
    # Whether a restore is already armed. Never-loaded only: the modal mounts on the press and
    # holds no earlier good answer to keep, and falling through to the idle card would invite an
    # upload on top of a restore already staged.
    "frontend/src/components/SetupRestoreModal.tsx": 1,
    "frontend/src/components/SetupWizard.tsx": 1,
    "frontend/src/components/queueSettings.tsx": 1,
    # The policy editor's probe: what the engine says a rule would do at a value the operator
    # is dragging. A READ, and one that deliberately does NOT keep its content -- the hook
    # nulls the answer on failure rather than leaving the last one on screen, because the
    # last one is about a value the operator has already moved past, and a points figure
    # under a slider reads as being about where the slider is now.
    #
    # It is one handle rather than two because the hook answers the read-or-action question
    # once and hands down a `failed` boolean, so `PolicyEditor` stays at 4: its branch is on
    # the derived flag and this walk never sees it. That is the arrangement this population
    # is meant to encourage, not a gap in it -- the decision is made in one place instead of
    # re-made at every call site.
    "frontend/src/usePolicyProbe.ts": 1,
}

# The hooks that hand back a READ's failure. The last three wrap a ``useQuery`` in their own
# module, so the branch that acts on the failure is written at the CALL site and only this list
# reaches it.
_READ_HOOKS = {
    "useQuery",
    "useInfiniteQuery",
    "useSafety",
    "usePlexTrash",
    "useHoldsBackUnmeasured",
    # The second BAG, after ``useOverrideMutations`` below, and the first MIXED one: it hands
    # back ``{ libraries, sync }``, a query and the mutation that fills it when it has never
    # been filled. Filed as a read because the handle every call site branches on is the
    # query's -- "could we read your Plex libraries" -- and a failed refetch there leaves the
    # last good list in the pickers, which is exactly the keep-your-content case. Its mutation
    # half is not unclassified by omission: ``PlexPanel`` renders ``sync.error`` through the
    # action slot it already shared with ``saveLibraries``.
    "usePlexLibraries",
    # Wraps the general-settings query and returns the whole result, so `GeneralPanel` still
    # branches on `general.isError` and the number does not move.
    "useGeneralSettings",
}

# The hooks that hand back PAYLOAD and no failure handle at all, so a member of their result
# named ``error`` is the SERVER's word rather than a read that failed.
#
# ``ScanStatus.error`` is the case: the scan bar and the wizard both render "The scan hit a
# problem: {status.error}", which is a finished scan reporting what went wrong. The walk already
# refused to count that through ``_QUERY_PRIMITIVES``, while the query was declared inline and
# destructured; hoisting it into ``useScanStatus`` moved the same expression onto an unknown hook
# bound whole. Filing it as a read adds two handles for branches that do not exist (rule 141).
_PAYLOAD_HOOKS = {"useScanStatus"}

# The hooks whose failure is an action's, not a read's. Listed rather than assumed, so the walk
# fails on a hook it has never seen instead of quietly filing it here.
#
# ``useOverrideMutations`` is the tree's one BAG -- it hands back ``{ setOverride, clearOverride,
# refresh }`` and the call site reads ``setOverride.isError`` -- so it arrives here through the
# member branch of the walk rather than through a directly bound ``useMutation``. Both members are
# ``useMutation``, so a spare or a reap that fails is an action's failure. It is listed for the
# shape as much as the answer: a READ hook written this way is the one that would otherwise be
# invisible, and now it stops the run here until someone says which it is.
_ACTION_HOOKS = {"useMutation", "useOverrideMutations"}

# React Query's own hooks, whose result shape the walk already knows: a member other than
# ``error``/``isError`` is payload, never a nested failure handle. A custom hook's is unknown.
_QUERY_PRIMITIVES = {"useQuery", "useInfiniteQuery", "useMutation"}

_OBJECT_BINDING = re.compile(r"const\s+(\w+)\s*=\s*(use[A-Z]\w*)\s*[(<]")
_DESTRUCTURED_BINDING = re.compile(r"const\s*\{([^}]*)\}\s*=\s*(use[A-Z]\w*)\s*[(<]")


def _shipped_frontend_source() -> list[Path]:
    """The .ts and .tsx the app ships: no tests, no test harness, no ambient declarations."""
    return [
        p
        for p in sorted(FRONTEND_SRC.rglob("*.ts*"))
        if ".test." not in p.name
        and "test" not in p.relative_to(FRONTEND_SRC).parts
        and not p.name.endswith(".d.ts")
    ]


def _query_failure_handles() -> tuple[dict[str, int], set[str]]:
    """Read-failure handles per file, and every ``use*`` hook name the walk met.

    Both binding spellings the tree uses (rule 147): the whole result (``const libraries =
    useQuery(…)``, counted once per ``.error`` / ``.isError`` the same file goes on to read) and
    the destructure (``const { isError } = useSafety()``), including a rename (``error:
    vocabError``). Comments come out first, since several of them quote these very expressions.

    It does NOT resolve a handle passed into a function or through a prop -- ``NotInScanPanel``
    takes a plain ``error`` boolean from its parent, and the parent's own handle is what is
    counted. That is the right end to count from: the parent is where the query lives.
    """
    per_file: dict[str, int] = {}
    hooks: set[str] = set()
    for path in _shipped_frontend_source():
        text = _without_comments(path.read_text(encoding="utf-8"))
        name = str(path.relative_to(REPO))
        found = 0
        for match in _OBJECT_BINDING.finditer(text):
            binding, hook = match.group(1), match.group(2)
            # A payload hook's ``.error`` is the server's message on the value, so it is neither
            # counted nor left unclassified. Same exclusion the member branch below applies to
            # React Query's own hooks, for the same reason.
            if hook in _PAYLOAD_HOOKS:
                continue
            reads = [
                a
                for a in ("isError", "error")
                if re.search(rf"\b{re.escape(binding)}\.{a}\b", text)
            ]
            if reads:
                hooks.add(hook)
                found += len(reads) if hook in _READ_HOOKS else 0
        for match in _DESTRUCTURED_BINDING.finditer(text):
            inner, hook = match.group(1), match.group(2)
            for part in (p.strip() for p in inner.split(",")):
                if not part:
                    continue
                key, local = part.split(":", 1)[0].strip(), part.split(":", 1)[-1].strip()
                if key in ("error", "isError"):
                    hooks.add(hook)
                    found += 1 if hook in _READ_HOOKS else 0
                    continue
                # A BAG: ``const { health } = usePlexHealth()``, where the handle is a member of
                # the returned object and the ``useQuery`` lives in the hook's own module. Without
                # this the walk resolved nothing -- the member is not named ``error``, and the
                # hook's own file binds it to a name this file never mentions -- so such a read
                # was absent from the count AND from the unknown-hook arm, landing nowhere while
                # the gate stayed green. The tree already spells hooks this way.
                #
                # Only for a hook whose result shape this walk does not already know. React
                # Query's own hooks return payload under ``data``, and two of the tree's scan
                # lines rename it (``const { data: status } = useQuery``) and then read
                # ``status.error`` -- the SERVER's error message on the payload, not a handle.
                # Counting those would have moved the number for a read that does not exist.
                if hook in _QUERY_PRIMITIVES or hook in _PAYLOAD_HOOKS:
                    continue
                reads = [
                    a
                    for a in ("isError", "error")
                    if re.search(rf"\b{re.escape(local)}\.{a}\b", text)
                ]
                if reads:
                    hooks.add(hook)
                    found += len(reads) if hook in _READ_HOOKS else 0
        if found:
            per_file[name] = found
    return per_file, hooks


def test_every_query_failure_branch_is_counted() -> None:
    """A branch on a failed read is a decision, so a new one cannot arrive without one (#197).

    React Query keeps the last good value through a failed refetch and raises the failure beside
    it, so ``isError`` alone answers "did a read fail", never "is there still something to show".
    Half the sites in this tree want the first question (a safety indicator reading unknown, which
    is fail-closed) and half want the second (a panel keeping its content and saying it may be
    stale). Both are correct; picking without noticing there is a choice is not, and three sweeps
    running found sites the previous one missed.

    **This pins the population, not the shape.** It cannot tell a divided branch from an undivided
    one -- that would need the answer to a question only a human has -- so a file that swaps one
    kind for the other reads green here. What it does is make a new branch, or a deleted one, land
    as a failure with the classification comment above it in the message, where the previous gate
    (a count of ``<Notice>`` call sites) was a different population entirely and agreed with
    itself while disagreeing with the tree (rule 145).
    """
    per_file, hooks = _query_failure_handles()
    unknown = sorted(hooks - _READ_HOOKS - _ACTION_HOOKS - _PAYLOAD_HOOKS)
    assert not unknown, (
        "these hooks hand back an `error`/`isError` the walk has never seen, so it cannot say\n"
        "whether they are reads or actions and will not guess:\n  " + "\n  ".join(unknown) + "\n"
        "Add each to _READ_HOOKS (a read, whose failure leaves the last good value in hand),\n"
        "to _ACTION_HOOKS (a mutation, whose failure is an action's), or to _PAYLOAD_HOOKS (it\n"
        "returns the value alone, so `.error` on it is the server's message and not a handle)."
    )
    assert per_file == _QUERY_FAILURE_HANDLES, (
        "the query-failure population moved.\n"
        f"expected: {_QUERY_FAILURE_HANDLES}\nfound:    {per_file}\n"
        "A new one: decide which question it is asking before bumping the number here. If the\n"
        "surface should KEEP its content through a failed refetch, test `error && !data` and pair\n"
        "it with <StaleReadNotice what=... />, and pin both arms in a test: the never-loaded arm\n"
        "is why the branch exists and a fix that only deletes `isError` breaks it. If it is a\n"
        "safety indicator, reading unreadable as unknown is right and fail-closed; say so in a\n"
        "comment beside it, the way the sites named above do."
    )


# Every sentence in the shipped app that says the word "reload", by the file that renders it.
#
# A reload discards whatever is typed, staged or selected, and there is no ``beforeunload``
# handler anywhere in ``frontend/src`` to ask first -- so this advice is destructive exactly where
# there is something to destroy. #153 took it off the shared ``StaleReadNotice``; #195 took it off
# the eight hand-written siblings that render while a draft, a pasted secret or a bulk selection is
# on screen. What is left is here so the next one has to be classified rather than typed.
#
# Per file, and every entry is a deliberate keep:
#   AboutPanel.tsx (1)       the About read's never-loaded branch
#   BackupPanel.tsx (1)      the backup summary's never-loaded branch
#   Fairness.tsx (1)         NOT advice: the Refresh button's ``title``, "Reload requests and
#                            watch history". It is in the walk because the walk is of a word, and
#                            dropping it by hand is how a matcher starts lying about its own scope
#   GeneralPanel.tsx (1)     the general settings' never-loaded branch
#   JobsPanel.tsx (2)        two never-loaded branches, the upkeep jobs and the shelf status
#   NotInScanPanel.tsx (1)   a read-only panel with no draft, and now only on the arm where the
#                            list never landed (#190)
#   PlexPanel.tsx (1)        the panel's own never-loaded status read
#   PolicyEditor.tsx (1)     the policy's never-loaded branch, above no form
#   ReapBreakdown.tsx (1)    the ledger's refusal, which is undivided on purpose (#190)
#   ReapConfirm.tsx (1)      the not-armed branch, before the confirmation box exists
#   ReapPlan.tsx (1)         the plan loader. Not in #195's enumeration; see the note below
#   RestoreCard.tsx (3)      the stopping state -- the sentence, the button that performs it, and
#                            the call that button makes. The one site here where the advice is
#                            not about a failed read: it renders only after the operator pressed
#                            `Restart now` and the server took it, so the page is about to stop
#                            answering whatever anyone does. Nothing is on screen to lose -- the
#                            staged summary and the password typed against it went at the confirm,
#                            two states earlier -- and what comes back is a different database
#                            anyway, which is the point of pressing it (#386)
#   SecurityPanel.tsx (1)    the security settings' never-loaded branch
#
# The five settings panels above were one entry, ``Settings.tsx (6)``, until that file became a
# shell holding no read of its own. Same six branches, each on a read that never landed with
# nothing on screen to lose; only the file rendering each one is now named. The single entry said
# "above a form that never rendered", which was never true of all six -- About is read-only and
# the shelf-status branch sits above a status row.
#
# **#195's enumeration was not the whole population**, which is why this counts rather than
# trusting the issue: it named 8 to fix and 9 to leave, called that 15, and did not reach the reap
# sheet, the plan loader, the ledger refusal or the not-in-scan panel at all. #225 asked what those
# four cost, and the component tree answers it: ``<main>`` is a plain ternary on one ``view``
# state, so exactly one section is mounted. The plan loader, the ledger refusal and the not-in-scan
# panel all sit in a different arm from ``ReviewQueue``, so reaching them has already unmounted the
# queue and destroyed the selection -- their advice costs nothing the queue owned, and they stay.
# The reap sheet was the one that did not: it renders OUTSIDE ``<main>``, gated on ``reapSheetRun``,
# which the reap bar's View sets without touching ``view``, so it opens over a mounted queue by
# construction. Its line now points at the close the modal already has, and ``App.tsx`` is gone
# from this dict.
_RELOAD_ADVICE = {
    "frontend/src/components/AboutPanel.tsx": 1,
    "frontend/src/components/BackupPanel.tsx": 1,
    "frontend/src/components/Fairness.tsx": 1,
    "frontend/src/components/GeneralPanel.tsx": 1,
    "frontend/src/components/JobsPanel.tsx": 2,
    "frontend/src/components/NotInScanPanel.tsx": 1,
    "frontend/src/components/PlexPanel.tsx": 1,
    "frontend/src/components/PolicyEditor.tsx": 1,
    "frontend/src/components/ReapBreakdown.tsx": 1,
    "frontend/src/components/ReapConfirm.tsx": 1,
    "frontend/src/components/ReapPlan.tsx": 1,
    "frontend/src/components/RestoreCard.tsx": 3,
    "frontend/src/components/SecurityPanel.tsx": 1,
}

_BLOCK_COMMENT = re.compile(r"/\*.*?\*/", re.S)


def _without_comments(text: str) -> str:
    """``text`` with every block comment and every ``//`` run to end-of-line removed.

    Block comments first, because a ``{/* … */}`` in JSX wraps over many lines and none of the
    inner ones start with a marker a per-line skip could see -- which is the shape most of this
    tree's explanatory comments have, and several of them discuss reloads at length.
    """
    return _without_line_comments(_BLOCK_COMMENT.sub("", text))


def test_the_reload_advice_population_is_pinned_per_file() -> None:
    """Telling an operator to reload throws away their draft, so each one is deliberate (#195).

    Matches the bare word ``reload``, case-insensitively, in what is left of a shipped ``.tsx`` or
    ``.ts`` once comments are gone. Deliberately looser than the sentence it is about (rule 147):
    the tree spells the advice three ways -- "Reload to try again.", "Reload the page to try
    again." and "then reload this page." -- and the second of those WRAPS across two source lines in
    ``NotInScanPanel``, so a per-line match on the full phrase would have missed it. A word cannot
    wrap. The cost is that the walk also collects a Refresh button's tooltip, which is listed
    above rather than filtered out, because a matcher that quietly drops what does not fit stops
    being a count of anything.

    What this proves is that no site GAINED the advice and none of the fixed ones got it back. It
    says nothing about which branch inside a file renders it, so a file swapping one keep for a new
    one reads green here: the per-branch claims are pinned in the component tests.
    """
    found: dict[str, int] = {}
    for path in _shipped_frontend_source():
        n = len(re.findall(r"(?i)\breload", _without_comments(path.read_text(encoding="utf-8"))))
        if n:
            found[str(path.relative_to(REPO))] = n
    assert found == _RELOAD_ADVICE, (
        "the reload-advice population moved.\n"
        f"expected: {_RELOAD_ADVICE}\nfound:    {found}\n"
        "A new one: say what is on screen when it renders, and drop it if anything there is a\n"
        "draft, a staged file, a pasted secret or a selection -- a reload takes all four with no\n"
        "ask, since `frontend/src` has no `beforeunload` handler (grep: zero). Then add it here\n"
        "with that reasoning. One that went away: drop its entry."
    )


# Every "couldn't load" sentence the shipped tree renders, and the file rendering each one. The
# count above matches the WORD `reload` per file, so it is blind to two panels drifting apart on
# the sentence they print when a read never landed. That is what W11-36 is about: "Couldn't load
# these settings. Reload to try again." is written at four sites and "Couldn't load this page.
# Reload to try again." at two. Three more keys below hold two files each, and the finding named
# none of them.
#
# `PolicyEditor`'s "Couldn't load these settings." is the deliberate fifth copy and holds a key of
# its own. The distinction is per BRANCH rather than per panel, and that file carries both: at
# `:1618` the whole draft failed to read, so the workspace never rendered and there is nothing to
# lose, while `:2355` sits inside a mounted editor whose savebar may be holding unsaved edits, and
# a reload takes them with no ask (#195; `frontend/src` has no `beforeunload` handler). So the two
# keys differing by exactly that clause are both correct, and a sixth site dropping the advice
# earns its own key here with the same reasoning written down.
_NEVER_LOADED_COPY = {
    "Couldn't load Scales.": ["frontend/src/components/Fairness.tsx"],
    "Couldn't load new lines, and updates are paused.": ["frontend/src/components/LogsPanel.tsx"],
    "Couldn't load new lines. Reaper is trying again.": ["frontend/src/components/LogsPanel.tsx"],
    "Couldn't load the Leaving Soon settings.": ["frontend/src/components/PlexPanel.tsx"],
    "Couldn't load the library list.": [
        "frontend/src/components/PlexPanel.tsx",
        "frontend/src/components/SetupPlexStep.tsx",
    ],
    "Couldn't load the library list. Try again.": ["frontend/src/components/SetupPlexStep.tsx"],
    "Couldn't load the log.": ["frontend/src/components/LogsPanel.tsx"],
    "Couldn't load the reasons for this item. Close this panel and click the item to try again.": [
        "frontend/src/components/WhyPanelFallback.tsx"
    ],
    "Couldn't load the rest of the list, so nothing was selected. Your picks are as they were."
    " Try again.": ["frontend/src/components/ReviewQueue.tsx"],
    "Couldn't load the seasons. Collapse and expand to try again.": [
        "frontend/src/components/ReviewQueue.tsx"
    ],
    "Couldn't load the shelf status. Reload to try again.": [
        "frontend/src/components/JobsPanel.tsx"
    ],
    "Couldn't load the upkeep jobs. Reload to try again.": [
        "frontend/src/components/JobsPanel.tsx"
    ],
    "Couldn't load the watch history record.": ["frontend/src/components/PlexPanel.tsx"],
    "Couldn't load these settings.": ["frontend/src/components/PolicyEditor.tsx"],
    "Couldn't load these settings. Reload to try again.": [
        "frontend/src/components/GeneralPanel.tsx",
        "frontend/src/components/PlexPanel.tsx",
        "frontend/src/components/PolicyEditor.tsx",
        "frontend/src/components/SecurityPanel.tsx",
    ],
    "Couldn't load this page. Reload to try again.": [
        "frontend/src/components/AboutPanel.tsx",
        "frontend/src/components/BackupPanel.tsx",
    ],
    "Couldn't load this person's requests. Close this panel and click the card to try again.": [
        "frontend/src/components/ScalesPanel.tsx"
    ],
    "Couldn't load what a reap would remove. Reaper just can't show it right now."
    " Reload to try again.": ["frontend/src/components/ReapBreakdown.tsx"],
    "Couldn't load your connections.": [
        "frontend/src/components/ServicesPanel.tsx",
        "frontend/src/components/SetupConnectStep.tsx",
    ],
    "Couldn't load your lists, so there is no way to tell here whether they are working.": [
        "frontend/src/components/ListsPanel.tsx"
    ],
    "Couldn't load your review queue.": ["frontend/src/components/ReviewQueue.tsx"],
    "Reaper couldn't load the things a rule can look at, so there's nothing to pick from right"
    " now. The rules you've already added are still here.": [
        "frontend/src/components/PolicyRuleEditors.tsx",
        "frontend/src/components/PolicyRuleEditors.tsx",
    ],
    "Reaper couldn't load this plan. Reload the page to try again.": [
        "frontend/src/components/ReapPlan.tsx"
    ],
    "Reaper couldn't load this reap. Close this and try View again.": ["frontend/src/App.tsx"],
    "Reaper couldn't load your lists, so there's nothing to pick from right now.": [
        "frontend/src/components/PolicyRuleEditors.tsx"
    ],
}

#: What makes a run of text one of these. Accepts the spellings the tree uses plus the ones it
#: could reach for without anyone noticing (rule 147): ``couldn't load``, ``could not load``, and
#: the same with a typographic apostrophe (U+2019, which an editor substitutes on its own), in any
#: casing and anywhere in the run.
_NEVER_LOADED = re.compile("(?i)could(?:n['\\u2019]t| not) load")

#: What bounds one. The key is the WHOLE run between two of these, not the sentence starting at
#: the matched words, so a clause added at the FRONT of one copy moves that key: matching forward
#: from ``could`` left the front open, and prepending "Something went wrong." to one of a pinned
#: pair read green. These five are where JSX text, a string literal and a template all end.
#: Everything is read off the file flattened to a single line, so a sentence that WRAPS across
#: source lines is still one run, which four of them do. What this cannot see is a sentence
#: interpolating a value, since the run ends at the brace, and one assembled in a local; both land
#: as a shorter key rather than as a silent pass.
_TEXT_RUN = re.compile(r"[<>\"`{}]")


def test_the_never_loaded_sentences_are_pinned_per_sentence() -> None:
    """Five of these are written at more than one site, so they drift apart one copy at a time.

    Rule 144's shape on failure copy. One fact, "this panel has nothing to show you", is written
    32 times in 25 sentences, each by someone reading a different one. The reload-advice count
    above cannot see it: a file keeps its ``reload`` count while the sentence around the word
    changes.

    Keyed by sentence rather than by file, because a copy moving between files is not what this
    is about. A fifth panel picking up "Couldn't load these settings. Reload to try again." has to
    add itself to that key's list, where the four already on it are in view.

    Over ``.ts`` as well as ``.tsx``, because a sentence exported from a ``.ts`` module and
    rendered from a component is invisible to a ``.tsx``-only walk. That was demonstrated: a 26th
    sentence declared that way read green before the walk was widened.
    """
    found: dict[str, list[str]] = {}
    for path in _shipped_frontend_source():
        flat = " ".join(_without_comments(path.read_text(encoding="utf-8")).split())
        for run in _TEXT_RUN.split(flat):
            if _NEVER_LOADED.search(run):
                found.setdefault(run.strip(), []).append(str(path.relative_to(REPO)))
    assert {sentence: sorted(files) for sentence, files in found.items()} == _NEVER_LOADED_COPY, (
        "the never-loaded copy moved.\n"
        f"expected: {_NEVER_LOADED_COPY}\nfound:    {found}\n"
        "A new sentence: check first whether one of the keys above already says it, and reuse\n"
        "that rather than adding a 26th way to say the same thing. A new site on an existing\n"
        "key: add the file to that key's list. Adding or dropping 'Reload to try again.' is a\n"
        "separate decision, and _RELOAD_ADVICE above holds the reasoning behind it."
    )


#: Every `.field-sm` container the shipped tree writes, by file and by tag. `.field-sm` is a
#: `<label>` wherever exactly one control renders inside it, which is what lets the box name its
#: control with no `htmlFor`/`id` pair to keep in step, and a `<div>` wherever no single control
#: does. The four `<div>` sites and why each one is not a label: `ListModal`'s tag editor holds a
#: `TagsEditor` and a `Segmented`; `ServiceModal`'s library and instance pickers each render a
#: `<select>` per row of a `.map()`; `SetupPlexStep`'s manual address holds a host box and a port
#: box. That rule held at 26 sites across 9 files and was written down nowhere until this (W11-23).
_FIELD_SM_CONTAINERS = {
    "frontend/src/components/DiscordModal.tsx": {"label": 1},
    "frontend/src/components/JobsPanel.tsx": {"label": 2},
    "frontend/src/components/ListModal.tsx": {"div": 1, "label": 4},
    "frontend/src/components/NotificationsPanel.tsx": {"label": 1},
    "frontend/src/components/RestoreCard.tsx": {"label": 1},
    "frontend/src/components/SecurityPanel.tsx": {"label": 3},
    "frontend/src/components/ServiceModal.tsx": {"div": 2, "label": 6},
    "frontend/src/components/SetupPasswordStep.tsx": {"label": 2},
    "frontend/src/components/SetupPlexStep.tsx": {"div": 1, "label": 2},
}

#: One whole line: a `<label>` or `<div>` open tag whose `className` is the only attribute on it.
#: Accepts the two spellings the tree uses, a string literal and any one-line braced expression,
#: which covers `SecurityPanel`'s `viaRecovery ? "field-sm dim" : "field-sm"` and a template
#: literal alike. Rejects a class list broken over several lines, a second attribute on the open
#: tag, and any other tag. Those three leave the walk while `_FIELD_SM_WORD` still reads their
#: line, so the assertion below names them rather than skipping them (rule 147).
_FIELD_SM_OPEN = re.compile(r'^\s*<(label|div) className=(?:"[^"\n]*"|\{[^\n]*\})>\s*$')
_FIELD_SM_WORD = re.compile(r"\bfield-sm\b")
_FIELD_LABEL_SPAN = '<span className="field-label">'


def test_every_field_sm_box_names_itself_and_the_population_holds_still() -> None:
    """A `<label>` around two controls names the first one and leaves the second nameless.

    26 boxes across 9 files ride that rule and nothing declared it, so the 27th would copy
    whichever of the 26 its author had open. `.field-sm` is a `<label>` wherever exactly one
    control renders inside it and a `<div>` wherever no single control does.

    **This does not check the rule, and is named for what it does check** (rule 118). It pins two
    things: that every box opens with one `span.field-label`, and the population per file and per
    tag. What decides label-versus-div is invisible in source text, so a tag count would read the
    tree backwards at three of the 26. `ListModal`'s Plex library box holds a `<select>` and an
    `<input>` in the two arms of a ternary, so one renders. `ServiceModal`'s two pickers hold one
    `<select>` inside a `.map()`, so many do. A label over two controls therefore reads green
    here, and the per-file tag counts are what a wrong choice has to get past instead: a new
    `<div className="field-sm">` cannot be added without editing the comment above that says why
    each existing one is a div.

    Over `.ts` as well as `.tsx`, because a box taking its class from a constant in a `.ts` module
    leaves BOTH the matcher and the count at once, which is the shape rule 145 warns about. That
    was demonstrated: a 27th box holding two controls and no name read green before the walk was
    widened.
    """
    walked: dict[str, dict[str, int]] = {}
    unnamed: list[str] = []
    unread: list[str] = []
    for path in _shipped_frontend_source():
        lines = _without_comments(path.read_text(encoding="utf-8")).splitlines()
        for number, line in enumerate(lines):
            if not _FIELD_SM_WORD.search(line):
                continue
            name = str(path.relative_to(REPO))
            match = _FIELD_SM_OPEN.match(line)
            if not match:
                unread.append(f"{name}:{number + 1} -> {line.strip()[:70]}")
                continue
            walked.setdefault(name, {}).setdefault(match.group(1), 0)
            walked[name][match.group(1)] += 1
            below = lines[number + 1].strip() if number + 1 < len(lines) else ""
            if not below.startswith(_FIELD_LABEL_SPAN):
                unnamed.append(f"{name}:{number + 1} -> {below[:60]}")
    assert not unread, (
        "`field-sm` is written where the box matcher cannot read it:\n  "
        + "\n  ".join(unread)
        + "\nEach of these is either a box spelled in a form `_FIELD_SM_OPEN` rejects, which is a\n"
        "class list broken over several lines, a second attribute on the open tag, or a tag other\n"
        "than `<label>`/`<div>`, or a mention of the class that is not a box at all. Widen the\n"
        "matcher for the first. For the second, this walk needs a second population before it can\n"
        "tell one from the other, since one of each cancels out."
    )
    assert not unnamed, (
        "a `.field-sm` box whose first child is not its name:\n  " + "\n  ".join(unnamed) + "\n"
        f"Every one of them opens with {_FIELD_LABEL_SPAN}, which is what a screen reader reads\n"
        "out for the control inside. Put the span first, or say here why this box is different."
    )
    assert walked == _FIELD_SM_CONTAINERS, (
        "the `.field-sm` population moved.\n"
        f"expected: {_FIELD_SM_CONTAINERS}\nfound:    {walked}\n"
        'A new `<label className="field-sm">`: check exactly one control renders inside it,\n'
        "counting a `.map()` as many and a ternary as one, then bump the count here. If more\n"
        "than one renders, or none does, it is a `<div>` and the comment above gains a clause\n"
        "saying which of those it is."
    )


#: The surfaces that hold a connection-test verdict beside the fingerprint it was computed for,
#: so a badge can be withdrawn once it stops describing what is on screen. This is the population
#: the ban below scans, not a second one beside it (rule 147): every shipped file spelling an
#: ``of:`` key outside a comment. Pinned by name, because the fifth surface is written by copying
#: whichever of these four its author happened to open, and three of the four were the wrong copy.
_VOUCHED_TEST_SURFACES = {
    "frontend/src/components/DiscordModal.tsx",
    "frontend/src/components/NotificationsPanel.tsx",
    "frontend/src/components/ServiceModal.tsx",
    "frontend/src/components/ServicesPanel.tsx",
}

_OF_KEY = re.compile(r"\bof:")

#: What an ``of:`` key may be handed: a name, or a path of them. ``issued.of`` is the fingerprint
#: captured when the request was issued; ``string`` is the type annotation on the state that holds
#: it. Everything else is an expression, and an expression under ``of:`` is evaluated where it is
#: written, which is the question this asks.
_CAPTURED_NAME = re.compile(r"^[A-Za-z_$][\w$]*(?:\.[A-Za-z_$][\w$]*)*$")


def _without_comments_keeping_lines(text: str) -> str:
    """``_without_comments``, with every removed line still there as an empty one.

    The offender list reports line numbers, so the collapsing form cannot be used. A block
    comment becomes its own newlines.
    """
    blanked = _BLOCK_COMMENT.sub(lambda m: "\n" * m.group(0).count("\n"), text)
    return _without_line_comments(blanked)


def _value_after_of(line: str) -> str | None:
    """The expression ``line`` hands to an ``of:`` key, or ``None`` where it has no such key.

    Bounded crudely, at the first ``,``, ``;`` or closing bracket. That cuts a value carrying one
    of those inside a string (``of: `${kind}, ${baseUrl()}` ``) short, and the check below is
    written so a short read is a FAILURE rather than a pass: half a template literal is not a
    name either. An exact walk over bracket depth was tried first and is what made the cut matter,
    since it counted brackets and not quotes and so mis-read the same line the other way.
    """
    key = _OF_KEY.search(line)
    if key is None:
        return None
    for i in range(key.end(), len(line)):
        if line[i] in ",;)]}":
            return line[key.end() : i]
    return line[key.end() :]


def _settle_time_fingerprints(text: str) -> list[int]:
    """Line numbers where an ``of:`` key is handed anything but a name, outside ``onMutate``.

    Written as an allowlist rather than as a hunt for a call, because the defect is not "a call
    ran here" but "this value was computed here", and a template literal spelling out the same
    fingerprint is the same defect with no call in it. The first draft asked for a call and let
    exactly that through. So the two shapes a stored fingerprint may take are named and everything
    else fails, which is the direction a gate on this tree resolves.
    """
    found = []
    for n, line in enumerate(_without_comments_keeping_lines(text).splitlines(), 1):
        if "onMutate" in line:
            continue
        value = _value_after_of(line)
        if value is not None and not _CAPTURED_NAME.match(value.strip()):
            found.append(n)
    return found


def test_a_held_test_result_is_stamped_when_its_request_is_issued() -> None:
    """A "Passed" badge must describe the address that was tested, not the one now on screen.

    Four surfaces store ``{ result, of }`` and show the badge only while ``of`` still matches what
    the form holds. That comparison is the honesty of the badge (rule 85), and it is satisfied by
    computing the fingerprint at EITHER end -- which is why three of the four computed it at
    success time, where it is no longer the address the request asked about. The boxes stay live
    while the request is out, so pasting a second webhook while the first is being sent to left the
    two matching by construction and "Passed" beside a channel nobody tried. ``ServiceModal``
    captured it in ``onMutate`` and the other three did not, and nothing in the suite could see the
    difference; #178 and #264 each fixed one site of this family by hand.

    **The forms this reads** (rule 147): the value an ``of:`` key is handed, on a line that does
    not also spell ``onMutate``, in a shipped ``.ts``/``.tsx`` with block comments blanked and
    ``//`` runs cut. It passes a name or a path of names, ``issued.of`` and ``string``; everything
    else fails, template literals and concatenations included.
    ``test_the_fingerprint_matcher_reads_every_spelling_the_tree_puts_after_of`` runs both lists.

    One thing it cannot see, and the population pin is what covers it: a fingerprint computed into
    a local at success time and handed over by that local's name. The pin is over the same ``of:``
    keys this scans rather than over the helper's name, so a fifth surface arrives here to be read
    whatever it calls its fingerprint.

    One thing the pin cannot see either, named rather than implied: ``_BLOCK_COMMENT`` reads a
    ``/*`` inside a string literal as an opener, so the span to the next ``*/`` is blanked. The
    tree holds one, ``docs/toMdx.ts:21``'s ``GENERATED_MARKER``, measured at 123 characters over
    no line break and covering no ``of:``.
    """
    holders = {
        str(path.relative_to(REPO))
        for path in _shipped_frontend_source()
        if any(
            _OF_KEY.search(line)
            for line in _without_comments_keeping_lines(
                path.read_text(encoding="utf-8")
            ).splitlines()
        )
    }
    assert holders == _VOUCHED_TEST_SURFACES, (
        "the surfaces pairing a test result with its fingerprint moved.\n"
        f"expected: {sorted(_VOUCHED_TEST_SURFACES)}\nfound:    {sorted(holders)}\n"
        "A new one: capture the fingerprint in the mutation's `onMutate` and read it back off\n"
        "the context in `onSuccess`, the way `ServiceModal` does, then add the file here. One\n"
        "that went away: drop it."
    )

    offenders = [
        f"{path.relative_to(REPO)}:{n}"
        for path in _shipped_frontend_source()
        for n in _settle_time_fingerprints(path.read_text(encoding="utf-8"))
    ]
    assert not offenders, (
        "these compute a test result's fingerprint where the result is STORED, which is after\n"
        "the request came back. The operator can keep typing while it is out, so the answer gets\n"
        "filed against an address it was never asked about and the badge vouches for a host\n"
        "nobody tried (rule 85). Capture it at issuance instead:\n"
        "  onMutate: () => ({ of: testedWith() }),\n"
        "  onSuccess: (r, _v, issued) => setTest({ result: r, of: issued.of }),\n"
        + "\n".join(offenders)
    )


def test_the_fingerprint_matcher_reads_every_spelling_the_tree_puts_after_of() -> None:
    """The gate above is a source-text scan, so it is worth what its matcher can parse (rule 147).

    Every case here is a way the check could read green over a real one, or red over an innocent
    line. Four earned their place by failing a draft of it: the call hunt this replaced passed
    ``of: [kind, baseUrl()].join(" ")``, then passed an inlined template literal with no call in
    it at all, and the bracket walk written to fix the first counted brackets and not quotes, so a
    comma inside a template literal ended the value early. The JSX case is the fail-closed one, a
    block comment's continuation line, which the per-line prefix skip could not see.
    """
    caught = [
        "      setTest({ result: r, of: testedWith() });",
        '      setTest({ result: r, of: [kind, baseUrl()].join(" ") });',
        "  setProbe({ of: fingerprint(), result: r });",
        "      setTest({ result: r, of: `${instance.base_url} ${instance.has_key}` });",
        '      setTest({ result: r, of: "a, " + testedWith() });',
        '      setTest({ result: r, of: host + ":" + port });',
    ]
    passed = [
        "      setTest({ result: r, of: issued.of });",
        "    onMutate: () => ({ of: testedWith() }),",
        "    of: string;",
        "  const [test, setTest] = useState<{ result: InstanceTest; of: string } | null>(null);",
        # The value is a name; the call belongs to the member after it.
        "      setTest({ of: issued.of, result: normalize(r) });",
        # Prose, in the three shapes the tree writes it. Both blocks are whole, because a
        # continuation line is only ever reached with its opener: `ListsPanel` writes the JSDoc
        # one, "covers only part of:", and the diff that added this gate wrote the JSX one.
        "      // of: testedWith() is what this used to be",
        "/** A list a rule covers\n *  only part of: what it keeps (roughly).\n */",
        "{/* Two lines, and this is the second:\n    of: the operator presses Test again. */}",
    ]
    for line in caught:
        assert _settle_time_fingerprints(line) == [1], f"should be caught: {line}"
    for line in passed:
        assert _settle_time_fingerprints(line) == [], f"should NOT be caught: {line}"


# Every ``<select>`` the app ships, counted by the scan below rather than believed. The two the
# count once carried past were #147's library pickers, which shipped nameless; they have names
# now, and the number is here so a twentieth that does not cannot hide behind them (rule 145).
# +1 for the Plex library picker on Settings -> Lists: the field #483 was about, which stops
# being a name Reaper guesses and becomes one the operator picks off their own server. +1 for
# `PolicyRuleEditors`'s ListNameSelect, the picker an `on_list` keep rule names its list from --
# a rule that matches on the name, so it is a picker rather than a box (rule 108's separator half).
_EXPECTED_SELECTS = 23


#: A ``//`` that starts a comment, which is any ``//`` not preceded by a colon. Splitting on the
#: bare pair truncated a line at the first URL in it, taking the rest of that line out of every
#: walk below: `ServiceModal` writes an example address in running help text, and a sentence after
#: one would have been unscannable.
_LINE_COMMENT = re.compile(r"(?<!:)//.*")


def _without_line_comments(chunk: str) -> str:
    """``chunk`` with every ``//`` run to end-of-line removed, leaving a URL's ``//`` alone."""
    return "\n".join(_LINE_COMMENT.sub("", line) for line in chunk.splitlines())


def _select_is_named(tag: str, text: str) -> bool:
    """Whether ``tag`` carries a name a screen reader can say, resolved against its own file.

    Accepts, and these are the spellings the tree actually uses (rule 147):
      - ``aria-label="…"`` and ``aria-label={…}``
      - ``aria-labelledby=…``
      - ``id=X`` where the SAME file holds a ``htmlFor=X``, matched on the raw attribute value
        so ``id="tz"``/``htmlFor="tz"`` and ``id={rowId}``/``htmlFor={rowId}`` both resolve.

    Rejects, each of which the previous matcher accepted:
      - ``id=X`` with no ``htmlFor`` anywhere -- an id names nothing on its own, and the
        assertion's own message promised a label that nothing looked for (rule 7/24)
      - ``id=X`` where the file's only ``htmlFor`` points at a DIFFERENT id
      - a comment inside the tag that merely mentions ``aria-label=`` or ``id=``
    """
    if re.search(r"\baria-label(?:ledby)?=", tag):
        return True
    ident = re.search(r'\bid=("[^"]*"|\{[^}]*\})', tag)
    if not ident:
        return False
    return re.search(rf"\bhtmlFor={re.escape(ident.group(1))}", text) is not None


def _shipped_selects() -> list[tuple[Path, int, str]]:
    """Every ``<select>`` opening tag in shipped .tsx, as (path, line, tag text).

    **Brace-aware, and that is the whole implementation.** Every other check in this file is a
    per-line regex, and here that shape is catastrophically wrong: measured against the tree,
    only 3 of the named selects put ``aria-label`` on the same line as the tag. JSX wraps, and a
    picker with a handler and three attributes always wraps. A per-line matcher would report
    sixteen false offenders, be deleted within the week, and take the two real ones with it.

    So the walk goes from ``<select`` to the first ``>`` at brace depth 0 -- past every
    ``onChange={(e) => ...}`` whose own ``>`` and ``}`` sit inside the attribute -- and hands
    back the whole tag to be inspected as one string. That is rule 147: prefer reading the whole
    attribute or call over anchoring on a delimiter one spelling happens to put there.

    **A ``//`` comment inside the tag is dropped before anything reads it**, and that is not
    hypothetical tidying: two selects in this tree carry a multi-line comment between their
    attributes. Left in, the comment text is part of the string the name search runs against, so
    a comment merely *mentioning* ``aria-label=`` names the control -- and a ``>`` inside one
    ends the walk early, handing back half a tag.
    """
    found: list[tuple[Path, int, str]] = []
    for path in _shipped_tsx():
        text = path.read_text(encoding="utf-8")
        for match in re.finditer(r"<select\b", text):
            i, depth = match.end(), 0
            while i < len(text):
                # Skip a line comment whole: its `{`, `}` and `>` are prose, not syntax.
                if text.startswith("//", i):
                    nl = text.find("\n", i)
                    i = len(text) if nl == -1 else nl
                    continue
                char = text[i]
                if char == "{":
                    depth += 1
                elif char == "}":
                    depth -= 1
                elif char == ">" and depth == 0:
                    break
                i += 1
            tag = " ".join(_without_line_comments(text[match.start() : i + 1]).split())
            # The bare word ``<select>`` inside a comment is prose about a select, not one:
            # three sites in the tree talk about the element, and a scan that counts them
            # reports permanent offenders nobody can fix. A real one always carries at least a
            # `value` or a `defaultValue`, so an attribute-free tag is never markup here.
            if tag == "<select>":
                continue
            found.append((path, text[: match.start()].count("\n") + 1, tag))
    return found


def test_every_select_says_what_it_is_for() -> None:
    """A ``<select>`` with no name is a control a screen reader can only call "combo box".

    The component tests reach controls by the name an operator can hear, which is the right
    shape and is blind in one specific way: a control on a branch no fixture mounts is missing
    from the walk and from the count alike, and the two absences hide each other. A static scan
    closes that for ``<select>`` specifically, where the defect is *absence* of a name and a
    scanner can see absence (#180).

    **Why ``<select>`` and not every control.** Measured: a static ``<input>`` scan is 43 shipped
    inputs, 25 named, 18 unnamed -- and 17 of the 18 are inside a ``<label>`` wrapper and
    perfectly correct. 94% false positives, so that gate would be noise. ``<button>`` is worse
    still, because its defect class is an *ambiguous but present* name, which no scanner can
    see. This one is a two-line result set that went to zero when #147 landed and stays there.
    """
    selects = _shipped_selects()
    assert len(selects) == _EXPECTED_SELECTS, (
        f"expected {_EXPECTED_SELECTS} shipped <select> elements, found {len(selects)}:\n"
        + "\n".join(f"  {p.relative_to(REPO)}:{n}" for p, n, _ in selects)
        + "\n\nIf you ADDED or REMOVED one, bump _EXPECTED_SELECTS. If you did not, one dropped\n"
        "out of the walk -- and the assertion below passes happily on a select it cannot see."
    )
    unnamed = [
        f"{path.relative_to(REPO)}:{lineno} -> {tag}"
        for path, lineno, tag in selects
        if not _select_is_named(tag, path.read_text(encoding="utf-8"))
    ]
    assert not unnamed, (
        "every <select> needs a name a screen reader can say -- aria-label, aria-labelledby, or\n"
        "an id a <label htmlFor> points at. Without one it is announced as an unlabeled combo\n"
        "box, and the operator has to guess which row it belongs to:\n" + "\n".join(unnamed)
    )


def test_the_select_name_matcher_rejects_what_it_claims_to_reject() -> None:
    """The gate above is a source-text scan, so it is worth exactly what its matcher can parse.

    Rule 147: a matcher ships with the spellings it accepts AND the ones it rejects, run. Both
    rejections below were live holes -- the first shipped promising a ``<label htmlFor>`` lookup
    that did not exist (rule 7/24), and the second let a comment name a control, on a gate whose
    own commit put multi-line comments inside two select tags.
    """
    labelled = '<label htmlFor="tz">Zone</label>'
    accepted = [
        ('<select aria-label="Time zone" value={tz}>', ""),
        ("<select aria-label={label} value={tz}>", ""),
        ('<select aria-labelledby="tz-head" value={tz}>', ""),
        ('<select id="tz" value={tz}>', labelled),
        ("<select id={rowId} value={tz}>", "<label htmlFor={rowId}>Zone</label>"),
    ]
    rejected = [
        ("<select value={tz}>", labelled),
        # An id names nothing on its own, and nothing in this tree points a label at one.
        ('<select id="tz" value={tz}>', ""),
        # A label, but pointed somewhere else.
        ('<select id="tz" value={tz}>', '<label htmlFor="other">Zone</label>'),
    ]
    for tag, text in accepted:
        assert _select_is_named(tag, text), f"should count as named: {tag}"
    for tag, text in rejected:
        assert not _select_is_named(tag, text), f"should NOT count as named: {tag}"

    # And the comment stripping, which happens before the matcher ever sees the tag: prose about
    # a name is not a name, so a comment mentioning either spelling must not survive into the
    # string that gets searched. These are both spellings the old matcher fell for.
    for comment in ("// no aria-label= needed here", "// matches the id= of the row above"):
        stripped = " ".join(
            _without_line_comments(f"<select\n  {comment}\n  value={{tz}}>").split()
        )
        assert not _select_is_named(stripped, ""), f"comment named the control: {comment}"


# Test files that mount a tree and deliberately do not audit it, because what they render is not
# an operator surface of its own. The first five are shared primitives, audited wherever they are
# actually mounted -- auditing them alone would pin the same tree twice and, for a primitive that
# needs a labelled parent, would report a defect the running app does not have. The rest drive
# behavior (an announcement, a history entry, a focus move, a failed read) against whatever markup
# is cheapest, so their trees are fixtures rather than screens.
_A11Y_RENDERS_NO_SURFACE_OF_ITS_OWN = {
    "components/ModalShell.test.tsx": "the shell every modal is audited through",
    "components/Notice.test.tsx": "a primitive, audited in each panel that raises one",
    "components/Poster.test.tsx": "a primitive, audited inside the queue rows",
    "components/QuantityInput.test.tsx": "a primitive, audited inside the policy editor",
    "components/StatusChip.test.tsx": "a primitive, audited inside the rows that carry it",
    "announce.test.tsx": "live-region plumbing; the markup is a fixture",
    "backnav.test.tsx": "history entries, not a screen",
    "components/SettingsStaleRead.test.tsx": "a failed read's branch, audited in the panels",
    "components/StaleReadNotice.test.tsx": "one notice, audited in the panels that raise it",
    "components/StaleReadSweep.test.tsx": "a failed read's branch, audited in the panels",
    "components/TestBadgeFreshness.test.tsx": "one badge's freshness, audited in the panels",
    "components/PlexPin.test.tsx": "the poll's state machine; it mounts the announcer, no screen",
    "focus.test.tsx": "focus moves, not a screen",
    "AppFocus.test.tsx": "which view holds a jump's aim; both routes it drives to are stubs",
}

# The population the walk itself collects: every `*.test.tsx` under frontend/src that mounts
# something. Pinned separately from the audited count because they are DIFFERENT sets, and a file
# that drops out of the walk is otherwise missing from both halves while the two numbers agree
# (rule 145). Re-derive by running the test, never by arithmetic on the maps above.
# +1 for `ListModal.test.tsx`, the add/edit form on Settings -> Lists, which is a screen of its
# own and carries its own audit rather than being covered by the panel that opens it.
# +1 for `JobsShelfSkip.test.tsx`, which mounts the Jobs panel to drive the shelf row's
# skipped-scan branch and audits that branch, since the row draws copy no other test renders.
# +1 for `SetupPlexStep.test.tsx`, the wizard's Plex step, which had no test of its own while the
# settings panel's copy of the same behavior had a careful one (W10-7). It audits its linked
# state, where the server and connection pickers are.
# +1 for `PanelHead.test.tsx`, which mounts the item panel and the show panel to compare the head
# they share. It audits that head with every link set, the state neither panel's own suite drives.
# +1 for `AppFocus.test.tsx`, which is exempt rather than audited: it mounts the shell to ask
# which view is holding a jump's aim, and the two routes it drives to are stubs printing a prop.
# +1 for `artFallback.test.tsx`, which mounts `WhyHero` to drive the art-then-poster ladder the
# hook now declares once. It audits the banner on both rungs, the fallback included.
_EXPECTED_RENDERING_TEST_FILES = 58


def test_every_rendered_surface_is_audited_or_says_why_not() -> None:
    """A new panel must not ship unaudited just because nobody remembered (#231).

    ``src/test/a11y.ts`` is the only layer that reads the tree the browser BUILT, so a name
    assembled from props or a role that prunes its own children is visible to it and to nothing
    else. Whether a surface carries one was a convention, and rule 147 is the standing answer
    that prose cannot bind an author who never read it: a component could ship with a test that
    mounts it, render a control with no accessible name, and leave every gate green.

    So membership is total. Every rendering test file is audited, or named in one of the two maps
    above, and a new one that is neither fails here with its own path in the message. The count of
    the walk is pinned beside it, because a flag-shaped assertion cannot tell a member that
    complies from one that dropped out of the walk (rule 145).
    """
    rendering: dict[str, int] = {}
    for path in sorted(FRONTEND_SRC.rglob("*.test.tsx")):
        body = path.read_text(encoding="utf-8")
        # Every spelling of "mounts a tree" the suite uses, not just the bare one (rule 147):
        # testing-library's `render(` and `renderHook(`, the shared `renderWithProviders(` and
        # `renderHookWithProviders(`, and the file-local `renderPanel(`/`renderQueue(` helpers
        # that wrap them. `\b` keeps `rerender(` out, which mounts nothing new. This matched
        # `render(` alone until the provider trees moved onto the shared helper, and 29 of the
        # 53 files below silently left the walk in one commit -- caught by the count, which is
        # what it is here for.
        if not re.search(r"\brender[A-Za-z]*\(", body):
            continue
        rel = path.relative_to(FRONTEND_SRC).as_posix()
        rendering[rel] = len(re.findall(r"\bexpectNoA11yViolations\(", body))

    assert len(rendering) == _EXPECTED_RENDERING_TEST_FILES, (
        f"expected {_EXPECTED_RENDERING_TEST_FILES} frontend test files that mount a tree, "
        f"found {len(rendering)}.\nIf you ADDED or REMOVED one, bump "
        "_EXPECTED_RENDERING_TEST_FILES. If you did not, a file dropped out of the walk and is "
        "now missing from this gate AND from its own coverage."
    )

    named = _A11Y_RENDERS_NO_SURFACE_OF_ITS_OWN.keys()
    unaccounted = sorted(f for f, audits in rendering.items() if not audits and f not in named)
    assert not unaccounted, (
        "these test files mount a tree, never audit it, and say nothing about why:\n"
        + "\n".join(f"  {f}" for f in unaccounted)
        + "\n\nAdd `await expectNoA11yViolations(container)` after the file's own arrival await "
        "(a whole page takes `{ pageLevel: true }` so the region rule runs), or, if it renders "
        "no operator surface of its own, add it to _A11Y_RENDERS_NO_SURFACE_OF_ITS_OWN with the "
        "reason."
    )

    # The other direction: a map entry that has since gained an audit, or that names a file the
    # walk no longer finds. Either way the map is now describing a tree that does not exist, and
    # an exemption nobody can see the subject of is how a stale suppression outlives its reason.
    settled = sorted(f for f in named if rendering.get(f))
    assert not settled, (
        "these files are exempted from the axe audit but now call it:\n"
        + "\n".join(f"  {f}" for f in settled)
        + "\n\nDrop them from _A11Y_RENDERS_NO_SURFACE_OF_ITS_OWN."
    )
    missing = sorted(f for f in named if f not in rendering)
    assert not missing, (
        "these files are named in an axe-audit map but no longer mount a tree:\n"
        + "\n".join(f"  {f}" for f in missing)
    )


def test_the_plex_sign_in_window_has_a_page_that_closes_it() -> None:
    """The forward path, the page it names, and the two callers that ask for it agree.

    Reaper closes the Plex sign-in window by having plex.tv forward it to a page whose
    only job is ``window.close()``. Nothing fails loudly when that breaks: the sign-in
    still works and the window simply stays open, which is the bug it was built to fix
    (#372). So the three halves are pinned to each other here rather than left to a
    rename.
    """
    forward_path = re.search(
        r'^PLEX_FORWARD_PATH = "([^"]+)"$', (SRC / "api" / "schemas.py").read_text(), re.M
    )
    assert forward_path, "PLEX_FORWARD_PATH is gone from api/schemas.py"
    page = REPO / "frontend" / "public" / forward_path.group(1).lstrip("/")

    assert page.is_file(), (
        f"PLEX_FORWARD_PATH names {forward_path.group(1)}, but frontend/public/{page.name} "
        "does not exist. plex.tv would forward the sign-in window to a 404."
    )
    assert "window.close()" in page.read_text(), (
        f"{page.name} is what closes the Plex sign-in window, and it no longer calls "
        "window.close(). The window is opened with noopener, so nothing else can."
    )

    # The browser names its own origin (the server's Host is rewritten by every proxy in
    # front of it), so a caller that stops sending one silently stops closing its window.
    callers = (FRONTEND_SRC / "api.ts").read_text()
    for route in ("/api/auth/plex/start", "/api/settings/plex/link/start"):
        # The body is whatever follows the path on that line. Matching a balanced argument
        # list would reject `plexForward()` on its own parenthesis (rule 147).
        call = re.search(rf'"{re.escape(route)}",([^\n]+)', callers)
        assert call, f"{route} is no longer posted from api.ts under a matchable spelling"
        assert "plexForward()" in call.group(1), (
            f"{route} stopped sending the browser's origin, so the window it opens will "
            "never close. Both Plex start routes send plexForward()."
        )


# --- the reap-readiness tie -------------------------------------------------------------
#
# `reap_ready` is one fact declared in Python and re-derived in TypeScript, because "not ready"
# is not a sentence: the Reap page and the wizard's last step each need to say what to go and
# do. `reapReadiness.ts` said in prose that a test would fail if the server's definition changed
# and it did not, and no such test existed -- the agreement test beside it hand-transcribes the
# Python expression into the same file as its own assertion, so it can only prove the module
# agrees with that transcription. Adding a conjunct to `reap_ready` left the whole frontend
# suite green. This is rule 144's remedy: the generated-looking claim gets a test pointed at the
# other copy BY NAME, rather than a comment asking the next author to remember.


def _reap_ready_fields() -> set[str]:
    """The `SetupStatus` fields the server's own `reap_ready` is built from.

    Parsed, never transcribed. The expression is read off the `SetupStatus(...)` call, each name
    that is itself a pure and/or of other names is expanded to its leaves (so `scan_ready`
    becomes the instance checks the frontend words separately), and each leaf is mapped back to
    the payload field it is assigned to in the same call.
    """
    import ast

    tree = ast.parse((SRC / "api" / "setup.py").read_text(encoding="utf-8"))
    fn = next(
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.AsyncFunctionDef | ast.FunctionDef) and n.name == "setup_status"
    )
    call = next(
        n
        for n in ast.walk(fn)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "SetupStatus"
    )

    # local name -> the expression it was assigned, for the pure-boolean ones we can expand
    assigns: dict[str, ast.expr] = {}
    for node in ast.walk(fn):
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target = node.targets[0]
            if isinstance(target, ast.Name):
                assigns[target.id] = node.value

    def pure_bool(expr: ast.expr) -> bool:
        if isinstance(expr, ast.Name):
            return True
        if isinstance(expr, ast.BoolOp):
            return all(pure_bool(v) for v in expr.values)
        return False

    def leaves(name: str, seen: frozenset[str]) -> set[str]:
        expr = assigns.get(name)
        if name in seen or expr is None or not pure_bool(expr):
            return {name}
        out: set[str] = set()
        for sub in {n.id for n in ast.walk(expr) if isinstance(n, ast.Name)}:
            out |= leaves(sub, seen | {name})
        return out

    reap_ready = next(k.value for k in call.keywords if k.arg == "reap_ready")
    names: set[str] = set()
    for n in {x.id for x in ast.walk(reap_ready) if isinstance(x, ast.Name)}:
        names |= leaves(n, frozenset())

    # local name -> payload field, off the same constructor call (`has_password=password_set`)
    to_field = {k.value.id: k.arg for k in call.keywords if isinstance(k.value, ast.Name) and k.arg}
    return {to_field.get(n, n) for n in names}


def test_the_frontend_reap_blockers_read_the_fields_the_server_builds_reap_ready_from() -> None:
    """A conjunct added on one side and not the other must fail here.

    The failure it exists for is silent and points the reassuring way: the server gains a
    requirement, the browser does not hear about it, and an install is told "You're all set" over
    a run that will be refused at the button -- which is #383 arriving a second time, by the exact
    route the first one took.
    """
    server = _reap_ready_fields()

    body = (FRONTEND_SRC / "reapReadiness.ts").read_text(encoding="utf-8")
    # Comments name fields while explaining them, and a prose mention is not a read.
    code = re.sub(r"/\*.*?\*/", "", body, flags=re.DOTALL)
    code = re.sub(r"//[^\n]*", "", code)
    frontend = set(re.findall(r"\bsetup\.(\w+)", code))

    assert frontend == server, (
        "frontend/src/reapReadiness.ts and src/reaper/api/setup.py disagree about what a real "
        f"reap needs.\n  setup.py builds reap_ready from: {sorted(server)}\n  reapReadiness.ts "
        f"reads: {sorted(frontend)}\nEvery conjunct of reap_ready needs a sentence in "
        "reapReadiness.ts saying what to go and do about it, or the Reap page and the wizard's "
        "last step promise a run the server refuses."
    )


# ---------------------------------------------------------------------------
# The manual site's palette
# ---------------------------------------------------------------------------

#: The site copies Reaper's tokens rather than importing them, because the app and the site are
#: separate builds and sharing a stylesheet across that boundary would tie two node projects
#: together for a handful of hex values. A copy is exactly what rule 144 warns about, so the copy
#: is checked here instead of trusted.
_APP_TOKEN_CSS = REPO / "frontend" / "src" / "styles" / "00-tokens.css"
_SITE_TOKEN_CSS = REPO / "website" / "src" / "css" / "custom.css"

#: Reconciled by hand against `website/src/css/custom.css`: every `--rp-*` token it declares in
#: BOTH themes. Pinned because the comparison below is driven by what the site declares, and a
#: token deleted from the site drops out of the comparison rather than failing it (rule 145).
_EXPECTED_SHARED_TOKENS = 19


def _css_block(text: str, opener: str) -> str:
    """The brace-balanced body of the first block introduced by ``opener``."""
    start = text.index(opener) + len(opener)
    depth = 1
    for i in range(start, len(text)):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                return text[start:i]
    raise AssertionError(f"unbalanced braces after {opener!r}")


#: `--accent-text` is deliberately not the same declaration in the two files, so comparing it
#: would fail forever on a difference that is correct. The app writes a MEASURED ink at runtime
#: (`accent.ts` searches for a value clearing WCAG AA against each theme's ground, because the
#: accent is operator-configurable and a fixed darken does not clear a pale yellow), and the
#: token there is `var(--accent-text-light, <fallback>)` so the measurement can win. The site has
#: no runtime measurement and no custom accent, so it carries the fallback alone. The fallbacks
#: themselves are compared: they are the substring this exclusion does not reach.
_PALETTE_EXCLUDED = {"--rp-accent-text"}


def _declarations(block: str) -> dict[str, str]:
    """``--name: value`` pairs, whitespace collapsed so formatting cannot fail the compare.

    Comments are stripped first. Both files explain their tokens inline, and a `/* … */` sitting
    between two declarations otherwise lands inside the preceding value: the first run of this
    check read `--radius-sm` as seven pixels followed by a paragraph about progress fills.
    """
    block = re.sub(r"/\*.*?\*/", "", block, flags=re.DOTALL)
    return {
        m.group(1): " ".join(m.group(2).split())
        for m in re.finditer(r"(--[a-z0-9-]+)\s*:\s*([^;]+);", block)
    }


def test_the_site_palette_matches_the_app_palette() -> None:
    """Every color the manual site copies from the app still says what the app says.

    Named from `website/src/css/custom.css`, which tells the next author this check exists;
    rule 7 makes that comment a promise, and this is the promise kept. The failure it exists for
    is quiet and cosmetic-looking: someone retunes an accent in the app for contrast, the site
    keeps the old value, and the two surfaces of one product drift apart a shade at a time. The
    accent tokens are the ones that matter, because the app's are the output of a WCAG AA
    contrast search and a stale copy here fails that bar while looking fine.
    """
    app = _APP_TOKEN_CSS.read_text(encoding="utf-8")
    site = _SITE_TOKEN_CSS.read_text(encoding="utf-8")

    themes = {
        "light": (
            _declarations(_css_block(app, ":root {")),
            _declarations(_css_block(site, ":root {")),
        ),
        "dark": (
            _declarations(_css_block(app, "@media (prefers-color-scheme: dark) {")),
            _declarations(_css_block(site, '[data-theme="dark"] {')),
        ),
    }

    compared: set[str] = set()
    wrong: list[str] = []
    for theme, (app_decls, site_decls) in themes.items():
        for name, site_value in site_decls.items():
            if not name.startswith("--rp-") or name in _PALETTE_EXCLUDED:
                continue
            app_name = "--" + name.removeprefix("--rp-")
            if app_name not in app_decls:
                continue
            # The site's values reference `--rp-*`; the app's reference the same names without
            # the prefix. Normalizing lets a `color-mix(...)` be compared as text like any
            # other value, rather than being skipped as too hard.
            normalized = site_value.replace("var(--rp-", "var(--")
            compared.add(name)
            if normalized != app_decls[app_name]:
                wrong.append(
                    f"  {theme}: {name} is {normalized!r} here, "
                    f"but {app_name} is {app_decls[app_name]!r} in the app"
                )

    assert not wrong, (
        "website/src/css/custom.css has drifted from frontend/src/styles/00-tokens.css:\n"
        + "\n".join(wrong)
        + "\nThe app's tokens are the source. Copy the new value across, and re-read the "
        "contrast note beside it before assuming the change is cosmetic."
    )
    assert len(compared) == _EXPECTED_SHARED_TOKENS, (
        f"the palette walk compared {len(compared)} tokens, expected "
        f"{_EXPECTED_SHARED_TOKENS}. If you added or removed a --rp-* token in both themes of "
        "website/src/css/custom.css, move this number with it; if you did not, a token dropped "
        f"out of the comparison and is no longer checked. Compared: {sorted(compared)}"
    )


# --------------------------------------------------------------------------------------------
# The reaper-artifact skill hands an agent Reaper's live look for a mockup: which files carry the
# tokens and the component styles, and which variables to build on. A moved file or a renamed
# token turns that guidance into a wrong mockup silently, so both are pinned here rather than
# trusted. See .claude/skills/reaper-artifact/SKILL.md.
# --------------------------------------------------------------------------------------------
_ARTIFACT_SKILL = REPO / ".claude" / "skills" / "reaper-artifact" / "SKILL.md"

#: Files the skill sends an agent to read for the app's current look. Each must exist AND still be
#: named in the skill: the tokens already moved once (index.css -> styles/00-tokens.css), which is
#: the exact break this catches.
_ARTIFACT_SKILL_SOURCES = (
    "frontend/src/index.css",
    "frontend/src/styles/00-tokens.css",
)

#: Concrete `--tokens` the skill names in "The variables you will reach for", reconciled by hand
#: against that section. Family forms (`--text-*`, `--space-*`) are excluded on purpose: the
#: matcher takes only a backtick span whose whole content is `--<name>`, so a `*` form never
#: matches (rule 147), and a family cannot be checked against one declaration anyway. Pinned so a
#: token dropped from the skill leaves the scan rather than failing it (rule 145).
_ARTIFACT_SKILL_TOKENS = 16


def test_the_artifact_skill_points_at_files_that_exist() -> None:
    """The reaper-artifact skill's file pointers still resolve, so its mockup guidance is live.

    The skill tells an agent to read the app's own stylesheet and token file to build an artifact
    in Reaper's look. When one of those files moves and the skill is not repointed, the guidance
    reads fine and sends the agent nowhere. That already happened to the tokens once, so the path
    is guarded rather than trusted (rule 68).
    """
    skill = _ARTIFACT_SKILL.read_text(encoding="utf-8")
    for rel in _ARTIFACT_SKILL_SOURCES:
        assert (REPO / rel).exists(), (
            f"reaper-artifact sends an agent to {rel}, which is gone. Repoint the skill at the "
            "file's new home, or every mockup built from it inherits a dead reference."
        )
        assert rel in skill, (
            f"{rel} is guarded here but no longer named in the skill. Fix whichever is wrong so "
            "the guard and the skill agree on the file (rule 144)."
        )


def test_the_artifact_skill_names_live_tokens() -> None:
    """Every design token the reaper-artifact skill names still exists in 00-tokens.css.

    The skill lists the variables an agent reaches for: the verdict colors, the accent, the
    neutrals. Rename or drop one in the token file without fixing the skill, and a mockup built on
    the old name renders wrong with no error, because the browser resolves an undefined custom
    property to nothing. The count is pinned for rule 145's reason: a name silently dropped from
    the skill leaves the scan and stops being checked, and a "names a live token" assertion cannot
    tell that from compliance.
    """
    skill = _ARTIFACT_SKILL.read_text(encoding="utf-8")
    root = _declarations(_css_block(_APP_TOKEN_CSS.read_text(encoding="utf-8"), ":root {"))

    # The population: every backtick span whose ENTIRE content is `--<name>`. A family spelled
    # `--text-*` carries a `*` and never matches, which is deliberate (rule 147): it names a scale,
    # not one declaration.
    named = set(re.findall(r"`(--[a-z0-9-]+)`", skill))

    missing = sorted(t for t in named if t not in root)
    assert not missing, (
        "reaper-artifact names tokens that are gone from 00-tokens.css: "
        f"{missing}. A rename here breaks every mockup built from the skill; point the skill at "
        "the new token names, then re-read the contrast note beside them in the token file."
    )
    assert len(named) == _ARTIFACT_SKILL_TOKENS, (
        f"the skill names {len(named)} concrete tokens, expected {_ARTIFACT_SKILL_TOKENS}. If you "
        "added or removed one in the variables list, move this number with it; if you did not, a "
        f"token dropped out of the scan and is no longer checked (rule 145). Named: {sorted(named)}"
    )


def test_the_manual_states_the_ramp_the_shipped_policy_actually_uses() -> None:
    """The manual's signals table is the fourth copy of "what earns these points".

    The signal card's two bound boxes, the strip under them, and the why-panel row all derive
    the ramp from the policy the operator is holding. This table is written by hand, and rule
    144 is about exactly that pair: deriving three copies makes the fourth MORE dangerous, not
    less, because the derived ones are demonstrably right and vouch for a consistency nobody
    checked. It fails in the reassuring direction too, since a reader told a signal is worth
    10 points and not told it adds none of them above IMDb 6.0 concludes the wrong thing about
    their own library.

    So the figures are held against the shipped policies here rather than by a comment asking
    the next author to remember.
    """
    from reaper.clock import humanize_days
    from reaper.engine.policy import DEFAULT_MOVIE_POLICY, DEFAULT_TV_POLICY
    from reaper.engine.signals import SignalId

    page = (FRONTEND_SRC / "docs" / "content" / "understandingPolicy.ts").read_text()
    shipped = {
        s.signal: s for policy in (DEFAULT_MOVIE_POLICY, DEFAULT_TV_POLICY) for s in policy.signals
    }

    # Each phrase is BUILT from the shipped bound, so moving a default in `engine/policy.py`
    # fails here with the sentence the manual now has to carry.
    unwatched = shipped[SignalId.UNWATCHED]
    low_rating = shipped[SignalId.LOW_RATING]
    expected = {
        "unwatched": (
            f"Nothing until {humanize_days(unwatched.floor)}, "
            f"all of it at {humanize_days(unwatched.saturate_at)}"
        ),
        "few_watchers": (
            f"Nothing at {shipped[SignalId.FEW_WATCHERS].saturate_at} viewers or more, "
            "all of it at 0"
        ),
        "season_rank": (f"all of it at the {shipped[SignalId.SEASON_RANK].saturate_at}th-newest"),
        # Stored in tenths, said the way a person reads a rating.
        "low_rating": (
            f"Nothing at IMDb {low_rating.saturate_at / 10:.1f} or above, "
            f"all of it at IMDb {low_rating.floor / 10:.1f}"
        ),
    }

    missing = [
        f"  {name}: expected {phrase!r}" for name, phrase in expected.items() if phrase not in page
    ]
    assert not missing, (
        "frontend/src/docs/content/understandingPolicy.ts no longer states the ramp the "
        "shipped policy uses:\n"
        + "\n".join(missing)
        + "\nThe signal card, the strip and the why-panel row all derive this from the "
        "policy; this table is hand-written, so it is the copy that drifts (rule 144). "
        "Update the 'What it adds' column, and re-read frontend/src/components/signalRamp.ts "
        "so the manual and the app word the same bound the same way."
    )


_JOBS_PANEL_TSX = FRONTEND_SRC / "components" / "JobsPanel.tsx"
#: ``JOB_META`` read as the whole declaration up to the ``};`` that closes it, then picked
#: apart inside, rather than anchored on a delimiter one spelling happens to put there
#: (rule 147): a key may be a bare identifier or the computed ``[SCAN_ID]``, and both are
#: ordinary here.
_JOB_META_BLOCK = re.compile(r"const JOB_META: Record<string, JobMeta> = \{(.*?)\n\};", re.DOTALL)
_JOB_META_KEY = re.compile(r'^  (?:\[(\w+)\]|"?(\w+)"?):\s*\{', re.MULTILINE)
_SCAN_ID_CONST = re.compile(r'const SCAN_ID = "([\w]+)";')
#: Every entry carries exactly one ``title:``, so counting them counts the population the key
#: matcher is supposed to collect. A flag-shaped assertion cannot tell an entry that complies
#: from one this parser stopped seeing -- both read green (rule 145/147).
_JOB_META_TITLE = re.compile(r"^    title:", re.MULTILINE)


def test_every_scheduled_job_has_operator_copy_on_the_jobs_page() -> None:
    """A job the server schedules renders on the Jobs page with a title a person wrote.

    The list itself is the server's (rule 66): ``JobsPanel`` maps over the response, so a job
    added in ``scheduler.DEFAULT_MAINTENANCE_CRONS`` appears with no frontend edit at all --
    and ``jobMeta``'s fallback then prints the raw id as its title, so the operator reads
    "check_for_updates" where every neighbor reads a sentence, with no description and no
    off-warning under the switch that turns it off. Nothing failed when the nightly update
    check was added (#464), which is why this is a test and not a note: the fallback exists
    for the type checker, not as a shipping state.

    Both directions. A stale entry for a job that no longer exists is dead copy nobody will
    ever see, and it is the half a hand-maintained map keeps longest.
    """
    source = _JOBS_PANEL_TSX.read_text(encoding="utf-8")
    block_match = _JOB_META_BLOCK.search(source)
    assert block_match, (
        "parsed no JOB_META declaration out of JobsPanel.tsx -- the matcher is stale"
    )
    block = block_match.group(1)

    scan_id = _SCAN_ID_CONST.search(source)
    assert scan_id, "parsed no SCAN_ID constant out of JobsPanel.tsx -- the matcher is stale"
    constants = {"SCAN_ID": scan_id.group(1)}

    keys = {
        constants[computed] if computed else literal
        for computed, literal in _JOB_META_KEY.findall(block)
    }
    entries = len(_JOB_META_TITLE.findall(block))
    assert len(keys) == entries, (
        f"JOB_META holds {entries} entries but this test collected {len(keys)} keys. "
        "The key matcher missed a spelling -- fix it before trusting the comparison below, "
        "which cannot tell an entry that complies from one it never saw (rule 147)."
    )

    assert keys == set(SCHEDULABLE_JOB_IDS), (
        "frontend/src/components/JobsPanel.tsx's JOB_META and the jobs the server schedules "
        "disagree.\n"
        f"  scheduled with no copy: {sorted(set(SCHEDULABLE_JOB_IDS) - keys) or 'none'}\n"
        f"  copy for no such job:   {sorted(keys - set(SCHEDULABLE_JOB_IDS)) or 'none'}\n"
        "Add the title/desc/offWarning to JOB_META, or drop the stale entry. The off-warning "
        "states what stops happening when the job is off (rule 55)."
    )


# --- the layering CLAUDE.md's *Architecture* section describes ------------------------------

#: The four packages *Architecture* names, top of the stack first. An import that reaches a
#: package LATER in this tuple runs downward and is fine; one that reaches a package EARLIER
#: runs upward, and refusing that is the whole of the test below.
#:
#: **`clients` above `engine` is the one position the prose does not hand you**, and the live
#: edge is what fixes it: `clients/plex.py` takes `PlexFile`, `PlexItem`, `parse_guids` and
#: `to_basename` from `engine/identity.py`, whose own *Why this module is pure* section says it
#: holds data types and pure functions while the index builders that call Plex and Tautulli live
#: above it and hand the frozen types in. So that edge is the shape the module was designed for,
#: and the one that would be wrong is its reverse -- the decision engine reaching for a client,
#: which is zero today and which this ordering is what holds at zero.
#:
#: **Scoped to these four deliberately.** `notify` and `services` are a real runtime two-cycle
#: (`notify/discord.py` <-> `services/leaving_soon.py`), so a gate that swept every package under
#: `src/reaper` would be red the day it landed and would get deleted rather than fixed.
#:
#: That pair is a cycle between PACKAGES and not between modules: `discord.py` imports
#: `services/app_settings.py`, `leaving_soon.py` imports `discord.py`, and neither module
#: reaches itself. So `test_every_import_cycle_under_src_is_one_someone_declared` below does
#: sweep all of `src/reaper`, at module granularity, and the pair is not one of the two it
#: declares. The two gates answer different questions and both are wanted: this one holds a
#: direction across four packages, that one holds the module graph to a declared set of cycles.
_LAYERS = ("api", "services", "clients", "engine")

#: Every `.py` file under those four, which is the population the walk parses. It moves when a
#: module is added, split or deleted, and it is pinned because a walk that quietly stopped
#: reading the tree would satisfy every assertion below by finding nothing at all (rule 145).
_EXPECTED_LAYERED_MODULES = 84

#: Every ordered pair where one of the four imports another, reconciled by hand: all six
#: downward pairs are live, and no upward pair is. Asserted as an equality rather than a subset,
#: so a pair that goes to zero is a change someone declares here rather than one nobody sees.
_EXPECTED_LAYER_EDGES = frozenset(
    {
        ("api", "services"),
        ("api", "clients"),
        ("api", "engine"),
        ("services", "clients"),
        ("services", "engine"),
        ("clients", "engine"),
    }
)

#: Every cross-package import that does NOT run at module import time, by importing file, target
#: and how it is deferred. Written out rather than counted: a deferred import is how a layering
#: violation hides from a runtime graph, so a new one is a decision made here by hand, never a
#: number bumped to make a red test go green.
#:
#: **Empty, all three of wave 9's gone.** The last was `services/executor.py`'s `TYPE_CHECKING`
#: block, which named two symbols from `reaper.clients.plex` while the line above it imported
#: that same module at runtime, so the deferral hid nothing from any graph. Empty is the
#: interesting state for this set, not a broken one: every cross-package edge in the four
#: packages now runs at import time, so the runtime graph is the whole truth about them.
_DEFERRED_CROSS_PACKAGE_IMPORTS: frozenset[tuple[str, str, str]] = frozenset()


class _Edge(NamedTuple):
    """One import statement, resolved to the packages it leaves and reaches."""

    #: Repo-relative path of the importing file, posix-spelled.
    path: str
    lineno: int
    #: The importing package and the imported one, both members of `_LAYERS`.
    src: str
    dst: str
    #: The full dotted module the import names.
    target: str
    #: "" when the import runs at module import time, else how it is deferred.
    deferred: str


def _is_type_checking(test: ast.expr) -> bool:
    """``if TYPE_CHECKING:`` and ``if typing.TYPE_CHECKING:``, and nothing else.

    ``if not TYPE_CHECKING:`` is deliberately not one of them: it runs, so whatever it imports
    is a runtime edge however it is spelled.
    """
    if isinstance(test, ast.Name):
        return test.id == "TYPE_CHECKING"
    return isinstance(test, ast.Attribute) and test.attr == "TYPE_CHECKING"


def _deferred_import_lines(tree: ast.Module) -> dict[int, str]:
    """Line -> how it is deferred, for every import that does not run at module import time.

    Two spellings defer one and the tree uses both: inside a ``def``/``async def``, which runs
    on the first call, and inside ``if TYPE_CHECKING:``, which never runs. TYPE_CHECKING wins
    where they nest, since that import has no runtime existence to defer.
    """
    deferred: dict[int, str] = {}
    for scope in ast.walk(tree):
        if isinstance(scope, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for node in ast.walk(scope):
                if isinstance(node, (ast.Import, ast.ImportFrom)):
                    deferred[node.lineno] = "function-local"
    for scope in ast.walk(tree):
        if isinstance(scope, ast.If) and _is_type_checking(scope.test):
            for node in ast.walk(scope):
                if isinstance(node, (ast.Import, ast.ImportFrom)):
                    deferred[node.lineno] = "TYPE_CHECKING"
    return deferred


def _imported_modules(node: ast.Import | ast.ImportFrom, package: str) -> list[str]:
    """Every dotted module ``node`` reaches, made absolute against its containing ``package``.

    ``import a.b`` and ``import a.b as c`` both name ``a.b``. ``from a.b import c`` names
    ``a.b``, the ``c`` being a symbol rather than a module in every case but one.

    **The exception is ``from reaper import services``**, where the `from` clause is the
    parent and the imported NAME is the package. Reading the clause alone made that edge
    vanish, which is a layering violation the gate reported as clean, and the idiom is live
    in the tree: `api/logs.py` and `api/settings.py` both spell it, and both are non-edges
    today only because `logbuffer` and `launcher` are not among the four. One package over
    and it is silent. So each name is checked too, whenever the clause resolves to `reaper`
    itself. `from a.b import c` is left alone: `c` there really is a symbol, and a package
    that deep would already have been named by the clause.

    A relative ``from . import x`` resolves against ``package``: the four hold none today,
    and one that arrives must not drop out of the walk unseen (rule 147).
    """
    if isinstance(node, ast.Import):
        return [alias.name for alias in node.names]
    base = node.module or ""
    if node.level:
        parent = package.rsplit(".", node.level - 1)[0]
        base = f"{parent}.{node.module}" if node.module else parent
    if base == "reaper":
        return [f"reaper.{alias.name}" for alias in node.names]
    return [base] if base else []


def _edges_in(source: str, path: str) -> list[_Edge]:
    """Every cross-package import in ``source``, for a file at repo-relative ``path``.

    Split out from the walk so the classifier can be run against the import forms the tree
    does not currently spell as well as the ones it does (rule 147).
    """
    parts = Path(path).with_suffix("").parts
    src = parts[1]
    # The containing package, which is what a relative import resolves against. The same
    # expression serves `__init__.py`, where `.` means the package the file *is* rather than
    # the one above it, because dropping the last part gets there from either side.
    package = ".".join(parts[:-1])
    tree = ast.parse(source)
    deferred = _deferred_import_lines(tree)
    edges: list[_Edge] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Import, ast.ImportFrom)):
            continue
        for target in _imported_modules(node, package):
            bits = target.split(".")
            if len(bits) < 2 or bits[0] != "reaper" or bits[1] not in _LAYERS:
                continue
            if bits[1] == src:
                continue
            edges.append(
                _Edge(path, node.lineno, src, bits[1], target, deferred.get(node.lineno, ""))
            )
    return edges


@lru_cache(maxsize=1)
def _layered_modules() -> tuple[tuple[str, str], ...]:
    """Every module under the four packages, as (repo-relative posix path, source)."""
    return tuple(
        (p.relative_to(SRC.parent).as_posix(), p.read_text(encoding="utf-8"))
        for layer in _LAYERS
        for p in sorted((SRC / layer).rglob("*.py"))
    )


@lru_cache(maxsize=1)
def _cross_package_edges() -> tuple[_Edge, ...]:
    """Every import that leaves one of the four packages for another."""
    return tuple(edge for path, source in _layered_modules() for edge in _edges_in(source, path))


def test_the_four_packages_import_only_downward() -> None:
    """CLAUDE.md's *Architecture* section describes a stack, and nothing held it to one.

    The claim it makes is a dependency direction: routers sit on services, services compose
    the decision engine and the HTTP clients, and neither of those two reaches back up. That
    is true today and was true only because everyone who touched it happened to keep it true.
    The failure it prevents is not a crash -- Python imports a cycle happily until it does
    not -- it is `engine/` growing a reason to know about `services/`, at which point the one
    place a fate is decided stops being separable from the code that acts on it.

    **The pinned module count is the load-bearing half** (rule 145). A direction assertion is
    a flag: it cannot tell a module that complies from one the walk never opened, so a scan
    scoped to the wrong root, or one that stopped parsing, reports a clean stack while reading
    nothing. Same for `_EXPECTED_LAYER_EDGES`, which is an equality for the same reason.

    The plan this landed under expected the test to skip deferred imports to be green on day
    one. Measured, it does not need to: every cross-package import that does not run at module
    import time runs downward, so they are held to the same rule as the rest and pinned by name
    in the test below.
    """
    modules = _layered_modules()
    assert len(modules) == _EXPECTED_LAYERED_MODULES, (
        f"expected {_EXPECTED_LAYERED_MODULES} modules under {'/, '.join(_LAYERS)}/, walked "
        f"{len(modules)}.\n\nIf you ADDED or DELETED one, bump the number -- AND the two prose\n"
        "copies of it, which nothing else asserts (rule 144): docs/SIMPLIFICATION_PLAN.md's S7\n"
        "paragraph names this constant and restates the figure, and its C3 checkpoint row does\n"
        "too. Both were already stale by two when this message was written. Leave the *Landed*\n"
        "rows alone -- their figures are historical deltas, and editing one makes a correct\n"
        "record false.\n"
        "`_EXPECTED_SOURCE_MODULES` moves with the same module, counting all of src/reaper\n"
        "rather than these four, and it fails separately rather than telling you about this.\n"
        "If you did not add or delete one, the walk lost part of the tree -- and every\n"
        "assertion below passes on what it cannot see."
    )
    rank = {layer: i for i, layer in enumerate(_LAYERS)}
    upward = [e for e in _cross_package_edges() if rank[e.dst] < rank[e.src]]
    assert not upward, (
        "these imports run UP the stack:\n"
        + "\n".join(f"  {e.path}:{e.lineno}  {e.src} -> {e.dst}  ({e.target})" for e in upward)
        + "\n\nThe order is "
        + " -> ".join(_LAYERS)
        + ", and a package may only import one to its right. If the\n"
        "import is right and the order is wrong, move _LAYERS and say why -- but read\n"
        "engine/identity.py's 'Why this module is pure' section first: it is the reason the\n"
        "decision engine is at the bottom and takes nothing from anywhere."
    )
    found = frozenset((e.src, e.dst) for e in _cross_package_edges())
    assert found == _EXPECTED_LAYER_EDGES, (
        "the set of package pairs that import each other moved.\n"
        f"  new:  {sorted(found - _EXPECTED_LAYER_EDGES) or 'none'}\n"
        f"  gone: {sorted(_EXPECTED_LAYER_EDGES - found) or 'none'}\n\n"
        "A new pair is a layer boundary being crossed for the first time; one that went away\n"
        "is a dependency the stack no longer has. Both are worth a sentence in the pull\n"
        "request, and both are declared here rather than discovered later."
    )


def test_every_deferred_cross_package_import_is_named() -> None:
    """An import that does not run at module import time is invisible to a runtime graph.

    That is what makes the escape hatch worth pinning rather than counting. `if TYPE_CHECKING:`
    and a `def`-local import are both legitimate -- they are how a genuine cycle gets broken --
    and they are also exactly where a layering violation would go to hide, since the module
    graph a tool draws does not have the edge at all.

    `docs/SIMPLIFICATION_PLAN.md`'s wave 9 measured all three of these and found that none
    breaks the cycle it looks like it was written for. All three are gone. This list is what
    made that deletion visible: without it the walk skips the sites, the count never moves, and
    the gate is blind to the one change it exists to watch.

    **Empty, and still a live assertion.** It fires on any deferred cross-package import that
    arrives. What it cannot notice on its own is a walk that collects nothing, since an empty
    walk equals an empty expectation; `test_the_four_packages_import_only_downward` is what
    covers that, pinning the module count and a non-empty edge set off the same walk.
    """
    deferred = frozenset(
        (e.path, e.target, e.deferred) for e in _cross_package_edges() if e.deferred
    )
    assert deferred == _DEFERRED_CROSS_PACKAGE_IMPORTS, (
        "the deferred cross-package imports moved.\n"
        f"  new:  {sorted(deferred - _DEFERRED_CROSS_PACKAGE_IMPORTS) or 'none'}\n"
        f"  gone: {sorted(_DEFERRED_CROSS_PACKAGE_IMPORTS - deferred) or 'none'}\n\n"
        "The set is empty, so anything here at all is NEW, and a new one needs a\n"
        "reason written down: it is a cross-package dependency that no import graph will show,\n"
        "so if it is here to break a cycle, name the cycle.\n"
        "docs/SIMPLIFICATION_PLAN.md's S7 paragraph restates this set's size in prose, and\n"
        "nothing asserts that copy (rule 144)."
    )


def test_the_import_classifier_reads_every_form_the_tree_spells_an_import() -> None:
    """The walk above is worth what its parser can resolve, so the forms are run, not assumed.

    Rule 147: a matcher ships with the spellings it accepts AND the ones it rejects. The
    relative forms are the sharp ones -- the four packages hold none today, so nothing in the
    tree would notice `from ..engine import x` silently resolving to nothing and dropping out
    of the walk, which is a layering violation the gate reports as clean.
    """
    cases = {
        "from reaper.engine.gates import Facts": ("engine", "reaper.engine.gates", ""),
        "from reaper.engine import gates": ("engine", "reaper.engine", ""),
        "import reaper.clients.plex": ("clients", "reaper.clients.plex", ""),
        "import reaper.clients.plex as plex": ("clients", "reaper.clients.plex", ""),
        "from ..engine.gates import Facts": ("engine", "reaper.engine.gates", ""),
        "from ..engine import gates": ("engine", "reaper.engine", ""),
        # The `from` clause is the PARENT, so the package is the imported name. Reading the
        # clause alone dropped this edge entirely, and `from reaper import <name>` is spelled
        # in two of the four packages already -- for modules outside them, which is the only
        # reason it was not a live hole.
        "from reaper import engine": ("engine", "reaper.engine", ""),
        "from .. import engine": ("engine", "reaper.engine", ""),
        "from reaper import engine as e": ("engine", "reaper.engine", ""),
        "if TYPE_CHECKING:\n    from reaper.engine import gates": (
            "engine",
            "reaper.engine",
            "TYPE_CHECKING",
        ),
        "if typing.TYPE_CHECKING:\n    from reaper.engine import gates": (
            "engine",
            "reaper.engine",
            "TYPE_CHECKING",
        ),
        "def f():\n    from reaper.engine import gates": (
            "engine",
            "reaper.engine",
            "function-local",
        ),
        "async def f():\n    from reaper.engine import gates": (
            "engine",
            "reaper.engine",
            "function-local",
        ),
        "class C:\n    def m(self):\n        from reaper.engine import gates": (
            "engine",
            "reaper.engine",
            "function-local",
        ),
        # It runs, so it is a runtime edge whatever the condition is called.
        "if not TYPE_CHECKING:\n    from reaper.engine import gates": (
            "engine",
            "reaper.engine",
            "",
        ),
        # A class body runs at import time too.
        "class C:\n    from reaper.engine import gates": ("engine", "reaper.engine", ""),
    }
    for source, (dst, target, deferred) in cases.items():
        edges = _edges_in(source, "reaper/services/thing.py")
        assert len(edges) == 1, f"expected one edge from {source!r}, got {edges}"
        assert (edges[0].dst, edges[0].target, edges[0].deferred) == (dst, target, deferred), (
            f"{source!r} classified as {edges[0].dst}/{edges[0].target}/"
            f"{edges[0].deferred!r}, expected {dst}/{target}/{deferred!r}"
        )

    # And the forms that are correctly NOT an edge: the package importing itself, a package
    # outside the four, and a third-party module whose name merely starts the same way.
    for source in (
        "from reaper.services.planner import MediaRef",
        "from reaper.notify.discord import DiscordNotifier",
        "from reaper.config import Settings",
        "import structlog",
        # The `from reaper import <name>` arm above must not turn a module OUTSIDE the four
        # into an edge. Both of these are spelled in the tree today.
        "from reaper import logbuffer",
        "from reaper import launcher, crypto",
    ):
        assert not _edges_in(source, "reaper/services/thing.py"), f"should be no edge: {source}"


# --- the import cycles under src/reaper are declared, not discovered -----------------------

#: Every `.py` file under `src/reaper`, which is the population the cycle walk parses. Pinned
#: for the reason `_EXPECTED_LAYERED_MODULES` is (rule 145): a walk that stopped reading the
#: tree finds no cycles at all, and the assertion below cannot tell that from a clean graph.
#: A different population from that constant, which counts the 84 under the four packages only.
_EXPECTED_SOURCE_MODULES = 116

#: Every import cycle under `src/reaper`, each rotated to start at its smallest member. Two,
#: and both are one edge: `api/settings.py` imports `reaper.launcher` at module level, `launcher`
#: reaches `main` to serve, and `main` mounts every router. Written out rather than counted,
#: because a cycle is a coupling someone chose and the next reader needs to know which two.
#:
#: **That edge is two function calls, not two constants**, so it is not another one-string move:
#: `_desktop_out` calls `launcher.desktop_platform()` and the desktop save calls
#: `launcher.write_conf_values()`. Breaking it means moving behavior, not a name.
#:
#: There were **nine** before `LAUNCHER_CONF_NAME` moved to `config.py`; the seven that went
#: were `services/backup.py` and `services/restore.py` importing the process entry point for a
#: filename. Nothing failed when they were there, which is why this list exists.
_KNOWN_IMPORT_CYCLES = frozenset(
    {
        ("reaper.api.plex", "reaper.api.settings", "reaper.launcher", "reaper.main"),
        ("reaper.api.settings", "reaper.launcher", "reaper.main"),
    }
)


def _module_candidates(node: ast.Import | ast.ImportFrom, package: str) -> list[str]:
    """Every dotted name ``node`` could be naming, made absolute against ``package``.

    `_imported_modules` above answers which PACKAGE an import reaches, which is all the
    layering walk needs. This needs the MODULE, so each imported name is offered as a
    submodule too: `from reaper.services import app_settings` reaches
    `services/app_settings.py`, and reading the `from` clause alone stops at the package's
    empty `__init__.py` and loses the edge. Names that are symbols rather than modules
    resolve to nothing and drop out in `_module_import_graph`.
    """
    if isinstance(node, ast.Import):
        return [alias.name for alias in node.names]
    base = node.module or ""
    if node.level:
        parent = package.rsplit(".", node.level - 1)[0]
        base = f"{parent}.{node.module}" if node.module else parent
    if not base:
        return []
    return [base, *(f"{base}.{alias.name}" for alias in node.names)]


@lru_cache(maxsize=1)
def _module_import_graph() -> dict[str, frozenset[str]]:
    """Every module under `src/reaper`, mapped to the in-tree modules it imports at RUNTIME.

    Top-level and function-local imports both count; `if TYPE_CHECKING:` does not, because it
    never runs and cannot make a cycle real. **Counting the function-local ones is the whole
    reason this gate works**: the top-level graph alone is acyclic today and stays acyclic
    when the cycles below are put back, since `launcher.main()` imports `reaper.preflight`
    and `reaper.main` from inside the function. A cycle broken by deferring one edge is still
    a cycle, and the deferral is what a module graph drawn from top-level imports cannot see.

    A package's `__init__.py` is a module here like any other, because importing
    `reaper.services.app_settings` executes `reaper/services/__init__.py` as well. All eight
    of them import nothing today, so they carry no outgoing edges.
    """
    names = {}
    for path in sorted(SRC.rglob("*.py")):
        parts = list(path.relative_to(SRC.parent).with_suffix("").parts)
        if parts[-1] == "__init__":
            parts.pop()
        names[".".join(parts)] = path
    graph: dict[str, frozenset[str]] = {}
    for name, path in names.items():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        deferred = _deferred_import_lines(tree)
        package = name if path.name == "__init__.py" else name.rsplit(".", 1)[0]
        reached: set[str] = set()
        for node in ast.walk(tree):
            if not isinstance(node, (ast.Import, ast.ImportFrom)):
                continue
            if deferred.get(node.lineno) == "TYPE_CHECKING":
                continue
            for candidate in _module_candidates(node, package):
                if candidate in names and candidate != name:
                    reached.add(candidate)
        graph[name] = frozenset(reached)
    return graph


def _import_cycles(graph: dict[str, frozenset[str]]) -> set[tuple[str, ...]]:
    """Every simple cycle in ``graph``, each rotated to start at its smallest member.

    Enumerated from the smallest member outward, so each cycle is found exactly once and the
    result needs no de-duplication. Measured at 47ms over this tree's 115 modules and 667
    edges; the graph is a DAG plus two back edges, which is what keeps a walk over simple
    paths cheap.
    """
    order = {node: i for i, node in enumerate(sorted(graph))}
    found: set[tuple[str, ...]] = set()

    def walk(start: str, node: str, path: list[str]) -> None:
        for nxt in sorted(graph[node]):
            if nxt == start:
                found.add(tuple(path))
            elif nxt not in path and order[nxt] > order[start]:
                walk(start, nxt, [*path, nxt])

    for start in sorted(graph):
        walk(start, start, [start])
    return found


def test_every_import_cycle_under_src_is_one_someone_declared() -> None:
    """Nothing held the module graph, and a returning cycle costs one import line to add.

    Measured against this tree: `services/backup.py` and `services/restore.py` imported
    `reaper.launcher` for one filename, and that single edge was **7 of the 9** cycles under
    `src/reaper`. Putting `LAUNCHER_CONF_NAME` in `config.py` removed all 7, and re-adding
    either import restores them.

    **Nothing failed while they were there, and three gates were looking.** The layering walk
    above reads the four packages only and `reaper.launcher` is outside them; the
    deferred-import gate reads cross-package imports that do not run, and these run; and a
    graph built from top-level imports alone is acyclic either way, because the edges that
    close every one of these cycles are the function-local imports in `launcher.main()`.

    Python imports a cycle happily until it does not. The failure this prevents is the one a
    review pass measured in phase 6: a five-module cut whose graph would not have booted, and
    which read as clean right up to the boot.

    **It excludes ``TYPE_CHECKING`` edges, so a cycle broken by a type-only import is invisible
    to it** (rule 147). That is deliberate -- those imports do not run, so they cannot fail a
    boot -- but it means the walk is not an inventory of couplings. ``services/lists.py`` and
    ``services/list_config.py`` are the live example: they import each other, one of them under
    ``TYPE_CHECKING``, and promoting that one raises ``ImportError`` on three modules. Nothing
    here would say so, and the deferred-import gate skips it too for being same-package.
    """
    graph = _module_import_graph()
    assert len(graph) == _EXPECTED_SOURCE_MODULES, (
        f"expected {_EXPECTED_SOURCE_MODULES} modules under src/reaper/, walked {len(graph)}.\n\n"
        "If you ADDED or DELETED one, bump the number. `_EXPECTED_LAYERED_MODULES` counts a\n"
        "narrower population (the four packages only) and moves separately.\n"
        "If you did not, the walk lost part of the tree, and the assertion below passes on\n"
        "what it cannot see."
    )
    found = _import_cycles(graph)
    assert found == _KNOWN_IMPORT_CYCLES, (
        "the import cycles under src/reaper moved.\n"
        + "  new:  "
        + (
            "\n        ".join(" -> ".join(c) for c in sorted(found - _KNOWN_IMPORT_CYCLES))
            or "none"
        )
        + "\n  gone: "
        + (
            "\n        ".join(" -> ".join(c) for c in sorted(_KNOWN_IMPORT_CYCLES - found))
            or "none"
        )
        + "\n\nOne that went away is wave 9 landing, and the entry comes out. A NEW one is a\n"
        "coupling to break where the dependency is wrong, never by deferring the import:\n"
        "a function-local or `TYPE_CHECKING` import leaves the coupling and hides the edge.\n"
        "Where a constant or a type is the whole reason for the edge, move it to a leaf both\n"
        "sides already import. `config.LAUNCHER_CONF_NAME` is what that looks like."
    )


def test_the_cycle_walk_reports_the_cycles_it_is_given() -> None:
    """The gate above is mostly an absence, so the detector is driven (rule 145).

    A walk that returned nothing would be green on any tree with no cycles left to lose, and
    two of these shapes are ones a wrong detector reports: a diamond has two paths to one
    module and no cycle, and a self-edge-free chain has neither.

    The classifier gets the same treatment (rule 147). `from reaper.services import
    app_settings` is the form that decides whether this graph has module edges at all, and
    reading the `from` clause alone resolves it to the package's empty `__init__.py`.
    """
    two = {"a": frozenset({"b"}), "b": frozenset({"a"})}
    assert _import_cycles(two) == {("a", "b")}
    long_way = {
        "a": frozenset({"b"}),
        "b": frozenset({"c"}),
        "c": frozenset({"a"}),
        "d": frozenset(),
    }
    assert _import_cycles(long_way) == {("a", "b", "c")}
    # Two cycles sharing a module, so a detector that stops at the first one is short here.
    figure_eight = {
        "a": frozenset({"b"}),
        "b": frozenset({"a", "c"}),
        "c": frozenset({"b"}),
    }
    assert _import_cycles(figure_eight) == {("a", "b"), ("b", "c")}
    diamond = {
        "a": frozenset({"b", "c"}),
        "b": frozenset({"d"}),
        "c": frozenset({"d"}),
        "d": frozenset(),
    }
    assert _import_cycles(diamond) == set()
    assert _import_cycles({"a": frozenset({"b"}), "b": frozenset()}) == set()

    forms = {
        "from reaper.services import app_settings": "reaper.services.app_settings",
        "from reaper.config import Settings": "reaper.config",
        "import reaper.clients.plex": "reaper.clients.plex",
        "from reaper import launcher": "reaper.launcher",
        "from ..engine import gates": "reaper.engine.gates",
        "from ..engine.gates import Facts": "reaper.engine.gates",
    }
    modules = set(_module_import_graph())
    for source, expected in forms.items():
        node = ast.parse(source).body[0]
        assert isinstance(node, (ast.Import, ast.ImportFrom))
        resolved = [c for c in _module_candidates(node, "reaper.services") if c in modules]
        assert expected in resolved, f"{source!r} resolved to {resolved}, expected {expected}"


# --- the frontend has no import cycles at all ----------------------------------------------

#: Every `.ts`/`.tsx` file under `frontend/src`, which is the population the walk below parses.
#: Pinned for `_EXPECTED_SOURCE_MODULES`' reason (rule 145), and it carries more weight here:
#: the expected cycle set is EMPTY, so a walk that stopped reading the tree agrees with a clean
#: graph exactly.
_EXPECTED_FRONTEND_MODULES = 214

#: The two extensions a module in this tree can carry, and the only ones the walk resolves to.
_TS_SUFFIXES = (".ts", ".tsx")

#: A static `import`/`export` of a relative specifier, in every spelling the tree uses: a bare
#: side-effect import, a default, a braced list running over several lines, and a re-export.
#: The body may not cross a quote, a backtick, a paren or a semicolon, so it cannot run out of
#: its own statement into the next one's string. Anchored at a line start or a `;`, since
#: prettier puts every statement on its own line and the `;` arm is the belt for a file that
#: somehow arrives unformatted.
#:
#: `import type` and `export type` are left out, and under `verbatimModuleSyntax` (set in
#: `frontend/tsconfig.json`) that is the exact line the compiler draws: the `type` STATEMENT is
#: erased, and `import { type A } from "./x"` emits `import {} from "./x"`, a real runtime edge
#: this therefore counts. Measured on this tree, putting the type-only edges back changes
#: nothing: both spellings of the walk found the same two cycles before wave 9 and find none now.
#:
#: It runs over `_without_comments`, so a commented-out import is not an edge. Reading a
#: quotation of an import as a real one is fail-CLOSED, a cycle nobody wrote, which is the
#: harmless direction and still a false red somebody has to chase.
_TS_STATIC_IMPORT = re.compile(
    r"""(?m)(?:^|;)[ \t]*(?:import|export)\s+(?!type[\s{])"""
    r"""(?:[^'"`();]*?\bfrom\s*)?['"](\.[^'"]+)['"]"""
)

#: `await import("./x")`, which is a runtime edge like any other: `App.tsx` reaches five of the
#: six routes this way, and the policy editor is reached by nothing else in that file.
#:
#: Three spellings the first version of this missed, all of them fail-OPEN, which is the
#: direction that loses a cycle rather than inventing one: Vite's documented
#: `import(/* @vite-ignore */ "./x")`, a backtick specifier, and `typeof` separated from
#: `import(` by anything but one space. `typeof` is matched and discarded rather than excluded
#: by a lookbehind, because Python's lookbehind is fixed-width and `typeof\n  import("./x")` is
#: legal.
_TS_DYNAMIC_IMPORT = re.compile(
    r"""(?P<typeof>\btypeof\s+)?\bimport\(\s*(?:/\*.*?\*/\s*)?['"`](\.[^'"`]+)['"`]""",
    re.S,
)


def _ts_module_key(path: Path) -> str:
    """``frontend/src``-relative, extension dropped: `components/PolicyEditor`."""
    return path.relative_to(FRONTEND_SRC).with_suffix("").as_posix()


@lru_cache(maxsize=1)
def _frontend_import_graph() -> dict[str, frozenset[str]]:
    """Every module under `frontend/src`, mapped to the in-tree modules it imports at RUNTIME.

    Test files are in the population rather than filtered out. A component never imports a
    test, so they close no cycle, and leaving them in means no skip list to keep current.

    **A specifier resolves the way the bundler resolves it, and the near-miss is the bug.** The
    candidates are the specifier itself *only when it already spells a module extension*, then
    the specifier plus each extension, then an `index` barrel under it. Taking the bare
    specifier unconditionally and stripping a suffix off it is what the first version did, and
    `./dissolve.generated` then resolved to `brand/dissolve`: three modules carried an edge
    their source does not have, harmless only while that file stays a leaf. `./index.css`
    resolving to nothing was the same accident wearing the right answer.

    There are no barrels in the tree today, so that pair of candidates is what stops a future
    one from silently dropping its edges. Every candidate is checked for containment before it
    is made relative: `resolveJsonModule` is on and two files already import JSON, so a
    specifier reaching out of `frontend/src` is ordinary and must drop out rather than raise.
    """
    paths = [
        path
        for path, _ in _repo_text_files()
        if path.suffix in _TS_SUFFIXES and path.is_relative_to(FRONTEND_SRC)
    ]
    files = {_ts_module_key(path): path for path in paths}
    # `announce.ts` beside `announce.tsx` is one key for two files: one module's imports are
    # never parsed and the other's are silently replaced. The pinned count below counts KEYS and
    # cannot see it, so the collision is caught here instead of arriving as a missing edge.
    collided = sorted(k for k, n in Counter(_ts_module_key(p) for p in paths).items() if n > 1)
    assert not collided, (
        f"two files under frontend/src share a module key: {collided}. One of them is missing "
        "from the graph entirely and the other's edges were overwritten. Rename one, or teach "
        "`_ts_module_key` to keep the extension."
    )
    graph: dict[str, frozenset[str]] = {}
    for name, path in files.items():
        text = _without_comments(path.read_text(encoding="utf-8"))
        specs = [m.group(1) for m in _TS_STATIC_IMPORT.finditer(text)]
        specs += [m.group(2) for m in _TS_DYNAMIC_IMPORT.finditer(text) if not m.group("typeof")]
        reached = set()
        for spec in specs:
            base = (path.parent / spec).resolve()
            candidates = [base] if base.suffix in _TS_SUFFIXES else []
            candidates += [base.with_name(base.name + s) for s in _TS_SUFFIXES]
            candidates += [base / f"index{s}" for s in _TS_SUFFIXES]
            for candidate in candidates:
                if not candidate.is_relative_to(FRONTEND_SRC):
                    continue
                target = _ts_module_key(candidate)
                if target in files and target != name:
                    reached.add(target)
        graph[name] = frozenset(reached)
    return graph


def test_the_frontend_has_no_import_cycles() -> None:
    """The SPA had two, and each was one borrowed symbol.

    `PolicyRuleEditors` was lifted out of `PolicyEditor` and left three lookup tables behind,
    so the new module imported them back out of the file it had just left; the same import
    also carried `humanDays`, which `PolicyEditor` re-exported after the function itself had
    already moved to `format.ts` to break a different cycle. `UnmatchedList` reached into
    `ScalesPanel` for a twelve-line poster placeholder, while `ScalesPanel` renders
    `UnmatchedList`. Both are gone, and the expected set is empty rather than declared: a
    browser bundle tolerates a cycle until an initialization order changes under it, and
    nothing here needs one.

    The Python twin above declares its two instead, because those are a coupling someone chose
    and cannot cheaply undo. The walk is shared: this builds the graph, `_import_cycles` finds
    the cycles, and `test_the_cycle_walk_reports_the_cycles_it_is_given` is what proves the
    finder is not simply returning nothing.
    """
    graph = _frontend_import_graph()
    assert len(graph) == _EXPECTED_FRONTEND_MODULES, (
        f"expected {_EXPECTED_FRONTEND_MODULES} .ts/.tsx files under frontend/src/, walked "
        f"{len(graph)}.\n\n"
        "If you ADDED or DELETED one, bump the number. If you did not, the walk lost part of\n"
        "the tree, and an empty expected cycle set is green on a graph it never read."
    )
    found = _import_cycles(graph)
    assert found == set(), (
        "a new import cycle under frontend/src:\n  "
        + "\n  ".join(" -> ".join(c) for c in sorted(found))
        + "\n\nBreak it where the dependency is wrong, never by deferring the import: a\n"
        "`import type` or a dynamic `import()` written to dodge this leaves the coupling and\n"
        "hides the edge. Where a constant, a type or one small component is the whole reason\n"
        "for the edge, move it to a leaf both sides already import.\n"
        "`components/PosterFallback.tsx` is what that looks like."
    )


def test_the_frontend_import_walk_reads_the_spellings_the_tree_uses() -> None:
    """The gate above is an absence, so the two matchers behind it are driven (rule 147).

    A regex is bounded by the syntax it parses. The synthetic half fixes what each spelling
    must resolve to, **including every one that must resolve to NOTHING** — those are the cases
    a matcher passes by doing nothing at all, so they are written out rather than assumed. The
    live half then asserts edges the real tree has, because a matcher can be right about a
    string and still never fire against a file: `App.tsx` reaches `ReviewQueue` statically and
    the policy editor only through `lazy(async () => (await import(...)))`, which is that file's
    one non-type reference to it, so those two edges are one proof each for the two matchers.

    **The fail-open cases are the ones worth the lines.** A spelling the dynamic matcher misses
    is a real runtime edge dropped, which is a cycle this gate then reports as absent, and three
    of them shipped in the first version: `import(/* @vite-ignore */ "./x")`, a backtick
    specifier, and `typeof` separated from `import(` by a newline. A spelling the static matcher
    over-reads is the other direction, a cycle nobody wrote, and those are listed too.
    """
    graph = _frontend_import_graph()
    assert "components/ReviewQueue" in graph["App"], (
        "the static-import matcher found no edge from App.tsx to the review queue, which it "
        "imports at the top of the file."
    )
    assert "components/PolicyEditor" in graph["App"], (
        "the dynamic-import matcher found no edge from App.tsx to the policy editor, which is "
        "reached only through `lazy(async () => (await import(...)))`."
    )

    static = {
        'import { PosterFallback } from "./PosterFallback";': ["./PosterFallback"],
        'import App from "./App";': ["./App"],
        'import "./index.css";': ["./index.css"],
        'export { humanDays } from "../format";': ["../format"],
        'import {\n  a,\n  b,\n} from "./x";': ["./x"],
        # Erased by the compiler, so it cannot make a cycle real.
        'import type { Focus } from "./navIntent";': [],
        'export type { Panel } from "./Settings";': [],
        # A value import that merely mentions `type` inline still emits, and still counts.
        'import { type Focus, goTo } from "./navIntent";': ["./navIntent"],
        # Not an import at all, and the shape a `from`-anchored matcher reads as one.
        'const cfg = { from: "./x" };': [],
        # Nor is a quotation of one inside a template literal, which the body's own character
        # class is what rejects: the backtick is excluded along with the quotes.
        'export const N = `copied from "./x"`;': [],
        # Two statements on one line, which prettier never writes and which the `;` arm of the
        # anchor is here for: reading only the first is an edge silently dropped.
        'export { a } from "./x"; export { b } from "./y";': ["./x", "./y"],
    }
    for source, expected in static.items():
        assert [m.group(1) for m in _TS_STATIC_IMPORT.finditer(source)] == expected, (
            f"the static-import matcher misread {source!r}"
        )
    # The block-comment case is the walk's, not the pattern's: the pattern has no way to see a
    # `/* */` around a line, and `_without_comments` is what takes it away first.
    commented = '/*\nimport { a } from "./x";\n*/'
    assert [m.group(1) for m in _TS_STATIC_IMPORT.finditer(commented)] == ["./x"]
    assert _TS_STATIC_IMPORT.search(_without_comments(commented)) is None, (
        "a commented-out import still reads as an edge, so the walk must not have stripped "
        "comments before matching."
    )

    dynamic = {
        'const m = await import("./components/Settings");': ["./components/Settings"],
        'type Api = typeof import("./api");': [],
        # Vite's documented escape hatch, which prettier keeps exactly where it is.
        'await import(/* @vite-ignore */ "./x");': ["./x"],
        "await import(`./x`);": ["./x"],
        # `typeof` is matched and discarded rather than excluded by a lookbehind, so the space
        # between the two words may be anything.
        'type A = typeof\n  import("./api");': [],
        'type B = typeof  import("./api");': [],
    }
    for source, expected in dynamic.items():
        assert [
            m.group(2) for m in _TS_DYNAMIC_IMPORT.finditer(source) if not m.group("typeof")
        ] == expected, f"the dynamic-import matcher misread {source!r}"


# --- the HTTP status an InstanceError means is declared once (rule 144) ---------------

#: Every `except` arm in `api/` that catches one of the `services.instances` errors. Pinned
#: because the ban below can only judge the handlers the walk collected, and a walk that
#: stopped reading the tree would pass by finding none (rule 145). Six today: five map the
#: error to a response, and `test_new_instance`'s arm reports it as `map_error` copy beside a
#: passed connection test instead, which is why the second number is not the first.
_EXPECTED_INSTANCE_ERROR_HANDLERS = 6
_EXPECTED_INSTANCE_ERROR_RESPONSES = 5

#: The three the walk recognizes, by the trailing name. Spelled as a suffix set rather than
#: matched on the dotted path, so `instances.InstanceError` and a bare `InstanceError` are one
#: case and a future subclass is caught by being named here rather than by being missed.
_INSTANCE_ERROR_NAMES = frozenset(
    {"InstanceError", "InstanceNotFoundError", "InstanceConflictError"}
)


def _names_an_instance_error(node: ast.expr | None) -> bool:
    """Whether an ``except`` clause catches one of them, in any form the tree may spell it.

    Four forms reach here: a bare ``InstanceError``, a dotted ``instances.InstanceError``, a
    tuple holding either beside an unrelated error (``(IntegrationError,
    instances.InstanceError)``, live in the tree), and a bare ``except:`` -- which names
    nothing and so catches these too, but cannot be told apart from any other blanket catch
    and is banned elsewhere. Reading the trailing name is what makes the first two one case;
    anchoring on the dotted spelling would have read only the one the tree happens to use
    (rule 147).
    """
    if node is None:
        return False
    if isinstance(node, ast.Tuple):
        return any(_names_an_instance_error(el) for el in node.elts)
    if isinstance(node, ast.Attribute):
        return node.attr in _INSTANCE_ERROR_NAMES
    return isinstance(node, ast.Name) and node.id in _INSTANCE_ERROR_NAMES


def _http_status_args(handler: ast.ExceptHandler) -> list[tuple[int, ast.expr]]:
    """The status argument of every ``HTTPException(...)`` raised inside ``handler``.

    Collected from the whole handler body rather than from its first statement, so an arm that
    branches before raising is read too, and returned in source order -- ``ast.walk`` is
    breadth-first, so a raise nested inside an ``if`` comes back after one written below it.

    **Both spellings of the status, because the tree spells it both ways**: positionally, which
    is what every arm here uses, and as ``status_code=``, which ``api/lists.py`` uses twice. A
    reader of the positional form alone does not report a keyword-spelled arm as a violation --
    it drops that arm out of the population entirely, so the count below fails saying an arm
    stopped answering when the truth is that it answered in a spelling nobody could read
    (rule 147).
    """
    out: list[tuple[int, ast.expr]] = []
    for node in ast.walk(handler):
        if not isinstance(node, ast.Raise) or node.exc is None:
            continue
        exc = node.exc
        if not isinstance(exc, ast.Call):
            continue
        callee = exc.func
        name = callee.attr if isinstance(callee, ast.Attribute) else getattr(callee, "id", "")
        if name != "HTTPException":
            continue
        keyword = next((kw.value for kw in exc.keywords if kw.arg == "status_code"), None)
        status = exc.args[0] if exc.args else keyword
        if status is not None:
            out.append((node.lineno, status))
    return sorted(out, key=lambda pair: pair[0])


def _instance_error_handlers() -> list[tuple[str, int, ast.ExceptHandler]]:
    """Every such arm across ``api/``, as (repo-relative path, line, node)."""
    found: list[tuple[str, int, ast.ExceptHandler]] = []
    for path in sorted((SRC / "api").rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ExceptHandler) and _names_an_instance_error(node.type):
                found.append((path.relative_to(REPO).as_posix(), node.lineno, node))
    return found


def test_no_route_hand_writes_the_status_an_instance_error_already_declares() -> None:
    """The status comes from ``exc.status``, never from a number typed at the ``except``.

    ``services.instances`` declares one status per subclass -- 422 for the base, 404 for
    not-found, 409 for a name clash -- and each subclass's docstring says why. The five arms
    that answer these used to hand-write the number instead, and two of them wrote 404 for the
    base class: correct only because those callees can raise nothing but ``InstanceNotFound``
    today, so a service that grew a blank-field guard the way ``create_instance`` has would
    have told the operator the instance did not exist. Reading the declaration is what makes
    that unable to happen again, and this is what keeps a sixth arm from reintroducing it
    (rule 144).
    """
    handlers = _instance_error_handlers()
    assert len(handlers) == _EXPECTED_INSTANCE_ERROR_HANDLERS, (
        f"{len(handlers)} `except InstanceError` arms under api/, expected "
        f"{_EXPECTED_INSTANCE_ERROR_HANDLERS}. Update the count once the new arm reads "
        f"`exc.status`: {[(p, n) for p, n, _ in handlers]}"
    )

    answered = 0
    for path, _, handler in handlers:
        for lineno, arg in _http_status_args(handler):
            answered += 1
            assert isinstance(arg, ast.Attribute) and arg.attr == "status", (
                f"{path}:{lineno} raises HTTPException with a hand-written status. "
                f"Use `exc.status`, which services/instances.py declares per subclass."
            )
    assert answered == _EXPECTED_INSTANCE_ERROR_RESPONSES, (
        f"{answered} of those arms answer with an HTTPException, expected "
        f"{_EXPECTED_INSTANCE_ERROR_RESPONSES}"
    )


def test_the_instance_error_matcher_reads_every_form_the_clause_can_take() -> None:
    """The four forms it must collect, and the ones it must leave alone (rule 147).

    The ban above is only as wide as this classifier: an arm it does not collect is absent
    from the count and from the assertion at once, so the two agree while disagreeing with the
    tree. Only one of the accepted forms is live in `api/` today.
    """
    accepted = (
        "except InstanceError as exc: pass",
        "except instances.InstanceError as exc: pass",
        "except (IntegrationError, instances.InstanceError) as exc: pass",
        "except (instances.InstanceNotFoundError, ValueError): pass",
        "except InstanceConflictError: pass",
        "except services.instances.InstanceError: pass",
    )
    for clause in accepted:
        tree = ast.parse(f"try:\n    pass\n{clause}")
        handler = next(n for n in ast.walk(tree) if isinstance(n, ast.ExceptHandler))
        assert _names_an_instance_error(handler.type), f"should be collected: {clause}"

    for clause in (
        "except IntegrationError as exc: pass",
        "except (ValueError, KeyError): pass",
        "except restore.RestoreError as exc: pass",
        # Catches one, and is indistinguishable from any other blanket catch.
        "except Exception: pass",
    ):
        tree = ast.parse(f"try:\n    pass\n{clause}")
        handler = next(n for n in ast.walk(tree) if isinstance(n, ast.ExceptHandler))
        assert not _names_an_instance_error(handler.type), f"should be left alone: {clause}"


def test_the_status_reader_finds_a_hand_written_one_wherever_it_sits() -> None:
    """``_http_status_args`` reads the raise, not the handler's first line.

    An arm that logs first, or branches before raising, still raises a status -- and a reader
    that only inspected ``handler.body[0]`` would call both of those clean. Driven against a
    nested raise, since that is the shape a future arm most plausibly takes, and against
    ``status_code=``, which is a live spelling in ``api/lists.py`` and reaches the ban only
    because the reader takes it (rule 147).
    """
    nested = ast.parse(
        "try:\n"
        "    pass\n"
        "except InstanceError as exc:\n"
        "    log.warning('x')\n"
        "    if exc.status:\n"
        "        raise HTTPException(404, str(exc)) from exc\n"
        "    raise HTTPException(status_code=409, detail=str(exc)) from exc\n"
    )
    handler = next(n for n in ast.walk(nested) if isinstance(n, ast.ExceptHandler))
    args = _http_status_args(handler)
    assert len(args) == 2, f"both raises should be read, got {len(args)}"
    (_, positional), (_, keyword) = args
    assert isinstance(positional, ast.Constant) and positional.value == 404
    assert isinstance(keyword, ast.Constant) and keyword.value == 409

    # And the compliant spellings, so the ban's accept side is driven too.
    clean = ast.parse(
        "try:\n"
        "    pass\n"
        "except InstanceError as exc:\n"
        "    raise HTTPException(exc.status, str(exc)) from exc\n"
    )
    handler = next(n for n in ast.walk(clean) if isinstance(n, ast.ExceptHandler))
    ((_, arg),) = _http_status_args(handler)
    assert isinstance(arg, ast.Attribute) and arg.attr == "status"

    # A raise that is not an HTTPException contributes nothing, so a `raise` re-raising the
    # original cannot be read as an arm that stopped answering.
    bare = ast.parse("try:\n    pass\nexcept InstanceError:\n    raise\n")
    handler = next(n for n in ast.walk(bare) if isinstance(n, ast.ExceptHandler))
    assert _http_status_args(handler) == []


# --- the bold-when-active strut, and the sentence enumerating it (rule 144) -----------

_STYLES = FRONTEND_SRC / "styles"

#: Selecting a control bumps its label 500 -> 600, which would widen it and shove its
#: neighbors sideways. A hidden bold copy of the label reserves the wider width at every
#: weight. `04-buttons.css` holds the rule for four of the five controls carrying one and a
#: comment enumerating all five; `02-masthead.css` holds `.view-tab`'s, byte-identical.
_STRUT_ENUMERATION = _STYLES / "04-buttons.css"

#: Every control that bolds when active or selected. Pinned because both assertions below
#: are set comparisons, and a walk that stopped reading the tree satisfies a set comparison
#: by finding nothing on both sides at once (rule 145).
_EXPECTED_BOLD_WHEN_ACTIVE = 6

#: The one that bolds and deliberately carries no strut, with the reason -- classified in
#: writing rather than silenced, since the assertion below is an equality and would otherwise
#: just be relaxed. A vertical list has no sideways neighbor to shove.
_BOLDS_WITHOUT_A_STRUT = {".filter-mi": "a vertical menu item, so nothing sits beside it"}

#: A selector carrying a chosen state. `.sel` and `.active` are both live spellings here.
_ACTIVE_STATE = re.compile(r"\.active\b|\.sel\b|\[aria-selected|\[data-active")
_CSS_RULE = re.compile(r"(?m)^([^{}/@]+?)\{([^{}]*)\}")
#: The leading class of a selector, which is the control the rule is about.
_LEAD_CLASS = re.compile(r"^\s*(\.[a-z0-9-]+)")


def _css_rules() -> list[tuple[str, int, str, str]]:
    """Every flat CSS rule under `styles/`, as (file, line, selector, body).

    Flat, so a rule nested in `@media` is read too -- ``10-layout.css`` turns `.view-tab`'s
    strut back off inside one, and a walk that skipped media queries would not see it.
    """
    out: list[tuple[str, int, str, str]] = []
    for path in sorted(_STYLES.glob("*.css")):
        text = path.read_text(encoding="utf-8")
        for match in _CSS_RULE.finditer(text):
            line = text[: match.start()].count("\n") + 1
            out.append((path.name, line, " ".join(match.group(1).split()), match.group(2)))
    return out


def _lead_classes(selector: str) -> set[str]:
    """The control each comma-separated part of ``selector`` is about."""
    found = set()
    for part in selector.split(","):
        lead = _LEAD_CLASS.match(part)
        if lead:
            found.add(lead.group(1))
    return found


def _bolding_and_strutted() -> tuple[set[str], set[str]]:
    """(controls that bold when chosen, controls carrying a strut), by leading class."""
    bolding: set[str] = set()
    strutted: set[str] = set()
    for _, _, selector, body in _css_rules():
        if "font-weight" in body and _ACTIVE_STATE.search(selector):
            bolding |= _lead_classes(selector)
        # `content: none` is the phone bar turning the strut OFF, not a control carrying one.
        if "[data-label]" in selector and "::after" in selector and "content: none" not in body:
            strutted |= _lead_classes(selector)
    return bolding, strutted


#: The sentence in ``04-buttons.css`` that makes the claim. The matcher anchors on it rather
#: than on the comment block, and the block rather than the file, because both wider readings
#: are satisfied by prose: the file mentions `.seg2` and `.intent-band` in unrelated comments,
#: and the block's own closing paragraph names `.view-tab` while narrating the drift -- so
#: deleting the name from the list left the test green (rule 147). Reading the claim itself is
#: the only scope where a name's presence means what the test says it means.
_STRUT_CLAIM_OPENER = "Applied to every control that bolds when active"


def _strut_comment() -> str:
    """The enumeration sentence, from its opener to the end of that paragraph."""
    text = _STRUT_ENUMERATION.read_text(encoding="utf-8")
    start = text.find(_STRUT_CLAIM_OPENER)
    assert start != -1, (
        f"{_STRUT_ENUMERATION.name} no longer contains the sentence enumerating the "
        f"bold-width strut ({_STRUT_CLAIM_OPENER!r}). Restore it or retarget this gate: "
        f"without it nothing reconciles the comment against the selectors"
    )
    end = text.find("\n\n", start)
    return text[start : end if end != -1 else len(text)]


def test_every_control_that_bolds_when_chosen_reserves_the_bold_width() -> None:
    """A control that bolds on selection carries the strut, or is classified as not needing it.

    Without one the label widens on click and shoves its neighbors sideways. The exemption is
    written out rather than the assertion relaxed, so a sixth control that quietly stops
    reserving its width fails here instead of being covered by a subset check.
    """
    bolding, strutted = _bolding_and_strutted()
    assert len(bolding) == _EXPECTED_BOLD_WHEN_ACTIVE, (
        f"{len(bolding)} controls bold when chosen, expected {_EXPECTED_BOLD_WHEN_ACTIVE}: "
        f"{sorted(bolding)}. A new one either gets a `[data-label]::after` strut or a line in "
        f"_BOLDS_WITHOUT_A_STRUT saying why it does not need one"
    )
    assert bolding - strutted == set(_BOLDS_WITHOUT_A_STRUT), (
        f"unstrutted: {sorted(bolding - strutted)}, exempt: {sorted(_BOLDS_WITHOUT_A_STRUT)}"
    )


def test_the_strut_comment_names_every_control_that_carries_one() -> None:
    """The enumeration in ``04-buttons.css`` and the selectors are one fact, checked both ways.

    It claimed to cover "every control that bolds when active" and listed four, while five
    carried a strut: `.view-tab`'s rule lives in ``02-masthead.css``, so the author of either
    file could read their own and be right. That is rule 144's shape -- one sentence about what
    the app does, stated where the code it describes is not -- and the direction it failed in
    is the reassuring one, since a reader checking whether a control needs a strut was told the
    list was complete.

    Both directions, because they fail differently: a strutted control missing from the
    sentence is the drift that happened, and a name in the sentence with no rule behind it is a
    strut someone deleted while leaving the claim standing (rule 7/24).
    """
    _, strutted = _bolding_and_strutted()
    named = {f".{name}" for name in re.findall(r"`\.([a-z0-9-]+)`", _strut_comment())}

    missing = strutted - named
    assert not missing, (
        f"{_STRUT_ENUMERATION.name} enumerates the controls carrying the bold-width strut and "
        f"does not name {sorted(missing)}. Add them to the comment above "
        f"`.tab[data-label]::after`, or the next author reads a list that says it is complete"
    )
    claimed = named - set(_BOLDS_WITHOUT_A_STRUT)
    assert claimed <= strutted, (
        f"{_STRUT_ENUMERATION.name} names {sorted(claimed - strutted)} as carrying a strut, and "
        f"no `[data-label]::after` rule does"
    )


def test_the_css_walk_reads_the_forms_the_stylesheets_spell(tmp_path: Path) -> None:
    """The two assertions above are only as wide as this walk (rule 147).

    Driven against the spellings `styles/` uses -- a grouped selector, a rule nested in a media
    query, a state class other than `.active` -- and against the one that must NOT count, the
    phone bar's `content: none`, which turns a strut off rather than declaring one. A walk that
    counted that would report `.view-tab` strutted on a build where it is not.
    """
    assert _lead_classes(".tab[data-label]::after, .seg[data-label]::after") == {".tab", ".seg"}
    assert _lead_classes(".view-tab.active:hover:not(:disabled)") == {".view-tab"}
    assert _lead_classes(".filter-mi.sel") == {".filter-mi"}
    assert _lead_classes("  .settings-tab[data-label]::after") == {".settings-tab"}

    scratch = tmp_path / "styles"
    scratch.mkdir()
    (scratch / "x.css").write_text(
        ".a.active {\n  font-weight: var(--weight-semi);\n}\n"
        "@media (max-width: 40rem) {\n"
        "  .a[data-label]::after {\n    content: none;\n  }\n}\n"
        ".b[data-label]::after {\n  content: attr(data-label);\n}\n"
    )
    global _STYLES
    real, _STYLES = _STYLES, scratch
    try:
        bolding, strutted = _bolding_and_strutted()
    finally:
        _STYLES = real
    assert bolding == {".a"}, bolding
    # `.a`'s only `[data-label]` rule is the media query switching it off, so it is not strutted.
    assert strutted == {".b"}, strutted


# ---------------------------------------------------------------------------
# Rule 94: a scan-sized ``IN`` is chunked
# ---------------------------------------------------------------------------

#: The membership operators SQLAlchemy spells a ``WHERE col IN (...)`` with. ``not_in`` and
#: its legacy ``notin_`` alias expand identically -- one bound variable per element -- so a
#: ban that read only ``in_`` would miss half the population it claims to cover (rule 147).
_MEMBERSHIP_CALLS = frozenset({"in_", "not_in", "notin_"})

#: The third spelling, and the one that hides: a placeholder list built by hand and pasted into
#: the SQL, which is neither an ORM operator nor an ``expanding`` bindparam.
#: ``imdb_dataset.lookup`` does exactly this. A walk collecting only the first two reported 17
#: functions and had no count to pin for the eighteenth, which is rule 147's shape -- a form
#: that never enters the walk is missing from the ban and from the count alike.
#:
#: Read against string literals only, so the ``#:`` comments spelling ``IN (...)`` in prose are
#: not sites. Upper-case with a word boundary, so ``MIN(``, ``JOIN (`` and the rest do not match.
_RAW_IN = re.compile(r"\bIN\s*\(")


class _Membership(NamedTuple):
    """What one function does with membership filters."""

    sites: int
    """How many it carries. Counted rather than flagged so a SECOND filter added inside an
    already-classified function fails here too, instead of riding the first one's line."""
    chunked: bool
    """Whether its CODE reads ``KEY_CHUNK``. Not proof that the chunking is correct -- it is
    what stops a line below claiming "chunked" about a function that does no chunking. Read
    off the syntax tree rather than the text, because the text includes the comment saying
    why the read is chunked, and a check that comment satisfies proves nothing (rule 147)."""


class _MembershipWalk(ast.NodeVisitor):
    """Collects membership filters, remembering which ``def`` each one sits inside."""

    def __init__(self, rel: str, found: dict[str, _Membership]) -> None:
        self._rel = rel
        self._found = found
        self._stack: list[ast.AST] = []

    def _scope(self, node: ast.AST) -> None:
        self._stack.append(node)
        self.generic_visit(node)
        self._stack.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._scope(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._scope(node)

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self._scope(node)

    def visit_Call(self, node: ast.Call) -> None:
        func = node.func
        operator = isinstance(func, ast.Attribute) and func.attr in _MEMBERSHIP_CALLS
        expanding = (
            isinstance(func, ast.Name)
            and func.id == "bindparam"
            and any(kw.arg == "expanding" for kw in node.keywords)
        )
        if operator or expanding:
            self._record(node)
        self.generic_visit(node)

    def visit_Constant(self, node: ast.Constant) -> None:
        # Hand-written SQL, including the constant halves of an f-string. Recorded against the
        # innermost enclosing def, which is where the placeholder list is built.
        if isinstance(node.value, str) and _RAW_IN.search(node.value):
            self._record(node)
        self.generic_visit(node)

    def _record(self, node: ast.AST) -> None:
        names = [getattr(n, "name", "") for n in self._stack]
        key = f"{self._rel}::{'.'.join(names) or '<module>'}"
        enclosing = self._stack[-1] if self._stack else node
        prior = self._found.get(key)
        self._found[key] = _Membership(
            sites=(prior.sites if prior else 0) + 1,
            chunked=any(
                isinstance(inner, ast.Name) and inner.id == "KEY_CHUNK"
                for inner in ast.walk(enclosing)
            ),
        )


def _membership_sites(root: Path) -> dict[str, _Membership]:
    """Every ``IN``-shaped filter under ``root``, keyed by ``path::enclosing qualname``.

    Three spellings reach one SQL construct and all three are collected: the ORM operators
    above, ``bindparam(..., expanding=True)``, which is how the raw-SQL readers bind
    ``IN :keys``, and a hand-built placeholder list pasted into the SQL text. Keyed on the
    enclosing function rather than a line number, because line numbers move and a reviewer
    needs to know which read is being classified.
    """
    found: dict[str, _Membership] = {}
    for path in sorted(root.rglob("*.py")):
        rel = path.relative_to(REPO).as_posix()
        _MembershipWalk(rel, found).visit(ast.parse(path.read_text(encoding="utf-8")))
    return found


#: Every membership filter in ``src/reaper``, and what bounds the list it expands. A line
#: starting ``chunked`` means the read loops on ``KEY_CHUNK``; one starting ``bounded`` says
#: what holds its input under SQLite's ceiling without chunking. Reconciled by hand, and by
#: site count, so a filter added beside a classified one cannot ride its line (rule 145).
_MEMBERSHIP_INVENTORY: dict[str, tuple[int, str]] = {
    "src/reaper/api/review.py::list_candidates": (
        5,
        "bounded: four over the operator's hand overrides, one over the page (limit le=500)",
    ),
    "src/reaper/api/review.py::_group_rollups": (2, "chunked"),
    "src/reaper/api/review.py::_decided_keys": (2, "chunked"),
    "src/reaper/api/review.py::group_detail": (1, "bounded: the seasons of one show"),
    "src/reaper/services/condemned.py::_reap_overridden_rows": (
        2,
        "bounded: the operator's reap overrides, one row per hand click",
    ),
    "src/reaper/services/condemned.py::overridden_lane_shifts": (
        2,
        "bounded: the operator's overrides, one row per hand click",
    ),
    "src/reaper/services/executor.py::Executor.execute": (1, "chunked"),
    "src/reaper/services/executor.py::Executor._rolling_30d_deletions": (
        1,
        "bounded: the fixed _TERMINAL_DELETE_KINDS set",
    ),
    "src/reaper/services/fairness.py::_evidence_index": (1, "chunked"),
    "src/reaper/services/fairness.py::_distinct_episodes": (1, "chunked"),
    "src/reaper/services/grace.py::grace_report": (1, "chunked"),
    "src/reaper/services/imdb_dataset.py::ImdbRatings.lookup": (1, "chunked"),
    "src/reaper/services/instances.py::arr_rows": (1, "bounded: two enum members"),
    "src/reaper/services/retention.py::_doomed": (
        2,
        "bounded: both take a subquery, so nothing is bound at all",
    ),
    "src/reaper/services/retention.py::sweep_old_snapshots": (1, "bounded: SWEEP_BATCH ids"),
    "src/reaper/services/season_scan.py::season_watch_stats": (3, "chunked"),
    "src/reaper/services/snapshot.py::record_first_flagged_bulk": (1, "chunked"),
    "src/reaper/services/snapshot.py::_fold_merged_watch_stats": (1, "chunked"),
}


def test_every_scan_sized_in_clause_is_chunked_or_classified() -> None:
    """Rule 94, which was prose and nowhere in code until #556.

    An expanding ``IN`` binds one variable per key and SQLite refuses a statement past its
    ceiling, so a filter over a library-sized set is a scan or a report that dies outright
    rather than one that runs slowly. The grace report shipped that way: nothing bounds the
    condemned set, and the read raised ``OperationalError``, which is not an
    ``IntegrationError`` and so was caught nowhere.

    Reading the code is the only way to tell a scan-sized list from a two-element one, so this
    does not try to: it collects the three spellings the tree uses and requires each site to
    carry a written classification. A new one fails here, which is the point -- the site that
    broke was missed because nothing made anyone look at it. A **fourth** spelling would be
    missing from this walk and from its counts alike (rule 147), which is why the walk is
    driven against each form in the test below rather than trusted.
    """
    found = _membership_sites(SRC)

    missing = sorted(set(found) - set(_MEMBERSHIP_INVENTORY))
    assert not missing, (
        "membership filters with no classification:\n"
        + "\n".join(missing)
        + "\n\nChunk the read on `db.KEY_CHUNK` if its list is scan-sized, then add a line to "
        "_MEMBERSHIP_INVENTORY saying which it is (rule 94)."
    )
    gone = sorted(set(_MEMBERSHIP_INVENTORY) - set(found))
    assert not gone, (
        "_MEMBERSHIP_INVENTORY classifies filters that no longer exist, so the list is "
        "vouching for reads nobody can find:\n" + "\n".join(gone)
    )
    counts = {
        key: (found[key].sites, expected)
        for key, (expected, _why) in _MEMBERSHIP_INVENTORY.items()
        if found[key].sites != expected
    }
    assert not counts, (
        "these functions gained or lost a membership filter; re-read the classification "
        f"before moving the count (found, classified): {counts}"
    )
    unchunked = sorted(
        key
        for key, (_n, why) in _MEMBERSHIP_INVENTORY.items()
        if why.startswith("chunked") and not found[key].chunked
    )
    assert not unchunked, (
        "classified as chunked, but the function never names KEY_CHUNK:\n" + "\n".join(unchunked)
    )


def test_the_membership_walk_reads_the_forms_the_tree_spells(tmp_path: Path) -> None:
    """The guard above is only as wide as this walk (rule 147).

    Driven against every spelling ``src/`` uses -- the three ORM operators, a raw-SQL
    ``expanding`` bindparam, a hand-built placeholder list inside an f-string, several filters
    in one function, a method inside a class -- and against the three that must NOT count: a
    plain ``bindparam`` binds one value and cannot overflow anything, an unrelated method whose
    name merely ends in the same letters is not a membership filter, and neither is SQL naming
    a function that happens to end in those two letters.

    The chunked flag is driven both ways too, and the negative case is the one that matters:
    ``one`` carries ``KEY_CHUNK`` in a comment and nowhere in its code, which is exactly what
    a chunking loop deleted from under its own explanatory comment looks like.
    """
    scratch = tmp_path / "src" / "reaper"
    scratch.mkdir(parents=True)
    (scratch / "m.py").write_text(
        "def one():\n"
        "    # chunked on KEY_CHUNK, says the comment nothing here implements\n"
        "    return T.a.in_(keys), T.b.not_in(keys), T.c.notin_(keys)\n"
        "def two():\n"
        "    return text('x IN :k').bindparams(bindparam('k', expanding=True))\n"
        "def three():\n"
        "    q = 'SELECT MIN(x) FROM t JOIN (SELECT 1) u'\n"
        "    return text('x = :k').bindparams(bindparam('k')), obj.join_(keys), q\n"
        "class C:\n"
        "    def four(self):\n"
        "        for chunk in batched(keys, KEY_CHUNK):\n"
        "            yield T.a.in_(chunk)\n"
        "    def five(self):\n"
        "        marks = ', '.join(f':id{i}' for i in range(len(keys)))\n"
        "        return text(f'SELECT 1 FROM t WHERE k IN ({marks})')\n",
        encoding="utf-8",
    )
    global REPO
    real, REPO = REPO, tmp_path
    try:
        found = _membership_sites(scratch)
    finally:
        REPO = real

    assert set(found) == {
        "src/reaper/m.py::one",
        "src/reaper/m.py::two",
        "src/reaper/m.py::C.four",
        "src/reaper/m.py::C.five",
    }, found
    assert found["src/reaper/m.py::one"].sites == 3
    assert not found["src/reaper/m.py::one"].chunked
    assert found["src/reaper/m.py::C.four"].chunked
    # The placeholder list is attributed to the innermost def that builds it, not to its class.
    assert found["src/reaper/m.py::C.five"].sites == 1


#: Every argument a Sonarr or Radarr client must be handed, beyond the URL and the key.
#: Omitting ``api_path_prefix`` or ``verify`` is not a crash: the client falls back to its own
#: default, ``/api/v3`` for the path and ``True`` for TLS. Both are the *narrow* direction, so
#: the failure is a scan that reads nothing or a Test Connection that validates a path the
#: scan will never send to, never a wider deletion. ``safety`` is the one that crashes, being
#: keyword-only and required, so this gate can only ever report on the other two.
_ARR_CONSTRUCTION_ARGS = frozenset({"safety", "verify", "api_path_prefix"})

#: The two classes the argument set above belongs to.
_ARR_CLIENTS = ("RadarrClient", "SonarrClient")

#: Reconciled by hand against the tree, so a site that leaves the walk is noticed rather
#: than silently dropping out of the assertion below (rule 145). **Six, which is three
#: functions building two classes each**: ``scan_runner.build_sources``,
#: ``scan_runner.build_reap_gateway`` and ``instances._client``. The plan's finding says
#: "three places" and means the functions; this number counts calls, and the first draft of
#: this constant wrote the finding's figure down without re-deriving it.
_EXPECTED_ARR_CONSTRUCTIONS = 6


def _client_construction_sites(root: Path, names: tuple[str, ...]) -> dict[str, set[str]]:
    """Every ``<name>(...)`` call under ``src/`` for the given client classes, by address.

    Two gates read this, the *arr one below over six sites and the TLS one over 21. Reads the
    call node and inspects its keywords, rather than anchoring on the text after the paren:
    most sites wrap across several lines and some are a single line, so a line-oriented
    matcher reads one spelling and misses the rest (rule 147). A ``**kwargs`` splat would
    defeat this, and none exists; if one arrives it is recorded as passing nothing, and the
    caller's membership assertion names it.
    """
    found: dict[str, set[str]] = {}
    for path in sorted(root.rglob("*.py")):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name):
                continue
            if node.func.id not in names:
                continue
            rel = path.relative_to(REPO).as_posix()
            found[f"{rel}:{node.lineno} {node.func.id}"] = {
                kw.arg for kw in node.keywords if kw.arg is not None
            }
    return found


def test_every_arr_client_is_built_with_the_same_arguments() -> None:
    """The scan, the reap gateway and Test Connection each build these three ways.

    ``api_path_prefix`` once reached the first two and not the third, so a green connection
    test vouched for a path the scan does not use. The fix was adding the argument, and
    ``instances._client`` still carries the note. Nothing binds a fourth site written by
    someone who never read that note, which is what this is (rule 72, and CLAUDE.md's "write
    the gate instead" -- a shared constructor would only bind the sites that call it).
    """
    sites = _client_construction_sites(SRC, _ARR_CLIENTS)
    assert len(sites) == _EXPECTED_ARR_CONSTRUCTIONS, (
        f"expected {_EXPECTED_ARR_CONSTRUCTIONS} *arr client constructions under src/, walked "
        f"{len(sites)}: {sorted(sites)}. A new one is fine and must pass "
        f"{sorted(_ARR_CONSTRUCTION_ARGS)}; bump the number here AND in "
        "docs/SIMPLIFICATION_PLAN.md's wave 3 row, which states the population in prose."
    )
    missing = {
        site: sorted(_ARR_CONSTRUCTION_ARGS - args)
        for site, args in sites.items()
        if not args.issuperset(_ARR_CONSTRUCTION_ARGS)
    }
    assert not missing, (
        f"these *arr clients are built without every argument the others pass: {missing}. "
        "An omitted one falls back to the client's default rather than the operator's stored "
        "value, so the scan and the connection test stop agreeing about what they reached."
    )


def test_the_arr_construction_walk_reads_every_spelling_the_tree_uses(tmp_path: Path) -> None:
    """A guard that scans source is bounded by the syntax it parses (rule 147).

    The tree spells these two ways -- one call per line, and one wrapped across five -- and a
    matcher anchored on the text after the paren reads the first and misses the second. This
    drives both, plus the form that must NOT be collected under any name set: a same-named
    method call, ``self.RadarrClient(...)`` being an Attribute rather than a Name. The
    ``**kwargs`` splat is the one shape that defeats the argument check rather than the walk,
    so it is collected as passing nothing and the membership assertion is what names it.

    The ``TautulliClient`` call is collected by the TLS name set and not by the *arr one, off
    one scratch file read twice. A matcher serving two populations is proven in both.
    """
    scratch = tmp_path / "src" / "reaper"
    scratch.mkdir(parents=True)
    (scratch / "m.py").write_text(
        "def flat():\n"
        "    return RadarrClient(u, k, safety=s, verify=v, api_path_prefix=p)\n"
        "def wrapped():\n"
        "    return SonarrClient(\n"
        "        u,\n"
        "        k,\n"
        "        safety=s,\n"
        "        api_path_prefix=p,\n"
        "        verify=v,\n"
        "    )\n"
        "def splatted():\n"
        "    return RadarrClient(u, k, **kwargs)\n"
        "def not_a_construction(self):\n"
        "    return self.RadarrClient(u, k), TautulliClient(u, k, safety=s, verify=v)\n",
        encoding="utf-8",
    )
    global REPO
    real, REPO = REPO, tmp_path
    try:
        found = _client_construction_sites(scratch, _ARR_CLIENTS)
        wider = _client_construction_sites(scratch, _TLS_CLIENTS)
    finally:
        REPO = real

    assert set(found) == {
        "src/reaper/m.py:2 RadarrClient",
        "src/reaper/m.py:4 SonarrClient",
        "src/reaper/m.py:12 RadarrClient",
    }, found
    assert found["src/reaper/m.py:2 RadarrClient"].issuperset(_ARR_CONSTRUCTION_ARGS)
    assert found["src/reaper/m.py:4 SonarrClient"].issuperset(_ARR_CONSTRUCTION_ARGS)
    assert found["src/reaper/m.py:12 RadarrClient"] == set()

    # The same matcher under the wider name set. The Tautulli call the *arr walk must not
    # collect is one this one must, and the attribute form stays out of both.
    assert set(wider) == set(found) | {"src/reaper/m.py:14 TautulliClient"}, wider
    assert wider["src/reaper/m.py:14 TautulliClient"] == {"safety", "verify"}


#: What every client built from an address the operator typed must be handed. ``safety`` is
#: the transport guard's state, keyword-only and required (``BaseClient.__init__``,
#: ``PlexClient.__init__``), so omitting it raises ``TypeError`` and this gate never sees it.
#: ``verify`` is that instance's TLS switch and falls back to ``True``, so dropping it costs
#: agreement rather than safety. An operator whose server sits behind a self-signed
#: certificate gets one surface that cannot reach it while every other surface can, and
#: nothing announces the difference.
_TLS_CLIENT_ARGS = frozenset({"safety", "verify"})

#: Every class in ``clients/`` that is CONSTRUCTED against an address the operator stored.
#: This list is the walk's real bound and no count can cover it, since a class the matcher
#: never names contributes zero sites and the number below never moves (rule 145). So the
#: four classes that also declare ``verify`` are excluded in writing rather than by omission:
#:
#: * ``PlexTvClient`` reaches plex.tv, an address nobody configured, and declares no
#:   ``verify`` at all.
#: * ``GuardedSession`` is the transport plexapi rides, built inside ``PlexClient`` from the
#:   ``verify`` that client was already handed.
#: * ``BaseClient`` and ``ArrClient`` are base classes with no direct construction anywhere.
#:
#: ``_ProbeClient`` IS in: it probes one advertised address of the operator's own server, and
#: both callers thread that server's stored switch into it.
_TLS_CLIENTS = (
    "PlexClient",
    "RadarrClient",
    "SeerrClient",
    "SonarrClient",
    "TautulliClient",
    "_ProbeClient",
)

#: Reconciled by hand against the tree, so a site that leaves the walk is noticed rather than
#: silently dropping out of the assertion (rule 145). **Twenty-one**: six ``PlexClient``, five
#: ``TautulliClient``, three ``SeerrClient``, one ``_ProbeClient``, and the six ``*arr`` the
#: gate above counts separately. The six Plex ones are W3b-8's population, by AST at this tip.
_EXPECTED_TLS_CONSTRUCTIONS = 21


def test_every_client_carries_the_operators_own_tls_setting() -> None:
    """W3b-8's six ``PlexClient`` constructions, and the fifteen siblings beside them.

    The row proposed folding the six into one helper. Measured, that nets about six lines
    and binds only the callers that adopt it, which is the reasoning
    ``test_every_arr_client_is_built_with_the_same_arguments`` already wrote down one gate up.
    So the row is killed and its obligation lands here instead, widened to every client built
    against a stored address (rule 72). The *arr gate stays separate because it requires a
    third argument these do not have.

    **Two spellings are out of reach rather than covered** (rule 147): the walk matches an
    ``ast.Name``, so ``some_module.PlexClient(...)`` and ``from … import PlexClient as PC``
    are invisible to it. Neither is in the tree, checked by AST across ``src/`` at this tip,
    and the count above cannot see one arriving because a site the walk never collected is
    missing from both halves. Reading the whole call is what the shared walk already does;
    resolving a local alias per file is what ``_pending_pin_construction_sites`` does already.

    **The ceiling on the check itself**: it reads that ``verify`` was PASSED, not what was
    passed. ``verify=True`` written by hand would satisfy it and mean the opposite. All 21
    sites pass a stored value today (``server.verify_tls``, ``row.verify_tls``, ``r
    .verify_tls``, ``plex_verify``, ``verify``), and a literal there is a code review's job.
    """
    sites = _client_construction_sites(SRC, _TLS_CLIENTS)
    assert len(sites) == _EXPECTED_TLS_CONSTRUCTIONS, (
        f"expected {_EXPECTED_TLS_CONSTRUCTIONS} client constructions under src/ for "
        f"{sorted(_TLS_CLIENTS)}, walked {len(sites)}: {sorted(sites)}. A new one is fine and "
        f"must pass {sorted(_TLS_CLIENT_ARGS)}; bump the number here AND in "
        "docs/SIMPLIFICATION_PLAN.md's W3b-8 kill block, which states it in prose (rule 144). "
        "A new client CLASS built against a stored address belongs in _TLS_CLIENTS, or the "
        "walk cannot see any of its sites and this count cannot tell you."
    )
    missing = {
        site: sorted(_TLS_CLIENT_ARGS - args)
        for site, args in sites.items()
        if not args.issuperset(_TLS_CLIENT_ARGS)
    }
    assert not missing, (
        f"these clients are built without passing the operator's stored TLS setting: {missing}. "
        "`verify` then falls back to True, so a server the operator excused from certificate "
        "checks becomes unreachable from this one surface and reachable from every other."
    )


#: The one place a pending plex.tv PIN is written, ``plex_link.start_pin``.
#:
#: Two flows wrote their own before W3b-6 merged them, and the merge buys exactly one
#: thing: a third flow cannot arrive without the expiry sweep and without a ``purpose``.
#: Both matter. The sweep is the only thing bounding the table, and ``purpose`` is the
#: fence between an open sign-in route and an admin-only link route, so a row created
#: without one is a row either poller might spend. A docstring saying "go through
#: ``start_pin``" binds nobody who has not read it, which is what this counts instead
#: (rule 72, and CLAUDE.md's "write the gate instead").
#:
#: **Spellings the walk reads** (rule 147, written down before shipping the matcher, and
#: each one driven in ``test_the_pending_pin_walk_reads_every_spelling``): the bare name;
#: the ``models.PendingPlexLogin(...)`` attribute form; a local alias, because
#: ``from … import X as Y`` is live idiom here and not a hypothetical
#: (``services/list_rules.py`` imports ``Policy as PolicyModel``); and the Core
#: ``insert(PendingPlexLogin)``, which is a write with no construction in it at all.
#: **What it cannot read**: a name reached through ``getattr``. Nothing in ``src/``
#: addresses a model that way, and a walk that tried would be matching strings.
_PENDING_PIN_CONSTRUCTION_SITE = "src/reaper/services/plex_link.py"


def _names_the_pending_model(node: ast.expr, local_names: set[str]) -> bool:
    """Is this expression the model itself, bare, aliased, or attribute-qualified?"""
    if isinstance(node, ast.Name):
        return node.id in local_names
    if isinstance(node, ast.Attribute):
        return node.attr == "PendingPlexLogin"
    return False


def _pending_pin_construction_sites(root: Path) -> set[str]:
    """Every write of a ``PendingPlexLogin`` row under ``src/``, by address.

    Resolves the model's local names per file from that file's own ``ImportFrom`` nodes,
    rather than matching the class's own spelling: anchoring on the spelling would read
    the site that already complies and go blind to an aliased one, which is the form a
    second site is most likely to arrive in (rule 147). The constant above lists every
    spelling this accepts and the one it does not.
    """
    found: set[str] = set()
    for path in sorted(root.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        local_names = {"PendingPlexLogin"}
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                local_names.update(
                    alias.asname or alias.name
                    for alias in node.names
                    if alias.name == "PendingPlexLogin"
                )
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            verb = node.func
            # Constructing the model, or naming it as the target of a Core insert.
            # `select(PendingPlexLogin)` is the same AST shape as the second and is a
            # read, so the verb is what separates them.
            writes_a_row = _names_the_pending_model(verb, local_names) or (
                isinstance(verb, ast.Name | ast.Attribute)
                and (verb.id if isinstance(verb, ast.Name) else verb.attr) == "insert"
                and bool(node.args)
                and _names_the_pending_model(node.args[0], local_names)
            )
            if writes_a_row:
                found.add(f"{path.relative_to(REPO).as_posix()}:{node.lineno}")
    return found


def test_a_pending_plex_pin_is_written_in_exactly_one_place() -> None:
    sites = _pending_pin_construction_sites(SRC)
    assert len(sites) == 1 and next(iter(sites)).startswith(_PENDING_PIN_CONSTRUCTION_SITE), (
        f"expected one PendingPlexLogin write, in {_PENDING_PIN_CONSTRUCTION_SITE}, "
        f"walked {sorted(sites)}. A new plex.tv PIN flow calls plex_link.start_pin with its "
        "own purpose rather than inserting a row: start_pin is what sweeps expired pendings "
        "and what sets the TTL, and a hand-written row gets neither."
    )


def test_the_pending_pin_ttl_outlives_the_browsers_poll() -> None:
    """``PIN_TTL`` is a producer bound whose consumer is in the other language (rule 131).

    The row `start_pin` writes has to outlive the window the browser polls for, or it
    expires under an operator who is still on plex.tv and the sign-in fails for a reason
    nothing reports. The two numbers are declared 5 minutes apart in two languages and
    nothing but this reads both, which is why `PIN_TTL`'s comment used to cite
    ``PlexTvClient.PIN_TIMEOUT`` instead: same 5 minutes, but it governs ``wait_for_pin``,
    whose only caller is the CLI ``link``, and that path writes no pending row.
    """
    source = (REPO / "frontend" / "src" / "components" / "PlexPin.tsx").read_text()
    match = re.search(r"^const DEADLINE_MS = (.+);$", source, re.MULTILINE)
    assert match, (
        "DEADLINE_MS is gone from frontend/src/components/PlexPin.tsx, or is no longer a "
        "top-level const. It is the window services/plex_link.py's PIN_TTL is sized "
        "against; re-point both this matcher and that comment at whatever replaced it."
    )
    # `5 * 60 * 1000`, an arithmetic literal rather than a number, so it is evaluated
    # rather than parsed: `int()` reads the current spelling and nothing else.
    deadline = timedelta(milliseconds=eval(match.group(1), {"__builtins__": {}}))  # noqa: S307

    assert deadline <= plex_link.PIN_TTL, (
        f"services/plex_link.py's PIN_TTL is {plex_link.PIN_TTL}, shorter than the "
        f"{deadline} PlexPin.tsx polls for, so a pending PIN expires while the browser is "
        "still asking about it. Raise PIN_TTL, or lower DEADLINE_MS."
    )


def test_the_pending_pin_walk_reads_every_spelling(tmp_path: Path) -> None:
    """Rule 147: proven against the forms the tree does NOT use today, since those are
    the ones a second site arrives in, and against the reads it must not collect.

    The aliased form is the one that matters. A first draft of this walk matched the
    class's own spelling, and `from reaper.db.models import PendingPlexLogin as Pending`
    walked straight past it while the gate stayed green.
    """
    scratch = tmp_path / "src" / "reaper"
    scratch.mkdir(parents=True)
    (scratch / "m.py").write_text(
        "from reaper.db.models import AuthSession, PendingPlexLogin\n"
        "def bare():\n"
        "    session.add(PendingPlexLogin(pin_id=1, purpose='login'))\n"
        "def qualified():\n"
        "    session.add(\n"
        "        models.PendingPlexLogin(\n"
        "            pin_id=2,\n"
        "            purpose='link',\n"
        "        )\n"
        "    )\n"
        "def core_insert():\n"
        "    return insert(PendingPlexLogin).values(pin_id=3, purpose='login')\n"
        "def reads_are_not_writes():\n"
        "    return select(PendingPlexLogin), delete(PendingPlexLogin)\n"
        "def a_different_model_is_not_this_one():\n"
        "    return AuthSession(token_hash=h)\n",
        encoding="utf-8",
    )
    (scratch / "aliased.py").write_text(
        "from reaper.db.models import PendingPlexLogin as Pending\n"
        "def sneaks_one_in():\n"
        "    session.add(Pending(pin_id=4, purpose='login'))\n",
        encoding="utf-8",
    )
    # A local named `Pending` that is NOT this model: the alias set is per file, so the
    # name is only privileged in the file that imported the model under it.
    (scratch / "other.py").write_text(
        "from somewhere.other import Pending\ndef unrelated():\n    return Pending(whatever=1)\n",
        encoding="utf-8",
    )
    global REPO
    real, REPO = REPO, tmp_path
    try:
        found = _pending_pin_construction_sites(scratch)
    finally:
        REPO = real

    assert found == {
        "src/reaper/m.py:3",
        "src/reaper/m.py:6",
        "src/reaper/m.py:12",
        "src/reaper/aliased.py:3",
    }, found


#: The six configuration values ``snapshot.scan`` holds once per media type and hands to
#: ``_judge_item``. Movies are judged under the movie policy and seasons under the TV one:
#: separate keep rules, separate gates, separate condemn threshold, separate scoring window.
#: The scan holds all twelve as locals differing only by a ``movie_`` / ``tv_`` prefix. Four
#: are its own parameters (``movie_policy``, ``movie_gates``, and the TV pair); the other
#: eight it derives from those.
_LANE_ARGUMENTS = frozenset(
    {"gates", "signals", "custom_condemn", "keeps", "policy", "window_days"}
)

#: The prefix on each lane's locals, and the discriminator the walk below reads.
_LANES = frozenset({"movie", "tv"})

#: Reconciled by hand against the tree, so a call that leaves the walk is noticed rather than
#: dropping out of the assertions below (rule 145). Two: the movie loop and the season loop,
#: both in ``services/snapshot.py``.
_EXPECTED_JUDGE_ITEM_CALLS = 2


def _judge_item_lane_arguments(root: Path) -> dict[str, dict[str, str]]:
    """Every ``_judge_item(...)`` call under ``src/``, and the lane locals it passes.

    Reads the call node's keywords rather than the text after the paren: the two sites wrap
    across 67 and 52 lines, so a line-oriented matcher reads one keyword per attempt
    (rule 147). A bare ``_judge_item(...)`` and a qualified ``snapshot._judge_item(...)`` are
    both collected, since the count below is what notices a site leaving the walk and a
    qualified call would otherwise take a third site out of both halves.

    Only a bare ``Name`` whose first segment is a lane counts as a lane local, so an
    attribute, a call result or an unprefixed name is absent from the mapping and the
    keyword-set assertion below is what names it. A ``**kwargs`` splat passes nothing.
    """
    found: dict[str, dict[str, str]] = {}
    for path in sorted(root.rglob("*.py")):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            called = node.func
            name = called.id if isinstance(called, ast.Name) else getattr(called, "attr", "")
            if name != "_judge_item":
                continue
            rel = path.relative_to(REPO).as_posix()
            found[f"{rel}:{node.lineno}:{node.col_offset}"] = {
                kw.arg: kw.value.id
                for kw in node.keywords
                if kw.arg is not None
                and isinstance(kw.value, ast.Name)
                and kw.value.id.split("_")[0] in _LANES
            }
    return found


def test_a_judged_item_is_never_handed_the_other_lanes_policy() -> None:
    """The movie loop cannot reach a TV local, and the season loop cannot reach a movie one.

    Measured, on this tree: cross ``custom_condemn``, ``keeps`` and ``policy`` at the movie call
    site and this is the only test in the whole suite that fails. So the keep rules a movie is
    judged against, and the threshold it is condemned at, can both come from the TV policy with
    nothing else saying a word (rule 118). ``gates``, ``signals`` and ``window_days`` are the
    three ``test_scan_pipeline.py`` already catches.

    This is the gate rather than a lane carrier, per CLAUDE.md's "write the gate instead": a
    carrier binds the sites that adopt it, and ``services/snapshot.py`` is the deletion path.
    ``docs/SIMPLIFICATION_PLAN.md``'s wave 3 parameter-object paragraph carries the measurement.
    """
    sites = _judge_item_lane_arguments(SRC)
    assert len(sites) == _EXPECTED_JUDGE_ITEM_CALLS, (
        f"expected {_EXPECTED_JUDGE_ITEM_CALLS} `_judge_item` calls under src/, walked "
        f"{len(sites)}: {sorted(sites)}. A new one is fine and must pass every argument in "
        f"{sorted(_LANE_ARGUMENTS)} off one lane's locals; bump the number here AND in "
        "docs/SIMPLIFICATION_PLAN.md's wave 3 parameter-object paragraph, which states the "
        "population in prose."
    )
    lanes: dict[str, str] = {}
    for site, args in sorted(sites.items()):
        assert set(args).issuperset(_LANE_ARGUMENTS), (
            f"{site} does not pass {sorted(_LANE_ARGUMENTS - set(args))} as a bare `movie_*` / "
            "`tv_*` local. Each lane argument reaches `_judge_item` that way so that crossing "
            "one is visible here; computing it inline at the call site hides which lane it came "
            "from. A seventh per-lane value is fine and is checked by the same walk."
        )
        prefixes = {local.split("_")[0] for local in args.values()}
        assert len(prefixes) == 1, (
            f"{site} judges an item against both lanes at once: {sorted(args.items())}. Movies "
            "are judged under the movie policy and seasons under the TV one, so a crossed "
            "argument applies the wrong keep rules, gates or condemn threshold to every item in "
            "that loop."
        )
        lanes[site] = prefixes.pop()
    assert set(lanes.values()) == _LANES, (
        f"the `_judge_item` calls do not cover both lanes: {lanes}. One loop judges movies and "
        "the other seasons, so pointing them at one lane silences the check above instead of "
        "fixing the cross it caught."
    )


def test_the_lane_walk_reads_every_spelling_the_tree_uses(tmp_path: Path) -> None:
    """A guard that scans source is bounded by the syntax it parses (rule 147).

    Drives the wrapped form the tree actually spells, the flat one it does not, and the two
    qualified spellings a third site could arrive in. Plus the four shapes that must not be
    collected as a lane local: an attribute, a call result, an unprefixed name, and a
    ``**kwargs`` splat. Each of those leaves the keyword out of the mapping, which is what makes
    the keyword-set assertion the thing that names it rather than the walk quietly losing it.
    """
    scratch = tmp_path / "src" / "reaper"
    scratch.mkdir(parents=True)
    (scratch / "m.py").write_text(
        "def movie_lane():\n"
        "    return _judge_item(\n"
        "        session,\n"
        "        gates=movie_gates,\n"
        "        signals=movie_signals,\n"
        "        custom_condemn=movie_custom,\n"
        "        keeps=movie_keeps,\n"
        "        policy=movie_policy,\n"
        "        window_days=movie_window,\n"
        "    )\n"
        "def season_lane():\n"
        "    return _judge_item(session, gates=tv_gates, signals=tv_signals, "
        "custom_condemn=tv_custom, keeps=tv_keeps, policy=tv_policy, window_days=tv_window)\n"
        "def crossed():\n"
        "    return _judge_item(session, gates=movie_gates, keeps=tv_keeps)\n"
        "def indirect():\n"
        "    return _judge_item(\n"
        "        session,\n"
        "        gates=self.movie_gates,\n"
        "        signals=_signals(movie_policy),\n"
        "        keeps=keeps,\n"
        "        **rest,\n"
        "    )\n"
        "def qualified(self):\n"
        "    snapshot._judge_item(session, keeps=movie_keeps)\n"
        "    return self._judge_item(session, keeps=tv_keeps)\n",
        encoding="utf-8",
    )
    global REPO
    real, REPO = REPO, tmp_path
    try:
        found = _judge_item_lane_arguments(scratch)
    finally:
        REPO = real

    assert set(found) == {
        "src/reaper/m.py:2:11",
        "src/reaper/m.py:12:11",
        "src/reaper/m.py:14:11",
        "src/reaper/m.py:16:11",
        "src/reaper/m.py:24:4",
        "src/reaper/m.py:25:11",
    }, found
    assert set(found["src/reaper/m.py:2:11"]) == _LANE_ARGUMENTS
    assert set(found["src/reaper/m.py:12:11"]) == _LANE_ARGUMENTS
    assert {v.split("_")[0] for v in found["src/reaper/m.py:2:11"].values()} == {"movie"}
    assert {v.split("_")[0] for v in found["src/reaper/m.py:12:11"].values()} == {"tv"}
    # The cross the gate exists for: one call, both prefixes.
    assert {v.split("_")[0] for v in found["src/reaper/m.py:14:11"].values()} == {"movie", "tv"}
    # None of the four indirect spellings is read as a lane local.
    assert found["src/reaper/m.py:16:11"] == {}
    # A qualified call is a call site, whichever way it is qualified.
    assert found["src/reaper/m.py:24:4"] == {"keeps": "movie_keeps"}
    assert found["src/reaper/m.py:25:11"] == {"keeps": "tv_keeps"}


# ---------------------------------------------------------------------------
# The client failure sentences are worded in one place (rule 144)
# ---------------------------------------------------------------------------

#: The sentences a failed client call is reported with, as templates: literal text with `{}`
#: standing in for each interpolation, mapped to the factory in `clients/base.py` that words
#: each. Every one was written at two or three of the sites that raise it. Rewording one copy
#: leaves the others describing the same failure differently, which is what rule 144 is about,
#: and it had already happened: `public.py` spelled one of them with a hardcoded `GET` where
#: `base.py` names the method.
_CLIENT_FAILURE_SENTENCES = {
    "timed out ({})": "transport_failure",
    "unreachable ({})": "transport_failure",
    "refused redirect (HTTP {}) for {} {}": "refused_redirect",
    "HTTP {} for {} {}": "http_failure",
    "expected JSON from {}, got {}": "unexpected_body",
}

#: Where all four are allowed to be spelled.
_FAILURE_SENTENCE_HOME = "src/reaper/clients/base.py"


def _message_template(node: ast.expr) -> str | None:
    """An ``IntegrationError`` message argument as a template, or None if it cannot be read.

    A plain string is itself; an f-string becomes its literal parts with ``{}`` for every
    interpolation, so a copy spelling the method ``GET`` where the original interpolates it
    reads as a DIFFERENT template rather than the same one. Anything else -- a name, a ``%``
    format, a call -- reads as None and is counted rather than passed over, because a
    sentence the walk cannot see is a copy it cannot fence (rule 147).
    """
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if not isinstance(node, ast.JoinedStr):
        return None
    parts: list[str] = []
    for piece in node.values:
        if isinstance(piece, ast.FormattedValue):
            parts.append("{}")
        elif isinstance(piece, ast.Constant) and isinstance(piece.value, str):
            parts.append(piece.value)
        else:  # pragma: no cover -- a JoinedStr holds only those two node kinds
            return None
    return "".join(parts)


def _integration_error_messages(root: Path) -> tuple[dict[str, list[str]], list[str]]:
    """Every ``IntegrationError`` construction under ``root``, by message template.

    Returns the readable ones keyed by template, and the sites whose message the walk could
    not read at all. Both halves are asserted on: the second is the population another copy
    would hide in.

    ``message`` is positional-or-keyword, so both spellings are collected. A call with no
    message at all is not a construction this fence is about and is skipped; one whose message
    is there but unreadable is counted, which is the distinction the earlier draft of this walk
    got wrong by testing ``len(node.args) < 2`` alone and dropping every keyword call into
    neither half (rule 147).
    """
    by_template: dict[str, list[str]] = {}
    unreadable: list[str] = []
    for path in sorted(root.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
            if name != "IntegrationError":
                continue
            keyword = next((k.value for k in node.keywords if k.arg == "message"), None)
            message = node.args[1] if len(node.args) >= 2 else keyword
            if message is None:
                continue
            site = f"{path.relative_to(REPO).as_posix()}:{node.lineno}"
            template = _message_template(message)
            if template is None:
                unreadable.append(site)
            else:
                by_template.setdefault(template, []).append(site)
    return by_template, unreadable


def test_every_client_failure_sentence_is_worded_in_exactly_one_place() -> None:
    """Rule 144, for the sentences a failed integration call is reported with.

    ``_send``, ``_mutate``, ``PublicClient``, ``get_json`` and ``plextv._post`` each carried
    their own copy, thirteen in all. The factories in ``clients/base.py`` are the only place
    these five are spelled now, and this is what stops the next client writing its own.

    **It fences these and not every sentence this layer raises.** ``too many redirects`` stays
    out: it is written once per file, and ``public.py``'s copy already spells the method
    ``GET`` where ``base.py`` interpolates it, so adding it here fails rather than fences. The
    comment above the factories in ``clients/base.py`` says the same. Every other
    ``IntegrationError`` sentence under ``src/`` is written once, which the walk itself shows.
    """
    by_template, _ = _integration_error_messages(SRC)
    strays = {
        template: outside
        for template in _CLIENT_FAILURE_SENTENCES
        for outside in [
            [s for s in by_template.get(template, []) if not s.startswith(_FAILURE_SENTENCE_HOME)]
        ]
        if outside
    }
    assert not strays, (
        f"a client failure sentence is written outside {_FAILURE_SENTENCE_HOME}: {strays}. "
        "Call the factory that already words each one -- "
        f"{', '.join(sorted(set(_CLIENT_FAILURE_SENTENCES.values())))} -- rather than "
        "repeating the sentence, or the copies drift the next time one is reworded (rule 144)."
    )
    for template, factory in _CLIENT_FAILURE_SENTENCES.items():
        assert factory in (SRC / "clients" / "base.py").read_text(encoding="utf-8"), (
            f"_CLIENT_FAILURE_SENTENCES maps {template!r} to {factory}, which no longer "
            "exists in clients/base.py. Point it at whatever words the sentence now, or the "
            "message above sends the next author to a function that is gone."
        )
    missing = [t for t in _CLIENT_FAILURE_SENTENCES if not by_template.get(t)]
    assert not missing, (
        f"these sentences are no longer written anywhere under src/reaper: {missing}. If a "
        "factory was renamed or its wording changed, update _CLIENT_FAILURE_SENTENCES to the "
        "new spelling; leaving it stale makes this gate pass while fencing nothing."
    )


def test_the_failure_sentence_walk_reads_every_message_the_tree_writes() -> None:
    """Rule 147: the fence is only as wide as the spellings the walk can parse.

    A message built anywhere but at the ``IntegrationError(...)`` call -- assembled into a
    local, or handed in by a caller -- is invisible to the template match above, so a copy
    hiding there would pass. The tree writes none today, and a new one has to be looked at
    rather than silently joining the blind spot.
    """
    _, unreadable = _integration_error_messages(SRC)
    assert unreadable == [], (
        f"these IntegrationError messages are not literals the walk can read: {unreadable}. "
        "Spell the message at the raise, or widen _message_template to the form used and "
        "prove it against the forms it still rejects."
    )


def test_the_failure_sentence_matcher_reads_the_forms_it_claims_to(tmp_path: Path) -> None:
    """The template reader, against each form separately: literal, f-string, and neither.

    The keyword forms are here because they were the walk's blind spot and read as no blind
    spot: ``message`` is positional-or-keyword, so an argument count alone dropped them out of
    the readable half AND out of the unreadable half at once, which is a fence reporting
    itself complete over sentences it never saw (rule 147).
    """
    scratch = tmp_path / "src" / "reaper"
    scratch.mkdir(parents=True)
    (scratch / "m.py").write_text(
        "raise IntegrationError(svc, 'plain')\n"
        'raise IntegrationError(svc, f"HTTP {code} for {method} {path}")\n'
        'raise IntegrationError(svc, f"HTTP {code} for GET {path}")\n'
        "raise base.IntegrationError(svc, f'unreachable ({exc})')\n"
        "raise IntegrationError(svc, message)\n"
        "raise IntegrationError(svc)\n"
        'raise IntegrationError(svc, message=f"timed out ({kind})")\n'
        "raise IntegrationError(service=svc, message=held)\n",
        encoding="utf-8",
    )
    global REPO
    real, REPO = REPO, tmp_path
    try:
        by_template, unreadable = _integration_error_messages(scratch)
    finally:
        REPO = real

    # The two spellings of the HTTP sentence stay apart, which is the point: `GET` hardcoded
    # and `{method}` interpolated are not the same sentence, and the fence has to see that.
    assert sorted(by_template) == [
        "HTTP {} for GET {}",
        "HTTP {} for {} {}",
        "plain",
        "timed out ({})",
        "unreachable ({})",
    ]
    # A qualified call is a call site, and a keyword message is read like a positional one.
    assert by_template["unreachable ({})"] == ["src/reaper/m.py:4"]
    assert by_template["timed out ({})"] == ["src/reaper/m.py:7"]
    # A message that is there but unreadable is counted; a call carrying none is not a
    # construction this fence is about and is skipped rather than reported as a blind spot.
    assert unreadable == ["src/reaper/m.py:5", "src/reaper/m.py:8"]


# --- every Display field the source record carries reaches its lane's pack ----------------

#: The record each hand-written ``Display(...)`` pack unpacks, which is also its lane: the
#: movie loop reads a ``RawItem`` and the season loop a ``SeasonJudgment``. Taken off the
#: call's own value expressions rather than off the enclosing function, so a third pack in
#: another function is read by the same walk.
_DISPLAY_PACK_SOURCES: dict[str, type] = {"item": RawItem, "judgment": SeasonJudgment}

#: The fields one pack leaves at the ``None`` default, and why. Hand-written, and the first
#: draft of this gate derived it instead: a pack was allowed to skip any field its source
#: record did not declare BY NAME, which read the movie pack's missing ``ratings_json`` as
#: permitted, because the movie lane builds that value out of ``item.plex_ratings`` and the
#: dataset rather than copying a same-named field. Deleting it was green while deleting the
#: season's went red, and it blanks the ratings row for the whole movie lane
#: (``api/review.py``'s ``_ratings_out``). A derivation that reads a NAME cannot see a value
#: assembled from other fields, so the classification is written out (rule 103), and adding a
#: member here is an author saying in the diff why one lane cannot answer.
_DISPLAY_LANE_EXCEPTIONS: dict[str, str] = {
    "group_key": "a movie is not part of a show, so it joins no group",
    "group_title": "same, and the queue draws a movie under its own title",
    "title_slug": "Sonarr's series slug; Radarr's deep link keys on the tmdb id",
    "video_resolution": "a season spans episodes, so it has no single resolution",
}

#: Reconciled by hand against the tree (rule 145). Three ``Display(...)`` calls under
#: ``src/``: the two packs, plus the ``_NO_DISPLAY`` singleton, which sets nothing and is the
#: "no display fields" default.
_EXPECTED_DISPLAY_CALLS = 3


def _display_pack_sites(root: Path) -> dict[str, tuple[frozenset[str], frozenset[str]]]:
    """Every ``Display(...)`` call under ``src/``: the fields it sets, and the source records
    its values read off.

    Reads the call node rather than the text after the paren, because the two packs wrap
    across 24 and 16 lines (rule 147). The source base comes from every ``Name`` anywhere
    inside a keyword's value, not just a bare ``item.year``: the movie pack reaches ``item``
    through ``item.imdb_id or item.plex_imdb_id`` and through the arguments of
    ``build_ratings_json(...)``, and a base-of-the-attribute matcher would read neither.
    A keyword whose value names no source at all (``tvdb_id=None``) contributes no base and
    is still counted as set: an explicit ``None`` is an author saying the field does not
    apply.

    **Two spellings this cannot see**, written down rather than guessed at (rule 147): a pack
    built by ``dataclasses.replace(_NO_DISPLAY, …)``, and one calling ``Display`` through an
    aliased import. Neither is in the tree, and the count below cannot cover either, because a
    site that never entered the walk was never in the number. A positional argument and a
    ``**splat`` ARE covered, both by leaving the field out of ``keywords``.
    """
    found: dict[str, tuple[frozenset[str], frozenset[str]]] = {}
    for path in sorted(root.rglob("*.py")):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            called = node.func
            name = called.id if isinstance(called, ast.Name) else getattr(called, "attr", "")
            if name != "Display":
                continue
            keywords = frozenset(kw.arg for kw in node.keywords if kw.arg is not None)
            bases = frozenset(
                inner.id
                for kw in node.keywords
                for inner in ast.walk(kw.value)
                if isinstance(inner, ast.Name) and inner.id in _DISPLAY_PACK_SOURCES
            )
            rel = path.relative_to(REPO).as_posix()
            found[f"{rel}:{node.lineno}:{node.col_offset}"] = (keywords, bases)
    return found


def test_every_display_field_the_source_carries_reaches_its_lanes_pack() -> None:
    """A field added to ``Display`` and packed on one lane only is silent today.

    All fifteen default to ``None``, so an omission raises nothing and mypy sees nothing: the
    movie pack and the season pack are two hand-written mirrors of one dataclass (rule 103).
    Four of the fifteen do more than draw a queue row -- ``tmdb_id``, ``imdb_id`` and
    ``tvdb_id`` are what ``services/fairness.py`` joins a request to its candidate on, and
    ``title_slug`` builds the Sonarr link -- so a sixteenth of that kind, forgotten on one
    lane, drops that join for that lane rather than blanking a column (rules 29/106).

    Every field is set at both packs unless ``_DISPLAY_LANE_EXCEPTIONS`` names it, which is
    four today. That list is hand-written on purpose: see the constant for the derivation
    that replaced it and the movie-lane omission it read as permitted.

    ``docs/SIMPLIFICATION_PLAN.md``'s W5-2 row carries the measurement, including why the
    collapse this replaces was killed: ``_judge_item`` already takes ``Display`` whole, so
    merging the packs removes no parameter and no line. What it would have removed is this
    hazard, and the gate removes it from ``tests/``.
    """
    declared = frozenset(f.name for f in dataclasses.fields(Display))
    stale = sorted(set(_DISPLAY_LANE_EXCEPTIONS) - declared)
    assert not stale, (
        f"_DISPLAY_LANE_EXCEPTIONS names {stale}, which `Display` no longer declares. An "
        "exception outliving its field excuses nothing and reads as a live classification."
    )

    sites = _display_pack_sites(SRC)
    assert len(sites) == _EXPECTED_DISPLAY_CALLS, (
        f"expected {_EXPECTED_DISPLAY_CALLS} `Display(...)` calls under src/, walked "
        f"{len(sites)}: {sorted(sites)}. A new pack is fine and must set every field not in "
        "_DISPLAY_LANE_EXCEPTIONS; bump the number here."
    )

    empty = sorted(site for site, (keywords, _) in sites.items() if not keywords)
    assert len(empty) == 1, (
        f"expected exactly one `Display()` setting nothing, the `_NO_DISPLAY` singleton, "
        f"found {empty}. A pack that lost all its keywords would otherwise read as that "
        "singleton and leave this gate green."
    )

    packs = {site: value for site, value in sites.items() if value[0]}
    for site, (keywords, bases) in sorted(packs.items()):
        assert len(bases) == 1, (
            f"{site} packs a Display off {sorted(bases) or 'no known record'}; expected "
            f"exactly one of {sorted(_DISPLAY_PACK_SOURCES)}. Which record a pack unpacks is "
            "its lane, and a pack reading two of them, or none, has no lane to check."
        )
        forgotten = declared - keywords - set(_DISPLAY_LANE_EXCEPTIONS)
        assert not forgotten, (
            f"{site} leaves {sorted(forgotten)} at the None default. Every field defaults to "
            "None, so the other lane's pack setting it is the only sign anything is missing. "
            "Set it, or add it to _DISPLAY_LANE_EXCEPTIONS with the reason its lane cannot."
        )

    unused = sorted(
        field
        for field in _DISPLAY_LANE_EXCEPTIONS
        if all(field in keywords for keywords, _ in packs.values())
    )
    assert not unused, (
        f"_DISPLAY_LANE_EXCEPTIONS excuses {unused}, which both packs now set. An excuse no "
        "pack uses is the next lane-specific field's free pass."
    )

    assert {next(iter(bases)) for _, bases in packs.values()} == set(_DISPLAY_PACK_SOURCES), (
        f"the Display packs do not cover both lanes: {sorted(packs)}. One packs a movie and "
        "the other a season, so pointing them at one record silences the check above."
    )
