// SPDX-License-Identifier: AGPL-3.0-or-later
//
// The wire types, mirrored from the response models. Not one file: they live across
// reaper/api/ (schemas.py holds most, but the one route that deletes answers with
// runs.py's ReapStatus) plus engine/policy.py and engine/explanation.py.
//
// Hand-written rather than generated. The set is small, it changes rarely, and a
// codegen step is one more thing that can silently drift out of date in CI. If these
// grow much past this size, generate them from /openapi.json instead.
//
// tests/test_api_type_mirror.py checks this file against those declarations and fails
// naming the field, because hand-maintained meant nothing noticed when MatchOut gained
// `by` and `merged_rating_keys` and this file did not (#260, #289). It compares field
// NAMES only: the type and optionality differences here are deliberate and documented
// there. A new type with no server model is classified in that file, not ignored.

export type Verdict = "condemn" | "protect" | "abstain";

export interface Snapshot {
  id: number;
  created_at: string;
  policy_hash: string;
  horizon_at: string;
  item_count: number;
  degraded: boolean;
  degraded_reason: string | null;
  condemned: number;
  protected: number;
  abstained: number;
  reclaimable_bytes: number;
  /** How many condemned items have no size, and so sit outside the total above rather
   *  than inside it as zeros. Zero for a healthy library, and hidden at zero. */
  unknown_size_items: number;
}

/** The one short status chip a card wears, display-ready from the server. `kept`
 *  renders green (a protection fired), `quiet` gray (nothing to act on), `look`
 *  amber-outlined (deliberately left for the owner to decide). */
export interface Chip {
  tone: "kept" | "quiet" | "look";
  text: string;
  /** The same fact as `text`, worded as a lowercase clause that can follow "Reap
   *  requested, kept for now:" -- or null when this chip names no reason a reap would
   *  be refused. Read it through `chipWhy`; never parse `text` to recover it. */
  why?: string | null;
}

/** One square of a show card's season strip: the lightest per-season mark, across
 *  every lane of the whole snapshot. `season` is null for a row whose key carried
 *  no season number -- shown unnumbered, never dropped. */
export interface GroupSeasonMark {
  /** The candidate id for this season, so clicking its square opens that season's own
   *  reasoning rather than the whole show's panel. */
  id: number;
  season: number | null;
  verdict: Verdict;
  override: Override | null;
  /** For a "reap" override: whether the engine honors it (true paints the square solid
   *  red) or can't yet, for a safety stop or a row Reaper can't identify (false paints it
   *  dashed red with a scythe, "kept for now"). Null when there is no reap override. */
  override_effective: boolean | null;
  /** The season's size on disk, so the card can state whole-show totals without a
   *  second fetch. Null when nothing would report one, which is not zero. */
  size_bytes: number | null;
  /** For a "spare" override: when it stops keeping this season (ISO), or null for a forever
   *  spare. The square colors by the item's fate (rule 49), and a spare whose clock has passed
   *  is a fate of its own -- still keeping the file until a scan realizes the expiry, so still
   *  green, but dashed rather than solid because it is no longer a live decision. */
  spare_expires_at: string | null;
  /** When the LAST spare covering this season stops keeping it, or null for a forever one.
   *  What the square's COLOR reads; `spare_expires_at` above is the spare a control toggles.
   *  See `Candidate.spare_covers_until`. */
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
  /** How many seasons "Reap now" on this show would plan: its condemned, not-spared
   *  seasons across the whole snapshot, not just the fetched pages. Null for movies. */
  group_condemned_count: number | null;
  /** The byte total over that same set: the number the planner will act on. */
  group_condemned_bytes: number | null;
  /** How many of the show's actable seasons have no size. They are left out of both
   *  numbers above, because the planner will not plan them. Null for movies. */
  group_unknown_size: number | null;
  /** Canonical file resolution ("2160", "1080", ..., "sd") for the card's quality badge.
   *  Null hides the badge (TV seasons, unmatched items, rows from older scans). */
  video_resolution: string | null;
  /** The Plex library (section) this item lives in, as the operator named it -- the show's
   *  for a season. Drives the card/panel library chip and the library filter. Null when
   *  unknown (unmatched, or a row from before this shipped); the chip is then hidden. */
  library: string | null;
  /** How long the item has sat unwatched ("5 years, 9 months"), for the amber pill.
   *  Null hides the pill. */
  dormant_for: string | null;
  /** The one-line "why", drawn from the explanation: the protection keeping a spared item,
   *  or the strongest reason a reaped one scored. What the card shows instead of a synopsis. */
  reason: string | null;
  spared: boolean;
  /** The manual decision *in effect* -- "spare", "reap", or null -- own or inherited from
   *  the show. It colors the row's chip and score (the item's real fate). Set the moment they
   *  click, so the card shows the pending intent before the next scan bakes it in. To decide
   *  what a control can toggle, read `override_own`. */
  override: Override | null;
  /** This item's OWN decision, ignoring one it inherits from its show -- what a Spare/Reap
   *  control on this row toggles. Equals `override` for a movie. Null for a season kept only
   *  because the whole show is spared; `show_override` then says why it is still kept. */
  override_own: Override | null;
  /** The whole-show decision covering this season (its show's "spare"/"reap"), or null.
   *  Drives the "kept because the whole show is spared" note beside a season's control. Always
   *  null for a movie. */
  show_override: Override | null;
  /** For a "reap" override: whether the engine honors it -- it joins the counts, the
   *  grace countdown and the next plan -- or refuses it (someone is watching right now,
   *  or the file isn't managed). Null when there is no reap override. Red only on true. */
  override_effective: boolean | null;
  /** When the spare *in effect* on this item stops keeping it (ISO-8601). Read only when
   *  `override` is "spare": null then means "kept for good", a value drives the "N days left"
   *  countdown. A season with no spare of its own carries the show spare's expiry. */
  spare_expires_at: string | null;
  /** When the LAST spare covering this item stops keeping it, own or show, whichever runs
   *  longer. Null for a forever one. The fate question, where `spare_expires_at` above is the
   *  precedence question: that one names the spare a control toggles and clears (rule 50), this
   *  one names when the file stops being kept, which is what a color or a sentence about its
   *  fate must read (rules 49/61). They differ whenever both levels spare an item and the
   *  higher-precedence spare runs out first: a season spared 10 days inside a show spared
   *  forever is kept forever. A show set to REAP contributes no cover, so a season spare
   *  lapsing under one still reads expired. Read only when `override` is "spare". */
  spare_covers_until: string | null;
  /** When the whole-show spare covering this season stops keeping it (ISO-8601). Read only
   *  when `show_override` is "spare"; null means a forever show-spare. Always null for a movie. */
  show_spare_expires_at: string | null;
  /** The card's one status chip (Sanctuary and Limbo). Null on condemned rows, whose
   *  card leads with the amber dormancy pill instead. */
  chip: Chip | null;
  /** Which season this row is, for season rows. Null for movies and unparseable keys. */
  season_number: number | null;
  /** The whole show's per-season verdict marks, for the card's season strip. Null for
   *  movies. */
  group_seasons: GroupSeasonMark[] | null;
  /** Whether the show has finished. Null for a movie, where the question doesn't apply,
   *  and on a row stored before this field existed -- both render nothing at all. */
  show_status: ShowStatus | null;
}

/** Whether a show has finished, as three states rather than a bool, so "the server never
 *  said" can never be drawn as a definite answer. "continuing" is labeled "Still going"
 *  on screen: that arm also covers a show that hasn't started airing yet. */
export type ShowStatus = "ended" | "continuing" | "unknown";

export type Override = "spare" | "reap";

/** One show, whole: the show-level header plus every season row in the latest
 *  snapshot regardless of verdict -- what the show panel and the expanded card read. */
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
   *  owner's attention, else the highest-scoring one. */
  reason: string | null;
  /** The show's Plex library (section), shared by all its seasons. Null when unknown.
   *  Drives the show panel's library chip. */
  library: string | null;
  chip: Chip | null;
  /** The show's own decision (the show key's "spare"/"reap"), or null -- what the panel's
   *  whole-show control toggles and what lights it. Never an aggregate of the seasons' own
   *  decisions, which that control cannot clear. Null until the whole show is decided. */
  show_override: Override | null;
  /** When the whole-show spare stops keeping the show (ISO-8601), or null for a forever
   *  spare. Read only when `show_override` is "spare" -- the panel's whole-show countdown. */
  show_spare_expires_at: string | null;
  links: Links;
  /** Whether the show has finished, taken from whichever season rows carry it -- one
   *  reading of the series is stamped onto every season in the same scan, so they cannot
   *  disagree. Null only when no row carries it (a snapshot from before this field). */
  show_status: ShowStatus | null;
  /** Every season, sorted by season number (unnumbered rows last). */
  seasons: Candidate[];
}

/** One page of candidates, plus the full-set totals the server measured before the page
 *  window -- what the queue header counts and sizes. */
export interface CandidatePage {
  items: Candidate[];
  total: number;
  totalBytes: number;
  /** How many across the whole filtered set have no size. `totalBytes` is the sum of
   *  what is known; this is what it could not include. */
  unknownSize: number;
  offset: number;
  /** The snapshot this page was drawn from, or null before any scan. The queue compares it
   *  against the newest completed scan to notice when a fresher snapshot has landed under it. */
  snapshotId: number | null;
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

export interface SignalContribution {
  id: string;
  contribution: number;
  weight: number;
  detail: string;
  /** False means the input was Unknown. Its weight still counts in the denominator, so
   *  an unevaluated signal drags the score DOWN, never up. */
  evaluated: boolean;
  /** Optional: rows scored before this field existed carry none. Read a missing one as
   *  `not_applicable`, never `argues_keep` -- claiming an old row argued for keeping,
   *  when nothing recorded whether it did, overstates the case for keeping. */
  state?: SignalState | null;
  /** The ramp this row was scored against: no points below `floor`, all of them at
   *  `saturate_at`. Frozen at scan time, because the live policy need not be the one this
   *  score was computed under and a panel explaining a score with the wrong line is worse
   *  than one that stays quiet.
   *
   *  `null` in two cases the panel deliberately does not tell apart: a rule with no ramp (a
   *  yes/no rule of your own either matched or did not), and a row frozen before these
   *  shipped. Both mean "no line to state", and both render the plain row. */
  floor?: number | null;
  saturate_at?: number | null;
}

export interface GateOutcome {
  gate: string;
  detail: string;
  /** Whether the comparison behind a hold is one Reaper actually made. Only the season
   *  keep-rule guard sets it, where a conflict can also mean "a count nobody could read"
   *  or "a watch history too short to stand behind the counts" -- shapes that must never
   *  be described as a comparison. A row scanned before the flag shipped carries neither
   *  answer and must assert neither (rule 142).
   *
   *  **`null` is that row, and it is what actually arrives.** The stored explanation has no
   *  key, but the response always does: `GateOutcomeOut` defaults the field to `None` and
   *  nothing sets `exclude_none`, so the server serializes it as `null`. The `?` is defense
   *  against a shape the server does not emit -- never the case to branch on, and never the
   *  case to test against. Both read as "names neither shape", never as `false`. */
  defers_to_owner?: boolean | null;
}

/** How the item was tied to its Plex library entry. The panel stays quiet on "matched" and
 *  shows a plain "kept to be safe" notice on the other two -- the only cases where the owner
 *  needs to know the file was kept because Reaper couldn't be sure what it was looking at. */
export interface Match {
  /** `ambiguous` and `conflicted` are NOT interchangeable, and the panel must not treat
   *  them as one: ambiguous is several Plex rows answering to this item (a library really
   *  holding more than one copy), conflicted is each kind of evidence naming a DIFFERENT
   *  single row (Plex and the *arr describing one file differently, over a library that may
   *  hold exactly one copy). Saying the first when it is the second sends the owner hunting
   *  for a duplicate that is not there. */
  status: "matched" | "unmatched" | "ambiguous" | "conflicted" | null;
  /** Which kind of evidence bound this item, e.g. `tmdb`. Audit vocabulary, so it is declared
   *  here and deliberately not rendered: rule 21 keeps id kinds off the panel. It is typed so
   *  the next reader of this file finds it instead of re-discovering the drift (#260).
   *  Optional like `candidate_rating_keys` below, which the server also always sends: these
   *  are the fields no component reads, so fixtures are not made to carry them. */
  by?: string | null;
  /** For the audit log, not shown to the owner: "Bound by TMDB id 1001", etc. */
  detail: string | null;
  rating_key: number | null;
  /** Every Plex listing a merged bind covers, when one file is listed several times. Null on
   *  an ordinary single-listing bind, and on a record stored before this shipped. Load-bearing
   *  on the deletion path -- the executor re-reads this list, so the listings are protected
   *  together -- which is why the panel says the count out loud. */
  merged_rating_keys?: number[] | null;
  /** The Plex rows an abstain was choosing between, on `ambiguous` and `conflicted`. The
   *  panel renders `links.match_candidates` rather than these numbers; they are here so a
   *  reader can tell how many there were without following the links. */
  candidate_rating_keys?: number[] | null;
}

/** A graded keep's contribution to the score -- points subtracted, and whether it could be
 *  evaluated (false means Unknown, which takes the FULL discount -- fail-closed toward keeping). */
export interface KeepContribution {
  name: string;
  discount: number;
  max_discount: number;
  detail: string;
  evaluated: boolean;
}

export interface Explanation {
  score: number;
  /** The condemnation subtotal before any keep discount. Optional so an item scored before
   *  this shipped still parses. */
  base_score?: number;
  keep_discount?: number;
  /** The score the item had to beat. Null only when the stored explanation could not be
   *  read and the server sent the degraded fallback: the panel omits its "your threshold
   *  is N" clause rather than print a number that is not the operator's setting. */
  threshold: number | null;
  coverage: number;
  /** Whether this title is held because the plays Reaper recorded earlier stopped being
   *  readable. The panel offers the per-title escape on it, and on nothing else: the reason
   *  text beside it is operator copy and will be reworded (rule 92).
   *
   *  Three-state (rule 142), and only `true` may show the control. `false` is the positive
   *  claim that the scan took a reading and it was honest. **`null` is "cannot tell"** -- a
   *  row scanned before the key existed, or an item with no reading to judge -- and it is what
   *  actually arrives for such a row: `Explanation` defaults the field to `None` and nothing
   *  sets `exclude_none`, so the server serializes it as `null`. The `?` is defense against a
   *  shape the server does not emit. Both read as "cannot tell", never as `false`, because
   *  offering to discard a watch record on a guess is the wrong direction. */
  watch_blind?: boolean | null;
  signals: SignalContribution[];
  keeps?: KeepContribution[];
  /** Why it is being kept. */
  protections_fired: GateOutcome[];
  /** Protections evaluated that did NOT fire -- with the actual numbers. */
  protections_checked: GateOutcome[];
  /** Protections that could not be checked. "We could not look" is not "we looked and
   *  it was fine", and rendering them alike is the entire Deleterr failure class. */
  protections_unknown: GateOutcome[];
  /** How it was tied to Plex. Optional so a candidate scored before this shipped still
   *  parses (its explanation JSON has no match block). */
  match?: Match;
}

/** Where the item can be opened. Each link is null when it can't be built (unmatched in
 *  Plex, instance removed, a row from an older scan); the panel hides a missing link,
 *  never renders a broken one. At most one of radarr/sonarr is set. The rating-site
 *  links back the chips in the ratings row; rotten_tomatoes is a title search. */
/** One Plex row an abstain could not choose between, with the ways to open it. Reaper
 *  knows nothing about these rows but their keys, so the panel numbers them rather than
 *  naming them. */
export interface CandidateLink {
  rating_key: number;
  plex: string | null;
  tautulli: string | null;
}

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
   *  `tautulli` above are built from the item's OWN rating key, which is null for exactly
   *  these items, so without this the panel names a problem in Plex and offers no way to
   *  open it. */
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
  /** True when `explanation` is the server's degraded fallback rather than what the scan
   *  stored. The panel says so, instead of rendering empty reason blocks that would read
   *  as "nothing protected this" when the truth is that nothing could be read. */
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
 *  `kind` is a discriminator, not decoration: a second probe -- what a keep rule would
 *  discount, what a graded rule of your own would add -- joins this union and every client
 *  already sending `kind` keeps working. Inferring the shape from which fields turned up is
 *  what rule 142 exists to stop, and it is far cheaper to type before the format ships. */
export interface SignalProbe {
  kind: "signal";
  signal: string;
  weight: number;
  saturate_at: number;
  floor: number;
  /** In the units the signal stores: days, watchers, a season's rank, a rating in tenths,
   *  or bytes. */
  value: number;
  window_days?: number;
}

/** What `POST /api/policy/probe` accepts. One member today; see `SignalProbe`. */
export type PolicyProbe = SignalProbe;

/** One answer, the same shape for every probe kind, so a new kind needs no new rendering. */
export interface PolicyProbeResult {
  /** What the rule moves the score by, in its own direction. */
  points: number;
  /** The engine's own words for it, the sentence the panel's row would carry. */
  detail: string;
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

/** A user-authored graded "lean toward keeping" -- a subtractive discount, fail-closed. */
export interface GradedKeep {
  name: string;
  field: string;
  max_discount: number;
  floor: number;
  saturate_at: number;
  direction: "high_keeps" | "low_keeps";
}

/** Which rating source a keep bar reads. Movies can back every source (Radarr carries
 *  them); TV backs IMDb plus whatever Plex serves for the show. */
export type RatingSource =
  "imdb" | "tmdb" | "rotten_tomatoes_critic" | "rotten_tomatoes_audience" | "metacritic";

/** One "keep it if it clears this bar" rule. `floor` is in tenths (7.5 -> 75), and reads
 *  the same for a percentage source (75% -> 75). `min_votes` only bites on sources that
 *  count votes (IMDb, TMDb); it is 0 for the percentage sources. */
export interface RatingRule {
  source: RatingSource;
  floor: number;
  min_votes: number;
}

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
  keep_tags: string[];
  keep_tags_match: "any" | "all";
  keep_rating_rules: RatingRule[];
  keep_rating_match: "any" | "all";
}

/** The distribution of content-season counts across shows in the latest snapshot, so the
 *  editor can show live how many shows a keep-last-N value fully protects. */
export interface SeasonShape {
  total_shows: number;
  season_counts: Record<number, number>;
}

/** One field the owner may write a protect condition about (from the vocabulary endpoint). */
export interface VocabField {
  key: string;
  label: string;
  help_text: string;
  type: string;
  unit_suffix: string;
  ops: string[];
}
export interface Vocabulary {
  lane: string;
  fields: VocabField[];
}

export interface PolicyWarning {
  field: string;
  message: string;
  severity: string;
}

export interface Policy {
  policy_hash: string;
  name: string;
  /** The shipped bounds for this media type's signals, so a changed one can be put back.
   *
   *  Making the ramp editable made it losable: nothing said what 1825 had been, and the
   *  presets restore weights only. Sent rather than copied into this file, so the number the
   *  scorer reads is declared once.
   *
   *  Weights ride along and the editor ignores them: removal weights must total exactly 100,
   *  so restoring one on its own would break the budget the save bar enforces. */
  default_signals?: SignalSetting[];
  /** How far back your watch history goes, for the editor to say beside the controls it
   *  bounds. A never-played title is measured from the later of its arrival and this edge,
   *  so this IS the largest dormancy anything can present: a ramp whose far end sits past
   *  it can never pay in full. `null` when the scan did not record it, which the editor
   *  renders as not knowing rather than as no history at all. */
  history_reach_days?: number | null;
  body: PolicyBody;
  warnings: PolicyWarning[];
  /** This body was repaired on the way out and is NOT what is stored. Set when a policy
   *  written before removal weights had to total 100 was rescaled to fit. The editor
   *  opens on it dirty so the operator reviews and saves their own tuning in the new
   *  units; nothing is written, and approvals stay valid, until they do. */
  needs_save?: boolean;
  /** The stored body could not be repaired, so this is the shipped default: numbers the
   *  operator never chose. Louder than needs_save. */
  fell_back?: boolean;
  /** The rating bar was restored from an older saved setting, so this body is NOT what is
   *  stored. Its own flag rather than needs_save: that one moved points into new units,
   *  this one put back a protection that had stopped keeping anything. */
  rating_rules_restored?: boolean;
}

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

export interface Simulation {
  /** Whether these numbers actually answer the question that was asked. False when the
   *  candidate policy changed a weight or a gate, in which case the stored scores were
   *  produced by a different policy and every count below is zeroed. */
  exact: boolean;
  stale_reason: string | null;
  condemned: number;
  protected: number;
  abstained: number;
  reclaimable_bytes: number;
  /** How many of the condemned have no size, left out of the total above. Hidden at zero. */
  unknown_size_items: number;
  newly_condemned: number;
  no_longer_condemned: number;
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
  /** Why this step failed or was skipped, as the executor recorded it. Null on a step that
   *  has not run or that succeeded. Already operator copy: the executor writes one sentence
   *  and uses it both here and in the after-action report, and the report is the half that
   *  does not survive a restart. */
  error: string | null;
}

export interface Run {
  id: number;
  snapshot_id: number;
  policy_hash: string;
  state: string;
  item_count: number;
  total_bytes: number;
  confirmation_phrase: string;
  /** How many condemned items this plan left out because nothing would report their
   *  size. The plan is smaller than the queue implied, and this is what says so. */
  held_back_unknown_size: number;
  approved_manifest_hash: string;
  approved_by: string;
  approved_at: string;
  steps: ActionStep[];
}

/** One line of the run history: the stored row, and nothing derived. A past plan's counts,
 *  totals and phrase are all re-derived from today's overrides, which costs a whole snapshot's
 *  candidates per run and, for a finished run, describes a plan that never existed -- so the
 *  list carries none of them and opening a row fetches the full `Run` (P-3). */
export interface RunSummary {
  id: number;
  snapshot_id: number;
  state: string;
  approved_by: string;
  approved_at: string;
  aborted_reason: string | null;
  held_back_unknown_size: number;
}

export interface RunCheck {
  label: string;
  ok: boolean;
}

export interface RunOutcome {
  media_key: string;
  title: string;
  kind: string;
  state: string; // verified | failed | skipped
  detail: string;
  checks: RunCheck[];
}

export interface RunReport {
  run_id: number;
  dry_run: boolean;
  state: string;
  aborted_reason: string | null;
  would_delete_items: number;
  deleted_bytes: number;
  /** How many deleted items had no size, so are absent from `deleted_bytes`. Above zero
   *  only when the operator allowed unmeasured items. Hidden at zero. */
  deleted_unmeasured: number;
  skipped: number;
  outcomes: RunOutcome[];
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
  error: string | null;
  /** The after-action report, present once the run has ended (null while running). */
  report: RunReport | null;
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
  /** Read-only (GET). True when the stored settings couldn't be read and these are the
   *  shipped defaults, which can be looser than what was saved. The Pace page shows a
   *  recovery notice; a scan degrades until the operator saves again. Absent/ignored on save. */
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
  bytes: number;
  unknown_size: number;
}

/** What Plex would remove besides the files a reap deletes.
 *
 *  Reaper's end-of-run purge empties a library's WHOLE trash, not just the part this run
 *  caused, so anything already in there loses its watch history, ratings and collections
 *  too. Those items sit on both sides of the executor's before/after count, so its gate
 *  cannot see them. No file on disk is affected either way.
 */
export interface PlexTrash {
  /** False when no Plex server is linked, in which case nothing purges. */
  configured: boolean;
  /** Items in the trash across the libraries included in scans. A FLOOR: it counts only
   *  the libraries that answered, so read it with `sections_unreadable`. */
  trashed: number;
  /** Libraries whose trash could not be counted. Nonzero means `trashed` is incomplete,
   *  and the page warns rather than reading silence as zero. */
  sections_unreadable: number;
  /** Plex's own server-wide "empty trash after every scan" preference, which ships ON.
   *  When true Plex purges the trash itself, outside Reaper's interlock. Null if unread. */
  empties_after_scan: boolean | null;
}

export interface ReapBreakdown {
  /** False before the first scan, when every figure is zero. */
  has_snapshot: boolean;
  policy_condemned: number;
  policy_condemned_bytes: number;
  policy_condemned_unknown: number;
  hand_spared: number;
  /** The share of `hand_spared` a scan would hand back to policy: TITLES kept out of the plan
   *  by a spare whose clock has already passed. They are still being kept -- only a scan
   *  realizes a spare's expiry -- so they are absent from the plan with nothing else on the
   *  page to explain it. One notice when nonzero.
   *
   *  Titles, not spares: one whole-show spare can be holding several condemned seasons. A
   *  title another spare still covers is not counted, since a scan would not release it. */
  spares_expired: number;
  hand_reaped: number;
  hand_reaped_bytes: number;
  hand_reaped_unknown: number;
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
  added_count: number;
  cleared_count: number;
  /** Whether the shelf writes landed everywhere. False in read-only preview, and false
   *  when any library failed. */
  applied: boolean;
  notified: boolean;
  movies_on_shelves: number;
  seasons_on_shelves: number;
  /** Per-library failures, in plain words. */
  problems: string[];
}

export interface WatchEvidence {
  /** How many titles Reaper holds a watch record for. */
  titles: number;
  /** How many items the LAST scan found had plays it could no longer read.
   *  `null` when no scan has recorded it, either because none has run or because the newest
   *  one predates the count. Render that as "not recorded", never as zero: a scan that did
   *  not count is not a scan that counted none.
   *
   *  **Do not render this as items held back or kept.** It counts what was measured, not what
   *  was decided, and the hold it usually causes comes from three gates the operator can each
   *  switch off. "Held back" is also this app's phrase for an item with no readable size. See
   *  `watchEvidenceStatus` in `PlexPanel.tsx`, which is the one place this number is worded. */
  held_back: number | null;
}

export interface LeavingSoonSettings {
  enabled: boolean;
  allow_unarmed: boolean;
  last: {
    at: string;
    movies: number;
    seasons: number;
    applied: boolean;
    /** Whether the last sync was actually clean: false only for a real per-library
     *  problem, never merely because it ran in preview (unarmed). */
    ok: boolean;
    /** A short plain-language summary of the last sync. */
    result: string;
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
 *  three-state -- `null` when the check is off, unreachable, or the versions cannot be
 *  ordered -- and the surfaces render that as nothing: the check informs, never gates.
 *  `changes` lists every release newer than the running one, newest first; empty
 *  unless `update_available` is true, and always empty on the dev channel. */
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
 *  `app_settings.ExpandSeasonsMode`; "both" is what the on/off switch this replaced meant
 *  when it was on. */
export type ExpandSeasonsMode = "off" | "desktop" | "both" | "mobile";

/** The desktop build's own knobs, present only when Reaper runs as the Mac or Windows
 *  app. Backed by launcher.conf in the data folder, which the launcher reads at start,
 *  so every change applies the next time Reaper opens. */
export interface DesktopSettings {
  platform: "macos" | "windows";
  /** The menu-bar (macOS) / tray (Windows) icon with Open Reaper and Quit. */
  tray: boolean;
  /** macOS only: show the Dock icon beside the menu-bar icon. Rides along false on
   *  Windows and the row never renders there. */
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
   *  Already override-aware (rule 77), so this is the lane the review queue would file the
   *  title under, not just what the scan first said -- which is what lets the row open it on
   *  the tab it lives on. Typed as the `Verdict` union rather than a bare string for the same
   *  reason `Candidate.verdict` is: a jump routes on it. */
  verdict: Verdict;
  /** Exactly one of `item_id` / `group_key` is set: a movie or lone season opens its own
   *  card, a show its group. */
  item_id: number | null;
  group_key: string | null;
  co_requesters: string[];
  /** A `/api/poster/{key}` URL, or `null` when the title has no poster key. */
  poster_url: string | null;
}

/** One requested title the last scan didn't include, for the "not in the last scan" panel.
 *  Merged by title across co-requesters, and classified so the panel can say why. */
export interface UnmatchedRequest {
  /** The display name. `null` when it couldn't be looked up (no id, or the lookup failed);
   *  the row then shows a generic label from the type and date, never an id. */
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
  /** The requester's page on their request portal, or `null` when it can't be built. The
   *  panel links the name to it, and shows plain text otherwise. */
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

export interface Progress {
  phase: string;
  done: number;
  total: number;
  detail: string;
}

export interface ScanStatus {
  running: boolean;
  phase: string;
  done: number;
  total: number;
  /** A monotonic 0-100 for the progress bar. Rises smoothly across the scan's phases,
   *  unlike done/total whose denominator changes meaning between them. */
  percent: number;
  detail: string;
  error: string | null;
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
  email: string | null;
  thumb_url: string | null;
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
  /** Present only with status "retrying": why this poll couldn't finish yet. The sign-in
   *  is still good, so the browser keeps polling instead of failing. */
  reason?: string | null;
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
 *  Every kind reaches the DOM as a class name (`kind-radarr`, `conn-badge kind-sonarr`) and the
 *  stylesheet defines exactly these four, so a widened string here does not fail loudly -- it
 *  emits a class with no rule and the badge renders unstyled, losing the fill-and-ink pair that
 *  tells the services apart. */
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
   *  exclusion so a list can't re-add and re-download the title. Off by default. Wired for
   *  Radarr movie deletes; stored-but-inert on Sonarr (it prunes seasons, not whole shows). */
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

export interface InstanceTest {
  ok: boolean;
  detail: string;
  version: string | null;
  /** What this connection has to map, read on the pass that proved the credentials. Only ONE
   *  route ever fills these -- the pre-save test on the add form, which is the only caller with
   *  no instance id to ask a second question with. This is what lets that form map a service
   *  before it is saved. Only one list is ever filled (a test is for exactly one kind), and
   *  both are empty on a failed test, since nothing was reached to read them from. */
  root_folders: RootFolder[];
  seerr_services: SeerrService[];
  /** Why the list above is empty, when the read FAILED rather than there being nothing to map.
   *  `null` means the read landed, so an empty list really is nothing to map. */
  map_error: string | null;
}

/** What the test BADGE reads: the verdict, and nothing about mapping.
 *
 *  Declared as the subset rather than reusing `InstanceTest` because the service card renders a
 *  result it rebuilt from what it remembers of the last test (`last_ok_at`, `last_error`), and
 *  no folder read ever happened for that one. Handing the badge a whole `InstanceTest` would
 *  have made that card fill in a `map_error: null`, which reads as "we looked and nothing was
 *  wrong" about a read nobody ran (rule 93). The badge only ever needed these three. */
export type TestVerdict = Pick<InstanceTest, "ok" | "detail" | "version">;

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
  /** Present only with status "retrying": why this poll couldn't finish yet. The sign-in
   *  is still good, so the browser keeps polling instead of failing. */
  reason?: string | null;
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
  /** The last completion of this job: when it finished (ISO), whether it succeeded, and a
   *  short plain-language result. All `null` for a job that has never run. For the scan, a
   *  SUCCESSFUL run is read from the latest snapshot instead (see ScanRow); these fields are
   *  populated for the scan only when a scheduled run crashed outright. */
  last_run_at: string | null;
  last_ok: boolean | null;
  last_result: string | null;
}

export interface Schedule {
  jobs: ScheduledJob[];
}

export interface Safety {
  destructive_enabled: boolean;
  has_password: boolean;
  note: string | null;
}

export interface Notifications {
  /** Whether a Discord webhook is stored. The URL itself is a write-only credential and is
   *  never returned -- exactly like an instance API key, only its presence is reported. */
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
  /** The Reaper version that wrote the backup, or null if the file didn't say. */
  app_version: string | null;
  /** When the backup was taken (ISO 8601, UTC), or null. */
  created_at: string | null;
  /** The schema revision the backup sits at. */
  revision: string | null;
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

export class ApiError extends Error {
  constructor(
    readonly status: number,
    message: string,
  ) {
    super(message);
    this.name = "ApiError";
  }
}

/** Pull a human-readable reason out of a FastAPI error body.
 *
 *  `detail` is a string for HTTPException and a list of {loc, msg} for a validation
 *  failure. The domain's refusals arrive as the latter, and they are the most useful
 *  messages in the product ("a vote floor of 0 makes the rating floor meaningless") --
 *  so it would be a shame to render them as "[object Object]".
 *
 *  When there is no detail at all there is nothing of Reaper's to say, and what comes back
 *  is not Reaper's: a reverse proxy during a container restart answers with its own HTML
 *  and no `detail`. Every component renders `error.message` verbatim, so the old fallback
 *  put a bare "Request failed (502)." across the review queue, the reap sheet and every
 *  settings panel (U-14, rule 21). The status still goes to the console, where whoever is
 *  debugging can read it. */
function reason(status: number, body: unknown): string {
  const detail = (body as { detail?: unknown } | null)?.detail;

  if (typeof detail === "string") return detail;

  if (Array.isArray(detail)) {
    const messages = detail
      .map((e) => (e as { msg?: string }).msg)
      .filter((m): m is string => Boolean(m));
    if (messages.length) return messages.join(" ");
  }

  console.warn(`Reaper: request failed with HTTP ${status} and no reason in the body.`, body);
  return status >= 500
    ? "Reaper couldn't reach the server. Try again."
    : "Reaper couldn't do that. Try again.";
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

/** Read a success body, with a malformed one reported as an ApiError like every other failure.
 *
 *  Empty is fine: every endpoint returns JSON today, but the client is hand-maintained, and the
 *  day someone adds a 204 or an empty-body 200 this should resolve cleanly rather than throw
 *  "Unexpected end of JSON input". Unparseable is NOT fine, and is not hypothetical: a
 *  forward-auth proxy whose sign-in has expired answers 200 with an HTML login page, so
 *  `response.ok` is true and the parse throws a raw SyntaxError -- which is not an ApiError, so
 *  it falls past every `instanceof` branch in the app and surfaces to the operator as parser
 *  jargon about an unexpected token. */
async function parseBody<T>(response: Response): Promise<T> {
  if (response.status === 204) return undefined as T;
  const text = await response.text();
  if (!text) return undefined as T;
  try {
    return JSON.parse(text) as T;
  } catch {
    throw new ApiError(response.status, "Reaper got an unexpected reply from the server.");
  }
}

/** The not-ok half of every call: a failure means the same thing wherever it was made from. */
async function throwIfFailed(response: Response, path: string): Promise<void> {
  if (response.ok) return;
  const body: unknown = await response.json().catch(() => null);
  noteAuthFailure(response.status, path);
  throw new ApiError(response.status, reason(response.status, body));
}

/** EVERY request the app makes goes through here: the CSRF header, the session hook, and the
 *  error mapping, in one place, returning the raw Response for the few callers that need more
 *  than a parsed body (the paged queue reads its totals off the headers; the two downloads
 *  want a blob).
 *
 *  Four call sites used to hand-roll this `fetch` -- so the wrapper only looked like a choke
 *  point, and a cross-cutting change landed on three quarters of the surface. They had already
 *  drifted (R-3). Anything that must hold for all traffic -- a retry, a timeout, a header --
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
 *  That page closes the window, which is the only way Reaper can: the window is opened
 *  with `noopener` so plex.tv cannot reach the page holding the operator's Reaper
 *  password, and that also means `window.open` hands back nothing to close it with. A
 *  script-opened window may still close itself (#372).
 *
 *  The browser has to name its own origin because the server cannot: Vite's dev proxy and
 *  any reverse proxy rewrite `Host`, so a URL built server-side points at an address the
 *  operator is not on. It sends the origin only and the backend appends the path, so both
 *  Plex start routes forward to the same place without either caller restating it. */
const plexForward = () => ({ forward_origin: window.location.origin });

export const api = {
  latestSnapshot: () => request<Snapshot>("/api/snapshots/latest"),
  /** One page of the review queue. The full filtered totals (count + bytes, before the page
   *  window) ride along in response headers, so the queue can show the whole set's count and
   *  byte total without loading them all. Paged because a library runs to thousands of
   *  protected titles. */
  candidates: async (
    verdict: Verdict,
    q: CandidateQuery = {},
    limit = 100,
    offset = 0,
  ): Promise<CandidatePage> => {
    const params = new URLSearchParams({ verdict });
    if (q.search) params.set("search", q.search);
    if (q.media_type) params.set("media_type", q.media_type);
    if (q.requested && q.requested !== "any") params.set("requested", q.requested);
    if (q.genre) params.set("genre", q.genre);
    if (q.library) params.set("library", q.library);
    if (q.override && q.override !== "any") params.set("override", q.override);
    if (q.sort) params.set("sort", q.sort);
    if (q.order) params.set("order", q.order);
    params.set("limit", String(limit));
    params.set("offset", String(offset));

    const response = await fetchApi(`/api/candidates?${params.toString()}`, {
      headers: { "Content-Type": "application/json" },
    });
    const items = (await parseBody<Candidate[]>(response)) ?? [];
    const snapshotHeader = response.headers.get("X-Snapshot-Id");
    return {
      items,
      total: Number(response.headers.get("X-Total-Count") ?? items.length),
      totalBytes: Number(response.headers.get("X-Total-Bytes") ?? 0),
      unknownSize: Number(response.headers.get("X-Unknown-Size-Count") ?? 0),
      offset,
      snapshotId: snapshotHeader ? Number(snapshotHeader) : null,
    };
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
    post<InstanceTest>("/api/settings/instances/test", body),
  testSavedInstance: (id: number) => post<InstanceTest>(`/api/settings/instances/${id}/test`, {}),

  plexStatus: () => request<PlexStatus>("/api/settings/plex"),
  /** Save one or both Plex settings. A PATCH: a field left out is left alone, so a control
   *  sends what it changes and nothing else.
   *
   *  It took `(web_url, verify_tls)` and sent both fields always, which meant the
   *  certificate switch had to supply an address -- and the only one it had was the
   *  browser's cached status row, so it wrote that back over whatever was stored (#204).
   *  `web_url: ""` still resets the address to the hosted Plex Web default; omitting it is
   *  now how a caller says nothing about it. */
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
  /** The narrow twin of the reset: forget what was recorded for ONE title, so the next scan
   *  judges it on the plays it can see today. Answers whether a record existed. Removing one
   *  deletes nothing and approves nothing. No password, because the blast radius is the one
   *  title named in the path -- the gate above is priced on losing every record at once. The
   *  key is a media key, which carries colons, so it is encoded like every other path-borne
   *  key here. */
  forgetWatchEvidenceFor: (media_key: string) =>
    del<{ removed: boolean }>(`/api/settings/watch-evidence/${encodeURIComponent(media_key)}`),

  leavingSoonSettings: () => request<LeavingSoonSettings>("/api/settings/leaving-soon"),
  setLeavingSoonSettings: (body: { enabled?: boolean; allow_unarmed?: boolean }) =>
    put<LeavingSoonSettings>("/api/settings/leaving-soon", body),

  about: () => request<About>("/api/about"),
  update: () => request<Update>("/api/about/update"),

  general: () => request<GeneralSettings>("/api/settings/general"),
  saveGeneral: (body: {
    application_name?: string;
    application_url?: string;
    timezone?: string;
    accent_color?: string;
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
   *  already-stored webhook without re-pasting the secret. Reuses the connection-test shape. */
  testWebhook: (webhook_url: string | null) =>
    post<InstanceTest>("/api/settings/notifications/test", { webhook_url }),

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

  startScan: () => post<ScanStatus>("/api/scan/start", {}),
  scanStatus: () => request<ScanStatus>("/api/scan/status"),

  runs: () => request<RunSummary[]>("/api/runs"),
  run: (id: number) => request<Run>(`/api/runs/${id}`),
  /** Build a plan, over an explicitly named set. `"all"` covers the whole condemned set; an
   *  array reaps just those items -- the safe path for a first, hand-picked deletion.
   *
   *  "All" has to be spelled, never implied: the route reads an omitted `media_keys` as the
   *  whole condemned set, so a selection that filtered down to nothing must not be able to
   *  fall through into it. An empty array throws here rather than widening the request. */
  createRun: (target: "all" | string[]) => {
    if (target !== "all" && target.length === 0) {
      throw new Error("Nothing is selected, so there is nothing to reap.");
    }
    return post<Run>("/api/runs", target === "all" ? {} : { media_keys: target });
  },
  dryRun: (id: number) => post<RunReport>(`/api/runs/${id}/dry-run`, {}),
  /** Start a real reap. Requires deletion armed on the host and the exact content-bound
   *  confirmation phrase -- the server recomputes and refuses anything else. The reap then
   *  runs detached; this returns the initial status, and the report lands on the status
   *  (poll `reapStatus`) when the run ends. */
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
  syncLeavingSoon: () => post<LeavingSoonResult>("/api/leaving-soon/sync", {}),

  // The keep list has one pair of methods in the UI, `override` / `clearOverride` below.
  // `whitelist`, `spare` and `unspare` used to sit here too, uncalled by anything: three
  // more ways to write the same safety-adjacent row, with nothing to tell a reader which
  // one the app actually used (rule 38, R-6). Their routes are still served, but only the
  // READ is reachable by an API key: `_API_KEY_WRITES` admits scanning, planning, the
  // policy and the profile, so a key meets a 403 on both writes. This line offered the
  // lane all three, and #326 was filed against the refusal that mistake implied.
  /** Override a verdict by hand -- spare (keep) or reap (force onto the list). A show's
   *  media_key covers all its seasons. `spareDays` is how long a spare keeps it: 0 = forever,
   *  a positive count that many days; ignored for a reap. */
  override: (media_key: string, decision: Override, note?: string, spareDays = 0) =>
    post<WhitelistEntry>("/api/override", {
      media_key,
      decision,
      note: note ?? null,
      spare_days: spareDays,
    }),
  /** Clear any override (spare or reap). Does not delete anything -- the item is judged by
   *  the policy again on the next scan. */
  clearOverride: (media_key: string) =>
    request<{ removed: boolean }>(`/api/override/${encodeURIComponent(media_key)}`, {
      method: "DELETE",
    }),

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
