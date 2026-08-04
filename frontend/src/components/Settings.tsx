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
  type ReactNode,
  type RefObject,
  useEffect,
  useRef,
  useState,
} from "react";
import { accentInk, accentText, DEFAULT_ACCENT, isHexColor } from "../accent";
import { announce } from "../announce";
import { useSavebarFocus, useSuccessorFocus } from "../focus";
import {
  api,
  type InstanceKind,
  type ExpandSeasonsMode,
  type Instance,
  type InstanceTest,
  type ReleaseChange,
  type Schedule,
  type ScheduledJob,
} from "../api";
import Markdown from "react-markdown";
import { useUpdateStatus } from "../updateStatus";
import { useBackGuard } from "../backnav";
import { bytes, count, since } from "../format";
import { NARROW_SCREEN_QUERY, useMediaQuery } from "../useMediaQuery";
import { useSafety } from "../useSafety";
import { JobStatus, useJobFlash } from "./JobStatus";
import { LogsPanel } from "./LogsPanel";
import { ModalShell } from "./ModalShell";
import { PlexPanel } from "./PlexPanel";
import { FixedQuantity } from "./QuantityInput";
import { RestoreCard } from "./RestoreCard";
import { ScanRow } from "./ScanBar";
import { Segmented } from "./Segmented";
import { KINDS, kindLabel, ServiceModal, TestBadge, testSentence } from "./ServiceModal";
import {
  StaleReadNotice,
  type StaleReadPlan,
  StaleReadSlot,
  collapseStaleReads,
} from "./StaleReadNotice";
import { Switch } from "./Switch";
import { ListsPanel } from "./ListsPanel";
import { Notice } from "./Notice";
import { SwitchConfirm } from "./SwitchConfirm";

// The Plex panel moved to its own file; SetupWizard imports it from here, so the name
// stays available at this path.
export { PlexPanel };

export type Panel =
  | "general"
  | "services"
  | "plex"
  | "lists"
  | "jobs"
  | "notifications"
  | "security"
  | "backup"
  | "logs"
  | "about";

/** The ten sections, in rail order. Exported for the one test that owns the hand-written label
 *  table this must agree with (SettingsNav.test.tsx), so a section added here fails there naming
 *  what to do rather than as an unexplained label mismatch (rules 103, 144). */
export const PANELS: { id: Panel; label: string }[] = [
  { id: "general", label: "General" },
  { id: "services", label: "Services" },
  { id: "plex", label: "Plex" },
  { id: "lists", label: "Lists" },
  { id: "jobs", label: "Jobs" },
  { id: "notifications", label: "Notifications" },
  { id: "security", label: "Security" },
  // Named for both halves, matching the panel's own heading: restoring is the half an operator
  // comes looking for under pressure, and a tab reading "Backup" alone hides it.
  { id: "backup", label: "Backup & Restore" },
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
// via the field, so this is a shortcut.
// Each carries the color's name, because the swatch is a bare colored circle: its only other
// name would be the hex, which a screen reader spells out one character at a time and which
// rule 21 would not accept as operator copy either.
const ACCENT_PRESETS: { value: string; name: string }[] = [
  { value: DEFAULT_ACCENT, name: "Reaper blue" },
  { value: "#4f46e5", name: "Indigo" },
  { value: "#7c3aed", name: "Violet" },
  { value: "#0ea5e9", name: "Sky" },
  { value: "#14b8a6", name: "Teal" },
  { value: "#f59e0b", name: "Amber" },
  { value: "#ec4899", name: "Pink" },
];

// The browser's full IANA zone list, fetched once and cached: it never changes within a
// session, so recomputing it per render would be waste. Falls back to just UTC on an engine
// without Intl.supportedValuesOf, so the control still works.
let _zoneCache: string[] | null = null;
function allTimeZones(): string[] {
  if (_zoneCache) return _zoneCache;
  let zones: string[] = [];
  try {
    const supported = (Intl as { supportedValuesOf?: (key: string) => string[] }).supportedValuesOf;
    if (supported) zones = supported("timeZone");
  } catch {
    zones = [];
  }
  _zoneCache = zones.length ? zones : ["UTC"];
  return _zoneCache;
}

// What the day box starts at while the stored default is Forever, so pressing Days opens on a
// sensible length instead of an empty box. One declaration, because the seed and Discard have to
// agree: Discard putting back a different number is how a discarded draft came back (issue #90).
const SPARE_DAYS_SEED = 30;

/** The hex field's refusal message, named once so the box's `aria-describedby` and the message's
 *  own `id` are the same string rather than two that can drift (rule 67). A module constant and
 *  not a `useId`: this panel is a singleton, and the id is only useful while the message is
 *  rendered -- which is exactly when the box points at it. */
const ACCENT_ERROR_ID = "accent-hex-error";

export function GeneralPanel({
  /** Called whenever the save bar gains or loses a draft, so the section rail can hold a
   *  switch that would discard one. Pass a STABLE function: it is an effect dependency. */
  onDirtyChange,
}: {
  onDirtyChange?: ((dirty: boolean) => void) | undefined;
} = {}) {
  const queryClient = useQueryClient();
  const general = useQuery({ queryKey: ["general-settings"], queryFn: api.general });
  // Save and Discard both unmount the bar holding the pressed button (#173), the twin of the
  // policy editor's (rule 72). Declared ABOVE every early return, which is rule 146's shape:
  // this panel returns a loading line and a failure notice before the form exists, and a hook
  // below either is a different hook order on those renders.
  const bar = useSavebarFocus();

  const [name, setName] = useState("");
  const [url, setUrl] = useState("");
  const [tz, setTz] = useState("");
  const [proxies, setProxies] = useState("");
  const [accent, setAccent] = useState(DEFAULT_ACCENT);
  // The default spare length, as a draft in two halves: which mode is chosen, and the box's
  // live number. They are held apart because Forever stores 0 and the typed number has to
  // survive a trip through it; `spareValue` below folds them back into the one stored field.
  // The number seeds to a sensible 30 while the stored default is Forever, so switching to a
  // length starts somewhere reasonable.
  const [spareForever, setSpareForever] = useState(false);
  const [spareDays, setSpareDays] = useState(SPARE_DAYS_SEED);
  const [theme, setTheme] = useState<ThemeChoice>(readTheme);
  // Which theme the preview is being SEEN in, which "system" alone does not say. The accent's
  // text ink is computed per theme, so the preview needs the resolved one, not the choice.
  const systemDark = useMediaQuery("(prefers-color-scheme: dark)");
  const shownTheme: "light" | "dark" = theme === "system" ? (systemDark ? "dark" : "light") : theme;
  const [revealedKey, setRevealedKey] = useState<string | null>(null);
  const [copied, setCopied] = useState(false);
  const [confirmReplace, setConfirmReplace] = useState(false);
  const [confirmRemove, setConfirmRemove] = useState(false);
  // Removing the key unmounts the whole key-present block, taking the pressed Confirm with it, and
  // the button is `disabled` while the write is in flight so focus is at `<body>` before that even
  // happens (#173). Focus lands on "Generate API key", which is not a nearby neighbour but the one
  // thing left to do in this row -- and it only mounts once the refetch says the key is gone, which
  // is the round trip `useSuccessorFocus` exists to wait out.
  const afterKeyRemove = useSuccessorFocus();

  // Seed the editable fields from the server once per load (and re-seed after saves,
  // which return the canonical stored values -- rule 39).
  //
  // State rather than a ref, because the RENDER has to read it. An effect runs after the
  // commit, so the first pass where `general.data` exists paints with every box still on its
  // initial value ("", the accent default, spare 0) while `data` already holds the stored
  // ones -- and the dirty checks below then name four fields nobody typed in. `useEffect`
  // runs after paint, so that frame reaches the screen: the save bar appeared on its own on
  // every load of this panel and cleared itself a commit later. A ref would fix nothing here,
  // since mutating one does not re-render and the value read during that first pass would
  // still be the stale one.
  const [seeded, setSeeded] = useState(false);
  useEffect(() => {
    if (!general.data || seeded) return;
    setSeeded(true);
    setName(general.data.application_name);
    setUrl(general.data.application_url ?? "");
    setTz(general.data.timezone);
    setProxies(general.data.trusted_proxies.join(", "));
    setAccent(general.data.accent_color);
    setSpareForever(general.data.default_spare_days === 0);
    if (general.data.default_spare_days > 0) setSpareDays(general.data.default_spare_days);
  }, [general.data, seeded]);

  const save = useMutation({
    mutationFn: api.saveGeneral,
    onSuccess: (data, sent) => {
      // Re-seed from the canonical stored values (rule 39) -- but only the fields this Save
      // actually sent. The save bar sends every dirty field at once, so it no longer takes one
      // row's Save to reach here; the two controls that still save on the spot do. A Switch or
      // a select (the reverse-proxy toggle, the expand-seasons mode) writes immediately, and
      // re-seeding every field on its response would wipe whatever text was half-typed at the
      // time, with nothing on screen to say why (B-18).
      //
      // Setting the query cache stays unconditional: it is the canonical stored state, and it
      // is what re-applies the accent app-wide so a save re-tints everything.
      //
      // Two shapes reach here and both were silent: the savebar, whose success was the bar
      // unmounting under the button that had focus, and the two controls that save on the spot,
      // whose success was nothing at all.
      announce("Settings saved.");
      queryClient.setQueryData(["general-settings"], data);
      if ("application_name" in sent) setName(data.application_name);
      if ("application_url" in sent) setUrl(data.application_url ?? "");
      if ("timezone" in sent) setTz(data.timezone);
      if ("trusted_proxies" in sent) setProxies(data.trusted_proxies.join(", "));
      if ("accent_color" in sent) setAccent(data.accent_color);
      if ("default_spare_days" in sent) {
        setSpareForever(data.default_spare_days === 0);
        if (data.default_spare_days > 0) setSpareDays(data.default_spare_days);
      }
    },
  });

  // Three buttons (Show, Generate/Replace, Copy) share one notice, and a mutation holds its
  // error until its OWN next call -- so rendering `reveal.error ?? generate.error ?? copy.error`
  // left a failure on screen beside a key that had since worked: fail Copy on a plain-http LAN
  // page, then press Show, and the red notice was still the copy failure (B-33). The three
  // report through this one piece of state instead, cleared the moment any of them starts, so
  // the notice always describes the last thing the operator did.
  const [keyError, setKeyError] = useState<string | null>(null);
  const reveal = useMutation({
    mutationFn: api.revealApiKey,
    onMutate: () => setKeyError(null),
    // Show swaps a readonly box from dots to the live secret and flips its own button to
    // Hide. Revealing a credential on screen is worth saying out loud -- not least because
    // the operator may be somewhere they would rather it stayed hidden. The key itself is
    // never announced: it is in the box, and a live region is the wrong place for a secret.
    onSuccess: (r) => {
      setRevealedKey(r.key);
      announce("API key shown.");
    },
    onError: (e: Error) => setKeyError(e.message),
  });
  const generate = useMutation({
    mutationFn: api.generateApiKey,
    onMutate: () => setKeyError(null),
    onSuccess: (r) => {
      setRevealedKey(r.key);
      setConfirmReplace(false);
      announce("New API key generated. The old one no longer works.");
      void queryClient.invalidateQueries({ queryKey: ["general-settings"] });
    },
    onError: (e: Error) => setKeyError(e.message),
  });

  // Generating REPLACES whatever key the server holds, the moment it returns and with no undo,
  // which is why Replace two branches down is a two-step confirm reading "The old key stops
  // working immediately". The bare one-click Generate renders on `api_key_set === false` -- and
  // that is a CACHED answer: `["general-settings"]` has a 30-second staleTime,
  // `refetchOnWindowFocus` is off app-wide, and nothing else evicts it, so a key made from
  // another tab, a phone, or by another admin left this panel offering a one-click revoke of a
  // live key with none of the confirmation its own design says the action needs (#203).
  //
  // So this proves the absence rather than assuming it: re-read, and only a FRESH "no key"
  // generates straight away. That keeps the first-run flow at one click, which matters -- the
  // honest reading of a page parked for a minute is "I don't know yet", not "this is dangerous",
  // and putting a danger confirm in front of every setup is its own false claim. Neither other
  // answer generates:
  //   - a key exists: the row has already re-rendered into its key-present layout, Replace and
  //     all, on the fresh data. Say so and stop.
  //   - the re-read failed: nothing is provable, so fall back to the confirm. Rule 53's class --
  //     a control whose gate is derived from a value that went stale -- for a destructive button
  //     rather than a rendered limit.
  //
  // Only the un-armed press comes through here. The two armed buttons call `generate` straight,
  // the way `Confirm replace` beside them does, so there is no `if (confirmReplace)` arm at the
  // top of this function: the only handler that reaches it renders on the branch where the flag
  // is false, so such an arm is unreachable, and one that reads as though it routes the confirmed
  // press earns a later author the belief that path re-proves absence too.
  // A mutation, not a bare async onClick, the shape `copy` below uses for the same reason (rule
  // 17/36): the re-read is a round trip, and `retry: 1` app-wide means a failing one costs two
  // requests and a backoff between them. Through all of it `generate` has not started, so
  // `generate.isPending` -- the button's only pending input -- was false, and the button sat
  // enabled under its idle label looking dead. A second press then ran a second check, and both
  // cleared, so two keys were minted back to back: the second revokes the first, and whichever
  // RESPONSE landed last is the one left in the box, which need not be the key the server kept.
  // The operator copies it and it returns 401.
  const requestGenerate = useMutation({
    onMutate: () => setKeyError(null),
    mutationFn: async () => {
      const fresh = await general.refetch();
      if (fresh.isError) {
        setConfirmReplace(true);
        setKeyError("Couldn't check for an existing key. Confirming replaces one if it's there.");
        return;
      }
      if (fresh.data?.api_key_set) {
        setKeyError("A key already exists. Use Replace to make a new one.");
        return;
      }
      generate.mutate();
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
      throw new Error(
        "Copying needs a secure (https) page. Press Show, then select the key by hand.",
      );
    }
    await navigator.clipboard.writeText(key);
    // The only feedback was the button's own label reading "Copied" for two seconds -- a
    // change to the name of the control the operator is standing on, which is not announced.
    announce("API key copied.");
    setCopied(true);
    setTimeout(() => setCopied(false), 2000);
  };
  const copy = useMutation({
    mutationFn: copyKey,
    onMutate: () => setKeyError(null),
    onError: (e: Error) => setKeyError(e.message),
  });
  const removeKey = useMutation({
    mutationFn: api.removeApiKey,
    onMutate: () => setKeyError(null),
    onError: (e: Error) => setKeyError(e.message),
    onSuccess: () => {
      setRevealedKey(null);
      setConfirmRemove(false);
      // And the OTHER confirm, which this row also renders. Cleared here as well as in the effect
      // below, because the effect waits on the refetch: for that round trip `api_key_set` is still
      // true, so a Replace armed before the operator changed their mind to Remove would sit armed
      // over a key that is already gone.
      setConfirmReplace(false);
      // The three neighbours above all announce; this one's entire success signal was the key
      // block unmounting and taking the pressed Confirm with it, which is an absence and cannot
      // be heard (#192's shape, missed in its sweep).
      //
      // It names the consequence, not the wire mechanism (#221). "Nothing gets in on the header
      // now" was the one announcement here carrying a definite reference with no antecedent in
      // its own sentence: "the header" resolved only against the help paragraph below, and an
      // announcement is delivered through a live region, so it is HEARD ALONE and cannot borrow
      // a referent from elsewhere on the page (rule 21). The wording is that paragraph's own
      // phrase for what the key is for, so the two read alike (rule 144), and it follows the
      // house pattern the Discord `remove` mutation sets in `NotificationsPanel`: say what the
      // operator loses, not what stops working on the wire.
      announce(
        "API key removed. Scripts and other apps can no longer use Reaper without signing in.",
      );
      void queryClient.invalidateQueries({ queryKey: ["general-settings"] });
    },
  });

  // A confirm belongs to the row that raised it. `api_key_set` decides WHICH row renders, and one
  // flag arms a danger button on each of them, so it must not cross: a Replace armed on the
  // key-present row was still armed when Remove took the key away, and the no-key row then opened
  // on "Confirm generate" with no notice to explain it. The other direction is the worse one --
  // the fallback arms this flag while no key exists, so a key arriving from another tab (#203's
  // own scenario, pointed back the other way) re-rendered the key-present row with "Confirm
  // replace" ALREADY armed, leaving a live key one press from a confirm the operator never opened.
  // Whichever way the row changes, nothing destructive stays pressed.
  const keyPresent = general.data?.api_key_set;
  useEffect(() => {
    setConfirmReplace(false);
    setConfirmRemove(false);
  }, [keyPresent]);

  // The dirty checks and the pending list are computed BEFORE the early returns below, because
  // the effect that reports them to `Settings` is a hook and a hook may not sit after a
  // conditional return. Each one is guarded on `data`, which the second early return then
  // narrows to non-null for the whole render beneath it.
  const data = general.data;

  // `seeded`, not just `data`: between the commit that first has `data` and the effect above
  // that copies it into these boxes, every one of them still holds its initial value and so
  // differs from the stored one. Comparing there reports a draft the operator never typed
  // (#139). The same one-frame report reached `Settings` through `onDirtyChange`, which is
  // what makes this two claims rather than a cosmetic flash (rule 146). `PlexPanel` carries
  // the same defect on its two mirrored fields and is fixed beside this one (rule 72), by a
  // different guard: it re-seeds on every change of the stored value where this seeds once,
  // so it has to ask which value it was seeded FROM rather than merely whether it has been.
  const ready = !!data && seeded;

  const nameDirty = ready && name.trim() !== data.application_name;
  const urlDirty = ready && url.trim() !== (data.application_url ?? "");
  const tzDirty = ready && tz !== data.timezone;
  const accentValid = isHexColor(accent);
  const accentDirty = ready && accent.trim().toLowerCase() !== data.accent_color.toLowerCase();
  const proxyList = proxies
    .split(",")
    .map((p) => p.trim())
    .filter(Boolean);
  const proxiesDirty = ready && proxyList.join(", ") !== data.trusted_proxies.join(", ");
  // The two halves of the draft fold back into the one stored number before anything compares
  // them, because Forever IS 0 in that field. Pressing Forever therefore reads as a change to
  // the same field the box edits, and one Discard puts both back. It used to write 0 on the
  // press while the bar, gated on the stored value, unmounted and took its Discard with it --
  // so the number the bar had just called unsaved went in on the next press, without a Save.
  const spareValue = spareForever ? 0 : spareDays;
  const spareDirty = ready && spareValue !== data.default_spare_days;

  // One save for the panel (rule 43). Each of these rows used to carry its own Save, rendered
  // inside the right-aligned control box, so the first keystroke made the button appear and
  // shoved the field being typed in 71px to the left -- then back again on undo. The bar names
  // what is unsaved and sends all of it in one request. The controls that take effect the
  // moment they change are not drafts and do not join it: two of them call `save.mutate`
  // themselves -- the reverse-proxy `Switch` and the expand-seasons `<select>` -- and the theme
  // `<select>` calls `applyTheme`, which writes this browser's own localStorage and never
  // reaches the server, so it has no draft to hold. The spare-length `Segmented` was a third
  // until it started staging `default_spare_days` here instead (see `spareValue` above).
  const pending: { label: string; patch: Parameters<typeof api.saveGeneral>[0] }[] = [];
  if (nameDirty)
    pending.push({ label: "Application name", patch: { application_name: name.trim() } });
  if (urlDirty) pending.push({ label: "Application URL", patch: { application_url: url.trim() } });
  if (tzDirty) pending.push({ label: "Time zone", patch: { timezone: tz } });
  if (accentDirty)
    pending.push({ label: "Accent color", patch: { accent_color: accent.trim().toLowerCase() } });
  if (spareDirty)
    pending.push({ label: "Default spare length", patch: { default_spare_days: spareValue } });
  // Only while the switch is on. Turning it off disables the box, and a bar naming a field the
  // operator cannot reach to fix is worse than one that waits for them to turn it back on.
  if (proxiesDirty && data?.proxy_trust_enabled)
    pending.push({ label: "Trusted proxy addresses", patch: { trusted_proxies: proxyList } });
  // A half-typed hex code would be stored as the app-wide accent, so the whole save waits on
  // it rather than silently dropping that one field from a bar that just named it.
  const accentBlocks = accentDirty && !accentValid;

  // What this panel would LOSE, reported up to `Settings` so that leaving the section can stop
  // and ask first. Nearly always that is the bar (rule 43), but not quite, so the two are
  // computed apart rather than one read off the other. A proxy list typed and then parked behind
  // its own switch is dropped from `pending` on purpose just above, because the bar must not name
  // a field the operator cannot reach to fix -- yet the text is still sitting in the disabled
  // box, still unsaved, and still gone on unmount. Reading the bar alone let exactly that one
  // walk out silently, on the panel that had just promised to ask.
  //
  // Rule 146: this reports two things at once, that there is something to lose and that the
  // operator can still reach it, so both are read against every early return below -- the
  // report fires while this renders "Loading…" (nothing is dirty yet, `data` is undefined) and
  // it must not outlive the form, which is why the failure branch below now keeps the form
  // whenever there is a row to render.
  const hasDrafts = pending.length > 0 || (proxiesDirty && !data?.proxy_trust_enabled);
  useEffect(() => {
    onDirtyChange?.(hasDrafts);
  }, [hasDrafts, onDirtyChange]);
  useEffect(() => () => onDirtyChange?.(false), [onDirtyChange]);

  if (general.isPending) {
    return <p className="muted">Loading…</p>;
  }
  // Only when there is nothing to render. A refetch that fails AFTER a good load leaves `data` in
  // place (React Query keeps the last good row and raises `isError` beside it), and trading the
  // whole form for this paragraph there took the save bar and its Discard with it while the
  // drafts stayed in state -- still reported unsaved to `Settings`, which then asked to discard
  // edits the operator could no longer see, save, or put back. Pressing Generate API key on a
  // server that blinks is enough to reach it, because that invalidates this very query. So a
  // failed refetch keeps the form on the last good values; this is for a load that never
  // landed one.
  if (!data) {
    return <Notice tone="error">Couldn't load these settings. Reload to try again.</Notice>;
  }

  // The current zone may not be in the browser's list (an older engine, or a server-only
  // zone); keep it selectable so a save never silently drops it.
  const zoneOptions =
    data.timezone && !allTimeZones().includes(data.timezone)
      ? [data.timezone, ...allTimeZones()]
      : allTimeZones();

  const discardDrafts = () => {
    bar.leaving();
    setName(data.application_name);
    setUrl(data.application_url ?? "");
    setTz(data.timezone);
    setAccent(data.accent_color);
    setProxies(data.trusted_proxies.join(", "));
    setSpareForever(data.default_spare_days === 0);
    // BOTH halves, unlike the mount seed and the save response above, which leave the number
    // alone under a stored Forever so the last length is remembered. Discard is a full undo, so
    // it goes back to the stored length or to the same number the box seeds at. Skipping it left
    // the discarded figure in the hidden box, and the next press of Days re-staged it.
    setSpareDays(data.default_spare_days > 0 ? data.default_spare_days : SPARE_DAYS_SEED);
  };

  return (
    <div className="panel">
      <h2 ref={bar.ref as RefObject<HTMLHeadingElement>} tabIndex={-1}>
        General
      </h2>
      <p className="muted">How this Reaper presents itself, and how other tools may talk to it.</p>

      {/* Same obligation as the twin in `PlexPanel` (rule 72): the `!data` branch above keeps the
          form through a failed refetch so the drafts in it stay reachable, which leaves this line
          the only thing saying the values below may be stale. */}
      {general.isError && <StaleReadNotice />}

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
            </div>
          </div>
          <div className="set-row">
            <span className="set-label">Application URL</span>
            <p className="help">
              Where people reach Reaper, for example https://reaper.example.com. Notifications use
              it to link back here. Leave empty and notifications simply skip the link.
            </p>
            <div className="set-control">
              <input
                type="text"
                value={url}
                placeholder="https://reaper.example.com"
                onChange={(e) => setUrl(e.target.value)}
                aria-label="Application URL"
              />
            </div>
          </div>
          <div className="set-row">
            <span className="set-label">Time zone</span>
            <p className="help">The server's time zone.</p>
            <div className="set-control">
              <select value={tz} aria-label="Time zone" onChange={(e) => setTz(e.target.value)}>
                {zoneOptions.map((z) => (
                  <option key={z} value={z}>
                    {z}
                  </option>
                ))}
              </select>
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
                  // The box refuses the save and the sentence saying why sits below it, out of
                  // reach of anyone who arrived at the box by keyboard (#174).
                  aria-invalid={accentValid ? undefined : true}
                  aria-describedby={accentValid ? undefined : ACCENT_ERROR_ID}
                  onChange={(e) => setAccent(e.target.value)}
                />
              </span>
              {accent.toLowerCase() !== DEFAULT_ACCENT && (
                <button className="link" onClick={() => setAccent(DEFAULT_ACCENT)}>
                  Reset to default
                </button>
              )}
            </div>
            {!accentValid && (
              <p className="help field-error" id={ACCENT_ERROR_ID}>
                Enter a hex code like #25c3ff.
              </p>
            )}
            {/* role="group" is what carries the name: ARIA does not expose an aria-label on a
                plain div, so "Quick colors" reached nobody. Same shape as `Segmented`. */}
            <div className="presets" role="group" aria-label="Quick colors">
              {ACCENT_PRESETS.map((c) => (
                <button
                  key={c.value}
                  type="button"
                  className="preset-dot"
                  style={{ background: c.value }}
                  aria-label={c.name}
                  aria-pressed={accent.toLowerCase() === c.value}
                  onClick={() => setAccent(c.value)}
                />
              ))}
            </div>
            {/* A picture of the theme, not working controls: the button is disabled and the link
                goes nowhere on purpose. Hidden from the accessibility tree so the dead link stops
                being announced as a real way to reach the deletion switch, and tabIndex -1 keeps
                it out of the tab order (a focusable element inside aria-hidden is itself a
                failure). The disabled button is already out of both. */}
            <div
              className="accent-preview"
              aria-hidden="true"
              style={
                accentValid
                  ? ({
                      "--accent": accent,
                      "--accent-ink": accentInk(accent),
                      // --accent-text as well, or the link in the preview keeps the SAVED
                      // accent's ink while the button beside it moves. It is not derived from
                      // --accent at use time: the stylesheet computes it once on :root, from
                      // the values accent.ts writes there, so a child overriding --accent alone
                      // inherits an ink belonging to a different color (rule 67).
                      "--accent-text": accentText(accent, shownTheme),
                    } as CSSProperties)
                  : undefined
              }
            >
              <span className="pv-label">Preview</span>
              <button className="primary" type="button" disabled>
                Scan library
              </button>
              <a href="#" tabIndex={-1} onClick={(e) => e.preventDefault()}>
                Policy → Deletion
              </a>
            </div>
          </div>

          <div className="set-row">
            <span className="set-label">Theme</span>
            <p className="help">
              Light or dark. "Match my device" follows your system setting. Applies to this browser
              only.
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
              TV shows in the review queue open with every season showing. Mobile means a narrow
              screen, like a phone.
            </p>
            <div className="set-control">
              {/* Four choices, so a select rather than a Segmented (rule 41), on the same
                  control standard as the Theme picker above. */}
              <select
                value={data.expand_seasons_mode}
                aria-label="Expand seasons by default"
                disabled={save.isPending}
                onChange={(e) =>
                  save.mutate({ expand_seasons_mode: e.target.value as ExpandSeasonsMode })
                }
              >
                <option value="off">Off</option>
                <option value="desktop">Desktop</option>
                <option value="both">Desktop &amp; mobile</option>
                <option value="mobile">Mobile</option>
              </select>
            </div>
          </div>
          <div className="set-row">
            <span className="set-label">Default spare length</span>
            <p className="help">
              How long a plain Spare keeps a title before Reaper judges it again. Set a different
              length for any single title from its Spare menu.
            </p>
            <div className="set-control">
              {/* Both halves read and write the DRAFT, never the stored value. A press stages
                  the mode in the save bar beside the number, so the bar names one field, one
                  Discard puts both back, and neither is written until Save.

                  Both halves also stop taking presses while the save is in flight, for the
                  same reason: `save`'s `onSuccess` re-seeds this mode from the response, so a
                  press landing in the gap was overwritten and the bar cleared in the same
                  flush, leaving nothing that said so (#151). */}
              <Segmented
                value={spareForever ? "forever" : "days"}
                options={[
                  ["days", "Days"],
                  ["forever", "Forever"],
                ]}
                label="Default spare length"
                disabled={save.isPending}
                onChange={(mode) => setSpareForever(mode === "forever")}
              />
              {/* Only while the draft is a length -- Forever hides the box, matching how a
                  group's sub-controls disappear when its toggle is off. */}
              {!spareForever && (
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
              )}
            </div>
          </div>
        </div>
      </div>

      <div className="set-group">
        <h3>API access</h3>
        <div className="set-rows">
          {/* A cluster, not a box: the key field plus four buttons. It keeps a shrink-to-fit
              control column so those buttons stay on one line (see `.set-row-cluster`). */}
          <div className="set-row set-row-cluster">
            <span className="set-label">API key</span>
            {/* This sentence is the whole basis on which an operator decides to hand a key
                to a third-party dashboard, so it names what the fence in api/middleware.py
                (_API_KEY_READS_DENIED / _API_KEY_WRITES) actually allows, not a rounder
                claim. Two roundings have already been wrong here, both in the safe-sounding
                direction:

                - it said a key "cannot change any setting" while /api/profile sat in the
                  write allowlist, so a key holder could turn the run limits off (S-2);
                - it said a key "reads your library", which is most of what a key reads and
                  not the part that decides the question. A key also reads every settings
                  page, and it read one person's whole viewing breakdown until #117 moved
                  /api/fairness behind the browser.

                So the read clause overstates rather than understates: on the screen where a
                key is handed out, "more than you think" is the direction that fails safe.
                Note which way that cuts on the viewing clause. It is now a REFUSAL, so it
                moved to the closing list, where the safe direction reverses: naming a
                refusal the fence does not make is the rounding that gets someone hurt, and
                this one is only true while /api/fairness stays denied. The list still ends
                with "any other setting" rather than enumerating the rest, for the reason
                api_key_scope_description exists: the fence is far tighter than any short
                list of what it refuses, and naming four of them read as a promise that the
                rest were allowed.

                This paragraph is hand-written and its twin in the API reference is
                generated, so nothing here fails when the fence moves. The guard is on the
                other side: test_the_sentence_leads_with_what_the_key_can_do pins the twin
                phrase for phrase and names this file in every failure message. */}
            <p className="help">
              Send it as the X-Api-Key header so scripts and other apps can use Reaper without
              signing in. A key reads nearly everything, your settings included, and can start
              scans, build plans, and change your policy, run limits, and grace. Nothing else: it
              cannot turn deletion on, run a reap, read your logs, see who watched what, or change
              any other setting.
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
                      {/* Backing out clears the notice too. It is the shared one (above), and the
                          only thing that clears it otherwise is the NEXT mutation starting -- so a
                          notice raised to explain a confirm outlived the confirm it explained, and
                          went on describing a button no longer on the page. Same on the twin
                          Cancel below (rule 72). */}
                      <button
                        onClick={() => {
                          setConfirmReplace(false);
                          setKeyError(null);
                        }}
                      >
                        Cancel
                      </button>
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
                  {/* Replacing swaps one working key for another, so it never closes this
                      lane. Remove does: afterwards nothing gets in on the header at all.
                      Same two-step confirm the Replace control uses. */}
                  {confirmRemove ? (
                    <>
                      <button
                        className="danger"
                        disabled={removeKey.isPending}
                        onClick={() => {
                          afterKeyRemove.arriving();
                          removeKey.mutate();
                        }}
                      >
                        Confirm remove
                      </button>
                      <button onClick={() => setConfirmRemove(false)}>Cancel</button>
                    </>
                  ) : (
                    <button
                      className="ghost"
                      title="Anything using this key stops working immediately"
                      onClick={() => setConfirmRemove(true)}
                    >
                      Remove…
                    </button>
                  )}
                </>
              ) : confirmReplace ? (
                /* Reached only when the re-read in `requestGenerate` could not answer, so this
                   panel cannot prove there is no key to destroy. Same two-step shape as Replace
                   above, because it is the same act with a worse-known target; the notice under
                   the group says why it is being asked.
                   "Only" is true because the flag is reset whenever `api_key_set` changes (the
                   effect beside the mutations) and again the moment Remove succeeds. Without
                   those it was false in both directions, and this branch opened with no notice
                   after a Remove -- a danger confirm over a key the panel had just proved gone. */
                <>
                  <button
                    className="danger"
                    disabled={generate.isPending}
                    onClick={() => generate.mutate()}
                  >
                    Confirm generate
                  </button>
                  <button
                    onClick={() => {
                      setConfirmReplace(false);
                      setKeyError(null);
                    }}
                  >
                    Cancel
                  </button>
                </>
              ) : (
                <button
                  className="primary"
                  ref={afterKeyRemove.ref as RefObject<HTMLButtonElement>}
                  disabled={generate.isPending || requestGenerate.isPending}
                  onClick={() => requestGenerate.mutate()}
                >
                  {generate.isPending
                    ? "Generating…"
                    : requestGenerate.isPending
                      ? "Checking…"
                      : "Generate API key"}
                </button>
              )}
            </div>
          </div>
          {/* A link, not a box, so it releases the control track (`.set-row-plain`). */}
          <div className="set-row set-row-plain">
            <span className="set-label">API reference</span>
            {/* Says "as you", not "with your key", because the page preselects your SESSION:
                35 of the 47 writes do not offer the key at all, and the button reaches them
                all, arming included. Naming the key here would size the blast radius by the
                fence two rows up, which is far tighter than what this button spends (rule
                144). The key clause above is generated and guarded; this one is hand-written
                and its guard is test_the_reference_page_sends_the_csrf_header_it_names, which
                names this file. */}
            <p className="help">
              Every endpoint, documented from the running app. The try-it-out button sends real
              requests as you, so it can change settings and start work, not just read. Only visible
              while signed in.
            </p>
            <div className="set-control">
              <a className="btn-link" href="/api/docs" target="_blank" rel="noreferrer">
                Open the API reference <span aria-hidden="true">↗</span>
              </a>
            </div>
          </div>
        </div>
        {keyError && <Notice tone="error">{keyError}</Notice>}
      </div>

      <div className="set-group">
        <h3>Reverse proxy</h3>
        <div className="set-rows">
          {/* A Switch, not a box, so it releases the control track (`.set-row-plain`). The row
              below it holds the addresses box and keeps the track. */}
          <div className="set-row set-row-plain">
            <span className="set-label">Behind a reverse proxy</span>
            <p className="help">
              Turn this on if Nginx, Traefik, Caddy or similar sits in front of Reaper. Reaper will
              then trust the proxy to say which address each visitor really came from, which keeps
              sign-in rate limits accurate per visitor instead of lumping everyone together. It is
              also how Reaper learns that visitors arrive over HTTPS, so it can mark the sign-in
              cookie HTTPS-only.
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
            </div>
          </div>
        </div>
        <p className="group-hint muted">
          Off by default, and forwarded headers from anywhere else are always ignored: a stranger
          can't fake their address to dodge the login lockout.
        </p>
      </div>

      {/* Present only when the server says it runs as the Mac or Windows app; the container,
          the snap, and a source run report null and no group renders. Each Switch saves on
          the spot (the reverse-proxy Switch's shape) and the values render from the query
          data the save's response refreshed, so there is nothing here for the save bar. */}
      {data.desktop && (
        <div className="set-group">
          <h3>Desktop app</h3>
          <p className="group-blurb">These settings apply the next time Reaper opens.</p>
          <div className="set-rows">
            {data.desktop.platform === "macos" && (
              <div className="set-row set-row-plain">
                <span className="set-label">Show the Dock icon</span>
                <p className="help">
                  Reaper lives in the menu bar. Turn this on to show a Dock icon too.
                </p>
                <div className="set-control">
                  <Switch
                    checked={data.desktop.dock_icon}
                    disabled={save.isPending}
                    ariaLabel="Show the Dock icon"
                    onChange={(enabled) => save.mutate({ dock_icon: enabled })}
                  />
                </div>
              </div>
            )}
            <div className="set-row set-row-plain">
              <span className="set-label">
                {data.desktop.platform === "macos" ? "Menu bar icon" : "Tray icon"}
              </span>
              <p className="help">
                {data.desktop.platform === "macos"
                  ? "Open and quit Reaper from the menu bar. With this off, Reaper runs " +
                    "with nothing on screen, and quitting takes Activity Monitor."
                  : "Open and quit Reaper from the tray, next to the clock. With this off, " +
                    "Reaper runs with nothing on screen, and quitting takes Task Manager."}
              </p>
              <div className="set-control">
                <Switch
                  checked={data.desktop.tray}
                  disabled={save.isPending}
                  ariaLabel={data.desktop.platform === "macos" ? "Menu bar icon" : "Tray icon"}
                  onChange={(enabled) => save.mutate({ tray: enabled })}
                />
              </div>
            </div>
          </div>
        </div>
      )}

      {/* Only when there is no bar to put it in. A control that saves on the spot fails with
          nothing unsaved, so its refusal has nowhere else to go; a refused BAR save renders
          inside the bar instead, beside the fields it just refused to write. */}
      {save.error && pending.length === 0 && <Notice tone="error">{save.error.message}</Notice>}

      {/* The one save affordance on this panel (rule 43), the same bar the policy editor uses:
          it names what is unsaved, saves all of it in one press, and offers Discard. Rendered
          only while there is something to save, and sticky at the foot of the screen, so the
          field being typed in never moves. */}
      {pending.length > 0 && (
        <div className="savebar">
          <span className="savebar-what">
            Unsaved changes: <strong>{pending.map((p) => p.label).join(", ")}</strong>
            {accentBlocks && (
              <span className="savebar-blocked">Enter a hex code like #25c3ff to save.</span>
            )}
          </span>
          <button className="ghost" disabled={save.isPending} onClick={discardDrafts}>
            Discard
          </button>
          <button
            className="primary"
            disabled={save.isPending || accentBlocks}
            onClick={() => {
              bar.leaving();
              save.mutate(Object.assign({}, ...pending.map((p) => p.patch)));
            }}
          >
            {save.isPending ? "Saving…" : "Save changes"}
          </button>
          {/* Inside the bar, not below the panel (rule 42, and the same slot
              `PolicyEditor`'s bar uses): the route refuses the whole body before writing any
              of it, so this sentence is the only thing standing between the operator and the
              belief that all six fields went in. The bar is sticky, so a notice outside it
              renders at the document foot -- off screen for anyone editing the top group,
              which is where five of these six fields are. */}
          {save.error && <Notice tone="error">{save.error.message}</Notice>}
        </div>
      )}
    </div>
  );
}

// --- Services --------------------------------------------------------------

function ServiceCard({
  instance,
  onEdit,
  onRemoving,
}: {
  instance: Instance;
  onEdit: () => void;
  /** Called as the remove is sent, so the section above can catch focus when this card goes.
   *  Lives up there rather than here because the successor is not inside this card -- the card
   *  IS what unmounts (#173). */
  onRemoving?: () => void;
}) {
  const queryClient = useQueryClient();
  // The result and the address it was computed for, the third of this badge's siblings to keep the
  // pairing (rule 72; `ServiceModal` and the Discord row are the others). Nothing cleared this, and
  // editing a service through the modal invalidates `instances`, so the card re-rendered with a new
  // address while the local result went on vouching for the old one (rule 85, #178).
  //
  // The address and the certificate setting are what this card can see change. A key ROTATED at the
  // same address is not visible here -- `has_key` stays true -- so it is included for the false-to-
  // true case only. The rotation is answered on the server instead: `update_instance` clears
  // `last_ok_at` / `last_error` / `detected_version` whenever the address, the key or the
  // certificate setting changes, so the fallbacks below cannot outlive what they were computed
  // against either, and this card drops to "Not tested yet" (#264, rule 85).
  const [test, setTest] = useState<{ result: InstanceTest; of: string } | null>(null);
  const testedWith = () => `${instance.base_url} ${instance.verify_tls} ${instance.has_key}`;
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
    // Announced, like the modal's own test and the webhook's below -- three siblings of one
    // result that reached nobody by ear (#192, rule 72). `testSentence` is the one wording.
    onSuccess: (r) => {
      setTest({ result: r, of: testedWith() });
      announce(testSentence(r));
      invalidate();
    },
  });
  const remove = useMutation({
    mutationFn: () => api.deleteInstance(instance.id),
    // Adding a service speaks and removing one did not, which is the asymmetry #192 was about:
    // the whole card vanishes, and an absence cannot be perceived by ear.
    onSuccess: () => {
      announce(`${instance.name} removed.`);
      invalidate();
    },
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
          {test && test.of === testedWith() ? (
            <TestBadge result={test.result} />
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
          <Notice tone="error" inline>
            {remove.error
              ? `This service wasn't removed: ${remove.error.message}`
              : `The test didn't run: ${testSaved.error?.message}`}
          </Notice>
        )}
      </div>
      {/* Each button carries the instance it acts on. A tester with two Sonarr, two Radarr and a
          Tautulli renders this card five times, and by their visible text alone the buttons are
          five identical "Test"/"Edit"/"Remove" triplets with nothing saying which service each
          one would remove. The name is on screen at `.instance-id` above, but nothing bound it
          to the controls. */}
      <div className="service-card-foot">
        {confirmingRemove ? (
          <>
            <button
              type="button"
              className="danger"
              title="Only forgets it in Reaper. Nothing is changed in the service itself."
              aria-label={`Confirm remove ${instance.name}`}
              onClick={() => {
                setConfirmingRemove(false);
                onRemoving?.();
                remove.mutate();
              }}
            >
              Confirm remove
            </button>
            <button
              type="button"
              // The visible word first. A name that drops it entirely leaves a voice-control
              // operator saying "click Cancel" at a button that answers to "Keep", with the
              // red Confirm remove as the only other control in reach.
              aria-label={`Cancel, keep ${instance.name}`}
              onClick={() => setConfirmingRemove(false)}
            >
              Cancel
            </button>
          </>
        ) : (
          <>
            <button
              type="button"
              disabled={testSaved.isPending}
              // Carries the state, because the name REPLACES the visible text rather than
              // extending it: a fixed name freezes the button at "Test" while its label flips
              // to "Testing…", and nothing else here announces that the press did anything.
              // Visible word first, so "click Test" still reaches it by voice.
              aria-label={
                testSaved.isPending ? `Testing…, ${instance.name}` : `Test, ${instance.name}`
              }
              onClick={() => testSaved.mutate()}
            >
              {testSaved.isPending ? "Testing…" : "Test"}
            </button>
            <button type="button" aria-label={`Edit ${instance.name}`} onClick={onEdit}>
              Edit
            </button>
            <button
              type="button"
              className="danger"
              aria-label={`Remove ${instance.name}`}
              onClick={() => setConfirmingRemove(true)}
            >
              Remove
            </button>
          </>
        )}
      </div>
    </article>
  );
}

/** One kind's cards and its Add button.
 *
 *  Its own component only so it can hold a hook per kind: a `useSuccessorFocus()` inside the
 *  `KINDS.map` below would be a hook in a loop. What it buys is where focus goes when a card
 *  removes itself -- the card IS the thing that unmounts, so the successor cannot live inside it
 *  (#173). The Add button is the target rather than a neighbouring card: the cards' own focusable
 *  content is a Test/Edit/Remove triplet, and landing on another service's Test button reads as
 *  the wrong service being acted on, where Add is the one thing left to do in this section. It is
 *  also the only candidate that is always there -- removing a singleton kind's one card empties
 *  the grid, and `canAdd` flips the Add button ON in the same refetch, which the hook waits for. */
function ServiceSection({
  kind,
  rows,
  onOpen,
}: {
  kind: (typeof KINDS)[number];
  rows: Instance[];
  onOpen: (instance: Instance | null) => void;
}) {
  const afterRemove = useSuccessorFocus();
  // A singleton kind (Tautulli) shows no "Add" once one exists: it mirrors one Plex, and
  // Reaper connects to one Plex, so a second has no working setup.
  const canAdd = !kind.singleton || rows.length === 0;
  return (
    <section className="service-section">
      <h3>{kind.label}</h3>
      <p className="service-hint">{kind.hint}</p>
      <div className="service-grid">
        {rows.map((i) => (
          <ServiceCard
            key={i.id}
            instance={i}
            onEdit={() => onOpen(i)}
            onRemoving={afterRemove.arriving}
          />
        ))}
        {canAdd && (
          <button
            type="button"
            className="service-add"
            ref={afterRemove.ref as RefObject<HTMLButtonElement>}
            onClick={() => onOpen(null)}
          >
            <span aria-hidden="true">+</span> Add a {kind.label}
          </button>
        )}
      </div>
    </section>
  );
}

export function ServicesPanel() {
  const { data, isPending, error } = useQuery({ queryKey: ["instances"], queryFn: api.instances });
  const [modal, setModal] = useState<{ kind: InstanceKind; instance: Instance | null } | null>(
    null,
  );
  // The modal decides when it may be dismissed; it mirrors that whole answer here so Back
  // refuses exactly what the scrim, Escape and the ✕ refuse, the same arrangement the schedule
  // editor uses (B-19). It carried only the SAVE half once, and the moment the modal grew a
  // second reason to stay open -- a folder map read but never made -- Back walked through the
  // new guard while every other dismissal honored it (rule 80).
  const blockCloseRef = useRef(false);
  // Back closes the service editor instead of leaving Reaper -- unless the modal says otherwise.
  useBackGuard(
    modal !== null,
    () => setModal(null),
    () => !blockCloseRef.current,
  );

  return (
    <div className="panel panel-wide">
      <h2>Services</h2>
      {/* "It only ever reads" was false about Radarr and Sonarr, which are how a reap removes
          anything: the executor unmonitors, deletes files and adds exclusions through them.
          Bounded to the scan, which is what the claim was reaching for. Its twin is the
          wizard's Connect step (rule 72). */}
      <p className="blurb">
        The apps Reaper reads from. Scanning only reads. Nothing here can delete a file.
      </p>
      {/* The one Settings panel #140 did not reach. A raw exception string over the full service
          list broke rule 21 on its own, and said the read had failed above the connections it
          had read (#190): saving or removing an instance invalidates ["instances"], so an
          ordinary edit reaches it. */}
      {error && !data && <Notice tone="error">Couldn't load your connections.</Notice>}
      {error && data && <StaleReadNotice what="your connections" />}
      {isPending && <p className="muted">Loading…</p>}
      {data &&
        KINDS.map((k) => (
          <ServiceSection
            key={k.value}
            kind={k}
            rows={data.filter((i) => i.kind === k.value)}
            onOpen={(instance) => setModal({ kind: instance?.kind ?? k.value, instance })}
          />
        ))}
      {modal && (
        <ServiceModal
          key={modal.instance ? modal.instance.id : `add-${modal.kind}`}
          kind={modal.kind}
          instance={modal.instance}
          onClose={() => setModal(null)}
          blockCloseRef={blockCloseRef}
        />
      )}
    </div>
  );
}

// --- Backup ------------------------------------------------------------------

function BackupPanel({
  /** Called whenever the restore card is holding a staged backup, so the section rail can hold a
   *  switch that would drop it. Pass a STABLE function: it is an effect dependency. */
  onDirtyChange,
}: {
  onDirtyChange?: ((dirty: boolean) => void) | undefined;
} = {}) {
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
      {/* Backup became a draft-holding panel with #135, so it takes the same two-branch read the
          other three carry: the never-loaded sentence only where nothing ever landed, and the
          shared stale line beside a card that is still on screen. Saying "Couldn't load this page"
          over a staged file and a typed password sends the operator to reload, and a reload does
          not run the unmount cleanup -- so following this panel's own advice was the one exit that
          orphaned the archive. */}
      {isError && !data && (
        <Notice tone="error">Couldn't load this page. Reload to try again.</Notice>
      )}
      {isError && data && <StaleReadNotice />}
      {data && (
        <>
          <section className="rules-card">
            <h3>Download a backup</h3>
            <p className="help">
              One file with everything Reaper decided and everything you set up, plus the keys that
              unlock your saved credentials. The rebuildable cache is left out to keep the file
              small.
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
            {error && <Notice tone="error">The download didn't start: {error}</Notice>}
            {!data.key_in_backup && (
              // `standing`: where the key is set is a property of the deployment, read from
              // `["backup-info"]`, so this is on the panel from its first paint. The download
              // failure directly above answers a press and stays an alert.
              <Notice tone="warn" standing>
                Your encryption key is set through the environment, so it is not inside this backup.
                Keep that key with the file, or a restore cannot read your saved credentials.
              </Notice>
            )}
            {/* `standing`: this sits under the download button whenever the panel is open. It
                is not a reaction to anything, so an alert would cut the reader off mid-heading
                on every visit to Backup, with text that never changed. */}
            <Notice tone="warn" standing>
              This file can unlock your Plex and Sonarr/Radarr credentials. Keep it as safe as a
              password.
            </Notice>
          </section>

          {/* Rule 146's second claim, for this panel: the card is what holds the staged backup and
              it renders only inside this `data` branch, so the report and the surface arrive and
              leave together. This panel has no early return to re-read -- a failed refetch adds a
              line above without taking the card away, and React Query keeps `data` on the last
              good row, so there is no state where the card is gone and the report survives it. */}
          <RestoreCard armed={data.restore_armed} onDirtyChange={onDirtyChange} />
        </>
      )}
    </div>
  );
}

// --- About -------------------------------------------------------------------

export function AboutPanel() {
  const { data, isPending, isError } = useQuery({ queryKey: ["about"], queryFn: api.about });
  const update = useUpdateStatus();
  const [changesOpen, setChangesOpen] = useState(false);

  return (
    <div className="panel">
      <h2>About</h2>
      <p className="blurb">What's running, and where its data lives.</p>
      {/* standing: which channel this build is on is a fact about the install, true on
          first paint and unchanged for the process's whole life -- page furniture, not a
          reaction to anything pressed. */}
      {update.data?.channel === "dev" && (
        <Notice tone="warn" standing>
          You are running a <code>dev</code> build of Reaper. It changes daily and can break; use a
          release unless you are helping test.
        </Notice>
      )}
      {isPending && <p className="muted">Loading…</p>}
      {/* Two cases, not one. React Query keeps the last good row through a failed refetch and
          raises isError beside it, so an undivided `isError` printed "couldn't load this page"
          directly above the fully drawn page (rule 17/36). The trigger is a remount past
          `staleTime` -- leaving About and coming back 30 seconds later while the server is
          unreachable. Not window focus, which `main.tsx` turns off app-wide and only `useSafety`
          asks back, and not an invalidation: nothing in the app invalidates `["about"]`. */}
      {isError && !data && (
        <Notice tone="error">Couldn't load this page. Reload to try again.</Notice>
      )}
      {isError && data && <StaleReadNotice what="these details" />}
      {data && (
        <div className="set-rows">
          <dl className="about-kv">
            <dt>Version</dt>
            <dd>
              Reaper {data.version}
              {update.data?.update_available && (
                <span className="update-pill">
                  {update.data.channel === "dev" ? "Newer dev build" : "Update available"}
                </span>
              )}
            </dd>
            <dt>Update</dt>
            <dd>
              <UpdateCell status={update} onSeeChanges={() => setChangesOpen(true)} />
            </dd>
            <dt>License</dt>
            <dd>{data.license}</dd>
            <dt>Data folder</dt>
            <dd>
              <code>{data.data_dir}</code>
            </dd>
            <dt>Reaper's own data</dt>
            <dd>{bytes(data.reaper_db_bytes)}, decisions, audit trail, credentials</dd>
            <dt>Rebuildable cache</dt>
            <dd>{bytes(data.cache_db_bytes)}, watch history, ratings, lists</dd>
          </dl>
        </div>
      )}
      {changesOpen && update.data && (
        <ChangesModal
          changes={update.data.changes}
          url={update.data.url}
          onClose={() => setChangesOpen(false)}
        />
      )}
    </div>
  );
}

/** The Update row's sentence, one branch per state the check can be in. Pending and a
 *  failed read are spelled out (rule 17/36), and both no-answer shapes -- the HTTP
 *  call failing with nothing in hand, and the server answering "unknown" -- read the
 *  same, because to the operator they are the same fact: no answer today, and nothing
 *  they must do.
 *
 *  A failed REFETCH is deliberately not a branch: React Query keeps the last good
 *  answer and raises `isError` beside it, and the pill, the chip light, and the dev
 *  banner all render that retained answer -- so this row must too, or the pill says
 *  "Update available" directly above a row claiming the check failed (the exact
 *  stale-read split the `about` query above documents). */
function UpdateCell({
  status,
  onSeeChanges,
}: {
  status: ReturnType<typeof useUpdateStatus>;
  onSeeChanges: () => void;
}) {
  const { data, isPending } = status;
  // The two no-answer shapes read the same (see above), so the sentence is written once:
  // one operator claim in two places is two chances to drift (rule 144). "Later" is the
  // scheduled check (Settings, Jobs), which is what makes the promise real -- before it
  // existed nothing retried on a server nobody opened (#464).
  const noAnswer = (
    <span className="muted">Couldn't check for updates. Reaper will try again later.</span>
  );
  if (isPending) return <span className="muted">Checking for updates…</span>;
  if (!data) return noAnswer;
  if (!data.enabled)
    return (
      <span className="muted">
        Update checks are off, so Reaper never asks GitHub for versions. Remove REAPER_UPDATE_CHECK
        from launcher.conf in Reaper's data folder, or from your environment, to turn them back on.
      </span>
    );
  if (data.update_available === null) return noAnswer;
  if (!data.update_available)
    return (
      <span>
        {data.channel === "dev"
          ? "This build matches the dev branch."
          : "You are on the newest release."}
      </span>
    );
  if (data.channel === "dev")
    return (
      <>
        The dev branch has moved since this build.{" "}
        {data.url && (
          <a href={data.url} target="_blank" rel="noreferrer">
            See what changed
          </a>
        )}
        <br />
        <span className="muted">Dev builds change often. Releases are the steadier channel.</span>
      </>
    );
  return (
    <>
      Reaper {data.latest} is out.{" "}
      <button type="button" className="link-btn" onClick={onSeeChanges}>
        See what changed
      </button>
      <br />
      {/* Points at the schedule rather than naming one: the operator can change the cron
          or turn the job off, so a sentence saying "daily" is wrong the moment they do
          (rule 86). This used to say "Reaper checks a few times a day" while nothing
          checked on its own at all (#464, rule 25). */}
      <span className="muted">
        Reaper checks on a schedule you can change in Jobs, and never sends anything about your
        library.
      </span>
    </>
  );
}

/** The GitHub changelog for every release the operator has not taken, newest first, in
 *  the one modal shell. The markdown is rendered sanitized -- react-markdown emits no
 *  raw HTML, images are dropped so a note cannot phone home just for being read, and
 *  headings are demoted under the dialog's own so the outline stays honest. Every
 *  link inside leaves for GitHub in a new tab. */
function ChangesModal({
  changes,
  url,
  onClose,
}: {
  changes: ReleaseChange[];
  url: string | null;
  onClose: () => void;
}) {
  return (
    <ModalShell title="What changed" onClose={onClose} className="modal-changes">
      <div className="changes-body">
        {changes.length === 0 && (
          <p className="muted">
            No release notes to show.{" "}
            {url ? (
              <a href={url} target="_blank" rel="noreferrer">
                The release page on GitHub
              </a>
            ) : (
              "The releases page on GitHub"
            )}{" "}
            has the full story.
          </p>
        )}
        {changes.map((c) => (
          <section key={c.version} className="changes-release">
            <h3>Reaper {c.version}</h3>
            {c.notes ? (
              <div className="changes-notes">
                <Markdown
                  // Images out: a rendered <img> fetches from wherever the note says,
                  // beside copy promising nothing leaves the box just for reading.
                  disallowedElements={["img"]}
                  components={{
                    a: ({ node: _node, ...props }) => (
                      <a {...props} target="_blank" rel="noreferrer" />
                    ),
                    // GitHub's generated notes open at h2; undemoted that outranks the
                    // per-release h3 and sits level with the dialog's own name.
                    h1: ({ node: _node, ...props }) => <h4 {...props} />,
                    h2: ({ node: _node, ...props }) => <h4 {...props} />,
                    h3: ({ node: _node, ...props }) => <h4 {...props} />,
                    h4: ({ node: _node, ...props }) => <h4 {...props} />,
                    h5: ({ node: _node, ...props }) => <h4 {...props} />,
                    h6: ({ node: _node, ...props }) => <h4 {...props} />,
                  }}
                >
                  {c.notes}
                </Markdown>
              </div>
            ) : (
              <p className="muted">This release shipped without notes.</p>
            )}
            {c.url && (
              <p className="changes-link">
                <a href={c.url} target="_blank" rel="noreferrer">
                  View on GitHub
                </a>
              </p>
            )}
          </section>
        ))}
      </div>
    </ModalShell>
  );
}

// --- Jobs ------------------------------------------------------------------

const SCAN_ID = "scheduled_scan";

/** The one upkeep job whose result a DIFFERENT query already renders: the About row, the
 *  version pill and the account chip's light all read `["update"]`. Named here so the row can
 *  refresh that query when the job finishes. The job LIST is still the server's (rule 66);
 *  this is a behavior hook on one id, like `SCAN_ID` above. */
const UPDATE_CHECK_ID = "check_for_updates";

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
    desc: "Checks what changed since the last scan and re-scores it against your policy. A quick pass, not a full re-read.",
    modalDesc: "Reaper can scan on its own to keep the queue fresh.",
  },
  refresh_ratings: {
    title: "Refresh IMDb ratings",
    desc: "Downloads the latest IMDb ratings so scores use current numbers.",
    offWarning:
      "With this off, ratings won't refresh on a schedule. Reaper still refreshes them once at startup if they're over two weeks old.",
  },
  refresh_curated_lists: {
    // The job id is a stored schedule key and predates the registry, so it keeps its old
    // spelling; what it refreshes is every list on Settings -> Lists, whatever its source
    // (scheduler.refresh_curated_lists).
    title: "Refresh your lists",
    desc: "Re-checks every list on Settings, Lists, so a tag or a collection you edited starts protecting without waiting for a scan.",
    offWarning:
      "This only affects the standalone daily refresh. Every scan already re-checks your lists, and you can check one on Settings, Lists.",
  },
  full_history_sweep: {
    title: "Full watch-history update",
    desc: "Re-reads your whole watch history, not just new plays, so imported or backdated views still count and a wiped history is caught.",
    offWarning:
      "With this off, Reaper stops re-reading your full history. Imported or backdated plays won't be counted, and a wiped history won't be caught.",
  },
  check_for_updates: {
    title: "Check for updates",
    desc: "Asks GitHub whether a newer Reaper is available.",
    offWarning: "With this off, Reaper only checks when you open it.",
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
  // forever after the schedule query errored (U-6). It only costs the schedule when there is no
  // last good row to fall back on, though: React Query keeps the previous jobs and raises the
  // failure beside them, so an undivided `failed` blanked this line while the sibling JobRows
  // went on printing next-run times off that same held row, under a panel notice saying the rows
  // are kept but stale. The panel's 1.5s self-poll reaches that state with nobody touching
  // anything (rule 72: the same split the panel above takes).
  if (failed && !job) return "Couldn't check the schedule.";
  if (!job) return "Automatic scan: checking…";
  if (job.cron === null) return "Automatic scan is off. It runs when you ask.";
  return `Automatic scan: ${describeCron(job.cron)}, next ${whenText(job.next_run_at)}`;
}

function maintenanceScheduleText(job: ScheduledJob): string {
  if (job.cron === null) return "Off. Run it by hand.";
  return `${describeCron(job.cron)}, next ${whenText(job.next_run_at)}`;
}

/** The one schedule editor, for the scan and every upkeep job. Presets plus "off" plus a
 *  cron line of your own; turning an upkeep job off carries a plain warning of what stops. */
function ScheduleModal({
  job,
  onClose,
  savePendingRef,
}: {
  job: ScheduledJob;
  onClose: () => void;
  // Set by JobsPanel so its Back guard can read the same canClose the scrim/Escape/✕ use (B-11).
  savePendingRef?: RefObject<boolean>;
}) {
  const queryClient = useQueryClient();
  // The effective server time zone every timed job runs on, so the help names the real zone
  // instead of guessing "UTC in Docker" (U-1, rule 86). Shares GeneralPanel's cache.
  const zone = useQuery({ queryKey: ["general-settings"], queryFn: api.general }).data?.timezone;
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
      // The modal closing was the entire success signal, the same shape `ServiceModal`'s save
      // was fixed for -- and it takes the focused button with it.
      announce("Schedule saved.");
      void queryClient.invalidateQueries({ queryKey: ["schedule"] });
      onClose();
    },
    onError: (e: Error) => setError(e.message),
  });

  // Mirror the save's pending state up to JobsPanel's Back guard, and clear it on unmount so a
  // stale true never lingers after the modal closes (B-11).
  useEffect(() => {
    if (savePendingRef) savePendingRef.current = save.isPending;
    return () => {
      if (savePendingRef) savePendingRef.current = false;
    };
  }, [save.isPending, savePendingRef]);

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
          {/* The clock times above run on the server's configured time zone, not this browser's.
              Name the real zone so "2 AM" is not read as local time, and the operator is not left
              to guess (U-1, rule 86). Falls back to the generic phrasing only while it loads. */}
          <span className="help">
            {zone
              ? `Times use your server time zone: ${zone}. Change it in Settings, General.`
              : "Times use your server time zone. Change it in Settings, General."}
          </span>
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

        {turningOff && meta.offWarning && <Notice tone="warn">{meta.offWarning}</Notice>}
        {error && <Notice tone="error">{error}</Notice>}

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
      // Optimistically mark the job running so the spinner shows at once and the finish is
      // seen as a running->done transition (the flash) even for a job that completes inside
      // one poll interval. The real state, and the fresh last-run fields, land on the next
      // poll: the schedule query's own refetchInterval reacts to this optimistic flag right
      // away (nothing here needs to force an earlier refetch), so there is no fixed delay to
      // race against a scheduler that is slow to submit the job.
      queryClient.setQueryData<Schedule>(["schedule"], (prev) =>
        prev
          ? { ...prev, jobs: prev.jobs.map((j) => (j.id === job.id ? { ...j, running: true } : j)) }
          : prev,
      );
    },
  });
  const running = job.running || run.isPending;
  // The flash keys on the server's own running flag (which the mutation seeds optimistically),
  // never on `run.isPending` -- that would fire a stale flash the instant the POST returns,
  // before the job has even run. Compared with `!== null`, not truthiness: an empty (but
  // present) result must still flash, unlike a job that has simply never run.
  const flash = useJobFlash(
    job.running,
    job.last_result !== null ? { ok: job.last_ok !== false, text: job.last_result } : null,
  );

  // A finished update check has replaced the answer `["update"]` holds, and that query is
  // half an hour stale-free (updateStatus.ts) with nothing else to invalidate it -- so the
  // row would flash "Reaper 2026.9.2 is out" while the version pill, the About row and the
  // chip light all went on asserting the answer it just replaced (rule 79). Keyed on the same
  // running -> done edge the flash watches, so it fires when the ANSWER changed rather than
  // when the button was pressed.
  const wasRunning = useRef(false);
  useEffect(() => {
    if (wasRunning.current && !job.running && job.id === UPDATE_CHECK_ID) {
      void queryClient.invalidateQueries({ queryKey: ["update"] });
    }
    wasRunning.current = job.running;
  }, [job.running, job.id, queryClient]);

  return (
    <div className="jobrow">
      <div className="jobrow-main">
        <div className="jobrow-title">{meta.title}</div>
        <div className="jobrow-desc">{meta.desc}</div>
        <JobStatus
          running={running}
          runningLabel="Running now…"
          lastRunAt={job.last_run_at}
          lastOk={job.last_ok}
          lastResult={job.last_result}
          flash={flash}
        />
        <div className="jobrow-sched">{maintenanceScheduleText(job)}</div>
        {run.error && (
          <Notice tone="error" inline>
            The job didn't start: {run.error.message}
          </Notice>
        )}
      </div>
      {/* The Jobs page stacks this row once per server-returned job, above the scan row and the
          Leaving Soon row, so "Edit" and "Run now" appear several times over with the job's name
          sitting in `.jobrow-title` where no control referenced it. */}
      <div className="jobrow-actions">
        <span className="slot-edit">
          <button className="ghost" aria-label={`Edit ${meta.title}`} onClick={onEdit}>
            Edit
          </button>
        </span>
        <span className="slot-act">
          <button
            className="primary"
            // Same shape as the connection test above: the state is in the name, and the
            // visible words lead so voice control still reaches it. "Run now" is not a
            // contiguous part of "Run Trash sweep now", which is what the fixed name was.
            aria-label={running ? `Running…, ${meta.title}` : `Run now, ${meta.title}`}
            onClick={() => run.mutate()}
            disabled={running}
          >
            {running ? "Running…" : "Run now"}
          </button>
        </span>
      </div>
    </div>
  );
}

/** The Leaving Soon shelf update, moved here from Plex settings. Its on/off toggle still
 *  lives on the Plex tab, so this row links there; when off, it grays out and can't run. */
function LeavingSoonRow({
  onGoToPlex,
  plan,
}: {
  onGoToPlex: () => void;
  /** The Jobs panel's stale-read decision. This row draws its own line only while it is the
   *  only read that failed; when the panel's read failed too, the panel says it once, above
   *  these rows (#198). */
  plan: StaleReadPlan;
}) {
  const queryClient = useQueryClient();
  const ls = useQuery({ queryKey: ["leaving-soon-settings"], queryFn: api.leavingSoonSettings });
  const runSync = useMutation({
    mutationFn: api.syncLeavingSoon,
    onSuccess: () => void queryClient.invalidateQueries({ queryKey: ["leaving-soon-settings"] }),
  });
  // The manual-run confirmation for this row: the sync is a synchronous mutation, so its
  // result is read straight off the mutation when it settles (unlike the polled upkeep jobs).
  // Called before the early returns below, so the hook order never changes.
  const syncResult = runSync.data
    ? {
        ok: runSync.data.problems.length === 0,
        // A real per-library problem always wins the message, even in preview: "preview
        // only" is a benign caveat, but it must never mask an actual failure that happened
        // in the same pass.
        text:
          runSync.data.problems.length > 0
            ? "Some shelves didn't update"
            : !runSync.data.applied
              ? "Preview only, nothing written"
              : `${count(runSync.data.added_count)} added, ${count(runSync.data.cleared_count)} cleared`,
      }
    : runSync.error
      ? { ok: false, text: "It didn't update" }
      : null;
  const flash = useJobFlash(runSync.isPending, syncResult);

  // One declaration behind both the row heading and the button's spoken name, so they cannot
  // drift apart (rule 144). The name has to carry the visible words "Update now" first, and
  // `title` already opens with the verb, so pasting the two together says "Update" twice.
  const shelf = "Leaving Soon shelf";
  const title = `Update ${shelf}`;
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
  // Two states, not one (rule 17/36, and rule 72: the same split JobsPanel takes below). React
  // Query keeps the last good row through a failed refetch and raises `isError` beside it, so the
  // undivided test here threw that row away -- and the trigger was this row's OWN success path,
  // since a finished "Update now" invalidates this query. One blinked refetch after a shelf
  // update that WORKED reported the shelf status as unknown and took the "N added, M cleared"
  // confirmation down with it, before it had ever painted. The never-loaded sentence stays for
  // the read that really never landed, the only case it is true in.
  if (!ls.data) {
    return (
      <div className="jobrow">
        <div className="jobrow-main">
          <div className="jobrow-title">{title}</div>
          <div className="jobrow-desc">{desc}</div>
          <Notice tone="error" inline>
            Couldn't load the shelf status. Reload to try again.
          </Notice>
        </div>
      </div>
    );
  }

  const { enabled, last } = ls.data;
  // The row is still the best answer there is, so it renders -- and says it could not be
  // confirmed, above everything in the row the failed read could have changed.
  const stale = <StaleReadSlot plan={plan} slot="the shelf status" inline />;

  if (!enabled) {
    return (
      <div className="jobrow dimmed">
        <div className="jobrow-main">
          <div className="jobrow-title">{title}</div>
          <div className="jobrow-desc">{desc}</div>
          {stale}
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
        {stale}
        <JobStatus
          running={running}
          runningLabel="Updating…"
          lastRunAt={last?.at ?? null}
          lastOk={last ? last.ok : null}
          lastResult={last?.result ?? null}
          flash={flash}
        />
        {last && (
          <div className="jobrow-meta">
            <strong>{count(last.movies)}</strong> movie{last.movies === 1 ? "" : "s"} and{" "}
            <strong>{count(last.seasons)}</strong> season{last.seasons === 1 ? "" : "s"} on the
            shelves
          </div>
        )}
        <div className="jobrow-sched">Runs after every scan</div>
        <div className="jobrow-link">
          <button className="link" onClick={onGoToPlex}>
            Manage in Plex → Leaving Soon
          </button>
        </div>
        {runSync.error && (
          <Notice tone="error" inline>
            The shelves didn't update: {runSync.error.message}
          </Notice>
        )}
      </div>
      <div className="jobrow-actions">
        <span className="slot-edit" />
        <span className="slot-act">
          <button
            className="primary"
            aria-label={running ? `Updating…, ${shelf}` : `Update now, ${shelf}`}
            onClick={() => runSync.mutate()}
            disabled={running}
          >
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
  // The modal's save lives inside ScheduleModal; it mirrors its pending state here so the Back
  // guard can refuse a close mid-save exactly as the scrim/Escape/✕ do (canClose={!save.isPending}
  // below). Without this, Back would tear the modal down while the save is in flight, dropping
  // the error it would have shown (B-11, rule 80).
  const savePendingRef = useRef(false);
  // Back closes the schedule editor instead of leaving Reaper -- unless a save is in flight.
  useBackGuard(
    editing !== null,
    () => setEditing(null),
    () => !savePendingRef.current,
  );

  // The shelf row owns this read and renders inside this panel, so the panel has to know whether
  // it failed to decide whether both lines collapse into one (#198). A second `useQuery` on the
  // same key rather than a signal threaded up out of the row: React Query hands both observers
  // the one cache entry, so there is no second request and no state to keep in step -- and the
  // row's own early returns cannot leave a lifted flag asserting something its surface no longer
  // shows, which is the trap rule 146 is about.
  const shelf = useQuery({
    queryKey: ["leaving-soon-settings"],
    queryFn: api.leavingSoonSettings,
  });
  const stale = collapseStaleReads("these jobs", [
    { what: "these jobs", stale: schedule.isError && !!schedule.data },
    { what: "the shelf status", stale: shelf.isError && !!shelf.data },
  ]);

  const jobsById = new Map<string, ScheduledJob>((schedule.data?.jobs ?? []).map((j) => [j.id, j]));
  const scanJob = jobsById.get(SCAN_ID);

  return (
    <div className="panel">
      <h2>Jobs</h2>
      <p className="blurb">
        Everything Reaper runs on a timer lives here, and you can run any of it now without waiting.
        None of these can delete a thing. A scan just refreshes the review queue; the rest is
        upkeep.
      </p>

      {schedule.isPending && <p className="muted">Loading the upkeep jobs…</p>}
      {/* The rows below render from the last good row either way (`schedule.data?.jobs ?? []`),
          so a failed refetch already keeps them on screen -- only the sentence about them was
          wrong, and it read worst here: every row carries a next-run time and a running flag,
          and this query polls itself every 1.5s while anything runs, so it reaches the failed
          state with the operator doing nothing at all. The never-loaded line stays for the read
          that really never landed, which is the only case it is true in.

          ABOVE the rows, because the line says what's BELOW may be out of date and `.panel` is
          plain block flow, so DOM order is reading order: sat after `.set-rows` it pointed at the
          schedule editor and nothing else. Every other call site puts it over its content. */}
      {schedule.isError && !schedule.data && (
        <Notice tone="error">Couldn't load the upkeep jobs. Reload to try again.</Notice>
      )}
      {/* Both reads on this panel say the same thing when they fail together, so they say it
          once, here, above the rows (#198). Unlike Plex's four these are independent polls
          that can fail apart, which is why the rule counts the lines that would draw rather
          than grouping by invalidation: either one alone still speaks in its own words. */}
      <StaleReadSlot plan={stale} slot="these jobs" />

      <div className="set-rows">
        <ScanRow
          snapshot={snapshot}
          scanJob={scanJob}
          title={jobMeta(SCAN_ID).title}
          desc={jobMeta(SCAN_ID).desc}
          scheduleText={scanScheduleText(scanJob, schedule.isError)}
          onEdit={() => scanJob && setEditing(scanJob)}
          canEdit={!!scanJob}
        />
        <LeavingSoonRow onGoToPlex={onGoToPlex} plan={stale} />
        {/* Render the upkeep jobs from the server's own list (scan aside; it has its own
            row), in its order, so a job added server-side appears here without a frontend
            edit. jobMeta falls back to the raw id for a job with no copy yet. */}
        {(schedule.data?.jobs ?? [])
          .filter((job) => job.id !== SCAN_ID)
          .map((job) => (
            <JobRow key={job.id} job={job} onEdit={() => setEditing(job)} />
          ))}
      </div>

      {editing && (
        <ScheduleModal
          job={editing}
          onClose={() => setEditing(null)}
          savePendingRef={savePendingRef}
        />
      )}
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

/** The webhook box's format complaint, named once for both ends (rule 67). */
const WEBHOOK_ERROR_ID = "discord-webhook-error";

/** Exported so `DiscordModal` checks the format the same way this panel does. A second copy
 *  of these host and path rules would be one validator written twice, and the copy that drifts
 *  is the one that starts accepting a URL the backend then refuses (rule 18, rule 144). */
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

/** The Discord webhook is the only channel that actually warns your users before a title
 *  is deleted -- the Plex "Leaving Soon" label only reaches people who pinned the library. It
 *  is a write-only secret: the URL is sent once, encrypted on arrival, and never comes back,
 *  so the field is always blank and we report only *whether* a webhook is connected. Same
 *  pattern as an instance API key. */
// Exported for TestBadgeFreshness.test.tsx, which drives this row's badge against an edited URL.
export function NotificationsPanel({
  /** Called whenever the webhook box gains or loses a draft, so the section rail can hold a
   *  switch that would discard one. Pass a STABLE function: it is an effect dependency. */
  onDirtyChange,
}: {
  onDirtyChange?: ((dirty: boolean) => void) | undefined;
} = {}) {
  const queryClient = useQueryClient();
  const { data, isPending, isError } = useQuery({
    queryKey: ["notifications"],
    queryFn: api.notifications,
  });
  const [url, setUrl] = useState("");
  // The result and the URL it was computed for, the same pairing `ServiceModal` keeps for its own
  // badge (rule 72). Save, Remove and the Test button each cleared this, but EDITING the box did
  // not, so a passed test then a pasted-over URL left "Passed" beside a webhook nobody had sent
  // to (rule 85, #178).
  const [test, setTest] = useState<{ result: InstanceTest; of: string } | null>(null);
  const [error, setError] = useState<string | null>(null);
  /** What the test was sent. A blank box tests the webhook already STORED, so blank is a real
   *  value here and not an absence -- which is why this is the trimmed string rather than a
   *  truthiness check. */
  const testedWith = () => url.trim();

  const invalidate = () => void queryClient.invalidateQueries({ queryKey: ["notifications"] });

  const save = useMutation({
    mutationFn: () => api.setWebhook(url.trim()),
    onSuccess: () => {
      setUrl("");
      setTest(null);
      setError(null);
      // Success here is the box emptying and a line above it flipping, both silent. The test
      // button between these two mutations already speaks (#192); these are its siblings.
      announce("Discord webhook saved.");
      invalidate();
    },
    onError: (e: Error) => setError(e.message),
  });
  const testWebhook = useMutation({
    // Test the URL typed in the box (the one about to be saved) if there is one; otherwise
    // test the already-stored webhook, so a saved channel can be verified without re-pasting.
    mutationFn: () => api.testWebhook(url.trim() ? url.trim() : null),
    onSuccess: (r) => {
      setTest({ result: r, of: testedWith() });
      announce(testSentence(r));
    },
    onError: (e: Error) => setError(e.message),
  });
  // Remove is the rule 72 twin of the API key's, and the harder half of the pair: removing the
  // webhook disables BOTH of the pressed button's siblings in the same breath -- Save wants a
  // typed URL and `setUrl("")` has just cleared it, Send test wants a stored one and that is what
  // went -- so there is no successor control at all, only the box the operator would refill.
  // Which makes it the honest target: it is the one thing left to do here (#173).
  const afterWebhookRemove = useSuccessorFocus();
  const remove = useMutation({
    mutationFn: () => api.clearWebhook(),
    onSuccess: () => {
      setUrl("");
      setTest(null);
      setError(null);
      announce("Discord webhook removed. Leaving-soon warnings won't be sent.");
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

  // What this panel would LOSE, reported up to `Settings` so leaving the section can stop and ask
  // first. A pasted webhook is the costliest draft in Settings to drop: it is a secret, it is
  // never shown again once stored, and re-typing it means going back to Discord for it.
  //
  // Rule 146 asks two things of this signal, and here they are the same fact. There is something
  // to lose exactly when the box holds text, and the box is reachable in EVERY state this panel
  // renders: it has no early return, and the loading and failed-check branches above swap only
  // the one status line over it, never the box, its help, or its Save. `typed` rather than
  // `validNew`, because a half-pasted URL that Save refuses is still a draft that leaving throws
  // away -- reporting only the saveable form would drop the malformed one silently.
  useEffect(() => {
    onDirtyChange?.(typed);
  }, [typed, onDirtyChange]);
  useEffect(() => () => onDirtyChange?.(false), [onDirtyChange]);

  return (
    <div className="panel">
      <h2>Notifications</h2>
      <p className="blurb">
        A Discord webhook is how Reaper warns your users before anything is deleted: while a title
        is in its grace period it posts a "leaving soon" heads-up here, so someone can watch it or
        spare it in time. It's optional, but it's the one warning that reaches people who don't
        watch the Plex "Leaving Soon" shelf.
      </p>

      {/* Whether the warning channel exists is only worth stating once it has been read:
          an unread answer must not claim that nobody is being warned.

          Three branches, not two (rule 17/36, rule 72). React Query keeps the last good answer
          through a failed refetch -- which `save` and `remove` both trigger, since each
          invalidates this key on success -- and raises the failure beside it. This panel has no
          early return, so the "couldn't check" sentence printed directly above three controls
          derived from that very answer, each of them acting as though it HAD been checked: the
          "leave blank to keep the current webhook" placeholder, an enabled Remove, and a Send
          test that fires at the stored webhook. The same sentence also rendered over the opposite
          form when the FIRST read failed, so the two states could not be told apart.

          Neither branch says to reload (#195). The panel has no early return, so the webhook box
          below is on screen in EVERY branch, and what is typed into it is a secret Reaper stores
          encrypted and never shows again -- a reload costs the operator a value they have to go
          back to Discord for, and nothing anywhere in `frontend/src` asks first. That is the same
          harm #153 took off the shared line; this sentence is hand-written, so it kept it. */}
      {isPending ? (
        <p className="muted">Checking whether Discord is connected…</p>
      ) : isError && !data ? (
        <Notice tone="error">Couldn't check whether Discord is connected.</Notice>
      ) : (
        <>
          {isError && <StaleReadNotice what="whether Discord is connected" />}
          {connected ? (
            <p className="muted">
              {/* The sentence says the state in words either way, so the tick would only
                  interrupt it with a stray character -- the same call `:1467`'s `.dot` ✓ makes
                  a few hundred lines above, and the one #177 made for the `.gate-mark` pair. */}
              <span aria-hidden="true">✓</span> Discord connected. Leaving-soon warnings post to
              your channel.
            </p>
          ) : (
            <p className="muted">No Discord webhook set, so leaving-soon warnings won't be sent.</p>
          )}
        </>
      )}

      <div className="add-grid">
        <label className="field-sm wide">
          <span className="field-label">Discord webhook URL</span>
          <input
            type="password"
            ref={afterWebhookRemove.ref as RefObject<HTMLInputElement>}
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
            // The complaint renders after the whole button row, so in DOM order it is three
            // controls away from the box it is about (#174).
            aria-invalid={badFormat ? true : undefined}
            aria-describedby={badFormat ? WEBHOOK_ERROR_ID : undefined}
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
              afterWebhookRemove.arriving();
              remove.mutate();
            }}
          >
            {remove.isPending ? "Removing…" : "Remove"}
          </button>
        )}
        <TestBadge result={test && test.of === testedWith() ? test.result : null} />
      </div>
      {badFormat && (
        <Notice tone="error" id={WEBHOOK_ERROR_ID}>
          That doesn't look like a Discord webhook URL. Paste the full
          https://discord.com/api/webhooks/… URL from the channel's integration settings.
        </Notice>
      )}
      {error && <Notice tone="error">{error}</Notice>}
    </div>
  );
}

// --- Safety ----------------------------------------------------------------

// The same floor the server applies (MIN_PASSWORD_LENGTH in
// reaper/services/admin_password.py), so the placeholder, the live message, and the server
// rule all state one number. Exported because the first-run wizard sets this same password
// on its own step and states the same floor: a second literal 12 there would be one rule
// written twice, and the copy that drifts is the one nobody edits (rule 67, rule 144).
export const MIN_ADMIN_PASSWORD = 12;

/** The password form's one error region, named once for both ends of the association (rule 67).
 *  Which BOX claims it varies: the region carries whichever complaint is live, and only the box
 *  that complaint is about points at it -- see `errorOwner`, which derives that from the same
 *  chain the message comes off. Two independent predicates cannot decide it, because they
 *  overlap and the region does not. */
const PASSWORD_ERROR_ID = "admin-password-error";

function AdminPasswordForm({
  needed,
  /** Called whenever this form gains or loses typed text, so the section rail can hold a switch
   *  that would discard it. Pass a STABLE function: it is an effect dependency. */
  onDirtyChange,
}: {
  needed: boolean;
  onDirtyChange?: ((dirty: boolean) => void) | undefined;
}) {
  const queryClient = useQueryClient();
  const [current, setCurrent] = useState("");
  const [pw, setPw] = useState("");
  const [confirm, setConfirm] = useState("");
  const [msg, setMsg] = useState<string | null>(null);

  // Did a recovery code open this session? That is the one case the server takes a new
  // password without the current one, because a forgotten password is what recovery mode is
  // FOR -- demanding it here left the operator signed in and still locked out (#433).
  //
  // Rule 17/36 wants the unknown and failed states answered explicitly, and they are: `?? false`
  // is the strict answer, not a placeholder. A session this form cannot read is treated exactly
  // like an ordinary one, so the current-password box stays live and required. It reads the same
  // ["me"] entry `App` resolved before anything under it could mount, so in practice there is no
  // pending state to pass through. And the server re-reads the session's own mark on the request
  // either way: this can only relax what the FORM asks for, never what the API allows.
  const { data: me } = useQuery({ queryKey: ["me"], queryFn: api.me, retry: false });
  const viaRecovery = me?.via_recovery ?? false;
  const skipCurrent = needed || viaRecovery;

  const save = useMutation({
    // Only the new password is sent: the confirm field exists so a typo can't lock the
    // operator out of the key that arms deletion, and it never leaves the browser.
    mutationFn: () => api.setAdminPassword(pw, skipCurrent ? undefined : current),
    onSuccess: () => {
      setCurrent("");
      setPw("");
      setConfirm("");
      setMsg("Password saved.");
      // The visible half is a `.muted` span beside the button, which announces nothing. The
      // failure half of this form already speaks (`Notice`, `role="alert"`), so success was
      // the only outcome of changing the password that arms deletion an operator could not
      // hear -- the one asymmetry the comment below is careful about, reached another way.
      announce("Password saved.");
      void queryClient.invalidateQueries({ queryKey: ["safety"] });
      // The server spends the recovery mark in the same transaction as the new hash, so this
      // session is an ordinary one from here on. Re-read it, or the current-password box would
      // stay grayed out over a session that no longer excuses it and the next change would fail
      // at the API instead of at the form.
      void queryClient.invalidateQueries({ queryKey: ["me"] });
    },
    onError: () => {
      // Deliberately NOT the mirror of onSuccess: nothing is written to `msg`, because a
      // failure renders from `save.error` as an error notice and this password is what
      // confirms turning deletion on -- "saved" and "wrong password" must never look alike.
      //
      // The re-read is the whole point. A second tab mounted before the mark was spent holds
      // `via_recovery: true` in a cache that nothing refetches (`main.tsx` sets
      // `refetchOnWindowFocus: false`), so its box stays parked and empty while the server
      // refuses every submit: a form with no way out but a reload. Re-reading here turns the
      // refusal into the state that explains it, with the current-password box live again.
      void queryClient.invalidateQueries({ queryKey: ["me"] });
    },
  });

  const tooShort = pw.length > 0 && pw.length < MIN_ADMIN_PASSWORD;
  const mismatch = confirm.length > 0 && confirm !== pw;
  const needCurrent = !skipCurrent && current.length === 0;
  const valid =
    pw.length >= MIN_ADMIN_PASSWORD && confirm.length > 0 && confirm === pw && !needCurrent;

  // The third live complaint, and the one that used to be missing: an empty current password
  // turns Save off with nothing on the page saying so, on the form that sets the key arming
  // deletion (#188). Gated on a new password having been typed, like the two above are gated on
  // their own box: on a pristine form nothing is wrong yet, and a complaint about a box the
  // operator has not reached reads as a failure rather than a next step.
  const askCurrent = needCurrent && pw.length > 0;

  // One red error region under the row. Live validation (too short, then mismatch, then the
  // missing current password) explains why Save is off while typing; a failed submit reuses the
  // same box. Validation wins over a stale submit error so the operator sees the thing they can
  // fix right now -- including a current password they have just cleared, which is why
  // `askCurrent` sits above `save.error` rather than below it.
  const errorNode: ReactNode = tooShort ? (
    <>
      Use at least {MIN_ADMIN_PASSWORD} characters. <b>{pw.length} so far.</b>
    </>
  ) : mismatch ? (
    "The passwords don't match."
  ) : askCurrent ? (
    "Enter the current password to save."
  ) : save.error ? (
    needed ? (
      `The password wasn't set: ${save.error.message}`
    ) : (
      `The password wasn't changed: ${save.error.message}`
    )
  ) : null;

  // Which BOX the live complaint belongs to, derived from the same chain that picks it rather
  // than from the predicates separately. `tooShort` and `mismatch` are independent and both
  // hold constantly -- any short password with a non-matching confirm -- while the region shows
  // only the first. Read off the two predicates, the confirm box then pointed at a region
  // holding "Use at least 12 characters", reading the box above it out as its own problem, and
  // "The passwords don't match." was not on the page at all to be reached (#174).
  const errorOwner: "new" | "confirm" | "current" | null = tooShort
    ? "new"
    : mismatch
      ? "confirm"
      : askCurrent
        ? "current"
        : null;

  // What this form would LOSE, reported up through `SecurityPanel` to `Settings` so leaving the
  // section can stop and ask first. Any of the three boxes counts: a password too short to save,
  // or one whose confirm does not match yet, is still text the operator typed and still gone on
  // unmount -- reporting only the saveable form (`valid`) would drop exactly the half-finished
  // ones silently.
  //
  // Rule 146 asks two things of this signal, and this component answers both trivially: it has no
  // early return, so every state it renders is one where all three boxes are on screen. What the
  // second claim does bind is the panel above, whose own early returns unmount this form -- see
  // `SecurityPanel`.
  const typed = current.length > 0 || pw.length > 0 || confirm.length > 0;
  useEffect(() => {
    onDirtyChange?.(typed);
  }, [typed, onDirtyChange]);
  useEffect(() => () => onDirtyChange?.(false), [onDirtyChange]);

  // Typing clears the "saved" note and any stale failure, so neither lingers over a form the
  // operator is now re-editing.
  const onEdit = (set: (v: string) => void) => (e: ChangeEvent<HTMLInputElement>) => {
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
            : viaRecovery
              ? "You can set a new one without the old password."
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
              one below. First-time setup has no current password, so neither is shown.

              A recovery session keeps the box on screen but parks it: disabled and dimmed,
              the way an option behind a switch reads (`.set-row.dim`, rule 18). Hiding it
              instead would leave an operator who has used this form before wondering which
              form they were looking at, and the one line under it is the answer to the
              question the empty box asks. */}
          {!needed && (
            <>
              <label className={viaRecovery ? "field-sm dim" : "field-sm"}>
                <span className="field-label">Current password</span>
                <input
                  type="password"
                  value={viaRecovery ? "" : current}
                  onChange={onEdit(setCurrent)}
                  disabled={viaRecovery}
                  autoComplete="current-password"
                  maxLength={128}
                  aria-describedby={errorOwner === "current" ? PASSWORD_ERROR_ID : undefined}
                />
                {viaRecovery && (
                  <span className="help">Not needed. A recovery code signed you in.</span>
                )}
              </label>
              <hr className="pw-sep" />
            </>
          )}
          <label className="field-sm">
            <span className="field-label">New password</span>
            {/* The placeholder states the length up front; the label names the field. The
                cap is the server's own, so a long pasted passphrase is stopped in the box
                rather than coming back as a validator's sentence. */}
            <input
              type="password"
              value={pw}
              onChange={onEdit(setPw)}
              placeholder="at least 12 characters"
              autoComplete="new-password"
              maxLength={128}
              // One region carries three different complaints, so each box describes itself
              // with the live one only while it is the one about IT -- `errorOwner`, off the
              // same chain that picks the message. `aria-invalid` stays on this box's own
              // predicate: a short password is short whichever complaint is showing.
              aria-invalid={tooShort ? true : undefined}
              aria-describedby={errorOwner === "new" ? PASSWORD_ERROR_ID : undefined}
            />
          </label>
          <label className="field-sm">
            <span className="field-label">Confirm new password</span>
            <input
              type="password"
              value={confirm}
              onChange={onEdit(setConfirm)}
              autoComplete="new-password"
              maxLength={128}
              aria-invalid={mismatch ? true : undefined}
              aria-describedby={errorOwner === "confirm" ? PASSWORD_ERROR_ID : undefined}
            />
          </label>
          <div className="add-actions">
            <button type="submit" className="primary" disabled={!valid || save.isPending}>
              Save
            </button>
            {msg && <span className="muted">{msg}</span>}
          </div>
        </form>
        {/* One box, two kinds of message, so the flag is read off the same chain that picks the
            text. `errorOwner` is non-null for exactly the three live branches and null for the
            fourth, because `valid` requires all three to be clear before Save can be pressed at
            all -- so a submit failure always mounts this fresh, which is the insertion
            `role="alert"` is announced on.

            `standing` on the live ones: they explain why Save is off WHILE THE OPERATOR TYPES,
            and the first of them renders `{pw.length} so far`, so its text changed inside a live
            region on every keystroke and re-announced the whole string each time -- around
            eleven interruptions on the way to a valid password, on the form that sets the key
            arming deletion. Nothing is lost by not interrupting: all three inputs point here
            through `aria-describedby`, so the complaint is read as the description of the box
            the operator is standing in. This is the case `Notice.tsx` names outright. */}
        {errorNode && (
          <Notice tone="error" id={PASSWORD_ERROR_ID} standing={errorOwner !== null}>
            {errorNode}
          </Notice>
        )}
      </div>
    </div>
  );
}

export function SecurityPanel({
  /** Called whenever the password form gains or loses typed text, so the section rail can hold a
   *  switch that would discard it. Pass a STABLE function: it is an effect dependency. */
  onDirtyChange,
}: {
  onDirtyChange?: ((dirty: boolean) => void) | undefined;
} = {}) {
  const { data, isLoading, isError } = useSafety();

  if (isLoading) {
    return (
      <div className="panel">
        <h2>Security</h2>
        <p className="muted">Loading…</p>
      </div>
    );
  }
  // Rule 146: the draft this panel reports upward lives in `AdminPasswordForm` below, so an early
  // return here does not merely hide the form -- it unmounts it, and three typed password boxes go
  // with it. That is not a rare state: `useSafety` refetches every 15 seconds and on window focus,
  // so ONE failed poll while someone is choosing a password used to replace the form mid-typing
  // with a "couldn't load" paragraph and say nothing about what it took. React Query keeps the
  // last good row through a failed refetch, so the form stays on it and this branch is now only
  // for a load that never landed one -- the same shape `GeneralPanel` and `PlexPanel` already use
  // (rule 72), with the same one line saying the read failed.
  if (!data) {
    return (
      <div className="panel">
        <h2>Security</h2>
        <Notice tone="error">Couldn't load these settings. Reload to try again.</Notice>
      </div>
    );
  }

  return (
    <div className="panel">
      <h2>Security</h2>
      <p className="blurb">
        The admin password. It confirms turning deletion on (in <strong>Policy → Deletion</strong>),
        and it's also how you sign in without Plex.
      </p>

      {isError && <StaleReadNotice />}

      <AdminPasswordForm needed={!data.has_password} onDirtyChange={onDirtyChange} />
    </div>
  );
}

// --- shell -----------------------------------------------------------------

export function Settings({
  initialPanel,
  onGoToPolicy,
}: {
  initialPanel?: Panel | undefined;
  /** Jump to the Policy screen's keep-rules section, for the Lists rows' policy-use links.
   *  Optional the way `SafetyBanner`'s jump is: tests mount Settings without a navigator. */
  onGoToPolicy?: (() => void) | undefined;
}) {
  const [panel, setPanel] = useState<Panel>(initialPanel ?? "general");
  // General's save bar can hold six unsaved fields at once, and switching section unmounts the
  // panel holding them. So the switch waits for a yes, the same two-step confirm the policy
  // editor's Movies/TV switch uses and in the same place: directly under the control that was
  // clicked, so that control does not move under the pointer.
  //
  // Five panels report: General's save bar; Plex's web address and manual connection rows; the
  // Discord webhook URL, a secret the operator has to go back to Discord to re-copy; Security's
  // three admin-password boxes; and Backup's staged restore, which is the only one whose loss also
  // strands something on the SERVER. The guard first landed on General alone and then on three, so
  // the rest went on unmounting silently while the app had already trained the operator to expect
  // to be asked (rule 72). Each reports through its own `onDirtyChange`; the five are `useState`
  // setters and so are stable, which that prop requires.
  //
  // The other four are spelled out below rather than left out, because `dirtyPanels` is a total
  // `Record<Panel, …>`: a panel missing from it does not compile, where an absent key used to read
  // as "holds nothing" and switch straight through. That is rule 103's one-declaration branch, and
  // it replaces a comment claiming these five "are the whole population" -- a claim nothing checked
  // against the nine in `PANELS`, so the next section added would have been unguarded and silent
  // (#156). `npm run build` runs `tsc --noEmit` and is a CI gate, so the compiler is the guard.
  //
  // The last two took a hop the first three did not: their drafts live in CHILD components
  // (`AdminPasswordForm`, `RestoreCard`), so the signal is declared there and passed up through
  // the panel. That hop is what rule 146 is about -- a child that unmounts on its parent's early
  // return takes the draft with it, so `SecurityPanel`'s failed-read branch had to change too.
  const [generalDirty, setGeneralDirty] = useState(false);
  const [plexDirty, setPlexDirty] = useState(false);
  const [webhookDirty, setWebhookDirty] = useState(false);
  const [securityDirty, setSecurityDirty] = useState(false);
  const [backupDirty, setBackupDirty] = useState(false);
  const [pendingSwitch, setPendingSwitch] = useState<Panel | null>(null);
  // Bumped on every refused press so `SwitchConfirm` can move focus even when the press changed
  // no state at all -- pressing the same section twice sets `pendingSwitch` to the value it
  // already holds, which React treats as nothing happening (see SwitchConfirm.tsx).
  const [switchNonce, setSwitchNonce] = useState(0);

  // Every panel classified, in `PANELS` order. A `false` here is a claim that the section has
  // nothing to lose on the way out, so each one says why -- verified in the tree.
  const dirtyPanels: Record<Panel, boolean> = {
    general: generalDirty,
    // Its drafts live in `ServiceModal`, inside a `ModalShell`, whose scrim
    // covers the rail and whose `trapTab` keeps Tab inside, so the switch cannot be reached while
    // one is open. A draft added to the panel BEHIND the modal would need to report.
    services: false,
    plex: plexDirty,
    // Same shape as services: a list's drafts live in `ListModal`, inside a `ModalShell`, so
    // the rail cannot be reached while one is open. This said the panel was read-only and
    // "a list is still configured where it always was" -- both untrue as of the Lists screen,
    // which is now the one place a list IS defined, and the next author to add an inline edit
    // here would have read that and left this entry alone (rule 146).
    lists: false,
    // Same shape as services: the job editor (`ScheduleModal`) is a `ModalShell` too.
    jobs: false,
    notifications: webhookDirty,
    security: securityDirty,
    backup: backupDirty,
    // Holds view filters, and its one stored setting saves the moment it changes
    // (`LogsPanel`'s `setLevel`), so there is never an unsaved edit to lose. That file carries the
    // other half of this note: a draft added there is invisible from here.
    logs: false,
    // Read-only.
    about: false,
  };
  const leavingDirty = dirtyPanels[panel];

  // The notice exists only because there are edits to lose, so it goes when they do -- by
  // Discard, or by a Save that stores them. Keyed on the draft rather than on the Discard
  // handler so the save path is covered too, which is the bug `PolicyEditor` fixed in its own
  // copy of this: it kept warning about changes that no longer existed.
  useEffect(() => {
    if (!leavingDirty) setPendingSwitch(null);
  }, [leavingDirty]);

  const switchPanel = (next: Panel) => {
    if (next === panel) return;
    if (leavingDirty) {
      setPendingSwitch(next);
      setSwitchNonce((n) => n + 1);
      return;
    }
    setPendingSwitch(null);
    setPanel(next);
  };
  const pendingLabel = PANELS.find((p) => p.id === pendingSwitch)?.label ?? "";
  // The section being LEFT, so one string serves every panel that raises the shared sentence.
  const leavingLabel = PANELS.find((p) => p.id === panel)?.label ?? "";
  // Nine labels stop fitting one line well above this, but the app already has exactly one
  // definition of a narrow screen and a second would be worse than swapping a little early:
  // below this width the section rail is a bottom bar, so a compact settings header is the
  // same shape. Rendered as one or the other, never both hidden by CSS, so only the control
  // in use is in the accessibility tree. jsdom has no matchMedia, so a test sees the rail.
  const narrow = useMediaQuery(NARROW_SCREEN_QUERY);
  return (
    <div className="settings">
      {narrow ? (
        <nav className="settings-picker" aria-label="Settings sections">
          <select
            value={panel}
            aria-label="Settings section"
            onChange={(e) => switchPanel(e.target.value as Panel)}
          >
            {PANELS.map((p) => (
              <option key={p.id} value={p.id}>
                {p.label}
              </option>
            ))}
          </select>
        </nav>
      ) : (
        <nav className="settings-nav" aria-label="Settings sections">
          {PANELS.map((p) => (
            <button
              key={p.id}
              className={panel === p.id ? "settings-tab active" : "settings-tab"}
              // Reserve the bold (active) width so switching panels never shifts the rail.
              data-label={p.label}
              // The active panel is stated, not just colored, the same as the masthead.
              aria-current={panel === p.id ? "page" : undefined}
              onClick={() => switchPanel(p.id)}
            >
              {p.label}
            </button>
          ))}
        </nav>
      )}
      {/* Directly under the rail that was clicked, so the rail does not move: the same slot and
          the same two buttons the policy editor's own switch confirm uses (rule 18).
          On General the save bar below names WHICH fields are unsaved, so this does not repeat
          them. The other four have no bar and this line is all they get: an inline Save button is
          the only other cue, and on Notifications and Security the box is a password field showing
          dots. Naming the field here is what those actually want.
          Backup gets its own sentence because the shared one would be false there: what is waiting
          is an uploaded file, not a setting, and switching does not merely forget it -- the card
          cancels the staged upload on its way out. */}
      {pendingSwitch !== null && (
        <SwitchConfirm
          nonce={switchNonce}
          message={
            panel === "backup"
              ? `The backup file you chose isn't restored yet. Switching to ${pendingLabel} drops it.`
              : `You have unsaved ${leavingLabel} settings. Switching to ${pendingLabel} discards them.`
          }
          onDiscard={() => {
            setPendingSwitch(null);
            setPanel(pendingSwitch);
          }}
          onKeep={() => setPendingSwitch(null)}
        />
      )}
      <div className="settings-body">
        {panel === "general" && <GeneralPanel onDirtyChange={setGeneralDirty} />}
        {panel === "services" && <ServicesPanel />}
        {panel === "plex" && <PlexPanel onDirtyChange={setPlexDirty} />}
        {panel === "lists" && <ListsPanel onGoToPolicy={onGoToPolicy} />}
        {panel === "jobs" && <JobsPanel onGoToPlex={() => switchPanel("plex")} />}
        {panel === "notifications" && <NotificationsPanel onDirtyChange={setWebhookDirty} />}
        {panel === "security" && <SecurityPanel onDirtyChange={setSecurityDirty} />}
        {panel === "backup" && <BackupPanel onDirtyChange={setBackupDirty} />}
        {panel === "logs" && <LogsPanel />}
        {panel === "about" && <AboutPanel />}
      </div>
    </div>
  );
}
