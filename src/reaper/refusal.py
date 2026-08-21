# SPDX-License-Identifier: AGPL-3.0-or-later
"""The server's half of every refusal: a typed code, raw params, and its English template.

Phases 1 to 7 (#870 to #880) typed the chip, the policy warnings, the rule vocabulary and
the one-off sentences the engine composes -- see ``engine.reason.Reason``. This module is
the same move for the *error* surface: every refusal the API returns (a raised
``HTTPException``, a Pydantic validation failure, a middleware rejection) carries a dotted
``code`` and raw ``params`` beside the formatted English, so the browser can translate it
and an API client, a log line, or a reader with no catalog entry can still read the English.

``MESSAGES`` is the catalog: every code mapped to its ``str.format`` template. Codes are
namespaced after the condition and the area they fire in, never the HTTP status
(``error.policy.retired_gate``, not ``error.422.retired_gate``). Reuse one code across every
call site that means the same condition; a call site whose English differs on purpose (the
Plex-unreachable sentence reads differently in three different routes) earns its own code
rather than being forced to share one.

:class:`Refusal` is what the engine and a service raise directly -- a plain ``ValueError``
subclass, so every existing ``except ValueError`` keeps catching it. ``api.errors.refuse``
is the API layer's twin: it raises the ``HTTPException`` subclass carrying the same code and
params, formatted through this same catalog.
"""

from __future__ import annotations

#: Every code an API response, a stored explanation, or the engine's own ``str()`` can
#: surface, mapped to its ``str.format`` template. Plain language throughout (rule 21):
#: outcome first, no ids, no jargon, no em dashes. A ``{field}`` placeholder carries the
#: raw operator-authored key (``recent_watchers``), never its pretty label -- the browser
#: composes that from ``why.field.<key>``, and an API client that sent the key is the
#: right reader for it as-is.
MESSAGES: dict[str, str] = {
    # -----------------------------------------------------------------------------
    # Policy: the field registry's save-boundary checks (engine/fields.py, all raise
    # sites) and the policy body's own validators (engine/policy.py, all raise sites).
    # 37 sites total; tests/test_refusal_catalog.py pins the count.
    # -----------------------------------------------------------------------------
    "error.policy.unknown_field": 'There is no field named "{field}".',
    "error.policy.field_wrong_lane": (
        '"{field}" cannot be used to {use}. It only works as {allowed}.'
    ),
    "error.policy.field_wrong_operator": (
        '"{field}" cannot be compared with "{op}". It works with {allowed}.'
    ),
    "error.policy.field_expects_bool": '"{field}" expects true or false, got {value}.',
    "error.policy.field_expects_text": '"{field}" expects text, got {value}.',
    "error.policy.field_needs_value": '"{field}" needs a value.',
    "error.policy.field_needs_list_value": '"{field}" needs at least one value to match.',
    "error.policy.field_value_has_comma": (
        '"{field}" can\'t match a value with a comma in it. '
        "Reaper separates values with one, so pick a single one."
    ),
    "error.policy.field_expects_number": '"{field}" expects a whole number, got {value}.',
    "error.policy.value_not_numeric": '"{value}" is not a number.',
    "error.policy.gate_popularity_floor_zero": (
        "Keeping anything watched by 0 people would protect your whole library. "
        "Set it to at least 1, or switch this protection off instead."
    ),
    "error.policy.gate_returned_floor_zero": (
        "A title that came back has to be kept for at least a day. To stop keeping "
        "them at all, switch this protection off with its toggle."
    ),
    "error.policy.gate_dormancy_floor_low": (
        "Give titles at least 5 days before removing them. To remove things faster "
        "than that, switch this protection off with its toggle rather than setting "
        "it this low."
    ),
    "error.policy.signal_floor_not_below_saturation": (
        "floor ({floor}) must be below saturate_at ({saturate_at}), "
        "or the signal is either always off or always at full pressure."
    ),
    "error.policy.custom_rule_floor_not_below_saturation": (
        "floor ({floor}) must be below saturate_at ({saturate_at}), "
        "or the rule is either always off or always at full pressure."
    ),
    "error.policy.field_wrong_lane_condemn": '"{field}" cannot be used to remove things.',
    "error.policy.custom_rule_not_numeric": (
        '"{field}" is not a number, so it cannot be graded. Use a yes/no rule.'
    ),
    "error.policy.keep_rule_missing_list": "Say which list this keep rule is about.",
    "error.policy.keep_rule_not_a_list": (
        '"{field}" is not a list, so it cannot be graded. Use a protection instead.'
    ),
    "error.policy.keep_rule_unexpected_list_value": (
        '"{field}" is a number, so this rule ramps; it does not take a list name.'
    ),
    "error.policy.keep_rule_floor_not_below_saturation": (
        "floor ({floor}) must be below saturate_at ({saturate_at})."
    ),
    "error.policy.keep_rule_not_numeric": (
        '"{field}" is not a number, so it cannot be graded. Use a protection instead.'
    ),
    "error.policy.rating_vote_floor_on_percentage": (
        "{source} is a percentage with no vote count, so a vote floor on it would do "
        "nothing. Leave the votes at 0 for this source."
    ),
    "error.policy.rating_vote_floor_zero": (
        "A vote floor of 0 makes the bar on {source} meaningless: it would protect a "
        "high score drawn from a handful of votes. Use at least 1 (1000 is a sensible "
        "default)."
    ),
    "error.policy.weights_over_100": (
        "Your rules add up to {total} points. Take {amount} away before saving. "
        "Removal points always total 100, so each one is worth the same wherever you "
        "spend it."
    ),
    "error.policy.weights_under_100": (
        "Your rules add up to {total} points. Give out the other {amount} before "
        "saving. Removal points always total 100, so each one is worth the same "
        "wherever you spend it."
    ),
    "error.policy.duplicate_gate": "A gate is configured twice; the second would silently win.",
    "error.policy.duplicate_signal": (
        "A signal is configured twice; the second would silently win."
    ),
    "error.policy.duplicate_custom_rule_name": (
        "Two custom rules share a name; the second would silently double-count."
    ),
    "error.policy.custom_rule_name_collides": (
        '"{name}" is the name of a built-in signal. Give your rule a different name '
        "so the score breakdown cannot confuse the two."
    ),
    "error.policy.duplicate_keep_rule_name": (
        "Two keep rules share a name; the second would silently double-count."
    ),
    "error.policy.keep_rule_name_collides_rewatch": (
        '"{name}" is the name of the built-in rewatch keep. Give your rule a '
        "different name so the score breakdown cannot confuse the two."
    ),
    "error.policy.duplicate_rating_source": (
        "The same rating source is listed twice. Set one bar per source, or the "
        "second would silently win."
    ),
    "error.policy.run_cap_exceeds_rolling_cap_items": (
        "A single run may delete {run_cap} items but the 30-day cap is {rolling_cap}. "
        "The rolling cap would be meaningless."
    ),
    "error.policy.run_cap_exceeds_rolling_cap_bytes": (
        "A single run may delete more bytes than the entire 30-day budget."
    ),
    "error.policy.unmeasured_cap_exceeds_run_cap": (
        "A run may delete {unmeasured_cap} items with an unknown size but only "
        "{run_cap} items in total. Lower the first number, or raise the second."
    ),
    # -----------------------------------------------------------------------------
    # Policy: wire-schema validators (api/schemas.py, PydanticCustomError sites) and
    # the policy editor's own routes (api/policy.py).
    # -----------------------------------------------------------------------------
    "error.policy.retired_gate": (
        "That protection is left over from an older version and can't be saved. "
        "Turn it off, then save."
    ),
    "error.policy.probe_range_invalid": (
        "That range doesn't work: the starting point has to come before the far end."
    ),
    "error.policy.probe_unprobable": "Reaper can't try values against that rule.",
    # -----------------------------------------------------------------------------
    # Auth: sign-in, the admin-password gate, and the wire-schema forward-origin check.
    # -----------------------------------------------------------------------------
    "error.auth.not_authenticated": "Not authenticated.",
    "error.auth.recovery_link_invalid": "That recovery link is invalid, expired, or already used.",
    "error.auth.recovery_no_admin": (
        "The recovery code was valid, but this install has no admin account yet. "
        "Sign in with Plex to claim the server, or on Docker and snap run: "
        "reaper-admin create-admin --username admin"
    ),
    "error.auth.login_failed": "{error}",
    "error.auth.too_many_attempts": "Too many attempts. Please wait and try again.",
    "error.auth.password_hashing_busy": (
        "The server is busy checking passwords. Please try again shortly."
    ),
    "error.auth.sign_in_busy": "The server is busy verifying sign-ins. Please try again shortly.",
    "error.auth.restore_password_mismatch": "That password didn't match. Nothing was restored.",
    "error.auth.forget_watch_password_mismatch": (
        "That password didn't match. The record was kept."
    ),
    "error.auth.arm_deletion_password_mismatch": (
        "That password didn't match. Deletion stays off."
    ),
    "error.auth.change_password_mismatch": (
        "The current password didn't match. Nothing was changed."
    ),
    "error.auth.forward_origin_invalid": "That address must start with http:// or https://.",
    "error.auth.forward_origin_has_path": (
        "That address must be just the site, with nothing after it."
    ),
    "error.auth.csrf_blocked": "This request was blocked by Reaper's CSRF protection.",
    "error.auth.api_key_throttled": (
        "Too many bad API keys from this address. Wait a moment and try again."
    ),
    "error.auth.api_key_invalid": "That API key is not valid.",
    "error.auth.api_key_read_denied": (
        "This needs the web app, signed in. An API key reads everything except {exclusions}."
    ),
    "error.auth.api_key_write_denied": (
        "This needs the web app, signed in. An API key writes only these: {permissions}."
    ),
    # -----------------------------------------------------------------------------
    # Plex: linking, connection, libraries, watch evidence.
    # -----------------------------------------------------------------------------
    "error.plex.not_linked": "No Plex server is linked yet. Link one first.",
    "error.plex.verify_tls_no_server": (
        "No Plex server is linked yet. Link one before changing this."
    ),
    "error.plex.web_url_invalid": (
        "The Plex web address must be a full web address, like https://192.0.2.10:32400."
    ),
    "error.plex.connection_address_invalid": (
        "The server address must be a full web address, like https://192.0.2.10:32400."
    ),
    "error.plex.link_rejected": "{error}",
    "error.plex.switch_unreachable": "{error}",
    "error.plex.connection_probe_failed": (
        "Couldn't reach a Plex server at that address, so nothing was changed. "
        "Check the address and port, and whether the certificate check should be off."
    ),
    "error.plex.connection_wrong_server": (
        "That address is a different Plex server, so nothing was changed. Reaper is "
        "linked to {expected_name}; use an address for that server, or link the other "
        "one instead."
    ),
    "error.plex.libraries_sync_failed": "Could not reach Plex: {error}",
    "error.plex.no_password_set_for_watch_reset": (
        "Set an admin password first. It's what confirms forgetting the record."
    ),
    "error.plex.unreachable": "Reaper couldn't reach Plex: {error}",
    "error.plex_trash.unreadable": "Reaper couldn't read Plex's trash: {error}",
    # -----------------------------------------------------------------------------
    # Leaving Soon.
    # -----------------------------------------------------------------------------
    "error.leaving_soon.disabled": (
        "Leaving Soon is off. Turn it on in Settings, under Plex, and Reaper will "
        "keep the shelf up to date."
    ),
    "error.leaving_soon.degraded": (
        "The last scan couldn't be trusted, so the shelf wasn't updated. Run a scan "
        "that finishes cleanly first."
    ),
    "error.leaving_soon.unlinked": (
        "Leaving Soon needs a linked Plex server. Link one in Settings first."
    ),
    # The four phrases services.leaving_soon._record_skip used to write as free text into
    # leaving_soon_last_skip.result. Their own codes because a stored skip is read back long
    # after the pass that wrote it, by a reader that never saw the raising exception.
    "error.leaving_soon.skip_degraded": "the scan didn't finish cleanly",
    "error.leaving_soon.skip_unlinked": "no Plex server is linked",
    "error.leaving_soon.skip_unreachable": "Reaper couldn't reach Plex",
    "error.leaving_soon.skip_failed": "the update didn't finish",
    # -----------------------------------------------------------------------------
    # Logs, poster artwork, Plex trash (read-only surfaces).
    # -----------------------------------------------------------------------------
    "error.logs.bad_level": "Pick Debug, Info, or Warning.",
    "error.poster.no_tautulli": "No Tautulli configured to fetch artwork from.",
    "error.poster.not_found": "No artwork for this item.",
    # -----------------------------------------------------------------------------
    # Whitelist / overrides.
    # -----------------------------------------------------------------------------
    "error.whitelist.unknown_item": (
        "Reaper has no record of that item. It keeps only the last {keep_scans} scans."
    ),
    # -----------------------------------------------------------------------------
    # Backup and restore.
    # -----------------------------------------------------------------------------
    "error.backup.upload_too_large": "That file is too large to be a Reaper backup.",
    "error.backup.no_file_uploaded": "No file was uploaded.",
    "error.backup.restore_refused": "{error}",
    "error.backup.no_password_set": "Set an admin password first. It's what confirms a restore.",
    "error.backup.restore_not_waiting": "There's no restore waiting, so nothing was stopped.",
    "error.backup.reap_in_progress": (
        "A reap is running. Let it finish or stop it, then restart Reaper."
    ),
    # -----------------------------------------------------------------------------
    # Protection lists.
    # -----------------------------------------------------------------------------
    "error.lists.config_rejected": "{error}",
    "error.lists.sync_sources_failed": "{error}",
    "error.lists.registry_unreadable": (
        "One of your lists is saved in a form Reaper can't read, so it didn't check "
        "any of them. Open that list, set it up again, and save it."
    ),
    # -----------------------------------------------------------------------------
    # Scales (fairness).
    # -----------------------------------------------------------------------------
    "error.fairness.needs_seerr_and_tautulli": (
        "Scales needs a Seerr and a Tautulli instance: Seerr for who requested what, "
        "Tautulli for who watched it. Configure them in Settings."
    ),
    "error.fairness.build_failed": "Could not build Scales: {error}",
    "error.fairness.person_not_found": "No one by that id is in the last scan.",
    # -----------------------------------------------------------------------------
    # Simulation.
    # -----------------------------------------------------------------------------
    "error.simulate.no_scan": "No scan has run yet, so there is nothing to simulate.",
    # -----------------------------------------------------------------------------
    # Runs (plan / dry-run / execute) and the reap profile.
    # -----------------------------------------------------------------------------
    "error.runs.limits_unreadable": (
        "Reaper couldn't read the limits you saved, so it won't reap. Open Policy, "
        "go to Pace and limits, and save your limits again."
    ),
    "error.runs.no_scan_to_plan": "No scan has run yet, so there is nothing to plan.",
    "error.runs.plan_refused": "{error}",
    "error.runs.not_found": "No such run.",
    "error.runs.dry_run_refused": "{error}",
    "error.runs.already_running": (
        "A reap is already running. Wait for it to finish, or stop it, first."
    ),
    "error.runs.deletion_disabled": "{reason}",
    "error.runs.confirmation_mismatch": (
        "That confirmation does not match this plan. Expected: {expected}. The plan "
        "may have changed since the page loaded. Reload, review, and confirm again."
    ),
    "error.runs.preflight_no_plex": (
        "Refusing a real run without Plex: the active-stream veto (re-polled before "
        "every delete) cannot run, and deleting blind to who is watching is exactly "
        "what must never happen."
    ),
    "error.runs.preflight_no_tautulli": (
        "Refusing a real run without Tautulli: the played-since-approval check cannot "
        "run, and the grace period exists precisely so a late view can still spare an item."
    ),
    "error.runs.not_running": "That run is not currently running.",
    # -----------------------------------------------------------------------------
    # Review.
    # -----------------------------------------------------------------------------
    "error.review.no_scan": "No scan has run yet.",
    "error.review.candidate_not_found": "No such candidate.",
    "error.review.show_not_in_scan": "That show is not in the latest scan.",
    # -----------------------------------------------------------------------------
    # Settings: instances, schedule, safety, general.
    # -----------------------------------------------------------------------------
    "error.settings.unknown_service_kind": (
        '"{value}" is not a service Reaper knows. Use sonarr, radarr, tautulli or seerr.'
    ),
    "error.settings.instance_rejected": "{error}",
    "error.settings.folder_list_unreachable": "Could not read the folder list: {error}",
    "error.settings.service_list_unreachable": "Could not read the service list: {error}",
    "error.settings.external_url_invalid": (
        "The external URL must be a full web address, like https://192.0.2.10:8989."
    ),
    "error.settings.instance_base_url_invalid": (
        "The service address must be a full web address, like https://192.0.2.10:8989."
    ),
    "error.settings.application_url_invalid": (
        "The application URL must be a full web address, like https://reaper.example.com"
    ),
    "error.settings.discord_webhook_invalid": (
        "That is not a Discord webhook URL. Paste the full "
        "https://discord.com/api/webhooks/… URL from the channel's integration settings."
    ),
    "error.settings.bad_cron": (
        "That is not a valid schedule: {reason}. Use cron form, e.g. '30 4 * * *'."
    ),
    "error.settings.unknown_schedulable_job": 'No schedulable job named "{job_id}".',
    "error.settings.unknown_runnable_job": 'No runnable job named "{job_id}".',
    "error.settings.recovery_mode_blocks_arming": (
        "Recovery mode is on, so deletion stays off. Turn it off and restart first."
    ),
    "error.settings.no_password_set_for_arming": (
        "Set an admin password first. It's what confirms turning deletion on."
    ),
    "error.settings.password_change_rejected": "{error}",
    "error.settings.timezone_unknown": "That is not a known time zone. Pick one from the list.",
    "error.settings.accent_color_invalid": "The accent color must be a hex code like #25c3ff.",
    "error.settings.trusted_proxy_invalid": (
        '"{entry}" is not an address or a range. Use entries like 172.16.0.1 or 172.16.0.0/12.'
    ),
    "error.settings.desktop_only": "These settings exist only on the Windows and macOS apps.",
    "error.settings.dock_icon_macos_only": "The Dock icon setting exists only on the macOS app.",
    "error.settings.launcher_conf_write_failed": (
        "Reaper couldn't save this to launcher.conf in its data folder."
    ),
    "error.settings.no_api_key": "No API key exists yet. Generate one first.",
}


class Refusal(ValueError):  # noqa: N818 -- "Refusal" is the domain noun the plan names; not an -Error
    """A typed refusal the engine or a service raises: a catalog code plus raw params.

    A ``ValueError`` subclass, so every existing ``except ValueError`` around a save-boundary
    check keeps catching it unchanged. ``str(refusal)`` renders the English template so a log
    line or an old caller that only ever read the message still gets one; ``code`` and
    ``params`` are what a typed reader (the API layer, a translator) uses instead.

    ``status`` is the HTTP status the API answers with when this refusal reaches a route --
    422 by default (a well-formed request whose content was refused), overridden by a
    subclass or a call site that means something else (a 400, a 409).
    """

    def __init__(
        self, code: str, /, *, status: int = 422, **params: str | int | float | bool
    ) -> None:
        self.code = code
        self.params: dict[str, str | int | float | bool] = params
        self.status = status
        super().__init__(str(self))

    def __str__(self) -> str:
        template = MESSAGES.get(self.code, self.code)
        try:
            return template.format(**self.params)
        except (KeyError, IndexError):
            # A code with no catalog entry, or params that do not match its placeholders.
            # Never raise out of __str__: the fallback is the code itself, still readable.
            return template
