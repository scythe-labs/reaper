// SPDX-License-Identifier: AGPL-3.0-or-later
//
// The why-panel. This is Reaper's reason to exist.
//
// Every competitor can tell you *which rules matched*. None of them show the work. The
// three blocks below are, in order of how much trust they buy:
//
//   1. The reasons that pushed toward removing, each carrying ITS SHARE OF THE SCORE --
//      not its raw weight. The shares add up to the number beside them, so the receipt
//      can be checked by adding it up rather than by dividing by a total never shown.
//   2. The protections that were checked and did NOT fire -- with the real numbers.
//      "checked: popular here -- 0 watchers in the last 365 days, your floor is 3".
//      This is the block that makes a verdict auditable rather than merely asserted.
//   3. The protections that could not be checked at all, rendered *differently*.
//      "We could not look" is not "we looked and it was fine". Every tool that renders
//      them alike eventually deletes something during an API outage.
//
// And it renders for PROTECTED items too, showing the score it is overriding. A tool
// that only explains its deletions cannot be trusted about its keeps.

import { type ReactNode, useEffect, useRef, useState } from "react";
import {
  type CandidateDetail,
  type GateOutcome,
  type Links,
  type Match,
  type Ratings,
  type SignalContribution,
  type SignalState,
} from "../api";
import { coverage, itemBytes, since, spareRemaining } from "../format";
import { useOverrideMutations } from "../useOverrideMutations";
import {
  KeptByShowNote,
  LibraryChip,
  OverrideControls,
  ShowStatusChip,
} from "./ReviewQueue";
import { useHoldsBackUnmeasured } from "./queueSettings";
import { reapIsNoop } from "./reviewFate";

/** The built-in signal ids. Anything else in `explanation.signals[].id` is a custom
 *  rule's own name, and its row wears the "Your rule" tag. */
const BUILTIN_SIGNAL_IDS = new Set(["unwatched", "season_rank", "few_watchers", "low_rating", "size"]);

/** The little "this one is yours" marker worn by every policy row the owner authored. */
function RuleTag() {
  return <span className="rule-tag">Your rule</span>;
}

/** A pill that opens the item in one of the tools that manage it. Hidden without a URL --
 *  a missing link is hidden, never rendered broken. Shared with the show panel. */
export function JumpPill({ href, label }: { href: string | null; label: string }) {
  if (!href) return null;
  return (
    <a className="jump-pill" href={href} target="_blank" rel="noopener noreferrer">
      {label} ↗
    </a>
  );
}

/** Certification, runtime and genres, the way a library listing reads. The year stays in
 *  the title; anything unknown is simply skipped. */
function MetaLine({ item }: { item: CandidateDetail }) {
  const parts: string[] = [];
  if (item.runtime_minutes) parts.push(`${item.runtime_minutes} min`);
  if (item.genres.length > 0) parts.push(item.genres.slice(0, 3).join(", "));
  if (!item.content_rating && parts.length === 0) return null;
  return (
    <p className="why-meta">
      {item.content_rating && <span className="cert">{item.content_rating}</span>}
      {parts.join(" · ")}
    </p>
  );
}

/** One ratings chip. With a site link it opens the number's source in a new tab;
 *  without one it stays a plain chip -- never a dead link. */
function RatingChip({
  href,
  title,
  children,
}: {
  href: string | null;
  title: string;
  children: ReactNode;
}) {
  if (!href) {
    return (
      <span className="rating-chip" title={title}>
        {children}
      </span>
    );
  }
  return (
    <a className="rating-chip" href={href} target="_blank" rel="noopener noreferrer" title={title}>
      {children}
    </a>
  );
}

/** External ratings, no stars. IMDb is the same number the score used (its chip says so
 *  on hover); the rest come from Plex. Each chip opens its site when the link is known.
 *  The whole row hides when nothing is known. */
function RatingsRow({ ratings, links }: { ratings: Ratings | null; links: Links }) {
  if (!ratings) return null;
  const known =
    ratings.imdb ??
    ratings.rt_critic ??
    ratings.rt_audience ??
    ratings.tmdb ??
    ratings.trakt ??
    null;
  if (known === null) return null;
  return (
    <div className="why-ratings">
      {ratings.imdb != null && (
        <RatingChip
          href={links.imdb}
          title={
            ratings.imdb_votes
              ? `IMDb ${ratings.imdb.toFixed(1)} from ${ratings.imdb_votes.toLocaleString()} votes. The same number the score uses.`
              : "The same number the score uses."
          }
        >
          <span className="rating-src rating-imdb">IMDb</span> {ratings.imdb.toFixed(1)}
        </RatingChip>
      )}
      {/* The tomato and popcorn stand alone to save room; the hover text and the
          accessible label still say which score is which. */}
      {ratings.rt_critic != null && (
        <RatingChip
          href={links.rotten_tomatoes}
          title="Rotten Tomatoes critics score. Opens a Rotten Tomatoes search"
        >
          <span className="rating-src" role="img" aria-label="Rotten Tomatoes critics">
            🍅
          </span>{" "}
          {ratings.rt_critic}%
        </RatingChip>
      )}
      {ratings.rt_audience != null && (
        <RatingChip
          href={links.rotten_tomatoes}
          title="Rotten Tomatoes audience score. Opens a Rotten Tomatoes search"
        >
          <span className="rating-src" role="img" aria-label="Rotten Tomatoes audience">
            🍿
          </span>{" "}
          {ratings.rt_audience}%
        </RatingChip>
      )}
      {ratings.tmdb != null && (
        <RatingChip href={links.tmdb} title="TMDb user score">
          <span className="rating-src">TMDb</span> {ratings.tmdb}%
        </RatingChip>
      )}
      {ratings.trakt != null && (
        <RatingChip href={links.trakt} title="Trakt rating. Opens the title on Trakt">
          <span className="rating-src">Trakt</span> {ratings.trakt}%
        </RatingChip>
      )}
    </div>
  );
}

/** The synopsis, clamped to two lines with a "more" to expand. The card shows the *reason*
 *  now, so this slide-out is the one place the plot lives -- but it still should not push the
 *  reasoning below the fold, hence the clamp. Shared with the show panel. */
export function Synopsis({ text }: { text: string }) {
  const [expanded, setExpanded] = useState(false);
  return (
    <p className="why-summary">
      <span className={expanded ? undefined : "clamp-2"}>{text}</span>
      {text.length > 150 && (
        <button className="link-btn" onClick={() => setExpanded((v) => !v)}>
          {expanded ? "less" : "more"}
        </button>
      )}
    </p>
  );
}

/** The backdrop banner at the top of the panel: the item's wide Plex art, fading down into
 *  the panel so the reasoning below reads on a plain surface. Falls back to the poster when
 *  a title has no separate art, and to nothing at all when it has neither. Shared with the
 *  show panel. */
export function WhyHero({ posterUrl }: { posterUrl: string }) {
  const [src, setSrc] = useState(`${posterUrl}?kind=art`);
  const fellBack = useRef(false);

  // When the selected item changes without an unmount in between (the new item's detail was
  // already cached, so `detail` goes A -> B directly), this component is reused rather than
  // remounted. Reset the art to the new item's, or the hero keeps showing the previous
  // item's backdrop under the new title/score -- exactly the kind of mismatch this panel
  // exists to avoid. Mirrors ReviewQueue's Backdrop.
  useEffect(() => {
    fellBack.current = false;
    setSrc(`${posterUrl}?kind=art`);
  }, [posterUrl]);

  if (!src) return null;
  return (
    <div className="why-hero">
      <img
        src={src}
        alt=""
        aria-hidden="true"
        onError={() => {
          if (!fellBack.current) {
            fellBack.current = true;
            setSrc(posterUrl); // no wide art -> the poster still fills the banner
          } else {
            setSrc(""); // neither -> drop the banner
          }
        }}
      />
      <div className="why-hero-fade" aria-hidden="true" />
    </div>
  );
}

/** Every `.why` panel's close: a media-sheet disc floated over the hero art (see .why-close),
 *  never a boxed glyph in the title row, where it read as a stray form control. A stroked X to
 *  match the panel's round-capped glyphs, and NOT the scythe: closing is not reaping. Over a
 *  title with no art it rests on the surface top-right the same way. Render it FIRST in each
 *  panel so it is the panel's first tab stop; z-index clears the hero art and fade. One
 *  component so the four panels never drift (rule 18). */
export function WhyClose({ onClose }: { onClose: () => void }) {
  return (
    <button type="button" className="why-close" onClick={onClose} aria-label="Close">
      <svg viewBox="0 0 16 16" width="15" height="15" fill="none" aria-hidden="true">
        <path d="M4 4l8 8M12 4l-8 8" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" />
      </svg>
    </button>
  );
}

/** Shown ONLY when Reaper could not confidently tie the item to a Plex entry -- the one match
 *  outcome the owner needs told, because it is *why* the file was kept. A clean match shows
 *  nothing at all. Two plain wordings, no jargon: nothing found in Plex, or more than one
 *  possible match. Reuses the shared amber `.notice-warn` tone, like every other "we could not
 *  look" state. */
function KeptNotice({ match }: { match: Match | undefined }) {
  if (!match || match.status === "matched" || match.status == null) return null;

  const reason =
    match.status === "ambiguous"
      ? "This looks like more than one thing in your Plex, so we couldn't tell which one it is."
      : "We couldn't find this in your Plex, so there's no way to tell if anyone still watches it.";

  return (
    <p className="notice notice-warn kept-notice">
      <strong>Kept to be safe.</strong> {reason}
    </p>
  );
}

/** Is this abstain the keep-rule conflict -- a season the rule would prune but that was
 *  watched more than one it keeps, deliberately left for the owner? Distinct from a plumbing
 *  block ("could not check ..."), which is a genuine we-could-not-look. Mirrors the backend's
 *  own test in ``engine.verdict.block_holds_reap`` / ``api.routes._chip``. */
function isKeepRuleConflict(item: CandidateDetail): boolean {
  return item.explanation.protections_unknown.some(
    (o) => o.gate === "season_progression" && !o.detail.startsWith("could not check"),
  );
}

/** Why a hand reap is still held -- the one thing the engine will not delete past. After the
 *  keep-rule conflict became reap-overridable, a held reap can only be: something playing
 *  right now, a file no app manages, or a protection that genuinely could not be checked. */
function heldReapNote(item: CandidateDetail): string {
  const fired = new Set(item.explanation.protections_fired.map((o) => o.gate));
  if (fired.has("streaming_now")) {
    return "You asked to remove this, but someone is watching it right now, so it's kept for now.";
  }
  // Reads a STORED explanation, so it outlives the gate that wrote it. That gate is retired
  // (`engine/gates.py`) and no new scan can produce this, but a snapshot on disk is read back
  // by whatever version is running, and a wrong-but-specific sentence is worse than a right
  // one. Kept for the same reason `api/routes.py` keeps its phrase for `others_watching`.
  if (fired.has("unmanaged")) {
    return "You asked to remove this, but no app manages the file, so there's no safe way to remove it.";
  }
  if (item.explanation.protections_unknown.length > 0) {
    return "You asked to remove this, but a protection couldn't be checked, so Reaper is keeping it to be safe.";
  }
  return "You asked to remove this, but Reaper can't confirm it's safe to remove yet, so it's kept for now.";
}

/** What a hand spare says: kept by the owner, forever, on a countdown, or expired.
 *
 *  It reads the spare that COVERS the item, not the one in force by precedence, because every
 *  sentence here asserts what will happen to the file (rule 61). A season spared ten days inside
 *  a show spared forever was told "10 days left, then Reaper judges it again", and Reaper does
 *  no such thing: the season's spare lapses and the show's goes on keeping it.
 *
 *  The expired arm is the one that has to be careful. The item really is still kept -- only a
 *  scan realizes a spare's clock -- so this must not read as "it will be removed now". It says
 *  what happened and what is still true, in the same words the button's tooltip uses, from the
 *  one derivation (`spareRemaining().note`, rule 104). */
function spareNote(item: CandidateDetail): string {
  const cover = spareRemaining(item.spare_covers_until);
  // A show-level spare outlasts this item's own exactly when the two fields disagree: they are
  // the same date whenever the item's own spare is the one covering it, both being one
  // `expiries` lookup on the same key (see `whitelist.covering_spare_expiry`). Comparing them
  // beats re-deriving "is the show's longer" on the client, which would be the second
  // derivation rule 104 forbids -- and it needs no date math at all.
  if (item.spare_covers_until !== item.spare_expires_at) {
    const own = spareRemaining(item.spare_expires_at);
    const kept = cover.forever
      ? "The whole show is spared, so this won't be removed."
      : `The whole show is spared, ${cover.phrase}.`;
    // Named second, so the outcome leads and the operator's own decision is still accounted
    // for rather than silently overridden.
    const mine = own.expired
      ? "Your spare on this season has run out."
      : `Your spare on this season has ${own.phrase}.`;
    return `${kept} ${mine}`;
  }
  if (cover.forever) return "You chose to keep this, so it won't be removed.";
  if (cover.expired) return `${cover.note}.`;
  return `You chose to keep this. ${cover.phrase}, then Reaper judges it again.`;
}

/** The verdict headline, and the reason under it. It reads the EFFECTIVE decision, not the
 *  frozen scan verdict (rule 61): a hand reap the engine honors reads as a removal, a reap it
 *  can't honor yet reads as held (dashed red, never solid), and a hand spare says the owner
 *  kept it -- so a reaped or held item is never mislabeled "Sanctuary". Only with no hand
 *  decision does the scan verdict speak, and then "Sanctuary" is claimed only when a
 *  protection actually fired (a protect with nothing fired is a stale held-reap row from
 *  before this shipped, which a rescan resolves; it reads as left-for-you, not protected). */
function verdictLook(item: CandidateDetail): { klass: string; label: string; note: ReactNode } {
  const { verdict, override, override_effective, explanation } = item;

  if (override === "reap") {
    if (override_effective) {
      return {
        klass: "verdict-condemn",
        label: "Reaped by hand",
        note: "You marked this for removal, so it will be removed. Nothing is holding it back.",
      };
    }
    return { klass: "verdict-held", label: "Kept for now", note: heldReapNote(item) };
  }
  if (override === "spare") {
    return { klass: "verdict-protect", label: "Spared by hand", note: spareNote(item) };
  }

  if (verdict === "protect" && explanation.protections_fired.length > 0) {
    return {
      klass: "verdict-protect",
      label: "Sanctuary",
      note: (
        <>
          Something is protecting this, so <strong>the score doesn't matter</strong>: it's kept
          no matter what, and nothing can change that.
        </>
      ),
    };
  }
  if (verdict === "condemn") {
    return { klass: "verdict-condemn", label: "Condemned", note: null };
  }
  if (isKeepRuleConflict(item)) {
    // "Needs a look", not "Left for you to decide": the latter is the heading of the section
    // just below that lists the same block, so the headline would say it twice. Matches the
    // conflict chip's own words.
    return {
      klass: "verdict-abstain",
      label: "Needs a look",
      note: "This was watched more than a season your keep rule protects, so Reaper left the call to you. Spare it to keep it, or Reap it to remove it.",
    };
  }
  return {
    klass: "verdict-abstain",
    label: "Limbo",
    note: (
      <>
        Reaper is not confident enough to judge this one. It scored below your threshold, or too
        little of it could be seen. Either way, Reaper leaves it alone, and the file is kept.
      </>
    ),
  };
}

function Verdict({ item }: { item: CandidateDetail }) {
  const { score, explanation } = item;
  const look = verdictLook(item);

  return (
    <div className={`verdict ${look.klass}`}>
      <div className="verdict-label">{look.label}</div>
      <div className="verdict-score">
        <strong>{score}</strong>
        {/* The threshold clause is dropped when the server could not read this row's
            stored explanation: it sends no threshold rather than an invented one, and
            "your threshold is" with nothing after it would be worse than saying nothing. */}
        <span className="muted">
          /100
          {typeof explanation.threshold === "number" &&
            ` \u00b7 your threshold is ${explanation.threshold}`}
        </span>
      </div>
      {look.note && <p className="verdict-note">{look.note}</p>}
    </div>
  );
}

/** Hand out `target` whole points across `values`, in proportion, so the parts sum to the
 *  total EXACTLY. Rounding each row on its own does not: the six-row worked example gives
 *  27+11+7+7+4 = 56 against a score of 55, and a receipt that does not add up is worse
 *  than no receipt at all. Largest remainder fixes that.
 *
 *  Ties are broken by the larger exact value, then by original position, so the panel
 *  hands the same point to the same row on every render and never reshuffles. */
export function allocateShares(values: number[], target: number): number[] {
  const total = values.reduce((sum, v) => sum + v, 0);
  if (total <= 0 || target <= 0) return values.map(() => 0);

  const exact = values.map((v) => (v / total) * target);
  const out = exact.map(Math.floor);
  let left = target - out.reduce((sum, v) => sum + v, 0);

  const order = exact
    .map((v, i) => ({ i, frac: v - Math.floor(v), v }))
    .sort((a, b) => b.frac - a.frac || b.v - a.v || a.i - b.i);
  for (const { i } of order) {
    if (left <= 0) break;
    out[i] = (out[i] ?? 0) + 1;
    left -= 1;
  }
  return out;
}

/** A row's state, with the fallback for rows scored before the backend recorded one. A
 *  missing state is read as "did not apply", never as "argued for keeping": nothing
 *  recorded that it argued for anything, and inventing that overstates the case for
 *  keeping the file. Old unreadable rows stay amber, because `evaluated` still says so. */
function stateOf(signal: SignalContribution): SignalState {
  if (signal.state) return signal.state;
  if (signal.contribution > 0) return "adds";
  return signal.evaluated ? "not_applicable" : "unreadable";
}

/** One row, reduced to exactly what gets drawn. */
type Row = {
  signal: SignalContribution;
  /** Points this row put on the score. Every rendered row's share sums to the group total. */
  share: number;
  state: SignalState;
  /** How much of the whole bar track this row can occupy: its weight over the total. */
  footprint: number;
  /** How much of that footprint it actually filled, 0 to 1. */
  added: number;
};

/** One row: the points it added, what it was about, and its slice of the whole bar.
 *
 *  The bar is measured against the TOTAL, not against the row's own weight. Filling each
 *  row against itself is what let a 55 render as three near-full bars: every row looked
 *  maxed out, and the total they were shares of was never on screen. */
function SignalRow({ row }: { row: Row }) {
  const { signal, state } = row;
  // A custom condemn rule's id is its own name; built-ins use the closed id set.
  const custom = !BUILTIN_SIGNAL_IDS.has(signal.id);
  // A yes/no rule of your own either matched or did not, so it cannot land part-way. Flat
  // chrome, no ramp cap, or the bar would imply a sliding scale that does not exist.
  const flat = custom && (signal.contribution === 0 || signal.contribution === signal.weight);

  return (
    <li className={`sig-row sig-${state}`}>
      <div className="sig-head">
        {/* A plus sign is a claim that something was added. A row that added nothing shows
            a plain 0, and one that added less than a whole point says so rather than
            rounding its contribution away to "+0". */}
        <span className="sig-share">
          {state === "unreadable"
            ? "·"
            : row.share > 0
              ? `+${row.share}`
              : row.signal.contribution > 0
                ? "<1"
                : "0"}
        </span>
        <span className="sig-detail">
          {signal.detail}
          {custom && <RuleTag />}
        </span>
      </div>
      <div className="sig-track">
        <div
          className={flat ? "sig-foot sig-foot-flat" : "sig-foot"}
          style={{ width: `${row.footprint * 100}%` }}
        >
          <div className="sig-added" style={{ width: `${row.added * 100}%` }} />
        </div>
      </div>
    </li>
  );
}

/** How many rows a group shows before the rest fold away. Six because a stock policy
 *  carries three signals for movies and four for TV: a default install never collapses
 *  anything, and hiding only ever begins once the operator has added rules of their own.
 *  Never applied to "Couldn't check" -- more reasons Reaper could not read is more cause
 *  to look, not less. */
const GROUP_ROW_LIMIT = 6;

/** Biggest share first, so whatever gets folded away is always what mattered least.
 *  Weight, then id, break the ties an "argued to keep" group is made of (every one of its
 *  shares is 0), so the same rows land in the same order on every render. */
function byShare(a: Row, b: Row): number {
  return (
    b.share - a.share ||
    b.signal.weight - a.signal.weight ||
    a.signal.id.localeCompare(b.signal.id)
  );
}

/** A named group of rows. An empty group renders nothing at all.
 *
 *  Past `collapseAfter` rows the tail folds into the same disclosure the "didn't apply"
 *  rules use. The group's total still counts every row, hidden ones included, so opening
 *  the disclosure never changes the number beside the heading. */
function SignalGroup({
  title,
  rows,
  total,
  note,
  collapseAfter,
}: {
  title: string;
  rows: Row[];
  total?: number;
  note?: string;
  collapseAfter?: number;
}) {
  if (rows.length === 0) return null;

  const limit = collapseAfter ?? rows.length;
  const shown = rows.slice(0, limit);
  const hidden = rows.slice(limit);
  const hiddenTotal = hidden.reduce((sum, r) => sum + r.share, 0);
  // Shares are whole points, so "these added nothing" is exactly zero -- there is no
  // rounding band to pick a threshold inside. At zero the summary says only how many are
  // down there, because "adding +0" reads as a bug.
  const more = hiddenTotal > 0 ? `${hidden.length} more, adding +${hiddenTotal}` : `${hidden.length} more`;

  return (
    <div className="sig-group">
      <div className="sig-group-head">
        <h4>{title}</h4>
        {total != null && <span className="sig-group-total">+{total}</span>}
      </div>
      {note && <p className="sig-group-note">{note}</p>}
      <ul className="signals">
        {shown.map((row) => (
          <SignalRow key={row.signal.id} row={row} />
        ))}
      </ul>
      {hidden.length > 0 && (
        <details className="sig-rest">
          <summary>{more}</summary>
          <ul className="signals">
            {hidden.map((row) => (
              <SignalRow key={row.signal.id} row={row} />
            ))}
          </ul>
        </details>
      )}
    </div>
  );
}

/** The scoring receipt: every row carries its share of the score, and the shares add up.
 *
 *  WHICH number they add up to: the score BEFORE any graded-keep discount, because that is
 *  the number these rows produce. The final score is `base_score - keep_discount`, and the
 *  keeps block below owns the difference. Rows that visibly fail to add up to the number
 *  beside them are the exact bug this section exists to remove. */
function Signals({ item }: { item: CandidateDetail }) {
  const { explanation } = item;

  // A rule carrying no weight is not part of the score and has nothing to report.
  const signals = explanation.signals.filter((s) => s.weight > 0);
  const totalWeight = signals.reduce((sum, s) => sum + s.weight, 0);
  // Sum to the number actually PRINTED beside these rows, derived the same way it is
  // printed, so the two cannot disagree. Rounding the stored 1-decimal base_score a second
  // time here would drift: a base of 54.464 stores as 54.5 and rounds up to 55, beside a
  // heading of 54. Undiscounted, the printed number is the whole score itself.
  const discounted = (explanation.keep_discount ?? 0) > 0;
  const target =
    discounted && explanation.base_score != null
      ? Number(explanation.base_score.toFixed(0))
      : Math.round(explanation.score);
  const shares = allocateShares(
    signals.map((s) => s.contribution),
    target,
  );

  const rows: Row[] = signals.map((signal, i) => ({
    signal,
    share: shares[i] ?? 0,
    state: stateOf(signal),
    footprint: totalWeight > 0 ? signal.weight / totalWeight : 0,
    added: signal.weight > 0 ? Math.min(signal.contribution / signal.weight, 1) : 0,
  }));

  const adds = rows.filter((r) => r.state === "adds").sort(byShare);
  const argued = rows.filter((r) => r.state === "argues_keep").sort(byShare);
  const unreadable = rows.filter((r) => r.state === "unreadable");
  const inapplicable = rows.filter((r) => r.state === "not_applicable");
  const addedTotal = adds.reduce((sum, r) => sum + r.share, 0);

  return (
    <section className="block">
      <h3>Why it scored {explanation.score}</h3>
      <p className="blurb">
        Reasons to believe nobody will watch it again. Reaper saw <strong>{coverage(item.coverage_bp)}</strong>{" "}
        of the evidence.
      </p>

      <SignalGroup
        title="Pushed to remove"
        rows={adds}
        total={addedTotal}
        collapseAfter={GROUP_ROW_LIMIT}
      />
      <SignalGroup title="Argued to keep" rows={argued} collapseAfter={GROUP_ROW_LIMIT} />
      {/* "Couldn't check" is never folded away, and never capped. A reason Reaper could not
          read is the one thing an operator has to see before approving a deletion, and more
          of them is more cause to look. One note for the group, not one per row. */}
      <SignalGroup
        title="Couldn't check"
        rows={unreadable}
        note="These pull the score down, never up."
      />

      {inapplicable.length > 0 && (
        <details className="sig-rest">
          <summary>{inapplicable.length} didn't apply here</summary>
          <ul className="signals">
            {inapplicable.map((row) => (
              <SignalRow key={row.signal.id} row={row} />
            ))}
          </ul>
        </details>
      )}

      <p className="sig-legend">
        <span className="sig-key sig-key-added" />
        Added
        <span className="sig-key sig-key-held" />
        Held back
        <span className="sig-key sig-key-unread" />
        Couldn't check
        {/* The sum sentence is the legend's last item, not a stray line under it: what the
            colors mean and what the numbers add to belong on the same line. It needs its own
            element to BE an item: adjacent bare text nodes collapse into one anonymous flex
            item, and the row's gap never lands between them. */}
        <span>{discounted ? "Points before the keep rules." : "Points add up to the score."}</span>
      </p>
    </section>
  );
}

/** The backend words a custom-rule outcome as "your rule: {condition}" (fired) or
 *  "checked your rule: {condition}" (checked, didn't fire) -- see engine/fields.py.
 *  Reworded here for the panel; the stored detail stays untouched as the audit record. */
function customGateDetail(detail: string): string {
  if (detail.startsWith("your rule: ")) {
    return `Kept by your rule: ${detail.slice("your rule: ".length)}`;
  }
  if (detail.startsWith("checked your rule: ")) {
    return `Your rule didn't match: ${detail.slice("checked your rule: ".length)}`;
  }
  return detail;
}

function Gates({
  title,
  blurb,
  outcomes,
  tone,
  collapsible,
}: {
  title: string;
  blurb: string;
  outcomes: GateOutcome[];
  tone: "fired" | "checked";
  collapsible?: boolean;
}) {
  if (outcomes.length === 0) return null;

  const body = (
    <>
      <p className="blurb">{blurb}</p>
      <ul className={`gates gates-${tone}`}>
        {/* Keyed on gate AND detail: several custom rules share the "custom" gate id,
            and duplicate keys would make React drop rows. */}
        {outcomes.map((outcome) => {
          const custom = outcome.gate === "custom";
          return (
            <li key={`${outcome.gate}:${outcome.detail}`} className={custom ? "gate-custom" : ""}>
              <span className="gate-mark">✓</span>
              <span className="gate-detail">
                {custom ? customGateDetail(outcome.detail) : outcome.detail}
                {custom && <RuleTag />}
              </span>
            </li>
          );
        })}
      </ul>
    </>
  );

  return (
    <section className="block">
      <h3>{title}</h3>
      {/* The cleared list is the quietest of the three protection blocks -- every check came
          back clear -- so it rests folded behind one disclosure and the operator opens it only
          to read the full list. Same native-<details> grammar and muted summary as `.sig-rest`.
          A protection that spared the file is never folded: it is the reason the file lives. */}
      {collapsible ? (
        <details className="gates-fold">
          <summary>
            <span className="fold-caret" aria-hidden="true">
              ▸
            </span>
            <span className="fold-shut-label">
              {outcomes.length === 1 ? "Show 1" : `Show all ${outcomes.length}`}
            </span>
            <span className="fold-open-label">Hide</span>
          </summary>
          {body}
        </details>
      ) : (
        body
      )}
    </section>
  );
}

/** The backend words each unchecked protection as "could not check {check}: {cause}"
 *  (engine/gates.py `_blocked` and the custom-rule evaluator). Both vocabularies are
 *  closed and colon-free, so the first ": " splits them reliably; anything that doesn't
 *  parse (a season-order conflict, a named custom rule's wrapped detail) keeps its own
 *  row with the raw sentence, exactly as before. */
const CHECK_COPY: Record<string, string> = {
  "watch history": "watch history",
  "when it was last watched": "when it was last watched",
  "the watch horizon": "how far back its history goes",
  "active streams": "whether anyone is watching",
  "the IMDb rating": "its IMDb rating",
  "the IMDb vote count": "its IMDb vote count",
  "the whitelist": "your keep list",
  "curated lists": "protected lists",
  "which *arr owns this": "which app manages it",
  "who else watched it": "who else watched it",
};

const CAUSE_COPY: Record<string, string> = {
  "Plex has not matched this item": "This title couldn't be found in Plex.",
  "Plex has not matched this season": "This season couldn't be found in Plex.",
  "more than one Plex item matches this title": "This looks like more than one thing in Plex.",
  "more than one Plex item matches this show":
    "This show looks like more than one thing in Plex.",
  "no added-at date": "Plex didn't say when this was added.",
  "no added-at date for this season": "Plex didn't say when this season was added.",
  "could not read active sessions": "Reaper couldn't see what's playing right now.",
  "could not reach the requests app": "The requests app couldn't be reached.",
  "requests not loaded": "Requests weren't loaded for this scan.",
  "no TMDb id to match a request": "It couldn't be matched to a request.",
  "no TVDb id to match a request": "It couldn't be matched to a request.",
  "Sonarr did not report series status": "Sonarr didn't say whether the show has ended.",
  "season has no rank": "Reaper couldn't tell which season this is.",
};

function joinChecks(checks: string[]): string {
  if (checks.length === 1) return checks[0] ?? "";
  if (checks.length === 2) return `${checks[0]} and ${checks[1]}`;
  return `${checks.slice(0, -1).join(", ")}, and ${checks[checks.length - 1]}`;
}

/** "Left for you to decide", grouped by cause. Three rows all ending in "Plex has not
 *  matched this item" told the owner the same thing three times; one box states the cause
 *  once and lists what it blocked. Causes keep first-appearance order; unparseable details
 *  render verbatim as their own box. */
function LeftForYou({ outcomes }: { outcomes: GateOutcome[] }) {
  if (outcomes.length === 0) return null;

  const groups = new Map<string, { cause: string; checks: string[] }>();
  const rows: ({ kind: "group"; key: string } | { kind: "raw"; outcome: GateOutcome })[] = [];
  for (const outcome of outcomes) {
    const parsed = /^could not check (.+?): (.+)$/.exec(outcome.detail);
    if (!parsed || !parsed[1] || !parsed[2]) {
      rows.push({ kind: "raw", outcome });
      continue;
    }
    const check = CHECK_COPY[parsed[1]] ?? parsed[1];
    const cause = CAUSE_COPY[parsed[2]] ?? `${parsed[2]}.`;
    const group = groups.get(cause);
    if (group) {
      if (!group.checks.includes(check)) group.checks.push(check);
    } else {
      groups.set(cause, { cause, checks: [check] });
      rows.push({ kind: "group", key: cause });
    }
  }

  return (
    <section className="block">
      <h3>Left for you to decide</h3>
      <p className="blurb">
        Reaper wasn't sure enough to act on these on its own: a rule was too close to call, or
        something couldn't be reached. Everything here is kept, never removed, until you look.
      </p>
      <ul className="gates gates-unknown">
        {rows.map((row) =>
          row.kind === "raw" ? (
            <li key={row.outcome.gate + row.outcome.detail}>
              <span className="gate-detail">{row.outcome.detail}</span>
            </li>
          ) : (
            <li key={row.key}>
              <strong>{row.key}</strong>
              <span className="gate-detail">
                Couldn't check: {joinChecks(groups.get(row.key)?.checks ?? [])}.
              </span>
            </li>
          ),
        )}
      </ul>
    </section>
  );
}

export function WhyPanel({
  item,
  onClose,
  onShowGroup,
}: {
  item: CandidateDetail;
  onClose: () => void;
  onShowGroup?: (key: string) => void;
}) {
  const { explanation } = item;

  // The panel is where the deciding happens, so Spare and Reap live here too, through the
  // shared hook the cards use so both refresh the same caches.
  const { setOverride, clearOverride } = useOverrideMutations();
  // A card-side read: an unknown allowance answers "held back", which is the safe thing to
  // be wrong about beside a size cell that already says the size is unknown.
  const holdsBack = useHoldsBackUnmeasured().holdsBack;

  const mediaLabel = item.media_type === "season" ? "TV season" : item.media_type;

  return (
    <aside className="why">
      <WhyClose onClose={onClose} />
      {item.poster_url && <WhyHero posterUrl={item.poster_url} />}

      {/* A season's reasoning is one level down from its show: offer the way back up. */}
      {item.group_key && onShowGroup && (
        <button
          type="button"
          className="link-btn back-to-show"
          onClick={() => onShowGroup(item.group_key!)}
        >
          ◂ Back to {item.group_title ?? "the show"}
        </button>
      )}

      <header className="why-head">
        <div>
          <h2>
            {/* The title itself opens the item in Plex when it can; a title Reaper
                couldn't match stays plain text rather than linking somewhere wrong. */}
            {item.links.plex ? (
              <a
                className="title-link"
                href={item.links.plex}
                target="_blank"
                rel="noopener noreferrer"
                title="Open in Plex"
              >
                {item.title}
                {item.year && <span className="card-year"> {item.year}</span>}
                <svg
                  className="title-ext"
                  viewBox="0 0 16 16"
                  width="13"
                  height="13"
                  fill="none"
                  aria-hidden="true"
                >
                  <path
                    d="M6 3h7v7M13 3L4 12"
                    stroke="currentColor"
                    strokeWidth="1.7"
                    strokeLinecap="round"
                    strokeLinejoin="round"
                  />
                </svg>
              </a>
            ) : (
              <>
                {item.title}
                {item.year && <span className="card-year"> {item.year}</span>}
              </>
            )}
          </h2>
          <p className="muted why-sub">
            {itemBytes(item.size_bytes)} &middot; {mediaLabel}
            {/* The Plex library the file lives in -- same quiet chip as the cards, so movies
                and seasons read the same. */}
            <LibraryChip library={item.library} />
            {/* All three states here: the panel is where you came to find out. The card
                stays quiet about a show that is still going. */}
            <ShowStatusChip status={item.show_status} />
            <JumpPill href={item.links.tautulli} label="Tautulli" />
            <JumpPill href={item.links.seerr} label="Seerr" />
            <JumpPill href={item.links.radarr} label="Radarr" />
            <JumpPill href={item.links.sonarr} label="Sonarr" />
          </p>
        </div>
      </header>

      <MetaLine item={item} />
      <RatingsRow ratings={item.ratings} links={item.links} />

      {item.summary && <Synopsis text={item.summary} />}

      {/* After the summary, not before it: the block above is what the item IS
          (certification, ratings, synopsis) and reads as one unit, so splitting it with a
          notice buries the identity. Here the notice sits directly above the verdict it
          explains. Movies and seasons share this panel, so both orders move together. */}
      <KeptNotice match={explanation.match} />

      {/* The scan's stored reasoning for this one would not read back. Say that plainly:
          the blocks below are empty because nothing could be read, not because nothing
          protected it, and an empty panel alone would read as the second. Same amber
          `.notice-warn` every other "we could not check" uses. */}
      {item.explanation_unreadable && (
        <p className="notice notice-warn">
          Reaper couldn't read why it judged this one, so the reasons below are missing.
          Run a scan to rebuild them. Nothing is removed on a reason Reaper can't show.
        </p>
      )}

      <Verdict item={item} />

      {item.first_flagged_at && (
        <p className="flagged">
          On the list since {since(item.first_flagged_at)}. Watching it or sparing it keeps
          it, and you still start every removal by hand.
        </p>
      )}

      <Signals item={item} />

      {explanation.keeps && explanation.keeps.length > 0 && (
        <section className="block">
          <h3>Leaning toward keeping</h3>
          <p className="blurb">
            Your soft “keep” rules lowered the score
            {explanation.base_score != null
              ? ` from ${explanation.base_score.toFixed(0)} to ${explanation.score.toFixed(0)}`
              : ""}
            . These can only ever lower a score, and never overrule a protection.
          </p>
          <ul className="signals">
            {explanation.keeps.map((keep) => (
              <li key={keep.name} className={keep.evaluated ? "signal" : "signal signal-unknown"}>
                <div className="signal-head">
                  <span className="signal-amount">
                    −{keep.discount.toFixed(1)}
                    <span className="muted">/{keep.max_discount}</span>
                  </span>
                  <span className="signal-detail">
                    {keep.detail}
                    {/* Every graded keep is operator-authored, so every row is yours. */}
                    <RuleTag />
                  </span>
                </div>
                {!keep.evaluated && (
                  <p className="signal-note">
                    Reaper couldn’t check this one, so it kept the file fully: missing data only
                    ever leans toward <em>keeping</em>.
                  </p>
                )}
              </li>
            ))}
          </ul>
        </section>
      )}

      <Gates
        title="What spared it"
        blurb="Any one of these keeps the file, whatever it scored."
        outcomes={explanation.protections_fired}
        tone="fired"
      />

      <Gates
        title="Protections it cleared"
        blurb="The numbers are the ones it actually used."
        outcomes={explanation.protections_checked}
        tone="checked"
        collapsible
      />

      <LeftForYou outcomes={explanation.protections_unknown} />

      {/* This is the panel that answers "what happens to this one", so it has to say the
          thing that overrides every reason above it: no plan will include it. Gated on the
          allowance, because above zero that sentence would be false -- the item IS
          reapable then, and promising otherwise here is worse than saying nothing. The
          plain reason only, never which source was asked or when. */}
      {item.size_bytes === null && holdsBack && (
        <p className="notice notice-warn">
          Held back: size unknown. Sonarr and Radarr had none, so Reaper can't tell what
          removing this would free, and won't remove it.
        </p>
      )}

      {/* Decide without leaving the reasoning. Sticky, so the buttons stay in reach at
          the end of a long explanation. */}
      <div className="why-actions">
        {/* When a whole-show decision keeps or reaps this season, say so above the buttons:
            the control toggles the season's OWN decision (override_own), and clearing a season
            key cannot clear a show-level one. Renders nothing for a movie or an untouched-show
            season. */}
        <KeptByShowNote
          own={item.override_own}
          showOverride={item.show_override}
          effective={item.override_effective}
        />
        <div className="why-actions-row">
          <OverrideControls
            override={item.override_own}
            onSet={(decision, spareDays) =>
              setOverride.mutate({ key: item.media_key, decision, spareDays: spareDays ?? 0 })
            }
            onClear={() => clearOverride.mutate(item.media_key)}
            pending={setOverride.isPending || clearOverride.isPending}
            // Same rule as the queue: reaping an item POLICY already condemns changes nothing, so
            // its Reap is dropped (reapIsNoop) -- but a spared row keeps Reap (it can be flipped)
            // and a row condemned only by its OWN reap keeps it (to undo). Spare (rescue) always
            // stays.
            hideReap={reapIsNoop(item)}
            spareExpiresAt={item.spare_expires_at}
            // The footer gives the pair the whole row, so a timed spare spells its count out.
            roomy
          />
          {(setOverride.isError || clearOverride.isError) && (
            <span className="error">Couldn't save that. Try again.</span>
          )}
        </div>
      </div>
    </aside>
  );
}
