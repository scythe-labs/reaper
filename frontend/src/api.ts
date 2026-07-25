// SPDX-License-Identifier: AGPL-3.0-or-later
//
// The wire types, mirrored from reaper/api/schemas.py.
//
// Hand-written rather than generated. The set is small, it changes rarely, and a
// codegen step is one more thing that can silently drift out of date in CI. If these
// grow much past this size, generate them from /openapi.json instead.

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
   *  red) or can't yet, for a safety stop or an unchecked protection (false paints it dashed
   *  red with a scythe, "kept for now"). Null when there is no reap override. */
  override_effective: boolean | null;
  /** The season's size on disk, so the card can state whole-show totals without a
   *  second fetch. Null when nothing would report one, which is not zero. */
  size_bytes: number | null;
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
}

export interface GateOutcome {
  gate: string;
  detail: string;
}

/** How the item was tied to its Plex library entry. The panel stays quiet on "matched" and
 *  shows a plain "kept to be safe" notice on the other two -- the only cases where the owner
 *  needs to know the file was kept because Reaper couldn't be sure what it was looking at. */
export interface Match {
  status: "matched" | "unmatched" | "ambiguous" | null;
  /** For the audit log, not shown to the owner: "Bound by TMDB id 1001", etc. */
  detail: string | null;
  rating_key: number | null;
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
  secondary: number;
  window_days: number;
}

export interface SignalSetting {
  signal: string;
  weight: number;
  saturate_at: number;
  floor: number;
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
  | { kind: "graded"; name: string; field: string; weight: number; saturate_at: number; floor: number };

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
  | "imdb"
  | "tmdb"
  | "rotten_tomatoes_critic"
  | "rotten_tomatoes_audience"
  | "metacritic";

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

export interface ReapBreakdown {
  /** False before the first scan, when every figure is zero. */
  has_snapshot: boolean;
  policy_condemned: number;
  policy_condemned_bytes: number;
  policy_condemned_unknown: number;
  hand_spared: number;
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
  seasons: number;
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
  /** Whether the review queue opens each show with its season list already expanded. */
  expand_seasons_default: boolean;
  /** How long a plain Spare press keeps an item, in days. 0 means forever (the shipped
   *  default). A single title can still be spared for a different length from its menu. */
  default_spare_days: number;
  proxy_trust_enabled: boolean;
  trusted_proxies: string[];
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

/** A reclaimable title on a requester's row: what it is, the disk it holds, and how to open
 *  it. Every entry is condemned by the last scan, so the verdict is implicit. Exactly one of
 *  `item_id` / `group_key` is set: a movie or season opens its own card, a show its group. */
export interface ReclaimableTitle {
  title: string;
  /** `null` when nothing about the title is measured; the chip then reads "size unknown". */
  size_bytes: number | null;
  item_id: number | null;
  group_key: string | null;
}

export interface RequesterRow {
  /** The cross-portal person key (`plex:{id}` when linked, else `local:{portal}:{id}`):
   *  stable, always present, and unique across portals, so cards key on it, not the name. */
  identity: string;
  name: string;
  requests_made: number;
  gb_granted_bytes: number;
  played_by_them: number;
  reclaimable_items: number;
  reclaimable_bytes: number;
  /** The heaviest reclaimable titles behind `reclaimable_items` (capped at 25 server-side);
   *  `reclaimable_items` stays the exact count. */
  reclaimable: ReclaimableTitle[];
  /** Lifetime requests across every portal this person has an account on; `null` when the
   *  Seerr user list could not be read. Display only. */
  seerr_total: number | null;
  /** At their movie / series request limit on any portal right now. Independent: the two
   *  types have their own windows and units. */
  movie_at_limit: boolean;
  tv_at_limit: boolean;
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
  /** `condemn` (reclaimable), `protect` (kept), or `abstain` (left to decide). */
  verdict: string;
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
  plex_linked: boolean;
  instances: Record<string, number>;
  has_radarr: boolean;
  has_sonarr: boolean;
  has_tautulli: boolean;
  has_seerr: boolean;
  has_scanned: boolean;
  scan_ready: boolean;
  complete: boolean;
}

export type InstanceKind = "radarr" | "sonarr" | "tautulli" | "seerr";

export interface Instance {
  id: number;
  kind: string;
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
  api_path_prefix: string;
  detected_version: string | null;
  last_ok_at: string | null;
  last_error: string | null;
}

export interface InstanceTest {
  ok: boolean;
  detail: string;
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
  /** Whether a confirmed restore is staged and waiting for a container restart. */
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
 *  so it would be a shame to render them as "[object Object]". */
function reason(status: number, body: unknown): string {
  const detail = (body as { detail?: unknown } | null)?.detail;

  if (typeof detail === "string") return detail;

  if (Array.isArray(detail)) {
    const messages = detail
      .map((e) => (e as { msg?: string }).msg)
      .filter((m): m is string => Boolean(m));
    if (messages.length) return messages.join(" ");
  }

  return `Request failed (${status}).`;
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, {
    ...init,
    headers: { "Content-Type": "application/json", ...CSRF_HEADER, ...init?.headers },
  });

  if (!response.ok) {
    const body: unknown = await response.json().catch(() => null);
    throw new ApiError(response.status, reason(response.status, body));
  }
  // Tolerate empty bodies the way the error branch above does. Every endpoint returns JSON
  // today, but the client is hand-maintained: the day someone adds a 204 No Content or an
  // empty-body 200, `response.json()` would throw a raw "Unexpected end of JSON input"
  // SyntaxError instead of resolving cleanly. Parse only when there is something to parse.
  if (response.status === 204) return undefined as T;
  const text = await response.text();
  return (text ? JSON.parse(text) : undefined) as T;
}

const post = <T>(path: string, body: unknown): Promise<T> =>
  request<T>(path, { method: "POST", body: JSON.stringify(body) });

const put = <T>(path: string, body: unknown): Promise<T> =>
  request<T>(path, { method: "PUT", body: JSON.stringify(body) });

const del = <T>(path: string): Promise<T> => request<T>(path, { method: "DELETE" });

export const api = {
  latestSnapshot: () => request<Snapshot>("/api/snapshots/latest"),
  /** One page of the review queue. The full filtered totals (count + bytes, before the page
   *  window) ride along in response headers, so the queue can show "[redacted] items · [redacted]"
   *  without loading them all. Paged because a library runs to thousands of protected titles. */
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

    const response = await fetch(`/api/candidates?${params.toString()}`, {
      headers: { "Content-Type": "application/json", ...CSRF_HEADER },
    });
    if (!response.ok) {
      const body: unknown = await response.json().catch(() => null);
      throw new ApiError(response.status, reason(response.status, body));
    }
    const items = (await response.json()) as Candidate[];
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
  testInstance: (body: { kind: string; base_url: string; api_key: string; verify_tls?: boolean }) =>
    post<InstanceTest>("/api/settings/instances/test", body),
  testSavedInstance: (id: number) =>
    post<InstanceTest>(`/api/settings/instances/${id}/test`, {}),

  plexStatus: () => request<PlexStatus>("/api/settings/plex"),
  /**
   * Save the Plex settings. An empty web_url resets to the hosted default; verify_tls
   * (only valid once linked) flips the certificate check, omitted keeps it.
   */
  setPlexWebUrl: (web_url: string, verify_tls?: boolean) =>
    put<PlexStatus>("/api/settings/plex", { web_url, verify_tls }),
  plexLinkStart: () => post<PlexLinkStart>("/api/settings/plex/link/start", {}),
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

  leavingSoonSettings: () => request<LeavingSoonSettings>("/api/settings/leaving-soon"),
  setLeavingSoonSettings: (body: { enabled?: boolean; allow_unarmed?: boolean }) =>
    put<LeavingSoonSettings>("/api/settings/leaving-soon", body),

  about: () => request<About>("/api/about"),

  general: () => request<GeneralSettings>("/api/settings/general"),
  saveGeneral: (body: {
    application_name?: string;
    application_url?: string;
    timezone?: string;
    accent_color?: string;
    expand_seasons_default?: boolean;
    default_spare_days?: number;
    proxy_trust_enabled?: boolean;
    trusted_proxies?: string[];
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
  downloadLogs: async (): Promise<void> => {
    const response = await fetch("/api/logs/download", { headers: { ...CSRF_HEADER } });
    if (!response.ok) {
      const errorBody: unknown = await response.json().catch(() => null);
      throw new ApiError(response.status, reason(response.status, errorBody));
    }
    const blob = await response.blob();
    const disposition = response.headers.get("Content-Disposition") ?? "";
    const name = /filename="([^"]+)"/.exec(disposition)?.[1] ?? "reaper-logs.log";
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
  },

  // Backup: what a backup would contain, and the download itself. Like the log download,
  // the file comes back as a binary blob (not JSON), and the server names it with a stamp.
  backupInfo: () => request<BackupInfo>("/api/settings/backup"),
  downloadBackup: async (): Promise<void> => {
    const response = await fetch("/api/settings/backup/download", { headers: { ...CSRF_HEADER } });
    if (!response.ok) {
      const errorBody: unknown = await response.json().catch(() => null);
      throw new ApiError(response.status, reason(response.status, errorBody));
    }
    const blob = await response.blob();
    const disposition = response.headers.get("Content-Disposition") ?? "";
    const name = /filename="([^"]+)"/.exec(disposition)?.[1] ?? "reaper-backup.reaper";
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
  },

  /** Upload a backup file for restore. The bytes go up as the raw request body (not a
   *  multipart form), so the server streams them straight to disk. On success the file is
   *  staged, un-armed; `restoreConfirm` then verifies the password and arms the swap. */
  restorePrepare: async (file: File): Promise<RestoreSummary> => {
    const response = await fetch("/api/settings/backup/restore/prepare", {
      method: "POST",
      headers: { ...CSRF_HEADER },
      body: file,
    });
    if (!response.ok) {
      const body: unknown = await response.json().catch(() => null);
      throw new ApiError(response.status, reason(response.status, body));
    }
    return (await response.json()) as RestoreSummary;
  },
  /** Confirm a staged restore with the admin password. Arms the swap; the operator then
   *  restarts the container to finish. The token comes from the prepare summary and binds
   *  the confirm to the exact backup that was reviewed. */
  restoreConfirm: (password: string, token: string) =>
    post<{ ok: boolean }>("/api/settings/backup/restore/confirm", { password, token }),
  /** Discard a staged or armed restore. */
  restoreCancel: () => post<{ ok: boolean }>("/api/settings/backup/restore/cancel", {}),

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

  // The Discord webhook is the one channel that actually warns the household before a title
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
  validatePolicy: (body: PolicyBody) => post<Policy>("/api/policy/validate", body),
  simulate: (body: PolicyBody) => post<Simulation>("/api/policy/simulate", body),
  /** The season-count distribution from the latest snapshot, for the keep-last advisory.
   *  Independent of the current keep-last value, so it needs no re-scan. */
  seasonShape: () => request<SeasonShape>("/api/snapshot/season-shape"),

  startScan: () => post<ScanStatus>("/api/scan/start", {}),
  scanStatus: () => request<ScanStatus>("/api/scan/status"),

  runs: () => request<Run[]>("/api/runs"),
  run: (id: number) => request<Run>(`/api/runs/${id}`),
  /** Build a plan. With no keys it covers the whole condemned set; with `mediaKeys` it
   *  reaps just those items -- the safe path for a first, hand-picked deletion. */
  createRun: (mediaKeys?: string[]) =>
    post<Run>("/api/runs", mediaKeys && mediaKeys.length ? { media_keys: mediaKeys } : {}),
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
  syncLeavingSoon: () => post<LeavingSoonResult>("/api/leaving-soon/sync", {}),

  whitelist: () => request<WhitelistEntry[]>("/api/whitelist"),
  spare: (media_key: string, note?: string, spareDays = 0) =>
    post<WhitelistEntry>("/api/whitelist", { media_key, note: note ?? null, spare_days: spareDays }),
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
  unspare: (media_key: string) =>
    request<{ removed: boolean }>(`/api/whitelist/${encodeURIComponent(media_key)}`, {
      method: "DELETE",
    }),

  // --- auth ---------------------------------------------------------------
  me: () => request<AuthUser>("/api/auth/me"),
  authContext: () => request<AuthContext>("/api/auth/context"),
  plexStart: () => post<PlexStart>("/api/auth/plex/start", {}),
  plexPoll: (pin_id: number, machine_identifier?: string) =>
    post<PlexPoll>("/api/auth/plex/poll", { pin_id, machine_identifier }),
  localLogin: (username: string, password: string) =>
    post<AuthUser>("/api/auth/local", { username, password }),
  recover: (token: string) => post<AuthUser>("/api/auth/recover", { token }),
  logout: () => post<{ ok: boolean }>("/api/auth/logout", {}),
};
