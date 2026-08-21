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
import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type CSSProperties,
  type RefObject,
} from "react";
import { Trans, useTranslation } from "react-i18next";
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
  type RewatchOddsFit,
  type SignalSetting,
} from "../api";
import { announce } from "../announce";
import { REMOVES_ITS_ROW, useRemovalFocus, useSavebarFocus } from "../focus";
import { DocLink, HelpIcon } from "../docs/DocLink";
import { bytes, count, humanDays } from "../format";
import i18next from "../i18n";
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
import {
  APPLIES_ON_NEXT_SCAN,
  Outcome,
  RESCAN_HEADING,
  RESCAN_QUEUED_LEAD,
  StaleNotice,
} from "./PolicySimulator";
import { FixedQuantity, QuantityInput, SIZE_UNITS, TIME_UNITS } from "./QuantityInput";
import { Segmented } from "./Segmented";
import { probeSaid, rampFill, rampStrip, rampUnits } from "./signalRamp";
import { SETTLE_MS, usePolicyProbe } from "../usePolicyProbe";
import { useScanStatus } from "../useScanStatus";
import { Switch } from "./Switch";
import { Notice } from "./Notice";
import { SwitchConfirm, useSwitchConfirm } from "./SwitchConfirm";

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

/** The field name the server gives a warning about ONE setting of ONE protection.
 *  `engine/policy_warnings.py` builds every member of this family as
 *  `f"gates.{gate}.{setting}"`, where
 *  the setting is a field of the gate row itself -- which is why the suffix is typed off
 *  `GateSetting` rather than spelled out here: the shape both ends already share is the
 *  declaration, so a suffix the server cannot send does not compile (rule 144).
 *
 *  Generic over the gate id on purpose. Four of these arrive today, one per protection, and a
 *  fifth protection warning about any of the three settings binds here with no change;
 *  anything this cannot reach still renders in the `gates` block under the list. */
function gateWarningField(gate: string, setting: keyof Omit<GateSetting, "gate">): string {
  return `gates.${gate}.${setting}`;
}

/** One protection: a switch, a plain-English label and help, and -- where it has one -- a
 *  threshold in the units a person thinks in. */
function GateRow({
  gate,
  onChange,
  onRemove,
  warnings,
}: {
  gate: GateSetting;
  onChange: (g: GateSetting) => void;
  /** Take the row out of the body entirely. What "off" means for a retired id: there is no
   *  such protection to store in either position, and the save boundary refuses the id
   *  whether it is on or off (`GateSettingIn._must_be_authorable`). */
  onRemove: () => void;
  /** Every warning rendered at the `gates` anchor, unfiltered. `warningsDescribing` numbers
   *  ids by position within THAT list, so narrowing before handing it over would point each
   *  box at the wrong notice. */
  warnings: PolicyWarning[];
}) {
  const { t } = useTranslation();
  // Sibling of the simulator's spared-by row (rule 72), and the two fallbacks deliberately
  // DIFFER. There, an id appears once in a tally, so `UNNAMED_GATE_LABEL` reads correctly:
  // "Another protection, 7". Here it names a switch, and two ids this build has no copy for
  // would draw two controls carrying one name and no help, leaving the operator unable to
  // tell which protection they were turning off -- so the label stays per-id. Prefer a
  // title-cased slug the operator can at least tell apart over rule 21's nicer sentence,
  // because a control has to be identifiable before it can be plain.
  // Unreachable for every id the engine has (`GATE_META` covers all of `GateId`), and the SPA
  // ships inside the server's own image, so reaching this at all needs a browser holding a
  // stale bundle against a newer server. Rule 66's fallback, not a surface anyone should meet.
  const meta = GATE_META[gate.gate] ?? { label: titleCase(gate.gate), help: "" };
  // What this row's boxes point `aria-describedby` at. The block rendering these sits under
  // the whole list, so reaching one meant browsing the page in document order: a keyboard
  // operator moving control to control never met it (#189). Every gate warning the server
  // sends names one setting of one protection, so each lands on exactly the box that fixes
  // it -- rule 42's sentence, delivered to a reader standing on the control.
  const describes = (setting: keyof Omit<GateSetting, "gate">) =>
    warningsDescribing("gates", warnings, [gateWarningField(gate.gate, setting)]);

  return (
    <li className="rule-row">
      <label className="toggle rule-toggle">
        <Switch
          checked={gate.enabled}
          // The only switch in the product that removes its own row, so it is the only one
          // that has to say where focus goes afterwards. Sibling of `RatingFloorRow`'s bar
          // removal 280 lines below, which wears the same marker for the same reason
          // (rule 72): activating a control that unmounts itself drops focus to `<body>` and
          // the next Tab restarts at the top of a form this long (#173).
          removesRow={meta.retired}
          // Turning a RETIRED row off takes it out of the body rather than storing it off,
          // because there is nothing to store: the save boundary refuses the id in either
          // position, so `{...gate, enabled: false}` left the page unsavable and the notice
          // below naming an exit that did not work (#627). Every live protection keeps the
          // ordinary two-position switch, and `meta.retired` is exactly the server's
          // "no policy row can carry this id" -- `tests/test_api_type_mirror.py` pins the two
          // sets against each other, both directions.
          onChange={(enabled) =>
            meta.retired && !enabled ? onRemove() : onChange({ ...gate, enabled })
          }
          describedBy={describes("enabled")}
        />
        <span className="rule-name">{meta.label}</span>
      </label>
      {meta.help && <p className="help rule-help">{meta.help}</p>}

      {/* A retired gate only reaches this list when the loader could NOT convert it -- the list
          it names is gone -- and `scan_runner.build_gates` then refuses every scan over it
          (`ScanConfigError`). The row rendered through the ordinary path with a live-sounding
          label and no warning, so the one switch that was stopping every scan looked like an
          ordinary healthy protection. Said beside that switch, which is what fixes it
          (rules 25, 42). The sentence says what the switch now does, since the row leaves the
          page on that click and nothing else on screen would account for it. */}
      {meta.retired && gate.enabled && (
        <Notice tone="warn" inline>
          {t("policyEditor.gateRow.retiredNotice")}
        </Notice>
      )}

      {gate.enabled && meta.unit === "days" && (
        <div className="rule-control">
          <span>{meta.lead ?? t("policyEditor.atLeast")}</span>
          <QuantityInput
            value={gate.threshold}
            units={TIME_UNITS}
            min={meta.min ?? 5}
            ariaLabel={t("policyEditor.gateRow.thresholdAriaLabel", { label: meta.label })}
            describedBy={describes("threshold")}
            onChange={(v) => onChange({ ...gate, threshold: v })}
          />
        </div>
      )}
      {gate.enabled && meta.unit === "people" && (
        <div className="rule-control">
          <span>{t("policyEditor.atLeast")}</span>
          <FixedQuantity
            value={gate.threshold}
            suffix={t("policyEditor.units.person", { n: gate.threshold })}
            min={1}
            width="narrow"
            ariaLabel={t("policyEditor.gateRow.thresholdAriaLabel", { label: meta.label })}
            describedBy={describes("threshold")}
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
              ariaLabel={meta.window.aria}
              describedBy={describes("window_days")}
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
  scale: "ten" | "pct";
  votes: boolean;
  defFloor: number; // in tenths (7.5 -> 75; 75% -> 75)
  defVotes: number;
};

const RATING_META: Record<RatingSource, RatingSourceMeta> = {
  imdb: { scale: "ten", votes: true, defFloor: 65, defVotes: 5000 },
  rotten_tomatoes_critic: { scale: "pct", votes: false, defFloor: 75, defVotes: 0 },
  rotten_tomatoes_audience: { scale: "pct", votes: false, defFloor: 80, defVotes: 0 },
  metacritic: { scale: "pct", votes: false, defFloor: 70, defVotes: 0 },
  tmdb: { scale: "ten", votes: true, defFloor: 70, defVotes: 500 },
};
const RATING_ORDER: RatingSource[] = [
  "imdb",
  "rotten_tomatoes_critic",
  "rotten_tomatoes_audience",
  "metacritic",
  "tmdb",
];

/** The source's display name, read at call time (not a frozen module constant) so a language
 *  change is picked up the next time the editor opens -- same shape as `jobMeta` in
 *  `JobsPanel.tsx`: each source gets its own literal `t()` call, since a computed key is
 *  unreadable to the missing-key gate. */
function ratingSourceLabel(source: RatingSource): string {
  switch (source) {
    case "imdb":
      return i18next.t("policyEditor.ratingSource.imdb");
    case "rotten_tomatoes_critic":
      return i18next.t("policyEditor.ratingSource.rottenTomatoesCritic");
    case "rotten_tomatoes_audience":
      return i18next.t("policyEditor.ratingSource.rottenTomatoesAudience");
    case "metacritic":
      return i18next.t("policyEditor.ratingSource.metacritic");
    case "tmdb":
      return i18next.t("policyEditor.ratingSource.tmdb");
  }
}

/** One title clears one bar if `describe` reads true; the summary reads the whole set. */
function describeBar(rule: RatingRule): string {
  const meta = RATING_META[rule.source];
  const label = ratingSourceLabel(rule.source);
  if (meta.scale === "pct")
    return i18next.t("policyEditor.ratingBar.pct", { label, floor: rule.floor });
  const votes =
    meta.votes && rule.min_votes > 0
      ? i18next.t("policyEditor.ratingBar.fromVotes", { votes: rule.min_votes.toLocaleString() })
      : "";
  return i18next.t("policyEditor.ratingBar.ten", {
    score: (rule.floor / 10).toFixed(1),
    label,
    votes,
  });
}

/** The field name the server gives a warning about ONE setting of ONE rating bar.
 *  `engine/policy_warnings.py` builds every member of this family as
 *  `f"keep_rating_rules.{source}.{setting}"`, the same shape as the `gates.` family above and
 *  for the same reason: the source keys the row uniquely, because `PolicyBody` refuses two
 *  rules on one source. The suffix is typed off `RatingRule` so a setting the server cannot
 *  send does not compile (rule 144).
 *
 *  Generic over the source and the setting on purpose. Three warnings arrive today, all about
 *  `floor`; one about a vote floor would bind here with no change. The card's own complaint,
 *  which is about no bars existing at all, is not in this family and stays on the block. */
function ratingWarningField(source: string, setting: keyof Omit<RatingRule, "source">): string {
  return `keep_rating_rules.${source}.${setting}`;
}

/** One editable bar, as a row of the shared aligned table: the source name in the common
 *  column, then its threshold (and vote floor, where the source counts votes) in the
 *  app's one quantity control. */
function RatingBarRow({
  rule,
  onChange,
  onRemove,
  warnings,
}: {
  rule: RatingRule;
  onChange: (r: RatingRule) => void;
  onRemove: () => void;
  /** Every warning rendered at the `keep_rating_rules` anchor, unfiltered. `warningsDescribing`
   *  numbers ids by position within THAT list, so narrowing before handing it over would point
   *  each box at the wrong notice. */
  warnings: PolicyWarning[];
}) {
  const { t } = useTranslation();
  const meta = RATING_META[rule.source];
  const label = ratingSourceLabel(rule.source);
  // What this row's boxes point `aria-describedby` at. The block rendering these sits under the
  // whole list of bars, so reaching one meant browsing the card in document order (#189). The
  // filter is what keeps a complaint about IMDb off the Rotten Tomatoes row -- the worry the
  // issue was filed on, answered by the field rather than by a grouping decision.
  const describes = (setting: keyof Omit<RatingRule, "source">) =>
    warningsDescribing("keep_rating_rules", warnings, [ratingWarningField(rule.source, setting)]);
  return (
    <div className="bar-line">
      <span className="bar-src">{label}</span>
      <span className="bar-set">
        <span>{t("policyEditor.atLeast")}</span>
        {meta.scale === "ten" ? (
          <>
            <FixedQuantity
              value={(rule.floor / 10).toFixed(1)}
              suffix={t("policyEditor.units.outOfTen")}
              min={0}
              max={10}
              step={0.1}
              width="narrow"
              ariaLabel={t("policyEditor.ratingBar.scoreAriaLabel", { label })}
              describedBy={describes("floor")}
              onChange={(v) => onChange({ ...rule, floor: Math.round(v * 10) })}
            />
            {meta.votes && (
              <>
                <span>{t("policyEditor.ratingBar.votesFromLabel")}</span>
                <FixedQuantity
                  value={rule.min_votes}
                  suffix={t("policyEditor.units.votesSuffix")}
                  min={0}
                  step={100}
                  ariaLabel={t("policyEditor.ratingBar.voteFloorAriaLabel", { label })}
                  describedBy={describes("min_votes")}
                  onChange={(v) => onChange({ ...rule, min_votes: Math.max(0, v) })}
                />
              </>
            )}
          </>
        ) : (
          <FixedQuantity
            value={rule.floor}
            suffix={t("policyEditor.units.percent")}
            min={0}
            max={100}
            step={1}
            width="narrow"
            ariaLabel={t("policyEditor.ratingBar.percentAriaLabel", { label })}
            describedBy={describes("floor")}
            onChange={(v) => onChange({ ...rule, floor: v })}
          />
        )}
      </span>
      <button
        {...REMOVES_ITS_ROW}
        type="button"
        className="bar-x"
        onClick={onRemove}
        aria-label={t("policyEditor.ratingBar.removeAriaLabel", { label })}
      >
        ✕
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
  /** Every warning the `keep_rating_rules` anchor claims: the card's own, plus one per bar.
   *  Read twice below, by the block under the list and by each row in it, from this ONE array
   *  -- `warningsDescribing` numbers ids by position within it, so the two ends must never be
   *  handed different lists (#189).
   *
   *  `PolicyWarning`, not a structural subset of it: the looser type is what let this card
   *  render its own copy of `WarnBlock`'s markup, since the value it held could not be handed
   *  to the shared component. */
  warnings: PolicyWarning[];
  onGate: (g: GateSetting) => void;
  onRules: (r: RatingRule[]) => void;
  onMatch: (m: "any" | "all") => void;
}) {
  const { t } = useTranslation();
  const used = new Set(rules.map((r) => r.source));
  const available = RATING_ORDER.filter((s) => !used.has(s));
  const addSource = (source: RatingSource) => {
    const meta = RATING_META[source];
    onRules([
      ...rules,
      { source, floor: meta.defFloor, min_votes: meta.votes ? meta.defVotes : 0 },
    ]);
  };
  const joiner =
    match === "any"
      ? i18next.t("policyEditor.ratingFloor.joinerOr")
      : i18next.t("policyEditor.ratingFloor.joinerAnd");
  const summary =
    rules.length === 0
      ? t("policyEditor.ratingFloor.emptySummary")
      : t("policyEditor.ratingFloor.summary", { bars: rules.map(describeBar).join(joiner) });

  // The same shape as the keep-tag chips 290 lines above, on the same ~1,900-line form: the ✕
  // removes the row it lives in, so without this focus falls to `<body>` and the next Tab
  // restarts at the top (#173). Missed in that sweep and caught by a rule 72 pass over it.
  const addSourceRef = useRef<HTMLSelectElement>(null);
  const bars = useRemovalFocus(addSourceRef);

  return (
    <div className="rules-card">
      <label className="toggle card-head">
        <Switch checked={gate.enabled} onChange={(enabled) => onGate({ ...gate, enabled })} />
        <span className="rule-name">{t("policyEditor.ratingFloor.cardTitle")}</span>
      </label>
      <p className="help rule-help">{t("policyEditor.ratingFloor.clearsBars", { match })}</p>
      {gate.enabled && (
        <>
          {rules.length > 0 && (
            <div className="bar-table" ref={bars.ref as RefObject<HTMLDivElement>}>
              {rules.map((rule, i) => (
                <RatingBarRow
                  key={rule.source}
                  rule={rule}
                  warnings={warnings}
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
                aria-label={t("policyEditor.ratingFloor.addSourceAriaLabel")}
                onChange={(e) => {
                  if (e.target.value) addSource(e.target.value as RatingSource);
                }}
              >
                <option value="">{t("policyEditor.ratingFloor.addSourcePlaceholder")}</option>
                {available.map((s) => (
                  <option key={s} value={s}>
                    {ratingSourceLabel(s)}
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
                label={t("policyEditor.ratingFloor.matchLabel")}
                options={[
                  ["any", t("policyEditor.ratingFloor.matchAny")],
                  ["all", t("policyEditor.ratingFloor.matchAll")],
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
          {mediaType === "tv" && <p className="help">{t("policyEditor.ratingFloor.tvNote")}</p>}
        </>
      )}
    </div>
  );
}

/** The 100-point removal budget, in the savebar. Weights can be labeled as points only
 *  because the server refuses any policy whose removal weights do not total exactly 100
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
  const { t } = useTranslation();
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
          <Trans
            i18nKey="policyEditor.pointsBudget.totalUsed"
            values={{ total }}
            components={{ totalNum: <strong /> }}
          />
        </span>
        {left === 0 ? (
          <span className="muted">{pointsSplit(builtIn, yours)}</span>
        ) : (
          <span className={left < 0 ? "budget-over-text" : "muted"}>
            {left < 0
              ? t("policyEditor.pointsBudget.over", { n: -left })
              : t("policyEditor.pointsBudget.leftToGiveOut", { n: left })}
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
            ? t("policyEditor.pointsBudget.overNotice", { total, n: -left })
            : t("policyEditor.pointsBudget.underNotice", { total, n: left })}
        </Notice>
      )}
    </span>
  );
}

/** How the 100 points are split: "70 built in, 30 yours", or "all on built-in signals"
 *  when the operator has written no removal rules. Both arguments are point totals, not
 *  rule counts, so the two numbers always add up to the total shown beside them. */
function pointsSplit(builtIn: number, yours: number): string {
  return yours > 0
    ? i18next.t("policyEditor.pointsBudget.split", { builtIn, yours })
    : i18next.t("policyEditor.pointsBudget.allBuiltIn");
}

/** One signal: a plain-English label, its help, a slider, and the flat points it can add.
 *  Removal weights total exactly 100, so the weight IS the number of points, and the row
 *  reads "up to N points" because a signal only adds its full number at the far end of
 *  its range. */
function SignalRow({
  signal,
  reachDays,
  shipped,
  onChange,
}: {
  signal: SignalSetting;
  /** How far back the watch mirror goes, or null when the scan did not record it. */
  reachDays: number | null;
  /** This signal's bounds as Reaper ships them, for the way back. */
  shipped: SignalSetting | undefined;
  onChange: (s: SignalSetting) => void;
}) {
  const { t } = useTranslation();
  const meta = SIGNAL_META[signal.signal] ?? { label: titleCase(signal.signal), help: "" };

  return (
    <li className="rule-row">
      <div className="rule-name-row">
        <span className="rule-name">{meta.label}</span>
        <span className="rule-strength">
          {signal.weight === 0 ? (
            <span className="muted">{t("policyEditor.signalRow.off")}</span>
          ) : (
            // Points, not a share, and no second number beside it: removal weights total
            // exactly 100, so the weight IS what it adds. "up to" is not hedging -- a
            // signal ramps, and adds its full number only at the far end of its range.
            <Trans
              i18nKey="policyEditor.signalRow.upToPoints"
              values={{ weight: signal.weight }}
              components={{
                mutedLead: <span className="muted" />,
                weightNum: <strong />,
                mutedTail: <span className="muted" />,
              }}
            />
          )}
        </span>
      </div>
      {signal.weight === 0 ? (
        <p className="help rule-help">{t("policyEditor.signalRow.zeroWeightHelp")}</p>
      ) : (
        meta.help && <p className="help rule-help">{meta.help}</p>
      )}
      <input
        type="range"
        min={0}
        max={100}
        value={signal.weight}
        aria-label={t("policyEditor.signalRow.weightAriaLabel", { label: meta.label })}
        onChange={(e) => onChange({ ...signal, weight: Number(e.target.value) })}
      />
      {signal.weight > 0 && (
        <SignalRamp
          signal={signal}
          label={meta.label}
          reachDays={reachDays}
          shipped={shipped}
          onChange={onChange}
        />
      )}
    </li>
  );
}

/** Where a signal starts earning its points and where it earns all of them.
 *
 *  This was set and never shown. "Up to 10 points" is unreadable without it -- ten points on
 *  a library of well-rated titles is ten points that can never be earned, and the panel row
 *  underneath said `0  IMDb 6.4` with nothing naming the 6.0 it fell short of (#410).
 *
 *  **One box or two, decided by the signal's shape, and the difference is arithmetic rather
 *  than taste.** A shortfall signal ramps how far BELOW its bound a value sits, which works
 *  out to depend on `saturate_at - floor` alone: measured against `evaluate_signal`, the
 *  pairs (0,60), (10,70) and (40,100) score identically at every rating, and full points
 *  always land at zero. A second box there would be a control that provably does nothing, so
 *  editing writes the gap back as `floor: 0` -- one canonical spelling of the same curve.
 *
 *  Hidden at weight 0 rather than disabled, matching the gates: a signal worth no points has
 *  no range worth reading (rule 41). */
/** How wide a ramp box has to be, as the custom property `14-policy-editor.css` reads.
 *
 *  One declaration for a value the TSX and the stylesheet must agree on (rule 67): the
 *  component knows what the field can hold, the stylesheet knows the chrome it holds it in,
 *  and neither can size the box alone. */
function rampBoxWidth(widest: string): CSSProperties {
  return { "--ramp-chars": widest.length } as CSSProperties;
}

function SignalRamp({
  signal,
  label,
  reachDays,
  shipped,
  onChange,
}: {
  signal: SignalSetting;
  label: string;
  reachDays: number | null;
  /** This signal's bounds as Reaper ships them, or undefined where the server sent none. */
  shipped: SignalSetting | undefined;
  onChange: (s: SignalSetting) => void;
}) {
  const { t } = useTranslation();
  const units = rampUnits(signal.signal);
  const strip = rampStrip(signal.signal, signal.floor, signal.saturate_at);
  if (!units) return null;

  // Two controls, split on rule 40's line: a changeable unit gets the picker, a fixed one
  // gets the suffix box. `QuantityInput` stores and returns the BASE value -- days, bytes --
  // which is what the policy body holds, so nothing is converted on the way through; it just
  // draws 1825 as "5 years" and hands 1825 back.
  //
  // `max` is spread rather than passed, because `exactOptionalPropertyTypes` treats an
  // explicit `undefined` as a value and refuses it.
  const box = (
    value: number,
    ariaLabel: string,
    onNext: (stored: number) => void,
    bounds: { min?: number; max?: number } = {},
  ) =>
    units.unitKind !== "fixed" ? (
      <QuantityInput
        value={value}
        units={units.unitKind === "time" ? TIME_UNITS : SIZE_UNITS}
        min={bounds.min ?? 0}
        ariaLabel={ariaLabel}
        onChange={onNext}
      />
    ) : (
      <FixedQuantity
        // Formatted to the step's own precision, so a tenths box reads "6.0" and not "6".
        // `FixedQuantity` renders `String(value)`, which drops a trailing zero and makes a
        // decimal control look like a whole-number one.
        value={units.step < 1 ? units.fromStored(value).toFixed(1) : units.fromStored(value)}
        suffix={units.unit}
        step={units.step}
        // The standard width, not `narrow`. Narrow is 3.6rem, and a dormancy far end is four
        // digits plus the browser's spinner: "1825" came out clipped to "182". No new size
        // either way (rule 40) -- this is the other one that already exists.
        min={bounds.min ?? 0}
        {...(bounds.max === undefined ? {} : { max: bounds.max })}
        ariaLabel={ariaLabel}
        onChange={(next) => onNext(units.toStored(next))}
      />
    );

  return (
    <>
      <div className="rule-control rule-ramp">
        {units.shape === "shortfall" ? (
          <label
            className="ramp-field"
            style={units.unitKind === "fixed" ? rampBoxWidth(units.widest) : undefined}
          >
            <span>{units.nearLabel}</span>
            {box(
              signal.saturate_at - signal.floor,
              t("policyEditor.signalRamp.stopsAdding", { label }),
              (stored) =>
                // Floor back to zero with it: the pair carries one degree of freedom, and
                // leaving a stale floor behind would keep a second number in the body that
                // nothing reads and nobody can see.
                onChange({ ...signal, floor: 0, saturate_at: Math.max(1, stored) }),
            )}
          </label>
        ) : (
          <>
            <label
              className="ramp-field"
              style={units.unitKind === "fixed" ? rampBoxWidth(units.widest) : undefined}
            >
              <span>{units.nearLabel}</span>
              {box(
                signal.floor,
                t("policyEditor.signalRamp.startsAdding", { label }),
                // The server refuses `floor >= saturate_at` outright, so the box cannot
                // offer it: a policy that will not save is not a state to let an operator
                // type their way into and discover at the save bar.
                (stored) =>
                  onChange({ ...signal, floor: Math.min(stored, signal.saturate_at - 1) }),
                { max: units.fromStored(signal.saturate_at - 1) },
              )}
            </label>
            <label
              className="ramp-field"
              style={units.unitKind === "fixed" ? rampBoxWidth(units.widest) : undefined}
            >
              <span>{units.farLabel}</span>
              {box(
                signal.saturate_at,
                t("policyEditor.signalRamp.addsAll", { label }),
                (stored) =>
                  onChange({ ...signal, saturate_at: Math.max(stored, signal.floor + 1) }),
                { min: units.fromStored(signal.floor + 1) },
              )}
            </label>
          </>
        )}
      </div>
      {/* The way back. Making these editable made them losable: 1825 is the measured point
          the rewatch curve flattens, not a number anyone remembers, and the presets restore
          weights only. Shown only once the value has actually moved, so it is an undo rather
          than a permanent button.
          It named the value it goes to and no longer does: the near bound is usually the one
          the operator did not touch, so half the label restated a number already on screen in
          the box above it. The boxes and the strip answer "where did it go" the instant it is
          pressed, and the savebar's Discard is still there if the answer is unwelcome.
          Bounds alone, never the weight: removal weights total exactly 100, and putting one
          back on its own would break the budget the save bar enforces. */}
      {shipped &&
        (signal.floor !== shipped.floor || signal.saturate_at !== shipped.saturate_at) && (
          <p className="ramp-restore">
            <button
              type="button"
              className="link-btn"
              // The visible text is the same on every signal, so on its own it announces as
              // three identical buttons with nothing to tell them apart -- the failure the
              // threshold-naming test above this file's fixtures exists for. The name leads
              // with the visible text, so it still satisfies label-in-name for anyone
              // speaking it, and adds the signal it belongs to.
              aria-label={t("policyEditor.signalRamp.setDefaultAriaLabel", { label })}
              onClick={() =>
                onChange({
                  ...signal,
                  floor: shipped.floor,
                  saturate_at: shipped.saturate_at,
                })
              }
            >
              {t("policyEditor.signalRamp.setDefaultButton")}
            </button>
          </p>
        )}
      {/* Everything below this line is what the setting DOES, and nothing in it is a
          control: the strip draws the range, the sentence says what a title earns under it.
          One lighter panel around the pair, so the card reads as settings first and
          consequences second rather than as six alternating rows.

          The example keeps its bold numbers but loses its own fill -- inside the panel a
          second gray box on a gray box just draws a border nobody needs. */}
      <div className="ramp-shows">
        {/* The range, drawn rather than restated. This REPLACES the sentence that used to sit
            here: it said the same thing the picture says, and two grammars for one fact is
            the restatement rule 144 is about. What the picture adds is the direction -- a
            rating charges leftward and dormancy rightward, and no shared sentence carries
            that without the operator holding both rules in their head. */}
        {strip && (
          <div className="ramp-strip" aria-hidden="true">
            <div className="ramp-strip-track">
              <div
                className="ramp-strip-fill"
                style={{
                  left: `${strip.fillFrom}%`,
                  width: `${strip.fillTo - strip.fillFrom}%`,
                  background: rampFill(strip),
                }}
              />
              <div className="ramp-strip-bar" style={{ left: `${strip.bar}%` }} />
            </div>
            <div className="ramp-strip-scale">
              <span>{strip.scaleFrom}</span>
              <span>{strip.scaleTo}</span>
            </div>
          </div>
        )}
        <SignalProbe signal={signal} reachDays={reachDays} />
      </div>
    </>
  );
}

/** What a title would earn under the range above, worked out by the engine.
 *
 *  The number comes from `POST /api/policy/probe`, which runs the same `evaluate_signal` a
 *  scan runs. Computing it here instead would be a second scorer sitting beside the control
 *  that tunes deletions, free to drift from the one that decides and reading as
 *  authoritative while it did (rule 3/22).
 *
 *  **There is no slider.** It had one, and a second range control under a weight slider
 *  reads as another setting: the operator sees two tracks and no way to tell which one
 *  changes their policy. The value is chosen instead, and the sentence re-asks the engine
 *  as the range above is edited, which is where "live" belongs -- the answer moves when the
 *  thing it is about moves.
 *
 *  All three states render (rule 17/36). A preview that showed a stale number while the next
 *  was in flight, or fell silent when the read failed, would be a confident answer to a
 *  question nobody answered -- which is the whole failure this card exists to fix. */
function SignalProbe({ signal, reachDays }: { signal: SignalSetting; reachDays: number | null }) {
  const { t } = useTranslation();
  const units = rampUnits(signal.signal);
  // Which title to describe. Half way up the ramp, because that is the only choice that
  // MOVES with the setting: an example pinned to an end reads the same whatever the operator
  // types, which is an example that teaches nothing about the control it sits under.
  //
  // The dormancy ramp opened at the watch mirror's edge instead, and it was wrong whenever
  // the history was deep. That signal cannot read past the edge, so the edge is the most a
  // title can present -- but a mirror reaching 8 years against a far end of 5 puts it past
  // the point the signal already adds in full, and the example froze at "70 of these 70
  // points" no matter what either box said.
  //
  // So the edge is used only where it BINDS, below the far end, which is exactly the case it
  // was added for: there it is both a moving example and the ceiling the history imposes.
  const capped =
    units?.boundedByHistory === true && reachDays !== null && reachDays < signal.saturate_at;
  const value = capped
    ? Math.round(reachDays)
    : Math.round((signal.floor + signal.saturate_at) / 2);

  const probe = useMemo(
    () =>
      units
        ? ({
            kind: "signal",
            signal: signal.signal,
            weight: signal.weight,
            saturate_at: signal.saturate_at,
            floor: signal.floor,
            // The bounds go as stored; the value is the title's FACT, which SIZE reads in a
            // different unit from its bounds. `probeValue` is identity everywhere else.
            value: units.probeValue ? units.probeValue(value) : value,
          } as const)
        : null,
    [units, signal.signal, signal.weight, signal.saturate_at, signal.floor, value],
  );
  const { answer, pending, failed } = usePolicyProbe(probe);
  if (!units) return null;

  const said =
    answer && !pending ? probeSaid(signal.signal, value, answer.points, signal.weight) : null;

  return (
    <>
      <p className="ramp-said">
        {failed ? (
          t("policyEditor.signalProbe.failed")
        ) : said ? (
          <Trans
            i18nKey="policyEditor.signalProbe.said"
            values={{
              lead: said.lead,
              value: said.value,
              points: said.points,
              weight: said.weight,
            }}
            components={{ valueNum: <b />, pointsNum: <b />, weightNum: <b /> }}
          />
        ) : (
          t("policyEditor.signalProbe.pending")
        )}
      </p>
      {/* Only where the mirror actually caps this signal. A history deeper than the far end
          bounds nothing, so saying so there is a true sentence with no consequence, on a card
          that is already long. */}
      {capped && (
        <p className="help rule-help">
          {t("policyEditor.signalProbe.historyCap", {
            span: humanDays(Math.round(reachDays ?? 0)),
          })}
        </p>
      )}
    </>
  );
}

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
      <Trans
        i18nKey="policyEditor.seasonAdvisory.message"
        values={{
          bounded: bounded ? "yes" : "no",
          covered: count(covered),
          total: count(data.total_shows),
        }}
        components={{ coveredNum: <strong /> }}
      />
    </p>
  );
}

// ---------------------------------------------------------------------------
// The workspace.
// ---------------------------------------------------------------------------

/** What one `PolicyRepair` says, and where the page says it.
 *
 *  `where` is a property of the repair, not of the layout: the rescale is about the numbers
 *  you are ABOUT TO WRITE, so it belongs beside Save, while a body that could not be read at
 *  all belongs at the top where no scroll position can hide it.
 *
 *  Keyed on the server's ids and consulted with a fallback, so an id this file does not know
 *  still renders a sentence rather than nothing (rule 66). It is only the sentence that can
 *  be missing: `dirty` counts `repairs` and never reads this map, so an unknown repair still
 *  raises the savebar. That split is what makes the limbo in #516 unreachable.
 *
 *  The backend's twin is `_REPAIR_CHECKS` in `src/reaper/services/scan_runner.py`, which
 *  writes the same repair into the incomplete-scan notice. One repair, two sentences, in two
 *  trees (rule 144) -- `tests/test_policy_repairs.py` walks `PolicyRepair` against both and
 *  names whichever file is missing a member. */
type RepairNotice = {
  tone: "error" | "warn";
  where: "top" | "savebar";
};

export const REPAIR_NOTICES: Record<string, RepairNotice> = {
  fell_back: { tone: "error", where: "top" },
  rating_rules_restored: { tone: "warn", where: "top" },
  lists_migrated: { tone: "warn", where: "top" },
  rescaled: { tone: "warn", where: "savebar" },
};

/** The repair's sentence, read from the catalog at call time rather than baked into the
 *  frozen table above -- same shape as `jobMeta` in `JobsPanel.tsx`, and for the same
 *  reason: a language change is picked up the next render. Falls back to the one sentence
 *  true of every repair this build does not recognize, so a server newer than this bundle
 *  still tells the operator why the page is dirty and what clears it. */
function repairText(id: string): string {
  switch (id) {
    case "fell_back":
      return i18next.t("policyEditor.repairs.fellBack");
    case "rating_rules_restored":
      return i18next.t("policyEditor.repairs.ratingRulesRestored");
    case "lists_migrated":
      return i18next.t("policyEditor.repairs.listsMigrated");
    case "rescaled":
      return i18next.t("policyEditor.repairs.rescaled");
    default:
      return i18next.t("policyEditor.repairs.unknown");
  }
}

/** What an id this build does not know renders as -- the one thing true of every repair. */
const UNKNOWN_REPAIR: RepairNotice = { tone: "warn", where: "top" };

const SECTION_IDS = ["flags", "kept", "pace", "deletion"] as const;
export type PolicySectionId = (typeof SECTION_IDS)[number];
type SectionId = PolicySectionId;

/** The section's nav-rail and heading text, read at call time rather than baked into a
 *  frozen table -- same shape as `jobMeta` in `JobsPanel.tsx`. Both the rail button and the
 *  section's own `<h3>` name the same section, so they share this one declaration. */
function sectionLabel(id: SectionId): string {
  switch (id) {
    case "flags":
      return i18next.t("policyEditor.sections.flags");
    case "kept":
      return i18next.t("policyEditor.sections.kept");
    case "pace":
      return i18next.t("policyEditor.sections.pace");
    case "deletion":
      return i18next.t("policyEditor.sections.deletion");
  }
}

/** A mount condition one of the anchors below sits under. An anchor claims its fields only
 *  while its guard holds, so on the other branch they fall to the catch-all stack instead of
 *  off the page. Adding a value here does not compile in `PolicyEditor.warnings.test.tsx` until the
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
   *  member of the family, since a probe has to be a field the server could actually send.
   *
   *  The family is open, so it is the one shape whose fields cannot all be listed here, and
   *  `PolicyEditor.warnings.test.tsx`'s binding table is allowed to name more of it than `fields` does
   *  -- every member it names must still be one this anchor claims. */
  readonly prefix?: string;
  /** The mount condition this anchor's `WarnBlock` sits under, where it has one. */
  readonly guard?: WarningGuard;
};

const ANCHORS = [
  { id: "condemn_at", fields: ["condemn_at"] },
  { id: "gates", fields: ["gates.server_popularity.window_days"], prefix: "gates." },
  // Mixed, the way `keep_last` is: the bare field is the card's own complaint (the protection
  // is on with no sources), and the family under it is one bar each. Only the family binds to
  // a control -- the card-level one names two remedies and there is no single box to point it
  // at, which is where `PolicyEditor.warnings.test.tsx`'s `unbound` says so out loud (#189).
  {
    id: "keep_rating_rules",
    fields: ["keep_rating_rules", "keep_rating_rules.imdb.floor"],
    prefix: "keep_rating_rules.",
    guard: "ratingGate",
  },
  // `flag_keep_conflicts` rides this anchor rather than earning its own: this block is the
  // last thing in the season card, directly under that switch's row, so the warning about
  // shows older than the watch mirror (#224) already renders beside the control its help text
  // offers as the other way out. Its only mount condition is `tv`, the same one, so one guard
  // still names it exactly.
  {
    id: "keep_last",
    fields: ["keep_last_seasons", "keep_last_scope", "flag_keep_conflicts"],
    guard: "tv",
  },
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
 *  job: `PolicyEditor.warnings.test.tsx` walks THIS list, drives one warning per claimed field through
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

/** One dormancy edge in plain years: "1", "1.5", "3". The six canonical buckets
 *  (services/rewatch.py) land on round fractions of a year, and the monotonicity merge only
 *  ever joins adjacent ones, so every lo/hi the ladder and the echo read is still one of
 *  those six edges. */
function rewatchYears(days: number): string {
  const years = Math.round((days / 365) * 2) / 2;
  return Number.isInteger(years) ? String(years) : years.toFixed(1);
}

/** The edge above, with its unit: "1 year", "1.5 years", "5 years". */
function rewatchYearsPhrase(days: number): string {
  const years = Math.round((days / 365) * 2) / 2;
  return i18next.t("policyEditor.rewatch.yearsPhrase", { label: rewatchYears(days), n: years });
}

/** One ladder row's dormancy range in plain words: "sat under 1 year", "1 to 1.5 years",
 *  "over 5 years". */
function rewatchRangeLabel(lo: number, hi: number | null): string {
  if (hi === null)
    return i18next.t("policyEditor.rewatch.rangeOver", { phrase: rewatchYearsPhrase(lo) });
  return lo === 0
    ? i18next.t("policyEditor.rewatch.rangeUnder", { phrase: rewatchYearsPhrase(hi) })
    : i18next.t("policyEditor.rewatch.rangeTo", {
        lo: rewatchYears(lo),
        phrase: rewatchYearsPhrase(hi),
      });
}

/** Why the ladder or the echo cannot show a real number right now, or `null` when the fit
 *  is ready to read (rule 17: a fit that cannot be read says so beside the control, never a
 *  silent absence). Shared by both, so the two cannot describe the same failure two ways.
 *
 *  No reload advice on the error branch (#195): this sits inside an editor whose savebar
 *  may be holding unsaved policy edits, and a reload takes them with no ask -- the same
 *  reason the caps-and-grace read below states its own failure bare. */
function rewatchFitStatus(
  fit: RewatchOddsFit | undefined,
  isPending: boolean,
  isError: boolean,
  mediaType: "movie" | "tv",
): string | null {
  if (isPending) return i18next.t("policyEditor.rewatch.pending");
  if (isError) return i18next.t("policyEditor.rewatch.error");
  if (!fit || fit.blocks.length === 0) {
    return fit && fit.total_items > 0
      ? i18next.t("policyEditor.rewatch.notEnoughScanned", { mediaType })
      : i18next.t("policyEditor.rewatch.runScanFirst");
  }
  return null;
}

/** The library's own fitted rewatch ladder, read before the operator decides where to set
 *  the percentage below it. Not itself a sub-control (rule 41): it renders whenever the fit
 *  has something to show, switch on or off, since it is what the operator reads BEFORE
 *  turning the hold on. */
function RewatchLadder({
  fit,
  isPending,
  isError,
  mediaType,
}: {
  fit: RewatchOddsFit | undefined;
  isPending: boolean;
  isError: boolean;
  mediaType: "movie" | "tv";
}) {
  const status = rewatchFitStatus(fit, isPending, isError, mediaType);
  if (status || !fit) return <p className="help rule-help">{status}</p>;
  return (
    <dl className="rewatch-ladder">
      {[...fit.blocks]
        .sort((a, b) => a.lo_days - b.lo_days)
        .map((block) => (
          <div key={`${block.lo_days}-${String(block.hi_days)}`}>
            <dt>{rewatchRangeLabel(block.lo_days, block.hi_days)}</dt>
            <dd>{Math.round((100 * block.k) / block.n)}%</dd>
          </div>
        ))}
    </dl>
  );
}

/** The consequence echo beside the percentage box: what the current threshold protects,
 *  recomputed from the fetched fit rather than read off the stored gate, so it tracks the
 *  box as it moves, before Save. `fit.blocks` sorts ascending by dormancy, and the
 *  monotonicity merge that built them makes the rate non-increasing with it, so the
 *  cleared set is the leading run whose upper bound still clears the bar. */
function rewatchEchoSentence(
  fit: RewatchOddsFit,
  thresholdPct: number,
  mediaType: "movie" | "tv",
): string {
  // Every clearing block counts, not just the leading run: the gate fires per block, and
  // the merge only makes point RATES monotone -- an upper bound can invert across blocks
  // of very different cohort sizes, and an echo that stopped at the first miss would then
  // undercount what the gate actually protects (rule 62's class: the number beside a
  // control derives from the same set the server acts on).
  const sorted = [...fit.blocks].sort((a, b) => a.lo_days - b.lo_days);
  const cleared = sorted.filter((block) => block.upper_bound_pct >= thresholdPct);
  const protectedItems = cleared.reduce((sum, block) => sum + block.items, 0);
  if (cleared.length === 0) {
    return i18next.t("policyEditor.rewatch.echoEmpty", {
      pct: thresholdPct,
      total: count(fit.total_items),
    });
  }
  const last = cleared[cleared.length - 1]!;
  // The "under about N years" clause is only true of a contiguous leading run; on the rare
  // non-contiguous clear, the count stands alone rather than claiming a range it isn't.
  const contiguous = cleared.length === sorted.indexOf(last) + 1;
  const range =
    last.hi_days === null
      ? i18next.t("policyEditor.rewatch.anyAge")
      : contiguous
        ? i18next.t("policyEditor.rewatch.underAbout", { phrase: rewatchYearsPhrase(last.hi_days) })
        : null;
  return range === null
    ? i18next.t("policyEditor.rewatch.echoNoRange", {
        pct: thresholdPct,
        mediaType,
        protectedCount: count(protectedItems),
        total: count(fit.total_items),
      })
    : i18next.t("policyEditor.rewatch.echoWithRange", {
        pct: thresholdPct,
        mediaType,
        range,
        protectedCount: count(protectedItems),
        total: count(fit.total_items),
      });
}

export function PolicyEditor({
  focus,
  mediaType,
  onMediaTypeChange,
  section,
  onSectionChange,
}: {
  /** A cross-page jump target ("Turn it on in Policy → Deletion" lands on the Deletion
   *  section). The nonce makes each jump fire once, however often the caller re-renders, and it
   *  is what separates an instruction to SCROLL from the report of where the page is scrolled to.
   *  A jump can arrive with this page already mounted: the safety banner is on every screen. */
  focus?: { section: PolicySectionId; nonce: number } | null;
  /** Which policy is being edited. Movies and TV are tuned separately -- keep-last-N seasons and
   *  season rank only make sense for TV -- so this decides both the controls the page draws and
   *  the numbers in them. Owned by `App` for the same reason `section` is: it is half of where
   *  the operator is, and a URL naming only the other half reopens the page with the wrong
   *  numbers on it. */
  mediaType: "movie" | "tv";
  /** Reported when the Movies/TV switch goes through, which is after the unsaved-edits confirm
   *  where there is one. */
  onMediaTypeChange: (next: "movie" | "tv") => void;
  /** Which section is being read. Owned by `App`, not here: the rail's `aria-current` and the
   *  address bar (`/policy/tv/deletion`) are one fact, and the URL is written where every other
   *  nav is written. */
  section: PolicySectionId;
  /** Reported on a rail click, on a jump, and as the page is scrolled past a heading. */
  onSectionChange: (next: PolicySectionId) => void;
}) {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  // Save and Discard both unmount the bar holding the pressed button (#173). Declared here,
  // above the `if (!draft)` return further down, because a hook below an early return is a
  // different hook order on the renders that take it (rule 146).
  const bar = useSavebarFocus();
  // Where focus lands when a leftover protection's switch removes its own row. Declared with
  // the other focus hooks for rule 146's reason, and the fallback is Save because it is the
  // only place it CAN be: `useRemovalFocus` looks for the marked control that took the removed
  // one's index, and only a retired row wears the marker, so the marked set is always empty
  // afterwards and the fallback is the whole answer here rather than an edge case. Save is the
  // right target anyway -- the removal is a draft edit and pressing it is what makes the edit
  // real, which is the same "stable neighbour and the only thing left to do" the hook's own
  // add-a-row fallbacks are. It is mounted whenever this can fire: the body that carries a
  // leftover always arrives with a repair, and `dirty` counts repairs.
  const saveRef = useRef<HTMLButtonElement>(null);
  const protections = useRemovalFocus(saveRef);
  const { data: saved, isError: policyFailed } = useQuery({
    queryKey: ["policy", mediaType],
    queryFn: () => api.policy(mediaType),
  });

  // The rewatch card's own fitted ladder and consequence echo (#554 stage 2), on both
  // lanes: the hold half below now renders for TV too, since a TV body carries its own
  // rewatch_odds gate row the same way a movie body does. Keyed and refetched on
  // `mediaType` so a lane switch never shows the other lane's fit. Independent of `draft`
  // -- it reads the last scan's frozen numbers, not anything a save would change.
  const {
    data: rewatchFit,
    isPending: rewatchFitPending,
    isError: rewatchFitError,
  } = useQuery({
    queryKey: ["rewatch-odds", mediaType],
    queryFn: () => api.rewatchOddsFit(mediaType),
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
      announce(t("policyEditor.announce.paceSaved"));
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
    }, SETTLE_MS);
    return () => clearTimeout(id);
  }, [draft, draftedUnmeasured]);

  const {
    data: simulation,
    error: simError,
    isPlaceholderData,
  } = useQuery({
    queryKey: ["simulate", debounced],
    queryFn: () => api.simulate(debounced!),
    enabled: debounced !== null,
    placeholderData: keepPreviousData, // keep the last result visible while refetching
  });

  // Is the body those numbers describe actually an edit? The panel simulates on mount, before
  // anything has been touched, so "no title changes" on its own cannot tell the operator's
  // inert edit from their untouched policy -- and the second reading would be the panel calling
  // a rule useless on the evidence of nobody having tried it yet.
  //
  // Against `debounced` rather than `draft`, so the sentence cannot lead its own figures by the
  // debounce and flip under a keystroke nothing has simulated yet. Raw JSON.stringify compares
  // canonical forms for the same reason `dirty` below may (rule 39): the draft is re-seeded from
  // the server's own response after each save. `dirty`'s forced `repairs` term is deliberately
  // absent here: a repair is a reason to show a savebar, never evidence a rule was changed.
  const simulatedIsEdited = useMemo(
    () =>
      debounced !== null &&
      saved !== undefined &&
      JSON.stringify(debounced) !== JSON.stringify(saved.body),
    [debounced, saved],
  );

  // `debounced` is the body the REQUEST was made from, which is not the body the numbers on
  // screen came from: `keepPreviousData` keeps the previous answer rendered, `status` reads
  // "success" throughout, and this memo recomputes in the same commit the query key changes.
  // So for one round trip -- 305 to 366 ms measured, and `docs/LEARNINGS.md` projects seconds
  // on a large library -- `edited` described the new draft while `simulation` still answered
  // the old one. The untouched policy always returns `changed_titles === 0`, so the FIRST edit
  // of a session met "Your changes leave every title as it is." before anything had scored it.
  // Rule 85: the claim waits for the answer it is about.
  const simulationIsSettled = !isPlaceholderData;

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
  // a claim about itself: the walk in `PolicyEditor.warnings.test.tsx` drives every guard both ways
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
  // Read twice -- by the block under the protections list, and by every row in it, since each
  // row's boxes describe themselves from the same list the block renders. One call, so the two
  // ends cannot be handed different positions to number ids from (#189).
  const gateWarnings = warningsAt("gates");

  // A background scan, so the "Scan now" button in the stale notice actually does something.
  const scanState = useScanStatus();
  const scanning = scanState?.running ?? false;
  // No running->stopped effect here. This panel used to carry its own copy, invalidating
  // `simulate`, `snapshot` and `validate` off its own ref -- the first two already refreshed by
  // the shell's `useScanSettled`, and the third refreshed ONLY here, which is what let
  // `SCAN_SETTLED_KEYS` claim to be the single place while missing a key that reads from the
  // scan (#205, rule 79). All three are in that list now, the shell cannot be unmounted by a
  // scan, and both read the same `["scanStatus"]` cache, so the transition is the same one.

  // A mutation, not a fire-and-forget async onClick: a "Scan now" that fails must say so
  // in the stale notice, or the button appears to do nothing at all.
  const startScan = useMutation({
    mutationFn: () => api.startScan(),
    onSuccess: (started) => {
      queryClient.setQueryData(["scanStatus"], started);
      // The press swaps the notice for a progress bar, which is a visual change and nothing else:
      // the failure path speaks (`Notice` owns `role="alert"`) and the success path did not, so a
      // scan that started and one that did nothing sounded alike (#173). Both sentences come from
      // the panel that also shows them, since a reader lands on that heading in the next breath
      // and one fact written in two files drifts (rule 144).
      //
      // It branches because the two cases are not the same news. When a scan was already running,
      // the bar the operator is now watching belongs to a scan that started BEFORE they saved, so
      // "rescanning to apply your changes" would be wrong about the one thing it claims (#177).
      announce(started.followup_queued ? RESCAN_QUEUED_LEAD : `${RESCAN_HEADING}.`);
    },
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
      announce(
        savedType === "tv"
          ? t("policyEditor.announce.tvPolicySaved")
          : t("policyEditor.announce.moviePolicySaved"),
      );
      queryClient.setQueryData(["policy", savedType], policy);
      // Re-seed the draft from the server's response so the dirty check compares
      // canonical forms. The server can order fields differently from the draft the
      // save was built from, and a raw JSON.stringify comparison would then read
      // "unsaved changes" forever.
      setDraft((cur) => (cur && cur.media_type === policy.body.media_type ? policy.body : cur));
      void queryClient.invalidateQueries({ queryKey: ["policy", savedType] });
      // Settings -> Lists derives each row's "how Policy uses it" line from the active policy
      // server-side, so a save that softened or removed a list's keep rule changes that answer
      // with no client-side signal. Global `staleTime` is 30s and focus refetching is off, so
      // without this the operator edits a rule here, walks back, and reads "Keeps every title
      // on it" for a list nothing now protects with (rule 79).
      void queryClient.invalidateQueries({ queryKey: ["lists-configured"] });
      // Apply the saved policy to the review queue by re-scanning in the background. The
      // queue and the simulator read the last snapshot's stored verdicts, which were
      // produced by the OLD policy; a rescan re-scores the library under the new one, and the
      // shell's `useScanSettled` refreshes the simulator and queue when it lands. That used to
      // read "the running->stopped effect above", which this panel carried until its copy was
      // folded into `SCAN_SETTLED_KEYS` -- so the file told a reader to look 36 lines up for an
      // effect the line up there says outright is gone.
      // Idempotent server-side: if a scan is already running this just follows it.
      startScan.mutate();
    },
  });

  // EVERY load-time repair forces dirty, because in each case what is on screen is not what
  // is stored and the savebar has to offer the Save that replaces it.
  //
  // One term for every repair, and it counts rather than naming: a repair kind the server
  // adds raises the savebar here on the day it ships, before anyone writes copy for it. The
  // shape this replaced named three of the four, so a body whose list protections had been
  // converted opened CLEAN -- the page held no Save, while every scan degraded telling the
  // operator to go and press it (#516). Discard cannot clear it, which is right: the stored
  // body is the one that does not load.
  const repairs = useMemo(() => saved?.repairs ?? [], [saved]);
  const dirty = useMemo(
    () =>
      draft !== null &&
      saved !== undefined &&
      (repairs.length > 0 || JSON.stringify(draft) !== JSON.stringify(saved.body)),
    [draft, saved, repairs],
  );

  // The repairs split by where they read, resolved through `REPAIR_NOTICES` with the
  // unknown-id fallback so a server newer than this bundle still says something. Server
  // order is kept: it is the order the repairs were applied.
  const repairNotices = useMemo(() => {
    const resolved = repairs.map((id) => ({
      id,
      notice: { ...(REPAIR_NOTICES[id] ?? UNKNOWN_REPAIR), text: repairText(id) },
    }));
    return {
      top: resolved.filter((r) => r.notice.where === "top"),
      savebar: resolved.filter((r) => r.notice.where === "savebar"),
    };
  }, [repairs]);

  // The preset that was just applied, so the band can say "staged, not saved" until both
  // saves are clean again (or the drafts are discarded).
  const [staged, setStaged] = useState<PresetId | null>(null);
  useEffect(() => {
    if (staged !== null && !dirty && !paceDirty) setStaged(null);
  }, [staged, dirty, paceDirty]);

  // The Movies/TV switch the owner asked for while the draft still holds unsaved edits.
  // Switching re-seeds the draft from the other saved policy, which would silently throw
  // those edits away, so it waits for the same two-step confirm the rest of the app uses
  // (never a native confirm()). `useSwitchConfirm` is the shared caller half.
  //
  // The confirm stays HERE while the value it commits lives in `App`: `dirty` is this
  // component's, and the draft it guards is too. `App` is handed the switch only once the
  // operator has said the edits can go, so the address bar cannot name a policy that is not on
  // screen (rule 146).
  const confirmSwitch = useSwitchConfirm(mediaType, dirty, onMediaTypeChange);

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
  // A rail click and a cross-page jump both STATE a section. The spy below measures one, and the
  // two disagree wherever a click lands on the last screenful: scrolling to "Pace and limits"
  // reaches the end of the document, and that is the same position as scrolling down to read
  // Deletion. The rects are identical, so the spy cannot tell them apart and it overwrote the
  // click, marking a section nobody asked for (#795). What was stated holds until the page moves
  // again, which is the one thing that does separate the two.
  // The position a click or a jump left the page at. Which section it named is already in
  // `section`, so only the position is kept.
  const statedAt = useRef<number | null>(null);
  const goToSection = useCallback(
    (id: SectionId) => {
      // Instant, not smooth: smooth scrolling silently no-ops in some environments, and a jump
      // that always lands beats an animation that sometimes doesn't happen at all. It also
      // finishes before the next line runs, so the position recorded is where the page ended up.
      sectionRefs[id].current?.scrollIntoView({ block: "start" });
      statedAt.current = window.scrollY;
      onSectionChange(id);
    },
    [sectionRefs, onSectionChange],
  );

  // A cross-page jump lands on a specific section, and so does a cold load on a URL naming one
  // (`/policy/tv/deletion`). `App` seeds the same aim from both, so they scroll through one path.
  // The editor may still be loading when the aim arrives (the headings do not exist until the
  // draft renders), so the draft is a dependency: once it loads, this refires and consumes the
  // nonce exactly once.
  const handledFocus = useRef(0);
  useEffect(() => {
    if (!focus || draft === null || focus.nonce === handledFocus.current) return;
    if (!sectionRefs[focus.section]?.current) return;
    handledFocus.current = focus.nonce;
    goToSection(focus.section);
  }, [focus, sectionRefs, draft, goToSection]);

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
  // is mounted; an unchanged section is a React state bail-out, not a re-render. That still
  // holds now the section lives in `App`: a scroll frame that reports the section already on
  // screen hands the same value to the same setter, and React drops it without rendering.
  //
  // `section` is deliberately not a dependency below. A dependency re-registers the listeners,
  // and registering runs `pick` again, which on a page too short to scroll answers "the first
  // section" and would take a rail click straight back off the section it just landed on.
  const ready = draft !== null;
  useEffect(() => {
    if (!ready) return;
    const heads = SECTION_IDS.map((id) => [id, sectionRefs[id].current] as const).filter(
      (pair): pair is [SectionId, HTMLHeadingElement] => pair[1] !== null,
    );
    const first = heads[0];
    if (first === undefined) return;

    const pick = () => {
      // A click or a jump named this section and the page has not moved since, so nothing
      // measured here is more current than what was asked for. One pixel of slack, because a
      // scroll position is fractional on a zoomed page.
      const said = statedAt.current;
      if (said !== null) {
        if (Math.abs(window.scrollY - said) <= 1) return;
        statedAt.current = null;
      }
      // A page that does not scroll is read whole, so the first section is the one you are
      // on; there is no "further down" to have reached.
      const scrollable = document.documentElement.scrollHeight > window.innerHeight + 1;
      if (!scrollable) {
        onSectionChange(first[0]);
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
      onSectionChange(current);
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
  }, [ready, sectionRefs, onSectionChange]);

  // The draft only ever seeds from a successful read, so a failed one would otherwise
  // leave the whole workspace saying "Loading…" for good. Say what happened instead.
  if (!draft) {
    if (policyFailed) {
      return <Notice tone="error">{t("policyEditor.load.failedWithReload")}</Notice>;
    }
    return <p className="muted">{t("policyEditor.loading")}</p>;
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
  // meter renders directly below this line and the 422 notice renders with the protections card
  // (see `invalidMessage` below), so a second copy of either would be the third sentence in the
  // bar saying one thing. Points are named first when both are true, because that meter is the
  // one right beside this.
  //
  // The 422 clause quotes the notice's own heading rather than pointing anywhere. It used to send
  // the operator to "the top of this page" for a notice that renders near the bottom of the second
  // section, and a direction that survives one layout change is worth less than the words already
  // on screen (#178).
  const policyHeldBecause =
    pointsLeft !== 0
      ? t("policyEditor.savebar.becausePoints")
      : t("policyEditor.savebar.becauseInvalid");

  const kind =
    mediaType === "tv" ? t("policyEditor.mediaKind.tv") : t("policyEditor.mediaKind.movie");
  const otherKind =
    mediaType === "tv" ? t("policyEditor.mediaKind.movie") : t("policyEditor.mediaKind.tv");

  // The rewatch card's lane-dependent copy, both halves. `bar` and `barLabel` are one
  // sentence in two places, the span and the screen reader's label, so they are written
  // together.
  const rewatchCopy = {
    name: t("policyEditor.rewatchCopy.name", { mediaType }),
    help: t("policyEditor.rewatchCopy.help", { mediaType }),
    bar: t("policyEditor.rewatchCopy.bar", { mediaType }),
    barLabel: t("policyEditor.rewatchCopy.barLabel", { mediaType }),
    holdName: t("policyEditor.rewatchCopy.holdName", { mediaType }),
    holdHelp: t("policyEditor.rewatchCopy.holdHelp", { mediaType }),
  };
  const preset = activePreset(draft);
  const presetHelp =
    PRESETS.find((p) => p.id === preset)?.help ?? t("policyEditor.presetHelp.custom");

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
    popularity ? t("policyEditor.intent.watchedByPeople", { n: popularity.threshold || 1 }) : null,
    // "Untouched", never "played": this gate's clock runs from the last play only when
    // there IS one, and otherwise from the day the file arrived (engine/dormancy.py's
    // reference_instant). It therefore keeps titles nobody has ever played, so a clause
    // saying "played in the last N" was false about exactly the items it protects most --
    // the same claim the review queue's chip used to make, on the sentence this comment
    // block below calls the one an operator scans before arming (rules 21/72).
    dormancy
      ? t("policyEditor.intent.untouchedLessThan", { span: humanDays(dormancy.threshold) })
      : null,
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
      ? t("policyEditor.intent.newestSeasons", { n: draft.keep_last_seasons })
      : null,
    draft.keep_first_season ? t("policyEditor.intent.firstSeason") : null,
    draft.keep_in_progress ? t("policyEditor.intent.midBinge") : null,
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
      ? t("policyEditor.intent.withinCaps")
      : pace.caps_enabled
        ? t("policyEditor.intent.removesAtMost", {
            items: count(pace.max_items_per_run),
            size: bytes(pace.max_bytes_per_run),
          })
        : t("policyEditor.intent.noLimit");
  const paceTail = paceClause
    ? t("policyEditor.intent.paceTailWithClause", { clause: paceClause })
    : ".";

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
                {t("policyEditor.header.badge", { mediaType })}
              </span>
              <h2 ref={bar.ref as RefObject<HTMLHeadingElement>} tabIndex={-1}>
                {t("policyEditor.header.title", { mediaType })}
              </h2>
            </div>
            <p className="blurb pc-sub">{t("policyEditor.header.blurb", { mediaType })}</p>
          </div>
          <div className="policy-head-actions">
            {/* The request is refused into the two-step confirm when the draft has edits. */}
            <Segmented
              fill
              value={mediaType}
              onChange={confirmSwitch.request}
              label={t("policyEditor.header.whichPolicyLabel")}
              options={[
                ["movie", t("policyEditor.header.moviesOption")],
                ["tv", t("policyEditor.header.tvOption")],
              ]}
            />
            <DocLink doc="understanding-policy" className="doc-help">
              <HelpIcon />
              {t("policyEditor.header.helpLink")}
            </DocLink>
          </div>
        </div>
        {/* A repair notice renders on the load it explains, so it hangs off the response
            list alone and no dirty gate, disclosure or savebar can hide it.

            `standing` on all of them, for that same reason: a flag carried by the fetch is the
            state of the page from its first paint, and the reader meets it above the values it
            is about.

            All of them, in the order the server applied them, so a body that needed two repairs
            says both. Keyed on the repair id, so nothing here has to be remembered when a repair
            is added. */}
        {repairNotices.top.map(({ id, notice }) => (
          <Notice key={id} tone={notice.tone} as="div" standing>
            {notice.text}
          </Notice>
        ))}
        {confirmSwitch.pending !== null && (
          <SwitchConfirm
            nonce={confirmSwitch.nonce}
            message={t("policyEditor.switchConfirm.message", {
              kind,
              pending:
                confirmSwitch.pending === "tv"
                  ? t("policyEditor.header.tvOption")
                  : t("policyEditor.header.moviesOption"),
            })}
            onDiscard={confirmSwitch.discard}
            onKeep={confirmSwitch.keep}
          />
        )}

        <nav
          className="settings-nav policy-rail"
          aria-label={t("policyEditor.sections.navAriaLabel")}
        >
          {SECTION_IDS.map((id) => (
            <button
              key={id}
              className={section === id ? "settings-tab active" : "settings-tab"}
              // Reserve the bold (active) width so switching sections never shifts the rail.
              data-label={sectionLabel(id)}
              // The section being read is stated, not just colored, the same as the
              // masthead and the settings rail. True on a scroll as well as a click: the
              // observer above keeps activeSection on whatever heading has reached the
              // read line.
              aria-current={section === id ? "page" : undefined}
              onClick={() => goToSection(id)}
            >
              {sectionLabel(id)}
            </button>
          ))}
        </nav>

        {/* The intent band: the whole policy in one sentence, and three starting points. */}
        <div className="intent-band">
          <p className="intent-summary">
            {mediaType === "tv" ? (
              <>
                {t("policyEditor.intent.tvLead")}
                <strong>{t("policyEditor.intent.seasonWord")}</strong>
                {t("policyEditor.intent.scoresOnce")}
                <strong>{draft.condemn_at} / 100</strong>
                {tvKeepClauses.length > 0 && (
                  <>
                    {t("policyEditor.intent.alwaysKeeps")}
                    <strong>{andList(tvKeepClauses)}</strong>
                  </>
                )}
                {paceTail}
              </>
            ) : (
              <>
                {t("policyEditor.intent.movieLead")}
                <strong>{draft.condemn_at} / 100</strong>
                {keepClauses.length > 0 && (
                  <>
                    {t("policyEditor.intent.keepsAnything")}
                    <strong>{keepClauses.join(t("policyEditor.intent.orJoiner"))}</strong>
                  </>
                )}
                {paceTail}
              </>
            )}
          </p>
          <div className="intent-row">
            <span className="muted intent-label">{t("policyEditor.starting.label")}</span>
            {/* Hand-tuned drafts match no preset, so "custom" matches no option and no
                pill reads as active -- the same honesty the badge has always had. */}
            <Segmented
              value={preset ?? "custom"}
              onChange={(id) => {
                const p = PRESETS.find((x) => x.id === id);
                if (p) applyPreset(p);
              }}
              label={t("policyEditor.starting.label")}
              options={PRESETS.map((p) => [p.id, p.label] as const)}
            />
          </div>
          <p className="help">
            {presetHelp} {t("policyEditor.starting.presetHelpTail")}
          </p>
          {staged !== null && (
            <p className="help">
              <Trans
                i18nKey="policyEditor.starting.stagedNotice"
                components={{ strong: <strong /> }}
              />
            </p>
          )}
        </div>

        {/* ------------------------------------------------------------------ */}
        <h3 ref={sectionRefs.flags} className="policy-section">
          {t("policyEditor.sections.flags")}
          <DocLink doc="understanding-policy" anchor="signals" className="doc-learn">
            {t("policyEditor.learnMore")}
          </DocLink>
        </h3>
        <p className="blurb">{t("policyEditor.flags.blurb")}</p>

        <label className="field">
          <span className="field-label">
            {t("policyEditor.flags.condemnAtLabel")}
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
            aria-label={t("policyEditor.flags.condemnAtLabel")}
            // The warning about this threshold is rendered directly below and was reachable
            // only by leaving the slider to go looking for it (#174).
            aria-describedby={warningsDescribing("condemn_at", warningsAt("condemn_at"))}
            value={draft.condemn_at}
            onChange={(e) => update({ condemn_at: Number(e.target.value) })}
          />
          <span className="help">{t("policyEditor.flags.condemnAtHelp")}</span>
        </label>
        <WarnBlock anchor="condemn_at" warnings={warningsAt("condemn_at")} />

        <label className="field">
          <span className="field-label">
            {t("policyEditor.flags.coverageFloorLabel")}
            <strong>{Math.round(draft.coverage_floor_bp / 100)}%</strong>
          </span>
          <input
            type="range"
            min={0}
            max={10000}
            step={500}
            // Same reason as the threshold slider above (rule 72): the wrapping <label> takes in
            // the help text, so the name was the whole paragraph.
            aria-label={t("policyEditor.flags.coverageFloorLabel")}
            value={draft.coverage_floor_bp}
            onChange={(e) => update({ coverage_floor_bp: Number(e.target.value) })}
          />
          <span className="help">{t("policyEditor.flags.coverageFloorHelp")}</span>
        </label>

        <ul className="rule-list">
          {draft.signals.map((signal, i) => (
            <SignalRow
              key={signal.signal}
              signal={signal}
              // Off the SAVED policy, not the draft: it is a fact about the operator's watch
              // history rather than a policy value, so it does not move as they edit.
              reachDays={saved?.history_reach_days ?? null}
              shipped={saved?.default_signals?.find((d) => d.signal === signal.signal)}
              onChange={(s) => {
                const signals = [...draft.signals];
                signals[i] = s;
                update({ signals });
              }}
            />
          ))}
        </ul>
        {/* One key for the section, at its foot. Three strips down a column want one legend,
            not three, which is how the explanation panel does it too. Hidden from a reader
            for the same reason the strips are: it captions a picture, and read on its own it
            is two color names with nothing to attach them to. Every bound it describes is
            already in a labeled box above, spoken. */}
        <p className="ramp-key" aria-hidden="true">
          <span>
            <i className="earns" aria-hidden="true" /> {t("policyEditor.rampKey.earns")}
          </span>
          <span>
            <i className="alone" aria-hidden="true" /> {t("policyEditor.rampKey.alone")}
          </span>
        </p>
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
          {t("policyEditor.sections.kept")}
          <DocLink doc="understanding-policy" anchor="protections" className="doc-learn">
            {t("policyEditor.learnMore")}
          </DocLink>
        </h3>
        <p className="blurb">
          <Trans i18nKey="policyEditor.protections.blurb" components={{ em: <em /> }} />
        </p>

        <ul className="rule-list" ref={protections.ref as RefObject<HTMLUListElement>}>
          {draft.gates.map((gate, i) => {
            const setGate = (g: GateSetting) => {
              const gates = [...draft.gates];
              gates[i] = g;
              update({ gates });
            };
            // Only a retired row reaches this, and it is the one edit that is a REMOVAL: the
            // id cannot be saved in either switch position, so taking it off means taking it
            // out. By index rather than by id, like `setGate` above.
            //
            // `removing(0)`, never `removing(i)`: the index is into the MARKED controls, and
            // only a retired row wears the marker, so its position among all the protections
            // is not its position among the removable ones. That mismatch is the hazard
            // `useRemovalFocus`'s other call sites avoid by marking every row.
            const dropGate = () => {
              protections.removing(0);
              update({ gates: draft.gates.filter((_, j) => j !== i) });
            };
            // A protection that carries its own settings renders as a card below the plain
            // rows (the rating card, and the rewatch-odds hold folded into stage 1's rewatch
            // card), so the visual weight says which protections have more to configure.
            // Both are skipped here.
            if (gate.gate === "rating_floor" || gate.gate === "rewatch_odds") return null;
            return (
              <GateRow
                key={gate.gate}
                gate={gate}
                onChange={setGate}
                onRemove={dropGate}
                warnings={gateWarnings}
              />
            );
          })}
        </ul>
        <WarnBlock anchor="gates" warnings={gateWarnings} />

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

        {mediaType === "tv" && (
          <div className="season-card">
            <h3>{t("policyEditor.season.cardTitle")}</h3>
            <ul className="rule-list">
              <li className="rule-row">
                <span className="rule-name">{t("policyEditor.season.keepNewestLabel")}</span>
                <div className="rule-control">
                  <span>{t("policyEditor.season.theNewest")}</span>
                  <FixedQuantity
                    value={draft.keep_last_seasons}
                    suffix={t("policyEditor.units.season", { n: draft.keep_last_seasons })}
                    min={0}
                    max={1000}
                    width="narrow"
                    ariaLabel={t("policyEditor.season.keepNewestAriaLabel")}
                    // This anchor's block serves two controls, so each takes only the warnings
                    // about its own field. Handed the whole list, this box spoke the complaint
                    // about the scope control below -- a sentence ending "or switch this to
                    // 'All shows'", where "this" resolves to a box offering no such option.
                    describedBy={warningsDescribing("keep_last", warningsAt("keep_last"), [
                      "keep_last_seasons",
                    ])}
                    onChange={(v) => update({ keep_last_seasons: Math.max(0, v) })}
                  />
                  <span>{t("policyEditor.season.ofEveryShow")}</span>
                </div>
                <p className="help rule-help">{t("policyEditor.season.keepNewestHelp")}</p>
                <SeasonAdvisory keepLast={draft.keep_last_seasons} scope={draft.keep_last_scope} />
              </li>

              <li className="rule-row">
                <span className="rule-name">{t("policyEditor.season.applyToLabel")}</span>
                <div className="rule-control">
                  <Segmented
                    value={draft.keep_last_scope}
                    onChange={(keep_last_scope) => update({ keep_last_scope })}
                    label={t("policyEditor.season.scopeLabel")}
                    // The other half of `keep_last`: the warning about "Requested only" with no
                    // Seerr connected is fixed from HERE, and this is the control it names.
                    describedBy={warningsDescribing("keep_last", warningsAt("keep_last"), [
                      "keep_last_scope",
                    ])}
                    options={[
                      ["all", t("policyEditor.season.scopeAllOption")],
                      ["requested", t("policyEditor.season.scopeRequestedOption")],
                    ]}
                  />
                </div>
                <p className="help rule-help">{t("policyEditor.season.scopeHelp")}</p>
              </li>

              <li className="rule-row">
                <label className="toggle rule-toggle">
                  <Switch
                    checked={draft.keep_first_season}
                    onChange={(keep_first_season) => update({ keep_first_season })}
                  />
                  <span className="rule-name">{t("policyEditor.season.firstSeasonLabel")}</span>
                </label>
                <p className="help rule-help">{t("policyEditor.season.firstSeasonHelp")}</p>
              </li>

              <li className="rule-row">
                <label className="toggle rule-toggle">
                  <Switch
                    checked={draft.keep_in_progress}
                    onChange={(keep_in_progress) => update({ keep_in_progress })}
                  />
                  <span className="rule-name">{t("policyEditor.season.midBingeLabel")}</span>
                </label>
                <p className="help rule-help">{t("policyEditor.season.midBingeHelp")}</p>
                {draft.keep_in_progress && (
                  <>
                    <div className="rule-control">
                      <span>{t("policyEditor.season.holdAfterLabel")}</span>
                      <FixedQuantity
                        value={draft.in_progress_hold_days}
                        suffix={t("policyEditor.units.days")}
                        min={0}
                        // Matches `PolicyIn`'s ceiling, so the box clamps instead of the
                        // save coming back 422 (rule 131). Far past any watch history: 0
                        // is what "hold forever" is spelled as.
                        max={36500}
                        width="narrow"
                        ariaLabel={t("policyEditor.season.holdDaysAriaLabel")}
                        // One anchor, one field, one box: this is the control the warning's
                        // remedy names, so it takes the whole list unfiltered (#174).
                        describedBy={warningsDescribing("in_progress", warningsAt("in_progress"))}
                        onChange={(v) => update({ in_progress_hold_days: Math.max(0, v) })}
                      />
                      <span>{t("policyEditor.season.withoutWatching")}</span>
                    </div>
                    <p className="help rule-help">{t("policyEditor.season.holdDaysHelp")}</p>
                    <div className="rule-control">
                      <span>{t("policyEditor.season.alsoKeep")}</span>
                      <FixedQuantity
                        value={draft.season_lookahead}
                        suffix={t("policyEditor.units.season", { n: draft.season_lookahead })}
                        min={0}
                        max={1000}
                        width="narrow"
                        ariaLabel={t("policyEditor.season.lookaheadAriaLabel")}
                        onChange={(v) => update({ season_lookahead: Math.max(0, v) })}
                      />
                      <span>{t("policyEditor.season.aheadOfWhereTheyAre")}</span>
                    </div>
                    <p className="help rule-help">{t("policyEditor.season.lookaheadHelp")}</p>
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
                  <span className="rule-name">
                    {t("policyEditor.season.neverRemoveSpecialsLabel")}
                  </span>
                </label>
                <p className="help rule-help">{t("policyEditor.season.neverRemoveSpecialsHelp")}</p>
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
                  <span className="rule-name">{t("policyEditor.season.incompleteLabel")}</span>
                </label>
                <p className="help rule-help">{t("policyEditor.season.incompleteHelp")}</p>
              </li>

              <li className="rule-row">
                <label className="toggle rule-toggle">
                  <Switch
                    checked={draft.flag_keep_conflicts}
                    onChange={(flag_keep_conflicts) => update({ flag_keep_conflicts })}
                    // The warning about shows older than the watch mirror (#224) is anchored
                    // here, and this is the control it is about. Scoped to its own field so
                    // this switch does not also speak the seasons box's complaint.
                    describedBy={warningsDescribing("keep_last", warningsAt("keep_last"), [
                      "flag_keep_conflicts",
                    ])}
                  />
                  <span className="rule-name">{t("policyEditor.season.askFirstLabel")}</span>
                </label>
                <p className="help rule-help">{t("policyEditor.season.askFirstHelp")}</p>
              </li>
            </ul>
            <WarnBlock anchor="keep_last" warnings={warningsAt("keep_last")} />
          </div>
        )}

        {/* Both halves live on both lanes: the keep scores a TV body off show-level
            re-watch counts and a movie body off title-level ones (`keep_configs()`,
            engine/policy.py), and `PolicyBody._rewatch_odds_row` appends a rewatch_odds
            gate row to either body, so the hold's `findIndex` below finds it on both. */}
        <div className="rules-card">
          <div className="card-head">
            <label className="toggle">
              <Switch
                checked={draft.rewatch_keep_enabled}
                onChange={(rewatch_keep_enabled) => update({ rewatch_keep_enabled })}
              />
              <span className="rule-name">{rewatchCopy.name}</span>
            </label>
            <DocLink doc="understanding-policy" anchor="rewatch-keep" className="doc-learn">
              {t("policyEditor.learnMore")}
            </DocLink>
          </div>
          <p className="help rule-help">{rewatchCopy.help}</p>
          {draft.rewatch_keep_enabled && (
            <>
              <div className="rule-control">
                <span>{rewatchCopy.bar}</span>
                <FixedQuantity
                  value={draft.rewatch_min_viewings}
                  suffix={t("policyEditor.units.times")}
                  min={1}
                  max={1000}
                  width="narrow"
                  ariaLabel={rewatchCopy.barLabel}
                  onChange={(v) => update({ rewatch_min_viewings: v })}
                />
              </div>
              <div className="rule-control">
                <span>{t("policyEditor.rewatchCard.mostRecentlyWithin")}</span>
                <QuantityInput
                  value={draft.rewatch_recent_days}
                  units={TIME_UNITS}
                  min={1}
                  ariaLabel={t("policyEditor.rewatchCard.mostRecentlyWithinAriaLabel")}
                  onChange={(v) => update({ rewatch_recent_days: v })}
                />
              </div>
              <div className="rule-control">
                <span>{t("policyEditor.rewatchCard.lowersScoreBy")}</span>
                <FixedQuantity
                  value={draft.rewatch_keep_discount}
                  suffix={t("policyEditor.units.points")}
                  min={1}
                  max={50}
                  width="narrow"
                  ariaLabel={t("policyEditor.rewatchCard.lowersScoreByAriaLabel")}
                  onChange={(v) => update({ rewatch_keep_discount: v })}
                />
              </div>
            </>
          )}

          {/* The hold, stage 2's opt-in hard companion (#554): the grouped card's second
              half, on both lanes now -- same grammar as movies, per "Approved with the
              mockups, 2026-08-13" in docs/history/REWATCH_PLAN.md. Wired to draft.gates the
              same way RatingFloorRow is above, by index. */}
          {(() => {
            const gi = draft.gates.findIndex((g) => g.gate === "rewatch_odds");
            const hold = gi >= 0 ? draft.gates[gi] : undefined;
            // Always present on a loaded body on either lane (PolicyBody._rewatch_odds_row
            // appends it on load), so undefined only guards a body this editor has not
            // re-seeded yet.
            if (!hold) return null;
            const setHold = (g: GateSetting) => {
              const gates = [...draft.gates];
              gates[gi] = g;
              update({ gates });
            };
            const status = rewatchFitStatus(
              rewatchFit,
              rewatchFitPending,
              rewatchFitError,
              mediaType,
            );
            return (
              <>
                <div className="policy-divider" />
                <label className="toggle rule-toggle">
                  <Switch
                    checked={hold.enabled}
                    onChange={(enabled) => setHold({ ...hold, enabled })}
                  />
                  <span className="rule-name">{rewatchCopy.holdName}</span>
                </label>
                <p className="help rule-help">{rewatchCopy.holdHelp}</p>
                <RewatchLadder
                  fit={rewatchFit}
                  isPending={rewatchFitPending}
                  isError={rewatchFitError}
                  mediaType={mediaType}
                />
                {hold.enabled && (
                  <>
                    <div className="rule-control">
                      <span>{t("policyEditor.rewatchHold.keptWhenAtLeast")}</span>
                      <FixedQuantity
                        value={hold.threshold}
                        suffix={t("policyEditor.units.percent")}
                        min={1}
                        max={99}
                        width="narrow"
                        ariaLabel={t("policyEditor.rewatchHold.keptWhenAtLeastAriaLabel")}
                        onChange={(v) => setHold({ ...hold, threshold: v })}
                      />
                    </div>
                    <p className="help rule-help">
                      {status ?? rewatchEchoSentence(rewatchFit!, hold.threshold, mediaType)}
                    </p>
                  </>
                )}
              </>
            );
          })()}
        </div>

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
            gate-off popularity window (`engine/policy_warnings.py inspect`), which has nowhere
            else to go: with that protection off its window control is not rendered. */}
        <WarnBlock anchor="keep_rules" warnings={warningsAt("keep_rules")} />

        {/* A validation failure is an ERROR (red): this policy cannot be saved as-is.

            `standing` on both of these, for the reason `WarnBlock` above already gives and by
            rule 72, since all three read the same query: `["validate", debounced]` is keyed on
            the draft, so it refires as the operator types and this text changes inside the
            region rather than mounting once. Neither answers a press -- Save is disabled while
            `invalidMessage` holds, so there is no refused press for the red one to be the reply
            to. The savebar below says which halves it will write, and the failures that answer
            an actual Save are its own, further down, and stay alerts. */}
        {invalidMessage && (
          <Notice tone="error" standing>
            <strong>{t("policyEditor.validation.cantSaveThisPrefix")}</strong> {invalidMessage}
          </Notice>
        )}
        {/* The check itself failing is AMBER, and it does not lock Save: the server
            checks the policy again on save either way. */}
        {validatorDown && (
          <Notice tone="warn" standing>
            {t("policyEditor.validation.validatorDown")}
          </Notice>
        )}
        {/* Warnings live beside their controls; only one no anchor claims lands here. */}
        <WarnBlock anchor="unanchored" warnings={unanchoredWarnings} />

        <p className="hash">
          {validation && (
            <Trans
              i18nKey="policyEditor.hash.line"
              values={{ kind, hash: validation.policy_hash.slice(0, 12) }}
              components={{ hashCode: <code /> }}
            />
          )}
        </p>

        <div className="policy-divider" />

        {/* ------------------------------------------------------------------ */}
        <h3 ref={sectionRefs.pace} className="policy-section">
          {t("policyEditor.sections.pace")}
          <DocLink doc="understanding-policy" anchor="pace" className="doc-learn">
            {t("policyEditor.learnMore")}
          </DocLink>
        </h3>
        <p className="blurb">{t("policyEditor.pace.blurb")}</p>

        {/* Recovery notice: hangs off the response flag alone, so no dirty gate or disclosure
            can hide it (mirrors the policy recovery notice above), `standing` included. */}
        {savedPace?.settings_recovered && (
          <Notice tone="error" standing>
            {t("policyEditor.pace.recoveryNotice")}
          </Notice>
        )}

        {pace === null ? (
          paceFailed ? (
            // No reload advice (#195): this sits inside an editor whose savebar may be holding
            // unsaved policy edits, and a reload takes them with no ask.
            <Notice tone="error">{t("policyEditor.load.failedNoReload")}</Notice>
          ) : (
            <p className="muted">{t("policyEditor.loading")}</p>
          )
        ) : (
          <>
            <label className="toggle pace-approval">
              <Switch
                checked={pace.caps_enabled}
                onChange={(caps_enabled) => updatePace({ caps_enabled })}
              />
              <span>{t("policyEditor.pace.limitLabel")}</span>
            </label>
            <p className="help pace-approval-help">{t("policyEditor.pace.limitHelp")}</p>
            {/* `standing`: seeded from the stored profile, so it paints on load whenever caps are
                saved off, and after that it follows the Switch directly above it -- a control
                whose own state already says which way it went. */}
            {!pace.caps_enabled && (
              <Notice tone="warn" inline standing>
                {t("policyEditor.pace.noCapNotice")}
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
                  <span className="col-h">{t("policyEditor.pace.perRunHeader")}</span>
                  <span className="col-h">
                    <Trans
                      i18nKey="policyEditor.pace.per30DaysHeader"
                      components={{ em: <em /> }}
                    />
                  </span>

                  <span className="row-h">{t("policyEditor.pace.titlesRowHeader")}</span>
                  {/* The ceilings the server enforces, stated here so an out-of-range
                      number is pulled back in the box instead of coming home a 422. */}
                  <FixedQuantity
                    value={pace.max_items_per_run}
                    suffix={t("policyEditor.units.titles")}
                    min={1}
                    max={1000}
                    width="narrow"
                    ariaLabel={t("policyEditor.pace.mostTitlesPerRunAriaLabel")}
                    onChange={(v) => updatePace({ max_items_per_run: v })}
                  />
                  <FixedQuantity
                    value={pace.max_items_per_30d}
                    suffix={t("policyEditor.units.titles")}
                    min={1}
                    width="narrow"
                    ariaLabel={t("policyEditor.pace.mostTitlesPer30dAriaLabel")}
                    onChange={(v) => updatePace({ max_items_per_30d: v })}
                  />

                  <span className="row-h">{t("policyEditor.pace.diskFreedRowHeader")}</span>
                  <QuantityInput
                    value={pace.max_bytes_per_run}
                    units={SIZE_UNITS}
                    ariaLabel={t("policyEditor.pace.mostDiskPerRunAriaLabel")}
                    onChange={(v) => updatePace({ max_bytes_per_run: v })}
                  />
                  <QuantityInput
                    value={pace.max_bytes_per_30d}
                    units={SIZE_UNITS}
                    ariaLabel={t("policyEditor.pace.mostDiskPer30dAriaLabel")}
                    onChange={(v) => updatePace({ max_bytes_per_30d: v })}
                  />
                </div>
                <p className="help matrix-note">{t("policyEditor.pace.matrixNote")}</p>
              </>
            )}

            {/* Grace and unknown-size are not caps, so they step out of the matrix: one label,
                one control, one short line of help bound directly beneath it. */}
            <div className="pace-extra">
              <span className="ex-label">{t("policyEditor.pace.gracePeriodLabel")}</span>
              <span className="ex-ctl">
                <QuantityInput
                  value={pace.grace_days}
                  units={TIME_UNITS}
                  min={7}
                  ariaLabel={t("policyEditor.pace.gracePeriodLabel")}
                  onChange={(v) => updatePace({ grace_days: v })}
                />
                {/* A notice, not a hold: nothing on the deletion path reads this window
                    (services/grace.py), so help promising time "before removal" was false. */}
                <span className="help">{t("policyEditor.pace.gracePeriodHelp")}</span>
              </span>

              <span className="ex-label">{t("policyEditor.pace.unmeasuredLabel")}</span>
              <span className="ex-ctl">
                <FixedQuantity
                  value={pace.max_unmeasured_per_run}
                  suffix={t("policyEditor.units.perRun")}
                  min={0}
                  max={25}
                  width="narrow"
                  ariaLabel={t("policyEditor.pace.unmeasuredAriaLabel")}
                  // The one setting that lets deletions past the size caps, so the warning
                  // about it is worth hearing from the box rather than after it.
                  describedBy={warningsDescribing(
                    "max_unmeasured_per_run",
                    warningsAt("max_unmeasured_per_run"),
                  )}
                  onChange={(v) => updatePace({ max_unmeasured_per_run: v })}
                />
                {/* The clause about deleting is the only one an operator reads BEFORE
                    raising this, since the server's warning fires once it is already above
                    zero. Without it the help explained a keep and the control did the
                    opposite (#688). */}
                <span className="help">{t("policyEditor.pace.unmeasuredHelp")}</span>
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
          {t("policyEditor.sections.deletion")}
          <DocLink doc="arming" className="doc-learn">
            {t("policyEditor.learnMore")}
          </DocLink>
        </h3>
        <p className="blurb">{t("policyEditor.deletion.blurb")}</p>
        <DeletionToggle />

        {/* THE save bar: one place to save whatever is dirty -- the policy draft, the pace
            draft, or both (a preset stages both). Pinned to the viewport bottom while it
            has something to say, so Save is never a scroll away from the edit. */}
        {(dirty || paceDirty) && (
          <div className="savebar">
            <span className="savebar-what">
              <strong>
                {t("policyEditor.savebar.unsavedChangesPrefix")}
                {[
                  dirty ? t("policyEditor.savebar.kindPolicy", { kind }) : null,
                  paceDirty ? t("policyEditor.savebar.paceAndLimits") : null,
                ]
                  .filter(Boolean)
                  .join(", ")}
              </strong>
              <br />
              {/* What Save will ACTUALLY write, not what is merely dirty: a held-back policy
                  half must not be described as applying on the next scan (PR-7). */}
              {willSavePolicy && willSavePace
                ? `${APPLIES_ON_NEXT_SCAN} ${t("policyEditor.savebar.paceAppliesImmediately")}`
                : willSavePolicy
                  ? t("policyEditor.savebar.savesOnlyKind", {
                      kind,
                      otherKind,
                      appliesOnNextScan: APPLIES_ON_NEXT_SCAN,
                    })
                  : willSavePace
                    ? `${t("policyEditor.savebar.paceAppliesImmediately")} ${t("policyEditor.savebar.paceOnlyTail")}`
                    : null}
              {/* Only when the OTHER half will still be written: that is the new fact, and the
                  one an operator would otherwise have to infer. With the policy alone dirty
                  the button is simply disabled and the notice beside the cause already says
                  why, so a line here would be the bar's third sentence on one subject. */}
              {/* `standing`, like the other `budget-notice` on this page: it is a live readout of
                  what the button beside it would write, recomputed from `dirty` and `pointsLeft`
                  on every edit, so it changes under the operator rather than answering them. */}
              {policyBlocked && willSavePace && (
                <Notice tone="warn" className="budget-notice" as="span" standing>
                  {t("policyEditor.savebar.policyBlockedNotice", {
                    kind,
                    because: policyHeldBecause,
                  })}
                </Notice>
              )}
              {/* The repairs that are about what you are ABOUT TO WRITE, so they read beside
                  Save rather than at the top: today that is the rescale alone (`REPAIR_NOTICES`
                  carries the placement). From the response's repair list, not the warning list,
                  which is built by re-validating the draft and so can never carry anything about
                  how the policy loaded. No guard against the louder `fell_back` is needed: that
                  one is returned alone (`services/profiles.py`), because nothing of the stored
                  body survived for a second repair to be about.

                  `standing`, for the reason the `top` half already carries: a repair is carried
                  by the fetch, so it is the state of the page from its first paint and not a
                  reply to anything the operator pressed. Without it this half was an alert that
                  interrupted the moment the bar appeared, while its twin stayed silent. */}
              {repairNotices.savebar.map(({ id, notice }) => (
                <Notice key={id} tone={notice.tone} className="budget-notice" as="span" standing>
                  {notice.text}
                </Notice>
              ))}
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
                announce(t("policyEditor.announce.changesDiscarded"));
              }}
            >
              {t("policyEditor.savebar.discardButton")}
            </button>
            <button
              className="primary"
              ref={saveRef}
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
              {saving ? t("policyEditor.savebar.saving") : t("policyEditor.savebar.saveButton")}
            </button>
            {save.error && <Notice tone="error">{save.error.message}</Notice>}
            {savePace.error && <Notice tone="error">{savePace.error.message}</Notice>}
          </div>
        )}
      </div>

      <div className="editor-sim">
        <h2>{t("policyEditor.sim.heading")}</h2>
        <p className="blurb">{t("policyEditor.sim.blurb")}</p>
        {invalidMessage ? (
          <p className="muted">{t("policyEditor.sim.fixPolicyFirst")}</p>
        ) : simError ? (
          /* Checked BEFORE simulation: keepPreviousData can leave the previous draft's
             numbers in `simulation` when a refetch fails, and a stale count shown as
             current is exactly what this column must never do. */
          <div className="sim sim-info">
            <h3>{t("policyEditor.sim.errorHeading")}</h3>
            <p>{t("policyEditor.sim.errorBody")}</p>
            <p className="error">{simError.message}</p>
          </div>
        ) : simulation ? (
          simulation.exact ? (
            <Outcome
              simulation={simulation}
              threshold={draft.condemn_at}
              pace={pace}
              edited={simulatedIsEdited && simulationIsSettled}
            />
          ) : (
            <StaleNotice
              scanning={scanning}
              followupQueued={scanState?.followup_queued ?? false}
              starting={startScan.isPending}
              startError={startScan.error ? startScan.error.message : null}
              onScan={() => startScan.mutate()}
              percent={scanState?.percent ?? 0}
              detail={scanState?.detail ?? ""}
              staleKind={simulation.stale_kind}
              staleReason={simulation.stale_reason}
            />
          )
        ) : (
          <p className="muted">{t("policyEditor.sim.working")}</p>
        )}
      </div>
    </section>
  );
}

// Re-exported so the preset helpers keep their old import path while callers and tests move
// over to ./policyPresets, which now owns them (R-2).
export { andList, weightsMatchMix } from "./policyPresets";
