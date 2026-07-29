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
# 108, not the 109 hand-rolled sites this replaced: the two draft-refusal notices were
# byte-identical twins in Settings and PolicyEditor, and both now render through the single
# Notice inside ``SwitchConfirm`` (rule 18). The two figures mean different things and are
# deliberately not derived from each other.
_EXPECTED_NOTICES = 108


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
    # The bare ``notice`` token, not a substring of one: ``budget-notice`` and ``kept-notice``
    # are layout classes that ride ON a Notice via its ``className`` prop, and a ``\bnotice\b``
    # match counts them as offenders because the hyphen is a word boundary.
    classes = re.compile(r'className=\{?["`]([^"`}]*)["`]')
    offenders = [
        f"{p.relative_to(REPO)}:{n}"
        for p in _shipped_tsx()
        if p != component
        for n, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1)
        for m in classes.finditer(_strip_prose(line))
        if "notice" in m.group(1).split()
    ]
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
