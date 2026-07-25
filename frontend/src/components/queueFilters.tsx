// SPDX-License-Identifier: AGPL-3.0-or-later
//
// The review queue's filters: what can be filtered on, and what this device remembers per tab.
//
// Lifted out of ReviewQueue.tsx, which held the whole filter subsystem alongside the fate
// primitives, both card shapes and the queue container (R-1). Nothing here renders; the
// toolbar that draws these lives with the queue.

import type { ReactNode } from "react";
import type { OverrideFilter, RequestedFilter, SortKey, SortOrder } from "../api";

export const MEDIA_FILTERS: { value: string; label: string }[] = [
  { value: "", label: "Everything" },
  { value: "movie", label: "Movies" },
  { value: "season", label: "TV shows" },
];

export const REQUESTED_FILTERS: { value: RequestedFilter; label: string }[] = [
  { value: "any", label: "Anyone" },
  { value: "yes", label: "Requested" },
  { value: "no", label: "Not requested" },
];

export const OVERRIDE_FILTERS: { value: OverrideFilter; label: string }[] = [
  { value: "any", label: "Any override" },
  { value: "spare", label: "Spared by hand" },
  { value: "reap", label: "Reaped by hand" },
  { value: "none", label: "No override" },
];

export const SORTS: { value: SortKey; label: string }[] = [
  { value: "score", label: "Score" },
  { value: "size", label: "Size" },
  { value: "year", label: "Year" },
  { value: "title", label: "Title" },
];

/** One filterable dimension of the review queue: a queue-filter field paired with a value
 *  list, a label and an icon. The ＋ Filter menu and the active-filter chips are built from
 *  these, so adding a future filter is one more entry here -- never another toolbar control.
 *  `defaultValue` is the field's "off" value: a filter is active when its value differs from
 *  it, and clearing resets to it. Sort is deliberately not a dimension -- it orders the list
 *  and hides nothing, so it is never a removable chip. */
export interface FilterDimension {
  id: string;
  label: string;
  icon: ReactNode;
  defaultValue: string;
  options: { value: string; label: string }[];
  value: (f: QueueFilters) => string;
  set: (f: QueueFilters, value: string) => QueueFilters;
}

// --- remembered filters --------------------------------------------------------------------
// Each queue tab keeps its own filters and sort, on this device, until changed or cleared.

export interface QueueFilters {
  mediaType: string;
  requested: RequestedFilter;
  genre: string;
  library: string;
  override: OverrideFilter;
  sort: SortKey;
  order: SortOrder;
}

export const DEFAULT_FILTERS: QueueFilters = {
  mediaType: "",
  requested: "any",
  genre: "",
  library: "",
  override: "any",
  sort: "score",
  order: "desc",
};

export const filtersKey = (verdict: string) => `reaper.queue.filters.${verdict}`;

/** The remembered filters for one tab, sanitized field by field: an unknown or outgrown
 *  stored value falls back to that field's default instead of poisoning the whole set. */
export function loadFilters(verdict: string): QueueFilters {
  let raw: string | null;
  try {
    // window.localStorage, never the bare global: Node exposes an experimental global
    // of the same name, so the bare name is the wrong object under the test runner.
    raw = window.localStorage.getItem(filtersKey(verdict));
  } catch {
    return { ...DEFAULT_FILTERS };
  }
  if (!raw) return { ...DEFAULT_FILTERS };
  let parsed: unknown;
  try {
    parsed = JSON.parse(raw);
  } catch {
    return { ...DEFAULT_FILTERS };
  }
  const stored = (parsed ?? {}) as Partial<Record<keyof QueueFilters, unknown>>;
  const pick = <T,>(value: unknown, allowed: readonly T[], fallback: T): T =>
    allowed.includes(value as T) ? (value as T) : fallback;
  return {
    mediaType: pick(
      stored.mediaType,
      MEDIA_FILTERS.map((f) => f.value),
      DEFAULT_FILTERS.mediaType,
    ),
    requested: pick(
      stored.requested,
      REQUESTED_FILTERS.map((f) => f.value),
      DEFAULT_FILTERS.requested,
    ),
    genre: typeof stored.genre === "string" ? stored.genre : DEFAULT_FILTERS.genre,
    library: typeof stored.library === "string" ? stored.library : DEFAULT_FILTERS.library,
    override: pick(
      stored.override,
      OVERRIDE_FILTERS.map((f) => f.value),
      DEFAULT_FILTERS.override,
    ),
    sort: pick(
      stored.sort,
      SORTS.map((s) => s.value),
      DEFAULT_FILTERS.sort,
    ),
    order: pick(stored.order, ["asc", "desc"] as const, DEFAULT_FILTERS.order),
  };
}

export function saveFilters(verdict: string, filters: QueueFilters): void {
  try {
    window.localStorage.setItem(filtersKey(verdict), JSON.stringify(filters));
  } catch {
    // Storage can be unavailable (private mode, full quota); filters simply stop being
    // remembered, which is the pre-existing behavior, never an error.
  }
}
