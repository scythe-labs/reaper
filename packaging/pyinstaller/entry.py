# SPDX-License-Identifier: AGPL-3.0-or-later
"""What the frozen binary executes. Everything real lives in ``reaper.launcher``;
this shim only adds ``freeze_support``, which Windows needs before anything else
whenever a frozen process might spawn another (uvicorn's reload/workers machinery
imports :mod:`multiprocessing` even though the launcher starts neither), and stream
stand-ins for the windowed Windows build, where a double-click hands the process no
console and PyInstaller leaves ``sys.stdout``/``stderr`` as ``None`` -- uvicorn's
logging config and the launcher's own writes need file objects, not crashes. When a
parent redirects the handles (CI's boot probe), they are real streams and stay."""

import multiprocessing
import os
import sys
from pathlib import Path

if sys.stdout is None:
    sys.stdout = Path(os.devnull).open("w")  # noqa: SIM115 -- lives as long as the process
if sys.stderr is None:
    sys.stderr = Path(os.devnull).open("w")  # noqa: SIM115 -- lives as long as the process

from reaper.launcher import main

if __name__ == "__main__":
    multiprocessing.freeze_support()
    main()
