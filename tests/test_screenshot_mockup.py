# SPDX-License-Identifier: AGPL-3.0-or-later
"""Checks that the shipped screenshot is safe to publish.

The picture in the README is the one thing in this repository an operator judges the app by
before installing it, and the one place that could leak a real library: a real capture would
carry a real account name, real titles, and real cover art. `gen_screenshot_mockup.py` draws
it from invented data instead, and this file checks that the invented data stays invented.

Rendering the picture needs Chrome, so this file does not render a PNG. It checks everything
that does not need Chrome: that the generator still runs against the current stylesheet, that
the page it emits reaches no host and embeds no image it did not draw, and that the committed
PNG is the size the generator would produce, which is what goes stale first when the capture
box changes.
"""

from __future__ import annotations

import importlib.util
import re
import struct
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "gen_screenshot_mockup.py"
SHOT = ROOT / "docs" / "media" / "review-queue-mockup.png"


def _generator() -> ModuleType:
    """Imports the generator script as a module.

    `scripts/` is not a package, so a normal import statement cannot reach it.
    """
    spec = importlib.util.spec_from_file_location("gen_screenshot_mockup", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def gen() -> ModuleType:
    return _generator()


@pytest.fixture(scope="module")
def html(gen: ModuleType) -> str:
    page: str = gen.page()
    return page


def test_the_generator_builds_a_page_from_the_current_stylesheet(
    gen: ModuleType, html: str
) -> None:
    """It inlines `frontend/src/index.css`, so a renamed style file breaks this, not the README.

    The page carries no <link>, so the stylesheet has to arrive by this path or not at all.
    """
    assert gen.stylesheet().count("SPDX-License-Identifier") >= 20, "lost most of the stylesheet"
    # A token from 00-tokens.css and a rule from the queue's own sheet cover both halves of
    # the import list, so a truncated concatenation cannot pass this check.
    assert "--condemn:" in html
    assert ".card-scrim" in html
    assert 'data-theme="dark"' in html


def test_nothing_in_the_picture_comes_off_a_real_server(html: str) -> None:
    """Every image on the page is one this script drew. Nothing on it loads over the network.

    A remote `src` would put someone else's artwork in the README. A local one would put a
    path from this machine there instead. An `img` tag may only carry a `data:` URI.
    """
    srcs = re.findall(r'<img[^>]*\ssrc="([^"]{0,40})', html)
    assert srcs, "no images at all -- the page lost its art"
    assert all(src.startswith("data:image/svg+xml;base64,") for src in srcs), srcs

    # The only href on the page is `#`, used by the jump pills. Any real host here would let
    # the page fetch something when it is viewed, which could pull in outside artwork.
    hrefs = set(re.findall(r'href="([^"]*)"', html))
    assert hrefs <= {"#"}, hrefs
    assert "http://" not in html and "https://" not in html.replace(
        "http://www.w3.org/2000/svg", ""
    )


def test_the_account_and_the_titles_are_invented(gen: ModuleType, html: str) -> None:
    """This test exists so a later edit that pastes in a real library must delete a test
    to land."""
    assert gen.ACCOUNT == "reaper-demo"
    assert gen.ACCOUNT in html
    # Every row shown on screen comes from this invented list.
    assert len(gen.ITEMS) == 10
    for item in gen.ITEMS:
        assert item["title"] in html


def test_the_shipped_png_is_the_size_this_generator_shoots(gen: ModuleType) -> None:
    """Checks that the committed PNG still matches the generator's output size.

    The capture box can move without anyone updating the committed PNG, so this compares
    the two sizes directly by reading the PNG header's width and height fields.

    Re-shoot with `uv run python scripts/gen_screenshot_mockup.py --render`.
    """
    header = SHOT.read_bytes()[:24]
    assert header[:8] == b"\x89PNG\r\n\x1a\n", "not a PNG"
    width, height = struct.unpack(">II", header[16:24])
    assert (width, height) == (gen.WIDTH * gen.SCALE, gen.HEIGHT * gen.SCALE)
