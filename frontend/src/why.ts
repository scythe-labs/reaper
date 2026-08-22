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

export function composeIn(namespace: string, key: ReasonKey): string {
  if (key.k === "legacy") return String(key.p?.text ?? "");
  const params: Record<string, unknown> = {};
  for (const [name, value] of Object.entries(key.p ?? {})) {
    if (isReasonKey(value)) {
      params[name] = composeIn("why", value);
    } else if (Array.isArray(value)) {
      params[name] = value
        .filter(isReasonKey)
        .map((v) => composeIn("why", v))
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
  if (typeof params.field === "string") {
    const label = lookup(`why.field.${params.field}`);
    params.field_label = label ?? params.field;
    params.field_subject = label ?? params.field;
    params.field_check = lookup(`why.check.${params.field}`) ?? params.field;
  }
  if (typeof params.source === "string") {
    params.source_label = lookup(`why.source.${params.source}`) ?? params.source;
  }
  const text = lookup(`${namespace}.${key.k}`, params);
  if (text !== undefined) return text;
  // An id this build has no entry for: a stored legacy sentence riding in a slot, or a
  // row written by a newer build. Render what identifies it rather than dropping the row
  // (the same never-blank-the-panel posture the backend readers take), shorn of a slot
  // namespace so a legacy cause sentence reads as itself.
  const raw = key.p?.text;
  if (typeof raw === "string" && raw) return raw;
  return key.k.replace(/^(cause|check)\./, "");
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
