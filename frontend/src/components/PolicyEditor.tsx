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
// The single most important behavior in this file is what happens when that stops
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
import { useEffect, useId, useMemo, useRef, useState } from "react";
import {
  api,
  ApiError,
  type Condition,
  type CustomCondemn,
  type GateSetting,
  type GradedKeep,
  type Policy,
  type PolicyBody,
  type PolicyWarning,
  type ProfileSettings,
  type RatingRule,
  type RatingSource,
  type SignalSetting,
  type VocabField,
} from "../api";
import { useDocs } from "../docs/DocsContext";
import { bytes, count } from "../format";
import { DeletionToggle } from "./DeletionToggle";
import { GATE_META, SIGNAL_META, titleCase } from "./policyMeta";
import { Outcome, StaleNotice } from "./PolicySimulator";
import { FixedQuantity, QuantityInput, SIZE_UNITS, TIME_UNITS } from "./QuantityInput";
import { Segmented } from "./Segmented";
import { Switch } from "./Switch";

// ---------------------------------------------------------------------------
// Presets: three starting points that stage (never save) the threshold and the
// pace. Weights are RESET to the shipped mix on apply -- a preset is a known
// place to start from, not a tweak -- and the operator's own removal rules are
// rescaled alongside it (rescaleToBudget) so the lane still totals 100. The
// badge only claims a preset while the draft actually matches it.
// ---------------------------------------------------------------------------

type PresetId = "cautious" | "balanced" | "aggressive";

/** The shipped signal mixes (see engine/policy.py defaults). A preset resets to these. */
const DEFAULT_WEIGHTS: Record<"movie" | "tv", Record<string, number>> = {
  movie: { unwatched: 70, few_watchers: 20, low_rating: 10 },
  tv: { unwatched: 60, few_watchers: 15, season_rank: 15, low_rating: 10 },
};

type PresetCaps = Pick<
  ProfileSettings,
  | "max_items_per_run"
  | "max_bytes_per_run"
  | "max_items_per_30d"
  | "max_bytes_per_30d"
  | "grace_days"
  // Every preset promises enforcement ("removes less per run"), so it must also turn the
  // caps ON. Staging the numbers while leaving caps off saved an uncapped profile (B-10).
  | "caps_enabled"
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
      caps_enabled: true,
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
      caps_enabled: true,
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
      caps_enabled: true,
    },
  },
];

/** The removal budget every policy must total, matching the server
 *  (PolicyBody._weights_total_one_hundred). */
const REMOVAL_POINTS = 100;

/** A set of removal weights rescaled so they total exactly REMOVAL_POINTS, using the same
 *  largest-remainder arithmetic the server uses to repair a stored policy
 *  (engine/policy.rebalance). Score-preserving: the score is already
 *  100 * (pressure / total weight), so scaling every weight by one factor cannot move it,
 *  and largest-remainder keeps the rounding under a point.
 *
 *  A preset needs this because the shipped mix alone is already the whole budget, so
 *  without it any rule of the operator's own put the draft over budget and blocked Save
 *  for the pace draft too.
 *
 *  Returns the weights unchanged when there is nothing to scale (no weight at all), which
 *  the budget readout then reports as under budget, as before.
 */
function rescaleToBudget(weights: number[]): number[] {
  const total = weights.reduce((sum, w) => sum + w, 0);
  if (total <= 0) return weights;
  const exact = weights.map((w) => (w * REMOVAL_POINTS) / total);
  const floors = exact.map((x) => Math.floor(x));
  const spare = REMOVAL_POINTS - floors.reduce((sum, f) => sum + f, 0);
  // Largest fractional remainder first; ties keep their original order, as the server's
  // stable sort does.
  const order = floors
    .map((_, i) => i)
    .sort((a, b) => exact[b]! - floors[b]! - (exact[a]! - floors[a]!));
  for (const i of order.slice(0, spare)) floors[i]! += 1;
  return floors;
}

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
        <div className="tag-match">
          <span className="muted">Keep a title with</span>
          <Segmented
            value={match}
            onChange={onMatch}
            label="How many of these tags a title needs"
            options={[
              ["any", "any of these tags"],
              ["all", "all of these tags"],
            ]}
          />
        </div>
      )}
      {tags.length === 0 && <p className="help">No tags: this protection keeps nothing.</p>}
    </div>
  );
}

/** Inline warnings for one control group, rendered beside the control that fixes them.
 *  Renders nothing when the group has nothing to say. */
function WarnBlock({ warnings }: { warnings: PolicyWarning[] }) {
  if (warnings.length === 0) return null;
  return (
    <>
      {warnings.map((w) => (
        <p
          key={`${w.field}:${w.message}`}
          className={`notice notice-inline ${w.severity === "danger" ? "notice-error" : "notice-warn"}`}
        >
          {w.message}
        </p>
      ))}
    </>
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
            min={5}
            ariaLabel={`${meta.label} threshold`}
            onChange={(v) => onChange({ ...gate, threshold: v })}
          />
        </div>
      )}
      {gate.enabled && meta.unit === "people" && (
        <div className="rule-control">
          <span>at least</span>
          <FixedQuantity
            value={gate.threshold || 1}
            suffix={(gate.threshold || 1) === 1 ? "person" : "people"}
            min={1}
            width="narrow"
            ariaLabel={`${meta.label} threshold`}
            onChange={(v) => onChange({ ...gate, threshold: v || 1 })}
          />
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

/** One editable bar, as a row of the shared aligned table: the source name in the common
 *  column, then its threshold (and vote floor, where the source counts votes) in the
 *  app's one quantity control. */
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
    <div className="bar-line">
      <span className="bar-src">{meta.label}</span>
      <span className="bar-set">
        <span>at least</span>
        {meta.scale === "ten" ? (
          <>
            <FixedQuantity
              value={(rule.floor / 10).toFixed(1)}
              suffix="/ 10"
              min={0}
              max={10}
              step={0.1}
              width="narrow"
              ariaLabel={`${meta.label} score out of 10`}
              onChange={(v) => onChange({ ...rule, floor: Math.round(v * 10) })}
            />
            {meta.votes && (
              <>
                <span>from</span>
                <FixedQuantity
                  value={rule.min_votes}
                  suffix="+ votes"
                  min={0}
                  step={100}
                  ariaLabel={`${meta.label} vote floor`}
                  onChange={(v) => onChange({ ...rule, min_votes: Math.max(0, v) })}
                />
              </>
            )}
          </>
        ) : (
          <FixedQuantity
            value={rule.floor}
            suffix="%"
            min={0}
            max={100}
            step={1}
            width="narrow"
            ariaLabel={`${meta.label} percentage`}
            onChange={(v) => onChange({ ...rule, floor: v })}
          />
        )}
      </span>
      <button
        type="button"
        className="bar-x"
        onClick={onRemove}
        aria-label={`Remove the ${meta.label} bar`}
      >
        ×
      </button>
    </div>
  );
}

/** "Keep well-rated titles" as a card: the switch in the card header, a bar per source in
 *  one aligned table, an add-source picker beside the any/all choice, and a plain-English
 *  summary. Any warning about this protection renders here, beside its fix, not at the
 *  bottom of the page. */
function RatingFloorRow({
  gate,
  rules,
  match,
  mediaType,
  warnings,
  onGate,
  onRules,
  onMatch,
}: {
  gate: GateSetting;
  rules: RatingRule[];
  match: "any" | "all";
  mediaType: "movie" | "tv";
  /** Server-side policy warnings anchored to the rating rules (field keep_rating_rules). */
  warnings: { message: string; severity: string }[];
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
      ? "Nothing is kept yet: add a rating source to set the score a title must clear to stay."
      : `Keep a title rated at least ${rules.map(describeBar).join(joiner)}.`;

  return (
    <div className="rules-card">
      <label className="toggle card-head">
        <Switch checked={gate.enabled} onChange={(enabled) => onGate({ ...gate, enabled })} />
        <span className="rule-name">Keep well-rated titles</span>
      </label>
      <p className="help rule-help">
        A title that clears {match === "any" ? "any one" : "all"} of these bars is kept, whatever it
        scored.
      </p>
      {gate.enabled && (
        <>
          {rules.length > 0 && (
            <div className="bar-table">
              {rules.map((rule, i) => (
                <RatingBarRow
                  key={rule.source}
                  rule={rule}
                  onChange={(r) => onRules(rules.map((x, j) => (j === i ? r : x)))}
                  onRemove={() => onRules(rules.filter((_, j) => j !== i))}
                />
              ))}
            </div>
          )}
          <div className="bar-foot">
            {available.length > 0 ? (
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
            ) : (
              <span />
            )}
            {rules.length >= 2 && (
              <Segmented
                value={match}
                onChange={onMatch}
                label="How many bars a title must clear"
                options={[
                  ["any", "any one bar keeps it"],
                  ["all", "every bar must clear"],
                ]}
              />
            )}
          </div>
          {warnings.length > 0 ? (
            warnings.map((w) => (
              <p
                key={w.message}
                className={`notice notice-inline ${w.severity === "danger" ? "notice-error" : "notice-warn"}`}
              >
                {w.message}
              </p>
            ))
          ) : (
            <p className="help">{summary}</p>
          )}
          {mediaType === "tv" && (
            <p className="help">
              TV has IMDb, plus any scores Plex carries for the show. Rotten Tomatoes and Metacritic
              are often missing for TV, so those bars just won't match a show.
            </p>
          )}
        </>
      )}
    </div>
  );
}

/** The 100-point removal budget, in the savebar. The whole reason weights can be labeled
 *  as points: the server refuses any policy whose removal weights do not total exactly 100
 *  (PolicyBody._weights_total_one_hundred), so the number on a rule IS what it adds.
 *
 *  Both directions block Save. Over-budget is obvious. Under-budget matters just as much:
 *  the score divides by the total, so 75 points would stretch the lane and put every label
 *  on this page back to lying. Blocking both is what buys the honest labels.
 *
 *  Keeps are deliberately NOT in here. A keep discount is points off the same 0-100 score,
 *  but it is a different lane doing a different job, and folding it in would cap how much
 *  protection an operator can express. Hence "removal points", not "points".
 */
function PointsBudget({ builtIn, yours }: { builtIn: number; yours: number }) {
  const total = builtIn + yours;
  const left = 100 - total;
  const scale = Math.max(total, 100);

  return (
    <span className="budget">
      <span className="budget-meter" aria-hidden="true">
        <i className="budget-built" style={{ width: `${(Math.min(builtIn, 100) / scale) * 100}%` }} />
        <i className="budget-yours" style={{ width: `${(Math.min(yours, 100) / scale) * 100}%` }} />
        {left < 0 && <i className="budget-over" style={{ width: `${(-left / scale) * 100}%` }} />}
        {left > 0 && <i className="budget-free" style={{ width: `${(left / scale) * 100}%` }} />}
      </span>
      <span className="budget-line">
        <span>
          <strong>{total}</strong> of 100 removal points used
        </span>
        {left === 0 ? (
          <span className="muted">
            {pointsSplit(builtIn, yours)}
          </span>
        ) : (
          <span className={left < 0 ? "budget-over-text" : "muted"}>
            {left < 0 ? `${-left} over` : `${left} left to give out`}
          </span>
        )}
      </span>
      {left !== 0 && (
        <span className="notice notice-error budget-notice">
          {left < 0
            ? `Your rules add up to ${total} points. Take ${-left} away before saving.`
            : `Your rules add up to ${total} points. Give out the other ${left} before saving.`}
        </span>
      )}
    </span>
  );
}

/** How the 100 points are split: "70 built in · 30 yours", or "all on built-in signals"
 *  when the operator has written no removal rules. Both arguments are point totals, not
 *  rule counts, so the two numbers always add up to the total shown beside them. */
function pointsSplit(builtIn: number, yours: number): string {
  return yours > 0 ? `${builtIn} built in · ${yours} yours` : "all on built-in signals";
}

/** One signal: a plain-English label, its help, a slider, and the flat points it can add.
 *  Removal weights total exactly 100, so the weight IS the number of points, and the row
 *  reads "up to N points" because a signal only pays its full number at the far end of
 *  its range. */
function SignalRow({
  signal,
  onChange,
}: {
  signal: SignalSetting;
  onChange: (s: SignalSetting) => void;
}) {
  const meta = SIGNAL_META[signal.signal] ?? { label: titleCase(signal.signal), help: "" };

  return (
    <li className="rule-row">
      <div className="rule-name-row">
        <span className="rule-name">{meta.label}</span>
        <span className="rule-strength">
          {signal.weight === 0 ? (
            <span className="muted">off</span>
          ) : (
            // Points, not a share, and no second number beside it: removal weights total
            // exactly 100, so the weight IS what it adds. "up to" is not hedging -- a
            // signal ramps, and pays its full number only at the far end of its range.
            <>
              <span className="muted">up to </span>
              <strong>{signal.weight}</strong>
              <span className="muted"> points</span>
            </>
          )}
        </span>
      </div>
      {signal.weight === 0 ? (
        <p className="help rule-help">
          Its points go back into the pot. Give them to another signal before saving.
        </p>
      ) : (
        meta.help && <p className="help rule-help">{meta.help}</p>
      )}
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
  // Trimmed, so a typed space cannot compose a rule that matches everything: "contains"
  // with an empty target is true for every title that has the field at all. The server
  // refuses it too (engine/fields.py Condition._validate_value_type); this keeps the UI
  // from offering it in the first place.
  if (field.type === "text") return raw.trim();
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
  // The listbox and its options need stable ids so the input can point at the option the
  // arrow keys are on; without them, arrowing moves the highlight and announces nothing.
  const listboxId = useId();
  const optionId = (i: number) => `${listboxId}-option-${i}`;

  if (!isText) {
    // A number that has a unit wears it in the box, not in a placeholder that vanishes the
    // moment you type. Fields with no unit (a rank, a count) stay a plain box.
    return field.unit_suffix ? (
      <FixedQuantity
        value={value}
        suffix={field.unit_suffix}
        step={field.type === "rating_tenths" ? 0.1 : 1}
        ariaLabel={`${field.label} value`}
        onChange={(v) => onChange(String(v))}
      />
    ) : (
      <input
        type="number"
        value={value}
        aria-label={`${field.label} value`}
        onChange={(e) => onChange(e.target.value)}
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
        aria-controls={show ? listboxId : undefined}
        aria-activedescendant={show && highlight >= 0 ? optionId(highlight) : undefined}
        aria-label={`${field.label} value`}
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
        <ul className="suggest-pop" role="listbox" id={listboxId}>
          {matches.map((v, i) => (
            <li
              key={v}
              id={optionId(i)}
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
  mediaType,
}: {
  condemn: CustomCondemn[];
  onCondemn: (r: CustomCondemn[]) => void;
  mediaType: "movie" | "tv";
}) {
  // Narrowed to the policy's media type, so a TV-only reason ("the show has ended")
  // is never offered while tuning movies.
  const { data: condemnVocab } = useQuery({
    queryKey: ["vocabulary", "condemn", mediaType],
    queryFn: () => api.vocabulary("condemn", mediaType),
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
              {/* A yes/no rule pays its full number the moment it matches, so it says the
                  number flat. A sliding rule ramps between its floor and its top, so it
                  says "up to". Removal weights total 100, so both are literal points. */}
              <span className="rules-weight-remove">
                {r.kind === "graded" ? `up to +${r.weight} points` : `+${r.weight} points`}
              </span>
              <button className="ghost sm" onClick={() => onCondemn(condemn.filter((_, j) => j !== i))}>
                Remove
              </button>
            </div>
          ))}
        </div>
      )}

      <div className="condition-add">
        <select value={rField} aria-label="Field" onChange={(e) => setRField(e.target.value)}>
          <option value="">when…</option>
          {condemnFields.map((f) => (
            <option key={f.key} value={f.key}>
              {f.label}
            </option>
          ))}
        </select>
        {field && (
          <>
            <select value={rOp} aria-label="Comparison" onChange={(e) => setROp(e.target.value)}>
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
                  <QuantityInput
                    value={rFrom}
                    units={TIME_UNITS}
                    ariaLabel="Starts counting at"
                    onChange={setRFrom}
                  />
                ) : field.type === "bytes" ? (
                  <QuantityInput
                    value={rFrom}
                    units={SIZE_UNITS}
                    ariaLabel="Starts counting at"
                    onChange={setRFrom}
                  />
                ) : (
                  <input
                    type="number"
                    value={rFrom}
                    aria-label="Starts counting at"
                    onChange={(e) => setRFrom(Number(e.target.value) || 0)}
                  />
                )}
                <span className="muted">to</span>
                {field.type === "days" ? (
                  <QuantityInput
                    value={rTo}
                    units={TIME_UNITS}
                    ariaLabel="Full effect at"
                    onChange={setRTo}
                  />
                ) : field.type === "bytes" ? (
                  <QuantityInput
                    value={rTo}
                    units={SIZE_UNITS}
                    ariaLabel="Full effect at"
                    onChange={setRTo}
                  />
                ) : (
                  <input
                    type="number"
                    value={rTo}
                    aria-label="Full effect at"
                    onChange={(e) => setRTo(Number(e.target.value) || 1)}
                  />
                )}
              </span>
            ) : field.type === "bool" ? (
              // Two visible options, so both stay readable at a glance (Segmented.tsx).
              <Segmented
                value={rValue === "false" ? "false" : "true"}
                onChange={setRValue}
                label="Value"
                options={[
                  ["true", "yes"],
                  ["false", "no"],
                ]}
              />
            ) : (
              <SuggestInput field={field} value={rValue} onChange={setRValue} />
            )}
            {/* One control standard: a number with a fixed unit is a FixedQuantity, never
                a bare number box beside loose text. "up to" for a ramp, flat for a yes/no,
                matching how each rule actually pays out. */}
            <label className="inline-weight">
              {isRamp ? "up to" : ""}
              <FixedQuantity
                value={rWeight}
                onChange={setRWeight}
                suffix="points"
                min={0}
                max={100}
                width="narrow"
                ariaLabel="Points this rule adds"
              />
            </label>
            <button
              className="ghost sm"
              onClick={add}
              disabled={!isRamp && field.type !== "bool" && rValue.trim() === ""}
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
  mediaType,
  onConditions,
  onKeeps,
}: {
  conditions: Condition[];
  keeps: GradedKeep[];
  gateIds: string[];
  mediaType: "movie" | "tv";
  onConditions: (c: Condition[]) => void;
  onKeeps: (k: GradedKeep[]) => void;
}) {
  // Same media-type narrowing as the remove editor, so a movie policy is never offered
  // a keep rule on a field a movie does not have.
  const { data: vocab } = useQuery({
    queryKey: ["vocabulary", "protect", mediaType],
    queryFn: () => api.vocabulary("protect", mediaType),
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
                <span className="rules-weight-keep">
                  lowers the score, up to −{k.max_discount} points
                </span>
                <button className="ghost sm" onClick={() => onKeeps(keeps.filter((_, j) => j !== i))}>
                  Remove
                </button>
              </div>
            );
          })}
        </div>
      )}

      <div className="rules-add">
        <Segmented
          value={strength}
          onChange={setStrength}
          label="Rule strength"
          options={[
            ["hard", "Keeps it outright"],
            ["lean", "Leans toward keeping"],
          ]}
        />

        {strength === "hard" ? (
          <div className="condition-add">
            <select value={hField} aria-label="Field" onChange={(e) => setHField(e.target.value)}>
              <option value="">Keep it when…</option>
              {hardFields.map((f) => (
                <option key={f.key} value={f.key}>
                  {f.label}
                </option>
              ))}
            </select>
            {hardField && (
              <>
                <select value={hOp} aria-label="Comparison" onChange={(e) => setHOp(e.target.value)}>
                  {hardField.ops.map((o) => (
                    <option key={o} value={o}>
                      {OP_LABELS[o] ?? o}
                    </option>
                  ))}
                </select>
                {hardField.type === "bool" ? (
                  // Two visible options, so both stay readable at a glance (Segmented.tsx).
                  <Segmented
                    value={hValue === "false" ? "false" : "true"}
                    onChange={setHValue}
                    label="Value"
                    options={[
                      ["true", "yes"],
                      ["false", "no"],
                    ]}
                  />
                ) : (
                  <SuggestInput field={hardField} value={hValue} onChange={setHValue} />
                )}
                <button
                  className="ghost sm"
                  onClick={addHard}
                  disabled={hardField.type !== "bool" && hValue.trim() === ""}
                >
                  Add rule
                </button>
              </>
            )}
            {hardField?.help_text && <p className="help">{hardField.help_text}</p>}
          </div>
        ) : (
          <div className="condition-add">
            <select value={lField} aria-label="Field" onChange={(e) => setLField(e.target.value)}>
              <option value="">when…</option>
              {leanFields.map((f) => (
                <option key={f.key} value={f.key}>
                  {f.label}
                </option>
              ))}
            </select>
            {leanField && (
              <>
                {/* Both directions stay on screen: a dropdown let the default save
                    without the operator ever seeing there was a choice. */}
                <Segmented
                  value={lDir}
                  onChange={setLDir}
                  label="Which way it leans"
                  options={[
                    ["high_keeps", "the more, the safer"],
                    ["low_keeps", "the less, the safer"],
                  ]}
                />
                <span className="muted">full effect at</span>
                {leanField.unit_suffix ? (
                  <FixedQuantity
                    value={lAt}
                    suffix={leanField.unit_suffix}
                    step={leanField.type === "rating_tenths" ? 0.1 : 1}
                    ariaLabel="Full effect at"
                    onChange={(v) => setLAt(String(v))}
                  />
                ) : (
                  <input
                    type="number"
                    value={lAt}
                    aria-label="Full effect at"
                    onChange={(e) => setLAt(e.target.value)}
                  />
                )}
                {/* One control standard: a number with a fixed unit is a FixedQuantity,
                    never a bare number box beside loose text. */}
                <label className="inline-weight">
                  up to
                  <FixedQuantity
                    value={lPoints}
                    onChange={setLPoints}
                    suffix="points off"
                    min={1}
                    max={100}
                    width="narrow"
                    ariaLabel="Points this rule takes off"
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

/** A button that opens the in-app docs to a page, and optionally a section within it. The
 *  header wears it as "Help"; each section wears it as a "Learn more" that lands on the
 *  matching part of the guide. */
function DocLink({
  doc,
  anchor,
  className,
  children,
}: {
  doc: string;
  anchor?: string;
  className?: string;
  children: React.ReactNode;
}) {
  const { openDoc } = useDocs();
  return (
    <button type="button" className={className} onClick={() => openDoc(doc, anchor)}>
      {children}
    </button>
  );
}

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
  const { data: saved, isError: policyFailed } = useQuery({
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
  const { data: savedPace, isError: paceFailed } = useQuery({
    queryKey: ["profile"],
    queryFn: api.profile,
  });
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
      // The Reap breakdown reads grace_days (its countdown and unmeasured lines), so a saved
      // grace or cap change refreshes it.
      void queryClient.invalidateQueries({ queryKey: ["reap-breakdown"] });
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

  // validatePolicy 422s when the policy is *provably* invalid (e.g. a dormancy floor under
  // 5 days); that error is what "you can't save this" means, and it is shown near the controls,
  // not dressed up as a simulation result.
  const { data: validation, error: invalidError } = useQuery({
    queryKey: ["validate", debounced],
    queryFn: () => api.validatePolicy(debounced!),
    enabled: debounced !== null,
    placeholderData: keepPreviousData,
    retry: false,
  });

  // Warnings render beside the control they describe (each anchor below claims its
  // fields); anything no anchor claims still shows in the bottom stack, so a new
  // warning field can never be silently dropped.
  const allWarnings = useMemo(() => validation?.warnings ?? [], [validation]);
  const warningsFor = (pred: (field: string) => boolean) =>
    allWarnings.filter((w) => pred(w.field));
  const anchors: ((field: string) => boolean)[] = [
    (f) => f === "condemn_at",
    (f) => f.startsWith("gates."),
    (f) => f === "keep_rating_rules",
    (f) => f === "keep_last_seasons" || f === "keep_last_scope",
    (f) => f === "signals",
    (f) => f === "custom_condemn",
    (f) => f === "graded_keeps",
    (f) => f === "max_unmeasured_per_run",
  ];
  const unanchoredWarnings = allWarnings.filter((w) => !anchors.some((p) => p(w.field)));

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

  // EVERY load-time recovery forces dirty, because in each case what is on screen is not
  // what is stored and the savebar has to offer the Save that replaces it. The three are
  // mutually exclusive server-side (services/profiles.py active_policy): `needs_save` is an
  // old policy rescaled to the 100-point budget, `fell_back` is a body that could not be
  // read at all, so this shows the shipped default instead, and `rating_rules_restored` is
  // a rating bar put back after it stopped keeping anything. Missing the second one left
  // the only way out of the fallback behind a gate that never opened. Discard cannot clear
  // any of them, which is right: there is no stored body to go back to that this build can
  // load, or (for the restored bar) none that still protects what the operator set.
  const dirty = useMemo(
    () =>
      draft !== null &&
      saved !== undefined &&
      (Boolean(saved.needs_save) ||
        Boolean(saved.fell_back) ||
        Boolean(saved.rating_rules_restored) ||
        JSON.stringify(draft) !== JSON.stringify(saved.body)),
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

  // The draft only ever seeds from a successful read, so a failed one would otherwise
  // leave the whole workspace saying "Loading…" for good. Say what happened instead.
  if (!draft) {
    if (policyFailed) {
      return <p className="notice notice-error">Couldn't load these settings. Reload to try again.</p>;
    }
    return <p className="muted">Loading…</p>;
  }

  const update = (patch: Partial<PolicyBody>) => setDraft({ ...draft, ...patch });
  const updatePace = (patch: Partial<ProfileSettings>) =>
    setPace(pace === null ? null : { ...pace, ...patch });
  // The engine's denominator, not just the built-in one: score() sums the weights of the
  // built-in signals AND the owner's own rules into a single fixed total (engine/signals.py).
  // Dividing by the built-ins alone would overstate every built-in signal's share and leave
  // the owner's rules looking like they cost the score nothing.
  const builtInWeight = draft.signals.reduce((sum, s) => sum + s.weight, 0);
  const yourWeight = draft.custom_condemn.reduce((sum, c) => sum + c.weight, 0);
  const totalWeight = builtInWeight + yourWeight;
  // The budget the server enforces (PolicyBody._weights_total_one_hundred). Checked here
  // too so Save is blocked before the round trip, and so the gap is a number the operator
  // can see moving rather than an error they discover on submit.
  const pointsLeft = REMOVAL_POINTS - totalWeight;
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
    // The whole removal lane, not just the built-ins: the shipped mix is already the full
    // 100 points, so leaving the operator's own rules beside it put every preset over
    // budget and disabled Save for the pace draft too. Rescaling both together keeps the
    // preset's shape and the operator's rules, and the score itself does not move.
    const scaled = rescaleToBudget([
      ...draft.signals.map((s) => mix[s.signal] ?? 0),
      ...draft.custom_condemn.map((c) => c.weight),
    ]);
    update({
      condemn_at: p.condemn_at,
      signals: draft.signals.map((s, i) => ({ ...s, weight: scaled[i] ?? 0 })),
      custom_condemn: draft.custom_condemn.map((c, i) => ({
        ...c,
        weight: scaled[draft.signals.length + i] ?? c.weight,
      })),
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
  // Branch on the caps switch: with caps off the executor skips the per-run and rolling
  // checks entirely, so claiming a hard "at most N per run" here would contradict the
  // caps-off warning below and the run itself (B-2). Grace still binds either way.
  const paceClause = !pace
    ? "removes only within your caps"
    : pace.caps_enabled
      ? `removes at most ${count(pace.max_items_per_run)} titles or ${bytes(pace.max_bytes_per_run)} per run`
      : "removes with no per-run limit until you turn limits back on";

  return (
    <section className="editor">
      <div className="editor-controls">
        {/* The context band: which policy you're editing, colored by the arr that runs
            it -- Radarr (gold) for movies, Sonarr (blue) for TV, reusing the Settings
            service-badge tokens. The switch lives inside it so there's never any doubt
            which policy the controls below belong to, and the blurb sits under the title. */}
        <div className={`policy-context ${mediaType === "tv" ? "tv" : "movie"}`}>
          <div className="pc-left">
            <div className="pc-title">
              <span className={`kind-badge kind-${mediaType === "tv" ? "sonarr" : "radarr"}`}>
                {mediaType === "tv" ? "Sonarr" : "Radarr"}
              </span>
              <h2>{mediaType === "tv" ? "TV policy" : "Movies policy"}</h2>
            </div>
            <p className="blurb pc-sub">
              {mediaType === "tv"
                ? "How Reaper judges TV: seasons, not whole shows. Tuned separately from movies."
                : "How Reaper judges your movies. TV is tuned separately, with the toggle."}
            </p>
          </div>
          <div className="policy-head-actions">
            {/* switchMediaType holds the two-step confirm when the draft has unsaved edits. */}
            <Segmented
              fill
              value={mediaType}
              onChange={switchMediaType}
              label="Which policy"
              options={[
                ["movie", "Movies"],
                ["tv", "TV"],
              ]}
            />
            <DocLink doc="understanding-policy" className="doc-help">
              <svg
                viewBox="0 0 24 24"
                width="15"
                height="15"
                fill="none"
                stroke="currentColor"
                strokeWidth="2.2"
                strokeLinecap="round"
                strokeLinejoin="round"
                aria-hidden="true"
              >
                <circle cx="12" cy="12" r="9" />
                <path d="M9.1 9a3 3 0 0 1 5.8 1c0 2-3 2.5-3 4" />
                <path d="M12 17h.01" />
              </svg>
              Help
            </DocLink>
          </div>
        </div>
        {/* A recovery notice renders on the load it explains, so it hangs off the response
            flag alone and no dirty gate, disclosure or savebar can hide it. */}
        {saved?.fell_back && (
          <div className="notice notice-error">
            Your saved policy couldn't be read, so this shows the starting one instead.
            Nothing has changed on your server. Check the values below, then save to
            replace it.
          </div>
        )}
        {saved?.rating_rules_restored && (
          <div className="notice notice-warn">
            Keep well-rated titles had stopped keeping anything. Your saved rating is back
            below, unsaved. Reaper won't remove anything until you check it and save.
          </div>
        )}
        {pendingSwitch !== null && (
          <div className="notice notice-warn">
            You have unsaved {kind} policy changes. Switching to{" "}
            {pendingSwitch === "tv" ? "TV" : "Movies"} discards them.{" "}
            <button
              type="button"
              className="danger"
              onClick={() => {
                setPendingSwitch(null);
                setMediaType(pendingSwitch);
              }}
            >
              Discard and switch
            </button>{" "}
            <button type="button" className="ghost" onClick={() => setPendingSwitch(null)}>
              Keep editing
            </button>
          </div>
        )}

        <nav className="settings-nav policy-rail" aria-label="Policy sections">
          {SECTIONS.map((s) => (
            <button
              key={s.id}
              className={activeSection === s.id ? "settings-tab active" : "settings-tab"}
              // Reserve the bold (active) width so switching sections never shifts the rail.
              data-label={s.label}
              // The section being read is stated, not just colored, the same as the
              // masthead and the settings rail.
              aria-current={activeSection === s.id ? "page" : undefined}
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
            {/* Hand-tuned drafts match no preset, so "custom" matches no option and no
                pill reads as active -- the same honesty the badge has always had. */}
            <Segmented
              value={preset ?? "custom"}
              onChange={(id) => {
                const p = PRESETS.find((x) => x.id === id);
                if (p) applyPreset(p);
              }}
              label="Starting point"
              options={PRESETS.map((p) => [p.id, p.label] as const)}
            />
          </div>
          <p className="help">
            {presetHelp} Picking one resets the built-in points and rescales your own rules
            to fit 100. Your scores stay where they are.
          </p>
          {staged !== null && (
            <p className="help">
              <strong>Staged, not saved.</strong> Review the changes below, then Save changes in
              the bar at the bottom.
            </p>
          )}
        </div>

        {/* ------------------------------------------------------------------ */}
        <h3 ref={sectionRefs.flags} className="policy-section">
          What flags a title
          <DocLink doc="understanding-policy" anchor="signals" className="doc-learn">
            Learn more
          </DocLink>
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
        <WarnBlock warnings={warningsFor((f) => f === "condemn_at")} />

        <label className="field">
          <span className="field-label">
            Judge a title only when there's enough to go on
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
            Reaper judges a title on the reasons below. If it can't check enough of them, it
            holds the title in Limbo for you to decide.
          </span>
        </label>

        <ul className="rule-list">
          {draft.signals.map((signal, i) => (
            <SignalRow
              key={signal.signal}
              signal={signal}
              onChange={(s) => {
                const signals = [...draft.signals];
                signals[i] = s;
                update({ signals });
              }}
            />
          ))}
        </ul>
        <WarnBlock warnings={warningsFor((f) => f === "signals")} />

        <RemoveRulesEditor
          condemn={draft.custom_condemn}
          mediaType={mediaType}
          onCondemn={(custom_condemn) => update({ custom_condemn })}
        />
        <WarnBlock warnings={warningsFor((f) => f === "custom_condemn")} />

        <div className="policy-divider" />

        {/* ------------------------------------------------------------------ */}
        <h3 ref={sectionRefs.kept} className="policy-section">
          What's always kept
          <DocLink doc="understanding-policy" anchor="protections" className="doc-learn">
            Learn more
          </DocLink>
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
            // Protections that carry their own settings render as cards below the plain
            // rows (the tags card and the rating card), so the visual weight says which
            // protections have more to configure. They are skipped here.
            if (gate.gate === "whitelisted" || gate.gate === "rating_floor") return null;
            return <GateRow key={gate.gate} gate={gate} onChange={setGate} />;
          })}
        </ul>
        <WarnBlock warnings={warningsFor((f) => f.startsWith("gates."))} />

        {(() => {
          const i = draft.gates.findIndex((g) => g.gate === "rating_floor");
          const rating = i >= 0 ? draft.gates[i] : undefined;
          if (!rating) return null;
          const setRating = (g: GateSetting) => {
            const gates = [...draft.gates];
            gates[i] = g;
            update({ gates });
          };
          return (
            <RatingFloorRow
              gate={rating}
              rules={draft.keep_rating_rules}
              match={draft.keep_rating_match}
              mediaType={mediaType}
              warnings={warningsFor((f) => f === "keep_rating_rules")}
              onGate={setRating}
              onRules={(keep_rating_rules) => update({ keep_rating_rules })}
              onMatch={(keep_rating_match) => update({ keep_rating_match })}
            />
          );
        })()}

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
              <label className="toggle card-head">
                <Switch checked={whitelist.enabled} onChange={setWhitelist} />
                <span className="rule-name">Spare titles you've tagged</span>
              </label>
              <p className="help rule-help">
                A title carrying one of these tags in Sonarr/Radarr is kept, whatever it scored. A
                ‘Never Reap’ Plex collection is honored too.
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
            <ul className="rule-list">
              <li className="rule-row">
                <span className="rule-name">Always keep the newest seasons</span>
                <div className="rule-control">
                  <span>the newest</span>
                  <FixedQuantity
                    value={draft.keep_last_seasons}
                    suffix={draft.keep_last_seasons === 1 ? "season" : "seasons"}
                    min={0}
                    width="narrow"
                    ariaLabel="Newest seasons to always keep"
                    onChange={(v) => update({ keep_last_seasons: Math.max(0, v) })}
                  />
                  <span>of every show</span>
                </div>
                <p className="help rule-help">
                  The most recent seasons of every show are kept outright, whatever they score.
                  There is no upper limit.
                </p>
                <SeasonAdvisory keepLast={draft.keep_last_seasons} />
              </li>

              <li className="rule-row">
                <span className="rule-name">Apply that to</span>
                <div className="rule-control">
                  <Segmented
                    value={draft.keep_last_scope}
                    onChange={(keep_last_scope) => update({ keep_last_scope })}
                    label="Keep-last scope"
                    options={[
                      ["all", "All shows"],
                      ["requested", "Requested only"],
                    ]}
                  />
                </div>
                <p className="help rule-help">
                  “Requested only” lets older seasons of shows nobody asked for be removed, while
                  still keeping the recent seasons of requested shows. When Reaper can't tell
                  whether a show was requested, it keeps the seasons to be safe.
                </p>
              </li>

              <li className="rule-row">
                <label className="toggle rule-toggle">
                  <Switch
                    checked={draft.keep_first_season}
                    onChange={(keep_first_season) => update({ keep_first_season })}
                  />
                  <span className="rule-name">Always keep a show's first season</span>
                </label>
                <p className="help rule-help">
                  So a new viewer can still start the show.
                </p>
              </li>

              <li className="rule-row">
                <label className="toggle rule-toggle">
                  <Switch
                    checked={draft.keep_in_progress}
                    onChange={(keep_in_progress) => update({ keep_in_progress })}
                  />
                  <span className="rule-name">Keep seasons someone is partway through</span>
                </label>
                <p className="help rule-help">
                  Reaper holds the season a viewer is midway into, plus the next one once they
                  finish it. Turn this off and being mid-show protects nothing.
                </p>
                {draft.keep_in_progress && (
                  <>
                    <div className="rule-control">
                      <span>let go of their place after</span>
                      <FixedQuantity
                        value={draft.in_progress_hold_days}
                        suffix="days"
                        min={0}
                        width="narrow"
                        ariaLabel="Days without watching before a held place is released"
                        onChange={(v) => update({ in_progress_hold_days: Math.max(0, v) })}
                      />
                      <span>without watching</span>
                    </div>
                    <p className="help rule-help">
                      If someone has not watched any of the show in this many days, Reaper treats
                      the show as abandoned by them and lets go of their place. Set to 0 to hold
                      it forever. When Reaper can't tell when they last watched, it keeps holding.
                    </p>
                    <div className="rule-control">
                      <span>also keep</span>
                      <FixedQuantity
                        value={draft.season_lookahead}
                        suffix={draft.season_lookahead === 1 ? "season" : "seasons"}
                        min={0}
                        width="narrow"
                        ariaLabel="Seasons to keep ahead of a mid-binge viewer"
                        onChange={(v) => update({ season_lookahead: Math.max(0, v) })}
                      />
                      <span>ahead of where they are</span>
                    </div>
                    <p className="help rule-help">
                      Set this above 0 to also keep the seasons just ahead of each viewer.
                    </p>
                  </>
                )}
              </li>

              <li className="rule-row">
                <label className="toggle rule-toggle">
                  <Switch
                    checked={draft.keep_specials}
                    onChange={(keep_specials) => update({ keep_specials })}
                  />
                  <span className="rule-name">Never remove specials</span>
                </label>
                <p className="help rule-help">
                  On: specials (Season 0) are always kept. Off: specials are judged like any other
                  season. Either way, specials never count toward the newest seasons you keep.
                </p>
              </li>

              <li className="rule-row">
                <label className="toggle rule-toggle">
                  <Switch
                    checked={draft.protect_incomplete_seasons}
                    onChange={(protect_incomplete_seasons) =>
                      update({ protect_incomplete_seasons })
                    }
                  />
                  <span className="rule-name">Never remove a season that's still downloading</span>
                </label>
                <p className="help rule-help">
                  Keeps a season Sonarr is still filling in, so a removal never fights an active
                  download. Turn it off for ended shows that Sonarr permanently lists as missing an
                  episode.
                </p>
              </li>

              <li className="rule-row">
                <label className="toggle rule-toggle">
                  <Switch
                    checked={draft.flag_keep_conflicts}
                    onChange={(flag_keep_conflicts) => update({ flag_keep_conflicts })}
                  />
                  <span className="rule-name">Ask me first when a removal looks unusual</span>
                </label>
                <p className="help rule-help">
                  When a season your rule would remove was watched by more people than a season it
                  keeps, Reaper marks it "Needs a look" and waits for you. Turn this off and
                  Reaper follows your keep rule without asking.
                </p>
              </li>
            </ul>
            <WarnBlock
              warnings={warningsFor((f) => f === "keep_last_seasons" || f === "keep_last_scope")}
            />
          </div>
        )}

        <KeepRulesEditor
          conditions={draft.protect_conditions}
          keeps={draft.graded_keeps}
          // Only protections that are ON hide their field from custom keeps: a disabled
          // gate protects nothing, so its field must stay authorable here.
          gateIds={draft.gates.filter((g) => g.enabled).map((g) => g.gate)}
          mediaType={mediaType}
          onConditions={(protect_conditions) => update({ protect_conditions })}
          onKeeps={(graded_keeps) => update({ graded_keeps })}
        />
        <WarnBlock warnings={warningsFor((f) => f === "graded_keeps")} />

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
        {/* Warnings live beside their controls; only one no anchor claims lands here. */}
        <WarnBlock warnings={unanchoredWarnings} />

        <p className="hash">
          {validation && (
            <>
              {kind} policy <code>{validation.policy_hash.slice(0, 12)}</code> · saving does not
              arm anything
            </>
          )}
        </p>

        <div className="policy-divider" />

        {/* ------------------------------------------------------------------ */}
        <h3 ref={sectionRefs.pace} className="policy-section">
          Pace and limits
          <DocLink doc="understanding-policy" anchor="pace" className="doc-learn">
            Learn more
          </DocLink>
        </h3>
        <p className="blurb">
          Ceilings on how much one run and a rolling month may remove, plus the grace countdown.
          Movies and TV alike.
        </p>

        {/* Recovery notice: hangs off the response flag alone, so no dirty gate or disclosure
            can hide it (mirrors the policy recovery notice above). */}
        {savedPace?.settings_recovered && (
          <p className="notice notice-error">
            Your saved caps and grace couldn't be read, so these show the starting ones.
            Nothing has changed on your server, but a scan won't remove anything until you
            check these and save.
          </p>
        )}

        {pace === null ? (
          paceFailed ? (
            <p className="notice notice-error">
              Couldn't load these settings. Reload to try again.
            </p>
          ) : (
            <p className="muted">Loading…</p>
          )
        ) : (
          <>
            <label className="toggle pace-approval">
              <Switch
                checked={pace.caps_enabled}
                onChange={(caps_enabled) => updatePace({ caps_enabled })}
              />
              <span>Limit how much each run removes</span>
            </label>
            <p className="help pace-approval-help">
              An extra ceiling on how much one run and a rolling month remove, on top of the
              deletion password. Turn off for a big first cleanup, back on for routine runs.
            </p>
            {!pace.caps_enabled && (
              <p className="notice notice-warn notice-inline">
                No cap on run size. A run can remove everything you've approved at once.
                Deletion still needs the password and your approval of the list.
              </p>
            )}

            {/* The four caps as a 2x2 matrix: titles / disk freed down the side, per run /
                per 30 days across the top. The headers carry what four labels and four help
                lines used to, and the fixed grid tracks keep the boxes lined up. Hidden,
                not disabled, while the caps are off -- the same gates-are-hidden grammar the
                rest of the editor uses. */}
            {pace.caps_enabled && (
              <>
                <div className="pace-matrix">
                  <span />
                  <span className="col-h">Per run</span>
                  <span className="col-h">
                    Per 30 days <em>rolling</em>
                  </span>

                  <span className="row-h">Titles</span>
                  <FixedQuantity
                    value={pace.max_items_per_run}
                    suffix="titles"
                    min={1}
                    width="narrow"
                    ariaLabel="Most titles per run"
                    onChange={(v) => updatePace({ max_items_per_run: v || 1 })}
                  />
                  <FixedQuantity
                    value={pace.max_items_per_30d}
                    suffix="titles"
                    min={1}
                    width="narrow"
                    ariaLabel="Most titles per 30 days"
                    onChange={(v) => updatePace({ max_items_per_30d: v || 1 })}
                  />

                  <span className="row-h">Disk freed</span>
                  <QuantityInput
                    value={pace.max_bytes_per_run}
                    units={SIZE_UNITS}
                    ariaLabel="Most disk freed per run"
                    onChange={(v) => updatePace({ max_bytes_per_run: v })}
                  />
                  <QuantityInput
                    value={pace.max_bytes_per_30d}
                    units={SIZE_UNITS}
                    ariaLabel="Most disk freed per 30 days"
                    onChange={(v) => updatePace({ max_bytes_per_30d: v })}
                  />
                </div>
                <p className="help matrix-note">
                  Cross a limit and the whole run stops. It never deletes just the part that fits.
                </p>
              </>
            )}

            {/* Grace and unknown-size are not caps, so they step out of the matrix: one label,
                one control, one short line of help bound directly beneath it. */}
            <div className="pace-extra">
              <span className="ex-label">Grace period</span>
              <span className="ex-ctl">
                <QuantityInput
                  value={pace.grace_days}
                  units={TIME_UNITS}
                  min={7}
                  ariaLabel="Grace period"
                  onChange={(v) => updatePace({ grace_days: v })}
                />
                <span className="help">Time on the list to rescue a title before removal.</span>
              </span>

              <span className="ex-label">Unknown-size items</span>
              <span className="ex-ctl">
                <FixedQuantity
                  value={pace.max_unmeasured_per_run}
                  suffix="per run"
                  min={0}
                  width="narrow"
                  ariaLabel="Items with an unknown size"
                  onChange={(v) => updatePace({ max_unmeasured_per_run: v })}
                />
                <span className="help">
                  Kept by default. Size caps can't measure them. Set 0 to always keep.
                </span>
                <WarnBlock warnings={warningsFor((f) => f === "max_unmeasured_per_run")} />
              </span>
            </div>
          </>
        )}

        <div className="policy-divider" />

        {/* ------------------------------------------------------------------ */}
        <h3 ref={sectionRefs.deletion} className="policy-section">
          Deletion
          <DocLink doc="arming" className="doc-learn">
            Learn more
          </DocLink>
        </h3>
        <p className="blurb">
          Whether Reaper is allowed to remove anything at all. One switch for all of Reaper,
          movies and TV alike. Turning it on always asks for the admin password. Turning it off
          never does.
        </p>
        <DeletionToggle />

        {/* THE save bar: one place to save whatever is dirty -- the policy draft, the pace
            draft, or both (a preset stages both). Pinned to the viewport bottom while it
            has something to say, so Save is never a scroll away from the edit. */}
        {(dirty || paceDirty) && (
          <div className="savebar">
            <span className="savebar-what">
              <strong>
                Unsaved changes: {[dirty ? `${kind} policy` : null, paceDirty ? "pace and limits" : null]
                  .filter(Boolean)
                  .join(" · ")}
              </strong>
              <br />
              {dirty && paceDirty
                ? "Policy changes apply on the next scan, which starts by itself after saving. Pace applies immediately."
                : dirty
                  ? `Saves only your ${kind} policy. The ${otherKind} one is untouched. It applies on the next scan, which starts by itself after saving.`
                  : "Pace applies immediately. Changing a limit never affects a run you've already approved."}
              {/* The rescale recovery. Rendered here, beside Save, because it is about
                  what you are about to write -- and from the response FLAGS, not the
                  warning list, which is built by re-validating the draft and so can never
                  carry anything about how the policy loaded. The louder `fell_back`
                  recovery renders at the top of the page instead, unconditionally, so no
                  gate can hide it. */}
              {saved?.needs_save && !saved.fell_back && (
                <span className="notice notice-warn budget-notice">
                  Your points have been spread to add up to 100. Nothing has changed on
                  your server yet. Each rule keeps the same share it had, so your scores
                  stay where they are. Review and save.
                </span>
              )}
              {dirty && <PointsBudget builtIn={builtInWeight} yours={yourWeight} />}
            </span>
            <button
              className="ghost"
              onClick={() => {
                if (dirty) setDraft(saved?.body ?? null);
                if (paceDirty) setPace(savedPace ?? null);
              }}
            >
              Discard
            </button>
            <button
              className="primary"
              disabled={
                (dirty && (Boolean(invalidMessage) || pointsLeft !== 0)) ||
                save.isPending ||
                savePace.isPending
              }
              onClick={() => {
                // Two independent saves; each failure renders its own notice below.
                if (dirty) save.mutate(draft);
                if (paceDirty && pace) savePace.mutate(pace);
              }}
            >
              {save.isPending || savePace.isPending ? "Saving…" : "Save changes"}
            </button>
            {save.error && <p className="notice notice-error">{save.error.message}</p>}
            {savePace.error && <p className="notice notice-error">{savePace.error.message}</p>}
          </div>
        )}
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
              followupQueued={scanState?.followup_queued ?? false}
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
