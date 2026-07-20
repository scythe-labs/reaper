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
import { type CSSProperties, useEffect, useRef, useState } from "react";
import { accentInk, DEFAULT_ACCENT, isHexColor } from "../accent";
import { api, type Instance, type InstanceTest } from "../api";
import { bytes, date } from "../format";
import { LogsPanel } from "./LogsPanel";
import { PlexPanel } from "./PlexPanel";
import { ScanBar } from "./ScanBar";
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
  | "logs"
  | "about";

const PANELS: { id: Panel; label: string }[] = [
  { id: "general", label: "General" },
  { id: "services", label: "Services" },
  { id: "plex", label: "Plex" },
  { id: "jobs", label: "Jobs" },
  { id: "notifications", label: "Notifications" },
  { id: "security", label: "Security" },
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
// stay clear of the fixed red "remove" and green "keep" verdict colours. Any hex is allowed
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

function GeneralPanel() {
  const queryClient = useQueryClient();
  const general = useQuery({ queryKey: ["general-settings"], queryFn: api.general });

  const [name, setName] = useState("");
  const [url, setUrl] = useState("");
  const [proxies, setProxies] = useState("");
  const [accent, setAccent] = useState(DEFAULT_ACCENT);
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
    setProxies(general.data.trusted_proxies.join(", "));
    setAccent(general.data.accent_color);
  }, [general.data]);

  const save = useMutation({
    mutationFn: api.saveGeneral,
    onSuccess: (data) => {
      // Re-seed from the canonical stored values (rule 39). Setting the query cache also
      // makes the shell re-apply the accent app-wide, so a save re-tints everything.
      queryClient.setQueryData(["general-settings"], data);
      setName(data.application_name);
      setUrl(data.application_url ?? "");
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

  return (
    <div className="panel panel-wide">
      <h2>Services</h2>
      <p className="blurb">
        The apps Reaper reads from. It only ever reads. Nothing here can delete a file.
      </p>
      {error && <p className="notice notice-error">{(error as Error).message}</p>}
      {isPending && <p className="muted">Loading…</p>}
      {data &&
        KINDS.map((k) => (
          <section key={k.value} className="service-section">
            <h3>{k.label}</h3>
            <p className="service-hint">{k.hint}</p>
            <div className="service-grid">
              {data
                .filter((i) => i.kind === k.value)
                .map((i) => (
                  <ServiceCard
                    key={i.id}
                    instance={i}
                    onEdit={() => setModal({ kind: i.kind, instance: i })}
                  />
                ))}
              <button
                type="button"
                className="service-add"
                onClick={() => setModal({ kind: k.value, instance: null })}
              >
                <span aria-hidden="true">+</span> Add a {k.label}
              </button>
            </div>
          </section>
        ))}
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

const CRON_PRESETS: { label: string; cron: string | null }[] = [
  { label: "Off (scan by hand)", cron: null },
  { label: "Every night at 2am", cron: "0 2 * * *" },
  { label: "Every Sunday at 3am", cron: "0 3 * * 0" },
  { label: "First of the month, 3am", cron: "0 3 1 * *" },
];

/** "Off" is a real choice, so it needs a value of its own that is not a cron line. */
const SCHEDULE_OFF = "__off__";
/** The value the picker sits on before the schedule has been read: it matches no option,
 *  so nothing is shown as the current setting. */
const SCHEDULE_UNREAD = "__unread__";

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

/** The upkeep jobs — refresh ratings, refresh lists, sweep history. Each shows when it is
 *  next due and can be run on the spot; none of them can delete anything. */
function MaintenanceJobs() {
  const queryClient = useQueryClient();
  const { data, isPending, isError } = useQuery({ queryKey: ["schedule"], queryFn: api.schedule });
  const [ran, setRan] = useState<Record<string, string>>({});

  const run = useMutation({
    mutationFn: (id: string) => api.runJob(id),
    onSuccess: (_r, id) => {
      setRan((m) => ({ ...m, [id]: "Started. It will run in the background." }));
      void queryClient.invalidateQueries({ queryKey: ["schedule"] });
    },
  });

  // An unread list is not an empty one: say so, rather than showing no jobs at all.
  if (isPending) {
    return <p className="muted">Loading…</p>;
  }
  if (isError) {
    return <p className="notice notice-error">Couldn't load these jobs. Reload to try again.</p>;
  }

  // The scheduled scan (if any) is represented by the Library scan card above, not here.
  const jobs = (data?.jobs ?? []).filter((j) => j.id !== "scheduled_scan");

  return (
    <>
      <ul className="job-list">
        {jobs.map((job) => (
          <li key={job.id}>
            <div className="job-id">
              <strong>{job.label}</strong>
              <span className="muted">{ran[job.id] ?? `next ${whenText(job.next_run_at)}`}</span>
            </div>
            <button
              className="ghost sm"
              disabled={run.isPending}
              onClick={() => run.mutate(job.id)}
            >
              Run now
            </button>
          </li>
        ))}
      </ul>
      {run.error && (
        <p className="notice notice-error">The job didn't start: {run.error.message}</p>
      )}
    </>
  );
}

function AutoScanSchedule() {
  const queryClient = useQueryClient();
  const { data, isPending, isError } = useQuery({ queryKey: ["schedule"], queryFn: api.schedule });
  const [custom, setCustom] = useState("");
  const [error, setError] = useState<string | null>(null);

  const save = useMutation({
    mutationFn: (cron: string | null) => api.saveSchedule(cron),
    onSuccess: () => {
      setError(null);
      void queryClient.invalidateQueries({ queryKey: ["schedule"] });
    },
    onError: (e: Error) => setError(e.message),
  });

  // "No schedule" is itself a setting, so an unread query must not fall back to it: that
  // would show "Off (scan by hand)" as the answer. Until the schedule is read the picker
  // sits on a disabled "Checking…" entry, so no real choice reads as the current one.
  const current = data ? (data.scan_cron ?? null) : undefined;
  const chosen = data ? (data.scan_cron ?? SCHEDULE_OFF) : SCHEDULE_UNREAD;
  // A cron line the operator typed themselves is not one of the four, so it joins the
  // list as its own entry rather than leaving the picker blank.
  const customCron =
    current && !CRON_PRESETS.some((p) => p.cron === current) ? current : null;

  return (
    <>
      {isError && (
        <p className="notice notice-error">Couldn't load the schedule. Reload to try again.</p>
      )}
      <div className="set-rows">
        <div className="set-row">
          <span className="set-label">Automatic scan</span>
          <p className="help">
            When Reaper starts a scan on its own. Off means a scan only runs when you ask
            for one.
          </p>
          <div className="set-control">
            <select
              value={chosen}
              aria-label="Automatic scan"
              disabled={save.isPending}
              onChange={(e) => {
                const next = e.target.value;
                save.mutate(next === SCHEDULE_OFF ? null : next);
              }}
            >
              {!data && (
                <option value={SCHEDULE_UNREAD} disabled>
                  {isPending ? "Checking…" : "Couldn't check"}
                </option>
              )}
              {CRON_PRESETS.map((p) => (
                <option key={p.label} value={p.cron ?? SCHEDULE_OFF}>
                  {p.label}
                </option>
              ))}
              {customCron && <option value={customCron}>Your own schedule · {customCron}</option>}
            </select>
          </div>
        </div>
        <div className="set-row">
          <span className="set-label">Your own schedule</span>
          <p className="help">
            A cron line, for when none of the times above fit. For example 30 4 * * * runs
            at 4:30am every day.
          </p>
          <div className="set-control">
            <input
              type="text"
              value={custom}
              placeholder="30 4 * * *"
              aria-label="Your own schedule"
              onChange={(e) => setCustom(e.target.value)}
            />
            <button
              className="ghost"
              disabled={!custom.trim() || save.isPending}
              onClick={() => save.mutate(custom.trim())}
            >
              Set
            </button>
          </div>
        </div>
      </div>
      {error && <p className="notice notice-error">{error}</p>}
    </>
  );
}

function JobsPanel() {
  const { data: snapshot } = useQuery({
    queryKey: ["snapshot"],
    queryFn: api.latestSnapshot,
    retry: false,
  });

  return (
    <div className="panel">
      <h2>Jobs</h2>
      <p className="blurb">
        Everything Reaper runs on a timer lives here, and you can run any of it now without
        waiting. None of these can delete a thing. A scan just refreshes the review queue, and
        the rest is cache upkeep.
      </p>

      <h3>Library scan</h3>
      <p className="help">
        Reads your library and re-scores it. Watch the progress below.
        {snapshot && ` Last scan ${date(snapshot.created_at)}.`}
      </p>
      <ScanBar snapshot={snapshot} />

      <h3>Run automatically</h3>
      <p className="help">
        Reaper can scan on its own to keep the queue fresh. It still only reads. You approve
        every deletion by hand.
      </p>
      <AutoScanSchedule />

      <h3>Background upkeep</h3>
      <p className="help">
        Refreshing IMDb ratings and protection lists, and sweeping watch history, so scans stay
        accurate. These run once a day on their own; run one now if you can't wait.
      </p>
      <MaintenanceJobs />
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

function AdminPasswordForm({ needed }: { needed: boolean }) {
  const queryClient = useQueryClient();
  const [current, setCurrent] = useState("");
  const [pw, setPw] = useState("");
  const [msg, setMsg] = useState<string | null>(null);
  const save = useMutation({
    mutationFn: () => api.setAdminPassword(pw, needed ? undefined : current),
    onSuccess: () => {
      setCurrent("");
      setPw("");
      setMsg("Password saved.");
      void queryClient.invalidateQueries({ queryKey: ["safety"] });
    },
    // No onError: a failure renders from `save.error` as an error notice, never in `msg`.
    // This password is what confirms turning deletion on, so "saved" and "wrong password"
    // must not look alike here.
  });
  return (
    <div className="safety-row">
      <div>
        <strong>{needed ? "Set an admin password" : "Change the admin password"}</strong>
        <p className="help">
          {needed
            ? "Choose something long, and keep it somewhere safe."
            : "Changing it needs the current password first."}
        </p>
      </div>
      <form
        className="pw-form"
        onSubmit={(e) => {
          e.preventDefault();
          setMsg(null);
          save.mutate();
        }}
      >
        {!needed && (
          <input
            type="password"
            value={current}
            onChange={(e) => setCurrent(e.target.value)}
            placeholder="current password"
            aria-label="Current password"
            autoComplete="current-password"
          />
        )}
        {/* The placeholder is a hint, not a name: it says how long the password must be
            and disappears the moment you type. The label names the field either way. */}
        <input
          type="password"
          value={pw}
          onChange={(e) => setPw(e.target.value)}
          placeholder="at least 12 characters"
          aria-label="New password"
          autoComplete="new-password"
        />
        {/* The same floor the server applies (MIN_PASSWORD_LENGTH in
            reaper/services/admin_password.py), so the button, the hint above, and the
            server rule all state one number. */}
        <button
          type="submit"
          className="primary sm"
          disabled={pw.length < 12 || (!needed && current.length === 0) || save.isPending}
        >
          Save
        </button>
        {msg && <span className="muted">{msg}</span>}
      </form>
      {save.error && (
        <p className="notice notice-error">
          {needed
            ? `The password wasn't set: ${save.error.message}`
            : `The password wasn't changed: ${save.error.message}`}
        </p>
      )}
    </div>
  );
}

function SecurityPanel() {
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
            // The active panel is stated, not just coloured, the same as the masthead.
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
        {panel === "jobs" && <JobsPanel />}
        {panel === "notifications" && <NotificationsPanel />}
        {panel === "security" && <SecurityPanel />}
        {panel === "logs" && <LogsPanel />}
        {panel === "about" && <AboutPanel />}
      </div>
    </div>
  );
}
