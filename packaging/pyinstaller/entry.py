# SPDX-License-Identifier: AGPL-3.0-or-later
"""What the frozen binary executes. Everything real lives in ``reaper.launcher``;
this shim only adds ``freeze_support``, which Windows needs before anything else
whenever a frozen process might spawn another (uvicorn's reload/workers machinery
imports :mod:`multiprocessing` even though the launcher starts neither)."""

import multiprocessing

from reaper.launcher import main

if __name__ == "__main__":
    multiprocessing.freeze_support()
    main()
