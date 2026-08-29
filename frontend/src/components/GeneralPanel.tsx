// SPDX-License-Identifier: AGPL-3.0-or-later
//
// Settings -> General: what Reaper calls itself and where it lives, how it looks, what the
// review queue opens on, the API key, and the reverse-proxy and desktop settings.
//
// The API key is write-only end to end. It is sent once, encrypted on arrival, and never
// comes back, so the field for it is always blank, and the help text says "leave it empty to
// keep the current one". One save bar covers the whole panel, and it reports upward through
// `onDirtyChange` so the section rail can hold a switch that would discard a draft.
//
// The copy lives in `locales/en/ui.json` under `general.*`. Two of the tables below
// (`accentPresets`, `textFields`) are read outside a component, so they take the catalog from
// the plain `i18next` import rather than the `useTranslation` hook. Each is a function, per
// `i18n-module-scope.test.ts`: a string resolved in a module body keeps whatever language was
// serving when the module first loaded.

import { useMutation, useQueryClient } from "@tanstack/react-query";
import { type CSSProperties, type RefObject, useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { accentInk, accentText, DEFAULT_ACCENT, isHexColor } from "../accent";
import { announce } from "../announce";
import { useSavebarFocus, useSuccessorFocus } from "../focus";
import i18next, { LANGUAGES, languageName, preferredLanguage, setLanguage } from "../i18n";
import { api, type ExpandSeasonsMode, type GeneralSettings } from "../api";
import { describeError } from "../errors";
import { useGeneralSettings } from "../useGeneralSettings";
import { useMediaQuery } from "../useMediaQuery";
import { FixedQuantity } from "./QuantityInput";
import { Segmented } from "./Segmented";
import { StaleReadNotice } from "./StaleReadNotice";
import { Switch } from "./Switch";
import { Notice } from "./Notice";
import { SetRow } from "./SetRow";

type ThemeChoice = "system" | "light" | "dark";

function readTheme(): ThemeChoice {
  try {
    const stored = localStorage.getItem("reaper-theme");
    return stored === "light" || stored === "dark" ? stored : "system";
  } catch {
    return "system";
  }
}

/** Apply and remember the theme. "system" removes the override so the device decides.
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
    // Storage can be unavailable (private windows). The page still themes for this load, but
    // the choice does not persist.
  }
}

// Quick-pick accents. The first is the built-in default, and the rest are a spread of hues
// that stay clear of the fixed red "remove" and green "keep" verdict colors. Any hex is
// allowed via the field, so this is just a shortcut.
// Each carries the color's name, because the swatch is a bare colored circle. Its only other
// name would be the hex, which a screen reader spells out one character at a time and which
// is not something an operator would recognize as a color name either.
const accentPresets = (): { value: string; name: string }[] => [
  { value: DEFAULT_ACCENT, name: i18next.t("general.accentPresets.reaperBlue") },
  { value: "#4f46e5", name: i18next.t("general.accentPresets.indigo") },
  { value: "#7c3aed", name: i18next.t("general.accentPresets.violet") },
  { value: "#0ea5e9", name: i18next.t("general.accentPresets.sky") },
  { value: "#14b8a6", name: i18next.t("general.accentPresets.teal") },
  { value: "#f59e0b", name: i18next.t("general.accentPresets.amber") },
  { value: "#ec4899", name: i18next.t("general.accentPresets.pink") },
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
// sensible length instead of an empty box. One declaration, because the seed and Discard have
// to agree. If Discard put back a different number than the box seeds, a discarded draft
// would reappear on screen.
const SPARE_DAYS_SEED = 30;

/** The hex field's refusal message, named once so the box's `aria-describedby` and the
 *  message's own `id` are the same string rather than two that can drift. A module constant,
 *  not a `useId`: this panel is a singleton, and the id is only useful while the message
 *  renders, which is exactly when the box points at it. */
const ACCENT_ERROR_ID = "accent-hex-error";

type TextFieldName = "application_name" | "application_url" | "timezone";

/** One field of this panel whose whole draft is a string, compared one way and sent one way.
 *  `seed` reads the stored value, `clean` is the canonical form both the compare and the
 *  request use, and `patch` is the request body naming the field. */
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
 *  Discard. The state itself is one record rather than one hook per field, which removes a
 *  sixth echo. The JSX below is deliberately not a sixth walker either. A row names its own
 *  field, its own label, and its own control, and generating those is a different job from
 *  this one, since help text has to bind to exactly one control. Adding a plain text setting
 *  is a row here plus a row on screen.
 *
 *  Three of this panel's six drafts are deliberately not in this list, and each is
 *  hand-written just below with its reason: the accent alone blocks the save, the default
 *  spare length is one stored number held as two pieces of state, and the proxy list counts
 *  as unsaved exactly where it is kept out of the bar. Folding those in would need a
 *  per-field escape hatch each, which is most of what this descriptor exists to remove, and
 *  one of those hatches would have to re-derive the bar from the dirty set instead of the
 *  other way around.
 *
 *  Repeating a per-field check by hand across several places risks one of them drifting from
 *  the rest, or a new place forgetting the field entirely. That is the failure this
 *  descriptor removes. */
const textFields = (): readonly TextField[] => [
  {
    name: "application_name",
    label: i18next.t("general.fields.applicationName.label"),
    seed: (data) => data.application_name,
    clean: (draft) => draft.trim(),
    patch: (value) => ({ application_name: value }),
  },
  {
    name: "application_url",
    label: i18next.t("general.fields.applicationUrl.label"),
    // Null on the wire means "no URL". The box shows that as empty, and empty saves back as
    // the same nothing.
    seed: (data) => data.application_url ?? "",
    clean: (draft) => draft.trim(),
    patch: (value) => ({ application_url: value }),
  },
  {
    name: "timezone",
    label: i18next.t("general.fields.timezone.label"),
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
  for (const field of textFields()) next[field.name] = field.seed(data);
  return next;
}

export function GeneralPanel({
  /** Called whenever the save bar gains or loses a draft, so the section rail can hold a
   *  switch that would discard one. Pass a STABLE function: it is an effect dependency. */
  onDirtyChange,
}: {
  onDirtyChange?: ((dirty: boolean) => void) | undefined;
} = {}) {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const general = useGeneralSettings();
  // Save and Discard both unmount the bar holding the pressed button, the same as the policy
  // editor's. Declared above every early return, since this panel returns a loading line and a
  // failure notice before the form exists, and a hook declared below either would give those
  // renders a different hook order.
  const bar = useSavebarFocus();

  // One record over `textFields`, not one `useState` per field: the echoes below walk the
  // descriptor, and a field it does not know about would still need its own state here.
  //
  // Every writer of this record uses the functional form, and that is load-bearing rather than
  // style. Three separate `useState`s would each have their own update queue, so a keystroke
  // could not collide with anything. One record has one queue, so a `setText({ ...text, ... })`
  // built from the render's own value would replay over a still-pending update and throw it
  // away. The two updates that can be pending are the mount seed and the re-seed after a save,
  // so the functional form is what stops a box from snapping back to a pre-save draft, or
  // `seeded` going true over boxes that were never filled, which would report a draft the
  // operator never typed.
  const [text, setText] = useState<Record<TextFieldName, string>>(EMPTY_TEXT);
  const [proxies, setProxies] = useState("");
  const [accent, setAccent] = useState(DEFAULT_ACCENT);
  // The default spare length, as a draft in two halves: which mode is chosen, and the box's
  // live number. They are held apart because Forever stores 0 and the typed number has to
  // survive a trip through it. `spareValue` below folds them back into the one stored field.
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
  // Removing the key unmounts the whole key-present block, taking the pressed Confirm with it.
  // The button is `disabled` while the write is in flight, so focus is already at `<body>`
  // before the unmount even happens. Focus lands on "Generate API key" instead, the one thing
  // left to do in this row rather than a nearby neighbor, and it only mounts once the refetch
  // says the key is gone. `useSuccessorFocus` exists to wait out that round trip.
  const afterKeyRemove = useSuccessorFocus();

  // Seed the editable fields from the server once per load, and re-seed after saves, which
  // return the canonical stored values.
  //
  // This uses state rather than a ref, because the render has to read it. An effect runs after
  // the commit, so the first pass where `general.data` exists would paint with every box still
  // on its initial value ("", the accent default, spare 0) while `data` already holds the
  // stored ones, and the dirty checks below would then name four fields nobody typed in.
  // `useEffect` runs after paint, so that frame would still reach the screen: the save bar
  // would appear on its own on every load of this panel, then clear itself a commit later. A
  // ref would fix nothing here, since mutating one does not re-render, and the value read
  // during that first pass would still be the stale one.
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
      // Re-seed from the canonical stored values, but only the fields this Save actually sent.
      // The save bar sends every dirty field at once, so reaching this handler no longer takes
      // one row's own Save. The two controls that still save on the spot do reach it that way:
      // a Switch or a select (the reverse-proxy toggle, the expand-seasons mode) writes
      // immediately, and re-seeding every field on its response would wipe whatever text was
      // half-typed elsewhere at the time, with nothing on screen to say why.
      //
      // Setting the query cache stays unconditional. It is the canonical stored state, and it
      // is what re-applies the accent app-wide so a save re-tints everything.
      //
      // Two shapes reach here, and neither is otherwise announced: the save bar, whose only
      // success signal is the bar unmounting under the button that had focus, and the two
      // controls that save on the spot, whose success signal is nothing at all.
      announce(t("general.notices.settingsSaved"));
      queryClient.setQueryData(["general-settings"], data);
      setText((current) => {
        const next = { ...current };
        for (const field of textFields()) {
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
  // error until its own next call. Rendering `reveal.error ?? generate.error ?? copy.error`
  // would leave a failure on screen beside a key that had since worked: failing Copy on a
  // plain-http LAN page, then pressing Show, would still show the copy failure as the red
  // notice. The three report through this one piece of state instead, cleared the moment any
  // of them starts, so the notice always describes the last thing the operator did.
  const [keyError, setKeyError] = useState<string | null>(null);
  const reveal = useMutation({
    mutationFn: api.revealApiKey,
    onMutate: () => setKeyError(null),
    // Show swaps a readonly box from dots to the live secret and flips its own button to
    // Hide. Revealing a credential on screen is worth saying out loud, since the operator may
    // be somewhere they would rather it stayed hidden. The key itself is never announced: it
    // is in the box, and a live region is the wrong place for a secret.
    onSuccess: (r) => {
      setRevealedKey(r.key);
      announce(t("general.notices.apiKeyShown"));
    },
    onError: (e) => setKeyError(describeError(e)),
  });
  const generate = useMutation({
    mutationFn: api.generateApiKey,
    onMutate: () => setKeyError(null),
    onSuccess: (r) => {
      setRevealedKey(r.key);
      setConfirmReplace(false);
      announce(t("general.notices.apiKeyGenerated"));
      void queryClient.invalidateQueries({ queryKey: ["general-settings"] });
    },
    onError: (e) => setKeyError(describeError(e)),
  });

  // Generating replaces whatever key the server holds, the moment it returns, with no undo.
  // That is why Replace two branches down is a two-step confirm reading "The old key stops
  // working immediately". The bare one-click Generate renders on `api_key_set === false`, and
  // that answer is cached: `["general-settings"]` has a 30-second staleTime,
  // `refetchOnWindowFocus` is off app-wide, and nothing else evicts it. A key made from another
  // tab, a phone, or by another admin would leave this panel offering a one-click revoke of a
  // live key, with none of the confirmation its own design says the action needs.
  //
  // So this proves the absence instead of assuming it: it re-reads first, and only a fresh "no
  // key" answer generates straight away. That keeps the first-run flow at one click, which
  // matters. The honest reading of a page parked for a minute is "I don't know yet", not "this
  // is dangerous", and putting a danger confirm in front of every setup would be its own false
  // claim. Neither other answer generates:
  //   - a key exists: the row has already re-rendered into its key-present layout, Replace and
  //     all, on the fresh data. Say so and stop.
  //   - the re-read failed: nothing is provable, so fall back to the confirm. This is the same
  //     class of problem as a destructive button whose gate is derived from a value that went
  //     stale.
  //
  // Only the un-armed press comes through here. The two armed buttons call `generate` directly,
  // the way `Confirm replace` beside them does, so there is no `if (confirmReplace)` arm at the
  // top of this function. The only handler that reaches this one renders on the branch where the
  // flag is false, so such an arm would be unreachable, and one that reads as though it routes
  // the confirmed press would wrongly suggest that path also re-proves absence.
  //
  // This is a mutation, not a bare async onClick, the same shape `copy` below uses for the same
  // reason: the re-read is a round trip, and `retry: 1` app-wide means a failing one costs two
  // requests and a backoff between them. Through all of that, `generate` itself has not started,
  // so `generate.isPending`, the button's only pending input, would stay false, and the button
  // would sit enabled under its idle label looking dead. A second press would then run a second
  // check, and if both cleared, two keys would be minted back to back: the second revokes the
  // first, and whichever response lands last is the one left in the box, which need not be the
  // key the server kept. The operator copies it and it returns 401.
  const requestGenerate = useMutation({
    onMutate: () => setKeyError(null),
    mutationFn: async () => {
      const fresh = await general.refetch();
      if (fresh.isError) {
        setConfirmReplace(true);
        setKeyError(t("general.apiKey.checkFailed"));
        return;
      }
      if (fresh.data?.api_key_set) {
        setKeyError(t("general.apiKey.alreadyExists"));
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
      throw new Error(t("general.apiKey.copyNeedsHttps"));
    }
    await navigator.clipboard.writeText(key);
    // The only other feedback is the button's own label reading "Copied" for two seconds, a
    // change to the name of the control the operator is standing on, which is not announced.
    announce(t("general.notices.apiKeyCopied"));
    setCopied(true);
    setTimeout(() => setCopied(false), 2000);
  };
  const copy = useMutation({
    mutationFn: copyKey,
    onMutate: () => setKeyError(null),
    onError: (e) => setKeyError(describeError(e)),
  });
  const removeKey = useMutation({
    mutationFn: api.removeApiKey,
    onMutate: () => setKeyError(null),
    onError: (e) => setKeyError(describeError(e)),
    onSuccess: () => {
      setRevealedKey(null);
      setConfirmRemove(false);
      // And the other confirm, which this row also renders. This clears it here as well as in
      // the effect below, because the effect waits on the refetch. For that round trip
      // `api_key_set` is still true, so a Replace armed before the operator changed their mind
      // to Remove would sit armed over a key that is already gone.
      setConfirmReplace(false);
      // The three neighboring mutations above all announce. This one's only other success
      // signal is the key block unmounting and taking the pressed Confirm with it, an absence
      // that cannot be heard.
      //
      // This names the consequence, not the wire mechanism. An announcement is delivered
      // through a live region and is heard alone, with nothing else on the page for context, so
      // it cannot rely on a word like "the header" being explained somewhere else on screen.
      // The wording matches the help paragraph's own phrase for what the key is for, so the two
      // read alike, and it follows the house pattern the Discord `remove` mutation sets in
      // `NotificationsPanel`: say what the operator loses, not what stops working on the wire.
      announce(t("general.notices.apiKeyRemoved"));
      void queryClient.invalidateQueries({ queryKey: ["general-settings"] });
    },
  });

  // A confirm belongs to the row that raised it. `api_key_set` decides which row renders, and
  // one flag arms a danger button on each of them, so it must not carry across the switch. If
  // it did, a Replace armed on the key-present row would still be armed when Remove took the
  // key away, and the no-key row would open on "Confirm generate" with no notice to explain
  // it. The other direction is worse: a key arriving from another tab could re-render the
  // key-present row with "Confirm replace" already armed, leaving a live key one press from a
  // confirm the operator never opened. This effect resets both flags on every change, so
  // whichever way the row changes, nothing destructive stays pressed.
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

  // This checks `seeded`, not just `data`. Between the commit that first has `data` and the
  // effect above that copies it into these boxes, every box still holds its initial value and
  // so would differ from the stored one. Comparing at that frame would report a draft the
  // operator never typed. Since the same one-frame report reaches `Settings` through
  // `onDirtyChange`, this is two separate claims rather than one cosmetic flash. `PlexPanel`
  // guards the same risk on its two mirrored fields with a different check: it re-seeds on
  // every change of the stored value where this seeds once, so it has to ask which value it
  // was seeded from rather than merely whether it has been seeded at all.
  const ready = !!data && seeded;

  const dirtyText = ready
    ? textFields().filter((field) => field.clean(text[field.name]) !== field.seed(data))
    : [];
  const accentValid = isHexColor(accent);
  const accentDirty = ready && accent.trim().toLowerCase() !== data.accent_color.toLowerCase();
  const proxyList = proxies
    .split(",")
    .map((p) => p.trim())
    .filter(Boolean);
  const proxiesDirty = ready && proxyList.join(", ") !== data.trusted_proxies.join(", ");
  // The two halves of the draft fold back into the one stored number only here, at compare
  // time, since Forever is 0 in that field. Pressing Forever therefore reads as a change to
  // the same field the box edits, and one Discard puts both back. Writing 0 directly on the
  // press instead would let the save bar, gated on the stored value, unmount its own Discard
  // as soon as the two matched, carrying the unsaved number into the next press without a
  // Save.
  const spareValue = spareForever ? 0 : spareDays;
  const spareDirty = ready && spareValue !== data.default_spare_days;

  // One save affordance for the whole panel. The bar names what is unsaved and sends all of it
  // in one request. The controls that take effect the moment they change are not drafts and do
  // not join it. Three of them call `save` themselves: the reverse-proxy `Switch`, the
  // expand-seasons `<select>`, and the language `<select>`. The theme `<select>` writes this
  // browser's own localStorage and never reaches the server, so it has no draft to hold either.
  // The language select reloads the page once its save lands (see `setLanguage`), which would
  // take this bar's contents with it, so it is disabled while the bar has any. That is why it
  // reads `pending` rather than being a self-contained control like the theme beside it. The
  // spare-length `Segmented` stages `default_spare_days` here instead of saving on the spot
  // (see `spareValue` above).
  const pending: { label: string; patch: Parameters<typeof api.saveGeneral>[0] }[] = [];
  for (const field of dirtyText) {
    pending.push({ label: field.label, patch: field.patch(field.clean(text[field.name])) });
  }
  if (accentDirty)
    pending.push({
      label: t("general.accent.label"),
      patch: { accent_color: accent.trim().toLowerCase() },
    });
  if (spareDirty)
    pending.push({
      label: t("general.spareLength.label"),
      patch: { default_spare_days: spareValue },
    });
  // Only while the switch is on. Turning it off disables the box, and a bar naming a field the
  // operator cannot reach to fix is worse than one that waits for them to turn it back on.
  if (proxiesDirty && data?.proxy_trust_enabled)
    pending.push({
      label: t("general.proxy.addressesLabel"),
      patch: { trusted_proxies: proxyList },
    });
  // A half-typed hex code would be stored as the app-wide accent, so the whole save waits on
  // it rather than silently dropping that one field from a bar that just named it.
  const accentBlocks = accentDirty && !accentValid;

  // What this panel would lose, reported up to `Settings` so that leaving the section can stop
  // and ask first. Nearly always that is the same as the bar, but not quite, so the two are
  // computed apart rather than one read off the other. A proxy list typed and then parked
  // behind its own switch is dropped from `pending` on purpose just above, because the bar must
  // not name a field the operator cannot reach to fix. The text is still sitting in the
  // disabled box, though, still unsaved and still gone on unmount, so reading the bar alone
  // would let exactly that field walk out silently on a panel that had just promised to ask.
  //
  // This reports two things at once: that there is something to lose, and that the operator can
  // still reach it. Both are read against every early return below. The report fires while this
  // renders "Loading…" (nothing is dirty yet, `data` is undefined), and it must not outlive the
  // form, which is why the failure branch below keeps the form whenever there is a row to
  // render.
  const hasDrafts = pending.length > 0 || (proxiesDirty && !data?.proxy_trust_enabled);
  useEffect(() => {
    onDirtyChange?.(hasDrafts);
  }, [hasDrafts, onDirtyChange]);
  useEffect(() => () => onDirtyChange?.(false), [onDirtyChange]);

  if (general.isPending) {
    return <p className="muted">{t("common.loading")}</p>;
  }
  // Only when there is nothing to render. A refetch that fails after a good load leaves `data`
  // in place (React Query keeps the last good row and raises `isError` beside it). Trading the
  // whole form for this paragraph there would take the save bar and its Discard away while the
  // drafts stayed in state, still reported unsaved to `Settings`, which would then ask to
  // discard edits the operator could no longer see, save, or put back. Pressing Generate API
  // key on a server that blinks is enough to trigger that refetch, since it invalidates this
  // very query. So a failed refetch keeps the form on the last good values. This branch is for
  // a load that never landed one at all.
  if (!data) {
    return <Notice tone="error">{t("common.loadError")}</Notice>;
  }

  // The current zone may not be in the browser's list (an older engine, or a server-only
  // zone). Keep it selectable so a save never silently drops it.
  const zoneOptions =
    data.timezone && !allTimeZones().includes(data.timezone)
      ? [data.timezone, ...allTimeZones()]
      : allTimeZones();

  // Computed here rather than inline in the JSX below, so the Switch's `ariaLabel` and the
  // SetRow's `label` read the same value. This is guarded on `data.desktop` even though both
  // uses are gated the same way, so the ICU `select` below is never asked to format an
  // undefined `platform` on the far more common run where there is no desktop build at all.
  const trayLabel = data.desktop
    ? t("general.desktop.trayLabel", { platform: data.desktop.platform })
    : "";
  const trayHelp = data.desktop
    ? t("general.desktop.trayHelp", { platform: data.desktop.platform })
    : "";

  const discardDrafts = () => {
    bar.leaving();
    setText(seededText(data));
    setAccent(data.accent_color);
    setProxies(data.trusted_proxies.join(", "));
    setSpareForever(data.default_spare_days === 0);
    // Both halves reset here, unlike the mount seed and the save response above, which leave
    // the number alone under a stored Forever so the last length is remembered. Discard is a
    // full undo, so it goes back to the stored length or to the same number the box seeds at.
    // Without this reset, the discarded figure would stay in the hidden box, and the next
    // press of Days would re-stage it.
    setSpareDays(data.default_spare_days > 0 ? data.default_spare_days : SPARE_DAYS_SEED);
  };

  return (
    <div className="panel">
      <h2 ref={bar.ref as RefObject<HTMLHeadingElement>} tabIndex={-1}>
        {t("general.heading")}
      </h2>
      <p className="muted">{t("general.subheading")}</p>

      {/* Same obligation as the twin in `PlexPanel`: the `!data` branch above keeps the form
          through a failed refetch so the drafts in it stay reachable, which leaves this line
          the only thing saying the values below may be stale. */}
      {general.isError && <StaleReadNotice />}

      <div className="set-group">
        <h3>{t("general.sections.application")}</h3>
        <div className="set-rows">
          <SetRow
            label={t("general.fields.applicationName.label")}
            help={t("general.fields.applicationName.help")}
          >
            <input
              type="text"
              value={text.application_name}
              maxLength={60}
              onChange={(e) =>
                setText((current) => ({ ...current, application_name: e.target.value }))
              }
              aria-label={t("general.fields.applicationName.label")}
            />
          </SetRow>
          <SetRow
            label={t("general.fields.applicationUrl.label")}
            help={t("general.fields.applicationUrl.help")}
          >
            <input
              type="text"
              value={text.application_url}
              placeholder={t("general.fields.applicationUrl.placeholder")}
              onChange={(e) =>
                setText((current) => ({ ...current, application_url: e.target.value }))
              }
              aria-label={t("general.fields.applicationUrl.label")}
            />
          </SetRow>
          <SetRow
            label={t("general.fields.timezone.label")}
            help={t("general.fields.timezone.help")}
          >
            <select
              value={text.timezone}
              aria-label={t("general.fields.timezone.label")}
              onChange={(e) => setText((current) => ({ ...current, timezone: e.target.value }))}
            >
              {zoneOptions.map((z) => (
                <option key={z} value={z}>
                  {z}
                </option>
              ))}
            </select>
          </SetRow>
        </div>
      </div>

      <div className="set-group">
        <h3>{t("general.sections.appearance")}</h3>
        <div className="set-rows">
          <SetRow
            variant="accent"
            label={t("general.accent.label")}
            help={t("general.accent.help")}
            after={
              <>
                {!accentValid && (
                  <p className="help field-error" id={ACCENT_ERROR_ID}>
                    {t("general.accent.error")}
                  </p>
                )}
                {/* role="group" is what carries the name: ARIA does not expose an aria-label on
                    a plain div, so "Quick colors" reached nobody. Same shape as `Segmented`. */}
                <div
                  className="presets"
                  role="group"
                  aria-label={t("general.accent.quickColorsGroup")}
                >
                  {accentPresets().map((c) => (
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
                {/* A picture of the theme, not working controls: the button is disabled and the
                    link goes nowhere on purpose. Hidden from the accessibility tree so the dead
                    link stops being announced as a real way to reach the deletion switch, and
                    tabIndex -1 keeps it out of the tab order (a focusable element inside
                    aria-hidden is itself a failure). The disabled button is already out of
                    both. */}
                <div
                  className="accent-preview"
                  aria-hidden="true"
                  style={
                    accentValid
                      ? ({
                          "--accent": accent,
                          "--accent-ink": accentInk(accent),
                          // --accent-text is set here too, or the link in the preview would
                          // keep the saved accent's ink while the button beside it moves. It is
                          // not derived from --accent at use time: the stylesheet computes it
                          // once on :root, from the values accent.ts writes there, so a child
                          // overriding --accent alone would inherit an ink belonging to a
                          // different color.
                          "--accent-text": accentText(accent, shownTheme),
                        } as CSSProperties)
                      : undefined
                  }
                >
                  <span className="pv-label">{t("general.accent.previewLabel")}</span>
                  <button className="primary" type="button" disabled>
                    {t("general.accent.previewScanButton")}
                  </button>
                  <a href="#" tabIndex={-1} onClick={(e) => e.preventDefault()}>
                    {t("general.accent.previewPolicyLink")}
                  </a>
                </div>
              </>
            }
          >
            {/* Swatch and hex code are one control (the .url-join pattern): the swatch is a
                prefix fused inside the field's box, so a narrow screen can never split them
                onto two lines. */}
            <span className="hex-join">
              <span className="swatch-wrap">
                <input
                  type="color"
                  value={accentValid ? accent : DEFAULT_ACCENT}
                  aria-label={t("general.accent.label")}
                  onChange={(e) => setAccent(e.target.value)}
                />
              </span>
              <input
                type="text"
                className="hexfield"
                value={accent}
                spellCheck={false}
                maxLength={7}
                aria-label={t("general.accent.hexFieldAriaLabel")}
                // The box refuses the save, and the sentence saying why sits below it, out of
                // reach of anyone who arrived at the box by keyboard.
                aria-invalid={accentValid ? undefined : true}
                aria-describedby={accentValid ? undefined : ACCENT_ERROR_ID}
                onChange={(e) => setAccent(e.target.value)}
              />
            </span>
            {accent.toLowerCase() !== DEFAULT_ACCENT && (
              <button className="link" onClick={() => setAccent(DEFAULT_ACCENT)}>
                {t("general.accent.resetToDefault")}
              </button>
            )}
          </SetRow>

          <SetRow label={t("general.language.label")} help={t("general.language.help")}>
            {/* The server holds the choice, because a notification is composed there with no
                browser to ask. So this saves first and repaints second: `setLanguage` writes
                this browser's copy and reloads onto it. Reaching it only from `mutateAsync`
                means a refused save leaves both halves on the old language, rather than a page
                speaking one language while the server stores another.

                There is no "match my browser" entry. The browser still decides on a fresh
                install: `App` seeds the server from `preferredLanguage()` the first time it
                finds nothing stored, but only as a one-time seed, not a standing mode, so what
                the picker shows is always what a notification will be written in.

                This is disabled while the save bar holds anything, because the reload would
                discard it with no chance to ask first. */}
            <select
              value={data.language ?? preferredLanguage()}
              aria-label={t("general.language.label")}
              disabled={pending.length > 0 || save.isPending}
              onChange={(e) => {
                const tag = e.target.value;
                void save
                  .mutateAsync({ language: tag })
                  .then(() => setLanguage(tag))
                  // The refusal is already on screen through `save.error`. This only keeps the
                  // rejected promise from going unhandled, which a test would otherwise fail on.
                  .catch(() => {});
              }}
            >
              {LANGUAGES.map((tag) => (
                <option key={tag} value={tag}>
                  {languageName(tag)}
                </option>
              ))}
            </select>
          </SetRow>

          <SetRow label={t("general.theme.label")} help={t("general.theme.help")}>
            <select
              value={theme}
              aria-label={t("general.theme.label")}
              onChange={(e) => {
                const next = e.target.value as ThemeChoice;
                setTheme(next);
                applyTheme(next);
              }}
            >
              <option value="system">{t("general.theme.optionSystem")}</option>
              <option value="light">{t("general.theme.optionLight")}</option>
              <option value="dark">{t("general.theme.optionDark")}</option>
            </select>
          </SetRow>
        </div>
      </div>

      <div className="set-group">
        <h3>{t("general.sections.reviewQueue")}</h3>
        <div className="set-rows">
          <SetRow label={t("general.expandSeasons.label")} help={t("general.expandSeasons.help")}>
            {/* Four choices, so this is a select rather than a Segmented, on the same control
                standard as the Theme picker above. */}
            <select
              value={data.expand_seasons_mode}
              aria-label={t("general.expandSeasons.label")}
              disabled={save.isPending}
              onChange={(e) =>
                save.mutate({ expand_seasons_mode: e.target.value as ExpandSeasonsMode })
              }
            >
              <option value="off">{t("general.expandSeasons.optionOff")}</option>
              <option value="desktop">{t("general.expandSeasons.optionDesktop")}</option>
              <option value="both">{t("general.expandSeasons.optionBoth")}</option>
              <option value="mobile">{t("general.expandSeasons.optionMobile")}</option>
            </select>
          </SetRow>
          <SetRow label={t("general.spareLength.label")} help={t("general.spareLength.help")}>
            {/* Both halves read and write the draft, never the stored value. A press stages
                  the mode in the save bar beside the number, so the bar names one field, one
                  Discard puts both back, and neither is written until Save.

                  Both halves also stop taking presses while the save is in flight, for the
                  same reason: `save`'s `onSuccess` re-seeds this mode from the response. A
                  press landing in that gap would be overwritten and the bar cleared in the same
                  flush, with nothing on screen to explain why. */}
            <Segmented
              value={spareForever ? "forever" : "days"}
              options={[
                ["days", t("general.spareLength.optionDays")],
                ["forever", t("general.spareLength.optionForever")],
              ]}
              label={t("general.spareLength.label")}
              disabled={save.isPending}
              onChange={(mode) => setSpareForever(mode === "forever")}
            />
            {/* Only while the draft is a length. Forever hides the box, matching how a
                  group's sub-controls disappear when its toggle is off. */}
            {!spareForever && (
              <FixedQuantity
                value={spareDays}
                suffix={t("general.spareLength.daysSuffix")}
                min={1}
                max={3650}
                width="narrow"
                ariaLabel={t("general.spareLength.daysAriaLabel")}
                disabled={save.isPending}
                onChange={(n) => setSpareDays(Math.max(1, Math.min(3650, n)))}
              />
            )}
          </SetRow>
        </div>
      </div>

      <div className="set-group">
        <h3>{t("general.sections.apiAccess")}</h3>
        <div className="set-rows">
          {/* This is a cluster, not a box: the key field plus four buttons. It keeps a
              shrink-to-fit control column so those buttons stay on one line (see
              `.set-row-cluster`).

              The `help` sentence below is the whole basis on which an operator decides to hand
                a key to a third-party dashboard, so it names exactly what the fence in
                api/middleware.py (`_API_KEY_READS_DENIED` / `_API_KEY_WRITES`) allows, never a
                rounder claim. Which direction of rounding is safe differs by clause:

                - What a key can WRITE must never claim less access than the fence actually
                  allows. Understating write access is what lets a key holder change something
                  the sentence promised was off-limits, unnoticed.
                - What a key can READ should round toward more rather than less, since "more
                  than you think" is the safer way to be wrong on a screen where a key is being
                  handed out. A key reads more than just the library: every settings page too.
                - A stated REFUSAL is the opposite case: naming a refusal the fence does not
                  actually enforce is what gets someone hurt, since the operator will trust it
                  and act accordingly. Any refusal named here must be checked against the
                  current fence before it ships. The closing list stays generic ("any other
                  setting") rather than enumerating specifics, since naming a few would read as
                  a promise that the rest are allowed.

                This paragraph is hand-written, and its twin in the API reference is generated,
                so nothing here fails automatically when the fence moves.
                `test_the_sentence_leads_with_what_the_key_can_do` guards the other side: it
                pins the twin phrase for phrase and names this file in every failure
                message. */}
          <SetRow
            variant="cluster"
            label={t("general.apiKey.label")}
            help={t("general.apiKey.help")}
          >
            {data.api_key_set ? (
              <>
                <input
                  className="keyfield"
                  type="text"
                  readOnly
                  value={revealedKey ?? "••••••••••••••••••••••••"}
                  aria-label={t("general.apiKey.fieldAriaLabel")}
                />
                {revealedKey === null ? (
                  <button disabled={reveal.isPending} onClick={() => reveal.mutate()}>
                    {t("general.apiKey.show")}
                  </button>
                ) : (
                  <button onClick={() => setRevealedKey(null)}>{t("common.hide")}</button>
                )}
                <button disabled={copy.isPending} onClick={() => copy.mutate()}>
                  {copied ? t("general.apiKey.copied") : t("general.apiKey.copy")}
                </button>
                {confirmReplace ? (
                  <>
                    <button
                      className="danger"
                      disabled={generate.isPending}
                      onClick={() => generate.mutate()}
                    >
                      {t("general.apiKey.confirmReplace")}
                    </button>
                    {/* Backing out clears the notice too. It is the shared one above, and the
                          only other thing that clears it is the next mutation starting.
                          Without this, a notice raised to explain a confirm would outlive the
                          confirm it explained, and keep describing a button no longer on the
                          page. The twin Cancel below does the same. */}
                    <button
                      onClick={() => {
                        setConfirmReplace(false);
                        setKeyError(null);
                      }}
                    >
                      {t("common.cancel")}
                    </button>
                  </>
                ) : (
                  <button
                    className="ghost"
                    title={t("general.apiKey.replaceTitle")}
                    onClick={() => setConfirmReplace(true)}
                  >
                    {t("general.apiKey.replace")}
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
                      {t("common.confirmRemove")}
                    </button>
                    <button onClick={() => setConfirmRemove(false)}>{t("common.cancel")}</button>
                  </>
                ) : (
                  <button
                    className="ghost"
                    title={t("general.apiKey.removeTitle")}
                    onClick={() => setConfirmRemove(true)}
                  >
                    {t("general.apiKey.remove")}
                  </button>
                )}
              </>
            ) : confirmReplace ? (
              /* Reached only when the re-read in `requestGenerate` could not answer, so this
                   panel cannot prove there is no key to destroy. Same two-step shape as Replace
                   above, because it is the same act with a worse-known target. The notice under
                   the group says why it is being asked.
                   "Only" holds because the flag resets whenever `api_key_set` changes (the
                   effect beside the mutations), and again the moment Remove succeeds. Without
                   those resets it would stay true after a Remove too, opening this branch with
                   no notice, a danger confirm over a key the panel had just proved gone. */
              <>
                <button
                  className="danger"
                  disabled={generate.isPending}
                  onClick={() => generate.mutate()}
                >
                  {t("general.apiKey.confirmGenerate")}
                </button>
                <button
                  onClick={() => {
                    setConfirmReplace(false);
                    setKeyError(null);
                  }}
                >
                  {t("common.cancel")}
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
                  ? t("general.apiKey.generating")
                  : requestGenerate.isPending
                    ? t("common.checking")
                    : t("general.apiKey.generate")}
              </button>
            )}
          </SetRow>
          {/* A link, not a box, so it releases the control track (`.set-row-plain`).

              The `help` sentence says "as you", not "with your key", because the page
                preselects your session: 35 of the 47 writes do not offer the key at all, and
                the button reaches them all, arming included. Naming the key here would size
                the blast radius by the fence two rows up, which is far tighter than what this
                button actually spends. The key clause above is generated and guarded. This one
                is hand-written, and its guard is
                test_the_reference_page_sends_the_csrf_header_it_names, which names this
                file. */}
          <SetRow
            variant="plain"
            label={t("general.apiReference.label")}
            help={t("general.apiReference.help")}
          >
            <a className="btn-link" href="/api/docs" target="_blank" rel="noreferrer">
              {t("general.apiReference.link")} <span aria-hidden="true">↗</span>
            </a>
          </SetRow>
        </div>
        {keyError && <Notice tone="error">{keyError}</Notice>}
      </div>

      <div className="set-group">
        <h3>{t("general.sections.reverseProxy")}</h3>
        <div className="set-rows">
          {/* A Switch, not a box, so it releases the control track (`.set-row-plain`). The row
              below it holds the addresses box and keeps the track. */}
          <SetRow
            variant="plain"
            label={t("general.proxy.enabledLabel")}
            help={t("general.proxy.enabledHelp")}
          >
            <Switch
              checked={data.proxy_trust_enabled}
              disabled={save.isPending}
              ariaLabel={t("general.proxy.enabledLabel")}
              onChange={(enabled) => save.mutate({ proxy_trust_enabled: enabled })}
            />
          </SetRow>
          <SetRow
            dim={!data.proxy_trust_enabled}
            label={t("general.proxy.addressesLabel")}
            help={t("general.proxy.addressesHelp")}
          >
            <input
              type="text"
              value={proxies}
              disabled={!data.proxy_trust_enabled}
              placeholder={t("general.proxy.addressesPlaceholder")}
              onChange={(e) => setProxies(e.target.value)}
              aria-label={t("general.proxy.addressesLabel")}
            />
          </SetRow>
        </div>
        <p className="group-hint muted">{t("general.proxy.hint")}</p>
      </div>

      {/* Present only when the server says it runs as the Mac or Windows app; the container,
          the snap, and a source run report null and no group renders. Each Switch saves on
          the spot (the reverse-proxy Switch's shape) and the values render from the query
          data the save's response refreshed, so there is nothing here for the save bar. */}
      {data.desktop && (
        <div className="set-group">
          <h3>{t("general.sections.desktopApp")}</h3>
          <p className="group-blurb">{t("general.desktop.blurb")}</p>
          <div className="set-rows">
            {data.desktop.platform === "macos" && (
              <SetRow
                variant="plain"
                label={t("general.desktop.dockIconLabel")}
                help={t("general.desktop.dockIconHelp")}
              >
                <Switch
                  checked={data.desktop.dock_icon}
                  disabled={save.isPending}
                  ariaLabel={t("general.desktop.dockIconLabel")}
                  onChange={(enabled) => save.mutate({ dock_icon: enabled })}
                />
              </SetRow>
            )}
            <SetRow variant="plain" label={trayLabel} help={trayHelp}>
              <Switch
                checked={data.desktop.tray}
                disabled={save.isPending}
                ariaLabel={trayLabel}
                onChange={(enabled) => save.mutate({ tray: enabled })}
              />
            </SetRow>
          </div>
        </div>
      )}

      {/* Only when there is no bar to put it in. A control that saves on the spot fails with
          nothing unsaved, so its refusal has nowhere else to go. A refused bar save renders
          inside the bar instead, beside the fields it just refused to write. */}
      {save.error && pending.length === 0 && (
        <Notice tone="error">{describeError(save.error)}</Notice>
      )}

      {/* The one save affordance on this panel, the same bar the policy editor uses: it names
          what is unsaved, saves all of it in one press, and offers Discard. Rendered only
          while there is something to save, and sticky at the foot of the screen, so the field
          being typed in never moves. */}
      {pending.length > 0 && (
        <div className="savebar">
          <span className="savebar-what">
            {t("common.savebar.unsavedChangesPrefix")}{" "}
            <strong>{pending.map((p) => p.label).join(", ")}</strong>
            {accentBlocks && <span className="savebar-blocked">{t("general.accent.error")}</span>}
          </span>
          <button className="ghost" disabled={save.isPending} onClick={discardDrafts}>
            {t("common.discard")}
          </button>
          <button
            className="primary"
            disabled={save.isPending || accentBlocks}
            onClick={() => {
              bar.leaving();
              save.mutate(Object.assign({}, ...pending.map((p) => p.patch)));
            }}
          >
            {save.isPending ? t("common.saving") : t("common.saveChanges")}
          </button>
          {/* Inside the bar, not below the panel, the same slot `PolicyEditor`'s bar uses. The
              route refuses the whole body before writing any of it, so this sentence is the
              only thing standing between the operator and the belief that all six fields went
              in. The bar is sticky, so a notice outside it renders at the document foot,
              off screen for anyone editing the top group, which is where five of these six
              fields are. */}
          {save.error && <Notice tone="error">{describeError(save.error)}</Notice>}
        </div>
      )}
    </div>
  );
}
