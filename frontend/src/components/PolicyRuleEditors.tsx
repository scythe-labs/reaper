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
import { type CSSProperties, type RefObject, useEffect, useId, useRef, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { announce } from "../announce";
import { REMOVES_ITS_ROW, useRemovalFocus } from "../focus";
import { api, type Condition, type CustomCondemn, type GradedKeep, type VocabField } from "../api";
import { FIELD_TO_GATE, FIELD_TO_SIGNAL, humanDays, OP_LABELS } from "./PolicyEditor";
import { usePopoverShift } from "./popoverFit";
import {
  FixedQuantity,
  QuantityInput,
  SIZE_UNITS,
  TIME_UNITS,
  decimalsOfStep,
  useTypedNumber,
} from "./QuantityInput";
import { Segmented } from "./Segmented";
import { Notice } from "./Notice";

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

/** The backwards-ramp complaint, named once so the six boxes that can be wrong and the one
 *  sentence explaining it are the same string rather than seven that can drift (rule 67). */
const RAMP_ERROR_ID = "ramp-bounds-error";

/** Why an Add button is off, one id per editor so the three can be on screen together.
 *
 *  A `disabled` button is out of the Tab order, so a description hung on the BUTTON is
 *  unreachable by the operator it is for: the sentence binds to the empty box instead, which is
 *  both focusable and the thing that fixes it (rule 42). Same shape as `RAMP_ERROR_ID` above and
 *  the password row's `errorOwner` (#188). */
const CONDEMN_EMPTY_ID = "condemn-value-empty";
const HARD_EMPTY_ID = "keep-hard-value-empty";
const LEAN_EMPTY_ID = "keep-lean-at-empty";

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
  // The membership field reads as a sentence about the LIST, not as "field op value": the
  // value is the operator's own name for it. A stored rule whose list no longer exists still
  // renders, by that stored name.
  if (c.field === "on_list") return `Keep it when on your list ${String(c.value)}`;
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
  describedBy,
}: {
  field: VocabField;
  value: string;
  onChange: (v: string) => void;
  /** A sentence about this box, when there is one. Threaded to all three branches below: the
   *  branch a field takes is not the caller's business, and a prop honored by one of them is a
   *  message that vanishes on the other two (rule 72). */
  describedBy?: string | undefined;
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
  // The arrow keys move the active option without moving DOM focus, so the browser scrolls
  // nothing on their behalf: past the seventh option of a 14rem window the operator is marking
  // a value they cannot see, and ArrowUp from the top wraps straight to the last one (#333).
  // Only a KEYBOARD step scrolls -- `onMouseEnter` sets `highlight` too, and scrolling there
  // would drag the list out from under the pointer that is aiming at it.
  const steppedRef = useRef(false);
  useEffect(() => {
    if (!steppedRef.current) return;
    steppedRef.current = false;
    // "nearest" leaves an already-visible option where it is; the pane only moves when the
    // step left the window. Instant, like every other scroll here: smooth silently no-ops in
    // some environments, and a jump that always lands beats an animation that sometimes does
    // not (the reasoning DocsModal.tsx carries).
    popRef.current?.querySelector(".suggest-opt.active")?.scrollIntoView({ block: "nearest" });
  }, [highlight]);

  if (!isText) {
    // A number that has a unit wears it in the box, not in a placeholder that vanishes the
    // moment you type. Fields with no unit (a rank, a count) stay a plain box.
    return field.unit_suffix ? (
      <FixedQuantity
        value={value}
        suffix={field.unit_suffix}
        step={field.type === "rating_tenths" ? 0.1 : 1}
        ariaLabel={`${field.label} value`}
        describedBy={describedBy}
        onChange={(v) => onChange(String(v))}
      />
    ) : (
      <input
        type="number"
        value={value}
        aria-label={`${field.label} value`}
        aria-describedby={describedBy}
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
        aria-describedby={describedBy}
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
            steppedRef.current = true;
            setHighlight((h) => (h + 1) % matches.length);
          } else if (e.key === "ArrowUp") {
            e.preventDefault();
            steppedRef.current = true;
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
  const field = condemnFields.find((f) => f.key === rField);
  // The unitless ramp bounds are plain boxes (rule 40), but a number being typed lives in
  // the same one place it does everywhere else: clearing one to retype must not read as a
  // zero the next digits land after.
  //
  // Their precision is the chosen field's, decided by the same tenths test the `FixedQuantity`
  // siblings in this file make when they pick a `step` -- declared once here and read for both
  // the attribute and the bound, so the spinner and the value cannot disagree (rule 67). A
  // rating bound is in tenths; every other rampable field is whole, and a count ramp that could
  // start at 1.5 is #296's defect at a box that is not a `FixedQuantity` (rule 72).
  const rampStep = field?.type === "rating_tenths" ? 0.1 : 1;
  const rampBounds = { min: 0, decimals: decimalsOfStep(rampStep) };
  const rampFrom = useTypedNumber(String(rFrom), setRFrom, rampBounds);
  const rampTo = useTypedNumber(String(rTo), setRTo, rampBounds);
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
  // The other reason Add is off. A ramp reads its bounds and a yes/no always has an answer, so
  // only a typed value can be missing. It said nothing before: the button simply did not
  // respond, on a form whose next step is invisible (#188).
  const condemnEmpty =
    !isRamp && field !== undefined && field.type !== "bool" && rValue.trim() === "";

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
    // The new row appears in a list far above, the composer's boxes empty, and the Add button
    // disables itself -- three ways of saying nothing. Announced only on the branch that
    // actually added one: the guard above returns silently, so a no-op press and a successful
    // one were the same event to a reader.
    announce("Rule added.");
    // Picking the field again is where the operator continues, and clearing it below is what
    // unmounts the Add button they just pressed -- so without this the press that ADDS a rule
    // also drops focus to `<body>` (#173).
    fieldRef.current?.focus();
    setRField("");
    setRValue("");
  };
  // Removing a rule destroys the button holding focus. Focus goes to the next rule's Remove,
  // or to the field picker once the list is empty (#173).
  const fieldRef = useRef<HTMLSelectElement>(null);
  const rules = useRemovalFocus(fieldRef);

  return (
    <div className="rules-card">
      <h3>Your own reasons to remove</h3>
      <p className="blurb">
        Add pressure to the score with a rule of your own. These can flag a title, but a protection
        still wins, and missing data only ever leans toward keeping.
      </p>

      {condemn.length > 0 && (
        <div className="rules-table" ref={rules.ref as RefObject<HTMLDivElement>}>
          {condemn.map((r, i) => (
            <div className="rules-row rules-row-simple" key={`c-${r.name}-${i}`}>
              <span className="rules-rule">{describeCondemn(r, condemnAll)}</span>
              {/* A yes/no rule pays its full number the moment it matches, so it says the
                  number flat. A sliding rule ramps between its floor and its top, so it
                  says "up to". Removal weights total 100, so both are literal points. */}
              <span className="rules-weight-remove">
                {r.kind === "graded" ? `up to +${r.weight} points` : `+${r.weight} points`}
              </span>
              {/* Named for the rule it deletes. These lists stack one "Remove" per authored
                  rule across three tables on one page, and the sentence saying which rule is
                  in the sibling `.rules-rule` that no control referenced -- so pressing the
                  wrong one silently deletes a different rule. */}
              <button
                {...REMOVES_ITS_ROW}
                className="ghost sm"
                aria-label={`Remove rule: ${describeCondemn(r, condemnAll)}`}
                onClick={() => {
                  rules.removing(i);
                  onCondemn(condemn.filter((_, j) => j !== i));
                }}
              >
                Remove
              </button>
            </div>
          ))}
        </div>
      )}

      {/* An empty picker beside no explanation reads as "there is nothing to configure here",
          which is the wrong lesson to take from a failed fetch: say what happened instead, and
          drop the form rather than offer a dropdown with nothing in it.

          Only when the picker would BE empty, though. React Query keeps the last good vocabulary
          through a failed refetch, and `SetupWizard` fires a bare `invalidateQueries()` that
          refetches every key, so an undivided error took the whole add-rule form away while the
          list behind it was still in hand (#190). The vocabulary is a fixed server-defined list,
          not the operator's own data, so a held copy needs no staleness line: what a rule can
          look at does not change under them mid-session.

          It no longer says to reload either (#195), because it said so in the sentence before
          promising the rules already added were unaffected: those rules are unsaved draft state
          (`PolicyEditor` binds `onCondemn` to its own `update`), so the advice destroyed exactly
          what the next clause called safe. "Still here" is the honest half. */}
      {condemnVocabError && !condemnVocab ? (
        <Notice tone="error">
          Reaper couldn't load the things a rule can look at, so there's nothing to pick from right
          now. The rules you've already added are still here.
        </Notice>
      ) : (
        <>
          <div className="condition-add">
            <select
              ref={fieldRef}
              value={rField}
              aria-label="Field"
              onChange={(e) => setRField(e.target.value)}
            >
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
                    {/* All six boxes below carry the same pair, because the complaint is about
                        the PAIR: either number can be the one that is wrong, and the operator
                        may be standing on whichever they typed second (rule 72 -- three
                        branches per bound, and only fixing the branch in front of you leaves
                        the other two silent for a different `field.type`). */}
                    {field.type === "days" ? (
                      <QuantityInput
                        value={rFrom}
                        units={TIME_UNITS}
                        ariaLabel="Starts counting at"
                        describedBy={rampBackwards ? RAMP_ERROR_ID : undefined}
                        invalid={rampBackwards}
                        onChange={setRFrom}
                      />
                    ) : field.type === "bytes" ? (
                      <QuantityInput
                        value={rFrom}
                        units={SIZE_UNITS}
                        ariaLabel="Starts counting at"
                        describedBy={rampBackwards ? RAMP_ERROR_ID : undefined}
                        invalid={rampBackwards}
                        onChange={setRFrom}
                      />
                    ) : (
                      <input
                        type="number"
                        step={rampStep}
                        aria-label="Starts counting at"
                        aria-describedby={rampBackwards ? RAMP_ERROR_ID : undefined}
                        aria-invalid={rampBackwards ? true : undefined}
                        {...rampFrom}
                      />
                    )}
                    <span className="muted">to</span>
                    {field.type === "days" ? (
                      <QuantityInput
                        value={rTo}
                        units={TIME_UNITS}
                        ariaLabel="Full effect at"
                        describedBy={rampBackwards ? RAMP_ERROR_ID : undefined}
                        invalid={rampBackwards}
                        onChange={setRTo}
                      />
                    ) : field.type === "bytes" ? (
                      <QuantityInput
                        value={rTo}
                        units={SIZE_UNITS}
                        ariaLabel="Full effect at"
                        describedBy={rampBackwards ? RAMP_ERROR_ID : undefined}
                        invalid={rampBackwards}
                        onChange={setRTo}
                      />
                    ) : (
                      <input
                        type="number"
                        step={rampStep}
                        aria-label="Full effect at"
                        aria-describedby={rampBackwards ? RAMP_ERROR_ID : undefined}
                        aria-invalid={rampBackwards ? true : undefined}
                        {...rampTo}
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
                  <SuggestInput
                    field={field}
                    value={rValue}
                    onChange={setRValue}
                    describedBy={condemnEmpty ? CONDEMN_EMPTY_ID : undefined}
                  />
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
                <button className="ghost sm" onClick={add} disabled={rampBackwards || condemnEmpty}>
                  Add rule
                </button>
              </>
            )}
          </div>
          {/* Beside the boxes that fix it (rule 42), and only while it is true. */}
          {rampBackwards && (
            <p className="help help-warn" id={RAMP_ERROR_ID}>
              The second number has to be higher than the first: a rule like this builds up between
              them.
            </p>
          )}
          {condemnEmpty && (
            <p className="help help-warn" id={CONDEMN_EMPTY_ID}>
              Enter a value to add this rule.
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

/** The lists a membership rule can name, by the operator's own names for them. One select
 *  for both strengths (rule 72): the hard composer and the lean composer offer the same
 *  choice, so they render the same control. */
function ListNameSelect({
  value,
  names,
  onChange,
  describedBy,
}: {
  value: string;
  names: string[];
  onChange: (v: string) => void;
  describedBy?: string | undefined;
}) {
  return (
    <select
      value={value}
      aria-label="Which list"
      aria-describedby={describedBy}
      onChange={(e) => onChange(e.target.value)}
    >
      <option value="">Pick a list…</option>
      {names.map((n) => (
        <option key={n} value={n}>
          {n}
        </option>
      ))}
    </select>
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
  // A lean ramps a number, so only numeric fields (those that accept >=) can drive one --
  // plus the membership field, whose lean is FLAT: on the list takes the full discount, off
  // it takes none. Offered first, since a list is what most leans are about.
  const onListField = allFields.find((f) => f.key === "on_list");
  const leanFields = [
    ...(onListField ? [onListField] : []),
    ...allFields.filter((f) => f.ops.includes("gte")),
  ];

  // The names a membership rule can pick from. Read only while the vocabulary actually
  // offers the field (rule 66: the server decides what is authorable), which also keeps the
  // query out of every test tree that never composes one.
  const lists = useQuery({
    queryKey: ["lists-configured"],
    queryFn: api.listConfigs,
    enabled: onListField !== undefined,
  });
  const listNames = (lists.data ?? []).map((l) => l.name);
  // Why the list select is empty, said once for both composers (rule 72). A failed read is
  // named as one, never shown as "you have no lists" (rules 17/36).
  const noListsMessage =
    !lists.isPending && listNames.length === 0
      ? lists.isError
        ? "Reaper couldn't load your lists, so there's nothing to pick from right now."
        : "You have no lists yet. Add one on Settings → Lists first."
      : null;

  const [strength, setStrength] = useState<"hard" | "lean">("hard");

  // --- keep it outright ------------------------------------------------------
  const [hField, setHField] = useState("");
  const [hOp, setHOp] = useState("");
  const [hValue, setHValue] = useState("");
  const hardField = hardFields.find((f) => f.key === hField);
  const hardIsList = hardField?.key === "on_list";
  // Why Add is off here, the condemn composer's twin (rule 72). A yes/no always has an answer,
  // so only a typed value -- or an unpicked list -- can be missing.
  const hardEmpty = hardField !== undefined && hardField.type !== "bool" && hValue.trim() === "";
  // The keep-rule twin of the re-seed above, and suppressed for the same reason: a new field
  // needs its own comparison and value, and `hardFields` is reallocated every render (H-3).
  useEffect(() => {
    const f = hardFields.find((x) => x.key === hField);
    if (!f) return;
    setHOp(f.ops[0] ?? "");
    setHValue(f.type === "bool" ? "true" : "");
  }, [hField]); // eslint-disable-line react-hooks/exhaustive-deps
  const addHard = () => {
    if (!hardField || (!hOp && !hardIsList)) return;
    onConditions([
      ...conditions,
      hardIsList
        ? // A list is a membership, not a comparison: the one op is "is", so no op select
          // renders and none is read.
          { field: hardField.key, op: "eq", value: hValue }
        : { field: hardField.key, op: hOp, value: coerceValue(hardField, hValue) },
    ]);
    // Same silence as the condemn composer's Add, on the same guard (rule 72).
    announce("Rule added.");
    // And the same focus drop: clearing the field below unmounts the Add button.
    hardFieldRef.current?.focus();
    setHField("");
    setHValue("");
  };

  // --- lean toward keeping ---------------------------------------------------
  const [lField, setLField] = useState("");
  const [lPoints, setLPoints] = useState(15);
  const [lAt, setLAt] = useState("");
  const [lDir, setLDir] = useState<"high_keeps" | "low_keeps">("high_keeps");
  // The membership lean's own list, apart from `lAt`: the number box keeps its draft across
  // field changes on purpose, and a held "365" must not be sendable as a list name.
  const [lList, setLList] = useState("");
  const leanField = leanFields.find((f) => f.key === lField);
  const leanIsList = leanField?.key === "on_list";
  // And the third (rule 72). A numeric lean waits on its number; a membership lean on its list.
  const leanEmpty = leanField !== undefined && (leanIsList ? lList === "" : lAt === "");
  const addLean = () => {
    if (!leanField || leanEmpty) return;
    if (leanIsList) {
      // Flat, not a ramp: on the list takes the full discount, off it takes none, and a
      // membership that could not be read takes the full one, fail-closed like every keep.
      // The ramp fields are inert; floor 0 / saturate 1 is the shape the server expects.
      // The name clamps to the server's 60-character bound before `uniqueName` suffixes it.
      onKeeps([
        ...keeps,
        {
          name: uniqueName(keeps, lList.slice(0, 56)),
          field: leanField.key,
          value: lList,
          max_discount: lPoints,
          floor: 0,
          saturate_at: 1,
          direction: "high_keeps",
        },
      ]);
    } else {
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
    }
    // And the third (rule 72).
    announce("Rule added.");
    leanFieldRef.current?.focus();
    setLField("");
    setLAt("");
    setLList("");
  };
  const hardFieldRef = useRef<HTMLSelectElement>(null);
  const leanFieldRef = useRef<HTMLSelectElement>(null);
  // ONE list for focus purposes even though it is two arrays: both render into the same
  // `.rules-table`, so the position a removed row leaves behind is its position in the
  // combined run. A lean rule at index `i` therefore aims at `conditions.length + i`.
  // The fallback is the composer's field picker, whichever strength is showing.
  const rows = useRemovalFocus(strength === "hard" ? hardFieldRef : leanFieldRef);

  return (
    <div className="rules-card">
      <h3>Your own keep rules</h3>
      <p className="blurb">
        Two strengths: a rule can keep a title <strong>outright</strong>, or just{" "}
        <strong>lean toward keeping</strong> by lowering its score. Neither can ever flag anything,
        and missing data takes the full lean, to be safe.
      </p>

      {(conditions.length > 0 || keeps.length > 0) && (
        <div className="rules-table" ref={rows.ref as RefObject<HTMLDivElement>}>
          {conditions.map((c, i) => (
            <div className="rules-row rules-row-simple" key={`h-${c.field}-${c.op}-${i}`}>
              <span className="rules-rule">
                <span className="rule-kind">
                  Keeps it, always<span aria-hidden="true"> · </span>
                </span>
                {describeCondition(c, allFields)}
              </span>
              <span className="rules-weight-keep">kept outright</span>
              <button
                {...REMOVES_ITS_ROW}
                className="ghost sm"
                aria-label={`Remove rule: keeps it, always, ${describeCondition(c, allFields)}`}
                onClick={() => {
                  rows.removing(i);
                  onConditions(conditions.filter((_, j) => j !== i));
                }}
              >
                Remove
              </button>
            </div>
          ))}
          {keeps.map((k, i) => {
            const f = allFields.find((x) => x.key === k.field);
            // A membership lean is a sentence about the list, by its stored name -- which
            // still renders after the list itself is gone, so the rule can be found and
            // removed rather than orphaned invisibly.
            const sentence =
              k.value != null
                ? `On your list ${k.value}`
                : `${f?.label ?? k.field}: the ${
                    k.direction === "low_keeps" ? "less" : "more"
                  }, the safer (full effect at ${rampValue(f, k.saturate_at)})`;
            return (
              <div className="rules-row rules-row-simple" key={`k-${k.name}-${i}`}>
                <span className="rules-rule">
                  <span className="rule-kind">
                    Leans<span aria-hidden="true"> · </span>
                  </span>
                  {sentence}
                </span>
                <span className="rules-weight-keep">
                  lowers the score, up to −{k.max_discount} points
                </span>
                <button
                  {...REMOVES_ITS_ROW}
                  className="ghost sm"
                  // The whole visible sentence, not just the field. Two lean rules on one
                  // field are a supported setup -- `addLean` runs the name through
                  // `uniqueName` precisely because they collide -- and the field alone gives
                  // both Remove buttons the same name. What gets removed by mistake is a keep
                  // rule, so the next scan condemns titles the operator believed were held.
                  aria-label={`Remove rule: leans, ${sentence}, up to ${k.max_discount} points off`}
                  onClick={() => {
                    rows.removing(conditions.length + i);
                    onKeeps(keeps.filter((_, j) => j !== i));
                  }}
                >
                  Remove
                </button>
              </div>
            );
          })}
        </div>
      )}

      {/* Same reason as the remove editor, divided the same way (rule 72): a form with an empty
          dropdown looks like a feature with nothing behind it, so name the failure and drop the
          form -- but only when the dropdown really would be empty. */}
      {vocabError && !vocab ? (
        <Notice tone="error">
          Reaper couldn't load the things a rule can look at, so there's nothing to pick from right
          now. The rules you've already added are still here.
        </Notice>
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
                  ref={hardFieldRef}
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
                    {/* A membership rule's one comparison is "is", so no op select renders:
                        a dropdown with a single choice is a control that cannot be used. */}
                    {!hardIsList && (
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
                    )}
                    {hardIsList ? (
                      <ListNameSelect
                        value={hValue}
                        names={listNames}
                        onChange={setHValue}
                        describedBy={hardEmpty ? HARD_EMPTY_ID : undefined}
                      />
                    ) : hardField.type === "bool" ? (
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
                      <SuggestInput
                        field={hardField}
                        value={hValue}
                        onChange={setHValue}
                        describedBy={hardEmpty ? HARD_EMPTY_ID : undefined}
                      />
                    )}
                    <button className="ghost sm" onClick={addHard} disabled={hardEmpty}>
                      Add rule
                    </button>
                  </>
                )}
                {/* Beside the box that fixes it (rule 42), and bound to it: a disabled button
                    is out of the Tab order, so the sentence has to live on the box. */}
                {hardEmpty && (
                  <p className="help help-warn" id={HARD_EMPTY_ID}>
                    {hardIsList
                      ? "Pick a list to add this rule."
                      : "Enter a value to add this rule."}
                  </p>
                )}
                {hardIsList && noListsMessage && <p className="help help-warn">{noListsMessage}</p>}
                {hardField?.help_text && <p className="help">{hardField.help_text}</p>}
              </div>
            ) : (
              <div className="condition-add">
                <select
                  ref={leanFieldRef}
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
                    {leanIsList ? (
                      // Flat: on the list takes the full discount, so there is no direction
                      // and no ramp end to set -- membership isn't a number.
                      <ListNameSelect
                        value={lList}
                        names={listNames}
                        onChange={setLList}
                        describedBy={leanEmpty ? LEAN_EMPTY_ID : undefined}
                      />
                    ) : (
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
                            describedBy={leanEmpty ? LEAN_EMPTY_ID : undefined}
                            onChange={(v) => setLAt(String(v))}
                          />
                        ) : (
                          <input
                            type="number"
                            value={lAt}
                            aria-label="Full effect at"
                            aria-describedby={leanEmpty ? LEAN_EMPTY_ID : undefined}
                            onChange={(e) => setLAt(e.target.value)}
                          />
                        )}
                      </>
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
                    <button className="ghost sm" onClick={addLean} disabled={leanEmpty}>
                      Add rule
                    </button>
                  </>
                )}
                {/* And the third (rule 72). */}
                {leanEmpty && (
                  <p className="help help-warn" id={LEAN_EMPTY_ID}>
                    {leanIsList
                      ? "Pick a list to add this rule."
                      : "Enter a number to add this rule."}
                  </p>
                )}
                {leanIsList && noListsMessage && <p className="help help-warn">{noListsMessage}</p>}
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
