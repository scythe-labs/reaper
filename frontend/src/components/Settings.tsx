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
import { KINDS, kindLabel, ServiceModal, TestBadge } from "./ServiceModal";

type Panel = "services" | "plex" | "jobs" | "notifications" | "security";

const PANELS: { id: Panel; label: string }[] = [
  { id: "services", label: "Services" },
  { id: "plex", label: "Plex" },
  { id: "jobs", label: "Jobs" },
  { id: "notifications", label: "Notifications" },
  { id: "security", label: "Security" },
];

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
