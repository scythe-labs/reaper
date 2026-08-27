// SPDX-License-Identifier: AGPL-3.0-or-later
//
// The review queue's filters: what can be filtered on, and what this device remembers per tab.
//
// Split out of ReviewQueue.tsx into its own module. Nothing here renders; the toolbar that
// draws these lives with the queue.

import type { ReactNode } from "react";
import type { OverrideFilter, RequestedFilter, SortKey, SortOrder } from "../api";
import i18next from "../i18n";

// Each a function, not a module-level constant: the labels come from the catalog, and a
// constant built at import time would freeze at whatever language was active on first load
// (`applyStoredLanguage` resolves after this module's first import). Called fresh by the
// components that already re-render on a language change.
export const mediaFilters = (): { value: string; label: string }[] => [
  { value: "", label: i18next.t("reviewQueue.filterOptions.media.any") },
  { value: "movie", label: i18next.t("reviewQueue.filterOptions.media.movie") },
  { value: "season", label: i18next.t("reviewQueue.filterOptions.media.season") },
];

export const requestedFilters = (): { value: RequestedFilter; label: string }[] => [
  { value: "any", label: i18next.t("reviewQueue.filterOptions.requested.any") },
  { value: "yes", label: i18next.t("reviewQueue.filterOptions.requested.yes") },
  { value: "no", label: i18next.t("reviewQueue.filterOptions.requested.no") },
];

export const overrideFilters = (): { value: OverrideFilter; label: string }[] => [
  { value: "any", label: i18next.t("reviewQueue.filterOptions.override.any") },
  { value: "spare", label: i18next.t("reviewQueue.filterOptions.override.spare") },
  { value: "reap", label: i18next.t("reviewQueue.filterOptions.override.reap") },
  { value: "none", label: i18next.t("reviewQueue.filterOptions.override.none") },
];

export const sorts = (): { value: SortKey; label: string }[] => [
  { value: "score", label: i18next.t("reviewQueue.filterOptions.sort.score") },
  { value: "size", label: i18next.t("reviewQueue.filterOptions.sort.size") },
  { value: "year", label: i18next.t("reviewQueue.filterOptions.sort.year") },
  { value: "title", label: i18next.t("reviewQueue.filterOptions.sort.title") },
];

/** One filterable dimension of the review queue: a queue-filter field paired with a value
 *  list, a label and an icon. The ＋ Filter menu and the active-filter chips are built from
 *  these, so adding a future filter is one more entry here, never another toolbar control.
 *  `defaultValue` is the field's "off" value: a filter is active when its value differs from
 *  it, and clearing resets to it. Sort is deliberately not a dimension: it orders the list
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

/** One filter set from outside the app, this device's storage or a link, sanitized field by
 *  field: an unknown or outgrown value falls back to that field's default instead of poisoning
 *  the whole set. Every fallback shows MORE than the value it rejected, which is the direction
 *  an unreadable filter has to resolve in: a narrower list reads as the whole library. */
function sanitize(stored: Partial<Record<keyof QueueFilters, unknown>>): QueueFilters {
  const pick = <T,>(value: unknown, allowed: readonly T[], fallback: T): T =>
    allowed.includes(value as T) ? (value as T) : fallback;
  return {
    mediaType: pick(
      stored.mediaType,
      mediaFilters().map((f) => f.value),
      DEFAULT_FILTERS.mediaType,
    ),
    requested: pick(
      stored.requested,
      requestedFilters().map((f) => f.value),
      DEFAULT_FILTERS.requested,
    ),
    genre: typeof stored.genre === "string" ? stored.genre : DEFAULT_FILTERS.genre,
    library: typeof stored.library === "string" ? stored.library : DEFAULT_FILTERS.library,
    override: pick(
      stored.override,
      overrideFilters().map((f) => f.value),
      DEFAULT_FILTERS.override,
    ),
    sort: pick(
      stored.sort,
      sorts().map((s) => s.value),
      DEFAULT_FILTERS.sort,
    ),
    order: pick(stored.order, ["asc", "desc"] as const, DEFAULT_FILTERS.order),
  };
}

/** The remembered filters for one tab. */
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
  return sanitize((parsed ?? {}) as Partial<Record<keyof QueueFilters, unknown>>);
}

/** The filters that travel in a link, keyed on `QueueFilters` so a new dimension has to answer
 *  here before the app compiles. Sort is not one of them: it is not a dimension (it hides
 *  nothing), and it stays with the device that chose it, the same way `clearFilters` keeps it. */
const LINKED: Record<Exclude<keyof QueueFilters, "sort" | "order">, true> = {
  mediaType: true,
  library: true,
  requested: true,
  genre: true,
  override: true,
};
const LINKED_KEYS = Object.keys(LINKED) as (keyof typeof LINKED)[];

/** The queue's filters as a query string: `?q=` for the search, one parameter per active
 *  dimension, named for the dimension itself. A dimension at its default is left out, so an
 *  unfiltered lane has a bare path. */
export function filtersToQuery(search: string, filters: QueueFilters): string {
  const params = new URLSearchParams();
  if (search) params.set("q", search);
  for (const key of LINKED_KEYS) {
    if (filters[key] !== DEFAULT_FILTERS[key]) params.set(key, filters[key]);
  }
  const query = params.toString();
  return query ? `?${query}` : "";
}

/** The filter set a queue opens with, from the link it was opened by (`search` is
 *  `location.search`) or from this device's storage.
 *
 *  A link naming ANY filter brings the whole set: the dimensions it does not name are off,
 *  never filled in from what this browser had stored. Merging the two could only ever hide rows
 *  the sender was looking at, and a link is an explicit request that outranks a remembered
 *  preference. A link naming none leaves storage alone, so an ordinary visit is unchanged. */
export function initialFilters(verdict: string, search: string): QueueFilters {
  const stored = loadFilters(verdict);
  const params = new URLSearchParams(search);
  const named = LINKED_KEYS.filter((key) => params.has(key));
  if (named.length === 0) return stored;
  // A key given twice (`?requested=yes&requested=no`) takes neither. `get` would answer with the
  // first, which is a value the sender may never have meant and is narrower than the default on
  // every dimension here. Every other hostile shape in this function widens; so does this one.
  const one = (key: string) => (params.getAll(key).length > 1 ? null : params.get(key));
  return {
    ...sanitize(Object.fromEntries(named.map((key) => [key, one(key)]))),
    sort: stored.sort,
    order: stored.order,
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
