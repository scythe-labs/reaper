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

import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
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
  const [enabled, setEnabled] = useState(instance?.enabled ?? true);
  const [apiKey, setApiKey] = useState("");
  const [test, setTest] = useState<InstanceTest | null>(null);
  const [error, setError] = useState<string | null>(null);

  const baseUrl = () => joinBaseUrl({ ssl, host, port, urlBase });

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
        } = { name, base_url: baseUrl(), enabled };
        if (apiKey) body.api_key = apiKey; // blank keeps the stored key
        if (ssl) body.verify_tls = verifyCert; // over plain http the setting is moot; keep it stored
        return api.updateInstance(instance.id, body);
      }
      return api.createInstance({
        kind,
        name,
        base_url: baseUrl(),
        api_key: apiKey,
        verify_tls: ssl ? verifyCert : true,
      });
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
