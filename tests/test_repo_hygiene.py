"""Repo-wide invariants that were previously only prose in the instruction files.

CLAUDE.md and ``.claude/rules/*.md`` are context, not enforcement: an agent that never
loads them, or a human who never reads them, breaks the rule silently. Every rule in here
is one that can be checked mechanically, so it costs nothing to enforce and catches humans
and agents alike. A rule that needs judgment stays prose; only the greppable ones live here.

These are filesystem checks -- no app boot, no fixtures, no network.
"""

from __future__ import annotations

import hashlib
import json
import re
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest
import yaml

# The one guard here that reads the app rather than the tree: the documented example of a
# checked protection is derived by running the gate that builds it, never transcribed
# (rule 119). The hygiene CI lane installs the whole package, so this costs nothing extra.
from reaper.engine.gates import (
    Facts,
    GateConfig,
    GateId,
    MinDormancyGate,
    ServerPopularityGate,
)
from reaper.engine.observation import Absent, Known
from reaper.services.scheduler import SCHEDULABLE_JOB_IDS

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
DECISION_SECTIONS = 17


def _live_docs() -> list[Path]:
    """Every doc that claims to describe the present. Excludes the frozen archive."""
    return sorted(p for p in DOCS.rglob("*.md") if HISTORY not in p.parents)


INSTRUCTION_FILES = [REPO / "CLAUDE.md", *sorted((REPO / ".claude" / "rules").glob("*.md"))]


# Tab, newline and carriage return are the only control characters source here is allowed to hold.
# Everything else in the C0 range, plus DEL, is invisible in an editor and in a diff.
_ALLOWED_CONTROL_BYTES = frozenset(b"\t\n\r")


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

    validator = inspect.getsource(ProfileSettings._run_cap_within_rolling_cap)
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
        ProfileSettings(**caps)  # type: ignore[arg-type]


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


def _repo_text_files() -> list[tuple[Path, str]]:
    """Every readable text file in THIS checkout, with its contents.

    Scoped to this checkout on purpose. ``.claude/worktrees/`` is gitignored (``.gitignore``)
    and holds agent worktrees, which are entire repo copies sitting inside the repo root -- and
    ``rglob`` honors no ignore file, so walking into them judges other branches' files as if
    they were ours. A worktree's ``.git`` is a *file*, so the skip entry below does not stop the
    descent either. Left in, ``uv run pytest`` in the main checkout fails the moment any
    worktree cut before this fix is still on disk, naming files the branch under test cannot
    reach. A gate nobody can turn green from their own branch is a gate that gets deleted.

    The skip is matched on the REPO-RELATIVE path, which is the part that is easy to get
    backwards: ``skip`` is tested against ``path.parts``, so putting ``"worktrees"`` in it
    would match the ABSOLUTE path and skip every file in the tree whenever the suite runs
    from inside a worktree -- which is where these sessions run it. The relative form also
    stops an ancestor directory outside the repo that happens to be named ``dist`` from
    silently emptying the walk.
    """
    found: list[tuple[Path, str]] = []
    # ``.dev-logs`` is gitignored runtime output, like the rest of these: whatever the dev
    # servers happened to print last is not this branch's source. It earns a place here because
    # the walk reads it -- a stack trace echoing a uvicorn command line would be collected as a
    # LAUNCH SITE and fail ``_EXPECTED_LAUNCHES`` in whichever checkout last ran the script,
    # naming a file no branch can fix. One log file per port makes that likelier, not rarer.
    skip = {
        ".git",
        "node_modules",
        ".venv",
        "dist",
        "__pycache__",
        ".ruff_cache",
        ".mypy_cache",
        ".dev-logs",
    }
    for path in REPO.rglob("*"):
        if not path.is_file() or path.resolve() == SELF:
            continue
        relative = path.relative_to(REPO)
        if any(p in skip for p in relative.parts) or relative.parts[:2] == (".claude", "worktrees"):
            continue
        try:
            found.append((path, path.read_text(encoding="utf-8")))
        except (UnicodeDecodeError, OSError):
            continue
    return found


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
    """A ``<Branch>`` repeating the repository's own tag is discarded in silence.

    Community Applications expands one install row per ``<Branch>``, but skips any branch whose
    ``<Tag>`` equals the tag on ``<Repository>``, because the Default row already installs that
    one (``include/exec.php``). The branch's ``<TagDescription>`` then never reaches a screen,
    and nothing reports it: the template still parses, still installs, and still shows a picker.
    539 of the 585 branch-using templates in the live app feed carry one of these dead entries.

    So the offered channels are the repository's tag plus one branch each for the others, the
    default channel is described by ``<DefaultTagDescription>`` rather than by a branch, and the
    tags are checked against the registry retention job rather than a literal list here:
    everything outside its protected set is swept after a week, so offering a tag from outside
    it hands the operator a channel whose image stops resolving (rule 25).
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
    assert default_tag not in tags, (
        f"a <Branch> repeats {default_tag!r}, the tag already on <Repository> ({repository}). "
        "Community Applications drops that branch and installs it from the Default row instead, "
        f"so its <TagDescription> is never shown. Describe {default_tag!r} with "
        "<DefaultTagDescription> and give a <Branch> only to the other channels."
    )
    assert (root.findtext("DefaultTagDescription") or "").strip(), (
        f"{templates[0].name} needs a <DefaultTagDescription>: it is the only copy the picker's "
        f"Default row can show for {default_tag!r}, and that row is the one most operators take."
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


def _dependabot_updates() -> list[dict]:
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
    dormancy = MinDormancyGate(GateConfig(GateId.MIN_DORMANCY, threshold=1_095))
    popularity = ServerPopularityGate(
        GateConfig(GateId.SERVER_POPULARITY, threshold=3, window_days=365)
    )
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
# Re-derive it by running the test, never by arithmetic on this comment.
_EXPECTED_NOTICES = 136


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
# Re-derive it by running the test, never by arithmetic on this comment.
_EXPECTED_STANDING = 35

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
    "frontend/src/App.tsx": 8,
    "frontend/src/components/DeletionToggle.tsx": 1,
    "frontend/src/components/Fairness.tsx": 1,
    "frontend/src/components/LogsPanel.tsx": 1,
    # 5 -> 6 when the library list moved to ``usePlexLibraries``. No branch here changed: the
    # panel's JSX is untouched, and the extra handle is the walk seeing the bag's two members
    # where a directly-bound ``useQuery`` had been one. Counted rather than excused, because
    # the population is the thing this pins.
    "frontend/src/components/PlexPanel.tsx": 6,
    "frontend/src/components/PolicyEditor.tsx": 4,
    "frontend/src/components/PolicyRuleEditors.tsx": 2,
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
    # 6 -> 7: the library pickers now consult the SYNC's failure as well as the query's. A read
    # that lands empty while the sync that would fill it fails is the ordinary answer when Plex
    # is not linked at all, and with only the query consulted the panel stated as fact that the
    # server holds no libraries of this kind -- about a server nobody reached.
    "frontend/src/components/ServiceModal.tsx": 7,
    "frontend/src/components/Settings.tsx": 8,
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
}

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
                if hook in _QUERY_PRIMITIVES:
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
    unknown = sorted(hooks - _READ_HOOKS - _ACTION_HOOKS)
    assert not unknown, (
        "these hooks hand back an `error`/`isError` the walk has never seen, so it cannot say\n"
        "whether they are reads or actions and will not guess:\n  " + "\n  ".join(unknown) + "\n"
        "Add each to _READ_HOOKS (a read, whose failure leaves the last good value in hand) or\n"
        "to _ACTION_HOOKS (a mutation, whose failure is an action's)."
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
#   Fairness.tsx (1)         NOT advice: the Refresh button's ``title``, "Reload requests and
#                            watch history". It is in the walk because the walk is of a word, and
#                            dropping it by hand is how a matcher starts lying about its own scope
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
#   Settings.tsx (6)         six never-loaded branches, each above a form that never rendered
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
    "frontend/src/components/Fairness.tsx": 1,
    "frontend/src/components/NotInScanPanel.tsx": 1,
    "frontend/src/components/PlexPanel.tsx": 1,
    "frontend/src/components/PolicyEditor.tsx": 1,
    "frontend/src/components/ReapBreakdown.tsx": 1,
    "frontend/src/components/ReapConfirm.tsx": 1,
    "frontend/src/components/ReapPlan.tsx": 1,
    "frontend/src/components/RestoreCard.tsx": 3,
    "frontend/src/components/Settings.tsx": 6,
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

    Matches the bare word ``reload``, case-insensitively, in what is left of a shipped ``.tsx``
    once comments are gone. Deliberately looser than the sentence it is about (rule 147): the tree
    spells the advice three ways -- "Reload to try again.", "Reload the page to try again." and
    "then reload this page." -- and the second of those WRAPS across two source lines in
    ``NotInScanPanel``, so a per-line match on the full phrase would have missed it. A word cannot
    wrap. The cost is that the walk also collects a Refresh button's tooltip, which is listed
    above rather than filtered out, because a matcher that quietly drops what does not fit stops
    being a count of anything.

    What this proves is that no site GAINED the advice and none of the fixed ones got it back. It
    says nothing about which branch inside a file renders it, so a file swapping one keep for a new
    one reads green here: the per-branch claims are pinned in the component tests.
    """
    found: dict[str, int] = {}
    for path in _shipped_tsx():
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


# Every ``<select>`` the app ships, counted by the scan below rather than believed. The two the
# count once carried past were #147's library pickers, which shipped nameless; they have names
# now, and the number is here so a twentieth that does not cannot hide behind them (rule 145).
_EXPECTED_SELECTS = 21


def _without_line_comments(chunk: str) -> str:
    """``chunk`` with every ``//`` run to end-of-line removed."""
    return "\n".join(line.split("//", 1)[0] for line in chunk.splitlines())


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
        # Prose about a name is not a name. Both spellings the old matcher fell for.
        ("<select value={tz}>", ""),
    ]
    for tag, text in accepted:
        assert _select_is_named(tag, text), f"should count as named: {tag}"
    for tag, text in rejected:
        assert not _select_is_named(tag, text), f"should NOT count as named: {tag}"

    # And the comment stripping, which happens before the matcher ever sees the tag: a comment
    # mentioning either spelling must not survive into the string that gets searched.
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
}

# The population the walk itself collects: every `*.test.tsx` under frontend/src that mounts
# something. Pinned separately from the audited count because they are DIFFERENT sets, and a file
# that drops out of the walk is otherwise missing from both halves while the two numbers agree
# (rule 145). Re-derive by running the test, never by arithmetic on the maps above.
_EXPECTED_RENDERING_TEST_FILES = 50


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
        if not re.search(r"\brender\(", body):
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
    from reaper.engine.policy import DEFAULT_MOVIE_POLICY, DEFAULT_TV_POLICY, SignalId

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


_SETTINGS_TSX = FRONTEND_SRC / "components" / "Settings.tsx"
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
    source = _SETTINGS_TSX.read_text(encoding="utf-8")
    block_match = _JOB_META_BLOCK.search(source)
    assert block_match, "parsed no JOB_META declaration out of Settings.tsx -- the matcher is stale"
    block = block_match.group(1)

    scan_id = _SCAN_ID_CONST.search(source)
    assert scan_id, "parsed no SCAN_ID constant out of Settings.tsx -- the matcher is stale"
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
        "frontend/src/components/Settings.tsx's JOB_META and the jobs the server schedules "
        "disagree.\n"
        f"  scheduled with no copy: {sorted(set(SCHEDULABLE_JOB_IDS) - keys) or 'none'}\n"
        f"  copy for no such job:   {sorted(keys - set(SCHEDULABLE_JOB_IDS)) or 'none'}\n"
        "Add the title/desc/offWarning to JOB_META, or drop the stale entry. The off-warning "
        "states what stops happening when the job is off (rule 55)."
    )
