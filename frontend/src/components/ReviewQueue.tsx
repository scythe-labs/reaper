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

import { useInfiniteQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import {
  type MouseEvent as ReactMouseEvent,
  type PointerEvent as ReactPointerEvent,
  type ReactNode,
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import {
  api,
  type Candidate,
  type Override,
  type RequestedFilter,
  type Run,
  type SortKey,
  type SortOrder,
  type Verdict,
} from "../api";
import { bytes, count } from "../format";
import { ReapConfirm } from "./ReapConfirm";

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
    label: "Would reap",
    blurb: "Scored at or above your threshold, with nothing protecting them.",
    empty: "Nothing is on the block. Reaper would not reap anything.",
  },
  {
    verdict: "protect",
    label: "Spared",
    blurb: "Something is protecting these. They stay, whatever they scored.",
    empty: "Nothing is being spared by a protection right now.",
  },
  {
    verdict: "abstain",
    label: "Left alone",
    blurb: "Below your threshold, or too little to go on. Reaper leaves them be.",
    empty: "Nothing landed here.",
  },
];

const MEDIA_FILTERS: { value: string; label: string }[] = [
  { value: "", label: "Everything" },
  { value: "movie", label: "Movies" },
  { value: "season", label: "TV seasons" },
];

const REQUESTED_FILTERS: { value: RequestedFilter; label: string }[] = [
  { value: "any", label: "Anyone" },
  { value: "yes", label: "Requested" },
  { value: "no", label: "Not requested" },
];

const SORTS: { value: SortKey; label: string }[] = [
  { value: "score", label: "Score" },
  { value: "size", label: "Size" },
  { value: "year", label: "Year" },
  { value: "title", label: "Title" },
];

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
      <select value={value} onChange={(e) => onChange(e.target.value)}>
        {children}
      </select>
    </label>
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

/** The two hand-overrides, as a paired toggle: **Spare** (∞ keep forever) and **Reap** (force
 *  onto the list). The active one is lit; clicking it again clears the override and lets Reaper
 *  judge the item again. Clicking the other switches. Stops the click from opening the panel. */
function OverrideControls({
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
            ? "Spared — click to let Reaper judge it again"
            : "Never reap this — keep it forever"
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
            ? "Marked for reaping — click to undo"
            : "Force this onto the reap list"
        }
      >
        <span aria-hidden="true">✕</span> {override === "reap" ? "Reaping" : "Reap"}
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

/** The chip a card shows once the owner has overridden it by hand -- pending until the next
 *  scan moves it for real. */
function OverrideChip({ override }: { override: Override | null }) {
  if (override === "spare") return <span className="chip chip-spared">Spared — will be kept</span>;
  if (override === "reap") return <span className="chip chip-reap">Reap — will be removed</span>;
  return null;
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
  items: Candidate[];
  isShow: boolean;
};

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
      {/* The gutter holds the selection tick in Select mode, and matches the show card's chevron
          column so every poster (movie and show) lines up at the same left edge. */}
      <div className="card-gutter">{selectMode && <SelectTick selected={isSelected} />}</div>
      <Poster url={item.poster_url} alt={item.title} />
      <div className="card-body">
        <div className="card-title-row">
          <h3 className="card-title">
            {item.title}
            {item.year && <span className="card-year"> {item.year}</span>}
          </h3>
          <span className="chip chip-movie">Movie</span>
          <OverrideChip override={item.override} />
        </div>
        <div className="card-meta">
          <span>{bytes(item.size_bytes)}</span>
          <RequestedChip who={item.requested_by} />
        </div>
        {item.reason && <p className="card-reason">{item.reason}</p>}
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
  select,
  onOpen,
  onSet,
  onClear,
  pending,
}: {
  group: Group;
  selectedId: number | null;
  select: CardSelect;
  onOpen: (id: number) => void;
  onSet: (key: string, decision: Override) => void;
  onClear: (key: string) => void;
  pending: boolean;
}) {
  const [open, setOpen] = useState(false);
  const totalSize = group.items.reduce((sum, s) => sum + s.size_bytes, 0);
  const label = group.items.length === 1 ? "1 season" : `${group.items.length} seasons`;
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
        className="card-head clickable"
        onClick={() => !selectMode && setOpen((v) => !v)}
        role="button"
        tabIndex={0}
        aria-expanded={selectMode ? undefined : open}
        aria-pressed={selectMode ? isSelected : undefined}
        onKeyDown={(e) => {
          if (e.key === "Enter" || e.key === " ") {
            e.preventDefault();
            selectMode ? select.onSelectToggle(group.key) : setOpen((v) => !v);
          }
        }}
      >
        <Backdrop posterUrl={group.poster} />
        {/* The gutter shows the selection tick in Select mode, else the expand chevron; its
            column is mirrored on movie cards so posters stay aligned down the whole list. */}
        <div className="card-gutter">
          {selectMode ? (
            <SelectTick selected={isSelected} />
          ) : (
            <svg
              className={`chevron ${open ? "open" : ""}`}
              viewBox="0 0 12 12"
              width="13"
              height="13"
              aria-hidden="true"
            >
              <path d="M4 2l4 4-4 4" fill="none" stroke="currentColor" strokeWidth="1.8" />
            </svg>
          )}
        </div>
        <Poster url={group.poster} alt={group.title} />
        <div className="card-body">
          <div className="card-title-row">
            <h3 className="card-title">
              {group.title}
              {group.year && <span className="card-year"> {group.year}</span>}
            </h3>
            <span className="chip chip-tv">TV</span>
            <OverrideChip override={showOverride} />
          </div>
          <div className="card-meta">
            <span>
              {label} · {bytes(totalSize)}
              {!selectMode && ` · ${open ? "hide" : "show"} seasons`}
            </span>
            <RequestedChip who={group.requestedBy} />
          </div>
          {group.reason && <p className="card-reason">{group.reason}</p>}
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
        <ul className="season-list">
          {group.items.map((season) => (
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
                {season.title.replace(`${group.title} — `, "")}
                <OverrideChip override={season.override} />
              </span>
              <span className="season-size num">{bytes(season.size_bytes)}</span>
              <OverrideControls
                override={season.override}
                onSet={(d) => onSet(season.media_key, d)}
                onClear={() => onClear(season.media_key)}
                pending={pending}
              />
            </li>
          ))}
        </ul>
      )}
    </article>
  );
}

export function ReviewQueue({
  verdict,
  onVerdictChange,
  selectedId,
  onSelect,
}: {
  verdict: Verdict;
  onVerdictChange: (verdict: Verdict) => void;
  selectedId: number | null;
  onSelect: (id: number) => void;
}) {
  const queryClient = useQueryClient();
  const [searchInput, setSearchInput] = useState("");
  const [search, setSearch] = useState("");
  const [mediaType, setMediaType] = useState("");
  const [requested, setRequested] = useState<RequestedFilter>("any");
  const [sort, setSort] = useState<SortKey>("score");
  const [order, setOrder] = useState<SortOrder>("desc");
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

  // Start over from the top whenever the list itself changes (a new tab, filter or sort), and
  // drop any selection -- a key picked on one tab is not visible on another.
  useEffect(() => setVisible(PAGE), [verdict, search, mediaType, requested, sort, order]);
  useEffect(() => setSelected(new Set()), [verdict, search, mediaType, requested, sort, order]);

  const { data: pages, isPending, error, hasNextPage, isFetchingNextPage, fetchNextPage } =
    useInfiniteQuery({
      queryKey: ["candidates", verdict, search, mediaType, requested, sort, order],
      queryFn: ({ pageParam }) =>
        api.candidates(
          verdict,
          { search, media_type: mediaType, requested, sort, order },
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

  const invalidate = () => void queryClient.invalidateQueries({ queryKey: ["candidates"] });
  const setOverride = useMutation({
    mutationFn: ({ key, decision }: { key: string; decision: Override }) =>
      api.override(key, decision),
    onSuccess: invalidate,
  });
  const clearOverride = useMutation({
    mutationFn: (key: string) => api.clearOverride(key),
    onSuccess: invalidate,
  });
  const bulk = useMutation({
    // allSettled, not Promise.all: Promise.all rejects on the first failed request and skips
    // onSuccess entirely, so a single 500 among fifty would leave ~49 already-applied changes
    // with the queue unrefreshed and the whole selection still showing stale verdicts. We
    // instead let every request settle, then always refresh and clear, and tally the failures.
    mutationFn: async ({ keys, decision }: { keys: string[]; decision: Override | null }) => {
      const results = await Promise.allSettled(
        keys.map((key) => (decision === null ? api.clearOverride(key) : api.override(key, decision))),
      );
      return results.filter((r) => r.status === "rejected").length;
    },
    onMutate: () => setBulkFailures(0),
    onSuccess: (failures) => {
      invalidate();
      setSelected(new Set());
      setBulkFailures(failures);
    },
  });
  // Build a plan for exactly the selected items and open the confirmation sheet. Nothing
  // deletes here -- the sheet is the gauntlet (dry run, arm check, typed phrase).
  const reapNow = useMutation({
    mutationFn: (keys: string[]) => api.createRun(keys),
    onSuccess: (run) => setReapRun(run),
  });
  const pending =
    setOverride.isPending || clearOverride.isPending || bulk.isPending || reapNow.isPending;

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
  const filtering = Boolean(search || mediaType || requested !== "any");
  // The override key each shown card acts on: a show's group key, or a movie's media key.
  const shownGroups = groups.slice(0, visible);
  const shownKeys = shownGroups.map((g) => (g.isShow ? g.key : g.items[0]!.media_key));
  const shownItems = shownGroups.reduce((n, g) => n + g.items.length, 0);
  const allShownSelected = shownKeys.length > 0 && shownKeys.every((k) => selected.has(k));

  // Keep the server buffer ahead of the render window: once revealed cards reach within a
  // render-page of everything fetched, pull the next server page so scrolling never stalls.
  useEffect(() => {
    if (hasNextPage && !isFetchingNextPage && visible >= groups.length - PAGE) {
      void fetchNextPage();
    }
  }, [visible, groups.length, hasNextPage, isFetchingNextPage, fetchNextPage]);

  // An empty list under a filter should explain itself, not read as broken. The common
  // surprise: "Requested" on the reap tab is empty because a requested title people
  // actually watched is protected, not reaped -- so point them at where it did land.
  const emptyMessage =
    requested === "yes" && verdict === "condemn"
      ? "Nothing people requested is on the reap list. A requested title that got watched is " +
        "protected, not reaped — look under Spared to see them."
      : filtering
        ? "Nothing matches those filters."
        : tab.empty;

  return (
    <section className="queue">
      {/* A view-level heading, for parity with Policy/Fairness/Settings so heading navigation
          can land on "Review queue" the way it lands on those views. */}
      <h2>Review queue</h2>
      <nav className="tabs" role="tablist">
        {TABS.map((t) => (
          <button
            key={t.verdict}
            role="tab"
            aria-selected={t.verdict === verdict}
            className={t.verdict === verdict ? "tab active" : "tab"}
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
            placeholder="Search titles and shows…"
            value={searchInput}
            onChange={(e) => setSearchInput(e.target.value)}
          />
        </div>

        <Pill icon={<LayersIcon />} value={mediaType} onChange={setMediaType} title="Movies or TV">
          {MEDIA_FILTERS.map((f) => (
            <option key={f.value} value={f.value}>
              {f.label}
            </option>
          ))}
        </Pill>

        <Pill
          icon={<FunnelIcon />}
          value={requested}
          onChange={(v) => setRequested(v as RequestedFilter)}
          title="Filter by who asked for it through Seerr"
        >
          {REQUESTED_FILTERS.map((f) => (
            <option key={f.value} value={f.value}>
              {f.label}
            </option>
          ))}
        </Pill>

        <div className="sort-group">
          <Pill icon={<SortIcon />} value={sort} onChange={(v) => setSort(v as SortKey)} title="Sort by">
            {SORTS.map((s) => (
              <option key={s.value} value={s.value}>
                {s.label}
              </option>
            ))}
          </Pill>
          <button
            className="sort-dir"
            onClick={() => setOrder((o) => (o === "desc" ? "asc" : "desc"))}
            title={order === "desc" ? "High to low" : "Low to high"}
            aria-label={order === "desc" ? "Descending" : "Ascending"}
          >
            <svg viewBox="0 0 16 16" width="14" height="14" fill="none" aria-hidden="true" className={order}>
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
              ? "Done selecting — clears your picks"
              : "Select several at once to spare or reap"
          }
        >
          <CheckSquareIcon />
          {selectMode ? "Done" : "Select"}
        </button>
      </div>

      {error && <p className="error">{error.message}</p>}
      {isPending && <p className="muted">Loading…</p>}

      {data && data.length === 0 && <p className="empty">{emptyMessage}</p>}

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
              const key = group.isShow ? group.key : group.items[0]!.media_key;
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
                  select={cardSelect}
                  onOpen={onSelect}
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
              disabled={shownKeys.length === 0}
              onClick={() =>
                setSelected((prev) => {
                  const next = new Set(prev);
                  if (allShownSelected) shownKeys.forEach((k) => next.delete(k));
                  else shownKeys.forEach((k) => next.add(k));
                  return next;
                })
              }
              title="Select (or clear) every card loaded"
            >
              {allShownSelected ? "Deselect all" : "Select all"}
            </button>
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
              <span aria-hidden="true">✕</span> Reap
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

      {bulkFailures > 0 && (
        <p className="error bulk-error">
          {count(bulkFailures)} {bulkFailures === 1 ? "item" : "items"} could not be updated — the
          rest were saved.
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
