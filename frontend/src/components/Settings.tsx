// SPDX-License-Identifier: AGPL-3.0-or-later
//
// Settings: everything you point Reaper at, and everything you let it do.
//
// The API key is write-only end to end -- it is sent once, encrypted on arrival, and
// never comes back, so a field for it is always blank and "leave it empty to keep the
// current one". Nothing here can delete anything: the deletion switch lives in
// Policy → Deletion, and the Security panel only manages the admin password that
// confirms it.

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  type ChangeEvent,
  type CSSProperties,
  type DragEvent,
  type ReactNode,
  type RefObject,
  useEffect,
  useRef,
  useState,
} from "react";
import { accentInk, DEFAULT_ACCENT, isHexColor } from "../accent";
import {
  api,
  type ExpandSeasonsMode,
  type Instance,
  type InstanceTest,
  type RestoreSummary,
  type Schedule,
  type ScheduledJob,
} from "../api";
import { useBackGuard } from "../backnav";
import { bytes, count, since } from "../format";
import { NARROW_SCREEN_QUERY, useMediaQuery } from "../useMediaQuery";
import { useSafety } from "../useSafety";
import { JobStatus, useJobFlash } from "./JobStatus";
import { LogsPanel } from "./LogsPanel";
import { ModalShell } from "./ModalShell";
import { PlexPanel } from "./PlexPanel";
import { FixedQuantity } from "./QuantityInput";
import { ScanRow } from "./ScanBar";
import { Segmented } from "./Segmented";
import { KINDS, kindLabel, ServiceModal, TestBadge } from "./ServiceModal";
import { StaleReadNotice } from "./StaleReadNotice";
import { Switch } from "./Switch";

// The Plex panel moved to its own file; SetupWizard imports it from here, so the name
// stays available at this path.
export { PlexPanel };

export type Panel =
  | "general"
  | "services"
  | "plex"
  | "jobs"
  | "notifications"
  | "security"
  | "backup"
  | "logs"
  | "about";

const PANELS: { id: Panel; label: string }[] = [
  { id: "general", label: "General" },
  { id: "services", label: "Services" },
  { id: "plex", label: "Plex" },
  { id: "jobs", label: "Jobs" },
  { id: "notifications", label: "Notifications" },
  { id: "security", label: "Security" },
  // Named for both halves, matching the panel's own heading: restoring is the half an operator
  // comes looking for under pressure, and a tab reading "Backup" alone hides it.
  { id: "backup", label: "Backup & Restore" },
  { id: "logs", label: "Logs" },
  { id: "about", label: "About" },
];

// --- General -----------------------------------------------------------------

type ThemeChoice = "system" | "light" | "dark";

function readTheme(): ThemeChoice {
  try {
    const stored = localStorage.getItem("reaper-theme");
    return stored === "light" || stored === "dark" ? stored : "system";
  } catch {
    return "system";
  }
}

/** Apply and remember the theme. "system" removes the override so the device decides;
 *  index.html applies the stored choice before first paint, so there is no flash. */
function applyTheme(choice: ThemeChoice) {
  const root = document.documentElement;
  if (choice === "system") {
    delete root.dataset.theme;
  } else {
    root.dataset.theme = choice;
  }
  try {
    if (choice === "system") localStorage.removeItem("reaper-theme");
    else localStorage.setItem("reaper-theme", choice);
  } catch {
    // Storage can be unavailable (private windows); the page still themes for now.
  }
}

// Quick-pick accents. The first is the built-in default; the rest are a spread of hues that
// stay clear of the fixed red "remove" and green "keep" verdict colors. Any hex is allowed
// via the field, so this is a shortcut, not the whole choice.
const ACCENT_PRESETS = [
  DEFAULT_ACCENT,
  "#4f46e5",
  "#7c3aed",
  "#0ea5e9",
  "#14b8a6",
  "#f59e0b",
  "#ec4899",
];

// The browser's full IANA zone list, fetched once and cached: it never changes within a
// session, so recomputing it per render would be waste. Falls back to just UTC on an engine
// without Intl.supportedValuesOf, so the control still works.
let _zoneCache: string[] | null = null;
function allTimeZones(): string[] {
  if (_zoneCache) return _zoneCache;
  let zones: string[] = [];
  try {
    const supported = (Intl as { supportedValuesOf?: (key: string) => string[] }).supportedValuesOf;
    if (supported) zones = supported("timeZone");
  } catch {
    zones = [];
  }
  _zoneCache = zones.length ? zones : ["UTC"];
  return _zoneCache;
}

// What the day box starts at while the stored default is Forever, so pressing Days opens on a
// sensible length instead of an empty box. One declaration, because the seed and Discard have to
// agree: Discard putting back a different number is how a discarded draft came back (issue #90).
const SPARE_DAYS_SEED = 30;

export function GeneralPanel({
  /** Called whenever the save bar gains or loses a draft, so the section rail can hold a
   *  switch that would discard one. Pass a STABLE function: it is an effect dependency. */
  onDirtyChange,
}: {
  onDirtyChange?: ((dirty: boolean) => void) | undefined;
} = {}) {
  const queryClient = useQueryClient();
  const general = useQuery({ queryKey: ["general-settings"], queryFn: api.general });

  const [name, setName] = useState("");
  const [url, setUrl] = useState("");
  const [tz, setTz] = useState("");
  const [proxies, setProxies] = useState("");
  const [accent, setAccent] = useState(DEFAULT_ACCENT);
  // The default spare length, as a draft in two halves: which mode is chosen, and the box's
  // live number. They are held apart because Forever stores 0 and the typed number has to
  // survive a trip through it; `spareValue` below folds them back into the one stored field.
  // The number seeds to a sensible 30 while the stored default is Forever, so switching to a
  // length starts somewhere reasonable.
  const [spareForever, setSpareForever] = useState(false);
  const [spareDays, setSpareDays] = useState(SPARE_DAYS_SEED);
  const [theme, setTheme] = useState<ThemeChoice>(readTheme);
  const [revealedKey, setRevealedKey] = useState<string | null>(null);
  const [copied, setCopied] = useState(false);
  const [confirmReplace, setConfirmReplace] = useState(false);
  const [confirmRemove, setConfirmRemove] = useState(false);

  // Seed the editable fields from the server once per load (and re-seed after saves,
  // which return the canonical stored values -- rule 39).
  const seeded = useRef(false);
  useEffect(() => {
    if (!general.data || seeded.current) return;
    seeded.current = true;
    setName(general.data.application_name);
    setUrl(general.data.application_url ?? "");
    setTz(general.data.timezone);
    setProxies(general.data.trusted_proxies.join(", "));
    setAccent(general.data.accent_color);
    setSpareForever(general.data.default_spare_days === 0);
    if (general.data.default_spare_days > 0) setSpareDays(general.data.default_spare_days);
  }, [general.data]);

  const save = useMutation({
    mutationFn: api.saveGeneral,
    onSuccess: (data, sent) => {
      // Re-seed from the canonical stored values (rule 39) -- but only the fields this Save
      // actually sent. The save bar sends every dirty field at once, so it no longer takes one
      // row's Save to reach here; the two controls that still save on the spot do. A Switch or
      // a select (the reverse-proxy toggle, the expand-seasons mode) writes immediately, and
      // re-seeding every field on its response would wipe whatever text was half-typed at the
      // time, with nothing on screen to say why (B-18).
      //
      // Setting the query cache stays unconditional: it is the canonical stored state, and it
      // is what re-applies the accent app-wide so a save re-tints everything.
      queryClient.setQueryData(["general-settings"], data);
      if ("application_name" in sent) setName(data.application_name);
      if ("application_url" in sent) setUrl(data.application_url ?? "");
      if ("timezone" in sent) setTz(data.timezone);
      if ("trusted_proxies" in sent) setProxies(data.trusted_proxies.join(", "));
      if ("accent_color" in sent) setAccent(data.accent_color);
      if ("default_spare_days" in sent) {
        setSpareForever(data.default_spare_days === 0);
        if (data.default_spare_days > 0) setSpareDays(data.default_spare_days);
      }
    },
  });

  // Three buttons (Show, Generate/Replace, Copy) share one notice, and a mutation holds its
  // error until its OWN next call -- so rendering `reveal.error ?? generate.error ?? copy.error`
  // left a failure on screen beside a key that had since worked: fail Copy on a plain-http LAN
  // page, then press Show, and the red notice was still the copy failure (B-33). The three
  // report through this one piece of state instead, cleared the moment any of them starts, so
  // the notice always describes the last thing the operator did.
  const [keyError, setKeyError] = useState<string | null>(null);
  const reveal = useMutation({
    mutationFn: api.revealApiKey,
    onMutate: () => setKeyError(null),
    onSuccess: (r) => setRevealedKey(r.key),
    onError: (e: Error) => setKeyError(e.message),
  });
  const generate = useMutation({
    mutationFn: api.generateApiKey,
    onMutate: () => setKeyError(null),
    onSuccess: (r) => {
      setRevealedKey(r.key);
      setConfirmReplace(false);
      void queryClient.invalidateQueries({ queryKey: ["general-settings"] });
    },
    onError: (e: Error) => setKeyError(e.message),
  });

  const copyKey = async () => {
    let key = revealedKey;
    if (key === null) {
      const r = await api.revealApiKey();
      key = r.key;
    }
    // A self-hosted Reaper is often reached over plain http on a LAN, where the browser
    // withholds the clipboard API. Say so plainly instead of throwing a raw TypeError.
    if (!navigator.clipboard) {
      throw new Error(
        "Copying needs a secure (https) page. Press Show, then select the key by hand.",
      );
    }
    await navigator.clipboard.writeText(key);
    setCopied(true);
    setTimeout(() => setCopied(false), 2000);
  };
  const copy = useMutation({
    mutationFn: copyKey,
    onMutate: () => setKeyError(null),
    onError: (e: Error) => setKeyError(e.message),
  });
  const removeKey = useMutation({
    mutationFn: api.removeApiKey,
    onMutate: () => setKeyError(null),
    onError: (e: Error) => setKeyError(e.message),
    onSuccess: () => {
      setRevealedKey(null);
      setConfirmRemove(false);
      void queryClient.invalidateQueries({ queryKey: ["general-settings"] });
    },
  });

  // The dirty checks and the pending list are computed BEFORE the early returns below, because
  // the effect that reports them to `Settings` is a hook and a hook may not sit after a
  // conditional return. Each one is guarded on `data`, which the second early return then
  // narrows to non-null for the whole render beneath it.
  const data = general.data;

  const nameDirty = !!data && name.trim() !== data.application_name;
  const urlDirty = !!data && url.trim() !== (data.application_url ?? "");
  const tzDirty = !!data && tz !== data.timezone;
  const accentValid = isHexColor(accent);
  const accentDirty = !!data && accent.trim().toLowerCase() !== data.accent_color.toLowerCase();
  const proxyList = proxies
    .split(",")
    .map((p) => p.trim())
    .filter(Boolean);
  const proxiesDirty = !!data && proxyList.join(", ") !== data.trusted_proxies.join(", ");
  // The two halves of the draft fold back into the one stored number before anything compares
  // them, because Forever IS 0 in that field. Pressing Forever therefore reads as a change to
  // the same field the box edits, and one Discard puts both back. It used to write 0 on the
  // press while the bar, gated on the stored value, unmounted and took its Discard with it --
  // so the number the bar had just called unsaved went in on the next press, without a Save.
  const spareValue = spareForever ? 0 : spareDays;
  const spareDirty = !!data && spareValue !== data.default_spare_days;

  // One save for the panel (rule 43). Each of these rows used to carry its own Save, rendered
  // inside the right-aligned control box, so the first keystroke made the button appear and
  // shoved the field being typed in 71px to the left -- then back again on undo. The bar names
  // what is unsaved and sends all of it in one request. The controls that take effect the
  // moment they change are not drafts and do not join it: two of them call `save.mutate`
  // themselves -- the reverse-proxy `Switch` and the expand-seasons `<select>` -- and the theme
  // `<select>` calls `applyTheme`, which writes this browser's own localStorage and never
  // reaches the server, so it has no draft to hold. The spare-length `Segmented` was a third
  // until it started staging `default_spare_days` here instead (see `spareValue` above).
  const pending: { label: string; patch: Parameters<typeof api.saveGeneral>[0] }[] = [];
  if (nameDirty)
    pending.push({ label: "Application name", patch: { application_name: name.trim() } });
  if (urlDirty) pending.push({ label: "Application URL", patch: { application_url: url.trim() } });
  if (tzDirty) pending.push({ label: "Time zone", patch: { timezone: tz } });
  if (accentDirty)
    pending.push({ label: "Accent color", patch: { accent_color: accent.trim().toLowerCase() } });
  if (spareDirty)
    pending.push({ label: "Default spare length", patch: { default_spare_days: spareValue } });
  // Only while the switch is on. Turning it off disables the box, and a bar naming a field the
  // operator cannot reach to fix is worse than one that waits for them to turn it back on.
  if (proxiesDirty && data?.proxy_trust_enabled)
    pending.push({ label: "Trusted proxy addresses", patch: { trusted_proxies: proxyList } });
  // A half-typed hex code would be stored as the app-wide accent, so the whole save waits on
  // it rather than silently dropping that one field from a bar that just named it.
  const accentBlocks = accentDirty && !accentValid;

  // What this panel would LOSE, reported up to `Settings` so that leaving the section can stop
  // and ask first. Nearly always that is the bar (rule 43), but not quite, so the two are
  // computed apart rather than one read off the other. A proxy list typed and then parked behind
  // its own switch is dropped from `pending` on purpose just above, because the bar must not name
  // a field the operator cannot reach to fix -- yet the text is still sitting in the disabled
  // box, still unsaved, and still gone on unmount. Reading the bar alone let exactly that one
  // walk out silently, on the panel that had just promised to ask.
  //
  // Rule 146: this reports two things at once, that there is something to lose and that the
  // operator can still reach it, so both are read against every early return below -- the
  // report fires while this renders "Loading…" (nothing is dirty yet, `data` is undefined) and
  // it must not outlive the form, which is why the failure branch below now keeps the form
  // whenever there is a row to render.
  const hasDrafts = pending.length > 0 || (proxiesDirty && !data?.proxy_trust_enabled);
  useEffect(() => {
    onDirtyChange?.(hasDrafts);
  }, [hasDrafts, onDirtyChange]);
  useEffect(() => () => onDirtyChange?.(false), [onDirtyChange]);

  if (general.isPending) {
    return <p className="muted">Loading…</p>;
  }
  // Only when there is nothing to render. A refetch that fails AFTER a good load leaves `data` in
  // place (React Query keeps the last good row and raises `isError` beside it), and trading the
  // whole form for this paragraph there took the save bar and its Discard with it while the
  // drafts stayed in state -- still reported unsaved to `Settings`, which then asked to discard
  // edits the operator could no longer see, save, or put back. Pressing Generate API key on a
  // server that blinks is enough to reach it, because that invalidates this very query. So a
  // failed refetch keeps the form on the last good values; this is for a load that never
  // landed one.
  if (!data) {
    return (
      <p className="notice notice-error">Couldn't load these settings. Reload to try again.</p>
    );
  }

  // The current zone may not be in the browser's list (an older engine, or a server-only
  // zone); keep it selectable so a save never silently drops it.
  const zoneOptions =
    data.timezone && !allTimeZones().includes(data.timezone)
      ? [data.timezone, ...allTimeZones()]
      : allTimeZones();

  const discardDrafts = () => {
    setName(data.application_name);
    setUrl(data.application_url ?? "");
    setTz(data.timezone);
    setAccent(data.accent_color);
    setProxies(data.trusted_proxies.join(", "));
    setSpareForever(data.default_spare_days === 0);
    // BOTH halves, unlike the mount seed and the save response above, which leave the number
    // alone under a stored Forever so the last length is remembered. Discard is a full undo, so
    // it goes back to the stored length or to the same number the box seeds at. Skipping it left
    // the discarded figure in the hidden box, and the next press of Days re-staged it.
    setSpareDays(data.default_spare_days > 0 ? data.default_spare_days : SPARE_DAYS_SEED);
  };

  return (
    <div className="panel">
      <h2>General</h2>
      <p className="muted">How this Reaper presents itself, and how other tools may talk to it.</p>

      {/* Same obligation as the twin in `PlexPanel` (rule 72): the `!data` branch above keeps the
          form through a failed refetch so the drafts in it stay reachable, which leaves this line
          the only thing saying the values below may be stale. */}
      {general.isError && <StaleReadNotice />}

      <div className="set-group">
        <h3>Application</h3>
        <div className="set-rows">
          <div className="set-row">
            <span className="set-label">Application name</span>
            <p className="help">
              Shown in Discord messages and the browser tab, so you can tell two installs apart.
            </p>
            <div className="set-control">
              <input
                type="text"
                value={name}
                maxLength={60}
                onChange={(e) => setName(e.target.value)}
                aria-label="Application name"
              />
            </div>
          </div>
          <div className="set-row">
            <span className="set-label">Application URL</span>
            <p className="help">
              Where people reach Reaper, for example https://reaper.example.com. Notifications use
              it to link back here. Leave empty and notifications simply skip the link.
            </p>
            <div className="set-control">
              <input
                type="text"
                value={url}
                placeholder="https://reaper.example.com"
                onChange={(e) => setUrl(e.target.value)}
                aria-label="Application URL"
              />
            </div>
          </div>
          <div className="set-row">
            <span className="set-label">Time zone</span>
            <p className="help">The server's time zone.</p>
            <div className="set-control">
              <select value={tz} aria-label="Time zone" onChange={(e) => setTz(e.target.value)}>
                {zoneOptions.map((z) => (
                  <option key={z} value={z}>
                    {z}
                  </option>
                ))}
              </select>
            </div>
          </div>
        </div>
      </div>

      <div className="set-group">
        <h3>Appearance</h3>
        <div className="set-rows">
          <div className="set-row accent-row">
            <span className="set-label">Accent color</span>
            <p className="help">
              The color Reaper uses for buttons, links, and highlights. Everyone who opens this
              install sees it. Pick from the wheel or type a hex code.
            </p>
            <div className="set-control">
              {/* Swatch and hex code are one control (the .url-join pattern): the swatch is a
                  prefix fused inside the field's box, so a narrow screen can never split them
                  onto two lines. */}
              <span className="hex-join">
                <span className="swatch-wrap">
                  <input
                    type="color"
                    value={accentValid ? accent : DEFAULT_ACCENT}
                    aria-label="Accent color"
                    onChange={(e) => setAccent(e.target.value)}
                  />
                </span>
                <input
                  type="text"
                  className="hexfield"
                  value={accent}
                  spellCheck={false}
                  maxLength={7}
                  aria-label="Accent color hex code"
                  onChange={(e) => setAccent(e.target.value)}
                />
              </span>
              {accent.toLowerCase() !== DEFAULT_ACCENT && (
                <button className="link" onClick={() => setAccent(DEFAULT_ACCENT)}>
                  Reset to default
                </button>
              )}
            </div>
            {!accentValid && <p className="help field-error">Enter a hex code like #25c3ff.</p>}
            <div className="presets" aria-label="Quick colors">
              {ACCENT_PRESETS.map((c) => (
                <button
                  key={c}
                  type="button"
                  className="preset-dot"
                  style={{ background: c }}
                  aria-label={c}
                  aria-pressed={accent.toLowerCase() === c}
                  onClick={() => setAccent(c)}
                />
              ))}
            </div>
            <div
              className="accent-preview"
              style={
                accentValid
                  ? ({
                      "--accent": accent,
                      "--accent-ink": accentInk(accent),
                    } as CSSProperties)
                  : undefined
              }
            >
              <span className="pv-label">Preview</span>
              <button className="primary" type="button" disabled>
                Scan library
              </button>
              <a href="#" onClick={(e) => e.preventDefault()}>
                Policy → Deletion
              </a>
            </div>
          </div>

          <div className="set-row">
            <span className="set-label">Theme</span>
            <p className="help">
              Light or dark. "Match my device" follows your system setting. Applies to this browser
              only.
            </p>
            <div className="set-control">
              <select
                value={theme}
                aria-label="Theme"
                onChange={(e) => {
                  const next = e.target.value as ThemeChoice;
                  setTheme(next);
                  applyTheme(next);
                }}
              >
                <option value="system">Match my device</option>
                <option value="light">Light</option>
                <option value="dark">Dark</option>
              </select>
            </div>
          </div>
        </div>
      </div>

      <div className="set-group">
        <h3>Review queue</h3>
        <div className="set-rows">
          <div className="set-row">
            <span className="set-label">Expand seasons by default</span>
            <p className="help">
              TV shows in the review queue open with every season showing. Mobile means a narrow
              screen, like a phone.
            </p>
            <div className="set-control">
              {/* Four choices, so a select rather than a Segmented (rule 41), on the same
                  control standard as the Theme picker above. */}
              <select
                value={data.expand_seasons_mode}
                aria-label="Expand seasons by default"
                disabled={save.isPending}
                onChange={(e) =>
                  save.mutate({ expand_seasons_mode: e.target.value as ExpandSeasonsMode })
                }
              >
                <option value="off">Off</option>
                <option value="desktop">Desktop</option>
                <option value="both">Desktop &amp; mobile</option>
                <option value="mobile">Mobile</option>
              </select>
            </div>
          </div>
          <div className="set-row">
            <span className="set-label">Default spare length</span>
            <p className="help">
              How long a plain Spare keeps a title before Reaper judges it again. Set a different
              length for any single title from its Spare menu.
            </p>
            <div className="set-control">
              {/* Both halves read and write the DRAFT, never the stored value. A press stages
                  the mode in the save bar beside the number, so the bar names one field, one
                  Discard puts both back, and neither is written until Save. */}
              <Segmented
                value={spareForever ? "forever" : "days"}
                options={[
                  ["days", "Days"],
                  ["forever", "Forever"],
                ]}
                label="Default spare length"
                onChange={(mode) => setSpareForever(mode === "forever")}
              />
              {/* Only while the draft is a length -- Forever hides the box, matching how a
                  group's sub-controls disappear when its toggle is off. */}
              {!spareForever && (
                <FixedQuantity
                  value={spareDays}
                  suffix="days"
                  min={1}
                  max={3650}
                  width="narrow"
                  ariaLabel="Default spare length in days"
                  disabled={save.isPending}
                  onChange={(n) => setSpareDays(Math.max(1, Math.min(3650, n)))}
                />
              )}
            </div>
          </div>
        </div>
      </div>

      <div className="set-group">
        <h3>API access</h3>
        <div className="set-rows">
          {/* A cluster, not a box: the key field plus four buttons. It keeps a shrink-to-fit
              control column so those buttons stay on one line (see `.set-row-cluster`). */}
          <div className="set-row set-row-cluster">
            <span className="set-label">API key</span>
            {/* This sentence is the whole basis on which an operator decides to hand a key
                to a third-party dashboard, so it names what the fence in api/middleware.py
                (_API_KEY_READS_DENIED / _API_KEY_WRITES) actually allows, not a rounder
                claim. Two roundings have already been wrong here, both in the safe-sounding
                direction:

                - it said a key "cannot change any setting" while /api/profile sat in the
                  write allowlist, so a key holder could turn the run limits off (S-2);
                - it said a key "reads your library", which is most of what a key reads and
                  not the part that decides the question. A key also reads every settings
                  page, and it read one person's whole viewing breakdown until #117 moved
                  /api/fairness behind the browser.

                So the read clause overstates rather than understates: on the screen where a
                key is handed out, "more than you think" is the direction that fails safe.
                Note which way that cuts on the viewing clause. It is now a REFUSAL, so it
                moved to the closing list, where the safe direction reverses: naming a
                refusal the fence does not make is the rounding that gets someone hurt, and
                this one is only true while /api/fairness stays denied. The list still ends
                with "any other setting" rather than enumerating the rest, for the reason
                api_key_scope_description exists: the fence is far tighter than any short
                list of what it refuses, and naming four of them read as a promise that the
                rest were allowed.

                This paragraph is hand-written and its twin in the API reference is
                generated, so nothing here fails when the fence moves. The guard is on the
                other side: test_the_sentence_leads_with_what_the_key_can_do pins the twin
                phrase for phrase and names this file in every failure message. */}
            <p className="help">
              Send it as the X-Api-Key header so scripts and other apps can use Reaper without
              signing in. A key reads nearly everything, your settings included, and can start
              scans, build plans, and change your policy, run limits, and grace. Nothing else: it
              cannot turn deletion on, run a reap, read your logs, see who watched what, or change
              any other setting.
            </p>
            <div className="set-control">
              {data.api_key_set ? (
                <>
                  <input
                    className="keyfield"
                    type="text"
                    readOnly
                    value={revealedKey ?? "••••••••••••••••••••••••"}
                    aria-label="API key"
                  />
                  {revealedKey === null ? (
                    <button disabled={reveal.isPending} onClick={() => reveal.mutate()}>
                      Show
                    </button>
                  ) : (
                    <button onClick={() => setRevealedKey(null)}>Hide</button>
                  )}
                  <button disabled={copy.isPending} onClick={() => copy.mutate()}>
                    {copied ? "Copied" : "Copy"}
                  </button>
                  {confirmReplace ? (
                    <>
                      <button
                        className="danger"
                        disabled={generate.isPending}
                        onClick={() => generate.mutate()}
                      >
                        Confirm replace
                      </button>
                      <button onClick={() => setConfirmReplace(false)}>Cancel</button>
                    </>
                  ) : (
                    <button
                      className="ghost"
                      title="The old key stops working immediately"
                      onClick={() => setConfirmReplace(true)}
                    >
                      Replace…
                    </button>
                  )}
                  {/* Replacing swaps one working key for another, so it never closes this
                      lane. Remove does: afterwards nothing gets in on the header at all.
                      Same two-step confirm the Replace control uses. */}
                  {confirmRemove ? (
                    <>
                      <button
                        className="danger"
                        disabled={removeKey.isPending}
                        onClick={() => removeKey.mutate()}
                      >
                        Confirm remove
                      </button>
                      <button onClick={() => setConfirmRemove(false)}>Cancel</button>
                    </>
                  ) : (
                    <button
                      className="ghost"
                      title="Anything using this key stops working immediately"
                      onClick={() => setConfirmRemove(true)}
                    >
                      Remove…
                    </button>
                  )}
                </>
              ) : (
                <button
                  className="primary"
                  disabled={generate.isPending}
                  onClick={() => generate.mutate()}
                >
                  {generate.isPending ? "Generating…" : "Generate API key"}
                </button>
              )}
            </div>
          </div>
          {/* A link, not a box, so it releases the control track (`.set-row-plain`). */}
          <div className="set-row set-row-plain">
            <span className="set-label">API reference</span>
            {/* Says "as you", not "with your key", because the page preselects your SESSION:
                35 of the 47 writes do not offer the key at all, and the button reaches them
                all, arming included. Naming the key here would size the blast radius by the
                fence two rows up, which is far tighter than what this button spends (rule
                144). The key clause above is generated and guarded; this one is hand-written
                and its guard is test_the_reference_page_sends_the_csrf_header_it_names, which
                names this file. */}
            <p className="help">
              Every endpoint, documented from the running app. The try-it-out button sends real
              requests as you, so it can change settings and start work, not just read. Only visible
              while signed in.
            </p>
            <div className="set-control">
              <a className="btn-link" href="/api/docs" target="_blank" rel="noreferrer">
                Open the API reference ↗
              </a>
            </div>
          </div>
        </div>
        {keyError && <p className="notice notice-error">{keyError}</p>}
      </div>

      <div className="set-group">
        <h3>Reverse proxy</h3>
        <div className="set-rows">
          {/* A Switch, not a box, so it releases the control track (`.set-row-plain`). The row
              below it holds the addresses box and keeps the track. */}
          <div className="set-row set-row-plain">
            <span className="set-label">Behind a reverse proxy</span>
            <p className="help">
              Turn this on if Nginx, Traefik, Caddy or similar sits in front of Reaper. Reaper will
              then trust the proxy to say which address each visitor really came from, which keeps
              sign-in rate limits accurate per visitor instead of lumping everyone together. It is
              also how Reaper learns that visitors arrive over HTTPS, so it can mark the sign-in
              cookie HTTPS-only.
            </p>
            <div className="set-control">
              <Switch
                checked={data.proxy_trust_enabled}
                disabled={save.isPending}
                ariaLabel="Behind a reverse proxy"
                onChange={(enabled) => save.mutate({ proxy_trust_enabled: enabled })}
              />
            </div>
          </div>
          <div className={data.proxy_trust_enabled ? "set-row" : "set-row dim"}>
            <span className="set-label">Trusted proxy addresses</span>
            <p className="help">
              Only requests arriving from these addresses may claim to be forwarded. Comma
              separated, single addresses or ranges like 172.16.0.0/12.
            </p>
            <div className="set-control">
              <input
                type="text"
                value={proxies}
                disabled={!data.proxy_trust_enabled}
                placeholder="172.16.0.1, 10.0.0.0/8"
                onChange={(e) => setProxies(e.target.value)}
                aria-label="Trusted proxy addresses"
              />
            </div>
          </div>
        </div>
        <p className="group-hint muted">
          Off by default, and forwarded headers from anywhere else are always ignored: a stranger
          can't fake their address to dodge the login lockout.
        </p>
      </div>

      {/* Only when there is no bar to put it in. A control that saves on the spot fails with
          nothing unsaved, so its refusal has nowhere else to go; a refused BAR save renders
          inside the bar instead, beside the fields it just refused to write. */}
      {save.error && pending.length === 0 && (
        <p className="notice notice-error">{save.error.message}</p>
      )}

      {/* The one save affordance on this panel (rule 43), the same bar the policy editor uses:
          it names what is unsaved, saves all of it in one press, and offers Discard. Rendered
          only while there is something to save, and sticky at the foot of the screen, so the
          field being typed in never moves. */}
      {pending.length > 0 && (
        <div className="savebar">
          <span className="savebar-what">
            Unsaved changes: <strong>{pending.map((p) => p.label).join(" · ")}</strong>
            {accentBlocks && (
              <span className="savebar-blocked">Enter a hex code like #25c3ff to save.</span>
            )}
          </span>
          <button className="ghost" disabled={save.isPending} onClick={discardDrafts}>
            Discard
          </button>
          <button
            className="primary"
            disabled={save.isPending || accentBlocks}
            onClick={() => save.mutate(Object.assign({}, ...pending.map((p) => p.patch)))}
          >
            {save.isPending ? "Saving…" : "Save changes"}
          </button>
          {/* Inside the bar, not below the panel (rule 42, and the same slot
              `PolicyEditor`'s bar uses): the route refuses the whole body before writing any
              of it, so this sentence is the only thing standing between the operator and the
              belief that all six fields went in. The bar is sticky, so a notice outside it
              renders at the document foot -- off screen for anyone editing the top group,
              which is where five of these six fields are. */}
          {save.error && <p className="notice notice-error">{save.error.message}</p>}
        </div>
      )}
    </div>
  );
}

// --- Services --------------------------------------------------------------

function ServiceCard({ instance, onEdit }: { instance: Instance; onEdit: () => void }) {
  const queryClient = useQueryClient();
  const [test, setTest] = useState<InstanceTest | null>(null);
  // A two-step "Remove" -> "Confirm remove" toggle, mirroring the Safety arm flow below,
  // rather than a native confirm() dialog -- the OS alert box ignores the app's theme and
  // typography and is the only confirmation in the product that does.
  const [confirmingRemove, setConfirmingRemove] = useState(false);

  const invalidate = () => {
    void queryClient.invalidateQueries({ queryKey: ["instances"] });
    void queryClient.invalidateQueries({ queryKey: ["setup"] });
  };

  const testSaved = useMutation({
    mutationFn: () => api.testSavedInstance(instance.id),
    onSuccess: (r) => {
      setTest(r);
      invalidate();
    },
  });
  const remove = useMutation({
    mutationFn: () => api.deleteInstance(instance.id),
    onSuccess: invalidate,
  });

  const certCheckOff = !instance.verify_tls && instance.base_url.startsWith("https://");

  return (
    <article className={`service-card ${instance.enabled ? "" : "disabled"}`}>
      <div className="service-card-body">
        <div className="instance-id">
          <span className={`kind-badge kind-${instance.kind}`}>{kindLabel(instance.kind)}</span>
          <strong>{instance.name}</strong>
          {!instance.enabled && <span className="chip">disabled</span>}
          {certCheckOff && <span className="chip chip-warn">certificate check off</span>}
        </div>
        <div className="instance-url muted">{instance.base_url}</div>
        <div className="instance-status">
          {/* All three states render through the one badge. What the card remembers from
              the last test is the same shape a fresh test returns, so it is handed over as
              one rather than rebuilt with a second set of markup that can drift. */}
          {test ? (
            <TestBadge result={test} />
          ) : instance.last_error ? (
            <TestBadge result={{ ok: false, detail: instance.last_error, version: null }} />
          ) : instance.last_ok_at ? (
            <TestBadge
              result={{ ok: true, detail: "Reached", version: instance.detected_version }}
            />
          ) : (
            <span className="muted">Not tested yet</span>
          )}
        </div>
        {(remove.error ?? testSaved.error) && (
          <p className="notice notice-error notice-inline">
            {remove.error
              ? `This service wasn't removed: ${remove.error.message}`
              : `The test didn't run: ${testSaved.error?.message}`}
          </p>
        )}
      </div>
      <div className="service-card-foot">
        {confirmingRemove ? (
          <>
            <button
              type="button"
              className="danger"
              title="Only forgets it in Reaper. Nothing is changed in the service itself."
              onClick={() => {
                setConfirmingRemove(false);
                remove.mutate();
              }}
            >
              Confirm remove
            </button>
            <button type="button" onClick={() => setConfirmingRemove(false)}>
              Cancel
            </button>
          </>
        ) : (
          <>
            <button type="button" disabled={testSaved.isPending} onClick={() => testSaved.mutate()}>
              {testSaved.isPending ? "Testing…" : "Test"}
            </button>
            <button type="button" onClick={onEdit}>
              Edit
            </button>
            <button type="button" className="danger" onClick={() => setConfirmingRemove(true)}>
              Remove
            </button>
          </>
        )}
      </div>
    </article>
  );
}

export function ServicesPanel() {
  const { data, isPending, error } = useQuery({ queryKey: ["instances"], queryFn: api.instances });
  const [modal, setModal] = useState<{ kind: string; instance: Instance | null } | null>(null);
  // The modal's save lives inside ServiceModal; it mirrors its pending state here so Back
  // refuses a close mid-save exactly as the scrim/Escape/✕ do, the same arrangement the
  // schedule editor uses (B-19).
  const savePendingRef = useRef(false);
  // Back closes the service editor instead of leaving Reaper -- unless a save is in flight.
  useBackGuard(
    modal !== null,
    () => setModal(null),
    () => !savePendingRef.current,
  );

  return (
    <div className="panel panel-wide">
      <h2>Services</h2>
      <p className="blurb">
        The apps Reaper reads from. It only ever reads. Nothing here can delete a file.
      </p>
      {error && <p className="notice notice-error">{(error as Error).message}</p>}
      {isPending && <p className="muted">Loading…</p>}
      {data &&
        KINDS.map((k) => {
          const rows = data.filter((i) => i.kind === k.value);
          // A singleton kind (Tautulli) shows no "Add" once one exists: it mirrors one
          // Plex, and Reaper connects to one Plex, so a second has no working setup.
          const canAdd = !k.singleton || rows.length === 0;
          return (
            <section key={k.value} className="service-section">
              <h3>{k.label}</h3>
              <p className="service-hint">{k.hint}</p>
              <div className="service-grid">
                {rows.map((i) => (
                  <ServiceCard
                    key={i.id}
                    instance={i}
                    onEdit={() => setModal({ kind: i.kind, instance: i })}
                  />
                ))}
                {canAdd && (
                  <button
                    type="button"
                    className="service-add"
                    onClick={() => setModal({ kind: k.value, instance: null })}
                  >
                    <span aria-hidden="true">+</span> Add a {k.label}
                  </button>
                )}
              </div>
            </section>
          );
        })}
      {modal && (
        <ServiceModal
          key={modal.instance ? modal.instance.id : `add-${modal.kind}`}
          kind={modal.kind}
          instance={modal.instance}
          onClose={() => setModal(null)}
          savePendingRef={savePendingRef}
        />
      )}
    </div>
  );
}

// --- Backup ------------------------------------------------------------------

function BackupPanel() {
  const qc = useQueryClient();
  const { data, isPending, isError } = useQuery({
    queryKey: ["backup-info"],
    queryFn: api.backupInfo,
  });
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const download = async () => {
    setError(null);
    setBusy(true);
    try {
      await api.downloadBackup();
      // The server stamps "last backup" as the file goes out; pick it up.
      await qc.invalidateQueries({ queryKey: ["backup-info"] });
    } catch (err) {
      setError(err instanceof Error ? err.message : "Something went wrong.");
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="panel">
      <h2>Backup &amp; Restore</h2>
      <p className="blurb">
        Save everything Reaper knows to one file, and put it all back if you ever need to.
      </p>
      {isPending && <p className="muted">Loading…</p>}
      {isError && (
        <p className="notice notice-error">Couldn't load this page. Reload to try again.</p>
      )}
      {data && (
        <>
          <section className="rules-card">
            <h3>Download a backup</h3>
            <p className="help">
              One file with everything Reaper decided and everything you set up, plus the keys that
              unlock your saved credentials. The rebuildable cache is left out to keep the file
              small.
            </p>
            <dl className="backup-facts">
              <dt>Inside</dt>
              <dd>Decisions, settings, credentials</dd>
              <dt>Last backup</dt>
              <dd>{data.last_backup_at ? since(data.last_backup_at) : "Never"}</dd>
            </dl>
            <div className="backup-actions">
              <button className="primary" onClick={download} disabled={busy}>
                {busy ? "Preparing…" : "Download backup"}
              </button>
            </div>
            {error && <p className="notice notice-error">The download didn't start: {error}</p>}
            {!data.key_in_backup && (
              <p className="notice notice-warn">
                Your encryption key is set through the environment, so it is not inside this backup.
                Keep that key with the file, or a restore cannot read your saved credentials.
              </p>
            )}
            <p className="notice notice-warn">
              This file can unlock your Plex and Sonarr/Radarr credentials. Keep it as safe as a
              password.
            </p>
          </section>

          <RestoreCard armed={data.restore_armed} />
        </>
      )}
    </div>
  );
}

// The restore card: choose a backup file, confirm with the admin password, then restart the
// container to finish. A restore never happens live -- the upload is validated and staged, the
// password arms the swap, and the entrypoint does the swap on the next boot before migrations.
// Three states: idle (choose a file), chosen (a validated summary + password), and armed (a
// restore is staged and waiting for a restart). `armed` is server state (the READY marker), so
// it survives a reload and shows even if this browser never did the confirm.
function RestoreCard({ armed }: { armed: boolean }) {
  const qc = useQueryClient();
  const inputRef = useRef<HTMLInputElement>(null);
  const [summary, setSummary] = useState<RestoreSummary | null>(null);
  const [fileName, setFileName] = useState<string | null>(null);
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [dragging, setDragging] = useState(false);

  const refreshInfo = () => qc.invalidateQueries({ queryKey: ["backup-info"] });

  const reset = () => {
    setSummary(null);
    setFileName(null);
    setPassword("");
    setError(null);
  };

  // The admin password belongs to the summary it was typed against, so it goes whenever that
  // summary does: here, and on a failed confirm below. Dropping the summary alone unmounted
  // the field while the password stayed in state, and refilled it against a different backup
  // once the next file staged (S-5).
  const choose = async (file: File) => {
    setError(null);
    setSummary(null);
    setPassword("");
    setFileName(file.name);
    setBusy(true);
    try {
      setSummary(await api.restorePrepare(file));
    } catch (err) {
      setFileName(null);
      setError(err instanceof Error ? err.message : "That file couldn't be read.");
    } finally {
      setBusy(false);
    }
  };

  const onPick = (e: ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0];
    // Clear the input so choosing the same file name twice still fires a change.
    e.target.value = "";
    if (file) void choose(file);
  };

  const onDrop = (e: DragEvent) => {
    e.preventDefault();
    setDragging(false);
    const file = e.dataTransfer.files?.[0];
    if (file) void choose(file);
  };

  const restore = async () => {
    if (!summary) return;
    setError(null);
    setBusy(true);
    try {
      await api.restoreConfirm(password, summary.token);
      // The confirm armed the swap; refetch so `armed` flips on and this card shows the
      // restart prompt. Drop the staged summary from local state either way.
      reset();
      await refreshInfo();
    } catch (err) {
      setPassword("");
      setError(err instanceof Error ? err.message : "The restore couldn't be confirmed.");
    } finally {
      setBusy(false);
    }
  };

  const cancel = async () => {
    setBusy(true);
    try {
      await api.restoreCancel();
      reset();
      await refreshInfo();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Something went wrong.");
    } finally {
      setBusy(false);
    }
  };

  if (armed) {
    return (
      <section className="rules-card">
        <h3>Restore from a backup</h3>
        <div className="notice notice-warn restore-armed">
          <span>
            <strong>A restore is ready.</strong> Restart Reaper's container to finish. Nothing has
            changed yet.
          </span>
          <button type="button" className="link" onClick={() => void cancel()} disabled={busy}>
            Cancel restore
          </button>
        </div>
      </section>
    );
  }

  return (
    <section className="rules-card">
      <h3>Restore from a backup</h3>
      <p className="help">
        Replace everything here with a saved backup. Deletion stays off after a restore until you
        turn it back on.
      </p>

      <input ref={inputRef} type="file" accept=".reaper" hidden onChange={onPick} />

      {!summary && (
        <div
          className={`dropzone${dragging ? " dropzone-over" : ""}`}
          onDragOver={(e) => {
            e.preventDefault();
            setDragging(true);
          }}
          onDragLeave={() => setDragging(false)}
          onDrop={onDrop}
        >
          {busy && fileName ? (
            <div className="muted">Checking {fileName}…</div>
          ) : (
            <>
              <div>
                Drop a backup file here, or{" "}
                <button
                  type="button"
                  className="link"
                  onClick={() => inputRef.current?.click()}
                  disabled={busy}
                >
                  choose one
                </button>
                .
              </div>
              <div className="dz-hint">Reaper backup file (.reaper)</div>
            </>
          )}
        </div>
      )}

      {summary && (
        <>
          <div className="chosen">
            <div className="chosen-head">
              <span className="chosen-file">{fileName}</span>
              <button
                type="button"
                className="link chosen-x"
                onClick={() => void cancel()}
                disabled={busy}
              >
                Remove
              </button>
            </div>
            <div className="chosen-body">
              <div className="kv2">
                <span>From</span>
                <span>
                  {summary.app_version ? `Reaper ${summary.app_version}` : "an earlier setup"}
                  {summary.created_at ? `, saved ${since(summary.created_at)}` : ""}
                </span>
              </div>
              <div className="kv2">
                <span>Inside</span>
                <span>Decisions, settings, credentials</span>
              </div>
              <div className="verdict-ok">
                <span className="dot">✓</span>
                {summary.verdict === "current"
                  ? "Matches this server. Safe to restore."
                  : "Saved on an older version. Reaper will update it when it restarts."}
              </div>
            </div>
          </div>

          {!summary.key_in_backup && (
            <p className="notice notice-warn">
              This backup doesn't include the encryption key. Set REAPER_SECRET_KEY on this server
              to the value it was saved with, or your saved credentials can't be read after the
              restore.
            </p>
          )}

          <label className="field-sm restore-pw">
            <span className="field-label">Admin password</span>
            <input
              type="password"
              value={password}
              onChange={(e) => {
                setError(null);
                setPassword(e.target.value);
              }}
              autoComplete="current-password"
              maxLength={128}
              placeholder="Confirm to restore"
            />
          </label>

          <div className="backup-actions">
            <button
              type="button"
              className="danger"
              onClick={() => void restore()}
              disabled={busy || !password}
            >
              {busy ? "Restoring…" : "Restore"}
            </button>
          </div>
          <p className="help restore-when">
            Restoring takes effect the next time the container restarts. Nothing changes until then.
          </p>

          <p className="notice notice-warn">
            Restoring replaces your current decisions and settings. There is no undo, so download a
            backup first.
          </p>
        </>
      )}

      {error && <p className="notice notice-error">The restore didn't start: {error}</p>}
    </section>
  );
}

// --- About -------------------------------------------------------------------

function AboutPanel() {
  const { data, isPending, isError } = useQuery({ queryKey: ["about"], queryFn: api.about });

  return (
    <div className="panel">
      <h2>About</h2>
      <p className="blurb">What's running, and where its data lives.</p>
      {isPending && <p className="muted">Loading…</p>}
      {isError && (
        <p className="notice notice-error">Couldn't load this page. Reload to try again.</p>
      )}
      {data && (
        <div className="set-rows">
          <dl className="about-kv">
            <dt>Version</dt>
            <dd>Reaper {data.version}</dd>
            <dt>License</dt>
            <dd>{data.license}</dd>
            <dt>Data folder</dt>
            <dd>
              <code>{data.data_dir}</code>
            </dd>
            <dt>Reaper's own data</dt>
            <dd>{bytes(data.reaper_db_bytes)} · decisions, audit trail, credentials</dd>
            <dt>Rebuildable cache</dt>
            <dd>{bytes(data.cache_db_bytes)} · watch history, ratings, lists</dd>
          </dl>
        </div>
      )}
    </div>
  );
}

// --- Jobs ------------------------------------------------------------------

const SCAN_ID = "scheduled_scan";

interface JobMeta {
  title: string;
  desc: string;
  /** The schedule editor's intro; falls back to `desc`. */
  modalDesc?: string;
  /** Shown in the editor while the job is being turned off. */
  offWarning?: string;
}

// The display copy for every job, in one place, so the row and its editor never drift.
const JOB_META: Record<string, JobMeta> = {
  [SCAN_ID]: {
    title: "Update library and apply policy",
    desc: "Checks what changed since the last scan and re-scores it against your policy. A quick pass, not a full re-read. It only reads, and can't remove a thing.",
    modalDesc:
      "Reaper can scan on its own to keep the queue fresh. It still only reads. You approve every deletion by hand.",
  },
  refresh_ratings: {
    title: "Refresh IMDb ratings",
    desc: "Downloads the latest IMDb ratings so scores use current numbers.",
    offWarning:
      "With this off, ratings won't refresh on a schedule. Reaper still refreshes them once at startup if they're over two weeks old, because past that a scan comes back incomplete and can't remove anything until they're refreshed.",
  },
  refresh_curated_lists: {
    title: "Refresh curated lists",
    desc: "Re-pulls the protection lists Reaper ships with, like the IMDb Top 250, so nothing on them gets flagged.",
    offWarning:
      "This only affects the standalone daily refresh. Every scan already re-pulls these lists on its own, and they're only used during a scan, so turning this off changes little for anyone who scans.",
  },
  full_history_sweep: {
    title: "Full watch-history update",
    desc: 'Re-reads your whole watch history, not just new plays, so imported or backdated views still count and a wiped history is caught before "never watched" turns wrong.',
    offWarning:
      "With this off, Reaper stops re-reading your full history. Imported or backdated plays won't be counted, and a wiped history won't be caught, so \"never watched\" can drift out of date.",
  },
};

/** The copy for a job id. Every scheduled job has an entry; the fallback only exists so the
 *  lookup is total for the type checker. */
function jobMeta(id: string): JobMeta {
  return JOB_META[id] ?? { title: id, desc: "" };
}

const SCAN_PRESETS: { label: string; cron: string | null }[] = [
  { label: "Off (scan by hand)", cron: null },
  { label: "Every night at 2 AM", cron: "0 2 * * *" },
  { label: "Every Sunday at 3 AM", cron: "0 3 * * 0" },
  { label: "First of the month, 3 AM", cron: "0 3 1 * *" },
];

/** The upkeep presets. "Every day" carries the job's own default time (staggered off peak),
 *  so choosing it keeps the natural setting exactly what it was. */
function maintenancePresets(defaultCron: string): { label: string; cron: string | null }[] {
  return [
    { label: "Off (don't run)", cron: null },
    { label: "Every day", cron: defaultCron },
    { label: "Every 12 hours", cron: "0 */12 * * *" },
    { label: "Every 6 hours", cron: "0 */6 * * *" },
    { label: "Every hour", cron: "0 * * * *" },
  ];
}

/** Picker sentinels that are not cron lines: "off" and "type your own". */
const OFF_VALUE = "__off__";
const CUSTOM_VALUE = "__custom__";

function whenText(iso: string | null): string {
  if (!iso) return "not scheduled";
  const ms = new Date(iso).getTime() - Date.now();
  if (ms <= 0) return "any moment";
  const mins = Math.round(ms / 60000);
  if (mins < 60) return `in ${mins} min`;
  const hours = Math.round(mins / 60);
  if (hours < 24) return `in ${hours} hr`;
  return new Date(iso).toLocaleString();
}

const WEEKDAYS = ["Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"];

function clockLabel(hour: number, minute: number): string {
  const period = hour < 12 ? "AM" : "PM";
  const hour12 = hour % 12 === 0 ? 12 : hour % 12;
  return `${hour12}:${String(minute).padStart(2, "0")} ${period}`;
}

function ordinal(n: number): string {
  const tens = n % 100;
  if (tens >= 11 && tens <= 13) return `${n}th`;
  const ones = n % 10;
  return `${n}${ones === 1 ? "st" : ones === 2 ? "nd" : ones === 3 ? "rd" : "th"}`;
}

/** A cron line in plain words, for the shapes the presets and defaults produce. Anything
 *  outside those reads as its raw line rather than a confident wrong guess. */
function describeCron(cron: string): string {
  const parts = cron.trim().split(/\s+/);
  if (parts.length !== 5) return `Custom (${cron})`;
  const [m = "", h = "", dom = "", mon = "", dow = ""] = parts;
  const numeric = (x: string) => /^\d+$/.test(x);
  const everyDay = dom === "*" && mon === "*" && dow === "*";

  const hourStep = /^\*\/(\d+)$/.exec(h);
  if (numeric(m) && hourStep && everyDay) return `Every ${hourStep[1]} hours`;
  if (numeric(m) && h === "*" && everyDay) return "Every hour";
  if (!numeric(m) || !numeric(h)) return `Custom (${cron})`;

  const at = clockLabel(Number(h), Number(m));
  if (everyDay) return `Every day at ${at}`;
  if (dom === "*" && mon === "*" && numeric(dow)) {
    return `Every ${WEEKDAYS[Number(dow) % 7]} at ${at}`;
  }
  if (numeric(dom) && mon === "*" && dow === "*") {
    return `Monthly on the ${ordinal(Number(dom))} at ${at}`;
  }
  return `Custom (${cron})`;
}

function scanScheduleText(job: ScheduledJob | undefined, failed: boolean): string {
  // A failed load is not "still checking": say so, so the row doesn't claim to be checking
  // forever after the schedule query errored (U-6).
  if (failed) return "Couldn't check the schedule.";
  if (!job) return "Automatic scan: checking…";
  if (job.cron === null) return "Automatic scan is off. It runs when you ask.";
  return `Automatic scan: ${describeCron(job.cron)} · next ${whenText(job.next_run_at)}`;
}

function maintenanceScheduleText(job: ScheduledJob): string {
  if (job.cron === null) return "Off. Run it by hand.";
  return `${describeCron(job.cron)} · next ${whenText(job.next_run_at)}`;
}

/** The one schedule editor, for the scan and every upkeep job. Presets plus "off" plus a
 *  cron line of your own; turning an upkeep job off carries a plain warning of what stops. */
function ScheduleModal({
  job,
  onClose,
  savePendingRef,
}: {
  job: ScheduledJob;
  onClose: () => void;
  // Set by JobsPanel so its Back guard can read the same canClose the scrim/Escape/✕ use (B-11).
  savePendingRef?: RefObject<boolean>;
}) {
  const queryClient = useQueryClient();
  // The effective server time zone every timed job runs on, so the help names the real zone
  // instead of guessing "UTC in Docker" (U-1, rule 86). Shares GeneralPanel's cache.
  const zone = useQuery({ queryKey: ["general-settings"], queryFn: api.general }).data?.timezone;
  const meta = jobMeta(job.id);
  const presets =
    job.id === SCAN_ID ? SCAN_PRESETS : maintenancePresets(job.default_cron ?? "0 4 * * *");
  const isKnownPreset = presets.some((p) => p.cron !== null && p.cron === job.cron);

  const [choice, setChoice] = useState<string>(
    job.cron === null ? OFF_VALUE : isKnownPreset ? job.cron : CUSTOM_VALUE,
  );
  const [custom, setCustom] = useState(job.cron && !isKnownPreset ? job.cron : "");
  const [error, setError] = useState<string | null>(null);

  const save = useMutation({
    mutationFn: (cron: string | null) => api.saveJobSchedule(job.id, cron),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["schedule"] });
      onClose();
    },
    onError: (e: Error) => setError(e.message),
  });

  // Mirror the save's pending state up to JobsPanel's Back guard, and clear it on unmount so a
  // stale true never lingers after the modal closes (B-11).
  useEffect(() => {
    if (savePendingRef) savePendingRef.current = save.isPending;
    return () => {
      if (savePendingRef) savePendingRef.current = false;
    };
  }, [save.isPending, savePendingRef]);

  const chosenCron =
    choice === OFF_VALUE ? null : choice === CUSTOM_VALUE ? custom.trim() || null : choice;
  const turningOff = chosenCron === null;
  const saveDisabled = save.isPending || (choice === CUSTOM_VALUE && custom.trim() === "");

  return (
    <ModalShell title={meta.title} onClose={onClose} canClose={!save.isPending}>
      <div className="service-form">
        <p className="help">{meta.modalDesc ?? meta.desc}</p>

        <label className="field-sm">
          <span className="field-label">How often</span>
          <select
            value={choice}
            aria-label="How often"
            disabled={save.isPending}
            onChange={(e) => setChoice(e.target.value)}
          >
            {presets.map((p) => (
              <option key={p.label} value={p.cron ?? OFF_VALUE}>
                {p.label}
              </option>
            ))}
            <option value={CUSTOM_VALUE}>Your own schedule…</option>
          </select>
          {job.default_cron && (
            <span className="help">
              Default: {describeCron(job.default_cron)}. You can Run now anytime.
            </span>
          )}
          {/* The clock times above run on the server's configured time zone, not this browser's.
              Name the real zone so "2 AM" is not read as local time, and the operator is not left
              to guess (U-1, rule 86). Falls back to the generic phrasing only while it loads. */}
          <span className="help">
            {zone
              ? `Times use your server time zone: ${zone}. Change it in Settings, General.`
              : "Times use your server time zone. Change it in Settings, General."}
          </span>
        </label>

        {choice === CUSTOM_VALUE && (
          <label className="field-sm">
            <span className="field-label">Your own schedule</span>
            <input
              type="text"
              value={custom}
              placeholder="30 4 * * *"
              aria-label="Your own schedule"
              onChange={(e) => setCustom(e.target.value)}
            />
            <span className="help">
              A cron line, for when none of the presets fit. 30 4 * * * runs at 4:30 AM every day.
            </span>
          </label>
        )}

        {turningOff && meta.offWarning && <p className="notice notice-warn">{meta.offWarning}</p>}
        {error && <p className="notice notice-error">{error}</p>}

        <div className="add-actions">
          <span className="flex-spacer" />
          <button className="ghost" onClick={onClose} disabled={save.isPending}>
            Cancel
          </button>
          <button
            className="primary"
            onClick={() => save.mutate(chosenCron)}
            disabled={saveDisabled}
          >
            {save.isPending ? "Saving…" : "Save"}
          </button>
        </div>
      </div>
    </ModalShell>
  );
}

/** One upkeep job: what it is, when it runs, and Edit + Run now. It shows an honest
 *  "running now" while it works; none of these can delete anything. */
function JobRow({ job, onEdit }: { job: ScheduledJob; onEdit: () => void }) {
  const queryClient = useQueryClient();
  const meta = jobMeta(job.id);
  const run = useMutation({
    mutationFn: () => api.runJob(job.id),
    onSuccess: () => {
      // Optimistically mark the job running so the spinner shows at once and the finish is
      // seen as a running->done transition (the flash) even for a job that completes inside
      // one poll interval. The real state, and the fresh last-run fields, land on the next
      // poll: the schedule query's own refetchInterval reacts to this optimistic flag right
      // away (nothing here needs to force an earlier refetch), so there is no fixed delay to
      // race against a scheduler that is slow to submit the job.
      queryClient.setQueryData<Schedule>(["schedule"], (prev) =>
        prev
          ? { ...prev, jobs: prev.jobs.map((j) => (j.id === job.id ? { ...j, running: true } : j)) }
          : prev,
      );
    },
  });
  const running = job.running || run.isPending;
  // The flash keys on the server's own running flag (which the mutation seeds optimistically),
  // never on `run.isPending` -- that would fire a stale flash the instant the POST returns,
  // before the job has even run. Compared with `!== null`, not truthiness: an empty (but
  // present) result must still flash, unlike a job that has simply never run.
  const flash = useJobFlash(
    job.running,
    job.last_result !== null ? { ok: job.last_ok !== false, text: job.last_result } : null,
  );

  return (
    <div className="jobrow">
      <div className="jobrow-main">
        <div className="jobrow-title">{meta.title}</div>
        <div className="jobrow-desc">{meta.desc}</div>
        <JobStatus
          running={running}
          runningLabel="Running now…"
          lastRunAt={job.last_run_at}
          lastOk={job.last_ok}
          lastResult={job.last_result}
          flash={flash}
        />
        <div className="jobrow-sched">{maintenanceScheduleText(job)}</div>
        {run.error && (
          <p className="notice notice-error notice-inline">
            The job didn't start: {run.error.message}
          </p>
        )}
      </div>
      <div className="jobrow-actions">
        <span className="slot-edit">
          <button className="ghost" onClick={onEdit}>
            Edit
          </button>
        </span>
        <span className="slot-act">
          <button className="primary" onClick={() => run.mutate()} disabled={running}>
            {running ? "Running…" : "Run now"}
          </button>
        </span>
      </div>
    </div>
  );
}

/** The Leaving Soon shelf update, moved here from Plex settings. Its on/off toggle still
 *  lives on the Plex tab, so this row links there; when off, it grays out and can't run. */
function LeavingSoonRow({ onGoToPlex }: { onGoToPlex: () => void }) {
  const queryClient = useQueryClient();
  const ls = useQuery({ queryKey: ["leaving-soon-settings"], queryFn: api.leavingSoonSettings });
  const runSync = useMutation({
    mutationFn: api.syncLeavingSoon,
    onSuccess: () => void queryClient.invalidateQueries({ queryKey: ["leaving-soon-settings"] }),
  });
  // The manual-run confirmation for this row: the sync is a synchronous mutation, so its
  // result is read straight off the mutation when it settles (unlike the polled upkeep jobs).
  // Called before the early returns below, so the hook order never changes.
  const syncResult = runSync.data
    ? {
        ok: runSync.data.problems.length === 0,
        // A real per-library problem always wins the message, even in preview: "preview
        // only" is a benign caveat, but it must never mask an actual failure that happened
        // in the same pass.
        text:
          runSync.data.problems.length > 0
            ? "Some shelves didn't update"
            : !runSync.data.applied
              ? "Preview only, nothing written"
              : `${count(runSync.data.added_count)} added, ${count(runSync.data.cleared_count)} cleared`,
      }
    : runSync.error
      ? { ok: false, text: "It didn't update" }
      : null;
  const flash = useJobFlash(runSync.isPending, syncResult);

  const title = "Update Leaving Soon shelf";
  const desc =
    'Pushes the current countdown set to the Plex "Leaving Soon" shelf, so people get a heads-up before anything goes.';

  if (ls.isPending) {
    return (
      <div className="jobrow">
        <div className="jobrow-main">
          <div className="jobrow-title">{title}</div>
          <div className="jobrow-desc">{desc}</div>
          <div className="jobrow-sched">Loading…</div>
        </div>
      </div>
    );
  }
  if (ls.isError || !ls.data) {
    return (
      <div className="jobrow">
        <div className="jobrow-main">
          <div className="jobrow-title">{title}</div>
          <div className="jobrow-desc">{desc}</div>
          <p className="notice notice-error notice-inline">
            Couldn't load the shelf status. Reload to try again.
          </p>
        </div>
      </div>
    );
  }

  const { enabled, last } = ls.data;

  if (!enabled) {
    return (
      <div className="jobrow dimmed">
        <div className="jobrow-main">
          <div className="jobrow-title">{title}</div>
          <div className="jobrow-desc">{desc}</div>
          <div className="jobrow-sched">
            Off.{" "}
            <button className="link" onClick={onGoToPlex}>
              Turn it on in Plex → Leaving Soon
            </button>
          </div>
        </div>
        <div className="jobrow-actions">
          <span className="slot-edit" />
          <span className="slot-act">
            <button className="primary" disabled>
              Update now
            </button>
          </span>
        </div>
      </div>
    );
  }

  const running = runSync.isPending;
  return (
    <div className="jobrow">
      <div className="jobrow-main">
        <div className="jobrow-title">{title}</div>
        <div className="jobrow-desc">{desc}</div>
        <JobStatus
          running={running}
          runningLabel="Updating…"
          lastRunAt={last?.at ?? null}
          lastOk={last ? last.ok : null}
          lastResult={last?.result ?? null}
          flash={flash}
        />
        {last && (
          <div className="jobrow-meta">
            <strong>{count(last.movies)}</strong> movie{last.movies === 1 ? "" : "s"} and{" "}
            <strong>{count(last.seasons)}</strong> season{last.seasons === 1 ? "" : "s"} on the
            shelves
          </div>
        )}
        <div className="jobrow-sched">Runs after every scan</div>
        <div className="jobrow-link">
          <button className="link" onClick={onGoToPlex}>
            Manage in Plex → Leaving Soon
          </button>
        </div>
        {runSync.error && (
          <p className="notice notice-error notice-inline">
            The shelves didn't update: {runSync.error.message}
          </p>
        )}
      </div>
      <div className="jobrow-actions">
        <span className="slot-edit" />
        <span className="slot-act">
          <button className="primary" onClick={() => runSync.mutate()} disabled={running}>
            {running ? "Updating…" : "Update now"}
          </button>
        </span>
      </div>
    </div>
  );
}

function JobsPanel({ onGoToPlex }: { onGoToPlex: () => void }) {
  const { data: snapshot } = useQuery({
    queryKey: ["snapshot"],
    queryFn: api.latestSnapshot,
    retry: false,
  });
  const schedule = useQuery({
    queryKey: ["schedule"],
    queryFn: api.schedule,
    // Poll only while something is actually running, so the "running now" states and the
    // next-run lines stay live without hammering the endpoint the rest of the time.
    refetchInterval: (query) => (query.state.data?.jobs.some((j) => j.running) ? 1500 : false),
  });
  const [editing, setEditing] = useState<ScheduledJob | null>(null);
  // The modal's save lives inside ScheduleModal; it mirrors its pending state here so the Back
  // guard can refuse a close mid-save exactly as the scrim/Escape/✕ do (canClose={!save.isPending}
  // below). Without this, Back would tear the modal down while the save is in flight, dropping
  // the error it would have shown (B-11, rule 80).
  const savePendingRef = useRef(false);
  // Back closes the schedule editor instead of leaving Reaper -- unless a save is in flight.
  useBackGuard(
    editing !== null,
    () => setEditing(null),
    () => !savePendingRef.current,
  );

  const jobsById = new Map<string, ScheduledJob>((schedule.data?.jobs ?? []).map((j) => [j.id, j]));
  const scanJob = jobsById.get(SCAN_ID);

  return (
    <div className="panel">
      <h2>Jobs</h2>
      <p className="blurb">
        Everything Reaper runs on a timer lives here, and you can run any of it now without waiting.
        None of these can delete a thing. A scan just refreshes the review queue; the rest is
        upkeep.
      </p>

      <div className="set-rows">
        <ScanRow
          snapshot={snapshot}
          scanJob={scanJob}
          title={jobMeta(SCAN_ID).title}
          desc={jobMeta(SCAN_ID).desc}
          scheduleText={scanScheduleText(scanJob, schedule.isError)}
          onEdit={() => scanJob && setEditing(scanJob)}
          canEdit={!!scanJob}
        />
        <LeavingSoonRow onGoToPlex={onGoToPlex} />
        {/* Render the upkeep jobs from the server's own list (scan aside; it has its own
            row), in its order, so a job added server-side appears here without a frontend
            edit. jobMeta falls back to the raw id for a job with no copy yet. */}
        {(schedule.data?.jobs ?? [])
          .filter((job) => job.id !== SCAN_ID)
          .map((job) => (
            <JobRow key={job.id} job={job} onEdit={() => setEditing(job)} />
          ))}
      </div>

      {schedule.isPending && <p className="muted">Loading the upkeep jobs…</p>}
      {schedule.isError && (
        <p className="notice notice-error">Couldn't load the upkeep jobs. Reload to try again.</p>
      )}

      {editing && (
        <ScheduleModal
          job={editing}
          onClose={() => setEditing(null)}
          savePendingRef={savePendingRef}
        />
      )}
    </div>
  );
}

// --- Notifications ---------------------------------------------------------

/** Client-side twin of the server's webhook validation (reaper/api/settings.py). The token
 *  lives in the URL path, so a typo'd host would leak it to a stranger -- we only accept an
 *  https URL whose host is Discord's webhook endpoint (subdomains like ptb./canary. count)
 *  and whose path is a real /api/webhooks/ path. The server checks the same thing; this just
 *  spares a round-trip and gives an instant hint. */
const DISCORD_WEBHOOK_HOSTS = ["discord.com", "discordapp.com"];

function isDiscordWebhook(raw: string): boolean {
  let url: URL;
  try {
    url = new URL(raw.trim());
  } catch {
    return false;
  }
  if (url.protocol !== "https:") return false;
  const host = url.hostname.toLowerCase();
  const okHost =
    DISCORD_WEBHOOK_HOSTS.includes(host) ||
    DISCORD_WEBHOOK_HOSTS.some((h) => host.endsWith("." + h));
  return okHost && url.pathname.startsWith("/api/webhooks/");
}

/** The Discord webhook is the only channel that actually warns the household before a title
 *  is deleted -- the Plex "Leaving Soon" label only reaches people who pinned the library. It
 *  is a write-only secret: the URL is sent once, encrypted on arrival, and never comes back,
 *  so the field is always blank and we report only *whether* a webhook is connected. Same
 *  pattern as an instance API key. */
function NotificationsPanel({
  /** Called whenever the webhook box gains or loses a draft, so the section rail can hold a
   *  switch that would discard one. Pass a STABLE function: it is an effect dependency. */
  onDirtyChange,
}: {
  onDirtyChange?: ((dirty: boolean) => void) | undefined;
} = {}) {
  const queryClient = useQueryClient();
  const { data, isPending, isError } = useQuery({
    queryKey: ["notifications"],
    queryFn: api.notifications,
  });
  const [url, setUrl] = useState("");
  const [test, setTest] = useState<InstanceTest | null>(null);
  const [error, setError] = useState<string | null>(null);

  const invalidate = () => void queryClient.invalidateQueries({ queryKey: ["notifications"] });

  const save = useMutation({
    mutationFn: () => api.setWebhook(url.trim()),
    onSuccess: () => {
      setUrl("");
      setTest(null);
      setError(null);
      invalidate();
    },
    onError: (e: Error) => setError(e.message),
  });
  const testWebhook = useMutation({
    // Test the URL typed in the box (the one about to be saved) if there is one; otherwise
    // test the already-stored webhook, so a saved channel can be verified without re-pasting.
    mutationFn: () => api.testWebhook(url.trim() ? url.trim() : null),
    onSuccess: setTest,
    onError: (e: Error) => setError(e.message),
  });
  const remove = useMutation({
    mutationFn: () => api.clearWebhook(),
    onSuccess: () => {
      setUrl("");
      setTest(null);
      setError(null);
      invalidate();
    },
    onError: (e: Error) => setError(e.message),
  });

  const connected = data?.has_webhook ?? false;
  const typed = url.trim().length > 0;
  const validNew = typed && isDiscordWebhook(url);
  const badFormat = typed && !validNew;
  // Test either the freshly typed URL (must be valid) or, when the box is empty, the stored one.
  const canTest = (validNew || (!typed && connected)) && !testWebhook.isPending;

  // What this panel would LOSE, reported up to `Settings` so leaving the section can stop and ask
  // first. A pasted webhook is the costliest draft in Settings to drop: it is a secret, it is
  // never shown again once stored, and re-typing it means going back to Discord for it.
  //
  // Rule 146 asks two things of this signal, and here they are the same fact. There is something
  // to lose exactly when the box holds text, and the box is reachable in EVERY state this panel
  // renders: it has no early return, and the loading and failed-check branches above swap only
  // the one status line over it, never the box, its help, or its Save. `typed` rather than
  // `validNew`, because a half-pasted URL that Save refuses is still a draft that leaving throws
  // away -- reporting only the saveable form would drop the malformed one silently.
  useEffect(() => {
    onDirtyChange?.(typed);
  }, [typed, onDirtyChange]);
  useEffect(() => () => onDirtyChange?.(false), [onDirtyChange]);

  return (
    <div className="panel">
      <h2>Notifications</h2>
      <p className="blurb">
        A Discord webhook is how Reaper warns your household before anything is deleted: while a
        title is in its grace period it posts a "leaving soon" heads-up here, so someone can watch
        it or spare it in time. It's optional, but it's the one warning that reaches people who
        don't watch the Plex "Leaving Soon" shelf.
      </p>

      {/* Whether the warning channel exists is only worth stating once it has been read:
          an unread answer must not claim that nobody is being warned. */}
      {isPending ? (
        <p className="muted">Checking whether Discord is connected…</p>
      ) : isError ? (
        <p className="notice notice-error">
          Couldn't check whether Discord is connected. Reload to try again.
        </p>
      ) : connected ? (
        <p className="muted">✓ Discord connected. Leaving-soon warnings post to your channel.</p>
      ) : (
        <p className="muted">No Discord webhook set, so leaving-soon warnings won't be sent.</p>
      )}

      <div className="add-grid">
        <label className="field-sm wide">
          <span className="field-label">Discord webhook URL</span>
          <input
            type="password"
            value={url}
            onChange={(e) => {
              setUrl(e.target.value);
              setError(null);
            }}
            placeholder={
              connected
                ? "leave blank to keep the current webhook"
                : "https://discord.com/api/webhooks/…"
            }
            autoComplete="off"
          />
        </label>
      </div>
      <p className="help">
        In Discord: Channel settings → Integrations → Webhooks → New Webhook → Copy Webhook URL.
        It's a secret. Once saved it's encrypted and never shown again.
      </p>

      <div className="add-actions">
        <button
          type="button"
          className="primary"
          disabled={!validNew || save.isPending}
          onClick={() => {
            setError(null);
            save.mutate();
          }}
        >
          {save.isPending ? "Saving…" : "Save"}
        </button>
        <button
          type="button"
          className="ghost"
          disabled={!canTest}
          onClick={() => {
            setError(null);
            setTest(null);
            testWebhook.mutate();
          }}
        >
          {testWebhook.isPending ? "Testing…" : "Send test message"}
        </button>
        {connected && (
          <button
            type="button"
            className="ghost danger"
            disabled={remove.isPending}
            onClick={() => {
              setError(null);
              remove.mutate();
            }}
          >
            {remove.isPending ? "Removing…" : "Remove"}
          </button>
        )}
        <TestBadge result={test} />
      </div>
      {badFormat && (
        <p className="notice notice-error">
          That doesn't look like a Discord webhook URL. Paste the full
          https://discord.com/api/webhooks/… URL from the channel's integration settings.
        </p>
      )}
      {error && <p className="notice notice-error">{error}</p>}
    </div>
  );
}

// --- Safety ----------------------------------------------------------------

// The same floor the server applies (MIN_PASSWORD_LENGTH in
// reaper/services/admin_password.py), so the placeholder, the live message, and the server
// rule all state one number.
const MIN_ADMIN_PASSWORD = 12;

function AdminPasswordForm({ needed }: { needed: boolean }) {
  const queryClient = useQueryClient();
  const [current, setCurrent] = useState("");
  const [pw, setPw] = useState("");
  const [confirm, setConfirm] = useState("");
  const [msg, setMsg] = useState<string | null>(null);
  const save = useMutation({
    // Only the new password is sent: the confirm field exists so a typo can't lock the
    // operator out of the key that arms deletion, and it never leaves the browser.
    mutationFn: () => api.setAdminPassword(pw, needed ? undefined : current),
    onSuccess: () => {
      setCurrent("");
      setPw("");
      setConfirm("");
      setMsg("Password saved.");
      void queryClient.invalidateQueries({ queryKey: ["safety"] });
    },
    // No onError: a failure renders from `save.error` as an error notice, never in `msg`.
    // This password is what confirms turning deletion on, so "saved" and "wrong password"
    // must not look alike here.
  });

  const tooShort = pw.length > 0 && pw.length < MIN_ADMIN_PASSWORD;
  const mismatch = confirm.length > 0 && confirm !== pw;
  const needCurrent = !needed && current.length === 0;
  const valid =
    pw.length >= MIN_ADMIN_PASSWORD && confirm.length > 0 && confirm === pw && !needCurrent;

  // One red error region under the row. Live validation (too short, then mismatch) explains
  // why Save is off while typing; a failed submit reuses the same box. Validation wins over a
  // stale submit error so the operator sees the thing they can fix right now.
  const errorNode: ReactNode = tooShort ? (
    <>
      Use at least {MIN_ADMIN_PASSWORD} characters. <b>{pw.length} so far.</b>
    </>
  ) : mismatch ? (
    "The passwords don't match."
  ) : save.error ? (
    needed ? (
      `The password wasn't set: ${save.error.message}`
    ) : (
      `The password wasn't changed: ${save.error.message}`
    )
  ) : null;

  // Typing clears the "saved" note and any stale failure, so neither lingers over a form the
  // operator is now re-editing.
  const onEdit = (set: (v: string) => void) => (e: ChangeEvent<HTMLInputElement>) => {
    setMsg(null);
    if (save.isError) save.reset();
    set(e.target.value);
  };

  return (
    <div className="safety-row pw-row">
      <div className="pw-head">
        <strong>{needed ? "Set an admin password" : "Change the admin password"}</strong>
        <p className="help">
          {needed
            ? "Choose something long, and keep it somewhere safe."
            : "Changing it needs the current password first."}
        </p>
      </div>
      <div className="pw-col">
        <form
          className="pw-form"
          onSubmit={(e) => {
            e.preventDefault();
            setMsg(null);
            save.mutate();
          }}
        >
          {/* The current password proves who you are; a divider sets it apart from the new
              one below. First-time setup has no current password, so neither is shown. */}
          {!needed && (
            <>
              <label className="field-sm">
                <span className="field-label">Current password</span>
                <input
                  type="password"
                  value={current}
                  onChange={onEdit(setCurrent)}
                  autoComplete="current-password"
                  maxLength={128}
                />
              </label>
              <hr className="pw-sep" />
            </>
          )}
          <label className="field-sm">
            <span className="field-label">New password</span>
            {/* The placeholder states the length up front; the label names the field. The
                cap is the server's own, so a long pasted passphrase is stopped in the box
                rather than coming back as a validator's sentence. */}
            <input
              type="password"
              value={pw}
              onChange={onEdit(setPw)}
              placeholder="at least 12 characters"
              autoComplete="new-password"
              maxLength={128}
            />
          </label>
          <label className="field-sm">
            <span className="field-label">Confirm new password</span>
            <input
              type="password"
              value={confirm}
              onChange={onEdit(setConfirm)}
              autoComplete="new-password"
              maxLength={128}
            />
          </label>
          <div className="add-actions">
            <button type="submit" className="primary" disabled={!valid || save.isPending}>
              Save
            </button>
            {msg && <span className="muted">{msg}</span>}
          </div>
        </form>
        {errorNode && <p className="notice notice-error">{errorNode}</p>}
      </div>
    </div>
  );
}

export function SecurityPanel() {
  const { data, isLoading, isError } = useSafety();

  if (isLoading) {
    return (
      <div className="panel">
        <h2>Security</h2>
        <p className="muted">Loading…</p>
      </div>
    );
  }
  if (isError || !data) {
    return (
      <div className="panel">
        <h2>Security</h2>
        <p className="notice notice-error">Couldn't load these settings. Reload to try again.</p>
      </div>
    );
  }

  return (
    <div className="panel">
      <h2>Security</h2>
      <p className="blurb">
        The admin password. It confirms turning deletion on (in <strong>Policy → Deletion</strong>),
        and it's also how you sign in without Plex.
      </p>

      <AdminPasswordForm needed={!data.has_password} />
    </div>
  );
}

// --- shell -----------------------------------------------------------------

export function Settings({ initialPanel }: { initialPanel?: Panel | undefined }) {
  const [panel, setPanel] = useState<Panel>(initialPanel ?? "general");
  // General's save bar can hold six unsaved fields at once, and switching section unmounts the
  // panel holding them. So the switch waits for a yes, the same two-step confirm the policy
  // editor's Movies/TV switch uses and in the same place: directly under the control that was
  // clicked, so that control does not move under the pointer.
  //
  // Three panels report, because three panels hold a typed draft: General's save bar, Plex's web
  // address and manual connection rows, and the Discord webhook URL -- which is a secret the
  // operator has to go back to Discord to re-copy. The guard first landed on General alone, so
  // the other two still unmounted silently while the app had just trained the operator to expect
  // to be asked (rule 72). Each panel reports through its own `onDirtyChange`; the three are
  // `useState` setters and so are stable, which that prop requires. A panel with no draft to
  // lose is absent from this record and reads undefined, which switches straight through.
  const [generalDirty, setGeneralDirty] = useState(false);
  const [plexDirty, setPlexDirty] = useState(false);
  const [webhookDirty, setWebhookDirty] = useState(false);
  const [pendingSwitch, setPendingSwitch] = useState<Panel | null>(null);

  const dirtyPanels: Partial<Record<Panel, boolean>> = {
    general: generalDirty,
    plex: plexDirty,
    notifications: webhookDirty,
  };
  const leavingDirty = dirtyPanels[panel] ?? false;

  // The notice exists only because there are edits to lose, so it goes when they do -- by
  // Discard, or by a Save that stores them. Keyed on the draft rather than on the Discard
  // handler so the save path is covered too, which is the bug `PolicyEditor` fixed in its own
  // copy of this: it kept warning about changes that no longer existed.
  useEffect(() => {
    if (!leavingDirty) setPendingSwitch(null);
  }, [leavingDirty]);

  const switchPanel = (next: Panel) => {
    if (next === panel) return;
    if (leavingDirty) {
      setPendingSwitch(next);
      return;
    }
    setPendingSwitch(null);
    setPanel(next);
  };
  const pendingLabel = PANELS.find((p) => p.id === pendingSwitch)?.label ?? "";
  // The section being LEFT, so one string serves all three panels that can raise this.
  const leavingLabel = PANELS.find((p) => p.id === panel)?.label ?? "";
  // Nine labels stop fitting one line well above this, but the app already has exactly one
  // definition of a narrow screen and a second would be worse than swapping a little early:
  // below this width the section rail is a bottom bar, so a compact settings header is the
  // same shape. Rendered as one or the other, never both hidden by CSS, so only the control
  // in use is in the accessibility tree. jsdom has no matchMedia, so a test sees the rail.
  const narrow = useMediaQuery(NARROW_SCREEN_QUERY);
  return (
    <div className="settings">
      {narrow ? (
        <nav className="settings-picker" aria-label="Settings sections">
          <select
            value={panel}
            aria-label="Settings section"
            onChange={(e) => switchPanel(e.target.value as Panel)}
          >
            {PANELS.map((p) => (
              <option key={p.id} value={p.id}>
                {p.label}
              </option>
            ))}
          </select>
        </nav>
      ) : (
        <nav className="settings-nav" aria-label="Settings sections">
          {PANELS.map((p) => (
            <button
              key={p.id}
              className={panel === p.id ? "settings-tab active" : "settings-tab"}
              // Reserve the bold (active) width so switching panels never shifts the rail.
              data-label={p.label}
              // The active panel is stated, not just colored, the same as the masthead.
              aria-current={panel === p.id ? "page" : undefined}
              onClick={() => switchPanel(p.id)}
            >
              {p.label}
            </button>
          ))}
        </nav>
      )}
      {/* Directly under the rail that was clicked, so the rail does not move: the same slot and
          the same two buttons the policy editor's own switch confirm uses (rule 18). The save
          bar below names WHICH fields are unsaved, so this does not repeat them. */}
      {pendingSwitch !== null && (
        <div className="notice notice-warn">
          You have unsaved {leavingLabel} settings. Switching to {pendingLabel} discards them.{" "}
          <button
            type="button"
            className="danger"
            onClick={() => {
              setPendingSwitch(null);
              setPanel(pendingSwitch);
            }}
          >
            Discard and switch
          </button>{" "}
          <button type="button" className="ghost" onClick={() => setPendingSwitch(null)}>
            Keep editing
          </button>
        </div>
      )}
      <div className="settings-body">
        {panel === "general" && <GeneralPanel onDirtyChange={setGeneralDirty} />}
        {panel === "services" && <ServicesPanel />}
        {panel === "plex" && <PlexPanel onDirtyChange={setPlexDirty} />}
        {panel === "jobs" && <JobsPanel onGoToPlex={() => switchPanel("plex")} />}
        {panel === "notifications" && <NotificationsPanel onDirtyChange={setWebhookDirty} />}
        {panel === "security" && <SecurityPanel />}
        {panel === "backup" && <BackupPanel />}
        {panel === "logs" && <LogsPanel />}
        {panel === "about" && <AboutPanel />}
      </div>
    </div>
  );
}
