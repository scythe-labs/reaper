// SPDX-License-Identifier: AGPL-3.0-or-later
//
// Composing a typed reason into the operator's sentence (docs/history/I18N_PLAN.md §5).
//
// The backend stopped writing English details: a gate, signal or keep row carries a
// `detail_key` -- a catalog id plus raw params -- and this module turns it into the
// sentence under `why.*` in the catalog. A row frozen before the conversion carries a
// `legacy` key wrapping its sentence (`{k: "legacy", p: {text}}`), which this module
// composes verbatim, never translated.
//
// Params are raw values -- day counts, byte counts, tenths -- and the derived forms the
// messages reference are computed here, once, for every numeric param `x`:
//   {x_span}    humanDays(x)      "5 years, 9 months"
//   {x_window}  humanWindow(x)    "year" (for "in the last ...")
//   {x_gb}      x bytes as GB     "1.5"
//   {x_tenths}  x tenths, 1dp     "8.2"
//   {x_fixed1}  x, 1dp            "7.0"
// A `field` param additionally derives {field_label}, {field_subject} (why.field.*) and
// {field_check} (why.check.*); a `source` param derives {source_label} (why.source.*).
//
// A param may itself be a reason key, or a list of them (a blocked check's cause, the
// rating gate's per-bar clauses): those compose recursively, lists joined with "; ",
// so a message template only ever sees strings and numbers.
//
// composeIn(namespace, key) is the general form: it looks entries up under
// `${namespace}.${key.k}` instead of the hardcoded `why.`, so a chip status or a policy
// warning can carry its own catalog section (`chip.text.*`, `warning.*`) instead of
// crowding `why.*`. composeReason is composeIn("why", key), unchanged. A nested Reason
// param always recurses under "why", never the outer namespace: check/cause/because are
// a shared vocabulary a chip or warning row QUOTES, the same entries a why-panel row
// would show for the identical evidence, not copy that section owns. Splitting them by
// namespace would mean writing (and translating) the same cause sentence once per
// surface that can carry it.

import i18next from "./i18n";
import type { ReasonKey } from "./api";
import { humanDays, humanWindow } from "./format";

export function isReasonKey(value: unknown): value is ReasonKey {
  return (
    typeof value === "object" && value !== null && typeof (value as { k?: unknown }).k === "string"
  );
}

function lookup(key: string, params?: Record<string, unknown>): string | undefined {
  return i18next.exists(key) ? i18next.t(key, params ?? {}) : undefined;
}

// The movie and season halves of five `why.cause.*` pairs merged behind one media-typed
// key (identical concept, two literal ids -- `plex_unmatched`/`plex_season_unmatched` and
// four siblings). A scan snapshot frozen before that merge can still hold the retired
// season-side id, with no `mediaType` param at all: the catalog no longer has an entry for
// it, so this rewrites it to the surviving id plus the param the merge added, and the row
// renders the same "this season"/"Sonarr" wording it always did. Serves only rows frozen
// before the merge; delete once those scans have aged out of the last `keep_scans`
// snapshots Reaper keeps (`services.retention.KEEP_SNAPSHOTS`).
const RETIRED_SEASON_CAUSE_ALIASES: Record<string, string> = {
  "cause.plex_season_unmatched": "cause.plex_unmatched",
  "cause.plex_show_ambiguous": "cause.plex_ambiguous",
  "cause.sonarr_plex_disagree": "cause.radarr_plex_disagree",
  "cause.no_season_added_at": "cause.no_added_at",
  "cause.no_season_size": "cause.no_file_size",
};

// Every id that carries a `mediaType` select today but has a stored row from before it did:
// the five merged `cause.*` ids above, by their surviving key, plus all four Reasons
// `RewatchOddsGate` emits off its cohort. `rewatch_thin` got its select in #906, where it
// merged with the why-panel's own evidence block; the other three got theirs in #908, which
// swept the siblings that fix left saying "titles" on a TV lane.
// ICU's `select` does not fall to `other` when the variable is entirely absent from params
// -- it leaves the raw template unparsed instead, proven in why.test.ts -- so a row frozen
// before its select shipped prints broken syntax rather than the sentence it always
// rendered. Defaulted below to the wording those bare rows always had (every one of them
// read "titles", on both lanes, which is the bug #906 and #908 fixed); a fresh row's own
// "movie"/"season" param overrides it like any other entry the loop below sets.
const MEDIA_TYPED_IDS = new Set([
  ...Object.values(RETIRED_SEASON_CAUSE_ALIASES),
  "rewatch_thin",
  "rewatch_no_history",
  "rewatch_watched_again",
  "rewatch_under_floor",
]);

/** The Wilson 95% upper bound of k/n, as a whole percent -- mirrors
 *  `gates.wilson_upper` (same z, same formula) exactly, so a value computed here and one
 *  computed there never disagree. Used only to backfill `{bound_pct}` on a `rewatch_watched_again`
 *  / `rewatch_under_floor` row stored before that param shipped (#936): a fresh row always
 *  carries its own `bound_pct` from the gate that decided it, and this never overrides one.
 *  `WhyPanel.tsx`'s own rewatch-odds display block falls back to it the same way, off
 *  `RewatchOdds.bound_pct`. */
export function wilsonUpperPct(k: number, n: number): number {
  if (n <= 0) return 0;
  const z = 1.96;
  const p = k / n;
  const denom = 1 + (z * z) / n;
  const center = p + (z * z) / (2 * n);
  const spread = z * Math.sqrt((p * (1 - p)) / n + (z * z) / (4 * n * n));
  return Math.round(((center + spread) / denom) * 100);
}

/** The two `RewatchOddsGate` reasons whose sentence quotes the Wilson bound (#936). Both
 *  always carry `k`/`n`, so a row frozen before `bound_pct` shipped can still have it
 *  backfilled from them, the same shape `MEDIA_TYPED_IDS` above handles for `mediaType`. */
const REWATCH_BOUND_IDS = new Set(["rewatch_watched_again", "rewatch_under_floor"]);

export function composeIn(namespace: string, key: ReasonKey): string {
  if (key.k === "legacy") return String(key.p?.text ?? "");
  const retiredId = namespace === "why" ? RETIRED_SEASON_CAUSE_ALIASES[key.k] : undefined;
  const aliased: ReasonKey = retiredId
    ? { k: retiredId, p: { ...(key.p ?? {}), mediaType: "season" } }
    : key;
  const params: Record<string, unknown> = {};
  for (const [name, value] of Object.entries(aliased.p ?? {})) {
    if (isReasonKey(value)) {
      // A nested reason whose own id is a full `error.*` catalog code (an
      // `IntegrationError`/`PlexError`'s own code, carried in via `as_reason()`) composes
      // through the error namespace, never `why`: `why.error.integration.timed_out` is not
      // a key this catalog has, and it never should be -- the sentence already lives at
      // `error.integration.timed_out`, the same entry the backend's own `english()` reads.
      params[name] = value.k.startsWith("error.") ? composeError(value) : composeIn("why", value);
    } else if (Array.isArray(value)) {
      params[name] = value
        .filter(isReasonKey)
        .map((v) => (v.k.startsWith("error.") ? composeError(v) : composeIn("why", v)))
        .join("; ");
    } else {
      params[name] = value;
      if (typeof value === "number") {
        params[`${name}_span`] = humanDays(value);
        params[`${name}_window`] = humanWindow(value);
        params[`${name}_gb`] = (value / 1_000_000_000).toFixed(1);
        params[`${name}_tenths`] = (value / 10).toFixed(1);
        params[`${name}_fixed1`] = value.toFixed(1);
      }
    }
  }
  if (MEDIA_TYPED_IDS.has(aliased.k) && !("mediaType" in params)) {
    params.mediaType = "movie";
  }
  if (
    REWATCH_BOUND_IDS.has(aliased.k) &&
    typeof params.bound_pct !== "number" &&
    typeof params.k === "number" &&
    typeof params.n === "number"
  ) {
    params.bound_pct = wilsonUpperPct(params.k, params.n);
  }
  if (typeof params.field === "string") {
    const label = lookup(`why.field.${params.field}`);
    params.field_label = label ?? params.field;
    params.field_subject = label ?? params.field;
    params.field_check = lookup(`why.check.${params.field}`) ?? params.field;
  }
  if (typeof params.source === "string") {
    params.source_label = lookup(`why.source.${params.source}`) ?? params.source;
  }
  const text = lookup(`${namespace}.${aliased.k}`, params);
  if (text !== undefined) return text;
  // An id this build has no entry for: a stored legacy sentence riding in a slot, or a
  // row written by a newer build. Render what identifies it rather than dropping the row
  // (the same never-blank-the-panel posture the backend readers take), shorn of a slot
  // namespace so a legacy cause sentence reads as itself.
  const raw = aliased.p?.text;
  if (typeof raw === "string" && raw) return raw;
  return aliased.k.replace(/^(cause|check)\./, "");
}

export function composeReason(key: ReasonKey): string {
  return composeIn("why", key);
}

/** A bare wire-level `ReasonKey` whose `k` is already a full `error.*` catalog code (a poll's
 *  retrying reason, a stored Leaving Soon skip) -- unlike a `why`-side reason, whose id never
 *  repeats the namespace it lives under. `composeIn`'s own namespace prefixing would double it
 *  ("error.error.plex...."), so the leading "error." is stripped first; `composeIn` already
 *  handles the "legacy" shape and the missing-entry fallback (the bare code, still readable),
 *  so nothing else is needed here. */
export function composeError(key: ReasonKey): string {
  return composeIn("error", { k: key.k.replace(/^error\./, ""), p: key.p ?? null });
}

/** The check and cause of a blocked row, composed separately, or null where the row is
 *  not the blocked shape -- a deliberate left-for-you sentence keeps its own row. */
export function blockedParts(key: ReasonKey): { check: string; cause: string } | null {
  if (key.k !== "blocked") return null;
  const check = key.p?.check;
  const cause = key.p?.cause;
  if (!isReasonKey(check) || !isReasonKey(cause)) return null;
  return { check: composeReason(check), cause: composeReason(cause) };
}

/** The card's amber-pill span: composed from a fresh row's raw day count. Null on a
 *  legacy row -- and when the signal was not evaluated, or the row carries neither -- and
 *  the pill hides (#899). */
export function dormantSpan(item: { dormant_days?: number | null }): string | null {
  return item.dormant_days != null ? humanDays(item.dormant_days) : null;
}

/** The card's one-line "why", composed from its key. A row frozen before typed reasons
 *  carries a `legacy` key wrapping its stored sentence, composed verbatim. Null when the
 *  row carries no reason at all and the line is hidden. */
export function cardReason(item: { reason_key?: ReasonKey | null }): string | null {
  return item.reason_key ? composeReason(item.reason_key) : null;
}
