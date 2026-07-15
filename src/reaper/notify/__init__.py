# SPDX-License-Identifier: AGPL-3.0-or-later
"""Outbound notifications. Reaper tells people what it is about to do; it never asks
them for permission here -- that is the UI's job. Notifications are best-effort: a failed
webhook must never break a scan or a run."""
