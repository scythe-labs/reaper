// SPDX-License-Identifier: AGPL-3.0-or-later
//
// The two editors for the operator's OWN rules: reasons to remove (which add score pressure)
// and reasons to keep (which either protect outright or lean toward keeping).
//
// Both are one form -- a field, a comparison, a value, a weight -- over a vocabulary the server
// publishes, so most of this file is the shared plumbing that turns a stored rule back into a
// sentence and a typed value back into the field's own units. Lifted whole out of
// PolicyEditor.tsx, which held these alongside the presets, five more editors and a 1,000-line
// component (R-2).
//
// The one safety rule in here: a rule can add pressure or take it away, but missing data always
// leans toward keeping, and a ramp is refused rather than silently rewritten when its bounds run
// backwards (B-32).

// The ramp phrasing per field, offered as an extra choice in the condition dropdown.
// Curated: a phrase exists only where more-of-the-number honestly means more reason to
import { type CSSProperties, useEffect, useId, useRef, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { api, type Condition, type CustomCondemn, type GradedKeep, type VocabField } from "../api";
import { FIELD_TO_GATE, FIELD_TO_SIGNAL, humanDays, OP_LABELS } from "./PolicyEditor";
import { usePopoverShift } from "./popoverFit";
import {
  FixedQuantity,
  QuantityInput,
  SIZE_UNITS,
  TIME_UNITS,
  useTypedNumber,
} from "./QuantityInput";
import { Segmented } from "./Segmented";

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
    // The spelling the review queue's filters use for the same resource; two spellings meant
    // two caches of one list, and whichever refreshed did not reach the other.
    queryKey: ["vocabulary-values", field.key],
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
  // The list is left-aligned to the input, which on a phone can be the last box on a wrapped row
  // and so flush with the right edge; this slides it back on screen (popoverFit.ts). Called above
  // the early return so the hook order holds for both branches -- a number field renders no list,
  // and the ref it reads is simply null.
  const popRef = useRef<HTMLUListElement>(null);
  const popShift = usePopoverShift(popRef, "suggest");

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
        <ul
          className="suggest-pop"
          role="listbox"
          id={listboxId}
          ref={popRef}
          style={{ "--pop-shift": `${popShift}px` } as CSSProperties}
        >
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
export function RemoveRulesEditor({
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
  const { data: condemnVocab, error: condemnVocabError } = useQuery({
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
  // The unitless ramp bounds are plain boxes (rule 40), but a number being typed lives in
  // the same one place it does everywhere else: clearing one to retype must not read as a
  // zero the next digits land after.
  const rampFrom = useTypedNumber(String(rFrom), setRFrom, { min: 0 });
  const rampTo = useTypedNumber(String(rTo), setRTo, { min: 0 });
  const field = condemnFields.find((f) => f.key === rField);
  const rampable = Boolean(
    field && RAMP_PHRASES[field.key] && field.type !== "bool" && field.type !== "text",
  );
  const isRamp = rOp === RAMP_OP;
  // A ramp builds pressure from its low bound UP to its high one, so the high one has to be
  // higher (engine/policy.py refuses floor >= saturate_at outright). This used to be clamped
  // away at add time with `Math.max(rFrom + 1, rTo)`, which silently rewrote the operator's
  // own bound: "from 5 years to 1 year" became 1826 days, and the saved row read back "(from
  // 5 years to 1826 days)" -- a rule nobody asked for, on the lane that removes files (B-32).
  // Refuse it beside the boxes instead, and add exactly what was typed.
  const rampBackwards = isRamp && rTo <= rFrom;

  // Re-seed the half-built rule when the operator picks a DIFFERENT field: its comparison,
  // its value and its ramp bounds all belong to the old field and would otherwise be carried
  // over. `rField` alone on purpose -- `condemnFields` is a fresh array on every render of
  // the vocabulary query, so including it would re-run this on every keystroke and wipe the
  // value being typed. The lint rule cannot see that, hence the suppression; every other one
  // in this codebase carries its reason too (H-3).
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
    if (!field || !rOp || rampBackwards) return;
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
          saturate_at: rTo,
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
        Add pressure to the score with a rule of your own. These can flag a title, but a protection
        still wins, and missing data only ever leans toward keeping.
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
              <button
                className="ghost sm"
                onClick={() => onCondemn(condemn.filter((_, j) => j !== i))}
              >
                Remove
              </button>
            </div>
          ))}
        </div>
      )}

      {/* An empty picker beside no explanation reads as "there is nothing to configure here",
          which is the wrong lesson to take from a failed fetch: say what happened instead, and
          drop the form rather than offer a dropdown with nothing in it. */}
      {condemnVocabError ? (
        <p className="notice notice-error">
          Reaper couldn't load the things a rule can look at, so there's nothing to pick from right
          now. Reload to try again. The rules you've already added are unaffected.
        </p>
      ) : (
        <>
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
                <select
                  value={rOp}
                  aria-label="Comparison"
                  onChange={(e) => setROp(e.target.value)}
                >
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
                      <input type="number" aria-label="Starts counting at" {...rampFrom} />
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
                      <input type="number" aria-label="Full effect at" {...rampTo} />
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
                  disabled={
                    rampBackwards || (!isRamp && field.type !== "bool" && rValue.trim() === "")
                  }
                >
                  Add rule
                </button>
              </>
            )}
          </div>
          {/* Beside the boxes that fix it (rule 42), and only while it is true. */}
          {rampBackwards && (
            <p className="help help-warn">
              The second number has to be higher than the first: a rule like this builds up between
              them.
            </p>
          )}
          {field?.help_text && <p className="help">{field.help_text}</p>}
          <p className="help">
            The choices match the field: numbers get at least / at most, words get is / is one of /
            contains. A phrase like “the older it is” builds pressure gradually between two numbers,
            like the built-in signals above, and its weight is a ceiling. There is no “is not”, on
            purpose.
          </p>
        </>
      )}
    </div>
  );
}

/** The owner's own keep rules, both strengths in one card: a rule can keep a title
 *  outright (a protection), or just lean toward keeping by lowering its score. Neither
 *  can ever flag anything -- the protect vocabulary is filtered server-side. */
export function KeepRulesEditor({
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
  const { data: vocab, error: vocabError } = useQuery({
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
  // The keep-rule twin of the re-seed above, and suppressed for the same reason: a new field
  // needs its own comparison and value, and `hardFields` is reallocated every render (H-3).
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
        <strong>lean toward keeping</strong> by lowering its score. Neither can ever flag anything,
        and missing data takes the full lean, to be safe.
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
                <button
                  className="ghost sm"
                  onClick={() => onKeeps(keeps.filter((_, j) => j !== i))}
                >
                  Remove
                </button>
              </div>
            );
          })}
        </div>
      )}

      {/* Same reason as the remove editor: a form with an empty dropdown looks like a feature
          with nothing behind it, so name the failure and drop the form. */}
      {vocabError ? (
        <p className="notice notice-error">
          Reaper couldn't load the things a rule can look at, so there's nothing to pick from right
          now. Reload to try again. The rules you've already added are unaffected.
        </p>
      ) : (
        <>
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
                <select
                  value={hField}
                  aria-label="Field"
                  onChange={(e) => setHField(e.target.value)}
                >
                  <option value="">Keep it when…</option>
                  {hardFields.map((f) => (
                    <option key={f.key} value={f.key}>
                      {f.label}
                    </option>
                  ))}
                </select>
                {hardField && (
                  <>
                    <select
                      value={hOp}
                      aria-label="Comparison"
                      onChange={(e) => setHOp(e.target.value)}
                    >
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
                <select
                  value={lField}
                  aria-label="Field"
                  onChange={(e) => setLField(e.target.value)}
                >
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
        </>
      )}
    </div>
  );
}
