# SPDX-License-Identifier: AGPL-3.0-or-later
"""Backend translation catalogs (`reaper.i18n`), one directory per BCP 47 tag.

Data only: each `<tag>/backend.json` is loaded through `importlib.resources`, anchored on
this package, so it resolves the same way from a source checkout, a wheel install, and the
Docker image's editable install. `en` is hand-written and reviewed here; every other tag is
Weblate's (see CONTRIBUTING's "Translate it").
"""
