# SPDX-License-Identifier: AGPL-3.0-or-later
"""What holds the shipped screenshot honest.

The picture in the README is the one artifact in this repository that an operator judges the
app by before installing it, and the one that could quietly leak somebody's library: a real
capture carries a real account name, real titles and real cover art. `gen_screenshot_mockup.py`
draws it from invented data instead, and these are the properties that has to keep.

Rendering needs Chrome, so nothing here shoots a PNG. What it does check is everything that
does not: that the generator still runs against the current stylesheet, that the page it emits
reaches no host and embeds no image it did not draw, and that the committed PNG is the size
that generator would produce -- which is what goes stale first when the capture box moves.
"""

from __future__ import annotations

import importlib.util
import re
import struct
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "gen_screenshot_mockup.py"
SHOT = ROOT / "docs" / "media" / "review-queue.png"


def _generator():
    """The script, imported as a module. It lives in `scripts/`, which is not a package."""
    spec = importlib.util.spec_from_file_location("gen_screenshot_mockup", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def gen():
    return _generator()


@pytest.fixture(scope="module")
def html(gen) -> str:
    return gen.page()


def test_the_generator_builds_a_page_from_the_current_stylesheet(gen, html: str) -> None:
    """It inlines `frontend/src/index.css`, so a renamed style file breaks this, not the README.

    The page carries no <link>, so the stylesheet has to arrive by this path or not at all.
    """
    assert gen.stylesheet().count("SPDX-License-Identifier") >= 20, "lost most of the stylesheet"
    # A token from 00-tokens.css and a rule from the queue's own sheet: both halves of the
    # import list, so a truncated concatenation cannot pass.
    assert "--condemn:" in html
    assert ".card-scrim" in html
    assert 'data-theme="dark"' in html


def test_nothing_in_the_picture_comes_off_a_real_server(html: str) -> None:
    """Every image is one this script drew, and nothing loads.

    A remote `src` would put someone else's artwork in the README; a local one would put a
    path off this machine there. Both are the same failure, so neither is allowed: an `img`
    may only carry a `data:` URI.
    """
    srcs = re.findall(r'<img[^>]*\ssrc="([^"]{0,40})', html)
    assert srcs, "no images at all -- the page lost its art"
    assert all(src.startswith("data:image/svg+xml;base64,") for src in srcs), srcs

    # The only URLs on the page are the dead `#` the jump pills wear. A real host here would
    # mean the page fetches something at view time, which is how art gets in by the back door.
    hrefs = set(re.findall(r'href="([^"]*)"', html))
    assert hrefs <= {"#"}, hrefs
    assert "http://" not in html and "https://" not in html.replace(
        "http://www.w3.org/2000/svg", ""
    )


def test_the_account_and_the_titles_are_invented(gen, html: str) -> None:
    """Named here so a later edit that pastes in a real library has to delete a test to land."""
    assert gen.ACCOUNT == "reaper-demo"
    assert gen.ACCOUNT in html
    # Every row on screen is one of these, and each is drawn rather than fetched.
    assert len(gen.ITEMS) == 10
    for item in gen.ITEMS:
        assert item["title"] in html


def test_the_shipped_png_is_the_size_this_generator_shoots(gen) -> None:
    """The drift that actually happens: the capture box moves and the committed PNG does not.

    Re-shoot with `uv run python scripts/gen_screenshot_mockup.py --render`. Read straight from
    the PNG header, so this needs neither Chrome nor an image library.
    """
    header = SHOT.read_bytes()[:24]
    assert header[:8] == b"\x89PNG\r\n\x1a\n", "not a PNG"
    width, height = struct.unpack(">II", header[16:24])
    assert (width, height) == (gen.WIDTH * gen.SCALE, gen.HEIGHT * gen.SCALE)
