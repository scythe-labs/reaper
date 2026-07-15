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

/** How long ago, in the coarse terms the decisions are actually made in. */
export function since(iso: string): string {
  const days = Math.floor((Date.now() - new Date(iso).getTime()) / 86_400_000);

  if (days < 1) return "today";
  if (days === 1) return "yesterday";
  if (days < 60) return `${days} days ago`;
  if (days < 730) return `${Math.floor(days / 30)} months ago`;
  return `${(days / 365).toFixed(1)} years ago`;
}
