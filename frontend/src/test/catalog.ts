// SPDX-License-Identifier: AGPL-3.0-or-later

/** A catalog's leaf messages, dot-joined the way t() spells the key. Shared by the key gate
 *  (i18n-keys.test.ts) and the translated-catalog gate (i18n-locales.test.ts). */
export function leaves(node: unknown, prefix = ""): Record<string, string> {
  if (typeof node === "string") return { [prefix]: node };
  const out: Record<string, string> = {};
  for (const [k, v] of Object.entries(node as Record<string, unknown>)) {
    Object.assign(out, leaves(v, prefix ? `${prefix}.${k}` : k));
  }
  return out;
}
