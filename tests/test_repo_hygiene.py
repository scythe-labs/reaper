"""Repo-wide invariants that were previously only prose in the instruction files.

CLAUDE.md and ``.claude/rules/*.md`` are context, not enforcement: an agent that never
loads them, or a human who never reads them, breaks the rule silently. Every rule in here
is one that can be checked mechanically, so it costs nothing to enforce and catches humans
and agents alike. A rule that needs judgment stays prose; only the greppable ones live here.

These are filesystem checks -- no app boot, no fixtures, no network.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SELF = Path(__file__).resolve()
SRC = REPO / "src" / "reaper"
TESTS = REPO / "tests"
FRONTEND_SRC = REPO / "frontend" / "src"

INSTRUCTION_FILES = [REPO / "CLAUDE.md", *sorted((REPO / ".claude" / "rules").glob("*.md"))]

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


def test_every_rule_citation_in_code_resolves() -> None:
    """A comment may only cite a rule that exists.

    A review pass found 37 comments citing rules 70-87 while the list ended at 69, making
    every one of them unverifiable. This is the guard that makes that impossible.
    """
    defined = _defined_rules()
    dangling: list[str] = []
    for path in _code_files():
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
    for path in _code_files():
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
