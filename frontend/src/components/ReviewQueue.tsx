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
import {
  api,
  type Candidate,
  type Chip,
  type GroupSeasonMark,
  type Override,
  type OverrideFilter,
  type RequestedFilter,
  type Run,
  type SortKey,
  type SortOrder,
  type Verdict,
} from "../api";
import { bytes, count } from "../format";
import { useOverrideMutations } from "../useOverrideMutations";
import { ReapConfirm } from "./ReapConfirm";
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

// --- remembered filters --------------------------------------------------------------------
// Each queue tab keeps its own filters and sort, on this device, until changed or cleared.

export interface QueueFilters {
  mediaType: string;
  requested: RequestedFilter;
  genre: string;
  override: OverrideFilter;
  sort: SortKey;
  order: SortOrder;
}

export const DEFAULT_FILTERS: QueueFilters = {
  mediaType: "",
  requested: "any",
  genre: "",
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

/** A labelled dropdown pill with a leading icon -- the filter/sort control shape. */
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
function Poster({ url, alt }: { url: string | null; alt: string }) {
  const [broken, setBroken] = useState(false);
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

/** The score chip. Colour carries the verdict so it reads without the label. */
function Score({ item }: { item: Candidate }) {
  return (
    <span className={`score score-${item.verdict}`} title={`Score ${item.score} of 100`}>
      {item.score}
    </span>
  );
}

/** The reap glyph: a small scythe. Only reap ACTIONS wear it -- close buttons keep ✕. */
function ScytheIcon() {
  return (
    <svg className="scythe" viewBox="0 0 16 16" width="13" height="13" fill="none" aria-hidden="true">
      <path
        d="M12.9 2.1 C8.8 -0.4, 3.2 1.5, 1.3 7.3 C4.1 3.9, 8.9 3.3, 12.4 3.4 Z"
        fill="currentColor"
      />
      <path
        d="M12 3 C10.6 7.2, 9.4 10.8, 8.6 14.4"
        stroke="currentColor"
        strokeWidth="1.6"
        strokeLinecap="round"
      />
      <path d="M10.2 8.6 l2 0.7" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" />
    </svg>
  );
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

/** "5 years, 9 months" -> "5y 9m", the compact span the pill wears. */
function compactSpan(text: string): string {
  return text
    .replace(/ years?/g, "y")
    .replace(/ months?/g, "m")
    .replace(/ days?/g, "d")
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

/** The two hand-overrides, as a paired toggle: **Spare** (∞ keep forever) and **Reap** (force
 *  onto the list). The active one is lit; clicking it again clears the override and lets Reaper
 *  judge the item again. Clicking the other switches. Stops the click from opening the panel. */
export function OverrideControls({
  override,
  onSet,
  onClear,
  pending,
}: {
  override: Override | null;
  onSet: (decision: Override) => void;
  onClear: () => void;
  pending: boolean;
}) {
  const click = (e: ReactMouseEvent, decision: Override) => {
    e.stopPropagation();
    override === decision ? onClear() : onSet(decision);
  };
  return (
    <div className="override-controls" role="group" aria-label="Spare or reap this item">
      <button
        type="button"
        className={`ov-btn ov-spare ${override === "spare" ? "active" : ""}`}
        disabled={pending}
        aria-pressed={override === "spare"}
        onClick={(e) => click(e, "spare")}
        title={
          override === "spare"
            ? "Spared. Click to let Reaper judge it again"
            : "Never reap this. Keep it forever"
        }
      >
        <span className="infinity" aria-hidden="true">
          ∞
        </span>{" "}
        {override === "spare" ? "Spared" : "Spare"}
      </button>
      <button
        type="button"
        className={`ov-btn ov-reap ${override === "reap" ? "active" : ""}`}
        disabled={pending}
        aria-pressed={override === "reap"}
        onClick={(e) => click(e, "reap")}
        title={
          override === "reap"
            ? "Marked for reaping. Click to undo"
            : "Force this onto the reap list"
        }
      >
        <ScytheIcon /> {override === "reap" ? "Reaping" : "Reap"}
      </button>
    </div>
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
function CardStatusLine({
  condemned,
  dormantFor,
  reason,
  chip,
}: {
  condemned: boolean;
  dormantFor: string | null;
  reason: string | null;
  chip: Chip | null;
}) {
  if (!condemned) return <StatusChip chip={chip} />;
  return (
    <>
      <DormantPill dormantFor={dormantFor} />
      {reason && !dormantFor && <p className="card-reason">{reason}</p>}
    </>
  );
}

/** Whether a show-level reap actually takes anywhere: true when any season's reap is
 *  honoured, false when every one is refused, undefined outside a reap override. */
function groupReapEffective(items: Candidate[]): boolean | undefined {
  const reaped = items.filter((s) => s.override === "reap");
  if (reaped.length === 0) return undefined;
  return reaped.some((s) => s.override_effective !== false);
}

/** The single decision that governs a whole show: what all of its seasons agree on, or null
 *  when they are mixed. A show-level override makes every season inherit it, so agreement is
 *  the common case; a per-season override is what makes it mixed. */
function groupOverride(items: Candidate[]): Override | null {
  if (items.every((s) => s.override === "spare")) return "spare";
  if (items.every((s) => s.override === "reap")) return "reap";
  return null;
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

/** What a strip square's tooltip says about its season's lane. */
const MARK_LABELS: Record<string, string> = {
  condemn: "would be removed",
  protect: "kept",
  abstain: "left alone",
};

/** The season strip: one small square per season of the show, colored by its lane
 *  across the WHOLE snapshot -- so "which seasons stay and which go" reads at a glance
 *  without expanding anything. A hand decision paints its square solid; a reap the
 *  engine refuses keeps its scan color, and the tooltip says both facts. Each square
 *  opens that season's own reasoning (the show card itself opens the show). */
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
        const reapRefused = mark.override === "reap" && mark.override_effective === false;
        const handClass =
          mark.override === "spare"
            ? " strip-ov-spare"
            : mark.override === "reap" && !reapRefused
              ? " strip-ov-reap"
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
 *  and condemned read side by side. Rows from this tab keep their Spare/Reap buttons;
 *  rows from other lanes are dimmed and act from their own tab (clicking any row still
 *  opens its full reasoning). */
function SeasonList({
  groupKey,
  tabVerdict,
  selectedId,
  onOpen,
  onSet,
  onClear,
  pending,
}: {
  groupKey: string;
  tabVerdict: Verdict;
  selectedId: number | null;
  onOpen: (id: number) => void;
  onSet: (key: string, decision: Override) => void;
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

  return (
    <ul className="season-list">
      {data.seasons.map((season) => {
        const inLane = season.verdict === tabVerdict;
        return (
          <li
            key={season.id}
            className={`season-row clickable ${inLane ? "" : "season-other"} ${
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
                />
              ) : season.verdict === "condemn" ? (
                <CondemnedChip />
              ) : (
                <StatusChip chip={season.chip} />
              )}
            </span>
            <span className="season-size num">{bytes(season.size_bytes)}</span>
            {inLane ? (
              <OverrideControls
                override={season.override}
                onSet={(d) => onSet(season.media_key, d)}
                onClear={() => onClear(season.media_key)}
                pending={pending}
              />
            ) : (
              // Other-lane rows are read-only here: they act from their own tab, and an
              // empty cell keeps the grid's columns aligned.
              <span aria-hidden="true" />
            )}
          </li>
        );
      })}
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
}: {
  item: Candidate;
  selected: boolean;
  select: CardSelect;
  onOpen: (id: number) => void;
  onSet: (key: string, decision: Override) => void;
  onClear: (key: string) => void;
  pending: boolean;
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
          />
        </div>
        {/* The type chip lives on the meta line, not the title row, so a long title
            never fights a chip for space and the year stays glued to the title. */}
        <div className="card-meta">
          <span className="chip chip-movie">Movie</span>
          <span>{bytes(item.size_bytes)}</span>
          <ResolutionBadge value={item.video_resolution} />
          <RequestedChip who={item.requested_by} />
        </div>
        <CardStatusLine
          condemned={isCondemned(item)}
          dormantFor={item.dormant_for}
          reason={item.reason}
          chip={item.chip}
        />
      </div>
      <div className="card-side">
        <Score item={item} />
        {/* In Select mode the whole card is a target, so the inline buttons stand down -- the
            bulk bar carries the actions instead. */}
        {!selectMode && (
          <OverrideControls
            override={item.override}
            onSet={(d) => onSet(item.media_key, d)}
            onClear={() => onClear(item.media_key)}
            pending={pending}
          />
        )}
      </div>
    </article>
  );
}

function ShowCard({
  group,
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
  selectedId: number | null;
  selectedGroupKey: string | null;
  select: CardSelect;
  onOpen: (id: number) => void;
  onOpenGroup: (key: string) => void;
  onSet: (key: string, decision: Override) => void;
  onClear: (key: string) => void;
  pending: boolean;
}) {
  const [open, setOpen] = useState(false);
  const first = group.items[0]!;
  // The whole show's shape, across every lane of the snapshot -- what the strip and the
  // season count draw from. Null only on rows from before this field existed.
  const marks = first.group_seasons;
  const totalSeasons = marks?.length ?? group.items.length;
  const wholeShowBytes = marks?.reduce((sum, m) => sum + m.size_bytes, 0) ?? null;
  const fetchedSize = group.items.reduce((sum, s) => sum + s.size_bytes, 0);
  // On the "Condemned" tab the byte figure must state what "Reap now" will actually
  // plan: the server's whole-snapshot totals (every condemned season minus hand-spares),
  // never a sum over the fetched pages, which on a long sorted list can hold only some
  // of a show's seasons. Other tabs describe the whole show, which the strip shows.
  const isReapTab = isCondemned(first);
  const condemnedCount = first.group_condemned_count ?? group.items.length;
  const condemnedBytes = first.group_condemned_bytes ?? fetchedSize;
  // What the whole show agrees on. A show-level override makes every season inherit it, so
  // this is the show's decision in the common case; a per-season override reads as mixed.
  const showOverride = groupOverride(group.items);
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
            <OverrideChip override={showOverride} effective={groupReapEffective(group.items)} />
          </div>
          <div className="card-meta">
            <span className="chip chip-tv">TV</span>
            <SeasonExpander count={totalSeasons} open={open} onToggle={() => setOpen((v) => !v)} />
            <span>
              {isReapTab
                ? `${condemnedCount} of ${totalSeasons} would be removed · ${bytes(condemnedBytes)}`
                : bytes(wholeShowBytes ?? fetchedSize)}
            </span>
            <RequestedChip who={group.requestedBy} />
          </div>
          {marks && marks.length > 1 && <SeasonStrip marks={marks} onOpen={onOpen} />}
          <CardStatusLine
            condemned={isReapTab}
            dormantFor={group.dormantFor}
            reason={group.reason}
            chip={first.chip}
          />
        </div>
        <div className="card-side">
          {/* Spare or reap the whole show in one go -- the decision covers every season. In
              Select mode the inline buttons stand down; the bulk bar carries the actions. */}
          {!selectMode && (
            <OverrideControls
              override={showOverride}
              onSet={(d) => onSet(group.key, d)}
              onClear={() => onClear(group.key)}
              pending={pending}
            />
          )}
        </div>
      </div>

      {!selectMode && open && (
        <SeasonList
          groupKey={group.key}
          tabVerdict={first.verdict}
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
  searchFor,
  stepRef,
}: {
  verdict: Verdict;
  onVerdictChange: (verdict: Verdict) => void;
  selectedId: number | null;
  selectedGroupKey: string | null;
  onSelect: (id: number) => void;
  onSelectGroup: (key: string) => void;
  /** A title to look up, pushed in from another view. The nonce applies each jump once,
   *  so clearing the box afterwards is not undone by the next render. */
  searchFor?: { term: string; nonce: number } | null;
  /** Filled in with a way to move the open card one place up or down this list, for the
   *  keyboard review loop. The queue owns the order, so it owns the walk. */
  stepRef?: RefObject<((delta: 1 | -1) => void) | null>;
}) {
  const [searchInput, setSearchInput] = useState("");
  const [search, setSearch] = useState("");
  const [filters, setFilters] = useState<QueueFilters>(() => loadFilters(verdict));
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

  // A title handed over from another view fills the search box, once per jump: the box
  // shows what is being looked for, so it stays the operator's to edit or clear.
  const handledSearch = useRef(0);
  useEffect(() => {
    if (!searchFor || searchFor.nonce === handledSearch.current) return;
    handledSearch.current = searchFor.nonce;
    setSearchInput(searchFor.term);
    setSearch(searchFor.term);
  }, [searchFor]);

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
  const totalBytes = pages?.pages[0]?.totalBytes ?? 0;

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
    mutationFn: async ({ keys, decision }: { keys: string[]; decision: Override | null }) => {
      const results = await Promise.allSettled(
        keys.map((key) => (decision === null ? api.clearOverride(key) : api.override(key, decision))),
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

  const onSet = (key: string, decision: Override) => setOverride.mutate({ key, decision });
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
  const filtering = Boolean(
    search ||
      filters.mediaType ||
      filters.requested !== "any" ||
      filters.genre ||
      filters.override !== "any",
  );
  const clearFilters = () => {
    setSearchInput("");
    setSearch("");
    // Sort survives a clear: it orders the list, it hides nothing.
    setFilters((f) => ({ ...DEFAULT_FILTERS, sort: f.sort, order: f.order }));
  };

  // The genre choices are what the latest scan actually saw, most common first -- the
  // same suggestions the policy rule editors use.
  const { data: genreValues } = useQuery({
    queryKey: ["vocabulary-values", "genre"],
    queryFn: () => api.vocabularyValues("genre"),
    staleTime: 5 * 60 * 1000,
  });
  // A remembered genre that the newest scan no longer has stays selectable: the row set
  // it filters is honest (empty), and the option must exist for the pill to display it.
  const genreOptions = useMemo(() => {
    const values = genreValues?.values ?? [];
    return filters.genre && !values.includes(filters.genre)
      ? [filters.genre, ...values]
      : values;
  }, [genreValues, filters.genre]);

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
            // The list you are on is stated, not just coloured, the same as the masthead
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

        <Pill
          icon={<LayersIcon />}
          value={filters.mediaType}
          onChange={(v) => setFilters((f) => ({ ...f, mediaType: v }))}
          title="Movies or TV"
        >
          {MEDIA_FILTERS.map((f) => (
            <option key={f.value} value={f.value}>
              {f.label}
            </option>
          ))}
        </Pill>

        <Pill
          icon={<FunnelIcon />}
          value={filters.requested}
          onChange={(v) => setFilters((f) => ({ ...f, requested: v as RequestedFilter }))}
          title="Filter by who asked for it through Seerr"
        >
          {REQUESTED_FILTERS.map((f) => (
            <option key={f.value} value={f.value}>
              {f.label}
            </option>
          ))}
        </Pill>

        <Pill
          icon={<GenreIcon />}
          value={filters.genre}
          onChange={(v) => setFilters((f) => ({ ...f, genre: v }))}
          title="Filter by genre"
        >
          <option value="">Any genre</option>
          {genreOptions.map((g) => (
            <option key={g} value={g}>
              {g}
            </option>
          ))}
        </Pill>

        <Pill
          icon={<OverrideIcon />}
          value={filters.override}
          onChange={(v) => setFilters((f) => ({ ...f, override: v as OverrideFilter }))}
          title="Filter by your own spare and reap decisions"
        >
          {OVERRIDE_FILTERS.map((f) => (
            <option key={f.value} value={f.value}>
              {f.label}
            </option>
          ))}
        </Pill>

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

      {/* Every active filter as a removable chip, so a stacked combination is visible at a
          glance and each piece clears with one tap. Sort is not a chip: it hides nothing. */}
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
          {filters.mediaType && (
            <FilterChip
              label={MEDIA_FILTERS.find((f) => f.value === filters.mediaType)?.label}
              clearLabel="Remove the media type filter"
              onClear={() => setFilters((f) => ({ ...f, mediaType: "" }))}
            />
          )}
          {filters.requested !== "any" && (
            <FilterChip
              label={REQUESTED_FILTERS.find((f) => f.value === filters.requested)?.label}
              clearLabel="Remove the requested filter"
              onClear={() => setFilters((f) => ({ ...f, requested: "any" }))}
            />
          )}
          {filters.genre && (
            <FilterChip
              label={filters.genre}
              clearLabel="Remove the genre filter"
              onClear={() => setFilters((f) => ({ ...f, genre: "" }))}
            />
          )}
          {filters.override !== "any" && (
            <FilterChip
              label={OVERRIDE_FILTERS.find((f) => f.value === filters.override)?.label}
              clearLabel="Remove the override filter"
              onClear={() => setFilters((f) => ({ ...f, override: "any" }))}
            />
          )}
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
          <p className="queue-total">
            <strong>{count(totalItems)}</strong> {totalItems === 1 ? "item" : "items"}
            {" · "}
            <strong>{bytes(totalBytes)}</strong>
            {verdict === "condemn" && " would be freed"}
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
              onClick={() => bulk.mutate({ keys: [...selected], decision: "spare" })}
            >
              <span className="infinity" aria-hidden="true">
                ∞
              </span>{" "}
              Spare
            </button>
            <button
              type="button"
              className="sm ov-btn ov-reap"
              disabled={pending || selected.size === 0}
              onClick={() => bulk.mutate({ keys: [...selected], decision: "reap" })}
            >
              <ScytheIcon /> Reap
            </button>
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
