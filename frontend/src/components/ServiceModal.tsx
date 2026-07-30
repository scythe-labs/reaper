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
import { Fragment, useEffect, useState, type RefObject } from "react";
import { announce } from "../announce";
import { api, type Instance, type InstanceTest, type SeerrService } from "../api";
import { ModalShell } from "./ModalShell";
import { Switch } from "./Switch";
import { Notice } from "./Notice";
import { StaleReadNotice } from "./StaleReadNotice";

export const KINDS: {
  value: string;
  label: string;
  hint: string;
  port: string;
  // Only one may be added. Tautulli mirrors a single Plex, and Reaper connects to one Plex,
  // so a second has no working setup. The backend refuses it too; this only hides the add.
  singleton?: boolean;
}[] = [
  {
    value: "radarr",
    label: "Radarr",
    hint: "Your movies. At least one is required.",
    port: "7878",
  },
  {
    value: "sonarr",
    label: "Sonarr",
    hint: "Your TV shows. Needed for season pruning.",
    port: "8989",
  },
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

/** What a connection test SAYS, written once because two surfaces state it.
 *
 *  `TestBadge` renders it for whoever navigates onto the badge, and every test mutation
 *  announces it for whoever does not (#192). Deriving both from here is rule 144's whole point:
 *  one fact, and the copy that is spoken cannot drift away from the copy that is read.
 *
 *  `detail` is already a whole sentence from the server ("Connected to Sonarr.", or an explained
 *  failure), so the lead is the only thing added -- for the reason it was added to the badge in
 *  the first place, that "Couldn't reach it" read as a result rather than as a failure.
 *
 *  The one deliberate difference from the badge: the version is spoken as "version 4.0.1" where
 *  the badge shows "(v4.0.1)". A reader voices a bare "v" as a letter. */
export function testSentence(result: InstanceTest): string {
  return `${testLead(result.ok)}: ${result.detail}${
    result.version ? ` (version ${result.version})` : ""
  }`;
}

/** The word in front of a test's own detail, in both surfaces' hands. */
function testLead(ok: boolean): string {
  return ok ? "Passed" : "Failed";
}

/** A small inline pill reporting the result of a connection test. */
export function TestBadge({ result }: { result: InstanceTest | null }) {
  if (!result) return null;
  return (
    <span className={`test-badge ${result.ok ? "ok" : "bad"}`}>
      {/* Whether Reaper reached your Sonarr used to be a glyph and a color and nothing else:
          "✓" and "✗" are read the same way by a reader at its default symbol level, so the
          detail line that follows ("Reached", or the error) was the only difference. A word
          carries it now, which is the 1.4.1 half: color was doing the work alone.

          The badge still sits in no live region, and it does not need to: the three test
          mutations announce `testSentence` at the moment the result settles, which is 4.1.3's
          actual shape (#192). Announcing from HERE instead would fire on every re-render of a
          result the operator has already heard. */}
      <span className="sr-only">{testLead(result.ok)}: </span>
      <span aria-hidden="true">{result.ok ? "✓ " : "✗ "}</span>
      {result.detail}
      {result.version && ` (v${result.version})`}
    </span>
  );
}

/** The external-URL box's complaint, named once for both ends of the association (rule 67). */
const EXTERNAL_URL_ERROR_ID = "service-external-url-error";

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

/** Whether a value is a full http(s) web address. Mirrors the server's `_validate_external_url`
 * so a scheme-less paste ("host:8989") or a "javascript:" value is caught before save, not only
 * by the 422 (S-5). A `type="url"` input accepts any scheme with a colon, so it is not this check. */
function isWebUrl(value: string): boolean {
  try {
    const u = new URL(value);
    return (u.protocol === "http:" || u.protocol === "https:") && u.hostname !== "";
  } catch {
    return false;
  }
}

export function ServiceModal({
  kind,
  instance,
  onClose,
  savePendingRef,
}: {
  kind: string;
  instance: Instance | null;
  onClose: () => void;
  // Set by ServicesPanel so its Back guard can read the same canClose the scrim/Escape/✕ use,
  // exactly as the schedule editor does (B-11, B-19).
  savePendingRef?: RefObject<boolean>;
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
  // The address links open, when it differs from the one Reaper connects to. Blank means
  // "use the address above": on save a blank value clears the stored one back to null.
  const [externalUrl, setExternalUrl] = useState(instance?.external_url ?? "");
  // A test result, and the exact credentials it was computed against. The two are stored together
  // because nothing used to clear the badge: `setTest` was called only here and in the mutation's
  // `onSuccess`, so passing a test and then editing the hostname or the key left "Reached" on
  // screen vouching for an address that had never been tried (rule 85, #178). Clearing it from
  // each field's setter would be one more thing to remember every time a field joins `baseUrl()`;
  // comparing against what was tested cannot be forgotten.
  const [test, setTest] = useState<{ result: InstanceTest; of: string } | null>(null);
  const [error, setError] = useState<string | null>(null);
  // Kept apart from `error` above, which is the form's shared slot for a failed save and a
  // failed connection test. One flag rather than a message, because there is exactly one thing
  // wrong a malformed external URL can be, and the sentence lives at the field.
  const [extUrlBad, setExtUrlBad] = useState(false);

  const baseUrl = () => joinBaseUrl({ ssl, host, port, urlBase });
  /** Everything `testInstance` is sent, as one string. The badge shows only while this still
   *  matches what was tested, so any edit to an address, a key or a certificate setting takes the
   *  result down with it. Every field here is one the test's answer depends on: add a field to the
   *  request above and add it here too (rule 143). */
  const testedWith = () => [kind, baseUrl(), apiKey, ssl ? verifyCert : true].join(" ");
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
    // The exact key the Plex panel refreshes after a sync (`PlexPanel`). Cached under a
    // second spelling, this list went on offering libraries that had just been removed and
    // hiding ones that had just been added, until the page was reloaded.
    queryKey: ["plex-libraries"],
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

  // The multi-Seerr requester map: which Reaper Sonarr/Radarr instance each of this portal's
  // services adds media to. Only for a saved Seerr. `suggestedServices` marks the rows still
  // holding an auto-suggested value the operator has not confirmed by picking.
  const [serviceMap, setServiceMap] = useState<Record<string, number>>(
    instance?.service_instance_map ?? {},
  );
  const [suggestedServices, setSuggestedServices] = useState<Set<string>>(new Set());
  const seerrMapEditable = editing && kind === "seerr";

  const seerrServices = useQuery({
    queryKey: ["instance-seerr-services", instance?.id],
    queryFn: () => api.instanceSeerrServices(instance!.id),
    enabled: seerrMapEditable,
  });
  const arrInstances = useQuery({
    queryKey: ["instances"],
    queryFn: api.instances,
    enabled: seerrMapEditable,
  });
  const instanceOptions = (svcKind: "sonarr" | "radarr") =>
    (arrInstances.data ?? []).filter((i) => i.kind === svcKind);
  // Seerr numbers Sonarr and Radarr services in SEPARATE lists (both from 0), so the map key
  // must be kind + id, not the id alone -- else a sonarr and a radarr service collide.
  const svcKey = (s: SeerrService) => `${s.kind}:${s.service_id}`;

  // Prefill each unmapped service with its suggested instance, marked "suggested" until the
  // operator confirms it. A service already in the stored map is left as saved, never
  // overwritten by a suggestion, and never marked. Keyed on the service list's identity.
  const savedServiceMap = instance?.service_instance_map ?? {};
  useEffect(() => {
    const services = seerrServices.data;
    if (!services) return;
    setServiceMap((prev) => {
      const next = { ...prev };
      for (const s of services) {
        const key = svcKey(s);
        if (!(key in next) && s.suggested_instance_id != null) next[key] = s.suggested_instance_id;
      }
      return next;
    });
    setSuggestedServices((prev) => {
      const next = new Set(prev);
      for (const s of services) {
        const key = svcKey(s);
        if (!(key in savedServiceMap) && s.suggested_instance_id != null) next.add(key);
      }
      return next;
    });
    // savedServiceMap is derived from the immutable `instance` prop; the service list drives this.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [seerrServices.data]);

  const setServiceInstance = (key: string, instanceId: string) => {
    setServiceMap((m) => {
      const next = { ...m };
      if (instanceId) next[key] = Number(instanceId);
      else delete next[key];
      return next;
    });
    // Picking (even the same value) confirms the row, so the "suggested" tag clears.
    setSuggestedServices((s) => {
      const next = new Set(s);
      next.delete(key);
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
    // Whether Reaper can reach the Sonarr it deletes THROUGH is worth hearing, which is what
    // makes this more than cosmetic. `instances.py` never raises for a failed test, so an
    // unreachable host arrives as a 200 with `ok=False` and never reaches the shared error
    // notice -- the badge was the only report, and it announced nothing (#192).
    onSuccess: (r) => {
      setTest({ result: r, of: testedWith() });
      announce(testSentence(r));
    },
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
          external_url?: string;
          plex_library_map?: Record<string, string>;
          service_instance_map?: Record<string, number>;
          // Always sent (trimmed): a blank value clears the stored external URL back to null,
          // so links fall back to the address above.
        } = { name, base_url: baseUrl(), enabled, external_url: externalUrl.trim() };
        if (apiKey) body.api_key = apiKey; // blank keeps the stored key
        if (ssl) body.verify_tls = verifyCert; // over plain http the setting is moot; keep it stored
        if (isArr) body.add_import_exclusion = addExclusion;
        // Send the map only when a folder list has been read, and PRUNE it only against a list
        // that is current. Rebuilding from the folders in hand is what drops entries for folders
        // the *arr no longer has, which is right -- until the list is merely out of date, and
        // then it drops entries for folders that still exist. The map is what tells an HD copy
        // from a 4K one, and this is the modal's Save doing it silently.
        //
        // The read is stale, not absent, exactly when a refetch failed with the last good list
        // still held: React Query keeps it, the grid below deliberately stays on screen with the
        // stale line over it, and `.data` is truthy throughout -- so testing `.data` cannot tell
        // the two apart (#196 named the comment that claimed it did; this is the guard, #204).
        //
        // On a stale list the operator's own picks still save: withholding those would drop an
        // edit they can see, which is the same silent loss from the other side. What is withheld
        // is the DELETION of entries nothing has confirmed are gone, so both directions fail
        // toward keeping the mapping.
        if (mapEditable && rootFolders.data) {
          const paths = rootFolders.isError
            ? Object.keys(libMap)
            : rootFolders.data.map((f) => f.path);
          const map: Record<string, string> = {};
          for (const path of paths) {
            const chosen = libMap[path];
            if (chosen) map[path] = chosen;
          }
          body.plex_library_map = map;
        }
        // Same contract, same limit, same reasoning for the Seerr service map (rule 72): sent when
        // a service list has been read, pruned only against one that is current, and omitted only
        // when none ever landed.
        if (seerrMapEditable && seerrServices.data) {
          const keys = seerrServices.isError
            ? Object.keys(serviceMap)
            : seerrServices.data.map((s) => svcKey(s));
          const map: Record<string, number> = {};
          for (const key of keys) {
            const chosen = serviceMap[key];
            if (chosen) map[key] = chosen;
          }
          body.service_instance_map = map;
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
        external_url?: string;
      } = { kind, name, base_url: baseUrl(), api_key: apiKey, verify_tls: ssl ? verifyCert : true };
      if (isArr) createBody.add_import_exclusion = addExclusion;
      if (externalUrl.trim()) createBody.external_url = externalUrl.trim();
      return api.createInstance(createBody);
    },
    onSuccess: () => {
      // The modal closing was the entire success signal, and it takes the focused button with
      // it. Named for which of the two things just happened, because the operator gets back a
      // list they now have to find the row in.
      announce(instance ? `${name} saved.` : `${name} added.`);
      invalidate();
      onClose();
    },
    onError: (e: Error) => setError(e.message),
  });

  // Mirror the save's pending state up to ServicesPanel's Back guard, and clear it on unmount
  // so a stale true never lingers after the modal closes (B-11, B-19).
  useEffect(() => {
    if (savePendingRef) savePendingRef.current = save.isPending;
    return () => {
      if (savePendingRef) savePendingRef.current = false;
    };
  }, [save.isPending, savePendingRef]);

  const canTest = host.trim() !== "" && apiKey.trim() !== "" && !testConn.isPending;
  const ready = name.trim() !== "" && host.trim() !== "" && (editing || apiKey.trim() !== "");

  return (
    <ModalShell
      title={
        <>
          <span className={`kind-badge kind-${kind}`}>{kindLabel(kind)}</span>{" "}
          {editing ? `Edit ${instance.name}` : `Add a ${kindLabel(kind)}`}
        </>
      }
      onClose={onClose}
      // A close mid-save unmounts the only place the failure is ever shown: the scrim swallows
      // a 409 "a service with that name already exists", `invalidate()` never runs, and the
      // operator walks away believing the change saved (B-19).
      canClose={!save.isPending}
      className="service-modal"
    >
      <form
        className="service-form"
        onSubmit={(e) => {
          e.preventDefault();
          setError(null);
          const ext = externalUrl.trim();
          if (ext && !isWebUrl(ext)) {
            setExtUrlBad(true);
            return;
          }
          setExtUrlBad(false);
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
              <Notice tone="warn">
                Reaper will accept this server's certificate without checking who issued it. Only
                use this for a server you run yourself, like one with a self-signed certificate.
              </Notice>
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
        <label className="field-sm">
          <span className="field-label">External URL</span>
          <input
            type="url"
            value={externalUrl}
            // Cleared as soon as it is being fixed, so the box never claims a value the
            // operator has already retyped is still the one that was refused.
            onChange={(e) => {
              setExternalUrl(e.target.value);
              setExtUrlBad(false);
            }}
            placeholder={`https://${kind}.example.com`}
            autoComplete="off"
            aria-invalid={extUrlBad ? true : undefined}
            aria-describedby={extUrlBad ? EXTERNAL_URL_ERROR_ID : undefined}
          />
        </label>
        {/* Beside the control that fixes it (rule 42). This complaint used to be written into
            the form's one shared `error` slot, which renders after ~150 lines of other fields
            and is also where a failed save and a failed connection test land -- so it was both
            out of reach of the box and impossible to bind to it without claiming a save failure
            was the URL's fault. */}
        {extUrlBad && (
          <Notice tone="error" id={EXTERNAL_URL_ERROR_ID}>
            The external URL must be a full web address, like https://192.0.2.10:8989.
          </Notice>
        )}
        <p className="help">
          Where links to {kindLabel(kind)} open. Leave blank to use the address above.
        </p>
        {mapEditable && (
          <div className="field-sm plex-map">
            <span className="field-label">Plex libraries</span>
            {/* Divided on whether the folder list ever landed: any refetch while the modal is
                open reaches this, and an undivided error traded the whole mapping grid for one
                warning while React Query still held the folders (#190). */}
            {rootFolders.isPending ? (
              <p className="help">Reading this instance's folders…</p>
            ) : rootFolders.error && !rootFolders.data ? (
              /* Names the boxes, not the Test button: this block needs `mapEditable`, so it only
                 ever renders while editing, and the Test button renders under `!editing`. The copy
                 sent the operator to a control that is nowhere on screen in the only mode it
                 appears in (rule 25, #178). Its twin below says the same thing. */
              <Notice tone="warn">
                Reaper couldn't read this instance's folders. Check the address and key above, then
                reopen this to map them.
              </Notice>
            ) : rootFolders.data && rootFolders.data.length > 0 ? (
              <>
                {rootFolders.error && <StaleReadNotice what="this instance's folders" />}
                {/* Over the grid, beside the folder line, because the shared sentence says what
                    is BELOW may be out of date and the stale library names are inside the pickers
                    under it. Emitted after the grid it pointed at the help paragraph instead,
                    which is the placement `Settings.tsx` fixed for the jobs rows and says every
                    other call site already keeps.

                    `libOptions.length === 0` is what tells the two failures apart: a refetch that
                    fails with the libraries still held leaves every picker below populated, and
                    "couldn't read your Plex libraries" printed there sat beside a working list
                    (#190). Said as staleness instead, which is what it is. */}
                {plexLibraries.error && libOptions.length > 0 && (
                  <StaleReadNotice what="your Plex libraries" />
                )}
                <div className="plex-map-grid">
                  {rootFolders.data.map((f) => (
                    <Fragment key={f.path}>
                      <div className="pl-root">{f.path}</div>
                      <div className="pl-pick">
                        <select
                          className={`pl-select${libMap[f.path] ? "" : " unset"}`}
                          // The folder name is the only thing telling these rows apart, and it
                          // lives in a sibling cell the select is not labeled by. Without this
                          // a screen reader announces every row as "combobox, Not set".
                          aria-label={`Plex library for ${f.path}`}
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
                        {suggestedRoots.has(f.path) && (
                          <span className="pl-suggested">suggested</span>
                        )}
                      </div>
                    </Fragment>
                  ))}
                </div>
                {/* A failed fetch also empties `libOptions`, and "no libraries yet" would then
                    state as fact something we never learned -- and send the operator off to
                    re-sync a list that is already there. The empty sentence is only for a list
                    we genuinely read and found empty. This one stays under the grid: it replaces
                    the help text rather than describing the pickers. */}
                {plexLibraries.error && libOptions.length === 0 ? (
                  <Notice tone="warn">Reaper couldn't read your Plex libraries. Try again.</Notice>
                ) : !plexLibraries.isPending && libOptions.length === 0 ? (
                  <p className="help">
                    No Plex libraries yet. Sync them in Plex settings first, then pick one per
                    folder.
                  </p>
                ) : (
                  <p className="help">
                    Which Plex library each folder lands in. This tells an HD copy from a 4K one
                    when the same title is in two libraries. Matches are suggested from your
                    folders. Leave a folder on "Not set" to keep both copies when they can't be told
                    apart.
                  </p>
                )}
              </>
            ) : (
              // A list that landed EMPTY and then failed to refetch reaches this arm, not the
              // never-landed one above: `[]` is truthy, so `!rootFolders.data` is false. The
              // sentence below is a positive claim about the instance, so it takes the stale
              // line like every other held value -- without it, the one state where the read
              // failed AND the app has something to say rendered no warning at all.
              <>
                {rootFolders.error && <StaleReadNotice what="this instance's folders" />}
                <p className="help">This instance reports no root folders to map.</p>
              </>
            )}
          </div>
        )}
        {seerrMapEditable && (
          <div className="field-sm plex-map">
            <span className="field-label">Requested-by instances</span>
            {/* Divided exactly as the folder grid above (rule 72). */}
            {seerrServices.isPending ? (
              <p className="help">Reading this portal's services…</p>
            ) : seerrServices.error && !seerrServices.data ? (
              /* Same correction as the folder notice above (rule 72): no Test button exists in
                 edit mode, which is the only mode this renders in. */
              <Notice tone="warn">
                Reaper couldn't read this portal's services. Check the address and key above (it
                needs an admin key), then reopen this to map them.
              </Notice>
            ) : seerrServices.data && seerrServices.data.length > 0 ? (
              <>
                {seerrServices.error && <StaleReadNotice what="this portal's services" />}
                {/* Over the grid, for the same reason as the library line above (rule 72): the
                    stale connection names are inside the pickers below it. */}
                {arrInstances.error &&
                  seerrServices.data.some((s) => instanceOptions(s.kind).length > 0) && (
                    <StaleReadNotice what="your Sonarr and Radarr connections" />
                  )}
                <div className="plex-map-grid">
                  {seerrServices.data.map((s) => (
                    <Fragment key={svcKey(s)}>
                      <div className="pl-root">
                        {s.name}
                        {s.is_4k && <span className="pl-tag">4K</span>}
                      </div>
                      <div className="pl-pick">
                        <select
                          className={`pl-select${serviceMap[svcKey(s)] ? "" : " unset"}`}
                          // Same reason as the library picker above. The 4K tag is part of the
                          // visible name here, so it is part of the spoken one too -- a portal
                          // can carry two services that differ only by it. The row's identity
                          // is `svcKey` (kind + id), and the name has to carry every part of it
                          // that can differ: the two lists are numbered and named independently,
                          // so one portal can hold a TV and a Movies service both called "Media"
                          // and the tag alone would leave those two rows sharing a name. Said in
                          // words rather than as the stored kind, per rule 21.
                          aria-label={`Connection for ${s.name}${s.is_4k ? " 4K" : ""}, ${
                            s.kind === "sonarr" ? "TV" : "Movies"
                          }`}
                          value={String(serviceMap[svcKey(s)] ?? "")}
                          onChange={(e) => setServiceInstance(svcKey(s), e.target.value)}
                        >
                          <option value="">Not set</option>
                          {instanceOptions(s.kind).map((i) => (
                            <option key={i.id} value={String(i.id)}>
                              {i.name}
                            </option>
                          ))}
                        </select>
                        {suggestedServices.has(svcKey(s)) && (
                          <span className="pl-suggested">suggested</span>
                        )}
                      </div>
                    </Fragment>
                  ))}
                </div>
                {/* Same trap as the library picker above: a failed fetch leaves every
                    `instanceOptions` empty, and "none yet" would be a claim we never checked --
                    and divided the same way, so a refetch that fails with the connections still
                    held reads as stale rather than as unreadable beside populated pickers. This
                    one stays under the grid: it replaces the help text, it does not describe the
                    pickers. */}
                {arrInstances.error &&
                seerrServices.data.every((s) => instanceOptions(s.kind).length === 0) ? (
                  <Notice tone="warn">
                    Reaper couldn't read your Sonarr and Radarr connections. Try again.
                  </Notice>
                ) : !arrInstances.isPending &&
                  seerrServices.data.every((s) => instanceOptions(s.kind).length === 0) ? (
                  <p className="help">
                    No Sonarr or Radarr connections yet. Add them first, then map each service.
                  </p>
                ) : (
                  <p className="help">
                    Which Sonarr or Radarr connection each of this portal's services adds to. This
                    names the exact copy a person asked for when a title is in more than one
                    library. Matches are suggested from the addresses. Leave a service on "Not set"
                    to name everyone who asked for the title instead.
                  </p>
                )}
              </>
            ) : (
              // The empty-and-then-failed arm, exactly as the folder grid above (rule 72).
              <>
                {seerrServices.error && <StaleReadNotice what="this portal's services" />}
                <p className="help">This portal reports no Sonarr or Radarr services to map.</p>
              </>
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
        {error && <Notice tone="error">{error}</Notice>}
        {test && test.of === testedWith() && (
          <div className="instance-status">
            <TestBadge result={test.result} />
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
          <button type="button" className="ghost" onClick={onClose} disabled={save.isPending}>
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
