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
//      "Untouched for 5 years, 7 months, past the 3 years it has to sit unwatched first."
//      Each is a whole sentence from a gate's ABSTAIN branch, rendered verbatim below; the
//      tick is decoration and there is no label prefix. This is the block that makes a
//      verdict auditable rather than merely asserted.
//   3. The protections that could not be checked at all, rendered *differently*.
//      "We could not look" is not "we looked and it was fine". Every tool that renders
//      them alike eventually deletes something during an API outage.
//
// And it renders for PROTECTED items too, showing the score it is overriding. A tool
// that only explains its deletions cannot be trusted about its keeps.

import { useMutation, useQueryClient } from "@tanstack/react-query";
import {
  type ReactNode,
  type RefObject,
  useEffect,
  useId,
  useLayoutEffect,
  useRef,
  useState,
} from "react";
import {
  api,
  type CandidateDetail,
  type GateOutcome,
  type Links,
  type Match,
  type Ratings,
  type SignalContribution,
  type SignalState,
} from "../api";
import { announce } from "../announce";
import { useSuccessorFocus } from "../focus";
import { coverage, itemBytes, since, spareRemaining } from "../format";
import { useOverrideMutations } from "../useOverrideMutations";
import { KeptByShowNote, LibraryChip, OverrideControls, ShowStatusChip } from "./ReviewQueue";
import { useHoldsBackUnmeasured } from "./queueSettings";
import { reapIsNoop } from "./reviewFate";
import { rowRampSentence } from "./signalRamp";
import { useMediaQuery } from "../useMediaQuery";
import { WhyShell } from "./WhyShell";
import { Notice } from "./Notice";

/** The built-in signal ids. Anything else in `explanation.signals[].id` is a custom
 *  rule's own name, and its row wears the "Your rule" tag. */
const BUILTIN_SIGNAL_IDS = new Set([
  "unwatched",
  "season_rank",
  "few_watchers",
  "low_rating",
  "size",
]);

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
      {label} <span aria-hidden="true">↗</span>
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
      {parts.join(", ")}
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

/** The synopsis, clamped to two lines with a "more" to expand -- offered whenever the text is
 *  actually cut, at whatever width the panel has. The card shows the *reason* now, so this
 *  slide-out is the one place the plot lives, but it still should not push the reasoning below
 *  the fold, hence the clamp. Shared with the show panel. */
export function Synopsis({ text }: { text: string }) {
  const [expanded, setExpanded] = useState(false);
  const [clipped, setClipped] = useState(false);
  const ref = useRef<HTMLSpanElement>(null);

  // This component is reused when the selection changes without an unmount in between --
  // `WhyHero` documents the same reuse -- so a synopsis left open would open the next title's
  // too, and the measurement below, which is skipped while open, would go on answering for the
  // previous text. Before paint, so the new title never flashes expanded.
  useLayoutEffect(() => setExpanded(false), [text]);

  // Whether two lines are enough is a question about how wide the panel is, never about how
  // long the string is. The old `text.length > 150` answered it at one width, and the width it
  // was right at is not the one a phone has: measured against this stylesheet, the first length
  // two lines cannot hold is 115 characters at a 393px panel, 135 at 430px and 145 at 480px,
  // against a control that waited for 151 everywhere. Everything inside that band was cut with
  // nothing on screen to open it, and the band is widest exactly where the panel is narrowest
  // (#407). Ask the element instead, which is also right at a text size or browser zoom nobody
  // predicted.
  //
  // Same shape as `usePopoverShift` (rule 18): layout rather than paint, so the control is
  // there in the first frame instead of appearing after it, and re-measured on every render
  // plus a window listener, because React re-renders nothing on a resize. Setting the same
  // value bails out of the re-render, so this settles rather than looping.
  useLayoutEffect(() => {
    // An open synopsis is not clamped, so it measures as "nothing hidden" and would take away
    // the control that closes it. Its last collapsed answer stands until it is collapsed again.
    if (expanded) return;
    const measure = () => {
      const el = ref.current;
      // A pixel of tolerance: both heights are rounded to integers, and a line box whose
      // height does not land on one can leave a 1px difference with nothing actually hidden.
      if (el) setClipped(el.scrollHeight > el.clientHeight + 1);
    };
    measure();
    window.addEventListener("resize", measure);
    return () => window.removeEventListener("resize", measure);
  });

  return (
    <p className="why-summary">
      <span ref={ref} className={expanded ? "why-synopsis" : "why-synopsis clamp-2"}>
        {text}
      </span>
      {(clipped || expanded) && (
        <button
          className="link-btn"
          // A disclosure that never said it was one. The clamp is CSS-only (`.clamp-2`), so the
          // whole synopsis is in the accessibility tree either way and pressing this appeared to
          // do nothing at all -- twice, since the label flips back. Every other disclosure in
          // this file is a native `<details>` and states itself for free (#177).
          aria-expanded={expanded}
          onClick={() => setExpanded((v) => !v)}
        >
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
 *  nothing at all. Three plain wordings, no jargon, and they are NOT interchangeable: nothing
 *  found in Plex; several Plex rows answering to it; or each kind of evidence naming a
 *  different single row, which is the two apps describing one file differently and says
 *  nothing about how many copies Plex holds. Reuses the shared amber `.notice-warn` tone, like
 *  every other "we could not look" state.
 *
 *  Branches on the typed `status`, never on the wording (rule 92) -- and it is also why the
 *  candidate links hang here rather than off the matching "Left for you to decide" box, whose
 *  rows are grouped by a cause *string* and could only be picked out by substring. */
function KeptNotice({
  match,
  links,
  mediaType,
}: {
  match: Match | undefined;
  links: Links;
  mediaType: string;
}) {
  if (!match || match.status === "matched" || match.status == null) return null;

  const app = mediaType === "movie" ? "Radarr" : "Sonarr";
  const thing = mediaType === "movie" ? "file" : "show";
  const reason =
    match.status === "ambiguous"
      ? "This looks like more than one thing in your Plex, so we couldn't tell which one it is."
      : match.status === "conflicted"
        ? `Plex and ${app} describe this ${thing} differently, so we couldn't tell which Plex ` +
          "entry it is."
        : "We couldn't find this in your Plex, so there's no way to tell if anyone still watches it.";

  return (
    // `standing`: `match.status` is frozen into the scan's explanation, so it is true every time
    // the panel is opened on this title. Its candidate links are focusables a component deep
    // (`MatchCandidates` -> `JumpPill`), so they are reached by navigating rather than by being
    // interrupted -- and the same rows render bare at `ShowPanel.tsx`, with no notice at all.
    <Notice tone="warn" className="kept-notice" standing>
      <strong>Kept to be safe.</strong> {reason}
      <MatchCandidates links={links} />
    </Notice>
  );
}

/** How many Plex listings this one file answers to, when it answers to more than one.
 *
 *  Quiet and neutral, on the shared `.chip` base with the other facts on this line (rule 18, so
 *  no new CSS): how many times a file is listed is a fact about the library, never an argument
 *  for its fate. Silent below two, which covers both the ordinary single-listing bind (the
 *  server sends null) and a record stored before the field shipped -- "Listed 1 time" on every
 *  item would be noise, and this line is scanned.
 *
 *  Worth a chip at all because the count is load-bearing on the deletion path: the executor
 *  re-reads `merged_rating_keys`, so a watch on ANY of these listings counts for the file and
 *  all of them go together. The operator was told none of that before (#260). The chip carries
 *  the number and the `title` carries that consequence, which is the same split `LibraryChip`
 *  already uses.
 *
 *  NOT on `ShowPanel` (rule 72): its group has no `match` record, so there is no count to read
 *  there. Carrying it over is a backend change (a merged count on the group payload) and is
 *  deferred rather than half-done -- a season panel silently omitting a fact the movie panel
 *  states would be worse than both being quiet. */
export function MergedListingChip({ match }: { match: Match | undefined }) {
  const listings = match?.merged_rating_keys?.length ?? 0;
  if (listings < 2) return null;
  return (
    <span
      className="chip"
      title={`Listed ${listings} times in your Plex. A watch on any of them counts, and all ${listings} go together.`}
    >
      Listed {listings}&times;
    </span>
  );
}

/** The Plex rows Reaper was choosing between when it refused to choose.
 *
 *  Every other jump link on this panel is built from the item's own rating key, which is null
 *  on exactly the rows this notice appears over -- so before this, the panel named a problem
 *  in the owner's Plex and offered no way to open it. Reaper knows nothing about these rows
 *  but their keys, so they are numbered rather than named; a single candidate drops the
 *  number, since "Plex 1" alone reads as a label for something it is not.
 *
 *  Exported because `ShowPanel` renders the same rows under the same kind of sentence
 *  (rule 72): a conflicted SHOW lands there, its title link is `group.links.plex`, and that
 *  is null for exactly these rows -- the identical dead end this component exists to close. */
export function MatchCandidates({ links }: { links: Links }) {
  const candidates = links.match_candidates ?? [];
  // Two ways to have nothing to offer, and only the first was checked. `JumpPill` renders
  // null for a null href, so a row whose candidates all lack BOTH a Plex and a Tautulli link
  // -- no Plex server row or no `plex_web_url`, and no enabled Tautulli, at render time --
  // left the lead sentence standing over an empty row, ending on a colon. That is the exact
  // dead end this component was added to close, reached one step later (#209).
  if (!candidates.some((candidate) => candidate.plex ?? candidate.tautulli)) return null;
  const numbered = candidates.length > 1;

  return (
    <span className="match-candidates">
      <span className="match-candidates-lead">
        {/* The count decides the noun. The component already asserts a single-candidate case
            exists -- `numbered` drops the number for it -- while the lead said "1 possible
            matches" right beside it (#209). */}
        Reaper saw {candidates.length} possible {numbered ? "matches" : "match"}:
      </span>
      {candidates.map((candidate, i) => (
        // The rating key: unique among siblings and a stable server id (rule 19), where the
        // index would renumber every pill if the list ever reordered.
        <span className="candidate-group" key={candidate.rating_key}>
          <JumpPill href={candidate.plex} label={numbered ? `Plex ${i + 1}` : "Plex"} />
          <JumpPill href={candidate.tautulli} label={numbered ? `Tautulli ${i + 1}` : "Tautulli"} />
        </span>
      ))}
    </span>
  );
}

/** The keep-rule conflict this abstain is, if it is one -- a season the rule would prune but
 *  that the guard handed to the owner instead.
 *
 *  **Read the gate, never the sentence (rule 142).** This was a wording test until #86:
 *  `!detail.startsWith("could not check")`, which passed for all three conflict shapes and so
 *  told the caller nothing about which one it had. The producer has recorded the answer as a
 *  typed flag since `defers_to_owner` shipped, and `api.routes._chip` has read it since; the
 *  panel could not, because `GateOutcomeOut` did not serve it. Now it does, and the flag rides
 *  on the outcome for `verdictLook` to branch on.
 *
 *  A `season_progression` row reaches `protections_unknown` three ways, and `unestablishable`
 *  is what says which (rule 142). The conflict arm is the one this branch wants. The second is
 *  a season the guard PROTECTS but could not answer (`ProtectedSeason.unestablishable`); the
 *  third is the mid-binge check on a show Plex never resolved, which asked nobody and abstains
 *  beside the four Plex-dependent gates that say why. Both of those carry the flag.
 *
 *  The second used to be excluded by nothing structural: a fired protection makes the verdict
 *  `protect` with a non-empty `protections_fired`, and the Sanctuary branch returns on it
 *  first. That is still true and is no longer what is relied on. The third has no such cover
 *  -- it abstains, which is this branch -- and would have read as "Reaper couldn't check who
 *  watched these seasons, so it left the call to you", offering a decision on a comparison
 *  that was never attempted.
 *
 *  `undefined` and `null` both mean a row scanned before the flag, and both read as a
 *  conflict, because before the flag that is the only thing an abstaining item's season guard
 *  put here.
 *
 *  Reachability, so nobody re-derives it: the shortfall arm fires wherever the watch history
 *  is shallower than the library is old, which makes this the DEFAULT rendering for TV on such
 *  a server, not an edge case. */
function keepRuleConflict(item: CandidateDetail): GateOutcome | undefined {
  return item.explanation.protections_unknown.find(
    (o) => o.gate === "season_progression" && o.unestablishable !== true,
  );
}

/** What the panel may say about a conflict, given what Reaper could actually establish.
 *
 *  Every arm ends in the same offer, because the engine keeps it in every arm: no blocked gate
 *  holds a hand reap (`engine/verdict.py`), so "Reap it to remove it" is honored whichever
 *  shape this is. What varies is the clause before it, which is a claim about evidence.
 *
 *  - `true` -- Reaper compared the two seasons and the pruned one won. The one shape that may
 *    be described as a comparison.
 *  - `false` -- it could not: either the kept season's count was never readable, or the watch
 *    mirror does not reach back far enough to stand behind the counts. Asserting the
 *    comparison here states arithmetic against a number nobody took, which `LeftForYou` then
 *    denies in the producer's own "Reaper cannot tell whether Season N ..." (#86). Three whole
 *    sections separate them -- the score breakdown, the keep leanings, the cleared protections
 *    -- which is a large part of why the contradiction went unseen: nobody had both on screen
 *    at once. Shares its wording with the card's chip, so the queue and the panel agree.
 *  - absent -- nothing legible in the row distinguishes the two shapes, so it names neither.
 *    Two rows land here: one frozen before the flag shipped, and one carrying a value that is
 *    not a bool at all, which the server reads to `null` rather than coercing or refusing
 *    (`engine.gates.thaw_defers_to_owner`, #112). Vague and true beats specific and a coin
 *    flip; the next scan resolves either into one of the two above. */
function conflictNote(defersToOwner: boolean | null | undefined): string {
  if (defersToOwner === true) {
    return "This was watched more than a season your keep rule protects, so Reaper left the call to you. Spare it to keep it, or Reap it to remove it.";
  }
  if (defersToOwner === false) {
    return "Reaper couldn't check who watched these seasons, so it left the call to you. Spare it to keep it, or Reap it to remove it.";
  }
  return "Reaper couldn't settle this one on its own, so it left the call to you. Spare it to keep it, or Reap it to remove it.";
}

/** Why a hand reap is still held. Exactly three things refuse one: a FIRED structural stop
 *  (playing right now, or a file no app manages), a bad Plex match, and a stored explanation
 *  Reaper could not read. A protection that merely could not be CHECKED refuses nothing
 *  (`engine/verdict.py`).
 *
 *  The match is tested before the blocked-protections list, and the blocked branch is gone.
 *  It used to come first and was wrong every time it fired: an unmatched item has no rating
 *  key, so every Plex-dependent gate blocks, and the branch shadowed the real cause, sending
 *  the operator after watch-history depth when they needed a re-match. The card chip beside
 *  it tests the match first (`api.routes._chip`). These branches are mutually reachable and
 *  the first true one wins, so the order is load-bearing. */
/** The FIRED protection a hand reap cannot overrule, if one fired: `verdict.STRUCTURAL_GATES`,
 *  which is something playing right now and the retired `unmanaged`. Neither is a judgment about
 *  whether the file is wanted, which is the whole test -- deleting mid-stream breaks a session
 *  someone is watching, and a file no *arr manages has no path to delete through.
 *
 *  Every OTHER protection is cautious rather than structural, and a hand reap condemns straight
 *  past it (`docs/DECISIONS.md` under "What a hand reap may overrule", and the #96 reversal
 *  that deleted `DEFERRABLE_BLOCK_GATES`). So
 *  this is the one test that separates "kept no matter what" from "kept unless you say
 *  otherwise", and both sentences below turn on it: `heldReapNote` says why a reap is still
 *  held, and the Sanctuary note says whether one would be. Deriving them apart is exactly how
 *  the panel came to call a protection absolute beside a working Reap button (rule 104). */
function structuralStop(item: CandidateDetail): "streaming_now" | "unmanaged" | undefined {
  const fired = new Set(item.explanation.protections_fired.map((o) => o.gate));
  if (fired.has("streaming_now")) return "streaming_now";
  // Reads a STORED explanation, so it outlives the gate that wrote it. That gate is retired
  // (`engine/gates.py`) and no new scan can produce this, but a snapshot on disk is read back
  // by whatever version is running, and a wrong-but-specific sentence is worse than a right
  // one. Kept for the same reason `api/routes.py` keeps its phrase for `others_watching`.
  if (fired.has("unmanaged")) return "unmanaged";
  return undefined;
}

function heldReapNote(item: CandidateDetail): string {
  const stop = structuralStop(item);
  if (stop === "streaming_now") {
    return "You asked to remove this, but someone is watching it right now, so it's kept for now.";
  }
  if (stop === "unmanaged") {
    return "You asked to remove this, but no app manages the file, so there's no safe way to remove it.";
  }
  const status = item.explanation.match?.status;
  if (status === "ambiguous") {
    return "You asked to remove this, but it looks like more than one thing in your Plex, so Reaper can't tell which file to remove.";
  }
  if (status === "conflicted") {
    const app = item.media_type === "movie" ? "Radarr" : "Sonarr";
    const thing = item.media_type === "movie" ? "file" : "show";
    return `You asked to remove this, but Plex and ${app} describe this ${thing} differently, so Reaper can't tell which file to remove.`;
  }
  if (status === "unmatched") {
    return "You asked to remove this, but Reaper couldn't find it in your Plex, so it can't tell which file to remove.";
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
    // Which of the two it is turns on `structuralStop`, and the difference is the Reap button
    // rendered a few inches below this sentence (`reapIsNoop` is false on a protect row, so the
    // control is there). Telling the operator a protection is absolute while offering the one
    // control that removes the file is a claim the panel itself disproves, and they may put
    // titles on a keep list believing that list is inviolable.
    return {
      klass: "verdict-protect",
      label: "Sanctuary",
      note: structuralStop(item) ? (
        <>
          Something is protecting this, so <strong>the score doesn't matter</strong>: it's kept, and
          a Reap won't remove it.
        </>
      ) : (
        <>
          Something is protecting this, so <strong>the score doesn't matter</strong>: it's kept
          unless you Reap it yourself.
        </>
      ),
    };
  }
  if (verdict === "condemn") {
    return { klass: "verdict-condemn", label: "Condemned", note: null };
  }
  const conflict = keepRuleConflict(item);
  if (conflict) {
    // "Needs a look", not "Left for you to decide": the latter is the heading of the section
    // just below that lists the same block, so the headline would say it twice. Matches the
    // conflict chip's own words, in all three shapes.
    return {
      klass: "verdict-abstain",
      label: "Needs a look",
      note: conflictNote(conflict.defers_to_owner),
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
            `, your threshold is ${explanation.threshold}`}
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

/** A signal row that can say what it was measured against, opened from the row itself.
 *
 *  **A press is the state that survives; hover only previews.** On a phone there is no
 *  hover at all, so the press IS the whole interaction and the row behaves as a plain
 *  expander: tap to open, tap to close. It expands in place rather than floating, which also
 *  keeps it clear of the edge-clipping rule 138 exists for -- inside a full-screen sheet an
 *  anchored popover has nowhere to be pushed back to.
 *
 *  The hover pair is bound only where a real pointer exists, because a touch tap synthesises
 *  `mouseenter` BEFORE it sends `click`: bound unconditionally the two handlers fight, on
 *  exactly the device that can use neither. `useMediaQuery` reporting `false` under jsdom is
 *  the safe way round here -- press-only is the behavior that works everywhere.
 *
 *  **There is deliberately no focus handler**, and there was one for a draft. A press
 *  focuses the button on every platform, so opening on focus too left `previewing` true
 *  underneath the press: the second tap unpressed a row that stayed open, and the control
 *  read as dead. Enter and Space already arrive here as a click, so the keyboard is served
 *  by the press like everything else, and nothing can strand a row open behind a Tab.
 *
 *  Not a `title` tooltip, which a phone never shows and a screen reader never reads out.
 *  This is the panel an owner reads before approving a deletion. */
function RampWhy({ sentence, children }: { sentence: string; children: ReactNode }) {
  const [pressed, setPressed] = useState(false);
  const [previewing, setPreviewing] = useState(false);
  const canHover = useMediaQuery(HOVER_QUERY);
  const id = useId();
  const open = pressed || previewing;

  return (
    <>
      <button
        type="button"
        className="sig-why"
        aria-expanded={open}
        aria-controls={id}
        onClick={() => setPressed((v) => !v)}
        {...(canHover
          ? {
              onMouseEnter: () => setPreviewing(true),
              onMouseLeave: () => setPreviewing(false),
            }
          : {})}
      >
        {children}
      </button>
      <p className="sig-why-said" id={id} hidden={!open}>
        {sentence}
      </p>
    </>
  );
}

/** Where a pointer can hover at all. Read through `useMediaQuery` like every other query
 *  this app branches on, so there is one way of asking. */
const HOVER_QUERY = "(hover: hover) and (pointer: fine)";

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
  // The line this row was measured against, if the scan recorded one. A row that could not
  // be read gets none, because its share column prints "Couldn't check" rather than a
  // number, and "added 0" would contradict the column and assert the very zero the panel is
  // refusing to state. A `not_applicable` row also returns before the ramp runs, and it DOES
  // keep the sentence: it sits inside a disclosure headed "didn't apply here", beside a
  // detail line naming what was missing, and its "added 0" agrees with the 0 in its column.
  const why =
    state === "unreadable"
      ? null
      : rowRampSentence(
          signal.id,
          signal.floor,
          signal.saturate_at,
          signal.weight,
          signal.contribution,
        );

  const body = (
    <>
      <span className="sig-head">
        {/* A plus sign is a claim that something was added. A row that added nothing shows
            a plain 0, and one that added less than a whole point says so rather than
            rounding its contribution away to "+0". */}
        <span className="sig-share">
          {/* The one middot in the app that may not simply be hidden. Everywhere else the
              character separates two facts that survive without it; here it IS the fact -- the
              whole of what this column says for a reason Reaper could not read -- so hiding it
              alone would leave the column silent while every sibling row reads out "+5", "<1"
              or "0". It gets the word instead, in the same vocabulary as the group's own
              heading. On the panel an operator reads before approving a deletion, a row that
              could not be checked is the last one that should go by unheard. */}
          {state === "unreadable" ? (
            <>
              <span aria-hidden="true">·</span>
              <span className="sr-only">Couldn't check</span>
            </>
          ) : row.share > 0 ? (
            `+${row.share}`
          ) : row.signal.contribution > 0 ? (
            "<1"
          ) : (
            "0"
          )}
        </span>
        <span className="sig-detail">
          {signal.detail}
          {custom && <RuleTag />}
        </span>
      </span>
      <span className="sig-track">
        <span
          className={flat ? "sig-foot sig-foot-flat" : "sig-foot"}
          style={{ width: `${row.footprint * 100}%` }}
        >
          <span className="sig-added" style={{ width: `${row.added * 100}%` }} />
        </span>
      </span>
    </>
  );

  return (
    <li className={`sig-row sig-${state}`}>
      {why ? <RampWhy sentence={why}>{body}</RampWhy> : body}
    </li>
  );
}

/** Coverage in basis points when every weighted reason was readable. Stored as bp because
 *  the policy body is integers-only (see `format.coverage`), so full is 10,000 and not 1. */
const FULL_COVERAGE_BP = 10_000;

/** How many rows a group shows before the rest fold away. Six because a stock policy
 *  carries three signals for movies and four for TV: a default install never collapses
 *  anything, and hiding only ever begins once the operator has added rules of their own.
 *  Never applied to "Couldn't check" -- more reasons Reaper could not read is more cause
 *  to look. */
const GROUP_ROW_LIMIT = 6;

/** Biggest share first, so whatever gets folded away is always what mattered least.
 *  Weight, then id, break the ties an "argued to keep" group is made of (every one of its
 *  shares is 0), so the same rows land in the same order on every render. */
function byShare(a: Row, b: Row): number {
  return (
    b.share - a.share || b.signal.weight - a.signal.weight || a.signal.id.localeCompare(b.signal.id)
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
  const more =
    hiddenTotal > 0 ? `${hidden.length} more, adding +${hiddenTotal}` : `${hidden.length} more`;

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

  // Below the floor, name the line coverage fell under, the way the score names its threshold.
  // An abstain forced by too little readable evidence scores at or above the threshold and is
  // held anyway (`decide_verdict` checks coverage before the score), so the score alone never
  // explains that hold and the coverage number alone does not say it was too low. Only when the
  // two round to different percentages: "40%, under the 50% it needs" reads, "50%, under the
  // 50% it needs" reads as a bug. Silent when the floor predates the field (null, so the panel
  // omits it like a null threshold) or coverage cleared it. `coverage_bp < floor` implies
  // `< FULL_COVERAGE_BP`, so this clause only ever rides inside the sentence below.
  const floor = explanation.coverage_floor_bp ?? null;
  const floorClause =
    floor != null && item.coverage_bp < floor && coverage(item.coverage_bp) !== coverage(floor)
      ? `, under the ${coverage(floor)} it needs to judge this one`
      : "";

  return (
    <section className="block">
      <h3>Why it scored {explanation.score}</h3>
      {/* The coverage clause is silence at full coverage, and it should always have been.
          The ONLY thing that can lower this number is a reason Reaper could not read
          (`SignalState.UNREADABLE` is the one state that costs coverage), and those get
          their own "Couldn't check" group below with their own note. So at 100% the
          sentence announced the absence of a group that is already absent -- a number with
          nothing behind it, which is what made an owner ask "100% of WHAT evidence" (#410).
          Below full it is the one place the panel says how much of the scoring weight went
          unread, which the group's row count does not answer: one unread reason out of five
          can be most of the score. */}
      <p className="blurb">
        Reasons to believe nobody will watch it again.
        {item.coverage_bp < FULL_COVERAGE_BP && (
          <>
            {" "}
            Reaper could read {coverage(item.coverage_bp)} of what it scores on{floorClause}.
          </>
        )}
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
              {/* Decoration: every row in this list is a pass, so the tick adds nothing a
                  reader needs and lands mid-sentence as a stray character (#177). Where a
                  list can hold BOTH outcomes -- the reap report's per-item checks -- the
                  glyph is hidden and a word carries it instead (#170). */}
              <span className="gate-mark" aria-hidden="true">
                ✓
              </span>
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
  // The season guard, where it could not run at all. Its other outcomes are whole sentences
  // that do not parse as `could not check {what}: {cause}` and keep their own row.
  "who is part-way through it": "who's part-way through it",
};

const CAUSE_COPY: Record<string, string> = {
  "Plex has not matched this item": "This title couldn't be found in Plex.",
  "Plex has not matched this season": "This season couldn't be found in Plex.",
  "more than one Plex item matches this title": "This looks like more than one thing in Plex.",
  "more than one Plex item matches this show": "This show looks like more than one thing in Plex.",
  // The disagreement pair. Generated nowhere: these are the exact strings snapshot.py and
  // season_scan.py put in `_NO_KEY_REASONS`, and
  // tests/test_review_chips.py::TestTheMatchStatusVocabulary fails when one of those has no
  // entry here -- without which the box prints the backend's raw phrasing (rule 144).
  "Plex and Radarr describe this file differently":
    "Plex and Radarr describe this file differently.",
  "Plex and Sonarr describe this show differently":
    "Plex and Sonarr describe this show differently.",
  "no added-at date": "Plex didn't say when this was added.",
  "no added-at date for this season": "Plex didn't say when this season was added.",
  "could not read active sessions": "Reaper couldn't see what's playing right now.",
  "could not reach the requests app": "The requests app couldn't be reached.",
  "requests not loaded": "Requests weren't loaded for this scan.",
  "no TMDb id to match a request": "It couldn't be matched to a request.",
  "no TVDb id to match a request": "It couldn't be matched to a request.",
  "Sonarr did not report series status": "Sonarr didn't say whether the show has ended.",
  // Serves STORED rows only: nothing writes this reason any more. A season with no rank is
  // a special, and season_scan.py now records that as Absent (it genuinely has no rank slot)
  // rather than Unknown, so no fresh scan can compose the detail this translates. It stays
  // because a snapshot frozen before that fix survives an upgrade -- migrations only add --
  // and the queue renders the latest snapshot until the next scan replaces it, so an owner
  // who upgrades before re-scanning can still open a row carrying it. Dropping the entry
  // would print the backend's raw phrase at exactly that owner, which is what #282 closed.
  // tests/test_review_chips.py::test_every_panel_cause_has_a_live_producer holds the pair.
  "season has no rank": "Reaper couldn't tell which season this is.",
  // The five that used to fall through and print the backend's own words here. Two arrive
  // through built-in gates (an IMDb rating bar, and the dormancy and popularity gates); the
  // size pair arrives through a keep rule on "Size on disk", the same route the request
  // reasons above already take.
  "the IMDb ratings data could not be read": "Reaper couldn't read the IMDb ratings data.",
  "no IMDb id to look up": "Reaper couldn't look this title up on IMDb.",
  "the file's size was not reported": "Radarr didn't report this file's size.",
  "the season's size was not reported": "Sonarr didn't report this season's size.",
  "plays recorded on an earlier scan are no longer readable":
    "Reaper can no longer read the plays it recorded earlier.",
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

/** What goes stale the moment one title's watch record is forgotten, each with the surface it
 *  feeds. Read off the queries themselves rather than guessed: this panel is fed by
 *  `["candidate", id]` (App.tsx) and the queue by its `["candidates"]` pages (ReviewQueue), so
 *  both re-read; `["watch-evidence"]` is the Settings count of stored records, which really
 *  does drop by one here -- the same key its global Forget invalidates (PlexPanel).
 *
 *  What this does NOT do is re-judge the row: the verdict and its reasons are frozen snapshot
 *  data, so the refetch returns the same explanation and the notice stays up until a scan
 *  reads the title again. Nothing here says otherwise, and the button's help text claims only
 *  which plays get used. */
const WATCH_RECORD_STALE = [["candidate"], ["candidates"], ["watch-evidence"]];

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
  const headingId = useId();

  // Accept what Reaper can see today for THIS title, the narrow twin of Settings' global
  // Forget. It discards the high-water record and nothing else: no file is deleted and no
  // removal is approved, the title simply goes back to being judged on its current plays
  // (`api.settings.forget_watch_evidence_for`).
  const queryClient = useQueryClient();
  // The same handoff the global Forget makes, through the same hook (rule 72): that one is this
  // control's whole-library twin and already pairs the sentence with a place to stand.
  const afterForget = useSuccessorFocus();
  const forgetWatchRecord = useMutation({
    mutationFn: () => api.forgetWatchEvidenceFor(item.media_key),
    onSuccess: () => {
      // Success here is the button DISAPPEARING, replaced in place by the help line below, and it
      // happens inside a `standing` notice that is correctly not a live region -- so nothing was
      // said at all. The same sentence as that replacement, on purpose: one fact, one wording,
      // whichever way the operator meets it (rule 144).
      announce("Reaper will judge this title on what it can see now.");
      afterForget.arriving();
      for (const queryKey of WATCH_RECORD_STALE) void queryClient.invalidateQueries({ queryKey });
    },
  });

  const mediaLabel = item.media_type === "season" ? "TV season" : item.media_type;

  return (
    <WhyShell headingId={headingId} onClose={onClose}>
      {item.poster_url && <WhyHero posterUrl={item.poster_url} />}

      {/* A season's reasoning is one level down from its show: offer the way back up. */}
      {item.group_key && onShowGroup && (
        <button
          type="button"
          className="link-btn back-to-show"
          onClick={() => onShowGroup(item.group_key!)}
        >
          {/* The glyph marks "this goes somewhere" and the destination is already in the words,
              so it is decoration sitting inside a control's accessible name. The space stays
              OUTSIDE the wrapper: inside it, the name fuses into "◂Back" with no pause (#284).
              The five directional glyphs swept in #177 missed this one (rule 72). */}
          <span aria-hidden="true">◂</span> Back to {item.group_title ?? "the show"}
        </button>
      )}

      <div className="why-head">
        <div>
          <h2 id={headingId}>
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
            {itemBytes(item.size_bytes)}, {mediaLabel}
            {/* The Plex library the file lives in -- same quiet chip as the cards, so movies
                and seasons read the same. */}
            <LibraryChip library={item.library} />
            {/* Next to the library, because both answer "where does this file live in Plex".
                Silent on the ordinary single-listing bind. */}
            <MergedListingChip match={explanation.match} />
            {/* All three states here: the panel is where you came to find out. The card
                stays quiet about a show that is still going. */}
            <ShowStatusChip status={item.show_status} />
            <JumpPill href={item.links.tautulli} label="Tautulli" />
            <JumpPill href={item.links.seerr} label="Seerr" />
            <JumpPill href={item.links.radarr} label="Radarr" />
            <JumpPill href={item.links.sonarr} label="Sonarr" />
          </p>
        </div>
      </div>

      <MetaLine item={item} />
      <RatingsRow ratings={item.ratings} links={item.links} />

      {item.summary && <Synopsis text={item.summary} />}

      {/* After the summary, not before it: the block above is what the item IS
          (certification, ratings, synopsis) and reads as one unit, so splitting it with a
          notice buries the identity. Here the notice sits directly above the verdict it
          explains. Movies and seasons share this panel, so both orders move together. */}
      <KeptNotice match={explanation.match} links={item.links} mediaType={item.media_type} />

      {/* The scan's stored reasoning for this one would not read back. Say that plainly:
          the blocks below are empty because nothing could be read, not because nothing
          protected it, and an empty panel alone would read as the second. Same amber
          `.notice-warn` every other "we could not check" uses. */}
      {item.explanation_unreadable && (
        <Notice tone="warn">
          Reaper couldn't read why it judged this one, so the reasons below are missing. Run a scan
          to rebuild them. Nothing is removed on a reason Reaper can't show.
        </Notice>
      )}

      {/* The one hold the operator can lift from here, so the escape lives beside the sentence
          that names it (rule 42) rather than on the Settings page, where the only control is
          the whole-library one.

          `=== true`, never truthy: the field is three-state, and `false` (the scan looked and
          the reading was honest) and `null` (a row scanned before the field existed, which is
          what the server actually sends for one) both mean this control must not appear.
          Offering to discard a watch record on a row that cannot say whether it needs one is
          the wrong direction, so "cannot tell" shows nothing.

          `standing`: this is part of the page for as long as the title is held that way, not a
          reaction to anything the operator pressed, so it is read in document order rather than
          interrupting on every render of the panel. The failure below it is the opposite -- it
          answers a press -- and stays an alert. */}
      {explanation.watch_blind === true && (
        <>
          <Notice tone="warn" standing as="div">
            Plays Reaper recorded earlier are no longer readable. The file may have been added to
            your library again, and Reaper could not match it to your watch history.
            <div className="watch-blind-act">
              {/* Rule 85: the confirmation fires on SETTLED state, never at issuance. It also
                  has to exist at all. This panel is reading the scan's frozen explanation, so
                  the notice above still says "held" after the record is gone -- a refetch
                  returns the same `watch_blind: true` until the next scan re-judges the row.
                  Leaving the button at its resting label there tells the operator nothing
                  happened, and the next press asks the server to forget a record that is
                  already gone. So the action is replaced by what will happen instead. */}
              {forgetWatchRecord.isSuccess ? (
                // The place to stand, and a paragraph rather than a control because the press
                // removes the only control there was -- deliberately, since a second press would
                // forget a record that is already gone. The global twin hands focus to a button
                // that comes back; here there is nothing but this sentence, so it takes
                // `tabIndex={-1}` to be able to hold focus at all.
                //
                // Without it focus sits on `<body>`: the button carries `disabled={isPending}`,
                // so the browser blurs it at the press, and above 900px `WhyShell` is not modal
                // and traps nothing, which makes the next Tab restart at the top of the whole
                // application rather than anywhere in this panel (#393).
                //
                // A reader hears the sentence twice in the common case, once from the live region
                // and once on landing. That is the trade taken on purpose: the alternative on
                // offer was the panel's heading, which is a fixed point but throws the operator
                // out of the block they were reading, and saying one short sentence twice is the
                // smaller cost.
                <p
                  className="help"
                  tabIndex={-1}
                  ref={afterForget.ref as RefObject<HTMLParagraphElement>}
                >
                  Reaper will judge this title on what it can see now.
                </p>
              ) : (
                <>
                  <button
                    type="button"
                    className="ghost sm"
                    disabled={forgetWatchRecord.isPending}
                    onClick={() => forgetWatchRecord.mutate()}
                  >
                    {/* The label carries the pending state, so the control gates itself: a
                        press that lands while the first is in flight would forget a record
                        that is already gone and report a second, contradictory answer. */}
                    {forgetWatchRecord.isPending ? "Using…" : "Use what Reaper sees now"}
                  </button>
                  <p className="help">Use the plays visible today for this title only.</p>
                </>
              )}
            </div>
          </Notice>
          {forgetWatchRecord.isError && <Notice tone="error">That didn't save. Try again.</Notice>}
        </>
      )}

      <Verdict item={item} />

      {item.first_flagged_at && (
        <p className="flagged">
          On the list since {since(item.first_flagged_at)}. Watching it or sparing it keeps it, and
          you still start every removal by hand.
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
        <Notice tone="warn">
          Held back: size unknown. Sonarr and Radarr had none, so Reaper can't tell what removing
          this would free, and won't remove it.
        </Notice>
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
    </WhyShell>
  );
}
