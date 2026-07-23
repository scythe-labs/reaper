// SPDX-License-Identifier: AGPL-3.0-or-later
//
// The review queue: what Reaper would reap, and what it would spare.
//
// Three lists, all first-class. A tool you can only ask "what would you reap?" is one you
// have to take on faith; being able to ask "what did you *spare*, and why?" is what makes
// the first answer worth anything. So Spared and Left alone are real tabs, not a debug
// affordance.
//
// Each item is a card you click to open the full reasoning. A movie is one card; every
// season of a show collapses under one show card you expand. The card leads with the *reason*
// Reaper judged it -- not a plot synopsis -- because on this screen the question is "why did
// it decide that?", not "what is this about?". The synopsis lives in the slide-out.

import { useInfiniteQuery, useMutation, useQuery } from "@tanstack/react-query";
import {
  type CSSProperties,
  type MouseEvent as ReactMouseEvent,
  type PointerEvent as ReactPointerEvent,
  type ReactNode,
  type RefObject,
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import { createPortal } from "react-dom";
import {
  api,
  type Candidate,
  type Chip,
  type GroupSeasonMark,
  type Override,
  type OverrideFilter,
  type RequestedFilter,
  type Run,
  type ShowStatus,
  type SortKey,
  type SortOrder,
  type Verdict,
} from "../api";
import { bytes, count, itemBytes, spareRemaining, totalBytes } from "../format";
import { useOverrideMutations } from "../useOverrideMutations";
import { ReapConfirm } from "./ReapConfirm";
import { ScytheGlyph } from "./ScytheGlyph";
import { chipWhy, CondemnedChip, OverrideChip, StatusChip } from "./StatusChip";

//: How many cards to *render* at a time. A tab can hold thousands, so we draw a screenful and
//  reveal more as you scroll -- keeping the DOM (and the lazy poster fetches) small.
const PAGE = 40;

//: How many candidates to *fetch* per request. The server pages the query (the review queue of
//  a real library runs to thousands of protected titles), and we pull the next page in as the
//  render window nears the end of what we have.
const FETCH_PAGE = 100;

const TABS: { verdict: Verdict; label: string; blurb: string; empty: string }[] = [
  {
    verdict: "condemn",
    label: "Condemned Souls",
    blurb: "Scored at or above your threshold, with nothing protecting them.",
    empty: "No souls are on the block.",
  },
  {
    verdict: "protect",
    label: "Sanctuary",
    blurb: "Something is protecting these souls. They stay, whatever they weigh.",
    empty: "No souls are being spared by a protection right now.",
  },
  {
    verdict: "abstain",
    label: "Limbo",
    blurb: "Below your threshold, or too little to go on. Reaper leaves them be.",
    empty: "No souls in Limbo.",
  },
];

const MEDIA_FILTERS: { value: string; label: string }[] = [
  { value: "", label: "Everything" },
  { value: "movie", label: "Movies" },
  { value: "season", label: "TV shows" },
];

const REQUESTED_FILTERS: { value: RequestedFilter; label: string }[] = [
  { value: "any", label: "Anyone" },
  { value: "yes", label: "Requested" },
  { value: "no", label: "Not requested" },
];

const OVERRIDE_FILTERS: { value: OverrideFilter; label: string }[] = [
  { value: "any", label: "Any override" },
  { value: "spare", label: "Spared by hand" },
  { value: "reap", label: "Reaped by hand" },
  { value: "none", label: "No override" },
];

const SORTS: { value: SortKey; label: string }[] = [
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
interface FilterDimension {
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

const filtersKey = (verdict: string) => `reaper.queue.filters.${verdict}`;

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

// --- little inline icons for the filter/sort pills ------------------------------------------

function LayersIcon() {
  return (
    <svg viewBox="0 0 16 16" width="14" height="14" fill="none" aria-hidden="true">
      <path d="M8 2l6 3-6 3-6-3 6-3z" stroke="currentColor" strokeWidth="1.3" strokeLinejoin="round" />
      <path d="M2 8l6 3 6-3M2 11l6 3 6-3" stroke="currentColor" strokeWidth="1.3" strokeLinejoin="round" />
    </svg>
  );
}
function FunnelIcon() {
  return (
    <svg viewBox="0 0 16 16" width="14" height="14" fill="none" aria-hidden="true">
      <path d="M2 3h12l-4.5 5.5V13L6.5 11V8.5L2 3z" stroke="currentColor" strokeWidth="1.3" strokeLinejoin="round" />
    </svg>
  );
}
function SortIcon() {
  return (
    <svg viewBox="0 0 16 16" width="14" height="14" fill="none" aria-hidden="true">
      <path d="M3 4h10M3 8h6M3 12h3" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" />
    </svg>
  );
}
function GenreIcon() {
  return (
    <svg viewBox="0 0 16 16" width="14" height="14" fill="none" aria-hidden="true">
      <path
        d="M8 2.2l1.5 4.3L13.8 8l-4.3 1.5L8 13.8 6.5 9.5 2.2 8l4.3-1.5z"
        stroke="currentColor"
        strokeWidth="1.3"
        strokeLinejoin="round"
      />
    </svg>
  );
}
function OverrideIcon() {
  return (
    <svg viewBox="0 0 16 16" width="14" height="14" fill="none" aria-hidden="true">
      <rect x="2" y="5" width="12" height="6" rx="3" stroke="currentColor" strokeWidth="1.3" />
      <circle cx="11" cy="8" r="1.8" stroke="currentColor" strokeWidth="1.3" />
    </svg>
  );
}
/** The Plex library (section) glyph -- a small shelf of spines, distinct from the media-type
 *  layers icon so a library never reads as a media type. Shared by the library filter and the
 *  library chip. */
function LibraryIcon() {
  return (
    <svg viewBox="0 0 16 16" width="14" height="14" fill="none" aria-hidden="true">
      <rect x="2.5" y="3" width="2.4" height="10" rx="0.6" stroke="currentColor" strokeWidth="1.2" />
      <rect x="5.8" y="3" width="2.4" height="10" rx="0.6" stroke="currentColor" strokeWidth="1.2" />
      <path d="M9.6 4l2.4.6-1.9 8.2-2.4-.6" stroke="currentColor" strokeWidth="1.2" strokeLinejoin="round" />
    </svg>
  );
}
function PlusIcon() {
  return (
    <svg viewBox="0 0 16 16" width="14" height="14" fill="none" aria-hidden="true">
      <path d="M8 3v10M3 8h10" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" />
    </svg>
  );
}
function CaretIcon() {
  return (
    <svg viewBox="0 0 16 16" width="11" height="11" fill="none" aria-hidden="true" className="fchip-caret">
      <path d="M4 6l4 4 4-4" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  );
}
function CheckIcon() {
  return (
    <svg viewBox="0 0 16 16" width="13" height="13" fill="none" aria-hidden="true">
      <path d="M3 8.5l3 3 7-7" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  );
}

/** The Plex library an item lives in, as a quiet neutral chip on the facts line -- the same
 *  placement and weight as the resolution badge, deliberately not a verdict color. Hidden when
 *  the library is unknown (unmatched, or a row from before this shipped). Shared by movie and
 *  show cards and both info panels, so movies and seasons read the same. */
export function LibraryChip({ library }: { library: string | null }) {
  if (!library) return null;
  return (
    <span className="lib-chip" title={`Plex library: ${library}`}>
      <LibraryIcon />
      {library}
    </span>
  );
}

/** A labeled dropdown pill with a leading icon -- the filter/sort control shape. */
function Pill({
  icon,
  value,
  onChange,
  children,
  title,
}: {
  icon: ReactNode;
  value: string;
  onChange: (value: string) => void;
  children: ReactNode;
  title: string;
}) {
  return (
    <label className="pill" title={title}>
      <span className="pill-icon" aria-hidden="true">
        {icon}
      </span>
      {/* The name has to sit on the select itself: the label wraps only a hidden icon, so
          without this a screen reader reads a row of unnamed dropdowns. */}
      <select aria-label={title} value={value} onChange={(e) => onChange(e.target.value)}>
        {children}
      </select>
    </label>
  );
}

/** One active filter, as a chip that clears just that filter. The × is the button, so its
 *  label has to name what it stops filtering by: "Remove the genre filter", never a row of
 *  identical "×" controls. */
function FilterChip({
  label,
  clearLabel,
  onClear,
}: {
  label: ReactNode;
  clearLabel: string;
  onClear: () => void;
}) {
  return (
    <span className="filter-chip">
      {label}
      <button type="button" aria-label={clearLabel} onClick={onClear}>
        ×
      </button>
    </span>
  );
}

/** The dimmed backdrop behind a card. Tries the wide Plex art first and falls back to the
 *  poster when a title has no separate backdrop; a paired scrim keeps the text readable. */
function Backdrop({ posterUrl }: { posterUrl: string | null }) {
  const [src, setSrc] = useState(posterUrl ? `${posterUrl}?kind=art` : null);
  const fellBack = useRef(false);

  useEffect(() => {
    fellBack.current = false;
    setSrc(posterUrl ? `${posterUrl}?kind=art` : null);
  }, [posterUrl]);

  if (!src) return null;
  return (
    <>
      <img
        className="card-bg"
        src={src}
        alt=""
        aria-hidden="true"
        loading="lazy"
        onError={() => {
          if (!fellBack.current && posterUrl) {
            fellBack.current = true;
            setSrc(posterUrl);
          } else {
            setSrc(null);
          }
        }}
      />
      <div className="card-scrim" aria-hidden="true" />
    </>
  );
}

/** A poster thumbnail, with a themed fallback when there is no image (or it fails). */
export function Poster({ url, alt }: { url: string | null; alt: string }) {
  const [broken, setBroken] = useState(false);

  // Reset on a new url, exactly as Backdrop does. Without this the flag latches: one
  // failed image (a dropped session, a slow Plex) leaves the placeholder in place for
  // every later item this row is reused for, so the art never comes back until remount.
  useEffect(() => setBroken(false), [url]);

  if (!url || broken) {
    return (
      <div className="poster poster-empty" aria-hidden="true">
        <svg viewBox="0 0 24 24" width="20" height="20" fill="none">
          <path d="M4 5h16v14H4z" stroke="currentColor" strokeWidth="1.6" />
          <path d="M8 5v14M16 5v14M4 9h4M16 9h4M4 15h4M16 15h4" stroke="currentColor" strokeWidth="1.2" />
        </svg>
      </div>
    );
  }
  return <img className="poster" src={url} alt={alt} loading="lazy" onError={() => setBroken(true)} />;
}

/** Which color a score badge or strip square wears once a hand decision is in play.
 *  A hand SPARE, or a reap the engine honors, shows SOLID -- "you chose this" -- so the
 *  cell states the item's real fate, not just Reaper's first read. A reap the engine
 *  can't honor yet ("refused") reads DASHED RED, never solid: the ask is noted, the file is
 *  still held. Amber is no longer used here at all -- it means only "left for you to decide"
 *  (the abstain `status-look` chip). Anything untouched keeps its scan verdict. Shared by the
 *  score badge and the season strip so the two can never disagree with each other or with
 *  the row's chip. */
export type Fate = Verdict | "reap" | "spare" | "refused";
export function handFate(item: {
  verdict: Verdict;
  override: Override | null;
  override_effective: boolean | null;
}): Fate {
  if (item.override === "spare") return "spare";
  if (item.override === "reap") return item.override_effective === false ? "refused" : "reap";
  return item.verdict;
}

/** The score chip. Color carries the item's fate so it reads without the label. */
function Score({ item }: { item: Candidate }) {
  return (
    <span className={`score score-${handFate(item)}`} title={`Score ${item.score} of 100`}>
      {item.score}
    </span>
  );
}

/** The reap glyph: the brand scythe (see ScytheGlyph), shrunk into a reap ACTION -- the same
 *  drawing as the header mark, so the two never read as different icons. A heavier snath (5.5
 *  vs the header's 3.5) holds the shape's weight at button size, where the logo's own stroke
 *  would thin to a hairline. Only reap actions wear it -- close buttons keep ✕. */
function ScytheIcon() {
  return <ScytheGlyph className="scythe" width={13} height={13} strokeWidth={5.5} />;
}

/** The label the resolution badge wears: 4K, HD or SD. Null (no data) shows nothing. */
function resolutionLabel(value: string | null): string | null {
  if (!value) return null;
  if (value === "2160") return "4K";
  if (value === "1080" || value === "720") return "HD";
  return "SD";
}

function ResolutionBadge({ value }: { value: string | null }) {
  const label = resolutionLabel(value);
  if (!label) return null;
  const detail = value && value !== "sd" ? `${value}p` : null;
  return (
    <span className="res-badge" title="The file's resolution">
      {label}
      {detail && <span className="res-detail">&nbsp;{detail}</span>}
    </span>
  );
}

/** "5 years, 9 months" -> "5y 9m", the compact span the pill wears.
 *
 *  Only a unit that follows a number is shortened. The server also sends spans with no
 *  number in them ("less than a day"), and a bare unit rewrite would turn that one into
 *  "less than ad". */
export function compactSpan(text: string): string {
  return text
    .replace(/(\d+) years?/g, "$1y")
    .replace(/(\d+) months?/g, "$1m")
    .replace(/(\d+) days?/g, "$1d")
    .replace(/,/g, "");
}

/** The dormancy pill: how long the item has sat unwatched, in the shared amber tone. */
function DormantPill({ dormantFor }: { dormantFor: string | null }) {
  if (!dormantFor) return null;
  return (
    <span className="dormant-pill" title={`Not watched in ${dormantFor}`}>
      <svg viewBox="0 0 16 16" width="12" height="12" fill="none" aria-hidden="true">
        <circle cx="8" cy="8" r="6.2" stroke="currentColor" strokeWidth="1.4" />
        <path d="M8 4.6V8l2.4 1.4" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" />
      </svg>
      Not watched in {compactSpan(dormantFor)}
    </span>
  );
}

/** How long a plain Spare press keeps an item: the operator's General preference (0 = forever,
 *  N = N days). Read from the shared general-settings cache, so flipping it in Settings takes
 *  effect here without a reload. Unknown/error reads as 0 -- forever, the safe, unchanged
 *  default. One query key, deduped, so every control on screen costs one request. */
function useDefaultSpareDays(): number {
  const { data } = useQuery({ queryKey: ["general-settings"], queryFn: api.general });
  return data?.default_spare_days ?? 0;
}

/** A small clock, the dormancy pill's shape reused -- here in the spare's green to mean "kept,
 *  for now". It marks a TIMED spare, where ∞ marks a forever one. */
function ClockGlyph() {
  return (
    <svg viewBox="0 0 16 16" width="13" height="13" fill="none" aria-hidden="true">
      <circle cx="8" cy="8" r="6.2" stroke="currentColor" strokeWidth="1.4" />
      <path d="M8 4.6V8l2.4 1.4" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" />
    </svg>
  );
}

/** The Spare button's leading glyph: ∞ when a plain press keeps forever, the clock when it
 *  keeps for a set time -- so the button says what it will do before you open the menu. */
function SpareGlyph({ days }: { days: number }) {
  return days > 0 ? (
    <ClockGlyph />
  ) : (
    <span className="infinity" aria-hidden="true">
      ∞
    </span>
  );
}

function CaretDownGlyph() {
  return (
    <svg viewBox="0 0 16 16" width="11" height="11" fill="none" aria-hidden="true">
      <path
        d="M4 6l4 4 4-4"
        stroke="currentColor"
        strokeWidth="1.6"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  );
}

function PenGlyph() {
  return (
    <svg viewBox="0 0 16 16" width="13" height="13" fill="none" aria-hidden="true">
      <path
        d="M11 2.5l2.5 2.5L6 12.5 3 13l.5-3z"
        stroke="currentColor"
        strokeWidth="1.4"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  );
}

//: The quick day-lengths the Spare menu always offers, above Forever and a Custom entry. The
//  operator's own default is added to (and tagged in) this list when it is a different number.
const SPARE_PRESETS = [30, 90];

/** The hand-overrides, as a toggle: **Spare** (keep) and **Reap** (force onto the list). The
 *  active one is lit; clicking it again clears the override and lets Reaper judge the item
 *  again. Clicking the other switches. Stops the click from opening the panel.
 *
 *  Spare is a SPLIT button: the main press keeps the item for the operator's default length
 *  (Settings → General), and the chevron opens the other lengths -- a set number of days, or
 *  forever -- so a one-off choice is one click, never a settings trip. The leading glyph (∞ or
 *  a clock) says which the default is. On a spared item the chevron stays live, so the same
 *  menu extends or shortens the spare; the main button still toggles it off.
 *
 *  On the Condemned lane the item is already on the block, so Reap would change nothing:
 *  `hideReap` drops it there and leaves Spare (rescue) on its own. */
/** Where the Spare length menu is pinned (fixed, viewport coords). Always `left`, plus exactly
 *  one vertical anchor: `top` when it opens below the caret, `bottom` when it flips above -- the
 *  bottom anchor keeps it snug to the caret whatever the menu's rendered height. */
type MenuPos = { left: number; top?: number; bottom?: number };

export function OverrideControls({
  override,
  onSet,
  onClear,
  pending,
  hideReap = false,
}: {
  override: Override | null;
  onSet: (decision: Override, spareDays?: number) => void;
  onClear: () => void;
  pending: boolean;
  hideReap?: boolean;
}) {
  const defaultDays = useDefaultSpareDays();
  const [menuAt, setMenuAt] = useState<MenuPos | null>(null);
  const caretRef = useRef<HTMLButtonElement>(null);

  // The menu is position:fixed so the card's overflow:hidden can't clip it. Anchor it to the
  // chevron, right-aligned, and flip above when it would run off the bottom of the viewport.
  // Below: pin the menu's TOP under the caret. Above: pin its BOTTOM just over the caret with the
  // `bottom` property -- never a `top` computed from a guessed height, which floated the menu off
  // the button when the real menu was shorter than the guess. HEIGHT is only the flip decision's
  // upper bound now, not a coordinate.
  const toggleMenu = (e: ReactMouseEvent) => {
    e.stopPropagation();
    if (menuAt) {
      setMenuAt(null);
      return;
    }
    const btn = caretRef.current;
    if (!btn) return;
    const r = btn.getBoundingClientRect();
    const WIDTH = 224;
    const HEIGHT = 250;
    const left = Math.max(8, Math.min(r.right - WIDTH, window.innerWidth - WIDTH - 8));
    const below = r.bottom + HEIGHT <= window.innerHeight;
    setMenuAt(
      below ? { left, top: r.bottom + 4 } : { left, bottom: window.innerHeight - r.top + 4 },
    );
  };

  const clickSpare = (e: ReactMouseEvent) => {
    e.stopPropagation();
    override === "spare" ? onClear() : onSet("spare", defaultDays);
  };
  const clickReap = (e: ReactMouseEvent) => {
    e.stopPropagation();
    override === "reap" ? onClear() : onSet("reap");
  };

  return (
    <div
      className={`override-controls ${menuAt ? "menu-open" : ""}`}
      role="group"
      aria-label="Spare or reap this item"
      // The buttons activate on Enter/Space natively; stop the key from bubbling to a row or
      // card whose own handler calls preventDefault, which would cancel the button's
      // activation and open the panel instead (B-7). Mirrors the SeasonStrip square guard.
      onKeyDown={(e) => {
        if (e.key === "Enter" || e.key === " ") e.stopPropagation();
      }}
    >
      <span className="ov-split">
        <button
          type="button"
          className={`ov-btn ov-spare split-main ${override === "spare" ? "active" : ""}`}
          disabled={pending}
          aria-pressed={override === "spare"}
          onClick={clickSpare}
          title={
            override === "spare"
              ? "Spared. Click to let Reaper judge it again"
              : defaultDays > 0
                ? `Spare for ${defaultDays} days. Use the arrow for another length`
                : "Spare forever. Use the arrow for a set time"
          }
        >
          <SpareGlyph days={defaultDays} /> {override === "spare" ? "Spared" : "Spare"}
        </button>
        <button
          ref={caretRef}
          type="button"
          className={`ov-btn ov-spare split-caret ${override === "spare" ? "active" : ""}`}
          disabled={pending}
          aria-haspopup="menu"
          aria-expanded={menuAt !== null}
          aria-label="Choose how long to keep it"
          onClick={toggleMenu}
        >
          <CaretDownGlyph />
        </button>
      </span>
      {menuAt && (
        <SpareMenu
          at={menuAt}
          defaultDays={defaultDays}
          triggerRef={caretRef}
          onPick={(spareDays) => {
            setMenuAt(null);
            onSet("spare", spareDays);
          }}
          onClose={() => setMenuAt(null)}
        />
      )}
      {!hideReap && (
        <button
          type="button"
          className={`ov-btn ov-reap ${override === "reap" ? "active" : ""}`}
          disabled={pending}
          aria-pressed={override === "reap"}
          onClick={clickReap}
          title={
            override === "reap"
              ? "Marked for reaping. Click to undo"
              : "Force this onto the reap list"
          }
        >
          <ScytheIcon /> {override === "reap" ? "Reaping" : "Reap"}
        </button>
      )}
    </div>
  );
}

/** The length menu behind the Spare chevron: quick day-presets, Forever, and a Custom entry
 *  that expands to a days box. The operator's default is tagged. Picking a length spares the
 *  item at once -- the menu is the action, not a form. Portaled to <body> and rendered
 *  position:fixed at `at`: the card clips its overflow AND its `.card-side` is a z-index:2
 *  stacking context (`.card > *`), which a fixed child alone can't escape -- so a later card's
 *  score badge, spare button, or tooltip paints over an in-card menu. The portal lifts it to the
 *  root stacking context so its own z-index wins. Closed on an outside click, Escape, or a
 *  scroll that would strand it off its anchor. */
function SpareMenu({
  at,
  defaultDays,
  triggerRef,
  onPick,
  onClose,
}: {
  at: MenuPos;
  defaultDays: number;
  triggerRef: RefObject<HTMLButtonElement | null>;
  onPick: (days: number) => void;
  onClose: () => void;
}) {
  const ref = useRef<HTMLDivElement>(null);
  const [custom, setCustom] = useState(false);
  // Held as free text so the box types and clears naturally; clamped to [1, 3650] only when
  // Spare is pressed, never mid-keystroke (which would snap a half-typed number).
  const [customText, setCustomText] = useState(String(defaultDays > 0 ? defaultDays : 30));
  const spareCustom = () =>
    onPick(Math.max(1, Math.min(3650, Math.floor(Number(customText) || 1))));

  useEffect(() => {
    const onDown = (e: MouseEvent) => {
      const t = e.target as Node;
      // The chevron is its own toggle; leave it out of the outside-close so a click there
      // doesn't close-then-reopen the menu.
      if (!ref.current?.contains(t) && !triggerRef.current?.contains(t)) onClose();
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    const onScroll = () => onClose();
    document.addEventListener("mousedown", onDown);
    document.addEventListener("keydown", onKey);
    window.addEventListener("scroll", onScroll, true);
    return () => {
      document.removeEventListener("mousedown", onDown);
      document.removeEventListener("keydown", onKey);
      window.removeEventListener("scroll", onScroll, true);
    };
  }, [onClose, triggerRef]);

  // The day-rows: the presets, plus the operator's default when it is a custom number not
  // already among them, sorted so the ladder reads low to high.
  const dayRows = Array.from(
    new Set([...(defaultDays > 0 ? [defaultDays] : []), ...SPARE_PRESETS]),
  ).sort((a, b) => a - b);

  return createPortal(
    <div
      ref={ref}
      className="dur-menu"
      role="menu"
      aria-label="Spare this item for"
      style={{
        position: "fixed",
        left: at.left,
        ...(at.top !== undefined ? { top: at.top } : { bottom: at.bottom }),
      }}
      onClick={(e) => e.stopPropagation()}
    >
      <div className="dur-head">Spare for…</div>
      {dayRows.map((d) => (
        <button key={d} type="button" role="menuitem" className="dur-mi" onClick={() => onPick(d)}>
          <span className="mi-glyph">
            <ClockGlyph />
          </span>
          <span className="mi-label">{d} days</span>
          {d === defaultDays && <span className="mi-tag">Default</span>}
        </button>
      ))}
      <button type="button" role="menuitem" className="dur-mi" onClick={() => onPick(0)}>
        <span className="mi-glyph">
          <span className="infinity" aria-hidden="true">
            ∞
          </span>
        </span>
        <span className="mi-label">Forever</span>
        {defaultDays === 0 && <span className="mi-tag">Default</span>}
      </button>
      <div className="dur-div" />
      {custom ? (
        <div className="dur-custom">
          <span className="qty qty-narrow">
            <input
              type="number"
              min={1}
              max={3650}
              value={customText}
              autoFocus
              aria-label="Custom spare length in days"
              onChange={(e) => setCustomText(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter") spareCustom();
              }}
            />
            <span className="qty-suffix" aria-hidden="true">
              days
            </span>
          </span>
          <button type="button" className="dur-spare-go" onClick={spareCustom}>
            Spare
          </button>
        </div>
      ) : (
        <button type="button" role="menuitem" className="dur-mi" onClick={() => setCustom(true)}>
          <span className="mi-glyph mi-pen">
            <PenGlyph />
          </span>
          <span className="mi-label">Custom length…</span>
        </button>
      )}
    </div>,
    document.body,
  );
}

/** The note beside a season's Spare/Reap when a *whole-show* decision is what keeps or
 *  reaps it -- so the operator knows the season control toggles only the season's OWN
 *  decision, not the show's. Its wording turns on how the season's own decision relates to
 *  the show's: absent (the show decides), the same (clearing this one changes nothing), or
 *  opposite (the season's own decision wins). Renders nothing when no show decision covers it,
 *  so a movie or an untouched-show season shows no note. The glyph tracks the item's REAL
 *  fate, so the note never contradicts the row's chip. */
export function KeptByShowNote({
  own,
  showOverride,
  effective,
  className = "",
}: {
  own: Override | null;
  showOverride: Override | null;
  /** The row's ``override_effective``: false means a reap the engine can't honor yet (held). */
  effective: boolean | null;
  className?: string;
}) {
  if (!showOverride) return null;
  const fate = own ?? showOverride; // an item's own decision wins over its show's
  // A reap the engine can't honor yet (streaming now, a structural gate) is HELD, not done, so
  // the note must never promise removal for it -- it matches the chip's "kept for now" (U-1).
  const heldReap = fate === "reap" && effective === false;
  let body;
  if (!own) {
    body =
      showOverride === "spare" ? (
        <>
          <b>The whole show is spared</b>, so this season is kept. Undo it on the show.
        </>
      ) : heldReap ? (
        <>
          <b>The whole show is set to reap</b>, but this season is <b>kept for now</b>. Undo it
          on the show.
        </>
      ) : (
        <>
          <b>The whole show is set to reap</b>, so this season will be removed. Undo it on the
          show.
        </>
      );
  } else if (own === showOverride) {
    body =
      showOverride === "spare" ? (
        <>
          The whole show is <b>also spared</b>, so clearing this one won't remove it.
        </>
      ) : (
        <>
          The whole show is <b>also set to reap</b>, so clearing this one won't keep it.
        </>
      );
  } else {
    body =
      own === "reap" ? (
        heldReap ? (
          <>
            You reaped this season, but it is <b>kept for now</b>, even though the whole show is
            spared.
          </>
        ) : (
          <>
            You reaped this season, so it <b>will be removed</b> even though the whole show is
            spared.
          </>
        )
      ) : (
        <>
          You spared this season, so it <b>stays</b> even though the whole show is set to reap.
        </>
      );
  }
  return (
    <p className={`kept-note kept-${fate} ${className}`.trim()}>
      <span className="kept-note-mark" aria-hidden="true">
        {fate === "spare" ? <span className="mk-inf">∞</span> : <ScytheIcon />}
      </span>
      <span>{body}</span>
    </p>
  );
}

/** The resting decision marker: when a hand override is in force, the card rests as a small
 *  icon of that decision (∞ spared, scythe reaped) where the buttons sit, bottom-right. The
 *  hover rules fade it out as the buttons arrive, so the two never show together. Decorative:
 *  the same decision is named by the card's override chip and by the buttons themselves. */
function OverrideMark({
  override,
  spareExpiresAt = null,
}: {
  override: Override | null;
  /** For a spare, when it stops keeping the item (ISO), or null for forever. A forever spare
   *  rests as ∞; a timed one rests as the clock plus its days left ("27d"). */
  spareExpiresAt?: string | null;
}) {
  if (!override) return null;
  if (override !== "spare") {
    return (
      <span className="override-mark reap" aria-hidden="true">
        <ScytheIcon />
      </span>
    );
  }
  const remaining = spareRemaining(spareExpiresAt);
  return (
    <span className="override-mark spare" aria-hidden="true">
      {remaining.forever ? (
        <span className="mk-inf">∞</span>
      ) : (
        <>
          <ClockGlyph />
          <span className="mk-count">{remaining.short}</span>
        </>
      )}
    </span>
  );
}

/** How a card participates in Select mode. When ``selectMode`` is off these are inert and the
 *  card behaves normally (click opens the why-panel); when on, the whole card is a selection
 *  target -- press to toggle, drag across to paint a run. */
type CardSelect = {
  selectMode: boolean;
  isSelected: boolean;
  onSelectDown: (key: string, e: ReactPointerEvent) => void;
  onSelectEnter: (key: string) => void;
  // Keyboard activation (Enter/Space) toggles a single card *without* arming a drag. A key
  // press emits no pointerup, so routing it through onSelectDown would leave the drag mode
  // stuck and paint every card a later mouse-hover crossed.
  onSelectToggle: (key: string) => void;
};

/** The selection tick a card wears in Select mode: an empty ring until picked, a filled check
 *  once it is. Replaces the raw checkbox -- it reads as part of the card, not bolted on. */
function SelectTick({ selected }: { selected: boolean }) {
  return (
    <span className={`select-tick ${selected ? "on" : ""}`} aria-hidden="true">
      <svg viewBox="0 0 16 16" width="11" height="11">
        <path
          d="M3.5 8.5l3 3 6-7"
          fill="none"
          stroke="currentColor"
          strokeWidth="2.3"
          strokeLinecap="round"
          strokeLinejoin="round"
        />
      </svg>
    </span>
  );
}

function CheckSquareIcon() {
  return (
    <svg viewBox="0 0 16 16" width="14" height="14" fill="none" aria-hidden="true">
      <rect x="2" y="2" width="12" height="12" rx="3" stroke="currentColor" strokeWidth="1.4" />
      <path
        d="M5 8.2l2 2 4-4.4"
        stroke="currentColor"
        strokeWidth="1.6"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  );
}

/** Whether the row a card leads with is on the block. One expression for both card
 *  shapes: a show card reads its first (highest-scoring) season, a movie card reads
 *  itself, and neither may drift into asking the question a different way. */
function isCondemned(item: Candidate): boolean {
  return item.verdict === "condemn";
}

/** One status line per card. Condemned leads with the amber dormancy pill, and the reason
 *  paragraph stands down WHENEVER the pill is present -- two status lines is noise whatever
 *  the reason says; the full sentences live in the panel. Sanctuary and Limbo wear their
 *  single short chip. */
/** Whether an item with no size is actually being kept out of plans right now.
 *
 *  Only true while the allowance is off, which is its default. Above zero these items
 *  are reapable, and a card still promising "held back" would be telling the owner the
 *  opposite of what the plan will do. One shared query key, so this costs one request no
 *  matter how many cards ask. Unknown or failed reads answer TRUE: claiming an item is
 *  kept is the safe thing to be wrong about here, since the size cell already says the
 *  size is unknown either way. */
export function useHoldsBackUnmeasured(): boolean {
  const { data } = useQuery({ queryKey: ["profile"], queryFn: api.profile });
  return (data?.max_unmeasured_per_run ?? 0) === 0;
}

function CardStatusLine({
  condemned,
  dormantFor,
  reason,
  chip,
  unmeasured = false,
}: {
  condemned: boolean;
  dormantFor: string | null;
  reason: string | null;
  chip: Chip | null;
  /** No size. Said on the card because this is where the owner is already looking, and a
   *  count on the plan screen cannot name the item. */
  unmeasured?: boolean;
}) {
  const heldBack = useHoldsBackUnmeasured() && unmeasured;
  if (!condemned) return <StatusChip chip={chip} />;
  return (
    <>
      <DormantPill dormantFor={dormantFor} />
      {heldBack && <p className="card-reason">Held back: size unknown</p>}
      {reason && !dormantFor && !heldBack && <p className="card-reason">{reason}</p>}
    </>
  );
}

/** Whether a show-level reap actually takes anywhere: true when any season's reap is honored,
 *  false when every one is refused, undefined outside a reap override. Feeds the whole-show
 *  override chip's `effective` flag. Judges over the WHOLE show (`group_seasons`), every lane,
 *  never the tab-filtered page. */
function groupReapEffective(
  items: ReadonlyArray<{ override: Override | null; override_effective: boolean | null }>,
): boolean | undefined {
  const reaped = items.filter((s) => s.override === "reap");
  if (reaped.length === 0) return undefined;
  return reaped.some((s) => s.override_effective !== false);
}

/** Whether a whole-show Reap would change nothing -- the show analogue of rule 48's
 *  already-condemned test. It decides `hideReap` for a show on both the card and the panel,
 *  so the test lives here once rather than being reimplemented at each surface.
 *
 *  A movie on the Condemned lane is atomically condemned, so its own Reap is a no-op and is
 *  hidden. A show is not atomic: it is on that lane because SOME season is condemned, and a
 *  whole-show Reap still takes the seasons the scan kept. So Reap only falls away once every
 *  season is already headed for removal -- scan-condemned and not hand-spared. A show holding
 *  any hand reap keeps Reap too, so that decision stays toggleable back off.
 *
 *  It must run over the WHOLE show, every lane -- the panel already passes `group.seasons`.
 *  The card once passed only the seasons on the current tab, so on the Condemned lane it saw
 *  a set that was all-condemned by construction and wrongly dropped Reap, hiding the one
 *  control that reaps the show's kept seasons. So the parameter takes the strip-mark shape
 *  too (`group_seasons`), not just a full `Candidate`. */
export function showReapIsNoop(
  seasons: ReadonlyArray<{ verdict: Verdict; override: Override | null }>,
): boolean {
  if (seasons.some((s) => s.override === "reap")) return false;
  return seasons.every((s) => s.verdict === "condemn" && s.override !== "spare");
}

type Group = {
  key: string;
  title: string;
  year: number | null;
  poster: string | null;
  reason: string | null;
  requestedBy: string | null;
  /** The first (highest-scoring) season's dormancy span, like `reason` -- the show
   *  card's pill leads with its most condemned season. */
  dormantFor: string | null;
  /** The Plex library the item lives in -- a show's is shared by all its seasons, so the
   *  first season sets it for the whole card. Null when unknown; the chip is hidden. */
  library: string | null;
  items: Candidate[];
  isShow: boolean;
};

/** The override key a card acts on: a show's group key, or a movie's own key. One
 *  expression, so a bulk action can never pick a card by a different name than the one
 *  the card itself renders under. */
function groupKeyOf(group: Group): string {
  return group.isShow ? group.key : group.items[0]!.media_key;
}

/** Fold the flat candidate list into cards: a movie is its own card; every season of a
 *  show collapses under one show card. Order is preserved, so a group sits where its
 *  first (highest-scoring) member would. */
function toGroups(items: Candidate[]): Group[] {
  const groups: Group[] = [];
  const index = new Map<string, Group>();
  for (const item of items) {
    if (item.group_key) {
      let g = index.get(item.group_key);
      if (!g) {
        g = {
          key: item.group_key,
          title: item.group_title ?? item.title,
          year: item.year,
          poster: item.poster_url,
          reason: item.reason,
          requestedBy: item.requested_by,
          dormantFor: item.dormant_for,
          library: item.library,
          items: [],
          isShow: true,
        };
        index.set(item.group_key, g);
        groups.push(g);
      }
      g.items.push(item);
    } else {
      groups.push({
        key: item.media_key,
        title: item.title,
        year: item.year,
        poster: item.poster_url,
        reason: item.reason,
        requestedBy: item.requested_by,
        dormantFor: item.dormant_for,
        library: item.library,
        items: [item],
        isShow: false,
      });
    }
  }
  return groups;
}

function RequestedChip({ who }: { who: string | null }) {
  if (!who) return null;
  return (
    <span className="chip chip-requested" title="Someone asked for this through Seerr">
      Requested by {who}
    </span>
  );
}

/** What each show status is called on screen, and what it says out of context.
 *
 *  "continuing" reads as "Still going", never "Continuing": that state also covers a show
 *  that hasn't started airing yet, and the softer wording claims only what the server
 *  actually told us. A chip beside a title is ambiguous to a screen reader ("Ended" what?),
 *  so each one names its subject in the long form. */
const SHOW_STATUS_TEXT: Record<ShowStatus, { label: string; about: string }> = {
  ended: { label: "Ended", about: "This show has ended" },
  continuing: { label: "Still going", about: "This show hasn't ended, so more may still come" },
  unknown: {
    label: "Status unknown",
    about: "We couldn't check whether this show has ended",
  },
};

/** Whether the show has finished, as the one chip both the card and the why-panel wear.
 *
 *  The card passes `quiet`, which drops the "still going" chip: on the card, no chip means
 *  the show is still going. That is the common case, and leaving it unmarked keeps the row
 *  calm so the two states worth reading stand out. The panel is where you go to find out,
 *  so it names all three.
 *
 *  A status the server never reported wears the amber "we couldn't check" chip in both
 *  places. It is not a quiet no and not a yes: we could not look, and the chip has to say
 *  so, the same way an unchecked protection does. Shared with the why-panel. */
export function ShowStatusChip({
  status,
  quiet = false,
}: {
  status: ShowStatus | null;
  quiet?: boolean;
}) {
  // Null is a movie, or a row stored before the field existed. Neither is a claim about
  // any show, so neither draws anything.
  if (status === null) return null;
  if (status === "continuing" && quiet) return null;
  const { label, about } = SHOW_STATUS_TEXT[status];
  // role="img": a plain <span> has no role, and ARIA does not let a generic element carry
  // a name, so an aria-label on one is dropped and a screen reader reads the bare "Ended".
  // A role that supports naming makes the long form the announced text; `title` keeps the
  // same sentence as the mouse tooltip.
  return (
    <span
      className={status === "unknown" ? "chip chip-unchecked" : "chip"}
      role="img"
      title={about}
      aria-label={about}
    >
      {label}
    </span>
  );
}

/** What a strip square's tooltip says about its season's lane. */
const MARK_LABELS: Record<string, string> = {
  condemn: "would be removed",
  protect: "kept",
  abstain: "left alone",
};

/** The season strip: one small square per season of the show, colored by its fate
 *  across the WHOLE snapshot -- so "which seasons stay and which go" reads at a glance
 *  without expanding anything. A hand decision paints its square solid; a reap the engine
 *  can't honor yet reads dashed red and carries a small scythe corner-mark -- so it stays
 *  clearly YOURS (the way a movie card's resting scythe does) yet never the solid red of a
 *  removal, and never the plain condemned outline beside it. The tooltip says both facts.
 *  Each square opens that season's own reasoning (the show card itself opens the show). */
function SeasonStrip({
  marks,
  onOpen,
}: {
  marks: GroupSeasonMark[];
  onOpen: (id: number) => void;
}) {
  return (
    <div className="season-strip">
      {marks.map((mark, i) => {
        const name = mark.season === 0 ? "Specials" : `Season ${mark.season ?? "?"}`;
        const fate = handFate(mark);
        const reapRefused = fate === "refused";
        // The base square is the scan verdict; a hand decision paints over it. A reap the
        // engine can't honor yet reads dashed red (noted, but the file is held), never the
        // solid red of a removal, and carries the scythe mark below so it never blends into
        // the plain condemned outline (`.strip-ov-reap-refused` sits after `.strip-abstain`
        // in index.css and wins).
        const handClass =
          fate === "spare"
            ? " strip-ov-spare"
            : fate === "reap"
              ? " strip-ov-reap"
              : fate === "refused"
                ? " strip-ov-reap-refused"
                : "";
        const overrideNote =
          mark.override === "spare"
            ? ", you spared it"
            : reapRefused
              ? ", reap requested but it is kept for now"
              : mark.override === "reap"
                ? ", you reaped it by hand"
                : "";
        const lane = MARK_LABELS[mark.verdict] ?? mark.verdict;
        return (
          <button
            type="button"
            // Season numbers are unique within one show; an unnumbered row falls back
            // to its position, stable within the response.
            key={mark.season ?? `unnumbered-${i}`}
            className={`strip-sq strip-${mark.verdict}${handClass}`}
            title={`${name}: ${lane}${overrideNote}. Open for its full reasoning.`}
            aria-label={`Open ${name}, ${lane}`}
            onClick={(e) => {
              // The whole card head opens the show; a square opens just its season.
              e.stopPropagation();
              onOpen(mark.id);
            }}
            onKeyDown={(e) => {
              // The card head owns Enter/Space for "open the show". Keep a focused
              // square from bubbling into it; the button fires its own click natively.
              if (e.key === "Enter" || e.key === " ") e.stopPropagation();
            }}
          >
            {mark.season === 0 ? "SP" : (mark.season ?? "·")}
            {/* A held reap keeps the scythe so the square still reads as YOUR ask, the way a
                movie card's resting OverrideMark does -- the strip has no such mark of its own. */}
            {reapRefused && (
              <span className="strip-mark" aria-hidden="true">
                <ScytheIcon />
              </span>
            )}
          </button>
        );
      })}
    </div>
  );
}

/** The season count as the expand control: a small labeled pill with a chevron, in the
 *  meta line. The card itself opens the show's information panel, so expanding is its
 *  own, clearly-labeled target rather than the whole-card click. */
function SeasonExpander({
  count: seasonCount,
  open,
  onToggle,
}: {
  count: number;
  open: boolean;
  onToggle: () => void;
}) {
  return (
    <button
      type="button"
      className="season-expander"
      aria-expanded={open}
      title={open ? "Hide the season list" : "Show every season and where it stands"}
      onClick={(e) => {
        e.stopPropagation();
        onToggle();
      }}
    >
      <svg
        className={`chevron ${open ? "open" : ""}`}
        viewBox="0 0 12 12"
        width="11"
        height="11"
        aria-hidden="true"
      >
        <path d="M4 2l4 4-4 4" fill="none" stroke="currentColor" strokeWidth="1.8" />
      </svg>
      {seasonCount === 1 ? "1 season" : `${seasonCount} seasons`}
    </button>
  );
}

/** "Show Title · Season 3" -> "Season 3". The second form strips the pre-middot
 *  separator still present in titles frozen into older snapshots. */
function seasonName(title: string, showTitle: string): string {
  return title.replace(`${showTitle} · `, "").replace(`${showTitle} — `, "");
}

/** The expanded show: EVERY season in the latest snapshot, whatever its lane, so kept
 *  and condemned read side by side. Every row is actable from here, not just the ones on
 *  the tab you opened: each carries its own Spare/Reap, judged by that season's OWN verdict
 *  (rule 51), so an under-scored season inside a condemned show can be decided in place
 *  instead of only from Limbo. Clicking any row still opens its full reasoning. */
function SeasonList({
  groupKey,
  selectedId,
  onOpen,
  onSet,
  onClear,
  pending,
}: {
  groupKey: string;
  selectedId: number | null;
  onOpen: (id: number) => void;
  onSet: (key: string, decision: Override, spareDays?: number) => void;
  onClear: (key: string) => void;
  pending: boolean;
}) {
  const { data, isPending, error } = useQuery({
    queryKey: ["group", groupKey],
    queryFn: () => api.group(groupKey),
  });

  // The list is an always-visible surface once expanded: say "loading" and "failed"
  // out loud rather than rendering nothing under an open chevron.
  if (isPending) {
    return <p className="season-list-note muted">Loading seasons…</p>;
  }
  if (error || !data) {
    return (
      <p className="season-list-note error">
        Couldn't load the seasons. Collapse and expand to try again.
      </p>
    );
  }

  // The Reap column is reserved only when some season in this show can actually show a Reap
  // (any non-condemned season). A show that is condemned top to bottom shows Spare alone on
  // every row and leaves no empty Reap slot, so the size sits flush without a gap. Every
  // row in one list uses the same width, so Spare and Reap line up straight down it.
  const anyReapable = data.seasons.some((s) => !isCondemned(s));
  return (
    <ul
      className="season-list"
      style={
        {
          // Both widths derive from --ov-btn-w / --ov-btn-gap (index.css), so a button-width
          // change lands in one place and the columns can't drift (H-1, rule 16).
          "--btns": anyReapable
            ? "calc(2 * var(--ov-btn-w) + var(--ov-btn-gap))"
            : "var(--ov-btn-w)",
        } as CSSProperties
      }
    >
      {data.seasons.map((season) => (
        <li
          key={season.id}
          className={`season-row clickable ${
            season.override === "spare"
              ? "card-spared"
              : season.override === "reap"
                ? "card-reaped"
                : ""
          } ${season.id === selectedId ? "card-selected" : ""}`}
          onClick={() => onOpen(season.id)}
          role="button"
          tabIndex={0}
          onKeyDown={(e) => {
            if (e.key === "Enter" || e.key === " ") {
              e.preventDefault();
              onOpen(season.id);
            }
          }}
        >
          <Score item={season} />
          <span className="season-title">
            {seasonName(season.title, data.title)}
            {/* The owner's decision replaces the scan chip: one pill, the truth. */}
            {season.override !== null ? (
              <OverrideChip
                override={season.override}
                effective={season.override_effective}
                keptWhy={chipWhy(season.chip)}
                spareExpiresAt={season.spare_expires_at}
              />
            ) : season.verdict === "condemn" ? (
              <CondemnedChip />
            ) : (
              <StatusChip chip={season.chip} />
            )}
          </span>
          {/* The control toggles the season's OWN decision (override_own), never the one it
              inherits from its show (rule 50). Reap is dropped only when this season's own
              verdict is condemn -- reaping it changes nothing (rule 51); Spare is never
              dropped. The note below says when the whole show is what keeps it. */}
          <OverrideControls
            override={season.override_own}
            onSet={(d, sd) => onSet(season.media_key, d, sd)}
            onClear={() => onClear(season.media_key)}
            pending={pending}
            hideReap={isCondemned(season)}
          />
          <span className="season-size num">{itemBytes(season.size_bytes)}</span>
          <KeptByShowNote
            own={season.override_own}
            showOverride={season.show_override}
            effective={season.override_effective}
            className="season-row-note"
          />
        </li>
      ))}
    </ul>
  );
}

function MovieCard({
  item,
  selected,
  select,
  onOpen,
  onSet,
  onClear,
  pending,
  hideReap,
}: {
  item: Candidate;
  selected: boolean;
  select: CardSelect;
  onOpen: (id: number) => void;
  onSet: (key: string, decision: Override, spareDays?: number) => void;
  onClear: (key: string) => void;
  pending: boolean;
  hideReap: boolean;
}) {
  const state = item.override === "spare" ? "card-spared" : item.override === "reap" ? "card-reaped" : "";
  const { selectMode, isSelected } = select;
  return (
    <article
      className={`card clickable ${state} ${selected ? "card-selected" : ""} ${
        selectMode ? "card-select" : ""
      } ${isSelected ? "card-picked" : ""}`}
      onClick={() => !selectMode && onOpen(item.id)}
      onPointerDown={(e) => selectMode && select.onSelectDown(item.media_key, e)}
      onPointerEnter={() => selectMode && select.onSelectEnter(item.media_key)}
      role="button"
      tabIndex={0}
      aria-pressed={selectMode ? isSelected : undefined}
      aria-label={selectMode ? `Select ${item.title}` : `Why ${item.title} scored ${item.score}`}
      onKeyDown={(e) => {
        if (e.key === "Enter" || e.key === " ") {
          e.preventDefault();
          selectMode ? select.onSelectToggle(item.media_key) : onOpen(item.id);
        }
      }}
    >
      <Backdrop posterUrl={item.poster_url} />
      {selectMode && (
        <div className="card-tick-col">
          <SelectTick selected={isSelected} />
        </div>
      )}
      <Poster url={item.poster_url} alt={item.title} />
      <div className="card-body">
        <div className="card-title-row">
          <h3 className="card-title">
            {item.title}
            {item.year && <span className="card-year"> {item.year}</span>}
          </h3>
          <OverrideChip
            override={item.override}
            effective={item.override_effective}
            keptWhy={chipWhy(item.chip)}
            spareExpiresAt={item.spare_expires_at}
          />
        </div>
        {/* The type chip lives on the meta line, not the title row, so a long title
            never fights a chip for space and the year stays glued to the title. */}
        <div className="card-meta">
          <span className="chip chip-movie">Movie</span>
          <LibraryChip library={item.library} />
          <span>{itemBytes(item.size_bytes)}</span>
          <ResolutionBadge value={item.video_resolution} />
          <RequestedChip who={item.requested_by} />
        </div>
        <CardStatusLine
          condemned={isCondemned(item)}
          dormantFor={item.dormant_for}
          reason={item.reason}
          chip={item.chip}
          unmeasured={item.size_bytes === null}
        />
      </div>
      <div className="card-side">
        <Score item={item} />
        {/* In Select mode the whole card is a target, so the inline buttons stand down -- the
            bulk bar carries the actions instead. Otherwise the decision icon rests here until
            you hover, when the buttons take its place. */}
        {!selectMode && (
          <>
            <OverrideMark override={item.override} spareExpiresAt={item.spare_expires_at} />
            {/* A movie has no show to inherit from, so its own decision is its effective one;
                the control toggles override_own for the same contract every row now follows. */}
            <OverrideControls
              override={item.override_own}
              onSet={(d, sd) => onSet(item.media_key, d, sd)}
              onClear={() => onClear(item.media_key)}
              pending={pending}
              hideReap={hideReap}
            />
          </>
        )}
      </div>
    </article>
  );
}

function ShowCard({
  group,
  defaultOpen,
  selectedId,
  selectedGroupKey,
  select,
  onOpen,
  onOpenGroup,
  onSet,
  onClear,
  pending,
}: {
  group: Group;
  /** Whether the season list starts expanded, from the operator's General preference.
   *  Only the STARTING state -- a click on this card's season pill still wins. */
  defaultOpen: boolean;
  selectedId: number | null;
  selectedGroupKey: string | null;
  select: CardSelect;
  onOpen: (id: number) => void;
  onOpenGroup: (key: string) => void;
  onSet: (key: string, decision: Override, spareDays?: number) => void;
  onClear: (key: string) => void;
  pending: boolean;
}) {
  const [open, setOpen] = useState(defaultOpen);
  // The operator's "expand by default" preference may resolve a tick after this card first
  // mounts (it rides its own query), so apply it once known -- but never stomp a toggle the
  // user already made on THIS card. `touched` is the cross-render "the user has decided" flag
  // (rule 19); once set, the preference stops seeding this card's state.
  const touched = useRef(false);
  useEffect(() => {
    if (!touched.current) setOpen(defaultOpen);
  }, [defaultOpen]);
  const first = group.items[0]!;
  // The whole show's shape, across every lane of the snapshot -- what the strip and the
  // season count draw from. Null only on rows from before this field existed.
  const marks = first.group_seasons;
  // Whether the show has finished, from the first season row that carries it. This is a
  // fact about the series, stamped onto every one of its seasons in the same scan, so the
  // rows of one show cannot disagree; skipping the empty ones keeps a snapshot taken
  // before this field existed from blanking a show whose other rows do have it. Same
  // rollup the server does for the show panel.
  const showStatus = group.items.find((s) => s.show_status)?.show_status ?? null;
  const totalSeasons = marks?.length ?? group.items.length;
  // Sums over what is known, with the unmeasured counted separately. A `?? 0` in either
  // reduce would restore exactly the silent under-count this replaced: the total would
  // read low and nothing would say why.
  const wholeShowBytes = marks?.reduce((sum, m) => sum + (m.size_bytes ?? 0), 0) ?? null;
  const wholeShowUnknown = marks?.reduce((n, m) => n + (m.size_bytes === null ? 1 : 0), 0) ?? 0;
  const fetchedSize = group.items.reduce((sum, s) => sum + (s.size_bytes ?? 0), 0);
  const fetchedUnknown = group.items.reduce((n, s) => n + (s.size_bytes === null ? 1 : 0), 0);
  // On the "Condemned" tab the byte figure must state what "Reap now" will actually
  // plan: the server's whole-snapshot totals (every condemned season minus hand-spares),
  // never a sum over the fetched pages, which on a long sorted list can hold only some
  // of a show's seasons. Other tabs describe the whole show, which the strip shows.
  const isReapTab = isCondemned(first);
  const condemnedCount = first.group_condemned_count ?? group.items.length;
  const condemnedBytes = first.group_condemned_bytes ?? fetchedSize;
  const condemnedUnknown = first.group_unknown_size ?? fetchedUnknown;
  // The whole show's marks, across every lane -- `marks`, not the tab-filtered `group.items`
  // (on the Condemned lane that holds only this show's reaped/condemned seasons). Used for the
  // strip and for whether a whole-show reap would change anything.
  const showSeasons = marks ?? group.items;
  // The show's OWN decision -- the show key the whole-show control toggles, read straight from
  // the row, never rolled up from the seasons' own marks. The control clears only this key, so
  // lighting it from an aggregate it cannot clear was a dead toggle. Seasons overridden one by
  // one keep their marks in the strip; this stays null until the whole show is decided.
  const showOverride = first.show_override;
  const state = showOverride === "spare" ? "card-spared" : showOverride === "reap" ? "card-reaped" : "";
  const { selectMode, isSelected } = select;

  return (
    <article
      className={`card card-show ${state} ${selectMode ? "card-select" : ""} ${
        isSelected ? "card-picked" : ""
      }`}
      onPointerDown={(e) => selectMode && select.onSelectDown(group.key, e)}
      onPointerEnter={() => selectMode && select.onSelectEnter(group.key)}
    >
      <div
        className={`card-head clickable ${selectedGroupKey === group.key ? "card-selected" : ""}`}
        onClick={() => !selectMode && onOpenGroup(group.key)}
        role="button"
        tabIndex={0}
        aria-pressed={selectMode ? isSelected : undefined}
        aria-label={selectMode ? `Select ${group.title}` : `About ${group.title}`}
        onKeyDown={(e) => {
          if (e.key === "Enter" || e.key === " ") {
            e.preventDefault();
            selectMode ? select.onSelectToggle(group.key) : onOpenGroup(group.key);
          }
        }}
      >
        <Backdrop posterUrl={group.poster} />
        {selectMode && (
          <div className="card-tick-col">
            <SelectTick selected={isSelected} />
          </div>
        )}
        <Poster url={group.poster} alt={group.title} />
        <div className="card-body">
          <div className="card-title-row">
            <h3 className="card-title">
              {group.title}
              {group.year && <span className="card-year"> {group.year}</span>}
            </h3>
            <OverrideChip
              override={showOverride}
              effective={groupReapEffective(showSeasons)}
              // Seasons whose OWN decision opposes the show's (their effective override differs
              // from show_override), so the chip won't claim the whole show is kept/removed
              // when one season inside goes the other way (U-3).
              exceptions={
                showOverride
                  ? showSeasons.filter((s) => s.override != null && s.override !== showOverride)
                      .length
                  : 0
              }
              spareExpiresAt={first.show_spare_expires_at}
            />
          </div>
          <div className="card-meta">
            <span className="chip chip-tv">TV</span>
            <LibraryChip library={group.library} />
            <SeasonExpander
              count={totalSeasons}
              open={open}
              onToggle={() => {
                touched.current = true;
                setOpen((v) => !v);
              }}
            />
            <span>
              {isReapTab
                ? `${condemnedCount} of ${totalSeasons} would be removed · ${totalBytes(condemnedBytes, condemnedUnknown)}`
                : totalBytes(wholeShowBytes ?? fetchedSize, wholeShowBytes === null ? fetchedUnknown : wholeShowUnknown)}
            </span>
            {/* Ended, or a status we couldn't read. A show that is still going wears
                nothing here: the quiet row is the common case. */}
            <ShowStatusChip status={showStatus} quiet />
            <RequestedChip who={group.requestedBy} />
          </div>
          {marks && marks.length > 1 && <SeasonStrip marks={marks} onOpen={onOpen} />}
          <CardStatusLine
            condemned={isReapTab}
            dormantFor={group.dormantFor}
            reason={group.reason}
            chip={first.chip}
            // A show is held back only when EVERY actable season is: one measured season
            // still gives "Reap now" something to do, and the count beside it already
            // says how many it is leaving out.
            unmeasured={isReapTab && condemnedCount === 0 && condemnedUnknown > 0}
          />
        </div>
        <div className="card-side">
          {/* Spare or reap the whole show in one go -- the decision covers every season. In
              Select mode the inline buttons stand down; the bulk bar carries the actions.
              Otherwise the decision icon rests here until hover reveals the buttons. */}
          {!selectMode && (
            <>
              <OverrideMark override={showOverride} spareExpiresAt={first.show_spare_expires_at} />
              <OverrideControls
                override={showOverride}
                onSet={(d, sd) => onSet(group.key, d, sd)}
                onClear={() => onClear(group.key)}
                pending={pending}
                // Not the movie's tab-based `hideReap`: a whole-show Reap still takes the
                // show's kept seasons, so it stays until the WHOLE show is condemned
                // (showReapIsNoop). Judged over `marks` -- every season, every lane -- not
                // `group.items`, which on the Condemned tab holds only this show's condemned
                // seasons and would wrongly read as "all condemned" and hide Reap. The season
                // rows below keep the per-lane test.
                hideReap={showReapIsNoop(showSeasons)}
              />
            </>
          )}
        </div>
      </div>

      {!selectMode && open && (
        <SeasonList
          groupKey={group.key}
          selectedId={selectedId}
          onOpen={onOpen}
          onSet={onSet}
          onClear={onClear}
          pending={pending}
        />
      )}
    </article>
  );
}

export function ReviewQueue({
  verdict,
  onVerdictChange,
  selectedId,
  selectedGroupKey,
  onSelect,
  onSelectGroup,
  stepRef,
}: {
  verdict: Verdict;
  onVerdictChange: (verdict: Verdict) => void;
  selectedId: number | null;
  selectedGroupKey: string | null;
  onSelect: (id: number) => void;
  onSelectGroup: (key: string) => void;
  /** Filled in with a way to move the open card one place up or down this list, for the
   *  keyboard review loop. The queue owns the order, so it owns the walk. */
  stepRef?: RefObject<((delta: 1 | -1) => void) | null>;
}) {
  const [searchInput, setSearchInput] = useState("");
  const [search, setSearch] = useState("");
  const [filters, setFilters] = useState<QueueFilters>(() => loadFilters(verdict));
  // Which filter popover is open: a dimension id, "add" for the ＋ Filter menu, or null.
  // One at a time, so the list only ever renders one menu.
  const [openMenu, setOpenMenu] = useState<string | null>(null);
  const [visible, setVisible] = useState(PAGE);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [selectMode, setSelectMode] = useState(false);
  // How many of the last bulk override's requests failed, so the operator learns that a bulk
  // action was partial rather than seeing it silently succeed. 0 means nothing to report.
  const [bulkFailures, setBulkFailures] = useState(0);
  // The plan whose confirmation sheet is open, if any. Building it is the "Reap now" step;
  // the sheet then dry-runs, checks arming, and takes the typed confirmation before deleting.
  const [reapRun, setReapRun] = useState<Run | null>(null);
  // The in-flight drag: whether a press-and-drag is currently adding or removing, so every card
  // the pointer crosses paints the same way. A ref, not state -- it changes mid-drag and must
  // not re-render the list on every card it passes over.
  const dragRef = useRef<{ mode: "add" | "remove" } | null>(null);
  const sentinel = useRef<HTMLDivElement>(null);

  // Debounce the search box so we do not fire a request per keystroke.
  useEffect(() => {
    const id = setTimeout(() => setSearch(searchInput.trim()), 250);
    return () => clearTimeout(id);
  }, [searchInput]);

  // Close an open filter menu on an outside click or Escape. Each menu lives inside a
  // .filter-anchor wrapper (with its chip or button), so a click within any of them counts as
  // inside; switching to a different chip is handled by that chip's own toggle.
  useEffect(() => {
    if (openMenu === null) return;
    const onDown = (e: MouseEvent) => {
      if (!(e.target as HTMLElement).closest(".filter-anchor")) setOpenMenu(null);
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setOpenMenu(null);
    };
    document.addEventListener("mousedown", onDown);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onDown);
      document.removeEventListener("keydown", onKey);
    };
  }, [openMenu]);

  // Each tab remembers its own filters. On a tab switch, adopt that tab's remembered set
  // and skip the save below for that render -- otherwise the old tab's filters would be
  // written under the new tab's key before the load lands. A ref, not state: it flips
  // mid-effect and must not re-render anything (see the engineering rules on effect deps).
  const filtersVerdict = useRef(verdict);
  useEffect(() => {
    if (filtersVerdict.current !== verdict) {
      filtersVerdict.current = verdict;
      setFilters(loadFilters(verdict));
      return;
    }
    saveFilters(verdict, filters);
  }, [verdict, filters]);

  // Start over from the top whenever the list itself changes (a new tab, filter or sort), and
  // drop any selection -- a key picked on one tab is not visible on another.
  useEffect(() => setVisible(PAGE), [verdict, search, filters]);
  // The failure notice promises the failed items are still picked, so it has to die with the
  // selection it refers to -- otherwise it keeps offering a retry over an empty set.
  useEffect(() => {
    setSelected(new Set());
    setBulkFailures(0);
  }, [verdict, search, filters]);

  const { data: pages, isPending, error, hasNextPage, isFetchingNextPage, fetchNextPage } =
    useInfiniteQuery({
      queryKey: ["candidates", verdict, search, filters],
      queryFn: ({ pageParam }) =>
        api.candidates(
          verdict,
          {
            search,
            media_type: filters.mediaType,
            requested: filters.requested,
            genre: filters.genre,
            library: filters.library,
            override: filters.override,
            sort: filters.sort,
            order: filters.order,
          },
          FETCH_PAGE,
          pageParam,
        ),
      initialPageParam: 0,
      // The next offset, until we have fetched the whole filtered set the header counted.
      getNextPageParam: (last) => {
        const next = last.offset + last.items.length;
        return next < last.total ? next : undefined;
      },
    });

  // Every candidate loaded so far, flattened across pages; the totals come from the server (the
  // full filtered set, measured before the page window) so the header is right from page one.
  // Memoised on `pages` so the reference is stable between unrelated renders (a keystroke, a
  // drag repaint) -- otherwise the sentinel observer below, keyed on `data`, would tear down
  // and rebuild every render and could re-fire while the sentinel sits in view.
  const data = useMemo(
    () => (pages ? pages.pages.flatMap((p) => p.items) : undefined),
    [pages],
  );
  const totalItems = pages?.pages[0]?.total ?? 0;
  // Named apart from the `totalBytes` formatter, which this used to shadow -- which is
  // how the header kept rendering a bare sum while every other total had learned to say
  // what it could not include.
  const totalSize = pages?.pages[0]?.totalBytes ?? 0;
  const totalUnknownSize = pages?.pages[0]?.unknownSize ?? 0;

  // Reveal another render-page as the sentinel scrolls into view.
  useEffect(() => {
    const node = sentinel.current;
    if (!node) return;
    const observer = new IntersectionObserver((entries) => {
      if (entries.some((e) => e.isIntersecting)) setVisible((v) => v + PAGE);
    });
    observer.observe(node);
    return () => observer.disconnect();
    // `data` is now a stable reference (memoised above), so this only re-runs when the loaded
    // set actually changes; hasNextPage covers the sentinel appearing once more pages exist.
  }, [data, hasNextPage]);

  // The same overrides the why-panel sets, refreshing the same caches: one hook owns
  // the list of surfaces an override changes.
  const { setOverride, clearOverride, refresh } = useOverrideMutations();
  const bulk = useMutation({
    // allSettled, not Promise.all: Promise.all rejects on the first failed request and skips
    // onSuccess entirely, so a single 500 among fifty would leave ~49 already-applied changes
    // with the queue unrefreshed and the whole selection still showing stale verdicts. We
    // instead let every request settle, then always refresh, and report which ones failed.
    // Zipped back onto their keys, not counted: "3 failed" is useless in a list of hundreds
    // if you cannot tell which three.
    mutationFn: async ({
      keys,
      decision,
      spareDays = 0,
    }: {
      keys: string[];
      decision: Override | null;
      spareDays?: number;
    }) => {
      const results = await Promise.allSettled(
        keys.map((key) =>
          decision === null
            ? api.clearOverride(key)
            : api.override(key, decision, undefined, spareDays),
        ),
      );
      return keys.filter((_, i) => results[i]!.status === "rejected");
    },
    onMutate: () => setBulkFailures(0),
    onSuccess: (failedKeys) => {
      refresh();
      // The failures stay picked so retrying is one more press of the same button; a clean
      // run selects nothing, as before.
      setSelected(new Set(failedKeys));
      setBulkFailures(failedKeys.length);
    },
  });
  // Pick every card the filters match, not just the ones drawn so far: page the rest of the
  // list in, then select all of it. On a big library this walks several requests, which is
  // why it reports pending and failure like any other action. If it ever feels slow, the
  // cleaner shape is a server route that returns just the matching keys, so the browser
  // never has to hold the whole list to select it.
  const selectEverything = useMutation({
    mutationFn: async () => {
      let result = await fetchNextPage();
      let fetched = result.data?.pages.length ?? 0;
      while (result.hasNextPage && !result.isError) {
        result = await fetchNextPage();
        const grown = result.data?.pages.length ?? 0;
        // A fetch that added no page cannot make progress; stop rather than spin.
        if (grown <= fetched) break;
        fetched = grown;
      }
      // Short of the whole list, selecting what did arrive would claim "everything matching"
      // while meaning "some of it". Fail instead, and leave the selection untouched.
      if (result.hasNextPage || result.isError || !result.data) {
        throw new Error("The rest of the list didn't load.");
      }
      return toGroups(result.data.pages.flatMap((p) => p.items)).map(groupKeyOf);
    },
    onSuccess: (keys) => {
      setSelected(new Set(keys));
      // This selection is not the one the failure notice was talking about.
      setBulkFailures(0);
    },
  });
  // Build a plan for exactly the selected items and open the confirmation sheet. Nothing
  // deletes here -- the sheet is the gauntlet (dry run, arm check, typed phrase).
  const reapNow = useMutation({
    mutationFn: (keys: string[]) => api.createRun(keys),
    onSuccess: (run) => setReapRun(run),
  });
  const pending =
    setOverride.isPending ||
    clearOverride.isPending ||
    bulk.isPending ||
    reapNow.isPending ||
    // Acting while the rest of the list is still arriving would act on part of what the
    // operator asked for, under a button that says "everything matching".
    selectEverything.isPending;

  const onSet = (key: string, decision: Override, spareDays?: number) =>
    setOverride.mutate({ key, decision, spareDays: spareDays ?? 0 });
  const onClear = (key: string) => clearOverride.mutate(key);

  // --- Select mode: tap to toggle, or press-and-drag to paint a run of cards ------------------
  const applySelect = useCallback((key: string, mode: "add" | "remove") => {
    setSelected((prev) => {
      if (mode === "add" ? prev.has(key) : !prev.has(key)) return prev;
      const next = new Set(prev);
      mode === "add" ? next.add(key) : next.delete(key);
      return next;
    });
  }, []);
  // A press begins a drag whose direction (add vs remove) is fixed by the card pressed: press an
  // unpicked card to paint selections, a picked one to rub them out. Then every card the pointer
  // enters follows suit -- so a tap toggles one, a drag sweeps a section.
  const onSelectDown = (key: string, e: ReactPointerEvent) => {
    e.preventDefault();
    const mode: "add" | "remove" = selected.has(key) ? "remove" : "add";
    dragRef.current = { mode };
    applySelect(key, mode);
  };
  const onSelectEnter = (key: string) => {
    if (dragRef.current) applySelect(key, dragRef.current.mode);
  };
  // Keyboard toggle: flip one card and leave no drag armed -- the belt to onSelectDown's
  // pointer path, so tabbing through and pressing Space never strands a hover-painting mode.
  const onSelectToggle = (key: string) => {
    applySelect(key, selected.has(key) ? "remove" : "add");
  };
  // End the drag wherever the button is released -- even off the list.
  useEffect(() => {
    const end = () => {
      dragRef.current = null;
    };
    window.addEventListener("pointerup", end);
    window.addEventListener("pointercancel", end);
    return () => {
      window.removeEventListener("pointerup", end);
      window.removeEventListener("pointercancel", end);
    };
  }, []);
  // Leaving Select mode clears the picks -- the toggle is the one place selection is undone, so
  // turning it off can never strand a hidden selection behind a bulk action.
  const toggleSelectMode = () => {
    setBulkFailures(0);
    setSelectMode((on) => {
      if (on) setSelected(new Set());
      return !on;
    });
  };

  const tab = TABS.find((t) => t.verdict === verdict) ?? TABS[0]!;
  const groups = data ? toGroups(data) : [];
  // The genre and library choices are what the latest scan actually saw, most common first --
  // the genre list is the same one the policy rule editors suggest from.
  const { data: genreValues } = useQuery({
    queryKey: ["vocabulary-values", "genre"],
    queryFn: () => api.vocabularyValues("genre"),
    staleTime: 5 * 60 * 1000,
  });
  const { data: libraryValues } = useQuery({
    queryKey: ["vocabulary-values", "library"],
    queryFn: () => api.vocabularyValues("library"),
    staleTime: 5 * 60 * 1000,
  });
  // The operator's "expand seasons by default" preference (Settings -> General). It only
  // seeds each show card's starting state; a click on a card still wins for that card.
  // Shares the query key the settings panel writes, so flipping it there and returning here
  // takes effect. Unknown/error reads as off -- the safe, unchanged default.
  const { data: generalSettings } = useQuery({
    queryKey: ["general-settings"],
    queryFn: api.general,
    staleTime: 5 * 60 * 1000,
  });
  const expandSeasonsByDefault = generalSettings?.expand_seasons_default ?? false;
  // What a bulk Spare keeps items for: the operator's default length (0 = forever). The
  // per-card menu offers other lengths; the bulk bar acts on the whole selection at once, so
  // it uses the default and its glyph (∞ or a clock) shows which that is.
  const defaultSpareDays = generalSettings?.default_spare_days ?? 0;
  // A remembered value the newest scan no longer has stays selectable: the row set it filters
  // is honest (empty), and the option must exist for the chip to show it.
  const genreOptions = useMemo(() => {
    const values = genreValues?.values ?? [];
    return filters.genre && !values.includes(filters.genre) ? [filters.genre, ...values] : values;
  }, [genreValues, filters.genre]);
  const libraryOptions = useMemo(() => {
    const values = libraryValues?.values ?? [];
    return filters.library && !values.includes(filters.library)
      ? [filters.library, ...values]
      : values;
  }, [libraryValues, filters.library]);

  const dimensions: FilterDimension[] = useMemo(
    () => [
      {
        id: "mediaType",
        label: "Type",
        icon: <LayersIcon />,
        defaultValue: "",
        options: MEDIA_FILTERS.filter((f) => f.value !== ""),
        value: (f) => f.mediaType,
        set: (f, v) => ({ ...f, mediaType: v }),
      },
      {
        id: "library",
        label: "Library",
        icon: <LibraryIcon />,
        defaultValue: "",
        options: libraryOptions.map((l) => ({ value: l, label: l })),
        value: (f) => f.library,
        set: (f, v) => ({ ...f, library: v }),
      },
      {
        id: "requested",
        label: "Requested",
        icon: <FunnelIcon />,
        defaultValue: "any",
        options: REQUESTED_FILTERS.filter((f) => f.value !== "any"),
        value: (f) => f.requested,
        set: (f, v) => ({ ...f, requested: v as RequestedFilter }),
      },
      {
        id: "genre",
        label: "Genre",
        icon: <GenreIcon />,
        defaultValue: "",
        options: genreOptions.map((g) => ({ value: g, label: g })),
        value: (f) => f.genre,
        set: (f, v) => ({ ...f, genre: v }),
      },
      {
        id: "override",
        label: "Your decision",
        icon: <OverrideIcon />,
        defaultValue: "any",
        options: OVERRIDE_FILTERS.filter((f) => f.value !== "any"),
        value: (f) => f.override,
        set: (f, v) => ({ ...f, override: v as OverrideFilter }),
      },
    ],
    [genreOptions, libraryOptions],
  );

  const activeDimensions = dimensions.filter((d) => d.value(filters) !== d.defaultValue);
  // Only offer a filter that has something to pick: an open list (genre, library) with no
  // values in this scan would add an empty menu.
  const addableDimensions = dimensions.filter(
    (d) => d.value(filters) === d.defaultValue && d.options.length > 0,
  );

  const filtering = Boolean(search) || activeDimensions.length > 0;
  const clearFilters = () => {
    setSearchInput("");
    setSearch("");
    setOpenMenu(null);
    // Sort survives a clear: it orders the list, it hides nothing.
    setFilters((f) => ({ ...DEFAULT_FILTERS, sort: f.sort, order: f.order }));
  };

  // How many items the filters are hiding, for the filtered-empty state. Only asked for
  // when that state is actually on screen: one row, headers only.
  const { data: unfilteredPage } = useQuery({
    queryKey: ["candidates-unfiltered", verdict],
    queryFn: () => api.candidates(verdict, {}, 1, 0),
    enabled: filtering && !isPending && !error && (data?.length ?? 0) === 0,
  });
  // The override key each shown card acts on: a show's group key, or a movie's media key.
  const shownGroups = groups.slice(0, visible);
  const shownKeys = shownGroups.map(groupKeyOf);
  const shownItems = shownGroups.reduce((n, g) => n + g.items.length, 0);
  const allShownSelected = shownKeys.length > 0 && shownKeys.every((k) => selected.has(k));
  // Whether picking everything the filters match is still worth offering: some card is
  // either unfetched or drawn-but-unpicked beyond the window the "Select all" button reaches.
  const moreToSelect =
    allShownSelected && (hasNextPage || !groups.every((g) => selected.has(groupKeyOf(g))));
  // Picked cards that are not on screen: the state "Select everything matching" leaves behind.
  const holdsUndrawn = selected.size > shownKeys.length;
  // How many cards match, but only when that is knowable: a show card stands for all of its
  // seasons, so the server's item total is the card count only when the list is movies alone.
  // Otherwise the button states no number rather than a wrong one.
  const matchingCards = filters.mediaType === "movie" ? totalItems : null;

  // --- The keyboard review loop ---------------------------------------------------------
  // Move the open card one place along this list: the list the operator is looking at,
  // with this tab's filters and sort applied, so the arrows follow the order on screen.
  // A card is brought into view only when a step put it there; a click needs no scroll.
  const steppedRef = useRef(false);
  const step = (delta: 1 | -1) => {
    const at = groups.findIndex(
      (g) => (g.isShow && g.key === selectedGroupKey) || g.items.some((i) => i.id === selectedId),
    );
    // The open card is not in this list at all (the filters changed under it): nowhere to
    // step from, so stay where we are.
    if (at < 0) return;
    const next = groups[at + delta];
    if (!next) return;
    // Stepping past the rendered window reveals the card we are stepping to.
    if (at + delta >= visible) setVisible(at + delta + 1);
    steppedRef.current = true;
    if (next.isShow) onSelectGroup(next.key);
    else onSelect(next.items[0]!.id);
  };
  // Re-registered every render, on purpose: `step` closes over the list as it stands now.
  useEffect(() => {
    if (!stepRef) return;
    stepRef.current = step;
    return () => {
      stepRef.current = null;
    };
  });
  useEffect(() => {
    if (!steppedRef.current) return;
    steppedRef.current = false;
    document.querySelector(".card-selected")?.scrollIntoView({ block: "nearest" });
  }, [selectedId, selectedGroupKey]);

  // Keep the server buffer ahead of the render window: once revealed cards reach within a
  // render-page of everything fetched, pull the next server page so scrolling never stalls.
  useEffect(() => {
    if (hasNextPage && !isFetchingNextPage && visible >= groups.length - PAGE) {
      void fetchNextPage();
    }
  }, [visible, groups.length, hasNextPage, isFetchingNextPage, fetchNextPage]);

  // An empty list under a filter should explain itself, not read as broken: say how much
  // the filters hide, in this tab's own words. The common surprise gets its own sentence:
  // "Requested" on the reap tab is empty because requested media gets watched, and watched
  // media is protected, not reaped.
  const hiddenCount = unfilteredPage?.total ?? 0;
  const [hiddenOne, hiddenMany] =
    verdict === "condemn"
      ? ["condemned item", "condemned items"]
      : verdict === "protect"
        ? ["protected item", "protected items"]
        : ["item in Limbo", "items in Limbo"];
  const hiddenLine =
    hiddenCount === 1
      ? `1 ${hiddenOne} is hidden.`
      : `${count(hiddenCount)} ${hiddenMany} are hidden.`;
  const requestedExplainer =
    filters.requested === "yes" && verdict === "condemn"
      ? " None of them were requested, which is common: requested media gets watched, and " +
        "watched media doesn't end up condemned."
      : "";

  return (
    <section className="queue">
      {/* A view-level heading, for parity with Policy/Fairness/Settings so heading navigation
          can land on "Review queue" the way it lands on those views. */}
      <h2>Review queue</h2>
      <nav className="tabs" aria-label="Queue lists">
        {TABS.map((t) => (
          <button
            key={t.verdict}
            className={t.verdict === verdict ? "tab active" : "tab"}
            // Reserve the bold (active) width so switching lists never shifts the tab row.
            data-label={t.label}
            // The list you are on is stated, not just colored, the same as the masthead
            // and the settings rail. Plain buttons, not the tabs pattern: these switch a
            // whole list rather than swapping panels, and none of that pattern's keyboard
            // contract (arrow keys, a tabpanel to point at) exists here.
            aria-current={t.verdict === verdict ? "page" : undefined}
            onClick={() => onVerdictChange(t.verdict)}
          >
            {t.label}
          </button>
        ))}
      </nav>

      <p className="blurb">{tab.blurb}</p>

      <div className="queue-toolbar">
        <div className="search-wrap">
          <svg className="search-icon" viewBox="0 0 16 16" width="14" height="14" aria-hidden="true">
            <circle cx="7" cy="7" r="4.5" fill="none" stroke="currentColor" strokeWidth="1.5" />
            <path d="M11 11l3 3" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" />
          </svg>
          <input
            className="search-input"
            type="search"
            aria-label="Search titles and shows"
            placeholder="Search titles and shows…"
            value={searchInput}
            onChange={(e) => setSearchInput(e.target.value)}
          />
        </div>

        {/* One control adds any filter, so a new filter never means a new toolbar button.
            Each filter added here becomes a removable, editable chip below. Hidden once every
            filter is already applied. */}
        {addableDimensions.length > 0 && (
          <span className="filter-anchor">
            <button
              type="button"
              className={`add-filter ${openMenu === "add" ? "open" : ""}`}
              aria-haspopup="menu"
              aria-expanded={openMenu === "add"}
              onClick={() => setOpenMenu((m) => (m === "add" ? null : "add"))}
            >
              <PlusIcon />
              Filter
            </button>
            {openMenu === "add" && (
              <ul className="filter-menu" role="menu" aria-label="Add a filter">
                <li className="filter-menu-head" aria-hidden="true">
                  Add a filter
                </li>
                {addableDimensions.map((d) => (
                  <li key={d.id} role="none">
                    <button
                      type="button"
                      role="menuitem"
                      className="filter-mi"
                      onClick={() => {
                        setFilters((f) => d.set(f, d.options[0]!.value));
                        setOpenMenu(null);
                      }}
                    >
                      <span className="filter-mi-ic" aria-hidden="true">
                        {d.icon}
                      </span>
                      {d.label}
                    </button>
                  </li>
                ))}
              </ul>
            )}
          </span>
        )}

        <div className="sort-group">
          <Pill
            icon={<SortIcon />}
            value={filters.sort}
            onChange={(v) => setFilters((f) => ({ ...f, sort: v as SortKey }))}
            title="Sort by"
          >
            {SORTS.map((s) => (
              <option key={s.value} value={s.value}>
                {s.label}
              </option>
            ))}
          </Pill>
          <button
            className="sort-dir"
            onClick={() =>
              setFilters((f) => ({ ...f, order: f.order === "desc" ? "asc" : "desc" }))
            }
            title={filters.order === "desc" ? "High to low" : "Low to high"}
            aria-label={filters.order === "desc" ? "Descending" : "Ascending"}
          >
            <svg viewBox="0 0 16 16" width="14" height="14" fill="none" aria-hidden="true" className={filters.order}>
              <path d="M8 3v10M4 9l4 4 4-4" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" />
            </svg>
          </button>
        </div>

        {/* Turn the whole list into a selectable surface: tap a card to pick it, or press and
            drag across a run. Turning it off clears the picks. */}
        <button
          type="button"
          className={`select-toggle ${selectMode ? "active" : ""}`}
          onClick={toggleSelectMode}
          aria-pressed={selectMode}
          title={
            selectMode
              ? "Done selecting. Clears your picks"
              : "Select several at once to spare or reap"
          }
        >
          <CheckSquareIcon />
          {selectMode ? "Done" : "Select"}
        </button>
      </div>

      {/* Every active filter as a chip: the search term (cleared with one tap) and each added
          dimension (click to change its value, × to remove). A stacked combination is visible
          at a glance. Sort is not a chip: it hides nothing. */}
      {filtering && (
        <div className="active-filters">
          {search && (
            <FilterChip
              label={<>&ldquo;{search}&rdquo;</>}
              clearLabel={`Stop searching for ${search}`}
              onClear={() => {
                setSearchInput("");
                setSearch("");
              }}
            />
          )}
          {activeDimensions.map((d) => {
            const current = d.value(filters);
            const label = d.options.find((o) => o.value === current)?.label ?? current;
            return (
              <span className="filter-anchor" key={d.id}>
                <span className={`fchip ${openMenu === d.id ? "open" : ""}`}>
                  <button
                    type="button"
                    className="fchip-body"
                    title={`Filter: ${d.label}`}
                    aria-haspopup="listbox"
                    aria-expanded={openMenu === d.id}
                    onClick={() => setOpenMenu((m) => (m === d.id ? null : d.id))}
                  >
                    <span className="fchip-ic" aria-hidden="true">
                      {d.icon}
                    </span>
                    <b>{label}</b>
                    <CaretIcon />
                  </button>
                  <button
                    type="button"
                    className="fchip-x"
                    aria-label={`Remove the ${d.label} filter`}
                    onClick={() => {
                      setFilters((f) => d.set(f, d.defaultValue));
                      setOpenMenu((m) => (m === d.id ? null : m));
                    }}
                  >
                    ×
                  </button>
                </span>
                {openMenu === d.id && (
                  <ul className="filter-menu" role="listbox" aria-label={d.label}>
                    <li className="filter-menu-head" aria-hidden="true">
                      {d.label}
                    </li>
                    {d.options.map((o) => (
                      <li key={o.value} role="none">
                        <button
                          type="button"
                          role="option"
                          aria-selected={o.value === current}
                          className={`filter-mi ${o.value === current ? "sel" : ""}`}
                          onClick={() => {
                            setFilters((f) => d.set(f, o.value));
                            setOpenMenu(null);
                          }}
                        >
                          <span className="filter-mi-label">{o.label}</span>
                          {o.value === current && (
                            <span className="filter-mi-tick" aria-hidden="true">
                              <CheckIcon />
                            </span>
                          )}
                        </button>
                      </li>
                    ))}
                  </ul>
                )}
              </span>
            );
          })}
          <button type="button" className="link-btn" onClick={clearFilters}>
            Clear all
          </button>
        </div>
      )}

      {error && <p className="error">{error.message}</p>}
      {isPending && <p className="muted">Loading…</p>}

      {data && data.length === 0 && !filtering && <p className="empty">{tab.empty}</p>}
      {data && data.length === 0 && filtering && (
        <div className="empty-filtered">
          <p className="empty-headline">Nothing here matches your filters.</p>
          {hiddenCount > 0 && (
            <p className="muted">
              {hiddenLine}
              {requestedExplainer}
            </p>
          )}
          <button type="button" className="sm ghost" onClick={clearFilters}>
            Clear filters
          </button>
        </div>
      )}

      {data && data.length > 0 && (
        <>
          {/* The unknown count sits AFTER "would be freed", not inside the total: those
              items are precisely the ones that would not be freed, so folding them into
              the same phrase says the opposite of what is true. */}
          <p className="queue-total">
            <strong>{count(totalItems)}</strong> {totalItems === 1 ? "item" : "items"}
            {" · "}
            <strong>{bytes(totalSize)}</strong>
            {verdict === "condemn" && " would be freed"}
            {totalUnknownSize > 0 && (
              <>
                {" · "}
                <strong>
                  {count(totalUnknownSize)} {totalUnknownSize === 1 ? "size" : "sizes"} unknown
                </strong>
              </>
            )}
          </p>
          <div className={`card-list ${selectMode ? "card-list-selecting has-bulk-bar" : ""}`}>
            {shownGroups.map((group) => {
              const key = groupKeyOf(group);
              const cardSelect: CardSelect = {
                selectMode,
                isSelected: selected.has(key),
                onSelectDown,
                onSelectEnter,
                onSelectToggle,
              };
              return group.isShow ? (
                <ShowCard
                  key={group.key}
                  group={group}
                  defaultOpen={expandSeasonsByDefault}
                  selectedId={selectedId}
                  selectedGroupKey={selectedGroupKey}
                  select={cardSelect}
                  onOpen={onSelect}
                  onOpenGroup={onSelectGroup}
                  onSet={onSet}
                  onClear={onClear}
                  pending={pending}
                />
              ) : (
                <MovieCard
                  key={group.key}
                  item={group.items[0]!}
                  selected={group.items[0]!.id === selectedId}
                  select={cardSelect}
                  onOpen={onSelect}
                  onSet={onSet}
                  onClear={onClear}
                  pending={pending}
                  hideReap={verdict === "condemn"}
                />
              );
            })}
          </div>
          {(visible < groups.length || hasNextPage) && (
            <div ref={sentinel} className="load-more muted">
              {isFetchingNextPage
                ? "Loading more…"
                : `Showing ${count(shownItems)} of ${count(totalItems)}`}
            </div>
          )}
        </>
      )}

      {selectMode && (
        <div className="bulk-bar" role="region" aria-label="Bulk actions">
          <span className="bulk-count">
            {selected.size > 0 ? (
              <>
                <strong>{selected.size}</strong> selected
              </>
            ) : (
              "Tap or drag to pick"
            )}
          </span>
          <div className="bulk-actions">
            <button
              type="button"
              className="sm ghost"
              disabled={shownKeys.length === 0 && selected.size === 0}
              onClick={() =>
                setSelected((prev) => {
                  // "Select everything matching" can leave far more picked than is drawn, and
                  // clearing only the drawn cards would strand the rest selected and invisible.
                  if (holdsUndrawn) return new Set();
                  const next = new Set(prev);
                  if (allShownSelected) shownKeys.forEach((k) => next.delete(k));
                  else shownKeys.forEach((k) => next.add(k));
                  return next;
                })
              }
              title={
                holdsUndrawn
                  ? "Clear the whole selection, including the cards not drawn yet"
                  : "Select (or clear) every card drawn so far"
              }
            >
              {holdsUndrawn || allShownSelected ? "Deselect all" : "Select all"}
            </button>
            {/* Reach past the drawn cards to the whole filtered list, so a bulk action never
                depends on scrolling a few thousand cards into existence first. */}
            {moreToSelect && (
              <button
                type="button"
                className="sm ghost"
                disabled={selectEverything.isPending}
                onClick={() => selectEverything.mutate()}
                title="Load the rest of this list and select all of it"
              >
                {selectEverything.isPending
                  ? "Selecting…"
                  : matchingCards === null
                    ? "Select everything matching"
                    : `Select everything matching (${count(matchingCards)})`}
              </button>
            )}
            <button
              type="button"
              className="sm ov-btn ov-spare"
              disabled={pending || selected.size === 0}
              onClick={() =>
                bulk.mutate({ keys: [...selected], decision: "spare", spareDays: defaultSpareDays })
              }
              title={
                defaultSpareDays > 0
                  ? `Spare the selected items for ${defaultSpareDays} days`
                  : "Spare the selected items forever"
              }
            >
              <SpareGlyph days={defaultSpareDays} /> Spare
            </button>
            {/* On Condemned the items are already on the block, so a bulk Reap override does
                nothing: drop it there, exactly as the per-card and panel buttons do. The real
                deletion is "Reap now" below, a different button, which stays. */}
            {verdict !== "condemn" && (
              <button
                type="button"
                className="sm ov-btn ov-reap"
                disabled={pending || selected.size === 0}
                onClick={() => bulk.mutate({ keys: [...selected], decision: "reap" })}
              >
                <ScytheIcon /> Reap
              </button>
            )}
            <button
              type="button"
              className="sm ghost"
              disabled={pending || selected.size === 0}
              onClick={() => bulk.mutate({ keys: [...selected], decision: null })}
              title="Remove any override and let Reaper judge these again"
            >
              Clear override
            </button>
            {verdict === "condemn" && (
              <button
                type="button"
                className="sm danger"
                disabled={pending || selected.size === 0}
                onClick={() => reapNow.mutate([...selected])}
                title="Delete the selected items now (opens a confirmation)"
              >
                {reapNow.isPending ? "Planning…" : "Reap now…"}
              </button>
            )}
            <button type="button" className="sm select-done" onClick={toggleSelectMode}>
              Done
            </button>
          </div>
        </div>
      )}

      {/* A per-card Spare or Reap that failed says so here, in the same place as the bulk
          failures -- otherwise the button reads as a click the app ignored. Same wording as
          the why-panel's, since it is the same action. */}
      {(setOverride.isError || clearOverride.isError) && (
        <p className="error bulk-error">Couldn't save that. Try again.</p>
      )}
      {selectEverything.isError && (
        <p className="error bulk-error">
          Couldn't load the rest of the list, so nothing was selected. Your picks are as they
          were. Try again.
        </p>
      )}
      {bulkFailures > 0 && (
        <p className="error bulk-error">
          {bulkFailures === 1
            ? "1 item could not be updated; it is still selected so you can try again."
            : `${count(bulkFailures)} items could not be updated; they are still selected so ` +
              "you can try again."}
        </p>
      )}
      {reapNow.error && <p className="error bulk-error">{reapNow.error.message}</p>}
      {reapRun && (
        <ReapConfirm
          run={reapRun}
          onClose={() => setReapRun(null)}
          onDone={() => setSelected(new Set())}
        />
      )}
    </section>
  );
}
