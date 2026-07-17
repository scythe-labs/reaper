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
import { useEffect, useRef, useState } from "react";
import {
  api,
  type Instance,
  type InstanceTest,
  type LogLine,
  type PlexResourceConnection,
  type PlexServerChoice,
} from "../api";
import { bytes, count, date, since } from "../format";
import { ScanBar } from "./ScanBar";
import { KINDS, kindLabel, ServiceModal, TestBadge } from "./ServiceModal";
import { Switch } from "./Switch";

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

function GeneralPanel() {
  const queryClient = useQueryClient();
  const general = useQuery({ queryKey: ["general-settings"], queryFn: api.general });

  const [name, setName] = useState("");
  const [url, setUrl] = useState("");
  const [proxies, setProxies] = useState("");
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
  }, [general.data]);

  const save = useMutation({
    mutationFn: api.saveGeneral,
    onSuccess: (data) => {
      queryClient.setQueryData(["general-settings"], data);
      setName(data.application_name);
      setUrl(data.application_url ?? "");
      setProxies(data.trusted_proxies.join(", "));
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
              the X-Api-Key header. A key can read everything, start scans, and edit policy,
              but it can never turn deletion on, run a reap, or change sign-in settings. Those
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
          {test ? (
            <TestBadge result={test} />
          ) : instance.last_error ? (
            <span className="test-badge bad">✗ {instance.last_error}</span>
          ) : instance.last_ok_at ? (
            <span className="test-badge ok">
              ✓ Reached{instance.detected_version && ` (v${instance.detected_version})`}
            </span>
          ) : (
            <span className="muted">Not tested yet</span>
          )}
        </div>
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

// --- Plex ------------------------------------------------------------------

const MANUAL_CONNECTION = "__manual__";

/** The label a connection shows in the picker: where it goes, then how.
 *
 *  plex.direct hostnames embed the address as dashes ("192-168-20-73.abc….plex.direct"),
 *  which reads as noise; show the address itself and keep the certificate goodness as
 *  the "secure" tag. The full URI stays the option's value, so what is saved is exact. */
function connectionLabel(c: PlexResourceConnection): string {
  const kind = c.relay ? "Relay" : c.local ? "Local" : "Remote";
  let host = c.uri.replace(/^https?:\/\//, "");
  const direct = /^(\d+)-(\d+)-(\d+)-(\d+)\.[0-9a-f]+\.plex\.direct(:\d+)?$/i.exec(host);
  if (direct) {
    host = `${direct[1]}.${direct[2]}.${direct[3]}.${direct[4]}${direct[5] ?? ""}`;
  }
  const secure = c.protocol === "https" ? " · secure" : "";
  return `${kind} · ${host}${secure}`;
}

export function PlexPanel() {
  const queryClient = useQueryClient();
  const { data } = useQuery({ queryKey: ["plex"], queryFn: api.plexStatus });
  const linked = data?.linked ?? false;
  const [linking, setLinking] = useState(false);
  const [message, setMessage] = useState<string | null>(null);
  const [servers, setServers] = useState<PlexServerChoice[] | null>(null);
  const pollRef = useRef<number | null>(null);
  const pinRef = useRef<number | null>(null);
  // The web-address box mirrors the saved value and follows it when a save (or another
  // tab) changes it; typing diverges the two until Save or a refetch reconciles them.
  const [webUrl, setWebUrl] = useState("");
  const [webUrlError, setWebUrlError] = useState<string | null>(null);
  const savedWebUrl = data?.web_url ?? "";
  useEffect(() => setWebUrl(savedWebUrl), [savedWebUrl]);

  // The certificate check. Before linking it rides along with the link polls (so a
  // self-signed server can be reached at all); once linked it edits the stored server
  // row directly. The ref keeps the in-flight poll reading the current choice.
  const [verifyCert, setVerifyCert] = useState(true);
  const verifyRef = useRef(true);
  const savedVerify = data?.verify_tls ?? true;
  useEffect(() => {
    setVerifyCert(savedVerify);
    verifyRef.current = savedVerify;
  }, [savedVerify]);

  useEffect(() => () => (pollRef.current ? clearInterval(pollRef.current) : undefined), []);

  const saveWebUrl = useMutation({
    mutationFn: () => api.setPlexWebUrl(webUrl.trim()),
    onSuccess: () => {
      setWebUrlError(null);
      void queryClient.invalidateQueries({ queryKey: ["plex"] });
    },
    onError: (e: Error) => setWebUrlError(e.message),
  });

  // Flip the stored certificate check on the linked server. Sends the SAVED web
  // address, never the box's half-typed one, so this toggle cannot save a URL edit.
  const saveVerify = useMutation({
    mutationFn: (next: boolean) => api.setPlexWebUrl(savedWebUrl, next),
    onSuccess: () => void queryClient.invalidateQueries({ queryKey: ["plex"] }),
    onError: (e: Error) => setMessage(e.message),
  });

  const done = () => {
    setLinking(false);
    setServers(null);
    if (pollRef.current) clearInterval(pollRef.current);
    void queryClient.invalidateQueries({ queryKey: ["plex"] });
    void queryClient.invalidateQueries({ queryKey: ["setup"] });
  };

  // Give the poll a deadline, exactly like Login's PlexButton. Without one, an operator
  // who opens the approval tab and never approves leaves this POSTing every 2s forever,
  // with the button stuck disabled on "Waiting for Plex…" until a full page reload.
  const beginPoll = (pinId: number, machineId?: string) => {
    const deadline = Date.now() + 5 * 60 * 1000;
    pollRef.current = window.setInterval(async () => {
      if (Date.now() > deadline) {
        setMessage("Plex sign-in timed out. Please try again.");
        done();
        return;
      }
      try {
        const poll = await api.plexLinkPoll(pinId, machineId, verifyRef.current);
        if (poll.status === "ok") {
          setMessage(`Linked to ${poll.server?.name ?? "your server"}.`);
          done();
        } else if (poll.status === "choose_server") {
          // The account owns several servers. The sign-in stays valid; stop polling
          // and hold the list until the admin picks one.
          if (pollRef.current) clearInterval(pollRef.current);
          setServers(poll.servers ?? []);
        }
      } catch (e) {
        setMessage(e instanceof Error ? e.message : String(e));
        done();
      }
    }, 2000);
  };

  const startLink = async () => {
    setMessage(null);
    setLinking(true);
    try {
      const start = await api.plexLinkStart();
      pinRef.current = start.pin_id;
      window.open(start.auth_url, "_blank", "noopener");
      beginPoll(start.pin_id);
    } catch (e) {
      setMessage(e instanceof Error ? e.message : String(e));
      setLinking(false);
    }
  };

  /** The admin picked a server. One immediate poll usually finishes the link; a
   *  "pending" answer (plex.tv asking us to slow down) falls back to polling. */
  const pick = async (machineId: string) => {
    const pinId = pinRef.current;
    if (pinId == null) return;
    setServers(null);
    try {
      const poll = await api.plexLinkPoll(pinId, machineId, verifyRef.current);
      if (poll.status === "ok") {
        setMessage(`Linked to ${poll.server?.name ?? "your server"}.`);
        done();
      } else if (poll.status === "choose_server") {
        setServers(poll.servers ?? []);
      } else {
        beginPoll(pinId, machineId);
      }
    } catch (e) {
      setMessage(e instanceof Error ? e.message : String(e));
      done();
    }
  };

  const cancelChoice = () => {
    setMessage(null);
    done();
  };

  const unlink = useMutation({
    mutationFn: api.plexUnlink,
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["plex"] });
      void queryClient.invalidateQueries({ queryKey: ["setup"] });
    },
  });

  // --- the server and connection pickers, fed by the signed-in account ---------

  const resources = useQuery({
    queryKey: ["plex-resources"],
    queryFn: api.plexResources,
    enabled: linked,
    staleTime: 60_000,
    retry: false,
  });

  const invalidateAllPlex = () => {
    void queryClient.invalidateQueries({ queryKey: ["plex"] });
    void queryClient.invalidateQueries({ queryKey: ["plex-resources"] });
    void queryClient.invalidateQueries({ queryKey: ["plex-libraries"] });
    void queryClient.invalidateQueries({ queryKey: ["leaving-soon-settings"] });
  };

  const switchServer = useMutation({
    mutationFn: (machineId: string) => api.plexSwitchServer(machineId),
    onSuccess: () => {
      setMessage(null);
      invalidateAllPlex();
    },
    onError: (e: Error) => setMessage(e.message),
  });

  const [manualOpen, setManualOpen] = useState(false);
  const [manualHost, setManualHost] = useState("");
  const [manualPort, setManualPort] = useState("32400");
  const [manualSsl, setManualSsl] = useState(true);
  const [connError, setConnError] = useState<string | null>(null);

  const setConnection = useMutation({
    mutationFn: (uri: string) => api.plexSetConnection(uri),
    onSuccess: () => {
      setConnError(null);
      setManualOpen(false);
      void queryClient.invalidateQueries({ queryKey: ["plex"] });
    },
    onError: (e: Error) => setConnError(e.message),
  });

  const currentServer =
    resources.data?.servers.find((s) => s.current) ?? resources.data?.servers[0];
  const connections = currentServer?.connections ?? [];
  const savedUri = data?.connection_uri ?? "";
  const savedIsDiscovered = connections.some((c) => c.uri === savedUri);
  const connectionValue = manualOpen || !savedIsDiscovered ? MANUAL_CONNECTION : savedUri;

  const openManual = () => {
    // Seed the manual fields from wherever Reaper is pointed right now.
    try {
      const parsed = new URL(savedUri);
      setManualHost(parsed.hostname);
      setManualPort(parsed.port || (parsed.protocol === "https:" ? "443" : "32400"));
      setManualSsl(parsed.protocol === "https:");
    } catch {
      setManualHost("");
      setManualPort("32400");
      setManualSsl(true);
    }
    setConnError(null);
    setManualOpen(true);
  };

  const saveManual = () => {
    const host = manualHost.trim();
    if (!host) return;
    const scheme = manualSsl ? "https" : "http";
    setConnection.mutate(`${scheme}://${host}:${manualPort.trim() || "32400"}`);
  };

  // --- libraries ---------------------------------------------------------------

  const libraries = useQuery({
    queryKey: ["plex-libraries"],
    queryFn: api.plexLibraries,
    enabled: linked,
  });
  const syncLibraries = useMutation({
    mutationFn: api.syncPlexLibraries,
    onSuccess: (libs) => queryClient.setQueryData(["plex-libraries"], libs),
  });
  const saveLibraries = useMutation({
    mutationFn: api.setPlexLibraries,
    onSuccess: (libs) => queryClient.setQueryData(["plex-libraries"], libs),
  });

  // First visit on a linked install: the list has never been synced, so fetch it once
  // rather than showing an empty grid with a button to press. The ref makes it
  // once-per-mount even though the mutation object's identity changes per render.
  const autoSynced = useRef(false);
  useEffect(() => {
    if (linked && libraries.data && libraries.data.length === 0 && !autoSynced.current) {
      autoSynced.current = true;
      syncLibraries.mutate();
    }
  }, [linked, libraries.data, syncLibraries]);

  const toggleLibrary = (key: number, next: boolean) => {
    const libs = libraries.data ?? [];
    const enabled = new Set(libs.filter((l) => l.enabled).map((l) => l.key));
    if (next) enabled.add(key);
    else enabled.delete(key);
    saveLibraries.mutate([...enabled]);
  };

  // --- Leaving Soon --------------------------------------------------------------

  const leavingSoon = useQuery({
    queryKey: ["leaving-soon-settings"],
    queryFn: api.leavingSoonSettings,
    enabled: linked,
  });
  const saveLeavingSoon = useMutation({
    mutationFn: api.setLeavingSoonSettings,
    onSuccess: (s) => {
      queryClient.setQueryData(["leaving-soon-settings"], s);
      // The Reap page's grace bar reads these to pick its state.
      void queryClient.invalidateQueries({ queryKey: ["grace"] });
    },
  });

  const lsStatus = (() => {
    if (!leavingSoon.data) return null;
    const last = leavingSoon.data.last;
    if (!last) return "Not updated yet. It runs after every scan, or from the Reap page.";
    const movies = `${count(last.movies)} movie${last.movies === 1 ? "" : "s"}`;
    const seasons = `${count(last.seasons)} season${last.seasons === 1 ? "" : "s"}`;
    const wrote = last.applied ? "" : " · preview only, nothing was written in Plex";
    return `Last updated ${since(last.at)} · ${movies} and ${seasons} on the shelves · next update after the next scan${wrote}`;
  })();

  return (
    <div className="panel">
      <h2>Plex</h2>
      <p className="blurb">
        Linking Plex lets Reaper warn your library with a "Leaving Soon" shelf and read your
        "Never Reap" collection. It's optional. Scanning works without it.
      </p>

      <div className="set-group">
        <h3>Connection</h3>
        <div className="set-rows">
          {linked && data ? (
            <div className="set-row">
              <span className="set-label">{data.name}</span>
              <p className="help">Signed in with Plex. {data.connection_uri}</p>
              <div className="set-control">
                <button
                  className="ghost"
                  onClick={() => unlink.mutate()}
                  disabled={unlink.isPending}
                >
                  Unlink
                </button>
              </div>
            </div>
          ) : servers ? (
            <div className="set-row">
              <span className="set-label">Which server should Reaper manage?</span>
              <p className="help">
                This account owns more than one Plex server. Reaper will only ever scan and
                prune the one you pick.
              </p>
              <div className="set-control server-pick">
                {servers.map((s) => (
                  <button
                    key={s.machine_identifier}
                    className="server-pick-row"
                    onClick={() => void pick(s.machine_identifier)}
                  >
                    {s.name}
                  </button>
                ))}
                <button className="link" onClick={cancelChoice}>
                  Cancel
                </button>
              </div>
            </div>
          ) : (
            <div className="set-row">
              <span className="set-label">No Plex server linked</span>
              <p className="help">
                Sign in with Plex and Reaper discovers your servers. It never asks for a
                token by hand.
              </p>
              <div className="set-control">
                <button className="btn-plex" onClick={startLink} disabled={linking}>
                  {linking ? "Waiting for Plex…" : "Link with Plex"}
                </button>
              </div>
            </div>
          )}

          {linked && (
            <div className="set-row">
              <span className="set-label">Server</span>
              <p className="help">
                Plex servers this account can manage. Reaper works with one at a time.
                {resources.data?.source === "stored" &&
                  " Showing what was remembered at link time; plex.tv didn't answer."}
              </p>
              <div className="set-control">
                {resources.isPending ? (
                  <span className="muted">Looking for servers…</span>
                ) : resources.isError ? (
                  <>
                    <span className="muted">Couldn't list this account's servers.</span>
                    <button className="ghost sm" onClick={() => void resources.refetch()}>
                      Retry
                    </button>
                  </>
                ) : (
                  <>
                    <select
                      value={currentServer?.machine_identifier ?? ""}
                      disabled={switchServer.isPending}
                      onChange={(e) => {
                        const next = e.target.value;
                        if (next && next !== currentServer?.machine_identifier) {
                          switchServer.mutate(next);
                        }
                      }}
                    >
                      {(resources.data?.servers ?? []).map((s) => (
                        <option key={s.machine_identifier} value={s.machine_identifier}>
                          {s.name}
                        </option>
                      ))}
                    </select>
                    <button
                      className="ghost sm"
                      disabled={resources.isFetching}
                      onClick={() => void resources.refetch()}
                      title="Look for servers again"
                    >
                      {resources.isFetching ? "Refreshing…" : "Refresh"}
                    </button>
                  </>
                )}
              </div>
            </div>
          )}

          {linked && (
            <div className="set-row">
              <span className="set-label">Connection</span>
              <p className="help">
                How Reaper reaches the server. A local address is usually faster; remote
                works from anywhere. Pick "Manual address" to type your own.
              </p>
              <div className="set-control">
                <select
                  value={connectionValue}
                  disabled={setConnection.isPending || resources.isPending}
                  onChange={(e) => {
                    const next = e.target.value;
                    if (next === MANUAL_CONNECTION) openManual();
                    else {
                      setManualOpen(false);
                      if (next !== savedUri) setConnection.mutate(next);
                    }
                  }}
                >
                  {connections.map((c) => (
                    <option key={c.uri} value={c.uri}>
                      {connectionLabel(c)}
                    </option>
                  ))}
                  {!savedIsDiscovered && !manualOpen && (
                    <option value={MANUAL_CONNECTION}>Manual · {savedUri}</option>
                  )}
                  {(manualOpen || savedIsDiscovered) && (
                    <option value={MANUAL_CONNECTION}>Manual address…</option>
                  )}
                </select>
              </div>
            </div>
          )}

          {linked && manualOpen && (
            <div className="set-row">
              <span className="set-label">Manual address</span>
              <p className="help">Hostname or IP, port, and whether to use SSL.</p>
              <div className="set-control">
                <input
                  type="text"
                  value={manualHost}
                  onChange={(e) => setManualHost(e.target.value)}
                  placeholder="plex.example.net"
                  autoComplete="off"
                />
                <input
                  type="text"
                  className="input-port"
                  value={manualPort}
                  onChange={(e) => setManualPort(e.target.value.replace(/\D/g, ""))}
                  placeholder="32400"
                  inputMode="numeric"
                />
                <label className="toggle" title="Use SSL">
                  <Switch checked={manualSsl} onChange={setManualSsl} ariaLabel="Use SSL" />
                  <span>SSL</span>
                </label>
                <button
                  className="primary sm"
                  disabled={!manualHost.trim() || setConnection.isPending}
                  onClick={saveManual}
                >
                  {setConnection.isPending ? "Checking…" : "Save"}
                </button>
              </div>
            </div>
          )}

          <div className="set-row">
            <span className="set-label">Check the server's certificate</span>
            <p className="help">
              Turn this off only for a server you run yourself, like one with a self-signed
              certificate.
            </p>
            <div className="set-control">
              <Switch
                checked={verifyCert}
                disabled={saveVerify.isPending}
                ariaLabel="Check the server's certificate"
                onChange={(next) => {
                  setVerifyCert(next);
                  verifyRef.current = next;
                  if (linked) saveVerify.mutate(next);
                }}
              />
            </div>
          </div>

          <div className="set-row">
            <span className="set-label">Plex web address</span>
            <p className="help">
              Where links to your library open. Keep the default unless you host your own
              Plex Web. Clear it and save to go back to the default.
            </p>
            <div className="set-control">
              <input
                type="url"
                value={webUrl}
                onChange={(e) => {
                  setWebUrl(e.target.value);
                  setWebUrlError(null);
                }}
                placeholder="https://app.plex.tv"
                autoComplete="off"
              />
              {webUrl.trim() !== savedWebUrl && (
                <button
                  type="button"
                  className="primary sm"
                  disabled={saveWebUrl.isPending}
                  onClick={() => saveWebUrl.mutate()}
                >
                  {saveWebUrl.isPending ? "Saving…" : "Save"}
                </button>
              )}
            </div>
          </div>
        </div>

        {!verifyCert && (
          <p className="notice notice-warn">
            Reaper will accept this server's certificate without checking who issued it. Only
            use this for a server you run yourself, like one with a self-signed certificate.
          </p>
        )}
        {connError && <p className="notice notice-error">{connError}</p>}
        {webUrlError && <p className="notice notice-error">{webUrlError}</p>}
        {message && <p className="muted">{message}</p>}
      </div>

      {linked && (
        <div className="set-group">
          <h3>Libraries</h3>
          <p className="group-blurb">
            The libraries Reaper may touch in Plex. Leaving Soon shelves are managed only in
            libraries you turn on. This doesn't change what gets scanned: scanning reads from
            Radarr and Sonarr.
          </p>
          {libraries.isPending || syncLibraries.isPending ? (
            <p className="muted">Loading libraries…</p>
          ) : libraries.isError ? (
            <p className="notice notice-error">
              Couldn't load the library list. Reload to try again.
            </p>
          ) : (
            <>
              <div className="lib-head">
                <span className="muted">
                  {count((libraries.data ?? []).length)}{" "}
                  {(libraries.data ?? []).length === 1 ? "library" : "libraries"} found
                </span>
                <button
                  className="ghost sm"
                  disabled={syncLibraries.isPending}
                  onClick={() => syncLibraries.mutate()}
                >
                  Refresh libraries
                </button>
              </div>
              <div className="lib-grid">
                {(libraries.data ?? []).map((lib) => (
                  <div key={lib.key} className={lib.enabled ? "lib-card" : "lib-card off"}>
                    <span>
                      {lib.title}
                      <span className="lib-kind">{lib.kind === "movie" ? "movies" : "tv"}</span>
                    </span>
                    <Switch
                      checked={lib.enabled}
                      disabled={saveLibraries.isPending}
                      ariaLabel={`Let Reaper touch ${lib.title}`}
                      onChange={(next) => toggleLibrary(lib.key, next)}
                    />
                  </div>
                ))}
              </div>
              {(saveLibraries.error || syncLibraries.error) && (
                <p className="notice notice-error">
                  {(saveLibraries.error ?? syncLibraries.error)?.message}
                </p>
              )}
            </>
          )}
        </div>
      )}

      {linked && (
        <div className="set-group">
          <h3>Leaving Soon</h3>
          <p className="group-blurb">
            While an item counts down its grace period, Reaper can put it on a "Leaving Soon"
            shelf in Plex, so people get a heads-up before it goes: movies in your movie
            libraries, seasons in your TV libraries.
          </p>
          {leavingSoon.isPending ? (
            <p className="muted">Loading…</p>
          ) : leavingSoon.isError || !leavingSoon.data ? (
            <p className="notice notice-error">
              Couldn't load the Leaving Soon settings. Reload to try again.
            </p>
          ) : (
            <div className="set-rows">
              <div className="set-row">
                <span className="set-label">Show "Leaving Soon" in Plex</span>
                <p className="help">
                  Reaper keeps a Leaving Soon collection in each library you turned on above,
                  and puts the matching label on everything in it. Items appear when they
                  start counting down and drop off when they're spared or removed. Updates
                  after every scan, or any time from the Reap page.
                </p>
                <div className="set-control">
                  <Switch
                    checked={leavingSoon.data.enabled}
                    disabled={saveLeavingSoon.isPending}
                    ariaLabel='Show "Leaving Soon" in Plex'
                    onChange={(enabled) => saveLeavingSoon.mutate({ enabled })}
                  />
                </div>
              </div>
              <div className="set-row">
                <span className="set-label">Update while read-only</span>
                <p className="help">
                  Until deletion is on, Reaper writes nothing to Plex, including this shelf.
                  Turn this on to let the warning appear while Reaper is still read-only. It
                  can only manage the collection and label. It can never remove files.
                </p>
                <div className="set-control">
                  <Switch
                    checked={leavingSoon.data.allow_unarmed}
                    disabled={saveLeavingSoon.isPending}
                    ariaLabel="Update while read-only"
                    onChange={(allow_unarmed) => saveLeavingSoon.mutate({ allow_unarmed })}
                  />
                </div>
              </div>
              {lsStatus && (
                <div className="set-row set-status">
                  <span>{lsStatus}</span>
                </div>
              )}
            </div>
          )}
          {saveLeavingSoon.error && (
            <p className="notice notice-error">{saveLeavingSoon.error.message}</p>
          )}
        </div>
      )}

      {!linked && (
        <p className="help">
          Link Plex to pick libraries and turn on the "Leaving Soon" shelf.
        </p>
      )}
    </div>
  );
}

// --- Logs --------------------------------------------------------------------

const LEVEL_RANK: Record<string, number> = {
  DEBUG: 10,
  INFO: 20,
  WARNING: 30,
  ERROR: 40,
  CRITICAL: 50,
};

function levelClass(level: string): string {
  const upper = level.toUpperCase();
  if (upper === "DEBUG") return "log-lv debug";
  if (upper === "WARNING") return "log-lv warning";
  if (upper === "ERROR" || upper === "CRITICAL") return "log-lv error";
  return "log-lv info";
}

function logTime(ts: string): string {
  const parsed = new Date(ts);
  if (Number.isNaN(parsed.getTime())) return "";
  return parsed.toLocaleTimeString([], { hour12: false });
}

function LogsPanel() {
  const [live, setLive] = useState(true);
  const [search, setSearch] = useState("");
  const [minLevel, setMinLevel] = useState("all");
  const [lines, setLines] = useState<LogLine[]>([]);
  const [recordLevel, setRecordLevel] = useState<string | null>(null);
  const cursor = useRef(0);
  const consoleRef = useRef<HTMLDivElement | null>(null);

  const logs = useQuery({
    queryKey: ["logs"],
    queryFn: () => api.logs(cursor.current),
    refetchInterval: live ? 2000 : false,
  });

  // Fold each page into the accumulated window. The seq guard makes this idempotent
  // when React Query hands the same page twice (mount + focus refetch).
  useEffect(() => {
    const page = logs.data;
    if (!page) return;
    cursor.current = page.last_seq;
    setRecordLevel(page.level);
    if (page.lines.length) {
      setLines((prev) => {
        const newest = prev.at(-1)?.seq ?? 0;
        const fresh = page.lines.filter((l) => l.seq > newest);
        return fresh.length ? [...prev, ...fresh].slice(-2000) : prev;
      });
    }
  }, [logs.data]);

  const visible = lines.filter((line) => {
    if (minLevel !== "all" && (LEVEL_RANK[line.level] ?? 20) < (LEVEL_RANK[minLevel] ?? 0)) {
      return false;
    }
    if (search.trim() === "") return true;
    const needle = search.trim().toLowerCase();
    return line.text.toLowerCase().includes(needle) || line.level.toLowerCase().includes(needle);
  });

  // Follow the newest line while Live; leave the scroll alone while paused so reading
  // is undisturbed.
  useEffect(() => {
    if (!live) return;
    const el = consoleRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [visible.length, live]);

  const setLevel = useMutation({
    mutationFn: api.setLogLevel,
    onSuccess: (page) => setRecordLevel(page.level),
  });

  return (
    <div className="panel">
      <h2>Logs</h2>
      <p className="muted">
        What Reaper is doing right now, and the trail of what it did. Every removal decision
        is answerable from here. The newest {count(2000)} lines are kept.
      </p>

      <div className="logbar">
        <input
          type="search"
          className="log-search"
          placeholder="Search the log…"
          value={search}
          onChange={(e) => setSearch(e.target.value)}
          aria-label="Search the log"
        />
        <select
          value={minLevel}
          onChange={(e) => setMinLevel(e.target.value)}
          aria-label="Only show this level and up"
        >
          <option value="all">All levels</option>
          <option value="DEBUG">Debug and up</option>
          <option value="INFO">Info and up</option>
          <option value="WARNING">Warnings and up</option>
          <option value="ERROR">Errors only</option>
        </select>
        <button type="button" className={live ? "log-live on" : "log-live"} onClick={() => setLive(!live)}>
          <span className="live-dot" aria-hidden="true" />
          {live ? "Live · Pause" : "Paused · Resume"}
        </button>
        <span className="muted log-count">
          {visible.length === lines.length
            ? `${count(lines.length)} ${lines.length === 1 ? "line" : "lines"}`
            : `${count(visible.length)} of ${count(lines.length)} lines`}
        </span>
      </div>

      {logs.isPending && lines.length === 0 ? (
        <p className="muted">Loading the log…</p>
      ) : logs.isError && lines.length === 0 ? (
        <p className="notice notice-error">
          Couldn't load the log.{" "}
          <button className="ghost sm" onClick={() => void logs.refetch()}>
            Try again
          </button>
        </p>
      ) : (
        <div className="log-console" ref={consoleRef} role="log" aria-label="Application log">
          {visible.length === 0 ? (
            <p className="muted log-empty">
              {lines.length === 0
                ? "Nothing yet. New lines appear here as Reaper works."
                : "Nothing matches your search."}
            </p>
          ) : (
            visible.map((line) => (
              <div key={line.seq} className="log-row">
                <span className="log-t">{logTime(line.ts)}</span>
                <span className={levelClass(line.level)}>{line.level}</span>
                <span className="log-text">{line.text}</span>
              </div>
            ))
          )}
        </div>
      )}
      {logs.isError && lines.length > 0 && (
        <p className="notice notice-error">Live updates hit an error. Retrying…</p>
      )}

      <div className="set-group log-level-group">
        <h3>Logging</h3>
        <div className="set-rows">
          <div className="set-row">
            <span className="set-label">Logging level</span>
            <p className="help">
              How much Reaper writes, both here and in the container output. Info is the
              everyday setting. Debug is chatty and best while chasing a problem. Takes
              effect immediately, no restart.
            </p>
            <div className="set-control">
              <select
                value={recordLevel ?? "INFO"}
                disabled={setLevel.isPending || recordLevel === null}
                aria-label="Logging level"
                onChange={(e) => setLevel.mutate(e.target.value)}
              >
                <option value="DEBUG">Debug</option>
                <option value="INFO">Info</option>
                <option value="WARNING">Warning</option>
              </select>
            </div>
          </div>
        </div>
        {setLevel.error && <p className="notice notice-error">{setLevel.error.message}</p>}
      </div>
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
  const { data } = useQuery({ queryKey: ["schedule"], queryFn: api.schedule });
  const [ran, setRan] = useState<Record<string, string>>({});

  const run = useMutation({
    mutationFn: (id: string) => api.runJob(id),
    onSuccess: (_r, id) => {
      setRan((m) => ({ ...m, [id]: "Started. It will run in the background." }));
      void queryClient.invalidateQueries({ queryKey: ["schedule"] });
    },
  });

  // The scheduled scan (if any) is represented by the Library scan card above, not here.
  const jobs = (data?.jobs ?? []).filter((j) => j.id !== "scheduled_scan");

  return (
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
  );
}

function AutoScanSchedule() {
  const queryClient = useQueryClient();
  const { data } = useQuery({ queryKey: ["schedule"], queryFn: api.schedule });
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

  const current = data?.scan_cron ?? null;
  const matchedPreset = CRON_PRESETS.some((p) => p.cron === current);

  return (
    <>
      <div className="preset-list">
        {CRON_PRESETS.map((p) => (
          <button
            key={p.label}
            className={current === p.cron ? "preset active" : "preset"}
            onClick={() => save.mutate(p.cron)}
          >
            {p.label}
          </button>
        ))}
      </div>
      <div className="cron-custom">
        <input
          placeholder={current && !matchedPreset ? current : "custom cron, e.g. 30 4 * * *"}
          value={custom}
          onChange={(e) => setCustom(e.target.value)}
        />
        <button className="ghost" disabled={!custom.trim()} onClick={() => save.mutate(custom.trim())}>
          Set
        </button>
      </div>
      {current && !matchedPreset && (
        <p className="muted">
          Currently: <code>{current}</code>
        </p>
      )}
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
  const { data } = useQuery({ queryKey: ["notifications"], queryFn: api.notifications });
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

      {connected ? (
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
    onError: (e: Error) => setMsg(e.message),
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
            autoComplete="current-password"
          />
        )}
        <input
          type="password"
          value={pw}
          onChange={(e) => setPw(e.target.value)}
          placeholder="at least 12 characters"
          autoComplete="new-password"
        />
        <button
          type="submit"
          className="primary sm"
          disabled={pw.length < 8 || (!needed && current.length === 0) || save.isPending}
        >
          Save
        </button>
        {msg && <span className="muted">{msg}</span>}
      </form>
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
