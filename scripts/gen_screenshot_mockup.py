#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Build an invented review-queue screenshot, kept as the spare.

The README shows a real screenshot now, of the maintainer's own instance, because a picture
of invented titles sells nothing. This still renders the same screen from invented data, and
it is kept so that call can be reversed with a one-line README edit rather than rebuilt.

The markup is the markup `ReviewQueue`/`WhyPanel` emit, the stylesheet is
`frontend/src/index.css` inlined in its own load order, and every poster and backdrop is drawn
here as flat SVG, so nothing in the output belongs to anyone.

    uv run python scripts/gen_screenshot_mockup.py           # write the page
    uv run python scripts/gen_screenshot_mockup.py --render  # and shoot it with headless Chrome

`--render` wants Chrome and writes a 2x PNG beside the page, which is the artifact the README
would show if this call were reversed. The page itself is a build product and is not committed.

Rendering needs a browser, so CI cannot re-shoot it. `tests/test_screenshot_mockup.py` gates
what it can without one: that this script still runs against the current stylesheet, that the
page it emits fetches nothing and embeds only art drawn here, and that the committed PNG is the
size this capture box produces (rule 68).
"""

from __future__ import annotations

import argparse
import base64
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STYLES = ROOT / "frontend" / "src"
OUT_DIR = ROOT / "docs" / "media"
PAGE = OUT_DIR / "review-queue-mockup.html"
SHOT = OUT_DIR / "review-queue-mockup.png"

# The capture box. 1440 CSS px keeps the split view (the panel sits beside the list above
# 1100px) and 2x keeps the text sharp where a README scales the picture down. The height is
# measured, not guessed: it clears the sixth card's bottom edge (1114) and the panel's (1109),
# and stops short of the seventh card's top (1124), so the picture ends on a whole row rather
# than slicing one in half. Re-measure it after any change to the list OR to what sits above it
# -- the cards below are simply outside the frame.
WIDTH = 1440
HEIGHT = 1120
SCALE = 2

CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"


# --------------------------------------------------------------------------------------------
# The stylesheet, in the order index.css imports it -- that order is load-bearing (see the
# ordering note at the top of index.css), so this reads the import list rather than globbing.
# --------------------------------------------------------------------------------------------
def stylesheet() -> str:
    index = (STYLES / "index.css").read_text()
    parts = []
    for rel in re.findall(r'@import\s+"\./([^"]+)"', index):
        parts.append((STYLES / rel).read_text())
    if not parts:
        raise SystemExit("no @import lines in index.css -- the load order moved")
    return "\n".join(parts)


# --------------------------------------------------------------------------------------------
# Invented cover art. Two flat SVGs per title from one palette: a poster for the card thumbnail
# and a wide backdrop for the row wash and the panel hero.
# --------------------------------------------------------------------------------------------
def data_uri(svg: str) -> str:
    packed = base64.b64encode(" ".join(svg.split()).encode()).decode()
    return f"data:image/svg+xml;base64,{packed}"


def poster(title: str, year: str, palette: tuple[str, str, str], shape: str) -> str:
    top, bottom, ink = palette
    words, lines, line = title.split(), [], ""
    for word in words:
        candidate = f"{line} {word}".strip()
        if len(candidate) > 11 and line:
            lines.append(line)
            line = word
        else:
            line = candidate
    lines.append(line)
    lines = lines[:3]

    art = {
        "arc": (
            f'<circle cx="150" cy="150" r="118" fill="none" stroke="{ink}" stroke-width="10"'
            ' opacity=".5"/>'
            f'<circle cx="150" cy="150" r="72" fill="{ink}" opacity=".28"/>'
        ),
        "bars": (
            f'<rect x="26" y="66" width="248" height="13" fill="{ink}" opacity=".55"/>'
            f'<rect x="26" y="102" width="180" height="13" fill="{ink}" opacity=".38"/>'
            f'<rect x="26" y="138" width="212" height="13" fill="{ink}" opacity=".26"/>'
            f'<rect x="26" y="174" width="120" height="13" fill="{ink}" opacity=".16"/>'
        ),
        "peak": (
            f'<path d="M0 300 L96 150 L168 236 L228 168 L300 300Z" fill="{ink}" opacity=".45"/>'
            f'<circle cx="216" cy="92" r="42" fill="{ink}" opacity=".35"/>'
        ),
        "eye": (
            f'<path d="M40 150C40 150 92 82 150 82C208 82 260 150 260 150C260 150 208 218'
            f' 150 218C92 218 40 150 40 150Z" fill="none" stroke="{ink}" stroke-width="9"'
            ' opacity=".5"/>'
            f'<circle cx="150" cy="150" r="38" fill="{ink}" opacity=".4"/>'
        ),
        "wave": (
            f'<path d="M0 214C60 176 96 250 150 214C204 178 240 252 300 214V300H0Z"'
            f' fill="{ink}" opacity=".4"/>'
            f'<path d="M0 250C60 212 96 286 150 250C204 214 240 288 300 250V300H0Z"'
            f' fill="{ink}" opacity=".25"/>'
        ),
        "grid": (
            f'<g fill="{ink}">'
            f'<rect x="44" y="70" width="86" height="86" opacity=".42"/>'
            f'<rect x="168" y="70" width="86" height="86" opacity=".24"/>'
            f'<rect x="44" y="192" width="86" height="86" opacity=".2"/>'
            f'<rect x="168" y="192" width="86" height="86" opacity=".36"/>'
            "</g>"
        ),
    }[shape]

    text_y = 452 - 34 * len(lines)
    rows = "".join(
        f'<text x="150" y="{text_y + i * 34}" text-anchor="middle" fill="#fff"'
        ' font-family="Helvetica Neue, Helvetica, Arial, sans-serif" font-size="27"'
        f' font-weight="700" letter-spacing="-.4">{line}</text>'
        for i, line in enumerate(lines)
    )
    return data_uri(f"""
      <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 300 450" width="300" height="450">
        <defs>
          <linearGradient id="g" x1="0" y1="0" x2="1" y2="1">
            <stop offset="0" stop-color="{top}"/><stop offset="1" stop-color="{bottom}"/>
          </linearGradient>
          <linearGradient id="v" x1="0" y1="0" x2="0" y2="1">
            <stop offset=".45" stop-color="#000" stop-opacity="0"/>
            <stop offset="1" stop-color="#000" stop-opacity=".72"/>
          </linearGradient>
        </defs>
        <rect width="300" height="450" fill="url(#g)"/>
        {art}
        <rect width="300" height="450" fill="url(#v)"/>
        <rect x="0" y="0" width="300" height="450" fill="none" stroke="#fff" stroke-opacity=".14"
              stroke-width="2"/>
        {rows}
        <text x="150" y="{text_y + 34 * len(lines) - 6}" text-anchor="middle" fill="#fff"
              fill-opacity=".62" font-family="Helvetica Neue, Helvetica, Arial, sans-serif"
              font-size="17" letter-spacing="3">{year}</text>
      </svg>
    """)


def backdrop(palette: tuple[str, str, str], shape: str) -> str:
    # Bright end first. A row's art rides at 0.22 opacity under `.card-scrim` (21-queue-cards.css),
    # so a backdrop built from the poster's dark end lands on near-black and the row reads as
    # having no art at all -- which is the one thing the real queue never looks like.
    top, _bottom, ink = palette
    blob = top
    # Everything worth seeing sits in the upper-middle band. Both crops crossing this image are
    # wide and short -- a card row at `object-position: center 20%`, the panel hero at 25% -- so
    # art drawn down at the baseline is cropped away and the surface reads as a bare gradient.
    art = {
        "arc": (
            f'<circle cx="1140" cy="330" r="270" fill="{blob}" opacity=".38"/>'
            f'<circle cx="1140" cy="330" r="150" fill="{ink}" opacity=".3"/>'
        ),
        "bars": (
            f'<rect x="0" y="170" width="1600" height="60" fill="{blob}" opacity=".34"/>'
            f'<rect x="0" y="300" width="1180" height="60" fill="{blob}" opacity=".26"/>'
            f'<rect x="0" y="430" width="1420" height="60" fill="{blob}" opacity=".18"/>'
        ),
        "peak": (
            f'<path d="M0 900 L470 140 L820 520 L1150 240 L1600 900Z" fill="{blob}"'
            ' opacity=".42"/>'
            f'<circle cx="1300" cy="200" r="110" fill="{ink}" opacity=".45"/>'
        ),
        "eye": (
            f'<ellipse cx="800" cy="340" rx="520" ry="250" fill="none" stroke="{blob}"'
            ' stroke-width="46" opacity=".4"/>'
            f'<circle cx="800" cy="340" r="140" fill="{ink}" opacity=".34"/>'
        ),
        "wave": (
            f'<path d="M0 380C260 290 420 500 800 390C1180 280 1340 500 1600 410V620H0Z"'
            f' fill="{blob}" opacity=".4"/>'
            f'<path d="M0 250C300 190 500 330 800 260C1100 190 1320 330 1600 270V330H0Z"'
            f' fill="{ink}" opacity=".26"/>'
        ),
        "grid": (
            f'<g fill="{blob}" opacity=".34">'
            '<rect x="140" y="120" width="320" height="320"/>'
            '<rect x="640" y="260" width="320" height="320"/>'
            '<rect x="1140" y="120" width="320" height="320"/>'
            "</g>"
        ),
    }[shape]
    return data_uri(f"""
      <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1600 900" width="1600" height="900">
        <defs>
          <linearGradient id="g" x1="0" y1="0" x2="1" y2="1">
            <stop offset="0" stop-color="{ink}"/><stop offset="1" stop-color="{top}"/>
          </linearGradient>
          <radialGradient id="h" cx=".7" cy=".3" r=".8">
            <stop offset="0" stop-color="#fff" stop-opacity=".38"/>
            <stop offset="1" stop-color="#fff" stop-opacity="0"/>
          </radialGradient>
          <filter id="s"><feGaussianBlur stdDeviation="10"/></filter>
        </defs>
        <rect width="1600" height="900" fill="url(#g)"/>
        <g filter="url(#s)">{art}</g>
        <rect width="1600" height="900" fill="url(#h)"/>
      </svg>
    """)


# --------------------------------------------------------------------------------------------
# The library on screen. Invented outright: no title, person, size or count here came off a
# real server, and none is meant to name a real work.
# --------------------------------------------------------------------------------------------
PALETTES = {
    "rust": ("#7a2f22", "#2a1512", "#f0a35a"),
    "plum": ("#4a2352", "#1b1024", "#e8a0d8"),
    "sea": ("#123f52", "#0a1a24", "#63d7d2"),
    "moss": ("#26432a", "#101a12", "#8fd26a"),
    "dusk": ("#2b2f6b", "#111228", "#8b9bf0"),
    "ember": ("#6b2213", "#22100b", "#ffb27a"),
    "slate": ("#2d3742", "#12171c", "#a8c0d4"),
    "wine": ("#571f2c", "#1d0e13", "#f08a9c"),
    "gold": ("#5c4413", "#1e1708", "#f5cd68"),
    "ice": ("#1e3a4f", "#0c1720", "#9fd6f5"),
}

ITEMS = [
    {
        "kind": "show",
        "title": "Kitchen Rescue Squad",
        "year": "2011",
        "art": ("rust", "bars"),
        "seasons": 9,
        "meta": "7 of 9 would be removed, 61.4 GiB",
        "dormant": "5y 6m",
        "override": "reap",
        "strip": [(n, "condemn" if n <= 7 else "abstain") for n in range(1, 10)],
    },
    {
        "kind": "show",
        "title": "Frosting Wars",
        "year": "2017",
        "art": ("plum", "grid"),
        "seasons": 7,
        "meta": "7 of 7 would be removed, 44.2 GiB",
        "dormant": "4y 11m",
        "override": "reap",
        "strip": [(n, "condemn") for n in range(1, 8)],
    },
    {
        "kind": "movie",
        "title": "The Velvet Heist",
        "year": "1967",
        "art": ("gold", "arc"),
        "size": "1.0 GiB",
        "res": ("HD", "720p"),
        "dormant": "6y 2m",
        "score": 94,
    },
    {
        "kind": "movie",
        "title": "Sand Wyrms Deep Reef",
        "year": "2020",
        "art": ("sea", "wave"),
        "size": "5.5 GiB",
        "res": ("HD", "720p"),
        "dormant": "5y 8m",
        "score": 91,
        "override": "spare",
        "open": True,
    },
    {
        "kind": "movie",
        "title": "Whiskers and Sons 2",
        "year": "2002",
        "art": ("moss", "peak"),
        "size": "4.8 GiB",
        "res": ("HD", "1080p"),
        "dormant": "5y 3m",
        "score": 91,
    },
    {
        "kind": "movie",
        "title": "The Night Shift Job",
        "year": "2018",
        "art": ("dusk", "eye"),
        "size": "4.4 GiB",
        "res": ("HD", "720p"),
        "dormant": "7y 6m",
        "score": 90,
    },
    {
        "kind": "movie",
        "title": "Vector Zero Hour",
        "year": "2017",
        "art": ("ember", "bars"),
        "size": "6.1 GiB",
        "res": ("HD", "1080p"),
        "dormant": "5y 6m",
        "score": 89,
    },
    {
        "kind": "movie",
        "title": "Hollowmark",
        "year": "2020",
        "art": ("wine", "arc"),
        "size": "3.2 GiB",
        "res": ("HD", "1080p"),
        "dormant": "5y 1m",
        "score": 88,
    },
    {
        "kind": "show",
        "title": "Ironworks",
        "year": "2020",
        "art": ("slate", "grid"),
        "seasons": 4,
        "meta": "4 of 4 would be removed, 39.8 GiB",
        "dormant": "5y 5m",
        "override": "reap",
        "strip": [(n, "condemn") for n in range(1, 5)],
    },
    {
        "kind": "movie",
        "title": "Table for Three",
        "year": "2009",
        "art": ("ice", "wave"),
        "size": "9.7 GiB",
        "res": ("HD", "1080p"),
        "dormant": "6y 6m",
        "score": 86,
    },
]

ACCOUNT = "reaper-demo"
SCANNED = "Last scanned 3 hours ago, 4,812 items."
TOTAL_ITEMS = "418"
TOTAL_SIZE = "3.9 TiB"

SYNOPSIS = (
    "A salvage crew working a drowned reef wakes something that has been feeding on the wrecks "
    "for a century, and the only way off the water is straight through it."
)


# --------------------------------------------------------------------------------------------
# Icons, lifted from the components that draw them so the picture wears the app's own glyphs.
# --------------------------------------------------------------------------------------------
SCYTHE = (
    '<svg class="scythe" viewBox="0 0 48 48" width="13" height="13" fill="none" aria-hidden="true">'
    '<path d="M41 10C33 3 15 6 6 17C15 12 28 13 38 16Z" fill="currentColor"/>'
    '<path d="M38 12 21 42" stroke="currentColor" stroke-width="5.5" stroke-linecap="round"/>'
    "</svg>"
)
CLOCK = (
    '<svg viewBox="0 0 16 16" width="12" height="12" fill="none" aria-hidden="true">'
    '<circle cx="8" cy="8" r="6.2" stroke="currentColor" stroke-width="1.4"/>'
    '<path d="M8 4.6V8l2.4 1.4" stroke="currentColor" stroke-width="1.4"'
    ' stroke-linecap="round"/></svg>'
)
LIBRARY = (
    '<svg viewBox="0 0 16 16" width="14" height="14" fill="none" aria-hidden="true">'
    '<rect x="2.5" y="3" width="2.4" height="10" rx="0.6" stroke="currentColor"'
    ' stroke-width="1.2"/>'
    '<rect x="5.8" y="3" width="2.4" height="10" rx="0.6" stroke="currentColor"'
    ' stroke-width="1.2"/>'
    '<path d="M9.6 4l2.4.6-1.9 8.2-2.4-.6" stroke="currentColor" stroke-width="1.2"'
    ' stroke-linejoin="round"/></svg>'
)
CHEVRON = (
    '<svg class="chevron" viewBox="0 0 12 12" width="11" height="11" aria-hidden="true">'
    '<path d="M4 2l4 4-4 4" fill="none" stroke="currentColor" stroke-width="1.8"/></svg>'
)
SEARCH = (
    '<svg class="search-icon" viewBox="0 0 16 16" width="14" height="14" aria-hidden="true">'
    '<circle cx="7" cy="7" r="4.5" fill="none" stroke="currentColor" stroke-width="1.5"/>'
    '<path d="M11 11l3 3" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/></svg>'
)
PLUS = (
    '<svg viewBox="0 0 16 16" width="14" height="14" fill="none" aria-hidden="true">'
    '<path d="M8 3v10M3 8h10" stroke="currentColor" stroke-width="1.6"'
    ' stroke-linecap="round"/></svg>'
)
SORT = (
    '<svg viewBox="0 0 16 16" width="14" height="14" fill="none" aria-hidden="true">'
    '<path d="M3 4h10M3 8h6M3 12h3" stroke="currentColor" stroke-width="1.4"'
    ' stroke-linecap="round"/></svg>'
)
SORT_DIR = (
    '<svg viewBox="0 0 16 16" width="14" height="14" fill="none" aria-hidden="true" class="desc">'
    '<path d="M8 3v10M4 9l4 4 4-4" stroke="currentColor" stroke-width="1.5"'
    ' stroke-linecap="round" stroke-linejoin="round"/></svg>'
)
CHECK_SQUARE = (
    '<svg viewBox="0 0 16 16" width="14" height="14" fill="none" aria-hidden="true">'
    '<rect x="2" y="2" width="12" height="12" rx="3" stroke="currentColor" stroke-width="1.4"/>'
    '<path d="M5 8.2l2 2 4-4.4" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"'
    ' stroke-linejoin="round"/></svg>'
)
CARET_DOWN = (
    '<svg viewBox="0 0 16 16" width="11" height="11" fill="none" aria-hidden="true">'
    '<path d="M4 6l4 4 4-4" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"'
    ' stroke-linejoin="round"/></svg>'
)
CLOSE = (
    '<svg viewBox="0 0 16 16" width="15" height="15" fill="none" aria-hidden="true">'
    '<path d="M4 4l8 8M12 4l-8 8" stroke="currentColor" stroke-width="1.8"'
    ' stroke-linecap="round"/></svg>'
)
TITLE_EXT = (
    '<svg class="title-ext" viewBox="0 0 16 16" width="13" height="13" fill="none"'
    ' aria-hidden="true"><path d="M6 3h7v7M13 3L4 12" stroke="currentColor" stroke-width="1.7"'
    ' stroke-linecap="round" stroke-linejoin="round"/></svg>'
)

NAV_ICONS = {
    "review": (
        '<svg class="view-ico" viewBox="0 0 16 16" fill="none" aria-hidden="true">'
        '<path d="M1.3 8s2.5-4.3 6.7-4.3S14.7 8 14.7 8s-2.5 4.3-6.7 4.3S1.3 8 1.3 8z"'
        ' stroke="currentColor" stroke-width="1.3" stroke-linejoin="round"/>'
        '<circle cx="8" cy="8" r="1.95" fill="currentColor"/></svg>'
    ),
    "policy": (
        '<svg class="view-ico" viewBox="0 0 16 16" fill="none" aria-hidden="true">'
        '<path d="M4.6 2h6.8A1.6 1.6 0 0 1 13 3.6v8.8a1.6 1.6 0 0 1-1.6 1.6H4.6A1.6 1.6 0 0 1 3'
        ' 12.4V3.6A1.6 1.6 0 0 1 4.6 2z" stroke="currentColor" stroke-width="1.3"'
        ' stroke-linejoin="round"/>'
        '<path d="M5.7 5.6h4.6M5.7 8h4.6M5.7 10.4h2.9" stroke="currentColor" stroke-width="1.3"'
        ' stroke-linecap="round"/></svg>'
    ),
    "reap": (
        '<svg class="view-ico" viewBox="0 0 48 48" fill="none" aria-hidden="true">'
        '<path d="M41 10C33 3 15 6 6 17C15 12 28 13 38 16Z" fill="currentColor"/>'
        '<path d="M38 12 21 42" stroke="currentColor" stroke-width="4.5"'
        ' stroke-linecap="round"/></svg>'
    ),
    "fairness": (
        '<svg class="view-ico" viewBox="0 0 16 16" fill="none" aria-hidden="true">'
        '<path d="M8 2.4v11M5.5 13.4h5M2.6 5.1h10.8" stroke="currentColor" stroke-width="1.3"'
        ' stroke-linecap="round"/>'
        '<path d="M.9 8.7h3.4L2.6 5.1zM11.7 8.7h3.4L13.4 5.1z" stroke="currentColor"'
        ' stroke-width="1.3" stroke-linejoin="round"/>'
        '<circle cx="8" cy="5.1" r="1" fill="currentColor"/></svg>'
    ),
    "settings": (
        '<svg class="view-ico" viewBox="0 0 16 16" fill="none" aria-hidden="true">'
        '<path d="M6.61 3.72L6.8 1.82L9.2 1.82L9.39 3.72A4.5 4.5 0 0 1 11.01 4.66L12.75'
        " 3.87L13.96 5.95L12.4 7.06A4.5 4.5 0 0 1 12.4 8.94L13.96 10.05L12.75 12.13L11.01"
        " 11.34A4.5 4.5 0 0 1 9.39 12.28L9.2 14.18L6.8 14.18L6.61 12.28A4.5 4.5 0 0 1 4.99"
        " 11.34L3.25 12.13L2.04 10.05L3.6 8.94A4.5 4.5 0 0 1 3.6 7.06L2.04 5.95L3.25 3.87L4.99"
        ' 4.66A4.5 4.5 0 0 1 6.61 3.72Z" stroke="currentColor" stroke-width="1.3"'
        ' stroke-linejoin="round"/>'
        '<circle cx="8" cy="8" r="2.05" stroke="currentColor" stroke-width="1.3"/></svg>'
    ),
}

# The masthead mark, mask and all -- BrandMark.tsx's shape with brand/dissolve.ts's geometry.
BRAND_BLOCKS_UPPER = [(19, 40, 7), (31, 40, 7), (25, 47, 7), (39, 47, 6), (19, 48, 6), (45, 40, 6)]
BRAND_BLOCKS_LOWER = [
    (33, 55, 6),
    (21, 56, 5),
    (46, 53, 5),
    (12, 49, 5),
    (40, 62, 4),
    (52, 46, 4),
    (27, 63, 4),
    (53, 58, 3),
]


def brand_mark() -> str:
    def rects(blocks, fill):
        return "".join(
            f'<rect x="{x}" y="{y}" width="{s}" height="{s}" fill="{fill}"/>' for x, y, s in blocks
        )

    hood = "M32 6c11 0 15 13 14 26 8 4 13 16 13 32H5c0-16 5-28 13-32C17 19 21 6 32 6Z"
    face = "M32 14c8 0 11 9 10 19-1 9-4 14-4 31H26c0-17-3-22-4-31-1-10 2-19 10-19Z"
    return (
        '<svg class="brand-mark sm" viewBox="0 0 64 64" aria-hidden="true">'
        '<mask id="bm" maskUnits="userSpaceOnUse" x="0" y="0" width="64" height="64">'
        '<rect width="64" height="64" fill="#000"/>'
        f'<path d="{hood}" fill="#fff"/>'
        '<rect x="0" y="40" width="64" height="24" fill="#000"/>'
        f"{rects(BRAND_BLOCKS_UPPER, '#fff')}"
        f'<path d="{face}" fill="#000"/>'
        f"{rects(BRAND_BLOCKS_LOWER, '#fff')}"
        "</mask>"
        '<g mask="url(#bm)"><rect width="64" height="64" fill="currentColor"/></g>'
        '<path d="M23.5 27.5 30 31.5v6.5l-6.5-4Z" fill="var(--accent)"/>'
        '<path d="M40.5 27.5 34 31.5v6.5l6.5-4Z" fill="var(--accent)"/>'
        "</svg>"
    )


# --------------------------------------------------------------------------------------------
# The markup, component by component -- each block mirrors the JSX named above it.
# --------------------------------------------------------------------------------------------
def dormant_pill(span: str) -> str:
    return f'<span class="dormant-pill">{CLOCK}Not watched in {span}</span>'


def override_controls(state: str | None) -> str:
    spare_class = "ov-btn ov-spare split-main" + (" active" if state == "spare" else "")
    caret_class = "ov-btn ov-spare split-caret" + (" active" if state == "spare" else "")
    reap_class = "ov-btn ov-reap" + (" active" if state == "reap" else "")
    label = "Spared" if state == "spare" else "Spare"
    reap_label = "Reaping" if state == "reap" else "Reap"
    return (
        '<div class="override-controls" role="group" aria-label="Spare or reap this item">'
        '<span class="ov-split">'
        f'<button type="button" class="{spare_class}">'
        f'<span class="infinity" aria-hidden="true">∞</span> <span class="ov-label">{label}</span>'
        "</button>"
        f'<button type="button" class="{caret_class}">{CARET_DOWN}</button>'
        "</span>"
        f'<button type="button" class="{reap_class}">{SCYTHE} {reap_label}</button>'
        "</div>"
    )


def override_mark(state: str | None) -> str:
    if state == "reap":
        return f'<span class="override-mark reap" aria-hidden="true">{SCYTHE}</span>'
    if state == "spare":
        return (
            '<span class="override-mark spare" aria-hidden="true">'
            '<span class="mk-inf">∞</span></span>'
        )
    return ""


def override_chip(state: str | None) -> str:
    if state == "reap":
        return '<span class="chip chip-hand-reap">Reaped by hand, will be removed</span>'
    if state == "spare":
        return '<span class="chip chip-hand-spare">Spared by hand, will be kept</span>'
    return ""


def movie_card(item: dict) -> str:
    key, shape = item["art"]
    palette = PALETTES[key]
    state = item.get("override")
    classes = ["card", "clickable"]
    if state:
        classes.append("card-spared" if state == "spare" else "card-reaped")
    if item.get("open"):
        classes += ["card-selected", "mock-hover"]
    label, detail = item["res"]
    score_class = {"spare": "score-spare", "reap": "score-reap"}.get(state, "score-condemn")
    return f"""
      <article class="{" ".join(classes)}">
        <img class="card-bg" src="{backdrop(palette, shape)}" alt="" aria-hidden="true">
        <div class="card-scrim" aria-hidden="true"></div>
        <img class="poster" src="{poster(item["title"], item["year"], palette, shape)}"
             alt="{item["title"]}">
        <div class="card-body">
          <div class="card-title-row">
            <h3 class="card-title">
              <button type="button" class="card-open">{item["title"]}</button>
              <span class="card-year"> {item["year"]}</span>
            </h3>
            {override_chip(state)}
          </div>
          <div class="card-meta">
            <span class="chip chip-movie">Movie</span>
            <span class="lib-chip">{LIBRARY}Movies</span>
            <span>{item["size"]}</span>
            <span class="res-badge">{label}<span class="res-detail">&nbsp;{detail}</span></span>
          </div>
          {dormant_pill(item["dormant"])}
        </div>
        <div class="card-side">
          <span class="score {score_class}">{item["score"]}</span>
          {override_mark(state)}
          {override_controls(state)}
        </div>
      </article>
    """


def show_card(item: dict) -> str:
    key, shape = item["art"]
    palette = PALETTES[key]
    state = item.get("override")
    classes = ["card", "card-show"]
    if state:
        classes.append("card-spared" if state == "spare" else "card-reaped")
    squares = "".join(
        f'<button type="button" class="strip-sq strip-{verdict}'
        f'{" strip-ov-reap" if state == "reap" and verdict == "condemn" else ""}">{n}</button>'
        for n, verdict in item["strip"]
    )
    return f"""
      <article class="{" ".join(classes)}">
        <div class="card-head clickable">
          <img class="card-bg" src="{backdrop(palette, shape)}" alt="" aria-hidden="true">
          <div class="card-scrim" aria-hidden="true"></div>
          <img class="poster" src="{poster(item["title"], item["year"], palette, shape)}"
               alt="{item["title"]}">
          <div class="card-body">
            <div class="card-title-row">
              <h3 class="card-title">
                <button type="button" class="card-open">{item["title"]}</button>
                <span class="card-year"> {item["year"]}</span>
              </h3>
              {override_chip(state)}
            </div>
            <div class="card-meta">
              <span class="chip chip-tv">TV</span>
              <span class="lib-chip">{LIBRARY}TV Shows</span>
              <button type="button" class="season-expander" aria-expanded="false">
                {CHEVRON}{item["seasons"]} seasons
              </button>
              <span>{item["meta"]}</span>
              <span class="chip" role="img" aria-label="This show has ended">Ended</span>
            </div>
            <div class="season-strip">{squares}</div>
            {dormant_pill(item["dormant"])}
          </div>
          <div class="card-side">
            {override_mark(state)}
            {override_controls(state)}
          </div>
        </div>
      </article>
    """


def signal_row(share: str, detail: str, footprint: float, added: float) -> str:
    return f"""
      <li class="sig-row sig-adds">
        <div class="sig-head">
          <span class="sig-share">{share}</span>
          <span class="sig-detail">{detail}</span>
        </div>
        <div class="sig-track">
          <div class="sig-foot" style="width: {footprint * 100:.0f}%">
            <div class="sig-added" style="width: {added * 100:.0f}%"></div>
          </div>
        </div>
      </li>
    """


def why_panel() -> str:
    item = next(i for i in ITEMS if i.get("open"))
    key, shape = item["art"]
    palette = PALETTES[key]
    art = backdrop(palette, shape)
    rows = (
        signal_row("+70", "not watched in 5 years, 8 months", 1.0, 1.0)
        + signal_row("+20", "nobody watched it in the last year", 0.29, 1.0)
        + signal_row("+1", "IMDb 5.1", 0.06, 1.0)
    )
    return f"""
      <div class="why" role="complementary" tabindex="0">
        <button type="button" class="why-close" aria-label="Close">{CLOSE}</button>
        <div class="why-hero">
          <img src="{art}" alt="" aria-hidden="true">
          <div class="why-hero-fade" aria-hidden="true"></div>
        </div>
        <div class="why-head">
          <div>
            <h2>
              <a class="title-link" href="#">Sand Wyrms Deep Reef<span class="card-year">
                 2020</span>{TITLE_EXT}</a>
            </h2>
            <p class="muted why-sub">
              5.5 GiB, movie
              <span class="lib-chip">{LIBRARY}Movies</span>
              <a class="jump-pill" href="#">Tautulli <span aria-hidden="true">↗</span></a>
              <a class="jump-pill" href="#">Seerr <span aria-hidden="true">↗</span></a>
              <a class="jump-pill" href="#">Radarr <span aria-hidden="true">↗</span></a>
            </p>
          </div>
        </div>
        <p class="why-meta"><span class="cert">PG-13</span>98 min, Action, Horror,
           Science Fiction</p>
        <div class="why-ratings">
          <span class="rating-chip"><span class="rating-src rating-imdb">IMDb</span> 5.1</span>
          <span class="rating-chip"><span class="rating-src" role="img"
                aria-label="Rotten Tomatoes critics">🍅</span> 48%</span>
          <span class="rating-chip"><span class="rating-src" role="img"
                aria-label="Rotten Tomatoes audience">🍿</span> 39%</span>
          <span class="rating-chip"><span class="rating-src">TMDb</span> 57%</span>
          <span class="rating-chip"><span class="rating-src">Trakt</span> 55%</span>
        </div>
        <p class="why-summary"><span class="clamp-2">{SYNOPSIS}</span>
           <button class="link-btn">more</button></p>

        <div class="verdict verdict-protect">
          <div class="verdict-label">Spared by hand</div>
          <div class="verdict-score"><strong>91</strong>
            <span class="muted">/100, your threshold is 58</span></div>
          <p class="verdict-note">You chose to keep this, so it won't be removed.</p>
        </div>

        <section class="block">
          <h3>Why it scored 91</h3>
          <p class="blurb">Reasons to believe nobody will watch it again. Reaper saw
             <strong>100%</strong> of the evidence.</p>
          <div class="sig-group">
            <div class="sig-group-head">
              <h4>Pushed to remove</h4><span class="sig-group-total">+91</span>
            </div>
            <ul class="signals">{rows}</ul>
          </div>
          <p class="sig-legend">
            <span class="sig-key sig-key-added"></span>Added
            <span class="sig-key sig-key-held"></span>Held back
            <span class="sig-key sig-key-unread"></span>Couldn't check
            <span>Points add up to the score.</span>
          </p>
        </section>

        <section class="block">
          <h3>Protections it cleared</h3>
          <details class="gates-fold">
            <summary>
              <span class="fold-caret" aria-hidden="true">▸</span>
              <span class="fold-shut-label">Show all 7</span>
              <span class="fold-open-label">Hide</span>
            </summary>
          </details>
        </section>

        <div class="why-actions">
          <div class="why-actions-row">{override_controls("spare")}</div>
        </div>
      </div>
    """


def nav() -> str:
    labels = [
        ("review", "Review"),
        ("policy", "Policy"),
        ("reap", "Reap"),
        ("fairness", "Scales"),
        ("settings", "Settings"),
    ]
    buttons = "".join(
        f'<button class="view-tab{" active" if vid == "review" else ""}" data-label="{label}"'
        f"{' aria-current=page' if vid == 'review' else ''}>"
        f'<span class="view-mark">{NAV_ICONS[vid]}<span class="view-label">{label}</span></span>'
        "</button>"
        for vid, label in labels
    )
    return f'<nav class="views" aria-label="Sections">{buttons}</nav>'


# Mockup-only, and deliberately tiny: nothing here restyles the app. It freezes the one state a
# live pointer would be holding (the hovered card shows its Spare/Reap buttons, rule 46) and
# trims the page margin the capture would otherwise frame.
MOCK_CSS = """
body { margin: 0; }
.card.mock-hover > .card-side .override-controls { opacity: 1; visibility: visible; }
.card.mock-hover > .card-side .override-mark { opacity: 0; }
"""


def page() -> str:
    cards = "".join(movie_card(i) if i["kind"] == "movie" else show_card(i) for i in ITEMS)
    return f"""<!doctype html>
<html lang="en" data-theme="dark">
<head>
<meta charset="utf-8">
<title>Reaper, review queue</title>
<style>
{stylesheet()}
</style>
<style>
{MOCK_CSS}
</style>
</head>
<body>
<div class="app">
  <header class="masthead">
    <div class="brand">
      {brand_mark()}
      <div class="brand-text">
        <h1 class="brand-word">Reaper</h1>
        <span class="muted brand-sub">Grave decisions, clearly explained</span>
      </div>
    </div>
    {nav()}
    <div class="user-menu">
      <button class="user-chip">
        <span class="user-avatar user-avatar-fallback">{ACCOUNT[0].upper()}</span>
        <span class="user-name">{ACCOUNT}</span>
        {CHEVRON}
      </button>
    </div>
  </header>

  <section class="app-status" aria-label="Status">
    <div class="banner banner-safe">
      <span class="banner-dot" aria-hidden="true"></span>
      <span><strong>Read-only.</strong> Reaper can look but can't remove anything.
        <button class="link">Turn deletion on in Policy → Deletion</button> when you're ready.
      </span>
    </div>
    <p class="scan-freshness muted">{SCANNED}</p>
  </section>

  <main class="split">
    <section class="queue">
      <h2>Review queue</h2>
      <nav class="tabs" aria-label="Queue lists">
        <button class="tab active" data-label="Condemned" aria-current="page">Condemned</button>
        <button class="tab" data-label="Sanctuary">Sanctuary</button>
        <button class="tab" data-label="Limbo">Limbo</button>
      </nav>
      <div class="queue-toolbar">
        <div class="search-wrap">
          {SEARCH}
          <input class="search-input" type="search" aria-label="Search titles, shows, years"
                 placeholder="Search titles, shows, years…">
        </div>
        <span class="filter-anchor">
          <button type="button" class="add-filter">{PLUS}Filter</button>
        </span>
        <div class="sort-group">
          <label class="pill"><span class="pill-icon" aria-hidden="true">{SORT}</span>
            <select><option>Score</option></select>
          </label>
          <button class="sort-dir" aria-label="Descending">{SORT_DIR}</button>
        </div>
        <button type="button" class="select-toggle">{CHECK_SQUARE} Select</button>
      </div>
      <p class="queue-total"><strong>{TOTAL_ITEMS}</strong> items,
         <strong>{TOTAL_SIZE}</strong> would be freed</p>
      <div class="card-list">{cards}</div>
    </section>
    {why_panel()}
  </main>
</div>
</body>
</html>
"""


def render() -> None:
    if not Path(CHROME).exists() and not shutil.which("chrome"):
        raise SystemExit(f"no Chrome at {CHROME} -- write the page and shoot it yourself")
    chrome = CHROME if Path(CHROME).exists() else shutil.which("chrome")
    # Its own profile, and stopped by us. Two things this Chrome will not do: exit after
    # writing the shot (it holds the process open, in both headless modes), or share the
    # default user-data-dir with the browser the operator is sitting in front of without
    # blocking on its lock. So the render runs detached, and the file appearing -- at a size
    # that has stopped growing -- is what "done" means.
    SHOT.unlink(missing_ok=True)
    with tempfile.TemporaryDirectory(prefix="reaper-shot-") as profile:
        proc = subprocess.Popen(  # noqa: S603 -- argv is this file's flags and its own paths
            [
                chrome,
                "--headless",
                "--disable-gpu",
                "--no-first-run",
                "--no-default-browser-check",
                f"--user-data-dir={profile}",
                "--hide-scrollbars",
                f"--force-device-scale-factor={SCALE}",
                f"--window-size={WIDTH},{HEIGHT}",
                f"--screenshot={SHOT}",
                "--virtual-time-budget=4000",
                PAGE.as_uri(),
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            size = -1
            for _ in range(600):  # 60s, well past the ~4s this takes
                time.sleep(0.1)
                if SHOT.exists():
                    now = SHOT.stat().st_size
                    if now > 0 and now == size:
                        break
                    size = now
            else:
                raise SystemExit("Chrome never wrote the screenshot")
        finally:
            # SIGTERM is not enough -- this Chrome sits through it, which is the same
            # stubbornness that makes it hold the process open after the write.
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=10)

    print(f"wrote {SHOT.relative_to(ROOT)}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--render", action="store_true", help="also shoot the PNG with Chrome")
    args = parser.parse_args()

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    PAGE.write_text(page())
    print(f"wrote {PAGE.relative_to(ROOT)}")
    if args.render:
        render()
    return 0


if __name__ == "__main__":
    sys.exit(main())
