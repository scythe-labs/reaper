# SPDX-License-Identifier: AGPL-3.0-or-later
"""The server's half of every refusal: a typed code, raw params, and its English template.

``engine.reason.Reason`` already types the chip, the policy warnings, and the
rule vocabulary. This module does the same for the error surface: every
refusal the API returns (a raised ``HTTPException``, a Pydantic validation
failure, a middleware rejection) carries a dotted ``code`` and raw ``params``
beside the formatted English, so the browser can translate it, and an API
client, a log line, or a reader with no catalog entry can still read the
English.

``MESSAGES`` is the catalog: every code mapped to its ``str.format`` template.
Codes are namespaced after the condition and the area they fire in, never the
HTTP status (``error.policy.retired_gate``, not ``error.422.retired_gate``).
Reuse one code across every call site that means the same condition. A call
site whose English differs on purpose (the Plex-unreachable sentence reads
differently in three different routes) earns its own code instead of being
forced to share one.

:class:`Refusal` is what the engine and a service raise directly: a plain
``ValueError`` subclass, so every existing ``except ValueError`` keeps
catching it. ``api.errors.refuse`` is the API layer's twin. It raises the
``HTTPException`` subclass carrying the same code and params, formatted
through this same catalog.
"""

from __future__ import annotations

from typing import Any

from reaper.engine.reason import Reason, ReasonParam

#: Every code an API response, a stored explanation, or the engine's own
#: ``str()`` can surface, mapped to its ``str.format`` template. Plain language
#: throughout: outcome first, no ids, no jargon, no em dashes. A ``{field}``
#: placeholder carries the raw operator-authored key (``recent_watchers``),
#: never its pretty label. The browser composes the pretty label from
#: ``why.field.<key>``, and an API client that sent the key is the right
#: reader for the raw form.
MESSAGES: dict[str, str] = {
    # -----------------------------------------------------------------------------
    # Policy: the field registry's save-boundary checks (engine/fields.py, all raise
    # sites) and the policy body's own validators (engine/policy.py, all raise sites).
    # tests/test_repo_hygiene.py::test_every_refusal_code_has_a_raiser_and_a_catalog_entry
    # pins the catalog's whole code and site counts, not this section alone.
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
    # Auth: signing in (services.login.LoginError, all raise sites, 7 codes). A
    # wrapped PlexLinkError forwards that error's own plex.* code rather than a
    # new one, so it is not a new entry here.
    # -----------------------------------------------------------------------------
    "error.auth.login_request_invalid": "This sign-in is no longer valid. Please start again.",
    "error.auth.login_request_timed_out": "This sign-in timed out. Please start again.",
    "error.auth.login_check_failed": "Could not reach Plex to check the sign-in.",
    "error.auth.login_account_unreadable": ("Signed in to Plex, but could not read the account."),
    "error.auth.plex_not_owner": (
        "That Plex account does not own this server, so it cannot administer Reaper. "
        "Sign in as the server owner, or use a local account."
    ),
    "error.auth.account_deactivated": "This account has been deactivated.",
    "error.auth.wrong_credentials": "Wrong username or password.",
    # -----------------------------------------------------------------------------
    # The admin password (services.admin_password.PasswordError).
    # -----------------------------------------------------------------------------
    "error.password.too_short": "Use at least {min_length} characters.",
    # -----------------------------------------------------------------------------
    # Deletion-safety state, shared by the executor's own check and the
    # execute route's earlier one (services.executor, api.runs): one fact,
    # one pair of codes.
    # -----------------------------------------------------------------------------
    "error.safety.recovery_mode_active": (
        "Recovery mode is on, so Reaper can look but can't remove anything. "
        "Turn it off and restart."
    ),
    "error.safety.deletion_off": (
        "Deletion is turned off, so Reaper can look but can't remove anything. "
        "Turn it on in Policy → Deletion when you're ready."
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
    # services.plex_link.PlexLinkError and PlexLinkRetryableError, all raise
    # sites, 10 codes. An 11th site reuses error.plex.not_linked above:
    # switch_server links no new server, it repoints an existing link, so the
    # same "no server" sentence fits.
    "error.plex.link_unreachable": (
        'Found your server ("{name}") but could not reach it on any of its {count} '
        "advertised addresses. Reaper has to talk to the server directly; check that "
        "it is running and reachable from this host."
    ),
    "error.plex.link_timed_out": "Sign-in was not completed in time. Nothing was saved.",
    "error.plex.link_ambiguous_name": (
        'This account owns more than one server named "{choice}". Pick by machine '
        "identifier instead: {ids}."
    ),
    "error.plex.link_choice_not_found": (
        'No server this account owns matches "{choice}". It owns: {names}. Start the '
        "sign-in again and pick one of those."
    ),
    "error.plex.link_not_owner": (
        'Signed in as "{username}", but that account does not own a Plex server. '
        "Reaper must be linked by the server owner: it is going to be given permission "
        "to delete media."
    ),
    "error.plex.switch_owned_servers_failed": (
        "Could not ask plex.tv which servers this account owns: {error}"
    ),
    "error.plex.link_request_invalid": (
        "This link request is no longer valid. Please start again."
    ),
    "error.plex.link_request_timed_out": "This link request timed out. Please start again.",
    "error.plex.link_check_failed": "Could not reach Plex to check the link.",
    "error.plex.link_account_unreadable": ("Signed in to Plex, but could not read the account."),
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
    # These four phrases get their own codes because a stored skip is read
    # back long after the pass that wrote it, by a reader that never saw the
    # raising exception. `services.leaving_soon._record_skip` is where they
    # are written.
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
    "error.backup.no_password_set": "Set an admin password first. It's what confirms a restore.",
    "error.backup.restore_not_waiting": "There's no restore waiting, so nothing was stopped.",
    "error.backup.reap_in_progress": (
        "A reap is running. Let it finish or stop it, then restart Reaper."
    ),
    # services.restore.RestoreError, all raise sites, 15 codes across 19
    # sites. Two messages, "malformed" and "prepare_failed", each cover more
    # than one site, since every one of those sites means the same thing to
    # the operator.
    "error.restore.schema_unverifiable": (
        "Reaper couldn't check this backup against its own version. Try again, or update Reaper."
    ),
    "error.restore.newer_than_build": (
        "This backup was made by a newer version of Reaper than the one running. "
        "Update Reaper to that version or later, then restore."
    ),
    "error.restore.archive_too_large": "This backup is larger than Reaper can restore.",
    "error.restore.malformed": "This backup file is malformed.",
    "error.restore.unreadable_archive": "This isn't a readable Reaper backup file.",
    "error.restore.missing_contents": (
        "This isn't a Reaper backup: some of its contents are missing."
    ),
    "error.restore.manifest_unreadable": "This backup's description couldn't be read.",
    "error.restore.not_a_backup": "This isn't a Reaper backup file.",
    "error.restore.database_unreadable": "The database inside this backup isn't readable.",
    "error.restore.database_unverifiable": (
        "The database in this backup couldn't be verified. It may be damaged, or not "
        "a Reaper backup."
    ),
    "error.restore.manifest_mismatch": (
        "This backup's description doesn't match the database inside it. It may be "
        "damaged or altered."
    ),
    "error.restore.missing_key": (
        "This backup is missing its encryption key and can't be restored."
    ),
    "error.restore.prepare_failed": "Reaper couldn't prepare this backup. Nothing was restored.",
    "error.restore.nothing_staged": "There's no backup ready to restore. Choose a file first.",
    "error.restore.staged_changed": (
        "The staged backup changed since you reviewed it. Check it again before restoring."
    ),
    # -----------------------------------------------------------------------------
    # Protection lists.
    # -----------------------------------------------------------------------------
    "error.lists.registry_unreadable": (
        "One of your lists is saved in a form Reaper can't read, so it didn't check "
        "any of them. Open that list, set it up again, and save it."
    ),
    # services.list_config.ListConfigError, all raise sites, 11 codes across
    # 13 sites ("name_exists" covers three).
    "error.lists.not_found": "That list no longer exists. Reload the page.",
    "error.lists.name_required": (
        "Give the list a name, so you can pick it out on the Policy screen."
    ),
    "error.lists.name_too_long": "That name is too long. Keep it under 100 characters.",
    "error.lists.name_has_comma": (
        "A list name can't have a comma in it. Reaper separates names with one."
    ),
    "error.lists.name_exists": "You already have a list with that name. Pick another.",
    "error.lists.library_required": "Say which Plex library to look in.",
    "error.lists.collection_required": "Say which collection in that library to read.",
    "error.lists.tags_required": (
        "Add at least one tag, spelled as it appears in Sonarr or Radarr."
    ),
    "error.lists.imdb_choice_required": (
        "Pick one of the IMDb presets, or paste a list id instead."
    ),
    "error.lists.imdb_id_invalid": (
        "Paste the list's id or URL. An IMDb list id looks like ls000000000."
    ),
    "error.lists.source_required": "Pick where the list comes from.",
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
    "error.runs.not_found": "No such run.",
    "error.runs.already_running": (
        "A reap is already running. Wait for it to finish, or stop it, first."
    ),
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
    "error.settings.language_invalid": (
        '"{tag}" is not a language code. Pick a language from the list.'
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
    # -----------------------------------------------------------------------------
    # Services (services.instances.InstanceError and its two status-typed
    # subclasses), all raise sites, 6 codes across 7 sites ("name_exists"
    # covers two).
    # -----------------------------------------------------------------------------
    "error.instances.not_found": "No such instance.",
    "error.instances.required_fields": "A name, a URL and an API key are all required.",
    "error.instances.singleton_exists": (
        "Reaper uses one {kind}. It reads a single Plex server's watch history, and "
        "Reaper connects to one Plex. Edit the one you have, or remove it and add a "
        "different one."
    ),
    "error.instances.name_exists": 'A {kind} connection named "{name}" already exists.',
    "error.instances.wrong_kind_for_root_folders": (
        "Only Sonarr and Radarr have root folders to map to a Plex library."
    ),
    "error.instances.wrong_kind_for_seerr_services": (
        "Only Seerr portals have request services to map to an instance."
    ),
    # -----------------------------------------------------------------------------
    # A connection test's own verdict (services.instances.explain_failure),
    # all return sites, 16 codes. Branch order there is load-bearing (see the
    # function's docstring). This catalog just holds the words. Reused as a
    # nested `{error}` param inside `mapError` and inside
    # `TestOut.detail_reason` (api.settings), never composed as a top-level
    # refusal on its own.
    # -----------------------------------------------------------------------------
    "error.instance.cert_unknown_authority": (
        "The server's certificate is signed by an authority this machine doesn't know. "
        "Only turn off the certificate check if this is your own server on your own "
        "network: your API key travels on this connection."
    ),
    "error.instance.cert_rejected": (
        "The server's certificate was rejected. It may have expired, or be for a "
        "different address, or something may be sitting between Reaper and the server."
    ),
    "error.instance.auth_refused": (
        "{service} refused the API key. Copy it again from its own settings."
    ),
    "error.instance.address_empty": (
        "{service} answered, but there is nothing at this address. Check for a missing "
        "or extra path at the end of the URL."
    ),
    "error.instance.rate_limited": (
        "{service} asked Reaper to slow down. Wait a moment and test again."
    ),
    "error.instance.redirected": (
        "The server sent Reaper somewhere else, and Reaper won't send your API key to a "
        "different address. Check the URL and anything proxying it."
    ),
    "error.instance.server_error": (
        "{service} reported a problem of its own (HTTP {status}). Check its log."
    ),
    "error.instance.request_refused": "{service} refused the request (HTTP {status}).",
    "error.instance.connect_timed_out": "Couldn't open a connection to the server in time.",
    "error.instance.read_timed_out": "The server didn't answer in time.",
    "error.instance.bad_address": "That isn't an address Reaper can use. Start it with http:// or https://.",
    "error.instance.unreachable": (
        "Couldn't reach the server at this address. Check the URL and port, and that the "
        "service is running."
    ),
    "error.instance.connection_broke": "The connection to the server broke before it answered.",
    "error.instance.bad_body": (
        "The address answered, but not with data from {service}. Check the URL."
    ),
    "error.instance.refused": (
        "{service} answered, but turned the request down. Check the API key first, then the URL."
    ),
    "error.instance.unrecognized": "Couldn't connect. The full reason is in Reaper's log.",
    # -----------------------------------------------------------------------------
    # Every integration client's own failures (clients/base.py's
    # IntegrationError, and every other client that raises one: seerr.py,
    # tautulli.py, public.py, plextv.py, services/lists.py,
    # services/update_check.py, services/history_sync.py). Reused across
    # every service that speaks HTTP, so these stay generic rather than
    # naming one service. A caller that wants a service-specific sentence
    # nests this under one of its own (`explain_failure`'s
    # `error.instance.*`, above, is the model). `SafetyViolationError` (the
    # transport guard's own refusal, same file) shares this namespace too: it
    # is the same layer's refusal, just for a write rather than a read.
    # -----------------------------------------------------------------------------
    "error.integration.timed_out": "Timed out waiting for an answer.",
    "error.integration.unreachable": "Couldn't reach it: {detail}",
    "error.integration.refused_redirect": (
        "The service redirected instead of answering (HTTP {status})."
    ),
    "error.integration.too_many_redirects": "The service kept redirecting and never answered.",
    "error.integration.cross_origin_redirect_refused": (
        "The service tried to redirect somewhere else. Reaper won't send your credentials there."
    ),
    "error.integration.http_failure": "The service answered with an error (HTTP {status}).",
    "error.integration.unexpected_shape": "{path} sent back something Reaper couldn't read.",
    "error.integration.seerr_list_incomplete": "Seerr's list stopped partway through.",
    "error.integration.seerr_list_unbounded": "Seerr's list never finished loading.",
    "error.integration.redirect_missing_location": (
        "The download redirected without saying where to."
    ),
    "error.integration.tautulli_command_failed": "Tautulli turned the request down: {detail}",
    "error.integration.imdb_list_truncated": (
        "That list came back too short to trust, so Reaper left the stored one in place."
    ),
    "error.integration.keep_tag_missing": (
        "The keep tag {names} doesn't exist there anymore, so anything that used to carry "
        "it is no longer protected. The keep list was left as it was, so add the tag back "
        "or take it out of your keep tags."
    ),
    "error.integration.library_not_found": 'There\'s no library called "{name}" anymore.',
    "error.integration.update_check_incomplete": (
        "GitHub's answer didn't include what Reaper needed to check for updates."
    ),
    "error.integration.history_rows_unservable": (
        "{count} watch-history rows couldn't be read, so the sweep stopped."
    ),
    "error.integration.write_not_armed": "Blocked {method} {path}. {why}",
    "error.integration.write_not_declared": (
        "Blocked {method} {path}: this mutation wasn't declared to the action journal. "
        "Destructive calls must go through the action executor so that they are recorded "
        "before they are sent."
    ),
    "error.integration.write_shelf_blocked": (
        "Blocked {method} to Plex (Leaving Soon shelf). Turn deletion on, or turn on "
        '"Update while read-only" under Settings, Plex, Leaving Soon to allow this '
        "reversible shelf write while Reaper is read-only."
    ),
    "error.integration.tautulli_write_refused": (
        "Tautulli command {cmd} isn't on Reaper's read-only allow-list. Reaper never writes "
        "to Tautulli."
    ),
    # -----------------------------------------------------------------------------
    # The Plex client's own failures (clients/plex.py's PlexError). Plex is
    # always "the server" in these, since there is only ever one, so no
    # {service} param.
    # -----------------------------------------------------------------------------
    "error.plexclient.paging_failed": "Reaper couldn't read all of {what} from Plex.",
    "error.plexclient.connect_failed": "Could not reach Plex at {base_url}: {detail}",
    "error.plexclient.call_failed": "Could not {what}: {detail}",
    "error.plexclient.streams_unreadable": (
        "Could not read active sessions from Plex ({detail}). Refusing to delete: not "
        "being able to see who is watching is not the same as nobody watching."
    ),
    "error.plexclient.trash_count_failed": "Couldn't count the trash in section {section}.",
    # -----------------------------------------------------------------------------
    # Planning a run (services.planner.PlanError), all raise sites, 15 codes.
    # -----------------------------------------------------------------------------
    "error.plan.media_key_unroutable": 'Cannot route media_key "{media_key}" to an instance.',
    "error.plan.media_key_malformed": 'Malformed media_key "{media_key}": {error}',
    "error.plan.season_media_key_not_sonarr": (
        'A season media_key must be sonarr, got "{media_key}".'
    ),
    "error.plan.unmeasured_sort_key": "{media_key} has no measured size to order by.",
    "error.plan.no_canary": (
        "Reaper couldn't measure any of these items, so it has nothing safe to test "
        "the run on. The first thing a run deletes has to be something whose size it "
        "knows. Check these in Sonarr or Radarr, then run a new scan."
    ),
    "error.plan.no_snapshot": "No snapshot {snapshot_id}.",
    "error.plan.snapshot_degraded": (
        "That scan came back incomplete, so Reaper won't act on it. Fix the source and "
        "scan again. {reason}"
    ),
    "error.plan.selection_empty": "No items were selected to reap.",
    "error.plan.unmeasured_seasons": (
        "Reaper couldn't measure any of the seasons it would remove from {keys}, so "
        "there is nothing here it can reap. Check them in Sonarr, then run a new scan."
    ),
    "error.plan.items_not_condemned": (
        "These items are not condemned in this snapshot, so they cannot be reaped: {keys}."
    ),
    "error.plan.items_unmeasured": (
        "Reaper couldn't measure the size of these items, so it won't reap them: "
        "{keys}. Check them in Sonarr or Radarr, then run a new scan."
    ),
    "error.plan.items_spared": (
        "These items are spared, so they will not be reaped: {keys}. Remove the spare "
        "first if you really mean to delete them."
    ),
    "error.plan.unmeasured_over_limit": (
        "This plan holds {count} items Reaper couldn't measure, over your limit of "
        "{limit} per run. The plan is refused rather than trimmed: which of them gets "
        "deleted must not come down to the order they were listed in. Raise the "
        "limit, or reap fewer items at once."
    ),
    "error.plan.nothing_condemned": (
        "Nothing is condemned in this snapshot; there is no plan to build."
    ),
    "error.plan.instance_orphaned": (
        "Some of these items were found by a Radarr that is no longer connected, so "
        "Reaper cannot remove them. Reconnect it, or run a new scan to drop them from "
        "the list."
    ),
    # -----------------------------------------------------------------------------
    # Executing a run (services.executor.ExecutionError), all raise sites, 21
    # codes across 27 sites. "journal_halt" covers two. Two more reuse
    # error.runs.preflight_no_plex and error.runs.preflight_no_tautulli
    # below, and two reuse error.safety.* above, since the executor's own
    # backstop checks the same conditions the route already named.
    # -----------------------------------------------------------------------------
    "error.reap.radarr_instance_missing": (
        "No Radarr instance {instance_id} is configured, but the plan targets it. "
        "Refusing to guess which server to delete from."
    ),
    "error.reap.sonarr_instance_missing": (
        "No Sonarr instance {instance_id} is configured, but the plan targets it. "
        "Refusing to guess which server to delete from."
    ),
    "error.reap.no_run": "No run {run_id}.",
    "error.reap.unmeasured_for_caps": (
        "Reaper couldn't measure the size of these items, so it can't check the run "
        "against your limits: {keys}. The run is aborted."
    ),
    "error.reap.unmeasured_over_limit": (
        "This run would delete {unmeasured} items Reaper couldn't measure, over your "
        "limit of {limit} per run. It stops rather than deleting just part. Raise the "
        "unknown-size allowance in Policy, under Pace and limits."
    ),
    "error.reap.items_over_run_cap": (
        "This plan would remove {items} titles, over your per-run cap of {cap}. It "
        "stops rather than deleting just part, because which titles go must never "
        "come down to sort order. Raise the cap or turn limits off in Policy, under "
        "Pace and limits."
    ),
    "error.reap.bytes_over_run_cap": (
        "This plan would remove {gb} GB, over your per-run cap of {cap_gb} GB. It "
        "stops rather than deleting just part. Raise the cap or turn limits off in "
        "Policy, under Pace and limits."
    ),
    "error.reap.not_runnable": "Run {run_id} is {state}, not runnable. A run executes once.",
    "error.reap.no_clients_configured": (
        "Refusing a real run with no clients configured: there is nothing to issue "
        "the delete through, and no way to check who is watching."
    ),
    "error.reap.no_arm_check": (
        "Refusing a real run without a live arm check: turning deletion off could not "
        "stop a run already in progress."
    ),
    "error.reap.manifest_changed": (
        "The condemned set changed since this plan was approved: an item was added, "
        "removed, or resized. The approval was for a different plan and is void. "
        "Re-scan, re-review, and approve the new plan."
    ),
    "error.reap.policy_changed": (
        "Your policy changed after this plan was approved, so the plan is out of "
        "date and nothing was deleted. Run a new scan, then review and plan again."
    ),
    "error.reap.lists_changed": (
        "Your protection lists changed after this plan was approved, so the plan is "
        "out of date and nothing was deleted. Run a new scan, then review and plan again."
    ),
    "error.reap.already_claimed": (
        "Run {run_id} was already claimed by another request. A run executes once."
    ),
    "error.reap.items_over_rolling_cap": (
        "This run would delete {items} items on top of the {past_items} already "
        "deleted in the last 30 days, over the rolling cap of {cap}. It stops rather "
        "than deleting just part. Wait for the window to pass, raise the cap, or "
        "turn limits off in Policy, under Pace and limits."
    ),
    "error.reap.bytes_over_rolling_cap": (
        "This run would delete {gb} GB on top of the {past_gb} GB already deleted in "
        "the last 30 days, over the rolling cap of {cap_gb} GB. It stops rather than "
        "deleting just part. Wait for the window to pass, raise the cap, or turn "
        "limits off in Policy, under Pace and limits."
    ),
    "error.reap.deletion_disabled_mid_run": (
        "Deletion was turned off while this run was in progress, so the run stopped "
        "here. Anything already deleted stays deleted; nothing further was sent."
    ),
    "error.reap.stopped_by_operator": (
        "You stopped this run before it finished, so the rest of the plan was left alone."
    ),
    "error.reap.journal_halt": (
        "Reaper could not save its record of what it just did, so it stopped before "
        "touching anything else. Anything already removed stays removed. Check the "
        "free space and permissions on Reaper's data folder."
    ),
    "error.reap.canary_failed": (
        'The first item, the test item ("{title}"), did not finish the way Reaper '
        "expected: {detail}. Stopping now, before anything else is touched."
    ),
    "error.reap.item_failed_unexplained": (
        'The run stopped at "{title}". Reaper could not tell what went wrong there, '
        "so it did not touch anything after it. Anything already removed stays "
        "removed. The details are in the run's own list and in the log."
    ),
    "error.reap.overrides_unreadable": (
        "Reaper could not re-check your keep and remove decisions, so the run "
        "stopped here rather than risk deleting something you just kept. Anything "
        "already deleted stays deleted; nothing further was sent."
    ),
    # api.runs.execute_run's own catch-all, not one of ExecutionError's raise
    # sites: a background crash the executor itself never named. Anything
    # already removed stays removed. The executor's own interlocks are what
    # make that true, not this code.
    "error.reap.unexpected": "The reap hit a problem it didn't expect: {error}",
    # -----------------------------------------------------------------------------
    # Scanning (services.scan_runner.ScanConfigError), all raise sites, 3
    # codes across 4 sites ("missing_sources" covers two).
    # -----------------------------------------------------------------------------
    "error.scan.list_gate_missing_list": (
        "A protection you set up is pointing at a list that is no longer there, so "
        "the scan stopped instead of leaving titles unprotected. Add the list back "
        "on Settings, Lists, then open Policy and save. Turning that protection off "
        "instead drops it for good."
    ),
    "error.scan.gate_unimplemented": (
        'Policy enables the "{gate}" protection, but Reaper has no implementation '
        "for it. Refusing to scan rather than silently skipping a protection you "
        "asked for."
    ),
    "error.scan.missing_sources": (
        "A scan needs a Tautulli instance plus at least one Radarr or Sonarr. Add "
        "them in Settings first."
    ),
    # -----------------------------------------------------------------------------
    # Scanning: the background job's own status poll
    # (api.scan.ScanStatus.error_reason). Not raised through ScanConfigError.
    # These three cover the poller's other catch arms, so the browser has a
    # typed reason for every way a background scan can stop.
    # -----------------------------------------------------------------------------
    "error.scan.already_running": (
        "A scan is already running. Wait for it to finish, then start another."
    ),
    "error.scan.source_unreachable": "Reaper couldn't reach one of your sources: {error}",
    "error.scan.unexpected": "The scan hit a problem it didn't expect: {error}",
    # -----------------------------------------------------------------------------
    # The run journal's per-item detail and checklist, and the run-level
    # abort reason: reaper.services.executor, every ``StepOutcome.detail``,
    # ``StepCheck.label``, and ``RunReport.aborted_reason`` site. The prose
    # column (``ActionStep.error``, ``ReapRun.aborted_reason``) and the
    # report's own JSON field keep their English rendered from these same
    # entries through ``english()`` below, so the two can never drift. A
    # ``{live}``/``{approved}`` param is raw bytes. The ``{name}_gb``
    # placeholder is the browser's own derivation (``why.ts``'s
    # ``composeIn``), and ``english()`` mirrors it for the stored prose and
    # the log line.
    # -----------------------------------------------------------------------------
    "error.reap.canceled": "The run was stopped before it finished.",
    "error.reap.step.route_failed": 'Could not route "{media_key}": {error}',
    "error.reap.step.no_delete_path": ('No live delete path for "{media_key}" ({media_type}).'),
    "error.reap.step.arr_call_failed": "The *arr call failed: {error}",
    "error.reap.step.transport_guard_blocked": "The transport guard blocked the delete: {error}",
    "error.reap.step.item_unexpected": "An unexpected error stopped this item: {error}",
    "error.reap.step.spared_by_hand": (
        "You spared this by hand, so it is kept even though it was in the plan."
    ),
    "error.reap.step.not_in_confirmed_run": (
        "This was not part of the run you confirmed, so it is kept."
    ),
    "error.reap.step.hand_reap_removed": "The hand reap on this was removed, so it is kept.",
    "error.reap.step.no_plex_match": (
        "Plex has no rating key for this item, so Reaper cannot confirm nobody is "
        "watching it. Spared."
    ),
    "error.reap.step.being_watched": "Someone is watching it right now.",
    "error.reap.step.played_since_approval": "Played since the plan was approved.",
    "error.reap.step.dry_run": "Would send: {plan}",
    "error.reap.step.no_approved_size": (
        "Reaper never got a size for this when it was scanned, so it cannot confirm this "
        "is what you approved. Kept."
    ),
    "error.reap.step.size_unconfirmed_movie": (
        "Radarr did not report this movie's current size, so Reaper cannot confirm it is "
        "still the file that was approved. Kept."
    ),
    "error.reap.step.grew_since_approved_movie": (
        "The file is bigger now ({live_gb} GB) than when it was approved ({approved_gb} "
        "GB), so it was likely upgraded since the scan. Kept. Run a new scan to review it "
        "at its current size."
    ),
    "error.reap.step.no_tmdb_id": (
        "Radarr lists no TMDB id for this movie, so the import exclusion could never be "
        "verified after deleting. The file was kept. Fix the movie's identification in "
        "Radarr, then plan again."
    ),
    "error.reap.step.movie_not_removed": (
        "Radarr accepted the delete but the movie is still there. Nothing was removed."
    ),
    "error.reap.step.movie_removal_unconfirmed": (
        "Radarr accepted the delete, but Reaper could not reach it again to confirm the "
        "file is gone. It is counted against your limits as removed."
    ),
    "error.reap.step.exclusion_unconfirmed": (
        "The file was removed, but Reaper could not confirm the import exclusion, so a "
        "re-request could download it again."
    ),
    "error.reap.step.movie_deleted_verified": "Deleted. Import exclusion verified present.",
    "error.reap.step.movie_deleted_no_exclusion": "Deleted. Import exclusion off for this Radarr.",
    "error.reap.step.season_no_files": (
        "Sonarr lists no files for season {season}, so there is nothing to delete. Kept."
    ),
    "error.reap.step.size_unconfirmed_season": (
        "Sonarr did not report a size for every file in this season, so Reaper cannot "
        "confirm it is still what was approved. Kept."
    ),
    "error.reap.step.grew_since_approved_season": (
        "This season is bigger now ({live_gb} GB) than when it was approved ({approved_gb} "
        "GB), so its files likely changed since the scan. Kept. Run a new scan to review "
        "it at its current size."
    ),
    "error.reap.step.unmonitor_verify_failed": (
        "The season is still monitored after the unmonitor. Not deleting files."
    ),
    "error.reap.step.delete_skipped_unmonitor_unverified": "Unmonitor unverified.",
    "error.reap.step.unmonitor_not_verified": (
        "Unmonitor did not take. Refused to delete files while still monitored."
    ),
    "error.reap.step.season_files_arrived": (
        "{count} more file(s) landed in season {season} while Reaper was working, so it "
        "is no longer what was approved. Nothing was deleted, and the season was left "
        "unmonitored. Run a new scan to review it as it is now."
    ),
    "error.reap.step.season_files_vanished": (
        "Sonarr no longer lists any file for season {season}, so nothing was deleted. The "
        "season was left unmonitored."
    ),
    "error.reap.step.season_removal_unconfirmed": (
        "Sonarr accepted the delete for season {season}, but Reaper could not reach it "
        "again to confirm the files are gone. They are counted against your limits as "
        "removed."
    ),
    "error.reap.step.season_files_remain": (
        "{count} episode file(s) for season {season} remain after the delete. Not confirmed."
    ),
    "error.reap.step.season_pruned": (
        "Season {season} pruned: {count} file(s) deleted, unmonitor verified."
    ),
    "error.reap.check.not_watching": "Nobody was watching it right now",
    "error.reap.check.not_played_since": "Not played since you approved it",
    "error.reap.check.no_approved_size": "No size was recorded for it at scan time. Kept.",
    "error.reap.check.size_unconfirmed": "Couldn't confirm its current size. Kept.",
    "error.reap.check.grew_since_approved": "It grew since you approved it. Kept.",
    "error.reap.check.spared_by_hand": "You spared this by hand. Kept.",
    "error.reap.check.not_in_confirmed_run": "Not part of the run you confirmed. Kept.",
    "error.reap.check.hand_reap_removed": "No longer marked to reap by hand. Kept.",
    "error.reap.check.no_plex_match": "No Plex match, so we can't confirm it's idle. Kept.",
    "error.reap.check.being_watched": "Someone is watching it right now. Kept.",
    "error.reap.check.played_since_approval": "It was played since you approved the plan. Kept.",
    "error.reap.check.movie_removed": "Removed the file through Radarr",
    "error.reap.check.exclusion_confirmed": "Import exclusion confirmed. It won't re-download",
    "error.reap.check.exclusion_off": "Import exclusion off for this Radarr, so none was added",
    "error.reap.check.season_unmonitored": "Unmonitored season {season} in Sonarr",
    "error.reap.check.season_unmonitor_confirmed": "Confirmed the season is no longer monitored",
    "error.reap.check.season_files_not_deleted": "Deleted the season's episode files",
    "error.reap.check.season_files_deleted": "Deleted the season's {count} episode file(s)",
    "error.reap.check.season_no_files_kept": "No files left to remove. Kept.",
    "error.reap.check.season_no_files_unmonitored": (
        "No files left to remove. The season was left unmonitored."
    ),
    "error.reap.check.season_changed_unmonitored": (
        "It changed while Reaper was working. The season was left unmonitored."
    ),
}


class Refusal(ValueError):  # noqa: N818 -- "Refusal" is the domain noun the plan names; not an -Error
    """A typed refusal the engine or a service raises: a catalog code plus raw params.

    A ``ValueError`` subclass, so every existing ``except ValueError`` around a
    save-boundary check keeps catching it unchanged. ``str(refusal)`` renders the
    English template so a log line or an old caller that only ever read the
    message still gets one. ``code`` and ``params`` are what a typed reader (the
    API layer, a translator) uses instead.

    ``status`` is the HTTP status the API answers with when this refusal reaches
    a route: 422 by default (a well-formed request whose content was refused),
    overridden by a subclass or a call site that means something else (a 400, a
    409).
    """

    def __init__(self, code: str, /, *, status: int = 422, **params: ReasonParam) -> None:
        self.code = code
        self.params: dict[str, ReasonParam] = params
        self.status = status
        super().__init__(str(self))

    def __str__(self) -> str:
        # Through `english()`, not a bare `str.format`, so a nested `Reason`
        # param (an `IntegrationError`/`PlexError` carried in via
        # `as_reason()`) composes into its own sentence instead of printing
        # the dataclass.
        return english(self.as_reason())

    def as_reason(self) -> Reason:
        """This refusal's code and params, as the typed container a wire field carries.

        Lets a route hand a caught ``Refusal`` straight to a ``ReasonKey`` field
        (``ScanStatus.error_reason``, ``ReapStatus.error_reason``) the same way
        ``api.errors.refuse`` builds one for an HTTPException: one code, one
        params dict, read by both readers instead of a second English rendering.
        """
        return Reason(self.code, dict(self.params))


def english(reason: Reason) -> str:
    """A stored :class:`Reason` rendered as English. This is the one place that
    happens, so a prose column, a log line, and an API client with no catalog
    entry all read the same sentence a translated build's catalog was written
    from.

    A ``legacy`` reason (a sentence frozen before its condition got a code)
    renders its stored text verbatim, never looked up. Anything else formats
    ``MESSAGES[reason.id]`` against ``reason.params``, with two additions
    ``str.format`` cannot do on its own. A numeric param gets a ``{name}_gb``
    companion, the same derivation the browser's ``composeIn``
    (``frontend/src/why.ts``) applies to every numeric param, so a template
    can show a byte count in gigabytes without the raise site pre-rounding it.
    A param that is itself a :class:`Reason` (an ``IntegrationError`` or
    ``PlexError`` nested via ``as_reason()``, the same shape a stored
    explanation's own params can carry) recurses through this same function
    instead of printing the dataclass's ``repr``. A tuple of them renders each
    and joins with ``"; "``, mirroring ``why.ts``'s ``composeIn`` exactly, so
    the two composers can never read a nested reason two different ways. An id
    with no catalog entry, or params that do not match its placeholders,
    renders as the id itself. That is the same fallback :meth:`Refusal.__str__`
    uses, and for the same reason: never raise out of a report or a log line
    over a formatting slip.
    """
    if reason.id == "legacy":
        return str(reason.params.get("text", ""))
    template = MESSAGES.get(reason.id, reason.id)
    params: dict[str, Any] = {}
    for key, value in reason.params.items():
        if isinstance(value, Reason):
            params[key] = english(value)
        elif isinstance(value, tuple):
            params[key] = "; ".join(english(v) for v in value if isinstance(v, Reason))
        else:
            params[key] = value
            if isinstance(value, int | float) and not isinstance(value, bool):
                params[f"{key}_gb"] = f"{value / 1_000_000_000:.1f}"
    try:
        return template.format(**params)
    except (KeyError, IndexError):
        return template
