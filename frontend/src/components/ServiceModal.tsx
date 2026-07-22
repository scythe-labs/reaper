// SPDX-License-Identifier: AGPL-3.0-or-later
//
// Add or edit one service, in a modal.
//
// The address is edited as parts -- hostname, port, SSL, optional URL base -- but stored
// as the single base_url the backend has always kept; it is composed and re-split here.
// The API key stays write-only end to end: the edit form shows a blank field, and a blank
// field means "keep the stored key". "Check the server's certificate" is on by default;
// turning it off is a deliberate per-service choice for a self-signed server the operator
// runs themselves, and it only appears once SSL is on (plain http has no certificate).
//
// Sonarr and Radarr also carry "Block re-download after delete" (off by default): whether a
// delete asks the *arr to add an import exclusion so a list can't re-add the title. It is
// wired for Radarr movie deletes; on Sonarr it is stored but inert (Reaper prunes seasons,
// not whole series), and the help text says so rather than pretending otherwise.

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Fragment, useEffect, useState } from "react";
import { api, type Instance, type InstanceTest } from "../api";
import { ModalShell } from "./ModalShell";
import { Switch } from "./Switch";

export const KINDS: {
  value: string;
  label: string;
  hint: string;
  port: string;
  // Only one may be added. Tautulli mirrors a single Plex, and Reaper connects to one Plex,
  // so a second has no working setup. The backend refuses it too; this only hides the add.
  singleton?: boolean;
}[] = [
  { value: "radarr", label: "Radarr", hint: "Your movies. At least one is required.", port: "7878" },
  { value: "sonarr", label: "Sonarr", hint: "Your TV shows. Needed for season pruning.", port: "8989" },
  {
    value: "tautulli",
    label: "Tautulli",
    hint: "Watch history. Required. It's how Reaper knows what's watched.",
    port: "8181",
    singleton: true,
  },
  {
    value: "seerr",
    label: "Seerr",
    hint: "Requests. Lets Reaper show who asked for what.",
    port: "5055",
  },
];

export function kindLabel(kind: string): string {
  return KINDS.find((k) => k.value === kind)?.label ?? kind;
}

/** A small inline pill reporting the result of a connection test. */
export function TestBadge({ result }: { result: InstanceTest | null }) {
  if (!result) return null;
  return (
    <span className={`test-badge ${result.ok ? "ok" : "bad"}`}>
      {result.ok ? "✓ " : "✗ "}
      {result.detail}
      {result.version && ` (v${result.version})`}
    </span>
  );
}

interface UrlParts {
  ssl: boolean;
  host: string;
  port: string; // "" means the scheme default
  urlBase: string; // "" or "/path"
}

/** Split a stored base_url into editable parts. Never throws: a value that cannot be
 * parsed lands whole in the hostname field, where the operator can see and fix it. */
export function splitBaseUrl(raw: string): UrlParts {
  const trimmed = raw.trim();
  let url: URL | null;
  try {
    url = new URL(trimmed);
  } catch {
    url = null;
  }
  if (url === null || !url.hostname || (url.protocol !== "http:" && url.protocol !== "https:")) {
    // Legacy or hand-typed values without a scheme ("host:8989/path") still split cleanly.
    try {
      url = new URL(`http://${trimmed}`);
    } catch {
      url = null;
    }
  }
  if (url === null || !url.hostname) return { ssl: false, host: trimmed, port: "", urlBase: "" };
  const path = url.pathname.replace(/\/+$/, "");
  return {
    ssl: url.protocol === "https:",
    host: url.hostname,
    port: url.port,
    urlBase: path === "/" ? "" : path,
  };
}

/** Compose the parts back into the stored base_url. The scheme's default port is left
 * off, so an address that never mentioned a port round-trips unchanged. */
export function joinBaseUrl(parts: UrlParts): string {
  const scheme = parts.ssl ? "https" : "http";
  const host = parts.host.trim().replace(/\/+$/, "");
  const port = parts.port.trim();
  const defaultPort = parts.ssl ? "443" : "80";
  let base = parts.urlBase.trim();
  if (base && !base.startsWith("/")) base = `/${base}`;
  base = base.replace(/\/+$/, "");
  const portPart = port && port !== defaultPort ? `:${port}` : "";
  return `${scheme}://${host}${portPart}${base}`;
}

export function ServiceModal({
  kind,
  instance,
  onClose,
}: {
  kind: string;
  instance: Instance | null;
  onClose: () => void;
}) {
  const queryClient = useQueryClient();
  const editing = instance !== null;
  const meta = KINDS.find((k) => k.value === kind);
  const initial = instance ? splitBaseUrl(instance.base_url) : null;

  const [name, setName] = useState(instance?.name ?? "");
  const [host, setHost] = useState(initial?.host ?? "");
  const [port, setPort] = useState(initial ? initial.port : (meta?.port ?? ""));
  const [urlBase, setUrlBase] = useState(initial?.urlBase ?? "");
  const [ssl, setSsl] = useState(initial?.ssl ?? false);
  const [verifyCert, setVerifyCert] = useState(instance?.verify_tls ?? true);
  // Whether a delete through this instance adds an import exclusion. Off by default, and
  // only shown for the *arr (movies/shows) -- Tautulli and Seerr never delete.
  const [addExclusion, setAddExclusion] = useState(instance?.add_import_exclusion ?? false);
  const [enabled, setEnabled] = useState(instance?.enabled ?? true);
  const [apiKey, setApiKey] = useState("");
  const [test, setTest] = useState<InstanceTest | null>(null);
  const [error, setError] = useState<string | null>(null);

  const baseUrl = () => joinBaseUrl({ ssl, host, port, urlBase });
  // Only Sonarr and Radarr delete, so only they carry the re-download switch.
  const isArr = kind === "radarr" || kind === "sonarr";

  // The HD/4K library map: which Plex library each of this instance's root folders lands in.
  // Only for a saved *arr, whose folders we can read. `suggestedRoots` marks the rows still
  // holding an auto-suggested value the operator has not yet confirmed by picking.
  const [libMap, setLibMap] = useState<Record<string, string>>(instance?.plex_library_map ?? {});
  const [suggestedRoots, setSuggestedRoots] = useState<Set<string>>(new Set());
  const mapEditable = editing && isArr;
  const libKind = kind === "sonarr" ? "show" : "movie";

  const rootFolders = useQuery({
    queryKey: ["instance-root-folders", instance?.id],
    queryFn: () => api.instanceRootFolders(instance!.id),
    enabled: mapEditable,
  });
  const plexLibraries = useQuery({
    queryKey: ["plexLibraries"],
    queryFn: api.plexLibraries,
    enabled: mapEditable,
  });
  const libOptions = (plexLibraries.data ?? []).filter((l) => l.kind === libKind);

  // Prefill each unmapped folder with its suggested library, marked "suggested" until the
  // operator confirms it. A folder already in the stored map is left as saved, never
  // overwritten by a suggestion, and never marked. Keyed on the folder list's identity.
  const savedMap = instance?.plex_library_map ?? {};
  useEffect(() => {
    const folders = rootFolders.data;
    if (!folders) return;
    setLibMap((prev) => {
      const next = { ...prev };
      for (const f of folders) {
        if (!(f.path in next) && f.suggested_library) next[f.path] = f.suggested_library;
      }
      return next;
    });
    setSuggestedRoots((prev) => {
      const next = new Set(prev);
      for (const f of folders) {
        if (!(f.path in savedMap) && f.suggested_library) next.add(f.path);
      }
      return next;
    });
    // savedMap is derived from the immutable `instance` prop, so the folder data drives this.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [rootFolders.data]);

  const setFolderLibrary = (path: string, library: string) => {
    setLibMap((m) => ({ ...m, [path]: library }));
    // Picking (even the same value) confirms the row, so the "suggested" tag clears.
    setSuggestedRoots((s) => {
      const next = new Set(s);
      next.delete(path);
      return next;
    });
  };

  // A full URL pasted into the hostname field is split across the fields instead of
  // being stored as a "hostname" that silently contains a scheme and port.
  const onHostChange = (value: string) => {
    if (value.includes("://")) {
      const parts = splitBaseUrl(value);
      if (parts.host && parts.host !== value.trim()) {
        setSsl(parts.ssl);
        setHost(parts.host);
        setPort(parts.port);
        if (parts.urlBase) setUrlBase(parts.urlBase);
        return;
      }
    }
    setHost(value);
  };

  const invalidate = () => {
    void queryClient.invalidateQueries({ queryKey: ["instances"] });
    void queryClient.invalidateQueries({ queryKey: ["setup"] });
  };

  const testConn = useMutation({
    mutationFn: () =>
      api.testInstance({
        kind,
        base_url: baseUrl(),
        api_key: apiKey,
        verify_tls: ssl ? verifyCert : true,
      }),
    onSuccess: setTest,
    onError: (e: Error) => setError(e.message),
  });

  const save = useMutation({
    mutationFn: () => {
      if (instance) {
        const body: {
          name: string;
          base_url: string;
          enabled: boolean;
          api_key?: string;
          verify_tls?: boolean;
          add_import_exclusion?: boolean;
          plex_library_map?: Record<string, string>;
        } = { name, base_url: baseUrl(), enabled };
        if (apiKey) body.api_key = apiKey; // blank keeps the stored key
        if (ssl) body.verify_tls = verifyCert; // over plain http the setting is moot; keep it stored
        if (isArr) body.add_import_exclusion = addExclusion;
        // Only send the map when we have the authoritative folder list: build it from the
        // current folders (dropping any stale ones) and their non-empty picks. If the folders
        // could not be read, omit it entirely so the stored map is preserved, never cleared.
        if (mapEditable && rootFolders.data) {
          const map: Record<string, string> = {};
          for (const f of rootFolders.data) {
            const chosen = libMap[f.path];
            if (chosen) map[f.path] = chosen;
          }
          body.plex_library_map = map;
        }
        return api.updateInstance(instance.id, body);
      }
      const createBody: {
        kind: string;
        name: string;
        base_url: string;
        api_key: string;
        verify_tls?: boolean;
        add_import_exclusion?: boolean;
      } = { kind, name, base_url: baseUrl(), api_key: apiKey, verify_tls: ssl ? verifyCert : true };
      if (isArr) createBody.add_import_exclusion = addExclusion;
      return api.createInstance(createBody);
    },
    onSuccess: () => {
      invalidate();
      onClose();
    },
    onError: (e: Error) => setError(e.message),
  });

  const canTest = host.trim() !== "" && apiKey.trim() !== "" && !testConn.isPending;
  const ready =
    name.trim() !== "" && host.trim() !== "" && (editing || apiKey.trim() !== "");

  return (
    <ModalShell
      title={
        <>
          <span className={`kind-badge kind-${kind}`}>{kindLabel(kind)}</span>{" "}
          {editing ? `Edit ${instance.name}` : `Add a ${kindLabel(kind)}`}
        </>
      }
      onClose={onClose}
      className="service-modal"
    >
      <form
        className="service-form"
        onSubmit={(e) => {
          e.preventDefault();
          setError(null);
          save.mutate();
        }}
      >
        <label className="field-sm">
          <span className="field-label">Name</span>
          <input
            value={name}
            onChange={(e) => setName(e.target.value)}
            placeholder={kind === "tautulli" || kind === "seerr" ? "Main" : "HD"}
          />
        </label>
        <div className="host-row">
          <label className="field-sm">
            <span className="field-label">Hostname or IP</span>
            <span className="url-join">
              <span className="url-scheme">{ssl ? "https://" : "http://"}</span>
              <input
                value={host}
                onChange={(e) => onHostChange(e.target.value)}
                placeholder="192.168.1.10"
              />
            </span>
          </label>
          <label className="field-sm">
            <span className="field-label">Port</span>
            <input
              value={port}
              inputMode="numeric"
              onChange={(e) => setPort(e.target.value.replace(/[^0-9]/g, "").slice(0, 5))}
              placeholder={ssl ? "443" : "80"}
            />
          </label>
        </div>
        <label className="field-sm">
          <span className="field-label">URL base</span>
          <input
            value={urlBase}
            onChange={(e) => setUrlBase(e.target.value)}
            placeholder="only if it lives under a path, like /sonarr"
          />
        </label>
        <label className="toggle">
          <Switch checked={ssl} onChange={setSsl} />
          <span>Use SSL</span>
        </label>
        {ssl && (
          <>
            <label className="toggle">
              <Switch checked={verifyCert} onChange={setVerifyCert} />
              <span>Check the server's certificate</span>
            </label>
            {!verifyCert && (
              <p className="notice notice-warn">
                Reaper will accept this server's certificate without checking who issued
                it. Only use this for a server you run yourself, like one with a
                self-signed certificate.
              </p>
            )}
          </>
        )}
        {isArr && (
          <>
            <label className="toggle">
              <Switch checked={addExclusion} onChange={setAddExclusion} />
              <span>Block re-download after delete</span>
            </label>
            <p className="help">
              {kind === "sonarr"
                ? "This only applies when Reaper removes a whole show. Today it removes seasons, not whole shows, so your choice is saved but not used yet."
                : addExclusion
                  ? "When Reaper removes a movie, it adds a Radarr list exclusion so an import list can't add it back and re-download it."
                  : "A deleted movie can be added back by a list and re-downloaded. Reaper won't add or check the exclusion when it removes one."}
            </p>
          </>
        )}
        <label className="field-sm">
          <span className="field-label">{editing ? "New API key" : "API key"}</span>
          <input
            type="password"
            value={apiKey}
            onChange={(e) => setApiKey(e.target.value)}
            placeholder={
              editing ? "leave blank to keep the current key" : "from the service's settings"
            }
            autoComplete="off"
          />
        </label>
        {mapEditable && (
          <div className="field-sm plex-map">
            <span className="field-label">Plex libraries</span>
            {rootFolders.isPending ? (
              <p className="help">Reading this instance's folders…</p>
            ) : rootFolders.error ? (
              <p className="notice notice-warn">
                Reaper couldn't read this instance's folders. Test the connection above, then
                reopen this to map them.
              </p>
            ) : rootFolders.data && rootFolders.data.length > 0 ? (
              <>
                <div className="plex-map-grid">
                  {rootFolders.data.map((f) => (
                    <Fragment key={f.path}>
                      <div className="pl-root">{f.path}</div>
                      <div className="pl-pick">
                        <select
                          className={`pl-select${libMap[f.path] ? "" : " unset"}`}
                          value={libMap[f.path] ?? ""}
                          onChange={(e) => setFolderLibrary(f.path, e.target.value)}
                        >
                          <option value="">Not set</option>
                          {libOptions.map((l) => (
                            <option key={l.key} value={l.title}>
                              {l.title}
                            </option>
                          ))}
                        </select>
                        {suggestedRoots.has(f.path) && <span className="pl-suggested">suggested</span>}
                      </div>
                    </Fragment>
                  ))}
                </div>
                {!plexLibraries.isPending && libOptions.length === 0 ? (
                  <p className="help">
                    No Plex libraries yet. Sync them in Plex settings first, then pick one per folder.
                  </p>
                ) : (
                  <p className="help">
                    Which Plex library each folder lands in. This tells an HD copy from a 4K one
                    when the same title is in two libraries. Matches are suggested from your
                    folders. Leave a folder on "Not set" to keep both copies when they can't be
                    told apart.
                  </p>
                )}
              </>
            ) : (
              <p className="help">This instance reports no root folders to map.</p>
            )}
          </div>
        )}
        {editing && (
          <label className="toggle">
            <Switch checked={enabled} onChange={setEnabled} />
            <span>Enabled</span>
          </label>
        )}
        {meta && <p className="help">{meta.hint}</p>}
        {error && <p className="notice notice-error">{error}</p>}
        {test && (
          <div className="instance-status">
            <TestBadge result={test} />
          </div>
        )}
        <div className="add-actions">
          {!editing && (
            <button
              type="button"
              className="ghost"
              disabled={!canTest}
              onClick={() => {
                setError(null);
                testConn.mutate();
              }}
            >
              {testConn.isPending ? "Testing…" : "Test connection"}
            </button>
          )}
          <span className="flex-spacer" />
          <button type="button" className="ghost" onClick={onClose}>
            Cancel
          </button>
          <button type="submit" className="primary" disabled={!ready || save.isPending}>
            {save.isPending ? (editing ? "Saving…" : "Adding…") : editing ? "Save" : "Add service"}
          </button>
        </div>
      </form>
    </ModalShell>
  );
}
