# -*- mode: python ; coding: utf-8 -*-
# SPDX-License-Identifier: AGPL-3.0-or-later
# The one PyInstaller spec, shared by the Windows and macOS builds (binaries.yml).
# One-folder on purpose: one-file unpacks itself to a temp dir on every start (slow,
# and the self-extracting stub is what antivirus heuristics flag most), while a plain
# folder is transparent to a scanner and to the operator alike.
#
# Before building, generate the provenance file this spec bundles:
#   python scripts/write_buildinfo.py --out packaging/pyinstaller/buildinfo.json ...

import json
import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_submodules

ROOT = Path(SPECPATH).resolve().parent.parent  # noqa: F821 -- SPECPATH is a PyInstaller global

#: The one committed brand image; Pillow (in the `package` extra) lets PyInstaller cut
#: the platform icon formats from it, so no generated .ico/.icns is ever committed.
ICON = str(ROOT / "frontend" / "public" / "icon-512.png")

#: The version the .app's Info.plist declares. Dev builds have no dotted version, and
#: Info.plist wants one, so they declare 0.0.0 and the About page stays the truth.
_info = json.loads((Path(SPECPATH) / "buildinfo.json").read_text())  # noqa: F821
_plist_version = v if (v := _info.get("version", "")) and v[:1].isdigit() else "0.0.0"

datas = [
    # The built SPA and the migrations travel inside the bundle; reaper.main and
    # reaper.launcher resolve both under sys._MEIPASS exactly where these land.
    (str(ROOT / "frontend" / "dist"), "frontend/dist"),
    (str(ROOT / "alembic"), "alembic"),
    (str(Path(SPECPATH) / "buildinfo.json"), "."),  # noqa: F821
]

hiddenimports = [
    # uvicorn resolves its loop, http protocol, and lifespan classes from strings at
    # startup, which static analysis cannot see.
    *collect_submodules("uvicorn"),
    # The async SQLite dialect is resolved through SQLAlchemy's registry by URL
    # scheme, not imported by name anywhere in reaper.
    "sqlalchemy.dialects.sqlite.aiosqlite",
    "aiosqlite",
    # APScheduler looks trigger and executor plugins up through entry points.
    *collect_submodules("apscheduler"),
]

a = Analysis(
    [str(Path(SPECPATH) / "entry.py")],  # noqa: F821
    pathex=[str(ROOT / "src")],
    datas=datas,
    hiddenimports=hiddenimports,
    excludes=["tkinter"],
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    exclude_binaries=True,
    name="reaper",
    console=True,
    icon=ICON if sys.platform == "win32" else None,
    # macOS on arm64 refuses to run an unsigned binary at all, so PyInstaller ad-hoc
    # signs when no identity is given. Real signing and notarization are not wired
    # yet; packaging/README.md carries what they would take.
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    name="reaper",
)

# macOS also ships a double-clickable Reaper.app (in the drag-to-Applications DMG):
# a windowed twin of the same build, because a console binary inside a bundle would
# lose Dock quit handling. Its stdout goes nowhere; the in-app log page is the log.
# The launcher opens the browser once the server is healthy, so a double-click has a
# visible result.
if sys.platform == "darwin":
    app_exe = EXE(
        pyz,
        a.scripts,
        exclude_binaries=True,
        name="Reaper",
        console=False,
        codesign_identity=None,
        entitlements_file=None,
    )
    app_coll = COLLECT(
        app_exe,
        a.binaries,
        a.datas,
        name="Reaper-app",
    )
    app = BUNDLE(
        app_coll,
        name="Reaper.app",
        icon=ICON,
        bundle_identifier="io.scythe-labs.reaper",
        info_plist={
            "CFBundleDisplayName": "Reaper",
            "CFBundleShortVersionString": _plist_version,
            "CFBundleVersion": _plist_version,
            "NSHighResolutionCapable": True,
            "LSApplicationCategoryType": "public.app-category.utilities",
        },
    )
