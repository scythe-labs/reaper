// SPDX-License-Identifier: AGPL-3.0-or-later

/** Bytes, in the units people actually reason about disk in.
 *
 *  Binary units (TiB), because that is what `df`, Sonarr and Radarr report -- showing
 *  4.6 TB next to an *arr's 4.2 TiB for the same files invites the owner to conclude
 *  Reaper has miscounted, and they would be right to worry. */
export function bytes(value: number): string {
  if (value <= 0) return "0 B";

  const units = ["B", "KiB", "MiB", "GiB", "TiB", "PiB"];
  const exponent = Math.min(Math.floor(Math.log2(value) / 10), units.length - 1);
  const scaled = value / 1024 ** exponent;

  // One decimal below 100, none above: "5.9 GiB", "214 GiB".
  const digits = exponent === 0 || scaled >= 100 ? 0 : 1;
  return `${scaled.toFixed(digits)} ${units[exponent]}`;
}

/** One item's size on disk, for the surfaces the operator scans while deciding.
 *
 *  `null` means nothing would report a size, and the server says so directly now rather
 *  than sending a `0` for the client to guess about. A real `0` therefore renders as
 *  "0 B" again, honestly.
 *
 *  Totals use `totalBytes` below, which carries the unknown count beside the sum. */
export function itemBytes(value: number | null): string {
  return value === null ? "Size unknown" : bytes(value);
}

/** A total, plus how many items it could not include.
 *
 *  A sum with an unmeasured item in it is quietly low, and "low" is the dangerous
 *  direction beside a delete control. So the sum covers what is known and the count
 *  rides alongside it, suppressed entirely at zero: an operator whose sources all
 *  answer sees exactly what they saw before. */
export function totalBytes(known: number, unknown: number): string {
  if (unknown === 0) return bytes(known);
  return `${bytes(known)} · ${count(unknown)} ${unknown === 1 ? "size" : "sizes"} unknown`;
}

export function count(value: number): string {
  return value.toLocaleString();
}

/** Basis points to a percentage. Coverage is stored as bp because the policy body is
 *  integers-only -- floats do not canonicalise, and an unstable hash would void
 *  approvals at random. */
export function coverage(bp: number): string {
  return `${Math.round(bp / 100)}%`;
}

export function date(iso: string): string {
  return new Date(iso).toLocaleDateString(undefined, {
    year: "numeric",
    month: "short",
    day: "numeric",
  });
}

/** The clock time, for the surfaces that want the hour a thing happened, not just the day. */
export function time(iso: string): string {
  return new Date(iso).toLocaleTimeString(undefined, { hour: "numeric", minute: "2-digit" });
}

/** How long ago, in the coarse terms the decisions are actually made in. */
export function since(iso: string): string {
  const days = Math.floor((Date.now() - new Date(iso).getTime()) / 86_400_000);

  if (days < 1) return "today";
  if (days === 1) return "yesterday";
  if (days < 60) return `${days} days ago`;
  if (days < 730) return `${Math.floor(days / 30)} months ago`;
  return `${(days / 365).toFixed(1)} years ago`;
}
