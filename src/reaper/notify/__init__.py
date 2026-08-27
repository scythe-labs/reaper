# SPDX-License-Identifier: AGPL-3.0-or-later
"""Outbound notifications. Reaper tells people what it is about to do here. The UI is
what asks for permission. A notification is best-effort: a failed webhook must never
break a scan or a run."""
