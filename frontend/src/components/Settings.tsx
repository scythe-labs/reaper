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
  useEffect,
  useRef,
  useState,
} from "react";
import { accentInk, DEFAULT_ACCENT, isHexColor } from "../accent";
import {
  api,
  type Instance,
  type InstanceTest,
  type RestoreSummary,
  type ScheduledJob,
} from "../api";
import { useBackGuard } from "../backnav";
import { bytes, count, since } from "../format";
import { LogsPanel } from "./LogsPanel";
import { ModalShell } from "./ModalShell";
import { PlexPanel } from "./PlexPanel";
import { FixedQuantity } from "./QuantityInput";
import { ScanRow } from "./ScanBar";
import { Segmented } from "./Segmented";
import { KINDS, kindLabel, ServiceModal, TestBadge } from "./ServiceModal";
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
  { id: "backup", label: "Backup" },
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
    const supported = (Intl as { supportedValuesOf?: (key: string) => string[] })
      .supportedValuesOf;
    if (supported) zones = supported("timeZone");
  } catch {
    zones = [];
  }
  _zoneCache = zones.length ? zones : ["UTC"];
  return _zoneCache;
}

function GeneralPanel() {
  const queryClient = useQueryClient();
  const general = useQuery({ queryKey: ["general-settings"], queryFn: api.general });

  const [name, setName] = useState("");
  const [url, setUrl] = useState("");
  const [tz, setTz] = useState("");
  const [proxies, setProxies] = useState("");
  const [accent, setAccent] = useState(DEFAULT_ACCENT);
  // The days a plain Spare keeps an item, while the operator is editing it. The stored value
  // (0 = forever) is the source of truth for which mode is on; this is the box's live number,
  // seeded to a sensible 30 when the stored default is Forever so switching to a length starts
  // somewhere reasonable.
  const [spareDays, setSpareDays] = useState(30);
  const [theme, setTheme] = useState<ThemeChoice>(readTheme);
  const [revealedKey, setRevealedKey] = useState<string | null>(null);
  const [copied, setCopied] = useState(false);
  const [confirmReplace, setConfirmReplace] = useState(false);

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
    if (general.data.default_spare_days > 0) setSpareDays(general.data.default_spare_days);
  }, [general.data]);

  const save = useMutation({
    mutationFn: api.saveGeneral,
    onSuccess: (data) => {
      // Re-seed from the canonical stored values (rule 39). Setting the query cache also
      // makes the shell re-apply the accent app-wide, so a save re-tints everything.
      queryClient.setQueryData(["general-settings"], data);
      setName(data.application_name);
      setUrl(data.application_url ?? "");
      setTz(data.timezone);
      setProxies(data.trusted_proxies.join(", "));
      setAccent(data.accent_color);
    },
  });

  const reveal = useMutation({
    mutationFn: api.revealApiKey,
    onSuccess: (r) => setRevealedKey(r.key),
  });
  const generate = useMutation({
    mutationFn: api.generateApiKey,
    onSuccess: (r) => {
      setRevealedKey(r.key);
      setConfirmReplace(false);
      void queryClient.invalidateQueries({ queryKey: ["general-settings"] });
    },
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
      throw new Error("Copying needs a secure (https) page. Press Show, then select the key by hand.");
    }
    await navigator.clipboard.writeText(key);
    setCopied(true);
    setTimeout(() => setCopied(false), 2000);
  };
  const copy = useMutation({ mutationFn: copyKey });

  if (general.isPending) {
    return <p className="muted">Loading…</p>;
  }
  if (general.isError || !general.data) {
    return <p className="notice notice-error">Couldn't load these settings. Reload to try again.</p>;
  }
  const data = general.data;

  const nameDirty = name.trim() !== data.application_name;
  const urlDirty = url.trim() !== (data.application_url ?? "");
  const tzDirty = tz !== data.timezone;
  // The current zone may not be in the browser's list (an older engine, or a server-only
  // zone); keep it selectable so a save never silently drops it.
  const zoneOptions =
    data.timezone && !allTimeZones().includes(data.timezone)
      ? [data.timezone, ...allTimeZones()]
      : allTimeZones();
  const accentValid = isHexColor(accent);
  const accentDirty = accent.trim().toLowerCase() !== data.accent_color.toLowerCase();
  const proxiesDirty =
    proxies
      .split(",")
      .map((p) => p.trim())
      .filter(Boolean)
      .join(", ") !== data.trusted_proxies.join(", ");

  return (
    <div className="panel">
      <h2>General</h2>
      <p className="muted">How this Reaper presents itself, and how other tools may talk to it.</p>

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
              {nameDirty && (
                <button
                  className="primary"
                  disabled={save.isPending}
                  onClick={() => save.mutate({ application_name: name.trim() })}
                >
                  Save
                </button>
              )}
            </div>
          </div>
          <div className="set-row">
            <span className="set-label">Application URL</span>
            <p className="help">
              Where people reach Reaper, for example https://reaper.example.com. Notifications
              use it to link back here. Leave empty and notifications simply skip the link.
            </p>
            <div className="set-control">
              <input
                type="text"
                value={url}
                placeholder="https://reaper.example.com"
                onChange={(e) => setUrl(e.target.value)}
                aria-label="Application URL"
              />
              {urlDirty && (
                <button
                  className="primary"
                  disabled={save.isPending}
                  onClick={() => save.mutate({ application_url: url.trim() })}
                >
                  Save
                </button>
              )}
            </div>
          </div>
          <div className="set-row">
            <span className="set-label">Time zone</span>
            <p className="help">The server's time zone.</p>
            <div className="set-control">
              <select
                value={tz}
                aria-label="Time zone"
                onChange={(e) => setTz(e.target.value)}
              >
                {zoneOptions.map((z) => (
                  <option key={z} value={z}>
                    {z}
                  </option>
                ))}
              </select>
              {tzDirty && (
                <button
                  className="primary"
                  disabled={save.isPending}
                  onClick={() => save.mutate({ timezone: tz })}
                >
                  Save
                </button>
              )}
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
              {accentDirty && (
                <button
                  className="primary"
                  disabled={save.isPending || !accentValid}
                  onClick={() => save.mutate({ accent_color: accent.trim().toLowerCase() })}
                >
                  Save
                </button>
              )}
              {accent.toLowerCase() !== DEFAULT_ACCENT && (
                <button className="link" onClick={() => setAccent(DEFAULT_ACCENT)}>
                  Reset to default
                </button>
              )}
            </div>
            {!accentValid && (
              <p className="help field-error">Enter a hex code like #25c3ff.</p>
            )}
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
              Light or dark. "Match my device" follows your system setting. Applies to this
              browser only.
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
              TV shows in the review queue open with every season showing.
            </p>
            <div className="set-control">
              <Switch
                checked={data.expand_seasons_default}
                disabled={save.isPending}
                ariaLabel="Expand seasons by default"
                onChange={(enabled) => save.mutate({ expand_seasons_default: enabled })}
              />
            </div>
          </div>
          <div className="set-row">
            <span className="set-label">Default spare length</span>
            <p className="help">
              How long a plain Spare keeps a title before Reaper judges it again. Set a different
              length for any single title from its Spare menu.
            </p>
            <div className="set-control">
              <Segmented
                value={data.default_spare_days > 0 ? "days" : "forever"}
                options={[
                  ["days", "Days"],
                  ["forever", "Forever"],
                ]}
                label="Default spare length"
                onChange={(mode) =>
                  save.mutate({ default_spare_days: mode === "forever" ? 0 : spareDays })
                }
              />
              {/* Only while a length is in force -- Forever hides the box, matching how a
                  group's sub-controls disappear when its toggle is off. */}
              {data.default_spare_days > 0 && (
                <>
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
                  {spareDays !== data.default_spare_days && (
                    <button
                      className="primary"
                      disabled={save.isPending}
                      onClick={() => save.mutate({ default_spare_days: spareDays })}
                    >
                      Save
                    </button>
                  )}
                </>
              )}
            </div>
          </div>
        </div>
      </div>

      <div className="set-group">
        <h3>API access</h3>
        <div className="set-rows">
          <div className="set-row">
            <span className="set-label">API key</span>
            <p className="help">
              Lets scripts and other apps call the Reaper API without signing in: send it as
              the X-Api-Key header. A key can read your library, start scans, plan, and edit
              the policy. It cannot change any setting, turn deletion on, or run a reap. Those
              stay here in the browser, behind your password.
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
          <div className="set-row">
            <span className="set-label">API reference</span>
            <p className="help">
              Every endpoint, documented from the running app, with a try-it-out client that
              can use your key. Only visible while signed in.
            </p>
            <div className="set-control">
              <a className="btn-link" href="/api/docs" target="_blank" rel="noreferrer">
                Open the API reference ↗
              </a>
            </div>
          </div>
        </div>
        {(reveal.error || generate.error || copy.error) && (
          <p className="notice notice-error">
            {(reveal.error ?? generate.error ?? copy.error)?.message}
          </p>
        )}
      </div>

      <div className="set-group">
        <h3>Reverse proxy</h3>
        <div className="set-rows">
          <div className="set-row">
            <span className="set-label">Behind a reverse proxy</span>
            <p className="help">
              Turn this on if Nginx, Traefik, Caddy or similar sits in front of Reaper. Reaper
              will then trust the proxy to say which address each visitor really came from,
              which keeps sign-in rate limits accurate per visitor instead of lumping everyone
              together.
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
              {proxiesDirty && data.proxy_trust_enabled && (
                <button
                  className="primary"
                  disabled={save.isPending}
                  onClick={() =>
                    save.mutate({
                      trusted_proxies: proxies
                        .split(",")
                        .map((p) => p.trim())
                        .filter(Boolean),
                    })
                  }
                >
                  Save
                </button>
              )}
            </div>
          </div>
        </div>
        <p className="group-hint muted">
          Off by default, and forwarded headers from anywhere else are always ignored: a
          stranger can't fake their address to dodge the login lockout.
        </p>
      </div>

      {save.error && <p className="notice notice-error">{save.error.message}</p>}
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
  // Back closes the service editor instead of leaving Reaper.
  useBackGuard(modal !== null, () => setModal(null));

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
              One file with everything Reaper decided and everything you set up, plus the keys
              that unlock your saved credentials. The rebuildable cache is left out to keep the
              file small.
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
                Your encryption key is set through the environment, so it is not inside this
                backup. Keep that key with the file, or a restore cannot read your saved
                credentials.
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

  const choose = async (file: File) => {
    setError(null);
    setSummary(null);
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
    setError(null);
    setBusy(true);
    try {
      await api.restoreConfirm(password);
      // The confirm armed the swap; refetch so `armed` flips on and this card shows the
      // restart prompt. Drop the staged summary from local state either way.
      reset();
      await refreshInfo();
    } catch (err) {
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
            <strong>A restore is ready.</strong> Restart Reaper's container to finish. Nothing
            has changed yet.
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

      <input
        ref={inputRef}
        type="file"
        accept=".reaper"
        hidden
        onChange={onPick}
      />

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
              <button type="button" className="link chosen-x" onClick={() => void cancel()} disabled={busy}>
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
      'With this off, Reaper stops re-reading your full history. Imported or backdated plays won\'t be counted, and a wiped history won\'t be caught, so "never watched" can drift out of date.',
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
function ScheduleModal({ job, onClose }: { job: ScheduledJob; onClose: () => void }) {
  const queryClient = useQueryClient();
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
          {/* The clock times above are the server's, not this browser's -- commonly UTC in a
              Docker container. Said once so "2 AM" is not read as local time (U-5). */}
          <span className="help">Times are your server's clock, often UTC in Docker.</span>
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
      void queryClient.invalidateQueries({ queryKey: ["schedule"] });
      // A beat later, once the scheduler has actually submitted the job, so a long upkeep
      // run still reads as "running" even if the first refetch landed before it started.
      window.setTimeout(
        () => void queryClient.invalidateQueries({ queryKey: ["schedule"] }),
        700,
      );
    },
  });
  const running = job.running || run.isPending;

  return (
    <div className="jobrow">
      <div className="jobrow-main">
        <div className="jobrow-title">{meta.title}</div>
        <div className="jobrow-desc">{meta.desc}</div>
        {running && (
          <div className="jobrow-run">
            <span className="spin" aria-hidden="true" /> Running now…
          </div>
        )}
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
        {last && (
          <div className="jobrow-meta">
            <strong>{count(last.movies)}</strong> movie{last.movies === 1 ? "" : "s"} and{" "}
            <strong>{count(last.seasons)}</strong> season{last.seasons === 1 ? "" : "s"} on the
            shelves
          </div>
        )}
        {running ? (
          <div className="jobrow-run">
            <span className="spin" aria-hidden="true" /> Updating…
          </div>
        ) : (
          <div className="jobrow-sched">
            Runs after every scan{last ? ` · last updated ${since(last.at)}` : ""}
          </div>
        )}
        <div className="jobrow-link">
          <button className="link" onClick={onGoToPlex}>
            Manage in Plex → Leaving Soon
          </button>
        </div>
        {runSync.data && !running && (
          <div className="jobrow-sched">
            {runSync.data.applied
              ? `${count(runSync.data.added_count)} added, ${count(runSync.data.cleared_count)} cleared in Plex${runSync.data.notified ? " · Discord notified" : ""}`
              : `${count(runSync.data.added_count)} to add, ${count(runSync.data.cleared_count)} to clear · preview only, nothing was written`}
            {runSync.data.problems.length > 0 && " · some libraries didn't sync, see the logs"}
          </div>
        )}
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
  // Back closes the schedule editor instead of leaving Reaper.
  useBackGuard(editing !== null, () => setEditing(null));

  const jobsById = new Map<string, ScheduledJob>(
    (schedule.data?.jobs ?? []).map((j) => [j.id, j]),
  );
  const scanJob = jobsById.get(SCAN_ID);

  return (
    <div className="panel">
      <h2>Jobs</h2>
      <p className="blurb">
        Everything Reaper runs on a timer lives here, and you can run any of it now without
        waiting. None of these can delete a thing. A scan just refreshes the review queue; the
        rest is upkeep.
      </p>

      <div className="set-rows">
        <ScanRow
          snapshot={snapshot}
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

      {editing && <ScheduleModal job={editing} onClose={() => setEditing(null)} />}
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
function NotificationsPanel() {
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

  return (
    <div className="panel">
      <h2>Notifications</h2>
      <p className="blurb">
        A Discord webhook is how Reaper warns your household before anything is deleted: while a
        title is in its grace period it posts a "leaving soon" heads-up here, so someone can
        watch it or spare it in time. It's optional, but it's the one warning that reaches people
        who don't watch the Plex "Leaving Soon" shelf.
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
    needed
      ? `The password wasn't set: ${save.error.message}`
      : `The password wasn't changed: ${save.error.message}`
  ) : null;

  // Typing clears the "saved" note and any stale failure, so neither lingers over a form the
  // operator is now re-editing.
  const onEdit =
    (set: (v: string) => void) => (e: ChangeEvent<HTMLInputElement>) => {
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
                />
              </label>
              <hr className="pw-sep" />
            </>
          )}
          <label className="field-sm">
            <span className="field-label">New password</span>
            {/* The placeholder states the length up front; the label names the field. */}
            <input
              type="password"
              value={pw}
              onChange={onEdit(setPw)}
              placeholder="at least 12 characters"
              autoComplete="new-password"
            />
          </label>
          <label className="field-sm">
            <span className="field-label">Confirm new password</span>
            <input
              type="password"
              value={confirm}
              onChange={onEdit(setConfirm)}
              autoComplete="new-password"
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
  const { data, isLoading, isError } = useQuery({ queryKey: ["safety"], queryFn: api.safety });

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
        The admin password. It confirms turning deletion on (in{" "}
        <strong>Policy → Deletion</strong>), and it's also how you sign in without Plex.
      </p>

      <AdminPasswordForm needed={!data.has_password} />
    </div>
  );
}

// --- shell -----------------------------------------------------------------

export function Settings({ initialPanel }: { initialPanel?: Panel | undefined }) {
  const [panel, setPanel] = useState<Panel>(initialPanel ?? "general");
  return (
    <div className="settings">
      <nav className="settings-nav" aria-label="Settings sections">
        {PANELS.map((p) => (
          <button
            key={p.id}
            className={panel === p.id ? "settings-tab active" : "settings-tab"}
            // Reserve the bold (active) width so switching panels never shifts the rail.
            data-label={p.label}
            // The active panel is stated, not just colored, the same as the masthead.
            aria-current={panel === p.id ? "page" : undefined}
            onClick={() => setPanel(p.id)}
          >
            {p.label}
          </button>
        ))}
      </nav>
      <div className="settings-body">
        {panel === "general" && <GeneralPanel />}
        {panel === "services" && <ServicesPanel />}
        {panel === "plex" && <PlexPanel />}
        {panel === "jobs" && <JobsPanel onGoToPlex={() => setPanel("plex")} />}
        {panel === "notifications" && <NotificationsPanel />}
        {panel === "security" && <SecurityPanel />}
        {panel === "backup" && <BackupPanel />}
        {panel === "logs" && <LogsPanel />}
        {panel === "about" && <AboutPanel />}
      </div>
    </div>
  );
}
