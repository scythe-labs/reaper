// SPDX-License-Identifier: AGPL-3.0-or-later
//
// Add or edit one service, in a modal.
//
// The address is edited as parts: hostname, port, SSL, and an optional URL base. It is
// stored as the single base_url the backend has always kept, composed and re-split here.
// The API key stays write-only end to end: the edit form shows a blank field, and a blank
// field means "keep the stored key". "Check the server's certificate" is on by default.
// Turning it off is a deliberate per-service choice for a self-signed server the operator
// runs themselves, and it only appears once SSL is on, since plain http has no certificate.
//
// Sonarr and Radarr also carry "Block re-download after delete" (off by default): whether a
// delete asks the *arr to add an import exclusion so a list can't re-add the title. It is
// wired for Radarr movie deletes. On Sonarr it is stored but inert, because Reaper prunes
// seasons, not whole series, and the help text says so rather than pretending otherwise.

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Fragment, useEffect, useRef, useState, type RefObject } from "react";
import { useTranslation } from "react-i18next";
import { announce } from "../announce";
import {
  api,
  type DiscordTest,
  type Instance,
  type InstanceKind,
  type InstanceProbe,
  type InstanceTest,
  type RootFolder,
  type SeerrService,
} from "../api";
import { useBackCloseMirror } from "../backnav";
import { describeError } from "../errors";
import i18next from "../i18n";
import { usePlexLibraries } from "../usePlexLibraries";
import { composeError, composeIn } from "../why";
import { ModalShell } from "./ModalShell";
import { Switch } from "./Switch";
import { Notice } from "./Notice";
import { StaleReadNotice } from "./StaleReadNotice";

/** The service kinds Reaper can be pointed at, in the order the setup step lists them.
 *
 *  A function, not a constant: a string resolved in a module body keeps whatever language was
 *  serving when the module first loaded (`i18n-module-scope.test.ts`). */
export const kinds = (): {
  value: InstanceKind;
  label: string;
  hint: string;
  port: string;
  // Only one may be added. Tautulli mirrors a single Plex, and Reaper connects to one Plex,
  // so a second has no working setup. The backend refuses it too. This only hides the add.
  singleton?: boolean;
}[] => [
  {
    value: "radarr",
    label: i18next.t("common.brand.radarr"),
    hint: i18next.t("services.kinds.radarr.hint"),
    port: "7878",
  },
  {
    value: "sonarr",
    label: i18next.t("common.brand.sonarr"),
    hint: i18next.t("services.kinds.sonarr.hint"),
    port: "8989",
  },
  {
    value: "tautulli",
    label: i18next.t("common.brand.tautulli"),
    hint: i18next.t("services.kinds.tautulli.hint"),
    port: "8181",
    singleton: true,
  },
  {
    value: "seerr",
    label: i18next.t("common.brand.seerr"),
    hint: i18next.t("services.kinds.seerr.hint"),
    port: "5055",
  },
];

export function kindLabel(kind: InstanceKind): string {
  return kinds().find((k) => k.value === kind)?.label ?? kind;
}

/** What media a Seerr service asks for, in a person's words rather than the stored key.
 *
 *  Written once because two surfaces state it: the tag in the row's `.pl-root` cell, and the
 *  spoken name on that row's picker. Both are the operator's only way to tell a portal's TV
 *  service from its Movies one when the two share a name, so a drift between them would
 *  leave one audience right and the other wrong, the same shape as `testSentence` below. */
export function serviceKindLabel(kind: SeerrService["kind"]): string {
  return kind === "sonarr"
    ? i18next.t("services.serviceKind.tv")
    : i18next.t("services.serviceKind.movies");
}

/** A test's own detail, composed from its typed reason. A failure's id is a full `error.*`
 *  catalog code (`explain_failure`'s own, e.g. `error.instance.auth_refused`) and composes
 *  through the error namespace. A pass's id is bare (`services.test.connected` and kin,
 *  `ServiceModal.tsx`'s own namespace) and composes under `services.test`. `ServicesPanel.tsx`
 *  synthesizes a `{k: "legacy", p: {text}}` reason for the persisted card states (the stored
 *  `last_error` string, the fixed "Reached" detail), which `composeIn`/`composeError` both
 *  already render verbatim, the same shape a pre-conversion stored row carries. */
export function testDetailText(reason: InstanceTest["detail_reason"]): string {
  return reason.k.startsWith("error.") ? composeError(reason) : composeIn("services.test", reason);
}

/** A Discord webhook test's typed reason, composed into the same `InstanceTest` shape
 *  `TestBadge` and `testSentence` already render for a connection test. `DiscordModal.tsx`
 *  and `NotificationsPanel.tsx` both call this rather than each composing
 *  `services.discord.testResult` into a `legacy`-wrapped reason on its own. */
export function composeDiscordTestResult(r: DiscordTest): InstanceTest {
  return {
    ok: r.ok,
    detail_reason: { k: "legacy", p: { text: composeIn("services.discord.testResult", r.reason) } },
    version: r.version,
  };
}

/** What a connection test says, written once because two surfaces state it.
 *
 *  `TestBadge` renders it for whoever navigates onto the badge, and every test mutation
 *  announces it for whoever does not. Deriving both from here means one fact, and the copy
 *  that is spoken cannot drift away from the copy that is read.
 *
 *  `detail` is already a whole sentence ("Connected to Sonarr.", or an explained failure), so
 *  the lead is the only thing added: the badge needed it because a bare "Couldn't reach it"
 *  read as a result rather than as a failure.
 *
 *  The one deliberate difference from the badge: the version is spoken as "version 4.0.1"
 *  where the badge shows "(v4.0.1)". A reader voices a bare "v" as a letter. */
export function testSentence(result: InstanceTest): string {
  const version = result.version
    ? i18next.t("services.test.version", { version: result.version })
    : "";
  return i18next.t("services.test.sentence", {
    lead: testLead(result.ok),
    detail: testDetailText(result.detail_reason),
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
      {/* The glyph and color alone are not enough: "✓" and "✗" are read the same way by a
          reader at its default symbol level, so the detail line that follows ("Reached", or
          the error) would be the only difference without the word carrying it too.

          The badge sits in no live region, and it does not need one: the three test
          mutations announce `testSentence` at the moment the result settles. Announcing
          from here instead would fire on every re-render of a result the operator has
          already heard. */}
      <span className="sr-only">{testLead(result.ok)}: </span>
      <span aria-hidden="true">{result.ok ? "✓ " : "✗ "}</span>
      {testDetailText(result.detail_reason)}
      {result.version && i18next.t("services.test.badgeVersion", { version: result.version })}
    </span>
  );
}

/** Client-side twin of the server's webhook validation (reaper/api/settings.py). The token
 *  lives in the URL path, so a typo'd host would leak it to a stranger. This only accepts an
 *  https URL whose host is Discord's webhook endpoint (subdomains like ptb./canary. count)
 *  and whose path is a real /api/webhooks/ path. The server checks the same thing. This just
 *  spares a round-trip and gives an instant hint.
 *
 *  Lives here rather than in `DiscordModal` or `NotificationsPanel` so both webhook editors
 *  and `useWebhookTest` below share the one rule without importing each other. A second copy
 *  of these host and path rules would be one validator written twice, and the copy that
 *  drifts is the one that starts accepting a URL the backend then refuses. */
const DISCORD_WEBHOOK_HOSTS = ["discord.com", "discordapp.com"];

export function isDiscordWebhook(raw: string): boolean {
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

/** The shared half of testing a Discord webhook: whether the typed box is a valid new
 *  webhook, whether Test may fire, and the test mutation itself. `DiscordModal` and
 *  `NotificationsPanel` each point their own box and save/remove pair at this, so the
 *  validity rule and the test request can't drift into two answers about the same webhook.
 *  Each caller keeps its own pending-button label and error-message wording.
 *
 *  `onTestError` gets the raw thrown value, so the message stays the caller's to compose. */
export function useWebhookTest(url: string, connected: boolean, onTestError: (e: unknown) => void) {
  // The result and the URL it was computed for. Without the pairing, a passed test would
  // outlive a pasted-over box and show "Passed" for a webhook nobody has sent to.
  const [test, setTest] = useState<{ result: InstanceTest; of: string } | null>(null);

  /** What a test would be sent. A blank box tests the webhook already stored, so blank is a
   *  real value here rather than an absence. */
  const testedWith = () => url.trim();
  const typed = url.trim().length > 0;
  const validNew = typed && isDiscordWebhook(url);
  const badFormat = typed && !validNew;

  const sendTest = useMutation({
    // Test what is typed if anything is. Otherwise test what is stored, so a saved channel
    // can be verified without going back to Discord for the URL again.
    mutationFn: () => api.testWebhook(url.trim() ? url.trim() : null),
    // What this request is about, captured when it is issued rather than computed at
    // success time: the box stays live while the request is out, and `testedWith()` called
    // at success would fingerprint whatever is typed by then.
    onMutate: () => ({ of: testedWith() }),
    onSuccess: (r, _v, issued) => {
      const result = composeDiscordTestResult(r);
      setTest({ result, of: issued.of });
      announce(testSentence(result));
    },
    onError: onTestError,
  });

  const canTest = (validNew || (!typed && connected)) && !sendTest.isPending;

  return {
    typed,
    validNew,
    badFormat,
    canTest,
    test,
    testedWith,
    sendTest,
    clearTest: () => setTest(null),
  };
}

/** The external-URL box's complaint, named once for both ends of the association. */
const EXTERNAL_URL_ERROR_ID = "service-external-url-error";

/** Why the submit button is off, one id shared by the three boxes that can be the reason.
 *  Only one of them is named at a time, so one region is all there is to point at. */
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
 * by the 422. A `type="url"` input accepts any scheme with a colon, so it is not this check. */
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
 *  a Reaper connection. They are written out twice, down to the same `exhaustive-deps` disable
 *  for the same reason, and they share the same rules for what must never happen. A stored
 *  pick is never overwritten by a suggestion. A stored pick is never tagged as suggested, so
 *  the tag means "check this", not "this is set". Picking clears the tag even when the value
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
  /** Set by ServicesPanel so its Back guard reads the same predicate the scrim, Escape and the
   *  ✕ do. It mirrors the whole of `canClose`, inverted, not just the save. It began as a
   *  save-pending mirror, and the moment a second reason to stay open arrived (a folder map
   *  read but never made), a ref that still meant "saving" would have let browser Back walk
   *  straight through the new guard while every other dismissal honored it. A back-layer
   *  close that bypasses a declared guard is a real risk, so the name says what it holds
   *  rather than which of the reasons happened to come first. */
  blockCloseRef?: RefObject<boolean>;
  /** A name to start the box with, rather than only suggest in its placeholder. The setup
   *  wizard passes one because `ready` below requires a non-empty name, so a placeholder alone
   *  leaves a required box empty and the save off on the first screen a new operator meets,
   *  where the name is also the least interesting decision they could be asked to make. */
  defaultName?: string;
}) {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const editing = instance !== null;
  const meta = kinds().find((k) => k.value === kind);
  const initial = instance ? splitBaseUrl(instance.base_url) : null;

  const [name, setName] = useState(instance?.name ?? defaultName ?? "");
  const [host, setHost] = useState(initial?.host ?? "");
  const [port, setPort] = useState(initial ? initial.port : (meta?.port ?? ""));
  const [urlBase, setUrlBase] = useState(initial?.urlBase ?? "");
  const [ssl, setSsl] = useState(initial?.ssl ?? false);
  const [verifyCert, setVerifyCert] = useState(instance?.verify_tls ?? true);
  // Whether a delete through this instance adds an import exclusion. Off by default, and
  // only shown for the *arr (movies/shows), since Tautulli and Seerr never delete.
  const [addExclusion, setAddExclusion] = useState(instance?.add_import_exclusion ?? false);
  const [enabled, setEnabled] = useState(instance?.enabled ?? true);
  const [apiKey, setApiKey] = useState("");
  // The address links open, when it differs from the one Reaper connects to. Blank means
  // "use the address above": on save a blank value clears the stored one back to null.
  const [externalUrl, setExternalUrl] = useState(instance?.external_url ?? "");
  // A test result, and the exact credentials it was computed against. The two are stored
  // together because nothing else clears the badge: `setTest` is called only here and in the
  // mutation's `onSuccess`, so without this pairing, editing the hostname or the key after a
  // passing test would leave "Reached" on screen vouching for an address that was never
  // tried. Clearing it from each field's setter would be one more thing to remember every
  // time a field joins `baseUrl()`. Comparing against what was tested cannot be forgotten.
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
   *  matches what was tested, so any edit to an address, a key or a certificate setting takes
   *  the result down with it. Every field here is one the test's answer depends on: add a
   *  field to the request above and add it here too. */
  const testedWith = () => [kind, baseUrl(), apiKey, ssl ? verifyCert : true].join(" ");
  // Only Sonarr and Radarr delete, so only they carry the re-download switch.
  const isArr = kind === "radarr" || kind === "sonarr";

  // What the connection was when this modal opened. Compared against `testedWith()` to tell a
  // form that has been pointed somewhere new from one that is merely being renamed, which is
  // what decides whether a fresh test is demanded before Save (see `testRequired` below).
  /** The part of the connection a re-test is demanded for: the address and the key.
   *
   *  Narrower than `testedWith()` on purpose. That is the staleness key and rightly includes
   *  the certificate setting, since a pass computed with the check on does not vouch for it
   *  off. But demanding a fresh test for that switch would also demand a key, since the edit
   *  form's box is blank by design. Flipping "Check the server's certificate" would then tell
   *  the operator they had changed the address and block Save until they retyped a credential
   *  they had already stored. Turning the check off can only ever make a connection easier to
   *  make, so it is saved on its own terms. */
  const reachedAt = () => [kind, baseUrl(), apiKey].join(" ");
  const openedWith = useRef(reachedAt());
  const connectionEdited = reachedAt() !== openedWith.current;

  // The result currently vouching for what is on screen, or null. Everything downstream keys
  // off this rather than off `test` directly: a held result whose `of` no longer matches is a
  // result for an address that is no longer typed, and must vouch for nothing.
  const passed = test !== null && test.of === testedWith() && test.result.ok ? test : null;
  /** The result of a pre-save probe, or null when the test that passed was not one.
   *
   *  Only the probe answers the folder and service lists. A re-test of a saved instance
   *  answers the verdict alone. Reading `root_folders` off that would let an absent pair pose
   *  as "this instance has no folders", take the grid off the screen, and then prune the
   *  stored map to nothing at save. The two shapes are told apart by the field only one of
   *  them has, so the answer comes from the payload rather than from a flag captured beside
   *  it. */
  const probeResult = passed !== null && "map_error_reason" in passed.result ? passed.result : null;

  /** The lists that were actually read, or null. `map_error_reason` is the same hazard from
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
  // Uses the shared hook, which syncs a list that has never been synced. The pickers below
  // and their "suggested" tags read this same list: a separate plain query for the pickers
  // would answer `[]` on a fresh install, leaving them empty while the suggestion tags, from
  // a live Plex read, still named libraries that exist.
  const { libraries: plexLibraries, sync: syncLibraries } = usePlexLibraries({ enabled: isArr });
  const libOptions = (plexLibraries.data ?? []).filter((l) => l.kind === libKind);

  /** The root folders on screen, or null when none have been read.
   *
   *  Two sources, freshest first: a passing test carries the folders for the address that was
   *  just proved, and a saved instance can be asked by id. The test wins because it describes
   *  the address currently in the boxes, where the by-id read describes the address as
   *  stored: point a saved Radarr at a different server and the saved list is about the old
   *  one. Neither is allocated here, so this is a stable identity the prefill effect can
   *  depend on. */
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
   *  the same reason, freshest first. */
  const services: SeerrService[] | null = probed
    ? probed.seerr_services
    : (seerrServices.data ?? null);
  const instanceOptions = (svcKind: "sonarr" | "radarr") =>
    (arrInstances.data ?? []).filter((i) => i.kind === svcKind);
  // Seerr numbers Sonarr and Radarr services in separate lists (both from 0), so the map key
  // must be kind + id, not the id alone, or a sonarr and a radarr service collide.
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
  // The picker stores the instance id, so the name the operator actually read has to be
  // looked back up. The library picker beside it needs no equivalent, because there the
  // option's value is its title.
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

  /** Whether this press tests the stored instance rather than what is in the boxes.
   *
   *  A saved instance with an untouched address and a blank key ("leave blank to keep the
   *  current key") has nothing the pre-save probe could be sent: it would go out with an
   *  empty key and come back "refused that key". The by-id route tests exactly what is
   *  stored, which is the honest answer to "is this saved service reachable" and needs no
   *  key retyped. The stored key is never sent to an address the browser typed, which is why
   *  this is bounded by `!connectionEdited` and not merely by the key box being blank. */
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
    // Whether Reaper can reach the Sonarr it deletes through is worth hearing, which is what
    // makes this more than cosmetic. `instances.py` never raises for a failed test, so an
    // unreachable host arrives as a 200 with `ok=False` and never reaches the shared error
    // notice. The badge carries no live region, so the `announce` call below is the only way
    // a screen reader learns the result.
    // What this request is about, captured when it is issued rather than read back at
    // success time: `testedWith()` is derived from boxes the operator can keep typing into
    // while the request is in flight, so reading it later would file the answer against an
    // address it was never asked about.
    //
    // Which test answered is read off the payload's own shape
    // (`"map_error_reason" in result`, where `probeResult` is derived) rather than carried
    // alongside it as a flag, so it can never be captured against the wrong one.
    onMutate: () => ({ of: testedWith() }),
    onSuccess: (r, _v, issued) => {
      setTest({ result: r, of: issued.of });
      announce(testSentence(r));
    },
    onError: (e) => setError(describeError(e)),
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
        if (ssl) body.verify_tls = verifyCert; // moot over plain http, kept stored anyway
        if (isArr) body.add_import_exclusion = addExclusion;
        // Send the map only when a folder list has been read, and prune it only against a
        // list that is current. Rebuilding from the folders in hand drops entries for
        // folders the *arr no longer has, which is right when the list is current. When the
        // list is merely out of date, the same rebuild also drops entries for folders that
        // still exist. The map is what tells an HD copy from a 4K one, and Save would do
        // this silently.
        //
        // The read is stale, not absent, exactly when a refetch failed with the last good
        // list still held. React Query keeps that list, the grid below deliberately stays on
        // screen with the stale line over it, and `.data` is truthy throughout, so testing
        // `.data` alone cannot tell the two apart.
        //
        // On a stale list the operator's own picks still save: withholding those would drop
        // an edit they can see, which is the same silent loss from the other side. What is
        // withheld is deleting entries nothing has confirmed are gone, so both directions
        // fail toward keeping the mapping.
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
        // Same contract, same limit, same reasoning for the Seerr service map: sent when a
        // service list has been read, pruned only against one that is current, and omitted
        // only when none ever landed.
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
      // over a list that was actually read: on the add form that is the passing test's own
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
    onError: (e) => setError(describeError(e)),
  });

  // Either there is a whole address and key to send, or the instance is saved and untouched
  // and the by-id route can test what is stored. Without the second arm the button was dead
  // in exactly the states it exists for: a saved service the operator wants re-tested, where
  // the key box is blank by design, and re-testing meant retyping a key to prove nothing.
  const canTest =
    !testConn.isPending && ((host.trim() !== "" && apiKey.trim() !== "") || testsStored);

  /** Whether Save waits on a passing connection test.
   *
   *  Always on the add form: a service saved at an address Reaper never reached is a scan
   *  that fails later, on a screen that never mentioned it. On the edit form, only once
   *  something the connection depends on has changed. A stored instance was reachable when
   *  it was saved, and demanding a live test to fix a typo in its name would refuse the edit
   *  whenever the service happened to be down, which is the one moment an operator most
   *  wants to change something. Renaming stays free. Re-pointing does not. */
  const testRequired = !editing || connectionEdited;
  /** The key box is blank on the edit form and means "keep the stored key", which cannot be
   *  tested against a different address, because sending a stored secret to a host the
   *  browser just typed is exactly the shape an exfiltration bug takes. So re-pointing a
   *  saved instance asks for the key again, and the sentence below says so rather than
   *  letting the test fail as "refused that key". */
  const needsKeyToTest = testRequired && apiKey.trim() === "";

  /** How many of the folders on screen have a library, and whether at least one is required.
   *
   *  The floor lifts when there is nothing to satisfy it with: no folders read, or no
   *  libraries to pick from (a Plex that is down, or one that has never been synced). A
   *  requirement the operator has no way to meet is not a safeguard, it is a locked door.
   *  The map is there to tell an HD copy from a 4K one, which needs a library list to be
   *  possible at all. Seerr takes the prefill but no floor: its own help text says leaving a
   *  service unset is a real choice, meaning "credit everyone who asked". */
  const mappedFolders = folders ? folders.filter((f) => libMap[f.path]).length : 0;
  const mapFloorApplies = isArr && (folders?.length ?? 0) > 0 && libOptions.length > 0;
  const mapSatisfied = !mapFloorApplies || mappedFolders > 0;

  /** Whether a dismissal is allowed, computed once and handed to every path that can dismiss.
   *
   *  Two reasons to stay open. A close mid-save unmounts the only place the failure is ever
   *  shown: the scrim swallows a 409 "a service with that name already exists", `invalidate()`
   *  never runs, and the operator walks away believing the change saved. And a close over
   *  folders that were read but never mapped drops the one screen that can tell this
   *  instance's HD copy from its 4K one, silently.
   *
   *  Cancel is deliberately not gated on this: it is the deliberate way out, it saves
   *  nothing, and a guard whose only exit is the destructive button is a trap rather than a
   *  guard. What this refuses is the accidental dismissals: scrim, Escape, ✕, Back.
   *
   *  It is a mute gate, so the notice at the top of the form is what states the reason. */
  const canClose = !save.isPending && mapSatisfied;

  // Up to ServicesPanel's Back guard, whole rather than by term.
  useBackCloseMirror(blockCloseRef, canClose);

  const ready =
    name.trim() !== "" &&
    host.trim() !== "" &&
    (editing || apiKey.trim() !== "") &&
    (!testRequired || passed !== null) &&
    mapSatisfied;

  /** Fire the test for a combination that has not been tried yet.
   *
   *  Called from the blur of every box the connection depends on, so "enter the key and
   *  Reaper checks it" needs no button press, and from blur rather than from each keystroke,
   *  so typing a hostname does not send one request per character to a server that is about
   *  to be told a different one. A combination that already has a result is not re-sent. */
  const testIfUntried = () => {
    if (!canTest || (test && test.of === testedWith())) return;
    setError(null);
    testConn.mutate();
  };

  // Which box the submit button is waiting on, and the one sentence saying so. Three fields
  // can be empty at once but only the first is named: the operator fills it, the next one
  // appears, and each sentence names a single box rather than reciting a form. Ordered as
  // the boxes are on screen, so the complaint always points down the form, never back up it.
  //
  // The sentence binds to that box, not to the button. A `disabled` button is out of the Tab
  // order, so a description hung on it is unreachable by the operator it is for, the same
  // reasoning as the password row's `errorOwner`, which this mirrors.
  // The tail names what the button will do, read off the same `editing` the button's own
  // label is, so the two cannot come to disagree about it.
  //
  // The three boxes are still first and still named one at a time. What follows them is the
  // rest of the road to Save, in the order it is walked: the connection has to be proved, and
  // then the folders it handed back have to be mapped. Each reason names the control that
  // clears it, and `owner` is what binds the sentence to that control, including the two new
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
                  // it is disabled while it does, so a description hung on it would be
                  // unreachable by the operator it is for, the very trap the note above forbids.
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
              {/* The scheme is a fused prefix, not part of the box's name: the label wraps it,
                  so a reader would announce this field as "Hostname or IP http colon slash
                  slash" and the operator would hear punctuation where the box's job should
                  be. Hidden from the name only. It is drawn as before, and the "Use SSL"
                  switch below is the control that states and changes it, in words. */}
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
            // Once the key is entered, this tries to connect on leaving the box, so the
            // request goes once against a whole key rather than once per character of one.
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
        {/* Beside the control that fixes it. The form's one shared `error` slot renders after
            ~150 lines of other fields and doubles as where a failed save and a failed
            connection test land, so a complaint routed through it would be both out of reach
            of this box and impossible to bind to it without claiming a save failure was the
            URL's fault. */}
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
                open reaches this, and an undivided error would trade the whole mapping grid
                for one warning while React Query still held the folders.
                `mapErrorText` is the same division for the add form's source: the test
                reached the service but could not read its folders, which is not the same as a
                service that has none. */}
            {folders === null && (testConn.isPending || (editing && rootFolders.isPending)) ? (
              /* `editing &&` is load-bearing: this query is `enabled: editing && isArr`, and a
                 disabled query reports `status: "pending"` forever. Without this, the add form
                 would sit under "Reading this instance's folders…" from the moment it opened,
                 describing a read that was never started, and would take the sentence written
                 for that exact moment (below) off the screen entirely. */
              <p className="help">{t("services.modal.folders.reading")}</p>
            ) : folders === null && mapErrorText ? (
              /* Only when there is nothing to show. A probe that failed while the by-id read
                 still holds folders leaves the grid up and says this over it instead, because
                 the grid is the surface the operator needs and the failure is about a refresh
                 of it. */
              <Notice tone="warn">{mapErrorText}</Notice>
            ) : folders === null && !editing ? (
              /* The add form before a test. Says where the list comes from rather than showing
                 an empty grid, because on this form the folders are a result of the connection
                 test and not something that could have been read yet. */
              <p className="help">{t("services.modal.folders.beforeTest")}</p>
            ) : rootFolders.error && !rootFolders.data && !probed ? (
              /* `!probed` alone would let this outrank a live probe that just succeeded.
                 Re-pointing a saved *arr whose stored address is dead is the flow this whole
                 feature exists for: the by-id read fails against the old address, the operator
                 types the new one, the test passes and hands back folders, and without this
                 check that arm would still win. The grid would never render, Save would stay
                 off for a mapping with no picker to make it in, and the modal would refuse to
                 close. A guard whose signal outlives the surface that satisfies it is a trap,
                 and the sentence would be false besides, since the folders had just been read. */
              <Notice tone="warn">{t("services.modal.folders.readFailed")}</Notice>
            ) : folders && folders.length > 0 ? (
              <>
                {rootFolders.error && !probed && (
                  <StaleReadNotice what={t("services.modal.folders.staleWhat")} />
                )}
                {/* The probe failed while the by-id read still holds the folders. The grid
                    stays up, because it is the surface the operator needs and this is a
                    failure to refresh it, and the sentence sits over it like every other line
                    about something below. Rendered here as well as in the no-folders arm
                    above, which is the same fact in the two states it can be in. */}
                {mapErrorText && <Notice tone="warn">{mapErrorText}</Notice>}
                {/* Placed over the grid, beside the folder line, because the shared sentence
                    says what is below may be out of date, and the stale library names sit
                    inside the pickers under it. `JobsPanel.tsx` places its own version the
                    same way, above what it describes, and every other call site follows it.

                    `libOptions.length === 0` is what tells the two failures apart: a refetch
                    that fails with the libraries still held leaves every picker below
                    populated, so this reads as staleness rather than as a read failure, which
                    is what it actually is. */}
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
                          // The "map at least one" sentence binds to the first unmapped
                          // picker, which is a control that can actually be reached and used,
                          // where the disabled Save button it describes is out of the Tab
                          // order entirely.
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
                            one string in the control, on the screen that decides which folder
                            Reaper matches to which library. `aria-hidden` because the select
                            already announces its full selected option: the clipping is
                            visual, so the repair is too, and saying it twice would be the only
                            thing a screen reader noticed. Rendered only when set, since "Not
                            set" is legible at any width. */}
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
                    state as fact something never learned, sending the operator off to re-sync
                    a list that is already there. The empty sentence is only for a list that
                    was genuinely read and found empty. This one stays under the grid: it
                    replaces the help text rather than describing the pickers.

                    This never tells anyone to go to Plex settings and press Sync: the list
                    here is filled by a sync, and `usePlexLibraries` runs it right here, so a
                    first-run operator is never sent elsewhere to do the app's own job. */}
                {(plexLibraries.error || syncLibraries.error) && libOptions.length === 0 ? (
                  /* `syncLibraries.error` belongs here, not only the query's. The read can
                     succeed with `[]` while the sync that would fill it fails: Plex not
                     linked at all answers 400, Plex down answers 502. With only the query
                     consulted, the arm below would state as fact that the server has no
                     libraries of this kind, about a server nobody reached. */
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
              // A list that landed empty and then failed to refetch reaches this arm, not the
              // never-landed one above: `[]` is truthy, so `!rootFolders.data` is false. The
              // sentence below is a positive claim about the instance, so it takes the stale
              // line like every other held value. Without it, the one state where the read
              // failed and the app has something to say would render no warning at all.
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
            {/* Divided exactly as the folder grid above, including the add form's before-a-test
                arm and `mapErrorText` for a portal that answered but would not hand over its
                settings. */}
            {services === null && (testConn.isPending || (editing && seerrServices.isPending)) ? (
              /* `editing &&` for the same reason as the folder slot above. */
              <p className="help">{t("services.modal.requesters.reading")}</p>
            ) : services === null && mapErrorText ? (
              <Notice tone="warn">{mapErrorText}</Notice>
            ) : services === null && !editing ? (
              <p className="help">{t("services.modal.requesters.beforeTest")}</p>
            ) : seerrServices.error && !seerrServices.data && !probed ? (
              /* `!probed` for the same reason as the folder arm above. */
              <Notice tone="warn">{t("services.modal.requesters.readFailed")}</Notice>
            ) : services && services.length > 0 ? (
              <>
                {seerrServices.error && !probed && (
                  <StaleReadNotice what={t("services.modal.requesters.staleWhat")} />
                )}
                {/* Same as the folder grid above. */}
                {mapErrorText && <Notice tone="warn">{mapErrorText}</Notice>}
                {/* Over the grid, for the same reason as the library line above: the stale
                    connection names are inside the pickers below it. */}
                {arrInstances.error && services.some((s) => instanceOptions(s.kind).length > 0) && (
                  <StaleReadNotice what={t("services.modal.requesters.instancesStaleWhat")} />
                )}
                <div className="plex-map-grid">
                  {services.map((s) => (
                    <Fragment key={svcKey(s)}>
                      {/* Every part of `svcKey` (kind + id) that a person can see has to be on
                          this cell, because the name alone does not identify the row: a portal
                          numbers and names its Sonarr and Radarr lists independently, so one
                          can hold a TV and a Movies service both called "Media". The kind
                          rides beside the name the way the 4K marker already does, in that
                          order: what the service is, then which copy of it. */}
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
                          // Same reason as the library picker above, and the spoken name
                          // carries exactly what the cell beside it shows: both tags
                          // distinguish rows a shared name would otherwise merge, so both
                          // belong here too. Said in words rather than as the stored kind,
                          // and taken from `serviceKindLabel` so the two spellings cannot
                          // drift.
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
                            above. */}
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
                    `instanceOptions` empty, and "none yet" would be a claim never checked.
                    Divided the same way, so a refetch that fails with the connections still
                    held reads as stale rather than as unreadable beside populated pickers.
                    This one stays under the grid: it replaces the help text, it does not
                    describe the pickers. */}
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
              // The empty-and-then-failed arm, exactly as the folder grid above.
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
        {/* Why the ✕ and Escape are doing nothing, right beside Cancel, which is the way out
            it names, and right above the pickers that satisfy it. Only ever on screen while
            `canClose` is false, so the notice and the guard can never disagree about whether
            the form is holding something. */}
        {!mapSatisfied && <Notice tone="warn">{t("services.modal.mapWarning")}</Notice>}
        <div className="add-actions">
          {/* Shown on both the add and edit forms. The test fires on its own when a box is
              left, so this button is the way back from the states no blur will reach: a
              switch that was flipped, a service that was down a moment ago, a result the
              operator wants re-taken. Hiding it during editing would leave the two notices
              above pointing at a button that is nowhere on screen. */}
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
