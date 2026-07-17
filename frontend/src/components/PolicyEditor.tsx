// SPDX-License-Identifier: AGPL-3.0-or-later
//
// The policy workspace: everything that shapes Reaper's decision, on one page, in the
// order Reaper decides it -- what flags a title, what is always kept, how fast a reap
// may go, and whether deletion is allowed at all. The live simulator sits beside it.
//
// The design principle: **the knob and its blast radius sit in the same viewport.**
// Move the threshold, and the count, the byte total and the histogram move with it --
// instantly, with zero API calls, because the last snapshot's scores are re-decided in
// the database rather than the library being re-read.
//
// The single most important behaviour in this file is what happens when that stops
// being true. The simulator can only honestly re-decide a *stored* score, so it is
// exact for the threshold and the coverage floor, and **wrong for everything else**:
// change a signal weight or a protection, and the stored scores were produced by the
// old ones. The server detects this and refuses to answer. This component must then
// refuse to *show* anything -- because a stale count would look exactly as
// authoritative as a live one, and the owner would act on it.
//
// A dangerous number that looks trustworthy is worse than no number at all.
//
// Three things save from this page, and they stay separate on purpose:
//   1. the policy itself (hashed; editing it re-arms any pending approval),
//   2. pace and limits (un-hashed; tightening a cap never voids an approval),
//   3. the deletion switch (its own password-gated call).

import { keepPreviousData, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useMemo, useRef, useState } from "react";
import {
  api,
  ApiError,
  type Condition,
  type CustomCondemn,
  type GateSetting,
  type GradedKeep,
  type Policy,
  type PolicyBody,
  type ProfileSettings,
  type RatingRule,
  type RatingSource,
  type SignalSetting,
  type Simulation,
  type VocabField,
} from "../api";
import { bytes, count } from "../format";
import { DeletionToggle } from "./DeletionToggle";
import { QuantityInput, SIZE_UNITS, TIME_UNITS } from "./QuantityInput";
import { Switch } from "./Switch";

// Plain-English identities for every protection and signal, so the editor reads like a
// person wrote it instead of exposing the engine's field names. `unit` picks the control:
// a duration gets a value+unit picker, a rating a 0–10 box, a count a plain number.
type GateMeta = { label: string; help: string; unit?: "days" | "people" };

const GATE_META: Record<string, GateMeta> = {
  min_dormancy: {
    label: "Give every title time to be rewatched",
    help: "Nothing is removed until it has gone at least this long without a single play. Under about three years, people still circle back to a title surprisingly often.",
    unit: "days",
  },
  server_popularity: {
    label: "Keep what your users actually watch",
    help: "If at least this many different people have played it recently, it stays, whatever it scored.",
    unit: "people",
  },
  rating_floor: {
    label: "Keep well-rated titles",
    help: "A title well rated on any source you trust is kept.",
  },
  others_watching: {
    label: "Protect what other people watch",
    help: "If someone other than the requester has played it, keep it. Removing it would punish them for a request that wasn't theirs.",
    unit: "people",
  },
  streaming_now: {
    label: "Never touch something playing right now",
    help: "Re-checked in the seconds before any removal, not just at scan time.",
  },
  whitelisted: {
    label: "Spare titles you've tagged",
    help: "A title carrying one of these tags in Sonarr/Radarr is kept, whatever it scores. (A ‘Never Reap’ Plex collection is honoured too.)",
  },
  curated_list: {
    label: "Honour protected lists",
    help: "The IMDb Top 250, and any other list you mark as protected.",
  },
  data_horizon: {
    label: "Don't judge what predates your history",
    help: "Tautulli can't see plays from before it was installed, so anything older than your history is left alone rather than assumed unwatched.",
  },
  unmanaged: {
    label: "Only touch what Sonarr or Radarr manages",
    help: "If no *arr owns the file, Reaper has no safe way to remove it.",
  },
};

const SIGNAL_META: Record<string, { label: string; help: string }> = {
  unwatched: {
    label: "How long it's gone unwatched",
    help: "The longer since anyone played it, the stronger the reason to remove it. The biggest single signal.",
  },
  few_watchers: {
    label: "How few people watch it",
    help: "Fewer recent watchers means more pressure to remove it.",
  },
  season_rank: {
    label: "How old a season is",
    help: "Older seasons of a show carry more pressure than the newest one. The season floor below still wins.",
  },
  low_rating: {
    label: "How low it's rated",
    help: "A poorly-rated title carries a little more pressure.",
  },
  size: {
    label: "How big it is on disk",
    help: "Off by default. Big files are usually big because they're popular, so size makes a poor reason to delete. It only ranks titles the score has already chosen.",
  },
};

function titleCase(id: string): string {
  return id.replace(/_/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());
}

// ---------------------------------------------------------------------------
// Presets: three starting points that stage (never save) the threshold and the
// pace. Weights are RESET to the shipped mix on apply -- a preset is a known
// place to start from, not a tweak -- and the badge only claims a preset while
// the draft actually matches it.
// ---------------------------------------------------------------------------

type PresetId = "cautious" | "balanced" | "aggressive";

/** The shipped signal mixes (see engine/policy.py defaults). A preset resets to these. */
const DEFAULT_WEIGHTS: Record<"movie" | "tv", Record<string, number>> = {
  movie: { unwatched: 70, few_watchers: 20, low_rating: 10 },
  tv: { unwatched: 60, few_watchers: 15, season_rank: 15, low_rating: 10 },
};

type PresetCaps = Pick<
  ProfileSettings,
  "max_items_per_run" | "max_bytes_per_run" | "max_items_per_30d" | "max_bytes_per_30d" | "grace_days"
>;

const PRESETS: { id: PresetId; label: string; help: string; condemn_at: number; caps: PresetCaps }[] = [
  {
    id: "cautious",
    label: "Cautious",
    help: "Cautious: only flags a title it is very sure about, removes less per run, and waits a month of grace.",
    condemn_at: 82,
    caps: {
      max_items_per_run: 5,
      max_bytes_per_run: 250_000_000_000,
      max_items_per_30d: 50,
      max_bytes_per_30d: 1_000_000_000_000,
      grace_days: 30,
    },
  },
  {
    id: "balanced",
    label: "Balanced",
    help: "Balanced: the defaults Reaper ships with.",
    condemn_at: 70,
    caps: {
      max_items_per_run: 10,
      max_bytes_per_run: 500_000_000_000,
      max_items_per_30d: 100,
      max_bytes_per_30d: 2_000_000_000_000,
      grace_days: 14,
    },
  },
  {
    id: "aggressive",
    label: "Aggressive",
    help: "Aggressive: flags sooner, allows bigger runs, and keeps the one-week minimum grace.",
    condemn_at: 58,
    caps: {
      max_items_per_run: 25,
      max_bytes_per_run: 1_000_000_000_000,
      max_items_per_30d: 150,
      max_bytes_per_30d: 4_000_000_000_000,
      grace_days: 7,
    },
  },
];

/** Which preset the draft currently IS, or null for "Custom". Honest by construction:
 *  a preset badge is only shown while the threshold matches it AND the weights are the
 *  shipped mix -- hand-tuned weights always read as Custom. */
function activePreset(draft: PolicyBody): PresetId | null {
  const mix = DEFAULT_WEIGHTS[draft.media_type === "tv" ? "tv" : "movie"];
  const weightsAreDefault = draft.signals.every((s) => s.weight === (mix[s.signal] ?? 0));
  if (!weightsAreDefault) return null;
  return PRESETS.find((p) => p.condemn_at === draft.condemn_at)?.id ?? null;
}

/** "1095 days" said the way a person would: "3 years". */
function humanDays(days: number): string {
  if (days >= 365 && days % 365 === 0) {
    const y = days / 365;
    return y === 1 ? "1 year" : `${y} years`;
  }
  if (days >= 30 && days % 30 === 0) {
    const m = days / 30;
    return m === 1 ? "1 month" : `${m} months`;
  }
  return `${days} days`;
}

// ---------------------------------------------------------------------------
// Keep-tags: a set of *arr tags that spare a title, with an ANY/ALL switch.
// ---------------------------------------------------------------------------

/** Removable chips plus a free-type box. Lives in its own card in "What's always kept". */
function KeepTagsEditor({
  tags,
  match,
  onTags,
  onMatch,
}: {
  tags: string[];
  match: "any" | "all";
  onTags: (t: string[]) => void;
  onMatch: (m: "any" | "all") => void;
}) {
  const [input, setInput] = useState("");
  const add = () => {
    const t = input.trim();
    if (t && !tags.includes(t)) onTags([...tags, t]);
    setInput("");
  };
  return (
    <div className="keep-tags">
      <div className="tag-chips">
        {tags.map((t) => (
          <span key={t} className="tag-chip">
            {t}
            <button onClick={() => onTags(tags.filter((x) => x !== t))} aria-label={`Remove ${t}`}>
              ×
            </button>
          </span>
        ))}
        <input
          value={input}
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" || e.key === ",") {
              e.preventDefault();
              add();
            }
          }}
          onBlur={add}
          placeholder="add a tag…"
        />
      </div>
      {tags.length >= 1 && (
        <label className="tag-match">
          <span className="muted">Keep a title with</span>
          <select value={match} onChange={(e) => onMatch(e.target.value as "any" | "all")}>
            <option value="any">any of these tags</option>
            <option value="all">all of these tags</option>
          </select>
        </label>
      )}
      {tags.length === 0 && <p className="help">No tags: this protection keeps nothing.</p>}
    </div>
  );
}

/** One protection: a switch, a plain-English label and help, and -- where it has one -- a
 *  threshold in the units a person thinks in. */
function GateRow({ gate, onChange }: { gate: GateSetting; onChange: (g: GateSetting) => void }) {
  const meta = GATE_META[gate.gate] ?? { label: titleCase(gate.gate), help: "" };

  return (
    <li className="rule-row">
      <label className="toggle rule-toggle">
        <Switch
          checked={gate.enabled}
          onChange={(enabled) => onChange({ ...gate, enabled })}
        />
        <span className="rule-name">{meta.label}</span>
      </label>
      {meta.help && <p className="help rule-help">{meta.help}</p>}

      {gate.enabled && meta.unit === "days" && (
        <div className="rule-control">
          <span>at least</span>
          <QuantityInput
            value={gate.threshold}
            units={TIME_UNITS}
            min={365}
            onChange={(v) => onChange({ ...gate, threshold: v })}
          />
        </div>
      )}
      {gate.enabled && meta.unit === "people" && (
        <div className="rule-control">
          <span>at least</span>
          <input
            type="number"
            min={1}
            value={gate.threshold || 1}
            onChange={(e) => onChange({ ...gate, threshold: Number(e.target.value) || 1 })}
          />
          <span>{(gate.threshold || 1) === 1 ? "person" : "people"}</span>
        </div>
      )}
    </li>
  );
}

// ---------------------------------------------------------------------------
// Keep well-rated: a set of per-source rating bars, cleared ANY-of (or ALL).
// ---------------------------------------------------------------------------

type RatingSourceMeta = {
  label: string;
  scale: "ten" | "pct";
  votes: boolean;
  defFloor: number; // in tenths (7.5 -> 75; 75% -> 75)
  defVotes: number;
};

const RATING_META: Record<RatingSource, RatingSourceMeta> = {
  imdb: { label: "IMDb", scale: "ten", votes: true, defFloor: 65, defVotes: 5000 },
  rotten_tomatoes_critic: { label: "Rotten Tomatoes critics", scale: "pct", votes: false, defFloor: 75, defVotes: 0 },
  rotten_tomatoes_audience: { label: "Rotten Tomatoes audience", scale: "pct", votes: false, defFloor: 80, defVotes: 0 },
  metacritic: { label: "Metacritic", scale: "pct", votes: false, defFloor: 70, defVotes: 0 },
  tmdb: { label: "TMDb", scale: "ten", votes: true, defFloor: 70, defVotes: 500 },
};
const RATING_ORDER: RatingSource[] = [
  "imdb",
  "rotten_tomatoes_critic",
  "rotten_tomatoes_audience",
  "metacritic",
  "tmdb",
];

/** One title clears one bar if `describe` reads true; the summary reads the whole set. */
function describeBar(rule: RatingRule): string {
  const meta = RATING_META[rule.source];
  if (meta.scale === "pct") return `${meta.label} ${rule.floor}%`;
  const votes = meta.votes && rule.min_votes > 0 ? ` from ${rule.min_votes.toLocaleString()}+ votes` : "";
  return `${(rule.floor / 10).toFixed(1)} on ${meta.label}${votes}`;
}

/** One editable bar: a source, its threshold in the source's own units, and (for the vote
 *  sources) a vote floor. Styled with the same rule-control row the other protections use. */
function RatingBarRow({
  rule,
  onChange,
  onRemove,
}: {
  rule: RatingRule;
  onChange: (r: RatingRule) => void;
  onRemove: () => void;
}) {
  const meta = RATING_META[rule.source];
  return (
    <div className="rule-control rating-bar">
      <span className="rating-bar-src">{meta.label}</span>
      <span>at least</span>
      {meta.scale === "ten" ? (
        <>
          <input
            type="number"
            min={0}
            max={10}
            step={0.1}
            value={(rule.floor / 10).toFixed(1)}
            onChange={(e) => onChange({ ...rule, floor: Math.round(Number(e.target.value) * 10) })}
          />
          <span>/ 10</span>
          {meta.votes && (
            <>
              <span>from at least</span>
              <input
                type="number"
                min={0}
                step={100}
                value={rule.min_votes}
                onChange={(e) => onChange({ ...rule, min_votes: Math.max(0, Number(e.target.value) || 0) })}
              />
              <span>votes</span>
            </>
          )}
        </>
      ) : (
        <>
          <input
            type="number"
            min={0}
            max={100}
            step={1}
            value={rule.floor}
            onChange={(e) => onChange({ ...rule, floor: Number(e.target.value) || 0 })}
          />
          <span>%</span>
        </>
      )}
      <button
        type="button"
        className="rating-remove"
        onClick={onRemove}
        aria-label={`Remove the ${meta.label} bar`}
      >
        ×
      </button>
    </div>
  );
}

/** "Keep well-rated titles": the toggle, then a bar per source, an add-source picker, an
 *  any/all switch, and a plain-English summary -- the mockup's layout in the policy's own
 *  controls. Replaces the single-IMDb row the gate used to carry. */
function RatingFloorRow({
  gate,
  rules,
  match,
  mediaType,
  onGate,
  onRules,
  onMatch,
}: {
  gate: GateSetting;
  rules: RatingRule[];
  match: "any" | "all";
  mediaType: "movie" | "tv";
  onGate: (g: GateSetting) => void;
  onRules: (r: RatingRule[]) => void;
  onMatch: (m: "any" | "all") => void;
}) {
  const used = new Set(rules.map((r) => r.source));
  const available = RATING_ORDER.filter((s) => !used.has(s));
  const addSource = (source: RatingSource) => {
    const meta = RATING_META[source];
    onRules([...rules, { source, floor: meta.defFloor, min_votes: meta.votes ? meta.defVotes : 0 }]);
  };
  const joiner = match === "any" ? ", or " : ", and ";
  const summary =
    rules.length === 0
      ? "No rating bars set, so this protection keeps nothing."
      : `Keep a title rated at least ${rules.map(describeBar).join(joiner)}.`;

  return (
    <li className="rule-row">
      <label className="toggle rule-toggle">
        <Switch checked={gate.enabled} onChange={(enabled) => onGate({ ...gate, enabled })} />
        <span className="rule-name">Keep well-rated titles</span>
      </label>
      <p className="help rule-help">
        A title that clears {match === "any" ? "any one" : "all"} of these bars is kept, whatever it
        scored.
      </p>
      {gate.enabled && (
        <div className="rating-bars">
          {rules.map((rule, i) => (
            <RatingBarRow
              key={rule.source}
              rule={rule}
              onChange={(r) => onRules(rules.map((x, j) => (j === i ? r : x)))}
              onRemove={() => onRules(rules.filter((_, j) => j !== i))}
            />
          ))}
          {available.length > 0 && (
            <div className="rule-control">
              <select
                value=""
                aria-label="Add a rating source"
                onChange={(e) => {
                  if (e.target.value) addSource(e.target.value as RatingSource);
                }}
              >
                <option value="">Add a rating source…</option>
                {available.map((s) => (
                  <option key={s} value={s}>
                    {RATING_META[s].label}
                  </option>
                ))}
              </select>
            </div>
          )}
          {rules.length >= 2 && (
            <label className="tag-match">
              <span className="muted">Keep a title that clears</span>
              <select value={match} onChange={(e) => onMatch(e.target.value as "any" | "all")}>
                <option value="any">any one of these</option>
                <option value="all">all of these</option>
              </select>
            </label>
          )}
          <p className="help">{summary}</p>
          {mediaType === "tv" && (
            <p className="help">
              TV has IMDb, plus any scores Plex carries for the show. Rotten Tomatoes and Metacritic
              are often missing for TV, so those bars just won't match a show.
            </p>
          )}
        </div>
      )}
    </li>
  );
}

/** One signal: a plain-English label, its help, a slider, and a *quantified* readout -- the
 *  raw weight and the share of the score it currently accounts for, because "a lot" told you
 *  nothing you could act on. */
function SignalRow({
  signal,
  totalWeight,
  onChange,
}: {
  signal: SignalSetting;
  totalWeight: number;
  onChange: (s: SignalSetting) => void;
}) {
  const meta = SIGNAL_META[signal.signal] ?? { label: titleCase(signal.signal), help: "" };
  const share = totalWeight > 0 ? Math.round((signal.weight / totalWeight) * 100) : 0;

  return (
    <li className="rule-row">
      <div className="rule-name-row">
        <span className="rule-name">{meta.label}</span>
        <span className="rule-strength">
          {signal.weight === 0 ? (
            <span className="muted">off</span>
          ) : (
            <>
              <strong>{signal.weight}</strong>
              <span className="muted">/100 · {share}% of the score</span>
            </>
          )}
        </span>
      </div>
      {meta.help && <p className="help rule-help">{meta.help}</p>}
      <input
        type="range"
        min={0}
        max={100}
        value={signal.weight}
        aria-label={`How much "${meta.label}" matters`}
        onChange={(e) => onChange({ ...signal, weight: Number(e.target.value) })}
      />
    </li>
  );
}

// ---------------------------------------------------------------------------
// The owner's own rules: sentences in, sentences out.
// ---------------------------------------------------------------------------

const OP_LABELS: Record<string, string> = {
  gte: "is at least",
  lte: "is at most",
  eq: "is",
  in: "is one of",
  contains: "contains",
};

// A vocabulary field already handled by a built-in protection above -> not offered as a
// custom rule, so the two never say the same thing twice. Only fields with no built-in gate
// (size, all-time watchers, vote count, season rank) remain to be authored here.
const FIELD_TO_GATE: Record<string, string> = {
  days_unwatched: "min_dormancy",
  recent_watchers: "server_popularity",
  imdb_rating: "rating_floor",
  on_curated_list: "curated_list",
  whitelisted: "whitelisted",
  streaming_now: "streaming_now",
};

// The built-in signals already cover these fields, so they are not offered as custom
// "remove" rules -- the two never say the same thing twice. That leaves the new metadata
// fields (genre, requested, quality, release age, show ended) to be authored here.
const FIELD_TO_SIGNAL: Record<string, string> = {
  days_unwatched: "unwatched",
  recent_watchers: "few_watchers",
  imdb_rating: "low_rating",
  season_rank: "season_rank",
  size_bytes: "size",
};

// The ramp phrasing per field, offered as an extra choice in the condition dropdown.
// Curated: a phrase exists only where more-of-the-number honestly means more reason to
// remove. (Most of these fields are filtered out of the remove vocabulary today because a
// built-in signal covers them; the map stays complete so a future field just works.)
const RAMP_PHRASES: Record<string, string> = {
  days_unwatched: "the longer it sits unwatched",
  size_bytes: "the bigger it is",
  season_rank: "the older the season",
  release_age: "the older it is",
};

/** The sentinel option value for a ramp phrase in the condition dropdown. */
const RAMP_OP = "__ramp__";

/** Sensible starting ramp per field type, in the field's native units. */
function rampDefaults(field: VocabField): [number, number] {
  if (field.type === "days") return [365, 1825];
  if (field.type === "bytes") return [20_000_000_000, 80_000_000_000];
  if (field.type === "rating_tenths") return [40, 70];
  return [1, 5];
}

/** A ramp bound, said in the field's own units. */
function rampValue(field: VocabField | undefined, value: number): string {
  if (field?.type === "bytes") return `${Math.round(value / 1e9)} GB`;
  if (field?.type === "days") return humanDays(value);
  if (field?.type === "rating_tenths") return (value / 10).toFixed(1);
  const suffix = field?.unit_suffix ? ` ${field.unit_suffix}` : "";
  return `${value.toLocaleString()}${suffix}`;
}

/** Coerce a text input into the value the wire expects, in the field's own units. */
function coerceValue(field: VocabField, raw: string): number | string | boolean {
  if (field.type === "bool") return raw === "true";
  if (field.type === "rating_tenths") return Math.round(Number(raw) * 10);
  if (field.type === "bytes") return Math.round(Number(raw) * 1e9);
  if (field.type === "text") return raw;
  return Math.round(Number(raw));
}

/** A name that reads like the rule and does not collide with an existing one. */
function uniqueName(existing: { name: string }[], base: string): string {
  const taken = new Set(existing.map((r) => r.name));
  if (!taken.has(base)) return base;
  for (let i = 2; ; i++) if (!taken.has(`${base} ${i}`)) return `${base} ${i}`;
}

/** Turn a stored condition back into a sentence, in the units a person reads. */
function describeCondition(c: Condition, fields: VocabField[]): string {
  const f = fields.find((x) => x.key === c.field);
  const label = f?.label ?? c.field;
  const op = OP_LABELS[c.op] ?? c.op;
  let value: string;
  let unit = f?.unit_suffix ? ` ${f.unit_suffix}` : "";
  if (f?.type === "rating_tenths" && typeof c.value === "number") value = (c.value / 10).toFixed(1);
  else if (f?.type === "bytes" && typeof c.value === "number") {
    value = `${Math.round(c.value / 1e9)} GB`;
    unit = "";
  } else if (f?.type === "bool") {
    value = c.value ? "yes" : "no";
    unit = "";
  } else if (typeof c.value === "number") value = c.value.toLocaleString();
  else value = String(c.value);
  return `Keep it when ${label} ${op} ${value}${unit}`;
}

function describeCondemn(rule: CustomCondemn, fields: VocabField[]): string {
  const f = fields.find((x) => x.key === rule.field);
  const label = f?.label ?? rule.field;
  if (rule.kind === "graded") {
    const phrase = RAMP_PHRASES[rule.field] ?? "the higher it is";
    return `${label}: ${phrase} (from ${rampValue(f, rule.floor)} to ${rampValue(f, rule.saturate_at)})`;
  }
  const op = OP_LABELS[rule.op] ?? rule.op;
  const value = f?.type === "bool" ? (rule.value ? "yes" : "no") : String(rule.value);
  return `${label} ${op} ${value}`;
}

/** A value input that suggests what the library already contains -- and still takes
 *  anything typed. The suggestions are an ordinary in-app popover, NOT a native
 *  <datalist>: Safari only reveals a datalist after you type a matching prefix (an empty
 *  click shows nothing), and embedded panes can draw the native popup in the wrong place
 *  entirely. Free entry stays valid because validation is by type, never by membership. */
function SuggestInput({
  field,
  value,
  onChange,
}: {
  field: VocabField;
  value: string;
  onChange: (v: string) => void;
}) {
  const isText = field.type === "text";
  const { data } = useQuery({
    queryKey: ["vocab-values", field.key],
    queryFn: () => api.vocabularyValues(field.key),
    enabled: isText,
    staleTime: 60_000,
  });
  const [open, setOpen] = useState(false);
  const [highlight, setHighlight] = useState(-1);

  if (!isText) {
    return (
      <input
        type="number"
        step={field.type === "rating_tenths" ? "0.1" : undefined}
        value={value}
        onChange={(e) => onChange(e.target.value)}
        placeholder={field.unit_suffix || "value"}
      />
    );
  }

  const values = data?.values ?? [];
  const query = value.trim().toLowerCase();
  const matches = query ? values.filter((v) => v.toLowerCase().includes(query)) : values;
  const show = open && matches.length > 0;

  const pick = (v: string) => {
    onChange(v);
    setOpen(false);
    setHighlight(-1);
  };

  return (
    <span className="suggest">
      <input
        type="text"
        role="combobox"
        aria-expanded={show}
        aria-autocomplete="list"
        value={value}
        placeholder={field.unit_suffix || "value"}
        onChange={(e) => {
          onChange(e.target.value);
          setOpen(true);
          setHighlight(-1);
        }}
        onFocus={() => setOpen(true)}
        onBlur={() => {
          // Option clicks commit on mousedown, which fires before this blur.
          setOpen(false);
          setHighlight(-1);
        }}
        onKeyDown={(e) => {
          if (!show) {
            if (e.key === "ArrowDown") setOpen(true);
            return;
          }
          if (e.key === "ArrowDown") {
            e.preventDefault();
            setHighlight((h) => (h + 1) % matches.length);
          } else if (e.key === "ArrowUp") {
            e.preventDefault();
            setHighlight((h) => (h - 1 + matches.length) % matches.length);
          } else if (e.key === "Enter" && highlight >= 0) {
            e.preventDefault();
            const hit = matches[highlight];
            if (hit !== undefined) pick(hit);
          } else if (e.key === "Escape") {
            setOpen(false);
            setHighlight(-1);
          }
        }}
      />
      {show && (
        <ul className="suggest-pop" role="listbox">
          {matches.map((v, i) => (
            <li
              key={v}
              role="option"
              aria-selected={i === highlight}
              className={i === highlight ? "suggest-opt active" : "suggest-opt"}
              onMouseDown={(e) => {
                e.preventDefault();
                pick(v);
              }}
              onMouseEnter={() => setHighlight(i)}
            >
              {v}
            </li>
          ))}
        </ul>
      )}
    </span>
  );
}

/** The owner's own "reasons to remove" (custom condemn rules). One form: the condition
 *  dropdown carries plain comparisons AND, for numeric fields where it honestly applies,
 *  a ramp phrase like "the older it is" -- picking the phrase swaps the value box for a
 *  from/to pair and the rule scales up between them, like the built-in signals. */
function RemoveRulesEditor({
  condemn,
  onCondemn,
}: {
  condemn: CustomCondemn[];
  onCondemn: (r: CustomCondemn[]) => void;
}) {
  const { data: condemnVocab } = useQuery({
    queryKey: ["vocabulary", "condemn"],
    queryFn: () => api.vocabulary("condemn"),
  });
  const condemnAll = condemnVocab?.fields ?? [];
  // Only the new metadata fields, not those a built-in signal already scores.
  const condemnFields = condemnAll.filter((f) => !FIELD_TO_SIGNAL[f.key]);

  const [rField, setRField] = useState("");
  const [rOp, setROp] = useState("");
  const [rValue, setRValue] = useState("");
  const [rWeight, setRWeight] = useState(20);
  const [rFrom, setRFrom] = useState(0);
  const [rTo, setRTo] = useState(1);
  const field = condemnFields.find((f) => f.key === rField);
  const rampable = Boolean(
    field && RAMP_PHRASES[field.key] && field.type !== "bool" && field.type !== "text",
  );
  const isRamp = rOp === RAMP_OP;

  useEffect(() => {
    const f = condemnFields.find((x) => x.key === rField);
    if (!f) return;
    setROp(f.ops[0] ?? "");
    setRValue(f.type === "bool" ? "true" : "");
    const [from, to] = rampDefaults(f);
    setRFrom(from);
    setRTo(to);
  }, [rField]); // eslint-disable-line react-hooks/exhaustive-deps

  const add = () => {
    if (!field || !rOp) return;
    if (isRamp) {
      const phrase = RAMP_PHRASES[field.key];
      onCondemn([
        ...condemn,
        {
          kind: "graded",
          name: uniqueName(condemn, `${field.label}: ${phrase}`),
          field: field.key,
          weight: rWeight,
          floor: rFrom,
          saturate_at: Math.max(rFrom + 1, rTo),
        },
      ]);
    } else {
      onCondemn([
        ...condemn,
        {
          kind: "boolean",
          name: uniqueName(condemn, field.label),
          field: field.key,
          op: rOp,
          value: coerceValue(field, rValue),
          weight: rWeight,
        },
      ]);
    }
    setRField("");
    setRValue("");
  };

  return (
    <div className="rules-card">
      <h3>Your own reasons to remove</h3>
      <p className="blurb">
        Add pressure to the score with a rule of your own. These can flag a title, but a
        protection still wins, and missing data only ever leans toward keeping.
      </p>

      {condemn.length > 0 && (
        <div className="rules-table">
          {condemn.map((r, i) => (
            <div className="rules-row rules-row-simple" key={`c-${r.name}-${i}`}>
              <span className="rules-rule">{describeCondemn(r, condemnAll)}</span>
              <span className="rules-weight-remove">
                {r.kind === "graded" ? `up to +${r.weight}` : `+${r.weight} to the score`}
              </span>
              <button className="ghost sm" onClick={() => onCondemn(condemn.filter((_, j) => j !== i))}>
                Remove
              </button>
            </div>
          ))}
        </div>
      )}

      <div className="condition-add">
        <select value={rField} onChange={(e) => setRField(e.target.value)}>
          <option value="">when…</option>
          {condemnFields.map((f) => (
            <option key={f.key} value={f.key}>
              {f.label}
            </option>
          ))}
        </select>
        {field && (
          <>
            <select value={rOp} onChange={(e) => setROp(e.target.value)}>
              {field.ops.map((o) => (
                <option key={o} value={o}>
                  {OP_LABELS[o] ?? o}
                </option>
              ))}
              {rampable && <option value={RAMP_OP}>{RAMP_PHRASES[field.key]}</option>}
            </select>
            {isRamp ? (
              <span className="ramp-bounds">
                <span className="muted">from</span>
                {field.type === "days" ? (
                  <QuantityInput value={rFrom} units={TIME_UNITS} onChange={setRFrom} />
                ) : field.type === "bytes" ? (
                  <QuantityInput value={rFrom} units={SIZE_UNITS} onChange={setRFrom} />
                ) : (
                  <input
                    type="number"
                    value={rFrom}
                    onChange={(e) => setRFrom(Number(e.target.value) || 0)}
                  />
                )}
                <span className="muted">to</span>
                {field.type === "days" ? (
                  <QuantityInput value={rTo} units={TIME_UNITS} onChange={setRTo} />
                ) : field.type === "bytes" ? (
                  <QuantityInput value={rTo} units={SIZE_UNITS} onChange={setRTo} />
                ) : (
                  <input
                    type="number"
                    value={rTo}
                    onChange={(e) => setRTo(Number(e.target.value) || 1)}
                  />
                )}
              </span>
            ) : field.type === "bool" ? (
              <select value={rValue} onChange={(e) => setRValue(e.target.value)}>
                <option value="true">yes</option>
                <option value="false">no</option>
              </select>
            ) : (
              <SuggestInput field={field} value={rValue} onChange={setRValue} />
            )}
            <label className="inline-weight">
              {isRamp ? "up to" : "weight"}
              <input
                type="number"
                min={0}
                max={100}
                value={rWeight}
                onChange={(e) => setRWeight(Number(e.target.value))}
              />
            </label>
            <button
              className="ghost sm"
              onClick={add}
              disabled={!isRamp && field.type !== "bool" && rValue === ""}
            >
              Add rule
            </button>
          </>
        )}
      </div>
      {field?.help_text && <p className="help">{field.help_text}</p>}
      <p className="help">
        The choices match the field: numbers get at least / at most, words get is / is one of /
        contains. A phrase like “the older it is” builds pressure gradually between two numbers,
        like the built-in signals above, and its weight is a ceiling. There is no “is not”, on
        purpose.
      </p>
    </div>
  );
}

/** The owner's own keep rules, both strengths in one card: a rule can keep a title
 *  outright (a protection), or just lean toward keeping by lowering its score. Neither
 *  can ever flag anything -- the protect vocabulary is filtered server-side. */
function KeepRulesEditor({
  conditions,
  keeps,
  gateIds,
  onConditions,
  onKeeps,
}: {
  conditions: Condition[];
  keeps: GradedKeep[];
  gateIds: string[];
  onConditions: (c: Condition[]) => void;
  onKeeps: (k: GradedKeep[]) => void;
}) {
  const { data: vocab } = useQuery({
    queryKey: ["vocabulary", "protect"],
    queryFn: () => api.vocabulary("protect"),
  });
  const allFields = vocab?.fields ?? [];
  // Only offer fields that aren't already a built-in protection above.
  const hardFields = allFields.filter((f) => {
    const gate = FIELD_TO_GATE[f.key];
    return !gate || !gateIds.includes(gate);
  });
  // A lean ramps a number, so only numeric fields (those that accept >=) can drive one.
  const leanFields = allFields.filter((f) => f.ops.includes("gte"));

  const [strength, setStrength] = useState<"hard" | "lean">("hard");

  // --- keep it outright ------------------------------------------------------
  const [hField, setHField] = useState("");
  const [hOp, setHOp] = useState("");
  const [hValue, setHValue] = useState("");
  const hardField = hardFields.find((f) => f.key === hField);
  useEffect(() => {
    const f = hardFields.find((x) => x.key === hField);
    if (!f) return;
    setHOp(f.ops[0] ?? "");
    setHValue(f.type === "bool" ? "true" : "");
  }, [hField]); // eslint-disable-line react-hooks/exhaustive-deps
  const addHard = () => {
    if (!hardField || !hOp) return;
    onConditions([
      ...conditions,
      { field: hardField.key, op: hOp, value: coerceValue(hardField, hValue) },
    ]);
    setHField("");
    setHValue("");
  };

  // --- lean toward keeping ---------------------------------------------------
  const [lField, setLField] = useState("");
  const [lPoints, setLPoints] = useState(15);
  const [lAt, setLAt] = useState("");
  const [lDir, setLDir] = useState<"high_keeps" | "low_keeps">("high_keeps");
  const leanField = leanFields.find((f) => f.key === lField);
  const addLean = () => {
    if (!leanField || lAt === "") return;
    const saturate = Number(coerceValue(leanField, lAt));
    onKeeps([
      ...keeps,
      {
        name: uniqueName(keeps, leanField.label),
        field: leanField.key,
        max_discount: lPoints,
        floor: 0,
        saturate_at: Math.max(1, saturate),
        direction: lDir,
      },
    ]);
    setLField("");
    setLAt("");
  };

  return (
    <div className="rules-card">
      <h3>Your own keep rules</h3>
      <p className="blurb">
        Two strengths: a rule can keep a title <strong>outright</strong>, or just{" "}
        <strong>lean toward keeping</strong> by lowering its score. Neither can ever flag
        anything, and missing data takes the full lean, to be safe.
      </p>

      {(conditions.length > 0 || keeps.length > 0) && (
        <div className="rules-table">
          {conditions.map((c, i) => (
            <div className="rules-row rules-row-simple" key={`h-${c.field}-${c.op}-${i}`}>
              <span className="rules-rule">
                <span className="rule-kind">Keeps it, always · </span>
                {describeCondition(c, allFields)}
              </span>
              <span className="rules-weight-keep">kept outright</span>
              <button
                className="ghost sm"
                onClick={() => onConditions(conditions.filter((_, j) => j !== i))}
              >
                Remove
              </button>
            </div>
          ))}
          {keeps.map((k, i) => {
            const f = allFields.find((x) => x.key === k.field);
            return (
              <div className="rules-row rules-row-simple" key={`k-${k.name}-${i}`}>
                <span className="rules-rule">
                  <span className="rule-kind">Leans · </span>
                  {f?.label ?? k.field}: the {k.direction === "low_keeps" ? "less" : "more"}, the
                  safer (full effect at {rampValue(f, k.saturate_at)})
                </span>
                <span className="rules-weight-keep">lowers the score, up to −{k.max_discount}</span>
                <button className="ghost sm" onClick={() => onKeeps(keeps.filter((_, j) => j !== i))}>
                  Remove
                </button>
              </div>
            );
          })}
        </div>
      )}

      <div className="rules-add">
        <div className="segmented" role="group" aria-label="Rule strength">
          <button
            type="button"
            className={strength === "hard" ? "seg active" : "seg"}
            onClick={() => setStrength("hard")}
          >
            Keeps it outright
          </button>
          <button
            type="button"
            className={strength === "lean" ? "seg active" : "seg"}
            onClick={() => setStrength("lean")}
          >
            Leans toward keeping
          </button>
        </div>

        {strength === "hard" ? (
          <div className="condition-add">
            <select value={hField} onChange={(e) => setHField(e.target.value)}>
              <option value="">Keep it when…</option>
              {hardFields.map((f) => (
                <option key={f.key} value={f.key}>
                  {f.label}
                </option>
              ))}
            </select>
            {hardField && (
              <>
                <select value={hOp} onChange={(e) => setHOp(e.target.value)}>
                  {hardField.ops.map((o) => (
                    <option key={o} value={o}>
                      {OP_LABELS[o] ?? o}
                    </option>
                  ))}
                </select>
                {hardField.type === "bool" ? (
                  <select value={hValue} onChange={(e) => setHValue(e.target.value)}>
                    <option value="true">yes</option>
                    <option value="false">no</option>
                  </select>
                ) : (
                  <SuggestInput field={hardField} value={hValue} onChange={setHValue} />
                )}
                <button
                  className="ghost sm"
                  onClick={addHard}
                  disabled={hardField.type !== "bool" && hValue === ""}
                >
                  Add rule
                </button>
              </>
            )}
            {hardField?.help_text && <p className="help">{hardField.help_text}</p>}
          </div>
        ) : (
          <div className="condition-add">
            <select value={lField} onChange={(e) => setLField(e.target.value)}>
              <option value="">when…</option>
              {leanFields.map((f) => (
                <option key={f.key} value={f.key}>
                  {f.label}
                </option>
              ))}
            </select>
            {leanField && (
              <>
                <select
                  value={lDir}
                  onChange={(e) => setLDir(e.target.value as "high_keeps" | "low_keeps")}
                >
                  <option value="high_keeps">the more, the safer</option>
                  <option value="low_keeps">the less, the safer</option>
                </select>
                <input
                  type="number"
                  step={leanField.type === "rating_tenths" ? "0.1" : undefined}
                  value={lAt}
                  onChange={(e) => setLAt(e.target.value)}
                  placeholder={`full effect at ${leanField.unit_suffix || "value"}`}
                />
                <label className="inline-weight">
                  up to −
                  <input
                    type="number"
                    min={1}
                    max={100}
                    value={lPoints}
                    onChange={(e) => setLPoints(Number(e.target.value))}
                  />
                </label>
                <button className="ghost sm" onClick={addLean} disabled={lAt === ""}>
                  Add rule
                </button>
              </>
            )}
            {leanField?.help_text && <p className="help">{leanField.help_text}</p>}
          </div>
        )}
      </div>
      <p className="help">
        Suggestions are values from your own library. Pick one, or type anything else. Fields
        already covered by a protection you've turned on aren't offered for outright keeps, so a
        rule never repeats a built-in.
      </p>
    </div>
  );
}

/** Live advisory beside the keep-last input: how many shows a keep-last-N value fully
 *  protects, computed from the last scan's season shape -- no re-scan, since the shape does
 *  not depend on the keep-last value. */
function SeasonAdvisory({ keepLast }: { keepLast: number }) {
  const { data } = useQuery({ queryKey: ["season-shape"], queryFn: () => api.seasonShape() });
  if (!data || data.total_shows === 0) return null;
  const covered = Object.entries(data.season_counts).reduce(
    (sum, [seasons, shows]) => (Number(seasons) <= keepLast ? sum + shows : sum),
    0,
  );
  if (covered === 0) return null;
  return (
    <p className={`help ${covered === data.total_shows ? "help-warn" : ""}`}>
      With this setting, <strong>{count(covered)}</strong> of {count(data.total_shows)} shows have
      no season eligible for removal (from your last scan).
    </p>
  );
}

// ---------------------------------------------------------------------------
// The simulator column.
// ---------------------------------------------------------------------------

/** The histogram, with the threshold drawn across it.
 *
 *  This is what makes a threshold a decision rather than a guess: you place it against
 *  the actual shape of the library, and you can see how many items sit just the wrong
 *  side of it. */
function Histogram({ buckets, threshold }: { buckets: number[]; threshold: number }) {
  const peak = Math.max(...buckets, 1);

  return (
    <div className="histogram" aria-hidden>
      {buckets.map((n, i) => {
        const low = i * 10;
        const condemned = low + 10 > threshold;
        return (
          <div className="hist-col" key={low} title={`${low}–${low + 9}: ${count(n)} items`}>
            <div
              className={condemned ? "hist-bar hist-condemn" : "hist-bar"}
              style={{ height: `${(n / peak) * 100}%` }}
            />
            <span className="hist-label">{low}</span>
          </div>
        );
      })}
      <div className="hist-thresh" style={{ left: `${threshold}%` }}>
        <b>{threshold}</b>
      </div>
    </div>
  );
}

/** The "needs a scan" state. Informational, not an error: you didn't do anything wrong,
 *  the numbers just can't be re-derived from the old scan. So it's neutral, short, and gives
 *  you the one button that fixes it. A start that fails says so right here, or the button
 *  would appear to do nothing. */
function StaleNotice({
  scanning,
  starting,
  startError,
  onScan,
  percent,
  detail,
}: {
  scanning: boolean;
  starting: boolean;
  startError: string | null;
  onScan: () => void;
  percent: number;
  detail: string;
}) {
  return (
    <div className="sim sim-info">
      <h3>{scanning ? "Rescanning to apply your changes" : "Needs a fresh scan"}</h3>
      {scanning ? (
        <>
          <p>
            Scoring your library under the new policy. You can leave this page; it keeps running,
            and the numbers here refresh when it finishes.
          </p>
          <p className="muted">
            {detail || "Working"} · {percent}%
          </p>
          <div className="bar">
            <div className="bar-fill" style={{ width: `${percent}%` }} />
          </div>
        </>
      ) : (
        <>
          <p>
            You changed how the scan reads your library (a watch window, a keep tag, or a season
            rule), so the last scan's evidence no longer fits this policy. Scan to apply it.
          </p>
          <button className="primary sm" onClick={onScan} disabled={starting}>
            {starting ? "Starting…" : "Scan now"}
          </button>
        </>
      )}
      {startError && <p className="error">The scan didn't start: {startError}</p>}
    </div>
  );
}

function Outcome({
  simulation,
  threshold,
  pace,
}: {
  simulation: Simulation;
  threshold: number;
  pace: ProfileSettings | null;
}) {
  // The saved policy's count is derivable: what this draft flags, minus what only this
  // draft flags, plus what only the saved one flags.
  const savedFlags =
    simulation.condemned - simulation.newly_condemned + simulation.no_longer_condemned;
  const moreExamples = simulation.newly_condemned - simulation.examples_newly_condemned.length;

  return (
    <div className="sim">
      <div className="sim-headline">
        <div>
          <span className="sim-number">{count(simulation.condemned)}</span>
          <span className="sim-unit">items would be deleted</span>
        </div>
        <div>
          <span className="sim-number">{bytes(simulation.reclaimable_bytes)}</span>
          <span className="sim-unit">reclaimed</span>
        </div>
      </div>

      <p className="sim-compare">
        Your saved policy flags <strong>{count(savedFlags)}</strong>. This draft flags{" "}
        <strong>{count(simulation.condemned)}</strong>.
      </p>

      <Histogram buckets={simulation.histogram} threshold={threshold} />
      <p className="help">
        Everyone's score, 0 to 100. The line is your threshold. Red bars are past it.
      </p>

      {simulation.examples_newly_condemned.length > 0 && (
        <>
          <h3>New on the list</h3>
          <ul className="sim-examples">
            {simulation.examples_newly_condemned.map((e) => (
              <li key={`${e.title}-${e.year ?? ""}`}>
                <span>
                  {e.title}
                  {e.year !== null && <span className="muted"> ({e.year})</span>}
                </span>
                <span className="sim-example-score">{e.score}</span>
              </li>
            ))}
            {moreExamples > 0 && (
              <li className="muted">
                …and {count(moreExamples)} more that your saved policy left alone
              </li>
            )}
          </ul>
        </>
      )}

      {simulation.protected_by.length > 0 && (
        <>
          <h3>Why titles were spared</h3>
          <dl className="sim-delta">
            {simulation.protected_by.map((g) => (
              <div key={g.gate}>
                <dt>{GATE_META[g.gate]?.label ?? titleCase(g.gate)}</dt>
                <dd>{count(g.count)}</dd>
              </div>
            ))}
          </dl>
        </>
      )}

      <dl className="sim-delta">
        <div>
          <dt>Newly condemned</dt>
          <dd className={simulation.newly_condemned > 0 ? "danger" : ""}>
            +{count(simulation.newly_condemned)}
          </dd>
        </div>
        <div>
          <dt>No longer condemned</dt>
          <dd>−{count(simulation.no_longer_condemned)}</dd>
        </div>
        <div>
          <dt>Spared by a protection</dt>
          <dd>{count(simulation.protected)}</dd>
        </div>
        <div>
          <dt>Not judged</dt>
          <dd>{count(simulation.abstained)}</dd>
        </div>
      </dl>

      {pace && (
        <p className="help">
          Your pace: at most {count(pace.max_items_per_run)} titles /{" "}
          {bytes(pace.max_bytes_per_run)} per run, and nothing is removed until it has waited out
          the {pace.grace_days}-day grace period.
        </p>
      )}

      <p className="blurb">
        The delta is the number that matters before saving: not the total, but what changes
        relative to the list you have already reviewed.
      </p>
    </div>
  );
}

// ---------------------------------------------------------------------------
// The workspace.
// ---------------------------------------------------------------------------

const SECTIONS = [
  { id: "flags", label: "What flags a title" },
  { id: "kept", label: "What's always kept" },
  { id: "pace", label: "Pace and limits" },
  { id: "deletion", label: "Deletion" },
] as const;
export type PolicySectionId = (typeof SECTIONS)[number]["id"];
type SectionId = PolicySectionId;

export function PolicyEditor({
  focus,
}: {
  /** A cross-page jump target ("Turn it on in Policy → Deletion" lands on the Deletion
   *  section). The nonce makes each jump fire once, however often the caller re-renders. */
  focus?: { section: PolicySectionId; nonce: number } | null;
}) {
  const queryClient = useQueryClient();
  // Movies and TV are tuned separately -- keep-last-N seasons and season rank only make
  // sense for TV -- so this toggle picks which policy you are editing.
  const [mediaType, setMediaType] = useState<"movie" | "tv">("movie");
  const { data: saved } = useQuery({
    queryKey: ["policy", mediaType],
    queryFn: () => api.policy(mediaType),
  });

  const [draft, setDraft] = useState<PolicyBody | null>(null);

  // Seed the draft once the saved policy arrives, and re-seed when it is for a different media
  // type (the toggle changed). The editor must open on what is actually in force.
  useEffect(() => {
    if (saved && (draft === null || draft.media_type !== saved.body.media_type)) {
      setDraft(saved.body);
    }
  }, [saved, draft]);

  // Pace and limits: a SEPARATE draft with a separate save. Un-hashed on the server, so
  // changing a cap never voids a pending approval -- and deliberately media-type
  // independent, so the Movies/TV toggle never re-seeds it.
  const { data: savedPace } = useQuery({ queryKey: ["profile"], queryFn: api.profile });
  const [pace, setPace] = useState<ProfileSettings | null>(null);
  // Caps staged by a preset clicked before the profile query resolved. Held here and
  // folded in the moment the profile arrives, so "staged, not saved" is true for BOTH
  // halves of a preset; silently dropping the caps half would let the banner overclaim.
  const [pendingCaps, setPendingCaps] = useState<PresetCaps | null>(null);
  useEffect(() => {
    if (savedPace && pace === null) {
      setPace(pendingCaps ? { ...savedPace, ...pendingCaps } : savedPace);
      if (pendingCaps) setPendingCaps(null);
    }
  }, [savedPace, pace, pendingCaps]);
  const savePace = useMutation({
    mutationFn: (s: ProfileSettings) => api.saveProfile(s),
    onSuccess: (s) => {
      setPace(s);
      void queryClient.invalidateQueries({ queryKey: ["profile"] });
      // Reap's read-only grace countdown shows grace_days; keep it in step.
      void queryClient.invalidateQueries({ queryKey: ["grace"] });
    },
  });
  const paceDirty = useMemo(
    () => pace !== null && savedPace !== undefined && JSON.stringify(pace) !== JSON.stringify(savedPace),
    [pace, savedPace],
  );

  // Debounce the draft the simulator/validator run against, so dragging a slider fires one
  // request when you stop -- not one per pixel. Combined with keepPreviousData below, this is
  // what stops the outcome box flickering while you adjust a weight.
  const [debounced, setDebounced] = useState<PolicyBody | null>(null);
  useEffect(() => {
    const id = setTimeout(() => setDebounced(draft), 250);
    return () => clearTimeout(id);
  }, [draft]);

  const { data: simulation, error: simError } = useQuery({
    queryKey: ["simulate", debounced],
    queryFn: () => api.simulate(debounced!),
    enabled: debounced !== null,
    placeholderData: keepPreviousData, // keep the last result visible while refetching
  });

  // validatePolicy 422s when the policy is *provably* invalid (e.g. a dormancy floor under a
  // year); that error is what "you can't save this" means, and it is shown near the controls,
  // not dressed up as a simulation result.
  const { data: validation, error: invalidError } = useQuery({
    queryKey: ["validate", debounced],
    queryFn: () => api.validatePolicy(debounced!),
    enabled: debounced !== null,
    placeholderData: keepPreviousData,
    retry: false,
  });

  // A background scan, so the "Scan now" button in the stale notice actually does something.
  const { data: scanState } = useQuery({
    queryKey: ["scanStatus"],
    queryFn: api.scanStatus,
    refetchInterval: (query) => (query.state.data?.running ? 1000 : false),
  });
  const scanning = scanState?.running ?? false;
  // A ref, not useMemo: this is persistent mutable storage across renders, and useMemo is
  // only a performance hint React may discard -- if it did, the running->stopped transition
  // that refreshes the simulator would be missed. Matches ScanBar/SetupWizard.
  const wasScanning = useRef(false);
  useEffect(() => {
    // When a scan finishes, the stored scores are fresh -- re-simulate.
    if (wasScanning.current && !scanning) {
      void queryClient.invalidateQueries({ queryKey: ["simulate"] });
      void queryClient.invalidateQueries({ queryKey: ["snapshot"] });
    }
    wasScanning.current = scanning;
  }, [scanning, queryClient]);

  // A mutation, not a fire-and-forget async onClick: a "Scan now" that fails must say so
  // in the stale notice, or the button appears to do nothing at all.
  const startScan = useMutation({
    mutationFn: () => api.startScan(),
    onSuccess: (started) => queryClient.setQueryData(["scanStatus"], started),
  });

  const save = useMutation({
    mutationFn: (body: PolicyBody) => api.savePolicy(body),
    onSuccess: (policy: Policy) => {
      // Key the cache write by the media type the SERVER saved, not whichever tab is
      // showing when the response lands: a mid-flight Movies/TV toggle must not write
      // one type's policy into the other type's cache.
      const savedType = policy.body.media_type === "tv" ? "tv" : "movie";
      queryClient.setQueryData(["policy", savedType], policy);
      // Re-seed the draft from the server's response so the dirty check compares
      // canonical forms. The server can order fields differently from the draft the
      // save was built from, and a raw JSON.stringify comparison would then read
      // "unsaved changes" forever.
      setDraft((cur) =>
        cur && cur.media_type === policy.body.media_type ? policy.body : cur,
      );
      void queryClient.invalidateQueries({ queryKey: ["policy", savedType] });
      // Apply the saved policy to the review queue by re-scanning in the background. The
      // queue and the simulator read the last snapshot's stored verdicts, which were
      // produced by the OLD policy; a rescan re-scores the library under the new one, and
      // the running->stopped effect above refreshes the simulator and queue when it lands.
      // Idempotent server-side: if a scan is already running this just follows it.
      startScan.mutate();
    },
  });

  const dirty = useMemo(
    () => draft !== null && saved !== undefined && JSON.stringify(draft) !== JSON.stringify(saved.body),
    [draft, saved],
  );

  // The preset that was just applied, so the band can say "staged, not saved" until both
  // saves are clean again (or the drafts are discarded).
  const [staged, setStaged] = useState<PresetId | null>(null);
  useEffect(() => {
    if (staged !== null && !dirty && !paceDirty) setStaged(null);
  }, [staged, dirty, paceDirty]);

  // The Movies/TV switch the owner asked for while the draft still holds unsaved edits.
  // Switching re-seeds the draft from the other saved policy, which would silently throw
  // those edits away -- so it waits here for the same two-step confirm the rest of the
  // app uses (never a native confirm()).
  const [pendingSwitch, setPendingSwitch] = useState<"movie" | "tv" | null>(null);

  // Section jump targets for the rail. Memoized (the refs themselves are stable) so the
  // cross-page-jump effect below can depend on the record without refiring every render.
  const flagsRef = useRef<HTMLHeadingElement>(null);
  const keptRef = useRef<HTMLHeadingElement>(null);
  const paceRef = useRef<HTMLHeadingElement>(null);
  const deletionRef = useRef<HTMLHeadingElement>(null);
  const sectionRefs: Record<SectionId, React.RefObject<HTMLHeadingElement | null>> = useMemo(
    () => ({ flags: flagsRef, kept: keptRef, pace: paceRef, deletion: deletionRef }),
    [],
  );
  const [activeSection, setActiveSection] = useState<SectionId>("flags");

  // A cross-page jump lands on a specific section. The editor may still be loading when
  // the jump arrives (the headings do not exist until the draft renders), so the draft is
  // a dependency: once it loads, this refires and consumes the nonce exactly once.
  const handledFocus = useRef(0);
  useEffect(() => {
    if (!focus || draft === null || focus.nonce === handledFocus.current) return;
    const target = sectionRefs[focus.section]?.current;
    if (!target) return;
    handledFocus.current = focus.nonce;
    target.scrollIntoView({ block: "start" });
    setActiveSection(focus.section);
  }, [focus, sectionRefs, draft]);

  if (!draft) return <p className="muted">Loading…</p>;

  const update = (patch: Partial<PolicyBody>) => setDraft({ ...draft, ...patch });
  const updatePace = (patch: Partial<ProfileSettings>) =>
    setPace(pace === null ? null : { ...pace, ...patch });
  const totalWeight = draft.signals.reduce((sum, s) => sum + s.weight, 0);
  // Only a 422 is the server refusing the POLICY ("you can't save this as-is"). Anything
  // else (a timeout, a 500) means the CHECK itself failed, which must not be dressed up
  // as a policy error nor lock Save: the server re-validates on save regardless.
  const invalidMessage =
    invalidError instanceof ApiError && invalidError.status === 422
      ? invalidError.message
      : null;
  const validatorDown = invalidError !== null && invalidMessage === null;

  const switchMediaType = (next: "movie" | "tv") => {
    if (next === mediaType) return;
    if (dirty) {
      setPendingSwitch(next);
    } else {
      setPendingSwitch(null);
      setMediaType(next);
    }
  };

  const kind = mediaType === "tv" ? "TV" : "movie";
  const otherKind = mediaType === "tv" ? "movie" : "TV";
  const preset = activePreset(draft);
  const presetHelp =
    PRESETS.find((p) => p.id === preset)?.help ?? "Custom: your own tuning, not a preset.";

  const applyPreset = (p: (typeof PRESETS)[number]) => {
    const mix = DEFAULT_WEIGHTS[mediaType];
    update({
      condemn_at: p.condemn_at,
      signals: draft.signals.map((s) => ({ ...s, weight: mix[s.signal] ?? 0 })),
    });
    // The pace draft may not exist yet (the profile query can still be loading). Buffer
    // the caps so they land when it does; staging only the policy half while the banner
    // claims both would be a lie.
    if (pace === null) setPendingCaps(p.caps);
    else updatePace(p.caps);
    setStaged(p.id);
  };

  // The one-sentence read of the whole policy, from the live drafts, so it can never
  // disagree with the controls below it.
  const dormancy = draft.gates.find((g) => g.gate === "min_dormancy" && g.enabled);
  const popularity = draft.gates.find((g) => g.gate === "server_popularity" && g.enabled);
  const keepClauses = [
    popularity ? `watched by ${popularity.threshold || 1}+ people` : null,
    dormancy ? `played in the last ${humanDays(dormancy.threshold)}` : null,
  ].filter(Boolean);
  const paceClause = pace
    ? `removes at most ${count(pace.max_items_per_run)} titles or ${bytes(pace.max_bytes_per_run)} per run`
    : "removes only within your caps";

  return (
    <section className="editor">
      <div className="editor-controls">
        <div className="policy-head">
          <h2>Policy</h2>
          <div className="segmented" role="group" aria-label="Which policy">
            <button
              className={mediaType === "movie" ? "seg active" : "seg"}
              onClick={() => switchMediaType("movie")}
            >
              Movies
            </button>
            <button
              className={mediaType === "tv" ? "seg active" : "seg"}
              onClick={() => switchMediaType("tv")}
            >
              TV
            </button>
          </div>
        </div>
        {pendingSwitch !== null && (
          <div className="notice notice-warn">
            You have unsaved {kind} policy changes. Switching to{" "}
            {pendingSwitch === "tv" ? "TV" : "Movies"} discards them.{" "}
            <button
              type="button"
              className="sm danger"
              onClick={() => {
                setPendingSwitch(null);
                setMediaType(pendingSwitch);
              }}
            >
              Discard and switch
            </button>{" "}
            <button type="button" className="sm ghost" onClick={() => setPendingSwitch(null)}>
              Keep editing
            </button>
          </div>
        )}
        <p className="blurb">
          {mediaType === "tv"
            ? "How Reaper judges TV: seasons, not whole shows. Tuned separately from movies."
            : "How Reaper judges your movies. TV is tuned separately, with the toggle."}
        </p>

        <nav className="settings-nav policy-rail" aria-label="Policy sections">
          {SECTIONS.map((s) => (
            <button
              key={s.id}
              className={activeSection === s.id ? "settings-tab active" : "settings-tab"}
              onClick={() => {
                // Instant, not smooth: smooth scrolling silently no-ops in some
                // environments, and a jump that always lands beats an animation that
                // sometimes doesn't happen at all.
                sectionRefs[s.id].current?.scrollIntoView({ block: "start" });
                setActiveSection(s.id);
              }}
            >
              {s.label}
            </button>
          ))}
        </nav>

        {/* The intent band: the whole policy in one sentence, and three starting points. */}
        <div className="intent-band">
          <p className="intent-summary">
            {mediaType === "tv" ? (
              <>
                Right now Reaper flags a <strong>season</strong> once it scores{" "}
                <strong>{draft.condemn_at} / 100</strong>, always keeps the{" "}
                <strong>
                  newest {draft.keep_last_seasons}{" "}
                  {draft.keep_last_seasons === 1 ? "season" : "seasons"}
                </strong>{" "}
                of a show and anyone's mid-binge, and {paceClause}.
              </>
            ) : (
              <>
                Right now Reaper flags a movie once it scores{" "}
                <strong>{draft.condemn_at} / 100</strong>
                {keepClauses.length > 0 && (
                  <>
                    , keeps anything <strong>{keepClauses.join(" or ")}</strong>
                  </>
                )}
                , and {paceClause}.
              </>
            )}
          </p>
          <div className="intent-row">
            <span className="muted intent-label">Starting point</span>
            <div className="segmented" role="group" aria-label="Preset">
              {PRESETS.map((p) => (
                <button
                  key={p.id}
                  type="button"
                  className={preset === p.id ? "seg active" : "seg"}
                  onClick={() => applyPreset(p)}
                >
                  {p.label}
                </button>
              ))}
            </div>
          </div>
          <p className="help">{presetHelp}</p>
          {staged !== null && (
            <p className="help">
              <strong>Staged, not saved.</strong> Review the changes below, then Save {kind}{" "}
              policy and Save pace &amp; limits.
            </p>
          )}
        </div>

        {/* ------------------------------------------------------------------ */}
        <h3 ref={sectionRefs.flags} className="policy-section">
          What flags a title
        </h3>
        <p className="blurb">
          The reasons to believe nobody will watch it again. Nothing here removes a title on its
          own. The protections below can always overrule the score.
        </p>

        <label className="field">
          <span className="field-label">
            Put a title on the list once it scores
            <strong>{draft.condemn_at} / 100</strong>
          </span>
          <input
            type="range"
            min={1}
            max={100}
            value={draft.condemn_at}
            onChange={(e) => update({ condemn_at: Number(e.target.value) })}
          />
          <span className="help">
            The higher you set this, the more sure Reaper has to be before it flags a title.
            Protections below still win. This only decides among titles nothing is keeping.
          </span>
        </label>

        <label className="field">
          <span className="field-label">
            Only judge a title Reaper can mostly see
            <strong>{Math.round(draft.coverage_floor_bp / 100)}%</strong>
          </span>
          <input
            type="range"
            min={0}
            max={10000}
            step={500}
            value={draft.coverage_floor_bp}
            onChange={(e) => update({ coverage_floor_bp: Number(e.target.value) })}
          />
          <span className="help">
            How much of the evidence Reaper needs before it will judge a title at all. Below
            this it stays quiet rather than deciding on fragments.
          </span>
        </label>

        <ul className="rule-list">
          {draft.signals.map((signal, i) => (
            <SignalRow
              key={signal.signal}
              signal={signal}
              totalWeight={totalWeight}
              onChange={(s) => {
                const signals = [...draft.signals];
                signals[i] = s;
                update({ signals });
              }}
            />
          ))}
        </ul>

        <RemoveRulesEditor
          condemn={draft.custom_condemn}
          onCondemn={(custom_condemn) => update({ custom_condemn })}
        />

        <div className="policy-divider" />

        {/* ------------------------------------------------------------------ */}
        <h3 ref={sectionRefs.kept} className="policy-section">
          What's always kept
        </h3>
        <p className="blurb">
          Protections. Any one of these keeps a title no matter how it scored, and every one can
          only ever <em>keep</em> a file, never mark one for removal.
        </p>

        <ul className="rule-list">
          {draft.gates.map((gate, i) => {
            const setGate = (g: GateSetting) => {
              const gates = [...draft.gates];
              gates[i] = g;
              update({ gates });
            };
            // "Spare titles you've tagged" moves down into its own tags card below, where
            // the toggle and the tags it depends on sit together -- so it is skipped here.
            if (gate.gate === "whitelisted") return null;
            // "Keep well-rated titles" carries a whole set of per-source bars, so it gets
            // its own row rather than the single-threshold GateRow.
            if (gate.gate === "rating_floor") {
              return (
                <RatingFloorRow
                  key={gate.gate}
                  gate={gate}
                  rules={draft.keep_rating_rules}
                  match={draft.keep_rating_match}
                  mediaType={mediaType}
                  onGate={setGate}
                  onRules={(keep_rating_rules) => update({ keep_rating_rules })}
                  onMatch={(keep_rating_match) => update({ keep_rating_match })}
                />
              );
            }
            return <GateRow key={gate.gate} gate={gate} onChange={setGate} />;
          })}
        </ul>

        {(() => {
          const i = draft.gates.findIndex((g) => g.gate === "whitelisted");
          const whitelist = i >= 0 ? draft.gates[i] : undefined;
          if (!whitelist) return null;
          const setWhitelist = (enabled: boolean) => {
            const gates = [...draft.gates];
            gates[i] = { ...whitelist, enabled };
            update({ gates });
          };
          return (
            <div className="rules-card">
              <h3>Tags that spare a title</h3>
              <label className="toggle rule-toggle">
                <Switch checked={whitelist.enabled} onChange={setWhitelist} />
                <span className="rule-name">Spare titles you've tagged</span>
              </label>
              <p className="help rule-help">
                A title carrying one of these tags in Sonarr/Radarr is kept, whatever it scored. A
                ‘Never Reap’ Plex collection is honoured too.
              </p>
              {whitelist.enabled && (
                <>
                  <KeepTagsEditor
                    tags={draft.keep_tags}
                    match={draft.keep_tags_match}
                    onTags={(keep_tags) => update({ keep_tags })}
                    onMatch={(keep_tags_match) => update({ keep_tags_match })}
                  />
                  <p className="help">Type each tag exactly as it appears in Sonarr or Radarr.</p>
                </>
              )}
            </div>
          );
        })()}

        {mediaType === "tv" && (
          <div className="season-card">
            <h3>TV season protection</h3>
            <label className="field">
              <span className="field-label">
                <span>
                  Always keep the newest{" "}
                  <input
                    type="number"
                    min={0}
                    className="inline-number"
                    value={draft.keep_last_seasons}
                    onChange={(e) => update({ keep_last_seasons: Math.max(0, Number(e.target.value)) })}
                  />{" "}
                  {draft.keep_last_seasons === 1 ? "season" : "seasons"} of a show
                </span>
              </span>
              <span className="help">
                The most recent seasons of every show are kept outright, whatever they score.
                There is no upper limit.
              </span>
              <SeasonAdvisory keepLast={draft.keep_last_seasons} />
            </label>

            <label className="field">
              <span className="field-label">Apply that to</span>
              <div className="segmented" role="group" aria-label="Keep-last scope">
                <button
                  type="button"
                  className={draft.keep_last_scope === "all" ? "seg active" : "seg"}
                  onClick={() => update({ keep_last_scope: "all" })}
                >
                  All shows
                </button>
                <button
                  type="button"
                  className={draft.keep_last_scope === "requested" ? "seg active" : "seg"}
                  onClick={() => update({ keep_last_scope: "requested" })}
                >
                  Requested only
                </button>
              </div>
              <span className="help">
                “Requested only” lets older seasons of shows nobody asked for be removed, while
                still keeping the recent seasons of requested shows. When Reaper can't tell whether
                a show was requested, it keeps the seasons to be safe.
              </span>
            </label>

            <label className="toggle">
              <Switch
                checked={draft.keep_first_season}
                onChange={(keep_first_season) => update({ keep_first_season })}
              />
              <span>Always keep a show's first season, so a new viewer can still start it</span>
            </label>

            <label className="toggle">
              <Switch
                checked={draft.keep_in_progress}
                onChange={(keep_in_progress) => update({ keep_in_progress })}
              />
              <span>Keep seasons someone is partway through</span>
            </label>
            <p className="help">
              Reaper holds the season a viewer is midway into, plus the next one once they finish
              it. Turn this off and being mid-show protects nothing.
            </p>

            <label className="field">
              <span className="field-label">
                <span>
                  Stop holding someone's place after{" "}
                  <input
                    type="number"
                    min={0}
                    className="inline-number"
                    disabled={!draft.keep_in_progress}
                    value={draft.in_progress_hold_days}
                    onChange={(e) =>
                      update({ in_progress_hold_days: Math.max(0, Number(e.target.value)) })
                    }
                  />{" "}
                  days without watching
                </span>
              </span>
              <span className="help">
                If someone has not watched any of the show in this many days, Reaper treats the
                show as abandoned by them and lets go of their place. Set to 0 to hold it forever.
                When Reaper can't tell when they last watched, it keeps holding.
              </span>
            </label>

            <label className="field">
              <span className="field-label">
                <span>
                  When someone's mid-binge, also keep{" "}
                  <input
                    type="number"
                    min={0}
                    className="inline-number"
                    disabled={!draft.keep_in_progress}
                    value={draft.season_lookahead}
                    onChange={(e) =>
                      update({ season_lookahead: Math.max(0, Number(e.target.value)) })
                    }
                  />{" "}
                  season{draft.season_lookahead === 1 ? "" : "s"} ahead
                </span>
              </span>
              <span className="help">
                Set this above 0 to also keep the seasons just ahead of where each viewer is.
              </span>
            </label>

            <label className="toggle">
              <Switch
                checked={draft.keep_specials}
                onChange={(keep_specials) => update({ keep_specials })}
              />
              <span>Never remove specials</span>
            </label>
            <p className="help">
              On: specials (Season 0) are always kept. Off: specials are judged like any other
              season. Either way, specials never count toward the newest seasons you keep.
            </p>

            <label className="toggle">
              <Switch
                checked={draft.flag_keep_conflicts}
                onChange={(flag_keep_conflicts) => update({ flag_keep_conflicts })}
              />
              <span>Ask me first when a removal looks unusual</span>
            </label>
            <p className="help">
              When a season your rule would remove was watched by more people than a season it
              keeps, Reaper marks it "Needs a look" and waits for you. Turn this off and Reaper
              follows your keep rule without asking.
            </p>
          </div>
        )}

        <KeepRulesEditor
          conditions={draft.protect_conditions}
          keeps={draft.graded_keeps}
          // Only protections that are ON hide their field from custom keeps: a disabled
          // gate protects nothing, so its field must stay authorable here.
          gateIds={draft.gates.filter((g) => g.enabled).map((g) => g.gate)}
          onConditions={(protect_conditions) => update({ protect_conditions })}
          onKeeps={(graded_keeps) => update({ graded_keeps })}
        />

        {/* A validation failure is an ERROR (red): this policy cannot be saved as-is. */}
        {invalidMessage && (
          <p className="notice notice-error">
            <strong>Can't save this:</strong> {invalidMessage}
          </p>
        )}
        {/* The check itself failing is AMBER, and it does not lock Save: the server
            checks the policy again on save either way. */}
        {validatorDown && (
          <p className="notice notice-warn">
            Couldn't check this draft just now, so any problem with it can't be shown here.
            You can still save: the server checks it again when you do.
          </p>
        )}
        {/* A warning is AMBER: the policy is legal, but probably not what you meant. */}
        {validation?.warnings.map((w) => (
          <p key={w.field} className={`notice ${w.severity === "danger" ? "notice-error" : "notice-warn"}`}>
            {w.message}
          </p>
        ))}

        <div className="editor-actions">
          <button
            className="primary"
            disabled={!dirty || Boolean(invalidMessage) || save.isPending}
            onClick={() => save.mutate(draft)}
          >
            {save.isPending ? "Saving…" : dirty ? `Save ${kind} policy` : "Saved"}
          </button>
          {dirty && (
            <button className="ghost" onClick={() => setDraft(saved?.body ?? null)}>
              Discard
            </button>
          )}
          {save.error && <p className="notice notice-error">{save.error.message}</p>}
        </div>

        <p className="hash">
          {validation && (
            <>
              {kind} policy <code>{validation.policy_hash.slice(0, 12)}</code>
            </>
          )}
        </p>

        <p className="blurb">
          Saves only your {kind} policy. The {otherKind} one is untouched. Saving does not arm
          anything, and it takes effect on the next scan.
        </p>

        <div className="policy-divider" />

        {/* ------------------------------------------------------------------ */}
        <h3 ref={sectionRefs.pace} className="policy-section">
          Pace and limits
        </h3>
        <p className="blurb">
          How much a single reap may do, and how long the grace countdown lasts. One setting for
          all of Reaper, movies and TV alike.
        </p>

        {pace === null ? (
          <p className="muted">Loading…</p>
        ) : (
          <>
            <label className="toggle pace-approval">
              <Switch
                checked={pace.require_approval}
                onChange={(require_approval) => updatePace({ require_approval })}
              />
              <span>Ask me before every run deletes anything</span>
            </label>
            <div className="caps-grid">
              <label>
                <span>Most titles per run</span>
                <input
                  type="number"
                  min={1}
                  value={pace.max_items_per_run}
                  onChange={(e) => updatePace({ max_items_per_run: Number(e.target.value) || 1 })}
                />
                <span className="help">The most titles a single run will delete.</span>
              </label>
              <label>
                <span>Most space per run</span>
                <QuantityInput
                  value={pace.max_bytes_per_run}
                  units={SIZE_UNITS}
                  onChange={(v) => updatePace({ max_bytes_per_run: v })}
                />
                <span className="help">The most disk one run can free.</span>
              </label>
              <label>
                <span>Most titles per month</span>
                <input
                  type="number"
                  min={1}
                  value={pace.max_items_per_30d}
                  onChange={(e) => updatePace({ max_items_per_30d: Number(e.target.value) || 1 })}
                />
                <span className="help">A rolling limit over the last 30 days.</span>
              </label>
              <label>
                <span>Most space per month</span>
                <QuantityInput
                  value={pace.max_bytes_per_30d}
                  units={SIZE_UNITS}
                  onChange={(v) => updatePace({ max_bytes_per_30d: v })}
                />
                <span className="help">A rolling disk limit over the last 30 days.</span>
              </label>
              <label>
                <span>Grace period</span>
                <QuantityInput
                  value={pace.grace_days}
                  units={TIME_UNITS}
                  min={7}
                  onChange={(v) => updatePace({ grace_days: v })}
                />
                <span className="help">
                  How long a title stays on the list, where you can still rescue it, before
                  Reaper can remove it.
                </span>
              </label>
            </div>
            <p className="help">
              A run over a cap stops itself and removes nothing. It never quietly deletes just
              the part that fits.
            </p>
            <div className="editor-actions">
              <button
                className="primary"
                disabled={!paceDirty || savePace.isPending}
                onClick={() => savePace.mutate(pace)}
              >
                {savePace.isPending ? "Saving…" : paceDirty ? "Save pace & limits" : "Saved"}
              </button>
              <span className="muted pace-note">
                Takes effect immediately. Changing a limit never affects a run you've already
                approved.
              </span>
            </div>
            {savePace.error && <p className="notice notice-error">{savePace.error.message}</p>}
          </>
        )}

        <div className="policy-divider" />

        {/* ------------------------------------------------------------------ */}
        <h3 ref={sectionRefs.deletion} className="policy-section">
          Deletion
        </h3>
        <p className="blurb">
          Whether Reaper is allowed to remove anything at all. One switch for all of Reaper,
          movies and TV alike. Turning it on always asks for the admin password. Turning it off
          never does.
        </p>
        <DeletionToggle />
      </div>

      <div className="editor-sim">
        <h2>What this would do</h2>
        <p className="blurb">
          Re-decided against your last scan, with zero API calls. Nothing here touches Sonarr,
          Radarr or Tautulli.
        </p>
        {invalidMessage ? (
          <p className="muted">Fix the policy on the left, then this updates.</p>
        ) : simError ? (
          /* Checked BEFORE simulation: keepPreviousData can leave the previous draft's
             numbers in `simulation` when a refetch fails, and a stale count shown as
             current is exactly what this column must never do. */
          <div className="sim sim-info">
            <h3>Can't show what this would do</h3>
            <p>
              The simulator request failed, so there are no numbers to show. Nothing about
              your library or your saved policy has changed. Adjust any control to try
              again.
            </p>
            <p className="error">{simError.message}</p>
          </div>
        ) : simulation ? (
          simulation.exact ? (
            <Outcome simulation={simulation} threshold={draft.condemn_at} pace={pace} />
          ) : (
            <StaleNotice
              scanning={scanning}
              starting={startScan.isPending}
              startError={startScan.error ? startScan.error.message : null}
              onScan={() => startScan.mutate()}
              percent={scanState?.percent ?? 0}
              detail={scanState?.detail ?? ""}
            />
          )
        ) : (
          <p className="muted">Working…</p>
        )}
      </div>
    </section>
  );
}
