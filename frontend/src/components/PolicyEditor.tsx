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
import { useCallback, useEffect, useMemo, useRef, useState, type RefObject } from "react";
import {
  api,
  ApiError,
  type GateSetting,
  type Policy,
  type PolicyBody,
  type PolicyWarning,
  type ProfileSettings,
  type RatingRule,
  type RatingSource,
  type SignalSetting,
} from "../api";
import { announce } from "../announce";
import { REMOVES_ITS_ROW, useRemovalFocus, useSavebarFocus } from "../focus";
import { useDocs } from "../docs/DocsContext";
import { bytes, count } from "../format";
import { DeletionToggle } from "./DeletionToggle";
import { GATE_META, SIGNAL_META, titleCase } from "./policyMeta";
import { KeepRulesEditor, RemoveRulesEditor } from "./PolicyRuleEditors";
import {
  activePreset,
  andList,
  DEFAULT_WEIGHTS,
  PRESETS,
  REMOVAL_POINTS,
  rescaleToBudget,
  type PresetCaps,
  type PresetId,
} from "./policyPresets";
import { Outcome, StaleNotice } from "./PolicySimulator";
import { FixedQuantity, QuantityInput, SIZE_UNITS, TIME_UNITS } from "./QuantityInput";
import { Segmented } from "./Segmented";
import { Switch } from "./Switch";
import { Notice } from "./Notice";
import { SwitchConfirm } from "./SwitchConfirm";

/** "1095 days" said the way a person would: "3 years". */
export function humanDays(days: number): string {
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
  // Removing a chip destroys the button holding focus, so without this the operator lands on
  // `<body>` and the next Tab restarts above the whole ~1,900-line policy form -- three times
  // over for three tags (#173). Focus goes to the next chip's ×, or to the add box once the
  // last one is gone.
  const addRef = useRef<HTMLInputElement>(null);
  const chips = useRemovalFocus(addRef);
  return (
    <div className="keep-tags">
      <div className="tag-chips" ref={chips.ref as RefObject<HTMLDivElement>}>
        {tags.map((t, i) => (
          <span key={t} className="tag-chip">
            {t}
            <button
              {...REMOVES_ITS_ROW}
              onClick={() => {
                chips.removing(i);
                onTags(tags.filter((x) => x !== t));
              }}
              aria-label={`Remove ${t}`}
            >
              ×
            </button>
          </span>
        ))}
        <input
          ref={addRef}
          // A placeholder is a name of last resort, so this box was announcing itself as the
          // example text inside it and lost even that the moment anything was typed. Same
          // defect #136 fixed on the Plex panel's address pair.
          aria-label="Add a keep tag"
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

/** The DOM id of the `i`th warning rendered at one anchor. Both ends of the association are
 *  named by this one function, so the control's `aria-describedby` and the notice's `id` cannot
 *  drift apart the way two hand-written strings would. */
export function warningId(anchor: WarningAnchorId | "unanchored", i: number): string {
  return `policy-warning-${anchor}-${i}`;
}

/** What a control at `anchor` points `aria-describedby` at: every warning currently rendered
 *  there, in order.
 *
 *  `undefined` when there are none, and that is not a tidiness choice -- `WarnBlock` renders
 *  nothing at all when the list is empty, so a fixed id would be a reference to an element not
 *  in the document. Readers treat a dangling `aria-describedby` inconsistently, and the one
 *  behavior they share is that the operator learns nothing from it.
 *
 *  `fields` narrows it to the warnings about ONE of an anchor's fields, for an anchor whose
 *  block serves more than one control. The filter runs INSIDE the map and never before it:
 *  `warningId` is positional within the anchor's rendered list, so filtering the array first
 *  would number the survivors 0..n and point them at the wrong notices. */
export function warningsDescribing(
  anchor: WarningAnchorId | "unanchored",
  warnings: PolicyWarning[],
  fields?: readonly string[],
): string | undefined {
  const ids = warnings
    .map((w, i) => (fields === undefined || fields.includes(w.field) ? warningId(anchor, i) : null))
    .filter((id): id is string => id !== null);
  return ids.length === 0 ? undefined : ids.join(" ");
}

// No `aria-invalid` companion to the above, deliberately. A policy warning of EITHER severity
// leaves the policy saveable: `policyBlocked` below is a 422 from body validation plus the
// points budget, `severity` reaches nothing else, and the save route never inspects. ARIA 1.2
// defines `aria-invalid` as a value the application does not accept, so flagging a legal
// setting states a refusal that will not happen -- on a slider whose whole job is choosing an
// aggressive threshold on purpose. The warning still reaches the operator as the control's
// description, which is what #174 asked for.

/** Inline warnings for one control group, rendered beside the control that fixes them.
 *  Renders nothing when the group has nothing to say. */
function WarnBlock({
  anchor,
  warnings,
}: {
  /** Which anchor this block is rendering, so each notice can carry the id the control that
   *  fixes it points at. Required rather than optional: a block with no anchor emits no ids,
   *  and the control beside it would then describe itself with nothing (#174). */
  anchor: WarningAnchorId | "unanchored";
  warnings: PolicyWarning[];
}) {
  if (warnings.length === 0) return null;
  return (
    <>
      {warnings.map((w, i) => (
        // `standing`, not announced: these re-render on every debounced validate as the
        // operator types, so an alert per keystroke would talk over them continuously. The
        // right mechanism is `aria-describedby` from each field to its own warning, which is
        // what the `id` below now makes possible -- the field speaks it when the operator
        // reaches the control, instead of the page interrupting them mid-keystroke.
        <Notice
          tone={w.severity === "danger" ? "error" : "warn"}
          inline
          standing
          id={warningId(anchor, i)}
          // Field and message do not separate these. Two protect conditions on the same
          // movie-only field in a TV policy produce byte-identical warnings, because the
          // producer has no name to put in them -- `ConditionSpec` carries a field, an
          // operator and a value, and nothing an operator titled. The position within this
          // already-filtered list is the only thing left that differs, and it is stable
          // across a render since the list is derived, never reordered (rule 19).
          key={`${w.field}:${w.message}:${i}`}
        >
          {w.message}
        </Notice>
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
        <Switch checked={gate.enabled} onChange={(enabled) => onChange({ ...gate, enabled })} />
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
            value={gate.threshold}
            suffix={gate.threshold === 1 ? "person" : "people"}
            min={1}
            width="narrow"
            ariaLabel={`${meta.label} threshold`}
            onChange={(v) => onChange({ ...gate, threshold: v })}
          />
        </div>
      )}
      {/* The look-back window, for the gates that count activity inside one. The server
          already warns when it is set under 30 days and advises a year, and until this
          control existed that warning named a value with no control anywhere on the page
          (U-9). Same picker as the dormancy row, and its own help directly beneath it, so
          "recently" is defined where it is set (rule 45). */}
      {gate.enabled && meta.window && (
        <>
          <div className="rule-control">
            <span>{meta.window.label}</span>
            <QuantityInput
              value={gate.window_days}
              units={TIME_UNITS}
              min={1}
              ariaLabel="How far back recent plays count"
              onChange={(v) => onChange({ ...gate, window_days: v })}
            />
          </div>
          <p className="help rule-help">{meta.window.help}</p>
        </>
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
  rotten_tomatoes_critic: {
    label: "Rotten Tomatoes critics",
    scale: "pct",
    votes: false,
    defFloor: 75,
    defVotes: 0,
  },
  rotten_tomatoes_audience: {
    label: "Rotten Tomatoes audience",
    scale: "pct",
    votes: false,
    defFloor: 80,
    defVotes: 0,
  },
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
  const votes =
    meta.votes && rule.min_votes > 0 ? ` from ${rule.min_votes.toLocaleString()}+ votes` : "";
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
        {...REMOVES_ITS_ROW}
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
  /** Server-side policy warnings anchored to the rating rules (field keep_rating_rules).
   *  `PolicyWarning`, not a structural subset of it: the looser type is what let this card
   *  render its own copy of `WarnBlock`'s markup, since the value it held could not be handed
   *  to the shared component. */
  warnings: PolicyWarning[];
  onGate: (g: GateSetting) => void;
  onRules: (r: RatingRule[]) => void;
  onMatch: (m: "any" | "all") => void;
}) {
  const used = new Set(rules.map((r) => r.source));
  const available = RATING_ORDER.filter((s) => !used.has(s));
  const addSource = (source: RatingSource) => {
    const meta = RATING_META[source];
    onRules([
      ...rules,
      { source, floor: meta.defFloor, min_votes: meta.votes ? meta.defVotes : 0 },
    ]);
  };
  const joiner = match === "any" ? ", or " : ", and ";
  const summary =
    rules.length === 0
      ? "Nothing is kept yet: add a rating source to set the score a title must clear to stay."
      : `Keep a title rated at least ${rules.map(describeBar).join(joiner)}.`;

  // The same shape as the keep-tag chips 290 lines above, on the same ~1,900-line form: the ×
  // removes the row it lives in, so without this focus falls to `<body>` and the next Tab
  // restarts at the top (#173). Missed in that sweep and caught by a rule 72 pass over it.
  const addSourceRef = useRef<HTMLSelectElement>(null);
  const bars = useRemovalFocus(addSourceRef);

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
            <div className="bar-table" ref={bars.ref as RefObject<HTMLDivElement>}>
              {rules.map((rule, i) => (
                <RatingBarRow
                  key={rule.source}
                  rule={rule}
                  onChange={(r) => onRules(rules.map((x, j) => (j === i ? r : x)))}
                  onRemove={() => {
                    bars.removing(i);
                    onRules(rules.filter((_, j) => j !== i));
                  }}
                />
              ))}
            </div>
          )}
          <div className="bar-foot">
            {available.length > 0 ? (
              <select
                ref={addSourceRef}
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
          {/* Through the shared component, not a second copy of its markup (rule 18). This
              one keyed on the bare message, so it carried the duplicate-key defect in a
              worse form than the original and would not have been swept with it. */}
          {warnings.length > 0 ? (
            <WarnBlock anchor="keep_rating_rules" warnings={warnings} />
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
        <i
          className="budget-built"
          style={{ width: `${(Math.min(builtIn, 100) / scale) * 100}%` }}
        />
        <i className="budget-yours" style={{ width: `${(Math.min(yours, 100) / scale) * 100}%` }} />
        {left < 0 && <i className="budget-over" style={{ width: `${(-left / scale) * 100}%` }} />}
        {left > 0 && <i className="budget-free" style={{ width: `${(left / scale) * 100}%` }} />}
      </span>
      <span className="budget-line">
        <span>
          <strong>{total}</strong> of 100 removal points used
        </span>
        {left === 0 ? (
          <span className="muted">{pointsSplit(builtIn, yours)}</span>
        ) : (
          <span className={left < 0 ? "budget-over-text" : "muted"}>
            {left < 0 ? `${-left} over` : `${left} left to give out`}
          </span>
        )}
      </span>
      {/* `standing`: this is a live readout of the points meter directly above it, and its
          numbers change on every step of a weight slider. Announced, it would talk over the
          operator continuously for the whole of a drag. What they are owed at the moment it
          matters is a Save that says why it is disabled, which the savebar carries. */}
      {left !== 0 && (
        <Notice tone="error" className="budget-notice" as="span" standing>
          {left < 0
            ? `Your rules add up to ${total} points. Take ${-left} away before saving.`
            : `Your rules add up to ${total} points. Give out the other ${left} before saving.`}
        </Notice>
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

export const OP_LABELS: Record<string, string> = {
  gte: "is at least",
  lte: "is at most",
  eq: "is",
  in: "is one of",
  contains: "contains",
};

// A vocabulary field already handled by a built-in protection above -> not offered as a
// custom rule, so the two never say the same thing twice. Only fields with no built-in gate
// (size, all-time watchers, vote count, season rank) remain to be authored here.
export const FIELD_TO_GATE: Record<string, string> = {
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
export const FIELD_TO_SIGNAL: Record<string, string> = {
  days_unwatched: "unwatched",
  recent_watchers: "few_watchers",
  imdb_rating: "low_rating",
  season_rank: "season_rank",
  size_bytes: "size",
};

/** Live advisory beside the keep-last input: how many shows a keep-last-N value fully
 *  protects, computed from the last scan's season shape -- no re-scan, since the shape does
 *  not depend on the keep-last value.
 *
 *  It takes the SCOPE too, because the floor does. "Requested only" narrows the set this
 *  floor acts on to the shows someone asked for, plus every show Reaper cannot tell about
 *  (season_scan._keep_last_applies keeps those on purpose: Unknown counts as "might be
 *  requested"). That set is not derivable from the frozen snapshot -- it needs the live
 *  request index -- so under that scope the figure is stated as the upper bound it is,
 *  rather than printed as though the scope were off (U-7, rules 53/30). An upper bound also
 *  cannot assert "you have protected everything", so the warning styling stops there. */
function SeasonAdvisory({ keepLast, scope }: { keepLast: number; scope: "all" | "requested" }) {
  const { data } = useQuery({ queryKey: ["season-shape"], queryFn: () => api.seasonShape() });
  if (!data || data.total_shows === 0) return null;
  const covered = Object.entries(data.season_counts).reduce(
    (sum, [seasons, shows]) => (Number(seasons) <= keepLast ? sum + shows : sum),
    0,
  );
  if (covered === 0) return null;
  const bounded = scope === "requested";
  return (
    <p className={`help ${!bounded && covered === data.total_shows ? "help-warn" : ""}`}>
      With this setting, {bounded ? "up to " : null}
      <strong>{count(covered)}</strong> of {count(data.total_shows)} shows have no season eligible
      for removal (from your last scan).
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

/** A mount condition one of the anchors below sits under. An anchor claims its fields only
 *  while its guard holds, so on the other branch they fall to the catch-all stack instead of
 *  off the page. Adding a value here does not compile in `PolicyEditor.test.tsx` until the
 *  two page states that hold it and drop it are written there. */
export type WarningGuard = "pace" | "tv" | "ratingGate";

/** One place a policy warning renders, and the fields it claims. */
export type WarningAnchor = {
  /** Named by the `warningsAt(...)` call that renders it, so the claim and the render read
   *  from this one declaration rather than from two copies of the same field list. */
  readonly id: string;
  /** The fields claimed exactly -- and the fields the test probes this anchor with, which is
   *  why a claim cannot go unprobed: it is one list, not two. */
  readonly fields: readonly string[];
  /** Claimed as a family as well: any field starting with this. `fields` then holds one real
   *  member of the family, since a probe has to be a field the server could actually send. */
  readonly prefix?: string;
  /** The mount condition this anchor's `WarnBlock` sits under, where it has one. */
  readonly guard?: WarningGuard;
};

const ANCHORS = [
  { id: "condemn_at", fields: ["condemn_at"] },
  { id: "gates", fields: ["gates.server_popularity.window_days"], prefix: "gates." },
  { id: "keep_rating_rules", fields: ["keep_rating_rules"], guard: "ratingGate" },
  { id: "keep_last", fields: ["keep_last_seasons", "keep_last_scope"], guard: "tv" },
  // Its block sits inside the mid-binge row but OUTSIDE that row's `keep_in_progress`
  // subtree, so `tv` is the only mount condition it has and one guard names it exactly.
  //
  // Nesting it under the switch too would have been expressible -- `guardsHeld` folds a
  // conjunction into one boolean, which is what `ratingGate` already does for an anchor under
  // two conditions (issue #200 was closed as refuted on precisely that). It is not done
  // because it would buy nothing here and costs the operator something: the server sends this
  // field only while the protection is on, since a guard that is off is holding no seasons,
  // so the extra condition can never discriminate -- and a claim that narrow drops the warning
  // to the catch-all if the backend ever widens, printing "lower this" at the foot of the page
  // instead of beside the box. Claiming on `tv` alone keeps it in the card either way.
  { id: "in_progress", fields: ["in_progress_hold_days"], guard: "tv" },
  { id: "signals", fields: ["signals"] },
  { id: "custom_condemn", fields: ["custom_condemn"] },
  { id: "keep_rules", fields: ["graded_keeps", "protect_conditions"] },
  { id: "max_unmeasured_per_run", fields: ["max_unmeasured_per_run"], guard: "pace" },
] as const;

/** Where each policy warning renders.
 *
 *  A warning renders beside the control that fixes it (rule 42): each anchor claims the
 *  fields whose fix lives at one place on the page, and anything no anchor claims lands in
 *  the bottom catch-all stack, so a warning field is never silently dropped.
 *
 *  Claiming is therefore a promise to RENDER, and it is exactly what excludes a field from
 *  the catch-all. An anchor whose `WarnBlock` sits inside a conditional subtree takes its
 *  warning off the page altogether on the branch that subtree does not mount -- not down to
 *  the bottom, which is what the sentence above promises. `max_unmeasured_per_run` did that
 *  through a failed profile read, losing the one warning about a setting that lets deletions
 *  past the size caps (#145). So an anchor under a mount condition names it as its `guard`
 *  and claims only while it holds.
 *
 *  This is data, and exported, because reconciling an anchor against its renderer is a test's
 *  job: `PolicyEditor.test.tsx` walks THIS list, drives one warning per claimed field through
 *  the page in the state each guard requires, and fails when one renders nowhere -- an anchor
 *  added with no `warningsAt` call site, or a `WarnBlock` deleted from under one. That walk
 *  was a hand-mirrored copy of this list, which could not see a new anchor at all, and before
 *  that a count in a comment that went stale at seven against eight.
 *
 *  It also drives every anchor through every branch it does NOT name, which is what catches
 *  the `guard` that was never declared -- the omission #145 actually was. Naming a guard is
 *  therefore checked both ways round, so neither adding a mount condition nor forgetting to
 *  is a silent change (#167).
 *
 *  Two bounds on that, both real: the walk does not check WHICH control a warning landed
 *  beside, only that the claim reached the page; and `guard` is one condition, not a set, so
 *  a `WarnBlock` nested under two of them cannot be declared here and is not covered by
 *  either direction. Put one under a second mount condition and it needs this type widened
 *  and the test's states composed, not a second anchor. */
export const WARNING_ANCHORS: readonly WarningAnchor[] = ANCHORS;

/** The id of an anchor above. A render site naming one that does not exist is a type error. */
export type WarningAnchorId = (typeof ANCHORS)[number]["id"];

/** Whether an anchor claims a warning field. The one matcher, read by the render sites and by
 *  the catch-all alike, so the two cannot read this declaration differently. */
export function anchorClaims(anchor: WarningAnchor, field: string): boolean {
  return (
    anchor.fields.includes(field) ||
    (anchor.prefix !== undefined && field.startsWith(anchor.prefix))
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
  // Save and Discard both unmount the bar holding the pressed button (#173). Declared here,
  // above the `if (!draft)` return further down, because a hook below an early return is a
  // different hook order on the renders that take it (rule 146).
  const bar = useSavebarFocus();
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
      announce("Pace and limits saved.");
      setPace(s);
      void queryClient.invalidateQueries({ queryKey: ["profile"] });
      // The Reap breakdown reads grace_days (its countdown and unmeasured lines), so a saved
      // grace or cap change refreshes it.
      void queryClient.invalidateQueries({ queryKey: ["reap-breakdown"] });
    },
  });
  // `settings_recovered` forces dirty for the same reason `fell_back` does on the policy half
  // (see the comment on `dirty` below): the caps on screen are the shipped starting ones, not
  // what is stored, and the recovery notice tells the operator a scan will remove nothing until
  // they check these and save. Without this the savebar never appeared, so there was no Save to
  // press and the only way out was to change some value deliberately -- restoring the intended
  // caps and saving them was impossible (B-6). Discard cannot clear it, which is right: there is
  // no stored profile to go back to.
  const paceDirty = useMemo(
    () =>
      pace !== null &&
      savedPace !== undefined &&
      (Boolean(savedPace.settings_recovered) || JSON.stringify(pace) !== JSON.stringify(savedPace)),
    [pace, savedPace],
  );

  // Debounce the draft the simulator/validator run against, so dragging a slider fires one
  // request when you stop -- not one per pixel. Combined with keepPreviousData below, this is
  // what stops the outcome box flickering while you adjust a weight.
  const [debounced, setDebounced] = useState<PolicyBody | null>(null);
  // The unknown-size allowance rides along on the same timer. It is not part of the policy at
  // all -- it lives on the profile -- but its warning is anchored beneath the box that sets it,
  // so the validator has to see the DRAFTED value or that warning describes something else
  // (B-26). One timer, so dragging either the policy or this fires one request when you stop.
  const draftedUnmeasured = pace?.max_unmeasured_per_run ?? null;
  const [debouncedUnmeasured, setDebouncedUnmeasured] = useState<number | null>(null);
  useEffect(() => {
    const id = setTimeout(() => {
      setDebounced(draft);
      setDebouncedUnmeasured(draftedUnmeasured);
    }, 250);
    return () => clearTimeout(id);
  }, [draft, draftedUnmeasured]);

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
    queryKey: ["validate", debounced, debouncedUnmeasured],
    queryFn: () => api.validatePolicy(debounced!, debouncedUnmeasured),
    enabled: debounced !== null,
    placeholderData: keepPreviousData,
    retry: false,
  });

  // Where these land is `WARNING_ANCHORS` above, which also says why claiming a field is a
  // promise to render it (rules 42, 7/24).
  const allWarnings = useMemo(() => validation?.warnings ?? [], [validation]);
  // Which guards hold this render. The mount condition each one names is a checked fact, not
  // a claim about itself: the walk in `PolicyEditor.test.tsx` drives every guard both ways
  // and pins a control that exists on the held branch only, so a guard naming the wrong
  // condition fails there rather than reading green.
  const guardsHeld: Record<WarningGuard, boolean> = {
    pace: pace !== null,
    tv: mediaType === "tv",
    ratingGate: (draft?.gates ?? []).some((g) => g.gate === "rating_floor" && g.enabled),
  };
  const anchors = WARNING_ANCHORS.filter((a) => a.guard === undefined || guardsHeld[a.guard]);
  // Stable, so the WarnBlocks below are not handed a new filter on every render. Each names
  // its anchor rather than repeating that anchor's field list, so there is nothing here to
  // drift out of step with the declaration.
  const warningsAt = useCallback(
    (id: WarningAnchorId) =>
      allWarnings.filter((w) =>
        WARNING_ANCHORS.some((a) => a.id === id && anchorClaims(a, w.field)),
      ),
    [allWarnings],
  );
  const unanchoredWarnings = allWarnings.filter(
    (w) => !anchors.some((a) => anchorClaims(a, w.field)),
  );

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
      // And re-validate. A scan syncs watch history first, so it is the one thing that
      // moves the reach the popularity-window warning is computed from -- the fresh 0 in
      // the simulator and the sentence explaining that 0 arrive from the same event, and
      // invalidating only the former shows the count without the reason. The key is
      // structurally unchanged across a scan, so nothing else re-keys this query.
      void queryClient.invalidateQueries({ queryKey: ["validate"] });
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
      // The savebar unmounting is the only thing that used to happen here, and an operator
      // using a screen reader cannot perceive an absence: no message, then a lost focus point.
      // Named for the half that saved, because the other half saves separately and its own
      // sentence follows. On the server's answer, never on the press (rule 85).
      announce(savedType === "tv" ? "TV policy saved." : "Movie policy saved.");
      queryClient.setQueryData(["policy", savedType], policy);
      // Re-seed the draft from the server's response so the dirty check compares
      // canonical forms. The server can order fields differently from the draft the
      // save was built from, and a raw JSON.stringify comparison would then read
      // "unsaved changes" forever.
      setDraft((cur) => (cur && cur.media_type === policy.body.media_type ? policy.body : cur));
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
  // See SwitchConfirm.tsx: a repeat press of the same segment changes no state, so focus is
  // keyed on this rather than on `pendingSwitch`.
  const [switchNonce, setSwitchNonce] = useState(0);

  // The notice only exists because there are edits to lose, so it goes when they do -- by
  // Discard, or by a Save that stores them. It used to survive both, still warning that a
  // switch "discards them" and still offering a red "Discard and switch" for changes that
  // no longer existed (B-31). Keyed on `dirty` rather than on the Discard handler so the
  // save path is covered too; the switch itself is left to the operator, who asked to
  // discard, not to discard and go.
  useEffect(() => {
    if (!dirty) setPendingSwitch(null);
  }, [dirty]);

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

  // The rail states the section being READ, which is what its aria-current="page" claims.
  // Until this effect existed that claim only held for someone who had clicked the rail:
  // scrolling down to Deletion and arming left it marking "What flags a title" as current,
  // for sighted and assistive readers alike (U-18, rule 24).
  //
  // The line a heading has to reach is the offset the rail's own jumps land on, read back
  // off the computed scroll-margin-top the stylesheet puts on .policy-section -- one
  // declaration, so a jump and the highlight can never drift, and the phone's taller
  // wrapped rail is handled for free (rule 67).
  //
  // A scroll listener rather than an IntersectionObserver, deliberately: an observer only
  // fires when a heading CROSSES the line, and the last section's heading never can. The
  // document ends first, so Deletion -- the section that arms a removal, and the one this
  // finding named -- would have stayed unmarkable. Measuring from positions has no such
  // dead zone. The cost is four rect reads, at most once a frame, and only while this page
  // is mounted; an unchanged section is a React state bail-out, not a re-render.
  const ready = draft !== null;
  useEffect(() => {
    if (!ready) return;
    const heads = SECTIONS.map((s) => [s.id, sectionRefs[s.id].current] as const).filter(
      (pair): pair is [SectionId, HTMLHeadingElement] => pair[1] !== null,
    );
    const first = heads[0];
    if (first === undefined) return;

    const pick = () => {
      // A page that does not scroll is read whole, so the first section is the one you are
      // on; there is no "further down" to have reached.
      const scrollable = document.documentElement.scrollHeight > window.innerHeight + 1;
      if (!scrollable) {
        setActiveSection(first[0]);
        return;
      }
      // At the very bottom nothing more can reach the line, so the last heading on screen
      // is the section being read. Without this the final one is permanently unreachable.
      const atBottom =
        window.innerHeight + window.scrollY >= document.documentElement.scrollHeight - 2;
      const line = parseFloat(getComputedStyle(first[1]).scrollMarginTop) || 0;
      let current: SectionId = first[0];
      for (const [id, el] of heads) {
        const top = el.getBoundingClientRect().top;
        if (atBottom ? top < window.innerHeight : top <= line + 1) current = id;
      }
      setActiveSection(current);
    };

    let frame = 0;
    const onScroll = () => {
      // Scroll fires far faster than the rail can change, so coalesce to one measurement
      // per frame -- and never read layout inside the event itself.
      if (frame) return;
      frame = requestAnimationFrame(() => {
        frame = 0;
        pick();
      });
    };
    pick();
    window.addEventListener("scroll", onScroll, { passive: true });
    window.addEventListener("resize", onScroll, { passive: true });
    return () => {
      if (frame) cancelAnimationFrame(frame);
      window.removeEventListener("scroll", onScroll);
      window.removeEventListener("resize", onScroll);
    };
  }, [ready, sectionRefs]);

  // The draft only ever seeds from a successful read, so a failed one would otherwise
  // leave the whole workspace saying "Loading…" for good. Say what happened instead.
  if (!draft) {
    if (policyFailed) {
      return <Notice tone="error">Couldn't load these settings. Reload to try again.</Notice>;
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
  // Not memoized, and cannot be: everything from here down runs after the `if (!draft)` return
  // above, where a hook would change the hook order between renders. Each is a single pass over
  // a list of at most a dozen weights, so the cost that mattered was the RE-RENDER these fed,
  // and that is what the split into PolicyRuleEditors and policyPresets addresses (R-2).
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
    invalidError instanceof ApiError && invalidError.status === 422 ? invalidError.message : null;
  const validatorDown = invalidError !== null && invalidMessage === null;

  // The savebar saves two INDEPENDENT things, so a problem in one must not hold the other
  // (PR-7). Pace and limits are un-hashed and are not part of the policy at all -- this file's
  // header is explicit that tightening a cap never voids an approval -- yet a policy off the
  // 100-point budget used to disable the one Save button, so a grace-period edit could not be
  // written until an unrelated weight was fixed. One save affordance still (rule 43): the
  // button writes whichever halves are actually savable, and says which one it is leaving.
  const policyBlocked = dirty && (Boolean(invalidMessage) || pointsLeft !== 0);
  const willSavePolicy = dirty && !policyBlocked;
  const willSavePace = paceDirty;
  const saving = save.isPending || savePace.isPending;
  // Why the policy half is held, in one clause. The numbers are NOT repeated here: the points
  // meter renders directly below this line and the 422 notice at the top of the page, so a
  // second copy of either would be the third sentence in the bar saying one thing. Points are
  // named first when both are true, because that notice is the one right beside this.
  const policyHeldBecause =
    pointsLeft !== 0 ? "the points add up to 100" : "the problem at the top of this page is fixed";

  const switchMediaType = (next: "movie" | "tv") => {
    if (next === mediaType) return;
    if (dirty) {
      setPendingSwitch(next);
      setSwitchNonce((n) => n + 1);
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
    // "Untouched", never "played": this gate's clock runs from the last play only when
    // there IS one, and otherwise from the day the file arrived (engine/dormancy.py's
    // reference_instant). It therefore keeps titles nobody has ever played, so a clause
    // saying "played in the last N" was false about exactly the items it protects most --
    // the same claim the review queue's chip used to make, on the sentence this comment
    // block below calls the one an operator scans before arming (rules 21/72).
    dormancy ? `untouched for less than ${humanDays(dormancy.threshold)}` : null,
  ].filter((c): c is string => c !== null);
  // TV's protections are built the same way, and for the same reason: every clause is
  // pushed only when its own switch is on. The line used to assert two of them flat, so
  // a policy with the season floor at 0 and mid-binge holding turned OFF still read
  // "always keeps the newest 0 seasons of a show and anyone's mid-binge" -- both false,
  // on the sentence an operator scans before arming (U-3, rules 53/61).
  //
  // The gate clauses above are deliberately NOT folded in here: they read as conditions
  // ("keeps anything watched by 3+ people") and these read as things ("keeps the newest 2
  // seasons"), so one list cannot carry both without a sentence too long to scan. The
  // protections themselves are listed in full a screen below.
  const tvKeepClauses = [
    draft.keep_last_seasons > 0
      ? `the newest ${draft.keep_last_seasons === 1 ? "season" : `${draft.keep_last_seasons} seasons`} of a show`
      : null,
    draft.keep_first_season ? "a show's first season" : null,
    draft.keep_in_progress ? "anyone's mid-binge" : null,
  ].filter((c): c is string => c !== null);
  // Branch on the caps switch: with caps off the executor skips the per-run and rolling
  // checks entirely, so claiming a hard "at most N per run" here would contradict the
  // caps-off warning below and the run itself (B-2). The switch does not touch the grace
  // countdown, which is a notice rather than a hold either way (services/grace.py).
  // A failed profile read says nothing about caps at all. The neutral "within your caps"
  // wording covers the still-LOADING case only: asserting caps are in force while the section
  // below says "Couldn't load these settings" is the contradiction B-29 names, on the one
  // sentence an operator reads before arming (rules 53/65).
  const paceClause = paceFailed
    ? null
    : !pace
      ? "removes only within your caps"
      : pace.caps_enabled
        ? `removes at most ${count(pace.max_items_per_run)} titles or ${bytes(pace.max_bytes_per_run)} per run`
        : "removes with no per-run limit until you turn limits back on";
  const paceTail = paceClause ? `, and ${paceClause}.` : ".";

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
              <h2 ref={bar.ref as RefObject<HTMLHeadingElement>} tabIndex={-1}>
                {mediaType === "tv" ? "TV policy" : "Movies policy"}
              </h2>
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
          <Notice tone="error" as="div">
            Your saved policy couldn't be read, so this shows the starting one instead. Nothing has
            changed on your server. Check the values below, then save to replace it.
          </Notice>
        )}
        {saved?.rating_rules_restored && (
          <Notice tone="warn" as="div">
            Keep well-rated titles had stopped keeping anything. Your saved rating is back below,
            unsaved. Reaper won't remove anything until you check it and save.
          </Notice>
        )}
        {pendingSwitch !== null && (
          <SwitchConfirm
            nonce={switchNonce}
            message={`You have unsaved ${kind} policy changes. Switching to ${
              pendingSwitch === "tv" ? "TV" : "Movies"
            } discards them.`}
            onDiscard={() => {
              setPendingSwitch(null);
              setMediaType(pendingSwitch);
            }}
            onKeep={() => setPendingSwitch(null)}
          />
        )}

        <nav className="settings-nav policy-rail" aria-label="Policy sections">
          {SECTIONS.map((s) => (
            <button
              key={s.id}
              className={activeSection === s.id ? "settings-tab active" : "settings-tab"}
              // Reserve the bold (active) width so switching sections never shifts the rail.
              data-label={s.label}
              // The section being read is stated, not just colored, the same as the
              // masthead and the settings rail. True on a scroll as well as a click: the
              // observer above keeps activeSection on whatever heading has reached the
              // read line.
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
                <strong>{draft.condemn_at} / 100</strong>
                {tvKeepClauses.length > 0 && (
                  <>
                    , always keeps <strong>{andList(tvKeepClauses)}</strong>
                  </>
                )}
                {paceTail}
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
                {paceTail}
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
            {presetHelp} Picking one resets the built-in points and rescales your own rules to fit
            100. Your scores stay where they are.
          </p>
          {staged !== null && (
            <p className="help">
              <strong>Staged, not saved.</strong> Review the changes below, then Save changes in the
              bar at the bottom.
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
            // The wrapping <label> encloses the help paragraph too, so without this the name is
            // the label AND the help run together -- read out in full on every value change, of
            // the one control that sets the score a title is condemned at. The signal sliders
            // above already name themselves this way.
            aria-label="Put a title on the list once it scores"
            // The warning about this threshold is rendered directly below and was reachable
            // only by leaving the slider to go looking for it (#174).
            aria-describedby={warningsDescribing("condemn_at", warningsAt("condemn_at"))}
            value={draft.condemn_at}
            onChange={(e) => update({ condemn_at: Number(e.target.value) })}
          />
          <span className="help">
            The higher you set this, the more sure Reaper has to be before it flags a title.
            Protections below still win. This only decides among titles nothing is keeping.
          </span>
        </label>
        <WarnBlock anchor="condemn_at" warnings={warningsAt("condemn_at")} />

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
            // Same reason as the threshold slider above (rule 72): the wrapping <label> takes in
            // the help text, so the name was the whole paragraph.
            aria-label="Judge a title only when there's enough to go on"
            value={draft.coverage_floor_bp}
            onChange={(e) => update({ coverage_floor_bp: Number(e.target.value) })}
          />
          <span className="help">
            Reaper judges a title on the reasons below. If it can't check enough of them, it holds
            the title in Limbo for you to decide.
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
        <WarnBlock anchor="signals" warnings={warningsAt("signals")} />

        <RemoveRulesEditor
          condemn={draft.custom_condemn}
          mediaType={mediaType}
          onCondemn={(custom_condemn) => update({ custom_condemn })}
        />
        <WarnBlock anchor="custom_condemn" warnings={warningsAt("custom_condemn")} />

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
        <WarnBlock anchor="gates" warnings={warningsAt("gates")} />

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
              warnings={warningsAt("keep_rating_rules")}
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
                    // This anchor's block serves two controls, so each takes only the warnings
                    // about its own field. Handed the whole list, this box spoke the complaint
                    // about the scope control below -- a sentence ending "or switch this to
                    // 'All shows'", where "this" resolves to a box offering no such option.
                    describedBy={warningsDescribing("keep_last", warningsAt("keep_last"), [
                      "keep_last_seasons",
                    ])}
                    onChange={(v) => update({ keep_last_seasons: Math.max(0, v) })}
                  />
                  <span>of every show</span>
                </div>
                <p className="help rule-help">
                  The most recent seasons of every show are kept outright, whatever they score.
                  There is no upper limit.
                </p>
                <SeasonAdvisory keepLast={draft.keep_last_seasons} scope={draft.keep_last_scope} />
              </li>

              <li className="rule-row">
                <span className="rule-name">Apply that to</span>
                <div className="rule-control">
                  <Segmented
                    value={draft.keep_last_scope}
                    onChange={(keep_last_scope) => update({ keep_last_scope })}
                    label="Keep-last scope"
                    // The other half of `keep_last`: the warning about "Requested only" with no
                    // Seerr connected is fixed from HERE, and this is the control it names.
                    describedBy={warningsDescribing("keep_last", warningsAt("keep_last"), [
                      "keep_last_scope",
                    ])}
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
                <p className="help rule-help">So a new viewer can still start the show.</p>
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
                        // One anchor, one field, one box: this is the control the warning's
                        // remedy names, so it takes the whole list unfiltered (#174).
                        describedBy={warningsDescribing("in_progress", warningsAt("in_progress"))}
                        onChange={(v) => update({ in_progress_hold_days: Math.max(0, v) })}
                      />
                      <span>without watching</span>
                    </div>
                    <p className="help rule-help">
                      If someone has not watched any of the show in this many days, Reaper treats
                      the show as abandoned by them and lets go of their place. When Reaper can't
                      tell when they last watched, it keeps holding. Set this longer than your watch
                      history goes back, or to 0, and Reaper keeps every season: it can't tell who
                      is partway through.
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
                {/* Outside the subtree above on purpose: one mount condition, so the anchor
                    can declare it (rule 42). */}
                <WarnBlock anchor="in_progress" warnings={warningsAt("in_progress")} />
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
                  {/* Sonarr reports one thing here -- it wants episodes it does not have
                      (`wanted_episode_count > episode_file_count`, see
                      `clients/sonarr_stats.py`) -- and an active download and an ended show
                      permanently short one aired episode look identical in it. So the switch
                      is named for what was seen, and the help text names both causes rather
                      than promising a download is under way (rule 21, and rule 72 with
                      `services/season_pruning.py`'s reason for the same check). */}
                  <span className="rule-name">Never remove a season with episodes missing</span>
                </label>
                <p className="help rule-help">
                  Keeps a season Sonarr wants more episodes for, so a removal never fights a
                  download. Sonarr says the same thing about an ended show permanently missing an
                  episode, so turn this off if that is your library.
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
                  keeps, Reaper marks it "Needs a look" and waits for you. Turn this off and Reaper
                  follows your keep rule without asking.
                </p>
              </li>
            </ul>
            <WarnBlock anchor="keep_last" warnings={warningsAt("keep_last")} />
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
        {/* Both lanes of this card, because both can be warned about and the card is the
            one surface either can be fixed from. `protect_conditions` carries the
            gate-off popularity window (`engine/policy.py:inspect`), which has nowhere
            else to go: with that protection off its window control is not rendered. */}
        <WarnBlock anchor="keep_rules" warnings={warningsAt("keep_rules")} />

        {/* A validation failure is an ERROR (red): this policy cannot be saved as-is. */}
        {invalidMessage && (
          <Notice tone="error">
            <strong>Can't save this:</strong> {invalidMessage}
          </Notice>
        )}
        {/* The check itself failing is AMBER, and it does not lock Save: the server
            checks the policy again on save either way. */}
        {validatorDown && (
          <Notice tone="warn">
            Couldn't check this draft just now, so any problem with it can't be shown here. You can
            still save: the server checks it again when you do.
          </Notice>
        )}
        {/* Warnings live beside their controls; only one no anchor claims lands here. */}
        <WarnBlock anchor="unanchored" warnings={unanchoredWarnings} />

        <p className="hash">
          {validation && (
            <>
              {kind} policy <code>{validation.policy_hash.slice(0, 12)}</code> · saving does not arm
              anything
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
          <Notice tone="error">
            Your saved caps and grace couldn't be read, so these show the starting ones. Nothing has
            changed on your server, but a scan won't remove anything until you check these and save.
          </Notice>
        )}

        {pace === null ? (
          paceFailed ? (
            // No reload advice (#195): this sits inside an editor whose savebar may be holding
            // unsaved policy edits, and a reload takes them with no ask.
            <Notice tone="error">Couldn't load these settings.</Notice>
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
              <Notice tone="warn" inline>
                No cap on run size. A run can remove everything you've approved at once. Deletion
                still needs the password and your approval of the list.
              </Notice>
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
                  {/* The ceilings the server enforces, stated here so an out-of-range
                      number is pulled back in the box instead of coming home a 422. */}
                  <FixedQuantity
                    value={pace.max_items_per_run}
                    suffix="titles"
                    min={1}
                    max={1000}
                    width="narrow"
                    ariaLabel="Most titles per run"
                    onChange={(v) => updatePace({ max_items_per_run: v })}
                  />
                  <FixedQuantity
                    value={pace.max_items_per_30d}
                    suffix="titles"
                    min={1}
                    width="narrow"
                    ariaLabel="Most titles per 30 days"
                    onChange={(v) => updatePace({ max_items_per_30d: v })}
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
                  One run takes 1,000 titles at most.
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
                {/* A notice, not a hold: nothing on the deletion path reads this window
                    (services/grace.py), so help promising time "before removal" was false. */}
                <span className="help">
                  How long a flagged title shows as leaving, so someone can rescue it.
                </span>
              </span>

              <span className="ex-label">Unknown-size items</span>
              <span className="ex-ctl">
                <FixedQuantity
                  value={pace.max_unmeasured_per_run}
                  suffix="per run"
                  min={0}
                  max={25}
                  width="narrow"
                  ariaLabel="Items with an unknown size"
                  // The one setting that lets deletions past the size caps, so the warning
                  // about it is worth hearing from the box rather than after it.
                  describedBy={warningsDescribing(
                    "max_unmeasured_per_run",
                    warningsAt("max_unmeasured_per_run"),
                  )}
                  onChange={(v) => updatePace({ max_unmeasured_per_run: v })}
                />
                <span className="help">
                  Kept by default. Size caps can't measure them. Set 0 to always keep, 25 at most.
                </span>
                <WarnBlock
                  anchor="max_unmeasured_per_run"
                  warnings={warningsAt("max_unmeasured_per_run")}
                />
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
          Whether Reaper is allowed to remove anything at all. One switch for all of Reaper, movies
          and TV alike. Turning it on always asks for the admin password. Turning it off never does.
        </p>
        <DeletionToggle />

        {/* THE save bar: one place to save whatever is dirty -- the policy draft, the pace
            draft, or both (a preset stages both). Pinned to the viewport bottom while it
            has something to say, so Save is never a scroll away from the edit. */}
        {(dirty || paceDirty) && (
          <div className="savebar">
            <span className="savebar-what">
              <strong>
                Unsaved changes:{" "}
                {[dirty ? `${kind} policy` : null, paceDirty ? "pace and limits" : null]
                  .filter(Boolean)
                  .join(" · ")}
              </strong>
              <br />
              {/* What Save will ACTUALLY write, not what is merely dirty: a held-back policy
                  half must not be described as applying on the next scan (PR-7). */}
              {willSavePolicy && willSavePace
                ? "Policy changes apply on the next scan, which starts by itself after saving. Pace applies immediately."
                : willSavePolicy
                  ? `Saves only your ${kind} policy. The ${otherKind} one is untouched. It applies on the next scan, which starts by itself after saving.`
                  : willSavePace
                    ? "Pace applies immediately. Changing a limit never affects a run you've already approved."
                    : null}
              {/* Only when the OTHER half will still be written: that is the new fact, and the
                  one an operator would otherwise have to infer. With the policy alone dirty
                  the button is simply disabled and the notice beside the cause already says
                  why, so a line here would be the bar's third sentence on one subject. */}
              {policyBlocked && willSavePace && (
                <Notice tone="warn" className="budget-notice" as="span">
                  Save writes pace and limits only. Your {kind} policy can't go with it until{" "}
                  {policyHeldBecause}.
                </Notice>
              )}
              {/* The rescale recovery. Rendered here, beside Save, because it is about
                  what you are about to write -- and from the response FLAGS, not the
                  warning list, which is built by re-validating the draft and so can never
                  carry anything about how the policy loaded. The louder `fell_back`
                  recovery renders at the top of the page instead, unconditionally, so no
                  gate can hide it. */}
              {saved?.needs_save && !saved.fell_back && (
                <Notice tone="warn" className="budget-notice" as="span">
                  Your points have been spread to add up to 100. Nothing has changed on your server
                  yet. Each rule keeps the same share it had, so your scores stay where they are.
                  Review and save.
                </Notice>
              )}
              {dirty && <PointsBudget builtIn={builtInWeight} yours={yourWeight} />}
            </span>
            <button
              className="ghost"
              onClick={() => {
                bar.leaving();
                if (dirty) setDraft(saved?.body ?? null);
                if (paceDirty) setPace(savedPace ?? null);
                // Discard is the larger silence of the two: up to fifty controls revert at
                // once and the bar that did it disappears, so there was nothing to notice
                // except that the page had stopped offering to save.
                announce("Changes discarded.");
              }}
            >
              Discard
            </button>
            <button
              className="primary"
              // Enabled when EITHER half can be written. A blocked policy no longer holds
              // pace and limits hostage (PR-7); the line above says which half is waiting.
              disabled={(!willSavePolicy && !willSavePace) || saving}
              onClick={() => {
                bar.leaving();
                // Two independent saves; each failure renders its own notice below. The
                // blocked half is skipped, never sent for the server to refuse.
                if (willSavePolicy) save.mutate(draft);
                if (willSavePace && pace) savePace.mutate(pace);
              }}
            >
              {saving ? "Saving…" : "Save changes"}
            </button>
            {save.error && <Notice tone="error">{save.error.message}</Notice>}
            {savePace.error && <Notice tone="error">{savePace.error.message}</Notice>}
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
              The simulator request failed, so there are no numbers to show. Nothing about your
              library or your saved policy has changed. Adjust any control to try again.
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

// Re-exported so the preset helpers keep their old import path while callers and tests move
// over to ./policyPresets, which now owns them (R-2).
export { andList, weightsMatchMix } from "./policyPresets";
