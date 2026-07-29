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
DOCS = REPO / "docs"
HISTORY = DOCS / "history"

# docs/ splits by how long a statement stays true (see docs/README.md). Live docs are held to
# the same standards as code; docs/history/ is frozen and deliberately describes the past, so
# its stale dates, TBD placeholders and superseded rule wordings are correct as written.
STATUS_DOC = DOCS / "STATUS.md"
STATUS_MAX_LINES = 200


def _live_docs() -> list[Path]:
    """Every doc that claims to describe the present. Excludes the frozen archive."""
    return sorted(p for p in DOCS.rglob("*.md") if HISTORY not in p.parents)


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

#: The shipped ``CMD``, ``scripts/dev-local.sh``, ``README.md``, ``.claude/launch.json`` and
#: ``.claude/skills/verify/SKILL.md``. Pinned because "every launch carries the flag" is only
#: worth as much as the walk that finds them: the flag assertion below cannot distinguish a
#: launch that complies from one this matcher no longer sees, and both read as green (rule 145).
_EXPECTED_LAUNCHES = 5


def _uvicorn_launches() -> list[tuple[Path, int, str]]:
    """Every line in one of THIS checkout's own text files that boots the app under uvicorn.

    Scoped to this checkout on purpose. ``.claude/worktrees/`` is gitignored
    (``.gitignore``) and holds agent worktrees, which are entire repo copies sitting inside
    the repo root -- and ``rglob`` honors no ignore file, so walking into them judges other
    branches' launches as if they were ours. A worktree's ``.git`` is a *file*, so the skip
    entry below does not stop the descent either. Left in, ``uv run pytest`` in the main
    checkout fails the moment any worktree cut before this fix is still on disk, naming files
    the branch under test cannot reach. A gate nobody can turn green from their own branch is
    a gate that gets deleted.

    The skip is matched on the REPO-RELATIVE path, which is the part that is easy to get
    backwards: ``skip`` is tested against ``path.parts``, so putting ``"worktrees"`` in it
    would match the ABSOLUTE path and skip every file in the tree whenever the suite runs
    from inside a worktree -- which is where these sessions run it. The relative form also
    stops an ancestor directory outside the repo that happens to be named ``dist`` from
    silently emptying the walk.
    """
    found: list[tuple[Path, int, str]] = []
    skip = {".git", "node_modules", ".venv", "dist", "__pycache__", ".ruff_cache", ".mypy_cache"}
    for path in REPO.rglob("*"):
        if not path.is_file() or path.resolve() == SELF:
            continue
        relative = path.relative_to(REPO)
        if any(p in skip for p in relative.parts) or relative.parts[:2] == (".claude", "worktrees"):
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for lineno, line in enumerate(text.splitlines(), 1):
            if _UVICORN_LAUNCH.search(line) and "--factory" in line:
                found.append((path, lineno, line.strip()))
    return found


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
        "Move history to docs/history/ and measured findings to docs/LEARNINGS.md, "
        "then shorten what is left. Do not raise this number to make the test pass."
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
# Re-derive it by running the test, never by arithmetic on this comment.
_EXPECTED_NOTICES = 109


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


# Every sentence in the shipped app that says the word "reload", by the file that renders it.
#
# A reload discards whatever is typed, staged or selected, and there is no ``beforeunload``
# handler anywhere in ``frontend/src`` to ask first -- so this advice is destructive exactly where
# there is something to destroy. #153 took it off the shared ``StaleReadNotice``; #195 took it off
# the eight hand-written siblings that render while a draft, a pasted secret or a bulk selection is
# on screen. What is left is here so the next one has to be classified rather than typed.
#
# Per file, and every entry is a deliberate keep:
#   App.tsx (1)              the reap sheet's loader. Not in #195's enumeration; see the note below
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
#   Settings.tsx (6)         six never-loaded branches, each above a form that never rendered
#
# **#195's enumeration was not the whole population**, which is why this counts rather than
# trusting the issue: it named 8 to fix and 9 to leave, called that 15, and did not reach the reap
# sheet, the plan loader, the ledger refusal or this panel at all. Those four are kept here as a
# question, not as a settled answer -- a reload from any of them drops a bulk selection made in
# the queue underneath, which nobody has demonstrated (#225).
_RELOAD_ADVICE = {
    "frontend/src/App.tsx": 1,
    "frontend/src/components/Fairness.tsx": 1,
    "frontend/src/components/NotInScanPanel.tsx": 1,
    "frontend/src/components/PlexPanel.tsx": 1,
    "frontend/src/components/PolicyEditor.tsx": 1,
    "frontend/src/components/ReapBreakdown.tsx": 1,
    "frontend/src/components/ReapConfirm.tsx": 1,
    "frontend/src/components/ReapPlan.tsx": 1,
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
_EXPECTED_SELECTS = 19


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
