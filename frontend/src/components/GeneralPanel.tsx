// SPDX-License-Identifier: AGPL-3.0-or-later
//
// Settings -> General: what Reaper calls itself and where it lives, how it looks, what the
// review queue opens on, the API key, and the reverse-proxy and desktop settings.
//
// The API key is write-only end to end -- it is sent once, encrypted on arrival, and never
// comes back, so the field for it is always blank and "leave it empty to keep the current one".
// One save bar covers the whole panel (rule 43), and it reports upward through `onDirtyChange`
// so the section rail can hold a switch that would discard a draft (rule 146).

import { useMutation, useQueryClient } from "@tanstack/react-query";
import { type CSSProperties, type RefObject, useEffect, useState } from "react";
import { accentInk, accentText, DEFAULT_ACCENT, isHexColor } from "../accent";
import { announce } from "../announce";
import { useSavebarFocus, useSuccessorFocus } from "../focus";
import { api, type ExpandSeasonsMode, type GeneralSettings } from "../api";
import { useGeneralSettings } from "../useGeneralSettings";
import { useMediaQuery } from "../useMediaQuery";
import { FixedQuantity } from "./QuantityInput";
import { Segmented } from "./Segmented";
import { StaleReadNotice } from "./StaleReadNotice";
import { Switch } from "./Switch";
import { Notice } from "./Notice";

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

type TextFieldName = "application_name" | "application_url" | "timezone";

/** One field of this panel whose whole draft is a string, compared one way and sent one way.
 *  `seed` reads the stored value, `clean` is the canonical form both the compare and the
 *  request use (rule 39), and `patch` is the request body naming the field. */
type TextField = {
  name: TextFieldName;
  /** What the save bar calls it while it is unsaved. */
  label: string;
  seed: (data: GeneralSettings) => string;
  clean: (draft: string) => string;
  patch: (value: string) => Parameters<typeof api.saveGeneral>[0];
};

/** The three fields a descriptor covers. Five echoes walk this instead of naming each field
 *  once apiece: the seed, the re-seed after a save, the dirty check, the save bar's entry, and
 *  Discard. The sixth the finding counted was the `useState`, which is one record now rather
 *  than one hook per field. The JSX below is deliberately not a sixth walker -- a row names its
 *  own field, its own label and its own control, and generating those is a different job from
 *  this one (rule 45: help binds to exactly one control). Adding a plain text setting is a row
 *  here plus a row on screen.
 *
 *  THREE of this panel's six drafts are deliberately not in it, and each is hand-written just
 *  below with its reason: the accent alone blocks the save, the default spare length is one
 *  stored number held as two pieces of state, and the proxy list is counted unsaved exactly
 *  where it is kept OUT of the bar (rule 146). Folding those in would need a per-field escape
 *  hatch each, which is most of what the descriptor is here to remove -- and the third hatch
 *  would re-derive the bar from the dirty set, which is the defect rule 146 exists for.
 *
 *  #90 was this shape: one `> 0` condition shared by three of these echoes, plus a fourth that
 *  did not handle the field at all. */
const TEXT_FIELDS: readonly TextField[] = [
  {
    name: "application_name",
    label: "Application name",
    seed: (data) => data.application_name,
    clean: (draft) => draft.trim(),
    patch: (value) => ({ application_name: value }),
  },
  {
    name: "application_url",
    label: "Application URL",
    // Null on the wire means "no URL"; the box shows that as empty, and empty saves back as
    // the same nothing.
    seed: (data) => data.application_url ?? "",
    clean: (draft) => draft.trim(),
    patch: (value) => ({ application_url: value }),
  },
  {
    name: "timezone",
    label: "Time zone",
    seed: (data) => data.timezone,
    // A <select> value, so there is no stray whitespace to fold away.
    clean: (draft) => draft,
    patch: (value) => ({ timezone: value }),
  },
];

const EMPTY_TEXT: Record<TextFieldName, string> = {
  application_name: "",
  application_url: "",
  timezone: "",
};

function seededText(data: GeneralSettings): Record<TextFieldName, string> {
  const next = { ...EMPTY_TEXT };
  for (const field of TEXT_FIELDS) next[field.name] = field.seed(data);
  return next;
}

export function GeneralPanel({
  /** Called whenever the save bar gains or loses a draft, so the section rail can hold a
   *  switch that would discard one. Pass a STABLE function: it is an effect dependency. */
  onDirtyChange,
}: {
  onDirtyChange?: ((dirty: boolean) => void) | undefined;
} = {}) {
  const queryClient = useQueryClient();
  const general = useGeneralSettings();
  // Save and Discard both unmount the bar holding the pressed button (#173), the twin of the
  // policy editor's (rule 72). Declared ABOVE every early return, which is rule 146's shape:
  // this panel returns a loading line and a failure notice before the form exists, and a hook
  // below either is a different hook order on those renders.
  const bar = useSavebarFocus();

  // One record over `TEXT_FIELDS`, not one `useState` per field: the echoes below walk the
  // descriptor, and a field it does not know about would still need its own state here.
  //
  // Every writer of this record uses the functional form, and that is load-bearing rather than
  // style. Three `useState`s had three queues, so a keystroke could not collide with anything;
  // one record has one, and a `setText({ ...text, ... })` built from the render's own value
  // replays over a still-pending update and throws it away. The two that can be pending are
  // the mount seed and the re-seed after a save, so what would be lost is a box snapping back
  // to a pre-save draft, or `seeded` going true over boxes that were never filled -- which is
  // the save bar naming fields nobody typed in, #139's shape, reported upward by rule 146.
  const [text, setText] = useState<Record<TextFieldName, string>>(EMPTY_TEXT);
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
    setText(seededText(general.data));
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
      setText((current) => {
        const next = { ...current };
        for (const field of TEXT_FIELDS) {
          if (field.name in sent) next[field.name] = field.seed(data);
        }
        return next;
      });
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

  const dirtyText = ready
    ? TEXT_FIELDS.filter((field) => field.clean(text[field.name]) !== field.seed(data))
    : [];
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
  for (const field of dirtyText) {
    pending.push({ label: field.label, patch: field.patch(field.clean(text[field.name])) });
  }
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
    setText(seededText(data));
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
                value={text.application_name}
                maxLength={60}
                onChange={(e) =>
                  setText((current) => ({ ...current, application_name: e.target.value }))
                }
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
                value={text.application_url}
                placeholder="https://reaper.example.com"
                onChange={(e) =>
                  setText((current) => ({ ...current, application_url: e.target.value }))
                }
                aria-label="Application URL"
              />
            </div>
          </div>
          <div className="set-row">
            <span className="set-label">Time zone</span>
            <p className="help">The server's time zone.</p>
            <div className="set-control">
              <select
                value={text.timezone}
                aria-label="Time zone"
                onChange={(e) => setText((current) => ({ ...current, timezone: e.target.value }))}
              >
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
