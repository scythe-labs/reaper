// SPDX-License-Identifier: AGPL-3.0-or-later
//
// The why-panel. This is Reaper's reason to exist.
//
// Every competitor can tell you *which rules matched*. None of them show the work. The
// three blocks below are, in order of how much trust they buy:
//
//   1. The signals that fired, with their actual contributions.
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
} from "../api";
import { bytes, coverage, since } from "../format";
import { useOverrideMutations } from "../useOverrideMutations";
import { OverrideControls } from "./ReviewQueue";

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

/** Shown ONLY when Reaper could not confidently tie the item to a Plex entry -- the one match
 *  outcome the owner needs told, because it is *why* the file was kept. A clean match shows
 *  nothing at all. Two plain wordings, no jargon: nothing found in Plex, or more than one
 *  possible match. Reuses the shared amber `.warn` tone, like every other "we could not look"
 *  state. */
function KeptNotice({ match }: { match: Match | undefined }) {
  if (!match || match.status === "matched" || match.status == null) return null;

  const reason =
    match.status === "ambiguous"
      ? "This looks like more than one thing in your Plex, so we couldn't tell which one it is."
      : "We couldn't find this in your Plex, so there's no way to tell if anyone still watches it.";

  return (
    <p className="warn kept-notice">
      <strong>Kept to be safe.</strong> {reason}
    </p>
  );
}

/** The verdict headline, in the review queue's own tab words (Condemned Souls / Sanctuary /
 *  Limbo) rather than the engine's enum -- an operator should never be shown "abstain". */
const VERDICT_LABELS: Record<string, string> = {
  condemn: "Condemned",
  protect: "Sanctuary",
  abstain: "Limbo",
};

function Verdict({ item }: { item: CandidateDetail }) {
  const { verdict, score, explanation } = item;

  return (
    <div className={`verdict verdict-${verdict}`}>
      <div className="verdict-label">{VERDICT_LABELS[verdict] ?? verdict}</div>
      <div className="verdict-score">
        <strong>{score}</strong>
        <span className="muted">/100 &middot; your threshold is {explanation.threshold}</span>
      </div>

      {verdict === "protect" && (
        <p className="verdict-note">
          Something is protecting this, so <strong>the score doesn't matter</strong>: it's kept
          no matter what, and nothing can change that.
        </p>
      )}
      {verdict === "abstain" && (
        <p className="verdict-note">
          Reaper is not confident enough to judge this one. It scored below your threshold, or
          too little of it could be seen. Either way, Reaper leaves it alone, and the file is
          kept.
        </p>
      )}
    </div>
  );
}

/** One signal's contribution, as a bar you can compare against the others.
 *
 *  The denominator is the signal's *weight*, not the total, so a 70-weight signal
 *  contributing 70 fills the bar. That makes "this signal is maxed out" legible at a
 *  glance, which is the question you actually ask when tuning. */
function Signal({ signal }: { signal: CandidateDetail["explanation"]["signals"][number] }) {
  const filled = signal.weight > 0 ? (signal.contribution / signal.weight) * 100 : 0;
  // A custom condemn rule's id is its own name; built-ins use the closed id set.
  const custom = !BUILTIN_SIGNAL_IDS.has(signal.id);

  return (
    <li className={signal.evaluated ? "signal" : "signal signal-unknown"}>
      <div className="signal-head">
        <span className="signal-amount">
          {signal.evaluated ? `+${signal.contribution.toFixed(1)}` : "·"}
          <span className="muted">/{signal.weight}</span>
        </span>
        <span className="signal-detail">
          {signal.detail}
          {custom && <RuleTag />}
        </span>
      </div>
      <div className="bar">
        <div className="bar-fill" style={{ width: `${Math.min(filled, 100)}%` }} />
      </div>
      {!signal.evaluated && (
        <p className="signal-note">
          Reaper couldn't check this one, so it added nothing, which can only pull the score{" "}
          <em>down</em>, never up.
        </p>
      )}
    </li>
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
}: {
  title: string;
  blurb: string;
  outcomes: GateOutcome[];
  tone: "fired" | "checked";
}) {
  if (outcomes.length === 0) return null;

  return (
    <section className="block">
      <h3>{title}</h3>
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

  const mediaLabel = item.media_type === "season" ? "TV season" : item.media_type;

  return (
    <aside className="why">
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
            {bytes(item.size_bytes)} &middot; {mediaLabel}
            <JumpPill href={item.links.tautulli} label="Tautulli" />
            <JumpPill href={item.links.seerr} label="Seerr" />
            <JumpPill href={item.links.radarr} label="Radarr" />
            <JumpPill href={item.links.sonarr} label="Sonarr" />
          </p>
        </div>
        <button className="ghost why-close" onClick={onClose} aria-label="Close">
          ✕
        </button>
      </header>

      <KeptNotice match={explanation.match} />

      <MetaLine item={item} />
      <RatingsRow ratings={item.ratings} links={item.links} />

      {item.summary && <Synopsis text={item.summary} />}

      <Verdict item={item} />

      {item.first_flagged_at && (
        <p className="flagged">
          On the list since {since(item.first_flagged_at)}. It waits out a grace period,
          which you can cancel, before Reaper can remove it.
        </p>
      )}

      <section className="block">
        <h3>Why it scored {explanation.score}</h3>
        <p className="blurb">
          Reasons to believe nobody will watch it again. Reaper saw{" "}
          <strong>{coverage(item.coverage_bp)}</strong> of the evidence it looks for.
        </p>
        <ul className="signals">
          {explanation.signals.map((signal) => (
            <Signal key={signal.id} signal={signal} />
          ))}
        </ul>
      </section>

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
        title="Protections that fired"
        blurb="Any one of these keeps the file, whatever it scored."
        outcomes={explanation.protections_fired}
        tone="fired"
      />

      <Gates
        title="Protections that were checked and did not fire"
        blurb="What Reaper looked for and did not find. The numbers are the ones it actually used."
        outcomes={explanation.protections_checked}
        tone="checked"
      />

      <LeftForYou outcomes={explanation.protections_unknown} />

      {/* Decide without leaving the reasoning. Sticky, so the buttons stay in reach at
          the end of a long explanation. */}
      <div className="why-actions">
        <OverrideControls
          override={item.override}
          onSet={(decision) => setOverride.mutate({ key: item.media_key, decision })}
          onClear={() => clearOverride.mutate(item.media_key)}
          pending={setOverride.isPending || clearOverride.isPending}
        />
        {(setOverride.isError || clearOverride.isError) && (
          <span className="error">Couldn't save that. Try again.</span>
        )}
      </div>
    </aside>
  );
}
