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
import { Fragment, useEffect, useRef, useState, type RefObject } from "react";
import { useTranslation } from "react-i18next";
import { announce } from "../announce";
import {
  api,
  type Instance,
  type InstanceKind,
  type InstanceProbe,
  type InstanceTest,
  type RootFolder,
  type SeerrService,
} from "../api";
import { useBackCloseMirror } from "../backnav";
import i18next from "../i18n";
import { usePlexLibraries } from "../usePlexLibraries";
import { composeIn } from "../why";
import { ModalShell } from "./ModalShell";
import { Switch } from "./Switch";
import { Notice } from "./Notice";
import { StaleReadNotice } from "./StaleReadNotice";

export const KINDS: {
  value: InstanceKind;
  label: string;
  hint: string;
  port: string;
  // Only one may be added. Tautulli mirrors a single Plex, and Reaper connects to one Plex,
  // so a second has no working setup. The backend refuses it too; this only hides the add.
  singleton?: boolean;
}[] = [
  {
    value: "radarr",
    label: i18next.t("services.kinds.radarr.label"),
    hint: i18next.t("services.kinds.radarr.hint"),
    port: "7878",
  },
  {
    value: "sonarr",
    label: i18next.t("services.kinds.sonarr.label"),
    hint: i18next.t("services.kinds.sonarr.hint"),
    port: "8989",
  },
  {
    value: "tautulli",
    label: i18next.t("services.kinds.tautulli.label"),
    hint: i18next.t("services.kinds.tautulli.hint"),
    port: "8181",
    singleton: true,
  },
  {
    value: "seerr",
    label: i18next.t("services.kinds.seerr.label"),
    hint: i18next.t("services.kinds.seerr.hint"),
    port: "5055",
  },
];

export function kindLabel(kind: InstanceKind): string {
  return KINDS.find((k) => k.value === kind)?.label ?? kind;
}

/** What media a Seerr service asks for, in a person's words rather than the stored key.
 *
 *  Written once because two surfaces state it: the tag in the row's `.pl-root` cell, and the
 *  spoken name on that row's picker. Both are the operator's only way to tell a portal's TV
 *  service from its Movies one when the two share a name, so a drift between them would leave
 *  one audience right and the other wrong (rule 144, and the same shape as `testSays` below). */
export function serviceKindLabel(kind: SeerrService["kind"]): string {
  return kind === "sonarr"
    ? i18next.t("services.serviceKind.tv")
    : i18next.t("services.serviceKind.movies");
}

/** What a connection test SAYS, written once because two surfaces state it.
 *
 *  `TestBadge` renders it for whoever navigates onto the badge, and every test mutation
 *  announces it for whoever does not (#192). Deriving both from here is what rule 144 asks
 *  for: one fact, and the copy that is spoken cannot drift away from the copy that is read.
 *
 *  `detail` is already a whole sentence from the server ("Connected to Sonarr.", or an explained
 *  failure), so the lead is the only thing added -- for the reason it was added to the badge in
 *  the first place, that "Couldn't reach it" read as a result rather than as a failure.
 *
 *  The one deliberate difference from the badge: the version is spoken as "version 4.0.1" where
 *  the badge shows "(v4.0.1)". A reader voices a bare "v" as a letter. */
export function testSentence(result: InstanceTest): string {
  const version = result.version
    ? i18next.t("services.test.version", { version: result.version })
    : "";
  return i18next.t("services.test.sentence", {
    lead: testLead(result.ok),
    detail: result.detail,
    version,
  });
}

/** The word in front of a test's own detail, in both surfaces' hands. */
function testLead(ok: boolean): string {
  return ok ? i18next.t("services.test.passed") : i18next.t("common.failed");
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
      {result.version && i18next.t("services.test.badgeVersion", { version: result.version })}
    </span>
  );
}

/** The external-URL box's complaint, named once for both ends of the association (rule 67). */
const EXTERNAL_URL_ERROR_ID = "service-external-url-error";

/** Why the submit button is off, one id shared by the three boxes that can be the reason: only
 *  one of them is named at a time, so one region is all there is to point at (#188). */
const BLOCKED_ID = "service-blocked";

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

/** A "pick something for each row" grid, plus which rows still show a value the server guessed
 *  and nobody confirmed.
 *
 *  Two of these are on this form: each root folder to a Plex library, and each Seerr service to
 *  a Reaper connection. They were written out twice, down to the same `exhaustive-deps` disable
 *  for the same reason, and the rules they share are all about what must NOT happen. A stored
 *  pick is never overwritten by a suggestion. A stored pick is never tagged as suggested, so the
 *  tag means "check this" and not "this is set". And picking clears the tag even when the value
 *  did not change, because the operator looking at the row and leaving it is the confirmation.
 *
 *  `rows` is null until a list has actually been read. A read that has not landed must not look
 *  like an instance with nothing to map, which is what the null is for.
 *
 *  `suggestionOf` returns undefined for "no suggestion", so each caller decides what an empty
 *  one means rather than this deciding for both. */
function useSuggestedMap<Row, Value extends string | number>(
  rows: Row[] | null,
  saved: Record<string, Value>,
  keyOf: (row: Row) => string,
  suggestionOf: (row: Row) => Value | undefined,
) {
  const [map, setMap] = useState<Record<string, Value>>(saved);
  const [suggested, setSuggested] = useState<Set<string>>(new Set());

  // Keyed on the row list's identity, so a fresh test landing a new list re-suggests against it.
  useEffect(() => {
    if (!rows) return;
    setMap((prev) => {
      const next = { ...prev };
      for (const row of rows) {
        const key = keyOf(row);
        const suggestion = suggestionOf(row);
        if (!(key in next) && suggestion !== undefined) next[key] = suggestion;
      }
      return next;
    });
    setSuggested((prev) => {
      const next = new Set(prev);
      for (const row of rows) {
        const key = keyOf(row);
        if (!(key in saved) && suggestionOf(row) !== undefined) next.add(key);
      }
      return next;
    });
    // `saved` and both readers derive from the immutable `instance` prop, so the row list is
    // what drives this.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [rows]);

  /** Store a pick, or drop the row's entry with `undefined`. Either way the row is confirmed. */
  const choose = (key: string, value: Value | undefined) => {
    setMap((m) => {
      const next = { ...m };
      if (value === undefined) delete next[key];
      else next[key] = value;
      return next;
    });
    setSuggested((s) => {
      const next = new Set(s);
      next.delete(key);
      return next;
    });
  };

  return { map, suggested, choose };
}

export function ServiceModal({
  kind,
  instance,
  onClose,
  blockCloseRef,
  defaultName,
}: {
  kind: InstanceKind;
  instance: Instance | null;
  onClose: () => void;
  /** Set by ServicesPanel so its Back guard reads the SAME predicate the scrim, Escape and the
   *  ✕ do (rule 80). It mirrors the whole of `canClose`, inverted, not just the save: it began
   *  as a save-pending mirror, and the moment a second reason to stay open arrived -- a folder
   *  map read but never made -- a ref that still meant "saving" would have let browser Back
   *  walk straight through the new guard while every other dismissal honored it. A back-layer
   *  close that bypasses a declared guard is exactly what that rule is about, so the name says
   *  what it holds rather than which of the reasons happened to come first. */
  blockCloseRef?: RefObject<boolean>;
  /** A name to start the box with, rather than only suggest in its placeholder. The setup
   *  wizard passes one because `ready` below requires a non-empty name, so a placeholder alone
   *  leaves a required box empty and the save off on the first screen a new operator meets --
   *  where the name is also the least interesting decision they could be asked to make. */
  defaultName?: string;
}) {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const editing = instance !== null;
  const meta = KINDS.find((k) => k.value === kind);
  const initial = instance ? splitBaseUrl(instance.base_url) : null;

  const [name, setName] = useState(instance?.name ?? defaultName ?? "");
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
  // The result is the union of the two shapes the two test routes answer with. Which one
  // arrived is read off the shape itself below, rather than carried beside it: only the
  // pre-save probe reads the folder and service lists, and an empty list that was never read
  // must not be mistaken for one that was.
  const [test, setTest] = useState<{
    result: InstanceTest | InstanceProbe;
    of: string;
  } | null>(null);
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

  // What the connection was when this modal opened. Compared against `testedWith()` to tell a
  // form that has been pointed somewhere new from one that is merely being renamed, which is
  // what decides whether a fresh test is demanded before Save (see `testRequired` below).
  /** The part of the connection a re-test is demanded for: the address and the key.
   *
   *  Narrower than `testedWith()` on purpose. That is the STALENESS key and rightly includes the
   *  certificate setting, since a pass computed with the check on does not vouch for it off. But
   *  demanding a fresh test for that switch also demanded a KEY -- the edit form's box is blank
   *  by design -- so flipping "Check the server's certificate" told the operator they had
   *  changed the address and blocked Save until they retyped a credential they had already
   *  stored. Turning the check off can only ever make a connection easier to make, so it is
   *  saved on its own terms. */
  const reachedAt = () => [kind, baseUrl(), apiKey].join(" ");
  const openedWith = useRef(reachedAt());
  const connectionEdited = reachedAt() !== openedWith.current;

  // The result currently vouching for what is on screen, or null. Everything downstream keys
  // off this rather than off `test` directly: a held result whose `of` no longer matches is a
  // result for an address that is no longer typed, and must vouch for nothing.
  const passed = test !== null && test.of === testedWith() && test.result.ok ? test : null;
  /** The result of a pre-save PROBE, or null when the test that passed was not one.
   *
   *  Only the probe answers the folder and service lists; a re-test of a saved instance answers
   *  the verdict alone. Reading `root_folders` off that would let an absent pair pose as "this
   *  instance has no folders", take the grid off the screen, and then prune the stored map to
   *  nothing at save. The two shapes are told apart by the field only one of them has, so the
   *  answer comes from the payload rather than from a flag captured beside it. */
  const probeResult = passed !== null && "map_error_reason" in passed.result ? passed.result : null;

  /** The lists that were actually READ, or null. `map_error_reason` is the same hazard from
   *  the other side: the probe ran and failed, so its empty list is a read that did not land,
   *  never an instance with nothing to map. Collapsed here once rather than at each of the
   *  five sites. */
  const probed = probeResult && !probeResult.map_error_reason ? probeResult : null;
  /** The probe's map failure, composed once rather than at each of its four render sites
   *  (docs/history/I18N_PLAN.md §5). Null exactly when `probeResult.map_error_reason` is. */
  const mapErrorText = probeResult?.map_error_reason
    ? composeIn("services.modal", probeResult.map_error_reason)
    : null;

  const libKind = kind === "sonarr" ? "show" : "movie";

  const rootFolders = useQuery({
    queryKey: ["instance-root-folders", instance?.id],
    queryFn: () => api.instanceRootFolders(instance!.id),
    enabled: editing && isArr,
  });
  // Through the shared hook, which syncs a list that has never been synced. Read as a plain
  // query it answered `[]` on a fresh install -- so every picker below offered nothing while
  // the "suggested" tags beside them, which come from a LIVE Plex read on the server, named
  // libraries that plainly existed. The pickers and the suggestions now read one list (#384).
  const { libraries: plexLibraries, sync: syncLibraries } = usePlexLibraries({ enabled: isArr });
  const libOptions = (plexLibraries.data ?? []).filter((l) => l.kind === libKind);

  /** The root folders on screen, or null when none have been read.
   *
   *  Two sources, freshest first: a passing test carries the folders for the address that was
   *  just proved, and a saved instance can be asked by id. The test wins because it describes
   *  the address currently in the boxes, where the by-id read describes the address as STORED
   *  -- point a saved Radarr at a different server and the saved list is about the old one.
   *  Neither is allocated here, so this is a stable identity the prefill effect can depend on
   *  (rule 19). */
  const folders: RootFolder[] | null = probed ? probed.root_folders : (rootFolders.data ?? null);

  // Which Plex library each of this instance's root folders lands in, the HD/4K map.
  const libraries = useSuggestedMap<RootFolder, string>(
    folders,
    instance?.plex_library_map ?? {},
    (f) => f.path,
    // A blank suggestion is no suggestion: it would prefill the picker with "Not set" and then
    // tag the row for the operator to look at, over nothing.
    (f) => f.suggested_library || undefined,
  );
  const libMap = libraries.map;

  const isSeerr = kind === "seerr";

  const seerrServices = useQuery({
    queryKey: ["instance-seerr-services", instance?.id],
    queryFn: () => api.instanceSeerrServices(instance!.id),
    enabled: editing && isSeerr,
  });
  const arrInstances = useQuery({
    queryKey: ["instances"],
    queryFn: api.instances,
    enabled: isSeerr,
  });
  /** The portal's services on screen, from the same two sources as `folders` above and for
   *  the same reason, freshest first (rule 72). */
  const services: SeerrService[] | null = probed
    ? probed.seerr_services
    : (seerrServices.data ?? null);
  const instanceOptions = (svcKind: "sonarr" | "radarr") =>
    (arrInstances.data ?? []).filter((i) => i.kind === svcKind);
  // Seerr numbers Sonarr and Radarr services in SEPARATE lists (both from 0), so the map key
  // must be kind + id, not the id alone -- else a sonarr and a radarr service collide.
  const svcKey = (s: SeerrService) => `${s.kind}:${s.service_id}`;

  // Which Reaper Sonarr/Radarr connection each of this portal's services adds media to. Only
  // for a saved Seerr.
  const requesters = useSuggestedMap<SeerrService, number>(
    services,
    instance?.service_instance_map ?? {},
    svcKey,
    (s) => s.suggested_instance_id ?? undefined,
  );
  const serviceMap = requesters.map;

  // The connection name currently chosen for a service, or undefined when it is on "Not set".
  // The picker stores the instance id, so the name the operator actually read has to be looked
  // back up; the library picker beside it needs no equivalent, because there the option's value
  // IS its title.
  const chosenInstanceName = (s: SeerrService): string | undefined =>
    instanceOptions(s.kind).find((i) => String(i.id) === String(serviceMap[svcKey(s)] ?? ""))?.name;

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

  /** Whether this press tests the STORED instance rather than what is in the boxes.
   *
   *  A saved instance with an untouched address and a blank key ("leave blank to keep the
   *  current key") has nothing the pre-save probe could be sent: it would go out with an empty
   *  key and come back "refused that key". The by-id route tests exactly what is stored, which
   *  is the honest answer to "is this saved service reachable" and needs no key retyped. The
   *  stored key is never sent to an address the browser typed -- that is why this is bounded by
   *  `!connectionEdited` and not merely by the key box being blank. */
  const testsStored = editing && !connectionEdited && apiKey.trim() === "";

  const testConn = useMutation({
    mutationFn: () =>
      testsStored
        ? api.testSavedInstance(instance!.id)
        : api.testInstance({
            kind,
            base_url: baseUrl(),
            api_key: apiKey,
            verify_tls: ssl ? verifyCert : true,
          }),
    // Whether Reaper can reach the Sonarr it deletes THROUGH is worth hearing, which is what
    // makes this more than cosmetic. `instances.py` never raises for a failed test, so an
    // unreachable host arrives as a 200 with `ok=False` and never reaches the shared error
    // notice -- the badge was the only report, and it announced nothing (#192).
    // What this request is ABOUT, captured when it is issued rather than read back at success
    // time: `testedWith()` is derived from boxes the operator can keep typing into while the
    // request is in flight, so reading it later would file the answer against an address it was
    // never asked about.
    //
    // WHICH test answered is no longer carried alongside it. It used to be, as a boolean off
    // `testsStored`, and getting that wrong let a saved-instance verdict's absent lists pose as
    // a folder read. The answer now comes off the payload's own shape
    // (`"map_error_reason" in result`, where `probeResult` is derived), so it cannot be
    // captured wrong.
    onMutate: () => ({ of: testedWith() }),
    onSuccess: (r, _v, issued) => {
      setTest({ result: r, of: issued.of });
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
        if (isArr && folders) {
          const paths =
            rootFolders.isError && !probed ? Object.keys(libMap) : folders.map((f) => f.path);
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
        if (isSeerr && services) {
          const keys =
            seerrServices.isError && !probed
              ? Object.keys(serviceMap)
              : services.map((s) => svcKey(s));
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
        plex_library_map?: Record<string, string>;
        service_instance_map?: Record<string, number>;
      } = { kind, name, base_url: baseUrl(), api_key: apiKey, verify_tls: ssl ? verifyCert : true };
      if (isArr) createBody.add_import_exclusion = addExclusion;
      if (externalUrl.trim()) createBody.external_url = externalUrl.trim();
      // The mapping made on this form rides along with the service it belongs to. Sent only
      // over a list that was actually read -- on the add form that is the passing test's own
      // folders, so there is no stale-list case to guard here the way the update above has to.
      if (isArr && folders) {
        const map: Record<string, string> = {};
        for (const f of folders) {
          const chosen = libMap[f.path];
          if (chosen) map[f.path] = chosen;
        }
        createBody.plex_library_map = map;
      }
      if (isSeerr && services) {
        const map: Record<string, number> = {};
        for (const s of services) {
          const chosen = serviceMap[svcKey(s)];
          if (chosen) map[svcKey(s)] = chosen;
        }
        createBody.service_instance_map = map;
      }
      return api.createInstance(createBody);
    },
    onSuccess: () => {
      // The modal closing was the entire success signal, and it takes the focused button with
      // it. Named for which of the two things just happened, because the operator gets back a
      // list they now have to find the row in.
      announce(
        instance
          ? t("services.modal.savedAnnouncement", { name })
          : t("services.modal.addedAnnouncement", { name }),
      );
      invalidate();
      onClose();
    },
    onError: (e: Error) => setError(e.message),
  });

  // Either there is a whole address and key to send, or the instance is saved and untouched and
  // the by-id route can test what is stored. Without the second arm the button was dead in
  // exactly the states it exists for -- a saved service the operator wants re-tested, where the
  // key box is blank by design -- and re-testing meant retyping a key to prove nothing.
  const canTest =
    !testConn.isPending && ((host.trim() !== "" && apiKey.trim() !== "") || testsStored);

  /** Whether Save waits on a passing connection test.
   *
   *  Always on the add form: a service saved at an address Reaper never reached is a scan that
   *  fails later, on a screen that never mentioned it. On the EDIT form only once something the
   *  connection depends on has been changed -- a stored instance was reachable when it was
   *  saved, and demanding a live test to fix a typo in its NAME would refuse the edit whenever
   *  the service happened to be down, which is the one moment an operator most wants to change
   *  something. Renaming stays free; re-pointing does not. */
  const testRequired = !editing || connectionEdited;
  /** The key box is blank on the edit form and means "keep the stored key" -- which cannot be
   *  tested against a DIFFERENT address, because sending a stored secret to a host the browser
   *  just typed is exactly the shape an exfiltration bug takes. So re-pointing a saved instance
   *  asks for the key again, and the sentence below says so rather than letting the test fail
   *  as "refused that key". */
  const needsKeyToTest = testRequired && apiKey.trim() === "";

  /** How many of the folders on screen have a library, and whether at least one is required.
   *
   *  The floor lifts when there is nothing to satisfy it WITH: no folders read, or no libraries
   *  to pick from (a Plex that is down, or one that has never been synced). A requirement the
   *  operator has no way to meet is not a safeguard, it is a locked door -- and the map is
   *  there to tell an HD copy from a 4K one, which needs a library list to be possible at
   *  all. Seerr takes the prefill but no floor: its own help text says leaving a service
   *  unset is a real choice, meaning "credit everyone who asked". */
  const mappedFolders = folders ? folders.filter((f) => libMap[f.path]).length : 0;
  const mapFloorApplies = isArr && (folders?.length ?? 0) > 0 && libOptions.length > 0;
  const mapSatisfied = !mapFloorApplies || mappedFolders > 0;

  /** Whether a dismissal is allowed, computed ONCE and handed to every path that can dismiss.
   *
   *  Two reasons to stay open. A close mid-save unmounts the only place the failure is ever
   *  shown: the scrim swallows a 409 "a service with that name already exists", `invalidate()`
   *  never runs, and the operator walks away believing the change saved (B-19). And a close over
   *  folders that were read but never mapped drops the one screen that can tell this instance's
   *  HD copy from its 4K one, silently.
   *
   *  Cancel is deliberately NOT gated on this: it is the deliberate way out, it saves nothing,
   *  and a guard whose only exit is the destructive button is a trap rather than a guard
   *  (rule 146). What this refuses is the ACCIDENTAL dismissals -- scrim, Escape, ✕, Back.
   *
   *  It is a mute gate, so the notice at the top of the form is what states the reason. */
  const canClose = !save.isPending && mapSatisfied;

  // Up to ServicesPanel's Back guard, whole rather than by term (rule 80, B-11, B-19).
  useBackCloseMirror(blockCloseRef, canClose);

  const ready =
    name.trim() !== "" &&
    host.trim() !== "" &&
    (editing || apiKey.trim() !== "") &&
    (!testRequired || passed !== null) &&
    mapSatisfied;

  /** Fire the test for a combination that has not been tried yet.
   *
   *  Called from the blur of every box the connection depends on, so "enter the key and Reaper
   *  checks it" needs no button press -- and from blur rather than from each keystroke, so
   *  typing a hostname does not send one request per character to a server that is about to be
   *  told a different one. A combination that already has a result is not re-sent. */
  const testIfUntried = () => {
    if (!canTest || (test && test.of === testedWith())) return;
    setError(null);
    testConn.mutate();
  };

  // Which box the submit button is waiting on, and the one sentence saying so. Three fields can
  // be empty at once but only the FIRST is named: the operator fills it, the next one appears,
  // and each sentence names a single box rather than reciting a form (#188). Ordered as the boxes
  // are on screen, so the complaint always points DOWN the form, never back up it.
  //
  // The sentence binds to that box, not to the button. A `disabled` button is out of the Tab
  // order, so a description hung on it is unreachable by the operator it is for -- the same
  // reasoning as the password row's `errorOwner`, which this mirrors.
  // The tail names what the button will do, read off the same `editing` the button's own label
  // is, so the two cannot come to disagree about it (rule 144).
  //
  // The three boxes are still first and still named one at a time. What follows them is the
  // rest of the road to Save, in the order it is walked: the connection has to be proved, and
  // then the folders it handed back have to be mapped. Each reason names the control that
  // clears it, and `owner` is what binds the sentence to that control -- including the two new
  // owners, the Test button and the first unmapped picker, neither of which is a text box.
  const willDo = editing ? t("services.modal.willDo.save") : t("services.modal.willDo.add");
  const missing: { owner: "name" | "host" | "key" | "test" | "map"; says: string } | null =
    name.trim() === ""
      ? { owner: "name", says: t("services.modal.missing.name", { willDo }) }
      : host.trim() === ""
        ? { owner: "host", says: t("services.modal.missing.host", { willDo }) }
        : !editing && apiKey.trim() === ""
          ? { owner: "key", says: t("services.modal.missing.key", { willDo }) }
          : needsKeyToTest
            ? { owner: "key", says: t("services.modal.missing.keyForTest") }
            : testRequired && passed === null
              ? {
                  // No arm for "checking…": the button beside this says "Testing…" itself, and
                  // it is DISABLED while it does, so a description hung on it is unreachable by
                  // the operator it is for -- the very trap the note above forbids.
                  owner: "test",
                  says: t("services.modal.missing.test"),
                }
              : !mapSatisfied
                ? { owner: "map", says: t("services.modal.missing.map") }
                : null;
  /** The first folder with no library, so the sentence above binds to the control that clears
   *  it rather than to a disabled button nothing can Tab to. */
  const blockedFolder =
    missing?.owner === "map" ? (folders?.find((f) => !libMap[f.path])?.path ?? null) : null;

  return (
    <ModalShell
      title={
        <>
          <span className={`kind-badge kind-${kind}`}>{kindLabel(kind)}</span>{" "}
          {editing
            ? t("common.editNamed", { name: instance.name })
            : t("services.modal.titleAdd", { kind: kindLabel(kind) })}
        </>
      }
      onClose={onClose}
      // The two reasons a dismissal is refused, and why Cancel is not one of them, are at
      // `canClose`'s declaration above.
      canClose={canClose}
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
          <span className="field-label">{t("services.modal.field.name")}</span>
          <input
            value={name}
            onChange={(e) => setName(e.target.value)}
            placeholder={
              kind === "tautulli" || kind === "seerr"
                ? t("services.modal.field.namePlaceholderMain")
                : t("services.modal.field.namePlaceholderHd")
            }
            aria-describedby={missing?.owner === "name" ? BLOCKED_ID : undefined}
          />
        </label>
        <div className="host-row">
          <label className="field-sm">
            <span className="field-label">{t("services.modal.field.host")}</span>
            <span className="url-join">
              {/* The scheme is a fused prefix, not part of the box's name: the label wraps it, so
                  a reader announced this field as "Hostname or IP http colon slash slash" and the
                  operator heard punctuation where the box's job should have been (#214). Hidden
                  from the name only -- it is drawn as before, and the "Use SSL" switch below is
                  the control that states and changes it, in words. */}
              <span className="url-scheme" aria-hidden="true">
                {ssl ? t("services.modal.field.schemeHttps") : t("services.modal.field.schemeHttp")}
              </span>
              <input
                value={host}
                onChange={(e) => onHostChange(e.target.value)}
                onBlur={testIfUntried}
                placeholder="192.168.1.10"
                aria-describedby={missing?.owner === "host" ? BLOCKED_ID : undefined}
              />
            </span>
          </label>
          <label className="field-sm">
            <span className="field-label">{t("services.modal.field.port")}</span>
            <input
              value={port}
              inputMode="numeric"
              onChange={(e) => setPort(e.target.value.replace(/[^0-9]/g, "").slice(0, 5))}
              onBlur={testIfUntried}
              placeholder={ssl ? "443" : "80"}
            />
          </label>
        </div>
        <label className="field-sm">
          <span className="field-label">{t("services.modal.field.urlBase")}</span>
          <input
            value={urlBase}
            onChange={(e) => setUrlBase(e.target.value)}
            onBlur={testIfUntried}
            placeholder={t("services.modal.field.urlBasePlaceholder")}
          />
        </label>
        <label className="toggle">
          <Switch checked={ssl} onChange={setSsl} />
          <span>{t("services.modal.field.useSsl")}</span>
        </label>
        {ssl && (
          <>
            <label className="toggle">
              <Switch checked={verifyCert} onChange={setVerifyCert} />
              <span>{t("services.modal.field.verifyCert")}</span>
            </label>
            {!verifyCert && <Notice tone="warn">{t("services.modal.certificateWarning")}</Notice>}
          </>
        )}
        {isArr && (
          <>
            <label className="toggle">
              <Switch checked={addExclusion} onChange={setAddExclusion} />
              <span>{t("services.modal.field.addExclusion")}</span>
            </label>
            <p className="help">
              {kind === "sonarr"
                ? t("services.modal.exclusion.sonarrHelp")
                : addExclusion
                  ? t("services.modal.exclusion.onHelp")
                  : t("services.modal.exclusion.offHelp")}
            </p>
          </>
        )}
        <label className="field-sm">
          <span className="field-label">
            {editing ? t("services.modal.field.apiKeyEdit") : t("services.modal.field.apiKeyAdd")}
          </span>
          <input
            type="password"
            value={apiKey}
            onChange={(e) => setApiKey(e.target.value)}
            // "Once the key is entered, try to connect" -- on leaving the box, so the request
            // goes once against a whole key rather than once per character of one.
            onBlur={testIfUntried}
            placeholder={
              editing
                ? t("services.modal.field.apiKeyPlaceholderEdit")
                : t("services.modal.field.apiKeyPlaceholderAdd")
            }
            autoComplete="off"
            aria-describedby={missing?.owner === "key" ? BLOCKED_ID : undefined}
          />
        </label>
        <label className="field-sm">
          <span className="field-label">{t("services.modal.field.externalUrl")}</span>
          <input
            type="url"
            value={externalUrl}
            // Cleared as soon as it is being fixed, so the box never claims a value the
            // operator has already retyped is still the one that was refused.
            onChange={(e) => {
              setExternalUrl(e.target.value);
              setExtUrlBad(false);
            }}
            placeholder={t("services.modal.field.externalUrlPlaceholder", { kind })}
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
            {t("services.modal.externalUrlError")}
          </Notice>
        )}
        <p className="help">{t("services.modal.externalUrlHelp", { kind: kindLabel(kind) })}</p>
        {isArr && (
          <div className="field-sm plex-map">
            <span className="field-label">{t("services.modal.field.plexLibraries")}</span>
            {/* Divided on whether the folder list ever landed: any refetch while the modal is
                open reaches this, and an undivided error traded the whole mapping grid for one
                warning while React Query still held the folders (#190).
                `mapErrorText` is the same division for the add form's source: the test
                reached the service but could not read its folders, which is not the same as a
                service that has none. */}
            {folders === null && (testConn.isPending || (editing && rootFolders.isPending)) ? (
              /* `editing &&` is load-bearing: this query is `enabled: editing && isArr`, and a
                 DISABLED query reports `status: "pending"` forever, so the add form sat under
                 "Reading this instance's folders…" from the moment it opened -- describing a
                 read that was never started, and taking the sentence written for that exact
                 moment (below) off the screen entirely. */
              <p className="help">{t("services.modal.folders.reading")}</p>
            ) : folders === null && mapErrorText ? (
              /* Only when there is nothing to show. A probe that failed while the by-id read
                 still holds folders leaves the grid up and says this over it instead, because
                 the grid is the surface the operator needs and the failure is about a refresh
                 of it. */
              <Notice tone="warn">{mapErrorText}</Notice>
            ) : folders === null && !editing ? (
              /* The add form before a test. Says where the list comes from rather than showing
                 an empty grid, because on this form the folders are a RESULT of the connection
                 test and not something that could have been read yet. */
              <p className="help">{t("services.modal.folders.beforeTest")}</p>
            ) : rootFolders.error && !rootFolders.data && !probed ? (
              /* `!probed` or this outranks a live probe that just succeeded. Re-pointing a saved
                 *arr whose STORED address is dead is the flow this whole feature exists for: the
                 by-id read fails against the old address, the operator types the new one, the
                 test passes and hands back folders -- and this arm still won, so the grid never
                 rendered, Save stayed off for a mapping with no picker to make it in, and the
                 modal refused to close. A guard whose signal outlives the surface that satisfies
                 it is a trap (rule 146), and the sentence was false besides: the folders had
                 just been read. */
              <Notice tone="warn">{t("services.modal.folders.readFailed")}</Notice>
            ) : folders && folders.length > 0 ? (
              <>
                {rootFolders.error && !probed && (
                  <StaleReadNotice what={t("services.modal.folders.staleWhat")} />
                )}
                {/* The probe failed while the by-id read still holds the folders. The grid stays
                    up, because it is the surface the operator needs and this is a failure to
                    REFRESH it, and the sentence sits over it like every other line about
                    something below. Rendered here as well as in the no-folders arm above, which
                    is the same fact in the two states it can be in. */}
                {mapErrorText && <Notice tone="warn">{mapErrorText}</Notice>}
                {/* Over the grid, beside the folder line, because the shared sentence says what
                    is BELOW may be out of date and the stale library names are inside the pickers
                    under it. Emitted after the grid it pointed at the help paragraph instead,
                    which is the placement `JobsPanel.tsx` fixed for the jobs rows and says every
                    other call site already keeps.

                    `libOptions.length === 0` is what tells the two failures apart: a refetch that
                    fails with the libraries still held leaves every picker below populated, and
                    "couldn't read your Plex libraries" printed there sat beside a working list
                    (#190). Said as staleness instead, which is what it is. */}
                {plexLibraries.error && libOptions.length > 0 && (
                  <StaleReadNotice what={t("services.modal.folders.librariesStaleWhat")} />
                )}
                <div className="plex-map-grid">
                  {folders.map((f) => (
                    <Fragment key={f.path}>
                      <div className="pl-root">{f.path}</div>
                      <div className="pl-pick">
                        <select
                          className={`pl-select${libMap[f.path] ? "" : " unset"}`}
                          // The folder name is the only thing telling these rows apart, and it
                          // lives in a sibling cell the select is not labeled by. Without this
                          // a screen reader announces every row as "combobox, Not set".
                          aria-label={t("services.modal.folders.pickerAriaLabel", { path: f.path })}
                          // The "map at least one" sentence binds to the FIRST unmapped picker,
                          // which is a control that can actually be reached and used, where the
                          // disabled Save button it describes is out of the Tab order entirely.
                          aria-describedby={blockedFolder === f.path ? BLOCKED_ID : undefined}
                          value={libMap[f.path] ?? ""}
                          onChange={(e) => libraries.choose(f.path, e.target.value)}
                        >
                          <option value="">{t("services.modal.notSetOption")}</option>
                          {libOptions.map((l) => (
                            <option key={l.key} value={l.title}>
                              {l.title}
                            </option>
                          ))}
                        </select>
                        {libraries.suggested.has(f.path) && (
                          <span className="pl-suggested">{t("services.modal.suggestedTag")}</span>
                        )}
                        {/* The chosen library, said again as ordinary wrapping text. A native
                            <select> clips its selected option to its own width and nothing
                            reaches inside it, so two libraries sharing a long prefix render as
                            one string in the control -- on the screen that decides which folder
                            Reaper matches to which library (#306). This is the one shape rule
                            139's remedy does reach. `aria-hidden` because the select already
                            announces its full selected option: the clipping is visual, so the
                            repair is too, and saying it twice would be the only thing a screen
                            reader noticed. Rendered only when set, since "Not set" is legible
                            at any width. */}
                        {libMap[f.path] && (
                          <span className="pl-echo" aria-hidden="true">
                            {libMap[f.path]}
                          </span>
                        )}
                      </div>
                    </Fragment>
                  ))}
                </div>
                {/* A failed fetch also empties `libOptions`, and "no libraries yet" would then
                    state as fact something we never learned -- and send the operator off to
                    re-sync a list that is already there. The empty sentence is only for a list
                    we genuinely read and found empty. This one stays under the grid: it replaces
                    the help text rather than describing the pickers.

                    It no longer sends anyone to Plex settings to press Sync. That instruction was
                    the visible half of #384: the list this reads is filled by a sync, nothing in
                    the wizard ever ran one, and so the copy asked a first-run operator to go
                    somewhere else and do the app's own job. `usePlexLibraries` runs it here. */}
                {(plexLibraries.error || syncLibraries.error) && libOptions.length === 0 ? (
                  /* `syncLibraries.error` belongs here, not only the query's. The read can
                     succeed with `[]` while the SYNC that would fill it fails -- Plex not linked
                     at all answers 400, Plex down answers 502 -- and with only the query
                     consulted the arm below then stated as fact that the server has no libraries
                     of this kind, about a server nobody reached (rule 93). */
                  <Notice tone="warn">{t("services.modal.folders.librariesReadFailed")}</Notice>
                ) : plexLibraries.isPending || syncLibraries.isPending ? (
                  <p className="help">{t("services.modal.folders.librariesLoading")}</p>
                ) : libOptions.length === 0 ? (
                  <p className="help">{t("services.modal.folders.noneToMap", { libKind })}</p>
                ) : (
                  <p className="help">{t("services.modal.folders.help")}</p>
                )}
              </>
            ) : (
              // A list that landed EMPTY and then failed to refetch reaches this arm, not the
              // never-landed one above: `[]` is truthy, so `!rootFolders.data` is false. The
              // sentence below is a positive claim about the instance, so it takes the stale
              // line like every other held value -- without it, the one state where the read
              // failed AND the app has something to say rendered no warning at all.
              <>
                {rootFolders.error && !probed && (
                  <StaleReadNotice what={t("services.modal.folders.staleWhat")} />
                )}
                <p className="help">{t("services.modal.folders.none")}</p>
              </>
            )}
          </div>
        )}
        {isSeerr && (
          <div className="field-sm plex-map">
            <span className="field-label">{t("services.modal.field.requesterInstances")}</span>
            {/* Divided exactly as the folder grid above (rule 72), including the add form's
                before-a-test arm and `mapErrorText` for a portal that answered but would not
                hand over its settings. */}
            {services === null && (testConn.isPending || (editing && seerrServices.isPending)) ? (
              /* `editing &&` for the same reason as the folder slot above (rule 72). */
              <p className="help">{t("services.modal.requesters.reading")}</p>
            ) : services === null && mapErrorText ? (
              <Notice tone="warn">{mapErrorText}</Notice>
            ) : services === null && !editing ? (
              <p className="help">{t("services.modal.requesters.beforeTest")}</p>
            ) : seerrServices.error && !seerrServices.data && !probed ? (
              /* `!probed` for the same reason as the folder arm above (rule 72). */
              <Notice tone="warn">{t("services.modal.requesters.readFailed")}</Notice>
            ) : services && services.length > 0 ? (
              <>
                {seerrServices.error && !probed && (
                  <StaleReadNotice what={t("services.modal.requesters.staleWhat")} />
                )}
                {/* Same as the folder grid above (rule 72). */}
                {mapErrorText && <Notice tone="warn">{mapErrorText}</Notice>}
                {/* Over the grid, for the same reason as the library line above (rule 72): the
                    stale connection names are inside the pickers below it. */}
                {arrInstances.error && services.some((s) => instanceOptions(s.kind).length > 0) && (
                  <StaleReadNotice what={t("services.modal.requesters.instancesStaleWhat")} />
                )}
                <div className="plex-map-grid">
                  {services.map((s) => (
                    <Fragment key={svcKey(s)}>
                      {/* Every part of `svcKey` (kind + id) that a person can see has to be on
                          this cell, because the name alone does not identify the row: a portal
                          numbers and names its Sonarr and Radarr lists independently, so one can
                          hold a TV and a Movies service both called "Media". The kind rides
                          beside the name the way the 4K marker already does, and in that order --
                          what the service is, then which copy of it (#165). */}
                      <div className="pl-root">
                        {s.name}
                        <span className="pl-tag">{serviceKindLabel(s.kind)}</span>
                        {s.is_4k && (
                          <span className="pl-tag">{t("services.modal.requesters.tag4k")}</span>
                        )}
                      </div>
                      <div className="pl-pick">
                        <select
                          className={`pl-select${serviceMap[svcKey(s)] ? "" : " unset"}`}
                          // Same reason as the library picker above, and the spoken name carries
                          // exactly what the cell beside it shows: both tags distinguish rows a
                          // shared name would otherwise merge, so both belong here too. Said in
                          // words rather than as the stored kind, per rule 21, and taken from
                          // `serviceKindLabel` so the two spellings cannot drift (rule 144).
                          aria-label={t("services.modal.requesters.pickerAriaLabel", {
                            name: s.name,
                            is4k: s.is_4k ? "yes" : "no",
                            serviceKind: serviceKindLabel(s.kind),
                          })}
                          value={String(serviceMap[svcKey(s)] ?? "")}
                          onChange={(e) =>
                            requesters.choose(
                              svcKey(s),
                              e.target.value ? Number(e.target.value) : undefined,
                            )
                          }
                        >
                          <option value="">{t("services.modal.notSetOption")}</option>
                          {instanceOptions(s.kind).map((i) => (
                            <option key={i.id} value={String(i.id)}>
                              {i.name}
                            </option>
                          ))}
                        </select>
                        {requesters.suggested.has(svcKey(s)) && (
                          <span className="pl-suggested">{t("services.modal.suggestedTag")}</span>
                        )}
                        {/* The chosen connection, on exactly the terms as the library picker
                            above (rule 72). */}
                        {chosenInstanceName(s) && (
                          <span className="pl-echo" aria-hidden="true">
                            {chosenInstanceName(s)}
                          </span>
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
                services.every((s) => instanceOptions(s.kind).length === 0) ? (
                  <Notice tone="warn">{t("services.modal.requesters.instancesReadFailed")}</Notice>
                ) : !arrInstances.isPending &&
                  services.every((s) => instanceOptions(s.kind).length === 0) ? (
                  <p className="help">{t("services.modal.requesters.noInstances")}</p>
                ) : (
                  <p className="help">{t("services.modal.requesters.help")}</p>
                )}
              </>
            ) : (
              // The empty-and-then-failed arm, exactly as the folder grid above (rule 72).
              <>
                {seerrServices.error && !probed && (
                  <StaleReadNotice what={t("services.modal.requesters.staleWhat")} />
                )}
                <p className="help">{t("services.modal.requesters.none")}</p>
              </>
            )}
          </div>
        )}
        {editing && (
          <label className="toggle">
            <Switch checked={enabled} onChange={setEnabled} />
            <span>{t("services.modal.field.enabled")}</span>
          </label>
        )}
        {meta && <p className="help">{meta.hint}</p>}
        {error && <Notice tone="error">{error}</Notice>}
        {test && test.of === testedWith() && (
          <div className="instance-status">
            <TestBadge result={test.result} />
          </div>
        )}
        {/* Why the ✕ and Escape are doing nothing, beside Cancel, which is the way out it names
            (rule 42). It sat at the top of the form, eight controls above that button and above
            the pickers that satisfy it, and it said in its own words what the blocked-reason
            sentence below already says -- one requirement, written twice, in two places, neither
            of them where it is acted on. Only ever on screen while `canClose` is false, so the
            notice and the guard cannot disagree about whether the form is holding something. */}
        {!mapSatisfied && <Notice tone="warn">{t("services.modal.mapWarning")}</Notice>}
        <div className="add-actions">
          {/* On both forms now, not just the add one. The test fires on its own when a box is
              left, so this is the way back from the states no blur will reach: a switch that was
              flipped, a service that was down a moment ago, a result the operator wants
              re-taken. Hiding it while editing also left two notices above pointing at a button
              that was nowhere on screen (rule 25) -- they no longer have to apologize for it. */}
          <button
            type="button"
            className="ghost"
            disabled={!canTest}
            aria-describedby={missing?.owner === "test" ? BLOCKED_ID : undefined}
            onClick={() => {
              setError(null);
              testConn.mutate();
            }}
          >
            {testConn.isPending
              ? t("services.common.testing")
              : passed
                ? t("services.modal.testButton.again")
                : t("services.modal.testButton.initial")}
          </button>
          <span className="flex-spacer" />
          <button type="button" className="ghost" onClick={onClose} disabled={save.isPending}>
            {t("common.cancel")}
          </button>
          <button type="submit" className="primary" disabled={!ready || save.isPending}>
            {save.isPending
              ? editing
                ? t("common.saving")
                : t("services.modal.adding")
              : editing
                ? t("common.save")
                : t("services.modal.addService")}
          </button>
          {missing && (
            <span className="help help-warn" id={BLOCKED_ID}>
              {missing.says}
            </span>
          )}
        </div>
      </form>
    </ModalShell>
  );
}
