// SPDX-License-Identifier: AGPL-3.0-or-later
//
// Turning a failed request into the operator's sentence (docs/history/I18N_PLAN.md, phase 8b).
//
// api.ts stays free of i18next: every failure it throws is an ApiError carrying the server's
// own catalog code(s) and raw params beside the English `message` a client with no catalog
// still reads. This is where that gets composed into ui.json's `error.*` namespace, the same
// way why.ts composes a Reason under `why.*`. `composeIn` is what derives `field_label` etc.
// from the raw params either way, so this reuses it rather than re-deriving them.
//
// A refusal's `code` is already the catalog's full dotted path (`error.policy.unknown_field`,
// matching `reaper.refusal.MESSAGES`'s own key), and `composeIn(namespace, key)` looks
// entries up under `${namespace}.${key.k}`, the same shape `why.ts` uses for a bare reason
// id ("blocked", not "why.blocked"). So the leading "error." is stripped before handing the
// rest to `composeIn("error", ...)`, or the lookup would double it.
//
// A 422's several coded items (api.errors.validation_error_items) each compose on their own
// and join with a space, matching how api.ts already joins their English `msg` fields to
// build `ApiError.message`.

import type { ApiErrorItem } from "./api";
import { ApiError } from "./api";
import { composeError } from "./why";

/** One code composed through the catalog. A code this build's catalog has no entry for
 *  composes to its own bare code, the same fallback every other reason takes
 *  (`why.ts`'s `composeIn`), never the server's English `message`. */
function composeCode(code: string, params: Record<string, unknown>): string {
  return composeError({ k: code, p: params });
}

function composeItem(item: ApiErrorItem): string {
  return item.code ? composeCode(item.code, item.params) : item.msg;
}

/** The operator's sentence for a failed request, in the active locale: an `ApiError` with a
 *  code (or several, for a 422) composes through the catalog; anything else, a plain `Error`
 *  or a thrown non-error, falls back to its own English `message`. Every render of a caught
 *  error's `.message` goes through this. */
export function describeError(err: unknown): string {
  if (err instanceof ApiError) {
    if (err.items && err.items.length > 0) return err.items.map(composeItem).join(" ");
    if (err.code) return composeCode(err.code, err.params);
    return err.message;
  }
  return err instanceof Error ? err.message : String(err);
}
