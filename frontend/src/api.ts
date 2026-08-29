// SPDX-License-Identifier: AGPL-3.0-or-later
//
// The wire types, mirrored from the response models. They live across several files:
// reaper/api/schemas.py holds most of them, runs.py's ReapStatus answers the one route
// that deletes, and engine/policy.py and engine/explanation.py hold the rest.
//
// These types are hand-written rather than generated from /openapi.json. Most of the
// field comments below record a fact no generated annotation would carry: when a field is
// null and what to read instead, which of two spare expiries answers which question, and
// why a three-state field must never read as false. Generating the types would delete
// every one of those facts, with nothing to regenerate them from. This file keeps a test
// instead of a generator.
//
// tests/test_api_type_mirror.py checks this file against the server declarations and
// names the field that drifted when one side changes without the other. It compares
// field names and field types. The deliberate type differences are listed by name in
// that file's NARROWED and WIDENED sets; optionality is not compared, and that file
// explains why. A new type with no server model is classified there too, never ignored.

export type Verdict = "condemn" | "protect" | "abstain";

export interface Snapshot {
  id: number;
  created_at: string;
  policy_hash: string;
  horizon_at: string;
  item_count: number;
  degraded: boolean;
  degraded_reason: string | null;
  /** The in-app help page for that reason, by its `docs/registry.ts` id, or null where no page
   *  fits. Most degradations have none: an unreachable Radarr needs no guide. */
  degraded_doc: string | null;
  condemned: number;
  protected: number;
  abstained: number;
  reclaimable_bytes: number;
  /** How many condemned items have no size, and so sit outside the total above rather
   *  than inside it as zeros. Zero for a healthy library, and hidden at zero. */
  unknown_size_items: number;
  /** Every collection this scan saw, mapped from its name to Plex's own member count.
   *  The collection screen's header reads it for "N titles in this collection" beside
   *  the scan's own count. Null when none were read, whether none exist or the read
   *  failed. The UI omits that clause rather than guessing. Optional: most fixtures
   *  do not carry it. */
  collection_sizes?: Record<string, number> | null;
}

/** The one short status chip a card wears, display-ready from the server. `kept`
 *  renders green (a protection fired), `quiet` gray (nothing to act on), `look`
 *  amber-outlined (left for the owner to decide), `held` green-outlined (a protection
 *  that expires, saying how long is left). Filled marks the owner's own decision;
 *  outlined marks Reaper's. `held` is green, not amber, because the file is kept:
 *  amber means only "left for you to decide". */
export interface Chip {
  tone: "kept" | "quiet" | "look" | "held";
  /** The typed id plus its raw params. `why.ts`'s `composeIn` turns this into the
   *  chip's text and its standalone sentence (`StatusChip.tsx`'s `StatusChip` and
   *  `chipWhy`). The server never sends English for this field. */
  reason: ReasonKey;
}

/** One square of a show card's season strip: the lightest per-season mark, across
 *  every lane of the whole snapshot. `season` is null for a row whose key carried
 *  no season number; that row shows unnumbered rather than dropping out. */
export interface GroupSeasonMark {
  /** The candidate id for this season, so clicking its square opens that season's own
   *  reasoning rather than the whole show's panel. */
  id: number;
  season: number | null;
  verdict: Verdict;
  override: Override | null;
  /** For a "reap" override: whether the engine honors it. True paints the square solid
   *  red. False paints it dashed red with a scythe ("kept for now"), for a safety stop
   *  or a row Reaper can't identify. Null when there is no reap override. */
  override_effective: boolean | null;
  /** The season's size on disk, so the card can state whole-show totals without a
   *  second fetch. Null when nothing could report one; that is not the same as zero. */
  size_bytes: number | null;
  /** For a "spare" override: when it stops keeping this season (ISO), or null for a
   *  forever spare. The square's color reads the item's fate, and a spare whose clock
   *  has passed is a fate of its own: it still keeps the file until a scan notices the
   *  expiry, so it stays green, but dashed rather than solid because it is no longer a
   *  live decision. */
  spare_expires_at: string | null;
  /** When the last spare covering this season stops keeping it, or null for a forever
   *  one. This is what the square's color actually reads; `spare_expires_at` above is
   *  the spare a control toggles. See `Candidate.spare_covers_until`. */
  spare_covers_until: string | null;
}

export interface Candidate {
  id: number;
  media_key: string;
  title: string;
  media_type: string;
  /** Null when Reaper could not measure it. Never zero: the UI says "Size unknown", and
   *  the item is held back from any plan. */
  size_bytes: number | null;
  verdict: Verdict;
  score: number;
  coverage_bp: number;
  first_flagged_at: string | null;
  // Display fields captured at scan time. None affect the verdict.
  year: number | null;
  summary: string | null;
  poster_url: string | null;
  requested_by: string | null;
  group_key: string | null;
  group_title: string | null;
  /** Canonical file resolution ("2160", "1080", ..., "sd") for the card's quality badge.
   *  Null hides the badge (TV seasons, unmatched items, rows from older scans). */
  video_resolution: string | null;
  /** The Plex library (section) this item lives in, as the operator named it. A season
   *  reports its show's library. Drives the card and panel library chip, and the library
   *  filter. Null when unknown (unmatched, or a row from before this field shipped); the
   *  chip is then hidden. */
  library: string | null;
  /** The raw dormancy day count of a fresh row; the frontend composes the span. Null on a
   *  legacy row, which shows no amber pill. */
  dormant_days: number | null;
  /** The one-line "why", drawn from the explanation: the protection keeping a spared
   *  item, or the strongest reason a reaped one scored. The card shows this instead of
   *  a synopsis, composed through `why.ts`. A row recorded before typed reasons carries
   *  a `legacy` key that composes to its stored sentence exactly as written. Null only
   *  where the row has no reason at all. */
  reason_key?: ReasonKey | null;
  /** The manual decision in effect: "spare", "reap", or null, whether set on this item
   *  or inherited from its show. It decides the row's chip and score, the item's real
   *  fate. It updates the moment the operator clicks, so the card shows the pending
   *  intent before the next scan applies it. To decide what a control can toggle, read
   *  `override_own` instead. */
  override: Override | null;
  /** This item's own decision, ignoring any it inherits from its show. This is what a
   *  Spare/Reap control on this row toggles. Equals `override` for a movie. Null for a
   *  season kept only because the whole show is spared; `show_override` then says why
   *  it is still kept. */
  override_own: Override | null;
  /** The whole-show decision covering this season, its show's "spare"/"reap", or null.
   *  Drives the "kept because the whole show is spared" note beside a season's control.
   *  Always null for a movie. */
  show_override: Override | null;
  /** For a "reap" override: whether the engine honors it. True means it joins the
   *  counts, the grace countdown and the next plan. False means the engine refuses it,
   *  because someone is watching right now or the file isn't managed. Null when there
   *  is no reap override. The row reads red only when true. */
  override_effective: boolean | null;
  /** When the spare in effect on this item stops keeping it (ISO-8601). Read only when
   *  `override` is "spare": null then means "kept for good", and a value drives the
   *  "N days left" countdown. A season with no spare of its own carries its show's
   *  spare expiry. */
  spare_expires_at: string | null;
  /** When the last spare covering this item stops keeping it, whichever of its own
   *  spare or its show's spare runs longer. Null for a forever one. `spare_expires_at`
   *  above names the one spare a control toggles and clears; this field names when the
   *  file actually stops being kept, which is what a color or a sentence about its fate
   *  must read. The two differ whenever both levels spare an item and the
   *  higher-precedence spare runs out first: a season spared 10 days inside a show
   *  spared forever is kept forever. A show set to reap contributes no cover, so a
   *  season spare lapsing under one still reads as expired. Read only when `override`
   *  is "spare". */
  spare_covers_until: string | null;
  /** When the whole-show spare covering this season stops keeping it (ISO-8601). Read only
   *  when `show_override` is "spare"; null means a forever show-spare. Always null for a movie. */
  show_spare_expires_at: string | null;
  /** The card's one status chip (Sanctuary and Limbo). Null on condemned rows, whose
   *  card leads with the amber dormancy pill instead. */
  chip: Chip | null;
  /** Which season this row is, for season rows. Null for movies and unparseable keys. */
  season_number: number | null;
  /** Whether the show has finished. Null for a movie, where the question does not
   *  apply, and for a row stored before this field existed. Both render nothing. */
  show_status: ShowStatus | null;
  /** This item's Plex collection names (a season reports its show's), sorted smallest
   *  collection first: `CollectionChip` takes element 0. Navigation only; it never
   *  feeds the verdict. Null means "not recorded for this scan" (no Plex configured, a
   *  failed section read, or a row from before this field shipped), not "in no
   *  collection". Render no chip for null rather than an empty one. */
  collections: string[] | null;
  /** Which of three search blocks this row matched: 0 exact title, 1 partial title or
   *  show, 2 collection-name only. Null outside a search. Optional, since no component
   *  reads it yet. */
  search_rank?: number | null;
  /** For a `search_rank === 2` row, the collection name that matched. Read this field
   *  instead of `collections[0]`, which would show the smallest collection rather than
   *  the one the search found. Null for a title match, and outside a search. Optional,
   *  for the same reason as `search_rank`. */
  matched_collection?: string | null;
}

/** Whether a show has finished, as three states rather than a bool, so "the server never
 *  said" can never be drawn as a definite answer. "continuing" is labeled "Still going"
 *  on screen: that arm also covers a show that hasn't started airing yet. */
export type ShowStatus = "ended" | "continuing" | "unknown";

export type Override = "spare" | "reap";

/** One show, whole: the show-level header plus every season row in the latest
 *  snapshot, regardless of verdict. This is what the show panel and the expanded card
 *  read. */
export interface Group {
  group_key: string;
  title: string;
  year: number | null;
  poster_url: string | null;
  summary: string | null;
  /** Summed over the seasons that have a size; `unknown_size_seasons` counts the rest. */
  size_bytes: number;
  unknown_size_seasons: number;
  /** The show-level status line and chip: those of the season that most wants the
   *  owner's attention, else the highest-scoring one. The lead season's typed "why",
   *  exactly as on the candidate. */
  reason_key?: ReasonKey | null;
  /** The show's Plex library (section), shared by all its seasons. Null when unknown.
   *  Drives the show panel's library chip. */
  library: string | null;
  chip: Chip | null;
  /** The show's own decision, the show key's "spare"/"reap", or null. This is what the
   *  panel's whole-show control toggles and lights. It is never an aggregate of the
   *  seasons' own decisions, because that control cannot clear an aggregate. Null until
   *  the whole show is decided. */
  show_override: Override | null;
  /** When the whole-show spare stops keeping the show (ISO-8601), or null for a
   *  forever spare. Read only when `show_override` is "spare"; this drives the panel's
   *  whole-show countdown. */
  show_spare_expires_at: string | null;
  links: Links;
  /** Whether the show has finished, taken from whichever season rows carry it. One
   *  reading of the series is stamped onto every season in the same scan, so they
   *  cannot disagree. Null only when no row carries it, from a snapshot recorded before
   *  this field existed. */
  show_status: ShowStatus | null;
  /** Every season, sorted by season number (unnumbered rows last). */
  seasons: Candidate[];
}

/** What one show on the page looks like across the whole snapshot, sent once per show
 *  rather than stamped onto each of its season rows.
 *
 *  Every figure spans the whole snapshot, never the fetched page: a page can hold some of a
 *  show's seasons and not the rest, and these numbers sit beside "Reap now". */
export interface GroupRollup {
  group_key: string;
  /** How many seasons "Reap now" on this show would plan: its condemned, not-spared
   *  seasons, plus the hand reaps the engine honors. */
  condemned_count: number;
  /** The byte total over that same set: the number the planner will act on. */
  condemned_bytes: number;
  /** How many of those seasons have no size. They are left out of both numbers above,
   *  because the planner will not plan them. */
  unknown_size: number;
  /** Every season of the show, all lanes, for the card's season strip. */
  seasons: GroupSeasonMark[];
}

/** One page of candidates, plus the full-set totals the server measured before the
 *  page window. The queue header uses these totals for its counts and sizes. */
export interface CandidatePage {
  items: Candidate[];
  /** One entry per show with a row on this page. A show straddling two pages appears in
   *  both with the same figures, so merging pages by `group_key` cannot leave a partial
   *  rollup behind. */
  groups: GroupRollup[];
  total: number;
  total_bytes: number;
  /** How many across the whole filtered set have no size. `total_bytes` is the sum of
   *  what is known; this is what it could not include. */
  unknown_size: number;
  /** Where this page starts. The queue asks for `offset + items.length` next. */
  offset: number;
  /** The snapshot this page was drawn from, or null before any scan. The queue compares it
   *  against the newest completed scan to notice when a fresher snapshot has landed under it. */
  snapshot_id: number | null;
}

export type RequestedFilter = "any" | "yes" | "no";
export type OverrideFilter = "any" | "spare" | "reap" | "none";
export type SortKey = "score" | "size" | "year" | "title";
export type SortOrder = "asc" | "desc";

export interface CandidateQuery {
  search?: string;
  media_type?: string;
  requested?: RequestedFilter;
  genre?: string;
  /** Exact name match against a row's stored collection list, over `collections_json`
   *  instead of the genre field it otherwise mirrors. Navigation only; it never affects
   *  the verdict. Spelled `| undefined` because a caller forwards `activeCollection ??
   *  undefined` straight through, and `exactOptionalPropertyTypes` counts an explicit
   *  `undefined` as a value. */
  collection?: string | undefined;
  library?: string;
  override?: OverrideFilter;
  sort?: SortKey;
  order?: SortOrder;
}

/** What a row actually says, for a reader who only sees the number. Four situations all
 *  end at zero points and are otherwise indistinguishable: it pushed toward removing, it
 *  argued for keeping, it did not apply here, or it could not be read. `unreadable` is the
 *  only one that lowers coverage, and the only one the panel renders amber. */
export type SignalState = "adds" | "argues_keep" | "not_applicable" | "unreadable";

/** A typed detail on the wire: the catalog key under `why.*` plus its raw params.
 *  `frontend/src/why.ts` composes it into a sentence; params may nest further keys,
 *  such as a blocked check's cause or the rating gate's per-bar clauses. A row recorded
 *  before this format existed carries a `legacy` key wrapping its stored sentence,
 *  which composes to that sentence exactly as written. */
export interface ReasonKey {
  k: string;
  p?: Record<string, unknown> | null;
}

export interface SignalContribution {
  id: string;
  contribution: number;
  weight: number;
  /** The row's detail: typed on a fresh row, and a `legacy`-wrapped sentence on one
   *  recorded before details were typed. Null only where the row has no detail to show
   *  at all. */
  detail_key?: ReasonKey | null;
  /** False means the input was Unknown. Its weight still counts in the denominator, so
   *  an unevaluated signal can only lower the score, never raise it. */
  evaluated: boolean;
  /** Optional: a row scored before this field existed carries none. Read a missing
   *  value as `not_applicable`, never as `argues_keep`. Claiming an old row argued for
   *  keeping, when nothing recorded whether it did, overstates the case for keeping. */
  state?: SignalState | null;
  /** The ramp this row was scored against: no points below `floor`, all of them at
   *  `saturate_at`. Recorded at scan time, because the live policy may not be the one
   *  this score was computed under, and a panel explaining a score with the wrong line
   *  is worse than one that stays quiet.
   *
   *  `null` covers two cases the panel treats alike: a rule with no ramp (a yes/no rule
   *  of your own either matched or did not), and a row recorded before these fields
   *  shipped. Both mean "no line to state", and both render the plain row. */
  floor?: number | null;
  saturate_at?: number | null;
}

export interface GateOutcome {
  gate: string;
  /** The row's detail: typed on a fresh row, and a `legacy`-wrapped sentence on one
   *  recorded before details were typed. Null only where the row has no detail to show
   *  at all. */
  detail_key?: ReasonKey | null;
  /** Whether the comparison behind a hold is one Reaper actually made. Only the season
   *  keep-rule guard sets it, where a conflict can also mean "a count nobody could read"
   *  or "a watch history too short to stand behind the counts". Those shapes must never
   *  be described as a comparison. A row scanned before this flag shipped carries
   *  neither answer and must assert neither.
   *
   *  `null` is exactly that row, and it is what actually arrives: the stored
   *  explanation has no key, but the response always does, because `GateOutcomeOut`
   *  defaults the field to `None` and nothing sets `exclude_none`, so the server
   *  serializes it as `null`. The `?` defends against a shape the server does not emit;
   *  it is never the case to branch on or test against. Both `undefined` and `null`
   *  mean "names neither shape", never `false`. */
  defers_to_owner?: boolean | null;
  /** Whether this block is a check that never ran, as against one that ran and left
   *  its answer to the owner. Both are blocked and both abstain, so the list they
   *  arrive in cannot tell them apart on its own, and `keepRuleConflict` needs to: a
   *  keep-rule conflict is a decision waiting for a person, while the same guard
   *  failing because a show's Plex data was never resolved asked nobody, and belongs
   *  with the four Plex-dependent gates beside it.
   *
   *  `null` is a row scanned before this flag shipped, and reads as "not this": every
   *  such row's only possible season-guard result in `protections_unknown` was a
   *  conflict, so reading `null` as "not a conflict" is correct for that row. It
   *  arrives as `null` rather than absent, for the same reason `defers_to_owner` above
   *  does. */
  unestablishable?: boolean | null;
}

/** How the item was tied to its Plex library entry. The panel stays quiet on
 *  "matched" and shows a plain "kept to be safe" notice on the other two. Those are
 *  the only cases where the owner needs to know the file was kept because Reaper
 *  could not be sure what it was looking at. */
export interface Match {
  /** `ambiguous` and `conflicted` are not interchangeable, and the panel must not
   *  treat them as one. `ambiguous` means several Plex rows answer to this item, a
   *  library really holding more than one copy. `conflicted` means each kind of
   *  evidence names a different single row, Plex and the *arr describing one file
   *  differently, over a library that may hold exactly one copy. Saying the first
   *  when it is the second sends the owner hunting for a duplicate that is not there. */
  status: "matched" | "unmatched" | "ambiguous" | "conflicted" | null;
  /** Which kind of evidence bound this item, for example `tmdb`. Audit vocabulary: it
   *  is declared here but deliberately not rendered, since the panel keeps id kinds
   *  off the screen. It is typed here so a future reader finds it directly. Null when
   *  nothing bound this item, and for a record stored before this field shipped. */
  by: string | null;
  /** For the audit log, not shown to the owner: "Bound by TMDB id 1001", etc. */
  detail: string | null;
  rating_key: number | null;
  /** Every Plex listing a merged bind covers, when one file is listed several times.
   *  Null on an ordinary single-listing bind, and on a record stored before this field
   *  shipped. This list matters on the deletion path: the executor re-reads it, so all
   *  the listings are protected together, which is why the panel states the count out
   *  loud. */
  merged_rating_keys: number[] | null;
  /** The Plex rows an abstain was choosing between, on `ambiguous` and `conflicted`.
   *  Null outside those two states, and for a record stored before this field shipped.
   *  The panel renders `links.match_candidates` rather than these numbers; they are
   *  here so a reader can tell how many there were without following the links. */
  candidate_rating_keys: number[] | null;
}

/** A graded keep's contribution to the score: points subtracted, and whether it could
 *  be evaluated. False means Unknown, which takes the full discount, so the failure
 *  favors keeping the file. */
export interface KeepContribution {
  name: string;
  discount: number;
  max_discount: number;
  /** The row's detail: typed on a fresh row, and a `legacy`-wrapped sentence on one
   *  recorded before details were typed, the same shape every explanation row carries. */
  detail_key?: ReasonKey | null;
  evaluated: boolean;
}

/** The rewatch-probability context: what fraction of similarly-dormant titles got
 *  watched again, from the operator's own history. Display only. It feeds no verdict
 *  and no signal; the opt-in protective hold reads the recorded cohort facts directly
 *  instead of this block.
 *
 *  `n`/`k` are the block's pooled cohort size and watched-again count. `lo_days`/`hi_days`
 *  is its half-open dormancy range (`hi_days` is null on the open tail bucket). In the
 *  `"no_history"` state there is no usable block and those four fields carry a
 *  placeholder; check `state` first and ignore them in that state. */
export interface RewatchOdds {
  n: number;
  k: number;
  lo_days: number;
  hi_days: number | null;
  state: "measured" | "thin" | "no_history";
  /** The Wilson 95% upper bound of `k`/`n`, as a whole percent. This is the same
   *  figure the rewatch protection compares against the operator's floor, so this
   *  display block never reads a smaller "probability" than the number that can keep
   *  the file. `null` only for a row stored before this field shipped; the panel then
   *  falls back to `why.ts`'s `wilsonUpperPct(k, n)` rather than showing nothing. */
  bound_pct: number | null;
}

export interface Explanation {
  score: number;
  /** The score subtotal before any keep discount is applied. Optional so an item
   *  scored before this field shipped still parses, and nullable because that is what
   *  such a row arrives as: `Explanation` defaults both fields to `None` and nothing
   *  sets `exclude_none`, so the server sends `null` rather than omitting the key.
   *  `WhyPanel` already reads them that way (`base_score != null`, `keep_discount ??
   *  0`); only the type disagreed. */
  base_score?: number | null;
  keep_discount?: number | null;
  /** The score the item had to beat. Null only when the stored explanation could not
   *  be read and the server sent a fallback value instead: the panel omits its "your
   *  threshold is N" clause rather than print a number that is not the operator's
   *  setting. */
  threshold: number | null;
  coverage: number;
  /** The share of evidence that had to be checkable before Reaper would judge this
   *  item, in basis points (5000 = 50%). Recorded beside `threshold` so an abstain
   *  forced by the floor can name the line coverage fell under. Null when the row
   *  could not be read or predates this field: the panel then drops the floor clause
   *  rather than read the live policy. */
  coverage_floor_bp: number | null;
  /** Whether this title is held because the plays Reaper recorded earlier stopped
   *  being readable. The panel offers the per-title escape only on this field.
   *
   *  Three-state, and only `true` may show the control. `false` is the positive claim
   *  that the scan took a reading and it was honest. `null` means "cannot tell": a row
   *  scanned before this field existed, or an item with no reading to judge. It is
   *  what actually arrives for such a row, because `Explanation` defaults the field to
   *  `None` and nothing sets `exclude_none`, so the server serializes it as `null`.
   *  Both `undefined` and `null` mean "cannot tell", never `false`, because offering
   *  to discard a watch record on a guess is the wrong direction. */
  watch_blind: boolean | null;
  signals: SignalContribution[];
  keeps?: KeepContribution[];
  /** Why it is being kept. */
  protections_fired: GateOutcome[];
  /** Protections that were evaluated but did not fire, with the actual numbers. */
  protections_checked: GateOutcome[];
  /** Protections that could not be checked. "We could not look" is not the same as
   *  "we looked and it was fine". Rendering them the same way is the exact mistake
   *  Deleterr makes. */
  protections_unknown: GateOutcome[];
  /** How it was tied to Plex. `null` when the row was never matched, or was scanned
   *  before this field shipped. Both cases already have guards in place, and both
   *  mean nothing to show. */
  match: Match | null;
  /** The rewatch-probability context, movie lane only. `null` for a season row,
   *  since the fit is movie-only, and for a row stored before this field existed.
   *  Both read as nothing to show. */
  rewatch_odds?: RewatchOdds | null;
}

/** One Plex row an abstain could not choose between, with the ways to open it. Reaper
 *  knows nothing about these rows but their keys, so the panel numbers them rather than
 *  naming them. */
export interface CandidateLink {
  rating_key: number;
  plex: string | null;
  tautulli: string | null;
}

/** Where the item can be opened. Each link is null when it cannot be built (unmatched
 *  in Plex, instance removed, a row from an older scan). The panel hides a missing
 *  link rather than rendering a broken one. At most one of radarr/sonarr is set. The
 *  rating-site links back the chips in the ratings row; rotten_tomatoes is a title
 *  search. */
export interface Links {
  plex: string | null;
  tautulli: string | null;
  seerr: string | null;
  radarr: string | null;
  sonarr: string | null;
  imdb: string | null;
  tmdb: string | null;
  rotten_tomatoes: string | null;
  trakt: string | null;
  /** The rows an abstain was choosing between; empty on every other item. `plex` and
   *  `tautulli` above are built from the item's own rating key, which is null for
   *  exactly these items. Without this field the panel would name a problem in Plex
   *  and offer no way to open it. */
  match_candidates?: CandidateLink[];
}

/** The external-ratings row. `imdb` is the same number the score used; the percentage
 *  fields are 0-100 ints. `tmdb` and `trakt` are 0-10 scores in tenths, shown as the
 *  percentages both sites themselves display. Null means that source is unknown for
 *  this item. */
export interface Ratings {
  imdb: number | null;
  imdb_votes: number | null;
  rt_critic: number | null;
  rt_audience: number | null;
  tmdb: number | null;
  trakt: number | null;
}

export interface CandidateDetail extends Candidate {
  explanation: Explanation;
  /** True when `explanation` is a fallback value from the server rather than what the
   *  scan stored. The panel says so, instead of rendering empty reason blocks that
   *  would read as "nothing protected this" when the truth is that nothing could be
   *  read. */
  explanation_unreadable?: boolean;
  links: Links;
  ratings: Ratings | null;
  content_rating: string | null;
  runtime_minutes: number | null;
  genres: string[];
}

export interface GateSetting {
  gate: string;
  enabled: boolean;
  threshold: number;
  window_days: number;
}

export interface SignalSetting {
  signal: string;
  weight: number;
  saturate_at: number;
  floor: number;
}

/** Try one signal's settings against one value.
 *
 *  `kind` is a discriminator, not decoration. When a second probe joins this union,
 *  such as what a keep rule would discount or what a graded rule of your own would
 *  add, every client already sending `kind` keeps working. Typing the discriminator
 *  before the format ships is far cheaper than inferring the shape later from which
 *  fields turned up. */
export interface SignalProbe {
  kind: "signal";
  signal: string;
  weight: number;
  saturate_at: number;
  floor: number;
  /** In the units the signal stores: days, watchers, a season's rank, a rating in tenths,
   *  or bytes. */
  value: number;
}

/** What `POST /api/policy/probe` accepts. One member today; see `SignalProbe`. */
export type PolicyProbe = SignalProbe;

/** One answer, the same shape for every probe kind, so a new kind needs no new rendering. */
export interface PolicyProbeResult {
  /** What the rule moves the score by, in its own direction. This is the only field:
   *  `signalRamp.ts` composes both the editor's sentence and the panel's row from it,
   *  so no separate wording field is needed. */
  points: number;
}

export interface Condition {
  field: string;
  op: string;
  value: number | string | boolean;
}

/** A user-authored "reason to remove". Boolean: a match adds the full weight. Graded: a
 *  numeric field ramped floor->saturate, like a built-in signal. Both unsigned. */
export type CustomCondemn =
  | {
      kind: "boolean";
      name: string;
      field: string;
      op: string;
      value: number | string | boolean;
      weight: number;
    }
  | {
      kind: "graded";
      name: string;
      field: string;
      weight: number;
      saturate_at: number;
      floor: number;
    };

/** A user-authored graded "lean toward keeping": a subtractive discount, fail-closed. */
export interface GradedKeep {
  name: string;
  field: string;
  /** For a membership field (`on_list`): which list, by name. That keep is flat: being
   *  on the list takes the full `max_discount`, being off it takes none, so the ramp
   *  fields are inert (send floor 0, saturate_at 1). Null or absent for every numeric
   *  field. */
  value?: string | null;
  max_discount: number;
  floor: number;
  saturate_at: number;
  direction: "high_keeps" | "low_keeps";
}

/** Which rating source a keep bar reads. Movies can back every source (Radarr carries
 *  them); TV backs IMDb plus whatever Plex serves for the show. */
export type RatingSource =
  "imdb" | "tmdb" | "rotten_tomatoes_critic" | "rotten_tomatoes_audience" | "metacritic";

/** One "keep it if it clears this bar" rule. `floor` is in tenths (7.5 -> 75), and
 *  reads the same for a percentage source (75% -> 75). `min_votes` only applies on
 *  sources that count votes (IMDb, TMDb); it is 0 for the percentage sources. */
export interface RatingRule {
  source: RatingSource;
  floor: number;
  min_votes: number;
}

/** The built-in rewatch keep's name, as it arrives on `KeepContribution.name`.
 *  Mirrors `engine/signals.py`'s `REWATCH_KEEP`, and a mirror test pins the two
 *  together. Declared beside `PolicyBody` because that is where the keep's own four
 *  fields live. */
export const REWATCH_KEEP = "rewatch_habit";

export interface PolicyBody {
  name: string;
  media_type: string;
  condemn_at: number;
  coverage_floor_bp: number;
  keep_last_seasons: number;
  keep_first_season: boolean;
  keep_last_scope: "all" | "requested";
  season_lookahead: number;
  keep_in_progress: boolean;
  in_progress_hold_days: number;
  keep_specials: boolean;
  protect_incomplete_seasons: boolean;
  flag_keep_conflicts: boolean;
  gates: GateSetting[];
  signals: SignalSetting[];
  protect_conditions: Condition[];
  custom_condemn: CustomCondemn[];
  graded_keeps: GradedKeep[];
  // The built-in rewatch keep's knobs, live on both lanes: a movie body counts title
  // re-watches, a TV body counts whole show re-watches.
  rewatch_keep_enabled: boolean;
  rewatch_keep_discount: number;
  rewatch_min_viewings: number;
  rewatch_recent_days: number;
  keep_rating_rules: RatingRule[];
  keep_rating_match: "any" | "all";
}

/** The distribution of content-season counts across shows in the latest snapshot, so the
 *  editor can show live how many shows a keep-last-N value fully protects. */
export interface SeasonShape {
  total_shows: number;
  season_counts: Record<number, number>;
}

/** One measured rung of the fitted rewatch ladder: a merged dormancy block from the
 *  latest scan's fit, aggregated across every movie candidate that landed in it. */
export interface RewatchOddsBlock {
  lo_days: number;
  hi_days: number | null;
  n: number;
  k: number;
  /** The Wilson 95% upper bound of k/n, in percent: what the hold actually compares against
   *  the operator's threshold, so the ladder and the gate can never disagree. */
  upper_bound_pct: number;
  /** Candidates of the latest scan (movies, or seasons on the TV lane) whose current
   *  dormancy falls in this rung: what the consequence echo counts. */
  items: number;
}

/** The latest scan's fitted rewatch curve, for the Policy page's ladder and consequence
 *  echo. Empty `blocks` with `total_items === 0` means no scan has run on this build yet. */
export interface RewatchOddsFit {
  blocks: RewatchOddsBlock[];
  /** Every candidate of the latest scan on the requested lane, block or no block: what the
   *  consequence echo states its protected count out of. */
  total_items: number;
}

/** One legal delete-threshold score, from a scan with a trusted rewatch cohort somewhere in
 *  it: what the Policy page's consequence sentence reads off, for whichever score the
 *  slider currently sits on. */
export interface ThresholdCurveMeasuredRow {
  score: number;
  /** How many titles the newest scan would put in front of the operator at `score`. */
  flagged: number;
  /** About how many of `flagged` the operator's own history says come back, rounded up so
   *  the sentence never understates the risk. */
  expected_mistakes: number;
}

/** The whole score-to-consequence curve, from a scan with a trusted rewatch cohort
 *  somewhere in it. One row per legal score that flags anything at all, in ascending score
 *  order; a score between two rows, above the highest one, or below 1 flags nothing this
 *  scan measured. */
export interface ThresholdCurveMeasured {
  state: "measured";
  rows: ThresholdCurveMeasuredRow[];
}

/** One legal delete-threshold score's flagged count, from a scan with no rewatch cohort
 *  this server trusts anywhere. The count is real; a comeback estimate would not be. */
export interface ThresholdCurveCountsOnlyRow {
  score: number;
  flagged: number;
}

/** No candidate anywhere in this scan has a cohort large enough to trust, so the sentence
 *  renders its count clause alone, never a made-up comeback estimate. */
export interface ThresholdCurveCountsOnly {
  state: "counts_only";
  rows: ThresholdCurveCountsOnlyRow[];
}

/** No scan has run on this build yet. The editor renders nothing rather than a locked or
 *  error state: this is a readout, not a setting, and the slider keeps working exactly as
 *  it does today. */
export interface ThresholdCurveNoScan {
  state: "no_scan";
}

/** What `GET /api/policy/threshold-curve` answers: exactly one of the three states above,
 *  never inferred from which fields happen to be present. Mirrors
 *  `api.schemas.ThresholdCurveOut`. */
export type ThresholdCurve =
  ThresholdCurveMeasured | ThresholdCurveCountsOnly | ThresholdCurveNoScan;

/** What a vocabulary field's value is, which decides how it is typed, stored and read
 *  back (`engine/fields.py`'s `FieldType`). Two of the six convert: a size is typed in
 *  GB and stored in bytes, a rating is typed as 7.5 and stored as 75. Days are typed
 *  and stored alike.
 *
 *  Typing this as a union rather than a bare `string` lets `test_api_type_mirror.py`
 *  catch a member the server adds that this file does not yet know about. Its failure
 *  names every site in `PolicyRuleEditors.tsx` that dispatches on this value, and none
 *  of them is exhaustive: a member none handles takes a fall-through arm rather than
 *  failing. That list of sites lives in that test file, not here, so the two stay in
 *  step. */
export type FieldType = "days" | "bytes" | "count" | "rating_tenths" | "bool" | "text";

/** One field the owner may write a protect condition about, from the vocabulary
 *  endpoint. The label, help paragraph and unit are not on the wire: the browser reads
 *  them from the catalog by this key (`why.field.<key>`, `policyRules.fieldHelp.<key>`,
 *  `policyRules.fieldUnit.<key>`). */
export interface VocabField {
  key: string;
  type: FieldType;
  ops: string[];
}
export interface Vocabulary {
  lane: string;
  fields: VocabField[];
}

export interface PolicyWarning {
  field: string;
  reason: ReasonKey;
  severity: string;
}

export interface Policy {
  policy_hash: string;
  name: string;
  /** The shipped bounds for this media type's signals, so a changed one can be put
   *  back. Sent from the server rather than copied into this file, so the number the
   *  scorer actually reads is declared in one place.
   *
   *  Weights ride along, and the editor ignores them: removal weights must total
   *  exactly 100, so restoring one on its own would break the budget the save bar
   *  enforces. */
  default_signals?: SignalSetting[];
  /** How far back your watch history goes, for the editor to display beside the
   *  controls it bounds. A never-played title is measured from the later of its
   *  arrival and this edge, so this is the largest dormancy anything can present: a
   *  ramp whose far end sits past it can never pay out in full. `null` when the scan
   *  did not record it, which the editor renders as not knowing rather than as no
   *  history at all. */
  history_reach_days?: number | null;
  body: PolicyBody;
  warnings: PolicyWarning[];
  /** Every way this body had to be repaired to load it. It is not what is stored.
   *
   *  The editor reads the length of this list to open dirty, and reads the copy per
   *  member separately. That split matters: a repair the server reports raises the
   *  save bar whether or not anyone wrote a sentence for it, so the operator always
   *  has a Save button to press when the stored policy needed a repair. */
  repairs?: PolicyRepair[];
}

/** One way the server had to change a stored policy body to load it.
 *
 *  Mirrors `PolicyRepair` in `src/reaper/engine/policy_migrations.py`, which is the
 *  real declaration. A member the server adds that this union does not yet list is
 *  handled rather than assumed away (`REPAIR_NOTICES` in `PolicyEditor.tsx`). Widened
 *  to `string` on purpose, so an unknown id is a value TypeScript admits exists, not a
 *  cast. */
export type PolicyRepair =
  "rescaled" | "fell_back" | "rating_rules_restored" | "lists_migrated" | (string & {});

/** One title the draft would newly flag, for the simulator's "New on the list" block. */
export interface SimExample {
  title: string;
  year: number | null;
  score: number;
}

/** One protection and how many items it is keeping, for "Why titles were spared". */
export interface GateCount {
  gate: string;
  count: number;
}

/** Why the simulator would not answer. Mirrors `api.schemas.SimStale`, and the panel
 *  branches on it for the heading; the body paragraph is `stale_reason` beside it, composed
 *  from the catalog. */
export type SimStale = "gathers_differently" | "seasons_not_recorded" | "in_progress_not_read";

export interface Simulation {
  /** Whether these numbers actually answer the question that was asked. False when the
   *  candidate policy changed a weight or a gate, in which case the stored scores were
   *  produced by a different policy and every count below is zeroed. */
  exact: boolean;
  /** Which refusal this is. Null exactly when `exact`. */
  stale_kind: SimStale | null;
  /** The catalog id for the refusal, composed under `policySim.staleReason.<id>` by
   *  `PolicySimulator.tsx`'s `StaleNotice` (`composeIn`). */
  stale_reason: ReasonKey | null;
  condemned: number;
  protected: number;
  abstained: number;
  reclaimable_bytes: number;
  /** How many of the condemned have no size, left out of the total above. Hidden at zero. */
  unknown_size_items: number;
  /** How many of `condemned` are titles the operator marked to reap by hand. A hand reap
   *  condemns at any threshold, so these never move with the sliders and the panel says
   *  so under the headline. */
  hand_reaped: number;
  newly_condemned: number;
  no_longer_condemned: number;
  /** How many titles the last scan flags: the stored verdicts with overrides applied,
   *  which is what the panel's compare line names. Not the saved policy: saving starts
   *  a scan rather than being one, so the two differ until it finishes, and keep
   *  differing if the scan fails. The server counts this per row directly rather than
   *  the panel reconstructing it from the two deltas below, since that reconstruction
   *  only works while both deltas count every way into and out of the removal list. */
  condemned_before: number;
  /** Titles this draft puts in a different lane than the one they are in now. This is
   *  a superset of the two deltas above, which cannot see a protection edit that moves
   *  a title between spared and not judged. */
  changed_titles: number;
  histogram: number[];
  /** Populated only when exact; empty on a stale refusal, like every count above. */
  examples_newly_condemned: SimExample[];
  protected_by: GateCount[];
}

/** Distinct values the latest scan saw for one rule field. Suggestions only: an unknown
 *  field or a missing scan is an empty list, and typing an unlisted value stays valid. */
export interface FieldValues {
  field: string;
  values: string[];
}

export interface ActionStep {
  media_key: string;
  ordinal: number;
  kind: string;
  method: string;
  path: string;
  body: Record<string, unknown> | null;
  state: string;
  is_canary: boolean;
  /** Why this step failed or was skipped, as a typed reason: `null` on a step that has
   *  not run or that succeeded. Compose with `composeError` (`why.ts`); a `legacy` key
   *  composes to the sentence a row recorded before typed reasons existed, exactly as
   *  written. */
  error_reason: ReasonKey | null;
}

export interface Run {
  id: number;
  snapshot_id: number;
  state: string;
  item_count: number;
  total_bytes: number;
  confirmation_phrase: string;
  /** How many condemned items this plan left out because nothing would report their
   *  size. The plan is smaller than the queue implied, and this is what says so. */
  held_back_unknown_size: number;
  /** How many journal rows this run holds in total. `steps` below is a window, so
   *  anything counting rows should read this field: `steps.length` is the size of the
   *  page, never the size of the plan. `item_count` is not it either, since that counts
   *  deduplicated candidates, and a season is three steps sharing one key. */
  step_count: number;
  /** The first page of the journal, not all of it. `api.runSteps` serves any window. */
  steps: ActionStep[];
}

/** One window of a run's journal, from `GET /api/runs/{id}/steps`. Its own route rather than
 *  query parameters on the run detail: building that response re-derives the confirmation
 *  phrase, and the sheet holds the detail under one cache key with an infinite stale time so
 *  it keeps the exact plan it opened with. */
export interface RunSteps {
  steps: ActionStep[];
  step_count: number;
  offset: number;
}

/** One line of the run history: the stored row, and nothing derived. A past plan's
 *  counts, totals and phrase would all have to be re-derived from today's overrides,
 *  which costs a whole snapshot's candidates per run and, for a finished run,
 *  describes a plan that never existed. So the list carries none of them, and opening
 *  a row fetches the full `Run` instead. */
export interface RunSummary {
  id: number;
  state: string;
  approved_at: string;
  /** When the run reached a terminal state (completed or aborted). `null` while a run
   *  is planned or executing. */
  finished_at: string | null;
  /** Why the run stopped early, as a typed reason: `null` on a run that did not abort.
   *  Read back from storage the same way `ActionStep.error_reason` is. */
  aborted_reason: ReasonKey | null;
  /** How many items this run actually deleted. `null` until the run reaches a terminal
   *  state, read as unknown rather than zero. */
  deleted_items: number | null;
  /** Bytes reclaimed by `deleted_items`. `null` on the same terms. */
  deleted_bytes: number | null;
  /** How many of `deleted_items` had no size, so are absent from `deleted_bytes`. `null`
   *  on the same terms; above zero only when the operator's unmeasured allowance was
   *  open. */
  deleted_unmeasured: number | null;
  /** How many planned items this run left alone. `null` on the same terms. */
  skipped: number | null;
}

/** A page of the run history, plus how many rows match the request as a whole: the history
 *  footer's "Showing N of M" and the scroll paging that stops at the end both need `total`,
 *  which `runs.length` cannot answer once the list is paged. */
export interface RunList {
  runs: RunSummary[];
  total: number;
}

export interface RunCheck {
  /** The live reason the executor recorded this checklist line with, as a typed
   *  reason. Always present, since a check without one would have nothing to render. */
  label_reason: ReasonKey;
  ok: boolean;
}

export interface RunOutcome {
  media_key: string;
  title: string;
  kind: string;
  state: string; // verified | failed | skipped
  /** The live reason the executor recorded for this outcome, as a typed reason. Always
   *  present, the same way `RunCheck.label_reason` is. */
  detail_reason: ReasonKey;
  checks: RunCheck[];
  /** True when this item was the run's canary: the smallest item, executed (or, in a
   *  dry run, proven) first. The same fact `ActionStep.is_canary` carries for the step
   *  table. */
  is_canary: boolean;
}

export interface RunReport {
  run_id: number;
  dry_run: boolean;
  state: string;
  /** Why the run stopped early: the live reason the executor recorded on the run report, as
   *  a typed reason. `null` on a run that did not abort. */
  aborted_reason: ReasonKey | null;
  /** What a real run actually deleted, or what a dry run proved it would delete. */
  would_delete_items: number;
  /** The bytes behind `would_delete_items`, real or proven, minus the unmeasured ones. */
  deleted_bytes: number;
  /** How many deleted items had no size, so are absent from `deleted_bytes`. Above zero
   *  only when the operator allowed unmeasured items. Hidden at zero. */
  deleted_unmeasured: number;
  /** Items a check kept, in a real run and a dry run alike. */
  skipped: number;
  outcomes: RunOutcome[];
}

/** One item's outcome, reconstructed from the durable journal rather than the live
 *  in-memory report `RunOutcome` carries. `error_reason` is optional here, unlike
 *  `RunOutcome.detail_reason`: a verified step's success sentence lives only in the
 *  in-memory report, and the journal's own error column is null on a step that
 *  succeeded, so this mirrors `ActionStep.error_reason`'s own convention. */
export interface RunOutcomeRead {
  media_key: string;
  title: string;
  kind: string;
  size_bytes: number | null;
  state: string; // verified | failed | skipped
  error_reason: ReasonKey | null;
  is_canary: boolean;
  /** Whether the file's removal was confirmed. A failed step can carry true: the delete
   *  landed and a follow-up did not, so the row must say "removed", never "kept". */
  file_removed: boolean;
}

/** One window of a run's outcomes so far, from `GET /api/runs/{id}/outcomes`. Answers a
 *  run still executing exactly as it answers one long finished: an item with no decided
 *  outcome yet is left out, so the list grows as a run in flight goes. */
export interface RunOutcomes {
  outcomes: RunOutcomeRead[];
  /** How many items have a decided outcome so far, not the plan's whole item count. */
  outcome_count: number;
  offset: number;
}

/** A running (or just-finished) reap. Polled while a reap is in flight, and read once on
 *  load to re-attach to one already running. A reap runs detached from the request that
 *  started it, so it survives navigating away and closing the tab. */
export interface ReapStatus {
  running: boolean;
  run_id: number | null;
  /** The operator pressed Stop. The run halts after the item in flight, gracefully. */
  stopping: boolean;
  /** idle | reaping | complete | aborted | error */
  phase: string;
  done: number;
  total: number;
  deleted_items: number;
  deleted_bytes: number;
  skipped: number;
  /** The item last acted on, for the live line. */
  title: string;
  /** Why the run stopped, composed under `error.*` with `composeError` (`why.ts`). `null`
   *  while running and on a clean finish. */
  error_reason: ReasonKey | null;
}

export interface ProfileSettings {
  max_items_per_run: number;
  max_bytes_per_run: number;
  max_items_per_30d: number;
  max_bytes_per_30d: number;
  /** Whether the four caps above are enforced at all. On by default; off drops the
   *  run-size ceilings for a big first cleanup while every other gate stands. Never
   *  governs `max_unmeasured_per_run`. */
  caps_enabled: boolean;
  grace_days: number;
  /** How many items with no size one run may delete. 0, the default, means never: the GB
   *  caps cannot bound them, so this count is the only bound there is. */
  max_unmeasured_per_run: number;
  /** Read-only (GET). True when the stored settings could not be read and these are
   *  the shipped defaults, which can be looser than what was saved. The Pace page
   *  shows a recovery notice, and a scan runs degraded (untrusted, so nothing can be
   *  deleted from it) until the operator saves again. Absent or ignored on save. */
  settings_recovered?: boolean;
}

export interface WhitelistEntry {
  media_key: string;
  title: string;
  note: string | null;
  decision: Override;
  /** When a timed spare stops keeping the item (ISO-8601). Null means kept forever, and
   *  always null for a reap. */
  spare_expires_at: string | null;
  created_at: string;
}

export interface SignalCount {
  /** A built-in signal id or a custom rule's name. */
  id: string;
  count: number;
}

/** One protection list, and whether it is still protecting anything.
 *
 *  `state` is decided on the server, so this screen and the degraded-scan notice
 *  cannot tell the operator two different stories about the same failed check.
 *  `item_count` is what the stored copy still covers: a `failing` list above zero went
 *  on protecting those titles, because a failed refresh leaves the previous membership
 *  in place.
 *
 *  `name` comes from Plex or an *arr, so a surface rendering it wraps.
 */
export interface ProtectionList {
  /** The stable key rows are keyed on. Never shown, because a display name can collide. */
  slug: string;
  name: string;
  /** Which family this belongs to. The panel groups on it: one protection, not one
   *  row per *arr instance. Never derived in the component, since the slug spellings
   *  live server-side. */
  source: "arr_tag" | "plex_collection" | "plex_watchlist" | "imdb";
  state: "working" | "stale" | "failing" | "never_checked";
  item_count: number;
  /** When the last check that actually landed was. Null when none ever has. */
  last_checked_at: string | null;
  /** What the last failed check said, from the service that refused. Null when none did. */
  error: string | null;
  /** Which `ListConfig` this membership was synced for, so the panel can put Edit and
   *  Check now on the row without working out from a slug which definition made it.
   *  Several rows share one id, since a tag list is synced once per *arr instance. It
   *  is null for a row stored before its definition existed, and the next successful
   *  check re-homes it. Derived on the server, beside the slug spellings, never parsed
   *  here. */
  list_id: number | null;
  /** A tag list's per-tag counts from the last good check, by the operator's own spelling of
   *  each tag. Null for every other source, and for a row that has not synced since the
   *  counts started being recorded: unknown, never zero. */
  tags: Record<string, number> | null;
  /** Which *arr instance a tag list's row was read from, for the per-server counts. The
   *  operator named the instance on Settings; null for every other source. */
  server: string | null;
  /** Which media types this row's stored members span. Empty until the first sync. The
   *  panel compares it against the types a keep rule names (`policy_use`), so a rule
   *  covering one side of a mixed list reads as partial cover, not full. */
  media_types: ("movie" | "tv")[];
}

/** The settings one list source needs. A union in practice, kept as one
 *  optional-field shape because that is what crosses the wire and what
 *  `list_config._clean_config` validates: it reads only the keys its own source
 *  defines and refuses a shape that could never match.
 *
 *  A type alias rather than an interface, so it stays out of the wire-type mirror's
 *  walk. The server side is a bare `dict[str, Any]` for the same reason: the
 *  validation is per source rather than per model. */
export type ListConfigBody = {
  /** `plex_collection`: which library to look in, and which collection inside it. */
  library?: string;
  collection?: string;
  /** `arr_tag`: the tag spellings, and whether a title needs any of them or all of them. */
  tags?: string[];
  match?: "any" | "all";
  /** `imdb`: a shipped chart's key ("top250", "popular")... */
  preset?: string;
  /** ...or a public list's id. The server accepts a pasted URL and keeps the id inside it. */
  list_id?: string;
};

/** One keep rule naming a list, for the row's "how Policy uses it" line. Empty `policy_use`
 *  means no rule does, which the screen renders as a warning: a defined list that protects
 *  nothing. */
export interface ListPolicyUse {
  media_type: "movie" | "tv";
  strength: "hard" | "lean";
  /** The lean's discount. Null for a hard rule, which keeps outright. */
  points: number | null;
}

/** One list definition: what the operator named and where it points.
 *
 *  The other half of `ProtectionList`, which is what that definition is currently
 *  protecting. They are two tables on purpose. A definition lives in `reaper.db` and
 *  is not rebuildable from anything; membership is a mirror of somebody else's data in
 *  the cache. The two join on `ProtectionList.list_id`. A definition exists from the
 *  moment it is saved; its membership does not exist until a sync has run, so a new
 *  list has a row here and none there.
 */
export interface ListConfig {
  id: number;
  /** The operator's own words, so a surface rendering it wraps. */
  name: string;
  source: "arr_tag" | "plex_collection" | "plex_watchlist" | "imdb";
  config: ListConfigBody;
  /** How the policies use this list right now: one entry per keep rule naming it. */
  policy_use: ListPolicyUse[];
  /** The media types a keep rule on this list can be authored for: the set the Policy
   *  picker offers it on (`policy_migrations.authorable_media_scope`). A Plex
   *  collection takes its library's kind, and a watchlist takes both, known before any
   *  sync; a tag or IMDb list is known only once a sync has read it. Empty means offer
   *  on neither, since the type is unknown and a rule could then keep nothing. */
  authorable_media: ("movie" | "tv")[];
}

/** What one "Check now" did. Each failed list's own error is on its row, which the screen
 *  refetches, so this is only what the button says when it settles. */
export interface ListSyncResult {
  /** Stored rows whose check landed. A tag list across two *arr counts twice. */
  checked: number;
  failed: number;
  /** Set when Plex could not be reached at all, so no collection row carries an error
   *  explaining why it was not checked. Null when Plex answered or none is linked. The
   *  catalog id plus Plex's own error text as a raw `error` param, composed under
   *  `lists.plexError` by `ListsPanel.tsx`. */
  plex_error_reason: ReasonKey | null;
}

/** What Plex would remove besides the files a reap deletes.
 *
 *  Reaper's end-of-run purge empties a library's whole trash, not just the part this
 *  run caused, so anything already in there loses its watch history, ratings and
 *  collections too. Those items sit on both sides of the executor's before/after
 *  count, so its gate cannot see them. No file on disk is affected either way.
 */
export interface PlexTrash {
  /** False when no Plex server is linked, in which case nothing purges. */
  configured: boolean;
  /** Items in the trash across the libraries included in scans. A floor: it counts
   *  only the libraries that answered, so read it together with `sections_unreadable`. */
  trashed: number;
  /** Libraries whose trash could not be counted. Nonzero means `trashed` is incomplete,
   *  and the page warns rather than reading silence as zero. */
  sections_unreadable: number;
  /** Plex's own server-wide "empty trash after every scan" preference, which ships on.
   *  When true, Plex purges the trash itself, outside Reaper's interlock. Null if
   *  unread. */
  empties_after_scan: boolean | null;
}

export interface ReapBreakdown {
  /** False before the first scan, when every figure is zero. */
  has_snapshot: boolean;
  policy_condemned: number;
  policy_condemned_bytes: number;
  hand_spared: number;
  /** The share of `hand_spared` a scan would hand back to policy: titles kept out of
   *  the plan by a spare whose clock has already passed. They are still being kept,
   *  since only a scan notices a spare's expiry, so they are absent from the plan with
   *  nothing else on the page to explain it. The page shows one notice when this is
   *  nonzero.
   *
   *  This counts titles, not spares: one whole-show spare can be holding several
   *  condemned seasons. A title another spare still covers is not counted, since a
   *  scan would not release it. */
  spares_expired: number;
  hand_reaped: number;
  hand_reaped_bytes: number;
  /** Hand reaps the engine won't honor yet, so they are not in `will_reap`. The page shows
   *  one line when nonzero so the operator's held marks are not silently dropped. */
  hand_reaped_held: number;
  will_reap: number;
  will_reap_bytes: number;
  will_reap_unknown: number;
  movies: number;
  /** The unmeasured share of `movies`, so the split can subtract exactly the rows the
   *  planner holds back and stay in step with the total beside it. */
  movies_unknown: number;
  seasons: number;
  /** The unmeasured share of `seasons`; see `movies_unknown`. */
  seasons_unknown: number;
  /** Why the policy condemned them, most-common first. Overlapping: a title trips several. */
  condemned_by: SignalCount[];
}

export interface LeavingSoonResult {
  /** Whether the pass did what it set out to do. Running in preview is not a failure.
   *  No library turned on, or one that failed, is. */
  ok: boolean;
  /** The typed reason describing this pass, the same one stored on the Jobs row at
   *  the same time. Compose it with `jobResultText` (`JobStatus.tsx`); never write
   *  English for it here. */
  result_reason: ReasonKey;
  // No `problems` field: `result_reason` names the libraries that failed. See
  // `LeavingSoonOut` for the whole reason.
}

export interface WatchEvidence {
  /** How many titles Reaper holds a watch record for. */
  titles: number;
  /** How many items the last scan found had plays it could no longer read. `null`
   *  when no scan has recorded it, either because none has run or because the newest
   *  one predates the count. Render that as "not recorded", never as zero: a scan
   *  that did not count is not a scan that counted none.
   *
   *  Never render this as items held back or kept. It counts what was measured, not
   *  what was decided, and the hold it usually causes comes from three gates the
   *  operator can each switch off. "Held back" is also this app's phrase for an item
   *  with no readable size. See `watchEvidenceStatus` in `PlexPanel.tsx`, which is the
   *  one place this number is worded. */
  held_back: number | null;
}

export interface LeavingSoonSettings {
  enabled: boolean;
  allow_unarmed: boolean;
  /** What the operator calls the shelf: one name for the Plex collection and the label. */
  name: string;
  /** What Plex still shows. Equal to `name` except between saving a rename and the pass that
   *  carries it across, which is the window the Plex panel and the Jobs row report. */
  applied_name: string;
  last: {
    at: string;
    movies: number;
    seasons: number;
    applied: boolean;
    /** Whether the last sync did what it set out to do: no library failed, and there was
     *  one turned on to update. Never false merely because it ran in preview (unarmed). */
    ok: boolean;
    /** The pass's own typed reason, composed under `jobs.result.*` with `jobResultText`
     *  (`JobStatus.tsx`). Never written as English here. */
    result_reason: ReasonKey;
  } | null;
  /** A scan that finished without updating the shelf, and why. Reported beside `last`
   *  rather than replacing it: a skipped pass writes nothing to Plex, so the last
   *  completed pass's counts are still what is on the shelf. Nothing clears this
   *  field, so the reader prefers it only while it is newer than `last`, the same way
   *  `ScanRow` treats a scheduled scan that crashed. */
  last_skip: {
    at: string;
    /** Why, as a typed reason: composed through `why.ts`'s `composeIn("error", ...)`,
     *  trailing the exact time on the row's last-run line. A row written before this
     *  format existed carries `{k: "legacy", p: {text}}`, which composes to its
     *  stored text the same way `why.ts` handles any other legacy reason. */
    result_reason: ReasonKey;
  } | null;
}

export interface PlexResourceConnection {
  uri: string;
  local: boolean;
  relay: boolean;
  protocol: string;
}

export interface PlexResource {
  name: string;
  machine_identifier: string;
  /** Whether this is the server Reaper is linked to right now. */
  current: boolean;
  connections: PlexResourceConnection[];
}

export interface PlexResources {
  /** "plex.tv" when live; "stored" when plex.tv was unreachable and this is the linked
   *  server's remembered addresses (possibly stale). */
  source: "plex.tv" | "stored";
  servers: PlexResource[];
  /** The signed-in Plex account's name (the person, not the server). Present on the live
   *  path only; null on the stored fallback, where the UI shows the server name instead. */
  owner_username: string | null;
}

export interface PlexLibrary {
  key: number;
  title: string;
  kind: "movie" | "show";
  enabled: boolean;
}

export interface About {
  version: string;
  license: string;
  data_dir: string;
  reaper_db_bytes: number;
  cache_db_bytes: number;
}

/** One release the operator has not taken yet, for the what-changed modal. `notes` is
 *  the release's own changelog, GitHub-flavored markdown. */
export interface ReleaseChange {
  version: string;
  url: string | null;
  notes: string | null;
}

/** Whether a newer Reaper exists, on this build's channel: a release build follows
 *  published releases, everything else follows the dev branch. `update_available` is
 *  three-state. It is `null` when the check is off, unreachable, or the versions
 *  cannot be ordered, and the surfaces render that as nothing: the check informs,
 *  never gates. `changes` lists every release newer than the running one, newest
 *  first. It is empty unless `update_available` is true, and always empty on the dev
 *  channel. */
export interface Update {
  channel: "release" | "dev";
  enabled: boolean;
  current: string;
  latest: string | null;
  update_available: boolean | null;
  url: string | null;
  checked_at: string | null;
  changes: ReleaseChange[];
}

/** Which screens the review queue opens a show's season list on by default. Mirrors
 *  `app_settings.ExpandSeasonsMode`. "both" means expanded on every screen. */
export type ExpandSeasonsMode = "off" | "desktop" | "both" | "mobile";

/** The desktop build's own knobs, present only when Reaper runs as the Mac or Windows
 *  app. Backed by launcher.conf in the data folder, which the launcher reads at start,
 *  so every change applies the next time Reaper opens. */
export interface DesktopSettings {
  platform: "macos" | "windows";
  /** The menu-bar (macOS) / tray (Windows) icon with Open Reaper and Quit. */
  tray: boolean;
  /** macOS only: show the Dock icon beside the menu-bar icon. The row never renders
   *  on Windows and the PUT refuses to set it there; the reported value echoes
   *  launcher.conf, which nothing on Windows reads. */
  dock_icon: boolean;
}

export interface GeneralSettings {
  application_name: string;
  application_url: string | null;
  /** The server time zone every timed job runs on, as an IANA name (e.g. America/New_York).
   *  The effective value: the stored setting, else the env seed, else the host's own zone. */
  timezone: string;
  /** The UI accent as #rrggbb; the built-in sky blue until changed. */
  accent_color: string;
  /** The BCP 47 tag the app is shown in, and that a notification is written in. Null while
   *  nobody has chosen: this browser seeds it on first sign-in from its own languages. */
  language: string | null;
  /** Whether a key exists at all; the value only leaves through the reveal call. */
  api_key_set: boolean;
  /** Which screens the review queue opens each show's season list expanded on. */
  expand_seasons_mode: ExpandSeasonsMode;
  /** How long a plain Spare press keeps an item, in days. 0 means forever (the shipped
   *  default). A single title can still be spared for a different length from its menu. */
  default_spare_days: number;
  proxy_trust_enabled: boolean;
  trusted_proxies: string[];
  /** Present only on the Windows and macOS apps; null everywhere else, and the UI
   *  then shows no Desktop app group. */
  desktop: DesktopSettings | null;
}

export interface LogLine {
  seq: number;
  ts: string;
  level: string;
  text: string;
}

export interface LogsPage {
  lines: LogLine[];
  /** The newest sequence number the server has seen: the cursor for the next poll. */
  last_seq: number;
  /** The level Reaper is recording at right now. */
  level: string;
  /** How many rotating log files the server keeps (the live file plus its backups). Rendered
   *  in the download help so the "newest N files" copy tracks the backend, not a local guess. */
  files_kept: number;
}

export interface RequesterRow {
  /** The cross-portal person key (`plex:{id}` when linked, else `local:{portal}:{id}`):
   *  stable, always present, and unique across portals, so cards key on it, not the name. */
  identity: string;
  /** Their Plex account, or `null` for a request account nobody linked to one. Guard this
   *  before reading `played_by_them`: the server can only count plays for a linked account,
   *  so a `null` here makes that figure a structural zero rather than a measured one. */
  plex_id: number | null;
  name: string;
  requests_made: number;
  gb_granted_bytes: number;
  played_by_them: number;
  reclaimable_items: number;
  reclaimable_bytes: number;
}

/** One media type's request limit for a person. `limit` null is unlimited; the window
 *  (`days`) and unit differ per type, so movies and series each carry their own. */
export interface QuotaLine {
  limit: number | null;
  days: number | null;
  at_limit: boolean;
}

export interface PersonQuota {
  seerr_total: number;
  movie: QuotaLine;
  tv: QuotaLine;
}

/** One title a person requested that the last scan still has, for the details panel. */
export interface PersonTitle {
  title: string;
  year: number | null;
  media_type: string;
  is_4k: boolean;
  /** `null` when nothing about the title is measured; the row reads "size unknown". */
  size_bytes: number | null;
  requested_at: string | null;
  available_at: string | null;
  watched_by_them: number;
  /** `condemn` (reclaimable), `protect` (kept), or `abstain` (left to decide).
   *
   *  This already accounts for overrides, so it is the lane the review queue would
   *  file the title under, not just what the scan first said. That is what lets the
   *  row open it on the tab it lives on. Typed as the `Verdict` union rather than a
   *  bare string for the same reason `Candidate.verdict` is: a jump routes on it. */
  verdict: Verdict;
  /** Exactly one of `item_id` / `group_key` is set: a movie or lone season opens its own
   *  card, a show its group. */
  item_id: number | null;
  group_key: string | null;
  co_requesters: string[];
  /** A `/api/poster/{key}` URL, or `null` when the title has no poster key. */
  poster_url: string | null;
}

/** One requested title the last scan did not include, for the "not in the last scan"
 *  panel. Merged by title across co-requesters, and classified so the panel can say
 *  why. */
export interface UnmatchedRequest {
  /** The display name. `null` when it could not be looked up (no id, or the lookup
   *  failed). The row then shows a generic label from the type and date, never an id. */
  title: string | null;
  year: number | null;
  /** `movie` | `tv`. The row reads it as "Movie" / "Series". */
  media_type: string;
  is_4k: boolean;
  requested_at: string | null;
  available_at: string | null;
  /** `after_scan` (added since the scan ran), `set_aside` (present but not judged), or
   *  `no_id` (no id to line it up with). */
  reason: string;
  requested_by: string[];
  /** How many requests this row stands for, so the panel and the card's count agree. */
  request_count: number;
}

/** One person's full request story, behind a Scales row. */
export interface PersonDetail {
  plex_id: number | null;
  name: string;
  seerr_total: number | null;
  requests_in_scan: number;
  gb_granted_bytes: number;
  played_by_them: number;
  reclaimable_items: number;
  reclaimable_bytes: number;
  not_in_scan: number;
  quota: PersonQuota | null;
  titles: PersonTitle[];
  /** This person's not-in-scan requests, named and grouped by reason, for the panel. */
  unmatched: UnmatchedRequest[];
  /** How far back the watch history reaches. `played_by_them` and each title's
   *  `watched_by_them` are counted with no lower time bound, so a zero is a lower bound
   *  against this span; `null` is an empty mirror, where no watch figure means anything. */
  horizon_at: string | null;
  /** The requester's page on their request portal, or `null` when it cannot be built.
   *  The panel links the name to it, and shows plain text otherwise. */
  profile_url: string | null;
}

export interface FairnessReport {
  total_requests: number;
  total_reclaimable_bytes: number;
  total_reclaimable_items: number;
  /** Requests the last scan has not seen, so the numbers read as most of the requests. */
  not_in_scan: number;
  /** The not-in-scan requests themselves, named and grouped by reason, for the panel. */
  unmatched: UnmatchedRequest[];
  /** True when no scan has ever run; Scales has nothing to sit on. */
  no_snapshot: boolean;
  /** How far back the watch history reaches; older plays are invisible to this view. */
  horizon_at: string | null;
  rows: RequesterRow[];
}

export interface ScanStatus {
  running: boolean;
  phase: string;
  done: number;
  total: number;
  /** A monotonic 0-100 for the progress bar. Rises smoothly across the scan's phases,
   *  unlike done/total whose denominator changes meaning between them. */
  percent: number;
  /** The scan's current live step, composed under `shell.scanBar.step.*`
   *  (`why.ts`'s `composeIn`). `null` between phases with no sub-step of their own. */
  detail_reason: ReasonKey | null;
  /** Why the scan stopped, composed under `error.*` (`why.ts`'s `composeError`). `null`
   *  while running and on a clean finish. */
  error_reason: ReasonKey | null;
  snapshot_id: number | null;
  /** A second scan starts the moment this one finishes. Set when a scan was requested
   *  mid-run (a policy save, usually): the running scan began under the old policies,
   *  so only a scan started after the request can reflect the change. */
  followup_queued: boolean;
}

export interface AuthUser {
  id: number;
  username: string;
  provider: string;
  thumb_url: string | null;
  /** This session was opened with a recovery code, so Settings, Security accepts a new
   *  admin password without the current one. False on every ordinary sign-in. Read it
   *  only to loosen a requirement, never to grant one: the server decides, and it
   *  re-checks the session's own mark on the request. */
  via_recovery: boolean;
}

export interface AuthContext {
  setup_needed: boolean;
  plex_linked: boolean;
  local_login_available: boolean;
}

export interface PlexStart {
  pin_id: number;
  auth_url: string;
}

/** One owned server the account could link, when it owns several. */
export interface PlexServerChoice {
  name: string;
  machine_identifier: string;
}

export interface PlexPoll {
  status: "pending" | "retrying" | "ok" | "choose_server";
  user: AuthUser | null;
  setup: boolean;
  /** Present only with status "choose_server". */
  servers: PlexServerChoice[] | null;
  /** Present only with status "retrying": why this poll could not finish yet,
   *  composed through `why.ts`'s `composeIn("error", ...)` the same as any other coded
   *  refusal. The sign-in is still good, so the browser keeps polling instead of
   *  failing. */
  reason?: ReasonKey | null;
}

// --- setup + settings ------------------------------------------------------

export interface SetupStatus {
  admin_exists: boolean;
  /** Whether an admin password exists, which is also whether a local account does. The
   *  wizard reads it to know whether its first step is behind it, so the step shown comes
   *  from the server rather than from how far this browser got. */
  has_password: boolean;
  plex_linked: boolean;
  instances: Record<string, number>;
  has_radarr: boolean;
  has_sonarr: boolean;
  has_tautulli: boolean;
  has_seerr: boolean;
  has_scanned: boolean;
  scan_ready: boolean;
  /** Whether a *real* run could go ahead: scan-ready, plus a linked Plex and the password
   *  that arms deletion. A strictly higher bar than `scan_ready`, and the one `complete`
   *  does not answer. Read it through `reapBlockers` (`reapReadiness.ts`) rather than
   *  writing copy off it here, so the wizard and the Reap page say the same thing. */
  reap_ready: boolean;
  complete: boolean;
}

/** The four services Reaper connects to, mirroring `InstanceKind` in `db/models.py`.
 *
 *  Every kind reaches the DOM as a class name (`kind-radarr`, `conn-badge
 *  kind-sonarr`), and the stylesheet defines exactly these four. A widened string
 *  here does not fail loudly: it emits a class with no rule, and the badge renders
 *  unstyled, losing the fill-and-ink pair that tells the services apart. */
export type InstanceKind = "radarr" | "sonarr" | "tautulli" | "seerr";

export interface Instance {
  id: number;
  kind: InstanceKind;
  name: string;
  base_url: string;
  /** The address the UI's jump links open, or null to fall back to base_url. Display only,
   *  never connected to. Blank in the edit form clears it back to null. */
  external_url: string | null;
  enabled: boolean;
  verify_tls: boolean;
  /** When Reaper deletes through this instance, ask the *arr to add an import (list)
   *  exclusion so a list cannot re-add and re-download the title. Off by default.
   *  Wired for Radarr movie deletes; stored but inert on Sonarr, since it prunes
   *  seasons, not whole shows. */
  add_import_exclusion: boolean;
  /** HD/4K split-library map: each root folder path this instance manages, to the Plex library
   *  it lands in. When a title is in two libraries, the copy in the mapped library is bound.
   *  Empty means no mapping, so a duplicated title is kept, not matched. Sonarr/Radarr only. */
  plex_library_map: Record<string, string>;
  /** Multi-Seerr requester map: each of this portal's service ids, to the Reaper Sonarr/Radarr
   *  instance id it adds media to. Lets "requested by" name the exact copy a person asked for
   *  when a title is in more than one library. Empty means the loose union. Seerr only. */
  service_instance_map: Record<string, number>;
  has_key: boolean;
  detected_version: string | null;
  last_ok_at: string | null;
  last_error: string | null;
}

/** The verdict on a saved-instance connection test. `detail_reason` is a typed
 *  reason, the same move that gave the Discord webhook test its own typed
 *  `DiscordTest` below. A failure carries `explain_failure`'s own `error.instance.*`
 *  code; a pass carries a `services.test.*` id that `ServiceModal.tsx`'s own
 *  `testDetailText` composes. What a pre-save probe additionally reads is on
 *  `InstanceProbe`. */
export interface InstanceTest {
  ok: boolean;
  detail_reason: ReasonKey;
  version: string | null;
}

/** The pre-save test on the add form, which also reads what the connection has to map. Only
 *  this route can: it is the only caller with no instance id, so the mapping has to come back on
 *  the pass that proved the credentials, and that is what lets the form map a service before it
 *  is saved. Only one list is ever filled (a test is for exactly one kind), and both are empty
 *  on a failed test, since nothing was reached to read them from. */
export interface InstanceProbe extends InstanceTest {
  root_folders: RootFolder[];
  seerr_services: SeerrService[];
  /** Why the list above is empty, when the read failed rather than there being
   *  nothing to map. `null` means the read landed, so an empty list really is nothing
   *  to map. The catalog id plus the integration's own plain-language text as a raw
   *  `error` param, composed under `services.modal.mapError` by `ServiceModal.tsx`. */
  map_error_reason: ReasonKey | null;
}

/** The verdict on a Discord webhook test: exactly three fixed outcomes, so `reason`
 *  is typed rather than the `InstanceTest`/`InstanceProbe` shape's free-form `detail`.
 *  `DiscordModal.tsx` and `NotificationsPanel.tsx` compose
 *  `services.discord.testResult.<id>` into the same `{ok, detail, version}` shape that
 *  `TestBadge` and `testSentence` already render. */
export interface DiscordTest {
  ok: boolean;
  reason: ReasonKey;
  version: string | null;
}

/** One of an *arr instance's root folders, with a suggested Plex library to prefill the map. */
export interface RootFolder {
  path: string;
  suggested_library: string | null;
}

/** One Sonarr/Radarr service on a Seerr portal, with a suggested Reaper instance to prefill. */
export interface SeerrService {
  service_id: number;
  kind: "sonarr" | "radarr";
  name: string;
  is_4k: boolean;
  suggested_instance_id: number | null;
}

export interface PlexStatus {
  linked: boolean;
  name: string | null;
  connection_uri: string | null;
  last_ok_at: string | null;
  /** Whether the server's TLS certificate is checked. On unless the operator opted out. */
  verify_tls: boolean;
  /** Where "open in Plex" links point. Defaults to the hosted Plex Web app. */
  web_url: string;
}

export interface PlexLinkStart {
  pin_id: number;
  auth_url: string;
}

export interface PlexLinkPoll {
  status: "pending" | "retrying" | "ok" | "choose_server";
  server: PlexStatus | null;
  /** Present only with status "choose_server". */
  servers: PlexServerChoice[] | null;
  /** Present only with status "retrying": why this poll could not finish yet,
   *  composed through `why.ts`'s `composeIn("error", ...)` the same as any other coded
   *  refusal. The sign-in is still good, so the browser keeps polling instead of
   *  failing. */
  reason?: ReasonKey | null;
}

export interface ScheduledJob {
  id: string;
  /** The schedule the job runs on now, `null` when it is off. */
  cron: string | null;
  /** The built-in default cron, for reference in the editor. `null` for the scan. */
  default_cron: string | null;
  next_run_at: string | null;
  /** Whether the job is executing right this moment. */
  running: boolean;
  /** The last completion of this job: when it finished (ISO), whether it succeeded,
   *  and a typed reason. All `null` for a job that has never run. For the scan, a
   *  successful run is read from the latest snapshot instead (see ScanRow); these
   *  fields are populated for the scan only when a scheduled run crashed outright. */
  last_run_at: string | null;
  last_ok: boolean | null;
  /** Composed under `jobs.result.*` with `jobResultText` (`JobStatus.tsx`). */
  last_result_reason: ReasonKey | null;
}

export interface Schedule {
  jobs: ScheduledJob[];
}

export interface Safety {
  destructive_enabled: boolean;
  has_password: boolean;
  /** REAPER_RECOVERY is armed on the server. It holds `destructive_enabled` false however
   *  the stored switch is set, and the arm route refuses while it is on, so the banner says
   *  this rather than plain read-only: the operator is otherwise sent to a switch that
   *  cannot help them. Only a restart with the flag off clears it. */
  recovery_mode: boolean;
}

export interface Notifications {
  /** Whether a Discord webhook is stored. The URL itself is a write-only credential
   *  and is never returned, exactly like an instance API key: only its presence is
   *  reported. */
  has_webhook: boolean;
}

export interface BackupInfo {
  /** The live database size: roughly what the download weighs before compression. */
  reaper_db_bytes: number;
  /** When a backup was last downloaded (ISO 8601, UTC), or null if never. */
  last_backup_at: string | null;
  /** Whether the encryption key travels inside the backup. False when the key comes from
   *  the environment, so a restore needs that same value set on the target machine. */
  key_in_backup: boolean;
  app_version: string;
  /** Whether a confirmed restore is staged and waiting for Reaper to restart. */
  restore_armed: boolean;
}

/** What an uploaded backup turned out to be, once Reaper accepted it. Shown so the
 *  operator can confirm before restoring. */
export interface RestoreSummary {
  /** The Reaper version that wrote the backup, or null if the file did not say. */
  app_version: string | null;
  /** When the backup was taken (ISO 8601, UTC), or null. */
  created_at: string | null;
  /** "current" when it matches this server, "older" when this server will update it on
   *  restart. Both are safe to restore. */
  verdict: string;
  /** Whether the encryption key is inside the backup. False for an env-supplied key, so
   *  the target must have REAPER_SECRET_KEY set to the same value. */
  key_in_backup: boolean;
  reaper_db_bytes: number;
  /** Handed back at confirm time so the arm binds to the exact backup reviewed here. If
   *  another upload replaces the staged one before you confirm, this token stops matching
   *  and the confirm is refused. */
  token: string;
}

// ---------------------------------------------------------------------------

/** A header our own frontend sends on every request. It is the load-bearing half
 *  of the CSRF defense: a cross-origin page cannot set a custom header without a
 *  CORS preflight, which this server never grants. See reaper/api/middleware.py. */
const CSRF_HEADER = { "X-Reaper-CSRF": "1" };

/** One coded item of a 422 validation list (`api.errors.validation_error_items`'s wire
 *  shape): a field that failed a catalog-known check carries `code`/`params` beside its
 *  already-formatted English `msg`; one that failed a plain pydantic type check carries
 *  `code: null` and only `msg`. `describeError` (`errors.ts`) composes the former through
 *  the catalog and keeps the latter's `msg` as-is. */
export interface ApiErrorItem {
  code: string | null;
  params: Record<string, unknown>;
  msg: string;
}

export class ApiError extends Error {
  constructor(
    readonly status: number,
    message: string,
    /** The refusal's catalog id (`error.<area>.<name>`), or one of the three
     *  `error.transport.*` ids this client sets itself when the body carried no coded
     *  reason at all. `null` for a body this build has no code for, such as an older
     *  server or a refusal this catalog does not carry yet: `message` is still the
     *  right thing to show. Null whenever `items` is non-null, since a 422 list's own
     *  codes ride there instead, one per field, because a single top-level code cannot
     *  speak for several. */
    readonly code: string | null = null,
    /** The raw params `code` composes with (`why.ts`'s `composeIn` derives `field_label`
     *  etc. from these the same way it does for a `Reason`). Empty when `code` is null. */
    readonly params: Record<string, unknown> = {},
    /** A 422's own per-field list, each carrying its own `code`/`params` (or neither, for
     *  a plain pydantic type error) beside the English `msg` already folded into
     *  `message` above. `null` outside the 422-list shape. */
    readonly items: readonly ApiErrorItem[] | null = null,
  ) {
    super(message);
    this.name = "ApiError";
  }
}

/** What a failed response means for the ApiError to carry: the English `message`
 *  every caller has always read, plus the coded reason behind it, for `describeError`
 *  (`errors.ts`) to compose in the operator's own language.
 *
 *  `detail` is a string for HTTPException and a list of {loc, msg} for a validation
 *  failure. The domain's refusals arrive as the latter, and they carry the most
 *  useful messages in the product (for example, "a vote floor of 0 makes the rating
 *  floor meaningless"), so it would be a shame to render them as "[object Object]".
 *
 *  When there is no detail at all there is nothing of Reaper's to say, and what comes
 *  back is not Reaper's: a reverse proxy during a container restart answers with its
 *  own HTML and no `detail`. Every component renders `describeError(error)`, so a
 *  plain "Request failed (502)." would otherwise show across the review queue, the
 *  reap sheet and every settings panel. The status still goes to the console, where
 *  whoever is debugging can read it. These three fallbacks are coded too
 *  (`error.transport.*`), so a translated build reads them the same way as every
 *  other refusal. */
function parseFailure(
  status: number,
  body: unknown,
): {
  message: string;
  code: string | null;
  params: Record<string, unknown>;
  items: ApiErrorItem[] | null;
} {
  const b = body as { detail?: unknown; code?: unknown; params?: unknown } | null;
  const detail = b?.detail;

  if (typeof detail === "string") {
    const code = typeof b?.code === "string" ? b.code : null;
    const params = (b?.params as Record<string, unknown> | undefined) ?? {};
    return { message: detail, code, params, items: null };
  }

  if (Array.isArray(detail)) {
    const items: ApiErrorItem[] = detail
      .map((e) => {
        const entry = e as { msg?: unknown; code?: unknown; params?: unknown };
        return {
          code: typeof entry.code === "string" ? entry.code : null,
          params: (entry.params as Record<string, unknown> | undefined) ?? {},
          msg: typeof entry.msg === "string" ? entry.msg : "",
        };
      })
      .filter((item) => item.msg.length > 0);
    if (items.length) {
      return { message: items.map((i) => i.msg).join(" "), code: null, params: {}, items };
    }
  }

  console.warn(`Reaper: request failed with HTTP ${status} and no reason in the body.`, body);
  return status >= 500
    ? {
        message: "Reaper couldn't reach the server. Try again.",
        code: "error.transport.server_unreachable",
        params: {},
        items: null,
      }
    : {
        message: "Reaper couldn't do that. Try again.",
        code: "error.transport.request_failed",
        params: {},
        items: null,
      };
}

/** What to do when the server stops recognizing the session. Set once at startup (main.tsx).
 *
 *  Without it a dead cookie is reported one panel at a time and the app never goes back to
 *  the login screen: the operator sits on the Dashboard reading "Not authenticated." in every
 *  card with nothing to click. A restored backup carries different session rows, so the
 *  restore flow reaches this on the very next request; the 30-day session expiry reaches it
 *  eventually for everyone. */
let onUnauthorized: (() => void) | null = null;

export function setUnauthorizedHandler(handler: () => void): void {
  onUnauthorized = handler;
}

/** The session, not this request, is what failed. The gate's own probe is exempt: `/api/auth/me`
 *  answers 401 for every signed-out visitor, and that is the gate working, not a session dying.
 *  Firing there would also mean answering a refetch by asking for a refetch. */
function noteAuthFailure(status: number, path: string): void {
  if (status === 401 && !path.startsWith("/api/auth/")) onUnauthorized?.();
}

/** Read a success body, with a malformed one reported as an ApiError like every
 *  other failure.
 *
 *  Empty is fine: every endpoint returns JSON today, but the client is
 *  hand-maintained, and the day someone adds a 204 or an empty-body 200 this should
 *  resolve cleanly rather than throw "Unexpected end of JSON input". Unparseable is
 *  not fine, and is not hypothetical: a forward-auth proxy whose sign-in has expired
 *  answers 200 with an HTML login page, so `response.ok` is true and the parse throws
 *  a raw SyntaxError. That is not an ApiError, so it falls past every `instanceof`
 *  branch in the app and surfaces to the operator as parser jargon about an
 *  unexpected token. */
async function parseBody<T>(response: Response): Promise<T> {
  if (response.status === 204) return undefined as T;
  const text = await response.text();
  if (!text) return undefined as T;
  try {
    return JSON.parse(text) as T;
  } catch {
    throw new ApiError(
      response.status,
      "Reaper got an unexpected reply from the server.",
      "error.transport.bad_reply",
    );
  }
}

/** The not-ok half of every call: a failure means the same thing wherever it was made from. */
async function throwIfFailed(response: Response, path: string): Promise<void> {
  if (response.ok) return;
  const body: unknown = await response.json().catch(() => null);
  noteAuthFailure(response.status, path);
  const failure = parseFailure(response.status, body);
  throw new ApiError(response.status, failure.message, failure.code, failure.params, failure.items);
}

/** Every request the app makes goes through here: the CSRF header, the session
 *  hook, and the error mapping, in one place. It returns the raw Response for the few
 *  callers that need more than a parsed body: the two downloads want a blob, and the
 *  restore upload sends a file rather than the JSON body `request` names.
 *
 *  Anything that must hold for all traffic, such as a retry, a timeout, or a header,
 *  belongs here and nowhere else. */
async function fetchApi(path: string, init?: RequestInit): Promise<Response> {
  const response = await fetch(path, {
    ...init,
    headers: { ...CSRF_HEADER, ...init?.headers },
  });
  await throwIfFailed(response, path);
  return response;
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetchApi(path, {
    ...init,
    headers: { "Content-Type": "application/json", ...init?.headers },
  });
  return parseBody<T>(response);
}

/** Save a binary response to a file the browser downloads. The server names it in
 *  Content-Disposition; `fallbackName` covers a proxy that strips the header. */
async function download(path: string, fallbackName: string): Promise<void> {
  const response = await fetchApi(path);
  const blob = await response.blob();
  const disposition = response.headers.get("Content-Disposition") ?? "";
  const name = /filename="([^"]+)"/.exec(disposition)?.[1] ?? fallbackName;
  const url = URL.createObjectURL(blob);
  try {
    const anchor = document.createElement("a");
    anchor.href = url;
    anchor.download = name;
    document.body.appendChild(anchor);
    anchor.click();
    anchor.remove();
  } finally {
    URL.revokeObjectURL(url);
  }
}

const post = <T>(path: string, body: unknown): Promise<T> =>
  request<T>(path, { method: "POST", body: JSON.stringify(body) });

const put = <T>(path: string, body: unknown): Promise<T> =>
  request<T>(path, { method: "PUT", body: JSON.stringify(body) });

const del = <T>(path: string): Promise<T> => request<T>(path, { method: "DELETE" });

/** Where plex.tv should send the sign-in window once the operator is done with it.
 *
 *  That page closes the window, which is the only way Reaper can: the window is
 *  opened with `noopener` so plex.tv cannot reach the page holding the operator's
 *  Reaper password, and that also means `window.open` hands back nothing to close it
 *  with. A script-opened window may still close itself.
 *
 *  The browser has to name its own origin because the server cannot: Vite's dev proxy
 *  and any reverse proxy rewrite `Host`, so a URL built server-side points at an
 *  address the operator is not on. It sends the origin only, and the backend appends
 *  the path, so both Plex start routes forward to the same place without either
 *  caller restating it. */
const plexForward = () => ({ forward_origin: window.location.origin });

export const api = {
  latestSnapshot: () => request<Snapshot>("/api/snapshots/latest"),
  /** One page of the review queue. The full filtered totals (count + bytes, before the page
   *  window) ride in the envelope beside the rows, so the queue can show the whole set's
   *  count and byte total without loading them all. Paged because a library runs to
   *  thousands of protected titles. */
  candidates: async (
    // "any" is every stored lane at once. This is what makes the collection screen
    // cross-lane: a title's siblings show up whatever fate each one got.
    verdict: Verdict | "any",
    q: CandidateQuery = {},
    limit = 100,
    offset = 0,
  ): Promise<CandidatePage> => {
    const params = new URLSearchParams({ verdict });
    if (q.search) params.set("search", q.search);
    if (q.media_type) params.set("media_type", q.media_type);
    if (q.requested && q.requested !== "any") params.set("requested", q.requested);
    if (q.genre) params.set("genre", q.genre);
    if (q.collection) params.set("collection", q.collection);
    if (q.library) params.set("library", q.library);
    if (q.override && q.override !== "any") params.set("override", q.override);
    if (q.sort) params.set("sort", q.sort);
    if (q.order) params.set("order", q.order);
    params.set("limit", String(limit));
    params.set("offset", String(offset));

    const page = await request<CandidatePage>(`/api/candidates?${params.toString()}`);
    // `parseBody` reads a 200 with no body as `undefined`, which is right for calls
    // that expect nothing back and wrong here: this is the one read whose consumer
    // holds a list of pages and indexes into each, so the queue would reach
    // `undefined.items` and die with a TypeError no `instanceof ApiError` branch can
    // see. Throwing plainly here puts the same failure on the queue's own error
    // branch instead. Defaulting the body to `[]` would be worse: it would draw
    // "nothing to review" over a read that never landed.
    if (!page) {
      throw new ApiError(
        502,
        "Reaper got an unexpected reply from the server.",
        "error.transport.bad_reply",
      );
    }
    return page;
  },
  candidate: (id: number) => request<CandidateDetail>(`/api/candidates/${id}`),
  /** One show, whole: every season in the latest snapshot, across all lanes. */
  group: (key: string) => request<Group>(`/api/groups/${encodeURIComponent(key)}`),

  // --- setup + settings ---------------------------------------------------
  setupStatus: () => request<SetupStatus>("/api/setup/status"),

  instances: () => request<Instance[]>("/api/settings/instances"),
  createInstance: (body: {
    kind: string;
    name: string;
    base_url: string;
    api_key: string;
    verify_tls?: boolean;
    add_import_exclusion?: boolean;
    external_url?: string;
    /** Both maps ride along on creation, because the add form maps the service before saving
     *  it: the connection test hands back the folders (or the portal's services), so a first
     *  *arr is told apart from a second one here rather than only on a later edit. */
    plex_library_map?: Record<string, string>;
    service_instance_map?: Record<string, number>;
  }) => post<Instance>("/api/settings/instances", body),
  updateInstance: (
    id: number,
    body: {
      name?: string;
      base_url?: string;
      api_key?: string;
      enabled?: boolean;
      verify_tls?: boolean;
      add_import_exclusion?: boolean;
      // Blank clears the stored value to null; a value sets it; omitted keeps it.
      external_url?: string;
      plex_library_map?: Record<string, string>;
      service_instance_map?: Record<string, number>;
    },
  ) => put<Instance>(`/api/settings/instances/${id}`, body),
  deleteInstance: (id: number) => del<{ removed: boolean }>(`/api/settings/instances/${id}`),
  /** This instance's root folders, each with a suggested Plex library to prefill the map. */
  instanceRootFolders: (id: number) =>
    request<RootFolder[]>(`/api/settings/instances/${id}/root-folders`),
  /** This Seerr portal's Sonarr/Radarr services, each with a suggested Reaper instance. */
  instanceSeerrServices: (id: number) =>
    request<SeerrService[]>(`/api/settings/instances/${id}/seerr-services`),
  /** Test an address and key, and get back what this connection has to map. The add form's
   *  Save is gated on this passing, so a service can never be saved at an address Reaper has
   *  not reached. */
  testInstance: (body: { kind: string; base_url: string; api_key: string; verify_tls?: boolean }) =>
    post<InstanceProbe>("/api/settings/instances/test", body),
  testSavedInstance: (id: number) => post<InstanceTest>(`/api/settings/instances/${id}/test`, {}),

  plexStatus: () => request<PlexStatus>("/api/settings/plex"),
  /** Save one or both Plex settings. A PATCH: a field left out is left alone, so a
   *  control sends what it changes and nothing else. `web_url: ""` resets the address
   *  to the hosted Plex Web default; omitting the field says nothing about it. */
  setPlexSettings: (patch: { web_url?: string; verify_tls?: boolean }) =>
    put<PlexStatus>("/api/settings/plex", patch),
  plexLinkStart: () => post<PlexLinkStart>("/api/settings/plex/link/start", plexForward()),
  plexLinkPoll: (pin_id: number, machine_identifier?: string, verify_tls?: boolean) =>
    post<PlexLinkPoll>("/api/settings/plex/link/poll", {
      pin_id,
      machine_identifier,
      verify_tls,
    }),
  plexUnlink: () => del<{ removed: boolean }>("/api/settings/plex"),

  /** The servers this Plex account owns and every address each is reachable at. */
  plexResources: () => request<PlexResources>("/api/settings/plex/resources"),
  /** Point Reaper at a different server the same account owns. Probed before saving.
   *  verify_tls rides along so a self-signed target can turn the cert check off in the
   *  same step; omitted keeps the current setting. */
  plexSwitchServer: (machine_identifier: string, verify_tls?: boolean) =>
    put<PlexStatus>("/api/settings/plex/server", { machine_identifier, verify_tls }),
  /** Save how Reaper reaches the server: a discovered address or a manual one. The
   *  address is probed with the stored token first; a typo changes nothing. */
  plexSetConnection: (uri: string, verify_tls?: boolean) =>
    put<PlexStatus>("/api/settings/plex/connection", { uri, verify_tls }),

  plexLibraries: () => request<PlexLibrary[]>("/api/settings/plex/libraries"),
  syncPlexLibraries: () => post<PlexLibrary[]>("/api/settings/plex/libraries/sync", {}),
  setPlexLibraries: (enabled_keys: number[]) =>
    put<PlexLibrary[]>("/api/settings/plex/libraries", { enabled_keys }),

  watchEvidence: () => request<WatchEvidence>("/api/settings/watch-evidence"),
  /** Forgetting the record takes the admin password, like arming deletion and confirming a
   *  restore: it withdraws a protection from every title at once. */
  resetWatchEvidence: (password: string) =>
    post<{ forgotten: number }>("/api/settings/watch-evidence/reset", { password }),
  /** The narrow twin of the reset: forget what was recorded for one title, so the
   *  next scan judges it on the plays it can see today. Answers whether a record
   *  existed. Removing one deletes nothing and approves nothing. No password is
   *  needed, because the blast radius is the one title named in the path, and the
   *  gate above is priced for losing every record at once. The key is a media key,
   *  which carries colons, so it is encoded like every other path-borne key here. */
  forgetWatchEvidenceFor: (media_key: string) =>
    del<{ removed: boolean }>(`/api/settings/watch-evidence/${encodeURIComponent(media_key)}`),

  leavingSoonSettings: () => request<LeavingSoonSettings>("/api/settings/leaving-soon"),
  setLeavingSoonSettings: (body: {
    enabled?: boolean;
    allow_unarmed?: boolean;
    /** Empty resets to the shipped default. */
    name?: string;
  }) => put<LeavingSoonSettings>("/api/settings/leaving-soon", body),

  about: () => request<About>("/api/about"),
  update: () => request<Update>("/api/about/update"),

  general: () => request<GeneralSettings>("/api/settings/general"),
  saveGeneral: (body: {
    application_name?: string;
    application_url?: string;
    timezone?: string;
    accent_color?: string;
    language?: string;
    expand_seasons_mode?: ExpandSeasonsMode;
    default_spare_days?: number;
    proxy_trust_enabled?: boolean;
    trusted_proxies?: string[];
    tray?: boolean;
    dock_icon?: boolean;
  }) => put<GeneralSettings>("/api/settings/general", body),
  /** The stored key, for the Show button. Session-only; 404 when none exists yet. */
  revealApiKey: () => request<{ key: string }>("/api/settings/general/api-key"),
  /** Generate the key, replacing any previous one, which stops working immediately. */
  generateApiKey: () => post<{ key: string }>("/api/settings/general/api-key", {}),
  /** Close the header-credential lane entirely. Rotating only replaces one working key
   *  with another; this leaves none, so X-Api-Key stops being a way in. */
  removeApiKey: () => del<{ removed: boolean }>("/api/settings/general/api-key"),

  /** The log lines newer than `after`, oldest first, plus the recording level. */
  logs: (after: number) => request<LogsPage>(`/api/logs?after=${after}`),
  setLogLevel: (level: string) => put<LogsPage>("/api/logs/level", { level }),

  /** Fetch the full on-disk log and hand it to the browser as a file download.
   *  Bypasses `request` (which JSON-parses every body) to read a binary blob, and takes
   *  the filename the server offers so it carries a timestamp. */
  downloadLogs: () => download("/api/logs/download", "reaper-logs.log"),

  // Backup: what a backup would contain, and the download itself. Like the log download,
  // the file comes back as a binary blob (not JSON), and the server names it with a stamp.
  backupInfo: () => request<BackupInfo>("/api/settings/backup"),
  downloadBackup: () => download("/api/settings/backup/download", "reaper-backup.reaper"),

  /** Upload a backup file for restore. The bytes go up as the raw request body (not a
   *  multipart form), so the server streams them straight to disk. On success the file is
   *  staged, un-armed; `restoreConfirm` then verifies the password and arms the swap. */
  restorePrepare: async (file: File): Promise<RestoreSummary> => {
    // The one call that must not carry request()'s JSON Content-Type: the body is the file
    // itself, and the server streams it straight to disk.
    const response = await fetchApi("/api/settings/backup/restore/prepare", {
      method: "POST",
      body: file,
    });
    return parseBody<RestoreSummary>(response);
  },
  /** Confirm a staged restore with the admin password. Arms the swap, which happens on the
   *  next start (`restoreRestart`, or a restart the operator does themselves). The token comes
   *  from the prepare summary and binds the confirm to the exact backup that was reviewed. */
  restoreConfirm: (password: string, token: string) =>
    post<{ ok: boolean }>("/api/settings/backup/restore/confirm", { password, token }),
  /** Discard a staged or armed restore. `token` scopes the discard to one staging, so a card
   *  reclaiming what it staged cannot take one a second card staged since; `cleared` says
   *  whether it applied. Omit it to discard whatever is staged, which is what the armed
   *  card's Cancel means: it holds no summary, and no token to take from one. */
  restoreCancel: (token?: string) =>
    post<{ ok: boolean; cleared: boolean }>("/api/settings/backup/restore/cancel", { token }),
  /** Stop Reaper so the armed restore is applied on the way back up. The 200 means the stop
   *  was accepted, not that anything came back: the process goes about half a second later,
   *  and whether it returns is the container's restart policy to answer, which Reaper cannot
   *  read from inside itself. Refused (409) with no armed restore and while a reap is running. */
  restoreRestart: () => post<{ ok: boolean }>("/api/settings/backup/restore/restart", {}),

  schedule: () => request<Schedule>("/api/settings/schedule"),
  saveJobSchedule: (id: string, cron: string | null) =>
    put<Schedule>(`/api/settings/jobs/${id}/schedule`, { cron }),
  runJob: (id: string) => post<{ status: string; job: string }>(`/api/settings/jobs/${id}/run`, {}),

  safety: () => request<Safety>("/api/settings/safety"),
  setDeletion: (enabled: boolean, password?: string) =>
    put<Safety>("/api/settings/safety", { enabled, password: password ?? null }),
  setAdminPassword: (password: string, currentPassword?: string) =>
    post<{ ok: boolean }>("/api/settings/admin-password", {
      password,
      current_password: currentPassword ?? null,
    }),

  // The Discord webhook is the one channel that actually warns your users before a title
  // is deleted. Like an API key it is write-only: `has_webhook` says only whether one is set.
  notifications: () => request<Notifications>("/api/settings/notifications"),
  setWebhook: (webhook_url: string) =>
    put<Notifications>("/api/settings/notifications", { webhook_url }),
  clearWebhook: () => del<Notifications>("/api/settings/notifications"),
  /** Post a sample embed. Pass the URL about to be saved to test it, or null to test the
   *  already-stored webhook without re-pasting the secret. */
  testWebhook: (webhook_url: string | null) =>
    post<DiscordTest>("/api/settings/notifications/test", { webhook_url }),

  policy: (mediaType: "movie" | "tv" = "movie") =>
    request<Policy>(`/api/policy?media_type=${mediaType}`),
  vocabulary: (lane: "protect" | "condemn", mediaType?: "movie" | "tv") =>
    request<Vocabulary>(
      `/api/vocabulary?lane=${lane}${mediaType ? `&media_type=${mediaType}` : ""}`,
    ),
  /** Seen values for one field's input suggestions. Empty when nothing to suggest. */
  vocabularyValues: (field: string) =>
    request<FieldValues>(`/api/vocabulary/values?field=${encodeURIComponent(field)}`),
  savePolicy: (body: PolicyBody) => post<Policy>("/api/policy", body),
  /** Check a policy draft. `maxUnmeasured` is the unknown-size allowance as it stands in the
   *  editor: its warning renders beneath the box that sets it, so the check has to run against
   *  the drafted value, not the saved profile. Omit it and the server uses what is stored. */
  validatePolicy: (body: PolicyBody, maxUnmeasured?: number | null) =>
    post<Policy>("/api/policy/validate", {
      ...body,
      ...(maxUnmeasured === null || maxUnmeasured === undefined
        ? {}
        : { draft_max_unmeasured_per_run: maxUnmeasured }),
    }),
  simulate: (body: PolicyBody) => post<Simulation>("/api/policy/simulate", body),
  /** Try one policy rule against one value and let the engine answer.
   *
   *  A round trip for a number a slider could compute locally, deliberately: the local copy
   *  would be a second scorer beside the control that tunes deletions, free to drift from
   *  the one that actually decides. Stateless and snapshot-free, so it is cheap enough to
   *  sit under a drag. */
  probePolicy: (probe: PolicyProbe) => post<PolicyProbeResult>("/api/policy/probe", probe),
  /** The season-count distribution from the latest snapshot, for the keep-last advisory.
   *  Independent of the current keep-last value, so it needs no re-scan. */
  seasonShape: () => request<SeasonShape>("/api/snapshot/season-shape"),
  /** The latest scan's fitted rewatch ladder, for the rewatch card's ladder and
   *  consequence echo. Aggregated server-side from the stored explanation blocks,
   *  never refit here, so the page states exactly what the gate will actually
   *  compare. Movies and TV seasons carry their own fit, so the ladder never mixes
   *  the two lanes. */
  rewatchOddsFit: (mediaType: "movie" | "tv") =>
    request<RewatchOddsFit>(`/api/policy/rewatch-odds?media_type=${mediaType}`),
  /** The whole score-to-consequence curve behind the delete-threshold slider, from the
   *  latest scan and this server's own fitted rewatch curve. One request per media type:
   *  the editor re-decides every row locally as the slider moves, never a call per drag. */
  thresholdCurve: (mediaType: "movie" | "tv") =>
    request<ThresholdCurve>(`/api/policy/threshold-curve?media_type=${mediaType}`),

  startScan: () => post<ScanStatus>("/api/scan/start", {}),
  scanStatus: () => request<ScanStatus>("/api/scan/status"),

  /** The recent plans, newest first, with `total` (of whatever `executedOnly` matches) for
   *  the history view's "Showing N of M" and its scroll paging. `executedOnly`
   *  drops a plan that was built and never executed (the head Reap button, a standalone
   *  practice run): the Reap page's history, the one caller here, reads it true. */
  runs: (offset = 0, limit = 50, executedOnly = false) =>
    request<RunList>(`/api/runs?offset=${offset}&limit=${limit}&executed_only=${executedOnly}`),
  run: (id: number) => request<Run>(`/api/runs/${id}`),
  /** A window of one run's journal, past the page the detail route carries. No component
   *  reads this yet: the step table still draws the first page and says how many it is not
   *  showing. It ships with the route so the whole plan stays reachable, which is what the
   *  table's own paging will read when it is built. */
  runSteps: (id: number, offset = 0, limit = 50) =>
    request<RunSteps>(`/api/runs/${id}/steps?offset=${offset}&limit=${limit}`),
  /** A window of one run's per-item outcomes, reconstructed from the journal. Answers a
   *  run still executing exactly as it answers one long finished, which is what lets the
   *  Reap page's live item-status feed, its done card, and the run detail sheet all read
   *  the same source. */
  runOutcomes: (id: number, offset = 0, limit = 50) =>
    request<RunOutcomes>(`/api/runs/${id}/outcomes?offset=${offset}&limit=${limit}`),
  /** Build a plan, over an explicitly named set. `"all"` covers the whole condemned
   *  set; an array reaps just those items, the safe path for a first, hand-picked
   *  deletion.
   *
   *  "All" has to be spelled out, never implied: the route reads an omitted
   *  `media_keys` as the whole condemned set, so a selection that filtered down to
   *  nothing must not be able to fall through into it. An empty array throws here
   *  rather than widening the request. */
  createRun: (target: "all" | string[]) => {
    if (target !== "all" && target.length === 0) {
      throw new Error("Nothing is selected, so there is nothing to reap.");
    }
    return post<Run>("/api/runs", target === "all" ? {} : { media_keys: target });
  },
  dryRun: (id: number) => post<RunReport>(`/api/runs/${id}/dry-run`, {}),
  /** Start a real reap. Requires deletion armed on the host and the exact
   *  content-bound confirmation phrase, which the server recomputes and refuses
   *  anything else. The reap then runs detached. This returns the initial status, and
   *  the report lands on the status (poll `reapStatus`) when the run ends. */
  executeRun: (id: number, confirmationPhrase: string) =>
    post<ReapStatus>(`/api/runs/${id}/execute`, { confirmation_phrase: confirmationPhrase }),
  /** The running (or last) reap's progress. Polled while a reap runs, and read once on load
   *  to re-attach to one already in flight from any screen. */
  reapStatus: () => request<ReapStatus>("/api/runs/execute/status"),
  /** Stop the running reap, gracefully: it halts after the item in flight and still tidies
   *  Plex. Leaves deletion armed. Reachable from any screen. */
  stopRun: (id: number) => post<ReapStatus>(`/api/runs/${id}/stop`, {}),

  profile: () => request<ProfileSettings>("/api/profile"),
  saveProfile: (s: ProfileSettings) =>
    request<ProfileSettings>("/api/profile", { method: "PUT", body: JSON.stringify(s) }),

  fairness: () => request<FairnessReport>("/api/fairness"),
  /** One requester's full breakdown for the Scales panel: everything they asked for that
   *  the last scan still has, plus their request limits. Keyed on the cross-portal identity
   *  (which carries a `:`, so it is encoded into the path). */
  person: (identity: string) =>
    request<PersonDetail>(`/api/fairness/people/${encodeURIComponent(identity)}`),
  reapBreakdown: () => request<ReapBreakdown>("/api/reap/breakdown"),
  plexTrash: () => request<PlexTrash>("/api/reap/plex-trash"),
  /** What each list is currently protecting. Membership, from the cache. */
  lists: () => request<ProtectionList[]>("/api/lists"),
  /** What each list is. Definitions, from `reaper.db`. Joined to the above on `list_id`. */
  listConfigs: () => request<ListConfig[]>("/api/lists/configured"),
  addList: (name: string, source: ListConfig["source"], config: ListConfigBody) =>
    post<ListConfig>("/api/lists/configured", { name, source, config }),
  /** An edit. Both fields are optional, and omitting one means "leave it". */
  editList: (id: number, body: { name?: string; config?: ListConfigBody }) =>
    request<ListConfig>(`/api/lists/configured/${id}`, {
      method: "PATCH",
      body: JSON.stringify(body),
    }),
  removeList: (id: number) => del<void>(`/api/lists/configured/${id}`),
  /** Check lists now: one definition, or, with none named, all of them. The same
   *  pass a scan runs. Slow by nature, since it reads every *arr and Plex once. */
  syncLists: (target: { list_id?: number } = {}) => post<ListSyncResult>("/api/lists/sync", target),
  syncLeavingSoon: () => post<LeavingSoonResult>("/api/leaving-soon/sync", {}),

  // The keep list has one pair of methods, `override` / `clearOverride` below, and one
  // pair of routes behind them: one way to write this safety-adjacent row. Neither is
  // reachable by an API key: `_API_KEY_WRITES` admits only scanning, planning, the
  // policy and the profile.
  /** Override a verdict by hand: spare (keep) or reap (force onto the list). A
   *  show's media_key covers all its seasons. `spareDays` is how long a spare keeps
   *  it: 0 means forever, a positive count that many days. Ignored for a reap. */
  override: (media_key: string, decision: Override, note?: string, spareDays = 0) =>
    post<WhitelistEntry>("/api/override", {
      media_key,
      decision,
      note: note ?? null,
      spare_days: spareDays,
    }),
  /** Clear any override (spare or reap). This does not delete anything: the item is
   *  judged by the policy again on the next scan. `includeSeasons` widens a show key's
   *  clear to its season-level rows: only the bulk bar sends it, because a selected show
   *  card shows every season's hand mark; level-scoped controls clear one key. */
  clearOverride: (media_key: string, includeSeasons = false) =>
    request<{ removed: boolean }>(
      `/api/override/${encodeURIComponent(media_key)}${includeSeasons ? "?include_seasons=true" : ""}`,
      { method: "DELETE" },
    ),

  // --- auth ---------------------------------------------------------------
  me: () => request<AuthUser>("/api/auth/me"),
  authContext: () => request<AuthContext>("/api/auth/context"),
  plexStart: () => post<PlexStart>("/api/auth/plex/start", plexForward()),
  plexPoll: (pin_id: number, machine_identifier?: string) =>
    post<PlexPoll>("/api/auth/plex/poll", { pin_id, machine_identifier }),
  localLogin: (username: string, password: string) =>
    post<AuthUser>("/api/auth/local", { username, password }),
  recover: (token: string) => post<AuthUser>("/api/auth/recover", { token }),
  logout: () => post<{ ok: boolean }>("/api/auth/logout", {}),
};
