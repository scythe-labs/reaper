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
import { api, type Instance, type InstanceTest, type PlexServerChoice } from "../api";
import { date } from "../format";
import { ScanBar } from "./ScanBar";

type Panel = "services" | "plex" | "jobs" | "notifications" | "security";

const PANELS: { id: Panel; label: string }[] = [
  { id: "services", label: "Services" },
  { id: "plex", label: "Plex" },
  { id: "jobs", label: "Jobs" },
  { id: "notifications", label: "Notifications" },
  { id: "security", label: "Security" },
];

const KINDS: { value: string; label: string; hint: string }[] = [
  { value: "radarr", label: "Radarr", hint: "Your movies. At least one is required." },
  { value: "sonarr", label: "Sonarr", hint: "Your TV shows. Needed for season pruning." },
  { value: "tautulli", label: "Tautulli", hint: "Watch history. Required. It's how Reaper knows what's watched." },
  { value: "seerr", label: "Seerr", hint: "Requests. Lets Reaper show who asked for what." },
];

function kindLabel(kind: string): string {
  return KINDS.find((k) => k.value === kind)?.label ?? kind;
}

/** A small inline pill reporting the result of a connection test. */
function TestBadge({ result }: { result: InstanceTest | null }) {
  if (!result) return null;
  return (
    <span className={`test-badge ${result.ok ? "ok" : "bad"}`}>
      {result.ok ? "✓ " : "✗ "}
      {result.detail}
      {result.version && ` (v${result.version})`}
    </span>
  );
}

// --- Services --------------------------------------------------------------

function AddInstance() {
  const queryClient = useQueryClient();
  const [kind, setKind] = useState("radarr");
  const [name, setName] = useState("");
  const [baseUrl, setBaseUrl] = useState("");
  const [apiKey, setApiKey] = useState("");
  const [test, setTest] = useState<InstanceTest | null>(null);
  const [error, setError] = useState<string | null>(null);

  const reset = () => {
    setName("");
    setBaseUrl("");
    setApiKey("");
    setTest(null);
  };

  const testConn = useMutation({
    mutationFn: () => api.testInstance({ kind, base_url: baseUrl, api_key: apiKey }),
    onSuccess: setTest,
    onError: (e: Error) => setError(e.message),
  });
  const create = useMutation({
    mutationFn: () => api.createInstance({ kind, name, base_url: baseUrl, api_key: apiKey }),
    onSuccess: () => {
      reset();
      void queryClient.invalidateQueries({ queryKey: ["instances"] });
      void queryClient.invalidateQueries({ queryKey: ["setup"] });
    },
    onError: (e: Error) => setError(e.message),
  });

  const hint = KINDS.find((k) => k.value === kind)?.hint;
  const ready = name.trim() && baseUrl.trim() && apiKey.trim();

  return (
    <form
      className="add-instance"
      onSubmit={(e) => {
        e.preventDefault();
        setError(null);
        create.mutate();
      }}
    >
      <h3>Add a service</h3>
      <div className="add-grid">
        <label className="field-sm">
          <span className="field-label">Type</span>
          <select value={kind} onChange={(e) => setKind(e.target.value)}>
            {KINDS.map((k) => (
              <option key={k.value} value={k.value}>
                {k.label}
              </option>
            ))}
          </select>
        </label>
        <label className="field-sm">
          <span className="field-label">Name</span>
          <input
            value={name}
            onChange={(e) => setName(e.target.value)}
            placeholder={kind === "radarr" ? "HD" : "Main"}
          />
        </label>
        <label className="field-sm wide">
          <span className="field-label">Address</span>
          <input
            value={baseUrl}
            onChange={(e) => setBaseUrl(e.target.value)}
            placeholder="http://192.168.1.10:7878"
          />
        </label>
        <label className="field-sm wide">
          <span className="field-label">API key</span>
          <input
            type="password"
            value={apiKey}
            onChange={(e) => setApiKey(e.target.value)}
            placeholder="from the service's settings"
            autoComplete="off"
          />
        </label>
      </div>
      {hint && <p className="help">{hint}</p>}
      <div className="add-actions">
        <button
          type="button"
          className="ghost"
          disabled={!baseUrl.trim() || !apiKey.trim() || testConn.isPending}
          onClick={() => {
            setError(null);
            testConn.mutate();
          }}
        >
          {testConn.isPending ? "Testing…" : "Test connection"}
        </button>
        <button type="submit" className="primary" disabled={!ready || create.isPending}>
          {create.isPending ? "Adding…" : "Add service"}
        </button>
        <TestBadge result={test} />
      </div>
      {error && <p className="notice notice-error">{error}</p>}
    </form>
  );
}

function InstanceRow({ instance }: { instance: Instance }) {
  const queryClient = useQueryClient();
  const [editing, setEditing] = useState(false);
  const [name, setName] = useState(instance.name);
  const [baseUrl, setBaseUrl] = useState(instance.base_url);
  const [apiKey, setApiKey] = useState("");
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
  const save = useMutation({
    mutationFn: () => {
      const body: { name: string; base_url: string; api_key?: string } = {
        name,
        base_url: baseUrl,
      };
      if (apiKey) body.api_key = apiKey; // omit entirely when blank -> keep stored key
      return api.updateInstance(instance.id, body);
    },
    onSuccess: () => {
      setEditing(false);
      setApiKey("");
      invalidate();
    },
  });
  const toggle = useMutation({
    mutationFn: () => api.updateInstance(instance.id, { enabled: !instance.enabled }),
    onSuccess: invalidate,
  });
  const remove = useMutation({
    mutationFn: () => api.deleteInstance(instance.id),
    onSuccess: invalidate,
  });

  return (
    <li className={`instance-row ${instance.enabled ? "" : "disabled"}`}>
      <div className="instance-main">
        <div className="instance-id">
          <span className={`kind-badge kind-${instance.kind}`}>{kindLabel(instance.kind)}</span>
          <strong>{instance.name}</strong>
          {!instance.enabled && <span className="chip">disabled</span>}
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

      <div className="instance-actions">
        <button className="ghost sm" disabled={testSaved.isPending} onClick={() => testSaved.mutate()}>
          {testSaved.isPending ? "Testing…" : "Test"}
        </button>
        <button className="ghost sm" onClick={() => toggle.mutate()}>
          {instance.enabled ? "Disable" : "Enable"}
        </button>
        <button className="ghost sm" onClick={() => setEditing((v) => !v)}>
          Edit
        </button>
        {confirmingRemove ? (
          <>
            <button
              className="ghost sm danger"
              title="Only forgets it in Reaper. Nothing is changed in the service itself."
              onClick={() => {
                setConfirmingRemove(false);
                remove.mutate();
              }}
            >
              Confirm remove
            </button>
            <button className="ghost sm" onClick={() => setConfirmingRemove(false)}>
              Cancel
            </button>
          </>
        ) : (
          <button className="ghost sm danger" onClick={() => setConfirmingRemove(true)}>
            Remove
          </button>
        )}
      </div>

      {editing && (
        <form
          className="instance-edit"
          onSubmit={(e) => {
            e.preventDefault();
            save.mutate();
          }}
        >
          <label className="field-sm">
            <span className="field-label">Name</span>
            <input value={name} onChange={(e) => setName(e.target.value)} />
          </label>
          <label className="field-sm wide">
            <span className="field-label">Address</span>
            <input value={baseUrl} onChange={(e) => setBaseUrl(e.target.value)} />
          </label>
          <label className="field-sm wide">
            <span className="field-label">New API key</span>
            <input
              type="password"
              value={apiKey}
              onChange={(e) => setApiKey(e.target.value)}
              placeholder="leave blank to keep the current key"
              autoComplete="off"
            />
          </label>
          <div className="add-actions">
            <button type="submit" className="primary sm" disabled={save.isPending}>
              Save
            </button>
            <button type="button" className="ghost sm" onClick={() => setEditing(false)}>
              Cancel
            </button>
          </div>
        </form>
      )}
    </li>
  );
}

export function ServicesPanel() {
  const { data, isPending, error } = useQuery({ queryKey: ["instances"], queryFn: api.instances });
  return (
    <div className="panel">
      <h2>Services</h2>
      <p className="blurb">
        The apps Reaper reads from. It only ever reads. Nothing here can delete a file.
      </p>
      {error && <p className="notice notice-error">{(error as Error).message}</p>}
      {isPending && <p className="muted">Loading…</p>}
      {data && data.length === 0 && (
        <p className="empty">No services yet. Add Radarr and Tautulli below to get started.</p>
      )}
      {data && data.length > 0 && (
        <ul className="instance-list">
          {data.map((i) => (
            <InstanceRow key={i.id} instance={i} />
          ))}
        </ul>
      )}
      <AddInstance />
    </div>
  );
}

// --- Plex ------------------------------------------------------------------

export function PlexPanel() {
  const queryClient = useQueryClient();
  const { data } = useQuery({ queryKey: ["plex"], queryFn: api.plexStatus });
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

  useEffect(() => () => (pollRef.current ? clearInterval(pollRef.current) : undefined), []);

  const saveWebUrl = useMutation({
    mutationFn: () => api.setPlexWebUrl(webUrl.trim()),
    onSuccess: () => {
      setWebUrlError(null);
      void queryClient.invalidateQueries({ queryKey: ["plex"] });
    },
    onError: (e: Error) => setWebUrlError(e.message),
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
        const poll = await api.plexLinkPoll(pinId, machineId);
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
      const poll = await api.plexLinkPoll(pinId, machineId);
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

  return (
    <div className="panel">
      <h2>Plex</h2>
      <p className="blurb">
        Linking Plex lets Reaper mark items "Leaving Soon" during their grace period and read
        your "Never Reap" collection. It's optional. Scanning works without it.
      </p>
      {data?.linked ? (
        <div className="plex-status linked">
          <div>
            <strong>{data.name}</strong>
            <div className="muted">{data.connection_uri}</div>
          </div>
          <button className="ghost" onClick={() => unlink.mutate()} disabled={unlink.isPending}>
            Unlink
          </button>
        </div>
      ) : servers ? (
        <div className="server-pick">
          <strong>Which server should Reaper manage?</strong>
          <p className="muted">
            This account owns more than one Plex server. Reaper will only ever scan and
            prune the one you pick.
          </p>
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
      ) : (
        <div className="plex-status">
          <p className="muted">No Plex server linked.</p>
          <button className="btn-plex" onClick={startLink} disabled={linking}>
            {linking ? "Waiting for Plex…" : "Link with Plex"}
          </button>
        </div>
      )}
      {message && <p className="muted">{message}</p>}

      <div className="add-grid">
        <label className="field-sm wide">
          <span className="field-label">Plex web address</span>
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
        </label>
      </div>
      <p className="help">
        Where links to your Plex library open. Keep the default unless you host your own
        Plex Web. Clear the box and save to go back to the default.
      </p>
      <div className="add-actions">
        <button
          type="button"
          className="primary"
          disabled={saveWebUrl.isPending || webUrl.trim() === savedWebUrl}
          onClick={() => saveWebUrl.mutate()}
        >
          {saveWebUrl.isPending ? "Saving…" : "Save"}
        </button>
      </div>
      {webUrlError && <p className="error">{webUrlError}</p>}
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
  const [pw, setPw] = useState("");
  const [msg, setMsg] = useState<string | null>(null);
  const save = useMutation({
    mutationFn: () => api.setAdminPassword(pw),
    onSuccess: () => {
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
        <p className="help">Choose something long, and keep it somewhere safe.</p>
      </div>
      <form
        className="pw-form"
        onSubmit={(e) => {
          e.preventDefault();
          setMsg(null);
          save.mutate();
        }}
      >
        <input
          type="password"
          value={pw}
          onChange={(e) => setPw(e.target.value)}
          placeholder="at least 8 characters"
          autoComplete="new-password"
        />
        <button type="submit" className="primary sm" disabled={pw.length < 8 || save.isPending}>
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

export function Settings() {
  const [panel, setPanel] = useState<Panel>("services");
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
        {panel === "services" && <ServicesPanel />}
        {panel === "plex" && <PlexPanel />}
        {panel === "jobs" && <JobsPanel />}
        {panel === "notifications" && <NotificationsPanel />}
        {panel === "security" && <SecurityPanel />}
      </div>
    </div>
  );
}
