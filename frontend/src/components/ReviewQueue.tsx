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

import { useInfiniteQuery, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  type CSSProperties,
  type PointerEvent as ReactPointerEvent,
  type ReactNode,
  type RefObject,
  memo,
  useCallback,
  useEffect,
  useId,
  useLayoutEffect,
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
  type ShowStatus,
  type SortKey,
  type Verdict,
} from "../api";
import { announce } from "../announce";
import { useBackGuard } from "../backnav";
import { REMOVES_ITS_ROW, useRemovalFocus } from "../focus";
import { bytes, count, itemBytes, spareRemaining, totalBytes } from "../format";
import type { Focus } from "../navIntent";
import { NARROW_SCREEN_QUERY, useMediaQuery } from "../useMediaQuery";
import { useOverrideMutations } from "../useOverrideMutations";
import { useReviewFreshness } from "../useReviewFreshness";
import { CardOpen } from "./CardOpen";
import { ReapConfirm } from "./ReapConfirm";
import { KeptByShowNote, OverrideControls, OverrideMark } from "./OverrideControls";
import { usePopoverShift } from "./popoverFit";
import {
  CaretIcon,
  CheckIcon,
  CheckSquareIcon,
  ClockGlyph,
  FunnelIcon,
  GenreIcon,
  LayersIcon,
  LibraryIcon,
  OverrideIcon,
  PlusIcon,
  ScytheIcon,
  SpareGlyph,
  SelectTick,
  SortIcon,
} from "./queueIcons";
import {
  DEFAULT_FILTERS,
  loadFilters,
  MEDIA_FILTERS,
  OVERRIDE_FILTERS,
  REQUESTED_FILTERS,
  saveFilters,
  SORTS,
  type FilterDimension,
  type QueueFilters,
} from "./queueFilters";
import {
  QueueSettingsContext,
  shouldExpandSeasons,
  useHoldsBackUnmeasured,
  type QueueSettings,
} from "./queueSettings";
import {
  groupReapEffective,
  handFate,
  isCondemned,
  reapIsNoop,
  showReapIsNoop,
  showReapReach,
  showReapReaches,
  type Fate,
} from "./reviewFate";
import { chipWhy, CondemnedChip, OverrideChip, StatusChip } from "./StatusChip";
import { staleReadLine, StaleReadNotice } from "./StaleReadNotice";

//: How many cards to *render* at a time. A tab can hold thousands, so we draw a screenful and
//  reveal more as you scroll -- keeping the DOM (and the lazy poster fetches) small.
/** What the two scan-freshness surfaces say, in one place because each is said twice now: once
 *  on screen and once into the shared live region. Neither had a voice before -- both were bare
 *  `role="status"` nodes mounted with their own text, which several readers never announce -- and
 *  a hand-copied sentence in the announcement would be a second copy of an operator-facing claim
 *  free to drift from the one on screen (rule 144). */
const NUDGE_NEWER_SCAN = "A newer scan just finished";
const NUDGE_VIEWING_PREVIOUS = "You're viewing the previous scan.";
const TOAST_CAUGHT_UP = "Updated to the latest scan.";

const PAGE = 40;

//: How many candidates to *fetch* per request. The server pages the query (the review queue of
//  a real library runs to thousands of protected titles), and we pull the next page in as the
//  render window nears the end of what we have.
const FETCH_PAGE = 100;

const TABS: { verdict: Verdict; label: string; blurb: string; empty: string }[] = [
  {
    verdict: "condemn",
    label: "Condemned",
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

// --- remembered filters --------------------------------------------------------------------
// Each queue tab keeps its own filters and sort, on this device, until changed or cleared.

// --- little inline icons for the filter/sort pills ------------------------------------------

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
      <button {...REMOVES_ITS_ROW} type="button" aria-label={clearLabel} onClick={onClear}>
        ×
      </button>
    </span>
  );
}

/** Both filter popovers -- the ＋ Filter menu and a chip's value picker -- in one component, so
 *  neither can drift from the other.
 *
 *  The menu is absolutely positioned inside its `.filter-anchor`, which is what keeps it glued to
 *  its control as the page scrolls. Left-aligned to that anchor it ran clean off the right edge of
 *  a phone screen -- the ＋ Filter button sits at the end of the toolbar row, and a wrapped chip
 *  can land there too -- so `usePopoverShift` slides it back (see `popoverFit.ts`). */
function FilterMenu({
  id,
  label,
  children,
}: {
  /** Pointed at by its trigger's `aria-controls`, which is the only thing tying the two
   *  together: the popover is a sibling of the button, not a descendant. */
  id: string;
  /** Names the menu for a screen reader, and heads it for everyone else. */
  label: string;
  children: ReactNode;
}) {
  const ref = useRef<HTMLUListElement>(null);
  const shift = usePopoverShift(ref, "filter-anchor");

  // A plain list behind a disclosure, deliberately: this took `role="menu"` and `role="listbox"`
  // from a prop, and implemented neither one's keyboard contract -- no arrow keys, no roving
  // focus, no `aria-activedescendant`, and every option a separate Tab stop, which is not the
  // listbox pattern at all. A listbox is ANNOUNCED as an arrow-key widget, so the roles were
  // telling an operator to press keys that did nothing. App's UserMenu records the same defect
  // being fixed the same way, and PolicyRuleEditors' combobox shows what keeping the role costs.
  return (
    <ul
      ref={ref}
      id={id}
      className="filter-menu"
      aria-label={label}
      style={{ "--pop-shift": `${shift}px` } as CSSProperties}
    >
      <li className="filter-menu-head" aria-hidden="true">
        {label}
      </li>
      {children}
    </ul>
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
          <path
            d="M8 5v14M16 5v14M4 9h4M16 9h4M4 15h4M16 15h4"
            stroke="currentColor"
            strokeWidth="1.2"
          />
        </svg>
      </div>
    );
  }
  return (
    <img className="poster" src={url} alt={alt} loading="lazy" onError={() => setBroken(true)} />
  );
}

/** The score chip. Color carries the item's fate so it reads without the label. */
function Score({ item }: { item: Candidate }) {
  return (
    <span className={`score score-${handFate(item)}`} title={`Score ${item.score} of 100`}>
      {item.score}
    </span>
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

/** How a card participates in Select mode. When ``selectMode`` is off these are inert and the
 *  card behaves normally (click opens the why-panel); when on, the whole card is a selection
 *  target -- press to toggle, drag across to paint a run.
 *
 *  Everything here is the SAME for every card, so the queue builds one of these and passes it
 *  to all of them. Whether a given card is picked is its own `isSelected` prop, not a field in
 *  here: with it folded in, the object was rebuilt per card per render and no card could be
 *  memoized -- so painting one card re-rendered every drawn card (P-1). */
type CardSelect = {
  selectMode: boolean;
  onSelectDown: (key: string, e: ReactPointerEvent) => void;
  onSelectEnter: (key: string) => void;
  // Keyboard activation (Enter/Space) toggles a single card *without* arming a drag. A key
  // press emits no pointerup, so routing it through onSelectDown would leave the drag mode
  // stuck and paint every card a later mouse-hover crossed.
  onSelectToggle: (key: string) => void;
};

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
  const heldBack = useHoldsBackUnmeasured().holdsBack && unmeasured;
  // A condemned row carries no chip by construction, so the moment a hand spare flips
  // `condemned` false the card would lose this line outright and reflow under the cursor --
  // the live reflow the in-place patch exists to prevent. The dormancy fact is true on every
  // lane, so it stands in when there is no chip to show.
  if (!condemned)
    return chip ? <StatusChip chip={chip} /> : <DormantPill dormantFor={dormantFor} />;
  return (
    <>
      <DormantPill dormantFor={dormantFor} />
      {heldBack && <p className="card-reason">Held back: size unknown</p>}
      {reason && !dormantFor && !heldBack && <p className="card-reason">{reason}</p>}
    </>
  );
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
        // in styles/20-queue-cards.css and wins).
        const handClass =
          fate === "spare"
            ? " strip-ov-spare"
            : fate === "spare-expired"
              ? " strip-ov-spare-expired"
              : fate === "reap"
                ? " strip-ov-reap"
                : fate === "refused"
                  ? " strip-ov-reap-refused"
                  : "";
        const overrideNote =
          fate === "spare-expired"
            ? ", you spared it and that spare has expired"
            : mark.override === "spare"
              ? ", you spared it"
              : reapRefused
                ? ", reap requested but it is kept for now"
                : mark.override === "reap"
                  ? ", you reaped it by hand"
                  : "";
        // The lane word follows the EFFECTIVE fate (handFate), not the raw verdict, so a spared
        // condemnation reads "kept, you spared it" and never "would be removed, you spared it".
        // An expired spare is still "kept" for the same reason its square stays green: only a
        // scan realizes the clock, so nothing will reap it before then (rule 61).
        const lane =
          fate === "reap"
            ? MARK_LABELS.condemn
            : fate === "spare" || fate === "spare-expired" || fate === "refused"
              ? MARK_LABELS.protect
              : (MARK_LABELS[mark.verdict] ?? mark.verdict);
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
              // The whole card head still opens the show on a click; a square opens just its
              // season. No key guard beside it any more: the head's `role="button"` and its
              // Enter/Space handler are gone (#169), so nothing above this cancels the button's
              // own activation, and a guard against a handler that does not exist is a comment
              // claiming a safeguard (rule 7/24).
              e.stopPropagation();
              onOpen(mark.id);
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
        // A click still bubbles into the head, which opens the show, so it stops here.
        // Enter/Space needs no guard: the head no longer handles keys at all (#169).
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

/** "Show Title, Season 3" -> "Season 3".
 *
 *  Three separators, because a title is FROZEN into the snapshot that scanned it and a
 *  database predates whichever form the current code writes. The comma is what
 *  `season_scan.py` writes now; the middot is what it wrote until the screen-reader pass
 *  (a middot is decoration a reader may voice as "middle dot" mid-sentence), and the dash
 *  is older still. Dropping either older form would leave the show's name doubled on every
 *  season row of every library scanned before the change, until a rescan. */
function seasonName(title: string, showTitle: string): string {
  return title
    .replace(`${showTitle}, `, "")
    .replace(`${showTitle} · `, "")
    .replace(`${showTitle} — `, "");
}

/** Capitalize the first letter and end the clause as a sentence -- turns a lowercase "why"
 *  clause ("it couldn't be found in Plex") into a standalone reason line. */
function capitalizeSentence(text: string): string {
  const t = text.charAt(0).toUpperCase() + text.slice(1);
  return /[.!?]$/.test(t) ? t : `${t}.`;
}

/** The one-time banner atop an expanded show whose WHOLE-SHOW decision (spare or reap) drives
 *  its seasons. It states that decision once so the rows below no longer each repeat it -- every
 *  inheriting season used to carry an identical `KeptByShowNote`, which read as a wall of the
 *  same red sentence. The mark and tint track the inherited fate (green spare, red reap) so the
 *  banner never disagrees with the scores beneath it. Shown only when `show_override` is set.
 *
 *  A spare's mark and words describe the spare actually IN FORCE, not the shape of a spare in
 *  general: ∞ only for a forever one, the clock (dashed once its time is up) for a timed one.
 *  A fixed ∞ said "kept forever" directly above a control reading "30d", and went on saying it
 *  after the clock passed. The expired wording is the one that has to be careful: the seasons
 *  really are still kept, because only a scan realizes a spare's expiry, so it says what
 *  happened without claiming the seasons are back on the block (rule 61). */
function ShowInheritBanner({
  override,
  spareExpiresAt,
  reapReach,
}: {
  override: Override;
  /** When the SHOW's own spare stops keeping its seasons (ISO), null for a forever spare.
   *  Read only on a spare banner, alongside the show decision it belongs to. */
  spareExpiresAt: string | null;
  reapReach: "all" | "some" | "none";
}) {
  const reap = override === "reap";
  const remaining = spareRemaining(spareExpiresAt);
  return (
    <div className={`show-inherit ${reap ? "show-inherit-reap" : "show-inherit-spare"}`}>
      <span className="show-inherit-mark" aria-hidden="true">
        {reap ? (
          <ScytheIcon />
        ) : remaining.forever ? (
          <span className="mk-inf">∞</span>
        ) : (
          <ClockGlyph dashed={remaining.expired} />
        )}
      </span>
      <span>
        {reap ? (
          // The header must not assert removal the engine can't honor: with every inherited reap
          // held, the seasons are kept; with a mix, only some go (rule 61). The per-row chips
          // still mark each held season, so this just stops the header contradicting them (U-2).
          reapReach === "none" ? (
            <>
              <b>The whole show is set to reap.</b> The reap is noted, but the seasons are kept for
              now.
            </>
          ) : reapReach === "some" ? (
            <>
              <b>The whole show is set to reap.</b> Reaper removes the seasons it can, unless you
              spare them here. The rest are kept for now.
            </>
          ) : (
            <>
              <b>The whole show is set to reap.</b> Every season below is removed unless you spare
              it here.
            </>
          )
        ) : remaining.expired ? (
          // Still kept, and it must say so: only a scan realizes a spare's clock, so until one
          // runs nothing will remove these seasons (rule 61). What has run out is the decision.
          <>
            <b>The whole show's spare has run out.</b> The seasons are still kept until the next
            scan judges them again.
          </>
        ) : (
          <>
            <b>The whole show is spared{remaining.short ? `, ${remaining.phrase}` : ""}.</b> Every
            season below is kept unless you reap it here.
          </>
        )}
      </span>
    </div>
  );
}

/** What a season row says for ITSELF inside a show the header already explains. The banner states
 *  the inherited fate once, so a plain-inheriting season -- or one whose own decision agrees with
 *  its show -- carries nothing here and reads from its score's color alone. Only a season that
 *  goes its own way earns words:
 *    - a HELD reap (the engine can't honor it yet, own or inherited): a dashed-red "Kept for now"
 *      chip plus the reason it is held (the same "why" the refused OverrideChip would carry);
 *    - a season the owner decided AGAINST its show: a solid chip and a one-line "kept / removed
 *      anyway" -- dashed green, and saying so, when the spare doing the keeping has run out.
 *  The chips reuse the shared `.status-chip` tones (rule 18) and the score's color still comes
 *  from `handFate`, so the two can't disagree (rule 49). Only reached when the show has an
 *  override; a season with no whole-show decision keeps its own scan chip back in SeasonList. */
function seasonDivergence(
  season: Candidate,
  showOverride: Override,
): { chip: ReactNode; reason: string | null } {
  // A held reap is noted but the file is still kept -- dashed red, never the solid red of a
  // removal. Judged by the item's own fate, so an inherited held reap reads the same as an own one.
  if (handFate(season) === "refused") {
    // The specific reason the engine held it (from the stored explanation) beats the chip's
    // short phrase, which beats a generic line -- so the row says WHY, e.g. that the season it
    // was compared against is kept because Sonarr is still downloading it.
    const why =
      season.reason ?? chipWhy(season.chip) ?? "Reaper couldn't confirm it's safe to remove";
    return {
      chip: <span className="status-chip status-reap-held">Kept for now</span>,
      reason: capitalizeSentence(why),
    };
  }
  const own = season.override_own;
  // No own decision, or one that agrees with the show: the header covers it, and the control's
  // own lit/unlit state is the only extra signal an agreeing own decision needs (rule 50).
  if (own == null || own === showOverride) return { chip: null, reason: null };
  // Decided against the show: it goes the opposite way, and says so beside its control.
  if (own === "spare") {
    // The spare's own three states again, because this chip sits inches from the score badge
    // and the strip square that already draw them (rule 49). A spent spare wears the dashed
    // green, not the solid "you chose this and it holds" -- solid beside a dashed badge on one
    // row is the row disagreeing with itself. It still keeps the season either way, so the
    // sentence beneath is the same one; what changes is that it says the spare ran out, and
    // that a scan is what ends it (rule 61).
    const expired = handFate(season) === "spare-expired";
    return {
      chip: (
        <span className={`status-chip ${expired ? "status-spare-expired" : "status-hand-spare"}`}>
          {expired ? "Spare expired" : "Spared"}
        </span>
      ),
      reason: expired
        ? "Kept even though the whole show is set to reap. Your spare ran out, so the next scan judges it again."
        : "Kept even though the whole show is set to reap.",
    };
  }
  return {
    chip: <span className="status-chip status-hand-reap">Reaped</span>,
    reason: "Removed even though the whole show is spared.",
  };
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
  busyKey,
}: {
  groupKey: string;
  selectedId: number | null;
  onOpen: (id: number) => void;
  onSet: (key: string, decision: Override, spareDays?: number) => void;
  onClear: (key: string) => void;
  /** The whole show's wait: a list-wide write, or the show's own decision going out. Both cover
   *  every season, so they still disable these rows. */
  pending: boolean;
  /** The one row being written app-wide. A season writes its own `media_key`, so this is the
   *  only way it can know the wait is its own (rule 72: `MovieCard` and the whole-show control
   *  were already keyed, these rows were not). */
  busyKey: string | null;
}) {
  // One request per expanded show, and with "Expand seasons by default" on that is one per
  // drawn card -- unbounded as the render window grows, and fired all over again every time
  // entering or leaving Select mode remounts the lists (P-2). The five minutes is the same
  // staleTime the sibling vocabulary queries use: a show's seasons only change when a scan
  // lands, and a scan invalidates ["group"] outright (ScanBar), as does every override.
  const { data, isPending, error } = useQuery({
    queryKey: ["group", groupKey],
    queryFn: () => api.group(groupKey),
    staleTime: 5 * 60 * 1000,
  });

  // The list is an always-visible surface once expanded: say "loading" and "failed"
  // out loud rather than rendering nothing under an open chevron.
  //
  // `!data` alone, never `error || !data`: ["group", …] is override-aware, so sparing ONE season
  // refetches it, and an undivided `error` traded every season row -- each with its own Spare and
  // Reap -- for one red line while React Query still held the last good list (#190). A failed
  // refetch says so above the rows instead, in the list's own `.season-list-note` grammar, which
  // its loading and failed lines already speak. Not a rule: nothing keeps a `.notice` out of a
  // review surface, and the queue one screen out renders one.
  if (isPending) {
    return <p className="season-list-note muted">Loading seasons…</p>;
  }
  if (!data) {
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
  const anyReapable = data.seasons.some((s) => !reapIsNoop(s));
  // A whole-show decision covers every season here. When set, the header states it once and each
  // row only speaks up if it diverges (seasonDivergence); when null, every row wears its own scan
  // or hand chip as before. `show_override` is a property of the show, so all seasons share it.
  const showOverride = data.show_override;
  return (
    <>
      {error && <p className="season-list-note stale">{staleReadLine("the seasons")}</p>}
      {showOverride && (
        <ShowInheritBanner
          override={showOverride}
          // The SHOW's own spare, matching the show decision the banner states -- never a
          // season's, which the rows below carry themselves (rule 50).
          spareExpiresAt={data.show_spare_expires_at}
          reapReach={showReapReach(data.seasons)}
        />
      )}
      <ul
        className="season-list"
        style={
          {
            // Both widths derive from --ov-btn-w / --ov-btn-gap (styles/00-tokens.css), so a button-width
            // change lands in one place and the columns can't drift (H-1, rule 16).
            "--btns": anyReapable
              ? "calc(2 * var(--ov-btn-w) + var(--ov-btn-gap))"
              : "var(--ov-btn-w)",
          } as CSSProperties
        }
      >
        {data.seasons.map((season) => {
          const divergence = showOverride ? seasonDivergence(season, showOverride) : null;
          const reason = divergence?.reason ?? null;
          // With a whole-show decision the header explains the inherited fate, so a row's chip is
          // whatever `seasonDivergence` returns (often nothing). Without one, the row wears its own
          // decision, the condemned mark, or its scan chip -- one pill, the truth.
          let chip: ReactNode;
          if (divergence) {
            chip = divergence.chip;
          } else if (season.override !== null) {
            chip = (
              <OverrideChip
                override={season.override}
                effective={season.override_effective}
                keptWhy={chipWhy(season.chip)}
                spareCoversUntil={season.spare_covers_until}
                // The season list's own class family, exactly like ShowPanel's SeasonPill and
                // the row's other chips (StatusChip/CondemnedChip). The `.chip` default sits on
                // a card's meta line and does NOT clamp, so a long held-reap pill overflowed the
                // title column into the fixed button track; `.status-chip` ellipsizes in place.
                family="status-chip"
              />
            );
          } else if (season.verdict === "condemn") {
            chip = <CondemnedChip />;
          } else {
            chip = <StatusChip chip={season.chip} />;
          }
          return (
            <li
              key={season.id}
              className={`season-row clickable ${
                season.override === "spare"
                  ? "card-spared"
                  : season.override === "reap"
                    ? "card-reaped"
                    : ""
              } ${season.id === selectedId ? "card-selected" : ""} ${reason ? "has-reason" : ""}`}
              // A plain `<li>`. It carried `role="button"`, which stripped `listitem` off every
              // row -- so the list announced no item count -- and pruned the row's score, chip
              // and reason line out of the tree, on the row where a per-season keep-or-delete
              // decision is made (#169). `CardOpen` on the season name is the control.
              onClick={() => onOpen(season.id)}
            >
              <Score item={season} />
              <span className="season-title">
                <CardOpen
                  name={`Why ${seasonName(season.title, data.title)} scored ${season.score}`}
                  onActivate={() => onOpen(season.id)}
                >
                  <span className="season-name">{seasonName(season.title, data.title)}</span>
                </CardOpen>
                {chip}
              </span>
              {/* The control toggles the season's OWN decision (override_own), never the one it
                  inherits from its show (rule 50). Reap is dropped only when this season's own
                  verdict is condemn -- reaping it changes nothing (rule 51); Spare is never
                  dropped. A divergent season's reason line below says how it goes its own way. */}
              <OverrideControls
                override={season.override_own}
                onSet={(d, sd) => onSet(season.media_key, d, sd)}
                onClear={() => onClear(season.media_key)}
                pending={pending || busyKey === season.media_key}
                hideReap={reapIsNoop(season)}
                // Safe to pass the effective expiry beside `override_own`: a season with no
                // decision of its own carries its SHOW's expiry here, but its button is not in
                // the spared state and never reads it (rule 50, and the prop's own doc).
                spareExpiresAt={season.spare_expires_at}
              />
              <span className="season-size num">{itemBytes(season.size_bytes)}</span>
              {reason && <p className="season-reason">{reason}</p>}
            </li>
          );
        })}
      </ul>
    </>
  );
}

// Memoized: with every prop below stable or scalar, a card re-renders only when something it
// actually shows has changed. Painting a drag across a long list used to re-render every drawn
// card once per `pointerenter` (P-1).
const MovieCard = memo(function MovieCard({
  item,
  selected,
  isSelected,
  select,
  onOpen,
  onSet,
  onClear,
  pending,
  hideReap,
}: {
  item: Candidate;
  /** The open card -- the one whose reasoning the panel is showing. */
  selected: boolean;
  /** Picked in Select mode. A different question from `selected`, and the only per-card part
   *  of selection, which is why it is not inside `select`. */
  isSelected: boolean;
  select: CardSelect;
  onOpen: (id: number) => void;
  onSet: (key: string, decision: Override, spareDays?: number) => void;
  onClear: (key: string) => void;
  pending: boolean;
  hideReap: boolean;
}) {
  const state =
    item.override === "spare" ? "card-spared" : item.override === "reap" ? "card-reaped" : "";
  const { selectMode } = select;
  return (
    <article
      className={`card clickable ${state} ${selected ? "card-selected" : ""} ${
        selectMode ? "card-select" : ""
      } ${isSelected ? "card-picked" : ""}`}
      // A plain container: the control that opens it is `CardOpen` on the title below, and the
      // click here is the redundant mouse affordance beside it (#169). Carrying `role="button"`
      // pruned every chip, reason and season mark on the card out of the accessibility tree.
      onClick={() => !selectMode && onOpen(item.id)}
      onPointerDown={(e) => selectMode && select.onSelectDown(item.media_key, e)}
      onPointerEnter={() => selectMode && select.onSelectEnter(item.media_key)}
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
            <CardOpen
              name={selectMode ? `Select ${item.title}` : `Why ${item.title} scored ${item.score}`}
              pressed={selectMode ? isSelected : undefined}
              pressHandledByCard={selectMode}
              onActivate={() =>
                selectMode ? select.onSelectToggle(item.media_key) : onOpen(item.id)
              }
            >
              {item.title}
            </CardOpen>
            {/* Outside the control, so the control's visible text stays exactly the string its
                name contains (WCAG 2.5.3). */}
            {item.year && <span className="card-year"> {item.year}</span>}
          </h3>
          <OverrideChip
            override={item.override}
            effective={item.override_effective}
            keptWhy={chipWhy(item.chip)}
            spareCoversUntil={item.spare_covers_until}
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
              spareExpiresAt={item.spare_expires_at}
            />
          </>
        )}
      </div>
    </article>
  );
});

const ShowCard = memo(function ShowCard({
  group,
  defaultOpen,
  selectedId,
  selectedGroupKey,
  isSelected,
  select,
  onOpen,
  onOpenGroup,
  onSet,
  onClear,
  pending,
  busyKey,
}: {
  group: Group;
  /** Whether the season list starts expanded, from the operator's General preference.
   *  Only the STARTING state -- a click on this card's season pill still wins. */
  defaultOpen: boolean;
  selectedId: number | null;
  selectedGroupKey: string | null;
  /** Picked in Select mode -- the only per-card part of selection, hence not in `select`. */
  isSelected: boolean;
  select: CardSelect;
  onOpen: (id: number) => void;
  onOpenGroup: (key: string) => void;
  onSet: (key: string, decision: Override, spareDays?: number) => void;
  onClear: (key: string) => void;
  pending: boolean;
  /** The single row currently being written, app-wide, or null. This card's own controls read
   *  `pending`; the season rows below need the key itself, because each writes its own
   *  `media_key` and no boolean computed from the show's key can speak for them. */
  busyKey: string | null;
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
  // The whole show's marks, across every lane -- `marks`, not the tab-filtered `group.items`
  // (on the Condemned lane that holds only this show's reaped/condemned seasons). Used for the
  // strip and for whether a whole-show reap would change anything.
  const showSeasons = marks ?? group.items;
  // The show's OWN decision -- the show key the whole-show control toggles, read straight from
  // the row, never rolled up from the seasons' own marks. The control clears only this key, so
  // lighting it from an aggregate it cannot clear was a dead toggle. Seasons overridden one by
  // one keep their marks in the strip; this stays null until the whole show is decided.
  const showOverride = first.show_override;
  // A whole-show decision settles the card's story before the seasons' verdicts do, because
  // `patchShowOverride` deliberately leaves each season's own `override` alone. Reading the
  // seasons here left a card tinted "spared", chipped "will be kept", and still saying "3 of 5
  // would be removed" one line below, all session (rule 61).
  const isReapTab =
    showOverride === "spare" ? false : showOverride === "reap" ? true : isCondemned(first);
  // How many seasons carry no size, over whichever set the card is describing. The planner
  // holds an unmeasured season back, so it is counted apart from the removal count rather
  // than folded into it -- the same split the server's rollup makes.
  const unknownSeasons = marks ? wholeShowUnknown : fetchedUnknown;
  // What the removal line counts, and which source is right turns on whose decision put the
  // card on the reap side:
  //   - the scan's own condemnation: the server's whole-snapshot rollup, which is the exact
  //     set "Reap now" would plan (rule 30/62);
  //   - a hand reap on the WHOLE show: the show's own season marks, because the rollup still
  //     describes the set from BEFORE that decision and cannot catch up on its own.
  // The second case needs saying because `patchShowOverride` deliberately refetches nothing
  // (refetchType "none"): on a Sanctuary or Limbo show the rollup is a real 0, so reading it
  // printed "0 of 5 would be removed, 0 B" under a "will be removed" chip for the rest of
  // the session. Refetching instead would fix every figure and re-bucket the show out from
  // under the operator mid-review, the one thing the in-place patch exists to prevent.
  //
  // The marks are filtered through `showReapReaches`, NOT counted whole: a whole-show
  // decision does not take a season the operator spared individually, nor one the engine
  // refuses to reap. That helper says why, and keeps this number, the chip and the strip
  // squares reading the same `handFate` (rules 49/61).
  const reapsWholeShow = showOverride === "reap";
  const reapedMarks = reapsWholeShow ? (marks ?? []).filter(showReapReaches) : [];
  const reapedUnknown = reapedMarks.filter((m) => m.size_bytes === null).length;
  const condemnedCount = reapsWholeShow
    ? reapedMarks.length - reapedUnknown
    : (first.group_condemned_count ?? group.items.length);
  const condemnedBytes = reapsWholeShow
    ? reapedMarks.reduce((sum, m) => sum + (m.size_bytes ?? 0), 0)
    : (first.group_condemned_bytes ?? fetchedSize);
  const condemnedUnknown = reapsWholeShow
    ? reapedUnknown
    : (first.group_unknown_size ?? fetchedUnknown);
  const state =
    showOverride === "spare" ? "card-spared" : showOverride === "reap" ? "card-reaped" : "";
  const { selectMode } = select;

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
        // A plain container, for the reason MovieCard's is: the head held a `role="button"`, and
        // it holds the season strip, the removal count and the expander, every one of which was
        // pruned out of the accessibility tree by it (#169). `CardOpen` on the title is the
        // control; this click is the redundant mouse affordance.
        onClick={() => !selectMode && onOpenGroup(group.key)}
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
              <CardOpen
                name={selectMode ? `Select ${group.title}` : `About ${group.title}`}
                pressed={selectMode ? isSelected : undefined}
                pressHandledByCard={selectMode}
                onActivate={() =>
                  selectMode ? select.onSelectToggle(group.key) : onOpenGroup(group.key)
                }
              >
                {group.title}
              </CardOpen>
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
              // A show has no parent to inherit a longer spare from, so its own expiry IS the
              // covering one -- unlike a season's, which this chip must never read (rule 50).
              spareCoversUntil={first.show_spare_expires_at}
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
                ? `${condemnedCount} of ${totalSeasons} would be removed, ${totalBytes(condemnedBytes, condemnedUnknown)}`
                : totalBytes(wholeShowBytes ?? fetchedSize, unknownSeasons)}
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
                // The show's OWN spare, to match the show-level decision the button reflects.
                spareExpiresAt={first.show_spare_expires_at}
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
          busyKey={busyKey}
        />
      )}
    </article>
  );
});

export function ReviewQueue({
  verdict,
  onVerdictChange,
  selectedId,
  selectedGroupKey,
  onSelect,
  onSelectGroup,
  onClearItemSelection,
  stepRef,
  latestScanSnapshotId = null,
  focus = null,
}: {
  verdict: Verdict;
  onVerdictChange: (verdict: Verdict) => void;
  /** What a jump into this queue aimed it at (navIntent.ts): the search box's contents, so the
   *  list behind an opened panel is the title that was opened rather than the whole lane.
   *  Acted on once per `nonce`, so returning to Review later does not re-seed the box. */
  focus?: Focus<{ search: string }> | null;
  selectedId: number | null;
  selectedGroupKey: string | null;
  onSelect: (id: number) => void;
  onSelectGroup: (key: string) => void;
  /** Close an open why-panel. Called from Show latest, whose new snapshot makes the panel's
   *  candidate id stale (B-7). Optional so the queue still renders without the app shell. */
  onClearItemSelection?: () => void;
  /** Filled in with a way to move the open card one place up or down this list, for the
   *  keyboard review loop. The queue owns the order, so it owns the walk. */
  stepRef?: RefObject<((delta: 1 | -1) => void) | null>;
  /** The newest completed scan's snapshot, from the polled scan status. When it moves past
   *  the snapshot this list is showing, a scan has landed a fresher one underneath. */
  latestScanSnapshotId?: number | null;
}) {
  // Seeded from the jump that opened this queue, not set by an effect afterwards: the queue is
  // unmounted while the operator is on Scales, so a jump from there mounts it, and an effect
  // would let one unfiltered request for the whole lane go out before the seeded one replaced
  // it. `search` is seeded alongside `searchInput` for the same reason -- it is the debounced
  // copy the query is keyed on, and waiting 250 ms for it would draw the lane first.
  const [searchInput, setSearchInput] = useState(focus?.search ?? "");
  const [search, setSearch] = useState(focus?.search ?? "");
  // The last jump this queue has already acted on. Seeded with the one it mounted under, so the
  // effect below only ever handles a jump that arrives while it is already on screen.
  const handledFocus = useRef<number | null>(focus?.nonce ?? null);
  const [filters, setFilters] = useState<QueueFilters>(() => loadFilters(verdict));
  // Each tab remembers its own filters, and the new tab's set is adopted DURING the render
  // that brings the new verdict in -- React's supported "adjust state when a prop changes"
  // pattern. Doing it in an effect instead paired the new verdict with the old tab's filters
  // for one commit: switching from Condemned with Genre set to Sanctuary fired
  // `?verdict=protect&genre=...`, drew that wrong list, and only then fired the right
  // request, so every such switch flashed a wrong page and made the server answer twice
  // (B-30). A render-phase update is discarded before it commits, so neither the query nor
  // the DOM ever sees the mismatched pair.
  const [filtersVerdict, setFiltersVerdict] = useState(verdict);
  if (filtersVerdict !== verdict) {
    setFiltersVerdict(verdict);
    setFilters(loadFilters(verdict));
  }
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
  const menuIdBase = useId();
  // The control that opened the popover, so closing it can hand focus back rather than dropping
  // it on <body> -- where the next Tab restarts at the top of the page, above the whole queue.
  const menuTriggerRef = useRef<HTMLButtonElement | null>(null);
  const openMenuFrom = (id: string, trigger: HTMLButtonElement) => {
    menuTriggerRef.current = trigger;
    setOpenMenu((m) => (m === id ? null : id));
    if (openMenu === id) trigger.focus();
  };
  const closeMenu = () => {
    setOpenMenu(null);
    menuTriggerRef.current?.focus();
  };
  // Where focus goes after a pick that takes its own trigger away with it. The ＋ Filter button
  // renders only while something is still addable, so adding the LAST dimension unmounts the very
  // button `closeMenu` hands focus back to: `.focus()` lands on a node React removes in the next
  // commit and focus falls to <body>, which is the failure this plumbing exists to prevent. The
  // chip the pick just created is the honest successor, and it does not exist until that commit,
  // so the focus waits for one.
  const focusChip = useRef<string | null>(null);
  // The other direction: removing a chip destroys the × holding focus, so the operator lands on
  // `<body>` and the next Tab restarts above the toolbar (#173). Focus goes to the next chip's
  // ×, or to the ＋ Filter button once the row is empty -- which always exists after a removal,
  // since removing a filter is exactly what makes that dimension addable again.
  const addFilterRef = useRef<HTMLButtonElement>(null);
  const chips = useRemovalFocus(addFilterRef);
  useLayoutEffect(() => {
    const id = focusChip.current;
    if (id === null) return;
    focusChip.current = null;
    document.querySelector<HTMLButtonElement>(`.fchip-body[data-dim="${id}"]`)?.focus();
  });
  // Back closes an open filter popover, then the reap-confirm sheet, before leaving the app.
  useBackGuard(openMenu !== null, closeMenu);
  useBackGuard(reapRun !== null, () => setReapRun(null));
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

  // A jump that lands on a queue already on screen -- ShowPanel's season list is the one inside
  // Review today, and it deliberately sends no search. Both halves are set, skipping the
  // debounce, so the list the operator lands on is the filtered one and not the lane first.
  useEffect(() => {
    if (!focus || focus.nonce === handledFocus.current) return;
    handledFocus.current = focus.nonce;
    setSearchInput(focus.search);
    setSearch(focus.search.trim());
  }, [focus]);

  // Close an open filter menu on an outside click or Escape. Each menu lives inside a
  // .filter-anchor wrapper (with its chip or button), so a click within any of them counts as
  // inside; switching to a different chip is handled by that chip's own toggle.
  useEffect(() => {
    if (openMenu === null) return;
    const onDown = (e: MouseEvent) => {
      // A click elsewhere is the operator choosing where to be, so this one only closes.
      if (!(e.target as HTMLElement).closest(".filter-anchor")) setOpenMenu(null);
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== "Escape") return;
      // The popover is the newest layer, so it consumes the press: `document` bubbles on to
      // `window`, where an open `.why` panel's Escape sits (WhyShell), and the queue and that
      // panel are on screen together in split view. The spare-length menu stops the same key for
      // the same reason (rule 72).
      e.stopPropagation();
      setOpenMenu(null);
      menuTriggerRef.current?.focus();
    };
    document.addEventListener("mousedown", onDown);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onDown);
      document.removeEventListener("keydown", onKey);
    };
  }, [openMenu]);

  // Remembering is all this effect does now: the adoption happens during render above, so by
  // the time any effect runs `filters` already belongs to `verdict` and there is no render to
  // skip. Writing the pair unconditionally is what makes a tab's set stick.
  useEffect(() => {
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

  const {
    data: pages,
    isPending,
    error,
    hasNextPage,
    isFetchingNextPage,
    fetchNextPage,
  } = useInfiniteQuery({
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
  const data = useMemo(() => (pages ? pages.pages.flatMap((p) => p.items) : undefined), [pages]);
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
    // Fails closed on an empty selection rather than posting one: an omitted key list means
    // "the whole condemned set" to the route, so a selection that emptied out (a filter, a
    // race with a refresh) must never widen into a whole-library plan. The disabled button
    // above is a convenience, not the control.
    mutationFn: (keys: string[]) => {
      if (keys.length === 0) throw new Error("Nothing is selected, so there is nothing to reap.");
      return api.createRun(keys);
    },
    onSuccess: (run) => setReapRun(run),
  });
  // A write that covers the WHOLE list, so nothing anywhere may be pressed while it is in
  // flight. The single-row writes are deliberately not here (see `pendingFor`).
  const blocking =
    bulk.isPending ||
    reapNow.isPending ||
    // Acting while the rest of the list is still arriving would act on part of what the
    // operator asked for, under a button that says "everything matching".
    selectEverything.isPending;
  // Which single row is being written, if any. One mutation instance serves every row, so the
  // key it is carrying is the row that owns the wait.
  const busyKey = setOverride.isPending
    ? setOverride.variables.key
    : clearOverride.isPending
      ? clearOverride.variables
      : null;
  /** Whether THIS row's controls are disabled, which used to be whether ANY row's were.
   *
   *  Disabling the focused element drops focus to `<body>` in every major browser, and the
   *  `aria-pressed` flip on the Spare button is the app's only announcement that a spare
   *  succeeded -- so by the time the state settled there was no focused element to announce it,
   *  and the press that KEEPS a file confirmed itself to nobody (#173). Scoping the wait to the
   *  row doing the writing restores that for free, and it was never right anyway: one row's
   *  in-flight spare has nothing to say about another row's Reap. */
  const pendingFor = (key: string) => blocking || busyKey === key;
  /** Anything at all is writing. The bulk bar reads this rather than `pendingFor`, because a
   *  bulk action covers the whole selection and must not fire over a single row's write in
   *  flight; so does the memo that keeps the toolbar up while something is settling. What
   *  changed is only that the ROWS stopped reading it. */
  const pending = blocking || busyKey !== null;

  // Stable, so a memoized card is not re-rendered by a handler that is merely a new function.
  // React Query's `mutate` is itself stable across renders, so these never change identity.
  const setOverrideMutate = setOverride.mutate;
  const clearOverrideMutate = clearOverride.mutate;
  const onSet = useCallback(
    (key: string, decision: Override, spareDays?: number) =>
      setOverrideMutate({ key, decision, spareDays: spareDays ?? 0 }),
    [setOverrideMutate],
  );
  const onClear = useCallback((key: string) => clearOverrideMutate(key), [clearOverrideMutate]);

  // --- Keeping the list in step with the latest scan ------------------------------------------
  // A scan finishing while this queue is open leaves it showing an older snapshot. Pull the
  // whole review surface to the newest one -- the list, the tab counts, the freshness line, an
  // open why or show panel, the reap breakdown -- in the one place that names every review
  // cache, the same way an override does. Any of these landing the newer snapshot clears the
  // nudge on its own. `["candidate"]` is here so the claim above is true and a stale why-panel
  // refetches; the panel itself is also closed in showLatest, since its id is snapshot-bound
  // and a refetch of the old id can only return a stale row (B-7).
  const queryClient = useQueryClient();
  // Returns a promise that settles once these refetches have finished, which is what tells a
  // failed silent refresh from one still in flight (useReviewFreshness).
  const refreshReview = useCallback(
    () =>
      Promise.all(
        [
          ["candidates"],
          ["candidates-unfiltered"],
          ["candidate"],
          ["group"],
          ["snapshot"],
          ["reap-breakdown"],
        ].map((queryKey) => queryClient.invalidateQueries({ queryKey })),
      ),
    [queryClient],
  );
  // Mid-review, judged at the instant the newer scan appears: scrolled into the list, a why or
  // show panel open, a selection or a write in flight. Any of these means a quiet swap would
  // move the ground under the reviewer, so hold their place and nudge instead of refreshing.
  const isBusy = useCallback(
    () =>
      (typeof window !== "undefined" && window.scrollY > 120) ||
      selectedId !== null ||
      selectedGroupKey !== null ||
      selected.size > 0 ||
      pending,
    [selectedId, selectedGroupKey, selected, pending],
  );
  // A quiet refresh still says so. When a newer scan lands while the reviewer is idle at the
  // top, the list swaps under them; a brief toast confirms it moved to the newest scan, so the
  // numbers never change with no acknowledgment. It is the silent path's only signal -- the
  // nudge covers the mid-review one. Fired only once the swap has actually landed (the freshness
  // hook's caught-up callback), never at issuance, so a failed refetch can never claim it
  // (PR-5, rule 85). A tick re-arms the fade each time, then it clears.
  const [toastTick, setToastTick] = useState(0);
  const [toastOn, setToastOn] = useState(false);
  // Passed straight through, promise and all: the hook waits on it to tell a failed silent
  // refresh from one still in flight.
  const onSilentRefresh = refreshReview;
  useEffect(() => {
    if (toastTick === 0) return;
    setToastOn(true);
    // Said through the shared region rather than left to the toast's own markup. A `role="status"`
    // node mounted in the same commit as its text is unreliably announced -- several readers only
    // watch regions that were already there -- which is the bug `Notice` reached for `role="alert"`
    // to avoid, and it is why this toast was silent while looking correct (#177). The words are
    // the toast's own, so the ear and the eye get the same sentence (rule 144).
    announce(TOAST_CAUGHT_UP);
    const id = window.setTimeout(() => setToastOn(false), 2600);
    return () => window.clearTimeout(id);
  }, [toastTick]);
  const freshness = useReviewFreshness({
    viewSnapshotId: pages?.pages[0]?.snapshotId ?? null,
    latestSnapshotId: latestScanSnapshotId,
    isBusy,
    // A silent refresh whose refetch settles without catching up surfaces the nudge instead of a
    // phantom toast, so the list is never left silently stale (PR-5).
    onSilentRefresh,
    onSilentCaughtUp: () => setToastTick((n) => n + 1),
  });
  // The nudge's own voice. It appears when a scan lands under an open review, which is a change
  // the operator did not make to a page whose every number just moved -- so it is exactly the
  // event a reader has to hear, and the surface announcing it was the one that could not.
  // Fired on the EDGE the bar goes up, not on every render it is up for, or a re-render would
  // repeat it -- and the bar is sticky, so it stays up across a lot of them.
  const nudgeUp = freshness.showBar;
  const nudgeWasUp = useRef(false);
  useEffect(() => {
    if (nudgeUp && !nudgeWasUp.current) announce(`${NUDGE_NEWER_SCAN}. ${NUDGE_VIEWING_PREVIOUS}`);
    nudgeWasUp.current = nudgeUp;
  }, [nudgeUp]);

  const showLatest = () => {
    // Close an open why-panel first: its candidate id is from the snapshot being replaced, so a
    // refetch could only return a stale row. The show panel is keyed on a stable group key and
    // refreshes in place, so only the item selection is cleared (B-7).
    onClearItemSelection?.();
    void refreshReview();
    if (typeof window !== "undefined") window.scrollTo({ top: 0, behavior: "smooth" });
  };

  // --- Select mode: tap to toggle, or press-and-drag to paint a run of cards ------------------
  const applySelect = useCallback((key: string, mode: "add" | "remove") => {
    setSelected((prev) => {
      if (mode === "add" ? prev.has(key) : !prev.has(key)) return prev;
      const next = new Set(prev);
      mode === "add" ? next.add(key) : next.delete(key);
      return next;
    });
  }, []);
  // The live selection, for the two handlers that must read it without being rebuilt every time
  // it changes -- a fresh handler would re-render every memoized card on every painted card,
  // which is the loop P-1 is about. Same latest-ref pattern the modal shell uses for onClose.
  const selectedRef = useRef(selected);
  selectedRef.current = selected;
  // A press begins a drag whose direction (add vs remove) is fixed by the card pressed: press an
  // unpicked card to paint selections, a picked one to rub them out. Then every card the pointer
  // enters follows suit -- so a tap toggles one, a drag sweeps a section.
  const onSelectDown = useCallback(
    (key: string, e: ReactPointerEvent) => {
      e.preventDefault();
      const mode: "add" | "remove" = selectedRef.current.has(key) ? "remove" : "add";
      dragRef.current = { mode };
      applySelect(key, mode);
    },
    [applySelect],
  );
  const onSelectEnter = useCallback(
    (key: string) => {
      if (dragRef.current) applySelect(key, dragRef.current.mode);
    },
    [applySelect],
  );
  // Keyboard toggle: flip one card and leave no drag armed -- the belt to onSelectDown's
  // pointer path, so tabbing through and pressing Space never strands a hover-painting mode.
  const onSelectToggle = useCallback(
    (key: string) => {
      applySelect(key, selectedRef.current.has(key) ? "remove" : "add");
    },
    [applySelect],
  );
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
  // Memoized on the loaded set. Without it, every render re-folded every fetched candidate
  // into groups -- and a drag-select across a long list renders once per `pointerenter`
  // (P-1). Everything derived from it is memoized on it, all the way to the card props.
  const groups = useMemo(() => (data ? toGroups(data) : []), [data]);

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
  // ...and which screens it applies to. A phone's season list is long enough to bury the next
  // card, so the two screen sizes are separately choosable. Live rather than read once: drag a
  // window across the boundary and untouched cards re-seed to match, which is also what makes
  // the setting observable without a reload. `useMediaQuery` reports false where matchMedia is
  // missing, so that fallback is the wide screen -- the same one App's `fullSheet` assumes.
  const narrowScreen = useMediaQuery(NARROW_SCREEN_QUERY);
  const expandSeasonsByDefault = shouldExpandSeasons(
    generalSettings?.expand_seasons_mode ?? "off",
    narrowScreen,
  );
  // What a bulk Spare keeps items for: the operator's default length (0 = forever). The
  // per-card menu offers other lengths; the bulk bar acts on the whole selection at once, so
  // it uses the default and its glyph (∞ or a clock) shows which that is.
  const defaultSpareDays = generalSettings?.default_spare_days ?? 0;
  // The unmeasured allowance, read once here for the whole list. Together with the spare
  // length above this is everything the rows share, and the two are handed down through
  // QueueSettingsContext -- one subscription each, where there used to be one PER CONTROL:
  // four hundred cards with their seasons expanded came to roughly a thousand observers on
  // these two keys, and every write to either re-rendered all of them (P-7).
  const profile = useQuery({ queryKey: ["profile"], queryFn: api.profile });
  const queueSettings = useMemo<QueueSettings>(
    () => ({
      defaultSpareDays,
      unmeasured: {
        // Unknown reads as "held back", the safe answer on a card; the read's own state
        // travels with it so a surface that states a NUMBER can refuse to state a wrong one.
        holdsBack: (profile.data?.max_unmeasured_per_run ?? 0) === 0,
        isPending: profile.isPending,
        isError: profile.isError,
      },
    }),
    [defaultSpareDays, profile.data, profile.isPending, profile.isError],
  );
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
  const shownGroups = useMemo(() => groups.slice(0, visible), [groups, visible]);
  const shownKeys = useMemo(() => shownGroups.map(groupKeyOf), [shownGroups]);
  const shownItems = useMemo(
    () => shownGroups.reduce((n, g) => n + g.items.length, 0),
    [shownGroups],
  );
  const allShownSelected = shownKeys.length > 0 && shownKeys.every((k) => selected.has(k));
  // One object for every card (see CardSelect): identical for all of them, so it is built once
  // rather than per card, and a memoized card is not re-rendered by a new object of the same
  // shape. Whether a card is picked rides beside it as its own scalar prop.
  const cardSelect = useMemo<CardSelect>(
    () => ({ selectMode, onSelectDown, onSelectEnter, onSelectToggle }),
    [selectMode, onSelectDown, onSelectEnter, onSelectToggle],
  );
  // Whether picking everything the filters match is still worth offering: some card is
  // either unfetched or drawn-but-unpicked beyond the window the "Select all" button reaches.
  const moreToSelect =
    allShownSelected && (hasNextPage || !groups.every((g) => selected.has(groupKeyOf(g))));
  // Picked cards that are not on screen: the state "Select everything matching" leaves behind.
  const holdsUndrawn = selected.size > shownKeys.length;
  // What the picked CARDS cover in the items a reap would plan: a show card stands for every
  // actable season, so "3 selected" can sit beside a run of thirty (rule 30). A show's count
  // is the server's own actable total, never the seasons this page happened to fetch. Null --
  // and the bar says cards only -- off the condemned lane, where the bulk actions decide whole
  // cards rather than seasons, and whenever a picked card is not drawn, since its size is
  // unknown here.
  let selectedItems: number | null = null;
  if (verdict === "condemn" && selected.size > 0) {
    const perKey = new Map(
      groups.map(
        (g) =>
          [
            groupKeyOf(g),
            g.isShow ? (g.items[0]?.group_condemned_count ?? g.items.length) : g.items.length,
          ] as const,
      ),
    );
    let total = 0;
    for (const key of selected) {
      const n = perKey.get(key);
      if (n == null) {
        total = -1;
        break;
      }
      total += n;
    }
    selectedItems = total >= 0 ? total : null;
  }
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
    // Every row below reads the operator's spare length and the unmeasured allowance, and
    // reads them from HERE: one subscription for the whole list rather than one per control
    // (P-7, see QueueSettingsContext).
    <QueueSettingsContext.Provider value={queueSettings}>
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

        {/* A scan finished under an open review. Sticky, so it stays in reach however far the
          reviewer has scrolled; derived from the list being behind, so it clears itself the
          moment any refetch pulls the newer snapshot. */}
        {/* No `role="status"` here any more. It was mounted in the same commit as its text, which
            several readers do not announce at all, so the role read as correct and said nothing --
            and it wrapped two focusable buttons, which a live region should not. The sentence is
            spoken through the shared region instead, from the effect below. */}
        {freshness.showBar && (
          <div className="scan-nudge">
            <span className="nudge-dot" aria-hidden="true" />
            <span className="nudge-text">
              <b>{NUDGE_NEWER_SCAN}</b>
              <span>{NUDGE_VIEWING_PREVIOUS}</span>
            </span>
            <span className="nudge-actions">
              <button type="button" className="primary sm" onClick={showLatest}>
                Show latest
              </button>
              <button
                type="button"
                className="nudge-x"
                onClick={freshness.dismiss}
                aria-label="Keep viewing this scan"
                title="Keep viewing this scan"
              >
                <svg viewBox="0 0 14 14" width="13" height="13" aria-hidden="true">
                  <path
                    d="M3 3l8 8M11 3l-8 8"
                    stroke="currentColor"
                    strokeWidth="1.6"
                    strokeLinecap="round"
                  />
                </svg>
              </button>
            </span>
          </div>
        )}
        {freshness.showMarker && (
          <button type="button" className="scan-behind" onClick={showLatest}>
            <span className="nudge-dot" aria-hidden="true" />
            One scan behind. <span className="scan-behind-cta">Show latest</span>
          </button>
        )}
        {/* Same as the nudge above (rule 72): the role is gone and the sentence is announced. */}
        {toastOn && (
          <div className="scan-toast">
            <span className="scan-toast-check" aria-hidden="true">
              <svg viewBox="0 0 16 16" width="15" height="15">
                <path
                  d="M3.5 8.5l3 3 6-7"
                  fill="none"
                  stroke="currentColor"
                  strokeWidth="1.8"
                  strokeLinecap="round"
                  strokeLinejoin="round"
                />
              </svg>
            </span>
            <span className="scan-toast-msg">{TOAST_CAUGHT_UP}</span>
          </div>
        )}

        <div className="queue-toolbar">
          <div className="search-wrap">
            <svg
              className="search-icon"
              viewBox="0 0 16 16"
              width="14"
              height="14"
              aria-hidden="true"
            >
              <circle cx="7" cy="7" r="4.5" fill="none" stroke="currentColor" strokeWidth="1.5" />
              <path d="M11 11l3 3" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" />
            </svg>
            <input
              className="search-input"
              type="search"
              // The year is named because it is not guessable: the box takes "Example Alpha
              // 1979" and takes "1979" on its own, and an operator who is not told tries the
              // year once, gets nothing, and stops trying. `list_candidates` is the other copy
              // of this sentence (rule 144) and says the same three things.
              // The name repeats the placeholder word for word, because the placeholder is this
              // box's only visible label: a name that says "and years" where the screen says
              // "years" cannot be reached by someone who speaks what they can see.
              aria-label="Search titles, shows, years"
              placeholder="Search titles, shows, years…"
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
                ref={addFilterRef}
                className={`add-filter ${openMenu === "add" ? "open" : ""}`}
                aria-expanded={openMenu === "add"}
                aria-controls={openMenu === "add" ? `${menuIdBase}-add` : undefined}
                onClick={(e) => openMenuFrom("add", e.currentTarget)}
              >
                <PlusIcon />
                Filter
              </button>
              {openMenu === "add" && (
                <FilterMenu id={`${menuIdBase}-add`} label="Add a filter">
                  {addableDimensions.map((d) => (
                    <li key={d.id}>
                      <button
                        type="button"
                        className="filter-mi"
                        onClick={() => {
                          // The last addable one takes the ＋ Filter button with it, so name the
                          // chip this press is about to create as where focus should land.
                          if (addableDimensions.length === 1) focusChip.current = d.id;
                          setFilters((f) => d.set(f, d.options[0]!.value));
                          closeMenu();
                        }}
                      >
                        <span className="filter-mi-ic" aria-hidden="true">
                          {d.icon}
                        </span>
                        <span className="filter-mi-label">{d.label}</span>
                      </button>
                    </li>
                  ))}
                </FilterMenu>
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
              <svg
                viewBox="0 0 16 16"
                width="14"
                height="14"
                fill="none"
                aria-hidden="true"
                className={filters.order}
              >
                <path
                  d="M8 3v10M4 9l4 4 4-4"
                  stroke="currentColor"
                  strokeWidth="1.5"
                  strokeLinecap="round"
                  strokeLinejoin="round"
                />
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
          <div className="active-filters" ref={chips.ref as RefObject<HTMLDivElement>}>
            {search && (
              <FilterChip
                label={<>&ldquo;{search}&rdquo;</>}
                clearLabel={`Stop searching for ${search}`}
                onClear={() => {
                  // Always index 0: the search chip is rendered first in the row.
                  chips.removing(0);
                  setSearchInput("");
                  setSearch("");
                }}
              />
            )}
            {activeDimensions.map((d, chipIndex) => {
              const current = d.value(filters);
              const label = d.options.find((o) => o.value === current)?.label ?? current;
              return (
                <span className="filter-anchor" key={d.id}>
                  <span className={`fchip ${openMenu === d.id ? "open" : ""}`}>
                    <button
                      type="button"
                      className="fchip-body"
                      // The handle `focusChip` above finds this chip by, when the press that
                      // created it also removed the ＋ Filter button focus would have gone back to.
                      data-dim={d.id}
                      title={`Filter: ${d.label}`}
                      aria-expanded={openMenu === d.id}
                      aria-controls={openMenu === d.id ? `${menuIdBase}-${d.id}` : undefined}
                      onClick={(e) => openMenuFrom(d.id, e.currentTarget)}
                    >
                      <span className="fchip-ic" aria-hidden="true">
                        {d.icon}
                      </span>
                      <b>{label}</b>
                      <CaretIcon />
                    </button>
                    <button
                      {...REMOVES_ITS_ROW}
                      type="button"
                      className="fchip-x"
                      aria-label={`Remove the ${d.label} filter`}
                      onClick={() => {
                        // Offset by the search chip, which shares this row and is drawn first.
                        chips.removing(chipIndex + (search ? 1 : 0));
                        setFilters((f) => d.set(f, d.defaultValue));
                        setOpenMenu((m) => (m === d.id ? null : m));
                      }}
                    >
                      ×
                    </button>
                  </span>
                  {openMenu === d.id && (
                    <FilterMenu id={`${menuIdBase}-${d.id}`} label={d.label}>
                      {d.options.map((o) => (
                        <li key={o.value}>
                          <button
                            type="button"
                            // Which one is in force, without `aria-selected`'s promise that this
                            // is a listbox. `aria-current` is the app's existing way of saying
                            // "this is the one you are on" (the section rail says `page`).
                            aria-current={o.value === current ? "true" : undefined}
                            className={`filter-mi ${o.value === current ? "sel" : ""}`}
                            onClick={() => {
                              setFilters((f) => d.set(f, o.value));
                              closeMenu();
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
                    </FilterMenu>
                  )}
                </span>
              );
            })}
            <button type="button" className="link-btn" onClick={clearFilters}>
              Clear all
            </button>
          </div>
        )}

        {/* Divided, and in plain language. The raw `error.message` was an exception string over
            a fully drawn queue: it broke rule 21 on its own, and it said the read had failed
            directly above the rows it had read (#190). Which one shows turns on whether anything
            ever landed -- `data` is undefined only then. */}
        {error && !data && <p className="error">Couldn't load your review queue.</p>}
        {error && data && <StaleReadNotice what="the queue" />}
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
              {", "}
              <strong>{bytes(totalSize)}</strong>
              {verdict === "condemn" && " would be freed"}
              {totalUnknownSize > 0 && (
                <>
                  {", "}
                  <strong>
                    {count(totalUnknownSize)} {totalUnknownSize === 1 ? "size" : "sizes"} unknown
                  </strong>
                </>
              )}
            </p>
            <div className={`card-list ${selectMode ? "card-list-selecting has-bulk-bar" : ""}`}>
              {shownGroups.map((group) => {
                const key = groupKeyOf(group);
                return group.isShow ? (
                  <ShowCard
                    key={group.key}
                    group={group}
                    defaultOpen={expandSeasonsByDefault}
                    selectedId={selectedId}
                    selectedGroupKey={selectedGroupKey}
                    isSelected={selected.has(key)}
                    select={cardSelect}
                    onOpen={onSelect}
                    onOpenGroup={onSelectGroup}
                    onSet={onSet}
                    onClear={onClear}
                    pending={pendingFor(key)}
                    // The season rows write their OWN keys, which this card's key can never
                    // equal, so `pending` alone left them undimmed and pressable throughout
                    // their own round trip. A scalar, not `pendingFor` itself: a fresh function
                    // every render would defeat the memo on every card in the list.
                    busyKey={busyKey}
                  />
                ) : (
                  <MovieCard
                    key={group.key}
                    item={group.items[0]!}
                    selected={group.items[0]!.id === selectedId}
                    isSelected={selected.has(key)}
                    select={cardSelect}
                    onOpen={onSelect}
                    onSet={onSet}
                    onClear={onClear}
                    pending={pendingFor(group.items[0]!.media_key)}
                    // The ITEM's own verdict, through the one shared test -- never the tab's.
                    // Lane membership is the effective verdict, so a movie sits on Condemned
                    // with a stored abstain and an honored hand reap: Reap must stay, and a
                    // spared condemnation must stay flippable (rule 48).
                    hideReap={reapIsNoop(group.items[0]!)}
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
              {selected.size === 0 ? (
                "Tap or drag to pick"
              ) : selectedItems != null && selectedItems !== selected.size ? (
                <>
                  <strong>{selected.size}</strong> {selected.size === 1 ? "card" : "cards"},{" "}
                  <strong>{count(selectedItems)}</strong> {selectedItems === 1 ? "item" : "items"}
                </>
              ) : (
                <>
                  <strong>{selected.size}</strong> selected
                </>
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
                  bulk.mutate({
                    keys: [...selected],
                    decision: "spare",
                    spareDays: defaultSpareDays,
                  })
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
    </QueueSettingsContext.Provider>
  );
}

// Re-exported so these keep their old import path while callers and tests move over to the
// files that now own them (R-1). New code imports from ./queueFilters, ./queueSettings and
// ./reviewFate directly.
export { DEFAULT_FILTERS, loadFilters, saveFilters, type QueueFilters };
export { useHoldsBackUnmeasured };
export { handFate, isCondemned, reapIsNoop, showReapIsNoop, type Fate };
export { KeptByShowNote, OverrideControls };
